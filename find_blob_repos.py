#!/usr/bin/env python3
"""Repo and path of whole files sent by blob id (group 2, 4.x binary, tabular,
mixed ...).

Whole files are stored once per content across all repos, named
files/<ext>/<ab>/<blob>.<ext>, so the repo is not in their path; the
delivery's manifest.csv (extract_versions / active_files) maps each stored
file back to every org / repo / path / commit it came from.

Input: any text with the file names in it - the scanner's list as pasted
(path, file name, md5), one blob id per line, ... Every 40 / 64 hex name
followed by .<ext> is looked up; an md5 on the same line is carried along.

    python3 find_blob_repos.py --names scanner_list.txt \
        --manifest /home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group2/binary/manifest.csv \
        --out blob_repos.csv

--manifest may be given several times (or a glob in quotes). Without it the
usual places on the share are searched (P1/*/binary, P1/*_tracking/...).
Output: one row per file x repo path (a content in 3 repos gives 3 rows),
CRLF; files not found are listed with org 'NOT FOUND'.
"""
import argparse
import csv
import glob
import os
import re
import sys
from collections import defaultdict

P1 = "/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1"
DEFAULT = ["{p1}/*/binary/manifest.csv", "{p1}/*/*/binary/manifest.csv",
           "{p1}/*_tracking/*/manifest.csv", "{p1}/*_tracking/*/*/manifest.csv",
           "{p1}/*_tracking/*/*/*/manifest.csv"]
NAME = re.compile(r"\b([0-9a-f]{40}|[0-9a-f]{64})\.([A-Za-z0-9_~+-]+)")
MD5 = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")
STORED = ("written", "deduped", "in_stored")
OUT = ["file", "md5", "org", "repo", "path", "action", "first_commit", "first_date",
       "last_commit", "last_date", "manifest"]
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


def wanted(fh):
    want = {}
    for line in fh:
        line = line.lower()
        for m in NAME.finditer(line):
            md5 = [x for x in MD5.findall(line) if x != m.group(1)[:32]]
            want[m.group(1)] = (m.group(0), md5[-1] if md5 else "")
    return want


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", required=True, help="text with the file names ('-' = stdin)")
    ap.add_argument("--manifest", action="append", default=[])
    ap.add_argument("--p1", default=P1)
    ap.add_argument("--out", default="blob_repos.csv")
    a = ap.parse_args()

    want = wanted(sys.stdin if a.names == "-" else open(a.names, encoding="utf-8", errors="replace"))
    if not want:
        sys.exit("no <blob>.<ext> names found in %s" % a.names)
    pats = a.manifest or [p.format(p1=a.p1) for p in DEFAULT]
    mans = sorted({m for p in pats for m in glob.glob(p)})
    print("%d file(s) to find, %d manifest(s) to search" % (len(want), len(mans)))

    hits = defaultdict(list)
    for m in mans:
        with open(m, newline="", encoding="utf-8", errors="surrogateescape") as fh:
            r = csv.DictReader(fh)
            if not r.fieldnames or "stored_as" not in r.fieldnames:
                continue
            n = 0
            for row in r:
                if row.get("action") not in STORED:
                    continue
                key = os.path.basename((row.get("stored_as") or "").replace("\\", "/")).split(".", 1)[0].lower()
                if key not in want and (row.get("blob") or "").lower() in want:
                    key = row["blob"].lower()
                if key in want:
                    hits[key].append([row.get(k, "") for k in ("org", "repo", "path", "action", "first_commit",
                                                               "first_date", "last_commit", "last_date")] + [m])
                    n += 1
            if n:
                print("  %5d row(s)  %s" % (n, m))

    with open(a.out, "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(OUT)
        for key, (name, md5) in want.items():
            rows = sorted({tuple(x) for x in hits.get(key, [])}) or [("NOT FOUND",) + ("",) * 8]
            for x in rows:
                w.writerow([name, md5] + list(x))
    found = sum(1 for k in want if hits.get(k))
    print("found %d of %d; %d repo path row(s) -> %s" % (found, len(want), sum(len(set(map(tuple, v))) for v in hits.values()), a.out))
    for k, (name, _) in want.items():
        paths = sorted({(x[0], x[1], x[2]) for x in hits.get(k, [])})
        print("  %s" % name)
        for o, r, p in paths[:5] or [("NOT FOUND", "", "")]:
            print("      %s/%s  %s" % (o, r, p))
        if len(paths) > 5:
            print("      ... %d more" % (len(paths) - 5))


if __name__ == "__main__":
    main()
