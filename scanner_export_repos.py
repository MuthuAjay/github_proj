#!/usr/bin/env python3
"""Repo and path for every record of a scanner export (the .xls it gives is an
HTML table: Control Number, Group Identifier, GlobalOwners, Date Parent
Primary, Filename, Path, MD5 Hash, ...).

Whole files are named by their git blob id (files/<ext>/<ab>/<blob>.<ext>), so
the repo is not in their path. A record is the blob file itself (Filename =
<blob>.<ext>) or something the scanner unpacked from inside one - an email
attachment, an Office embedding - whose Path holds the blob
(...\\files\\eml\\35\\<blob>.eml\\attachment.pdf). Either way the FIRST
<blob>.<ext> in Filename or Path is looked up in the --manifest files
(stored_as column) for every org / repo / path it came from.

    python3 scanner_export_repos.py ~/export_20261028_163128.xls \
        --manifest /data/workarea/binary_versions/manifest.csv \
        --out ~/export_repos.csv

--manifest may be given several times (other whole-file stores).
Output (CRLF), one row per scanner record, in the export's order:
  the export's columns, then
  delivery    the share folder the scanner read (group2, group4_7_9_group2_binary_files, ...)
  kind        whole file / inside a whole file / group 4.5 text (G45_ name) /
              other (not a whole-file delivery)
  blob_file   the <blob>.<ext> looked up
  found       yes / NOT FOUND / - (not looked up)
  repos       distinct org/repo, ' | ' between them
  repo_paths  org/repo/path for each, ' | ' between them (cut at Excel's
              32,767 characters: '... N more')
  n_paths     how many repo paths
and --out with _paths.csv: one row per record x repo path (control number,
blob_file, org, repo, path, action, first/last commit and date).
"""
import argparse
import csv
import html
import os
import re
import sys
from collections import Counter, defaultdict

BLOB = re.compile(r"(?<![0-9a-f])([0-9a-f]{40}|[0-9a-f]{64})\.([A-Za-z0-9_~+-]+)")
STORED = ("written", "deduped", "in_stored")
ROW = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
DELIV = re.compile(r"diff_analysis_p1_(.+?)_[0-9a-f]{16}(?:\\|$)")
CELL_MAX = 32000
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")


def read_export(p):
    raw = open(p, "rb").read()
    if raw[:2] == b"PK":
        sys.exit("%s is a real .xlsx - save it as CSV first" % p)
    text = raw.decode("utf-8", "replace")
    rows = [[html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in CELL.findall(r)]
            for r in ROW.findall(text)]
    if not rows:
        sys.exit("no table rows in %s" % p)
    return rows[0], rows[1:]


def delivery(path):
    """share folder the scanner read, from its mount name in Path:
    ..._diff_analysis_p1_group2_079d0fcaa8da1e63\\ -> group2"""
    m = DELIV.search(path)
    return m.group(1) if m else ""


def kind_of(fn, path):
    m = BLOB.search(fn.lower())
    if m:
        return "whole file", m.group(0)
    m = BLOB.search(path.lower())
    if m:
        return "inside a whole file", m.group(0)
    if fn.startswith("G45_") or "group5_group4_5" in path:
        return "group 4.5 text (G45_ name)", ""
    return "other (not a whole-file delivery)", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("--manifest", action="append", required=True)
    ap.add_argument("--out", default="export_repos.csv")
    a = ap.parse_args()

    head, recs = read_export(a.export)
    col = {h: i for i, h in enumerate(head)}
    fi, pi = col.get("Filename"), col.get("Path")
    if fi is None or pi is None:
        sys.exit("no Filename / Path column in %s: %s" % (a.export, head))
    look = []
    for r in recs:
        r += [""] * (len(head) - len(r))
        look.append(kind_of(r[fi], r[pi]))
    want = {b.split(".", 1)[0] for _, b in look if b}
    print("%d records, %d distinct whole files to look up" % (len(recs), len(want)))

    hits = defaultdict(set)
    for m in a.manifest:
        n = 0
        with open(m, **KW) as fh:
            for row in csv.DictReader(fh):
                if row.get("action") not in STORED:
                    continue
                key = os.path.basename((row.get("stored_as") or "").replace("\\", "/")).split(".", 1)[0].lower()
                if key not in want:
                    key = (row.get("blob") or "").lower()
                    if key not in want:
                        continue
                hits[key].add(tuple(row.get(k, "") for k in ("org", "repo", "path", "action", "first_commit",
                                                            "first_date", "last_commit", "last_date")))
                n += 1
        print("  %8d row(s) found in %s" % (n, m))

    base = os.path.splitext(a.out)[0]
    kinds, found = Counter(), Counter()
    cn = col.get("Control Number", 0)
    with open(a.out, "w", **KW) as fo, open(base + "_paths.csv", "w", **KW) as fp:
        wo = csv.writer(fo, lineterminator="\r\n")
        wp = csv.writer(fp, lineterminator="\r\n")
        wo.writerow(head + ["delivery", "kind", "blob_file", "found", "repos", "repo_paths", "n_paths"])
        wp.writerow(["control_number", "blob_file", "org", "repo", "path", "action",
                     "first_commit", "first_date", "last_commit", "last_date"])
        for r, (k, b) in zip(recs, look):
            kinds[k] += 1
            h = sorted(hits.get(b.split(".", 1)[0], ())) if b else []
            f = "-" if not b else ("yes" if h else "NOT FOUND")
            found[(k, f)] += 1
            repos = " | ".join(sorted({"%s/%s" % (x[0], x[1]) for x in h}))
            paths, used = [], 0
            for i, x in enumerate(h):
                s = "%s/%s/%s" % (x[0], x[1], x[2])
                if used + len(s) > CELL_MAX:
                    paths.append("... %d more" % (len(h) - i))
                    break
                paths.append(s)
                used += len(s) + 3
            wo.writerow(r[:len(head)] + [delivery(r[pi]), k, b, f, repos[:CELL_MAX], " | ".join(paths), len(h)])
            for x in h:
                wp.writerow([r[cn], b] + list(x))
    for k, n in kinds.most_common():
        print("  %-36s %7d  %s" % (k, n, "  ".join("%s %d" % (f, c) for (kk, f), c in sorted(found.items()) if kk == k)))
    print("-> %s, %s_paths.csv" % (a.out, base))


if __name__ == "__main__":
    main()
