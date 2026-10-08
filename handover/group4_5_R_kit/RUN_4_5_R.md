# Group 4.5_R: run on the server (LVM20)

890 readable text types from groups 4.3 / 4.6 (`text_extensions.txt`,
from group4_5_R.xlsx; `qa` was listed twice there). Same text track as 4.5:
every line any version had, then today's lines removed (delta).
Vendored files (node_modules, packages, vendor, bin, obj, dist, build, ...)
are left out: `SKIP_VENDORED=1`.

## 1. Copy (as files, scp or this tarball), check, make runnable

```bash
mkdir -p /data/workarea/scripts_4_5_R
cd /data/workarea/scripts_4_5_R
tar xzf ~/group4_5_R_kit.tar.gz --strip-components=1
md5sum -c md5sums.txt            # every line must say OK
chmod +x run_text_versions.sh copy_group4_5.sh
```

## 2. Before starting

```bash
df -h /data/workarea                                        # free space
ls /home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos | head   # active copy mounted
ls /data/workarea/file_history_out_4/batches/plan.csv       # batch plan
```

## 3. Try one small batch, then the full run

```bash
cd /data/workarea/scripts_4_5_R
SCRIPTS=$PWD SKIP_VENDORED=1 \
EXTS="$(tr '\n' ' ' < text_extensions.txt)" \
OUT=/data/workarea/group4_5_R/extract \
nohup ./run_text_versions.sh S01 > /data/workarea/group4_5_R_S01.out 2>&1 &
```

When S01 looks right, the full run (S01 is skipped as done):

```bash
SCRIPTS=$PWD SKIP_VENDORED=1 \
EXTS="$(tr '\n' ' ' < text_extensions.txt)" \
OUT=/data/workarea/group4_5_R/extract \
nohup ./run_text_versions.sh > /data/workarea/group4_5_R.out 2>&1 &
tail -f /data/workarea/group4_5_R.out
```

After an SSH drop: do NOT start it again; `tail -f` the .out file.
If it stopped (exit 3: disk or mount), run the same command again.
The delta (what to send) is `/data/workarea/group4_5_R/extract_delta`.
The skipped vendored files are listed in
`/data/workarea/group4_5_R/extract/skipped_vendored_all.csv` (org, repo,
path, vendored folder, versions); one list per repo in `extract/_state`.
The copy step puts it in the tracking folder on the share.

## 4. Report

```bash
python3 delta_by_extension.py \
    --extract /data/workarea/group4_5_R/extract \
    --delta /data/workarea/group4_5_R/extract_delta \
    --extensions text_extensions.txt \
    --out /data/workarea/group4_5_R/extract_delta_by_extension.csv
```

## 5. Copy to the share (after the log ends with "runner done")

Check DST first (the share folder for 4.5_R).

```bash
sudo bash -c 'cd /data/workarea/scripts_4_5_R && \
    OUT=/data/workarea/group4_5_R/extract \
    EXTS_FILE=/data/workarea/scripts_4_5_R/text_extensions.txt \
    DST=/home/ganeshk/eng-gh2-data-fs/diff_analysis/p1/group4_5_R \
    nohup ./copy_group4_5.sh > /data/workarea/copy_group4_5_R.out 2>&1 &'
tail -f /data/workarea/copy_group4_5_R.out
```

Tracking files (manifests, done.json, report) go to `DST` + `_tracking`,
never into the scanned folder. It ends with `ALL DONE`.
