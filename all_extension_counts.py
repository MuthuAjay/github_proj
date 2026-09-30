#!/usr/bin/env python3
"""
all_extension_counts.py - EVERY file extension in the repos, with how many
files and versions of it the full git history holds and how many are in
the active copy. Counts only - nothing is extracted.

The same reading as extension_versions.py pass 1 (every branch, merges
against their first parent, repos with no HEAD/refs through recovery, and
the active copy listed once per repo), but for every path instead of a list
of extensions, and kept per EXTENSION rather than per file: a repo with a
million files still gives one row per extension it has.

Per repo and extension:
  files_in_history      distinct paths ever in history
  files_at_head         ... still in the active copy
  files_history_only    ... not in it (deleted, renamed away, branch-only)
  files_no_active_repo  ... in a repo with no folder in the active copy
  files_active_only     in the active copy, never in the archive's history
  files_vendored        in node_modules, packages, bin, obj, ... (included
                        in the counts above)
  commits               commits that touched these files
  versions              distinct contents per file, summed
  versions_in_repo      distinct contents within the repo (a copy under a
                        second path counts once)
  bytes_versions, bytes_in_repo, max_bytes
  missing_objects       versions whose blob the archive does not have

Files without an extension are counted as "(none)"; extensions are matched
the way the other scripts match them (lower case; ".gitignore" is
"gitignore"). Not done here, on purpose: the LFS check, the comparison with
the active files, and dedup across repos - they decide what to send, which
is extension_versions.py's job for the extensions chosen.

A failing active-copy mount is not recorded as missing files: the repo gets
no result and the run stops with exit code 3 (as extension_versions.py).

--history-only reads the history and nothing else: files_in_history,
files_vendored, commits and versions per extension - no active copy (the
mount is not needed), no sizes. That is all extension_counts.csv needs.

Output, under --out:
  extension_counts.csv   THE short answer: ext, files, group - every
                     extension and how many files with it the history ever
                     held, most first; group: "group 1" = the 32 of the first
                     extraction (repo_extension_summary.py), "group 2" = the
                     27 of the second, "rest" = neither; a total per group
                     and an overall total at the end
  _state/<org>/<repo>/ext_counts.csv, done.json     per repo (resume)
  _logs/<name>.log, <name>_repos.csv
  by_extension.csv   one row per extension, most files first; in_27 says
                     whether it is one of the 27 of the PII extraction
  by_repo.csv        one row per repo and extension
  summary.md         totals, the top extensions, and the long tail

Usage:
    python3 all_extension_counts.py --batch batches/S01.csv \\
        --repos-root /data/workarea/archive \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --out /data/workarea/all_ext_counts --workers 16
    # just the history: ext -> files, no mount needed
    python3 all_extension_counts.py --batch batches/S01.csv --history-only \\
        --repos-root /data/workarea/archive --out /data/workarea/all_ext_counts
    python3 all_extension_counts.py --out /data/workarea/all_ext_counts --combine-only
"""

import argparse
import csv
import datetime
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

from explain_file_counts import vendor_of
from explore_input_csv import is_repo_dir
from extension_versions import (BINARY_EXTS, LOGS, STATE, TEXT_EXTS,
                                active_listing, blob_sizes, read_batch,
                                read_history, read_json, read_rows)
from extract_commits import bind_job
from file_delta import (MountDown, check_mount, csv_text, write_atomic)
from file_history_for_list import RecoveredRepo, RepoJob, git_can_open
from repo_extension_summary import EXTENSIONS, ext_key

NONE = "(none)"
COLS = ["files_in_history", "files_at_head", "files_history_only",
        "files_no_active_repo", "files_active_only", "files_vendored",
        "commits", "versions", "versions_in_repo", "bytes_versions",
        "bytes_in_repo", "max_bytes", "missing_objects"]
GB = 1e9
IN_27 = set(TEXT_EXTS) | set(BINARY_EXTS)
GROUP_1 = set(EXTENSIONS)            # the 32 of the first extraction
GROUP_2 = IN_27                      # the 27 of the second


def group_label(ext):
    return ("group 1" if ext in GROUP_1 else
            "group 2" if ext in GROUP_2 else "rest")
log = logging.getLogger("all_extension_counts")


def ext_of(path):
    return ext_key(path) or NONE


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def count_repo(cfg, org, repo, job):
    """-> ({ext: Counter of COLS}, info)."""
    rp = os.path.join(cfg.repos_root, org, repo)
    if not is_repo_dir(rp):
        raise RuntimeError("no repository at " + rp)
    info = {"recovered": False}
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
        hist = read_history(gp, None, job)
        sizes = {}
        if not cfg.history_only:
            job.phase = "blob sizes"
            sizes = blob_sizes(gp, sorted({b for r in hist.values()
                                           for b in r["blobs"]}))
    finally:
        if recovery:
            recovery.__exit__(None, None, None)

    active = None
    if not cfg.history_only:
        active = active_listing(cfg.active_root, org, repo, None, job)
        info["active_copy"] = active is not None
    job.phase = "counting"
    acc = defaultdict(Counter)
    in_repo = defaultdict(dict)                     # ext -> {blob: bytes}
    for p, r in hist.items():
        e = ext_of(p)
        c = acc[e]
        c["files_in_history"] += 1
        if not cfg.history_only:
            c["files_" + ("no_active_repo" if active is None else
                          "at_head" if p in active else "history_only")] += 1
        c["files_vendored"] += bool(vendor_of(p))
        c["commits"] += r["commits"]
        c["versions"] += len(r["blobs"])
        if cfg.history_only:
            continue                          # no sizes read
        for b in r["blobs"]:
            n = sizes.get(b)
            if n is None:
                c["missing_objects"] += 1
                continue
            c["bytes_versions"] += n
            c["max_bytes"] = max(c["max_bytes"], n)
            in_repo[e][b] = n
    for e, bl in in_repo.items():
        acc[e]["versions_in_repo"] = len(bl)
        acc[e]["bytes_in_repo"] = sum(bl.values())
    for p in (active or set()) - set(hist):
        c = acc[ext_of(p)]
        c["files_active_only"] += 1
        c["files_vendored"] += bool(vendor_of(p))
    return acc, info


def run_repo(cfg, org, repo, job):
    """One repo. A MountDown leaves it with no done.json."""
    job.start = time.time()
    bind_job(job)
    if not cfg.history_only:
        check_mount(cfg.active_root)
    sd = os.path.join(cfg.out, STATE, org, repo)
    shutil.rmtree(sd, ignore_errors=True)
    try:
        acc, info = count_repo(cfg, org, repo, job)
        status, error = "ok", ""
    except MountDown:
        raise
    except Exception as exc:                       # noqa: BLE001 - one repo
        acc, info = {}, {}
        status = "timeout" if job.expired else "error"
        error = "%s: %s" % (type(exc).__name__, exc)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "ext_counts.csv"), csv_text(
        [["ext"] + COLS] + [[e] + [acc[e][c] for c in COLS]
                            for e in sorted(acc)]))
    rec = {"org": org, "repo": repo, "status": status,
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - job.start, 1),
           "extensions": len(acc),
           "files": sum(c["files_in_history"] for c in acc.values()),
           "active_only": sum(c["files_active_only"] for c in acc.values()),
           **info, "error": error}
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# combine
# --------------------------------------------------------------------------

def combine(out):
    """by_extension.csv, by_repo.csv and summary.md from every repo done."""
    state = os.path.join(out, STATE)
    acc = defaultdict(Counter)
    repos_of = Counter()
    per_repo = []
    repos = 0
    statuses = Counter()
    bad = []
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            sd = os.path.join(od, repo)
            done = read_json(os.path.join(sd, "done.json"))
            statuses[done.get("status") if done else "not run"] += 1
            if not done or done.get("status") != "ok":
                continue
            repos += 1
            for r in read_rows(os.path.join(sd, "ext_counts.csv")):
                e = r["ext"]
                if e is None or any(not (r.get(c) or "").isdigit()
                                    for c in COLS):
                    # a row that does not read back as ext + numbers: kept
                    # out of the totals and listed, never a crash
                    bad.append([org, repo, repr(e),
                                repr([r.get(c) for c in COLS])[:300],
                                repr(r.get(None))[:300]])
                    continue
                vals = {c: int(r[c]) for c in COLS}
                for c in COLS:
                    if c == "max_bytes":
                        acc[e][c] = max(acc[e][c], vals[c])
                    else:
                        acc[e][c] += vals[c]
                repos_of[e] += 1
                per_repo.append([org, repo, e] + [vals[c] for c in COLS])

    order = sorted(acc, key=lambda e: (-(acc[e]["files_in_history"]
                                         + acc[e]["files_active_only"]), e))
    head = ["ext", "in_27", "group", "repos"] + COLS[:-4] + [
        "gb_versions", "gb_in_repo", "max_bytes", "missing_objects"]
    table = []
    for e in order:
        a = acc[e]
        table.append([e, "yes" if e in IN_27 else "",
                      "binary" if e in BINARY_EXTS else
                      "text" if e in TEXT_EXTS else "", repos_of[e]]
                     + [a[c] for c in COLS[:-4]]
                     + [round(a["bytes_versions"] / GB, 3),
                        round(a["bytes_in_repo"] / GB, 3), a["max_bytes"],
                        a["missing_objects"]])
    with open(os.path.join(out, "by_extension.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        w.writerows(table)
    with open(os.path.join(out, "by_repo.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "ext"] + COLS)
        w.writerows(per_repo)
    with open(os.path.join(out, "extension_counts.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "files", "group"])
        w.writerows(sorted(([e, acc[e]["files_in_history"], group_label(e)]
                            for e in acc if acc[e]["files_in_history"]),
                           key=lambda r: (-r[1], r[0])))
        for g in ("group 1", "group 2", "rest"):
            w.writerow(["total " + g, sum(acc[e]["files_in_history"]
                                          for e in acc if group_label(e) == g),
                        g])
        w.writerow(["total", sum(acc[e]["files_in_history"] for e in acc),
                    ""])
    with open(os.path.join(out, "combine_problems.csv"), "w", newline="",
              encoding="utf-8", errors="backslashreplace") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "ext_as_read", "values_as_read", "extra"])
        w.writerows(bad)
    if bad:
        print("WARNING %d row(s) in %d repo(s) did not read back and are not "
              "counted - listed in combine_problems.csv"
              % (len(bad), len({(b[0], b[1]) for b in bad})), file=sys.stderr)
    write_summary(out, table, head, repos, statuses, len(bad))
    return repos, len(order)


def write_summary(out, table, head, repos, statuses, bad=0):
    ix = {c: i for i, c in enumerate(head)}

    def n(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    def tot(c, rows=table):
        return sum(r[ix[c]] for r in rows)

    files = tot("files_in_history")
    show = ["repos", "files_in_history", "files_at_head", "files_history_only",
            "files_active_only", "files_vendored", "versions", "gb_versions"]
    md = ["# All file extensions", "",
          "Generated %s by all_extension_counts.py from %s repositories "
          "(%s)." % (datetime.date.today().isoformat(), f"{repos:,}",
                     ", ".join("%s %s" % (k, f"{v:,}")
                               for k, v in statuses.most_common())), "",
          "| | count |", "|---|---:|",
          "| extensions | %s |" % f"{len(table):,}",
          "| files ever in history | %s |" % f"{files:,}",
          "| ... still in the active copy | %s |" % f"{tot('files_at_head'):,}",
          "| ... history only | %s |" % f"{tot('files_history_only'):,}",
          "| ... in repos with no active copy | %s |"
          % f"{tot('files_no_active_repo'):,}",
          "| files only in the active copy | %s |"
          % f"{tot('files_active_only'):,}",
          "| versions (distinct contents per file) | %s |"
          % f"{tot('versions'):,}",
          "| size of those versions | %.1f GB |" % tot("gb_versions"), "",
          "## Top 50 extensions by files", "",
          "| ext | in 27 | " + " | ".join(show) + " |",
          "|---|---|" + "---:|" * len(show)]
    for r in table[:50]:
        md.append("| %s | %s | %s |" % (r[0], r[1], " | ".join(
            n(r[ix[c]]) for c in show)))
    small = [r for r in table if r[ix["files_in_history"]]
             + r[ix["files_active_only"]] < 10]
    md += ["", "## The long tail", "",
           "%s extension(s) have fewer than 10 files each (%s files in all) - "
           "often names with a dot rather than real types (version numbers, "
           "dates, backups). All are in by_extension.csv."
           % (f"{len(small):,}", f"{tot('files_in_history', small):,}"), "",
           "Columns: see the docstring of all_extension_counts.py. "
           "\"(none)\" = files without an extension."]
    if bad:
        md += ["", "**%d row(s) could not be read back and are not counted** "
               "- see combine_problems.csv." % bad]
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos-root", help="the archive repos, <org>/<repo>")
    ap.add_argument("--active-root", help="the active copy, <org>/<repo>/...")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV",
                    help="only the repos in this batch file (repeatable)")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--history-only", action="store_true",
                    help="count the history only: files, commits and versions "
                         "per extension - no active copy (the mount is not "
                         "needed), no sizes. Much faster")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos at once (default 8)")
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo after this long (default never)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only repos whose last attempt failed or timed out")
    ap.add_argument("--redo", action="store_true",
                    help="reprocess repos already done")
    ap.add_argument("--combine-only", action="store_true",
                    help="only rebuild by_extension.csv, by_repo.csv, summary.md")
    ap.add_argument("--no-combine", action="store_true",
                    help="skip the rebuild at the end (a runner combines once)")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    args = ap.parse_args()

    if args.combine_only:
        repos, exts = combine(args.out)
        print("combined %s repo(s), %s extension(s) -> %s"
              % (f"{repos:,}", f"{exts:,}",
                 os.path.join(args.out, "by_extension.csv")))
        return 0
    if not args.repos_root or not (args.active_root or args.history_only):
        sys.exit("--repos-root and --active-root (or --history-only) are "
                 "needed")
    if not args.history_only:
        try:
            check_mount(args.active_root)
        except MountDown as exc:
            print("STOPPED: %s - remount it and run the same command again"
                  % exc, file=sys.stderr)
            return 3
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
        root = args.repos_root
        chosen = [(o, r) for o in sorted(os.listdir(root))
                  if os.path.isdir(os.path.join(root, o))
                  for r in sorted(os.listdir(os.path.join(root, o)))
                  if is_repo_dir(os.path.join(root, o, r))]
    chosen = list(dict.fromkeys(chosen))
    prior = {}
    for k in chosen:
        rec = read_json(os.path.join(args.out, STATE, k[0], k[1], "done.json"))
        prior[k] = rec.get("status") if rec else None
    if args.redo:
        todo = chosen
    elif args.retry_failed:
        todo = [k for k in chosen if prior[k] not in (None, "ok")]
    else:
        todo = [k for k in chosen if prior[k] is None]
    print("all extensions  %s repo(s) to process (%s done, %s failed earlier)"
          % (f"{len(todo):,}",
             f"{sum(1 for v in prior.values() if v == 'ok'):,}",
             f"{sum(1 for v in prior.values() if v not in (None, 'ok')):,}"),
          flush=True)

    class Cfg:
        pass
    cfg = Cfg()
    cfg.out, cfg.repos_root, cfg.active_root = (args.out, args.repos_root,
                                                args.active_root)
    cfg.history_only = args.history_only
    workers = max(1, args.workers)
    log.info("START %s repos=%d workers=%d %s", name, len(todo), workers,
             "history only" if args.history_only
             else "active=" + args.active_root)

    t0 = time.time()
    statuses = Counter()
    stopped = ""
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds",
                         "files", "extensions", "active_only", "error"])
        pool = ThreadPoolExecutor(max_workers=workers)
        queue, running, done = list(todo), {}, 0
        while queue or running:
            while queue and not stopped and len(running) < workers:
                k = queue.pop(0)
                job = RepoJob(k[0], k[1], 0)
                running[pool.submit(run_repo, cfg, k[0], k[1], job)] = (k, job)
            if not running:
                break
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
                try:
                    rec = fut.result()
                except MountDown as exc:
                    statuses["mount_down"] += 1
                    log.error("MOUNT  %s/%s not done, retried next run: %s",
                              k[0], k[1], exc)
                    if not stopped:
                        stopped = str(exc)
                        log.error("STOP   the active copy is not reachable - "
                                  "no new repos are started")
                    continue
                except Exception as exc:          # noqa: BLE001 - one repo
                    statuses["failed"] += 1
                    log.error("FAIL   %s/%s %s: %s", k[0], k[1],
                              type(exc).__name__, exc)
                    continue
                done += 1
                statuses[rec["status"]] += 1
                lvl = logging.INFO if rec["status"] == "ok" else logging.ERROR
                log.log(lvl, "done   %s/%s %s %.0fs files=%d extensions=%d%s%s",
                        k[0], k[1], rec["status"], rec["seconds"], rec["files"],
                        rec["extensions"],
                        "" if rec.get("active_copy", True)
                        else " no_active_copy",
                        (" error=" + rec["error"]) if rec["error"] else "")
                rw.writerow([rec["finished"], k[0], k[1], rec["status"],
                             rec["seconds"], rec["files"], rec["extensions"],
                             rec["active_only"], rec["error"]])
                rf.flush()
                if done % 200 == 0:
                    el = time.time() - t0
                    print("  %s / %s repos  %.0fs  (~%.0f min left)"
                          % (f"{done:,}", f"{len(todo):,}", el,
                             el / done * (len(todo) - done) / 60),
                          file=sys.stderr, flush=True)
        pool.shutdown()

    repos = "-" if args.no_combine else f"{combine(args.out)[0]:,}"
    summary = ("END %s: %s repo(s) (%s) | combined %s repo(s) | %.0fs"
               % (name, f"{done:,}",
                  ", ".join("%s %d" % kv for kv in statuses.most_common())
                  or "-", repos, time.time() - t0))
    log.info(summary)
    print(summary)
    if stopped:
        log.error("STOPPED: the active copy stopped answering (%s). Remount "
                  "it, then run the SAME command again - finished repos are "
                  "skipped.", stopped)
        return 3
    return 1 if (statuses["failed"] or statuses["error"]
                 or statuses["timeout"]) else 0


if __name__ == "__main__":
    sys.exit(main())
