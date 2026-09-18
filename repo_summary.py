#!/usr/bin/env python3
"""
repo_summary.py - roll a hash_inventory.py CSV up to one row per repo.

The raw inventory is far too large to open in a spreadsheet (run2 is 728 MB /
5.3M rows). This collapses it to one row per repo - small enough for Excel -
while answering "how many individual repos do we actually have?".

Repo labels need normalising before counting. hash_inventory.py labels a
repo's object store as its own repo, because a .git directory has
HEAD + objects/ + refs/ and so satisfies get_git_dir()'s bare-repo test. Left
alone that double-counts every repo. Any label segment that is a git
internals directory (".git", or a Windows copy artefact like ".git (2)") is
folded back into its parent, and its files are attributed to that parent
under a git_dir_files column so the split stays visible.

Usage:
    repo_summary.py active_inventory_run2.csv --out repo_summary_run2.csv
    repo_summary.py inventory_E.csv --out summary_E.csv --by-org
"""

import argparse
import csv
import re
import sys

csv.field_size_limit(sys.maxsize)

# ".git", ".git (2)", ".git - Copy" - any git-internals dir, however mangled
GIT_DIR = re.compile(r"^\.git(\s*[\(\-].*)?$", re.IGNORECASE)


def normalise(label, path=""):
    """(real_repo_label, is_git_internals).

    A file's git-internals status can be recorded in either column depending
    on which code path produced the row: a disk walk folds .git into the repo
    label (repo="x/y/.git", path="objects/..."), while a raw .git hash of an
    unresolvable repo leaves it in the path (repo="x/y", path=".git/...").
    Both must count as internals, so check the label's trailing segments and
    every segment of the path."""
    parts = [p for p in label.split("/") if p]
    internals = False
    while len(parts) > 1 and GIT_DIR.match(parts[-1]):
        parts.pop()
        internals = True
    if not internals:
        internals = any(GIT_DIR.match(seg) for seg in path.split("/") if seg)
    return "/".join(parts), internals


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="inventory CSV from hash_inventory.py")
    ap.add_argument("--out", required=True, help="per-repo CSV to write")
    ap.add_argument("--by-org", action="store_true",
                    help="also write <out>.orgs.csv rolled up to org level")
    ap.add_argument("--min-files", type=int, default=0,
                    help="only emit repos with at least this many files")
    args = ap.parse_args()

    files, gitfiles, size, errs = {}, {}, {}, {}
    rows = 0
    with open(args.csv_in, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            repo, internals = normalise(row.get("repo") or "",
                                        row.get("file_path") or "")
            if not repo:
                continue
            files[repo] = files.get(repo, 0) + 1
            if internals:
                gitfiles[repo] = gitfiles.get(repo, 0) + 1
            raw = (row.get("size_bytes") or "").strip()
            if raw.isdigit():
                size[repo] = size.get(repo, 0) + int(raw)
            if (row.get("error") or "").strip():
                errs[repo] = errs.get(repo, 0) + 1

    keep = sorted(r for r in files if files[r] >= args.min_files)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["repo", "org", "name", "files", "worktree_files",
                    "git_dir_files", "total_bytes", "size_mb", "error_rows"])
        for r in keep:
            org, _, name = r.partition("/")
            g = gitfiles.get(r, 0)
            b = size.get(r, 0)
            w.writerow([r, org, name or r, files[r], files[r] - g, g, b,
                        f"{b / 1024 ** 2:.2f}", errs.get(r, 0)])

    total_b = sum(size.values())
    print(f"read   {rows:,} rows from {args.csv_in}")
    print(f"wrote  {len(keep):,} repos -> {args.out}")
    print(f"       {sum(files.values()):,} files | "
          f"{total_b / 1024 ** 3:.2f} GB | "
          f"{sum(errs.values()):,} error rows")

    if args.by_org:
        orgs = {}
        for r in keep:
            org = r.partition("/")[0]
            o = orgs.setdefault(org, [0, 0, 0])
            o[0] += 1
            o[1] += files[r]
            o[2] += size.get(r, 0)
        path = args.out + ".orgs.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["org", "repos", "files", "total_bytes", "size_gb"])
            for org in sorted(orgs, key=lambda k: -orgs[k][0]):
                n, f_, b = orgs[org]
                w.writerow([org, n, f_, b, f"{b / 1024 ** 3:.2f}"])
        print(f"wrote  {len(orgs)} orgs -> {path}")


if __name__ == "__main__":
    main()
