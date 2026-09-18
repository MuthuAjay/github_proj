#!/usr/bin/env python3
"""
inventory.py - Stage 0 structural inventory for git repositories.

Answers, without reading any file for secrets:
  - what files exist now, and what files have EVER existed (the two differ a lot)
  - what type each file is, how big it is (bytes / chars / lines / longest line)
  - how often each file changed, by how many authors, over what lifespan
  - how many branches exist, and which ones hold content found nowhere else

This is the cheap targeting pass. It does not classify anything as a secret.
It tells you where the expensive passes should look first.

Usage:
    inventory.py REPO [REPO ...]        scan one or more repos
    inventory.py --batch PARENT_DIR     scan every git repo directly under PARENT_DIR
    inventory.py REPO --out ./results   write JSON reports to a directory

Stdlib only. Requires the `git` CLI on PATH.
"""

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from datetime import date

# ---------------------------------------------------------------- file classes

CODE_EXT = {
    ".cs", ".vb", ".fs", ".java", ".kt", ".scala", ".py", ".rb", ".go", ".rs",
    ".js", ".jsx", ".ts", ".tsx", ".php", ".c", ".h", ".cpp", ".hpp", ".m",
    ".sql", ".sh", ".bash", ".ps1", ".psm1", ".bat", ".cmd",
    ".bicep", ".tf", ".csproj", ".vbproj", ".sqlproj", ".sln", ".gradle",
}
DATA_EXT = {
    ".json", ".yml", ".yaml", ".xml", ".toml", ".ini", ".conf", ".config",
    ".properties", ".env", ".bicepparam", ".tfvars", ".csv", ".tsv",
    ".editorconfig", ".gitignore", ".dockerignore", ".gitattributes", ".ssc",
}
DOC_EXT = {".md", ".txt", ".rst", ".adoc", ".pdf", ".docx", ".xlsx", ".pptx"}
BIN_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf",
    ".zip", ".gz", ".tar", ".rar", ".7z", ".dll", ".exe", ".so", ".dylib",
    ".pdb", ".nupkg", ".jar", ".war", ".bin", ".dat", ".mdf", ".ldf", ".bak",
}

# Filenames that are a finding on their own, before anyone reads them.
RISKY_NAME = re.compile(
    r"(^|/)("
    r"\.env(\..+)?|\.envrc|\.npmrc|\.pypirc|\.netrc|\.htpasswd|"
    r"id_rsa|id_dsa|id_ecdsa|id_ed25519|known_hosts|authorized_keys|"
    r"credentials?|secrets?|passwords?|apikeys?|"
    r".*\.publishsettings|.*servicedependencies\.local\.json|"
    r".*\.pem|.*\.key|.*\.pfx|.*\.p12|.*\.jks|.*\.keystore|.*\.ppk|"
    r".*\.asc|.*\.gpg|.*\.kdbx|.*\.ovpn|.*\.tfstate.*|.*\.tfvars|"
    r".*\.mdf|.*\.bak|.*\.dump|.*\.sql\.gz"
    r")$",
    re.IGNORECASE,
)

RE_RENAME_BRACE = re.compile(r"\{(.*?) => (.*?)\}")


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in CODE_EXT:
        return "code"
    if ext in DATA_EXT:
        return "config/data"
    if ext in DOC_EXT:
        return "doc"
    if ext in BIN_EXT:
        return "binary"
    if ext == "":
        return "no-ext"
    return "other"


# ------------------------------------------------------------------ git helpers

def git(repo, *args, check=False):
    """Run a git command, return stdout as text ('' on failure)."""
    p = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, errors="replace",
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}")
    return p.stdout


def git_stream(repo, *args):
    """Run a git command, yield stdout line by line without buffering it all."""
    p = subprocess.Popen(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, errors="replace", bufsize=1,
    )
    try:
        for line in p.stdout:
            yield line.rstrip("\n")
    finally:
        p.stdout.close()
        p.wait()


def is_repo(path):
    return os.path.isdir(os.path.join(path, ".git")) or \
        git(path, "rev-parse", "--is-inside-work-tree").strip() == "true"


def normalize_rename(path):
    """git --numstat writes renames as 'a/{b => c}/d' or 'old => new'."""
    m = RE_RENAME_BRACE.search(path)
    if m:
        return path.replace(m.group(0), m.group(2)).replace("//", "/")
    if " => " in path:
        return path.split(" => ")[-1]
    return path


# -------------------------------------------------------------------- branches

def collect_branches(repo, max_branches, default_branch,
                     compare_files=True, distance=False, progress=False):
    """Branch inventory, plus which branches carry paths found on no other branch."""
    branches = []
    fmt = "%(refname:short)%09%(objectname)%09%(committerdate:short)%09%(authorname)"
    for line in git_stream(repo, "for-each-ref", "--format=" + fmt,
                           "refs/heads", "refs/remotes"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, sha, cdate, author = parts[0], parts[1], parts[2], parts[3]
        if name.endswith("/HEAD"):
            continue
        branches.append({"name": name, "sha": sha, "date": cdate, "author": author})

    branches.sort(key=lambda b: b["date"], reverse=True)

    if not compare_files or not default_branch:
        return branches, {}

    # Every path the default branch has ever contained, not just its tip.
    base = set()
    for line in git_stream(repo, "log", default_branch, "--full-history",
                           "--diff-filter=A", "--name-only", "--pretty=format:"):
        line = line.strip()
        if line:
            base.add(normalize_rename(line))
    base.update(git_stream(repo, "ls-tree", "-r", "--name-only", default_branch))
    base.discard("")

    # Paths per branch, capped so a repo with thousands of refs stays quick.
    exclusive = collections.defaultdict(list)
    for i, b in enumerate(branches[:max_branches], 1):
        if progress and i % 50 == 0:
            print(f"    ... {i}/{min(len(branches), max_branches)} branches compared",
                  file=sys.stderr)
        if b["name"] == default_branch:
            b["files"] = len(base)
            b["files_never_on_default"] = 0
            continue
        files = set(git_stream(repo, "ls-tree", "-r", "--name-only", b["sha"]))
        files.discard("")
        only = files - base
        b["files"] = len(files)
        b["files_never_on_default"] = len(only)
        for f in only:
            exclusive[f].append(b["name"])
        if distance:
            ab = git(repo, "rev-list", "--left-right", "--count",
                     f"{default_branch}...{b['sha']}").split()
            if len(ab) == 2:
                b["behind_default"], b["ahead_of_default"] = int(ab[0]), int(ab[1])

    return branches, exclusive


def detect_default_branch(repo):
    for cand in ("origin/HEAD", "HEAD"):
        out = git(repo, "symbolic-ref", "--quiet", "--short", cand).strip()
        if out:
            return out
    for cand in ("master", "main", "origin/master", "origin/main"):
        if git(repo, "rev-parse", "--verify", "--quiet", cand).strip():
            return cand
    return ""


# ----------------------------------------------------------------- path + churn

def collect_paths_and_churn(repo):
    """
    Single pass over `git log --all --numstat` gives us, per path:
      commits, authors, lines added/deleted, first/last date,
      and the most recent commit that touched it (used to fetch gone content).
    """
    rec = collections.defaultdict(lambda: {
        "commits": 0, "added": 0, "deleted": 0, "binary_edits": 0,
        "authors": set(), "dates": [], "last_commit": None,
    })
    sha = author = cdate = None
    for line in git_stream(
        repo, "log", "--all", "--full-history", "--numstat",
        "--pretty=format:\x01%H\x02%ae\x02%ad", "--date=short",
    ):
        if line.startswith("\x01"):
            parts = line[1:].split("\x02")
            sha = parts[0]
            author = parts[1] if len(parts) > 1 else ""
            cdate = parts[2] if len(parts) > 2 else ""
            continue
        if not line.strip() or sha is None:
            continue
        cols = line.split("\t")
        if len(cols) != 3:
            continue
        add, dele, path = cols
        path = normalize_rename(path)
        r = rec[path]
        r["commits"] += 1
        r["authors"].add(author)
        r["dates"].append(cdate)
        if r["last_commit"] is None:      # git log is newest-first
            r["last_commit"] = sha
        if add == "-" or dele == "-":
            r["binary_edits"] += 1
        else:
            r["added"] += int(add or 0)
            r["deleted"] += int(dele or 0)
    return rec


def collect_head_paths(repo):
    head = set(git_stream(repo, "ls-files"))
    head.discard("")
    return head


# ------------------------------------------------------------ content measuring

def measure_head_file(abspath):
    try:
        with open(abspath, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    return measure_blob(blob)


def measure_blob(blob):
    binary = b"\x00" in blob[:8192]
    out = {"bytes": len(blob), "binary": binary}
    if binary:
        out.update({"chars": None, "lines": None, "max_line": None, "avg_line": None})
        return out
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines() or [""]
    out["chars"] = len(text)
    out["lines"] = len(lines)
    out["max_line"] = max(len(x) for x in lines)
    out["avg_line"] = round(sum(len(x) for x in lines) / len(lines), 1)
    return out


def _read_exactly(stream, n):
    """Pipes give short reads. Loop until we have all n bytes or EOF."""
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def measure_gone_files(repo, wanted, progress=False):
    """
    Fetch the last known content of deleted paths through a single
    `git cat-file --batch` process, one request/response pair at a time.
    """
    results = {}
    if not wanted:
        return results
    proc = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        for i, (path, sha) in enumerate(wanted, 1):
            if not sha:
                continue
            if progress and i % 200 == 0:
                print(f"    ... {i}/{len(wanted)} deleted files measured",
                      file=sys.stderr)
            blob_found = False
            for spec in (f"{sha}:{path}", f"{sha}^:{path}"):
                try:
                    proc.stdin.write(spec.encode("utf-8", "surrogateescape") + b"\n")
                    proc.stdin.flush()
                except (BrokenPipeError, UnicodeEncodeError):
                    break
                header = proc.stdout.readline().decode("utf-8", "replace").strip()
                if not header:
                    break
                bits = header.split()
                # "<sha> <type> <size>" on success; "<query> missing" otherwise.
                # A path can resolve to a tree (a directory). git still emits the
                # payload, so it MUST be drained whatever the type is - skipping
                # it desyncs the stream and every later answer shifts by one.
                if len(bits) < 3 or header.endswith("missing"):
                    continue
                try:
                    size = int(bits[-1])
                except ValueError:
                    continue
                payload = _read_exactly(proc.stdout, size)
                proc.stdout.read(1)      # trailing newline
                if bits[-2] == "blob" and len(payload) == size:
                    blob = payload
                    blob_found = True
                    break
            if not blob_found:
                continue
            results[path] = measure_blob(blob)
    finally:
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        proc.wait()
    return results


# --------------------------------------------------------------------- assembly

def build_records(repo, churn, head_paths, exclusive, measure=True,
                  progress=False):
    records = []
    gone_wanted = []
    for path, r in churn.items():
        dates = sorted(r["dates"]) or [""]
        alive = path in head_paths
        add, dele = r["added"], r["deleted"]
        biggest = max(add, dele)
        symmetry = round(abs(add - dele) / biggest, 4) if biggest else 0.0
        rec = {
            "path": path,
            "name": os.path.basename(path),
            "dir": os.path.dirname(path) or ".",
            "ext": os.path.splitext(path)[1].lower(),
            "class": classify(path),
            "alive": alive,
            "created": dates[0],
            "last_touched": dates[-1],
            "lifespan_days": days_between(dates[0], dates[-1]),
            "commits": r["commits"],
            "authors": len(r["authors"]),
            "lines_added": add,
            "lines_deleted": dele,
            "churn": add + dele,
            "binary_edits": r["binary_edits"],
            "symmetric_delete": bool(not alive and biggest > 0 and symmetry < 0.02),
            "symmetry": symmetry,
            "risky_name": bool(RISKY_NAME.search(path)),
            "branches_exclusive": exclusive.get(path, [])[:5],
            "bytes": None, "chars": None, "lines": None,
            "max_line": None, "avg_line": None, "binary": None,
        }
        records.append(rec)
        if measure:
            if alive:
                m = measure_head_file(os.path.join(repo, path))
                if m:
                    rec.update(m)
            else:
                gone_wanted.append((path, r["last_commit"]))

    if measure and gone_wanted:
        by_path = {r["path"]: r for r in records}
        for path, m in measure_gone_files(repo, gone_wanted, progress).items():
            if path in by_path:
                by_path[path].update(m)

    # Files present on disk but never in history (untracked / ignored leftovers).
    known = {r["path"] for r in records}
    for path in sorted(head_paths - known):
        rec = {
            "path": path, "name": os.path.basename(path),
            "dir": os.path.dirname(path) or ".",
            "ext": os.path.splitext(path)[1].lower(),
            "class": classify(path), "alive": True,
            "created": "", "last_touched": "", "lifespan_days": None,
            "commits": 0, "authors": 0, "lines_added": 0, "lines_deleted": 0,
            "churn": 0, "binary_edits": 0, "symmetric_delete": False,
            "symmetry": 0.0, "risky_name": bool(RISKY_NAME.search(path)),
            "branches_exclusive": [],
        }
        if measure:
            m = measure_head_file(os.path.join(repo, path))
            rec.update(m or {})
        records.append(rec)

    return records


def days_between(a, b):
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------- reports

def fmt_int(n):
    return "-" if n is None else f"{n:,}"


def print_report(repo, data, top):
    recs = data["files"]
    st = data["stats"]
    name = os.path.basename(os.path.abspath(repo))
    bar = "=" * 78

    print(f"\n{bar}\n  {name}\n{bar}")
    print(f"  paths ever      {st['paths_ever']:>8}")
    print(f"  paths at HEAD   {st['paths_head']:>8}")
    print(f"  gone from HEAD  {st['paths_gone']:>8}  ({st['gone_pct']}% of history)")
    print(f"  commits         {st['commits']:>8}")
    print(f"  branches        {st['branches']:>8}")
    print(f"  authors         {st['authors']:>8}")
    print(f"  bytes at HEAD   {fmt_int(st['bytes_head']):>8}")
    print(f"  chars at HEAD   {fmt_int(st['chars_head']):>8}")
    print(f"  lines at HEAD   {fmt_int(st['lines_head']):>8}")

    print(f"\n  FILE CLASSES (full history)")
    print(f"  {'class':13}{'files':>7}{'gone':>7}{'mort%':>7}"
          f"{'commits':>9}{'chars':>12}{'lines':>9}")
    print("  " + "-" * 66)
    for cls, c in sorted(data["classes"].items(), key=lambda kv: -kv[1]["files"]):
        print(f"  {cls:13}{c['files']:>7}{c['gone']:>7}{c['mortality']:>6}%"
              f"{c['commits']:>9}{fmt_int(c['chars']):>12}{fmt_int(c['lines']):>9}")

    print(f"\n  EXTENSIONS (top 12, history vs HEAD)")
    print(f"  {'ext':10}{'ever':>7}{'head':>7}{'gone':>7}{'chars':>12}")
    print("  " + "-" * 43)
    for ext, c in data["extensions"][:12]:
        print(f"  {ext or '(none)':10}{c['ever']:>7}{c['head']:>7}"
              f"{c['gone']:>7}{fmt_int(c['chars']):>12}")

    print(f"\n  DIRECTORIES (top {top} by files gone)")
    print(f"  {'dir':46}{'ever':>6}{'head':>6}{'gone':>6}")
    print("  " + "-" * 64)
    for d in data["directories"][:top]:
        flag = "  <-- FULLY REMOVED" if d["head"] == 0 else ""
        print(f"  {d['dir'][:46]:46}{d['ever']:>6}{d['head']:>6}{d['gone']:>6}{flag}")

    print(f"\n  BRANCHES ({st['branches']} total, top {top} by unmerged files)")
    print(f"  {'branch':46}{'date':>12}{'never-on-def':>13}")
    print("  " + "-" * 69)
    for b in data["branches_ranked"][:top]:
        print(f"  {b['name'][:46]:46}{b['date']:>12}"
              f"{b.get('files_never_on_default', 0):>13}")

    print(f"\n  HIGH-INTEREST FILES (top {top})")
    print(f"  {'created':11}{'last':11}{'cmt':>5}{'auth':>5}{'chars':>14}"
          f"{'lines':>9}{'maxln':>7}  {'flags':7} path")
    print("  " + "-" * 104)
    for r in data["ranked"][:top]:
        flags = "".join([
            "R" if r["risky_name"] else ".",
            "S" if r["symmetric_delete"] else ".",
            "D" if not r["alive"] else ".",
            "B" if r.get("binary") else ".",
            "X" if r["branches_exclusive"] else ".",
        ])
        print(f"  {r['created'] or '-':11}{r['last_touched'] or '-':11}"
              f"{r['commits']:>5}{r['authors']:>5}{fmt_int(r.get('chars')):>14}"
              f"{fmt_int(r.get('lines')):>9}{fmt_int(r.get('max_line')):>7}"
              f"  {flags:7} {r['path'][:58]}")
    print("\n  flags: R=risky name  S=symmetric add/delete  D=deleted from HEAD"
          "  B=binary  X=on unmerged branch only")


def interest_score(r):
    """Targeting heuristic. Advisory only - this never says 'secret'."""
    s = 0
    if r["risky_name"]:
        s += 50
    if r["symmetric_delete"]:
        s += 25
    if not r["alive"]:
        s += 15
    if r["class"] in ("config/data", "doc"):
        s += 10
    if r["branches_exclusive"]:
        s += 15
    if r["authors"] >= 10:
        s += 10
    if r["commits"] >= 15 and r["class"] != "code":
        s += 10
    if r["lifespan_days"] is not None and r["lifespan_days"] < 90 and not r["alive"]:
        s += 10
    return s


def analyse(repo, args):
    default_branch = detect_default_branch(repo)
    prog = not args.quiet
    if prog:
        print(f"  scanning {repo} ...", file=sys.stderr)
    branches, exclusive = collect_branches(
        repo, args.max_branches, default_branch,
        not args.no_branch_files, args.branch_distance, prog,
    )
    if prog:
        print(f"    {len(branches)} branches; reading history ...", file=sys.stderr)
    churn = collect_paths_and_churn(repo)
    head_paths = collect_head_paths(repo)
    if prog:
        print(f"    {len(churn)} historical paths; measuring content ...",
              file=sys.stderr)
    records = build_records(repo, churn, head_paths, exclusive,
                            not args.no_content, prog)

    for r in records:
        r["interest"] = interest_score(r)

    # aggregates
    classes = collections.defaultdict(
        lambda: {"files": 0, "gone": 0, "commits": 0, "chars": 0, "lines": 0})
    exts = collections.defaultdict(
        lambda: {"ever": 0, "head": 0, "gone": 0, "chars": 0})
    dirs = collections.defaultdict(lambda: {"ever": 0, "head": 0})
    authors = set()
    bytes_head = chars_head = lines_head = 0

    for r in records:
        c = classes[r["class"]]
        c["files"] += 1
        c["commits"] += r["commits"]
        c["chars"] += r.get("chars") or 0
        c["lines"] += r.get("lines") or 0
        if not r["alive"]:
            c["gone"] += 1
        e = exts[r["ext"]]
        e["ever"] += 1
        e["chars"] += r.get("chars") or 0
        e["head" if r["alive"] else "gone"] += 1
        d = dirs[r["dir"].split("/")[0] if r["dir"] != "." else "."]
        d["ever"] += 1
        if r["alive"]:
            d["head"] += 1
            bytes_head += r.get("bytes") or 0
            chars_head += r.get("chars") or 0
            lines_head += r.get("lines") or 0

    for c in classes.values():
        c["mortality"] = round(100 * c["gone"] / max(1, c["files"]))

    for b in branches:
        authors.add(b["author"])
    commits = len(git(repo, "rev-list", "--all").split())

    gone = sum(1 for r in records if not r["alive"])
    stats = {
        "paths_ever": len(records),
        "paths_head": sum(1 for r in records if r["alive"]),
        "paths_gone": gone,
        "gone_pct": round(100 * gone / max(1, len(records))),
        "commits": commits,
        "branches": len(branches),
        "authors": len({a for r in churn.values() for a in r["authors"]}),
        "bytes_head": bytes_head,
        "chars_head": chars_head,
        "lines_head": lines_head,
        "default_branch": default_branch,
    }

    dir_list = [{"dir": k, **v, "gone": v["ever"] - v["head"]}
                for k, v in dirs.items()]
    dir_list.sort(key=lambda d: (-d["gone"], -d["ever"]))

    return {
        "repo": os.path.abspath(repo),
        "stats": stats,
        "classes": dict(classes),
        "extensions": sorted(exts.items(), key=lambda kv: -kv[1]["ever"]),
        "directories": dir_list,
        "branches": branches,
        "branches_ranked": sorted(
            branches, key=lambda b: -b.get("files_never_on_default", 0)),
        "files": records,
        "ranked": sorted(records, key=lambda r: (-r["interest"], -r["churn"])),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="*", help="repository paths")
    ap.add_argument("--batch", metavar="DIR",
                    help="scan every git repo directly under DIR")
    ap.add_argument("--out", metavar="DIR", help="write <repo>.inventory.json here")
    ap.add_argument("--top", type=int, default=20, help="rows per table (default 20)")
    ap.add_argument("--max-branches", type=int, default=300,
                    help="branches to diff against default (default 300)")
    ap.add_argument("--no-branch-files", action="store_true",
                    help="skip per-branch file comparison (much faster)")
    ap.add_argument("--branch-distance", action="store_true",
                    help="also compute ahead/behind per branch (one extra "
                         "rev-list per branch - slow on big repos)")
    ap.add_argument("--no-content", action="store_true",
                    help="skip byte/char/line measurement (fastest)")
    ap.add_argument("--quiet", action="store_true", help="JSON only, no tables")
    args = ap.parse_args()

    targets = list(args.repos)
    if args.batch:
        for entry in sorted(os.listdir(args.batch)):
            p = os.path.join(args.batch, entry)
            if os.path.isdir(p) and is_repo(p):
                targets.append(p)
    if not targets:
        ap.error("no repositories given (pass paths or --batch DIR)")

    if args.out:
        os.makedirs(args.out, exist_ok=True)

    summary, failures = [], []
    for repo in targets:
        if not is_repo(repo):
            print(f"skip (not a git repo): {repo}", file=sys.stderr)
            continue
        name = os.path.basename(os.path.abspath(repo))
        try:
            data = analyse(repo, args)
        except Exception as exc:                      # one bad repo must not
            failures.append((name, repr(exc)))        # abort the whole batch
            print(f"  FAILED {name}: {exc!r}", file=sys.stderr)
            continue
        if not args.quiet:
            print_report(repo, data, args.top)
            sys.stdout.flush()
        if args.out:
            with open(os.path.join(args.out, f"{name}.inventory.json"), "w") as fh:
                json.dump(data, fh, indent=1, default=str)
        summary.append((name, data["stats"]))

    if failures:
        print(f"\n  {len(failures)} repo(s) failed:", file=sys.stderr)
        for name, err in failures:
            print(f"    {name}: {err}", file=sys.stderr)
    if len(summary) > 1 and not args.quiet:
        print("\n" + "=" * 78 + "\n  ESTATE SUMMARY\n" + "=" * 78)
        print(f"  {'repo':32}{'ever':>7}{'head':>7}{'gone%':>7}"
              f"{'commits':>9}{'branch':>8}")
        print("  " + "-" * 70)
        for name, s in summary:
            print(f"  {name[:32]:32}{s['paths_ever']:>7}{s['paths_head']:>7}"
                  f"{s['gone_pct']:>6}%{s['commits']:>9}{s['branches']:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
