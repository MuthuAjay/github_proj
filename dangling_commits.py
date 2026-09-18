#!/usr/bin/env python3
"""
dangling_commits.py - find repos with unreachable (dangling) commits.

For every repo found under the given path(s), reports one of:

  OK       git plumbing works; every commit object in the store is
           reachable from some ref.
  DANGLING git plumbing works, refs resolve, but N of M commit objects
           are NOT reachable from any ref - the classic case (an old
           commit orphaned by a reset/rebase/deleted branch, still
           sitting in the object store until gc'd).
  BROKEN   git doesn't recognize the directory as a repository at all
           (missing HEAD/refs/config - only .git/objects survived, as
           seen in some bulk-cloned/archived estates). "Reachable from
           a ref" is undefined here: there are no refs. Commit objects
           are instead counted directly out of the raw .pack file(s)
           via `git verify-pack -v`, which needs no repo context - but
           there's no path/branch information for them, and loose
           objects (outside any pack) aren't counted at all, since
           reading those needs git to recognize the repo. See
           hash_inventory.py / extract_from_pack.py for recovering
           this kind of repo properly.
  ERROR    something else went wrong; see the note column.

DANGLING and BROKEN are NOT the same finding - DANGLING means "this repo
has abandoned history sitting around", BROKEN means "this repo's ref
metadata itself is gone", which is why they're kept as separate statuses
rather than one "has unreachable commits" flag.

Discovery (finding repo roots) is pure filesystem checks, same as
pack_count.py - no git subprocess per candidate directory. Only repos
that are actually found get a git subprocess call, run across --workers
threads.

Usage:
    dangling_commits.py /path/to/one/repo --out report.csv
    dangling_commits.py /path/to/Test /path/to/archive --out report.csv
    dangling_commits.py /mnt/ntfsdrive/.../AllRepos --out report.csv --scan-workers 24
"""

import argparse
import csv
import os
import queue
import subprocess
import sys
import threading
import time

SENTINEL = object()

# `-c safe.directory=*` avoids git refusing to touch a repo whose files look
# owned by a different uid than the current process - common on mounted
# drives (NTFS/network) where every file reports the same fixed owner.
GIT = ["git", "-c", "safe.directory=*"]


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None.
    Purely file-based - this can say "looks like a repo" even for a repo
    git itself can't fully recognize (e.g. objects/ present but no HEAD)."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD"))):
        return path
    return None


def git_lines(repo, *args):
    p = subprocess.run(GIT + ["-C", repo, *args], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True)
    return [l for l in p.stdout.splitlines() if l]


def git_recognizes(repo):
    return subprocess.run(
        GIT + ["-C", repo, "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def all_commit_and_tag_shas(repo):
    """Every commit/tag object physically in the store, regardless of
    reachability - reads the object database directly, independent of
    which refs exist. Requires git to recognize the repo at all."""
    p = subprocess.Popen(
        GIT + ["-C", repo, "cat-file", "--batch-all-objects",
              "--batch-check=%(objectname) %(objecttype)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    out = []
    try:
        for line in p.stdout:
            bits = line.split()
            if len(bits) == 2 and bits[1] in ("commit", "tag"):
                out.append(bits[0])
    finally:
        p.stdout.close()
        p.wait()
    return out


def commit_labels(repo, shas):
    """sha -> 'YYYY-MM-DD author: subject', batched in one `git log` call."""
    if not shas:
        return {}
    out = {}
    proc = subprocess.Popen(
        GIT + ["-C", repo, "log", "--no-walk", "--date=short",
              "--format=%H%x09%ad%x09%an%x09%s", "--stdin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    try:
        proc.stdin.write("\n".join(shas) + "\n")
        proc.stdin.close()
        for line in proc.stdout:
            bits = line.rstrip("\n").split("\t", 3)
            if len(bits) == 4:
                out[bits[0]] = f"{bits[1]} {bits[2]}: {bits[3]}"
    finally:
        proc.stdout.close()
        proc.wait()
    return out


def find_pack_paths(git_dir):
    pack_dir = os.path.join(git_dir, "objects", "pack")
    try:
        return sorted(os.path.join(pack_dir, f)
                      for f in os.listdir(pack_dir) if f.endswith(".pack"))
    except OSError:
        return []


def verify_pack_objects(pack_path):
    """sha -> type, read straight from a pack's own index - no repo context
    needed, so this still works when git can't recognize the repo at all."""
    out = {}
    p = subprocess.run(["git", "verify-pack", "-v", pack_path],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in p.stdout.splitlines():
        bits = line.split()
        if len(bits) >= 3 and len(bits[0]) == 40 and \
                bits[1] in ("commit", "tree", "blob", "tag"):
            out[bits[0]] = bits[1]
    return out


def analyze_repo(repo_path, git_dir):
    if not git_recognizes(repo_path):
        commit_shas = []
        for pack in find_pack_paths(git_dir):
            commit_shas += [s for s, t in verify_pack_objects(pack).items()
                            if t == "commit"]
        return {
            "status": "BROKEN", "refs": 0,
            "commits_total": len(commit_shas), "commits_dangling": len(commit_shas),
            "dangling_shas": commit_shas, "labels": {},
            "note": "no resolvable HEAD/refs - counted commit objects "
                    "directly from .pack file(s); loose objects outside a "
                    "pack aren't counted (git can't read them either)",
        }

    refs = git_lines(repo_path, "for-each-ref", "--format=%(refname)")
    reachable = set(git_lines(repo_path, "rev-list", "--all"))
    commit_ish = all_commit_and_tag_shas(repo_path)
    dangling = [c for c in commit_ish if c not in reachable]
    labels = commit_labels(repo_path, dangling) if dangling else {}
    return {
        "status": "DANGLING" if dangling else "OK", "refs": len(refs),
        "commits_total": len(commit_ish), "commits_dangling": len(dangling),
        "dangling_shas": dangling, "labels": labels, "note": "",
    }


# --------------------------------------------------------------------- walk

class PendingCounter:
    """Tracks directories enqueued-but-not-yet-fully-processed. Hits zero
    exactly when scanning is completely done, the signal to stop workers."""

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


def analysis_worker(task_q, result_q):
    while True:
        item = task_q.get()
        if item is SENTINEL:
            result_q.put(SENTINEL)
            return
        repo_path, git_dir = item
        try:
            res = analyze_repo(repo_path, git_dir)
        except Exception as exc:
            res = {"status": "ERROR", "refs": 0, "commits_total": 0,
                  "commits_dangling": 0, "dangling_shas": [], "labels": {},
                  "note": repr(exc)}
        name = os.path.basename(repo_path.rstrip(os.sep)) or repo_path
        result_q.put((name, repo_path, res))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="repo(s) or parent directory/directories to scan")
    ap.add_argument("--out", required=True,
                    help="summary CSV: repo_name, full_path, status, "
                         "refs, commits_total, commits_dangling, note")
    ap.add_argument("--workers", type=int, default=4,
                    help="git-analysis worker threads (default 4)")
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
    threads += [threading.Thread(target=analysis_worker, daemon=True,
                                 args=(task_q, result_q))
               for _ in range(args.workers)]
    for t in threads:
        t.start()

    rows = []
    detail_rows = []
    sentinels_seen = 0
    start = time.time()
    while sentinels_seen < args.workers:
        item = result_q.get()
        if item is SENTINEL:
            sentinels_seen += 1
            continue
        name, path, res = item
        rows.append((name, path, res))
        if not args.quiet:
            print(f"  {res['status']:9} {path}", file=sys.stderr)
        for sha in res["dangling_shas"]:
            detail_rows.append((name, path, res["status"], sha,
                               res["labels"].get(sha, "")))

    for t in threads:
        t.join()

    rows.sort(key=lambda r: r[1])
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["repo_name", "full_path", "status", "refs",
                   "commits_total", "commits_dangling", "note"])
        for name, path, res in rows:
            w.writerow([name, path, res["status"], res["refs"],
                       res["commits_total"], res["commits_dangling"], res["note"]])

    detail_out = os.path.splitext(args.out)[0] + ".dangling_detail.csv"
    if detail_rows:
        with open(detail_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["repo_name", "full_path", "status", "sha", "label"])
            w.writerows(detail_rows)

    counts = {}
    for _, _, res in rows:
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    elapsed = time.time() - start
    print(f"\n{len(rows)} repo(s) checked in {elapsed:.1f}s -> {args.out}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if detail_rows:
        print(f"  {len(detail_rows)} unreachable commit(s) listed -> {detail_out}")

    return 1 if counts.get("DANGLING") or counts.get("BROKEN") or counts.get("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
