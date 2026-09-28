#!/usr/bin/env python3
"""
check_missing_repos.py - double-check repositories recorded as missing
(excluded.csv from make_batches.py, repo_status = repo_missing) by looking
for them in every place a copy may live, under the exact name and under
similar names.

A repo was recorded as missing when <archive>/<org>/<repo> did not exist or
was not a git repository. That can also happen when:

  * the folder name differs slightly - letter case, "-" vs "_", spaces,
    dots (Repo.Name vs repo-name)
  * the folder exists but is not a usable repository (empty, or .git without
    objects)
  * the repository is stored in another location (the active copy, the
    inventory root, another container)

For each repo and each --place it reports what is there, and a verdict:

  FOUND        a git repository exists (exact or similar name) somewhere
  FOLDER ONLY  a folder exists but is not a git repository
  MISSING      nothing with that name, or a similar one, anywhere

Nothing is modified: folders are only listed.

Output:
  <out>.csv          org, repo, verdict, where (one row per repo)
  <out>_found.csv    org, repo, place, root, folder - the FOUND repos, ready
                     to be processed as an extra batch (root/org/folder is
                     the repository)

Usage:
    python3 check_missing_repos.py --excluded batches/excluded.csv \\
        --place archive=/data/workarea/archive \\
        --place active=/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --place github=/home/ganeshk/blobcontainer/EYGCO_29062026/github \\
        --out /data/workarea/missing_repos_check
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter

DEFAULT_PLACES = [
    "archive=/data/workarea/archive",
    "active=/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos",
    "github=/home/ganeshk/blobcontainer/EYGCO_29062026/github",
]


def norm(name):
    """Name for fuzzy matching: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def describe(path):
    """What is at path: a git repo (work tree or bare), a plain folder, or
    None when there is nothing."""
    try:
        if not os.path.isdir(path):
            return None
        for objects in (os.path.join(path, ".git", "objects"),
                        os.path.join(path, "objects")):
            if os.path.isdir(objects):
                pack = os.path.join(objects, "pack")
                n = sum(1 for f in os.listdir(pack) if f.endswith(".pack")) \
                    if os.path.isdir(pack) else 0
                loose = sum(1 for d in os.listdir(objects)
                            if len(d) == 2 and os.path.isdir(os.path.join(objects, d)))
                if n or loose:
                    return "git repo (%d pack file(s), %d loose object folder(s))" % (n, loose)
                return "NOT a usable git repo (objects folder is empty)"
        return "NOT a git repo (plain folder, %d entries)" % len(os.listdir(path))
    except OSError as exc:
        return "unreadable (%s)" % exc


class Listing:
    """Cached folder listings: one listdir per <root>/<org>."""

    def __init__(self):
        self.cache = {}

    def names(self, root, org):
        key = (root, org)
        if key not in self.cache:
            try:
                self.cache[key] = os.listdir(os.path.join(root, org))
            except OSError:
                self.cache[key] = []
        return self.cache[key]

    def orgs(self, root):
        key = (root, None)
        if key not in self.cache:
            try:
                self.cache[key] = [d for d in os.listdir(root)
                                   if os.path.isdir(os.path.join(root, d))]
            except OSError:
                self.cache[key] = []
        return self.cache[key]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--excluded", required=True,
                    help="excluded.csv from make_batches.py (org, repo, ...)")
    ap.add_argument("--place", action="append", metavar="LABEL=PATH",
                    help="a folder holding <org>/<repo> to search (repeatable; "
                         "default: archive, active copy and github root)")
    ap.add_argument("--any-org", action="store_true",
                    help="also look for the repo under every other org folder "
                         "(slower)")
    ap.add_argument("--out", default="missing_repos_check",
                    help="output path without extension (default "
                         "missing_repos_check)")
    args = ap.parse_args()

    places = []
    for spec in args.place or DEFAULT_PLACES:
        label, sep, path = spec.partition("=")
        if not sep:
            label, path = os.path.basename(spec.rstrip("/")) or spec, spec
        places.append((label, path))
    for label, path in places:
        state = "ok" if os.path.isdir(path) else "NOT FOUND / not mounted"
        print("place  %-10s %s  [%s]" % (label, path, state))
    if not any(os.path.isdir(p) for _l, p in places):
        sys.exit("none of the places exist - is the storage mounted?")

    with open(args.excluded, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("org") and r.get("repo")]
    print("checking %d repo(s) from %s" % (len(rows), args.excluded))

    ls = Listing()
    results, found_rows, verdicts = [], [], Counter()
    for r in rows:
        org, repo = r["org"].strip(), r["repo"].strip()
        where, found = [], False
        for label, root in places:
            orgs = [org] + ([o for o in ls.orgs(root) if o != org]
                            if args.any_org else [])
            for o in orgs:
                exact = describe(os.path.join(root, o, repo)) if o == org else None
                hits = [(repo, exact)] if exact else []
                hits += [(e, describe(os.path.join(root, o, e)))
                         for e in ls.names(root, o)
                         if norm(e) == norm(repo) and not (o == org and e == repo)]
                for name, what in hits:
                    tag = "exact name" if (name == repo and o == org) else \
                        "name '%s'%s" % (name, "" if o == org else " in org " + o)
                    where.append("%s: %s - %s" % (label, tag, what))
                    if what and what.startswith("git repo"):
                        found = True
                        found_rows.append([org, repo, label, root,
                                           os.path.join(o, name)])
        verdict = "FOUND" if found else ("FOLDER ONLY" if where else "MISSING")
        verdicts[verdict] += 1
        results.append([org, repo, verdict, " ; ".join(where)])

    with open(args.out + ".csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "verdict", "where"])
        w.writerows(results)
    with open(args.out + "_found.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "place", "root", "folder"])
        w.writerows(found_rows)

    print("\nresult: " + ", ".join("%s %d" % kv for kv in verdicts.most_common()))
    for org, repo, verdict, where in results:
        if verdict != "MISSING":
            print("  %-11s %s/%s -> %s" % (verdict, org, repo, where))
    print("\n-> %s.csv\n-> %s_found.csv" % (args.out, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
