#!/usr/bin/env python3
"""
diagnose_repo.py - find out why one repo is slow for file_history_for_list.py.

Times the same steps the script runs and reports what makes them slow:

  1. opening the repo (a repo git cannot open - .git with only objects - is
     rebuilt as a temporary stand-in, exactly as file_history_for_list.py does)
  2. the object store: how many objects, commits, trees, blobs; the biggest blobs
  3. reading the whole history WITHOUT rename detection
  4. reading the whole history WITH rename detection (what the script does)

For 3 and 4 it reports commits read, file changes, the commits that touch the
most files, and the speed. Each pass is stopped after --max-seconds, and the
report then says how far it got and estimates the total time. It only reads
the repo (a temporary stand-in is deleted afterwards).

Usage:
    python diagnose_repo.py /data/workarea/archive/ey-org/gvrt
    python diagnose_repo.py /data/workarea/archive/ey-org/gvrt --max-seconds 600
"""

import argparse
import heapq
import os
import subprocess
import sys
import tempfile
import threading
import time

from extract_commits import GIT, bind_job, changes_from_git
from file_history_for_list import (RecoveredRepo, fmt_dur, git_can_open,
                                   pack_bytes)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


class Job:
    """Lets a timer kill the git processes of the pass that is running."""

    def __init__(self):
        self.procs, self.expired = [], False

    def register(self, proc):
        self.procs.append(proc)
        if self.expired:
            proc.kill()

    def kill(self):
        self.expired = True
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass


def object_stats(git_path):
    """Stream every object once: counts by type and the biggest blobs."""
    counts, biggest, t0 = {}, [], time.time()
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            GIT + ["-C", git_path, "cat-file", "--batch-all-objects", "--unordered",
                   "--batch-check=%(objecttype) %(objectsize) %(objectname)"],
            stdout=subprocess.PIPE, stderr=err)
        for raw in proc.stdout:
            parts = raw.split()
            if len(parts) != 3:
                continue
            kind, size = parts[0].decode(), int(parts[1])
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "blob":
                if len(biggest) < 5:
                    heapq.heappush(biggest, (size, parts[2].decode()))
                else:
                    heapq.heappushpop(biggest, (size, parts[2].decode()))
        proc.wait()
    return counts, sorted(biggest, reverse=True), time.time() - t0


def history_pass(git_path, feed, renames, max_seconds, total_commits):
    job, timer = Job(), None
    bind_job(job)
    timer = threading.Timer(max_seconds, job.kill)
    timer.start()
    commits = changes = 0
    top = []                                  # (files changed, sha)
    t0 = time.time()
    try:
        for sha, ch in changes_from_git(git_path, [], None, None, None, feed,
                                        renames=renames):
            commits += 1
            changes += len(ch)
            if len(top) < 3:
                heapq.heappush(top, (len(ch), sha))
            else:
                heapq.heappushpop(top, (len(ch), sha))
    except RuntimeError:
        if not job.expired:          # a real git failure, not our own kill
            raise
    finally:
        timer.cancel()
        bind_job(None)
    # finished = every commit was read (a timer that fires just as the pass
    # ends must not turn a complete read into a "stopped" one)
    done = not job.expired or (total_commits and commits >= total_commits)
    return {"commits": commits, "changes": changes, "seconds": time.time() - t0,
            "finished": bool(done), "top": sorted(top, reverse=True)}


def report_pass(title, r, total_commits):
    rate = r["commits"] / r["seconds"] if r["seconds"] else 0
    print("\n%s" % title)
    if r["finished"]:
        print("  finished in %s: %s commits, %s file changes (%.0f commits/s)"
              % (fmt_dur(r["seconds"]), f"{r['commits']:,}", f"{r['changes']:,}", rate))
    else:
        frac = r["commits"] / total_commits if total_commits else 0
        est = r["seconds"] / frac if frac else 0
        print("  STOPPED after %s: read %s of %s commits (%.1f%%), %s file changes"
              % (fmt_dur(r["seconds"]), f"{r['commits']:,}", f"{total_commits:,}",
                 frac * 100, f"{r['changes']:,}"))
        if est:
            print("  at this rate the full pass would take about %s" % fmt_dur(est))
    if r["top"]:
        print("  commits touching the most files: %s"
              % ", ".join("%s (%s files)" % (sha[:10], f"{n:,}") for n, sha in r["top"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="path to the repo folder (or its .git)")
    ap.add_argument("--max-seconds", type=float, default=300,
                    help="stop each history pass after this long (default 300)")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        print("not a directory: %s" % repo, file=sys.stderr)
        return 2

    print("repo        %s" % repo)
    print("pack files  %s" % human(pack_bytes(repo)))
    stand_in = None
    t0 = time.time()
    if git_can_open(repo):
        gp, feed = repo, None
        print("opening     git opens it directly")
    else:
        stand_in = RecoveredRepo(repo)
        print("opening     git CANNOT open it (no HEAD/refs): building a stand-in ...",
              flush=True)
        stand_in.__enter__()
        gp, feed = stand_in.tmp, stand_in.revs
        with open(feed) as fh:
            n = sum(1 for _ in fh)
        print("            done in %s; %s commits found in the object store"
              % (fmt_dur(time.time() - t0), f"{n:,}"))
    try:
        counts, biggest, secs = object_stats(gp)
        total_commits = counts.get("commit", 0)
        print("\nobject store (counted in %s)" % fmt_dur(secs))
        print("  " + ", ".join("%s %s" % (f"{v:,}", k) for k, v in sorted(counts.items())))
        if biggest:
            print("  biggest files stored: " + ", ".join(human(s) for s, _ in biggest))

        a = history_pass(gp, feed, False, args.max_seconds, total_commits)
        report_pass("history WITHOUT rename detection", a, total_commits)
        b = history_pass(gp, feed, True, args.max_seconds, total_commits)
        report_pass("history WITH rename detection (what file_history_for_list.py does; "
                    "it reads it twice)", b, total_commits)

        print("\nverdict")
        if not b["finished"] and a["finished"]:
            print("  rename detection is the bottleneck: the history reads quickly "
                  "without it (%s) but not with it." % fmt_dur(a["seconds"]))
        elif a["seconds"] and b["seconds"] > 3 * a["seconds"]:
            print("  rename detection makes it %.1fx slower (%s vs %s)."
                  % (b["seconds"] / a["seconds"], fmt_dur(b["seconds"]), fmt_dur(a["seconds"])))
        elif not a["finished"]:
            print("  even the plain history read is slow: the repo is simply large "
                  "(many commits or many changed files per commit).")
        else:
            print("  nothing unusual: this repo is not slow by itself. If it stalls in "
                  "the full run the cause is load (many big repos at once, memory).")
        if a["top"] and a["top"][0][0] > 20000:
            print("  a single commit touches %s files, which is very heavy for "
                  "rename detection." % f"{a['top'][0][0]:,}")
    finally:
        if stand_in:
            stand_in.__exit__()
    print("\ntotal %s" % fmt_dur(time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
