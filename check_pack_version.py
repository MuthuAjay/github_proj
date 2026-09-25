#!/usr/bin/env python3
"""
check_pack_version.py - is this pack file the same as what a repo holds, or
a different (older / newer / diverged) version of it?

Given a repo and a .pack file (for example one copied from another archive of
the same repo):

  1. identical file   every pack ends with a checksum of its contents; if one
                      of the repo's own packs ends with the same checksum (and
                      has the same size) it is the very same pack
  2. same content     otherwise the objects inside are compared - every
                      commit, tree and blob - against ALL of the repo's
                      objects (every pack plus loose objects):

        same objects          SAME CONTENT, packed differently (a repack)
        pack inside repo      OLDER - the repo has everything in the pack and
                              more; the pack is an earlier state of the repo
        repo inside pack      NEWER - the pack holds objects the repo lacks
        both have extras      DIVERGED - each side has objects the other lacks

     and the commits on each side that the other lacks are listed with their
     dates, so "older" and "newer" can be checked against the calendar.

The .pack is indexed into a throwaway repo (its .idx is used if it sits next
to it); nothing is written to the repo or next to the pack. Repos git cannot
open normally (objects only, no HEAD or refs) work too: their object store
is read through a throwaway repo that borrows it.

Exit code: 0 identical or same content, 10 older, 11 newer, 12 diverged,
2 on errors - so it can be used in a loop.

Usage:
    python3 check_pack_version.py ey-org/atl-program /path/to/pack-abc.pack
    python3 check_pack_version.py /data/workarea/archive/ey-org/atl-program \\
        /other/copy/objects/pack/pack-abc.pack --show 20
"""

import argparse
import datetime
import glob
import os
import shutil
import subprocess
import sys
import tempfile

GIT = ["git", "-c", "safe.directory=*"]


def git(args, **kw):
    return subprocess.run(GIT + args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kw)


def objects_dir(repo):
    """The objects/ folder of a work tree (.git/objects) or a bare repo."""
    for d in (os.path.join(repo, ".git", "objects"), os.path.join(repo, "objects")):
        if os.path.isdir(d):
            return os.path.abspath(d)
    return None


def trailer(path):
    """Size and the checksum at the end of a pack (20 bytes SHA-1)."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(4)
        fh.seek(max(0, size - 20))
        tail = fh.read(20)
    if head != b"PACK":
        raise ValueError("%s is not a git pack file" % path)
    return size, tail.hex()


def scratch_repo(tmp, name):
    d = os.path.join(tmp, name)
    p = git(["init", "--bare", "-q", d])
    if p.returncode:
        raise RuntimeError("git init failed: " + p.stderr.decode()[:200])
    return d


def all_objects(gd):
    """{sha: type} for every object git can see in repo gd (alternates
    included)."""
    p = git(["-C", gd, "cat-file", "--batch-all-objects", "--unordered",
             "--batch-check=%(objectname) %(objecttype)"])
    if p.returncode:
        raise RuntimeError("cat-file failed: " + p.stderr.decode()[:300])
    out = {}
    for line in p.stdout.decode().splitlines():
        sha, _, typ = line.partition(" ")
        if sha:
            out[sha] = typ
    return out


def commit_info(gd, shas):
    """{sha: (committer unix time, subject)} via one cat-file --batch."""
    info = {}
    if not shas:
        return info
    proc = subprocess.Popen(GIT + ["-C", gd, "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        for sha in shas:
            proc.stdin.write(sha.encode() + b"\n")
            proc.stdin.flush()
            head = proc.stdout.readline().split()
            if len(head) < 3 or head[1] != b"commit":
                continue
            body = proc.stdout.read(int(head[2]))
            proc.stdout.read(1)
            hdr, _, msg = body.partition(b"\n\n")
            ts = 0
            for ln in hdr.split(b"\n"):
                if ln.startswith(b"committer "):
                    try:
                        ts = int(ln.rsplit(b" ", 2)[-2])
                    except (ValueError, IndexError):
                        pass
            subject = msg.split(b"\n", 1)[0].decode("utf-8", "replace")
            info[sha] = (ts, subject)
    finally:
        proc.stdin.close()
        proc.wait()
    return info


def when(ts):
    if not ts:
        return "?"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def span(info):
    ts = [t for t, _s in info.values() if t]
    return (when(min(ts)), when(max(ts))) if ts else ("?", "?")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="repo path, or ORG/REPO under --repos-root")
    ap.add_argument("pack", help="the .pack file to check")
    ap.add_argument("--repos-root", default="/data/workarea/archive",
                    help="where ORG/REPO is looked up (default /data/workarea/archive)")
    ap.add_argument("--show", type=int, default=10,
                    help="commits to list on each side (default 10)")
    args = ap.parse_args()

    repo = args.repo if os.path.isdir(args.repo) else \
        os.path.join(args.repos_root, args.repo)
    odir = objects_dir(repo)
    if not odir:
        print("no git objects folder found for %s" % repo, file=sys.stderr)
        return 2
    if not os.path.isfile(args.pack):
        print("not a file: %s" % args.pack, file=sys.stderr)
        return 2

    try:
        size, csum = trailer(args.pack)
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print("repo      %s" % repo)
    print("pack      %s  (%s bytes, checksum %s)" % (args.pack, f"{size:,}", csum))

    # ---- 1. the same file? ---------------------------------------------------
    own = sorted(glob.glob(os.path.join(odir, "pack", "*.pack")))
    print("repo has  %d pack(s)" % len(own))
    for p in own:
        try:
            if trailer(p) == (size, csum):
                print("\nRESULT    IDENTICAL - byte-for-byte the same pack as %s"
                      % p)
                return 0
        except (OSError, ValueError):
            continue

    tmp = tempfile.mkdtemp(prefix="packcheck_")
    try:
        # ---- 2. what is in the pack -------------------------------------------
        pk = scratch_repo(tmp, "pack.git")
        dst = os.path.join(pk, "objects", "pack", "pack-check.pack")
        shutil.copyfile(args.pack, dst)
        idx = os.path.splitext(args.pack)[0] + ".idx"
        if os.path.isfile(idx):
            shutil.copyfile(idx, dst[:-5] + ".idx")
        else:
            print("indexing  the pack (no .idx next to it) ...")
            p = git(["-C", pk, "index-pack", dst])
            if p.returncode:
                print("index-pack failed: " + p.stderr.decode()[:300],
                      file=sys.stderr)
                return 2
        in_pack = all_objects(pk)

        # ---- the repo's objects, through a repo that borrows its store ----------
        rp = scratch_repo(tmp, "repo.git")
        with open(os.path.join(rp, "objects", "info", "alternates"), "w") as fh:
            fh.write(odir + "\n")
        in_repo = all_objects(rp)

        only_pack = {s for s in in_pack if s not in in_repo}
        only_repo = {s for s in in_repo if s not in in_pack}
        both = len(in_pack) - len(only_pack)

        def kinds(shas, types):
            c = {}
            for s in shas:
                c[types[s]] = c.get(types[s], 0) + 1
            return ", ".join("%s %s" % (k, f"{v:,}") for k, v in sorted(c.items())) or "-"

        print("\nobjects   pack %s | repo %s | in both %s"
              % (f"{len(in_pack):,}", f"{len(in_repo):,}", f"{both:,}"))
        print("          only in pack  %s  (%s)"
              % (f"{len(only_pack):,}", kinds(only_pack, in_pack)))
        print("          only in repo  %s  (%s)"
              % (f"{len(only_repo):,}", kinds(only_repo, in_repo)))

        pack_commits = [s for s, t in in_pack.items() if t == "commit"]
        repo_commits = [s for s, t in in_repo.items() if t == "commit"]
        pc = commit_info(pk, pack_commits)
        rc = commit_info(rp, repo_commits)
        print("\ncommits   pack %s, dated %s .. %s"
              % (f"{len(pc):,}", *span(pc)))
        print("          repo %s, dated %s .. %s"
              % (f"{len(rc):,}", *span(rc)))

        def listing(title, shas, info):
            rows = sorted(((info[s][0], s, info[s][1]) for s in shas if s in info),
                          reverse=True)
            if not rows:
                return
            print("\n%s: %s (newest first)" % (title, f"{len(rows):,}"))
            for ts, s, subj in rows[:args.show]:
                print("  %s  %s  %s" % (s[:12], when(ts), subj[:70]))
            if len(rows) > args.show:
                print("  ... %s more" % f"{len(rows) - args.show:,}")

        listing("commits only in the pack", only_pack, pc)
        listing("commits only in the repo", only_repo, rc)

        if not only_pack and not only_repo:
            verdict, code = ("SAME CONTENT - the same objects, packed "
                             "differently (a repack of the same state)"), 0
        elif not only_pack:
            verdict, code = ("OLDER - everything in the pack is in the repo, "
                             "which has %s more object(s); the pack is an "
                             "earlier state" % f"{len(only_repo):,}"), 10
        elif not only_repo:
            verdict, code = ("NEWER - the pack has %s object(s) the repo lacks"
                             % f"{len(only_pack):,}"), 11
        else:
            verdict, code = ("DIVERGED - the pack has %s object(s) the repo "
                             "lacks and the repo has %s the pack lacks"
                             % (f"{len(only_pack):,}", f"{len(only_repo):,}")), 12
        if both == 0:
            verdict += " (no objects in common - possibly a different repo)"
        print("\nRESULT    " + verdict)
        return code
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
