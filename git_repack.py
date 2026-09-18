#!/usr/bin/env python3
"""
git_repack.py - run `git repack -ad` (consolidate loose objects + existing
packs into a single pack, drop the old ones once the new one is written)
across every repo found under one or more paths, writing a before/after
size CSV.

This is standard git maintenance, per repo, independently - it does NOT
touch the cross-repo redundancy that pack_redundancy.py/redundancy.csv
found (a pack whose objects are all duplicated in some OTHER repo's bigger
pack is untouched by this; that needs alternates or a different approach).

Repo discovery and threading model match pack_count.py: directory
discovery (scanner threads) and the actual work (repack workers) are two
thread pools connected by queues, with a PendingCounter tracking
in-flight directories so the pipeline knows exactly when scanning is done.

Before repacking a repo, free disk space is checked against that repo's
current on-disk object-store size (`git repack -ad` needs to write the new
pack before deleting the old ones, so it transiently needs roughly that
much extra space); repos that don't clear --min-free-ratio are skipped,
not attempted, so a big repo can't fill the disk mid-repack.

Usage:
    git_repack.py /path/to/one/repo --out repack_log.csv
    git_repack.py /path/to/Test /path/to/archive --out repack_log.csv
    git_repack.py /path/to/AllRepos --out repack_log.csv --workers 4 --dry-run
"""

import argparse
import csv
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

SENTINEL = object()


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None.
    Same detection pack_count.py / hash_inventory.py use: a normal .git
    subdir, or a bare repo (objects/ + refs/ + HEAD directly in path)."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD"))):
        return path
    return None


def dir_size_bytes(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
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
        task_q.put((cur_dir, git_dir))
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


def repack_one(repo_path, git_dir, args):
    name = os.path.basename(repo_path.rstrip(os.sep)) or repo_path
    size_before = dir_size_bytes(os.path.join(git_dir, "objects"))

    _total, _used, free = shutil.disk_usage(git_dir)
    if free < size_before * args.min_free_ratio:
        return {
            "repo_name": name, "full_path": repo_path, "git_dir": git_dir,
            "size_before_mb": round(size_before / 1e6, 2), "size_after_mb": "",
            "reclaimed_mb": "", "duration_s": "", "status": "skipped_low_disk",
            "error": f"free={free/1e6:.0f}MB < required={size_before*args.min_free_ratio/1e6:.0f}MB",
        }

    if args.dry_run:
        return {
            "repo_name": name, "full_path": repo_path, "git_dir": git_dir,
            "size_before_mb": round(size_before / 1e6, 2), "size_after_mb": "",
            "reclaimed_mb": "", "duration_s": "", "status": "dry_run", "error": "",
        }

    cmd = ["git", "--git-dir", git_dir, "repack", "-ad"]
    if args.aggressive:
        cmd.append("-f")

    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
        duration = time.time() - start
        if proc.returncode != 0:
            return {
                "repo_name": name, "full_path": repo_path, "git_dir": git_dir,
                "size_before_mb": round(size_before / 1e6, 2), "size_after_mb": "",
                "reclaimed_mb": "", "duration_s": round(duration, 1), "status": "error",
                "error": proc.stderr.strip()[:500],
            }
    except subprocess.TimeoutExpired:
        return {
            "repo_name": name, "full_path": repo_path, "git_dir": git_dir,
            "size_before_mb": round(size_before / 1e6, 2), "size_after_mb": "",
            "reclaimed_mb": "", "duration_s": args.timeout, "status": "timeout", "error": "",
        }

    duration = time.time() - start
    size_after = dir_size_bytes(os.path.join(git_dir, "objects"))
    return {
        "repo_name": name, "full_path": repo_path, "git_dir": git_dir,
        "size_before_mb": round(size_before / 1e6, 2),
        "size_after_mb": round(size_after / 1e6, 2),
        "reclaimed_mb": round((size_before - size_after) / 1e6, 2),
        "duration_s": round(duration, 1), "status": "ok", "error": "",
    }


def repack_worker(task_q, result_q, args):
    while True:
        item = task_q.get()
        if item is SENTINEL:
            result_q.put(SENTINEL)
            return
        repo_path, git_dir = item
        result_q.put(repack_one(repo_path, git_dir, args))


CSV_FIELDS = ["repo_name", "full_path", "git_dir", "size_before_mb", "size_after_mb",
              "reclaimed_mb", "duration_s", "status", "error"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--workers", type=int, default=4,
                    help="repack worker threads (default 4) - each git repack is "
                         "itself CPU/IO heavy, keep this modest")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads (default 8)")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="follow symlinks instead of skipping them")
    ap.add_argument("--aggressive", action="store_true",
                    help="pass -f to git repack (better compression, much slower)")
    ap.add_argument("--min-free-ratio", type=float, default=1.3,
                    help="skip a repo unless free disk space >= this * its current "
                         "object-store size (default 1.3)")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="per-repo timeout in seconds (default 3600)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only measure current sizes, run no git repack")
    ap.add_argument("--quiet", action="store_true", help="no live progress output")
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
    threads += [threading.Thread(target=repack_worker, daemon=True,
                                 args=(task_q, result_q, args))
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
        reclaimed = sum(r["reclaimed_mb"] for r in rows if isinstance(r["reclaimed_mb"], (int, float)))
        errors = sum(1 for r in rows if r["status"] not in ("ok", "dry_run"))
        frame = "done" if final else spinner[n % len(spinner)]
        line = (f"\r  [{frame}] {n:,} repo(s) | {reclaimed:,.0f}MB reclaimed | "
                f"{errors} skipped/error | {rate:.2f} repo/s | {elapsed:,.0f}s")
        print(line.ljust(90), end="\n" if final else "", file=sys.stderr, flush=True)

    while sentinels_seen < args.workers:
        item = result_q.get()
        if item is SENTINEL:
            sentinels_seen += 1
            continue
        rows.append(item)
        if not args.quiet:
            render()
            last_render = time.time()

    if not args.quiet:
        render(final=True)

    for t in threads:
        t.join()

    rows.sort(key=lambda r: r["full_path"])
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

    reclaimed = sum(r["reclaimed_mb"] for r in rows if isinstance(r["reclaimed_mb"], (int, float)))
    ok = sum(1 for r in rows if r["status"] == "ok")
    errors = [r for r in rows if r["status"] not in ("ok", "dry_run")]
    elapsed = time.time() - start
    print(f"\n{len(rows)} repo(s): {ok} repacked, {len(errors)} skipped/error, "
          f"{reclaimed:,.0f}MB reclaimed, {elapsed:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
