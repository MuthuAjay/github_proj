#!/usr/bin/env bash
# copy_group4_2.sh - copy the files taken from inside the 4.2 (archives) and
# 4.2_R (packages) archives to the file share and check them, keeping the
# tracking files OUT of the scanning scope (group 2's lesson: the scanner
# took every manifest and done.json in the folder as data). Everything is
# sent (no delta: archives were never scanned), both runs, every step:
#
#   step folder        what                        (in $WORK/<run>/)
#   group1_text        group 1 text, one .txt per path
#   group4_5_R_text    group 4.5_R text
#   group4_5_text      group 4.5 text
#   group2_text        group 2 text
#   group2_binary      group 2 binary files, each distinct content once
#   group3_pkl_text    group 3 pkl strings, one .txt per path
#   group3_pdf_merged  group 3 PDFs, distinct pages, one PDF per path
#
#   data      $WORK/<run>/<step>/<org>/<repo>/...  -> $DST_ROOT/<run>/<step>/
#             (nothing from _state/, _logs/, stats/, stats_combined/, _tmp/,
#             _removed_duplicates/ or the step folder's own files:
#             summary.csv, settings.json)
#   tracking  each step's _state/ (manifest.csv, left_out.csv = vendored /
#             macOS / DocFX paths left out, done.json), stats/,
#             stats_combined/, summary.csv; <run>/nested/ (todo, compare,
#             delta, by_extension_report - not the before / before_out
#             copies); the 4.1 path list; the checks
#                                    -> $DST_ROOT/<run>_tracking/<step>/
#   checks    before: names the share cannot keep apart (differ only by
#             case) or hold at all (\ : * ? " < > |, trailing dot or space,
#             a name over 255 bytes) -> $CHECKS/name_problems.csv; macOS
#             metadata among the data (should be none); after: files per
#             step source vs share, no tracking file among the data, and
#             contents with CHECKSUM=1
#
# rsync skips what is already on the share: after an interruption run the
# same command again and it carries on. The share needs sudo, the whole
# command inside sudo bash -c:
#
#   sudo bash -c 'cd /data/workarea/scripts && \
#       nohup ./copy_group4_2.sh > /data/workarea/copy_group4_2.out 2>&1 &'
#   tail -f /data/workarea/copy_group4_2.out
#
#   ONLY=verify ...    only the checks (after a finished copy)
#   RUNS=group4_2 ...  one run only (default "group4_2 group4_2_R")
#   STEP_DIRS="group1_text" ...   some steps only (default all seven)
#   PARALLEL=16 ...    rsync processes at once, one per <org>/<repo> folder,
#                      biggest first (default 16)
#   CHECKSUM=1 ...     compare contents too, not only size + date
#
# Settings: WORK (/data/workarea), DST_ROOT (the share's p1 folder), CHECKS
# ($WORK/group4_2/copy_checks), TRACK_ROOT (where <run>_tracking goes,
# default $DST_ROOT; set it when DST_ROOT is a folder the scanner reads as a
# whole), CHECKS_DST (where name_problems.csv and steps.txt go on the share,
# default $TRACK_ROOT/group4_2_tracking/copy_checks: give another copy - e.g.
# other STEP_DIRS - its own, so it does not overwrite the first copy's),
# EXTRAS (1 = also the 4.2 runs' nested/ files and the 4.1 path lists; 0 for
# a copy of other steps), PYTHON.
#
# Group 4.7+4.9 inside the archives (run_group4_7_9_inner.sh), under
# p1/group4_7_9 with its tracking in p1/group4_7_9_tracking:
#
#   sudo bash -c 'cd /data/workarea/scripts && P1=/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1 && \
#       STEP_DIRS="group4_7_9_text group4_7_9_binary" EXTRAS=0 \
#       DST_ROOT=$P1/group4_7_9 TRACK_ROOT=$P1/group4_7_9_tracking \
#       CHECKS=/data/workarea/group4_7_9/inner/copy_checks \
#       nohup ./copy_group4_2.sh > /data/workarea/group4_7_9/inner/copy.out 2>&1 &'
#   and the group 3 / tabular / container types inside the archives:
#       STEP_DIRS="group4_7_9_group3_text group4_7_9_tabular_binary group4_7_9_bi_data"
#       with CHECKS and CHECKS_DST of their own (see copy_group4_7_9_rest.sh)
# Exit code: 0 = copied and checked, 1 = a copy or check failed (see log).

set -u

WORK="${WORK:-/data/workarea}"
DST_ROOT="${DST_ROOT:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1}"
RUNS="${RUNS:-group4_2 group4_2_R}"
STEP_DIRS="${STEP_DIRS:-group1_text group4_5_R_text group4_5_text group2_text group2_binary group3_pkl_text group3_pdf_merged}"
CHECKS="${CHECKS:-$WORK/group4_2/copy_checks}"
TRACK_ROOT="${TRACK_ROOT:-$DST_ROOT}"
CHECKS_DST="${CHECKS_DST:-$TRACK_ROOT/group4_2_tracking/copy_checks}"
EXTRAS="${EXTRAS:-1}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PARALLEL="${PARALLEL:-16}"
PYTHON="${PYTHON:-python3}"
OPTS=(-rt --no-perms --no-owner --no-group --partial)
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)
TRACK_DIRS="_state stats stats_combined"     # per step folder, to tracking

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

data_files() {   # step folder -> its data files, relative, one per line
    ( cd "$1" && find . -mindepth 1 -maxdepth 1 -type d \
        ! -name _state ! -name _logs ! -name stats ! -name stats_combined \
        ! -name _tmp ! -name _removed_duplicates -print0 \
        | xargs -0 -r -I{} find {} -type f ! -name '*.tmp.*' -print \
        | sed 's#^\./##' )
}

[ -d "$(dirname "$DST_ROOT")" ] || { say "ERROR: $(dirname "$DST_ROOT") not found - is the share mounted? set DST_ROOT=..."; exit 1; }
mkdir -p "$CHECKS"
say "start: runs [$RUNS], steps [$STEP_DIRS] -> $DST_ROOT/<run>/<step>  (tracking -> $TRACK_ROOT/<run>_tracking)"

# ------------------------------------------------------ what there is to send
# all data files as <run>/<step>/<org>/<repo>/..., relative to $WORK and to
# $DST_ROOT alike
: > "$CHECKS/data_files.txt"
: > "$CHECKS/steps.txt"
for g in $RUNS; do
    for d in $STEP_DIRS; do
        src="$WORK/$g/$d"
        if [ ! -d "$src" ]; then
            say "  $g/$d: not found - skipped"; continue
        fi
        [ -d "$src/_state" ] || say "  $g/$d: WARNING no _state folder - is the step finished?"
        n0=$(wc -l < "$CHECKS/data_files.txt")
        data_files "$src" | sed "s#^#$g/$d/#" >> "$CHECKS/data_files.txt"
        n=$(( $(wc -l < "$CHECKS/data_files.txt") - n0 ))
        printf '%s/%s\t%d\n' "$g" "$d" "$n" >> "$CHECKS/steps.txt"
        say "  $g/$d: $n file(s)"
    done
done
say "  total: $(wc -l < "$CHECKS/data_files.txt") file(s) to send"

# ------------------------------------------------------------- name checks
say "=== names the share may not keep apart, macOS metadata"
"$PYTHON" - "$CHECKS/data_files.txt" "$CHECKS/name_problems.csv" <<'PY' || fail=1
import csv, re, sys
from collections import defaultdict
paths = [l.rstrip("\n") for l in open(sys.argv[1], encoding="utf-8", errors="surrogateescape")]
by_low = defaultdict(list)
for p in paths:
    by_low[p.lower()].append(p)
bad = re.compile(r'[\\:*?"<>|]|[ .]$')
rows = [(p, "differs only by case from: " + " | ".join(x for x in v if x != p))
        for v in by_low.values() if len(v) > 1 for p in v]
for p in paths:
    parts = p.split("/")
    if any(bad.search(x) for x in parts):
        rows.append((p, "character or ending the share does not allow"))
    if any(len(x.encode("utf-8", "surrogateescape")) > 255 for x in parts):
        rows.append((p, "a name longer than 255 bytes"))
    if "__MACOSX" in parts or parts[-1].startswith("._") or parts[-1].startswith(".DS_Store"):
        rows.append((p, "macOS metadata (should have been left out)"))
with open(sys.argv[2], "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
    w = csv.writer(fh)
    w.writerow(["path", "problem"])
    w.writerows(rows)
print("  %d files to send, %d with a name problem -> %s" % (len(paths), len(rows), sys.argv[2]))
PY

if [ "$ONLY" != verify ]; then
    mkdir -p "$DST_ROOT" || { say "ERROR: cannot create $DST_ROOT"; exit 1; }
    say "free on the share: $(df -h "$DST_ROOT" | awk 'NR==2 {print $4}')"
    # ------------------------------------------------------------------ data
    say "=== data -> $DST_ROOT/<run>/<step> ($PARALLEL rsync at a time, one per repo folder)"
    # repo folders (<run>/<step>/<org>/<repo>) by number of files, most first
    awk -F/ 'NF > 4 {c[$1 "/" $2 "/" $3 "/" $4]++} END {for (k in c) printf "%d\t%s\n", c[k], k}' \
        "$CHECKS/data_files.txt" | sort -t "$(printf '\t')" -k1,1rn | cut -f2- \
        > "$CHECKS/copy_repos.txt"
    : > "$CHECKS/copy_failed.txt"
    export WORK DST_ROOT CHECKS
    export OPTS_STR="${OPTS[*]}"
    copy_one() {
        mkdir -p "$DST_ROOT/$1" \
            && rsync $OPTS_STR --exclude "*.tmp.*" "$WORK/$1/" "$DST_ROOT/$1/" \
            || { echo "$1" >> "$CHECKS/copy_failed.txt"; return 1; }
    }
    export -f copy_one
    n_rep=$(wc -l < "$CHECKS/copy_repos.txt")
    say "  $n_rep repo folder(s)"
    tr '\n' '\0' < "$CHECKS/copy_repos.txt" \
        | xargs -0 -r -P "$PARALLEL" -n 1 bash -c 'copy_one "$1"' _ || fail=1
    # files not inside an <org>/<repo> folder (none expected)
    awk -F/ 'NF <= 4' "$CHECKS/data_files.txt" > "$CHECKS/copy_loose.txt"
    if [ -s "$CHECKS/copy_loose.txt" ]; then
        say "  $(wc -l < "$CHECKS/copy_loose.txt") file(s) outside a repo folder"
        rsync "${OPTS[@]}" --files-from="$CHECKS/copy_loose.txt" "$WORK/" "$DST_ROOT/" || fail=1
    fi
    n_bad=$(wc -l < "$CHECKS/copy_failed.txt")
    say "  repo folders done: $(( n_rep - n_bad )) of $n_rep$([ "$n_bad" -gt 0 ] && echo " - FAILED: $n_bad, listed in $CHECKS/copy_failed.txt (run again to retry)")"

    # -------------------------------------------------------------- tracking
    for g in $RUNS; do
        trk="$TRACK_ROOT/${g}_tracking"
        say "=== tracking $g -> $trk (not for scanning)"
        for d in $STEP_DIRS; do
            src="$WORK/$g/$d"
            [ -d "$src" ] || continue
            mkdir -p "$trk/$d" || { fail=1; continue; }
            for t in $TRACK_DIRS; do
                [ -d "$src/$t" ] && { rsync "${OPTS[@]}" "$src/$t/" "$trk/$d/$t/" || fail=1; }
            done
            find "$src" -mindepth 1 -maxdepth 1 -type f ! -name '*.tmp.*' \
                -exec cp -p {} "$trk/$d/" \; || fail=1
        done
        nd="$WORK/$g/nested"
        if [ "$EXTRAS" = 1 ] && [ -d "$nd" ]; then
            mkdir -p "$trk/nested"
            find "$nd" -mindepth 1 -maxdepth 1 -type f -exec cp -p {} "$trk/nested/" \; || fail=1
            [ -d "$nd/delta" ] && { rsync "${OPTS[@]}" "$nd/delta/" "$trk/nested/delta/" || fail=1; }
        fi
    done
    lists="$WORK/group4_2/group4_1_paths"
    if [ "$EXTRAS" = 1 ] && [ -d "$lists" ]; then
        rsync "${OPTS[@]}" --exclude "*.tmp.*" "$lists/" "$TRACK_ROOT/group4_2_tracking/group4_1_paths/" || fail=1
    fi
fi

# ------------------------------------------------------------------ checks
say "=== check: files per step, source -> share"
while IFS="$(printf '\t')" read -r k n_src; do
    n_dst=$(find "$DST_ROOT/$k" -type f ! -name '*.tmp.*' 2>/dev/null | wc -l)
    say "  $(printf '%-34s %9s -> %9s' "$k" "$n_src" "$n_dst")  $([ "$n_src" = "$n_dst" ] && echo OK || echo DIFFERENT)"
    [ "$n_src" = "$n_dst" ] || fail=1
done < "$CHECKS/steps.txt"
for g in $RUNS; do
    [ -d "$DST_ROOT/$g" ] || continue
    stray=$(find "$DST_ROOT/$g" \( -name done.json -o -path '*/_state/*' -o -path '*/stats/by_type.csv' \) 2>/dev/null | wc -l)
    [ "$stray" = 0 ] || { say "  $g: $stray tracking file(s) inside the data folder"; fail=1; }
done
if [ "$CHECKSUM" = 1 ]; then
    diff=$(rsync -rcn --out-format='%n' --files-from="$CHECKS/data_files.txt" \
        "$WORK/" "$DST_ROOT/" | grep -vc '/$')
    say "  content check: $diff file(s) differ"
    [ "$diff" = 0 ] || fail=1
fi
if [ "$ONLY" != verify ] && [ -d "$TRACK_ROOT" ]; then
    mkdir -p "$CHECKS_DST"
    cp -p "$CHECKS/name_problems.csv" "$CHECKS/steps.txt" "$CHECKS_DST/" || fail=1
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE - data: $DST_ROOT/<run>/<step>, tracking: $TRACK_ROOT/<run>_tracking"
else
    say "FINISHED WITH PROBLEMS - see above. If the counts differ, check"
    say "  $CHECKS/name_problems.csv (names the share cannot keep apart)"
fi
exit "$fail"
