#!/usr/bin/env bash
# copy_group4_7_9_group1.sh - copy the group 4.7+4.9 group 1 (160 text types)
# to the file share and check it, keeping the tracking files OUT of the
# scanning scope (the scanner takes every file in the folder as data).
#
#   text      SEND=all:   $G1/text/<org>/<repo>/<path>.txt   every line any
#                         version ever had (today's lines included)
#             SEND=delta: $G1/text_delta/...  the same minus the lines still
#                         in today's file
#                       -> $DST_ROOT/group4_7_9/group1_text/<org>/<repo>/...
#             SEND has no default: say which one.
#   binary    the paths the text route found binary (no .txt), sent whole:
#             $G1/not_text/binary/files (history versions) and
#             $G1/not_text/active/files (today's files), merged
#                       -> $DST_ROOT/group4_7_9/group1_binary/files/<ext>/...
#             Each file is named by its git blob id: one name = one content.
#   tracking  the text run's _state (per repo manifest.csv), the skipped
#             vendored list, the extension list, not_text_paths.csv, the
#             not_text manifests and summaries, the name checks
#                       -> $DST_ROOT/group4_7_9_tracking/group1_{text,binary}/
#   names     paths the share cannot keep apart (it ignores case): every
#             path after the first (sorted) of a group that differs only by
#             case is sent as <name>__case2.txt, __case3.txt, ...; old -> new
#             in case_renamed.csv. Names with characters the share may not
#             allow (\ : * ? " < > |, trailing dot or space) are listed in
#             name_problems.csv and sent as they are.
#   checks    .txt files source -> share, no tracking file among the data,
#             binary files source -> share; VERIFY=1 hashes every binary
#             file on the share against its name; CHECKSUM=1 also compares
#             the .txt contents
#
# rsync skips what is already on the share: after an interruption run the
# same command again and it carries on. The share needs sudo, the whole
# command inside sudo bash -c:
#
#   sudo bash -c 'cd /data/workarea/scripts && SEND=all \
#       nohup ./copy_group4_7_9_group1.sh > /data/workarea/group4_7_9/group1/copy.out 2>&1 &'
#   tail -f /data/workarea/group4_7_9/group1/copy.out
#
#   ONLY=verify ...    only the checks (after a finished copy)
#   PARALLEL=16 ...    rsync processes at once, one per <org>/<repo> folder
#                      (default 16)
#   BINARY=0 ...       text only: no binary files, no binary tracking or checks
#   VERIFY=0 ...       no hashing of the binary files
#   CHECKSUM=1 ...     compare contents, not size + date (repairs too)
#
# Settings: G1 (/data/workarea/group4_7_9/group1), NAME (group1: the share
# folders <NAME>_text, <NAME>_binary and their tracking), DST_ROOT (the
# share's p1 folder), EXTS_FILE, CHECKS ($G1/copy_checks), PYTHON.
# The mixed types (run_group4_7_9_mixed.sh) use the same script:
#   G1=/data/workarea/group4_7_9/mixed NAME=mixed SEND=all
#   EXTS_FILE=/data/workarea/group4_7_9/mixed/extensions.txt
# Exit code: 0 = copied and checked, 1 = a copy or check failed (see log).

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
G1="${G1:-/data/workarea/group4_7_9/group1}"
NAME="${NAME:-group1}"            # share folders <NAME>_text, <NAME>_binary
SEND="${SEND:-}"
DST_ROOT="${DST_ROOT:-/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1}"
EXTS_FILE="${EXTS_FILE:-$HERE/group4_7_9_group1_extensions.txt}"
CHECKS="${CHECKS:-$G1/copy_checks}"
PARALLEL="${PARALLEL:-16}"
BINARY="${BINARY:-1}"
VERIFY="${VERIFY:-1}"
CHECKSUM="${CHECKSUM:-0}"
ONLY="${ONLY:-}"
PYTHON="${PYTHON:-python3}"
NT="$G1/not_text"
STORES="binary active"
DST_TEXT="$DST_ROOT/group4_7_9/${NAME}_text"
DST_BIN="$DST_ROOT/group4_7_9/${NAME}_binary"
TRACK="$DST_ROOT/group4_7_9_tracking"
OPTS=(-rt --no-perms --no-owner --no-group --partial)
[ "$CHECKSUM" = 1 ] && OPTS+=(--checksum)

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail=0

case "$SEND" in
    all)   SRC="$G1/text" ;;
    delta) SRC="$G1/text_delta" ;;
    *)     say "ERROR: set SEND=all (every line, today's included) or SEND=delta (minus today's lines)"; exit 1 ;;
esac
[ -d "$SRC/_state" ] || { say "ERROR: $SRC has no _state folder - is the run finished?"; exit 1; }
[ "$BINARY" = 1 ] || { STORES=""; VERIFY=0; }
for s in $STORES; do
    [ -d "$NT/$s/files" ] || { say "ERROR: $NT/$s/files not found (BINARY=0 for text only)"; exit 1; }
done
[ -d "$(dirname "$DST_ROOT")" ] || { say "ERROR: $(dirname "$DST_ROOT") not found - is the share mounted? set DST_ROOT=..."; exit 1; }
mkdir -p "$CHECKS/repo_lists"
say "start: SEND=$SEND, $SRC -> $DST_TEXT; $([ "$BINARY" = 1 ] && echo "$NT/{binary,active}/files -> $DST_BIN/files" || echo "binary: not sent (BINARY=0)") (tracking -> $TRACK)"

# ---------------------------------------------------------- name checks
# the .txt files: <org>/<repo>/... only - not _state/, _logs/ or the run's
# own files at the top (skipped_vendored_all.csv, ...)
say "=== the .txt files and their names on the share"
( cd "$SRC" && find . \( -path ./_state -o -path ./_logs \) -prune \
    -o -type f ! -name '*.tmp.*' -print | sed 's#^\./##' | awk -F/ 'NF >= 3' \
    | LC_ALL=C sort ) > "$CHECKS/data_files.txt"
"$PYTHON" - "$CHECKS" <<'PY' || exit 1
import csv, os, re, sys
from collections import defaultdict
chk = sys.argv[1]
paths = [l.rstrip("\n") for l in open(os.path.join(chk, "data_files.txt"),
                                      encoding="utf-8", errors="surrogateescape")]
by_low = defaultdict(list)
for p in paths:
    by_low[p.lower()].append(p)
taken = set(by_low)
renamed = []                                     # (source path, name on the share)
for low, group in sorted(by_low.items()):
    for i, p in enumerate(group[1:], 2):
        stem, dot, ext = p.rpartition(".")
        k = i
        while True:
            new = "%s__case%d.%s" % (stem, k, ext) if dot else "%s__case%d" % (p, k)
            if new.lower() not in taken:
                break
            k += 1
        taken.add(new.lower())
        renamed.append((p, new))
moved = {p for p, _ in renamed}
bad = re.compile(r'[\\:*?"<>|]|[ .]$')
problems = [(p, "character or ending the share may not allow")
            for p in paths if any(bad.search(x) for x in p.split("/"))]
kw = dict(newline="", encoding="utf-8", errors="surrogateescape")
with open(os.path.join(chk, "case_renamed.csv"), "w", **kw) as fh:
    w = csv.writer(fh)
    w.writerow(["path", "on_the_share_as"])
    w.writerows(renamed)
with open(os.path.join(chk, "name_problems.csv"), "w", **kw) as fh:
    w = csv.writer(fh)
    w.writerow(["path", "problem"])
    w.writerows(problems)
# one list per <org>/<repo> (relative to the source), case-renamed left out,
# biggest repo first
per = defaultdict(list)
for p in paths:
    if p not in moved:
        per["/".join(p.split("/")[:2])].append(p)
d = os.path.join(chk, "repo_lists")
for f in os.listdir(d):
    os.remove(os.path.join(d, f))
order = sorted(per, key=lambda k: -len(per[k]))
with open(os.path.join(chk, "copy_repos.txt"), "w", **kw) as fo:
    for n, k in enumerate(order):
        name = "%05d.txt" % n
        with open(os.path.join(d, name), "w", **kw) as fh:
            fh.write("\n".join(per[k]) + "\n")
        fo.write(name + "\n")
print("  %d .txt files in %d repo folders; %d renamed for case (case_renamed.csv), "
      "%d with a character problem (name_problems.csv)"
      % (len(paths), len(per), len(renamed), len(problems)))
PY

if [ "$ONLY" != verify ]; then
    mkdir -p "$DST_TEXT" "$TRACK/${NAME}_text" \
        $([ "$BINARY" = 1 ] && echo "$DST_BIN/files $TRACK/${NAME}_binary") \
        || { say "ERROR: cannot create the folders under $DST_ROOT"; exit 1; }
    say "free on the share: $(df -h "$DST_TEXT" | awk 'NR==2 {print $4}')"

    # ----------------------------------------------------------- text
    n_rep=$(wc -l < "$CHECKS/copy_repos.txt")
    say "=== text -> $DST_TEXT ($n_rep repo folders, $PARALLEL at a time)"
    : > "$CHECKS/copy_failed.txt"
    export SRC DST_TEXT CHECKS
    export OPTS_STR="${OPTS[*]}"
    copy_one() {
        rsync $OPTS_STR --files-from="$CHECKS/repo_lists/$1" "$SRC/" "$DST_TEXT/" \
            || { echo "$1 $(head -1 "$CHECKS/repo_lists/$1")" >> "$CHECKS/copy_failed.txt"; return 1; }
    }
    export -f copy_one
    tr '\n' '\0' < "$CHECKS/copy_repos.txt" \
        | xargs -0 -r -P "$PARALLEL" -n 1 bash -c 'copy_one "$1"' _ || fail=1
    n_bad=$(wc -l < "$CHECKS/copy_failed.txt")
    say "  repo folders done: $(( n_rep - n_bad )) of $n_rep$([ "$n_bad" -gt 0 ] && echo " - FAILED: $n_bad, in $CHECKS/copy_failed.txt (run again to retry)")"

    n_ren=$(($(wc -l < "$CHECKS/case_renamed.csv") - 1))
    if [ "$n_ren" -gt 0 ]; then
        say "=== text: $n_ren file(s) under a __case name"
        "$PYTHON" - "$CHECKS/case_renamed.csv" "$SRC" "$DST_TEXT" <<'PY' || fail=1
import csv, os, shutil, sys
src, dst, bad = sys.argv[2], sys.argv[3], 0
for r in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8",
                             errors="surrogateescape")):
    a, b = os.path.join(src, r["path"]), os.path.join(dst, r["on_the_share_as"])
    try:
        if os.path.exists(b) and os.path.getsize(b) == os.path.getsize(a):
            continue
        os.makedirs(os.path.dirname(b), exist_ok=True)
        shutil.copyfile(a, b + ".tmp.copy")
        os.replace(b + ".tmp.copy", b)
    except OSError as exc:
        bad += 1
        print("  FAILED", r["path"], exc)
sys.exit(1 if bad else 0)
PY
    fi

    # ----------------------------------------------------------- binary
    for s in $STORES; do
        say "=== binary: not_text/$s, $(ls "$NT/$s/files" | wc -l) type folder(s)"
        rsync "${OPTS[@]}" --exclude ".tmp.*" "$NT/$s/files/" "$DST_BIN/files/" || fail=1
    done

    # ----------------------------------------------------------- tracking
    say "=== tracking -> $TRACK (not for scanning)"
    rsync "${OPTS[@]}" "$SRC/_state/" "$TRACK/${NAME}_text/_state/" || fail=1
    for f in "$G1/text/skipped_vendored_all.csv" "$EXTS_FILE" "$G1/not_text_paths.csv" \
             "$G1/not_text_left_out.csv" \
             "$CHECKS/case_renamed.csv" "$CHECKS/name_problems.csv"; do
        if [ -f "$f" ]; then cp "$f" "$TRACK/${NAME}_text/" || fail=1
        else say "  (not found, skipped: $f)"; fi
    done
    echo "SEND=$SEND: $SRC" > "$TRACK/${NAME}_text/sent_from.txt"
    for s in $STORES; do
        mkdir -p "$TRACK/${NAME}_binary/$s"
        for f in manifest.csv by_extension.csv summary.md; do
            [ -f "$NT/$s/$f" ] && { cp "$NT/$s/$f" "$TRACK/${NAME}_binary/$s/" || fail=1; }
        done
    done
    # the active manifest's stored_as -> files/... (relative to the share folder)
    [ "$BINARY" = 1 ] && { "$PYTHON" - "$TRACK/${NAME}_binary/active/manifest.csv" <<'PY' || fail=1; }
import csv, os, sys
p = sys.argv[1]
rows = list(csv.reader(open(p, newline="", encoding="utf-8")))
i = rows[0].index("stored_as")
for r in rows[1:]:
    if "/files/" in r[i]:
        r[i] = "files/" + r[i].split("/files/", 1)[1]
with open(p + ".tmp", "w", newline="", encoding="utf-8") as fh:
    csv.writer(fh).writerows(rows)
os.replace(p + ".tmp", p)
PY
fi

# --------------------------------------------------------------- checks
say "=== check: files, source -> share"
t_src=$(wc -l < "$CHECKS/data_files.txt")
t_dst=$(find "$DST_TEXT" -type f ! -name '*.tmp.*' | wc -l)
say "  .txt files    $t_src -> $t_dst  $([ "$t_src" = "$t_dst" ] && echo OK || echo DIFFERENT)"
[ "$t_src" = "$t_dst" ] || fail=1
if [ "$BINARY" = 1 ]; then
b_src=$(cat <(cd "$NT/binary/files" && find . -type f ! -name '*.tmp.*') \
            <(cd "$NT/active/files" && find . -type f ! -name '*.tmp.*') | LC_ALL=C sort -u | wc -l)
b_dst=$(find "$DST_BIN/files" -type f ! -name '*.tmp.*' | wc -l)
say "  binary files  $b_src -> $b_dst  $([ "$b_src" = "$b_dst" ] && echo OK || echo DIFFERENT)"
[ "$b_src" = "$b_dst" ] || fail=1
fi
# data is only <org>/<repo>/... in the text folder and files/ in the binary one
stray=$(find "$DST_TEXT" -mindepth 1 -maxdepth 2 -type f | wc -l)
for d in _state _logs; do [ -e "$DST_TEXT/$d" ] && stray=$((stray + 1)); done
other=0
[ -d "$DST_BIN" ] && other=$(find "$DST_BIN" -type f ! -path "$DST_BIN/files/*" | wc -l)
say "  tracking files among the data: $((stray + other)) $([ $((stray + other)) = 0 ] && echo OK || echo PROBLEM)"
[ $((stray + other)) = 0 ] || fail=1
if [ "$CHECKSUM" = 1 ]; then
    diff=$(rsync -rcn --out-format='%n' --files-from=<(cd "$CHECKS/repo_lists" && cat $(cat ../copy_repos.txt)) "$SRC/" "$DST_TEXT/" \
        | grep -vc '/$')
    say "  .txt content check (case-renamed ones not included): $diff differ"
    [ "$diff" = 0 ] || fail=1
fi

if [ "$VERIFY" = 1 ]; then
    say "=== verify: hashing every binary file on the share"
    "$PYTHON" - "$DST_BIN/files" <<'PY'
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
print("  checked %d, bad %d  %.0fs" % (n, bad, time.time() - t0))
sys.exit(1 if bad else 0)
PY
    [ $? -eq 0 ] || fail=1
fi

if [ "$fail" = 0 ]; then
    say "ALL DONE - scan: $DST_TEXT$([ "$BINARY" = 1 ] && echo " and $DST_BIN")   (not $TRACK)"
else
    say "FINISHED WITH PROBLEMS - see the lines above. Run the same command again"
    say "  to retry; for BAD or differing files add CHECKSUM=1"
fi
exit "$fail"
