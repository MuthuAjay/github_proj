#!/usr/bin/env python3
"""
data_to_text.py - turn the group 4.7+4.9 table / serialized data types
(group 3 route: parquet, rds, pickle, npy, ...) into text a PII scanner can
read: one .txt per file path, every value any version held, each line once.
Nothing is ever loaded as code: pickles are walked with pickletools (as
pkl_to_text.py does, whose reader is used), R files are scanned for their
string records, the rest is read by a format library or scanned for text.

Input: as pkl_to_text.py - an extract_versions.py --per-repo output (--in),
whose _state/<org>/<repo>/manifest.csv lists every stored version; or with
--archive-inner an archive_unpack.py output (the files inside the 4.2 /
4.2_R archives: vendored, macOS metadata and DocFX cache paths left out).

How a stored file is read (the format comes from its first bytes, the type
name only breaks ties); read_as in the manifest says which way was taken:

  gzip bz2 xz zstd lz4    unpacked first (zstd / lz4: needs zstandard / lz4)
  parquet orc arrow       pyarrow: a line of column names, then one line per
  feather                 row, the values joined by " | "
  pickle pk joblib        pkl_to_text.py's opcode walk (joblib zlib/gzip too)
  rds rda rdata           R serialization: every string record (values,
                          column / level / attribute names) is taken
  npy                     text arrays (numpy U / S) item by item, object
                          arrays through the pickle walk; numbers only ->
                          no text (npy_numeric)
  npz ckpt (zip)          every member read as above, its name a line too;
                          PyTorch tensor storages (data/<n>) are numbers and
                          are skipped
  bson                    every field, "key.sub: value"
  avro                    fastavro: one line per record (JSON)
  sav (SPSS)              pyreadstat: names, labels, value labels, rows
  nippy                   lz4 unpacked when possible, then the strings scan
  text                    a file that is plain text: its lines
  anything else           the raw strings scan (ASCII / UTF-8 / UTF-16 /
                          UTF-32 runs, as `strings` reads a binary)

A format whose library is not installed, or a file the library can not
read, gets the raw strings scan and says so (e.g. parquet_no_pyarrow_raw_scan,
parquet_error_raw_scan); after installing the library run again with
--redo. --check-libs shows what is installed.

--strict-raw: the raw strings scan keeps only runs that look like text -
6+ characters with a word, or a number with separators (5+ digits) - as
number buffers in pickles / models give millions of random 4-character
lines (pkl_to_text.raw_keep). Text read from the format itself (opcode
strings, table values, R strings, numpy text) is never filtered. Off by
default: the 26 Oct group 4.7+4.9 output was made without it (user: kept
as it is).

--active-root (repo level): today's file at a path that has stored versions
is read too. With --state (the extension_versions.py output the extraction
came from) every path at head is read from --active-root, also those whose
only content is today's file (not extracted, as it was "already
processed" - but a scanner can not read these types, so it was not).

Output, under --out (as pkl_to_text.py):
  <org>/<repo>/<path>.txt              the text, content only
  _state/<org>/<repo>/manifest.csv     per path: type, versions, how each
                                       was read, lines written, notes
  _state/<org>/<repo>/left_out.csv     vendored / macOS / DocFX cache paths
  _state/<org>/<repo>/done.json        written last (resume)
  _logs/<name>.log, <name>_repos.csv
  summary.csv, by_type.csv             totals per repo / per type

Usage:
    python3 data_to_text.py --check-libs
    python3 data_to_text.py --in /data/workarea/group4_7_9/group3/extract \\
        --out /data/workarea/group4_7_9/group3/text \\
        --batch /data/workarea/group4_7_9/group3/batches/M479G3.csv \\
        --state /data/workarea/group4_7_9/group3/counts \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos
    python3 data_to_text.py --in /data/workarea/group4_2/unpacked \\
        --out /data/workarea/group4_2/group4_7_9_group3_text --archive-inner
"""

import argparse
import ast
import bz2
import csv
import datetime
import gzip
import hashlib
import importlib
import io
import json
import logging
import lzma
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from types import SimpleNamespace

import pkl_to_text
from extension_versions import read_batch, read_json, read_rows
from file_delta import csv_text, write_atomic
from group4_2_text import LEFT_OUT_HEADER, left_out_reason
from pkl_to_text import out_txt, pickle_text, scan_bytes, text_of
from repo_extension_summary import ext_key, load_extensions

STATE, LOGS = "_state", "_logs"
STORED = ("written", "deduped")
DEFAULT_EXTS = ["parquet", "rds", "pickle", "npy", "sav", "npz", "ckpt",
                "joblib", "rda", "nippy", "orc", "arrow", "rdata", "bson",
                "feather", "pk", "avro"]
PICKLE_EXTS = {"pickle", "pk", "pkl", "joblib"}
R_EXTS = {"rds", "rda", "rdata"}
OPTIONAL = ["pyarrow", "zstandard", "lz4", "fastavro", "pyreadstat", "pandas"]
MANIFEST_HEADER = ["path", "type", "versions", "versions_read",
                   "active_included", "read_as", "lines", "bytes_read", "note"]
SEP = " | "
BATCH_ROWS = 10_000
MAX_DEPTH = 4                          # compression / zip inside each other
# an R CHARSXP header (XDR, big-endian): flags 00 0L LL 09 (L = encoding /
# cache levels), then a 4-byte length and the bytes
R_CHARSXP = re.compile(rb"\x00[\x00-\x07][\x00\x20\x40\x60\x80\xa0\xc0\xe0]\x09")
TORCH_STORAGE = re.compile(r"(^|/)data/\d+$")
log = logging.getLogger("data_to_text")
MAX_MEMBER = 2_000_000_000             # set per worker from --max-bytes
_libs = {}


def lib(name):
    """An optional module, or None when it is not installed."""
    if name not in _libs:
        try:
            _libs[name] = importlib.import_module(name)
        except Exception:                            # noqa: BLE001
            _libs[name] = None
    return _libs[name]


class Reading:
    """How one stored file was read: every way taken, in order, once."""
    def __init__(self):
        self.ways = []

    def add(self, how):
        if how not in self.ways:
            self.ways.append(how)

    def how(self):
        return "+".join(self.ways) or "nothing"


# --------------------------------------------------------------------------
# formats
# --------------------------------------------------------------------------

def decompress(data):
    """-> (bytes, how) when data is a compressed stream, else (None, '')."""
    try:
        if data[:2] == b"\x1f\x8b":
            return gzip.decompress(data), "gzip"
        if data[:3] == b"BZh" and data[3:4].isdigit():
            return bz2.decompress(data), "bz2"
        if data[:6] == b"\xfd7zXZ\x00":
            return lzma.decompress(data), "xz"
        if data[:4] == b"\x28\xb5\x2f\xfd":
            zs = lib("zstandard")
            if not zs:
                return None, "zstd_no_zstandard"
            return zs.ZstdDecompressor().decompressobj().decompress(data), "zstd"
        if data[:4] == b"\x04\x22\x4d\x18":
            lz = lib("lz4.frame")
            if not lz:
                return None, "lz4_no_lz4"
            return lz.decompress(data), "lz4"
    except Exception:                                # noqa: BLE001 - bad data
        return None, "unpack_failed"
    return None, ""


def looks_text(data):
    head = data[:65536]
    if head[:3] == b"\xef\xbb\xbf" or head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start > len(head) - 4               # cut in a character


def detect(data, ext):
    if data[:4] == b"PAR1":
        return "parquet"
    if data[:3] == b"ORC":
        return "orc"
    if data[:6] == b"ARROW1":
        return "arrow_file"
    if data[:4] == b"FEA1":
        return "feather_v1"
    if data[:4] == b"\xff\xff\xff\xff" and ext in ("arrow", "feather"):
        return "arrow_stream"
    if data[:6] == b"\x93NUMPY":
        return "npy"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if data[:4] == b"Obj\x01":
        return "avro"
    if data[:4] in (b"$FL2", b"$FL3"):
        return "spss"
    if data[:4] == b"SR\x00\x04":
        return "idl"
    if data[:5] in (b"RDX2\n", b"RDX3\n", b"RDA2\n", b"RDA3\n", b"RDB2\n",
                    b"RDB3\n") or (ext in R_EXTS and data[:2] in (b"X\n", b"A\n", b"B\n")):
        return "r"
    if data[:3] == b"NPY":
        return "nippy"
    if data[:1] == b"\x80" and data[1:2] in (b"\x02", b"\x03", b"\x04", b"\x05") \
            or ext in PICKLE_EXTS:
        return "pickle"
    if ext == "bson" and len(data) >= 5 and 5 <= int.from_bytes(data[:4], "little") <= len(data):
        return "bson"
    if looks_text(data):
        return "text"
    return "raw"


def read_any(data, ext, rd, depth=0):
    """Yield the strings of one stored file (or zip member)."""
    if not data:
        rd.add("empty")
        return
    if depth < MAX_DEPTH:
        inner, how = decompress(data)
        if inner is not None:
            rd.add(how)
            yield from read_any(inner, ext, rd, depth + 1)
            return
        if how:                                      # compressed, no reader
            rd.add(how + "_raw_scan")
            yield from scan_bytes(data)
            return
    fmt = detect(data, ext)
    yield from READERS[fmt](data, ext, rd, depth)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v)
        return v.decode("utf-8", "replace") if looks_text(v) else ""
    if isinstance(v, (list, tuple, dict)):
        v = json.dumps(v, default=str, ensure_ascii=False)
    return str(v).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def rows_lines(names, columns):
    """names + columns (lists of values) -> one header line, a line per row."""
    yield SEP.join(names)
    for row in zip(*columns):
        line = SEP.join(cell(v) for v in row)
        if line.replace("|", "").strip():
            yield line


def orc_batches(pa, of):
    for i in range(of.nstripes):
        s = of.read_stripe(i)                        # a RecordBatch (or Table)
        yield from (s.to_batches() if isinstance(s, pa.Table) else [s])


def read_table(data, ext, rd, depth, fmt):
    pa = lib("pyarrow")
    if not pa:
        rd.add(fmt + "_no_pyarrow_raw_scan")
        yield from scan_bytes(data)
        return
    try:
        buf = pa.py_buffer(data)
        if fmt == "parquet":
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(pa.BufferReader(buf))
            batches = pf.iter_batches(batch_size=BATCH_ROWS)
        elif fmt == "orc":
            import pyarrow.orc as po
            of = po.ORCFile(pa.BufferReader(buf))
            batches = orc_batches(pa, of)
        elif fmt == "arrow_file":
            r = pa.ipc.open_file(buf)
            batches = (r.get_batch(i) for i in range(r.num_record_batches))
        elif fmt == "arrow_stream":
            batches = iter(pa.ipc.open_stream(buf))
        else:                                        # feather_v1
            import pyarrow.feather as pfe
            batches = iter(pfe.read_table(pa.BufferReader(buf)).to_batches(BATCH_ROWS))
        rd.add(fmt)
        header = True
        for b in batches:
            names = [str(n) for n in b.schema.names]
            lines = rows_lines(names, [c.to_pylist() for c in b.columns])
            if not header:
                next(lines)                          # column names once
            header = False
            yield from lines
    except Exception:                                # noqa: BLE001 - bad file
        rd.add(fmt + "_error_raw_scan")
        yield from scan_bytes(data)


def read_npy(data, ext, rd, depth):
    try:
        size = 2 if data[6] == 1 else 4
        hlen = int.from_bytes(data[8:8 + size], "little")
        start = 8 + size
        head = ast.literal_eval(data[start:start + hlen].decode("latin-1"))
        descr, body = head["descr"], data[start + hlen:]
    except Exception:                                # noqa: BLE001
        rd.add("npy_bad_header_raw_scan")
        yield from scan_bytes(data)
        return
    if not isinstance(descr, str):                   # structured: raw scan
        rd.add("npy_structured_raw_scan")
        yield from scan_bytes(body)
        return
    kind, width = descr.lstrip("<>|=")[:1], descr.lstrip("<>|=")[1:]
    if kind == "O":
        rd.add("npy_object_pickle")
        yield from pickle_text(body)[0]
        return
    if kind in ("U", "S", "a") and width.isdigit() and int(width) > 0:
        n = int(width) * (4 if kind == "U" else 1)
        enc = ("utf-32-be" if descr[0] == ">" else "utf-32-le") if kind == "U" else "utf-8"
        rd.add("npy_text")
        for i in range(0, len(body) - n + 1, n):
            s = body[i:i + n].decode(enc, "replace").rstrip("\x00")
            if s:
                yield s
        return
    rd.add("npy_numeric")                            # numbers only: no text


def read_zip(data, ext, rd, depth):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except Exception:                                # noqa: BLE001
        rd.add("zip_bad_raw_scan")
        yield from scan_bytes(data)
        return
    rd.add("zip")
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename
        if TORCH_STORAGE.search(name):
            rd.add("zip_tensor_storage_skipped")
            continue
        if info.file_size > MAX_MEMBER:
            rd.add("zip_member_too_large")
            continue
        try:
            b = zf.read(info)
        except Exception:                            # noqa: BLE001 - encrypted ...
            rd.add("zip_member_unreadable")
            continue
        yield name
        mext = ext_key(name)
        if depth + 1 >= MAX_DEPTH:
            rd.add("too_deep_raw_scan")
            yield from scan_bytes(b)
        else:
            yield from read_any(b, "pickle" if mext == "pkl" else mext, rd, depth + 1)


def read_pickle(data, ext, rd, depth):
    strs, how = text_of(data)
    rd.add(how)
    yield from strs


def read_r(data, ext, rd, depth):
    body = data[5:] if data[:3] in (b"RDX", b"RDA", b"RDB") else data
    if body[:2] == b"A\n":                           # ASCII serialization
        rd.add("r_ascii")
        yield from body.decode("utf-8", "replace").splitlines()
        return
    if body[:2] != b"X\n":                           # native binary: rare
        rd.add("r_native_raw_scan")
        yield from scan_bytes(data)
        return
    got, pos, n = 0, 0, len(body)
    while True:
        m = R_CHARSXP.search(body, pos)
        if not m:
            break
        p = m.start()
        length = int.from_bytes(body[p + 4:p + 8], "big", signed=True)
        if 0 < length <= n - p - 8:
            raw = body[p + 8:p + 8 + length]
            try:
                s = raw.decode("latin-1" if body[p + 2] & 0x40 and not body[p + 2] & 0x80
                               else "utf-8")
            except UnicodeDecodeError:
                s = None
            if s is not None and (length >= 4 or s.isprintable()):
                got += 1
                yield s
                pos = p + 8 + length
                continue
        pos = p + 1
    if got:
        rd.add("r_xdr")
    else:
        rd.add("r_raw_scan")
        yield from scan_bytes(body)


def bson_doc(b, i, end, prefix, out):
    while i < end:
        t = b[i]
        j = b.index(b"\x00", i + 1)
        name = prefix + b[i + 1:j].decode("utf-8", "replace")
        i = j + 1
        if t in (0x02, 0x0D, 0x0E):                  # string, code, symbol
            n = struct.unpack_from("<i", b, i)[0]
            out.append("%s: %s" % (name, b[i + 4:i + 3 + n].decode("utf-8", "replace")))
            i += 4 + n
        elif t in (0x03, 0x04):                      # document, array
            n = struct.unpack_from("<i", b, i)[0]
            bson_doc(b, i + 4, i + n - 1, name + ".", out)
            i += n
        elif t == 0x01:
            out.append("%s: %r" % (name, struct.unpack_from("<d", b, i)[0]))
            i += 8
        elif t == 0x05:                              # binary
            n = struct.unpack_from("<i", b, i)[0]
            sub, v = b[i + 4], b[i + 5:i + 5 + n]
            if sub in (3, 4) and n == 16:
                out.append("%s: %s" % (name, v.hex()))
            else:
                out += ["%s: %s" % (name, s) for s in scan_bytes(v)]
            i += 5 + n
        elif t == 0x07:
            out.append("%s: %s" % (name, b[i:i + 12].hex()))
            i += 12
        elif t == 0x08:
            out.append("%s: %s" % (name, bool(b[i])))
            i += 1
        elif t == 0x09:
            ms = struct.unpack_from("<q", b, i)[0]
            try:
                v = (datetime.datetime(1970, 1, 1)
                     + datetime.timedelta(milliseconds=ms)).isoformat()
            except OverflowError:
                v = str(ms)
            out.append("%s: %s" % (name, v))
            i += 8
        elif t in (0x06, 0x0A, 0x7F, 0xFF):          # undefined, null, min/max
            pass
        elif t == 0x0B:                              # regex: two cstrings
            k = b.index(b"\x00", i)
            e = b.index(b"\x00", k + 1)
            out.append("%s: /%s/" % (name, b[i:k].decode("utf-8", "replace")))
            i = e + 1
        elif t == 0x0C:                              # DBPointer
            n = struct.unpack_from("<i", b, i)[0]
            out.append("%s: %s" % (name, b[i + 4:i + 3 + n].decode("utf-8", "replace")))
            i += 4 + n + 12
        elif t == 0x0F:                              # code with scope
            total = struct.unpack_from("<i", b, i)[0]
            n = struct.unpack_from("<i", b, i + 4)[0]
            out.append("%s: %s" % (name, b[i + 8:i + 7 + n].decode("utf-8", "replace")))
            k = i + 8 + n
            bson_doc(b, k + 4, k + struct.unpack_from("<i", b, k)[0] - 1, name + ".", out)
            i += total
        elif t == 0x10:
            out.append("%s: %d" % (name, struct.unpack_from("<i", b, i)[0]))
            i += 4
        elif t in (0x11, 0x12):
            out.append("%s: %d" % (name, struct.unpack_from("<q", b, i)[0]))
            i += 8
        elif t == 0x13:                              # decimal128
            out.append("%s: %s" % (name, b[i:i + 16].hex()))
            i += 16
        else:
            raise ValueError("bson type %#x" % t)


def read_bson(data, ext, rd, depth):
    out, pos, n = [], 0, len(data)
    try:
        while pos + 5 <= n:
            size = int.from_bytes(data[pos:pos + 4], "little")
            if size < 5 or pos + size > n or data[pos + size - 1] != 0:
                raise ValueError("bad document size")
            bson_doc(data, pos + 4, pos + size - 1, "", out)
            pos += size
        rd.add("bson")
    except Exception:                                # noqa: BLE001
        rd.add("bson_error_raw_scan")
        out += scan_bytes(data[pos:])
    yield from out


def read_avro(data, ext, rd, depth):
    fa = lib("fastavro")
    if not fa:
        rd.add("avro_no_fastavro_raw_scan")
        yield from scan_bytes(data)
        return
    try:
        r = fa.reader(io.BytesIO(data))
        recs = list(r)
        rd.add("avro")
    except Exception:                                # noqa: BLE001
        rd.add("avro_error_raw_scan")
        yield from scan_bytes(data)
        return
    for rec in recs:
        yield json.dumps(rec, default=str, ensure_ascii=False)


def read_spss(data, ext, rd, depth):
    prs = lib("pyreadstat")
    if not prs:
        rd.add("spss_no_pyreadstat_raw_scan")
        yield from scan_bytes(data)
        return
    try:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "f.sav")
            with open(fp, "wb") as fh:
                fh.write(data)
            df, meta = prs.read_sav(fp)
        rd.add("spss")
    except Exception:                                # noqa: BLE001
        rd.add("spss_error_raw_scan")
        yield from scan_bytes(data)
        return
    yield from (s for s in (meta.column_labels or []) if s)
    for var, labels in (meta.variable_value_labels or {}).items():
        for k, v in labels.items():
            yield "%s: %s = %s" % (var, k, v)
    names = [str(c) for c in df.columns]
    yield from rows_lines(names, [df[c].tolist() for c in df.columns])


def read_nippy(data, ext, rd, depth):
    lz = lib("lz4.block")
    if lz and len(data) > 8:
        try:
            n = int.from_bytes(data[4:8], "big")
            out = lz.decompress(data[8:], uncompressed_size=n)
            rd.add("nippy_lz4")
            yield from scan_bytes(out)
            return
        except Exception:                            # noqa: BLE001 - other codec
            pass
    rd.add("nippy_raw_scan" if lz else "nippy_no_lz4_raw_scan")
    yield from scan_bytes(data)


def read_text(data, ext, rd, depth):
    rd.add("text")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        yield data.decode("utf-16", "replace")
    else:
        yield data.decode("utf-8-sig", "replace")


def read_raw(data, ext, rd, depth):
    rd.add("raw_scan" if ext != "idl" else "idl_raw_scan")
    yield from scan_bytes(data)


READERS = {
    "parquet": lambda d, e, r, x: read_table(d, e, r, x, "parquet"),
    "orc": lambda d, e, r, x: read_table(d, e, r, x, "orc"),
    "arrow_file": lambda d, e, r, x: read_table(d, e, r, x, "arrow_file"),
    "arrow_stream": lambda d, e, r, x: read_table(d, e, r, x, "arrow_stream"),
    "feather_v1": lambda d, e, r, x: read_table(d, e, r, x, "feather_v1"),
    "npy": read_npy, "zip": read_zip, "pickle": read_pickle, "r": read_r,
    "bson": read_bson, "avro": read_avro, "spss": read_spss,
    "idl": lambda d, e, r, x: read_raw(d, "idl", r, x),
    "nippy": read_nippy, "text": read_text, "raw": read_raw,
}


def strings_of(data, ext):
    """-> (iterator of strings, Reading) for one stored file."""
    rd = Reading()
    return read_any(data, ext, rd), rd


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

def rows_by_path(manifest, org, repo, exts, inner):
    """A manifest's rows of the chosen types -> ({path: [rows]}, left_out
    rows). inner: files inside an archive only. Vendored, macOS and DocFX
    cache paths are left out (with their reason and versions) in both modes."""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    by_path, left = {}, {}
    with open(manifest, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            e = ext_key(r["path"])
            if e not in exts or (inner and not r.get("inner_path")):
                continue
            why = left_out_reason(r["path"])
            if why:
                left.setdefault(r["path"], [org, repo, r["path"], e, why, 0])[5] += 1
                continue
            by_path.setdefault(r["path"], []).append(r)
    return by_path, left


def run_repo(cfg, org, repo):
    global MAX_MEMBER
    MAX_MEMBER = cfg.max_bytes or MAX_MEMBER
    pkl_to_text.RAW_KEEP = pkl_to_text.raw_keep if cfg.strict_raw else None
    t0 = time.time()
    sd_in = os.path.join(cfg.src, STATE, org, repo)
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_extraction",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "paths": 0, "files_written": 0, "lines": 0,
           "read_as": {}, "by_type": {}, "left_out_paths": 0, "error": ""}
    done_in = read_json(os.path.join(sd_in, "done.json"))
    if not done_in or done_in.get("status") != "ok":
        return rec                                   # nothing written: later
    by_path, left = rows_by_path(os.path.join(sd_in, "manifest.csv"), org,
                                 repo, cfg.exts, cfg.inner)
    head = set()
    if cfg.state and cfg.active_root:                # every path at head
        fp = os.path.join(cfg.state, STATE, org, repo, "files.csv")
        for f in read_rows(fp) if os.path.isfile(fp) else []:
            e = ext_key(f["path"])
            if e not in cfg.exts or f.get("at_head") != "yes":
                continue
            why = left_out_reason(f["path"])
            if why:
                left.setdefault(f["path"], [org, repo, f["path"], e, why, 0])
                continue
            head.add(f["path"])
            by_path.setdefault(f["path"], [])
    shutil.rmtree(os.path.join(cfg.out, org, repo), ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)
    rows, read_as, by_type, status, error = [], Counter(), {}, "ok", ""
    fh = part = None
    try:
        for path in sorted(by_path):
            ext = ext_key(path)
            vers = sorted(by_path[path], key=lambda r: (r["first_date"], r["blob"]))
            sources, notes = [], Counter()
            for r in vers:
                if r["action"] in STORED and r["stored_as"]:
                    sources.append(os.path.join(cfg.src, r["stored_as"]))
                else:
                    notes[r["action"]] += 1
            active = ""
            if cfg.active_root and not cfg.inner:
                ap = os.path.join(cfg.active_root, org, repo, *path.split("/"))
                if os.path.isfile(ap):
                    sources.append(ap)
                    active = "yes" if vers else "yes_only_today"
                else:
                    active = "no_file_today"
            dest = out_txt(cfg.out, org, repo, path)
            part = dest + ".part"
            seen, nlines, nread, nbytes, how_all = set(), 0, 0, 0, Counter()
            for fp in sources:
                size = os.path.getsize(fp)
                if cfg.max_bytes and size > cfg.max_bytes:
                    notes["too_large"] += 1
                    continue
                with open(fp, "rb") as f:
                    data = f.read()
                strs, rd = strings_of(data, ext)
                for s in strs:
                    for line in s.splitlines():
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
                del data
                how = rd.how()
                how_all[how] += 1
                read_as[how] += 1
                nread += 1
                nbytes += size
            if fh is not None:
                fh.close()
                fh = None
                os.replace(part, dest)
            t = by_type.setdefault(ext, [0, 0, 0, 0])   # paths written lines bytes
            t[0] += 1
            t[1] += 1 if nlines else 0
            t[2] += nlines
            t[3] += nbytes
            rows.append([path, ext, len(vers), nread, active,
                         ";".join("%s:%d" % kv for kv in sorted(how_all.items())),
                         nlines, nbytes,
                         ";".join("%s:%d" % kv for kv in sorted(notes.items()))
                         + ("" if nlines else (";" if notes else "") + "no_text")])
    except Exception as exc:                         # noqa: BLE001 - one repo
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
        if fh is not None:                           # no half file left behind
            fh.close()
            os.remove(part)
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"),
                 csv_text([MANIFEST_HEADER] + rows))
    write_atomic(os.path.join(sd, "left_out.csv"),
                 csv_text([LEFT_OUT_HEADER] + list(left.values())))
    rec.update(status=status, seconds=round(time.time() - t0, 1),
               paths=len(rows), files_written=sum(1 for r in rows if r[6]),
               lines=sum(r[6] for r in rows), read_as=dict(read_as),
               by_type=by_type, left_out_paths=len(left), error=error)
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# summary and main
# --------------------------------------------------------------------------

def combine(out):
    state = os.path.join(out, STATE)
    tot, reads, repos, types = Counter(), Counter(), 0, {}
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
            for k in ("paths", "files_written", "lines", "left_out_paths"):
                tot[k] += d.get(k, 0)
            for e, v in d.get("by_type", {}).items():
                t = types.setdefault(e, [0, 0, 0, 0, 0])
                for i in range(4):
                    t[i] += v[i]
                t[4] += 1
            per_repo.append([org, repo, d["paths"], d["files_written"], d["lines"],
                             ";".join("%s:%d" % kv for kv in sorted(d["read_as"].items())),
                             d.get("left_out_paths", 0)])
    with open(os.path.join(out, "summary.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["org", "repo", "paths", "txt_files_written", "lines",
                    "read_as", "left_out_paths"])
        w.writerows(per_repo)
        w.writerow([])
        w.writerow(["total", "", tot["paths"], tot["files_written"], tot["lines"],
                    ";".join("%s:%d" % kv for kv in sorted(reads.items())),
                    tot["left_out_paths"]])
        w.writerow([])
        w.writerow(["column", "meaning"])
        w.writerows([
            ("paths", "different paths of the chosen types (history + today)"),
            ("txt_files_written", "paths that gave any text (one .txt each)"),
            ("lines", "lines written, each once per path"),
            ("read_as", "stored files / today's files by how they were read "
             "(see data_to_text.py's help); *_raw_scan = bytes scanned like "
             "`strings` only; *_no_<library>_raw_scan = install it and redo"),
            ("left_out_paths", "vendored / macOS / DocFX cache paths left out "
             "(left_out.csv per repo)")])
    with open(os.path.join(out, "by_type.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["type", "repos", "paths", "txt_files_written", "lines", "bytes_read"])
        for e, t in sorted(types.items(), key=lambda kv: -kv[1][1]):
            w.writerow([e, t[4], t[0], t[1], t[2], t[3]])
        w.writerow(["total", repos, tot["paths"], tot["files_written"], tot["lines"],
                    sum(t[3] for t in types.values())])
    return repos, tot


def check_libs():
    for m in OPTIONAL:
        x = lib(m)
        print("%-11s %s" % (m, ("yes " + getattr(x, "__version__", "")) if x else "no"))
    print("needed for: pyarrow = parquet orc arrow feather (else raw scan); "
          "zstandard / lz4 = zstd / lz4 compressed files, lz4 also nippy; "
          "fastavro = avro; pyreadstat (+ pandas) = SPSS sav")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src",
                    help="extract_versions.py --per-repo output folder "
                         "(or archive_unpack.py output with --archive-inner)")
    ap.add_argument("--out", help="output folder")
    ap.add_argument("--extensions", help="comma list or file (default the 17: %s)"
                    % ",".join(DEFAULT_EXTS))
    ap.add_argument("--batch", action="append", default=[], metavar="CSV")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO")
    ap.add_argument("--active-root", help="include today's files from here")
    ap.add_argument("--state", help="extension_versions.py output: with "
                    "--active-root every path at head is read, also those "
                    "with no stored version")
    ap.add_argument("--max-bytes", type=int, default=2_000_000_000,
                    help="skip files (and zip members) bigger than this (default 2 GB)")
    ap.add_argument("--archive-inner", action="store_true",
                    help="--in is archive_unpack.py output: files inside archives")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--redo", action="store_true", help="redo repos already done")
    ap.add_argument("--strict-raw", action="store_true",
                    help="keep only raw-scan runs that look like text")
    ap.add_argument("--combine-only", action="store_true")
    ap.add_argument("--check-libs", action="store_true",
                    help="show which optional libraries are installed")
    ap.add_argument("--name", help="log name (default: batch name or 'run')")
    args = ap.parse_args()

    if args.check_libs:
        check_libs()
        return 0
    if not args.out:
        sys.exit("--out is needed")
    if args.combine_only:
        repos, tot = combine(args.out)
        print("combined %d repo(s): %s" % (repos, dict(tot)))
        return 0
    if not args.src:
        sys.exit("--in is needed")
    if os.path.realpath(args.out) == os.path.realpath(args.src):
        sys.exit("--out must be a different folder from --in")
    if not os.path.isdir(os.path.join(args.src, STATE)):
        sys.exit("no _state folder under " + args.src)
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
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(args.out, LOGS, name + ".log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)

    cfg = SimpleNamespace(src=args.src, out=args.out, active_root=args.active_root,
                          state=args.state, max_bytes=args.max_bytes,
                          inner=args.archive_inner, exts=exts, strict_raw=args.strict_raw)
    log.info("START %s repos=%d workers=%d in=%s active=%s state=%s types=%s libs=%s "
             "raw=%s", name, len(todo), args.workers, args.src, args.active_root or "-",
             args.state or "-", ",".join(sorted(exts)),
             ",".join(m for m in OPTIONAL if lib(m)) or "none",
             "text-like only" if args.strict_raw else "all")
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
            if rec["status"] not in ("ok", "no_extraction"):
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
