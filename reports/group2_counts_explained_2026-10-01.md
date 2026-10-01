# Group 2: file counts explained (1 Oct 2026)

The numbers are correct. Here is how they fit together.

## 1. Why the folder shows 283,210 files

The folder holds the data to scan **plus our tracking files**:

- **228,774 files to scan** (228,676 on the share now; the last 98 are being
  copied)
- **54,534 tracking files**: per-repository manifests and status files in the
  `_state` folders, the overall `manifest.csv`, and the reports. They contain
  no repository content and don't need scanning.

To count only the data: everything under `binary/files/`, plus everything
under `text/` except `text/_state/`.

| | Files |
|---|---:|
| Data to scan in `binary/files/` | 195,472 |
| Data to scan in `text/` (98 still being copied) | 33,204 |
| `_state` folders: a manifest and a status file per repository, twice | 54,524 |
| `binary/manifest.csv`, summaries and reports | 10 |
| **Total in the folder** | **283,210** |

## 2. Why 228,774 is much lower than the 847,407 files in history

We send only content that hasn't been scanned yet, and each identical file
only once.

| Step | Count |
|---|---:|
| Files in Git history (27 types) | 847,407 |
| Removed: versions identical to the current file, already scanned | 350,760 |
| Removed: identical copies of the same file across folders, repos and commits (sent once) | 351,823 |
| Removed: text files with nothing new (every line still in the current file) | 7,523 |
| Not available: Git LFS pointers (file not in the archive), empty or non-text files | 21,492 |
| **Delivered for scanning** | **228,774** |

The steps above mix versions and files, so they don't subtract directly from
847,407. How it adds up exactly (Office, email and image files are counted in
versions, because every old version is a separate file; text files are
counted in files, because the old lines of a file go into one file):

- Office, email and image files: 918,618 versions − 350,760 already scanned −
  20,552 LFS pointers − 351,823 duplicates − 11 empty = **195,472**
- Text files: 41,754 files − 7,523 with nothing new − 929 not text =
  **33,302**
- **Total: 228,774**

The largest reduction is **duplicates**, mostly images. For example, **png**
goes from 405,794 versions to send to **99,882 unique files**: the same logos
and icons appear in hundreds of repositories (one logo is in 134). Every
location is still recorded in `manifest.csv`, so a finding in one file applies
to all its occurrences.

## 3. Files delivered per type

| Type | Files | Type | Files | Type | Files |
|---|---:|---|---:|---|---:|
| png | 99,882 | feature | 11,499 | msg | 663 |
| jpg | 36,099 | rst | 8,440 | xls | 925 |
| xlsx | 31,722 | bicep | 6,226 | jpeg | 803 |
| xlsm | 8,001 | suo | 4,216 | woff | 803 |
| docx | 3,801 | pptx | 3,716 | pptm | 177 |
| xlsb | 3,085 | groovy | 2,650 | pfx | 138 |
| lock | 1,874 | tfvars | 1,375 | doc | 109 |
| p7s | 1,312 | erb | 1,007 | xcconfig | 229 |
| eml | 18 | mpg | 2 | azcli | 2 |
| | | | | **Total** | **228,774** |

The full step-by-step breakdown per type is in
`group2_recheck_by_extension.csv`. Every row adds up, so any type can be
rechecked:

- Office, email and image types: `versions − already_processed − lfs_stubs =
  versions_to_send`, then `versions_to_send − duplicates_removed − empty_files
  = files_for_pii`. Example, png: 670,145 − 256,187 − 8,164 = 405,794;
  405,794 − 305,911 − 1 = 99,882.
- Text types: `files_in_history − text_no_new_lines − text_unreadable_or_empty = files_for_pii`.
  Example, feature: 12,833 − 1,317 − 17 = 11,499.
