#!/usr/bin/env python3
"""
fill_at_head.py - fill the at_head column of file_added_lines.py manifests
from the checked-out copy of each repo, instead of git's HEAD.

The archive repos the extraction read are git data only (no usable HEAD), so
every manifest row says at_head = "". What exists "today" is the checked-out
copy the inventory was taken from - the blob container's github root. Each
manifest row becomes:

  at_head = yes   the path exists in that checked-out copy
  at_head = no    it does not: deleted, renamed away or branch-only

Two sources for the checked-out file list (use one):

  --inventory CSV   the inventory of that copy: file_summary_github.csv (org,
                    repo, matched_path) or any list with org, repo and
                    relpath (+ filename). Fast - no access to the mount
  --disk-root DIR   the mounted folder holding <org>/<repo>/...; each repo's
                    folder is walked once (.git skipped). Always current, but
                    slow on a network mount

Manifests are rewritten atomically, one repo at a time, and only the at_head
column changes. --workers lists that many repos at once: listing a network
mount is waiting, not work, so parallel listings overlap the waits. Repos with no checked-out copy at all (not in the inventory,
or no folder under --disk-root) are left as "" and counted, so "no" always
means "the repo is there but this file is not".

Usage:
    python3 fill_at_head.py /data/workarea/full_extract \\
        --inventory /data/workarea/file_history_out_4/file_summary_github.csv
    python3 fill_at_head.py /data/workarea/full_extract \\
        --disk-root /home/ganeshk/blobcontainer/EYGCO_29062026/github
"""

import argparse
import csv
import io
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from explore_input_csv import detect_delimiter
from file_history_for_list import full_path


def inventory_paths(path):
    """{(org, repo): set of repo-relative paths} from an inventory CSV."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    out = defaultdict(set)
    with open(path, newline="", encoding="utf-8-sig",
              errors="surrogateescape") as fh:
        reader = csv.reader(fh, delimiter=detect_delimiter(path))
        header = [h.strip().lower() for h in next(reader, [])]
        ix = {c: i for i, c in enumerate(header)}
        if not {"org", "repo"} <= set(ix) or not (
                "matched_path" in ix or "relpath" in ix):
            sys.exit("need org, repo and matched_path or relpath; found %s"
                     % header)
        width = len(header)
        for row in reader:
            if len(row) < width:
                row = row + [""] * (width - len(row))
            org, repo = row[ix["org"]].strip(), row[ix["repo"]].strip()
            if "matched_path" in ix and row[ix["matched_path"]]:
                p = row[ix["matched_path"]]
            else:
                p = full_path(row[ix["relpath"]] if "relpath" in ix else "",
                              row[ix["filename"]] if "filename" in ix else "",
                              org, repo)
            if org and repo and p:
                out[(org, repo)].add(p)
    return out


def disk_paths(root, org, repo):
    """Set of repo-relative paths under <root>/<org>/<repo>, or None if the
    folder does not exist."""
    base = os.path.join(root, org, repo)
    if not os.path.isdir(base):
        return None
    out = set()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel = os.path.relpath(dirpath, base)
        rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        out.update(rel + f for f in filenames)
    return out


def update_repo(mp, present, dry_run):
    """Set at_head in one manifest. -> Counter of at_head over written
    files. present=None: the repo has no checked-out copy."""
    counts = Counter()
    with open(mp, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return counts
    hi = rows[0].index("at_head")
    oi = rows[0].index("output")
    for r in rows[1:]:
        r[hi] = "" if present is None else ("yes" if r[2] in present else "no")
        if r[oi]:
            counts[r[hi] or "no checked-out copy"] += 1
    if not dry_run:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape",
                  newline="") as fh:
            fh.write(buf.getvalue())
        os.replace(tmp, mp)
    return counts


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="the --out folder of file_added_lines.py")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--inventory", help="inventory CSV of the checked-out copy")
    src.add_argument("--disk-root", help="mounted folder holding <org>/<repo>")
    ap.add_argument("--workers", type=int, default=16,
                    help="repos handled at once (default 16); with "
                         "--disk-root the listings of a network mount "
                         "overlap, which is where the speed comes from")
    ap.add_argument("--dry-run", action="store_true",
                    help="count only, do not rewrite manifests")
    args = ap.parse_args()

    state = os.path.join(args.out_dir, "_state")
    if not os.path.isdir(state):
        sys.exit("no _state folder under " + args.out_dir)
    t0 = time.time()
    inv = None
    if args.inventory:
        inv = inventory_paths(args.inventory)
        print("inventory %s repo(s), %s path(s), read in %.0fs"
              % (f"{len(inv):,}", f"{sum(map(len, inv.values())):,}",
                 time.time() - t0), flush=True)

    todo = []
    for org in sorted(os.listdir(state)):
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            mp = os.path.join(od, repo, "manifest.csv")
            if os.path.isfile(mp):
                todo.append((org, repo, mp))
    print("repos     %s to update, %d worker(s)%s"
          % (f"{len(todo):,}", args.workers,
             " - dry run" if args.dry_run else ""), flush=True)

    def one(item):
        org, repo, mp = item
        present = (inv.get((org, repo)) if inv is not None
                   else disk_paths(args.disk_root, org, repo))
        return present is None, update_repo(mp, present, args.dry_run)

    counts = Counter()
    done = no_copy = errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(one, it): it for it in todo}
        for fut in as_completed(futs):
            org, repo, _mp = futs[fut]
            try:
                missing, c = fut.result()
            except Exception as exc:              # noqa: BLE001 - one repo
                errors += 1
                print("ERROR %s/%s: %s: %s" % (org, repo, type(exc).__name__,
                                               exc), file=sys.stderr, flush=True)
                continue
            no_copy += missing
            counts.update(c)
            done += 1
            if done % 500 == 0:
                el = time.time() - t0
                print("  %s / %s repos  %.0fs  (~%.0f min left)"
                      % (f"{done:,}", f"{len(todo):,}", el,
                         el / done * (len(todo) - done) / 60),
                      file=sys.stderr, flush=True)

    written = sum(counts.values())
    print("repos %s (%s with no checked-out copy, %d error(s))%s"
          % (f"{done:,}", f"{no_copy:,}", errors,
             " - dry run, nothing rewritten" if args.dry_run else ""))
    print("written files by at_head:")
    for k, v in counts.most_common():
        print("  %-22s %12s  %5.1f%%" % (k, f"{v:,}",
                                        100.0 * v / written if written else 0))
    print("%.0fs" % (time.time() - t0))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
