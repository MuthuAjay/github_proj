#!/usr/bin/env python3
"""
test_group4_2_nested.py - run_group4_2_nested.sh on the throwaway repo of
test_group4_2.py, first unpacked only 2 levels deep: find lists the closed
deep.zip, backup saves the old _state, unpack opens it, compare shows it,
reset clears the steps' done.json, a second run changes nothing.

    python3 test_group4_2_nested.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from test_group4_2 import EXTS, build
from test_smoke import read_csv, run

HERE = os.path.dirname(os.path.abspath(__file__))
DEEP = "data/export.zip/nested.tar.gz/inner/deep.zip"


class Nested(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="g42nest_")
        act, batch, st, x = build(cls.tmp)
        cls.work = os.path.join(cls.tmp, "work")
        g = os.path.join(cls.work, "group4_2")
        os.makedirs(g)
        os.symlink(x, os.path.join(g, "4_2_extract"))
        os.symlink(st, os.path.join(g, "counts"))
        cls.un = os.path.join(g, "unpacked")
        run("archive_unpack.py", "--in", x, "--counts", st, "--active-root", act,
            "--batch", batch, "--min-free-disk-gb", "0", "--workers", "1",
            "--extensions", EXTS, "--out", cls.un, "--max-depth", "2")
        done = os.path.join(g, "group2_text", "_state", "org1", "repoA")
        os.makedirs(done)
        with open(os.path.join(done, "done.json"), "w") as fh:
            fh.write('{"status": "ok"}')
        cls.env = dict(os.environ, WORK=cls.work, ACTIVE=act, RUNS="group4_2",
                       OLD_DEPTH="2", WORKERS="1", MIN_DISK_GB="0")
        cls.p = cls.runner("find backup unpack compare reset")
        cls.nd = os.path.join(g, "nested")

    @classmethod
    def runner(cls, steps):
        env = dict(cls.env, STEPS=steps)
        return subprocess.run(["bash", os.path.join(HERE, "run_group4_2_nested.sh")],
                              env=env, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def manifest(self):
        return read_csv(os.path.join(self.un, "_state", "org1", "repoA",
                                     "manifest.csv"))

    def test_run(self):
        self.assertEqual(self.p.returncode, 0, self.p.stdout + self.p.stderr)
        files = read_csv(os.path.join(self.nd, "todo_files.csv"))
        self.assertIn(DEEP, {r["path"] for r in files})
        self.assertEqual([(r["org"], r["repo"]) for r in
                          read_csv(os.path.join(self.nd, "todo.csv"))],
                         [("org1", "repoA")])
        old = read_csv(os.path.join(self.nd, "before", "org1", "repoA",
                                    "manifest.csv"))
        self.assertLessEqual({r["action"] for r in old if r["path"] == DEEP},
                             {"written", "deduped"})        # kept whole
        new = self.manifest()
        self.assertEqual({r["action"] for r in new if r["path"] == DEEP},
                         {"opened_nested"})
        self.assertIn(DEEP + "/x.json", {r["path"] for r in new})
        c = read_csv(os.path.join(self.nd, "compare.csv"))[0]
        self.assertEqual((c["status"], c["before_max_depth"], c["after_max_depth"],
                          c["before_still_closed"], c["after_still_closed"]),
                         ("done", "2", "3", "3", "0"))
        self.assertFalse(os.path.exists(os.path.join(
            self.work, "group4_2", "group2_text", "_state", "org1", "repoA",
            "done.json")))

    def test_rerun_keeps_todo_and_backup(self):
        p = self.runner("find backup unpack")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("find: kept", p.stdout)
        self.assertIn("backup: 0 repo(s)", p.stdout)
        self.assertIn("0 repo(s) to do", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
