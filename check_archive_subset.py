#!/usr/bin/env python3
"""
check_archive_subset.py - bulk-verify "archive is a subset of active" across
many repo pairs at once.

For every repo name present under ARCHIVE_ROOT, finds the matching repo
under ACTIVE_ROOT and, per side, unpacks the ENTIRE git object store - every
blob (file content), every tree (directory snapshot), and every commit,
reachable or dangling or pack-only - the same walk compare_repos.py's
--mode history/objects use. Archive is always side B, so `only_in_b` on
each of the three comparisons is exactly "exists in archive, doesn't exist
anywhere in active's history".

A repo PASSes only if all three (blobs, trees, commits) come back with
only_in_b == 0. That is a stronger claim than "the files look the same" -
it means nothing in archive's object store, at any level, is missing from
active's.

Usage:
    check_archive_subset.py ACTIVE_ROOT ARCHIVE_ROOT
    check_archive_subset.py ACTIVE_ROOT ARCHIVE_ROOT --out results/
    check_archive_subset.py ACTIVE_ROOT ARCHIVE_ROOT --quiet

Stdlib only. Imports compare_repos.py directly (must sit next to this
script) rather than shelling out per repo, and reuses its git plumbing.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare_repos as cr  # noqa: E402


def side_objects(path, progress=False):
    """
    One full object-store walk for one repo (or plain directory), split into
    the three kinds compare_repos.py compares: blobs, trees, commits.

    A plain directory (no .git) has no tree or commit objects at all - only
    its files-on-disk hash into blobs - so those two come back empty, same
    as compare_repos.py's dir_history()/dir fallback.
    """
    if not cr.is_repo(path):
        blob_names, blob_sizes, _ = cr.dir_history(path)
        empty = {}
        return {
            "blob": (blob_names, blob_sizes),
            "tree": (empty, empty),
            "commit": (empty, empty),
        }

    names, sizes, types, _stats = cr.walk_object_store(path, progress)

    def subset(kind):
        keys = [s for s, t in types.items() if t == kind]
        return ({s: names[s] for s in keys}, {s: sizes[s] for s in keys})

    return {
        "blob": subset("blob"),
        "tree": subset("tree"),
        "commit": subset("commit"),
    }


def check_pair(active, archive, progress=False):
    """
    Three-way (blobs, trees, commits) comparison of one repo pair, archive
    as side B. Returns {"blob": h, "tree": h, "commit": h}, each h being a
    compare_repos.compare_object_set() result with "verdict" added.
    """
    a_objs = side_objects(active, progress)
    b_objs = side_objects(archive, progress)

    results = {}
    for kind in ("blob", "tree", "commit"):
        na, sa = a_objs[kind]
        nb, sb = b_objs[kind]
        h = cr.compare_object_set(active, archive, na, sa, nb, sb, kind=kind)
        h["verdict"] = {
            "blob": cr.history_verdict,
            "tree": cr.trees_verdict,
            "commit": cr.commits_verdict,
        }[kind](h)
        results[kind] = h
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Bulk-check that every repo under ARCHIVE_ROOT has no "
                     "blob, tree, or commit missing from its counterpart "
                     "under ACTIVE_ROOT.")
    ap.add_argument("active_root")
    ap.add_argument("archive_root")
    ap.add_argument("--out", metavar="DIR",
                    help="write full delta JSON for every FAILING repo here")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = ap.parse_args()

    if not os.path.isdir(args.archive_root):
        print(f"not a directory: {args.archive_root}", file=sys.stderr)
        return 2

    names = sorted(
        n for n in os.listdir(args.archive_root)
        if os.path.isdir(os.path.join(args.archive_root, n))
    )
    if not names:
        print(f"no subdirectories found under {args.archive_root}", file=sys.stderr)
        return 2

    if args.out:
        os.makedirs(args.out, exist_ok=True)

    results = []
    for name in names:
        archive_path = os.path.join(args.archive_root, name)
        active_path = os.path.join(args.active_root, name)

        if not os.path.isdir(active_path):
            results.append((name, "NO ACTIVE COPY", None))
            print(f"  {name:40} NO ACTIVE COPY - can't check hypothesis",
                  file=sys.stderr)
            continue

        if not args.quiet:
            print(f"checking {name} ...", file=sys.stderr)
        try:
            res = check_pair(active_path, archive_path, progress=not args.quiet)
        except Exception as exc:                       # one bad repo must not
            results.append((name, "ERROR", None))       # abort the whole run
            print(f"  {name:40} ERROR: {exc!r}", file=sys.stderr)
            continue

        deltas = {k: res[k]["only_in_b"] for k in ("blob", "tree", "commit")}
        passed = all(d == 0 for d in deltas.values())
        status = "PASS" if passed else "FAIL"
        results.append((name, status, {"deltas": deltas, "res": res}))

        if not passed and args.out:
            with open(os.path.join(args.out, f"{name}.json"), "w") as fh:
                json.dump({k: res[k] for k in ("blob", "tree", "commit")},
                          fh, indent=2)

    print("\n" + "=" * 88)
    print("  SUBSET CHECK: is every blob/tree/commit under archive/ also in active/?")
    print("=" * 88)
    print(f"  {'repo':40}{'status':8}{'blobs only in archive':>22}"
          f"{'trees only':>12}{'commits only':>14}")
    print("  " + "-" * 86)
    for name, status, data in results:
        if data is None:
            print(f"  {name:40}{status:8}{'-':>22}{'-':>12}{'-':>14}")
        else:
            d = data["deltas"]
            print(f"  {name:40}{status:8}{d['blob']:>22}{d['tree']:>12}"
                  f"{d['commit']:>14}")

    failures = [n for n, s, _ in results if s != "PASS"]
    print()
    if failures:
        print(f"  {len(failures)} of {len(results)} repo(s) did NOT confirm "
              f"the subset hypothesis: {', '.join(failures)}")
        if args.out:
            print(f"  delta details written to {args.out}/<repo>.json")
    else:
        print(f"  all {len(results)} repo(s) confirm: every blob, tree, and "
              f"commit in archive also exists in active.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
