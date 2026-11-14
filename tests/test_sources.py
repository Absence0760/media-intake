#!/usr/bin/env python3
"""Guard tests: the intake never lists or copies anything outside the media allowlist.

  python3 -I -m unittest discover -s tests -p test_sources.py
"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources  # noqa: E402


def touch(root, rel):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()
    return p


class Walk(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        r = self.root = self.d.name
        self.keep = {touch(r, x) for x in [
            "DCIM/Camera/20261108_071502.jpg", "DCIM/Camera/20261108_071600.mp4",
            "DCIM/Screenshots/Screenshot_20261101-101010.png", "Pictures/WhatsApp/IMG-20261101-WA0001.jpg"]}
        for x in ["Android/data/com.x8bit.bitwarden/files/export.json",
                  "Android/media/com.whatsapp/WhatsApp/Media/a.jpg",
                  "Download/bank statement.pdf", "Download/photo.jpg", "Documents/passport.jpg",
                  "DCIM/Camera/notes.txt", "DCIM/.thumbnails/1.jpg", "Pictures/.trash/x.jpg",
                  "DCIM/Bitwarden/vault.png", "Pictures/Authenticator/qr.png", "s.png", "ui.xml"]:
            touch(r, x)
        os.symlink(os.path.join(r, "Download"), os.path.join(r, "DCIM", "link"))

    def tearDown(self):
        self.d.cleanup()

    def test_only_allowlisted_media(self):
        got = {p for p, _ in sources.walk(self.root, "phone")}
        self.assertEqual(got, self.keep)

    def test_check_rejects_by_hand(self):
        for rel in ["Android/data/x/a.jpg", "Download/photo.jpg", "Documents/a.png",
                    "DCIM/Camera/notes.txt", "DCIM/Bitwarden/vault.png", "../etc/passwd"]:
            with self.assertRaises(sources.Denied, msg=rel):
                sources.check(self.root, os.path.join(self.root, rel))

    def test_whatsapp_media_only(self):
        r, wa = self.root, "Android/media/com.whatsapp/WhatsApp"
        good = {touch(r, x) for x in [f"{wa}/Media/WhatsApp Images/IMG-1.jpg",
                f"{wa}/Media/WhatsApp Images/Sent/IMG-2.jpg", f"{wa}/Media/WhatsApp Images/Private/IMG-3.jpg",
                f"{wa}/Media/WhatsApp Video/VID-1.mp4", f"{wa}/Media/WhatsApp Video/Sent/VID-2.mp4"]}
        bad = [f"{wa}/Databases/x.jpg", f"{wa}/Backups/x.jpg", f"{wa}/Media/.Statuses/x.jpg",
               f"{wa}/Media/.Links/x.jpg", f"{wa}/Media/WhatsApp Images/.trash/x.jpg",
               f"{wa}/Media/WhatsApp Documents/x.jpg", f"{wa}/Media/whatsapp images/x.jpg",
               "Android/media/com.whatsapp/x.jpg", "Android/data/com.whatsapp/files/x.jpg",
               "Android/media/org.telegram.messenger/Telegram/Telegram Images/x.jpg",
               f"{wa}/Media/WhatsApp Images/notes.txt", f"{wa}/Media/WhatsApp Images/Authenticator/qr.png"]
        for x in bad:
            touch(r, x)
        os.symlink(os.path.join(r, "Download"), os.path.join(r, wa, "Media/WhatsApp Images/link"))
        got = {p for p, _ in sources.walk(r, "phone")}
        self.assertEqual(got, self.keep | good)
        for p in good:
            sources.check(r, p)
        for rel in bad + [f"{wa}/Media/WhatsApp Images/link/photo.jpg",
                          f"{wa}/Media/WhatsApp Images/../../Databases/x.jpg"]:
            with self.assertRaises(sources.Denied, msg=rel):
                sources.check(r, os.path.join(r, rel))
        self.assertEqual(sources.thumbnail_dirs(r), [os.path.join(r, "DCIM/.thumbnails")])
        with self.assertRaises(sources.Denied):
            sources.check_thumbnail(r, os.path.join(r, wa, "Media/WhatsApp Images/IMG-1.jpg"))

    def test_gopro_only_numbered_folders(self):
        r = self.root
        good = touch(r, "DCIM/100GOPRO/GX010123.MP4")
        touch(r, "DCIM/100GOPRO/GL010123.LRV")
        touch(r, "DCIM/100GOPRO/GX010123.THM")
        touch(r, "DCIM/Other/x.mp4")
        got = {p for p, _ in sources.walk(r, "gopro")}
        self.assertEqual(got, {good})

    def test_thumbnail_dirs_only(self):
        r = self.root
        self.assertEqual(sources.thumbnail_dirs(r), [os.path.join(r, "DCIM/.thumbnails")])
        sources.check_thumbnail(r, os.path.join(r, "DCIM/.thumbnails/1.jpg"))
        for rel in ["DCIM/Camera/20261108_071502.jpg", "Pictures/.trash/x.jpg", "Android/.thumbnails/x.jpg",
                    "DCIM/.thumbnails", "../DCIM/.thumbnails/x.jpg"]:
            with self.assertRaises(sources.Denied, msg=rel):
                sources.check_thumbnail(r, os.path.join(r, rel))
        os.symlink(os.path.join(r, "Download"), os.path.join(r, "Pictures", ".thumbnails"))
        self.assertEqual(sources.thumbnail_dirs(r), [os.path.join(r, "DCIM/.thumbnails")], "a symlink is skipped")


class Drive(unittest.TestCase):
    """kind 'drive': a named folder on an external drive (MEDIA_INTAKE_TEST_MOUNT stands in for findmnt)."""
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.mount = os.path.join(self.d.name, "drive")
        self.root = os.path.join(self.mount, "Old phone")
        self.env = dict(os.environ)
        os.environ["MEDIA_INTAKE_TEST_MOUNT"] = self.mount
        os.environ["MEDIA_INTAKE_TEST_UUID"] = "ABCD-1234"
        r = self.root
        self.keep = {touch(r, x) for x in [
            "2019/IMG_0001.JPG", "2019/holiday/clip.MOV", "Screenshot_20190101-101010.png",
            "WhatsApp/IMG-20190101-WA0001.jpg", "Documents/scan.jpg", "deep/a/b/c/d.webp"]}
        for x in ["$RECYCLE.BIN/x.jpg", "System Volume Information/y.jpg", ".Trashes/501/z.jpg",
                  ".Spotlight-V100/s.png", ".fseventsd/f.jpg", "lost+found/#123.jpg",
                  ".DocumentRevisions-V100/r.jpg", ".hidden/a.jpg", "._IMG_0001.JPG", "2019/.DS_Store",
                  "notes.txt", "2019/IMG_0001.AAE", "Bitwarden/export.png", "KeePass/db.jpg",
                  "recovery codes.png", "GOPRO/GL010001.LRV"]:
            touch(r, x)
        touch(self.mount, "elsewhere/outside.jpg")
        os.symlink(os.path.join(self.mount, "elsewhere"), os.path.join(r, "link-dir"))
        os.symlink(os.path.join(self.mount, "elsewhere/outside.jpg"), os.path.join(r, "link.jpg"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.d.cleanup()

    def test_walk_only_media_outside_system_dirs(self):
        got = {p for p, _ in sources.walk(self.root, "drive")}
        self.assertEqual(got, self.keep)

    def test_check_rejects_by_hand(self):
        for rel in ["$RECYCLE.BIN/x.jpg", ".Trashes/501/z.jpg", "lost+found/#123.jpg", "._IMG_0001.JPG",
                    "Bitwarden/export.png", "notes.txt", "link.jpg", "link-dir/outside.jpg", "../elsewhere/outside.jpg"]:
            with self.assertRaises(sources.Denied, msg=rel):
                sources.check_drive(self.root, os.path.join(self.root, rel))

    def test_categories(self):
        c = sources.drive_category
        self.assertEqual(c("2019/IMG_0001.JPG"), "photo")
        self.assertEqual(c("2019/holiday/clip.MOV"), "video")
        self.assertEqual(c("Screenshot_20190101-101010.png"), "screenshot")
        self.assertEqual(c("a/Screen Shot 2019-01-01 at 10.10.10.png"), "screenshot")
        self.assertEqual(c("WhatsApp/IMG-20190101-WA0001.jpg"), "messaging:WhatsApp")
        self.assertEqual(c("VID-20190101-WA0002.mp4"), "messaging:WhatsApp")
        self.assertEqual(c("IMG-20190101.jpg"), "photo")

    def test_backup_layout_refused_unless_marker(self):
        for folder in sources.BACKUP_LAYOUT:
            os.makedirs(os.path.join(self.mount, folder))
            with self.assertRaises(sources.Denied, msg=folder):
                sources.check_drive_root(self.root)
            with self.assertRaises(sources.Denied):
                list(sources.walk(self.root, "drive"))
            os.rmdir(os.path.join(self.mount, folder))
        os.makedirs(os.path.join(self.mount, "photos"))
        touch(self.mount, sources.SOURCE_OK_MARKER)
        self.assertEqual(sources.check_drive_root(self.root), self.mount)

    def test_laptop_disk_refused(self):
        del os.environ["MEDIA_INTAKE_TEST_MOUNT"]
        with self.assertRaises(sources.Denied):  # a temp dir is on the laptop's own disk (or tmpfs /tmp)
            sources.check_drive_root(self.root)
        orig = sources._findmnt
        try:
            for m in ("/", "/home", "/tmp", "/var"):
                sources._findmnt = lambda field, path, m=m: m
                with self.assertRaises(sources.Denied, msg=m):
                    sources.check_drive_root(self.root)
        finally:
            sources._findmnt = orig

    def test_root_must_be_a_real_folder(self):
        os.symlink(self.root, os.path.join(self.mount, "alias"))
        for bad in (os.path.join(self.mount, "alias"), os.path.join(self.mount, "missing")):
            with self.assertRaises(sources.Denied, msg=bad):
                sources.check_drive_root(bad)


class CaseCollisions(unittest.TestCase):
    """check_case: a device path that differs from another only by case is never a delete target."""

    def test_case_only_twins_refused_with_every_name(self):
        known = ["/dev-root/DCIM/Camera/IMG_1.jpg", "/dev-root/DCIM/Camera/img_1.JPG",
                 "/dev-root/DCIM/camera/IMG_1.jpg", "/dev-root/DCIM/Camera/IMG_2.jpg"]
        with self.assertRaises(sources.CaseCollision) as cm:
            sources.check_case(["/dev-root/DCIM/Camera/IMG_1.jpg"], known)
        self.assertEqual(cm.exception.groups, [sorted(known[:3])])
        self.assertIsInstance(cm.exception, sources.Denied, "a collision is a refusal like any other")
        for p in known[:3]:
            self.assertIn(p, str(cm.exception))

    def test_other_name_need_not_be_a_target(self):
        with self.assertRaises(sources.CaseCollision):
            sources.check_case(["/r/DCIM/a.jpg"], ["/r/DCIM/A.jpg"])
        with self.assertRaises(sources.CaseCollision, msg="two targets collide with each other"):
            sources.check_case(["/r/DCIM/a.jpg", "/r/DCIM/A.jpg"], [])

    def test_unicode_case_and_normalisation(self):
        with self.assertRaises(sources.CaseCollision):
            sources.check_case(["/r/DCIM/STRASSE.jpg"], ["/r/DCIM/straße.jpg"])
        with self.assertRaises(sources.CaseCollision, msg="NFC vs NFD spelling of the same name"):
            sources.check_case(["/r/DCIM/café.jpg"], ["/r/DCIM/café.jpg"])

    def test_distinct_names_pass(self):
        known = ["/r/DCIM/Camera/IMG_1.jpg", "/r/DCIM/Camera/IMG_2.jpg", "/r/Pictures/IMG_1.jpg"]
        sources.check_case(known, known)  # the same path twice is one name, not a collision
        sources.check_case(["/r/DCIM/Camera/IMG_1.jpg"], known + ["/r/DCIM/Camera/IMG_1.jpeg"])
        sources.check_case([], ["/r/a.jpg", "/r/A.jpg"])  # a collision nobody deletes is left alone


if __name__ == "__main__":
    unittest.main()
