#!/usr/bin/env python3
"""
repo_extension_summary.py - one row per repo from the file_summary.csv that
file_history_for_list.py writes: how many files, how many commits touched
them, and how those files split across a fixed list of extensions.

Output columns:

    org, repo
    files                 distinct files with history (status found)
    commits_touched       sum of commits_touched over those files - a commit
                          that changed three files counts three times
    not_found             listed paths with no history, excluding .git
                          internals (hooks, HEAD, config - never committed)
    repo_status           ok / repo_missing / repo_error / timeout / bad_row
                          when EVERY row of the repo had that status
    <ext>                 files with that extension, one column per entry in
                          the extension list (default: EXTENSIONS below)
    other                 files whose extension is not in the list
    other_extensions      what those were, most common first: "dll:12|png:7"

With --ext-commits, each <ext> column is followed by <ext>_commits (the
commits_touched of that extension's files) and `other` by other_commits.

A last row, org = "(all)", totals every column.

Extensions are matched on the file name, case-insensitively. A dotfile such as
.gitignore counts as the extension "gitignore". Rows repeating the same
(org, repo, matched_path) - duplicate input rows - are counted once.

Usage:
    python3 repo_extension_summary.py file_summary.csv --out repo_summary.csv
    python3 repo_extension_summary.py file_summary.csv --out repo_summary.csv \\
        --ext-commits --extensions json,cs,ts
"""

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

from analyse_file_summary import is_git_internal

EXTENSIONS = ["json", "csv", "xml", "sql", "py", "txt", "cs", "yaml", "md",
              "yml", "html", "ts", "java", "tsx", "properties", "log", "bat",
              "js", "jsx", "ps1", "pem", "jks", "ini", "manifest", "sln",
              "xhtml", "gitignore", "sh", "css", "tsv", "config", "htm"]

NEEDED = ["org", "repo", "relpath", "filename", "matched_path", "status",
          "commits_touched"]


def ext_key(name):
    """'a/b/App.Config' -> 'config', '.gitignore' -> 'gitignore',
    'Makefile' -> '' (no extension)."""
    leaf = (name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    if leaf.startswith(".") and leaf.count(".") == 1:
        return leaf[1:]
    stem, dot, e = leaf.rpartition(".")
    return e if dot and stem else ""


def load_extensions(spec):
    """--extensions: a comma list, or a file with one extension per line."""
    if not spec:
        return list(EXTENSIONS)
    if os.path.isfile(spec):
        with open(spec, encoding="utf-8") as fh:
            items = fh.read().split()
    else:
        items = spec.split(",")
    out = []
    for e in items:
        e = e.strip().lstrip(".").lower()
        if e and e not in out:
            out.append(e)
    return out


class Repo:
    __slots__ = ("files", "commits", "not_found", "statuses", "ext_files",
                 "ext_commits", "other_exts")

    def __init__(self):
        self.files = self.commits = self.not_found = 0
        self.statuses = Counter()
        self.ext_files = Counter()
        self.ext_commits = Counter()
        self.other_exts = Counter()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="file_summary.csv from file_history_for_list.py")
    ap.add_argument("--out", default="repo_extension_summary.csv",
                    help="output CSV (default: repo_extension_summary.csv)")
    ap.add_argument("--extensions",
                    help="comma list, or a file with one per line "
                         "(default: the built-in list)")
    ap.add_argument("--ext-commits", action="store_true",
                    help="also a <ext>_commits column after each extension")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        sys.exit("not a file: " + args.csv_in)
    exts = load_extensions(args.extensions)
    wanted = set(exts)
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

    repos = defaultdict(Repo)
    seen = set()                  # hash of (org, repo, path): 5M tuples is GBs
    rows = dups = 0
    with open(args.csv_in, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            sys.exit("empty file: " + args.csv_in)
        missing = [c for c in NEEDED if c not in header]
        if missing:
            sys.exit("missing column(s) %s; found %s" % (missing, header))
        ix = {c: header.index(c) for c in NEEDED}
        width = len(header)

        for row in reader:
            rows += 1
            if len(row) < width:
                row = row + [""] * (width - len(row))
            org, repo = row[ix["org"]], row[ix["repo"]]
            st = row[ix["status"]]
            r = repos[(org, repo)]
            r.statuses[st] += 1
            path = row[ix["matched_path"]]
            if st == "not_found":
                if not is_git_internal(path, row[ix["relpath"]]):
                    r.not_found += 1
                continue
            if st != "found":
                continue

            key = hash((org, repo, path))
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            try:
                n = int(row[ix["commits_touched"]] or 0)
            except ValueError:
                n = 0
            r.files += 1
            r.commits += n
            e = ext_key(path or row[ix["filename"]])
            if e in wanted:
                r.ext_files[e] += 1
                r.ext_commits[e] += n
            else:
                r.ext_files[None] += 1
                r.ext_commits[None] += n
                r.other_exts[e or "(none)"] += 1
    seen.clear()

    header = ["org", "repo", "files", "commits_touched", "not_found",
              "repo_status"]
    for e in exts:
        header += [e, e + "_commits"] if args.ext_commits else [e]
    header += (["other", "other_commits"] if args.ext_commits else ["other"])
    header.append("other_extensions")

    def line(org, repo, r, status):
        out = [org, repo, r.files, r.commits, r.not_found, status]
        for e in exts + [None]:
            out.append(r.ext_files[e])
            if args.ext_commits:
                out.append(r.ext_commits[e])
        out.append("|".join("%s:%d" % kv for kv in r.other_exts.most_common()))
        return out

    total = Repo()
    with open(args.out, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for (org, repo), r in sorted(repos.items()):
            only = list(r.statuses)
            status = ("ok" if r.files or r.not_found or len(only) != 1
                      else only[0])
            w.writerow(line(org, repo, r, status))
            total.files += r.files
            total.commits += r.commits
            total.not_found += r.not_found
            total.ext_files.update(r.ext_files)
            total.ext_commits.update(r.ext_commits)
            total.other_exts.update(r.other_exts)
        w.writerow(line("(all)", "", total, ""))

    print("%s row(s), %d repo(s), %s file(s), %s commit(s) touched%s -> %s"
          % (f"{rows:,}", len(repos), f"{total.files:,}",
             f"{total.commits:,}",
             (", %s duplicate row(s) skipped" % f"{dups:,}") if dups else "",
             args.out))


if __name__ == "__main__":
    main()
