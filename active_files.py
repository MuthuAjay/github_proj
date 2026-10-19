#!/usr/bin/env python3
"""
active_files.py - today's files (the active copy) of the binary extensions
as real files, for a group whose active copy was NEVER scanned: the group 3
way ("today's file included"), for whole files.

extract_versions.py leaves out every version whose content is a file in the
same repo's active copy (it counts as processed). When the active copy was
not scanned, those contents still have to go out - and so do the active
files history does not have (active_only, no_match, lfs_active_real). This
script sends them, read from the active copy at the same path, the way
pkl_to_text.py --active-root reads today's .pkl.

Works from what extension_versions.py left (pass 1 AND pass 2 done, the
same --state extract_versions.py read):

  _state/<org>/<repo>/files.csv, identical.csv

and takes, per repo, every binary file of --extensions with at_head = yes
(in history or active_only). Each is read once from
<active-root>/<org>/<repo>/<path>, streamed to disk while its git blob id is
computed, and stored like extract_versions.py stores contents - once per
extension across all repos:

    files/<ext>/<first 2 of blob>/<blob>.<ext>

A content already in an --also-stored folder (the extract_versions.py output
of the same group: same layout) is not written again: the row says
in_stored and points there. So the two folders together hold every content
once, and can be copied into one place (same names = same bytes).

Manifest action per file:
  written       stored now
  deduped       already stored here by an earlier file (same content)
  in_stored     already in an --also-stored folder
  empty         0 bytes - nothing to scan, not written
  gone          in pass 1, not in the active copy any more
  unreadable    there, but could not be read (the error is kept)
and why (the file's state from pass 2): same_as_latest, same_as_older,
same_as_other_path (its content is a history version extract_versions.py
left out), no_match, lfs_active_real (history does not have it), active_only
(never in history), unchecked (pass 2 has no row for it).

A failing active-copy MOUNT is not recorded as a missing file: the repo gets
no done.json, no new repos start, and the run exits with code 3 - remount,
run the same command again. The same exit when --out's disk drops below
--min-free-disk-gb. Nothing is written to the active copy or to --state.

Output, under --out:
  files/<ext>/<ab>/<blob>.<ext>       the contents to send
  _state/<org>/<repo>/manifest.csv, done.json      per repo (resume)
  _logs/<name>.log, <name>_repos.csv
  manifest.csv, by_extension.csv, summary.md   (--combine-only rebuilds them)

Usage:
    python3 active_files.py --state /data/workarea/group4_7_9/group2_counts \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --out /data/workarea/group4_7_9/group2_active \\
        --also-stored /data/workarea/group4_7_9/group2_binary \\
        --batch /data/workarea/group4_7_9/batches/M41R.csv \\
        --extensions group4_7_9_group2_extensions.txt --workers 8
    python3 active_files.py --out /data/workarea/group4_7_9/group2_active --combine-only
"""

import argparse
import csv
import datetime
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

from extension_versions import read_batch, read_json, read_rows
from file_delta import (MOUNT_ERRNOS, MountDown, check_mount, csv_text,
                        repo_exists, write_atomic)
from repo_extension_summary import load_extensions

STATE, LOGS = "_state", "_logs"
CHUNK = 4 * 1024 * 1024
GB = 1e9
MANIFEST_HEADER = ["org", "repo", "path", "ext", "blob", "bytes", "action",
                   "stored_as", "why", "vendored"]
ACTIONS = ["written", "deduped", "in_stored", "empty", "gone", "unreadable"]
STORED = ("written", "deduped", "in_stored")
log = logging.getLogger("active_files")


class DiskLow(RuntimeError):
    pass


def store_path(ext, blob):
    return os.path.join("files", ext, blob[:2], "%s.%s" % (blob, ext))


def free_gb(path):
    return shutil.disk_usage(path).free / GB


def copy_hashed(src, tmp):
    """Stream src into tmp -> (git blob id, bytes). Mount errors raise
    MountDown; anything else is the caller's (unreadable / gone)."""
    size = os.stat(src).st_size
    h = hashlib.sha1(b"blob %d\0" % size)
    n = 0
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        for chunk in iter(lambda: fi.read(CHUNK), b""):
            h.update(chunk)
            fo.write(chunk)
            n += len(chunk)
    if n != size:                       # changed while being read
        raise OSError("size changed while reading (%d -> %d)" % (size, n))
    return h.hexdigest(), n


def todo_files(cfg, sd):
    """(files.csv row, why) of every file of --extensions at head."""
    ij = read_json(os.path.join(sd, "identical.json"))
    ident = ({r["path"]: r["result"] for r in
              read_rows(os.path.join(sd, "identical.csv"))}
             if ij and ij.get("status") == "ok" else {})
    out = []
    for f in read_rows(os.path.join(sd, "files.csv")):
        if f["ext"] not in cfg.exts or f["at_head"] != "yes":
            continue
        why = ("active_only" if f["in_history"] != "yes"
               else ident.get(f["path"], "unchecked"))
        out.append((f, why))
    return out


def run_repo(cfg, org, repo):
    """Today's files of one repo. MountDown / DiskLow leave it with no
    done.json (rerun carries on)."""
    t0 = time.time()
    sd_in = os.path.join(cfg.state, STATE, org, repo)
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_pass1",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "files": 0, "actions": {}, "bytes_written": 0,
           "error": ""}
    done1 = read_json(os.path.join(sd_in, "done.json"))
    if not done1 or done1.get("status") != "ok":
        return rec
    todo = todo_files(cfg, sd_in)
    rows, written = [], 0
    status, error = "ok", ""
    if todo and not repo_exists(cfg.active_root, org, repo):
        check_mount(cfg.active_root)
        todo = []                       # the folder went away since pass 1
        status, error = "error", "no folder in the active copy any more"
    tmp = os.path.join(cfg.out, "files", ".tmp.%d.%d"
                       % (os.getpid(), threading.get_ident()))
    try:
        for f, why in todo:
            if cfg.min_free_gb and free_gb(cfg.out) < cfg.min_free_gb:
                raise DiskLow("less than %s GB free on %s"
                              % (cfg.min_free_gb, cfg.out))
            p, e = f["path"], f["ext"]
            row = [org, repo, p, e, "", "", "", "", why, f["vendored"]]
            src = os.path.join(cfg.active_root, org, repo, *p.split("/"))
            try:
                blob, n = copy_hashed(src, tmp)
            except FileNotFoundError:
                check_mount(cfg.active_root)
                row[6] = "gone"
                rows.append(row)
                continue
            except OSError as exc:
                if exc.errno in MOUNT_ERRNOS:
                    raise MountDown("%s: %s" % (src, exc))
                row[6] = "unreadable"
                row[8] = "%s (%s: %s)" % (why, type(exc).__name__, exc)
                rows.append(row)
                continue
            row[4], row[5] = blob, n
            rel = store_path(e, blob)
            if n == 0:
                row[6] = "empty"
            elif any(os.path.exists(os.path.join(d, rel)) for d in cfg.also):
                row[6] = "in_stored"
                row[7] = next(os.path.join(d, rel) for d in cfg.also
                              if os.path.exists(os.path.join(d, rel)))
            elif os.path.exists(os.path.join(cfg.out, rel)):
                row[6], row[7] = "deduped", rel
            else:
                dest = os.path.join(cfg.out, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(tmp, dest)
                row[6], row[7] = "written", rel
                written += n
            rows.append(row)
    except (MountDown, DiskLow):
        raise
    except Exception as exc:                     # noqa: BLE001 - one repo
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
        rows = []
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"),
                 csv_text([MANIFEST_HEADER] + rows))
    rec.update(status=status, seconds=round(time.time() - t0, 1),
               files=len(rows), actions=dict(Counter(r[6] for r in rows)),
               bytes_written=written, error=error)
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


def combine(out):
    """manifest.csv, by_extension.csv and summary.md from every repo done,
    plus a count of the files actually under files/ as a check."""
    state = os.path.join(out, STATE)
    acc = defaultdict(Counter)
    stored = defaultdict(dict)                     # ext -> {stored_as: bytes}
    repos_of = defaultdict(set)
    repos = 0
    comb = os.path.join(out, "manifest.csv")
    with open(comb + ".tmp", "w", newline="", encoding="utf-8",
              errors="surrogateescape") as cf:
        w = csv.writer(cf)
        w.writerow(MANIFEST_HEADER)
        for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
            od = os.path.join(state, org)
            if not os.path.isdir(od):
                continue
            for repo in sorted(os.listdir(od)):
                sd = os.path.join(od, repo)
                done = read_json(os.path.join(sd, "done.json"))
                if not done or done.get("status") != "ok":
                    continue
                repos += 1
                for r in read_rows(os.path.join(sd, "manifest.csv")):
                    w.writerow([r[k] for k in MANIFEST_HEADER])
                    e, a = r["ext"], r["action"]
                    acc[e][a] += 1
                    acc[e]["files"] += 1
                    repos_of[e].add((org, repo))
                    if a in ("written", "deduped"):
                        stored[e][r["stored_as"]] = int(r["bytes"])
    os.replace(comb + ".tmp", comb)

    on_disk, disk_bytes = Counter(), Counter()
    root = os.path.join(out, "files")
    for e in os.listdir(root) if os.path.isdir(root) else []:
        for dirpath, _d, names in os.walk(os.path.join(root, e)):
            for n in names:
                on_disk[e] += 1
                disk_bytes[e] += os.path.getsize(os.path.join(dirpath, n))

    cols = ["repos", "files", "files_stored", "gb_stored", "files_on_disk",
            "gb_on_disk"] + ACTIONS
    table = []
    for e in sorted(acc):
        a = acc[e]
        table.append([e, len(repos_of[e]), a["files"], len(stored[e]),
                      round(sum(stored[e].values()) / GB, 3), on_disk[e],
                      round(disk_bytes[e] / GB, 3)] + [a[x] for x in ACTIONS])
    with open(os.path.join(out, "by_extension.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext"] + cols)
        w.writerows(table)

    def n(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    tot = [sum(r[i] for r in table) for i in range(1, len(cols) + 1)]
    md = ["# Today's files (active copy)", "",
          "Repositories: %s. Generated %s by active_files.py."
          % (f"{repos:,}", datetime.date.today().isoformat()), "",
          "| ext | " + " | ".join(cols) + " |",
          "|---|" + "---:|" * len(cols)]
    md += ["| %s | %s |" % (r[0], " | ".join(n(v) for v in r[1:]))
           for r in table]
    md.append("| **total** | %s |" % " | ".join(
        "" if c == "repos" else n(round(t, 3) if c.startswith("gb") else t)
        for c, t in zip(cols, tot)))
    bad = [r[0] for r in table if r[3] != r[5]]
    md += ["", "files_stored = distinct contents written here (in_stored "
           "ones are in the --also-stored folder); files_on_disk = what is "
           "under files/. "
           + ("They agree for every extension." if not bad else
              "**They differ for: %s** - a run still going, or files removed "
              "by hand." % ", ".join(bad))]
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    return repos, bad


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="extension_versions.py --out folder "
                                    "(pass 1 and pass 2 done)")
    ap.add_argument("--active-root", help="the active copy, <org>/<repo>/...")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--also-stored", action="append", default=[], metavar="DIR",
                    help="an extract_versions.py output (shared layout): "
                         "contents already there are not written (repeatable)")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV",
                    help="only the repos in this batch file (repeatable)")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="only this repo (repeatable)")
    ap.add_argument("--extensions", help="comma list or file (required)")
    ap.add_argument("--min-free-disk-gb", type=float, default=20,
                    help="stop below this much free space on --out "
                         "(default 20; 0 = no check)")
    ap.add_argument("--workers", type=int, default=8,
                    help="repos at once (default 8)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only the repos whose last attempt failed")
    ap.add_argument("--redo", action="store_true",
                    help="do the chosen repos again, even if done")
    ap.add_argument("--combine-only", action="store_true",
                    help="only rebuild manifest.csv / by_extension.csv / summary.md")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    args = ap.parse_args()

    if args.combine_only:
        repos, bad = combine(args.out)
        print("combined %s repo(s) -> %s%s"
              % (f"{repos:,}", os.path.join(args.out, "summary.md"),
                 "  (files on disk differ for: %s)" % ", ".join(bad)
                 if bad else ""))
        return 0
    if not args.state or not args.active_root or not args.extensions:
        sys.exit("--state, --active-root and --extensions are needed")
    if not os.path.isdir(os.path.join(args.state, STATE)):
        sys.exit("no _state folder under " + args.state)
    for d in [args.state] + args.also_stored:
        if os.path.realpath(d) == os.path.realpath(args.out):
            sys.exit("--out must be a different folder from %s" % d)
    for d in args.also_stored:
        if not os.path.isdir(os.path.join(d, "files")):
            sys.exit("no files/ folder under --also-stored " + d)
    try:
        check_mount(args.active_root)
    except MountDown as exc:
        print("STOPPED: %s - remount it and run the same command again" % exc,
              file=sys.stderr)
        return 3
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    os.makedirs(os.path.join(args.out, "files"), exist_ok=True)
    if args.min_free_disk_gb and free_gb(args.out) < args.min_free_disk_gb:
        print("STOPPED: less than %s GB free on %s"
              % (args.min_free_disk_gb, args.out), file=sys.stderr)
        return 3
    name = args.name or (os.path.splitext(os.path.basename(args.batch[0]))[0]
                         if args.batch else "run")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(args.out, LOGS, name + ".log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    eh = logging.StreamHandler(sys.stderr)
    eh.setLevel(logging.WARNING)
    log.addHandler(eh)

    chosen = []
    for b in args.batch:
        chosen += read_batch(b)
    chosen += [tuple(r.strip("/").split("/", 1)) for r in args.repo]
    if not chosen:
        st = os.path.join(args.state, STATE)
        chosen = [(o, r) for o in sorted(os.listdir(st))
                  if os.path.isdir(os.path.join(st, o))
                  for r in sorted(os.listdir(os.path.join(st, o)))]
    chosen = list(dict.fromkeys(chosen))
    prior = {}
    for k in chosen:
        rec = read_json(os.path.join(args.out, STATE, k[0], k[1], "done.json"))
        prior[k] = rec.get("status") if rec else None
    if args.redo:
        todo = chosen
    elif args.retry_failed:
        todo = [k for k in chosen if prior[k] not in (None, "ok")]
    else:
        todo = [k for k in chosen if prior[k] is None]
    print("active  %s repo(s) to process (%s done, %s failed earlier)"
          % (f"{len(todo):,}",
             f"{sum(1 for v in prior.values() if v == 'ok'):,}",
             f"{sum(1 for v in prior.values() if v not in (None, 'ok')):,}"),
          flush=True)

    class Cfg:
        pass
    cfg = Cfg()
    cfg.state, cfg.out, cfg.active_root = args.state, args.out, args.active_root
    cfg.also = args.also_stored
    cfg.exts = set(load_extensions(args.extensions))
    cfg.min_free_gb = args.min_free_disk_gb
    log.info("START %s repos=%d workers=%d exts=%d active=%s also=%s",
             name, len(todo), args.workers, len(cfg.exts), args.active_root,
             ",".join(cfg.also) or "-")

    t0 = time.time()
    statuses, actions = Counter(), Counter()
    stopped = ""
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds",
                         "files", "actions", "error"])
        pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
        queue, running, done = list(todo), {}, 0
        while queue or running:
            while queue and not stopped and len(running) < max(1, args.workers):
                k = queue.pop(0)
                running[pool.submit(run_repo, cfg, *k)] = k
            if not running:
                break
            fin, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in fin:
                k = running.pop(fut)
                try:
                    rec = fut.result()
                except (MountDown, DiskLow) as exc:
                    stopped = stopped or str(exc)
                    log.error("STOP %s/%s: %s", k[0], k[1], exc)
                    continue
                done += 1
                statuses[rec["status"]] += 1
                actions.update(rec["actions"])
                rw.writerow([rec["finished"], k[0], k[1], rec["status"],
                             rec["seconds"], rec["files"],
                             json.dumps(rec["actions"]), rec["error"]])
                rf.flush()
                lvl = logging.INFO if rec["status"] == "ok" else logging.WARNING
                log.log(lvl, "%d/%d %s/%s %s %ss files=%d %s %s", done,
                        len(todo), k[0], k[1], rec["status"], rec["seconds"],
                        rec["files"], json.dumps(rec["actions"]), rec["error"])
        pool.shutdown()

    repos, bad = combine(args.out)
    summary = "END %s repos=%d %s actions=%s in %.0fs" % (
        name, done, dict(statuses), dict(actions), time.time() - t0)
    log.info(summary)
    print(summary)
    if stopped:
        print("STOPPED: %s - fix it and run the same command again" % stopped,
              file=sys.stderr)
        return 3
    return 1 if any(s != "ok" for s in statuses) else 0


if __name__ == "__main__":
    sys.exit(main())
