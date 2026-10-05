#!/usr/bin/env bash
# run_group4_2.sh - group 4.2 category A (zip, gz, tar, 7z, rar, ...), per
# repo, from the archive to every file inside, deduped per repo:
#
#   0. batches   only the repos whose history has one of the archive types
#                (from the all-extension count) -> $G/batches/M42.csv, plus
#                a 20-repo test batch M4T
#   1. counts    run_ext_versions.sh, the types as binary (pass 1 + 2)
#   2. extract   run_extract_versions.sh --per-repo --skip-vendored: every
#                archive version not in today's copy -> $G/4_2_extract
#   3. unpack    archive_unpack.py: every version + today's copy opened,
#                every file inside written once per repo, manifest + stats
#                -> $G/unpacked (stats in $G/unpacked/stats)
#
#   mkdir -p /data/workarea/group4_2
#   nohup ./run_group4_2.sh M4T > /data/workarea/group4_2/run_test.out 2>&1 &
#   nohup ./run_group4_2.sh     > /data/workarea/group4_2/run_full.out 2>&1 &
#   STEPS="unpack" ./run_group4_2.sh    only some steps (the others done)
#
# Every step resumes; after a stop run the same command again.
# Exit codes: 0 done; 1 done, some repos failed; 3 stopped (disk low / the
# active copy's mount not answering) - fix it and run again.
# Needs: 7z (apt p7zip-full) for 7z/rar/cab/.Z; pip zstandard, lz4 for
# .zst/.lz4 - without them those archives are listed, not opened.

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
ALL_COUNTS="${ALL_COUNTS:-/data/workarea/all_ext_counts/by_repo.csv}"
G="${G:-/data/workarea/group4_2}"
BATCHES="${BATCHES:-$G/batches}"
COUNTS="${COUNTS:-$G/counts}"
EXTRACT="${EXTRACT:-$G/4_2_extract}"
UNPACKED="${UNPACKED:-$G/unpacked}"
EXTS="${EXTS:-zip gz gzip tar tgz bz2 xz z lzma 7z rar zst lz4 zipx cab}"
STEPS="${STEPS:-batches counts extract unpack}"
WORKERS="${WORKERS:-8}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
PYTHON="${PYTHON:-python3}"
export SCRIPTS ROOT ACTIVE PYTHON MIN_DISK_GB BATCHES

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$G"
csv_exts=$(echo $EXTS | tr ' ' ',')

if [[ " $STEPS " == *" batches "* ]] && [ ! -f "$BATCHES/plan.csv" ]; then
    say "=== 0. batches from $ALL_COUNTS"
    mkdir -p "$BATCHES"
    EXTS="$EXTS" "$PYTHON" - "$ALL_COUNTS" "$BATCHES" <<'PY' || exit 1
import csv, os, sys
exts = set(os.environ["EXTS"].split())
repos = sorted({(r["org"], r["repo"]) for r in csv.DictReader(open(sys.argv[1], encoding="utf-8"))
                if r["ext"] in exts and int(r["files_in_history"] or 0) > 0})
def write(name, rows):
    with open(os.path.join(sys.argv[2], name + ".csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["org", "repo", "tier", "weight", "listed_files"])
        for o, r in rows: w.writerow([o, r, "group4_2", 1, 0])
write("M42", repos)
write("M4T", repos[:20])
open(os.path.join(sys.argv[2], "plan.csv"), "w").write("batch,repos\nM42,%d\n" % len(repos))
print("  %d repos -> M42.csv, %d -> M4T.csv (test)" % (len(repos), min(20, len(repos))))
PY
fi

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi
say "group 4.2 start: steps '$STEPS', batch(es): ${list[*]}"
failed=0

step_rc() {
    case "$2" in
        0) say "DONE $1" ;;
        1) say "DONE $1 - some repos failed (see its logs)"; failed=1 ;;
        3) say "STOP $1: disk low or the active copy not answering - fix it and run this again"; exit 3 ;;
        *) say "FAIL $1 exit=$2 - see its logs; rerun to resume"; exit "$2" ;;
    esac
}

for step in $STEPS; do
    case "$step" in
    batches) ;;
    counts)
        say "=== 1. counts -> $COUNTS"
        EXTS="$EXTS" BINARY_EXTS="$EXTS" OUT="$COUNTS" "$SCRIPTS/run_ext_versions.sh" "${list[@]}"
        step_rc counts $?
        ;;
    extract)
        say "=== 2. extract per repo (vendored listed only) -> $EXTRACT"
        STATE="$COUNTS" OUT="$EXTRACT" EXTRA="--extensions $csv_exts --per-repo --skip-vendored" \
            "$SCRIPTS/run_extract_versions.sh" "${list[@]}"
        step_rc extract $?
        ;;
    unpack)
        say "=== 3. unpack every version + today's copy -> $UNPACKED"
        mkdir -p "$UNPACKED/_logs"
        rc_step=0
        for b in "${list[@]}"; do
            if [ -f "$UNPACKED/_logs/$b.done" ]; then
                say "SKIP unpack $b: done ($(cat "$UNPACKED/_logs/$b.done"))"; continue
            fi
            start=$(date +%s)
            "$PYTHON" "$SCRIPTS/archive_unpack.py" --in "$EXTRACT" --counts "$COUNTS" \
                --active-root "$ACTIVE" --out "$UNPACKED" --batch "$BATCHES/$b.csv" \
                --extensions "$csv_exts" --workers "$WORKERS" \
                --min-free-disk-gb "$MIN_DISK_GB" >> "$UNPACKED/_logs/$b.out" 2>&1
            rc=$?
            took=$(( $(date +%s) - start ))
            case $rc in
                0|1) echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s rc=$rc" > "$UNPACKED/_logs/$b.done"
                     say "DONE unpack $b in ${took}s: $(grep '^END' "$UNPACKED/_logs/$b.out" | tail -1)"
                     [ "$rc" = 1 ] && rc_step=1 ;;
                3)   say "STOP unpack $b: $(grep STOPPED "$UNPACKED/_logs/$b.out" | tail -1)"; exit 3 ;;
                *)   say "FAIL unpack $b exit=$rc - see $UNPACKED/_logs/$b.out"; exit "$rc" ;;
            esac
        done
        step_rc unpack "$rc_step"
        ;;
    *) say "unknown step '$step' (batches counts extract unpack)"; exit 2 ;;
    esac
done

say "group 4.2 done - stats: $UNPACKED/stats (by_type.csv: what is inside, by route)"
exit "$failed"
