**Subject: Git history PII extraction: files inside the 4.2 / 4.2_R archives, update 14 Oct**

Hi all,

We have now taken the files found inside the group 4.2 (archives) and 4.2_R (packages) archives, for every group we have already run: 1, 2, 3, 4.5 and 4.5_R. Each group used the same method as its original run. All repos finished ok (4.2: 1,291 repos, 4.2_R: 242). The output is on the server and not yet copied to the share.

**How**
- The archives were unpacked earlier, with nested zip/tar/7z opened up to 3 levels. These runs read only that unpacked output.
- There is no delta. Archives are containers, so today's copies were most likely never scanned, and every version is included.
- Left out and listed, not sent: vendored or generated folders (node_modules, packages, bin, obj, dist…), macOS metadata (`__MACOSX/`, `._*` files, `.DS_Store`) and the DocFX build cache.

**Results**

| Group | Method | 4.2: files in scope → written | 4.2_R: files in scope → written |
|---|---|---|---|
| Group 1 (32 text types) | One .txt per path, lines merged, each once | 2,151,155 → **712,949 .txt** (219 M lines, 19.1 GB) | 384,570 → **112,375 .txt** (19.4 M lines, 13.9 GB) |
| Group 4.5_R (890 text types) | Same | 157,574 → **39,285 .txt** (2.7 M lines, 1.0 GB) | 35,852 → **10,489 .txt** (7.9 M lines, 0.8 GB) |
| Group 4.5 (756 text types) | Same | 181,571 → **95,110 .txt** (24.6 M lines, 2.3 GB) | 24,755 → **[ ] .txt** ([ ] lines, [ ] GB) |
| Group 2 text (9 types) | Same | 65,121 → **9,732 .txt** (0.27 M lines) | 49 → 0 (all in vendored folders) |
| Group 2 binary (18 types: Office, images, msg, pfx…) | Whole files, each distinct content once per repo | 71,969 → **18,256 files** (3.9 GB) | 34,207 → **3,525 files** (0.13 GB) |
| Group 3 pkl | Strings as text, never unpickled | 89 → **62 .txt** (1.9 M lines) | none found |
| Group 3 pdf | One PDF per path, every distinct page once, no OCR | 3,005 → **2,832 files** (77,145 pages kept of 90,996) | 7 → **7 files** (70 pages) |

**Notes**
- For every group, the scope counts match the per-type counts from the unpack step.
- Most of the group 2 binary files inside packages are repeats. Power Platform canvas apps carry the same template images about 1,700 times each, in `ey-assurance-di-power-platform` and `-test`, which look like copies of each other. These were written once per repo, and the repeats are listed.
- PDF: 2,027 kept pages (2.6%) have no text, meaning scans or images. A scanner without OCR cannot read them. They are listed per page. 12 PDFs could not be opened (damaged or password-protected) and were copied whole.

**Next**
- Copy all of this to the share, with data and tracking files in separate folders.
- Group 4.1 (databases) and 4.2 (packages such as jar/nupkg found inside archives): path lists only.
- Group 4.2_R packages found inside archives (msapp, sppkg…, about 430 files): to be decided.

Regards,
Muthu
