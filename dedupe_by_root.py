#!/usr/bin/env python3
"""
dedupe_by_root.py - drop the files inventoried twice, once under each copy of
a repo, keeping ONE copy per repo: the one with the most files.

The inventory walked two roots holding the same repos -
AllRepos\\<org>\\<repo>\\... and github/<org>/<repo>/... - so most files are
listed twice. Per (org, repo), whichever root lists more files is the
population, and every row from the other root is dropped:

  * a file both roots list keeps the winning root's row only
  * a file ONLY the losing root lists is dropped too (it is not part of the
    population); --keep-unique keeps those rows instead. Either way their
    count is reported, per repo, so nothing disappears silently
  * a path listed twice within the winning root keeps its first row

The root is whatever relpath has before the <org>/<repo> folders ("AllRepos",
"github"); a relpath that does not contain them is its own root, "(none)".
A file's identity is its path inside the repo, from the same full_path() that
file_history_for_list.py matches with, so the two roots' rows line up
whatever their slashes.

Ties (both roots list the same number of files) go to the first root named
in --prefer (default github, whose paths also keep non-ASCII file names that
the AllRepos copy turned into underscores).

Works on any CSV/TSV with org, repo and relpath columns (filename optional):
the input list for file_history_for_list.py, or its file_summary.csv. Every
column of a kept row is written back unchanged.

Writes:
  <out>                  the kept rows
  <out>_roots.csv        per repo: files per root, the winner, rows kept,
                         dropped as duplicates, dropped / kept as
                         losing-root-only

Three streaming passes over the input (count, collect the winners' paths,
write); memory is one integer per winning file, not the rows.

Usage:
    python3 dedupe_by_root.py input.csv --out input_dedup.csv
    python3 dedupe_by_root.py file_summary.csv --out file_summary_dedup.csv \\
        --keep-unique
"""

import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict

from explore_input_csv import detect_delimiter
from file_history_for_list import full_path


def root_of(rel, org, repo):
    """The folders of relpath before <org>/<repo>: 'AllRepos', 'github'."""
    segs = (rel or "").strip().replace("\\", "/").lstrip("/").split("/")
    low = [x.lower() for x in segs]
    org, repo = org.lower(), repo.lower()
    for i in range(len(segs) - 1):
        if low[i] == org and low[i + 1] == repo:
            return "/".join(segs[:i]) or "(none)"
    return "(none)"


class Input:
    """Re-readable streaming view of the input: yields (row, org, repo,
    root, path) per data row. Opened once per pass."""

    def __init__(self, path):
        self.path = path
        self.delim = detect_delimiter(path)
        with self._open() as fh:
            self.header = next(csv.reader(fh, delimiter=self.delim), None)
        if not self.header:
            sys.exit("empty file: " + path)
        low = [h.strip().lower().lstrip("﻿") for h in self.header]
        missing = [c for c in ("org", "repo", "relpath") if c not in low]
        if missing:
            sys.exit("missing column(s) %s; found %s" % (missing, low))
        self.ix = {c: low.index(c) for c in ("org", "repo", "relpath")}
        self.fx = low.index("filename") if "filename" in low else None

    def _open(self):
        # surrogateescape: bytes that are not UTF-8 are written back as-is
        return open(self.path, newline="", encoding="utf-8-sig",
                    errors="surrogateescape")

    def rows(self, label, quiet):
        ix, fx, width = self.ix, self.fx, len(self.header)
        t0 = time.time()
        with self._open() as fh:
            reader = csv.reader(fh, delimiter=self.delim)
            next(reader)
            for n, row in enumerate(reader, 1):
                if not quiet and n % 1_000_000 == 0:
                    print("  %s: %s rows (%.0fs)" % (label, f"{n:,}",
                                                    time.time() - t0),
                          file=sys.stderr, flush=True)
                if len(row) < width:
                    row = row + [""] * (width - len(row))
                org, repo = row[ix["org"]].strip(), row[ix["repo"]].strip()
                rel = row[ix["relpath"]]
                fname = row[fx] if fx is not None else ""
                yield (row, org, repo, root_of(rel, org, repo),
                       full_path(rel, fname, org, repo))


def pick_winner(roots, prefer):
    """roots: Counter {root: files}. Most files wins; ties by --prefer order,
    then by name."""
    rank = {r.lower(): i for i, r in enumerate(prefer)}
    return min(roots, key=lambda r: (-roots[r], rank.get(r.lower(), len(rank)),
                                     r))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="input list or file_summary.csv")
    ap.add_argument("--out", help="output CSV (default: <input>_dedup.csv)")
    ap.add_argument("--keep-unique", action="store_true",
                    help="keep files that only the losing root lists "
                         "(default: drop them - the winning root is the "
                         "population)")
    ap.add_argument("--prefer", default="github,AllRepos",
                    help="tie-break order of roots (default github,AllRepos)")
    ap.add_argument("--quiet", action="store_true", help="no progress lines")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        sys.exit("not a file: " + args.csv_in)
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    stem, ext = os.path.splitext(args.csv_in)
    out = args.out or stem + "_dedup" + (ext or ".csv")
    report = os.path.splitext(out)[0] + "_roots.csv"
    prefer = [p.strip() for p in args.prefer.split(",") if p.strip()]
    src = Input(args.csv_in)
    t0 = time.time()

    # ---- pass 1: files per root, per repo --------------------------------
    per_repo = defaultdict(Counter)
    total = 0
    for _row, org, repo, root, _p in src.rows("pass 1/3 count", args.quiet):
        total += 1
        per_repo[(org, repo)][root] += 1
    winner = {k: pick_winner(v, prefer) for k, v in per_repo.items()}

    # ---- pass 2: the winning roots' paths ----------------------------------
    # a hash per file, not the tuple: millions of tuples cost gigabytes
    in_winner = set()
    for _row, org, repo, root, p in src.rows("pass 2/3 index", args.quiet):
        if root == winner[(org, repo)]:
            in_winner.add(hash((org, repo, p)))

    # ---- pass 3: write -----------------------------------------------------
    # per repo: kept, dup (loser row, winner has it), loser_only_dropped,
    #           loser_only_kept, same_root_dup
    stat = defaultdict(lambda: [0, 0, 0, 0, 0])
    written = set()
    with open(out, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh, delimiter=src.delim)
        w.writerow(src.header)
        for row, org, repo, root, p in src.rows("pass 3/3 write", args.quiet):
            s = stat[(org, repo)]
            key = hash((org, repo, p))
            if root != winner[(org, repo)]:
                if key in in_winner:
                    s[1] += 1
                    continue
                if not args.keep_unique:
                    s[2] += 1
                    continue
                s[3] += 1
            if key in written:
                s[4] += 1
                continue
            written.add(key)
            w.writerow(row)
            s[0] += 1
    in_winner.clear()
    written.clear()

    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "roots", "winner", "rows_in", "kept",
                    "dropped_duplicate", "dropped_loser_only",
                    "kept_loser_only", "dropped_same_root_duplicate"])
        for k in sorted(per_repo):
            roots = per_repo[k]
            w.writerow([k[0], k[1],
                        "|".join("%s:%d" % rc for rc in roots.most_common()),
                        winner[k], sum(roots.values())] + stat[k])

    tot = [sum(s[i] for s in stat.values()) for i in range(5)]
    two_roots = sum(1 for v in per_repo.values() if len(v) > 1)
    root_wins = Counter(winner[k] for k, v in per_repo.items() if len(v) > 1)
    print("rows in              %s  (%s repo(s), %s listed under 2+ roots)"
          % (f"{total:,}", f"{len(per_repo):,}", f"{two_roots:,}"))
    print("winning root         %s" % (", ".join(
        "%s %s" % (r, f"{n:,}") for r, n in root_wins.most_common()) or "-"))
    print("kept                 %s" % f"{tot[0]:,}")
    print("dropped duplicate    %s  (the winning root lists the same file)"
          % f"{tot[1]:,}")
    if args.keep_unique:
        print("kept loser-only      %s  (only the losing root lists them)"
              % f"{tot[3]:,}")
    else:
        print("dropped loser-only   %s  (only the losing root lists them; "
              "--keep-unique keeps them)" % f"{tot[2]:,}")
    print("dropped same-root    %s  (a path listed twice in one root)"
          % f"{tot[4]:,}")
    print("-> %s\n-> %s\n   %.1fs" % (out, report, time.time() - t0))


if __name__ == "__main__":
    main()
