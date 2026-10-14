#!/usr/bin/env python3
"""
test_group4_2_path_list.py - group4_2_path_list.py on two small made-up
archive_unpack.py outputs: one row per path with its versions, contents,
dates and sizes, left-out paths listed with the reason, other types
ignored, the per-type summary with a total row.

    python3 test_group4_2_path_list.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_group4_2_text import Fake, ORG, REPO, read_csv       # noqa: E402


class PathList(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.runs = []
        for g in ("group4_2", "group4_2_R"):
            un = os.path.join(self.tmp, g, "unpacked")
            f = Fake(un)
            a = "data/backup.zip"
            f.add(a, "app.db", b"SQLite format 3\0one", "2024-01-01")
            f.add(a, "app.db", b"SQLite format 3\0two!", "2024-02-01")
            f.add(a, "app.db", b"SQLite format 3\0one", "2024-03-01")
            f.add(a, "app.db", None, "2024-04-01")              # not stored
            if g == "group4_2":
                f.add(a, "sql/Main.MDF", b"\1\0mdf", "2024-01-01")
                f.add(a, "node_modules/x/t.sqlite", b"SQLite x", "2024-01-01")
                f.add(a, "notes.txt", b"hello\n", "2024-01-01")  # not 4.1
            for r in f.rows:
                r["bytes"] = str(len(r["_data"] or b""))
            f.save()
            self.runs.append(un)
        self.out = os.path.join(self.tmp, "list", "inner_paths.csv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list(self):
        cmd = [sys.executable, os.path.join(HERE, "group4_2_path_list.py"),
               "--extensions", "db,mdf,sqlite", "--out", self.out]
        for un in self.runs:
            cmd += ["--unpacked", un]
        p = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = read_csv(self.out)
        got = {(r["run"], r["inner_path"]): r for r in rows}
        self.assertEqual(sorted(got), [
            ("group4_2", "app.db"), ("group4_2", "node_modules/x/t.sqlite"),
            ("group4_2", "sql/Main.MDF"), ("group4_2_R", "app.db")])
        db = got[("group4_2", "app.db")]
        self.assertEqual((db["archive_path"], db["ext"], db["versions"],
                          db["contents"], db["not_stored"], db["first_date"],
                          db["last_date"], db["max_bytes"], db["left_out"]),
                         ("data/backup.zip", "db", "4", "2", "1",
                          "2024-01-01", "2024-04-01", "20", ""))
        self.assertTrue(db["stored_as"].startswith("files/db/"))
        self.assertEqual(got[("group4_2", "node_modules/x/t.sqlite")]["left_out"],
                         "vendored")
        self.assertEqual(got[("group4_2", "sql/Main.MDF")]["ext"], "mdf")
        by = {(r["run"], r["ext"]): r for r in read_csv(
            self.out[:-4] + "_by_extension.csv")}
        self.assertEqual(by[("group4_2", "db")]["versions"], "4")
        self.assertEqual(by[("group4_2", "sqlite")]["left_out_paths"], "1")
        t = by[("total", "")]
        self.assertEqual((t["repos"], t["paths"], t["versions"]), ("2", "4", "10"))
        self.assertTrue(os.path.isfile(self.out[:-4] + "_legend.csv"))


if __name__ == "__main__":
    unittest.main()
