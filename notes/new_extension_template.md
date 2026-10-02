# Template: extracting a new file extension for the PII scan

Use one copy of this file per extension (or per small group of extensions).
Part A explains how the extraction works, each idea both exactly and in
plain words; Part B is the step-by-step with blanks (`____`) to fill in.
Everything here follows the method already used for group 1 (32 text types)
and group 2 (27 types), so new numbers line up with what was delivered before.

---

## Part A: How the extraction works

Each idea below is explained twice: first **exactly** (what the scripts
do), then **in plain words** (the same idea without the technical terms).

### A1. What we are doing

**Exactly:** the files in each repo as of 13 Aug 2026 have already been
scanned for PII. Git keeps every earlier version of every file too: changed
files, deleted files and files that only exist on other branches. None of
that history has been scanned. We pull the history out of git, remove
whatever is the same as what was already scanned, and put the rest on the
file share for the scanning team.

**In plain words:** imagine a team that writes documents and keeps every
draft they ever made in a cabinet. Someone has already checked today's
final versions for personal data. Nobody has checked the old drafts, and an
old draft can still contain a name or a phone number that was later
removed. Our job is to take the old drafts out, set aside any that are
exactly the same as a checked final version, and hand the rest over to be
checked.

### A2. The two copies of the repos: the archive and the active copy

| | Archive | Active copy |
|---|---|---|
| **Exactly** | All 13,631 repos as git data only: every commit, every branch, every old version. It has no working files and **no HEAD/refs** (git's pointers to "the current version"), so ordinary `git` commands fail on it. The scripts read it through `RecoveredRepo`, which finds every commit stored inside. | The checked-out files of each repo as of **13 Aug 2026**: what you would see if you opened the repo's folder. These are the files **already scanned**. It is a network drive (blobfuse) that **drops now and then** (`Errno 107`, "Transport endpoint is not connected"). Nothing is ever written to it. |
| **In plain words** | The cabinet with every draft ever made, but without the label saying which draft is the final one. We have a tool that can still open every drawer. | The final versions on the desk today. They were already checked. The desk is in another building and the line to it sometimes cuts out. |
| **Where (server)** | `/data/workarea/archive/<org>/<repo>` | `/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos/<org>/<repo>` |

We take the content from the archive. We use the active copy only to find
out what was already checked.

### A3. Words used in the reports

**File**
- Exactly: one path in one repo (`org/repo:path`), however many versions
  it had.
- In plain words: one document, e.g. `contracts/offer.pdf` in one project.

**Change (version)**
- Exactly: one commit that changed a file. A file edited 10 times has 10
  changes. "Versions" in every delivered table means this.
- In plain words: each time someone saved a new draft of the document.

**Distinct version**
- Exactly: one distinct content of a file. If two commits leave exactly the
  same bytes (e.g. a change and then its undo), that is 2 changes but 1
  distinct version. Reported as `distinct_versions`.
- In plain words: the drafts that actually differ. Saving the same text
  twice is still one draft.

**Blob**
- Exactly: git's id for a piece of content, a hash of its bytes. The same
  bytes always give the same blob id, in any repo or path.
- In plain words: a fingerprint of the content. Two files with the same
  fingerprint are exactly the same inside, whatever they are called.

**At head**
- Exactly: `yes` = the path still exists in the active copy; `no` = it does
  not (deleted, renamed or only on another branch); blank = the whole repo
  has no folder in the active copy.
- In plain words: is this document still on the desk today?

**Identical to active**
- Exactly: a history version whose bytes are the same as the same repo's
  active file (the latest one, an older one after a revert, or the same file
  moved to another path). It was already scanned, so it is skipped.
- In plain words: an old draft that is word for word today's checked
  version. Checking it again would be wasted work.

**History only**
- Exactly: the file is not in the active copy, so all its versions are
  sent.
- In plain words: the document was thrown away or moved off the desk, so
  nobody ever checked any of its drafts.

**No active repo**
- Exactly: the repo has no folder in the active copy at all; everything in
  its history is sent.
- In plain words: the whole project is gone from the desk, so all of it
  needs checking.

**Vendored**
- Exactly: the file sits under `node_modules`, `packages`, `bin`, `obj` or a
  similar folder of third-party or generated code. It is counted and **sent
  anyway**; the flag only marks it.
- In plain words: material copied in from outside (other companies'
  libraries, build output). It rarely holds our data, but we send it anyway
  to be safe and mark it so it can be told apart.

**LFS stub**
- Exactly: for large files stored with Git LFS, git keeps only a small
  pointer (a hash and a size); the real file lives on another server we do
  not have. Nothing to send.
- In plain words: a note saying "the real file is in another storeroom".
  We only have the note, so there is nothing to check.

**Duplicate**
- Exactly: the same content (same blob) found in another path, repo or
  commit. It is stored and sent **once**; `manifest.csv` lists every place
  it came from, so a finding maps back to all of them.
- In plain words: the same document copied into many projects (e.g. a logo
  used in 134 repos). We send one copy and keep a list of every place it
  appears.

### A4. The skip rule

**Exactly:** a history version is skipped **only** when its content is a
file in the **same repo's** active copy, because only that was scanned.
Everything else is sent: older versions, deleted files, other branches and
repos with no active copy. Identical content in different repos is stored
once (deduplicated), never skipped.

This only holds if the active copy's files **of that type** were scanned
before. If they were not, nothing is skipped and today's version is sent
too. Confirm this per extension (Part B, step 2).

**In plain words:** we only leave out a draft when it is exactly the same as
a document that was already checked in the same project. If a type of
document was never checked at all, we send everything, including today's
version.

### A5. Two tracks: text and binary

Files are handled in one of two ways, depending on whether they can be read
as lines of text.

#### Text track: send only the new lines

For source code, configuration and similar files.

**Exactly:**
1. `file_added_lines.py` writes one `.txt` per file holding every line the
   file ever had, once each, in the order they first appeared. The first
   version counts in full; each later version adds only the lines its commit
   added (removed lines were already in an earlier version). Lines are
   trimmed of spaces, blank lines are dropped, and a line already written
   for that file is not written again. The `.txt` holds content only: no
   commit ids, authors or dates.
2. `fill_at_head.py` marks each file `at_head = yes/no` from the active copy.
3. `file_delta.py` writes the **delta** (what is left to scan) into a new
   folder:
   - file in the active copy: the history lines **minus** the current
     file's lines. If nothing is left, no file is written.
   - file not in the active copy, or the repo is not there: the whole
     history file.
   - current file unreadable: the whole history, so nothing unscanned is
     ever dropped.

Lines are deduplicated **within** a file, not across files.
`run_text_versions.sh` runs all three steps (`EXTS="..."` picks the
extensions).

**In plain words:** for a text document we collect every sentence it ever
had into one list, each sentence once. Then we cross out every sentence
still in today's version, because that was already checked. What is left
are the sentences that were deleted along the way: exactly what nobody has
checked. If nothing is left, we send nothing for that document.

#### Binary track: send whole files

For documents, spreadsheets, images, certificates: files that are not lines
of text (pdf, xlsx, docx, png, pfx, ...).

**Exactly:**
1. `extension_versions.py` **pass 1** lists every version of every file on
   every branch, with its size, whether it is an LFS stub and whether the
   file is at head. Nothing is extracted yet; it only counts.
2. **Pass 2** (`--identical`) computes the fingerprint of each active file
   and checks whether it equals one of its history versions (the latest, an
   older one, or another path in the same repo). Those versions are the
   "already scanned" ones.
3. `extract_versions.py` writes every other version out of the archive as a
   real file, named by its blob and stored once per extension:
   `files/<ext>/<first 2 of blob>/<blob>.<ext>`. Each written file is
   fingerprinted again and must match its blob id, or it is dropped and
   reported. `manifest.csv` has one row per occurrence (org, repo, path,
   version, why it is sent) pointing to the stored file. See
   `notes/binary_dedup_mapping.md`.

`run_ext_versions.sh` runs passes 1 and 2, `run_extract_versions.sh` runs the
extraction, and `ext_versions_analysis.py` writes the report.

**In plain words:** a spreadsheet or a PDF cannot be split into sentences,
so each old version is sent as a whole file. First we count every old
version (pass 1). Then we check which ones are the same as today's checked
file (pass 2) and leave those out. Each remaining file is saved once under
its fingerprint, even if it appears in many places, and a list (the
manifest) records every place it came from. After saving, we fingerprint
each file again to prove it was copied without damage.

#### Types that need both

**Exactly:** `.pkl` (Python pickle) is sent whole **plus** a `.txt` of the
strings inside it, named by the same blob so findings map back. Pickles are
read with `pickletools` and **never unpickled** (loading one can run code).

**In plain words:** some files are in a format the scanner cannot read. We
send the file, plus a readable text copy of the words inside it. We read
these files safely, without opening them the normal way, because opening
them can run hidden instructions.

### A6. How runs work on the server

| Rule | Exactly | In plain words |
|---|---|---|
| Two machines | Scripts are written and tested on the dev machine, copied by hand to `/data/workarea/scripts`, and checked with `md5sum` on both sides. | We build and test on one computer, copy to the big one, and compare fingerprints to be sure the copy is identical. |
| Detached runs | `nohup ./X.sh > /data/workarea/X.out 2>&1 &` | Runs take hours or days. This keeps them going if our connection drops, and writes their progress to a log file. |
| Resume | Each repo writes `done.json` last; each batch leaves a marker in `_logs/`. Rerunning the same command carries on. | If a run stops, start it again: it skips what is finished. |
| Batches | `/data/workarea/file_history_out_4/batches/` (G01–G05, L01–L08, M01–M04, S01–S06, order in `plan.csv`) | The 13,631 repos are split into groups by size so the work goes in manageable chunks. |
| Exit codes | **0** ok; **1** some repos failed (retried once, named in the log); **3** stopped (disk low or the active-copy mount not answering: remount and run the same command again). | 0 = all good. 1 = mostly done, a few projects need a look. 3 = paused on purpose: fix the disk or the connection and start it again. |
| Share needs sudo | `sudo bash -c 'cd /data/workarea/scripts && nohup ./X.sh > /data/workarea/X.out 2>&1 &'` (a background `sudo nohup ...` waits for a password nobody sees). | Only an administrator can write to the share, so the whole command runs as administrator in one go. |

---

## Part B: Steps for one extension

### Extension card

**In plain words:** a quick profile of the file type: how many projects and documents have it, how many drafts there are, and what kind of file it is.

From `/data/workarea/all_ext_counts/extension_counts.csv` (whole history).
The pdf column is a worked example.

| Field | This extension | Example: pdf |
|---|---|---|
| Extension | ____ | pdf |
| Repos | ____ | 1,110 |
| Files | ____ | 44,432 |
| Changes (versions) | ____ | 140,009 |
| Distinct versions | ____ | 47,956 |
| Vendored files | ____ | 3,081 |
| What it is | ____ | documents: strong PII candidate |
| Track | text / binary / exclude | binary |
| Owner, start date | ____ | ____ |

### Step 1: Look at samples

**In plain words:** open a handful of real files of this type to see what is inside before deciding whether it is worth sending.

```bash
cd /data/workarea/scripts
python3 sample_ext_files.py --by-repo /data/workarea/all_ext_counts/by_repo.csv \
    --repos-root /data/workarea/archive --out /data/workarea/ext_samples \
    --extensions ____ --per-ext 5
```

Look for: names, emails, IDs, customer or employee data; secrets (keys,
passwords, connection strings, certificates); whether the files are real
content, empty markers or public data; whether the file is really what its
extension says (first bytes: `%PDF`, pickle opcodes, zlib/lz4).

Wider look (optional): `extension_versions.py --extensions ____` (pass 1),
then `extension_rnd.py --counts <that out folder> --repos-root
/data/workarea/archive --out ____`.

**Findings:** ____

### Step 2: Decide (confirm with the lead)

**In plain words:** based on the samples, decide whether to send this type, how (as text or as whole files), and whether today's versions were already checked. Get it confirmed before running anything big.

| Question | Answer |
|---|---|
| Extract or exclude? Reason from the samples | ____ |
| Track: text or binary? Needs a text conversion? | ____ |
| Were the active copy's files of this type scanned before? (yes = skip versions identical to the active file; no = send them too) | ____ |
| Confirmed by / date | ____ |

### Step 3: Count (binary track)

**In plain words:** count every old draft and find which ones match today's checked files, so we know how much will be sent before copying anything.

Use **new** output folders so group 2's results stay untouched.

Before running: `run_ext_versions.sh` has no extension setting yet, and
`extension_versions.py` / `extract_versions.py` treat anything outside
`BINARY_EXTS` as text. Add an `EXTS` setting to the runners (passed as
`--extensions`) and a `--binary-exts` option instead of editing the group 2
lists. Test locally (`test_extension_versions.py`, `test_smoke.py`), copy,
check `md5sum`.

```bash
OUT=/data/workarea/ext_versions_____ nohup ./run_ext_versions.sh \
    > /data/workarea/ext_versions_____.out 2>&1 &
python3 ext_versions_analysis.py /data/workarea/ext_versions_____ \
    --out /data/workarea/ext_versions_____/analysis
```

### Step 4: Extract

**In plain words:** pull the drafts that still need checking out of the archive and save them as real files, each content once.

Binary:

```bash
STATE=/data/workarea/ext_versions_____ OUT=/data/workarea/binary_versions_____ \
    nohup ./run_extract_versions.sh > /data/workarea/extract_____.out 2>&1 &
```

Text:

```bash
EXTS="____" OUT=/data/workarea/text_____ nohup ./run_text_versions.sh \
    > /data/workarea/text_____.out 2>&1 &
```

Fill in the breakdown. The rows must add up.

| Binary | Value | Example: group 2 |
|---|---|---|
| Versions (changes) | ____ | 918,618 |
| − already scanned (identical to active) | ____ | 350,760 |
| − LFS stubs | ____ | 20,552 |
| − duplicates | ____ | 351,823 |
| − empty | ____ | 11 |
| **= files sent (size)** | ____ | **195,472 (~72 GB)** |

| Text | Value | Example: group 2 text |
|---|---|---|
| Files extracted | ____ | 41,754 |
| − no new lines (all in the active file) | ____ | 7,523 |
| − unreadable / empty | ____ | 929 |
| **= files sent** | ____ | **33,302** |

### Step 5: Copy to the share and verify

**In plain words:** put the files where the scanning team can reach them, then prove that every file arrived complete and none went missing.

Adapt `copy_group2.sh` (`BIN`, `TEXT`, `COUNTS`, `DST` are settings), e.g.
`DST=/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/____`.

```bash
sudo bash -c 'cd /data/workarea/scripts && BIN=____ TEXT=____ COUNTS=____ DST=____ \
    nohup ./copy_group2.sh > /data/workarea/copy_____.out 2>&1 &'
```

- Binary: every file is hashed on the share against its name. Expect
  `bad 0`. Repair with `CHECKSUM=1`; checks only with `ONLY=verify`.
- Text: compare file counts source vs share. The share is case-insensitive,
  so names that differ only by case, or with characters it does not allow,
  can go missing without an rsync error (group 2: 98 files). Copy those
  under adjusted names and add a `renamed_files.csv`.

| Check | Value |
|---|---|
| Files on the source | ____ |
| Files on the share | ____ |
| Bad hashes | ____ |
| Missing / renamed | ____ |

### Step 6: Report and mail

**In plain words:** tell the receiving team what they got, how many files, what was left out and why, in numbers that add up.

- Table: files sent per extension, with versions (changes).
- Recheck CSV per extension, **with a legend** for every column.
- Note anything sensitive by type (e.g. `.pfx` can hold private keys; list
  them like `reports/certificates.csv`).
- Save notes and reports under `notes/` and `reports/` (committed). CSVs are
  gitignored.
- Short mail, plain language, ready to paste:

```
Subject: History extraction for ____: files on the share

Hi ____,

The history of ____ is now on the share in ____.
- Files sent: ____ (____ GB), from ____ versions in ____ repos.
- Not sent: ____ already scanned (same as today's file), ____ duplicates
  (stored once, every location is in manifest.csv), ____ LFS pointers,
  ____ empty.
- All files were checked on the share by hash: ____ bad.

manifest.csv maps every file back to its repo, path and commit.

Thanks,
____
```

### Checklist

- [ ] Extension card filled
- [ ] Samples reviewed, findings written
- [ ] Decision and "scanned before?" confirmed
- [ ] Script changes tested locally, copied, `md5sum` matches
- [ ] Counts run (pass 1 + 2) into a new folder, exit code 0
- [ ] Extraction run, breakdown adds up
- [ ] Failed repos checked in the log and retried
- [ ] Copied to the share, hashes verified, missing files resolved
- [ ] Report, recheck CSV with legend, mail sent
