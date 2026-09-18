#!/usr/bin/env python3
"""
compare_inventories.py - diff two hash_inventory.py CSVs against each other.

Answers: how much of drive A's content is also on drive B, and where do they
diverge? Built for comparing separate dumps of the same repo estate (e.g. an
E: snapshot vs a D: snapshot) where the two runs may have taken different
code paths and labelled the same file differently.

Three independent lenses, because "the same" means different things:

  path      same file path in both -> do the hashes match? This is the
            "did this file change between snapshots" question.

  content   hash only, path ignored. Answers "does this content exist
            anywhere in the other snapshot?" - survives renames, repacks,
            and repo-label differences.

  repo      per-repo rollup: which repos are byte-identical, which drifted,
            which exist on only one side.

KEY NORMALISATION
hash_inventory.py labels the same physical file two ways depending on which
code path produced it: a repo whose git history resolved yields
(repo, "sub/f") relative to the repo root, while a .git directory reached by
the disk walk is itself detected as a bare repo, yielding
(repo + "/.git", "objects/...") - see get_git_dir() in hash_inventory.py.
Joining on `repo + "/" + file_path` collapses both to one canonical path,
so the two labellings compare correctly. --strict-key disables this and
joins on the raw (repo, file_path) tuple instead.

CRLF WARNING
A git-sourced inventory (--algo git, no --disk-only) hashes the LF blob git
stored; a --disk-only inventory hashes the bytes on disk, which on a Windows
clone with core.autocrlf=true are CRLF. Both are valid git blob SHA-1s of
different byte streams, so every text file will read as "changed" when you
compare a git-sourced run against a disk run. The content lens is equally
affected. Binary files (.pack, .idx, .png, .dll) are unaffected. This tool
reports a CRLF-shaped heuristic on the changed set so the effect is visible
rather than silently inflating the diff.

Usage:
    compare_inventories.py A.csv B.csv
    compare_inventories.py A.csv B.csv --show 40
    compare_inventories.py inventory_E.csv active_inventory_run2.csv \
        --out-prefix cmp_E_vs_run2 --json cmp.json

Stdlib only.
"""

import argparse
import csv
import json
import os
import sys

csv.field_size_limit(sys.maxsize)


def norm_key(repo, path, strict):
    """Canonical identity for a row. See KEY NORMALISATION above."""
    if strict:
        return (repo, path)
    joined = f"{repo.strip('/')}/{path.strip('/')}" if path else repo.strip("/")
    return joined.replace("\\", "/")


def load(csv_path, strict):
    """key -> (hash, size). Also returns per-key repo label and error count."""
    by_key = {}
    repo_of = {}
    rows = errors = dupes = 0
    repos = set()
    with open(csv_path, "r", newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            repo = row.get("repo") or ""
            path = row.get("file_path") or ""
            repos.add(repo)
            if (row.get("error") or "").strip():
                errors += 1
                continue
            digest = (row.get("hash") or "").strip()
            if not digest:
                errors += 1
                continue
            raw_size = (row.get("size_bytes") or "").strip()
            size = int(raw_size) if raw_size.isdigit() else -1
            key = norm_key(repo, path, strict)
            if key in by_key:
                dupes += 1
            by_key[key] = (digest, size)
            repo_of[key] = repo
    return {"path": csv_path, "rows": rows, "errors": errors, "dupes": dupes,
            "repos": repos, "by_key": by_key, "repo_of": repo_of}


def crlf_shaped(size_a, size_b):
    """A grew-by-a-plausible-line-count delta - the CRLF signature. Not proof
    for any single file; meaningful in aggregate."""
    if size_a < 0 or size_b < 0 or size_b <= size_a:
        return False
    delta = size_b - size_a
    return delta <= max(1, size_a // 2)


def repo_root(key, repo_of):
    """Strip a trailing /.git so a repo and its object store roll up together."""
    label = repo_of.get(key, "")
    return label[:-5] if label.endswith("/.git") else label


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_a", help="first inventory CSV (the baseline)")
    ap.add_argument("csv_b", help="second inventory CSV (compared against A)")
    ap.add_argument("--strict-key", action="store_true",
                    help="join on raw (repo, file_path) instead of the "
                         "normalised full path - see KEY NORMALISATION")
    ap.add_argument("--show", type=int, default=10,
                    help="sample rows to print per bucket (default 10)")
    ap.add_argument("--out-prefix", metavar="PREFIX",
                    help="write <PREFIX>_{identical,changed,only_a,only_b}.csv")
    ap.add_argument("--json", metavar="FILE", help="write the summary as JSON")
    ap.add_argument("--quiet", action="store_true", help="totals only")
    args = ap.parse_args()

    a = load(args.csv_a, args.strict_key)
    b = load(args.csv_b, args.strict_key)
    ka, kb = set(a["by_key"]), set(b["by_key"])

    both = ka & kb
    identical = [k for k in both if a["by_key"][k][0] == b["by_key"][k][0]]
    changed = [k for k in both if a["by_key"][k][0] != b["by_key"][k][0]]
    only_a = sorted(ka - kb)
    only_b = sorted(kb - ka)

    crlf = sum(1 for k in changed
               if crlf_shaped(a["by_key"][k][1], b["by_key"][k][1]))

    # ---- content lens: hash only, path ignored
    ha = {d for d, _ in a["by_key"].values()}
    hb = {d for d, _ in b["by_key"].values()}
    shared_h = ha & hb
    a_content_in_b = sum(1 for d, _ in a["by_key"].values() if d in shared_h)
    b_content_in_a = sum(1 for d, _ in b["by_key"].values() if d in shared_h)

    # ---- repo lens
    ra = {repo_root(k, a["repo_of"]) for k in ka}
    rb = {repo_root(k, b["repo_of"]) for k in kb}
    drifted = {repo_root(k, a["repo_of"]) for k in changed}
    drifted |= {repo_root(k, a["repo_of"]) for k in only_a}
    clean_repos = (ra & rb) - drifted

    def pct(n, d):
        return f"{n / d * 100:5.1f}%" if d else "    -"

    out = sys.stdout
    print(f"A  {a['path']}", file=out)
    print(f"   {a['rows']:>12,} rows | {len(a['repos']):>7,} repo labels | "
          f"{a['errors']:,} error rows | {len(ka):,} usable keys", file=out)
    print(f"B  {b['path']}", file=out)
    print(f"   {b['rows']:>12,} rows | {len(b['repos']):>7,} repo labels | "
          f"{b['errors']:,} error rows | {len(kb):,} usable keys", file=out)
    for side in (a, b):
        if side["dupes"]:
            print(f"   note: {side['dupes']:,} duplicate keys in "
                  f"{side['path']} (last row won)", file=out)

    print(f"\nPATH LENS  (same normalised path in both)", file=out)
    print(f"  identical  path + hash match   {len(identical):>12,}  "
          f"{pct(len(identical), len(both))} of overlap", file=out)
    print(f"  changed    path match, hash differs {len(changed):>7,}  "
          f"{pct(len(changed), len(both))} of overlap", file=out)
    if changed:
        print(f"             of which CRLF-shaped {crlf:>11,}  "
              f"{pct(crlf, len(changed))} of changed", file=out)
    print(f"  only in A                      {len(only_a):>12,}", file=out)
    print(f"  only in B                      {len(only_b):>12,}", file=out)
    print(f"  union                          {len(ka | kb):>12,}", file=out)

    print(f"\nCONTENT LENS  (hash only, path ignored)", file=out)
    print(f"  distinct hashes in A           {len(ha):>12,}", file=out)
    print(f"  distinct hashes in B           {len(hb):>12,}", file=out)
    print(f"  shared hashes                  {len(shared_h):>12,}", file=out)
    print(f"  A files whose content is in B  {a_content_in_b:>12,}  "
          f"{pct(a_content_in_b, len(ka))} of A", file=out)
    print(f"  B files whose content is in A  {b_content_in_a:>12,}  "
          f"{pct(b_content_in_a, len(kb))} of B", file=out)

    print(f"\nREPO LENS", file=out)
    print(f"  repos in A                     {len(ra):>12,}", file=out)
    print(f"  repos in B                     {len(rb):>12,}", file=out)
    print(f"  repos in both                  {len(ra & rb):>12,}", file=out)
    print(f"  fully accounted for in B       {len(clean_repos):>12,}  "
          f"{pct(len(clean_repos), len(ra))} of A's repos", file=out)
    print(f"  drifted / partly missing       {len((ra & rb) - clean_repos):>12,}", file=out)
    print(f"  only in A                      {len(ra - rb):>12,}", file=out)
    print(f"  only in B                      {len(rb - ra):>12,}", file=out)

    if not args.quiet and args.show:
        for label, keys in (("changed", sorted(changed)),
                            ("only in A", only_a),
                            ("only in B", only_b)):
            if not keys:
                continue
            print(f"\nsample {label} ({min(args.show, len(keys))} of "
                  f"{len(keys):,}):", file=out)
            for k in keys[:args.show]:
                if label == "changed":
                    sa, sb = a["by_key"][k][1], b["by_key"][k][1]
                    print(f"  {sa:>10} -> {sb:>10}  {k[:96]}", file=out)
                else:
                    src = a if label == "only in A" else b
                    print(f"  {src['by_key'][k][1]:>10}  {k[:96]}", file=out)

    if args.out_prefix:
        buckets = {"identical": sorted(identical), "changed": sorted(changed),
                   "only_a": only_a, "only_b": only_b}
        for name, keys in buckets.items():
            path = f"{args.out_prefix}_{name}.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["key", "hash_a", "size_a", "hash_b", "size_b"])
                for k in keys:
                    ea = a["by_key"].get(k, ("", ""))
                    eb = b["by_key"].get(k, ("", ""))
                    w.writerow([k, ea[0], ea[1], eb[0], eb[1]])
            print(f"wrote {path} ({len(keys):,} rows)", file=out)

    if args.json:
        summary = {
            "a": {"path": a["path"], "rows": a["rows"], "keys": len(ka),
                  "errors": a["errors"], "distinct_hashes": len(ha)},
            "b": {"path": b["path"], "rows": b["rows"], "keys": len(kb),
                  "errors": b["errors"], "distinct_hashes": len(hb)},
            "path_lens": {"overlap": len(both), "identical": len(identical),
                          "changed": len(changed), "crlf_shaped": crlf,
                          "only_a": len(only_a), "only_b": len(only_b),
                          "union": len(ka | kb)},
            "content_lens": {"shared_hashes": len(shared_h),
                             "a_content_in_b": a_content_in_b,
                             "b_content_in_a": b_content_in_a},
            "repo_lens": {"a": len(ra), "b": len(rb), "both": len(ra & rb),
                          "clean": len(clean_repos),
                          "only_a": len(ra - rb), "only_b": len(rb - ra)},
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        print(f"wrote {args.json}", file=out)


if __name__ == "__main__":
    main()
