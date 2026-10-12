# Session handoff (7 Oct 2026): Git history PII extraction

Where the work stands after the 1-7 Oct session. Previous handoff:
`notes/session_handoff_2026-10-01.md` (groups 1 and 2, server layout).
Next up: **group 4.3 R&D** (top 30 types, samples running), then routing
the **4.2** unpack output, then **group 5** (no extension).

## How we work (unchanged, plus what was learned)

- Scripts written and tested here, copied **by hand** to the server
  (`ganeshk@USSPNXT1778009520LVM20`, `/data/workarea/scripts`); always give
  the `md5sum`. **Copy as files** (`scp` / tarball): pasting through Windows
  turned files into CRLF once (every md5 wrong, bash scripts fail); fix with
  `sed -i 's/\r$//' <file>`. Copies can lose the executable bit: `chmod +x`.
- Server Python is **3.10** (test with a 3.10 venv: `uv venv --python 3.10`);
  7z is p7zip at `/usr/bin/7z`; pip packages installed with `--user`:
  `pypdfium2 pypdf zstandard lz4`.
- Long runs: `nohup … > log 2>&1 &`, run from `/data/workarea/scripts`.
  After an SSH drop **never start the command again** (a second start
  emptied a log once): reattach with `tail -f`, check with `./progress.sh`
  (read only: every group's repos done per step, running or not).
- Share needs sudo: `sudo bash -c 'cd … && nohup ./X.sh > … 2>&1 &'`.
- **Tracking files never go into a scan folder** (group 2 lesson): copy
  scripts put data and `_state`/manifests in separate share folders.
- Scope is **strictly per repo**: dedup, page matching, caches only inside
  a repo; nothing matched across repos.
- **No OCR anywhere** (neither the PII team nor us); pages/images without
  text are flagged "ocr_needed".
- Commit only when asked; end messages with the Co-Authored-By line. CSVs
  and .txt are gitignored (force-add with `git add -f` when wanted).
- The user wants plain-language explanations, pasteable mails, CSVs with a
  legend, and recommendations rather than option lists.

## Groups: status

Source of the groups: `All File Extensions_Summary_20261001_V2.xlsx`, sheet
"All File Extensions", column **Sub Group** (read with pandas/openpyxl in a
venv: `dtype={"x": str}, keep_default_na=False`, else "nan"/numbers break).
The sheet's "All Versions" column is wrong for some rows (lookup errors);
use our own counts (`csv/by_extension.csv` = `/data/workarea/all_ext_counts`).

| Group | What | Status |
|---|---|---|
| 1 | 32 text types | delivered (see 1 Oct handoff; 7-repo gap still open) |
| 2 | 27 types | delivered; scanner output cleaned (below) |
| 3 | pdf, pkl | **delivered + mailed** (below) |
| 4.1 | 28 db types | path list built (below) |
| 4.2 | 39 archive / package types | category A **unpacked**, stats done; routing next |
| 4.3 | 2,891 "do not process / unknown" types | **R&D on the top 30** started |
| 4.4, 4.6 | executables (25), ? (148) | not looked at |
| 4.5 | 756 text types | colleague running it (LVM16) |
| 5 | no extension, 277,516 files | samples script written; full listing needs a change |

### Group 2: scanner output cleanup (done 2 Oct)
The PII team scanned the whole group 2 folder, our tracking files included.
`group2_tracking_files.py` moved the 651 tracking results out of
`~/eng-gh2-data-fs/pii_scanner_exports_group2/pii_scanner_output_20261001T134241Z`
into `…_tracking` next to it (26,732 data results stay; verified 0 left).
Lists: `/data/workarea/g2_scanner_tracking/moved_full_list.txt`,
`pii_scanner_exports_group2/group2_tracking_files_moved.csv`. Their
`aggregate_index/` tables still count those 651 (not fixed).
Their `files.csv` (283,210 rows) shows **137,589 images skipped_ocr_required**
(png, jpg, jpeg, woff, mpg) and 10,448 `skipped_binary_no_parser`: only
~78K of 228,676 group 2 data files were really scanned. Raised to the user.

### Group 3: pdf + pkl (delivered)
Pipeline `run_group3.sh`: counts (pdf, pkl as binary) -> `extract_versions.py
--per-repo` -> `pkl_to_text.py` (strings via pickletools, never unpickled;
today's file included, PKL_ACTIVE=1) -> `pdf_page_merge.py` (every page
rendered with pypdfium2 + hashed, duplicate pages dropped, ORIGINAL pages
merged with pypdf so text stays; one merged PDF per path; ocr_needed = page
with no text; unreadable versions copied whole; today's pages kept,
PDF_DROP_ACTIVE=0) -> `copy_group3.sh`. Folders `/data/workarea/group3/`
{batches (M30 + test M3T), counts, extract, pkl_text, pdf_merged, copy_checks}.
Share: `diff_analysis/p1/group3/{pdf,pkl}` (scan) and `group3_tracking/`.
Numbers: 28,751 pdf paths -> 28,254 files; 1,116,952 pages, 1,079,664 kept,
37,288 duplicate, **13,449 ocr_needed**, **450 unreadable**; pkl 1,086 paths
-> 1,062 .txt, 12,445,716 lines. Mail sent (paths given as
`/media/eng-gh2-data-fs/diff_analysis/p1/group3/...`, the scanner's mount).
Open: were today's PDFs scanned before (decides dropping today's pages).

### Group 4.1: database files (path list)
`group4_1_extensions.txt` (28 types). Targeted batch of the 730 repos that
have them (`/data/workarea/group4_1/batches/M41.csv`), pass 1 only:
`PASSES=1 BATCHES=… OUT=/data/workarea/group4_1/counts EXTS="$(tr '\n' ' ' <
group4_1_extensions.txt)" ./run_ext_versions.sh`, then `ext_path_list.py`
-> `group4_1_db_paths.csv` (+ legend, by_extension). "commits" = commits
that added/changed/deleted the path (not commits containing it). Expected
~5,987 paths / 18,422 commits. Copy to the share = `cp` of the 3 CSVs to
`diff_analysis/p1/group4_1/`. Whether the run finished: check `progress.sh g41`.

### Group 4.2: archives (unpacked; routing next)
Scope agreed: category A (zip gz gzip tar tgz bz2 xz z lzma 7z rar zst lz4
zipx cab) not vendored = opened; packages (nupkg, jar, whl, apk, ...),
asset/disk containers (pak, pck, img, bundle, unitypackage) and vendored =
list only. `group4_2_extensions.csv` (39 types, scope, counts, legend).
Today's archives were probably never scanned, so **every version incl.
today's** is read. `run_group4_2.sh` (batches -> counts -> extract
--per-repo --skip-vendored -> `archive_unpack.py`), folder
`/data/workarea/group4_2/` {batches M42 + test M4T, counts, 4_2_extract,
unpacked}. `archive_unpack.py`: opens by magic bytes, hashes every file
inside (sha256), writes each content once per repo as
`files/<type>/<org>/<repo>/<sha>.<type>`, manifest per repo in
extract_versions.py columns (so pdf_page_merge.py / pkl_to_text.py run on
it unchanged), nested archives to depth 3, limits (2 GB file, 5 GB / 200x
per archive, 100,000 files), names never used to write.
Result (5 Oct): 1,291 repos, 21,934 archive files, 24,551 versions read
(17,435 opened, 6,798 vendored listed, 318 not opened: 238 LFS stubs, 48 not
archives, 17 empty, 15 damaged, 3 limits), **3,022,096 files inside,
1,156,186 distinct written, 71.6 GB**; 20 repos hold 67% (daet = daet-test;
il-model-monitoring hit the 100,000-file limit; gvrt has 2,306 archives).
Report: `archive_unpack_report.py /data/workarea/group4_2/unpacked` ->
`stats/report.md`, `archives_per_repo.csv`, `by_route.csv`, `not_opened.csv`.
By route (distinct): group1_text 829,170 (26.5 GB), **other 187,203 (24.7 GB,
3,891 types: still to look at)**, group4_5_text 75,197, no_extension
25,017 (7.2 GB), group2_binary 23,069, group2_text 11,130, package 3,550,
group3 1,638, db 172, nested archive 40.
Next: look at the "other" and no_extension types (command in the
conversation: by_type.csv filtered on route), then decide: text members as
whole files (now) or lines merged per inner path (group 1 style); pdf/pkl
through the group 3 scripts; lz4 (2 files failed: check `import lz4.frame`).

### Group 4.3: R&D (started 7 Oct)
2,941 values in the sheet; `ts`, `json`, `gitignore` there are stray rows
(1-3 files) of group 1 types -> left out: 2,891 types, 949,392 files.
**Top 30 = 803,691 files (85%)**: `group4_3_top30.txt` (md5
f2a6660dea3f9e5e1d3ed4b9479fdb15) and `group4_3_top30.csv` (counts):
map ttf woff2 _ meta mapping eot pdb cache tree pp otf dtsx bcmap deploy gs
flow m al msapp pcf rds snap pubxml psd1 xdt mo as dat typed.
First guess: fonts/pdb/cache/bcmap/mo exclude; map, snap, code types text;
**dtsx, pubxml (credentials), rds (R data), msapp (zip)** high interest.
Samples command given (user about to run):
`python3 sample_ext_files.py --by-repo /data/workarea/all_ext_counts/by_repo.csv
--repos-root /data/workarea/archive --out /data/workarea/group4_3/samples
--extensions group4_3_top30.txt --per-ext 5` -> `samples/summary.md`,
`index.csv`. Next: decide per type (text / binary whole / container / exclude).

### Group 4.5: text (colleague)
Kit `handover/text_rest_kit` (+ tarball, md5sums): 756 types in
`text_extensions.txt` (Sub Group 4.5; dropped py, duplicate rb /
stylelintignore, "html~stashed changes"), `group4_5_population.csv`.
Colleague (LVM16, user rohitr) edited the defaults in his
run_text_versions.sh (OUT=/data/workarea/output, BATCHES=/data/workarea/batches,
ACTIVE=/home/rohitr/blobcontainer/...). Full run started ~2 Oct, was in
batch G03 (giant repos, 1 of 5 done) on 3 Oct. Copy:
`copy_group4_5.sh` (data -> DST, tracking -> DST_tracking; default DST
`/home/rohitr/eng-gh2-data-fs/diff_analysis/p1/group4_5`, unverified path).
His copy of copy_group4_5.sh had md5 029ee5ed… instead of e8ae0556… (not
CRLF; edited or truncated): unresolved, asked to recopy as a file.

### Group 5: no extension
`sample_noext.py` (md5 12cddf40e690f2d4cc9cda4da18fc63f, uncommitted): 5
random repos, every no-extension path in history, name counts, 10 samples
each -> `/data/workarea/group5_samples/summary.md`. A **full listing** does
not exist: `extension_versions.py` selects by `*.ext` pathspec; proposed a
~5-line change ("(none)" = read all paths, keep those without extension),
then pass 1 + `ext_path_list.py` like 4.1 (7,570 repos). Not done yet.

## Scripts added this session (all committed unless noted)

| Script | Does |
|---|---|
| `group2_tracking_files.py` | list / move tracking files out of a delivery or scanner output folder |
| `extension_versions.py --binary-exts`, `run_ext_versions.sh` EXTS/BINARY_EXTS | count other types as binary; remembered in `<out>/binary_exts.txt` |
| `extract_versions.py --per-repo` | store each content once per repo; `layout.txt` guard |
| `pkl_to_text.py`, `pdf_page_merge.py`, `run_group3.sh`, `copy_group3.sh` | group 3 |
| `ext_path_list.py` | unique paths + commits per repo from a pass 1 folder |
| `archive_unpack.py`, `run_group4_2.sh`, `archive_unpack_report.py` | group 4.2 |
| `handover/text_rest_kit/scripts/copy_group4_5.sh` | group 4.5 copy |
| `progress.sh` | read-only status of all runs |
| `sample_noext.py` | **uncommitted**: group 5 samples |
| `notes/new_extension_template.md` | method explained (exact + plain words) + per-extension template |

Tests: `test_extension_versions.py` (36), `test_smoke.py` (46),
`test_group3.py` (16; PDF tests need pypdfium2/pypdf), `test_group4_2.py`
(15; SEVENZIP=/path/to/7zz for the 7z test). One old test
(`AllExtensionsOddNames`) fails on Python 3.10 only (a file name ending in
"\r"): pre-existing, does not affect current work.

Uncommitted at the end: `sample_noext.py`, `group4_3_top30.txt`,
`group4_3_top30.csv`, this note; plus the older `classify_extensions.py`,
`probe_extensions.py`, `ext_versions_analysis.md` (not reviewed).
