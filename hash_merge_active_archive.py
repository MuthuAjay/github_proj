#!/usr/bin/env python3
"""
hash_merge_active_archive.py - md5 + sha256 + git blob hash for every file
in each repo's .git folder, for matching active/archive repo pairs, merged
into one wide CSV keyed by repo name.

Repo discovery is pack_count.py's exact model: directory-discovery threads
recursively walk down from each root, and a directory is recognized as a
repo root - stopping the walk right there, without descending into its
internals - via get_git_dir(): a normal .git subdirectory, or (a true bare
repo) objects/ + refs/ + HEAD directly inside it. A repo's label is its
path relative to the root it was found under (POSIX-separated), so nesting
depth (ROOT/<repo>, ROOT/<org>/<repo>, or deeper) falls out of wherever a
.git actually turns up - nothing needs to be told to this script up front.
Both roots (active and archive) are discovered together in one pass,
sharing the same --scan-workers pool.

Once both sides are discovered, repos present under the same label on both
are matched (present-on-one-side-only repos are reported and skipped).

For each matched repo, ONE worker thread hashes every file under that
repo's active .git dir AND every file under its archive .git dir (md5,
sha256, git blob sha1 - one streamed read per file, no subprocess), sorts
each side by path, zips them together, and hands the whole repo's rows
back as a single unit - exactly like pack_count.py where a repo is the
unit of work a worker thread picks up and finishes before taking the next
one. That's what keeps a bounded number of repos (= --workers) "in flight"
at any moment, so repos finish and get written to the CSV continuously.
Seeding thousands of repos into one shared per-file queue instead would
make every repo progress in lockstep breadth-first, so with a big estate
NONE of them would finish - and free their memory - until the whole run
was nearly done. Repo-level task granularity avoids that by construction.

Usage:
    python hash_merge_active_archive.py ACTIVE_ROOT ARCHIVE_ROOT --out merged.csv
    python hash_merge_active_archive.py ACTIVE_ROOT ARCHIVE_ROOT --out merged.csv \\
        --workers 16 --scan-workers 24
"""

import argparse
import csv
import hashlib
import os
import queue
import sys
import threading
import time
from itertools import zip_longest

CHUNK = 4 * 1024 * 1024
SENTINEL = object()

CSV_HEADER = [
    "active_repo", "active_full_path", "active_md5", "active_sha256",
    "active_git_hash", "active_size_bytes", "active_error",
    "archive_repo", "archive_full_path", "archive_md5", "archive_sha256",
    "archive_git_hash", "archive_size_bytes", "archive_error",
]


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None.
    Same detection pack_count.py uses: a normal .git subdir, or a bare repo
    (objects/ + refs/ + HEAD directly in path)."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD"))):
        return path
    return None


def to_posix(rel):
    return rel.replace(os.sep, "/")


def hash_file(path):
    """One streamed read -> (md5, sha256, git_blob_sha1, size). git_blob_sha1
    is sha1("blob {size}\\0" + content), the same framing `git hash-object`
    uses, computed directly from these bytes - no subprocess needed."""
    size = os.path.getsize(path)
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    git_h = hashlib.sha1()
    git_h.update(f"blob {size}\0".encode("utf-8"))
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            md5.update(chunk)
            sha256.update(chunk)
            git_h.update(chunk)
    return md5.hexdigest(), sha256.hexdigest(), git_h.hexdigest(), size


def hash_tree(git_dir):
    """[(full_path, md5, sha256, git_hash, size, error), ...] for every file
    under git_dir, recursively - errors recorded per file, not fatal."""
    rows = []
    for dirpath, _dirnames, filenames in os.walk(git_dir):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                md5, sha256, git_hash, size = hash_file(path)
                rows.append((path, md5, sha256, git_hash, size, ""))
            except OSError as exc:
                rows.append((path, "", "", "", "", str(exc)))
    return rows


# --------------------------------------------------------------- discovery
# ported from pack_count.py: same PendingCounter/process_dir/scanner_loop
# shape, just recording (side, label) -> git_dir instead of counting.

class PendingCounter:
    """Tracks directories enqueued-but-not-yet-fully-processed. Hits zero
    exactly when scanning is completely done, the signal to stop scanners."""

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


def process_dir(cur_dir, root, side, dir_queue, pending, found, found_lock,
                follow_symlinks):
    git_dir = get_git_dir(cur_dir)
    if git_dir:
        label = to_posix(os.path.relpath(cur_dir, root)) or "."
        with found_lock:
            found[side][label] = git_dir
        return          # a repo's own internals aren't a separate repo

    try:
        entries = list(os.scandir(cur_dir))
    except OSError as exc:
        print(f"warning: cannot list {cur_dir}: {exc}", file=sys.stderr)
        return

    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
            is_symlink = entry.is_symlink()
        except OSError as exc:
            print(f"warning: cannot stat {entry.path}: {exc}", file=sys.stderr)
            continue
        if not is_dir or (is_symlink and not follow_symlinks):
            continue
        pending.inc()
        dir_queue.put((os.path.join(cur_dir, entry.name), root, side))


def scanner_loop(dir_queue, pending, found, found_lock, follow_symlinks):
    while True:
        try:
            cur_dir, root, side = dir_queue.get(timeout=0.2)
        except queue.Empty:
            if pending.done.is_set():
                return
            continue
        try:
            process_dir(cur_dir, root, side, dir_queue, pending, found,
                        found_lock, follow_symlinks)
        finally:
            pending.dec()


def discover_repos(roots, scan_workers, follow_symlinks=False):
    """roots: [(side, root_path), ...], discovered together in one pass,
    sharing scan_workers. Returns {side: {label: git_dir}}."""
    dir_queue = queue.Queue()
    pending = PendingCounter()
    found = {side: {} for side, _ in roots}
    found_lock = threading.Lock()

    for side, root in roots:
        root_abs = os.path.abspath(root)
        pending.inc()
        dir_queue.put((root_abs, root_abs, side))

    threads = [threading.Thread(target=scanner_loop, daemon=True,
                                args=(dir_queue, pending, found, found_lock,
                                      follow_symlinks))
              for _ in range(scan_workers)]
    for t in threads:
        t.start()
    pending.done.wait()
    for t in threads:
        t.join(timeout=1)
    return found


# ------------------------------------------------------------------ hashing

def write_repo_rows(writer, repo, active_rows, archive_rows):
    active_rows.sort(key=lambda r: r[0])
    archive_rows.sort(key=lambda r: r[0])
    for a, b in zip_longest(active_rows, archive_rows, fillvalue=None):
        row = []
        if a:
            path, md5, sha256, gh, size, err = a
            row += [repo, path, md5, sha256, gh, size, err]
        else:
            row += [repo, "", "", "", "", "", ""]
        if b:
            path, md5, sha256, gh, size, err = b
            row += [repo, path, md5, sha256, gh, size, err]
        else:
            row += [repo, "", "", "", "", "", ""]
        writer.writerow(row)


def hash_worker(task_q, result_q):
    while True:
        item = task_q.get()
        if item is SENTINEL:
            result_q.put(SENTINEL)
            return
        repo, active_git_dir, archive_git_dir = item
        active_rows = hash_tree(active_git_dir)
        archive_rows = hash_tree(archive_git_dir)
        result_q.put((repo, active_rows, archive_rows))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("active_root", help="directory holding normal (working-tree) repos")
    ap.add_argument("archive_root", help="directory holding archived repos (raw git objects only)")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--workers", type=int, default=8,
                    help="repo-hashing worker threads (default 8) - each "
                         "worker hashes one whole repo (both sides) before "
                         "taking the next, so this also caps how many "
                         "repos are in flight at once")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads, shared across both "
                         "roots (default 8)")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="follow symlinks instead of skipping them")
    ap.add_argument("--quiet", action="store_true", help="no progress output")
    args = ap.parse_args()

    for label, root in (("active", args.active_root), ("archive", args.archive_root)):
        if not os.path.isdir(root):
            print(f"not a directory ({label}): {root}", file=sys.stderr)
            return 2

    if not args.quiet:
        print("discovering repos ...", file=sys.stderr)
    t0 = time.time()
    found = discover_repos(
        [("active", args.active_root), ("archive", args.archive_root)],
        args.scan_workers, args.follow_symlinks)
    active_map, archive_map = found["active"], found["archive"]
    if not args.quiet:
        print(f"  active:  {len(active_map):,} repo(s) found under {args.active_root}",
              file=sys.stderr)
        print(f"  archive: {len(archive_map):,} repo(s) found under {args.archive_root}",
              file=sys.stderr)
        print(f"  discovery took {time.time() - t0:.0f}s", file=sys.stderr)

    only_active = sorted(set(active_map) - set(archive_map))
    only_archive = sorted(set(archive_map) - set(active_map))
    repos = sorted(set(active_map) & set(archive_map))

    if only_active:
        print(f"warning: {len(only_active)} repo(s) only in active, skipped: "
              f"{', '.join(only_active[:10])}{' ...' if len(only_active) > 10 else ''}",
              file=sys.stderr)
    if only_archive:
        print(f"warning: {len(only_archive)} repo(s) only in archive, skipped: "
              f"{', '.join(only_archive[:10])}{' ...' if len(only_archive) > 10 else ''}",
              file=sys.stderr)
    if not repos:
        print("no matching repos between active and archive roots", file=sys.stderr)
        return 2

    task_q = queue.Queue()
    result_q = queue.Queue()
    for repo in repos:
        task_q.put((repo, active_map[repo], archive_map[repo]))
    for _ in range(args.workers):
        task_q.put(SENTINEL)

    workers = [threading.Thread(target=hash_worker, daemon=True,
                                args=(task_q, result_q))
              for _ in range(args.workers)]
    for t in workers:
        t.start()

    count_files = errors = 0
    total_bytes = 0
    repos_written = 0
    start = time.time()
    last_render = 0.0
    spinner = "|/-\\"

    def render(final=False):
        elapsed = time.time() - start
        gb = total_bytes / (1024 ** 3)
        line = (f"\r  [{'done' if final else spinner[repos_written % len(spinner)]}] "
                f"{repos_written}/{len(repos)} repo(s) | {count_files:,} files | "
                f"{gb:.2f} GB | {errors} error(s) | {elapsed:,.0f}s")
        print(line.ljust(100), end="\n" if final else "", file=sys.stderr, flush=True)

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)

        sentinels_seen = 0
        while sentinels_seen < args.workers:
            item = result_q.get()
            if item is SENTINEL:
                sentinels_seen += 1
                continue
            repo, active_rows, archive_rows = item
            write_repo_rows(writer, repo, active_rows, archive_rows)
            repos_written += 1
            for rows in (active_rows, archive_rows):
                for _, _, _, _, size, err in rows:
                    count_files += 1
                    if err:
                        errors += 1
                    else:
                        total_bytes += size or 0
            fh.flush()

            now = time.time()
            if not args.quiet and (now - last_render) >= 0.2:
                render()
                last_render = now

        fh.flush()
        if not args.quiet:
            render(final=True)

    for t in workers:
        t.join(timeout=1)

    elapsed = time.time() - start
    print(f"done: {len(repos)} repo(s), {count_files:,} file(s), "
          f"{total_bytes / (1024 ** 3):.2f} GB, {errors} error(s), "
          f"{elapsed:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
