#!/usr/bin/env python3
"""
repo_extension_summary.py - one row per repo from the file_summary.csv that
file_history_for_list.py writes: how many files, how many commits touched
them, and how those files split across a fixed list of extensions.

Output columns:

    org, repo
    files                 distinct files with history (status found)
    commits_touched       sum of commits_touched over those files - a commit
                          that changed three files counts three times
    not_found             listed paths with no history, excluding .git
                          internals (hooks, HEAD, config - never committed)
    repo_status           ok / repo_missing / repo_error / timeout / bad_row
                          when EVERY row of the repo had that status
    listed_files          files whose extension IS in the list
    listed_commits        commits_touched of those files
    files_bucket          which --file-buckets range listed_files falls in
    commits_bucket        which --commit-buckets range listed_commits falls in
    <ext>, <ext>_commits  files with that extension and their commits_touched,
                          one pair per entry in the extension list (default:
                          EXTENSIONS below)
    other, other_commits  files whose extension is not in the list
    other_extensions      what those were, most common first: "dll:12|png:7"

A last row, org = "(all)", totals every column.

Also written next to --out:

  <out>_distribution.csv   repos, files and commits per bucket, once bucketed
                           by file count and once by commit count
  <out>.html               the same as bar charts (repos per bucket, files /
                           commits per bucket, files / commits per extension),
                           self-contained - open it in any browser

Buckets are on the LISTED extensions by default (the files you are looking
for); --bucket-on all buckets on every file instead. Repos that failed as a
whole (repo_missing, repo_error, timeout, bad_row) are left out of the
buckets and counted separately.

Extensions are matched on the file name, case-insensitively. A dotfile such as
.gitignore counts as the extension "gitignore". Rows repeating the same
(org, repo, matched_path) - duplicate input rows - are counted once.

Usage:
    python3 repo_extension_summary.py file_summary.csv --out repo_summary.csv
    python3 repo_extension_summary.py file_summary.csv --out repo_summary.csv \\
        --extensions json,cs,ts --file-buckets 10,100,1000
"""

import argparse
import csv
import html
import os
import sys
from collections import Counter, defaultdict

from analyse_file_summary import is_git_internal

EXTENSIONS = ["json", "csv", "xml", "sql", "py", "txt", "cs", "yaml", "md",
              "yml", "html", "ts", "java", "tsx", "properties", "log", "bat",
              "js", "jsx", "ps1", "pem", "jks", "ini", "manifest", "sln",
              "xhtml", "gitignore", "sh", "css", "tsv", "config", "htm"]

NEEDED = ["org", "repo", "relpath", "filename", "matched_path", "status",
          "commits_touched"]

# upper edges, inclusive: 0 | 1-10 | 11-50 | ... | 10001+
FILE_BUCKETS = [0, 10, 50, 100, 500, 1000, 5000, 10000]
COMMIT_BUCKETS = [0, 10, 100, 500, 1000, 5000, 10000, 50000]


def ext_key(name):
    """'a/b/App.Config' -> 'config', '.gitignore' -> 'gitignore',
    'Makefile' -> '' (no extension)."""
    leaf = (name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    if leaf.startswith(".") and leaf.count(".") == 1:
        return leaf[1:]
    stem, dot, e = leaf.rpartition(".")
    return e if dot and stem else ""


def load_extensions(spec):
    """--extensions: a comma list, or a file with one extension per line."""
    if not spec:
        return list(EXTENSIONS)
    if os.path.isfile(spec):
        with open(spec, encoding="utf-8") as fh:
            items = fh.read().split()
    else:
        items = spec.split(",")
    out = []
    for e in items:
        e = e.strip().lstrip(".").lower()
        if e and e not in out:
            out.append(e)
    return out


class Repo:
    __slots__ = ("files", "commits", "not_found", "statuses", "ext_files",
                 "ext_commits", "other_exts")

    def __init__(self):
        self.files = self.commits = self.not_found = 0
        self.statuses = Counter()
        self.ext_files = Counter()
        self.ext_commits = Counter()
        self.other_exts = Counter()




def parse_edges(spec, default):
    """--file-buckets / --commit-buckets: '10,50,100' -> [0, 10, 50, 100]."""
    if not spec:
        return list(default)
    try:
        edges = sorted({int(x) for x in spec.split(",") if x.strip()})
    except ValueError:
        sys.exit("bucket edges must be whole numbers: " + spec)
    return edges if edges and edges[0] == 0 else [0] + edges


def bucket_labels(edges):
    """[0, 10, 50] -> ['0', '1-10', '11-50', '51+']"""
    labels = ["0" if edges[0] == 0 else "0-%d" % edges[0]]
    labels += ["%d-%d" % (lo + 1, hi) for lo, hi in zip(edges, edges[1:])]
    return labels + ["%d+" % (edges[-1] + 1)]


def bucket_index(n, edges):
    for i, e in enumerate(edges):
        if n <= e:
            return i
    return len(edges)


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --border: #e3e2dc;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77766f;
  --track: #eeede8; --series-1: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --border: #2e2e2c;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #939289;
    --track: #242423; --series-1: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-0: #111110; --surface-1: #1a1a19; --border: #2e2e2c;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #939289;
  --track: #242423; --series-1: #3987e5;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text-primary);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 28px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 36px 0 4px; }
.sub, .note { color: var(--text-secondary); margin: 0 0 12px; }
.note { font-size: 13px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
         gap: 12px; margin: 20px 0 8px; }
.tile { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 8px; padding: 12px 14px; }
.tile .k { color: var(--text-secondary); font-size: 12px; }
.tile .v { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
        gap: 16px; }
.card { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 8px; padding: 14px 16px; min-width: 0; }
.card h3 { font-size: 14px; margin: 0 0 10px; }
.row { display: grid; grid-template-columns: 96px 1fr; align-items: center;
       gap: 10px; padding: 3px 0; border-radius: 4px; outline: none; }
.row:hover, .row:focus-visible { background: var(--track); }
.lbl { color: var(--text-secondary); font-size: 12px; text-align: right;
       font-variant-numeric: tabular-nums; overflow: hidden;
       text-overflow: ellipsis; white-space: nowrap; }
.track { display: flex; align-items: center; min-width: 0; }
.bar { flex: none; height: 18px; background: var(--series-1);
       border-radius: 0 4px 4px 0; }
.val { margin-left: 6px; font-size: 12px; color: var(--text-primary);
       font-variant-numeric: tabular-nums; white-space: nowrap; }
.val span { color: var(--text-muted); }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--text-secondary); font-size: 12px; }
table { border-collapse: collapse; width: 100%; margin-top: 6px; font-size: 12px;
        font-variant-numeric: tabular-nums; }
th, td { padding: 3px 6px; border-bottom: 1px solid var(--border); text-align: right; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-secondary); font-weight: 500; }
#tip { position: fixed; pointer-events: none; display: none; z-index: 10;
       background: var(--surface-1); color: var(--text-primary);
       border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
       font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.18); white-space: pre; }
@media (max-width: 480px) { .row { grid-template-columns: 72px 1fr; } }
"""

JS = """
const tip = document.getElementById('tip');
function show(el, x, y) {
  tip.textContent = el.dataset.tip; tip.style.display = 'block';
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(x + 14, innerWidth - w - 8) + 'px';
  tip.style.top = Math.min(y + 14, innerHeight - h - 8) + 'px';
}
document.querySelectorAll('.row').forEach(el => {
  el.addEventListener('mousemove', e => show(el, e.clientX, e.clientY));
  el.addEventListener('mouseleave', () => tip.style.display = 'none');
  el.addEventListener('focus', () => { const r = el.getBoundingClientRect();
                                       show(el, r.left + 100, r.bottom); });
  el.addEventListener('blur', () => tip.style.display = 'none');
});
"""


def fmt(n):
    return f"{n:,}"


def pct(n, total):
    return (100.0 * n / total) if total else 0.0


def bar_chart(title, rows, unit):
    """rows: [(label, value, tooltip text)] - one horizontal bar each, with the
    value and its share printed after the bar end, and a table view below."""
    esc = html.escape
    total = sum(v for _l, v, _t in rows)
    top = max((v for _l, v, _t in rows), default=0) or 1
    out = ['<div class="card"><h3>%s</h3>' % esc(title),
           '<div role="img" aria-label="%s">' % esc(
               "%s: %s" % (title, "; ".join("%s %s" % (l, fmt(v))
                                            for l, v, _t in rows)))]
    for label, v, tip in rows:
        # leave room after the longest bar for its value label
        width = "calc((100%% - 110px) * %.5f)" % (v / top)
        out.append(
            '<div class="row" tabindex="0" data-tip="%s"><span class="lbl" '
            'title="%s">%s</span><span class="track"><span class="bar" '
            'style="width:%s"></span><span class="val">%s <span>%.1f%%</span>'
            '</span></span></div>'
            % (esc(tip), esc(label), esc(label), width, fmt(v), pct(v, total)))
    out.append("</div><details><summary>Table</summary><table><tr><th></th>"
               "<th>%s</th><th>share</th></tr>" % esc(unit))
    for label, v, _t in rows:
        out.append("<tr><td>%s</td><td>%s</td><td>%.1f%%</td></tr>"
                   % (esc(label), fmt(v), pct(v, total)))
    out.append("<tr><th>total</th><th>%s</th><th></th></tr></table></details>"
               "</div>" % fmt(total))
    return "\n".join(out)


def write_html(path, src, basis, dists, ext_rows, totals, skipped):
    """dists: {"files": [(label, repos, files, commits)], "commits": [...]}"""
    what = "listed extensions" if basis == "listed" else "all files"
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>Repo Extension Summary</title><style>%s</style></head>" % CSS,
             "<body><main><h1>Repo extension summary</h1>",
             "<p class='sub'>%s &middot; files and commits counted on %s</p>"
             % (html.escape(src), what)]
    tiles = [("Repos bucketed", totals["repos"]),
             ("Files (%s)" % what, totals["files"]),
             ("Commits touched (%s)" % what, totals["commits"]),
             ("Repos not bucketed", skipped)]
    parts.append("<div class='tiles'>" + "".join(
        "<div class='tile'><div class='k'>%s</div><div class='v'>%s</div></div>"
        % (html.escape(k), fmt(v)) for k, v in tiles) + "</div>")
    if skipped:
        parts.append("<p class='note'>Repos not bucketed failed as a whole "
                     "(repo_missing, repo_error, timeout, bad_row) - see "
                     "repo_status in the CSV.</p>")

    def tip(label, repos, files, commits, by):
        return ("%s %s\nrepos    %s  (%.1f%%)\nfiles    %s  (%.1f%%)\n"
                "commits  %s  (%.1f%%)"
                % (by, label, fmt(repos), pct(repos, totals["repos"]),
                   fmt(files), pct(files, totals["files"]),
                   fmt(commits), pct(commits, totals["commits"])))

    for key, by, heading, measure in (
            ("files", "files", "Repos bucketed by file count", "Files"),
            ("commits", "commits", "Repos bucketed by commits touched",
             "Commits touched")):
        rows = dists[key]
        idx = 2 if key == "files" else 3
        parts.append("<h2>%s</h2><p class='note'>Each repo falls in one "
                     "bucket by its number of %s. Hover a bar for all three "
                     "counts.</p><div class='pair'>" % (heading, by))
        parts.append(bar_chart("Repos per bucket",
                               [(r[0], r[1], tip(*r, by)) for r in rows],
                               "repos"))
        parts.append(bar_chart("%s per bucket" % measure,
                               [(r[0], r[idx], tip(*r, by)) for r in rows],
                               measure.lower()))
        parts.append("</div>")

    parts.append("<h2>Extensions</h2><p class='note'>Across every repo, "
                 "largest first; <i>other</i> is every extension not on the "
                 "list.</p><div class='pair'>")
    ext_tip = {e: "%s\nfiles    %s\ncommits  %s\nrepos    %s"
                  % (e, fmt(f), fmt(c), fmt(n)) for e, f, c, n in ext_rows}
    # "other" is pinned last: it is usually the biggest and would set the
    # scale the listed extensions are read against
    def ranked(i):
        return sorted(ext_rows, key=lambda r: (r[0] == "other", -r[i]))
    parts.append(bar_chart("Files per extension",
                           [(r[0], r[1], ext_tip[r[0]]) for r in ranked(1)],
                           "files"))
    parts.append(bar_chart("Commits touched per extension",
                           [(r[0], r[2], ext_tip[r[0]]) for r in ranked(2)],
                           "commits"))
    parts.append("</div></main><div id='tip' role='tooltip'></div>"
                 "<script>%s</script></body></html>" % JS)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="file_summary.csv from file_history_for_list.py")
    ap.add_argument("--out", default="repo_extension_summary.csv",
                    help="output CSV (default: repo_extension_summary.csv)")
    ap.add_argument("--extensions",
                    help="comma list, or a file with one per line "
                         "(default: the built-in list)")
    ap.add_argument("--bucket-on", choices=["listed", "all"], default="listed",
                    help="bucket repos on the listed extensions' files and "
                         "commits (default) or on every file")
    ap.add_argument("--file-buckets",
                    help="upper edges of the file-count buckets, comma "
                         "separated (default %s)"
                         % ",".join(map(str, FILE_BUCKETS[1:])))
    ap.add_argument("--commit-buckets",
                    help="upper edges of the commit buckets (default %s)"
                         % ",".join(map(str, COMMIT_BUCKETS[1:])))
    args = ap.parse_args()

    if not os.path.isfile(args.csv_in):
        sys.exit("not a file: " + args.csv_in)
    exts = load_extensions(args.extensions)
    wanted = set(exts)
    f_edges = parse_edges(args.file_buckets, FILE_BUCKETS)
    c_edges = parse_edges(args.commit_buckets, COMMIT_BUCKETS)
    f_labels, c_labels = bucket_labels(f_edges), bucket_labels(c_edges)
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

    repos = defaultdict(Repo)
    seen = set()                  # hash of (org, repo, path): 5M tuples is GBs
    rows = dups = 0
    with open(args.csv_in, newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            sys.exit("empty file: " + args.csv_in)
        missing = [c for c in NEEDED if c not in header]
        if missing:
            sys.exit("missing column(s) %s; found %s" % (missing, header))
        ix = {c: header.index(c) for c in NEEDED}
        width = len(header)

        for row in reader:
            rows += 1
            if len(row) < width:
                row = row + [""] * (width - len(row))
            org, repo = row[ix["org"]], row[ix["repo"]]
            st = row[ix["status"]]
            r = repos[(org, repo)]
            r.statuses[st] += 1
            path = row[ix["matched_path"]]
            if st == "not_found":
                if not is_git_internal(path, row[ix["relpath"]]):
                    r.not_found += 1
                continue
            if st != "found":
                continue

            key = hash((org, repo, path))
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            try:
                n = int(row[ix["commits_touched"]] or 0)
            except ValueError:
                n = 0
            r.files += 1
            r.commits += n
            e = ext_key(path or row[ix["filename"]])
            if e in wanted:
                r.ext_files[e] += 1
                r.ext_commits[e] += n
            else:
                r.ext_files[None] += 1
                r.ext_commits[None] += n
                r.other_exts[e or "(none)"] += 1
    seen.clear()

    def listed(r):
        return r.files - r.ext_files[None], r.commits - r.ext_commits[None]

    def basis(r):
        return listed(r) if args.bucket_on == "listed" else (r.files, r.commits)

    def status_of(r):
        only = list(r.statuses)
        return "ok" if r.files or r.not_found or len(only) != 1 else only[0]

    header = ["org", "repo", "files", "commits_touched", "not_found",
              "repo_status", "listed_files", "listed_commits",
              "files_bucket", "commits_bucket"]
    for e in exts:
        header += [e, e + "_commits"]
    header += ["other", "other_commits", "other_extensions"]

    def line(org, repo, r, status, fb, cb):
        out = [org, repo, r.files, r.commits, r.not_found, status,
               *listed(r), fb, cb]
        for e in exts + [None]:
            out += [r.ext_files[e], r.ext_commits[e]]
        out.append("|".join("%s:%d" % kv for kv in r.other_exts.most_common()))
        return out

    # per bucket: [repos, files, commits] on the chosen basis
    by_files = [[0, 0, 0] for _ in f_labels]
    by_commits = [[0, 0, 0] for _ in c_labels]
    total = Repo()
    ext_repos = Counter()
    skipped = 0
    with open(args.out, "w", newline="", encoding="utf-8",
              errors="surrogateescape") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for (org, repo), r in sorted(repos.items()):
            status = status_of(r)
            fb = cb = ""
            if status == "ok":
                nf, nc = basis(r)
                fi, ci = bucket_index(nf, f_edges), bucket_index(nc, c_edges)
                fb, cb = f_labels[fi], c_labels[ci]
                for dist, i in ((by_files, fi), (by_commits, ci)):
                    dist[i][0] += 1
                    dist[i][1] += nf
                    dist[i][2] += nc
            else:
                skipped += 1
            w.writerow(line(org, repo, r, status, fb, cb))
            total.files += r.files
            total.commits += r.commits
            total.not_found += r.not_found
            total.ext_files.update(r.ext_files)
            total.ext_commits.update(r.ext_commits)
            total.other_exts.update(r.other_exts)
            ext_repos.update(e for e, v in r.ext_files.items() if v)
        w.writerow(line("(all)", "", total, "", "", ""))

    stem = os.path.splitext(args.out)[0]
    dist_path = stem + "_distribution.csv"
    dists = {"files": [(l, *v) for l, v in zip(f_labels, by_files)],
             "commits": [(l, *v) for l, v in zip(c_labels, by_commits)]}
    sums = {"repos": sum(v[0] for v in by_files),
            "files": sum(v[1] for v in by_files),
            "commits": sum(v[2] for v in by_files)}
    with open(dist_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bucketed_by", "bucket", "repos", "repos_pct", "files",
                    "files_pct", "commits", "commits_pct"])
        for key in ("files", "commits"):
            for label, nr, nf, nc in dists[key]:
                w.writerow([key, label,
                            nr, "%.2f" % pct(nr, sums["repos"]),
                            nf, "%.2f" % pct(nf, sums["files"]),
                            nc, "%.2f" % pct(nc, sums["commits"])])

    ext_rows = [(e, total.ext_files[e], total.ext_commits[e], ext_repos[e])
                for e in exts]
    ext_rows.append(("other", total.ext_files[None], total.ext_commits[None],
                     ext_repos[None]))
    html_path = stem + ".html"
    write_html(html_path, os.path.basename(args.csv_in), args.bucket_on,
               dists, ext_rows, sums, skipped)

    lf, lc = listed(total)
    print("%s row(s), %d repo(s), %s file(s), %s commit(s) touched%s"
          % (fmt(rows), len(repos), fmt(total.files), fmt(total.commits),
             (", %s duplicate row(s) skipped" % fmt(dups)) if dups else ""))
    print("listed extensions: %s file(s), %s commit(s) touched"
          % (fmt(lf), fmt(lc)))
    print("-> %s\n-> %s\n-> %s" % (args.out, dist_path, html_path))


if __name__ == "__main__":
    main()
