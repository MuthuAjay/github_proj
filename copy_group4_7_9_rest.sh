#!/usr/bin/env bash
# copy_group4_7_9_rest.sh - copy the group 4.7+4.9 outputs not sent yet,
# repo level, to the file share and check them, keeping the tracking files
# OUT of the scanning scope (the scanner takes every file in the folder as
# data). Three parts (PARTS, default all three):
#
#   group3_text     data_to_text.py (rds rda rdata npy npz ckpt pickle joblib
#                   pk bson nippy): $G3/text/<org>/<repo>/<path>.txt
#                       -> $DST_ROOT/group4_7_9/group3_text/<org>/<repo>/...
#                   tracking: _state/, summary.csv, by_type.csv and the
#                   run's other top files -> group4_7_9_tracking/group3_text/
#   tabular_binary  parquet orc arrow feather avro sav, whole (as group 2):
#                   $TAB/tabular_binary/files (history versions) and
#                   $TAB/tabular_active/files (today's files), merged
#                       -> $DST_ROOT/group4_7_9/tabular_binary/files/<ext>/...
#                   Each file is named by its git blob id: one name = one
#                   content. tracking: manifest.csv, by_extension.csv,
#                   summary.md of both stores (active stored_as made relative
#                   to the share folder) and the counts' by_extension.csv,
#                   summary.md -> group4_7_9_tracking/tabular_{binary,active,counts}/
#   bi_data         bi_to_data.py (pbix pbit pbi twbx dbc mwb numbers kmz
#                   qgz), data and images only:
#                   $BI/data/data_text/<org>/<repo>/<path>.txt
#                   $BI/data/files/<type>/<org>/<repo>/<sha256>.<type>
#                       -> $DST_ROOT/group4_7_9/bi_data/{data_text,files}/
#                   tracking: _state/ (manifest, files, left_out, same_as,
#                   done.json), summary.csv, by_type.csv, dedupe_* ->
#                   group4_7_9_tracking/bi_data/
#                   Run dedupe_text_paths.py --apply BEFORE this; whatever is
#                   still in _removed_duplicates/ is never sent.
#
# Never sent: _state/, _logs/, _tmp/, _removed_duplicates/, *.tmp.* files.
#
# The same types inside the 4.2 / 4.2_R archives (<run>/group4_7_9_group3_text,
# group4_7_9_tabular_binary, group4_7_9_bi_data) are copy_group4_2.sh's
# layout:
#
#   sudo bash -c 'cd /data/workarea/scripts && P1=/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1 && \
#       STEP_DIRS="group4_7_9_group3_text group4_7_9_tabular_binary group4_7_9_bi_data" EXTRAS=0 \
#       DST_ROOT=$P1/group4_7_9 TRACK_ROOT=$P1/group4_7_9_tracking \
#       CHECKS=/data/workarea/group4_7_9/inner_rest_checks \
#       CHECKS_DST=$P1/group4_7_9_tracking/group4_2_tracking/copy_checks_rest \
#       nohup ./copy_group4_2.sh > /data/workarea/group4_7_9/copy_inner_rest.out 2>&1 &'
#
# rsync skips what is already on the share: after an interruption run the
# same command again and it carries on. The share needs sudo, the whole
# command inside sudo bash -c:
#
#   sudo bash -c 'cd /data/workarea/scripts && \
#       nohup ./copy_group4_7_9_rest.sh > /data/workarea/group4_7_9/copy_rest.out 2>&1 &'
#   tail -f /data/workarea/group4_7_9/copy_rest.out
#
#   PARTS="bi_data" ...   some parts only
#   ONLY=verify ...       only the checks (after a finished copy)
#   VERIFY=0 ...          no hashing of the tabular / bi files on the share
#   CHECKSUM=1 ...        compare contents, not size + date (repairs too)
#   PARALLEL=16 ...       rsync processes at once, one per <org>/<repo> folder
#                         (default 16)
#
# Settings: G3 (/data/workarea/group4_7_9/group3), TAB (.../tabular), BI
# (.../bi), DST_ROOT (the share's p1 folder), CHECKS
# (/data/workarea/group4_7_9/copy_checks_rest), PYTHON.
# Exit code: 0 = copied and checked, 1 = a copy or check failed (see log).

set -u

W="${W:-/data/workarea/group4_7_9}"
G3="${G3:-$W/group3}"
TAB="${TAB:-$W/tabular}"
BI="${BI:-$W/bi}"
DST_ROOT="${DST_ROOT:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1}"
PARTS="${PARTS:-group3_text tabular_binary bi_data}"
CHECKS="${CHECKS:-$W/copy_checks_rest}"
PARALLEL="${PARALLEL:-16}"
VERIFY="${VERIFY:-1}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PYTHON="${PYTHON:-python3}"
DATA="$DST_ROOT/group4_7_9"
TRACK="$DST_ROOT/group4_7_9_tracking"
SKIP="_state _logs _tmp _removed_duplicates"
OPTS=(-rt --no-perms --no-owner --no-group --partial --exclude "*.tmp.*")
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

# ------------------------------------------------------------------ helpers
text_files() {   # folder of <org>/<repo>/... -> its data files, relative
    ( cd "$1" && find . -mindepth 1 -maxdepth 1 -type d \
        $(for s in $SKIP; do printf -- '! -name %s ' "$s"; done) -print0 \
        | xargs -0 -r -I{} find {} -type f ! -name '*.tmp.*' -print \
        | sed 's#^\./##' | awk -F/ 'NF >= 3' | LC_ALL=C sort )
}

name_check() {   # list of paths, csv out
    "$PYTHON" - "$1" "$2" <<'PY'
import csv, re, sys
from collections import defaultdict
paths = [l.rstrip("\n") for l in open(sys.argv[1], encoding="utf-8", errors="surrogateescape")]
by_low = defaultdict(list)
for p in paths:
    by_low[p.lower()].append(p)
bad = re.compile(r'[\\:*?"<>|]|[ .]$')
rows = [(p, "differs only by case from: " + " | ".join(x for x in v if x != p))
        for v in by_low.values() if len(v) > 1 for p in v]
for p in paths:
    parts = p.split("/")
    if any(bad.search(x) for x in parts):
        rows.append((p, "character or ending the share does not allow"))
    if any(len(x.encode("utf-8", "surrogateescape")) > 255 for x in parts):
        rows.append((p, "a name longer than 255 bytes"))
with open(sys.argv[2], "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
    w = csv.writer(fh)
    w.writerow(["path", "problem"])
    w.writerows(rows)
print("  %d files, %d with a name problem -> %s" % (len(paths), len(rows), sys.argv[2]))
PY
}

copy_tree() {    # part, source folder, share folder: one rsync per <org>/<repo>
    local part="$1" src="$2" dst="$3" lists="$CHECKS/$1"
    text_files "$src" > "$lists/data_files.txt"
    name_check "$lists/data_files.txt" "$lists/name_problems.csv" || fail=1
    [ "$ONLY" = verify ] && return
    awk -F/ '{c[$1 "/" $2]++} END {for (k in c) printf "%d\t%s\n", c[k], k}' \
        "$lists/data_files.txt" | sort -t "$(printf '\t')" -k1,1rn | cut -f2- \
        > "$lists/copy_repos.txt"
    : > "$lists/copy_failed.txt"
    mkdir -p "$dst" || { say "ERROR: cannot create $dst"; fail=1; return; }
    say "  $(wc -l < "$lists/copy_repos.txt") repo folder(s), $(wc -l < "$lists/data_files.txt") file(s), $(du -sh --exclude=_removed_duplicates --exclude=_state --exclude=_logs --exclude=_tmp "$src" | cut -f1) -> $dst"
    export SRC_T="$src" DST_T="$dst" LISTS="$lists" OPTS_STR="${OPTS[*]}"
    copy_one() {
        mkdir -p "$DST_T/$1" \
            && rsync $OPTS_STR "$SRC_T/$1/" "$DST_T/$1/" \
            || { echo "$1" >> "$LISTS/copy_failed.txt"; return 1; }
    }
    export -f copy_one
    tr '\n' '\0' < "$lists/copy_repos.txt" \
        | xargs -0 -r -P "$PARALLEL" -n 1 bash -c 'copy_one "$1"' _ || fail=1
    local n_bad; n_bad=$(wc -l < "$lists/copy_failed.txt")
    [ "$n_bad" -gt 0 ] && say "  FAILED: $n_bad repo folder(s), in $lists/copy_failed.txt (run again to retry)"
}

count_check() {  # label, source count, share folder
    local n_dst; n_dst=$(find "$3" -type f ! -name '*.tmp.*' 2>/dev/null | wc -l)
    say "  $(printf '%-28s %9s -> %9s' "$1" "$2" "$n_dst")  $([ "$2" = "$n_dst" ] && echo OK || echo DIFFERENT)"
    [ "$2" = "$n_dst" ] || fail=1
}

stray_check() {  # share data folder: no tracking file among the data
    local n; n=$(find "$1" \( -name done.json -o -name manifest.csv -o -name summary.csv \
        -o -name by_type.csv -o -name 'dedupe_*' -o -path '*/_state/*' -o -path '*/_logs/*' \
        -o -path '*/_removed_duplicates/*' \) 2>/dev/null | wc -l)
    say "  tracking files among the data in $(basename "$1"): $n $([ "$n" = 0 ] && echo OK || echo PROBLEM)"
    [ "$n" = 0 ] || fail=1
}

top_files() {    # folder, tracking folder: the run's own files at the top
    find "$1" -mindepth 1 -maxdepth 1 -type f ! -name '*.tmp.*' -exec cp -p {} "$2/" \; || fail=1
}

verify_names() { # share folder, sha1-blob | sha256
    "$PYTHON" - "$1" "$2" <<'PY'
import hashlib, os, sys, time
root, kind, bad, n, t0 = sys.argv[1], sys.argv[2], 0, 0, time.time()
for d, _, fs in os.walk(root):
    for f in fs:
        if ".tmp." in f:
            continue
        p = os.path.join(d, f)
        if kind == "blob":
            h = hashlib.sha1(b"blob %d\0" % os.path.getsize(p))
        else:
            h = hashlib.sha256()
        with open(p, "rb") as fh:
            for c in iter(lambda: fh.read(1 << 20), b""):
                h.update(c)
        n += 1
        if h.hexdigest() != f.split(".")[0]:
            bad += 1
            print("  BAD", p, flush=True)
        if n % 5000 == 0:
            print("  %d checked  %.0fs" % (n, time.time() - t0), flush=True)
print("  checked %d, bad %d  %.0fs" % (n, bad, time.time() - t0))
sys.exit(1 if bad else 0)
PY
    [ $? -eq 0 ] || fail=1
}

# -------------------------------------------------------------- start
[ -d "$(dirname "$DST_ROOT")" ] || { say "ERROR: $(dirname "$DST_ROOT") not found - is the share mounted? set DST_ROOT=..."; exit 1; }
for p in $PARTS; do
    case "$p" in
        group3_text)    [ -d "$G3/text/_state" ] || { say "ERROR: $G3/text/_state not found - is the text step finished?"; exit 1; } ;;
        tabular_binary) for s in tabular_binary tabular_active; do
                            [ -d "$TAB/$s/files" ] || { say "ERROR: $TAB/$s/files not found"; exit 1; }
                        done ;;
        bi_data)        [ -d "$BI/data/_state" ] || { say "ERROR: $BI/data/_state not found - is the data step finished?"; exit 1; }
                        [ -d "$BI/data/_removed_duplicates" ] && \
                            say "NOTE: $BI/data/_removed_duplicates still there ($(du -sh "$BI/data/_removed_duplicates" | cut -f1)) - not sent" ;;
        *)              say "ERROR: unknown part '$p' (group3_text tabular_binary bi_data)"; exit 1 ;;
    esac
    mkdir -p "$CHECKS/$p"
done
say "start: parts [$PARTS] -> $DATA  (tracking -> $TRACK, parallel $PARALLEL)"
if [ "$ONLY" != verify ]; then
    mkdir -p "$DATA" "$TRACK" || { say "ERROR: cannot create the folders under $DST_ROOT"; exit 1; }
    say "free on the share: $(df -h "$DATA" | awk 'NR==2 {print $4}')"
fi

part_group3_text() {
    say "=== group3_text: $G3/text -> $DATA/group3_text"
    copy_tree group3_text "$G3/text" "$DATA/group3_text"
    if [ "$ONLY" != verify ]; then
        mkdir -p "$TRACK/group3_text"
        rsync "${OPTS[@]}" "$G3/text/_state/" "$TRACK/group3_text/_state/" || fail=1
        top_files "$G3/text" "$TRACK/group3_text"
        cp -p "$CHECKS/group3_text/name_problems.csv" "$TRACK/group3_text/" || fail=1
    fi
    say "=== check group3_text"
    count_check ".txt files" "$(wc -l < "$CHECKS/group3_text/data_files.txt")" "$DATA/group3_text"
    stray_check "$DATA/group3_text"
}

store_list() {   # tabular store -> its file names, relative, sorted
    ( cd "$TAB/$1/files" && find . -type f ! -name '*.tmp.*' | LC_ALL=C sort )
}

active_manifest() {   # active manifest: stored_as -> files/... (relative to the share folder)
    "$PYTHON" - "$1" "$2" <<'PY'
import csv, os, sys
src, dst = sys.argv[1:]
with open(src, newline="", encoding="utf-8") as fi, \
        open(dst + ".tmp", "w", newline="", encoding="utf-8") as fo:
    rd, wr = csv.reader(fi), csv.writer(fo)
    head = next(rd)
    wr.writerow(head)
    i, n = head.index("stored_as"), 0
    for r in rd:
        if "/files/" in r[i]:
            r[i], n = "files/" + r[i].split("/files/", 1)[1], n + 1
        wr.writerow(r)
os.replace(dst + ".tmp", dst)
print("  active manifest: %d stored_as made relative" % n)
PY
}

part_tabular_binary() {
    say "=== tabular_binary: $TAB/{tabular_binary,tabular_active}/files -> $DATA/tabular_binary/files"
    store_list tabular_binary > "$CHECKS/tabular_binary/binary_names.txt"
    store_list tabular_active > "$CHECKS/tabular_binary/active_names.txt"
    both=$(LC_ALL=C comm -12 "$CHECKS/tabular_binary/binary_names.txt" "$CHECKS/tabular_binary/active_names.txt" | wc -l)
    if [ "$both" = 0 ]; then say "  names in both stores: 0 OK"
    else say "  names in both stores: $both - same content, copied once"; fi
    if [ "$ONLY" != verify ]; then
        mkdir -p "$DATA/tabular_binary/files" "$TRACK/tabular_binary" "$TRACK/tabular_active" "$TRACK/tabular_counts" || fail=1
        for s in tabular_binary tabular_active; do
            say "  $s: $(find "$TAB/$s/files" -type f ! -name '*.tmp.*' | wc -l) file(s), $(du -sh "$TAB/$s/files" | cut -f1)"
            rsync "${OPTS[@]}" "$TAB/$s/files/" "$DATA/tabular_binary/files/" || fail=1
        done
        for f in tabular_binary/manifest.csv tabular_binary/by_extension.csv tabular_binary/summary.md \
                 tabular_active/by_extension.csv tabular_active/summary.md; do
            if [ -f "$TAB/$f" ]; then cp -p "$TAB/$f" "$TRACK/$f" || fail=1
            else say "  (not found, skipped: $TAB/$f)"; fi
        done
        for f in by_extension.csv summary.md; do
            [ -f "$G3/counts/$f" ] && { cp -p "$G3/counts/$f" "$TRACK/tabular_counts/" || fail=1; }
        done
        # active manifest: stored_as -> files/... (relative to the share folder)
        active_manifest "$TAB/tabular_active/manifest.csv" "$TRACK/tabular_active/manifest.csv" || fail=1
    fi
    say "=== check tabular_binary"
    n_src=$(LC_ALL=C sort -u "$CHECKS/tabular_binary/binary_names.txt" "$CHECKS/tabular_binary/active_names.txt" | wc -l)
    count_check "files" "$n_src" "$DATA/tabular_binary/files"
    other=$(find "$DATA/tabular_binary" -type f ! -path "$DATA/tabular_binary/files/*" | wc -l)
    say "  files outside files/ (tracking among the data): $other $([ "$other" = 0 ] && echo OK || echo PROBLEM)"
    [ "$other" = 0 ] || fail=1
    if [ "$VERIFY" = 1 ]; then
        say "  verify: hashing every tabular file on the share against its name"
        verify_names "$DATA/tabular_binary/files" blob
    fi
}

part_bi_data() {
    say "=== bi_data: $BI/data/{data_text,files} -> $DATA/bi_data"
    copy_tree bi_data "$BI/data/data_text" "$DATA/bi_data/data_text"
    if [ "$ONLY" != verify ]; then
        if [ -d "$BI/data/files" ]; then
            say "  files: $(find "$BI/data/files" -type f ! -name '*.tmp.*' | wc -l) image / data file(s), $(du -sh "$BI/data/files" | cut -f1)"
            mkdir -p "$DATA/bi_data/files" && rsync "${OPTS[@]}" "$BI/data/files/" "$DATA/bi_data/files/" || fail=1
        fi
        mkdir -p "$TRACK/bi_data"
        rsync "${OPTS[@]}" "$BI/data/_state/" "$TRACK/bi_data/_state/" || fail=1
        top_files "$BI/data" "$TRACK/bi_data"
        cp -p "$CHECKS/bi_data/name_problems.csv" "$TRACK/bi_data/" || fail=1
    fi
    say "=== check bi_data"
    count_check "data .txt files" "$(wc -l < "$CHECKS/bi_data/data_files.txt")" "$DATA/bi_data/data_text"
    if [ -d "$BI/data/files" ]; then
        count_check "images / data files" "$(find "$BI/data/files" -type f ! -name '*.tmp.*' | wc -l)" "$DATA/bi_data/files"
    fi
    other=$(find "$DATA/bi_data" -mindepth 1 -maxdepth 1 ! -name data_text ! -name files | wc -l)
    say "  other entries next to data_text/ and files/: $other $([ "$other" = 0 ] && echo OK || echo PROBLEM)"
    [ "$other" = 0 ] || fail=1
    stray_check "$DATA/bi_data"
    if [ "$VERIFY" = 1 ] && [ -d "$DATA/bi_data/files" ]; then
        say "  verify: hashing every image / data file on the share against its name"
        verify_names "$DATA/bi_data/files" sha256
    fi
}

for p in $PARTS; do
    "part_$p"
done

if [ "$CHECKSUM" = 1 ]; then
    say "=== content check (.txt)"
    for p in $PARTS; do
        if [ "$p" = group3_text ]; then s="$G3/text" d="$DATA/group3_text"
        elif [ "$p" = bi_data ]; then s="$BI/data/data_text" d="$DATA/bi_data/data_text"
        else continue; fi
        diff=$(rsync -rcn --out-format='%n' --files-from="$CHECKS/$p/data_files.txt" "$s/" "$d/" | grep -vc '/$')
        say "  $p: $diff file(s) differ"
        [ "$diff" = 0 ] || fail=1
    done
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE - scan: $(for p in $PARTS; do printf '%s ' "$DATA/$p"; done)  (not $TRACK)"
else
    say "FINISHED WITH PROBLEMS - see the lines above. If counts differ, look at"
    say "  $CHECKS/<part>/name_problems.csv (names the share cannot keep apart);"
    say "  run the same command again to retry; for BAD files add CHECKSUM=1"
fi
exit "$fail"
