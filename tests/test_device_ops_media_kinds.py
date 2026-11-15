#!/usr/bin/env python3
"""The device and drive side of intake.py with photos, videos and a mix, for every source kind.

  python3 -I -m unittest discover -s tests -p test_device_ops_media_kinds.py

stage, plan, apply, erase (--phone, --keep-phone-thumbnails, --drive, --no-wipe-free), offload,
verify-gone and wipe-free, plus the drive queue, on a fake phone (camera roll, screenshots, screen
recordings, a messaging app's folders), a fake GoPro card (chapters, .LRV/.THM previews, bursts), a
fake external drive and a laptop folder, in a throwaway HOME. Pictures are Pillow patterns and videos
ffmpeg test patterns (ffmpeg is in the CI image); scan.json is written by hand, labels only.
MEDIA_INTAKE_TEST_MOUNT/UUID stand in for findmnt; MEDIA_INTAKE_WIPE_ANY_DEST lets wipe-free write
to a temp folder, capped by MEDIA_INTAKE_WIPE_MAX_MB.
"""
import hashlib, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
from test_intake import INTAKE, PY, pattern  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

HERE = os.path.dirname(INTAKE)
NY = ZoneInfo("America/New_York")
WA_IMG, WA_VID = support.EXTRA  # the configured messaging folders (WhatsApp Images, WhatsApp Video)


def jload(p):
    with open(p) as f:
        return json.load(f)


def jsave(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(obj, f)


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def photo(path, seed, dt=None, make=None, model=None, size=(800, 600), im=None):
    """A JPEG pattern with DateTimeOriginal (written as a whole Exif IFD: Pillow 10.2 keeps that) and Make/Model."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ex = Image.Exif()
    if make:
        ex[271] = make
    if model:
        ex[272] = model
    if dt:
        ex[0x8769] = {36867: dt}
    (im or pattern(seed, size)).save(path, quality=90, exif=ex)
    return path


def clip(path, seed, created=None, make=None, model=None, mtime=None):
    """A 1 s ffmpeg test pattern. created: QuickTime creation_time (UTC, as the spec says); make/model as
    QuickTime Keys; mtime: a local wall time (datetime) for the file's modification time. The tone's
    pitch follows seed, so two seeds never give the same bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    md = ["-metadata", f"creation_time={created}"] if created else []
    if make or model:
        md += ["-movflags", "use_metadata_tags", "-metadata", f"com.apple.quicktime.make={make}",
               "-metadata", f"com.apple.quicktime.model={model}"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                    "-f", "lavfi", "-i", f"sine=frequency={300 + 40 * seed}:duration=1", "-shortest",
                    "-c:v", "mpeg4", "-c:a", "aac", *md, path], check=True)
    if mtime:
        t = mtime.replace(tzinfo=NY).timestamp()
        os.utime(path, (t, t))
    return path


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def files_under(root):
    return sorted(os.path.relpath(os.path.join(r, f), root) for r, _, fs in os.walk(root) for f in fs)


class Base(unittest.TestCase):
    """A throwaway HOME, state, library, videos and demo project folder; intake.py run as the CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="device-ops-")
        t = self.tmp
        self.home = os.path.join(t, "home")
        os.makedirs(self.home)
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.footage = os.path.join(self.home, "Videos/demo-footage")
        self.project = os.path.join(self.footage, "originals")
        self.backup = os.path.join(t, "backup-drive")  # erase --drive / wipe-free target
        os.makedirs(self.backup)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.footage),
                        TZ="America/New_York", MEDIA_INTAKE_WIPE_ANY_DEST="1", MEDIA_INTAKE_WIPE_MAX_MB="2",
                        MEDIA_INTAKE_NO_LOCALSEARCH="1")
        for k in ("MEDIA_INTAKE_TEST_MOUNT", "MEDIA_INTAKE_TEST_UUID"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0, answer=None, env=None):
        """answer: what is typed at a prompt (None: stdin is closed, as under a cron job or the queue)."""
        r = subprocess.run([PY, "-I", INTAKE, *args], env=env or self.env, capture_output=True, text=True,
                           **({"stdin": subprocess.DEVNULL} if answer is None else {"input": answer}))
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr if code else r.stdout

    def bd(self, batch):
        return os.path.join(self.state, "batches", batch)

    def plan(self, batch):
        return {p["rel"]: p for p in jload(os.path.join(self.bd(batch), "plan.json"))}

    def flag(self, batch, rels):
        jsave(os.path.join(self.bd(batch), "scan.json"), {r: ["nudity"] for r in rels})

    def apply(self, batch, flagged=()):
        self.flag(batch, flagged)
        return self.intake("apply", batch, "--allow-missing-detectors")

    def V(self, *p):
        return os.path.join(self.vids, *p)

    def L(self, *p):
        return os.path.join(self.lib, *p)


# ---------------------------------------------------------------- phone
class Phone(Base):
    """An Android phone: camera roll photos and videos, screenshots, screen recordings, WhatsApp images and videos."""

    PHOTO = "DCIM/Camera/20261020_120000.jpg"
    VID_UTC = "DCIM/Camera/clip_utc.mp4"            # 2026-11-01 02:30 UTC = 2026-10-31 22:30 in New York
    VID_MTIME = "DCIM/Camera/clip_nodate.mp4"       # no QuickTime date, no date in the name: mtime
    SHOT = "DCIM/Screenshots/Screenshot_20261101-101010.png"
    SCREENREC = "DCIM/Screen recordings/Screen_Recording_20261102-090000.mp4"
    WA_VIDEO = f"{WA_VID}/VID-20261021-WA0001.mp4"
    WA_VIDEO_DUP = f"{WA_VID}/VID-20261101-WA0002.mp4"   # the same bytes as VID_UTC, shared over WhatsApp
    WA_PHOTO = f"{WA_IMG}/IMG-20261021-WA0003.jpg"
    SENS_VID = "DCIM/Camera/20261023_100000.mp4"
    SENS_VID_COPY = "DCIM/Restored/20261023_100000.mp4"
    SENS_PHOTO = "DCIM/Camera/20261024_100000.jpg"

    def setUp(self):
        super().setUp()
        self.phone = os.path.join(self.tmp, "phone", "Internal storage")
        P = self.P
        photo(P(self.PHOTO), 1, "2026:10:20 12:00:00")
        clip(P(self.VID_UTC), 1, "2026-11-01T02:30:00Z")
        clip(P(self.VID_MTIME), 2, mtime=datetime(2025, 6, 15, 12, 0))
        os.makedirs(os.path.dirname(P(self.SHOT)))
        pattern(6, (1080, 2340)).save(P(self.SHOT))
        clip(P(self.SCREENREC), 3, "2026-11-02T14:00:00Z")
        clip(P(self.WA_VIDEO), 4, "2026-10-21T14:00:00Z")
        copy(P(self.VID_UTC), P(self.WA_VIDEO_DUP))
        photo(P(self.WA_PHOTO), 5)
        clip(P(self.SENS_VID), 6, "2026-10-23T14:00:00Z")
        copy(P(self.SENS_VID), P(self.SENS_VID_COPY))
        photo(P(self.SENS_PHOTO), 7, "2026:10:24 10:00:00")
        for rel in ("DCIM/.thumbnails/1.jpg", "Pictures/.thumbnails/2.jpg"):
            os.makedirs(os.path.dirname(P(rel)), exist_ok=True)
            pattern(9, (64, 64)).save(P(rel))
        self.all = [self.PHOTO, self.VID_UTC, self.VID_MTIME, self.SHOT, self.SCREENREC, self.WA_VIDEO,
                    self.WA_VIDEO_DUP, self.WA_PHOTO, self.SENS_VID, self.SENS_VID_COPY, self.SENS_PHOTO]

    def P(self, rel):
        return os.path.join(self.phone, rel)

    def staged(self, batch, *plan_args):
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch, *plan_args)
        return self.plan(batch)

    def test_stage_plan_apply_file_photos_and_videos_by_kind(self):
        b = "phone-mixed"
        before = files_under(self.phone)
        out = self.intake("stage", self.phone, "--kind", "phone", "--batch", b)
        man = jload(os.path.join(self.bd(b), "manifest.json"))
        self.assertEqual(sorted(man["files"]), sorted(self.all), out)
        for rel, m in man["files"].items():
            self.assertEqual((m["src"], m["size"]), (self.P(rel), os.path.getsize(self.P(rel))), rel)
            self.assertEqual(sha(os.path.join(self.bd(b), "raw", rel)), sha(self.P(rel)), rel)
        self.intake("plan", b)
        p = self.plan(b)
        # Dates: QuickTime is UTC, turned into local time (it crosses into the previous month here); mtime last.
        self.assertEqual((p[self.VID_UTC]["date_src"], p[self.VID_UTC]["date"]), ("video", "2026-10-31T22:30:00"))
        self.assertEqual((p[self.VID_MTIME]["date_src"], p[self.VID_MTIME]["date"]), ("mtime", "2025-06-15T12:00:00"))
        self.assertEqual(p[self.PHOTO]["date_src"], "exif")
        cats = {r: p[r]["cat"] for r in p}
        self.assertEqual((cats[self.PHOTO], cats[self.VID_UTC], cats[self.SHOT], cats[self.SCREENREC],
                          cats[self.WA_VIDEO], cats[self.WA_PHOTO]),
                         ("photo", "video", "screenshot", "screenrec", "messaging:WhatsApp", "messaging:WhatsApp"))
        # Exact duplicates: the camera video is kept over its WhatsApp copy, the camera roll over Restored/.
        self.assertEqual((p[self.WA_VIDEO_DUP]["action"], p[self.WA_VIDEO_DUP]["why"]),
                         ("delete", f"duplicate of {self.VID_UTC}"))
        self.assertEqual((p[self.SENS_VID_COPY]["action"], p[self.SENS_VID_COPY]["why"]),
                         ("delete", f"duplicate of {self.SENS_VID}"))
        for r in (self.VID_UTC, self.VID_MTIME, self.SCREENREC, self.WA_VIDEO, self.SENS_VID):
            self.assertEqual(p[r]["action"], "file", r)
            self.assertIsNone(p[r]["dhash"], "videos get no dHash")
        out = self.apply(b)
        self.assertIn("'filed': 9", out)
        self.assertIn("'deleted (exact duplicate)': 2", out)
        want = {self.L("2026", "2026-10", "20261020_120000.jpg"), self.V("2026", "2026-10", "clip_utc.mp4"),
                self.V("2025", "2025-06", "clip_nodate.mp4"),
                self.L("Screenshots", "2026", "Screenshot_20261101-101010.png"),
                self.V("Screen recordings", "2026", "Screen_Recording_20261102-090000.mp4"),
                self.L("Messaging", "WhatsApp", "2026", "VID-20261021-WA0001.mp4"),
                self.L("Messaging", "WhatsApp", "2026", "IMG-20261021-WA0003.jpg"),
                self.V("2026", "2026-10", "20261023_100000.mp4"), self.L("2026", "2026-10", "20261024_100000.jpg")}
        for f in want:
            self.assertTrue(os.path.isfile(f), f)
        index = jload(os.path.join(self.state, "index.json"))
        self.assertEqual(set(index), want)
        self.assertEqual(index[self.V("2026", "2026-10", "clip_utc.mp4")]["sha"], sha(self.P(self.VID_UTC)))
        self.assertEqual(files_under(self.phone), before, "the phone is only read")
        # A later batch from another phone with the same video: a duplicate of the library copy, never filed twice.
        other = os.path.join(self.tmp, "phone2")
        copy(self.P(self.VID_UTC), os.path.join(other, "DCIM/Camera/clip_utc.mp4"))
        self.intake("stage", other, "--kind", "phone", "--batch", "phone-2")
        self.intake("plan", "phone-2")
        row = self.plan("phone-2")["DCIM/Camera/clip_utc.mp4"]
        self.assertEqual((row["action"], row["why"]),
                         ("delete", "duplicate of library " + self.V("2026", "2026-10", "clip_utc.mp4")))

    def test_event_routes_phone_photo_and_video_by_their_local_day(self):
        b = "phone-event"
        # The UTC clip is on 2026-11-01 in UTC but 2026-10-31 in New York: the event day is the local one.
        p = self.staged(b, "--event", "demo/river-shoot@2026-10-31", "--event", "birthday@2026-10-20")
        shoot = os.path.join(self.project, "2026-10-31-river-shoot", "phone")
        self.assertEqual(p[self.VID_UTC]["dst"], os.path.join(shoot, "clip_utc.mp4"))
        self.assertEqual((p[self.VID_UTC]["event"], p[self.VID_UTC]["project"]), ("river-shoot", "Demo"))
        # A personal event (no project) only tags: the photo stays in its month folder.
        self.assertEqual(p[self.PHOTO]["dst"], self.L("2026", "2026-10", "20261020_120000.jpg"))
        self.assertEqual((p[self.PHOTO]["event"], p[self.PHOTO]["project"]), ("birthday", None))
        # A screen recording of an event day is not camera footage: it stays in Screen recordings.
        p = self.staged(b, "--event", "demo/river-shoot@2026-11-02")
        self.assertEqual(p[self.SCREENREC]["dst"],
                         self.V("Screen recordings", "2026", "Screen_Recording_20261102-090000.mp4"))
        self.assertIsNone(p[self.SCREENREC]["project"])
        # Photo and video of one event day both go to the project's phone/ folder.
        photo(self.P("DCIM/Camera/20261031_090000.jpg"), 11, "2026:10:31 09:00:00")
        self.staged(b, "--event", "demo/river-shoot@2026-10-31")
        self.apply(b)
        self.assertEqual(sorted(os.listdir(shoot)), ["20261031_090000.jpg", "clip_utc.mp4"])

    def quarantined(self, batch, rels):
        self.staged(batch)
        out = self.apply(batch, rels)
        return out

    def test_erase_a_quarantined_video_and_photo_from_phone_drive_home_and_thumbnails(self):
        b = "phone-erase"
        out = self.quarantined(b, [self.SENS_VID, self.SENS_VID_COPY, self.SENS_PHOTO])
        self.assertIn("'quarantined': 2", out)
        quar = os.path.join(self.state, "quarantine", b)
        self.assertEqual(sorted(jload(os.path.join(quar, "quarantine.json"))), [self.SENS_VID, self.SENS_PHOTO])
        self.assertFalse(os.path.exists(self.V("2026", "2026-10", "20261023_100000.mp4")), "a flagged video is not filed")
        vsha, vsize = sha(self.P(self.SENS_VID)), os.path.getsize(self.P(self.SENS_VID))
        outside = copy(self.P(self.SENS_VID), os.path.join(self.tmp, "outside", "v.mp4"))  # outside HOME: kept
        home_copy = copy(self.P(self.SENS_VID), os.path.join(self.home, "Downloads", "sent.mp4"))
        trash_copy = copy(self.P(self.SENS_VID), os.path.join(self.home, ".local/share/Trash/files/old.mp4"))
        drive_vid = copy(self.P(self.SENS_VID), os.path.join(self.backup, "videos", "2026", "a.mp4"))
        drive_pic = copy(self.P(self.SENS_PHOTO), os.path.join(self.backup, "photos", "b.jpg"))
        kept_on_drive = copy(self.P(self.VID_UTC), os.path.join(self.backup, "videos", "2026", "kept.mp4"))
        out = self.intake("erase", b, "--phone", self.phone, "--drive", self.backup, "--yes")
        self.assertIn("erased 2 files; library copies 0; phone 3; drive 2", out)
        for gone in (self.SENS_VID, self.SENS_VID_COPY, self.SENS_PHOTO, "DCIM/.thumbnails", "Pictures/.thumbnails"):
            self.assertFalse(os.path.exists(self.P(gone)), gone)
        for keep in (self.PHOTO, self.VID_UTC, self.WA_VIDEO, self.SCREENREC):
            self.assertTrue(os.path.isfile(self.P(keep)), keep)
        for gone in (home_copy, trash_copy, drive_vid, drive_pic, quar):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertTrue(os.path.isfile(kept_on_drive))
        self.assertIn("home sweep: 2 more copies deleted", out)
        self.assertIn("drive free space wiped: 2 MB", out)
        self.assertIn("videos: only exact copies are found", out)
        self.assertNotIn("near-duplicates (resized", out, "an erased video moves no image")
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            self.assertIn(f"sha256:{vsha} size:{vsize}\n", f.read())
        self.assertIn("no copy of 2 erased items in HOME or on the drive", out)
        # verify-gone finds a video copy that comes back, on the drive or in HOME, and only reads.
        self.assertIn("no copy of 2 erased items", self.intake("verify-gone", b, "--drive", self.backup))
        again = copy(outside, os.path.join(self.backup, "videos", "again.mp4"))
        out = self.intake("verify-gone", b, "--drive", self.backup, code=1)
        self.assertIn("STILL PRESENT: 1 copies: drive:videos 1", out)
        self.assertTrue(os.path.isfile(again), "verify-gone is read-only")
        os.remove(again)
        desk = copy(outside, os.path.join(self.home, "Desktop", "x.mp4"))
        self.assertIn("STILL PRESENT: 1 copies: Desktop 1", self.intake("verify-gone", code=1))
        self.assertTrue(os.path.isfile(desk))

    def test_erase_keeps_phone_thumbnails_and_skips_the_wipe_when_asked(self):
        b = "phone-erase-keep"
        self.quarantined(b, [self.SENS_VID])
        dcopy = copy(self.P(self.SENS_VID), os.path.join(self.backup, "v.mp4"))
        out = self.intake("erase", b, "--phone", self.phone, "--keep-phone-thumbnails",
                          "--drive", self.backup, "--no-wipe-free", "--yes")
        self.assertIn("erased 1 files; library copies 0; phone 2; drive 1", out)
        self.assertFalse(os.path.exists(dcopy))
        self.assertTrue(os.path.isfile(self.P("DCIM/.thumbnails/1.jpg")))
        self.assertTrue(os.path.isfile(self.P("Pictures/.thumbnails/2.jpg")))
        self.assertNotIn("phone thumbnail folders removed", out)
        self.assertNotIn("free space", out)
        self.assertEqual(os.listdir(self.backup), [], "nothing written to the drive without the wipe")

    def test_erase_a_video_already_in_the_library_from_an_earlier_batch(self):
        """The same video filed by an earlier batch is the same content: erase removes the library copy too."""
        self.staged("phone-first")
        self.apply("phone-first")
        filed = self.V("2026", "2026-10", "clip_utc.mp4")
        with open(filed + ".xmp", "w") as f:
            f.write("tags")
        other = os.path.join(self.tmp, "phone2")
        copy(self.P(self.VID_UTC), os.path.join(other, "DCIM/Camera/clip_utc.mp4"))
        self.intake("stage", other, "--kind", "phone", "--batch", "phone-second")
        self.intake("plan", "phone-second")
        # plan marks it a duplicate of the library copy, so apply never quarantines it: the record is written
        # by hand, as a later scan of the library would (forget is lane C's).
        q = os.path.join(self.state, "quarantine", "phone-second")
        qf = copy(filed, os.path.join(q, "DCIM/Camera/clip_utc.mp4"))
        jsave(os.path.join(q, "quarantine.json"), {"DCIM/Camera/clip_utc.mp4": dict(
            reasons=["nudity"], sha=sha(qf), size=os.path.getsize(qf), dst=None)})
        out = self.intake("erase", "phone-second", "--phone", other, "--yes", "--keep-phone-thumbnails")
        self.assertIn("erased 1 files; library copies 1; phone 1", out)
        self.assertFalse(os.path.exists(filed) or os.path.exists(filed + ".xmp"))
        self.assertNotIn(filed, jload(os.path.join(self.state, "index.json")))
        self.assertFalse(os.path.exists(os.path.join(other, "DCIM/Camera/clip_utc.mp4")))

    def test_offload_after_apply_frees_photo_and_video_originals(self):
        b = "phone-offload"
        self.quarantined(b, [self.SENS_VID, self.SENS_VID_COPY])
        out = self.intake("offload", b, "--phone", self.phone, code=1)  # no --yes, no answer: nothing deleted
        self.assertIn("10 of 11 phone files have a verified copy here", out)
        self.assertEqual(len([r for r in self.all if os.path.exists(self.P(r))]), len(self.all), "nothing without a yes")
        out = self.intake("offload", b, "--phone", self.phone, "--yes")
        # Filed photos and videos (camera, screenshot, screen recording, WhatsApp) and the exact duplicates go;
        # the quarantined video's original stays until erase.
        self.assertIn("deleted 10 files from the phone", out)
        left = [r for r in self.all if os.path.exists(self.P(r))]
        self.assertEqual(left, [self.SENS_VID])
        self.assertTrue(os.path.isfile(self.P("DCIM/.thumbnails/1.jpg")), "offload never touches thumbnails")

    def test_offload_skips_a_video_whose_filed_copy_changed_size(self):
        b = "phone-offload-size"
        self.staged(b)
        self.apply(b)
        filed = self.plan(b)[self.VID_UTC]["dst"]
        with open(filed, "ab") as f:
            f.write(b"\0")  # the library copy is no longer the staged copy
        out = self.intake("offload", b, "--phone", self.phone, "--yes")
        self.assertTrue(os.path.isfile(self.P(self.VID_UTC)), "no verified copy: the phone original stays")
        self.assertIn("deleted 10 files from the phone", out)

    def test_video_in_a_screenshots_folder_is_a_screen_recording(self):
        """Some phones (Xiaomi, OnePlus, older Samsung) save screen recordings next to screenshots."""
        for rel in ("DCIM/Screenshots/Screenrecorder-2026-11-03-10-00-00.mp4",
                    "Pictures/Screenshots/Screen_recording_20261103_100000.mp4"):
            clip(self.P(rel), 12 + len(rel) % 7, "2026-11-03T15:00:00Z")
        pattern(13, (1080, 2340)).save(self.P("Pictures/Screenshots/Screenshot_20261103-100000.png"))
        p = self.staged("phone-screenrec")
        for rel in ("DCIM/Screenshots/Screenrecorder-2026-11-03-10-00-00.mp4",
                    "Pictures/Screenshots/Screen_recording_20261103_100000.mp4"):
            self.assertEqual(p[rel]["cat"], "screenrec", rel)
            self.assertEqual(p[rel]["dst"], self.V("Screen recordings", "2026", os.path.basename(rel)), rel)
        self.assertEqual(p[self.SHOT]["cat"], "screenshot")
        self.assertEqual(p["Pictures/Screenshots/Screenshot_20261103-100000.png"]["cat"], "screenshot")


# ---------------------------------------------------------------- GoPro
class GoPro(Base):
    """A GoPro card: a two-chapter recording across midnight and a month end, .LRV/.THM previews, a photo, a burst."""

    G = "DCIM/100GOPRO/"
    DEV = "gopro-hero11-black"

    def setUp(self):
        super().setUp()
        self.card = os.path.join(self.tmp, "card")
        C = self.C
        kw = dict(make="GoPro", model="HERO11 Black")
        # 2026-11-01 03:50 UTC = 2026-10-31 23:50 EDT; the second chapter starts after midnight.
        clip(C(self.G + "GX010042.MP4"), 1, "2026-11-01T03:50:00Z", **kw)
        clip(C(self.G + "GX020042.MP4"), 2, "2026-11-01T04:10:00Z", **kw)
        clip(C(self.G + "GX010043.MP4"), 3, "2026-10-20T15:00:00Z", **kw)
        for n in ("GL010042.LRV", "GX010042.THM", "GL020042.LRV", "GX020042.THM", "GL010043.LRV", "GX010043.THM"):
            with open(C(self.G + n), "wb") as f:
                f.write(b"preview " + n.encode())
        photo(C(self.G + "GOPR0044.JPG"), 4, "2026:10:31 12:00:00", **kw)
        a = pattern(5)
        b = a.copy()
        ImageDraw.Draw(b).rectangle([600, 100, 640, 140], fill=(255, 255, 255))
        photo(C(self.G + "G0010045.JPG"), 5, "2026:10:31 12:01:00", im=a, **kw)
        photo(C(self.G + "G0010046.JPG"), 5, "2026:10:31 12:01:01", im=b, **kw)

    def C(self, rel):
        return os.path.join(self.card, rel)

    def staged(self, batch, *plan_args):
        out = self.intake("stage", self.card, "--kind", "gopro", "--batch", batch)
        self.intake("plan", batch, *plan_args)
        return out, self.plan(batch)

    def test_stage_skips_previews_and_plan_files_a_recording_together(self):
        out, p = self.staged("gopro-1")
        self.assertIn("skipped 6 gopro preview (.lrv/.thm)", out)
        self.assertEqual(sorted(p), sorted(self.G + n for n in ("GX010042.MP4", "GX020042.MP4", "GX010043.MP4",
                                                               "GOPR0044.JPG", "G0010045.JPG", "G0010046.JPG")))
        first, second = p[self.G + "GX010042.MP4"], p[self.G + "GX020042.MP4"]
        self.assertEqual((first["date"], first["date_src"], first["device"]), ("2026-10-31T23:50:00", "video", self.DEV))
        self.assertEqual(second["chapter_of"], "GX010042.MP4")
        self.assertEqual(second["date"], first["date"], "a later chapter takes the first one's date")
        month = self.V("2026", "2026-10", self.DEV)
        self.assertEqual((first["dst"], second["dst"]),
                         (os.path.join(month, "GX010042.MP4"), os.path.join(month, "GX020042.MP4")))
        self.apply("gopro-1")
        self.assertEqual(sorted(os.listdir(month)), ["G0010045.JPG", "G0010046.JPG", "GOPR0044.JPG",
                                                     "GX010042.MP4", "GX010043.MP4", "GX020042.MP4"])
        self.assertTrue(os.path.isfile(self.C(self.G + "GL010042.LRV")), "the card is only read")

    def test_burst_frames_are_never_merged_as_near_duplicates(self):
        """A GoPro burst or time-lapse is camera footage like a phone burst: every frame is kept."""
        _, p = self.staged("gopro-burst")
        for n in ("G0010045.JPG", "G0010046.JPG"):
            self.assertEqual((p[self.G + n]["action"], p[self.G + n]["why"]), ("file", ""), n)

    def test_two_cameras_with_the_same_file_numbers_get_two_folders(self):
        self.staged("gopro-a")
        self.apply("gopro-a")
        card2 = os.path.join(self.tmp, "card2")
        clip(os.path.join(card2, self.G + "GX010042.MP4"), 9, "2026-10-31T16:00:00Z", make="GoPro", model="HERO12 Black")
        self.intake("stage", card2, "--kind", "gopro", "--batch", "gopro-b")
        self.intake("plan", "gopro-b")
        self.apply("gopro-b")
        self.assertTrue(os.path.isfile(self.V("2026", "2026-10", self.DEV, "GX010042.MP4")))
        self.assertTrue(os.path.isfile(self.V("2026", "2026-10", "gopro-hero12-black", "GX010042.MP4")))
        self.assertFalse(os.path.exists(self.V("2026", "2026-10", self.DEV, "GX010042_1.MP4")))

    def test_event_routes_footage_to_the_projects_gopro_device_folder(self):
        _, p = self.staged("gopro-event", "--event", "demo/race-shoot@2026-10-31")
        gp = os.path.join(self.project, "2026-10-31-race-shoot", "gopro", self.DEV)
        for n in ("GX010042.MP4", "GX020042.MP4", "GOPR0044.JPG"):
            self.assertEqual(p[self.G + n]["dst"], os.path.join(gp, n), n)
            self.assertEqual((p[self.G + n]["event"], p[self.G + n]["project"]), ("race-shoot", "Demo"), n)
        self.assertEqual(p[self.G + "GX010043.MP4"]["dst"], self.V("2026", "2026-10", self.DEV, "GX010043.MP4"))
        self.apply("gopro-event")
        self.assertEqual(sorted(os.listdir(gp)), ["G0010045.JPG", "G0010046.JPG", "GOPR0044.JPG",
                                                  "GX010042.MP4", "GX020042.MP4"])

    def test_erase_a_quarantined_chapter_takes_its_previews_off_the_card(self):
        """The .LRV is a low-resolution copy of the recording and the .THM its first frame: erasing a
        sensitive chapter from the card removes both, and only that chapter's."""
        b = "gopro-erase"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        out = self.intake("erase", b, "--phone", self.card, "--yes")
        self.assertIn("erased 1 files; library copies 0; phone 1;", out)
        self.assertIn("camera previews removed: 2", out)
        left = files_under(self.card)
        for n in ("GX010042.MP4", "GL010042.LRV", "GX010042.THM"):
            self.assertNotIn(self.G + n, left, n)
        for n in ("GX020042.MP4", "GL020042.LRV", "GX020042.THM", "GX010043.MP4", "GL010043.LRV", "GX010043.THM"):
            self.assertIn(self.G + n, left, n)

    def test_erase_leaves_the_previews_of_a_chapter_it_skips(self):
        """A chapter no longer the staged size is skipped by the gate: its previews stay with it."""
        b = "gopro-erase-changed"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        with open(self.C(self.G + "GX010042.MP4"), "ab") as f:
            f.write(b"\0")  # re-recorded or edited on the card since stage
        out = self.intake("erase", b, "--phone", self.card, "--yes")
        self.assertIn("phone 0;", out)
        self.assertNotIn("camera previews removed", out)
        for n in ("GX010042.MP4", "GL010042.LRV", "GX010042.THM"):
            self.assertTrue(os.path.isfile(self.C(self.G + n)), n)

    def test_erase_refuses_a_preview_with_a_case_only_twin(self):
        b = "gopro-erase-case"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        with open(self.C(self.G + "gl010042.lrv"), "wb") as f:
            f.write(b"twin")
        before = files_under(self.card)
        out = self.intake("erase", b, "--phone", self.card, "--yes", code=1)
        self.assertIn("differ only by letter case", out)
        self.assertIn("nothing deleted from the device", out)
        self.assertEqual(files_under(self.card), before, "the chapter and every preview stay")
        self.assertTrue(os.path.isdir(os.path.join(self.state, "quarantine", b)), "nothing was erased")

    def test_erase_never_follows_a_symlinked_preview(self):
        b = "gopro-erase-link"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        thm = self.C(self.G + "GX010042.THM")
        os.remove(thm)
        os.symlink(self.C(self.G + "GX010043.THM"), thm)
        out = self.intake("erase", b, "--phone", self.card, "--yes")
        self.assertIn("skip phone delete, symlink: " + self.G + "GX010042.THM", out)
        self.assertIn("camera previews removed: 1", out)
        self.assertTrue(os.path.islink(thm) and os.path.isfile(self.C(self.G + "GX010043.THM")))
        self.assertFalse(os.path.exists(self.C(self.G + "GL010042.LRV")))

    def test_erase_without_yes_takes_nothing_off_the_card(self):
        """Without --yes erase asks; any answer but ERASE, or no answer at all (stdin closed), deletes
        nothing: not the chapter, not its previews, not the quarantined copy."""
        b = "gopro-erase-ask"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        before = files_under(self.card)
        quar = os.path.join(self.state, "quarantine", b)
        for answer in ("no\n", "erase\n", "", None):
            out = self.intake("erase", b, "--phone", self.card, code=1, answer=answer)
            self.assertIn("nothing erased", out, repr(answer))
            self.assertNotIn("Traceback", out, repr(answer))
            self.assertEqual(files_under(self.card), before, repr(answer))
            self.assertTrue(os.path.isfile(os.path.join(quar, self.G + "GX010042.MP4")), repr(answer))

    def test_erase_twice_is_safe(self):
        """A second erase of the same batch finds the quarantine gone and touches nothing on the card."""
        b = "gopro-erase-twice"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        self.intake("erase", b, "--phone", self.card, "--yes")
        after = files_under(self.card)
        out = self.intake("erase", b, "--phone", self.card, "--yes", code=1)
        self.assertIn("quarantine is empty", out)
        self.assertEqual(files_under(self.card), after)
        self.assertIn(self.G + "GL020042.LRV", after, "the other chapter's previews stay")

    def test_erase_stopped_by_a_failed_preview_delete_finishes_on_the_next_run(self):
        """A device that refuses one delete stops erase. The previews go before the chapter, so a run
        stopped part way still has the chapter on the card, and the next run (which finds the previews
        beside it) takes what is left. Chapter first would leave the .LRV, a copy of the footage, behind
        for good: the next run finds no chapter and so looks for no previews."""
        b = "gopro-erase-retry"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        gio = shutil.which("gio")
        if not gio:
            self.skipTest("gio missing (the CI image has it)")
        stub = os.path.join(self.tmp, "stub-bin")
        os.makedirs(stub)
        with open(os.path.join(stub, "gio"), "w") as f:  # refuses any .LRV, like a busy or read-only file
            f.write(f'#!/bin/sh\ncase "$2" in *.LRV) exit 1;; esac\nexec "{gio}" "$@"\n')
        os.chmod(os.path.join(stub, "gio"), 0o700)
        env = dict(self.env, PATH=stub + os.pathsep + self.env.get("PATH", ""))
        out = self.intake("erase", b, "--phone", self.card, "--yes", code=1, env=env)
        self.assertIn("gio remove failed", out)
        self.assertTrue(os.path.isfile(self.C(self.G + "GX010042.MP4")), "the chapter waits for its previews")
        out = self.intake("erase", b, "--phone", self.card, "--yes")
        self.assertIn("phone 1;", out)
        self.assertIn("camera previews removed: 2", out)
        left = files_under(self.card)
        for n in ("GX010042.MP4", "GL010042.LRV", "GX010042.THM"):
            self.assertNotIn(self.G + n, left, n)

    def test_erase_never_touches_a_chapter_or_preview_in_a_denied_folder(self):
        """A manifest naming a chapter under a denied folder (hand-edited, or from another tool): the
        chapter is skipped by the allowlist, so its .LRV/.THM are never looked for, and nothing there
        is touched."""
        b = "gopro-erase-denied"
        self.staged(b)
        self.apply(b, [self.G + "GX010042.MP4"])
        hidden = "Android/data/"
        copy(self.C(self.G + "GX010042.MP4"), self.C(hidden + "GX010042.MP4"))
        for n in ("GL010042.LRV", "GX010042.THM"):
            copy(self.C(self.G + n), self.C(hidden + n))
        mp = os.path.join(self.bd(b), "manifest.json")
        man = jload(mp)
        man["files"][self.G + "GX010042.MP4"]["src"] = self.C(hidden + "GX010042.MP4")
        jsave(mp, man)
        before = files_under(self.card)
        out = self.intake("erase", b, "--phone", self.card, "--yes")
        self.assertIn("skip phone delete, denied folder: " + hidden + "GX010042.MP4", out)
        self.assertIn("phone 0;", out)
        self.assertNotIn("camera previews removed", out)
        self.assertEqual(files_under(self.card), before)

    def test_offload_frees_the_card_of_videos_and_photos(self):
        b = "gopro-offload"
        self.staged(b)
        self.apply(b)
        out = self.intake("offload", b, "--phone", self.card, "--yes")
        self.assertIn("deleted 6 files from the phone", out)
        left = files_under(self.card)
        self.assertFalse([f for f in left if f.endswith((".MP4", ".JPG"))])
        self.assertEqual(len([f for f in left if f.endswith((".LRV", ".THM"))]), 6, "offload leaves previews")

    def test_a_gopro_photo_near_a_library_photo_is_filed_a_screenshot_goes_to_review(self):
        phone = os.path.join(self.tmp, "phone")
        a = pattern(20)
        photo(os.path.join(phone, "DCIM/Camera/20261030_100000.jpg"), 20, "2026:10:30 10:00:00", im=a)
        self.intake("stage", phone, "--kind", "phone", "--batch", "lib-first")
        self.intake("plan", "lib-first")
        self.apply("lib-first")
        near = a.copy()
        ImageDraw.Draw(near).rectangle([600, 100, 640, 140], fill=(255, 255, 255))
        photo(self.C(self.G + "GOPR0050.JPG"), 20, "2026:10:30 10:00:05", im=near, make="GoPro", model="HERO11 Black")
        _, p = self.staged("gopro-near-lib")
        self.assertEqual(p[self.G + "GOPR0050.JPG"]["action"], "file", "camera footage is never a near-duplicate")
        shutil.rmtree(phone)
        os.makedirs(os.path.join(phone, "DCIM/Screenshots"))
        near.save(os.path.join(phone, "DCIM/Screenshots/Screenshot_20261030-100010.png"))
        self.intake("stage", phone, "--kind", "phone", "--batch", "shot-near-lib")
        self.intake("plan", "shot-near-lib")
        row = self.plan("shot-near-lib")["DCIM/Screenshots/Screenshot_20261030-100010.png"]
        self.assertEqual((row["action"], row["why"]),
                         ("review", "near-duplicate of library " + self.L("2026", "2026-10", "20261030_100000.jpg")))


class PreviewNames(unittest.TestCase):
    """devices.previews names the .LRV/.THM a GoPro writes beside a chapter; sources.check_preview gates them."""

    def test_names_per_family(self):
        import devices
        self.assertEqual(devices.previews("GX010042.MP4"), ["GL010042.LRV", "GX010042.THM"])
        self.assertEqual(devices.previews("GH020042.MP4"), ["GL020042.LRV", "GH020042.THM"])
        self.assertEqual(devices.previews("GOPR0042.MP4"), ["GOPR0042.LRV", "GOPR0042.THM"])
        self.assertEqual(devices.previews("GP010042.MP4"), ["GP010042.LRV", "GP010042.THM"])
        for n in ("GOPR0042.JPG", "gx010042.mp4", "GX000042.MP4", "VID_0042.MP4", "GL010042.LRV"):
            self.assertEqual(devices.previews(n), [], n)

    def test_check_preview_allows_previews_only_where_media_is_allowed(self):
        import sources
        r = "/card"
        sources.check_preview(r, "/card/DCIM/100GOPRO/GL010042.LRV")
        sources.check_preview(r, "/card/DCIM/100GOPRO/GX010042.THM")
        for bad in ("/card/DCIM/100GOPRO/GX010042.MP4", "/card/Android/data/GL010042.LRV",
                    "/card/DCIM/Vault/GL010042.LRV", "/elsewhere/GL010042.LRV"):
            with self.assertRaises(sources.Denied, msg=bad):
                sources.check_preview(r, bad)
        with self.assertRaises(sources.Denied):
            sources.check(r, "/card/DCIM/100GOPRO/GL010042.LRV")  # never a media target

    def test_a_preview_in_a_denied_folder_is_never_a_device_target(self):
        """erase gates previews through device_targets with check_preview: one in a denied folder is
        skipped with a line, never returned for deletion, even with no size to check."""
        import intake
        import sources
        root = tempfile.mkdtemp(prefix="preview-gate-")
        self.addCleanup(shutil.rmtree, root, True)
        ok = os.path.join(root, "DCIM/100GOPRO/GL010042.LRV")
        bad = [os.path.join(root, d, "GL010042.LRV") for d in ("Android/data", "DCIM/Vault", "Download")]
        for p in [ok] + bad:
            copy(__file__, p)
        buf = io.StringIO()
        with redirect_stdout(buf):
            todo = intake.device_targets(root, [(p, intake.NO_SIZE_CHECK) for p in bad + [ok]], [],
                                         sources.check_preview)
        self.assertEqual(todo, [ok])
        self.assertEqual(buf.getvalue().count("skip phone delete, denied"), 3, buf.getvalue())
        self.assertTrue(all(os.path.isfile(p) for p in bad))


# ---------------------------------------------------------------- external drive (kind drive)
class Drive(Base):
    """An old external drive (not the backup): photos, videos, a WhatsApp video; erase and offload on it."""

    def setUp(self):
        super().setUp()
        self.mount = os.path.join(self.tmp, "old-drive")
        self.root = os.path.join(self.mount, "Phone backup 2019")
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=self.mount, MEDIA_INTAKE_TEST_UUID="1234-ABCD")
        R = self.R
        photo(R("2019/IMG_0001.jpg"), 1, "2019:07:04 12:00:00")
        clip(R("2019/VID_0001.mp4"), 1, "2019-07-04T16:00:00Z")
        clip(R("2019/VID_0002.mp4"), 2, "2019-07-05T16:00:00Z")   # flagged
        clip(R("WhatsApp/VID-20190706-WA0001.mp4"), 3, "2019-07-06T16:00:00Z")
        clip(R("$RECYCLE.BIN/VID_9.mp4"), 4, "2019-07-07T16:00:00Z")   # system folder: never read or deleted

    def R(self, rel):
        return os.path.join(self.root, rel)

    def test_drive_photos_and_videos_end_to_end(self):
        b = "drive-1"
        self.intake("stage", self.root, "--kind", "drive", "--batch", b)
        man = jload(os.path.join(self.bd(b), "manifest.json"))
        self.assertEqual(sorted(man["files"]), ["2019/IMG_0001.jpg", "2019/VID_0001.mp4", "2019/VID_0002.mp4",
                                                "WhatsApp/VID-20190706-WA0001.mp4"])
        self.assertEqual((man["kind"], man["uuid"]), ("drive", "1234-ABCD"))
        self.intake("plan", b)
        p = self.plan(b)
        self.assertEqual(p["2019/VID_0001.mp4"]["dst"], self.V("2019", "2019-07", "VID_0001.mp4"))
        self.assertEqual(p["2019/IMG_0001.jpg"]["dst"], self.L("2019", "2019-07", "IMG_0001.jpg"))
        self.assertEqual(p["WhatsApp/VID-20190706-WA0001.mp4"]["dst"],
                         self.L("Messaging", "WhatsApp", "2019", "VID-20190706-WA0001.mp4"))
        self.apply(b, ["2019/VID_0002.mp4"])
        bcopy = copy(self.R("2019/VID_0002.mp4"), os.path.join(self.backup, "x", "VID_0002.mp4"))
        out = self.intake("erase", b, "--phone", self.root, "--drive", self.backup, "--no-wipe-free", "--yes")
        self.assertIn("erased 1 files; library copies 0; phone 1; drive 1", out)
        self.assertFalse(os.path.exists(self.R("2019/VID_0002.mp4")) or os.path.exists(bcopy))
        # offload: the same drive (UUID) only, then every filed original goes, photo and video alike.
        self.env["MEDIA_INTAKE_TEST_UUID"] = "OTHER-0000"
        self.assertIn("UUID", self.intake("offload", b, "--phone", self.root, "--yes", code=1))
        self.env["MEDIA_INTAKE_TEST_UUID"] = "1234-ABCD"
        out = self.intake("offload", b, "--phone", self.root, "--yes")
        self.assertIn("deleted 3 files from the phone", out)
        self.assertEqual(files_under(self.root), ["$RECYCLE.BIN/VID_9.mp4"])


# ---------------------------------------------------------------- laptop folder (kind folder)
class Folder(Base):
    """A folder on the laptop: never offloaded or deleted from as a device, but erase's HOME sweep reaches it."""

    def test_folder_photos_and_videos_and_a_quarantined_video(self):
        f = os.path.join(self.home, "Camera import")
        photo(os.path.join(f, "IMG_0001.jpg"), 1, "2024:05:01 10:00:00")
        clip(os.path.join(f, "MVI_0001.mp4"), 1, "2024-05-01T14:00:00Z")
        clip(os.path.join(f, "MVI_0002.mp4"), 2, "2024-05-02T14:00:00Z")
        b = "folder-1"
        self.intake("stage", f, "--kind", "folder", "--batch", b)
        self.intake("plan", b, "--event", "demo/may-shoot@2024-05-01")
        p = self.plan(b)
        shoot = os.path.join(self.project, "2024-05-01-may-shoot", "phone")
        self.assertEqual(p["IMG_0001.jpg"]["dst"], os.path.join(shoot, "IMG_0001.jpg"))
        self.assertEqual(p["MVI_0001.mp4"]["dst"], os.path.join(shoot, "MVI_0001.mp4"))
        self.apply(b, ["MVI_0002.mp4"])
        for args in (["offload", b, "--phone", f, "--yes"], ["erase", b, "--phone", f, "--yes"]):
            self.assertIn("never deletes", self.intake(*args, code=1), args)
        self.assertEqual(sorted(os.listdir(f)), ["IMG_0001.jpg", "MVI_0001.mp4", "MVI_0002.mp4"])
        out = self.intake("erase", b, "--yes")
        self.assertIn("home sweep: 1 more copies deleted", out)
        self.assertRegex(out, r"\n  Camera import +1\n")
        self.assertEqual(sorted(os.listdir(f)), ["IMG_0001.jpg", "MVI_0001.mp4"])


# ---------------------------------------------------------------- the drive queue with videos
class QueueWithVideos(Base):
    """drive_queue.py over a drive of photos and videos: counts, chunks through the real stage/plan/apply, offload."""

    KEYS = ("HOME", "MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG",
            "MEDIA_INTAKE_TEST_MOUNT", "MEDIA_INTAKE_TEST_UUID", "MEDIA_QUEUE_WRAPPER", "MEDIA_INTAKE_NO_LOCALSEARCH", "TZ")

    def setUp(self):
        super().setUp()
        self.mount = os.path.join(self.tmp, "old-drive")
        self.root = os.path.join(self.mount, "Videos and photos")
        photo(os.path.join(self.root, "2018", "IMG_0001.jpg"), 1, "2018:03:01 10:00:00")
        clip(os.path.join(self.root, "2018", "VID_0001.mp4"), 1, "2018-03-01T15:00:00Z")
        clip(os.path.join(self.root, "2018", "VID_0002.mp4"), 2, "2018-03-02T15:00:00Z")
        clip(os.path.join(self.root, "WhatsApp", "VID-20180303-WA0001.mp4"), 3, "2018-03-03T15:00:00Z")
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ.update({k: self.env[k] for k in self.KEYS if k in self.env},
                          MEDIA_INTAKE_TEST_MOUNT=self.mount, MEDIA_INTAKE_TEST_UUID="1234-ABCD",
                          MEDIA_QUEUE_WRAPPER="fake-heavy")
        sys.modules.pop("review_ui", None)
        spec = importlib.util.spec_from_file_location("drive_queue_media_kinds", os.path.join(HERE, "drive_queue.py"))
        self.q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.q)
        self.q.free_bytes = lambda: 10**15
        self.q.run_cmd = self.fake_run

    def tearDown(self):
        sys.modules.pop("review_ui", None)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def fake_run(self, cmd, log):
        """The real intake.py for stage, plan and apply; scan finds nothing, tag/index/vlm are simulated."""
        if cmd[0] == "fake-heavy":
            cmd = cmd[1:]
        q = self.q
        if q.INTAKE not in cmd:
            return 0 if q.SEMANTIC in cmd else 2  # index done; the VLM (Ollama) is not running
        step, batch = cmd[cmd.index(q.INTAKE) + 1], cmd[cmd.index(q.INTAKE) + 2]
        if step == "scan":
            jsave(os.path.join(self.bd(batch), "scan.json"), {})
            return 0
        if step == "tag":
            return 0
        if step == "apply":
            cmd = cmd + ["--allow-missing-detectors"]
        with open(log, "a") as f:
            return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode

    def qrun(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            self.q.main(list(args))
        return out.getvalue()

    def test_queue_counts_files_and_offloads_photos_and_videos(self):
        out = self.qrun("add", self.root, "--label", "old")
        self.assertIn("queued old: 4 files", out)
        self.assertIn("(messaging 1, photo 1, video 2)", out)
        self.qrun("run", "--chunk", "2")
        self.assertTrue(os.path.isfile(self.V("2018", "2018-03", "VID_0001.mp4")))
        self.assertTrue(os.path.isfile(self.V("2018", "2018-03", "VID_0002.mp4")))
        self.assertTrue(os.path.isfile(self.L("2018", "2018-03", "IMG_0001.jpg")))
        self.assertTrue(os.path.isfile(self.L("Messaging", "WhatsApp", "2018", "VID-20180303-WA0001.mp4")))
        out = self.qrun("offload", "old", "--yes")
        self.assertIn("chunk 2:", out)
        self.assertEqual(files_under(self.root), [])


if __name__ == "__main__":
    unittest.main()
