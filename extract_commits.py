#!/usr/bin/env python3
"""
extract_commits.py - materialise every commit of a git repo into its own folder.

Given a repo path, writes:

    <out>/
      manifest.json          run parameters, totals, timings
      commits.csv            one row per commit
      refs.csv               every branch/tag - live, or recovered from reflogs
      file_churn.csv         every path ranked by commits that touched it, plus
                             branch_count (and branch names with --branch-list)
      tree_churn.csv         every directory ranked by commits that touched it
      file_history.csv       repo / tree / commit / file path / nth change, per change
      <commit-sha>/          (not written with --churn-only)
        metadata.json        author, committer, parents, tree, refs, branches, counts
        tree.csv             every path at that commit: path, mode, blob, size
        changes.csv          what the commit touched vs its first parent
        files/               file content (see --content)

Commit enumeration covers every ref (local branches, remote-tracking branches,
tags), the reflog, and - with --dangling - commits still in the pack that no
ref reaches any more.

Note on scope. A clone only ever received objects reachable from the refs the
server advertised at clone time, so branches deleted on the server before the
clone left no trace and no flag can recover them.

Branch names are also weaker evidence than commits. `git branch -D` deletes
the branch's reflog along with the ref, and `git fetch --prune` drops a
remote-tracking ref without leaving a reflog behind - so a deleted branch's
NAME usually survives only as prose, in a `checkout: moving from X to Y`
reflog action or a `Merge branch 'X'` subject. refs.csv reports those with
confidence=inferred to keep them apart from refs that genuinely exist. The
COMMITS on such a branch normally do survive, reachable via --reflog or
--dangling; they just no longer carry a branch name.
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Repos on foreign-uid mounts (Windows drives under /mnt, network shares) trip
# git's "detected dubious ownership" check, which aborts every plumbing call
# before it emits a byte. safe.directory is only honoured from protected
# config - system, global, or the command line - so pass it per-invocation.
GIT = ["git", "-c", "safe.directory=*"]

FS = "\x1f"   # field separator inside a commit record
RS = "\x1e"   # record separator between commits

LOG_FMT = FS.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI",
                   "%T", "%s", "%B"]) + RS

# reflog line: <old> <new> <who> <ts> <tz>\t<action>: <message>
REFLOG_RE = re.compile(r"^([0-9a-f]{40}) ([0-9a-f]{40}) (.*?)\t(.*)$")

# Branch names that survive only as prose. Deleting a branch also deletes its
# reflog file, and `fetch --prune` removes a remote-tracking ref without
# leaving one behind, so these action messages and merge subjects are the only
# local record that such a branch ever had a name.
CHECKOUT_RE = re.compile(r"^checkout: moving from (\S+) to (\S+)$")
MERGE_ACTION_RE = re.compile(r"^merge ([^:]+):")
BRANCH_FROM_RE = re.compile(r"^branch: Created from (\S+)")
REBASE_RE = re.compile(r"^rebase[^:]*: checkout (\S+)")
SUBJ_BRANCH_RE = re.compile(r"Merge (?:remote-tracking )?branch '([^']+)'")
SUBJ_PR_RE = re.compile(r"Merge pull request #(\d+) from (\S+)")
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _plausible_branch(name):
    """Filter out detached-HEAD SHAs and placeholders from action messages."""
    if not name or HEX40.match(name) or name in ("HEAD", "-"):
        return False
    return not name.startswith("refs/") or name.count("/") >= 2


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def git_out(repo, args, check=True):
    """Run a git command, return stdout as text."""
    proc = subprocess.run(GIT + ["-C", repo] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed (%d): %s"
                           % (" ".join(args[:3]), proc.returncode,
                              proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout.decode("utf-8", "replace")


def git_dir(repo):
    return git_out(repo, ["rev-parse", "--absolute-git-dir"]).strip()


def live_refs(repo):
    """Every ref that currently exists. Annotated tags also report their target."""
    fmt = "%(objectname)%09%(objecttype)%09%(*objectname)%09%(refname)"
    rows = []
    for line in git_out(repo, ["for-each-ref", "--format=" + fmt]).splitlines():
        if not line.strip():
            continue
        obj, otype, peeled, name = (line.split("\t") + ["", "", "", ""])[:4]
        rows.append({"ref": name, "sha": obj, "type": otype,
                     "commit": peeled or obj, "source": "live"})
    return rows


def reflog_refs(repo, gdir):
    """Ref names recovered from reflogs.

    A branch deleted after the clone (or pruned by `fetch --prune`) loses its
    ref but keeps its log under .git/logs/, and that log still carries the
    name and every SHA the branch ever pointed at. It is the only local record
    of a branch that no longer exists.
    """
    logs_root = os.path.join(gdir, "logs")
    mentioned = {}
    if not os.path.isdir(logs_root):
        return [], mentioned
    out = []
    for dirpath, _dirnames, filenames in os.walk(logs_root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            name = os.path.relpath(full, logs_root).replace(os.sep, "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            except OSError:
                continue
            if not lines:
                continue
            m = REFLOG_RE.match(lines[-1])
            tip = m.group(2) if m else ""
            seen = set()
            # Walking HEAD's log chronologically lets us follow which branch
            # was checked out, so a commit line can be attributed to it. That
            # recovers a deleted branch's last known tip, not just the commit
            # it was forked from.
            current = None
            for ln in lines:
                mm = REFLOG_RE.match(ln)
                if not mm:
                    continue
                old_sha, new_sha, _who, action = mm.groups()
                seen.add(old_sha)
                seen.add(new_sha)

                co = CHECKOUT_RE.match(action)
                if co:
                    _note(mentioned, co.group(1), old_sha, name)
                    _note(mentioned, co.group(2), new_sha, name)
                    if name == "HEAD":
                        current = co.group(2)
                elif name == "HEAD" and current:
                    _note(mentioned, current, new_sha, name, overwrite=True)
                for rx in (MERGE_ACTION_RE, BRANCH_FROM_RE, REBASE_RE):
                    hit = rx.match(action)
                    if hit:
                        _note(mentioned, hit.group(1), "", name)
            seen.discard("0" * 40)
            out.append({"ref": name, "sha": tip, "type": "reflog",
                        "commit": tip, "source": "reflog",
                        "historic": sorted(seen)})
    return out, mentioned


def _note(store, name, sha, evidence, overwrite=False):
    """Record a branch name seen in prose, keeping the best SHA we have.

    overwrite=True is for walking forward through HEAD's log, where a later
    line supersedes an earlier one and the last write is the branch's tip.
    """
    if not _plausible_branch(name):
        return
    rec = store.setdefault(name, {"sha": "", "evidence": set()})
    if sha and (overwrite or not HEX40.match(rec["sha"] or "")):
        rec["sha"] = sha
    rec["evidence"].add(evidence)


def names_from_merge_subjects(meta):
    """Branch names quoted in merge commit messages.

    `Merge branch 'feature/x'` and GitHub's `Merge pull request #12 from
    org/fix-auth` name a branch that may no longer exist anywhere. This is
    inference from text, not from git's object model - treat it as a lead.
    """
    found = {}
    for sha, m in meta.items():
        subject = m.get("subject") or ""
        for hit in SUBJ_BRANCH_RE.finditer(subject):
            _note(found, hit.group(1), "", "merge:" + sha[:12])
        pr = SUBJ_PR_RE.search(subject)
        if pr:
            branch = pr.group(2).split("/", 1)[-1] if "/" in pr.group(2) \
                else pr.group(2)
            _note(found, branch, "", "pr#%s:%s" % (pr.group(1), sha[:12]))
    return found


def branch_refs(live):
    """{branch name: (ref, tip commit)} for local and remote-tracking branches.
    Tags and the symbolic origin/HEAD are skipped."""
    out = {}
    for r in live:
        ref = r["ref"]
        if ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/"):]
        elif ref.startswith("refs/remotes/") and not ref.endswith("/HEAD"):
            name = ref[len("refs/"):]           # remotes/origin/foo
        else:
            continue
        out[name] = (ref, r["commit"])
    return out


def branch_membership(repo, live, wanted, progress=None):
    """{commit sha: {branch names that contain it}}. A commit belongs to a
    branch if it is reachable from that branch's tip, so history shared with
    main shows up under every branch that descends from it. Only commits in
    `wanted` are kept."""
    member = {}
    branches = branch_refs(live)
    for i, (name, (ref, _tip)) in enumerate(branches.items(), 1):
        if progress:
            progress.update(i - 1)
        try:
            shas = git_out(repo, ["rev-list", ref]).split()
        except RuntimeError:
            continue
        for sha in shas:
            if sha in wanted:
                member.setdefault(sha, set()).add(name)
    if progress:
        progress.close()
    return member


def reachable_commits(repo, revs, since, until, limit, oldest_first=False):
    """Commits reachable from refs and the reflog, newest first (or, with
    oldest_first, parents strictly before children)."""
    args = ["rev-list"]
    if oldest_first:
        args += ["--topo-order", "--reverse"]
    args += revs if revs else ["--all", "--reflog"]
    if since:
        args.append("--since=" + since)
    if until:
        args.append("--until=" + until)
    if limit:
        args.append("--max-count=%d" % limit)
    return [ln.strip() for ln in git_out(repo, args).splitlines() if ln.strip()]


def pack_commits(repo):
    """Every commit object physically present, reachable or not."""
    proc = subprocess.Popen(
        GIT + ["-C", repo, "cat-file", "--batch-all-objects",
               "--batch-check=%(objecttype) %(objectname)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    for raw in proc.stdout:
        line = raw.decode("ascii", "replace").split()
        if len(line) == 2 and line[0] == "commit":
            out.append(line[1])
    proc.wait()
    return out


def commit_metadata(repo, shas, chunk=4000):
    """Batch-read commit headers. One git process per chunk, not per commit."""
    meta = {}
    for i in range(0, len(shas), chunk):
        batch = shas[i:i + chunk]
        proc = subprocess.run(
            GIT + ["-C", repo, "log", "--no-walk", "--stdin",
                   "--format=" + LOG_FMT],
            input="\n".join(batch).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError("git log failed: %s"
                               % proc.stderr.decode("utf-8", "replace")[:400])
        text = proc.stdout.decode("utf-8", "replace")
        for record in text.split(RS):
            record = record.strip("\n")
            if not record.strip():
                continue
            f = record.split(FS)
            if len(f) < 11:
                continue
            meta[f[0]] = {
                "sha": f[0],
                "parents": f[1].split() if f[1] else [],
                "author_name": f[2], "author_email": f[3], "author_date": f[4],
                "committer_name": f[5], "committer_email": f[6],
                "committer_date": f[7],
                "tree": f[8], "subject": f[9], "message": f[10],
            }
    return meta


def ls_tree(repo, sha):
    """Every blob at a commit: (path, mode, blob_sha, size)."""
    proc = subprocess.run(GIT + ["-C", repo, "ls-tree", "-r", "-l", "-z", sha],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:300])
    rows = []
    for entry in proc.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        head, _, path = entry.partition("\t")
        parts = head.split()
        if len(parts) < 4:
            continue
        mode, otype, blob, size = parts[0], parts[1], parts[2], parts[3]
        if otype != "blob":
            continue          # submodule gitlinks carry no content here
        rows.append((path, mode, blob, -1 if size == "-" else int(size)))
    return rows


def diff_tree(repo, sha, parents):
    """Raw diff against the first parent (or the empty tree for a root commit).

    Merges are diffed against their first parent only: that is the change the
    merge introduced onto the branch it landed on, which is what a per-commit
    view wants.
    """
    args = ["diff-tree", "-r", "-z", "-M", "--no-commit-id"]
    args += [parents[0], sha] if parents else ["--root", sha]
    proc = subprocess.run(GIT + ["-C", repo] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return []
    fields = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    rows, i = [], 0
    while i < len(fields):
        head = fields[i]
        if not head.startswith(":"):
            i += 1
            continue
        parts = head[1:].split()
        if len(parts) < 5:
            i += 1
            continue
        src_mode, dst_mode, src_sha, dst_sha, status = parts[:5]
        # R and C statuses carry a score suffix and two paths
        if status[0] in ("R", "C"):
            src_path = fields[i + 1] if i + 1 < len(fields) else ""
            dst_path = fields[i + 2] if i + 2 < len(fields) else ""
            i += 3
        else:
            src_path = ""
            dst_path = fields[i + 1] if i + 1 < len(fields) else ""
            i += 2
        rows.append({"status": status, "path": dst_path, "old_path": src_path,
                     "old_mode": src_mode, "new_mode": dst_mode,
                     "old_blob": src_sha, "new_blob": dst_sha})
    return rows


# --------------------------------------------------------------------------
# content extraction
# --------------------------------------------------------------------------

def extract_full_tree(repo, sha, dest):
    """Stream `git archive` straight into dest - no temp tarball on disk."""
    os.makedirs(dest, exist_ok=True)
    proc = subprocess.Popen(GIT + ["-C", repo, "archive", "--format=tar", sha],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(path=dest, filter="data")
    except tarfile.ReadError:
        pass          # a commit with an empty tree yields an empty archive
    finally:
        if proc.stdout:
            proc.stdout.read()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("git archive: " + err.strip()[:300])


def extract_changed(repo, changes, dest):
    """Write only the blobs this commit added or modified.

    Blob SHAs come straight out of diff-tree, so one `cat-file --batch` process
    serves the whole commit regardless of how many paths changed - no argv
    limits, no per-file process spawn.
    """
    wanted = [c for c in changes
              if c["status"][0] not in ("D",)
              and c["new_blob"] and set(c["new_blob"]) != {"0"}]
    if not wanted:
        os.makedirs(dest, exist_ok=True)
        return 0
    os.makedirs(dest, exist_ok=True)
    proc = subprocess.Popen(GIT + ["-C", repo, "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    written = 0
    try:
        for c in wanted:
            proc.stdin.write((c["new_blob"] + "\n").encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode("utf-8", "replace").split()
            if len(header) < 3 or header[1] != "blob":
                if len(header) >= 2 and header[1] == "missing":
                    continue
                continue
            size = int(header[2])
            body = b""
            while len(body) < size:
                block = proc.stdout.read(size - len(body))
                if not block:
                    break
                body += block
            proc.stdout.read(1)                       # trailing newline
            if len(body) != size:
                raise RuntimeError("short read on blob " + c["new_blob"])
            out_path = os.path.join(dest, c["path"])
            os.makedirs(os.path.dirname(out_path) or dest, exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(body)
            written += 1
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait()
    return written


# --------------------------------------------------------------------------
# per-commit worker
# --------------------------------------------------------------------------

def process_commit(repo, sha, folder, meta, refs_at, content):
    if os.path.exists(os.path.join(folder, ".complete")):
        with open(os.path.join(folder, "metadata.json")) as fh:
            cached = json.load(fh)
        return {"sha": sha, "skipped": True,
                "files": cached.get("file_count", 0),
                "bytes": cached.get("total_bytes", 0),
                "changed": cached.get("changed_count", 0), "error": ""}

    os.makedirs(folder, exist_ok=True)
    tree = ls_tree(repo, sha)
    total_bytes = sum(s for _p, _m, _b, s in tree if s > 0)

    with open(os.path.join(folder, "tree.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "mode", "blob_sha", "size"])
        for path, mode, blob, size in tree:
            w.writerow([path, mode, blob, size if size >= 0 else ""])

    changes = diff_tree(repo, sha, meta["parents"])
    with open(os.path.join(folder, "changes.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "path", "old_path", "old_blob", "new_blob",
                    "old_mode", "new_mode"])
        for c in changes:
            w.writerow([c["status"], c["path"], c["old_path"],
                        c["old_blob"], c["new_blob"],
                        c["old_mode"], c["new_mode"]])

    files_dir = os.path.join(folder, "files")
    if content == "full":
        extract_full_tree(repo, sha, files_dir)
    elif content == "changed":
        extract_changed(repo, changes, files_dir)

    record = dict(meta)
    record.update({
        "refs": refs_at.get(sha, []),
        "file_count": len(tree),
        "total_bytes": total_bytes,
        "changed_count": len(changes),
        "content_mode": content,
        "is_merge": len(meta["parents"]) > 1,
        "is_root": not meta["parents"],
    })
    with open(os.path.join(folder, "metadata.json"), "w",
              encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    with open(os.path.join(folder, ".complete"), "w") as fh:
        fh.write(str(int(time.time())))

    return {"sha": sha, "skipped": False, "files": len(tree),
            "bytes": total_bytes, "changed": len(changes), "error": ""}


# --------------------------------------------------------------------------
# file churn
# --------------------------------------------------------------------------

def changes_from_folders(ordered, folder_for):
    """(sha, changes) per commit, read back from each commit folder's
    changes.csv. Commits whose folder has no changes.csv are skipped."""
    for sha in ordered:
        path = os.path.join(folder_for(sha), "changes.csv")
        try:
            with open(path, newline="", encoding="utf-8",
                      errors="surrogateescape") as fh:
                yield sha, list(csv.DictReader(fh))
        except OSError:
            continue


def _parse_log_record(part):
    """One `git log --raw -z` record -> (sha, [change dicts])."""
    toks = part.decode("utf-8", "surrogateescape").split("\0")
    sha = toks[0].strip()
    changes, i = [], 1
    while i < len(toks):
        head = toks[i].lstrip("\n")
        if not head.startswith(":"):
            i += 1
            continue
        fields = head[1:].split()
        if len(fields) < 5:
            i += 1
            continue
        old_mode, new_mode, old_blob, new_blob, status = fields[:5]
        if status[0] in ("R", "C"):
            old_path = toks[i + 1] if i + 1 < len(toks) else ""
            path = toks[i + 2] if i + 2 < len(toks) else ""
            i += 3
        else:
            old_path = ""
            path = toks[i + 1] if i + 1 < len(toks) else ""
            i += 2
        changes.append({"status": status, "path": path, "old_path": old_path,
                        "old_mode": old_mode, "new_mode": new_mode,
                        "old_blob": old_blob, "new_blob": new_blob})
    return sha, changes


def changes_from_git(repo, revs, since, until, limit):
    """(sha, changes) per commit, oldest first (parents before children),
    streamed from ONE `git log` over the object store - no commit folders, no
    file content. Merges are diffed against their first parent and renames are
    detected (-M), exactly as diff_tree() does per commit."""
    cmd = GIT + ["-C", repo, "log"] + (revs if revs else ["--all", "--reflog"])
    cmd += ["--topo-order", "--reverse", "--raw", "-z", "-M", "--no-abbrev",
            "--diff-merges=first-parent", "--format=%x1e%H"]
    if since:
        cmd.append("--since=" + since)
    if until:
        cmd.append("--until=" + until)
    if limit:
        cmd.append("--max-count=%d" % limit)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        buf = b""
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            buf += chunk
            parts = buf.split(b"\x1e")
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    yield _parse_log_record(part)
        if buf.strip():
            yield _parse_log_record(buf)
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError("git log failed: "
                               + err.read().decode("utf-8", "replace")[:400])


def write_file_churn(out, changes_iter, meta, repo_name="",
                     commit_branches=None, branch_list=False,
                     changed_counts=None):
    """Rank paths by how many commits touched them, into file_churn.csv.

    Built from `changes_iter`, a stream of (sha, changes) over reachable commits
    only (dangling ones would double-count work already on a branch). It comes
    either from the per-commit changes.csv files or straight from git (see
    changes_from_folders / changes_from_git) and must be oldest-first, parents
    before children (git --topo-order), so renames are applied in the order
    they happened. If given, `changed_counts` is filled with {sha: number of
    changed paths}.
    A rename (R) moves the old path's history onto the new path, so a renamed
    file keeps one row under its latest name. Merges count only what they
    changed against their first parent, as in changes.csv.

    Also writes, from the same pass:
      tree_churn.csv    one row per directory (a git "tree"): how many commits
                        changed anything under it, and how many file changes.
                        "tree" is "." for the repo root.
      file_history.csv  one row per (commit, file change): repo, tree (the
                        file's directory), commit, path, status, and nth_change
                        - how many times that file had changed up to and
                        including this commit (renames carry the count over).
    file_churn.csv also carries branch_count: how many branches contain at least
    one commit that changed the file (see branch_membership). With branch_list
    it gets an extra `branches` column naming them, joined with "|".
    Returns (files, trees, history_rows).
    """
    commit_branches = commit_branches or {}
    stats = {}
    trees = {}
    hist_rows = 0
    hist_fh = open(os.path.join(out, "file_history.csv"), "w", newline="",
                   encoding="utf-8", errors="surrogateescape")
    hist = csv.writer(hist_fh)
    hist.writerow(["repo", "tree", "commit", "commit_date", "author", "subject",
                   "path", "old_path", "status", "nth_change",
                   "old_blob", "new_blob"])

    def dirs_of(path):
        """'a/b/c.txt' -> ['.', 'a', 'a/b']"""
        parts = path.split("/")[:-1]
        return ["."] + ["/".join(parts[:i + 1]) for i in range(len(parts))]

    def tree_rec(d):
        return trees.setdefault(d, {"commits": 0, "changes": 0, "files": set(),
                                    "first": "", "last": ""})

    def rec(path):
        return stats.setdefault(path, {
            "commits": 0, "added": 0, "modified": 0, "deleted": 0,
            "renamed": 0, "first": "", "last": "",
            "branches": set()})

    for sha, changes in changes_iter:
        date = meta.get(sha, {}).get("committer_date", "")
        if changed_counts is not None:
            changed_counts[sha] = len(changes)
        touched = set()
        m = meta.get(sha, {})
        for c in changes:
            status, p, old = c["status"][:1], c["path"], c["old_path"]
            if status == "R" and old and old in stats:
                moved = stats.pop(old)
                cur = rec(p)
                for k in ("commits", "added", "modified", "deleted", "renamed"):
                    cur[k] += moved[k]
                cur["first"] = min(x for x in (cur["first"], moved["first"]) if x) \
                    if (cur["first"] or moved["first"]) else ""
                cur["branches"] |= moved["branches"]
            r = rec(p)
            r["commits"] += 1
            key = {"A": "added", "D": "deleted", "R": "renamed"}.get(status, "modified")
            r[key] += 1
            r["first"] = r["first"] or date
            r["last"] = date
            r["branches"] |= commit_branches.get(sha, frozenset())

            for d in dict.fromkeys(dirs_of(p) + (dirs_of(old) if old else [])):
                t = tree_rec(d)
                t["changes"] += 1
                t["files"].add(p)
                t["first"] = t["first"] or date
                t["last"] = date
                touched.add(d)
            hist.writerow([repo_name, "/".join(p.split("/")[:-1]) or ".", sha,
                           date, m.get("author_name", ""),
                           (m.get("subject", "") or "").replace("\n", " "),
                           p, old, c["status"], r["commits"],
                           c["old_blob"], c["new_blob"]])
            hist_rows += 1
        for d in touched:
            trees[d]["commits"] += 1
    hist_fh.close()

    with open(os.path.join(out, "tree_churn.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["repo", "tree", "commits_touched", "file_changes",
                    "distinct_files", "first_changed", "last_changed"])
        for d, t in sorted(trees.items(), key=lambda kv: (-kv[1]["commits"], kv[0])):
            w.writerow([repo_name, d, t["commits"], t["changes"],
                        len(t["files"]), t["first"], t["last"]])

    with open(os.path.join(out, "file_churn.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "commits_touched", "added", "modified", "deleted",
                    "renamed", "first_seen", "last_changed",
                    "branch_count"] + (["branches"] if branch_list else []))
        for p, r in sorted(stats.items(), key=lambda kv: (-kv[1]["commits"], kv[0])):
            w.writerow([p, r["commits"], r["added"], r["modified"], r["deleted"],
                        r["renamed"], r["first"], r["last"],
                        len(r["branches"])] +
                       (["|".join(sorted(r["branches"]))] if branch_list else []))
    return len(stats), len(trees), hist_rows


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

class Progress:
    """One-line progress bar on stderr: [####----] 42% 630/1,495 | 210/s | ETA.

    Redraws in place on a terminal; when stderr is not a terminal (log file,
    pipe) it prints a plain line every few seconds instead. `enabled=False`
    (--quiet) makes every call a no-op."""

    def __init__(self, label, total, enabled=True):
        self.label, self.total, self.enabled = label, max(total, 0), enabled
        self.tty = sys.stderr.isatty()
        self.start = self.last = time.time()
        self.gap = 0.2 if self.tty else 5.0
        self.done = 0

    def update(self, done, extra=""):
        self.done = done
        if not self.enabled:
            return
        now = time.time()
        if now - self.last < self.gap or done >= self.total:
            return                      # the last frame is drawn by close()
        self.last = now
        self._draw(extra, final=False)

    def _draw(self, extra, final):
        elapsed = time.time() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0
        frac = min(1.0, self.done / self.total) if self.total else 1.0
        eta = (self.total - self.done) / rate if rate and self.total else 0
        width = 24
        bar = "#" * int(width * frac) + "-" * (width - int(width * frac))
        line = ("%-10s [%s] %5.1f%% %s/%s | %s/s | %s ETA %s%s"
                % (self.label, bar, frac * 100, f"{self.done:,}",
                   f"{self.total:,}", f"{rate:,.0f}", _hms(elapsed),
                   _hms(eta), (" | " + extra) if extra else ""))
        if self.tty:
            print("\r" + line.ljust(110), end="\n" if final else "",
                  file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)

    def close(self, extra=""):
        if self.enabled:
            self.done = max(self.done, self.total)
            self._draw(extra, final=True)


def _hms(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def with_progress(stream, progress):
    """Pass a (sha, changes) stream through, ticking the bar per commit."""
    n = files = 0
    for sha, changes in stream:
        n += 1
        files += len(changes)
        progress.update(n, "%s change(s)" % f"{files:,}")
        yield sha, changes
    progress.close("%s change(s)" % f"{files:,}")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.2f %s" % (n, unit)
        n /= 1024.0


def write_commits_csv(out, commits, meta, refs_at, membership, reachable_set,
                      extras):
    """commits.csv. extras(sha) -> (file_count, total_bytes, changed_files,
    folder, error) for the columns that depend on extraction."""
    with open(os.path.join(out, "commits.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["seq", "sha", "parents", "is_merge", "is_root",
                    "author_name", "author_email", "author_date",
                    "committer_date", "tree", "subject",
                    "file_count", "total_bytes", "changed_files",
                    "refs", "branch_count", "branches", "reachable", "folder",
                    "error"])
        for i, sha in enumerate(commits):
            m = meta.get(sha, {})
            files, nbytes, changed, folder, error = extras(sha)
            w.writerow([
                i, sha, " ".join(m.get("parents", [])),
                int(len(m.get("parents", [])) > 1),
                int(not m.get("parents", [])),
                m.get("author_name", ""), m.get("author_email", ""),
                m.get("author_date", ""), m.get("committer_date", ""),
                m.get("tree", ""), (m.get("subject", "") or "").replace("\n", " "),
                files, nbytes, changed,
                " ".join(refs_at.get(sha, [])),
                len(membership.get(sha, ())),
                "|".join(sorted(membership.get(sha, ()))),
                int(sha in reachable_set), folder, error,
            ])


def run_churn_only(args, repo, gdir, t0, commits, reachable_set, meta,
                   membership, refs_at, live, recovered, inferred):
    """--churn-only: build the summary CSVs straight from the object store.
    No commit folders, no file content, no per-commit CSVs."""
    counts = {}
    stream = with_progress(
        changes_from_git(repo, args.rev, args.since, args.until, args.limit),
        Progress("churn", len(reachable_set), not args.quiet))
    n_files, n_trees, n_hist = write_file_churn(
        args.out, stream, meta, os.path.basename(repo), membership,
        args.branch_list, counts)
    print("churn     %d file(s) -> file_churn.csv, %d tree(s) -> tree_churn.csv, "
          "%d change row(s) -> file_history.csv" % (n_files, n_trees, n_hist))

    # file_count / total_bytes / folder need a tree walk or an extraction, so
    # they stay blank rather than showing a misleading 0.
    write_commits_csv(args.out, commits, meta, refs_at, membership,
                      reachable_set,
                      lambda sha: ("", "", counts.get(sha, 0), "", ""))
    manifest = {
        "repo": repo, "git_dir": gdir, "generated": int(t0),
        "elapsed_seconds": round(time.time() - t0, 1),
        "mode": "churn-only",
        "commits_total": len(commits),
        "commits_reachable": len(reachable_set),
        "refs_live": len(live), "refs_recovered_from_reflog": len(recovered),
        "refs_inferred_from_prose": len(inferred),
        "revs": args.rev or ["--all", "--reflog"],
    }
    with open(os.path.join(args.out, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print("\ndone  %d commits (churn only, nothing extracted) -> %s"
          % (len(commits), args.out))
    print("      %.1fs" % (time.time() - t0))


def main():
    ap = argparse.ArgumentParser(
        description="Materialise every commit of a repo into its own folder.")
    ap.add_argument("repo", help="path to the repository (work tree or .git)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--content", choices=["none", "changed", "full"],
                    default="full",
                    help="what to put in each commit's files/ dir: nothing, "
                         "only the paths that commit touched, or the whole "
                         "tree (default: full)")
    ap.add_argument("--dangling", action="store_true",
                    help="also extract commits in the pack that no ref reaches")
    ap.add_argument("--rev", action="append", default=[],
                    help="restrict to these revs (repeatable); default is "
                         "--all --reflog")
    ap.add_argument("--since", help="only commits after this date")
    ap.add_argument("--until", help="only commits before this date")
    ap.add_argument("--limit", type=int, help="cap the number of commits")
    ap.add_argument("--layout", choices=["sha", "seq"], default="sha",
                    help="folder name: full sha, or NNNNN_<short sha> in "
                         "reverse-chronological order (default: sha)")
    ap.add_argument("--churn-only", action="store_true",
                    help="write only the summary CSVs (commits, refs, "
                         "file/tree churn, file history), read straight from "
                         "the object store with one git log - no commit "
                         "folders, no file content. Merges are diffed against "
                         "the first parent, renames are detected. Ignores "
                         "--content, --layout, --resume and --dangling.")
    ap.add_argument("--quiet", action="store_true",
                    help="no progress bars (the summary lines still print)")
    ap.add_argument("--branch-list", action="store_true",
                    help="add a `branches` column to file_churn.csv naming the "
                         "branches each file changed on (default: only the "
                         "branch_count)")
    ap.add_argument("--jobs", type=int, default=8, help="parallel workers")
    ap.add_argument("--resume", action="store_true",
                    help="skip commit folders already marked complete")
    ap.add_argument("--estimate", action="store_true",
                    help="report projected commit count and disk size, then "
                         "exit without extracting")
    ap.add_argument("--max-gb", type=float,
                    help="abort before extracting if the projection exceeds "
                         "this many GB")
    args = ap.parse_args()
    if args.churn_only:
        if args.dangling:
            print("note: --dangling is ignored with --churn-only "
                  "(churn covers reachable commits only)")
            args.dangling = False
        args.content = "none"

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        sys.exit("not a directory: " + repo)
    try:
        gdir = git_dir(repo)
    except RuntimeError as exc:
        sys.exit("not a git repository: %s\n  %s" % (repo, exc))

    t0 = time.time()
    print("repo      %s" % repo)
    print("git dir   %s" % gdir)

    # ---- refs -------------------------------------------------------------
    live = live_refs(repo)
    reflog, mentioned = reflog_refs(repo, gdir)
    live_names = {r["ref"] for r in live}
    recovered = [r for r in reflog if r["ref"] not in live_names
                 and r["ref"] != "HEAD"]
    print("refs      %d live, %d with a reflog but no ref"
          % (len(live), len(recovered)))

    # ---- commits ----------------------------------------------------------
    commits = reachable_commits(repo, args.rev, args.since, args.until,
                                args.limit)
    reachable_set = set(commits)
    dangling = []
    if args.dangling:
        dangling = [c for c in pack_commits(repo) if c not in reachable_set]
        commits += dangling
    print("commits   %d reachable%s"
          % (len(reachable_set),
             (", %d dangling" % len(dangling)) if args.dangling else ""))

    if not commits:
        sys.exit("no commits found - empty repository?")

    # ---- projection -------------------------------------------------------
    sample = ([] if args.content == "none"
              else commits[::max(1, len(commits) // 25)][:25])
    sizes = []
    for sha in sample:
        try:
            sizes.append(sum(s for _p, _m, _b, s in ls_tree(repo, sha) if s > 0))
        except RuntimeError:
            pass
    avg = (sum(sizes) / len(sizes)) if sizes else 0
    if args.content == "full":
        projected = avg * len(commits)
    elif args.content == "changed":
        projected = avg * len(commits) * 0.02      # rough: touched paths only
    else:
        projected = 0
    if args.content != "none":
        print("avg tree  %s across %d sampled commits" % (human(avg), len(sizes)))
    if not args.churn_only:
        print("projected %s on disk with --content %s"
              % (human(projected), args.content))
    if args.content == "full" and len(commits) > 1:
        print("          (--content changed would be roughly %s)"
              % human(avg * len(commits) * 0.02))

    if args.estimate:
        return
    if args.max_gb and projected / (1024 ** 3) > args.max_gb:
        sys.exit("aborting: projection %s exceeds --max-gb %.2f"
                 % (human(projected), args.max_gb))

    os.makedirs(args.out, exist_ok=True)

    # ---- metadata ---------------------------------------------------------
    meta = commit_metadata(repo, commits)

    # Which branches contain each commit (a commit records no branch of its
    # own; membership is reachability from a branch tip). Reflog-only and
    # dangling commits belong to none.
    membership = branch_membership(
        repo, live, set(commits),
        Progress("branches", len(branch_refs(live)), not args.quiet))

    # ---- refs.csv ---------------------------------------------------------
    # Three tiers of confidence: refs that exist, refs whose log outlived them,
    # and names that survive only as prose in a reflog action or merge subject.
    # A branch named feature/x lives at refs/heads/feature/x, so match on the
    # whole name after the prefix - splitting on "/" and taking the last
    # segment would leave "x" and wrongly report the branch as deleted.
    short_live = set()
    for r in live:
        ref = r["ref"]
        for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
            if ref.startswith(prefix):
                rest = ref[len(prefix):]
                short_live.add(rest)
                if prefix == "refs/remotes/" and "/" in rest:
                    short_live.add(rest.split("/", 1)[1])   # drop the remote
                break
    inferred = dict(mentioned)
    for name, rec in names_from_merge_subjects(meta).items():
        tgt = inferred.setdefault(name, {"sha": "", "evidence": set()})
        tgt["evidence"] |= rec["evidence"]
    inferred = {n: r for n, r in inferred.items()
                if n not in short_live and n not in live_names}

    with open(os.path.join(args.out, "refs.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ref", "kind", "sha", "commit", "source", "confidence",
                    "evidence", "historic_shas"])
        for r in live:
            kind = ("branch" if r["ref"].startswith("refs/heads/") else
                    "remote" if r["ref"].startswith("refs/remotes/") else
                    "tag" if r["ref"].startswith("refs/tags/") else "other")
            w.writerow([r["ref"], kind, r["sha"], r["commit"], "ref",
                        "certain", "", ""])
        for r in recovered:
            w.writerow([r["ref"], "deleted", r["sha"], r["commit"], "reflog",
                        "certain", "orphaned reflog",
                        " ".join(r.get("historic", []))])
        for name, rec in sorted(inferred.items()):
            w.writerow([name, "deleted", rec["sha"], rec["sha"],
                        "inferred", "inferred",
                        " ".join(sorted(rec["evidence"])), ""])
    print("          %d name(s) recoverable only from reflog actions or "
          "merge subjects" % len(inferred))
    refs_at = {}
    for r in live:
        refs_at.setdefault(r["commit"], []).append(r["ref"])

    if args.churn_only:
        return run_churn_only(args, repo, gdir, t0, commits, reachable_set,
                              meta, membership, refs_at, live, recovered,
                              inferred)

    order = {sha: i for i, sha in enumerate(commits)}

    def folder_for(sha):
        if args.layout == "seq":
            return os.path.join(args.out, "%06d_%s" % (order[sha], sha[:12]))
        return os.path.join(args.out, sha)

    # ---- extract ----------------------------------------------------------
    print("extracting %d commits with %d workers ..." % (len(commits), args.jobs))
    results, done, errors = [], 0, 0
    bar = Progress("extract", len(commits), not args.quiet)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {}
        for sha in commits:
            m = meta.get(sha)
            if m is None:
                errors += 1
                results.append({"sha": sha, "error": "metadata missing",
                                "files": 0, "bytes": 0, "changed": 0,
                                "skipped": False})
                continue
            folder = folder_for(sha)
            if not args.resume and os.path.exists(os.path.join(folder, ".complete")):
                shutil.rmtree(folder, ignore_errors=True)
            futures[pool.submit(process_commit, repo, sha, folder, m,
                                refs_at, args.content)] = sha

        for fut in as_completed(futures):
            sha = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                errors += 1
                res = {"sha": sha, "error": "%s: %s" % (type(exc).__name__, exc),
                       "files": 0, "bytes": 0, "changed": 0, "skipped": False}
            results.append(res)
            done += 1
            bar.update(done, "%d error%s" % (errors, "" if errors == 1 else "s"))
        bar.close("%d error%s" % (errors, "" if errors == 1 else "s"))

    # ---- branches into each commit's metadata.json --------------------------
    # Done after extraction so commit folders skipped by --resume get it too.
    for sha in commits:
        mpath = os.path.join(folder_for(sha), "metadata.json")
        try:
            with open(mpath, encoding="utf-8") as fh:
                rec = json.load(fh)
            names = sorted(membership.get(sha, ()))
            if rec.get("branches") != names:
                rec["branches"] = names
                rec["branch_count"] = len(names)
                with open(mpath, "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=2, ensure_ascii=False)
        except (OSError, ValueError):
            pass

    # ---- commits.csv + churn ----------------------------------------------
    by_sha = {r["sha"]: r for r in results}

    def extras(sha):
        r = by_sha.get(sha, {})
        return (r.get("files", 0), r.get("bytes", 0), r.get("changed", 0),
                os.path.relpath(folder_for(sha), args.out), r.get("error", ""))

    write_commits_csv(args.out, commits, meta, refs_at, membership,
                      reachable_set, extras)

    churn_order = reachable_commits(repo, args.rev, args.since, args.until,
                                    args.limit, oldest_first=True)
    n_files, n_trees, n_hist = write_file_churn(
        args.out,
        with_progress(changes_from_folders(churn_order, folder_for),
                      Progress("churn", len(churn_order), not args.quiet)),
        meta,
        os.path.basename(repo), membership, args.branch_list)
    print("churn     %d file(s) -> file_churn.csv, %d tree(s) -> tree_churn.csv, "
          "%d change row(s) -> file_history.csv" % (n_files, n_trees, n_hist))

    total_bytes = sum(r.get("bytes", 0) for r in results)
    manifest = {
        "repo": repo, "git_dir": gdir, "generated": int(t0),
        "elapsed_seconds": round(time.time() - t0, 1),
        "content_mode": args.content, "layout": args.layout,
        "commits_total": len(commits),
        "commits_reachable": len(reachable_set),
        "commits_dangling": len(dangling),
        "refs_live": len(live), "refs_recovered_from_reflog": len(recovered),
        "refs_inferred_from_prose": len(inferred),
        "errors": errors,
        "tracked_bytes_at_head_of_each_commit": total_bytes,
        "revs": args.rev or ["--all", "--reflog"],
    }
    with open(os.path.join(args.out, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    dt = time.time() - t0
    print("\ndone  %d commits -> %s" % (len(commits), args.out))
    print("      %d live refs, %d from orphaned reflogs, %d inferred names, "
          "%d error(s)" % (len(live), len(recovered), len(inferred), errors))
    print("      %.1fs" % dt)
    if errors:
        print("      see the error column in commits.csv")


if __name__ == "__main__":
    main()
