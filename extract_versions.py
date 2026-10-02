#!/usr/bin/env python3
"""
extract_versions.py - write the previous versions of the binary extensions
(xlsx, docx, pptx, msg, png, ...) out of the archive as real files, each
content once, with a manifest that says where every one came from.

Works from what extension_versions.py left (pass 1 AND pass 2 done):

  _state/<org>/<repo>/files.csv, versions.csv, identical.csv

and takes, per repo, the same versions its versions_to_send counts: every
version of a binary extension except those whose content is a file in the
SAME repo's active copy (already processed). History is not read again and
the active copy is not touched - the content comes from the archive's object
store, one `git cat-file --batch` per repo, streamed to disk (no file is held
in memory) and checked: the git hash of what was written must equal the
version's blob id, or the file is dropped and reported.

Dedup: a content is stored once per extension, however many paths, repos or
commits have it:

    files/<ext>/<first 2 of blob>/<blob>.<ext>

and every (org, repo, path, version) that has it is a manifest row pointing
at that file. A repo finding its content already stored (another repo wrote
it) records it as `deduped` and writes nothing.

With --per-repo (group 3: the scope is one repo, nothing is shared across
repos) a content is stored once per repo instead:

    files/<ext>/<org>/<repo>/<blob>.<ext>

so a file in two repos is written twice, and `deduped` never occurs.

Manifest action per version:
  written          stored now
  deduped          already stored by an earlier repo (same content)
  lfs_stub         the archive holds only a Git LFS pointer - no content
  empty            0 bytes - nothing to scan, not written
  missing_object   the archive does not have the blob
  hash_mismatch    what git returned does not hash to the blob id (dropped)
  skipped_vendored / too_large   only with --skip-vendored / --max-bytes
and why it is sent (the file's state): history_only, no_active_repo,
older_version (its active file is another version), active_differs (its
active file is none of its versions), active_unreadable.

A repo whose pass 2 has not finished is not extracted (status no_pass2): its
processed version is not known. Nothing is ever written to the archive or to
the extension_versions.py folder.

Disk: no new repo starts, and a running repo stops, while --out's disk has
less than --min-free-disk-gb free. The repo then has no done.json, the run
exits with code 3; free space and run the same command again - files
already stored are not written twice.

Output, under --out:
  files/<ext>/<ab>/<blob>.<ext>       the contents to send
  _state/<org>/<repo>/manifest.csv, done.json      per repo (resume)
  _logs/<name>.log, <name>_repos.csv
  manifest.csv       every repo's rows (--combine-only rebuilds it)
  by_extension.csv   per extension: versions, files stored, GB, and every
                     action; plus the files actually on disk as a check
  summary.md

Usage:
    python3 extract_versions.py --state /data/workarea/ext_versions \\
        --repos-root /data/workarea/archive --out /data/workarea/binary_versions \\
        --batch batches/S01.csv --workers 16
    python3 extract_versions.py --out /data/workarea/binary_versions --combine-only
"""

import argparse
import csv
import datetime
import hashlib
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

from explore_input_csv import is_repo_dir
from extension_versions import (BINARY_EXTS, LOGS, PROCESSED, STATE,
                                processed_in_repo, read_batch, read_json,
                                read_rows)
from extract_commits import GIT, bind_job, run_tracked, spawn
from file_delta import csv_text, write_atomic
from file_history_for_list import RepoJob, git_can_open
from repo_extension_summary import load_extensions

CHUNK = 4 * 1024 * 1024
GB = 1e9
MANIFEST_HEADER = ["org", "repo", "path", "ext", "blob", "bytes", "action",
                   "stored_as", "why", "vendored", "first_commit", "first_date",
                   "last_commit", "last_date"]
ACTIONS = ["written", "deduped", "lfs_stub", "empty", "missing_object",
           "hash_mismatch", "skipped_vendored", "too_large"]
STORED = ("written", "deduped")
log = logging.getLogger("extract_versions")


class DiskLow(RuntimeError):
    pass


def store_path(ext, blob, org=None, repo=None):
    """Where a content is stored: once per extension across all repos
    (files/<ext>/<ab>/<blob>.<ext>), or with --per-repo once per repo
    (files/<ext>/<org>/<repo>/<blob>.<ext>)."""
    if org is not None:
        return os.path.join("files", ext, org, repo, "%s.%s" % (blob, ext))
    return os.path.join("files", ext, blob[:2], "%s.%s" % (blob, ext))


def where(cfg, org, repo):
    """(org, repo) for store_path in --per-repo mode, else (None, None)."""
    return (org, repo) if cfg.per_repo else (None, None)


def why_sent(at_head, result):
    """Why a version not in the active copy is sent, from its file's state."""
    if at_head == "":
        return "no_active_repo"
    if at_head == "no":
        return "history_only"
    if result in PROCESSED:
        return "older_version"
    if result in ("no_match", "lfs_active_real"):
        return "active_differs"
    return "active_unreadable"


class ObjectStore:
    """A repo `git cat-file` can read blobs from: the repo itself, or - for a
    repo git cannot open (objects but no HEAD/refs, like every repo of the
    archive) - a throwaway bare repo that borrows its objects/ as an
    alternate. Nothing is written to the original."""

    def __init__(self, repo_path):
        self.repo_path, self.tmp = repo_path, None

    def __enter__(self):
        if git_can_open(self.repo_path):
            return self.repo_path
        gitdir = os.path.join(self.repo_path, ".git")
        if not os.path.isdir(gitdir):
            gitdir = self.repo_path
        objects = os.path.join(gitdir, "objects")
        if not os.path.isdir(objects):
            raise RuntimeError("no objects/ directory in %s" % gitdir)
        self.tmp = tempfile.mkdtemp(prefix="xv_")
        if run_tracked(GIT + ["init", "--bare", "-q", self.tmp]).returncode:
            raise RuntimeError("git init failed")
        with open(os.path.join(self.tmp, "objects", "info", "alternates"),
                  "w") as fh:
            fh.write(os.path.abspath(objects) + "\n")
        return self.tmp

    def __exit__(self, *exc):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


def free_gb(path):
    return shutil.disk_usage(path).free / GB


def fetch(gp, blobs, dest_of, cfg, job):
    """Stream `blobs` out of the object store into dest_of(blob), each
    written to a temp file, hash-checked and then renamed into place.
    -> {blob: action} (written / missing_object / hash_mismatch)."""
    done = {}
    if not blobs:
        return done
    proc = spawn(GIT + ["-C", gp, "cat-file", "--batch"],
                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL)
    tmp = None
    try:
        for i, (blob, size_hint) in enumerate(blobs, 1):
            if job.expired:
                raise TimeoutError("timed out after %d of %d files"
                                   % (i - 1, len(blobs)))
            if cfg.min_free_gb and \
                    free_gb(cfg.out) - size_hint / GB < cfg.min_free_gb:
                raise DiskLow("less than %s GB free on %s"
                              % (cfg.min_free_gb, cfg.out))
            job.phase = "writing %d/%d" % (i, len(blobs))
            proc.stdin.write(blob.encode() + b"\n")
            proc.stdin.flush()
            head = proc.stdout.readline().split()
            if len(head) < 3 or head[1] != b"blob":
                done[blob] = "missing_object"
                continue
            size = int(head[2])
            dest = dest_of(blob)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = "%s.tmp.%d.%d" % (dest, os.getpid(), threading.get_ident())
            h = hashlib.sha1(b"blob %d\0" % size)
            left = size
            with open(tmp, "wb") as fh:
                while left:
                    chunk = proc.stdout.read(min(left, CHUNK))
                    if not chunk:
                        raise RuntimeError("cat-file stopped in the middle of "
                                           + blob)
                    h.update(chunk)
                    fh.write(chunk)
                    left -= len(chunk)
            proc.stdout.read(1)                  # the "\n" after each object
            if h.hexdigest() != blob:
                os.remove(tmp)
                done[blob] = "hash_mismatch"
            else:
                os.replace(tmp, dest)
                done[blob] = "written"
            tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        try:
            proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()
    return done


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def plan_repo(cfg, org, repo, sd):
    """-> (rows, {blob: (ext, bytes)} to fetch), or None if pass 2 is
    missing where it is needed. rows are MANIFEST_HEADER lists whose action
    is filled in later for the ones to fetch."""
    files = {f["path"]: f for f in read_rows(os.path.join(sd, "files.csv"))}
    ij = read_json(os.path.join(sd, "identical.json"))
    pass2 = bool(ij and ij.get("status") == "ok")
    if not pass2 and any(f["group"] == "binary" and f["at_head"] == "yes"
                         and f["ext"] in cfg.exts and f["in_history"] == "yes"
                         for f in files.values()):
        return None
    ident = ({r["path"]: r for r in read_rows(os.path.join(sd, "identical.csv"))}
             if pass2 else {})
    processed = processed_in_repo(sd)
    rows, want = [], {}
    for v in read_rows(os.path.join(sd, "versions.csv")):
        f = files.get(v["path"])
        if f is None or f["group"] != "binary" or f["ext"] not in cfg.exts:
            continue
        if bytes.fromhex(v["blob"]) in processed:
            continue                               # in this repo's active copy
        e, b = f["ext"], v["blob"]
        n = int(v["bytes"]) if v["bytes"].isdigit() else None
        r = ident.get(v["path"])
        row = [org, repo, v["path"], e, b, "" if n is None else n, "", "",
               why_sent(f["at_head"], r["result"] if r else None),
               f["vendored"], v["first_commit"], v["first_date"],
               v["last_commit"], v["last_date"]]
        if v["lfs_oid"]:
            row[6] = "lfs_stub"
        elif n is None:
            row[6] = "missing_object"
        elif n == 0:
            row[6] = "empty"
        elif cfg.skip_vendored and f["vendored"]:
            row[6] = "skipped_vendored"
        elif cfg.max_bytes and n > cfg.max_bytes:
            row[6] = "too_large"
        else:
            row[7] = store_path(e, b, *where(cfg, org, repo))
            want[(e, b)] = n
        rows.append(row)
    return rows, want


def run_repo(cfg, org, repo, job):
    """Extract one repo. DiskLow leaves it with no done.json."""
    job.start = time.time()
    bind_job(job)
    sd_in = os.path.join(cfg.state, STATE, org, repo)
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_pass1",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "versions": 0, "actions": {}, "bytes_written": 0,
           "error": ""}
    done1 = read_json(os.path.join(sd_in, "done.json"))
    if not done1 or done1.get("status") != "ok":
        return rec                           # nothing written: rerun later
    planned = plan_repo(cfg, org, repo, sd_in)
    if planned is None:
        rec["status"] = "no_pass2"
        return rec
    rows, want = planned
    shutil.rmtree(sd, ignore_errors=True)
    status, error, written = "ok", "", 0
    try:
        # what is not stored yet, per extension (a content wanted under two
        # extensions is stored once for each, so each keeps an openable name)
        by_ext = defaultdict(list)
        results = {}
        for (e, b), n in sorted(want.items()):
            if not os.path.exists(os.path.join(
                    cfg.out, store_path(e, b, *where(cfg, org, repo)))):
                by_ext[e].append((b, n))
            elif cfg.per_repo:
                # only this repo writes under its own folder: the file is
                # from an earlier, interrupted attempt of the same repo
                results[(e, b)] = "written"
        if by_ext:
            rp = os.path.join(cfg.repos_root, org, repo)
            if not is_repo_dir(rp):
                raise RuntimeError("no repository at " + rp)
            with ObjectStore(rp) as gp:
                for e, blobs in sorted(by_ext.items()):
                    got = fetch(gp, blobs,
                                lambda b, e=e: os.path.join(
                                    cfg.out, store_path(
                                        e, b, *where(cfg, org, repo))),
                                cfg, job)
                    for b, action in got.items():
                        results[(e, b)] = action
        for row in rows:
            if row[6]:
                continue
            action = results.get((row[3], row[4]), "deduped")
            row[6] = action
            if action not in STORED:
                row[7] = ""
        written = sum(want[k] for k, a in results.items() if a == "written")
    except DiskLow:
        raise
    except Exception as exc:                     # noqa: BLE001 - one repo
        status = "timeout" if job.expired else "error"
        error = "%s: %s" % (type(exc).__name__, exc)
        rows = []
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"),
                 csv_text([MANIFEST_HEADER] + rows))
    rec.update(status=status, seconds=round(time.time() - job.start, 1),
               versions=len(rows), actions=dict(Counter(r[6] for r in rows)),
               bytes_written=written, error=error)
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# combine
# --------------------------------------------------------------------------

def combine(out):
    """manifest.csv, by_extension.csv and summary.md from every repo done,
    plus a count of the files actually under files/ as a check."""
    state = os.path.join(out, STATE)
    acc = defaultdict(Counter)
    stored = defaultdict(dict)                     # ext -> {blob: bytes}
    repos_of = defaultdict(set)
    repos = 0
    comb = os.path.join(out, "manifest.csv")
    with open(comb + ".tmp", "w", newline="", encoding="utf-8",
              errors="surrogateescape") as cf:
        w = csv.writer(cf)
        w.writerow(MANIFEST_HEADER)
        for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
            od = os.path.join(state, org)
            if not os.path.isdir(od):
                continue
            for repo in sorted(os.listdir(od)):
                sd = os.path.join(od, repo)
                done = read_json(os.path.join(sd, "done.json"))
                if not done or done.get("status") != "ok":
                    continue
                repos += 1
                for r in read_rows(os.path.join(sd, "manifest.csv")):
                    w.writerow([r[k] for k in MANIFEST_HEADER])
                    e, a = r["ext"], r["action"]
                    acc[e][a] += 1
                    acc[e]["versions"] += 1
                    acc[e]["why_" + r["why"]] += 1
                    repos_of[e].add((org, repo))
                    if a in STORED:      # one stored file per stored_as
                        stored[e][r["stored_as"]] = int(r["bytes"])
    os.replace(comb + ".tmp", comb)

    on_disk = Counter()
    disk_bytes = Counter()
    root = os.path.join(out, "files")
    for e in os.listdir(root) if os.path.isdir(root) else []:
        for dirpath, _d, names in os.walk(os.path.join(root, e)):
            for n in names:
                if ".tmp." not in n:
                    on_disk[e] += 1
                    disk_bytes[e] += os.path.getsize(os.path.join(dirpath, n))

    cols = ["repos", "versions", "files_stored", "gb_stored", "files_on_disk",
            "gb_on_disk"] + ACTIONS
    order = [e for e in BINARY_EXTS if e in acc] + sorted(
        e for e in acc if e not in BINARY_EXTS)
    table = []
    for e in order:
        a = acc[e]
        table.append([e, len(repos_of[e]), a["versions"], len(stored[e]),
                      round(sum(stored[e].values()) / GB, 3), on_disk[e],
                      round(disk_bytes[e] / GB, 3)] + [a[x] for x in ACTIONS])
    with open(os.path.join(out, "by_extension.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext"] + cols)
        w.writerows(table)

    def n(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    tot = [sum(r[i] for r in table) for i in range(1, len(cols) + 1)]
    md = ["# Binary versions extracted", "",
          "Repositories: %s. Generated %s by extract_versions.py."
          % (f"{repos:,}", datetime.date.today().isoformat()), "",
          "| ext | " + " | ".join(cols) + " |",
          "|---|" + "---:|" * len(cols)]
    md += ["| %s | %s |" % (r[0], " | ".join(n(v) for v in r[1:]))
           for r in table]
    md.append("| **total** | %s |" % " | ".join(
        "" if c == "repos" else n(round(t, 3) if c.startswith("gb") else t)
        for c, t in zip(cols, tot)))
    bad = [r[0] for r in table if r[3] != r[5]]
    md += ["", "files_stored = distinct contents the manifests point at; "
           "files_on_disk = what is under files/. "
           + ("They agree for every extension." if not bad else
              "**They differ for: %s** - a run still going, or files removed "
              "by hand." % ", ".join(bad))]
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    return repos, bad


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="extension_versions.py --out folder "
                                    "(pass 1 and pass 2 done)")
    ap.add_argument("--repos-root", help="the archive repos, <org>/<repo>")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV",
                    help="only the repos in this batch file (repeatable)")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--extensions", help="comma list or file (default: the 18 "
                                         "binary extensions)")
    ap.add_argument("--per-repo", action="store_true",
                    help="store each content once per repo "
                         "(files/<ext>/<org>/<repo>/<blob>.<ext>), not once "
                         "across all repos")
    ap.add_argument("--skip-vendored", action="store_true",
                    help="do not extract files in node_modules, packages, "
                         "bin, obj, ... (listed as skipped_vendored)")
    ap.add_argument("--max-bytes", type=int, default=0,
                    help="do not extract contents bigger than this (listed "
                         "as too_large; default 0 = no limit)")
    ap.add_argument("--min-free-disk-gb", type=float, default=20,
                    help="stop while --out's disk has less free (default 20, "
                         "0 = off)")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos at once (default 8)")
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo after this long (default never)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only repos whose last attempt failed or timed out")
    ap.add_argument("--redo", action="store_true",
                    help="redo repos already done (stored files are kept)")
    ap.add_argument("--combine-only", action="store_true",
                    help="only rebuild manifest.csv, by_extension.csv and "
                         "summary.md")
    ap.add_argument("--no-combine", action="store_true",
                    help="skip the rebuild at the end (a runner combines once)")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    args = ap.parse_args()

    if args.combine_only:
        repos, bad = combine(args.out)
        print("combined %s repo(s) -> %s%s"
              % (f"{repos:,}", os.path.join(args.out, "summary.md"),
                 "  (files on disk differ for: %s)" % ", ".join(bad)
                 if bad else ""))
        return 0
    if not args.state or not args.repos_root:
        sys.exit("--state and --repos-root are needed")
    if not os.path.isdir(os.path.join(args.state, STATE)):
        sys.exit("no _state folder under " + args.state)
    if os.path.realpath(args.out) == os.path.realpath(args.state):
        sys.exit("--out must be a different folder from --state")
    # one layout per folder: a resumed run with the other one would mix them
    layout = "per_repo" if args.per_repo else "shared"
    lpath = os.path.join(args.out, "layout.txt")
    before = open(lpath, encoding="utf-8").read().strip() \
        if os.path.isfile(lpath) else ""
    if before and before != layout:
        sys.exit("%s was written with the %s layout; run it again %s "
                 "--per-repo, or use another --out"
                 % (args.out, before, "with" if before == "per_repo" else "without"))
    if not before:
        os.makedirs(args.out, exist_ok=True)
        write_atomic(lpath, layout + "\n")
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    if args.min_free_disk_gb and free_gb(args.out) < args.min_free_disk_gb:
        print("STOPPED: less than %s GB free on %s"
              % (args.min_free_disk_gb, args.out), file=sys.stderr)
        return 3
    name = args.name or (os.path.splitext(os.path.basename(args.batch[0]))[0]
                         if args.batch else "run")
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
        st = os.path.join(args.state, STATE)
        chosen = [(o, r) for o in sorted(os.listdir(st))
                  if os.path.isdir(os.path.join(st, o))
                  for r in sorted(os.listdir(os.path.join(st, o)))]
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
    print("extract  %s repo(s) to process (%s done, %s failed earlier)"
          % (f"{len(todo):,}",
             f"{sum(1 for v in prior.values() if v == 'ok'):,}",
             f"{sum(1 for v in prior.values() if v not in (None, 'ok')):,}"),
          flush=True)

    class Cfg:
        pass
    cfg = Cfg()
    cfg.state, cfg.repos_root, cfg.out = args.state, args.repos_root, args.out
    cfg.exts = set(load_extensions(args.extensions or ",".join(BINARY_EXTS)))
    cfg.skip_vendored, cfg.max_bytes = args.skip_vendored, args.max_bytes
    cfg.per_repo = args.per_repo
    cfg.min_free_gb = args.min_free_disk_gb
    workers = max(1, args.workers)
    log.info("START %s repos=%d workers=%d exts=%s skip_vendored=%s "
             "max_bytes=%s state=%s out=%s", name, len(todo), workers,
             ",".join(sorted(cfg.exts)), cfg.skip_vendored, cfg.max_bytes,
             args.state, args.out)

    t0 = time.time()
    statuses, actions = Counter(), Counter()
    written = 0
    stopped = ""
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds",
                         "versions", "bytes_written", "actions", "error"])
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
                except DiskLow as exc:
                    statuses["disk_low"] += 1
                    log.error("DISK   %s/%s not done, retried next run: %s",
                              k[0], k[1], exc)
                    if not stopped:
                        stopped = str(exc)
                        log.error("STOP   disk low - no new repos are started")
                    continue
                except Exception as exc:          # noqa: BLE001 - one repo
                    statuses["failed"] += 1
                    log.error("FAIL   %s/%s %s: %s", k[0], k[1],
                              type(exc).__name__, exc)
                    continue
                done += 1
                statuses[rec["status"]] += 1
                actions.update(rec["actions"])
                written += rec["bytes_written"]
                lvl = logging.INFO if rec["status"] == "ok" else logging.ERROR
                log.log(lvl, "done   %s/%s %s %.0fs versions=%d written=%.3fGB "
                        "%s%s", k[0], k[1], rec["status"], rec["seconds"],
                        rec["versions"], rec["bytes_written"] / GB,
                        json.dumps(rec["actions"]),
                        (" error=" + rec["error"]) if rec["error"] else "")
                rw.writerow([rec["finished"], k[0], k[1], rec["status"],
                             rec["seconds"], rec["versions"],
                             rec["bytes_written"], json.dumps(rec["actions"]),
                             rec["error"]])
                rf.flush()
                if done % 200 == 0:
                    el = time.time() - t0
                    print("  %s / %s repos  %.0fs  %.1f GB written  "
                          "(~%.0f min left)"
                          % (f"{done:,}", f"{len(todo):,}", el, written / GB,
                             el / done * (len(todo) - done) / 60),
                          file=sys.stderr, flush=True)
        pool.shutdown()

    repos = "-" if args.no_combine else f"{combine(args.out)[0]:,}"
    summary = ("END %s: %s repo(s) (%s) | %s | %.2f GB written | combined %s "
               "repo(s) | %.0fs"
               % (name, f"{done:,}",
                  ", ".join("%s %d" % kv for kv in statuses.most_common())
                  or "-",
                  ", ".join("%s %s" % (k, f"{v:,}")
                            for k, v in actions.most_common()) or "no versions",
                  written / GB, repos, time.time() - t0))
    log.info(summary)
    print(summary)
    if stopped:
        log.error("STOPPED: %s. Free space on %s, then run the SAME command "
                  "again - finished repos are skipped and stored files are "
                  "not written twice.", stopped, args.out)
        return 3
    if statuses["no_pass2"] or statuses["no_pass1"]:
        log.warning("%d repo(s) skipped: extension_versions.py pass 1 or "
                    "pass 2 not done for them",
                    statuses["no_pass2"] + statuses["no_pass1"])
    return 1 if (statuses["failed"] or statuses["error"]
                 or statuses["timeout"]) else 0


if __name__ == "__main__":
    sys.exit(main())
