#!/usr/bin/env python3
"""
test_group4_2_text.py - group4_2_text.py on a small made-up archive_unpack.py
output: lines merged across versions of one path, a content read once,
UTF-16 and cp1252 text, a binary version, a version not stored, macOS /
DocFX / vendored paths left out, '..' in a path inside, other types
ignored, resume and stats.

    python3 test_group4_2_text.py
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import archive_unpack as au                                  # noqa: E402

ORG, REPO = "ey-org", "demo"


def read_csv(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Fake:
    """Builds files/ and _state/<org>/<repo>/manifest.csv like archive_unpack."""

    def __init__(self, root):
        self.root, self.rows, self.n = root, [], 0

    def add(self, archive, inner, data, date, action=None):
        path = archive + "/" + inner
        ext = au.ext_key(inner)
        stored = ""
        if data is not None:
            self.n += 1
            stored = "files/%s/%s/%s/%040d.%s" % (ext, ORG, REPO, self.n, ext)
            for r in self.rows:      # same content in this repo: deduped
                if r["_data"] == data:
                    stored, action = r["stored_as"], action or "deduped"
                    break
            else:
                fp = os.path.join(self.root, stored)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "wb") as fh:
                    fh.write(data)
        row = {k: "" for k in au.MANIFEST_HEADER}
        row.update(org=ORG, repo=REPO, path=path, ext=ext, stored_as=stored,
                   action=action or ("written" if data is not None else "error"),
                   first_date=date, archive_path=archive, inner_path=inner,
                   _data=data)
        self.rows.append(row)

    def save(self):
        sd = os.path.join(self.root, "_state", ORG, REPO)
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "manifest.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.DictWriter(fh, au.MANIFEST_HEADER, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.rows)
        with open(os.path.join(sd, "done.json"), "w") as fh:
            json.dump({"status": "ok"}, fh)


class GroupText(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.un = os.path.join(self.tmp, "unpacked")
        self.out = os.path.join(self.tmp, "group1_text")
        f = Fake(self.un)
        a = "docs/pack.zip"
        f.add(a, "cfg/app.json", b'  {"a": 1,\n  "user": "jane"\n}\n', "2024-01-01")
        f.add(a, "cfg/app.json", b'{"a": 2,\n"user": "jane"\n\n}\n', "2024-02-01")
        f.add(a, "cfg/app.json", b'  {"a": 1,\n  "user": "jane"\n}\n', "2024-03-01")
        f.add(a, "cfg/app.json", None, "2024-04-01")              # error row
        f.add(a, "notes.txt", "caf\xe9 line\n".encode("cp1252"), "2024-01-01")
        f.add(a, "w.sql", "select 1\r\n".encode("utf-16"), "2024-01-01")
        f.add(a, "k.jks", b"\xfe\xed\xfe\xed\0\0\0\x02", "2024-01-01")
        f.add(a, "__MACOSX/cfg/._app.json", b"\0\x05\x16\x07", "2024-01-01")
        f.add(a, "site/obj/.cache/build/x.json", b"{}\n", "2024-01-01")
        f.add(a, "web/node_modules/lib/index.js", b"var x\n", "2024-01-01")
        f.add(a, "../../escape.txt", b"climb\n", "2024-01-01")
        f.add(a, "pic.png", b"\x89PNG\0", "2024-01-01")           # not group 1
        f.save()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_it(self, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "group4_2_text.py"),
             "--unpacked", self.un, "--out", self.out, "--workers", "2"]
            + list(extra), capture_output=True, text=True)

    def test_all(self):
        p = self.run_it()
        self.assertEqual(p.returncode, 0, p.stderr)
        base = os.path.join(self.out, ORG, REPO, "docs", "pack.zip")
        with open(os.path.join(base, "cfg", "app.json.txt")) as fh:
            self.assertEqual(fh.read().splitlines(),
                             ['{"a": 1,', '"user": "jane"', '}', '{"a": 2,'])
        with open(os.path.join(base, "notes.txt.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "caf\xe9 line\n")
        with open(os.path.join(base, "w.sql.txt")) as fh:
            self.assertEqual(fh.read(), "select 1\n")
        self.assertTrue(os.path.isfile(os.path.join(base, "_..", "_..",
                                                    "escape.txt.txt")))
        m = {r["path"].split("/", 2)[2]: r for r in read_csv(
            os.path.join(self.out, "_state", ORG, REPO, "manifest.csv"))}
        app = m["cfg/app.json"]
        self.assertEqual((app["status"], app["versions"], app["contents"],
                          app["not_stored"]), ("ok", "4", "2", "1"))
        self.assertEqual(m["k.jks"]["status"], "binary")
        self.assertNotIn("pic.png", m)
        left = {r["path"].split("/", 2)[2]: r["reason"] for r in read_csv(
            os.path.join(self.out, "_state", ORG, REPO, "left_out.csv"))}
        self.assertEqual(left, {"__MACOSX/cfg/._app.json": "macos",
                                "site/obj/.cache/build/x.json": "docfx_cache",
                                "web/node_modules/lib/index.js": "vendored"})
        with open(os.path.join(self.out, "stats", "summary.json")) as fh:
            s = json.load(fh)
        self.assertEqual(s["files_written"], 4)
        self.assertEqual(s["left_out"], {"macos": 1, "docfx_cache": 1,
                                         "vendored": 1})
        bt = {r["type"]: r for r in read_csv(
            os.path.join(self.out, "stats", "by_type.csv"))}
        self.assertEqual(bt["json"]["status_ok"], "1")
        self.assertEqual(bt["json"]["left_docfx_cache"], "1")
        # resume: nothing to do the second time
        p = self.run_it()
        self.assertIn("(0 to do)", p.stderr)
        p = self.run_it("--redo", "--repo", ORG + "/" + REPO)
        self.assertIn("(1 to do)", p.stderr)


if __name__ == "__main__":
    unittest.main()
