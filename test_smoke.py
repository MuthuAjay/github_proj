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
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
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


def run(script, *args, check=True, env=None):
    proc = subprocess.run([PY, os.path.join(HERE, script)] + list(args),
                          capture_output=True, text=True, env=env)
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


class SlowRepoHandling(Base):
    """A repo whose git commands hang: timeout, SLOW warning, Ctrl+C, resume.
    A fake `git` first on PATH sleeps for 60s when the repo is called slowrepo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = os.path.join(cls.tmp, "root")
        for name in ("slowrepo", "fastrepo"):
            os.makedirs(os.path.join(cls.root, "orgA"), exist_ok=True)
            shutil.copytree(cls.repo, os.path.join(cls.root, "orgA", name))
        fake = os.path.join(cls.tmp, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "git"), "w") as fh:
            fh.write('#!/bin/sh\ncase "$*" in\n *slowrepo*) case "$*" in '
                     '*" log "*|*cat-file*) exec sleep 60;; esac;;\nesac\n'
                     'exec %s "$@"\n' % shutil.which("git"))
        os.chmod(os.path.join(fake, "git"), 0o755)
        cls.env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"])
        cls.inp = os.path.join(cls.tmp, "slow.tsv")
        with open(cls.inp, "w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["org", "repo", "relpath", "filename", "sha256"])
            for repo in ("slowrepo", "fastrepo"):
                for f in ("b.txt", "c.txt"):
                    w.writerow(["orgA", repo, f, f, ""])

    def test_timeout_kills_the_stuck_repo_and_carries_on(self):
        out = os.path.join(self.tmp, "t_out")
        t0 = time.time()
        proc = run("file_history_for_list.py", self.inp, "--repos-root", self.root,
                   "--out", out, "--workers", "2", "--repo-timeout", "3",
                   "--warn-after", "1", env=self.env)
        self.assertLess(time.time() - t0, 30)            # not the 60s the sleep wanted
        got = {(r["repo"], r["filename"]): r["status"]
               for r in read_csv(os.path.join(out, "file_summary.csv"))}
        self.assertEqual({v for k, v in got.items() if k[0] == "slowrepo"}, {"timeout"})
        self.assertEqual({v for k, v in got.items() if k[0] == "fastrepo"}, {"found"})
        detail = next(r for r in read_csv(os.path.join(out, "file_summary.csv"))
                      if r["repo"] == "slowrepo")["error"]
        self.assertIn("gave up after", detail)
        self.assertIn("reading", detail)                  # names the phase it was in
        with open(os.path.join(out, "timed_out_repos.tsv")) as fh:
            self.assertIn("slowrepo", fh.read())
        self.assertIn("SLOW", proc.stderr)
        self.assertIn("orgA/slowrepo", proc.stderr)
        self.assertIn("timeout", proc.stdout)

    def test_ctrl_c_stops_cleanly_and_resume_finishes(self):
        out = os.path.join(self.tmp, "c_out")
        args = [PY, os.path.join(HERE, "file_history_for_list.py"), self.inp,
                "--repos-root", self.root, "--out", out, "--workers", "2", "--quiet"]
        p = subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        time.sleep(2.5)                                   # slowrepo is stuck by now
        p.send_signal(signal.SIGINT)
        try:
            _, err = p.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
            self.fail("did not stop after one Ctrl+C")
        self.assertEqual(p.returncode, 130)
        self.assertIn("--resume", err)
        # finish the job with a working git: every input row exactly once
        run("file_history_for_list.py", self.inp, "--repos-root", self.root,
            "--out", out, "--workers", "2", "--quiet", "--resume")
        rows = read_csv(os.path.join(out, "file_summary.csv"))
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["status"] for r in rows}, {"found"})


class MakeInputCsv(Base):
    def test_generated_input_feeds_the_history_script(self):
        root = os.path.join(self.tmp, "mroot")
        os.makedirs(os.path.join(root, "orgA"))
        shutil.copytree(self.repo, os.path.join(root, "orgA", "good"))
        broken = os.path.join(root, "orgA", "brk")
        shutil.copytree(self.repo, broken)                 # objects only: no HEAD, no refs
        os.remove(os.path.join(broken, ".git", "HEAD"))
        shutil.rmtree(os.path.join(broken, ".git", "refs"))
        inp = os.path.join(self.tmp, "made.csv")
        proc = run("make_input_csv.py", root, "--out", inp)
        self.assertIn("1 repo(s) had no HEAD", proc.stdout)
        rows = read_csv(inp)
        self.assertEqual({r["repo"] for r in rows}, {"good", "brk"})
        self.assertEqual(len([r for r in rows if r["repo"] == "good"]), 5)
        # no HEAD: the newest commit is used (ties on timestamp in a toy repo)
        self.assertGreaterEqual(len([r for r in rows if r["repo"] == "brk"]), 1)
        self.assertTrue(rows[0]["relpath"].startswith("AllRepos\\orgA\\"))
        capped = os.path.join(self.tmp, "capped.csv")
        run("make_input_csv.py", root, "--out", capped, "--max-per-repo", "1")
        self.assertEqual(len(read_csv(capped)), 2)        # 1 per repo
        out = os.path.join(self.tmp, "made_out")
        run("file_history_for_list.py", inp, "--repos-root", root, "--out", out, "--quiet")
        got = read_csv(os.path.join(out, "file_summary.csv"))
        self.assertEqual(len(got), len(rows))
        self.assertEqual({r["status"] for r in got}, {"found"})


class LogStreamParser(unittest.TestCase):
    """parse_log_stream reads `git log --raw -z` output as a stream and can drop
    unwanted changes before building anything for them."""

    @staticmethod
    def stream(n_noise=0):
        import io
        sha1, sha2, sha3 = "a" * 40, "b" * 40, "c" * 40
        z = b"\0"
        raw = [b"\x1e" + sha1.encode() + z,
               b"\n:000000 100644 " + b"0" * 40 + b" " + b"1" * 40 + b" A" + z + b"keep/one.txt" + z]
        for i in range(n_noise):
            raw.append(b":000000 100644 " + b"0" * 40 + b" " + b"2" * 40 + b" A" + z
                       + b"node_modules/p%d/index.js" % i + z)
        raw += [b"\x1e" + sha2.encode() + z,                      # a commit with no changes
                b"\x1e" + sha3.encode() + z,
                b"\n:100644 100644 " + b"3" * 40 + b" " + b"3" * 40 + b" R100" + z
                + b"old name.txt" + z + "n\u00e9w\nname.txt".encode() + z,
                b":100644 100644 " + b"4" * 40 + b" " + b"5" * 40 + b" M" + z + b"keep/one.txt" + z]
        return io.BytesIO(b"".join(raw))

    def test_parses_adds_renames_odd_names_and_empty_commits(self):
        from extract_commits import parse_log_stream
        got = list(parse_log_stream(self.stream()))
        self.assertEqual([s[0] for s, _ in [(g, 0) for g in got]], ["a" * 40, "b" * 40, "c" * 40])
        self.assertEqual(got[1][1], [])                            # empty commit kept
        ren = got[2][1][0]
        self.assertEqual((ren["status"], ren["old_path"], ren["path"]),
                         ("R100", "old name.txt", "n\u00e9w\nname.txt"))
        self.assertEqual(got[2][1][1]["status"], "M")

    def test_keep_filter_drops_changes_before_they_are_built(self):
        from extract_commits import parse_log_stream
        got = list(parse_log_stream(self.stream(50),
                                    keep=lambda st, p, old: p.startswith("keep/")))
        self.assertEqual([len(c) for _, c in got], [1, 0, 1])
        self.assertEqual(got[0][1][0]["path"], "keep/one.txt")

    def test_filtering_keeps_memory_flat_for_a_huge_commit(self):
        import tracemalloc
        from extract_commits import parse_log_stream
        n = 200000
        for keep, label in ((lambda st, p, old: p.startswith("keep/"), "filtered"),
                            (None, "unfiltered")):
            s = self.stream(n)                         # built before measuring
            tracemalloc.start()
            list(parse_log_stream(s, keep=keep))
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            if label == "filtered":
                filtered = peak
            else:
                unfiltered = peak
        self.assertLess(filtered, 8 * 1024 * 1024)               # a few MB at most
        self.assertGreater(unfiltered, 20 * filtered)            # unfiltered is far bigger
        print("\n    peak memory, %d changes in one commit: filtered %.1f MB, "
              "unfiltered %.1f MB" % (n, filtered / 1e6, unfiltered / 1e6))


class DiagnoseRepo(Base):
    def test_report_on_a_repo_git_cannot_open(self):
        broken = os.path.join(self.tmp, "dx")
        shutil.copytree(self.repo, broken)
        os.remove(os.path.join(broken, ".git", "HEAD"))
        shutil.rmtree(os.path.join(broken, ".git", "refs"))
        out = run("diagnose_repo.py", broken).stdout
        self.assertIn("git CANNOT open it", out)
        self.assertIn("commits found in the object store", out)
        self.assertIn("history WITHOUT rename detection", out)
        self.assertIn("history WITH rename detection", out)
        self.assertIn("nothing unusual", out)               # a 10-commit repo is not slow
        self.assertNotIn("STOPPED", out)

    def test_early_stop_reports_progress(self):
        out = run("diagnose_repo.py", self.repo, "--max-seconds", "0").stdout
        self.assertIn("git opens it directly", out)
        self.assertIn("verdict", out)


class SimpleFileInfo(Base):
    def test_counts_and_root_replacement(self):
        root = os.path.join(self.tmp, "sroot")
        os.makedirs(os.path.join(root, "orgA"))
        shutil.copytree(self.repo, os.path.join(root, "orgA", "repoX"))
        inp = os.path.join(self.tmp, "simple.csv")
        with open(inp, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["org", "repo", "relpath", "filename"])
            w.writerow(["orgA", "repoX", "AllRepos\\orgA\\repoX\\b.txt", "b.txt"])
            w.writerow(["orgA", "repoX", "AllRepos\\orgA\\repoX\\src\\y.txt", "y.txt"])
            w.writerow(["orgA", "repoX", "AllRepos\\orgA\\repoX\\nope.txt", "nope.txt"])
            w.writerow(["orgA", "gone", "AllRepos\\orgA\\gone\\x.txt", "x.txt"])
        out = os.path.join(self.tmp, "simple_out.csv")
        loud = run("simple_file_info.py", inp, "--root", root, "--out", out)
        self.assertIn("100.0%", loud.stderr)
        self.assertIn("found 2", loud.stderr)
        quiet = run("simple_file_info.py", inp, "--root", root, "--out",
                    os.path.join(self.tmp, "simple_q.csv"), "--quiet")
        self.assertNotIn("%", quiet.stderr)
        rows = read_csv(out)
        b, y, nope, gone = rows
        self.assertEqual((b["status"], b["commits"], b["added"], b["renamed"]),
                         ("found", "5", "1", "1"))       # the merge commit is not counted
        self.assertEqual((y["status"], y["commits"], y["renamed"]), ("found", "2", "1"))
        self.assertEqual(nope["status"], "not_found")
        self.assertTrue(gone["status"].startswith("repo_error"))
        self.assertTrue(b["repo_path"].endswith("sroot/orgA/repoX"))
        self.assertEqual(list(rows[0])[:4], ["org", "repo", "relpath", "filename"])


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
        # a repo git cannot open: objects intact, HEAD and refs gone
        broken = os.path.join(cls.root, "orgA", "brk")
        shutil.copytree(cls.repo, broken)
        os.remove(os.path.join(broken, ".git", "HEAD"))
        shutil.rmtree(os.path.join(broken, ".git", "refs"))
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
            ["orgA", "repoX", "AllRepos\\orgA\\repoX\\src\\y.txt", "y.txt", ""],  # full Windows path
            ["orgA", "brk", "AllRepos\\orgA\\brk\\b.txt", "b.txt", ""],   # broken repo
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
        self.assertEqual(sorted(int(r["row"]) for r in self.summary), list(range(1, 13)))

    def test_statuses(self):
        got = {int(r["row"]): r["status"] for r in self.summary}
        self.assertEqual(got, {1: "found", 2: "found", 3: "found", 4: "found",
                               5: "found", 6: "found", 7: "found",
                               8: "not_found", 9: "repo_missing", 10: "bad_row",
                               11: "found", 12: "found"})

    def test_renamed_file_keeps_full_history(self):
        b = self.drow(1)
        self.assertEqual(b["commits_touched"], "6")
        self.assertEqual(b["renamed"], "1")
        self.assertEqual(b["present_at_head"], "yes")
        self.assertEqual(b["sha256"], "wrong-hash")          # carried, not used
        self.assertEqual(b["first_subject"], "c1")
        self.assertEqual(b["last_subject"], "Merge side")
        hist = [h for h in read_csv(os.path.join(self.out2, "file_history.csv"))
                if h["repo"] == "repoX" and h["path"] == "b.txt"]
        self.assertEqual([h["nth_change"] for h in hist], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(hist[2]["old_path"], "a.txt")

    def test_old_name_keeps_history_up_to_the_rename(self):
        a = self.drow(7)                                 # a.txt, renamed to b.txt
        self.assertEqual(a["present_at_head"], "no")
        self.assertEqual(a["commits_touched"], "2")      # add, edit (the rename
        self.assertEqual(a["last_change_type"], "M")     # is recorded on b.txt)

    def test_full_windows_path_is_reduced_to_repo_relative(self):
        r = self.row(11)
        self.assertEqual(r["matched_path"], "src/y.txt")
        self.assertEqual(r["commits_touched"], self.row(2)["commits_touched"])

    def test_broken_repo_is_recovered(self):
        r = self.row(12)                     # git cannot open this repo at all
        self.assertEqual(r["status"], "found")
        self.assertIn("recovered", r["error"])
        self.assertEqual(r["present_at_head"], "")            # no HEAD to check
        self.assertEqual(r["branch_count"], "0")              # no refs survived
        good = self.row(1)                                    # same file, intact repo
        for col in ("commits_touched", "added", "modified", "deleted", "renamed"):
            self.assertEqual(r[col], good[col], col)
        self.assertNotIn("recovered", good["error"])

    def test_big_repo_limit_gives_the_same_results(self):
        out = os.path.join(self.tmp, "fh_big")
        run("file_history_for_list.py", self.inp, "--repos-root", self.root,
            "--out", out, "--quiet", "--workers", "4",
            "--big-repo-gb", "0.0000001", "--max-big-repos", "1")   # every repo is "big"
        key = lambda r: int(r["row"])
        got = sorted(read_csv(os.path.join(out, "file_summary.csv")), key=key)
        want = sorted(self.summary, key=key)
        self.assertEqual(got, want)

    def test_memory_guard_never_deadlocks(self):
        out = os.path.join(self.tmp, "fh_mem")
        # an impossible threshold: repos must still run, one at a time
        run("file_history_for_list.py", self.inp, "--repos-root", self.root,
            "--out", out, "--quiet", "--workers", "4", "--min-free-gb", "1000000")
        key = lambda r: int(r["row"])
        self.assertEqual(sorted(read_csv(os.path.join(out, "file_summary.csv")), key=key),
                         sorted(self.summary, key=key))

    def test_resume_into_a_new_folder_warns(self):
        proc = run("file_history_for_list.py", self.inp, "--repos-root", self.root,
                   "--out", os.path.join(self.tmp, "fh_fresh"), "--quiet", "--resume")
        self.assertIn("starts from scratch", proc.stderr)
        self.assertIn("SAME --out", proc.stderr)

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
                if h["repo"] == "repoX" and h["path"] == "src/y.txt"]
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
