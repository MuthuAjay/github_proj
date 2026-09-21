#!/usr/bin/env python3
"""
explore_input_csv.py - profile a big file-list CSV (columns: org, repo,
relpath, filename, sha256) before running anything against it.

Streams the file once (no pandas), so five million rows is fine. Reports:

  * size: rows, orgs, repos (org/repo pairs), distinct paths
  * files per org and per repo (top N, plus min/median/mean/p90/p99/max)
  * repo names that occur under more than one org (they would collide if you
    ever key on the repo name alone)
  * how relpath and filename relate: does relpath already include the
    filename, or is it only the folder? any rows where they disagree?
  * path shape: depth distribution, backslashes, leading slashes, "./",
    "..", non-ASCII, spaces, leading/trailing whitespace, case-only clashes
  * extensions (top N), files with no extension, dotfiles
  * duplicates: the same org/repo/path more than once
  * sha256: blank, malformed (not 64 hex), and hashes shared by many paths
  * with --repos-root: how many org/repo pairs exist on disk as
    <root>/<org>/<repo> (a repo with a .git folder or a bare repo)

Usage:
    python explore_input_csv.py input.csv
    python explore_input_csv.py input.csv --repos-root /path/to/AllRepos \\
        --out-dir profile_out --top 30
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict

REQUIRED = ["org", "repo", "relpath", "filename", "sha256"]
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def detect_delimiter(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        head = fh.readline()
    counts = {d: head.count(d) for d in (",", "\t", "|", ";")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def dist_line(values):
    v = sorted(values)
    if not v:
        return "n/a"
    return ("min %s | median %s | mean %.1f | p90 %s | p99 %s | max %s"
            % (f"{v[0]:,}", f"{pct(v, .5):,}", sum(v) / len(v),
               f"{pct(v, .9):,}", f"{pct(v, .99):,}", f"{v[-1]:,}"))


def show_top(title, counter, n, indent="  "):
    print(f"\n{title}")
    for key, cnt in counter.most_common(n):
        print(f"{indent}{cnt:>12,}  {key}")
    if len(counter) > n:
        print(f"{indent}{'...':>12}  ({len(counter) - n:,} more)")


def is_repo_dir(path):
    if os.path.isdir(os.path.join(path, ".git")):
        return True
    return (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="input CSV/TSV")
    ap.add_argument("--delimiter", help="field delimiter (default: auto-detect)")
    ap.add_argument("--top", type=int, default=15, help="rows in each top-N list")
    ap.add_argument("--repos-root",
                    help="check each org/repo exists as <root>/<org>/<repo>")
    ap.add_argument("--out-dir",
                    help="also write orgs.csv, repos.csv, extensions.csv here")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        print(f"not a file: {args.csv_in}", file=sys.stderr)
        return 2
    delim = args.delimiter or detect_delimiter(args.csv_in)
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

    fh = open(args.csv_in, newline="", encoding="utf-8-sig", errors="replace")
    reader = csv.reader(fh, delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        print("empty file", file=sys.stderr)
        return 2
    norm = [h.strip().lower() for h in header]
    missing = [c for c in REQUIRED if c not in norm]
    if missing:
        print(f"missing column(s) {missing}; found {header}", file=sys.stderr)
        return 2
    ix = {c: norm.index(c) for c in REQUIRED}
    width = len(header)

    rows = bad_width = 0
    org_files = Counter()
    repo_files = Counter()                 # (org, repo) -> files
    repo_orgs = defaultdict(set)           # repo -> {orgs}
    ext = Counter()
    depth = Counter()
    seen = set()                           # hash of (org, repo, path)
    dup_rows = 0
    dup_examples = []
    sha_count = Counter()                  # first 16 hex chars as int
    blank = Counter()
    sha_bad = 0
    rel_includes = rel_dir_only = rel_conflict = 0
    conflict_examples = []
    flags = Counter()
    lower_seen = {}                        # hash(lowercased key) -> hash(key)
    case_clash = 0
    noext = dotfiles = 0
    start = time.time()

    for row in reader:
        rows += 1
        if len(row) < width:
            bad_width += 1
            row = row + [""] * (width - len(row))
        org = row[ix["org"]]
        repo = row[ix["repo"]]
        rel = row[ix["relpath"]]
        fname = row[ix["filename"]]
        sha = row[ix["sha256"]]

        for name, val in (("org", org), ("repo", repo), ("relpath", rel),
                          ("filename", fname), ("sha256", sha)):
            if not val.strip():
                blank[name] += 1

        org_files[org] += 1
        repo_files[(org, repo)] += 1
        repo_orgs[repo].add(org)

        # relpath vs filename
        if fname and (rel == fname or rel.endswith("/" + fname)
                      or rel.endswith("\\" + fname)):
            rel_includes += 1
            full = rel
        elif fname and rel:
            rel_dir_only += 1
            full = rel.rstrip("/\\") + "/" + fname
            base = rel.replace("\\", "/").rsplit("/", 1)[-1]
            if base == fname:
                pass
            elif "." in base and "." in fname and base != fname \
                    and rel.replace("\\", "/").rstrip("/").endswith(base) \
                    and base.split(".")[-1] == fname.split(".")[-1]:
                rel_conflict += 1
                if len(conflict_examples) < 5:
                    conflict_examples.append((rel, fname))
        else:
            full = rel or fname

        # path shape
        p = full
        if "\\" in p:
            flags["contains backslash"] += 1
        if p.startswith("/"):
            flags["leading slash"] += 1
        if p.startswith("./") or p.startswith(".\\"):
            flags['starts with "./"'] += 1
        if ".." in p.replace("\\", "/").split("/"):
            flags['contains ".." segment'] += 1
        if any(ord(ch) > 127 for ch in p):
            flags["non-ASCII characters"] += 1
        if " " in p:
            flags["contains space"] += 1
        if p != p.strip():
            flags["leading/trailing whitespace"] += 1
        if "//" in p:
            flags['contains "//"'] += 1
        parts = p.replace("\\", "/").strip("/").split("/")
        depth[len(parts) - 1] += 1

        leaf = parts[-1]
        stem, dot, e = leaf.rpartition(".")
        if leaf.startswith(".") and leaf.count(".") == 1:
            dotfiles += 1
            ext["(dotfile)"] += 1
        elif dot:
            ext["." + e.lower()] += 1
        else:
            noext += 1
            ext["(no extension)"] += 1

        # duplicates and case-only clashes
        key = hash((org, repo, p))
        if key in seen:
            dup_rows += 1
            if len(dup_examples) < 5:
                dup_examples.append((org, repo, p))
        else:
            seen.add(key)
            lk = hash((org.lower(), repo.lower(), p.lower()))
            prev = lower_seen.get(lk)
            if prev is not None and prev != key:
                case_clash += 1
            else:
                lower_seen[lk] = key

        # sha256
        s = sha.strip()
        if s:
            if not HEX64.match(s):
                sha_bad += 1
            sha_count[int(s[:16], 16) if re.match(r"^[0-9a-fA-F]{16}", s)
                      else hash(s)] += 1

        if rows % 250000 == 0:
            print(f"\r  read {rows:,} rows | {time.time() - start:,.0f}s",
                  end="", file=sys.stderr, flush=True)
    fh.close()
    if rows >= 250000:
        print(" " * 60, end="\r", file=sys.stderr)

    # ------------------------------------------------------------------ report
    line = "=" * 72
    print(line)
    print(f"file      {args.csv_in}")
    print(f"delimiter {delim!r}   columns {header}")
    print(f"read in   {time.time() - start:,.1f}s")
    print(line)

    orgs = len(org_files)
    repos = len(repo_files)
    print("\nSIZE")
    print(f"  rows                {rows:>12,}")
    print(f"  orgs                {orgs:>12,}")
    print(f"  repos (org/repo)    {repos:>12,}")
    print(f"  distinct repo names {len(repo_orgs):>12,}")
    print(f"  distinct paths      {len(seen):>12,}  (org/repo/path)")
    if bad_width:
        print(f"  rows shorter than the header: {bad_width:,}")

    print("\nFILES PER ORG")
    print("  " + dist_line(list(org_files.values())))
    show_top("  Top orgs by files:", org_files, args.top, "    ")
    orgs_repo_counts = Counter(o for o, _ in repo_files)
    print("\nREPOS PER ORG")
    print("  " + dist_line(list(orgs_repo_counts.values())))
    show_top("  Top orgs by repo count:", orgs_repo_counts, args.top, "    ")

    print("\nFILES PER REPO")
    print("  " + dist_line(list(repo_files.values())))
    show_top("  Top repos by files:",
             Counter({f"{o}/{r}": c for (o, r), c in repo_files.items()}),
             args.top, "    ")
    small = sum(1 for c in repo_files.values() if c <= 10)
    print(f"\n  repos with <= 10 files: {small:,} of {repos:,}")

    shared = {r: o for r, o in repo_orgs.items() if len(o) > 1}
    print("\nREPO NAMES UNDER MORE THAN ONE ORG")
    print(f"  {len(shared):,} repo name(s) (these collide if you key on repo alone)")
    for r, o in sorted(shared.items(), key=lambda kv: -len(kv[1]))[:args.top]:
        print(f"    {r}  ->  {len(o)} orgs: {', '.join(sorted(o)[:5])}"
              f"{' ...' if len(o) > 5 else ''}")

    print("\nRELPATH vs FILENAME")
    print(f"  relpath already ends with the filename : {rel_includes:>12,}")
    print(f"  relpath looks like the folder only     : {rel_dir_only:>12,}")
    if rel_conflict:
        print(f"  filename differs from relpath's leaf   : {rel_conflict:>12,}")
        for rel, fn in conflict_examples:
            print(f"      relpath={rel!r} filename={fn!r}")
    print("  -> the full path is relpath when it includes the filename, else "
          "relpath/filename")

    print("\nBLANK VALUES")
    if blank:
        for k in REQUIRED:
            if blank[k]:
                print(f"  {k:<10} {blank[k]:>12,}")
    else:
        print("  none")

    print("\nPATH DEPTH (folders above the file)")
    for d in sorted(depth)[:12]:
        print(f"  {d:>3}  {depth[d]:>12,}")
    if len(depth) > 12:
        print(f"  ... deepest: {max(depth)}")

    print("\nPATH ANOMALIES")
    if flags or case_clash:
        for k, v in flags.most_common():
            print(f"  {v:>12,}  {k}")
        if case_clash:
            print(f"  {case_clash:>12,}  paths that differ only by letter case "
                  f"(matters on case-insensitive drives)")
    else:
        print("  none")

    show_top("EXTENSIONS", ext, args.top)

    print("\nDUPLICATES")
    print(f"  repeated org/repo/path rows: {dup_rows:,}")
    for ex in dup_examples:
        print(f"    {ex}")

    print("\nSHA256")
    total_sha = sum(sha_count.values())
    shared_sha = sum(1 for c in sha_count.values() if c > 1)
    biggest = sha_count.most_common(1)
    print(f"  non-blank            {total_sha:>12,}")
    print(f"  not 64 hex chars     {sha_bad:>12,}")
    print(f"  distinct values      {len(sha_count):>12,}")
    print(f"  values on >1 path    {shared_sha:>12,}")
    if biggest and biggest[0][1] > 1:
        print(f"  most-shared value covers {biggest[0][1]:,} paths "
              f"(empty files or copied files share a hash)")
    print("  (sha256 is reported only; it is not used for matching)")

    on_disk = None
    if args.repos_root:
        print(f"\nREPOS ON DISK  ({args.repos_root})")
        found = 0
        missing_list = []
        for org, repo in repo_files:
            if is_repo_dir(os.path.join(args.repos_root, org, repo)):
                found += 1
            elif len(missing_list) < args.top:
                missing_list.append(f"{org}/{repo}")
        on_disk = found
        print(f"  found   {found:>10,} of {repos:,}")
        print(f"  missing {repos - found:>10,}")
        for m in missing_list:
            print(f"    {m}")
        if repos - found > len(missing_list):
            print(f"    ... ({repos - found - len(missing_list):,} more)")
        if found:
            have = sum(c for (o, r), c in repo_files.items()
                       if is_repo_dir(os.path.join(args.repos_root, o, r)))
            print(f"  rows in repos that exist: {have:,} of {rows:,} "
                  f"({have * 100 / rows:.1f}%)")

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "orgs.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["org", "repos", "files"])
            for o, c in org_files.most_common():
                w.writerow([o, orgs_repo_counts[o], c])
        with open(os.path.join(args.out_dir, "repos.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["org", "repo", "files", "orgs_with_this_repo_name"])
            for (o, r), c in repo_files.most_common():
                w.writerow([o, r, c, len(repo_orgs[r])])
        with open(os.path.join(args.out_dir, "extensions.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["extension", "files"])
            for e, c in ext.most_common():
                w.writerow([e, c])
        print(f"\nwrote orgs.csv, repos.csv, extensions.csv -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
