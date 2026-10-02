#!/usr/bin/env python3
"""
test_group3.py - checks for pkl_to_text.py: real pickles in every protocol
and the compressed forms, text hidden in byte blocks (numpy-style UTF-32),
several pickles in one file, a pickle that would run code (it must not),
and the whole chain: counts -> per-repo extraction -> text, with today's
file included. Needs only python3 and git.

    python3 test_group3.py
"""

import bz2
import gzip
import lzma
import os
import pickle
import shutil
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pkl_to_text as pt                                 # noqa: E402
from test_smoke import git, read_csv, run                # noqa: E402
from test_extension_versions import blob_id, commit, put, unlink_refs  # noqa: E402

DATA = {"name": "Jane Doe", "email": "jane@example.com",
        "rows": [{"phone": "+44 20 7946 0000"}, ("Zoë Müller", 42)],
        "note": "line one\nline two\n\n  line one  "}
MARKER = os.path.join(tempfile.gettempdir(), "pkl_to_text_must_not_exist")


class Boom:
    """Unpickling this would create MARKER: reading must never do that."""
    def __reduce__(self):
        return (open, (MARKER, "w"))


def lines(data):
    strs, how = pt.text_of(data)
    return list(pt.lines_of(strs)), how


class Reading(unittest.TestCase):
    def test_every_protocol(self):
        for proto in range(0, pickle.HIGHEST_PROTOCOL + 1):
            got, how = lines(pickle.dumps(DATA, protocol=proto))
            for s in ("Jane Doe", "jane@example.com", "+44 20 7946 0000",
                      "Zoë Müller", "line one", "line two", "phone"):
                self.assertIn(s, got, "protocol %d" % proto)
            self.assertEqual(how, "pickle", "protocol %d" % proto)

    def test_compressed(self):
        raw = pickle.dumps(DATA, protocol=4)
        joblib_old = b"ZF" + hex(len(raw)).encode().ljust(19) + zlib.compress(raw)
        for data, how in ((gzip.compress(raw), "gzip"), (bz2.compress(raw), "bz2"),
                          (lzma.compress(raw), "xz"), (zlib.compress(raw), "zlib"),
                          (joblib_old, "joblib_zlib")):
            got, h = lines(data)
            self.assertIn("jane@example.com", got, how)
            self.assertEqual(h, how)

    def test_text_in_byte_blocks(self):
        # a numpy '<U' column is stored as raw UTF-32 bytes in the pickle
        utf32 = "alice@example.com".encode("utf-32-le")
        got, _ = lines(pickle.dumps({"col": utf32, "ascii": b"bob@example.org"},
                                    protocol=4))
        self.assertIn("alice@example.com", got)
        self.assertIn("bob@example.org", got)

    def test_inline_buffer_and_several_pickles(self):
        # joblib writes array bytes between pickle opcodes: the walk stops
        # there and the rest is scanned, so text after it is still found
        first = pickle.dumps({"k": "first@example.com"}, protocol=4)
        broken = first[:-1] + b"\xff\xfe\x00\x01garbage-then carol@example.net"
        got, how = lines(broken)
        self.assertIn("first@example.com", got)
        self.assertTrue(any("carol@example.net" in s for s in got))
        self.assertEqual(how, "pickle+raw_scan")
        two = first + pickle.dumps(["second@example.com"], protocol=2)
        got, how = lines(two)
        self.assertEqual(how, "pickle")
        self.assertIn("second@example.com", got)

    def test_not_a_pickle(self):
        got, how = lines(b"\x00\x01\x02 not a pickle: dave@example.com \x00")
        self.assertEqual(how, "not_pickle_raw_scan")
        self.assertIn("not a pickle: dave@example.com", got)
        self.assertEqual(lines(b""), ([], "empty"))

    def test_never_unpickled(self):
        if os.path.exists(MARKER):
            os.remove(MARKER)
        data = pickle.dumps({"x": Boom(), "who": "eve@example.com"}, protocol=4)
        got, _ = lines(data)
        self.assertIn("eve@example.com", got)
        self.assertIn(MARKER, got)            # the path is just a string in it
        self.assertFalse(os.path.exists(MARKER), "the pickle was executed")

    def test_lines_trimmed_no_blanks(self):
        # once-per-path is run_repo's job (Chain); here: split, trim, no blanks
        got, _ = lines(pickle.dumps(DATA, protocol=4))
        self.assertEqual(got.count("line one"), 2)      # "line one", "  line one  "
        self.assertNotIn("", got)


V1 = pickle.dumps({"owner": "jane@example.com", "v": 1}, protocol=4)
V2 = gzip.compress(pickle.dumps({"owner": "jane@example.com",
                                 "new": "tom@example.com"}, protocol=4))
TODAY = pickle.dumps({"owner": "jane@example.com", "today": "amy@example.com"},
                     protocol=4)


class Chain(unittest.TestCase):
    """extension_versions -> extract_versions --per-repo -> pkl_to_text."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="g3pkl_")
        arch, act = os.path.join(cls.tmp, "archive"), os.path.join(cls.tmp, "active")
        a = os.path.join(arch, "org1", "repoA")
        os.makedirs(a)
        git(a, "init", "-q", "-b", "master")
        commit(a, "c1", [("models/m.pkl", V1), ("gone.pkl", V1)])
        commit(a, "c2", [("models/m.pkl", V2)], rm=["gone.pkl"])
        commit(a, "c3", [("models/m.pkl", TODAY)])
        unlink_refs(a)
        put(os.path.join(act, "org1", "repoA"), "models/m.pkl", TODAY)
        cls.batch = os.path.join(cls.tmp, "B.csv")
        with open(cls.batch, "w") as fh:
            fh.write("org,repo,tier,weight,listed_files\norg1,repoA,small,1,1\n")
        st, x = os.path.join(cls.tmp, "counts"), os.path.join(cls.tmp, "x")
        base = ["--batch", cls.batch, "--repos-root", arch, "--active-root", act,
                "--out", st, "--extensions", "pkl"]
        run("extension_versions.py", *base, "--binary-exts", "pkl")
        run("extension_versions.py", *base, "--identical")
        run("extract_versions.py", "--state", st, "--repos-root", arch, "--out", x,
            "--batch", cls.batch, "--min-free-disk-gb", "0", "--extensions", "pkl",
            "--per-repo")
        cls.x, cls.act = x, act
        cls.out = os.path.join(cls.tmp, "text")
        cls.p = run("pkl_to_text.py", "--in", x, "--out", cls.out, "--batch",
                    cls.batch, "--active-root", act, "--workers", "2")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.out, "org1", "repoA", rel), encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_one_txt_per_path_lines_once(self):
        got = self.read("models/m.pkl.txt")
        self.assertEqual(got.count("jane@example.com"), 1)     # in all three
        self.assertIn("tom@example.com", got)                  # v2, gzip
        self.assertIn("amy@example.com", got)                  # today's file
        self.assertLess(got.index("tom@example.com"), got.index("amy@example.com"))
        self.assertIn("jane@example.com", self.read("gone.pkl.txt"))

    def test_manifest_and_summary(self):
        m = {r["path"]: r for r in read_csv(os.path.join(
            self.out, "_state", "org1", "repoA", "manifest.csv"))}
        self.assertEqual(m["models/m.pkl"]["versions"], "2")   # v1, v2 extracted
        self.assertEqual(m["models/m.pkl"]["versions_read"], "3")  # + today
        self.assertEqual(m["models/m.pkl"]["active_included"], "yes")
        self.assertIn("gzip:1", m["models/m.pkl"]["read_as"])
        self.assertEqual(m["gone.pkl"]["active_included"], "no_file_today")
        self.assertIn("END", self.p.stdout)

    def test_resume_and_redo(self):
        p = run("pkl_to_text.py", "--in", self.x, "--out", self.out,
                "--batch", self.batch)
        self.assertIn("0 repo(s) to do", p.stdout)
        run("pkl_to_text.py", "--in", self.x, "--out", self.out, "--batch",
            self.batch, "--active-root", self.act, "--redo")
        self.assertIn("amy@example.com", self.read("models/m.pkl.txt"))


# --------------------------------------------------------------------------
# pdf_page_merge.py
# --------------------------------------------------------------------------

try:
    import pypdf                                          # noqa: F401
    import pypdfium2                                      # noqa: F401
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False


def make_pdf(pages, info=b""):
    """A small valid PDF: one page per text in `pages` (None = a page with
    no text, only a filled box - like a scan). `info` changes the bytes
    without changing any page (metadata)."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for t in pages:
        if t is None:
            stream = b"0.2 g 50 50 200 300 re f"
        else:
            safe = t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = b"BT /F1 18 Tf 50 700 Td (" + safe.encode("latin-1") + b") Tj ET"
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        cid = len(objs)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % cid)
        kids.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    objs.append(b"<< /Producer (" + (info or b"test") + b") >>")
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, len(objs), x)
    return bytes(out)


def encrypted(data, password):
    import io
    w = pypdf.PdfWriter()
    w.append(io.BytesIO(data))
    w.encrypt(user_password=password, owner_password="owner")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def commit_on(repo, day, msg, files=(), rm=()):
    """commit() with its own date, so versions have a clear oldest-first order."""
    import subprocess
    from test_smoke import GIT_ENV
    for rel, data in files:
        put(repo, rel, data)
    for r in rm:
        git(repo, "rm", "-q", r)
    git(repo, "add", "-A")
    d = "2026-01-%02dT12:00:00" % day
    subprocess.run(["git", "-C", repo, "commit", "-qm", msg], check=True,
                   env=dict(GIT_ENV, GIT_AUTHOR_DATE=d, GIT_COMMITTER_DATE=d))


def pdf_texts(path):
    return [(pg.extract_text() or "").strip() for pg in pypdf.PdfReader(path).pages]


@unittest.skipUnless(HAVE_PDF, "pip install pypdfium2 pypdf")
class PdfMerge(unittest.TestCase):
    """counts -> extract --per-repo -> pdf_page_merge.py.

    docs/report.pdf: v1 [Hello Jane, Terms, scan], v2 = the same three pages
    in a file with other bytes + [New page], v3 (today) [Hello Jane, Terms v3].
    old/copy.pdf: v1's bytes, deleted. secret.pdf: needs a password.
    broken.pdf: damaged."""

    @classmethod
    def setUpClass(cls):
        cls.v1 = make_pdf(["Hello Jane", "Terms and conditions", None])
        cls.v2 = make_pdf(["Hello Jane", "Terms and conditions", None,
                           "New page added later"], info=b"resaved")
        cls.v3 = make_pdf(["Hello Jane", "Terms version three"])
        cls.secret = encrypted(make_pdf(["Private salary list"]), "pw")
        cls.tmp = tempfile.mkdtemp(prefix="g3pdf_")
        arch, act = os.path.join(cls.tmp, "archive"), os.path.join(cls.tmp, "active")
        a = os.path.join(arch, "org1", "repoA")
        os.makedirs(a)
        git(a, "init", "-q", "-b", "master")
        commit_on(a, 1, "c1", [("docs/report.pdf", cls.v1), ("old/copy.pdf", cls.v1),
                               ("secret.pdf", cls.secret),
                               ("broken.pdf", b"%PDF-1.4 not really")])
        commit_on(a, 2, "c2", [("docs/report.pdf", cls.v2)], rm=["old/copy.pdf"])
        commit_on(a, 3, "c3", [("docs/report.pdf", cls.v3)])
        unlink_refs(a)
        put(os.path.join(act, "org1", "repoA"), "docs/report.pdf", cls.v3)
        cls.batch = os.path.join(cls.tmp, "B.csv")
        with open(cls.batch, "w") as fh:
            fh.write("org,repo,tier,weight,listed_files\norg1,repoA,small,1,1\n")
        st, x = os.path.join(cls.tmp, "counts"), os.path.join(cls.tmp, "x")
        base = ["--batch", cls.batch, "--repos-root", arch, "--active-root", act,
                "--out", st, "--extensions", "pdf"]
        run("extension_versions.py", *base, "--binary-exts", "pdf")
        run("extension_versions.py", *base, "--identical")
        run("extract_versions.py", "--state", st, "--repos-root", arch, "--out", x,
            "--batch", cls.batch, "--min-free-disk-gb", "0", "--extensions", "pdf",
            "--per-repo")
        cls.x, cls.act = x, act
        cls.out = os.path.join(cls.tmp, "merged")
        cls.p = run("pdf_page_merge.py", "--in", x, "--out", cls.out,
                    "--batch", cls.batch, "--workers", "2")
        sd = os.path.join(cls.out, "_state", "org1", "repoA")
        cls.m = {r["path"]: r for r in read_csv(os.path.join(sd, "manifest.csv"))}
        cls.pages = read_csv(os.path.join(sd, "pages.csv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def f(self, rel):
        return os.path.join(self.out, "org1", "repoA", rel)

    def test_merged_distinct_pages_text_kept(self):
        # v3 is today's file: not extracted. v1 + v2 -> 4 distinct pages
        self.assertEqual(pdf_texts(self.f("docs/report.pdf")),
                         ["Hello Jane", "Terms and conditions", "",
                          "New page added later"])
        m = self.m["docs/report.pdf"]
        self.assertEqual((m["versions"], m["versions_read"], m["pages_seen"],
                          m["pages_kept"], m["pages_duplicate"],
                          m["pages_ocr_needed"]), ("2", "2", "7", "4", "3", "1"))

    def test_pages_csv(self):
        rows = [r for r in self.pages if r["path"] == "docs/report.pdf"]
        st = [(r["page"], r["status"], r["merged_page"]) for r in rows]
        self.assertEqual(st, [("1", "kept", "1"), ("2", "kept", "2"),
                              ("3", "kept_ocr_needed", "3"),
                              ("1", "duplicate", ""), ("2", "duplicate", ""),
                              ("3", "duplicate", ""), ("4", "kept", "4")])
        # same page in two files with different bytes -> same hash
        self.assertEqual(rows[0]["hash"], rows[3]["hash"])
        self.assertEqual(rows[2]["text_chars"], "0")

    def test_single_version_copied_as_is(self):
        with open(self.f("old/copy.pdf"), "rb") as fh:
            self.assertEqual(fh.read(), self.v1)

    def test_unreadable_copied_whole_and_listed(self):
        for rel, data in (("secret.pdf", self.secret),
                          ("broken.pdf", b"%PDF-1.4 not really")):
            m = self.m[rel]
            self.assertEqual(m["versions_unreadable"], "1", rel)
            self.assertEqual(m["pages_kept"], "0", rel)
            whole = self.f("%s.%s.pdf" % (rel, blob_id(data)[:12]))
            with open(whole, "rb") as fh:
                self.assertEqual(fh.read(), data, rel)
            self.assertFalse(os.path.exists(self.f(rel)), rel)
        self.assertIn("unreadable", [r["status"].split(":")[0] for r in self.pages
                                     if r["path"] == "secret.pdf"][0])

    def test_drop_active_pages(self):
        out = os.path.join(self.tmp, "merged_drop")
        run("pdf_page_merge.py", "--in", self.x, "--out", out, "--batch",
            self.batch, "--active-root", self.act, "--drop-active-pages")
        m = {r["path"]: r for r in read_csv(os.path.join(
            out, "_state", "org1", "repoA", "manifest.csv"))}["docs/report.pdf"]
        # Hello Jane is on today's page 1: dropped in v1 and v2
        self.assertEqual((m["pages_kept"], m["pages_duplicate"], m["pages_in_today"]),
                         ("3", "2", "2"))
        self.assertEqual(pdf_texts(os.path.join(out, "org1", "repoA",
                                                "docs/report.pdf")),
                         ["Terms and conditions", "", "New page added later"])

    def test_resume_settings_guard_summary(self):
        p = run("pdf_page_merge.py", "--in", self.x, "--out", self.out,
                "--batch", self.batch)
        self.assertIn("0 repo(s) to do", p.stdout)
        p = run("pdf_page_merge.py", "--in", self.x, "--out", self.out,
                "--batch", self.batch, "--scale", "2", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("other settings", p.stderr)
        tot = [r for r in read_csv(os.path.join(self.out, "summary.csv"))
               if r["org"] == "total"][0]
        # report.pdf 4 + old/copy.pdf 3 (copied whole)
        self.assertEqual((tot["pages_kept"], tot["pages_ocr_needed"],
                          tot["versions_unreadable"]), ("7", "2", "2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
