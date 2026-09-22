#!/usr/bin/env python3
"""
analyse_file_summary.py - profile the file_summary.csv that
file_history_for_list.py writes.

Streams the file once (no pandas), so five and a half million rows is fine.
Nothing is held that grows with the row count: distributions are kept as
{value: count} maps and the hot-file list as a bounded heap, so the memory
cost is set by how many repos and extensions the input has, not how many
files.

Reports:

  * statuses: found / not_found / repo_missing / ... with shares
  * churn: commits_touched distribution, and the added/modified/deleted/
    renamed mix across every matched file
  * one-commit files (added and never touched again) and files that were
    deleted at some point
  * age: first_seen and last_changed by year, and how stale each file is now
  * hot files: the paths with the most commits behind them
  * extensions: rows, total commits, and mean commits per file
  * orgs and repos ranked by rows and by total commits

and, for the rows that did NOT match, the question worth asking first:

  * not_found split into REPO-LEVEL (the repo matched nothing at all, so the
    repo or the clone is wrong) and PATH-LEVEL (the same repo matched other
    rows, so that one path is wrong). They have completely different fixes,
    and the totals alone cannot tell them apart.
  * the repos contributing the most not_found rows, and the extensions those
    rows carry - a `not_found` list dominated by build output or vendored
    directories usually means the input lists files that were never committed
  * every <org>/<repo> that was missing from --repos-root

With --out-dir, also writes by_org.csv, by_repo.csv, top_churn.csv,
extensions.csv, not_found_repos.csv, repo_missing.csv and a sample of
not_found rows for eyeballing.

Usage:
    python analyse_file_summary.py /data/workarea/file_history_out_4/file_summary.csv
    python analyse_file_summary.py out_4/file_summary.csv --out-dir profile --top 30
"""

import argparse
import csv
import heapq
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date

NEEDED = ["org", "repo", "relpath", "filename", "status", "matched_path",
          "commits_touched", "added", "modified", "deleted", "renamed",
          "first_seen", "last_changed", "present_at_head"]

# how stale a file is, by days since its last commit
STALE_BUCKETS = [(30, "under a month"), (90, "1-3 months"),
                 (365, "3-12 months"), (730, "1-2 years"),
                 (1825, "2-5 years"), (10 ** 6, "over 5 years")]


def pct_from_hist(hist, total, p):
    """The p-quantile (0..1) of a {value: count} map, by nearest rank - the
    same definition explore_input_csv.py uses on a sorted list."""
    if not total:
        return 0
    idx = min(total - 1, int(total * p))
    seen = 0
    for v in sorted(hist):
        seen += hist[v]
        if seen > idx:
            return v
    return max(hist) if hist else 0


def dist_line(hist):
    """min / median / mean / p90 / p99 / max of a {value: count} map."""
    total = sum(hist.values())
    if not total:
        return "n/a"
    ssum = sum(v * c for v, c in hist.items())
    return ("min %s | median %s | mean %.1f | p90 %s | p99 %s | max %s"
            % (f"{min(hist):,}", f"{pct_from_hist(hist, total, .5):,}",
               ssum / total, f"{pct_from_hist(hist, total, .9):,}",
               f"{pct_from_hist(hist, total, .99):,}", f"{max(hist):,}"))


def show_top(title, counter, n, indent="  "):
    print("\n%s" % title)
    if not counter:
        print("%s(none)" % indent)
        return
    for key, cnt in counter.most_common(n):
        print("%s%12s  %s" % (indent, f"{cnt:,}", key))
    if len(counter) > n:
        print("%s%12s  (%s more)" % (indent, "...", f"{len(counter) - n:,}"))


def ext_of(name):
    """'.java', '(dotfile)' or '(no extension)' for a file name."""
    leaf = (name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not leaf:
        return "(blank)"
    if leaf.startswith(".") and leaf.count(".") == 1:
        return "(dotfile)"
    stem, dot, e = leaf.rpartition(".")
    if dot and stem:
        return "." + e.lower()
    return "(no extension)"


def days_since(iso, today):
    """Whole days between an ISO-8601 commit date and today, or None. Only the
    date part is read: the UTC offset cannot move a file between staleness
    buckets, and parsing it for every row is not free."""
    try:
        return (today - date.fromisoformat(iso[:10])).days
    except (ValueError, TypeError):
        return None


def bucket_for(days):
    for limit, label in STALE_BUCKETS:
        if days < limit:
            return label
    return STALE_BUCKETS[-1][1]


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="file_summary.csv from file_history_for_list.py")
    ap.add_argument("--top", type=int, default=20, help="rows in each top-N list")
    ap.add_argument("--out-dir", help="also write the full rankings as CSVs here")
    ap.add_argument("--samples", type=int, default=200,
                    help="not_found rows to keep as examples (default 200)")
    ap.add_argument("--churn-rows", type=int, default=1000,
                    help="paths to keep for top_churn.csv (default 1,000); the "
                         "printed list is --top long whatever this is")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        print("not a file: %s" % args.csv_in, file=sys.stderr)
        return 2
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    today = date.today()

    fh = open(args.csv_in, newline="", encoding="utf-8", errors="surrogateescape")
    reader = csv.reader(fh)
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        print("empty file", file=sys.stderr)
        return 2
    missing = [c for c in NEEDED if c not in header]
    if missing:
        print("missing column(s) %s; found %s" % (missing, header), file=sys.stderr)
        return 2
    ix = {c: header.index(c) for c in NEEDED}
    width = len(header)

    rows = 0
    status = Counter()
    head = Counter()
    commits_hist = Counter()
    kind = Counter()                      # added / modified / deleted / renamed
    first_year = Counter()
    last_year = Counter()
    stale = Counter()
    ext_rows, ext_commits, ext_nf = Counter(), Counter(), Counter()
    org_stat = defaultdict(lambda: [0, 0, 0, 0])   # rows, found, not_found, commits
    repo_stat = defaultdict(lambda: [0, 0, 0, 0])
    hot = []                              # bounded heap of (commits, org, repo, path)
    hot_cap = max(args.top, args.churn_rows)
    nf_seen = Counter()                   # not_found examples kept per repo
    one_commit = ever_deleted = ever_renamed = 0
    missing_repos = set()
    nf_samples = []
    t0 = time.time()

    for row in reader:
        rows += 1
        if len(row) < width:
            row = row + [""] * (width - len(row))
        st = row[ix["status"]]
        org, repo = row[ix["org"]], row[ix["repo"]]
        status[st] += 1
        o, r = org_stat[org], repo_stat[(org, repo)]
        o[0] += 1
        r[0] += 1

        if st == "repo_missing":
            missing_repos.add((org, repo))
        if st == "not_found":
            o[2] += 1
            r[2] += 1
            ext_nf[ext_of(row[ix["filename"]] or row[ix["relpath"]])] += 1
            # at most two per repo, so the examples show different problems
            # rather than the first repo's first twenty rows
            if len(nf_samples) < args.samples and nf_seen[(org, repo)] < 2:
                nf_seen[(org, repo)] += 1
                nf_samples.append([org, repo, row[ix["relpath"]],
                                   row[ix["filename"]], row[ix["matched_path"]],
                                   row[ix["present_at_head"]]])
            continue
        if st != "found":
            continue

        o[1] += 1
        r[1] += 1
        head[row[ix["present_at_head"]] or "(blank)"] += 1
        try:
            n = int(row[ix["commits_touched"]] or 0)
        except ValueError:
            n = 0
        commits_hist[n] += 1
        o[3] += n
        r[3] += n
        if n == 1:
            one_commit += 1

        for k in ("added", "modified", "deleted", "renamed"):
            try:
                v = int(row[ix[k]] or 0)
            except ValueError:
                v = 0
            kind[k] += v
            if v and k == "deleted":
                ever_deleted += 1
            if v and k == "renamed":
                ever_renamed += 1

        path = row[ix["matched_path"]]
        e = ext_of(path or row[ix["filename"]])
        ext_rows[e] += 1
        ext_commits[e] += n

        fs, ls = row[ix["first_seen"]], row[ix["last_changed"]]
        if len(fs) >= 4 and fs[:4].isdigit():
            first_year[fs[:4]] += 1
        if len(ls) >= 4 and ls[:4].isdigit():
            last_year[ls[:4]] += 1
            d = days_since(ls, today)
            if d is not None:
                stale[bucket_for(d)] += 1

        item = (n, org, repo, path)
        if len(hot) < hot_cap:
            heapq.heappush(hot, item)
        elif n > hot[0][0]:
            heapq.heappushpop(hot, item)

    fh.close()
    found = status["found"]
    print("=" * 78)
    print("file_summary  %s" % args.csv_in)
    print("rows          %s in %.1fs" % (f"{rows:,}", time.time() - t0))
    print("orgs          %s      repos  %s"
          % (f"{len(org_stat):,}", f"{len(repo_stat):,}"))

    print("\nSTATUS")
    for st, cnt in status.most_common():
        print("  %-20s %12s  %5.1f%%" % (st, f"{cnt:,}", 100.0 * cnt / rows))

    if not found:
        print("\nnothing matched - no statistics to report")
        return 0

    print("\nCHURN  (%s matched files)" % f"{found:,}")
    print("  commits per file    %s" % dist_line(commits_hist))
    total_commits = sum(v * c for v, c in commits_hist.items())
    print("  total file-commits  %s" % f"{total_commits:,}")
    print("  changed once only   %12s  %5.1f%%  (added and never touched again)"
          % (f"{one_commit:,}", 100.0 * one_commit / found))
    print("  deleted at least once %10s  %5.1f%%"
          % (f"{ever_deleted:,}", 100.0 * ever_deleted / found))
    print("  renamed at least once %10s  %5.1f%%  (0 unless --follow-renames)"
          % (f"{ever_renamed:,}", 100.0 * ever_renamed / found))
    print("\n  change mix")
    kt = sum(kind.values()) or 1
    for k in ("added", "modified", "deleted", "renamed"):
        print("    %-10s %14s  %5.1f%%" % (k, f"{kind[k]:,}", 100.0 * kind[k] / kt))

    print("\n  still present at HEAD")
    for k, c in head.most_common():
        print("    %-10s %14s  %5.1f%%" % (k, f"{c:,}", 100.0 * c / found))

    print("\nAGE")
    print("  first seen by year")
    for y in sorted(first_year):
        print("    %s %12s  %s" % (y, f"{first_year[y]:,}",
                                   "#" * int(40.0 * first_year[y] / max(first_year.values()))))
    print("  last changed by year")
    for y in sorted(last_year):
        print("    %s %12s  %s" % (y, f"{last_year[y]:,}",
                                   "#" * int(40.0 * last_year[y] / max(last_year.values()))))
    print("\n  staleness (days since last commit)")
    for _limit, label in STALE_BUCKETS:
        if stale[label]:
            print("    %-16s %12s  %5.1f%%"
                  % (label, f"{stale[label]:,}", 100.0 * stale[label] / found))

    print("\nHOT FILES  (most commits behind them)")
    for n, org, repo, path in heapq.nlargest(args.top, hot):
        print("  %8s  %s/%s  %s" % (f"{n:,}", org, repo, path))

    show_top("EXTENSIONS by matched files", ext_rows, args.top)
    show_top("EXTENSIONS by total commits", ext_commits, args.top)

    show_top("ORGS by rows", Counter({k: v[0] for k, v in org_stat.items()}), args.top)
    show_top("REPOS by total commits",
             Counter({"%s/%s" % k: v[3] for k, v in repo_stat.items()}), args.top)

    # ---- why rows did not match -------------------------------------------
    nf = status["not_found"]
    if nf:
        repo_level = path_level = 0
        nf_by_repo = Counter()
        dead_repos = 0
        for key, v in repo_stat.items():
            if not v[2]:
                continue
            nf_by_repo["%s/%s" % key] = v[2]
            if v[1] == 0:                 # the repo matched nothing at all
                repo_level += v[2]
                dead_repos += 1
            else:
                path_level += v[2]
        print("\n" + "=" * 78)
        print("NOT_FOUND  %s rows (%.1f%% of all rows)" % (f"{nf:,}", 100.0 * nf / rows))
        print("  repo-level  %12s  %5.1f%%  in %s repo(s) where NOTHING matched"
              % (f"{repo_level:,}", 100.0 * repo_level / nf, f"{dead_repos:,}"))
        print("              -> suspect the clone: wrong repo, shallow/partial "
              "clone, or all\n                 history under a different path root")
        print("  path-level  %12s  %5.1f%%  in repos that matched other rows"
              % (f"{path_level:,}", 100.0 * path_level / nf))
        print("              -> suspect the path: never committed (build output, "
              "vendored\n                 deps), or a rename the default run does "
              "not follow")
        show_top("  repos with the most not_found rows", nf_by_repo, args.top, "    ")
        show_top("  not_found by extension", ext_nf, args.top, "    ")
        if nf_samples:
            print("\n  examples (relpath -> matched_path tried)")
            for org, repo, rel, fname, matched, at_head in nf_samples[:8]:
                print("    %s/%s" % (org, repo))
                print("      relpath  %s" % rel)
                print("      tried    %s   (at HEAD: %s)" % (matched, at_head or "-"))

    if missing_repos:
        print("\nREPO_MISSING  %s row(s) across %s <org>/<repo> not under --repos-root"
              % (f"{status['repo_missing']:,}", f"{len(missing_repos):,}"))
        for org, repo in sorted(missing_repos)[:args.top]:
            print("    %s/%s" % (org, repo))
        if len(missing_repos) > args.top:
            print("    ... (%s more)" % f"{len(missing_repos) - args.top:,}")

    # ---- CSVs -------------------------------------------------------------
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        d = args.out_dir
        write_csv(os.path.join(d, "by_org.csv"),
                  ["org", "rows", "found", "not_found", "total_commits",
                   "mean_commits_per_found"],
                  [[k, v[0], v[1], v[2], v[3], round(v[3] / v[1], 2) if v[1] else ""]
                   for k, v in sorted(org_stat.items(), key=lambda kv: -kv[1][0])])
        write_csv(os.path.join(d, "by_repo.csv"),
                  ["org", "repo", "rows", "found", "not_found", "total_commits",
                   "mean_commits_per_found"],
                  [[k[0], k[1], v[0], v[1], v[2], v[3],
                    round(v[3] / v[1], 2) if v[1] else ""]
                   for k, v in sorted(repo_stat.items(), key=lambda kv: -kv[1][0])])
        write_csv(os.path.join(d, "top_churn.csv"),
                  ["commits_touched", "org", "repo", "matched_path"],
                  [[n, o, r, p] for n, o, r, p in sorted(hot, reverse=True)])
        write_csv(os.path.join(d, "extensions.csv"),
                  ["extension", "matched_files", "total_commits",
                   "mean_commits", "not_found_rows"],
                  [[e, ext_rows[e], ext_commits[e],
                    round(ext_commits[e] / ext_rows[e], 2) if ext_rows[e] else "",
                    ext_nf.get(e, 0)]
                   for e in sorted(set(ext_rows) | set(ext_nf),
                                   key=lambda x: -ext_rows[x])])
        write_csv(os.path.join(d, "not_found_repos.csv"),
                  ["org", "repo", "not_found", "found", "kind"],
                  [[k[0], k[1], v[2], v[1], "repo-level" if v[1] == 0 else "path-level"]
                   for k, v in sorted(repo_stat.items(), key=lambda kv: -kv[1][2])
                   if v[2]])
        write_csv(os.path.join(d, "repo_missing.csv"), ["org", "repo"],
                  sorted(missing_repos))
        write_csv(os.path.join(d, "not_found_samples.csv"),
                  ["org", "repo", "relpath", "filename", "matched_path",
                   "present_at_head"], nf_samples)
        print("\nwrote by_org.csv, by_repo.csv, top_churn.csv, extensions.csv,")
        print("      not_found_repos.csv, repo_missing.csv, not_found_samples.csv -> %s" % d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
