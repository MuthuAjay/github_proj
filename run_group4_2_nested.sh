#!/usr/bin/env bash
# run_group4_2_nested.sh - open everything inside the 4.2 / 4.2_R archives,
# all the way down: the first unpack opened archives (zip, tar, 7z...) inside
# archives up to 3 levels and kept the 4.2_R packages found inside (msapp,
# sppkg, vsix...) whole. Only the repos where something was left closed are
# redone, in place, then the earlier steps run again for those repos.
#
#   find     group4_2_nested.py find, per run -> $WORK/<run>/nested/todo.csv
#            (+ todo_files.csv, todo_by_extension.csv). Kept on a rerun
#            (FORCE_FIND=1 to look again)
#   backup   each todo repo's _state (manifest, archives, done.json) copied
#            to <run>/nested/before/<org>/<repo>, then its done.json removed
#            so the unpack does it again (a repo already copied is left alone)
#   unpack   archive_unpack.py on the todo repos, --nested-extensions
#            (archive types + the 19 4.2_R types), --max-depth $MAX_DEPTH;
#            resumes: repos done are skipped. LIMIT=1 = the biggest only
#   compare  group4_2_nested.py compare: before / after per repo
#            -> <run>/nested/compare.csv
#   --- after checking compare (not in the default STEPS):
#   snapshot the todo repos' current outputs in the step folders (below),
#            as hard links (no extra space) -> <run>/nested/before_out/<step>
#            /<org>/<repo>; once (marker before_out/.done). Needed by reset
#   reset    removes those repos' done.json in the steps' folders (group1_text,
#            group4_5_R_text, group4_5_text, group2_text, group2_binary,
#            group3_pkl_text, group3_pdf_merged): the next run does them again
#   inner    run_group4_2_text.sh then run_group4_2_inner.sh (only the reset
#            repos are done; stats rebuilt over all repos)
#   lists    4.1 and 4.1_R path lists inside the archives again
#   delta    group4_2_nested.py delta: per step, the files new / changed /
#            gone against the snapshot -> <run>/nested/delta/<step>.csv +
#            summary.csv = what is left to send (unchanged files are not)
#
#   cd /data/workarea/scripts
#   STEPS="find backup unpack" LIMIT=1 ./run_group4_2_nested.sh   time test
#   nohup ./run_group4_2_nested.sh > /data/workarea/group4_2_nested.out 2>&1 &
#   STEPS="snapshot reset inner lists delta" nohup ./run_group4_2_nested.sh > /data/workarea/group4_2_nested_inner.out 2>&1 &
#
# Settings: RUNS ("group4_2 group4_2_R"), STEPS, MAX_DEPTH (20), WORKERS (8),
# MIN_DISK_GB (20), LIMIT, FORCE_FIND, NAME (nested), PYTHON, PDF_PYTHON.
# Exit codes: 0 done; 1 some repos failed; 3 stopped (disk low or the active
# copy not answering) - fix it and run the same command again.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-/data/workarea}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
RUNS="${RUNS:-group4_2 group4_2_R}"
STEPS="${STEPS:-find backup unpack compare}"
MAX_DEPTH="${MAX_DEPTH:-20}"
OLD_DEPTH="${OLD_DEPTH:-3}"
WORKERS="${WORKERS:-8}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
LIMIT="${LIMIT:-0}"
FORCE_FIND="${FORCE_FIND:-0}"
NAME="${NAME:-nested}"
PYTHON="${PYTHON:-python3}"
export PDF_PYTHON="${PDF_PYTHON:-$PYTHON}"

# the types each run opened at the top (as run_group4_2.sh was run)
EXTS_group4_2="zip gz gzip tar tgz bz2 xz z lzma 7z rar zst lz4 zipx cab"
EXTS_group4_2_R="001 002 003 br dxp ez hpi ispac msapp pbiviz ppkg skill sppkg ucs vhd vsix wsp xap zap"
# opened when found inside, in both runs: every archive type + the 4.2_R ones
NESTED=$("$PYTHON" -c "import sys; sys.path.insert(0, '$HERE')
from archive_unpack import ARCHIVE_EXTS
print(','.join(sorted(ARCHIVE_EXTS | set('$EXTS_group4_2_R'.split()))))") || exit 1
STEP_DIRS="group1_text group4_5_R_text group4_5_text group2_text group2_binary group3_pkl_text group3_pdf_merged"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
repos_of() { tail -n +2 "$1" | cut -d, -f1,2 | tr , /; }
fail=0
say "start: steps [$STEPS], runs [$RUNS], max depth $MAX_DEPTH, nested types $NESTED"

for step in $STEPS; do
  case "$step" in
  find|backup|unpack|compare|snapshot|reset|delta)
    for g in $RUNS; do
        un="$WORK/$g/unpacked" nd="$WORK/$g/nested"
        todo="$nd/todo.csv"
        [ -d "$un/_state" ] || { say "[$g] ERROR: $un/_state not found"; exit 1; }
        if [ "$step" != find ] && [ ! -f "$todo" ]; then
            say "[$g] ERROR: $todo not found - run the find step first"; exit 1
        fi
        if [ "$step" != find ] && [ "$(wc -l < "$todo")" -le 1 ]; then
            say "[$g] $step: nothing to redo"; continue
        fi
        case "$step" in
        find)
            if [ -f "$todo" ] && [ "$FORCE_FIND" != 1 ]; then
                say "[$g] find: kept $todo ($(($(wc -l < "$todo") - 1)) repos; FORCE_FIND=1 to redo)"
                continue
            fi
            say "[$g] === find"
            "$PYTHON" "$HERE/group4_2_nested.py" find --unpacked "$un" \
                --nested-extensions "$NESTED" --old-depth "$OLD_DEPTH" --out "$nd" || exit 1
            ;;
        backup)
            n=0
            while IFS= read -r k; do
                [ -n "$k" ] || continue
                b="$nd/before/$k"
                [ -d "$b" ] && continue
                mkdir -p "$b"
                cp -p "$un/_state/$k/manifest.csv" "$un/_state/$k/archives.csv" \
                      "$un/_state/$k/done.json" "$b/" || { rm -rf "$b"; exit 1; }
                rm -f "$un/_state/$k/done.json"
                n=$((n + 1))
            done < <(repos_of "$todo")
            say "[$g] backup: $n repo(s) copied to $nd/before (done.json removed)"
            ;;
        unpack)
            eval "exts=\$EXTS_$g"
            batch="$todo"
            if [ "$LIMIT" -gt 0 ]; then
                batch="$nd/todo_limit.csv"; head -n $((LIMIT + 1)) "$todo" > "$batch"
            fi
            say "[$g] === unpack $(($(wc -l < "$batch") - 1)) repo(s) (log $un/_logs/$NAME.log)"
            start=$(date +%s)
            "$PYTHON" "$HERE/archive_unpack.py" --in "$WORK/$g/4_2_extract" \
                --counts "$WORK/$g/counts" --active-root "$ACTIVE" --out "$un" \
                --batch "$batch" --extensions "$(echo $exts | tr ' ' ,)" \
                --nested-extensions "$NESTED" --max-depth "$MAX_DEPTH" \
                --workers "$WORKERS" --min-free-disk-gb "$MIN_DISK_GB" --name "$NAME"
            rc=$?
            say "[$g] unpack finished in $(( $(date +%s) - start ))s, exit $rc"
            case $rc in
                0) ;;
                1) fail=1 ;;
                3) say "STOP: disk low or the active copy not answering - fix and run again"; exit 3 ;;
                *) exit "$rc" ;;
            esac
            ;;
        compare)
            say "[$g] === compare"
            "$PYTHON" "$HERE/group4_2_nested.py" compare --unpacked "$un" \
                --before "$nd/before" --nested-extensions "$NESTED" \
                --old-depth "$OLD_DEPTH" --new-depth "$MAX_DEPTH" --out "$nd" || fail=1
            ;;
        snapshot)
            snap="$nd/before_out"
            if [ -f "$snap/.done" ]; then
                say "[$g] snapshot: kept $snap ($(cat "$snap/.done"))"; continue
            fi
            n=0
            while IFS= read -r k; do
                [ -n "$k" ] || continue
                for d in $STEP_DIRS; do
                    dst="$snap/$d/$k"
                    rm -rf "$dst"; mkdir -p "$dst"
                    if [ -d "$WORK/$g/$d/$k" ]; then
                        cp -al "$WORK/$g/$d/$k/." "$dst/" || exit 1
                        n=$((n + 1))
                    fi
                done
            done < <(repos_of "$todo")
            echo "$(date '+%Y-%m-%d %H:%M:%S') $n repo folder(s)" > "$snap/.done"
            say "[$g] snapshot: $n repo folder(s) hard-linked to $snap"
            ;;
        delta)
            say "[$g] === delta"
            "$PYTHON" "$HERE/group4_2_nested.py" delta --run-dir "$WORK/$g" \
                --before-out "$nd/before_out" --steps "$(echo $STEP_DIRS | tr ' ' ,)" \
                --out "$nd" || fail=1
            ;;
        reset)
            if [ ! -f "$nd/before_out/.done" ]; then
                say "[$g] ERROR: no snapshot - run the snapshot step before reset"; exit 1
            fi
            n=0
            while IFS= read -r k; do
                [ -n "$k" ] || continue
                if [ ! -f "$un/_state/$k/done.json" ]; then
                    say "[$g] ERROR: $k not unpacked again yet - run the unpack step"; exit 1
                fi
                for d in $STEP_DIRS; do
                    [ -f "$WORK/$g/$d/_state/$k/done.json" ] && rm -f "$WORK/$g/$d/_state/$k/done.json" && n=$((n + 1))
                done
            done < <(repos_of "$todo")
            say "[$g] reset: $n done.json removed in the step folders"
            ;;
        esac
    done
    ;;
  inner)
    say "=== inner: group 1 text, then 4.5_R 4.5 2_text 2_bin 3_pkl 3_pdf"
    RUNS="$RUNS" NAME="$NAME" WORKERS="$WORKERS" bash "$HERE/run_group4_2_text.sh" || fail=1
    RUNS="$RUNS" NAME="$NAME" WORKERS="$WORKERS" PYTHON="$PYTHON" bash "$HERE/run_group4_2_inner.sh" || fail=1
    ;;
  lists)
    say "=== lists: 4.1 and 4.1_R inside the archives"
    "$PYTHON" "$HERE/group4_2_path_list.py" \
        --unpacked "$WORK/group4_2/unpacked" --unpacked "$WORK/group4_2_R/unpacked" \
        --extensions "$HERE/group4_1_extensions.txt" \
        --out "$WORK/group4_2/group4_1_paths/group4_1_inner_paths.csv" || fail=1
    STEPS=inner bash "$HERE/run_group4_1_R.sh" || fail=1
    ;;
  *) say "unknown step '$step' (find backup unpack compare snapshot reset inner lists delta)"; exit 2 ;;
  esac
done

if [ "$fail" = 0 ]; then say "ALL DONE"; else say "FINISHED WITH PROBLEMS - see above"; fi
exit "$fail"
