#!/usr/bin/env bash
# run_extract_versions.sh - run extract_versions.py over every batch: write
# the previous versions of the binary extensions out of the archive, each
# content once, then build one manifest.
#
#   ./run_extract_versions.sh                  every batch in plan.csv order
#   ./run_extract_versions.sh S01 S02          just these, in this order
#
# Needs extension_versions.py's output (pass 1 AND pass 2) in $STATE; reads
# the archive repos; does not touch the active copy. A finished batch is
# marked with $OUT/_logs/<batch>.extract_done and skipped next time; a batch
# that stopped half way resumes at repo level, and files already stored are
# never written twice.
#
# Repos that fail or time out do not stop the run: the batch is still marked
# done (its log names them), and at the end one --retry-failed pass per batch
# with those repos is made, with more time. The runner STOPS (exit 3) when
# free disk on $OUT drops below MIN_DISK_GB - free space and run this again.
# It also stops when a batch itself crashes (exit code other than 0, 1, 3).
#
# Settings can be overridden from the environment:
#   OUT=/data/workarea/binary_versions MIN_DISK_GB=50 ./run_extract_versions.sh
# Extra flags for extract_versions.py (e.g. --skip-vendored) go in EXTRA:
#   EXTRA="--max-bytes 104857600" ./run_extract_versions.sh
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_extract_versions.sh > /data/workarea/run_extract_versions.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
ROOT="${ROOT:-/data/workarea/archive}"
STATE="${STATE:-/data/workarea/ext_versions}"
OUT="${OUT:-/data/workarea/binary_versions}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
EXTRA="${EXTRA:-}"
RETRY="${RETRY:-1}"                 # 0 = skip the final --retry-failed pass
PYTHON="${PYTHON:-python3}"

# workers, per-repo timeout (seconds) - by tier
settings() {
    case "$1" in
        S*) echo "16 1800" ;;
        M*) echo "8 3600" ;;
        L*) echo "6 14400" ;;
        G*) echo "2 43200" ;;
        *)  echo "" ;;
    esac
}

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

failed_in() {  # batch -> repos whose last attempt did not succeed
    "$PYTHON" - "$BATCHES/$1.csv" "$OUT/_state" <<'PY'
import csv, json, os, sys
batch, state = sys.argv[1], sys.argv[2]
n = 0
for r in csv.DictReader(open(batch, newline="", encoding="utf-8-sig")):
    try:
        with open(os.path.join(state, r["org"].strip(), r["repo"].strip(),
                               "done.json"), encoding="utf-8") as fh:
            n += json.load(fh).get("status") != "ok"
    except (OSError, ValueError, KeyError):
        pass          # not attempted yet
print(n)
PY
}

run_one() {    # batch, workers, timeout, extra flags...
    local b="$1" workers="$2" timeout="$3"
    shift 3
    # shellcheck disable=SC2086
    "$PYTHON" "$SCRIPTS/extract_versions.py" \
        --state "$STATE" --repos-root "$ROOT" --out "$OUT" \
        --batch "$BATCHES/$b.csv" --workers "$workers" \
        --repo-timeout "$timeout" --min-free-disk-gb "$MIN_DISK_GB" \
        --no-combine $EXTRA "$@" >> "$OUT/_logs/$b.out" 2>&1
}

disk_low() {
    say "STOP $1: less than ${MIN_DISK_GB} GB free on $OUT ($(df -h "$OUT" | awk 'NR==2 {print $4}') left)."
    say "     Free space and run this script again - finished batches and repos are skipped."
    exit 3
}

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi

mkdir -p "$OUT/_logs"
say "runner start: ${#list[@]} batch(es): ${list[*]}"
say "state=$STATE out=$OUT min_disk=${MIN_DISK_GB}GB ${EXTRA:+extra='$EXTRA'}"
with_failures=()

for b in "${list[@]}"; do
    marker="$OUT/_logs/$b.extract_done"
    if [ ! -f "$BATCHES/$b.csv" ]; then
        say "SKIP $b: no batch file $BATCHES/$b.csv"
        continue
    fi
    if [ -f "$marker" ]; then
        say "SKIP $b: already finished ($(cat "$marker"))"
        [ "$(failed_in "$b")" -gt 0 ] && with_failures+=("$b")
        continue
    fi
    read -r workers timeout <<< "$(settings "$b")"
    if [ -z "${workers:-}" ]; then
        say "SKIP $b: unknown tier (name should start with S, M, L or G)"
        continue
    fi

    say "RUN  $b: workers=$workers timeout=${timeout}s"
    start=$(date +%s)
    run_one "$b" "$workers" "$timeout"
    rc=$?
    took=$(( $(date +%s) - start ))
    summary=$(grep '^END ' "$OUT/_logs/$b.out" | tail -1)
    failed=$(failed_in "$b")

    case $rc in
        0|1)
            echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s failed=$failed $summary" > "$marker"
            say "DONE $b in ${took}s (failed repos: $failed): ${summary#END }"
            [ "$failed" -gt 0 ] && with_failures+=("$b")
            ;;
        3)
            disk_low "$b after ${took}s"
            ;;
        *)
            say "FAIL $b exit=$rc after ${took}s - see $OUT/_logs/$b.out; rerun to resume"
            exit "$rc"
            ;;
    esac
done

if [ "$RETRY" = "1" ] && [ "${#with_failures[@]}" -gt 0 ]; then
    say "RETRY failed repos in: ${with_failures[*]}"
    for b in "${with_failures[@]}"; do
        read -r workers timeout <<< "$(settings "$b")"
        run_one "$b" 1 $(( timeout * 3 )) --retry-failed
        [ "$?" = 3 ] && disk_low "$b retry"
        say "RETRY $b: $(failed_in "$b") repo(s) still failed"
    done
fi

say "combining every finished repo"
"$PYTHON" "$SCRIPTS/extract_versions.py" --out "$OUT" --combine-only \
    >> "$OUT/_logs/combine.out" 2>&1 || say "combine failed - see $OUT/_logs/combine.out"
tail -1 "$OUT/_logs/combine.out"
say "runner done - summary: $OUT/summary.md, manifest: $OUT/manifest.csv, files: $OUT/files/"
