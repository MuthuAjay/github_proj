#!/usr/bin/env python3
"""One file -> repo / path index for every whole file on the share that is
named by its content (git blob id), so the scanner team can look files up.

Whole files from extract_versions / active_files stores sit at
files/<ext>/<ab>/<blob>.<ext> (ab = first 2 characters of the name), once per
content across all repos - the repo is not in their path. Their manifest.csv
(stored_as column) maps each one to every org / repo / path / commit.
Whole files whose share path already holds the repo (inside-archive
<org>/<repo>/<path>, container images files/<type>/<org>/<repo>/<sha256>)
are counted but not indexed.

One walk of the share (skipping _state, _logs, _not_sent,
_removed_duplicates): content-named files outside *_tracking, and every
manifest.csv with a stored_as column. Read only on the share.

    nohup python3 build_whole_file_index.py --out /data/workarea/whole_file_index > /data/workarea/whole_file_index.log 2>&1 &

Writes to --out (CRLF):
  whole_file_index.csv  share_file, file, org, repo, path, action,
                        first_commit, first_date, last_commit, last_date,
                        manifest - one row per file x repo path
                        (a content in 3 repos = 3 rows)
  not_in_manifest.csv   content-named files no manifest names (should be empty)
  summary.csv           per delivery folder: files, indexed, repo paths,
                        not in manifest; files with the repo in their path
  README.txt            how to use the index
"""
import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict

P1 = "/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1"
SKIP_DIRS = {"_state", "_logs", "_not_sent", "_removed_duplicates", "_tmp"}
HASHNAME = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})\.[^/]+$")
STORED = ("written", "deduped", "in_stored")
COLS = ["share_file", "file", "org", "repo", "path", "action", "first_commit",
        "first_date", "last_commit", "last_date", "manifest"]
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def delivery(rel):
    """p1-relative path -> its delivery folder (first 2 levels, or 3 under group4_2)."""
    parts = rel.split("/")
    n = 3 if len(parts) > 3 and parts[1] in ("group4_2", "group4_2_R") else 2
    return "/".join(parts[:n])


def walk(p1):
    files = defaultdict(list)            # name -> [p1-relative share paths]
    in_path = Counter()                  # delivery -> content-named files with the repo in their path
    manifests = []
    seen = 0
    for d, dirs, fs in os.walk(p1):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        rel_d = os.path.relpath(d, p1).replace(os.sep, "/")
        tracking = rel_d.split("/")[0].endswith("_tracking")
        for f in fs:
            seen += 1
            if seen % 200000 == 0:
                say("  walked %d files, %d content-named, %d manifests" % (seen, len(files), len(manifests)))
            if f == "manifest.csv":
                manifests.append(os.path.join(d, f))
            elif not tracking and HASHNAME.match(f):
                rel = rel_d + "/" + f
                if os.path.basename(d) == f[:2]:
                    files[f].append(rel)
                else:
                    in_path[delivery(rel)] += 1
    say("walk done: %d files, %d content-named (store layout), %d manifests" % (seen, len(files), len(manifests)))
    return files, in_path, manifests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p1", default=P1)
    ap.add_argument("--out", default="/data/workarea/whole_file_index")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    say("walking %s" % a.p1)
    files, in_path, manifests = walk(a.p1)

    hits = defaultdict(set)              # name -> {(org, repo, path, action, fc, fd, lc, ld, manifest)}
    used = 0
    for m in sorted(manifests):
        with open(m, **KW) as fh:
            r = csv.DictReader(fh)
            if not r.fieldnames or "stored_as" not in r.fieldnames:
                continue
            n = 0
            for row in r:
                if row.get("action") not in STORED:
                    continue
                name = os.path.basename((row.get("stored_as") or "").replace("\\", "/"))
                if name not in files:
                    continue
                hits[name].add((row.get("org", ""), row.get("repo", ""), row.get("path", ""), row["action"],
                                row.get("first_commit", ""), row.get("first_date", ""),
                                row.get("last_commit", ""), row.get("last_date", ""),
                                os.path.relpath(m, a.p1).replace(os.sep, "/")))
                n += 1
        if n:
            used += 1
            say("  %8d row(s)  %s" % (n, os.path.relpath(m, a.p1)))
    say("manifests with matches: %d" % used)

    per = defaultdict(Counter)
    with open(os.path.join(a.out, "whole_file_index.csv"), "w", **KW) as fi, \
            open(os.path.join(a.out, "not_in_manifest.csv"), "w", **KW) as fn:
        wi, wn = csv.writer(fi, lineterminator="\r\n"), csv.writer(fn, lineterminator="\r\n")
        wi.writerow(COLS)
        wn.writerow(["share_file", "file"])
        for name in sorted(files):
            rows = sorted(hits.get(name, ()))
            for rel in sorted(files[name]):
                c = per[delivery(rel)]
                c["files"] += 1
                if not rows:
                    c["not_in_manifest"] += 1
                    wn.writerow([rel, name])
                    continue
                c["indexed"] += 1
                c["repo_paths"] += len(rows)
                for x in rows:
                    wi.writerow([rel, name] + list(x))

    with open(os.path.join(a.out, "summary.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["delivery", "content_named_files", "indexed", "repo_path_rows", "not_in_manifest",
                    "files_with_repo_in_their_path"])
        tot = Counter()
        for dlv in sorted(set(per) | set(in_path)):
            c = per[dlv]
            row = [c["files"], c["indexed"], c["repo_paths"], c["not_in_manifest"], in_path[dlv]]
            w.writerow([dlv] + row)
            for k, v in zip(("f", "i", "r", "n", "p"), row):
                tot[k] += v
        w.writerow(["total", tot["f"], tot["i"], tot["r"], tot["n"], tot["p"]])
    say("index: %d files, %d indexed, %d repo path rows, %d not in a manifest; %d files have the repo in their path"
        % (tot["f"], tot["i"], tot["r"], tot["n"], tot["p"]))

    with open(os.path.join(a.out, "README.txt"), "w", newline="\r\n") as fh:
        fh.write("""Whole files named by their content - which repo and path they came from
=========================================================================

Whole files (Group 2, 4.7+4.9 group 2, tabular, mixed, ...) are stored once per
distinct content across all repos and named by their git blob id:
    files/<ext>/<first 2 characters>/<blob id>.<ext>
so the repo is not in their path. whole_file_index.csv gives, for each such
file on the share, every repo and path it was found in and the commits:

  share_file    the file's path under diff_analysis/p1
  file          its name (<blob id>.<ext>) - look files up by this
  org, repo     the repository
  path          the file's path in the repository
  action        written = first repo the content was taken from;
                deduped / in_stored = the same content in another path / repo
  first_commit, first_date, last_commit, last_date
                the commits where this content of the path first / last appears
  manifest      the delivery manifest the row comes from

A file found in 3 repos has 3 rows. Look up: search the 'file' column for the
name, e.g.  grep 0314f25299898dbd1c931f2da3305df65ce26d46 whole_file_index.csv

Whole files whose share path already holds org/repo (inside-archive folders,
container images) are not listed - the repo is in their path.
summary.csv: counts per delivery folder.
""")
    say("done -> %s" % a.out)


if __name__ == "__main__":
    main()
