#!/usr/bin/env python3
"""
simple_file_info.py - for each file in a list: number of commits, and how many
of them added / modified / deleted / renamed it.

Input CSV/TSV needs columns org, repo, relpath (the file's full path, e.g.
AllRepos\\ey-org\\my-repo\\src\\a.py). The folders up to and including
<org>\\<repo> are replaced with --root, so the file is looked up in
<root>/<org>/<repo>, and `git log --follow` is run on that one file.

Adds these columns to a copy of the input:
    repo_path status commits added modified deleted renamed first_seen last_changed

Counts are for the history behind HEAD (the checked-out branch) unless you pass
--all-branches. Renames are followed with `git log --follow`; if that finds
nothing (a known git quirk) a plain `git log` is used. Merge commits are counted
only when they changed the file themselves, so the numbers can differ slightly
from file_history_for_list.py, which diffs every commit against its first parent.
This script runs one git command per file: fine for thousands to a few million
files. For broken repos (no HEAD/refs) or resumable bulk runs use
file_history_for_list.py.

Usage:
    python simple_file_info.py input.csv --root /data/workarea/archive --out result.csv
"""

import argparse
import csv
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from extract_commits import Progress

NEW_COLUMNS = ["repo_path", "status", "commits", "added", "modified", "deleted",
               "renamed", "first_seen", "last_changed"]


def split_path(org, repo, relpath):
    """'AllRepos\\org\\repo\\a\\b.py' -> 'a/b.py' (root folders dropped)."""
    parts = relpath.strip().replace("\\", "/").split("/")
    low = [p.lower() for p in parts]
    for i in range(len(parts) - 1):
        if low[i] == org.lower() and low[i + 1] == repo.lower():
            return "/".join(parts[i + 2:])
    return "/".join(p for p in parts if p not in ("", "."))     # already repo-relative


def git_log(repo_dir, path, follow, all_branches):
    cmd = ["git", "-c", "safe.directory=*", "-C", repo_dir, "log"]
    cmd += ["--follow"] if follow else []
    cmd += ["--all"] if all_branches else []
    cmd += ["--name-status", "--format=%x00%cI", "--", path]
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


def file_info(repo_dir, path, all_branches=False):
    """-> (status, stats dict). One `git log --follow` for the file."""
    proc = git_log(repo_dir, path, True, all_branches)
    if proc.returncode != 0:
        msg = proc.stderr.strip().splitlines()
        return "repo_error: " + (msg[0][:150] if msg else "git failed"), {}
    commits = [c for c in proc.stdout.split("\0") if c.strip()]
    if not commits:
        # --follow silently finds nothing for some files; plain log still can
        proc = git_log(repo_dir, path, False, all_branches)
        commits = [c for c in proc.stdout.split("\0") if c.strip()]
    if not commits:
        return "not_found", {}
    counts = {"A": 0, "M": 0, "D": 0, "R": 0}
    for commit in commits:
        for line in commit.splitlines()[1:]:
            code = line[:1]
            if code in counts:
                counts[code] += 1
    dates = [c.splitlines()[0] for c in commits]          # newest first
    return "found", {"commits": len(commits), "added": counts["A"],
                     "modified": counts["M"], "deleted": counts["D"],
                     "renamed": counts["R"], "first_seen": dates[-1],
                     "last_changed": dates[0]}


def process(row, ix, root, all_branches=False):
    org, repo = row[ix["org"]].strip(), row[ix["repo"]].strip()
    path = split_path(org, repo, row[ix["relpath"]])
    repo_dir = "%s/%s/%s" % (root.rstrip("/"), org, repo)
    if not org or not repo or not path:
        status, stats = "bad_row", {}
    else:
        status, stats = file_info(repo_dir, path, all_branches)
    return row + [repo_dir, status] + [stats.get(c, "") for c in NEW_COLUMNS[2:]]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in")
    ap.add_argument("--root", required=True, help="folder holding <org>/<repo>")
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--delimiter", help="default: guessed from the header line")
    ap.add_argument("--all-branches", action="store_true",
                    help="count history on every branch, not just the "
                         "checked-out one (slower)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--quiet", action="store_true", help="no progress bar")
    args = ap.parse_args()

    with open(args.csv_in, newline="", encoding="utf-8-sig") as fh:
        head = fh.readline()
    delim = args.delimiter or max(",\t|;", key=head.count)

    with open(args.csv_in, "rb") as fh:            # row count for the progress bar
        total = max(sum(1 for _ in fh) - 1, 0)
    done, counts = 0, {}
    bar = Progress("files", total, not args.quiet)

    def tally():
        return "found %s | not found %s | errors %s" % (
            f"{counts.get('found', 0):,}", f"{counts.get('not_found', 0):,}",
            f"{counts.get('repo_error', 0) + counts.get('bad_row', 0):,}")

    with open(args.csv_in, newline="", encoding="utf-8-sig") as fin, \
            open(args.out, "w", newline="", encoding="utf-8") as fout, \
            ThreadPoolExecutor(max_workers=args.workers) as pool:
        reader, writer = csv.reader(fin, delimiter=delim), csv.writer(fout)
        header = next(reader)
        low = [h.strip().lower() for h in header]
        missing = [c for c in ("org", "repo", "relpath") if c not in low]
        if missing:
            sys.exit("missing column(s): %s (found %s)" % (missing, header))
        ix = {c: low.index(c) for c in ("org", "repo", "relpath")}
        writer.writerow(header + NEW_COLUMNS)

        while True:                       # work in batches so memory stays small
            batch = [r + [""] * (len(header) - len(r)) for _, r in
                     zip(range(2000), reader)]
            if not batch:
                break
            for out in pool.map(lambda r: process(r, ix, args.root, args.all_branches), batch):
                writer.writerow(out)
                status = out[len(header) + 1].split(":")[0]
                counts[status] = counts.get(status, 0) + 1
            done += len(batch)
            fout.flush()
            bar.update(done, tally())
        bar.close(tally())
    print("done: %s row(s) -> %s" % (f"{done:,}", args.out))


if __name__ == "__main__":
    main()
