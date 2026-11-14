#!/usr/bin/env python3
"""Device identity and GoPro chapters (media_intake/devices.py, and their use in dates.py and
intake.py stage / plan / tag). Synthetic files only; every serial number here is made up.

  python3 -I -m unittest discover -s tests -p test_devices.py

Serial numbers are personal identifiers: the tests check that none reaches a folder name, a
batch name, plan.json, a sidecar or the command output. Cases needing exiftool or ffmpeg skip
when it is missing.
"""
import datetime, errno, glob, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
from test_dates import HAVE_EXIFTOOL, HAVE_FFMPEG, NOW, NY, jpg, mov  # noqa: E402
from test_intake import INTAKE, PY  # noqa: E402

import dates  # noqa: E402  (support put media_intake on sys.path)
import devices  # noqa: E402

D = datetime.datetime
SERIAL_A, SERIAL_B = "TESTSERIAL0001", "TESTSERIAL0002"  # synthetic
KEY = "ab" * 32


class Identity(unittest.TestCase):
    def test_make_and_model_slug(self):
        d = devices.identify({"Make": "GoPro", "Model": "HERO11  Black"})
        self.assertEqual((d.id, d.model, d.label), ("gopro-hero11-black", "HERO11 Black", "GoPro HERO11 Black"))
        self.assertEqual(devices.identify({"Make": "Canon", "Model": "Canon EOS R6"}).id, "canon-eos-r6")
        self.assertEqual(devices.identify({"Make": "GoPro"}).model, "GoPro")
        self.assertIsNone(devices.identify({}))
        self.assertIsNone(devices.identify({"SerialNumber": SERIAL_A}), "a serial alone names nothing")

    def test_serial_becomes_a_salted_pseudonym(self):
        meta = {"Make": "GoPro", "Model": "HERO11 Black", "SerialNumber": SERIAL_A}
        a = devices.identify(meta, KEY)
        self.assertRegex(a.id, r"^gopro-hero11-black-[0-9a-f]{6}$")
        self.assertEqual(a, devices.identify(dict(meta), KEY), "same camera, same salt: same id")
        self.assertNotEqual(a.id, devices.identify(dict(meta, SerialNumber=SERIAL_B), KEY).id)
        self.assertNotEqual(a.id, devices.identify(meta, "cd" * 32).id, "the salt is in the id")
        for field in (a.id, a.model, a.label):
            self.assertNotIn(SERIAL_A.casefold(), field.casefold())
        with self.assertRaises(ValueError):
            devices.identify(meta)  # a serial without the salt is never used raw

    def test_placeholder_serials_are_no_serial(self):
        for s in ("", "0", "0000000000", "Unknown", " N/A ", "--"):
            d = devices.identify({"Make": "GoPro", "Model": "HERO9 Black", "SerialNumber": s})
            self.assertEqual(d.id, "gopro-hero9-black", repr(s))
        self.assertEqual(devices.serial_of({"CameraSerialNumber": SERIAL_A}), SERIAL_A)

    def test_tag_and_folder_safe(self):
        d = devices.identify({"Make": "Ac|me\n", "Model": "Cam/../X 1"})
        self.assertEqual(d.model, "Cam/../X 1")
        self.assertNotIn("|", d.model + d.label)
        self.assertRegex(d.id, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(devices.identify({"Model": "X" * 200}).id), devices.SLUG_MAX)

    def test_strip_serials(self):
        m = {"Make": "GoPro", "SerialNumber": SERIAL_A, "CameraSerialNumber": SERIAL_B}
        self.assertEqual(devices.strip_serials(m), {"Make": "GoPro"})

    def test_batch_name(self):
        day = datetime.date(2026, 11, 13)
        a, b = devices.Device("gopro-hero11-black-aaaaaa", "HERO11 Black", ""), devices.Device("pixel-8", "Pixel 8", "")
        self.assertEqual(devices.batch_name([a, None, a, b], "gopro", day), "gopro-hero11-black-aaaaaa-20261113")
        self.assertEqual(devices.batch_name([b, a], "phone", day), "gopro-hero11-black-aaaaaa-20261113", "ties: first id")
        self.assertEqual(devices.batch_name([None], "drive", day), "drive-20261113")


class Salt(unittest.TestCase):
    def test_made_once_private_and_checked(self):
        with tempfile.TemporaryDirectory() as t:
            st = os.path.join(t, "state")
            s = devices.salt(st)
            self.assertRegex(s, r"^[0-9a-f]{64}$")
            self.assertEqual(devices.salt(st), s)
            p = os.path.join(st, devices.SALT_FILE)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(os.listdir(st), [devices.SALT_FILE], "no temp file left behind")
            with open(p, "w") as f:
                f.write("not hex\n")
            with self.assertRaises(ValueError):
                devices.salt(st)  # never silently replaced: every camera would get a new folder


    def test_filesystem_without_hard_links(self):
        """os.link failing with an OSError other than EEXIST (vfat, some FUSE mounts) falls back to an
        exclusive create: a private salt, made once, no temp file left, never another run's salt replaced."""
        with tempfile.TemporaryDirectory() as t:
            st = os.path.join(t, "state")
            with mock.patch.object(devices.os, "link", side_effect=OSError(errno.EPERM, "no hard links")) as link:
                s = devices.salt(st)
                self.assertEqual(devices.salt(st), s)
            self.assertEqual(link.call_count, 1, "the second call reads the salt the first made")
            self.assertRegex(s, r"^[0-9a-f]{64}$")
            p = os.path.join(st, devices.SALT_FILE)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(os.listdir(st), [devices.SALT_FILE], "no temp file left behind")
        with tempfile.TemporaryDirectory() as t:  # another run made it between our read and our create
            st = os.path.join(t, "state")
            theirs = "cd" * 32

            def race(src, dst):
                with open(dst, "w") as f:
                    f.write(theirs + "\n")
                raise OSError(errno.EOPNOTSUPP, "no hard links")
            with mock.patch.object(devices.os, "link", side_effect=race):
                self.assertEqual(devices.salt(st), theirs, "theirs is kept, never replaced")


class Chapters(unittest.TestCase):
    def test_gopro_chapter_names(self):
        for name, want in (("GX010042.MP4", ("GX", "0042", 0)), ("GX020042.MP4", ("GX", "0042", 1)),
                           ("GH120042.MP4", ("GH", "0042", 11)), ("GOPR0042.MP4", ("GP", "0042", 0)),
                           ("GP010042.MP4", ("GP", "0042", 1)), ("GP990042.MP4", ("GP", "0042", 99))):
            self.assertEqual(devices.chapter(name), want, name)

    def test_lookalikes_are_not_chapters(self):
        for name in ("GX000042.MP4", "GH000042.MP4", "GP000042.MP4",  # chapter 00 doesn't exist
                     "gx010042.mp4", "Gx010042.MP4", "GX010042.mp4",   # not how the camera writes it
                     "GX01042.MP4", "GX0100420.MP4", "GOPR042.MP4",     # wrong digit counts
                     "GX010042.JPG", "GX010042.LRV", "GL010042.LRV", "GX010042.THM", "GS010042.360",
                     "GOPR0042.JPG", "G0010042.JPG", "XGX010042.MP4", "GX010042.MP4.xmp", "GX010042 (1).MP4",
                     "GA010042.MP4", "GXAB0042.MP4", "DJI_0042.MP4", "VID_20261108_071502.mp4",
                     "PXL_20261108_071502123.mp4", "IMG_0042.MOV", "MVI_0042.MP4"):
            self.assertIsNone(devices.chapter(name), name)
        self.assertTrue(devices.gopro_name("GOPR0042.JPG") and devices.gopro_name("G0010042.JPG"))
        self.assertFalse(devices.gopro_name("IMG_0042.JPG") or devices.gopro_name("gopr0042.jpg"))

    def test_groups_by_folder_device_family_and_number(self):
        ps = ["/c/100GOPRO/GX010042.MP4", "/c/100GOPRO/GX020042.MP4", "/c/100GOPRO/GX030042.MP4",
              "/c/100GOPRO/GH010042.MP4", "/c/100GOPRO/GH020043.MP4",  # another family / another recording
              "/c/101GOPRO/GX020042.MP4",                              # another folder
              "/c/100GOPRO/GOPR0007.MP4", "/c/100GOPRO/GP010007.MP4", "/c/100GOPRO/GOPR0007.JPG"]
        self.assertEqual(devices.chapter_groups(ps), {
            "/c/100GOPRO/GX020042.MP4": "/c/100GOPRO/GX010042.MP4",
            "/c/100GOPRO/GX030042.MP4": "/c/100GOPRO/GX010042.MP4",
            "/c/100GOPRO/GP010007.MP4": "/c/100GOPRO/GOPR0007.MP4"})
        dev = {"/d/GX010001.MP4": "cam-a", "/d/GX020001.MP4": "cam-b"}
        self.assertEqual(devices.chapter_groups(dev, dev.get), {}, "two cameras' files never form one recording")

    def test_later_chapters_take_the_first_chapters_date(self):
        # One recording from 23:50 to 00:20 New York time: chapter 2 starts after midnight, in another month.
        g = "/card/DCIM/100GOPRO/"
        metas = {g + "GX010042.MP4": {"CreateDate": "2026:11:01 03:50:00"},   # 2026-10-31 23:50 EDT
                 g + "GX020042.MP4": {"CreateDate": "2026:11:01 04:10:00"},   # 2026-11-01 00:10 EDT
                 g + "GX030042.MP4": {"CreateDate": "2026:11:01 05:15:00"},   # 01:15 EDT
                 g + "GX010043.MP4": {"CreateDate": "2026:11:01 06:00:00"}}   # 01:00 EST: a recording of its own
        out = dates.resolve_all(list(metas), now=NOW, local_tz=NY, metas=metas)
        first = out[g + "GX010042.MP4"]
        self.assertEqual((first.dt, first.chapter_of), (D(2026, 10, 31, 23, 50), None))
        for n in ("GX020042.MP4", "GX030042.MP4"):
            self.assertEqual((out[g + n].dt, out[g + n].tz, out[g + n].src, out[g + n].chapter_of),
                             (first.dt, first.tz, "video", "GX010042.MP4"), n)
            self.assertIn("first chapter GX010042.MP4", out[g + n].note)
        self.assertEqual((out[g + "GX010043.MP4"].dt, out[g + "GX010043.MP4"].chapter_of), (D(2026, 11, 1, 1, 0), None))

    def test_a_suspect_first_chapter_makes_the_recording_suspect(self):
        metas = {"/c/GX010001.MP4": {"CreateDate": "2016:01:01 00:00:05"}, "/c/GX020001.MP4": {}}
        out = dates.resolve_all(list(metas), now=NOW, local_tz=NY, metas=metas)
        self.assertTrue(out["/c/GX020001.MP4"].suspect)
        self.assertIn("clock-reset default", out["/c/GX020001.MP4"].note)


class Reading(unittest.TestCase):
    def test_read_meta_replaces_the_serial_with_the_device(self):
        if not HAVE_EXIFTOOL:
            self.skipTest("serials are read only through exiftool")
        for writer in ("pillow", "exiftool"):
            with self.subTest(writer=writer), tempfile.TemporaryDirectory() as t:
                p = jpg(os.path.join(t, "GOPR0001.JPG"), "2026:11:08 07:00:00", make="GoPro",
                        model="HERO11 Black", serial=SERIAL_A, writer=writer)
                m = dates.read_meta([p], use_exiftool=None, state=os.path.join(t, "state"))[p]
                self.assertRegex(m["Device"], r"^gopro-hero11-black-[0-9a-f]{6}$")
                self.assertEqual((m["DeviceModel"], m["DeviceLabel"]), ("HERO11 Black", "GoPro HERO11 Black"))
                self.assertNotIn(SERIAL_A, json.dumps(m))
                self.assertEqual(m["Device"], devices.identify(
                    {"Make": "GoPro", "Model": "HERO11 Black", "SerialNumber": SERIAL_A},
                    devices.salt(os.path.join(t, "state"))).id)

    def test_fallbacks_ignore_serials_so_photos_and_videos_agree(self):
        """Without exiftool, Pillow could read a photo's serial but ffprobe never a video's: neither is read,
        so one camera's photos and videos get one id (one folder), and no salt is made for it."""
        with tempfile.TemporaryDirectory() as t:
            photo = jpg(os.path.join(t, "GOPR0001.JPG"), "2026:11:08 07:00:00", make="GoPro",
                        model="HERO11 Black", serial=SERIAL_A)
            vid = os.path.join(t, "GX010002.MP4")
            open(vid, "wb").close()
            probe = {"CreateDate": "2026-11-08T12:00:00Z", "Make": "GoPro", "Model": "HERO11 Black"}
            st = os.path.join(t, "state")
            with mock.patch.object(dates, "_ffprobe", return_value=dict(probe)):
                m = dates.read_meta([photo, vid], use_exiftool=False, state=st)
            self.assertEqual((m[photo]["Device"], m[vid]["Device"]), ("gopro-hero11-black", "gopro-hero11-black"))
            self.assertNotIn(SERIAL_A, json.dumps(m))
            self.assertNotIn("SerialNumber", dates._pil(photo))
            self.assertFalse(os.path.exists(st), "no serial was read, so no salt is needed")

    def test_no_serial_needs_no_salt(self):
        with tempfile.TemporaryDirectory() as t:
            p = jpg(os.path.join(t, "a.jpg"), "2026:11:08 07:00:00", make="GoPro", model="HERO11 Black")
            st = os.path.join(t, "state")
            self.assertEqual(dates.read_meta([p], state=st)[p]["Device"], "gopro-hero11-black")
            self.assertFalse(os.path.exists(st), "no salt is made until a serial needs one")


class EndToEnd(unittest.TestCase):
    """stage / plan / apply / tag on fake GoPro cards in a throwaway HOME."""
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="devices-e2e-")
        home = os.path.join(self.t, "home")
        os.makedirs(home)
        self.state = os.path.join(home, ".local/share/media-intake")
        self.videos = os.path.join(home, "Videos/personal")
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(home, "Pictures/library"), PERSONAL_FOOTAGE=self.videos,
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(home, "Videos/demo-footage")),
                        TZ="America/New_York", MEDIA_INTAKE_NO_LOCALSEARCH="1")
        self.out = []

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_intake(self, *args, ok=True):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.out.append(r.stdout + r.stderr)
        if ok:
            self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r

    def plan(self, batch):
        with open(os.path.join(self.state, "batches", batch, "plan.json")) as f:
            return {p["rel"]: p for p in json.load(f)}

    def card(self, name, serial, model="HERO11 Black", n="0001"):
        root = os.path.join(self.t, name)
        jpg(os.path.join(root, f"DCIM/100GOPRO/GOPR{n}.JPG"), "2026:11:08 07:00:00", offset="-05:00",
            make="GoPro", model=model, serial=serial)
        return root

    def assert_no_serial(self):
        """No raw serial in any output, state file, library path or sidecar."""
        blobs = list(self.out)
        for root, dirs, files in os.walk(self.t):
            for n in dirs + files:
                blobs.append(os.path.join(root, n))
                p = os.path.join(root, n)
                if n.endswith((".json", ".xmp", ".txt")) and os.path.isfile(p):
                    with open(p, errors="replace") as f:
                        blobs.append(f.read())
        for s in (SERIAL_A, SERIAL_B):
            self.assertFalse([b[:200] for b in blobs if s in b], f"raw serial {s} leaked")

    def test_two_gopros_same_numbers_get_device_folders_never_a_suffix(self):
        a, b = self.card("card-a", SERIAL_A), self.card("card-b", SERIAL_B)
        self.run_intake("stage", a, "--kind", "gopro", "--batch", "a")
        self.run_intake("plan", "a")
        self.run_intake("apply", "a", "--no-scan")
        self.run_intake("stage", b, "--kind", "gopro", "--batch", "b")
        out = self.run_intake("plan", "b").stdout
        pa, pb = self.plan("a")["DCIM/100GOPRO/GOPR0001.JPG"], self.plan("b")["DCIM/100GOPRO/GOPR0001.JPG"]
        month = os.path.join(self.videos, "2026", "2026-11")
        for p in (pa, pb):  # lib_dst: b's file may go to review first (same synthetic pixels as a's)
            self.assertRegex(p["device"], r"^gopro-hero11-black-[0-9a-f]{6}$")
            self.assertEqual((p["lib_dst"], p["device_model"]),
                             (os.path.join(month, p["device"], "GOPR0001.JPG"), "HERO11 Black"))
        self.assertNotEqual(pa["device"], pb["device"])
        self.assertIn(f"devices {{'{pb['device']}': 1}}", out)
        self.run_intake("apply", "b", "--no-scan")
        self.assertFalse(glob.glob(os.path.join(month, "**", "*_1.*"), recursive=True))
        self.assert_no_serial()

    def test_same_model_without_serials_shares_a_folder(self):
        # Nothing tells the two cameras apart: one folder, and the second file gets _1 (documented).
        a = self.card("card-a", None)
        b = os.path.join(self.t, "card-b")
        jpg(os.path.join(b, "DCIM/100GOPRO/GOPR0001.JPG"), "2026:11:08 09:00:00", make="GoPro", model="HERO11 Black")
        self.run_intake("stage", a, "--kind", "gopro", "--batch", "a")
        self.run_intake("plan", "a")
        self.run_intake("apply", "a", "--no-scan")
        self.run_intake("stage", b, "--kind", "gopro", "--batch", "b")
        self.run_intake("plan", "b")
        dst = self.plan("b")["DCIM/100GOPRO/GOPR0001.JPG"]["lib_dst"]
        self.assertEqual(dst, os.path.join(self.videos, "2026", "2026-11", "gopro-hero11-black", "GOPR0001_1.JPG"))

    def test_stage_names_the_batch_by_device(self):
        a = self.card("card-a", SERIAL_A)
        out = self.run_intake("stage", a, "--kind", "gopro").stdout
        m = re.search(r"^batch (gopro-hero11-black-[0-9a-f]{6}-\d{8})$", out, re.M)
        self.assertTrue(m, out)
        name = m.group(1)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "batches", name, "manifest.json")))
        self.assertEqual(self.run_intake("stage", a, "--kind", "gopro").stdout.splitlines()[0], f"batch {name}",
                         "re-staging the same card resumes its batch")
        b = os.path.join(self.t, "copy-of-a")
        shutil.copytree(a, b)
        self.assertEqual(self.run_intake("stage", b, "--kind", "gopro").stdout.splitlines()[0], f"batch {name}-2",
                         "another root never merges into an existing batch")
        r = self.run_intake("stage", a, "--kind", "gopro", "--batch", "../x", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assert_no_serial()

    def test_clock_offset_by_device_id(self):
        a, b = self.card("card-a", SERIAL_A), self.card("card-b", SERIAL_B, n="0002")
        drive = os.path.join(self.t, "both")
        for c, n in ((a, "0001"), (b, "0002")):
            os.makedirs(os.path.join(drive, "DCIM/100GOPRO"), exist_ok=True)
            shutil.copy(os.path.join(c, f"DCIM/100GOPRO/GOPR{n}.JPG"), os.path.join(drive, "DCIM/100GOPRO"))
        self.run_intake("stage", drive, "--kind", "gopro", "--batch", "both")
        self.run_intake("plan", "both")
        dev = self.plan("both")["DCIM/100GOPRO/GOPR0001.JPG"]["device"]
        self.run_intake("plan", "both", "--clock-offset", f"{dev}=+2h")
        p = self.plan("both")
        self.assertEqual((p["DCIM/100GOPRO/GOPR0001.JPG"]["date_src"], p["DCIM/100GOPRO/GOPR0001.JPG"]["date"]),
                         ("offset", "2026-11-08T09:00:00"))
        self.assertEqual(p["DCIM/100GOPRO/GOPR0002.JPG"]["date_src"], "exif", "the other HERO11 keeps its clock")
        r = self.run_intake("plan", "both", "--clock-offset", "gopro-hero11-black-000000=+2h", ok=False)
        self.assertIn(f"GoPro HERO11 Black ({dev})", r.stderr, "the refusal lists the ids it saw")
        self.assert_no_serial()

    @unittest.skipUnless(HAVE_EXIFTOOL, "exiftool missing (tags are written with it)")
    def test_source_model_tag_in_the_sidecar(self):
        a = self.card("card-a", SERIAL_A)
        self.run_intake("stage", a, "--kind", "gopro", "--batch", "a")
        self.run_intake("plan", "a")
        self.run_intake("apply", "a", "--no-scan")
        self.run_intake("tag", "a", "--no-labels")
        dst = self.plan("a")["DCIM/100GOPRO/GOPR0001.JPG"]["dst"]

        def tags():
            r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", dst + ".xmp"],
                               capture_output=True, text=True, check=True)
            v = json.loads(r.stdout)[0].get("HierarchicalSubject", [])
            return {v} if isinstance(v, str) else set(v)
        self.assertTrue({"Source|gopro", "Source|HERO11 Black"} <= tags(), tags())
        self.run_intake("tag-files", dst, "--no-labels")  # a re-tag reads the model from the file itself
        self.assertTrue({"Source|gopro", "Source|HERO11 Black"} <= tags(), tags())
        self.assert_no_serial()

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_chapters_across_midnight_and_a_month_stay_together(self):
        g = os.path.join(self.t, "card", "DCIM", "100GOPRO")
        mov(os.path.join(g, "GX010042.MP4"), "2026-11-01T03:50:00Z")  # 2026-10-31 23:50 New York
        mov(os.path.join(g, "GX020042.MP4"), "2026-11-01T04:10:00Z")  # 2026-11-01 00:10
        mov(os.path.join(g, "GX010043.MP4"), "2026-11-01T06:00:00Z")  # its own recording
        self.run_intake("stage", os.path.join(self.t, "card"), "--kind", "gopro", "--batch", "c")
        out = self.run_intake("plan", "c", "--event", "night-ride@2026-10-31").stdout
        p = self.plan("c")
        one, two, other = (p[f"DCIM/100GOPRO/{n}"] for n in ("GX010042.MP4", "GX020042.MP4", "GX010043.MP4"))
        self.assertEqual((two["date"], two["chapter_of"], two["event"]), (one["date"], "GX010042.MP4", "night-ride"))
        self.assertEqual(os.path.dirname(one["dst"]), os.path.dirname(two["dst"]))
        self.assertIn(os.path.join("2026", "2026-10"), two["dst"])
        self.assertIn(os.path.join("2026", "2026-11"), other["dst"])
        self.assertIn("1 GoPro chapters dated by their first chapter: DCIM/100GOPRO/GX020042.MP4", out)


if __name__ == "__main__":
    unittest.main()
