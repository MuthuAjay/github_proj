**Subject: PII extraction – Group 4.7+4.9: table / serialized data and Power BI–type files copied to the share**

Hi all,

This update covers the 4.7+4.9 types that had no route on 23 Oct. **26 types are now on the share**, at repo level and inside the 4.2 archives:

1. **Serialized data → text** (11 types: rds, pickle, ckpt, joblib, nippy, rda, npz, rdata, bson, pk, npy).
2. **Table data files → whole files**, like Group 2 (6 types: parquet, sav, orc, arrow, feather, avro).
3. **Power BI and other container files → their data as text, and their images** (9 types: pbix, pbit, pbi, dbc, mwb, twbx, numbers, kmz, qgz).

The credential and key types (16 types: snk, p12, rsa, gpg, keystore, pvk, keytab…) were **taken out of scope** and are not sent.

| | types | repo level | inside 4.2 | total sent |
|---|---:|---:|---:|---:|
| Serialized data (`.txt`) | 11 | 4,929 | 211 | 5,140 |
| Table data (whole files) | 6 | 3,235 | 75 | 3,310 |
| Containers (`.txt` + images) | 9 | 13,537 + 2,750 | 96 + 28 | 16,411 |
| **Total** | **26** | **24,451** | **410** | **24,861** |

None of these types is inside the 4.2_R packages.

## 1. Serialized data → text (11 types)

*What was taken*

- One `.txt` per file path, with **every text value found in any version in the git history, plus today's version**, each value once.
- The files are decoded, never loaded or run. Values are read from R data (rds/rda/rdata), pickle and joblib, NumPy text arrays, BSON documents, Nippy data and model checkpoints. Numeric arrays and model weights are skipped.
- When a file can't be decoded, the readable strings in it are taken.

| | repo level | inside 4.2 |
|---|---:|---:|
| Paths read | 5,002 | 216 |
| **Sent** | **4,929 `.txt`, 45.7 M lines, 0.52 GB** | **211 `.txt`, 6.5 M lines, 0.09 GB** |
| Not sent | 73 paths with no text values (numeric arrays, empty pickles) | 5 |

- **pickle** accounts for 40.3 M of the lines. Many of them are readable strings taken from pickles that could not be decoded, so expect noise there.
- **Worth a look:** `pk` (`sanction_update_status.pk`), `bson` (initial data for tests), `nippy` (including a "real_example"), `rdata` (R workspaces).

## 2. Table data → whole files (6 types)

*What was taken:* the file itself, **each distinct content once**, from every version in the history plus today's file. This is the same as Group 2.

| | repo level | inside 4.2 |
|---|---:|---:|
| History versions | 5,819 (1,760 of them Git LFS pointers with no content) | 116 |
| **Sent** | **3,235 files, 1.56 GB** (2,681 history + 554 today's) | **75 files, 0.55 GB** |

- **parquet** is most of it: 3,136 files at repo level and 58 inside 4.2.
- The files are named by their git blob id, and each one was checked on the share against its name: 0 bad.

## 3. Power BI and other container files → data + images (9 types)

*What was taken:* **only the data, as text, and the images**, nothing else. No layout, measures, queries, code, connection strings or settings, and the `.pbix` (etc.) files themselves are not sent.

- **pbix / pbi:** every table of the imported data model, as `table: <name>`, then the column names, then one line per row with the values joined by ` | `.
- **twbx:** the tables of the packaged `.hyper` extract.
- **dbc** (Databricks archives): notebook outputs only (result tables, printed text), no code.
- **mwb:** the sample rows stored in the model.
- **numbers:** cell text. **kmz:** placemark names, descriptions and fields.
- **pbit, qgz:** images only, since templates and projects hold no data.
- Every version in the history plus today's file goes into one `.txt` per path, each line once.

| | repo level | inside 4.2 |
|---|---:|---:|
| Paths | 16,542 | 162 |
| `.txt` written | 15,170 (1.09 B lines, 897 GB) | 142 |
| Removed: same contents as another path in the same repo | 1,633 (340 GB) | 46 |
| **Sent** | **13,537 `.txt` (557 GB) + 2,750 images** | **96 `.txt` + 28 images** |

- **Duplicates were removed within each repo.** Many reports were committed several times in one repo, as release, rollback and deployment copies of the same file. Their `.txt` files were identical, so only one copy per repo is sent. `same_as.csv` in the tracking folder names the copy that was kept for each one removed.
  - **We did not dedupe across repos.** That would have removed a further 225 GB, but whole repos would have been left with nothing sent (for example, about 20 `ce5-*` repos hold the same 220 reports).
- **Size:** the text is large. Three repos hold most of it: `sdt-ey-isqc1-web` about 229 GB, `sdt-documentation` about 115 GB and `sdt-scripts-reporting` about 95 GB. Single files reach 18 GB. The scanner team confirmed this is OK.
- **Not read** (details in the workbook):
  - 262 pbix versions use a live connection, so they hold no data;
  - 6 placeholder files are named `.pbix` but aren't reports;
  - 5 damaged pbix versions;
  - one table in one damaged pbix;
  - 27 Git LFS pointers.
- **Open: 460 `.dbc` files** turned out to be plain notebook source code (Python / SQL), not Databricks archives. Most are in `building-blocks-ansible-playbooks/fabric-il-databricks-storage-control` (Mount_ADLS, Create_ADLSContainer…). They hold no data, but storage-mount notebooks often contain account names or keys. They are small (under 2 KB each). **Should we send them as text?**

## 4. Mixed types → text where readable, whole file otherwise (7 types)

*Types:* deploy, tmp, out, dump, db_backup, pbl-old, simflows.

*Why these needed their own process:* for these types the file name doesn't say what is inside. The same type holds both text files and binary files:

- `.deploy` is a ClickOnce publishing suffix added to any file. Of the 5,129 `.deploy` paths, 4,800+ are XML schemas, stylesheets, XML and config (text), and about 190 are programs (`.dll`, `.exe`).
- `.dump`, `.out` and `.tmp` can be text logs, SQL / LDAP dumps or raw binary data, depending on the tool that wrote them.

A single route per type would have handled part of each type badly. So **each file path was routed by its content**, not by its type.

*How each path was routed:* every version of the path in the git history, plus today's file, was checked.

1. **All versions are readable text → text route** (as in Group 1). One `.txt` per path holds every line from every version plus today's, each line once.
   - **Why text:** the scanner reads the text directly. The same lines repeat across versions, so sending them once keeps the volume small. The `.txt` mirrors the repo path, so a finding points straight to the file.
2. **Any version is binary → whole-file route** (as in Group 2). The file itself is sent, each distinct content once, named by its git blob id.
   - **Why whole:** a binary file can't be cut into meaningful lines; taking the readable strings out would lose structure and mix in noise. Sent whole, the scanner can open it with its own readers. A path goes whole if *any* version is binary, so all of its versions travel the same way.
3. **Left out:**
   - **`.dll.deploy` / `.exe.deploy` (153 paths):** these are compiled application programs, not data, so per our decision they are not sent.
   - **21 paths with no text in any version:** there is nothing to scan.

| | repo level | inside 4.2 |
|---|---:|---:|
| Paths read as text → sent as `.txt` | 4,923 | 12 (dump 9, deploy 2, tmp 1) |
| Binary paths → sent whole | 175 paths = 160 files, 4.1 GB | 6 files |
| Left out: dll / exe programs | 153 paths | (included in the 17 not sent) |
| Left out: no text | 21 | 2 |

- Binary paths by type: deploy 90, dump 41, tmp 31, out 11, pbl-old 2. `db_backup` and `simflows` went entirely as text.
- None of these types is inside the 4.2_R packages.

## Correction to the 23 Oct mail

The 23 Oct mail listed files named `creds` and `cred-dontuse` among the files sent from inside the 4.2 archives. **They were not sent.** Their type names contain characters that our type lookup dropped, so they were missed. Since credentials are now out of scope, we propose to leave them out along with the other credential and key files.

## Where

| | to scan | tracking (**not for scanning**) |
|---|---|---|
| Serialized data | `diff_analysis/p1/group4_7_9/group3_text/` | `diff_analysis/p1/group4_7_9_tracking/group3_text/` |
| Table data | `diff_analysis/p1/group4_7_9/tabular_binary/files/` | `diff_analysis/p1/group4_7_9_tracking/tabular_{binary,active,counts}/` |
| Containers | `diff_analysis/p1/group4_7_9/bi_data/data_text/` and `…/bi_data/files/` | `diff_analysis/p1/group4_7_9_tracking/bi_data/` |
| Mixed types | `diff_analysis/p1/group4_7_9/mixed_text/` and `…/mixed_binary/files/` | `diff_analysis/p1/group4_7_9_tracking/mixed_{text,binary}/` |
| Inside 4.2 archives | `diff_analysis/p1/group4_7_9/group4_2/group4_7_9_{group3_text,tabular_binary,bi_data,mixed_text,mixed_binary}/` | `diff_analysis/p1/group4_7_9_tracking/group4_2_tracking/` |

The text folders mirror `<org>/<repo>/<path>.txt`. Whole files and images are named by their content hash, and the manifests in the tracking folders map each one back to its repo, path and versions.

**Checks:** every share folder has the same file count as the source. No tracking file is among the data. All whole files and images were hash-checked: 0 bad. 19 files had names that differ from another only by upper/lower case; they were sent as `<name>__case2.txt`, listed in `case_renamed.csv`.

Numbers per type: `group4_7_9_delivery_stats_2026-10-27.xlsx` (attached).

## Still open in 4.7+4.9 (10 types)

- **Proposed exclude** (9): arff, xcuserstate, test-db, train, sublime-workspace, ftz, rsuser, 2ph, mmdb. Also **list paths only** for `jar1`. Please confirm.
- **The plain-text `.dbc` notebooks** (above): send as text or not?

Regards,
