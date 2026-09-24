#!/usr/bin/env bash
# run_batches.sh - run extraction batches one after another.
#
#   ./run_batches.sh                     every batch in plan order (S, M, L, G)
#   ./run_batches.sh M02 M03 M04 L01     just these, in this order
#
# Per batch it runs file_added_lines.py with the tier's workers and timeout
# (from the batch name's first letter), then marks the batch finished in
# $OUT/_logs/<batch>.batch_done. Batches with that marker are skipped, so
# running this again carries on where it stopped; a batch that stopped half
# way resumes at repo level (file_added_lines.py skips repos already done).
#
# It stops (without starting the next batch) when:
#   - free disk on $OUT drops below MIN_DISK_GB before a batch starts
#   - a batch exits 3 (its own disk / --until guard stopped it)
#   - a batch fails (any other non-zero exit)
#
# Settings can be overridden from the environment, e.g.
#   OUT=/data/workarea/full_extract UNTIL=07:00 ./run_batches.sh G01 G02
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_batches.sh M02 M03 M04 > /data/workarea/run_batches.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
ROOT="${ROOT:-/data/workarea/archive}"
OUT="${OUT:-/data/workarea/full_extract}"
MIN_DISK_GB="${MIN_DISK_GB:-50}"   # checked before each batch starts
UNTIL="${UNTIL:-}"                 # HH:MM - passed to each batch as --until
PYTHON="${PYTHON:-python3}"

# workers and per-repo timeout (seconds) by tier
settings() {
    case "$1" in
        S*) echo "16 900" ;;
        M*) echo "8 3600" ;;
        L*) echo "6 14400" ;;
        G*) echo "2 43200" ;;
        *)  echo "" ;;
    esac
}

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

free_gb() { df -Pk "$OUT" | awk 'NR==2 {printf "%d", $4 / 1024 / 1024}'; }

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    # plan.csv order: batch name is the first column
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi

mkdir -p "$OUT/_logs"
say "runner start: ${#list[@]} batch(es): ${list[*]}"

for b in "${list[@]}"; do
    csv="$BATCHES/$b.csv"
    marker="$OUT/_logs/$b.batch_done"
    if [ ! -f "$csv" ]; then
        say "SKIP $b: no batch file $csv"
        continue
    fi
    if [ -f "$marker" ]; then
        say "SKIP $b: already finished ($(cat "$marker"))"
        continue
    fi
    read -r workers timeout <<< "$(settings "$b")"
    if [ -z "${workers:-}" ]; then
        say "SKIP $b: unknown tier (name should start with S, M, L or G)"
        continue
    fi
    free=$(free_gb)
    if [ "$free" -lt "$MIN_DISK_GB" ]; then
        say "STOP before $b: ${free} GB free on $OUT, below MIN_DISK_GB=$MIN_DISK_GB"
        exit 3
    fi

    say "RUN  $b: workers=$workers timeout=${timeout}s free=${free}GB${UNTIL:+ until=$UNTIL}"
    start=$(date +%s)
    "$PYTHON" "$SCRIPTS/file_added_lines.py" --batch "$csv" \
        --repos-root "$ROOT" --out "$OUT" \
        --workers "$workers" --repo-timeout "$timeout" --quiet \
        ${UNTIL:+--until "$UNTIL"} \
        > "$OUT/_logs/$b.out" 2>&1
    rc=$?
    took=$(( $(date +%s) - start ))
    summary=$(grep '^END ' "$OUT/_logs/$b.out" | tail -1)

    case $rc in
        0)
            echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s $summary" > "$marker"
            say "DONE $b in ${took}s: ${summary#END }"
            ;;
        3)
            say "STOP $b stopped early (disk or --until): ${summary#END }"
            say "run the same command again to continue"
            exit 3
            ;;
        *)
            say "FAIL $b exit=$rc after ${took}s - see $OUT/_logs/$b.out and $OUT/_logs/$b.log"
            exit "$rc"
            ;;
    esac
done

say "runner done"
