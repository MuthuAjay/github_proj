#!/usr/bin/env bash
# run_all_versions.sh - both extraction tracks in one go, each into its own
# folder:
#
#   binary  run_extract_versions.sh -> $BIN_OUT   (files/ + manifest.csv)
#   text    run_text_versions.sh    -> $TEXT_OUT, delta in ${TEXT_OUT}_delta
#
#   ./run_all_versions.sh                  binary, then text
#   PARALLEL=1 ./run_all_versions.sh       both at once (they compete for the
#                                          archive's disk, so each is slower)
#   ONLY=text ./run_all_versions.sh        just one track (binary | text)
#
# Each track keeps its own log and resumes on its own: running this again
# skips whatever either track already finished. Sequential mode stops (exit
# 3) when the binary track stops for disk space, without starting the text
# track; any other failure of the binary track is reported and the text track
# still runs. The exit code is the worst of the two.
#
# Settings (and every setting of the two runners) come from the environment:
#   BIN_OUT=/data/workarea/binary_versions TEXT_OUT=/data/workarea/text9_extract \
#       ./run_all_versions.sh
# Run it detached so an SSH drop does not stop it:
#   nohup ./run_all_versions.sh > /data/workarea/run_all_versions.out 2>&1 &

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
LOGDIR="${LOGDIR:-/data/workarea}"
BIN_OUT="${BIN_OUT:-/data/workarea/binary_versions}"
TEXT_OUT="${TEXT_OUT:-/data/workarea/text9_extract}"
PARALLEL="${PARALLEL:-0}"
ONLY="${ONLY:-}"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

binary() {
    say "BINARY start -> $BIN_OUT  (log $LOGDIR/run_extract_versions.out)"
    OUT="$BIN_OUT" "$SCRIPTS/run_extract_versions.sh" \
        >> "$LOGDIR/run_extract_versions.out" 2>&1
    local rc=$?
    say "BINARY end rc=$rc: $(tail -1 "$LOGDIR/run_extract_versions.out")"
    return $rc
}

text() {
    say "TEXT   start -> $TEXT_OUT, delta ${DELTA:-${TEXT_OUT}_delta}  (log $LOGDIR/run_text_versions.out)"
    OUT="$TEXT_OUT" "$SCRIPTS/run_text_versions.sh" \
        >> "$LOGDIR/run_text_versions.out" 2>&1
    local rc=$?
    say "TEXT   end rc=$rc: $(tail -1 "$LOGDIR/run_text_versions.out")"
    return $rc
}

say "run_all start: ${ONLY:-binary + text}$([ "$PARALLEL" = 1 ] && echo ' (parallel)')"
rc_bin=0
rc_text=0

if [ "$ONLY" = binary ]; then
    binary; rc_bin=$?
elif [ "$ONLY" = text ]; then
    text; rc_text=$?
elif [ "$PARALLEL" = 1 ]; then
    binary & pid_bin=$!
    text & pid_text=$!
    wait "$pid_bin"; rc_bin=$?
    wait "$pid_text"; rc_text=$?
else
    binary; rc_bin=$?
    if [ "$rc_bin" = 3 ]; then
        say "STOP: the binary track stopped for disk space - text not started."
        say "      Free space and run this again; finished work is skipped."
        exit 3
    fi
    text; rc_text=$?
fi

rc=$(( rc_bin > rc_text ? rc_bin : rc_text ))
say "run_all done: binary rc=$rc_bin, text rc=$rc_text"
[ "$rc_bin" = 0 ] && [ -z "$ONLY" -o "$ONLY" = binary ] && say "  binary -> $BIN_OUT/summary.md"
[ "$rc_text" = 0 ] && [ -z "$ONLY" -o "$ONLY" = text ] && say "  text   -> ${DELTA:-${TEXT_OUT}_delta}"
exit "$rc"
