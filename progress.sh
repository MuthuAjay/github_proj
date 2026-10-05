#!/usr/bin/env bash
# progress.sh - where every run stands, read only (nothing is started,
# stopped or changed). Runs it does not find are skipped.
#
#   ./progress.sh              every run found on this server
#   ./progress.sh g3 g42       only these: g45 (text, group 4.5), g3, g41, g42
#   watch -n 300 ./progress.sh     refresh every 5 minutes (Ctrl-C to leave)
#
# Paths can be changed in front, e.g. G45_OUT=/data/workarea/output ./progress.sh

set -u
W=/data/workarea
G45_OUT="${G45_OUT:-$W/output}"
G45_LOG="${G45_LOG:-$W/output_full.out}"
G45_BATCHES="${G45_BATCHES:-$( [ -d $W/batches ] && echo $W/batches || echo $W/file_history_out_4/batches )}"
G3="${G3:-$W/group3}"
G41="${G41:-$W/group4_1}"
G42="${G42:-$W/group4_2}"

line() { printf '%s\n' "------------------------------------------------------------"; }

running() {   # pattern -> "running (pid ...)" or "not running"
    local p
    p=$(pgrep -f "$1" | head -3 | tr '\n' ' ')
    [ -n "$p" ] && echo "RUNNING (pid $p)" || echo "not running"
}

done_in() {   # folder -> repos with done.json
    [ -d "$1/_state" ] && find "$1/_state" -name done.json 2>/dev/null | wc -l || echo 0
}

repos_in() {  # batches folder -> repos in the batches of plan.csv
    local n=0 b
    [ -f "$1/plan.csv" ] || { echo "?"; return; }
    for b in $(tail -n +2 "$1/plan.csv" | cut -d, -f1); do
        [ -f "$1/$b.csv" ] && n=$(( n + $(wc -l < "$1/$b.csv") - 1 ))
    done
    echo "$n"
}

last() {      # file -> its last non-empty line
    [ -f "$1" ] && grep -av '^\s*$' "$1" | tail -1 | tr -d '\000' | cut -c1-150
}

show_g45() {
    [ -d "$G45_OUT/_state" ] || return
    line; echo "GROUP 4.5 (text)   $(running 'run_text_versions|file_added_lines|fill_at_head|file_delta')"
    local total batches_done nb
    total=$(repos_in "$G45_BATCHES")
    nb=$(tail -n +2 "$G45_BATCHES/plan.csv" 2>/dev/null | wc -l)
    batches_done=$(ls "$G45_OUT/_logs/"*.text_done 2>/dev/null | wc -l)
    echo "  step 1 extract : $(done_in "$G45_OUT") of $total repos | batches $batches_done of $nb"
    echo "  step 2 at_head : $([ -f "$G45_OUT/_logs/fill_at_head.done" ] && echo done || echo 'not yet')"
    echo "  step 3 delta   : $(done_in "${G45_OUT}_delta") repos $([ -f "$G45_OUT/_logs/delta.done" ] && echo '(done)')"
    local cur
    cur=$(ls -t "$G45_OUT/_logs/"*.log 2>/dev/null | head -1)
    [ -n "$cur" ] && echo "  current log    : $(basename "$cur"): $(last "$cur")"
    echo "  runner         : $(last "$G45_LOG")"
}

show_g3() {
    [ -d "$G3" ] || return
    line; echo "GROUP 3 (pdf, pkl)  $(running 'run_group3|pkl_to_text|pdf_page_merge')"
    local total; total=$(repos_in "$G3/batches")
    echo "  counts   : $(done_in "$G3/counts") of $total repos"
    echo "  extract  : $(done_in "$G3/extract") of $total"
    echo "  pkl text : $(done_in "$G3/pkl_text") of $total"
    echo "  pdf merge: $(done_in "$G3/pdf_merged") of $total"
    local f; f=$(ls -t "$G3"/run_*.out 2>/dev/null | head -1)
    [ -n "$f" ] && echo "  runner   : $(last "$f")"
}

show_g41() {
    [ -d "$G41/counts" ] || return
    line; echo "GROUP 4.1 (db files)  $(running 'group4_1')"
    echo "  counts   : $(done_in "$G41/counts") of $(repos_in "$G41/batches") repos"
    echo "  path list: $([ -f "$G41/group4_1_db_paths.csv" ] && echo "$(( $(wc -l < "$G41/group4_1_db_paths.csv") - 1 )) paths" || echo 'not made yet')"
    [ -f "$G41/counts/_logs/M41.log" ] && echo "  log      : $(last "$G41/counts/_logs/M41.log")"
}

show_g42() {
    [ -d "$G42" ] || return
    line; echo "GROUP 4.2 (archives)  $(running 'run_group4_2|archive_unpack')"
    local total; total=$(repos_in "$G42/batches")
    echo "  counts : $(done_in "$G42/counts") of $total repos"
    echo "  extract: $(done_in "$G42/4_2_extract") of $total"
    echo "  unpack : $(done_in "$G42/unpacked") of $total"
    local f; f=$(ls -t "$G42"/run_*.out 2>/dev/null | head -1)
    [ -n "$f" ] && echo "  runner : $(last "$f")"
}

echo "$(date '+%Y-%m-%d %H:%M:%S') on $(hostname)   disk /data: $(df -h /data 2>/dev/null | awk 'NR==2 {print $4" free ("$5" used)"}')"
want="${*:-g45 g3 g41 g42}"
for g in $want; do
    case "$g" in
        g45) show_g45 ;; g3) show_g3 ;; g41) show_g41 ;; g42) show_g42 ;;
        *) echo "unknown: $g (g45 g3 g41 g42)" ;;
    esac
done
line
