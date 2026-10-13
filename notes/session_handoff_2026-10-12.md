# Session handoff (12 Oct 2026): Git history PII extraction

Covers 8-12 Oct. Previous handoff: `notes/session_handoff_2026-10-07.md`
(how we work, groups 1-3, 4.1, 4.2 category A, server layout); read it for
anything not repeated here. Server: `ganeshk@USSPNXT1778009520LVM20`,
scripts in `/data/workarea/scripts`, Python 3.10.

**Next up:** copy the new archive_unpack.py and redo the billing repo
(below); then look at the Group 4.2_R results (stats) and decide routing,
copy + mail; ovf when the user asks.

## How we work (additions)

- **Time the exact command on a big repo before giving run commands** when
  a setting grows (extension count, repo count, new flag). 4.5_R lost >24 h
  because 890 git `:(glob,icase)` patterns made `git log` ~200x slower; the
  user called this out. Use `--match-in-python` (below) for any multi-type run.
- `--match-in-python` exists in `file_added_lines.py` (text track) and now
  in `extension_versions.py` (counts step); `run_group4_2.sh` has it on by
  default, `run_text_versions.sh` / `run_ext_versions.sh` via `MATCH_IN_PYTHON=1`.
  Same output, verified (files.csv identical; 45.6 s -> 1.6 s on the
  benchmark repo).
- Under `nohup`, a Python run with `--quiet` writes almost nothing to its
  `.out`; progress is in `<out>/_logs/<name>.log`.
- Folders made by a `sudo` script are root-owned: write checks to `~/...`.
- Archive repos with only `.git/objects` (no HEAD/refs) cannot be read with
  plain git: use `file_history_for_list.RecoveredRepo` (temp repo in /tmp/fhl_*).
- md5 lists: the user now checks md5s by hand; just give the md5s.

## Group 4.3 + 4.6: classification (delivered to the user)

`group4_3_4_6_classification.xlsx/.csv` + `_legend.md` (xlsx/csv
gitignored, legend untracked; scratch build scripts not kept): every sheet
type of 4.3 (2,891 + 51 sheet-issue values) and 4.6 (148), with category
(media split into Images / Audio-video / Fonts / 3D-design), pii_risk,
target_group, `versions` (= changes; distinct versions dropped at the
user's request), sheet_rows. The user then made their own lists from it:
`group4_5_R.xlsx` (890 text types) and `group4_2_R.xlsx` (21 package types).

## Group 4.5_R: text (DONE, copied, mail drafted)

890 readable text types from 4.3/4.6 (`group4_5_R_extensions.txt`).
History minus today's lines (delta), vendored skipped and listed.
`OUT=/data/workarea/group4_5_R/extract`, delta `…/extract_delta`.
- 13,631 repos ok (7 timed out at first: rerun 12 Oct with
  `--repo … --retry-failed --skip-vendored --match-in-python
  --repo-timeout 43200`, done in 2-5 min each; then `file_delta.py --repo …
  --redo --name delta_late`). List: `/data/workarea/group4_5_R/failed_repos.txt`.
- Report: 890 types, 356,026 files in history (= planned non-vendored scope),
  **290,608 sent, 19,964,659 lines** (32.55 M in history, 12.59 M removed =
  38.7%), 25 GB, 2,862 repo folders; vendored skipped 270,793
  (`extract/skipped_vendored_all.csv`).
- Copied with `copy_group4_5_R.sh` (16 parallel rsync) to
  `/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_5_R` (+ `_tracking`).
  **290,551 landed; 57 lost to case-only name clashes** (100 rows in
  `extract_copy_checks/name_problems.csv`). The user was checking that the
  missing list = name problems (`~/g45r_check/missing.txt`); not confirmed back.
- Update mail drafted in the conversation (numbers above).
- Note: `ey-cdap` and `ey-cdap-ui` look like copies (same files/lines).

## Group 4.2_R: packages (full run DONE 12 Oct, results not yet reviewed)

From the user's `group4_2_R.xlsx` (21 types). Run for 19:
`001 002 003 br dxp ez hpi ispac msapp pbiviz ppkg skill sppkg ucs vhd vsix wsp xap zap`
with `G=/data/workarea/group4_2_R ./run_group4_2.sh` (batches from
`/data/workarea/all_ext_counts/by_repo.csv`, files_in_history > 0 = 242 repos;
test batch M4T = first 20).
- **Result:** 242 repos, files inside 701,158, **distinct written 406,036
  (25.48 GB)**, not stored 88, problem archives 637 (most are probably
  `skipped_vendored`; the test's 63 were 62 vendored + 1 not_an_archive).
  Stats: `/data/workarea/group4_2_R/unpacked/stats/` (by_type.csv with
  route, by_archive_type.csv, problems.csv);
  `python3 archive_unpack_report.py /data/workarea/group4_2_R/unpacked` for report.md.
- Test repo `ey-org/assurance-eyv-eydna-appstore-billing` failed in M4T
  ("brotli: decoder failed", fixed in 5ae9401); **redone 12 Oct (M4T_fix):
  ok**, all 242 repos ok. Its 3 versions of `app/env/.env.prod.br` are
  `error` rows (not stored).
- Status change (commit below): an archive whose files inside ALL failed is
  now `damaged: no file inside could be read` (or `encrypted`), not `ok`,
  so it lands in problems.csv. **Open:** copy archive_unpack.py (md5
  `958d5c7c556e21849969d470f19640b2`) to the server and redo the billing
  repo (`--repo … --redo --name M4T_fix2`): its 3 rows should turn damaged,
  problems 637 -> 640. Other repos keep the old status until redone (a
  full `--redo` of the 242 takes ~2 min; optional).
- The full run's counts step ran before `--match-in-python` was added to
  it (fine, same output).
- Findings: its `.env.prod.br` is NOT brotli (BR = Brazil prod env profile,
  plain text, 2 versions, likely secrets): **user chose to ignore it**
  (listed, not sent). The only `.vhd` is VHDL source in `node_modules_/ace-builds`
  (not a disk). After the run, check `not_an_archive` rows in problems.csv
  the same way.
- Split archives seen in today's copy: `*.7z.001/.002` (c-ukra-pearson GL
  audit samples), `*.zip.001/.002` (helix process-mining wikis), numbered
  wiki attachments `attachments/<page>/<id>.001…003` (ci-wiki, tms-wiki,
  100/80 MiB parts), fc-lunch-learn-wiki has only `.001` (incomplete).
- Next: look at by_type.csv routes (as for 4.2), decide how inner files go
  (group 1/2/3 methods), copy + mail.

### 4.2 + 4.2_R types mapped to ext_groups (13 Oct)
- `group4_2_and_R_by_type.csv` (both runs' by_type side by side) ->
  `group4_2_and_R_by_type.xlsx` (+ sub_group from the user's
  `ext_groups.xlsx`), `group4_2_and_R_by_type_mapped.xlsx/.csv` (by_sub_group
  summary, match, also_listed_as), `group4_2_and_R_not_in_ext_groups.xlsx`
  (3,247 types only found inside archives, 0.6% of distinct files). Built in
  the scratchpad (map42.py), not kept. Duplicate sheet rows resolved by
  priority group 1 > 2 > 3 > 4.1 > 4.2 > ... > 4.4_R > 5; Excel stored
  001/002/003 as 1/2/3 in ext_groups.xlsx.
- By group (distinct): group 1 65%, Group 4.4 16% (mostly `class` from 4.2_R
  plugins), 4.5_R 6%, 4.5 5.6%, group 2 2.4%, Group 5 (_noext) 2.4% / 13 GB.
- **`_xxx` types (577, not `_noext`) = macOS AppleDouble files** (`._name`,
  mostly under `__MACOSX/`, 210 or 0 bytes, Mac metadata, no content).
  Across all types: 4.2 11,956 occurrences / 668 written, 4.2_R 228 / 4.
  `ds_store` = Finder metadata too. Decision: not re-unpacked; **the 4.2
  copy script must leave out paths under `__MACOSX/`, names starting `._`,
  and `.DS_Store`** (listed in tracking, like vendored).

- `group4_2_and_R_not_in_ext_groups.xlsx` now has **proposed_action + reason**
  (sheets types, by_action, by_reason): Send as text 2 types (**icf =
  Citibank BAI2 bank files, HIGH**; cl2 COBOL sample), Sample first 33 (**oxps
  engagement migration reports**, emb Spotfire, creds/secret/key names, parq,
  ADF/SSAS definitions, 7 small unknowns), Exclude 1,813 (DocFX cache 1,132
  random-name types, `_` types 556, cgroup 56, Java services, plugin/build
  files), Review 1,399 (long tail, 3,610 distinct, not looked at). Also: a GPO
  backup of `anz.ey.net` Default Domain Policy sits in a 4.2 repo (security
  finding). Script: scratchpad actions42.py (not kept).
- Review resolved with a server scan of every manifest row
  (`group4_2_type_location.csv` in the repo folder: per type docfx_cache /
  macos / vendored_path / elsewhere + sample paths; script in the 13 Oct
  conversation). **Final: Send as text 12 types (60 files: icf, cl2,
  canton logs, Dockerfile/agents.md variants, csn, dfm), Sample first 40
  (517: emb, oxps, GPO cmt/ins, firewall backup wfw, Figma, Webex wrf, Neo4j
  store, secret/creds/key names, configs, ADF/SSAS), Exclude 3,195 (8,942;
  DocFX cache 2,446 types).** Columns with locations added to the xlsx.

### ovf and vsto (out of 4.2_R)
- **ovf** = not VM files: Kùzu graph-db hash-index overflow files
  (`*/kg/n-*.hindex.ovf`) in `ey-org/conversational-account-insights-collector`
  (abc_telecomm, sonic, volvo graphs), 18 files / 54 versions, history only.
  Has readable text, possible PII; 4 KB files may have data in older
  versions. **User will run it later; take every version.**
- **vsto** = UpSlide (3rd-party) ClickOnce XML manifests, 3 distinct files
  in `tas-big-files-test-repo` and `tas-databook-automation-scriptless-automation`:
  list only.
- Samples in `~/ovf_vsto_sample` on the server.

## Code changes this session (all committed)

| Commit | What |
|---|---|
| 5ef2094, bf862c4 | `file_added_lines.py --skip-vendored` + `skipped_vendored.csv` per repo; runner combines them |
| dd72094 | `file_added_lines.py --match-in-python`; `run_text_versions.sh` MATCH_IN_PYTHON |
| 5058a2e, 49875c1 | `copy_group4_5_R.sh` (16 parallel rsync, name checks, count check) |
| 5fb15fe | archive_unpack: unitypackage with Unity paths; git / Unity bundles recognised |
| 9b0288e | archive_unpack: split sets joined (001/002/003; parts = `split_part`, set = `split_set`, gaps = `incomplete_set`), `.br` (brotli module or tool), vhd/vhdx via 7z |
| 5ae9401 | bad .br = file error not repo error; vhd 511-byte footer |
| 7384fd6 | `extension_versions.py --match-in-python`; on in run_group4_2.sh |
| ad3958c | archive_unpack: archive with no readable file inside = damaged/encrypted, not ok |

Tests: `test_group4_2.py` 21 OK (7z and pdf tests skip without the tools;
brotli test needs `pip install brotli`), `test_extension_versions.py` 36 OK.

Uncommitted (as before): classification legends, `sample_noext.py`,
`classify_extensions.py`, `probe_extensions.py`, `ext_versions_analysis.md`,
the 7 Oct handoff, this note.

## Still open from earlier
- 4.2 (category A) routing of "other"/no_extension types.
- Group 5 (no extension) full listing.
- Colleague's 4.5 run (LVM16): its kit lacks `--match-in-python` (offered to update; no answer).
- Group 1 7-repo gap; group 3 "were today's PDFs scanned".
