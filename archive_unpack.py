#!/usr/bin/env python3
"""
archive_unpack.py - group 4.2: open every version of every archive (zip,
tar, gz, 7z, rar, ...) in a repo, today's copy included, and write out every
file inside - each distinct content ONCE per repo - with a manifest of every
occurrence and stats by type. A first, neutral step: what is inside, how
much, of which type; the later steps run the earlier groups' methods on it.

Input
  --in       extract_versions.py --per-repo output for the archive types:
             _state/<org>/<repo>/manifest.csv (every history version not in
             today's copy) + files/<ext>/<org>/<repo>/<blob>.<ext>
  --counts   extension_versions.py output for the same types: its files.csv
             says which archives exist TODAY (at_head = yes) - with
             --active-root they are read too, since a scanner cannot look
             inside an archive: today's copy was not really scanned
  vendored archives (node_modules, packages, bin, obj...) are only listed

Per repo, archive versions oldest first (today's last):
  * opened by their bytes, not their name: zip (zipfile), tar / tgz
    (tarfile), gz / bz2 / xz / lzma (one file inside, or a tar), zst
    (zstandard), lz4 (lz4.frame), 7z / rar / cab / zipx / .Z and zips
    Python cannot read (the 7z tool, one member at a time to stdout - 7z
    never writes files itself)
  * every regular file inside is streamed, hashed (sha256) and written as
    files/<type>/<org>/<repo>/<sha256>.<type> - only the first time that
    content is seen in the repo; later occurrences are manifest rows
    (action deduped). Names inside an archive are never used to write
  * an archive inside an archive (by type: zip, tar, gz, 7z, ...) is opened
    the same way, up to --max-depth levels. Office files (docx, xlsx...)
    are zips too but are files, never opened
  * not opened, listed with the reason: password-protected, damaged, no
    tool for the format, links / devices, over the limits (one file
    > --max-member-bytes; one archive unpacking to > --max-archive-bytes
    or > --max-ratio x its size; > --max-members files)

Output, under --out:
  files/<type>/<org>/<repo>/<sha256>.<type>   each distinct content once
  _state/<org>/<repo>/manifest.csv   one row per file inside per archive
      version, in extract_versions.py's columns (path = <archive path>/
      <path inside>, blob = sha256, action written / deduped / why not)
      + archive_path, archive_blob, archive_source, inner_path, depth, route
      - so pdf_page_merge.py and pkl_to_text.py run on --out as it is
  _state/<org>/<repo>/archives.csv   one row per archive version opened
  _state/<org>/<repo>/done.json      written last (resume)
  stats/by_type.csv     per type inside: route (which group), occurrences,
                        distinct files, size, repos
  stats/by_archive_type.csv, stats/by_repo.csv, stats/problems.csv,
  stats/legend.csv
  _logs/<name>.log, <name>_repos.csv

Exit codes: 0 done, 1 some repos failed, 3 stopped (disk below
--min-free-disk-gb, or the active copy not answering) - run again.

Usage:
    python3 archive_unpack.py --in /data/workarea/group4_2/4_2_extract \\
        --counts /data/workarea/group4_2/counts --out /data/workarea/group4_2/unpacked \\
        --active-root /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos \\
        --batch /data/workarea/group4_2/batches/M42.csv --workers 8
"""

import argparse
import bz2
import csv
import datetime
import gzip
import hashlib
import io
import json
import logging
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from types import SimpleNamespace

from extension_versions import (BINARY_EXTS, TEXT_EXTS, read_batch, read_json,
                                read_rows)
from file_delta import csv_text, write_atomic
from repo_extension_summary import EXTENSIONS, ext_key, load_extensions

HERE = os.path.dirname(os.path.abspath(__file__))
STATE, LOGS = "_state", "_logs"
STORED = ("written", "deduped")
CHUNK = 1 << 20
# category A of group 4.2: opened. B (packages) and C (assets) are files.
ARCHIVE_EXTS = {"zip", "gz", "gzip", "tar", "tgz", "bz2", "xz", "z", "lzma",
                "7z", "rar", "zst", "lz4", "zipx", "cab"}
PACKAGE_EXTS = {"nupkg", "snupkg", "jar", "war", "whl", "egg", "aar", "apk",
                "aab", "ipa", "xpi", "crx", "deb", "rpm", "phar", "msix",
                "intunewin", "dmg", "pkg", "pak", "pck", "img", "bundle",
                "unitypackage"}
MANIFEST_HEADER = ["org", "repo", "path", "ext", "blob", "bytes", "action",
                   "stored_as", "why", "vendored", "first_commit", "first_date",
                   "last_commit", "last_date", "archive_path", "archive_blob",
                   "archive_source", "inner_path", "depth", "route"]
ARCHIVES_HEADER = ["archive_path", "archive_blob", "archive_source", "bytes",
                   "format", "status", "members", "written", "deduped",
                   "not_stored", "bytes_unpacked", "error"]
log = logging.getLogger("archive_unpack")


class DiskLow(RuntimeError):
    pass


class Limit(RuntimeError):
    pass


# --------------------------------------------------------------------------
# what a type is
# --------------------------------------------------------------------------

def load_set(name):
    p = os.path.join(HERE, name)
    return set(load_extensions(p)) if os.path.isfile(p) else set()


ROUTES = None


def route_of(ext):
    """Which group's method a type inside an archive belongs to."""
    global ROUTES
    if ROUTES is None:
        ROUTES = [("group1_text", set(EXTENSIONS)),
                  ("group2_text", set(TEXT_EXTS)),
                  ("group2_binary", set(BINARY_EXTS)),
                  ("group3", {"pdf", "pkl"}),
                  ("group4_1_db", load_set("group4_1_extensions.txt")),
                  ("group4_5_text", load_set(os.path.join(
                      "handover", "text_rest_kit", "text_extensions.txt"))),
                  ("archive", ARCHIVE_EXTS),
                  ("package_or_asset", PACKAGE_EXTS)]
    for name, s in ROUTES:
        if ext in s:
            return name
    return "no_extension" if not ext else "other"


def detect(fp):
    """Format by magic bytes: zip, tar, gz, bz2, xz, zst, lz4, 7z, rar, cab,
    Z, or '' (not an archive we know)."""
    with open(fp, "rb") as fh:
        head = fh.read(512)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zst"
    if head[:4] == b"\x04\x22\x4d\x18":
        return "lz4"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:4] == b"MSCF":
        return "cab"
    if head[:2] in (b"\x1f\x9d", b"\x1f\xa0"):
        return "Z"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    if head[:1] == b"\x5d" and head[1:5] != b"\x00\x00\x00\x00":
        return "lzma"                                # LZMA alone (best effort)
    return ""


def safe_type(ext):
    e = re.sub(r"[^a-z0-9_+-]", "", (ext or "").lower())[:24]
    return e or "_noext"


def clean_name(name):
    """A name from inside an archive, for the manifest only."""
    n = (name or "").replace("\\", "/")
    n = re.sub(r"^[a-zA-Z]:", "", n).lstrip("/")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    return "/".join(parts) or "(unnamed)"


# --------------------------------------------------------------------------
# reading archives: each opener yields Member(name, size, kind, opener)
# --------------------------------------------------------------------------

def free_gb(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def sevenzip():
    for c in (os.environ.get("SEVENZIP"), "7zz", "7z", "7za"):
        if c and shutil.which(c):
            return shutil.which(c)
    return None


def members_zip(fp):
    zf = zipfile.ZipFile(fp)
    for zi in zf.infolist():
        if zi.is_dir():
            continue
        if zi.flag_bits & 0x1:
            yield zi.filename, zi.file_size, "encrypted", None
            continue
        mode = (zi.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            yield zi.filename, zi.file_size, "link", None
            continue
        yield zi.filename, zi.file_size, "file", (lambda zi=zi: zf.open(zi))


def members_tar(fp):
    tf = tarfile.open(fp, mode="r:*")
    for ti in tf:
        if ti.isdir():
            continue
        if not ti.isfile():
            yield ti.name, ti.size, "link", None
            continue
        yield ti.name, ti.size, "file", (lambda ti=ti: tf.extractfile(ti))


def single_opener(fmt, fp):
    if fmt == "gz":
        return lambda: gzip.open(fp, "rb")
    if fmt == "bz2":
        return lambda: bz2.open(fp, "rb")
    if fmt in ("xz", "lzma"):
        return lambda: lzma.open(fp, "rb")
    if fmt == "zst":
        import zstandard                              # pip install zstandard
        return lambda: zstandard.ZstdDecompressor().stream_reader(open(fp, "rb"))
    if fmt == "lz4":
        import lz4.frame                              # pip install lz4
        return lambda: lz4.frame.open(fp, "rb")
    raise ValueError(fmt)


def members_7z(fp, tool):
    """List with 7z -slt, then each member through 7z x -so (7z writes no
    files)."""
    p = subprocess.run([tool, "l", "-slt", "-p", fp], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=600)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0 or "----------" not in out:
        err = (p.stderr.decode("utf-8", "replace") + out)[-300:]
        if "assword" in err or "ncrypted" in err:
            raise PermissionError("encrypted")
        raise ValueError("7z cannot list it: " + " ".join(err.split())[-200:])
    blocks = out.split("----------", 1)[1].split("\n\n")
    for b in blocks:
        kv = dict(line.split(" = ", 1) for line in b.strip().splitlines()
                  if " = " in line)
        if "Path" not in kv:
            continue
        name = kv["Path"]
        if kv.get("Folder") == "+" or kv.get("Attributes", "").startswith("D"):
            continue
        size = int(kv.get("Size") or 0)
        if kv.get("Encrypted") == "+":
            yield name, size, "encrypted", None
            continue
        if kv.get("Symbolic Link") or "l" in kv.get("Attributes", "")[:1]:
            yield name, size, "link", None
            continue

        def opener(name=name):
            proc = subprocess.Popen([tool, "x", "-so", "-p", fp, name],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL)
            return Proc(proc)
        yield name, size, "file", opener


class Proc(io.RawIOBase):
    """A 7z -so process as a readable file; non-zero exit = error."""
    def __init__(self, proc):
        self.proc = proc

    def readable(self):
        return True

    def read(self, n=-1):
        return self.proc.stdout.read(n)

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdout.read()
        rc = self.proc.wait()
        self.proc.stdout.close()
        super().close()
        if rc not in (0, None):
            raise IOError("7z exited %d" % rc)


def open_archive(fp, cfg):
    """-> (format, iterator of (name, size, kind, opener)) for an archive,
    or raises. Single-file compression yields one member, which the caller
    opens again when it is a tar."""
    fmt = detect(fp)
    if fmt == "zip":
        try:
            zf = zipfile.ZipFile(fp)
            methods = {zi.compress_type for zi in zf.infolist()}
            zf.close()
            if methods <= {0, 8, 12, 14} or not cfg.sevenzip:
                return "zip", members_zip(fp)
        except zipfile.BadZipFile:
            if not cfg.sevenzip:
                raise
        return "zip(7z)", members_7z(fp, cfg.sevenzip)
    if fmt == "tar":
        return "tar", members_tar(fp)
    if fmt in ("gz", "bz2", "xz", "lzma", "zst", "lz4"):
        if fmt in ("gz", "bz2", "xz") and tarfile.is_tarfile(fp):
            try:
                return "tar." + fmt, members_tar(fp)
            except tarfile.TarError:
                pass
        return fmt, iter([("", None, "single", single_opener(fmt, fp))])
    if fmt in ("7z", "rar", "cab", "Z"):
        if not cfg.sevenzip:
            raise LookupError("no_tool")
        return fmt, members_7z(fp, cfg.sevenzip)
    raise ValueError("not_an_archive")


# --------------------------------------------------------------------------
# per repo
# --------------------------------------------------------------------------

class Repo:
    """Writes the distinct contents of one repo and its manifest rows."""

    def __init__(self, cfg, org, repo):
        self.cfg, self.org, self.repo = cfg, org, repo
        self.seen = {}                      # sha256 -> stored_as
        self.rows, self.archives = [], []
        self.tmpdir = tempfile.mkdtemp(prefix=".unpack_", dir=cfg.out)

    def close(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def stream_to_tmp(self, src, cap):
        """Copy a stream to a temp file, hashing; -> (tmp, size, sha256).
        Raises Limit past `cap` bytes."""
        fd, tmp = tempfile.mkstemp(dir=self.tmpdir)
        h, n = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > cap:
                        raise Limit("over %d bytes" % cap)
                    h.update(chunk)
                    out.write(chunk)
        except BaseException:
            os.remove(tmp)
            raise
        return tmp, n, h.hexdigest()

    def keep(self, tmp, sha, ext):
        """-> (action, stored_as): the first copy of a content is moved into
        files/, later ones are dropped."""
        if sha in self.seen:
            os.remove(tmp)
            return "deduped", self.seen[sha]
        if self.cfg.min_free_gb and free_gb(self.cfg.out) < self.cfg.min_free_gb:
            os.remove(tmp)
            raise DiskLow("less than %s GB free on %s"
                          % (self.cfg.min_free_gb, self.cfg.out))
        t = safe_type(ext)
        rel = os.path.join("files", t, self.org, self.repo,
                           sha + ("" if t == "_noext" else "." + t))
        dest = os.path.join(self.cfg.out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(tmp, dest)
        self.seen[sha] = rel
        return "written", rel

    def row(self, av, path, inner, depth, action, blob="", size="", stored=""):
        e = ext_key(inner) if inner else ""
        self.rows.append([self.org, self.repo, path, e, blob, size, action,
                          stored, av["why"], av["vendored"], av["first_commit"],
                          av["first_date"], av["last_commit"], av["last_date"],
                          av["path"], av["blob"], av["source"], inner, depth,
                          route_of(e) if inner else ""])

    def unpack(self, av, fp, prefix, depth, budget):
        """Open archive file `fp` (an archive version or a nested one) and
        record / keep every file in it. -> (format, status, counts)."""
        cfg = self.cfg
        cnt = Counter()
        try:
            fmt, members = open_archive(fp, cfg)
        except LookupError:
            return "", "no_tool", cnt
        except PermissionError:
            return "", "encrypted", cnt
        except ValueError as exc:
            return "", ("not_an_archive" if "not_an_archive" in str(exc)
                        else "damaged: %s" % str(exc)[:150]), cnt
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError,
                lzma.LZMAError) as exc:
            return "", "damaged: %s" % str(exc)[:150], cnt
        except ImportError as exc:
            return "", "no_tool: %s" % exc, cnt
        status = "ok"
        try:
            for name, size, kind, opener in members:
                if kind == "single":                  # data.csv.gz -> data.csv
                    base = os.path.basename(prefix.rstrip("/"))
                    name = base.rsplit(".", 1)[0] if "." in base else base + ".out"
                inner = clean_name(name)
                path = prefix + inner
                cnt["members"] += 1
                if cnt["members"] > cfg.max_members:
                    self.row(av, path, inner, depth, "limit_members")
                    cnt["not_stored"] += 1
                    status = "limit_members"
                    break
                if kind != "file" and kind != "single":
                    self.row(av, path, inner, depth, kind, size=size or "")
                    cnt["not_stored"] += 1
                    continue
                if size is not None and size > cfg.max_member_bytes:
                    self.row(av, path, inner, depth, "too_large", size=size)
                    cnt["not_stored"] += 1
                    continue
                cap = min(cfg.max_member_bytes, budget["left"])
                try:
                    src = opener()
                    try:
                        tmp, n, sha = self.stream_to_tmp(src, cap)
                    finally:
                        src.close()
                except Limit:
                    over = budget["left"] < cfg.max_member_bytes
                    self.row(av, path, inner, depth,
                             "limit_archive_bytes" if over else "too_large")
                    cnt["not_stored"] += 1
                    if over:
                        status = "limit_archive_bytes"
                        break
                    continue
                except DiskLow:
                    raise
                except (RuntimeError, NotImplementedError) as exc:
                    act = "encrypted" if "password" in str(exc).lower() \
                        or "encrypted" in str(exc).lower() else "error"
                    self.row(av, path, inner, depth, act)
                    cnt["not_stored"] += 1
                    continue
                except (OSError, EOFError, zipfile.BadZipFile, tarfile.TarError,
                        lzma.LZMAError, ValueError) as exc:
                    self.row(av, path, inner, depth, "error")
                    cnt["not_stored"] += 1
                    log.warning("member error %s/%s %s: %s", self.org, self.repo,
                                path, exc)
                    continue
                budget["left"] -= n
                cnt["bytes"] += n
                e = ext_key(inner)
                nested = e in ARCHIVE_EXTS or (kind == "single" and detect(tmp) == "tar")
                if nested and depth < cfg.max_depth:
                    if kind == "single":              # a.tar.gz: one level
                        fmt2, st2, c2 = self.unpack(av, tmp, prefix, depth, budget)
                    else:
                        fmt2, st2, c2 = self.unpack(av, tmp, path + "/", depth + 1,
                                                    budget)
                    if st2 == "ok":
                        os.remove(tmp)
                        cnt.update(c2)
                        if kind != "single":
                            self.row(av, path, inner, depth, "opened_nested",
                                     sha, n)
                        continue
                    # not openable as an archive: keep it as a file
                if nested and depth >= cfg.max_depth:
                    status = "too_deep" if status == "ok" else status
                action, stored = self.keep(tmp, sha, e)
                cnt[action] += 1
                self.row(av, path, inner, depth, action, sha, n, stored)
                if budget["left"] <= 0:
                    status = "limit_archive_bytes"
                    break
        except DiskLow:
            raise
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError,
                lzma.LZMAError, ValueError, subprocess.SubprocessError) as exc:
            status = "damaged: %s" % str(exc)[:150]
        return fmt, status, cnt


def archive_versions(cfg, org, repo):
    """-> list of archive versions (dicts) oldest first, today's last, or
    None when the repo's extraction did not finish."""
    sd = os.path.join(cfg.src, STATE, org, repo)
    done = read_json(os.path.join(sd, "done.json"))
    if not done or done.get("status") != "ok":
        return None
    out, blobs = [], set()
    for r in read_rows(os.path.join(sd, "manifest.csv")):
        if ext_key(r["path"]) not in cfg.exts:
            continue
        blobs.add(r["blob"])
        av = dict(path=r["path"], blob=r["blob"], source="history",
                  why=r["why"], vendored=r["vendored"],
                  first_commit=r["first_commit"], first_date=r["first_date"],
                  last_commit=r["last_commit"], last_date=r["last_date"],
                  action=r["action"], file=None)
        if r["action"] in STORED and r["stored_as"]:
            av["file"] = os.path.join(cfg.src, r["stored_as"])
        out.append(av)
    out.sort(key=lambda a: (a["first_date"], a["path"], a["blob"]))
    if cfg.active_root and cfg.counts:
        files = os.path.join(cfg.counts, STATE, org, repo, "files.csv")
        today = []
        for f in read_rows(files) if os.path.exists(files) else []:
            if f["at_head"] != "yes" or f["ext"] not in cfg.exts:
                continue
            fp = os.path.join(cfg.active_root, org, repo, *f["path"].split("/"))
            av = dict(path=f["path"], blob="", source="today", why="today",
                      vendored=f["vendored"], first_commit="", first_date="today",
                      last_commit="", last_date="", action="", file=fp)
            if f["vendored"] and cfg.skip_vendored:
                av["action"] = "skipped_vendored"
                av["file"] = None
            elif not os.path.isfile(fp):
                av["action"] = "missing_today"
                av["file"] = None
            else:
                h = hashlib.sha1(b"blob %d\0" % os.path.getsize(fp))
                with open(fp, "rb") as fh:
                    for c in iter(lambda: fh.read(CHUNK), b""):
                        h.update(c)
                av["blob"] = h.hexdigest()
                if av["blob"] in blobs:
                    continue                     # the same bytes as a version
            today.append(av)
        out += sorted(today, key=lambda a: a["path"])
    return out


def run_repo(cfg, org, repo):
    t0 = time.time()
    sd = os.path.join(cfg.out, STATE, org, repo)
    rec = {"org": org, "repo": repo, "status": "no_extraction",
           "finished": datetime.datetime.now().isoformat(timespec="seconds"),
           "seconds": 0, "archive_versions": 0, "opened": 0, "members": 0,
           "written": 0, "deduped": 0, "not_stored": 0, "bytes_written": 0,
           "error": ""}
    avs = archive_versions(cfg, org, repo)
    if avs is None:
        return rec
    for t in os.listdir(os.path.join(cfg.out, "files")) \
            if os.path.isdir(os.path.join(cfg.out, "files")) else []:
        shutil.rmtree(os.path.join(cfg.out, "files", t, org, repo),
                      ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)
    R = Repo(cfg, org, repo)
    status, error = "ok", ""
    try:
        for av in avs:
            rec["archive_versions"] += 1
            if not av["file"]:
                R.row(av, av["path"] + "/", "", 0, av["action"] or "not_extracted")
                R.archives.append([av["path"], av["blob"], av["source"], "", "",
                                   av["action"] or "not_extracted", 0, 0, 0, 0, 0, ""])
                continue
            size = os.path.getsize(av["file"])
            # what this version may unpack in all, nested archives included
            budget = {"left": min(cfg.max_archive_bytes,
                                  max(size * cfg.max_ratio, cfg.ratio_floor))}
            fmt, st, c = R.unpack(av, av["file"], av["path"] + "/", 1, budget)
            if st != "ok" and not c["members"]:
                R.row(av, av["path"] + "/", "", 0,
                      st.split(":")[0])
            rec["opened"] += st == "ok" or bool(c["members"])
            for k in ("members", "written", "deduped", "not_stored"):
                rec[k] += c[k]
            rec["bytes_written"] += c["bytes"] if c["written"] else 0
            R.archives.append([av["path"], av["blob"], av["source"], size, fmt, st,
                               c["members"], c["written"], c["deduped"],
                               c["not_stored"], c["bytes"], ""])
    except DiskLow:
        R.close()
        raise
    except Exception as exc:                          # noqa: BLE001 - one repo
        status, error = "error", "%s: %s" % (type(exc).__name__, exc)
    finally:
        R.close()
    os.makedirs(sd, exist_ok=True)
    write_atomic(os.path.join(sd, "manifest.csv"), csv_text([MANIFEST_HEADER] + R.rows))
    write_atomic(os.path.join(sd, "archives.csv"), csv_text([ARCHIVES_HEADER] + R.archives))
    rec["bytes_written"] = sum(os.path.getsize(os.path.join(cfg.out, s))
                               for s in R.seen.values())
    rec.update(status=status, error=error, seconds=round(time.time() - t0, 1))
    write_atomic(os.path.join(sd, "done.json"), json.dumps(rec, indent=1))
    return rec


# --------------------------------------------------------------------------
# stats and main
# --------------------------------------------------------------------------

LEGEND = [
    ("by_type.csv", "one row per type of file found inside the archives"),
    ("  type", "extension of the file inside (lower case; _noext = none)"),
    ("  route", "which earlier group's method fits it: group1_text, "
                "group2_text, group2_binary, group3 (pdf, pkl), group4_1_db, "
                "group4_5_text, archive (nested, opened), package_or_asset, "
                "other, no_extension"),
    ("  occurrences", "times it appears inside an archive version"),
    ("  distinct", "different contents, counted once per repo: the files "
                   "written to files/"),
    ("  gb_distinct", "size of those files"),
    ("  not_stored", "occurrences not written: encrypted, link, too large, "
                     "error (see manifest action)"),
    ("  repos", "repos where the type was found"),
    ("by_archive_type.csv", "per archive type: versions, opened, files inside, "
                            "and why the rest could not be opened"),
    ("by_repo.csv", "per repo: archive versions, files inside, written, "
                    "deduped, GB"),
    ("problems.csv", "every archive version not opened (or opened only in "
                     "part), with the reason"),
]


def combine(out):
    state = os.path.join(out, STATE)
    by_type = defaultdict(Counter)
    type_repos = defaultdict(set)
    by_arch = defaultdict(Counter)
    per_repo, problems, tot = [], [], Counter()
    for org in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        for repo in sorted(os.listdir(os.path.join(state, org))):
            sd = os.path.join(state, org, repo)
            d = read_json(os.path.join(sd, "done.json"))
            if not d:
                continue
            tot["repos_" + d["status"]] += 1
            if d["status"] != "ok":
                continue
            per_repo.append([org, repo, d["archive_versions"], d["opened"],
                             d["members"], d["written"], d["deduped"],
                             d["not_stored"], round(d["bytes_written"] / 1e9, 3)])
            for r in read_rows(os.path.join(sd, "manifest.csv")):
                if not r["inner_path"]:
                    continue
                t = safe_type(r["ext"])
                c = by_type[t]
                c["route:" + r["route"]] = 1
                if r["action"] == "opened_nested":
                    c["opened_nested"] += 1
                    continue
                c["occurrences"] += 1
                if r["action"] == "written":
                    c["distinct"] += 1
                    c["bytes"] += int(r["bytes"] or 0)
                elif r["action"] != "deduped":
                    c["not_stored"] += 1
                type_repos[t].add((org, repo))
            for a in read_rows(os.path.join(sd, "archives.csv")):
                e = ext_key(a["archive_path"])
                st = a["status"].split(":")[0]
                ca = by_arch[e]
                ca["versions"] += 1
                ca["status:" + st] += 1
                ca["members"] += int(a["members"] or 0)
                if st != "ok":
                    problems.append([org, repo, a["archive_path"], a["archive_blob"],
                                     a["archive_source"], a["format"], a["status"],
                                     a["members"]])
    sd = os.path.join(out, "stats")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "by_type.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "route", "occurrences", "distinct", "gb_distinct",
                    "not_stored", "opened_nested", "repos"])
        for t in sorted(by_type, key=lambda t: (-by_type[t]["occurrences"], t)):
            c = by_type[t]
            route = next((k[6:] for k in c if k.startswith("route:")), "")
            w.writerow([t, route, c["occurrences"], c["distinct"],
                        round(c["bytes"] / 1e9, 3), c["not_stored"],
                        c["opened_nested"], len(type_repos[t])])
            for k in ("occurrences", "distinct", "bytes", "not_stored"):
                tot[k] += c[k]
        w.writerow(["total", "", tot["occurrences"], tot["distinct"],
                    round(tot["bytes"] / 1e9, 3), tot["not_stored"], "", ""])
    sts = sorted({k[7:] for c in by_arch.values() for k in c if k.startswith("status:")})
    with open(os.path.join(sd, "by_archive_type.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["archive_type", "versions", "files_inside"] + sts)
        for e in sorted(by_arch, key=lambda e: -by_arch[e]["versions"]):
            c = by_arch[e]
            w.writerow([e, c["versions"], c["members"]] + [c["status:" + s] for s in sts])
    with open(os.path.join(sd, "by_repo.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "archive_versions", "opened", "files_inside",
                    "written", "deduped", "not_stored", "gb_written"])
        w.writerows(per_repo)
    with open(os.path.join(sd, "problems.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "archive_path", "archive_blob", "source",
                    "format", "status", "files_read_before"])
        w.writerows(problems)
    with open(os.path.join(sd, "legend.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file / column", "meaning"])
        w.writerows(LEGEND)
    return len(per_repo), tot, len(problems)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src", required=True,
                    help="extract_versions.py --per-repo output (archive types)")
    ap.add_argument("--counts", help="extension_versions.py output (for today's files)")
    ap.add_argument("--active-root", help="today's copy: its archives are read too")
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", action="append", default=[], metavar="CSV")
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO")
    ap.add_argument("--extensions", default=",".join(sorted(ARCHIVE_EXTS)),
                    help="archive types to open (default: the 15 of category A)")
    ap.add_argument("--include-vendored", action="store_true",
                    help="also open today's vendored archives (default: listed)")
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--max-member-bytes", type=int, default=2_000_000_000)
    ap.add_argument("--max-archive-bytes", type=int, default=5_000_000_000,
                    help="stop an archive (and what is nested in it) after this "
                         "much unpacked (default 5 GB)")
    ap.add_argument("--max-ratio", type=int, default=200,
                    help="... or after this many times its own size")
    ap.add_argument("--ratio-floor", type=int, default=100_000_000,
                    help="the ratio limit never stops before this (100 MB)")
    ap.add_argument("--max-members", type=int, default=100_000)
    ap.add_argument("--min-free-disk-gb", type=float, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--combine-only", action="store_true")
    ap.add_argument("--name")
    args = ap.parse_args()

    if args.combine_only:
        n, tot, prob = combine(args.out)
        print("combined %d repo(s): %s, %d problem archive(s)" % (n, dict(tot), prob))
        return 0
    if os.path.realpath(args.out) == os.path.realpath(args.src):
        sys.exit("--out must be a different folder from --in")
    if not os.path.isdir(os.path.join(args.src, STATE)):
        sys.exit("no _state folder under " + args.src)
    if args.active_root and not args.counts:
        sys.exit("--active-root needs --counts (which archives exist today)")
    os.makedirs(os.path.join(args.out, LOGS), exist_ok=True)
    for d in os.listdir(args.out):                  # temp dirs of a killed run
        if d.startswith(".unpack_"):
            shutil.rmtree(os.path.join(args.out, d), ignore_errors=True)
    if args.active_root:
        try:
            os.listdir(args.active_root)
        except OSError as exc:
            print("STOPPED: the active copy is not answering (%s) - remount it "
                  "and run the same command again" % exc, file=sys.stderr)
            return 3

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
    cfg = SimpleNamespace(
        src=args.src, out=args.out, counts=args.counts, active_root=args.active_root,
        exts=set(load_extensions(args.extensions)), skip_vendored=not args.include_vendored,
        max_depth=args.max_depth, max_member_bytes=args.max_member_bytes,
        max_archive_bytes=args.max_archive_bytes, max_ratio=args.max_ratio,
        ratio_floor=args.ratio_floor, max_members=args.max_members,
        min_free_gb=args.min_free_disk_gb, sevenzip=sevenzip())
    missing = [m for m, mod in (("zstandard", "zstandard"), ("lz4", "lz4.frame"))
               if not _has(mod)]
    log.info("START %s repos=%d workers=%d 7z=%s missing=%s", name, len(todo),
             args.workers, cfg.sevenzip or "-", missing or "-")
    print("%d repo(s) to do | 7z: %s%s" % (
        len(todo), cfg.sevenzip or "NOT FOUND (7z/rar/cab/.Z listed only)",
        " | missing: %s (those files listed only)" % ", ".join(missing)
        if missing else ""), flush=True)
    t0, st, stopped = time.time(), Counter(), ""
    rpath = os.path.join(args.out, LOGS, name + "_repos.csv")
    new = not os.path.exists(rpath)
    cols = ["archive_versions", "opened", "members", "written", "deduped",
            "not_stored", "bytes_written"]
    with open(rpath, "a", newline="", encoding="utf-8") as rf, \
            ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rw = csv.writer(rf)
        if new:
            rw.writerow(["finished", "org", "repo", "status", "seconds"] + cols
                        + ["error"])
        futs = {pool.submit(run_repo, cfg, o, r): (o, r) for o, r in todo}
        for fut in as_completed(futs):
            o, r = futs[fut]
            try:
                rec = fut.result()
            except DiskLow as exc:
                st["disk_low"] += 1
                if not stopped:
                    stopped = str(exc)
                    log.error("STOP   %s - cancelling the repos not started", exc)
                    for f in futs:
                        f.cancel()
                continue
            except Exception as exc:                  # noqa: BLE001
                st["failed"] += 1
                log.error("FAIL   %s/%s %s", o, r, exc)
                continue
            st[rec["status"]] += 1
            rw.writerow([rec["finished"], o, r, rec["status"], rec["seconds"]]
                        + [rec[c] for c in cols] + [rec["error"]])
            rf.flush()
            if rec["status"] not in ("ok", "no_extraction"):
                log.error("%s %s/%s %s", rec["status"].upper(), o, r, rec["error"])
    n, tot, prob = combine(args.out)
    msg = ("END %s: %s | %s repo(s): files inside %s, distinct written %s "
           "(%.2f GB), not stored %s, problem archives %s | %.0fs" % (
               name, dict(st), f"{n:,}", f"{tot['occurrences']:,}",
               f"{tot['distinct']:,}", tot["bytes"] / 1e9, f"{tot['not_stored']:,}",
               f"{prob:,}", time.time() - t0))
    log.info(msg)
    print(msg)
    if stopped:
        print("STOPPED: %s - free space and run the same command again" % stopped)
        return 3
    return 1 if st["error"] or st["failed"] else 0


def _has(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    sys.exit(main())
