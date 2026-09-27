#!/usr/bin/env python3
"""
history_only_files.py - for every extracted file that no longer exists in
the active copy (at_head = no in file_added_lines.py's manifests), where it
went: when it appeared, when it last changed, which commit removed or renamed
it, and which branches still hold it.

Per file, one CSV row:

  category             own_code, or vendored (third-party / generated code in
                       node_modules, site-packages, packages, vendor, bin,
                       obj, dist, build, ...). Vendored files are listed but
                       NOT looked up - their history columns stay empty
  reason               still_on_branch    a branch tip still contains it
                       renamed            its last event is a rename; see
                                          renamed_to
                       deleted            its last event is a deletion
                       orphaned           in history, but no branch tip holds
                                          it and no deletion or rename was
                                          seen (commits only reachable from
                                          deleted branches or the reflog)
                       vendored           not looked up
  first_commit/date    the commit that first added the path
  last_change_commit/date
                       the last commit that added or modified it
  removed_commit/date/message
                       the last commit that deleted or renamed it
  renamed_to           for renames: the new path, and renamed_to_exists_today
                       (yes/no, from the same manifest's at_head)
  branches             branch tips that still contain it ("|"-joined)
  versions             commits that touched it (from the manifest)
  renames_checked      no when the repo ran with --no-renames (a rename then
                       shows as deleted)

No author names or emails are read or written. Dates are commit dates.

History is read the way the extraction read it - every branch, merges
against their first parent - with one `git log --raw` per repo: the list of
changed paths only, no file content, and vendored folders excluded up front.
Branch tips are listed with `git ls-tree`; a repo with more than
--max-branches branches has only its default branch and release* branches
checked (recorded in its done.json). Repos git cannot open (objects only)
are read through a throwaway repo that borrows their object store; they have
no branch names, so their files are never still_on_branch.

Output, under --out:
  _state/<org>/<repo>/rows.csv, done.json   per repo (done.json last -> resume)
  _logs/<name>.log, <name>_repos.csv        run log, one row per repo
  history_only_files.csv                    every repo's rows combined
  summary.md                                totals by reason and category,
                                            removals by year, the top mass
                                            removal commits, the top repos
  top_removal_commits.csv, by_repo.csv, by_year.csv

The combined files are rebuilt from every finished repo at the end of each
run (--combine-only rebuilds them without processing anything).

Usage:
    python3 history_only_files.py /data/workarea/full_extract \\
        --repos-root /data/workarea/archive --out /data/workarea/history_only \\
        --batch batches/S01.csv --workers 16
    # the giant repos: rename detection off
    python3 history_only_files.py /data/workarea/full_extract \\
        --repos-root /data/workarea/archive --out /data/workarea/history_only \\
        --batch batches/G01.csv --workers 2 --no-renames
"""

import argparse
import csv
import datetime
import heapq
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

from explain_file_counts import VENDOR_DIRS, vendor_of
from explore_input_csv import is_repo_dir
from extract_commits import (FS, GIT, bind_job, branch_refs, live_refs,
                             parse_log_stream, spawn)
from extract_commits import _nul_tokens as nul_tokens
from file_history_for_list import RecoveredRepo, RepoJob, git_can_open
from repo_extension_summary import load_extensions

STATE, LOGS = "_state", "_logs"
ROW_HEADER = ["org", "repo", "path", "category", "vendor_folder", "reason",
              "first_commit", "first_date", "last_change_commit",
              "last_change_date", "removed_commit", "removed_date",
              "removed_message", "renamed_to", "renamed_to_exists_today",
              "branches", "versions", "renames_checked"]
log = logging.getLogger("history_only_files")


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def pathspecs(exts):
    """The extensions at any depth, case-insensitive, minus the vendored
    folders (their files are not looked up, so git need not diff them)."""
    specs = [":(glob,icase)**/*." + e for e in sorted(exts)]
    specs += [":(exclude,glob,icase)**/%s/**" % d for d in VENDOR_DIRS]
    return specs


def _feed(stdin, specs):
    try:
        stdin.write(b"--\n")
        for p in specs:
            stdin.write(p.encode("utf-8", "surrogateescape") + b"\n")
        stdin.close()
    except (BrokenPipeError, OSError):
        pass


def read_history(gp, own, exts, renames, job):
    """One pass over every branch's history. -> {path: record} for the paths
    in `own`: first / last_change / removed (sha, date[, subject]) and, for
    a rename, the new path."""
    rec = {}

    def get(p):
        r = rec.get(p)
        if r is None:
            r = rec[p] = {"first": None, "last": None, "removed": None,
                          "renamed_to": "", "last_event": ""}
        return r

    def keep(status, path, old_path):
        return path in own or (old_path and old_path in own)

    cmd = GIT + ["-c", "core.quotePath=false", "-C", gp, "log", "--all",
                 "--stdin", "--full-history", "--topo-order", "--reverse",
                 "--raw", "-z", "--no-abbrev", "--diff-merges=first-parent",
                 "-M" if renames else "--no-renames",
                 "--format=%x1e%H" + FS + "%cI" + FS + FS + "%s"]
    job.phase = "reading history"
    with tempfile.TemporaryFile() as err:
        proc = spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=err)
        feeder = threading.Thread(target=_feed,
                                  args=(proc.stdin, pathspecs(exts)),
                                  daemon=True)
        feeder.start()
        try:
            for sha, info, changes in parse_log_stream(proc.stdout, keep,
                                                       meta=True):
                date, _author, subject = info
                for c in changes:
                    st, p, old = c["status"][:1], c["path"], c["old_path"]
                    if st == "R":
                        if old in own:
                            r = get(old)
                            r["removed"] = (sha, date, subject)
                            r["renamed_to"] = p
                            r["last_event"] = "R"
                        if p in own:
                            r = get(p)
                            r["first"] = r["first"] or (sha, date)
                            r["last"] = (sha, date)
                            r["last_event"] = "A"
                    elif st == "D":
                        if p in own:
                            r = get(p)
                            r["removed"] = (sha, date, subject)
                            r["last_event"] = "D"
                    elif p in own:                  # A, M, T, C
                        r = get(p)
                        r["first"] = r["first"] or (sha, date)
                        r["last"] = (sha, date)
                        r["last_event"] = "A"
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
            feeder.join(timeout=1)
        if job.expired:
            raise TimeoutError("timed out while reading history")
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError("git log failed: "
                               + err.read().decode("utf-8", "replace")[:300])
    return rec


def default_branch(gp):
    p = subprocess.run(GIT + ["-C", gp, "symbolic-ref", "-q", "HEAD"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ref = p.stdout.decode().strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""


def branch_holdings(gp, own, max_branches, job):
    """{path: [branch names whose tip tree contains it]} for paths in `own`,
    plus (branches checked, branches total)."""
    branches = branch_refs(live_refs(gp))       # {name: (ref, tip)}
    total = len(branches)
    names = sorted(branches)
    if total > max_branches:
        dflt = default_branch(gp)
        names = [n for n in names
                 if n == dflt or n.endswith("/" + dflt) and dflt
                 or "release" in n.lower()]
    held = defaultdict(list)
    for i, name in enumerate(names, 1):
        job.phase = "branch tips %d/%d" % (i, len(names))
        ref = branches[name][0]
        with tempfile.TemporaryFile() as err:
            proc = spawn(GIT + ["-c", "core.quotePath=false", "-C", gp,
                                "ls-tree", "-r", "-z", "--name-only", ref],
                         stdout=subprocess.PIPE, stderr=err)
            try:
                for tok in nul_tokens(proc.stdout):
                    p = tok.decode("utf-8", "surrogateescape")
                    if p in own:
                        held[p].append(name)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stdout.close()
                proc.wait()
        if job.expired:
            raise TimeoutError("timed out while listing branch tips")
    return held, len(names), total


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def load_manifest(src, org, repo):
    """-> (history-only rows [(path, versions)], {path: at_head})."""
    mp = os.path.join(src, STATE, org, repo, "manifest.csv")
    hist, at_head = [], {}
    with open(mp, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            at_head[r["path"]] = r.get("at_head", "")
            if r.get("output") and r.get("at_head") == "no":
                hist.append((r["path"], r.get("versions", "")))
    return hist, at_head


def csv_text(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape",
              newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def process_repo(cfg, org, repo, job):
    hist, at_head = load_manifest(cfg.src, org, repo)
    info = {"branches_checked": 0, "branches_total": 0, "recovered": False}
    own = {p for p, _v in hist if not vendor_of(p)}
    records, held = {}, {}
    if own:
        rp = os.path.join(cfg.repos_root, org, repo)
        if not is_repo_dir(rp):
            raise RuntimeError("no repository at " + rp)
        recovery = None if git_can_open(rp) else RecoveredRepo(rp)
        try:
            if recovery:
                info["recovered"] = True
                job.phase = "recovering repo (listing objects)"
                recovery.__enter__()
                recovery.add_all_commits_as_refs()
                gp = recovery.tmp
            else:
                gp = rp
            records = read_history(gp, own, cfg.exts, cfg.renames, job)
            held, info["branches_checked"], info["branches_total"] = \
                branch_holdings(gp, own, cfg.max_branches, job)
        finally:
            if recovery:
                recovery.__exit__(None, None, None)

    rows = []
    for p, versions in hist:
        v = vendor_of(p)
        if v:
            rows.append([org, repo, p, "vendored", v, "vendored"]
                        + [""] * 10 + [versions, ""])
            continue
        r = records.get(p) or {"first": None, "last": None, "removed": None,
                               "renamed_to": "", "last_event": ""}
        br = held.get(p, [])
        if br:
            reason = "still_on_branch"
        elif r["last_event"] == "R":
            reason = "renamed"
        elif r["last_event"] == "D":
            reason = "deleted"
        else:
            reason = "orphaned"
        first = r["first"] or ("", "")
        last = r["last"] or ("", "")
        rem = r["removed"] or ("", "", "")
        to = r["renamed_to"] if reason == "renamed" else ""
        rows.append([org, repo, p, "own_code", "", reason,
                     first[0], first[1], last[0], last[1],
                     rem[0], rem[1], rem[2].replace("\n", " ")[:300],
                     to, at_head.get(to, "") if to else "",
                     "|".join(br), versions,
                     "yes" if cfg.renames else "no"])
    return rows, info


def run_repo(cfg, org, repo, job):
    job.start = time.time()
    bind_job(job)
    sd = os.path.join(cfg.out, STATE, org, repo)
    shutil.rmtree(sd, ignore_errors=True)
    try:
        rows, info = process_repo(cfg, org, repo, job)
        status, error = "ok", ""
    except Exception as exc:                      # noqa: BLE001 - one repo
        rows, info = [], {}
        status = "timeout" if job.expired else "error"
        error = "%s: %s" % (type(exc).__name__, exc)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "rows.csv"), csv_text([ROW_HEADER] + rows))
    rec = {"org": org, "repo": repo, "status": status,
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - job.start, 1),
           "files": len(rows),
           "reasons": dict(Counter(r[5] for r in rows)),
           "renames_checked": cfg.renames, **info, "error": error}
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# combine + summaries
# --------------------------------------------------------------------------

def combine(out, top_n=30):
    state = os.path.join(out, STATE)
    reasons, cats, vend = Counter(), Counter(), Counter()
    by_year = defaultdict(Counter)                 # year -> reason
    removal = Counter()                            # (org, repo, sha) -> files
    removal_meta = {}
    by_repo = defaultdict(Counter)
    total = repos = 0
    comb = os.path.join(out, "history_only_files.csv")
    with open(comb + ".tmp", "w", newline="", encoding="utf-8",
              errors="surrogateescape") as cf:
        w = csv.writer(cf)
        w.writerow(ROW_HEADER)
        for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
            od = os.path.join(state, org)
            for repo in sorted(os.listdir(od)):
                rp = os.path.join(od, repo, "rows.csv")
                if not os.path.isfile(os.path.join(od, repo, "done.json")):
                    continue
                repos += 1
                with open(rp, newline="", encoding="utf-8",
                          errors="surrogateescape") as fh:
                    rd = csv.reader(fh)
                    next(rd, None)
                    for r in rd:
                        w.writerow(r)
                        total += 1
                        reason, cat = r[5], r[3]
                        reasons[reason] += 1
                        cats[cat] += 1
                        by_repo[(r[0], r[1])][reason] += 1
                        if cat == "vendored":
                            vend[r[4]] += 1
                            continue
                        if r[11]:
                            by_year[r[11][:4]][reason] += 1
                        if r[10]:
                            k = (r[0], r[1], r[10])
                            removal[k] += 1
                            if k not in removal_meta:
                                removal_meta[k] = (r[11], r[12])
    os.replace(comb + ".tmp", comb)

    def write(name, header, rows):
        with open(os.path.join(out, name), "w", newline="",
                  encoding="utf-8", errors="surrogateescape") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)

    top = heapq.nlargest(1000, removal.items(), key=lambda kv: kv[1])
    write("top_removal_commits.csv",
          ["org", "repo", "commit", "date", "files_removed", "message"],
          [[k[0], k[1], k[2], removal_meta[k][0], n, removal_meta[k][1]]
           for k, n in top])
    reason_cols = ["deleted", "renamed", "still_on_branch", "orphaned",
                   "vendored"]
    write("by_repo.csv", ["org", "repo", "history_only_files"] + reason_cols,
          [[o, r, sum(c.values())] + [c[x] for x in reason_cols]
           for (o, r), c in sorted(by_repo.items(),
                                   key=lambda kv: -sum(kv[1].values()))])
    write("by_year.csv", ["year_removed", "deleted", "renamed"],
          [[y, c["deleted"], c["renamed"]] for y, c in sorted(by_year.items())])

    def pct(n, d):
        return "%.1f%%" % (100.0 * n / d) if d else "-"

    own = cats["own_code"]
    md = ["# History-only files", "",
          "Files extracted from history that no longer exist in the active "
          "copy (at_head = no).", "",
          "| | files | share |", "|---|---:|---:|",
          "| **Total** | **%s** | 100%% |" % f"{total:,}",
          "| own code | %s | %s |" % (f"{own:,}", pct(own, total)),
          "| vendored (not looked up) | %s | %s |"
          % (f"{cats['vendored']:,}", pct(cats["vendored"], total)), "",
          "Repositories: %s" % f"{repos:,}", "",
          "## Own code: why each file is gone", "",
          "| reason | files | share of own code |", "|---|---:|---:|"]
    for x in ("deleted", "renamed", "still_on_branch", "orphaned"):
        md.append("| %s | %s | %s |" % (x, f"{reasons[x]:,}", pct(reasons[x], own)))
    md += ["", "## Vendored files by folder", "",
           "| folder | files |", "|---|---:|"]
    md += ["| %s | %s |" % (k, f"{v:,}") for k, v in vend.most_common(15)]
    md += ["", "## Own code removed, by year", "",
           "| year | deleted | renamed |", "|---|---:|---:|"]
    md += ["| %s | %s | %s |" % (y, f"{c['deleted']:,}", f"{c['renamed']:,}")
           for y, c in sorted(by_year.items())]
    md += ["", "## Top %d mass-removal commits (own code)" % top_n, "",
           "| repo | commit | date | files removed | message |",
           "|---|---|---|---:|---|"]
    for (o, r, sha), n in top[:top_n]:
        d, m = removal_meta[(o, r, sha)]
        md.append("| %s/%s | %s | %s | %s | %s |"
                  % (o, r, sha[:10], d[:10], f"{n:,}",
                     m.replace("|", "/")[:80]))
    md += ["", "## Top %d repositories by history-only files" % top_n, "",
           "| repo | files | deleted | renamed | on branch | orphaned | "
           "vendored |", "|---|---:|---:|---:|---:|---:|---:|"]
    for (o, r), c in sorted(by_repo.items(),
                            key=lambda kv: -sum(kv[1].values()))[:top_n]:
        md.append("| %s/%s | %s | %s |" % (
            o, r, f"{sum(c.values()):,}",
            " | ".join(f"{c[x]:,}" for x in reason_cols)))
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    return total, repos


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def read_batch(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [(r["org"].strip(), r["repo"].strip()) for r in csv.DictReader(fh)
                if r.get("org") and r["org"] != "(all)"]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="file_added_lines.py output (full_extract)")
    ap.add_argument("--repos-root", required=True,
                    help="the git repos, holding <org>/<repo>")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV",
                    help="only the repos in this batch file (repeatable)")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos at once (default 8)")
    ap.add_argument("--no-renames", action="store_true",
                    help="skip rename detection (for the giant repos); "
                         "renames then show as deleted")
    ap.add_argument("--max-branches", type=int, default=200,
                    help="above this many branches only the default and "
                         "release* ones are checked (default 200)")
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo after this long (default never)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only repos whose last attempt failed or timed out")
    ap.add_argument("--redo", action="store_true",
                    help="reprocess repos already done")
    ap.add_argument("--combine-only", action="store_true",
                    help="only rebuild the combined CSV and summaries")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    ap.add_argument("--extensions", help="as in file_added_lines.py")
    args = ap.parse_args()

    if args.combine_only:
        total, repos = combine(args.out)
        print("combined %s file(s) from %s repo(s) -> %s"
              % (f"{total:,}", f"{repos:,}", args.out))
        return 0
    src_state = os.path.join(args.src, STATE)
    if not os.path.isdir(src_state):
        sys.exit("no _state folder under " + args.src)
    name = args.name or (os.path.splitext(os.path.basename(args.batch[0]))[0]
                         if args.batch else "run")
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(args.out, LOGS, name + ".log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    eh = logging.StreamHandler(sys.stderr)
    eh.setLevel(logging.WARNING)
    log.addHandler(eh)

    chosen = []
    for b in args.batch:
        chosen += read_batch(b)
    chosen += [tuple(r.strip("/").split("/", 1)) for r in args.repo]
    if not chosen:
        chosen = [(o, r) for o in sorted(os.listdir(src_state))
                  if os.path.isdir(os.path.join(src_state, o))
                  for r in sorted(os.listdir(os.path.join(src_state, o)))]
    chosen = [k for k in dict.fromkeys(chosen) if os.path.isfile(
        os.path.join(src_state, k[0], k[1], "manifest.csv"))]

    def state_of(k):
        try:
            with open(os.path.join(args.out, STATE, k[0], k[1], "done.json"),
                      encoding="utf-8") as f:
                return json.load(f).get("status")
        except (OSError, ValueError):
            return None

    prior = {k: state_of(k) for k in chosen}
    if args.redo:
        todo = chosen
    elif args.retry_failed:
        todo = [k for k in chosen if prior[k] not in (None, "ok")]
    else:
        todo = [k for k in chosen if prior[k] is None]
    print("repos     %s to process (%s done, %s failed earlier)"
          % (f"{len(todo):,}",
             f"{sum(1 for v in prior.values() if v == 'ok'):,}",
             f"{sum(1 for v in prior.values() if v not in (None, 'ok')):,}"),
          flush=True)

    class Cfg:
        pass
    cfg = Cfg()
    cfg.src, cfg.out, cfg.repos_root = args.src, args.out, args.repos_root
    cfg.renames, cfg.max_branches = not args.no_renames, args.max_branches
    cfg.exts = set(load_extensions(args.extensions))
    log.info("START %s repos=%d workers=%d renames=%s max_branches=%d",
             name, len(todo), args.workers, cfg.renames, cfg.max_branches)

    t0 = time.time()
    statuses, reasons = Counter(), Counter()
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds",
                         "files", "branches_checked", "branches_total",
                         "recovered", "error"])
        pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
        queue, running, done = list(todo), {}, 0
        while queue or running:
            while queue and len(running) < args.workers:
                k = queue.pop(0)
                job = RepoJob(k[0], k[1], 0)
                running[pool.submit(run_repo, cfg, k[0], k[1], job)] = (k, job)
            ready, _ = wait(running, timeout=5, return_when=FIRST_COMPLETED)
            if args.repo_timeout:
                for fut, (k, job) in running.items():
                    if fut not in ready and not job.expired \
                            and job.elapsed() > args.repo_timeout:
                        log.warning("TIMEOUT %s/%s in phase %s", k[0], k[1],
                                    job.phase)
                        job.kill()
            for fut in ready:
                k, job = running.pop(fut)
                rec = fut.result()
                done += 1
                statuses[rec["status"]] += 1
                reasons.update(rec["reasons"])
                lvl = logging.INFO if rec["status"] == "ok" else logging.ERROR
                log.log(lvl, "done   %s/%s %s %.0fs files=%d %s%s", k[0], k[1],
                        rec["status"], rec["seconds"], rec["files"],
                        rec["reasons"],
                        (" error=" + rec["error"]) if rec["error"] else "")
                rw.writerow([rec["finished"], k[0], k[1], rec["status"],
                             rec["seconds"], rec["files"],
                             rec.get("branches_checked", ""),
                             rec.get("branches_total", ""),
                             rec.get("recovered", ""), rec["error"]])
                rf.flush()
                if done % 200 == 0:
                    el = time.time() - t0
                    print("  %s / %s repos  %.0fs  (~%.0f min left)"
                          % (f"{done:,}", f"{len(todo):,}", el,
                             el / done * (len(todo) - done) / 60),
                          file=sys.stderr, flush=True)
        pool.shutdown()

    total, repos = combine(args.out)
    summary = ("END %s: %s repo(s) (%s) | files by reason: %s | combined %s "
               "file(s) from %s repo(s) | %.0fs"
               % (name, f"{done:,}",
                  ", ".join("%s %d" % kv for kv in statuses.most_common()) or "-",
                  ", ".join("%s %s" % (k, f"{v:,}")
                            for k, v in reasons.most_common()) or "-",
                  f"{total:,}", f"{repos:,}", time.time() - t0))
    log.info(summary)
    print(summary)
    print("-> %s" % os.path.join(args.out, "summary.md"))
    return 0 if not (statuses["error"] or statuses["timeout"]) else 1


if __name__ == "__main__":
    sys.exit(main())
