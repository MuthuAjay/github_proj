#!/usr/bin/env python3
"""
file_history_for_list.py - full commit history for every file in a big list.

Input CSV/TSV columns: org, repo, relpath, filename, sha256 (sha256 is carried
through to the output and never used for matching). Each row is looked up in
the repo at <repos-root>/<org>/<repo>.

The work is done once per repo, not once per file: the rows are grouped by
(org, repo), and each repo's history is read with two `git log` passes over
the object store (no checkout, no file content), then joined to the list.
Merges are diffed against their first parent and renames are followed (-M), so
a file that was renamed keeps the commits from before the rename.

Output (in --out): file_summary.csv, one row per input row - the same statistics
as file_churn.csv, looked up for each listed file:

    row org repo relpath filename sha256 matched_path status present_at_head
    commits_touched added modified deleted renamed first_seen last_changed
    branch_count

Optional extras:
  --details   also first_commit/first_author/first_subject, last_commit/
              last_author/last_subject and last_change_type
  --history   also write file_history.csv, one row per change to each matched
              file, oldest first (org repo path commit commit_date author subject
              status nth_change old_path old_blob new_blob); join it to the
              summary on org, repo and matched_path

status is one of:
  found               the path has history in the repo
  in_head_no_history  in the current tree but no commit reached it (shallow?)
  not_found           no history under that path. An old name that was renamed
                      away is still "found" (its history up to the rename, with
                      present_at_head = no); the new name's history includes it
  repo_missing        <repos-root>/<org>/<repo> is not a git repo
  repo_error          git failed on that repo (message in `error`)
  bad_row             org or repo blank, or no usable path

first_seen / first_commit is the earliest commit of the file by real (UTC) commit
time, and last_changed / last_commit the latest, so both are correct when the
history has parallel branches or mixed time zones. file_churn.csv orders by
git's graph order instead, so on files added on several branches its dates can
differ (yours is the earlier first_seen and the later last_changed). A file
that was renamed away on some other branch but still exists at HEAD keeps its
own history here; file_churn.csv can lose it.

The path is relpath when it already ends with the filename, otherwise
relpath/filename. Backslashes, a leading "./" or "/" and doubled slashes are
normalised; if the path is not found and starts with "<repo>/", the path
without that prefix is tried as well.

Usage:
    python file_history_for_list.py input.tsv --repos-root /data/workarea/archive \\
        --out out_dir --workers 8
    (re-run the same command with --resume after an interruption)
"""

import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext

from explore_input_csv import detect_delimiter, is_repo_dir
from extract_commits import (GIT, Progress, branch_membership,
                             changes_from_git, commit_metadata, live_refs)

SUMMARY_HEADER = ["row", "org", "repo", "relpath", "filename", "sha256",
                  "matched_path", "status", "present_at_head",
                  "last_change_type", "commits_touched", "added", "modified",
                  "deleted", "renamed", "first_seen", "last_changed",
                  "branch_count", "first_commit", "first_author",
                  "first_subject", "last_commit", "last_author",
                  "last_subject", "error"]
# what is written by default (file_churn's columns); --details adds the rest
CORE_COLUMNS = ["row", "org", "repo", "relpath", "filename", "sha256",
                "matched_path", "status", "present_at_head", "commits_touched",
                "added", "modified", "deleted", "renamed", "first_seen",
                "last_changed", "branch_count", "error"]
DETAIL_COLUMNS = ["last_change_type", "first_commit", "first_author",
                  "first_subject", "last_commit", "last_author", "last_subject"]
HISTORY_HEADER = ["org", "repo", "path", "commit", "commit_date", "author",
                  "subject", "status", "nth_change", "old_path", "old_blob",
                  "new_blob"]
DONE_FILE = ".done_repos"


def full_path(rel, fname):
    """The repo-relative, forward-slash path a row refers to."""
    rel = (rel or "").strip().replace("\\", "/")
    fname = (fname or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if rel == ".":
        rel = ""
    if fname and (rel == fname or rel.endswith("/" + fname)):
        p = rel
    elif rel and fname:
        p = rel.rstrip("/") + "/" + fname
    else:
        p = rel or fname
    p = p.strip("/")
    while "//" in p:
        p = p.replace("//", "/")
    return p


def candidates(path, repo):
    out = [path] if path else []
    if path.startswith(repo + "/"):
        out.append(path[len(repo) + 1:])
    return out


def head_paths(repo_path):
    """Set of paths in HEAD's tree, or None if HEAD can't be resolved."""
    proc = subprocess.run(GIT + ["-C", repo_path, "ls-tree", "-r", "-z",
                                 "--name-only", "HEAD"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return None
    return set(proc.stdout.decode("utf-8", "surrogateescape").split("\0")) - {""}


def utc(iso):
    """ISO-8601 commit date -> UTC timestamp (0 if unparsable). Comparing the
    strings would misorder dates written with different UTC offsets."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
            timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def commit_dates(repo_path, shas, chunk=4000):
    """{sha: {"committer_date": ...}} - just the dates, without reading full
    commit messages (all that the default output needs)."""
    out = {}
    for i in range(0, len(shas), chunk):
        proc = subprocess.run(
            GIT + ["-C", repo_path, "log", "--no-walk", "--stdin",
                   "--format=%H%x1f%cI"],
            input="\n".join(shas[i:i + chunk]).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError("git log failed: "
                               + proc.stderr.decode("utf-8", "replace")[:300])
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            sha, _, date = line.partition("\x1f")
            if sha:
                out[sha] = {"committer_date": date}
    return out


def new_rec():
    return {"n": 0, "added": 0, "modified": 0, "deleted": 0, "renamed": 0,
            "first_sha": "", "last_sha": "", "last_type": "", "hist": []}


def process_repo(root, org, repo, rows, want_history=False, details=False):
    """-> (summary_rows, history_rows) for one repo. `rows` is a list of
    (rowno, relpath, filename, sha256)."""
    rp = os.path.join(root, org, repo)
    prepared = []
    for rowno, rel, fname, sha in rows:
        p = full_path(rel, fname)
        prepared.append((rowno, rel, fname, sha, p, candidates(p, repo)))

    def blank_rows(status, err=""):
        out = []
        for rowno, rel, fname, sha, p, _c in prepared:
            r = [""] * len(SUMMARY_HEADER)
            r[:6] = [rowno, org, repo, rel, fname, sha]
            r[SUMMARY_HEADER.index("status")] = status if p else "bad_row"
            r[SUMMARY_HEADER.index("error")] = err
            out.append(r)
        return out, []

    if not is_repo_dir(rp):
        return blank_rows("repo_missing")
    try:
        wanted = {c for *_x, cands in prepared for c in cands}

        # pass 1: rename edges (new path -> old paths), so a wanted file's
        # earlier names are tracked in pass 2
        edges = defaultdict(set)
        for _sha, changes in changes_from_git(rp, [], None, None, None):
            for c in changes:
                if c["status"][:1] == "R" and c["old_path"]:
                    edges[c["path"]].add(c["old_path"])
        track, stack = set(wanted), list(wanted)
        while stack:
            for old in edges.get(stack.pop(), ()):
                if old not in track:
                    track.add(old)
                    stack.append(old)

        # pass 2: oldest first, keep only tracked paths; a rename moves the
        # old name's record onto the new name
        recs, needed = {}, set()
        for sha, changes in changes_from_git(rp, [], None, None, None):
            for c in changes:
                st, p, old = c["status"][:1], c["path"], c["old_path"]
                if p not in track and old not in track:
                    continue
                if st == "R" and old in recs:
                    if old in wanted:
                        # the old path may still exist elsewhere (renamed on
                        # another branch only): keep its own history too
                        moved = dict(recs[old], hist=list(recs[old]["hist"]))
                    else:
                        moved = recs.pop(old)
                    cur = recs.get(p)
                    if cur is not None:
                        for k in ("n", "added", "modified", "deleted", "renamed"):
                            moved[k] += cur[k]
                        moved["hist"] += cur["hist"]
                    recs[p] = moved
                rec = recs.setdefault(p, new_rec())
                rec["n"] += 1
                rec["renamed" if st == "R" else "added" if st == "A"
                    else "deleted" if st == "D" else "modified"] += 1
                rec["hist"].append(
                    (sha, c["status"], old, c["old_blob"], c["new_blob"], rec["n"])
                    if want_history else (sha, c["status"]))
                needed.add(sha)

        if not needed:
            meta = {}
        elif details or want_history:
            meta = commit_metadata(rp, sorted(needed))
        else:
            meta = commit_dates(rp, sorted(needed))
        member = (branch_membership(rp, live_refs(rp), needed)
                  if needed else {})
        head = head_paths(rp)
    except Exception as exc:                       # noqa: BLE001 - per-repo isolation
        return blank_rows("repo_error", "%s: %s" % (type(exc).__name__, str(exc)[:200]))

    def m(sha, key):
        return meta.get(sha, {}).get(key, "")

    # first / last change by commit time (UTC); ties fall back to git's order
    for rec in recs.values():
        keyed = [(utc(m(h[0], "committer_date")), i, h)
                 for i, h in enumerate(rec["hist"])]
        rec["first_sha"] = min(keyed)[2][0]
        last = max(keyed)[2]
        rec["last_sha"], rec["last_type"] = last[0], last[1]

    summary, history, emitted = [], [], set()
    for rowno, rel, fname, sha256, p, cands in prepared:
        r = [""] * len(SUMMARY_HEADER)
        r[:6] = [rowno, org, repo, rel, fname, sha256]
        if not p:
            r[SUMMARY_HEADER.index("status")] = "bad_row"
            summary.append(r)
            continue
        matched = next((c for c in cands if c in recs), None)
        if matched is None:
            matched = next((c for c in cands if head and c in head), cands[0])
        rec = recs.get(matched)
        in_head = head is not None and matched in head
        vals = {"matched_path": matched,
                "present_at_head": ("yes" if in_head else "no")
                if head is not None else ""}
        if rec:
            vals.update(status="found", last_change_type=rec["last_type"],
                        commits_touched=rec["n"], added=rec["added"],
                        modified=rec["modified"], deleted=rec["deleted"],
                        renamed=rec["renamed"],
                        first_seen=m(rec["first_sha"], "committer_date"),
                        last_changed=m(rec["last_sha"], "committer_date"),
                        branch_count=len(set().union(
                            *(member.get(h[0], ()) for h in rec["hist"]))),
                        first_commit=rec["first_sha"],
                        first_author=m(rec["first_sha"], "author_name"),
                        first_subject=(m(rec["first_sha"], "subject") or "")
                        .replace("\n", " "),
                        last_commit=rec["last_sha"],
                        last_author=m(rec["last_sha"], "author_name"),
                        last_subject=(m(rec["last_sha"], "subject") or "")
                        .replace("\n", " "))
            if want_history and (org, repo, matched) not in emitted:
                emitted.add((org, repo, matched))
                for sha, status, old, ob, nb, nth in rec["hist"]:
                    history.append([org, repo, matched, sha,
                                    m(sha, "committer_date"), m(sha, "author_name"),
                                    (m(sha, "subject") or "").replace("\n", " "),
                                    status, nth, old, ob, nb])
        else:
            vals["status"] = "in_head_no_history" if in_head else "not_found"
        for k, v in vals.items():
            r[SUMMARY_HEADER.index(k)] = v
        summary.append(r)
    return summary, history


def read_input(path, delimiter):
    """-> ({(org, repo): [(rowno, relpath, filename, sha256)]}, bad_rows,
    total_rows). rowno is the 1-based data-row number."""
    groups, bad, total = defaultdict(list), [], 0
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        header = [h.strip().lower() for h in next(reader)]
        need = ["org", "repo", "relpath", "filename"]
        missing = [c for c in need if c not in header]
        if missing:
            raise SystemExit("missing column(s) %s; found %s" % (missing, header))
        ix = {c: header.index(c) for c in need}
        sx = header.index("sha256") if "sha256" in header else None
        width = len(header)
        for rowno, row in enumerate(reader, 1):
            total += 1
            row = row + [""] * (width - len(row))
            org, repo = row[ix["org"]].strip(), row[ix["repo"]].strip()
            item = (rowno, row[ix["relpath"]], row[ix["filename"]],
                    row[sx] if sx is not None else "")
            if not org or not repo:
                bad.append((rowno, org, repo) + item[1:])
            else:
                groups[(org, repo)].append(item)
    return groups, bad, total


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="input CSV/TSV (org, repo, relpath, filename, sha256)")
    ap.add_argument("--repos-root", required=True,
                    help="folder holding <org>/<repo>, e.g. /data/workarea/archive")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--delimiter", help="field delimiter (default: auto-detect)")
    ap.add_argument("--workers", type=int, default=8, help="repos processed in parallel")
    ap.add_argument("--resume", action="store_true",
                    help="skip repos already finished in a previous run and "
                         "append to the existing outputs")
    ap.add_argument("--details", action="store_true",
                    help="add first/last commit, author, subject and "
                         "last_change_type to file_summary.csv")
    ap.add_argument("--history", action="store_true",
                    help="also write file_history.csv (one row per change to "
                         "each matched file)")
    ap.add_argument("--quiet", action="store_true", help="no progress bar")
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        print("not a file: %s" % args.csv_in, file=sys.stderr)
        return 2
    if not os.path.isdir(args.repos_root):
        print("not a directory: %s" % args.repos_root, file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    delim = args.delimiter or detect_delimiter(args.csv_in)
    groups, bad, total = read_input(args.csv_in, delim)
    print("input     %s rows, %s repo(s), read in %.1fs"
          % (f"{total:,}", f"{len(groups):,}", time.time() - t0))

    done_path = os.path.join(args.out, DONE_FILE)
    done = set()
    sum_path = os.path.join(args.out, "file_summary.csv")
    his_path = os.path.join(args.out, "file_history.csv")
    resuming = args.resume and os.path.exists(sum_path)
    if resuming and os.path.exists(done_path):
        with open(done_path, encoding="utf-8") as fh:
            done = {tuple(ln.rstrip("\n").split("\t")) for ln in fh if ln.strip()}
    mode = "a" if resuming else "w"
    out_cols = CORE_COLUMNS + (DETAIL_COLUMNS if args.details else [])
    pick = [SUMMARY_HEADER.index(c) for c in out_cols]

    todo = sorted((k for k in groups if k not in done),
                  key=lambda k: -len(groups[k]))
    rows_done = sum(len(groups[k]) for k in done if k in groups)
    counts, hist_rows = Counter(), 0
    bar = Progress("files", total, not args.quiet)

    kw = dict(newline="", encoding="utf-8", errors="surrogateescape")
    with open(sum_path, mode, **kw) as sf, \
            open(his_path, mode, **kw) if args.history else nullcontext() as hf, \
            open(done_path, mode if resuming else "w", encoding="utf-8") as df:
        sw = csv.writer(sf)
        hw = csv.writer(hf) if args.history else None
        if not resuming:
            sw.writerow(out_cols)
            if hw:
                hw.writerow(HISTORY_HEADER)
        if bad and not resuming:
            for rowno, org, repo, rel, fname, sha in bad:
                r = [""] * len(SUMMARY_HEADER)
                r[:6] = [rowno, org, repo, rel, fname, sha]
                r[SUMMARY_HEADER.index("status")] = "bad_row"
                sw.writerow([r[i] for i in pick])
            counts["bad_row"] += len(bad)
        rows_done += len(bad)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(process_repo, args.repos_root, org, repo,
                                groups[(org, repo)], args.history,
                                args.details): (org, repo)
                    for org, repo in todo}
            repos_done = len(done)
            for fut in as_completed(futs):
                org, repo = futs[fut]
                summary, history = fut.result()
                sw.writerows([[r[i] for i in pick] for r in summary])
                if hw:
                    hw.writerows(history)
                for r in summary:
                    counts[r[SUMMARY_HEADER.index("status")]] += 1
                hist_rows += len(history)
                sf.flush()
                if hw:
                    hf.flush()
                df.write("%s\t%s\n" % (org, repo))
                df.flush()
                repos_done += 1
                rows_done += len(summary)
                bar.update(rows_done, "%d/%d repos" % (repos_done, len(groups)))
        bar.close("%d/%d repos" % (len(groups), len(groups)))

    print("\ndone      %s row(s)%s -> %s"
          % (f"{sum(counts.values()):,}",
             (", %s history row(s)" % f"{hist_rows:,}") if args.history else "",
             args.out))
    for k in ("found", "in_head_no_history", "not_found", "repo_missing",
              "repo_error", "bad_row"):
        if counts[k]:
            print("  %-20s %12s" % (k, f"{counts[k]:,}"))
    print("  %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
