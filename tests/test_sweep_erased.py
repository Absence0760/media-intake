#!/usr/bin/env python3
"""`media-intake sweep-erased`: copies of erased items that an erase could not reach (a backup drive that was
unplugged at erase time) are found later by size + sha256 from the erase log, and deleted only with --yes.

  python3 -I -m unittest discover -s tests -p test_sweep_erased.py

Synthetic only: random bytes stand in for media, in a throwaway HOME and a plain folder as the drive. gio and
localsearch are off (tests/support.py). --no-wipe-free everywhere a drive copy is deleted: wipe-free needs
findmnt (Linux) and has its own tests.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import intake  # noqa: E402  (LOCKED, trash_info, where)

INTAKE = os.path.join(support.PKG, "intake.py")
PY = sys.executable


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def read(p):
    with open(p) as f:
        return f.read()


class SweepErased(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-sweep-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.drive = os.path.join(self.tmp, "drive")
        os.makedirs(self.drive)
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.data = os.urandom(4096)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.batch = "2026-11-16-unplugged"
        self.log(self.batch, self.sha, len(self.data))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def H(self, *p):
        return os.path.join(self.home, *p)

    def D(self, *p):
        return os.path.join(self.drive, *p)

    def log(self, batch, sha, size):
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"2026-11-16 10:00 {batch} sha256:{sha} size:{size}\n")

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        self.stderr = r.stderr
        return r.stdout

    def left_on_drive(self):
        """Copies a drive unplugged at erase time still holds: a backup folder, the freedesktop Trash with its
        .trashinfo (it names the original path), a .Trash/<uid> Trash, macOS's .Trashes, and a sidecar."""
        left = [write(self.D("media-intake-backup/library/2026/2026-11/clip.mp4"), self.data),
                write(self.D(".Trash-1000/files/clip.mp4"), self.data),
                write(self.D(".Trash/1000/files/clip-2.mp4"), self.data),
                write(self.D(".Trashes/501/clip.mp4"), self.data)]
        extra = [write(left[0] + ".xmp", b"<x/>"),
                 write(self.D(".Trash-1000/info/clip.mp4.trashinfo"), b"[Trash Info]\n"),
                 write(self.D(".Trash/1000/info/clip-2.mp4.trashinfo"), b"[Trash Info]\n")]
        return left, extra

    def test_dry_run_counts_then_yes_deletes_and_verifies(self):
        left, extra = self.left_on_drive()
        home_copy = write(self.H("Downloads/IMG-1.mp4"), self.data)
        other = write(self.D("media-intake-backup/library/2026/2026-11/other.mp4"), os.urandom(4096))
        same_size = write(self.D("media-intake-backup/library/2026/2026-11/same-size.mp4"), os.urandom(4096))
        out = self.intake("sweep-erased", self.batch, "--drive", self.drive)
        self.assertIn("dry run: 5 copies of 1 erased items in HOME and the drive", out)
        self.assertRegex(out, r"\n  drive:Trash +3\n")
        self.assertRegex(out, r"\n  drive:media-intake-backup +1\n")
        self.assertRegex(out, r"\n  Downloads +1\n")
        self.assertIn("nothing deleted (--yes deletes them)", out)
        for p in left + extra + [home_copy]:
            self.assertTrue(os.path.exists(p), "the dry run deletes nothing")
        for name in ("clip", "IMG-1", self.drive, self.home):
            self.assertNotIn(name, out, "counts only, never a file name")

        out = self.intake("sweep-erased", self.batch, "--drive", self.drive, "--yes", "--no-wipe-free")
        for p in left + extra + [home_copy]:
            self.assertFalse(os.path.lexists(p), p)
        for p in (other, same_size):
            self.assertTrue(os.path.isfile(p), "other content, same size included, stays")
        self.assertIn("sweep-erased: 5 copies deleted in HOME and the drive", out)
        self.assertRegex(out, r"\n  Trash entries +2\n")
        self.assertRegex(out, r"\n  sidecars \(\.xmp\) +1\n")
        self.assertIn("verify-gone: no copy of 1 erased items in HOME or on the drive", out)
        for name in ("clip", "IMG-1", self.home):
            self.assertNotIn(name, out, "counts only, never a file name")
        self.assertNotIn("drive free space", out, "--no-wipe-free")
        self.intake("verify-gone", self.batch, "--drive", self.drive)

    def test_drive_only_leaves_home_alone(self):
        left, _ = self.left_on_drive()
        home_copy = write(self.H("Downloads/IMG-1.mp4"), self.data)
        out = self.intake("sweep-erased", "--drive", self.drive, "--drive-only", "--yes", "--no-wipe-free")
        self.assertIn("sweep-erased: 4 copies deleted in the drive", out)
        self.assertIn("verify-gone: no copy of 1 erased items on the drive", out)
        self.assertNotIn("not swept: toolchains", out, "HOME was not swept")
        self.assertTrue(os.path.isfile(home_copy), "--drive-only never walks HOME")
        self.assertFalse(any(os.path.lexists(p) for p in left))

    def test_drive_only_needs_a_drive_and_a_missing_drive_refuses(self):
        self.intake("sweep-erased", "--drive-only", "--yes", code=1)
        self.assertIn("--drive-only needs --drive MOUNT", self.stderr)
        self.intake("sweep-erased", "--drive", self.D("unplugged"), "--yes", code=1)
        self.assertIn("is the drive plugged in?", self.stderr)

    def test_never_content_in_the_library_index(self):
        """An erased checksum that is in index.json now (released from quarantine, or filed since) is never swept,
        on the drive or in the library, and verify-gone at the end does not count it."""
        kept = {}
        index = {}
        for flag in ("released", None):
            data = os.urandom(2048)
            s = hashlib.sha256(data).hexdigest()
            lib = write(self.H("Pictures/library/2026/2026-11", f"kept-{flag}.mp4"), data)
            index[lib] = dict(sha=s, size=len(data), **({flag: True} if flag else {}))
            kept[lib] = write(self.D("media-intake-backup/library/2026/2026-11", f"kept-{flag}.mp4"), data)
            self.log(self.batch, s, len(data))
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump(index, f)
        left, _ = self.left_on_drive()
        out = self.intake("sweep-erased", self.batch, "--drive", self.drive, "--yes", "--no-wipe-free")
        self.assertIn("2 erased checksums are in the library index now (released, or filed since): never swept", out)
        self.assertIn("sweep-erased: 4 copies deleted", out)
        for lib, dcopy in kept.items():
            self.assertTrue(os.path.isfile(lib), lib)
            self.assertTrue(os.path.isfile(dcopy), dcopy)
        self.assertFalse(any(os.path.lexists(p) for p in left))
        self.assertEqual(set(json.loads(read(os.path.join(self.state, "index.json")))), set(index))

    def test_skips_toolchains_caches_and_git_and_node_modules(self):
        skipped = [write(self.H(".cargo/registry/clip.mp4"), self.data),
                   write(self.H(".cache/pip/clip.mp4"), self.data),
                   write(self.H("code/app/node_modules/x/clip.mp4"), self.data),
                   write(self.D("project/node_modules/clip.mp4"), self.data),
                   write(self.D("project/.git/objects/clip.mp4"), self.data)]
        repo = self.H("code/site")
        os.makedirs(os.path.join(repo, ".git"))
        in_git = write(os.path.join(repo, "assets/clip.mp4"), self.data)
        swept = write(self.H(".cache/some-app/clip.mp4"), self.data)
        out = self.intake("sweep-erased", self.batch, "--drive", self.drive, "--yes", "--no-wipe-free")
        self.assertFalse(os.path.lexists(swept), "other caches are swept")
        for p in skipped + [in_git]:
            self.assertTrue(os.path.isfile(p), p)
        self.assertIn("1 exact copies inside git repos, not deleted", out)
        self.assertIn("1 exact copies inside git repos (erase leaves them)\n", out, "verify-gone: a count, no path")
        self.assertNotIn(repo, out)
        self.assertIn("sweep-erased: 1 copies deleted", out)

    def test_one_batch_only_and_an_empty_log(self):
        other = os.urandom(3000)
        self.log("another-batch", hashlib.sha256(other).hexdigest(), len(other))
        not_this = write(self.D("x/other.mp4"), other)
        this = write(self.D("x/clip.mp4"), self.data)
        self.intake("sweep-erased", self.batch, "--drive", self.drive, "--drive-only", "--yes", "--no-wipe-free")
        self.assertFalse(os.path.lexists(this))
        self.assertTrue(os.path.isfile(not_this), "another batch's content is left for its own sweep")
        out = self.intake("sweep-erased", "no-such-batch", "--drive", self.drive)
        self.assertIn("nothing to sweep: no erase-log entries with sizes for no-such-batch", out)

    def test_a_drive_inside_home_is_walked_once(self):
        self.drive = self.H("mnt/backup")
        write(self.D("media-intake-backup/clip.mp4"), self.data)
        out = self.intake("sweep-erased", self.batch, "--drive", self.drive)
        self.assertIn("dry run: 1 copies of 1 erased items in HOME and the drive", out)
        self.assertRegex(out, r"\n  drive:media-intake-backup +1\n")

    def test_erase_of_an_empty_quarantine_points_at_sweep_erased(self):
        self.intake("erase", self.batch, "--yes", code=1)
        self.assertIn(f"media-intake sweep-erased {self.batch} --drive MOUNT", self.stderr)

    def test_erase_drive_takes_the_drive_trashinfo_too(self):
        """erase --drive (drive_purge) deletes a drive Trash copy's .trashinfo with it, and the coverage report
        counts it."""
        batch = "2026-11-16-plugged"
        quar = os.path.join(self.state, "quarantine", batch)
        write(os.path.join(quar, "DCIM/Camera/clip.mp4"), self.data)
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"DCIM/Camera/clip.mp4": {"reasons": ["nudity"], "sha": self.sha, "size": len(self.data),
                                                "dst": None}}, f)
        t = write(self.D(".Trash-1000/files/clip.mp4"), self.data)
        info = write(self.D(".Trash-1000/info/clip.mp4.trashinfo"), b"[Trash Info]\n")
        self.intake("erase", batch, "--yes", "--drive", self.drive, "--no-wipe-free")
        self.assertFalse(os.path.lexists(t))
        self.assertFalse(os.path.lexists(info))
        cov = [ln for ln in read(os.path.join(self.state, "erase-log.txt")).splitlines() if " coverage " in ln]
        reached = json.loads(cov[-1].split(" coverage ", 1)[1])["reached"]
        self.assertEqual(reached["backup drive Trash entries (.Trash-<uid>/info)"], 1)
        self.assertEqual(reached["backup drive copies"], 1)

    def test_is_a_library_writer(self):
        self.assertIn("sweep-erased", intake.LOCKED)
        self.assertNotIn("sweep-erased", intake.READ_ONLY)

    def test_trash_info_and_where(self):
        d = self.drive
        self.assertEqual(intake.trash_info(os.path.join(d, ".Trash-1000/files/a.jpg")),
                         os.path.join(d, ".Trash-1000/info/a.jpg.trashinfo"))
        self.assertEqual(intake.trash_info(os.path.join(d, ".Trash/1000/files/a.jpg")),
                         os.path.join(d, ".Trash/1000/info/a.jpg.trashinfo"))
        self.assertIsNone(intake.trash_info(os.path.join(d, ".Trashes/501/a.jpg")))
        self.assertIsNone(intake.trash_info(os.path.join(d, "photos/files/a.jpg")), "a folder named files")
        self.assertIsNone(intake.trash_info(os.path.join(d, ".Trash-1000/files/sub/a.jpg")), "inside a trashed folder")
        for rel in (".Trash-1000/files/a.jpg", ".Trash/1000/files/a.jpg", ".Trashes/501/a.jpg"):
            self.assertEqual(intake.where(os.path.join(d, rel), d), "Trash", rel)


if __name__ == "__main__":
    unittest.main()
