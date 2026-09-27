#!/usr/bin/env python3
"""
history_only_analysis.py - analyse history_only_files.csv (from
history_only_files.py) and write a Markdown report: for each analysis a
one-line explanation, the exact code that produced it, and its result.

Analyses: overview, repositories, reasons, orgs, file types, removals by
year and month, first-added years, file lifetimes, versions, renames and
folder moves, merge-attributed removals, concentration, the Motif
repositories, sensitive-looking files, removals mentioning secrets,
short-lived files, groups of copied repositories, third-party code - and,
when their outputs are given, the share of each repo that is history-only
(--file-counts) and the unprocessed lines left per repo (--delta).

Detailed lists go to CSV files next to the report:
  sensitive_history_only.csv, secret_removal_commits.csv,
  short_lived_files.csv, copy_groups.csv, own_code_by_repo.csv

Only pandas is needed; the CSV is read as text, with bad bytes replaced.

Usage:
    python3 history_only_analysis.py /data/workarea/history_only/history_only_files.csv \\
        --out /data/workarea/history_only/analysis \\
        --file-counts /data/workarea/file_counts/files_per_repo.csv \\
        --delta /data/workarea/full_extract_delta
"""

import argparse
import glob
import inspect
import json
import os
import sys
import textwrap
import time

import pandas as pd

TOTAL_REPOS = None        # set from --repos-analysed, for the repository section


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def md_table(obj, max_rows=25):
    """DataFrame / Series -> Markdown table (no tabulate needed)."""
    if isinstance(obj, pd.Series):
        obj = obj.to_frame(obj.name or "value")
    df = obj.head(max_rows)
    idx_names = [n or "" for n in (df.index.names if df.index.nlevels > 1
                                   else [df.index.name])]
    show_idx = not isinstance(df.index, pd.RangeIndex)
    cols = (idx_names if show_idx else []) + [str(c) for c in df.columns]

    def cell(v):
        if isinstance(v, (int,)) and not isinstance(v, bool):
            return f"{v:,}"
        if hasattr(v, "item") and not isinstance(v, str):   # numpy scalars
            v = v.item()
        if isinstance(v, float):
            if pd.isna(v):
                return ""
            return f"{int(v):,}" if v.is_integer() else f"{v:,.1f}"
        if isinstance(v, int) and not isinstance(v, bool):
            return f"{v:,}"
        return str(v).replace("|", "/").replace("\n", " ")[:90]

    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for idx, row in df.iterrows():
        left = (list(idx) if isinstance(idx, tuple) else [idx]) if show_idx else []
        lines.append("| " + " | ".join(cell(v) for v in left + list(row)) + " |")
    if len(obj) > max_rows:
        lines.append("\n_%s more row(s) not shown._" % f"{len(obj) - max_rows:,}")
    return "\n".join(lines)


def with_share(s, total=None, name="files"):
    total = total if total is not None else s.sum()
    t = s.to_frame(name)
    t["share %"] = (t[name] / total * 100).round(1) if total else 0.0
    return t


SECTIONS = []


def section(title, why):
    """Register an analysis: fn(ctx) -> Markdown body."""
    def deco(fn):
        SECTIONS.append((title, why, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# analyses - each gets ctx (df, own, vend, out, args) and returns Markdown
# --------------------------------------------------------------------------

@section("Overview", "How many history-only files there are, and how many are "
         "the teams' own code versus third-party code.")
def overview(c):
    df, own, vend = c["df"], c["own"], c["vend"]
    t = pd.Series({"history-only files": len(df), "own code": len(own),
                   "third-party (vendored)": len(vend)}, name="files")
    return md_table(with_share(t, len(df)))


@section("Repositories", "How many repositories have history-only files at "
         "all, counted by org and repo together.")
def repositories(c):
    df, own = c["df"], c["own"]
    any_ = df[["org", "repo"]].drop_duplicates().shape[0]
    own_ = own[["org", "repo"]].drop_duplicates().shape[0]
    rows = {"with history-only files": any_,
            "... with the teams' own files": own_,
            "... with third-party code only": any_ - own_}
    if TOTAL_REPOS:
        rows = {"analysed": TOTAL_REPOS, **rows,
                "with no history-only files": TOTAL_REPOS - any_}
    return md_table(pd.Series(rows, name="repositories"))


@section("Why the teams' own files are gone", "deleted, renamed, or branch "
         "unknown ('orphaned': in history, but the archived copies hold no "
         "branch names).")
def reasons(c):
    own = c["own"]
    return md_table(with_share(own.reason.value_counts(), len(own)))


@section("By organisation", "History-only files per GitHub organisation, "
         "own code versus third-party.")
def by_org(c):
    t = c["df"].groupby(["org", "category"]).size().unstack(fill_value=0)
    t["total"] = t.sum(axis=1)
    return md_table(t.sort_values("total", ascending=False))


@section("File types", "The most common extensions among the teams' own "
         "history-only files.")
def file_types(c):
    own = c["own"]
    ext = own.path.str.extract(r"\.([^./]+)$")[0].str.lower().fillna("(none)")
    return md_table(with_share(ext.value_counts(), len(own)), 20)


@section("Removals by year", "Own files removed each year, split by deleted "
         "and renamed (files with branch unknown have no removal date).")
def removals_by_year(c):
    own = c["own"]
    r = own[own.removed_date != ""]
    t = r.assign(year=r.removed_date.str[:4]).groupby(["year", "reason"]) \
        .size().unstack(fill_value=0)
    t["total"] = t.sum(axis=1)
    return md_table(t, 40)


@section("Removals by month (last 24 months)", "Spots large clean-up "
         "campaigns: months with unusually many removals.")
def removals_by_month(c):
    own = c["own"]
    m = own[own.removed_date != ""].removed_date.str[:7].value_counts() \
        .sort_index().tail(24)
    return md_table(m.rename("files removed"), 24)


@section("When the files were first added", "Year each own history-only "
         "file first appeared.")
def first_added(c):
    own = c["own"]
    y = own[own.first_date != ""].first_date.str[:4].value_counts().sort_index()
    return md_table(with_share(y, len(own)), 40)


@section("How long files lived", "Days between a file first being added and "
         "being removed (own files with both dates).")
def lifetime(c):
    own = c["own"]
    d0 = pd.to_datetime(own.first_date, utc=True, errors="coerce")
    d1 = pd.to_datetime(own.removed_date, utc=True, errors="coerce")
    life = (d1 - d0).dt.days.dropna()
    c["life"] = life
    stats = life.describe(percentiles=[.25, .5, .75, .9]).round(0) \
        .rename({"count": "files", "50%": "median", "mean": "mean"})
    buckets = pd.cut(life, [-1, 1, 7, 30, 180, 365, 730, 1825, 10 ** 6],
                     labels=["within 1 day", "<1 week", "<1 month",
                             "<6 months", "<1 year", "1-2 years",
                             "2-5 years", "5+ years"]).value_counts().sort_index()
    return ("**Days, summary**\n\n" + md_table(stats.rename("days")) +
            "\n\n**Buckets**\n\n" + md_table(with_share(buckets, len(life))))


@section("Versions per file", "How many commits touched each own "
         "history-only file: created once and removed, or edited often.")
def versions(c):
    own = c["own"]
    v = pd.to_numeric(own.versions, errors="coerce")
    b = pd.cut(v, [0, 1, 2, 5, 10, 50, 10 ** 9],
               labels=["1", "2", "3-5", "6-10", "11-50", "50+"]) \
        .value_counts().sort_index()
    return md_table(with_share(b, len(own)))


@section("Renamed files", "Whether the new name exists in the active "
         "repositories today, and the most common folder moves.")
def renames(c):
    own = c["own"]
    ren = own[own.reason == "renamed"]
    exists = ren.renamed_to_exists_today.replace("", "unknown").value_counts()
    moves = (ren.path.str.rsplit("/", n=1).str[0].where(ren.path.str.contains("/"), ".")
             + "  ->  " +
             ren.renamed_to.str.rsplit("/", n=1).str[0].where(
                 ren.renamed_to.str.contains("/"), "."))
    moves = moves[moves.str.split("  ->  ").str[0] != moves.str.split("  ->  ").str[1]]
    return ("**New name exists today?**\n\n" + md_table(with_share(exists, len(ren)))
            + "\n\n**Most common folder moves**\n\n"
            + md_table(moves.value_counts().rename("files"), 20))


@section("Removals recorded on a merge", "Share of removals where the "
         "removing commit is a merge: the file was removed elsewhere and the "
         "merge carried that removal over.")
def merges(c):
    own = c["own"]
    rem = own[own.removed_commit != ""]
    is_merge = rem.removed_message.str.match(r"(?i)^(merge|merged pr)")
    t = pd.Series({"removals": len(rem), "on a merge commit": int(is_merge.sum()),
                   "distinct removing commits": rem.removed_commit.nunique()},
                  name="count")
    return md_table(t) + "\n\n%.1f%% of removals are on a merge commit." % (
        is_merge.mean() * 100 if len(rem) else 0)


@section("Concentration", "How much of the own-code history-only content "
         "sits in the largest repositories.")
def concentration(c):
    own = c["own"]
    per = own.groupby(["org", "repo"]).size().sort_values(ascending=False)
    per.to_csv(os.path.join(c["out"], "own_code_by_repo.csv"), header=["files"])
    rows = {f"top {n} repos": per.head(n).sum() for n in (10, 50, 100, 500)}
    t = with_share(pd.Series(rows), len(own))
    return md_table(t) + "\n\n**Top 20 repositories (own code)**\n\n" + \
        md_table(with_share(per, len(own)), 20)


@section("Motif design-system repositories", "Their share of the history-"
         "only files (generated icon and component files).")
def motif(c):
    df = c["df"]
    m = df[df.repo.str.contains("motif", case=False)]
    per = m.groupby(["org", "repo"]).size().sort_values(ascending=False)
    return md_table(with_share(per, len(df))) + \
        "\n\nTotal: %s files, %.1f%% of all history-only files." % (
            f"{len(m):,}", len(m) / len(df) * 100 if len(df) else 0)


@section("Sensitive-looking files", "History-only files whose names suggest "
         "certificates, keystores, credentials or configuration. A file "
         "deleted from a repository is still readable in its history. Full "
         "list: sensitive_history_only.csv.")
def sensitive(c):
    own = c["own"]
    name = own.path.str.rsplit("/", n=1).str[-1].str.lower()
    pat = (r"\.pem$|\.jks$|\.pfx$|\.p12$|\.key$|appsettings.*\.json$|web\.config$"
           r"|app\.config$|\.properties$|\.ini$|secret|credential|password|"
           r"connectionstring|\.env$")
    risky = own[name.str.contains(pat, regex=True)]
    risky.to_csv(os.path.join(c["out"], "sensitive_history_only.csv"), index=False)
    kind = name[risky.index].str.extract(
        r"(\.pem|\.jks|\.pfx|\.p12|\.key|appsettings|web\.config|app\.config|"
        r"\.properties|\.ini|secret|credential|password|connectionstring|\.env)")[0]
    t = kind.value_counts()
    per = risky.groupby(["org", "repo"]).size().sort_values(ascending=False)
    return ("%s files in %s repositories.\n\n**By kind**\n\n" % (
        f"{len(risky):,}", f"{len(per):,}") + md_table(t.rename("files"))
        + "\n\n**Top repositories**\n\n" + md_table(per.rename("files"), 15))


@section("Removals mentioning secrets", "Files removed by a commit whose "
         "message mentions passwords, keys, tokens or credentials - often a "
         "sign they were committed by mistake. Full list: "
         "secret_removal_commits.csv.")
def secret_removals(c):
    own = c["own"]
    rem = own[own.removed_message != ""]
    hits = rem[rem.removed_message.str.contains(
        r"(?i)secret|password|passwd|credential|api[ _-]?key|token|private key|"
        r"sensitive|\.env\b|connection ?string", regex=True)]
    g = hits.groupby(["org", "repo", "removed_commit", "removed_date",
                      "removed_message"]).size().sort_values(ascending=False) \
        .rename("files").reset_index()
    g.to_csv(os.path.join(c["out"], "secret_removal_commits.csv"), index=False)
    g["removed_commit"] = g.removed_commit.str[:10]
    g["removed_date"] = g.removed_date.str[:10]
    g["removed_message"] = g.removed_message.str[:60]
    return "%s files removed by %s such commits.\n\n" % (
        f"{len(hits):,}", f"{len(g):,}") + md_table(g, 20)


@section("Short-lived files", "Own files removed within a day of being "
         "added - often accidental commits. Full list: short_lived_files.csv.")
def short_lived(c):
    own = c["own"]
    d0 = pd.to_datetime(own.first_date, utc=True, errors="coerce")
    d1 = pd.to_datetime(own.removed_date, utc=True, errors="coerce")
    short = own[(d1 - d0) <= pd.Timedelta(days=1)]
    short.to_csv(os.path.join(c["out"], "short_lived_files.csv"), index=False)
    per = short.groupby(["org", "repo"]).size().sort_values(ascending=False)
    return "%s files in %s repositories.\n\n" % (
        f"{len(short):,}", f"{len(per):,}") + md_table(per.rename("files"), 15)


@section("Groups of copied repositories", "Repositories that share the same "
         "removal commit are copies or forks of each other; their history is "
         "counted once per copy. Full list: copy_groups.csv.")
def copy_groups(c):
    own = c["own"]
    rem = own[own.removed_commit != ""]
    per_commit = rem.groupby("removed_commit").agg(
        repos=("repo", "nunique"), files=("path", "size"))
    shared = per_commit[per_commit.repos > 1]
    sub = rem[rem.removed_commit.isin(shared.index)]
    groups = sub.groupby("removed_commit").repo.apply(
        lambda s: " | ".join(sorted(set(s))))
    files = sub.groupby("removed_commit").size()
    g = pd.DataFrame({"group": groups, "files": files}).groupby("group") \
        .files.sum().sort_values(ascending=False)
    g.to_csv(os.path.join(c["out"], "copy_groups.csv"), header=["file_rows"])
    return ("%s removal commits appear in 2+ repositories, covering %s file "
            "rows.\n\n" % (f"{len(shared):,}", f"{int(shared.files.sum()):,}")
            + md_table(g.rename("file rows"), 20))


@section("Third-party code", "Vendored history-only files by folder and by "
         "repository.")
def vendored(c):
    vend = c["vend"]
    return ("**By folder**\n\n" + md_table(with_share(
        vend.vendor_folder.value_counts(), len(vend)))
        + "\n\n**Top repositories**\n\n" + md_table(
            vend.groupby(["org", "repo"]).size().sort_values(ascending=False)
            .rename("files"), 15))


@section("History-only share per repository", "Share of each repository's "
         "extracted files that exist only in history (repos with 100+ files; "
         "needs --file-counts).")
def history_share(c):
    fc = c["args"].file_counts
    if not fc or not os.path.isfile(fc):
        return "_Skipped: no --file-counts file given._"
    fr = pd.read_csv(fc, dtype={"org": str, "repo": str})
    ho = c["df"].groupby(["org", "repo"]).size().rename("history_only").reset_index()
    m = fr.merge(ho, on=["org", "repo"], how="left").fillna({"history_only": 0})
    m["history_only"] = m.history_only.astype(int)
    m["pct history-only"] = (m.history_only / m.written.clip(lower=1) * 100).round(1)
    m = m[m.written >= 100].sort_values("pct history-only", ascending=False)
    return md_table(m.set_index(["org", "repo"])[
        ["written", "history_only", "pct history-only"]], 20)


@section("Unprocessed content per repository", "Lines left after the delta "
         "(history minus already-processed content), per repository (needs "
         "--delta).")
def unprocessed(c):
    dd = c["args"].delta
    if not dd or not os.path.isdir(dd):
        return "_Skipped: no --delta folder given._"
    rows = []
    for p in glob.glob(os.path.join(dd, "_state", "*", "*", "done.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                rows.append(json.load(fh))
        except (OSError, ValueError):
            pass
    d = pd.DataFrame(rows)[["org", "repo", "files_written", "lines_history",
                            "lines_removed", "lines_kept"]]
    d["pct kept"] = (d.lines_kept / d.lines_history.clip(lower=1) * 100).round(1)
    d = d.sort_values("lines_kept", ascending=False)
    top = d.head(20).lines_kept.sum() / max(d.lines_kept.sum(), 1) * 100
    return ("Top 20 repositories hold %.1f%% of all unprocessed lines.\n\n" % top
            + md_table(d.set_index(["org", "repo"]), 20))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    global TOTAL_REPOS
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="history_only_files.csv")
    ap.add_argument("--out", required=True, help="folder for the report and CSVs")
    ap.add_argument("--file-counts", help="files_per_repo.csv from "
                                          "explain_file_counts.py (optional)")
    ap.add_argument("--delta", help="file_delta.py output folder (optional)")
    ap.add_argument("--repos-analysed", type=int, default=13631,
                    help="repositories analysed in total (default 13631)")
    ap.add_argument("--no-code", action="store_true",
                    help="leave the code snippets out of the report")
    args = ap.parse_args()
    TOTAL_REPOS = args.repos_analysed

    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()
    print("reading %s ..." % args.csv, flush=True)
    df = pd.read_csv(args.csv, dtype=str, keep_default_na=False,
                     encoding_errors="replace", low_memory=False)
    ctx = {"df": df, "own": df[df.category == "own_code"],
           "vend": df[df.category == "vendored"], "out": args.out, "args": args}
    print("loaded %s rows in %.0fs" % (f"{len(df):,}", time.time() - t0), flush=True)

    md = ["# History-only files: analysis", "",
          "Source: `%s` (%s rows). Generated %s." % (
              args.csv, f"{len(df):,}", time.strftime("%Y-%m-%d %H:%M")), "",
          "Each section gives what it shows, the code that produced it, and "
          "the result. `df` is the CSV read as text; `own` / `vend` are its "
          "own-code / vendored rows.", "", "## Contents", ""]
    md += ["%d. [%s](#%s)" % (i, t, t.lower().replace(" ", "-")
                              .replace("'", "").replace("(", "").replace(")", "")
                              .replace(",", "").replace(":", ""))
           for i, (t, _w, _f) in enumerate(SECTIONS, 1)]
    for i, (title, why, fn) in enumerate(SECTIONS, 1):
        t1 = time.time()
        print("  [%d/%d] %s" % (i, len(SECTIONS), title), flush=True)
        try:
            body = fn(ctx)
        except Exception as exc:                  # noqa: BLE001 - keep going
            body = "_Failed: %s: %s_" % (type(exc).__name__, exc)
        md += ["", "## %s" % title, "", why, ""]
        if not args.no_code:
            src = inspect.getsource(fn)
            code = "\n".join(src.splitlines()[_body_start(src):])
            md += ["<details><summary>Code</summary>", "", "```python",
                   textwrap.dedent(code), "```", "", "</details>", ""]
        md += [body, "", "_(%.0fs)_" % (time.time() - t1)]

    report = os.path.join(args.out, "history_only_analysis.md")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    print("-> %s  (%.0fs)" % (report, time.time() - t0))
    return 0


def _body_start(src):
    """Index of the `def` line in a decorated function's source, so the
    report shows the function without its @section decorator."""
    lines = src.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("def "):
            return i
    return 0


if __name__ == "__main__":
    sys.exit(main())
