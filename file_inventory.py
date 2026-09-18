#!/usr/bin/env python3
"""
file_inventory.py - list every file under a directory tree, tagged with
which repo it belongs to, and write repo_name, full_path, extension to a
CSV.

Plain disk walk: whatever physically exists on disk is listed, including
each repo's .git internals (pack files, refs, hooks, everything). A repo
whose working tree was never checked out (content only sits in git's
object store, nothing on disk) will show only its .git contents here -
this script does not read git's object store at all, it only walks files.
For that kind of repo, hash_inventory.py / compare_repos.py are the tools
that source content from git directly instead of the working tree.

A directory is treated as a "repo root" if it has a .git subdirectory OR
is itself a bare git dir (HEAD + objects/ + refs/ present directly in it) -
same detection pack_count.py and dangling_commits.py use. Every file under
a repo root - including inside nested directories and .git - is tagged
with that repo's name. Files not under any detected repo are tagged
"(no-repo)". A nested repo (e.g. a submodule's own .git) gets tagged with
its own name, not its parent's, from that point down.

Usage:
    file_inventory.py /path/to/one/repo --out files.csv
    file_inventory.py /path/to/Test /path/to/archive --out files.csv
    file_inventory.py /mnt/ntfsdrive/.../AllRepos --out files.csv --scan-workers 24
"""

import argparse
import csv
import os
import queue
import sys
import threading
import time

SENTINEL = object()
NO_REPO = "(no-repo)"


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


class PendingCounter:
    """Tracks directories enqueued-but-not-yet-fully-processed. Hits zero
    exactly when scanning is completely done, the signal to stop."""

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


def process_dir(cur_dir, repo_ctx, args, dir_queue, pending, result_q):
    # A directory literally named ".git" always has objects/+refs/+HEAD
    # directly inside it - that matches the bare-repo heuristic too, so
    # without this guard, descending into a repo's own .git would get
    # misread as finding a nested bare repo called ".git".
    if os.path.basename(cur_dir) != ".git" and get_git_dir(cur_dir):
        repo_ctx = cur_dir      # innermost enclosing repo wins

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
        if is_symlink and not args.follow_symlinks:
            continue

        abs_path = os.path.join(cur_dir, entry.name)
        if is_dir:
            pending.inc()
            dir_queue.put((abs_path, repo_ctx))
            continue

        repo_name = os.path.basename(repo_ctx.rstrip(os.sep)) if repo_ctx else NO_REPO
        ext = os.path.splitext(entry.name)[1].lower()
        result_q.put((repo_name, abs_path, ext))


def scanner_loop(args, dir_queue, pending, result_q):
    while True:
        try:
            cur_dir, repo_ctx = dir_queue.get(timeout=0.2)
        except queue.Empty:
            if pending.done.is_set():
                return
            continue
        try:
            process_dir(cur_dir, repo_ctx, args, dir_queue, pending, result_q)
        finally:
            pending.dec()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True,
                    help="CSV file to write: repo_name, full_path, extension")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-listing threads (default 8) - raise this "
                         "on a slow/network drive with many directories")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="follow symlinks instead of skipping them")
    ap.add_argument("--save-every", type=int, default=5000,
                    help="flush the CSV to disk every N rows (default 5000)")
    ap.add_argument("--quiet", action="store_true", help="no progress output")
    args = ap.parse_args()

    for p in args.paths:
        if not os.path.isdir(p):
            print(f"not a directory: {p}", file=sys.stderr)
            return 2

    dir_queue = queue.Queue()
    pending = PendingCounter()
    result_q = queue.Queue(maxsize=5000)

    for p in args.paths:
        pending.inc()
        dir_queue.put((os.path.abspath(p), None))

    def closer():
        pending.done.wait()
        result_q.put(SENTINEL)

    threads = [threading.Thread(target=scanner_loop, daemon=True,
                                args=(args, dir_queue, pending, result_q))
              for _ in range(args.scan_workers)]
    threads.append(threading.Thread(target=closer, daemon=True))
    for t in threads:
        t.start()

    count = 0
    start = time.time()
    last_render = 0.0
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["repo_name", "full_path", "extension"])
        while True:
            item = result_q.get()
            if item is SENTINEL:
                break
            w.writerow(item)
            count += 1
            if count % max(1, args.save_every) == 0:
                fh.flush()
            now = time.time()
            if not args.quiet and (now - last_render) >= 0.5:
                print(f"\r  {count:,} file(s) ...", end="", file=sys.stderr)
                last_render = now
        fh.flush()

    for t in threads:
        t.join()

    if not args.quiet:
        print(file=sys.stderr)
    elapsed = time.time() - start
    print(f"{count:,} file(s) in {elapsed:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
