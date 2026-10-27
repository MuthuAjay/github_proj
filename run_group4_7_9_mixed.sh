#!/usr/bin/env bash
# run_group4_7_9_mixed.sh - the 7 "sample first" types of group 4.7+4.9
# (deploy tmp out dump db_backup pbl-old simflows), sent whole without
# sampling (user, 27 Oct): everything, today's files included (no delta),
# as group 1 of 4.7+4.9 - text as text, binary whole - except the program
# binaries of the ClickOnce .deploy files (*.dll.deploy, *.exe.deploy):
# listed, not sent.
#
#   0. batches  the repos whose history has one of the types (from the
#               all-extension count) -> $G/batches/M479MX.csv
#   1. text     run_text_versions.sh: one .txt per path, every line of every
#               version + today's file -> $G/text (send this, SEND=all);
#               binary versions give no lines (manifest status binary /
#               partly_binary)
#   2. binary   the paths step 1 found binary or partly binary, whole:
#               not_text_paths.csv (sent) and not_text_left_out.csv (the
#               .dll / .exe.deploy paths, not sent); counts of those types
#               in those repos, every history version (extract_versions.py,
#               each content once) -> $G/not_text/binary, today's files
#               (active_files.py) -> $G/not_text/active. These read every
#               path of the types, so the files of paths that are not on the
#               sent list (text paths, dll / exe) are then moved out of
#               files/ to <store>/_not_sent/ -> $G/not_text_filter.csv
#   3. inner    the same types inside the 4.2 / 4.2_R archives:
#               group4_2_text.py -> <run>/group4_7_9_mixed_text,
#               group4_2_files.py -> <run>/group4_7_9_mixed_binary; from the
#               whole files, those of paths that gave a .txt (status ok) and
#               the dll / exe.deploy ones are moved to _not_sent/
#
#   mkdir -p /data/workarea/group4_7_9/mixed
#   nohup ./run_group4_7_9_mixed.sh > /data/workarea/group4_7_9/mixed/run.out 2>&1 &
#   tail -f /data/workarea/group4_7_9/mixed/run.out
#   STEPS="binary inner" ./run_group4_7_9_mixed.sh     some steps
#
# Time test before step 1 (ey-org/aut-migration holds 4,958 of the 5,129
# deploy paths):
#   STEPS=batches ./run_group4_7_9_mixed.sh
#   time python3 file_added_lines.py --repo ey-org/aut-migration \
#       --repos-root /data/workarea/archive --out /data/workarea/group4_7_9/mixed/timetest \
#       --extensions /data/workarea/group4_7_9/mixed/extensions.txt \
#       --skip-vendored --match-in-python --workers 1 --repo-timeout 3600
#
# Copy: copy_group4_7_9_group1.sh with G1=$G NAME=mixed SEND=all
# EXTS_FILE=$G/extensions.txt (-> p1/group4_7_9/mixed_{text,binary});
# inner: copy_group4_2.sh with STEP_DIRS="group4_7_9_mixed_text
# group4_7_9_mixed_binary".
#
# Every step resumes: after a stop run the same command again.
# Exit codes: 0 done; 1 done, some repos failed (named in the logs);
# 3 stopped (disk low / the active copy's mount not answering) - fix it and
# run again; anything else: a step crashed, see its log.

set -u

SCRIPTS="${SCRIPTS:-/data/workarea/scripts}"
ROOT="${ROOT:-/data/workarea/archive}"
ACTIVE="${ACTIVE:-/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos}"
ALL_COUNTS="${ALL_COUNTS:-/data/workarea/all_ext_counts/by_repo.csv}"
WORK="${WORK:-/data/workarea}"
G="${G:-$WORK/group4_7_9/mixed}"
EXTS="${EXTS:-deploy tmp out dump db_backup pbl-old simflows}"
NOT_SENT_RE="${NOT_SENT_RE:-\\.(dll|exe)(\\.v[0-9]+)?\\.deploy\$}"   # program binaries, not sent
STEPS="${STEPS:-batches text binary inner}"
RUNS="${RUNS:-group4_2 group4_2_R}"
WORKERS="${WORKERS:-8}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"
PYTHON="${PYTHON:-python3}"
BATCHES="$G/batches"
NT="$G/not_text"
B=M479MX
export SCRIPTS ROOT ACTIVE PYTHON MIN_DISK_GB NOT_SENT_RE

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
mkdir -p "$G"
echo $EXTS | tr ' ' '\n' > "$G/extensions.txt"
csv_exts=$(echo $EXTS | tr ' ' ',')
failed=0
say "group 4.7+4.9 mixed types start: steps '$STEPS', types $csv_exts -> $G"

step_rc() {   # step name, exit code
    case "$2" in
        0) say "DONE $1" ;;
        1) say "DONE $1 - some repos failed (see its logs)"; failed=1 ;;
        3) say "STOP $1: disk low or the active copy not answering - fix it and run this again"
           exit 3 ;;
        *) say "FAIL $1 exit=$2 - see its logs; rerun to resume"; exit "$2" ;;
    esac
}

# ------------------------------------------------------------------ 0.
step_batches() {
    say "=== 0. batches from $ALL_COUNTS"
    mkdir -p "$BATCHES"
    EXTS="$EXTS" "$PYTHON" - "$ALL_COUNTS" "$BATCHES" "$B" <<'PY' || exit 1
import csv, os, sys
from collections import Counter
exts = set(os.environ["EXTS"].split())
files = Counter()
for r in csv.DictReader(open(sys.argv[1], encoding="utf-8")):
    if r["ext"] in exts and int(r["files_in_history"] or 0) > 0:
        files[(r["org"], r["repo"].strip())] += int(r["files_in_history"])
with open(os.path.join(sys.argv[2], sys.argv[3] + ".csv"), "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["org", "repo", "tier", "weight", "listed_files"])
    for (o, r), n in sorted(files.items()): w.writerow([o, r, "group4_7_9_mixed", 1, n])
open(os.path.join(sys.argv[2], "plan.csv"), "w").write("batch,repos\n%s,%d\n" % (sys.argv[3], len(files)))
print("%d repos, %d files in history; the biggest:" % (len(files), sum(files.values())))
for (o, r), n in files.most_common(5): print("  %s/%s  %d files" % (o, r, n))
PY
}

# ------------------------------------------------------------------ 1.
step_text() {
    say "=== 1. text, every line + today's -> $G/text"
    BATCHES="$BATCHES" OUT="$G/text" EXTS="$EXTS" SKIP_VENDORED=1 MATCH_IN_PYTHON=1 \
        "$SCRIPTS/run_text_versions.sh"
    step_rc text $?
}

# ------------------------------------------------------------------ 2.
nt_list() {      # -> not_text_paths.csv, not_text_left_out.csv, $NT/M_nt.csv, $NT/exts.txt
    EXTS="$EXTS" "$PYTHON" - "$G" <<'PY'
import csv, glob, os, re, sys
from collections import Counter
g = sys.argv[1]
exts = sorted(os.environ["EXTS"].split(), key=len, reverse=True)
not_sent = re.compile(os.environ["NOT_SENT_RE"], re.I)
send, left, st = [], [], Counter()
for m in glob.glob(os.path.join(g, "text", "_state", "*", "*", "manifest.csv")):
    for r in csv.DictReader(open(m, newline="", encoding="utf-8", errors="surrogateescape")):
        st[r["status"]] += 1
        if r["status"] not in ("binary", "partly_binary"):
            continue
        low = r["path"].lower()
        ext = next((e for e in exts if low.endswith("." + e.lower())), low.rsplit(".", 1)[-1])
        row = [r["org"], r["repo"], r["path"], ext, r["status"], r["versions"]]
        (left if not_sent.search(r["path"]) else send).append(row)
kw = dict(newline="", encoding="utf-8", errors="surrogateescape")
with open(os.path.join(g, "not_text_paths.csv"), "w", **kw) as fh:
    w = csv.writer(fh); w.writerow(["org", "repo", "path", "ext", "status", "versions"]); w.writerows(send)
with open(os.path.join(g, "not_text_left_out.csv"), "w", **kw) as fh:
    w = csv.writer(fh); w.writerow(["org", "repo", "path", "ext", "status", "versions", "reason"])
    w.writerows([r + ["program binary (.dll / .exe.deploy), not sent"] for r in left])
os.makedirs(os.path.join(g, "not_text"), exist_ok=True)
repos = sorted({(r[0], r[1]) for r in send})
with open(os.path.join(g, "not_text", "M_nt.csv"), "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["org", "repo"]); w.writerows(repos)
open(os.path.join(g, "not_text", "exts.txt"), "w").write(",".join(sorted({r[3] for r in send})))
print("  text manifests: %s" % dict(st))
print("  binary / partly binary paths: %d to send whole (%d repos), %d dll / exe.deploy left out"
      % (len(send), len(repos), len(left)))
for e, n in Counter(r[3] for r in send).most_common():
    print("    %-10s %d" % (e, n))
PY
}

nt_filter() {    # move what is not on the sent list out of files/
    "$PYTHON" - "$G" <<'PY'
import csv, os, shutil, sys
g = sys.argv[1]
nt = os.path.join(g, "not_text")
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
kw = dict(newline="", encoding="utf-8", errors="surrogateescape")
keep = {(r["org"], r["repo"], r["path"]) for r in csv.DictReader(open(os.path.join(g, "not_text_paths.csv"), **kw))}
def rel(s):
    s = s.replace("\\", "/")
    if "/files/" in s: return s.split("/files/", 1)[1]
    return s[6:] if s.startswith("files/") else ""
want = {"binary": set(), "active": set()}
for r in csv.DictReader(open(os.path.join(nt, "binary", "manifest.csv"), **kw)):
    if (r["org"], r["repo"], r["path"]) in keep and r.get("stored_as"):
        want["binary"].add(rel(r["stored_as"]))
am = os.path.join(nt, "active", "manifest.csv")
for r in csv.DictReader(open(am, **kw)) if os.path.isfile(am) else []:
    if (r["org"], r["repo"], r["path"]) in keep and r.get("stored_as"):
        want["active" if r["action"] == "written" else "binary"].add(rel(r["stored_as"]))
out = []
for store in ("binary", "active"):
    root = os.path.join(nt, store, "files")
    kept = moved = mb = 0
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            r_ = os.path.relpath(p, root).replace(os.sep, "/")
            if ".tmp." in f or r_ in want[store]:
                kept += 1
                continue
            dest = os.path.join(nt, store, "_not_sent", "files", r_)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            mb += os.path.getsize(p)
            shutil.move(p, dest)
            moved += 1
    out.append([store, kept, moved, round(mb / 1e6, 1)])
    print("  %-6s kept %d, moved to _not_sent %d (%.1f MB: text paths, dll / exe)" % (store, kept, moved, mb / 1e6))
with open(os.path.join(g, "not_text_filter.csv"), "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["store", "files_kept", "files_moved_not_sent", "mb_moved"]); w.writerows(out)
PY
}

step_binary() {
    say "=== 2. binary paths, whole -> $NT"
    nt_list || exit 1
    if [ ! -s "$NT/exts.txt" ]; then
        say "  no binary paths to send"; mkdir -p "$NT/binary/files" "$NT/active/files"; return
    fi
    local E; E=$(cat "$NT/exts.txt")
    for pass in "" "--identical"; do
        "$PYTHON" "$SCRIPTS/extension_versions.py" --batch "$NT/M_nt.csv" --repos-root "$ROOT" \
            --active-root "$ACTIVE" --out "$NT/counts" --extensions "$E" --binary-exts "$E" \
            --match-in-python $pass >> "$G/binary.log" 2>&1
        step_rc "binary counts ${pass:-pass 1}" $?
    done
    "$PYTHON" "$SCRIPTS/extract_versions.py" --state "$NT/counts" --repos-root "$ROOT" \
        --out "$NT/binary" --batch "$NT/M_nt.csv" --extensions "$E" --skip-vendored \
        --min-free-disk-gb "$MIN_DISK_GB" >> "$G/binary.log" 2>&1
    step_rc "binary extract" $?
    "$PYTHON" "$SCRIPTS/active_files.py" --state "$NT/counts" --active-root "$ACTIVE" \
        --out "$NT/active" --also-stored "$NT/binary" --batch "$NT/M_nt.csv" --extensions "$E" \
        --min-free-disk-gb "$MIN_DISK_GB" >> "$G/binary.log" 2>&1
    step_rc "binary active" $?
    mkdir -p "$NT/binary/files" "$NT/active/files"
    nt_filter || exit 1
    say "  sent whole: $(find "$NT/binary/files" "$NT/active/files" -type f ! -name '*.tmp.*' | wc -l) file(s), $(du -shc "$NT/binary/files" "$NT/active/files" | tail -1 | cut -f1)"
}

# ------------------------------------------------------------------ 3.
inner_filter() { # unpacked run, text out, binary out
    "$PYTHON" - "$2" "$3" <<'PY'
import csv, glob, os, re, shutil, sys
txt, bino = sys.argv[1], sys.argv[2]
not_sent = re.compile(os.environ["NOT_SENT_RE"], re.I)
kw = dict(newline="", encoding="utf-8", errors="surrogateescape")
as_text = set()
for m in glob.glob(os.path.join(txt, "_state", "*", "*", "manifest.csv")):
    for r in csv.DictReader(open(m, **kw)):
        if r["status"] == "ok":
            as_text.add((r["org"], r["repo"], r["path"]))
keep, rows = set(), []
for m in glob.glob(os.path.join(bino, "_state", "*", "*", "manifest.csv")):
    for r in csv.DictReader(open(m, **kw)):
        rows.append(r)
        k = (r["org"], r["repo"], r["path"])
        if k in as_text or not_sent.search(r["path"]):
            continue
        if r["status"] == "written": keep.add(r["output"])
        if r["status"] == "same_as_other_path" and r["same_as"]: keep.add(r["same_as"])
moved = 0
for r in rows:
    o = r["output"]
    if r["status"] != "written" or not o or o in keep or not os.path.isfile(os.path.join(bino, o)):
        continue
    dest = os.path.join(bino, "_not_sent", o)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(os.path.join(bino, o), dest)
    moved += 1
print("  whole files kept %d, moved to _not_sent %d (sent as .txt, or dll / exe.deploy)" % (len(keep), moved))
PY
}

step_inner() {
    local g un t b
    for g in $RUNS; do
        un="$WORK/$g/unpacked" t="$WORK/$g/group4_7_9_mixed_text" b="$WORK/$g/group4_7_9_mixed_binary"
        if [ ! -d "$un/_state" ]; then
            say "[$g] ERROR: $un/_state not found - skipped"; failed=1; continue
        fi
        say "=== 3. inner [$g] text -> $t"
        mkdir -p "$t/_logs" "$b/_logs"
        "$PYTHON" "$SCRIPTS/group4_2_text.py" --unpacked "$un" --out "$t" --extensions "$csv_exts" \
            --workers "$WORKERS" --name mixed >> "$t/_logs/mixed.out" 2>&1
        step_rc "inner text $g" $?
        say "=== 3. inner [$g] whole -> $b"
        "$PYTHON" "$SCRIPTS/group4_2_files.py" --unpacked "$un" --out "$b" --extensions "$csv_exts" \
            --workers "$WORKERS" --name mixed >> "$b/_logs/mixed.out" 2>&1
        step_rc "inner whole $g" $?
        inner_filter "$un" "$t" "$b" || exit 1
        [ -f "$t/stats/by_type.csv" ] && tr -d '\r' < "$t/stats/by_type.csv"
    done
}

for step in $STEPS; do
    if [ "$(type -t "step_$step")" = function ]; then
        "step_$step"
    else
        say "unknown step '$step' (batches text binary inner)"; exit 2
    fi
done

say "done - send: $G/text (SEND=all) + $NT/{binary,active}/files (copy_group4_7_9_group1.sh NAME=mixed) and <run>/group4_7_9_mixed_{text,binary} (copy_group4_2.sh)"
exit "$failed"
