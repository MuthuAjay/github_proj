#!/usr/bin/env bash
# run_group4_2_inner.sh - the files found inside the 4.2 / 4.2_R archives,
# group by group, each with the method its group was delivered with. Reads
# only <run>/unpacked (archive_unpack.py output): no git, no repos.
#
#   step    types                                 script              output folder
#   4.5_R   group4_5_R_extensions.txt (890)       group4_2_text.py    <run>/group4_5_R_text
#   4.5     group4_5_extensions.txt (756)         group4_2_text.py    <run>/group4_5_text
#   2_text  group 2 text (9, TEXT_EXTS)           group4_2_text.py    <run>/group2_text
#   2_bin   group 2 binary (18, BINARY_EXTS)      group4_2_files.py   <run>/group2_binary
#
# Text steps: one .txt per path, every line of every version once (as
# group 1 inside the archives). 2_bin: each distinct content once, the
# file itself. All steps: no delta; vendored, macOS metadata and DocFX
# cache paths left out and listed. Group 1 is done already (group1_text).
#
#   cd /data/workarea/scripts
#   nohup ./run_group4_2_inner.sh > /data/workarea/group4_2_inner.out 2>&1 &
#   tail -f /data/workarea/group4_2_inner.out
#
# Runs one step at a time, 4.2 then 4.2_R inside each step. Run it again
# after a stop and it carries on (repos already done are skipped). When
# both runs are in RUNS, each step's stats of the two runs are summed into
# /data/workarea/group4_2/<step folder>/stats_combined/by_type.csv.
#
# Settings (put them in front, e.g. STEPS="4.5_R" ./run_group4_2_inner.sh):
#   STEPS      steps, space separated (default "4.5_R 4.5 2_text 2_bin")
#   RUNS       runs (default "group4_2 group4_2_R")
#   WORKERS    processes per run, one repo each (default 8)
#   MIN_FREE_GB     stop before a step if less is free (default 50)
#   NAME       log name (default full)
#   EXTRA      more options for the scripts (e.g. "--redo", "--limit 1"
#              = the biggest repo only, to time a step)
# Exit code: 0 = all ok, 1 = a step had failed repos or did not start.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-/data/workarea}"
STEPS="${STEPS:-4.5_R 4.5 2_text 2_bin}"
RUNS="${RUNS:-group4_2 group4_2_R}"
WORKERS="${WORKERS:-8}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
NAME="${NAME:-full}"
EXTRA="${EXTRA:-}"
PYTHON="${PYTHON:-python3}"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

py_list() {     # extension_versions.TEXT_EXTS -> "erb,feature,..."
    "$PYTHON" -c "import sys; sys.path.insert(0, '$HERE')
from extension_versions import $1; print(','.join($1))"
}

# step -> script, output folder name, --extensions value
step_def() {
    case "$1" in
        4.5_R)  echo "group4_2_text.py group4_5_R_text $HERE/group4_5_R_extensions.txt" ;;
        4.5)    echo "group4_2_text.py group4_5_text $HERE/group4_5_extensions.txt" ;;
        2_text) echo "group4_2_text.py group2_text $(py_list TEXT_EXTS)" ;;
        2_bin)  echo "group4_2_files.py group2_binary $(py_list BINARY_EXTS)" ;;
        *)      return 1 ;;
    esac
}

for s in $STEPS; do
    def=$(step_def "$s") || { say "ERROR: unknown step '$s'"; exit 1; }
    set -- $def
    [ -f "$HERE/$1" ] || { say "ERROR: $HERE/$1 not found"; exit 1; }
    case "$3" in /*) [ -f "$3" ] || { say "ERROR: $3 not found"; exit 1; } ;; esac
    [ -n "$3" ] || { say "ERROR: no types for step $s"; exit 1; }
done

say "start: steps [$STEPS], runs [$RUNS], $WORKERS workers"

run_one() {     # step run script outdir exts
    local s="$1" g="$2" script="$3" dir="$4" exts="$5"
    local un="$WORK/$g/unpacked" out="$WORK/$g/$dir"
    if [ ! -d "$un/_state" ]; then
        say "[$s $g] ERROR: $un/_state not found - skipped"
        return 1
    fi
    say "[$s $g] $un -> $out (log: $out/_logs/$NAME.log)"
    # shellcheck disable=SC2086
    "$PYTHON" "$HERE/$script" --unpacked "$un" --out "$out" \
        --extensions "$exts" --workers "$WORKERS" --name "$NAME" $EXTRA
    local rc=$?
    say "[$s $g] finished, exit $rc$([ $rc -ne 0 ] && echo ' - some repos failed, see the log')"
    [ -f "$out/stats/summary.json" ] && { cat "$out/stats/summary.json"; echo; }
    return $rc
}

fail=0
failed_dirs=()
for s in $STEPS; do
    set -- $(step_def "$s")
    script="$1" dir="$2" exts="$3"
    free_gb=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc '0-9')
    say "=== step $s: $script, $free_gb GB free on $WORK"
    if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
        say "ERROR: less than $MIN_FREE_GB GB free - stopped before step $s"
        fail=1
        break
    fi
    outs=()
    for g in $RUNS; do
        run_one "$s" "$g" "$script" "$dir" "$exts" || { fail=1; failed_dirs+=("$WORK/$g/$dir"); }
        outs+=("$WORK/$g/$dir")
    done
    if [ "${#outs[@]}" -gt 1 ]; then
        first="${outs[0]}"
        "$PYTHON" "$HERE/$script" --unpacked "$WORK/${RUNS%% *}/unpacked" \
            --out "$first" --name "$NAME" --stats-only --combine "${outs[@]}" \
            > /dev/null && say "[$s] combined -> $first/stats_combined/by_type.csv"
    fi
done

if [ "$fail" = 0 ]; then
    say "ALL DONE"
else
    say "FINISHED WITH PROBLEMS - failed repos:"
    for d in "${failed_dirs[@]}"; do
        grep -L '"status": "ok"' "$d"/_state/*/*/done.json 2>/dev/null
    done
fi
exit "$fail"
