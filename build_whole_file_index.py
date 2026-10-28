#!/usr/bin/env python3
"""One file -> repo / path index for every whole file on the share that is
named by its content (git blob id), so the scanner team can look files up.

Whole files from extract_versions / active_files stores sit at
files/<ext>/<ab>/<blob>.<ext>, once per content across all repos - the repo
is not in their path. Each store's manifest.csv (stored_as column) maps every
stored file to all org / repo / path / commits.

Read from the LOCAL stores on this server (the sources of the share copies),
not by walking the share: each store's manifest.csv, and a file counts as sent
when it is in the store's files/ (what was moved to _not_sent is not). Its
share path = the store's share folder + files/<ext>/<ab>/<name>.

    nohup python3 build_whole_file_index.py --out /data/workarea/whole_file_index > /data/workarea/whole_file_index.log 2>&1 &

  --store LOCAL=SHARE   add a store (share folder relative to p1); repeatable
  --check-share         also check every indexed file is on the share (slow on CIFS)

Whole files whose share path already holds org/repo (inside-archive
<org>/<repo>/<path>, container images files/<type>/<org>/<repo>/<sha256>)
are not indexed - the repo is in their path.

Writes to --out (CRLF):
  whole_file_index.csv  share_file, file, org, repo, path, action,
                        first_commit, first_date, last_commit, last_date,
                        store - one row per file x repo path
  summary.csv           per store: manifest rows, files indexed, repo paths
  README.txt            how to use the index
"""
import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict

P1 = "/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1"
W = "/data/workarea"
# local store -> share folder (relative to p1) whose files/ it was copied to
STORES = [
    (W + "/binary_versions", "group2/binary"),
    (W + "/group4_7_9/group1/not_text/binary", "group4_7_9/group1_binary"),
    (W + "/group4_7_9/group1/not_text/active", "group4_7_9/group1_binary"),
    (W + "/group4_7_9/group2_binary", "group4_7_9/group2_binary"),
    (W + "/group4_7_9/group2_active", "group4_7_9/group2_binary"),
    (W + "/group4_7_9/tabular/tabular_binary", "group4_7_9/tabular_binary"),
    (W + "/group4_7_9/tabular/tabular_active", "group4_7_9/tabular_binary"),
    (W + "/group4_7_9/mixed/not_text/binary", "group4_7_9/mixed_binary"),
    (W + "/group4_7_9/mixed/not_text/active", "group4_7_9/mixed_binary"),
]
STORED = ("written", "deduped", "in_stored")
COLS = ["share_file", "file", "org", "repo", "path", "action", "first_commit",
        "first_date", "last_commit", "last_date", "store"]
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def under_files(stored):
    """stored_as (relative files/... or an absolute path) -> the part after files/."""
    s = stored.replace("\\", "/")
    if s.startswith("files/"):
        return s[6:]
    return s.split("/files/", 1)[1] if "/files/" in s else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p1", default=P1)
    ap.add_argument("--out", default=W + "/whole_file_index")
    ap.add_argument("--store", action="append", default=[], help="LOCAL=SHARE (share folder relative to p1)")
    ap.add_argument("--check-share", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    stores = STORES + [tuple(x.split("=", 1)) for x in a.store]

    rows_by_file = defaultdict(set)      # share_file -> {(org, repo, path, action, fc, fd, lc, ld, store)}
    summ = []
    for local, share in stores:
        man = os.path.join(local, "manifest.csv")
        if not os.path.isfile(man):
            say("  (no manifest, skipped: %s)" % man)
            summ.append([local, share, "no manifest", 0, 0, 0])
            continue
        n_rows = n_kept = 0
        files = set()
        with open(man, **KW) as fh:
            for r in csv.DictReader(fh):
                if r.get("action") not in STORED or not r.get("stored_as"):
                    continue
                n_rows += 1
                rel = under_files(r["stored_as"])
                st = r["stored_as"].replace("\\", "/")
                src = st if os.path.isabs(st) else os.path.join(local, st)
                if not rel or not os.path.isfile(src):
                    continue                     # moved to _not_sent / not stored
                if os.path.isabs(st) and not st.startswith(local.rstrip("/") + "/"):
                    # in_stored: the file sits in another store; find its share folder
                    owner = [sh for lo, sh in stores if st.startswith(lo.rstrip("/") + "/")]
                    sh = owner[0] if owner else share
                else:
                    sh = share
                sf = "%s/files/%s" % (sh, rel)
                files.add(sf)
                n_kept += 1
                rows_by_file[sf].add((r.get("org", ""), r.get("repo", ""), r.get("path", ""), r["action"],
                                      r.get("first_commit", ""), r.get("first_date", ""),
                                      r.get("last_commit", ""), r.get("last_date", ""), local))
        say("  %-50s -> %-28s %8d stored rows, %8d kept, %7d files" % (local, share, n_rows, n_kept, len(files)))
        summ.append([local, share, "ok", n_rows, len(files), n_kept])

    missing = []
    if a.check_share:
        say("checking %d files on the share" % len(rows_by_file))
        missing = [f for f in rows_by_file if not os.path.isfile(os.path.join(a.p1, f))]
        say("  not on the share: %d" % len(missing))

    n_rows = 0
    with open(os.path.join(a.out, "whole_file_index.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(COLS)
        for sf in sorted(rows_by_file):
            for x in sorted(rows_by_file[sf]):
                w.writerow([sf, os.path.basename(sf)] + list(x))
                n_rows += 1
    if missing:
        with open(os.path.join(a.out, "not_on_share.csv"), "w", **KW) as fh:
            w = csv.writer(fh, lineterminator="\r\n")
            w.writerow(["share_file"])
            w.writerows([m] for m in sorted(missing))

    with open(os.path.join(a.out, "summary.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["local_store", "share_folder", "status", "stored_rows", "files_indexed", "repo_path_rows"])
        w.writerows(summ)
        w.writerow(["total", "", "", sum(x[3] for x in summ), len(rows_by_file), n_rows])
    say("index: %d files, %d repo path rows%s -> %s" % (len(rows_by_file), n_rows,
        (", %d not on the share" % len(missing)) if a.check_share else "", a.out))

    with open(os.path.join(a.out, "README.txt"), "w", newline="\r\n") as fh:
        fh.write(README)
    say("done -> %s" % a.out)


README = """Whole files named by their content - which repo and path they came from
=========================================================================

Whole files (Group 2, 4.7+4.9 group 1 / group 2 binaries, tabular, mixed) are
stored once per distinct content across all repos and named by their git blob
id:   files/<ext>/<first 2 characters>/<blob id>.<ext>
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
  store         the run that produced it

A file found in 3 repos has 3 rows. Look up: search the 'file' column for the
name, e.g.  grep 0314f25299898dbd1c931f2da3305df65ce26d46 whole_file_index.csv

Whole files whose share path already holds org/repo (inside-archive folders,
container images) are not listed - the repo is in their path.
summary.csv: counts per store.
"""


if __name__ == "__main__":
    main()
