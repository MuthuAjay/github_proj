#!/usr/bin/env python3
"""
test_group4_2.py - archive_unpack.py on a throwaway archive: counts ->
per-repo extraction -> unpack, with today's copy read too. Covers dedup of
the same file across versions and archives, nesting (zip > tar.gz > zip),
a single .csv.gz, split archives (x.zip.001 + .002, numbered wiki
attachments, an incomplete set), 7z (when a 7z tool is found: SEVENZIP=... or 7zz/7z on
PATH), a password-protected zip, a damaged zip, a vendored archive, the
depth and size limits, resume, and pdf_page_merge.py running on the output.

    SEVENZIP=/path/to/7zz python3 test_group4_2.py
"""

import csv
import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import archive_unpack as au                                  # noqa: E402
from test_smoke import git, read_csv, run                     # noqa: E402
from test_extension_versions import put, unlink_refs          # noqa: E402
from test_group3 import HAVE_PDF, commit_on, make_pdf         # noqa: E402

SEVEN = au.sevenzip()
EXTS = ",".join(sorted(au.ARCHIVE_EXTS | {"001", "002", "003"}))
PDF = make_pdf(["Quarterly report", "Contact: jane@example.com"])


def zbytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def seven(tmp, name, files, fmt="7z", password=None):
    src = tempfile.mkdtemp(dir=tmp)
    for n, data in files:
        put(src, n, data)
    out = os.path.join(tmp, name)
    cmd = [SEVEN, "a", "-t" + fmt, out, "."] + (["-p" + password] if password else [])
    subprocess.run(cmd, cwd=src, check=True, capture_output=True)
    with open(out, "rb") as fh:
        return fh.read()


def export(version):
    contacts = ("name,email\nJane,jane@example.com\n" if version == 1 else
                "name,email\nJane,jane@example.com\nTom,tom@example.com\n")
    files = [("contacts.csv", contacts.encode()), ("report.pdf", PDF),
             ("img/logo.png", b"\x89PNG\x00logo"),
             ("nested.tar.gz", tgz([("notes.txt", b"call amy@example.com"),
                                    ("inner/deep.zip",
                                     zbytes([("x.json", b'{"k": "deep"}')]))]))]
    if version == 3:
        files.append(("today.txt", b"only in today's copy"))
    return zbytes(files)


def build(tmp):
    arch, act = os.path.join(tmp, "archive"), os.path.join(tmp, "active")
    a = os.path.join(arch, "org1", "repoA")
    os.makedirs(a)
    git(a, "init", "-q", "-b", "master")
    v1, v2, v3 = export(1), export(2), export(3)
    c1 = [("data/export.zip", v1),
          ("data/data.csv.gz", gzip.compress(b"id,phone\n1,+44 20 7946 0000\n")),
          ("node_modules/pkg/fixture.zip", zbytes([("f.txt", b"vendored")]))]
    commit_on(a, 1, "c1", c1)
    c2 = [("data/export.zip", v2),
          ("old/broken.zip", b"PK\x03\x04 this is not really a zip")]
    if SEVEN:
        c2 += [("backup/b.7z", seven(tmp, "b.7z", [("secret.txt", b"pw=hunter2")])),
               ("backup/locked.zip", seven(tmp, "l.zip", [("s.txt", b"x")],
                                           "zip", password="pw"))]
    commit_on(a, 2, "c2", c2)
    big = zbytes([("inner.txt", b"split zip: bob@example.com"),
                  ("more.csv", b"id\n" + b"7" * 3000)])
    cut = len(big) // 2
    num = zbytes([("att.txt", b"wiki attachment: eve@example.com")])
    commit_on(a, 3, "c3", [("data/export.zip", v3),
                           ("data/big.zip.001", big[:cut]),
                           ("data/big.zip.002", big[cut:]),
                           ("attachments/42/1111.001", num[:20]),
                           ("attachments/42/2222.002", num[20:]),
                           ("attachments/43/3333.002", b"a middle part only"),
                           ("env/.env.prod.br", b"# not brotli\nDB_PASSWORD=x\n")])
    unlink_refs(a)
    ra = os.path.join(act, "org1", "repoA")
    put(ra, "data/export.zip", v3)                   # today = v3
    put(ra, "data/data.csv.gz", c1[1][1])
    put(ra, "node_modules/pkg/fixture.zip", c1[2][1])
    batch = os.path.join(tmp, "B.csv")
    with open(batch, "w") as fh:
        fh.write("org,repo,tier,weight,listed_files\norg1,repoA,small,1,1\n")
    st, x = os.path.join(tmp, "counts"), os.path.join(tmp, "x")
    base = ["--batch", batch, "--repos-root", arch, "--active-root", act,
            "--out", st, "--extensions", EXTS]
    run("extension_versions.py", *base, "--binary-exts", EXTS)
    run("extension_versions.py", *base, "--identical")
    run("extract_versions.py", "--state", st, "--repos-root", arch, "--out", x,
        "--batch", batch, "--min-free-disk-gb", "0", "--extensions", EXTS,
        "--per-repo", "--skip-vendored")
    return act, batch, st, x


class Unpack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="g42_")
        cls.act, cls.batch, cls.st, cls.x = build(cls.tmp)
        cls.out = os.path.join(cls.tmp, "unpacked")
        cls.base = ["--in", cls.x, "--counts", cls.st, "--active-root", cls.act,
                    "--batch", cls.batch, "--min-free-disk-gb", "0", "--workers", "2",
                    "--extensions", EXTS]
        cls.p = run("archive_unpack.py", *cls.base, "--out", cls.out)
        sd = os.path.join(cls.out, "_state", "org1", "repoA")
        cls.rows = read_csv(os.path.join(sd, "manifest.csv"))
        cls.arcs = read_csv(os.path.join(sd, "archives.csv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def of(self, path):
        return [r for r in self.rows if r["path"] == path]

    def test_versions_today_included_and_dedup(self):
        c = self.of("data/export.zip/contacts.csv")
        # v1, v2 from history, v3 (today, not in the extraction) read from
        # the active copy; v3's contacts = v2's -> deduped
        self.assertEqual([(r["archive_source"], r["action"]) for r in c],
                         [("history", "written"), ("history", "written"),
                          ("today", "deduped")])
        self.assertEqual(c[2]["first_date"], "today")
        pdf = self.of("data/export.zip/report.pdf")
        self.assertEqual([r["action"] for r in pdf], ["written", "deduped", "deduped"])
        self.assertEqual(len({r["stored_as"] for r in pdf}), 1)
        self.assertEqual(self.of("data/export.zip/today.txt")[0]["action"], "written")

    def test_stored_files_are_the_contents(self):
        import hashlib
        for r in self.rows:
            if r["action"] == "written":
                with open(os.path.join(self.out, r["stored_as"]), "rb") as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), r["blob"])
                self.assertTrue(r["stored_as"].startswith(
                    "files/%s/org1/repoA/" % au.safe_type(r["ext"])))

    def test_nesting_and_single_gz(self):
        deep = self.of("data/export.zip/nested.tar.gz/inner/deep.zip/x.json")
        self.assertEqual(deep[0]["action"], "written")
        self.assertEqual(deep[0]["depth"], "3")
        self.assertEqual(self.of("data/export.zip/nested.tar.gz")[0]["action"],
                         "opened_nested")
        self.assertEqual(self.of("data/export.zip/nested.tar.gz/notes.txt")[0]
                         ["route"], "group1_text")
        gz = self.of("data/data.csv.gz/data.csv")
        self.assertEqual([r["action"] for r in gz], ["written"])   # today = same

    def test_vendored_damaged_listed(self):
        v = self.of("node_modules/pkg/fixture.zip/")
        self.assertTrue(v and all(r["action"] == "skipped_vendored" for r in v))
        st = {a["archive_path"]: a["status"] for a in self.arcs}
        self.assertTrue(st["old/broken.zip"].startswith("damaged"))
        # a .br that is plain text: opened by name, nothing inside readable
        # -> not "ok", so it is in problems.csv
        self.assertTrue(st["env/.env.prod.br"].startswith(("damaged", "no_tool")),
                        st["env/.env.prod.br"])
        probs = read_csv(os.path.join(self.out, "stats", "problems.csv"))
        self.assertIn("old/broken.zip", {p["archive_path"] for p in probs})

    def test_split_sets_joined(self):
        self.assertEqual(self.of("data/big.zip/inner.txt")[0]["action"], "written")
        self.assertEqual(self.of("attachments/42/1111.001-002/att.txt")[0]["action"],
                         "written")
        st = {a["archive_path"]: (a["status"], a["format"]) for a in self.arcs}
        self.assertEqual(st["data/big.zip.001"], ("split_part", "split"))
        self.assertEqual(st["data/big.zip.002"], ("split_part", "split"))
        self.assertEqual(st["data/big.zip"], ("ok", "split:zip"))
        self.assertEqual(st["attachments/43/3333.002"][0], "incomplete_set")
        self.assertEqual(self.of("attachments/43/3333.002/")[0]["action"],
                         "incomplete_set")
        by = {r["archive_type"]: r for r in read_csv(os.path.join(
            self.out, "stats", "by_archive_type.csv"))}
        self.assertEqual(by["001"]["versions"], "2")          # the parts, not the sets
        self.assertEqual(by["001"]["files_inside"], "3")      # both sets' files

    @unittest.skipUnless(SEVEN, "no 7z tool (set SEVENZIP)")
    def test_7z_and_password(self):
        self.assertEqual(self.of("backup/b.7z/secret.txt")[0]["action"], "written")
        locked = self.of("backup/locked.zip/s.txt")
        self.assertEqual(locked[0]["action"], "encrypted")

    def test_stats(self):
        by = {r["type"]: r for r in read_csv(os.path.join(self.out, "stats",
                                                          "by_type.csv"))}
        self.assertEqual(by["csv"]["route"], "group1_text")
        self.assertEqual(by["pdf"]["route"], "group3")
        self.assertEqual(by["png"]["route"], "group2_binary")
        self.assertEqual((by["pdf"]["occurrences"], by["pdf"]["distinct"]), ("3", "1"))
        self.assertEqual(by["csv"]["distinct"], "4")    # 2 contacts, data.csv, more.csv
        self.assertIn("END", self.p.stdout)

    def test_resume(self):
        p = run("archive_unpack.py", *self.base, "--out", self.out)
        self.assertIn("0 repo(s) to do", p.stdout)

    def test_limits(self):
        out = os.path.join(self.tmp, "deep2")
        run("archive_unpack.py", *self.base, "--out", out, "--max-depth", "2")
        rows = read_csv(os.path.join(out, "_state", "org1", "repoA", "manifest.csv"))
        d = [r for r in rows if r["path"] == "data/export.zip/nested.tar.gz/inner/deep.zip"]
        self.assertEqual(d[0]["action"], "written")     # kept as a file, not opened
        out = os.path.join(self.tmp, "small")
        run("archive_unpack.py", *self.base, "--out", out,
            "--max-archive-bytes", "40", "--ratio-floor", "1")
        arcs = read_csv(os.path.join(out, "_state", "org1", "repoA", "archives.csv"))
        self.assertIn("limit_archive_bytes", {a["status"] for a in arcs})

    def test_report(self):
        p = run("archive_unpack_report.py", self.out)
        self.assertIn("repos 1 |", p.stdout)
        rows = [r for r in read_csv(os.path.join(self.out, "stats",
                                                 "archives_per_repo.csv")) if r["org"] == "org1"]
        by = {r["type"]: r for r in read_csv(os.path.join(self.out, "stats", "by_type.csv"))}
        # the report and archive_unpack.py's own stats agree
        self.assertEqual(rows[0]["files_inside"], by["total"]["occurrences"])
        self.assertEqual(rows[0]["distinct_files_written"], by["total"]["distinct"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "stats", "report.md")))
        bad = run("archive_unpack_report.py", self.tmp, check=False)
        self.assertNotEqual(bad.returncode, 0)

    @unittest.skipUnless(HAVE_PDF, "pip install pypdfium2 pypdf")
    def test_pdf_page_merge_runs_on_it(self):
        out = os.path.join(self.tmp, "pdfm")
        run("pdf_page_merge.py", "--in", self.out, "--out", out,
            "--batch", self.batch)
        merged = os.path.join(out, "org1", "repoA", "data", "export.zip", "report.pdf")
        import pypdf
        self.assertEqual(len(pypdf.PdfReader(merged).pages), 2)


class Formats(unittest.TestCase):
    def test_detect(self):
        tmp = tempfile.mkdtemp(prefix="g42d_")
        try:
            for name, data, fmt in (("a", zbytes([("x", b"1")]), "zip"),
                                    ("b", gzip.compress(b"x"), "gz"),
                                    ("c", tgz([("x", b"1")]), "gz"),
                                    ("d", b"just text", "")):
                p = os.path.join(tmp, name)
                with open(p, "wb") as fh:
                    fh.write(data)
                self.assertEqual(au.detect(p), fmt, name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zst_lz4_single_files(self):
        tmp = tempfile.mkdtemp(prefix="g42z_")
        try:
            cfg = type("C", (), {"sevenzip": None})()
            for mod, fmt, comp in (("zstandard", "zst",
                                    lambda d: __import__("zstandard").ZstdCompressor().compress(d)),
                                   ("lz4.frame", "lz4",
                                    lambda d: __import__("lz4.frame").frame.compress(d))):
                try:
                    __import__(mod)
                except ImportError:
                    continue
                p = os.path.join(tmp, "d.csv." + fmt)
                with open(p, "wb") as fh:
                    fh.write(comp(b"a,b\n1,jane@example.com\n"))
                got_fmt, members = au.open_archive(p, cfg)
                name, size, kind, opener = next(members)
                self.assertEqual((got_fmt, kind), (fmt, "single"))
                with opener() as fh:
                    self.assertIn(b"jane@example.com", fh.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_brotli_single_file(self):
        try:
            import brotli
        except ImportError:
            self.skipTest("pip install brotli")
        tmp = tempfile.mkdtemp(prefix="g42br_")
        try:
            data = b"DB_PASSWORD=hunter2\n" + os.urandom(200000).hex().encode()
            p = os.path.join(tmp, "x")                # stored by blob: no name
            with open(p, "wb") as fh:
                fh.write(brotli.compress(data))
            cfg = type("C", (), {"sevenzip": None})()
            self.assertEqual(au.detect(p), "")        # no magic bytes
            fmt, members = au.open_archive(p, cfg, "br")
            name, size, kind, opener = next(members)
            self.assertEqual((fmt, kind), ("br", "single"))
            with opener() as fh:
                got = b"".join(iter(lambda: fh.read(1 << 20), b""))
            self.assertEqual(got, data)
            with open(p, "wb") as fh:                 # cut short -> an error
                fh.write(brotli.compress(data)[:5000])
            fmt, members = au.open_archive(p, cfg, "br")
            with self.assertRaises(IOError):
                with next(members)[3]() as fh:
                    while fh.read(1 << 20):
                        pass
            with open(p, "wb") as fh:                 # not brotli at all
                fh.write(b"version https://git-lfs.github.com/spec/v1\n" * 50)
            fmt, members = au.open_archive(p, cfg, "br")
            with self.assertRaises(IOError):          # a file error, not the repo's
                with next(members)[3]() as fh:
                    while fh.read(1 << 20):
                        pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_vhd_detected(self):
        tmp = tempfile.mkdtemp(prefix="g42v_")
        try:
            fixed = os.path.join(tmp, "f")             # footer at the end only
            with open(fixed, "wb") as fh:
                fh.write(b"\x00" * 4096 + b"conectix" + b"\x00" * 504)
            dyn = os.path.join(tmp, "d")               # copy at the start too
            with open(dyn, "wb") as fh:
                fh.write(b"conectix" + b"\x00" * 2000)
            old = os.path.join(tmp, "o")               # 511-byte footer
            with open(old, "wb") as fh:
                fh.write(b"\x00" * 4096 + b"conectix" + b"\x00" * 503)
            self.assertEqual(au.detect(old), "vhd")
            vx = os.path.join(tmp, "x")
            with open(vx, "wb") as fh:
                fh.write(b"vhdxfile" + b"\x00" * 100)
            self.assertEqual([au.detect(f) for f in (fixed, dyn, vx)],
                             ["vhd", "vhd", "vhdx"])
            with self.assertRaises(LookupError):      # 7z opens them
                au.open_archive(fixed, type("C", (), {"sevenzip": None})())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_split_part_names(self):
        others, sets = au.split_sets([{"path": p} for p in (
            "a/x.7z.001", "a/x.7z.002", "w/1/11.001", "w/1/22.002", "b/y.zip")])
        self.assertEqual([o["path"] for o in others], ["b/y.zip"])
        self.assertEqual({k: sorted(v) for k, v in sets.items()},
                         {"a/x.7z": [1, 2], "w/1/#numbered": [1, 2]})

    def test_no_7z_tool(self):
        tmp = tempfile.mkdtemp(prefix="g42n_")
        try:
            p = os.path.join(tmp, "x.7z")
            with open(p, "wb") as fh:
                fh.write(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 40)
            with self.assertRaises(LookupError):
                au.open_archive(p, type("C", (), {"sevenzip": None})())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clean_name_never_escapes(self):
        self.assertEqual(au.clean_name("../../etc/passwd"), "../../etc/passwd")
        self.assertEqual(au.clean_name("/abs/x"), "abs/x")
        self.assertEqual(au.clean_name("C:\\win\\x.txt"), "win/x.txt")
        # names are only recorded; files are stored by hash, so even a
        # '..' name cannot write outside files/

    def test_routes(self):
        self.assertEqual(au.route_of("xlsx"), "group2_binary")
        self.assertEqual(au.route_of("zip"), "archive")
        self.assertEqual(au.route_of("jar"), "package_or_asset")
        self.assertEqual(au.route_of(""), "no_extension")
        self.assertEqual(au.route_of("sqlite"), "group4_1_db")
        self.assertEqual(au.route_of("scss"), "group4_5_text")

    def test_unitypackage_paths(self):
        g1, g2 = "0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"
        data = tgz([(g1 + "/pathname", b"Assets/Scripts/Player.cs\n00\n"),
                    (g1 + "/asset", b"class Player {}\n"),
                    (g1 + "/asset.meta", b"guid: 0123\n"),
                    (g2 + "/pathname", b"Assets/Data/users.csv\n"),
                    (g2 + "/asset", b"name,email\njane,jane@example.com\n"),
                    (g2 + "/preview.png", b"PNG")])
        tmp = tempfile.mkdtemp(prefix="g42u_")
        try:
            p = os.path.join(tmp, "x.unitypackage")
            with open(p, "wb") as fh:
                fh.write(data)
            fmt, members = au.open_archive(p, type("C", (), {"sevenzip": None})())
            got = {}
            for name, size, kind, opener in members:
                with opener() as fh:
                    got[name] = fh.read()
            self.assertEqual(fmt, "unitypackage")
            self.assertEqual(sorted(got), ["Assets/Data/users.csv",
                                           "Assets/Data/users.csv.preview.png",
                                           "Assets/Scripts/Player.cs",
                                           "Assets/Scripts/Player.cs.meta"])
            self.assertIn(b"jane@example.com", got["Assets/Data/users.csv"])
            self.assertEqual(au.route_of("unitypackage"), "archive")
            # an ordinary tar.gz keeps its own names
            q = os.path.join(tmp, "y.tgz")
            with open(q, "wb") as fh:
                fh.write(tgz([("docs/a.txt", b"1")]))
            fmt, members = au.open_archive(q, type("C", (), {"sevenzip": None})())
            self.assertEqual((fmt, [m[0] for m in members]), ("tar.gz", ["docs/a.txt"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bundles_recognised_not_opened(self):
        tmp = tempfile.mkdtemp(prefix="g42b_")
        try:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            git(repo, "init", "-q")
            put(repo, "a.txt", "hello\n")
            git(repo, "add", "-A")
            git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
            gb = os.path.join(tmp, "repo.bundle")
            git(repo, "bundle", "create", "-q", gb, "--all")
            ub = os.path.join(tmp, "asset.bundle")
            with open(ub, "wb") as fh:
                fh.write(b"UnityFS\x00\x00\x00\x00\x07" + b"\x00" * 40)
            cfg = type("C", (), {"sevenzip": None})()
            for fp, kind in ((gb, "git_bundle"), (ub, "unity_assetbundle")):
                self.assertEqual(au.detect(fp), kind)
                with self.assertRaises(ValueError) as cm:
                    au.open_archive(fp, cfg)
                self.assertEqual(str(cm.exception), kind)
            self.assertEqual(au.route_of("bundle"), "package_or_asset")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
