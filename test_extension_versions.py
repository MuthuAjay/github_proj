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
from collections import Counter
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


def unlink_refs(repo):
    """Leave only the object store, as in the archive: no HEAD, no refs."""
    os.remove(os.path.join(repo, ".git", "HEAD"))
    shutil.rmtree(os.path.join(repo, ".git", "refs"))
    os.makedirs(os.path.join(repo, ".git", "refs"))


def build(tmp):
    arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "active")
    a = os.path.join(arch, "org1", "repoA")
    os.makedirs(a)
    git(a, "init", "-q", "-b", "master")
    commit(a, "c1", [("docs/report.xlsx", X[0]), ("logo.png", PNG),
                     ("Gemfile.lock", "a\n"), ("infra/main.tfvars", "x=1\n"),
                     ("README.md", "hi\n"), ("a.jpg", JA), ("b.jpg", JB),
                     ("docs/old.rst", "Contact: jane@example.com\n"),
                     ("notes.msg", b"msg1\x00")])
    commit(a, "c2", [("docs/report.xlsx", X[1]), ("Gemfile.lock", "a\nb\n")])
    commit(a, "c3", [("node_modules/pkg/font.woff", b"\x00woff"),
                     ("Old.DOCX", b"\x00docx"), ("big.mpg", POINTER)])
    commit(a, "c4", rm=["Old.DOCX", "docs/old.rst"])
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
    commit(b, "b1", [("logo.png", PNG), ("c.xls", b"\x00xls"),
                     ("old.docx", b"\x00docx"),       # = repoA's Old.DOCX
                     ("empty.pptx", b"")])
    unlink_refs(b)                                   # like the archive

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

    def test_to_send_head_processed(self):
        # a version identical to an active file is skipped in every repo
        want = {"xlsx": 3,      # 4 minus the active one; active-only adds 0
                "png": 1,       # PNG is in repoA's active copy (the copy):
                                # repoA's logo and copy skip it; repoB has no
                                # active copy, so its logo is sent
                "jpg": 1,       # a.jpg is b.jpg's content: JB skipped, JA sent
                "pptx": 2,      # DECK1 is active (same_as_older), DECK2
                                # sent; + repoB's empty.pptx
                "msg": 1,       # no_match: the one archived version
                "woff": 0, "mpg": 0,          # mpg: only an LFS stub
                "xls": 1,
                "docx": 2}      # repoA deleted Old.DOCX + repoB's copy
        got = {e: int(self.e(e, "versions_to_send")) for e in want}
        self.assertEqual(got, want)
        self.assertEqual(self.e("xlsx", "versions_to_send_all_repos"), "3")
        self.assertEqual(self.e("png", "versions_to_send_all_repos"), "1")
        self.assertEqual(self.e("lock", "versions_to_send"), "")   # text: n/a

    def test_to_send_incl_head(self):
        want = {"xlsx": 5,      # 4 + the active-only new.xlsx
                "png": 4,       # 3 + the active logo (no_match)
                "jpg": 2, "pptx": 3,
                "mpg": 1,       # stub 0 + the real file in the active copy
                "woff": 1, "xls": 1,
                "msg": 2,       # 1 + the active file (no_match)
                "docx": 2}
        got = {e: int(self.e(e, "versions_to_send_incl_head")) for e in want}
        self.assertEqual(got, want)
        self.assertEqual(self.e("jpg", "files_history_only"), "1")
        self.assertEqual(self.e("mpg", "lfs_stub_versions"), "1")
        self.assertEqual(self.e("woff", "files_vendored"), "1")
        self.assertEqual(self.e("msg", "at_head_unchecked"), "0")

    def test_commit_per_version(self):
        a = os.path.join(self.arch, "org1", "repoA")
        vers = {r["blob"]: r for r in read_csv(os.path.join(
            self.out, "_state", "org1", "repoA", "versions.csv"))
            if r["path"] == "docs/report.xlsx"}
        first = git(a, "rev-parse", "HEAD~5:docs/report.xlsx").strip()   # c1
        last = git(a, "rev-parse", "HEAD:docs/report.xlsx").strip()      # c5
        self.assertEqual(vers[first]["first_commit"],
                         git(a, "rev-parse", "HEAD~5").strip())
        self.assertEqual(vers[last]["last_commit"],
                         git(a, "rev-parse", "HEAD").strip())
        self.assertTrue(vers[last]["first_date"].startswith("20"))

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


class Analysis(unittest.TestCase):
    def test_report_matches_counts(self):
        tmp = tempfile.mkdtemp(prefix="extv_an_")
        try:
            arch, act, batch = build(tmp)
            out = os.path.join(tmp, "out")
            base = ["--batch", batch, "--repos-root", arch, "--active-root",
                    act, "--out", out]
            run("extension_versions.py", *base)
            run("extension_versions.py", *base, "--identical")
            run("ext_versions_analysis.py", out)
            an = os.path.join(out, "analysis")
            md = load(an, "ext_versions_analysis.md")
            self.assertIn("All match.", md)
            self.assertIn("Empty (0-byte) contents among them: pptx 1", md)
            self.assertIn("| pass 2 ok | 3 |", md)
            reasons = {(r["ext"], r["reason"]): int(r["versions"]) for r in
                       read_csv(os.path.join(an, "to_send_by_ext_reason.csv"))}
            self.assertEqual(reasons[("xlsx", "older_version")], 3)
            self.assertEqual(reasons[("png", "no_active_repo")], 1)
            self.assertEqual(reasons[("msg", "active_differs")], 1)
            self.assertEqual(reasons[("docx", "history_only")], 1)
            locks = read_csv(os.path.join(an, "lock_files.csv"))
            self.assertEqual(locks[0]["file_name"], "gemfile.lock")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Extract(unittest.TestCase):
    """extract_versions.py on the counts of the same archive."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="xv_")
        cls.arch, cls.act, cls.batch = build(cls.tmp)
        cls.state = os.path.join(cls.tmp, "counts")
        base = ["--batch", cls.batch, "--repos-root", cls.arch,
                "--active-root", cls.act, "--out", cls.state]
        run("extension_versions.py", *base)
        run("extension_versions.py", *base, "--identical")
        cls.out = os.path.join(cls.tmp, "binary")
        cls.args = ["--state", cls.state, "--repos-root", cls.arch,
                    "--out", cls.out, "--batch", cls.batch, "--workers", "1",
                    "--min-free-disk-gb", "0"]
        cls.p = run("extract_versions.py", *cls.args)
        cls.rows = read_csv(os.path.join(cls.out, "manifest.csv"))
        cls.by = {(r["repo"], r["path"], r["blob"]): r for r in cls.rows}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def row(self, repo, path, data):
        return self.by[(repo, path, blob_id(data))]

    def test_only_unprocessed_versions(self):
        got = sorted((r["repo"], r["path"], r["action"]) for r in self.rows)
        self.assertEqual(got, [
            ("repoA", "Old.DOCX", "written"),
            ("repoA", "a.jpg", "written"),              # JA; JB is a.jpg now
            ("repoA", "big.mpg", "lfs_stub"),
            ("repoA", "deck.pptx", "written"),          # DECK2; DECK1 active
            ("repoA", "docs/report.xlsx", "written"),   # X1, X2, X3; X4 active
            ("repoA", "docs/report.xlsx", "written"),
            ("repoA", "docs/report.xlsx", "written"),
            ("repoA", "notes.msg", "written"),
            ("repoB", "c.xls", "written"),
            ("repoB", "empty.pptx", "empty"),
            ("repoB", "logo.png", "written"),           # repoB has no active copy
            ("repoB", "old.docx", "deduped")])          # same bytes as Old.DOCX
        self.assertNotIn(blob_id(X[3]), {r["blob"] for r in self.rows})

    def test_files_are_the_versions(self):
        for repo, path, data in (("repoA", "docs/report.xlsx", X[0]),
                                 ("repoA", "deck.pptx", DECK2),
                                 ("repoB", "logo.png", PNG)):
            r = self.row(repo, path, data)
            with open(os.path.join(self.out, r["stored_as"]), "rb") as fh:
                self.assertEqual(fh.read(), data)
        a = self.row("repoA", "Old.DOCX", b"\x00docx")
        b = self.row("repoB", "old.docx", b"\x00docx")
        self.assertEqual(a["stored_as"], b["stored_as"])      # stored once
        self.assertTrue(a["stored_as"].endswith(".docx"))

    def test_reasons_and_commits(self):
        why = {(r["repo"], r["path"]): r["why"] for r in self.rows}
        self.assertEqual(why[("repoA", "Old.DOCX")], "history_only")
        self.assertEqual(why[("repoA", "docs/report.xlsx")], "older_version")
        self.assertEqual(why[("repoA", "notes.msg")], "active_differs")
        self.assertEqual(why[("repoB", "logo.png")], "no_active_repo")
        r = self.row("repoA", "docs/report.xlsx", X[0])
        self.assertEqual(len(r["first_commit"]), 40)
        self.assertTrue(r["first_date"].startswith("20"))

    def test_summary_matches_disk_and_counts(self):
        ext = {r["ext"]: r for r in read_csv(os.path.join(self.out,
                                                          "by_extension.csv"))}
        self.assertEqual(ext["xlsx"]["files_stored"], "3")
        self.assertEqual(ext["docx"]["files_stored"], "1")
        self.assertEqual(ext["docx"]["deduped"], "1")
        for e, r in ext.items():
            self.assertEqual(r["files_stored"], r["files_on_disk"], e)
        self.assertIn("They agree for every extension.",
                      load(self.out, "summary.md"))
        counts = {r["ext"]: r for r in read_csv(os.path.join(
            self.state, "by_extension.csv"))}
        for e in ("xlsx", "pptx", "docx", "png", "jpg", "msg", "xls"):
            sent = sum(1 for r in self.rows if r["ext"] == e)
            self.assertEqual(str(sent), counts[e]["versions_to_send"], e)

    def test_resume(self):
        again = run("extract_versions.py", *self.args)
        self.assertIn("0 repo(s) to process (3 done", again.stdout)

    def test_limits(self):
        out = os.path.join(self.tmp, "limited")
        run("extract_versions.py", "--state", self.state, "--repos-root",
            self.arch, "--out", out, "--batch", self.batch,
            "--min-free-disk-gb", "0", "--max-bytes", "10")
        rows = read_csv(os.path.join(out, "manifest.csv"))
        act = Counter((r["ext"], r["action"]) for r in rows)
        self.assertEqual(act[("png", "too_large")], 1)         # 55 bytes
        self.assertEqual(act[("xlsx", "written")], 3)          # 7 bytes each
        self.assertFalse(any(r["stored_as"] for r in rows
                             if r["action"] == "too_large"))

    def test_disk_low_stops_and_resumes(self):
        import extract_versions as xv
        out = os.path.join(self.tmp, "disk")
        argv = ["extract_versions.py", "--state", self.state, "--repos-root",
                self.arch, "--out", out, "--batch", self.batch,
                "--workers", "1", "--min-free-disk-gb", "5"]
        free = iter([100.0] + [1.0] * 1000)           # fine at start, then low
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(xv, "free_gb", lambda p: next(free)):
            self.assertEqual(xv.main(), 3)
        self.assertFalse(os.path.exists(os.path.join(
            out, "_state", "org1", "repoA", "done.json")))
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(xv, "free_gb", lambda p: 100.0):
            self.assertEqual(xv.main(), 0)
        self.assertEqual(len(read_csv(os.path.join(out, "manifest.csv"))),
                         len(self.rows))

    def test_needs_pass_two(self):
        tmp = tempfile.mkdtemp(prefix="xv_p1_")
        try:
            arch, act, batch = build(tmp)
            state = os.path.join(tmp, "counts")
            run("extension_versions.py", "--batch", batch, "--repos-root",
                arch, "--active-root", act, "--out", state)
            out = os.path.join(tmp, "binary")
            p = run("extract_versions.py", "--state", state, "--repos-root",
                    arch, "--out", out, "--batch", batch,
                    "--min-free-disk-gb", "0")
            self.assertIn("no_pass2", p.stdout)
            st = os.path.join(out, "_state", "org1")
            self.assertFalse(os.path.exists(os.path.join(st, "repoA",
                                                         "done.json")))
            self.assertTrue(os.path.exists(os.path.join(st, "repoB",
                                                        "done.json")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PerRepo(unittest.TestCase):
    """ext_versions_analysis.py's repo_delta.csv, with the text track's
    delta joined in."""

    def test_repo_delta(self):
        tmp = tempfile.mkdtemp(prefix="extv_repo_")
        try:
            arch, act, batch = build(tmp)
            out = os.path.join(tmp, "counts")
            base = ["--batch", batch, "--repos-root", arch, "--active-root",
                    act, "--out", out]
            run("extension_versions.py", *base)
            run("extension_versions.py", *base, "--identical")
            text = os.path.join(tmp, "text")
            run("file_added_lines.py", "--batch", batch, "--repos-root", arch,
                "--out", text, "--extensions", "lock,rst,tfvars,groovy",
                "--quiet", "--min-free-disk-gb", "0")
            run("fill_at_head.py", text, "--disk-root", act)
            run("file_delta.py", text, "--active-root", act, "--out",
                text + "_delta")
            run("ext_versions_analysis.py", out, "--text-delta", text + "_delta")
            an = os.path.join(out, "analysis")
            rd = {(r["repo"], r["ext"]): r for r in
                  read_csv(os.path.join(an, "repo_delta.csv"))}
            self.assertEqual(rd[("repoA", "xlsx")]["versions_to_send"], "3")
            self.assertEqual(rd[("repoA", "xlsx")]["delta_dedup"], "3")
            # the docx content is in repoA and repoB: unique within each,
            # but in neither only
            self.assertEqual(rd[("repoB", "docx")]["delta_dedup"], "1")
            self.assertEqual(rd[("repoB", "docx")]["delta_only_in_this_repo"],
                             "0")
            self.assertEqual(rd[("repoB", "png")]["delta_only_in_this_repo"],
                             "1")
            # text: the deleted old.rst keeps its line, lock loses nothing
            self.assertEqual(rd[("repoA", "rst")]["text_files_with_delta"], "1")
            self.assertEqual(rd[("repoA", "rst")]["text_lines_kept"], "1")
            self.assertEqual(rd[("repoA", "lock")]["text_lines_kept"], "0")
            self.assertEqual(rd[("repoA", "rst")]["versions_to_send"], "")
            self.assertEqual(rd[("repoA", "xlsx")]["text_lines_kept"], "")
            tot = {r["repo"]: r for r in
                   read_csv(os.path.join(an, "repo_delta_totals.csv"))}
            self.assertEqual(tot["repoA"]["text_lines_kept"], "1")
            self.assertEqual(
                sum(int(r["binary_delta_dedup"]) for r in tot.values()),
                sum(int(r["delta_dedup"]) for r in rd.values()
                    if r["group"] == "binary"))
            self.assertIn("## Per repository",
                          load(an, "ext_versions_analysis.md"))
            # every unique file once, with one original; the originals per
            # repo add up to the same total as the counts
            uniq = read_csv(os.path.join(an, "unique_files.csv"))
            counts = read_csv(os.path.join(out, "by_extension.csv"))
            total = sum(int(r["versions_to_send_all_repos"]) for r in counts
                        if r["group"] == "binary")
            self.assertEqual(len(uniq), total)
            self.assertEqual(sum(int(r["binary_delta_original"])
                                 for r in tot.values()), total)
            docx = [r for r in uniq if r["ext"] == "docx"]
            self.assertEqual(len(docx), 1)                  # in two repos
            self.assertEqual((docx[0]["repos"], docx[0]["occurrences"]),
                             ("2", "2"))
            self.assertEqual((docx[0]["original_repo"],
                              docx[0]["original_path"]), ("repoA", "Old.DOCX"))
            self.assertEqual(docx[0]["stored_as"],
                             "files/docx/%s/%s.docx" % (docx[0]["blob"][:2],
                                                        docx[0]["blob"]))
            self.assertEqual(rd[("repoA", "docx")]["delta_original"], "1")
            self.assertEqual(rd[("repoB", "docx")]["delta_original"], "0")
            self.assertEqual(rd[("repoB", "docx")]["delta_dedup"], "1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DeltaSummary(unittest.TestCase):
    """extension_delta_summary.py from the counts, the text delta and the
    extraction of the same archive."""

    def test_summary(self):
        tmp = tempfile.mkdtemp(prefix="extv_sum_")
        try:
            arch, act, batch = build(tmp)
            counts = os.path.join(tmp, "counts")
            base = ["--batch", batch, "--repos-root", arch, "--active-root",
                    act, "--out", counts]
            run("extension_versions.py", *base)
            run("extension_versions.py", *base, "--identical")
            text = os.path.join(tmp, "text")
            run("file_added_lines.py", "--batch", batch, "--repos-root", arch,
                "--out", text, "--extensions", "lock,rst,tfvars,groovy",
                "--quiet", "--min-free-disk-gb", "0")
            run("fill_at_head.py", text, "--disk-root", act)
            run("file_delta.py", text, "--active-root", act, "--out",
                text + "_delta")
            binary = os.path.join(tmp, "binary")
            run("extract_versions.py", "--state", counts, "--repos-root", arch,
                "--out", binary, "--batch", batch, "--min-free-disk-gb", "0")

            # counts only
            out1 = os.path.join(tmp, "s1.csv")
            run("extension_delta_summary.py", counts, "--out", out1)
            s1 = {r["ext"]: r for r in read_csv(out1)}
            self.assertEqual(s1["xlsx"]["versions"], "4")
            self.assertEqual(s1["xlsx"]["versions_processed(in_active)"], "1")
            self.assertEqual(s1["xlsx"]["versions_to_send"], "3")
            self.assertEqual(s1["xlsx"]["delta_dedup"], "3")
            self.assertEqual(s1["mpg"]["lfs_stubs(not_in_archive)"], "1")
            self.assertEqual(s1["rst"]["delta_dedup"], "")
            self.assertIn("--text-delta", s1["rst"]["note"])

            # with the text delta and the extraction
            out2 = os.path.join(tmp, "s2.csv")
            run("extension_delta_summary.py", counts, "--out", out2,
                "--text-delta", text + "_delta", "--extraction", binary)
            s2 = {r["ext"]: r for r in read_csv(out2)}
            self.assertEqual((s2["rst"]["delta_dedup"],
                              s2["rst"]["delta_lines"]), ("1", "1"))
            self.assertEqual(s2["lock"]["delta_lines"], "0")
            self.assertEqual(s2["xlsx"]["extracted_files"], "3")
            self.assertEqual(s2["pptx"]["delta_dedup"], "2")     # 1 + empty
            self.assertEqual(s2["pptx"]["extracted_files"], "1") # empty not
            bin_rows = [r for e, r in s2.items()
                        if r["group"] == "binary"]
            self.assertEqual(s2["total"]["delta_dedup"],
                             str(sum(int(r["delta_dedup"]) for r in bin_rows)))
            self.assertEqual(s2["total"]["delta_lines"], "1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PassOneOnly(unittest.TestCase):
    def test_latest_assumed_processed(self):
        tmp = tempfile.mkdtemp(prefix="extv_p1_")
        try:
            arch, act, batch = build(tmp)
            out = os.path.join(tmp, "out")
            run("extension_versions.py", "--batch", batch, "--repos-root", arch,
                "--active-root", act, "--out", out)
            ext = {r["ext"]: r for r in
                   read_csv(os.path.join(out, "by_extension.csv"))}
            # no pass 2: each binary file's latest version counts as the
            # active one - pptx then sends DECK1, not DECK2 (pass 2 corrects)
            self.assertEqual(ext["xlsx"]["versions_to_send"], "3")
            self.assertEqual(ext["png"]["versions_to_send"], "1")   # repoB
            self.assertEqual(ext["pptx"]["versions_to_send"], "2")
            self.assertEqual(ext["xlsx"]["at_head_unchecked"], "1")
            self.assertEqual(ext["xlsx"]["at_head_checked"], "0")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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
