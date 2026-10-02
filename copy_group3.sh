#!/usr/bin/env bash
# copy_group3.sh - copy group 3 to the file share and check it, keeping the
# tracking files OUT of the scanning scope (group 2's lesson: the scanner
# took every manifest and done.json in the folder as data).
#
#   data      $PDF_OUT  merged PDFs + unreadable versions  -> $DST/pdf/
#             $PKL_OUT  one .txt per .pkl path              -> $DST/pkl/
#             (nothing from _state/, _logs/, summary.csv, settings.json)
#   tracking  every _state/ (manifest.csv, pages.csv, done.json), the
#             summaries and the counts                     -> $DST_TRACKING/
#   checks    before: names the share cannot hold apart (differ only by
#             case) or at all (\ : * ? " < > |, trailing dot or space) -
#             listed in $REPORT_DIR/name_problems.csv; after: data file
#             counts source vs share (and content with CHECKSUM=1)
#
#   nohup ./copy_group3.sh > /data/workarea/group3/copy_group3.out 2>&1 &
#   ONLY=verify ./copy_group3.sh      only the checks
#   CHECKSUM=1 ./copy_group3.sh       compare contents, not size + date
#
# The share needs sudo: sudo bash -c 'cd /data/workarea/scripts && nohup
# ./copy_group3.sh > /data/workarea/group3/copy_group3.out 2>&1 &'
# Exit code: 0 = copied and checked, 1 = a copy or check failed (see log).

set -u

PDF_OUT="${PDF_OUT:-/data/workarea/group3/pdf_merged}"
PKL_OUT="${PKL_OUT:-/data/workarea/group3/pkl_text}"
COUNTS="${COUNTS:-/data/workarea/group3/counts}"
EXTRACT="${EXTRACT:-/data/workarea/group3/extract}"
DST="${DST:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group3}"
DST_TRACKING="${DST_TRACKING:-${DST}_tracking}"
REPORT_DIR="${REPORT_DIR:-/data/workarea/group3/copy_checks}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PYTHON="${PYTHON:-python3}"
OPTS=(-rt --no-perms --no-owner --no-group --partial)
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)
DATA_EXCL=(--exclude "/_state/" --exclude "/_logs/" --exclude "/summary.csv"
           --exclude "/settings.json" --exclude "*.tmp.*")

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

data_files() {   # folder -> its data files, relative, one per line
    ( cd "$1" && find . -mindepth 1 \( -path ./_state -o -path ./_logs \) -prune \
        -o -type f ! -name '*.tmp.*' ! -path ./summary.csv ! -path ./settings.json \
        -print | sed 's#^\./##' )
}

for d in "$PDF_OUT" "$PKL_OUT"; do
    [ -d "$d" ] || { say "ERROR: $d not found"; exit 1; }
done
mkdir -p "$REPORT_DIR"

# ------------------------------------------------------------ name checks
say "=== names the share may not keep apart"
{ data_files "$PDF_OUT" | sed 's#^#pdf/#'; data_files "$PKL_OUT" | sed 's#^#pkl/#'; } \
    > "$REPORT_DIR/data_files.txt"
"$PYTHON" - "$REPORT_DIR/data_files.txt" "$REPORT_DIR/name_problems.csv" <<'PY'
import csv, re, sys
from collections import defaultdict
paths = [l.rstrip("\n") for l in open(sys.argv[1], encoding="utf-8", errors="surrogateescape")]
by_low = defaultdict(list)
for p in paths:
    by_low[p.lower()].append(p)
bad = re.compile(r'[\\:*?"<>|]|[ .]$')
rows = [(p, "differs only by case from: " + " | ".join(x for x in v if x != p))
        for v in by_low.values() if len(v) > 1 for p in v]
rows += [(p, "character or ending the share does not allow")
         for p in paths if any(bad.search(part) for part in p.split("/"))]
with open(sys.argv[2], "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
    w = csv.writer(fh)
    w.writerow(["path", "problem"])
    w.writerows(rows)
print("  %d data files, %d with a name problem -> %s" % (len(paths), len(rows), sys.argv[2]))
PY

if [ "$ONLY" != verify ]; then
    mkdir -p "$DST/pdf" "$DST/pkl" "$DST_TRACKING" \
        || { say "ERROR: cannot create $DST - is the share mounted?"; exit 1; }
    say "free on the share: $(df -h "$DST" | awk 'NR==2 {print $4}'), data: $(du -sh --exclude=_state --exclude=_logs "$PDF_OUT" "$PKL_OUT" 2>/dev/null | awk '{print $1}' | paste -sd+)"
    # ------------------------------------------------------------- data
    say "=== data: pdf -> $DST/pdf"
    rsync "${OPTS[@]}" "${DATA_EXCL[@]}" "$PDF_OUT/" "$DST/pdf/" || fail=1
    say "=== data: pkl -> $DST/pkl"
    rsync "${OPTS[@]}" "${DATA_EXCL[@]}" "$PKL_OUT/" "$DST/pkl/" || fail=1
    # --------------------------------------------------------- tracking
    say "=== tracking -> $DST_TRACKING (not for scanning)"
    for pair in "$PDF_OUT:pdf" "$PKL_OUT:pkl"; do
        src="${pair%%:*}"; name="${pair#*:}"
        mkdir -p "$DST_TRACKING/$name"
        [ -d "$src/_state" ] && { rsync "${OPTS[@]}" "$src/_state/" "$DST_TRACKING/$name/_state/" || fail=1; }
        for f in summary.csv settings.json; do
            [ -f "$src/$f" ] && { cp "$src/$f" "$DST_TRACKING/$name/$f" || fail=1; }
        done
    done
    mkdir -p "$DST_TRACKING/extraction" "$DST_TRACKING/counts"
    for f in manifest.csv by_extension.csv summary.md; do
        [ -f "$EXTRACT/$f" ] && { cp "$EXTRACT/$f" "$DST_TRACKING/extraction/$f" || fail=1; }
    done
    for f in by_extension.csv by_repo.csv summary.md; do
        [ -f "$COUNTS/$f" ] && { cp "$COUNTS/$f" "$DST_TRACKING/counts/$f" || fail=1; }
    done
    cp "$REPORT_DIR/name_problems.csv" "$DST_TRACKING/" || fail=1
fi

# ---------------------------------------------------------------- checks
say "=== check: data files, source -> share"
for name in pdf pkl; do
    src=$([ "$name" = pdf ] && echo "$PDF_OUT" || echo "$PKL_OUT")
    n_src=$(data_files "$src" | wc -l)
    n_dst=$(find "$DST/$name" -type f ! -name '*.tmp.*' 2>/dev/null | wc -l)
    say "  $name  $n_src -> $n_dst  $([ "$n_src" = "$n_dst" ] && echo OK || echo DIFFERENT)"
    [ "$n_src" = "$n_dst" ] || fail=1
    stray=$(find "$DST/$name" \( -name done.json -o -name pages.csv -o -path '*/_state/*' \) 2>/dev/null | wc -l)
    [ "$stray" = 0 ] || { say "  $name: $stray tracking file(s) inside the data folder"; fail=1; }
    if [ "$CHECKSUM" = 1 ]; then
        diff=$(rsync -rcn --out-format='%n' "${DATA_EXCL[@]}" "$src/" "$DST/$name/" | grep -vc '/$')
        say "  $name content check: $diff file(s) differ"
        [ "$diff" = 0 ] || fail=1
    fi
done

if [ "$fail" = 0 ]; then
    say "ALL DONE - data: $DST (pdf/, pkl/), tracking: $DST_TRACKING"
else
    say "FINISHED WITH PROBLEMS - see above. If counts differ, check"
    say "  $REPORT_DIR/name_problems.csv (names the share cannot keep apart)"
fi
exit "$fail"
