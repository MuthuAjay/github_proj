#!/usr/bin/env bash
# run_group4_7_9_group3.sh - the 17 group 4.7+4.9 types routed to group 3
# (table / serialized data), per repo, from the archive to what is sent.
# Two routes (user, 26 Oct):
#
#   tabular  TAB_EXTS: parquet orc arrow feather avro sav - as group 2:
#            whole files, each distinct content once, history + today
#   text     EXTS: rds rda rdata npy npz ckpt pickle joblib pk bson nippy -
#            data_to_text.py, one .txt per path, every value once
#
#   0. batches     the repos whose history has one of the 17 types (from the
#                  all-extension count) -> $G/batches/M479G3.csv
#   1. counts      run_ext_versions.sh, all 17 as binary (pass 1 + 2)
#                  -> $G/counts (both routes read it)
#   text route:
#   2. extract     run_extract_versions.sh --per-repo --skip-vendored: every
#                  version not in the same repo's active copy, each content
#                  once per repo -> $G/extract/files/<ext>/<org>/<repo>/...
#   3. text        data_to_text.py: one .txt per path, every value of every
#                  version + today's file (also paths whose only content is
#                  today's file, from the counts) -> $G/text
#   4. inner       data_to_text.py --archive-inner: the same types inside the
#                  4.2 / 4.2_R archives -> <run>/group4_7_9_group3_text
#   tabular route (as group 2 of 4.7+4.9: global dedupe, no size cap):
#   5. tab_extract run_extract_versions.sh: every history version not in the
#                  active copy -> $TAB/tabular_binary/files/<ext>/<ab>/<blob>
#   6. tab_active  active_files.py: today's files (never scanned), contents
#                  not already in tabular_binary -> $TAB/tabular_active
#   7. tab_inner   group4_2_files.py: the same types inside the 4.2 / 4.2_R
#                  archives, each distinct content once, whole (vendored /
#                  macOS / DocFX cache left out) -> <run>/group4_7_9_tabular_binary
#
#   mkdir -p /data/workarea/group4_7_9/group3
#   nohup ./run_group4_7_9_group3.sh > /data/workarea/group4_7_9/group3/run.out 2>&1 &
#   tail -f /data/workarea/group4_7_9/group3/run.out
#   STEPS="tab_extract tab_active tab_inner" ./run_group4_7_9_group3.sh   some steps
#
# Time test before the full text step (the biggest repos are printed by the
# batches step):
#   STEPS=text EXTRA="--repo ORG/REPO" ./run_group4_7_9_group3.sh
# then run text again without EXTRA: repos already done are skipped.
#
# Libraries: the tabular route needs none. data_to_text.py runs without
# any; zstd / lz4 compressed files and nippy then get only the raw strings
# scan (flagged in read_as) - pip install zstandard lz4, then
# STEPS="text inner" EXTRA="--redo". Check: $PYTHON data_to_text.py --check-libs
#
# Every step resumes: after a stop run the same command again.
# Exit codes: 0 done; 1 done, some repos failed (named in the logs);
# 3 stopped (disk low / the active copy's mount not answering) - fix it and
# run again; anything else: a step crashed, see its log.

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
ALL_COUNTS="${ALL_COUNTS:-/data/workarea/all_ext_counts/by_repo.csv}"
WORK="${WORK:-/data/workarea}"
G="${G:-$WORK/group4_7_9/group3}"
TAB="${TAB:-$WORK/group4_7_9/tabular}"
EXTS="${EXTS:-rds rda rdata npy npz ckpt pickle joblib pk bson nippy}"
TAB_EXTS="${TAB_EXTS:-parquet orc arrow feather avro sav}"
STEPS="${STEPS:-batches counts extract text inner tab_extract tab_active tab_inner}"
RUNS="${RUNS:-group4_2 group4_2_R}"
WORKERS="${WORKERS:-8}"               # steps 3, 4, 6, 7: repos at once
EXTRA="${EXTRA:-}"                    # more options for data_to_text.py
MIN_DISK_GB="${MIN_DISK_GB:-20}"
PYTHON="${PYTHON:-python3}"
BATCHES="$G/batches"
B=M479G3
export SCRIPTS ROOT ACTIVE PYTHON MIN_DISK_GB

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$G" "$TAB"
ALL_EXTS="$EXTS $TAB_EXTS"
csv_exts=$(echo $EXTS | tr ' ' ',')
csv_tab=$(echo $TAB_EXTS | tr ' ' ',')
failed=0
say "group 4.7+4.9 group 3 start: steps '$STEPS'"
say "text: $csv_exts -> $G | tabular (whole): $csv_tab -> $TAB"

step_rc() {   # step name, exit code
    case "$2" in
        0) say "DONE $1" ;;
        1) say "DONE $1 - some repos failed (see its logs)"; failed=1 ;;
        3) say "STOP $1: disk low or the active copy not answering - fix it and run this again"
           exit 3 ;;
        *) say "FAIL $1 exit=$2 - see its logs; rerun to resume"; exit "$2" ;;
    esac
}

for step in $STEPS; do
    case "$step" in
    batches)
        say "=== 0. batches from $ALL_COUNTS"
        mkdir -p "$BATCHES"
        EXTS="$ALL_EXTS" "$PYTHON" - "$ALL_COUNTS" "$BATCHES" "$B" <<'PY' || exit 1
import csv, os, sys
from collections import Counter
exts = set(os.environ["EXTS"].split())
files = Counter()
for r in csv.DictReader(open(sys.argv[1], encoding="utf-8")):
    if r["ext"] in exts and int(r["files_in_history"] or 0) > 0:
        files[(r["org"], r["repo"])] += int(r["files_in_history"])
with open(os.path.join(sys.argv[2], sys.argv[3] + ".csv"), "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["org", "repo", "tier", "weight", "listed_files"])
    for (o, r), n in sorted(files.items()): w.writerow([o, r, "group4_7_9_group3", 1, n])
open(os.path.join(sys.argv[2], "plan.csv"), "w").write("batch,repos\n%s,%d\n" % (sys.argv[3], len(files)))
print("%d repos, %d files in history; the biggest (for a time test):" % (len(files), sum(files.values())))
for (o, r), n in files.most_common(5): print("  %s/%s  %d files" % (o, r, n))
PY
        ;;
    counts)
        say "=== 1. counts (all 17 as binary) -> $G/counts"
        BATCHES="$BATCHES" OUT="$G/counts" EXTS="$ALL_EXTS" BINARY_EXTS="$ALL_EXTS" \
            "$SCRIPTS/run_ext_versions.sh" "$B"
        step_rc counts $?
        ;;
    extract)
        say "=== 2. extract per repo, vendored skipped -> $G/extract"
        BATCHES="$BATCHES" STATE="$G/counts" OUT="$G/extract" \
            EXTRA="--extensions $csv_exts --per-repo --skip-vendored" \
            "$SCRIPTS/run_extract_versions.sh" "$B"
        step_rc extract $?
        ;;
    text)
        say "=== 3. text, today's files included -> $G/text"
        mkdir -p "$G/text/_logs"
        # shellcheck disable=SC2086
        "$PYTHON" "$SCRIPTS/data_to_text.py" --in "$G/extract" --out "$G/text" \
            --batch "$BATCHES/$B.csv" --extensions "$csv_exts" \
            --state "$G/counts" --active-root "$ACTIVE" \
            --workers "$WORKERS" $EXTRA >> "$G/text/_logs/$B.out" 2>&1
        rc=$?
        say "$(grep '^END' "$G/text/_logs/$B.out" | tail -1)"
        [ -f "$G/text/by_type.csv" ] && cat "$G/text/by_type.csv"
        step_rc text "$rc"
        ;;
    inner)
        for g in $RUNS; do
            un="$WORK/$g/unpacked" out="$WORK/$g/group4_7_9_group3_text"
            if [ ! -d "$un/_state" ]; then
                say "[$g] ERROR: $un/_state not found - skipped"; failed=1; continue
            fi
            say "=== 4. inner text [$g] $un -> $out"
            mkdir -p "$out/_logs"
            # shellcheck disable=SC2086
            "$PYTHON" "$SCRIPTS/data_to_text.py" --in "$un" --out "$out" --archive-inner \
                --extensions "$csv_exts" --workers "$WORKERS" --name inner $EXTRA \
                >> "$out/_logs/inner.out" 2>&1
            rc=$?
            say "[$g] $(grep '^END' "$out/_logs/inner.out" | tail -1)"
            [ -f "$out/by_type.csv" ] && cat "$out/by_type.csv"
            step_rc "inner $g" "$rc"
        done
        ;;
    tab_extract)
        say "=== 5. tabular: history versions, global dedupe -> $TAB/tabular_binary"
        BATCHES="$BATCHES" STATE="$G/counts" OUT="$TAB/tabular_binary" \
            EXTRA="--extensions $csv_tab" \
            "$SCRIPTS/run_extract_versions.sh" "$B"
        step_rc tab_extract $?
        ;;
    tab_active)
        say "=== 6. tabular: today's files -> $TAB/tabular_active"
        mkdir -p "$TAB/tabular_active/_logs"
        "$PYTHON" "$SCRIPTS/active_files.py" --state "$G/counts" --active-root "$ACTIVE" \
            --out "$TAB/tabular_active" --also-stored "$TAB/tabular_binary" \
            --batch "$BATCHES/$B.csv" --extensions "$csv_tab" --workers "$WORKERS" \
            --min-free-disk-gb "$MIN_DISK_GB" >> "$TAB/tabular_active/_logs/$B.out" 2>&1
        rc=$?
        tail -3 "$TAB/tabular_active/_logs/$B.out"
        step_rc tab_active "$rc"
        ;;
    tab_inner)
        outs=()
        for g in $RUNS; do
            un="$WORK/$g/unpacked" out="$WORK/$g/group4_7_9_tabular_binary"
            if [ ! -d "$un/_state" ]; then
                say "[$g] ERROR: $un/_state not found - skipped"; failed=1; continue
            fi
            say "=== 7. inner tabular [$g] $un -> $out"
            "$PYTHON" "$SCRIPTS/group4_2_files.py" --unpacked "$un" --out "$out" \
                --extensions "$csv_tab" --workers "$WORKERS" --name tabular
            rc=$?
            [ -f "$out/stats/summary.json" ] && { cat "$out/stats/summary.json"; echo; }
            step_rc "tab_inner $g" "$rc"
            outs+=("$out")
        done
        if [ "${#outs[@]}" -gt 1 ]; then
            "$PYTHON" "$SCRIPTS/group4_2_files.py" --unpacked "$WORK/${RUNS%% *}/unpacked" \
                --out "${outs[0]}" --name tabular --stats-only --combine "${outs[@]}" \
                > /dev/null && say "[tab_inner] combined -> ${outs[0]}/stats_combined/by_type.csv"
        fi
        ;;
    *)
        say "unknown step '$step' (batches counts extract text inner tab_extract tab_active tab_inner)"
        exit 2 ;;
    esac
done

say "done - send: $G/text, $TAB/tabular_{binary,active}/files, <run>/group4_7_9_{group3_text,tabular_binary} (without _state/_logs)"
exit "$failed"
