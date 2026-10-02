#!/usr/bin/env bash
# run_group3.sh - group 3 (pdf, pkl), per repo, from the archive to what is
# sent:
#
#   1. counts    run_ext_versions.sh, pdf + pkl as binary (pass 1 + 2)
#                -> $COUNTS
#   2. extract   run_extract_versions.sh --per-repo: every version not in the
#                same repo's active copy, each content once per repo
#                -> $EXTRACT/files/<ext>/<org>/<repo>/<blob>.<ext>
#   3. pkl       pkl_to_text.py: one .txt per .pkl path, every string once
#                (today's file included unless PKL_ACTIVE=0) -> $PKL_OUT
#   4. pdf       pdf_page_merge.py: one PDF per .pdf path, every distinct page
#                once, original pages kept -> $PDF_OUT
#
#   ./run_group3.sh                every batch in plan.csv order
#   ./run_group3.sh S01            just these batches (each step for them)
#   STEPS="pkl pdf" ./run_group3.sh    only some steps (the others done)
#
# Every step resumes: finished repos are skipped, so after a stop run the
# same command again. Steps 3 and 4 leave $OUT/_logs/<batch>.done per batch.
# Exit codes: 0 done; 1 done, some repos failed (named in the logs);
# 3 stopped (disk low / the active copy's mount not answering) - fix it and
# run again; anything else: a step crashed, see its log.
#
#   nohup ./run_group3.sh > /data/workarea/run_group3.out 2>&1 &
#
# Steps 3 and 4 need: pip install pypdfium2 pypdf (pdf only; pkl needs
# nothing). Use PYTHON=/path/to/venv/bin/python3 if they are in a venv.

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
BATCHES="${BATCHES:-/data/workarea/file_history_out_4/batches}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
COUNTS="${COUNTS:-/data/workarea/ext_versions_g3}"
EXTRACT="${EXTRACT:-/data/workarea/binary_versions_g3}"
PKL_OUT="${PKL_OUT:-/data/workarea/pkl_text_g3}"
PDF_OUT="${PDF_OUT:-/data/workarea/pdf_merged_g3}"
STEPS="${STEPS:-counts extract pkl pdf}"
WORKERS="${WORKERS:-8}"               # steps 3 and 4: repos at once
PKL_ACTIVE="${PKL_ACTIVE:-1}"         # 1 = include today's .pkl in the text
PDF_DROP_ACTIVE="${PDF_DROP_ACTIVE:-0}"   # 1 = drop pages identical to today's
MIN_DISK_GB="${MIN_DISK_GB:-20}"
PYTHON="${PYTHON:-python3}"
export SCRIPTS BATCHES ROOT ACTIVE PYTHON MIN_DISK_GB

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

if [ "$#" -gt 0 ]; then
    list=("$@")
else
    mapfile -t list < <(tail -n +2 "$BATCHES/plan.csv" | cut -d, -f1)
fi
say "group 3 start: steps '$STEPS', ${#list[@]} batch(es): ${list[*]}"
say "counts=$COUNTS extract=$EXTRACT pkl=$PKL_OUT pdf=$PDF_OUT"
failed=0

step_rc() {   # step name, exit code
    case "$2" in
        0) say "DONE $1" ;;
        1) say "DONE $1 - some repos failed (see its logs)"; failed=1 ;;
        3) say "STOP $1: disk low or the active copy not answering - fix it and run this again"
           exit 3 ;;
        *) say "FAIL $1 exit=$2 - see its logs; rerun to resume"; exit "$2" ;;
    esac
}

for step in $STEPS; do
    case "$step" in
    counts)
        say "=== 1. counts (pdf, pkl as binary) -> $COUNTS"
        EXTS="pdf pkl" BINARY_EXTS="pdf pkl" OUT="$COUNTS" \
            "$SCRIPTS/run_ext_versions.sh" "$@"
        step_rc counts $?
        ;;
    extract)
        say "=== 2. extract per repo -> $EXTRACT"
        STATE="$COUNTS" OUT="$EXTRACT" EXTRA="--extensions pdf,pkl --per-repo" \
            "$SCRIPTS/run_extract_versions.sh" "$@"
        step_rc extract $?
        ;;
    pkl|pdf)
        if [ "$step" = pkl ]; then
            out="$PKL_OUT"; script=pkl_to_text.py; extra=()
            [ "$PKL_ACTIVE" = 1 ] && extra=(--active-root "$ACTIVE")
            say "=== 3. pkl -> text -> $PKL_OUT (today's file included: $PKL_ACTIVE)"
        else
            out="$PDF_OUT"; script=pdf_page_merge.py; extra=()
            [ "$PDF_DROP_ACTIVE" = 1 ] && extra=(--active-root "$ACTIVE" --drop-active-pages)
            say "=== 4. pdf -> distinct pages merged -> $PDF_OUT (drop today's pages: $PDF_DROP_ACTIVE)"
        fi
        mkdir -p "$out/_logs"
        rc_step=0
        for b in "${list[@]}"; do
            if [ -f "$out/_logs/$b.done" ]; then
                say "SKIP $step $b: done ($(cat "$out/_logs/$b.done"))"
                continue
            fi
            start=$(date +%s)
            "$PYTHON" "$SCRIPTS/$script" --in "$EXTRACT" --out "$out" \
                --batch "$BATCHES/$b.csv" --workers "$WORKERS" ${extra[@]+"${extra[@]}"} \
                >> "$out/_logs/$b.out" 2>&1
            rc=$?
            took=$(( $(date +%s) - start ))
            case $rc in
                0|1) echo "$(date '+%Y-%m-%d %H:%M:%S') ${took}s rc=$rc" > "$out/_logs/$b.done"
                     say "DONE $step $b in ${took}s: $(grep '^END' "$out/_logs/$b.out" | tail -1)"
                     [ "$rc" = 1 ] && rc_step=1 ;;
                *)   say "FAIL $step $b exit=$rc - see $out/_logs/$b.out"; exit "$rc" ;;
            esac
        done
        step_rc "$step" "$rc_step"
        ;;
    *)
        say "unknown step '$step' (counts extract pkl pdf)"; exit 2 ;;
    esac
done

say "group 3 done - send: $PDF_OUT and $PKL_OUT (without _state/_logs: use copy_group3.sh)"
exit "$failed"
