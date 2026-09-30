#!/usr/bin/env bash
# copy_group2.sh - copy the group 2 extraction to the file share and check it.
#
#   binary   $BIN/files/<ext>/..., manifest.csv, summaries -> $DST/binary/
#            (the <ext> folders are copied $PARALLEL at a time)
#   text     $TEXT (the text delta, .txt files + _state manifests) -> $DST/text/
#            hard-linked files are copied as real files
#   reports  the counts, analysis, unique_files, certificates, text delta
#            summary -> $DST/reports/
#   verify   file counts, source vs share; then every binary file's content
#            is hashed on the share and compared with its name (each file is
#            named by its git blob id) - "bad 0" means every byte is right
#
# Everything is copied (pfx and p7s included). rsync skips what is already on
# the share, so after an interruption run the same command again and it
# carries on. The _logs folders (run logs) are not copied.
#
#   nohup ./copy_group2.sh > /data/workarea/copy_group2.out 2>&1 &
#   tail -f /data/workarea/copy_group2.out
#
#   VERIFY=0 ./copy_group2.sh      copy only, no content check
#   CHECKSUM=1 ./copy_group2.sh    compare contents, not size + date, when
#                                  deciding what to copy - to repair files the
#                                  check reported BAD
#   ONLY=verify ./copy_group2.sh   only the checks (after a finished copy)
#
# Exit code: 0 = copied and verified, 1 = a copy or check failed (see log).

set -u

BIN="${BIN:-/data/workarea/binary_versions}"
TEXT="${TEXT:-/data/workarea/text9_extract_delta}"
COUNTS="${COUNTS:-/data/workarea/ext_versions}"
TEXT_SUMMARY="${TEXT_SUMMARY:-/data/workarea/group2_text_delta_by_extension.csv}"
DST="${DST:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group2}"
PARALLEL="${PARALLEL:-4}"
VERIFY="${VERIFY:-1}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PYTHON="${PYTHON:-python3}"
OPTS=(-rt --no-perms --no-owner --no-group --partial)
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

for d in "$BIN/files" "$TEXT/_state"; do
    [ -d "$d" ] || { say "ERROR: $d not found"; exit 1; }
done
mkdir -p "$DST/binary/files" "$DST/text" "$DST/reports" \
    || { say "ERROR: cannot create $DST - is the share mounted?"; exit 1; }
say "start: $BIN + $TEXT -> $DST  (parallel $PARALLEL)"
say "free on the share: $(df -h "$DST" | awk 'NR==2 {print $4}'), needed about $(du -sh "$BIN/files" 2>/dev/null | cut -f1) + text"

if [ "$ONLY" != verify ]; then
    # ---------------------------------------------------------- binary files
    say "=== binary: $(ls "$BIN/files" | wc -l) extension folder(s)"
    export DST BIN CHECKSUM
    ls "$BIN/files" | xargs -P "$PARALLEL" -I{} bash -c '
        mkdir -p "$DST/binary/files/{}" &&
        rsync -rt --no-perms --no-owner --no-group --partial \
            $([ "$CHECKSUM" = 1 ] && echo --checksum) \
            "$BIN/files/{}/" "$DST/binary/files/{}/" &&
        echo "$(date "+%Y-%m-%d %H:%M:%S")   done {} ($(find "$BIN/files/{}" -type f | wc -l) files)" ||
        { echo "$(date "+%Y-%m-%d %H:%M:%S")   FAILED {}"; exit 1; }'
    [ $? -eq 0 ] || fail=1

    say "=== binary: manifest and summaries"
    rsync "${OPTS[@]}" --exclude "files/" --exclude "_logs/" "$BIN/" "$DST/binary/" || fail=1

    # ---------------------------------------------------------- text delta
    say "=== text: $TEXT"
    rsync "${OPTS[@]}" -L --exclude "_logs/" "$TEXT/" "$DST/text/" || fail=1

    # ---------------------------------------------------------- reports
    say "=== reports"
    for f in "$COUNTS/by_extension.csv:counts_by_extension.csv" \
             "$COUNTS/analysis/ext_versions_analysis.md:" \
             "$COUNTS/analysis/unique_files.csv:" \
             "$COUNTS/analysis/certificates.csv:" \
             "$COUNTS/analysis/repo_delta.csv:" \
             "$TEXT_SUMMARY:" "$TEXT_SUMMARY.md:"; do
        src="${f%%:*}"; name="${f#*:}"; name="${name:-$(basename "$src")}"
        if [ -f "$src" ]; then cp "$src" "$DST/reports/$name" || fail=1
        else say "  (not found, skipped: $src)"; fi
    done
fi

# -------------------------------------------------------------- checks
say "=== check: file counts, source -> share"
b_src=$(find "$BIN/files" -type f | wc -l)
b_dst=$(find "$DST/binary/files" -type f -not -name '*.tmp.*' | wc -l)
t_src=$(find "$TEXT" -type f -name '*.txt' -not -path '*/_state/*' -not -path '*/_logs/*' | wc -l)
t_dst=$(find "$DST/text" -type f -name '*.txt' -not -path '*/_state/*' | wc -l)
say "  binary files  $b_src -> $b_dst  $([ "$b_src" = "$b_dst" ] && echo OK || echo DIFFERENT)"
say "  text files    $t_src -> $t_dst  $([ "$t_src" = "$t_dst" ] && echo OK || echo DIFFERENT)"
[ -f "$DST/binary/manifest.csv" ] && say "  manifest.csv  $(($(wc -l < "$DST/binary/manifest.csv") - 1)) rows" \
    || { say "  manifest.csv  MISSING"; fail=1; }
[ "$b_src" = "$b_dst" ] && [ "$t_src" = "$t_dst" ] || fail=1

if [ "$VERIFY" = 1 ]; then
    say "=== verify: hashing every binary file on the share"
    "$PYTHON" - "$DST/binary/files" <<'PY'
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
        if n % 20000 == 0:
            print("  %d checked  %.0fs" % (n, time.time() - t0), flush=True)
print("  checked %d, bad %d  %.0fs" % (n, bad, time.time() - t0))
sys.exit(1 if bad else 0)
PY
    [ $? -eq 0 ] || fail=1
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE - copied and checked: $DST"
else
    say "FINISHED WITH PROBLEMS - see the lines above. Rerun the same command to"
    say "  retry; for files reported BAD run it with CHECKSUM=1 so their content is"
    say "  compared and they are copied again"
fi
exit "$fail"
