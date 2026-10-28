#!/usr/bin/env python3
"""Exact counts for the process record (QRM), from the LOCAL run outputs on
the server. Read only: nothing is written except the report (stdout).

Per delivery: the data files a copy sends (count, bytes; .txt files and
their lines where cheap), the run's own summary rows (stats/by_type.csv
total row, summary.json, summary.csv totals), and the checks that the
record leaves "to confirm" (failed group 1 repos, case-clash lists, removed
duplicates, top folders on the share). No file content is printed - only
counts, sizes, CSV headers and total rows.

    nohup python3 collect_record_counts.py > /data/workarea/record_counts.txt 2> /data/workarea/record_counts.err &

Walking group 1 (6.8 M files) takes a few minutes; --no-lines skips line
counting (the slow part) for the big text folders.

The '.tmp.' column counts DATA files with ".tmp." in their name (not our
tools' temp files <name>.tmp.<pid>): copy scripts that exclude "*.tmp.*"
would have left those out.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

W = os.environ.get("RECORD_W", "/data/workarea")
P1 = os.environ.get("RECORD_P1", "/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1")
SKIP = {"_state", "_logs", "_tmp", "_not_sent", "_removed_duplicates", "stats", "stats_combined",
        "nested", "copy_checks", "_case"}
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="replace")

FAILED_G1 = ["ey-org/sat-innovation-lab-program-office", "globalpartnerworkloadtoolrevampcloud", "iam-smime",
             "ni-admrt-notification", "aena-assistants-marketplace", "ey-fso-tax/powerbi-reports",
             "2025-q-1-ai-powered-aml", "smt-monitoring", "sdt-automation", "ey-fso-tax/core-pipelines",
             "ey-fso-tax/workmgmt-db"]

# (label, folder, kind) - kind txt = count .txt (+ lines), files = every file
DELIVERIES = [
    ("Group 1 text (delta)", W + "/full_extract_delta_2", "txt"),
    ("Group 2 whole files", W + "/binary_versions/files", "files"),
    ("Group 2 text (delta)", W + "/text9_extract_delta", "txt"),
    ("Group 3 pdf merged", W + "/group3/pdf_merged", "files"),
    ("Group 3 pkl text", W + "/group3/pkl_text", "txt"),
    ("4.5_R text (delta)", W + "/group4_5_R/extract_delta", "txt"),
    ("4.7+4.9 group 1 text", W + "/group4_7_9/group1/text", "txt"),
    ("4.7+4.9 group 1 whole (history)", W + "/group4_7_9/group1/not_text/binary/files", "files"),
    ("4.7+4.9 group 1 whole (today)", W + "/group4_7_9/group1/not_text/active/files", "files"),
    ("4.7+4.9 group 2 whole (history)", W + "/group4_7_9/group2_binary/files", "files"),
    ("4.7+4.9 group 2 whole (today)", W + "/group4_7_9/group2_active/files", "files"),
    ("4.7+4.9 group 3 text", W + "/group4_7_9/group3/text", "txt"),
    ("4.7+4.9 tabular (history)", W + "/group4_7_9/tabular/tabular_binary/files", "files"),
    ("4.7+4.9 tabular (today)", W + "/group4_7_9/tabular/tabular_active/files", "files"),
    ("4.7+4.9 containers text", W + "/group4_7_9/bi/data/data_text", "txt"),
    ("4.7+4.9 containers images", W + "/group4_7_9/bi/data/files", "files"),
    ("4.7+4.9 mixed text", W + "/group4_7_9/mixed/text", "txt"),
    ("4.7+4.9 mixed whole (history)", W + "/group4_7_9/mixed/not_text/binary/files", "files"),
    ("4.7+4.9 mixed whole (today)", W + "/group4_7_9/mixed/not_text/active/files", "files"),
]
INNER_RUNS = [W + "/group4_2", W + "/group4_2_R"]
INNER_STEPS = ["group1_text", "group4_5_R_text", "group4_5_text", "group2_text", "group2_binary",
               "group3_pkl_text", "group3_pdf_merged", "group4_7_9_text", "group4_7_9_binary",
               "group4_7_9_group3_text", "group4_7_9_tabular_binary", "group4_7_9_bi_data",
               "group4_7_9_mixed_text", "group4_7_9_mixed_binary"]


def say(msg=""):
    print(msg, flush=True)


def log(msg):
    sys.stderr.write(time.strftime("%T ") + msg + "\n")
    sys.stderr.flush()


WORKERS = 16


def walk(root, kind, lines):
    """(files, bytes, .txt, lines) under root, one task per repo folder
    (root/<org>/<repo>) on WORKERS processes."""
    tasks, top = [], [0, 0, 0, 0, 0]
    try:
        level1 = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return 0, 0, 0, 0, 0
    for e in level1:
        if e.is_dir(follow_symlinks=False):
            if e.name in SKIP:
                continue
            try:
                for e2 in os.scandir(e.path):
                    if e2.is_dir(follow_symlinks=False):
                        tasks.append(e2.path)
                    elif e2.name not in ("done.json", "manifest.csv"):
                        _count_file(e2.path, e2.name, lines, top)
            except OSError:
                pass
        elif e.name not in ("done.json", "manifest.csv"):
            _count_file(e.path, e.name, lines, top)
    if WORKERS > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(WORKERS) as ex:
            parts = list(ex.map(_walk_one, tasks, [lines] * len(tasks), chunksize=8))
    else:
        parts = [_walk_one(t, lines) for t in tasks]
    for x in parts:
        for i in range(5):
            top[i] += x[i]
    return tuple(top)


TMP = re.compile(r"\.tmp\.(\d+(\.\d+)?|copy)$")    # temp files of our tools: <name>.tmp.<pid>[.<thread>], .tmp.copy


def is_temp(f):
    return f.startswith(".tmp.") or bool(TMP.search(f))


def _count_file(p, f, lines, acc):
    if is_temp(f):
        return
    try:
        acc[1] += os.path.getsize(p)
    except OSError:
        return
    acc[0] += 1
    if f.endswith(".txt"):
        acc[2] += 1
        if lines:
            try:
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        acc[3] += chunk.count(b"\n")
            except OSError:
                pass


def _walk_one(root, lines):
    """everything under a repo folder: tracking folders only exist at the top
    of a delivery folder, so nothing is skipped below it."""
    acc = [0, 0, 0, 0, 0]
    for d, dirs, fs in os.walk(root):
        for f in fs:
            _count_file(os.path.join(d, f), f, lines, acc)
            if ".tmp." in f and not is_temp(f):
                acc[4] += 1
    return acc


def total_row(path):
    """header + 'total' row of a by_type / summary CSV."""
    try:
        rows = list(csv.reader(open(path, **KW)))
    except OSError:
        return None
    if not rows:
        return None
    tot = [r for r in rows[1:] if r and r[0].strip().lower() in ("total", "(all)", "all")]
    return rows[0], (tot[-1] if tot else None), len(rows) - 1


def show_stats(folder, indent="    "):
    for name in ("stats/by_type.csv", "stats_combined/by_type.csv", "summary.csv", "stats/summary.csv"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            t = total_row(p)
            if t:
                h, tot, n = t
                say("%s%s (%d rows)" % (indent, name, n))
                if tot:
                    for k, v in zip(h, tot):
                        if v not in ("", "0", "0.0"):
                            say("%s    %-28s %s" % (indent, k, v))
    for name in ("stats/summary.json", "summary.json"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            try:
                j = json.load(open(p))
            except (OSError, ValueError):
                continue
            flat = {k: v for k, v in j.items() if isinstance(v, (int, float, str)) and len(str(v)) < 60}
            say("%s%s: %s" % (indent, name, json.dumps(flat, sort_keys=True)))


def has_stats(folder):
    names = ("stats/by_type.csv", "stats_combined/by_type.csv", "summary.csv", "stats/summary.csv",
             "stats/summary.json", "summary.json", "manifest.csv")
    return os.path.isdir(folder) and (any(os.path.isfile(os.path.join(folder, n)) for n in names)
                                      or bool(glob.glob(os.path.join(folder, "_state", "*", "*", "manifest.csv"))))


def _one_manifest(m):
    c = Counter()
    try:
        with open(m, **KW) as fh:
            r = csv.DictReader(fh)
            col = "status" if "status" in (r.fieldnames or []) else ("action" if "action" in (r.fieldnames or []) else None)
            if col:
                for row in r:
                    c[row.get(col, "")] += 1
    except OSError:
        pass
    return c


def manifest_status(folder):
    """status / action counts over <folder>/_state/*/*/manifest.csv (or manifest.csv), on WORKERS processes."""
    c = Counter()
    paths = glob.glob(os.path.join(folder, "_state", "*", "*", "manifest.csv"))
    if not paths and os.path.isfile(os.path.join(folder, "manifest.csv")):
        paths = [os.path.join(folder, "manifest.csv")]
    if WORKERS > 1 and len(paths) > 1:
        with ProcessPoolExecutor(WORKERS) as ex:
            for x in ex.map(_one_manifest, paths, chunksize=32):
                c.update(x)
    else:
        for m in paths:
            c.update(_one_manifest(m))
    return len(paths), c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-lines", action="store_true", help="do not count lines in the big text folders")
    ap.add_argument("--workers", type=int, default=16, help="processes for walking / reading manifests (default 16)")
    a = ap.parse_args()
    global WORKERS
    WORKERS = max(1, a.workers)
    say("# Record counts, %s (local outputs on the server; read only)" % time.strftime("%F %T"))

    # ---------------------------------------------------------- A. repositories
    say("\n## A. Repositories")
    bdir = W + "/file_history_out_4/batches"
    repos, tiers, nb = set(), Counter(), 0
    for p in sorted(glob.glob(bdir + "/*.csv")):
        base = os.path.basename(p)
        if base in ("plan.csv", "excluded.csv"):
            continue
        nb += 1
        for r in csv.DictReader(open(p, **KW)):
            repos.add((r.get("org"), r.get("repo")))
            tiers[base[0]] += 1
    say("  batches: %d repos in %d batch files; by tier %s" % (len(repos), nb, dict(tiers)))
    ex = bdir + "/excluded.csv"
    if os.path.isfile(ex):
        rows = list(csv.DictReader(open(ex, **KW)))
        say("  excluded.csv: %d rows; status %s" % (len(rows), dict(Counter(r.get("repo_status", "") for r in rows))))
    mc = W + "/missing_repos_check.csv"
    if os.path.isfile(mc):
        rows = list(csv.DictReader(open(mc, **KW)))
        say("  missing_repos_check.csv: %d rows; columns %s" % (len(rows), list(rows[0].keys()) if rows else []))
        for k in (rows[0].keys() if rows else []):
            vals = Counter(r[k] for r in rows)
            if 1 < len(vals) <= 8:
                say("    %s: %s" % (k, dict(vals)))
    say("  archive repos on disk: %d" % len(glob.glob(W + "/archive/*/*")))

    # ---------------------------------------------------------- B. all types
    say("\n## B. All file types in history")
    ec = W + "/all_ext_counts/extension_counts.csv"
    if os.path.isfile(ec):
        rows = list(csv.DictReader(open(ec, **KW)))
        sums = Counter()
        for r in rows:
            for k in ("files", "versions", "distinct_versions"):
                try:
                    sums[k] += int(r.get(k) or 0)
                except ValueError:
                    pass
        say("  extension_counts.csv: %d types; %s; by group %s" % (len(rows), dict(sums),
            dict(Counter(r.get("group", "") for r in rows))))

    # ---------------------------------------------------------- C. deliveries
    say("\n## C. Deliveries: data files the copies send (local source)")
    say("  %-34s %10s %16s %10s %14s %9s  folder" % ("delivery", "files", "bytes", ".txt", "lines", "'.tmp.'"))
    for label, folder, kind in DELIVERIES:
        if not os.path.isdir(folder):
            say("  %-34s %s" % (label, "(not found: %s)" % folder))
            continue
        log("walking " + folder)
        big = folder.endswith(("full_extract_delta_2", "data_text"))
        n, b, txt, ln, tn = walk(folder, kind, lines=(kind == "txt" and not (a.no_lines and big)))
        say("  %-34s %10d %16d %10d %14s %9d  %s" % (label, n, b, txt, ln if kind == "txt" and not (a.no_lines and big) else "-", tn, folder))
    say("\n  run summaries:")
    for label, folder, kind in DELIVERIES:
        if not os.path.isdir(folder):
            continue
        base = folder[:-len("/files")] if folder.endswith("/files") else folder
        for f in (base, os.path.dirname(base)):
            if not has_stats(f):
                continue
            say("  [%s] %s" % (label, f))
            show_stats(f)
            fs, c = manifest_status(f)
            if c:
                say("    manifests: %d; status/action %s" % (fs, dict(c)))
            break

    # ---------------------------------------------------------- D. inside archives
    say("\n## D. Inside the 4.2 / 4.2_R archives")
    for run in INNER_RUNS:
        for step in INNER_STEPS:
            f = os.path.join(run, step)
            if not os.path.isdir(f):
                continue
            log("walking " + f)
            n, b, txt, ln, tn = walk(f, "txt", lines=True)
            say("  %-12s %-28s files %8d  bytes %14d  .txt %8d  lines %12d  '.tmp.' names %d" % (os.path.basename(run), step, n, b, txt, ln, tn))
            show_stats(f, indent="      ")
        un = os.path.join(run, "unpacked")
        if os.path.isdir(un):
            say("  %s unpacked:" % os.path.basename(run))
            show_stats(un, indent="      ")
    for p in (W + "/group4_2/group4_1_paths/group4_1_inner_paths.csv",):
        if os.path.isfile(p):
            say("  %s: %d rows" % (p, sum(1 for _ in open(p, **KW)) - 1))

    # ---------------------------------------------------------- E. path lists
    say("\n## E. Path lists (4.1, 4.1_R)")
    for p in sorted(set(glob.glob(W + "/group4_1*/**/*paths*.csv", recursive=True))):
        try:
            n = sum(1 for _ in open(p, **KW)) - 1
        except OSError:
            continue
        say("  %-90s %d rows" % (p, n))

    # ---------------------------------------------------------- F. checks
    say("\n## F. Checks for the record")
    say("  Group 1 failed repos (1 Oct): done.json status in each text run")
    for rp in FAILED_G1:
        hits = []
        for run in ("full_extract", "full_extract_delta_2"):
            pat = os.path.join(W, run, "_state", "*" if "/" not in rp else rp.split("/")[0],
                               rp.split("/")[-1], "done.json")
            for dj in glob.glob(pat):
                try:
                    st = json.load(open(dj)).get("status", "?")
                except (OSError, ValueError):
                    st = "unreadable"
                hits.append("%s=%s" % (run, st))
        say("    %-45s %s" % (rp, ", ".join(hits) or "no done.json"))
    say("  case-clash / name-problem lists:")
    for p in sorted(glob.glob(W + "/**/case_renamed*.csv", recursive=True) + glob.glob(W + "/**/name_problems*.csv", recursive=True)):
        if "/archive/" in p:
            continue
        try:
            n = sum(1 for _ in open(p, **KW)) - 1
        except OSError:
            continue
        say("    %-90s %d rows" % (p, n))
    say("  removed duplicates still on disk:")
    for p in glob.glob(W + "/group4_7_9/**/_removed_duplicates", recursive=True) + glob.glob(W + "/group4_2*/**/_removed_duplicates", recursive=True):
        tot = 0
        cnt = 0
        for d, _, fs in os.walk(p):
            for f in fs:
                try:
                    tot += os.path.getsize(os.path.join(d, f))
                    cnt += 1
                except OSError:
                    pass
        say("    %-80s %d files, %.1f GiB" % (p, cnt, tot / 2 ** 30))
    say("  gitleaks outputs (pii_secret_scan.py): %s" % (glob.glob(W + "/*gitleaks*") + glob.glob(W + "/*secret*") or "none found"))

    # ---------------------------------------------------------- G. share (top level only, cheap)
    say("\n## G. Share: top-level folders (no counting on the share)")
    if os.path.isdir(P1):
        for d in sorted(os.listdir(P1)):
            sub = os.path.join(P1, d)
            if os.path.isdir(sub):
                try:
                    kids = sorted(os.listdir(sub))
                except OSError:
                    kids = ["(unreadable)"]
                say("  %-32s %s" % (d, ", ".join(kids[:15]) + (" ..." if len(kids) > 15 else "")))
    else:
        say("  share not mounted: %s" % P1)
    say("\n# end")


if __name__ == "__main__":
    main()
