#!/usr/bin/env python3
"""
make_input_csv.py - build a test input file for file_history_for_list.py from
the repos that are actually on disk.

Walks <root>/<org>/<repo>, lists the files in each repo's newest tree (HEAD, or
for a repo git cannot open - a .git with only objects - the newest commit in
its object store) and writes one row per file in the input format:

    org, repo, relpath, filename, sha256

relpath looks like AllRepos\\<org>\\<repo>\\<path> (Windows style, like the real
input) and sha256 is left blank (it is never used for matching).

Usage:
    python make_input_csv.py "/mnt/4tb/.../AllRepos" --out input.csv
    python make_input_csv.py "/mnt/4tb/.../AllRepos" --out input.csv \\
        --max-per-repo 200 --max-repos 50        # a small, quick sample
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from explore_input_csv import is_repo_dir
from extract_commits import GIT, run_tracked
from file_history_for_list import RecoveredRepo, git_can_open


def tree_files(repo_path, rev):
    proc = run_tracked(GIT + ["-C", repo_path, "ls-tree", "-r", "-z",
                              "--name-only", rev])
    if proc.returncode != 0:
        return None
    return sorted(set(proc.stdout.decode("utf-8", "surrogateescape").split("\0")) - {""})


def list_files(repo_path):
    """-> (paths, source) for the repo's newest tree, or (None, reason)."""
    if git_can_open(repo_path):
        files = tree_files(repo_path, "HEAD")
        if files is not None:
            return files, "HEAD"
    try:
        with RecoveredRepo(repo_path) as rec:
            rec.add_all_commits_as_refs()
            proc = run_tracked(GIT + ["-C", rec.tmp, "log", "--all", "-1",
                                      "--format=%H"])
            sha = proc.stdout.decode().strip()
            if not sha:
                return None, "no commits found"
            files = tree_files(rec.tmp, sha)
            return (files, "newest commit") if files is not None else (None, "ls-tree failed")
    except Exception as exc:                                  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:100])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="folder holding <org>/<repo>")
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--max-per-repo", type=int, default=0,
                    help="keep at most this many files per repo, evenly spread "
                         "(default 0 = all)")
    ap.add_argument("--max-repos", type=int, default=0,
                    help="stop after this many repos (default 0 = all)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print("not a directory: %s" % args.root, file=sys.stderr)
        return 2

    repos = []
    for org in sorted(os.listdir(args.root)):
        op = os.path.join(args.root, org)
        if not os.path.isdir(op):
            continue
        for repo in sorted(os.listdir(op)):
            rp = os.path.join(op, repo)
            if os.path.isdir(rp) and is_repo_dir(rp):
                repos.append((org, repo, rp))
    if args.max_repos:
        repos = repos[:args.max_repos]
    print("found %d repo(s) under %s" % (len(repos), args.root))

    t0, rows, skipped, recovered = time.time(), 0, [], 0
    with open(args.out, "w", newline="", encoding="utf-8") as fh, \
            ThreadPoolExecutor(max_workers=args.workers) as pool:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "relpath", "filename", "sha256"])
        results = pool.map(lambda t: (t, list_files(t[2])), repos)
        for i, ((org, repo, _rp), (files, source)) in enumerate(results, 1):
            if files is None:
                skipped.append((org, repo, source))
            else:
                recovered += source != "HEAD"
                if args.max_per_repo and len(files) > args.max_per_repo:
                    step = len(files) / args.max_per_repo
                    files = [files[int(k * step)] for k in range(args.max_per_repo)]
                for p in files:
                    w.writerow([org, repo, "AllRepos\\%s\\%s\\%s" % (org, repo, p.replace("/", "\\")),
                                p.rsplit("/", 1)[-1], ""])
                    rows += 1
            print("\r  %d/%d repos | %s rows | %.0fs" % (i, len(repos), f"{rows:,}",
                                                       time.time() - t0),
                  end="", file=sys.stderr, flush=True)
    print("\nwrote %s row(s) from %d repo(s) to %s (%d repo(s) had no HEAD and "
          "were read from their newest commit)"
          % (f"{rows:,}", len(repos) - len(skipped), args.out, recovered))
    for org, repo, why in skipped[:20]:
        print("  skipped %s/%s: %s" % (org, repo, why))
    if len(skipped) > 20:
        print("  ... and %d more skipped" % (len(skipped) - 20))
    return 0


if __name__ == "__main__":
    sys.exit(main())
