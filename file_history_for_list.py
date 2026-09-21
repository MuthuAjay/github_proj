#!/usr/bin/env python3
"""
file_history_for_list.py - full commit history for every file in a big list.

Input CSV/TSV columns: org, repo, relpath, filename, sha256 (sha256 is carried
through to the output and never used for matching). Each row is looked up in
the repo at <repos-root>/<org>/<repo>.

The work is done once per repo, not once per file: the rows are grouped by
(org, repo), and each repo's history is read with two `git log` passes over
the object store (no checkout, no file content), then joined to the list.
Merges are diffed against their first parent and renames are followed (-M), so
a file that was renamed keeps the commits from before the rename.

Output (in --out): file_summary.csv, one row per input row - the same statistics
as file_churn.csv, looked up for each listed file:

    row org repo relpath filename sha256 matched_path status present_at_head
    commits_touched added modified deleted renamed first_seen last_changed
    branch_count

Optional extras:
  --details   also first_commit/first_author/first_subject, last_commit/
              last_author/last_subject and last_change_type
  --history   also write file_history.csv, one row per change to each matched
              file, oldest first (org repo path commit commit_date author subject
              status nth_change old_path old_blob new_blob); join it to the
              summary on org, repo and matched_path

status is one of:
  found               the path has history in the repo
  in_head_no_history  in the current tree but no commit reached it (shallow?)
  not_found           no history under that path. An old name that was renamed
                      away is still "found" (its history up to the rename, with
                      present_at_head = no); the new name's history includes it
  repo_missing        <repos-root>/<org>/<repo> is not a git repo
  repo_error          git failed on that repo (message in `error`)
  timeout             the repo ran past --repo-timeout and was abandoned; the
                      phase it was in is in `error`, and it is listed in
                      timed_out_repos.tsv
  bad_row             org or repo blank, or no usable path

first_seen / first_commit is the earliest commit of the file by real (UTC) commit
time, and last_changed / last_commit the latest, so both are correct when the
history has parallel branches or mixed time zones. file_churn.csv orders by
git's graph order instead, so on files added on several branches its dates can
differ (yours is the earlier first_seen and the later last_changed). A file
that was renamed away on some other branch but still exists at HEAD keeps its
own history here; file_churn.csv can lose it.

relpath may be a full path like AllRepos\\<org>\\<repo>\\Library\\x.prefs; the
folders up to and including <org>\\<repo> are dropped before matching.

A repo folder that git refuses to open (a .git with the objects but no HEAD or
refs) is recovered automatically: its history is built from every commit
object in the store, and each of its rows carries a "recovered: ..." note in
the `error` column. branch_count is 0 for such a repo unless some refs survive,
and present_at_head is blank without a usable HEAD.

The path is relpath when it already ends with the filename, otherwise
relpath/filename. Backslashes, a leading "./" or "/" and doubled slashes are
normalised; if the path is not found and starts with "<repo>/", the path
without that prefix is tried as well.

While it runs, the bar shows how many repos are in flight and the slowest one
with its current phase; a repo running longer than --warn-after seconds also
gets its own SLOW line. Ctrl+C once stops cleanly (running git processes are
killed, finished repos stay saved) - rerun with --resume.

Usage:
    python file_history_for_list.py input.tsv --repos-root /data/workarea/archive \\
        --out out_dir --workers 8
    (re-run the same command with --resume after an interruption)
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import nullcontext

from explore_input_csv import detect_delimiter, is_repo_dir
from extract_commits import (GIT, Progress, bind_job, branch_membership,
                             changes_from_git, commit_metadata, live_refs,
                             run_tracked, spawn)

SUMMARY_HEADER = ["row", "org", "repo", "relpath", "filename", "sha256",
                  "matched_path", "status", "present_at_head",
                  "last_change_type", "commits_touched", "added", "modified",
                  "deleted", "renamed", "first_seen", "last_changed",
                  "branch_count", "first_commit", "first_author",
                  "first_subject", "last_commit", "last_author",
                  "last_subject", "error"]
# what is written by default (file_churn's columns); --details adds the rest
CORE_COLUMNS = ["row", "org", "repo", "relpath", "filename", "sha256",
                "matched_path", "status", "present_at_head", "commits_touched",
                "added", "modified", "deleted", "renamed", "first_seen",
                "last_changed", "branch_count", "error"]
DETAIL_COLUMNS = ["last_change_type", "first_commit", "first_author",
                  "first_subject", "last_commit", "last_author", "last_subject"]
HISTORY_HEADER = ["org", "repo", "path", "commit", "commit_date", "author",
                  "subject", "status", "nth_change", "old_path", "old_blob",
                  "new_blob"]
DONE_FILE = ".done_repos"


class RepoJob:
    """Bookkeeping for one repo being processed: when it started, which phase
    it is in, and its live git processes, so a watchdog can kill them."""

    def __init__(self, org, repo, rows):
        self.org, self.repo, self.rows = org, repo, rows
        self.start, self.phase = None, "queued"
        self.expired = False
        self.warned = False
        self.big = False
        self._procs, self._lock = [], threading.Lock()

    def register(self, proc):
        with self._lock:
            self._procs = [p for p in self._procs if p.poll() is None] + [proc]
            if self.expired:
                proc.kill()

    def kill(self):
        with self._lock:
            self.expired = True
            for p in self._procs:
                try:
                    p.kill()
                except OSError:
                    pass

    def elapsed(self):
        return time.time() - self.start if self.start else 0.0


def pack_bytes(repo_path):
    """Size of the repo's pack files: a cheap stand-in for how heavy it is."""
    gitdir = os.path.join(repo_path, ".git")
    pack = os.path.join(gitdir if os.path.isdir(gitdir) else repo_path, "objects", "pack")
    try:
        with os.scandir(pack) as it:
            return sum(e.stat().st_size for e in it if e.is_file())
    except OSError:
        return 0


def mem_available_gb():
    """GiB of memory the system says is available (Linux /proc/meminfo), or None."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError):
        pass
    return None


def fmt_dur(sec):
    sec = int(sec)
    return "%dm%02ds" % (sec // 60, sec % 60) if sec < 3600 else \
        "%dh%02dm" % (sec // 3600, sec % 3600 // 60)


def full_path(rel, fname, org="", repo="", strip_root=True):
    """The repo-relative, forward-slash path a row refers to.

    relpath may be a full path such as AllRepos\\<org>\\<repo>\\Library\\x.prefs:
    everything up to and including the <org>/<repo> folders is dropped."""
    rel = (rel or "").strip().replace("\\", "/")
    fname = (fname or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if strip_root and org and repo:
        segs = rel.split("/")
        low = [x.lower() for x in segs]
        for i in range(len(segs) - 1):
            if low[i] == org.lower() and low[i + 1] == repo.lower():
                rel = "/".join(segs[i + 2:])
                break
    if rel == ".":
        rel = ""
    if fname and (rel == fname or rel.endswith("/" + fname)):
        p = rel
    elif rel and fname:
        p = rel.rstrip("/") + "/" + fname
    else:
        p = rel or fname
    p = p.strip("/")
    while "//" in p:
        p = p.replace("//", "/")
    return p


def candidates(path, repo, raw=""):
    out = [path] if path else []
    if path.startswith(repo + "/"):
        out.append(path[len(repo) + 1:])
    if raw and raw not in out:
        out.append(raw)          # in case the "root" folders were part of the repo
    return out


def head_paths(repo_path):
    """Set of paths in HEAD's tree, or None if HEAD can't be resolved."""
    proc = run_tracked(GIT + ["-C", repo_path, "ls-tree", "-r", "-z",
                              "--name-only", "HEAD"])
    if proc.returncode != 0:
        return None
    return set(proc.stdout.decode("utf-8", "surrogateescape").split("\0")) - {""}


def git_can_open(repo_path):
    return subprocess.run(GIT + ["-C", repo_path, "rev-parse", "--git-dir"],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


class RecoveredRepo:
    """A stand-in git repo for a folder git refuses to open (typically a .git
    with the objects intact but HEAD and/or refs gone).

    It is a fresh bare repo that uses the broken repo's object store as an
    alternate, plus a copy of whatever refs / packed-refs / HEAD survive.
    Commits are then every commit object in the store - reachable or not - so
    the history is complete, but branch_count is 0 when no refs survive and
    present_at_head is blank when there is no usable HEAD."""

    NOTE = ("recovered: git could not open this repo (no valid HEAD/refs); "
            "history built from every commit object in the store")

    def __init__(self, repo_path):
        self.repo_path = repo_path
        self.tmp = None

    def __enter__(self):
        gitdir = os.path.join(self.repo_path, ".git")
        if not os.path.isdir(gitdir):
            gitdir = self.repo_path
        objects = os.path.join(gitdir, "objects")
        if not os.path.isdir(objects):
            raise RuntimeError("no objects/ directory in %s" % gitdir)
        self.tmp = tempfile.mkdtemp(prefix="fhl_")
        if run_tracked(GIT + ["init", "--bare", "-q", self.tmp]).returncode != 0:
            raise RuntimeError("git init failed")
        with open(os.path.join(self.tmp, "objects", "info", "alternates"), "w") as fh:
            fh.write(os.path.abspath(objects) + "\n")
        if os.path.isdir(os.path.join(gitdir, "refs")):
            shutil.copytree(os.path.join(gitdir, "refs"),
                            os.path.join(self.tmp, "refs"), dirs_exist_ok=True)
        packed = os.path.join(gitdir, "packed-refs")
        if os.path.isfile(packed):
            shutil.copy(packed, os.path.join(self.tmp, "packed-refs"))
        head = os.path.join(gitdir, "HEAD")
        if os.path.isfile(head):
            with open(head, encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if text.startswith("ref: ") or re.fullmatch(r"[0-9a-f]{40}", text):
                shutil.copy(head, os.path.join(self.tmp, "HEAD"))
        # Stream the object list and keep only the commits: a big repo has
        # millions of objects, and --unordered is much faster than sorted output.
        self.revs = os.path.join(self.tmp, "revs.txt")
        with tempfile.TemporaryFile() as err:
            proc = spawn(GIT + ["-C", self.tmp, "cat-file", "--batch-all-objects",
                                "--unordered",
                                "--batch-check=%(objectname) %(objecttype)"],
                         stdout=subprocess.PIPE, stderr=err)
            with open(self.revs, "w") as fh:
                for raw in proc.stdout:
                    parts = raw.split()
                    if len(parts) == 2 and parts[1] == b"commit":
                        fh.write(parts[0].decode("ascii") + "\n")
            proc.wait()
            if proc.returncode != 0:
                err.seek(0)
                raise RuntimeError("cannot read objects: "
                                   + err.read().decode("utf-8", "replace")[:200])
        return self

    def add_all_commits_as_refs(self):
        """Give every commit object a ref (refs/recovered/<sha>) in packed-refs
        so that plain `git log --all` walks the whole store; used by callers
        that run several separate git commands against one recovered repo."""
        packed = os.path.join(self.tmp, "packed-refs")
        keep = []
        if os.path.isfile(packed):
            with open(packed, encoding="utf-8", errors="replace") as fh:
                keep = [ln.rstrip("\n") for ln in fh if not ln.startswith("#")]
        with open(self.revs) as fh:
            shas = [ln.strip() for ln in fh if ln.strip()]
        with open(packed, "w") as fh:
            fh.write("# pack-refs with: peeled fully-peeled\n")
            for ln in keep:
                fh.write(ln + "\n")
            for sha in shas:
                fh.write("%s refs/recovered/%s\n" % (sha, sha))

    def __exit__(self, *exc):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


def utc(iso):
    """ISO-8601 commit date -> UTC timestamp (0 if unparsable). Comparing the
    strings would misorder dates written with different UTC offsets."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
            timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def commit_dates(repo_path, shas, chunk=4000):
    """{sha: {"committer_date": ...}} - just the dates, without reading full
    commit messages (all that the default output needs)."""
    out = {}
    for i in range(0, len(shas), chunk):
        proc = run_tracked(
            GIT + ["-C", repo_path, "log", "--no-walk", "--stdin",
                   "--format=%H%x1f%cI"],
            input="\n".join(shas[i:i + chunk]).encode())
        if proc.returncode != 0:
            raise RuntimeError("git log failed: "
                               + proc.stderr.decode("utf-8", "replace")[:300])
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            sha, _, date = line.partition("\x1f")
            if sha:
                out[sha] = {"committer_date": date}
    return out


def new_rec():
    return {"n": 0, "added": 0, "modified": 0, "deleted": 0, "renamed": 0,
            "first_sha": "", "last_sha": "", "last_type": "", "hist": []}


def process_repo(root, org, repo, rows, want_history=False, details=False,
                 job=None):
    """-> (summary_rows, history_rows) for one repo. `rows` is a list of
    (rowno, relpath, filename, sha256)."""
    rp = os.path.join(root, org, repo)
    job = job or RepoJob(org, repo, len(rows))
    job.start = time.time()
    bind_job(job)
    prepared = []
    for rowno, rel, fname, sha in rows:
        p = full_path(rel, fname, org, repo)
        prepared.append((rowno, rel, fname, sha, p,
                         candidates(p, repo, full_path(rel, fname, strip_root=False))))

    def blank_rows(status, err=""):
        out = []
        for rowno, rel, fname, sha, p, _c in prepared:
            r = [""] * len(SUMMARY_HEADER)
            r[:6] = [rowno, org, repo, rel, fname, sha]
            r[SUMMARY_HEADER.index("status")] = status if p else "bad_row"
            r[SUMMARY_HEADER.index("error")] = err
            out.append(r)
        return out, []

    if not is_repo_dir(rp):
        return blank_rows("repo_missing")
    job.phase = "opening repo"
    recovery = None if git_can_open(rp) else RecoveredRepo(rp)
    try:
        if recovery:
            job.phase = "recovering repo (listing objects)"
            recovery.__enter__()
            gp, feed = recovery.tmp, recovery.revs
        else:
            gp, feed = rp, None
        wanted = {c for *_x, cands in prepared for c in cands}

        job.phase = "reading renames (pass 1/2)"
        # pass 1: walk newest first (children before parents); whenever a
        # tracked path was created by a rename, its old name is tracked too.
        # Nothing but the tracked set is kept, so memory stays small even for
        # repos with millions of renames.
        track = set(wanted)

        def renamed_from_tracked(status, path, old_path):
            return status[:1] == "R" and bool(old_path) and path in track

        for _sha, changes in changes_from_git(gp, [], None, None, None, feed,
                                              oldest_first=False,
                                              keep=renamed_from_tracked):
            for c in changes:
                track.add(c["old_path"])

        # pass 2: oldest first, keep only tracked paths; a rename moves the
        # old name's record onto the new name
        recs, needed = {}, set()
        # only changes to tracked paths are ever built; the rest are dropped
        # while git's output is being read
        for sha, changes in changes_from_git(
                gp, [], None, None, None, feed,
                keep=lambda status, path, old: path in track or old in track):
            for c in changes:
                st, p, old = c["status"][:1], c["path"], c["old_path"]
                if st == "R" and old in recs:
                    if old in wanted:
                        # the old path may still exist elsewhere (renamed on
                        # another branch only): keep its own history too
                        moved = dict(recs[old], hist=list(recs[old]["hist"]))
                    else:
                        moved = recs.pop(old)
                    cur = recs.get(p)
                    if cur is not None:
                        for k in ("n", "added", "modified", "deleted", "renamed"):
                            moved[k] += cur[k]
                        moved["hist"] += cur["hist"]
                    recs[p] = moved
                rec = recs.setdefault(p, new_rec())
                rec["n"] += 1
                rec["renamed" if st == "R" else "added" if st == "A"
                    else "deleted" if st == "D" else "modified"] += 1
                rec["hist"].append(
                    (sha, c["status"], old, c["old_blob"], c["new_blob"], rec["n"])
                    if want_history else (sha, c["status"]))
                needed.add(sha)

        job.phase = "reading commit dates"
        if not needed:
            meta = {}
        elif details or want_history:
            meta = commit_metadata(gp, sorted(needed))
        else:
            meta = commit_dates(gp, sorted(needed))
        job.phase = "counting branches"
        member = (branch_membership(gp, live_refs(gp), needed)
                  if needed else {})
        job.phase = "listing current files"
        head = head_paths(gp)
    except Exception as exc:                       # noqa: BLE001 - per-repo isolation
        if job.expired:
            return blank_rows("timeout", "gave up after %s during: %s"
                              % (fmt_dur(job.elapsed()), job.phase))
        return blank_rows("repo_error", "%s: %s" % (type(exc).__name__, str(exc)[:200]))
    finally:
        bind_job(None)
        if recovery:
            recovery.__exit__()

    def m(sha, key):
        return meta.get(sha, {}).get(key, "")

    # first / last change by commit time (UTC); ties fall back to git's order
    for rec in recs.values():
        keyed = [(utc(m(h[0], "committer_date")), i, h)
                 for i, h in enumerate(rec["hist"])]
        rec["first_sha"] = min(keyed)[2][0]
        last = max(keyed)[2]
        rec["last_sha"], rec["last_type"] = last[0], last[1]

    summary, history, emitted = [], [], set()
    for rowno, rel, fname, sha256, p, cands in prepared:
        r = [""] * len(SUMMARY_HEADER)
        r[:6] = [rowno, org, repo, rel, fname, sha256]
        if recovery:
            r[SUMMARY_HEADER.index("error")] = RecoveredRepo.NOTE
        if not p:
            r[SUMMARY_HEADER.index("status")] = "bad_row"
            summary.append(r)
            continue
        matched = next((c for c in cands if c in recs), None)
        if matched is None:
            matched = next((c for c in cands if head and c in head), cands[0])
        rec = recs.get(matched)
        in_head = head is not None and matched in head
        vals = {"matched_path": matched,
                "present_at_head": ("yes" if in_head else "no")
                if head is not None else ""}
        if rec:
            vals.update(status="found", last_change_type=rec["last_type"],
                        commits_touched=rec["n"], added=rec["added"],
                        modified=rec["modified"], deleted=rec["deleted"],
                        renamed=rec["renamed"],
                        first_seen=m(rec["first_sha"], "committer_date"),
                        last_changed=m(rec["last_sha"], "committer_date"),
                        branch_count=len(set().union(
                            *(member.get(h[0], ()) for h in rec["hist"]))),
                        first_commit=rec["first_sha"],
                        first_author=m(rec["first_sha"], "author_name"),
                        first_subject=(m(rec["first_sha"], "subject") or "")
                        .replace("\n", " "),
                        last_commit=rec["last_sha"],
                        last_author=m(rec["last_sha"], "author_name"),
                        last_subject=(m(rec["last_sha"], "subject") or "")
                        .replace("\n", " "))
            if want_history and (org, repo, matched) not in emitted:
                emitted.add((org, repo, matched))
                for sha, status, old, ob, nb, nth in rec["hist"]:
                    history.append([org, repo, matched, sha,
                                    m(sha, "committer_date"), m(sha, "author_name"),
                                    (m(sha, "subject") or "").replace("\n", " "),
                                    status, nth, old, ob, nb])
        else:
            vals["status"] = "in_head_no_history" if in_head else "not_found"
        for k, v in vals.items():
            r[SUMMARY_HEADER.index(k)] = v
        summary.append(r)
    return summary, history


def read_input(path, delimiter):
    """-> ({(org, repo): [(rowno, relpath, filename, sha256)]}, bad_rows,
    total_rows). rowno is the 1-based data-row number."""
    groups, bad, total = defaultdict(list), [], 0
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        header = [h.strip().lower() for h in next(reader)]
        need = ["org", "repo", "relpath", "filename"]
        missing = [c for c in need if c not in header]
        if missing:
            raise SystemExit("missing column(s) %s; found %s" % (missing, header))
        ix = {c: header.index(c) for c in need}
        sx = header.index("sha256") if "sha256" in header else None
        width = len(header)
        for rowno, row in enumerate(reader, 1):
            total += 1
            row = row + [""] * (width - len(row))
            org, repo = row[ix["org"]].strip(), row[ix["repo"]].strip()
            item = (rowno, row[ix["relpath"]], row[ix["filename"]],
                    row[sx] if sx is not None else "")
            if not org or not repo:
                bad.append((rowno, org, repo) + item[1:])
            else:
                groups[(org, repo)].append(item)
    return groups, bad, total


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="input CSV/TSV (org, repo, relpath, filename, sha256)")
    ap.add_argument("--repos-root", required=True,
                    help="folder holding <org>/<repo>, e.g. /data/workarea/archive")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--delimiter", help="field delimiter (default: auto-detect)")
    ap.add_argument("--workers", type=int, default=8, help="repos processed in parallel")
    ap.add_argument("--resume", action="store_true",
                    help="skip repos already finished in a previous run and "
                         "append to the existing outputs")
    ap.add_argument("--details", action="store_true",
                    help="add first/last commit, author, subject and "
                         "last_change_type to file_summary.csv")
    ap.add_argument("--history", action="store_true",
                    help="also write file_history.csv (one row per change to "
                         "each matched file)")
    ap.add_argument("--big-repo-gb", type=float, default=1.0, metavar="GB",
                    help="a repo whose pack files total at least this much counts "
                         "as big (default 1)")
    ap.add_argument("--max-big-repos", type=int, default=0, metavar="N",
                    help="run at most N big repos at the same time, so that "
                         "several huge repos cannot exhaust memory (default 0 = "
                         "one quarter of --workers, at least 1)")
    ap.add_argument("--min-free-gb", type=float, default=16, metavar="GB",
                    help="do not start another repo while the system has less than "
                         "this much memory available (repos already running are "
                         "left to finish; one always runs). Default 16, 0 = off")
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo that runs longer than this: its git "
                         "processes are killed, its rows get status `timeout` "
                         "and the run carries on (default 0 = never)")
    ap.add_argument("--warn-after", type=float, default=300, metavar="SECONDS",
                    help="print a SLOW line for a repo running longer than this "
                         "(default 300, 0 = off)")
    ap.add_argument("--quiet", action="store_true", help="no progress bar")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        print("not a file: %s" % args.csv_in, file=sys.stderr)
        return 2
    if not os.path.isdir(args.repos_root):
        print("not a directory: %s" % args.repos_root, file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    delim = args.delimiter or detect_delimiter(args.csv_in)
    groups, bad, total = read_input(args.csv_in, delim)
    print("input     %s rows, %s repo(s), read in %.1fs"
          % (f"{total:,}", f"{len(groups):,}", time.time() - t0))

    done_path = os.path.join(args.out, DONE_FILE)
    done = set()
    sum_path = os.path.join(args.out, "file_summary.csv")
    his_path = os.path.join(args.out, "file_history.csv")
    resuming = args.resume and os.path.exists(sum_path)
    if resuming and os.path.exists(done_path):
        with open(done_path, encoding="utf-8") as fh:
            done = {tuple(ln.rstrip("\n").split("\t")) for ln in fh if ln.strip()}
    if args.resume and not resuming:
        print("warning: --resume given but %s does not exist, so this starts from "
              "scratch. --resume only continues an earlier run in the SAME --out "
              "folder." % sum_path, file=sys.stderr)
    mode = "a" if resuming else "w"
    out_cols = CORE_COLUMNS + (DETAIL_COLUMNS if args.details else [])
    pick = [SUMMARY_HEADER.index(c) for c in out_cols]

    todo = sorted((k for k in groups if k not in done),
                  key=lambda k: -len(groups[k]))
    rows_done = sum(len(groups[k]) for k in done if k in groups)
    counts, hist_rows = Counter(), 0
    bar = Progress("files", total, not args.quiet)

    kw = dict(newline="", encoding="utf-8", errors="surrogateescape")
    with open(sum_path, mode, **kw) as sf, \
            open(his_path, mode, **kw) if args.history else nullcontext() as hf, \
            open(done_path, mode if resuming else "w", encoding="utf-8") as df:
        sw = csv.writer(sf)
        hw = csv.writer(hf) if args.history else None
        if not resuming:
            sw.writerow(out_cols)
            if hw:
                hw.writerow(HISTORY_HEADER)
        if bad and not resuming:
            for rowno, org, repo, rel, fname, sha in bad:
                r = [""] * len(SUMMARY_HEADER)
                r[:6] = [rowno, org, repo, rel, fname, sha]
                r[SUMMARY_HEADER.index("status")] = "bad_row"
                sw.writerow([r[i] for i in pick])
            counts["bad_row"] += len(bad)
        rows_done += len(bad)

        status_ix = SUMMARY_HEADER.index("status")
        pool = ThreadPoolExecutor(max_workers=args.workers)
        jobs = {}                       # future -> RepoJob, only repos in flight
        todo_iter = iter(todo)
        repos_done = len(done)
        timed_out, interrupted = [], False

        max_big = args.max_big_repos or max(1, args.workers // 4)
        big_bytes = args.big_repo_gb * 1024 ** 3
        deferred = []                   # big repos waiting for a free big slot

        def next_repo():
            big_running = sum(1 for j in jobs.values() if j.big)
            while True:
                key = next(todo_iter, None)
                if key is None:
                    break
                if pack_bytes(os.path.join(args.repos_root, *key)) >= big_bytes:
                    if big_running < max_big:
                        return key, True
                    deferred.append(key)
                    continue
                return key, False
            if deferred and big_running < max_big:
                return deferred.pop(0), True
            return None, False

        def submit_next():              # keep the queue short: 2 repos per worker
            while len(jobs) < args.workers * 2:
                avail = mem_available_gb()
                if (args.min_free_gb and jobs and avail is not None
                        and avail < args.min_free_gb):
                    return              # wait for memory to come back
                key, big = next_repo()
                if key is None:
                    return
                job = RepoJob(key[0], key[1], len(groups[key]))
                job.big = big
                jobs[pool.submit(process_repo, args.repos_root, key[0], key[1],
                                 groups[key], args.history, args.details,
                                 job)] = job

        def status_text():
            run = [j for j in jobs.values() if j.start]
            text = "%s/%s repos" % (f"{repos_done:,}", f"{len(groups):,}")
            avail = mem_available_gb()
            if avail is not None:
                text += " | %.0fG free" % avail
            if run:
                slow = max(run, key=RepoJob.elapsed)
                name = "%s/%s" % (slow.org, slow.repo)
                name = name if len(name) <= 32 else name[:31] + "~"
                text += " | %d running, slowest %s %s [%s]" % (
                    len(run), name, fmt_dur(slow.elapsed()), slow.phase)
            return text

        try:
            submit_next()
            while jobs or deferred:
                if not jobs:
                    submit_next()
                finished, _ = wait(list(jobs), timeout=1, return_when=FIRST_COMPLETED)
                for fut in finished:
                    job = jobs.pop(fut)
                    summary, history = fut.result()
                    sw.writerows([[r[i] for i in pick] for r in summary])
                    if hw:
                        hw.writerows(history)
                    for r in summary:
                        counts[r[status_ix]] += 1
                    if summary and summary[0][status_ix] == "timeout":
                        timed_out.append((job, summary[0][SUMMARY_HEADER.index("error")]))
                    hist_rows += len(history)
                    sf.flush()
                    if hw:
                        hf.flush()
                    df.write("%s\t%s\n" % (job.org, job.repo))
                    df.flush()
                    repos_done += 1
                    rows_done += len(summary)
                submit_next()
                for job in jobs.values():            # watchdog + slow warnings
                    if not job.start or job.expired:
                        continue
                    if args.repo_timeout and job.elapsed() > args.repo_timeout:
                        job.kill()
                    elif (args.warn_after and job.elapsed() > args.warn_after
                          and not job.warned):
                        job.warned = True
                        print(("\n" if bar.tty else "") + "SLOW      %s/%s running %s "
                              "(%s rows) [%s]" % (job.org, job.repo,
                                                  fmt_dur(job.elapsed()),
                                                  f"{job.rows:,}", job.phase),
                              file=sys.stderr, flush=True)
                bar.update(rows_done, status_text())
            bar.close("%s/%s repos" % (f"{repos_done:,}", f"{len(groups):,}"))
        except KeyboardInterrupt:
            interrupted = True
            for job in list(jobs.values()):     # stop every running git process
                job.kill()
            pool.shutdown(wait=False, cancel_futures=True)
        finally:
            pool.shutdown(wait=True)
            sf.flush()
            if hw:
                hf.flush()
            df.flush()
            if timed_out:
                mode_t = "a" if resuming and os.path.exists(
                    os.path.join(args.out, "timed_out_repos.tsv")) else "w"
                with open(os.path.join(args.out, "timed_out_repos.tsv"), mode_t,
                          encoding="utf-8") as tf:
                    if mode_t == "w":
                        tf.write("org\trepo\trows\tdetail\n")
                    for job, detail in timed_out:
                        tf.write("%s\t%s\t%d\t%s\n" % (job.org, job.repo,
                                                         job.rows, detail))

    if interrupted:
        print("\ninterrupted: %s of %s repo(s) finished and are saved in %s.\n"
              "Run the same command with --resume to carry on."
              % (f"{repos_done:,}", f"{len(groups):,}", args.out), file=sys.stderr)
        return 130

    print("\ndone      %s row(s)%s -> %s"
          % (f"{sum(counts.values()):,}",
             (", %s history row(s)" % f"{hist_rows:,}") if args.history else "",
             args.out))
    for k in ("found", "in_head_no_history", "not_found", "repo_missing",
              "repo_error", "timeout", "bad_row"):
        if counts[k]:
            print("  %-20s %12s" % (k, f"{counts[k]:,}"))
    if timed_out:
        print("  %d repo(s) timed out (see timed_out_repos.tsv); rerun those rows "
              "with a larger --repo-timeout" % len(timed_out))
    print("  %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
