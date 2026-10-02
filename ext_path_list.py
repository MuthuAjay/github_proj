#!/usr/bin/env python3
"""
ext_path_list.py - the unique file paths of some extensions, per repo, with
the number of commits each one is in: one CSV a team can work from, plus a
legend and a per-extension summary.

Reads an extension_versions.py output folder (pass 1 is enough):
  _state/<org>/<repo>/files.csv      one row per path (commits, versions...)
  _state/<org>/<repo>/versions.csv   one row per distinct content (dates)
of every repo whose pass 1 finished ok, and writes:

  <out>                one row per (org, repo, path) ever in history:
                       ext, commits, first_date, last_date, last_event,
                       still_today, sizes, vendored
  <out minus .csv>_legend.csv     what each column means
  <out minus .csv>_by_extension.csv   repos, paths, commits per extension

"commits" = the commits that added, changed or deleted the path, on any
branch (merges counted against their first parent) - the "changes" of the
other reports. Paths only in today's copy (never in history) are left out:
they have no commits.

Usage:
    python3 ext_path_list.py /data/workarea/group4_1/counts \\
        --out /data/workarea/group4_1/group4_1_paths.csv
"""

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

from extension_versions import STATE, read_json, read_rows
from repo_extension_summary import load_extensions

HEADER = ["org", "repo", "path", "ext", "commits", "first_date", "last_date", "last_event", "still_today",
          "latest_bytes", "max_bytes", "lfs_versions", "vendored"]
LEGEND = [
    ("org, repo", "the repository"),
    ("path", "the file's path in the repo (every path it ever had: a rename "
             "is a new path)"),
    ("ext", "the extension, lower case"),
    ("commits", "commits that added, changed or deleted this path, on any "
                "branch"),
    ("first_date", "date of the first commit that gave the path a content"),
    ("last_date", "date of the last commit that gave the path a content"),
    ("last_event", "what the last commit did: A added, M modified, D deleted, "
                   "T type change"),
    ("still_today", "yes = the path is in today's copy of the repo (13 Aug "
                    "2026); no = deleted, renamed away or only on another "
                    "branch; blank = the repo has no copy today"),
    ("latest_bytes", "size of the latest content, in bytes"),
    ("max_bytes", "size of the largest content it ever had, in bytes"),
    ("lfs_versions", "versions stored in Git LFS: only a pointer is in the "
                     "archive, not the file"),
    ("vendored", "the path is under node_modules, packages, bin, obj, ... "
                 "(third-party or build output)"),
]
EVENTS = {"A": "A", "M": "M", "D": "D", "T": "T"}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("counts", help="extension_versions.py output folder")
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--extensions", help="only these (comma list or file); "
                                         "default: every extension counted")
    args = ap.parse_args()
    state = os.path.join(args.counts, STATE)
    if not os.path.isdir(state):
        sys.exit("no _state folder under " + args.counts)
    only = set(load_extensions(args.extensions)) if args.extensions else None

    per_ext = defaultdict(Counter)
    repos_of = defaultdict(set)
    n_repos = n_skipped = n_rows = 0
    base = args.out[:-4] if args.out.lower().endswith(".csv") else args.out
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out + ".tmp", "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for org in sorted(os.listdir(state)):
            od = os.path.join(state, org)
            if not os.path.isdir(od):
                continue
            for repo in sorted(os.listdir(od)):
                sd = os.path.join(od, repo)
                done = read_json(os.path.join(sd, "done.json"))
                if not done or done.get("status") != "ok":
                    n_skipped += 1
                    continue
                n_repos += 1
                dates = defaultdict(list)
                for v in read_rows(os.path.join(sd, "versions.csv")):
                    for d in (v["first_date"], v["last_date"]):
                        if d:
                            dates[v["path"]].append(d)
                for f in read_rows(os.path.join(sd, "files.csv")):
                    e = f["ext"]
                    if f["in_history"] != "yes" or (only and e not in only):
                        continue
                    ds = sorted(dates.get(f["path"], []))
                    w.writerow([org, repo, f["path"], e, f["commits"],
                                ds[0] if ds else "",
                                ds[-1] if ds else "",
                                EVENTS.get(f["last_event"], f["last_event"]),
                                f["at_head"], f["latest_bytes"], f["max_bytes"],
                                f["lfs_versions"], "yes" if f["vendored"] else ""])
                    n_rows += 1
                    c = per_ext[e]
                    c["paths"] += 1
                    c["commits"] += int(f["commits"] or 0)
                    c["still_today"] += f["at_head"] == "yes"
                    c["vendored"] += bool(f["vendored"])
                    repos_of[e].add((org, repo))
    os.replace(args.out + ".tmp", args.out)

    with open(base + "_legend.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["column", "meaning"])
        w.writerows(LEGEND)
    cols = ["repos", "paths", "commits", "still_today", "vendored"]
    with open(base + "_by_extension.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext"] + cols)
        tot = Counter()
        for e in sorted(per_ext, key=lambda e: (-per_ext[e]["paths"], e)):
            per_ext[e]["repos"] = len(repos_of[e])
            w.writerow([e] + [per_ext[e][c] for c in cols])
            tot.update({c: per_ext[e][c] for c in cols if c != "repos"})
        w.writerow(["total", len(set().union(*repos_of.values())) if repos_of else 0]
                   + [tot[c] for c in cols[1:]])
    print("%s paths from %s repo(s) -> %s" % (f"{n_rows:,}", f"{n_repos:,}", args.out))
    print("legend: %s_legend.csv | per extension: %s_by_extension.csv"
          % (base, base))
    if n_skipped:
        print("note: %d repo(s) without a finished pass 1 are not in it - "
              "rerun the counts to finish them" % n_skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
