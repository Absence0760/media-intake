#!/usr/bin/env python3
"""End-to-end test of intake.py on synthetic media in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_intake.py

stage -> plan -> apply -> quarantine -> release -> purge-review -> erase ->
offload -> forget -> share-copy, against a fake phone root, a fake backup
drive and a fake ~/.cache/thumbnails. Images are drawn with Pillow, videos are
ffmpeg test patterns: no real photos are read. scan.json is written by hand
(labels only) for the quarantine path; the real `scan` runs once when the
NudeNet venv exists, and the OCR/barcode cases only when those tools are
installed.
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
INTAKE = os.path.join(HERE, "intake.py")
REAL_VENV = os.path.join(os.path.expanduser("~"), ".local/share/media-intake/venv")
PY = sys.executable

from PIL import Image, ImageDraw  # noqa: E402

DAY = "2026-11-08"  # the demo project's shoot
EVENT = "city-marathon"


def read(p):
    with open(p) as f:
        return f.read()


def jload(p):
    return json.loads(read(p))


def have_zxing():
    return subprocess.run([PY, "-c", "import zxingcpp"], capture_output=True).returncode == 0


def pattern(seed, size=(1200, 900)):
    """A deterministic picture with enough structure for dHash."""
    im = Image.new("RGB", size, (20 * seed % 255, 90, 160))
    d = ImageDraw.Draw(im)
    w, h = size
    for i in range(12):
        x = (i * 97 * (seed + 3)) % w
        y = (i * 61 * (seed + 5)) % h
        d.rectangle([x, y, x + w // 6, y + h // 5], fill=((i * 40 + seed * 70) % 255, (i * 23) % 255, (seed * 50) % 255))
    d.ellipse([w // 3, h // 3, w // 3 + w // 4, h // 3 + h // 4], fill=(250, 240, 30))
    return im


def exif_for(dt, gps=False):
    ex = Image.Exif()
    ex[0x0110] = "SyntheticCam"
    ex.get_ifd(0x8769)[36867] = dt
    if gps:
        g = ex.get_ifd(0x8825)
        g[1], g[2] = "N", (37.0, 32.0, 10.0)
        g[3], g[4] = "W", (77.0, 26.0, 5.0)
    return ex


def save_jpg(im, path, dt=None, gps=False, quality=92):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kw = dict(quality=quality)
    if dt:
        kw["exif"] = exif_for(dt, gps)
    im.save(path, **kw)
    if gps and dt and shutil.which("exiftool"):
        # Older Pillow (e.g. Ubuntu 24.04's 10.x) drops the GPS IFD on save; write it with exiftool too.
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-GPSLatitude=37.536", "-GPSLatitudeRef=N",
                        "-GPSLongitude=77.4347", "-GPSLongitudeRef=W", path], check=True)


def video(path, created, seconds=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest",
                    "-c:v", "mpeg4", "-c:a", "aac", "-metadata", f"creation_time={created}", path], check=True)


class IntakeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="intake-test-")
        t = cls.tmp
        cls.home = os.path.join(t, "home")
        cls.state = os.path.join(cls.home, ".local/share/media-intake")
        cls.lib = os.path.join(cls.home, "Pictures/library")
        cls.vids = os.path.join(cls.home, "Videos/personal")
        cls.footage = os.path.join(cls.home, "Videos/demo-footage")
        cls.phone = os.path.join(t, "phone", "Internal storage")
        cls.drive = os.path.join(t, "drive")
        cls.out = os.path.join(t, "share-out")
        os.makedirs(cls.home)
        cls.env = dict(os.environ, HOME=cls.home, MEDIA_INTAKE_STATE=cls.state, PHOTO_LIBRARY=cls.lib,
                       PERSONAL_FOOTAGE=cls.vids, MEDIA_INTAKE_CONFIG=support.write_config(cls.footage), TZ="America/New_York",
                       MEDIA_INTAKE_WIPE_ANY_DEST="1", MEDIA_INTAKE_WIPE_MAX_MB="2",
                       MEDIA_INTAKE_NO_LOCALSEARCH="1")
        P = lambda rel: os.path.join(cls.phone, rel)  # noqa: E731
        # Camera roll: a burst pair (similar, both kept), a GPS photo on
        # another day, an exact duplicate in DCIM/Restored, and a video each day.
        a = pattern(1)
        burst = a.copy()
        ImageDraw.Draw(burst).rectangle([600, 100, 640, 140], fill=(255, 255, 255))
        save_jpg(a, P("DCIM/Camera/20261108_071502.jpg"), "2026:11:08 07:15:02")
        save_jpg(burst, P("DCIM/Camera/20261108_071503.jpg"), "2026:11:08 07:15:03")
        os.makedirs(P("DCIM/Restored"))
        shutil.copyfile(P("DCIM/Camera/20261108_071502.jpg"), P("DCIM/Restored/20261108_071502.jpg"))
        b = pattern(2)
        save_jpg(b, P("DCIM/Camera/20261020_120000.jpg"), "2026:10:20 12:00:00", gps=True)
        video(P("DCIM/Camera/20261108_150000.mp4"), "2026-11-08T15:00:00Z")
        video(P("DCIM/Camera/20261021_090000.mp4"), "2026-10-21T14:00:00Z")
        # Files the test will mark sensitive (labels only; the pixels are patterns).
        save_jpg(pattern(3), P("DCIM/Camera/20261022_090000.jpg"), "2026:10:22 09:00:00")  # released later
        save_jpg(pattern(4), P("DCIM/Camera/20261023_100000.jpg"), "2026:10:23 10:00:00")  # erased
        shutil.copyfile(P("DCIM/Camera/20261023_100000.jpg"), P("DCIM/Restored/20261023_100000.jpg"))
        save_jpg(pattern(5), P("Pictures/WhatsApp/IMG-20261021-WA0003.jpg"))  # erased
        # Screenshot, WhatsApp recompressed copy of b (near-dup), a sticker (junk).
        os.makedirs(P("DCIM/Screenshots"))
        pattern(6, (1080, 2340)).save(P("DCIM/Screenshots/Screenshot_20261101-101010.png"))
        save_jpg(b.resize((600, 450)), P("Pictures/WhatsApp/IMG-20261020-WA0001.jpg"), quality=55)
        pattern(7, (50, 50)).save(P("Pictures/WhatsApp/STK-20261020-WA0002.webp"))
        # Decoys: must never be staged, read for content, or deleted.
        cls.decoys = [P(x) for x in ("Android/data/com.example/files/a.jpg", "Download/x.jpg",
                                     "Documents/y.png", "DCIM/Bitwarden/vault.png", "Pictures/Bitwarden/export.png")]
        for d in cls.decoys:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            pattern(9, (300, 300)).save(d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_intake(self, *args, ok=True):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        if ok:
            self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r

    def L(self, *p):
        return os.path.join(self.lib, *p)

    def test_end_to_end(self):
        batch = "2026-11-13-testphone"
        bd = os.path.join(self.state, "batches", batch)
        # ---- stage
        out = self.run_intake("stage", self.phone, "--kind", "phone", "--batch", batch).stdout
        man = jload(os.path.join(bd, "manifest.json"))
        staged = set(man["files"])
        self.assertEqual(len(staged), 13, out)
        for d in self.decoys:
            self.assertNotIn(os.path.relpath(d, self.phone), staged)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700, "state root must be 0700")
        # ---- plan
        self.run_intake("plan", batch, "--event", f"demo/{EVENT}@{DAY}")
        plan = {p["rel"]: p for p in jload(os.path.join(bd, "plan.json"))}
        act = {k: v["action"] for k, v in plan.items()}
        self.assertEqual(act["DCIM/Restored/20261108_071502.jpg"], "delete")
        self.assertEqual(act["DCIM/Restored/20261023_100000.jpg"], "delete")
        self.assertEqual(act["DCIM/Camera/20261108_071502.jpg"], "file")
        self.assertEqual(act["DCIM/Camera/20261108_071503.jpg"], "file", "burst shots must not be merged")
        self.assertEqual(act["Pictures/WhatsApp/IMG-20261020-WA0001.jpg"], "review")
        self.assertIn("near-duplicate of DCIM/Camera/20261020_120000.jpg",
                      plan["Pictures/WhatsApp/IMG-20261020-WA0001.jpg"]["why"])
        # A near-duplicate still gets a library place, so a flagged one the owner keeps can be released.
        self.assertTrue(plan["Pictures/WhatsApp/IMG-20261020-WA0001.jpg"]["lib_dst"].endswith(
            os.path.join("Messaging", "WhatsApp", "2026", "IMG-20261020-WA0001.jpg")))
        self.assertIsNone(plan["DCIM/Restored/20261023_100000.jpg"]["lib_dst"], "exact duplicates have no place")
        self.assertEqual(plan["Pictures/WhatsApp/STK-20261020-WA0002.webp"]["why"], "junk: tiny or empty")
        self.assertEqual(plan["DCIM/Camera/20261108_150000.mp4"]["date_src"], "video")
        # ---- scan.json by hand: three sensitive files (and the Restored copy, as scan would)
        flags = {"DCIM/Camera/20261022_090000.jpg": ["passport-mrz"],
                 "DCIM/Camera/20261023_100000.jpg": ["nudity"],
                 "DCIM/Restored/20261023_100000.jpg": ["nudity"],
                 "Pictures/WhatsApp/IMG-20261021-WA0003.jpg": ["payment-card"]}
        with open(os.path.join(bd, "scan.json"), "w") as f:
            json.dump(flags, f)
        # ---- apply
        out = self.run_intake("apply", batch, "--allow-missing-detectors").stdout
        self.assertIn("'quarantined': 3", out)
        self.assertFalse(os.path.exists(os.path.join(bd, "raw")), "raw must be removed once applied")
        shoot = os.path.join(self.footage, "originals", f"{DAY}-{EVENT}", "phone")
        self.assertEqual(sorted(os.listdir(shoot)),
                         ["20261108_071502.jpg", "20261108_071503.jpg", "20261108_150000.mp4"])
        self.assertTrue(os.path.isfile(self.L("2026", "2026-10", "20261020_120000.jpg")))
        self.assertTrue(os.path.isfile(self.L("Screenshots", "2026", "Screenshot_20261101-101010.png")))
        self.assertTrue(os.path.isfile(os.path.join(self.vids, "2026", "2026-10", "20261021_090000.mp4")))
        self.assertEqual(sorted(os.listdir(os.path.join(bd, "review", "Pictures", "WhatsApp"))),
                         ["IMG-20261020-WA0001.jpg", "STK-20261020-WA0002.webp"])
        quar = os.path.join(self.state, "quarantine", batch)
        self.assertEqual(stat.S_IMODE(os.stat(quar).st_mode), 0o700)
        qf = os.path.join(quar, "DCIM/Camera/20261023_100000.jpg")
        self.assertEqual(stat.S_IMODE(os.stat(qf).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.L("2026", "2026-10", "20261023_100000.jpg")))
        gps = read(os.path.join(bd, "gps.txt")).split()
        self.assertEqual(gps, [self.L("2026", "2026-10", "20261020_120000.jpg")])
        # ---- quarantine (counts only)
        out = self.run_intake("quarantine", batch).stdout
        self.assertIn("3 files in quarantine", out)
        for label in ("passport-mrz", "nudity", "payment-card"):
            self.assertIn(label, out)
        # ---- release a false positive
        out = self.run_intake("release", batch, "DCIM/Camera/20261022_090000.jpg").stdout
        released = self.L("2026", "2026-10", "20261022_090000.jpg")
        self.assertIn(released, out)
        self.assertTrue(os.path.isfile(released))
        index = jload(os.path.join(self.state, "index.json"))
        self.assertIsNotNone(index[released]["dhash"])
        # ---- purge-review
        self.assertIn("deleted 2", self.run_intake("purge-review", batch).stdout)
        self.assertFalse(os.path.exists(os.path.join(bd, "review")))
        # ---- erase, with a backup drive and a thumbnail cache
        sens = os.path.join(self.drive, "photos", "2026", "2026-10", "20261023_100000.jpg")
        safe = os.path.join(self.drive, "photos", "2026", "2026-10", "20261022_090000.jpg")
        os.makedirs(os.path.dirname(sens))
        shutil.copyfile(self.phone + "/DCIM/Camera/20261023_100000.jpg", sens)
        shutil.copyfile(released, safe)
        thumbs = os.path.join(self.home, ".cache", "thumbnails", "large")
        os.makedirs(thumbs)
        open(os.path.join(thumbs, "0123.png"), "wb").close()
        kept_thumb = os.path.join(self.phone, "DCIM/.thumbnails/1.jpg")
        os.makedirs(os.path.dirname(kept_thumb))
        open(kept_thumb, "wb").close()
        out = self.run_intake("erase", batch, "--phone", self.phone, "--drive", self.drive, "--yes",
                              "--keep-phone-thumbnails").stdout
        self.assertIn("erased 2 files; library copies 0; phone 3; drive 1", out)
        self.assertTrue(os.path.exists(kept_thumb), "--keep-phone-thumbnails keeps them")
        shutil.rmtree(os.path.dirname(kept_thumb))
        self.assertFalse(os.path.exists(quar))
        for rel in ("DCIM/Camera/20261023_100000.jpg", "DCIM/Restored/20261023_100000.jpg",
                    "Pictures/WhatsApp/IMG-20261021-WA0003.jpg"):
            self.assertFalse(os.path.exists(os.path.join(self.phone, rel)), rel)
        self.assertTrue(os.path.exists(os.path.join(self.phone, "DCIM/Camera/20261022_090000.jpg")),
                        "a released file stays on the phone")
        self.assertFalse(os.path.exists(sens))
        self.assertTrue(os.path.exists(safe))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cache", "thumbnails")))
        log = read(os.path.join(self.state, "erase-log.txt"))
        self.assertNotIn("20261023", log, "the erase log holds checksums only")
        for d in self.decoys:
            self.assertTrue(os.path.exists(d), d)
        # ---- offload: phone copies of filed files go; review originals stay
        out = self.run_intake("offload", batch, "--phone", self.phone, "--yes").stdout
        self.assertIn("deleted 8 files from the phone", out)
        left = sorted(os.path.relpath(os.path.join(r, f), self.phone)
                      for top in ("DCIM", "Pictures") for r, _, fs in os.walk(os.path.join(self.phone, top))
                      for f in fs)
        self.assertEqual(left, ["DCIM/Bitwarden/vault.png", "Pictures/Bitwarden/export.png",
                                "Pictures/WhatsApp/IMG-20261020-WA0001.jpg", "Pictures/WhatsApp/STK-20261020-WA0002.webp"])
        for d in self.decoys:
            self.assertTrue(os.path.exists(d), d)
        # ---- forget: two library files with the same name, one on the drive too
        f1, f2 = self.L("2025", "2025-01", "same.jpg"), self.L("2025", "2025-02", "same.jpg")
        save_jpg(pattern(10), f1)
        save_jpg(pattern(11), f2)
        dcopy = os.path.join(self.drive, "photos", "2025", "2025-01", "same.jpg")
        os.makedirs(os.path.dirname(dcopy))
        shutil.copyfile(f1, dcopy)
        out = self.run_intake("forget", f1, f2, "--drive", self.drive, "--yes").stdout
        self.assertIn("erased 2 files; library copies 0; phone 0; drive 1", out)
        self.assertFalse(os.path.exists(f1) or os.path.exists(f2) or os.path.exists(dcopy))
        self.assertEqual([d for d in os.listdir(os.path.join(self.state, "quarantine")) if d.startswith("forget-")], [])
        # ---- share-copy strips GPS (image) and metadata/data streams (video)
        gpsfile = self.L("2026", "2026-10", "20261020_120000.jpg")
        vid = os.path.join(shoot, "20261108_150000.mp4")
        out = self.run_intake("share-copy", gpsfile, vid, "--out", self.out).stdout
        self.assertNotIn("STILL PRESENT", out)
        with Image.open(os.path.join(self.out, "20261020_120000.jpg")) as im:
            self.assertFalse(im.getexif().get_ifd(0x8825))
        probe = subprocess.run(["ffprobe", "-v", "quiet", "-of", "json", "-show_format", "-show_streams",
                                os.path.join(self.out, "20261108_150000.mp4")], capture_output=True, text=True)
        info = json.loads(probe.stdout)
        self.assertNotIn("creation_time", info["format"].get("tags", {}))
        self.assertTrue(all(s["codec_type"] in ("video", "audio") for s in info["streams"]))

class EraseEverywhere(unittest.TestCase):
    """erase reaches every copy in HOME, near-duplicates, recent files, app state and phone thumbnails."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-erase-")
        t = self.tmp
        self.home = os.path.join(t, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.phone = os.path.join(t, "phone", "Internal storage")
        self.drive = os.path.join(t, "drive")
        os.makedirs(self.drive)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        MEDIA_INTAKE_WIPE_ANY_DEST="1", MEDIA_INTAKE_WIPE_MAX_MB="3",
                        MEDIA_INTAKE_NO_LOCALSEARCH="1")  # never talk to the real index
        P = lambda rel: os.path.join(self.phone, rel)  # noqa: E731
        self.sens = P("DCIM/Camera/20261023_100000.jpg")
        save_jpg(pattern(4), self.sens, "2026:10:23 10:00:00")
        self.keep = os.path.join(t, "outside-copy.jpg")
        shutil.copyfile(self.sens, self.keep)
        with open(self.sens, "rb") as f:
            self.sens_sha = __import__("hashlib").sha256(f.read()).hexdigest()
        save_jpg(pattern(8), P("DCIM/Camera/20261024_100000.jpg"), "2026:10:24 10:00:00")
        for rel in ("DCIM/.thumbnails/1.jpg", "DCIM/.thumbnails/sub/2.jpg", "Pictures/.thumbnails/3.jpg",
                    "Android/data/com.example/.thumbnails/4.jpg", "DCIM/.other/5.jpg"):
            os.makedirs(os.path.dirname(P(rel)), exist_ok=True)
            pattern(9, (64, 64)).save(P(rel))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def H(self, *p):
        return os.path.join(self.home, *p)

    def plant(self, dst, src=None):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src or self.sens, dst)
        return dst

    def quarantine(self, batch):
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        bd = os.path.join(self.state, "batches", batch)
        with open(os.path.join(bd, "scan.json"), "w") as f:
            json.dump({"DCIM/Camera/20261023_100000.jpg": ["nudity"]}, f)
        self.intake("apply", batch, "--allow-missing-detectors")
        return os.path.join(self.state, "quarantine", batch, "DCIM/Camera/20261023_100000.jpg")

    def test_erase_everywhere(self):
        batch = "2026-11-13-sweep"
        qpath = self.quarantine(batch)
        copies = [self.plant(self.H("Pictures/phone-import-2026-11-13/DCIM/Camera/20261023_100000.jpg")),
                  self.plant(self.H("Downloads/IMG-1.jpg")),
                  self.plant(self.H(".cache/some-app/x/frame.jpg")),
                  self.plant(self.H(".local/share/Trash/files/IMG-1.jpg"))]
        dcopy = self.plant(os.path.join(self.drive, "anywhere", "a.jpg"))
        skipped = self.plant(self.H(".cache/ms-playwright/x.jpg"))  # a skipped tree is not swept
        tinfo = self.H(".local/share/Trash/info/IMG-1.jpg.trashinfo")
        os.makedirs(os.path.dirname(tinfo))
        with open(tinfo, "w") as f:
            f.write("[Trash Info]\nPath=/x/IMG-1.jpg\nDeletionDate=2026-11-13T10:00:00\n")
        other_trash = self.plant(self.H(".local/share/Trash/files/other.jpg"),
                                 self.phone + "/DCIM/Camera/20261024_100000.jpg")
        # Near-duplicate (WhatsApp-style resize + recompress) and an unrelated image.
        near = self.H("Pictures/library/2026/IMG-20261023-WA0001.jpg")
        save_jpg(pattern(4).resize((600, 450)), near, quality=55)
        # Near-dups outside the media library (a cache, a git repo) are left alone.
        near_cache = self.H(".cache/x/tile.jpg")
        near_repo = self.H("code/site/static/hero.jpg")
        for p in (near_cache, near_repo):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            save_jpg(pattern(4).resize((600, 450)), p, quality=55)
        os.makedirs(self.H("code/site/.git"))
        repo_copy = self.plant(self.H("code/site/static/exact.jpg"))  # exact copy in a repo: reported only
        unrelated = self.H("Pictures/somewhere/unrelated.jpg")
        save_jpg(pattern(8), unrelated)
        # Recent files: one quarantine entry, one swept copy, one to keep.
        uri = lambda p: "file://" + p.replace(" ", "%20")  # noqa: E731
        bm = lambda u: (f'  <bookmark href="{u}" added="2026-11-13T10:00:00Z" modified="2026-11-13T10:00:00Z" '  # noqa: E731
                        f'visited="2026-11-13T10:00:00Z">\n    <info>\n      <metadata owner="http://freedesktop.org">\n'
                        f'        <mime:mime-type type="image/jpeg"/>\n      </metadata>\n    </info>\n  </bookmark>\n')
        xbel = self.H(".local/share/recently-used.xbel")
        keep_uri = uri(unrelated)
        with open(xbel, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<xbel version="1.0"\n'
                    '      xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"\n'
                    '      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info"\n>\n'
                    + bm(uri(qpath)) + bm(keep_uri) + bm(uri(copies[1])) + "</xbel>\n")
        # Showtime playback positions and a localsearch error report for a swept copy.
        import hashlib
        st = self.H(".local/state/showtime")
        os.makedirs(st)
        k_q, k_keep = (hashlib.sha256(uri(p).encode()).hexdigest() for p in (qpath, unrelated))
        with open(os.path.join(st, "playback_history.json"), "w") as f:
            json.dump({k_q: 1.5, k_keep: 2.5}, f)
        with open(os.path.join(st, "showtime.log"), "w") as f:
            f.write(f"DEBUG Playing video: {uri(qpath)}\nDEBUG Starting\n")
        errs = self.H(".cache/tracker3/files/errors")
        os.makedirs(errs)
        err = os.path.join(errs, hashlib.md5(uri(copies[1]).encode()).hexdigest())
        open(err, "w").close()

        out = self.intake("erase", batch, "--phone", self.phone, "--drive", self.drive, "--yes")
        for c in copies + [dcopy, tinfo, qpath, self.sens, err]:
            self.assertFalse(os.path.exists(c), c)
        self.assertIn("home sweep: 4 more copies deleted", out)
        for loc in ("Pictures", "Downloads", ".cache/some-app", "Trash"):
            self.assertRegex(out, rf"\n  {re.escape(loc)} +1\n")
        self.assertTrue(os.path.exists(other_trash))
        self.assertTrue(os.path.exists(skipped), "skipped trees are not swept")
        self.assertIn("drive 1", out)
        self.assertIn("drive free space wiped: 3 MB", out)
        self.assertFalse([f for f in os.listdir(self.drive) if f.startswith(".media-intake-wipe")])
        # near-duplicate moved to near-<batch>, not deleted; the unrelated image stays
        nq = os.path.join(self.state, "quarantine", f"near-{batch}")
        self.assertFalse(os.path.exists(near))
        self.assertTrue(os.path.isfile(os.path.join(nq, "Pictures/library/2026/IMG-20261023-WA0001.jpg")))
        nrec = jload(os.path.join(nq, "quarantine.json"))["Pictures/library/2026/IMG-20261023-WA0001.jpg"]
        self.assertEqual(nrec["near_of_sha"], self.sens_sha)  # which erased item it matched, by checksum only
        self.assertRegex(nrec["erased_on"], r"^\d{4}-\d\d-\d\d$")
        self.assertNotIn("20261023_100000", json.dumps(nrec))  # never the erased file's path
        self.assertTrue(os.path.exists(near_cache), "no near-dup search in caches")
        self.assertTrue(os.path.exists(near_repo), "no near-dup search in git repos")
        self.assertTrue(os.path.exists(repo_copy), "exact copies in git repos are not deleted")
        self.assertIn(f"1 exact copies inside git repos, not deleted: {repo_copy}", out)
        self.assertTrue(os.path.exists(unrelated))
        self.assertIn(f"1 near-duplicates", out)
        self.assertIn(f"erase near-{batch}", out)
        # recent files: only the unrelated bookmark is left, and the file still parses
        import xml.etree.ElementTree as ET
        hrefs = [b.get("href") for b in ET.parse(xbel).getroot().iter("bookmark")]
        self.assertEqual(hrefs, [keep_uri])
        self.assertEqual(jload(os.path.join(st, "playback_history.json")), {k_keep: 2.5})
        self.assertEqual(read(os.path.join(st, "showtime.log")), "DEBUG Starting\n")
        # phone: only the two thumbnail folders went
        self.assertFalse(os.path.exists(self.phone + "/DCIM/.thumbnails"))
        self.assertFalse(os.path.exists(self.phone + "/Pictures/.thumbnails"))
        for keep in ("Android/data/com.example/.thumbnails/4.jpg", "DCIM/.other/5.jpg", "DCIM/Camera/20261024_100000.jpg"):
            self.assertTrue(os.path.exists(os.path.join(self.phone, keep)), keep)
        for todo in ("Gallery", "Google Photos", "OneDrive", "WhatsApp", "fstrim -v", "Still yours"):
            self.assertNotIn(todo, out, "erase prints no manual steps for the owner")
        # erase log: checksums and sizes, no names
        log = read(os.path.join(self.state, "erase-log.txt"))
        self.assertRegex(log, r"sha256:[0-9a-f]{64} size:\d+\n")
        self.assertNotIn("20261023", log)
        # verify-gone: clean now, fails once a copy reappears
        self.assertIn("no copy of 1 erased items", self.intake("verify-gone", batch, "--drive", self.drive))
        again = self.plant(self.H("Desktop/again.jpg"), self.keep)  # a copy kept outside HOME and the phone
        out = self.intake("verify-gone", code=1)
        self.assertIn("STILL PRESENT: 1 copies: Desktop 1", out)
        self.assertTrue(os.path.exists(again), "verify-gone is read-only")

    def test_xcheck_erase_spares_its_original(self):
        """Erasing a messaging copy (quarantine/xcheck-*, no plan/manifest) never moves its `of` original
        to near-quarantine, though the two are near-duplicates; another near-copy still moves."""
        import hashlib
        orig = self.H("Pictures/library/2025/2025-06/IMG_1.jpg")
        save_jpg(pattern(6), orig, "2025:06:01 10:00:00")
        other = self.H("Pictures/library/2025/2025-06/IMG_1-edit.jpg")
        save_jpg(pattern(6).resize((900, 675)), other, quality=60)
        xb = "xcheck-2026-11-13-test"
        quar = os.path.join(self.state, "quarantine", xb)
        rel = "Pictures/library/Messaging/WhatsApp/2025/w1.jpg"
        wa = os.path.join(quar, rel)
        save_jpg(pattern(6).resize((800, 600)), wa, quality=50)
        with open(wa, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({rel: {"reasons": ["messaging copy of a camera photo"], "sha": sha,
                             "size": os.path.getsize(wa), "dst": self.H(rel), "of": orig}}, f)
        self.intake("erase", xb, "--yes", "--drive", self.drive, "--no-wipe-free")
        self.assertFalse(os.path.exists(wa))
        self.assertTrue(os.path.isfile(orig), "the full-quality original stays in the library")
        nq = jload(os.path.join(self.state, "quarantine", f"near-{xb}", "quarantine.json"))
        self.assertEqual(list(nq), ["Pictures/library/2025/2025-06/IMG_1-edit.jpg"])
        self.assertFalse(os.path.exists(other))

    # ---- device-delete guards: each refusal leaves every device file (and the quarantine) as it was
    def refused(self, *args):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 1, f"{args}\n{r.stdout}\n{r.stderr}")
        self.assertIn("nothing deleted from the device", r.stderr)
        return r.stderr

    def phone_files(self):
        return {os.path.join(r, f): os.path.realpath(os.path.join(r, f))
                for r, _, fs in os.walk(self.phone) for f in fs}

    def edit_manifest(self, batch, fn):
        mp = os.path.join(self.state, "batches", batch, "manifest.json")
        man = jload(mp)
        fn(man["files"])
        with open(mp, "w") as f:
            json.dump(man, f)

    def filed(self, batch):
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        self.intake("apply", batch, "--no-scan")

    def test_erase_refuses_a_phone_original_with_no_recorded_size(self):
        batch = "2026-11-13-nosize"
        qpath = self.quarantine(batch)
        self.edit_manifest(batch, lambda fs: fs["DCIM/Camera/20261023_100000.jpg"].pop("size"))
        before = self.phone_files()
        err = self.refused("erase", batch, "--phone", self.phone, "--yes")
        self.assertIn("records no size", err)
        self.assertIn("DCIM/Camera/20261023_100000.jpg", err)
        self.assertEqual(self.phone_files(), before, "no phone file (thumbnails included) was deleted")
        self.assertTrue(os.path.isfile(qpath), "nothing was erased")
        self.assertFalse(os.path.exists(os.path.join(self.state, "erase-log.txt")))

    def test_offload_refuses_a_phone_original_with_no_recorded_size(self):
        batch = "2026-11-13-nosize-off"
        self.filed(batch)
        self.edit_manifest(batch, lambda fs: fs["DCIM/Camera/20261024_100000.jpg"].pop("size"))
        before = self.phone_files()
        self.assertIn("records no size", self.refused("offload", batch, "--phone", self.phone, "--yes"))
        self.assertEqual(self.phone_files(), before)

    def test_offload_refuses_everything_for_one_target_off_the_allowlist(self):
        batch = "2026-11-13-allow"
        self.filed(batch)
        off = os.path.join(self.phone, "Download/20261024_100000.jpg")
        os.makedirs(os.path.dirname(off))
        shutil.copyfile(os.path.join(self.phone, "DCIM/Camera/20261024_100000.jpg"), off)

        def redirect(fs):
            fs["DCIM/Camera/20261024_100000.jpg"]["src"] = off
        self.edit_manifest(batch, redirect)
        before = self.phone_files()
        err = self.refused("offload", batch, "--phone", self.phone, "--yes")
        self.assertIn("refused: denied folder: Download/20261024_100000.jpg", err)
        self.assertEqual(self.phone_files(), before, "the allowed files are not offloaded either")

    def test_erase_refuses_a_symlink_in_phone_thumbnails(self):
        batch = "2026-11-13-link"
        qpath = self.quarantine(batch)
        target = os.path.join(self.phone, "DCIM/Camera/20261024_100000.jpg")
        link = os.path.join(self.phone, "DCIM/.thumbnails/link.jpg")
        os.symlink(target, link)
        before = self.phone_files()
        err = self.refused("erase", batch, "--phone", self.phone, "--yes")
        self.assertIn("refused: symlink: DCIM/.thumbnails/link.jpg", err)
        self.assertIn("--keep-phone-thumbnails", err)
        self.assertEqual(self.phone_files(), before, "neither the link, its target nor any thumbnail went")
        self.assertTrue(os.path.isfile(qpath), "nothing was erased")
        # Leaving the thumbnail folders alone, erase goes ahead; the link's target is untouched.
        self.intake("erase", batch, "--phone", self.phone, "--yes", "--keep-phone-thumbnails",
                    "--drive", self.drive, "--no-wipe-free")
        self.assertFalse(os.path.exists(self.sens))
        self.assertTrue(os.path.isfile(target) and os.path.islink(link))

    def test_wipe_free(self):
        out = self.intake("wipe-free", self.drive)
        self.assertIn("3 MB of zeros", out)
        self.assertEqual(os.listdir(self.drive), [])
        env = dict(self.env)
        env.pop("MEDIA_INTAKE_WIPE_ANY_DEST")
        mnt = subprocess.run(["findmnt", "-n", "-o", "TARGET", "-T", self.drive], capture_output=True, text=True).stdout.strip()
        if re.match(r"^(/|/home|/var|/tmp|/boot.*)$", mnt):
            r = subprocess.run([PY, "-I", INTAKE, "wipe-free", self.drive], env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("laptop's own disk", r.stderr)
            self.assertEqual(os.listdir(self.drive), [])


class Trash(unittest.TestCase):
    """trash: plain delete of non-sensitive junk, remembered by checksum; suggest-trash; digiKam Rejected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-trash-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.phone = os.path.join(self.tmp, "phone", "Internal storage")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")), TZ="America/New_York",
                        MEDIA_INTAKE_NO_LOCALSEARCH="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def sha(self, p):
        import hashlib
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def xmp(self, media, *args):
        x = media + ".xmp"
        with open(x, "w") as f:
            f.write("<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
                    "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n</rdf:RDF>\n</x:xmpmeta>\n"
                    "<?xpacket end='w'?>\n")
        if args:
            subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", *args, x], check=True)
        return x

    def tags(self, media):
        r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", media + ".xmp"], capture_output=True, text=True)
        v = json.loads(r.stdout)[0].get("HierarchicalSubject", [])
        return v if isinstance(v, list) else [v]

    def test_trash_library_file_and_plan_drops_it_later(self):
        batch = "2026-11-13-trash"
        P = lambda rel: os.path.join(self.phone, rel)  # noqa: E731
        save_jpg(pattern(11), P("DCIM/Camera/20261101_100000.jpg"), "2026:11:01 10:00:00")  # the pasta box
        save_jpg(pattern(12), P("DCIM/Camera/20261101_110000.jpg"), "2026:11:01 11:00:00")  # a keeper
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        self.intake("apply", batch, "--no-scan")
        junk = os.path.join(self.lib, "2026/2026-11/20261101_100000.jpg")
        keeper = os.path.join(self.lib, "2026/2026-11/20261101_110000.jpg")
        self.assertTrue(os.path.isfile(junk))
        side = self.xmp(junk)
        uri = "file://" + __import__("urllib.parse").parse.quote(junk, safe="/!$&'()*+,;=:@")
        thumb = os.path.join(self.home, ".cache/thumbnails/normal", __import__("hashlib").md5(uri.encode()).hexdigest() + ".png")
        os.makedirs(os.path.dirname(thumb))
        open(thumb, "w").close()
        sha, size = self.sha(junk), os.path.getsize(junk)
        out = self.intake("trash", batch, junk, "--phone", self.phone)
        self.assertIn("trashed 1 files; sidecars 1; thumbnails 1; phone 1", out)
        for p in (junk, side, thumb, P("DCIM/Camera/20261101_100000.jpg")):
            self.assertFalse(os.path.exists(p), p)
        self.assertTrue(os.path.isfile(keeper) and os.path.isfile(P("DCIM/Camera/20261101_110000.jpg")))
        self.assertNotIn(junk, jload(os.path.join(self.state, "index.json")))
        self.assertEqual(jload(os.path.join(self.state, "trashed.json")), {sha: size})  # checksum + size, no names
        self.assertFalse(os.path.exists(os.path.join(self.state, "erase-log.txt")), "trash is not erase")
        # the same picture from a backup comes back: plan drops it
        backup = os.path.join(self.tmp, "backup", "Internal storage")
        save_jpg(pattern(11), os.path.join(backup, "DCIM/Camera/20261101_100000.jpg"), "2026:11:01 10:00:00")
        self.intake("stage", backup, "--kind", "phone", "--batch", "2026-11-13-backup")
        self.intake("plan", "2026-11-13-backup")
        plan = jload(os.path.join(self.state, "batches/2026-11-13-backup/plan.json"))
        self.assertEqual([(p["action"], p["why"]) for p in plan], [("delete", "trashed before")])

    def test_trash_refuses_outside_and_updates_quarantine(self):
        dl = os.path.join(self.home, "Downloads/x.jpg")
        save_jpg(pattern(13), dl)
        link = os.path.join(self.lib, "link.jpg")
        os.makedirs(self.lib)
        os.symlink(dl, link)
        out = self.intake("trash", dl, link, code=1)
        self.assertIn("refused 2", out)
        self.assertTrue(os.path.isfile(dl))
        quar = os.path.join(self.state, "quarantine", "xcheck-b")
        q = os.path.join(quar, "Pictures/library/Messaging/w.jpg")
        save_jpg(pattern(14), q)
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"Pictures/library/Messaging/w.jpg": {"reasons": ["x"], "sha": "0" * 64, "size": 1, "dst": None},
                       "Pictures/library/Messaging/v.jpg": {"reasons": ["x"], "sha": "1" * 64, "size": 1, "dst": None}}, f)
        self.intake("trash", "xcheck-b", q)
        self.assertFalse(os.path.exists(q))
        self.assertEqual(list(jload(os.path.join(quar, "quarantine.json"))), ["Pictures/library/Messaging/v.jpg"])

    def test_suggest_and_not_trash(self):
        f = {}
        for n, extra in (("plain", []), ("used", ["Used|2026-11-14-demo-x"]), ("demo", ["Project|Demo"]),
                         ("kept", ["Owner|not-trash"]), ("legacy", ["Review|not-trash"])):
            f[n] = os.path.join(self.lib, f"2026/2026-11/{n}.jpg")
            save_jpg(pattern(15), f[n])
            self.xmp(f[n], *[f"-XMP-lr:HierarchicalSubject+={t}" for t in ["Review|maybe-trash", "Label|food"] + extra])
        out = self.intake("suggest-trash", "--no-serve")
        self.assertIn("1 library files suggested", out)
        mt = os.path.join(self.state, "maybe-trash.json")
        self.assertEqual(stat.S_IMODE(os.stat(mt).st_mode), 0o600)
        self.assertEqual(jload(mt)["items"], [{"path": f["plain"], "labels": ["Review|maybe-trash", "Label|food"]}])
        self.intake("not-trash", f["plain"])
        self.assertEqual(sorted(self.tags(f["plain"])), ["Label|food", "Owner|not-trash"])
        self.assertIn("0 library files suggested", self.intake("suggest-trash", "--no-serve"))
        self.assertTrue(all(os.path.isfile(p) for p in f.values()), "suggesting never deletes")

    def test_suggest_sources_and_owner_precedence(self):
        f = {}
        for n, args in (("vlm", ["-XMP-lr:HierarchicalSubject+=Review|vlm-trash/packaging"]),
                        ("vlm-ai", ["-XMP-lr:HierarchicalSubject+=AI|vlm/qwen/trash/blurry"]),
                        ("siglip-vlm-keep", ["-XMP-lr:HierarchicalSubject+=Review|maybe-trash",
                                             "-XMP-lr:HierarchicalSubject+=Review|vlm-keep"]),
                        ("accepted", ["-XMP-lr:HierarchicalSubject+=Review|maybe-trash", "-XMP-digiKam:PickLabel=3"]),
                        ("rejected-only", ["-XMP-digiKam:PickLabel=1"]),
                        ("owner-keep", ["-XMP-lr:HierarchicalSubject+=Review|vlm-trash/floor",
                                        "-XMP-lr:HierarchicalSubject+=Owner|keep"])):
            f[n] = os.path.join(self.lib, f"2026/2026-11/{n}.jpg")
            save_jpg(pattern(18), f[n])
            self.xmp(f[n], *args)
        self.intake("suggest-trash", "--no-serve")
        got = [x["path"] for x in jload(os.path.join(self.state, "maybe-trash.json"))["items"]]
        self.assertEqual(got, sorted([f["vlm"], f["vlm-ai"]]))  # digiKam Rejected: only trash --from-digikam

    def fake_venv(self, labels):
        """A stand-in venv interpreter in the temp state that answers label.py with canned labels."""
        py = os.path.join(self.state, "venv/bin/python")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        lj = os.path.join(self.tmp, "labels.json")
        with open(lj, "w") as fh:
            json.dump(labels, fh)
        with open(py, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\ncat '{lj}'\n")
        os.chmod(py, 0o755)

    def subjects(self, media):
        r = subprocess.run(["exiftool", "-j", "-q", "-XMP-dc:Subject", media + ".xmp"], capture_output=True, text=True)
        v = json.loads(r.stdout)[0].get("Subject", [])
        return sorted(v if isinstance(v, list) else [v])

    def test_retag_replaces_only_ai_namespace(self):
        f = os.path.join(self.lib, "2026/2026-11/p.jpg")
        save_jpg(pattern(19), f, "2026:11:05 10:00:00")
        self.xmp(f, *[f"-XMP-lr:HierarchicalSubject+={t}" for t in
                      ("pasta", "Label|food", "Review|child-in-shot", "Used|2026-11-14-demo-x", "Review|vlm-keep")],
                 "-XMP-dc:Subject+=pasta", "-XMP-dc:Subject+=food", "-XMP-digiKam:TagsList+=pasta")
        self.fake_venv({f: {"labels": {"kitchen": 0.3}, "review": ["maybe-trash"]}})
        self.intake("tag-files", f)
        t = sorted(self.tags(f))
        for want in ("pasta", "Review|child-in-shot", "Used|2026-11-14-demo-x", "Review|vlm-keep",
                     "Label|kitchen", "Review|maybe-trash", "Project|Personal"):
            self.assertIn(want, t)  # owner keyword, Used, VLM verdict survive; child flag is never cleared by AI
        self.assertNotIn("Label|food", t)  # stale label replaced
        self.assertIn("pasta", self.subjects(f))
        self.assertNotIn("food", self.subjects(f))
        # owner keeps it: maybe-trash never comes back on a re-tag
        self.intake("not-trash", f)
        self.intake("tag-files", f)
        t = self.tags(f)
        self.assertIn("Owner|not-trash", t)
        self.assertNotIn("Review|maybe-trash", t)
        self.assertIn("Review|child-in-shot", t)
        # --no-labels touches no AI tag
        self.intake("tag-files", f, "--no-labels")
        self.assertIn("Label|kitchen", self.tags(f))

    def test_from_digikam_rejected(self):
        rej = os.path.join(self.lib, "2026/2026-11/rej.jpg")
        ok = os.path.join(self.lib, "2026/2026-11/ok.jpg")
        for p, v in ((rej, "1"), (ok, "3")):
            save_jpg(pattern(16), p)
            self.xmp(p, f"-XMP-digiKam:PickLabel={v}")
        out = self.intake("trash", "--from-digikam")
        self.assertIn("1 library files marked Rejected", out)
        self.assertIn("dry run", out)
        self.assertTrue(os.path.isfile(rej))
        self.intake("trash", "--from-digikam", "--yes")
        self.assertFalse(os.path.exists(rej) or os.path.exists(rej + ".xmp"))
        self.assertTrue(os.path.isfile(ok))

    def test_quality_flags(self):
        import numpy as np
        sys.path.insert(0, HERE)
        import label
        rng = np.random.default_rng(1)
        sharp = np.asarray(pattern(17, (224, 224)), dtype=np.uint8)
        self.assertEqual(label.quality_flags(sharp, np), [])
        self.assertEqual(label.quality_flags(rng.integers(0, 255, (224, 224, 3), dtype=np.uint8), np), [])
        self.assertEqual(label.quality_flags(np.full((224, 224, 3), 128, np.uint8), np), ["maybe-trash"])  # no detail
        dark = (rng.integers(0, 12, (224, 224, 3))).astype(np.uint8)
        self.assertEqual(label.quality_flags(dark, np), ["maybe-trash"])  # near-black


@unittest.skipUnless(os.path.exists(os.path.join(REAL_VENV, "bin", "python")), "NudeNet venv not set up")
class RealScan(unittest.TestCase):
    """The real detectors on synthetic patterns: completes, valid JSON, no nudity."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-scan-")
        home = os.path.join(self.tmp, "home")
        self.state = os.path.join(home, ".local/share/media-intake")
        os.makedirs(self.state)
        os.symlink(REAL_VENV, os.path.join(self.state, "venv"))  # read-only use of the real venv
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(home, "lib"), PERSONAL_FOOTAGE=os.path.join(home, "vid"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(home, "foot")))
        self.phone = os.path.join(self.tmp, "phone")
        save_jpg(pattern(1), os.path.join(self.phone, "DCIM/Camera/20261108_071502.jpg"), "2026:11:08 07:15:02")
        os.makedirs(os.path.join(self.phone, "DCIM/Screenshots"))
        pattern(6, (540, 1170)).save(os.path.join(self.phone, "DCIM/Screenshots/Screenshot_20261101-101010.png"))
        pattern(7, (50, 50)).save(os.path.join(self.phone, "DCIM/Camera/s.webp"))
        video(os.path.join(self.phone, "DCIM/Camera/20261108_150000.mp4"), "2026-11-08T15:00:00Z", seconds=3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r

    def scan(self, batch):
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        out = self.intake("scan", batch).stdout
        summary = json.loads(out.strip().splitlines()[-1])
        flags = jload(os.path.join(self.state, "batches", batch, "scan.json"))
        return summary, flags

    def test_scan_completes_without_nudity(self):
        summary, flags = self.scan("scan-test")
        self.assertEqual(summary["scanned"], 4)
        self.assertNotIn("nudity", summary["by_reason"])
        self.assertFalse(any("nudity" in r or "unscanned-nudity" in r for r in flags.values()), flags)
        for bad in ("unscanned-video", "unreadable"):
            self.assertNotIn(bad, summary["by_reason"], flags)
        self.assertNotIn("ocr-unavailable", summary["by_reason"], "a missing tool must not quarantine every image")
        if not shutil.which("tesseract"):
            self.assertIn("ocr", summary["unavailable"])

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract not installed")
    def test_ocr_words(self):
        from PIL import ImageFont
        im = Image.new("RGB", (1400, 400), "white")
        ImageDraw.Draw(im).text((40, 120), "Your recovery code", fill="black", font=ImageFont.load_default(size=80))
        im.save(os.path.join(self.phone, "DCIM/Camera/codes.png"))
        _, flags = self.scan("ocr-test")
        self.assertIn("secret-words:credentials", flags.get("DCIM/Camera/codes.png", []))

    @unittest.skipUnless(have_zxing(), "python3-zxing-cpp not installed")
    def test_2fa_qr(self):
        # zxing-cpp 2.2 API (Fedora python3-zxing-cpp); a dummy, well-known demo secret.
        code = ("import sys, numpy, zxingcpp; from PIL import Image; "
                "img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, "
                "'otpauth://totp/test?secret=JBSWY3DPEHPK3PXP', 400, 400); "
                "Image.fromarray(numpy.array(img)).save(sys.argv[1])")
        subprocess.run([PY, "-c", code, os.path.join(self.phone, "DCIM/Camera/qr.png")], check=True)
        _, flags = self.scan("qr-test")
        self.assertIn("2fa-qr", flags.get("DCIM/Camera/qr.png", []))

    def text_image(self, name, lines, size=56):
        from PIL import ImageFont
        font = ImageFont.load_default(size=size)
        im = Image.new("RGB", (1800, 120 + 110 * len(lines)), "white")
        d = ImageDraw.Draw(im)
        for i, line in enumerate(lines):
            d.text((40, 60 + 110 * i), line, fill="black", font=font)
        im.save(os.path.join(self.phone, "DCIM/Camera", name))

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract not installed")
    def test_documents(self):
        # ICAO 9303 specimen MRZ (fictional "Utopia" passport), the standard Visa
        # test number, and negatives: a race bib and a receipt with long numbers.
        self.text_image("passport.png", ["P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
                                         "L898902C36UTO7408122F1204159ZE184226B<<<<<10"], size=44)
        self.text_image("card.png", ["VISA DEBIT", "4111 1111 1111 1111", "VALID THRU 12/29"])
        self.text_image("bib.png", ["POINT_A MARATHON", "1234"])
        self.text_image("receipt.png", ["ORDER 4111111111111111 SHIPPED", "TOTAL 42.00"])
        _, flags = self.scan("doc-test")
        self.assertIn("passport-mrz", flags.get("DCIM/Camera/passport.png", []), flags)
        self.assertIn("payment-card", flags.get("DCIM/Camera/card.png", []), flags)
        self.assertNotIn("DCIM/Camera/bib.png", flags, flags)
        self.assertNotIn("payment-card", flags.get("DCIM/Camera/receipt.png", []), flags)

    @unittest.skipUnless(have_zxing(), "python3-zxing-cpp not installed")
    def test_id_barcode(self):
        # Fake AAMVA header as on the back of a US licence (no real data).
        code = ("import sys, numpy, zxingcpp; from PIL import Image; "
                "img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.PDF417, "
                "'@\\n\\x1e\\rANSI 636000090002DL00410278ZV03190008DLDAQTEST000000', 900, 300); "
                "Image.fromarray(numpy.array(img)).save(sys.argv[1])")
        subprocess.run([PY, "-c", code, os.path.join(self.phone, "DCIM/Camera/licence.png")], check=True)
        _, flags = self.scan("pdf417-test")
        self.assertIn("id-barcode", flags.get("DCIM/Camera/licence.png", []), flags)


if __name__ == "__main__":
    unittest.main()
