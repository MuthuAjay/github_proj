#!/usr/bin/env bash
# copy_group4_5_R.sh - copy the group 4.5_R text delta (890 readable text
# types from groups 4.3 / 4.6) to the file share and
# check it, keeping the tracking files OUT of the scanning scope (in group 2
# the scanner took every manifest and done.json in the folder as data).
#
#   report    delta_by_extension.py: files and lines sent per extension
#             (made first if not there yet)
#   data      $DELTA/<org>/<repo>/<path>.txt              -> $DST/
#             (nothing from _state/ or _logs/; hard-linked files are
#             copied as real files)
#   tracking  $DELTA/_state (per repo manifest.csv, done.json), the
#             list of skipped vendored files (with SKIP_VENDORED=1), the
#             report, the extension list, the name check  -> $DST_TRACKING/
#   checks    before: names the share cannot keep apart (differ only by
#             case) or hold at all (\ : * ? " < > |, trailing dot or
#             space) -> $CHECKS/name_problems.csv; after: .txt counts
#             source vs share, no tracking file among the data, and
#             contents with CHECKSUM=1
#
# Run it after run_text_versions.sh finished (its log ends with "runner
# done"). The share needs sudo, the whole command inside sudo bash -c:
#
#   sudo bash -c 'cd /data/workarea/scripts && \
#       nohup ./copy_group4_5_R.sh > /data/workarea/copy_group4_5_R.out 2>&1 &'
#   tail -f /data/workarea/copy_group4_5_R.out
#
#   ONLY=verify ...    only the checks (after a finished copy)
#   CHECKSUM=1 ...     compare contents too, not only size + date
#
# Settings (put them in front, e.g. DST=/path ./copy_group4_5_R.sh):
#   OUT        the run's OUT folder (file_added_lines.py output)
#   DELTA      the delta to send (default $OUT"_delta")
#   DST        folder on the share for the data
#   DST_TRACKING  folder on the share for the tracking files
# Exit code: 0 = copied and checked, 1 = a copy or check failed (see log).

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-/data/workarea/group4_5_R/extract}"
DELTA="${DELTA:-${OUT}_delta}"
DST="${DST:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_5_R}"
DST_TRACKING="${DST_TRACKING:-${DST}_tracking}"
CHECKS="${CHECKS:-${OUT}_copy_checks}"
EXTS_FILE="${EXTS_FILE:-$HERE/group4_5_R_extensions.txt}"
REPORT="${REPORT:-${OUT}_delta_by_extension.csv}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PYTHON="${PYTHON:-python3}"
OPTS=(-rt --no-perms --no-owner --no-group --partial)
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)
DATA_EXCL=(--exclude "/_state/" --exclude "/_logs/" --exclude "*.tmp.*")

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

data_files() {   # the .txt files to send, relative, one per line
    ( cd "$DELTA" && find . -mindepth 1 \( -path ./_state -o -path ./_logs \) -prune \
        -o -type f ! -name '*.tmp.*' -print | sed 's#^\./##' )
}

[ -d "$DELTA/_state" ] || { say "ERROR: $DELTA has no _state folder - is the run finished? (OUT=$OUT)"; exit 1; }
[ -d "$(dirname "$DST")" ] || { say "ERROR: $(dirname "$DST") not found - is the share mounted? set DST=..."; exit 1; }
mkdir -p "$CHECKS"
say "start: $DELTA -> $DST  (tracking -> $DST_TRACKING)"

# --------------------------------------------------------------- report
if [ ! -f "$REPORT" ] && [ "$ONLY" != verify ]; then
    say "=== report: files and lines sent per extension -> $REPORT"
    ext_arg=()
    [ -f "$EXTS_FILE" ] && ext_arg=(--extensions "$EXTS_FILE")
    "$PYTHON" "$HERE/delta_by_extension.py" --extract "$OUT" --delta "$DELTA" \
        ${ext_arg[@]+"${ext_arg[@]}"} --out "$REPORT" \
        || { say "  report failed (the copy goes on)"; fail=1; }
fi

# ---------------------------------------------------------- name checks
say "=== names the share may not keep apart"
data_files > "$CHECKS/data_files.txt"
"$PYTHON" - "$CHECKS/data_files.txt" "$CHECKS/name_problems.csv" <<'PY'
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
print("  %d .txt files to send, %d with a name problem -> %s" % (len(paths), len(rows), sys.argv[2]))
PY

if [ "$ONLY" != verify ]; then
    mkdir -p "$DST" "$DST_TRACKING" || { say "ERROR: cannot create $DST"; exit 1; }
    say "free on the share: $(df -h "$DST" | awk 'NR==2 {print $4}'), to send: $(du -sh --exclude=_state --exclude=_logs "$DELTA" 2>/dev/null | cut -f1)"
    # ----------------------------------------------------------------- data
    say "=== data -> $DST"
    rsync "${OPTS[@]}" "${DATA_EXCL[@]}" "$DELTA/" "$DST/" || fail=1
    # ------------------------------------------------------------- tracking
    say "=== tracking -> $DST_TRACKING (not for scanning)"
    rsync "${OPTS[@]}" "$DELTA/_state/" "$DST_TRACKING/_state/" || fail=1
    for f in "$REPORT" "$REPORT.md" "$EXTS_FILE" "$CHECKS/name_problems.csv" \
             "$OUT/skipped_vendored_all.csv"; do
        [ -f "$f" ] && { cp "$f" "$DST_TRACKING/" || fail=1; }
    done
fi

# --------------------------------------------------------------- checks
say "=== check: .txt files, source -> share"
n_src=$(wc -l < "$CHECKS/data_files.txt")
n_dst=$(find "$DST" -type f ! -name '*.tmp.*' 2>/dev/null | wc -l)
say "  files  $n_src -> $n_dst  $([ "$n_src" = "$n_dst" ] && echo OK || echo DIFFERENT)"
[ "$n_src" = "$n_dst" ] || fail=1
stray=$(find "$DST" \( -name done.json -o -name manifest.csv -o -path '*/_state/*' \) 2>/dev/null | wc -l)
[ "$stray" = 0 ] || { say "  $stray tracking file(s) inside the data folder"; fail=1; }
if [ "$CHECKSUM" = 1 ]; then
    diff=$(rsync -rcn --out-format='%n' "${DATA_EXCL[@]}" "$DELTA/" "$DST/" | grep -vc '/$')
    say "  content check: $diff file(s) differ"
    [ "$diff" = 0 ] || fail=1
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE - data: $DST, tracking: $DST_TRACKING"
else
    say "FINISHED WITH PROBLEMS - see above. If the counts differ, check"
    say "  $CHECKS/name_problems.csv (names the share cannot keep apart)"
fi
exit "$fail"
