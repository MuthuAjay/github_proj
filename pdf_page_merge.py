#!/usr/bin/env python3
"""
pdf_page_merge.py - group 3 PDFs: per repo, per file path, one merged PDF
holding every distinct page any version of the file ever had, once.

Input: extract_versions.py --per-repo output (--in): each repo's
_state/<org>/<repo>/manifest.csv lists every version of every file and the
stored file holding it (files/pdf/<org>/<repo>/<blob>.pdf). Identical
versions are already stored once there (file-level dedup within the repo).

Per repo, per .pdf path, oldest version first (first_date):

  1. every page of every version is RENDERED to an image (pdfium, the
     engine Chrome uses) and the image is hashed (sha256 of the pixels and
     size). Two pages that look exactly alike have the same hash, even when
     the file bytes differ (re-saved, new metadata, pages reordered)
  2. a page whose hash was already seen for this path is a duplicate and is
     dropped; the first occurrence (oldest version) is kept. Exact matches
     only - never "looks similar": two copies of a form that differ by one
     name are two pages
  3. the kept pages are merged into ONE PDF by copying the ORIGINAL pages
     (pypdf), not the images: text, fonts and layout stay, so a scanner
     that reads PDF text can still read it. A path with one version whose
     pages are all kept is copied as it is
  4. each kept page is checked for text: no text at all (or fewer than
     --min-text-chars characters, not counting spaces) = "ocr_needed" - a
     scan or an image a scanner without OCR cannot read. Listed per page
     and counted, never dropped

A version that cannot be opened (needs a password, damaged) cannot be split:
it is copied whole next to the merged file as <path>.<blob 12>.pdf and
listed (action unreadable). PDFs that are only restricted (no printing...)
open normally.

--active-root + --drop-active-pages: also render today's file at the same
path and drop the pages identical to one of its pages (already processed,
like the text delta). Off by default: a page of today's PDF that is a scan
was not readable by the scanner, so dropping it is a decision to take.

Output, under --out:
  <org>/<repo>/<path>                  the merged PDF (same name)
  <org>/<repo>/<path>.<blob12>.pdf     an unreadable version, whole
  _state/<org>/<repo>/manifest.csv     per path: versions, pages seen, kept,
                                       duplicate, dropped as today's,
                                       ocr_needed, output files
  _state/<org>/<repo>/pages.csv        per page of every version: hash,
                                       chars of text, kept / duplicate /
                                       in_today, its page in the merged file
  _state/<org>/<repo>/done.json        written last (resume)
  _logs/<name>.log, <name>_repos.csv
  settings.json                        render settings (a resumed run must
                                       use the same, or hashes would differ)
  summary.csv                          per repo and totals, with a legend

Usage:
    python3 pdf_page_merge.py --in /data/workarea/binary_versions_g3 \\
        --out /data/workarea/pdf_merged_g3 --batch batches/S01.csv --workers 8
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
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from types import SimpleNamespace

from extension_versions import read_batch, read_json, read_rows
from file_delta import csv_text, write_atomic
from repo_extension_summary import ext_key

STATE, LOGS = "_state", "_logs"
STORED = ("written", "deduped")
MANIFEST_HEADER = ["path", "versions", "versions_read", "versions_unreadable",
                   "pages_seen", "pages_kept", "pages_duplicate",
                   "pages_in_today", "pages_ocr_needed", "output", "note"]
PAGES_HEADER = ["path", "source", "blob", "first_date", "page", "hash",
                "width", "height", "text_chars", "status", "merged_page"]
log = logging.getLogger("pdf_page_merge")


# --------------------------------------------------------------------------
# one PDF
# --------------------------------------------------------------------------

def page_prints(fp, cfg):
    """-> [(hash, width, height, text_chars)] per page, or raises when the
    file cannot be opened (password, damaged). Rendering is the same for
    every page of every run: --scale, capped so the longer side is at most
    --max-px pixels."""
    import pypdfium2 as pdfium                     # imported in the worker
    doc = pdfium.PdfDocument(fp)
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                w, h = page.get_size()
                scale = min(cfg.scale, cfg.max_px / max(w, h, 1))
                bm = page.render(scale=scale)
                try:
                    digest = hashlib.sha256(b"%dx%d:" % (bm.width, bm.height)
                                            + bytes(bm.buffer)).hexdigest()
                    size = (bm.width, bm.height)
                finally:
                    bm.close()
                tp = page.get_textpage()
                try:
                    chars = len("".join(tp.get_text_range().split()))
                finally:
                    tp.close()
            finally:
                page.close()
            out.append((digest, size[0], size[1], chars))
        return out
    finally:
        doc.close()


def open_reader(fp):
    import pypdf
    r = pypdf.PdfReader(fp)
    if r.is_encrypted and not r.decrypt(""):
        raise ValueError("needs a password")
    return r


def out_path(out, org, repo, path):
    return os.path.join(out, org, repo, *path.split("/"))


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def merge_path(cfg, org, repo, path, vers, pages_rows):
    """One .pdf path: -> manifest row (MANIFEST_HEADER)."""
    import pypdf
    dest = out_path(cfg.out, org, repo, path)
    notes = Counter()
    today = set()
    if cfg.active_root and cfg.drop_active:
        ap = os.path.join(cfg.active_root, org, repo, *path.split("/"))
        if os.path.isfile(ap):
            try:
                today = {p[0] for p in page_prints(ap, cfg)}
            except Exception:                          # noqa: BLE001
                notes["today_unreadable"] += 1
        else:
            notes["no_file_today"] += 1
    seen, kept, outputs = set(), [], []
    n_read = n_bad = n_seen = n_dup = n_today = n_ocr = 0
    for r in vers:
        if r["action"] not in STORED or not r["stored_as"]:
            notes[r["action"]] += 1
            continue
        fp = os.path.join(cfg.src, r["stored_as"])
        try:
            if cfg.max_bytes and os.path.getsize(fp) > cfg.max_bytes:
                raise ValueError("too_large")
            prints = page_prints(fp, cfg)
            reader = open_reader(fp)
            if len(reader.pages) != len(prints):
                raise ValueError("page count differs between readers")
        except Exception as exc:                       # noqa: BLE001
            n_bad += 1
            notes["unreadable"] += 1
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            whole = "%s.%s.pdf" % (dest, r["blob"][:12])
            shutil.copyfile(fp, whole)
            outputs.append(os.path.relpath(whole, os.path.join(cfg.out, org, repo)))
            pages_rows.append([path, "version", r["blob"], r["first_date"], "",
                               "", "", "", "", "unreadable: %s" % str(exc)[:200], ""])
            continue
        n_read += 1
        for i, (hsh, w, h, chars) in enumerate(prints, 1):
            n_seen += 1
            if hsh in today:
                status, mp = "in_today", ""
                n_today += 1
            elif hsh in seen:
                status, mp = "duplicate", ""
                n_dup += 1
            else:
                seen.add(hsh)
                kept.append((reader, i - 1, fp))
                status, mp = "kept", len(kept)
                if chars < cfg.min_text_chars:
                    status = "kept_ocr_needed"
                    n_ocr += 1
            pages_rows.append([path, "version", r["blob"], r["first_date"], i,
                               hsh, w, h, chars, status, mp])
    if kept:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        sources = {fp for _r, _i, fp in kept}
        whole_file = (len(sources) == 1 and n_read == 1 and not n_dup
                      and not n_today
                      and len(kept) == len(kept[0][0].pages))
        tmp = dest + ".tmp.%d" % os.getpid()
        if whole_file:                                 # nothing to merge
            shutil.copyfile(kept[0][2], tmp)
        else:
            w = pypdf.PdfWriter()
            for reader, i, _fp in kept:
                w.add_page(reader.pages[i])
            with open(tmp, "wb") as fh:
                w.write(fh)
        os.replace(tmp, dest)
        outputs.insert(0, os.path.relpath(dest, os.path.join(cfg.out, org, repo)))
    return [path, len(vers), n_read, n_bad, n_seen, len(kept), n_dup, n_today,
            n_ocr, ";".join(outputs),
            ";".join("%s:%d" % kv for kv in sorted(notes.items()))]


def run_repo(cfg, org, repo):
    t0 = time.time()
    sd_in = os.path.join(cfg.src, STATE, org, repo)
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_extraction",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "paths": 0, "pages_seen": 0, "pages_kept": 0,
           "pages_duplicate": 0, "pages_in_today": 0, "pages_ocr_needed": 0,
           "versions_unreadable": 0, "files_written": 0, "error": ""}
    done_in = read_json(os.path.join(sd_in, "done.json"))
    if not done_in or done_in.get("status") != "ok":
        return rec
    by_path = {}
    for r in read_rows(os.path.join(sd_in, "manifest.csv")):
        if ext_key(r["path"]) == "pdf":
            by_path.setdefault(r["path"], []).append(r)
    shutil.rmtree(os.path.join(cfg.out, org, repo), ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)
    rows, pages_rows, status, error = [], [], "ok", ""
    try:
        for path in sorted(by_path):
            vers = sorted(by_path[path], key=lambda r: (r["first_date"], r["blob"]))
            rows.append(merge_path(cfg, org, repo, path, vers, pages_rows))
    except Exception as exc:                           # noqa: BLE001
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"), csv_text([MANIFEST_HEADER] + rows))
    write_atomic(os.path.join(sd, "pages.csv"), csv_text([PAGES_HEADER] + pages_rows))
    ix = {c: i for i, c in enumerate(MANIFEST_HEADER)}
    rec.update(status=status, seconds=round(time.time() - t0, 1), paths=len(rows),
               error=error, files_written=sum(len([o for o in r[ix["output"]].split(";")
                                                   if o]) for r in rows))
    for c in ("pages_seen", "pages_kept", "pages_duplicate", "pages_in_today",
              "pages_ocr_needed", "versions_unreadable"):
        rec[c] = sum(r[ix[c]] for r in rows)
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# summary and main
# --------------------------------------------------------------------------

COUNTS = ["paths", "files_written", "pages_seen", "pages_kept", "pages_duplicate",
          "pages_in_today", "pages_ocr_needed", "versions_unreadable"]


def combine(out):
    state = os.path.join(out, STATE)
    tot, per_repo, repos = Counter(), [], 0
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        for repo in sorted(os.listdir(os.path.join(state, org))):
            d = read_json(os.path.join(state, org, repo, "done.json"))
            if not d:
                continue
            repos += 1
            tot["repos_" + d["status"]] += 1
            if d["status"] != "ok" or not d["paths"]:
                continue
            tot.update({c: d[c] for c in COUNTS})
            per_repo.append([org, repo] + [d[c] for c in COUNTS])
    with open(os.path.join(out, "summary.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo"] + COUNTS)
        w.writerows(per_repo)
        w.writerow([])
        w.writerow(["total", ""] + [tot[c] for c in COUNTS])
        w.writerow([])
        w.writerow(["column", "meaning"])
        w.writerows([
            ("paths", "different .pdf paths in the repo's history (extracted)"),
            ("files_written", "merged PDFs + unreadable versions copied whole"),
            ("pages_seen", "pages in all readable versions"),
            ("pages_kept", "distinct pages (by rendered image), in the merged PDFs"),
            ("pages_duplicate", "pages identical to one kept earlier for the same path"),
            ("pages_in_today", "pages identical to today's file, dropped "
             "(only with --drop-active-pages)"),
            ("pages_ocr_needed", "kept pages with (almost) no text: scans/images, "
             "a scanner without OCR cannot read them"),
            ("versions_unreadable", "versions that could not be opened (password, "
             "damaged): copied whole, not merged")])
    return repos, tot


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src", required=True,
                    help="extract_versions.py --per-repo output folder")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--batch", action="append", default=[], metavar="CSV")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO")
    ap.add_argument("--active-root", help="today's files (for --drop-active-pages)")
    ap.add_argument("--drop-active-pages", action="store_true",
                    help="drop pages identical to a page of today's file")
    ap.add_argument("--scale", type=float, default=1.5,
                    help="render scale for the page hash (default 1.5 = 108 dpi)")
    ap.add_argument("--max-px", type=int, default=2400,
                    help="cap on the longer side of a rendered page (default 2400)")
    ap.add_argument("--min-text-chars", type=int, default=1,
                    help="a kept page with fewer text characters is ocr_needed "
                         "(default 1: a page with no text at all)")
    ap.add_argument("--max-bytes", type=int, default=0,
                    help="treat stored PDFs bigger than this as unreadable "
                         "(copied whole; default 0 = no limit)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--combine-only", action="store_true")
    ap.add_argument("--name")
    args = ap.parse_args()

    if args.combine_only:
        repos, tot = combine(args.out)
        print("combined %d repo(s): %s" % (repos, dict(tot)))
        return 0
    if args.drop_active_pages and not args.active_root:
        sys.exit("--drop-active-pages needs --active-root")
    if os.path.realpath(args.out) == os.path.realpath(args.src):
        sys.exit("--out must be a different folder from --in")
    if not os.path.isdir(os.path.join(args.src, STATE)):
        sys.exit("no _state folder under " + args.src)
    try:
        import pypdf                                    # noqa: F401
        import pypdfium2                                # noqa: F401
    except ImportError as exc:
        sys.exit("missing package: %s - pip install pypdfium2 pypdf" % exc)
    settings = {"scale": args.scale, "max_px": args.max_px,
                "min_text_chars": args.min_text_chars,
                "drop_active_pages": args.drop_active_pages}
    spath = os.path.join(args.out, "settings.json")
    before = read_json(spath)
    if before and before != settings and not args.redo:
        sys.exit("%s was made with other settings %s; use the same, or --redo "
                 "everything into a new --out" % (args.out, before))
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    write_atomic(spath, json.dumps(settings, indent=1))

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
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(args.out, LOGS, name + ".log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    cfg = SimpleNamespace(src=args.src, out=args.out, active_root=args.active_root,
                          drop_active=args.drop_active_pages, scale=args.scale,
                          max_px=args.max_px, min_text_chars=args.min_text_chars,
                          max_bytes=args.max_bytes)
    log.info("START %s repos=%d workers=%d settings=%s", name, len(todo),
             args.workers, settings)
    print("%d repo(s) to do" % len(todo), flush=True)
    t0, st = time.time(), Counter()
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf, \
            ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds"] + COUNTS
                        + ["error"])
        futs = {pool.submit(run_repo, cfg, o, r): (o, r) for o, r in todo}
        for fut in as_completed(futs):
            o, r = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:                    # noqa: BLE001
                st["failed"] += 1
                log.error("FAIL   %s/%s %s", o, r, exc)
                continue
            st[rec["status"]] += 1
            rw.writerow([rec["finished"], o, r, rec["status"], rec["seconds"]]
                        + [rec[c] for c in COUNTS] + [rec["error"]])
            rf.flush()
            if rec["status"] not in ("ok", "no_extraction"):
                log.error("%s %s/%s %s", rec["status"].upper(), o, r, rec["error"])
    repos, tot = combine(args.out)
    msg = ("END %s: %s | %s repo(s) combined: %s files, pages %s seen, %s kept, "
           "%s duplicate, %s ocr_needed | %.0fs" % (
               name, dict(st), f"{repos:,}", f"{tot['files_written']:,}",
               f"{tot['pages_seen']:,}", f"{tot['pages_kept']:,}",
               f"{tot['pages_duplicate']:,}", f"{tot['pages_ocr_needed']:,}",
               time.time() - t0))
    log.info(msg)
    print(msg)
    return 1 if st["error"] or st["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
