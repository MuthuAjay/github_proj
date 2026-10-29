#!/usr/bin/env python3
"""READ ONLY. File by file: which data files of each delivery are not on the
share, and why. Lists the local source (what was to be sent) and the share
copy side by side, per repo folder (text, inside-archive steps) or per type
folder (whole-file stores files/<ext>/), in parallel.

    nohup python3 reconcile_copied_files.py --out ~/reconcile_files --workers 32 \
        > ~/reconcile_files.log 2>&1 &

  --only TEXT       only deliveries whose label contains TEXT (repeatable);
                    time-test one first, e.g. --only "4.2_R group5"
  --pair LABEL=LOCAL=SHARE   add a delivery (share folder relative to p1;
                    several LOCAL for one SHARE: LOCAL1,LOCAL2)
  --no-sizes        compare names only (default: names and sizes)
  --list            print the deliveries and exit

Data = every file under the source except its top-level tracking folders
(_state, _logs, stats, stats_combined, _tmp, _removed_duplicates, _not_sent,
nested) and files directly in the source folder (summary.csv ...). Our own
temp files (<name>.tmp.<pid>[.<n>], .tmp.copy, names starting .tmp.) are not
data and only counted.

A source file not on the share under its exact name is put down as:
  case overwritten  another source file has the same path in another case
                    (A.txt / a.txt): the share ignores case, so only one of
                    the two is there - this one is lost
  other-case path   (not a failure) the only source file with this path; the
                    share holds it with a folder in another case (Src/ and
                    src/ in the source end up in one folder on the share)
  sent as __case    the share holds it under a __case<n> name
  .tmp. in name     a real data file whose name contains .tmp. - the copies'
                    --exclude "*.tmp.*" left it out
  not on share      none of the above (copy failed / interrupted / not run)
and a file on both sides with another size as 'size differs'.

Writes to --out (CRLF):
  summary.csv          per delivery: source / share files and each reason
  missing_files.csv    delivery, reason, source_file, share_file, bytes
  extra_on_share.csv   delivery, share_file - on the share, not in the source
                       (older deliveries, __case copies, partial temps)
"""
import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

W = os.environ.get("RECORD_W", "/data/workarea")
P1 = os.environ.get("RECORD_P1", "/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1")
SKIP = {"_state", "_logs", "stats", "stats_combined", "_tmp", "_removed_duplicates",
        "_not_sent", "nested"}
OUR_TMP = re.compile(r"\.tmp\.(\d+(\.\d+)?|copy)$")
CASE = re.compile(r"__case\d+")
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")

# label, [local sources], share folder (relative to p1), kind
# kind: repos = <org>/<repo>/... ; types = files/<ext>/<ab>/<name>
FIXED = [
    ("Group 1 text", ["full_extract_delta_2"], "32_ext/full_extract_delta_2", "repos"),
    ("Group 2 whole files", ["binary_versions/files"], "group2/binary/files", "types"),
    ("Group 2 text", ["text9_extract_delta"], "group2/text", "repos"),
    ("Group 3 pdf", ["group3/pdf_merged"], "group3/pdf", "repos"),
    ("Group 3 pkl", ["group3/pkl_text"], "group3/pkl", "repos"),
    ("4.5_R text", ["group4_5_R/extract_delta"], "group4_5_R", "repos"),
    ("4.7+4.9 group1 text", ["group4_7_9/group1/text"], "group4_7_9/group1_text", "repos"),
    ("4.7+4.9 group1 whole", ["group4_7_9/group1/not_text/binary/files",
                              "group4_7_9/group1/not_text/active/files"],
     "group4_7_9/group1_binary/files", "types"),
    ("4.7+4.9 group2 whole", ["group4_7_9/group2_binary/files", "group4_7_9/group2_active/files"],
     "group4_7_9/group2_binary/files", "types"),
    ("4.7+4.9 group3 text", ["group4_7_9/group3/text"], "group4_7_9/group3_text", "repos"),
    ("4.7+4.9 tabular whole", ["group4_7_9/tabular/tabular_binary/files",
                               "group4_7_9/tabular/tabular_active/files"],
     "group4_7_9/tabular_binary/files", "types"),
    ("4.7+4.9 containers text", ["group4_7_9/bi/data/data_text"], "group4_7_9/bi_data/data_text", "repos"),
    ("4.7+4.9 containers images", ["group4_7_9/bi/data/files"], "group4_7_9/bi_data/files", "types"),
    ("4.7+4.9 mixed text", ["group4_7_9/mixed/text"], "group4_7_9/mixed_text", "repos"),
    ("4.7+4.9 mixed whole", ["group4_7_9/mixed/not_text/binary/files",
                             "group4_7_9/mixed/not_text/active/files"],
     "group4_7_9/mixed_binary/files", "types"),
]
INNER_RUNS = ["group4_2", "group4_2_R"]    # every step folder with a _state
NOT_SENT = {"4_2_extract", "counts", "unpacked"}    # work steps, never delivered


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def deliveries():
    out = [(lab, [os.path.join(W, s) for s in src], os.path.join(P1, dst), kind)
           for lab, src, dst, kind in FIXED]
    for run in INNER_RUNS:
        base = os.path.join(W, run)
        if not os.path.isdir(base):
            continue
        for step in sorted(os.listdir(base)):
            if step in NOT_SENT or not os.path.isdir(os.path.join(base, step, "_state")):
                continue
            # 4.7+4.9 inside the archives went under p1/group4_7_9/<run>/<step>
            dst = os.path.join(P1, "group4_7_9", run, step) if step.startswith("group4_7_9") \
                else os.path.join(P1, run, step)
            out.append(("%s %s" % (run, step), [os.path.join(base, step)], dst, "repos"))
    return out


def units(root, kind):
    """repo folders <org>/<repo> (repos) or type folders <ext> (types)."""
    found = set()
    try:
        tops = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return found
    for t in tops:
        if kind == "types":
            found.add(t.name)
        elif t.name not in SKIP:
            try:
                found.update(t.name + "/" + e.name for e in os.scandir(t.path)
                             if e.is_dir(follow_symlinks=False))
            except OSError:
                pass
    return found


def listing(root, sizes):
    """rel path -> size (or -1) of every file under root; errors counted."""
    files, errs = {}, []
    for dp, dns, fns in os.walk(root, onerror=errs.append, followlinks=True):
        for f in fns:
            p = os.path.join(dp, f)
            s = -1
            if sizes:
                try:
                    s = os.stat(p).st_size
                except OSError:
                    s = -2
            files[os.path.relpath(p, root)] = s
    return files, len(errs)


def our_tmp(rel):
    b = os.path.basename(rel)
    return bool(OUR_TMP.search(b)) or b.startswith(".tmp.")


def check(i, label, srcs, dst, unit, sizes):
    src = {}
    for s in srcs:
        got, _ = listing(os.path.join(s, unit), sizes)
        for rel, n in got.items():
            src.setdefault(rel, (os.path.join(s, unit, rel), n))
    shr, errs = listing(os.path.join(dst, unit), sizes)
    c = Counter(share_unreadable=errs)
    miss, extra = [], []
    low = {k.lower(): k for k in shr}
    src_low = Counter(r.lower() for r in src if not our_tmp(r))
    uncased = {CASE.sub("", k).lower() for k in shr if CASE.search(k)}
    # case twins (same path but for case): the share holds one file for the
    # group - the survivor is the twin of that name and size, else of that size
    groups = defaultdict(list)
    for r in src:
        if src_low[r.lower()] > 1:
            groups[r.lower()].append(r)
    survivor = {}
    for L, g in groups.items():
        if L in low:
            k = low[L]
            same = [r for r in g if not sizes or src[r][1] == shr[k]]
            survivor[L] = k if k in same else (same[0] if same else None)
    for rel, (full, n) in src.items():
        if our_tmp(rel):
            c["our_temp_in_source"] += 1
            continue
        c["source_files"] += 1
        sf = os.path.join(unit, rel)
        twin = src_low[rel.lower()] > 1
        if twin and CASE.sub("", rel).lower() not in uncased:
            if survivor.get(rel.lower()) == rel:
                c["on_share" if rel in shr else "other-case path"] += 1
            else:
                c["case overwritten"] += 1
                miss.append((label, "case overwritten", full, sf, n))
            continue
        if rel in shr:
            if sizes and shr[rel] != n:
                c["size differs"] += 1
                miss.append((label, "size differs", full, sf, n))
            else:
                c["on_share"] += 1
            continue
        if ".tmp." in os.path.basename(rel):
            why = ".tmp. in name"
        elif CASE.sub("", rel).lower() in uncased:
            why = "sent as __case"
        elif rel.lower() in low:
            if sizes and shr[low[rel.lower()]] != n:
                c["size differs"] += 1
                miss.append((label, "size differs", full, sf, n))
            else:
                c["other-case path"] += 1
            continue
        else:
            why = "not on share"
        c[why] += 1
        miss.append((label, why, full, sf, n))
    for rel in shr:
        if our_tmp(rel):
            c["our_temp_on_share"] += 1
        elif rel not in src and rel.lower() not in src_low:
            c["extra_on_share"] += 1
            extra.append((label, os.path.join(unit, rel)))
    c["share_files"] = len(shr) - c["our_temp_on_share"]
    return i, c, miss, extra


REASONS = ["on_share", "other-case path", "not on share", "case overwritten", "sent as __case", ".tmp. in name",
           "size differs", "extra_on_share", "our_temp_in_source", "our_temp_on_share",
           "share_unreadable"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/reconcile_files"))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--pair", action="append", default=[])
    ap.add_argument("--no-sizes", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    ds = deliveries()
    for p in a.pair:
        lab, loc, sh = p.split("=", 2)
        ds.append((lab, loc.split(","), os.path.join(P1, sh), "repos" if not sh.rstrip("/").endswith("files") else "types"))
    if a.only:
        ds = [d for d in ds if any(o in d[0] for o in a.only)]
    if a.list:
        for lab, srcs, dst, kind in ds:
            print("%-40s %-6s %s -> %s" % (lab, kind, ",".join(srcs), dst))
        return
    os.makedirs(a.out, exist_ok=True)

    status, jobs = {}, []
    for i, (lab, srcs, dst, kind) in enumerate(ds):
        have = [s for s in srcs if os.path.isdir(s)]
        if not have:
            status[i] = "no local source"
            continue
        if not os.path.isdir(dst):
            status[i] = "not copied (no share folder)"
        u = set()
        for s in have:
            u |= units(s, kind)
        if os.path.isdir(dst):
            u |= units(dst, kind)
        say("  %-40s %6d %s folder(s)" % (lab, len(u), "repo" if kind == "repos" else "type"))
        jobs += [(i, lab, have, dst, x) for x in sorted(u)]
    say("%d deliveries, %d folders to compare, %d workers" % (len(ds), len(jobs), a.workers))

    tot = defaultdict(Counter)
    t0 = time.time()
    with open(os.path.join(a.out, "missing_files.csv"), "w", **KW) as fm, \
            open(os.path.join(a.out, "extra_on_share.csv"), "w", **KW) as fe, \
            ProcessPoolExecutor(a.workers) as ex:
        wm = csv.writer(fm, lineterminator="\r\n")
        we = csv.writer(fe, lineterminator="\r\n")
        wm.writerow(["delivery", "reason", "source_file", "share_file", "bytes"])
        we.writerow(["delivery", "share_file"])
        futs = [ex.submit(check, *j, not a.no_sizes) for j in jobs]
        for n, f in enumerate(as_completed(futs), 1):
            i, c, miss, extra = f.result()
            tot[i].update(c)
            wm.writerows(miss)
            we.writerows(extra)
            if n % 2000 == 0:
                say("  %d / %d folders (%.0f s)" % (n, len(futs), time.time() - t0))

    with open(os.path.join(a.out, "summary.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["delivery", "source", "share", "status", "source_files", "share_files"] + REASONS)
        for i, (lab, srcs, dst, kind) in enumerate(ds):
            c = tot[i]
            bad = sum(c[r] for r in ("not on share", "case overwritten", ".tmp. in name", "size differs"))
            st = status.get(i) or ("OK" if not bad else "MISSING / DIFFERENT")
            w.writerow([lab, ",".join(srcs), dst, st, c["source_files"], c["share_files"]]
                       + [c[r] for r in REASONS])
            say("  %-40s %-28s source %9d  share %9d  on share %9d  not %6d  case lost %5d  __case %4d  .tmp. %4d  size %4d  extra %6d"
                % (lab, st, c["source_files"], c["share_files"], c["on_share"], c["not on share"],
                   c["case overwritten"], c["sent as __case"], c[".tmp. in name"], c["size differs"],
                   c["extra_on_share"]))
    say("done -> %s" % a.out)


if __name__ == "__main__":
    main()
