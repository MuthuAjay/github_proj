#!/usr/bin/env python3
"""
delta_by_extension.py - per extension, what a line-level extraction held and
what its delta sends: files in history, files still in the active copy,
files that still had lines to send, and the lines.

For the extensions sent as LINES (group 1, and the text extensions of group
2): file_added_lines.py collected every line a file ever had, once per file,
and file_delta.py removed the lines still in today's file. This reads both
outputs' per-repo manifests:

  <extract>/_state/<org>/<repo>/manifest.csv    file_added_lines.py
  <delta>/_state/<org>/<repo>/manifest.csv      file_delta.py

Columns, per extension:
  repos                repos with at least one such file
  files_in_history     distinct paths the extraction saw in history
  files_at_head        ... still in the active copy
  files_history_only   ... not in it any more
  files_no_active_repo ... in repos with no active copy (at_head blank)
  versions             commits that touched them (file_added_lines.py's
                       "versions")
  files_with_text      files that had any text (not binary / empty)
  lines_in_history     distinct lines per file, summed (what was compared)
  lines_removed        lines still in today's file - already processed
  lines_sent           lines left: the delta
  files_sent           files with at least one line left (in the delta)
  files_nothing_left   files whose every line is still in today's file
  files_not_text       binary / empty / name too long - nothing to compare
  files_unreadable     today's file could not be read: whole history kept
  files_error          the delta failed for the file

and a total row. Lines are deduplicated WITHIN each file (every line once),
not across files - the unit of a line-level delta. Repos the extraction
failed on (done.json not ok) have no rows; they are counted separately.

Output: <out> (CSV), plus <out>.md with the same table and a legend.

Usage:
    python3 delta_by_extension.py --extract /data/workarea/full_extract \\
        --delta /data/workarea/full_extract_delta \\
        --out /data/workarea/group1_delta_by_extension.csv
    # the text extensions of group 2, after run_text_versions.sh
    python3 delta_by_extension.py --extract /data/workarea/text9_extract \\
        --delta /data/workarea/text9_extract_delta --extensions all \\
        --out /data/workarea/group2_text_delta_by_extension.csv
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time
from collections import Counter, defaultdict

from repo_extension_summary import EXTENSIONS, ext_key, load_extensions

COLS = ["repos", "files_in_history", "files_at_head", "files_history_only",
        "files_no_active_repo", "versions", "files_with_text",
        "lines_in_history", "lines_removed", "lines_sent", "files_sent",
        "files_nothing_left", "files_not_text", "files_unreadable",
        "files_error"]
LEGEND = [
    ("repos", "Repositories with at least one file of this type"),
    ("files_in_history", "Unique file paths the extraction saw in history"),
    ("files_at_head", "Of those, still in the active copy today"),
    ("files_history_only", "Of those, no longer there (deleted, renamed, "
                           "other branches)"),
    ("files_no_active_repo", "Of those, in repositories with no active copy"),
    ("versions", "Commits that touched these files"),
    ("files_with_text", "Files that had any text to compare"),
    ("lines_in_history", "Every line the files ever had, once per file"),
    ("lines_removed", "Lines still in today's file - already processed"),
    ("lines_sent", "Lines left to send: the delta"),
    ("files_sent", "Files with at least one line left (files in the delta)"),
    ("files_nothing_left", "Files whose every line is still in today's file"),
    ("files_not_text", "Binary, empty or name too long - nothing to compare"),
    ("files_unreadable", "Today's file could not be read: whole history sent"),
    ("files_error", "The delta failed for the file"),
]


def state_repos(root):
    state = os.path.join(root, "_state")
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            yield org, repo, os.path.join(od, repo)


def rows_of(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        return list(csv.DictReader(fh))


def num(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else 0


def build(extract, delta, exts):
    acc = defaultdict(Counter)
    repos_of = defaultdict(set)
    failed = Counter()
    t0, n = time.time(), 0
    for org, repo, sd in state_repos(extract):
        n += 1
        if n % 1000 == 0:
            print("  %s repos read  %.0fs" % (f"{n:,}", time.time() - t0),
                  file=sys.stderr, flush=True)
        try:
            with open(os.path.join(sd, "done.json"), encoding="utf-8") as fh:
                status = json.load(fh).get("status", "?")
        except (OSError, ValueError):
            status = "no done.json"
        if status != "ok":
            failed[status] += 1
            continue
        dl = {r["path"]: r for r in rows_of(os.path.join(
            delta, "_state", org, repo, "manifest.csv"))}
        for r in rows_of(os.path.join(sd, "manifest.csv")):
            p = r.get("path") or ""
            e = ext_key(p) or "(none)"
            if exts is not None and e not in exts:
                continue
            c = acc[e]
            repos_of[e].add((org, repo))
            c["files_in_history"] += 1
            c[{"yes": "files_at_head", "no": "files_history_only"}.get(
                r.get("at_head", ""), "files_no_active_repo")] += 1
            c["versions"] += num(r.get("versions"))
            if not r.get("output"):
                c["files_not_text"] += 1
                continue
            c["files_with_text"] += 1
            d = dl.get(p)
            if d is None:
                c["files_error"] += 1          # no delta row for it
                continue
            c["lines_in_history"] += num(d.get("lines_history"))
            c["lines_removed"] += num(d.get("lines_removed"))
            c["lines_sent"] += num(d.get("lines_kept"))
            action = d.get("action", "")
            if action == "error":
                c["files_error"] += 1
            elif action == "unreadable_current":
                c["files_unreadable"] += 1
                c["files_sent"] += 1
            elif d.get("output"):
                c["files_sent"] += 1
            else:
                c["files_nothing_left"] += 1
    for e in acc:
        acc[e]["repos"] = len(repos_of[e])
    all_repos = set().union(*repos_of.values()) if repos_of else set()
    return acc, failed, n, len(all_repos)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extract", required=True,
                    help="file_added_lines.py output")
    ap.add_argument("--delta", required=True, help="file_delta.py output")
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--extensions", default="group1",
                    help="'group1' (the 32 of the first extraction, default), "
                         "'all', or a comma list / file")
    args = ap.parse_args()
    for d in (args.extract, args.delta):
        if not os.path.isdir(os.path.join(d, "_state")):
            sys.exit("no _state folder under " + d)
    exts = (set(EXTENSIONS) if args.extensions == "group1" else
            None if args.extensions == "all" else
            set(load_extensions(args.extensions)))

    acc, failed, n, n_repos = build(args.extract, args.delta, exts)
    order = sorted(acc, key=lambda e: (-acc[e]["files_in_history"], e))
    rows = [[e] + [acc[e][c] for c in COLS] for e in order]
    total = ["total"] + [sum(r[i] for r in rows)
                         for i in range(1, len(COLS) + 1)]
    total[1] = n_repos               # repos with any of them, not a sum
    with open(args.out, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["ext"] + COLS)
        w.writerows(rows)
        w.writerow(total)
        w.writerow([])
        w.writerow(["column", "meaning"])
        w.writerows(LEGEND)

    ix = {c: i + 1 for i, c in enumerate(COLS)}
    show = ["files_in_history", "files_at_head", "files_history_only",
            "files_sent", "files_nothing_left", "lines_in_history",
            "lines_removed", "lines_sent"]

    def f(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    li, lr = total[ix["lines_in_history"]], total[ix["lines_removed"]]
    md = ["# Line-level delta by extension", "",
          "Generated %s by delta_by_extension.py from `%s` and `%s`."
          % (datetime.date.today().isoformat(), args.extract, args.delta), "",
          "Repositories read: %s. Repositories the extraction failed on (no "
          "rows, not in these numbers): %s." % (
              f"{n:,}", ", ".join("%s %d" % kv for kv in failed.most_common())
              or "none"), "",
          "| ext | " + " | ".join(show) + " |",
          "|---|" + "---:|" * len(show)]
    md += ["| %s | %s |" % (r[0], " | ".join(f(r[ix[c]]) for c in show))
           for r in rows + [total]]
    md += ["", "Lines removed as already processed: %s of %s (%.1f%%)."
           % (f(lr), f(li), 100.0 * lr / li if li else 0), "",
           "Lines are deduplicated within each file (every line once), not "
           "across files.", "", "| column | meaning |", "|---|---|"]
    md += ["| %s | %s |" % kv for kv in LEGEND]
    with open(args.out + ".md", "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")
    print("%d extension(s): %s files in history, %s sent, %s lines sent -> %s"
          % (len(rows), f(total[ix["files_in_history"]]),
             f(total[ix["files_sent"]]), f(total[ix["lines_sent"]]), args.out))
    if failed:
        print("repos the extraction failed on (not counted): %s"
              % ", ".join("%s %d" % kv for kv in failed.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
