#!/usr/bin/env python3
"""
file_version_diffs.py - every version of ONE file, saved as a chain of diffs.

Given a repo and a path inside it, walks the history straight out of the
object store (.git - no checkout, nothing written to the repo) and writes a
single text file with one section per commit that changed the file, oldest
first. Each section starts with an identifier banner:

    ##########################################################################
    ### VERSION 3 of 12
    ### commit   <sha>
    ### parents  <sha> [<sha> ...]
    ### author   Name <email>
    ### date     2024-05-01T10:20:30+02:00
    ### subject  Fix the thing
    ### change   M  path/to/file   (or R old/path -> new/path, A, D)
    ### blobs    <old blob> -> <new blob>
    ##########################################################################

followed by ONLY the difference against the previous version of the file:

  A (added)     the whole first version, every line prefixed with "+"
  M / R         a unified diff (git's own diff between the two blobs)
  D (deleted)   a one-line note - the removed content is the previous version
  binary        "Binary files differ" / the new size, never the bytes

Scope: commits reachable from --rev (default: every ref, --all). Merges are
diffed against their first parent, exactly as extract_commits.py does, so a
merge only shows up if it changed the file relative to the branch it landed
on. On parallel branches each commit is diffed against ITS parent's version,
so a section is always a true "before -> after" for that commit - two
branches editing the same file give two sections against the same base.

Renames: by default only the exact path you give is followed; history under
an earlier name is not included. --follow-renames uses git's --follow to
carry the history across renames.

Usage:
    python3 file_version_diffs.py <repo> <path/in/repo> [--out diffs.txt]
    python3 file_version_diffs.py /full/path/to/file   [--out diffs.txt]
"""

import argparse
import os
import subprocess
import sys
import tempfile

from extract_commits import (GIT, commit_metadata, git_dir,
                             parse_log_stream, run_tracked, spawn)

ZERO = "0" * 40
BAR = "#" * 78


def is_null(blob):
    return not blob or set(blob) == {"0"}


def normalise_path(repo, path):
    """Accept a repo-relative path, or an absolute/cwd-relative one that sits
    inside the work tree; always return a forward-slash repo-relative path."""
    if os.path.isabs(path) or os.path.exists(path):
        full = os.path.abspath(path)
        if full == repo or full.startswith(repo + os.sep):
            path = os.path.relpath(full, repo)
    return path.replace(os.sep, "/").lstrip("/")


def is_bare_repo(d):
    return (os.path.isfile(os.path.join(d, "HEAD"))
            and os.path.isdir(os.path.join(d, "objects"))
            and os.path.isdir(os.path.join(d, "refs")))


def find_repo(file_path):
    """The repo holding `file_path`, found by walking up from it: the first
    directory that has a .git, or is itself a bare repo (HEAD, objects/,
    refs/ directly inside). The file need not exist on disk - in a bare
    clone or a deleted file it never does - so missing directories are
    walked past, not treated as the end."""
    d = os.path.dirname(os.path.abspath(file_path))
    while True:
        if os.path.isdir(d) and (os.path.exists(os.path.join(d, ".git"))
                                 or is_bare_repo(d)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def file_changes(repo, path, revs, follow):
    """[(sha, change)] for every commit that changed `path`, oldest first.

    One `git log` limited to the path. git's --reverse is unreliable together
    with --follow, so the (small) list is reversed here instead.
    """
    cmd = GIT + ["-C", repo, "log"] + (revs or ["--all"])
    cmd += ["--topo-order", "--raw", "-z", "--no-abbrev",
            "--diff-merges=first-parent", "--format=%x1e%H"]
    cmd += ["--follow", "-M"] if follow else ["--no-renames"]
    cmd += ["--", path]
    out = []
    with tempfile.TemporaryFile() as err:
        proc = spawn(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            for sha, changes in parse_log_stream(proc.stdout):
                # without --follow the pathspec already narrowed it to one
                # change; with it, pick the entry that is the followed file
                for c in changes:
                    out.append((sha, c))
                    break
        finally:
            proc.stdout.close()
            proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError("git log failed: "
                               + err.read().decode("utf-8", "replace")[:400])
    out.reverse()
    return out


def read_blob(repo, blob):
    proc = run_tracked(GIT + ["-C", repo, "cat-file", "blob", blob])
    if proc.returncode != 0:
        raise RuntimeError("cannot read blob %s: %s" % (
            blob, proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout


def looks_binary(data):
    return b"\0" in data[:8000]


def blob_diff(repo, old, new, context):
    """Unified diff between two blobs, headers stripped down to the hunks."""
    proc = run_tracked(GIT + ["-C", repo, "diff", "--no-color", "--no-ext-diff",
                              "-U%d" % context, old, new])
    if proc.returncode not in (0, 1):
        raise RuntimeError("git diff failed: "
                           + proc.stderr.decode("utf-8", "replace").strip())
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("@@") or ln.startswith("Binary files"):
            return "\n".join(lines[i:])
    return "(no content difference)"


def section_body(repo, change, context):
    status = change["status"][:1]
    old, new = change["old_blob"], change["new_blob"]
    if status == "D" or is_null(new):
        return "(file deleted in this commit)"
    if is_null(old):
        data = read_blob(repo, new)
        if looks_binary(data):
            return "(binary file added, %d bytes)" % len(data)
        text = data.decode("utf-8", "replace")
        if not text:
            return "(empty file added)"
        return "\n".join("+" + ln for ln in text.splitlines())
    if old == new:
        if change["old_mode"] != change["new_mode"]:
            return "(mode changed %s -> %s, content unchanged)" % (
                change["old_mode"], change["new_mode"])
        return "(content unchanged)"
    return blob_diff(repo, old, new, context)


def banner(n, total, sha, meta, change):
    status = change["status"]
    if change["old_path"]:
        what = "%s  %s -> %s" % (status, change["old_path"], change["path"])
    else:
        what = "%s  %s" % (status, change["path"])
    rows = [
        "VERSION %d of %d" % (n, total),
        "commit   " + sha,
        "parents  " + (" ".join(meta.get("parents", [])) or "(root commit)"),
        "author   %s <%s>" % (meta.get("author_name", ""),
                              meta.get("author_email", "")),
        "date     " + meta.get("author_date", ""),
        "subject  " + (meta.get("subject", "") or "").replace("\n", " "),
        "change   " + what,
        "blobs    %s -> %s" % (change["old_blob"] or ZERO,
                               change["new_blob"] or ZERO),
    ]
    return "\n".join([BAR] + ["### " + r for r in rows] + [BAR])


def main():
    ap = argparse.ArgumentParser(
        description="Save every version of one file as a chain of diffs.")
    ap.add_argument("repo", help="path to the repository (work tree or .git); "
                                 "or, on its own, the full path of the file - "
                                 "the repo is then found by walking up from it")
    ap.add_argument("path", nargs="?", help="file path inside the repo")
    ap.add_argument("--out", help="output .txt (default: "
                                  "<file name>.versions.txt in the cwd)")
    ap.add_argument("--rev", action="append", default=[],
                    help="restrict to these revs (repeatable); default --all")
    ap.add_argument("--follow-renames", action="store_true",
                    help="carry history across renames (git log --follow)")
    ap.add_argument("--context", type=int, default=3,
                    help="lines of context around each change (default 3)")
    args = ap.parse_args()
    if args.path is None:
        args.path = os.path.abspath(args.repo)
        found = find_repo(args.path)
        if not found:
            sys.exit("no git repository found above " + args.path)
        args.repo = found

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        sys.exit("not a directory: " + repo)
    try:
        gdir = git_dir(repo)
    except RuntimeError as exc:
        sys.exit("not a git repository: %s\n  %s" % (repo, exc))
    path = normalise_path(repo, args.path)
    out = args.out or os.path.basename(path) + ".versions.txt"

    changes = file_changes(repo, path, args.rev, args.follow_renames)
    if not changes:
        sys.exit("no history for %s in %s" % (path, repo))
    meta = commit_metadata(repo, [sha for sha, _c in changes],
                           with_message=False)

    total = len(changes)
    with open(out, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("file      %s\nrepo      %s\ngit dir   %s\nrevs      %s\n"
                 "renames   %s\nversions  %d (oldest first)\n\n"
                 % (path, repo, gdir, " ".join(args.rev or ["--all"]),
                    "followed" if args.follow_renames else "not followed",
                    total))
        for n, (sha, change) in enumerate(changes, 1):
            fh.write(banner(n, total, sha, meta.get(sha, {}), change) + "\n")
            try:
                fh.write(section_body(repo, change, args.context))
            except RuntimeError as exc:
                fh.write("(error: %s)" % exc)
            fh.write("\n\n")
        fh.write("### END (%d version(s))\n" % total)

    print("%s: %d version(s) -> %s" % (path, total, out))


if __name__ == "__main__":
    main()
