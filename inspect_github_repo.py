#!/usr/bin/env python3
"""
inspect_github_repo.py - read-only inspection of a single repo: remote URL,
HEAD/commit info, and whether it contains the files GitHub treats as
org-wide defaults when a repo is literally named ".github" (workflow
templates, CODEOWNERS, issue/PR templates, the org profile README, etc.).

Everything is read via `git -C <repo> ...` - never cd's into the repo, so
it's safe to point at any path without touching the current shell's cwd.
Includes the `-c safe.directory=*` override needed on foreign-uid mounts
(NTFS drives under /mnt, network shares) - see dangling_commits.py /
extract_commits.py for the same pattern.

Usage:
    inspect_github_repo.py /path/to/repo
    inspect_github_repo.py /mnt/4tb/.../AllRepos/ey-org/.github
"""

import argparse
import subprocess
import sys

GIT = ["git", "-c", "safe.directory=*"]

ORG_DEFAULT_MARKERS = [
    "profile/README.md", "README.md", "CONTRIBUTING.md", "SECURITY.md",
    "CODEOWNERS", ".github/CODEOWNERS",
    "ISSUE_TEMPLATE", ".github/ISSUE_TEMPLATE",
    "PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE.md",
    "workflow-templates",
]


def run(repo, *args):
    p = subprocess.run(GIT + ["-C", repo] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="path to the repository (never cd'd into)")
    ap.add_argument("--files", action="store_true",
                    help="also print the full tracked file list (can be long)")
    args = ap.parse_args()
    repo = args.repo

    out, err, rc = run(repo, "remote", "-v")
    print("remote(s):")
    print(out or "  (none)")
    if err:
        print("  stderr:", err)

    out, err, rc = run(repo, "rev-parse", "--is-bare-repository")
    print("\nbare repo:", out if rc == 0 else f"<error: {err}>")

    out, err, rc = run(repo, "log", "-1", "--format=%H %ad %s", "--date=short")
    print("HEAD commit:", out if rc == 0 else f"<error: {err}>")

    out, err, rc = run(repo, "rev-list", "--all", "--count")
    print("commit count:", out if rc == 0 else f"<error: {err}>")

    print("\ntop-level tracked entries:")
    out, err, rc = run(repo, "ls-tree", "--name-only", "HEAD")
    if rc == 0:
        for line in out.splitlines():
            print(" ", line)
    else:
        print("  <error:", err, ">")

    out, err, rc = run(repo, "ls-tree", "-r", "--name-only", "HEAD")
    files = set(out.splitlines()) if rc == 0 else set()
    if rc != 0:
        print("\n<error listing full tree:", err, ">")
        return 1
    print(f"\n{len(files)} tracked file(s) total")

    if args.files:
        print("\nfull tracked file list:")
        for f in sorted(files):
            print(" ", f)

    print("\nmatches against known org-wide-default filenames:")
    any_hit = False
    for marker in ORG_DEFAULT_MARKERS:
        hits = [f for f in files if f == marker or f.startswith(marker + "/")
               or ("/" + marker) in f]
        if hits:
            any_hit = True
            print(f"  {marker}: {len(hits)} file(s)")
            for f in hits[:5]:
                print(f"    - {f}")
            if len(hits) > 5:
                print(f"    ... and {len(hits) - 5} more")
    if not any_hit:
        print("  (none found)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
