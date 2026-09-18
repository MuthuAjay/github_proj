#!/usr/bin/env python3
"""Build one Excel workbook, one sheet per repo, listing every changed-file
event (repo, branch, file_name, full_filepath) across all remote branches.

Reads the per-repo output of extract_commits.py (refs.csv, commits.csv, and
each <sha>/changes.csv) plus the original repos under Test/ (for `git
rev-list <branch>`) to attribute every file change to every branch that
reaches it. Requires openpyxl.
"""
import csv
import os
import subprocess
import posixpath

import openpyxl

GIT = ["git", "-c", "safe.directory=*"]
TEST_BASE = "/home/eyadmin/Documents/work/github_data/Test"
EXT_BASE = "/home/eyadmin/Documents/work/github_data/extractions"
OUT_PATH = "/home/eyadmin/Documents/work/github_data/reports/run2/changed_files_by_branch.xlsx"
ZERO_SHA = "0" * 40

REPOS = ["allokate-backend-modules", "allokate-databricks", "allokate-datastax",
         "allokate-eyds", "allokate-iac", "ctors-gtrs-reportability-api",
         "ctors-gtrs-reportability-ui", "grs-api", "grs-ui"]

wb = openpyxl.Workbook(write_only=True)

for name in REPOS:
    repo_path = os.path.join(TEST_BASE, name)
    ext_dir = os.path.join(EXT_BASE, name + "-changed")

    ws = wb.create_sheet(title=name[:31])
    ws.append(["repo", "branch", "commit_id", "commit_date", "file_name",
               "full_filepath", "file_hash"])

    refs_csv = os.path.join(ext_dir, "refs.csv")
    branches = []
    with open(refs_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["kind"] == "remote":
                branches.append(row["ref"])

    commit_date = {}
    with open(os.path.join(ext_dir, "commits.csv"), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            commit_date[row["sha"]] = row["committer_date"]

    path_cache = {}

    def paths_for(sha):
        cached = path_cache.get(sha)
        if cached is not None:
            return cached
        cpath = os.path.join(ext_dir, sha, "changes.csv")
        entries = []
        try:
            with open(cpath, newline="", encoding="utf-8", errors="surrogateescape") as f:
                for row in csv.DictReader(f):
                    p = row["path"]
                    if not p:
                        continue
                    new_blob = row.get("new_blob", "")
                    old_blob = row.get("old_blob", "")
                    fhash = new_blob if new_blob and new_blob != ZERO_SHA else old_blob
                    entries.append((p, fhash))
        except FileNotFoundError:
            pass
        path_cache[sha] = entries
        return entries

    row_count = 0
    for i, ref in enumerate(branches, 1):
        short = ref[len("refs/remotes/"):] if ref.startswith("refs/remotes/") else ref
        out = subprocess.run(GIT + ["-C", repo_path, "rev-list", ref],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        shas = out.stdout.split()
        for sha in shas:
            date = commit_date.get(sha, "")
            for p, fhash in paths_for(sha):
                ws.append([name, short, sha, date, posixpath.basename(p), p, fhash])
                row_count += 1
        if i % 20 == 0 or i == len(branches):
            print(f"  {name}: {i}/{len(branches)} branches, {row_count:,} rows so far",
                  flush=True)

    print(f"{name}: DONE, {row_count:,} rows, {len(branches)} branches", flush=True)

wb.save(OUT_PATH)
print("saved ->", OUT_PATH)
