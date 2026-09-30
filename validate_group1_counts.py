#!/usr/bin/env python3
"""
validate_group1_counts.py - do the first extraction (file_added_lines.py,
the 32 extensions of group 1) and the all-extension count
(all_extension_counts.py) agree on how many files each repo's history holds?

Both read the same archive history - every branch, merges against their
first parent, renames not followed - so for the 32 extensions they should
count the same paths. This compares, for those extensions:

  first run   rows of <first>/_state/<org>/<repo>/manifest.csv (one per path
              file_added_lines.py saw), and the repo's done.json status
  new run     files_in_history of <counts>/_state/<org>/<repo>/ext_counts.csv

per repo and per extension, and for the repos that differ (--drill N, needs
--repos-root) reads their history again for the 32 extensions and lists the
exact paths one run has and the other does not - which is what shows the
cause (a repo the first run timed out on, an unusual file name, ...).

Only reads; nothing in either output is changed.

Output, under --out:
  by_repo.csv        org, repo, first_status, new_status, first_files,
                     new_files, diff (new - first); every repo of either run
  by_extension.csv   ext, first_files, new_files, diff
  paths_diff.csv     for the drilled repos: org, repo, path, ext,
                     in_first_run, in_history_now, first_run_row_status
  summary.md         totals, the status of the repos that differ, the
                     biggest differences, extensions, the drill result

Usage:
    python3 validate_group1_counts.py \\
        --first /data/workarea/full_extract \\
        --counts /data/workarea/all_ext_counts \\
        --repos-root /data/workarea/archive \\
        --out /data/workarea/validate_group1 --drill 20
"""

import argparse
import csv
import datetime
import os
import sys
import time
from collections import Counter, defaultdict

from extension_versions import read_history, read_json
from file_history_for_list import RecoveredRepo, RepoJob, git_can_open
from repo_extension_summary import EXTENSIONS, ext_key

GROUP_1 = set(EXTENSIONS)


def repos_under(root, what=""):
    """(org, repo, state dir) under root/_state; with `what`, a progress line
    on stderr every 500 repos."""
    state = os.path.join(root, "_state")
    t0, n = time.time(), 0
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            yield org, repo, os.path.join(od, repo)
            n += 1
            if what and n % 500 == 0:
                print("  %s: %s repos read  %.0fs" % (what, f"{n:,}",
                                                    time.time() - t0),
                      file=sys.stderr, flush=True)
    if what:
        print("  %s: %s repos read  %.0fs - done" % (what, f"{n:,}",
                                                    time.time() - t0),
              file=sys.stderr, flush=True)


def read_first(first):
    """{(org, repo): (status, {ext: n}, {path: row status})} - paths only
    kept later for drilled repos, so here: counts per extension."""
    out = {}
    for org, repo, sd in repos_under(first, "first run"):
        done = read_json(os.path.join(sd, "done.json"))
        status = done.get("status", "?") if done else "no done.json"
        per_ext = Counter()
        mp = os.path.join(sd, "manifest.csv")
        if os.path.isfile(mp):
            with open(mp, newline="", encoding="utf-8",
                      errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    e = ext_key(r.get("path") or "")
                    if e in GROUP_1:
                        per_ext[e] += 1
        elif status == "ok":
            status = "ok, no manifest"
        out[(org, repo)] = (status, per_ext)
    return out


def read_new(counts):
    out = {}
    for org, repo, sd in repos_under(counts, "new count"):
        done = read_json(os.path.join(sd, "done.json"))
        status = done.get("status", "?") if done else "no done.json"
        per_ext = Counter()
        cp = os.path.join(sd, "ext_counts.csv")
        if os.path.isfile(cp):
            with open(cp, newline="", encoding="utf-8",
                      errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    if r.get("ext") in GROUP_1 and \
                            (r.get("files_in_history") or "").isdigit():
                        per_ext[r["ext"]] += int(r["files_in_history"])
        out[(org, repo)] = (status, per_ext)
    return out


def first_paths(first, org, repo):
    """{path: row status} of the first run's manifest, group 1 only."""
    mp = os.path.join(first, "_state", org, repo, "manifest.csv")
    out = {}
    if os.path.isfile(mp):
        with open(mp, newline="", encoding="utf-8",
                  errors="surrogateescape") as fh:
            for r in csv.DictReader(fh):
                p = r.get("path") or ""
                if ext_key(p) in GROUP_1:
                    out[p] = r.get("status", "")
    return out


def history_paths(repos_root, org, repo):
    """Every group-1 path the history holds now (as all_extension_counts.py
    counts them)."""
    rp = os.path.join(repos_root, org, repo)
    job = RepoJob(org, repo, 0)
    job.start = time.time()
    recovery = None if git_can_open(rp) else RecoveredRepo(rp)
    try:
        if recovery:
            recovery.__enter__()
            recovery.add_all_commits_as_refs()
            gp = recovery.tmp
        else:
            gp = rp
        return set(read_history(gp, GROUP_1, job))
    finally:
        if recovery:
            recovery.__exit__(None, None, None)


def fmt(v):
    return f"{v:,}" if isinstance(v, int) else str(v)


def table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" if i == 0 else "---:"
                          for i in range(len(header))) + "|"]
    out += ["| " + " | ".join(fmt(v).replace("|", "/") for v in r) + " |"
            for r in rows]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--first", required=True,
                    help="file_added_lines.py output of the first extraction")
    ap.add_argument("--counts", required=True,
                    help="all_extension_counts.py --out folder")
    ap.add_argument("--out", required=True, help="folder for the results")
    ap.add_argument("--repos-root", help="the archive repos (for --drill)")
    ap.add_argument("--drill", type=int, default=0, metavar="N",
                    help="list the differing paths of the N repos that differ "
                         "most (reads their history again; needs --repos-root)")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="also drill into this repo (repeatable)")
    args = ap.parse_args()
    for d in (args.first, args.counts):
        if not os.path.isdir(os.path.join(d, "_state")):
            sys.exit("no _state folder under " + d)
    if (args.drill or args.repo) and not args.repos_root:
        sys.exit("--drill / --repo need --repos-root")
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()

    print("reading the first run ...", file=sys.stderr, flush=True)
    first = read_first(args.first)
    print("reading the new count ...", file=sys.stderr, flush=True)
    new = read_new(args.counts)

    rows, by_ext = [], defaultdict(lambda: [0, 0])
    for k in sorted(set(first) | set(new)):
        fs, fe = first.get(k, ("not in first run", Counter()))
        ns, ne = new.get(k, ("not in new run", Counter()))
        f, n = sum(fe.values()), sum(ne.values())
        rows.append([k[0], k[1], fs, ns, f, n, n - f])
        for e in GROUP_1:
            by_ext[e][0] += fe[e]
            by_ext[e][1] += ne[e]
    with open(os.path.join(args.out, "by_repo.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "first_status", "new_status", "first_files",
                    "new_files", "diff"])
        w.writerows(sorted(rows, key=lambda r: (-abs(r[6]), r[0], r[1])))
    ext_rows = sorted(([e, f, n, n - f] for e, (f, n) in by_ext.items()),
                      key=lambda r: (-abs(r[3]), r[0]))
    with open(os.path.join(args.out, "by_extension.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "first_files", "new_files", "diff"])
        w.writerows(ext_rows)

    tot_f = sum(r[4] for r in rows)
    tot_n = sum(r[5] for r in rows)
    differ = [r for r in rows if r[6]]
    more = [r for r in differ if r[6] > 0]
    less = [r for r in differ if r[6] < 0]

    # drill: the exact paths
    drill = sorted(differ, key=lambda r: -abs(r[6]))[:args.drill]
    drill_keys = [(r[0], r[1]) for r in drill]
    for spec in args.repo:
        o, _, rp = spec.strip("/").partition("/")
        if (o, rp) not in drill_keys:
            drill_keys.append((o, rp))
    pd_rows, drill_summary = [], []
    for i, (o, rp) in enumerate(drill_keys, 1):
        print("drill %d/%d %s/%s ..." % (i, len(drill_keys), o, rp),
              file=sys.stderr, flush=True)
        fp = first_paths(args.first, o, rp)
        try:
            hp = history_paths(args.repos_root, o, rp)
        except Exception as exc:                 # noqa: BLE001 - one repo
            drill_summary.append([o, rp, len(fp), "error: %s" % exc, "", ""])
            continue
        only_hist = sorted(hp - set(fp))
        only_first = sorted(set(fp) - hp)
        for p in only_hist:
            pd_rows.append([o, rp, p, ext_key(p), "no", "yes", ""])
        for p in only_first:
            pd_rows.append([o, rp, p, ext_key(p), "yes", "no", fp[p]])
        drill_summary.append([o, rp, len(fp), len(hp), len(only_hist),
                              len(only_first)])
    with open(os.path.join(args.out, "paths_diff.csv"), "w", newline="",
              encoding="utf-8", errors="backslashreplace") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "path", "ext", "in_first_run",
                    "in_history_now", "first_run_row_status"])
        w.writerows(pd_rows)

    status_of_differ = Counter(r[2] for r in differ)
    md = ["# Group 1 counts: first extraction vs all-extension count", "",
          "Generated %s by validate_group1_counts.py. The 32 extensions of "
          "the first extraction; files = distinct paths in the history."
          % datetime.date.today().isoformat(), "",
          table(["", "first run", "new count", "difference"], [
              ["files", tot_f, tot_n, "%+d" % (tot_n - tot_f)],
              ["repos", len(first), len(new), "%+d" % (len(new) - len(first))]]),
          "",
          table(["repos", "count"], [
              ["agree", len(rows) - len(differ)],
              ["differ", len(differ)],
              ["... new count higher", len(more)],
              ["... new count lower", len(less)]]), "",
          "## First-run status of the repos that differ", "",
          "_A repo the first run timed out on, or failed, has fewer (or no) "
          "files there._", "",
          table(["first-run status", "repos", "files (new - first)"],
                [[s, n, "%+d" % sum(r[6] for r in differ if r[2] == s)]
                 for s, n in status_of_differ.most_common()]), "",
          "## Biggest differences", "",
          table(["org", "repo", "first status", "first", "new", "diff"],
                [[r[0], r[1], r[2], r[4], r[5], "%+d" % r[6]]
                 for r in sorted(differ, key=lambda r: -abs(r[6]))[:25]]), "",
          "## Per extension", "",
          table(["ext", "first run", "new count", "diff"],
                [[e, f, n, "%+d" % d] for e, f, n, d in ext_rows]), ""]
    if drill_summary:
        md += ["## Drill: exact paths", "",
               "_History read again for these repos. only in history now = "
               "paths the first run did not record; only in first run = paths "
               "the history does not hold now. Every path is in "
               "paths_diff.csv._", "",
               table(["org", "repo", "first run", "history now",
                      "only in history now", "only in first run"],
                     drill_summary), ""]
        if pd_rows:
            md += ["Examples:", "",
                   table(["repo", "path", "in first run", "in history now",
                          "first-run row status"],
                         [[r[1], repr(r[2])[:80], r[4], r[5], r[6]]
                          for r in pd_rows[:20]]), ""]
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")

    print("first run %s files, new count %s files, difference %+d; %d repo(s) "
          "differ -> %s  (%.0fs)"
          % (f"{tot_f:,}", f"{tot_n:,}", tot_n - tot_f, len(differ),
             os.path.join(args.out, "summary.md"), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
