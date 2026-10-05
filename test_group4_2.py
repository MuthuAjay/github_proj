#!/usr/bin/env python3
"""
test_group4_2.py - archive_unpack.py on a throwaway archive: counts ->
per-repo extraction -> unpack, with today's copy read too. Covers dedup of
the same file across versions and archives, nesting (zip > tar.gz > zip),
a single .csv.gz, 7z (when a 7z tool is found: SEVENZIP=... or 7zz/7z on
PATH), a password-protected zip, a damaged zip, a vendored archive, the
depth and size limits, resume, and pdf_page_merge.py running on the output.

    SEVENZIP=/path/to/7zz python3 test_group4_2.py
"""

import csv
import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import archive_unpack as au                                  # noqa: E402
from test_smoke import git, read_csv, run                     # noqa: E402
from test_extension_versions import put, unlink_refs          # noqa: E402
from test_group3 import HAVE_PDF, commit_on, make_pdf         # noqa: E402

SEVEN = au.sevenzip()
EXTS = ",".join(sorted(au.ARCHIVE_EXTS))
PDF = make_pdf(["Quarterly report", "Contact: jane@example.com"])


def zbytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def seven(tmp, name, files, fmt="7z", password=None):
    src = tempfile.mkdtemp(dir=tmp)
    for n, data in files:
        put(src, n, data)
    out = os.path.join(tmp, name)
    cmd = [SEVEN, "a", "-t" + fmt, out, "."] + (["-p" + password] if password else [])
    subprocess.run(cmd, cwd=src, check=True, capture_output=True)
    with open(out, "rb") as fh:
        return fh.read()


def export(version):
    contacts = ("name,email\nJane,jane@example.com\n" if version == 1 else
                "name,email\nJane,jane@example.com\nTom,tom@example.com\n")
    files = [("contacts.csv", contacts.encode()), ("report.pdf", PDF),
             ("img/logo.png", b"\x89PNG\x00logo"),
             ("nested.tar.gz", tgz([("notes.txt", b"call amy@example.com"),
                                    ("inner/deep.zip",
                                     zbytes([("x.json", b'{"k": "deep"}')]))]))]
    if version == 3:
        files.append(("today.txt", b"only in today's copy"))
    return zbytes(files)


def build(tmp):
    arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "active")
    a = os.path.join(arch, "org1", "repoA")
    os.makedirs(a)
    git(a, "init", "-q", "-b", "master")
    v1, v2, v3 = export(1), export(2), export(3)
    c1 = [("data/export.zip", v1),
          ("data/data.csv.gz", gzip.compress(b"id,phone\n1,+44 20 7946 0000\n")),
          ("node_modules/pkg/fixture.zip", zbytes([("f.txt", b"vendored")]))]
    commit_on(a, 1, "c1", c1)
    c2 = [("data/export.zip", v2),
          ("old/broken.zip", b"PK\x03\x04 this is not really a zip")]
    if SEVEN:
        c2 += [("backup/b.7z", seven(tmp, "b.7z", [("secret.txt", b"pw=hunter2")])),
               ("backup/locked.zip", seven(tmp, "l.zip", [("s.txt", b"x")],
                                           "zip", password="pw"))]
    commit_on(a, 2, "c2", c2)
    commit_on(a, 3, "c3", [("data/export.zip", v3)])
    unlink_refs(a)
    ra = os.path.join(act, "org1", "repoA")
    put(ra, "data/export.zip", v3)                   # today = v3
    put(ra, "data/data.csv.gz", c1[1][1])
    put(ra, "node_modules/pkg/fixture.zip", c1[2][1])
    batch = os.path.join(tmp, "B.csv")
    with open(batch, "w") as fh:
        fh.write("org,repo,tier,weight,listed_files\norg1,repoA,small,1,1\n")
    st, x = os.path.join(tmp, "counts"), os.path.join(tmp, "x")
    base = ["--batch", batch, "--repos-root", arch, "--active-root", act,
            "--out", st, "--extensions", EXTS]
    run("extension_versions.py", *base, "--binary-exts", EXTS)
    run("extension_versions.py", *base, "--identical")
    run("extract_versions.py", "--state", st, "--repos-root", arch, "--out", x,
        "--batch", batch, "--min-free-disk-gb", "0", "--extensions", EXTS,
        "--per-repo", "--skip-vendored")
    return act, batch, st, x


class Unpack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="g42_")
        cls.act, cls.batch, cls.st, cls.x = build(cls.tmp)
        cls.out = os.path.join(cls.tmp, "unpacked")
        cls.base = ["--in", cls.x, "--counts", cls.st, "--active-root", cls.act,
                    "--batch", cls.batch, "--min-free-disk-gb", "0", "--workers", "2"]
        cls.p = run("archive_unpack.py", *cls.base, "--out", cls.out)
        sd = os.path.join(cls.out, "_state", "org1", "repoA")
        cls.rows = read_csv(os.path.join(sd, "manifest.csv"))
        cls.arcs = read_csv(os.path.join(sd, "archives.csv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def of(self, path):
        return [r for r in self.rows if r["path"] == path]

    def test_versions_today_included_and_dedup(self):
        c = self.of("data/export.zip/contacts.csv")
        # v1, v2 from history, v3 (today, not in the extraction) read from
        # the active copy; v3's contacts = v2's -> deduped
        self.assertEqual([(r["archive_source"], r["action"]) for r in c],
                         [("history", "written"), ("history", "written"),
                          ("today", "deduped")])
        self.assertEqual(c[2]["first_date"], "today")
        pdf = self.of("data/export.zip/report.pdf")
        self.assertEqual([r["action"] for r in pdf], ["written", "deduped", "deduped"])
        self.assertEqual(len({r["stored_as"] for r in pdf}), 1)
        self.assertEqual(self.of("data/export.zip/today.txt")[0]["action"], "written")

    def test_stored_files_are_the_contents(self):
        import hashlib
        for r in self.rows:
            if r["action"] == "written":
                with open(os.path.join(self.out, r["stored_as"]), "rb") as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), r["blob"])
                self.assertTrue(r["stored_as"].startswith(
                    "files/%s/org1/repoA/" % au.safe_type(r["ext"])))

    def test_nesting_and_single_gz(self):
        deep = self.of("data/export.zip/nested.tar.gz/inner/deep.zip/x.json")
        self.assertEqual(deep[0]["action"], "written")
        self.assertEqual(deep[0]["depth"], "3")
        self.assertEqual(self.of("data/export.zip/nested.tar.gz")[0]["action"],
                         "opened_nested")
        self.assertEqual(self.of("data/export.zip/nested.tar.gz/notes.txt")[0]
                         ["route"], "group1_text")
        gz = self.of("data/data.csv.gz/data.csv")
        self.assertEqual([r["action"] for r in gz], ["written"])   # today = same

    def test_vendored_damaged_listed(self):
        v = self.of("node_modules/pkg/fixture.zip/")
        self.assertTrue(v and all(r["action"] == "skipped_vendored" for r in v))
        st = {a["archive_path"]: a["status"] for a in self.arcs}
        self.assertTrue(st["old/broken.zip"].startswith("damaged"))
        probs = read_csv(os.path.join(self.out, "stats", "problems.csv"))
        self.assertIn("old/broken.zip", {p["archive_path"] for p in probs})

    @unittest.skipUnless(SEVEN, "no 7z tool (set SEVENZIP)")
    def test_7z_and_password(self):
        self.assertEqual(self.of("backup/b.7z/secret.txt")[0]["action"], "written")
        locked = self.of("backup/locked.zip/s.txt")
        self.assertEqual(locked[0]["action"], "encrypted")

    def test_stats(self):
        by = {r["type"]: r for r in read_csv(os.path.join(self.out, "stats",
                                                          "by_type.csv"))}
        self.assertEqual(by["csv"]["route"], "group1_text")
        self.assertEqual(by["pdf"]["route"], "group3")
        self.assertEqual(by["png"]["route"], "group2_binary")
        self.assertEqual((by["pdf"]["occurrences"], by["pdf"]["distinct"]), ("3", "1"))
        self.assertEqual(by["csv"]["distinct"], "3")    # 2 contacts + data.csv
        self.assertIn("END", self.p.stdout)

    def test_resume(self):
        p = run("archive_unpack.py", *self.base, "--out", self.out)
        self.assertIn("0 repo(s) to do", p.stdout)

    def test_limits(self):
        out = os.path.join(self.tmp, "deep2")
        run("archive_unpack.py", *self.base, "--out", out, "--max-depth", "2")
        rows = read_csv(os.path.join(out, "_state", "org1", "repoA", "manifest.csv"))
        d = [r for r in rows if r["path"] == "data/export.zip/nested.tar.gz/inner/deep.zip"]
        self.assertEqual(d[0]["action"], "written")     # kept as a file, not opened
        out = os.path.join(self.tmp, "small")
        run("archive_unpack.py", *self.base, "--out", out,
            "--max-archive-bytes", "40", "--ratio-floor", "1")
        arcs = read_csv(os.path.join(out, "_state", "org1", "repoA", "archives.csv"))
        self.assertIn("limit_archive_bytes", {a["status"] for a in arcs})

    def test_report(self):
        p = run("archive_unpack_report.py", self.out)
        self.assertIn("repos 1 |", p.stdout)
        rows = [r for r in read_csv(os.path.join(self.out, "stats",
                                                 "archives_per_repo.csv")) if r["org"] == "org1"]
        by = {r["type"]: r for r in read_csv(os.path.join(self.out, "stats", "by_type.csv"))}
        # the report and archive_unpack.py's own stats agree
        self.assertEqual(rows[0]["files_inside"], by["total"]["occurrences"])
        self.assertEqual(rows[0]["distinct_files_written"], by["total"]["distinct"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "stats", "report.md")))
        bad = run("archive_unpack_report.py", self.tmp, check=False)
        self.assertNotEqual(bad.returncode, 0)

    @unittest.skipUnless(HAVE_PDF, "pip install pypdfium2 pypdf")
    def test_pdf_page_merge_runs_on_it(self):
        out = os.path.join(self.tmp, "pdfm")
        run("pdf_page_merge.py", "--in", self.out, "--out", out,
            "--batch", self.batch)
        merged = os.path.join(out, "org1", "repoA", "data", "export.zip", "report.pdf")
        import pypdf
        self.assertEqual(len(pypdf.PdfReader(merged).pages), 2)


class Formats(unittest.TestCase):
    def test_detect(self):
        tmp = tempfile.mkdtemp(prefix="g42d_")
        try:
            for name, data, fmt in (("a", zbytes([("x", b"1")]), "zip"),
                                    ("b", gzip.compress(b"x"), "gz"),
                                    ("c", tgz([("x", b"1")]), "gz"),
                                    ("d", b"just text", "")):
                p = os.path.join(tmp, name)
                with open(p, "wb") as fh:
                    fh.write(data)
                self.assertEqual(au.detect(p), fmt, name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zst_lz4_single_files(self):
        tmp = tempfile.mkdtemp(prefix="g42z_")
        try:
            cfg = type("C", (), {"sevenzip": None})()
            for mod, fmt, comp in (("zstandard", "zst",
                                    lambda d: __import__("zstandard").ZstdCompressor().compress(d)),
                                   ("lz4.frame", "lz4",
                                    lambda d: __import__("lz4.frame").frame.compress(d))):
                try:
                    __import__(mod)
                except ImportError:
                    continue
                p = os.path.join(tmp, "d.csv." + fmt)
                with open(p, "wb") as fh:
                    fh.write(comp(b"a,b\n1,jane@example.com\n"))
                got_fmt, members = au.open_archive(p, cfg)
                name, size, kind, opener = next(members)
                self.assertEqual((got_fmt, kind), (fmt, "single"))
                with opener() as fh:
                    self.assertIn(b"jane@example.com", fh.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_7z_tool(self):
        tmp = tempfile.mkdtemp(prefix="g42n_")
        try:
            p = os.path.join(tmp, "x.7z")
            with open(p, "wb") as fh:
                fh.write(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 40)
            with self.assertRaises(LookupError):
                au.open_archive(p, type("C", (), {"sevenzip": None})())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clean_name_never_escapes(self):
        self.assertEqual(au.clean_name("../../etc/passwd"), "../../etc/passwd")
        self.assertEqual(au.clean_name("/abs/x"), "abs/x")
        self.assertEqual(au.clean_name("C:\\win\\x.txt"), "win/x.txt")
        # names are only recorded; files are stored by hash, so even a
        # '..' name cannot write outside files/

    def test_routes(self):
        self.assertEqual(au.route_of("xlsx"), "group2_binary")
        self.assertEqual(au.route_of("zip"), "archive")
        self.assertEqual(au.route_of("jar"), "package_or_asset")
        self.assertEqual(au.route_of(""), "no_extension")
        self.assertEqual(au.route_of("sqlite"), "group4_1_db")
        self.assertEqual(au.route_of("scss"), "group4_5_text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
