#!/usr/bin/env python3
"""
extension_versions.py - per extension, how many versions of every file the
archive's history holds, how big they are, and how much of it is still in
the active copy. Nothing is extracted: file content is only counted.

Made for the next set of extensions to send for PII (the default list):

  text (9)     erb feature bicep lock rst tfvars azcli groovy xcconfig
  binary (18)  xlsx xlsm xlsb xls docx doc pptx pptm msg eml png jpg jpeg
               mpg pfx p7s woff suo

Pass 1 (default), per repo - reads the archive, lists the active copy:
  * one `git log --raw` over every branch (--all, --full-history, merges
    against their first parent - the way file_added_lines.py reads it),
    limited to those extensions, case-insensitive: which commits touched
    which path and the blob (content) each one left. Repos git cannot open
    (objects but no HEAD/refs) are read through RecoveredRepo
  * one `cat-file --batch-check` for the size of every distinct blob; the
    small ones are read to spot Git LFS stubs - the real file is then NOT
    in the archive, only its sha256 and size
  * one walk of <active-root>/<org>/<repo> (.git skipped) for files with
    those extensions: at_head = yes / no, blank when the repo has no folder
    in the active copy; files there that history never had are active_only

Pass 2 (--identical), after pass 1, for the BINARY extensions at head: is
the active file byte-identical to one of its history versions? Its git blob
id (and sha256, for LFS) is computed and compared with the ids pass 1
recorded - no history version is read. A file whose size matches no version
in the repo is not read at all.

  same_as_latest      the newest version (topo order)
  same_as_older       an earlier version (a revert, or another branch)
  same_as_other_path  no version of this path, but a version of another
                      path in the repo (moved / copied)
  no_match            differs from every version in the archive
  lfs_active_real     the path's history holds LFS stubs - the archive never
                      had the content; lfs_match: is the active file the one
                      a stub points to (latest / older / none)
  gone                found in pass 1, not in the active copy any more
  unreadable          there, but could not be read (the error is kept)

A failing active-copy MOUNT (blobfuse "Transport endpoint is not connected",
I/O errors, timeouts) is not recorded as a missing file: the repo gets no
result, no new repos are started, and the run exits with code 3. Remount,
run the same command again, and it carries on.

Output, under --out:
  _state/<org>/<repo>/files.csv     pass 1: one row per path
  _state/<org>/<repo>/versions.csv  pass 1: one row per (path, distinct blob),
                                    with the first and last commit (and
                                    date) that gave the path that content
  _state/<org>/<repo>/done.json     pass 1 outcome, written last (resume)
  _state/<org>/<repo>/identical.csv, identical.json      pass 2
  _logs/<name>.log, <name>_repos.csv
  by_extension.csv   THE summary: one row per extension (columns below)
  by_repo.csv        the same per repo and extension
  all_files.csv      every repo's files.csv, with the pass 2 result joined
  summary.md         by_extension as a readable table

by_extension.csv columns:
  repos                  repos with the extension in history or at head
  files_in_history       distinct paths ever in history
  files_at_head          ... still in the active copy
  files_history_only     ... not in it (deleted, renamed away, branch-only)
  files_no_active_repo   ... in repos with no folder in the active copy
  files_active_only      in the active copy, never in the archive's history
  files_vendored         in node_modules, packages, bin, obj, ... (included
                         in every count above; see explain_file_counts.py)
  commits                commits that touched these files (what `versions`
                         meant in file_added_lines.py's manifests)
  versions_per_file      distinct contents per path, summed
  versions_beyond_head   versions_per_file minus the current version of each
                         file at head (assumed to be its latest)
  versions_per_repo      distinct contents per repo, summed (a copy under a
                         second path counts once)
  versions_all_repos     distinct contents across every repo
  gb_per_file / gb_per_repo / gb_all_repos   their size
  median_bytes, max_bytes                    over versions_all_repos
  lfs_stub_versions      versions that are LFS stubs (content not archived)
  missing_objects        versions whose blob the archive does not have
  pass 2, binary extensions only:
  at_head_checked, same_as_latest, same_as_older, same_as_other_path,
  no_match, lfs_active_real, gone, unreadable, at_head_unchecked (pass 2
  not run for those yet)
  versions_to_send       the head IS processed: every version except those
                         whose content is a file in the SAME repo's active
                         copy (pass 2's same_as_* - that path or another path
                         of the repo) and LFS stubs (nothing to send). A repo
                         with no active copy sends every version, even if
                         another repo's active copy has the same content.
                         Where pass 2 has not run, each binary file's latest
                         version is assumed to be the active one. This is
                         what an extraction of the previous versions writes
  versions_to_send_all_repos, gb_to_send_all_repos
                         the same, each content written once however many
                         repos need it
  versions_to_send_incl_head
                         if the head were NOT processed: every archived
                         version (LFS stubs excluded), plus the active file
                         where the archive lacks its content (no_match,
                         lfs_active_real), plus each active_only file

Usage:
    python3 extension_versions.py --batch batches/S01.csv \\
        --repos-root /data/workarea/archive \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --out /data/workarea/ext_versions --workers 16
    # then, per batch, the identical check (reads the active files)
    python3 extension_versions.py --batch batches/S01.csv --identical \\
        --repos-root /data/workarea/archive \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --out /data/workarea/ext_versions --workers 16
    # rebuild the summaries from every finished repo
    python3 extension_versions.py --out /data/workarea/ext_versions --combine-only
"""

import argparse
import csv
import datetime
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

from explain_file_counts import vendor_of
from explore_input_csv import is_repo_dir
from extract_commits import (FS, GIT, bind_job, parse_log_stream, run_tracked,
                             spawn)
from file_added_lines import _feed, read_blobs
from file_delta import (MOUNT_ERRNOS, MountDown, check_mount, csv_text,
                        repo_exists, write_atomic)
from file_history_for_list import RecoveredRepo, RepoJob, git_can_open
from hash_working_tree import hash_file
from repo_extension_summary import ext_key, load_extensions

TEXT_EXTS = ["erb", "feature", "bicep", "lock", "rst", "tfvars", "azcli",
             "groovy", "xcconfig"]
BINARY_EXTS = ["xlsx", "xlsm", "xlsb", "xls", "docx", "doc", "pptx", "pptm",
               "msg", "eml", "png", "jpg", "jpeg", "mpg", "pfx", "p7s", "woff",
               "suo"]
LFS_MAX = 1024            # an LFS stub is ~130 bytes; spec caps it at 1024

STATE, LOGS = "_state", "_logs"
FILE_HEADER = ["org", "repo", "path", "ext", "group", "vendored", "in_history",
               "at_head", "commits", "versions", "latest_blob", "latest_bytes",
               "bytes_versions", "max_bytes", "lfs_versions",
               "missing_objects", "last_event"]
VERSION_HEADER = ["path", "blob", "bytes", "commits", "latest", "lfs_oid",
                  "lfs_bytes", "first_commit", "first_date", "last_commit",
                  "last_date"]
ID_HEADER = ["path", "active_bytes", "result", "matched_blob", "matched_path",
             "lfs_match", "hashed", "error"]
RESULTS = ["same_as_latest", "same_as_older", "same_as_other_path", "no_match",
           "lfs_active_real", "gone", "unreadable"]
SKIP_MODES = {"120000", "160000"}            # symlink, submodule: no content
log = logging.getLogger("extension_versions")


BINARY = set(BINARY_EXTS)  # + --binary-exts, remembered in <out>/binary_exts.txt
BINARY_FILE = "binary_exts.txt"


def group_of(ext):
    return "binary" if ext in BINARY else "text"


def add_binary(out, exts=()):
    """Treat `exts` as binary too (pass 2, extraction) and remember them in
    <out>/binary_exts.txt, so a later pass 2 or --combine-only on the same
    folder keeps them binary without being told again. -> the extra list."""
    path = os.path.join(out, BINARY_FILE)
    known = []
    if os.path.isfile(path):
        known = load_extensions(path)
    extra = [e for e in known + list(exts) if e not in BINARY_EXTS]
    extra = list(dict.fromkeys(extra))
    if exts and extra != known:
        os.makedirs(out, exist_ok=True)
        write_atomic(path, "".join(e + "\n" for e in extra))
    BINARY.update(extra)
    return extra


# --------------------------------------------------------------------------
# pass 1: the archive
# --------------------------------------------------------------------------

def read_history(gp, exts, job):
    """{path: rec} for every path with one of `exts` that any commit on any
    branch touched. Newest first (no --reverse: git streams it), so the
    first change seen for a path is its last event and the first blob seen
    its latest version. rec = {"commits": n, "blobs": {blob: [commits,
    first sha, first date, last sha, last date]}, "latest": blob,
    "last_event": A/M/D/T} - first / last: the oldest and newest commit (in
    topo order) that gave the path that content."""
    files = {}

    def keep(status, path, old_path):
        return exts is None or ext_key(path) in exts

    cmd = GIT + ["-c", "core.quotePath=false", "-C", gp, "log", "--all",
                 "--stdin", "--full-history", "--topo-order", "--raw", "-z",
                 "--no-abbrev", "--no-renames", "--diff-merges=first-parent",
                 "--format=%x1e%H" + FS + "%cI"]
    # exts=None: every path (all_extension_counts.py)
    specs = [":(glob,icase)**/*." + e for e in sorted(exts or ())]
    job.phase = "reading history"
    with tempfile.TemporaryFile() as err:
        proc = spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=err)
        feeder = threading.Thread(target=_feed, args=(proc.stdin, specs),
                                  daemon=True)
        feeder.start()
        try:
            for sha, info, changes in parse_log_stream(proc.stdout, keep,
                                                       meta=True):
                date = info[0]
                for c in changes:
                    r = files.get(c["path"])
                    if r is None:
                        r = files[c["path"]] = {"commits": 0, "blobs": {},
                                                "latest": None,
                                                "last_event": c["status"][:1]}
                    r["commits"] += 1
                    blob = c["new_blob"]
                    if c["status"][:1] == "D" or not blob \
                            or set(blob) == {"0"} or c["new_mode"] in SKIP_MODES:
                        continue
                    v = r["blobs"].get(blob)
                    if v is None:              # newest first: this is the last
                        v = r["blobs"][blob] = [0, sha, date, sha, date]
                    v[0] += 1
                    v[1], v[2] = sha, date     # ... and this, so far, the first
                    if r["latest"] is None:
                        r["latest"] = blob
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
    return files


def blob_sizes(gp, blobs):
    """{blob: size} via one `cat-file --batch-check`; missing blobs left out."""
    if not blobs:
        return {}
    proc = run_tracked(GIT + ["-C", gp, "cat-file",
                              "--batch-check=%(objectname) %(objectsize)"],
                       input=("\n".join(blobs) + "\n").encode())
    if proc.returncode != 0:
        raise RuntimeError("cat-file failed: "
                           + proc.stderr.decode("utf-8", "replace")[:300])
    out = {}
    for ln in proc.stdout.decode("ascii", "replace").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def lfs_pointer(data):
    """(sha256, size) of the real file if `data` is a Git LFS stub, else None."""
    if not data.startswith(b"version https://git-lfs"):
        return None
    oid, size = "", None
    for ln in data.decode("ascii", "replace").splitlines():
        k, _, v = ln.partition(" ")
        if k == "oid" and v.startswith("sha256:"):
            oid = v[7:].strip().lower()
        elif k == "size" and v.strip().isdigit():
            size = int(v.strip())
    return (oid, size) if oid else None


def active_listing(root, org, repo, exts, job):
    """Repo-relative paths with `exts` under <root>/<org>/<repo> (.git
    skipped), or None if the repo has no folder there. A failing mount
    raises MountDown, never an empty or missing listing."""
    if not repo_exists(root, org, repo):
        return None
    base = os.path.join(root, org, repo)

    def onerror(exc):
        if exc.errno in MOUNT_ERRNOS:
            raise MountDown("%s: %s" % (base, exc))
        check_mount(root)
        raise exc

    job.phase = "listing active copy"
    out = set()
    for dirpath, dirnames, filenames in os.walk(base, onerror=onerror):
        if job.expired:
            raise TimeoutError("timed out while listing the active copy")
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel = os.path.relpath(dirpath, base)
        rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        out.update(rel + f for f in filenames
                   if exts is None or ext_key(f) in exts)
    return out


def process_repo(cfg, org, repo, job):
    """-> (files rows, versions rows, info)."""
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
        hist = read_history(gp, cfg.exts, job)
        job.phase = "blob sizes"
        blobs = sorted({b for r in hist.values() for b in r["blobs"]})
        sizes = blob_sizes(gp, blobs)
        job.phase = "checking LFS stubs"
        small = [b for b in blobs if sizes.get(b, LFS_MAX + 1) <= LFS_MAX]
        lfs = {}
        for b, data in read_blobs(gp, small).items():
            p = lfs_pointer(data)
            if p:
                lfs[b] = p
    finally:
        if recovery:
            recovery.__exit__(None, None, None)

    active = active_listing(cfg.active_root, org, repo, cfg.exts, job)
    info["active_copy"] = active is not None
    frows, vrows = [], []
    for p in sorted(hist):
        r = hist[p]
        e = ext_key(p)
        byts = [sizes[b] for b in r["blobs"] if b in sizes]
        at = "" if active is None else ("yes" if p in active else "no")
        latest = r["latest"] or ""
        frows.append([org, repo, p, e, group_of(e), vendor_of(p), "yes", at,
                      r["commits"], len(r["blobs"]), latest,
                      sizes.get(latest, ""), sum(byts), max(byts, default=0),
                      sum(1 for b in r["blobs"] if b in lfs),
                      sum(1 for b in r["blobs"] if b not in sizes),
                      r["last_event"]])
        for b, (n, fsha, fdate, lsha, ldate) in r["blobs"].items():
            oid, osz = lfs.get(b, ("", ""))
            vrows.append([p, b, sizes.get(b, ""), n,
                          "yes" if b == latest else "", oid,
                          "" if osz is None else osz, fsha, fdate, lsha, ldate])
    for p in sorted((active or set()) - set(hist)):
        e = ext_key(p)
        frows.append([org, repo, p, e, group_of(e), vendor_of(p), "no", "yes",
                      0, 0, "", "", 0, 0, 0, 0, ""])
    return frows, vrows, info


def run_repo(cfg, org, repo, job):
    """Pass 1 for one repo. A MountDown leaves it with no done.json."""
    job.start = time.time()
    bind_job(job)
    check_mount(cfg.active_root)
    sd = os.path.join(cfg.out, STATE, org, repo)
    shutil.rmtree(sd, ignore_errors=True)          # pass 2 results go too
    try:
        frows, vrows, info = process_repo(cfg, org, repo, job)
        status, error = "ok", ""
    except MountDown:
        raise
    except Exception as exc:                       # noqa: BLE001 - one repo
        frows, vrows, info = [], [], {}
        status = "timeout" if job.expired else "error"
        error = "%s: %s" % (type(exc).__name__, exc)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "files.csv"), csv_text([FILE_HEADER] + frows))
    write_atomic(os.path.join(sd, "versions.csv"),
                 csv_text([VERSION_HEADER] + vrows))
    hist = [r for r in frows if r[6] == "yes"]
    rec = {"org": org, "repo": repo, "status": status,
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - job.start, 1),
           "files": len(hist), "active_only": len(frows) - len(hist),
           "at_head": sum(1 for r in hist if r[7] == "yes"),
           "versions": sum(r[9] for r in hist), **info, "error": error}
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# pass 2: identical to a history version?
# --------------------------------------------------------------------------

def read_rows(path):
    with open(path, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        return list(csv.DictReader(fh))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def compare_file(full, root, vers, is_lfs, repo_sizes, repo_blobs):
    """-> ID_HEADER row minus the path. vers = {blob: versions.csv row} of
    this path; repo_blobs = {blob: a path holding it} over the repo (a blob
    not in vers is necessarily some other path's)."""
    try:
        size = os.stat(full).st_size
    except FileNotFoundError:
        check_mount(root)
        return ["", "gone", "", "", "", "no", ""]
    except OSError as exc:
        if exc.errno in MOUNT_ERRNOS:
            raise MountDown("%s: %s" % (full, exc))
        return ["", "unreadable", "", "", "", "no",
                "%s: %s" % (type(exc).__name__, exc)]
    lfs_sizes = {int(v["lfs_bytes"]) for v in vers.values()
                 if v["lfs_bytes"].isdigit()}
    if size not in repo_sizes and size not in lfs_sizes:
        return [size, "lfs_active_real" if is_lfs else "no_match", "", "",
                "none" if is_lfs else "", "no", ""]
    try:
        _md5, sha256, gid, _n = hash_file(full)
    except OSError as exc:
        if exc.errno in MOUNT_ERRNOS:
            raise MountDown("%s: %s" % (full, exc))
        if isinstance(exc, FileNotFoundError):
            check_mount(root)
            return [size, "gone", "", "", "", "no", ""]
        return [size, "unreadable", "", "", "", "no",
                "%s: %s" % (type(exc).__name__, exc)]
    if gid in vers:
        return [size, "same_as_latest" if vers[gid]["latest"] else
                "same_as_older", gid, "", "", "yes", ""]
    if is_lfs:
        hit = [v for v in vers.values() if v["lfs_oid"] == sha256]
        match = ("latest" if any(v["latest"] for v in hit) else
                 "older" if hit else "none")
        return [size, "lfs_active_real", hit[0]["blob"] if hit else "", "",
                match, "yes", ""]
    if gid in repo_blobs:
        return [size, "same_as_other_path", gid, repo_blobs[gid], "", "yes",
                ""]
    return [size, "no_match", "", "", "", "yes", ""]


def run_identical(cfg, org, repo, job):
    """Pass 2 for one repo: the binary files at head against their versions."""
    job.start = time.time()
    bind_job(job)
    sd = os.path.join(cfg.out, STATE, org, repo)
    done = read_json(os.path.join(sd, "done.json"))
    rec = {"org": org, "repo": repo, "status": "no_pass1",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "checked": 0, "results": {}, "error": ""}
    if not done or done.get("status") != "ok":
        return rec                    # nothing written: done after pass 1
    check_mount(cfg.active_root)
    rows = []
    try:
        files = read_rows(os.path.join(sd, "files.csv"))
        by_path = defaultdict(dict)
        repo_blobs, repo_sizes = {}, set()
        for v in read_rows(os.path.join(sd, "versions.csv")):
            by_path[v["path"]][v["blob"]] = v
            repo_blobs.setdefault(v["blob"], v["path"])
            if v["bytes"].isdigit():
                repo_sizes.add(int(v["bytes"]))
        todo = [f for f in files if f["group"] == "binary"
                and f["in_history"] == "yes" and f["at_head"] == "yes"]
        for i, f in enumerate(todo, 1):
            if job.expired:
                raise TimeoutError("timed out after %d of %d files"
                                   % (i - 1, len(todo)))
            job.phase = "comparing %d/%d" % (i, len(todo))
            p = f["path"]
            vers = by_path.get(p, {})
            full = os.path.join(cfg.active_root, org, repo, *p.split("/"))
            rows.append([p] + compare_file(full, cfg.active_root, vers,
                                           int(f["lfs_versions"] or 0) > 0,
                                           repo_sizes, repo_blobs))
        status, error = "ok", ""
    except MountDown:
        raise
    except Exception as exc:                       # noqa: BLE001 - one repo
        status = "timeout" if job.expired else "error"
        error = "%s: %s" % (type(exc).__name__, exc)
    write_atomic(os.path.join(sd, "identical.csv"), csv_text([ID_HEADER] + rows))
    rec.update(status=status, seconds=round(time.time() - job.start, 1),
               checked=len(rows), results=dict(Counter(r[2] for r in rows)),
               hashed=sum(1 for r in rows if r[6] == "yes"), error=error)
    write_atomic(os.path.join(sd, "identical.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# combine
# --------------------------------------------------------------------------

EXT_COLS = ["repos", "files_in_history", "files_at_head", "files_history_only",
            "files_no_active_repo", "files_active_only", "files_vendored",
            "commits", "versions_per_file", "versions_beyond_head",
            "versions_per_repo", "versions_all_repos", "gb_per_file",
            "gb_per_repo", "gb_all_repos", "median_bytes", "max_bytes",
            "lfs_stub_versions", "missing_objects", "at_head_checked"] \
    + RESULTS + ["at_head_unchecked", "versions_to_send",
                 "versions_to_send_all_repos", "gb_to_send_all_repos",
                 "versions_to_send_incl_head"]
BINARY_ONLY = RESULTS + ["at_head_checked", "at_head_unchecked",
                         "versions_to_send", "versions_to_send_all_repos",
                         "gb_to_send_all_repos", "versions_to_send_incl_head"]
REPO_COLS = ["files_in_history", "files_at_head", "files_history_only",
             "files_active_only", "commits", "versions_per_file",
             "versions_per_repo", "bytes_per_repo", "lfs_stub_versions"] \
    + RESULTS + ["versions_to_send", "versions_to_send_incl_head"]
GB = 1e9
PROCESSED = ("same_as_latest", "same_as_older", "same_as_other_path")


def done_repos(out):
    """(org, repo, state dir) of every repo whose pass 1 finished ok."""
    state = os.path.join(out, STATE)
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            sd = os.path.join(od, repo)
            done = read_json(os.path.join(sd, "done.json"))
            if done and done.get("status") == "ok":
                yield org, repo, sd


def processed_in_repo(sd):
    """Blob ids (20 bytes) of one repo whose content is a file in THAT
    repo's active copy - already processed, so not sent for this repo: the
    versions pass 2 found an active file identical to (same path, or another
    path of the same repo). Another repo's active copy never counts. Where
    pass 2 has not run, the latest version of each binary file at head is
    assumed to be the active one."""
    done = set()
    ij = read_json(os.path.join(sd, "identical.json"))
    if ij and ij.get("status") == "ok":
        for r in read_rows(os.path.join(sd, "identical.csv")):
            if r["result"] in PROCESSED and r["matched_blob"]:
                done.add(bytes.fromhex(r["matched_blob"]))
    else:
        for f in read_rows(os.path.join(sd, "files.csv")):
            if f["group"] == "binary" and f["at_head"] == "yes" \
                    and f["latest_blob"]:
                done.add(bytes.fromhex(f["latest_blob"]))
    return done


def combine(out):
    """Rebuild by_extension.csv, by_repo.csv, all_files.csv and summary.md
    from every repo whose pass 1 finished ok."""
    acc = defaultdict(Counter)                   # ext -> column -> n
    repos_of = defaultdict(set)
    everywhere = defaultdict(dict)               # ext -> {blob: bytes}
    to_send = defaultdict(dict)                  # ext -> {blob: bytes}
    per_repo = []
    repos = 0
    comb = os.path.join(out, "all_files.csv")
    with open(comb + ".tmp", "w", newline="", encoding="utf-8",
              errors="surrogateescape") as cf:
        w = csv.writer(cf)
        w.writerow(FILE_HEADER + ["result", "matched_path", "lfs_match"])
        for org, repo, sd in done_repos(out):
            repos += 1
            ij = read_json(os.path.join(sd, "identical.json"))
            ident = {}
            if ij and ij.get("status") == "ok":
                ident = {r["path"]: r for r in
                         read_rows(os.path.join(sd, "identical.csv"))}
            rc = defaultdict(Counter)
            processed = processed_in_repo(sd)
            seen = defaultdict(dict)          # ext -> {blob: bytes} here
            send_of = Counter()               # path -> versions to send
            for v in read_rows(os.path.join(sd, "versions.csv")):
                e = ext_key(v["path"])
                n = int(v["bytes"]) if v["bytes"].isdigit() else 0
                seen[e][v["blob"]] = n
                d = bytes.fromhex(v["blob"])
                everywhere[e].setdefault(d, n)
                if not v["lfs_oid"] and d not in processed:
                    send_of[v["path"]] += 1
                    if group_of(e) == "binary":
                        to_send[e].setdefault(d, n)
            for e, bl in seen.items():
                acc[e]["versions_per_repo"] += len(bl)
                acc[e]["bytes_per_repo"] += sum(bl.values())
                rc[e]["versions_per_repo"] += len(bl)
                rc[e]["bytes_per_repo"] += sum(bl.values())
            for f in read_rows(os.path.join(sd, "files.csv")):
                e = f["ext"]
                a, r = acc[e], rc[e]
                repos_of[e].add((org, repo))
                i = ident.get(f["path"])
                w.writerow([f[k] for k in FILE_HEADER]
                           + ([i["result"], i["matched_path"],
                               i["lfs_match"]] if i else ["", "", ""]))
                if f["in_history"] != "yes":
                    a["files_active_only"] += 1
                    r["files_active_only"] += 1
                    if f["group"] == "binary":        # only copy there is
                        a["versions_to_send_incl_head"] += 1
                        r["versions_to_send_incl_head"] += 1
                    continue
                nv = int(f["versions"])
                for c in (a, r):
                    c["files_in_history"] += 1
                    c["commits"] += int(f["commits"])
                    c["versions_per_file"] += nv
                    c["lfs_stub_versions"] += int(f["lfs_versions"])
                a["files_vendored"] += bool(f["vendored"])
                a["bytes_per_file"] += int(f["bytes_versions"])
                a["missing_objects"] += int(f["missing_objects"])
                at = f["at_head"]
                col = {"yes": "files_at_head", "no": "files_history_only",
                       "": "files_no_active_repo"}[at]
                a[col] += 1
                r[col] += 1
                a["versions_beyond_head"] += max(nv - (at == "yes"), 0)
                if f["group"] != "binary":
                    continue
                # head processed: the versions whose content is in no
                # active file (LFS stubs have nothing to send)
                send = send_of[f["path"]]
                a["versions_to_send"] += send
                r["versions_to_send"] += send
                # head NOT processed: every archived version, plus the
                # active file when the archive lacks its content
                incl = nv - int(f["lfs_versions"])
                if at == "yes":
                    if i:
                        a["at_head_checked"] += 1
                        a[i["result"]] += 1
                        r[i["result"]] += 1
                        if i["result"] in ("no_match", "lfs_active_real"):
                            incl += 1
                    else:
                        a["at_head_unchecked"] += 1
                a["versions_to_send_incl_head"] += incl
                r["versions_to_send_incl_head"] += incl
            for e in sorted(rc):
                per_repo.append([org, repo, e, group_of(e)]
                                + [rc[e][c] for c in REPO_COLS])
    os.replace(comb + ".tmp", comb)

    order = [e for e in TEXT_EXTS + BINARY_EXTS if e in acc] \
        + sorted(e for e in acc if e not in TEXT_EXTS + BINARY_EXTS)
    table = []
    for e in order:
        a = acc[e]
        sizes = sorted(everywhere[e].values())
        a["repos"] = len(repos_of[e])
        a["versions_all_repos"] = len(sizes)
        a["gb_per_file"] = round(a["bytes_per_file"] / GB, 3)
        a["gb_per_repo"] = round(a["bytes_per_repo"] / GB, 3)
        a["gb_all_repos"] = round(sum(sizes) / GB, 3)
        a["median_bytes"] = int(statistics.median(sizes)) if sizes else 0
        a["max_bytes"] = sizes[-1] if sizes else 0
        a["versions_to_send_all_repos"] = len(to_send[e])
        a["gb_to_send_all_repos"] = round(sum(to_send[e].values()) / GB, 3)
        binary = group_of(e) == "binary"
        table.append([e, group_of(e)] + [
            a[c] if binary or c not in BINARY_ONLY else "" for c in EXT_COLS])
    with open(os.path.join(out, "by_extension.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "group"] + EXT_COLS)
        w.writerows(table)
    with open(os.path.join(out, "by_repo.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "ext", "group"] + REPO_COLS)
        w.writerows(per_repo)
    write_summary(out, table, repos)
    return repos, len(order)


def write_summary(out, table, repos):
    ix = {c: i + 2 for i, c in enumerate(EXT_COLS)}

    def n(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    md = ["# Versions per extension", "",
          "Repositories counted: %s. Generated %s by extension_versions.py."
          % (f"{repos:,}", datetime.date.today().isoformat()), ""]
    for grp, title in (("text", "Text extensions - line delta"),
                       ("binary", "Binary extensions - file-level dedup")):
        rows = [r for r in table if r[1] == grp]
        if not rows:
            continue
        cols = ["repos", "files_in_history", "files_at_head",
                "files_history_only", "files_no_active_repo",
                "files_active_only", "commits",
                "versions_per_file", "versions_beyond_head",
                "versions_all_repos", "gb_per_file", "gb_all_repos",
                "lfs_stub_versions"]
        if grp == "binary":
            cols += RESULTS + ["at_head_unchecked", "versions_to_send",
                               "versions_to_send_all_repos",
                               "gb_to_send_all_repos",
                               "versions_to_send_incl_head"]
        md += ["## " + title, "",
               "| ext | " + " | ".join(cols) + " |",
               "|---|" + "---:|" * len(cols)]
        tot = Counter()
        for r in rows:
            md.append("| %s | %s |" % (r[0], " | ".join(n(r[ix[c]])
                                                        for c in cols)))
            for c in cols:
                if isinstance(r[ix[c]], (int, float)) and c != "repos":
                    tot[c] += r[ix[c]]
        md.append("| **total** | %s |" % " | ".join(
            "" if c == "repos" else
            n(round(tot[c], 3) if c.startswith("gb") else tot[c])
            for c in cols))
        md.append("")
    md += ["Column meanings: see the docstring of extension_versions.py. "
           "gb_all_repos and versions_all_repos count each distinct content "
           "once across every repository."]
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")


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
    ap.add_argument("--repos-root", help="the archive repos, <org>/<repo>")
    ap.add_argument("--active-root",
                    help="the active copy, <org>/<repo>/... (the one "
                         "file_delta.py subtracts against)")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV",
                    help="only the repos in this batch file (repeatable)")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--identical", action="store_true",
                    help="pass 2: compare the binary files at head with their "
                         "history versions (needs pass 1 done)")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos at once (default 8)")
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo after this long (default never)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only repos whose last attempt failed or timed out")
    ap.add_argument("--redo", action="store_true",
                    help="reprocess repos already done (pass 1 also drops "
                         "the repo's pass 2 result)")
    ap.add_argument("--combine-only", action="store_true",
                    help="only rebuild by_extension.csv and the rest")
    ap.add_argument("--no-combine", action="store_true",
                    help="skip rebuilding the summaries at the end (a runner "
                         "going through many batches combines once)")
    ap.add_argument("--name", help="log name (default: batch name or 'run', "
                                   "+ '_identical' for pass 2)")
    ap.add_argument("--extensions",
                    help="comma list or file (default: the 27 above, plus "
                         "--binary-exts)")
    ap.add_argument("--binary-exts", metavar="LIST",
                    help="comma list or file: treat these as binary too "
                         "(pass 2 checks them, extract_versions.py writes "
                         "them); remembered in <out>/binary_exts.txt")
    args = ap.parse_args()

    extra = add_binary(args.out, load_extensions(args.binary_exts)
                       if args.binary_exts else ())
    if args.combine_only:
        repos, exts = combine(args.out)
        print("combined %s repo(s), %d extension(s) -> %s"
              % (f"{repos:,}", exts, os.path.join(args.out, "by_extension.csv")))
        return 0
    if not args.repos_root or not args.active_root:
        sys.exit("--repos-root and --active-root are needed")
    try:
        check_mount(args.active_root)
    except MountDown as exc:
        print("STOPPED: %s - remount it and run the same command again" % exc,
              file=sys.stderr)
        return 3
    name = args.name or (os.path.splitext(os.path.basename(args.batch[0]))[0]
                         if args.batch else "run")
    if args.identical and not args.name:
        name += "_identical"
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

    marker = "identical.json" if args.identical else "done.json"
    prior = {}
    for k in chosen:
        rec = read_json(os.path.join(args.out, STATE, k[0], k[1], marker))
        prior[k] = rec.get("status") if rec else None
    if args.redo:
        todo = chosen
    elif args.retry_failed:
        todo = [k for k in chosen if prior[k] not in (None, "ok")]
    else:
        todo = [k for k in chosen if prior[k] is None]
    print("%s  %s repo(s) to process (%s done, %s failed earlier)"
          % ("pass 2 (identical)" if args.identical else "pass 1",
             f"{len(todo):,}",
             f"{sum(1 for v in prior.values() if v == 'ok'):,}",
             f"{sum(1 for v in prior.values() if v not in (None, 'ok')):,}"),
          flush=True)

    class Cfg:
        pass
    cfg = Cfg()
    cfg.out, cfg.repos_root, cfg.active_root = (args.out, args.repos_root,
                                                args.active_root)
    cfg.exts = set(load_extensions(args.extensions or
                                   ",".join(TEXT_EXTS + BINARY_EXTS + extra)))
    worker = run_identical if args.identical else run_repo
    workers = max(1, args.workers)
    log.info("START %s %s repos=%d workers=%d exts=%s active=%s",
             name, "identical" if args.identical else "pass1", len(todo),
             workers, ",".join(sorted(cfg.exts)), args.active_root)

    t0 = time.time()
    statuses, results = Counter(), Counter()
    stopped = ""
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds",
                         "files_or_checked", "detail", "error"])
        pool = ThreadPoolExecutor(max_workers=workers)
        queue, running, done = list(todo), {}, 0
        while queue or running:
            while queue and not stopped and len(running) < workers:
                k = queue.pop(0)
                job = RepoJob(k[0], k[1], 0)
                running[pool.submit(worker, cfg, k[0], k[1], job)] = (k, job)
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
                if args.identical:
                    results.update(rec["results"])
                    size, detail = rec["checked"], json.dumps(rec["results"])
                else:
                    size = rec["files"]
                    detail = "at_head=%s active_only=%s versions=%s%s" % (
                        rec.get("at_head", ""), rec.get("active_only", ""),
                        rec.get("versions", ""),
                        "" if rec.get("active_copy", True)
                        else " no_active_copy")
                lvl = logging.INFO if rec["status"] in ("ok", "no_pass1") \
                    else logging.ERROR
                log.log(lvl, "done   %s/%s %s %.0fs files=%s %s%s", k[0], k[1],
                        rec["status"], rec["seconds"], size, detail,
                        (" error=" + rec["error"]) if rec["error"] else "")
                rw.writerow([rec["finished"], k[0], k[1], rec["status"],
                             rec["seconds"], size, detail, rec["error"]])
                rf.flush()
                if done % 200 == 0:
                    el = time.time() - t0
                    print("  %s / %s repos  %.0fs  (~%.0f min left)"
                          % (f"{done:,}", f"{len(todo):,}", el,
                             el / done * (len(todo) - done) / 60),
                          file=sys.stderr, flush=True)
        pool.shutdown()

    repos = "-" if args.no_combine else f"{combine(args.out)[0]:,}"
    summary = ("END %s: %s repo(s) (%s)%s | combined %s repo(s) | %.0fs"
               % (name, f"{done:,}",
                  ", ".join("%s %d" % kv for kv in statuses.most_common())
                  or "-",
                  (" | results: " + ", ".join("%s %s" % (k, f"{v:,}") for k, v
                                              in results.most_common()))
                  if results else "", repos, time.time() - t0))
    log.info(summary)
    print(summary)
    print("-> %s" % os.path.join(args.out, "by_extension.csv"))
    if stopped:
        left = len(todo) - done - statuses["failed"]
        log.error("STOPPED: the active copy stopped answering (%s). %d repo(s) "
                  "are not done yet. Remount it, then run the SAME command "
                  "again - finished repos are skipped.", stopped, left)
        return 3
    return 1 if (statuses["failed"] or statuses["error"]
                 or statuses["timeout"]) else 0


if __name__ == "__main__":
    sys.exit(main())
