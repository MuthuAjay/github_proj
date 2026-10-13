#!/usr/bin/env bash
# run_group4_2_text.sh - group4_2_text.py for both runs: the group 1 text
# found inside the 4.2 archives, then inside the 4.2_R archives (one after
# the other, so the two do not fight over the disk).
#
#   cd /data/workarea/scripts
#   nohup ./run_group4_2_text.sh > /data/workarea/group4_2_text.out 2>&1 &
#   tail -f /data/workarea/group4_2_text.out
#
# Progress per repo is in <run>/group1_text/_logs/$NAME.log. Run it again
# after a stop and it carries on (repos already done are skipped).
#
# Settings (put them in front, e.g. WORKERS=12 ./run_group4_2_text.sh):
#   WORKERS    processes per run, one repo each (default 8)
#   RUNS       which runs, space separated (default "group4_2 group4_2_R")
#   BOTH_AT_ONCE=1  start both runs together instead of one after another
#   MIN_FREE_GB     stop before starting if less is free (default 50)
#   NAME       log name (default full)
#   EXTRA      more options for group4_2_text.py (e.g. "--redo")
# Exit code: 0 = all runs ok, 1 = a run had failed repos or did not start.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-/data/workarea}"
RUNS="${RUNS:-group4_2 group4_2_R}"
WORKERS="${WORKERS:-8}"
BOTH_AT_ONCE="${BOTH_AT_ONCE:-0}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
NAME="${NAME:-full}"
EXTRA="${EXTRA:-}"
PYTHON="${PYTHON:-python3}"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

free_gb=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc '0-9')
say "start: runs [$RUNS], $WORKERS workers each, $free_gb GB free on $WORK"
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
    say "ERROR: less than $MIN_FREE_GB GB free - free space or set MIN_FREE_GB"
    exit 1
fi

run_one() {
    local g="$1"
    local un="$WORK/$g/unpacked" out="$WORK/$g/group1_text"
    if [ ! -d "$un/_state" ]; then
        say "[$g] ERROR: $un/_state not found - skipped"
        return 1
    fi
    say "[$g] $un -> $out (log: $out/_logs/$NAME.log)"
    # shellcheck disable=SC2086
    "$PYTHON" "$HERE/group4_2_text.py" --unpacked "$un" --out "$out" \
        --workers "$WORKERS" --name "$NAME" $EXTRA 2>/dev/null
    local rc=$?
    say "[$g] finished, exit $rc$([ $rc -ne 0 ] && echo ' - some repos failed, see the log')"
    [ -f "$out/stats/summary.json" ] && { cat "$out/stats/summary.json"; echo; }
    return $rc
}

fail=0
if [ "$BOTH_AT_ONCE" = 1 ]; then
    pids=()
    for g in $RUNS; do run_one "$g" & pids+=($!); done
    for p in "${pids[@]}"; do wait "$p" || fail=1; done
else
    for g in $RUNS; do run_one "$g" || fail=1; done
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE"
else
    say "FINISHED WITH PROBLEMS - failed repos:"
    for g in $RUNS; do
        grep -L '"status": "ok"' "$WORK/$g"/group1_text/_state/*/*/done.json 2>/dev/null
    done
fi
exit "$fail"
