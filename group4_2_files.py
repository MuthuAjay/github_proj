#!/usr/bin/env python3
"""
group4_2_files.py - the group 2 whole files (xlsx, docx, png, msg, pfx, ...)
found inside the group 4.2 / 4.2_R archives, as group 2 delivered its
binary files: each distinct content once, the file itself, not its text.

Input is an archive_unpack.py output folder (--unpacked): its per-repo
_state/<org>/<repo>/manifest.csv (one row per file inside per archive
version, in version order) and files/ (each distinct content once per repo).

Per repo, paths in manifest order, versions oldest first:

  * every distinct content of a path is written once, under the path it
    had: <org>/<repo>/<archive path>/<path inside>. A path with more than
    one content gets one file per content: <name>.v1.<type>, .v2, ...
    (v1 = the oldest)
  * a content already written in this repo under another path is not
    written again (same_as_other_path; the row says which file has it),
    like group 2 stored identical files once
  * no delta: today's archives were probably never scanned
  * empty contents (0 bytes) are not written (group 2 left them out too)
  * files are hard links to the unpacked copy when they are on the same
    disk (no extra space), else copies

Left out (listed in left_out.csv per repo, not written), as in
group4_2_text.py:
  docfx_cache   a path with obj/.cache/build/ (DocFX build cache)
  macos         __MACOSX/ folders, names starting "._", .DS_Store
  vendored      a vendored / generated folder in the path

Output, under --out:
  <org>/<repo>/<archive path>/<path inside>
  _state/<org>/<repo>/manifest.csv   one row per content per path: output,
      status, sha256, versions (of the path with this content), bytes,
      same_as, not_stored (versions of the path archive_unpack.py could
      not store; on the path's first row)
  _state/<org>/<repo>/left_out.csv, done.json (written last: resume)
  stats/by_type.csv per type from scope to output, a total row;
  stats/by_type_legend.csv, stats/summary.json, _logs/<name>.log
  --stats-only rebuilds stats/ from the finished repos (seconds);
  --stats-only --combine OUT1 OUT2 also sums both runs per type into
  <--out>/stats_combined/by_type.csv

Status per row: written, same_as_other_path, empty, not_stored (no
version of the path was stored), name_too_long, name_clash (another path
already has that output name; counted, not written).

Usage:
    python3 group4_2_files.py --unpacked /data/workarea/group4_2/unpacked \\
        --out /data/workarea/group4_2/group2_binary --workers 8
    --extensions LIST|FILE  types to take (default: the 18 group 2 binary
                            types, extension_versions.BINARY_EXTS);
                            _noext = files without an extension (group 5)
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

from extension_versions import BINARY_EXTS                      # noqa: E402
from file_added_lines import _csv_text, human, too_long, write_atomic  # noqa: E402
from group4_2_text import (STATE, LOGS, STORED, clean_path,     # noqa: E402
                           clear_repo, left_out_reason, list_repos, read_done)
from repo_extension_summary import ext_key, load_extensions     # noqa: E402

NOEXT = "_noext"     # --extensions name for files without an extension (archive_unpack.py's)

MANIFEST_HEADER = ["org", "repo", "path", "ext", "output", "status",
                   "sha256", "versions", "bytes", "same_as", "not_stored",
                   "first_date", "last_date"]
LEFT_OUT_HEADER = ["org", "repo", "path", "ext", "reason", "versions"]

log = logging.getLogger("group4_2_files")


def read_manifest(path, exts):
    """-> OrderedDict path -> [(action, stored_as, sha256, bytes, date)],
    rows of the wanted types only, in manifest (version) order."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    by_path = OrderedDict()
    with open(path, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            if not r.get("inner_path") or (r["ext"] or NOEXT) not in exts:
                continue
            stored = r["stored_as"]
            sha = r.get("blob") or os.path.splitext(os.path.basename(stored))[0]
            by_path.setdefault(r["path"], []).append(
                (r["action"], stored, sha, r.get("bytes") or "", r["first_date"]))
    return by_path


def versioned(parts, n):
    """['a', 'x.docx'], 2 -> ['a', 'x.v2.docx'] (no type: 'x' -> 'x.v2')."""
    stem, dot, ext = parts[-1].rpartition(".")
    leaf = "%s.v%d.%s" % (stem, n, ext) if dot and stem else \
        "%s.v%d" % (parts[-1], n)
    return parts[:-1] + [leaf]


def place(src, dst):
    """Hard link src at dst; copy when a link is not possible."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def process_repo(cfg, org, repo):
    """-> (manifest rows, left_out rows)."""
    src = os.path.join(cfg.unpacked, STATE, org, repo, "manifest.csv")
    by_path = read_manifest(src, cfg.exts)
    out_root = os.path.join(cfg.out, org, repo)
    seen = {}                                   # sha256 -> output written
    rows, left = [], []
    for path, versions in by_path.items():
        e = ext_key(path) or NOEXT
        why = left_out_reason(path)
        if why:
            left.append([org, repo, path, e, why, len(versions)])
            continue
        # distinct contents of this path, oldest first
        contents, not_stored = OrderedDict(), 0
        for action, stored, sha, size, date in versions:
            if action not in STORED or not stored:
                not_stored += 1
                continue
            c = contents.setdefault(sha, {"stored": stored, "versions": 0,
                                          "dates": []})
            c["versions"] += 1
            if date:
                c["dates"].append(date)
        dates = [v[4] for v in versions if v[4]]
        if not contents:
            rows.append([org, repo, path, e, "", "not_stored", "",
                         0, 0, "", not_stored,
                         dates[0] if dates else "", dates[-1] if dates else ""])
            continue
        parts = clean_path(path)
        new = [s for s in contents if s not in seen]
        n = 0
        for i, (sha, c) in enumerate(contents.items()):
            fp = os.path.join(cfg.unpacked, c["stored"])
            size = os.path.getsize(fp) if os.path.isfile(fp) else 0
            output, same_as = "", ""
            if sha in seen:
                status, same_as = "same_as_other_path", seen[sha]
            elif not os.path.isfile(fp):
                status = "not_stored"
            elif size == 0:
                status = "empty"
            else:
                n += 1
                rel = versioned(parts, n) if len(new) > 1 else parts
                out = os.path.join(out_root, *rel)
                if too_long(out, rel):
                    status = "name_too_long"
                elif os.path.lexists(out):
                    status = "name_clash"
                else:
                    place(fp, out)
                    status = "written"
                    output = os.path.relpath(out, cfg.out)
                    seen[sha] = output
            rows.append([org, repo, path, e, output, status, sha,
                         c["versions"], size if output else 0, same_as,
                         not_stored if i == 0 else 0,
                         c["dates"][0] if c["dates"] else "",
                         c["dates"][-1] if c["dates"] else ""])
    return rows, left


def run_repo(cfg, org, repo):
    """Worker: clear, write, then manifest + left_out, done.json last."""
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
           "paths": len({r[2] for r in rows}),
           "files_written": sum(1 for r in rows if r[4]),
           "bytes": sum(r[8] for r in rows),
           "file_status": dict(Counter(r[5] for r in rows)),
           "left_out": dict(Counter(r[4] for r in left)),
           "error": error}
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# per type, from scope to output: files in every archive version -> paths
# -> left out -> contents -> files written (+ why the rest were not)
BY_TYPE_COLS = [
    ("scope_files", "files of the type inside the archives, every version"),
    ("scope_paths", "the same file across versions counted once: repo + "
                    "archive path + path inside"),
    ("left_vendored", "paths left out: a vendored folder in the path"),
    ("left_macos", "paths left out: macOS metadata"),
    ("left_docfx_cache", "paths left out: DocFX build cache"),
    ("left_versions", "versions of the left-out paths"),
    ("paths", "paths kept (scope_paths - left out)"),
    ("versions", "versions of the kept paths (stored or not)"),
    ("contents", "different contents per path (one manifest row each)"),
    ("files_written", "files written (status written)"),
    ("st_same_as_other_path", "not written: same content already written "
                              "in this repo under another path"),
    ("st_empty", "not written: 0 bytes"),
    ("st_not_stored", "not written: no version stored by archive_unpack.py"),
    ("st_name_too_long", "not written: name too long for the disk"),
    ("st_name_clash", "not written: another path of the repo got the same "
                      "output name"),
    ("not_stored_versions", "versions archive_unpack.py could not store"),
    ("bytes", "size of the files written"),
]


def stats(out):
    """stats/by_type.csv (+ legend) and stats/summary.json over every done
    repo."""
    st = os.path.join(out, STATE)
    by_type, status, left, repos = {}, Counter(), Counter(), Counter()
    for org in sorted(os.listdir(st)) if os.path.isdir(st) else []:
        for repo in sorted(os.listdir(os.path.join(st, org))):
            d = read_done(out, org, repo)
            if not d:
                continue
            repos[d["status"]] += 1
            sd = os.path.join(st, org, repo)
            paths = set()
            with open(os.path.join(sd, "manifest.csv"), newline="",
                      encoding="utf-8", errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    t = by_type.setdefault(r["ext"], Counter())
                    if r["path"] not in paths:
                        paths.add(r["path"])
                        t["paths"] += 1
                    t["versions"] += int(r["versions"]) + int(r["not_stored"])
                    t["contents"] += r["status"] != "not_stored"
                    t["files_written"] += bool(r["output"])
                    t["not_stored_versions"] += int(r["not_stored"])
                    t["bytes"] += int(r["bytes"])
                    if r["status"] != "written":
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
        c["scope_paths"] = c["paths"] + c["left_vendored"] + \
            c["left_macos"] + c["left_docfx_cache"]
        c["scope_files"] = c["versions"] + c["left_versions"]
        tot.update(c)
    write_by_type(os.path.join(out, "stats"), by_type, tot)
    summary = {"repos": dict(repos), "scope_files": tot["scope_files"],
               "scope_paths": tot["scope_paths"], "left_out_paths": dict(left),
               "left_out_versions": tot["left_versions"],
               "paths": tot["paths"], "contents": tot["contents"],
               "files_written": tot["files_written"], "bytes": tot["bytes"],
               "status": dict(status)}
    write_atomic(os.path.join(out, "stats", "summary.json"),
                 json.dumps(summary, indent=1))
    return summary


def write_by_type(sd, by_type, tot, extra=()):
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
    """Several runs' stats/by_type.csv -> one by_type.csv in `to`."""
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
    for h in (logging.FileHandler(os.path.join(out, LOGS, name + ".log"),
                                  encoding="utf-8"),
              logging.StreamHandler(sys.stderr)):
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                         "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--unpacked", required=True,
                    help="archive_unpack.py --out folder")
    ap.add_argument("--out", required=True)
    ap.add_argument("--extensions", default=",".join(BINARY_EXTS),
                    help="types to take (default: group 2 binary)")
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
                       "files_written": 0, "bytes": 0}
            failed += rec["status"] != "ok"
            log.info("[%d/%d] %s/%s %s %.0fs files %d %s%s", i, len(todo), o,
                     r, rec["status"], rec["seconds"], rec["files_written"],
                     human(rec["bytes"]),
                     (" - " + rec["error"]) if rec.get("error") else "")
    s = stats(args.out)
    log.info("done in %.0f min: repos %s, scope files %d -> paths %d, "
             "left out %s -> kept %d -> contents %d -> files written %d, %s; "
             "status %s", (time.time() - t0) / 60, s["repos"],
             s["scope_files"], s["scope_paths"], s["left_out_paths"],
             s["paths"], s["contents"], s["files_written"],
             human(s["bytes"]), s["status"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
