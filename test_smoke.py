#!/usr/bin/env python3
"""
test_smoke.py - fast end-to-end checks for the scripts in this folder.

Builds a throwaway git repo (a rename, edits, a side branch, a real merge
commit) and runs each script against it. Needs only python3 and git; touches
nothing outside a temp directory.

    python3 test_smoke.py            # or: python3 -m unittest test_smoke -v
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
PY = sys.executable

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), env=GIT_ENV,
                          check=True, capture_output=True, text=True).stdout


def write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def run(script, *args, check=True):
    proc = subprocess.run([PY, os.path.join(HERE, script)] + list(args),
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError("%s failed (%d)\nSTDOUT:\n%s\nSTDERR:\n%s"
                             % (script, proc.returncode, proc.stdout, proc.stderr))
    return proc


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_repo(repo):
    """c1 add a.txt | c2 edit | c3 rename a->b | c4 edit b + add c |
    c5 add src/x/f.txt | c6 move it to src/y.txt | side: feat.txt + edit b |
    master edit | --no-ff merge of the side branch. wip forks at c6 and is never merged."""
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "master")
    write(repo, "a.txt", "line1\nline2\nline3\nline4\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "c1")
    write(repo, "a.txt", "more\n")
    git(repo, "commit", "-qam", "c2")
    git(repo, "mv", "a.txt", "b.txt"); git(repo, "commit", "-qm", "rename")
    write(repo, "b.txt", "3\n"); write(repo, "c.txt", "x\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "c4")
    write(repo, "src/x/f.txt", "1\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "c5")
    git(repo, "mv", "src/x/f.txt", "src/y.txt"); git(repo, "commit", "-qm", "c6")
    git(repo, "checkout", "-qb", "wip")            # forks here, never merged
    write(repo, "wip.txt", "w\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "wip1")
    git(repo, "checkout", "-q", "master")
    git(repo, "checkout", "-qb", "side")
    write(repo, "feat.txt", "z\n"); write(repo, "b.txt", "side\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "side1")
    git(repo, "checkout", "-q", "master")
    write(repo, "master2.txt", "m\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "master2")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="smoke_")
        cls.repo = os.path.join(cls.tmp, "repo")
        build_repo(cls.repo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class ExtractCommits(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.full = os.path.join(cls.tmp, "full")
        cls.only = os.path.join(cls.tmp, "only")
        cls.full_run = run("extract_commits.py", cls.repo, "--out", cls.full,
                           "--content", "changed")
        cls.only_run = run("extract_commits.py", cls.repo, "--out", cls.only,
                           "--churn-only")

    def test_commit_folders(self):
        commits = read_csv(os.path.join(self.full, "commits.csv"))
        self.assertEqual(len(commits), 10)
        for c in commits:
            d = os.path.join(self.full, c["sha"])
            for f in ("metadata.json", "tree.csv", "changes.csv", ".complete"):
                self.assertTrue(os.path.exists(os.path.join(d, f)), (c["sha"], f))
        self.assertEqual(sum(int(c["is_merge"]) for c in commits), 1)
        self.assertEqual(sum(int(c["is_root"]) for c in commits), 1)

    def test_file_churn_follows_rename(self):
        rows = {r["path"]: r for r in read_csv(os.path.join(self.full, "file_churn.csv"))}
        self.assertNotIn("a.txt", rows)                 # folded into b.txt
        b = rows["b.txt"]
        self.assertEqual(b["renamed"], "1")
        self.assertEqual(b["added"], "1")
        # add, edit, rename, edit, side edit, and the merge (first-parent diff)
        self.assertEqual(int(b["commits_touched"]), 6)
        self.assertEqual(rows["src/y.txt"]["commits_touched"], "2")
        self.assertEqual(rows["feat.txt"]["branch_count"], "2")   # side + master
        self.assertEqual(rows["wip.txt"]["branch_count"], "1")    # wip only

    def test_merge_diffed_against_first_parent(self):
        merge = next(c for c in read_csv(os.path.join(self.full, "commits.csv"))
                     if c["is_merge"] == "1")
        changes = read_csv(os.path.join(self.full, merge["sha"], "changes.csv"))
        self.assertEqual({c["path"] for c in changes}, {"b.txt", "feat.txt"})

    def test_tree_churn(self):
        rows = {r["tree"]: r for r in read_csv(os.path.join(self.full, "tree_churn.csv"))}
        self.assertEqual(set(rows), {".", "src", "src/x"})
        self.assertEqual(rows["src"]["commits_touched"], "2")
        self.assertEqual(int(rows["."]["commits_touched"]), 10)

    def test_file_history(self):
        rows = read_csv(os.path.join(self.full, "file_history.csv"))
        b = [r for r in rows if r["path"] == "b.txt"]
        self.assertEqual(max(int(r["nth_change"]) for r in b), 6)
        self.assertTrue(any(r["status"].startswith("R") and r["old_path"] == "a.txt"
                            for r in rows))

    def test_branches_on_commits(self):
        commits = read_csv(os.path.join(self.full, "commits.csv"))
        by = {c["subject"]: c for c in commits}
        self.assertEqual(by["wip1"]["branches"], "wip")           # unmerged
        self.assertEqual(by["side1"]["branches"], "master|side")  # merged in
        self.assertEqual(by["Merge side"]["branches"], "master")
        self.assertEqual(by["c1"]["branch_count"], "3")           # shared root
        with open(os.path.join(self.full, by["wip1"]["sha"], "metadata.json")) as fh:
            self.assertEqual(json.load(fh)["branches"], ["wip"])

    def test_content_changed_writes_only_touched_files(self):
        commits = read_csv(os.path.join(self.full, "commits.csv"))
        c4 = next(c for c in commits if c["subject"] == "c4")
        files = sorted(os.listdir(os.path.join(self.full, c4["sha"], "files")))
        self.assertEqual(files, ["b.txt", "c.txt"])

    def test_churn_only_matches_full_extraction(self):
        for name in ("file_churn.csv", "tree_churn.csv", "file_history.csv"):
            with open(os.path.join(self.full, name), "rb") as a, \
                    open(os.path.join(self.only, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), name)
        full = {r["sha"]: r for r in read_csv(os.path.join(self.full, "commits.csv"))}
        only = {r["sha"]: r for r in read_csv(os.path.join(self.only, "commits.csv"))}
        self.assertEqual(set(full), set(only))
        for sha in full:
            for col in ("changed_files", "branches", "parents", "subject"):
                self.assertEqual(full[sha][col], only[sha][col], (sha, col))

    def test_churn_only_writes_no_commit_folders(self):
        self.assertEqual([n for n in os.listdir(self.only) if len(n) == 40], [])
        with open(os.path.join(self.only, "manifest.json")) as fh:
            self.assertEqual(json.load(fh)["mode"], "churn-only")

    def test_branch_list_flag(self):
        out = os.path.join(self.tmp, "bl")
        run("extract_commits.py", self.repo, "--out", out, "--churn-only",
            "--branch-list", "--quiet")
        rows = {r["path"]: r for r in read_csv(os.path.join(out, "file_churn.csv"))}
        self.assertEqual(rows["wip.txt"]["branches"], "wip")
        self.assertEqual(rows["feat.txt"]["branches"], "master|side")
        self.assertEqual(rows["b.txt"]["branches"], "master|side|wip")

    def test_resume_skips_finished_commits(self):
        out = os.path.join(self.tmp, "resume")
        run("extract_commits.py", self.repo, "--out", out, "--content", "none")
        marker = os.path.join(out, os.listdir(out)[0])
        again = run("extract_commits.py", self.repo, "--out", out,
                    "--content", "none", "--resume")
        self.assertIn("0 error", again.stdout)
        self.assertTrue(os.path.exists(os.path.dirname(marker)))

    def test_progress_bar_and_quiet(self):
        loud = self.only_run.stderr
        self.assertIn("churn", loud)
        self.assertIn("100.0%", loud)
        self.assertIn("ETA", loud)
        self.assertEqual(loud.count("100.0%"), 2)   # once per bar (branches, churn), no repeats
        self.assertNotIn("projected", self.only_run.stdout)
        quiet = run("extract_commits.py", self.repo, "--out",
                    os.path.join(self.tmp, "q"), "--churn-only", "--quiet")
        self.assertNotIn("%", quiet.stderr)
        self.assertIn("done", quiet.stdout)         # summary still prints

    def test_missing_repo_is_a_clean_error(self):
        proc = run("extract_commits.py", os.path.join(self.tmp, "nope"),
                   "--out", os.path.join(self.tmp, "x"), check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)


class ExploreInputCsv(Base):
    def test_profile(self):
        path = os.path.join(self.tmp, "in.tsv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["org", "repo", "relpath", "filename", "sha256"])
            w.writerow(["o1", "r1", "src/a.py", "a.py", "a" * 64])
            w.writerow(["o1", "r1", "src", "b.py", "b" * 64])        # folder only
            w.writerow(["o1", "r1", "src/a.py", "a.py", "a" * 64])   # duplicate
            w.writerow(["o2", "r1", "x\\y.txt", "y.txt", "short"])   # backslash, bad sha
            w.writerow(["o2", "r2", "Q.md", "Q.md", ""])
        out = run("explore_input_csv.py", path, "--out-dir",
                  os.path.join(self.tmp, "prof"), "--repos-root", self.tmp).stdout
        self.assertRegex(out, r"rows\s+5\b")
        self.assertRegex(out, r"orgs\s+2\b")
        self.assertRegex(out, r"repos \(org/repo\)\s+3\b")
        self.assertRegex(out, r"repeated org/repo/path rows: 1\b")
        self.assertRegex(out, r"not 64 hex chars\s+1\b")
        self.assertIn("contains backslash", out)
        self.assertIn("r1  ->  2 orgs", out)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "prof", "repos.csv")))

    def test_bad_columns_fail_cleanly(self):
        path = os.path.join(self.tmp, "bad.csv")
        with open(path, "w") as fh:
            fh.write("a,b\n1,2\n")
        proc = run("explore_input_csv.py", path, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing column", proc.stderr)


class HashActive(Base):
    def test_match_diff_missing(self):
        sys.path.insert(0, HERE)
        from hash_working_tree import hash_file
        archive = os.path.join(self.tmp, "archive")
        active = os.path.join(self.tmp, "active")
        files = {"same.pack": b"abc" * 100, "changed.idx": b"old",
                 "gone.rev": b"zzz"}
        rows = []
        for name, data in files.items():
            for root in (archive, active):
                d = os.path.join(root, "org1", "repoA", ".git", "objects", "pack")
                os.makedirs(d, exist_ok=True)
                if root == active and name == "gone.rev":
                    continue
                with open(os.path.join(d, name), "wb") as fh:
                    fh.write(b"NEW" if (root == active and name == "changed.idx")
                             else data)
            p = os.path.join(archive, "org1", "repoA", ".git", "objects", "pack", name)
            md5, sha, gh, size = hash_file(p)
            rows.append(["org1", "repoA", "/data/workarea/archive/org1/repoA/.git/"
                         "objects/pack/" + name, md5, sha, gh, size, ""])
        inp = os.path.join(self.tmp, "inv.csv")
        with open(inp, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["repo_org", "repo_name", "full_path", "md5", "sha256",
                        "git_object", "size_bytes", "error"])
            w.writerows(rows)
        out = os.path.join(self.tmp, "inv_active.csv")
        proc = run("hash_active_from_archive_csv.py", inp, "--active-root", active,
                   "--out", out)
        got = {os.path.basename(r["full_path"]): r["match"] for r in read_csv(out)}
        self.assertEqual(got, {"same.pack": "YES", "changed.idx": "NO",
                               "gone.rev": "MISSING"})
        self.assertIn("YES: 1", proc.stdout)
        self.assertIn("MISSING: 1", proc.stdout)
        self.assertEqual(len(read_csv(out)[0]), 8 + 7)   # 7 columns appended


if __name__ == "__main__":
    unittest.main(verbosity=2)
