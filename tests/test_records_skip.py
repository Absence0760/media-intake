#!/usr/bin/env python3
"""The private records folder (records.root, $PRIVATE_RECORDS, default ~/Documents/Records) is never entered by any
sweep: erase, forget, sweep-erased and verify-gone neither hash nor delete a copy there, from HOME or a drive.

  python3 -I -m unittest discover -s tests -p test_records_skip.py

Synthetic only: random bytes stand in for media, in a throwaway HOME (tests/support.py) and a plain folder as the
drive. RecordsSweepCLI runs the real intake.py; RecordsSweepInProcess patches intake's HOME and records folder and
records every path sha256 is asked for, so "never hashed" is checked, not only "still there".
"""
import collections, contextlib, hashlib, io, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import config  # noqa: E402
import intake  # noqa: E402

INTAKE = os.path.join(support.PKG, "intake.py")
PY = sys.executable


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


class RecordsSweepCLI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-records-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("PRIVATE_RECORDS", None)
        self.data = os.urandom(4096)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def H(self, *p):
        return os.path.join(self.home, *p)

    def intake(self, *args, code=0, env=None):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=env or self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def log(self, batch="2026-11-16-b"):
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"2026-11-16 10:00 {batch} sha256:{self.sha} size:{len(self.data)}\n")
        return batch

    def test_default_records_folder_survives_sweep_erased_and_verify_gone(self):
        """No config, no $PRIVATE_RECORDS: ~/Documents/Records is protected, in any letter case."""
        batch = self.log()
        kept = [write(self.H("Documents/Records/00 Inbox/scan.jpg"), self.data),
                write(self.H("Documents/Records/2026/Receipts/scan.jpg"), self.data),
                write(self.H("Documents/Records/2026/Receipts/scan.jpg.xmp"), b"<x/>")]
        other = write(self.H("Downloads/scan.jpg"), self.data)
        out = self.intake("sweep-erased", batch)
        self.assertIn("dry run: 1 copies of 1 erased items in HOME", out)
        out = self.intake("sweep-erased", batch, "--yes")
        self.assertIn("sweep-erased: 1 copies deleted in HOME", out)
        self.assertFalse(os.path.exists(other))
        for p in kept:
            self.assertTrue(os.path.isfile(p), p)
        self.intake("verify-gone", batch)  # the records copies are not "left": verify-gone never looks there

    def test_forget_and_its_erase_leave_the_records_copy(self):
        photo = write(os.path.join(self.lib, "2026/2026-11/IMG_0001.jpg"), self.data)
        filed = write(self.H("Documents/Records/2026/Receipts/IMG_0001.jpg"), self.data)
        stray = write(self.H("Downloads/IMG_0001.jpg"), self.data)
        out = self.intake("forget", photo, "--yes")
        self.assertFalse(os.path.exists(photo))
        self.assertFalse(os.path.exists(stray), "every other copy in HOME still goes")
        self.assertTrue(os.path.isfile(filed), "the records copy stays")
        self.assertIn("your private records folder", out)  # the coverage report says it was not searched
        self.assertIn("never searched", out)

    def test_private_records_env_through_a_symlink_and_another_case(self):
        """$PRIVATE_RECORDS wins over the default; a symlinked spelling is resolved, and a case-insensitive disk's
        other spelling of the folder is skipped too."""
        real = self.H("Archive/Paperwork")
        filed = write(os.path.join(real, "2026/scan.jpg"), self.data)
        os.symlink(real, self.H("records-link"))
        upper = write(self.H("Archive/PAPERWORK/scan.jpg"), self.data)  # a separate folder here; one on exFAT
        default = write(self.H("Documents/Records/scan.jpg"), self.data)  # no longer the records folder
        batch = self.log()
        env = dict(self.env, PRIVATE_RECORDS=self.H("records-link"))
        out = self.intake("sweep-erased", batch, "--yes", env=env)
        self.assertIn("sweep-erased: 1 copies deleted in HOME", out)
        self.assertTrue(os.path.isfile(filed))
        self.assertTrue(os.path.isfile(upper))
        self.assertFalse(os.path.exists(default))

    def test_a_records_folder_on_the_drive_is_skipped_by_the_drive_sweep(self):
        drive = os.path.join(self.tmp, "drive")
        on_drive = write(os.path.join(drive, "Records/scan.jpg"), self.data)
        mirror = write(os.path.join(drive, "media-intake-backup/library/scan.jpg"), self.data)
        batch = self.log()
        env = dict(self.env, PRIVATE_RECORDS=os.path.join(drive, "Records"))
        out = self.intake("sweep-erased", batch, "--drive", drive, "--yes", "--no-wipe-free", env=env)
        self.assertIn("sweep-erased: 1 copies deleted", out)
        self.assertTrue(os.path.isfile(on_drive))
        self.assertFalse(os.path.exists(mirror))


class RecordsSweepInProcess(unittest.TestCase):
    """intake.sweep (erase's, forget's, sweep-erased's and verify-gone's walk) and drive_purge with sha256 recorded:
    no path under the records folder is ever hashed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-records-ip-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.records = os.path.join(self.home, "Documents/Records")
        self.data = os.urandom(2048)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.inside = [write(os.path.join(self.records, n), self.data) for n in ("a.jpg", "deep/b.jpg")]
        self.outside = write(os.path.join(self.home, "Downloads/c.jpg"), self.data)
        self.hashed = []
        real = intake.sha256

        def recording(p):
            self.hashed.append(os.path.abspath(p))
            return real(p)
        for name, value in (("HOME", self.home), ("RECORDS", self.records),
                            ("RECORDS_SKIP", intake.records_skip(self.records, (self.home,))),
                            ("sha256", recording)):
            p = mock.patch.object(intake, name, value)
            p.start()
            self.addCleanup(p.stop)

    def assert_never_hashed(self):
        under = [p for p in self.hashed if p.startswith(self.records + os.sep)]
        self.assertEqual(under, [], "a file under the records folder was hashed")

    def test_sweep_neither_hashes_nor_deletes_there(self):
        for delete in (False, True):  # verify-gone's look, then erase's delete
            with self.subTest(delete=delete), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                found, paths, _, _ = intake.sweep({self.sha: len(self.data)}, delete=delete,
                                                  stats=collections.Counter())
            self.assertEqual(paths, [self.outside])
            self.assert_never_hashed()
        for p in self.inside:
            self.assertTrue(os.path.isfile(p), p)

    def test_drive_purge_and_a_walk_from_inside_skip_it(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            n = intake.drive_purge(self.home, {self.sha: (len(self.data), "x")})
        self.assertEqual(n, 1)  # the Downloads copy; the records ones stay
        self.assert_never_hashed()
        self.assertEqual(list(intake.walk_files(self.records)), [])
        self.assertEqual(list(intake.walk_files(os.path.join(self.records, "deep"))), [])

    def test_a_root_that_is_a_symlink_into_it_yields_nothing(self):
        """A --drive path or a configured folder that is a link into the records folder: never walked."""
        for target in (self.records, os.path.join(self.records, "deep")):
            link = os.path.join(self.tmp, "link-" + os.path.basename(target))
            os.symlink(target, link)
            self.assertEqual(list(intake.walk_files(link)), [], target)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(intake.drive_purge(link, {self.sha: (len(self.data), "x")}), 0)
        self.assert_never_hashed()
        self.assertEqual(sorted(self.hashed), [])
        for p in self.inside:
            self.assertTrue(os.path.isfile(p), p)


class RecordsConfig(unittest.TestCase):

    def load(self, text, **env):
        d = tempfile.mkdtemp(prefix="intake-records-cfg-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "config.toml")
        with open(p, "w") as f:
            f.write(text)
        with mock.patch.dict(os.environ, env, clear=False):
            if "PRIVATE_RECORDS" not in env:
                os.environ.pop("PRIVATE_RECORDS", None)
            return config.load(p)

    def test_default_key_and_env(self):
        self.assertEqual(self.load("")["records_root"], os.path.join(config.HOME, "Documents", "Records"))
        self.assertEqual(self.load('[records]\nroot = "/srv/paper"\n')["records_root"], "/srv/paper")
        self.assertEqual(self.load('[records]\nroot = "/srv/paper"\n', PRIVATE_RECORDS="/srv/env")["records_root"],
                         "/srv/env")

    def test_home_or_above_and_unknown_keys_refused(self):
        for root in (config.HOME, os.path.dirname(config.HOME), "/"):
            with self.subTest(root=root), self.assertRaises(config.ConfigError):
                self.load(f'[records]\nroot = "{root}"\n')
        with self.assertRaises(config.ConfigError):
            self.load('[records]\ninbox = "x"\n')

    def test_overlap_with_media_intake_folders_refused(self):
        """No sweep enters the records folder, so the library, videos, state or a project folder inside it (or
        holding it) would never be swept: refused."""
        for root, env in (("/srv/lib", {"PHOTO_LIBRARY": "/srv/lib/photos"}),
                          ("/srv/lib/photos/records", {"PHOTO_LIBRARY": "/srv/lib/photos"}),
                          ("/srv/vids", {"PERSONAL_FOOTAGE": "/srv/vids"}),
                          ("/srv/st/records", {"MEDIA_INTAKE_STATE": "/srv/st"})):
            with self.subTest(root=root), self.assertRaises(config.ConfigError) as e:
                self.load(f'[records]\nroot = "{root}"\n', **env)
            self.assertIn("overlap", str(e.exception))
        self.assertEqual(self.load('[records]\nroot = "/srv/paper"\n', PHOTO_LIBRARY="/srv/paperwork")["records_root"],
                         "/srv/paper")  # a name prefix is not an overlap


if __name__ == "__main__":
    unittest.main()
