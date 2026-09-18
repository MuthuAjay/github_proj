#!/usr/bin/env python3
"""
extract_from_pack.py - rebuild a real, checked-out working tree from a repo
whose refs/HEAD are gone or broken but whose git object store (loose objects
and/or .pack files) is still intact on disk.

This is the recovery counterpart to compare_repos.py's history mode: when
that script reports "IDENTICAL OBJECT STORES" for a repo git itself sees as
"0 commits", the content isn't missing - it's just unreachable through any
ref. This script copies the raw objects into a fresh, healthy repo, points a
ref at the right commit, and checks the tree out to a destination folder.

Usage:
    # you already know the commit sha (e.g. from compare_repos.py's report
    # on a healthy sibling copy of the same repo)
    python extract_from_pack.py <broken_repo> --commit <sha> --dest <out_dir>

    # or let it pull the sha straight from a healthy sibling repo
    python extract_from_pack.py <broken_repo> --from-repo <good_repo> --dest <out_dir>

    # not sure what commits exist in there at all? just point it at the repo
    python extract_from_pack.py <broken_repo> --dest <out_dir>
    # -> if there's exactly one commit object in the store it's used
    #    automatically; if there are several, they're listed so you can
    #    pick one with --commit
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys


def force_rmtree(path):
    """shutil.rmtree that also clears git's read-only pack/object files."""
    def _on_error(func, p, exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=_on_error)


def run_git(args, cwd=None, check=True):
    proc = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (cwd={cwd}):\n{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def find_objects_dir(repo):
    """Locate the objects/ directory of a repo, whether it's a normal repo,
    a bare repo, or just handed to us as the .git dir itself."""
    for candidate in (
        os.path.join(repo, ".git", "objects"),
        os.path.join(repo, "objects"),
    ):
        if os.path.isdir(candidate):
            return candidate
    raise SystemExit(f"error: no objects/ directory found under {repo!r}")


def copy_objects(src_objects, dest_git_dir):
    """Copy every pack file and loose object into dest's object store."""
    dest_objects = os.path.join(dest_git_dir, "objects")
    copied_packs = 0
    copied_loose = 0

    src_pack_dir = os.path.join(src_objects, "pack")
    if os.path.isdir(src_pack_dir):
        dest_pack_dir = os.path.join(dest_objects, "pack")
        os.makedirs(dest_pack_dir, exist_ok=True)
        for name in os.listdir(src_pack_dir):
            if name.endswith((".pack", ".idx", ".rev")):
                shutil.copy2(os.path.join(src_pack_dir, name),
                             os.path.join(dest_pack_dir, name))
                if name.endswith(".pack"):
                    copied_packs += 1

    for name in os.listdir(src_objects):
        if name in ("pack", "info") or len(name) != 2:
            continue
        src_sub = os.path.join(src_objects, name)
        if not os.path.isdir(src_sub):
            continue
        dest_sub = os.path.join(dest_objects, name)
        os.makedirs(dest_sub, exist_ok=True)
        for fname in os.listdir(src_sub):
            shutil.copy2(os.path.join(src_sub, fname),
                         os.path.join(dest_sub, fname))
            copied_loose += 1

    return copied_packs, copied_loose


def list_commit_candidates(dest_repo):
    """Every commit object physically present in dest's object store,
    regardless of reachability, newest first where dates are available."""
    out = run_git(
        ["cat-file", "--batch-all-objects",
         "--batch-check=%(objectname) %(objecttype)"],
        cwd=dest_repo,
    )
    shas = [line.split()[0] for line in out.splitlines()
            if line.endswith(" commit")]

    candidates = []
    for sha in shas:
        info = run_git(
            ["log", "-1", "--format=%ad|%s", "--date=iso-strict", sha],
            cwd=dest_repo, check=False,
        )
        if "|" in info:
            date, subject = info.split("|", 1)
        else:
            date, subject = "", ""
        candidates.append((sha, date, subject))

    candidates.sort(key=lambda c: c[1], reverse=True)
    return candidates


def resolve_target_sha(args, dest_repo):
    if args.commit:
        return args.commit

    if args.from_repo:
        sha = run_git(["rev-parse", args.ref], cwd=args.from_repo)
        print(f"resolved {args.ref} in {args.from_repo} -> {sha}")
        return sha

    candidates = list_commit_candidates(dest_repo)
    if not candidates:
        raise SystemExit(
            "error: no commit objects found anywhere in this object store - "
            "nothing to check out. If you know a specific commit sha, pass "
            "--commit."
        )
    if len(candidates) == 1:
        sha, date, subject = candidates[0]
        print(f"exactly one commit object found in the store, using it:")
        print(f"  {sha}  {date}  {subject}")
        return sha

    print(f"{len(candidates)} commit objects found in this object store - "
          f"pick one with --commit:\n")
    for sha, date, subject in candidates:
        print(f"  {sha}  {date}  {subject}")
    raise SystemExit(
        "\nerror: multiple candidates, re-run with --commit <sha>"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("broken_repo",
                     help="path holding the intact objects (repo, bare repo, or .git dir)")
    ap.add_argument("--dest", required=True,
                     help="output directory - becomes a real git repo with the tree checked out")
    ap.add_argument("--commit", help="commit sha to check out")
    ap.add_argument("--from-repo",
                     help="resolve the commit sha from this healthy sibling repo instead")
    ap.add_argument("--ref", default="HEAD",
                     help="ref to resolve in --from-repo (default: HEAD)")
    ap.add_argument("--force", action="store_true",
                     help="allow --dest to already exist / be non-empty")
    ap.add_argument("--strip-git", action="store_true",
                     help="delete the .git folder from --dest after checkout, leaving plain files only")
    args = ap.parse_args()

    if not os.path.isdir(args.broken_repo):
        raise SystemExit(f"error: {args.broken_repo!r} is not a directory")

    src_objects = find_objects_dir(args.broken_repo)

    if os.path.exists(args.dest):
        if os.listdir(args.dest) and not args.force:
            raise SystemExit(
                f"error: {args.dest!r} already exists and is not empty "
                f"(pass --force to reuse it)"
            )
    else:
        os.makedirs(args.dest)

    run_git(["init", "-q", args.dest])
    dest_git_dir = os.path.join(args.dest, ".git")

    packs, loose = copy_objects(src_objects, dest_git_dir)
    print(f"copied {packs} pack file(s) and {loose} loose object(s) from "
          f"{src_objects} into {dest_git_dir}\\objects")

    sha = resolve_target_sha(args, args.dest)

    obj_type = run_git(["cat-file", "-t", sha], cwd=args.dest, check=False)
    if obj_type != "commit":
        raise SystemExit(
            f"error: {sha} is not a commit object in this store "
            f"(git cat-file -t reports: {obj_type or '<not found>'!r}). "
            f"Wrong sha, or it belongs to a different repo/pack."
        )

    run_git(["update-ref", "refs/heads/recovered", sha], cwd=args.dest)
    run_git(["symbolic-ref", "HEAD", "refs/heads/recovered"], cwd=args.dest)
    run_git(["reset", "--hard", "-q", "recovered"], cwd=args.dest)

    file_count = 0
    total_size = 0
    for root, dirs, files in os.walk(args.dest):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            file_count += 1
            total_size += os.path.getsize(os.path.join(root, f))

    subject = run_git(["log", "-1", "--format=%s", sha], cwd=args.dest, check=False)
    print(f"\nchecked out commit {sha}  ({subject})")
    print(f"{file_count} file(s), {total_size:,} bytes, written to {args.dest}")

    if args.strip_git:
        force_rmtree(dest_git_dir)
        print("--strip-git: removed .git, dest now holds plain files only")


if __name__ == "__main__":
    main()
