#!/usr/bin/env python3
"""
file_delta.py - keep only the history that has NOT been processed yet.

The files in the active repos (the checked-out copy, e.g.
/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos/<org>/<repo>/...)
have already been processed. From file_added_lines.py's output (every line a
file ever had, one .txt per file) this writes, into a NEW folder, only what
is left to process:

  file in the active copy         history lines MINUS the lines of the
  (at_head = yes)                 current file (stripped, blanks dropped, the
                                  same normalisation as the extraction), in
                                  history order. Nothing left -> no file.
  file not in the active copy     the whole history file, hard-linked (no
  (at_head = no)                  extra space; copied if linking fails)
  repo has no active copy at all  the whole history file, hard-linked
  no .txt in the source           nothing (binary / no_text / name too long)

at_head comes from the source manifests (filled by fill_at_head.py). Where it
is blank but the repo DOES have a folder in the active copy - fill_at_head.py
had not reached it - the file is looked up in the active copy directly.

A current file that cannot be read (permission, binary where history is text)
keeps the whole history, flagged in the manifest: nothing unprocessed is ever
dropped. A failing MOUNT is different: if the active copy stops answering
(blobfuse "Transport endpoint is not connected", I/O errors, timeouts) the
repo is not recorded at all - no done.json - and the run STOPS starting new
repos and exits with code 3. Remount, run the same command again, and it
carries on with the repos not done yet. --redo-suspect reruns only done repos
a broken mount may have spoiled (recorded with no active copy, or with a
mount error on a current file) - for runs made before this check existed.

Nothing is written to the source folder or the active copy. The output is
self-contained, so the source can be deleted afterwards: hard-linked files
keep their data.

Output, under --out:
  <org>/<repo>/<path>.txt        content only
  _state/<org>/<repo>/manifest.csv
                                 per file: action, at_head, lines in history,
                                 lines in the current file, lines removed,
                                 lines kept, note
  _state/<org>/<repo>/done.json  the repo's totals - written last; a repo with
                                 one is skipped on the next run (--redo: not)
  _logs/<name>.log, _logs/<name>_repos.csv

Actions: subtracted, empty (all history lines are in the current file),
linked / copied (whole history), linked_no_active_copy, unreadable_current
(whole history, current file could not be read), skipped (no .txt).

Usage:
    python3 file_delta.py /data/workarea/full_extract \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --out /data/workarea/full_extract_delta --workers 16
"""

import argparse
import csv
import datetime
import errno
import io
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

from file_added_lines import decode, utf16_text

STATE, LOGS = "_state", "_logs"
MANIFEST_HEADER = ["org", "repo", "path", "output", "action", "at_head",
                   "lines_history", "lines_current", "lines_removed",
                   "lines_kept", "note"]
log = logging.getLogger("file_delta")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def current_lines(path):
    """Set of stripped, non-blank lines of the active copy's file, decoded
    the way the extraction decodes. Raises OSError / ValueError when it
    cannot be read as text."""
    with open(path, "rb") as fh:
        data = fh.read()
    text = utf16_text(data)
    if text is not None:
        lines = text.splitlines()          # UTF-16: as the extraction splits it
    else:
        if b"\0" in data[:8000]:
            raise ValueError("current file is binary")
        # one line per "\n", as in git's diff - not splitlines(), which would
        # also split at a "\r", form feed etc. inside a line
        lines = (decode(ln) for ln in data.split(b"\n"))
    return {s for s in (ln.strip() for ln in lines) if s}


# errors that mean the MOUNT is failing, not that a file is missing or bad:
# a repo that hits one is failed (no done.json) so the next run retries it,
# instead of recording "no active copy" / "unreadable" for good
MOUNT_ERRNOS = {errno.ENOTCONN, errno.EIO, errno.ETIMEDOUT, errno.ESTALE,
                errno.EHOSTDOWN, errno.ECONNABORTED, errno.ENODEV}


class MountDown(RuntimeError):
    pass


def check_mount(path):
    """Raise MountDown unless `path` (the active root) can be listed."""
    try:
        with os.scandir(path) as it:
            next(it, None)
    except OSError as exc:
        raise MountDown("active copy not reachable at %s: %s" % (path, exc))


def repo_exists(root, org, repo):
    """Does <root>/<org>/<repo> exist? A missing folder on a WORKING mount
    is False; a failing mount raises MountDown."""
    try:
        os.stat(os.path.join(root, org, repo))
        return True
    except FileNotFoundError:
        check_mount(root)              # missing, or is the mount gone?
        return False
    except OSError as exc:
        if exc.errno in MOUNT_ERRNOS:
            raise MountDown("%s/%s: %s" % (org, repo, exc))
        raise


MOUNT_ERROR_TEXT = ("Errno 107", "Transport endpoint is not connected",
                    "Errno 5]", "Input/output error", "Errno 110",
                    "timed out", "Errno 116", "Stale file handle")


def suspect(out, org, repo):
    """A done repo whose result a broken mount may have spoiled."""
    sd = os.path.join(out, STATE, org, repo)
    try:
        with open(os.path.join(sd, "done.json"), encoding="utf-8") as fh:
            if not json.load(fh).get("active_copy", True):
                return True
        with open(os.path.join(sd, "manifest.csv"), encoding="utf-8",
                  errors="replace") as fh:
            return any(t in line for line in fh for t in MOUNT_ERROR_TEXT
                       if "unreadable_current" in line)
    except (OSError, ValueError):
        return True


def link_or_copy(src, dst):
    """Hard link, else copy. -> 'linked' | 'copied'."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        shutil.copyfile(src, dst)
        return "copied"


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape",
              newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def csv_text(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def clear_repo(out, org, repo):
    root = os.path.realpath(out)
    for d in (os.path.join(out, org, repo), os.path.join(out, STATE, org, repo)):
        real = os.path.realpath(d)
        if real.startswith(root + os.sep) and real != root:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# per file / per repo
# --------------------------------------------------------------------------

def one_file(cfg, org, repo, row, active_repo_exists):
    """-> manifest row for one source manifest row."""
    path, output, at_head = row["path"], row["output"], row.get("at_head", "")
    base = [org, repo, path]
    if not output:
        return base + ["", "skipped", at_head, 0, 0, 0, 0,
                       "no .txt in source: " + row.get("status", "")]
    src = os.path.join(cfg.src, output)
    dst = os.path.join(cfg.out, output)
    active = os.path.join(cfg.active_root, org, repo, *path.split("/"))

    if not active_repo_exists:
        at_head, how = "", "no_active_copy"
    elif at_head not in ("yes", "no"):
        try:                                   # not filled yet: look it up
            at_head = "yes" if os.stat(active) else "no"
        except FileNotFoundError:
            check_mount(cfg.active_root)
            at_head = "no"
        except OSError as exc:
            if exc.errno in MOUNT_ERRNOS:
                raise MountDown("%s: %s" % (active, exc))
            at_head = "no"
        how = "checked"
    else:
        how = ""

    def whole(action, note):
        with open(src, encoding="utf-8", errors="surrogateescape",
                  newline="\n") as fh:
            n = sum(1 for _ in fh)
        if not cfg.dry_run:
            kind = link_or_copy(src, dst)
            action = action.replace("linked", kind) if action.startswith(
                "linked") else action
        return base + [output, action, at_head, n, 0, 0, n, note]

    if at_head != "yes":
        return whole("linked_no_active_copy" if how == "no_active_copy"
                     else "linked",
                     "at_head looked up in the active copy" if how == "checked"
                     else "")

    try:
        cur = current_lines(active)
    except OSError as exc:
        if exc.errno in MOUNT_ERRNOS:
            raise MountDown("%s: %s" % (active, exc))
        if isinstance(exc, FileNotFoundError):
            check_mount(cfg.active_root)
        return whole("unreadable_current", "%s: %s" % (type(exc).__name__, exc))
    except ValueError as exc:
        return whole("unreadable_current", "%s: %s" % (type(exc).__name__, exc))

    kept, n_hist = [], 0
    # newline="\n": file_added_lines.py wrote one line per "\n"; the default
    # would also split at a lone "\r" inside a line, and the halves would not
    # match the current file's (stripped) lines
    with open(src, encoding="utf-8", errors="surrogateescape",
              newline="\n") as fh:
        for ln in fh:
            s = ln.rstrip("\n")
            if not s:
                continue
            n_hist += 1
            if s not in cur:
                kept.append(s)
    if kept and not cfg.dry_run:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", errors="surrogateescape",
                  newline="\n") as fh:
            fh.write("\n".join(kept) + "\n")
    return base + [output if kept else "", "subtracted" if kept else "empty",
                   at_head, n_hist, len(cur), n_hist - len(kept), len(kept),
                   "at_head looked up in the active copy" if how == "checked"
                   else ""]


def run_repo(cfg, org, repo):
    t0 = time.time()
    check_mount(cfg.active_root)          # before clearing anything
    if not cfg.dry_run:
        clear_repo(cfg.out, org, repo)
    mp = os.path.join(cfg.src, STATE, org, repo, "manifest.csv")
    with open(mp, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        src_rows = list(csv.DictReader(fh))
    active_repo_exists = repo_exists(cfg.active_root, org, repo)
    rows, error = [], ""
    with ThreadPoolExecutor(max_workers=cfg.file_workers) as pool:
        futs = [pool.submit(one_file, cfg, org, repo, r, active_repo_exists)
                for r in src_rows]
        for f, r in zip(futs, src_rows):
            try:
                rows.append(f.result())
            except MountDown:
                for g in futs:
                    g.cancel()
                raise                         # the whole repo is retried
            except Exception as exc:          # noqa: BLE001 - one file
                error = "%s: %s" % (type(exc).__name__, exc)
                rows.append([org, repo, r["path"], "", "error",
                             r.get("at_head", ""), 0, 0, 0, 0, error[:300]])
    status = "ok" if not error else "error"
    rec = {"org": org, "repo": repo, "status": status,
           "active_copy": active_repo_exists,
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - t0, 1),
           "files": len(rows),
           "files_written": sum(1 for r in rows if r[3]),
           "actions": dict(Counter(r[4] for r in rows)),
           "lines_history": sum(r[6] for r in rows),
           "lines_removed": sum(r[8] for r in rows),
           "lines_kept": sum(r[9] for r in rows),
           "error": error}
    if not cfg.dry_run:
        sd = os.path.join(cfg.out, STATE, org, repo)
        os.makedirs(sd, exist_ok=True)
        write_atomic(os.path.join(sd, "manifest.csv"),
                     csv_text([MANIFEST_HEADER] + rows))
        write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="file_added_lines.py output (e.g. full_extract)")
    ap.add_argument("--active-root", required=True,
                    help="the processed checked-out copy, holding <org>/<repo>")
    ap.add_argument("--out", required=True, help="new output folder")
    ap.add_argument("--workers", type=int, default=16,
                    help="repos at once (default 16)")
    ap.add_argument("--file-workers", type=int, default=8,
                    help="files at once within a repo (default 8) - reading "
                         "the active copy is network waits, so they overlap")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--redo", action="store_true",
                    help="reprocess repos already done in --out")
    ap.add_argument("--redo-suspect", action="store_true",
                    help="reprocess only repos a broken mount may have spoiled: "
                         "recorded as having no active copy, or with a file "
                         "whose current version failed with a mount error "
                         "(Transport endpoint is not connected, I/O error, "
                         "timeout). Other done repos are skipped")
    ap.add_argument("--dry-run", action="store_true",
                    help="work everything out and print the totals, write nothing")
    ap.add_argument("--name", default="delta", help="log file name (default delta)")
    args = ap.parse_args()

    src_state = os.path.join(args.src, STATE)
    if not os.path.isdir(src_state):
        sys.exit("no _state folder under " + args.src)
    try:
        check_mount(args.active_root)
    except MountDown as exc:
        sys.exit(str(exc))
    if os.path.realpath(args.out) == os.path.realpath(args.src):
        sys.exit("--out must be a different folder from the source")

    class Cfg:
        pass
    cfg = Cfg()
    cfg.src, cfg.out, cfg.active_root = args.src, args.out, args.active_root
    cfg.dry_run, cfg.file_workers = args.dry_run, max(1, args.file_workers)

    log.setLevel(logging.INFO)
    if not args.dry_run:
        os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
        fh = logging.FileHandler(os.path.join(args.out, LOGS, args.name + ".log"),
                                 encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                          "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    log.addHandler(err)

    want = {tuple(r.strip("/").split("/", 1)) for r in args.repo}
    todo, skipped = [], 0
    for org in sorted(os.listdir(src_state)):
        od = os.path.join(src_state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            if want and (org, repo) not in want:
                continue
            if not os.path.isfile(os.path.join(od, repo, "manifest.csv")):
                continue
            done_path = os.path.join(args.out, STATE, org, repo, "done.json")
            if not args.redo and not args.dry_run and os.path.isfile(done_path):
                if not (args.redo_suspect and suspect(args.out, org, repo)):
                    skipped += 1
                    continue
            todo.append((org, repo))
    print("repos     %s to process, %s already done%s"
          % (f"{len(todo):,}", f"{skipped:,}",
             " - DRY RUN, nothing is written" if args.dry_run else ""),
          flush=True)
    log.info("START src=%s active=%s out=%s repos=%d workers=%d file_workers=%d",
             args.src, args.active_root, args.out, len(todo), args.workers,
             cfg.file_workers)

    t0 = time.time()
    actions, totals, statuses = Counter(), Counter(), Counter()
    no_active = done = 0
    repos_csv = None
    if not args.dry_run:
        path = os.path.join(args.out, LOGS, args.name + "_repos.csv")
        new = not os.path.exists(path)
        repos_csv = open(path, "a", newline="", encoding="utf-8")
        rw = csv.writer(repos_csv)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "active_copy",
                         "seconds", "files", "files_written", "lines_history",
                         "lines_removed", "lines_kept", "error"])
    try:
        pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
        queue, running, stopped = list(todo), {}, ""

        def fill():
            while queue and not stopped and len(running) < args.workers:
                o, r = queue.pop(0)
                running[pool.submit(run_repo, cfg, o, r)] = (o, r)

        fill()
        while running:
            finished_now, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished_now:
                o, r = running.pop(fut)
                try:
                    rec = fut.result()
                except MountDown as exc:
                    log.error("MOUNT  %s/%s not done, retried next run: %s",
                              o, r, exc)
                    statuses["mount_down"] += 1
                    if not stopped:
                        stopped = str(exc)
                        log.error("STOP   the active copy is not reachable - "
                                  "no new repos are started; the %d running "
                                  "finish or fail, the rest stay to do",
                                  len(running))
                    continue
                except Exception as exc:          # noqa: BLE001 - one repo
                    log.error("FAIL   %s/%s %s: %s", o, r, type(exc).__name__, exc)
                    statuses["failed"] += 1
                    continue
                done += 1
                statuses[rec["status"]] += 1
                actions.update(rec["actions"])
                no_active += not rec["active_copy"]
                for k in ("files_written", "lines_history", "lines_removed",
                          "lines_kept"):
                    totals[k] += rec[k]
                lvl = logging.INFO if rec["status"] == "ok" else logging.ERROR
                log.log(lvl, "done   %s/%s %s %.0fs files=%d written=%d kept=%d "
                        "removed=%d%s", o, r, rec["status"], rec["seconds"],
                        rec["files"], rec["files_written"], rec["lines_kept"],
                        rec["lines_removed"],
                        (" error=" + rec["error"]) if rec["error"] else "")
                if repos_csv:
                    rw.writerow([rec["finished"], o, r, rec["status"],
                                 rec["active_copy"], rec["seconds"], rec["files"],
                                 rec["files_written"], rec["lines_history"],
                                 rec["lines_removed"], rec["lines_kept"],
                                 rec["error"]])
                    repos_csv.flush()
                if done % 200 == 0:
                    el = time.time() - t0
                    print("  %s / %s repos  %.0fs  (~%.0f min left)"
                          % (f"{done:,}", f"{len(todo):,}", el,
                             el / done * (len(todo) - done) / 60),
                          file=sys.stderr, flush=True)
            fill()
        pool.shutdown()
    finally:
        if repos_csv:
            repos_csv.close()

    hist = totals["lines_history"]
    lines = [
        "END %s: %s repo(s) (%s)%s, %s without an active copy"
        % (args.name, f"{done:,}",
           ", ".join("%s %d" % kv for kv in statuses.most_common()) or "-",
           " - DRY RUN" if args.dry_run else "", f"{no_active:,}"),
        "files by action: " + (", ".join("%s %s" % (k, f"{v:,}")
                                          for k, v in actions.most_common()) or "-"),
        "files written %s | history lines %s | removed as already processed %s "
        "(%.1f%%) | kept %s"
        % (f"{totals['files_written']:,}", f"{hist:,}",
           f"{totals['lines_removed']:,}",
           100.0 * totals["lines_removed"] / hist if hist else 0,
           f"{totals['lines_kept']:,}"),
        "%.0fs" % (time.time() - t0)]
    for ln in lines:
        log.info(ln)
        print(ln)
    left = len(todo) - done - statuses["failed"]
    if stopped:
        msg = ("STOPPED: the active copy stopped answering (%s). %d repo(s) are "
               "not done yet. Remount it, then run the SAME command again "
               "(without --redo) - finished repos are skipped."
               % (stopped, left))
        log.error(msg)                    # shown on stderr and in the log
        return 3
    return 1 if statuses["failed"] or statuses["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
