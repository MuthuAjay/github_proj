#!/usr/bin/env bash
# run_group4_7_9_bi.sh - the container types of group 4.7+4.9 (zips under
# another name: pbix pbit pbi twbx dbc mwb numbers kmz qgz): ONLY the data
# and the images (user, 26 Oct), per repo, from the archive:
#
#   0. batches   the repos whose history has one of the types (from the
#                all-extension count) -> $G/batches/M479BI.csv
#   1. counts    run_ext_versions.sh, the types as binary (pass 1 + 2)
#                -> $G/counts (summary.md: versions and GB per type - look
#                at it before extract: every pbix version holds its data)
#   2. extract   run_extract_versions.sh --per-repo --skip-vendored: every
#                version not in today's copy -> $G/extract
#   3. data      bi_to_data.py: the data as text, one .txt per path, every
#                version + today's file - pbix data model tables (pbixray),
#                twbx .hyper extracts (Hyper API), dbc notebook outputs, mwb
#                sample rows (SQLite), numbers cell text, kmz placemarks;
#                images and twbx packaged data files whole, each distinct
#                content once per repo (pbit, qgz: images only) -> $G/data
#   4. inner     bi_to_data.py --archive-inner: the same types inside the
#                4.2 / 4.2_R archives -> <run>/group4_7_9_bi_data
#
#   mkdir -p /data/workarea/group4_7_9/bi
#   STEPS="batches counts" nohup ./run_group4_7_9_bi.sh > /data/workarea/group4_7_9/bi/run.out 2>&1 &
#   (look at counts/summary.md, then)
#   STEPS="extract data inner" nohup ./run_group4_7_9_bi.sh >> /data/workarea/group4_7_9/bi/run.out 2>&1 &
#
# Time test before the full data step (biggest repos printed by batches):
#   STEPS=data EXTRA="--repo ORG/REPO" ./run_group4_7_9_bi.sh
#
# Needs: pip install pbixray tableauhyperapi (check: $PYTHON bi_to_data.py
# --check-libs). Without them the data is noted as not read and only the
# images go; install, then STEPS="data inner" EXTRA="--redo".
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
G="${G:-$WORK/group4_7_9/bi}"
EXTS="${EXTS:-pbix pbit pbi twbx dbc mwb numbers kmz qgz}"
STEPS="${STEPS:-batches counts extract data inner}"
RUNS="${RUNS:-group4_2 group4_2_R}"
WORKERS="${WORKERS:-4}"               # steps 3 and 4: repos at once (pbixray uses memory)
EXTRA="${EXTRA:-}"                    # more options for bi_to_data.py
MIN_DISK_GB="${MIN_DISK_GB:-50}"
PYTHON="${PYTHON:-python3}"
BATCHES="$G/batches"
B=M479BI
export SCRIPTS ROOT ACTIVE PYTHON MIN_DISK_GB

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$G"
csv_exts=$(echo $EXTS | tr ' ' ',')
failed=0
say "group 4.7+4.9 containers (data + images only) start: steps '$STEPS', types $csv_exts -> $G"

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
    for (o, r), n in sorted(files.items()): w.writerow([o, r, "group4_7_9_bi", 1, n])
open(os.path.join(sys.argv[2], "plan.csv"), "w").write("batch,repos\n%s,%d\n" % (sys.argv[3], len(files)))
print("%d repos, %d files in history; the biggest (for a time test):" % (len(files), sum(files.values())))
for (o, r), n in files.most_common(5): print("  %s/%s  %d files" % (o, r, n))
PY
        ;;
    counts)
        say "=== 1. counts (the types as binary) -> $G/counts"
        BATCHES="$BATCHES" OUT="$G/counts" EXTS="$EXTS" BINARY_EXTS="$EXTS" MATCH_IN_PYTHON=1 \
            "$SCRIPTS/run_ext_versions.sh" "$B"
        rc=$?
        [ -f "$G/counts/by_extension.csv" ] && cat "$G/counts/by_extension.csv"
        step_rc counts "$rc"
        ;;
    extract)
        say "=== 2. extract per repo, vendored skipped -> $G/extract"
        BATCHES="$BATCHES" STATE="$G/counts" OUT="$G/extract" \
            EXTRA="--extensions $csv_exts --per-repo --skip-vendored" \
            "$SCRIPTS/run_extract_versions.sh" "$B"
        step_rc extract $?
        ;;
    data)
        say "=== 3. data + images, today's files included -> $G/data"
        mkdir -p "$G/data/_logs"
        # shellcheck disable=SC2086
        "$PYTHON" "$SCRIPTS/bi_to_data.py" --in "$G/extract" --out "$G/data" \
            --batch "$BATCHES/$B.csv" --extensions "$csv_exts" \
            --state "$G/counts" --active-root "$ACTIVE" \
            --workers "$WORKERS" $EXTRA >> "$G/data/_logs/$B.out" 2>&1
        rc=$?
        say "$(grep '^END' "$G/data/_logs/$B.out" | tail -1)"
        [ -f "$G/data/by_type.csv" ] && cat "$G/data/by_type.csv"
        step_rc data "$rc"
        ;;
    inner)
        for g in $RUNS; do
            un="$WORK/$g/unpacked" out="$WORK/$g/group4_7_9_bi_data"
            if [ ! -d "$un/_state" ]; then
                say "[$g] ERROR: $un/_state not found - skipped"; failed=1; continue
            fi
            say "=== 4. inner [$g] $un -> $out"
            mkdir -p "$out/_logs"
            # shellcheck disable=SC2086
            "$PYTHON" "$SCRIPTS/bi_to_data.py" --in "$un" --out "$out" --archive-inner \
                --extensions "$csv_exts" --workers "$WORKERS" --name inner $EXTRA \
                >> "$out/_logs/inner.out" 2>&1
            rc=$?
            say "[$g] $(grep '^END' "$out/_logs/inner.out" | tail -1)"
            [ -f "$out/by_type.csv" ] && cat "$out/by_type.csv"
            step_rc "inner $g" "$rc"
        done
        ;;
    *)
        say "unknown step '$step' (batches counts extract data inner)"; exit 2 ;;
    esac
done

say "done - send: $G/data/{data_text,files} and <run>/group4_7_9_bi_data/{data_text,files} (without _state/_logs/_tmp)"
exit "$failed"
