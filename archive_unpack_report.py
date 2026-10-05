#!/usr/bin/env python3
"""
archive_unpack_report.py - what archive_unpack.py found, in one readable
report: per repo how many archive files and versions, how many opened, how
many files were inside and how many distinct; by route (which earlier
group's method the files belong to); by archive type; and why archives were
not opened.

Reads <unpacked>/_state/<org>/<repo>/{archives.csv, manifest.csv, done.json}
of every repo that finished and writes into <unpacked>/stats/:

  archives_per_repo.csv   one row per repo (legend at the end)
  by_route.csv            per route: types, occurrences, distinct, GB
  not_opened.csv          archive versions not opened, by reason and type
  report.md               the overview, top repos, routes, reasons

Usage:
    python3 archive_unpack_report.py /data/workarea/group4_2/unpacked
    python3 archive_unpack_report.py /data/workarea/group4_2/unpacked --top 30
"""

import argparse
import csv
import datetime
import json
import os
import sys
from collections import Counter, defaultdict

csv.field_size_limit(1 << 30)
REPO_HEADER = ["org", "repo", "archive_files", "archive_versions", "opened",
               "vendored_not_opened", "problem_not_opened", "files_inside",
               "distinct_files_written", "duplicates", "not_stored", "gb_written"]
REPO_LEGEND = [
    ("archive_files", "different archive files (paths) in the repo, history and today"),
    ("archive_versions", "all their versions read: history + today's copy (when it differed)"),
    ("opened", "versions opened (fully or in part)"),
    ("vendored_not_opened", "in node_modules, packages, bin, obj...: listed only"),
    ("problem_not_opened", "password-protected, damaged, no tool, not an archive, over limits"),
    ("files_inside", "files found inside, every occurrence (same file in 3 versions = 3)"),
    ("distinct_files_written", "different contents, each written once per repo"),
    ("duplicates", "occurrences whose content was already written for the repo"),
    ("not_stored", "files inside not written: encrypted, link, too large, error"),
    ("gb_written", "size of the distinct files written"),
]


def rows_of(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def n(x):
    return f"{x:,}"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("unpacked", help="archive_unpack.py --out folder")
    ap.add_argument("--top", type=int, default=20, help="repos in the top lists")
    args = ap.parse_args()
    root = os.path.abspath(args.unpacked)
    state = os.path.join(root, "_state")
    if not os.path.isdir(state):
        sys.exit("no _state folder in %s - give the archive_unpack.py --out folder, "
                 "e.g. /data/workarea/group4_2/unpacked" % root)

    repos, skipped = [], Counter()
    route = defaultdict(Counter)
    route_types = defaultdict(set)
    reasons = Counter()
    reason_type = Counter()
    arch_type = defaultdict(Counter)
    tot = Counter()
    for org in sorted(os.listdir(state)):
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            sd = os.path.join(od, repo)
            try:
                with open(os.path.join(sd, "done.json"), encoding="utf-8") as fh:
                    done = json.load(fh)
            except (OSError, ValueError):
                skipped["no done.json (not finished)"] += 1
                continue
            if done.get("status") != "ok":
                skipped["status " + str(done.get("status"))] += 1
                continue
            paths = set()
            c = Counter()
            for a in rows_of(os.path.join(sd, "archives.csv")):
                paths.add(a["archive_path"])
                c["versions"] += 1
                st = a["status"].split(":")[0]
                ext = os.path.splitext(a["archive_path"])[1].lstrip(".").lower() or "_noext"
                arch_type[ext]["versions"] += 1
                if st == "ok":
                    c["opened"] += 1
                    arch_type[ext]["opened"] += 1
                elif st in ("limit_members", "limit_archive_bytes", "too_deep"):
                    c["opened"] += 1                  # opened in part
                    arch_type[ext]["opened_in_part"] += 1
                    reasons[st] += 1
                    reason_type[(st, ext)] += 1
                elif st == "skipped_vendored":
                    c["vendored"] += 1
                    arch_type[ext]["vendored"] += 1
                else:
                    c["problem"] += 1
                    arch_type[ext]["problem"] += 1
                    reasons[st] += 1
                    reason_type[(st, ext)] += 1
            for r in rows_of(os.path.join(sd, "manifest.csv")):
                if not r["inner_path"] or r["action"] == "opened_nested":
                    continue
                c["inside"] += 1
                rt = r["route"] or "?"
                route[rt]["occurrences"] += 1
                route_types[rt].add(r["ext"] or "_noext")
                if r["action"] == "written":
                    c["written"] += 1
                    b = int(r["bytes"] or 0)
                    c["bytes"] += b
                    route[rt]["distinct"] += 1
                    route[rt]["bytes"] += b
                elif r["action"] == "deduped":
                    c["deduped"] += 1
                else:
                    c["not_stored"] += 1
                    route[rt]["not_stored"] += 1
            row = [org, repo, len(paths), c["versions"], c["opened"], c["vendored"],
                   c["problem"], c["inside"], c["written"], c["deduped"],
                   c["not_stored"], round(c["bytes"] / 1e9, 3)]
            repos.append(row)
            tot.update(c)
            tot["archive_files"] += len(paths)
    if not repos:
        sys.exit("no finished repo found under %s (%s)" % (state, dict(skipped) or "empty"))

    sd = os.path.join(root, "stats")
    os.makedirs(sd, exist_ok=True)
    repos.sort(key=lambda r: (-r[7], r[0], r[1]))
    with open(os.path.join(sd, "archives_per_repo.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REPO_HEADER)
        w.writerows(repos)
        w.writerow([])
        w.writerow(["column", "meaning"])
        w.writerows(REPO_LEGEND)
    with open(os.path.join(sd, "by_route.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["route", "types", "occurrences", "distinct", "gb_distinct",
                    "not_stored", "example_types"])
        for k in sorted(route, key=lambda k: -route[k]["distinct"]):
            ts = sorted(route_types[k])
            w.writerow([k, len(ts), route[k]["occurrences"], route[k]["distinct"],
                        round(route[k]["bytes"] / 1e9, 3), route[k]["not_stored"],
                        " ".join(ts[:15]) + (" ..." if len(ts) > 15 else "")])
    with open(os.path.join(sd, "not_opened.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["reason", "archive_type", "versions"])
        for (st, ext), v in sorted(reason_type.items(), key=lambda kv: -kv[1]):
            w.writerow([st, ext, v])

    # ----------------------------------------------------------- report.md
    buckets = Counter()
    for r in repos:
        k = r[2]
        buckets["1" if k == 1 else "2-10" if k <= 10 else "11-100" if k <= 100
                else "101+"] += 1
    md = ["# Group 4.2 archives - what was inside", "",
          "Generated %s from `%s`." % (datetime.date.today().isoformat(), root), "",
          "## Totals", "",
          "| | |", "|---|---:|",
          "| repos | %s |" % n(len(repos)),
          "| archive files (paths) | %s |" % n(tot["archive_files"]),
          "| archive versions read (history + today) | %s |" % n(tot["versions"]),
          "| opened | %s |" % n(tot["opened"]),
          "| not opened: vendored (listed only) | %s |" % n(tot["vendored"]),
          "| not opened: problem | %s |" % n(tot["problem"]),
          "| **files inside** (every occurrence) | **%s** |" % n(tot["inside"]),
          "| **distinct files written** (once per repo) | **%s** |" % n(tot["written"]),
          "| duplicates (already written for the repo) | %s |" % n(tot["deduped"]),
          "| not stored (encrypted, link, too large, error) | %s |" % n(tot["not_stored"]),
          "| GB written | %.2f |" % (tot["bytes"] / 1e9), ""]
    if skipped:
        md += ["Not counted: %s." % ", ".join("%s %s" % (v, k) for k, v in skipped.items()), ""]
    md += ["## Repos by number of archive files", "", "| archive files | repos |", "|---|---:|"]
    md += ["| %s | %s |" % (k, n(buckets[k])) for k in ("1", "2-10", "11-100", "101+") if buckets[k]]
    md += ["", "## Top %d repos by files inside" % args.top, "",
           "| org/repo | archive files | versions | files inside | distinct | GB |",
           "|---|---:|---:|---:|---:|---:|"]
    for r in repos[:args.top]:
        md.append("| %s/%s | %s | %s | %s | %s | %.2f |"
                  % (r[0], r[1], n(r[2]), n(r[3]), n(r[7]), n(r[8]), r[11]))
    share = sum(r[8] for r in repos[:args.top]) / max(tot["written"], 1)
    md += ["", "These %d repos hold %.0f%% of the distinct files." % (min(args.top, len(repos)),
                                                                    share * 100), ""]
    md += ["## By route (which earlier group's method fits the files)", "",
           "| route | types | occurrences | distinct | GB |", "|---|---:|---:|---:|---:|"]
    for k in sorted(route, key=lambda k: -route[k]["distinct"]):
        md.append("| %s | %d | %s | %s | %.2f |" % (k, len(route_types[k]), n(route[k]["occurrences"]),
                                                 n(route[k]["distinct"]), route[k]["bytes"] / 1e9))
    md += ["", "## By archive type", "",
           "| type | versions | opened | opened in part | vendored | problem |",
           "|---|---:|---:|---:|---:|---:|"]
    for e in sorted(arch_type, key=lambda e: -arch_type[e]["versions"]):
        a = arch_type[e]
        md.append("| %s | %s | %s | %s | %s | %s |" % (e, n(a["versions"]), n(a["opened"]),
                                                    n(a["opened_in_part"]), n(a["vendored"]),
                                                    n(a["problem"])))
    md += ["", "## Why archives were not (fully) opened", "", "| reason | versions |", "|---|---:|"]
    md += ["| %s | %s |" % (k, n(v)) for k, v in reasons.most_common()]
    md += ["", "Details: `stats/not_opened.csv` (by type), `stats/problems.csv` (each archive), "
           "`stats/archives_per_repo.csv` (each repo)."]
    with open(os.path.join(sd, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    # ------------------------------------------------------------- console
    print("repos %s | archive files %s | versions %s | opened %s | vendored %s | problem %s"
          % (n(len(repos)), n(tot["archive_files"]), n(tot["versions"]), n(tot["opened"]),
             n(tot["vendored"]), n(tot["problem"])))
    print("files inside %s | distinct written %s | duplicates %s | not stored %s | %.1f GB"
          % (n(tot["inside"]), n(tot["written"]), n(tot["deduped"]), n(tot["not_stored"]),
             tot["bytes"] / 1e9))
    print("repos by archive files:", {k: buckets[k] for k in ("1", "2-10", "11-100", "101+")
                                      if buckets[k]})
    print("by route:", ", ".join("%s %s" % (k, n(route[k]["distinct"]))
                                 for k in sorted(route, key=lambda k: -route[k]["distinct"])))
    print("not opened:", ", ".join("%s %s" % (k, n(v)) for k, v in reasons.most_common(6)))
    print("\ntop %d repos by files inside:" % min(10, len(repos)))
    for r in repos[:10]:
        print("  %-55s archives %5s  versions %6s  inside %10s  distinct %9s  %7.2f GB"
              % ((r[0] + "/" + r[1])[:55], n(r[2]), n(r[3]), n(r[7]), n(r[8]), r[11]))
    print("\nwritten: %s" % ", ".join(os.path.join(sd, f) for f in
                                       ("report.md", "archives_per_repo.csv", "by_route.csv",
                                        "not_opened.csv")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
