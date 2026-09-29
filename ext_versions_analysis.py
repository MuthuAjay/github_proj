#!/usr/bin/env python3
"""
ext_versions_analysis.py - analyse the output of extension_versions.py and
write a Markdown report aimed at the decisions still open before the binary
extraction: which extensions, vendored folders or not, a size limit, the
output layout and the disk it needs.

Reads, under the extension_versions.py --out folder:
  _state/<org>/<repo>/done.json, files.csv, versions.csv,
  identical.json, identical.csv            per repo
  by_extension.csv                         for the cross-check

"To send" is the same rule extension_versions.py counts (it is the same
code): every version of a BINARY extension except LFS stubs and the ones
whose content is a file in the same repo's active copy. Each version sent is
also given a reason:

  history_only      the path is not in the active copy
  no_active_repo    the repo has no folder in the active copy
  older_version     the path is at head and its active file is one of its
                    versions - these are the others
  active_differs    at head, but the active file matches no version
                    (no_match / lfs_active_real) - every version goes
  unchecked         at head, pass 2 not run for the repo (latest assumed
                    processed)
  active_unreadable at head, pass 2 could not read the active file

Sections: run health, headline per extension (text and binary), pass 2
results, why versions are sent, vendored share, sizes, age, concentration
by repo, duplication across repos, LFS, certificates (pfx/p7s), lock files,
and a cross-check against by_extension.csv. Only the standard library is
used; everything is streamed, so memory grows with the number of distinct
contents to send, not with the number of rows.

Detail CSVs next to the report:
  run_problems.csv          repos whose pass 1 or pass 2 did not finish ok
  largest_to_send.csv       the biggest distinct contents to send
  top_repos_to_send.csv     repos by GB to send
  duplicated_contents.csv   contents to send that sit in the most repos
  certificates.csv          every pfx / p7s file (for the security team)
  lock_files.csv            lock files by file name
  to_send_by_ext_reason.csv versions / GB to send per extension and reason

Usage:
    python3 ext_versions_analysis.py /data/workarea/ext_versions \\
        --out /data/workarea/ext_versions/analysis
"""

import argparse
import csv
import datetime
import heapq
import os
import sys
import time
from collections import Counter, defaultdict

from extension_versions import (BINARY_EXTS, PROCESSED, STATE, TEXT_EXTS,
                                group_of, processed_in_repo, read_json,
                                read_rows)

MB, GB = 1e6, 1e9
SIZE_BUCKETS = [(100e3, "< 100 KB"), (1 * MB, "100 KB - 1 MB"),
                (10 * MB, "1 - 10 MB"), (100 * MB, "10 - 100 MB"),
                (float("inf"), ">= 100 MB")]
REASONS = ["history_only", "no_active_repo", "older_version", "active_differs",
           "unchecked", "active_unreadable"]
CERTS = ("pfx", "p7s")


def bucket(n):
    for edge, name in SIZE_BUCKETS:
        if n < edge:
            return name
    return SIZE_BUCKETS[-1][1]


def gb(n):
    return "%.2f" % (n / GB)


def num(v):
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.1f}"
    return str(v).replace("|", "/").replace("\n", " ")[:90]


def table(header, rows, max_rows=40):
    rows = list(rows)
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" if i == 0 else "---:"
                          for i in range(len(header))) + "|"]
    out += ["| " + " | ".join(num(v) for v in r) + " |" for r in rows[:max_rows]]
    if len(rows) > max_rows:
        out.append("\n_%s more row(s) in the CSV._" % f"{len(rows) - max_rows:,}")
    return "\n".join(out)


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def ordered(exts):
    known = [e for e in TEXT_EXTS + BINARY_EXTS if e in exts]
    return known + sorted(e for e in exts if e not in known)


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


# --------------------------------------------------------------------------
# one pass over every repo
# --------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.repos = Counter()              # status buckets
        self.problems = []                  # [org, repo, pass, status, error]
        self.ext = defaultdict(Counter)     # ext -> column -> n (files level)
        self.ext_repos = defaultdict(set)
        self.reason = defaultdict(Counter)  # (ext, reason) -> n / bytes
        self.vendor = defaultdict(Counter)  # ext -> vendored/own versions, bytes
        self.years = defaultdict(Counter)   # ext -> first_date year -> versions
        self.results = defaultdict(Counter)  # ext -> pass 2 result -> files
        self.repo_send = Counter()          # (org, repo) -> bytes to send
        self.repo_send_n = Counter()
        self.distinct = {}                  # blob -> [bytes, ext, repos, where]
        self.lfs = defaultdict(Counter)     # ext -> stub versions / repos
        self.lfs_repos = set()
        self.certs = []
        self.locks = defaultdict(Counter)   # file name -> files/versions/bytes
        self.missing = Counter()


def scan(root):
    st = Stats()
    state = os.path.join(root, STATE)
    orgs = sorted(os.listdir(state)) if os.path.isdir(state) else []
    t0, seen_repos = time.time(), 0
    for org in orgs:
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            sd = os.path.join(od, repo)
            done = read_json(os.path.join(sd, "done.json"))
            if not done:
                st.repos["pass 1 not run"] += 1
                continue
            if done.get("status") != "ok":
                st.repos["pass 1 " + done.get("status", "?")] += 1
                st.problems.append([org, repo, "pass 1", done.get("status"),
                                    done.get("error", "")[:300]])
                continue
            st.repos["pass 1 ok"] += 1
            if done.get("recovered"):
                st.repos["read through recovery (no HEAD/refs)"] += 1
            if not done.get("active_copy", True):
                st.repos["no folder in the active copy"] += 1
            ij = read_json(os.path.join(sd, "identical.json"))
            p2 = ij.get("status") if ij else None
            if p2 == "ok":
                st.repos["pass 2 ok"] += 1
            else:
                st.repos["pass 2 " + (p2 or "not run")] += 1
                if p2:
                    st.problems.append([org, repo, "pass 2", p2,
                                        ij.get("error", "")[:300]])
            scan_repo(st, org, repo, sd, p2 == "ok")
            seen_repos += 1
            if seen_repos % 1000 == 0:
                print("  %s repos read  %.0fs" % (f"{seen_repos:,}",
                                                  time.time() - t0),
                      file=sys.stderr, flush=True)
    return st


def scan_repo(st, org, repo, sd, pass2):
    files = {f["path"]: f for f in read_rows(os.path.join(sd, "files.csv"))}
    ident = {}
    if pass2:
        ident = {r["path"]: r for r in
                 read_rows(os.path.join(sd, "identical.csv"))}
    processed = processed_in_repo(sd)
    here = set()                                 # blobs counted for this repo
    lfs_here = set()                             # exts with an LFS stub here

    for p, f in files.items():
        e = f["ext"]
        c = st.ext[e]
        st.ext_repos[e].add((org, repo))
        if f["in_history"] != "yes":
            c["files_active_only"] += 1
            continue
        c["files"] += 1
        c["versions"] += int(f["versions"])
        c["bytes"] += int(f["bytes_versions"])
        c["at_" + (f["at_head"] or "none")] += 1
        c["vendored_files"] += bool(f["vendored"])
        st.missing[e] += int(f["missing_objects"])
        if f["group"] == "binary" and f["at_head"] == "yes":
            r = ident.get(p)
            st.results[e][r["result"] if r else "unchecked"] += 1
        if e in CERTS:
            st.certs.append([org, repo, p, e, f["at_head"] or "no active repo",
                             f["versions"], f["commits"], f["bytes_versions"],
                             f["vendored"]])
        if e == "lock":
            name = p.rsplit("/", 1)[-1].lower()
            lk = st.locks[name]
            lk["files"] += 1
            lk["versions"] += int(f["versions"])
            lk["bytes"] += int(f["bytes_versions"])
            lk["vendored"] += bool(f["vendored"])

    for v in read_rows(os.path.join(sd, "versions.csv")):
        p = v["path"]
        f = files.get(p)
        if f is None or f["group"] != "binary":
            continue
        e = f["ext"]
        n = int(v["bytes"]) if v["bytes"].isdigit() else 0
        if v["lfs_oid"]:
            st.lfs[e]["stub_versions"] += 1
            st.lfs_repos.add((org, repo))
            lfs_here.add(e)
            continue
        d = bytes.fromhex(v["blob"])
        if d in processed:
            st.ext[e]["versions_processed"] += 1
            continue
        at = f["at_head"]
        if at == "":
            why = "no_active_repo"
        elif at == "no":
            why = "history_only"
        else:
            r = ident.get(p)
            res = r["result"] if r else None
            if res is None:
                why = "unchecked"
            elif res in PROCESSED:
                why = "older_version"
            elif res in ("no_match", "lfs_active_real"):
                why = "active_differs"
            else:
                why = "active_unreadable"
        rc = st.reason[(e, why)]
        rc["versions"] += 1
        rc["bytes"] += n
        vc = st.vendor[e]
        vc["vendored" if f["vendored"] else "own"] += 1
        vc["vendored_bytes" if f["vendored"] else "own_bytes"] += n
        st.years[e][(v["first_date"] or "????")[:4]] += 1
        st.repo_send[(org, repo)] += n
        st.repo_send_n[(org, repo)] += 1
        g = st.distinct.get(d)
        if g is None:
            st.distinct[d] = [n, e, 1, "%s/%s:%s" % (org, repo, p)]
        elif d not in here:
            g[2] += 1
        here.add(d)
    for e in lfs_here:
        st.lfs[e]["repos"] += 1


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def report(st, root, out):
    md = ["# Extension versions - analysis", "",
          "Source: `%s`. Generated %s by ext_versions_analysis.py." % (
              root, datetime.date.today().isoformat()), "",
          "\"To send\" = the previous versions of the binary extensions: every "
          "version except LFS stubs and those whose content is a file in the "
          "same repo's active copy (the head is processed).", ""]

    def sec(title, why, body):
        md.extend(["## " + title, "", "_%s_" % why, "", body, ""])

    # run health
    rows = sorted(st.repos.items(), key=lambda kv: kv[0])
    body = table(["repos", "count"], rows)
    if st.problems:
        body += "\n\nProblems: %d (listed in run_problems.csv)." % len(st.problems)
        write_csv(os.path.join(out, "run_problems.csv"),
                  ["org", "repo", "pass", "status", "error"], st.problems)
    sec("Run health", "Did every repo finish both passes? Repos that did not "
        "are missing from every number below.", body)

    # headline - text
    text = [e for e in ordered(st.ext) if group_of(e) == "text"]
    sec("Text extensions", "Files and versions of the line-delta group (these "
        "go through file_added_lines.py + file_delta.py, not this "
        "extraction).",
        table(["ext", "repos", "files", "at head", "history only",
               "no active repo", "active only", "versions", "GB (per file)",
               "vendored files"],
              [[e, len(st.ext_repos[e]), st.ext[e]["files"],
                st.ext[e]["at_yes"], st.ext[e]["at_no"], st.ext[e]["at_none"],
                st.ext[e]["files_active_only"], st.ext[e]["versions"],
                gb(st.ext[e]["bytes"]), st.ext[e]["vendored_files"]]
               for e in text]))

    # headline - binary
    binary = [e for e in ordered(st.ext) if group_of(e) == "binary"]
    send_n = {e: sum(st.reason[(e, r)]["versions"] for r in REASONS)
              for e in binary}
    send_b = {e: sum(st.reason[(e, r)]["bytes"] for r in REASONS)
              for e in binary}
    dist_n, dist_b = Counter(), Counter()
    for size, e, _r, _w in st.distinct.values():
        dist_n[e] += 1
        dist_b[e] += size
    rows = [[e, len(st.ext_repos[e]), st.ext[e]["files"], st.ext[e]["at_yes"],
             st.ext[e]["versions"], gb(st.ext[e]["bytes"]),
             st.ext[e]["versions_processed"], st.lfs[e]["stub_versions"],
             send_n[e], dist_n[e], gb(dist_b[e])] for e in binary]
    rows.append(["**total**", "", sum(st.ext[e]["files"] for e in binary),
                 sum(st.ext[e]["at_yes"] for e in binary),
                 sum(st.ext[e]["versions"] for e in binary),
                 gb(sum(st.ext[e]["bytes"] for e in binary)),
                 sum(st.ext[e]["versions_processed"] for e in binary),
                 sum(st.lfs[e]["stub_versions"] for e in binary),
                 sum(send_n.values()), sum(dist_n.values()),
                 gb(sum(dist_b.values()))])
    sec("Binary extensions - what would be extracted",
        "versions = every distinct content per file; processed = the ones "
        "already in the same repo's active copy; to send = the rest (minus "
        "LFS stubs); distinct = each content once across all repos, which is "
        "what the extraction writes to disk.",
        table(["ext", "repos", "files", "at head", "versions", "GB",
               "processed", "LFS stubs", "to send", "distinct to send",
               "GB distinct"], rows, max_rows=60))

    # pass 2
    res_cols = ["same_as_latest", "same_as_older", "same_as_other_path",
                "no_match", "lfs_active_real", "gone", "unreadable",
                "unchecked"]
    rows = []
    for e in binary:
        r = st.results[e]
        tot = sum(r.values())
        rows.append([e, tot] + [r[c] for c in res_cols]
                    + [pct(r["same_as_latest"] + r["same_as_older"]
                           + r["same_as_other_path"], tot)])
    sec("Pass 2 - is the active file one of its versions?",
        "Binary files at head. A high no_match share means the active copy "
        "holds content the archive never had (edited after the archive, or "
        "another branch) - all of that file's versions are sent.",
        table(["ext", "at head"] + res_cols + ["matched %"], rows))

    # reasons
    rows, csv_rows = [], []
    for e in binary:
        tot = send_n[e]
        rows.append([e, tot] + ["%s (%s%%)" % (f"{st.reason[(e, r)]['versions']:,}",
                                               pct(st.reason[(e, r)]["versions"],
                                                   tot)) for r in REASONS])
        for r in REASONS:
            csv_rows.append([e, r, st.reason[(e, r)]["versions"],
                             st.reason[(e, r)]["bytes"]])
    write_csv(os.path.join(out, "to_send_by_ext_reason.csv"),
              ["ext", "reason", "versions", "bytes"], csv_rows)
    sec("Why each version is sent", "history_only / no_active_repo: the file "
        "is not in the active copy at all. older_version: the file is there, "
        "these are its earlier contents. active_differs: the active file is "
        "none of its versions. unchecked: pass 2 missing for the repo.",
        table(["ext", "to send"] + REASONS, rows))

    # vendored
    rows = []
    for e in binary:
        v = st.vendor[e]
        tot = v["vendored"] + v["own"]
        rows.append([e, tot, v["vendored"], pct(v["vendored"], tot),
                     gb(v["vendored_bytes"]), v["own"], gb(v["own_bytes"])])
    sec("Vendored folders (node_modules, packages, bin, obj, ...)",
        "Versions to send that sit in third-party / build folders - "
        "candidates for --skip-vendored.",
        table(["ext", "to send", "vendored", "vendored %", "vendored GB",
               "own", "own GB"], rows))

    # sizes
    by = defaultdict(Counter)
    byb = defaultdict(Counter)
    for size, e, _r, _w in st.distinct.values():
        by[e][bucket(size)] += 1
        byb[e][bucket(size)] += size
    names = [n for _e, n in SIZE_BUCKETS]
    rows = [[e] + ["%s / %s GB" % (f"{by[e][n]:,}", gb(byb[e][n]))
                   for n in names] for e in binary]
    tb = Counter()
    for e in binary:
        tb.update(byb[e])
    rows.append(["**total**"] + ["%s / %s GB" % (
        f"{sum(by[e][n] for e in binary):,}", gb(tb[n])) for n in names])
    sec("Sizes of the distinct contents to send",
        "Files / GB per size band - what a --max-bytes limit would cut.",
        table(["ext"] + names, rows, max_rows=60))
    big = heapq.nlargest(200, st.distinct.items(), key=lambda kv: kv[1][0])
    write_csv(os.path.join(out, "largest_to_send.csv"),
              ["blob", "bytes", "ext", "repos", "example"],
              [[d.hex(), g[0], g[1], g[2], g[3]] for d, g in big])
    sec("Largest contents to send", "The 15 biggest; the top 200 are in "
        "largest_to_send.csv.",
        table(["bytes", "ext", "repos", "example"],
              [[g[0], g[1], g[2], g[3]] for _d, g in big[:15]]))

    # age
    years = sorted({y for e in binary for y in st.years[e]})
    rows = [[e] + [st.years[e][y] for y in years] for e in binary]
    sec("Age - year each version to send first appeared",
        "Old versions may be less relevant, or more likely to hold data "
        "nobody reviewed.", table(["ext"] + years, rows))

    # repos
    top = st.repo_send.most_common()
    tot = sum(st.repo_send.values())
    write_csv(os.path.join(out, "top_repos_to_send.csv"),
              ["org", "repo", "versions", "bytes", "share %"],
              [[o, r, st.repo_send_n[(o, r)], b, pct(b, tot)]
               for (o, r), b in top])
    conc = [["top %d repos" % k, pct(sum(b for _k, b in top[:k]), tot)]
            for k in (10, 50, 100, 500) if len(top) >= k]
    sec("Concentration by repo", "How much of the GB to send a few repos "
        "hold - they could be extracted first or reviewed on their own.",
        table(["repos", "share of GB %"], conc) + "\n\n"
        + table(["org", "repo", "versions", "GB", "share %"],
                [[o, r, st.repo_send_n[(o, r)], gb(b), pct(b, tot)]
                 for (o, r), b in top[:20]]))

    # duplication
    multi = [g for g in st.distinct.values() if g[2] > 1]
    per_file = sum(send_n.values())
    dups = heapq.nlargest(200, st.distinct.items(), key=lambda kv: kv[1][2])
    write_csv(os.path.join(out, "duplicated_contents.csv"),
              ["blob", "bytes", "ext", "repos", "example"],
              [[d.hex(), g[0], g[1], g[2], g[3]] for d, g in dups])
    sec("Duplication across repos", "The same content in several repos is "
        "written once (layout A); a readable per-repo tree (layout B) would "
        "show it once per repo.",
        table(["", "count"], [
            ["versions to send, per file", per_file],
            ["distinct contents to send", len(st.distinct)],
            ["contents in more than one repo", len(multi)],
            ["saved by dedup (versions)", per_file - len(st.distinct)]])
        + "\n\n" + table(["bytes", "ext", "repos", "example"],
                         [[g[0], g[1], g[2], g[3]]
                          for _d, g in dups[:10] if g[2] > 1]))

    # LFS
    sec("Git LFS", "Versions that are only LFS stubs: the archive does not "
        "hold the content, so nothing can be sent for them.",
        table(["ext", "stub versions", "repos"],
              [[e, st.lfs[e]["stub_versions"], st.lfs[e]["repos"]]
               for e in binary if st.lfs[e]["stub_versions"]])
        + "\n\nRepos with any LFS stub: %s" % f"{len(st.lfs_repos):,}")

    # certificates
    write_csv(os.path.join(out, "certificates.csv"),
              ["org", "repo", "path", "ext", "at_head", "versions", "commits",
               "bytes_versions", "vendored"], st.certs)
    cc = Counter((c[3], c[4]) for c in st.certs)
    sec("Certificates (pfx / p7s)", "pfx usually holds a private key. Listed "
        "in certificates.csv for the security team; sending the files to a "
        "PII scanner is a decision of its own.",
        table(["ext", "at head", "files"],
              [[e, h, n] for (e, h), n in sorted(cc.items())])
        + "\n\nRepos: %s" % f"{len({(c[0], c[1]) for c in st.certs}):,}")

    # lock files
    lk = sorted(st.locks.items(), key=lambda kv: -kv[1]["bytes"])
    write_csv(os.path.join(out, "lock_files.csv"),
              ["file_name", "files", "versions", "bytes", "vendored"],
              [[n, c["files"], c["versions"], c["bytes"], c["vendored"]]
               for n, c in lk])
    sec("Lock files by name", "What `.lock` actually is here - package-manager "
        "lock files are large and rarely hold PII.",
        table(["file name", "files", "versions", "GB", "vendored"],
              [[n, c["files"], c["versions"], gb(c["bytes"]), c["vendored"]]
               for n, c in lk[:20]]))

    # missing objects
    miss = [[e, n] for e, n in st.missing.items() if n]
    if miss:
        sec("Missing objects", "Versions named in history whose blob is not in "
            "the archive - they cannot be extracted.",
            table(["ext", "versions"], miss))

    # cross-check
    rows = []
    be = os.path.join(root, "by_extension.csv")
    if os.path.isfile(be):
        with open(be, newline="", encoding="utf-8") as fh:
            ref = {r["ext"]: r for r in csv.DictReader(fh)}
        for e in binary:
            want = ref.get(e, {})
            rows.append([e, want.get("versions_to_send", "-"), send_n[e],
                         want.get("versions_to_send_all_repos", "-"), dist_n[e],
                         "yes" if str(send_n[e]) == want.get("versions_to_send")
                         and str(dist_n[e]) == want.get(
                             "versions_to_send_all_repos") else "NO"])
        ok = all(r[-1] == "yes" for r in rows)
        sec("Cross-check with by_extension.csv",
            "The same counts, computed twice. \"NO\" means by_extension.csv "
            "is older than the state - rerun extension_versions.py "
            "--combine-only.",
            ("All match." if ok else "**Mismatch - see below.**") + "\n\n"
            + table(["ext", "by_extension to send", "here", "by_extension "
                     "distinct", "here", "match"], rows))

    path = os.path.join(out, "ext_versions_analysis.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="extension_versions.py --out folder")
    ap.add_argument("--out", help="folder for the report and CSVs "
                                  "(default: <root>/analysis)")
    args = ap.parse_args()
    if not os.path.isdir(os.path.join(args.root, STATE)):
        sys.exit("no _state folder under " + args.root)
    out = args.out or os.path.join(args.root, "analysis")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    st = scan(args.root)
    path = report(st, args.root, out)
    print("report -> %s  (%.0fs)" % (path, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
