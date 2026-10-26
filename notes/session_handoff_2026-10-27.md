# Session handoff (27 Oct 2026): group 4.7+4.9, the remaining types

Covers 23-27 Oct. Previous handoff: `notes/session_handoff_2026-10-19.md`
(4.7 plan, how we work). Server: `ganeshk@USSPNXT1778009520LVM20`, scripts
in `/data/workarea/scripts`, Python 3.10. Share p1 =
`/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1` (writes need `sudo bash -c`).

**Next up: copy what is not on the share yet** (section "Copy"), then the
team mail, then the 7 sample-first types.

## How we work (additions)

- Unchanged: commit only on "commit"; md5s for every script copied; nohup
  runners; real data stays on the server; tracking files never in a folder
  the scanner reads; CRLF CSVs; time-test before big runs.
- **Active copy is a blobfuse2 mount** (`~/blobcontainer/EYGCO_13082026_777Gb/AllRepos`,
  config `~/blobconfig.yml`, file cache `/data/blobfuse2tmp`). It is
  read-write: never write there (a vim swap file nearly went up). Suggested
  `read-only: true` in the config, not confirmed.
- **26 Oct: every read failed with PermissionError** = cache subfolders owned
  by root. Fix: `fusermount -u ~/blobcontainer`,
  `sudo chown -R ganeshk:ganeshk /data/blobfuse2tmp`, mount again.
- Workbooks (gitignored): `reports/group4_7_9_remaining_43_plan_2026-10-26.xlsx`
  (the 43 types and their route), `reports/group4_7_9_43_types_status_2026-10-26.xlsx`
  (counts run so far + inside-4.2 stats: 22 of the 43 types, 701 files).
  Builders in the session scratchpad (`build_plan43.py`, `build_status43.py`).

## Decisions (user)

- 4.7 and 4.9 are one group (376 types). Groups 1 and 2 and the inner 4.2
  copies of them were delivered 23 Oct (see the 19 Oct handoff / memory).
- **Credentials and keys out** (26 Oct): the 16 Hold types (snk p12 rsa gpg
  keystore encrypted mobileprovision pgp pvk der keytab kdbx keybackup pkcs12
  pkpass android) -> 59 remaining became 43. Asked: the 2 CyberArk
  `cred-dont use` files in 4.2 count as credentials too - **not confirmed**.
- **Tabular formats as group 2** (whole, history + today, each content once):
  parquet orc arrow feather avro sav. The other 11 group-3 types (rds rda
  rdata npy npz ckpt pickle joblib pk bson nippy) go to text with
  `data_to_text.py`.
- **pickle text not redone** with the raw-scan filter: `--strict-raw` stays
  opt-in, off by default (40 M lines of raw-scan strings are in the output).
- **Containers: data and images only, nothing else** (pbix pbit pbi twbx
  dbc mwb numbers kmz qgz) -> `bi_to_data.py`.
- **Duplicate paths, repo scope** (27 Oct): `dedupe_text_paths.py --scope repo
  --apply`. Scope `all` would free 225 GB more but empties whole repos
  (e.g. ~20 `ce5-*` repos share the same 220 pbix); delivery stays per repo.

## Group 3 types (DONE on the server, NOT copied)

`run_group4_7_9_group3.sh` (G=/data/workarea/group4_7_9/group3,
TAB=/data/workarea/group4_7_9/tabular).

| output | where | size |
|---|---|---|
| text, repo level | `$G/text` | 4,929 .txt, 45.7 M lines, 520 MB |
| text inside 4.2 | `group4_2/group4_7_9_group3_text` | 211 .txt, 93 MB |
| tabular history | `$TAB/tabular_binary/files` | 1.2 GB |
| tabular today | `$TAB/tabular_active/files` | 315 MB (554 files) |
| tabular inside 4.2 / 4.2_R | `<run>/group4_7_9_tabular_binary` | 550 MB / 3.9 MB |

Group 3 text not deduped (small); `dedupe_text_paths.py --text-dir .` works
on it if wanted.

## Containers (DONE on the server, NOT copied)

`run_group4_7_9_bi.sh` (G=/data/workarea/group4_7_9/bi). Counts: pbix
15,540 paths / 27,046 versions; extract 25,508 versions, 28.89 GB.

- data: 178/178 repos ok, 15,170 .txt, 1.09 B lines, 2,750 images (177 MB).
  By type: pbix 15,034 .txt; dbc 78 (only notebooks with outputs, code left
  out); pbit 0 (templates: 70 images only); mwb 52; pbi 2; kmz 2; twbx 1;
  numbers 1; qgz 0.
- pbixray date overflow lost whole tables -> `patch_pbixray()` (commit
  3fb06d5); the 11 repos read before it redone (`--redo --name table_fix`).
- **Dedup applied (repo scope)**: 15,170 -> 13,537 .txt, 1,633 duplicate
  paths (339.7 GB) moved out; data_text now **520 GiB**. The moved files
  are in `$G/data/_removed_duplicates` (317 GiB) until deleted. Inside 4.2:
  142 -> 96 .txt, 1.8 GB. 4.2_R has none of these types.
- Biggest repos left: sdt-ey-isqc1-web ~229 GB, sdt-documentation ~115 GB,
  sdt-scripts-reporting ~95 GB; single .txt up to 18 GB. **Ask the scanner
  team the largest .txt it takes and whether ~520 GiB is OK** - a splitter
  (fixed size parts, table header repeated) is offered, not written.

Notes in `summary.csv` (what was not read, and why):

- `table_error:1` - `ey-fso-tax/leapfrog-datafabric`, `TPG Data External
  Investment_PROD.pbix`: the pbix is missing one internal file
  (`...Date Last Refreshed...idfxmeta`); only its `refresh` table (last
  refresh date) is lost. Damaged file - leave it.
- `no_datamodel:262` - pbix on a live connection, no data inside.
- `not_zip:547` (versions):
  - **dbc ~460: plain source notebooks** (Python / SQL) saved as .dbc, not
    Databricks archives; 128 B - 2 KB, mostly
    `building-blocks-ansible-playbooks/fabric-il-databricks-storage-control`
    (Mount_ADLS, Create_ADLSContainer, Auto_Loader_Storage ...). Code, but
    may hold storage names / keys. Proposed: send as text like group 1 -
    **user to decide**.
  - pbix placeholders: 1-byte files (`employees_report.pbix`,
    `GHGUnified.pbix`), 36-byte "blah" stubs (`updatedReport.pbix` x2), two
    small scripts named .pbix (`demo_adb_endpoint_sample`,
    `error_department_employee_script`). No data.
  - pbix damaged: 5 versions in `tp-periscope-server` (`ClientName PTP
    Dashboard_Analysis.pbix`, blobs bb2edf71f6 0e31ac5a5e c3855b0af5
    6c36f3b208 52dc7b370f): central directory broken, `unzip` fails too
    ("transferred in text mode" = probably committed as text, line ends
    changed). The other 32 versions of that path were read. A CRLF->LF
    recovery test was given, result not seen.

## Copy (scripts written 27 Oct, not run)

Repo level - `copy_group4_7_9_rest.sh` (parts group3_text, tabular_binary,
bi_data; tracking to `p1/group4_7_9_tracking/<part>`; never sends `_state`,
`_logs`, `_tmp`, `_removed_duplicates`; hashes tabular files against the
blob name and bi files against the sha256 name):

    sudo bash -c 'cd /data/workarea/scripts && \
        nohup ./copy_group4_7_9_rest.sh > /data/workarea/group4_7_9/copy_rest.out 2>&1 &'

-> `p1/group4_7_9/group3_text`, `p1/group4_7_9/tabular_binary/files`,
`p1/group4_7_9/bi_data/{data_text,files}`. bi_data only after the scanner
team says yes on size (`PARTS="group3_text tabular_binary"` first).

Inside the archives - `copy_group4_2.sh` (now also skips `_tmp` and
`_removed_duplicates`):

    sudo bash -c 'cd /data/workarea/scripts && P1=/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1 && \
        STEP_DIRS="group4_7_9_group3_text group4_7_9_tabular_binary group4_7_9_bi_data" EXTRAS=0 \
        DST_ROOT=$P1/group4_7_9 TRACK_ROOT=$P1/group4_7_9_tracking \
        CHECKS=/data/workarea/group4_7_9/inner_rest_checks \
        CHECKS_DST=$P1/group4_7_9_tracking/group4_2_tracking/copy_checks_rest \
        nohup ./copy_group4_2.sh > /data/workarea/group4_7_9/copy_inner_rest.out 2>&1 &'

## Open items

1. Copy (above); scanner limits for the container text.
2. dbc plain-text notebooks as text: yes / no.
3. Confirm: CyberArk `cred-dont use` dropped; the 9 Exclude types and jar1;
   the `config,txt` / `un~` / `cred-dont use` resolver fix (archive folder
   names with stripped characters - proposed, not done).
4. Team mail update: groups 3, tabular, containers, and a **correction**:
   the 23 Oct mail lists `creds` / `cred-dontuse` as sent.
5. Sample first, 7 types: deploy, tmp, out, dump, db_backup, pbl-old,
   simflows.
6. Fill the container results into the 43-type status workbook.
7. Delete `$G/data/_removed_duplicates` (and the 4.2 one) after checking.
8. Uncommitted: `bi_to_data.py` by_type total counts only repos with the
   types (md5 cdb39aefe2fca2caa784e1dde11b990c); `copy_group4_7_9_rest.sh`;
   `copy_group4_2.sh` excludes; this note. main was 1 ahead of origin
   (cc47926) - user pushes.
