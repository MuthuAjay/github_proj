# Group 4.2: paths to scan

The scanning team should scan these two folders on the share. Each one contains the step folders listed below.

```
/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_2/       (files from the 4.2 archives)
/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_2_R/     (files from the 4.2_R packages)
```

| Folder under `p1/` | What it is | Files |
|---|---|---|
| `group4_2/group1_text` | Group 1 text, one .txt per path | 720,901 |
| `group4_2/group4_5_R_text` | 4.5_R text | 48,496 |
| `group4_2/group4_5_text` | 4.5 text | 96,166 |
| `group4_2/group2_text` | Group 2 text | 9,732 |
| `group4_2/group2_binary` | Group 2 binary (Office, images, MSG, PFX…) | 18,522 |
| `group4_2/group3_pkl_text` | PKL strings as .txt | 62 |
| `group4_2/group3_pdf_merged` | PDFs | 2,832 |
| `group4_2_R/group1_text` | Group 1 text | 112,375 |
| `group4_2_R/group4_5_R_text` | 4.5_R text | 10,489 |
| `group4_2_R/group4_5_text` | 4.5 text | 10,972 |
| `group4_2_R/group2_binary` | Group 2 binary | 3,525 |
| `group4_2_R/group3_pdf_merged` | PDFs | 7 |
| **Total** | | **1,034,079** |

**Do not scan these folders:**

```
p1/group4_2_tracking/
p1/group4_2_R_tracking/
```

They hold the manifests, done.json files, stats and copy checks. If these are scanned, the tracking records get counted as data, which is what went wrong with group 2.

**Under each step folder the paths are `<org>/<repo>/<archive path>/<file inside>`.** For example, `group1_text/<org>/<repo>/data/export.zip/inner/config.json.txt`. A scan hit therefore points straight to the repo, the archive and the file inside it.

**Notes:**

- `group4_2_R/group2_text` and `group4_2_R/group3_pkl_text` had no files (0 to send), so those folders won't exist on the share.
- Wait for the copy to finish and its count check to pass before handing the paths over. It was still copying the tracking files when we last looked.
- About 300 files are expected to be missing because their names differ only by case. Once the counts are in, we can resend them under changed names.
