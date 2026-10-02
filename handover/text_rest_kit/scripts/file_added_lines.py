#!/usr/bin/env python3
"""
file_added_lines.py - for every file, every line it has ever had, once each,
as one plain .txt: the content of all its versions merged, with duplicates
dropped.

Per file, in the order the lines first appeared (oldest commit first):

  * version 1 counts as entirely added, so all of it is in
  * every later version adds only the lines its commit ADDED; removed lines
    were added by an earlier commit, so they are already in
  * each line is stripped of spaces/tabs at both ends, blank lines are
    dropped, and a line already written for this file is not written again
    (so re-indenting a script adds nothing)

The .txt holds file content only - no headers, commit ids, authors or dates.

Two ways to say which files:

  history mode   --batch batches/S01.csv (org, repo per row) and/or
                 --repo ORG/REPO, no input CSV: EVERY file that ever existed
                 in the repo's history, on any branch, whose extension is in
                 the list - deleted files and node_modules included
  list mode      an input CSV (file_summary.csv from file_history_for_list.py,
                 or its deduped version): only the files it lists, by
                 matched_path, status `found` only. --batch / --repo narrow it
                 to those repos

Extensions: the list repo_extension_summary.py uses (json, cs, ts, sql, ...),
matched the same way (.gitignore counts as "gitignore"); --extensions picks
another list, --all-extensions takes every file.

History covers every branch (--all), or for a repo git cannot open (objects
but no HEAD/refs) every commit in its object store. Merges are diffed against
their first parent. Renames are not followed: the old and the new name are
two files. Binary files get no .txt; UTF-16 text (common for .sql/.ps1 saved
on Windows), which git treats as binary, is decoded version by version.

Output, under --out:
  <org>/<repo>/<path>.txt        the content, nothing else in these folders
  _state/<org>/<repo>/manifest.csv
                                 one row per file: output, status, at_head
                                 (still in HEAD's tree), versions (commits
                                 that touched it), lines added across all
                                 versions, lines kept, bytes written
  _state/<org>/<repo>/done.json  the repo's outcome - written LAST, so its
                                 presence means the repo is complete
  _logs/<name>.log               run log: settings, one line per repo start /
                                 finish, slow repos, errors, the totals
  _logs/<name>_repos.csv         one row per repo processed, appended

Resume is always on: a repo with a done.json is skipped. A repo that failed
(repo_error, timeout, repo_missing) also has one, so it is not retried every
run - --retry-failed reruns only those, --redo reruns everything given. A
repo interrupted part-way has no done.json: its output and state are cleared
and it starts again from scratch, so no half-written file survives.

Guards: --repo-timeout kills a stuck repo; --min-free-gb holds back new repos
while memory is low; --min-free-disk-gb and --until HH:MM stop starting new
repos (those running finish) - the exit code is then 3, and running the same
command again carries on where it stopped.

Usage:
    python3 file_added_lines.py --batch batches/S01.csv \\
        --repos-root /data/workarea/archive --out /data/workarea/added_lines \\
        --workers 16 --repo-timeout 900
    python3 file_added_lines.py --repo ey-org/atl-program \\
        --repos-root /data/workarea/archive --out test_out
    python3 file_added_lines.py file_summary_github.csv --repo ey-org/atl-program \\
        --repos-root /data/workarea/archive --out test_out_list
"""

import argparse
import csv
import datetime
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
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from explore_input_csv import detect_delimiter, is_repo_dir
from extract_commits import GIT, Progress, bind_job, spawn
from file_history_for_list import (RecoveredRepo, RepoJob, full_path,
                                   git_can_open, head_paths, mem_available_gb)
from repo_extension_summary import ext_key, load_extensions

STATE = "_state"
LOGS = "_logs"
MANIFEST_HEADER = ["org", "repo", "path", "output", "status", "at_head",
                   "versions", "lines_added", "lines_kept", "bytes", "error"]
REPOS_HEADER = ["finished", "batch", "org", "repo", "status", "seconds",
                "files_seen", "files_written", "lines_kept", "bytes", "error"]
FLUSH_CHARS = 64 * 1024 * 1024      # buffered text per repo before writing out
NAME_MAX = 255                      # bytes per path component (ext4, xfs)
PATH_MAX = 4000

log = logging.getLogger("file_added_lines")


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_input(path, wanted_exts=None):
    """List mode: -> {(org, repo): sorted repo-relative paths}, rows read,
    rows used. wanted_exts: keep only paths with these extensions."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    groups = defaultdict(set)
    total = used = 0
    with open(path, newline="", encoding="utf-8-sig",
              errors="surrogateescape") as fh:
        reader = csv.reader(fh, delimiter=detect_delimiter(path))
        header = [h.strip().lower() for h in next(reader, [])]
        has = set(header)
        if not {"org", "repo"} <= has or not (
                "matched_path" in has or "relpath" in has):
            sys.exit("need org, repo and matched_path or relpath; found %s"
                     % header)
        ix = {c: header.index(c) for c in header}
        width = len(header)
        for row in reader:
            total += 1
            if len(row) < width:
                row = row + [""] * (width - len(row))
            if "status" in ix and row[ix["status"]] != "found":
                continue
            org, repo = row[ix["org"]].strip(), row[ix["repo"]].strip()
            if "matched_path" in ix and row[ix["matched_path"]]:
                p = row[ix["matched_path"]]
            else:
                p = full_path(row[ix["relpath"]] if "relpath" in ix else "",
                              row[ix["filename"]] if "filename" in ix else "",
                              org, repo)
            if wanted_exts is not None and ext_key(p) not in wanted_exts:
                continue
            if org and repo and p:
                groups[(org, repo)].add(p)
                used += 1
    return {k: sorted(v) for k, v in groups.items()}, total, used


def read_batch(path):
    """A batch file from make_batches.py (org, repo, ...) -> [(org, repo)]
    in file order (heaviest first)."""
    with open(path, newline="", encoding="utf-8-sig",
              errors="surrogateescape") as fh:
        reader = csv.DictReader(fh, delimiter=detect_delimiter(path))
        if not reader.fieldnames or not {"org", "repo"} <= {
                f.strip().lower() for f in reader.fieldnames}:
            sys.exit("%s: need org and repo columns" % path)
        out = []
        for row in reader:
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            if row["org"] and row["repo"] and row["org"] != "(all)":
                out.append((row["org"], row["repo"]))
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------
# per-file accumulation
# --------------------------------------------------------------------------

def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")     # Windows-saved scripts


def too_long(out_path, rel_parts):
    if len(out_path.encode("utf-8", "surrogateescape")) > PATH_MAX:
        return True
    return any(len(p.encode("utf-8", "surrogateescape")) > NAME_MAX
               for p in rel_parts)


class FileLines:
    """The distinct stripped lines of one file, in first-seen order. Lines
    are buffered and appended to the .txt in batches; only a hash per line
    already written stays in memory."""

    __slots__ = ("out", "seen", "buf", "started", "versions", "added",
                 "kept", "bytes", "binary_blobs", "too_long")

    def __init__(self, out, is_too_long=False):
        self.out = out
        self.seen = set()
        self.buf = []
        self.started = False
        self.versions = self.added = self.kept = self.bytes = 0
        self.binary_blobs = []
        self.too_long = is_too_long

    def add(self, text):
        s = text.strip()
        if not s:
            return 0
        self.added += 1
        h = hash(s)
        if h in self.seen:
            return 0
        self.seen.add(h)
        self.kept += 1
        if self.too_long:
            return 0                  # counted, never written
        self.buf.append(s)
        return len(s) + 1

    def flush(self):
        if not self.buf:
            return
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        data = "\n".join(self.buf) + "\n"
        with open(self.out, "a" if self.started else "w", encoding="utf-8",
                  errors="surrogateescape", newline="\n") as fh:
            fh.write(data)
        self.bytes += len(data.encode("utf-8", "surrogateescape"))
        self.started = True
        self.buf = []


# --------------------------------------------------------------------------
# git log -p parsing
# --------------------------------------------------------------------------

def unquote_c(s):
    """git's C-style quoted path ("a/tab\\there") -> raw bytes."""
    s = s[1:-1] if s.startswith(b'"') and s.endswith(b'"') else s
    out, i = bytearray(), 0
    esc = {b"n": 10, b"t": 9, b'"': 34, b"\\": 92, b"a": 7, b"b": 8,
           b"f": 12, b"r": 13, b"v": 11}
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            nxt = s[i + 1:i + 2]
            if nxt in esc:
                out.append(esc[nxt])
                i += 2
                continue
            if nxt.isdigit():
                out.append(int(s[i + 1:i + 4], 8))
                i += 4
                continue
        out += c
        i += 1
    return bytes(out)


def diff_path(line):
    """Path from `diff --git a/<p> b/<p>` (renames are off, so both sides
    are the same path)."""
    rest = line[len(b"diff --git "):].rstrip(b"\n")
    if rest.startswith(b'"'):
        end = rest.index(b'" ', 1) if b'" ' in rest else len(rest)
        raw = unquote_c(rest[:end + 1])
        raw = raw[2:] if raw.startswith(b"a/") else raw
    else:
        raw = rest[2:2 + (len(rest) - 5) // 2]      # a/<p> b/<p>
    return raw.decode("utf-8", "surrogateescape")


def log_cmd(gp):
    return GIT + ["-c", "core.quotePath=false",
                  "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
                  "-c", "diff.relative=false", "-C", gp, "log", "--all",
                  "--stdin", "--full-history", "--topo-order", "--reverse",
                  "-p", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                  "--no-renames", "--full-index", "--src-prefix=a/",
                  "--dst-prefix=b/", "--diff-merges=first-parent",
                  "--format=%x1e%H"]


def pathspecs(paths, exts):
    """What git log is limited to: the listed paths, literally (list mode),
    or one case-insensitive glob per extension at any depth (history mode);
    [] = everything."""
    if paths is not None:
        return [":(literal)" + p for p in paths]
    if exts is None:
        return []
    return [":(glob,icase)**/*." + e for e in sorted(exts)]


def read_blobs(gp, shas):
    """{sha: bytes} via one `cat-file --batch`."""
    out = {}
    if not shas:
        return out
    proc = spawn(GIT + ["-C", gp, "cat-file", "--batch"],
                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL)
    try:
        for sha in shas:
            proc.stdin.write(sha.encode() + b"\n")
            proc.stdin.flush()
            head = proc.stdout.readline().split()
            if len(head) < 3 or head[1] == b"missing":
                continue
            size = int(head[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)
            out[sha] = body
    finally:
        proc.stdin.close()
        proc.wait()
    return out


def utf16_text(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return None


def _feed(stdin, specs):
    try:
        stdin.write(b"--\n")
        for p in specs:
            stdin.write(p.encode("utf-8", "surrogateescape") + b"\n")
        stdin.close()
    except (BrokenPipeError, OSError):
        pass


def collect(gp, out_root, job, paths=None, exts=None):
    """Stream the repo's history once and fill a FileLines per file: the
    listed `paths` (list mode) or every path with an extension in `exts`
    (history mode, paths=None)."""

    def new_file(p):
        parts = p.split("/")
        out = os.path.join(out_root, *parts) + ".txt"
        return FileLines(out, too_long(out, parts[:-1] + [parts[-1] + ".txt"]))

    files = {p: new_file(p) for p in paths} if paths is not None else {}
    buffered = 0
    job.phase = "reading history"
    with tempfile.TemporaryFile() as err:
        proc = spawn(log_cmd(gp), stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=err)
        # the pathspecs go in on stdin after "--": no argv limit however many
        feeder = threading.Thread(target=_feed,
                                  args=(proc.stdin, pathspecs(paths, exts)),
                                  daemon=True)
        feeder.start()
        cur = blob = None
        in_hunk = False

        def close_section():
            # a section that ends with no hunk but a new blob is binary (or
            # UTF-16 text, which git will not diff); deletions have a null
            # blob and mode-only changes no index line, so neither lands here
            if cur is not None and not in_hunk and blob and set(blob) != {"0"}:
                cur.binary_blobs.append(blob)

        try:
            for line in proc.stdout:
                if line.startswith(b"\x1e") or line.startswith(b"diff --git "):
                    close_section()
                    cur, blob, in_hunk = None, None, False
                    if line.startswith(b"diff --git "):
                        p = diff_path(line)
                        cur = files.get(p)
                        if cur is None and paths is None and (
                                exts is None or ext_key(p) in exts):
                            cur = files[p] = new_file(p)
                        if cur is not None:
                            cur.versions += 1
                elif cur is None:
                    continue
                elif in_hunk:
                    if line.startswith(b"+"):
                        buffered += cur.add(decode(line[1:].rstrip(b"\n")))
                elif line.startswith(b"@@"):
                    in_hunk = True
                elif line.startswith(b"index "):
                    # index <old>..<new>[ <mode>]
                    blob = line.split()[1].split(b"..")[-1].decode()
                if buffered > FLUSH_CHARS:
                    for f in files.values():
                        f.flush()
                    buffered = 0
            close_section()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
            feeder.join(timeout=1)
        if job.expired:
            raise TimeoutError("repo timed out while reading history")
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError("git log failed: "
                               + err.read().decode("utf-8", "replace")[:300])

    # UTF-16 text is binary to git: decode every such version here and add
    # its lines like any other; what is left in binary_blobs is truly binary
    job.phase = "decoding UTF-16"
    for f in files.values():
        if not f.binary_blobs:
            continue
        blobs, f.binary_blobs = list(dict.fromkeys(f.binary_blobs)), []
        for sha, data in read_blobs(gp, blobs).items():
            text = utf16_text(data)
            if text is not None:
                for ln in text.splitlines():
                    f.add(ln)
            elif data:
                f.binary_blobs.append(sha)
    job.phase = "writing"
    for f in files.values():
        f.flush()
    return files


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def safe_parts(p):
    return all(x not in ("", ".", "..") for x in p.split("/"))


def state_dir(out, org, repo):
    return os.path.join(out, STATE, org, repo)


def read_done(out, org, repo):
    try:
        with open(os.path.join(state_dir(out, org, repo), "done.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape",
              newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def clear_repo(out, org, repo):
    """Remove a repo's content and state before (re)processing it, so an
    interrupted earlier attempt leaves nothing behind."""
    root = os.path.realpath(out)
    for d in (os.path.join(out, org, repo), state_dir(out, org, repo)):
        real = os.path.realpath(d)
        if real.startswith(root + os.sep) and real != root:
            shutil.rmtree(d, ignore_errors=True)


def process_repo(cfg, org, repo, paths, job):
    """-> (repo status, manifest rows, error). paths=None: history mode."""
    rp = os.path.join(cfg.repos_root, org, repo)
    out_root = os.path.join(cfg.out, org, repo)
    if not is_repo_dir(rp):
        return "repo_missing", [], "no repository at " + rp
    rows = []
    good = paths
    if paths is not None:
        good = [p for p in paths if safe_parts(p)]
        rows += [[org, repo, p, "", "bad_path", "", 0, 0, 0, 0, ""]
                 for p in paths if not safe_parts(p)]
    recovery = None if git_can_open(rp) else RecoveredRepo(rp)
    try:
        if recovery:
            job.phase = "recovering repo (listing objects)"
            recovery.__enter__()
            recovery.add_all_commits_as_refs()
            gp = recovery.tmp
        else:
            gp = rp
        files = collect(gp, out_root, job, good, cfg.exts)
        job.phase = "checking HEAD"
        head = head_paths(gp, set(files))
    except Exception as exc:                  # noqa: BLE001 - one repo only
        if job.expired:
            return "timeout", [], "timed out in phase: %s" % job.phase
        return "repo_error", [], "%s: %s" % (type(exc).__name__, exc)
    finally:
        if recovery:
            recovery.__exit__(None, None, None)

    note = RecoveredRepo.NOTE if recovery else ""
    for p in sorted(files):
        f = files[p]
        if not safe_parts(p):
            status = "bad_path"
        elif not f.versions:
            status = "no_history"
        elif f.too_long and f.kept:
            status = "name_too_long"
        elif f.kept:
            status = "partly_binary" if f.binary_blobs else "ok"
        elif f.binary_blobs:
            status = "binary"
        else:
            status = "no_text"          # only empty / whitespace lines ever
        at_head = "" if head is None else ("yes" if p in head else "no")
        rows.append([org, repo, p,
                     os.path.relpath(f.out, cfg.out) if f.started else "",
                     status, at_head, f.versions, f.added, f.kept, f.bytes,
                     note])
    return "ok", rows, ""


def run_repo(cfg, org, repo, paths, job):
    """Worker: clear, process, then record the outcome - manifest first,
    done.json last, both written atomically. -> the done record."""
    job.start = time.time()
    bind_job(job)
    clear_repo(cfg.out, org, repo)
    status, rows, error = process_repo(cfg, org, repo, paths, job)
    if status != "ok":
        # a repo that timed out or failed part-way may already have flushed
        # some files; they have no manifest rows, so they must not stay
        clear_repo(cfg.out, org, repo)
    sd = state_dir(cfg.out, org, repo)
    os.makedirs(sd, exist_ok=True)
    buf = _csv_text([MANIFEST_HEADER] + rows)
    write_atomic(os.path.join(sd, "manifest.csv"), buf)
    written = [r for r in rows if r[3]]
    rec = {"org": org, "repo": repo, "status": status, "batch": cfg.name,
           "mode": "list" if paths is not None else "history",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - job.start, 1),
           "files_seen": len(rows), "files_written": len(written),
           "lines_added": sum(r[7] for r in rows),
           "lines_kept": sum(r[8] for r in rows),
           "bytes": sum(r[9] for r in rows),
           "file_status": dict(Counter(r[4] for r in rows)),
           "error": error}
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


def _csv_text(rows):
    import io
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def deadline_for(hhmm):
    """--until 07:00 -> the next time it is 07:00 (today, or tomorrow if that
    has passed)."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        sys.exit("--until wants HH:MM, got " + hhmm)
    now = datetime.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + datetime.timedelta(days=1)


def setup_logging(out, name):
    os.makedirs(os.path.join(out, LOGS), exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(os.path.join(out, LOGS, name + ".log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(err)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", nargs="?",
                    help="list mode: file_summary.csv (or a plain input list). "
                         "Leave out for history mode")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV",
                    help="repos to process: a batch file from make_batches.py "
                         "(org, repo); repeatable")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="a repo to process (repeatable)")
    ap.add_argument("--repos-root", required=True,
                    help="folder holding <org>/<repo>")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--name",
                    help="run name for the log files (default: the batch "
                         "file's name, else 'run')")
    ap.add_argument("--extensions",
                    help="only these extensions: a comma list or a file with "
                         "one per line (default: the list in "
                         "repo_extension_summary.py)")
    ap.add_argument("--all-extensions", action="store_true",
                    help="every file, whatever its extension")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos processed in parallel (default 8)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only the repos whose last attempt failed "
                         "(repo_error, timeout, repo_missing)")
    ap.add_argument("--redo", action="store_true",
                    help="process every repo given, even ones already done")
    ap.add_argument("--resume", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo after this long (default 0 = never)")
    ap.add_argument("--warn-after", type=float, default=1800, metavar="SECONDS",
                    help="log a SLOW warning for a repo running longer "
                         "(default 1800, 0 = off)")
    ap.add_argument("--until", metavar="HH:MM",
                    help="start no new repo after this time of day; running "
                         "repos finish. Rerun the same command to continue")
    ap.add_argument("--min-free-disk-gb", type=float, default=20, metavar="GB",
                    help="start no new repo while --out's disk has less free "
                         "(default 20, 0 = off)")
    ap.add_argument("--min-free-gb", type=float, default=8, metavar="GB",
                    help="hold back new repos while free memory is below this "
                         "(one always runs; default 8, 0 = off)")
    ap.add_argument("--quiet", action="store_true", help="no progress bar")
    args = ap.parse_args()

    if args.csv_in and not os.path.isfile(args.csv_in):
        sys.exit("not a file: " + args.csv_in)
    if not args.csv_in and not (args.batch or args.repo):
        sys.exit("history mode needs --batch or --repo (or give an input CSV "
                 "for list mode)")
    if not os.path.isdir(args.repos_root):
        sys.exit("not a directory: " + args.repos_root)
    if args.retry_failed and args.redo:
        sys.exit("--retry-failed and --redo do not go together")
    os.makedirs(args.out, exist_ok=True)
    name = args.name or (os.path.splitext(os.path.basename(args.batch[0]))[0]
                         if args.batch else "run")
    setup_logging(args.out, name)

    t0 = time.time()
    exts = None if args.all_extensions else set(load_extensions(args.extensions))
    chosen = []
    for b in args.batch:
        if not os.path.isfile(b):
            sys.exit("not a file: " + b)
        chosen += read_batch(b)
    for r in args.repo:
        org, _, repo = r.strip("/").partition("/")
        if not repo:
            sys.exit("--repo wants ORG/REPO, got " + r)
        chosen.append((org, repo))
    chosen = list(dict.fromkeys(chosen))

    if args.csv_in:
        groups, total, _used = read_input(args.csv_in, exts)
        if chosen:
            keep = set(chosen)
            groups = {k: v for k, v in groups.items() if k in keep}
            order = [k for k in chosen if k in groups]
        else:
            order = sorted(groups, key=lambda k: -len(groups[k]))
        mode = "list"
        print("input     %s rows, %s file(s) in %s repo(s)"
              % (f"{total:,}", f"{sum(map(len, groups.values())):,}",
                 f"{len(groups):,}"))
    else:
        groups = {k: None for k in chosen}
        order = chosen
        mode = "history"
        print("input     %s repo(s), history mode" % f"{len(order):,}")

    # ---- what to do this run --------------------------------------------
    prior = {k: read_done(args.out, *k) for k in order}
    if args.redo:
        todo = list(order)
    elif args.retry_failed:
        todo = [k for k in order if prior[k] and prior[k]["status"] != "ok"]
    else:
        todo = [k for k in order if not prior[k]]
    n_ok = sum(1 for k in order if prior[k] and prior[k]["status"] == "ok")
    n_failed = sum(1 for k in order if prior[k] and prior[k]["status"] != "ok")
    print("state     %d done, %d failed earlier, %d to process now%s"
          % (n_ok, n_failed, len(todo),
             "" if args.retry_failed or not n_failed
             else " (--retry-failed reruns the failed ones)"))

    class Cfg:
        pass
    cfg = Cfg()
    cfg.repos_root, cfg.out, cfg.exts, cfg.name = (args.repos_root, args.out,
                                                   exts, name)
    deadline = deadline_for(args.until) if args.until else None
    log.info("START %s mode=%s repos=%d todo=%d done=%d failed=%d workers=%d "
             "timeout=%s until=%s exts=%s out=%s", name, mode, len(order),
             len(todo), n_ok, n_failed, args.workers, args.repo_timeout or "-",
             args.until or "-", "all" if exts is None else len(exts), args.out)

    repos_csv = os.path.join(args.out, LOGS, name + "_repos.csv")
    new_csv = not os.path.exists(repos_csv)
    counts, files_by_status = Counter(), Counter()
    totals = Counter()
    stopped = ""
    bar = Progress("repos", len(todo), not args.quiet)
    with open(repos_csv, "a", newline="", encoding="utf-8",
              errors="surrogateescape") as rf:
        rw = csv.writer(rf)
        if new_csv:
            rw.writerow(REPOS_HEADER)
        pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
        running = {}
        queue = list(todo)
        finished = 0

        def may_start():
            """-> '' to go ahead, 'wait' to hold back, or why to stop."""
            if deadline and datetime.datetime.now() >= deadline:
                return "reached --until %s" % args.until
            if args.min_free_disk_gb:
                free = shutil.disk_usage(args.out).free / 1024 ** 3
                if free < args.min_free_disk_gb:
                    return "free disk %.1f GB below --min-free-disk-gb %g" % (
                        free, args.min_free_disk_gb)
            if args.min_free_gb and running:
                mem = mem_available_gb()
                if mem is not None and mem < args.min_free_gb:
                    return "wait"
            return ""

        while queue or running:
            while queue and not stopped and len(running) < args.workers:
                why = may_start()
                if why == "wait":
                    break
                if why:
                    stopped = why
                    log.warning("STOP starting new repos: %s (%d left)",
                                why, len(queue))
                    break
                k = queue.pop(0)
                job = RepoJob(k[0], k[1], 0)
                log.info("begin  %s/%s", *k)
                running[pool.submit(run_repo, cfg, k[0], k[1], groups[k],
                                    job)] = (k, job)
            if not running:
                break
            ready, _ = wait(running, timeout=5, return_when=FIRST_COMPLETED)
            for fut, (k, job) in running.items():
                if fut in ready:
                    continue
                el = job.elapsed()
                if args.repo_timeout and el > args.repo_timeout \
                        and not job.expired:
                    log.warning("TIMEOUT %s/%s after %.0fs in phase: %s",
                                k[0], k[1], el, job.phase)
                    job.kill()
                elif args.warn_after and el > args.warn_after \
                        and not job.warned:
                    job.warned = True
                    log.warning("SLOW   %s/%s running %.0fs, phase: %s",
                                k[0], k[1], el, job.phase)
            for fut in ready:
                k, job = running.pop(fut)
                try:
                    rec = fut.result()
                except Exception as exc:          # noqa: BLE001
                    rec = {"org": k[0], "repo": k[1], "status": "repo_error",
                           "seconds": round(job.elapsed(), 1),
                           "files_seen": 0, "files_written": 0,
                           "lines_kept": 0, "bytes": 0, "file_status": {},
                           "error": "%s: %s" % (type(exc).__name__, exc)}
                    log.error("CRASH  %s/%s %s", k[0], k[1], rec["error"])
                counts[rec["status"]] += 1
                files_by_status.update(rec.get("file_status", {}))
                for c in ("files_written", "lines_kept", "bytes"):
                    totals[c] += rec.get(c, 0)
                lvl = logging.INFO if rec["status"] == "ok" else logging.ERROR
                log.log(lvl, "done   %s/%s %s %.0fs files=%d written=%d "
                        "lines=%d size=%s%s", k[0], k[1], rec["status"],
                        rec["seconds"], rec["files_seen"],
                        rec["files_written"], rec["lines_kept"],
                        human(rec["bytes"]),
                        (" error=" + rec["error"]) if rec["error"] else "")
                rw.writerow([rec.get("finished", ""), name, k[0], k[1],
                             rec["status"], rec["seconds"], rec["files_seen"],
                             rec["files_written"], rec["lines_kept"],
                             rec["bytes"], rec["error"]])
                rf.flush()
                finished += 1
                bar.update(finished, "%d failed" % (finished - counts["ok"]))
        pool.shutdown()
        bar.close()

    left = len(todo) - finished
    summary = ("END %s: %d repo(s) processed (%s), %s file(s) written, %s "
               "line(s), %s, %.0fs%s"
               % (name, finished,
                  ", ".join("%s %d" % kv for kv in counts.most_common()) or "-",
                  f"{totals['files_written']:,}", f"{totals['lines_kept']:,}",
                  human(totals["bytes"]), time.time() - t0,
                  ("; STOPPED EARLY (%s), %d repo(s) left - run the same "
                   "command again to continue" % (stopped, left))
                  if left else ""))
    log.info(summary)
    print("\n" + summary)
    if files_by_status:
        print("files by status: " + ", ".join(
            "%s %s" % (st, f"{n:,}") for st, n in files_by_status.most_common()))
    print("log       %s" % os.path.join(args.out, LOGS, name + ".log"))
    return 3 if left else 0


if __name__ == "__main__":
    sys.exit(main())
