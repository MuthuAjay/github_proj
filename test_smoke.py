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


class FileHistoryForList(Base):
    """file_history_for_list.py against <root>/orgA/repoX (a copy of the repo)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = os.path.join(cls.tmp, "root")
        os.makedirs(os.path.join(cls.root, "orgA"))
        shutil.copytree(cls.repo, os.path.join(cls.root, "orgA", "repoX"))
        cls.inp = os.path.join(cls.tmp, "list.tsv")
        rows = [
            ["orgA", "repoX", "b.txt", "b.txt", "wrong-hash"],       # renamed file
            ["orgA", "repoX", "src", "y.txt", ""],                    # folder-only relpath
            ["orgA", "repoX", "src\\y.txt", "y.txt", ""],             # backslashes, dup path
            ["orgA", "repoX", "./wip.txt", "wip.txt", ""],            # unmerged branch only
            ["orgA", "repoX", "feat.txt", "feat.txt", ""],
            ["orgA", "repoX", "repoX/c.txt", "c.txt", ""],            # repo-name prefix
            ["orgA", "repoX", "a.txt", "a.txt", ""],                  # old name, renamed to b.txt
            ["orgA", "repoX", "nope.txt", "nope.txt", ""],
            ["orgA", "gone", "x.txt", "x.txt", ""],                   # repo missing
            ["", "repoX", "b.txt", "b.txt", ""],                      # blank org
        ]
        with open(cls.inp, "w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["org", "repo", "relpath", "filename", "sha256"])
            w.writerows(rows)
        cls.out = os.path.join(cls.tmp, "fh")
        cls.run1 = run("file_history_for_list.py", cls.inp, "--repos-root",
                       cls.root, "--out", cls.out, "--workers", "2")
        cls.summary = read_csv(os.path.join(cls.out, "file_summary.csv"))
        cls.out2 = os.path.join(cls.tmp, "fh_full")
        run("file_history_for_list.py", cls.inp, "--repos-root", cls.root,
            "--out", cls.out2, "--workers", "2", "--details", "--history",
            "--quiet")
        cls.detailed = read_csv(os.path.join(cls.out2, "file_summary.csv"))

    def row(self, n):
        return next(r for r in self.summary if r["row"] == str(n))

    def drow(self, n):
        return next(r for r in self.detailed if r["row"] == str(n))

    def test_default_output_is_just_the_churn_columns(self):
        self.assertEqual(
            list(self.summary[0]),
            ["row", "org", "repo", "relpath", "filename", "sha256",
             "matched_path", "status", "present_at_head", "commits_touched",
             "added", "modified", "deleted", "renamed", "first_seen",
             "last_changed", "branch_count", "error"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "file_history.csv")))
        self.assertIn("first_subject", self.detailed[0])
        self.assertTrue(os.path.exists(os.path.join(self.out2, "file_history.csv")))
        self.assertEqual(len(self.summary), len(self.detailed))

    def test_every_input_row_gets_one_summary_row(self):
        self.assertEqual(sorted(int(r["row"]) for r in self.summary), list(range(1, 11)))

    def test_statuses(self):
        got = {int(r["row"]): r["status"] for r in self.summary}
        self.assertEqual(got, {1: "found", 2: "found", 3: "found", 4: "found",
                               5: "found", 6: "found", 7: "found",
                               8: "not_found", 9: "repo_missing", 10: "bad_row"})

    def test_renamed_file_keeps_full_history(self):
        b = self.drow(1)
        self.assertEqual(b["commits_touched"], "6")
        self.assertEqual(b["renamed"], "1")
        self.assertEqual(b["present_at_head"], "yes")
        self.assertEqual(b["sha256"], "wrong-hash")          # carried, not used
        self.assertEqual(b["first_subject"], "c1")
        self.assertEqual(b["last_subject"], "Merge side")
        hist = [h for h in read_csv(os.path.join(self.out2, "file_history.csv"))
                if h["path"] == "b.txt"]
        self.assertEqual([h["nth_change"] for h in hist], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(hist[2]["old_path"], "a.txt")

    def test_old_name_keeps_history_up_to_the_rename(self):
        a = self.drow(7)                                 # a.txt, renamed to b.txt
        self.assertEqual(a["present_at_head"], "no")
        self.assertEqual(a["commits_touched"], "2")      # add, edit (the rename
        self.assertEqual(a["last_change_type"], "M")     # is recorded on b.txt)

    def test_path_normalisation(self):
        self.assertEqual(self.row(2)["matched_path"], "src/y.txt")
        self.assertEqual(self.row(3)["matched_path"], "src/y.txt")
        self.assertEqual(self.row(4)["matched_path"], "wip.txt")
        self.assertEqual(self.row(6)["matched_path"], "c.txt")   # "repoX/" prefix dropped

    def test_branch_count(self):
        self.assertEqual(self.row(4)["branch_count"], "1")       # wip only
        self.assertEqual(self.row(5)["branch_count"], "2")       # side + master

    def test_history_written_once_per_distinct_path(self):
        hist = [h for h in read_csv(os.path.join(self.out2, "file_history.csv"))
                if h["path"] == "src/y.txt"]
        self.assertEqual(len(hist), 2)                            # rows 2 and 3 share it

    def test_matches_file_churn_from_extract_commits(self):
        churn = os.path.join(self.tmp, "churn_ref")
        run("extract_commits.py", os.path.join(self.root, "orgA", "repoX"),
            "--out", churn, "--churn-only", "--quiet")
        ref = {r["path"]: r for r in read_csv(os.path.join(churn, "file_churn.csv"))}
        for n, path in ((1, "b.txt"), (2, "src/y.txt"), (5, "feat.txt"), (4, "wip.txt")):
            for col in ("commits_touched", "added", "modified", "deleted",
                        "renamed", "branch_count"):
                self.assertEqual(self.row(n)[col], ref[path][col], (path, col))

    def test_resume_does_not_duplicate_rows(self):
        out = os.path.join(self.tmp, "fh_resume")
        args = [self.inp, "--repos-root", self.root, "--out", out, "--quiet"]
        run("file_history_for_list.py", *args)
        n1 = len(read_csv(os.path.join(out, "file_summary.csv")))
        run("file_history_for_list.py", *args, "--resume")
        self.assertEqual(len(read_csv(os.path.join(out, "file_summary.csv"))), n1)

    def test_bad_columns_fail_cleanly(self):
        bad = os.path.join(self.tmp, "bad_cols.csv")
        with open(bad, "w") as fh:
            fh.write("a,b\n1,2\n")
        proc = run("file_history_for_list.py", bad, "--repos-root", self.root,
                   "--out", os.path.join(self.tmp, "x"), check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing column", proc.stderr + proc.stdout)

    def test_progress_bar(self):
        self.assertIn("100.0%", self.run1.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
