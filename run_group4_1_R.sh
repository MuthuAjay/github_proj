#!/usr/bin/env bash
# run_group4_1_R.sh - group 4.1_R (bacpac ctrl dacpac dbf_11_8_2018 init sdb
# wal) as group 4.1 was done: path lists, nothing copied; plus a few samples
# to see what the files really are.
#
#   0. batches   only the repos whose history has one of the types (from the
#                all-extension count) -> $G/batches/M41R.csv
#   1. counts    run_ext_versions.sh pass 1 (versions per path, at_head)
#                -> $G/counts
#   2. paths     ext_path_list.py: one row per path in history
#                -> $G/group4_1_R_paths.csv (+ _legend, _by_extension)
#   3. inner     group4_2_path_list.py: the same types inside the 4.2 / 4.2_R
#                archives -> $G/group4_1_R_inner_paths.csv (+ ...)
#   4. samples   sample_ext_files.py: $PER_EXT newest files per type
#                -> $G/samples (index.md to read; real data, keep it here)
#
#   mkdir -p /data/workarea/group4_1_R
#   nohup ./run_group4_1_R.sh > /data/workarea/group4_1_R/run.out 2>&1 &
#   STEPS="paths inner" ./run_group4_1_R.sh    only some steps
#
# Every step resumes or is quick to redo; after a stop run it again.
# Exit codes: 0 done; 1 done, some repos failed; 3 stopped (the active
# copy's mount not answering) - fix it and run again.

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
ALL_COUNTS="${ALL_COUNTS:-/data/workarea/all_ext_counts/by_repo.csv}"
WORK="${WORK:-/data/workarea}"
G="${G:-$WORK/group4_1_R}"
EXTS="${EXTS:-bacpac ctrl dacpac dbf_11_8_2018 init sdb wal}"
STEPS="${STEPS:-batches counts paths inner samples}"
PER_EXT="${PER_EXT:-3}"
PYTHON="${PYTHON:-python3}"
BATCHES="$G/batches"
export SCRIPTS ROOT ACTIVE PYTHON

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$G"
csv_exts=$(echo $EXTS | tr ' ' ',')
fail=0
say "group 4.1_R start: steps '$STEPS', types $csv_exts -> $G"

for step in $STEPS; do
    case "$step" in
    batches)
        say "=== 0. batches from $ALL_COUNTS"
        mkdir -p "$BATCHES"
        EXTS="$EXTS" "$PYTHON" - "$ALL_COUNTS" "$BATCHES" <<'PY' || exit 1
import csv, os, sys
exts = set(os.environ["EXTS"].split())
repos = sorted({(r["org"], r["repo"]) for r in csv.DictReader(open(sys.argv[1], encoding="utf-8"))
                if r["ext"] in exts and int(r["files_in_history"] or 0) > 0})
with open(os.path.join(sys.argv[2], "M41R.csv"), "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["org", "repo", "tier", "weight", "listed_files"])
    for o, r in repos: w.writerow([o, r, "group4_1_R", 1, 0])
open(os.path.join(sys.argv[2], "plan.csv"), "w").write("batch,repos\nM41R,%d\n" % len(repos))
print("%d repos" % len(repos))
PY
        ;;
    counts)
        say "=== 1. counts, pass 1 -> $G/counts"
        PASSES=1 BATCHES="$BATCHES" OUT="$G/counts" EXTS="$EXTS" MATCH_IN_PYTHON=1 \
            "$SCRIPTS/run_ext_versions.sh" M41R
        rc=$?
        case $rc in
            0) say "DONE counts" ;;
            1) say "DONE counts - some repos failed (see its logs)"; fail=1 ;;
            3) say "STOP counts: the active copy not answering - fix it and run again"; exit 3 ;;
            *) say "FAIL counts exit=$rc"; exit "$rc" ;;
        esac
        ;;
    paths)
        say "=== 2. path list -> $G/group4_1_R_paths.csv"
        "$PYTHON" "$SCRIPTS/ext_path_list.py" "$G/counts" \
            --out "$G/group4_1_R_paths.csv" --extensions "$csv_exts" || fail=1
        ;;
    inner)
        say "=== 3. inside the archives -> $G/group4_1_R_inner_paths.csv"
        "$PYTHON" "$SCRIPTS/group4_2_path_list.py" \
            --unpacked "$WORK/group4_2/unpacked" --unpacked "$WORK/group4_2_R/unpacked" \
            --extensions "$csv_exts" --out "$G/group4_1_R_inner_paths.csv" || fail=1
        ;;
    samples)
        say "=== 4. samples ($PER_EXT per type) -> $G/samples"
        "$PYTHON" "$SCRIPTS/sample_ext_files.py" --by-repo "$ALL_COUNTS" \
            --repos-root "$ROOT" --out "$G/samples" --extensions "$csv_exts" \
            --per-ext "$PER_EXT" || fail=1
        ;;
    *) say "unknown step '$step' (batches counts paths inner samples)"; exit 2 ;;
    esac
done

for f in "$G/group4_1_R_paths_by_extension.csv" "$G/group4_1_R_inner_paths_by_extension.csv"; do
    [ -f "$f" ] && { say "$f:"; cat "$f"; }
done
if [ "$fail" = 0 ]; then say "ALL DONE"; else say "FINISHED WITH PROBLEMS - see above"; fi
exit "$fail"
