#!/usr/bin/env python3
"""
extension_rnd.py - research a few file extensions before deciding how to
treat them: where they are, how big, how many versions, and what they
really contain.

Works from extension_versions.py's per-repo state for those extensions
(pass 1 is enough):

    python3 extension_versions.py --extensions pkl,sentinel,dectest,noun \\
        --repos-root ... --active-root ... --out /data/workarea/rnd_counts \\
        --batch ...            (or run_ext_versions.sh with PASSES=1)

and the archive, from which it reads a SAMPLE of versions per extension
(git cat-file - the repos are never changed) and looks at the bytes.

Per extension (rnd_report.md):
  counts      repos, files, still in the active copy, history only,
              versions, vendored files, total / median / largest size
  where       the top repos, the top folders (first two path parts), the
              most common file names
  content     of the sampled versions: empty / text / binary, the file
              signature found (zip, pdf, pickle, ...), and per sample the
              first lines (text) or the signature (binary)
  pickle      for .pkl: the protocol and the Python classes the pickle
              would create (pandas DataFrame, numpy array, sklearn model,
              ...), read with pickletools - NOTHING IS UNPICKLED, so no
              code in the file runs

samples.csv: one row per sampled version - ext, org, repo, path, blob,
bytes, kind, signature, detail, preview (first 200 characters of text).
The preview may contain the data the files hold - keep the output where
the extracted data is kept.

Usage:
    python3 extension_rnd.py --counts /data/workarea/rnd_counts \\
        --repos-root /data/workarea/archive --out /data/workarea/rnd \\
        --samples 25
"""

import argparse
import csv
import datetime
import io
import os
import pickletools
import random
import statistics
import subprocess
import sys
from collections import Counter, defaultdict

from extension_versions import STATE, read_json, read_rows
from extract_commits import GIT, spawn
from extract_versions import ObjectStore

HEAD = 8192
PRINTABLE = set(range(32, 127)) | {9, 10, 13, 12}
MAGIC = [
    (b"\x80\x05", "pickle (protocol 5)"), (b"\x80\x04", "pickle (protocol 4)"),
    (b"\x80\x03", "pickle (protocol 3)"), (b"\x80\x02", "pickle (protocol 2)"),
    (b"%PDF", "pdf"), (b"PK\x03\x04", "zip"), (b"\x1f\x8b", "gzip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole (legacy office)"),
    (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF8", "gif"),
    (b"\x93NUMPY", "numpy .npy"), (b"BZh", "bzip2"), (b"\xfd7zXZ", "xz"),
    (b"SQLite format 3", "sqlite"), (b"\x7fELF", "elf"), (b"MZ", "windows exe"),
    (b"version https://git-lfs", "git lfs pointer"),
]


def signature(data):
    for m, name in MAGIC:
        if data.startswith(m):
            return name
    return ""


def kind_of(data):
    if not data:
        return "empty"
    head = data[:HEAD]
    if b"\0" in head:
        return "binary"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:       # not just a cut multi-byte char
            return "binary"
    printable = sum(1 for b in head if b in PRINTABLE or b >= 0x80)
    return "text" if printable / len(head) > 0.95 else "binary"


def pickle_classes(data):
    """(protocol, [module.Class, ...]) without unpickling: the GLOBAL /
    STACK_GLOBAL opcodes name what the pickle would import."""
    proto, names, strings = "", [], []
    try:
        for op, arg, _pos in pickletools.genops(io.BytesIO(data)):
            if op.name == "PROTO":
                proto = arg
            elif op.name == "GLOBAL":
                names.append(str(arg).replace(" ", "."))
            elif op.name in ("SHORT_BINUNICODE", "BINUNICODE",
                             "BINUNICODE8", "UNICODE"):
                strings.append(arg)
            elif op.name == "STACK_GLOBAL" and len(strings) >= 2:
                names.append("%s.%s" % (strings[-2], strings[-1]))
            elif op.name == "STOP":
                break
    except Exception as exc:                  # noqa: BLE001 - odd file
        names.append("(not readable as pickle: %s)" % type(exc).__name__)
    return proto, names


def read_blobs(gp, blobs):
    """{blob: bytes} via one cat-file --batch."""
    out = {}
    if not blobs:
        return out
    proc = spawn(GIT + ["-C", gp, "cat-file", "--batch"],
                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL)
    try:
        for b in blobs:
            proc.stdin.write(b.encode() + b"\n")
            proc.stdin.flush()
            head = proc.stdout.readline().split()
            if len(head) < 3 or head[1] != b"blob":
                continue
            data = proc.stdout.read(int(head[2]))
            proc.stdout.read(1)
            out[b] = data
    finally:
        proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()
    return out


def top_folder(path):
    parts = path.split("/")[:-1]
    return "/".join(parts[:2]) + "/" if parts else "(repo root)"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--counts", required=True,
                    help="extension_versions.py --out folder for these "
                         "extensions (pass 1 done)")
    ap.add_argument("--repos-root", required=True, help="the archive repos")
    ap.add_argument("--out", required=True, help="folder for the results")
    ap.add_argument("--samples", type=int, default=25,
                    help="versions sampled per extension (default 25)")
    ap.add_argument("--seed", type=int, default=1,
                    help="sampling seed - the same seed, the same sample")
    args = ap.parse_args()
    state = os.path.join(args.counts, STATE)
    if not os.path.isdir(state):
        sys.exit("no _state folder under " + args.counts)
    os.makedirs(args.out, exist_ok=True)

    files = defaultdict(list)             # ext -> [(org, repo, file row)]
    versions = defaultdict(list)          # ext -> [(org, repo, path, blob, n)]
    for org in sorted(os.listdir(state)):
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            sd = os.path.join(od, repo)
            done = read_json(os.path.join(sd, "done.json"))
            if not done or done.get("status") != "ok":
                continue
            fr = read_rows(os.path.join(sd, "files.csv"))
            if not fr:
                continue
            ext_of = {}
            for f in fr:
                files[f["ext"]].append((org, repo, f))
                ext_of[f["path"]] = f["ext"]
            for v in read_rows(os.path.join(sd, "versions.csv")):
                e = ext_of.get(v["path"])
                if e:
                    n = int(v["bytes"]) if v["bytes"].isdigit() else None
                    versions[e].append((org, repo, v["path"], v["blob"], n))

    rng = random.Random(args.seed)
    sample_rows = []
    md = ["# Extension R&D", "",
          "Generated %s by extension_rnd.py from `%s`. Samples: up to %d "
          "versions per extension, spread over repos; pickles are inspected "
          "with pickletools, never unpickled." % (
              datetime.date.today().isoformat(), args.counts, args.samples),
          ""]
    for e in sorted(files, key=lambda x: -len(files[x])):
        fl, vl = files[e], versions[e]
        hist = [f for _o, _r, f in fl if f["in_history"] == "yes"]
        sizes = sorted(n for *_x, n in vl if n is not None)
        repos = Counter((o, r) for o, r, f in fl)
        folders = Counter(top_folder(f["path"]) for _o, _r, f in fl)
        names = Counter(f["path"].rsplit("/", 1)[-1] for _o, _r, f in fl)
        md += ["## .%s" % e, "",
               "| | |", "|---|---:|",
               "| repos | %s |" % f"{len(repos):,}",
               "| files in history | %s |" % f"{len(hist):,}",
               "| ... still in the active copy | %s |"
               % f"{sum(1 for f in hist if f['at_head'] == 'yes'):,}",
               "| ... history only | %s |"
               % f"{sum(1 for f in hist if f['at_head'] == 'no'):,}",
               "| files only in the active copy | %s |"
               % f"{sum(1 for _o, _r, f in fl if f['in_history'] != 'yes'):,}",
               "| versions (distinct contents per file) | %s |" % f"{len(vl):,}",
               "| vendored files (node_modules, site-packages, ...) | %s |"
               % f"{sum(1 for f in hist if f['vendored']):,}",
               "| total size of the versions | %.2f MB |"
               % (sum(sizes) / 1e6),
               "| median / largest version | %s / %s bytes |" % (
                   f"{int(statistics.median(sizes)):,}" if sizes else "-",
                   f"{sizes[-1]:,}" if sizes else "-"), "",
               "**Top repos:** " + ", ".join(
                   "%s/%s (%d)" % (o, r, n) for (o, r), n in repos.most_common(8)),
               "", "**Top folders:** " + ", ".join(
                   "`%s` (%d)" % (k, n) for k, n in folders.most_common(8)),
               "", "**Common file names:** " + ", ".join(
                   "`%s` (%d)" % (k, n) for k, n in names.most_common(8)), ""]

        # sample: one version per repo first, round-robin, then the rest
        by_repo = defaultdict(list)
        for v in vl:
            by_repo[(v[0], v[1])].append(v)
        for lst in by_repo.values():
            rng.shuffle(lst)
        order = list(by_repo)
        rng.shuffle(order)
        picked = []
        while len(picked) < args.samples and any(by_repo.values()):
            for k in order:
                if by_repo[k] and len(picked) < args.samples:
                    picked.append(by_repo[k].pop())
        kinds, sigs, classes = Counter(), Counter(), Counter()
        per_repo = defaultdict(list)
        for v in picked:
            per_repo[(v[0], v[1])].append(v)
        for (o, r), vs in per_repo.items():
            try:
                with ObjectStore(os.path.join(args.repos_root, o, r)) as gp:
                    data = read_blobs(gp, [v[3] for v in vs])
            except Exception as exc:          # noqa: BLE001 - one repo
                data = {}
                print("  %s/%s: %s" % (o, r, exc), file=sys.stderr)
            for v in vs:
                b = data.get(v[3])
                if b is None:
                    sample_rows.append([e, o, r, v[2], v[3], v[4] or "",
                                        "not read", "", "", ""])
                    kinds["not read"] += 1
                    continue
                k, sig = kind_of(b), signature(b)
                detail = preview = ""
                if sig.startswith("pickle") or e == "pkl":
                    proto, cl = pickle_classes(b)
                    classes.update(set(cl))
                    detail = "protocol %s; %s" % (proto, ", ".join(
                        sorted(set(cl))[:8]) or "no classes (plain data)")
                if k == "text":
                    preview = b[:200].decode("utf-8", "replace")
                kinds[k] += 1
                if sig:
                    sigs[sig] += 1
                sample_rows.append([e, o, r, v[2], v[3], len(b), k, sig,
                                    detail, preview])
        md += ["**Sampled %d version(s):** %s" % (
            len(picked), ", ".join("%s %d" % kv for kv in kinds.most_common())
            or "-")]
        if sigs:
            md += ["", "**Signatures:** " + ", ".join(
                "%s %d" % kv for kv in sigs.most_common())]
        if classes:
            md += ["", "**Python classes in the pickles** (in how many "
                   "samples):", "", "| class | samples |", "|---|---:|"]
            md += ["| `%s` | %d |" % kv for kv in classes.most_common(20)]
        md += ["", "| repo | path | bytes | kind | detail / first line |",
               "|---|---|---:|---|---|"]
        for row in [x for x in sample_rows if x[0] == e][:args.samples]:
            first = row[8] or (row[9].splitlines()[0] if row[9] else "")
            md.append("| %s/%s | `%s` | %s | %s | %s |" % (
                row[1], row[2], row[3][:70], row[5], row[6] + (
                    " (%s)" % row[7] if row[7] else ""),
                first.replace("|", "/")[:90]))
        md.append("")

    with open(os.path.join(args.out, "samples.csv"), "w", newline="",
              encoding="utf-8", errors="backslashreplace") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "org", "repo", "path", "blob", "bytes", "kind",
                    "signature", "detail", "preview"])
        w.writerows(sample_rows)
    with open(os.path.join(args.out, "rnd_report.md"), "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")
    print("%d extension(s), %d sample(s) -> %s" % (
        len(files), len(sample_rows), os.path.join(args.out, "rnd_report.md")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
