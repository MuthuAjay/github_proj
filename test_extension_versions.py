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
from collections import Counter, defaultdict
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


def ext_counts(path):
    """extension_counts.csv rows up to the legend."""
    out = []
    for r in read_csv(path):
        if r["ext"] == "column":
            break
        if r["ext"]:
            out.append(r)
    return out


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
            self.assertEqual(s1["xlsx"]["duplicates_removed"], "0")
            # the docx content is in repoA and repoB: 2 to send, 1 copy
            self.assertEqual((s1["docx"]["versions_to_send"],
                              s1["docx"]["duplicates_removed"],
                              s1["docx"]["delta_dedup"]), ("2", "1", "1"))
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


class AllExtensions(unittest.TestCase):
    """all_extension_counts.py: every extension, and for the 27 the same
    numbers as extension_versions.py."""

    def test_counts(self):
        tmp = tempfile.mkdtemp(prefix="extv_all_")
        try:
            arch, act, batch = build(tmp)
            put(os.path.join(act, "org1", "repoA"), "Makefile", "all:\n")
            ev = os.path.join(tmp, "ev")
            run("extension_versions.py", "--batch", batch, "--repos-root",
                arch, "--active-root", act, "--out", ev)
            out = os.path.join(tmp, "all")
            p = run("all_extension_counts.py", "--batch", batch,
                    "--repos-root", arch, "--active-root", act, "--out", out)
            self.assertIn("(ok 3)", p.stdout)
            got = {r["ext"]: r for r in read_csv(os.path.join(
                out, "by_extension.csv"))}
            ref = {r["ext"]: r for r in read_csv(os.path.join(
                ev, "by_extension.csv"))}
            # every extension of the archive, not only the 27
            self.assertEqual(got["md"]["files_in_history"], "1")  # README.md
            self.assertEqual(got["md"]["in_27"], "")
            self.assertEqual(got["(none)"]["files_active_only"], "1")
            self.assertEqual(got["xlsx"]["in_27"], "yes")
            # the 27: identical to extension_versions.py
            for e in ref:
                for a, b in (("files_in_history", "files_in_history"),
                             ("files_at_head", "files_at_head"),
                             ("files_history_only", "files_history_only"),
                             ("files_no_active_repo", "files_no_active_repo"),
                             ("files_active_only", "files_active_only"),
                             ("commits", "commits"),
                             ("versions", "versions_per_file"),
                             ("versions_in_repo", "versions_per_repo"),
                             ("files_vendored", "files_vendored")):
                    self.assertEqual(got[e][a], ref[e][b], (e, a))
            per = read_csv(os.path.join(out, "by_repo.csv"))
            self.assertEqual(sum(int(r["files_in_history"]) for r in per
                                 if r["ext"] == "xlsx"),
                             int(got["xlsx"]["files_in_history"]))
            self.assertIn("# All file extensions", load(out, "summary.md"))

            # history only (no active copy): ext -> files, the same counts
            quick = os.path.join(tmp, "quick")
            run("all_extension_counts.py", "--batch", batch, "--repos-root",
                arch, "--out", quick, "--history-only")
            counts = {r["ext"]: r["files"] for r in ext_counts(os.path.join(
                quick, "extension_counts.csv"))}
            want = {e: r["files_in_history"] for e, r in got.items()
                    if r["files_in_history"] != "0"}
            want["total"] = str(sum(int(v) for v in want.values()))
            groups = {r["ext"]: r["group"] for r in ext_counts(os.path.join(
                quick, "extension_counts.csv"))}
            # both version measures, as extension_versions.py counts them
            ev = {r["ext"]: r for r in ext_counts(os.path.join(
                quick, "extension_counts.csv"))}
            for e in ("xlsx", "pptx", "lock"):
                self.assertEqual(ev[e]["versions(changes)"], ref[e]["commits"])
                self.assertEqual(ev[e]["distinct_versions"],
                                 ref[e]["versions_per_file"])
            self.assertEqual((ev["xlsx"]["versions(changes)"],
                              ev["xlsx"]["distinct_versions"]), ("4", "4"))
            self.assertEqual((groups["md"], groups["xlsx"]),
                             ("group 1", "group 2"))
            self.assertEqual(counts.pop("total group 1"), "1")      # md
            self.assertEqual(counts.pop("total rest"), "0")
            self.assertEqual(counts.pop("total group 2"),
                             str(int(want["total"]) - 1))
            self.assertEqual(counts, want)
            self.assertNotIn("(none)", counts)   # Makefile: active copy only
            full_counts = {r["ext"]: r["files"] for r in ext_counts(
                os.path.join(out, "extension_counts.csv"))
                if not r["ext"].startswith("total ")}
            self.assertEqual(full_counts, counts)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class AllExtensionsOddNames(unittest.TestCase):
    """File names with a carriage return, a comma, a quote or bytes that
    are not UTF-8 count like any other; a row that does not read back is
    listed, not a crash."""

    def test_odd_names_and_bad_row(self):
        tmp = tempfile.mkdtemp(prefix="extv_odd_")
        try:
            r = os.path.join(tmp, "archive", "o", "r")
            os.makedirs(r)
            git(r, "init", "-q")
            for n in (b"a.txt", b"b.txt\r", b"Icon\r", b"c.x,y", b'd.q"t',
                      b"e.caf\xe9"):
                with open(os.path.join(r.encode(), n), "wb") as fh:
                    fh.write(b"x")
            git(r, "add", "-A")
            git(r, "commit", "-qm", "c")
            out = os.path.join(tmp, "out")
            run("all_extension_counts.py", "--repo", "o/r", "--repos-root",
                os.path.join(tmp, "archive"), "--out", out, "--history-only")
            with open(os.path.join(out, "extension_counts.csv"), newline="",
                      encoding="utf-8", errors="surrogateescape") as fh:
                counts = {}
                for row in csv.reader(fh):
                    if row and row[0] == "column":
                        break
                    if row:
                        counts[row[0]] = row[1]
            self.assertEqual(counts["txt"], "1")
            self.assertEqual(counts["txt\r"], "1")
            self.assertEqual(counts["x,y"], "1")
            self.assertEqual(counts['q"t'], "1")
            self.assertEqual(counts["total"], "6")
            self.assertEqual(len(read_csv(os.path.join(
                out, "combine_problems.csv"))), 0)

            # a damaged per-repo file: listed, the rest still combined
            with open(os.path.join(out, "_state", "o", "r", "ext_counts.csv"),
                      "a", encoding="utf-8") as fh:
                fh.write("broken\n")
            p = run("all_extension_counts.py", "--out", out, "--combine-only")
            self.assertIn("combined 1 repo(s)", p.stdout)
            self.assertIn("1 row(s)", p.stderr)
            probs = read_csv(os.path.join(out, "combine_problems.csv"))
            self.assertEqual((probs[0]["repo"], probs[0]["ext_as_read"]),
                             ("r", "'broken'"))
            self.assertIn("could not be read back", load(out, "summary.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ValidateGroup1(unittest.TestCase):
    """validate_group1_counts.py: the first extraction and the all-extension
    count agree on the same archive; a path missing from the first run is
    found and named."""

    def test_agree_then_gap(self):
        tmp = tempfile.mkdtemp(prefix="extv_val_")
        try:
            arch = os.path.join(tmp, "archive")
            r = os.path.join(arch, "o", "r")
            os.makedirs(r)
            git(r, "init", "-q")
            for n, d in (("a.json", "{}"), ("src/b.py", "x=1\n"),
                         ("db/c.sql", "select 1;\n"), ("d.txt", "t\n"),
                         ("notes.MD", "# n\n"), ("e.png", "\x00png")):
                put(r, n, d)
            git(r, "add", "-A")
            git(r, "commit", "-qm", "c1")
            git(r, "rm", "-q", "db/c.sql")
            git(r, "commit", "-qm", "c2")
            r2 = os.path.join(arch, "o", "r2")
            os.makedirs(r2)
            git(r2, "init", "-q")
            put(r2, "x.yaml", "a: 1\n")
            git(r2, "add", "-A")
            git(r2, "commit", "-qm", "c")
            unlink_refs(r2)                               # like the archive
            batch = os.path.join(tmp, "B.csv")
            with open(batch, "w") as fh:
                fh.write("org,repo\no,r\no,r2\n")
            first = os.path.join(tmp, "first")
            run("file_added_lines.py", "--batch", batch, "--repos-root", arch,
                "--out", first, "--quiet", "--min-free-disk-gb", "0")
            counts = os.path.join(tmp, "counts")
            run("all_extension_counts.py", "--batch", batch, "--repos-root",
                arch, "--out", counts, "--history-only")

            out = os.path.join(tmp, "val")
            p = run("validate_group1_counts.py", "--first", first, "--counts",
                    counts, "--out", out)
            self.assertIn("difference +0; 0 repo(s) differ", p.stdout)
            rows = {r["repo"]: r for r in read_csv(os.path.join(out,
                                                               "by_repo.csv"))}
            self.assertEqual((rows["r"]["first_files"], rows["r"]["new_files"]),
                             ("5", "5"))          # json py sql txt md; not png
            self.assertEqual(rows["r2"]["new_files"], "1")

            # a gap in the first run: drop c.sql from its manifest
            mp = os.path.join(first, "_state", "o", "r", "manifest.csv")
            with open(mp, newline="") as fh:
                keep = [x for x in csv.reader(fh) if x[2] != "db/c.sql"]
            with open(mp, "w", newline="") as fh:
                csv.writer(fh).writerows(keep)
            p = run("validate_group1_counts.py", "--first", first, "--counts",
                    counts, "--out", out, "--repos-root", arch, "--drill", "5")
            self.assertIn("difference +1; 1 repo(s) differ", p.stdout)
            pd = read_csv(os.path.join(out, "paths_diff.csv"))
            self.assertEqual([(x["repo"], x["path"], x["in_first_run"],
                               x["in_history_now"]) for x in pd],
                             [("r", "db/c.sql", "no", "yes")])
            ext = {x["ext"]: x for x in read_csv(os.path.join(
                out, "by_extension.csv"))}
            self.assertEqual(ext["sql"]["diff"], "1")
            self.assertIn("## Drill: exact paths", load(out, "summary.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DeltaByExtension(unittest.TestCase):
    """delta_by_extension.py on a real line-level chain: file_added_lines,
    fill_at_head, file_delta."""

    def test_group1_delta(self):
        tmp = tempfile.mkdtemp(prefix="extv_dbe_")
        try:
            arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "act")
            r = os.path.join(arch, "o", "r")
            os.makedirs(r)
            git(r, "init", "-q")
            for n, d in (("a.json", "a\nb\n"), ("db/c.sql", "select 1;\n"),
                         ("notes.md", "# n\n"), ("d.txt", "t\n"),
                         ("e.png", "\x00png")):
                put(r, n, d)
            git(r, "add", "-A")
            git(r, "commit", "-qm", "c1")
            put(r, "a.json", "a\nc\n")                     # b -> c
            git(r, "rm", "-q", "db/c.sql")
            git(r, "commit", "-qam", "c2")
            for n, d in (("a.json", "a\nc\n"), ("notes.md", "# n\n"),
                         ("d.txt", "t\n"), ("e.png", "\x00png")):
                put(os.path.join(act, "o", "r"), n, d)
            r2 = os.path.join(arch, "o", "r2")               # no active copy
            os.makedirs(r2)
            git(r2, "init", "-q")
            put(r2, "x.yaml", "k: 1\n")
            git(r2, "add", "-A")
            git(r2, "commit", "-qm", "c")
            batch = os.path.join(tmp, "B.csv")
            with open(batch, "w") as fh:
                fh.write("org,repo\no,r\no,r2\no,gone\n")     # gone: fails
            ext = os.path.join(tmp, "extract")
            run("file_added_lines.py", "--batch", batch, "--repos-root", arch,
                "--out", ext, "--quiet", "--min-free-disk-gb", "0")
            run("fill_at_head.py", ext, "--disk-root", act)
            run("file_delta.py", ext, "--active-root", act, "--out",
                ext + "_delta")
            out = os.path.join(tmp, "g1.csv")
            p = run("delta_by_extension.py", "--extract", ext, "--delta",
                    ext + "_delta", "--out", out)
            self.assertIn("repo_missing 1", p.stdout)
            with open(out, newline="") as fh:
                rows = {x[0]: x for x in csv.reader(fh) if x}
            head = rows["ext"]
            g = {e: dict(zip(head, rows[e])) for e in
                 ("json", "sql", "md", "txt", "yaml", "total")}
            # a.json: lines a, b, c ever; a, c still there -> b is sent
            self.assertEqual((g["json"]["lines_in_history"],
                              g["json"]["lines_removed"],
                              g["json"]["lines_sent"],
                              g["json"]["files_sent"]), ("3", "2", "1", "1"))
            # deleted c.sql: whole history sent
            self.assertEqual((g["sql"]["files_history_only"],
                              g["sql"]["lines_sent"], g["sql"]["files_sent"]),
                             ("1", "1", "1"))
            # unchanged files: nothing left
            self.assertEqual(g["md"]["files_nothing_left"], "1")
            self.assertEqual(g["txt"]["files_nothing_left"], "1")
            # a repo with no active copy: whole history sent
            self.assertEqual((g["yaml"]["files_no_active_repo"],
                              g["yaml"]["files_sent"]), ("1", "1"))
            self.assertNotIn("png", rows)                   # not group 1
            self.assertEqual((g["total"]["repos"],
                              g["total"]["files_in_history"],
                              g["total"]["files_sent"],
                              g["total"]["lines_sent"]), ("2", "5", "3", "3"))
            self.assertIn("Line-level delta", load(out + ".md"))
            self.assertIn("delta per repo: ok 2", p.stdout)
            self.assertEqual(g["total"]["files_no_delta"], "0")

            # a repo not (yet) through the delta: its files are flagged
            shutil.rmtree(os.path.join(ext + "_delta", "_state", "o", "r2"))
            p = run("delta_by_extension.py", "--extract", ext, "--delta",
                    ext + "_delta", "--out", out)
            self.assertIn("not run 1", p.stdout)
            self.assertIn("1 file(s) have no delta result", p.stdout)
            with open(out, newline="") as fh:
                rows = {x[0]: x for x in csv.reader(fh) if x}
            y = dict(zip(rows["ext"], rows["yaml"]))
            self.assertEqual((y["files_no_delta"], y["files_sent"]), ("1", "0"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SentDuplicates(unittest.TestCase):
    """sent_duplicates.py on a real line-level chain."""

    def test_js_duplicates(self):
        tmp = tempfile.mkdtemp(prefix="extv_dup_")
        try:
            arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "act")
            lib = "function a(){}\nvar b = 1;\n"
            for name, files in (
                    ("r1", [("lib/jq.js", lib), ("app.js", "own1\n")]),
                    ("r2", [("vendor/jq.js", lib),
                            ("node_modules/x/jq.js", lib)])):
                r = os.path.join(arch, "o", name)
                os.makedirs(r)
                git(r, "init", "-q")
                for n, d in files:
                    put(r, n, d)
                git(r, "add", "-A")
                git(r, "commit", "-qm", "c")
                os.makedirs(os.path.join(act, "o", name))   # repo exists,
            batch = os.path.join(tmp, "B.csv")               # files deleted
            with open(batch, "w") as fh:
                fh.write("org,repo\no,r1\no,r2\n")
            ext = os.path.join(tmp, "extract")
            run("file_added_lines.py", "--batch", batch, "--repos-root", arch,
                "--out", ext, "--quiet", "--min-free-disk-gb", "0")
            put(os.path.join(act, "o", "r1"), "keep.txt", "x")
            put(os.path.join(act, "o", "r2"), "keep.txt", "x")
            run("fill_at_head.py", ext, "--disk-root", act)
            run("file_delta.py", ext, "--active-root", act, "--out",
                ext + "_delta")
            out = os.path.join(tmp, "js.csv")
            p = run("sent_duplicates.py", "--delta", ext + "_delta", "--ext",
                    "js", "--out", out)
            # 4 js files sent: 3 identical copies of lib + app.js
            self.assertIn("files sent 4, unique 2, duplicates 2", p.stdout)
            dups = read_csv(out)
            self.assertEqual(len(dups), 1)
            self.assertEqual((dups[0]["copies"], dups[0]["repos"],
                              dups[0]["vendored_copies"]), ("3", "2", "2"))
            md = load(out + ".md")
            self.assertIn("| 2-10 | 1 |", md)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ExtensionRnd(unittest.TestCase):
    """extension_rnd.py: counts, where, and content of sampled versions -
    pickles read with pickletools, never unpickled."""

    def test_rnd(self):
        import pickle
        from collections import OrderedDict
        tmp = tempfile.mkdtemp(prefix="extv_rnd_")
        try:
            arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "act")
            r = os.path.join(arch, "o", "r")
            os.makedirs(r)
            git(r, "init", "-q")
            model = pickle.dumps(OrderedDict(a=1), protocol=4)
            put(r, "models/m.pkl", model)
            put(r, "lib/site-packages/dec/abs.decTest",
                "-- abs test\nabsx001 abs 1 -> 1\n")
            put(r, "build/.done.sentinel", "")
            put(r, "x.noun", "apple\n")
            git(r, "add", "-A")
            git(r, "commit", "-qm", "c1")
            put(r, "models/m.pkl", pickle.dumps([1, 2], protocol=2))
            git(r, "commit", "-qam", "c2")
            git(r, "rm", "-q", "x.noun")
            git(r, "commit", "-qm", "c3")
            put(os.path.join(act, "o", "r"), "models/m.pkl", b"\x80\x02]q\x00.")
            batch = os.path.join(tmp, "B.csv")
            with open(batch, "w") as fh:
                fh.write("org,repo\no,r\n")
            counts = os.path.join(tmp, "counts")
            run("extension_versions.py", "--batch", batch, "--repos-root", arch,
                "--active-root", act, "--out", counts,
                "--extensions", "pkl,sentinel,decTest,noun")
            out = os.path.join(tmp, "rnd")
            run("extension_rnd.py", "--counts", counts, "--repos-root", arch,
                "--out", out)
            rows = read_csv(os.path.join(out, "samples.csv"))
            by = defaultdict(list)
            for x in rows:
                by[x["ext"]].append(x)
            self.assertEqual(sorted(by), ["dectest", "noun", "pkl", "sentinel"])
            self.assertEqual(len(by["pkl"]), 2)                # two versions
            pk = {x["detail"] for x in by["pkl"]}
            self.assertTrue(any("collections.OrderedDict" in d for d in pk), pk)
            self.assertTrue(any("protocol 2" in d for d in pk), pk)
            self.assertEqual({x["kind"] for x in by["pkl"]}, {"binary"})
            self.assertEqual(by["dectest"][0]["kind"], "text")
            self.assertIn("abs test", by["dectest"][0]["preview"])
            self.assertEqual(by["sentinel"][0]["kind"], "empty")
            md = load(out, "rnd_report.md")
            self.assertIn("## .pkl", md)
            self.assertIn("`collections.OrderedDict`", md)
            self.assertIn("| vendored files (node_modules, site-packages, ...) "
                          "| 1 |", md)                     # the decTest
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SampleExtFiles(unittest.TestCase):
    """sample_ext_files.py: real files saved per extension; pickles get an
    .inspect.txt with their classes and strings, never unpickled."""

    def test_samples(self):
        import pickle
        from collections import OrderedDict
        tmp = tempfile.mkdtemp(prefix="extv_smp_")
        try:
            arch = os.path.join(tmp, "archive")
            for name, files in (
                    ("r1", [("data/cache.pkl", pickle.dumps(
                        {"name": "John Smith", "email": "john@example.com"},
                        protocol=4)),
                            ("t/abs.decTest", "-- abs\nabsx001 abs 1 -> 1\n"),
                            ("b/.ok.sentinel", "")]),
                    ("r2", [("m/model.pkl", pickle.dumps(OrderedDict(k=[1.5]),
                                                         protocol=2)),
                            ("wn/index.noun", "apple 1\n")])):
                r = os.path.join(arch, "o", name)
                os.makedirs(r)
                git(r, "init", "-q")
                for n, d in files:
                    put(r, n, d)
                git(r, "add", "-A")
                git(r, "commit", "-qm", "c")
            unlink_refs(os.path.join(arch, "o", "r2"))      # like the archive
            counts = os.path.join(tmp, "counts")
            batch = os.path.join(tmp, "B.csv")
            with open(batch, "w") as fh:
                fh.write("org,repo\no,r1\no,r2\n")
            run("all_extension_counts.py", "--batch", batch, "--repos-root",
                arch, "--out", counts, "--history-only")
            out = os.path.join(tmp, "smp")
            run("sample_ext_files.py", "--by-repo",
                os.path.join(counts, "by_repo.csv"), "--repos-root", arch,
                "--out", out, "--extensions", "pkl,sentinel,decTest,noun")
            idx = read_csv(os.path.join(out, "index.csv"))
            per = Counter(r["ext"] for r in idx)
            self.assertEqual(dict(per), {"pkl": 2, "sentinel": 1,
                                         "dectest": 1, "noun": 1})
            for r in idx:
                with open(os.path.join(out, r["saved_as"]), "rb") as fh:
                    self.assertEqual(len(fh.read()), int(r["bytes"]))
            cache = [r for r in idx if r["path"] == "data/cache.pkl"][0]
            insp = load(out, cache["saved_as"] + ".inspect.txt")
            self.assertIn("John Smith", insp)
            self.assertIn("john@example.com", insp)
            self.assertIn("protocol:   4", insp)
            model = [r for r in idx if r["path"] == "m/model.pkl"][0]
            self.assertIn("collections.OrderedDict",
                          load(out, model["saved_as"] + ".inspect.txt"))
            self.assertEqual([r["kind"] for r in idx if r["ext"] == "sentinel"],
                             ["empty"])
            self.assertIn("## .pkl (2 samples", load(out, "index.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RepoExtensionStatus(unittest.TestCase):
    """repo_extension_status.py on the real outputs of every stage."""

    def test_status(self):
        tmp = tempfile.mkdtemp(prefix="extv_res_")
        try:
            arch, act, batch = build(tmp)
            W = lambda *p: os.path.join(tmp, *p)          # noqa: E731
            run("all_extension_counts.py", "--batch", batch, "--repos-root",
                arch, "--out", W("counts"), "--history-only")
            for name, exts in (("g1", None), ("g2t", "lock,rst,tfvars,groovy")):
                extra = ["--extensions", exts] if exts else []
                run("file_added_lines.py", "--batch", batch, "--repos-root",
                    arch, "--out", W(name), "--quiet", "--min-free-disk-gb",
                    "0", *extra)
                run("fill_at_head.py", W(name), "--disk-root", act)
                run("file_delta.py", W(name), "--active-root", act, "--out",
                    W(name + "_delta"))
            base = ["--batch", batch, "--repos-root", arch, "--active-root",
                    act, "--out", W("ev")]
            run("extension_versions.py", *base)
            run("extension_versions.py", *base, "--identical")
            run("extract_versions.py", "--state", W("ev"), "--repos-root", arch,
                "--out", W("bin"), "--batch", batch, "--workers", "1",
                "--min-free-disk-gb", "0")
            out = W("status.csv")
            p = run("repo_extension_status.py", "--repo", "org1/repoA",
                    "--repo", "orgX/repoB", "--repo", "org1/nope",
                    "--counts", W("counts"), "--group1-extract", W("g1"),
                    "--group1-delta", W("g1_delta"), "--group2-text",
                    W("g2t_delta"), "--group2-binary", W("bin"), "--out", out)
            self.assertIn("orgX/repoB: not found as given - using org1/repoB",
                          p.stdout)
            self.assertIn("org1/nope: no repository", p.stdout)
            rows = {(r["repo"], r["ext"]): r for r in read_csv(out)}
            md = rows[("repoA", "md")]                 # README.md, gone today
            self.assertEqual((md["stage"], md["files_sent"]),
                             ("Group 1 - text", "1"))
            x = rows[("repoA", "xlsx")]
            self.assertEqual((x["stage"], x["files_in_history"],
                              x["versions(changes)"], x["files_sent"]),
                             ("Group 2 - whole files", "1", "4", "3"))
            self.assertEqual(rows[("repoA", "rst")]["files_sent"], "1")
            self.assertEqual(rows[("repoA", "lock")]["files_sent"], "0")
            # the docx of repoB is stored once (repoA's copy) - still counted
            self.assertEqual(rows[("repoB", "docx")]["files_sent"], "1")
            self.assertNotIn(("repoA", "bicep"), rows)   # active copy only
            md_text = load(out + ".md")
            self.assertIn("## org1/repoB (asked as orgX/repoB)", md_text)
            self.assertIn("| Group 2 - whole files |", md_text)

            # a repo the first extraction failed on: flagged, not "0 sent"
            with open(W("g1", "_state", "org1", "repoA", "done.json")) as fh:
                d = json.load(fh)
            d["status"] = "timeout"
            with open(W("g1", "_state", "org1", "repoA", "done.json"),
                      "w") as fh:
                json.dump(d, fh)
            run("repo_extension_status.py", "--repo", "org1/repoA",
                "--counts", W("counts"), "--group1-extract", W("g1"),
                "--group1-delta", W("g1_delta"), "--group2-text",
                W("g2t_delta"), "--group2-binary", W("bin"), "--out", out)
            rows = {(r["repo"], r["ext"]): r for r in read_csv(out)}
            self.assertEqual(rows[("repoA", "md")]["files_sent"],
                             "not sent (first extraction: timeout)")
            self.assertEqual(rows[("repoA", "xlsx")]["files_sent"], "3")
            self.assertIn("did not complete for this repository (timeout)",
                          load(out + ".md"))
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


class PathList(unittest.TestCase):
    """ext_path_list.py: unique paths + commits from a pass 1 folder."""

    def test_paths_and_commits(self):
        tmp = tempfile.mkdtemp(prefix="pathlist_")
        try:
            arch, act, batch = build(tmp)
            out = os.path.join(tmp, "out")
            run("extension_versions.py", "--batch", batch, "--repos-root", arch,
                "--active-root", act, "--out", out)
            lst = os.path.join(tmp, "list", "paths.csv")
            run("ext_path_list.py", out, "--out", lst,
                "--extensions", "xlsx,docx,png,pptx,bicep")
            rows = {(r["repo"], r["path"]): r for r in read_csv(lst)}
            got = {k: (r["commits"], r["last_event"], r["still_today"])
                   for k, r in rows.items()}
            self.assertEqual(got[("repoA", "docs/report.xlsx")], ("4", "M", "yes"))
            self.assertEqual(got[("repoA", "Old.DOCX")], ("2", "D", "no"))
            self.assertEqual(got[("repoB", "old.docx")], ("1", "A", ""))
            self.assertNotIn("distinct_versions", read_csv(lst)[0])
            self.assertEqual(len(rows), 8)
            self.assertNotIn(("repoA", "extra.bicep"), rows)    # today only
            by = {r["ext"]: r for r in read_csv(os.path.join(
                tmp, "list", "paths_by_extension.csv"))}
            self.assertEqual((by["total"]["paths"], by["total"]["commits"]),
                             ("8", "13"))
            self.assertEqual(by["png"]["repos"], "2")
            self.assertTrue(os.path.exists(os.path.join(tmp, "list",
                                                        "paths_legend.csv")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


PDF = [b"%PDF-1.4\n% version " + str(i).encode() + b"\n%%EOF\n" for i in range(1, 4)]
PKL = [b"\x80\x04\x95" + b"pickle-%d" % i + b"\x94." for i in range(1, 3)]


def build_group3(tmp):
    """repoA: report.pdf in 3 versions (v3 active), the same v1 bytes under a
    second path, model.pkl in 2 versions (no active copy of it); repoB (no
    active copy): the same v1 pdf as repoA - per repo it is stored again."""
    arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "active")
    a = os.path.join(arch, "org1", "repoA")
    os.makedirs(a)
    git(a, "init", "-q", "-b", "master")
    commit(a, "c1", [("docs/report.pdf", PDF[0]), ("copy/Report.PDF", PDF[0]),
                     ("model.pkl", PKL[0]), ("notes.xlsx", b"\x00x")])
    commit(a, "c2", [("docs/report.pdf", PDF[1]), ("model.pkl", PKL[1])])
    commit(a, "c3", [("docs/report.pdf", PDF[2])])
    b = os.path.join(arch, "org1", "repoB")
    os.makedirs(b)
    git(b, "init", "-q", "-b", "master")
    commit(b, "b1", [("old/report.pdf", PDF[0])])
    unlink_refs(b)
    put(os.path.join(act, "org1", "repoA"), "docs/report.pdf", PDF[2])
    put(os.path.join(act, "org1", "repoA"), "copy/Report.PDF", PDF[0])
    batch = os.path.join(tmp, "B01.csv")
    with open(batch, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "tier", "weight", "listed_files"])
        for r in ("repoA", "repoB"):
            w.writerow(["org1", r, "small", 1, 1])
    return arch, act, batch


class Group3(unittest.TestCase):
    """--binary-exts (counts) and --per-repo (extraction) for pdf and pkl."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="g3_")
        cls.arch, cls.act, cls.batch = build_group3(cls.tmp)
        cls.state = os.path.join(cls.tmp, "counts")
        base = ["--batch", cls.batch, "--repos-root", cls.arch, "--active-root",
                cls.act, "--out", cls.state, "--extensions", "pdf,pkl"]
        run("extension_versions.py", *base, "--binary-exts", "pdf,pkl")
        run("extension_versions.py", *base, "--identical")   # not told again
        cls.out = os.path.join(cls.tmp, "g3")
        cls.args = ["--state", cls.state, "--repos-root", cls.arch,
                    "--out", cls.out, "--batch", cls.batch, "--workers", "1",
                    "--min-free-disk-gb", "0", "--extensions", "pdf,pkl"]
        run("extract_versions.py", *cls.args, "--per-repo")
        cls.rows = read_csv(os.path.join(cls.out, "manifest.csv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_counted_as_binary_and_remembered(self):
        self.assertEqual(load(self.state, "binary_exts.txt").split(),
                         ["pdf", "pkl"])
        files = read_csv(os.path.join(self.state, "_state", "org1", "repoA",
                                      "files.csv"))
        self.assertEqual({f["path"]: f["group"] for f in files},
                         {"docs/report.pdf": "binary", "copy/Report.PDF": "binary",
                          "model.pkl": "binary"})        # xlsx not asked for
        ident = read_csv(os.path.join(self.state, "_state", "org1", "repoA",
                                      "identical.csv"))
        self.assertEqual({r["path"]: r["result"] for r in ident},
                         {"docs/report.pdf": "same_as_latest",
                          "copy/Report.PDF": "same_as_latest"})
        run("extension_versions.py", "--out", self.state, "--combine-only")
        ext = {r["ext"]: r for r in
               read_csv(os.path.join(self.state, "by_extension.csv"))}
        self.assertEqual(ext["pdf"]["group"], "binary")
        self.assertEqual(ext["pkl"]["group"], "binary")

    def test_per_repo_layout_and_dedup(self):
        got = sorted((r["repo"], r["path"], r["blob"], r["action"], r["stored_as"])
                     for r in self.rows)
        v1, v2 = blob_id(PDF[0]), blob_id(PDF[1])
        k1, k2 = blob_id(PKL[0]), blob_id(PKL[1])
        self.assertEqual(got, sorted([
            # v3 and v1 are active in repoA: v1 is the active copy/Report.PDF,
            # so neither path sends it (same repo); v2 is sent once
            ("repoA", "docs/report.pdf", v2, "written",
             "files/pdf/org1/repoA/%s.pdf" % v2),
            ("repoA", "model.pkl", k1, "written", "files/pkl/org1/repoA/%s.pkl" % k1),
            ("repoA", "model.pkl", k2, "written", "files/pkl/org1/repoA/%s.pkl" % k2),
            # repoB has the same v1 bytes: stored again, under repoB
            ("repoB", "old/report.pdf", v1, "written",
             "files/pdf/org1/repoB/%s.pdf" % v1)]))
        for r in self.rows:
            with open(os.path.join(self.out, r["stored_as"]), "rb") as fh:
                self.assertEqual(blob_id(fh.read()), r["blob"])
        self.assertNotIn("deduped", {r["action"] for r in self.rows})
        ext = {r["ext"]: r for r in read_csv(os.path.join(self.out,
                                                          "by_extension.csv"))}
        self.assertEqual((ext["pdf"]["files_stored"], ext["pdf"]["files_on_disk"]),
                         ("2", "2"))
        self.assertEqual((ext["pkl"]["files_stored"], ext["pkl"]["files_on_disk"]),
                         ("2", "2"))

    def test_same_content_twice_in_one_repo_stored_once(self):
        tmp = tempfile.mkdtemp(prefix="g3_dup_")
        try:
            arch, act, batch = build_group3(tmp)
            os.remove(os.path.join(act, "org1", "repoA", "copy", "Report.PDF"))
            state, out = os.path.join(tmp, "counts"), os.path.join(tmp, "g3")
            base = ["--batch", batch, "--repos-root", arch, "--active-root", act,
                    "--out", state, "--extensions", "pdf", "--binary-exts", "pdf"]
            run("extension_versions.py", *base)
            run("extension_versions.py", *base, "--identical")
            run("extract_versions.py", "--state", state, "--repos-root", arch,
                "--out", out, "--batch", batch, "--min-free-disk-gb", "0",
                "--extensions", "pdf", "--per-repo")
            rows = [r for r in read_csv(os.path.join(out, "manifest.csv"))
                    if r["repo"] == "repoA" and r["blob"] == blob_id(PDF[0])]
            # v1 under two paths (docs/ old version, copy/ deleted today)
            self.assertEqual(sorted(r["path"] for r in rows),
                             ["copy/Report.PDF", "docs/report.pdf"])
            self.assertEqual(len({r["stored_as"] for r in rows}), 1)
            self.assertEqual({r["action"] for r in rows}, {"written"})
            folder = os.path.join(out, "files", "pdf", "org1", "repoA")
            self.assertEqual(len(os.listdir(folder)), 2)          # v1, v2
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resume_and_layout_guard(self):
        # an interrupted repo (no done.json) redone: its files are already
        # there - still "written", never "deduped" in the per-repo layout
        os.remove(os.path.join(self.out, "_state", "org1", "repoA", "done.json"))
        run("extract_versions.py", *self.args, "--per-repo")
        rows = read_csv(os.path.join(self.out, "manifest.csv"))
        self.assertEqual(sorted(r["action"] for r in rows), ["written"] * 4)
        p = run("extract_versions.py", *self.args, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("per_repo layout", p.stderr)

    def test_default_layout_unchanged(self):
        self.assertEqual(
            __import__("extract_versions").store_path("png", "3fab"),
            os.path.join("files", "png", "3f", "3fab.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
