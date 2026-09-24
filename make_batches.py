#!/usr/bin/env python3
"""
make_batches.py - split the repos to process into scheduled batches,
balanced by how much work each repo is.

Input: the per-repo CSV repo_extension_summary.py writes (org, repo,
repo_status, listed_files, listed_commits, ...). A repo's weight is its
commits_touched on the listed extensions (--weight), which is what the
history extraction's run time scales with.

Repos are ranked by weight and cut into tiers, because the work is very
lopsided (a few dozen repos can be 40% of it):

  G  giant    the top --giant repos
  L  large    the next ones, down to rank --large
  M  medium   down to rank --medium
  S  small    everything else

G, L and M are split into --giant-batches / --large-batches /
--medium-batches batches of near-equal total weight: repos are taken largest
first and each goes to the batch with the least weight so far. A repo heavier
than a batch's fair share ends up alone in its batch. S is cut into batches
of --small-size repos, dealt round-robin in weight order so each small batch
gets the same mix of sizes - their run time is per-repo overhead more than
content.

Repos whose repo_status is repo_missing (or any status not in --statuses)
are left out and listed in excluded.csv. The "(all)" totals row is ignored.

Writes, under --out:
  G01.csv, G02.csv, ..., L01.csv, ..., S06.csv
                one per batch: org, repo, tier, weight, listed_files -
                usable as the repo list for one scheduled run
  plan.csv      one row per batch: batch, tier, repos, weight, weight share,
                files, heaviest repo, and the suggested workers and per-repo
                timeout for that tier
  excluded.csv  repos left out, with their status

Usage:
    python3 make_batches.py repo_extension_summary_github.csv --out batches
    python3 make_batches.py repo_extension_summary_github.csv --out batches \\
        --giant 25 --large 500 --medium 2000 --small-size 2000
"""

import argparse
import csv
import heapq
import os
import sys

# suggested run settings per tier: (workers, per-repo timeout in seconds)
TIER_SETTINGS = {"G": (2, 12 * 3600), "L": (6, 4 * 3600),
                 "M": (8, 3600), "S": (16, 900)}
TIER_NAMES = {"G": "giant", "L": "large", "M": "medium", "S": "small"}


def balanced(repos, n, weight):
    """Split repos into n batches of near-equal total weight (largest first,
    each to the lightest batch so far)."""
    n = max(1, min(n, len(repos)))
    heap = [(0, i) for i in range(n)]
    batches = [[] for _ in range(n)]
    for r in sorted(repos, key=weight, reverse=True):
        w, i = heapq.heappop(heap)
        batches[i].append(r)
        heapq.heappush(heap, (w + weight(r), i))
    # heaviest batch first, so G01 / L01 are the biggest of their tier
    return sorted((b for b in batches if b),
                  key=lambda b: -sum(map(weight, b)))


def dealt(repos, size, weight):
    """Cut repos into batches of about `size`, dealt round-robin in weight
    order so every batch gets the same mix."""
    if not repos:
        return []
    n = max(1, -(-len(repos) // size))
    batches = [[] for _ in range(n)]
    for i, r in enumerate(sorted(repos, key=weight, reverse=True)):
        batches[i % n].append(r)
    return batches


def hms(sec):
    return "%dh" % (sec // 3600) if sec >= 3600 else "%dm" % (sec // 60)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="repo_extension_summary CSV (one row per repo)")
    ap.add_argument("--out", default="batches", help="output folder (default batches)")
    ap.add_argument("--weight", default="listed_commits",
                    help="column used as a repo's work (default listed_commits)")
    ap.add_argument("--statuses", default="ok,only_git_listed",
                    help="repo_status values to include (default ok,"
                         "only_git_listed - history mode reads .git, so repos "
                         "whose inventory listed only .git files still count)")
    ap.add_argument("--giant", type=int, default=25,
                    help="how many of the heaviest repos are giant (default 25)")
    ap.add_argument("--large", type=int, default=500,
                    help="rank where large ends (default 500)")
    ap.add_argument("--medium", type=int, default=2000,
                    help="rank where medium ends (default 2000)")
    ap.add_argument("--giant-batches", type=int, default=5)
    ap.add_argument("--large-batches", type=int, default=8)
    ap.add_argument("--medium-batches", type=int, default=4)
    ap.add_argument("--small-size", type=int, default=2000,
                    help="repos per small batch (default 2000)")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        sys.exit("not a file: " + args.csv_in)
    if not args.giant <= args.large <= args.medium:
        sys.exit("need --giant <= --large <= --medium")
    statuses = {s.strip() for s in args.statuses.split(",") if s.strip()}

    with open(args.csv_in, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit("empty file: " + args.csv_in)
    need = {"org", "repo", "repo_status", args.weight}
    missing = need - set(rows[0])
    if missing:
        sys.exit("missing column(s) %s" % sorted(missing))

    def weight(r):
        try:
            return int(r[args.weight] or 0)
        except ValueError:
            return 0

    def files(r):
        try:
            return int(r.get("listed_files") or 0)
        except ValueError:
            return 0

    rows = [r for r in rows if r["org"] != "(all)"]
    keep = [r for r in rows if r["repo_status"] in statuses]
    excluded = [r for r in rows if r["repo_status"] not in statuses]
    keep.sort(key=lambda r: (-weight(r), r["org"], r["repo"]))

    tiers = {"G": keep[:args.giant],
             "L": keep[args.giant:args.large],
             "M": keep[args.large:args.medium],
             "S": keep[args.medium:]}
    plan = []
    for t, n in (("G", args.giant_batches), ("L", args.large_batches),
                 ("M", args.medium_batches)):
        plan += [(t, b) for b in balanced(tiers[t], n, weight)]
    plan += [("S", b) for b in dealt(tiers["S"], args.small_size, weight)]

    os.makedirs(args.out, exist_ok=True)
    total_w = sum(map(weight, keep)) or 1
    seq = {}
    summary = []
    for t, batch in plan:
        seq[t] = seq.get(t, 0) + 1
        name = "%s%02d" % (t, seq[t])
        with open(os.path.join(args.out, name + ".csv"), "w", newline="",
                  encoding="utf-8", errors="surrogateescape") as fh:
            w = csv.writer(fh)
            w.writerow(["org", "repo", "tier", "weight", "listed_files"])
            for r in sorted(batch, key=weight, reverse=True):
                w.writerow([r["org"], r["repo"], TIER_NAMES[t], weight(r),
                            files(r)])
        bw = sum(map(weight, batch))
        top = max(batch, key=weight)
        workers, timeout = TIER_SETTINGS[t]
        summary.append([name, TIER_NAMES[t], len(batch), bw,
                        "%.2f" % (100.0 * bw / total_w),
                        sum(map(files, batch)),
                        "%s/%s" % (top["org"], top["repo"]), weight(top),
                        workers, timeout])

    with open(os.path.join(args.out, "plan.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["batch", "tier", "repos", "weight", "weight_pct", "files",
                    "heaviest_repo", "heaviest_weight", "suggested_workers",
                    "suggested_repo_timeout_s"])
        w.writerows(summary)
    with open(os.path.join(args.out, "excluded.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "repo_status"])
        for r in sorted(excluded, key=lambda r: (r["org"], r["repo"])):
            w.writerow([r["org"], r["repo"], r["repo_status"]])

    print("%s repo(s) in %d batch(es), %s excluded -> %s"
          % (f"{len(keep):,}", len(summary), f"{len(excluded):,}", args.out))
    print("\n%-6s %-7s %7s %13s %7s  %-9s %s"
          % ("batch", "tier", "repos", "weight", "share", "settings",
             "heaviest repo"))
    for name, tier, n, bw, pct, _f, top, tw, wk, to in summary:
        print("%-6s %-7s %7s %13s %6s%%  %2dw %4s  %s (%s)"
              % (name, tier, f"{n:,}", f"{bw:,}", pct, wk, hms(to), top,
                 f"{tw:,}"))


if __name__ == "__main__":
    main()
