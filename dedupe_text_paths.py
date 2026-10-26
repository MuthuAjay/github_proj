#!/usr/bin/env python3
"""
dedupe_text_paths.py - remove the .txt of a path that was built from exactly
the same contents as a path already kept: bi_to_data.py / data_to_text.py
read path by path, so the same file committed at several paths (release /
rollback / deployment copies) gave identical .txt files.

A path's signature is the set of contents its .txt was made from: the git
blob ids of its stored versions (the extraction's manifest, actions written
/ deduped) plus today's file when it was read - its blob from the counts'
pass 2 (identical.csv: same_as_latest / same_as_older / same_as_other_path).
Today's file that matches no history version (no_match, lfs_active_real,
unchecked) makes the path unique: it is never removed. Same signature ->
same contents read -> the same lines (only their order can differ).

  --scope repo   a path is a duplicate of an earlier path in the SAME repo
  --scope all    ... or of a path in any repo (repos in name order; the
                 first one keeps the .txt)

Dry run by default: prints and writes the counts, changes nothing. With
--apply each duplicate .txt is MOVED (not deleted) to
<out>/_removed_duplicates/<same path> - check, then delete that folder.
Run it last: a later --redo of a repo writes its .txt files again.

Output, under --out (the bi_to_data.py / data_to_text.py output):
  _state/<org>/<repo>/same_as.csv   per removed (or removable) path: the
                                    path kept, its repo, bytes
  dedupe_<scope>.csv                per repo: paths, kept, duplicates, GB
  dedupe_<scope>.md                 totals

Usage:
    python3 dedupe_text_paths.py --out /data/workarea/group4_7_9/bi/data \\
        --in /data/workarea/group4_7_9/bi/extract \\
        --state /data/workarea/group4_7_9/bi/counts --text-dir data_text --scope repo
    (then the same with --apply)
"""

import argparse
import csv
import os
import shutil
import sys
from collections import Counter

from extension_versions import read_rows
from file_delta import csv_text, write_atomic
from group4_2_text import clean_path

STATE = "_state"
STORED = ("written", "deduped")
TODAY_MATCH = ("same_as_latest", "same_as_older", "same_as_other_path")
SAME_AS_HEADER = ["path", "kept_org", "kept_repo", "kept_path", "bytes", "action"]


def signatures(cfg, org, repo):
    """-> {path: signature or None (unique)} for the paths that have a .txt."""
    man = os.path.join(cfg.out, STATE, org, repo, "manifest.csv")
    if not os.path.isfile(man):
        return {}
    out_rows = [r for r in read_rows(man) if int(r.get("lines") or 0) > 0]
    if not out_rows:
        return {}
    blobs = {}
    src = os.path.join(cfg.src, STATE, org, repo, "manifest.csv")
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with open(src, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            if r["action"] in STORED and r.get("stored_as"):
                blobs.setdefault(r["path"], set()).add(r["blob"])
    today = {}
    if cfg.state:
        ident = os.path.join(cfg.state, STATE, org, repo, "identical.csv")
        for r in read_rows(ident) if os.path.isfile(ident) else []:
            today[r["path"]] = r["matched_blob"] if r["result"] in TODAY_MATCH \
                and r.get("matched_blob") else None
    sig = {}
    for r in out_rows:
        p, s = r["path"], set(blobs.get(r["path"], ()))
        if r.get("active_included", "").startswith("yes"):
            b = today.get(p)
            if not b:                       # today's content unknown: unique
                sig[p] = None
                continue
            s.add(b)
        sig[p] = frozenset(s) if s else None
    return sig


def txt_path(cfg, org, repo, path):
    return os.path.join(cfg.out, cfg.text_dir, org, repo, *clean_path(path)) + ".txt"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True,
                    help="bi_to_data.py / data_to_text.py output folder")
    ap.add_argument("--in", dest="src", required=True,
                    help="the extraction it was made from (extract_versions.py "
                         "--per-repo or archive_unpack.py output)")
    ap.add_argument("--state", help="extension_versions.py output (today's files)")
    ap.add_argument("--text-dir", default="data_text",
                    help="folder of the .txt under --out ('data_text' for "
                         "bi_to_data.py, '.' for data_to_text.py)")
    ap.add_argument("--scope", choices=("repo", "all"), default="repo")
    ap.add_argument("--apply", action="store_true",
                    help="move the duplicate .txt files out (default: dry run)")
    args = ap.parse_args()
    cfg = args
    st = os.path.join(args.out, STATE)
    repos = [(o, r) for o in sorted(os.listdir(st)) for r in sorted(os.listdir(os.path.join(st, o)))]
    kept_global, per_repo, tot = {}, [], Counter()
    removed_root = os.path.join(args.out, "_removed_duplicates")
    for org, repo in repos:
        kept = {} if args.scope == "repo" else kept_global
        sig = signatures(cfg, org, repo)
        rows, c = [], Counter()
        for path in sorted(sig):
            f = txt_path(cfg, org, repo, path)
            if not os.path.isfile(f):
                c["txt_missing"] += 1
                continue
            size = os.path.getsize(f)
            c["paths"] += 1
            c["bytes"] += size
            s = sig[path]
            if s is not None and s in kept:
                ko, kr, kp = kept[s]
                act = "moved" if args.apply else "would_move"
                if args.apply:
                    dest = os.path.join(removed_root, os.path.relpath(f, os.path.join(args.out, args.text_dir)))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(f, dest)
                rows.append([path, ko, kr, kp, size, act])
                c["duplicates"] += 1
                c["dup_bytes"] += size
                c["cross_repo"] += (ko, kr) != (org, repo)
            else:
                if s is not None:
                    kept[s] = (org, repo, path)
                c["kept"] += 1
                c["unique_today"] += s is None
        if rows or args.apply:
            write_atomic(os.path.join(st, org, repo, "same_as.csv"),
                         csv_text([SAME_AS_HEADER] + rows))
        if c["paths"]:
            per_repo.append([org, repo, c["paths"], c["kept"], c["duplicates"],
                             c["cross_repo"], round(c["bytes"] / 1e9, 3),
                             round(c["dup_bytes"] / 1e9, 3), c["unique_today"]])
        tot.update(c)
    per_repo.sort(key=lambda r: -r[7])
    name = "dedupe_%s" % args.scope
    with open(os.path.join(args.out, name + ".csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["org", "repo", "paths_with_txt", "kept", "duplicates",
                    "duplicates_of_other_repos", "gb", "gb_duplicates",
                    "kept_unique_today"])
        w.writerows(per_repo)
        w.writerow(["total", "", tot["paths"], tot["kept"], tot["duplicates"],
                    tot["cross_repo"], round(tot["bytes"] / 1e9, 3),
                    round(tot["dup_bytes"] / 1e9, 3), tot["unique_today"]])
    msg = ("scope %s, %s: %d paths with a .txt (%.1f GB) -> kept %d, duplicates %d "
           "(%.1f GB; %d of them copies of another repo's path); %d kept because "
           "today's file matches no history version%s" % (
               args.scope, "APPLIED - moved to " + removed_root if args.apply else "dry run",
               tot["paths"], tot["bytes"] / 1e9, tot["kept"], tot["duplicates"],
               tot["dup_bytes"] / 1e9, tot["cross_repo"], tot["unique_today"],
               "; %d .txt listed but not found" % tot["txt_missing"] if tot["txt_missing"] else ""))
    with open(os.path.join(args.out, name + ".md"), "w", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    print(msg)
    for r in per_repo[:10]:
        print("  %-60s %5d paths, %5d dup, %7.1f GB -> -%.1f GB" % (
            r[0] + "/" + r[1], r[2], r[4], r[6], r[7]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
