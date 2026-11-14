#!/usr/bin/env python3
"""Capture dates (media_intake/dates.py): implausible camera clocks, GPS dates, QuickTime UTC,
EXIF offsets, iPhone DCIM/YYYYMM__ folders and --clock-offset. Synthetic files only.

  python3 -I -m unittest discover -s tests -p test_dates.py

The rules are tested on metadata dicts (no tools needed); the readers on JPEGs written with
Pillow (and exiftool when installed) and ffmpeg test patterns; plan and tag end to end through
intake.py in a throwaway HOME. Cases needing exiftool or ffmpeg skip when it is missing.
"""
import datetime, json, os, shutil, subprocess, sys, tempfile, unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
from test_intake import INTAKE, PY  # noqa: E402

import dates  # noqa: E402  (support put media_intake on sys.path)
from PIL import Image  # noqa: E402

NY = ZoneInfo("America/New_York")
NOW = datetime.datetime(2026, 11, 13, 12, 0, 0)
HAVE_EXIFTOOL = bool(shutil.which("exiftool"))
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
D = datetime.datetime


def res(meta, path="/x/IMG_0001.JPG", offsets=None):
    return dates.resolve(path, meta, offsets, now=NOW, local_tz=NY)


def mtime(d):
    return d.replace(tzinfo=NY).timestamp()


def jpg(path, dt=None, gps_day=None, gps_hms=None, offset=None, make=None, model=None, writer="pillow", serial=None):
    """A small JPEG with the given EXIF, written by Pillow or (writer="exiftool") by exiftool.

    The IFDs are assigned as whole dicts: on Pillow < 11, Exif.get_ifd() on a fresh Exif returns a
    dict that is never saved, so tags set through it were silently dropped. Every tag asked for is
    read back with exiftool when installed (else Pillow), and a missing one fails the test loudly."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    want = {k: v for k, v in (("Make", make), ("Model", model), ("DateTimeOriginal", dt),
                              ("OffsetTimeOriginal", offset), ("GPSDateStamp", gps_day), ("SerialNumber", serial)) if v}
    if gps_hms:
        want["GPSTimeStamp"] = "%02d:%02d:%02d" % tuple(int(x) for x in gps_hms)
    if writer == "exiftool":
        if not HAVE_EXIFTOOL:
            raise unittest.SkipTest("exiftool missing")
        Image.new("RGB", (400, 300), (40, 90, 160)).save(path)
        args = [f"-{'ExifIFD:' if k == 'SerialNumber' else ''}{k}={v}" for k, v in want.items()]
        if gps_day:
            args += ["-GPSLatitude=37.536", "-GPSLatitudeRef=N", "-GPSLongitude=77.435", "-GPSLongitudeRef=W"]
        if args:  # with no assignments exiftool would print every tag instead
            subprocess.run(["exiftool", "-q", "-overwrite_original", *args, path], check=True)
    else:
        ex = Image.Exif()
        if make:
            ex[271] = make
        if model:
            ex[272] = model
        e = {k: v for k, v in ((36867, dt), (36881, offset), (0xA431, serial)) if v}
        if e:
            ex[0x8769] = e
        if gps_day:
            g = {1: "N", 2: (37.0, 32.0, 10.0), 3: "W", 4: (77.0, 26.0, 5.0), 29: gps_day}
            if gps_hms:
                g[7] = tuple(float(x) for x in gps_hms)
            ex[0x8825] = g
        Image.new("RGB", (400, 300), (40, 90, 160)).save(path, exif=ex)
    got = _written(path)
    missing = {k: v for k, v in want.items() if str(got.get(k)) != v}
    assert not missing, f"{writer} did not write {missing} to {path} (read back: {got})"
    return path


def _written(path):
    """The tags jpg() writes, as read back (exiftool when installed, else Pillow)."""
    if HAVE_EXIFTOOL:
        r = subprocess.run(["exiftool", "-j", "-Make", "-Model", "-DateTimeOriginal", "-OffsetTimeOriginal",
                            "-GPSDateStamp", "-GPSTimeStamp", "-SerialNumber", path],
                           capture_output=True, text=True, check=True)
        return json.loads(r.stdout)[0]
    with Image.open(path) as im:
        ex = im.getexif()
    e, g = ex.get_ifd(0x8769), ex.get_ifd(0x8825)
    out = {"Make": ex.get(271), "Model": ex.get(272), "DateTimeOriginal": e.get(36867),
           "OffsetTimeOriginal": e.get(36881), "GPSDateStamp": g.get(29), "SerialNumber": e.get(0xA431)}
    if 7 in g:
        out["GPSTimeStamp"] = "%02d:%02d:%02d" % tuple(int(float(x)) for x in g[7])
    return out


def mov(path, created, apple=None, model=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    md = ["-metadata", f"creation_time={created}"]
    if apple:
        md += ["-metadata", f"com.apple.quicktime.creationdate={apple}", "-movflags", "use_metadata_tags"]
    if model:
        md += ["-metadata", "com.apple.quicktime.make=Apple", "-metadata", f"com.apple.quicktime.model={model}"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                    "-c:v", "mpeg4", *md, path], check=True)
    return path


class Plausibility(unittest.TestCase):
    def test_exif_wins_when_plausible(self):
        d = res({"DateTimeOriginal": "2026:11:08 07:15:02"})
        self.assertEqual((d.dt, d.src, d.suspect, d.note), (D(2026, 11, 8, 7, 15, 2), "exif", False, None))

    def test_floor_is_2000_01_02(self):
        self.assertTrue(dates.impossible(D(2000, 1, 1, 23, 59, 59), NOW))
        self.assertFalse(dates.impossible(D(2000, 1, 2), NOW))
        for epoch in ("1904:01:01 00:00:00", "1970:01:01 00:00:00", "1980:01:01 00:00:00", "2000:01:01 00:00:00"):
            d = res({"DateTimeOriginal": epoch, "mtime": mtime(D(2026, 10, 1, 9))}, "/x/IMG_0001.JPG")
            self.assertEqual((d.src, d.dt), ("mtime", D(2026, 10, 1, 9)), epoch)
            self.assertIn("impossible", d.note)

    def test_future_beyond_a_day_is_impossible(self):
        self.assertFalse(dates.impossible(NOW + datetime.timedelta(hours=23), NOW))
        self.assertTrue(dates.impossible(NOW + datetime.timedelta(days=1, seconds=1), NOW))
        d = res({"DateTimeOriginal": "2031:01:01 00:00:00"}, "/x/20261101_101010.jpg")
        self.assertEqual((d.src, d.dt), ("filename", D(2026, 11, 1, 10, 10, 10)))

    def test_gopro_unset_clock_takes_the_gps_date(self):
        meta = {"DateTimeOriginal": "2016:01:01 00:00:12", "GPSDateTime": "2026:11:08 15:04:05Z",
                "Make": "GoPro", "Model": "HERO11 Black"}
        d = res(meta, "/x/DCIM/100GOPRO/GOPR0001.JPG")
        self.assertEqual((d.src, d.dt, d.tz), ("gps", D(2026, 11, 8, 10, 4, 5), "-05:00"))  # UTC -> New York
        self.assertIn("disagrees with GPS", d.note)
        self.assertFalse(d.suspect)

    def test_any_wrong_day_is_caught_not_only_reset_defaults(self):
        d = res({"DateTimeOriginal": "2024:03:02 10:00:00", "GPSDateTime": "2026:11:08 15:00:00Z"})
        self.assertEqual(d.src, "gps")
        self.assertTrue(d.suspect, "GPS overruling a working-looking clock must be listed to check")
        self.assertIn("disagrees with GPS", d.note)

    def test_stale_phone_gps_does_not_beat_a_backed_camera_date(self):
        # A phone's GPS stamp is its last fix, here two days old; the name and the offset both back the camera.
        d = res({"DateTimeOriginal": "2026:11:08 07:15:02", "OffsetTimeOriginal": "+01:00",
                 "GPSDateTime": "2026:11:06 10:00:00Z"}, "/x/IMG_20261108_071502.jpg")
        self.assertEqual((d.src, d.dt, d.tz, d.suspect), ("exif", D(2026, 11, 8, 7, 15, 2), "+01:00", False))
        self.assertIn("stale", d.note)

    def test_file_name_alone_backs_the_camera(self):
        stale = "2026:11:06 10:00:00Z"
        for name in ("IMG_20261108_071502.jpg", "PXL_20261108_121502123.jpg", "Screenshot_20261108.jpg"):
            d = res({"DateTimeOriginal": "2026:11:08 07:15:02", "GPSDateTime": stale}, "/x/" + name)
            self.assertEqual((d.src, d.suspect), ("exif", False), name)  # local, UTC and date-only names agree
            self.assertIn("file name agrees", d.note)
        d = res({"DateTimeOriginal": "2026:11:08 07:15:02", "GPSDateTime": stale}, "/x/IMG_20261107_071502.jpg")
        self.assertEqual((d.src, d.suspect), ("gps", True), "a name a day off backs nothing")

    def test_recorded_offset_alone_backs_the_camera_but_is_flagged(self):
        d = res({"DateTimeOriginal": "2026:11:08 07:15:02", "OffsetTimeOriginal": "+01:00",
                 "GPSDateTime": "2026:11:06 10:00:00Z"})
        self.assertEqual((d.src, d.dt, d.suspect), ("exif", D(2026, 11, 8, 7, 15, 2), True))
        self.assertIn("UTC offset", d.note)

    def test_unbacked_camera_loses_to_gps_flagged(self):
        d = res({"DateTimeOriginal": "2026:11:08 07:15:02", "GPSDateTime": "2026:11:06 10:00:00Z"})
        self.assertEqual((d.src, d.dt, d.suspect), ("gps", D(2026, 11, 6, 5, 0), True))

    def test_impossible_camera_date_takes_gps_unflagged(self):
        d = res({"DateTimeOriginal": "1970:01:01 00:00:00", "GPSDateTime": "2026:11:08 15:00:00Z"},
                "/x/IMG_19700101_000000.jpg")
        self.assertEqual((d.src, d.suspect), ("gps", False))
        self.assertIn("impossible", d.note)

    def test_reset_default_ignores_name_and_offset_backing(self):
        d = res({"DateTimeOriginal": "2016:01:01 00:00:12", "OffsetTimeOriginal": "+00:00",
                 "GPSDateTime": "2026:11:08 15:04:05Z"}, "/x/20160101_000012.jpg")
        self.assertEqual((d.src, d.suspect), ("gps", False))

    def test_video_gps_against_apple_and_quicktime_clocks(self):
        stale = {"GPSDateTimes": ["2026:11:06 10:00:00"]}
        d = res(dict(stale, CreateDate="2026:11:08 15:00:00", CreationDate="2026:11:08 07:00:00-08:00"), "/x/v.mov")
        self.assertEqual((d.src, d.suspect), ("video", True))  # Apple's offset is recorded
        d = res(dict(stale, CreateDate="2026:11:08 15:00:00"), "/x/v.mp4")
        self.assertEqual((d.src, d.suspect), ("gps", True))  # QuickTime UTC is no recorded offset

    def test_gps_uses_exif_offset_for_local_time(self):
        d = res({"DateTimeOriginal": "2016:01:01 00:00:00", "OffsetTimeOriginal": "+09:00",
                 "GPSDateTime": "2026:11:08 15:00:00Z"})
        self.assertEqual((d.dt, d.tz), (D(2026, 11, 9, 0, 0), "+09:00"))

    def test_time_zones_are_not_a_disagreement(self):
        # No offset known: up to 24 h + 14 h apart still agrees (local wall time vs UTC).
        d = res({"DateTimeOriginal": "2026:11:09 13:00:00", "GPSDateTime": "2026:11:08 00:00:00Z"})
        self.assertEqual(d.src, "exif")
        d = res({"DateTimeOriginal": "2026:11:09 14:00:01", "GPSDateTime": "2026:11:08 00:00:00Z"})
        self.assertEqual(d.src, "gps")
        # Offset known: the instants are compared, 24 h apart at most.
        d = res({"DateTimeOriginal": "2026:11:08 07:00:00", "OffsetTimeOriginal": "-05:00",
                 "GPSDateTime": "2026:11:09 12:00:00Z"})
        self.assertEqual(d.src, "exif")
        self.assertEqual(d.tz, "-05:00")
        d = res({"DateTimeOriginal": "2026:11:08 07:00:00", "OffsetTimeOriginal": "-05:00",
                 "GPSDateTime": "2026:11:09 12:00:01Z"})
        self.assertEqual((d.src, d.suspect), ("exif", True))  # past 24 h; the recorded offset keeps it, flagged

    def test_reset_default_without_gps_is_kept_and_flagged(self):
        d = res({"DateTimeOriginal": "2015:12:31 19:00:03", "mtime": mtime(D(2026, 11, 8))},
                "/x/DCIM/100GOPRO/GOPR0002.JPG")
        self.assertEqual((d.src, d.dt, d.suspect), ("exif", D(2015, 12, 31, 19, 0, 3), True))
        self.assertIn("clock-reset", d.note)

    def test_new_years_eve_confirmed_by_gps_is_genuine(self):
        d = res({"DateTimeOriginal": "2015:12:31 23:30:00", "GPSDateTime": "2016:01:01 04:30:00Z"})
        self.assertEqual((d.src, d.suspect, d.note), ("exif", False, None))

    def test_gps_date_stamp_alone(self):
        d = res({"DateTimeOriginal": "2016:01:01 00:00:00", "GPSDateStamp": "2026:11:08"})
        self.assertEqual((d.src, d.dt, d.tz), ("gps", D(2026, 11, 8, 12, 0), None))
        # Date-only GPS allows 12 h more before calling the camera wrong.
        self.assertEqual(res({"DateTimeOriginal": "2026:11:10 01:00:00", "GPSDateStamp": "2026:11:08"}).src, "exif")

    def test_gps_without_camera_date_beats_filename_and_mtime(self):
        d = res({"GPSDateTime": "2026:11:08 15:00:00Z", "mtime": mtime(D(2026, 11, 12))}, "/x/20250101_000000.jpg")
        self.assertEqual((d.src, d.note), ("gps", None))

    def test_impossible_gps_is_ignored(self):
        d = res({"DateTimeOriginal": "2026:11:08 07:00:00", "GPSDateTime": "1980:01:06 00:00:00Z"})
        self.assertEqual(d.src, "exif")

    def test_gopro_gpmf_times_earliest_fix(self):
        meta = {"CreateDate": "2016:01:01 00:00:00",
                "GPSDateTimes": ["2026:11:08 15:00:09.5", "2026:11:08 15:00:01.1", "1970:01:01 00:00:00"]}
        d = res(meta, "/x/DCIM/100GOPRO/GX010001.MP4")
        self.assertEqual((d.src, d.dt, d.suspect), ("gps", D(2026, 11, 8, 10, 0, 1), False))

    def test_gpmf_stale_pre_lock_samples_are_skipped(self):
        # Two samples before lock carry an old fix; the locked run starts at 15:00:00.
        run = [f"2026:11:08 15:{m:02d}:{s:02d}" for m in range(3) for s in range(0, 60, 15)]
        meta = {"CreateDate": "2016:01:01 00:00:00", "GPSDateTimes": ["2026:11:05 09:00:00", "2026:11:05 09:00:01", *run]}
        d = res(meta, "/x/DCIM/100GOPRO/GX010002.MP4")
        self.assertEqual((d.src, d.dt), ("gps", D(2026, 11, 8, 10, 0)))
        # A gap of more than GPS_GAP ends the run; 2 minutes does not.
        self.assertEqual(dates._locked_start([datetime.datetime(2026, 11, 8, 15, m, tzinfo=dates.UTC)
                                              for m in (0, 2, 4, 30, 32, 34, 36)]),
                         datetime.datetime(2026, 11, 8, 15, 30, tzinfo=dates.UTC))

    def test_nothing_plausible_falls_back_to_mtime_flagged(self):
        d = res({"DateTimeOriginal": "1970:01:01 00:00:00", "mtime": mtime(D(1999, 1, 1))})
        self.assertEqual((d.src, d.suspect), ("mtime", True))
        self.assertIn("no plausible date", d.note)


class VideoAndFolders(unittest.TestCase):
    def test_quicktime_is_utc(self):
        d = res({"CreateDate": "2026:10:21 14:00:00"}, "/x/v.mp4")
        self.assertEqual((d.src, d.dt, d.tz), ("video", D(2026, 10, 21, 10, 0), "-04:00"))  # EDT that day

    def test_quicktime_honours_offset_time_original(self):
        d = res({"CreateDate": "2026:10:21 14:00:00", "OffsetTimeOriginal": "+02:00"}, "/x/v.mp4")
        self.assertEqual((d.dt, d.tz), (D(2026, 10, 21, 16, 0), "+02:00"))

    def test_apple_creation_date_is_local_with_its_offset(self):
        d = res({"CreateDate": "2026:11:08 15:00:00", "CreationDate": "2026:11:08 07:00:00-08:00"}, "/x/v.mov")
        self.assertEqual((d.src, d.dt, d.tz), ("video", D(2026, 11, 8, 7, 0), "-08:00"))

    def test_unset_quicktime_falls_through(self):
        d = res({"CreateDate": "0000:00:00 00:00:00", "mtime": mtime(D(2026, 9, 9, 9))}, "/x/v.mp4")
        self.assertEqual(d.src, "mtime")

    def test_iphone_folder_month_not_mtime(self):
        d = res({"mtime": mtime(D(2026, 11, 13, 8))}, "/raw/DCIM/202403__/IMG_0001.HEIC")
        self.assertEqual((d.src, d.dt), ("folder", D(2024, 3, 1)))
        self.assertEqual(res({"mtime": mtime(D(2026, 11, 13))}, "/raw/DCIM/202413__/IMG_0001.HEIC").src, "mtime")
        self.assertEqual(res({"mtime": mtime(D(2026, 11, 13))}, "/raw/Pictures/202403__/IMG_0001.HEIC").src, "mtime")

    def test_exif_and_filename_beat_the_folder(self):
        self.assertEqual(res({"DateTimeOriginal": "2024:03:09 10:00:00"}, "/r/DCIM/202403__/IMG_1.JPG").src, "exif")
        self.assertEqual(res({}, "/r/DCIM/202403__/20240309_100000.jpg").src, "filename")


class ClockOffset(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(dates.parse_clock_offset("HERO11 Black=+3h"), ("hero11 black", datetime.timedelta(hours=3)))
        self.assertEqual(dates.parse_clock_offset("GoPro=-1.5h"), ("gopro", datetime.timedelta(hours=-1.5)))
        self.assertEqual(dates.parse_clock_offset("all=+87600h")[0], "all")
        for bad in ("GoPro", "GoPro=3h", "GoPro=+3", "GoPro=+3d", "GoPro=+0h", "=+3h", "Go/Pro=+1h",
                    "GoPro=+999999h", "GoPro=+1.234h", "GoPro=++1h", "GoPro=+ 1h"):
            with self.assertRaises(ValueError, msg=bad):
                dates.parse_clock_offset(bad)

    def test_repeats_and_all_are_refused(self):
        with self.assertRaises(ValueError):
            dates.clock_offsets(["GoPro=+1h", "gopro=+2h"])
        with self.assertRaises(ValueError):
            dates.clock_offsets(["all=+1h", "GoPro=+2h"])

    def test_shift_matches_make_model_or_both(self):
        meta = {"DateTimeOriginal": "2026:11:08 04:15:00", "Make": "GoPro", "Model": "HERO11  Black"}
        for dev in ("GoPro", "hero11 black", "GoPro HERO11 Black", "all"):
            d = res(meta, offsets=dates.clock_offsets([f"{dev}=+3h"]))
            self.assertEqual((d.src, d.base, d.dt), ("offset", "exif", D(2026, 11, 8, 7, 15)), dev)
        d = res(meta, offsets=dates.clock_offsets(["iPhone 15=+3h"]))
        self.assertEqual((d.src, d.offset), ("exif", None))

    def test_shift_matches_the_device_id_too(self):
        # read_meta puts devices.identify's id in "Device"; DEVICE may name it, old spellings still work.
        meta = {"DateTimeOriginal": "2026:11:08 04:15:00", "Make": "GoPro", "Model": "HERO11 Black",
                "Device": "gopro-hero11-black-3fa9c2"}
        other = dict(meta, Device="gopro-hero11-black-77d01e")
        offs = dates.clock_offsets(["gopro-hero11-black-3fa9c2=+3h"])
        d = res(meta, offsets=offs)
        self.assertEqual((d.src, d.offset, d.dt, d.device), ("offset", "gopro-hero11-black-3fa9c2",
                                                              D(2026, 11, 8, 7, 15), "gopro-hero11-black-3fa9c2"))
        self.assertEqual(res(other, offsets=offs).src, "exif", "the other camera of that model keeps its clock")
        with self.assertRaises(ValueError):  # the model and the id both match: ambiguous, as before
            res(meta, offsets=dates.clock_offsets(["HERO11 Black=+1h", "gopro-hero11-black-3fa9c2=+2h"]))

    def test_two_matching_offsets_are_ambiguous(self):
        with self.assertRaises(ValueError):
            res({"Make": "GoPro", "Model": "HERO11 Black"}, offsets=dates.clock_offsets(["GoPro=+1h", "HERO11 Black=+2h"]))

    def test_shift_fixes_an_unset_clock_and_is_checked_after(self):
        meta = {"DateTimeOriginal": "2016:01:01 00:00:00", "Make": "GoPro"}
        d = res(meta, offsets=dates.clock_offsets(["GoPro=+95000h"]))
        self.assertEqual((d.src, d.suspect), ("offset", False))
        self.assertEqual(d.dt, D(2016, 1, 1) + datetime.timedelta(hours=95000))

    def test_gps_dates_are_never_shifted(self):
        meta = {"DateTimeOriginal": "2016:01:01 00:00:00", "GPSDateTime": "2026:11:08 15:00:00Z", "Make": "GoPro"}
        d = res(meta, offsets=dates.clock_offsets(["GoPro=+2h"]))
        self.assertEqual((d.src, d.dt, d.offset), ("gps", D(2026, 11, 8, 10, 0), "gopro"))

    def test_video_shift_moves_the_instant(self):
        d = res({"CreateDate": "2026:10:21 14:00:00", "Model": "X1"}, "/x/v.mp4", dates.clock_offsets(["X1=-2h"]))
        self.assertEqual((d.src, d.base, d.dt, d.tz), ("offset", "video", D(2026, 10, 21, 8, 0), "-04:00"))

    def test_unmatched_device_fails_loudly(self):
        with tempfile.TemporaryDirectory() as t:
            p = jpg(os.path.join(t, "a.jpg"), "2026:11:08 07:00:00", make="GoPro", model="HERO11 Black")
            with self.assertRaises(ValueError) as cm:
                dates.resolve_all([p], dates.clock_offsets(["HERO12 Black=+1h"]), now=NOW, local_tz=NY)
            self.assertIn("GoPro HERO11 Black", str(cm.exception))

    def test_exif_string(self):
        self.assertEqual(dates.exif_string(D(2026, 11, 8, 7, 15)), "2026:11:08 07:15:00")
        self.assertEqual(dates.exif_string(dates.aware(D(2026, 11, 8, 7, 15), "-05:00")), "2026:11:08 07:15:00-05:00")
        self.assertEqual(dates.exif_string(dates.aware(D(2026, 11, 8, 7, 15), "+05:30")), "2026:11:08 07:15:00+05:30")


class Readers(unittest.TestCase):
    """Real files: exiftool and the Pillow/ffprobe fallback must read the same metadata."""
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="dates-test-")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def readers(self):
        return [False] + ([True] if HAVE_EXIFTOOL else [])

    def writers(self):
        """Pillow and exiftool both write the test files, so neither reader is only ever fed one
        writer's layout (CI's older Pillow once left the exiftool-written layout untested here)."""
        return ["pillow"] + (["exiftool"] if HAVE_EXIFTOOL else [])

    def test_gopro_photo_with_gps(self):
        for w in self.writers():
            p = jpg(os.path.join(self.t, w, "DCIM/100GOPRO/GOPR0001.JPG"), "2016:01:01 00:00:12", "2026:11:08",
                    (15, 4, 5), make="GoPro", model="HERO11 Black", writer=w)
            for use in self.readers():
                d = dates.resolve_all([p], now=NOW, local_tz=NY, use_exiftool=use)[p]
                self.assertEqual((d.src, d.dt), ("gps", D(2026, 11, 8, 10, 4, 5)), f"writer={w} exiftool={use}")

    def test_offset_time_original_read(self):
        for w in self.writers():
            p = jpg(os.path.join(self.t, w, "a.jpg"), "2026:11:08 07:00:00", offset="+01:00", writer=w)
            for use in self.readers():
                d = dates.resolve_all([p], now=NOW, local_tz=NY, use_exiftool=use)[p]
                self.assertEqual((d.src, d.dt, d.tz), ("exif", D(2026, 11, 8, 7, 0), "+01:00"),
                                 f"writer={w} exiftool={use}")

    def test_iphone_folder_without_exif(self):
        for w in self.writers():
            p = jpg(os.path.join(self.t, w, "DCIM/202403__/IMG_0001.JPG"), writer=w)
            os.utime(p, (mtime(D(2026, 11, 13)),) * 2)
            for use in self.readers():
                d = dates.resolve_all([p], now=NOW, local_tz=NY, use_exiftool=use)[p]
                self.assertEqual((d.src, d.dt), ("folder", D(2024, 3, 1)), f"writer={w} exiftool={use}")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe missing")
    def test_videos(self):
        utc = mov(os.path.join(self.t, "v.mp4"), "2026-10-21T14:00:00Z")
        apple = mov(os.path.join(self.t, "IMG_0002.MOV"), "2026-11-08T15:00:00Z", "2026-11-08T07:00:00-0800", "iPhone 15")
        for use in self.readers():
            r = dates.resolve_all([utc, apple], now=NOW, local_tz=NY, use_exiftool=use)
            self.assertEqual((r[utc].src, r[utc].dt), ("video", D(2026, 10, 21, 10, 0)), f"exiftool={use}")
            self.assertEqual((r[apple].dt, r[apple].tz), (D(2026, 11, 8, 7, 0), "-08:00"), f"exiftool={use}")
            r = dates.resolve_all([apple], dates.clock_offsets(["Apple iPhone 15=+1h"]), now=NOW, local_tz=NY,
                                  use_exiftool=use)
            self.assertEqual((r[apple].src, r[apple].dt), ("offset", D(2026, 11, 8, 8, 0)), f"exiftool={use}")


class SameExtensions(unittest.TestCase):
    def test_video_ext_matches_intake(self):
        import intake
        self.assertEqual(dates.VIDEO_EXT, intake.VID)


class PlanAndTag(unittest.TestCase):
    """intake.py plan / apply / tag on a fake GoPro card: sources in plan.json and its output,
    --clock-offset validation, and the corrected date in the sidecar with the original untouched."""
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="dates-e2e-")
        home = os.path.join(self.t, "home")
        os.makedirs(home)
        self.state = os.path.join(home, ".local/share/media-intake")
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(home, "Videos/demo-footage")),
                        TZ="America/New_York", MEDIA_INTAKE_NO_LOCALSEARCH="1")
        self.card = os.path.join(self.t, "card")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_intake(self, *args, ok=True):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        if ok:
            self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r

    def plan(self, batch):
        with open(os.path.join(self.state, "batches", batch, "plan.json")) as f:
            return {p["rel"]: p for p in json.load(f)}

    def test_plan_records_and_shows_sources(self):
        g = "DCIM/100GOPRO/"
        jpg(os.path.join(self.card, g + "GOPR0001.JPG"), "2016:01:01 00:00:12", "2026:11:08", (15, 4, 5),
            make="GoPro", model="HERO11 Black")
        jpg(os.path.join(self.card, g + "GOPR0002.JPG"), "2016:01:01 00:01:00", make="GoPro", model="HERO11 Black")
        self.run_intake("stage", self.card, "--kind", "gopro", "--batch", "b1")
        out = self.run_intake("plan", "b1").stdout
        p = self.plan("b1")
        self.assertEqual(p[g + "GOPR0001.JPG"]["date_src"], "gps")
        self.assertEqual(p[g + "GOPR0001.JPG"]["date"], "2026-11-08T10:04:05")
        self.assertEqual(p[g + "GOPR0001.JPG"]["date_tz"], "-05:00")
        self.assertTrue(p[g + "GOPR0002.JPG"]["date_suspect"])
        self.assertIn("1 camera dates replaced by GPS: " + g + "GOPR0001.JPG", out)
        self.assertIn("1 dates to check", out)
        self.assertIn(g + "GOPR0002.JPG", out)
        # The clock was simply wrong: shift it. GPS stays as it is.
        out = self.run_intake("plan", "b1", "--clock-offset", "HERO11 Black=+95000h").stdout
        p = self.plan("b1")
        self.assertEqual((p[g + "GOPR0002.JPG"]["date_src"], p[g + "GOPR0002.JPG"]["date_from"]), ("offset", "exif"))
        self.assertFalse(p[g + "GOPR0002.JPG"]["date_suspect"])
        self.assertEqual(p[g + "GOPR0001.JPG"]["date_src"], "gps")
        self.assertIn("1 dates shifted by --clock-offset", out)

    def test_gps_overruling_a_camera_is_listed_to_check(self):
        jpg(os.path.join(self.card, "DCIM/100GOPRO/GOPR0004.JPG"), "2024:03:02 10:00:00", "2026:11:08", (15, 0, 0))
        jpg(os.path.join(self.card, "DCIM/100GOPRO/GOPR0005.JPG"), "2026:11:08 07:15:02", "2026:11:06", (10, 0, 0),
            offset="+01:00")
        self.run_intake("stage", self.card, "--kind", "gopro", "--batch", "b4")
        out = self.run_intake("plan", "b4").stdout
        p = self.plan("b4")
        self.assertEqual((p["DCIM/100GOPRO/GOPR0004.JPG"]["date_src"], p["DCIM/100GOPRO/GOPR0004.JPG"]["date_suspect"]),
                         ("gps", True))
        self.assertEqual(p["DCIM/100GOPRO/GOPR0005.JPG"]["date_src"], "exif")
        self.assertIn("1 camera dates replaced by GPS: DCIM/100GOPRO/GOPR0004.JPG", out)
        self.assertIn("2 dates to check", out)

    def test_bad_clock_offsets_are_refused(self):
        jpg(os.path.join(self.card, "DCIM/100GOPRO/GOPR0001.JPG"), "2026:11:08 04:15:00", make="GoPro")
        self.run_intake("stage", self.card, "--kind", "gopro", "--batch", "b2")
        for bad in ("GoPro=3h", "GoPro", "GoPro=+1d", "GoPro=+0h"):
            r = self.run_intake("plan", "b2", "--clock-offset", bad, ok=False)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("--clock-offset", r.stderr)
        r = self.run_intake("plan", "b2", "--clock-offset", "Sony=+1h", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("seen: GoPro", r.stderr)
        r = self.run_intake("plan", "b2", "--clock-offset", "GoPro=+1h", "--clock-offset", "gopro=+2h", ok=False)
        self.assertIn("given twice", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "batches", "b2", "plan.json")),
                         "a refused offset must not leave a plan behind")

    @unittest.skipUnless(HAVE_EXIFTOOL, "exiftool missing (tags are written with it)")
    def test_corrected_date_goes_to_the_sidecar_only(self):
        src = jpg(os.path.join(self.card, "DCIM/100GOPRO/GOPR0003.JPG"), "2026:11:08 04:15:00", offset="-05:00",
                  make="GoPro", model="HERO11 Black")
        with open(src, "rb") as f:
            before = f.read()
        self.run_intake("stage", self.card, "--kind", "gopro", "--batch", "b3")
        self.run_intake("plan", "b3", "--clock-offset", "GoPro=+3h")
        dst = self.plan("b3")["DCIM/100GOPRO/GOPR0003.JPG"]["dst"]
        self.assertIn(os.path.join("2026", "2026-11"), dst)
        self.run_intake("apply", "b3", "--no-scan")
        self.run_intake("tag", "b3", "--no-labels")

        def read():
            r = subprocess.run(["exiftool", "-j", "-q", "-XMP-exif:DateTimeOriginal", dst + ".xmp"],
                               capture_output=True, text=True, check=True)
            return json.loads(r.stdout)[0]["DateTimeOriginal"]
        self.assertEqual(read(), "2026:11:08 07:15:00-05:00")
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), before, "the original must never be written")
        r = subprocess.run(["exiftool", "-j", "-q", "-DateTimeOriginal", dst], capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)[0]["DateTimeOriginal"], "2026:11:08 04:15:00")
        # A later tag-files keeps the corrected sidecar date instead of re-reading the camera clock.
        self.run_intake("tag-files", dst, "--no-labels")
        self.assertEqual(read(), "2026:11:08 07:15:00-05:00")


if __name__ == "__main__":
    unittest.main()
