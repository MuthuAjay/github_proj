# Git archive analysis: delta completed (27 Sep 2026)

The delta step has now finished for all 13,631 repositories. The final output
holds only the content that hasn't already been processed.

## Key figures

| | |
|---|---|
| Repositories processed | **13,631** (all successful) |
| History lines analysed | **1,721,776,940** |
| Lines removed (already processed) | **481,866,737 (28.0%)** |
| **Lines remaining (not yet processed)** | **1,239,910,203 (72.0%)** |
| **Files in the final output** | **6,838,128** |
| File types in scope | 32 |
| Run time | about 71 minutes |

## What happened to each file

| Outcome | Files | Share | What it means |
|---|---:|---:|---|
| **Kept in full: no longer exists in the repositories** | 6,059,817 | 69.6% | Deleted, renamed or only on other branches; none of its content was processed |
| **Reduced to older content only** | 778,305 | 8.9% | Still exists, but has older lines that are no longer in today's version; only those were kept |
| **No new content (not kept)** | 1,797,135 | 20.6% | Still exists, and everything in its history is also in today's version |
| **Not text / empty (not kept)** | 74,135 | 0.9% | Images and other non-text files, or files with no content |
| **Kept in full: current file unreadable** | 6 | 0.0% | Today's version couldn't be read as text, so the full history was kept to be safe |
| **Total files analysed** | **8,709,398** | 100% | |

Files in the final output = 6,059,817 + 778,305 + 6 = **6,838,128**.

## How the comparison works

The delta is worked out one file at a time, comparing the file's history with
its current version in the active repositories:

1. **Does the file exist today?** If not, the whole history is kept, since
   none of it has been processed.
2. **If it does,** the current file is read and prepared the same way as the
   history: spaces at the start and end of each line are trimmed and blank lines
   removed.
3. **Each line of the history is checked against the current file:**
   - lines still present today are dropped (already processed)
   - lines that only existed in older versions are kept
4. **The kept lines are saved in their original order.** If nothing is left,
   no file is saved for it.

**Example:** if a configuration file once had `DB_HOST = "old-server"` and
`DEBUG = True`, and today has `DB_HOST = "new-server"` without the debug
setting, the delta keeps the old server value and the removed debug line.
Everything still in the current file is dropped.

**Matching rules:**

- A line must match exactly after trimming. Changes in indentation are
  ignored, but any other change counts as different.
- A line counts as present even if it has moved elsewhere in the same file.
- The comparison is within each file only, so content that moved to a
  different file is kept. The rules always err towards keeping content, never
  losing it.
- If a current file can't be read, its full history is kept.

## Issue encountered and resolved

During the first run, the storage mount for the active repositories
disconnected part-way through, so about 10,900 repositories were processed
without their current files and nothing was removed for them. We identified
the cause, reconnected the storage and corrected a mount setting. We also
changed the process so that it now stops automatically if the storage becomes
unavailable, and resumes from where it stopped once it's reconnected. Only the
affected repositories were rerun, and the figures above are from the
completed, corrected run.

## Next steps

1. Final validation: spot-checks of the output against the current repository
   files.
2. Remove the full history extraction once validation is complete, to free up
   storage.
3. Share a consolidated report with the final figures.
