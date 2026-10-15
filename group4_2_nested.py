#!/usr/bin/env python3
"""
group4_2_nested.py - what the first archive_unpack.py run left closed inside
the 4.2 / 4.2_R archives, and what a recursive redo changed.

  find      in one archive_unpack.py output (--unpacked): the files inside
            an archive that were kept whole although they can be opened
            now: a type of --nested-extensions that is not an archive type
            (msapp, sppkg, vsix... = the 4.2_R packages), or an archive at
            depth >= --old-depth (left closed: too deep). -> in --out:
              todo.csv        the repos, as a batch for archive_unpack.py,
                              biggest manifest first
              todo_files.csv  the files (org, repo, path, ext, depth, ...)
              todo_by_extension.csv
  compare   per repo, the manifests saved before the redo (--before) against
            the new ones: files inside, stored, written, opened nested,
            deepest level, still closed (same test: before at --old-depth,
            after at --new-depth = the redo's --max-depth), archives too
            deep -> --out/compare.csv + totals; and per type of file inside,
            in archive_unpack.py's by_type.csv terms (occurrences, distinct,
            GB), before / after / added, plus new_distinct = contents the
            repo did not have before -> --out/compare_by_extension.csv. Only
            these repos changed, so "added" is the change for the whole run
  delta     after the steps ran again for the todo repos: each step's output
            (--run-dir/<step>/<org>/<repo>/...) against the hard-link
            snapshot taken before (--before-out/<step>/<org>/<repo>/...):
            files new, changed (other content) or gone -> --out/delta/
            <step>.csv (step, org, repo, file, status, bytes) and
            delta/summary.csv; unchanged files are not listed
  report    one row per file type and step, for the todo repos, from the
            archives to the files on disk -> --out/by_extension_report.csv:
              inside_*    files inside the archives (as compare_by_extension)
              out_before  output files of the type in the snapshot
              out_after   output files of the type on disk now
              manifest    outputs the step's manifest says it wrote
              delta_new / delta_gone   from delta/<step>.csv
              check       ok when out_after = manifest and out_after -
                          out_before = delta_new - delta_gone
            The type of an output file is taken from its name (a .txt of a
            text step: the name before .txt)

Usage (delta):
    python3 group4_2_nested.py delta --run-dir /data/workarea/group4_2 \
        --before-out /data/workarea/group4_2/nested/before_out \
        --steps group1_text,group2_binary,... --out /data/workarea/group4_2/nested

Usage:
    python3 group4_2_nested.py find --unpacked /data/workarea/group4_2/unpacked \\
        --nested-extensions zip,...,msapp,... --old-depth 3 \\
        --out /data/workarea/group4_2/nested
    python3 group4_2_nested.py compare --unpacked /data/workarea/group4_2/unpacked \\
        --before /data/workarea/group4_2/nested/before \\
        --nested-extensions ... --old-depth 3 --new-depth 20 --out /data/workarea/group4_2/nested
"""

import argparse
import csv
import hashlib
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from archive_unpack import ARCHIVE_EXTS, STATE, STORED, safe_type  # noqa: E402
from repo_extension_summary import ext_key                      # noqa: E402
from group4_2_text import list_repos                            # noqa: E402
from repo_extension_summary import load_extensions              # noqa: E402

FILES_HEADER = ["org", "repo", "path", "ext", "depth", "action", "bytes",
                "archive_path", "stored_as"]


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def read_manifest(path):
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    with open(path, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        return list(csv.DictReader(fh))


def closed(r, nested, depth):
    """A file inside an archive kept whole that could be opened."""
    e = r["ext"]
    return (r["action"] in STORED and bool(r.get("inner_path"))
            and e in nested
            and (e not in ARCHIVE_EXTS or to_int(r.get("depth")) >= depth))


def repo_counts(sd, nested, depth):
    m = os.path.join(sd, "manifest.csv")
    if not os.path.isfile(m):
        return None
    c = Counter()
    for r in read_manifest(m):
        if not r.get("inner_path"):
            continue
        c["files_inside"] += 1
        c["stored"] += r["action"] in STORED
        c["written"] += r["action"] == "written"
        c["opened_nested"] += r["action"] == "opened_nested"
        c["max_depth"] = max(c["max_depth"], to_int(r.get("depth")))
        c["still_closed"] += closed(r, nested, depth)
    a = os.path.join(sd, "archives.csv")
    if os.path.isfile(a):
        c["too_deep"] = sum(r["status"] == "too_deep" for r in read_manifest(a))
    return c


def find(args, nested):
    os.makedirs(args.out, exist_ok=True)
    st = os.path.join(args.unpacked, STATE)
    repos, by_ext = [], {}
    with open(os.path.join(args.out, "todo_files.csv"), "w", newline="",
              encoding="utf-8", errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(FILES_HEADER)
        for org, repo in list_repos(args.unpacked):
            m = os.path.join(st, org, repo, "manifest.csv")
            n = 0
            for r in read_manifest(m):
                if closed(r, nested, args.old_depth):
                    n += 1
                    w.writerow([org, repo, r["path"], r["ext"], r["depth"],
                                r["action"], r["bytes"], r["archive_path"],
                                r["stored_as"]])
                    c = by_ext.setdefault(r["ext"], Counter())
                    c["files"] += 1
                    c["written"] += r["action"] == "written"
                    c["bytes"] += to_int(r["bytes"])
                    c.setdefault("repos", set()).add((org, repo))
            if n:
                repos.append((org, repo, n))
    with open(os.path.join(args.out, "todo.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "tier", "weight", "listed_files"])
        for org, repo, n in repos:          # list_repos: biggest first
            w.writerow([org, repo, "nested", 1, n])
    with open(os.path.join(args.out, "todo_by_extension.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ext", "repos", "files", "distinct", "gb"])
        for e, c in sorted(by_ext.items(), key=lambda kv: -kv[1]["files"]):
            w.writerow([e, len(c["repos"]), c["files"], c["written"],
                        round(c["bytes"] / 1e9, 3)])
    print("%s: %d repo(s) to redo, %d closed file(s) inside -> %s/todo.csv"
          % (args.unpacked, len(repos), sum(n for *_, n in repos), args.out))
    for e, c in sorted(by_ext.items(), key=lambda kv: -kv[1]["files"])[:25]:
        print("  %-14s %6d file(s) in %d repo(s)" % (e, c["files"], len(c["repos"])))
    return 0


COLS = ["files_inside", "stored", "written", "opened_nested", "max_depth",
        "still_closed", "too_deep"]


def type_counts(m, by_type, side, shas=None):
    """Add one manifest's files inside to by_type[type][side_*], as
    archive_unpack.py's by_type.csv counts them; -> {sha: (type, bytes)} of
    the contents written."""
    written = {}
    if not os.path.isfile(m):
        return written
    for r in read_manifest(m):
        if not r.get("inner_path"):
            continue
        c = by_type.setdefault(safe_type(r["ext"]), Counter())
        c["route:" + r.get("route", "")] = 1
        if r["action"] == "opened_nested":
            c[side + "_opened_nested"] += 1
            continue
        c[side + "_occurrences"] += 1
        if r["action"] == "written":
            c[side + "_distinct"] += 1
            c[side + "_bytes"] += to_int(r["bytes"])
            written[r["blob"]] = (safe_type(r["ext"]), to_int(r["bytes"]))
        elif r["action"] != "deduped":
            c[side + "_not_stored"] += 1
    return written


def write_by_type(path, by_type):
    cols = ["occurrences", "distinct", "not_stored", "opened_nested"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "route"] + ["before_" + c for c in cols]
                   + ["after_" + c for c in cols]
                   + ["added_occurrences", "added_distinct", "new_distinct",
                      "new_gb"])
        tot = Counter()
        for t in sorted(by_type, key=lambda t: (-(by_type[t]["after_occurrences"]
                                                  - by_type[t]["before_occurrences"]), t)):
            c = by_type[t]
            route = next((k[6:] for k in c if k.startswith("route:")), "")
            row = ([c["before_" + k] for k in cols] + [c["after_" + k] for k in cols]
                   + [c["after_occurrences"] - c["before_occurrences"],
                      c["after_distinct"] - c["before_distinct"],
                      c["new_distinct"], round(c["new_bytes"] / 1e9, 3)])
            w.writerow([t, route] + row)
            for k in cols:
                tot["b" + k] += c["before_" + k]
                tot["a" + k] += c["after_" + k]
            tot["new_distinct"] += c["new_distinct"]
            tot["new_bytes"] += c["new_bytes"]
        w.writerow(["total", ""] + [tot["b" + k] for k in cols]
                   + [tot["a" + k] for k in cols]
                   + [tot["aoccurrences"] - tot["boccurrences"],
                      tot["adistinct"] - tot["bdistinct"], tot["new_distinct"],
                      round(tot["new_bytes"] / 1e9, 3)])
    return tot


def compare(args, nested):
    todo = os.path.join(args.out, "todo.csv")
    with open(todo, newline="", encoding="utf-8") as fh:
        repos = [(r["org"], r["repo"]) for r in csv.DictReader(fh)]
    tot_b, tot_a, missing, by_type = Counter(), Counter(), [], {}
    with open(os.path.join(args.out, "compare.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["org", "repo", "status"] + ["before_" + c for c in COLS]
                   + ["after_" + c for c in COLS])
        for org, repo in repos:
            b = repo_counts(os.path.join(args.before, org, repo), nested,
                            args.old_depth) or Counter()
            sd = os.path.join(args.unpacked, STATE, org, repo)
            a = (repo_counts(sd, nested, args.new_depth)
                 if os.path.isfile(os.path.join(sd, "done.json")) else None)
            if a is None:
                missing.append("%s/%s" % (org, repo))
                a = Counter()
            else:
                old = type_counts(os.path.join(args.before, org, repo,
                                               "manifest.csv"), by_type, "before")
                new = type_counts(os.path.join(sd, "manifest.csv"), by_type, "after")
                for sha, (t, n) in new.items():
                    if sha not in old:
                        by_type[t]["new_distinct"] += 1
                        by_type[t]["new_bytes"] += n
            for t, c in ((tot_b, b), (tot_a, a)):
                for k in COLS:
                    t[k] = max(t[k], c[k]) if k == "max_depth" else t[k] + c[k]
            w.writerow([org, repo, "not_done" if not a else "done"]
                       + [b[c] for c in COLS] + [a[c] for c in COLS])
    print("%s: %d repo(s), %d not redone yet" % (args.unpacked, len(repos),
                                                  len(missing)))
    print("  %-14s %12s %12s" % ("", "before", "after"))
    for k in COLS:
        print("  %-14s %12s %12s" % (k, f"{tot_b[k]:,}", f"{tot_a[k]:,}"))
    tt = write_by_type(os.path.join(args.out, "compare_by_extension.csv"), by_type)
    print("  -> %s/compare.csv" % args.out)
    print("  per type: %s more files inside, %s new contents (%.2f GB) -> "
          "%s/compare_by_extension.csv" % (
              f"{tt['aoccurrences'] - tt['boccurrences']:,}",
              f"{tt['new_distinct']:,}", tt["new_bytes"] / 1e9, args.out))
    return 1 if missing else 0


def md5(fp):
    h = hashlib.md5()
    with open(fp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree(root):
    """-> {relative path: size} of the files under root."""
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            fp = os.path.join(d, f)
            out[os.path.relpath(fp, root)] = os.path.getsize(fp)
    return out


def delta(args):
    with open(os.path.join(args.out, "todo.csv"), newline="", encoding="utf-8") as fh:
        repos = [(r["org"], r["repo"]) for r in csv.DictReader(fh)]
    dd = os.path.join(args.out, "delta")
    os.makedirs(dd, exist_ok=True)
    summ = []
    for step in args.steps.split(","):
        if not os.path.isdir(os.path.join(args.run_dir, step)):
            continue
        c = Counter()
        with open(os.path.join(dd, step + ".csv"), "w", newline="",
                  encoding="utf-8", errors="surrogateescape") as fh:
            w = csv.writer(fh)
            w.writerow(["step", "org", "repo", "file", "status", "bytes"])
            for org, repo in repos:
                old_root = os.path.join(args.before_out, step, org, repo)
                new_root = os.path.join(args.run_dir, step, org, repo)
                old, new = tree(old_root), tree(new_root)
                for f in sorted(set(old) | set(new)):
                    if f not in old:
                        st = "new"
                    elif f not in new:
                        st = "gone"
                    elif old[f] != new[f] or md5(os.path.join(old_root, f)) \
                            != md5(os.path.join(new_root, f)):
                        st = "changed"
                    else:
                        c["unchanged"] += 1
                        continue
                    c[st] += 1
                    c[st + "_bytes"] += new.get(f, old.get(f, 0))
                    w.writerow([step, org, repo, f, st, new.get(f, old.get(f))])
        summ.append([step, c["unchanged"], c["new"], c["changed"], c["gone"],
                     round((c["new_bytes"] + c["changed_bytes"]) / 1e9, 3)])
    with open(os.path.join(dd, "summary.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["step", "unchanged", "new", "changed", "gone", "gb_to_send"])
        w.writerows(summ)
    print("delta of %d repo(s) -> %s" % (len(repos), dd))
    print("  %-18s %10s %8s %8s %6s %10s" % ("step", "unchanged", "new",
                                              "changed", "gone", "GB"))
    for r in summ:
        print("  %-18s %10s %8s %8s %6s %10s" % tuple(r))
    return 0


TXT_STEPS = {"group1_text", "group4_5_R_text", "group4_5_text", "group2_text",
             "group3_pkl_text"}


def out_type(step, rel):
    """The source type of an output file, from its name."""
    name = os.path.basename(rel)
    if step in TXT_STEPS and name.endswith(".txt"):
        name = name[:-4]
    return safe_type(ext_key(name))


def report(args):
    with open(os.path.join(args.out, "todo.csv"), newline="", encoding="utf-8") as fh:
        repos = [(r["org"], r["repo"]) for r in csv.DictReader(fh)]
    inside = {}
    for org, repo in repos:
        old = type_counts(os.path.join(args.before, org, repo, "manifest.csv"),
                          inside, "before")
        new = type_counts(os.path.join(args.run_dir, "unpacked", STATE, org, repo,
                                       "manifest.csv"), inside, "after")
        for sha, (t, n) in new.items():
            if sha not in old:
                inside[t]["new_distinct"] += 1
    out = {}                                  # (type, step) -> Counter
    for step in args.steps.split(","):
        sdir = os.path.join(args.run_dir, step)
        if not os.path.isdir(sdir):
            continue
        for org, repo in repos:
            for side, root in (("out_before", os.path.join(args.before_out, step)),
                               ("out_after", sdir)):
                for rel in tree(os.path.join(root, org, repo)):
                    out.setdefault((out_type(step, rel), step), Counter())[side] += 1
            for f in ("left_out.csv",):                # handled, not written
                lo = os.path.join(sdir, STATE, org, repo, f)
                if os.path.isfile(lo):
                    for r in read_manifest(lo):
                        if r.get("ext") is not None:
                            out.setdefault((safe_type(r["ext"]), step), Counter())
            m = os.path.join(sdir, STATE, org, repo, "manifest.csv")
            if os.path.isfile(m):
                seen = set()
                for r in read_manifest(m):
                    if r.get("ext") is not None:
                        out.setdefault((safe_type(r["ext"]), step), Counter())
                    if "output" not in r:              # pkl_to_text: no column
                        out.setdefault((out_type(step, r["path"] + ".txt"), step),
                                       Counter())["no_manifest"] = 1
                        continue
                    for o in (r["output"] or "").split(";"):   # pdf: a; b
                        if o and o not in seen:
                            seen.add(o)
                            out.setdefault((out_type(step, o), step),
                                           Counter())["manifest"] += 1
        d = os.path.join(args.out, "delta", step + ".csv")
        if os.path.isfile(d):
            for r in read_manifest(d):
                out.setdefault((out_type(step, r["file"]), step),
                               Counter())["delta_" + r["status"]] += 1
    rows = []
    for (t, step), c in out.items():
        rows.append((t, step, c))
    stepped = {t for t, _ in out}
    for t in inside:
        if t not in stepped:
            rows.append((t, "", Counter()))
    cols = ["out_before", "out_after", "manifest", "delta_new", "delta_gone"]
    tot = defaultdict(Counter)
    bad = 0
    path = os.path.join(args.out, "by_extension_report.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "route", "step", "inside_before", "inside_after",
                    "inside_added", "inside_new_distinct"] + cols + ["check"])
        for t, step, c in sorted(rows, key=lambda x: (
                -(inside.get(x[0], Counter())["after_occurrences"]
                  - inside.get(x[0], Counter())["before_occurrences"]), x[0], x[1])):
            i = inside.get(t, Counter())
            route = next((k[6:] for k in i if k.startswith("route:")), "")
            if step:
                ok = ((c["no_manifest"] or c["out_after"] == c["manifest"])
                      and c["out_after"] - c["out_before"]
                      == c["delta_new"] - c["delta_gone"])
                check = "ok" if ok else "CHECK"
                bad += not ok
            else:
                check = "not processed"
            w.writerow([t, route, step, i["before_occurrences"], i["after_occurrences"],
                        i["after_occurrences"] - i["before_occurrences"], i["new_distinct"]]
                       + [c[k] for k in cols[:2]]
                       + ["" if c["no_manifest"] else c["manifest"]]
                       + [c[k] for k in cols[3:]] + [check])
            for k in cols:
                tot[step or "not processed"][k] += c[k]
            tot[step or "not processed"]["added"] += \
                i["after_occurrences"] - i["before_occurrences"]
    print("%d repo(s) -> %s, %d type/step row(s) to CHECK" % (len(repos), path, bad))
    print("  %-18s %8s %10s %10s %9s %9s %6s" % ("step", "added in", "out_before",
                                                "out_after", "manifest", "new", "gone"))
    for step, c in sorted(tot.items()):
        print("  %-18s %8s %10s %10s %9s %9s %6s" % (
            step, c["added"], c["out_before"], c["out_after"], c["manifest"],
            c["delta_new"], c["delta_gone"]))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["find", "compare", "delta", "report"])
    ap.add_argument("--unpacked", help="archive_unpack.py --out")
    ap.add_argument("--nested-extensions",
                    help="types opened inside: comma list or a file")
    ap.add_argument("--old-depth", type=int, default=3,
                    help="archives at this depth or deeper count as closed")
    ap.add_argument("--new-depth", type=int, default=20,
                    help="compare: the redo's --max-depth")
    ap.add_argument("--before", help="compare: the saved _state copies")
    ap.add_argument("--run-dir", help="delta: the run folder, e.g. "
                                      "/data/workarea/group4_2")
    ap.add_argument("--before-out", help="delta: the snapshot folder")
    ap.add_argument("--steps", help="delta: step folders, comma list")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.action in ("delta", "report"):
        if not (args.run_dir and args.before_out and args.steps):
            ap.error("%s needs --run-dir, --before-out and --steps" % args.action)
        if args.action == "report" and not args.before:
            ap.error("report needs --before")
        return delta(args) if args.action == "delta" else report(args)
    if not (args.unpacked and args.nested_extensions):
        ap.error("find / compare need --unpacked and --nested-extensions")
    nested = set(load_extensions(args.nested_extensions))
    if args.action == "find":
        return find(args, nested)
    if not args.before:
        ap.error("compare needs --before")
    return compare(args, nested)


if __name__ == "__main__":
    sys.exit(main())
