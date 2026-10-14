#!/usr/bin/env python3
"""
group4_2_text.py - the group 1 (text) files found inside the group 4.2 /
4.2_R archives, as group 1 delivered its own files: one plain .txt per file
path, holding every line that path ever had across all versions of its
archive, once each.

Input is an archive_unpack.py output folder (--unpacked): its per-repo
_state/<org>/<repo>/manifest.csv (one row per file inside per archive
version, in version order) and files/ (each distinct content once per repo).

Per file path inside an archive (<archive path>/<path inside>):

  * its versions are read oldest first (the order of the manifest); a
    content already read for this path is not read again
  * each line is stripped of spaces/tabs at both ends, blank lines are
    dropped, and a line already written for this path is not written again
  * no delta: today's archives were probably never scanned, so today's
    version is merged in like any other
  * binary versions (a NUL byte in the first 8 KB, not UTF-16) give no
    lines; UTF-16 text (with a BOM) is decoded; other text is UTF-8, else
    cp1252 per line

Left out (listed in left_out.csv per repo, not merged):
  docfx_cache   a path with obj/.cache/build/ (DocFX build cache)
  macos         __MACOSX/ folders, names starting "._", .DS_Store
  vendored      a vendored / generated folder in the path (node_modules,
                packages, vendor, bin, obj, dist, build, target, ...)

Output, under --out:
  <org>/<repo>/<archive path>/<path inside>.txt
  _state/<org>/<repo>/manifest.csv   one row per path: output, status,
      versions, contents (distinct), lines_added, lines_kept, bytes,
      not_stored (versions archive_unpack.py could not store)
  _state/<org>/<repo>/left_out.csv, done.json (written last: resume)
  stats/by_type.csv per type from scope to output: files in every archive
      version -> paths -> left out -> merged -> .txt written (and why the
      rest got none) -> lines, bytes; a total row; by_type_legend.csv
  stats/summary.json, _logs/<name>.log
  --stats-only rebuilds stats/ from the finished repos (seconds);
  --stats-only --combine OUT1 OUT2 also sums both runs per type into
  <--out>/stats_combined/by_type.csv

Status per path: ok, partly_binary, binary, no_text (only blank lines),
not_stored (no version was stored), name_too_long (counted, not written).

Usage:
    python3 group4_2_text.py --unpacked /data/workarea/group4_2/unpacked \\
        --out /data/workarea/group4_2/group1_text --workers 8
    --repo ORG/REPO (repeatable) only these; --limit N the first N repos
    --redo  do repos again that are already done
"""

import argparse
import csv
import datetime
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from explain_file_counts import vendor_of                       # noqa: E402
from file_added_lines import (FileLines, _csv_text, decode,      # noqa: E402
                              human, too_long, write_atomic)
from repo_extension_summary import ext_key, load_extensions     # noqa: E402

STATE = "_state"
LOGS = "_logs"
STORED = ("written", "deduped")
SNIFF = 8192
MANIFEST_HEADER = ["org", "repo", "path", "ext", "output", "status",
                   "versions", "contents", "lines_added", "lines_kept",
                   "bytes", "not_stored", "first_date", "last_date"]
LEFT_OUT_HEADER = ["org", "repo", "path", "ext", "reason", "versions"]

log = logging.getLogger("group4_2_text")


def left_out_reason(path):
    """docfx_cache / macos / vendored, or '' to merge."""
    low = path.lower()
    leaf = low.rsplit("/", 1)[-1]
    if "obj/.cache/build/" in low:
        return "docfx_cache"
    if "__macosx/" in ("/" + low) or leaf.startswith("._") or leaf == ".ds_store":
        return "macos"
    if vendor_of(path):
        return "vendored"
    return ""


def rows_by_path(manifest, org, repo, ext, inner):
    """For the group 3 scripts (pdf_page_merge.py, pkl_to_text.py): a
    manifest's rows of type `ext` -> ({path: [rows]}, left_out rows). inner
    (an archive_unpack.py manifest): files inside an archive only, and the
    left-out paths apart with their reason and versions."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    by_path, left = {}, {}
    with open(manifest, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            if ext_key(r["path"]) != ext:
                continue
            if inner:
                if not r.get("inner_path"):
                    continue
                why = left_out_reason(r["path"])
                if why:
                    left.setdefault(r["path"],
                                    [org, repo, r["path"], ext, why, 0])[5] += 1
                    continue
            by_path.setdefault(r["path"], []).append(r)
    return by_path, list(left.values())


def clean_path(path):
    """A path from inside an archive -> safe relative parts ('..' and empty
    parts can not climb out of the output folder)."""
    parts = []
    for p in path.replace("\\", "/").split("/"):
        if p in ("", "."):
            continue
        parts.append("_.." if p == ".." else p)
    return parts


def file_lines(fp):
    """-> iterator of decoded lines, or None when the content is binary."""
    with open(fp, "rb") as fh:
        head = fh.read(SNIFF)
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return _utf16_lines(fp)
    if b"\0" in head:
        return None
    return _byte_lines(fp)


def _utf16_lines(fp):
    with open(fp, encoding="utf-16", errors="replace") as fh:
        yield from fh


def _byte_lines(fp):
    with open(fp, "rb") as fh:
        for raw in fh:
            yield decode(raw)


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def read_manifest(path, exts):
    """-> OrderedDict path -> [rows], rows of the wanted types only, in
    manifest (version) order."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    by_path = OrderedDict()
    with open(path, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            if not r.get("inner_path") or r["ext"] not in exts:
                continue
            by_path.setdefault(r["path"], []).append(
                (r["action"], r["stored_as"], r["first_date"]))
    return by_path


def process_repo(cfg, org, repo):
    """-> (manifest rows, left_out rows)."""
    src = os.path.join(cfg.unpacked, STATE, org, repo, "manifest.csv")
    by_path = read_manifest(src, cfg.exts)
    out_root = os.path.join(cfg.out, org, repo)
    rows, left = [], []
    for path, versions in by_path.items():
        e = ext_key(path)
        why = left_out_reason(path)
        if why:
            left.append([org, repo, path, e, why, len(versions)])
            continue
        parts = clean_path(path)
        out = os.path.join(out_root, *parts) + ".txt"
        f = FileLines(out, too_long(out, parts[:-1] + [parts[-1] + ".txt"]))
        read, binary, not_stored = set(), 0, 0
        for action, stored, _ in versions:
            f.versions += 1
            if action not in STORED or not stored:
                not_stored += 1
                continue
            if stored in read:
                continue
            read.add(stored)
            lines = file_lines(os.path.join(cfg.unpacked, stored))
            if lines is None:
                binary += 1
                continue
            for line in lines:
                f.add(line)
            f.flush()
        f.flush()
        if not read:
            status = "not_stored"
        elif f.too_long and f.kept:
            status = "name_too_long"
        elif f.kept:
            status = "partly_binary" if binary else "ok"
        elif binary:
            status = "binary"
        else:
            status = "no_text"
        dates = [d for _, _, d in versions if d]
        rows.append([org, repo, path, e,
                     os.path.relpath(out, cfg.out) if f.started else "",
                     status, f.versions, len(read), f.added, f.kept, f.bytes,
                     not_stored, dates[0] if dates else "",
                     dates[-1] if dates else ""])
    return rows, left


def clear_repo(out, org, repo):
    root = os.path.realpath(out)
    for d in (os.path.join(out, org, repo), os.path.join(out, STATE, org, repo)):
        real = os.path.realpath(d)
        if real.startswith(root + os.sep) and real != root:
            shutil.rmtree(d, ignore_errors=True)


def run_repo(cfg, org, repo):
    """Worker: clear, merge, then manifest + left_out, done.json last."""
    t0 = time.time()
    clear_repo(cfg.out, org, repo)
    try:
        rows, left = process_repo(cfg, org, repo)
        status, error = "ok", ""
    except Exception as exc:                  # noqa: BLE001 - one repo only
        clear_repo(cfg.out, org, repo)
        rows, left = [], []
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
    sd = os.path.join(cfg.out, STATE, org, repo)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"),
                 _csv_text([MANIFEST_HEADER] + rows))
    write_atomic(os.path.join(sd, "left_out.csv"),
                 _csv_text([LEFT_OUT_HEADER] + left))
    rec = {"org": org, "repo": repo, "status": status, "name": cfg.name,
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - t0, 1),
           "paths": len(rows), "files_written": sum(1 for r in rows if r[4]),
           "versions": sum(r[6] for r in rows),
           "lines_added": sum(r[8] for r in rows),
           "lines_kept": sum(r[9] for r in rows),
           "bytes": sum(r[10] for r in rows),
           "file_status": dict(Counter(r[5] for r in rows)),
           "left_out": dict(Counter(r[4] for r in left)),
           "error": error}
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def list_repos(unpacked):
    """Repos with a finished archive_unpack.py manifest, biggest first."""
    st = os.path.join(unpacked, STATE)
    out = []
    for org in sorted(os.listdir(st)) if os.path.isdir(st) else []:
        for repo in sorted(os.listdir(os.path.join(st, org))):
            m = os.path.join(st, org, repo, "manifest.csv")
            if os.path.isfile(os.path.join(st, org, repo, "done.json")) \
                    and os.path.isfile(m):
                out.append((os.path.getsize(m), org, repo))
    return [(o, r) for _, o, r in sorted(out, key=lambda x: -x[0])]


def read_done(out, org, repo):
    try:
        with open(os.path.join(out, STATE, org, repo, "done.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# per type, from scope to output: files in every archive version -> paths
# -> left out -> merged -> .txt written (+ why the rest got none) -> lines
BY_TYPE_COLS = [
    ("scope_files", "files of the type inside the archives, every version"),
    ("scope_paths", "the same file across versions counted once: repo + "
                    "archive path + path inside"),
    ("left_vendored", "paths left out: a vendored folder in the path"),
    ("left_macos", "paths left out: macOS metadata"),
    ("left_docfx_cache", "paths left out: DocFX build cache"),
    ("left_versions", "versions of the left-out paths"),
    ("merged_paths", "paths merged (scope_paths - left out)"),
    ("merged_versions", "versions of the merged paths read"),
    ("contents", "different contents read (per path)"),
    ("files_written", ".txt written (status ok + partly_binary)"),
    ("st_ok", "written; every version text"),
    ("st_no_text", "no .txt: only empty / blank lines"),
    ("st_binary", "no .txt: every version binary"),
    ("st_not_stored", "no .txt: no version stored by archive_unpack.py"),
    ("st_name_too_long", "no .txt: name too long for the disk"),
    ("st_partly_binary", "written; some versions binary"),
    ("not_stored_versions", "versions archive_unpack.py could not store"),
    ("lines_added", "lines in all versions read (blank lines not counted)"),
    ("lines_kept", "lines written, each once per path"),
    ("bytes", "size of the .txt written"),
]


def stats(out):
    """stats/by_type.csv (+ by_type_legend.csv) and stats/summary.json over
    every done repo."""
    st = os.path.join(out, STATE)
    by_type, status, left, repos = {}, Counter(), Counter(), Counter()
    for org in sorted(os.listdir(st)) if os.path.isdir(st) else []:
        for repo in sorted(os.listdir(os.path.join(st, org))):
            d = read_done(out, org, repo)
            if not d:
                continue
            repos[d["status"]] += 1
            sd = os.path.join(st, org, repo)
            with open(os.path.join(sd, "manifest.csv"), newline="",
                      encoding="utf-8", errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    t = by_type.setdefault(r["ext"], Counter())
                    t["merged_paths"] += 1
                    t["merged_versions"] += int(r["versions"])
                    t["files_written"] += bool(r["output"])
                    t["not_stored_versions"] += int(r["not_stored"])
                    for k in ("contents", "lines_added", "lines_kept", "bytes"):
                        t[k] += int(r[k])
                    t["st_" + r["status"]] += 1
                    status[r["status"]] += 1
            with open(os.path.join(sd, "left_out.csv"), newline="",
                      encoding="utf-8", errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    t = by_type.setdefault(r["ext"], Counter())
                    t["left_" + r["reason"]] += 1
                    t["left_versions"] += int(r["versions"])
                    left[r["reason"]] += 1
    tot = Counter()
    for c in by_type.values():
        c["scope_paths"] = c["merged_paths"] + c["left_vendored"] + \
            c["left_macos"] + c["left_docfx_cache"]
        c["scope_files"] = c["merged_versions"] + c["left_versions"]
        tot.update(c)
    write_by_type(os.path.join(out, "stats"), by_type, tot)
    summary = {"repos": dict(repos), "scope_files": tot["scope_files"],
               "scope_paths": tot["scope_paths"],
               "left_out_paths": dict(left),
               "left_out_versions": tot["left_versions"],
               "paths": tot["merged_paths"],
               "versions": tot["merged_versions"],
               "contents": tot["contents"],
               "files_written": tot["files_written"],
               "lines_added": tot["lines_added"],
               "lines_kept": tot["lines_kept"], "bytes": tot["bytes"],
               "status": dict(status), "left_out": dict(left)}
    write_atomic(os.path.join(out, "stats", "summary.json"),
                 json.dumps(summary, indent=1))
    return summary


def write_by_type(sd, by_type, tot, extra=()):
    """by_type.csv: a row per type (most scope files first), a total row;
    by_type_legend.csv beside it. extra: more (column, text) at the end."""
    os.makedirs(sd, exist_ok=True)
    cols = [c for c, _ in BY_TYPE_COLS] + [c for c, _ in extra]
    name = {c: c.replace("st_", "status_") for c in cols}
    with open(os.path.join(sd, "by_type.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["type"] + [name[c] for c in cols] + ["gb"])
        rows = sorted(by_type.items(), key=lambda x: (-x[1]["scope_files"], x[0]))
        for t, c in rows + [("total", tot)]:
            w.writerow([t] + [c.get(k, 0) for k in cols]
                       + [round(c.get("bytes", 0) / 1e9, 3)])
    with open(os.path.join(sd, "by_type_legend.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["column", "meaning"])
        w.writerow(["type", "file type (extension) inside the archives"])
        for c, text in list(BY_TYPE_COLS) + list(extra):
            w.writerow([name[c], text])
        w.writerow(["gb", "bytes in GB"])


def combine(outs, to):
    """Several runs' stats/by_type.csv -> one by_type.csv in `to`: every
    column summed, plus scope_files and files_written per run."""
    by_type, tot, extra = {}, Counter(), []
    for o in outs:
        run = os.path.basename(os.path.dirname(os.path.abspath(o))) or o
        extra += [(run + "_scope_files", "scope_files in " + o),
                  (run + "_files_written", "files_written in " + o)]
        with open(os.path.join(o, "stats", "by_type.csv"), newline="",
                  encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["type"] == "total":
                    continue
                c = by_type.setdefault(r["type"], Counter())
                for k, _ in BY_TYPE_COLS:
                    c[k] += int(r[k.replace("st_", "status_")])
                c[run + "_scope_files"] += int(r["scope_files"])
                c[run + "_files_written"] += int(r["files_written"])
    for c in by_type.values():
        tot.update(c)
    write_by_type(to, by_type, tot, extra)
    return tot


def setup_logging(out, name):
    os.makedirs(os.path.join(out, LOGS), exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for h, lvl in ((logging.FileHandler(os.path.join(out, LOGS, name + ".log"),
                                        encoding="utf-8"), logging.INFO),
                   (logging.StreamHandler(sys.stderr), logging.INFO)):
        h.setLevel(lvl)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                         "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--unpacked", required=True,
                    help="archive_unpack.py --out folder")
    ap.add_argument("--out", required=True)
    ap.add_argument("--extensions", default=None,
                    help="types to merge (default: the group 1 list)")
    ap.add_argument("--repo", action="append", default=[],
                    help="ORG/REPO, repeatable: only these")
    ap.add_argument("--limit", type=int, default=0, help="first N repos only")
    ap.add_argument("--redo", action="store_true",
                    help="do repos again that are already done")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--name", default="run", help="log name")
    ap.add_argument("--stats-only", action="store_true",
                    help="only rebuild stats/ from the finished repos")
    ap.add_argument("--combine", nargs="+", metavar="OUT", default=None,
                    help="with --stats-only: also sum these runs' "
                         "stats/by_type.csv into --out/stats_combined/")
    args = ap.parse_args()

    if os.path.realpath(args.out).startswith(
            os.path.realpath(os.path.join(args.unpacked, "files")) + os.sep):
        sys.exit("--out must not be inside the unpacked files folder")
    os.makedirs(args.out, exist_ok=True)
    setup_logging(args.out, args.name)
    if args.stats_only:
        print(json.dumps(stats(args.out), indent=1))
        if args.combine:
            to = os.path.join(args.out, "stats_combined")
            combine(args.combine, to)
            print("combined by type -> " + os.path.join(to, "by_type.csv"))
        return 0

    cfg = argparse.Namespace(unpacked=args.unpacked, out=args.out,
                             exts=set(load_extensions(args.extensions)),
                             name=args.name)
    repos = list_repos(args.unpacked)
    if args.repo:
        want = {tuple(x.split("/", 1)) for x in args.repo}
        repos = [r for r in repos if r in want]
        missing = want - set(repos)
        if missing:
            log.warning("not in %s (or not done there): %s", args.unpacked,
                        ", ".join("/".join(m) for m in sorted(missing)))
    if args.limit:
        repos = repos[:args.limit]
    todo = [r for r in repos if args.redo or not (
        (read_done(args.out, *r) or {}).get("status") == "ok")]
    log.info("start: %s -> %s, %d types, %d repos (%d to do), %d workers",
             args.unpacked, args.out, len(cfg.exts), len(repos), len(todo),
             args.workers)
    t0, failed = time.time(), 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_repo, cfg, o, r): (o, r) for o, r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            o, r = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:          # noqa: BLE001
                rec = {"status": "error", "error": str(exc), "seconds": 0,
                       "files_written": 0, "lines_kept": 0, "bytes": 0}
            failed += rec["status"] != "ok"
            log.info("[%d/%d] %s/%s %s %.0fs files %d lines %d %s%s", i,
                     len(todo), o, r, rec["status"], rec["seconds"],
                     rec["files_written"], rec["lines_kept"],
                     human(rec["bytes"]),
                     (" - " + rec["error"]) if rec.get("error") else "")
    s = stats(args.out)
    log.info("done in %.0f min: repos %s, scope files %d -> paths %d, "
             "left out %s -> merged %d -> files written %d, "
             "lines kept %d of %d, %s; status %s",
             (time.time() - t0) / 60, s["repos"], s["scope_files"],
             s["scope_paths"], s["left_out"], s["paths"],
             s["files_written"], s["lines_kept"], s["lines_added"],
             human(s["bytes"]), s["status"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
