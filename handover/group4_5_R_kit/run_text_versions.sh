#!/usr/bin/env bash
# run_text_versions.sh - the text track: for the text extensions, every line
# any version ever had, minus the lines still in the active file.
#
#   1. extract   file_added_lines.py per batch (plan.csv order), only $EXTS
#                -> $OUT/<org>/<repo>/<path>.txt, one line per distinct line
#   2. at_head   fill_at_head.py from the active copy (--disk-root)
#   3. delta     file_delta.py -> $DELTA: what is left to process (send this)
#
#   ./run_text_versions.sh                 all three steps, every batch
#   ./run_text_versions.sh S01 S02         step 1 for just these batches (steps
#                                          2 and 3 run only once every batch
#                                          in plan.csv is done)
#
# Each step leaves a marker in $OUT/_logs (<batch>.text_done, fill_at_head.done,
# delta.done) and is skipped next time; a step that stopped half way resumes
# (file_added_lines.py and file_delta.py at repo level, fill_at_head.py
# simply rewrites every manifest again).
#
# Repos that fail in step 1 do not stop the run: after the last batch one
# --retry-failed pass per batch with those repos is made, with more time.
# The runner STOPS (exit 3) when a step says so - low disk in step 1, the
# active copy's mount not answering in steps 2 and 3. Fix that and run this
# again. It also stops when a step crashes (any other non-zero exit, except
# 1 = "finished, some repos failed", which is logged and carried on from).
#
# Settings can be overridden from the environment:
#   OUT=/data/workarea/text9_extract MIN_DISK_GB=50 ./run_text_versions.sh
#   EXTS="tfvars bicep" OUT=/data/workarea/tf_extract ./run_text_versions.sh
#   SKIP_VENDORED=1 leaves out files in node_modules, packages, vendor, bin,
#   obj, dist, build, ... (file_added_lines.py --skip-vendored)
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_text_versions.sh > /data/workarea/run_text_versions.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
OUT="${OUT:-/data/workarea/text9_extract}"
DELTA="${DELTA:-${OUT}_delta}"
EXTS="${EXTS:-erb feature bicep lock rst tfvars azcli groovy xcconfig}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
WORKERS_AT_HEAD="${WORKERS_AT_HEAD:-16}"
WORKERS_DELTA="${WORKERS_DELTA:-16}"
RETRY="${RETRY:-1}"                 # 0 = skip the --retry-failed pass
SKIP_VENDORED="${SKIP_VENDORED:-0}" # 1 = leave out vendored paths
PYTHON="${PYTHON:-python3}"

# workers, per-repo timeout (seconds) - by tier, as run_batches.sh
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

failed_in() {  # batch -> repos of the batch whose extraction did not succeed
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

extract() {    # batch, workers, timeout, extra flags...
    local b="$1" workers="$2" timeout="$3"
    shift 3
    "$PYTHON" "$SCRIPTS/file_added_lines.py" --batch "$BATCHES/$b.csv" \
        --repos-root "$ROOT" --out "$OUT" --extensions "$EXTFILE" \
        --workers "$workers" --repo-timeout "$timeout" \
        --min-free-disk-gb "$MIN_DISK_GB" --quiet \
        $([ "$SKIP_VENDORED" = 1 ] && echo --skip-vendored) "$@" \
        >> "$OUT/_logs/$b.text.out" 2>&1
}

stop() {       # why
    say "STOP $1"
    say "     Fix that and run this script again - finished steps, batches and repos are skipped."
    exit 3
}

active_up() {  # the active copy must be there AND not empty: an unmounted
               # blobfuse mount point is an empty folder, which steps 2 and 3
               # would read as "no repo has an active copy" - silently wrong
    [ -d "$ACTIVE" ] && [ -n "$(ls -A "$ACTIVE" 2>/dev/null | head -1)" ] \
        || stop "the active copy is missing or empty: $ACTIVE - is it mounted?"
}

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi
mapfile -t all < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)

mkdir -p "$OUT/_logs"
EXTFILE="$OUT/_logs/text_extensions.txt"
printf '%s\n' $EXTS > "$EXTFILE"
say "runner start: ${#list[@]} batch(es): ${list[*]}"
say "extensions: $(tr '\n' ' ' < "$EXTFILE")| out=$OUT delta=$DELTA active=$ACTIVE skip_vendored=$SKIP_VENDORED"

# ---------------------------------------------------------------- 1. extract
say "=== STEP 1: extract"
with_failures=()
for b in "${list[@]}"; do
    marker="$OUT/_logs/$b.text_done"
    if [ ! -f "$BATCHES/$b.csv" ]; then
        say "SKIP $b: no batch file $BATCHES/$b.csv"
        continue
    fi
    if [ -f "$marker" ]; then
        say "SKIP $b: already extracted ($(cat "$marker"))"
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
    extract "$b" "$workers" "$timeout"
    rc=$?
    took=$(( $(date +%s) - start ))
    summary=$(grep '^END ' "$OUT/_logs/$b.text.out" | tail -1)
    case $rc in
        0)
            failed=$(failed_in "$b")
            echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s failed=$failed $summary" > "$marker"
            say "DONE $b in ${took}s (failed repos: $failed): ${summary#END }"
            [ "$failed" -gt 0 ] && with_failures+=("$b")
            ;;
        3)
            stop "$b stopped early after ${took}s (disk below ${MIN_DISK_GB} GB?): ${summary#END }"
            ;;
        *)
            say "FAIL $b exit=$rc after ${took}s - see $OUT/_logs/$b.text.out; rerun to resume"
            exit "$rc"
            ;;
    esac
done

if [ "$RETRY" = "1" ] && [ "${#with_failures[@]}" -gt 0 ]; then
    say "RETRY failed repos in: ${with_failures[*]}"
    for b in "${with_failures[@]}"; do
        [ -f "$OUT/_logs/$b.retried" ] && continue
        read -r workers timeout <<< "$(settings "$b")"
        extract "$b" 1 $(( timeout * 3 )) --retry-failed
        [ "$?" = 3 ] && stop "$b retry stopped early (disk?)"
        date '+%Y-%m-%d %H:%M:%S' > "$OUT/_logs/$b.retried"
        say "RETRY $b: $(failed_in "$b") repo(s) still failed (repo_missing ones stay)"
    done
fi

pending=()
for b in "${all[@]}"; do
    [ -f "$OUT/_logs/$b.text_done" ] || pending+=("$b")
done
if [ "${#pending[@]}" -gt 0 ]; then
    say "steps 2 and 3 wait until every batch is extracted - not yet: ${pending[*]}"
    exit 0
fi

# ---------------------------------------------------------------- 2. at_head
say "=== STEP 2: at_head from the active copy"
active_up
if [ -f "$OUT/_logs/fill_at_head.done" ]; then
    say "SKIP at_head: already done ($(cat "$OUT/_logs/fill_at_head.done"))"
else
    start=$(date +%s)
    "$PYTHON" "$SCRIPTS/fill_at_head.py" "$OUT" --disk-root "$ACTIVE" \
        --workers "$WORKERS_AT_HEAD" >> "$OUT/_logs/fill_at_head.out" 2>&1
    rc=$?
    took=$(( $(date +%s) - start ))
    case $rc in
        0|1)
            echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s rc=$rc" > "$OUT/_logs/fill_at_head.done"
            say "DONE at_head in ${took}s (rc=$rc$([ $rc = 1 ] && echo ', some repos failed - file_delta looks those files up itself'))"
            ;;
        3) stop "at_head: the active copy stopped answering ($ACTIVE)" ;;
        *) say "FAIL at_head exit=$rc - see $OUT/_logs/fill_at_head.out"; exit "$rc" ;;
    esac
fi

# ---------------------------------------------------------------- 3. delta
say "=== STEP 3: delta -> $DELTA"
active_up
if [ -f "$OUT/_logs/delta.done" ]; then
    say "SKIP delta: already done ($(cat "$OUT/_logs/delta.done"))"
else
    start=$(date +%s)
    "$PYTHON" "$SCRIPTS/file_delta.py" "$OUT" --active-root "$ACTIVE" \
        --out "$DELTA" --workers "$WORKERS_DELTA" >> "$OUT/_logs/delta.out" 2>&1
    rc=$?
    took=$(( $(date +%s) - start ))
    case $rc in
        0|1)
            echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s rc=$rc" > "$OUT/_logs/delta.done"
            say "DONE delta in ${took}s (rc=$rc)"
            grep -E '^(END|files |files by action)' "$OUT/_logs/delta.out" | tail -3
            ;;
        3) stop "delta: the active copy stopped answering ($ACTIVE)" ;;
        *) say "FAIL delta exit=$rc - see $OUT/_logs/delta.out"; exit "$rc" ;;
    esac
fi

say "runner done - send $DELTA (its _state/*/manifest.csv says what each file is)"
