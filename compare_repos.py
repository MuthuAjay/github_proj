#!/usr/bin/env python3
"""
compare_repos.py - blob-level diff between two git repositories.

Answers one question: are these two repos the same content, and if not,
exactly where do they differ?

Comparison is by blob SHA-1, not by reading files. Two files are "the same"
only if git hashed them to the same object id, which means byte-identical
content. No diffing, no heuristics, no false equivalence.

Two lenses:

  tree     (default) compares the checked-out trees of one ref in each repo.
           Tells you whether the working content matches today.

  history  compares every blob ever reachable from any ref in each repo.
           Tells you whether one repo carries content the other never had -
           including files deleted from both checkouts. This is the lens that
           matters when the question is "did this fork keep something the
           original scrubbed?".

  objects  compares every TREE (directory snapshot) and every COMMIT object
           physically present in each repo's store, by their own SHA. Tells
           you which exact snapshots and which exact revisions exist in one
           repo but not the other - the structural analogue of `history`,
           one level up from individual files.

Usage:
    compare_repos.py REPO_A REPO_B
    compare_repos.py REPO_A REPO_B --mode history
    compare_repos.py REPO_A REPO_B --mode objects
    compare_repos.py A B --ref-a main --ref-b master
    compare_repos.py A B --json out.json --csv delta.csv
    compare_repos.py A B --mode both --show 100

Stdlib only. Requires the `git` CLI on PATH.
"""

import argparse
import csv
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inventory import (  # noqa: E402
    classify, RISKY_NAME, git, git_stream, is_repo, detect_default_branch,
)


def fmt_int(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


def fmt_bytes(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def ell(s, n):
    """Truncate from the LEFT - the tail of a path is what distinguishes it."""
    return s if len(s) <= n else "..." + s[-(n - 3):]


def risky(path):
    return bool(RISKY_NAME.search("/" + path.lower()))


# ------------------------------------------------------------------- listings

def tree_blobs(repo, ref):
    """path -> (blob_sha, mode) for every file in one ref's tree."""
    out = {}
    p = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-r", "-z", ref],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    for rec in p.stdout.split(b"\0"):
        if not rec:
            continue
        try:
            meta, path = rec.split(b"\t", 1)
            mode, otype, sha = meta.split()
        except ValueError:
            continue
        if otype != b"blob":
            continue          # submodules (commit) and nested trees
        out[path.decode("utf-8", "replace")] = (
            sha.decode(), mode.decode(),
        )
    return out


def all_objects_meta(repo):
    """sha -> (type, size) for every object physically in the store - packed
    or loose, reachable from a ref or not. This is the one pass that can see
    dangling/orphaned commits: cat-file --batch-all-objects walks the object
    database directly, ignoring refs entirely."""
    meta = {}
    p = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch-all-objects",
         "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    try:
        for line in p.stdout:
            bits = line.split()
            if len(bits) == 3:
                try:
                    meta[bits[0]] = (bits[1], int(bits[2]))
                except ValueError:
                    continue
    finally:
        p.stdout.close()
        p.wait()
    return meta


def all_blob_meta(repo):
    """sha -> size, for every object in the store that is a blob."""
    return {sha: size for sha, (t, size) in all_objects_meta(repo).items()
            if t == "blob"}


def find_pack_files(repo):
    """Locate every *.pack file backing this repo, working-tree or bare."""
    candidates = []
    if is_repo(repo):
        git_dir = git(repo, "rev-parse", "--git-dir").strip()
        if git_dir:
            if not os.path.isabs(git_dir):
                git_dir = os.path.join(repo, git_dir)
            candidates.append(os.path.join(git_dir, "objects", "pack"))
    candidates += [os.path.join(repo, ".git", "objects", "pack"),
                   os.path.join(repo, "objects", "pack")]
    seen_dirs, packs = set(), []
    for d in candidates:
        d = os.path.abspath(d)
        if d in seen_dirs or not os.path.isdir(d):
            continue
        seen_dirs.add(d)
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".pack"):
                packs.append(os.path.join(d, fn))
    return packs


def verify_pack_objects(pack_path):
    """
    sha -> (type, size) read straight from a pack's own index.

    Deliberately run with no -C and no repo context: verify-pack only needs
    the .pack and its .idx next to it, so this still works when the rest of
    .git is missing or damaged and cat-file/rev-list refuse to run at all.
    """
    out = {}
    p = subprocess.run(
        ["git", "verify-pack", "-v", pack_path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    for line in p.stdout.splitlines():
        bits = line.split()
        if len(bits) >= 3 and len(bits[0]) == 40 and \
                bits[1] in ("commit", "tree", "blob", "tag"):
            try:
                out[bits[0]] = (bits[1], int(bits[2]))
            except ValueError:
                continue
    return out


DIR_IGNORE = {".git", ".hg", ".svn"}


def dir_tree_and_sizes(root):
    """
    tree_blobs()+all_blob_meta() equivalent for a plain directory that has no
    .git of its own (e.g. a zip/tarball extracted to disk).

    git hash-object needs no repository to produce a blob sha - it just
    applies git's "blob <len>\\0<content>" framing and sha1s it - so a file
    here hashes identically to the same bytes committed anywhere else. Run
    with cwd=root and relative paths so Windows path quoting/MSYS translation
    can't corrupt an absolute path fed over stdin.
    """
    native_rels = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DIR_IGNORE]
        for fn in filenames:
            native_rels.append(
                os.path.relpath(os.path.join(dirpath, fn), root))
    tree, sizes = {}, {}
    if not native_rels:
        return tree, sizes
    p = subprocess.run(
        ["git", "hash-object", "--stdin-paths"],
        input="\n".join(native_rels) + "\n", cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    shas = p.stdout.split()
    for native_rel, sha in zip(native_rels, shas):
        rel = native_rel.replace(os.sep, "/")
        tree[rel] = (sha, "100644")
        try:
            sizes[sha] = os.path.getsize(os.path.join(root, native_rel))
        except OSError:
            pass
    return tree, sizes


EMPTY_HISTORY_STATS = {"commits_total": 0, "commits_dangling": 0,
                       "pack_files": 0, "pack_only_blobs": 0}


def dir_history(root):
    """history_blobs() equivalent for a plain directory: one snapshot, no past."""
    tree, sizes = dir_tree_and_sizes(root)
    names = {}
    for path, (sha, _mode) in tree.items():
        names.setdefault(sha, set()).add(path)
    return names, sizes, dict(EMPTY_HISTORY_STATS)


def walk_object_store(repo, progress=False):
    """
    names: sha -> set of paths, for every object reachable from any ref OR
           from any commit/tag object physically present in the store
           (packed or loose). Blobs and trees get real paths out of
           `rev-list --objects` (a tree's "path" is the directory it sits
           at); commits and tags get an empty set - rev-list has no path
           for those.
    sizes: sha -> size, for every object regardless of type.
    types: sha -> "blob" | "tree" | "commit" | "tag".
    stats: see EMPTY_HISTORY_STATS.

    `rev-list --all --objects` only follows refs. A repo whose refs were
    wiped, never written, or partially copied - but whose pack files still
    hold real commits - reports zero history under --all even though the
    bytes are sitting right there in .git/objects/pack. Seeding the walk
    from every commit/tag cat-file can see (not just ref tips) recovers
    that content without touching the packs by hand. If even that plumbing
    finds no commits at all, .pack files are read directly via their own
    index as a last resort - that still can't recover path names, but it
    proves whether the bytes exist.
    """
    if progress:
        print(f"    reading object store of {os.path.basename(repo)} ...",
              file=sys.stderr)
    meta = all_objects_meta(repo)
    types = {sha: t for sha, (t, _size) in meta.items()}
    sizes = {sha: size for sha, (_t, size) in meta.items()}
    commit_ish = [sha for sha, t in types.items() if t in ("commit", "tag")]

    names = {sha: set() for sha in meta}
    stats = dict(EMPTY_HISTORY_STATS)
    stats["commits_total"] = len(commit_ish)

    if commit_ish:
        ref_reachable = set(git_stream(repo, "rev-list", "--all"))
        stats["commits_dangling"] = sum(
            1 for c in commit_ish if c not in ref_reachable)
        if progress and stats["commits_dangling"]:
            print(f"    {stats['commits_dangling']} of {len(commit_ish)} "
                  f"commit(s) aren't reachable from any ref - walking them "
                  f"anyway", file=sys.stderr)
        proc = subprocess.Popen(
            ["git", "-C", repo, "rev-list", "--objects", "--stdin"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        try:
            proc.stdin.write("\n".join(commit_ish) + "\n")
            proc.stdin.close()
            for line in proc.stdout:
                sha, _, path = line.rstrip("\n").partition(" ")
                if sha in names:
                    # A root tree's path is "" (it has no name of its own) -
                    # keep that visible as "." rather than dropping it, or
                    # every repo's root tree(s) show up as "<unnamed tree>".
                    names[sha].add(path or ".")
        finally:
            proc.stdout.close()
            proc.wait()

    packs = find_pack_files(repo)
    stats["pack_files"] = len(packs)
    if not commit_ish and packs:
        if progress:
            print(f"    git plumbing found no commits at all, but "
                  f"{len(packs)} pack file(s) exist on disk - reading their "
                  f"indexes directly", file=sys.stderr)
        for pack in packs:
            for sha, (t, size) in verify_pack_objects(pack).items():
                if sha not in types:
                    types[sha] = t
                    sizes[sha] = size
                    names[sha] = set()
                    if t == "blob":
                        stats["pack_only_blobs"] += 1

    return names, sizes, types, stats


def history_blobs(repo, progress=False):
    """sha -> set of paths, and sha -> size, for every blob in the store."""
    names, sizes, types, stats = walk_object_store(repo, progress)
    blob_shas = [s for s, t in types.items() if t == "blob"]
    blob_names = {s: names[s] for s in blob_shas}
    blob_sizes = {s: sizes[s] for s in blob_shas}
    return blob_names, blob_sizes, stats


def objects_walk(repo, progress=False):
    """
    Tree and commit objects out of the same walk `history_blobs` uses for
    blobs - one pass over the store, split by type.

    Returns (tree_names, tree_sizes, commit_names, commit_sizes, stats).
    """
    names, sizes, types, stats = walk_object_store(repo, progress)

    def subset(kind):
        keys = [s for s, t in types.items() if t == kind]
        return ({s: names[s] for s in keys}, {s: sizes[s] for s in keys})

    tree_names, tree_sizes = subset("tree")
    commit_names, commit_sizes = subset("commit")
    return tree_names, tree_sizes, commit_names, commit_sizes, stats


def commit_labels(repo, shas):
    """
    sha -> "YYYY-MM-DD author: subject" for a specific handful of commits.

    Only ever called on the delta (only_in_a / only_in_b), not the whole
    history, so this stays a single small batched `git log` call even on a
    repo with a huge commit count. Works on dangling/unreachable commits
    too - `git log <sha>` needs the object in the store, not a ref onto it.
    """
    if not shas:
        return {}
    out = {}
    proc = subprocess.Popen(
        ["git", "-C", repo, "log", "--no-walk", "--date=short",
         "--format=%H%x09%ad%x09%an%x09%s", "--stdin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    try:
        proc.stdin.write("\n".join(shas) + "\n")
        proc.stdin.close()
        for line in proc.stdout:
            bits = line.rstrip("\n").split("\t", 3)
            if len(bits) == 4:
                out[bits[0]] = f"{bits[1]} {bits[2]}: {bits[3]}"
    finally:
        proc.stdout.close()
        proc.wait()
    return out


# ------------------------------------------------------------------- identity

def repo_identity(repo, ref):
    head = git(repo, "rev-parse", "--verify", "--quiet", ref).strip()
    roots = sorted(git(repo, "rev-list", "--max-parents=0", "--all").split())
    commits = git(repo, "rev-list", "--all", "--count").strip()
    branches = len([b for b in git_stream(
        repo, "for-each-ref", "--format=%(refname)",
        "refs/heads", "refs/remotes") if b])
    return {
        "path": os.path.abspath(repo),
        "name": os.path.basename(os.path.abspath(repo).rstrip("/")),
        "ref": ref,
        "ref_commit": head,
        "commits": int(commits) if commits.isdigit() else 0,
        "branches": branches,
        "root_commits": roots,
        "is_git": True,
    }


def dir_identity(path):
    """repo_identity() equivalent for a plain directory with no .git."""
    return {
        "path": os.path.abspath(path),
        "name": os.path.basename(os.path.abspath(path).rstrip(os.sep).rstrip("/")),
        "ref": "<extracted, no .git>",
        "ref_commit": "",
        "commits": 0,
        "branches": 0,
        "root_commits": [],
        "is_git": False,
    }


def shared_ancestry(ida, idb):
    ra, rb = set(ida["root_commits"]), set(idb["root_commits"])
    if not ra or not rb:
        return "unknown"
    if ra == rb:
        return "same"
    if ra & rb:
        return "partial"
    return "none"


# ---------------------------------------------------------------- comparisons

def compare_trees(a, b, ta, tb, sizes_a, sizes_b):
    """Path-keyed comparison of two ref trees."""
    pa, pb = set(ta), set(tb)
    same, differs = [], []
    for path in pa & pb:
        sa, sb = ta[path][0], tb[path][0]
        (same if sa == sb else differs).append(path)

    only_a = sorted(pa - pb)
    only_b = sorted(pb - pa)

    # A file present under a different name is a move, not a deletion.
    sha_a = {}
    for p in only_a:
        sha_a.setdefault(ta[p][0], []).append(p)
    sha_b = {}
    for p in only_b:
        sha_b.setdefault(tb[p][0], []).append(p)
    moved = []
    for sha in set(sha_a) & set(sha_b):
        if sha == EMPTY_BLOB:
            continue          # every empty file hashes here; not a rename
        # Pair 1:1, sorted. A cross product would invent len(a)*len(b)
        # "renames" for a blob that simply appears several times.
        for pa_, pb_ in zip(sorted(sha_a[sha]), sorted(sha_b[sha])):
            moved.append({"sha": sha, "path_a": pa_, "path_b": pb_,
                          "bytes": sizes_a.get(sha)})
    moved_a = {m["path_a"] for m in moved}
    moved_b = {m["path_b"] for m in moved}

    rows = []
    for path in sorted(differs):
        rows.append({
            "status": "differs", "path": path,
            "sha_a": ta[path][0], "sha_b": tb[path][0],
            "bytes_a": sizes_a.get(ta[path][0]),
            "bytes_b": sizes_b.get(tb[path][0]),
            "class": classify(path), "risky_name": risky(path),
        })
    for path in only_a:
        if path in moved_a:
            continue
        rows.append({
            "status": "only_in_a", "path": path,
            "sha_a": ta[path][0], "sha_b": None,
            "bytes_a": sizes_a.get(ta[path][0]), "bytes_b": None,
            "class": classify(path), "risky_name": risky(path),
        })
    for path in only_b:
        if path in moved_b:
            continue
        rows.append({
            "status": "only_in_b", "path": path,
            "sha_a": None, "sha_b": tb[path][0],
            "bytes_a": None, "bytes_b": sizes_b.get(tb[path][0]),
            "class": classify(path), "risky_name": risky(path),
        })
    for m in moved:
        rows.append({
            "status": "moved", "path": m["path_a"], "path_b": m["path_b"],
            "sha_a": m["sha"], "sha_b": m["sha"],
            "bytes_a": m["bytes"], "bytes_b": m["bytes"],
            "class": classify(m["path_a"]), "risky_name": risky(m["path_a"]),
        })

    return {
        "files_a": len(ta), "files_b": len(tb),
        "identical": len(same), "differs": len(differs),
        "only_in_a": len(only_a) - len(moved_a),
        "only_in_b": len(only_b) - len(moved_b),
        "moved": len(moved),
        "rows": rows,
    }


def compare_object_set(repo_a, repo_b, na, sa, nb, sb, kind="blob"):
    """
    Content-keyed (by object SHA) comparison of two sha->paths/sizes maps.

    `kind` picks the vocabulary: "blob" (files, classified/risky-flagged by
    path as usual), "tree" (directory snapshots, path = where they sat -
    still worth risky-flagging, e.g. a tree named "secrets"), or "commit"
    (revisions, labeled by date/author/subject instead of a path, fetched
    for just the delta via commit_labels - classify/risky_name don't apply).
    """
    ka, kb = set(na), set(nb)
    shared = ka & kb
    only_a = ka - kb
    only_b = kb - ka

    labels = {}
    if kind == "commit":
        labels.update(commit_labels(repo_a, sorted(only_a)))
        labels.update(commit_labels(repo_b, sorted(only_b)))

    def rows_for(shas, names, sizes, status):
        out = []
        for sha in shas:
            if sha in labels:
                path, other = labels[sha], 0
            else:
                paths = sorted(names[sha])
                if paths:
                    path, other = paths[0], len(paths) - 1
                else:
                    path = sha[:12] if kind == "commit" else f"<unnamed {kind}>"
                    other = 0
            out.append({
                "status": status, "sha": sha, "path": path,
                "other_paths": other,
                "bytes": sizes.get(sha),
                "class": classify(path) if kind == "blob" else kind,
                "risky_name": risky(path) if kind in ("blob", "tree") else False,
            })
        return out

    rows = rows_for(only_a, na, sa, "only_in_a") + \
        rows_for(only_b, nb, sb, "only_in_b")
    rows.sort(key=lambda r: (not r["risky_name"], -(r["bytes"] or 0)))

    # One path can hold dozens of distinct blobs across its history. Rolling
    # up by path is what a reader actually wants: which FILES differ, not
    # which revisions of them. (For "commit" rows, each label is already
    # unique per commit, so this rollup is effectively a no-op passthrough.)
    agg = {}
    for r in rows:
        k = (r["status"], r["path"])
        entry = agg.setdefault(k, {"status": r["status"], "path": r["path"],
                                   "blobs": 0, "bytes": 0,
                                   "class": r["class"],
                                   "risky_name": r["risky_name"]})
        entry["blobs"] += 1
        entry["bytes"] += r["bytes"] or 0
    by_path = sorted(agg.values(),
                     key=lambda a: (not a["risky_name"], -a["bytes"]))

    return {
        f"{kind}s_a": len(ka), f"{kind}s_b": len(kb),
        "shared": len(shared),
        "only_in_a": len(only_a), "only_in_b": len(only_b),
        "bytes_only_a": sum(sa.get(s, 0) for s in only_a),
        "bytes_only_b": sum(sb.get(s, 0) for s in only_b),
        "by_path": by_path,
        "rows": rows,
    }


def compare_history(a, b, na, sa, nb, sb):
    """Content-keyed comparison of two whole object stores (blobs)."""
    return compare_object_set(a, b, na, sa, nb, sb, kind="blob")


# ------------------------------------------------------------------- verdicts

def tree_verdict(t):
    if t["differs"] == 0 and t["only_in_a"] == 0 and t["only_in_b"] == 0:
        return "IDENTICAL" if t["moved"] == 0 else "IDENTICAL CONTENT, PATHS MOVED"
    if t["differs"] == 0 and t["only_in_a"] == 0:
        return "A IS A SUBSET OF B"
    if t["differs"] == 0 and t["only_in_b"] == 0:
        return "B IS A SUBSET OF A"
    return "DIVERGED"


def history_verdict(h):
    if h["only_in_a"] == 0 and h["only_in_b"] == 0:
        return "IDENTICAL OBJECT STORES"
    if h["only_in_a"] == 0:
        return "A'S HISTORY IS CONTAINED IN B"
    if h["only_in_b"] == 0:
        return "B'S HISTORY IS CONTAINED IN A"
    if h["shared"] == 0:
        return "NO SHARED CONTENT AT ALL"
    return "BOTH HOLD CONTENT THE OTHER LACKS"


def trees_verdict(t):
    if t["only_in_a"] == 0 and t["only_in_b"] == 0:
        return "IDENTICAL TREES (every snapshot in A exists in B, and vice versa)"
    if t["only_in_a"] == 0:
        return "A'S TREES ARE ALL CONTAINED IN B"
    if t["only_in_b"] == 0:
        return "B'S TREES ARE ALL CONTAINED IN A"
    if t["shared"] == 0:
        return "NO SHARED TREES AT ALL"
    return "BOTH HOLD SNAPSHOTS THE OTHER NEVER HAD"


def commits_verdict(c):
    if c["only_in_a"] == 0 and c["only_in_b"] == 0:
        return "IDENTICAL COMMIT SETS"
    if c["only_in_a"] == 0:
        return "A'S COMMITS ARE ALL CONTAINED IN B"
    if c["only_in_b"] == 0:
        return "B'S COMMITS ARE ALL CONTAINED IN A"
    if c["shared"] == 0:
        return "NO SHARED COMMITS AT ALL"
    return "BOTH HOLD COMMITS THE OTHER LACKS"


# -------------------------------------------------------------------- reports

def rule(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def print_report(res, show):
    ida, idb = res["repo_a"], res["repo_b"]
    rule(f"{ida['name']}  vs  {idb['name']}")
    for label, ident in (("A", ida), ("B", idb)):
        print(f"  {label}  {ident['path']}")
        if ident["is_git"]:
            print(f"     ref {ident['ref']} @ {ident['ref_commit'][:12]}   "
                  f"{fmt_int(ident['commits'])} commits, {ident['branches']} refs")
        else:
            print("     no .git")
        if ident.get("scanned_raw") and res.get("mode") in ("tree", "both"):
            reason = "0 commits, unborn HEAD" if ident["is_git"] else "not a git repo"
            print(f"     -> tree comparison uses raw files on disk ({reason})")
    print(f"  shared ancestry (root commits): {res['ancestry']}")
    if ida["ref_commit"] and ida["ref_commit"] == idb["ref_commit"]:
        print("  refs point at the SAME commit - trees cannot differ")

    t = res.get("tree")
    if t:
        rule("TREE  (files in the compared ref)")
        print(f"  files in A            {fmt_int(t['files_a']):>10}")
        print(f"  files in B            {fmt_int(t['files_b']):>10}")
        print(f"  identical blob        {fmt_int(t['identical']):>10}")
        print(f"  same path, differs    {fmt_int(t['differs']):>10}")
        print(f"  only in A             {fmt_int(t['only_in_a']):>10}")
        print(f"  only in B             {fmt_int(t['only_in_b']):>10}")
        print(f"  moved (same blob)     {fmt_int(t['moved']):>10}")
        print(f"\n  VERDICT: {tree_verdict(t)}")
        deltas = [r for r in t["rows"] if r["status"] != "moved"]
        if deltas:
            print(f"\n  first {min(show, len(deltas))} of {fmt_int(len(deltas))} "
                  f"delta rows (risky names first):")
            deltas.sort(key=lambda r: (not r["risky_name"],
                                       -((r["bytes_a"] or 0) + (r["bytes_b"] or 0))))
            print(f"  {'status':<11} {'A bytes':>9} {'B bytes':>9}  path")
            print("  " + "-" * 74)
            for r in deltas[:show]:
                flag = "!" if r["risky_name"] else " "
                print(f" {flag}{r['status']:<11} {fmt_bytes(r['bytes_a']):>9} "
                      f"{fmt_bytes(r['bytes_b']):>9}  {ell(r['path'], 44)}")
        moves = [r for r in t["rows"] if r["status"] == "moved"]
        if moves:
            print(f"\n  first {min(show, len(moves))} of {fmt_int(len(moves))} "
                  f"moves (identical blob, different path):")
            for r in moves[:show]:
                print(f"    {ell(r['path'], 36):<36} ->  {ell(r['path_b'], 36)}")

    h = res.get("history")
    if h:
        rule("HISTORY  (every blob ever reachable from any ref)")
        raw = [n["name"] for n in (ida, idb) if n.get("scanned_raw_history")]
        if raw:
            print(f"  note: {', '.join(raw)} was scanned as raw files on disk "
                  f"- its side of this comparison is only today's on-disk "
                  f"snapshot, not everything it has ever contained")
        for label, name, st in (("A", ida["name"], h.get("stats_a")),
                                ("B", idb["name"], h.get("stats_b"))):
            if not st:
                continue
            if st["commits_dangling"]:
                print(f"  note: {name} ({label}) has {st['commits_dangling']} "
                      f"of {st['commits_total']} commit(s) not reachable from "
                      f"any ref - included anyway (dangling/orphaned history)")
            if st["pack_only_blobs"]:
                print(f"  note: {name} ({label}) - git plumbing found no "
                      f"commits, but {st['pack_files']} pack file(s) on disk "
                      f"held {st['pack_only_blobs']} blob(s), recovered by "
                      f"reading the pack index directly (no path names "
                      f"available for these)")
        print(f"  blobs in A            {fmt_int(h['blobs_a']):>10}")
        print(f"  blobs in B            {fmt_int(h['blobs_b']):>10}")
        print(f"  shared                {fmt_int(h['shared']):>10}")
        print(f"  only in A             {fmt_int(h['only_in_a']):>10}"
              f"   {fmt_bytes(h['bytes_only_a'])}")
        print(f"  only in B             {fmt_int(h['only_in_b']):>10}"
              f"   {fmt_bytes(h['bytes_only_b'])}")
        print(f"\n  VERDICT: {history_verdict(h)}")
        bp = h.get("by_path", [])
        if bp:
            print(f"\n  first {min(show, len(bp))} of {fmt_int(len(bp))} paths "
                  f"holding unique blobs ({fmt_int(len(h['rows']))} blobs "
                  f"total; risky names first, then largest):")
            print(f"  {'status':<11} {'revs':>5} {'bytes':>9}  path")
            print("  " + "-" * 74)
            for r in bp[:show]:
                flag = "!" if r["risky_name"] else " "
                print(f" {flag}{r['status']:<11} {r['blobs']:>5} "
                      f"{fmt_bytes(r['bytes']):>9}  {ell(r['path'], 44)}")

    tr = res.get("trees")
    if tr:
        rule("TREES  (every directory snapshot physically in the store, by tree SHA)")
        raw = [n["name"] for n in (ida, idb) if n.get("scanned_raw_history")]
        if raw:
            print(f"  note: {', '.join(raw)} was scanned as raw files on disk "
                  f"(no .git) - it has no tree or commit objects, so its side "
                  f"of this comparison is empty by definition")
        print(f"  trees in A            {fmt_int(tr['trees_a']):>10}")
        print(f"  trees in B            {fmt_int(tr['trees_b']):>10}")
        print(f"  shared                {fmt_int(tr['shared']):>10}")
        print(f"  only in A             {fmt_int(tr['only_in_a']):>10}")
        print(f"  only in B             {fmt_int(tr['only_in_b']):>10}")
        print(f"\n  VERDICT: {trees_verdict(tr)}")
        bp = tr.get("by_path", [])
        if bp:
            print(f"\n  first {min(show, len(bp))} of {fmt_int(len(bp))} "
                  f"tree(s) unique to one side (risky names first):")
            print(f"  {'status':<11} {'bytes':>9}  path")
            print("  " + "-" * 74)
            for r in bp[:show]:
                flag = "!" if r["risky_name"] else " "
                print(f" {flag}{r['status']:<11} {fmt_bytes(r['bytes']):>9}  "
                      f"{ell(r['path'], 44)}")

    cm = res.get("commits")
    if cm:
        rule("COMMITS  (every commit object physically in the store, by commit SHA)")
        print(f"  commits in A          {fmt_int(cm['commits_a']):>10}")
        print(f"  commits in B          {fmt_int(cm['commits_b']):>10}")
        print(f"  shared                {fmt_int(cm['shared']):>10}")
        print(f"  only in A             {fmt_int(cm['only_in_a']):>10}")
        print(f"  only in B             {fmt_int(cm['only_in_b']):>10}")
        print(f"\n  VERDICT: {commits_verdict(cm)}")
        rows = cm.get("rows", [])
        if rows:
            print(f"\n  first {min(show, len(rows))} of {fmt_int(len(rows))} "
                  f"commit(s) unique to one side:")
            for r in rows[:show]:
                print(f"    {r['status']:<11} {ell(r['path'], 70)}")
    print()


# ------------------------------------------------------------------- exports

CSV_COLS = ["lens", "status", "path", "path_b", "sha_a", "sha_b", "sha",
            "bytes_a", "bytes_b", "bytes", "blobs", "class", "risky_name"]


def write_csv(res, out):
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for lens in ("tree", "history", "trees", "commits"):
            block = res.get(lens)
            if not block:
                continue
            for r in block["rows"]:
                w.writerow({"lens": lens, **r})
            for r in block.get("by_path", []):
                w.writerow({"lens": lens + "_by_path", **r})
    print(f"  csv  -> {out}")


def write_json(res, out, rows):
    payload = json.loads(json.dumps(res))   # deep copy, all plain types already
    if not rows:
        for lens in ("tree", "history", "trees", "commits"):
            if payload.get(lens):
                payload[lens]["rows"] = []
                payload[lens].pop("by_path", None)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"  json -> {out}")


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Blob-level comparison of two git repositories.")
    ap.add_argument("repo_a")
    ap.add_argument("repo_b")
    ap.add_argument("--mode", choices=("tree", "history", "objects", "both"),
                    default="tree",
                    help="tree: compare one ref's files (default). "
                         "history: compare every blob ever reachable. "
                         "objects: compare every tree and every commit "
                         "object physically in the store, by SHA. "
                         "both: run tree + history.")
    ap.add_argument("--ref-a", help="ref to compare in A (default: its own default branch)")
    ap.add_argument("--ref-b", help="ref to compare in B (default: its own default branch)")
    ap.add_argument("--ref", help="use this ref in both repos")
    ap.add_argument("--show", type=int, default=40,
                    help="delta rows to print per lens (default 40)")
    ap.add_argument("--json", metavar="FILE", help="write the full result as JSON")
    ap.add_argument("--csv", metavar="FILE", help="write delta rows as CSV")
    ap.add_argument("--summary-json", action="store_true",
                    help="omit per-file rows from the JSON, keep the counts")
    ap.add_argument("--force-dir-a", action="store_true",
                    help="scan A's raw files on disk even if it has a .git "
                         "(ignores git tracking/history entirely for A)")
    ap.add_argument("--force-dir-b", action="store_true",
                    help="scan B's raw files on disk even if it has a .git "
                         "(ignores git tracking/history entirely for B)")
    ap.add_argument("--quiet", action="store_true", help="verdict lines only")
    args = ap.parse_args()

    for r in (args.repo_a, args.repo_b):
        if not os.path.isdir(r):
            print(f"not a directory: {r}", file=sys.stderr)
            return 2

    is_git_a, is_git_b = is_repo(args.repo_a), is_repo(args.repo_b)

    ref_a = (args.ref or args.ref_a or detect_default_branch(args.repo_a) or "HEAD") \
        if is_git_a else "<extracted, no .git>"
    ref_b = (args.ref or args.ref_b or detect_default_branch(args.repo_b) or "HEAD") \
        if is_git_b else "<extracted, no .git>"

    res = {
        "repo_a": repo_identity(args.repo_a, ref_a) if is_git_a else dir_identity(args.repo_a),
        "repo_b": repo_identity(args.repo_b, ref_b) if is_git_b else dir_identity(args.repo_b),
        "mode": args.mode,
    }
    res["ancestry"] = shared_ancestry(res["repo_a"], res["repo_b"])

    # A .git with zero commits (unborn HEAD) has an empty checked-out tree no
    # matter what - git has nothing to report even if the working directory
    # is full of real files. Comparing "are the files the same" in that case
    # means reading the files off disk directly, same as a non-git directory.
    # HISTORY mode is deliberately NOT included in this: history_blobs() can
    # recover content from dangling commits or straight from .pack files even
    # when refs/HEAD show zero commits, so forcing it into a raw disk scan
    # here would throw that recovery away before it gets a chance to run.
    use_dir_tree_a = args.force_dir_a or not is_git_a or res["repo_a"]["commits"] == 0
    use_dir_tree_b = args.force_dir_b or not is_git_b or res["repo_b"]["commits"] == 0
    use_dir_hist_a = args.force_dir_a or not is_git_a
    use_dir_hist_b = args.force_dir_b or not is_git_b
    res["repo_a"]["scanned_raw"] = use_dir_tree_a
    res["repo_b"]["scanned_raw"] = use_dir_tree_b

    if not args.quiet:
        for side, path, is_git, use_dir, forced in (
            ("A", args.repo_a, is_git_a, use_dir_tree_a, args.force_dir_a),
            ("B", args.repo_b, is_git_b, use_dir_tree_b, args.force_dir_b),
        ):
            if not use_dir:
                continue
            if forced:
                why = "--force-dir given"
            elif not is_git:
                why = "no .git"
            else:
                why = "git repo but 0 commits (unborn HEAD)"
            print(f"note: {path} scanned as raw files on disk ({why}) - "
                  f"git tracking ignored for the tree comparison (history "
                  f"still tries git's object store first)", file=sys.stderr)

    if args.mode in ("tree", "both"):
        if use_dir_tree_a:
            ta, sa = dir_tree_and_sizes(args.repo_a)
        else:
            ta, sa = tree_blobs(args.repo_a, ref_a), all_blob_meta(args.repo_a)
        if use_dir_tree_b:
            tb, sb = dir_tree_and_sizes(args.repo_b)
        else:
            tb, sb = tree_blobs(args.repo_b, ref_b), all_blob_meta(args.repo_b)
        res["tree"] = compare_trees(args.repo_a, args.repo_b, ta, tb, sa, sb)
        res["tree"]["verdict"] = tree_verdict(res["tree"])

    if args.mode in ("history", "both"):
        na, sa2, hstats_a = dir_history(args.repo_a) if use_dir_hist_a \
            else history_blobs(args.repo_a, progress=not args.quiet)
        nb, sb2, hstats_b = dir_history(args.repo_b) if use_dir_hist_b \
            else history_blobs(args.repo_b, progress=not args.quiet)
        res["history"] = compare_history(args.repo_a, args.repo_b,
                                         na, sa2, nb, sb2)
        res["history"]["verdict"] = history_verdict(res["history"])
        res["history"]["stats_a"] = hstats_a
        res["history"]["stats_b"] = hstats_b
        res["repo_a"]["scanned_raw_history"] = use_dir_hist_a
        res["repo_b"]["scanned_raw_history"] = use_dir_hist_b

    if args.mode == "objects":
        # A plain directory (or --force-dir side) has no tree/commit objects
        # at all - there's nothing to extract, so that side is just empty.
        empty_objects = ({}, {}, {}, {}, dict(EMPTY_HISTORY_STATS))
        nta, sta, nca, sca, ostats_a = empty_objects if use_dir_hist_a \
            else objects_walk(args.repo_a, progress=not args.quiet)
        ntb, stb, ncb, scb, ostats_b = empty_objects if use_dir_hist_b \
            else objects_walk(args.repo_b, progress=not args.quiet)

        res["trees"] = compare_object_set(args.repo_a, args.repo_b,
                                          nta, sta, ntb, stb, kind="tree")
        res["trees"]["verdict"] = trees_verdict(res["trees"])
        res["trees"]["stats_a"] = ostats_a
        res["trees"]["stats_b"] = ostats_b

        res["commits"] = compare_object_set(args.repo_a, args.repo_b,
                                            nca, sca, ncb, scb, kind="commit")
        res["commits"]["verdict"] = commits_verdict(res["commits"])
        res["commits"]["stats_a"] = ostats_a
        res["commits"]["stats_b"] = ostats_b

        res["repo_a"]["scanned_raw_history"] = use_dir_hist_a
        res["repo_b"]["scanned_raw_history"] = use_dir_hist_b

    if args.quiet:
        if res.get("tree"):
            print(f"tree:    {res['tree']['verdict']}")
        if res.get("history"):
            print(f"history: {res['history']['verdict']}")
        if res.get("trees"):
            print(f"trees:   {res['trees']['verdict']}")
        if res.get("commits"):
            print(f"commits: {res['commits']['verdict']}")
    else:
        print_report(res, args.show)

    if args.json:
        write_json(res, args.json, rows=not args.summary_json)
    if args.csv:
        write_csv(res, args.csv)

    same = all(
        res[l]["verdict"].startswith("IDENTICAL")
        for l in ("tree", "history", "trees", "commits") if res.get(l)
    )
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
