#!/usr/bin/env python3
"""
hash_inventory.py - hash every file belonging to every repo under a
(potentially huge) directory tree, and write repo, file_path, hash, size to
a CSV.

Built for scanning something like an AllRepos\\<org>\\<repo>\\... dump. A
directory is treated as a "repo root" if it has a .git subdirectory OR is
itself a bare git dir (HEAD + objects/ + refs/ present directly in it) -
matching the mix of normal clones, mirrors, and bare backups these dumps
tend to contain.

IMPORTANT: for a repo whose git history resolves (HEAD/a branch points at a
real commit), content is read straight from git's object store via
`git ls-tree` / `git cat-file` - NOT by walking the working directory. This
matters a lot for repo dumps: many bulk-cloned/mirrored estates never run a
checkout step, so the working tree is empty even though the repo's full
history and content sit intact in .git/objects. Sourcing from git means
those repos are still inventoried correctly. A repo whose git plumbing is
totally broken (missing HEAD, no resolvable ref at all - see
compare_repos.py / extract_from_pack.py for that scenario) can't be sourced
this way, and is logged to "<out>.needs_recovery.csv" so it's not silently
invisible. For these, the .git directory's raw contents (pack files, .idx
files, loose objects, hooks - everything) are hashed directly as plain
files instead: there's no resolvable commit to walk a tree from, so a named
path per blob isn't available, but the .pack/.idx files themselves are real
recoverable content and are hashed as-is.

Files that aren't under any repo at all are hashed straight off disk and
labeled "(no-repo)". For a HEALTHY repo (sourced via git ls-tree/cat-file
above), .git is skipped entirely - its internals would just duplicate
content already captured properly, with real paths, from the tree walk.

Default hash is git's own blob hash (sha1("blob <size>\\0" + content)), so a
hash in this CSV can be compared directly against `git hash-object` /
compare_repos.py output. For git-sourced repos this comes for free from
`git ls-tree` with zero bytes read; --algo sha256/blake2b/md5 read blob
content via `git cat-file --batch` instead. For plain disk files (no-repo,
or broken-repo fallback), hashing is done in-process, streamed, no
subprocess per file.

Repo *discovery* (finding which directories are repos and what they resolve
to) never shells out to git - it reads .git/HEAD, the ref file it points at,
and packed-refs directly as plain files. Spawning a git.exe process per
candidate directory (as earlier versions of this script did) is the actual
bottleneck on an estate with tens of thousands of folders, especially over a
network drive or with antivirus intercepting each process launch; a file
read has none of that overhead. git subprocesses are only used afterwards,
per repo that's actually going to be hashed. Directory scanning itself is
also parallelized (--scan-workers) since listing thousands of directories
one at a time is latency-bound, not CPU-bound - concurrency hides that
latency.

Designed to survive a run measured in hours over a big/flaky drive:
  - streams rows to disk as it goes (no in-memory result buffering)
  - per-file/per-repo errors are recorded in the CSV, not fatal to the run
  - --resume skips (repo, file_path) pairs already present in --out
  - --scan-workers parallelizes directory discovery; --workers hashes/reads
    the git objects, one repo or one disk file per task
  - long Windows paths (>260 chars) are opened via the \\\\?\\ prefix

Usage:
    python hash_inventory.py D:\\AllRepos --out inventory.csv
    python hash_inventory.py D:\\AllRepos --out inventory.csv --workers 8 --resume
    python hash_inventory.py D:\\AllRepos --out inventory.csv --disk-only   # old behavior: ignore git, walk disk only
"""

import argparse
import csv
import hashlib
import os
import queue
import subprocess
import sys
import threading
import time

CHUNK = 4 * 1024 * 1024
NO_REPO = "(no-repo)"
SENTINEL = object()


def get_git_dir(path):
    """The directory holding HEAD/objects/refs for this repo root, or None.
    Cheap: pure os.path checks, no subprocess, no file reads."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if (os.path.isdir(os.path.join(path, "objects")) and
            os.path.isdir(os.path.join(path, "refs")) and
            os.path.exists(os.path.join(path, "HEAD"))):
        return path  # bare repo: path itself is the git dir
    return None


def winlong(path):
    """Prefix with \\\\?\\ on Windows so paths over MAX_PATH can still be opened."""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def to_posix(rel):
    return rel.replace(os.sep, "/")


def hash_file(path, algo):
    open_path = winlong(path)
    size = os.path.getsize(open_path)
    if algo == "git":
        h = hashlib.sha1()
        h.update(f"blob {size}\0".encode("utf-8"))
    else:
        h = hashlib.new(algo)
    with open(open_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest(), size


# ------------------------------------------------------------------ git side

# Repos on foreign-uid mounts (Windows drives under /mnt, network shares) trip
# git's "detected dubious ownership" check, which aborts every plumbing call
# before it emits a byte. safe.directory is only honoured from protected
# config - system, global, or the command line - so pass it per-invocation
# here rather than relying on the machine's global git config.
GIT = ["git", "-c", "safe.directory=*"]

_HEX = set("0123456789abcdefABCDEF")


def _looks_like_sha(s):
    return len(s) in (40, 64) and all(c in _HEX for c in s)


def _read_text(path):
    try:
        with open(winlong(path), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def read_ref(git_dir, ref, _depth=0):
    """Resolve a ref name to a commit sha by reading loose ref files and
    packed-refs directly - no subprocess."""
    if _depth > 5:
        return None
    content = _read_text(os.path.join(git_dir, *ref.split("/")))
    if content:
        if content.startswith("ref:"):
            return read_ref(git_dir, content[4:].strip(), _depth + 1)
        if _looks_like_sha(content):
            return content
    packed = _read_text(os.path.join(git_dir, "packed-refs"))
    if packed:
        for line in packed.splitlines():
            line = line.strip()
            if not line or line[0] in "#^":
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def resolve_rev_fast(git_dir):
    """First ref that actually resolves to a commit, read straight off disk.
    Returns None if the repo's git metadata is too broken to resolve
    anything at all (e.g. HEAD missing, or points at a branch that was
    never created and isn't in packed-refs either - unborn HEAD)."""
    head = _read_text(os.path.join(git_dir, "HEAD"))
    if head:
        if head.startswith("ref:"):
            sha = read_ref(git_dir, head[4:].strip())
            if sha:
                return sha
        elif _looks_like_sha(head):
            return head
    for cand in ("refs/heads/master", "refs/heads/main",
                 "refs/remotes/origin/HEAD", "refs/remotes/origin/master",
                 "refs/remotes/origin/main"):
        sha = read_ref(git_dir, cand)
        if sha:
            return sha
    return None


def _parse_ls_tree_entry(entry):
    if not entry:
        return None
    try:
        meta, path = entry.split(b"\t", 1)
    except ValueError:
        return None
    parts = meta.split()
    if len(parts) < 4 or parts[1] != b"blob":
        return None
    sha, size = parts[2], parts[3]
    try:
        size_i = int(size)
    except ValueError:
        size_i = None
    return path.decode("utf-8", "surrogateescape"), sha.decode(), size_i


def ls_tree_blobs(repo, rev):
    """(path, blob_sha, size) for every blob in rev's tree - works whether or
    not the repo has ever been checked out to disk."""
    proc = subprocess.Popen(GIT + ["-C", repo, "ls-tree", "-r", "-l", "-z", rev],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            *complete, buf = buf.split(b"\x00")
            for entry in complete:
                row = _parse_ls_tree_entry(entry)
                if row:
                    yield row
        if buf:
            row = _parse_ls_tree_entry(buf)
            if row:
                yield row
    finally:
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git ls-tree failed in {repo}: {err.decode('utf-8', 'replace').strip()}"
            )


def cat_file_batch_hash(repo, specs, algo):
    """specs: [(path, blob_sha), ...]. Yields (path, hexdigest, size, error)
    with blob content pulled from git's object store, not from disk."""
    if not specs:
        return
    proc = subprocess.Popen(GIT + ["-C", repo, "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        for path, sha in specs:
            try:
                proc.stdin.write((sha + "\n").encode("ascii"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                yield path, "", "", f"cat-file pipe error: {exc}"
                continue
            header = proc.stdout.readline().decode("utf-8", "replace").strip()
            bits = header.split()
            if len(bits) < 3 or header.endswith("missing"):
                yield path, "", "", f"cat-file miss: {header!r}"
                continue
            try:
                size = int(bits[-1])
            except ValueError:
                yield path, "", "", f"bad cat-file header: {header!r}"
                continue
            h = hashlib.new(algo)
            remaining = size
            while remaining > 0:
                chunk = proc.stdout.read(min(CHUNK, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
            proc.stdout.read(1)  # trailing newline
            yield path, h.hexdigest(), size, ""
    finally:
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        proc.wait()


def git_repo_rows(repo_label, repo_dir, rev, algo, done_keys):
    if algo == "git":
        for path, sha, size in ls_tree_blobs(repo_dir, rev):
            if (repo_label, path) in done_keys:
                continue
            yield repo_label, path, sha, size if size is not None else "", ""
    else:
        specs = [(path, sha) for path, sha, _size in ls_tree_blobs(repo_dir, rev)
                 if (repo_label, path) not in done_keys]
        for path, digest, size, err in cat_file_batch_hash(repo_dir, specs, algo):
            yield repo_label, path, digest, size, err


# --------------------------------------------------------------------- walk

class PendingCounter:
    """Tracks directories enqueued-but-not-yet-fully-processed. Hits zero
    exactly when scanning is completely done (nothing left in the queue and
    nothing still being expanded), which is the signal to stop the scanners."""

    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()
        self.done = threading.Event()

    def inc(self, k=1):
        with self._lock:
            self._n += k

    def dec(self):
        with self._lock:
            self._n -= 1
            if self._n <= 0:
                self.done.set()


def process_dir(root, cur_dir, repo_ctx, force_git_dir, args, dir_queue, pending,
                task_q, done_keys, broken_repos, broken_lock):
    git_dir = None if args.disk_only else get_git_dir(cur_dir)
    if git_dir:
        repo_label = to_posix(os.path.relpath(cur_dir, root)) or "."
        rev = resolve_rev_fast(git_dir)
        if rev:
            task_q.put(("git_repo", repo_label, cur_dir, rev))
            return
        with broken_lock:
            broken_repos.append((repo_label, cur_dir))
        repo_ctx = cur_dir
        # git plumbing can't resolve this repo at all - the only real
        # content left is whatever's physically in .git (pack/idx/loose
        # objects). Hash it raw instead of skipping it like a healthy repo's
        # internals would be.
        force_git_dir = True
    elif args.disk_only and get_git_dir(cur_dir):
        repo_ctx = cur_dir

    try:
        entries = list(os.scandir(winlong(cur_dir)))
    except OSError as exc:
        print(f"warning: cannot list {cur_dir}: {exc}", file=sys.stderr)
        return

    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=args.follow_symlinks)
            is_symlink = entry.is_symlink()
        except OSError as exc:
            print(f"warning: cannot stat {entry.path}: {exc}", file=sys.stderr)
            continue

        if is_symlink and not args.follow_symlinks:
            continue

        if is_dir:
            if entry.name == ".git" and not (args.include_git_dir or force_git_dir):
                continue
            pending.inc()
            dir_queue.put((os.path.join(cur_dir, entry.name), repo_ctx, force_git_dir))
            continue

        abs_path = os.path.join(cur_dir, entry.name)
        if repo_ctx is not None:
            repo_label = to_posix(os.path.relpath(repo_ctx, root))
            rel_path = to_posix(os.path.relpath(abs_path, repo_ctx))
        else:
            repo_label = NO_REPO
            rel_path = to_posix(os.path.relpath(abs_path, root))
        if (repo_label, rel_path) in done_keys:
            continue
        task_q.put(("disk_file", repo_label, abs_path, rel_path))


def scanner_loop(root, args, dir_queue, pending, task_q, done_keys,
                 broken_repos, broken_lock):
    while True:
        try:
            cur_dir, repo_ctx, force_git_dir = dir_queue.get(timeout=0.2)
        except queue.Empty:
            if pending.done.is_set():
                return
            continue
        try:
            process_dir(root, cur_dir, repo_ctx, force_git_dir, args, dir_queue,
                        pending, task_q, done_keys, broken_repos, broken_lock)
        finally:
            pending.dec()


def load_done_keys(csv_path):
    done = set()
    if not os.path.exists(csv_path):
        return done
    with open(csv_path, "r", newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            repo, path = row.get("repo"), row.get("file_path")
            if repo is not None and path is not None:
                done.add((repo, path))
    return done


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="directory to scan")
    ap.add_argument("--out", required=True, help="CSV file to write")
    ap.add_argument("--algo", default="git",
                    choices=["git", "sha1", "sha256", "md5", "blake2b"],
                    help="hash algorithm (default: git = git's blob sha1, "
                         "comparable with git hash-object)")
    ap.add_argument("--workers", type=int, default=4,
                    help="hashing worker threads (default 4); each task is "
                         "either one repo (git-sourced) or one disk file")
    ap.add_argument("--scan-workers", type=int, default=8,
                    help="directory-discovery threads (default 8) - listing "
                         "and resolving repos is latency-bound, not CPU-bound, "
                         "so raise this on a slow/network drive")
    ap.add_argument("--disk-only", action="store_true",
                    help="ignore git entirely, hash whatever's literally on "
                         "disk (old/simple behavior; misses content in "
                         "repos that were never checked out)")
    ap.add_argument("--include-git-dir", action="store_true",
                    help="also hash files inside .git (off by default)")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="follow symlinks instead of skipping them")
    ap.add_argument("--resume", action="store_true",
                    help="skip (repo, file_path) pairs already present in --out "
                         "and append new rows to it")
    ap.add_argument("--save-every", type=int, default=2000,
                    help="flush/checkpoint the CSV to disk every N rows "
                         "(default 2000) - a crash or interrupt loses at "
                         "most this many rows")
    ap.add_argument("--quiet", action="store_true",
                    help="no progress loader / checkpoint messages")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        raise SystemExit(f"error: {args.root!r} is not a directory")

    done_keys = load_done_keys(args.out) if args.resume else set()
    if args.resume and done_keys:
        print(f"resume: {len(done_keys)} row(s) already in {args.out}, skipping them",
              file=sys.stderr)

    write_header = not (args.resume and os.path.exists(args.out))
    mode = "a" if (args.resume and os.path.exists(args.out)) else "w"

    task_q = queue.Queue(maxsize=2000)
    result_q = queue.Queue(maxsize=2000)
    broken_repos = []
    broken_lock = threading.Lock()

    root = os.path.abspath(args.root)
    dir_queue = queue.Queue()
    pending = PendingCounter()
    pending.inc()
    dir_queue.put((root, None, False))

    def closer():
        pending.done.wait()
        for _ in range(args.workers):
            task_q.put(SENTINEL)

    def worker():
        while True:
            item = task_q.get()
            if item is SENTINEL:
                result_q.put(SENTINEL)
                return
            kind = item[0]
            if kind == "disk_file":
                _, repo_label, abs_path, rel_path = item
                try:
                    digest, size = hash_file(abs_path, args.algo)
                    result_q.put((repo_label, rel_path, digest, size, ""))
                except OSError as exc:
                    result_q.put((repo_label, rel_path, "", "", str(exc)))
            elif kind == "git_repo":
                _, repo_label, repo_dir, rev = item
                try:
                    for row in git_repo_rows(repo_label, repo_dir, rev,
                                             args.algo, done_keys):
                        result_q.put(row)
                except Exception as exc:
                    result_q.put((repo_label, "", "", "",
                                  f"git repo processing failed: {exc}"))

    threads = [threading.Thread(target=scanner_loop, daemon=True, args=(
                   root, args, dir_queue, pending, task_q, done_keys,
                   broken_repos, broken_lock))
               for _ in range(args.scan_workers)]
    threads.append(threading.Thread(target=closer, daemon=True))
    threads += [threading.Thread(target=worker, daemon=True)
                for _ in range(args.workers)]
    for t in threads:
        t.start()

    count = errors = 0
    total_bytes = 0
    start = time.time()
    sentinels_seen = 0
    last_render = 0.0
    spinner = "|/-\\"

    def render(final=False):
        elapsed = time.time() - start
        gb = total_bytes / (1024 ** 3)
        rate = gb / elapsed * 3600 if elapsed else 0
        frame = "done" if final else spinner[count % len(spinner)]
        line = (f"\r  [{frame}] {count:,} rows | {gb:.2f} GB | "
                f"{errors} error(s) | {rate:.1f} GB/hr | {elapsed:,.0f}s")
        print(line.ljust(90), end="\n" if final else "", file=sys.stderr, flush=True)

    with open(args.out, mode, newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(["repo", "file_path", "hash", "size_bytes", "error"])
            fh.flush()

        while sentinels_seen < args.workers:
            item = result_q.get()
            if item is SENTINEL:
                sentinels_seen += 1
                continue
            repo_label, rel_path, digest, size, error = item
            writer.writerow([repo_label, rel_path, digest, size, error])
            count += 1
            if error:
                errors += 1
            else:
                total_bytes += size or 0

            if count % max(1, args.save_every) == 0:
                fh.flush()   # checkpoint: at most save_every rows lost on a crash

            now = time.time()
            if not args.quiet and (now - last_render) >= 0.2:
                render()
                last_render = now
        fh.flush()
        if not args.quiet:
            render(final=True)

    for t in threads:
        t.join()

    elapsed = time.time() - start
    print(f"done: {count:,} row(s), {total_bytes / (1024 ** 3):.2f} GB, "
          f"{errors} error(s), {elapsed:.0f}s -> {args.out}")

    if broken_repos:
        sidecar = args.out + ".needs_recovery.csv"
        with open(sidecar, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["repo", "path"])
            for repo_label, repo_dir in broken_repos:
                w.writerow([repo_label, repo_dir])
        print(f"\n{len(broken_repos)} repo(s) have no resolvable git history "
              f"(broken HEAD/refs, e.g. unborn HEAD with no valid ref) - see "
              f"{sidecar}. Their raw .git contents (pack/idx/loose objects) "
              f"were hashed above under their repo label since no named path "
              f"per blob is recoverable without a resolvable commit. Use "
              f"extract_from_pack.py (optionally with --from-repo pointing "
              f"at a healthy sibling) to recover real paths for these.")


if __name__ == "__main__":
    main()
