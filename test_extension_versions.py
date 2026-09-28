#!/usr/bin/env python3
"""
test_extension_versions.py - end-to-end checks for extension_versions.py.

Builds a throwaway archive (three repos: one with branches, a merge, an LFS
stub, a vendored file and a copy; one with no active copy; one git cannot
open) and an active copy next to it, then runs pass 1, pass 2 and the
combine and checks every number. Also simulates the active copy's mount
dropping mid-run. Needs only python3 and git.

    python3 test_extension_versions.py
"""

import csv
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_smoke import git, read_csv, run          # noqa: E402

X = [b"\x00XLSX-%d" % i for i in range(1, 5)]
PNG = b"\x89PNG\x00" + b"a" * 50
PNG_ACTIVE = b"\x89PNG\x00" + b"b" * 50           # same size, other bytes
JA, JB = b"jpgA\x00" * 10, b"jpgB\x00" * 10        # same size
DECK1, DECK2 = b"\x00deck-one", b"\x00deck-two-longer"
REAL_MPG = b"\x00\x01" * 1000
POINTER = ("version https://git-lfs.github.com/spec/v1\n"
           "oid sha256:%s\nsize %d\n"
           % (hashlib.sha256(REAL_MPG).hexdigest(), len(REAL_MPG))).encode()


def load(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def put(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode())


def commit(repo, msg, files=(), rm=()):
    for rel, data in files:
        put(repo, rel, data)
    for rel in rm:
        git(repo, "rm", "-q", rel)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


def build(tmp):
    arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "active")
    a = os.path.join(arch, "org1", "repoA")
    os.makedirs(a)
    git(a, "init", "-q", "-b", "master")
    commit(a, "c1", [("docs/report.xlsx", X[0]), ("logo.png", PNG),
                     ("Gemfile.lock", "a\n"), ("infra/main.tfvars", "x=1\n"),
                     ("README.md", "hi\n"), ("a.jpg", JA), ("b.jpg", JB),
                     ("notes.msg", b"msg1\x00")])
    commit(a, "c2", [("docs/report.xlsx", X[1]), ("Gemfile.lock", "a\nb\n")])
    commit(a, "c3", [("node_modules/pkg/font.woff", b"\x00woff"),
                     ("Old.DOCX", b"\x00docx"), ("big.mpg", POINTER)])
    commit(a, "c4", rm=["Old.DOCX"])
    git(a, "checkout", "-qb", "side")
    commit(a, "s1", [("docs/report.xlsx", X[2]), ("deck.pptx", DECK1)])
    git(a, "checkout", "-q", "master")
    git(a, "merge", "-q", "-s", "ours", "-m", "merge ours", "side")
    git(a, "branch", "-qD", "side")                 # reachable via the merge only
    commit(a, "c5", [("docs/report.xlsx", X[3]), ("deck.pptx", DECK2),
                     ("copy/logo_copy.png", PNG)])

    b = os.path.join(arch, "org1", "repoB")          # no active copy
    os.makedirs(b)
    git(b, "init", "-q", "-b", "master")
    commit(b, "b1", [("logo.png", PNG), ("c.xls", b"\x00xls")])

    c = os.path.join(arch, "org1", "repoC")          # git cannot open it
    os.makedirs(c)
    git(c, "init", "-q", "-b", "master")
    commit(c, "r1", [("r.groovy", "println 1\n")])
    os.remove(os.path.join(c, ".git", "HEAD"))
    shutil.rmtree(os.path.join(c, ".git", "refs"))
    os.makedirs(os.path.join(c, ".git", "refs"))

    ra = os.path.join(act, "org1", "repoA")
    for rel, data in [("docs/report.xlsx", X[3]), ("logo.png", PNG_ACTIVE),
                      ("copy/logo_copy.png", PNG), ("a.jpg", JB),
                      ("notes.msg", b"a different, longer msg\x00"),
                      ("node_modules/pkg/font.woff", b"\x00woff"),
                      ("big.mpg", REAL_MPG), ("deck.pptx", DECK1),
                      ("Gemfile.lock", "a\nb\n"), ("infra/main.tfvars", "x=1\n"),
                      ("extra.bicep", "param x\n"), ("docs/new.xlsx", b"\x00new"),
                      (".git/skip.png", PNG)]:
        put(ra, rel, data)
    put(os.path.join(act, "org1", "repoC"), "r.groovy", "println 1\n")
    batch = os.path.join(tmp, "B01.csv")
    with open(batch, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "tier", "weight", "listed_files"])
        for r in ("repoA", "repoB", "repoC"):
            w.writerow(["org1", r, "small", 1, 1])
    return arch, act, batch


class ExtensionVersions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="extv_")
        cls.arch, cls.act, cls.batch = build(cls.tmp)
        cls.out = os.path.join(cls.tmp, "out")
        base = ["--batch", cls.batch, "--repos-root", cls.arch,
                "--active-root", cls.act, "--out", cls.out, "--workers", "2"]
        cls.p1 = run("extension_versions.py", *base)
        cls.p2 = run("extension_versions.py", *base, "--identical")
        cls.ext = {r["ext"]: r for r in
                   read_csv(os.path.join(cls.out, "by_extension.csv"))}
        cls.files = {(r["repo"], r["path"]): r for r in
                     read_csv(os.path.join(cls.out, "all_files.csv"))}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def e(self, ext, col):
        return self.ext[ext][col]

    def test_every_repo_done(self):
        for r in ("repoA", "repoB", "repoC"):
            d = json.loads(load(self.out, "_state", "org1", r,
                                            "done.json"))
            self.assertEqual(d["status"], "ok", d)
        c = json.loads(load(self.out, "_state", "org1", "repoC",
                                        "done.json"))
        self.assertTrue(c["recovered"])
        b = json.loads(load(self.out, "_state", "org1", "repoB",
                                        "done.json"))
        self.assertFalse(b["active_copy"])

    def test_versions_across_branches(self):
        # 4 contents, one only on a deleted branch merged with -s ours
        self.assertEqual(self.e("xlsx", "commits"), "4")
        self.assertEqual(self.e("xlsx", "versions_per_file"), "4")
        self.assertEqual(self.e("xlsx", "versions_beyond_head"), "3")
        self.assertEqual(self.e("xlsx", "same_as_latest"), "1")
        self.assertEqual(self.e("xlsx", "versions_to_send"), "5")  # 4 + active-only
        self.assertEqual(self.e("xlsx", "files_active_only"), "1")
        self.assertEqual(self.files[("repoA", "docs/report.xlsx")]["latest_blob"],
                         git(os.path.join(self.arch, "org1", "repoA"),
                             "rev-parse", "HEAD:docs/report.xlsx").strip())

    def test_dedup_levels(self):
        # repoA logo + copy (one blob), repoB logo (the same blob again)
        self.assertEqual(self.e("png", "repos"), "2")
        self.assertEqual(self.e("png", "files_in_history"), "3")
        self.assertEqual(self.e("png", "versions_per_file"), "3")
        self.assertEqual(self.e("png", "versions_per_repo"), "2")
        self.assertEqual(self.e("png", "versions_all_repos"), "1")
        self.assertEqual(self.e("png", "files_no_active_repo"), "1")
        self.assertEqual(self.e("png", "max_bytes"), str(len(PNG)))

    def test_identical_results(self):
        res = {p: self.files[("repoA", p)]["result"] for p in
               ("docs/report.xlsx", "logo.png", "copy/logo_copy.png", "a.jpg",
                "notes.msg", "node_modules/pkg/font.woff", "big.mpg",
                "deck.pptx", "b.jpg")}
        self.assertEqual(res, {
            "docs/report.xlsx": "same_as_latest", "logo.png": "no_match",
            "copy/logo_copy.png": "same_as_latest",
            "a.jpg": "same_as_other_path", "notes.msg": "no_match",
            "node_modules/pkg/font.woff": "same_as_latest",
            "big.mpg": "lfs_active_real", "deck.pptx": "same_as_older",
            "b.jpg": ""})
        self.assertEqual(self.files[("repoA", "a.jpg")]["matched_path"], "b.jpg")
        self.assertEqual(self.files[("repoA", "big.mpg")]["lfs_match"], "latest")
        ident = {r["path"]: r for r in read_csv(os.path.join(
            self.out, "_state", "org1", "repoA", "identical.csv"))}
        self.assertEqual(ident["logo.png"]["hashed"], "yes")    # size matched
        self.assertEqual(ident["notes.msg"]["hashed"], "no")    # size did not

    def test_to_send(self):
        self.assertEqual(self.e("png", "versions_to_send"), "4")   # 3 + active logo
        self.assertEqual(self.e("jpg", "versions_to_send"), "2")
        self.assertEqual(self.e("jpg", "files_history_only"), "1")
        self.assertEqual(self.e("pptx", "versions_to_send"), "2")
        self.assertEqual(self.e("mpg", "versions_to_send"), "1")   # stub 0 + real 1
        self.assertEqual(self.e("mpg", "lfs_stub_versions"), "1")
        self.assertEqual(self.e("woff", "files_vendored"), "1")
        self.assertEqual(self.e("woff", "versions_to_send"), "1")
        self.assertEqual(self.e("xls", "versions_to_send"), "1")
        self.assertEqual(self.e("msg", "versions_to_send"), "2")  # + no_match
        self.assertEqual(self.e("docx", "versions_to_send"), "1")
        self.assertEqual(self.e("msg", "at_head_unchecked"), "0")

    def test_case_delete_and_text(self):
        f = self.files[("repoA", "Old.DOCX")]
        self.assertEqual((f["ext"], f["at_head"], f["commits"], f["versions"],
                          f["last_event"]), ("docx", "no", "2", "1", "D"))
        self.assertEqual(self.e("lock", "versions_per_file"), "2")
        self.assertEqual(self.e("lock", "versions_beyond_head"), "1")
        self.assertEqual(self.e("lock", "versions_to_send"), "")   # text: n/a
        self.assertEqual(self.e("bicep", "files_active_only"), "1")
        self.assertEqual(self.e("groovy", "files_at_head"), "1")
        self.assertNotIn("md", self.ext)
        self.assertNotIn(("repoA", ".git/skip.png"), self.files)

    def test_resume_and_summary(self):
        again = run("extension_versions.py", "--batch", self.batch,
                    "--repos-root", self.arch, "--active-root", self.act,
                    "--out", self.out)
        self.assertIn("0 repo(s) to process (3 done", again.stdout)
        md = load(self.out, "summary.md")
        self.assertIn("| xlsx |", md)
        self.assertIn("Binary extensions", md)


class MountDrop(unittest.TestCase):
    def test_stops_and_resumes(self):
        import extension_versions as ev
        tmp = tempfile.mkdtemp(prefix="extv_mount_")
        try:
            arch, act, batch = build(tmp)
            out = os.path.join(tmp, "out")
            argv = ["extension_versions.py", "--batch", batch, "--repos-root",
                    arch, "--active-root", act, "--out", out, "--workers", "1"]
            real = ev.repo_exists

            def dead(root, org, repo):
                raise ev.MountDown(OSError(errno.ENOTCONN, "Transport "
                                           "endpoint is not connected"))
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(ev, "repo_exists", dead):
                self.assertEqual(ev.main(), 3)
            st = os.path.join(out, "_state", "org1")
            self.assertFalse(os.path.exists(os.path.join(st, "repoA",
                                                         "done.json")))
            self.assertFalse(os.path.exists(os.path.join(st, "repoB")))
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(ev, "repo_exists", real):
                self.assertEqual(ev.main(), 0)
            for r in ("repoA", "repoB", "repoC"):
                self.assertTrue(os.path.isfile(os.path.join(st, r,
                                                            "done.json")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
