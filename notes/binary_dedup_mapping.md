# Binary extraction: deduplication and mapping back to repositories

Notes, 30 Sep 2026.

## The question

The binary extraction stores each identical file only once, even when it
appears in many repositories, paths or commits. When the PII scan flags a
file, we need to know every place it came from.

## The answer: nothing is lost by deduplicating

Only the **file** is stored once. Every place it came from is recorded, and
every record points to the one stored file.

| File | One row per | Contains |
|---|---|---|
| `binary_versions/manifest.csv` (written by `extract_versions.py`) | every occurrence: each repo, path and version | org, repo, path, ext, **blob** (the content fingerprint), **stored_as** (the file sent), why it is sent, first/last commit and date, vendored |
| `ext_versions/analysis/unique_files.csv` (written by `ext_versions_analysis.py`) | every unique file (195,483) | stored_as, number of **occurrences** and **repos**, and its **original**: the first repo, path and commit where it appeared |

The two link on **`blob` / `stored_as`**. Every manifest row with the same
`blob` is the same content and points to the same stored file:
`files/<ext>/<first 2 characters of blob>/<blob>.<ext>`.

### Example

`logo192.png`, present in 134 repositories, stored once:

```
unique_files.csv:  files/png/3f/3f2a…c1.png   occurrences 150   repos 134
                   original: ey-org/Capital-Edge:copilot/ui/public/logo192.png
manifest.csv:      150 rows with blob 3f2a…c1, one per repo/path/version,
                   all with stored_as files/png/3f/3f2a…c1.png
```

A PII finding in one stored file therefore applies to **every** repository
and path listed for it in the manifest.

## How to use it

All places one stored file came from:

```bash
grep <blob> /data/workarea/binary_versions/manifest.csv
```

One row per sent file, with all its locations in one cell:

```bash
cd /data/workarea/binary_versions
python3 - <<'EOF'
import csv, collections
loc = collections.defaultdict(list)
for r in csv.DictReader(open("manifest.csv", encoding="utf-8", errors="replace")):
    if r["stored_as"]:
        loc[r["stored_as"]].append("%s/%s:%s" % (r["org"], r["repo"], r["path"]))
with open("file_locations.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["stored_as", "repos", "occurrences", "locations"])
    for f, l in sorted(loc.items()):
        w.writerow([f, len({x.split(":")[0] for x in l}), len(l), " | ".join(sorted(set(l)))])
print(len(loc), "files -> file_locations.csv")
EOF
```

## How the numbers relate

```
versions                       every different content each path has had
 − versions_processed          identical to a file in the same repo's active copy
 − lfs_stubs                   Git LFS pointers; content not in the archive
 = versions_to_send
 − duplicates_removed          exact copies in other paths or repos
 = delta_dedup                 unique files actually sent
```

Totals across the 18 binary extensions:
918,618 − 350,760 − 20,552 = 547,306 versions to send;
547,306 − 351,823 = **195,483 unique files** (~76 GB).

## Possible follow-up

`file_locations.csv` could be written automatically by the extraction's
combine step (`extract_versions.py --combine-only`), with a repo count and
the list of occurrences per stored file.
