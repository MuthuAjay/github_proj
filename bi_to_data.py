#!/usr/bin/env python3
"""
bi_to_data.py - from the container files of group 4.7+4.9 (zips under
another name: pbix, pbit, pbi, twbx, dbc, mwb, numbers, kmz, qgz) take ONLY
the data and the images - nothing else (no report layout, queries, code,
connections, styles or settings; user, 26 Oct):

  data     pbix / pbi: the imported data model (DataModel: VertiPaq, XPress9
           compressed), every table read with pbixray
           twbx: every extract (Data/Extracts/*.hyper) read with Tableau's
           Hyper API; the data files packaged in it (anything else under
           Data/: xlsx, csv, ...) go whole
           dbc: the cell OUTPUTS of the notebooks (result tables, text,
           html as text, images in outputs); the code is not taken
           mwb: the sample rows in the model's SQLite file (@db/data.db)
           numbers: the cell text of the tables (Index/*.iwa: snappy +
           protobuf, unpacked here; strings taken, numbers not)
           kmz: the placemarks of every .kml inside (name, description,
           address, extra fields, point location)
           -> one .txt per file path: "table: <name>", its column names,
              then one line per row (values joined by " | "), every version,
              each line once
  images   png jpg gif svg ... anywhere inside -> whole, each distinct
           content once per repo
  pbit, qgz  hold no data: images only. Old Tableau extracts (.tde) and a
           pbix without a data model (live connection) are noted, not read

Input: as data_to_text.py - extract_versions.py --per-repo output (--in),
with --state + --active-root today's files too (today's copy of these types
was never really scanned); or --archive-inner on an archive_unpack.py output
(vendored / macOS / DocFX cache paths left out).

Output, under --out:
  data_text/<org>/<repo>/<path>.txt                 the data, as text
  files/<type>/<org>/<repo>/<sha256>.<type>         images, packaged data files
  _state/<org>/<repo>/manifest.csv   per path: versions read, tables, lines,
                                     images / data files, notes
  _state/<org>/<repo>/files.csv      per whole file: path, version, member
                                     inside, sha256, written / deduped
  _state/<org>/<repo>/left_out.csv, done.json
  _logs/<name>.log, <name>_repos.csv; summary.csv, by_type.csv

Needs: pip install pbixray tableauhyperapi (without them the data is noted
as not read - no_pbixray / no_hyperapi - and images still go; install and
run again with --redo). --check-libs shows what is installed.

Usage:
    python3 bi_to_data.py --in /data/workarea/group4_7_9/bi/extract \\
        --out /data/workarea/group4_7_9/bi/data \\
        --batch /data/workarea/group4_7_9/bi/batches/M479BI.csv \\
        --state /data/workarea/group4_7_9/bi/counts \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos
"""

import argparse
import atexit
import base64
import csv
import datetime
import gzip
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from types import SimpleNamespace

from archive_unpack import safe_type
from data_to_text import SEP, cell, lib, rows_by_path
from extension_versions import read_batch, read_json, read_rows
from file_delta import csv_text, write_atomic
from group4_2_text import LEFT_OUT_HEADER, clean_path, left_out_reason
from repo_extension_summary import ext_key, load_extensions

STATE, LOGS = "_state", "_logs"
STORED = ("written", "deduped")
DEFAULT_EXTS = ["pbix", "pbit", "pbi", "twbx", "dbc", "mwb", "numbers", "kmz",
                "qgz"]
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "tif", "tiff",
              "ico", "emf", "wmf", "jfif"}
CHUNK_ROWS = 50_000
MAX_MEMBER = 2_000_000_000
TAG = re.compile(r"<[^>]+>")
OUTPUT_IMAGES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                 "image/svg+xml": "svg"}
MANIFEST_HEADER = ["path", "type", "versions", "versions_read", "active_included",
                   "data_read", "tables", "lines", "images", "data_files",
                   "bytes_read", "note"]
FILES_HEADER = ["path", "version", "member", "kind", "bytes", "sha256", "action",
                "stored_as"]
log = logging.getLogger("bi_to_data")
_hyper = {}


# --------------------------------------------------------------------------
# data readers
# --------------------------------------------------------------------------

def row_lines(rows):
    for row in rows:
        line = SEP.join(cell(v) for v in row)
        if line.replace("|", "").strip():
            yield line


def pbix_lines(fp, notes, tmp):
    """Every table of a pbix data model, or nothing (noted)."""
    pr = lib("pbixray")
    if not pr:
        notes["no_pbixray"] += 1
        return
    try:
        model = pr.PBIXRay(fp, on_disk=True, temp_dir=tmp)
    except Exception as exc:                         # noqa: BLE001
        notes["datamodel_error"] += 1
        log.warning("datamodel %s: %s: %s", fp, type(exc).__name__, exc)
        return
    try:
        for t in list(model.tables):
            notes["tables"] += 1
            yield "table: %s" % t
            try:
                first = True
                for df in model.iter_table(t, chunk_size=CHUNK_ROWS,
                                           strings_as_categorical=False):
                    if first:
                        yield SEP.join(str(c) for c in df.columns)
                        first = False
                    yield from row_lines(df.itertuples(index=False, name=None))
            except Exception as exc:                 # noqa: BLE001 - one table
                notes["table_error"] += 1
                log.warning("table %s in %s: %s: %s", t, fp, type(exc).__name__, exc)
    finally:
        close = getattr(model, "close", None)
        if close:
            close()


def hyper_process():
    """One Hyper server per worker process, started on first use."""
    if "p" not in _hyper:
        th = lib("tableauhyperapi")
        _hyper["p"] = th.HyperProcess(telemetry=th.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU)
        atexit.register(_hyper["p"].close)
    return _hyper["p"]


def hyper_lines(fp, notes):
    """Every table of a Tableau .hyper extract."""
    th = lib("tableauhyperapi")
    if not th:
        notes["no_hyperapi"] += 1
        return
    try:
        with th.Connection(hyper_process().endpoint, fp) as c:
            for schema in c.catalog.get_schema_names():
                for tn in c.catalog.get_table_names(schema):
                    cols = [str(col.name.unescaped)
                            for col in c.catalog.get_table_definition(tn).columns]
                    notes["tables"] += 1
                    yield "table: %s" % tn
                    yield SEP.join(cols)
                    with c.execute_query("SELECT * FROM %s" % tn) as res:
                        yield from row_lines(res)
    except Exception as exc:                         # noqa: BLE001
        notes["hyper_error"] += 1
        log.warning("hyper %s: %s: %s", fp, type(exc).__name__, exc)


def notebook_lines(zf, infos, whole, notes):
    """dbc: the outputs of every notebook (Databricks JSON or .ipynb) - result
    tables, text, html as text; images in outputs go whole. No code."""
    for i in infos:
        if i.file_size > MAX_MEMBER or ext_key(i.filename) in IMAGE_EXTS:
            continue
        try:
            doc = json.loads(zf.read(i).decode("utf-8", "replace"))
        except (ValueError, zipfile.BadZipFile, OSError, NotImplementedError):
            continue
        if not isinstance(doc, dict):
            continue
        got = False
        for cmd in doc.get("commands") or []:            # Databricks
            todo = [(cmd or {}).get("results")]
            while todo:
                res = todo.pop(0)
                if not isinstance(res, dict):
                    continue
                todo += res.get("listResults") or []
                t, data = res.get("type"), res.get("data")
                if t == "table" and isinstance(data, list):
                    names = [str(s.get("name", "")) for s in res.get("schema") or []
                             if isinstance(s, dict)]
                    notes["tables"] += 1
                    got = True
                    yield "table: %s" % (doc.get("name") or i.filename)
                    if names:
                        yield SEP.join(names)
                    yield from row_lines(r if isinstance(r, list) else [r] for r in data)
                elif t in ("text", "raw", "ansi", "plain") and isinstance(data, str):
                    got = True
                    yield data
                elif t == "html" and isinstance(data, str):
                    got = True
                    yield html.unescape(TAG.sub(" ", data))
        for n, cell_ in enumerate(doc.get("cells") or []):  # Jupyter
            for k, out in enumerate((cell_ or {}).get("outputs") or []):
                if not isinstance(out, dict) or out.get("output_type") == "error":
                    continue
                if out.get("text"):
                    got = True
                    yield "".join(out["text"]) if isinstance(out["text"], list) else str(out["text"])
                data = out.get("data") or {}
                for mime, e in OUTPUT_IMAGES.items():
                    if mime in data:
                        raw = "".join(data[mime]) if isinstance(data[mime], list) else data[mime]
                        try:
                            b = raw.encode() if e == "svg" else base64.b64decode(raw)
                        except ValueError:
                            continue
                        whole.append(("%s#cell%d.%d.%s" % (i.filename, n, k, e), "image",
                                      len(b), lambda b=b: io.BytesIO(b)))
                        break
                else:
                    for mime in ("text/plain", "text/html"):
                        if mime in data:
                            v = data[mime]
                            v = "".join(v) if isinstance(v, list) else str(v)
                            got = True
                            yield html.unescape(TAG.sub(" ", v)) if mime == "text/html" else v
                            break
        notes["notebooks_with_output"] += got


def sqlite_lines(zf, infos, notes, tmp):
    """mwb: every table of every SQLite file inside (the sample rows)."""
    for i in infos:
        with zf.open(i) as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                continue
        dest = os.path.join(tmp, "db.sqlite")
        with zf.open(i) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        try:
            con = sqlite3.connect("file:%s?mode=ro" % dest, uri=True)
            try:
                names = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                for t in names:
                    cur = con.execute('SELECT * FROM "%s"' % t.replace('"', '""'))
                    notes["tables"] += 1
                    yield "table: %s" % t
                    yield SEP.join(str(d[0]) for d in cur.description)
                    yield from row_lines(cur)
            finally:
                con.close()
        except sqlite3.Error as exc:
            notes["sqlite_error"] += 1
            log.warning("sqlite %s: %s", i.filename, exc)


def snappy_raw(b):
    """Decompress raw snappy (no framing), as Apple's IWA chunks hold it."""
    pos = n = shift = 0
    while True:                                      # uncompressed length
        c = b[pos]
        pos += 1
        n |= (c & 0x7f) << shift
        shift += 7
        if c < 0x80:
            break
    out = bytearray()
    while pos < len(b):
        tag = b[pos]
        pos += 1
        t = tag & 3
        if t == 0:                                   # literal
            ln = tag >> 2
            if ln >= 60:
                k = ln - 59
                ln = int.from_bytes(b[pos:pos + k], "little")
                pos += k
            ln += 1
            out += b[pos:pos + ln]
            pos += ln
            continue
        if t == 1:
            ln, off = ((tag >> 2) & 7) + 4, ((tag >> 5) << 8) | b[pos]
            pos += 1
        elif t == 2:
            ln, off = (tag >> 2) + 1, int.from_bytes(b[pos:pos + 2], "little")
            pos += 2
        else:
            ln, off = (tag >> 2) + 1, int.from_bytes(b[pos:pos + 4], "little")
            pos += 4
        if off <= 0 or off > len(out):
            raise ValueError("bad snappy offset")
        if off >= ln:
            out += out[len(out) - off:len(out) - off + ln]
        else:                                        # overlapping copy
            for _ in range(ln):
                out.append(out[-off])
    return bytes(out)


def iwa_strings(data):
    """Strings of an IWA file: snappy chunks unpacked, then every protobuf
    length-delimited field whose bytes are printable UTF-8 text."""
    out, p, raw = [], 0, bytearray()
    while p + 4 <= len(data):
        ln = int.from_bytes(data[p + 1:p + 4], "little")
        raw += snappy_raw(data[p + 4:p + 4 + ln])
        p += 4 + ln
    raw, i, n = bytes(raw), 0, len(raw)
    while i < n - 2:
        if raw[i] & 7 == 2:                          # wire type 2 key byte
            ln, j, shift = 0, i + 1, 0
            while j < n and shift < 21:
                c = raw[j]
                ln |= (c & 0x7f) << shift
                j += 1
                shift += 7
                if c < 0x80:
                    break
            if 2 <= ln <= 100_000 and j + ln <= n:
                try:
                    s = raw[j:j + ln].decode("utf-8")
                except UnicodeDecodeError:
                    s = None
                if s and s.isprintable() and any(c.isalnum() for c in s):
                    out.append(s)
                    i = j + ln
                    continue
        i += 1
    return out


def numbers_lines(zf, infos, notes):
    """numbers: the text of the tables (Index/Tables/*.iwa, else every .iwa
    under Index/); the 2009 format: the text nodes of index.xml(.gz)."""
    names = [i for i in infos if i.filename.startswith("Index/") and i.filename.endswith(".iwa")]
    tables = [i for i in names if i.filename.startswith("Index/Tables/")]
    for i in tables or names:
        try:
            strs = iwa_strings(zf.read(i))
        except (ValueError, IndexError) as exc:
            notes["iwa_error"] += 1
            log.warning("iwa %s: %s", i.filename, exc)
            continue
        notes["tables"] += 1
        yield "table: %s" % i.filename
        yield from strs
    for i in infos:
        if i.filename.lower() in ("index.xml", "index.xml.gz"):
            b = zf.read(i)
            b = gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b
            notes["tables"] += 1
            for el in ET.iterparse(io.BytesIO(b)):
                if el[1].text and el[1].text.strip():
                    yield el[1].text


def kml_lines(zf, infos, notes):
    """kmz: one line per placemark of every .kml inside."""
    for i in infos:
        if ext_key(i.filename) != "kml":
            continue
        try:
            root = ET.fromstring(zf.read(i))
        except ET.ParseError as exc:
            notes["kml_error"] += 1
            log.warning("kml %s: %s", i.filename, exc)
            continue
        notes["tables"] += 1
        yield "table: %s" % i.filename
        yield SEP.join(["name", "description", "address", "fields", "point"])
        for pm in root.iter():
            if not pm.tag.endswith("Placemark"):
                continue
            val = {"name": "", "description": "", "address": "", "fields": [], "point": ""}
            for el in pm.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                text = (el.text or "").strip()
                if tag in ("name", "address") and not val[tag]:
                    val[tag] = text
                elif tag == "description" and not val["description"]:
                    val["description"] = html.unescape(TAG.sub(" ", text))
                elif tag in ("Data", "SimpleData"):
                    v = text if tag == "SimpleData" else "".join(
                        (c.text or "").strip() for c in el if c.tag.endswith("value"))
                    val["fields"].append("%s=%s" % (el.get("name", ""), v))
                elif tag == "Point":
                    val["point"] = " ".join((c.text or "").strip() for c in el
                                            if c.tag.endswith("coordinates"))
            val["fields"] = "; ".join(val["fields"])
            yield SEP.join(cell(val[k]) for k in ("name", "description", "address",
                                                   "fields", "point"))


def member_kind(ext, name):
    """image / extract / tde / data_file / '' (not taken) for a member."""
    low = name.replace("\\", "/").lower()
    e = ext_key(low)
    if e in IMAGE_EXTS:
        return "image"
    if ext == "twbx" and (low.startswith("data/") or "/data/" in low):
        return {"hyper": "extract", "tde": "tde"}.get(e, "data_file")
    return ""


def read_version(fp, ext, notes, tmp):
    """-> (line iterator, [(member, kind, size, opener)], zipfile) for one
    stored file. Readers may add to the list while their lines are read."""
    try:
        zf = zipfile.ZipFile(fp)
    except (zipfile.BadZipFile, OSError):
        notes["not_zip"] += 1
        return iter(()), [], None
    infos = [i for i in zf.infolist() if not i.is_dir()]
    whole = []
    gens = []
    if ext in ("pbix", "pbi"):
        if any(i.filename == "DataModel" for i in infos):
            gens.append(pbix_lines(fp, notes, tmp))
        else:
            notes["no_datamodel"] += 1
    elif ext == "dbc":
        gens.append(notebook_lines(zf, infos, whole, notes))
    elif ext == "mwb":
        gens.append(sqlite_lines(zf, infos, notes, tmp))
    elif ext == "numbers":
        gens.append(numbers_lines(zf, infos, notes))
    elif ext == "kmz":
        gens.append(kml_lines(zf, infos, notes))
    for i in infos:
        k = member_kind(ext, i.filename)
        if k in ("image", "data_file"):
            whole.append((i.filename, k, i.file_size, lambda i=i: zf.open(i)))
        elif k == "extract":
            dest = os.path.join(tmp, "x%d.hyper" % len(gens))
            with zf.open(i) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            gens.append(hyper_lines(dest, notes))
        elif k == "tde":
            notes["tde_not_read"] += 1
    return (line for g in gens for line in g), whole, zf


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def run_repo(cfg, org, repo):
    t0 = time.time()
    sd_in = os.path.join(cfg.src, STATE, org, repo)
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_extraction",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "paths": 0, "data_written": 0, "lines": 0,
           "images": 0, "data_files": 0, "notes": {}, "by_type": {},
           "left_out_paths": 0, "error": ""}
    done_in = read_json(os.path.join(sd_in, "done.json"))
    if not done_in or done_in.get("status") != "ok":
        return rec
    by_path, left = rows_by_path(os.path.join(sd_in, "manifest.csv"), org,
                                 repo, cfg.exts, cfg.inner)
    if cfg.state and cfg.active_root:
        fp = os.path.join(cfg.state, STATE, org, repo, "files.csv")
        for f in read_rows(fp) if os.path.isfile(fp) else []:
            e = ext_key(f["path"])
            if e not in cfg.exts or f.get("at_head") != "yes":
                continue
            why = left_out_reason(f["path"])
            if why:
                left.setdefault(f["path"], [org, repo, f["path"], e, why, 0])
                continue
            by_path.setdefault(f["path"], [])
    shutil.rmtree(os.path.join(cfg.out, "data_text", org, repo), ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)
    for t in os.listdir(os.path.join(cfg.out, "files")) \
            if os.path.isdir(os.path.join(cfg.out, "files")) else []:
        shutil.rmtree(os.path.join(cfg.out, "files", t, org, repo), ignore_errors=True)
    rows, frows, notes_all, by_type, status, error = [], [], Counter(), {}, "ok", ""
    seen_sha = set()
    fh = part = None
    tmp = tempfile.mkdtemp(prefix="bi_", dir=cfg.tmp)
    try:
        for path in sorted(by_path):
            ext = ext_key(path)
            vers = sorted(by_path[path], key=lambda r: (r["first_date"], r["blob"]))
            sources, notes = [], Counter()
            for r in vers:
                if r["action"] in STORED and r["stored_as"]:
                    sources.append((r["blob"][:12], os.path.join(cfg.src, r["stored_as"])))
                else:
                    notes[r["action"]] += 1
            active = ""
            if cfg.active_root and not cfg.inner:
                ap = os.path.join(cfg.active_root, org, repo, *path.split("/"))
                if os.path.isfile(ap):
                    sources.append(("today", ap))
                    active = "yes" if vers else "yes_only_today"
                else:
                    active = "no_file_today"
            dest = os.path.join(cfg.out, "data_text", org, repo, *clean_path(path)) + ".txt"
            part = dest + ".part"
            seen, nlines, nread, nbytes, nimg, ndata, ndata_read = set(), 0, 0, 0, 0, 0, 0
            for ver, fp in sources:
                size = os.path.getsize(fp)
                if cfg.max_bytes and size > cfg.max_bytes:
                    notes["too_large"] += 1
                    continue
                before = notes["tables"]
                before_out = notes["notebooks_with_output"]
                lines, whole, zf = read_version(fp, ext, notes, tmp)
                for s in lines:
                    for line in str(s).splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        k = hashlib.blake2b(line.encode("utf-8", "surrogatepass"),
                                            digest_size=16).digest()
                        if k in seen:
                            continue
                        seen.add(k)
                        if fh is None:
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            fh = open(part, "w", encoding="utf-8", errors="replace",
                                      newline="\n")
                        fh.write(line + "\n")
                        nlines += 1
                ndata_read += (notes["tables"] > before
                               or notes["notebooks_with_output"] > before_out)
                for member, kind, msize, opener in whole:
                    t = safe_type(ext_key(member)) or "noext"
                    h = hashlib.sha256()
                    tf = os.path.join(tmp, "member")
                    with opener() as src, open(tf, "wb") as out:
                        for b in iter(lambda: src.read(1 << 20), b""):
                            h.update(b)
                            out.write(b)
                    sha = h.hexdigest()
                    rel = os.path.join("files", t, org, repo, "%s.%s" % (sha, t))
                    if msize == 0:
                        act = "empty"
                    elif sha in seen_sha:
                        act = "deduped"
                    else:
                        act = "written"
                        seen_sha.add(sha)
                        os.makedirs(os.path.dirname(os.path.join(cfg.out, rel)), exist_ok=True)
                        shutil.move(tf, os.path.join(cfg.out, rel))
                        if kind == "image":
                            nimg += 1
                        else:
                            ndata += 1
                    if os.path.exists(tf):
                        os.remove(tf)
                    frows.append([path, ver, member, kind, msize, sha, act,
                                  rel if act != "empty" else ""])
                if zf:
                    zf.close()
                for f in os.listdir(tmp):                # pbixray / hyper temp files
                    p = os.path.join(tmp, f)
                    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
                nread += 1
                nbytes += size
            if fh is not None:
                fh.close()
                fh = None
                os.replace(part, dest)
            tables = notes.pop("tables", 0)
            notes = +notes                          # drop zero counts
            notes_all.update(notes)
            t = by_type.setdefault(ext, [0, 0, 0, 0, 0, 0])  # paths data lines img files bytes
            for i, v in enumerate((1, 1 if nlines else 0, nlines, nimg, ndata, nbytes)):
                t[i] += v
            rows.append([path, ext, len(vers), nread, active, ndata_read, tables,
                         nlines, nimg, ndata, nbytes,
                         ";".join("%s:%d" % kv for kv in sorted(notes.items()))])
    except Exception as exc:                         # noqa: BLE001 - one repo
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
        if fh is not None:
            fh.close()
            os.remove(part)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"), csv_text([MANIFEST_HEADER] + rows))
    write_atomic(os.path.join(sd, "files.csv"), csv_text([FILES_HEADER] + frows))
    write_atomic(os.path.join(sd, "left_out.csv"),
                 csv_text([LEFT_OUT_HEADER] + list(left.values())))
    rec.update(status=status, seconds=round(time.time() - t0, 1), paths=len(rows),
               data_written=sum(1 for r in rows if r[7]),
               lines=sum(r[7] for r in rows), images=sum(r[8] for r in rows),
               data_files=sum(r[9] for r in rows), notes=dict(notes_all),
               by_type=by_type, left_out_paths=len(left), error=error)
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# summary and main
# --------------------------------------------------------------------------

def combine(out):
    state = os.path.join(out, STATE)
    tot, notes, repos, types, per_repo = Counter(), Counter(), 0, {}, []
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        for repo in sorted(os.listdir(os.path.join(state, org))):
            d = read_json(os.path.join(state, org, repo, "done.json"))
            if not d:
                continue
            repos += 1
            tot[d["status"]] += 1
            if d["status"] != "ok":
                continue
            notes.update(d["notes"])
            for k in ("paths", "data_written", "lines", "images", "data_files",
                      "left_out_paths"):
                tot[k] += d.get(k, 0)
            for e, v in d.get("by_type", {}).items():
                t = types.setdefault(e, [0] * 7)
                for i in range(6):
                    t[i] += v[i]
                t[6] += 1
            per_repo.append([org, repo, d["paths"], d["data_written"], d["lines"],
                             d["images"], d["data_files"],
                             ";".join("%s:%d" % kv for kv in sorted(d["notes"].items())),
                             d.get("left_out_paths", 0)])
    with open(os.path.join(out, "summary.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["org", "repo", "paths", "data_txt_written", "lines", "images",
                    "data_files", "notes", "left_out_paths"])
        w.writerows(per_repo)
        w.writerow([])
        w.writerow(["total", "", tot["paths"], tot["data_written"], tot["lines"],
                    tot["images"], tot["data_files"],
                    ";".join("%s:%d" % kv for kv in sorted(notes.items())),
                    tot["left_out_paths"]])
        w.writerow([])
        w.writerow(["column", "meaning"])
        w.writerows([
            ("paths", "different paths of the chosen types (history + today)"),
            ("data_txt_written", "paths whose data gave text (one .txt each, data_text/)"),
            ("lines", "data lines written (table names, column names, rows), each once per path"),
            ("images", "images written whole (files/), each distinct content once per repo"),
            ("data_files", "twbx: packaged data files written whole (files/)"),
            ("notes", "no_datamodel = pbix without imported data (live connection); "
             "no_pbixray / no_hyperapi = library missing, data not read; "
             "datamodel_error / table_error / hyper_error = could not be read (see "
             "the log); tde_not_read = old Tableau extract; sqlite_error / "
             "iwa_error / kml_error (mwb / numbers / kmz); notebooks_with_output = "
             "dbc notebooks that had outputs; not_zip; too_large; "
             "lfs_stub / empty ... = versions with no content in the archive"),
            ("left_out_paths", "vendored / macOS / DocFX cache paths left out")])
    with open(os.path.join(out, "by_type.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["type", "repos", "paths", "data_txt_written", "lines", "images",
                    "data_files", "bytes_read"])
        for e, t in sorted(types.items(), key=lambda kv: -kv[1][0]):
            w.writerow([e, t[6]] + t[:6])
        w.writerow(["total", repos, tot["paths"], tot["data_written"], tot["lines"],
                    tot["images"], tot["data_files"], sum(t[5] for t in types.values())])
    return repos, tot


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out")
    ap.add_argument("--extensions", help="comma list or file (default %s)"
                    % ",".join(DEFAULT_EXTS))
    ap.add_argument("--batch", action="append", default=[], metavar="CSV")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO")
    ap.add_argument("--active-root", help="include today's files from here")
    ap.add_argument("--state", help="extension_versions.py output: with "
                    "--active-root every path at head is read")
    ap.add_argument("--archive-inner", action="store_true",
                    help="--in is archive_unpack.py output: files inside archives")
    ap.add_argument("--max-bytes", type=int, default=5_000_000_000,
                    help="skip files bigger than this (default 5 GB)")
    ap.add_argument("--tmp", help="folder for temporary files (default: --out/_tmp)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--redo", action="store_true", help="redo repos already done")
    ap.add_argument("--combine-only", action="store_true")
    ap.add_argument("--check-libs", action="store_true")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    args = ap.parse_args()

    if args.check_libs:
        for m in ("pbixray", "tableauhyperapi", "pandas"):
            print("%-16s %s" % (m, "yes" if lib(m) else "no"))
        return 0
    if not args.out:
        sys.exit("--out is needed")
    if args.combine_only:
        repos, tot = combine(args.out)
        print("combined %d repo(s): %s" % (repos, dict(tot)))
        return 0
    if not args.src or not os.path.isdir(os.path.join(args.src, STATE)):
        sys.exit("--in with a _state folder is needed")
    if os.path.realpath(args.out) == os.path.realpath(args.src):
        sys.exit("--out must be a different folder from --in")
    if args.archive_inner and (args.active_root or args.state):
        sys.exit("--archive-inner reads archives only: no --active-root / --state")
    if args.state and not args.active_root:
        sys.exit("--state needs --active-root")
    exts = set(load_extensions(args.extensions) if args.extensions else DEFAULT_EXTS)
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
    tmp = args.tmp or os.path.join(args.out, "_tmp")
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    os.makedirs(tmp, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(args.out, LOGS, name + ".log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    cfg = SimpleNamespace(src=args.src, out=args.out, active_root=args.active_root,
                          state=args.state, max_bytes=args.max_bytes,
                          inner=args.archive_inner, exts=exts, tmp=tmp)
    log.info("START %s repos=%d workers=%d in=%s active=%s types=%s libs=%s", name,
             len(todo), args.workers, args.src, args.active_root or "-",
             ",".join(sorted(exts)),
             ",".join(m for m in ("pbixray", "tableauhyperapi") if lib(m)) or "none")
    print("%d repo(s) to do" % len(todo), flush=True)
    t0, st = time.time(), Counter()
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as rf, \
            ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds", "paths",
                         "data_written", "lines", "images", "error"])
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
                         rec["paths"], rec["data_written"], rec["lines"],
                         rec["images"], rec["error"]])
            rf.flush()
            if rec["status"] not in ("ok", "no_extraction"):
                log.error("%s %s/%s %s", rec["status"].upper(), o, r, rec["error"])
    repos, tot = combine(args.out)
    msg = "END %s: %s | %s repo(s): %s data .txt, %s lines, %s images, %s data files | %.0fs" % (
        name, dict(st), f"{repos:,}", f"{tot['data_written']:,}", f"{tot['lines']:,}",
        f"{tot['images']:,}", f"{tot['data_files']:,}", time.time() - t0)
    log.info(msg)
    print(msg)
    return 1 if st["error"] or st["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
