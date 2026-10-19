# Session handoff (19 Oct 2026): Git history PII extraction

Covers 14-16 Oct. Previous handoff: `notes/session_handoff_2026-10-12.md`
(8-13 Oct: 4.3/4.6 classification, 4.5_R, 4.2_R, group 1 inside archives),
before it `notes/session_handoff_2026-10-07.md` (how we work, groups 1-3,
server layout). Server: `ganeshk@USSPNXT1778009520LVM20`, scripts in
`/data/workarea/scripts`, Python 3.10.

**Next up: group 4.7** (section below). Plan agreed, odd type names tested
OK. Next code: (1) the 4.7 type list files, (2) two 4.7 steps in
`run_group4_2_inner.sh`, (3) the case-clash rename in the copy (also fixes
the 302 lost 4.2 files). Decisions still with the user: hold the 16
key/cert types? text as delta (as 4.5_R)? confirm today's files of these
types were scanned.

## How we work (additions)

- Unchanged: commit only when the user says "commit it"; give md5s, the user
  checks them by hand; copy scripts to the server as files; nohup runners;
  share writes need `sudo bash -c '... nohup ... &'`; real data stays on the
  server; tracking files never in a folder the scanner reads.
- **Pass `STEPS` explicitly to child runners.** A user's `STEPS` leaks into a
  runner started from another runner (`unknown step 'snapshot'` on 15 Oct).
- **Written CSVs have CRLF line ends**: in shell use `tr -d '\r'`, or match
  `",ok.$"` not `",ok$"`.
- **Run logs print GiB as "GB"** (21.3 "GB" = 22.87e9 bytes). The 14 Oct
  mail's group 1 "19.1 GB" is GiB (= 20.5 GB). 4.2_R group 1 "13.9" is
  probably GiB too, still to confirm:
  `grep '"bytes"' /data/workarea/group4_2_R/group1_text/stats/summary.json`.
- `du` over the share mount (1 M files) takes very long: rely on the copy
  script's own count check.
- Excel here: no LibreOffice; build with openpyxl from the scratchpad venv
  (`<scratchpad>/venv/bin/python`), check formulas in Python, set
  `fullCalcOnLoad`. Report xlsx/md under `reports/` are gitignored
  (`git add -f` when asked to commit).

## Inside the 4.2 / 4.2_R archives: other groups (14 Oct, DONE)

Scope (user, 14 Oct): only groups already run - 1, 2, 3, 4.1, 4.1_R, 4.2,
4.2_R, 4.5, 4.5_R. Out: 4.4, 4.4_R, 4.7, 4.8, 4.9, 5 (_noext), types not in
`ext_groups.xlsx`.

- `run_group4_2_inner.sh` steps 4.5_R, 4.5, 2_text, 2_bin (`group4_2_files.py`),
  3_pkl, 3_pdf (`--archive-inner` in `pkl_to_text.py` / `pdf_page_merge.py`);
  output in `/data/workarea/<run>/<step folder>`:
  group1_text, group4_5_R_text, group4_5_text, group2_text, group2_binary,
  group3_pkl_text, group3_pdf_merged. Vendored / macOS / DocFX cache left
  out and listed in `_state/<org>/<repo>/left_out.csv`.
- 4.1 inside archives: `group4_2_path_list.py` ->
  `/data/workarea/group4_2/group4_1_paths/group4_1_inner_paths.csv`.
  4.1_R: `run_group4_1_R.sh` (batches, counts, paths, inner, samples;
  887 files in history, 87 inside archives; samples not reviewed yet).
  `/data/workarea/group4_2/group4_1_inner` (92K) is not ours - unknown.
- Mail: `reports/group4_2_inner_update_2026-10-14.md` (+ xlsx). Its numbers
  are the BEFORE of the nested redo below.

## Nested redo: everything inside the archives opened (15 Oct, DONE)

First unpack opened archives in archives only 3 levels deep and kept 4.2_R
packages found inside (msapp, sppkg, vsix...) whole.

- `archive_unpack.py --nested-extensions` (default ARCHIVE_EXTS; now also
  the 19 4.2_R types), `--max-depth 20`. Bug fixed: a nested archive opened
  only in part was kept whole; now counts as opened, status goes up.
- `group4_2_nested.py` (find / compare / delta / report) +
  `run_group4_2_nested.sh` (steps find backup unpack compare | snapshot
  reset inner lists delta report). Old outputs hard-linked to
  `<run>/nested/before_out`, old manifests in `<run>/nested/before`.
- Result: 4.2: 95 repos, 640 packages opened, 34,672 files inside (33,470 in
  scope; 1,202 out-of-scope types). 18,485 new output files (~3.75 GB); no
  existing output changed; 21 "gone" group 2 binaries = renamed. Report
  `nested/by_extension_report.csv`: 0 rows to CHECK. **4.2_R: nothing
  closed, unchanged.**
- 15 still closed (kept whole): dxp 7 (Spotfire, client lease data), ppkg 5
  (provisioning packages, may hold Wi-Fi keys / admin passwords), ucs 3.
  Open decisions for the user. conmgr (4.7, SSIS connection strings) seen
  inside packages: possible exception.
- Totals after (both runs): input 3,143,394, output **1,034,079** files
  (4.2 896,711 / 31.4 GB; 4.2_R 137,368). Excel:
  `reports/group4_2_nested_update_2026-10-15.xlsx` (4.2 Archives,
  4.2_R Packages, Packages opened, Excluded & other; gitignored).
  A mail with the after numbers was offered, not written.

## Copy of 4.2 + 4.2_R to the share (16 Oct, DONE)

`copy_group4_2.sh` (813f591, md5 0f681c04a6c957907956b324bc88752f): both
runs, 7 step folders, 16 rsync in parallel per repo folder, name checks,
per-step count check, `ONLY=verify`, `CHECKSUM=1`.

- Data: `/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_2/<step>/`
  and `.../p1/group4_2_R/<step>/`; tracking (`_state`, stats, summary.csv,
  nested reports, 4.1 path list, copy checks) in `p1/group4_2_tracking/`,
  `p1/group4_2_R_tracking/`. Scan paths for the PII team:
  `reports/group4_2_scan_paths_2026-10-16.md` (5b9da68). User shared them.
- Ran 06:36-12:58; 3,428/3,428 repo folders, no rsync failure.
  **1,033,777 of 1,034,079 on the share: 302 lost to case-only name clashes**
  (share is case-insensitive; second file overwrites the first): 4.2
  group1_text 121, 4.5_R 5, 4.5 6, pdf 2; 4.2_R group1_text 152, 4.5_R 11,
  4.5 5. List: `/data/workarea/group4_2/copy_checks/name_problems.csv`
  (1,030 lines; the 427 "character not allowed" ones all landed - the mount
  maps them).
- **Open: resend the 302 as `<name>__case2<ext>`** in the same folder, list
  old -> new in tracking. Same issue lost 57 files in 4.5_R.
- `ONLY=verify CHECKSUM=1` content check not run yet.

## Group 4.7 (16 Oct, exploring; nothing run)

User's list `group4_7.xlsx` (294 types, Sheet1: ext, subgroup). All come
from the 7 Oct 4.3 (234) / 4.6 (60) classification. Excel:
`reports/group4_7_exploration_2026-10-16.xlsx` (Summary + Types with
proposed_route and note; gitignored; build script in the scratchpad, not
kept).

- 30,346 files, 164,675 versions, 4% vendored. rpt 9,437 files (8 repos,
  Crystal Reports, binary) + rdl 9,026 (72 repos, 114,068 versions = 69%
  of all) dominate; then tmdl 2,545, snk 720, vsdx 699, conmgr 651.
- Proposed route: Group 1 text 160 types / 14,809 files; Group 2 whole file
  116 / 14,057; **hold: 16 key/cert types / 1,360** (snk, p12, rsa, gpg,
  keystore, pvk, pgp, keytab, der, keybackup, encrypted, mobileprovision,
  pkcs12, kdbx...: secrets, not PII); sample first `out` (119); list only
  `jar1`. 70 of the types are also inside the 4.2 archives (screenshot 514,
  conmgr 438, rpt 158, xsn 149, svn-base 140).
- Flags: connection strings (conmgr, rsds, tmdl, bim, dim, smproj, dbproj,
  pbism); real data (rdl.data, evtx, dmp, hprof, pcap, mpp, oc5, qdf,
  vector stores); 124 backup/old-copy types (small, may keep old secrets).
- **Plan (no script changes for the main runs):** batch via
  `run_group4_1_R.sh` STEPS=batches with `G=/data/workarea/group4_7` and
  4.7 EXTS (file named M41R.csv); counts `run_ext_versions.sh` (EXTS all,
  BINARY_EXTS the 116); text `run_text_versions.sh` with
  `MATCH_IN_PYTHON=1 SKIP_VENDORED=1`; whole file `run_extract_versions.sh`
  with `EXTRA="--extensions <file>"`; time test on the biggest rdl repo
  first; inside archives: add two steps to `run_group4_2_inner.sh`; copy
  script with the case-clash rename; Excel + mail.
- **Odd names tested (16 Oct):** 44 names with # ~ - _ / digits only
  (csv#, xlsx#, ~vsdx, 18, 18_0, __backup, backup-20251028-111447,
  svn-base, jpg_large, svg_...) + decoys through extension_versions.py,
  file_added_lines.py, extract_versions.py, git patterns and
  --match-in-python: all exact; bash passes them through EXTS unchanged.
  Test script in the scratchpad (`odd_names_test.py`), not kept.

## Code changes 14-16 Oct (all committed)

| Commit | What |
|---|---|
| 5d26120 | `group4_2_files.py`: group 2 binary inside archives |
| 857af9a, 497b32a | `run_group4_2_inner.sh`: 4.5_R, 4.5, 2_text, 2_bin, 3_pkl, 3_pdf |
| a737ca2 | `pdf_page_merge.py`, `pkl_to_text.py` `--archive-inner` |
| e0525be, 9dfb0f1 | `group4_2_path_list.py`; `run_group4_1_R.sh` |
| 74a49ba..f541684 | 14 Oct mail |
| 14a98f5 | archive_unpack `--nested-extensions`, partial-open fix, test |
| 12d559f, 733de65, 23c014f, fb42b7f | `group4_2_nested.py`, `run_group4_2_nested.sh` (+ `test_group4_2_nested.py`) |
| 813f591 | `copy_group4_2.sh` |
| 5b9da68 | `reports/group4_2_scan_paths_2026-10-16.md` |

Uncommitted (as before): classification legends, `sample_noext.py`,
`classify_extensions.py`, `probe_extensions.py`, `ext_versions_analysis.md`,
this note.

## Still open

- 4.7: decisions above, then build + run.
- 4.2: resend 302 case clashes; checksum verify; after-numbers mail;
  dxp / ppkg / ucs decisions; 4.2_R group 1 GiB check.
- 4.1_R samples review; PDF 3,005 scope confirmation (4.2).
- Earlier: billing repo redo; 40 "Sample first" types; ovf (user runs
  later, every version); group 5 full listing; group 1 7-repo gap;
  colleague's 4.5 kit without `--match-in-python`.
