#!/usr/bin/env python3
"""Guard tests: the intake never lists or copies anything outside the media allowlist.

  python3 -I -m unittest discover -s tests -p test_sources.py
"""
import os, sys, tempfile, unittest, unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intake  # noqa: E402  (category: the phone paths' messaging app)
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

    def test_telegram_and_signal_phone_folders(self):
        """Telegram's and Signal's folders are read only when the config names them, each exactly."""
        r = self.root
        tg_pub = "Android/media/org.telegram.messenger/Telegram"  # Android 11+: getExternalMediaDirs()/Telegram
        extra = {f"{tg_pub}/Telegram Images": "Telegram", f"{tg_pub}/Telegram Video": "Telegram",
                 "Telegram/Telegram Images": "Telegram",  # Android 10 and older: <storage>/Telegram
                 "Movies/Telegram": "Telegram", "Movies/Signal": "Signal"}
        good = {touch(r, x) for x in [
            f"{tg_pub}/Telegram Images/1234_567.jpg", f"{tg_pub}/Telegram Video/4_5.mp4",
            "Telegram/Telegram Images/old.jpg", "Movies/Telegram/VID_20261114_102030_123.mp4",
            "Movies/Signal/signal-2026-11-14-10-20-30-123.mp4",
            "Pictures/Telegram/IMG_20261114_102030_123.jpg",  # Pictures/ is read anyway: "Save to gallery"
            "Pictures/Signal/signal-2026-11-14-10-20-30-456.jpg"]}
        bad = [f"{tg_pub}/Telegram Documents/passport.jpg", f"{tg_pub}/Telegram Audio/a.mp4",
               f"{tg_pub}/Telegram Files/x.jpg", f"{tg_pub}/Telegram Stories/s.jpg", f"{tg_pub}/x.jpg",
               f"{tg_pub}/Telegram Images/.nomedia", f"{tg_pub}/Telegram Images/.hidden/x.jpg",
               "Android/data/org.telegram.messenger/files/Telegram/Telegram Images/x.jpg",
               "Android/data/org.telegram.messenger/cache/x.jpg",
               "Download/Signal/signal-2026-11-14-10-20-30-789.jpg", "Documents/Signal/x.jpg",
               "Android/data/org.thoughtcrime.securesms/files/x.jpg",
               "Signal/Backups/signal-2026-11-14-10-20-30.backup", "Movies/Signal/notes.txt"]
        unlisted = ["Telegram/Telegram Documents/x.jpg", "Telegram/x.jpg", "Movies/Other/x.mp4", "Movies/x.mp4"]
        for x in bad + unlisted:
            touch(r, x)
        with unittest.mock.patch.object(sources, "PHONE_EXTRA", extra), \
                unittest.mock.patch.object(sources, "PHONE_ALLOW_EXTRA", tuple(extra)):
            got = {p for p, _ in sources.walk(r, "phone")}
            self.assertEqual(got, self.keep | good)
            for rel in bad:
                with self.assertRaises(sources.Denied, msg=rel):
                    sources.check(r, os.path.join(r, rel))
            cats = {os.path.relpath(p, r): intake.category(os.path.relpath(p, r), "phone") for p in good}
        self.assertEqual({k: v for k, v in cats.items() if v != "messaging:Telegram"},
                         {"Movies/Signal/signal-2026-11-14-10-20-30-123.mp4": "messaging:Signal",
                          "Pictures/Signal/signal-2026-11-14-10-20-30-456.jpg": "messaging:Signal"})
        # Without the config entries nothing of Telegram's app folders is read (Pictures/ still is).
        got = {os.path.relpath(p, r) for p, _ in sources.walk(r, "phone")}
        self.assertFalse({x for x in got if x.startswith(("Android/", "Telegram/", "Movies/"))})

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
        self.assertEqual(c("Signal/signal-2026-11-14-10-20-30-123.jpg"), "messaging:Signal")
        self.assertEqual(c("signal-2026-11-14-10-20-30-123_002.mp4"), "messaging:Signal")
        self.assertEqual(c("signal.jpg"), "photo")
        self.assertEqual(c("photo_1@14-11-2026_10-20-30.jpg"), "messaging:Telegram")
        self.assertEqual(c("x/video_2@14-11-2026_10-20-30.mp4"), "messaging:Telegram")
        self.assertEqual(c("ChatExport_2026-11-14/video_files/holiday.MOV"), "messaging:Telegram")
        self.assertEqual(c("a/DataExport_2026-11-14 (2)/chats/chat_01/files/IMG_7.HEIC"), "messaging:Telegram")
        self.assertEqual(c("ChatExport_2026-11-14/x.jpg"), "photo")
        # rel alone can't show an export staged as the root itself: stage records export_app()'s answer.
        self.assertEqual(c("photos/IMG_1.jpg"), "photo")
        self.assertEqual(c("photos/IMG_1.jpg", "Telegram"), "messaging:Telegram")
        self.assertEqual(c("video_files/clip.MOV", "Telegram"), "messaging:Telegram")
        self.assertEqual(c("photos/photo_4@14-11-2026_10-20-30.jpg"), "messaging:Telegram")
        self.assertEqual(c("photos/IMG_1.jpg", None), "photo")

    def test_telegram_export_media_folders_only(self):
        """A Telegram Desktop export: photos/, video_files/ and files/ only, never its previews or own assets."""
        r = self.root
        chat, data = "Telegram/ChatExport_2026-11-14", "Telegram/DataExport_2026-11-14 (1)/chats/chat_001"
        good = {touch(r, x) for x in [
            f"{chat}/photos/photo_1@14-11-2026_10-20-30.jpg", f"{chat}/video_files/video_1@14-11-2026_10-21-00.mp4",
            f"{chat}/video_files/holiday.MOV", f"{chat}/files/IMG_0007.HEIC",
            f"{data}/photos/photo_2@01-10-2026_08-00-00.jpg"]}
        bad = [f"{chat}/photos/photo_1@14-11-2026_10-20-30_thumb.jpg",
               f"{chat}/video_files/video_1@14-11-2026_10-21-00.mp4_thumb.jpg",
               f"{chat}/stickers/sticker.webp", f"{chat}/animations/a.mp4", f"{chat}/round_video_messages/r.mp4",
               f"{chat}/voice_messages/audio_1@14-11-2026_10-22-00.ogg", f"{chat}/images/back.png",
               f"{chat}/css/style.css", f"{chat}/files/report.pdf", f"{chat}/x.jpg",
               "Telegram/DataExport_2026-11-14 (1)/profile_pictures/p.jpg",
               "Telegram/DataExport_2026-11-14 (1)/stories/s.mp4", f"{data}/photos/deeper/x.jpg"]
        for x in bad + [f"{chat}/result.json", f"{chat}/messages.html"]:
            touch(r, x)
        # Renamed export folder: known by result.json beside photos/; a saved web page (messages.html beside
        # files/, no photos/ or video_files/) is not an export, and everything in it is taken as usual.
        good |= {touch(r, x) for x in ["old chats/photos/photo_3@01-01-2025_09-00-00.jpg",
                                       "saved page/files/pic.png", "saved page/images/logo.png"]}
        for x in ["old chats/result.json", "old chats/stickers/s.webp", "saved page/messages.html"]:
            touch(r, x)
        got = {p for p, _ in sources.walk(r, "drive")}
        self.assertEqual(got, self.keep | good)
        for rel in bad:
            with self.assertRaises(sources.Denied, msg=rel):
                sources.check_drive(r, os.path.join(r, rel))
        # The source root itself is an export (named, or renamed with its result.json).
        for name in ("ChatExport_2026-11-14", "chat with a friend"):
            root = os.path.join(self.mount, name)
            ok = touch(root, "photos/photo_1@14-11-2026_10-20-30.jpg")
            for x in ["photos/photo_1@14-11-2026_10-20-30_thumb.jpg", "stickers/s.webp", "images/back.png",
                      "result.json"]:
                touch(root, x)
            self.assertEqual([p for p, _ in sources.walk(root, "drive")], [ok], name)
        # export_app: what stage records so plan's category sees an export at (or above) rel's top.
        for name in ("ChatExport_2026-11-14", "chat with a friend"):
            root = os.path.join(self.mount, name)
            touch(root, "video_files/holiday.MOV")
            self.assertEqual(sources.export_app(root, "video_files/holiday.MOV"), "Telegram", name)
            self.assertEqual(sources.drive_category("video_files/holiday.MOV",
                                                    sources.export_app(root, "video_files/holiday.MOV")),
                             "messaging:Telegram", name)
        self.assertEqual(sources.export_app(r, f"{chat}/video_files/holiday.MOV"), "Telegram")
        self.assertEqual(sources.export_app(r, "old chats/photos/photo_3@01-01-2025_09-00-00.jpg"), "Telegram")
        self.assertIsNone(sources.export_app(r, "saved page/files/pic.png"), "a saved web page is not an export")
        self.assertIsNone(sources.export_app(r, "2019/IMG_0001.JPG"))
        # A symlinked result.json marks nothing: the folder is walked as usual.
        link = os.path.join(self.mount, "linked")
        touch(link, "photos/a.jpg")
        touch(link, "stickers/s.webp")
        os.symlink(os.path.join(self.mount, "chat with a friend", "result.json"), os.path.join(link, "result.json"))
        self.assertEqual(sorted(rel for _, rel in sources.walk(link, "drive")), ["photos/a.jpg", "stickers/s.webp"])
        self.assertIsNone(sources.export_app(link, "photos/a.jpg"))

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

    def test_symlinked_parent_into_state_or_library_refused(self):
        """A link in a parent folder (not the last component) that leads into the state or the library:
        only the resolved path can tell, and it is what every check compares."""
        h = self.home
        for target, why in ((".local/share/media-intake", "media-intake state"), ("Pictures/library", "photo library")):
            os.makedirs(os.path.join(h, target, "sub"), exist_ok=True)
            link = os.path.join(h, "Documents", "pics-link-" + target.split("/")[-1])
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(os.path.join(h, target), link)
            self.refused(os.path.join(link, "sub"), why)

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


class LiveCaseMatches(unittest.TestCase):
    """live_case_matches: what the device lists now that is one name with a target."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.real = sources.list_dir

    def tearDown(self):
        sources.list_dir = self.real
        self.tmp.cleanup()

    def test_twins_of_the_file_and_of_its_folders(self):
        t = touch(self.root, "DCIM/Camera/IMG_1.jpg")
        twins = [touch(self.root, "DCIM/Camera/img_1.JPG"), touch(self.root, "DCIM/camera/IMG_1.jpg"),
                 touch(self.root, "dcim/CAMERA/Img_1.Jpg")]
        touch(self.root, "DCIM/Camera/IMG_2.jpg")
        touch(self.root, "DCIM/camera/IMG_3.jpg")
        self.assertEqual(sources.live_case_matches(self.root, [t]), sorted([t, *twins]))

    def test_only_the_target_when_unique(self):
        t = touch(self.root, "DCIM/Camera/IMG_1.jpg")
        touch(self.root, "DCIM/Camera/IMG_1.jpeg")
        self.assertEqual(sources.live_case_matches(self.root, [t]), [t])
        self.assertEqual(sources.live_case_matches(self.root, []), [])

    def test_unicode_case_and_normalisation(self):
        t = touch(self.root, "DCIM/café.jpg")
        nfd = touch(self.root, "DCIM/CAFE\u0301.JPG")
        self.assertEqual(sources.live_case_matches(self.root, [t]), sorted([t, nfd]))

    def test_file_where_a_folder_is_expected(self):
        t = touch(self.root, "DCIM/Camera/IMG_1.jpg")
        touch(self.root, "DCIM/camera")
        self.assertIn(os.path.join(self.root, "DCIM/camera/IMG_1.jpg"), sources.live_case_matches(self.root, [t]))

    def test_each_folder_listed_once(self):
        ts = [touch(self.root, f"DCIM/Camera/IMG_{i}.jpg") for i in range(5)]
        seen = []
        sources.list_dir = lambda d: seen.append(d) or self.real(d)
        self.assertEqual(sources.live_case_matches(self.root, ts), sorted(ts))
        self.assertEqual(len(seen), len(set(seen)))

    def test_fails_closed(self):
        t = touch(self.root, "DCIM/Camera/IMG_1.jpg")

        def broken(d):
            raise PermissionError(13, "Permission denied")
        sources.list_dir = broken
        with self.assertRaises(sources.ListingFailed) as cm:
            sources.live_case_matches(self.root, [t])
        self.assertIsInstance(cm.exception, sources.Denied)
        self.assertIn("Permission denied", str(cm.exception))
        sources.list_dir = lambda d: [] if d.endswith("Camera") else self.real(d)
        with self.assertRaisesRegex(sources.ListingFailed, "does not show DCIM/Camera/IMG_1.jpg"):
            sources.live_case_matches(self.root, [t])



class RootPresent(unittest.TestCase):
    """sources.root_present: a library folder counts as there only when it exists, holds something and, where
    drives mount, sits on a mounted drive (findmnt's answer, mocked here)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="root-present-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_missing_empty_and_populated(self):
        root = os.path.join(self.tmp, "lib")
        self.assertFalse(sources.root_present(root))
        os.makedirs(root)
        self.assertFalse(sources.root_present(root), "an empty folder: an unmounted drive's mountpoint")
        open(os.path.join(root, "a.jpg"), "w").close()
        self.assertTrue(sources.root_present(root))

    def test_under_a_drive_prefix_the_drive_must_be_mounted(self):
        media = os.path.join(self.tmp, "media")
        root = os.path.join(media, "user", "LABEL", "Videos")
        os.makedirs(root)
        open(os.path.join(root, "stale.txt"), "w").close()  # something written to the laptop disk by mistake
        with unittest.mock.patch.object(sources, "FOLDER_DRIVE_PREFIXES", (media,)):
            with unittest.mock.patch.object(sources, "drive_mount", return_value="/"):
                self.assertFalse(sources.root_present(root), "the laptop's own disk: the drive is not mounted")
            with unittest.mock.patch.object(sources, "drive_mount", return_value=media):
                self.assertFalse(sources.root_present(root), "the prefix itself is no drive")
            with unittest.mock.patch.object(sources, "drive_mount", return_value=os.path.join(media, "user", "LABEL")):
                self.assertTrue(sources.root_present(root))
            with unittest.mock.patch.object(sources, "drive_mount", side_effect=OSError("no findmnt")):
                self.assertTrue(sources.root_present(root), "no findmnt: the non-empty folder is all there is")

    def test_file_gone_uses_the_innermost_root(self):
        lib = os.path.join(self.tmp, "lib")
        proj = os.path.join(lib, "projects", "p")  # a project folder inside the library, on its own drive
        os.makedirs(proj)
        open(os.path.join(lib, "x.jpg"), "w").close()
        roots = [lib, proj]
        gone = os.path.join(proj, "v.mp4")
        self.assertFalse(sources.file_gone(gone, roots, sources.present_roots(roots)), "its own root is empty")
        open(os.path.join(proj, "w.mp4"), "w").close()
        self.assertTrue(sources.file_gone(gone, roots, sources.present_roots(roots)))
        self.assertFalse(sources.file_gone(os.path.join(self.tmp, "else", "y.jpg"), roots, []), "no root: kept")
        self.assertTrue(sources.file_gone(os.path.join(self.tmp, "y.jpg"), roots, [], outside=True))


if __name__ == "__main__":
    unittest.main()
