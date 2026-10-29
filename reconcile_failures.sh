#!/usr/bin/env bash
# reconcile_failures.sh - READ ONLY. Collect every record of something that
# failed or did not arrive, across all runs and copies in /data/workarea, so
# the process record can reconcile them. Writes only to $OUT.
#
#   nohup ./reconcile_failures.sh > ~/reconcile.out 2>&1 &
#
# Writes to $OUT (default ~/reconcile):
#   summary.txt              the counts below, in one place - paste this
#   copy_failed_all.txt      every non-empty copy_failed.txt: <list>\t<repo folder>
#   copy_logs.txt            per copy log: start, DIFFERENT / FAILED lines, end
#   name_problems_all.csv    every name_problems.csv row: list, path, problem
#   case_lists.txt           case-renamed / case-clash lists and their row counts
#   repo_not_ok.csv          per repo done.json with status other than ok:
#                            run, org, repo, status
#   repo_unfinished.csv      per repo _state folder with no done.json: run, org, repo
#   repo_missing_lists.txt   repo_missing.csv / missing_repos_check.csv found, rows
#
# copy_failed.txt is emptied at the start of each copy, so it holds the LAST
# copy's failures only - a retry that went through leaves it empty.
# Settings: W (/data/workarea), OUT (~/reconcile).

set -u
W="${W:-/data/workarea}"
OUT="${OUT:-$HOME/reconcile}"
mkdir -p "$OUT"
say() { echo "$(date '+%F %T') $*"; }
S="$OUT/summary.txt"
: > "$S"
sum() { echo "$*" | tee -a "$S"; }

say "finding _state folders, check and log files under $W (one walk)"
find "$W" \( -name _removed_duplicates -o -name _not_sent -o -name files \
        -o -name unpacked -o -name _tmp \) -prune \
    -o -type d -name _state -print \
    -o -type f \( -name copy_failed.txt -o -name name_problems.csv \
        -o -name case_renamed.csv -o -name repo_missing.csv \
        -o -name missing_repos_check.csv \) -print \
    2>/dev/null | sort > "$OUT/_found.txt"
say "  $(wc -l < "$OUT/_found.txt") found"

# ------------------------------------------------------------ copy failures
sum "=== A. copy_failed.txt (last copy of each list; empty lists not shown)"
: > "$OUT/copy_failed_all.txt"
grep '/copy_failed.txt$' "$OUT/_found.txt" | while read -r f; do
    n=$(grep -c . "$f")
    [ "$n" -gt 0 ] || continue
    sum "  $n  $f"
    sed "s#^#$f\t#" "$f" >> "$OUT/copy_failed_all.txt"
done
sum "  total repo folders: $(wc -l < "$OUT/copy_failed_all.txt")"

# ------------------------------------------------------------- copy logs
sum "=== B. copy logs: count checks that differ, failures, how each ended"
: > "$OUT/copy_logs.txt"
find "$W" -maxdepth 4 -type f -name '*.out' 2>/dev/null | sort | while read -r f; do
    grep -q -E 'start: |=== check' "$f" 2>/dev/null || continue
    {
        echo "##### $f"
        grep -E ' start: |DIFFERENT|FAILED|differ|tracking file|ALL DONE|FINISHED WITH|ERROR' "$f"
    } >> "$OUT/copy_logs.txt"
    end=$(grep -E 'ALL DONE|FINISHED WITH' "$f" | tail -1 | cut -c1-60)
    nd=$(grep -c DIFFERENT "$f")
    sum "  $f: ${end:-no end line (still running or stopped)}; DIFFERENT lines: $nd"
done

# ---------------------------------------------------------- name problems
sum "=== C. name_problems.csv (names the share cannot keep apart / hold)"
echo "list,path,problem" > "$OUT/name_problems_all.csv"
grep '/name_problems.csv$' "$OUT/_found.txt" | while read -r f; do
    n=$(( $(wc -l < "$f") - 1 ))
    sum "  $n  $f"
    tail -n +2 "$f" | sed "s#^#$f,#" >> "$OUT/name_problems_all.csv"
done
sum "  by problem:"
tail -n +2 "$OUT/name_problems_all.csv" | awk -F, '{print $NF}' \
    | sed 's/from: .*/from: .../' | sort | uniq -c | sed 's/^/    /' | tee -a "$S"

sum "=== D. case lists"
grep '/case_renamed.csv$' "$OUT/_found.txt" | while read -r f; do
    echo "$(( $(wc -l < "$f") - 1 ))  $f"
done | tee "$OUT/case_lists.txt" | tee -a "$S"

# ------------------------------------------------- repos that did not finish
sum "=== E. per repo state: done.json not ok, or no done.json"
echo "run,org,repo,status" > "$OUT/repo_not_ok.csv"
echo "run,org,repo" > "$OUT/repo_unfinished.csv"
grep '/_state$' "$OUT/_found.txt" | while read -r sd; do
    run="${sd%/_state}"
    run="${run#$W/}"
    find "$sd" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | while read -r rd; do
        rel="${rd#$sd/}"
        if [ ! -f "$rd/done.json" ]; then
            echo "$run,${rel%%/*},${rel#*/}" >> "$OUT/repo_unfinished.csv"
        else
            st=$(grep -o '"status": *"[^"]*"' "$rd/done.json" | head -1 | sed 's/.*: *"//; s/"$//')
            [ "$st" = ok ] || echo "$run,${rel%%/*},${rel#*/},${st:-no status}" >> "$OUT/repo_not_ok.csv"
        fi
    done
done
sum "  _state folders: $(grep -c '/_state$' "$OUT/_found.txt")"
sum "  repos with status not ok: $(( $(wc -l < "$OUT/repo_not_ok.csv") - 1 )), by run and status:"
tail -n +2 "$OUT/repo_not_ok.csv" | awk -F, '{print $1 "  " $NF}' | sort | uniq -c \
    | sed 's/^/    /' | tee -a "$S"
sum "  repos with no done.json: $(( $(wc -l < "$OUT/repo_unfinished.csv") - 1 )), by run:"
tail -n +2 "$OUT/repo_unfinished.csv" | cut -d, -f1 | sort | uniq -c \
    | sed 's/^/    /' | tee -a "$S"

# ------------------------------------------------------ repo scope checks
sum "=== F. repo scope lists (missing / excluded repos)"
grep -E '/(repo_missing|missing_repos_check)\.csv$' "$OUT/_found.txt" | while read -r f; do
    echo "$(( $(wc -l < "$f") - 1 ))  $f"
done | tee "$OUT/repo_missing_lists.txt" | tee -a "$S"

say "done -> $OUT (paste $S)"
