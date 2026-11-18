#!/usr/bin/env python3
"""Tests for backup.py on a synthetic library in a throwaway HOME and a fake drive folder.

  python3 -I -m unittest discover -s tests -p test_backup.py

The "media" are a few bytes with photo and video extensions: backup never decodes them.
MEDIA_INTAKE_TEST_MOUNT/UUID stand in for findmnt; the laptop-disk test fakes findmnt's answer.
Never touches a real drive.
"""
import hashlib, json, os, stat, subprocess, sys, tempfile, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

import backup  # noqa: E402
import sources  # noqa: E402

CLI = os.path.join(support.PKG, "cli.py")
KEYS = ("HOME", "MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG",
        "MEDIA_INTAKE_TEST_MOUNT", "MEDIA_INTAKE_TEST_UUID")


def put(base, rel, data):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def tree(base):
    out = set()
    for r, _, fs in os.walk(base):
        out.update(os.path.relpath(os.path.join(r, f), base) for f in fs)
    return out


class Backup(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in KEYS}
        self.d = tempfile.TemporaryDirectory()
        t = self.d.name
        self.home = os.path.join(t, "home")
        self.lib = os.path.join(self.home, "Pictures", "library")
        self.vids = os.path.join(self.home, "Videos", "personal")
        footage = os.path.join(self.home, "demo-footage")
        self.proj = os.path.join(footage, "originals")
        self.state = os.path.join(self.lib, "state")  # inside the library on purpose: must never be copied
        self.mount = os.path.join(t, "drive")
        os.makedirs(self.mount, mode=0o700)
        os.environ.update(HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                          PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(footage),
                          MEDIA_INTAKE_TEST_MOUNT=self.mount, MEDIA_INTAKE_TEST_UUID="TEST-UUID")
        put(self.lib, "2024/2024-05/IMG_0001.jpg", b"photo one")
        put(self.lib, "2024/2024-05/IMG_0001.jpg.xmp", b"<x:xmpmeta/>")
        put(self.lib, "2024/2024-06/IMG_0002.heic", b"photo two, longer")
        put(self.lib, "Screenshots/2024/Screenshot_1.png", b"screenshot")
        put(self.vids, "2024/2024-05/VID_0001.mp4", b"video bytes" * 10)
        put(self.proj, "2024-05-01-demo-shoot/gopro/GX010001.MP4", b"gopro chapter")
        # never copied: the state folder (quarantine, staging, review, index), hidden folders and files,
        # non-media, orphaned sidecars, symlinks
        put(self.state, "quarantine/b1/IMG_9999.jpg", b"flagged")
        put(self.state, "batches/b1/raw/IMG_8888.jpg", b"staged")
        put(self.state, "batches/b1/review/IMG_7777.jpg", b"review copy")
        put(self.state, "index.json", b"{}")
        put(self.lib, ".dtrash/files/IMG_6666.jpg", b"digikam trash")
        put(self.lib, "2024/2024-05/.hidden.jpg", b"hidden")
        put(self.lib, "digikam4.db", b"database")
        put(self.lib, "2024/2024-05/notes.txt", b"text")
        put(self.lib, "2024/2024-05/GONE.jpg.xmp", b"<x:xmpmeta/>")
        os.symlink(os.path.join(self.lib, "2024/2024-05/IMG_0001.jpg"), os.path.join(self.lib, "link.jpg"))
        self.dest = os.path.join(self.mount, backup.DEST_DIR)
        self.expected = {"library/2024/2024-05/IMG_0001.jpg", "library/2024/2024-05/IMG_0001.jpg.xmp",
                         "library/2024/2024-06/IMG_0002.heic", "library/Screenshots/2024/Screenshot_1.png",
                         "videos/2024/2024-05/VID_0001.mp4",
                         "projects/demo/2024-05-01-demo-shoot/gopro/GX010001.MP4"}

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.d.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", CLI, "backup", *args], capture_output=True, text=True,
                              env=dict(os.environ))

    def manifest(self):
        p = os.path.join(self.state, "backup")
        names = os.listdir(p)
        self.assertEqual(len(names), 1)
        with open(os.path.join(p, names[0])) as f:
            return json.load(f)["files"]

    def test_copies_library_media_and_sidecars_only(self):
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree(self.dest), self.expected)
        self.assertIn("6 copied", r.stdout)
        for rel in self.expected:
            with open(os.path.join(self.dest, rel), "rb") as a:
                src = rel.replace("library/", self.lib + "/", 1).replace("videos/", self.vids + "/", 1) \
                    .replace("projects/demo/", self.proj + "/", 1)
                with open(src, "rb") as b:
                    self.assertEqual(a.read(), b.read(), rel)
        self.assertTrue(os.path.isfile(os.path.join(self.mount, sources.BACKUP_MARKER)))
        self.assertNotIn("flagged", r.stdout + r.stderr)
        self.assertEqual(set(self.manifest()), self.expected)
        self.assertFalse([n for n in tree(self.mount) if n.endswith(backup.TMP_SUFFIX)])

    def test_second_run_copies_nothing(self):
        self.assertEqual(self.run_cli(self.mount).returncode, 0)
        before = {rel: os.stat(os.path.join(self.dest, rel)).st_mtime_ns for rel in self.expected}
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("6 already on the drive, 0 to copy", r.stdout)
        self.assertIn("0 copied, 0 replaced", r.stdout)
        self.assertEqual(before, {rel: os.stat(os.path.join(self.dest, rel)).st_mtime_ns for rel in self.expected})
        # without the manifest the copies are compared by content, and still nothing is copied
        os.remove(os.path.join(self.state, "backup", os.listdir(os.path.join(self.state, "backup"))[0]))
        r = self.run_cli(self.mount)
        self.assertIn("6 already on the drive, 0 to copy", r.stdout)

    def test_verify_catches_a_corrupted_copy_and_the_next_run_repairs_it(self):
        self.assertEqual(self.run_cli(self.mount, "--verify").returncode, 0)
        bad = os.path.join(self.dest, "library/2024/2024-06/IMG_0002.heic")
        st = os.stat(bad)
        with open(bad, "r+b") as f:  # same size, same mtime: only the content says it is wrong
            f.write(b"X")
        os.utime(bad, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertIn("0 to copy", self.run_cli(self.mount).stdout)  # a plain run trusts size and mtime
        r = self.run_cli(self.mount, "--verify")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("5 copies match their source (sha256), 1 differ, 0 missing", r.stdout)
        self.assertIn("differs: library/2024/2024-06/IMG_0002.heic", r.stdout)
        r = self.run_cli(self.mount, "--verify")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 replaced", r.stdout)
        with open(bad, "rb") as f:
            self.assertEqual(f.read(), b"photo two, longer")

    def test_verify_reports_a_missing_copy(self):
        self.assertEqual(self.run_cli(self.mount).returncode, 0)
        os.remove(os.path.join(self.dest, "videos/2024/2024-05/VID_0001.mp4"))
        r = self.run_cli(self.mount, "--dry-run", "--verify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing: videos/2024/2024-05/VID_0001.mp4", r.stdout)

    def test_dry_run_writes_nothing(self):
        r = self.run_cli(self.mount, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("6 to copy", r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertEqual(os.listdir(self.mount), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "backup")))

    def test_changed_file_replaced_and_removed_file_left_on_the_drive(self):
        self.assertEqual(self.run_cli(self.mount).returncode, 0)
        put(self.lib, "2024/2024-05/IMG_0001.jpg", b"photo one, edited")
        os.remove(os.path.join(self.lib, "Screenshots/2024/Screenshot_1.png"))
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("0 copied, 1 replaced", r.stdout)
        self.assertIn("1 files on the drive are no longer in the library; left alone", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "library/Screenshots/2024/Screenshot_1.png")))
        with open(os.path.join(self.dest, "library/2024/2024-05/IMG_0001.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"photo one, edited")

    def test_file_changed_while_copied_is_skipped_then_copied_next_run(self):
        src = os.path.join(self.lib, "2024/2024-06/IMG_0002.heic")
        orig, calls = backup._stat, []

        def flaky(p):  # the listing, then before and after the copy: the third look sees a new mtime
            st = orig(p)
            if p == src and st:
                calls.append(p)
                if len(calls) == 3:
                    st = (st[0], st[1] + 1)
            return st

        lines = []
        backup._stat = flaky
        try:
            self.assertEqual(backup.backup(self.mount, out=lines.append), 1)
        finally:
            backup._stat = orig
        self.assertIn("backup: 1 changed while being copied, skipped (run backup again):", lines)
        rel = "library/2024/2024-06/IMG_0002.heic"
        self.assertFalse(os.path.lexists(os.path.join(self.dest, rel)))
        self.assertFalse([n for n in tree(self.mount) if n.endswith(backup.TMP_SUFFIX)])
        lines = []
        self.assertEqual(backup.backup(self.mount, out=lines.append), 0)
        self.assertIn("backup: 1 copied, 0 replaced (changed in the library)", lines)

    def test_file_vanished_before_its_copy_is_counted_and_leaves_nothing(self):
        src = os.path.join(self.lib, "2024/2024-06/IMG_0002.heic")
        orig = backup.copy_one

        def vanish_first(s, dest, rel):
            if s == src:
                os.remove(src)
            return orig(s, dest, rel)

        lines = []
        with mock.patch.object(backup, "copy_one", vanish_first):
            self.assertEqual(backup.backup(self.mount, out=lines.append), 0)
        self.assertIn("backup: 1 gone from the library since it was listed, skipped", lines)
        self.assertIn("backup: 5 copied, 0 replaced (changed in the library)", lines)
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "library/2024/2024-06/IMG_0002.heic")))
        self.assertFalse([n for n in tree(self.mount) if n.endswith(backup.TMP_SUFFIX)])

    def test_not_enough_free_space_refused_before_writing(self):
        lines = []
        with mock.patch.object(backup.shutil, "disk_usage", return_value=mock.Mock(free=10)):
            self.assertEqual(backup.backup(self.mount, out=lines.append), 1)
        self.assertTrue(any(ln.startswith("backup: refused:") and "MB free" in ln and "--skip PART" in ln for ln in lines), lines)
        self.assertEqual(os.listdir(self.mount), [])  # no marker, no backup folder

    def test_skip_leaves_a_root_out_and_its_drive_copy_alone(self):
        # An earlier run's copy of videos/ (and its manifest entry) must survive a run that skips videos.
        self.assertEqual(self.run_cli(self.mount).returncode, 0)
        old = os.path.join(self.dest, "videos/2024/2024-05/VID_0001.mp4")
        put(self.vids, "2024/2024-05/VID_0002.mp4", b"a new video")  # not copied while videos is skipped
        r = self.run_cli(self.mount, "--skip", "videos", "--verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("backup: videos: skipped this run (--skip)", r.stdout)
        self.assertIn("5 already on the drive, 0 to copy", r.stdout)
        self.assertNotIn("no longer in the library", r.stdout, "a skipped root's copies are not counted as extra")
        self.assertIn("verify: 5 copies match", r.stdout)
        self.assertTrue(os.path.isfile(old))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "videos/2024/2024-05/VID_0002.mp4")))
        self.assertIn("videos/2024/2024-05/VID_0001.mp4", self.manifest(), "a skipped root keeps its entries")
        r = self.run_cli(self.mount, "--skip", "projects/demo", "--skip", "library")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree(os.path.join(self.dest, "videos")), {"2024/2024-05/VID_0001.mp4", "2024/2024-05/VID_0002.mp4"})
        r = self.run_cli(self.mount, "--skip", "photos")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--skip photos: not a library root (one of: library, videos, projects/demo)", r.stdout)

    def test_originals_unchanged(self):
        def snap():
            out = {}
            for base in (self.lib, self.vids, self.proj):
                for rel in tree(base):
                    p = os.path.join(base, rel)
                    if os.path.islink(p):
                        continue
                    with open(p, "rb") as f:
                        out[p] = (hashlib.sha256(f.read()).hexdigest(), os.stat(p).st_mtime_ns)
            return out

        before = snap()
        self.assertEqual(self.run_cli(self.mount, "--verify").returncode, 0)
        after = snap()
        after = {p: v for p, v in after.items() if not p.startswith(os.path.join(self.state, "backup"))}
        self.assertEqual(before, after)

    def test_record_and_marker_are_private(self):
        self.assertEqual(self.run_cli(self.mount).returncode, 0)
        d = os.path.join(self.state, "backup")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for p in (os.path.join(d, os.listdir(d)[0]), os.path.join(self.mount, sources.BACKUP_MARKER)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)

    def test_mount_that_is_a_symlink_refused(self):
        link = os.path.join(self.d.name, "drive-link")
        os.symlink(self.mount, link)
        os.environ["MEDIA_INTAKE_TEST_MOUNT"] = link
        r = self.run_cli(link)
        self.assertEqual(r.returncode, 2)
        self.assertIn("symlink", r.stdout)
        self.assertEqual(os.listdir(self.mount), [])

    def test_destination_file_that_is_a_symlink_or_folder_refused(self):
        target = put(self.d.name, "elsewhere/keep.jpg", b"not yours")
        os.makedirs(os.path.join(self.dest, "library/2024/2024-05"), mode=0o700)
        os.symlink(target, os.path.join(self.dest, "library/2024/2024-05/IMG_0001.jpg"))
        os.makedirs(os.path.join(self.dest, "library/2024/2024-06/IMG_0002.heic"), mode=0o700)
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 1)
        self.assertIn("backup: 2 could not be copied:", r.stdout)
        self.assertIn("library/2024/2024-05/IMG_0001.jpg: not a plain file at the destination", r.stdout)
        self.assertIn("library/2024/2024-06/IMG_0002.heic: not a plain file at the destination", r.stdout)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"not yours")
        self.assertTrue(os.path.islink(os.path.join(self.dest, "library/2024/2024-05/IMG_0001.jpg")))
        self.assertEqual(os.listdir(os.path.join(self.dest, "library/2024/2024-06/IMG_0002.heic")), [])
        self.assertFalse([n for n in tree(self.mount) if n.endswith(backup.TMP_SUFFIX)])

    def test_case_only_clash_is_reported_not_copied(self):
        put(self.lib, "2024/2024-05/img_0001.JPG", b"another photo")
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 1)
        self.assertIn("differs from another only by letter case", r.stdout)
        self.assertEqual(len([n for n in tree(self.dest) if n.lower() == "library/2024/2024-05/img_0001.jpg"]), 1)

    def test_destination_inside_the_library_or_state_refused(self):
        for inside in (os.path.join(self.lib, "2024"), os.path.join(self.state, "x"), self.proj):
            os.makedirs(inside, exist_ok=True)
            os.environ["MEDIA_INTAKE_TEST_MOUNT"] = inside
            r = self.run_cli(inside)
            self.assertEqual(r.returncode, 2, inside)
            self.assertIn("refused", r.stdout)
            self.assertFalse(os.path.exists(os.path.join(inside, backup.DEST_DIR)), inside)

    def test_drive_holding_the_library_refused(self):
        os.environ["MEDIA_INTAKE_TEST_MOUNT"] = self.home
        r = self.run_cli(self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("is on", r.stdout)

    def test_not_a_mount_point_refused(self):
        sub = os.path.join(self.mount, "folder")
        os.makedirs(sub)
        r = self.run_cli(sub)  # MEDIA_INTAKE_TEST_MOUNT says the drive's mount is self.mount
        self.assertEqual(r.returncode, 2)
        self.assertIn("is the drive mounted?", r.stdout)
        self.assertEqual(os.listdir(sub), [])

    def test_source_ok_marker_refused(self):
        put(self.mount, sources.SOURCE_OK_MARKER, b"")
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 2)
        self.assertIn(sources.SOURCE_OK_MARKER, r.stdout)

    def test_laptop_disk_refused(self):
        del os.environ["MEDIA_INTAKE_TEST_MOUNT"]
        orig = sources._findmnt
        try:
            for m in ("/", "/home", "/tmp", "/var"):
                sources._findmnt = lambda field, path, m=m: m
                with self.assertRaises(backup.Refused, msg=m) as e:
                    backup.check_dest(m)
                self.assertIn("laptop", str(e.exception))
            sources._findmnt = lambda field, path: ""  # findmnt missing or no answer: refused too
            with self.assertRaises(backup.Refused):
                backup.check_dest(self.mount)
            sources._findmnt = lambda field, path: "/"  # an unmounted drive's empty mount folder is on /
            with self.assertRaises(backup.Refused) as e:
                backup.check_dest(self.mount)
            self.assertIn("is the drive mounted?", str(e.exception))
        finally:
            sources._findmnt = orig

    def test_symlink_on_the_drive_never_redirects_a_write(self):
        outside = os.path.join(self.d.name, "outside")
        os.makedirs(outside)
        os.makedirs(os.path.join(self.dest, "library", "2024"), mode=0o700)
        os.symlink(outside, os.path.join(self.dest, "library", "2024", "2024-05"))
        r = self.run_cli(self.mount)
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not be copied", r.stdout)
        self.assertEqual(os.listdir(outside), [])


if __name__ == "__main__":
    unittest.main()
