#!/usr/bin/env python3
"""Send again the data files reconcile_copied_files.py found missing on the
share - into a NEW folder, rerun, on the server and on the share; the first
deliveries are not touched. Reads its --out folder (summary.csv: delivery ->
share folder, missing_files.csv: delivery, reason, source_file, share_file,
bytes).

    server   /data/workarea/rerun/<delivery folder under p1>/<path>
    share    $P1/rerun/<delivery folder under p1>/<path>       (the same tree)
    tracking /data/workarea/rerun_tracking/ and $P1/rerun_tracking/
             (never inside rerun: the scanner takes every file there as data)

e.g. Group 1's  p1/32_ext/full_extract_delta_2/<org>/<repo>/<path>.txt
         ->     p1/rerun/32_ext/full_extract_delta_2/<org>/<repo>/<path>.txt

DRY RUN unless --go: writes the plan to the server's tracking folder and
prints the counts, copies nothing. --go copies to the server folder, then
rsyncs it to the share, then checks share against server (the share needs
sudo, the whole command inside sudo bash -c):

    python3 resend_missing.py --rec /home/ganeshk/reconcile_files2
    sudo bash -c 'cd /data/workarea/scripts && nohup python3 resend_missing.py \
        --rec /home/ganeshk/reconcile_files2 --go > /data/workarea/rerun.out 2>&1 &'

Sent (reasons of missing_files.csv): not on share, .tmp. in name (the
copies' '*.tmp.*' exclude skipped them), case overwritten (the share kept
its twin), size differs (the share's file is left as it is; this is the
source's). --skip REASON leaves a reason out, --only LABEL limits it to
deliveries of summary.csv. A source file no longer there is listed, not sent.

Names: the share ignores case, so two files sent here whose paths differ only
by case (or a file where another sent path has a folder - LICENSE vs
license/) would land on one: the first (sorted) keeps its name, the others
go as <name>__case<n>.<ext>, the same name on the server and the share; old
-> new in case_renamed.csv. Files go through <dest>.tmp.copy and a rename;
a file already there at the same size is skipped, so after a stop run the
same command again.

Tracking (CRLF): plan.csv (delivery, reason, source_file, rerun_path, bytes,
action), case_renamed.csv, with --go staged.csv (+ result) and check.csv
(path, problem) and check.txt.
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")
SEND = ["not on share", ".tmp. in name", "case overwritten", "size differs"]
OUR_TMP = re.compile(r"\.tmp\.(\d+(\.\d+)?|copy)$")
P1 = "/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1"
RSYNC = ["rsync", "-rt", "--no-perms", "--no-owner", "--no-group", "--partial"]


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def our_tmp(name):
    return bool(OUR_TMP.search(name)) or name.startswith(".tmp.")


def case_name(rel, k):
    d, base = os.path.split(rel)
    stem, dot, ext = base.rpartition(".")
    new = "%s__case%d.%s" % (stem, k, ext) if dot and stem else "%s__case%d" % (base, k)
    return os.path.join(d, new)


def keep_apart(rels):
    """rel -> rel on the share: paths equal but for case, or a file where
    another path has a folder, get __case<n> names (first sorted keeps its)."""
    dirs = {os.path.dirname(r).lower() for r in rels}
    dirs = {d for x in dirs for d in [x] + [x.rsplit("/", i)[0] for i in range(1, x.count("/") + 1)]}
    taken, out = set(dirs), {}
    for r in sorted(rels):
        new, k = r, 2
        while new.lower() in taken:
            new, k = case_name(r, k), k + 1
        taken.add(new.lower())
        out[r] = new
    return out


def stage_one(job):
    src, dest = job
    try:
        if os.path.isfile(dest) and os.path.getsize(dest) == os.path.getsize(src):
            return "already there"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest + ".tmp.copy")
        os.replace(dest + ".tmp.copy", dest)
        return "copied"
    except OSError as exc:
        return "FAILED: %s" % exc


def listing(root):
    got = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            if our_tmp(f):
                continue
            p = os.path.join(dp, f)
            try:
                got[os.path.relpath(p, root)] = os.stat(p).st_size
            except OSError:
                got[os.path.relpath(p, root)] = -1
    return got


def write_csv(path, head, rows):
    with open(path, "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(head)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", required=True, help="reconcile_copied_files.py --out folder")
    ap.add_argument("--only", action="append", default=[], metavar="LABEL")
    ap.add_argument("--skip", action="append", default=[], metavar="REASON", choices=SEND)
    ap.add_argument("--p1", default=P1, help="the share's p1 folder")
    ap.add_argument("--server", default="/data/workarea/rerun")
    ap.add_argument("--name", default="rerun", help="share folder under p1 (+ _tracking)")
    ap.add_argument("--go", action="store_true", help="copy (default: dry run, plan only)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    server = a.server.rstrip("/")
    s_track = server + "_tracking"
    share = os.path.join(a.p1, a.name)
    sh_track = share + "_tracking"
    for p in (share, sh_track):
        if os.path.realpath(p).startswith(os.path.realpath(server)):
            sys.exit("the share folder is inside the server folder: %s" % p)
    os.makedirs(s_track, exist_ok=True)

    dst_of = {}
    with open(os.path.join(a.rec, "summary.csv"), **KW) as fh:
        for r in csv.DictReader(fh):
            dst_of[r["delivery"]] = r["share"]
    want = set(SEND) - set(a.skip)

    rows, c = [], Counter()
    with open(os.path.join(a.rec, "missing_files.csv"), **KW) as fh:
        for r in csv.DictReader(fh):
            lab, why = r["delivery"], r["reason"]
            if a.only and lab not in a.only:
                continue
            if why not in want:
                c[(lab, why, "not sent (reason not asked for)")] += 1
                continue
            dst = dst_of.get(lab, "")
            base = os.path.relpath(dst, a.p1) if dst and not os.path.relpath(dst, a.p1).startswith("..") \
                else lab
            rows.append([lab, why, r["source_file"], os.path.join(base, r["share_file"]), r["bytes"]])

    names = keep_apart({x[3] for x in rows})
    plan, renamed = [], []
    for lab, why, src, rel, n in rows:
        new = names[rel]
        if not os.path.isfile(src):
            act = "left: source gone"
        else:
            act = "send" if new == rel else "send as __case"
            if new != rel:
                renamed.append([lab, src, rel, new])
        c[(lab, why, act)] += 1
        plan.append([lab, why, src, new, n, act])
    write_csv(os.path.join(s_track, "plan.csv"),
              ["delivery", "reason", "source_file", "rerun_path", "bytes", "action"], plan)
    write_csv(os.path.join(s_track, "case_renamed.csv"),
              ["delivery", "source_file", "path", "rerun_path"], renamed)

    say("plan (%s):" % ("copying" if a.go else "DRY RUN, nothing copied"))
    for (lab, why, act), n in sorted(c.items()):
        say("  %-40s %-18s %-32s %7d" % (lab, why, act, n))
    todo = [p for p in plan if p[5].startswith("send")]
    say("to send: %d file(s), %.1f MB, %d under a __case name" % (
        len(todo), sum(int(p[4] or 0) for p in todo) / 1e6, len(renamed)))
    say("  server %s, share %s, tracking %s and %s" % (server, share, s_track, sh_track))
    if not a.go:
        return

    # ------------------------------------------------------- 1. server copy
    say("=== 1. server copy -> %s" % server)
    res, staged = Counter(), []
    with ThreadPoolExecutor(a.workers) as ex:
        jobs = [(p[2], os.path.join(server, p[3])) for p in todo]
        for i, (p, got) in enumerate(zip(todo, ex.map(stage_one, jobs)), 1):
            staged.append(p + [got])
            res["FAILED" if got.startswith("FAILED") else got] += 1
            if got.startswith("FAILED"):
                say("  %s %s" % (got, p[2]))
            if i % 2000 == 0:
                say("  %d / %d" % (i, len(todo)))
    write_csv(os.path.join(s_track, "staged.csv"),
              ["delivery", "reason", "source_file", "rerun_path", "bytes", "action", "result"], staged)
    say("  %s" % dict(res))
    if res["FAILED"]:
        say("FINISHED WITH PROBLEMS: server copy failed for %d file(s), share not started - run again"
            % res["FAILED"])
        sys.exit(1)

    # -------------------------------------------------------- 2. the share
    say("=== 2. share -> %s" % share)
    os.makedirs(share, exist_ok=True)
    rc = subprocess.call(RSYNC + ["--exclude", "*.tmp.copy", server + "/", share + "/"])
    say("  rsync exit %d" % rc)

    # ------------------------------------------------------- 3. the check
    say("=== 3. check: server against share")
    srv, shr = listing(server), listing(share)
    low = {k.lower(): k for k in shr}
    probs = []
    for rel, n in sorted(srv.items()):
        k = rel if rel in shr else low.get(rel.lower())
        if k is None:
            probs.append([rel, "not on share"])
        elif shr[k] != n:
            probs.append([rel, "size differs (server %d, share %d)" % (n, shr[k])])
    planned = {p[3] for p in todo}
    probs += [[rel, "on the server, not in this plan (earlier run?)"] for rel in sorted(srv) if rel not in planned]
    write_csv(os.path.join(s_track, "check.csv"), ["path", "problem"], probs)
    bad = sum(1 for p in probs if not p[1].startswith("on the server"))
    lines = ["server files %d, share files %d, planned %d" % (len(srv), len(shr), len(planned)),
             "not on share / size differs: %d" % bad,
             "on the server, not in this plan: %d" % (len(probs) - bad)]
    with open(os.path.join(s_track, "check.txt"), "w", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    for l in lines:
        say("  " + l)

    # ------------------------------------------------------- 4. tracking
    os.makedirs(sh_track, exist_ok=True)
    rc2 = subprocess.call(RSYNC + [s_track + "/", sh_track + "/"])
    say("=== 4. tracking -> %s (rsync exit %d)" % (sh_track, rc2))
    ok = rc == 0 and rc2 == 0 and bad == 0
    say("ALL DONE" if ok else "FINISHED WITH PROBLEMS - run the same command again; see check.csv")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
