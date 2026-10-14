**Subject: Git history PII extraction: files inside the 4.2 / 4.2_R archives, update 14 Oct**

Hi all,

We have now taken the files found inside the group 4.2 (archives) and 4.2_R (packages) archives, for every group we have already run: 1, 2, 3, 4.5 and 4.5_R. Each group used the same method as its original run.

**How**
- The archives were unpacked earlier, with nested zip/tar/7z opened up to 3 levels. These runs read only that unpacked output.
- There is no delta. Archives are containers, so today's copies were most likely never scanned, and every version is included.
- Left out and listed, not sent: vendored or generated folders (node_modules, packages, bin, obj, dist…), macOS metadata (`__MACOSX/`, `._*` files, `.DS_Store`) and the DocFX build cache.

**Results**

| Group | Method | 4.2 Archives: input files | 4.2 Archives: output | 4.2 Archives: volume | 4.2 Archives: size | 4.2_R Packages: input files | 4.2_R Packages: output | 4.2_R Packages: volume | 4.2_R Packages: size |
|---|---|---|---|---|---|---|---|---|---|
| Group 1 – Text (32 types) | One .txt per path, lines merged, each line written once | 2,151,155 | 712,949 .txt | 219 M lines | 19.1 GB | 384,570 | 112,375 .txt | 19.4 M lines | 13.9 GB |
| Group 4.5_R – Text, reclassified from 4.3/4.6 (890 types) | Same | 157,574 | 39,285 .txt | 2.7 M lines | 1.0 GB | 35,852 | 10,489 .txt | 7.9 M lines | 0.8 GB |
| Group 4.5 – Text, other types (756 types) | Same | 181,571 | 95,110 .txt | 24.6 M lines | 2.3 GB | 24,755 | 10,972 .txt | 1.0 M lines | 56 MB |
| Group 2 – Text (9 types) | Same | 65,121 | 9,732 .txt | 0.27 M lines | 15 MB | 49 | 0 (all in vendored folders) | – | – |
| Group 2 – Binary (18 types: Office, images, MSG, PFX, etc.) | One copy of each distinct file content per repo | 71,969 | 18,256 files | – | 3.9 GB | 34,207 | 3,525 files | – | 0.13 GB |
| Group 3 – PKL | Strings extracted as text; files never unpickled | 89 | 62 .txt | 1.9 M lines | – | 0 | – | – | – |
| Group 3 – PDF | One PDF per path; distinct pages kept; no OCR | 3,005 | 2,832 PDFs¹ | 77,145 pages kept (of 90,996) | – | 7 | 7 PDFs | 70 pages | – |

¹ Includes 12 PDFs that could not be opened (damaged or password-protected), copied whole.

**Summary**
- 4.2 Archives: 1,291 repositories processed successfully.
- 4.2_R Packages: 242 repositories processed successfully.
- The output is on the server and has not yet been copied to the share.
- In total 1,015,594 files written (about 41 GB, PDF and PKL sizes not counted).
- Scope counts match the archive-unpack counts per type for groups 1, 2, 4.5_R, 4.5 and PKL. Still to confirm: PDF (4.2).

**Notes**
- Most of the group 2 binary files inside packages are repeats. Power Platform canvas apps carry the same template images about 1,700 times each, in `ey-assurance-di-power-platform` and `-test`, which look like copies of each other. These were written once per repo, and the repeats are listed.
- PDF: 2,027 kept pages (2.6%) have no text, meaning scans or images. A scanner without OCR cannot read them. They are listed per page.

**Next**
- Copy all of this to the share, with data and tracking files in separate folders.
- Group 4.1 (databases) and 4.2 (packages such as jar/nupkg found inside archives): path lists only.
- Group 4.2_R packages found inside archives (msapp, sppkg…, about 430 files): to be decided.

Regards,
Muthu
