#!/usr/bin/env bash
# run_group4_7_9_inner.sh - the group 4.7+4.9 types found inside the 4.2 /
# 4.2_R archives, through run_group4_2_inner.sh's two 479 steps:
#
#   479_text  group4_7_9_inner_text_extensions.txt (179): the 160 group 1
#             types + 19 of the 40 types added on 22 Oct (text when sampled)
#             -> <run>/group4_7_9_text: one .txt per path, every line of
#             every version once; binary versions give no lines (counted)
#   479_bin   group4_7_9_inner_binary_extensions.txt (128): the 117 group 2
#             types + 11 of the 40 (binary or mixed when sampled: emb and
#             h70 go whole, their text files too)
#             -> <run>/group4_7_9_binary: each distinct content once, whole
#
# Left out of the 40 (not in either list): snowflake (an executable),
# 272 273 274 639 63b 83f (ZooKeeper test snapshots), cmt (one empty file),
# logerror and accounts (nothing stored when the archives were unpacked).
#
# First the lists are resolved: archive_unpack.py cut the type folders to 24
# characters (safe_type), the manifests keep the full type, and the scripts
# match on the full type. A 24-character entry is replaced by the full types
# the manifests give for the files in that folder -> $OUT/resolved.csv; the
# lists actually used -> $OUT/{text,binary}_extensions.txt.
#
#   cd /data/workarea/scripts
#   nohup ./run_group4_7_9_inner.sh > /data/workarea/group4_7_9/inner/run.out 2>&1 &
#   tail -f /data/workarea/group4_7_9/inner/run.out
#
# Settings: STEPS (default "479_text 479_bin"), RUNS, WORKERS, EXTRA (e.g.
# "--limit 1" to time the biggest repo), NAME - passed to
# run_group4_2_inner.sh. Run it again after a stop and it carries on.
# Exit code: run_group4_2_inner.sh's (0 = all ok, 1 = failed repos).

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-/data/workarea}"
OUT="${OUT:-$WORK/group4_7_9/inner}"
RUNS="${RUNS:-group4_2 group4_2_R}"
STEPS="${STEPS:-479_text 479_bin}"
PYTHON="${PYTHON:-python3}"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$OUT"
for f in group4_7_9_inner_text_extensions.txt group4_7_9_inner_binary_extensions.txt \
         run_group4_2_inner.sh group4_2_text.py group4_2_files.py archive_unpack.py; do
    [ -f "$HERE/$f" ] || { say "ERROR: $HERE/$f not found"; exit 1; }
done

say "=== resolving the type lists -> $OUT"
"$PYTHON" - "$HERE" "$WORK" "$OUT" $RUNS <<'PY' || { say "ERROR: resolving failed"; exit 1; }
import csv, os, sys
here, work, out, runs = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
sys.path.insert(0, here)
from archive_unpack import safe_type
from repo_extension_summary import load_extensions
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

found = []
def resolve(e):
    if len(e) < 24:
        return [e]
    full = {}
    for g in runs:
        top = os.path.join(work, g, "unpacked", "files", e)
        if not os.path.isdir(top):
            continue
        for org in os.listdir(top):
            for repo in os.listdir(os.path.join(top, org)):
                m = os.path.join(work, g, "unpacked", "_state", org, repo, "manifest.csv")
                with open(m, newline="", encoding="utf-8", errors="surrogateescape") as fh:
                    for r in csv.DictReader(fh):
                        if r.get("inner_path") and safe_type(r["ext"]) == e:
                            full.setdefault(r["ext"], set()).add((g, org, repo))
    for x, where in sorted(full.items()):
        found.append([e, x, len(where), " ".join(sorted({w[0] for w in where}))])
    if not full:
        found.append([e, "(no folder of that name - used as is)", 0, ""])
    return sorted(full) or [e]

for kind in ("text", "binary"):
    src = os.path.join(here, "group4_7_9_inner_%s_extensions.txt" % kind)
    exts = []
    for e in load_extensions(src):
        for x in resolve(e):
            if x not in exts:
                exts.append(x)
    with open(os.path.join(out, "%s_extensions.txt" % kind), "w") as fh:
        fh.write("\n".join(exts) + "\n")
    print("  %s: %d types" % (kind, len(exts)))
with open(os.path.join(out, "resolved.csv"), "w", newline="") as fh:
    w = csv.writer(fh, lineterminator="\r\n")
    w.writerow(["listed", "full_type", "repos", "runs"])
    w.writerows(found)
for r in found:
    print("  %-26s -> %s (%s repos)" % tuple(r[:3]))
PY

say "=== run_group4_2_inner.sh, steps [$STEPS]"
LIST_479_TEXT="$OUT/text_extensions.txt" LIST_479_BIN="$OUT/binary_extensions.txt" \
    STEPS="$STEPS" RUNS="$RUNS" WORK="$WORK" PYTHON="$PYTHON" \
    "$HERE/run_group4_2_inner.sh"
rc=$?
for s in group4_7_9_text group4_7_9_binary; do
    f="$WORK/${RUNS%% *}/$s/stats_combined/by_type.csv"
    [ -f "$f" ] && say "$s, both runs: $f" && grep -m1 -i '^total' "$f"
done
exit "$rc"
