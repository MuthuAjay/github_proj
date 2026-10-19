#!/usr/bin/env bash
# copy_group4_7_9_group2.sh - copy the group 4.7+4.9 group 2 (whole file)
# types, repo level, to the file share and check them. Two stores become ONE
# folder on the share, and the tracking files stay OUT of it (the scanner
# takes every file in the folder as data):
#
#   data      $G/group2_binary/files/<ext>/<ab>/<blob>.<ext>   history versions
#             $G/group2_active/files/<ext>/<ab>/<blob>.<ext>   today's files
#                       -> $DST_ROOT/group4_7_9/group2_binary/files/
#             Each file is named by its git blob id, so a name means one
#             content: the two stores never hold the same name (active_files
#             .py did not write what extract_versions.py already had), and
#             the share gets each content once.
#   tracking  manifest.csv, by_extension.csv, summary.md of both stores, and
#             the counts' by_extension.csv, summary.md
#                       -> $DST_ROOT/group4_7_9_tracking/group2_{binary,active,counts}/
#             In the active manifest stored_as is made relative to the share
#             folder (files/...) - on the server it is a path into
#             group2_binary for the in_stored rows.
#   checks    names in both stores (should be none), files source -> share,
#             no tracking file among the data; VERIFY=1 then hashes every
#             file on the share and compares it with its name ("bad 0" = every
#             byte is right)
#
# The files inside the 4.2 / 4.2_R archives (step 4, group4_2_files.py) are
# NOT copied here: they are copy_group4_2.sh's layout, so use that with
# STEP_DIRS=group4_7_9_group2_binary.
#
# rsync skips what is already on the share: after an interruption run the
# same command again and it carries on. The share needs sudo, the whole
# command inside sudo bash -c:
#
#   sudo bash -c 'cd /data/workarea/scripts && \
#       nohup ./copy_group4_7_9_group2.sh > /data/workarea/group4_7_9/copy_group2.out 2>&1 &'
#   tail -f /data/workarea/group4_7_9/copy_group2.out
#
#   ONLY=verify ...    only the checks (after a finished copy)
#   VERIFY=0 ...       copy and count, no content hashing
#   CHECKSUM=1 ...     compare contents, not size + date, when deciding what
#                      to copy - to repair files the check reported BAD
#   PARALLEL=8 ...     rsync processes at once, one per <ext> folder (default 4)
#
# Settings: G (/data/workarea/group4_7_9), DST_ROOT (the share's p1 folder),
# PYTHON.
# Exit code: 0 = copied and checked, 1 = a copy or check failed (see log).

set -u

G="${G:-/data/workarea/group4_7_9}"
DST_ROOT="${DST_ROOT:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1}"
PARALLEL="${PARALLEL:-4}"
VERIFY="${VERIFY:-1}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PYTHON="${PYTHON:-python3}"
STORES="group2_binary group2_active"
DATA="$DST_ROOT/group4_7_9/group2_binary"
TRACK="$DST_ROOT/group4_7_9_tracking"
OPTS=(-rt --no-perms --no-owner --no-group --partial)
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

for s in $STORES; do
    [ -d "$G/$s/files" ] || { say "ERROR: $G/$s/files not found"; exit 1; }
done
[ -d "$(dirname "$DST_ROOT")" ] || { say "ERROR: $(dirname "$DST_ROOT") not found - is the share mounted? set DST_ROOT=..."; exit 1; }
mkdir -p "$DATA/files" "$TRACK/group2_binary" "$TRACK/group2_active" "$TRACK/group2_counts" \
    || { say "ERROR: cannot create $DATA - is the share mounted?"; exit 1; }
say "start: $G/{${STORES// /,}}/files -> $DATA/files  (tracking -> $TRACK, parallel $PARALLEL)"
say "free on the share: $(df -h "$DATA" | awk 'NR==2 {print $4}'), needed about $(du -shc $(for s in $STORES; do echo "$G/$s/files"; done) 2>/dev/null | tail -1 | cut -f1)"

# names in both stores: should be none (same name = same bytes, so harmless,
# but it means the in_stored check missed something)
both=$(LC_ALL=C comm -12 \
    <(cd "$G/group2_binary/files" && find . -type f ! -name '*.tmp.*' | LC_ALL=C sort) \
    <(cd "$G/group2_active/files" && find . -type f ! -name '*.tmp.*' | LC_ALL=C sort) | wc -l)
say "names in both stores: $both $([ "$both" = 0 ] && echo OK || echo '(same content, copied once)')"

if [ "$ONLY" != verify ]; then
    # ------------------------------------------------------------ data
    for s in $STORES; do
        say "=== data: $s, $(ls "$G/$s/files" | wc -l) extension folder(s)"
        export DATA CHECKSUM SRC="$G/$s/files"
        ls "$SRC" | grep -v '^\.tmp\.' | xargs -P "$PARALLEL" -I{} bash -c '
            mkdir -p "$DATA/files/{}" &&
            rsync -rt --no-perms --no-owner --no-group --partial --exclude ".tmp.*" \
                $([ "$CHECKSUM" = 1 ] && echo --checksum) \
                "$SRC/{}/" "$DATA/files/{}/" &&
            echo "$(date "+%Y-%m-%d %H:%M:%S")   done {} ($(find "$SRC/{}" -type f | wc -l) files)" ||
            { echo "$(date "+%Y-%m-%d %H:%M:%S")   FAILED {}"; exit 1; }'
        [ $? -eq 0 ] || fail=1
    done

    # ------------------------------------------------------------ tracking
    say "=== tracking"
    for f in group2_binary/manifest.csv group2_binary/by_extension.csv \
             group2_binary/summary.md group2_active/by_extension.csv \
             group2_active/summary.md group2_counts/by_extension.csv \
             group2_counts/summary.md; do
        if [ -f "$G/$f" ]; then cp "$G/$f" "$TRACK/$f" || fail=1
        else say "  (not found, skipped: $G/$f)"; fi
    done
    # active manifest: stored_as -> files/... (relative to the share folder)
    "$PYTHON" - "$G/group2_active/manifest.csv" "$TRACK/group2_active/manifest.csv" <<'PY' || fail=1
import csv, sys
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
import os
os.replace(dst + ".tmp", dst)
print("  active manifest: %d stored_as made relative" % n)
PY
fi

# -------------------------------------------------------------- checks
say "=== check: file counts, source -> share"
n_src=$(cat <(cd "$G/group2_binary/files" && find . -type f ! -name '*.tmp.*') \
            <(cd "$G/group2_active/files" && find . -type f ! -name '*.tmp.*') | sort -u | wc -l)
n_dst=$(find "$DATA/files" -type f ! -name '*.tmp.*' | wc -l)
say "  files  $n_src -> $n_dst  $([ "$n_src" = "$n_dst" ] && echo OK || echo DIFFERENT)"
[ "$n_src" = "$n_dst" ] || fail=1
other=$(find "$DATA" -type f ! -path "$DATA/files/*" | wc -l)
say "  files in the data folder outside files/ (tracking among the data): $other $([ "$other" = 0 ] && echo OK || echo PROBLEM)"
[ "$other" = 0 ] || fail=1
for s in group2_binary group2_active; do
    [ -f "$TRACK/$s/manifest.csv" ] && say "  $s/manifest.csv  $(($(wc -l < "$TRACK/$s/manifest.csv") - 1)) rows" \
        || { say "  $s/manifest.csv  MISSING"; fail=1; }
done

if [ "$VERIFY" = 1 ]; then
    say "=== verify: hashing every file on the share"
    "$PYTHON" - "$DATA/files" <<'PY'
import hashlib, os, sys, time
root, bad, n, t0 = sys.argv[1], 0, 0, time.time()
for d, _, fs in os.walk(root):
    for f in fs:
        if ".tmp." in f:
            continue
        p = os.path.join(d, f)
        h = hashlib.sha1(b"blob %d\0" % os.path.getsize(p))
        with open(p, "rb") as fh:
            for c in iter(lambda: fh.read(1 << 20), b""):
                h.update(c)
        n += 1
        if h.hexdigest() != f.split(".")[0]:
            bad += 1
            print("  BAD", p, flush=True)
        if n % 1000 == 0:
            print("  %d checked  %.0fs" % (n, time.time() - t0), flush=True)
print("  checked %d, bad %d  %.0fs" % (n, bad, time.time() - t0))
sys.exit(1 if bad else 0)
PY
    [ $? -eq 0 ] || fail=1
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE - copied and checked. Scan: $DATA   (not $TRACK)"
else
    say "FINISHED WITH PROBLEMS - see the lines above. Rerun the same command to"
    say "  retry; for files reported BAD run it with CHECKSUM=1 so their content is"
    say "  compared and they are copied again"
fi
exit "$fail"
