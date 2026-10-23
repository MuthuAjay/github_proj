**Subject: PII extraction – Group 4.7+4.9: text types (Group 1) and the files inside the 4.2 / 4.2_R archives copied to the share**

Hi all,

This update covers two more parts of the merged 4.7+4.9 group:

1. **Group 1**, the 160 text types, at repo level.
2. **The 4.7+4.9 types found inside the 4.2 archives and 4.2_R packages**, for the types that already have a route.

The whole-file types (Group 2, 117 types, 5,078 files, 6.08 GB) were delivered earlier.

## 1. Group 1 – text types (160), repo level

*What was taken*

- One `.txt` per file path, holding **every line that path ever had in any version in the git history, plus today's version**, each line once.
- Today's files of these types had not been scanned, so their lines are included (no delta).
- Before the run we sampled the 20 largest types, in two rounds; all of them proved to be text files.

| | |
|---|---|
| Types | 160 |
| Repos | 560 processed; 495 have output |
| Paths in history | 14,084 |
| **Sent** | **13,905 `.txt`, about 7.1 M lines, 0.76 GB**: share = source |
| Not sent: binary | 133 paths (mostly sdcs, backup, hash-named cache files). Listed in the tracking folder; content not sent |
| Not sent: only blank lines | 46 paths |
| Left out: vendored folders (node_modules, packages, bin, obj…) | 725 files, listed |

- 71 files had names that differed from another only by upper/lower case, which the share can't keep apart. They were sent under `<name>__case2.txt`, and the mapping is in `case_renamed.csv` in the tracking folder.

## 2. 4.7+4.9 types inside the 4.2 / 4.2_R archives

The types inside the archives are the same as at repo level: the 4.7+4.9 types with a route, plus 34 types that were reclassified into 4.9 on 22 Oct and exist only inside archives.

- **Text types** go as one `.txt` per path, every line of every version once.
- **The others** go whole, each distinct content once.
- **No delta.** Vendored, macOS-metadata and DocFX-cache paths are left out and listed.

| | text | whole files |
|---|---:|---:|
| Files of these types inside the archives (every version) | 1,119 | 5,100 |
| **Sent** | **1,022 `.txt`, 60,699 lines, 5.7 MB** | **1,012 files, 0.61 GB** |
| Not sent | 10 (binary / blank only) | 2,639 empty, 1,255 identical to another path's file |
| Paths left out (vendored / macOS / DocFX) | 45 | 72 |

- **Total on the share:** 2,034 files, no name problems, and the counts per folder match the source.
- **Worth a look:**
  - files named `creds`, `cred-dontuse` and `secret`;
  - a GPO backup (`ins`) and a Windows firewall registry export (`wfw`);
  - XPS engagement-migration reports (`oxps`), likely with client names;
  - ADF / SSAS definitions (JSON) that may hold connection strings.
- **Of the 34 new types, 6 weren't sent:** an executable, ZooKeeper test snapshots, and empty files.

## Where

| | to scan | tracking (**not for scanning**) |
|---|---|---|
| Group 1 | `diff_analysis/p1/group4_7_9/group1_text/` | `diff_analysis/p1/group4_7_9_tracking/group1_text/` |
| Inside 4.2 archives | `diff_analysis/p1/group4_7_9/group4_2/group4_7_9_text/` and `…/group4_7_9_binary/` | `diff_analysis/p1/group4_7_9_tracking/group4_2_tracking/` |
| Inside 4.2_R packages | `diff_analysis/p1/group4_7_9/group4_2_R/group4_7_9_text/` and `…/group4_7_9_binary/` | `diff_analysis/p1/group4_7_9_tracking/group4_2_R_tracking/` |

The scan folders mirror `<org>/<repo>/<path>`, with paths inside an archive under the archive's own path. The manifests in the tracking folders map every file back to its repo, path and versions.

## Next: 59 types that have no route yet

These are at repo level, and 29 of them are also inside the archives.

- **Credential and key files** (snk, p12, rsa, gpg, keystore, pvk, keytab…, 1,360 files): send whole, list paths only, or skip? Decision needed.
- **Power BI** (pbix 15,540 files, pbit, dbc): what to do with the embedded data. Decision needed.
- **Table and serialized data** (parquet, rds, npy, orc, avro…): needs a reader to turn the data into text.
- **pickle / joblib**: can go through the existing pickle reader.
- **deploy, dump, tmp, out**: to be sampled first.

Regards,
