#!/usr/bin/env python3
"""
group4_2_path_list.py - the unique paths of some types found inside the
group 4.2 / 4.2_R archives, as group 4.1 was delivered: one CSV a team can
work from, a legend and a per-type summary. Nothing is copied: the files
stay on the server (stored_as says where).

Reads one or more archive_unpack.py output folders (--unpacked, repeatable,
e.g. group4_2 and group4_2_R): each finished repo's
_state/<org>/<repo>/manifest.csv (one row per file inside per archive
version). Only rows of files inside an archive of the --extensions types.

One row per (run, org, repo, path inside the repo = archive path + path
inside the archive), with every archive version that held it counted:

  <out>                         the list (columns in the legend)
  <out minus .csv>_legend.csv   what each column means
  <out minus .csv>_by_extension.csv   per run and type: repos, paths,
                                versions, distinct contents, left-out paths,
                                GB (each path at its largest); a total row

Paths in a vendored folder, macOS metadata or the DocFX cache are listed
too, with the reason in left_out (the other steps leave them out).

Usage:
    python3 group4_2_path_list.py \\
        --unpacked /data/workarea/group4_2/unpacked \\
        --unpacked /data/workarea/group4_2_R/unpacked \\
        --extensions group4_1_extensions.txt \\
        --out /data/workarea/group4_2/group4_1_paths/group4_1_inner_paths.csv
"""

import argparse
import csv
import os
import sys
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from group4_2_text import STATE, STORED, left_out_reason, list_repos  # noqa: E402
from repo_extension_summary import load_extensions              # noqa: E402

HEADER = ["run", "org", "repo", "archive_path", "inner_path", "ext",
          "versions", "contents", "not_stored", "first_date", "last_date",
          "latest_bytes", "max_bytes", "depth", "left_out", "stored_as"]
LEGEND = [
    ("run", "which unpack run: group4_2 (archives) or group4_2_R (packages)"),
    ("org, repo", "the repository"),
    ("archive_path", "the archive's path in the repo (an archive inside an "
                     "archive: outer path / inner archive path)"),
    ("inner_path", "the file's path inside the archive"),
    ("ext", "the type, lower case"),
    ("versions", "archive versions in the repo's history that held this "
                 "file"),
    ("contents", "different contents among those versions"),
    ("not_stored", "versions not stored by the unpack (error, too large, ...)"),
    ("first_date", "date of the first commit of an archive version holding "
                   "the file"),
    ("last_date", "date of the last such commit"),
    ("latest_bytes", "size of the file in the latest version, in bytes"),
    ("max_bytes", "the largest size it ever had, in bytes"),
    ("depth", "0 = directly in the archive, 1+ = in an archive inside it"),
    ("left_out", "vendored / macos / docfx_cache: a path the other steps "
                 "leave out (third-party, Mac metadata, DocFX build cache); "
                 "blank otherwise"),
    ("stored_as", "where the latest stored content is on the server, under "
                  "the run's unpacked folder (not sent)"),
]


def run_name(unpacked):
    """/data/workarea/group4_2_R/unpacked -> group4_2_R"""
    return os.path.basename(os.path.dirname(os.path.abspath(unpacked))) or unpacked


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def repo_paths(unpacked, org, repo, exts):
    """-> OrderedDict path -> [manifest rows], wanted types inside archives."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    by_path = OrderedDict()
    m = os.path.join(unpacked, STATE, org, repo, "manifest.csv")
    with open(m, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            if r.get("inner_path") and r["ext"] in exts:
                by_path.setdefault(r["path"], []).append(r)
    return by_path


def path_row(run, org, repo, rows):
    stored = [r for r in rows if r["action"] in STORED and r["stored_as"]]
    dates = sorted(d for r in rows for d in (r["first_date"], r["last_date"]) if d)
    sizes = [to_int(r["bytes"]) for r in rows]
    last = rows[-1]
    return [run, org, repo, last["archive_path"], last["inner_path"],
            last["ext"], len(rows),
            len({r["blob"] or r["stored_as"] for r in stored}),
            len(rows) - len(stored),
            dates[0] if dates else "", dates[-1] if dates else "",
            sizes[-1] if sizes else 0, max(sizes) if sizes else 0,
            last.get("depth", ""), left_out_reason(last["path"]),
            stored[-1]["stored_as"] if stored else ""]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unpacked", action="append", required=True,
                    help="archive_unpack.py --out folder (repeatable)")
    ap.add_argument("--extensions", required=True,
                    help="types: comma list or a file, one per line")
    ap.add_argument("--out", required=True, help="CSV to write")
    args = ap.parse_args()

    exts = set(load_extensions(args.extensions))
    base = args.out[:-4] if args.out.endswith(".csv") else args.out
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    summ, all_repos = {}, set()
    n = 0
    tmp = args.out + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for un in args.unpacked:
            run = run_name(un)
            repos = list_repos(un)
            print("%s: %d repos" % (run, len(repos)), flush=True)
            for org, repo in sorted(repos):
                for path, rows in repo_paths(un, org, repo, exts).items():
                    row = path_row(run, org, repo, rows)
                    w.writerow(row)
                    n += 1
                    c = summ.setdefault((run, row[5]), Counter())
                    c["paths"] += 1
                    c["versions"] += row[6]
                    c["contents"] += row[7]
                    c["not_stored"] += row[8]
                    c["bytes"] += row[12]
                    c["left_out_paths"] += bool(row[14])
                    c.setdefault("repos", set()).add((org, repo))
                    all_repos.add((run, org, repo))
    os.replace(tmp, args.out)

    with open(base + "_legend.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["column", "meaning"])
        w.writerows(LEGEND)
    cols = ["repos", "paths", "versions", "contents", "not_stored",
            "left_out_paths", "gb"]
    with open(base + "_by_extension.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "ext"] + cols)
        tot = Counter()
        for (run, e), c in sorted(summ.items(),
                                  key=lambda kv: (kv[0][0], -kv[1]["versions"])):
            vals = [len(c["repos"])] + [c[k] for k in cols[1:-1]] \
                + [round(c["bytes"] / 1e9, 3)]
            w.writerow([run, e] + vals)
            tot.update({k: v for k, v in c.items() if k != "repos"})
        w.writerow(["total", ""] + [len(all_repos)]
                   + [tot[k] for k in cols[1:-1]] + [round(tot["bytes"] / 1e9, 3)])
    print("%d paths -> %s (+ _legend.csv, _by_extension.csv)" % (n, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
