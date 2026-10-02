#!/usr/bin/env python3
"""
pkl_to_text.py - turn the extracted .pkl versions (group 3) into text a PII
scanner can read: one .txt per file path, every string any version held,
each line once. Pickles are NEVER unpickled - loading one can run code - they
are walked with pickletools, which only reads the opcodes.

Input: extract_versions.py --per-repo output (--in), whose per-repo
_state/<org>/<repo>/manifest.csv lists every version of every file and the
stored file holding it (files/pkl/<org>/<repo>/<blob>.pkl).

Per repo, per .pkl path, oldest version first (first_date):

  * the stored file is unpacked when compressed - gzip, bz2, xz, zlib (as
    joblib writes them); lz4 has no reader here: those get the raw scan only
    and are flagged
  * the pickle's opcodes are walked: every text string it holds (dict keys,
    DataFrame column names and object values, ...) is taken; byte blocks
    (numpy arrays: fixed-width text columns are stored there, often as
    UTF-32) are scanned for readable text
  * where the opcodes stop making sense (a numpy buffer written inline, a
    file that is not a pickle) the rest of the bytes get a raw strings scan,
    the way `strings` reads a binary - nothing readable is dropped
  * strings are split into lines, trimmed, blanks dropped, and a line
    already written for this path is not written again (as file_added_lines.py)

--active-root: the extraction leaves out versions identical to today's file
(already processed). A scanner cannot read a pickle, so today's .pkl was not
really scanned: with --active-root today's file at the same path is read
too and its strings are included (the manifest says so).

Output, under --out:
  <org>/<repo>/<path>.txt              the text, content only
  _state/<org>/<repo>/manifest.csv     per path: versions, how each was read,
                                       lines written, notes
  _state/<org>/<repo>/done.json        written last (resume)
  _logs/<name>.log, <name>_repos.csv
  summary.csv                          totals per way of reading, per repo

Usage:
    python3 pkl_to_text.py --in /data/workarea/group3/extract \\
        --out /data/workarea/group3/pkl_text --batch batches/S01.csv \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos
"""

import argparse
import bz2
import csv
import datetime
import gzip
import io
import json
import logging
import lzma
import os
import pickletools
import re
import shutil
import sys
import time
import zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from types import SimpleNamespace

from extension_versions import read_batch, read_json, read_rows
from file_delta import csv_text, write_atomic
from repo_extension_summary import ext_key

STATE, LOGS = "_state", "_logs"
STORED = ("written", "deduped")
STRING_OPS = {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE",
              "STRING", "BINSTRING", "SHORT_BINSTRING"}
BYTES_OPS = {"SHORT_BINBYTES", "BINBYTES", "BINBYTES8", "BYTEARRAY8"}
# readable runs: ASCII or UTF-8 characters, 4 or more
RAW_TEXT = re.compile(rb"(?:[\x20-\x7e]|[\xc2-\xf4][\x80-\xbf]{1,3}){4,}")
UTF32 = re.compile(rb"(?:[\x20-\x7e]\x00\x00\x00){4,}")    # numpy '<U' text
UTF16 = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
MANIFEST_HEADER = ["path", "versions", "versions_read", "active_included",
                   "read_as", "lines", "bytes_read", "note"]
log = logging.getLogger("pkl_to_text")


# --------------------------------------------------------------------------
# reading one pickle
# --------------------------------------------------------------------------

def unpack(data):
    """-> (bytes to walk, how it was read). Compressed pickles (joblib,
    pandas to_pickle with compression) are unpacked; the rest as is."""
    try:
        if data[:2] == b"\x1f\x8b":
            return gzip.decompress(data), "gzip"
        if data[:3] == b"BZh":
            return bz2.decompress(data), "bz2"
        if data[:6] == b"\xfd7zXZ\x00":
            return lzma.decompress(data), "xz"
        if data[:4] == b"\x04\x22\x4d\x18":
            return data, "lz4_raw_scan"              # no lz4 reader here
        if data[:2] == b"ZF":              # old joblib: ZF + padded length
            m = re.search(rb"\x78[\x01\x5e\x9c\xda]", data[:64])
            if m:
                return zlib.decompress(data[m.start():]), "joblib_zlib"
            return data, "unpack_failed_raw_scan"
        if data[:1] == b"\x78" and data[1:2] in (b"\x01", b"\x5e", b"\x9c",
                                                  b"\xda"):
            return zlib.decompress(data), "zlib"
    except (OSError, EOFError, zlib.error, lzma.LZMAError, ValueError):
        return data, "unpack_failed_raw_scan"
    return data, "pickle"


def scan_bytes(b):
    """Readable text in a block of bytes: ASCII/UTF-8 runs, plus UTF-32 and
    UTF-16 little-endian runs (numpy and Windows text)."""
    out = [m.decode("utf-8", "replace") for m in RAW_TEXT.findall(b)]
    out += [m.decode("utf-32-le", "replace") for m in UTF32.findall(b)]
    out += [m.decode("utf-16-le", "replace") for m in UTF16.findall(b)
            if not UTF32.fullmatch(m + b"\x00\x00")]
    return out


def pickle_text(data):
    """-> (strings, how): every string the pickle stream(s) hold, without
    unpickling. Several pickles one after another are all read; where the
    opcodes stop making sense the rest is raw-scanned."""
    out, pos, n, how = [], 0, len(data), "pickle"
    while pos < n:
        stream = io.BytesIO(data)
        stream.seek(pos)
        end, got, last = None, len(out), None
        try:
            for op, arg, at in pickletools.genops(stream):
                last = at
                if op.name in STRING_OPS and isinstance(arg, (str, bytes)):
                    out.append(arg if isinstance(arg, str)
                               else arg.decode("latin-1"))
                elif op.name in BYTES_OPS and isinstance(arg, (bytes, bytearray)):
                    out += scan_bytes(bytes(arg))
                elif op.name == "STOP":
                    end = at + 1
                    break
        except Exception:                            # noqa: BLE001 - odd bytes
            end = None
        if end is None:                              # not (or no longer) a pickle
            if pos == 0 and (last is None or last == 0):
                del out[got:]                        # not a pickle at all
                how = "not_pickle_raw_scan"
                out += scan_bytes(data)
            else:                                    # e.g. a numpy buffer inline
                how = "pickle+raw_scan"
                out += scan_bytes(data[last if last is not None else pos:])
            break
        pos = end
        while pos < n and data[pos:pos + 1] in (b"\x00", b"\n"):
            pos += 1                                 # padding between pickles
    return out, how


def text_of(data):
    """-> (strings, how) for one stored file."""
    if not data:
        return [], "empty"
    raw, how = unpack(data)
    if how.endswith("raw_scan"):
        return scan_bytes(raw), how
    strs, how2 = pickle_text(raw)
    if how == "pickle" or how2 == "pickle":
        return strs, how2 if how == "pickle" else how
    return strs, how + "+" + how2                    # e.g. gzip+pickle+raw_scan


def lines_of(strings):
    for s in strings:
        for line in s.splitlines():
            line = line.strip()
            if line:
                yield line


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def out_txt(out, org, repo, path):
    return os.path.join(out, org, repo, *path.split("/")) + ".txt"


def run_repo(cfg, org, repo):
    t0 = time.time()
    sd_in = os.path.join(cfg.src, STATE, org, repo)
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_extraction",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "paths": 0, "files_written": 0, "lines": 0,
           "read_as": {}, "error": ""}
    done_in = read_json(os.path.join(sd_in, "done.json"))
    if not done_in or done_in.get("status") != "ok":
        return rec                                   # nothing written: later
    by_path = {}
    for r in read_rows(os.path.join(sd_in, "manifest.csv")):
        if ext_key(r["path"]) == "pkl":
            by_path.setdefault(r["path"], []).append(r)
    shutil.rmtree(os.path.join(cfg.out, org, repo), ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)
    rows, read_as, status, error = [], Counter(), "ok", ""
    try:
        for path in sorted(by_path):
            vers = sorted(by_path[path], key=lambda r: (r["first_date"], r["blob"]))
            sources, notes = [], Counter()
            for r in vers:
                if r["action"] in STORED and r["stored_as"]:
                    sources.append(("version", os.path.join(cfg.src, r["stored_as"])))
                else:
                    notes[r["action"]] += 1
            active = ""
            if cfg.active_root:
                ap = os.path.join(cfg.active_root, org, repo, *path.split("/"))
                if os.path.isfile(ap):
                    sources.append(("active", ap))
                    active = "yes"
                else:
                    active = "no_file_today"
            seen, out_lines, nread, nbytes, how_all = set(), [], 0, 0, Counter()
            for kind, fp in sources:
                size = os.path.getsize(fp)
                if cfg.max_bytes and size > cfg.max_bytes:
                    notes["too_large"] += 1
                    continue
                with open(fp, "rb") as fh:
                    data = fh.read()
                strs, how = text_of(data)
                how_all[how] += 1
                read_as[how] += 1
                nread += 1
                nbytes += size
                for line in lines_of(strs):
                    if line not in seen:
                        seen.add(line)
                        out_lines.append(line)
            if out_lines:
                dest = out_txt(cfg.out, org, repo, path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8", errors="replace",
                          newline="\n") as fh:
                    fh.write("\n".join(out_lines) + "\n")
            rows.append([path, len(vers), nread, active,
                         ";".join("%s:%d" % kv for kv in sorted(how_all.items())),
                         len(out_lines), nbytes,
                         ";".join("%s:%d" % kv for kv in sorted(notes.items()))
                         + ("" if out_lines else (";" if notes else "") + "no_text")])
    except Exception as exc:                         # noqa: BLE001 - one repo
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"),
                 csv_text([MANIFEST_HEADER] + rows))
    rec.update(status=status, seconds=round(time.time() - t0, 1),
               paths=len(rows), files_written=sum(1 for r in rows if r[5]),
               lines=sum(r[5] for r in rows), read_as=dict(read_as), error=error)
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# summary and main
# --------------------------------------------------------------------------

def combine(out):
    state = os.path.join(out, STATE)
    tot, reads, repos = Counter(), Counter(), 0
    per_repo = []
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        for repo in sorted(os.listdir(os.path.join(state, org))):
            d = read_json(os.path.join(state, org, repo, "done.json"))
            if not d:
                continue
            repos += 1
            tot[d["status"]] += 1
            if d["status"] != "ok":
                continue
            reads.update(d["read_as"])
            for k in ("paths", "files_written", "lines"):
                tot[k] += d[k]
            per_repo.append([org, repo, d["paths"], d["files_written"], d["lines"],
                             ";".join("%s:%d" % kv for kv in sorted(d["read_as"].items()))])
    with open(os.path.join(out, "summary.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "pkl_paths", "txt_files_written", "lines",
                    "read_as"])
        w.writerows(per_repo)
        w.writerow([])
        w.writerow(["total", "", tot["paths"], tot["files_written"], tot["lines"],
                    ";".join("%s:%d" % kv for kv in sorted(reads.items()))])
        w.writerow([])
        w.writerow(["column", "meaning"])
        w.writerows([
            ("pkl_paths", "different .pkl paths in the repo's history"),
            ("txt_files_written", "paths that gave any text (one .txt each)"),
            ("lines", "lines written, each once per path"),
            ("read_as", "versions by how they were read: pickle = opcodes "
             "walked; +raw_scan = the rest of the bytes scanned like "
             "`strings`; gzip/bz2/xz/zlib/joblib_zlib = unpacked first; "
             "lz4_raw_scan / not_pickle_raw_scan / unpack_failed_raw_scan "
             "= bytes scanned only")])
    return repos, tot


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src", required=True,
                    help="extract_versions.py --per-repo output folder")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO")
    ap.add_argument("--active-root", help="include today's .pkl from here")
    ap.add_argument("--max-bytes", type=int, default=2_000_000_000,
                    help="skip stored files bigger than this (default 2 GB)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--redo", action="store_true",
                    help="redo repos already done")
    ap.add_argument("--combine-only", action="store_true")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    args = ap.parse_args()

    if args.combine_only:
        repos, tot = combine(args.out)
        print("combined %d repo(s): %s" % (repos, dict(tot)))
        return 0
    if os.path.realpath(args.out) == os.path.realpath(args.src):
        sys.exit("--out must be a different folder from --in")
    if not os.path.isdir(os.path.join(args.src, STATE)):
        sys.exit("no _state folder under " + args.src)
    chosen = []
    for b in args.batch:
        chosen += read_batch(b)
    chosen += [tuple(r.split("/", 1)) for r in args.repo]
    if not chosen:
        st = os.path.join(args.src, STATE)
        chosen = [(o, r) for o in sorted(os.listdir(st))
                  for r in sorted(os.listdir(os.path.join(st, o)))]
    todo = [k for k in dict.fromkeys(chosen)
            if args.redo or not read_json(os.path.join(args.out, STATE, k[0], k[1],
                                                       "done.json"))]
    name = args.name or (os.path.splitext(os.path.basename(args.batch[0]))[0]
                         if args.batch else "run")
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(args.out, LOGS, name + ".log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)

    cfg = SimpleNamespace(src=args.src, out=args.out, active_root=args.active_root,
                          max_bytes=args.max_bytes)
    log.info("START %s repos=%d workers=%d in=%s active=%s", name, len(todo),
             args.workers, args.src, args.active_root or "-")
    print("%d repo(s) to do" % len(todo), flush=True)
    t0, st = time.time(), Counter()
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf, \
            ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds", "paths",
                         "files_written", "lines", "error"])
        futs = {pool.submit(run_repo, cfg, o, r): (o, r) for o, r in todo}
        for fut in as_completed(futs):
            o, r = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:                 # noqa: BLE001
                st["failed"] += 1
                log.error("FAIL   %s/%s %s", o, r, exc)
                continue
            st[rec["status"]] += 1
            rw.writerow([rec["finished"], o, r, rec["status"], rec["seconds"],
                         rec["paths"], rec["files_written"], rec["lines"],
                         rec["error"]])
            rf.flush()
            if rec["status"] != "ok" and rec["status"] != "no_extraction":
                log.error("%s %s/%s %s", rec["status"].upper(), o, r, rec["error"])
    repos, tot = combine(args.out)
    msg = "END %s: %s | %s repo(s) combined: %s .txt, %s lines | %.0fs" % (
        name, dict(st), f"{repos:,}", f"{tot['files_written']:,}",
        f"{tot['lines']:,}", time.time() - t0)
    log.info(msg)
    print(msg)
    return 1 if st["error"] or st["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
