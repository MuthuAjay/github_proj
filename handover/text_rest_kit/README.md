# Text extensions kit: history extraction for the remaining text file types

This kit runs the **text track** used for group 1 (32 types) and group 2's
text types, for a new list of text-based extensions. For each repo it
collects every line any version of a file ever had, then removes the lines
still in today's file. What's left (the **delta**) is what gets sent for the
PII scan.

For background (the archive, the active copy, what "versions" means, the
skip rule), read `notes/new_extension_template.md` (Part A) in the project
repo.

## What is in the kit

| File | What it does |
|---|---|
| `scripts/run_text_versions.sh` | **The one you run.** Runs the three steps below over every batch, resumes after a stop |
| `scripts/file_added_lines.py` | Step 1: every line each file ever had, once each → one `.txt` per file |
| `scripts/fill_at_head.py` | Step 2: marks each file as still in today's copy (`at_head = yes/no`) |
| `scripts/file_delta.py` | Step 3: removes the lines still in today's file → the delta (send this) |
| `scripts/delta_by_extension.py` | Report afterwards: per extension, files and lines in history, removed, sent |
| `scripts/copy_group4_5.sh` | Copy to the share: data and tracking kept apart, name check, count check |
| `scripts/file_history_for_list.py`, `extract_commits.py`, `explore_input_csv.py`, `explain_file_counts.py`, `repo_extension_summary.py`, `analyse_file_summary.py` | Helpers the scripts above import. They must sit in the same folder |
| `text_extensions.txt` | **You fill this in:** the extensions to run, one per line |
| `md5sums.txt` | Checksums of every script, to check the copy on the server |

## In plain words

1. For every file of the listed types, in every repo, on every branch,
   including deleted files: collect every line it ever had into one text
   file, each line once.
2. Check which of those files still exist in today's copy of the repo.
3. For those that do, cross out every line still in today's version,
   because today's files were already scanned. Files that no longer exist
   keep all their lines.
4. Send what's left.

## Before you start: confirm these

- [ ] **The extension list.** Text types only (binary types like pdf, xlsx
      or png go through a different track). None of them should already be
      in group 1 or group 2; the check below catches that.
- [ ] **Today's files of these types were scanned before.** The delta
      removes today's lines on that assumption. If a type was never scanned,
      its lines must not be removed: run it separately with the delta step
      skipped, and ask before doing that.
- [ ] **Disk space.** Step 1 writes every line ever, step 3 a smaller copy.
      Check `df -h /data/workarea`. The run pauses (exit 3) below
      `MIN_DISK_GB` (default 20).
- [ ] **The active copy is mounted:**
      `ls /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos | head`

## Setup on the server

1. Copy the kit to the server, into a **new** folder, so the existing scripts
   in `/data/workarea/scripts` are not overwritten:
   ```bash
   mkdir -p /data/workarea/scripts_text_rest
   # copy scripts/*, text_extensions.txt and md5sums.txt into it
   cd /data/workarea/scripts_text_rest
   md5sum -c md5sums.txt          # every line must say OK
   chmod +x run_text_versions.sh
   ```
2. Fill in `text_extensions.txt` (one extension per line, no dot, any case;
   it ships empty, and nothing else may be in it: no comments or headers),
   then check it against groups 1 and 2:
   ```bash
   python3 - <<'EOF'
   from repo_extension_summary import EXTENSIONS, load_extensions
   group2_text = "erb feature bicep lock rst tfvars azcli groovy xcconfig".split()
   group2_bin = ("xlsx xlsm xlsb xls docx doc pptx pptm msg eml png jpg jpeg "
                 "mpg pfx p7s woff suo").split()
   group3 = ["pdf", "pkl"]
   exts = load_extensions("text_extensions.txt")
   clash = [e for e in exts if e in EXTENSIONS or e in group2_text + group2_bin + group3]
   print(len(exts), "extensions;", "already done / other track:", clash or "none")
   EOF
   ```
   Remove anything it lists before running (pdf and pkl are group 3, a
   separate track).

## Run

Use **your own output folders** so earlier results are not touched:

```bash
cd /data/workarea/scripts_text_rest
SCRIPTS=$PWD \
EXTS="$(tr '\n' ' ' < text_extensions.txt)" \
OUT=/data/workarea/text_rest_extract \
nohup ./run_text_versions.sh > /data/workarea/text_rest.out 2>&1 &
```

- `nohup ... &` keeps it running if your SSH session drops.
- `SKIP_VENDORED=1` in front leaves out files in third-party and build
  folders (node_modules, packages, vendor, bin, obj, dist, build, ...), the
  same rule as the extension counts. Each repo's `done.json` then says how
  many were left out (`skipped_vendored`).
- The delta is written to `$OUT` + `_delta`:
  `/data/workarea/text_rest_extract_delta`.
- Repos are processed in batches (G01–G05, L01–L08, M01–M04, S01–S06, from
  `/data/workarea/file_history_out_4/batches/plan.csv`). To try one small
  batch first, add its name: `... ./run_text_versions.sh S01`. Steps 2 and
  3 run only once every batch is done.

Follow it:

```bash
tail -f /data/workarea/text_rest.out
ls /data/workarea/text_rest_extract/_logs/      # one marker per finished batch / step
```

## When it stops: exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Finished | Go to the report |
| 1 | Finished, some repos failed | They were retried once. The log names them; check `_state/<org>/<repo>/done.json` for the error |
| 3 | Paused on purpose: disk low, or the active copy's mount stopped answering (`Errno 107`) | Free disk or remount, then run **the same command again**. It carries on where it stopped |
| other | A step crashed | Read the last lines of the `.out` file and the step's log in `_logs/` |

Running the same command again is always safe: finished repos (`done.json`)
and finished batches (markers in `_logs/`) are skipped.

## Output

```
/data/workarea/text_rest_extract_delta/
  <org>/<repo>/<path>.txt              the lines to send, content only
  _state/<org>/<repo>/manifest.csv     per file: action, at_head, lines in
                                       history, removed, kept
  _logs/                               run logs (not sent)
```

Actions in the manifest: `subtracted` (today's lines removed), `empty`
(nothing left, no file written), `linked` / `copied` (file gone today:
whole history), `linked_no_active_copy` (repo not in today's copy),
`unreadable_current` (today's file unreadable: whole history kept).

## Report

```bash
python3 delta_by_extension.py \
    --extract /data/workarea/text_rest_extract \
    --delta /data/workarea/text_rest_extract_delta \
    --extensions text_extensions.txt \
    --out /data/workarea/text_rest_delta_by_extension.csv
```

This writes a CSV plus a `.md` with a legend: per extension, files in
history, still today, lines in history, lines removed, **lines sent, files
sent**. The `files_no_delta` column must be 0 before the numbers are final.

## Copy to the share

`scripts/copy_group4_5.sh` does the copy and the checks. Run it after the
full run has finished (its log ends with `runner done`). Check the settings
at its top first, mainly `DST`, the folder on the share.

```bash
sudo bash -c 'cd /data/workarea/scripts/handover/text_rest_kit/scripts && \
    OUT=/data/workarea/output \
    DST=/home/rohitr/eng-gh2-data-fs/diff_analysis/p1/group4_5 \
    nohup ./copy_group4_5.sh > /data/workarea/copy_group4_5.out 2>&1 &'
tail -f /data/workarea/copy_group4_5.out
```

What it does:

1. **Report:** `delta_by_extension.py`, files and lines sent per extension
   (`<OUT>_delta_by_extension.csv` + `.md` with a legend), if not made yet.
2. **Name check:** lists the `.txt` files whose names the share cannot keep
   apart: names differing only by case (`Readme.md` / `README.md`) or with
   characters it does not allow. Written to `<OUT>_copy_checks/name_problems.csv`.
   The share is case-insensitive; in group 2, 98 files went missing this way
   without an rsync error.
3. **Data:** the `.txt` files of the delta go to `DST`. Nothing from `_state/`
   or `_logs/` goes there.
4. **Tracking:** the per-repo `manifest.csv` and `done.json`, the report, the
   extension list and the name check go to `DST_tracking` (default:
   `DST` + `_tracking`), kept out of the scan. In group 2 the scanner took
   every manifest and `done.json` in the folder as data.
5. **Checks:** the number of `.txt` files must match, source vs share, and no
   tracking file may be in the data folder. It ends with `ALL DONE` (exit 0)
   or `FINISHED WITH PROBLEMS` (exit 1).

Rerunning is safe: files already on the share are skipped.
`ONLY=verify` in front runs only the checks; `CHECKSUM=1` also compares the
contents. If the counts differ, look at `name_problems.csv` first.

## Ask if unsure

- Anything about an extension's type (text vs binary) or whether it was
  scanned before: ask before running.
- Keep the `.out` file and `_logs/`: they answer most questions afterwards.
