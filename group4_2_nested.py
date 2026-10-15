#!/usr/bin/env python3
"""
group4_2_nested.py - what the first archive_unpack.py run left closed inside
the 4.2 / 4.2_R archives, and what a recursive redo changed.

  find      in one archive_unpack.py output (--unpacked): the files inside
            an archive that were kept whole although they can be opened
            now: a type of --nested-extensions that is not an archive type
            (msapp, sppkg, vsix... = the 4.2_R packages), or an archive at
            depth >= --old-depth (left closed: too deep). -> in --out:
              todo.csv        the repos, as a batch for archive_unpack.py,
                              biggest manifest first
              todo_files.csv  the files (org, repo, path, ext, depth, ...)
              todo_by_extension.csv
  compare   per repo, the manifests saved before the redo (--before) against
            the new ones: files inside, stored, written, opened nested,
            deepest level, still closed (same test: before at --old-depth,
            after at --new-depth = the redo's --max-depth), archives too
            deep -> --out/compare.csv + totals

Usage:
    python3 group4_2_nested.py find --unpacked /data/workarea/group4_2/unpacked \\
        --nested-extensions zip,...,msapp,... --old-depth 3 \\
        --out /data/workarea/group4_2/nested
    python3 group4_2_nested.py compare --unpacked /data/workarea/group4_2/unpacked \\
        --before /data/workarea/group4_2/nested/before \\
        --nested-extensions ... --old-depth 3 --new-depth 20 --out /data/workarea/group4_2/nested
"""

import argparse
import csv
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from archive_unpack import ARCHIVE_EXTS, STATE, STORED          # noqa: E402
from group4_2_text import list_repos                            # noqa: E402
from repo_extension_summary import load_extensions              # noqa: E402

FILES_HEADER = ["org", "repo", "path", "ext", "depth", "action", "bytes",
                "archive_path", "stored_as"]


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def read_manifest(path):
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with open(path, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        return list(csv.DictReader(fh))


def closed(r, nested, depth):
    """A file inside an archive kept whole that could be opened."""
    e = r["ext"]
    return (r["action"] in STORED and bool(r.get("inner_path"))
            and e in nested
            and (e not in ARCHIVE_EXTS or to_int(r.get("depth")) >= depth))


def repo_counts(sd, nested, depth):
    m = os.path.join(sd, "manifest.csv")
    if not os.path.isfile(m):
        return None
    c = Counter()
    for r in read_manifest(m):
        if not r.get("inner_path"):
            continue
        c["files_inside"] += 1
        c["stored"] += r["action"] in STORED
        c["written"] += r["action"] == "written"
        c["opened_nested"] += r["action"] == "opened_nested"
        c["max_depth"] = max(c["max_depth"], to_int(r.get("depth")))
        c["still_closed"] += closed(r, nested, depth)
    a = os.path.join(sd, "archives.csv")
    if os.path.isfile(a):
        c["too_deep"] = sum(r["status"] == "too_deep" for r in read_manifest(a))
    return c


def find(args, nested):
    os.makedirs(args.out, exist_ok=True)
    st = os.path.join(args.unpacked, STATE)
    repos, by_ext = [], {}
    with open(os.path.join(args.out, "todo_files.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(FILES_HEADER)
        for org, repo in list_repos(args.unpacked):
            m = os.path.join(st, org, repo, "manifest.csv")
            n = 0
            for r in read_manifest(m):
                if closed(r, nested, args.old_depth):
                    n += 1
                    w.writerow([org, repo, r["path"], r["ext"], r["depth"],
                                r["action"], r["bytes"], r["archive_path"],
                                r["stored_as"]])
                    c = by_ext.setdefault(r["ext"], Counter())
                    c["files"] += 1
                    c["written"] += r["action"] == "written"
                    c["bytes"] += to_int(r["bytes"])
                    c.setdefault("repos", set()).add((org, repo))
            if n:
                repos.append((org, repo, n))
    with open(os.path.join(args.out, "todo.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "tier", "weight", "listed_files"])
        for org, repo, n in repos:          # list_repos: biggest first
            w.writerow([org, repo, "nested", 1, n])
    with open(os.path.join(args.out, "todo_by_extension.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "repos", "files", "distinct", "gb"])
        for e, c in sorted(by_ext.items(), key=lambda kv: -kv[1]["files"]):
            w.writerow([e, len(c["repos"]), c["files"], c["written"],
                        round(c["bytes"] / 1e9, 3)])
    print("%s: %d repo(s) to redo, %d closed file(s) inside -> %s/todo.csv"
          % (args.unpacked, len(repos), sum(n for *_, n in repos), args.out))
    for e, c in sorted(by_ext.items(), key=lambda kv: -kv[1]["files"])[:25]:
        print("  %-14s %6d file(s) in %d repo(s)" % (e, c["files"], len(c["repos"])))
    return 0


COLS = ["files_inside", "stored", "written", "opened_nested", "max_depth",
        "still_closed", "too_deep"]


def compare(args, nested):
    todo = os.path.join(args.out, "todo.csv")
    with open(todo, newline="", encoding="utf-8") as fh:
        repos = [(r["org"], r["repo"]) for r in csv.DictReader(fh)]
    tot_b, tot_a, missing = Counter(), Counter(), []
    with open(os.path.join(args.out, "compare.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "status"] + ["before_" + c for c in COLS]
                   + ["after_" + c for c in COLS])
        for org, repo in repos:
            b = repo_counts(os.path.join(args.before, org, repo), nested,
                            args.old_depth) or Counter()
            sd = os.path.join(args.unpacked, STATE, org, repo)
            a = (repo_counts(sd, nested, args.new_depth)
                 if os.path.isfile(os.path.join(sd, "done.json")) else None)
            if a is None:
                missing.append("%s/%s" % (org, repo))
                a = Counter()
            for t, c in ((tot_b, b), (tot_a, a)):
                for k in COLS:
                    t[k] = max(t[k], c[k]) if k == "max_depth" else t[k] + c[k]
            w.writerow([org, repo, "not_done" if not a else "done"]
                       + [b[c] for c in COLS] + [a[c] for c in COLS])
    print("%s: %d repo(s), %d not redone yet" % (args.unpacked, len(repos),
                                                  len(missing)))
    print("  %-14s %12s %12s" % ("", "before", "after"))
    for k in COLS:
        print("  %-14s %12s %12s" % (k, f"{tot_b[k]:,}", f"{tot_a[k]:,}"))
    print("  -> %s/compare.csv" % args.out)
    return 1 if missing else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["find", "compare"])
    ap.add_argument("--unpacked", required=True, help="archive_unpack.py --out")
    ap.add_argument("--nested-extensions", required=True,
                    help="types opened inside: comma list or a file")
    ap.add_argument("--old-depth", type=int, default=3,
                    help="archives at this depth or deeper count as closed")
    ap.add_argument("--new-depth", type=int, default=20,
                    help="compare: the redo's --max-depth")
    ap.add_argument("--before", help="compare: the saved _state copies")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    nested = set(load_extensions(args.nested_extensions))
    if args.action == "find":
        return find(args, nested)
    if not args.before:
        ap.error("compare needs --before")
    return compare(args, nested)


if __name__ == "__main__":
    sys.exit(main())
