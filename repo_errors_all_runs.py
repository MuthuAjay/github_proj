#!/usr/bin/env python3
"""READ ONLY. Every repo that errored in any run, from the start: the scope
step (repos left out of the batches), then each run's per-repo log and state,
and whether a failed repo came right later - in the same run (rerun) or in
another run (e.g. full_extract -> full_extract_delta_2).

    nohup python3 repo_errors_all_runs.py --out ~/repo_errors > ~/repo_errors.log 2>&1 &

Sources (under --w, default /data/workarea; data folders are not walked):
  scope   file_history_out_4/batches/excluded.csv (make_batches: repos left
          out, repo_status) and missing_repos_check.csv (MISSING / FOUND)
  logs    every <run>/_logs/<name>_repos.csv: one row per repo per attempt
          (finished, org, repo, status, ..., error), appended - the history,
          a repo fixed on a rerun has its failed row and its ok row
  state   every <run>/_state/<org>/<repo>/done.json: the repo's last status

A status other than ok (error, timeout, repo_error, failed, no_pass1,
no_pass2, skipped_low_disk, ...) is a failure; no_extraction (nothing to do)
and dry_run are not.

Writes to --out (CRLF):
  summary.txt          per run: repos tried, failed at some point, failed at
                       the end, by status; the scope step's counts
  repo_failures.csv    run, org, repo, statuses (in order), attempts,
                       last_status, last_error, fixed_in_run (yes/no),
                       ok_in_other_runs, source
  still_failed.csv     repos whose last status in their run is not ok and
                       that are not ok in any other run either
  scope_excluded.csv   the scope step's left-out repos
"""
import argparse
import csv
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")
OKISH = {"ok", "no_extraction", "dry_run"}
PRUNE = {"files", "unpacked", "_removed_duplicates", "_not_sent", "_tmp", "_state",
         "archive", "before", "before_out"}


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def find_runs(w, max_depth=6):
    """folders that hold _logs and/or _state, without walking the data."""
    logs, states = [], []
    base = w.rstrip("/").count("/")
    for dp, dns, _ in os.walk(w):
        if "_logs" in dns:
            logs.append(os.path.join(dp, "_logs"))
        if "_state" in dns:
            states.append(os.path.join(dp, "_state"))
        depth = dp.count("/") - base
        # a run folder's <org>/<repo>/... data folders have the same names as
        # its _state/<org> folders: skip them, keep walking step folders
        orgs = set()
        if "_state" in dns:
            try:
                orgs = set(os.listdir(os.path.join(dp, "_state")))
            except OSError:
                pass
        dns[:] = [] if depth >= max_depth else \
            [d for d in dns if d not in PRUNE and d not in orgs and d != "_logs" and not d.startswith(".")]
    return sorted(logs), sorted(states)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", default="/data/workarea")
    ap.add_argument("--out", default=os.path.expanduser("~/repo_errors"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    W = a.w.rstrip("/")
    rel = lambda p: os.path.relpath(p, W)

    say("finding run folders under %s" % W)
    logs, states = find_runs(W)
    say("  %d _logs, %d _state folders" % (len(logs), len(states)))

    hist = defaultdict(list)     # (run, org, repo) -> [(finished, status, error, source)]
    for ld in logs:
        run = rel(os.path.dirname(ld))
        for f in sorted(glob.glob(os.path.join(ld, "*_repos.csv"))):
            try:
                with open(f, **KW) as fh:
                    rd = csv.DictReader(fh)
                    if not rd.fieldnames or not {"org", "repo", "status"} <= set(rd.fieldnames):
                        continue
                    for r in rd:
                        hist[(run, r["org"], r["repo"])].append(
                            (r.get("finished", ""), r["status"], (r.get("error") or "")[:300], rel(f)))
            except OSError as exc:
                say("  cannot read %s: %s" % (f, exc))
    say("  logs: %d run x repo" % len(hist))

    last = {}                    # (run, org, repo) -> done.json status
    for sd in states:
        run = rel(os.path.dirname(sd))
        for dj in glob.glob(os.path.join(glob.escape(sd), "*", "*", "done.json")):
            org, repo = dj.split("/")[-3:-1]
            try:
                rec = json.load(open(dj, encoding="utf-8", errors="replace"))
                st, err = rec.get("status", "no status"), str(rec.get("error", ""))[:300]
            except (OSError, ValueError) as exc:
                st, err = "unreadable done.json", str(exc)
            last[(run, org, repo)] = (st, err)
    say("  state: %d run x repo" % len(last))

    keys = set(hist) | set(last)
    ok_runs = defaultdict(set)   # (org, repo) -> runs where its last status is ok
    final = {}
    for k in keys:
        h = sorted(hist.get(k, []))
        st, err = last.get(k) or ((h[-1][1], h[-1][2]) if h else ("?", ""))
        final[k] = (st, err)
        if st in OKISH:
            ok_runs[(k[1], k[2])].add(k[0])

    rows, per_run = [], defaultdict(Counter)
    for k in sorted(keys):
        h = sorted(hist.get(k, []))
        sts = [x[1] for x in h]
        st, err = final[k]
        per_run[k[0]]["repos"] += 1
        bad_ever = any(s not in OKISH for s in sts) or st not in OKISH
        if not bad_ever:
            continue
        per_run[k[0]]["failed at some point"] += 1
        if st not in OKISH:
            per_run[k[0]]["failed at the end"] += 1
            per_run[k[0]]["end: " + st] += 1
        others = sorted(ok_runs[(k[1], k[2])] - {k[0]})
        err = err or next((x[2] for x in reversed(h) if x[2]), "")
        rows.append([k[0], k[1], k[2], " > ".join(sts) or "(state only)", len(sts), st, err,
                     "yes" if st in OKISH else "no", " ".join(others),
                     h[-1][3] if h else "_state/done.json"])

    head = ["run", "org", "repo", "statuses", "attempts", "last_status", "last_error",
            "fixed_in_run", "ok_in_other_runs", "source"]
    with open(os.path.join(a.out, "repo_failures.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(head)
        w.writerows(rows)
    still = [r for r in rows if r[7] == "no" and not r[8]]
    with open(os.path.join(a.out, "still_failed.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(head)
        w.writerows(still)

    # ------------------------------------------------------------ scope step
    scope = []
    exc = os.path.join(W, "file_history_out_4", "batches", "excluded.csv")
    if os.path.isfile(exc):
        with open(exc, **KW) as fh:
            scope = list(csv.DictReader(fh))
    mc = os.path.join(W, "missing_repos_check.csv")
    mrows = []
    if os.path.isfile(mc):
        with open(mc, **KW) as fh:
            mrows = list(csv.DictReader(fh))
    if scope:
        with open(os.path.join(a.out, "scope_excluded.csv"), "w", **KW) as fh:
            w = csv.writer(fh, lineterminator="\r\n")
            w.writerow(list(scope[0].keys()))
            w.writerows([list(r.values()) for r in scope])

    with open(os.path.join(a.out, "summary.txt"), "w", newline="\r\n") as fh:
        def out(s=""):
            print(s)
            fh.write(s + "\n")
        out("=== scope step (before any extraction)")
        out("  %s: %d repos left out, repo_status %s" % (rel(exc), len(scope),
            dict(Counter(r.get("repo_status", "") for r in scope)) if scope else "(not found)"))
        if mrows:
            sc = [c for c in mrows[0] if c.lower() in ("status", "check", "result")]
            out("  %s: %d rows%s" % (rel(mc), len(mrows),
                (", " + str(dict(Counter(r[sc[0]] for r in mrows)))) if sc else ""))
        out()
        out("=== per run: repos, failed at some point, still failed at the end (by status)")
        for run in sorted(per_run):
            c = per_run[run]
            if not c["failed at some point"]:
                continue
            ends = ", ".join("%s %d" % (k[5:], v) for k, v in sorted(c.items()) if k.startswith("end: "))
            out("  %-55s repos %7d  failed %5d  at the end %5d  %s"
                % (run, c["repos"], c["failed at some point"], c["failed at the end"], ends))
        clean = sum(1 for r in per_run if not per_run[r]["failed at some point"])
        out("  (%d runs with no failure not shown)" % clean)
        out()
        out("repos failed at some point (run x repo): %d" % len(rows))
        out("  fixed in the same run (rerun ok):        %d" % sum(1 for r in rows if r[7] == "yes"))
        out("  not fixed there, but ok in another run:  %d" % sum(1 for r in rows if r[7] == "no" and r[8]))
        out("  still failed everywhere:                 %d  (distinct repos %d)"
            % (len(still), len({(r[1], r[2]) for r in still})))
    say("done -> %s" % a.out)


if __name__ == "__main__":
    main()
