#!/usr/bin/env python3
"""
pack_redundancy.py - local-disk equivalent of getunique.py's Azure blob
audit: which repos' pack files are entirely redundant (every object they
contain already exists in some OTHER, bigger pack somewhere under the same
scan root), and which contribute content found nowhere else.

Reads each repo's .idx file(s) directly off disk and hand-parses the raw
git pack-idx v2 format - no `git` subprocess per repo, so this stays fast
across tens of thousands of repos. Repo discovery reuses the same
filesystem-only pattern as pack_count.py / object_stats.py (get_git_dir +
a scanner/worker thread pool).

This answers a different question than compare_repos.py's history mode:
that tool checks "does repo B have content repo A doesn't" for a SPECIFIC
pair you name. This tool checks "does ANY repo under this root have
content that exists nowhere else under this root" - across the WHOLE
estate at once, without you needing to already suspect which pairs might
overlap.

Caveats carried over from getunique.py, unchanged here:
  - Only handles SHA-1 (20-byte hash) idx files; a SHA-256 repo's idx is
    detected and rejected (reported as a parse failure), not misparsed.
  - "100% redundant" is evaluated against the cumulative pool of every
    BIGGER pack processed so far (largest-first) - order-dependent by
    design: with N-way identical packs, the largest is credited as
    "unique" and the rest as "redundant", not an arbitrary one of them.
  - A repo can have more than one pack; each pack is evaluated on its own
    the same way a repo is here (the CSV's unit is "pack", tagged with
    which repo it came from).

Usage:
    pack_redundancy.py /path/to/one/repo --out redundancy.csv
    pack_redundancy.py /path/to/Test /path/to/archive --out redundancy.csv
    pack_redundancy.py /mnt/.../AllRepos --out redundancy.csv --scan-workers 24
"""

import argparse
import csv
import os
import queue
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

SENTINEL = object()
HASH_LEN = 20   # SHA-1 only - see parse_idx_bytes()


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


def find_pack_idx_pairs(git_dir):
    """[(pack_path, idx_path, pack_size_bytes), ...] for every pack in this repo."""
    pack_dir = os.path.join(git_dir, "objects", "pack")
    try:
        names = os.listdir(pack_dir)
    except OSError:
        return []
    names = set(names)
    pairs = []
    for n in names:
        if not n.endswith(".pack"):
            continue
        idx_name = n[:-5] + ".idx"
        if idx_name in names:
            pack_path = os.path.join(pack_dir, n)
            idx_path = os.path.join(pack_dir, idx_name)
            try:
                size = os.path.getsize(pack_path)
            except OSError:
                size = 0
            pairs.append((pack_path, idx_path, size))
    return pairs


def parse_idx_bytes(idx_bytes, label=""):
    """Same parser (and same SHA-1-only validation guard) as getunique.py -
    kept in sync deliberately rather than imported, since that script talks
    to Azure and has no local-file mode of its own to import from."""
    try:
        if len(idx_bytes) < 8:
            print(f"Error parsing {label}: too short to be a pack idx")
            return None
        magic = idx_bytes[0:4]
        if magic != b'\xfftOc':
            print(f"Error parsing {label}: not a git pack idx (bad magic)")
            return None
        version = struct.unpack('>I', idx_bytes[4:8])[0]
        if version != 2:
            print(f"Error parsing {label}: idx version {version} unsupported "
                  f"(only v2 handled)")
            return None

        fanout_start = 8
        total_objects = struct.unpack(
            '>I', idx_bytes[fanout_start + 255 * 4: fanout_start + 256 * 4])[0]
        sha_table_start = fanout_start + 256 * 4   # == 1032

        needed = total_objects * HASH_LEN
        if len(idx_bytes) - sha_table_start < needed:
            print(f"Error parsing {label}: truncated - needs {needed} bytes "
                  f"for {total_objects} SHA-1 hashes, only "
                  f"{len(idx_bytes) - sha_table_start} available")
            return None

        trailing = len(idx_bytes) - (sha_table_start + needed)
        min_trailing = total_objects * 8 + 40
        max_trailing = total_objects * 16 + 40
        if not (min_trailing <= trailing <= max_trailing):
            print(f"Error parsing {label}: idx layout doesn't match a SHA-1 "
                  f"v2 index ({trailing} trailing bytes, expected "
                  f"{min_trailing}-{max_trailing}) - likely a SHA-256 "
                  f"repository (unsupported) or a corrupted idx")
            return None

        hashes = set()
        pos = sha_table_start
        for _ in range(total_objects):
            hashes.add(idx_bytes[pos:pos + HASH_LEN].hex())
            pos += HASH_LEN
        return hashes
    except Exception as e:
        print(f"Error parsing idx bytes for {label}: {e}")
        return None


def read_idx(path):
    with open(path, "rb") as fh:
        return fh.read()


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
        repo_name = os.path.basename(cur_dir.rstrip(os.sep)) or cur_dir
        for pack_path, idx_path, size in find_pack_idx_pairs(git_dir):
            task_q.put((repo_name, cur_dir, pack_path, idx_path, size))
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


def _fetch_and_parse(repo_name, repo_path, pack_path, idx_path, size):
    idx_bytes = read_idx(idx_path)
    objs = parse_idx_bytes(idx_bytes, label=f"{repo_name} ({idx_path})")
    return repo_name, repo_path, pack_path, size, objs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads (default 8)")
    ap.add_argument("--parse-workers", type=int, default=16,
                    help="idx read+parse threads (default 16) - reading "
                         "many small files benefits from concurrency even "
                         "on local disk if there are thousands of them")
    ap.add_argument("--follow-symlinks", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="less progress output")
    args = ap.parse_args()

    for p in args.paths:
        if not os.path.isdir(p):
            print(f"not a directory: {p}", file=sys.stderr)
            return 2

    dir_queue = queue.Queue()
    pending = PendingCounter()
    task_q = queue.Queue()

    for p in args.paths:
        pending.inc()
        dir_queue.put(os.path.abspath(p))

    spinner = "|/-\\"

    def render(prefix, n, total, elapsed, final=False):
        frame = "done" if final else spinner[n % len(spinner)]
        pct = f"{n}/{total} " if total else f"{n} "
        rate = n / elapsed if elapsed else 0
        line = f"\r  [{frame}] {prefix} {pct}| {rate:.1f}/s | {elapsed:,.0f}s"
        print(line.ljust(80), end="\n" if final else "", file=sys.stderr, flush=True)

    discover_start = time.time()
    threads = [threading.Thread(target=scanner_loop, daemon=True,
                                args=(args, dir_queue, pending, task_q))
              for _ in range(args.scan_workers)]
    for t in threads:
        t.start()
    last_render = 0.0
    while not pending.done.is_set():
        pending.done.wait(timeout=0.2)
        now = time.time()
        if not args.quiet and (now - last_render) >= 0.2:
            render("discovering repos", task_q.qsize(), 0, now - discover_start)
            last_render = now
    for t in threads:
        t.join()
    if not args.quiet:
        render("discovering repos", task_q.qsize(), 0, time.time() - discover_start, final=True)

    packs = []
    while not task_q.empty():
        packs.append(task_q.get_nowait())
    if not args.quiet:
        print(f"found {len(packs)} pack(s) across the scanned path(s)", file=sys.stderr)

    parsed = {}          # pack_path -> (repo_name, repo_path, size_bytes, objs)
    parse_failures = []
    start = time.time()
    done = 0
    last_render = 0.0
    with ThreadPoolExecutor(max_workers=args.parse_workers) as pool:
        futures = [pool.submit(_fetch_and_parse, *p) for p in packs]
        for fut in as_completed(futures):
            repo_name, repo_path, pack_path, size, objs = fut.result()
            done += 1
            now = time.time()
            if not args.quiet and (now - last_render) >= 0.2:
                render("parsing idx files", done, len(packs), now - start)
                last_render = now
            if objs is None:
                parse_failures.append((repo_name, repo_path, pack_path))
            else:
                parsed[pack_path] = (repo_name, repo_path, size, objs)
    if not args.quiet:
        render("parsing idx files", done, len(packs), time.time() - start, final=True)

    # Largest pack first: the biggest, most-complete pack in any duplicate
    # cluster is credited as "unique"; identical/subset packs after it show
    # up as fully redundant.
    order = sorted(parsed.items(), key=lambda kv: kv[1][2], reverse=True)

    # master_pool maps each object hash to the pack_path that FIRST
    # contributed it (always a bigger-or-equal pack, since we go
    # largest-first) - that's what lets us report which specific pack a
    # redundant/overlapping pack's objects actually came from, instead of
    # just a bare "redundant" flag with no explanation.
    master_pool = {}
    rows = []
    for pack_path, (repo_name, repo_path, size, objs) in order:
        already = {}   # origin_pack_path -> count of THIS pack's objects it explains
        new_objects = set()
        for h in objs:
            origin = master_pool.get(h)
            if origin is None:
                new_objects.add(h)
            else:
                already[origin] = already.get(origin, 0) + 1

        status = "redundant" if not new_objects else "unique"
        if new_objects:
            for h in new_objects:
                master_pool[h] = pack_path

        if already:
            best_pack, best_count = max(already.items(), key=lambda kv: kv[1])
            best_repo = parsed[best_pack][0]
            match_pct = round(100 * best_count / len(objs), 1) if objs else 0.0
        else:
            best_pack, best_repo, best_count, match_pct = "", "", 0, 0.0

        rows.append({
            "repo_name": repo_name, "full_path": repo_path,
            "pack_path": pack_path, "pack_mb": round(size / (1024 * 1024), 2),
            "total_objects": len(objs), "new_objects": len(new_objects),
            "status": status,
            "matched_repo": best_repo, "matched_pack_path": best_pack,
            "matched_object_count": best_count, "matched_pct": match_pct,
        })

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["repo_name", "full_path", "pack_path", "pack_mb",
                   "total_objects", "new_objects", "status",
                   "matched_repo", "matched_pack_path",
                   "matched_object_count", "matched_pct"])
        for r in rows:
            w.writerow([r["repo_name"], r["full_path"], r["pack_path"],
                       r["pack_mb"], r["total_objects"], r["new_objects"],
                       r["status"], r["matched_repo"], r["matched_pack_path"],
                       r["matched_object_count"], r["matched_pct"]])

    redundant = [r for r in rows if r["status"] == "redundant"]
    elapsed = time.time() - start
    print(f"\n{len(rows)} pack(s) analyzed, {len(redundant)} fully redundant "
          f"(every object already covered by a bigger pack elsewhere), "
          f"{len(parse_failures)} unparseable, {elapsed:.1f}s -> {args.out}")
    if redundant:
        print("redundant packs (repo, MB -> matched repo):")
        for r in redundant[:20]:
            print(f"  {r['repo_name']}  ({r['pack_mb']} MB) -> "
                  f"{r['matched_repo']} ({r['matched_pct']}% match)")
        if len(redundant) > 20:
            print(f"  ... and {len(redundant) - 20} more (see {args.out})")
    if parse_failures:
        print(f"\n{len(parse_failures)} idx file(s) could not be parsed "
              f"(excluded from the analysis above):")
        for repo_name, repo_path, pack_path in parse_failures[:20]:
            print(f"  {repo_name}  {pack_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
