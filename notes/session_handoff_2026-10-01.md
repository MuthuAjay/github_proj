# Session handoff (1 Oct 2026): Git history PII extraction

Where the work stands, where everything is, and what comes next. Next up:
the **remaining file types** (pkl, sentinel, decTest, noun) **and pdf**.

## How we work

- **Two machines.** Scripts are written and tested here
  (`/home/eyadmin/Documents/work/github_proj`, git, `test_extension_versions.py`
  + `test_smoke.py`). They run on the **server** (`ganeshk@USSPNXT1778009520LVM20`)
  from `/data/workarea/scripts`, copied there **by hand**. After each copy,
  give the `md5sum` to check (a bad paste happened once).
- **Long runs** use `nohup … > /data/workarea/<name>.out 2>&1 &` (SSH drops).
  Runners resume: per-repo `done.json`, per-batch markers in `_logs/`.
- **Exit codes:** 0 ok, 1 some repos failed (retried once), 3 stopped (low disk
  or the active-copy mount not answering: remount, rerun the same command).
- **The file share needs sudo:**
  `sudo bash -c 'cd /data/workarea/scripts && nohup ./X.sh > /data/workarea/X.out 2>&1 &'`
  (a background `sudo nohup …` waits for a password nobody sees).
- **Commit only when asked.** End commit messages with the Co-Authored-By
  line. CSVs are gitignored; notes and reports (`notes/`, `reports/`) are
  committed.
- **The user wants plain-language explanations** ("documents and drafts"),
  messages and mails in a pasteable format, and CSVs with a legend.

## Inputs (server)

| What | Where |
|---|---|
| Archive: 13,631 repos, git objects only, **no HEAD/refs**; all read through `RecoveredRepo` / a bare stand-in | `/data/workarea/archive/<org>/<repo>` |
| Active copy (13 Aug 2026), **blobfuse, drops** (`Errno 107`) | `/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos` |
| Batches (G01–G05, L01–L08, M01–M04, S01–S06) | `/data/workarea/file_history_out_4/batches/` (+ `plan.csv`) |
| File share for deliveries | `/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/` |

## What was delivered

Versions in the tables below = **changes** (commits that changed a file),
the measure the first extraction used. Distinct contents are reported as
`distinct_versions` where needed.

### Group 1: 32 text types (`EXTENSIONS` in `repo_extension_summary.py`)
- First extraction `file_added_lines.py` → `/data/workarea/full_extract`;
  delta `file_delta.py` → **`/data/workarea/full_extract_delta_2`** (the
  corrected run).
- 8,709,398 files → **6,838,128 files / 1,239,910,203 lines sent** (lines
  deduplicated within each file; not across files).
- **Open gap (validated, `validate_group1_counts.py`):** 7 repos failed in
  the first extraction, so their group 1 content was never sent (+26,800
  files); 4 more repos missed 11 files (odd names):
  - timeout: `ey-org/sat-innovation-lab-program-office` (21,608 files)
  - repo_error: `globalpartnerworkloadtoolrevampcloud` (2,853),
    `iam-smime` (1,384), `ni-admrt-notification` (693),
    `aena-assistants-marketplace` (153), `ey-fso-tax/powerbi-reports` (86),
    `2025-q-1-ai-powered-aml` (23)
  - few files: `smt-monitoring` (5), `sdt-automation` (4),
    `ey-fso-tax/core-pipelines` (1), `ey-fso-tax/workmgmt-db` (1)
  - Next: read the error in `full_extract/_state/<org>/<repo>/done.json`.
    Likely git cannot read some content with `-p` (the name-only reads
    work). Re-extract those repos, then `fill_at_head` and `file_delta`
    for them only.

### Group 2: 27 types (`TEXT_EXTS` 9 + `BINARY_EXTS` 18 in `extension_versions.py`)
- Counts: `run_ext_versions.sh` → `/data/workarea/ext_versions` (pass 1 all
  versions, pass 2 identical-to-active check). Analysis:
  `ext_versions_analysis.py` → `ext_versions/analysis/` (report,
  `unique_files.csv` with the original occurrence, `repo_delta.csv`,
  `certificates.csv`).
- **Rule (user confirmed the head is processed):** a version is skipped
  only when its content is a file in the **same repo's** active copy;
  identical files across repos are stored once.
- Binary (18): `run_extract_versions.sh` → `/data/workarea/binary_versions`
  (`files/<ext>/<ab>/<blob>.<ext>`, `manifest.csv` 567,858 rows).
  918,618 versions − 350,760 already scanned − 20,552 LFS − 351,823
  duplicates − 11 empty = **195,472 files (~72 GB)**.
- Text (9): `run_text_versions.sh` → `/data/workarea/text9_extract` →
  delta **`/data/workarea/text9_extract_delta`**: 41,754 files − 7,523 no
  new lines − 929 unreadable/empty = **33,302 files**.
- **Copied to the share** with `copy_group2.sh` →
  `…/diff_analysis/p1/group2/{binary,text,reports}`. Binary: 195,472
  verified by hash, 0 bad.
- **Open: 98 text files missing on the share** (33,302 → 33,204), with no
  rsync error. Most likely names that differ only by case (the share is
  case-insensitive) or characters it does not allow. Check commands are in
  the conversation of 1 Oct. Fix: copy them under adjusted names plus a
  `renamed_files.csv`.
- The receiving team counted **283,210** files in the folder = 228,676 data
  + 54,534 tracking files (`_state`, manifest, reports). Explained in
  `reports/group2_counts_explained_2026-10-01.md`; per-extension recheck in
  `group2_recheck_by_extension.csv` (project folder, not committed).
- `.pfx` (138) and `.p7s` (1,312) **were sent** (user's choice). pfx can
  hold private keys; the list is `reports/certificates.csv`.

### All extensions
- `run_all_ext_counts.sh` (history-only) → `/data/workarea/all_ext_counts`:
  4,012 extensions, 12,710,793 files, 90,630,855 changes.
  `extension_counts.csv` = ext, files, versions(changes), distinct_versions,
  group (group 1 / group 2 / rest). Local copy of its `by_extension.csv`:
  `csv/by_extension.csv`.
- Group 1 here is +26,811 files / +610,973 changes over the first
  extraction, which is the failed repos above (verified per extension).

## Next: the remaining types and pdf

From the all-extension count (whole history):

| Ext | Repos | Files | Changes | Distinct versions | Vendored files | What it is |
|---|---:|---:|---:|---:|---:|---|
| **pdf** | 1,110 | **44,432** | 140,009 | 47,956 | 3,081 | documents: strong PII candidate |
| **pkl** | 157 | 2,364 | 11,545 | 2,807 | 166 | Python pickle (models, **DataFrames**, dicts): can hold data |
| sentinel | 19 | 1,719 | 3,460 | 1,732 | 14 | build/pipeline "done" marker files, usually empty |
| decTest | 3 | 858 | 2,431 | 858 | 0 | Python's decimal test data (public) |
| noun | 1 | 2 | 52 | 2 | 0 | likely WordNet `index.noun` / `data.noun` (public) |

ICF, hyper and clone_complete: **0 in history** (only on disk), so nothing
to do. "NULL"/no extension: 277,516 files (Dockerfile, .env, …) is not
planned; it would need filtering by file name (`.env` = secrets).

### Steps
1. **Look at samples:** `sample_ext_files.py` (5 real files per type; each
   pickle gets an `.inspect.txt` with its classes and every string,
   **never unpickled**):
   ```
   python3 sample_ext_files.py --by-repo /data/workarea/all_ext_counts/by_repo.csv \
       --repos-root /data/workarea/archive --out /data/workarea/ext_samples \
       --extensions pkl,sentinel,decTest,noun,pdf --per-ext 5
   ```
   Optionally run the wider R&D: `extension_versions.py --extensions …`
   (pass 1) then `extension_rnd.py` (counts, where, 25 samples).
2. **Decide per type** (expected): **pdf** and **pkl** extracted;
   **sentinel, decTest, noun** excluded once the samples confirm they're
   markers or public data.
3. **Ask the user:** were the active copy's pdf / pkl files processed
   before? That decides whether today's versions are skipped (as in
   group 2) or included.
4. **pdf: whole files, like group 2 binary.** Add `pdf` (and `pkl`) to the
   binary handling. `BINARY_EXTS` in `extension_versions.py` drives pass 2
   and `extract_versions.py`, so preferably add a `--binary-exts` option
   rather than changing the group 2 list. Then counts (pass 1+2) into a
   new folder, e.g. `/data/workarea/ext_versions_g3`, extraction into
   `/data/workarea/binary_versions_g3`, and a copy to the share (adapt
   `copy_group2.sh`: `BIN`/`TEXT`/`DST` are variables).
5. **pkl: whole files plus a text conversion.** A scanner cannot read
   pickles. Extract each unique pkl, then write a `.txt` of its strings
   (pickletools walk + raw strings scan, never unpickled; see
   `inspect_text()` in `sample_ext_files.py`) named by the same hash, so the
   manifest maps findings back. Compressed (joblib zlib/lz4) or non-pickle
   `.pkl` files need handling (the signature shows it).
6. Summaries in the established formats: files sent per type (with
   versions), the recheck CSV with a legend, and a short mail.

## Scripts (all committed; tests in `test_extension_versions.py`)

| Script | Does |
|---|---|
| `extension_versions.py` | counts per extension: pass 1 all versions (+ commits, sizes, LFS, at head), pass 2 identical-to-active; `--extensions` |
| `run_ext_versions.sh` | runs it over the batches (`PASSES=1`, `2`) |
| `ext_versions_analysis.py` | report and CSVs from the counts (`--text-delta`) |
| `extension_delta_summary.py` | one CSV per extension with the delta |
| `extract_versions.py` / `run_extract_versions.sh` | binary extraction, deduped, hash-checked, manifest |
| `run_text_versions.sh` | text track: `file_added_lines` → `fill_at_head` → `file_delta` |
| `run_all_versions.sh` | both tracks |
| `copy_group2.sh` | copy to the share and verify (`CHECKSUM=1`, `ONLY=verify`) |
| `all_extension_counts.py` / `run_all_ext_counts.sh` | every extension, files and changes (`--history-only`) |
| `validate_group1_counts.py` | first extraction vs the new count, drill to exact paths |
| `delta_by_extension.py` | line-level delta per extension (files and lines sent) |
| `sent_duplicates.py` | identical files among the sent text files (e.g. js) |
| `repo_extension_status.py` | for given repos: extensions, counts, stage, files sent |
| `extension_rnd.py` / `sample_ext_files.py` | research and samples for new types |

Related notes: `notes/binary_dedup_mapping.md` (how deduplicated files map
back to repos), `reports/group2_counts_explained_2026-10-01.md`.
