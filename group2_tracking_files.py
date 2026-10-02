#!/usr/bin/env python3
"""
group2_tracking_files.py - list the tracking files in a group 2 delivery
folder (manifests, done.json, summaries, reports) and, if asked, move them
out into another folder so only the data files stay in scope.

The delivery made by copy_group2.sh has three parts:

  binary/files/<ext>/<ab>/<blob>.<ext>   DATA  (the files to scan)
  binary/manifest.csv, summaries,
  binary/_state/<org>/<repo>/...         tracking
  text/<org>/<repo>/<path>.txt           DATA  (the text delta)
  text/_state/<org>/<repo>/manifest.csv,
               done.json                 tracking
  reports/...                            tracking

Everything that is not DATA is tracking, except rsync leftovers
(`*.tmp.*` under binary/files), which are listed apart and never moved.

Default: list only, nothing changes. Writes <out>/tracking_files.csv (one
row per tracking file: path relative to the folder, kind, bytes) and prints
the counts per kind, plus data and leftover counts, so they can be checked
against the delivery (group 2: 228,676 data + 54,534 tracking = 283,210).

The folder can also be the PII scanner's output, which mirrors the
delivery once per run (pii_scanner_output_<ts>/run01_<ts>/<source>/binary,
text, reports): every folder up to 3 levels down that holds binary/, text/
or reports/ is treated as one delivery, and paths are listed relative to
the folder given (run01_<ts>/<source>/text/_state/...).

--move DEST moves every listed tracking file to DEST, keeping its relative
path (DEST/text/_state/<org>/<repo>/done.json, ...). DEST must not be
inside the folder. A file already at DEST and gone from the folder counts as
moved, so an interrupted move can be run again; a file present in both
places is not touched and is reported. Each move is logged in
<out>/moved_files.csv. Empty folders left behind are removed.

Usage:
    python3 group2_tracking_files.py /share/.../pii_scanner_exports_group2 --out /data/workarea/g2_tracking
    python3 group2_tracking_files.py /share/.../pii_scanner_exports_group2 --out /data/workarea/g2_tracking \\
        --move /share/.../pii_scanner_exports_group2_tracking
"""

import argparse
import csv
import os
import shutil
import sys
from collections import Counter


def kind_of(rel):
    """'data', 'leftover' or the tracking kind of a path relative to the
    delivery folder (always with '/')."""
    parts = rel.split("/")
    top = parts[0]
    if top == "binary":
        if len(parts) > 2 and parts[1] == "files":
            return "leftover" if ".tmp." in parts[-1] else "data"
        if len(parts) > 1 and parts[1] == "_state":
            return "binary_state"
        return "binary_manifest" if rel == "binary/manifest.csv" else "binary_summary"
    if top == "text":
        if len(parts) > 1 and parts[1] == "_state":
            leaf = parts[-1]
            return ("text_done_json" if leaf == "done.json" else
                    "text_manifest" if leaf == "manifest.csv" else "text_state_other")
        return "data" if rel.endswith(".txt") else "text_other"
    if top == "reports":
        return "reports"
    return "other"


PARTS = ("binary", "text", "reports")


def join(pre, rel):
    return pre + "/" + rel if pre else rel


def delivery_roots(root, depth=3):
    """[(prefix relative to root, parts found)]: root itself when it holds
    binary/, text/ or reports/, else every folder up to `depth` levels down
    that does (the scanner output: run01_<ts>/<source>/)."""
    out, level = [], [""]
    for _ in range(depth + 1):
        nxt = []
        for pre in level:
            d = os.path.join(root, pre) if pre else root
            found = [t for t in PARTS if os.path.isdir(os.path.join(d, t))]
            if found:
                out.append((pre, found))
                continue
            try:
                kids = sorted(e.name for e in os.scandir(d) if e.is_dir())
            except OSError:
                continue
            nxt += [join(pre, k) for k in kids]
        level = nxt
    return out


def walk(root):
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            yield os.path.relpath(p, root).replace(os.sep, "/"), p


def remove_empty_dirs(root):
    for d, dirs, files in os.walk(root, topdown=False):
        if d != root and not os.listdir(d):
            os.rmdir(d)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="the group 2 delivery folder (binary/, text/, reports/)")
    ap.add_argument("--out", required=True, help="folder for tracking_files.csv / moved_files.csv")
    ap.add_argument("--move", metavar="DEST", help="move the tracking files here")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit("not a folder: " + root)
    subs = delivery_roots(root)
    if not subs:
        sys.exit("no binary/, text/ or reports/ under %s (3 levels) - is this "
                 "the delivery or the scanner output folder?" % root)
    dest = os.path.abspath(args.move) if args.move else None
    if dest and (dest == root or dest.startswith(root + os.sep)):
        sys.exit("--move DEST must be outside " + root)
    os.makedirs(args.out, exist_ok=True)

    counts, tracking = Counter(), []
    for pre, found in subs:
        for rel, p in walk(os.path.join(root, pre) if pre else root):
            k = kind_of(rel)
            counts[k] += 1
            if k not in ("data", "leftover"):
                tracking.append((join(pre, rel), k, os.path.getsize(p)))
    if dest and os.path.isdir(dest):            # a rerun: count what is already moved
        have = {r for r, _, _ in tracking}
        for rel, _ in walk(dest):
            if rel not in have:
                counts["already_moved"] += 1

    listing = os.path.join(args.out, "tracking_files.csv")
    with open(listing, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "kind", "bytes"])
        w.writerows(tracking)

    print("folder:", root)
    for pre, found in subs:
        print("  delivery: %s (%s)" % (pre or ".", ", ".join(found)))
    n_track = len(tracking)
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("  %-18s %10s" % (k, format(n, ",")))
    print("  %-18s %10s" % ("tracking total", format(n_track, ",")))
    print("  %-18s %10s" % ("all files", format(sum(v for k, v in counts.items()
                                                       if k != "already_moved"), ",")))
    print("list:", listing)
    if not dest:
        print("nothing moved (add --move DEST to move them)")
        return 0

    log = os.path.join(args.out, "moved_files.csv")
    new_log = not os.path.exists(log)
    moved = clash = failed = 0
    with open(log, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_log:
            w.writerow(["path", "kind", "bytes", "result"])
        for rel, k, size in tracking:
            src = os.path.join(root, rel)
            dst = os.path.join(dest, rel)
            if os.path.exists(dst):
                clash += 1
                w.writerow([rel, k, size, "exists_at_dest_not_moved"])
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moved += 1
                w.writerow([rel, k, size, "moved"])
            except OSError as e:
                failed += 1
                w.writerow([rel, k, size, "error: %s" % e])
    for pre, found in subs:
        for t in found:
            remove_empty_dirs(os.path.join(root, pre, t))
    print("moved %s, already at dest (not moved) %s, failed %s -> %s"
          % (format(moved, ","), format(clash, ","), format(failed, ","), dest))
    print("log:", log)
    left = sum(1 for pre, _ in subs
               for rel, _ in walk(os.path.join(root, pre) if pre else root)
               if kind_of(rel) not in ("data", "leftover"))
    print("tracking files left in the folder:", format(left, ","))
    return 1 if (failed or clash or left) else 0


if __name__ == "__main__":
    sys.exit(main())
