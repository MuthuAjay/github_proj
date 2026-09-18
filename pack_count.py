#!/usr/bin/env python3
"""
pack_count.py - count .pack files and loose objects per repo, write
repo_name, full_path, pack_count, loose_count to a CSV.

Each positional argument can be a single repo (has its own .git, or is
itself a bare repo - HEAD + objects/ + refs/ directly inside it) or a
parent directory holding many repos (e.g. an AllRepos\\<org>\\... dump);
either way, every repo found underneath is counted exactly once.

Concurrency model ported from hash_inventory.py: directory discovery
(walking down to find repo roots) and the actual counting are two
separate thread pools connected by queues, with a PendingCounter tracking
directories enqueued-but-not-yet-processed so the pipeline knows exactly
when it's done. Discovery is latency-bound (one os.scandir per directory,
mostly waiting on the filesystem/network), not CPU-bound, so
--scan-workers threads hide that latency; --workers threads then do the
(cheap) objects/pack listing per repo found. No git subprocess, no
hashing - this only ever reads directory entries.

Usage:
    pack_count.py /path/to/one/repo --out counts.csv
    pack_count.py /path/to/Test /path/to/archive --out counts.csv
    pack_count.py /path/to/AllRepos --out counts.csv --scan-workers 16
"""

import argparse
import csv
import os
import queue
import sys
import threading
import time

SENTINEL = object()


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None.
    Same detection hash_inventory.py uses: a normal .git subdir, or a bare
    repo (objects/ + refs/ + HEAD directly in path)."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD"))):
        return path
    return None


def count_pack_files(git_dir):
    """*.pack files directly under objects/pack - the packed half of the
    object store (loose objects and .idx files aren't counted)."""
    pack_dir = os.path.join(git_dir, "objects", "pack")
    try:
        return sum(1 for f in os.listdir(pack_dir) if f.endswith(".pack"))
    except OSError:
        return 0


def count_loose_objects(git_dir):
    """Files under objects/xx/... (skips pack/ and info/) - the loose half
    of the object store. Just a filename count, no file open, no
    decompression - type/size aren't needed here, only "how many"."""
    objects_dir = os.path.join(git_dir, "objects")
    total = 0
    try:
        shard_names = os.listdir(objects_dir)
    except OSError:
        return 0
    for shard in shard_names:
        if shard in ("pack", "info") or len(shard) != 2:
            continue
        try:
            total += len(os.listdir(os.path.join(objects_dir, shard)))
        except OSError:
            continue
    return total


# --------------------------------------------------------------------- walk

class PendingCounter:
    """Tracks directories enqueued-but-not-yet-fully-processed. Hits zero
    exactly when scanning is completely done (nothing left in the queue and
    nothing still being expanded), which is the signal to stop the workers."""

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
        task_q.put(("repo", cur_dir, git_dir))
        return          # a repo's own internals aren't a separate repo

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


def counter_worker(task_q, result_q):
    while True:
        item = task_q.get()
        if item is SENTINEL:
            result_q.put(SENTINEL)
            return
        _, repo_path, git_dir = item
        name = os.path.basename(repo_path.rstrip(os.sep)) or repo_path
        result_q.put((name, repo_path, count_pack_files(git_dir),
                     count_loose_objects(git_dir)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--workers", type=int, default=4,
                    help="pack-counting worker threads (default 4)")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads (default 8) - raise "
                         "this on a slow/network drive with many directories")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="follow symlinks instead of skipping them")
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
    threads += [threading.Thread(target=counter_worker, daemon=True,
                                 args=(task_q, result_q))
               for _ in range(args.workers)]
    for t in threads:
        t.start()

    rows = []
    sentinels_seen = 0
    start = time.time()
    spinner = "|/-\\"
    last_render = 0.0

    def render(final=False):
        elapsed = time.time() - start
        n = len(rows)
        rate = n / elapsed if elapsed else 0
        total_packs = sum(r[2] for r in rows)
        total_loose = sum(r[3] for r in rows)
        frame = "done" if final else spinner[n % len(spinner)]
        line = (f"\r  [{frame}] {n:,} repo(s) | {total_packs:,} pack file(s) | "
                f"{total_loose:,} loose object(s) | {rate:.1f} repo/s | {elapsed:,.0f}s")
        print(line.ljust(90), end="\n" if final else "", file=sys.stderr, flush=True)

    while sentinels_seen < args.workers:
        item = result_q.get()
        if item is SENTINEL:
            sentinels_seen += 1
            continue
        rows.append(item)
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
        w.writerow(["repo_name", "full_path", "pack_count", "loose_count"])
        w.writerows(rows)

    total_packs = sum(r[2] for r in rows)
    total_loose = sum(r[3] for r in rows)
    elapsed = time.time() - start
    print(f"\n{len(rows)} repo(s): {total_packs:,} .pack file(s), "
          f"{total_loose:,} loose object(s), {elapsed:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
