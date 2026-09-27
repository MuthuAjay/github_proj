# Git archive analysis: history-only files (28 Sep 2026)

We looked at every file that existed in the repositories' history but no
longer exists in the active repositories, and worked out why each one
disappeared: when it was added, when it was removed, by which change, and
whether it was deleted or renamed.

## Summary

- **6,059,816 files** exist only in history. Of the **13,631** repositories
  analysed, **8,420** have such files; the other 5,211 still contain every
  file they ever had.
- **30% (1.83M)** are third-party code, mostly JavaScript packages
  (`node_modules`), saved into repositories and later cleaned out.
- **70% (4.23M)** are the teams' own files:
  - **50%** were deleted
  - **19%** were renamed or moved; their content largely continues under a
    new name
  - **31%** are in history that can't be placed on a branch, because the
    archived copies don't record branch names
- **Removals are recent and growing:** over 550,000 own files were removed
  in 2025, and over 530,000 so far in 2026.
- **A few repositories dominate:** the Motif design-system repositories
  alone account for about **900,000 files (15%)**, mostly generated icon and
  preview files.
- **Several repositories are near-copies of each other,** so some history
  is counted more than once.

## Key figures

| | Files | Share |
|---|---:|---:|
| **History-only files** | **6,059,816** | 100% |
| Teams' own files | 4,225,510 | 69.7% |
| Third-party code (not traced further) | 1,834,306 | 30.3% |

| Repositories | Count |
|---|---:|
| Analysed | 13,631 |
| **With history-only files** | **8,420** |
| of which with the teams' own files | 8,406 |
| of which with third-party code only | 14 |
| With no history-only files (every file still exists) | 5,211 |

## Why the teams' own files are gone

| Reason | Files | Share of own files | Meaning |
|---|---:|---:|---|
| **Deleted** | 2,122,019 | 50.2% | Removed and not replaced |
| **Renamed or moved** | 793,441 | 18.8% | Continued under a new name or folder (see how renames are identified, below) |
| **Branch unknown** | 1,310,050 | 31.0% | In history, but not on the main line; the archived copies hold no branch names, so the branch can't be identified |
| Still on a branch | 0 | 0.0% | Can't be determined: branch information isn't available in the archived copies |
| **Total** | **4,225,510** | 100% | |

## Third-party code by folder

These files weren't written by the teams. They're listed but not traced
further.

| Folder | Files | What it is |
|---|---:|---|
| node_modules | 1,304,983 | JavaScript packages |
| site-packages | 185,505 | Python packages |
| packages | 119,172 | .NET / NuGet packages |
| vendor | 104,348 | Bundled third-party code |
| bin | 39,156 | Build output |
| obj | 28,221 | Build output |
| dist | 24,580 | Built / packaged output |
| build | 13,773 | Build output |
| target | 6,166 | Build output (Java / Rust) |
| coverage | 3,576 | Test coverage reports |
| wwwroot/lib | 2,422 | Web libraries |
| .next | 1,749 | Next.js build output |
| bower_components | 654 | Older JavaScript packages |
| **Total** | **1,834,306** | |

## Own files removed, by year

| Year | Deleted | Renamed | Total |
|---|---:|---:|---:|
| 2010 | 141 | 109 | 250 |
| 2011 | 366 | 1,329 | 1,695 |
| 2012 | 2,533 | 1,115 | 3,648 |
| 2013 | 5,245 | 9,095 | 14,340 |
| 2014 | 5,927 | 12,258 | 18,185 |
| 2015 | 13,970 | 5,690 | 19,660 |
| 2016 | 7,296 | 2,592 | 9,888 |
| 2017 | 24,097 | 8,504 | 32,601 |
| 2018 | 68,748 | 16,418 | 85,166 |
| 2019 | 113,643 | 39,177 | 152,820 |
| 2020 | 177,256 | 55,104 | 232,360 |
| 2021 | 183,775 | 89,583 | 273,358 |
| 2022 | 156,301 | 103,593 | 259,894 |
| 2023 | 211,506 | 120,813 | 332,319 |
| 2024 | 294,592 | 95,019 | 389,611 |
| 2025 | 447,651 | 107,966 | 555,617 |
| 2026 (to date) | 408,972 | 125,076 | 534,048 |

Files in the "branch unknown" group have no removal date, so they aren't in
this table.

## Largest single removals

The biggest removals are mostly clean-ups of generated or bulk content.

| Repository | Date | Files removed | Change description |
|---|---|---:|---|
| ey-fso-tax/reitsuite | 2024-01-04 | 37,163 | Remove old node module folder |
| ey-org/appon-gehealthcare | 2025-09-02 | 28,021 | backup |
| ey-org/velocity-main | 2021-08-20 | 25,144 | Deleted Release |
| ey-org/velocity-prod-without-f2f | 2021-08-20 | 25,144 | Deleted Release |
| ey-org/tp-web | 2022-04-03 | 20,552 | Merge remote-tracking branch 'origin/main' into Ticket/776 |
| ey-org/assurance-aigw-survey-engine-client | 2018-02-16 | 18,495 | adding master... |
| ey-org/goaip-chatbot | 2020-06-10 | 17,165 | Deleted OgpDb |
| ey-org/goaip-et-chatbot | 2020-06-10 | 17,165 | Deleted OgpDb |
| ey-org/ctors-ctdm | 2023-10-27 | 13,042 | Merge branch 'feature/dotnet6' into bugfix/classificationfilter |
| ey-org/tax-ctdm | 2023-10-27 | 13,042 | Merge branch 'feature/dotnet6' into bugfix/classificationfilter |
| ey-org/ey-finops-insightX | 2026-03-19 | 12,287 | Main v2 (#30) |
| ey-org/dellpilot | 2024-07-01 | 10,275 | deleted generated images |
| ey-org/canal-plm | 2023-04-19 | 10,006 | Automatic solution commit |
| ey-org/wam-fa-databricks | 2024-09-11 | 8,712 | Normalized the code |
| ey-org/eyxp-accelerator-application | 2026-02-18 | 8,145 | fix: remove accidentally committed .v10-temp icon files and update gitignore |
| ey-org/motif-icon | 2025-11 to 2026-02 | ~6,100 each | Remove preview for closed PR (repeated for every closed PR) |

The same removal appears in pairs of repositories (for example
`velocity-main` / `velocity-prod-without-f2f` and `goaip-chatbot` /
`goaip-et-chatbot`). These repositories are copies of each other.

## Repositories with the most history-only files

| Repository | Files | Deleted | Renamed | Branch unknown | Third-party |
|---|---:|---:|---:|---:|---:|
| ey-org/Motif-React | 340,879 | 281,537 | 54,866 | 4,457 | 19 |
| ey-org/motif-icon | 298,775 | 201,942 | 1,111 | 91,026 | 4,696 |
| ey-org/motif-web-components | 152,345 | 81,149 | 61,431 | 8,833 | 932 |
| ey-org/sotf-test-assessments-phase-2 | 112,063 | 0 | 0 | 16 | 112,047 |
| ey-org/via-repo | 99,700 | 123 | 111 | 2,260 | 97,206 |
| ey-org/tp-web | 90,948 | 44,670 | 35,474 | 10,350 | 454 |
| ey-org/Ansible-PlayBooks | 74,538 | 1,706 | 141 | 72,691 | 0 |
| ey-org/ui-nexus-design-system | 63,820 | 2,867 | 633 | 58,618 | 1,702 |
| ey-org/ey-cdap-ui | 63,122 | 13,813 | 17,967 | 2,495 | 28,847 |
| ey-org/ey-cdap | 62,285 | 10,825 | 16,334 | 6,278 | 28,848 |
| ey-org/asu-motif | 62,269 | 29,199 | 10 | 32,188 | 872 |
| ey-org/starbucks | 53,326 | 0 | 0 | 435 | 52,891 |
| ey-org/NG-Motif | 53,252 | 49,789 | 1,286 | 2,156 | 21 |
| ey-org/tax-ctdm | 52,409 | 12,371 | 21,097 | 17,219 | 1,722 |
| ey-org/nclcai-initiaitves | 51,905 | 257 | 37 | 457 | 51,154 |

The five Motif repositories (Motif-React, motif-icon, motif-web-components,
asu-motif, NG-Motif) hold about **907,000** history-only files between them.
Several others are almost entirely third-party code (for example
sotf-test-assessments-phase-2, via-repo, starbucks and nclcai-initiaitves).

## How this was worked out

**Which files:** every file that was extracted from history (32 in-scope
file types) but doesn't exist in the active repositories today.

**For each of the teams' own files, from the repository's git history:**

- when it was first added, and when it was last changed
- the change that removed it, with its date and description
- whether it was deleted or renamed and, if renamed, its new name and
  whether that exists today
- how many times it changed

No author names or email addresses were collected.

**Third-party code** (files inside `node_modules`, `site-packages`,
`packages`, `vendor`, `bin`, `obj`, `dist`, `build` and similar folders)
is listed and labelled, but not traced further.

### How a rename is identified

Git doesn't record renames. It works them out by comparing content, and we
use git's own built-in rename detection:

1. In a single change, git looks at the files that disappeared and the
   files that appeared.
2. An identical pair counts as a rename (a pure move).
3. Otherwise git measures how much of the old file's content appears in the
   new one. **If at least 50% is the same, it's a rename.** Otherwise the
   old file is deleted and the new one is a separate new file.
4. Names don't matter, only content. `Old/Helper.cs` → `Common/Helper.cs`
   is a rename if the content matches.

A file counts as **renamed** if the last thing that happened to it was such
a rename.

## Points to note

1. **"Renamed" is a minimum figure.** Some renames show as deletions:
   - the file was removed in one change and re-added in a later one
   - it was heavily rewritten while being moved
   - thousands of files were moved in a single change (git then only matches
     exact copies)
   - the file is in one of the 25 largest repositories, which were processed
     without rename matching to keep run times practical
2. **"Branch unknown" is not the same as lost.** The archived copies don't
   record branch names. These files existed in history, but we can't say
   which branch they were on, or whether a branch still holds them. That's
   also why "still on a branch" shows 0.
3. **Some removals are credited to a merge.** When a file was removed on one
   branch and that change was later merged into another, the merge can
   appear as the removing change (for example "Merge branch 'master' into
   …"). The file really was removed, but the original deletion was an
   earlier change.
4. **Duplicate repositories inflate the totals.** Several repositories are
   copies of the same code base (the Grafana variants, the CE4/CE5 family,
   velocity-main / velocity-prod-without-f2f, goaip-chatbot /
   goaip-et-chatbot, ctors-ctdm / tax-ctdm). Their history is counted once
   per copy.
5. **Renamed files largely repeat content held elsewhere.** A renamed
   file's content mostly continues under its new name, either in the
   current file (already processed) or in the new name's own history.
   The delta keeps the old name's history in full, so it overstates truly
   new content by roughly the size of the renamed files' old history.
6. The total is one file short of the delta's history-only count
   (6,059,817). That's a single row being checked; it doesn't affect the
   figures.

## Next steps

1. Optional refinements:
   - record the original deletion rather than a later merge
   - add git's similarity score for each rename
   - remove rename duplicates from the delta
2. Estimate unique content across duplicate repositories.
3. Decide how to treat the Motif generated files and the third-party code in
   the next processing step.
