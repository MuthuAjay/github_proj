#!/usr/bin/env python3
"""
extension_delta_summary.py - one CSV, one row per extension: how many files
and versions history holds, how many are already in the active copy, and the
delta that is sent for PII scanning.

  ext, group, repos
  total_files_in_pack        distinct paths ever in history
  files_at_head(active)      ... still in the active copy
  files_history_only         ... not in it any more
  versions                   distinct contents per file, summed
  versions_processed(in_active)
                             binary: versions identical to a file in the same
                             repo's active copy (already scanned)
  lfs_stubs(not_in_archive)  binary: Git LFS pointers, content not archived
  versions_to_send           binary: versions - processed - LFS stubs
  duplicates_removed         binary: versions_to_send that are exact copies
                             of another one (same file in several places or
                             repos) - sent once: versions_to_send - delta_dedup
  delta_dedup                binary: versions_to_send with identical copies
                             stored once - the files extracted;
                             text: files that still have lines to send
                             (needs --text-delta)
  delta_dedup_gb             binary: their size
  delta_lines                text: lines left to send (needs --text-delta)
  extracted_files, extracted_gb
                             binary: files actually on disk after
                             extract_versions.py (needs --extraction)
  note

and a total row. Reads only CSVs / manifests the other scripts wrote:

  <ext_versions>/by_extension.csv            extension_versions.py
  <text delta>/_state/*/*/manifest.csv       file_delta.py (text track)
  <extraction>/by_extension.csv              extract_versions.py

Usage:
    python3 extension_delta_summary.py /data/workarea/ext_versions \\
        --out /data/workarea/extension_delta_summary.csv
    # later, with the text delta and the extraction result
    python3 extension_delta_summary.py /data/workarea/ext_versions \\
        --text-delta /data/workarea/text9_extract_delta \\
        --extraction /data/workarea/binary_versions \\
        --out /data/workarea/extension_delta_summary.csv
"""

import argparse
import csv
import os
import sys
from collections import Counter

from ext_versions_analysis import read_text_delta

HEADER = ["ext", "group", "repos", "total_files_in_pack",
          "files_at_head(active)", "files_history_only", "versions",
          "versions_processed(in_active)", "lfs_stubs(not_in_archive)",
          "versions_to_send", "duplicates_removed", "delta_dedup",
          "delta_dedup_gb", "delta_lines",
          "extracted_files", "extracted_gb", "note"]
SUMMED = HEADER[3:16]


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build(counts_dir, text_delta=None, extraction=None):
    """-> rows (lists, HEADER order) incl. the total row."""
    counts = read_csv(os.path.join(counts_dir, "by_extension.csv"))
    tdelta = Counter()
    tlines = Counter()
    if text_delta:
        for (_o, _r, e), c in read_text_delta(text_delta).items():
            tdelta[e] += c["text_files_with_delta"]
            tlines[e] += c["text_lines_kept"]
    extracted = {}
    if extraction:
        extracted = {r["ext"]: r for r in
                     read_csv(os.path.join(extraction, "by_extension.csv"))}

    rows = []
    for r in counts:
        e, binary = r["ext"], r["group"] == "binary"
        versions = int(r["versions_per_file"])
        row = dict.fromkeys(HEADER, "")
        row.update({"ext": e, "group": r["group"], "repos": int(r["repos"]),
                    "total_files_in_pack": int(r["files_in_history"]),
                    "files_at_head(active)": int(r["files_at_head"]),
                    "files_history_only": int(r["files_history_only"]),
                    "versions": versions})
        if binary:
            lfs = int(r["lfs_stub_versions"])
            send = int(r["versions_to_send"])
            dedup = int(r["versions_to_send_all_repos"])
            row.update({"versions_processed(in_active)": versions - lfs - send,
                        "lfs_stubs(not_in_archive)": lfs,
                        "versions_to_send": send,
                        "duplicates_removed": send - dedup,
                        "delta_dedup": dedup,
                        "delta_dedup_gb": float(r["gb_to_send_all_repos"])})
            x = extracted.get(e)
            if x:
                row["extracted_files"] = int(x["files_on_disk"])
                row["extracted_gb"] = float(x["gb_on_disk"])
            elif extraction:
                row["extracted_files"], row["extracted_gb"] = 0, 0.0
        elif text_delta:
            row["delta_dedup"] = tdelta[e]
            row["delta_lines"] = tlines[e]
            row["note"] = "text: delta = files with lines left to send"
        else:
            row["note"] = "text: delta in lines - rerun with --text-delta " \
                          "after the text track"
        rows.append([row[h] for h in HEADER])

    total = ["total", "", ""]
    for i, h in enumerate(SUMMED, 3):
        # delta_dedup is files for binary and text alike, but only the
        # binary ones are the extracted unique files: total those
        vals = [r[i] for r in rows if isinstance(r[i], (int, float))
                and (h not in ("delta_dedup", "delta_dedup_gb")
                     or r[1] == "binary")]
        s = sum(vals) if vals else ""
        total.append(round(s, 3) if isinstance(s, float) else s)
    total.append("files / versions: all extensions; delta_dedup: binary "
                 "only; delta_lines: text")
    rows.append(total)
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("counts", help="extension_versions.py --out folder")
    ap.add_argument("--text-delta", metavar="DIR",
                    help="file_delta.py output of the text track")
    ap.add_argument("--extraction", metavar="DIR",
                    help="extract_versions.py --out folder")
    ap.add_argument("--out", help="CSV to write (default: "
                                  "<counts>/extension_delta_summary.csv)")
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(args.counts, "by_extension.csv")):
        sys.exit("no by_extension.csv in " + args.counts)
    for d in (args.text_delta,):
        if d and not os.path.isdir(os.path.join(d, "_state")):
            sys.exit("no _state folder under " + d)
    if args.extraction and not os.path.isfile(
            os.path.join(args.extraction, "by_extension.csv")):
        sys.exit("no by_extension.csv in %s - run extract_versions.py "
                 "--combine-only there first" % args.extraction)
    rows = build(args.counts, args.text_delta, args.extraction)
    out = args.out or os.path.join(args.counts, "extension_delta_summary.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)
    t = rows[-1]
    print("%d extension(s) -> %s" % (len(rows) - 1, out))
    print("binary: %s versions to send, %s duplicates removed, %s unique "
          "files, %s GB" % tuple(f"{t[i]:,}" if t[i] != "" else "-"
                                 for i in (9, 10, 11, 12)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
