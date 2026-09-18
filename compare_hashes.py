#!/usr/bin/env python3
"""
Compare file hashes between two inventory CSVs.

Both CSVs are expected to have: repo,file_path,hash,size_bytes,error

Adds a `present` column to csv1 indicating whether each hash exists in csv2,
prints a summary (overall + per repo), and writes the annotated CSVs out.

Usage:
    python compare_hashes.py archived.csv active.csv
    python compare_hashes.py archived.csv active.csv --by-repo --exclude-git
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

COLS = ["repo", "file_path", "hash", "size_bytes", "error"]
GIT_SUFFIXES = (".pack", ".idx", ".rev")


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        sys.exit(f"{path}: missing columns {missing}. Found: {list(df.columns)}")

    # normalise so casing / whitespace differences don't create false mismatches
    df["hash"] = df["hash"].str.strip().str.lower()
    df["repo"] = df["repo"].str.strip()
    df["file_path"] = df["file_path"].str.strip()
    df["size_bytes"] = pd.to_numeric(df["size_bytes"], errors="coerce")
    df["__source"] = path.name
    return df


def is_git_internal(path_series: pd.Series) -> pd.Series:
    """Rows that live inside .git/ or are pack/idx artefacts."""
    p = path_series.str.replace("\\", "/", regex=False)
    in_git_dir = p.str.contains(r"(^|/)\.git(/|$)", regex=True, na=False)
    packish = p.str.lower().str.endswith(GIT_SUFFIXES)
    return in_git_dir | packish


def split_usable(df: pd.DataFrame):
    """Separate rows that have a real hash from rows that errored / are blank."""
    bad = (df["hash"] == "") | (df["error"].str.strip() != "")
    return df[~bad].copy(), df[bad].copy()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv1", type=Path, help="source CSV (e.g. archived)")
    ap.add_argument("csv2", type=Path, help="CSV to look hashes up in (e.g. active)")
    ap.add_argument("--by-repo", action="store_true",
                    help="match on (repo, hash) instead of hash alone")
    ap.add_argument("--exclude-git", action="store_true",
                    help="drop .git internals (.pack/.idx and anything under .git/) before comparing")
    ap.add_argument("--outdir", type=Path, default=Path("."),
                    help="where to write the annotated CSVs (default: cwd)")
    args = ap.parse_args()

    df1, df2 = load(args.csv1), load(args.csv2)

    if args.exclude_git:
        n1, n2 = len(df1), len(df2)
        df1 = df1[~is_git_internal(df1["file_path"])]
        df2 = df2[~is_git_internal(df2["file_path"])]
        print(f"excluded git internals: csv1 -{n1 - len(df1)}, csv2 -{n2 - len(df2)}")

    good1, bad1 = split_usable(df1)
    good2, bad2 = split_usable(df2)

    if args.by_repo:
        lookup = set(zip(good2["repo"], good2["hash"]))
        found = pd.Series(list(zip(good1["repo"], good1["hash"])),
                          index=good1.index).isin(lookup)
    else:
        lookup = set(good2["hash"])
        found = good1["hash"].isin(lookup)

    good1["present"] = found.map({True: "present", False: "not present"})
    bad1["present"] = "unknown (no hash / error)"
    out1 = pd.concat([good1, bad1]).sort_index().drop(columns="__source")

    # ---- summary -------------------------------------------------------
    n_present = int(found.sum())
    n_missing = int((~found).sum())
    total = len(good1)
    key = "(repo, hash)" if args.by_repo else "hash"

    print(f"\nmatching on {key}")
    print(f"csv1: {len(df1):>8} rows  ({total} with usable hash, {len(bad1)} skipped)")
    print(f"csv2: {len(df2):>8} rows  ({len(good2)} with usable hash, {len(bad2)} skipped)")
    print(f"\npresent in csv2     : {n_present:>8}  ({n_present / total:.1%})" if total else "")
    print(f"not present in csv2 : {n_missing:>8}  ({n_missing / total:.1%})" if total else "")
    print(f"unique hashes csv1  : {good1['hash'].nunique():>8}")
    print(f"unique hashes csv2  : {good2['hash'].nunique():>8}")

    per_repo = (good1.assign(_p=found.astype(int))
                     .groupby("repo")
                     .agg(files=("hash", "size"),
                          present=("_p", "sum")))
    per_repo["not_present"] = per_repo["files"] - per_repo["present"]
    per_repo["pct_present"] = (per_repo["present"] / per_repo["files"] * 100).round(1)
    # print("\nper repo:")
    # print(per_repo.to_string())

    # reverse direction: what's in csv2 but not csv1
    if args.by_repo:
        rev = ~pd.Series(list(zip(good2["repo"], good2["hash"])),
                         index=good2.index).isin(set(zip(good1["repo"], good1["hash"])))
    else:
        rev = ~good2["hash"].isin(set(good1["hash"]))
    print(f"\nin csv2 but not csv1: {int(rev.sum())}")

    # ---- outputs -------------------------------------------------------
    args.outdir.mkdir(parents=True, exist_ok=True)
    annotated = args.outdir / f"{args.csv1.stem}_annotated.csv"
    missing_only = args.outdir / f"{args.csv1.stem}_not_present.csv"
    only_in_2 = args.outdir / f"{args.csv2.stem}_only.csv"

    out1.to_csv(annotated, index=False)
    good1[~found].drop(columns="__source").to_csv(missing_only, index=False)
    good2[rev].drop(columns="__source").to_csv(only_in_2, index=False)

    print(f"\nwrote {annotated}")
    print(f"wrote {missing_only}")
    print(f"wrote {only_in_2}")


if __name__ == "__main__":
    main()