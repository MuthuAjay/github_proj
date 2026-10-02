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
from test_extension_versions import commit, put, unlink_refs  # noqa: E402

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
