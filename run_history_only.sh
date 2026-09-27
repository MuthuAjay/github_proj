#!/usr/bin/env bash
# run_history_only.sh - run history_only_files.py over every batch, in order.
#
#   ./run_history_only.sh                  every batch in plan.csv order (S, M, L, G)
#   ./run_history_only.sh G01 G02          just these, in this order
#
# Per batch it runs history_only_files.py with the tier's workers and timeout
# (from the batch name's first letter); the giant batches (G*) run with
# --no-renames. A finished batch is marked with $OUT/_logs/<batch>.batch_done
# and skipped next time; a batch that stopped half way resumes at repo level.
#
# Repos that fail or time out do not stop the run: the batch is still marked
# done (its log names them), and once every batch has run, one
# --retry-failed pass per batch with those repos is made, with more time.
# The runner stops only when a batch itself crashes (exit code other than
# 0 or 1). history_only_files.py reads the archive repos only - the blob
# mount is not needed.
#
# Settings can be overridden from the environment:
#   OUT=/data/workarea/history_only ./run_history_only.sh
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_history_only.sh > /data/workarea/run_history_only.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
SRC="${SRC:-/data/workarea/full_extract}"
ROOT="${ROOT:-/data/workarea/archive}"
OUT="${OUT:-/data/workarea/history_only}"
RETRY="${RETRY:-1}"                 # 0 = skip the final --retry-failed pass
PYTHON="${PYTHON:-python3}"

# workers, per-repo timeout (seconds), extra flags - by tier
settings() {
    case "$1" in
        S*) echo "16 1800 " ;;
        M*) echo "8 3600 " ;;
        L*) echo "8 14400 " ;;
        G*) echo "2 43200 --no-renames" ;;
        *)  echo "" ;;
    esac
}

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

failed_in() {  # batch -> number of its repos whose last attempt did not succeed
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
        pass          # never attempted (e.g. no manifest in the source)
print(n)
PY
}

run_one() {    # batch, workers, timeout, extra flags...
    local b="$1" workers="$2" timeout="$3"
    shift 3
    "$PYTHON" "$SCRIPTS/history_only_files.py" "$SRC" \
        --repos-root "$ROOT" --out "$OUT" --batch "$BATCHES/$b.csv" \
        --workers "$workers" --repo-timeout "$timeout" "$@" \
        >> "$OUT/_logs/$b.out" 2>&1
}

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi

mkdir -p "$OUT/_logs"
say "runner start: ${#list[@]} batch(es): ${list[*]}"
with_failures=()

for b in "${list[@]}"; do
    marker="$OUT/_logs/$b.batch_done"
    if [ ! -f "$BATCHES/$b.csv" ]; then
        say "SKIP $b: no batch file $BATCHES/$b.csv"
        continue
    fi
    if [ -f "$marker" ]; then
        say "SKIP $b: already finished ($(cat "$marker"))"
        [ "$(failed_in "$b")" -gt 0 ] && with_failures+=("$b")
        continue
    fi
    read -r workers timeout extra <<< "$(settings "$b")"
    if [ -z "${workers:-}" ]; then
        say "SKIP $b: unknown tier (name should start with S, M, L or G)"
        continue
    fi

    say "RUN  $b: workers=$workers timeout=${timeout}s ${extra:-}"
    start=$(date +%s)
    # shellcheck disable=SC2086
    run_one "$b" "$workers" "$timeout" $extra
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
        *)
            say "FAIL $b exit=$rc after ${took}s - see $OUT/_logs/$b.out; rerun to resume"
            exit "$rc"
            ;;
    esac
done

if [ "$RETRY" = "1" ] && [ "${#with_failures[@]}" -gt 0 ]; then
    say "RETRY failed repos in: ${with_failures[*]}"
    for b in "${with_failures[@]}"; do
        read -r workers timeout extra <<< "$(settings "$b")"
        # shellcheck disable=SC2086
        run_one "$b" 1 $(( timeout * 3 )) --retry-failed $extra
        say "RETRY $b: $(failed_in "$b") repo(s) still failed - $(grep '^END ' "$OUT/_logs/$b.out" | tail -1 | cut -c1-140)"
    done
fi

say "runner done - summary: $OUT/summary.md"
