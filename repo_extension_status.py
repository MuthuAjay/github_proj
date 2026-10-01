#!/usr/bin/env python3
"""
repo_extension_status.py - for specific repos: every file extension in their
history, how many files and versions, where each extension sits in the
process, and how many files were sent for it.

Reads only outputs that already exist (nothing is rerun):

  --counts          all_extension_counts.py output: by_repo.csv (files and
                    commits per repo and extension, the whole history)
  --group1-extract  first extraction (file_added_lines.py): its done.json
                    says whether the repo was extracted at all
  --group1-delta    its delta (file_delta.py): group 1 files sent
  --group2-text     group 2 text delta (file_delta.py): files sent
  --group2-binary   group 2 binary extraction (extract_versions.py): files
                    sent (unique stored files of the repo)

Stage per extension:
  Group 1 - text          the 32 of the first extraction: historical lines
                          not in the current file, one file per file
  Group 2 - text          the 9 text types of the second: the same
  Group 2 - whole files   the 18 binary types: old versions as whole files,
                          identical ones sent once (files_sent counts the
                          repo's distinct files, also when another repo
                          holds the same file)
  Under review            pkl, sentinel, decTest, noun, pdf - not extracted
  Not in scope            everything else - not extracted

files_sent: blank when the extension is not extracted; 0 when everything was
already scanned or had nothing new. A repo the first extraction failed on
gets a note (its group 1 files were not sent).

A repo is given as ORG/REPO. When the org does not match (eyorg vs ey-org),
the repo is looked up by its name in every org and the match is used (and
reported); an ambiguous or missing name is reported, not guessed.

Output: <out> (CSV, one row per repo and extension) and <out>.md (per repo:
totals per stage and the extensions).

Usage:
    python3 repo_extension_status.py --repo ey-org/qs2-kdp-hcaa \\
        --repo ey-org/qs2-poc-kdp --out /data/workarea/repo_extension_status.csv
"""

import argparse
import csv
import datetime
import json
import os
import sys
from collections import Counter, defaultdict

from extension_versions import BINARY_EXTS, TEXT_EXTS
from repo_extension_summary import EXTENSIONS, ext_key

REVIEW = {"pkl", "sentinel", "dectest", "noun", "pdf"}
STAGES = ["Group 1 - text", "Group 2 - text", "Group 2 - whole files",
          "Under review", "Not in scope"]
STAGE_TEXT = {
    "Group 1 - text": "extracted: historical lines not in the current file",
    "Group 2 - text": "extracted: historical lines not in the current file",
    "Group 2 - whole files": "extracted: old versions as whole files, "
                             "identical ones sent once",
    "Under review": "not extracted yet - being analysed",
    "Not in scope": "not extracted",
}
W = "/data/workarea"


def stage_of(e):
    if e in EXTENSIONS:
        return "Group 1 - text"
    if e in TEXT_EXTS:
        return "Group 2 - text"
    if e in BINARY_EXTS:
        return "Group 2 - whole files"
    if e in REVIEW:
        return "Under review"
    return "Not in scope"


def rows_of(path):
    if not os.path.isfile(path):
        return None
    with open(path, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        return list(csv.DictReader(fh))


def sent_text(delta, org, repo):
    """{ext: files with an output} from a file_delta.py manifest, or None."""
    rows = rows_of(os.path.join(delta, "_state", org, repo, "manifest.csv"))
    if rows is None:
        return None
    out = Counter()
    for r in rows:
        if r.get("output"):
            out[ext_key(r["path"]) or "(none)"] += 1
    return out


def sent_binary(binary, org, repo):
    """{ext: distinct stored files} from extract_versions.py's manifest."""
    rows = rows_of(os.path.join(binary, "_state", org, repo, "manifest.csv"))
    if rows is None:
        return None
    out, seen = Counter(), set()
    for r in rows:
        s = r.get("stored_as")
        if s and s not in seen:
            seen.add(s)
            out[r["ext"]] += 1
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", default=[], metavar="ORG/REPO",
                    help="a repo (repeatable)")
    ap.add_argument("--repos-file", help="a file with one ORG/REPO per line")
    ap.add_argument("--counts", default=W + "/all_ext_counts")
    ap.add_argument("--group1-extract", default=W + "/full_extract")
    ap.add_argument("--group1-delta", default=W + "/full_extract_delta_2")
    ap.add_argument("--group2-text", default=W + "/text9_extract_delta")
    ap.add_argument("--group2-binary", default=W + "/binary_versions")
    ap.add_argument("--out", required=True, help="CSV to write")
    args = ap.parse_args()

    wanted = list(args.repo)
    if args.repos_file:
        with open(args.repos_file, encoding="utf-8") as fh:
            wanted += [ln.strip() for ln in fh if ln.strip()
                       and not ln.startswith("#")]
    if not wanted:
        sys.exit("give --repo ORG/REPO (or --repos-file)")
    by_repo = os.path.join(args.counts, "by_repo.csv")
    if not os.path.isfile(by_repo):
        sys.exit("no by_repo.csv in " + args.counts)

    counts = defaultdict(list)
    with open(by_repo, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        for r in csv.DictReader(fh):
            counts[(r["org"], r["repo"])].append(r)
    by_name = defaultdict(list)
    for o, rp in counts:
        by_name[rp.lower()].append((o, rp))

    resolved, notes = [], []
    for spec in wanted:
        o, _, rp = spec.strip().strip("/").partition("/")
        if (o, rp) in counts:
            resolved.append((spec, o, rp))
            continue
        m = by_name.get(rp.lower(), [])
        if len(m) == 1:
            resolved.append((spec, m[0][0], m[0][1]))
            notes.append("%s: not found as given - using %s/%s"
                         % (spec, m[0][0], m[0][1]))
        elif m:
            notes.append("%s: not found as given; the name matches %s - give "
                         "the org" % (spec, ", ".join("%s/%s" % x for x in m)))
        else:
            notes.append("%s: no repository with that name in the counts"
                         % spec)

    out_rows, md = [], ["# Extensions per repository", "",
                        "Generated %s by repo_extension_status.py." %
                        datetime.date.today().isoformat(), ""]
    for spec, o, rp in resolved:
        try:
            with open(os.path.join(args.group1_extract, "_state", o, rp,
                                   "done.json"), encoding="utf-8") as fh:
                g1_status = json.load(fh).get("status", "?")
        except (OSError, ValueError):
            g1_status = "not run"
        s1 = sent_text(args.group1_delta, o, rp)
        s2t = sent_text(args.group2_text, o, rp)
        s2b = sent_binary(args.group2_binary, o, rp)
        rows = sorted((r for r in counts[(o, rp)]
                       if (r.get("files_in_history") or "0") != "0"),
                      key=lambda r: (-int(r["files_in_history"]), r["ext"]))
        per_stage = defaultdict(lambda: [0, 0, 0, 0])   # exts files changes sent
        for r in rows:
            e = r["ext"]
            st = stage_of(e)
            src = {"Group 1 - text": s1, "Group 2 - text": s2t,
                   "Group 2 - whole files": s2b}.get(st)
            if st in ("Under review", "Not in scope"):
                n = ""
            elif st == "Group 1 - text" and g1_status != "ok":
                # the first extraction failed / did not run for the repo:
                # its group 1 files were never extracted, whatever the delta
                n = "not sent (first extraction: %s)" % g1_status
            elif src is None:
                n = 0
            else:
                n = src.get(e, 0)
            out_rows.append([o, rp, e, int(r["files_in_history"]),
                             int(r["commits"]), st, STAGE_TEXT[st], n])
            ps = per_stage[st]
            ps[0] += 1
            ps[1] += int(r["files_in_history"])
            ps[2] += int(r["commits"])
            ps[3] += n if isinstance(n, int) else 0
        md += ["## %s/%s%s" % (o, rp, "" if spec == "%s/%s" % (o, rp)
                               else " (asked as %s)" % spec), ""]
        if g1_status != "ok":
            md += ["**Note:** the first extraction did not complete for this "
                   "repository (%s), so its Group 1 files were not sent." %
                   g1_status, ""]
        md += ["| stage | extensions | files | versions (changes) | files "
               "sent |", "|---|---:|---:|---:|---:|"]
        for st in STAGES:
            if st in per_stage:
                ps = per_stage[st]
                md.append("| %s | %d | %s | %s | %s |" % (
                    st, ps[0], f"{ps[1]:,}", f"{ps[2]:,}",
                    f"{ps[3]:,}" if st.startswith("Group") else "-"))
        md += ["", "| ext | files | versions (changes) | stage | files sent |",
               "|---|---:|---:|---|---:|"]
        for x in out_rows:
            if (x[0], x[1]) == (o, rp):
                md.append("| %s | %s | %s | %s | %s |" % (
                    x[2], f"{x[3]:,}", f"{x[4]:,}", x[5],
                    f"{x[7]:,}" if isinstance(x[7], int) else (x[7] or "-")))
        md.append("")
    if notes:
        md += ["## Notes", ""] + ["- " + n for n in notes] + [""]
    md += ["files sent: blank / - when the extension is not extracted; 0 when "
           "everything was already scanned or had nothing new. Versions = "
           "commits that changed the files."]

    with open(args.out, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "ext", "files_in_history",
                    "versions(changes)", "stage", "what_happened",
                    "files_sent"])
        w.writerows(out_rows)
    with open(args.out + ".md", "w", encoding="utf-8",
              errors="backslashreplace") as fh:
        fh.write("\n".join(md) + "\n")
    for n in notes:
        print("NOTE " + n)
    print("%d repo(s), %d row(s) -> %s (and .md)"
          % (len(resolved), len(out_rows), args.out))
    return 0 if resolved else 1


if __name__ == "__main__":
    sys.exit(main())
