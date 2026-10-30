#!/usr/bin/env python3
"""Send again the data files reconcile_copied_files.py found missing on the
share, from its --out folder (summary.csv: delivery -> share folder,
missing_files.csv: delivery, reason, source_file, share_file, bytes).

DRY RUN unless --go: writes resend_plan.csv to --rec and prints the counts,
copies nothing. With --go it copies (the share needs sudo):

    python3 resend_missing.py --rec /home/ganeshk/reconcile_files2
    sudo nohup python3 resend_missing.py --rec /home/ganeshk/reconcile_files2 --go \
        > /home/ganeshk/resend.out 2>&1 &

By reason:
  not on share       copied to its path on the share
  .tmp. in name      the same (the copies' '*.tmp.*' exclude skipped them)
  case overwritten   copied under <name>__case<n>.<ext> beside the survivor
                     (n from 2, the first name the folder does not hold in
                     any case), the way the copy scripts do it; old -> new
                     in case_renamed_resend.csv
  size differs       only with --size-differs: the share's file is REPLACED.
                     Look at the dry run's list of them first.
Left, and listed in the plan with the reason:
  blocked            the share holds a folder where the file goes (or a file
                     where a folder goes) under another case - LICENSE vs
                     license/ - nothing written
  source gone        the source file is no longer there

--only LABEL (repeatable) limits it to deliveries of summary.csv.
Files go through <dest>.tmp.copy and a rename; a file already on the share
at the same size is skipped, so after a stop run the same command again.
Writes to --rec (CRLF): resend_plan.csv (delivery, reason, source_file,
share_dest, bytes, action), with --go resend_done.csv (+ result) and
case_renamed_resend.csv (delivery, source_file, share_dest). Tracking only,
never on the share's data folders.
"""
import argparse
import csv
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")
PLAIN = ("not on share", ".tmp. in name")


def say(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def blocked(dst, rel):
    """a folder at the file's place, or a file at one of its folders' places
    (the share ignores case, so os.path sees the other-case one)."""
    p = os.path.join(dst, rel)
    if os.path.isdir(p):
        return True
    parts = rel.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        q = os.path.join(dst, *parts[:i])
        if os.path.lexists(q) and not os.path.isdir(q):
            return True
    return False


class Names:
    """free __case<n> names per share folder, lower case, cached."""
    def __init__(self):
        self.taken = {}

    def next(self, folder, base):
        if folder not in self.taken:
            try:
                self.taken[folder] = {n.lower() for n in os.listdir(folder)}
            except OSError:
                self.taken[folder] = set()
        t = self.taken[folder]
        stem, dot, ext = base.rpartition(".")
        k = 2
        while True:
            new = "%s__case%d.%s" % (stem, k, ext) if dot and stem else "%s__case%d" % (base, k)
            if new.lower() not in t:
                t.add(new.lower())
                return new
            k += 1


def copy_one(job):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", required=True, help="reconcile_copied_files.py --out folder")
    ap.add_argument("--only", action="append", default=[], metavar="LABEL")
    ap.add_argument("--size-differs", action="store_true", help="also replace the share's files of another size")
    ap.add_argument("--go", action="store_true", help="copy (default: dry run, plan only)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    dst_of = {}
    with open(os.path.join(a.rec, "summary.csv"), **KW) as fh:
        for r in csv.DictReader(fh):
            dst_of[r["delivery"]] = r["share"]
    want = set(PLAIN) | {"case overwritten"} | ({"size differs"} if a.size_differs else set())

    # a rerun after a stop keeps the __case names the last --go gave
    prior, cr = {}, os.path.join(a.rec, "case_renamed_resend.csv")
    if os.path.isfile(cr):
        with open(cr, **KW) as fh:
            prior = {(r["delivery"], r["source_file"]): r["share_dest"] for r in csv.DictReader(fh)}
    names, plan, c = Names(), [], Counter()
    with open(os.path.join(a.rec, "missing_files.csv"), **KW) as fh:
        for r in csv.DictReader(fh):
            lab, why = r["delivery"], r["reason"]
            if a.only and lab not in a.only:
                continue
            if why not in want:
                c[(lab, why, "not asked for")] += 1
                continue
            dst, rel, src = dst_of.get(lab), r["share_file"], r["source_file"]
            if not dst:
                act, dest = "no share folder in summary.csv", ""
            elif not os.path.isfile(src):
                act, dest = "left: source gone", ""
            elif blocked(dst, rel):
                act, dest = "left: blocked (file / folder by case)", os.path.join(dst, rel)
            elif why == "case overwritten" and (lab, src) in prior:
                act, dest = "send as __case", prior[(lab, src)]
            elif why == "case overwritten":
                folder = os.path.dirname(os.path.join(dst, rel))
                dest = os.path.join(folder, names.next(folder, os.path.basename(rel)))
                act = "send as __case"
            else:
                act, dest = "replace" if why == "size differs" else "send", os.path.join(dst, rel)
            c[(lab, why, act)] += 1
            plan.append([lab, why, src, dest, r["bytes"], act])

    with open(os.path.join(a.rec, "resend_plan.csv"), "w", **KW) as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["delivery", "reason", "source_file", "share_dest", "bytes", "action"])
        w.writerows(plan)
    say("plan (%s):" % ("copying" if a.go else "DRY RUN, nothing copied"))
    for (lab, why, act), n in sorted(c.items()):
        say("  %-40s %-18s %-40s %7d" % (lab, why, act, n))
    todo = [p for p in plan if p[5] in ("send", "send as __case", "replace")]
    say("to copy: %d file(s), %.1f MB -> %s/resend_plan.csv"
        % (len(todo), sum(int(p[4] or 0) for p in todo) / 1e6, a.rec))
    if not a.go:
        return

    res = Counter()
    with ThreadPoolExecutor(a.workers) as ex, \
            open(os.path.join(a.rec, "resend_done.csv"), "w", **KW) as fd, \
            open(os.path.join(a.rec, "case_renamed_resend.csv"), "w", **KW) as fc:
        wd = csv.writer(fd, lineterminator="\r\n")
        wc = csv.writer(fc, lineterminator="\r\n")
        wd.writerow(["delivery", "reason", "source_file", "share_dest", "bytes", "action", "result"])
        wc.writerow(["delivery", "source_file", "share_dest"])
        for i, (p, got) in enumerate(zip(todo, ex.map(copy_one, [(p[2], p[3]) for p in todo])), 1):
            wd.writerow(p + [got])
            if p[5] == "send as __case" and not got.startswith("FAILED"):
                wc.writerow([p[0], p[2], p[3]])
            res[got if not got.startswith("FAILED") else "FAILED"] += 1
            if got.startswith("FAILED"):
                say("  %s %s" % (got, p[2]))
            if i % 2000 == 0:
                say("  %d / %d" % (i, len(todo)))
    say("done: %s%s" % (dict(res), " - FINISHED WITH PROBLEMS, run again to retry" if res["FAILED"] else " - ALL DONE"))
    sys.exit(1 if res["FAILED"] else 0)


if __name__ == "__main__":
    main()
