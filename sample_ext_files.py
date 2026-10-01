#!/usr/bin/env python3
"""
sample_ext_files.py - save a few real files of each given extension out of
the archive's history, to open and look at.

The repos are picked from all_extension_counts.py's by_repo.csv (the repos
that have the extension), shuffled with --seed, one file per repo until
--per-ext files are saved (more per repo when fewer repos have it). The
newest version of each picked file is read with git cat-file; the archive is
never changed.

Output, under --out:
  <ext>/<n>__<org>__<repo>__<path with / as __>   the sampled file itself
  <ext>/<same>.inspect.txt   for pickles (.pkl, or a pickle signature): the
                             protocol, the Python classes it would create and
                             every text string in it - read with pickletools,
                             NOTHING IS UNPICKLED - plus a raw "strings" scan
                             of the bytes (runs of 4+ readable characters).
                             This is what a text conversion of the file would
                             give a PII scanner.
  index.csv                  one row per sample: ext, n, org, repo, path,
                             blob, last change date, bytes, kind (empty /
                             text / binary), signature, saved_as
  index.md                   the same, readable, with the first line of each
                             text file and the classes of each pickle

The samples are real data from the repositories - keep the folder where the
extracted data is kept.

Usage:
    python3 sample_ext_files.py --by-repo /data/workarea/all_ext_counts/by_repo.csv \\
        --repos-root /data/workarea/archive --out /data/workarea/ext_samples \\
        --extensions pkl,sentinel,decTest,noun --per-ext 5
"""

import argparse
import csv
import datetime
import io
import os
import pickletools
import random
import re
import sys
import time
from collections import defaultdict

from extension_rnd import kind_of, pickle_classes, read_blobs, signature
from extension_versions import read_history
from file_history_for_list import RecoveredRepo, RepoJob, git_can_open
from repo_extension_summary import load_extensions

STRING_OPS = {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE",
              "STRING", "BINSTRING", "SHORT_BINSTRING"}
RAW_STRINGS = re.compile(rb"[\x20-\x7e]{4,}")


def history(rp, ext, job):
    """{path: rec} of read_history for one extension, through recovery for
    a repo with no HEAD/refs."""
    recovery = None if git_can_open(rp) else RecoveredRepo(rp)
    try:
        if recovery:
            recovery.__enter__()
            recovery.add_all_commits_as_refs()
            gp = recovery.tmp
        else:
            gp = rp
        return read_history(gp, {ext}, job), gp, recovery
    except Exception:
        if recovery:
            recovery.__exit__(None, None, None)
        raise


def pickle_strings(data, limit=5000):
    out = []
    try:
        for op, arg, _pos in pickletools.genops(io.BytesIO(data)):
            if op.name in STRING_OPS and isinstance(arg, (str, bytes)):
                s = arg if isinstance(arg, str) else arg.decode("latin-1")
                out.append(s)
                if len(out) >= limit:
                    break
            elif op.name == "STOP":
                break
    except Exception as exc:                      # noqa: BLE001 - odd file
        out.append("(stopped: %s)" % type(exc).__name__)
    return out


def inspect_text(data, limit=5000):
    proto, classes = pickle_classes(data)
    strs = pickle_strings(data, limit)
    raw = [m.decode("ascii") for m in RAW_STRINGS.findall(data)[:limit]]
    lines = ["signature:  %s" % (signature(data) or "-"),
             "protocol:   %s" % (proto if proto != "" else "-"),
             "bytes:      %d" % len(data), "",
             "== classes the pickle would create (%d)" % len(set(classes))]
    lines += sorted(set(classes)) or ["(none - plain Python data)"]
    lines += ["", "== text strings in the pickle (%d%s)"
              % (len(strs), "+" if len(strs) >= limit else "")]
    lines += strs or ["(none)"]
    lines += ["", "== raw strings scan of the bytes (%d%s, 4+ readable chars)"
              % (len(raw), "+" if len(raw) >= limit else "")]
    lines += raw or ["(none)"]
    return "\n".join(lines) + "\n"


def safe_name(s, n=180):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s if len(s) <= n else s[:n // 2] + "~" + s[-(n // 2):]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--by-repo", required=True,
                    help="all_extension_counts.py by_repo.csv")
    ap.add_argument("--repos-root", required=True, help="the archive repos")
    ap.add_argument("--out", required=True, help="folder for the samples")
    ap.add_argument("--extensions", required=True,
                    help="comma list, e.g. pkl,sentinel,decTest,noun")
    ap.add_argument("--per-ext", type=int, default=5,
                    help="files per extension (default 5)")
    ap.add_argument("--max-bytes", type=int, default=50_000_000,
                    help="skip files bigger than this (default 50 MB)")
    ap.add_argument("--seed", type=int, default=1,
                    help="the same seed picks the same files")
    args = ap.parse_args()
    exts = load_extensions(args.extensions)
    os.makedirs(args.out, exist_ok=True)

    repos_of = defaultdict(list)
    with open(args.by_repo, newline="", encoding="utf-8",
              errors="replace") as fh:
        for r in csv.DictReader(fh):
            if r.get("ext") in exts and (r.get("files_in_history") or "0") != "0":
                repos_of[r["ext"]].append((r["org"], r["repo"]))
    rng = random.Random(args.seed)
    index = []
    for e in exts:
        cands = repos_of.get(e, [])
        rng.shuffle(cands)
        print("%s: %d repo(s) have it" % (e, len(cands)), file=sys.stderr,
              flush=True)
        got, per_repo_round, t0 = [], 1, time.time()
        while len(got) < args.per_ext and per_repo_round <= args.per_ext:
            for org, repo in cands:
                if len(got) >= args.per_ext:
                    break
                taken = {g[2] for g in got if g[0] == org and g[1] == repo}
                if len(taken) >= per_repo_round:
                    continue
                rp = os.path.join(args.repos_root, org, repo)
                job = RepoJob(org, repo, 0)
                job.start = time.time()
                try:
                    hist, gp, recovery = history(rp, e, job)
                except Exception as exc:          # noqa: BLE001 - one repo
                    print("  %s/%s: %s" % (org, repo, exc), file=sys.stderr)
                    continue
                try:
                    paths = [p for p, r in hist.items()
                             if r["latest"] and p not in taken]
                    rng.shuffle(paths)
                    for p in paths:
                        blob = hist[p]["latest"]
                        data = read_blobs(gp, [blob]).get(blob)
                        if data is None or len(data) > args.max_bytes:
                            continue
                        v = hist[p]["blobs"][blob]
                        got.append((org, repo, p, blob, v[4], data))
                        break
                finally:
                    if recovery:
                        recovery.__exit__(None, None, None)
            per_repo_round += 1
        os.makedirs(os.path.join(args.out, e), exist_ok=True)
        for n, (org, repo, p, blob, date, data) in enumerate(got, 1):
            name = "%d__%s__%s__%s" % (n, safe_name(org), safe_name(repo),
                                       safe_name(p.replace("/", "__")))
            dest = os.path.join(args.out, e, name)
            with open(dest, "wb") as fh:
                fh.write(data)
            sig = signature(data)
            is_pickle = e == "pkl" or sig.startswith("pickle")
            extra = ""
            if is_pickle:
                with open(dest + ".inspect.txt", "w", encoding="utf-8",
                          errors="backslashreplace") as fh:
                    fh.write(inspect_text(data))
                _proto, cl = pickle_classes(data)
                extra = ", ".join(sorted(set(cl))[:6]) or "plain Python data"
            elif kind_of(data) == "text":
                extra = data[:200].decode("utf-8", "replace").splitlines()[0] \
                    if data.strip() else ""
            index.append([e, n, org, repo, p, blob, date, len(data),
                          kind_of(data), sig, os.path.join(e, name), extra])
        print("  %s: %d sample(s)  %.0fs" % (e, len(got), time.time() - t0),
              file=sys.stderr, flush=True)

    with open(os.path.join(args.out, "index.csv"), "w", newline="",
              encoding="utf-8", errors="backslashreplace") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "n", "org", "repo", "path", "blob", "last_date",
                    "bytes", "kind", "signature", "saved_as",
                    "classes_or_first_line"])
        w.writerows(index)
    md = ["# File samples", "", "Generated %s by sample_ext_files.py; up to "
          "%d file(s) per extension, newest version of each. Pickles: see the "
          ".inspect.txt next to each file (never unpickled)."
          % (datetime.date.today().isoformat(), args.per_ext), ""]
    for e in exts:
        rows = [r for r in index if r[0] == e]
        md += ["## .%s (%d sample%s, from %d repo(s) that have it)"
               % (e, len(rows), "" if len(rows) == 1 else "s",
                  len(repos_of.get(e, []))), "",
               "| # | repo | path | last change | bytes | kind | classes / "
               "first line |", "|---:|---|---|---|---:|---|---|"]
        for r in rows:
            md.append("| %d | %s/%s | `%s` | %s | %s | %s | %s |" % (
                r[1], r[2], r[3], r[4][:70], r[6][:10], f"{r[7]:,}",
                r[8] + (" (%s)" % r[9] if r[9] else ""),
                str(r[11]).replace("|", "/")[:90]))
        md.append("")
    with open(os.path.join(args.out, "index.md"), "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")
    print("%d sample(s) -> %s" % (len(index), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
