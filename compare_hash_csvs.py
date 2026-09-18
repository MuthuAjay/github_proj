#!/usr/bin/env python3
"""
compare_hash_csvs.py - diff two hash_working_tree.py CSVs (e.g. an
"active" root vs an "archive" root) and report per-file matches,
mismatches, and files present on only one side.

Rows are matched by (repo_name, path relative to the repo's .git dir) -
not by full_path, since the two roots live at different absolute paths.
A file only "matches" if its md5, sha256, AND git_object all agree.

Usage:
    python compare_hash_csvs.py left.csv right.csv --out diff.csv
    python compare_hash_csvs.py left.csv right.csv --out diff.csv \\
        --left-label active --right-label archive
"""

import argparse
import csv
import sys

REPORT_HEADER = [
    "repo_name", "rel_path", "status",
    "{label}_full_path", "{label}_md5", "{label}_sha256",
    "{label}_git_object", "{label}_size_bytes",
]


def rel_to_git_dir(full_path):
    """full_path -> path relative to its repo's .git dir (or the bare repo
    root), by cutting at the last '/.git/'. Falls back to the full path
    unchanged if that marker isn't present (e.g. a bare repo root itself)."""
    marker = "/.git/"
    idx = full_path.rfind(marker)
    if idx == -1:
        return full_path
    return full_path[idx + len(marker):]


def load(path):
    """{(repo_name, rel_path): row_dict, ...}"""
    rows = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["repo_name"], rel_to_git_dir(row["full_path"]))
            rows[key] = row
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("left_csv", help="hash_working_tree.py CSV, side A")
    ap.add_argument("right_csv", help="hash_working_tree.py CSV, side B")
    ap.add_argument("--out", required=True, help="CSV file to write the diff to")
    ap.add_argument("--left-label", default="left", help="column prefix for left_csv (default: left)")
    ap.add_argument("--right-label", default="right", help="column prefix for right_csv (default: right)")
    args = ap.parse_args()

    left = load(args.left_csv)
    right = load(args.right_csv)

    left_repos = {k[0] for k in left}
    right_repos = {k[0] for k in right}
    common_repos = left_repos & right_repos

    only_left_repos = sorted(left_repos - right_repos)
    only_right_repos = sorted(right_repos - left_repos)
    if only_left_repos:
        print(f"repos only in {args.left_csv}: {', '.join(only_left_repos)}", file=sys.stderr)
    if only_right_repos:
        print(f"repos only in {args.right_csv}: {', '.join(only_right_repos)}", file=sys.stderr)

    keys_left = set(left)
    keys_right = set(right)

    header = ([REPORT_HEADER[0], REPORT_HEADER[1], REPORT_HEADER[2]] +
              [c.format(label=args.left_label) for c in REPORT_HEADER[3:]] +
              [c.format(label=args.right_label) for c in REPORT_HEADER[3:]])

    def empty_side():
        return ["", "", "", "", ""]

    def side_fields(row):
        return [row["full_path"], row["md5"], row["sha256"],
                row["git_object"], row["size_bytes"]]

    matches = mismatches = only_left = only_right = 0

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)

        for key in sorted(keys_left | keys_right):
            repo, rel_path = key
            if key in keys_left and key in keys_right:
                l, r = left[key], right[key]
                same = (l["md5"] == r["md5"] and l["sha256"] == r["sha256"] and
                       l["git_object"] == r["git_object"])
                status = "match" if same else "mismatch"
                if same:
                    matches += 1
                else:
                    mismatches += 1
                writer.writerow([repo, rel_path, status] + side_fields(l) + side_fields(r))
            elif key in keys_left:
                only_left += 1
                writer.writerow([repo, rel_path, f"only_{args.left_label}"] +
                                side_fields(left[key]) + empty_side())
            else:
                only_right += 1
                writer.writerow([repo, rel_path, f"only_{args.right_label}"] +
                                empty_side() + side_fields(right[key]))

    print(f"common repos: {len(common_repos)}", file=sys.stderr)
    print(f"match: {matches:,}  mismatch: {mismatches:,}  "
          f"only_{args.left_label}: {only_left:,}  only_{args.right_label}: {only_right:,}",
          file=sys.stderr)
    print(f"-> {args.out}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
