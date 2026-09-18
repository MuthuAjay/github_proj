#!/usr/bin/env python3
"""
summarise_extracts.py - one row per commit extraction produced by
extract_commits.py.

Rolls each <base>/<extraction>/ directory up into a single CSV row so the whole
corpus fits in a spreadsheet. Reads manifest.json, commits.csv, refs.csv and
every per-commit changes.csv, and measures what actually landed on disk.

"unique" is reported two ways, because they answer different questions:
  unique_paths  - distinct file paths ever touched  (how wide is the repo)
  unique_blobs  - distinct file contents ever written (how much real data)
files_written / unique_blobs is the duplication factor: a commit-per-folder
layout rewrites a file every time any commit touches it, so a value of 7 means
seven copies of the average version are on disk.
"""

import argparse
import collections
import csv
import json
import os
import statistics
import sys

GB = 1024 ** 3

COLUMNS = [
    "extraction", "source_repo", "content_mode",
    "commits", "merges", "root_commits", "dangling_commits", "errors",
    "authors", "first_commit", "last_commit",
    "refs_live", "branches", "remote_branches", "tags",
    "refs_from_reflog", "refs_inferred", "branch_names_total",
    "change_records", "files_written", "unique_paths", "unique_blobs",
    "redundancy_x",
    "added", "modified", "deleted", "renamed_copied",
    "median_changed_files", "mean_changed_files", "max_changed_files",
    "max_tree_files", "median_tree_files",
    "content_bytes", "content_gb",
    "tree_csv_bytes", "tree_csv_gb",
    "metadata_bytes", "disk_bytes", "disk_gb",
    "content_share_pct", "tree_share_pct",
    "extract_seconds",
]


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        return list(csv.DictReader(fh))


def summarise(base, name):
    d = os.path.join(base, name)
    man_path = os.path.join(d, "manifest.json")
    com_path = os.path.join(d, "commits.csv")
    if not (os.path.isfile(man_path) and os.path.isfile(com_path)):
        return None

    man = json.load(open(man_path))
    commits = read_csv_rows(com_path)

    # --- refs ------------------------------------------------------------
    kinds = collections.Counter()
    refs_path = os.path.join(d, "refs.csv")
    if os.path.isfile(refs_path):
        for r in read_csv_rows(refs_path):
            kinds[r.get("kind", "")] += 1

    # --- per-commit change records ----------------------------------------
    status = collections.Counter()
    paths, blobs = set(), set()
    change_records = 0
    for row in commits:
        cpath = os.path.join(d, row["folder"], "changes.csv")
        if not os.path.isfile(cpath):
            continue
        with open(cpath, newline="", encoding="utf-8",
                  errors="surrogateescape") as fh:
            for c in csv.DictReader(fh):
                change_records += 1
                st = (c.get("status") or "?")[0]
                status[st] += 1
                if c.get("path"):
                    paths.add(c["path"])
                nb = c.get("new_blob") or ""
                # an all-zero blob is the null object a deletion points at
                if st != "D" and nb and set(nb) != {"0"}:
                    blobs.add(nb)

    # --- what is actually on disk ----------------------------------------
    content_bytes = files_written = 0
    tree_bytes = meta_bytes = 0
    disk_bytes = 0
    for root, _dirs, filenames in os.walk(d):
        in_files = os.sep + "files" + os.sep in root + os.sep
        for fn in filenames:
            full = os.path.join(root, fn)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            disk_bytes += st.st_blocks * 512
            if in_files:
                content_bytes += st.st_size
                files_written += 1
            elif fn == "tree.csv":
                tree_bytes += st.st_size
            else:
                meta_bytes += st.st_size

    ch = [int(r["changed_files"] or 0) for r in commits]
    ft = [int(r["file_count"] or 0) for r in commits]
    dates = sorted(r["author_date"][:10] for r in commits if r["author_date"])

    return {
        "extraction": name,
        "source_repo": os.path.basename(man.get("repo", "").rstrip("/")),
        "content_mode": man.get("content_mode", ""),
        "commits": len(commits),
        "merges": sum(1 for r in commits if r["is_merge"] == "1"),
        "root_commits": sum(1 for r in commits if r["is_root"] == "1"),
        "dangling_commits": man.get("commits_dangling", 0),
        "errors": sum(1 for r in commits if r["error"]),
        "authors": len({r["author_email"] or r["author_name"] for r in commits}),
        "first_commit": dates[0] if dates else "",
        "last_commit": dates[-1] if dates else "",
        "refs_live": man.get("refs_live", 0),
        "branches": kinds.get("branch", 0),
        "remote_branches": kinds.get("remote", 0),
        "tags": kinds.get("tag", 0),
        "refs_from_reflog": man.get("refs_recovered_from_reflog", 0),
        "refs_inferred": man.get("refs_inferred_from_prose", 0),
        "branch_names_total": (kinds.get("branch", 0) + kinds.get("remote", 0)
                               + kinds.get("deleted", 0)),
        "change_records": change_records,
        "files_written": files_written,
        "unique_paths": len(paths),
        "unique_blobs": len(blobs),
        "redundancy_x": round(files_written / len(blobs), 2) if blobs else 0,
        "added": status.get("A", 0),
        "modified": status.get("M", 0),
        "deleted": status.get("D", 0),
        "renamed_copied": status.get("R", 0) + status.get("C", 0),
        "median_changed_files": int(statistics.median(ch)) if ch else 0,
        "mean_changed_files": round(statistics.mean(ch), 1) if ch else 0,
        "max_changed_files": max(ch) if ch else 0,
        "max_tree_files": max(ft) if ft else 0,
        "median_tree_files": int(statistics.median(ft)) if ft else 0,
        "content_bytes": content_bytes,
        "content_gb": round(content_bytes / GB, 3),
        "tree_csv_bytes": tree_bytes,
        "tree_csv_gb": round(tree_bytes / GB, 3),
        "metadata_bytes": meta_bytes,
        "disk_bytes": disk_bytes,
        "disk_gb": round(disk_bytes / GB, 3),
        "content_share_pct": round(100 * content_bytes / disk_bytes, 1) if disk_bytes else 0,
        "tree_share_pct": round(100 * tree_bytes / disk_bytes, 1) if disk_bytes else 0,
        "extract_seconds": man.get("elapsed_seconds", ""),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", help="directory holding the extraction folders")
    ap.add_argument("--out", help="output CSV (default: <base>/extraction_summary.csv)")
    args = ap.parse_args()

    base = os.path.abspath(args.base)
    out = args.out or os.path.join(base, "extraction_summary.csv")

    rows = []
    for name in sorted(os.listdir(base)):
        if not os.path.isdir(os.path.join(base, name)):
            continue
        print("  reading %s ..." % name, flush=True)
        row = summarise(base, name)
        if row is None:
            print("      skipped (no manifest.json / commits.csv)")
            continue
        rows.append(row)

    if not rows:
        sys.exit("no extractions found under " + base)

    rows.sort(key=lambda r: -r["disk_bytes"])
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    tot = {k: sum(r[k] for r in rows) for k in
           ("commits", "merges", "change_records", "files_written",
            "unique_blobs", "disk_bytes", "content_bytes", "tree_csv_bytes",
            "errors", "refs_live", "refs_inferred")}
    print("\nwrote %d extraction(s) -> %s" % (len(rows), out))
    print("      %d commits | %d change records | %d files written"
          % (tot["commits"], tot["change_records"], tot["files_written"]))
    print("      %.2f GB on disk (%.2f GB content, %.2f GB tree.csv) | %d error(s)"
          % (tot["disk_bytes"] / GB, tot["content_bytes"] / GB,
             tot["tree_csv_bytes"] / GB, tot["errors"]))


if __name__ == "__main__":
    main()
