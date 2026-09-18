#!/usr/bin/env python3
"""
hash_working_tree.py - md5 + sha256 + git blob hash for every file INSIDE
the .git directory (pack_count.py's same object-store folder - .git/
itself for a normal repo, or the repo root for a bare repo) of every repo
found under the given path(s), written to a CSV keyed by repo org / repo
name / full path.

Repo discovery and threading model are ported straight from pack_count.py:
directory-discovery threads walk down from each given path, recognizing a
repo root via get_git_dir() (a normal .git subdirectory, or a true bare
repo - objects/ + refs/ + HEAD directly inside it) and stopping there
without descending into it. Discovery (scanner threads) and hashing
(worker threads) are two pools connected by queues, with a PendingCounter
tracking in-flight directories so the pipeline knows exactly when
discovery is done.

This hashes the .git dir's contents (packs, loose objects, refs, etc.) -
NOT the checked-out working-tree files. Same per-file hashing (md5 +
sha256 + git blob sha1) that hash_merge_active_archive.py uses for .git
internals, just for one set of repos instead of matching active/archive
pairs.

repo_org / repo_name come from the repo's path relative to the root it
was found under: the last path component is repo_name, everything before
it is repo_org (empty string for a repo sitting directly under the given
root, e.g. AllRepos/<org>/<repo> -> org="<org>", name="<repo>").

Usage:
    python hash_working_tree.py /path/to/one/repo --out out.csv
    python hash_working_tree.py /path/to/AllRepos --out out.csv \\
        --workers 8 --scan-workers 16
"""

import argparse
import csv
import hashlib
import os
import queue
import sys
import threading
import time

CHUNK = 4 * 1024 * 1024
SENTINEL = object()

CSV_HEADER = ["repo_org", "repo_name", "full_path", "md5", "sha256",
              "git_object", "size_bytes", "error"]


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None.
    Same detection pack_count.py uses: a normal .git subdir, or a bare
    repo (objects/ + refs/ + HEAD directly in path)."""
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


def split_org_name(label):
    """'a/b/repo' -> ('a/b', 'repo'); 'repo' -> ('', 'repo')."""
    parts = label.split("/")
    return "/".join(parts[:-1]), parts[-1]


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


def hash_repo_files(git_dir):
    """[(full_path, md5, sha256, git_hash, size, error), ...] for every
    file under git_dir (the .git folder for a normal repo, or the repo
    root itself for a bare repo) - recursively, errors recorded per file,
    not fatal."""
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


# --------------------------------------------------------------------- walk
# ported from pack_count.py: same PendingCounter/process_dir/scanner_loop.

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


def process_dir(cur_dir, root, args, dir_queue, pending, task_q):
    git_dir = get_git_dir(cur_dir)
    if git_dir:
        label = to_posix(os.path.relpath(cur_dir, root)) or "."
        task_q.put((cur_dir, git_dir, label))
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
        dir_queue.put((os.path.join(cur_dir, entry.name), root))


def scanner_loop(args, dir_queue, pending, task_q):
    while True:
        try:
            cur_dir, root = dir_queue.get(timeout=0.2)
        except queue.Empty:
            if pending.done.is_set():
                return
            continue
        try:
            process_dir(cur_dir, root, args, dir_queue, pending, task_q)
        finally:
            pending.dec()


def hash_worker(task_q, result_q):
    while True:
        item = task_q.get()
        if item is SENTINEL:
            result_q.put(SENTINEL)
            return
        repo_path, git_dir, label = item
        org, name = split_org_name(label)
        rows = hash_repo_files(git_dir)
        result_q.put((org, name, rows))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--workers", type=int, default=8,
                    help="repo-hashing worker threads (default 8) - each "
                         "worker hashes one whole repo before taking the "
                         "next")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads (default 8)")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="follow symlinks instead of skipping them")
    ap.add_argument("--quiet", action="store_true", help="no progress output")
    args = ap.parse_args()

    for p in args.paths:
        if not os.path.isdir(p):
            print(f"not a directory: {p}", file=sys.stderr)
            return 2

    dir_queue = queue.Queue()
    pending = PendingCounter()
    task_q = queue.Queue(maxsize=2000)
    result_q = queue.Queue()

    for p in args.paths:
        root_abs = os.path.abspath(p)
        pending.inc()
        dir_queue.put((root_abs, root_abs))

    def closer():
        pending.done.wait()
        for _ in range(args.workers):
            task_q.put(SENTINEL)

    threads = [threading.Thread(target=scanner_loop, daemon=True,
                                args=(args, dir_queue, pending, task_q))
              for _ in range(args.scan_workers)]
    threads.append(threading.Thread(target=closer, daemon=True))
    threads += [threading.Thread(target=hash_worker, daemon=True,
                                 args=(task_q, result_q))
               for _ in range(args.workers)]
    for t in threads:
        t.start()

    count_repos = count_files = errors = 0
    total_bytes = 0
    start = time.time()
    last_render = 0.0
    spinner = "|/-\\"

    def render(final=False):
        elapsed = time.time() - start
        gb = total_bytes / (1024 ** 3)
        frame = "done" if final else spinner[count_repos % len(spinner)]
        line = (f"\r  [{frame}] {count_repos:,} repo(s) | {count_files:,} files | "
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
            org, name, rows = item
            count_repos += 1
            for path, md5, sha256, git_hash, size, err in rows:
                writer.writerow([org, name, path, md5, sha256, git_hash, size, err])
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

    for t in threads:
        t.join(timeout=1)

    elapsed = time.time() - start
    print(f"done: {count_repos:,} repo(s), {count_files:,} file(s), "
          f"{total_bytes / (1024 ** 3):.2f} GB, {errors} error(s), "
          f"{elapsed:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
