#!/usr/bin/env bash
# run_all_ext_counts.sh - run all_extension_counts.py over every batch: every
# file extension and how many files with it the full history ever held
# ($OUT/extension_counts.csv).
#
#   ./run_all_ext_counts.sh                  every batch in plan.csv order
#   ./run_all_ext_counts.sh S01 S02          just these, in this order
#   MODE=full ./run_all_ext_counts.sh        also the active copy (at head /
#                                            history only) and sizes; needs the
#                                            mount and takes longer
#
# By default (MODE=history) only the archive is read - the active copy's
# mount is not needed. Writes only to $OUT. A finished batch is marked with $OUT/_logs/<batch>.counts_done
# and skipped next time; a batch that stopped half way resumes at repo level.
# Repos that fail or time out do not stop the run: after the last batch one
# --retry-failed pass per batch with those repos is made, with more time.
# The runner STOPS (exit 3) when the active copy's mount does not answer or
# is empty (an unmounted blobfuse mount point is an empty folder) - remount
# and run it again. It also stops when a batch crashes (exit other than 0, 1).
#
# Settings can be overridden from the environment:
#   OUT=/data/workarea/all_ext_counts ./run_all_ext_counts.sh
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_all_ext_counts.sh > /data/workarea/run_all_ext_counts.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
OUT="${OUT:-/data/workarea/all_ext_counts}"
RETRY="${RETRY:-1}"                 # 0 = skip the final --retry-failed pass
MODE="${MODE:-history}"             # history | full
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

stop() {
    say "STOP $1"
    say "     Fix that and run this script again - finished batches and repos are skipped."
    exit 3
}

active_up() {
    [ -d "$ACTIVE" ] && [ -n "$(ls -A "$ACTIVE" 2>/dev/null | head -1)" ] \
        || stop "the active copy is missing or empty: $ACTIVE - is it mounted?"
}

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
    if [ "$MODE" = full ]; then
        set -- --active-root "$ACTIVE" "$@"
    else
        set -- --history-only "$@"
    fi
    "$PYTHON" "$SCRIPTS/all_extension_counts.py" \
        --repos-root "$ROOT" --out "$OUT" \
        --batch "$BATCHES/$b.csv" --workers "$workers" \
        --repo-timeout "$timeout" --no-combine "$@" >> "$OUT/_logs/$b.out" 2>&1
}

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi

mkdir -p "$OUT/_logs"
say "runner start: ${#list[@]} batch(es): ${list[*]} (mode $MODE)"
[ "$MODE" = full ] && active_up
with_failures=()

for b in "${list[@]}"; do
    marker="$OUT/_logs/$b.counts_done"
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
        3) stop "$b after ${took}s: the active copy stopped answering ($ACTIVE)" ;;
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
        [ "$?" = 3 ] && stop "$b retry: the active copy stopped answering"
        say "RETRY $b: $(failed_in "$b") repo(s) still failed"
    done
fi

say "combining every finished repo"
"$PYTHON" "$SCRIPTS/all_extension_counts.py" --out "$OUT" --combine-only \
    >> "$OUT/_logs/combine.out" 2>&1 || say "combine failed - see $OUT/_logs/combine.out"
tail -1 "$OUT/_logs/combine.out"
say "runner done - ext / files: $OUT/extension_counts.csv (details: by_extension.csv, summary.md)"
