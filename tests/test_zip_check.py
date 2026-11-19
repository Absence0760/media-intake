#!/usr/bin/env python3
"""Tests for zip_check.py (`media-intake zip-check`) on synthetic zips in a throwaway HOME and state.

  python3 -I -m unittest discover -s tests -p test_zip_check.py

The "media" are a few bytes with photo, video and document extensions: zip-check only hashes them.
It runs as `cli.py zip-check` in a subprocess, so intake.py reads this test's state folder.
"""
import hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

support.check_hermetic()
CLI = os.path.join(support.PKG, "cli.py")
PY = sys.executable


def sha(b):
    return hashlib.sha256(b).hexdigest()


def put(base, rel, data):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


class ZipCheck(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="zip-check-")
        self.addCleanup(shutil.rmtree, self.t)
        self.home = os.path.join(self.t, "home")
        self.state = os.path.join(self.home, ".local", "share", "media-intake")
        self.exports = os.path.join(self.t, "exports")
        self.docs = os.path.join(self.home, "Documents", "kept")
        os.makedirs(self.state, mode=0o700)
        lib = os.path.join(self.home, "Pictures", "library", "2024", "2024-05", "IMG_0001.jpg")
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({lib: {"sha": sha(b"in the library"), "size": 14}}, f)
        with open(os.path.join(self.state, "trashed.json"), "w") as f:
            json.dump({sha(b"trashed before"): 14}, f)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-01 10:00:00 b1 sha256:{sha(b'erased before')} size:13 IMG_0005.jpg\n"
                    f"2026-10-01 10:00:00 b0 sha256:{sha(b'erased long ago')}\n"  # an older line: no size
                    '2026-11-01 10:00:01 b1 coverage {"home": 1}\n')
        put(self.docs, "letter.pdf", b"a kept document")
        self.members = {
            "Takeout/Google Photos/2024/IMG_0001.jpg": b"in the library",
            "Takeout/Google Photos/2024/IMG_0001.jpg.supplemental-metadata.json": b'{"title": "x"}',
            "Takeout/Google Photos/2024/IMG_0002.jpg": b"trashed before",
            "Takeout/Google Photos/2024/IMG_0003.jpg": b"erased before",
            "Takeout/Google Photos/2024/IMG_0004.HEIC": b"erased long ago",
            "Takeout/Google Photos/2024/VID_0005.mp4": b"a new video",
            "Takeout/Drive/letter.pdf": b"a kept document",
            "Takeout/Drive/report.docx": b"a new document",
            "Takeout/Drive/notes.bin": b"something else",
        }
        z = os.path.join(self.exports, "google", "takeout-001.zip")
        os.makedirs(os.path.dirname(z))
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Takeout/", b"")
            for name, data in self.members.items():
                zf.writestr(name, data)
        put(self.exports, "google/takeout-002.zip", b"not a zip at all")
        self.before = self.snapshot(self.exports)

    def snapshot(self, root):
        out = {}
        for r, _, fs in os.walk(root):
            for f in fs:
                p = os.path.join(r, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = sha(fh.read())
        return out

    def run_cli(self, *args, code=0):
        env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state)
        r = subprocess.run([PY, "-I", CLI, "zip-check", *args], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r

    def totals(self, out):
        line = next(x for x in out.splitlines() if x.startswith("TOTAL: "))
        return {k: int(v) for k, v in re.findall(r"([a-z][a-z -]*?) (\d+) \(", line[len("TOTAL: "):])}

    def test_classifies_members_by_checksum_and_prints_counts_only(self):
        out_json = os.path.join(self.t, "zip-check.json")
        r = self.run_cli(self.exports, "--known", self.docs, "--out", out_json)
        self.assertEqual(self.totals(r.stdout), {
            "dir": 1, "erased": 2, "json": 1, "known": 1, "library": 1, "new-doc": 1, "new-media": 1,
            "new-other": 1, "trashed": 1, "unreadable zip": 1})
        self.assertIn("google: ", r.stdout)
        self.assertIn("[1/2] checked", r.stdout)
        for name in self.members:  # counts only: never a member name
            self.assertNotIn(os.path.basename(name), r.stdout + r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(out_json).st_mode), 0o600)
        with open(out_json) as f:
            detail = json.load(f)
        z = detail[os.path.join("google", "takeout-001.zip")]
        self.assertEqual(z["Takeout/Google Photos/2024/IMG_0003.jpg"], "erased")
        self.assertEqual(z["Takeout/Google Photos/2024/IMG_0004.HEIC"], "erased")
        self.assertEqual(z["Takeout/Drive/letter.pdf"], "known")
        self.assertEqual(z["Takeout/Google Photos/2024/VID_0005.mp4"], "new-media")
        self.assertEqual(detail[os.path.join("google", "takeout-002.zip")], {})
        self.assertEqual(self.snapshot(self.exports), self.before, "the zips and their folder are never written")
        cache = os.path.join(self.state, "zipcheck", "sha-cache.json")
        self.assertEqual(stat.S_IMODE(os.stat(cache).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(cache)).st_mode), 0o700)

    def test_without_known_the_document_is_new(self):
        r = self.run_cli(self.exports)
        self.assertEqual(self.totals(r.stdout)["new-doc"], 2)
        self.assertNotIn("known", self.totals(r.stdout))
        self.assertFalse(os.path.exists(os.path.join(self.state, "zipcheck")), "no cache without --known")

    def test_an_unreadable_member_is_counted(self):
        z = os.path.join(self.exports, "other", "bad-crc.zip")
        os.makedirs(os.path.dirname(z))
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("a.jpg", b"0123456789" * 10)
        with open(z, "r+b") as f:  # corrupt the stored bytes, so the CRC check fails as they are read
            data = f.read()
            i = data.index(b"0123456789")
            f.seek(i)
            f.write(b"X")
        r = self.run_cli(self.exports)
        self.assertIn("other: unreadable member 1", r.stdout)

    def test_refusals(self):
        r = self.run_cli(os.path.join(self.t, "nowhere"), code=1)
        self.assertIn("not a folder", r.stderr)
        r = self.run_cli(self.exports, "--known", os.path.join(self.t, "nowhere"), code=1)
        self.assertIn("--known", r.stderr)
        r = self.run_cli(self.exports, "--out", os.path.join(self.exports, "x.json"), code=1)
        self.assertIn("inside ROOT", r.stderr)
        self.assertEqual(self.snapshot(self.exports), self.before)

    def test_no_zips(self):
        empty = os.path.join(self.t, "empty")
        os.makedirs(empty)
        r = self.run_cli(empty)
        self.assertIn("TOTAL: no zip files under", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
