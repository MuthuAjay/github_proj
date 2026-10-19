#!/usr/bin/env python3
"""
test_active_files.py - end-to-end checks for active_files.py.

Uses test_extension_versions.py's throwaway archive and active copy: pass 1,
pass 2 and extract_versions.py, then active_files.py on top, and checks that
today's files are the active copy's bytes, stored once, and that what
extract_versions.py already holds is not written twice. Also a file gone
from the active copy, resume, and a missing active copy. Needs only python3
and git.

    python3 test_active_files.py
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_smoke import read_csv, run                # noqa: E402
from test_extension_versions import (DECK1, JB, PNG, PNG_ACTIVE, REAL_MPG, X,
                                     blob_id, build)  # noqa: E402

def raw(path):
    with open(path, "rb") as fh:
        return fh.read()


EXTS = ("xlsx,xlsm,xlsb,xls,docx,doc,pptx,pptm,msg,eml,png,jpg,jpeg,mpg,pfx,"
        "p7s,woff,suo")


class ActiveFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="af_")
        cls.arch, cls.act, cls.batch = build(cls.tmp)
        cls.state = os.path.join(cls.tmp, "counts")
        base = ["--batch", cls.batch, "--repos-root", cls.arch,
                "--active-root", cls.act, "--out", cls.state]
        run("extension_versions.py", *base)
        run("extension_versions.py", *base, "--identical")
        cls.binary = os.path.join(cls.tmp, "binary")
        run("extract_versions.py", "--state", cls.state, "--repos-root",
            cls.arch, "--out", cls.binary, "--batch", cls.batch,
            "--min-free-disk-gb", "0")
        cls.out = os.path.join(cls.tmp, "active_out")
        cls.args = ["--state", cls.state, "--active-root", cls.act,
                    "--out", cls.out, "--also-stored", cls.binary,
                    "--batch", cls.batch, "--extensions", EXTS,
                    "--workers", "2", "--min-free-disk-gb", "0"]
        cls.p = run("active_files.py", *cls.args)
        cls.rows = read_csv(os.path.join(cls.out, "manifest.csv"))
        cls.by = {(r["repo"], r["path"]): r for r in cls.rows}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_file_at_head(self):
        got = sorted((r["repo"], r["path"], r["action"], r["why"])
                     for r in self.rows)
        self.assertEqual(got, [
            ("repoA", "a.jpg", "written", "same_as_other_path"),
            ("repoA", "big.mpg", "written", "lfs_active_real"),
            ("repoA", "copy/logo_copy.png", "in_stored", "same_as_latest"),
            ("repoA", "deck.pptx", "written", "same_as_older"),
            ("repoA", "docs/new.xlsx", "written", "active_only"),
            ("repoA", "docs/report.xlsx", "written", "same_as_latest"),
            ("repoA", "logo.png", "written", "no_match"),
            ("repoA", "node_modules/pkg/font.woff", "written", "same_as_latest"),
            ("repoA", "notes.msg", "written", "no_match"),
        ])                    # repoB has no active copy; .git/ is skipped

    def test_files_are_todays_bytes(self):
        for (repo, path), r in self.by.items():
            src = os.path.join(self.act, "org1", repo, *path.split("/"))
            data = raw(src)
            self.assertEqual(r["blob"], blob_id(data), path)
            self.assertEqual(int(r["bytes"]), len(data))
            where = (r["stored_as"] if r["action"] == "in_stored"
                     else os.path.join(self.out, r["stored_as"]))
            self.assertEqual(raw(where), data, path)
        for path, data in [("docs/report.xlsx", X[3]), ("a.jpg", JB),
                           ("deck.pptx", DECK1), ("big.mpg", REAL_MPG),
                           ("logo.png", PNG_ACTIVE)]:
            self.assertEqual(self.by[("repoA", path)]["stored_as"],
                             os.path.join("files", path.rsplit(".", 1)[1],
                                          blob_id(data)[:2],
                                          "%s.%s" % (blob_id(data),
                                                     path.rsplit(".", 1)[1])))

    def test_not_written_twice(self):
        # PNG was extracted from repoB (no active copy there): not again
        r = self.by[("repoA", "copy/logo_copy.png")]
        self.assertEqual(r["blob"], blob_id(PNG))
        self.assertTrue(r["stored_as"].startswith(self.binary))
        self.assertFalse(os.path.exists(os.path.join(
            self.out, "files", "png", blob_id(PNG)[:2],
            blob_id(PNG) + ".png")))

    def test_summary_matches_disk(self):
        ext = {r["ext"]: r for r in
               read_csv(os.path.join(self.out, "by_extension.csv"))}
        self.assertEqual(ext["png"]["written"], "1")
        self.assertEqual(ext["png"]["in_stored"], "1")
        for r in ext.values():
            self.assertEqual(r["files_stored"], r["files_on_disk"], r["ext"])
        self.assertIn("They agree for every extension",
                      raw(os.path.join(self.out, "summary.md")).decode())

    def test_resume(self):
        p = run("active_files.py", *self.args)
        self.assertIn("0 repo(s) to process", p.stdout)

    def test_gone_and_dedup_without_also_stored(self):
        out = os.path.join(self.tmp, "active_gone")
        act = os.path.join(self.tmp, "active_copy2")
        shutil.copytree(self.act, act)
        os.remove(os.path.join(act, "org1", "repoA", "notes.msg"))
        run("active_files.py", "--state", self.state, "--active-root", act,
            "--out", out, "--batch", self.batch, "--extensions", EXTS,
            "--min-free-disk-gb", "0")
        by = {r["path"]: r for r in read_csv(os.path.join(out, "manifest.csv"))}
        self.assertEqual(by["notes.msg"]["action"], "gone")
        self.assertEqual(by["copy/logo_copy.png"]["action"], "written")

    def test_missing_active_copy_stops(self):
        p = run("active_files.py", "--state", self.state, "--active-root",
                os.path.join(self.tmp, "nowhere"), "--out",
                os.path.join(self.tmp, "x"), "--batch", self.batch,
                "--extensions", EXTS, check=False)
        self.assertEqual(p.returncode, 3)


if __name__ == "__main__":
    unittest.main()
