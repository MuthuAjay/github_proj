# Git history PII extraction: process and decisions record

| | |
|---|---|
| Purpose of this document | Record for QRM / review: how the repositories and file types were chosen, how each run worked, the decisions taken, the checks made and the known limitations |
| Status | **Draft for review**, as of 28 Oct 2026 |
| Period covered | 18 Sep – 28 Oct 2026 |
| Prepared by | [to confirm: name / role] |
| Reviewed by | [to confirm] |
| Sources | session notes (`notes/session_handoff_*.md`), team updates and reports (`reports/`), the scripts and their git history (138 commits) |

Items marked **[to confirm]** could not be confirmed from the project records and need an owner's answer before this record is final.

---

## 1. Background and objective

[to confirm: approved wording of the background — e.g. the incident, the "threat actor dataset" of repositories, and who requested the work.]

The repositories in scope had **already been scanned for PII in their current state** (the active repositories). Git also keeps every earlier version of every file, including files that were later deleted, renamed or overwritten. That history had not been scanned.

**Objective:** extract from the git history everything that had **not** been scanned yet, in a form the PII scanning team can read, and deliver it to the file share for scanning. Each delivered file must be traceable back to its repository, path and commits.

Our work ends at delivery. Scanning and the assessment of findings are done by the PII scanning team.

---

## 2. Scope: which repositories

### 2.1 From the requested list to the repositories analysed

| Step | Repositories |
|---|---:|
| Repositories in the threat actor dataset (as provided) | 13,834 |
| In the `github` archive inventory | 13,833 |
| Listed in the inventory but not present in the archive storage | −202 |
| **Repositories analysed** | **13,631** |

- **13,834 vs 13,833:** most likely a counting difference (for example a name in two organisations, or names differing only in letter case). We offered to match the provided list against ours. [to confirm: whether that was done]
- **The 202 missing repositories** were searched for in all three storage locations (the archive used, the archive in blob storage dated 29 Jun 2026, and the active repositories in blob storage dated 13 Aug 2026). We searched by exact name and by similar spellings (case, dashes, underscores), in the repository's own organisation and in every other one:
  - **197 are absent everywhere.** No data exists for them.
  - **4 exist under a slightly different name** and were already included under their real names (for example `ey-org/evolveagenda-portal` = `ey-org/evolve-agenda-portal`).
  - **1** (`ey-fso-tax/core`) matches only a different repository with the same generic name in another organisation (`ey-org/core`). It is treated as absent unless the owner confirms otherwise.
  - The full list is in `missing_repos_check.csv` (`check_missing_repos.py`).
- Reference: `reports/repo_count_reconciliation_2026-09-28.md`.

### 2.2 Data sources

| Source | What it is | How it was used |
|---|---|---|
| **Archive** (`/data/workarea/archive/<org>/<repo>`) | The full git history: git objects only, **no HEAD or branch references** | All history read from here. Ordinary git commands fail on these repositories, so all reads go through a recovery wrapper (`RecoveredRepo`) that rebuilds a readable view. **Read only, never changed.** |
| **Active copy** (blob storage, snapshot of 13 Aug 2026) | The repositories as they are today: the already-scanned state | Used to decide what had already been scanned (section 3.2). Mounted read-only in practice. **Never written to.** |
| **Inventory** | The file list of both copies | Listed most repositories twice, under two roots (`AllRepos\…` and `github/…`). `dedupe_by_root.py` keeps one copy per repository: the one with more files (ties go to `github`, which keeps non-ASCII file names). |

All processing ran on one project server. Extracted data never left the server, except as the deliveries to the file share.

### 2.3 Batches

Repositories were split into batches by workload (`make_batches.py`): the weight is the number of commits touching the file types in scope. The tiers are giant / large / medium / small (G01–G05, L01–L08, M01–M04, S01–S06), and each batch has near-equal total weight. A few dozen repositories make up about 40% of the work, so the giants run in their own batches.

Later runs for a narrower set of types used **targeted batches**: only the repositories that have those types in their history, taken from the all-extension count (`all_ext_counts/by_repo.csv`).

---

## 3. Approach and principles (apply to every run)

### 3.1 Principles

| # | Principle | Why |
|---|---|---|
| P1 | **Only send what has not been scanned.** For types whose current files were scanned, today's content is removed (the "delta"). For types whose current files were never scanned, today's file is included. | Avoid re-sending scanned content; never miss unscanned content. |
| P2 | **Every version in the history is read**, on every branch, including deleted and renamed files. | History-only files are the main target. 6.06 M files exist only in history (section 5.1). |
| P3 | **Strictly per repository.** Deduplication, page matching and caches never compare across repositories, except for whole-file storage (P4). | Findings stay attributable to a repository; one repository's content never hides another's. |
| P4 | **Each distinct content sent once.** Text: each line once per file path. Whole files: each distinct file content once, named by its git blob id, with a manifest mapping it back to every repo, path and commit. | Volume: the same file is often committed in many places. |
| P5 | **Vendored and generated folders are left out but listed**: node_modules, packages, vendor, bin, obj, dist, build, target, site-packages, `__pycache__`, and also macOS metadata (`__MACOSX/`, `._*`, `.DS_Store`) and the DocFX build cache. | Third-party code, not the teams' data. Listed so nothing disappears silently. |
| P6 | **Files are decoded, never executed or loaded.** Pickles are read with `pickletools` and never unpickled. Archives are opened by their content signature, and names inside them are never used to write. Power BI and other containers are read for data only. | Safety: history can hold untrusted or malicious files. |
| P7 | **No OCR.** Pages and images without a text layer are flagged (`ocr_needed`), not converted. | The scanning team does not OCR either. Flagged items are listed (section 8). |
| P8 | **Data stays on the server.** | Real data must not be copied to other machines. |
| P9 | **Tracking files are never in a folder the scanner reads.** Each delivery has a data folder and a separate `*_tracking` folder. | Lesson from group 2 (section 7.4). |
| P10 | **Each run is time-tested** on the largest repository before the full run. | One run (4.5_R) lost over 24 hours to a slow setting that a time test would have caught (section 7.5). |

### 3.2 What "already scanned" means: the delta

For text types whose current files had been scanned, each file is handled as follows (`file_added_lines.py` → `fill_at_head.py` → `file_delta.py`):

1. All versions of the file are merged into one `.txt`: every line it ever had, each line once, in the order the lines first appeared. Lines are trimmed and blank lines dropped. The file holds content only, with no commit ids, authors or dates.
2. **If the file no longer exists today,** the whole history is kept: none of it was scanned.
3. **If it exists today,** every line still present in today's file is removed. Lines that only existed in older versions are kept.
4. If nothing is left, no file is written.
5. **If today's file cannot be read,** the full history is kept. The rules always err towards keeping content.

Matching is exact after trimming and within the same file only. Content that moved to a different file is kept.

For **whole files** (Office, images etc.), a history version is skipped only when its content is **identical to a file in the same repository's active copy** (the user confirmed today's files were scanned for these types).

**Where today's files had not been scanned,** there is no delta and today's file is included: archives (4.2 / 4.2_R), Group 3, and 4.7+4.9.

---

## 4. File types: classification and scope

### 4.1 Source

File types come from the client workbook `All File Extensions_Summary_20261001_V2.xlsx` (sheet "All File Extensions", column **Sub Group**), and later from the project's `ext_groups.xlsx`. [to confirm: who owns the classification and who approved the groups.]

The workbook's "All Versions" column is wrong for some rows (lookup errors), so all counts in this record come from our own count of the whole history (`all_extension_counts.py`): **4,012 extensions, 12,710,793 files, 90,630,855 changes**.

### 4.2 Groups and outcome

| Group | Content | Route | Status (28 Oct) |
|---|---|---|---|
| 1 | 32 text types | Text, with delta | Delivered (27 Sep); 7-repo gap open (section 8) |
| 2 | 27 types: 9 text + 18 whole (Office, images, email, certificates) | Text with delta / whole files minus identical-to-today | Delivered (1 Oct) |
| 3 | pdf, pkl | PDF merged per path; pkl as text | Delivered (≈5 Oct) |
| 4.1 | 28 database types | **Path list only** | Built (7 Oct) [to confirm: copied to the share] |
| 4.1_R | 7 database / export types (bacpac, dacpac, …) | Path list + samples | Path lists built; samples not reviewed |
| 4.2 | 39 archive / package types | Archives opened, contents routed by group; packages listed only | Delivered (16 Oct) |
| 4.2_R | 21 package types (19 run) | Packages opened | Delivered (16 Oct) |
| 4.3 / 4.6 | 2,891 "do not process / unknown" + 148 types | **Classified**, then re-grouped into 4.5_R, 4.2_R and 4.7 | Classification delivered (≈10 Oct) |
| 4.4 | Executables (25 types) | Not processed | **Out of scope** [to confirm: decision and owner] |
| 4.5 | 756 text types | Text, with delta | Run by a team colleague on a second server [to confirm: method, numbers, delivery] |
| 4.5_R | 890 text types re-grouped from 4.3/4.6 | Text, with delta | Delivered (12 Oct) |
| 4.7 + 4.9 | 376 types (merged 19 Oct) | By type: text / whole / data-to-text / containers / per path by content | Delivered in parts 23–28 Oct; 10 types being sampled |
| 4.8 | [to confirm] | Not processed | **Out of scope** [to confirm] |
| 5 | No extension (277,516 files: Dockerfile, `.env`, …) | Not processed | **Not done.** Samples taken; a full listing needs a script change [to confirm: in or out] |

---

## 5. Runs: method, results and decisions per group

Numbers are as reported at the time of each delivery. GB = 10⁹ bytes unless stated.

### 5.1 Group 1: 32 text types (27 Sep)

**Method:** history extraction of every file of these types (`file_added_lines.py`), the "today" flag from the checked-out copy (`fill_at_head.py`), then the delta (`file_delta.py`, section 3.2).

| | |
|---|---:|
| Repositories | 13,631 (all completed) |
| Files analysed | 8,709,398 |
| History lines | 1,721,776,940 |
| Removed as already scanned | 481,866,737 (28.0%) |
| **Sent** | **6,838,128 files, 1,239,910,203 lines** |

What happened to each file:
- **6,059,817 kept in full:** no longer exist today (deleted, renamed or branch-only).
- **778,305 reduced** to their older lines only.
- **1,797,135 not sent:** nothing new.
- **74,135 not sent:** not text, or empty.
- **6 kept in full:** today's file unreadable.

**Incident:** the active-copy mount dropped during the first run. About 10,900 repositories were processed without their current files, so nothing was removed for them (over-sending, never under-sending). The mount was fixed and the process changed to **stop automatically when the mount fails and resume after**. The affected repositories were re-run, and the figures above are from the corrected run.

**History-only analysis (28 Sep, informational):** 6,059,816 files exist only in history, in 8,420 of the 13,631 repositories.
- 30% are third-party code that was committed and later cleaned out (mostly node_modules).
- Of the teams' own files: 50% were deleted, 19% renamed or moved, and 31% are on branches that can't be identified, because the archive keeps no branch names.

Report: `reports/history_only_update_2026-09-28.md`.

### 5.2 Group 2: 27 types (1 Oct)

**Rule (confirmed by the user):** a version is skipped only when its content is a file in the **same repository's** active copy. Identical files across repositories are stored once.

| | |
|---|---|
| Whole files (18 types) | 918,618 versions − 350,760 already scanned − 20,552 Git LFS pointers (content not in the archive) − 351,823 duplicates − 11 empty = **195,472 files (~72 GB)**, hash-verified on the share: 0 bad |
| Text (9 types) | 41,754 files − 7,523 with nothing new − 929 unreadable/empty = **33,302 files** |

**Decision:** certificate files `.pfx` (138) and `.p7s` (1,312) **were sent** (user's choice). pfx can hold private keys; the list is in `reports/certificates.csv`.

**Count explained to the receiving team:** they counted 283,210 files in the folder, which is 228,676 data + 54,534 tracking files. See `reports/group2_counts_explained_2026-10-01.md` and section 7.4.

### 5.3 Group 3: pdf and pkl (≈5 Oct)

Strictly per repository. Today's files included.

- **PDF:** every page is rendered and hashed, and duplicate pages are dropped. The **original** pages are merged into one PDF per path, so their text layer stays. Unreadable versions are copied whole.
  - 28,751 paths → **28,254 PDFs**.
  - 1,116,952 pages → 1,079,664 kept, 37,288 duplicates.
  - **13,449 pages have no text** (`ocr_needed`) and **450 versions are unreadable**.
- **pkl:** strings read with `pickletools`, never unpickled. 1,086 paths → **1,062 `.txt`, 12,445,716 lines**.
- **sentinel, decTest, noun** (build markers, public test data): expected to be excluded after samples. [to confirm: final decision recorded]

### 5.4 Group 4.1 / 4.1_R: database files (path lists)

- **Decision:** database files are listed, not extracted.
- **4.1** (28 types, 730 repositories): `group4_1_db_paths.csv`, one row per unique path with the commits that added, changed or deleted it. Expected about 5,987 paths and 18,422 commits. [to confirm: final numbers and copy to the share]
- **4.1_R** (added 14 Oct, 7 types: bacpac, ctrl, dacpac, dbf_11_8_2018, init, sdb, wal): 887 files in history, 87 inside archives. **bacpac is a full SQL Server export with table data (high risk).** Path lists and samples were built; the samples have not been reviewed.

### 5.5 Group 4.2 / 4.2_R: archives and packages (5–16 Oct)

**Scope agreed (5 Oct):**
- **Opened:** category A archives (zip, gz, tar, 7z, rar, …) not in vendored folders.
- **Listed only:** packages (nupkg, jar, whl, apk, …), asset/disk containers, and vendored archives.
- Today's archives were probably never scanned, so **every version, today's included,** is read.

**Unpacking (`archive_unpack.py`):**
- Archives are opened by their content signature.
- Every file inside is hashed (sha256) and written once per repository.
- Limits: 2 GB per file, 5 GB or 200× per archive, 100,000 files.
- Names inside archives are never used to write.

| | 4.2 archives | 4.2_R packages |
|---|---:|---:|
| Repositories | 1,291 | 242 |
| Files inside | 3,022,096 | 701,158 |
| Distinct written | 1,156,186 (71.6 GB) | 406,036 (25.5 GB) |

**Nested archives (15 Oct):** the first pass opened archives inside archives only 3 levels deep, and kept packages found inside whole. A redo opened every level (to depth 20): 640 packages, 18,485 new output files. 15 remain closed and are kept whole: dxp 7, ppkg 5, ucs 3 (section 9).

**Files inside the archives (14 Oct).** **Decision:** inside archives, use only the groups already run (1, 2, 3, 4.1, 4.1_R, 4.2, 4.2_R, 4.5, 4.5_R), each with **its own method**, and **no delta**. Totals after the nested redo: **1,034,079 files** (4.2: 896,711; 4.2_R: 137,368).

| Group | Method inside archives | 4.2 output | 4.2_R output |
|---|---|---|---|
| 1 text | One `.txt` per path, lines merged | 712,949 `.txt` | 112,375 `.txt` |
| 4.5_R text | Same | 39,285 | 10,489 |
| 4.5 text | Same | 95,110 | 10,972 |
| 2 text | Same | 9,732 | 0 |
| 2 whole | Each distinct content once per repo | 18,256 | 3,525 |
| 3 pkl | Strings, never unpickled | 62 | 0 |
| 3 pdf | Merged per path, no OCR | 2,832 | 7 |
| 4.1 | Path list | list | list |

(These are the figures before the nested redo; see `reports/group4_2_inner_update_2026-10-14.md`.)

**Decisions and findings:**
- `.env.prod.br` (plain text, not brotli, likely secrets): **user chose to leave it out** (listed, not sent).
- A GPO backup of a domain policy was seen in a 4.2 repository (security finding, reported).
- **Copy (16 Oct):** 1,033,777 of 1,034,079 landed. **302 were overwritten by case-only name clashes** (section 7.3).

### 5.6 Groups 4.3 / 4.6: classification (≈10 Oct)

Every type in 4.3 (2,891 + 51 sheet issues) and 4.6 (148) was classified: category, PII risk, proposed target group and version counts (`group4_3_4_6_classification.xlsx` + legend). From this the user built:
- **4.5_R** (890 text types);
- **4.2_R** (21 package types);
- later **4.7** (294 types).

### 5.7 Group 4.5 and 4.5_R: other text types

- **4.5** (756 types): run by a team colleague on a second server with the same text kit (`handover/text_rest_kit`). [to confirm: completion, numbers, delivery.] The 4.5 types *inside archives* were run by us (5.5).
- **4.5_R** (890 types, 12 Oct): delta as group 1, vendored skipped and listed.
  - 356,026 files in history → **290,608 sent, 19,964,659 lines, 25 GB**.
  - 12.59 M of 32.55 M lines (38.7%) were removed as already scanned.
  - 7 repositories timed out and were re-run individually. All 13,631 completed.
  - **Copy:** 290,551 landed; **57 lost to case-only name clashes** (section 7.3).

### 5.8 Group 4.7 + 4.9 (16–28 Oct)

**Decisions:**
- **4.7 and 4.9 are handled as one group** (19 Oct).
- 40 types the user reclassified on 22 Oct were added, giving **376 types**.
- **Today's files of these types had not been scanned,** so today's file is included (no delta).

| Part | Types | Method | Sent: repo level | Sent: inside 4.2 / 4.2_R | Delivered |
|---|---:|---|---|---|---|
| Text (as group 1) | 160 | One `.txt` per path: every line of every version + today's | 13,905 `.txt`, 7.1 M lines, 0.76 GB | 1,022 `.txt` (all inner text types) | 23 Oct |
| Whole files (as group 2) | 117 | History (global dedup) + today's files (`active_files.py`) | 5,078 files, 6.08 GB | 1,012 files, 0.61 GB | 21–23 Oct |
| Serialized data → text | 11 (rds, pickle, npy, …) | `data_to_text.py`: values decoded, numeric arrays and model weights skipped, never loaded | 4,929 `.txt`, 45.7 M lines, 0.52 GB | 211 `.txt` | 27 Oct |
| Table data (whole) | 6 (parquet, orc, avro, …) | Whole files, each distinct content once | 3,235 files, 1.56 GB | 75 files | 27 Oct |
| Containers → data + images | 9 (pbix, dbc, twbx, …) | `bi_to_data.py`: **only the data tables as text and the images**, no layout, queries, code or connections | 13,537 `.txt` (557 GB) + 2,750 images | 96 `.txt` + 28 images | 27 Oct |
| Mixed types, per path by content | 7 (deploy, dump, tmp, out, …) | Text where every version is readable, whole otherwise; programs left out | 4,923 `.txt` + 158 files | 12 `.txt` + 6 files | 28 Oct (copy in progress) |
| Credentials and keys | 16 | — | **out of scope** | — | — |
| Proposed exclude / list only | 10 | — | **being sampled** | — | — |

**Decisions in this group:**
- **Credentials and keys out of scope** (26 Oct): snk, p12, rsa, gpg, keystore, pvk, keytab, kdbx and others (16 types). [to confirm: decided by whom.] Two CyberArk files named `cred-dont use` inside archives are treated as credentials too. [to confirm]
- **Correction:** the 23 Oct update listed files named `creds` / `cred-dontuse` as sent. They were not (their type names lost characters in the lookup). A correction is in the 27 Oct update.
- **Containers:** the scanner team **agreed to the 557 GB of container text** (single files up to 18 GB).
- **Duplicate container text, repository scope** (27 Oct): 1,633 `.txt` identical to another path in the same repository were removed (340 GB); `same_as.csv` records the kept copy for each. Deduplicating across repositories would have saved 225 GB more, but would have left whole repositories with nothing (for example about 20 `ce5-*` repositories share the same 220 reports), so it was not done.
- **pickle text** keeps the raw-string scan lines (40.3 M of the 45.7 M lines). A strict filter exists but was not applied (user, 26 Oct). Expect noise.
- **Mixed types** (28 Oct): the file name does not say what is inside. `.deploy` is a ClickOnce suffix on any file (XML, images, programs), and `.dump` / `.out` / `.tmp` can be text logs, database dumps or binary. So **each path was routed by its content**:
  - text if every version is readable;
  - whole file otherwise;
  - `.dll` / `.exe` programs (153 paths) and 2 ELF programs **left out** (user: "leave out dll exe");
  - 21 paths with no text not sent.

  A check confirmed that names and contents agree: every xsd/xml/config went as text, every image whole, and all 85 left-out dll/exe versions are programs.

---

## 6. Decision log

"User" = the project lead for this work [to confirm role title]. Decisions by others are marked.

| Date | Decision | By | Reason / impact |
|---|---|---|---|
| Sep | Use the archive for history and the 13 Aug active copy as "already scanned" | [to confirm] | Only the history is unscanned |
| 27 Sep | Text delta rules: exact line match after trimming, per file, keep when in doubt | Project team | Never drop unscanned content |
| 28 Sep | 202 missing repositories: searched in 3 locations; 197 absent, 4 renamed (included), 1 treated as absent | Project team, reported | No data available |
| 1 Oct | Group 2: skip a version only if identical to a file in the same repo's active copy | User | Same-repo rule; cross-repo copies stored once |
| 1 Oct | Send `.pfx` / `.p7s` certificates | User | Listed in `certificates.csv` |
| 2 Oct | Move scanner results of tracking files out of the scanner output | Project team | Section 7.4 |
| ≈5 Oct | Group 3: strictly per repo, no OCR, today's files included | User | — |
| 5 Oct | 4.2: open category A archives only; packages, assets, vendored listed | User / scanner team [to confirm] | — |
| 7 Oct | 4.1 database files: path list only | User | Not extracted |
| 12 Oct | `.env.prod.br` left out | User | Listed, not sent |
| 13 Oct | macOS metadata, DocFX cache, vendored: left out of archive contents, listed | User | No content / third-party |
| 14 Oct | Inside archives: only groups already run, each with its own method, no delta | User | Archives were never scanned |
| 14 Oct | 4.1_R added to scope | User | bacpac = full DB export |
| 19 Oct | 4.7 and 4.9 merged; today's files included | User | Not scanned before |
| 22 Oct | 40 reclassified types added to 4.9 (376 types) | User | — |
| 26 Oct | Credentials and keys out of scope (16 types) | [to confirm] | Secrets, not PII |
| 26 Oct | Tabular formats as whole files; other serialized types to text | User | Scanner reads text |
| 26 Oct | Containers: data and images only | User | No layout, code, connections |
| 26 Oct | pickle raw-scan lines kept | User | Noise accepted |
| 27 Oct | Container text deduplicated within each repository, not across | User | Keeps every repository represented |
| 27 Oct | Container text size accepted | Scanner team | — |
| 27 Oct | Mixed types: route by content; leave out dll / exe | User | Programs, not data |
| 28 Oct | 2 ELF programs left out like dll / exe | Project team, per the above | Same reason |
| 28 Oct | 10 proposed-exclude types sampled before deciding | User | Verify before excluding |
| [to confirm] | 4.4, 4.8 and Group 5 out of scope | [to confirm] | [to confirm] |

---

## 7. Quality controls and verification

### 7.1 Controls built into every run
- **Read only on sources.** The archive is never changed; the active copy is never written to.
- **Resumable runs:** per-repository `done.json` and per-batch markers. A run that stops (low disk, failed mount) exits with code 3 and continues from where it stopped when started again.
- **Mount check:** if the active copy stops answering, the run stops rather than treating files as missing (added after the 27 Sep incident).
- **Every repository accounted for:** failed or timed-out repositories are listed and re-run individually (for example 7 in 4.5_R).
- **Scripts:** written and tested on a development machine with automated tests, copied to the server as files, and checked by md5 before each run.

### 7.2 Reconciliations
- **Scope counts tie to the classification counts per type.** Differences are explained: for example 50 editor-backup files in 4.2 group 1, all in vendored folders.
- **Source = share** after every copy: file counts per folder, and names and sizes.
- **Whole files are hash-checked on the share against their name** (git blob id or sha256). Every check so far: 0 bad.
- **Whole-file index (28 Oct):** every content-named whole file maps to its repository, path and commits. Its counts match the stores exactly: group 2 195,472; 4.7+4.9 group 1 334, group 2 5,078, tabular 3,235, mixed 160 (158 after the programs are moved). See 7.6.

### 7.3 File share limitations found
- **The share ignores letter case.** Two names that differ only in case overwrite each other.
  - Losses: 302 files in the 4.2 copy and 57 in 4.5_R.
  - **Fixed from 4.7+4.9 onward:** such files are sent as `<name>__case2.<ext>`, with the mapping in `case_renamed.csv` (71 in 4.7+4.9 group 1, 19 in the containers / Group 3 text).
  - [to confirm: the 302 (4.2) and 57 (4.5_R) re-sent]
- **Group 2 text:** 98 of 33,302 files were missing on the share after the copy, probably for the same reason. [to confirm: resolved]

### 7.4 Tracking files scanned by mistake (group 2)
The scanning team scanned the whole group 2 folder, our tracking files included. The 651 results for tracking files were moved out of the scanner output (26,732 data results kept; 0 left, verified).

**Control added:** tracking files are always delivered to separate `*_tracking` folders, and every copy checks that no tracking file is among the data.

### 7.5 Performance incident (4.5_R)
A run with 890 file types lost more than 24 hours: matching types with git patterns was about 200× slower than expected. It was replaced by matching in Python (same output, verified).

**Control added:** time the exact command on the largest repository before every big run.

### 7.6 Traceability
- **Text:** delivered text mirrors `<org>/<repo>/<path>.txt` (inside archives: `<archive path>/<path inside>.txt`), so a finding points straight to the file.
- **Whole files:** named by content and stored once across repositories, so **the repository is not in their path**. The manifests map each one to every repository, path and commit.
  - On 28 Oct the scanner team asked for the repository of 9 priority documents. All 9 were traced to one repository with 36 paths, within minutes.
  - A full index (`whole_file_index.csv`, 204,279 files, 568,047 rows) is being put on the share so the team can look files up themselves.

---

## 8. Known limitations and residual risks

| # | Limitation | Effect | Mitigation / status |
|---|---|---|---|
| L1 | **No OCR** | 13,449 PDF pages (Group 3) and 2,027 pages inside archives have no text layer. The scanner skipped **137,589 images** in group 2 (`skipped_ocr_required`) and 10,448 binaries with no parser, so only about 78,000 of 228,676 group 2 files were actually scanned | Listed per page / file; raised with the user (7 Oct). [to confirm: decision on OCR] |
| L2 | **202 repositories not available** | Not analysed | Search documented (2.1) |
| L3 | **Git LFS pointers** | Content not in the archive (20,552 versions in group 2; 1,760 parquet versions; others) | Counted and listed |
| L4 | **Group 1: 7 repositories failed in the first extraction** (+26,800 files never sent), 4 more missed 11 files | Gap in group 1 | Validated 1 Oct (`validate_group1_counts.py`). [to confirm: re-run done?] |
| L5 | **Branch information not in the archive** | 31% of history-only own files can't be placed on a branch | Content extracted regardless (P2) |
| L6 | **Out of scope by decision:** credentials/keys (16 types), dll/exe programs, 4.4, 4.8, Group 5 (277,516 no-extension files incl. `.env`) | Not scanned | Decision log (6) |
| L7 | **Damaged or unreadable files** | Not read: 450 PDF versions, 5 damaged pbix versions, 15 archives, 1 pbix table, and others | Listed in each run's summary |
| L8 | **Noise in pickle text** | 40.3 M raw-string lines | Accepted (26 Oct) |
| L9 | **Archives kept closed:** dxp 7 (Spotfire, client lease data), ppkg 5 (may hold Wi-Fi keys / admin passwords), ucs 3 | Not opened | Open decision |
| L10 | **Group 4.5 run outside this project's server** | Not verified by us | [to confirm] |
| L11 | **Case-only name clashes before 23 Oct** | 302 + 57 files overwritten on the share | 7.3 |
| L12 | **Container data is large** (557 GB, single files up to 18 GB) | Scanner load | Agreed by the scanner team |

---

## 9. Open items (28 Oct)

1. Mixed types: finish the copy and confirm the counts (4,923 + 12 `.txt`, 158 + 6 files).
2. Whole-file index: copy it to `p1/whole_file_index_tracking/`.
3. 10 proposed-exclude types (arff, xcuserstate, test-db, train, sublime-workspace, ftz, rsuser, 2ph, mmdb; jar1 list only): sampled 28 Oct, decision pending.
4. 460 `.dbc` files that are plain notebook source (may hold storage keys): send as text or not.
5. dxp / ppkg / ucs closed archives: open or not.
6. 4.1_R samples review.
7. Group 3 text: apply the one remaining case rename (4,928 → 4,929 on the share).
8. Delete the removed duplicate container text from the server (317 GiB) once confirmed.
9. [to confirm] Re-send the 302 (4.2) and 57 (4.5_R) case-clash files; the 98 group 2 text files.
10. [to confirm] Group 1 7-repo gap.
11. [to confirm] Group 5, 4.4, 4.8 scope; OCR.
12. [to confirm] CyberArk `cred-dont use` files treated as credentials.

---

## 10. Where the deliveries are

All under `diff_analysis/p1/` on the file share. Scan only the data folders; the tracking folders are records.

| Delivery | Data (scan) | Tracking (do not scan) |
|---|---|---|
| Group 2 | `group2/binary/files/`, `group2/text/` | `group2/binary/manifest.csv`, `group2/text/_state/` (delivered together, before P9) |
| Group 3 | `group3/pdf/`, `group3/pkl/` | `group3_tracking/` |
| 4.1 | — | `group4_1/` (path lists) [to confirm] |
| 4.2 / 4.2_R | `group4_2/<step>/`, `group4_2_R/<step>/` | `group4_2_tracking/`, `group4_2_R_tracking/` |
| 4.5_R | `group4_5_R/` | `group4_5_R_tracking/` |
| 4.7+4.9 | `group4_7_9/{group1_text, group2_binary/files, group3_text, tabular_binary/files, bi_data/{data_text,files}, mixed_text, mixed_binary/files}`, `group4_7_9/group4_2/…`, `group4_7_9/group4_2_R/…` | `group4_7_9_tracking/` |
| Whole-file index | — | `whole_file_index_tracking/` |
| Group 1 | [to confirm: share location of the 27 Sep delivery] | |

---

## Appendix A: Glossary

| Term | Meaning |
|---|---|
| Active copy | The repositories as they are today (13 Aug 2026 snapshot), already scanned |
| Archive | The git history of each repository (objects only) |
| Version | One content of a file in one commit |
| Delta | History lines minus the lines of today's file |
| Vendored | Third-party or generated folders (node_modules, packages, bin, …) |
| Blob id | Git's content hash of a file; the name of each delivered whole file |
| Manifest | Per-run CSV mapping every output file to its repository, path, versions and commits |
| Tracking folder | Share folder with the manifests and records of a delivery; not for scanning |
| `__case2` | Rename applied when two names differ only by letter case on the share |
| Git LFS pointer | A small stub committed instead of a large file; the content is not in the archive |

## Appendix B: Main scripts

| Stage | Scripts |
|---|---|
| Repository list and batches | `dedupe_by_root.py`, `repo_extension_summary.py`, `make_batches.py`, `check_missing_repos.py` |
| Counting | `all_extension_counts.py`, `extension_versions.py` |
| Text | `file_added_lines.py`, `fill_at_head.py`, `file_delta.py`, `run_text_versions.sh` |
| Whole files | `extract_versions.py`, `active_files.py` |
| Group 3 | `pdf_page_merge.py`, `pkl_to_text.py`, `run_group3.sh` |
| Archives | `archive_unpack.py`, `group4_2_text.py`, `group4_2_files.py`, `group4_2_nested.py`, `group4_2_path_list.py` |
| 4.7+4.9 | `data_to_text.py`, `bi_to_data.py`, `dedupe_text_paths.py`, `run_group4_7_9_mixed.sh`, `check_group4_7_9_mixed.py` |
| Samples and classification | `sample_ext_files.py`, `extension_rnd.py`, `ext_path_list.py` |
| Copy and checks | `copy_group*.sh`, `validate_group1_counts.py`, `progress.sh` |
| Traceability | `find_blob_repos.py`, `build_whole_file_index.py` |

## Appendix C: Related records

- `reports/delta_update_2026-09-27.md`: Group 1 delta and method
- `reports/history_only_update_2026-09-28.md`: history-only files
- `reports/repo_count_reconciliation_2026-09-28.md`: repository count
- `reports/group2_counts_explained_2026-10-01.md`: Group 2 counts
- `reports/group4_2_inner_update_2026-10-14.md`, `reports/group4_2_scan_paths_2026-10-16.md`: archives
- `reports/group4_7_9_group2_stats_2026-10-21.md`, `reports/group4_7_9_group1_and_inner_update_2026-10-23.md`, `reports/group4_7_9_remaining_update_2026-10-27.md`, `reports/group4_7_9_delivery_stats_2026-10-27.xlsx`: 4.7+4.9
- `notes/session_handoff_2026-10-{01,07,12,19,27}.md`: working notes
