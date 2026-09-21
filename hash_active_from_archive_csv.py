#!/usr/bin/env python3
"""
hash_active_from_archive_csv.py - for every row of the archive inventory CSV
(produced by hash_working_tree.py), locate the same file under the active
directory, hash it (md5 + sha256 + git blob sha1) and write a COPY of the CSV
with the active-side results appended.

Path mapping: the archive full_path looks like
    <archive_root>/<repo_org>/<repo_name>/.git/...
The part before the org folder (<archive_root>) is swapped for --active-root,
giving <active_root>/<repo_org>/<repo_name>/.git/... . The archive root is
detected per row from repo_org/repo_name, so it need not be given (pass
--archive-root only to force a specific prefix).

Added columns: active_path, active_md5, active_sha256, active_git_object,
active_size_bytes, active_error, match (YES / NO / MISSING / ERROR).

Usage:
    python hash_active_from_archive_csv.py archive_inventory_2.csv \\
        --active-root /data/workarea/active --out archive_inventory_2_active.csv
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from hash_working_tree import hash_file

NEW_COLS = ["active_path", "active_md5", "active_sha256", "active_git_object",
            "active_size_bytes", "active_error", "match"]


def map_path(row, active_root, archive_root):
    """Archive full_path -> active full_path, or None if the org/repo
    anchor can't be located in the path."""
    full = row["full_path"]
    if archive_root:
        prefix = archive_root.rstrip("/") + "/"
        return active_root + "/" + full[len(prefix):] if full.startswith(prefix) else None
    org, name = row["repo_org"], row["repo_name"]
    marker = "/" + (org + "/" if org else "") + name + "/"
    idx = full.find(marker)
    if idx < 0:
        return None
    return active_root.rstrip("/") + full[idx:]


def process(row, active_root, archive_root):
    out = {c: "" for c in NEW_COLS}
    if row.get("error"):
        out["match"] = "ERROR"
        out["active_error"] = "archive row had an error; not checked"
        return out
    active = map_path(row, active_root, archive_root)
    if active is None:
        out["match"] = "ERROR"
        out["active_error"] = "cannot derive active path (org/repo not found in full_path)"
        return out
    out["active_path"] = active
    if not os.path.isfile(active):
        out["match"] = "MISSING"
        out["active_error"] = "file not found"
        return out
    try:
        md5, sha256, git_hash, size = hash_file(active)
    except OSError as exc:
        out["match"] = "ERROR"
        out["active_error"] = str(exc)
        return out
    out.update(active_md5=md5, active_sha256=sha256, active_git_object=git_hash,
               active_size_bytes=size)
    out["match"] = "YES" if (md5 == row["md5"] and sha256 == row["sha256"]
                             and git_hash == row["git_object"]) else "NO"
    return out


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def render(done, total, start, nbytes, counts):
    elapsed = time.time() - start
    frac = done / total if total else 1.0
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate else 0
    bar_w = 30
    filled = int(bar_w * frac)
    bar = "#" * filled + "-" * (bar_w - filled)
    line = (f"\r[{bar}] {frac * 100:5.1f}% {done:,}/{total:,} | "
            f"{nbytes / 1024 ** 3:.2f} GB | {rate:,.0f} files/s | "
            f"ok {counts.get('YES', 0):,} diff {counts.get('NO', 0):,} "
            f"miss {counts.get('MISSING', 0):,} err {counts.get('ERROR', 0):,} | "
            f"{fmt_time(elapsed)} ETA {fmt_time(eta)}")
    print(line.ljust(140), end="", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="archive inventory CSV")
    ap.add_argument("--active-root", required=True,
                    help="active directory that replaces the archive root "
                         "(the folder that contains the org folders)")
    ap.add_argument("--archive-root", default=None,
                    help="archive root prefix to strip (default: auto-detect "
                         "from repo_org/repo_name)")
    ap.add_argument("--out", required=True, help="CSV copy to write")
    ap.add_argument("--workers", type=int, default=8, help="hashing threads")
    args = ap.parse_args()

    if not os.path.isdir(args.active_root):
        print(f"not a directory: {args.active_root}", file=sys.stderr)
        return 2

    with open(args.csv_in, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)

    counts = {}
    total_bytes = 0
    start = last_render = time.time()
    with open(args.out, "w", newline="", encoding="utf-8") as fh, \
            ThreadPoolExecutor(max_workers=args.workers) as pool:
        writer = csv.DictWriter(fh, fieldnames=fields + NEW_COLS)
        writer.writeheader()
        # map() yields in input order, so output order matches the input CSV
        results = pool.map(lambda r: process(r, args.active_root, args.archive_root), rows)
        for i, (row, res) in enumerate(zip(rows, results), 1):
            row.update(res)
            writer.writerow(row)
            counts[res["match"]] = counts.get(res["match"], 0) + 1
            total_bytes += int(res["active_size_bytes"] or 0)
            now = time.time()
            if i == len(rows) or now - last_render >= 0.2:
                last_render = now
                render(i, len(rows), start, total_bytes, counts)

    print(f"done:{len(rows):,} row(s) in {time.time() - start:.0f}s -> {args.out}")
    for k in ("YES", "NO", "MISSING", "ERROR"):
        print(f"  {k}: {counts.get(k, 0):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
