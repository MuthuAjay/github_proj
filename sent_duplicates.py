#!/usr/bin/env python3
"""
sent_duplicates.py - how many of the files a line-level delta sends are
identical copies of each other, for one extension (or several).

A line-level delta (file_delta.py) deduplicates lines within each file, not
files across repos: a jquery.min.js committed in 500 repos is sent 500
times. This fingerprints (sha256) every file the delta wrote for the
extension and counts identical contents.

Reads <delta>/_state/<org>/<repo>/manifest.csv (the rows with an output)
and the files they name under <delta>/. Only reads.

Printed, and written to <out>.md:
  files sent, unique contents, duplicate files (sent - unique), bytes and
  bytes if each content were sent once; the same split for vendored
  folders (node_modules, packages, dist, ...) and the teams' own files;
  how many contents occur in 1, 2-10, 11-100, >100 files; the biggest
  groups of identical files.
<out> (CSV): one row per content sent more than once - sha256, bytes,
  copies, repos, vendored copies, an example path.

Usage:
    python3 sent_duplicates.py --delta /data/workarea/full_extract_delta_2 \\
        --ext js --out /data/workarea/js_sent_duplicates.csv
"""

import argparse
import csv
import datetime
import hashlib
import os
import sys
import time
from collections import Counter, defaultdict

from explain_file_counts import vendor_of
from repo_extension_summary import ext_key, load_extensions

CHUNK = 1 << 20


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.digest()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delta", required=True, help="file_delta.py output")
    ap.add_argument("--ext", required=True,
                    help="extension(s): js, or a comma list")
    ap.add_argument("--out", required=True, help="CSV of duplicated contents")
    ap.add_argument("--top", type=int, default=25,
                    help="groups shown in the .md (default 25)")
    args = ap.parse_args()
    exts = set(load_extensions(args.ext))
    state = os.path.join(args.delta, "_state")
    if not os.path.isdir(state):
        sys.exit("no _state folder under " + args.delta)

    groups = {}                 # sha -> [bytes, copies, vendored, repos set, example]
    sent = vend = missing = 0
    sent_bytes = 0
    t0, n = time.time(), 0
    for org in sorted(os.listdir(state)):
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            mp = os.path.join(od, repo, "manifest.csv")
            if not os.path.isfile(mp):
                continue
            n += 1
            if n % 500 == 0:
                print("  %s repos, %s files  %.0fs" % (f"{n:,}", f"{sent:,}",
                                                      time.time() - t0),
                      file=sys.stderr, flush=True)
            with open(mp, newline="", encoding="utf-8",
                      errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    if not r.get("output") or ext_key(r["path"]) not in exts:
                        continue
                    fp = os.path.join(args.delta, r["output"])
                    try:
                        size = os.path.getsize(fp)
                        d = sha256_of(fp)
                    except OSError:
                        missing += 1
                        continue
                    v = bool(vendor_of(r["path"]))
                    sent += 1
                    vend += v
                    sent_bytes += size
                    g = groups.get(d)
                    if g is None:
                        groups[d] = [size, 1, int(v), {(org, repo)},
                                     "%s/%s:%s" % (org, repo, r["path"])]
                    else:
                        g[1] += 1
                        g[2] += v
                        g[3].add((org, repo))

    unique = len(groups)
    uniq_bytes = sum(g[0] for g in groups.values())
    dup_groups = [(d, g) for d, g in groups.items() if g[1] > 1]
    # vendored / own split: a content counts as vendored if all its copies are
    v_sent = vend
    o_sent = sent - vend
    v_unique = sum(1 for g in groups.values() if g[2] == g[1])
    o_unique = unique - v_unique
    bands = Counter()
    for g in groups.values():
        c = g[1]
        bands["1 (no copies)" if c == 1 else "2-10" if c <= 10 else
              "11-100" if c <= 100 else ">100"] += 1

    with open(args.out, "w", newline="", encoding="utf-8",
              errors="backslashreplace") as fh:
        w = csv.writer(fh)
        w.writerow(["sha256", "bytes", "copies", "repos", "vendored_copies",
                    "example"])
        for d, g in sorted(dup_groups, key=lambda kv: -kv[1][1]):
            w.writerow([d.hex(), g[0], g[1], len(g[3]), g[2], g[4]])

    def f(v):
        return f"{v:,}"

    gb = 1e9
    md = ["# Duplicates among the files sent: %s" % ", ".join(sorted(exts)),
          "", "Generated %s by sent_duplicates.py from `%s`."
          % (datetime.date.today().isoformat(), args.delta), "",
          "| | files | GB |", "|---|---:|---:|",
          "| **files sent** | **%s** | %.2f |" % (f(sent), sent_bytes / gb),
          "| unique contents | %s | %.2f |" % (f(unique), uniq_bytes / gb),
          "| **duplicate files** (identical to another sent file) | **%s** "
          "| %.2f |" % (f(sent - unique), (sent_bytes - uniq_bytes) / gb),
          "", "| | files sent | unique contents |", "|---|---:|---:|",
          "| vendored folders (node_modules, packages, dist, ...) | %s | %s |"
          % (f(v_sent), f(v_unique)),
          "| teams' own files | %s | %s |" % (f(o_sent), f(o_unique)), "",
          "_A content counts as vendored when every copy of it is in a "
          "vendored folder._", "",
          "| copies of the same content | contents |", "|---|---:|"]
    md += ["| %s | %s |" % (k, f(bands[k]))
           for k in ("1 (no copies)", "2-10", "11-100", ">100")]
    md += ["", "## Biggest groups of identical files", "",
           "| copies | repos | bytes | vendored copies | example |",
           "|---:|---:|---:|---:|---|"]
    for d, g in sorted(dup_groups, key=lambda kv: -kv[1][1])[:args.top]:
        md.append("| %s | %s | %s | %s | %s |" % (
            f(g[1]), f(len(g[3])), f(g[0]), f(g[2]),
            g[4].replace("|", "/")[:100]))
    if missing:
        md += ["", "%s file(s) named in the manifests were not found on disk."
               % f(missing)]
    with open(args.out + ".md", "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")
    print("files sent %s, unique %s, duplicates %s (%.2f of %.2f GB) -> %s"
          % (f(sent), f(unique), f(sent - unique),
             (sent_bytes - uniq_bytes) / gb, sent_bytes / gb, args.out + ".md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
