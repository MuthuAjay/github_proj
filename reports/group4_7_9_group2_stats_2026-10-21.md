# Group 4.7+4.9: Group 2 (whole file) delivery numbers

Groups 4.7 and 4.9 are handled as one group (336 types). These are the
**117 types sent as whole files** (Group 2, `group4_7_9_group2_extensions.txt`),
at repo level. Files inside the 4.2 / 4.2_R archives are not counted here.

**How:** every version in the git history (`extract_versions.py`, global
dedupe, no size cap) plus today's files from the active copy
(`active_files.py`), because today's files of these types had never been
scanned. Each distinct content is stored once and named by its git blob id.
The numbers come from `group4_7_9_group2_stats.py`, run on the server on
21 Oct 2026.

## Where

```
data (scan):      /home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_7_9/group2_binary/files/<ext>/
tracking (don't): /home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_7_9_tracking/
                  group2_binary/  manifest.csv (one row per history version), by_extension.csv, summary.md
                  group2_active/  manifest.csv (one row per file today; stored_as = files/...), by_extension.csv, summary.md
                  group2_counts/  by_extension.csv, summary.md (pass 1 + 2)
server:           /data/workarea/group4_7_9/{group2_counts,group2_binary,group2_active,group2_copy_stats}
```

## Totals

| | |
|---|---|
| types | 117 |
| history: repos / paths / versions | 327 / 3,298 / 4,054 |
| history versions by action | written 3,496, deduped 507, lfs_stub 33, empty 18 |
| today: repos / files | 265 / 11,111 |
| today files by action | written 2,448, deduped 6,300, in_stored 2,355, empty 8 |
| stored: history + today (in both) | 2,630 + 2,448 (0) |
| to send (distinct contents) | 5,078 files, 6.077 GB |
| on the share | 5,078 files, 6.077 GB |
| share = source (names and sizes) | yes (missing 0, extra 0) |

- `in_stored` = a file today whose content was already taken from history,
  so it isn't stored twice. `deduped` = the same content seen earlier in the
  same store.
- GB = 10^9 bytes. The extract runner's "8.14 GB written" counted some
  contents twice (forks writing the same blob at the same moment); the store
  holds 4.92 GB, as above.
- The 7 Oct exploration (14,183 files, 25,995 versions) counted changes, not
  distinct contents, so it doesn't compare directly with these numbers.

## Notes

- **rpt** (Crystal Reports): 9,437 of its files are only in today's copy,
  unchanged since their one commit (history has 1 repo, 721 versions). They
  come down to 2,240 distinct files. Without today's files they would have
  been missed.
- **Possibly sensitive:** evtx (event logs), dmp / jfr (dumps), pcap
  (network captures), pst (mailbox), faiss / lance (vector indexes).
- **Nothing to send:** hprof (both versions are Git LFS pointers; the file
  isn't in today's copy), xod and done (empty files only).
- Types with 0 history repos were found only in today's copy (never
  committed): dts, one, hpz, erwin, qix, px0-px10, oc5, xps, jpg_large, ggb,
  oft, dotm, sbn, ong, trdp, sbx, dcm, pst.

## By type (biggest first)

| ext | hist repos | hist versions | today files | to send | GB | share | ok |
|---|---:|---:|---:|---:|---:|---:|---|
| ltrar | 4 | 188 | 45 | 60 | 1.381 | 60 | yes |
| exr | 7 | 327 | 31 | 321 | 1.228 | 321 | yes |
| blg | 4 | 93 | 24 | 31 | 0.585 | 31 | yes |
| tif | 13 | 97 | 31 | 91 | 0.401 | 91 | yes |
| faiss | 16 | 76 | 27 | 76 | 0.323 | 76 | yes |
| data | 27 | 130 | 93 | 102 | 0.304 | 102 | yes |
| evtx | 1 | 24 | 0 | 24 | 0.265 | 24 | yes |
| ppt | 20 | 36 | 44 | 62 | 0.261 | 62 | yes |
| shp | 1 | 1 | 11 | 7 | 0.134 | 7 | yes |
| px9 | 2 | 18 | 20 | 17 | 0.126 | 17 | yes |
| bmp | 38 | 166 | 161 | 124 | 0.118 | 124 | yes |
| eps | 8 | 57 | 102 | 73 | 0.116 | 73 | yes |
| vsdx | 28 | 438 | 279 | 451 | 0.111 | 451 | yes |
| lance | 1 | 26 | 3 | 19 | 0.103 | 19 | yes |
| chm | 21 | 43 | 34 | 39 | 0.102 | 39 | yes |
| tiff | 9 | 19 | 16 | 22 | 0.098 | 22 | yes |
| xltm | 3 | 7 | 38 | 44 | 0.082 | 44 | yes |
| rpt | 1 | 721 | 9,437 | 2,240 | 0.057 | 2,240 | yes |
| potx | 3 | 32 | 5 | 18 | 0.040 | 18 | yes |
| crdownload | 3 | 8 | 0 | 7 | 0.036 | 7 | yes |
| docm | 3 | 126 | 128 | 123 | 0.034 | 123 | yes |
| jfr | 1 | 25 | 0 | 25 | 0.025 | 25 | yes |
| jfif | 14 | 27 | 34 | 38 | 0.024 | 38 | yes |
| exec | 15 | 50 | 4 | 53 | 0.023 | 53 | yes |
| vsd | 4 | 30 | 22 | 22 | 0.010 | 22 | yes |
| dotx | 6 | 37 | 73 | 19 | 0.010 | 19 | yes |
| dts | 0 | 0 | 2 | 2 | 0.009 | 2 | yes |
| coverage | 11 | 74 | 7 | 81 | 0.009 | 81 | yes |
| ldb | 3 | 15 | 0 | 15 | 0.006 | 15 | yes |
| mpp | 14 | 24 | 39 | 15 | 0.006 | 15 | yes |
| pdfx | 1 | 2 | 0 | 2 | 0.006 | 2 | yes |
| dmp | 2 | 9 | 0 | 9 | 0.004 | 9 | yes |
| erd | 2 | 11 | 2 | 10 | 0.003 | 10 | yes |
| one | 0 | 0 | 4 | 4 | 0.003 | 4 | yes |
| raw | 1 | 3 | 5 | 3 | 0.003 | 3 | yes |
| swp | 17 | 43 | 10 | 40 | 0.003 | 40 | yes |
| hpz | 0 | 0 | 1 | 1 | 0.003 | 1 | yes |
| erwin | 0 | 0 | 5 | 5 | 0.002 | 5 | yes |
| ps | 19 | 99 | 47 | 111 | 0.002 | 111 | yes |
| xsn | 6 | 60 | 6 | 24 | 0.001 | 24 | yes |
| frx | 2 | 105 | 3 | 98 | 0.001 | 98 | yes |
| qix | 0 | 0 | 2 | 2 | 0.001 | 2 | yes |
| px0 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| px1 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| px10 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| px4 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| px5 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| px6 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| px7 | 0 | 0 | 2 | 1 | 0.001 | 1 | yes |
| heic | 2 | 6 | 1 | 6 | 0.001 | 6 | yes |
| oc5 | 0 | 0 | 28 | 20 | 0.001 | 20 | yes |
| odg | 2 | 36 | 0 | 16 | 0.001 | 16 | yes |
| dib | 3 | 58 | 0 | 55 | 0.001 | 55 | yes |
| blend1 | 2 | 2 | 0 | 1 | 0.001 | 1 | yes |
| paw | 1 | 3 | 1 | 4 | 0.001 | 4 | yes |
| shx | 1 | 1 | 11 | 7 | 0.001 | 7 | yes |
| pen | 1 | 7 | 1 | 8 | 0.001 | 8 | yes |
| screenshot | 2 | 6 | 0 | 3 | 0.001 | 3 | yes |
| xbm | 7 | 310 | 66 | 118 | 0.000 | 118 | yes |
| xps | 0 | 0 | 2 | 2 | 0.000 | 2 | yes |
| xltx | 4 | 10 | 5 | 12 | 0.000 | 12 | yes |
| ppm | 6 | 20 | 4 | 8 | 0.000 | 8 | yes |
| avif | 17 | 32 | 7 | 14 | 0.000 | 14 | yes |
| odp | 1 | 2 | 2 | 3 | 0.000 | 3 | yes |
| pps | 1 | 1 | 2 | 2 | 0.000 | 2 | yes |
| thumb | 1 | 4 | 2 | 1 | 0.000 | 1 | yes |
| qdf | 1 | 1 | 1 | 2 | 0.000 | 2 | yes |
| jpg_large | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| xlt | 1 | 1 | 2 | 3 | 0.000 | 3 | yes |
| ggb | 0 | 0 | 3 | 3 | 0.000 | 3 | yes |
| oc5m | 1 | 1 | 9 | 6 | 0.000 | 6 | yes |
| lnk | 54 | 74 | 36 | 69 | 0.000 | 69 | yes |
| jpe | 1 | 2 | 3 | 3 | 0.000 | 3 | yes |
| ppsx | 1 | 1 | 2 | 3 | 0.000 | 3 | yes |
| rgb | 1 | 7 | 0 | 7 | 0.000 | 7 | yes |
| ods | 1 | 2 | 4 | 5 | 0.000 | 5 | yes |
| lk | 1 | 2 | 2 | 4 | 0.000 | 4 | yes |
| xla | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| xlr | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| xlw | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| xslm | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| pbm | 3 | 8 | 2 | 3 | 0.000 | 3 | yes |
| xpm | 3 | 121 | 24 | 45 | 0.000 | 45 | yes |
| oft | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| epub | 3 | 3 | 1 | 2 | 0.000 | 2 | yes |
| dotm | 0 | 0 | 3 | 3 | 0.000 | 3 | yes |
| onetoc2 | 1 | 7 | 0 | 7 | 0.000 | 7 | yes |
| svgz | 2 | 2 | 0 | 1 | 0.000 | 1 | yes |
| heif | 2 | 3 | 0 | 2 | 0.000 | 2 | yes |
| odt | 5 | 6 | 2 | 4 | 0.000 | 4 | yes |
| sbn | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| ~vsdx | 3 | 5 | 0 | 5 | 0.000 | 5 | yes |
| psess | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| swo | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| ong | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| dvi | 1 | 4 | 0 | 4 | 0.000 | 4 | yes |
| oc5c | 1 | 2 | 0 | 2 | 0.000 | 2 | yes |
| px2 | 0 | 0 | 2 | 1 | 0.000 | 1 | yes |
| px3 | 0 | 0 | 2 | 1 | 0.000 | 1 | yes |
| px8 | 0 | 0 | 2 | 1 | 0.000 | 1 | yes |
| trdp | 0 | 0 | 6 | 1 | 0.000 | 1 | yes |
| txn | 1 | 6 | 38 | 44 | 0.000 | 44 | yes |
| pgm | 4 | 9 | 1 | 3 | 0.000 | 3 | yes |
| pnm | 1 | 1 | 0 | 1 | 0.000 | 1 | yes |
| sbx | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| exif | 1 | 2 | 1 | 2 | 0.000 | 2 | yes |
| svg_ | 1 | 1 | 2 | 2 | 0.000 | 2 | yes |
| pcap | 3 | 5 | 0 | 2 | 0.000 | 2 | yes |
| ras | 2 | 6 | 1 | 1 | 0.000 | 1 | yes |
| random | 1 | 1 | 1 | 2 | 0.000 | 2 | yes |
| textclipping | 1 | 1 | 1 | 2 | 0.000 | 2 | yes |
| qtp | 2 | 8 | 5 | 2 | 0.000 | 2 | yes |
| dcm | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| pst | 0 | 0 | 1 | 1 | 0.000 | 1 | yes |
| done | 0 | 0 | 6 | 0 | 0.000 | 0 | yes |
| hprof | 2 | 2 | 0 | 0 | 0.000 | 0 | yes |
| xod | 6 | 9 | 1 | 0 | 0.000 | 0 | yes |

Types where the share differs from the source: none.

## Still to do for Group 2

- The same 117 types inside the 4.2 / 4.2_R archives (`group4_2_files.py`
  into `<run>/group4_7_9_group2_binary`, copy with
  `STEP_DIRS=group4_7_9_group2_binary ./copy_group4_2.sh`).
