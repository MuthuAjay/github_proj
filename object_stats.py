#!/usr/bin/env python3
"""
object_stats.py - per-repo git object store stats: how many .pack files,
how many loose objects, how many objects of each type (commit/tree/blob/
tag), and their sizes - both on-disk (compressed) and logical/uncompressed
content size. Writes one row per repo to a CSV.

Deliberately filesystem-based, not `git count-objects`: that command needs
git to recognize the repo at all (valid HEAD/refs/config), which fails
outright on repos like the ones this project has been investigating -
archival dumps with only .git/objects/ surviving, or repos with a
corrupted config. This script instead:

  - counts and sizes .pack files directly (os.listdir/os.path.getsize -
    no git needed at all for that part).
  - reads each pack's own object index via `git verify-pack -v <pack>`,
    which needs no repo context either - it operates on the pack file by
    itself. This gives per-object type and uncompressed size.
  - reads loose objects (individual files under objects/xx/...) with pure
    Python + zlib: a loose object is just a zlib-deflated "<type>
    <size>\\0<content>" blob, so its header is decodable directly, no git
    subprocess and no repo context needed for that either.

So a repo with a totally broken .git (no HEAD, no refs, no config - only
objects/ survived) still gets a full, accurate report here.

Usage:
    object_stats.py /path/to/one/repo --out stats.csv
    object_stats.py /path/to/Test /path/to/archive --out stats.csv
    object_stats.py /mnt/ntfsdrive/.../AllRepos --out stats.csv --scan-workers 24
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
import zlib
from collections import Counter

SENTINEL = object()
OBJ_TYPES = ("commit", "tree", "blob", "tag")

# Repos on foreign-uid mounts (Windows drives under /mnt, network shares) trip
# git's "detected dubious ownership" check, which aborts every plumbing call
# before it emits a byte. safe.directory is only honoured from protected
# config - system, global, or the command line - so pass it per-invocation
# here rather than relying on the machine's global git config.
GIT = ["git", "-c", "safe.directory=*"]


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD"))):
        return path
    return None


# --------------------------------------------------------------- pack side

def find_pack_paths(git_dir):
    pack_dir = os.path.join(git_dir, "objects", "pack")
    try:
        return sorted(os.path.join(pack_dir, f)
                      for f in os.listdir(pack_dir) if f.endswith(".pack"))
    except OSError:
        return []


def verify_pack_objects(pack_path):
    """sha -> (type, uncompressed_size), read straight from a pack's own
    index. No repo context needed - works even if git can't recognize the
    surrounding directory as a repository at all. SLOW: verify-pack does a
    full cryptographic re-verification of every object (decompress, walk
    every delta chain, recheck hashes) - this is the fallback path for
    repos git can't recognize, not the default."""
    out = {}
    p = subprocess.run(["git", "verify-pack", "-v", pack_path],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in p.stdout.splitlines():
        bits = line.split()
        if len(bits) >= 3 and len(bits[0]) == 40 and bits[1] in OBJ_TYPES:
            try:
                out[bits[0]] = (bits[1], int(bits[2]))
            except ValueError:
                out[bits[0]] = (bits[1], 0)
    return out


def repo_recognized(repo_path):
    """Does git consider this a valid repository at all (resolvable HEAD/
    refs/config)? Cheap check - just repo discovery, no object reading."""
    return subprocess.run(
        GIT + ["-C", repo_path, "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def fast_object_check(repo_path):
    """sha -> (type, uncompressed_size) for EVERY object - loose and packed
    together - via one cheap batch-check pass over the whole object store.
    This is a metadata LOOKUP (reads each object's header straight out of
    the pack index / delta chain), not a verification, so it's dramatically
    faster than verify_pack_objects() on large packs. Needs git to
    recognize the repo; returns None (signalling "fall back") if it can't
    run at all."""
    try:
        p = subprocess.Popen(
            GIT + ["-C", repo_path, "cat-file", "--batch-all-objects",
                  "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return None
    out = {}
    try:
        for line in p.stdout:
            bits = line.split()
            if len(bits) == 3 and bits[1] in OBJ_TYPES:
                try:
                    out[bits[0]] = (bits[1], int(bits[2]))
                except ValueError:
                    pass
    finally:
        p.stdout.close()
        p.wait()
    return out if (out or p.returncode == 0) else None


# -------------------------------------------------------------- loose side

def loose_object_paths(git_dir):
    """(sha, path) for every loose object file under objects/xx/... (skips
    pack/ and info/). The sha is just the shard name + filename - free,
    no file open or decompression needed to know it."""
    objects_dir = os.path.join(git_dir, "objects")
    try:
        shard_names = os.listdir(objects_dir)
    except OSError:
        return
    for shard in shard_names:
        if shard in ("pack", "info") or len(shard) != 2:
            continue
        shard_dir = os.path.join(objects_dir, shard)
        try:
            entries = os.listdir(shard_dir)
        except OSError:
            continue
        for fn in entries:
            yield shard + fn, os.path.join(shard_dir, fn)


def read_loose_object_header(path):
    """(type, uncompressed_size) for one loose object, or (None, None) if it
    doesn't decode as one. Reads and inflates only a small prefix - never
    the whole object - since the header is all that's needed."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(256)
    except OSError:
        return None, None
    d = zlib.decompressobj()
    try:
        header = d.decompress(raw, 64)
    except zlib.error:
        return None, None
    if b"\0" not in header:
        return None, None
    hdr = header.split(b"\0", 1)[0]
    parts = hdr.split(b" ")
    if len(parts) != 2 or parts[0].decode("ascii", "replace") not in OBJ_TYPES:
        return None, None
    try:
        return parts[0].decode(), int(parts[1])
    except ValueError:
        return None, None


# ------------------------------------------------------------- per-repo

def analyze_repo(repo_path, git_dir):
    packs = find_pack_paths(git_dir)
    pack_disk_bytes = sum(os.path.getsize(p) for p in packs if os.path.exists(p))

    loose = list(loose_object_paths(git_dir))       # [(sha, path), ...]
    loose_disk_bytes = sum(os.path.getsize(p) for _sha, p in loose
                           if os.path.exists(p))
    loose_shas = {sha for sha, _path in loose}

    packed_types = Counter()
    packed_content_bytes = 0
    loose_types = Counter()
    loose_content_bytes = 0
    loose_unparsed = 0

    fast = fast_object_check(repo_path) if repo_recognized(repo_path) else None

    if fast is not None:
        # Fast path: one cheap metadata pass covers loose + packed objects
        # together: split the results using the loose shas we already have
        # from the filesystem, instead of running the expensive per-pack
        # verify-pack.
        matched_loose = 0
        for sha, (t, size) in fast.items():
            if sha in loose_shas:
                loose_types[t] += 1
                loose_content_bytes += size
                matched_loose += 1
            else:
                packed_types[t] += 1
                packed_content_bytes += size
        loose_unparsed = len(loose_shas) - matched_loose
        note = ""
    else:
        # Fallback: git doesn't recognize this repo at all (broken HEAD/
        # refs/config) - fast_object_check can't run. verify-pack works on
        # a pack file with no repo context needed, so use that per pack
        # instead; loose objects are read directly via zlib either way.
        for pack in packs:
            for _sha, (t, size) in verify_pack_objects(pack).items():
                packed_types[t] += 1
                packed_content_bytes += size
        for _sha, path in loose:
            t, size = read_loose_object_header(path)
            if t:
                loose_types[t] += 1
                loose_content_bytes += size or 0
            else:
                loose_unparsed += 1
        # verify-pack -v's "size" column reports the DELTA size, not the
        # reconstructed content size, for any object stored as a delta -
        # so packed_content_bytes is an undercount here. Only affects this
        # fallback path (repos git can't recognize); the fast path above
        # reports true canonical sizes.
        note = ("packed_content_bytes is approximate: repo has no "
                "resolvable HEAD/refs, so sizes came from verify-pack, "
                "which under-reports delta-compressed objects' true size")

    loose_objects = sum(loose_types.values()) + loose_unparsed
    packed_objects = sum(packed_types.values())

    return {
        "note": note,
        "pack_files": len(packs), "pack_disk_bytes": pack_disk_bytes,
        "packed_objects": packed_objects, "packed_content_bytes": packed_content_bytes,
        "packed_commits": packed_types["commit"], "packed_trees": packed_types["tree"],
        "packed_blobs": packed_types["blob"], "packed_tags": packed_types["tag"],
        "loose_objects": loose_objects, "loose_disk_bytes": loose_disk_bytes,
        "loose_content_bytes": loose_content_bytes, "loose_unparsed": loose_unparsed,
        "loose_commits": loose_types["commit"], "loose_trees": loose_types["tree"],
        "loose_blobs": loose_types["blob"], "loose_tags": loose_types["tag"],
        "total_objects": packed_objects + loose_objects,
        "total_commits": packed_types["commit"] + loose_types["commit"],
        "total_trees": packed_types["tree"] + loose_types["tree"],
        "total_blobs": packed_types["blob"] + loose_types["blob"],
        "total_tags": packed_types["tag"] + loose_types["tag"],
    }


# --------------------------------------------------------------------- walk

class PendingCounter:
    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()
        self.done = threading.Event()

    def inc(self, k=1):
        with self._lock:
            self._n += k

    def dec(self):
        with self._lock:
            self._n -= 1
            if self._n <= 0:
                self.done.set()


def process_dir(cur_dir, args, dir_queue, pending, task_q):
    git_dir = get_git_dir(cur_dir)
    if git_dir:
        task_q.put((cur_dir, git_dir))
        return

    try:
        entries = list(os.scandir(cur_dir))
    except OSError as exc:
        print(f"warning: cannot list {cur_dir}: {exc}", file=sys.stderr)
        return

    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=args.follow_symlinks)
            is_symlink = entry.is_symlink()
        except OSError as exc:
            print(f"warning: cannot stat {entry.path}: {exc}", file=sys.stderr)
            continue
        if not is_dir or (is_symlink and not args.follow_symlinks):
            continue
        pending.inc()
        dir_queue.put(os.path.join(cur_dir, entry.name))


def scanner_loop(args, dir_queue, pending, task_q):
    while True:
        try:
            cur_dir = dir_queue.get(timeout=0.2)
        except queue.Empty:
            if pending.done.is_set():
                return
            continue
        try:
            process_dir(cur_dir, args, dir_queue, pending, task_q)
        finally:
            pending.dec()


def stats_worker(task_q, result_q):
    while True:
        item = task_q.get()
        if item is SENTINEL:
            result_q.put(SENTINEL)
            return
        repo_path, git_dir = item
        try:
            stats = analyze_repo(repo_path, git_dir)
        except Exception as exc:
            stats = {k: 0 for k in (
                "pack_files", "pack_disk_bytes", "packed_objects",
                "packed_content_bytes", "packed_commits", "packed_trees",
                "packed_blobs", "packed_tags", "loose_objects",
                "loose_disk_bytes", "loose_content_bytes", "loose_unparsed",
                "loose_commits", "loose_trees", "loose_blobs", "loose_tags",
                "total_objects", "total_commits", "total_trees",
                "total_blobs", "total_tags")}
            stats["note"] = repr(exc)
        name = os.path.basename(repo_path.rstrip(os.sep)) or repo_path
        result_q.put((name, repo_path, stats))


CSV_COLS = [
    "repo_name", "full_path",
    "pack_files", "pack_disk_bytes",
    "packed_objects", "packed_content_bytes",
    "packed_commits", "packed_trees", "packed_blobs", "packed_tags",
    "loose_objects", "loose_disk_bytes", "loose_content_bytes", "loose_unparsed",
    "loose_commits", "loose_trees", "loose_blobs", "loose_tags",
    "total_objects", "total_commits", "total_trees", "total_blobs", "total_tags",
    "note",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--workers", type=int, default=4,
                    help="per-repo analysis worker threads (default 4) - "
                         "these spawn `git verify-pack` per pack file")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads (default 8)")
    ap.add_argument("--follow-symlinks", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no per-repo progress output")
    args = ap.parse_args()

    for p in args.paths:
        if not os.path.isdir(p):
            print(f"not a directory: {p}", file=sys.stderr)
            return 2

    dir_queue = queue.Queue()
    pending = PendingCounter()
    task_q = queue.Queue(maxsize=2000)
    result_q = queue.Queue(maxsize=2000)

    for p in args.paths:
        pending.inc()
        dir_queue.put(os.path.abspath(p))

    def closer():
        pending.done.wait()
        for _ in range(args.workers):
            task_q.put(SENTINEL)

    threads = [threading.Thread(target=scanner_loop, daemon=True,
                                args=(args, dir_queue, pending, task_q))
              for _ in range(args.scan_workers)]
    threads.append(threading.Thread(target=closer, daemon=True))
    threads += [threading.Thread(target=stats_worker, daemon=True,
                                 args=(task_q, result_q))
               for _ in range(args.workers)]
    for t in threads:
        t.start()

    import csv
    rows = []
    sentinels_seen = 0
    start = time.time()
    spinner = "|/-\\"
    last_render = 0.0

    def render(final=False):
        elapsed = time.time() - start
        n = len(rows)
        rate = n / elapsed if elapsed else 0
        total_packs = sum(s["pack_files"] for _, _, s in rows)
        total_loose = sum(s["loose_objects"] for _, _, s in rows)
        frame = "done" if final else spinner[n % len(spinner)]
        line = (f"\r  [{frame}] {n:,} repo(s) | {total_packs:,} pack file(s) | "
                f"{total_loose:,} loose object(s) | {rate:.1f} repo/s | {elapsed:,.0f}s")
        print(line.ljust(90), end="\n" if final else "", file=sys.stderr, flush=True)

    while sentinels_seen < args.workers:
        item = result_q.get()
        if item is SENTINEL:
            sentinels_seen += 1
            continue
        name, path, stats = item
        rows.append((name, path, stats))
        now = time.time()
        if not args.quiet and (now - last_render) >= 0.2:
            render()
            last_render = now

    if not args.quiet:
        render(final=True)

    for t in threads:
        t.join()

    rows.sort(key=lambda r: r[1])
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for name, path, stats in rows:
            w.writerow([name, path] + [stats.get(c, "") for c in CSV_COLS[2:]])

    elapsed = time.time() - start
    total_packs = sum(s["pack_files"] for _, _, s in rows)
    total_loose = sum(s["loose_objects"] for _, _, s in rows)
    print(f"\n{len(rows)} repo(s): {total_packs:,} pack file(s), "
          f"{total_loose:,} loose object(s), {elapsed:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
