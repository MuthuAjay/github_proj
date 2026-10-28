#!/usr/bin/env python3
"""
test_group4_2_files.py - group4_2_files.py on a small made-up archive_unpack.py
output: one file per distinct content (.v1/.v2 when a path has several), a
content already written under another path, empty and not stored versions,
macOS / vendored paths left out, other types ignored, resume and stats; and
run_group4_2_inner.sh over two made-up runs (3_pdf only with pypdf).

    python3 test_group4_2_files.py
"""

import json
import pickle
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_group4_2_text import Fake, ORG, REPO, read_csv       # noqa: E402

try:
    import pypdf                                                # noqa: F401
    import pypdfium2                                            # noqa: F401
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False


def build(root):
    f = Fake(root)
    a = "docs/pack.zip"
    f.add(a, "plan.xlsx", b"PK xlsx one", "2024-01-01")
    f.add(a, "plan.xlsx", b"PK xlsx two", "2024-02-01")
    f.add(a, "plan.xlsx", b"PK xlsx one", "2024-03-01")      # back to one
    f.add(a, "plan.xlsx", None, "2024-04-01")                # error row
    f.add(a, "copy/plan_old.xlsx", b"PK xlsx two", "2024-02-01")
    f.add(a, "logo.png", b"\x89PNG\0data", "2024-01-01")
    f.add(a, "blank.docx", b"", "2024-01-01")
    f.add(a, "lost.msg", None, "2024-01-01")
    f.add(a, "__MACOSX/._logo.png", b"\0\x05\x16\x07", "2024-01-01")
    f.add(a, "web/node_modules/x/icon.png", b"\x89PNG\0icon", "2024-01-01")
    f.add(a, "app.json", b"{}\n", "2024-01-01")              # not group 2
    f.add(a, "notes.rst", b"Title\n=====\n", "2024-01-01")   # group 2 text
    f.add(a, "model.pkl", pickle.dumps({"owner": "jane@example.com"}),
          "2024-01-01")                                      # group 3
    f.save()


class GroupFiles(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.un = os.path.join(self.tmp, "unpacked")
        self.out = os.path.join(self.tmp, "group2_binary")
        build(self.un)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_it(self, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "group4_2_files.py"),
             "--unpacked", self.un, "--out", self.out, "--workers", "2"]
            + list(extra), capture_output=True, text=True)

    def test_all(self):
        p = self.run_it()
        self.assertEqual(p.returncode, 0, p.stderr)
        base = os.path.join(self.out, ORG, REPO, "docs", "pack.zip")
        got = sorted(os.path.relpath(os.path.join(d, n), base)
                     for d, _, ns in os.walk(base) for n in ns)
        self.assertEqual(got, ["logo.png", "plan.v1.xlsx", "plan.v2.xlsx"])
        with open(os.path.join(base, "plan.v1.xlsx"), "rb") as fh:
            self.assertEqual(fh.read(), b"PK xlsx one")
        rows = read_csv(os.path.join(self.out, "_state", ORG, REPO,
                                     "manifest.csv"))
        st = [(r["path"].split("/", 2)[2], r["status"], r["versions"],
               r["not_stored"]) for r in rows]
        self.assertEqual(st, [
            ("plan.xlsx", "written", "2", "1"),
            ("plan.xlsx", "written", "1", "0"),
            ("copy/plan_old.xlsx", "same_as_other_path", "1", "0"),
            ("logo.png", "written", "1", "0"),
            ("blank.docx", "empty", "1", "0"),
            ("lost.msg", "not_stored", "0", "1")])
        self.assertEqual(rows[2]["same_as"], os.path.join(
            ORG, REPO, "docs", "pack.zip", "plan.v2.xlsx"))
        left = {r["path"].split("/", 2)[2]: r["reason"] for r in read_csv(
            os.path.join(self.out, "_state", ORG, REPO, "left_out.csv"))}
        self.assertEqual(left, {"__MACOSX/._logo.png": "macos",
                                "web/node_modules/x/icon.png": "vendored"})
        with open(os.path.join(self.out, "stats", "summary.json")) as fh:
            s = json.load(fh)
        self.assertEqual((s["scope_files"], s["scope_paths"], s["paths"],
                          s["contents"], s["files_written"]), (10, 7, 5, 5, 3))
        bt = {r["type"]: r for r in read_csv(
            os.path.join(self.out, "stats", "by_type.csv"))}
        x = bt["xlsx"]
        self.assertEqual((x["scope_files"], x["paths"], x["versions"],
                          x["contents"], x["files_written"],
                          x["status_same_as_other_path"],
                          x["not_stored_versions"]),
                         ("5", "2", "5", "3", "2", "1", "1"))
        self.assertEqual(bt["png"]["left_vendored"], "1")
        self.assertNotIn("json", bt)
        self.assertNotIn("rst", bt)
        # resume, redo
        p = self.run_it()
        self.assertIn("(0 to do)", p.stderr)
        p = self.run_it("--redo")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(base, "plan.v2.xlsx")))

    def test_noext_and_other_types(self):
        f = Fake(self.un)
        a = "data/drop.zip"
        f.add(a, "Dockerfile", b"FROM python\n", "2024-01-01")       # no extension
        f.add(a, "export/customers", b"\0\1binary", "2024-01-01")    # no extension
        f.add(a, ".gitignore", b"*.pyc\n", "2024-01-01")             # type gitignore
        f.add(a, "app.db", b"SQLite format 3\0", "2024-01-01")        # group 4.1
        f.add(a, "intro.mp4", b"\0\0\0 ftypmp4", "2024-01-01")       # group 4.8
        f.save()
        p = self.run_it("--extensions", "_noext,db,mp4", "--redo")
        self.assertEqual(p.returncode, 0, p.stderr)
        base = os.path.join(self.out, ORG, REPO, "data", "drop.zip")
        got = sorted(os.path.relpath(os.path.join(d, n), base)
                     for d, _, ns in os.walk(base) for n in ns)
        self.assertEqual(got, ["Dockerfile", "app.db", "export/customers", "intro.mp4"])
        bt = {r["type"]: r for r in read_csv(
            os.path.join(self.out, "stats", "by_type.csv"))}
        self.assertEqual(bt["_noext"]["files_written"], "2")
        self.assertNotIn("gitignore", bt)
        self.assertNotIn("xlsx", bt)


class Runner(unittest.TestCase):

    def test_runner(self):
        tmp = tempfile.mkdtemp()
        try:
            for g in ("group4_2", "group4_2_R"):
                build(os.path.join(tmp, g, "unpacked"))
            steps = "2_text 2_bin 3_pkl" + (" 3_pdf" if HAVE_PDF else "")
            env = dict(os.environ, WORK=tmp, MIN_FREE_GB="0", WORKERS="2",
                       STEPS=steps, PYTHON=sys.executable)
            p = subprocess.run(["bash", os.path.join(HERE,
                                                     "run_group4_2_inner.sh")],
                               env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("ALL DONE", p.stdout)
            for g in ("group4_2", "group4_2_R"):
                t = os.path.join(tmp, g, "group2_text", ORG, REPO, "docs",
                                 "pack.zip", "notes.rst.txt")
                self.assertTrue(os.path.isfile(t))
                b = os.path.join(tmp, g, "group2_binary", ORG, REPO, "docs",
                                 "pack.zip", "logo.png")
                self.assertTrue(os.path.isfile(b))
                k = os.path.join(tmp, g, "group3_pkl_text", ORG, REPO, "docs",
                                 "pack.zip", "model.pkl.txt")
                with open(k, encoding="utf-8") as fh:
                    self.assertIn("jane@example.com", fh.read())
                if HAVE_PDF:      # no pdf in the made-up archive: 0 paths
                    self.assertTrue(os.path.isfile(os.path.join(
                        tmp, g, "group3_pdf_merged", "summary.csv")))
            self.assertIn("[3_pkl group4_2_R] finished, exit 0", p.stdout)
            self.assertFalse(os.path.exists(os.path.join(
                tmp, "group4_2", "group3_pkl_text", "stats_combined")))
            cb = {r["type"]: r for r in read_csv(os.path.join(
                tmp, "group4_2", "group2_binary", "stats_combined",
                "by_type.csv"))}
            self.assertEqual(cb["xlsx"]["files_written"], "4")
            self.assertTrue(os.path.isfile(os.path.join(
                tmp, "group4_2", "group2_text", "stats_combined",
                "by_type.csv")))
            env["STEPS"] = "nope"
            p = subprocess.run(["bash", os.path.join(HERE,
                                                     "run_group4_2_inner.sh")],
                               env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertIn("unknown step", p.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
