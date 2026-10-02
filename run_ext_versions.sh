#!/usr/bin/env bash
# run_ext_versions.sh - run extension_versions.py over every batch: pass 1
# (versions per extension, at_head) for all batches, then pass 2 (--identical:
# is the active file one of its versions) for all batches, then one combine.
#
#   ./run_ext_versions.sh                  every batch in plan.csv order
#   ./run_ext_versions.sh S01 S02          just these, in this order
#   PASSES=1 ./run_ext_versions.sh         pass 1 only (PASSES=2: pass 2 only)
#
# Other extensions than group 2's 27 (e.g. group 3, in its own folder):
#   EXTS="pdf pkl" BINARY_EXTS="pdf pkl" OUT=/data/workarea/ext_versions_g3 \
#       ./run_ext_versions.sh
# EXTS = the extensions to count; BINARY_EXTS = those of them to treat as
# binary (pass 2 compares them with the active copy; remembered in
# $OUT/binary_exts.txt). Use the same values when resuming a run.
#
# Per batch it runs with the tier's workers and timeout (from the batch
# name's first letter). A finished batch is marked with
# $OUT/_logs/<batch>.pass1_done / .identical_done and skipped next time; a
# batch that stopped half way resumes at repo level.
#
# Repos that fail or time out do not stop the run: the batch is still marked
# done (its log names them), and after each pass one --retry-failed pass per
# batch with those repos is made, with more time. The runner STOPS (exit 3)
# when the active copy's mount stops answering - remount it and run this
# again, it carries on where it stopped. It also stops when a batch itself
# crashes (exit code other than 0, 1 or 3).
#
# Both passes read the archive and the active copy (the mount must be up);
# nothing is written to either.
#
# Settings can be overridden from the environment:
#   OUT=/data/workarea/ext_versions ./run_ext_versions.sh
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_ext_versions.sh > /data/workarea/run_ext_versions.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
OUT="${OUT:-/data/workarea/ext_versions}"
PASSES="${PASSES:-1 2}"
RETRY="${RETRY:-1}"                 # 0 = skip the --retry-failed passes
PYTHON="${PYTHON:-python3}"
EXTS="${EXTS:-}"                    # empty = the script's default (27)
BINARY_EXTS="${BINARY_EXTS:-}"
EXT_ARGS=()
[ -n "$EXTS" ] && EXT_ARGS+=(--extensions "$(echo $EXTS | tr ' ' ',')")
[ -n "$BINARY_EXTS" ] && EXT_ARGS+=(--binary-exts "$(echo $BINARY_EXTS | tr ' ' ',')")

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

failed_in() {  # batch, marker file -> repos whose last attempt did not succeed
    "$PYTHON" - "$BATCHES/$1.csv" "$OUT/_state" "$2" <<'PY'
import csv, json, os, sys
batch, state, marker = sys.argv[1], sys.argv[2], sys.argv[3]
n = 0
for r in csv.DictReader(open(batch, newline="", encoding="utf-8-sig")):
    try:
        with open(os.path.join(state, r["org"].strip(), r["repo"].strip(),
                               marker), encoding="utf-8") as fh:
            n += json.load(fh).get("status") != "ok"
    except (OSError, ValueError, KeyError):
        pass          # not attempted yet (pass 2: pass 1 not ok)
print(n)
PY
}

run_one() {    # pass, batch, workers, timeout, extra flags...
    local pass="$1" b="$2" workers="$3" timeout="$4" log
    shift 4
    if [ "$pass" = 2 ]; then
        set -- --identical "$@"
        log="$OUT/_logs/$b.identical.out"
    else
        log="$OUT/_logs/$b.out"
    fi
    "$PYTHON" "$SCRIPTS/extension_versions.py" \
        --repos-root "$ROOT" --active-root "$ACTIVE" --out "$OUT" \
        --batch "$BATCHES/$b.csv" --workers "$workers" \
        --repo-timeout "$timeout" --no-combine ${EXT_ARGS[@]+"${EXT_ARGS[@]}"} \
        "$@" >> "$log" 2>&1
}

mount_down() {
    say "STOP $1: the active copy is not answering ($ACTIVE)."
    say "     Remount it and run this script again - finished batches and repos are skipped."
    exit 3
}

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi

mkdir -p "$OUT/_logs"
say "runner start: passes '$PASSES', ${#list[@]} batch(es): ${list[*]}"
say "extensions: ${EXTS:-default 27}${BINARY_EXTS:+ | also binary: $BINARY_EXTS} | out=$OUT"

for pass in $PASSES; do
    if [ "$pass" = 2 ]; then
        suffix=identical_done; marker_json=identical.json; outname=identical.out
    else
        suffix=pass1_done; marker_json=done.json; outname=out
    fi
    with_failures=()
    say "=== PASS $pass"

    for b in "${list[@]}"; do
        marker="$OUT/_logs/$b.$suffix"
        if [ ! -f "$BATCHES/$b.csv" ]; then
            say "SKIP $b: no batch file $BATCHES/$b.csv"
            continue
        fi
        if [ -f "$marker" ]; then
            say "SKIP $b: pass $pass already finished ($(cat "$marker"))"
            [ "$(failed_in "$b" "$marker_json")" -gt 0 ] && with_failures+=("$b")
            continue
        fi
        read -r workers timeout <<< "$(settings "$b")"
        if [ -z "${workers:-}" ]; then
            say "SKIP $b: unknown tier (name should start with S, M, L or G)"
            continue
        fi

        say "RUN  $b pass $pass: workers=$workers timeout=${timeout}s"
        start=$(date +%s)
        run_one "$pass" "$b" "$workers" "$timeout"
        rc=$?
        took=$(( $(date +%s) - start ))
        summary=$(grep '^END ' "$OUT/_logs/$b.$outname" | tail -1)
        failed=$(failed_in "$b" "$marker_json")

        case $rc in
            0|1)
                echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s failed=$failed $summary" > "$marker"
                say "DONE $b pass $pass in ${took}s (failed repos: $failed): ${summary#END }"
                [ "$failed" -gt 0 ] && with_failures+=("$b")
                ;;
            3)
                mount_down "$b pass $pass after ${took}s"
                ;;
            *)
                say "FAIL $b pass $pass exit=$rc after ${took}s - see $OUT/_logs/$b.$outname; rerun to resume"
                exit "$rc"
                ;;
        esac
    done

    if [ "$RETRY" = "1" ] && [ "${#with_failures[@]}" -gt 0 ]; then
        say "RETRY pass $pass, failed repos in: ${with_failures[*]}"
        for b in "${with_failures[@]}"; do
            read -r workers timeout <<< "$(settings "$b")"
            run_one "$pass" "$b" 1 $(( timeout * 3 )) --retry-failed
            rc=$?
            [ "$rc" = 3 ] && mount_down "$b pass $pass retry"
            say "RETRY $b pass $pass: $(failed_in "$b" "$marker_json") repo(s) still failed"
        done
    fi
done

say "combining every finished repo"
"$PYTHON" "$SCRIPTS/extension_versions.py" --out "$OUT" --combine-only \
    >> "$OUT/_logs/combine.out" 2>&1 || say "combine failed - see $OUT/_logs/combine.out"
say "runner done - summary: $OUT/summary.md, table: $OUT/by_extension.csv"
