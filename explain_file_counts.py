#!/usr/bin/env python3
"""
explain_file_counts.py - where the files in a file_added_lines.py output
came from, read from its per-repo manifests (_state/<org>/<repo>/manifest.csv).

History mode writes a file for every path that ever existed in a repo's
history, so it finds far more files than an inventory of the checked-out
folders. This splits the total into the parts that explain the gap:

  * written vs not written (no_text, binary, name_too_long)
  * vendored / generated folders (node_modules, bower_components, vendor,
    dist, build, ...) - committed third-party code
  * at_head: still in HEAD's tree vs deleted / branch-only / renamed away
  * extensions, and the repos contributing most
  * with --inventory repo_extension_summary_*.csv: per repo, files written
    against the inventory's listed_files, and the repos with the largest gap

It also checks the output is self-consistent: .txt files on disk against
manifest rows that say a file was written (--check-disk, slower).

Usage:
    python3 explain_file_counts.py /data/workarea/full_extract
    python3 explain_file_counts.py /data/workarea/full_extract \\
        --inventory repo_extension_summary_github.csv --out counts
"""

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

VENDOR_DIRS = ["node_modules", "bower_components", "jspm_packages", "vendor",
               "packages", "dist", "build", "bin", "obj", "target", ".next",
               "coverage", "__pycache__", "site-packages", "wwwroot/lib"]


def vendor_of(path):
    """The first vendored/generated folder in a path, or ''."""
    segs = path.lower().split("/")[:-1]
    joined = "/".join(segs)
    for v in VENDOR_DIRS:
        if "/" in v:
            if ("/" + v + "/") in ("/" + joined + "/"):
                return v
        elif v in segs:
            return v
    return ""


def ext_of(path):
    leaf = path.rsplit("/", 1)[-1].lower()
    if leaf.startswith(".") and leaf.count(".") == 1:
        return leaf[1:]
    stem, dot, e = leaf.rpartition(".")
    return e if dot and stem else "(none)"


def pct(n, d):
    return "%5.1f%%" % (100.0 * n / d) if d else "    -"


def table(title, counter, total, n=15):
    print("\n" + title)
    for k, v in counter.most_common(n):
        print("  %-28s %12s  %s" % (k or "(none)", f"{v:,}", pct(v, total)))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="the --out folder of file_added_lines.py")
    ap.add_argument("--inventory",
                    help="repo_extension_summary CSV to compare per repo "
                         "(listed_files)")
    ap.add_argument("--out", help="folder for per-repo CSVs (optional)")
    ap.add_argument("--check-disk", action="store_true",
                    help="also count the .txt files on disk and compare")
    args = ap.parse_args()

    state = os.path.join(args.out_dir, "_state")
    if not os.path.isdir(state):
        sys.exit("no _state folder under " + args.out_dir)
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

    rows = written = 0
    status = Counter()
    head = Counter()
    vendor = Counter()                # written files per vendor folder
    ext = Counter()
    kind = Counter()                  # vendored x at_head, written files
    per_repo = defaultdict(lambda: Counter())
    manifests = 0
    for org in sorted(os.listdir(state)):
        od = os.path.join(state, org)
        if not os.path.isdir(od):
            continue
        for repo in sorted(os.listdir(od)):
            mp = os.path.join(od, repo, "manifest.csv")
            if not os.path.isfile(mp):
                continue
            manifests += 1
            pr = per_repo[(org, repo)]
            with open(mp, newline="", encoding="utf-8",
                      errors="surrogateescape") as fh:
                for r in csv.DictReader(fh):
                    rows += 1
                    status[r["status"]] += 1
                    if not r["output"]:
                        continue
                    written += 1
                    p = r["path"]
                    v = vendor_of(p)
                    h = r["at_head"] or "unknown"
                    head[h] += 1
                    vendor[v or "(own code)"] += 1
                    ext[ext_of(p)] += 1
                    kind["%s, %s" % ("vendored" if v else "own code",
                                     {"yes": "at HEAD", "no": "not at HEAD"}
                                     .get(h, "HEAD unknown"))] += 1
                    pr["written"] += 1
                    pr["vendored"] += bool(v)
                    pr["not_at_head"] += h == "no"

    print("repos with a manifest   %s" % f"{manifests:,}")
    print("manifest rows (files)   %s" % f"{rows:,}")
    print("files written (.txt)    %s" % f"{written:,}")
    print("state files on disk     ~%s (manifest.csv + done.json per repo)"
          % f"{2 * manifests:,}")
    table("files by status (all rows)", status, rows)
    table("written files: own code vs vendored, at HEAD or not", kind, written)
    table("written files by vendored folder", vendor, written)
    table("written files at HEAD (no = deleted, renamed away or branch-only)",
          head, written)
    table("written files by extension", ext, written, 20)

    top = sorted(per_repo.items(), key=lambda kv: -kv[1]["written"])[:15]
    print("\nrepos with the most files written")
    print("  %-50s %10s %10s %11s" % ("repo", "written", "vendored",
                                      "not at HEAD"))
    for (o, r), c in top:
        print("  %-50s %10s %10s %11s" % (o + "/" + r, f"{c['written']:,}",
                                          f"{c['vendored']:,}",
                                          f"{c['not_at_head']:,}"))

    inv = {}
    if args.inventory:
        with open(args.inventory, newline="", encoding="utf-8",
                  errors="surrogateescape") as fh:
            for r in csv.DictReader(fh):
                if r.get("org") and r["org"] != "(all)":
                    try:
                        inv[(r["org"], r["repo"])] = int(r.get("listed_files") or 0)
                    except ValueError:
                        pass
        both = [k for k in per_repo if k in inv]
        inv_total = sum(inv[k] for k in both)
        got = sum(per_repo[k]["written"] for k in both)
        own_head = sum(per_repo[k]["written"] - per_repo[k]["vendored"]
                       - per_repo[k]["not_at_head"] for k in both)
        print("\ncompared with the inventory (%s repos in both)"
              % f"{len(both):,}")
        print("  inventory listed_files          %12s" % f"{inv_total:,}")
        print("  history mode files written      %12s  (x%.1f)"
              % (f"{got:,}", got / inv_total if inv_total else 0))
        print("  ... of which own code at HEAD   %12s  (roughly what an "
              "inventory can see)" % f"{max(own_head, 0):,}")
        gap = sorted(both, key=lambda k: -(per_repo[k]["written"] - inv[k]))[:15]
        print("\nrepos with the largest gap (written - inventory)")
        for k in gap:
            c = per_repo[k]
            print("  %-50s inventory %8s  written %9s  vendored %9s  not at "
                  "HEAD %9s" % (k[0] + "/" + k[1], f"{inv[k]:,}",
                                f"{c['written']:,}", f"{c['vendored']:,}",
                                f"{c['not_at_head']:,}"))

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "files_per_repo.csv"), "w",
                  newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["org", "repo", "written", "vendored", "not_at_head",
                        "inventory_listed_files"])
            for (o, r), c in sorted(per_repo.items()):
                w.writerow([o, r, c["written"], c["vendored"],
                            c["not_at_head"], inv.get((o, r), "")])
        print("\n-> %s" % os.path.join(args.out, "files_per_repo.csv"))

    if args.check_disk:
        n = 0
        for dirpath, dirnames, filenames in os.walk(args.out_dir):
            if dirpath == args.out_dir:
                dirnames[:] = [d for d in dirnames if not d.startswith("_")]
            n += sum(1 for f in filenames if f.endswith(".txt"))
        print("\n.txt files on disk      %s  (manifests say %s)%s"
              % (f"{n:,}", f"{written:,}",
                 "" if n == written else "  <- MISMATCH"))


if __name__ == "__main__":
    main()
