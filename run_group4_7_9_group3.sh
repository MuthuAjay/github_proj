#!/usr/bin/env bash
# run_group4_7_9_group3.sh - the 17 group 4.7+4.9 types routed to group 3
# (table / serialized data: parquet rds pickle npy sav npz ckpt joblib rda
# nippy orc arrow rdata bson feather pk avro), per repo, from the archive to
# text, as group 3 (pkl) was done:
#
#   0. batches   only the repos whose history has one of the types (from the
#                all-extension count) -> $G/batches/M479G3.csv
#   1. counts    run_ext_versions.sh, the types as binary (pass 1 + 2)
#                -> $G/counts
#   2. extract   run_extract_versions.sh --per-repo --skip-vendored: every
#                version not in the same repo's active copy, each content
#                once per repo -> $G/extract/files/<ext>/<org>/<repo>/...
#   3. text      data_to_text.py: one .txt per path, every value of every
#                version + today's file (also paths whose only content is
#                today's file, from the counts) -> $G/text
#   4. inner     data_to_text.py --archive-inner: the same types inside the
#                4.2 / 4.2_R archives (already unpacked, no git)
#                -> /data/workarea/<run>/group4_7_9_group3_text
#
#   mkdir -p /data/workarea/group4_7_9/group3
#   nohup ./run_group4_7_9_group3.sh > /data/workarea/group4_7_9/group3/run.out 2>&1 &
#   tail -f /data/workarea/group4_7_9/group3/run.out
#   STEPS="text inner" ./run_group4_7_9_group3.sh    only some steps
#
# Time test before the full text step (the biggest repos are printed by the
# batches step):
#   STEPS=text EXTRA="--repo ORG/REPO" ./run_group4_7_9_group3.sh
# then run text again without EXTRA: repos already done are skipped.
#
# Libraries: data_to_text.py runs without any, but then parquet / orc /
# arrow / feather (pyarrow), avro (fastavro), sav (pyreadstat), zstd / lz4 /
# nippy (zstandard, lz4) get only the raw strings scan, flagged in read_as.
# Check with: $PYTHON data_to_text.py --check-libs. After installing, redo
# steps 3-4: STEPS="text inner" EXTRA="--redo" ./run_group4_7_9_group3.sh
# Use PYTHON=/path/to/venv/bin/python3 if they are in a venv.
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
EXTS="${EXTS:-parquet rds pickle npy sav npz ckpt joblib rda nippy orc arrow rdata bson feather pk avro}"
STEPS="${STEPS:-batches counts extract text inner}"
RUNS="${RUNS:-group4_2 group4_2_R}"
WORKERS="${WORKERS:-8}"               # steps 3 and 4: repos at once
EXTRA="${EXTRA:-}"                    # more options for data_to_text.py
MIN_DISK_GB="${MIN_DISK_GB:-20}"
PYTHON="${PYTHON:-python3}"
BATCHES="$G/batches"
B=M479G3
export SCRIPTS ROOT ACTIVE PYTHON MIN_DISK_GB

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$G"
csv_exts=$(echo $EXTS | tr ' ' ',')
failed=0
say "group 4.7+4.9 group 3 start: steps '$STEPS', types $csv_exts -> $G"

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
        EXTS="$EXTS" "$PYTHON" - "$ALL_COUNTS" "$BATCHES" "$B" <<'PY' || exit 1
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
        say "=== 1. counts (the types as binary) -> $G/counts"
        BATCHES="$BATCHES" OUT="$G/counts" EXTS="$EXTS" BINARY_EXTS="$EXTS" \
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
            say "=== 4. inner [$g] $un -> $out"
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
    *)
        say "unknown step '$step' (batches counts extract text inner)"; exit 2 ;;
    esac
done

say "group 4.7+4.9 group 3 done: $G/text and <run>/group4_7_9_group3_text (send without _state/_logs)"
exit "$failed"
