#!/usr/bin/env python3
"""READ ONLY. Per delivery and per file type (extension): how many files our
runs produced to send, how many the first copy put on the share, how many it
lost and why, how many were re-sent to p1/rerun, and how many are still not
delivered. Nothing on the share is read - the share side comes from what
reconcile_copied_files.py found (its --out: summary.csv, missing_files.csv)
and what resend_missing.py checked on the share (rerun_tracking: *_staged.csv,
*_check.csv).

    nohup python3 ext_reconcile.py --rec ~/reconcile_files2 \
        --out ~/ext_reconcile > ~/ext_reconcile.log 2>&1 &

Same deliveries and the same files as reconcile_copied_files.py (it imports
its list): the local data folders, our tools' temp files left out.

Type of a file:
  whole-file stores   files/<ext>/<ab>/<blob>.<ext>: the <ext> folder
  everything else     its name without a __case<n>, without the .txt our
                      text routes add (only where the delivery is text:
                      a.json.txt -> json, Dockerfile.txt -> (no extension)),
                      the part after the last dot, lower case (.gitignore ->
                      gitignore)

Columns (by_delivery_ext.csv; by_ext.csv sums them over the deliveries):
  produced          files the run made to send (local)
  first_copy        on the share from the first copy (produced - lost);
                    files the share kept under a __case name count here
  lost_case         lost to a case-only twin on the share
  lost_not_on_share not on the share (incl. names the share refuses)
  lost_tmp_name     skipped by the copies' *.tmp.* exclude
  lost_size         on the share with another size
  rerun_ok          re-sent to p1/rerun and found there at the right size
  rerun_failed      re-sent but not found / other size on the share
  still_missing     lost - rerun_ok (0 = every file delivered)
  delivered         first_copy + rerun_ok
The 7 Group 1 repositories never extracted before (re-run 30 Oct) are their
own delivery, sent to rerun only.

summary.txt: per delivery the totals, checked against reconcile's
source_files (must be equal - else the folders changed since that run).
"""
import argparse
import csv
import glob
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reconcile_copied_files as rc  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")
CASE = re.compile(r"__case\d+")
LOST = {"case overwritten": "lost_case", "not on share": "lost_not_on_share",
        ".tmp. in name": "lost_tmp_name", "size differs": "lost_size"}
COLS = ["produced", "first_copy", "lost_case", "lost_not_on_share", "lost_tmp_name", "lost_size",
        "rerun_ok", "rerun_failed", "still_missing", "delivered"]
FE7 = "Group 1 text, 7 repos re-run 30 Oct"


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def is_text(label):
    return not any(w in label for w in ("whole", "binary", "images"))


def ext_of(rel, kind, text):
    """rel: <unit>/<path> (for types the unit is the <ext> folder)."""
    if kind == "types":
        return rel.split("/", 1)[0].lower()
    b = CASE.sub("", os.path.basename(rel))
    if text and b.lower().endswith(".txt"):
        b = b[:-4]
    return b.rsplit(".", 1)[1].lower() if "." in b else "(no extension)"


def count_unit(i, label, srcs, unit, kind):
    text = is_text(label)
    rels = set()
    for s in srcs:
        got, _ = rc.listing(os.path.join(s, unit), False)
        rels.update(r for r in got if not rc.our_tmp(r))
    return i, Counter(ext_of(unit + "/" + r, kind, text) for r in rels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", default=os.path.expanduser("~/reconcile_files2"),
                    help="reconcile_copied_files.py --out")
    ap.add_argument("--track", default="/data/workarea/rerun_tracking",
                    help="resend_missing.py tracking folder")
    ap.add_argument("--out", default=os.path.expanduser("~/ext_reconcile"))
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ds = rc.deliveries()
    kind_of = {lab: kind for lab, _, _, kind in ds}
    n = defaultdict(Counter)                  # (delivery, ext) -> column -> count

    # ------------------------------------------------- produced (local data)
    jobs = []
    for i, (lab, srcs, dst, kind) in enumerate(ds):
        have = [s for s in srcs if os.path.isdir(s)]
        u = set()
        for s in have:
            u |= rc.units(s, kind)
        jobs += [(i, lab, have, x, kind) for x in sorted(u)]
    say("%d deliveries, %d folders to count, %d workers" % (len(ds), len(jobs), a.workers))
    t0 = time.time()
    with ProcessPoolExecutor(a.workers) as ex:
        futs = [ex.submit(count_unit, *j) for j in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            i, c = f.result()
            for e, m in c.items():
                n[(ds[i][0], e)]["produced"] += m
            if k % 2000 == 0:
                say("  %d / %d folders (%.0f s)" % (k, len(futs), time.time() - t0))

    # ------------------------------------------------- lost in the first copy
    ext_by_src = {}
    with open(os.path.join(a.rec, "missing_files.csv"), **KW) as fh:
        for r in csv.DictReader(fh):
            col = LOST.get(r["reason"])
            if not col:
                continue
            lab = r["delivery"]
            e = ext_of(r["share_file"], kind_of.get(lab, "repos"), is_text(lab))
            n[(lab, e)][col] += 1
            ext_by_src[(lab, r["source_file"])] = e

    # ------------------------------------------------- re-sent to rerun
    def staged(tag):
        p = os.path.join(a.track, tag + "_staged.csv")
        if not os.path.isfile(p):
            return [], set()
        bad = set()
        cp = os.path.join(a.track, tag + "_check.csv")
        if os.path.isfile(cp):
            with open(cp, **KW) as fh:
                bad = {r["path"] for r in csv.DictReader(fh)}
        with open(p, **KW) as fh:
            return list(csv.DictReader(fh)), bad

    rows, bad = staged("missing")
    for r in rows:
        lab = r["delivery"]
        e = ext_by_src.get((lab, r["source_file"]))
        if e is None:
            e = ext_of(r["rerun_path"], kind_of.get(lab, "repos"), is_text(lab))
        ok = not r["result"].startswith("FAILED") and r["rerun_path"] not in bad
        n[(lab, e)]["rerun_ok" if ok else "rerun_failed"] += 1
    rows, bad = staged("repos")
    for r in rows:
        e = ext_of(r["rerun_path"], "repos", True)
        ok = not r["result"].startswith("FAILED") and r["rerun_path"] not in bad
        n[(FE7, e)]["produced"] += 1
        n[(FE7, e)]["rerun_ok" if ok else "rerun_failed"] += 1

    # ------------------------------------------------- derived columns
    for k, c in n.items():
        lost = sum(c[x] for x in LOST.values())
        if k[0] == FE7:
            c["still_missing"] = c["produced"] - c["rerun_ok"]
        else:
            c["first_copy"] = c["produced"] - lost
            c["still_missing"] = lost - c["rerun_ok"]
        c["delivered"] = c["first_copy"] + c["rerun_ok"]

    order = [d[0] for d in ds] + [FE7]
    with open(os.path.join(a.out, "by_delivery_ext.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["delivery", "ext"] + COLS)
        for lab in order:
            for e in sorted(e for l, e in n if l == lab):
                w.writerow([lab, e] + [n[(lab, e)][c] for c in COLS])
    by_ext = defaultdict(Counter)
    for (lab, e), c in n.items():
        by_ext[e].update({k: c[k] for k in COLS})
    with open(os.path.join(a.out, "by_ext.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["ext", "deliveries"] + COLS)
        for e in sorted(by_ext, key=lambda x: -by_ext[x]["produced"]):
            w.writerow([e, sum(1 for l, x in n if x == e)] + [by_ext[e][c] for c in COLS])

    src_files = {}
    with open(os.path.join(a.rec, "summary.csv"), **KW) as fh:
        for r in csv.DictReader(fh):
            src_files[r["delivery"]] = int(r["source_files"] or 0)
    with open(os.path.join(a.out, "summary.txt"), "w", newline="\r\n") as fh:
        def out(s=""):
            print(s, flush=True)
            fh.write(s + "\n")
        out("%-40s %5s %10s %10s %7s %7s %5s %5s %8s %6s %7s %10s  %s"
            % ("delivery", "types", "produced", "1st copy", "case", "not on", ".tmp.", "size",
               "rerun ok", "failed", "missing", "delivered", "check vs reconcile"))
        tot = Counter()
        for lab in order:
            t = Counter()
            ts = 0
            for (l, e), c in n.items():
                if l == lab:
                    t.update({k: c[k] for k in COLS})
                    ts += 1
            if not ts:
                continue
            tot.update(t)
            ref = src_files.get(lab)
            chk = "-" if ref is None else ("ok" if ref == t["produced"] else "DIFFERS (reconcile %d)" % ref)
            out("%-40s %5d %10d %10d %7d %7d %5d %5d %8d %6d %7d %10d  %s"
                % (lab[:40], ts, t["produced"], t["first_copy"], t["lost_case"], t["lost_not_on_share"],
                   t["lost_tmp_name"], t["lost_size"], t["rerun_ok"], t["rerun_failed"],
                   t["still_missing"], t["delivered"], chk))
        out("%-40s %5d %10d %10d %7d %7d %5d %5d %8d %6d %7d %10d"
            % ("TOTAL", len(by_ext), tot["produced"], tot["first_copy"], tot["lost_case"],
               tot["lost_not_on_share"], tot["lost_tmp_name"], tot["lost_size"], tot["rerun_ok"],
               tot["rerun_failed"], tot["still_missing"], tot["delivered"]))
        out()
        out("types with files still missing: %d" % sum(1 for e in by_ext if by_ext[e]["still_missing"]))
        for e in sorted(by_ext, key=lambda x: -by_ext[x]["still_missing"]):
            if by_ext[e]["still_missing"]:
                out("  %-20s %d" % (e, by_ext[e]["still_missing"]))
    say("done -> %s (by_delivery_ext.csv, by_ext.csv, summary.txt)" % a.out)


if __name__ == "__main__":
    main()
