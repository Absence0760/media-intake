#!/usr/bin/env python3
"""Guard tests: the intake never lists or copies anything outside the media allowlist.

  python3 -I -m unittest discover -s tests -p test_sources.py
"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

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


class Folder(unittest.TestCase):
    """kind 'folder': a folder on the laptop's own disk, in a throwaway HOME with its own config."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        for k in ("MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_TEST_MOUNT",
                  "MEDIA_INTAKE_TEST_UUID", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            os.environ.pop(k, None)
        self.home = os.path.join(self.d.name, "home")
        os.environ["HOME"] = self.home
        # the demo project's folder is ~/Videos/demo-footage/originals
        os.environ["MEDIA_INTAKE_CONFIG"] = support.write_config(os.path.join(self.home, "Videos", "demo-footage"))
        r = self.root = os.path.join(self.home, "Pictures", "Old phone")
        self.keep = {touch(r, x) for x in [
            "2019/IMG_0001.JPG", "2019/holiday/clip.MOV", "Screenshot_20190101-101010.png",
            "WhatsApp/IMG-20190101-WA0001.jpg"]}
        for x in [".hidden/a.jpg", "._IMG_0001.JPG", "notes.txt", "Bitwarden/export.png",
                  "recovery codes.png", "$RECYCLE.BIN/x.jpg",
                  "some-repo/.git/HEAD", "some-repo/assets/logo.png", "some-repo/deep/a/b.jpg",
                  "worktree/.git", "worktree/img/c.jpg"]:  # a worktree's .git is a file
            touch(r, x)
        touch(self.home, "Documents/outside.jpg")
        os.symlink(os.path.join(self.home, "Documents"), os.path.join(r, "link-dir"))
        os.symlink(os.path.join(self.home, "Documents/outside.jpg"), os.path.join(r, "link.jpg"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.d.cleanup()

    def test_walk_accepts_folder_and_skips_git_trees(self):
        self.assertEqual(sources.check_folder_root(self.root), os.path.realpath(self.root))
        got = {p for p, _ in sources.walk(self.root, "folder")}
        self.assertEqual(got, self.keep, "symlinks, hidden, system, non-media, denied names and git trees skipped")
        for rel in ["link.jpg", "link-dir/outside.jpg", ".hidden/a.jpg", "notes.txt", "../../Documents/outside.jpg"]:
            with self.assertRaises(sources.Denied, msg=rel):
                sources.check_drive(self.root, os.path.join(self.root, rel))

    def refused(self, path, why=""):
        os.makedirs(path, exist_ok=True)
        with self.assertRaises(sources.Denied, msg=path) as e:
            sources.check_folder_root(path)
        if why:
            self.assertIn(why, str(e.exception), path)
        with self.assertRaises(sources.Denied, msg=path):
            list(sources.walk(path, "folder"))

    def test_home_and_outside_home_refused(self):
        self.refused(self.home, "home folder")
        self.refused(self.d.name, "home folder")  # contains HOME
        self.refused(os.path.join(self.d.name, "elsewhere"), "outside your home")
        for d in sources.FOLDER_DRIVE_PREFIXES:
            if os.path.isdir(d) and not os.path.islink(d):
                with self.assertRaises(sources.Denied, msg=d) as e:
                    sources.check_folder_root(d)
                self.assertIn("--kind drive", str(e.exception))

    def test_state_library_and_project_folders_refused(self):
        h = self.home
        # is, is inside, or contains: the state, the library, the videos and the project folder
        for p in (".local/share/media-intake", ".local/share/media-intake/quarantine/b",
                  "Pictures/library", "Pictures/library/2019", "Pictures",
                  "Videos/personal", "Videos/personal/2019", "Videos/demo-footage/originals/2019-x", "Videos"):
            self.refused(os.path.join(h, p))
        self.refused(os.path.join(h, "Pictures/library/2019"), "photo library")
        self.refused(os.path.join(h, "Videos/demo-footage/originals"), "Demo project folder")

    def test_config_overrides_are_refused_too(self):
        lib = os.path.join(self.home, "Photos lib")
        state = os.path.join(self.home, "intake-state")
        os.environ.update(PHOTO_LIBRARY=lib, MEDIA_INTAKE_STATE=state)
        self.refused(os.path.join(lib, "2020"), "photo library")
        self.refused(os.path.join(state, "batches"), "media-intake state")
        self.assertEqual(sources.check_folder_root(self.root), os.path.realpath(self.root),
                         "~/Pictures no longer holds the library")
        os.environ["XDG_DATA_HOME"] = os.path.join(self.home, "appdata")
        self.refused(os.path.join(self.home, "appdata", "photos"), "XDG_DATA_HOME")

    def test_app_data_keys_and_code_trees_refused(self):
        h = self.home
        for p in (".local", ".local/share/Trash/files", ".config", ".config/x", ".cache/thumbnails",
                  ".var/app/some.app", ".ssh", ".gnupg", "github", "github/proj/media", "repos/x",
                  "src/app/assets", "code", "development/sdk", "Android/Sdk", "go/pkg", "snap/app"):
            self.refused(os.path.join(h, p))
        self.refused(os.path.join(h, "GitHub", "x"))  # case-insensitive disk: same folder
        self.refused(os.path.join(h, ".mozilla/photos"), "hidden folder")
        self.refused(os.path.join(h, "Documents/.secret"), "hidden folder")

    def test_password_manager_paths_refused(self):
        for p in ("Downloads/vault backup", "Bitwarden export", "KeePass", "Documents/recovery codes"):
            self.refused(os.path.join(self.home, p), "denied name")

    def test_git_repository_refused(self):
        touch(self.home, "projects/site/.git/HEAD")
        self.refused(os.path.join(self.home, "projects/site"), "git repository")
        self.refused(os.path.join(self.home, "projects/site/img/2019"), "git repository")
        touch(self.home, "work/tree/.git")  # a worktree: .git is a file
        self.refused(os.path.join(self.home, "work/tree/pics"), "git repository")

    def test_sweep_skip_trees_are_refused(self):
        """erase's home sweep skips these trees: a folder source never sits in one, so a confirmed
        erase always reaches a copy left in the source folder."""
        import intake
        for rel in intake.SWEEP_SKIP:
            if not rel.startswith(".."):
                self.refused(os.path.join(self.home, rel))

    def test_root_must_be_a_real_folder(self):
        alias = os.path.join(self.home, "alias")
        os.symlink(self.root, alias)
        for bad in (alias, os.path.join(self.home, "missing"), os.path.join(self.root, "2019/IMG_0001.JPG")):
            with self.assertRaises(sources.Denied, msg=bad):
                sources.check_folder_root(bad)

    def test_fails_closed(self):
        os.environ["HOME"] = "/"
        with self.assertRaises(sources.Denied):
            sources.check_folder_root(self.root)
        os.environ["HOME"] = self.home
        orig = sources.folder_deny_roots
        try:
            def broken():
                raise OSError("cannot read")
            sources.folder_deny_roots = broken
            with self.assertRaises(sources.Denied) as e:
                sources.check_folder_root(self.root)
            self.assertIn("could not check", str(e.exception))
        finally:
            sources.folder_deny_roots = orig


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
