#!/usr/bin/env python3
"""
group4_7_9_group2_stats.py - per type numbers for the group 4.7+4.9 group 2
(whole file) delivery: what history and today's copy gave, and what landed
on the share.

  history   --binary (extract_versions.py --out): manifest.csv, one row per
            version -> repos, paths, versions, versions by action
  today     --active (active_files.py --out): manifest.csv, one row per file
            at head -> repos, files, files by action
  stored    files under each store's files/<ext>/ (count, bytes)
  share     files under --share (the merged folder): count, bytes, and
            whether it holds exactly the union of both stores (by name)

Output, under --out:
  by_type.csv   one row per type (CRLF), plus a TOTAL row
  summary.md    totals, then the table, then any type where share != source

    python3 group4_7_9_group2_stats.py --binary $G/group2_binary \\
        --active $G/group2_active \\
        --share /home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_7_9/group2_binary \\
        --out $G/group2_copy_stats
"""

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

H_ACTIONS = ["written", "deduped", "lfs_stub", "empty", "missing_object",
             "hash_mismatch", "skipped_vendored", "too_large"]
A_ACTIONS = ["written", "deduped", "in_stored", "empty", "gone", "unreadable"]
GB = 1e9


def store(root):
    """{ext: {name: bytes}} of root/files/<ext>/<ab>/<name>."""
    out = defaultdict(dict)
    base = os.path.join(root, "files")
    for ext in sorted(os.listdir(base)):
        d = os.path.join(base, ext)
        if not os.path.isdir(d):
            continue
        for sub, _dirs, files in os.walk(d):
            for f in files:
                if ".tmp." not in f:
                    out[ext][f] = os.path.getsize(os.path.join(sub, f))
    return out


def manifest(path):
    csv.field_size_limit(sys.maxsize)
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        return list(csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", required=True, help="extract_versions.py --out")
    ap.add_argument("--active", required=True, help="active_files.py --out")
    ap.add_argument("--share", required=True,
                    help="the merged folder on the share (holds files/)")
    ap.add_argument("--out", required=True, help="folder for the reports")
    args = ap.parse_args()

    hist, act = manifest(os.path.join(args.binary, "manifest.csv")), \
        manifest(os.path.join(args.active, "manifest.csv"))
    print("reading the stores ...", file=sys.stderr, flush=True)
    s_bin, s_act, s_share = store(args.binary), store(args.active), store(args.share)

    h = defaultdict(lambda: {"repos": set(), "paths": set(), "act": Counter()})
    for r in hist:
        x = h[r["ext"]]
        x["repos"].add((r["org"], r["repo"]))
        x["paths"].add((r["org"], r["repo"], r["path"]))
        x["act"][r["action"]] += 1
    a = defaultdict(lambda: {"repos": set(), "act": Counter()})
    for r in act:
        x = a[r["ext"]]
        x["repos"].add((r["org"], r["repo"]))
        x["act"][r["action"]] += 1

    cols = (["ext", "hist_repos", "hist_paths", "hist_versions"]
            + ["hist_" + k for k in H_ACTIONS]
            + ["today_repos", "today_files"] + ["today_" + k for k in A_ACTIONS]
            + ["stored_history", "stored_today", "stored_both_stores",
               "source_files", "source_bytes", "share_files", "share_bytes",
               "share_missing", "share_extra", "share_ok"])
    rows, tot = [], Counter()
    exts = sorted(set(h) | set(a) | set(s_bin) | set(s_act) | set(s_share))
    for e in exts:
        hx, ax = h[e], a[e]
        src = dict(s_bin.get(e, {}))
        src.update(s_act.get(e, {}))
        sh = s_share.get(e, {})
        miss = len(set(src) - set(sh))
        extra = len(set(sh) - set(src))
        r = {"ext": e, "hist_repos": len(hx["repos"]),
             "hist_paths": len(hx["paths"]),
             "hist_versions": sum(hx["act"].values()),
             "today_repos": len(ax["repos"]),
             "today_files": sum(ax["act"].values()),
             "stored_history": len(s_bin.get(e, {})),
             "stored_today": len(s_act.get(e, {})),
             "stored_both_stores": len(set(s_bin.get(e, {})) & set(s_act.get(e, {}))),
             "source_files": len(src), "source_bytes": sum(src.values()),
             "share_files": len(sh), "share_bytes": sum(sh.values()),
             "share_missing": miss, "share_extra": extra,
             "share_ok": "yes" if not miss and not extra
                         and all(sh[k] == src[k] for k in src) else "NO"}
        for k in H_ACTIONS:
            r["hist_" + k] = hx["act"][k]
        for k in A_ACTIONS:
            r["today_" + k] = ax["act"][k]
        rows.append(r)
        for k, v in r.items():
            if isinstance(v, int):
                tot[k] += v
    total = dict(tot, ext="TOTAL",
                 hist_repos=len(set().union(*(x["repos"] for x in h.values()))) if h else 0,
                 today_repos=len(set().union(*(x["repos"] for x in a.values()))) if a else 0,
                 share_ok="yes" if all(r["share_ok"] == "yes" for r in rows) else "NO")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "by_type.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        w.writerows(rows + [total])

    t = total
    lines = [
        "# Group 4.7+4.9 group 2 (whole file): delivery numbers", "",
        "| | |", "|---|---|",
        "| types | %d |" % len(rows),
        "| history: repos / paths / versions | %s / %s / %s |"
        % (f"{t['hist_repos']:,}", f"{t['hist_paths']:,}", f"{t['hist_versions']:,}"),
        "| history versions by action | %s |" % ", ".join(
            "%s %s" % (k, f"{t['hist_' + k]:,}") for k in H_ACTIONS if t["hist_" + k]),
        "| today: repos / files | %s / %s |"
        % (f"{t['today_repos']:,}", f"{t['today_files']:,}"),
        "| today files by action | %s |" % ", ".join(
            "%s %s" % (k, f"{t['today_' + k]:,}") for k in A_ACTIONS if t["today_" + k]),
        "| stored: history + today (in both) | %s + %s (%s) |"
        % (f"{t['stored_history']:,}", f"{t['stored_today']:,}",
           f"{t['stored_both_stores']:,}"),
        "| to send (distinct contents) | %s files, %.3f GB |"
        % (f"{t['source_files']:,}", t["source_bytes"] / GB),
        "| on the share | %s files, %.3f GB |"
        % (f"{t['share_files']:,}", t["share_bytes"] / GB),
        "| share = source (names and sizes) | %s (missing %d, extra %d) |"
        % (t["share_ok"], t["share_missing"], t["share_extra"]),
        "", "| ext | hist repos | hist versions | today files | to send | GB | share | ok |",
        "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in sorted(rows, key=lambda r: -r["source_bytes"]):
        lines.append("| %s | %d | %s | %s | %s | %.3f | %s | %s |" % (
            r["ext"], r["hist_repos"], f"{r['hist_versions']:,}",
            f"{r['today_files']:,}", f"{r['source_files']:,}",
            r["source_bytes"] / GB, f"{r['share_files']:,}", r["share_ok"]))
    bad = [r["ext"] for r in rows if r["share_ok"] != "yes"]
    lines += ["", "Types where the share differs from the source: %s"
              % (", ".join(bad) if bad else "none")]
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines[:15]))
    print("\n-> %s" % os.path.join(args.out, "summary.md"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
