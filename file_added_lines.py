#!/usr/bin/env python3
"""
file_added_lines.py - for every file in the input list, every line it has
ever had, once each, as one plain .txt: the content of all its versions
merged, with duplicates dropped.

Per file, in the order the lines first appeared (oldest commit first):

  * version 1 counts as entirely added, so all of it is in
  * every later version adds only the lines its commit ADDED; removed lines
    were added by an earlier commit, so they are already in
  * each line is stripped of spaces/tabs at both ends, blank lines are
    dropped, and a line already written for this file is not written again
    (so re-indenting a script adds nothing)

The .txt holds file content only - no headers, commit ids, authors or dates.

History covers every branch (--all), or for a repo git cannot open (objects
but no HEAD/refs) every commit in its object store. Merges are diffed against
their first parent, so a merge only adds what it changed on the branch it
landed on; the branch's own commits contributed their lines already. Renames
are not followed: a file's history is the history of the exact path listed.

Binary files have no lines and get no .txt. The exception is UTF-16 text
(common for .sql/.ps1 saved on Windows), which git treats as binary: every
version of such a file is decoded and its lines added the same way.

Only files whose extension is in the list repo_extension_summary.py uses
(json, cs, ts, sql, ...) are processed, matched the same way (.gitignore
counts as "gitignore"); --extensions picks another list, --all-extensions
takes every file.

Input: the file_summary.csv from file_history_for_list.py (or its deduped
version from dedupe_by_root.py) - only rows with status `found` are used, by
their matched_path. A plain input list (org, repo, relpath, filename) also
works; its paths are resolved as file_history_for_list.py does.

Output, under --out:
  <org>/<repo>/<path>.txt    one per file that has any text lines
  _manifest.csv              one row per file: output path, status, versions
                             (commits that touched it), lines added across
                             all versions, lines kept after dropping duplicates
  _done_repos                repos finished, for --resume

Usage:
    python3 file_added_lines.py file_summary_dedup.csv \\
        --repos-root /data/workarea/archive --out /data/workarea/added_lines
    python3 file_added_lines.py file_summary_dedup.csv \\
        --repos-root /data/workarea/archive --out out --repo ey-org/admin-ui
"""

import argparse
import csv
import os
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
                                   git_can_open)
from repo_extension_summary import ext_key, load_extensions

MANIFEST = "_manifest.csv"
DONE_FILE = "_done_repos"
MANIFEST_HEADER = ["org", "repo", "path", "output", "status", "versions",
                   "lines_added", "lines_kept", "error"]
FLUSH_CHARS = 64 * 1024 * 1024      # buffered text per repo before writing out


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_input(path, wanted_exts=None):
    """-> {(org, repo): sorted repo-relative paths}, rows read, rows used.
    wanted_exts: keep only paths with these extensions (None = all)."""
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


# --------------------------------------------------------------------------
# per-file accumulation
# --------------------------------------------------------------------------

def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")     # Windows-saved scripts


class FileLines:
    """The distinct stripped lines of one file, in first-seen order. Lines
    are buffered and appended to the .txt in batches; only a hash per line
    already written stays in memory."""

    __slots__ = ("out", "seen", "buf", "started", "versions", "added",
                 "kept", "binary_blobs")

    def __init__(self, out):
        self.out = out
        self.seen = set()
        self.buf = []
        self.started = False
        self.versions = self.added = self.kept = 0
        self.binary_blobs = []

    def add(self, text):
        s = text.strip()
        if not s:
            return 0
        self.added += 1
        h = hash(s)
        if h in self.seen:
            return 0
        self.seen.add(h)
        self.buf.append(s)
        self.kept += 1
        return len(s) + 1

    def flush(self):
        if not self.buf:
            return
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        with open(self.out, "a" if self.started else "w", encoding="utf-8",
                  errors="surrogateescape", newline="\n") as fh:
            fh.write("\n".join(self.buf))
            fh.write("\n")
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
    return GIT + ["--literal-pathspecs", "-c", "core.quotePath=false",
                  "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
                  "-c", "diff.relative=false", "-C", gp, "log", "--all",
                  "--stdin", "--full-history", "--topo-order", "--reverse",
                  "-p", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                  "--no-renames", "--full-index", "--src-prefix=a/",
                  "--dst-prefix=b/", "--diff-merges=first-parent",
                  "--format=%x1e%H"]


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


def collect(gp, paths, out_root, job):
    """Stream the repo's history once and fill a FileLines per path."""
    files = {p: FileLines(os.path.join(out_root, *p.split("/")) + ".txt")
             for p in paths}
    buffered = 0
    job.phase = "reading history"
    with tempfile.TemporaryFile() as err:
        proc = spawn(log_cmd(gp), stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=err)
        # the paths go in on stdin after "--": no argv limit however many
        feeder = threading.Thread(target=_feed, args=(proc.stdin, paths),
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
                        cur = files.get(diff_path(line))
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
    for f in files.values():
        f.flush()
    return files


def _feed(stdin, paths):
    try:
        stdin.write(b"--\n")
        for p in paths:
            stdin.write(p.encode("utf-8", "surrogateescape") + b"\n")
        stdin.close()
    except (BrokenPipeError, OSError):
        pass


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def safe_parts(p):
    parts = p.split("/")
    return all(x not in ("", ".", "..") for x in parts)


def process_repo(root, out, org, repo, paths, job):
    job.start = time.time()
    bind_job(job)
    rp = os.path.join(root, org, repo)
    out_root = os.path.join(out, org, repo)

    def rows(status, err=""):
        return [[org, repo, p, "", status, 0, 0, 0, err] for p in paths]

    if not is_repo_dir(rp):
        return rows("repo_missing")
    good = [p for p in paths if safe_parts(p)]
    bad = [[org, repo, p, "", "bad_path", 0, 0, 0, ""]
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
        files = collect(gp, good, out_root, job)
    except TimeoutError as exc:
        return rows("timeout", str(exc))
    except (RuntimeError, OSError) as exc:
        return rows("repo_error", "%s: %s" % (type(exc).__name__, exc))
    finally:
        if recovery:
            recovery.__exit__(None, None, None)

    result = bad
    for p in good:
        f = files[p]
        if not f.versions:
            status = "no_history"
        elif f.kept:
            status = "partly_binary" if f.binary_blobs else "ok"
        elif f.binary_blobs:
            status = "binary"
        else:
            status = "no_text"          # only empty / whitespace lines ever
        note = RecoveredRepo.NOTE if recovery else ""
        result.append([org, repo, p,
                       os.path.relpath(f.out, out) if f.started else "",
                       status, f.versions, f.added, f.kept, note])
    return result


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="file_summary.csv (or a plain input list)")
    ap.add_argument("--repos-root", required=True,
                    help="folder holding <org>/<repo>")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--extensions",
                    help="only files with these extensions: a comma list or a "
                         "file with one per line (default: the list in "
                         "repo_extension_summary.py)")
    ap.add_argument("--all-extensions", action="store_true",
                    help="every file in the input, whatever its extension")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos processed in parallel (default 8)")
    ap.add_argument("--resume", action="store_true",
                    help="skip repos finished by an earlier run into --out")
    ap.add_argument("--repo-timeout", type=float, default=0, metavar="SECONDS",
                    help="give up on a repo after this long (default 0 = never)")
    ap.add_argument("--quiet", action="store_true", help="no progress bar")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        sys.exit("not a file: " + args.csv_in)
    if not os.path.isdir(args.repos_root):
        sys.exit("not a directory: " + args.repos_root)
    os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    exts = None if args.all_extensions else set(load_extensions(args.extensions))
    groups, total, used = read_input(args.csv_in, exts)
    if args.repo:
        pick = {tuple(r.strip("/").split("/", 1)) for r in args.repo}
        groups = {k: v for k, v in groups.items() if k in pick}
    print("input     %s rows, %s file(s) in %s repo(s)%s"
          % (f"{total:,}", f"{sum(map(len, groups.values())):,}",
             f"{len(groups):,}",
             "" if exts is None else ", %d extension(s)" % len(exts)))

    man_path = os.path.join(args.out, MANIFEST)
    done_path = os.path.join(args.out, DONE_FILE)
    done = set()
    resuming = args.resume and os.path.exists(man_path)
    if resuming and os.path.exists(done_path):
        with open(done_path, encoding="utf-8") as fh:
            done = {tuple(ln.rstrip("\n").split("\t")) for ln in fh
                    if ln.strip()}
    todo = sorted((k for k in groups if k not in done),
                  key=lambda k: -len(groups[k]))       # biggest lists first
    if done:
        print("resume    %d repo(s) already done" % len(done & set(groups)))

    counts = Counter()
    bar = Progress("repos", len(todo), not args.quiet)
    mode = "a" if resuming else "w"
    with open(man_path, mode, newline="", encoding="utf-8",
              errors="surrogateescape") as mf, \
            open(done_path, mode, encoding="utf-8") as df:
        mw = csv.writer(mf)
        if not resuming:
            mw.writerow(MANIFEST_HEADER)
        pool = ThreadPoolExecutor(max_workers=args.workers)
        running = {}
        it = iter(todo)
        finished = 0

        def submit():
            k = next(it, None)
            if k is None:
                return False
            job = RepoJob(k[0], k[1], len(groups[k]))
            running[pool.submit(process_repo, args.repos_root, args.out,
                                k[0], k[1], groups[k], job)] = (k, job)
            return True

        for _ in range(args.workers):
            if not submit():
                break
        while running:
            ready, _ = wait(running, timeout=5, return_when=FIRST_COMPLETED)
            if args.repo_timeout:
                for fut, (k, job) in running.items():
                    if fut not in ready and job.elapsed() > args.repo_timeout:
                        job.kill()
            for fut in ready:
                k, job = running.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:          # keep the run going
                    result = [[k[0], k[1], p, "", "repo_error", 0, 0, 0,
                               "%s: %s" % (type(exc).__name__, exc)]
                              for p in groups[k]]
                mw.writerows(result)
                mf.flush()
                counts.update(r[4] for r in result)
                df.write("%s\t%s\n" % k)
                df.flush()
                del groups[k]
                finished += 1
                bar.update(finished, "%s file(s) written"
                           % f"{counts['ok'] + counts['partly_binary']:,}")
                submit()
        pool.shutdown()
        bar.close()

    print("\nfiles by status:")
    for st, n in counts.most_common():
        print("  %-14s %s" % (st, f"{n:,}"))
    print("-> %s\n   %s\n   %.1fs" % (args.out, man_path, time.time() - t0))


if __name__ == "__main__":
    main()
