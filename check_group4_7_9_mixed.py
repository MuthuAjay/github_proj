#!/usr/bin/env python3
"""Check what is inside the 4.7+4.9 mixed types (run_group4_7_9_mixed.sh output).

Read only. For each path it takes the inner extension (a.xsd.deploy -> xsd,
a.dll.v2.deploy -> dll, a.tmp -> -) and prints, at repo level and inside 4.2:

  1. inner extension x route (text / whole / left out / no text)
  2. the file signature (first bytes) of every whole file kept to send and of
     every file moved to _not_sent, so we see that programs (MZ) are only on
     the left-out side and what the sent binaries really are
  3. warnings: programs among the sent files, dll / exe names sent as text,
     dll / exe names whose content is not a program

    python3 check_group4_7_9_mixed.py [--g /data/workarea/group4_7_9/mixed]

--move-programs (repo level): program files found by content (MZ / ELF) among
the whole files to send are moved to <store>/_not_sent/files/ like the dll /
exe.deploy ones, and their paths are added to not_text_left_out.csv. Run it
before the copy.
"""
import argparse
import csv
import glob
import os
import re
import sys
from collections import Counter, defaultdict

EXTS = ["db_backup", "pbl-old", "simflows", "deploy", "dump", "tmp", "out"]
NOT_SENT = re.compile(r"\.(dll|exe)(\.v[0-9]+)?\.deploy$", re.I)
KW = dict(newline="", encoding="utf-8", errors="surrogateescape")
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

SIGS = [(b"MZ", "program (MZ)"), (b"PK\x03\x04", "zip"), (b"\x1f\x8b", "gzip"),
        (b"%PDF", "pdf"), (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpeg"),
        (b"GIF8", "gif"), (b"\x00\x00\x01\x00", "ico"), (b"BM", "bmp"),
        (b"SQLite format 3", "sqlite"), (b"\xd0\xcf\x11\xe0", "ms office (old)"),
        (b"PGDMP", "postgres dump"), (b"BZh", "bzip2"), (b"\xfd7zXZ", "xz"),
        (b"7z\xbc\xaf", "7z"), (b"\x7fELF", "elf program"), (b"\xef\xbb\xbf", "utf-8 bom text"),
        (b"\xff\xfe", "utf-16 text"), (b"\xfe\xff", "utf-16 text")]


def sig(p):
    try:
        with open(p, "rb") as fh:
            b = fh.read(4096)
    except OSError:
        return "unreadable"
    if not b:
        return "empty"
    for m, name in SIGS:
        if b.startswith(m):
            return name
    if b"\x00" not in b:
        return "text-like"
    return "other binary"


def split(path):
    """-> (type, inner extension)"""
    low = path.lower()
    t = next((e for e in EXTS if low.endswith("." + e)), low.rsplit(".", 1)[-1])
    stem = os.path.basename(low)[: -len(t) - 1]
    stem = re.sub(r"\.v[0-9]+$", "", stem)
    inner = stem.rsplit(".", 1)[1] if "." in stem.lstrip(".") else "-"
    return t, inner if len(inner) <= 12 else "-"


def table(title, routes):
    print("\n## %s: type / inner extension x route" % title)
    cols = ["text", "whole", "left_out", "no_text"]
    print("  %-10s %-12s %7s %7s %8s %7s" % ("type", "inner", *cols))
    for (t, i), c in sorted(routes.items(), key=lambda kv: (kv[0][0], -sum(kv[1].values()))):
        print("  %-10s %-12s %7d %7d %8d %7d" % (t, i, *[c[k] for k in cols]))


def sigs(title, files):
    print("\n## %s: file signatures (%d files)" % (title, len(files)))
    c = Counter(sig(p) for p in files)
    for s, n in c.most_common():
        print("  %-18s %d" % (s, n))
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--g", default="/data/workarea/group4_7_9/mixed")
    ap.add_argument("--inner", default="/data/workarea/group4_2")
    ap.add_argument("--move-programs", action="store_true")
    a = ap.parse_args()
    g, warn = a.g, []

    # ---------------------------------------------------------- repo level
    routes = defaultdict(Counter)
    status = {}
    for m in glob.glob(os.path.join(g, "text", "_state", "*", "*", "manifest.csv")):
        for r in csv.DictReader(open(m, **KW)):
            k = (r["org"], r["repo"], r["path"])
            status[k] = r["status"]
            ti = split(r["path"])
            if r["status"] == "ok":
                routes[ti]["text"] += 1
                if NOT_SENT.search(r["path"]):
                    warn.append("dll / exe name sent as text: %s/%s %s" % k)
            elif r["status"] in ("binary", "partly_binary"):
                routes[ti]["left_out" if NOT_SENT.search(r["path"]) else "whole"] += 1
            else:
                routes[ti]["no_text"] += 1
    table("repo level", routes)

    nt = os.path.join(g, "not_text")
    sent, notsent = [], []
    for store in ("binary", "active"):
        for d, _, fs in os.walk(os.path.join(nt, store, "files")):
            sent += [os.path.join(d, f) for f in fs if ".tmp." not in f]
        for d, _, fs in os.walk(os.path.join(nt, store, "_not_sent", "files")):
            notsent += [os.path.join(d, f) for f in fs]
    c = sigs("repo level, whole files to send", sent)
    if (c["program (MZ)"] or c["elf program"]) and not a.move_programs:
        warn.append("%d program file(s) among the whole files to send (--move-programs moves them)"
                    % (c["program (MZ)"] + c["elf program"]))
    sigs("repo level, moved to _not_sent", notsent)

    # whole files to send per type (a file counts for the type of its first path)
    by_blob = defaultdict(list)
    for store in ("binary", "active"):
        m = os.path.join(nt, store, "manifest.csv")
        for r in csv.DictReader(open(m, **KW)) if os.path.isfile(m) else []:
            if r.get("stored_as"):
                by_blob[os.path.basename(r["stored_as"])].append(r)
    per = Counter(split(by_blob[os.path.basename(p)][0]["path"])[0] if by_blob.get(os.path.basename(p)) else "?"
                  for p in sent)
    print("\n## repo level, whole files to send per type")
    for t, n in per.most_common():
        print("  %-10s %d" % (t, n))

    progs = [p for p in sent if sig(p) in ("program (MZ)", "elf program")]
    if progs:
        print("\n## repo level, programs among the whole files to send%s"
              % (" -> moved to _not_sent" if a.move_programs else " (--move-programs moves them)"))
        rows = []
        for p in progs:
            store = "active" if os.sep + "active" + os.sep + "files" + os.sep in p else "binary"
            rel = os.path.relpath(p, os.path.join(nt, store, "files"))
            paths = sorted({(r["org"], r["repo"], r["path"]) for r in by_blob.get(os.path.basename(p), [])})
            for o, rp, pa in paths or [("?", "?", "?")]:
                print("  %-12s %-14s %s/%s %s" % (sig(p), os.path.basename(p)[:14], o, rp, pa))
                rows.append([o, rp, pa, split(pa)[0], "binary", "", "program binary (%s, by content: %s), not sent" % (sig(p), rel)])
            if a.move_programs:
                dest = os.path.join(nt, store, "_not_sent", "files", rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(p, dest)
        if a.move_programs:
            lo = os.path.join(g, "not_text_left_out.csv")
            with open(lo, "a", **KW) as fh:
                csv.writer(fh).writerows(rows)
            sent = [p for p in sent if p not in progs]
            print("  moved %d file(s); %d whole files left to send; paths added to %s" % (len(progs), len(sent), lo))

    # left-out dll / exe paths: is every stored version really a program?
    left_sig = Counter()
    for r in csv.DictReader(open(os.path.join(nt, "binary", "manifest.csv"), **KW)):
        if not NOT_SENT.search(r["path"]) or not r.get("stored_as"):
            continue
        s = r["stored_as"].replace("\\", "/")
        rel = s.split("/files/", 1)[1] if "/files/" in s else s[6:] if s.startswith("files/") else ""
        p = os.path.join(nt, "binary", "_not_sent", "files", rel)
        if not os.path.isfile(p):
            p = os.path.join(nt, "binary", "files", rel)
        left_sig[sig(p)] += 1
    print("\n## repo level, dll / exe.deploy versions left out: signatures")
    for s, n in left_sig.most_common():
        print("  %-18s %d" % (s, n))
    odd = sum(n for s, n in left_sig.items() if s != "program (MZ)")
    if odd:
        warn.append("%d dll / exe.deploy version(s) left out are not MZ programs" % odd)

    # biggest whole files to send, with their path
    where = {}
    for r in csv.DictReader(open(os.path.join(nt, "binary", "manifest.csv"), **KW)):
        if r.get("stored_as"):
            where.setdefault(os.path.basename(r["stored_as"]), "%s/%s %s" % (r["org"], r["repo"], r["path"]))
    print("\n## repo level, 15 biggest whole files to send")
    for p in sorted(sent, key=os.path.getsize, reverse=True)[:15]:
        print("  %8.3f GB  %-14s %s" % (os.path.getsize(p) / 1e9, sig(p), where.get(os.path.basename(p), "?")))

    # ---------------------------------------------------------- inside 4.2
    for run in (a.inner, a.inner + "_R"):
        txt, bino = os.path.join(run, "group4_7_9_mixed_text"), os.path.join(run, "group4_7_9_mixed_binary")
        if not os.path.isdir(txt):
            continue
        routes = defaultdict(Counter)
        for m in glob.glob(os.path.join(txt, "_state", "*", "*", "manifest.csv")):
            for r in csv.DictReader(open(m, **KW)):
                ti = split(r["path"])
                if r["status"] == "ok":
                    routes[ti]["text"] += 1
                    if NOT_SENT.search(r["path"]):
                        warn.append("[%s] dll / exe name sent as text: %s" % (run, r["path"]))
                elif r["status"] in ("binary", "partly_binary"):
                    routes[ti]["left_out" if NOT_SENT.search(r["path"]) else "whole"] += 1
                else:
                    routes[ti]["no_text"] += 1
        if not routes:
            print("\n## %s: nothing" % run)
            continue
        table(run, routes)
        keep, gone = [], []
        for d, _, fs in os.walk(bino):
            rel = os.path.relpath(d, bino)
            if rel.split(os.sep)[0] in ("_state", "_logs", "_tmp", "stats"):
                continue
            (gone if rel.split(os.sep)[0] == "_not_sent" else keep).extend(
                os.path.join(d, f) for f in fs if not f.endswith(".csv"))
        c = sigs(run + ", whole files to send", keep)
        if c["program (MZ)"] or c["elf program"]:
            warn.append("[%s] program file(s) among the whole files to send" % run)
        sigs(run + ", moved to _not_sent", gone)

    print("\n## warnings")
    for w in warn or ["none"]:
        print("  " + w)


if __name__ == "__main__":
    main()
