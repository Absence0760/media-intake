#!/usr/bin/env python3
"""check-dates and fix-dates (datecheck.py) on synthetic media in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_datecheck.py

RulesTest runs each check in-process on crafted records (no file is read). CheckDatesE2E builds a small library
(Pillow JPEGs and an ffmpeg MP4 tagged with exiftool: a GoPro sequence with a clock-reset shot, a phone sequence
with an outlier, a phone older than its date, a file name and a GPS time that disagree, GPS at 0, 0, a place far
from GPS, two copies with other dates, and a flagged file that must never be read), runs the real
`intake.py check-dates` and `fix-dates` (set, shift, neighbours, suggested, like, undo) and checks that only
sidecars change: every original's bytes stay as they were.
"""
import datetime, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
from test_intake import INTAKE, PY, pattern  # noqa: E402

import datecheck  # noqa: E402
import intake  # noqa: E402

HAVE = all(shutil.which(t) for t in ("exiftool", "ffmpeg"))
D = datetime.datetime


def rec(**kw):
    """A gather() record with nothing but what the test sets."""
    r = dict(path="/lib/x.jpg", name="x.jpg", ext=".jpg", video=False, make="", model="", device="",
             used=D(2024, 5, 12, 10, 0), used_tz=None, used_src="sidecar", camera=None, camera_src=None, gps=None,
             gps_slack=datetime.timedelta(0), name_date=None, name_day_only=False, received=False, export=None,
             dcim=None, mtime=D(2030, 1, 1), size=1, sha=None, plan_src=None, codec="", lat=None, lon=None,
             x_lat=None, x_lon=None, place=None, resolved_suspect=False, fixed=False)
    r.update(kw)
    if "name" in kw and "path" not in kw:
        r["path"] = "/lib/" + kw["name"]
    return r


class RulesTest(unittest.TestCase):
    def test_reset_defaults(self):
        f = datecheck.check_reset(rec(used=D(2016, 1, 1, 0, 7), make="GoPro"))
        self.assertEqual((f["kind"], f["confidence"]), ("reset", "high"))
        self.assertIn("GoPro", f["reason"])
        self.assertEqual(datecheck.check_reset(rec(used=D(2015, 12, 31, 19, 0), make="GoPro"))["confidence"], "high")
        self.assertEqual(datecheck.check_reset(rec(used=D(2016, 1, 1, 0, 7)))["confidence"], "medium")  # no make
        self.assertIsNone(datecheck.check_reset(rec(used=D(2016, 1, 1, 0, 7), make="Google")))  # New Year's photo
        for day in (D(1970, 1, 1, 3), D(1980, 1, 1), D(2000, 1, 1, 12), D(1904, 1, 1)):
            self.assertEqual(datecheck.check_reset(rec(used=day, make="Google"))["confidence"], "high", day)
        self.assertEqual(datecheck.check_reset(rec(used=D(2005, 1, 1, 0, 30)))["confidence"], "low")
        self.assertIsNone(datecheck.check_reset(rec(used=D(2005, 1, 1, 14, 30))))  # midnight hour only
        self.assertIsNone(datecheck.check_reset(rec(used=D(2016, 1, 2), make="GoPro")))
        # a GPS time that disagrees is the suggestion
        f = datecheck.check_reset(rec(used=D(2016, 1, 1), make="GoPro", gps=D(2024, 5, 12, 9, 30)))
        self.assertEqual((f["suggest"], f["suggest_src"]), ("2024-05-12T09:30:00", "gps"))

    def test_device_era(self):
        for make, model, when, family in (("GoPro", "HERO12 Black", (2023, 9), "GoPro HERO12"),
                                          ("GoPro", "HERO3+ Black", (2012, 10), "GoPro HERO3"),
                                          ("Apple", "iPhone 13 Pro Max", (2021, 9), "iPhone 13"),
                                          ("Apple", "iPhone XR", (2018, 10), "iPhone XR"),
                                          ("Apple", "iPhone SE (2nd generation)", (2020, 4), "iPhone SE (2nd generation)"),
                                          ("Google", "Pixel 8 Pro", (2023, 10), "Pixel 8"),
                                          ("Google", "Pixel 7a", (2023, 5), "Pixel 7a"),
                                          ("Google", "Pixel XL", (2016, 10), "Pixel"),
                                          ("samsung", "SM-S921B", (2024, 1), "Galaxy S24"),
                                          ("GoPro", "GoPro Max", (2019, 10), "GoPro MAX")):
            self.assertEqual(datecheck.first_sale(make, model), (when, family), model)
        for make, model in (("GoPro", "HERO"), ("Apple", "iPad Pro"), ("Canon", "EOS R5"), ("", ""),
                            ("SyntheticCam", "HERO12 Black")):
            self.assertIsNone(datecheck.first_sale(make, model), model)
        f = datecheck.check_era(rec(make="GoPro", model="HERO12 Black", used=D(2016, 1, 4)))
        self.assertEqual((f["kind"], f["confidence"]), ("era", "high"))
        self.assertIn("September 2023", f["reason"])
        self.assertIsNone(datecheck.check_era(rec(make="GoPro", model="HERO12 Black", used=D(2023, 8, 1))))  # slack
        self.assertIsNone(datecheck.check_era(rec(make="GoPro", model="HERO12 Black", used=D(2024, 5, 1))))

    def test_codec_floor(self):
        f = datecheck.check_codec(rec(video=True, ext=".mp4", codec="hvc1", used=D(2010, 6, 1)))
        self.assertEqual((f["kind"], f["confidence"]), ("codec", "high"))
        self.assertIsNone(datecheck.check_codec(rec(video=True, ext=".mp4", codec="hvc1", used=D(2016, 1, 1))))
        self.assertIsNotNone(datecheck.check_codec(rec(video=True, ext=".mp4", codec="av01", used=D(2017, 1, 1))))
        self.assertIsNone(datecheck.check_codec(rec(video=True, ext=".mp4", codec="avc1", used=D(2005, 1, 1))))
        self.assertIsNotNone(datecheck.check_codec(rec(ext=".heic", used=D(2012, 1, 1))))

    def test_sources(self):
        u = D(2021, 3, 1, 10, 15)
        f = datecheck.check_sources(rec(used=u, name_date=D(2024, 3, 1, 10, 15)))
        self.assertEqual((f["kind"], f["confidence"], f["suggest_src"]), ("sources", "high", "filename"))
        self.assertIn("file name", f["reason"])
        self.assertIsNone(datecheck.check_sources(rec(used=u, name_date=u + datetime.timedelta(hours=5))))  # UTC name
        self.assertIsNone(datecheck.check_sources(rec(used=u, name_date=D(2021, 3, 1), name_day_only=True)))
        # a messaging name says when it arrived: taken earlier is normal, taken after it arrived is not
        self.assertIsNone(datecheck.check_sources(rec(used=u, name_date=D(2024, 1, 1), received=True)))
        self.assertIsNotNone(datecheck.check_sources(rec(used=u, name_date=D(2020, 1, 1), received=True)))
        f = datecheck.check_sources(rec(used=u, gps=D(2024, 8, 8, 12)))
        self.assertEqual((f["confidence"], f["suggest_src"], f["suggest"]), ("high", "gps", "2024-08-08T12:00:00"))
        self.assertIsNone(datecheck.check_sources(rec(used=u, gps=u + datetime.timedelta(hours=30))))
        f = datecheck.check_sources(rec(used=u, camera=D(2016, 1, 1)))
        self.assertEqual((f["confidence"], f["suggest_src"]), ("medium", "camera"))
        for corrected in (dict(plan_src="gps"), dict(fixed=True), dict(used_src="gps")):
            self.assertIsNone(datecheck.check_sources(rec(used=u, camera=D(2016, 1, 1), **corrected)), corrected)
        self.assertEqual(datecheck.check_sources(rec(used=u, dcim=(2019, 5)))["confidence"], "medium")
        self.assertIsNone(datecheck.check_sources(rec(used=u, dcim=(2021, 2))))
        self.assertEqual(datecheck.check_sources(rec(used=u, mtime=D(2020, 1, 1)))["confidence"], "low")
        self.assertIsNone(datecheck.check_sources(rec(used=u, mtime=D(2020, 1, 1), fixed=True)))
        self.assertEqual(datecheck.check_sources(rec(used=u, export=D(2023, 1, 1)))["suggest_src"], "export")

    def test_gps_checks(self):
        f = datecheck.check_gps(rec(lat=0.0, lon=0.0))
        self.assertEqual([x["kind"] for x in f], ["null-island"])
        self.assertEqual([x["kind"] for x in datecheck.check_gps(rec(x_lat=0.0, x_lon=0.00001))], ["null-island"])
        self.assertEqual(datecheck.check_gps(rec(lat=0.0, lon=12.5)), [])
        f = datecheck.check_gps(rec(lat=37.5, lon=-77.4, x_lat=48.85, x_lon=2.35, place="Paris, FR"))
        self.assertEqual([x["kind"] for x in f], ["place"])
        self.assertIn("Paris", f[0]["reason"])
        self.assertEqual(datecheck.check_gps(rec(lat=37.5, lon=-77.4, x_lat=37.6, x_lon=-77.3)), [])

    def test_sequence_outlier_and_bracket(self):
        base = D(2023, 3, 1, 9)
        rs = [rec(name=f"IMG_{n:04d}.JPG", device="apple-iphone-13", used=base + datetime.timedelta(days=i))
              for i, n in enumerate(range(201, 207))]
        rs[2]["used"] = D(2022, 11, 20)  # IMG_0203, between 03-02 and 03-04
        out, hints, skipped = datecheck.check_sequences(rs, set())
        self.assertEqual([f["path"] for f in out], [rs[2]["path"]])
        self.assertEqual((out[0]["confidence"], out[0]["suggest"], out[0]["suggest_src"]),
                         ("high", "2023-03-03T09:00:00", "neighbours"))
        self.assertEqual(out[0]["bracket"], ["2023-03-02T09:00:00", "2023-03-04T09:00:00"])
        # a flagged file (a reset default) is left out of the order and gets its neighbours' date as a hint
        out, hints, _ = datecheck.check_sequences(rs, {rs[2]["path"]})
        self.assertEqual(out, [])
        self.assertEqual(hints[rs[2]["path"]][0], D(2023, 3, 3, 9))
        # a counter that repeats (wrapped, or two cameras with one id) is not compared
        rs[3]["name"] = rs[1]["name"]
        self.assertEqual(datecheck.check_sequences(rs, set())[2], 1)
        # no device id: never grouped
        self.assertEqual(datecheck.check_sequences([dict(r, device="") for r in rs], set()), ([], {}, 0))

    def test_sequence_gopro_chapters_and_a_late_run(self):
        t = D(2024, 5, 12, 10)
        rs = [rec(name=n, device="gopro-hero12-black", used=t + datetime.timedelta(minutes=10 * i))
              for i, n in enumerate(["GX010041.MP4", "GX010042.MP4", "GX020042.MP4", "GX010043.MP4"])]
        rs.append(rec(name="GX010044.MP4", device="gopro-hero12-black", used=D(2016, 1, 1, 0, 2)))
        out, hints, _ = datecheck.check_sequences(rs, set())
        self.assertEqual([f["path"] for f in out], [rs[4]["path"]])  # the last one: after GX010043 only
        self.assertEqual(out[0]["suggest"], "2024-05-12T10:30:00")
        self.assertEqual(out[0]["confidence"], "medium")
        self.assertEqual(datecheck.seq_key("GX020042.MP4"), ("gopro-gx", 42, 2))
        self.assertEqual(datecheck.seq_key("GOPR0042.JPG"), ("gopro", 42, 0))
        self.assertEqual(datecheck.seq_key("DSC01234.ARW"), ("DSC", 1234, 0))
        self.assertIsNone(datecheck.seq_key("PXL_20240512_101500123.jpg"))

    def test_lnds(self):
        self.assertEqual(datecheck.lnds([1, 2, 9, 3, 4]), [0, 1, 3, 4])
        self.assertEqual(datecheck.lnds([]), [])
        self.assertEqual(datecheck.lnds([5, 5, 5]), [0, 1, 2])

    def test_duplicates(self):
        a = rec(name="a.jpg", sha="ab" * 32, used=D(2024, 1, 1))
        b = rec(name="b.jpg", sha="ab" * 32, used=D(2016, 1, 1))
        c = rec(name="c.jpg", sha="cd" * 32, used=D(2016, 1, 1))
        out = datecheck.check_duplicates([a, b, c])
        self.assertEqual(sorted(f["path"] for f in out), [a["path"], b["path"]])

    def test_when_and_shift(self):
        self.assertEqual(datecheck.parse_when("2024-05-12"), (D(2024, 5, 12), False, None))
        self.assertEqual(datecheck.parse_when("2024-05-12T14:30"), (D(2024, 5, 12, 14, 30), True, None))
        self.assertEqual(datecheck.parse_when("2024-05-12 14:30:05+02:00"), (D(2024, 5, 12, 14, 30, 5), True, "+02:00"))
        for bad in ("2024-13-01", "12/05/2024", "2024-05-12T14:30+25:00", ""):
            with self.assertRaises(ValueError):
                datecheck.parse_when(bad)
        self.assertEqual(datecheck.shifted(D(2016, 1, 1, 0, 5), datecheck.parse_shift("+8y")), D(2024, 1, 1, 0, 5))
        self.assertEqual(datecheck.shifted(D(2024, 2, 29), datecheck.parse_shift("+1y")), D(2025, 2, 28))
        self.assertEqual(datecheck.shifted(D(2024, 1, 1, 12), datecheck.parse_shift("-3h30m")), D(2024, 1, 1, 8, 30))
        self.assertEqual(datecheck.shifted(D(2024, 1, 1), datecheck.parse_shift("+1y2d")), D(2025, 1, 3))
        with self.assertRaises(OverflowError):  # plan_fix refuses that file (test_refusals)
            datecheck.shifted(D(2020, 1, 1), datecheck.parse_shift("+999999d999999d999999d999999d"))
        for bad in ("8y", "+0d", "+3w", "+101y", "+", "--3h"):
            with self.assertRaises(ValueError):
                datecheck.parse_shift(bad)

    def test_best_suggestion_prefers_gps(self):
        fs = [dict(suggest="2024-01-01T00:00:00", suggest_src="neighbours"),
              dict(suggest="2024-02-02T00:00:00", suggest_src="gps"), dict(suggest=None)]
        self.assertEqual(datecheck.best_suggestion(fs), (D(2024, 2, 2), "gps"))
        self.assertEqual(datecheck.best_suggestion([]), (None, None))


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def exif(path, *args):
    subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", *args, path], check=True)


def jpg(path, seed, when=None, *args):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pattern(seed, (160, 120)).save(path, quality=85)
    tags = [*([f"-DateTimeOriginal={when}"] if when else []), *args]
    if tags:
        exif(path, *tags)


def side_date(path):
    x = path + ".xmp"
    if not os.path.exists(x):
        return None
    r = subprocess.run(["exiftool", "-j", "-q", "-XMP-exif:DateTimeOriginal", x], capture_output=True, text=True,
                       check=True)
    return json.loads(r.stdout)[0].get("DateTimeOriginal")


def set_side(path, *args):
    x = path + ".xmp"
    if not os.path.exists(x):
        with open(x, "w", encoding="utf-8") as f:
            f.write(intake.EMPTY_XMP)
    exif(x, *args)


@unittest.skipUnless(HAVE, "exiftool and ffmpeg are needed")

class ForgetRecordsTest(unittest.TestCase):
    """Erase, forget, trash and sweep-erased drop a file's records from datecheck.json and date-fixes.json
    (datecheck.forget_paths); verify-gone counts what is left of files gone from a library folder (stale)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="datecheck-forget-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.state, mode=0o700)
        os.makedirs(self.lib)
        self.a, self.b, self.c = (os.path.join(self.lib, n) for n in ("IMG_0101.JPG", "IMG_0102.JPG", "c.jpg"))
        for p in (self.a, self.b, self.c):
            open(p, "wb").close()
        f = lambda p, kind, reason="x": dict(path=p, kind=kind, confidence="high", reason=reason,  # noqa: E731
                                             date="2016-01-01T00:00:00")
        datecheck.save_private(datecheck.report_path(self.state), dict(
            version=1, id="r1", checked="2026-11-19T10:00:00", files=3, findings=[
                f(self.a, "reset"), f(self.b, "sequence", "numbered after IMG_0101.JPG (2024-05-12 10:00)"),
                f(self.c, "reset"), f(self.c, "duplicate", "the same content is in the library as IMG_0101.JPG")]))
        fx = lambda i, *ps: dict(id=i, at="2026-11-19T10:01:00", reason="", report="r1", undone=None,  # noqa: E731
                                 dropped=[f(p, "reset") for p in ps],
                                 files=[dict(path=p, old="2016:01:01 00:00:00", new="2024-05-12T10:00:00", tz=None,
                                             sha=None) for p in ps])
        datecheck.save_private(datecheck.fixes_path(self.state), dict(version=1, fixes=[fx("f1", self.a),
                                                                                        fx("f2", self.a, self.c)]))

    def dump(self):
        out = ""
        for p in (datecheck.report_path(self.state), datecheck.fixes_path(self.state)):
            with open(p) as fh:
                out += fh.read()
        return out

    def test_forget_drops_paths_names_and_empty_fixes(self):
        n, err = datecheck.forget_paths({self.a}, self.state)
        self.assertEqual(err, "")
        self.assertEqual(n, 3 + 4, "3 findings (its own, a neighbour's and a copy's that name it); in the fixes "
                                   "its 2 file entries and the 2 findings set aside for it")
        self.assertNotIn("IMG_0101", self.dump(), "no path, no name, no old or new date of it is left")
        rep = datecheck.load_report(self.state)
        self.assertEqual([(x["path"], x["kind"]) for x in rep["findings"]], [(self.c, "reset")])
        fixes = datecheck.load_fixes(self.state)["fixes"]
        self.assertEqual([(x["id"], [y["path"] for y in x["files"]]) for x in fixes], [("f2", [self.c])],
                         "a fix left with no file goes")
        for p in (datecheck.report_path(self.state), datecheck.fixes_path(self.state)):
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual([x for x in os.listdir(self.state) if x.endswith(".tmp")], [], "written atomically")
        self.assertEqual(datecheck.forget_paths({self.a}, self.state), (0, ""), "a second run changes nothing")

    def test_stale_counts_gone_files_only_under_a_folder_that_is_there(self):
        self.assertEqual(datecheck.stale([self.lib], self.state), set())
        os.remove(self.a)
        self.assertEqual(datecheck.stale([self.lib], self.state), {self.a})
        away = self.lib + "-unplugged"
        os.rename(self.lib, away)  # its drive is not mounted: nothing is gone
        self.assertEqual(datecheck.stale([self.lib], self.state), set())
        os.makedirs(self.lib)  # ...nor with its empty mountpoint folder left behind
        self.assertEqual(datecheck.stale([self.lib], self.state), set())
        self.assertEqual(datecheck.forget_paths(datecheck.stale([self.lib], self.state), self.state), (0, ""))
        os.rmdir(self.lib)
        os.rename(away, self.lib)
        datecheck.forget_paths(datecheck.stale([self.lib], self.state), self.state)
        self.assertEqual(datecheck.stale([self.lib], self.state), set())

    def test_a_name_is_matched_whole_and_only_nearby(self):
        """Erasing 1.jpg drops a neighbour's finding that names it (same or sibling month folder), never one
        naming 11.jpg or 1.jpg.bak, nor a same-named file's in another year."""
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        gone = L("2024", "2024-05", "1.jpg")
        seq = lambda p, why: dict(path=p, kind="sequence", confidence="high", reason=why, date="2024")  # noqa: E731
        keep = [seq(L("2024", "2024-05", "12.jpg"), "numbered after 11.jpg (2024-05-12 10:00)"),
                seq(L("2024", "2024-05", "13.jpg"), "numbered after 1.jpg.bak (2024-05-12 10:00)"),
                seq(L("2019", "2019-07", "x.jpg"), "numbered after 1.jpg (2019-07-01 12:00)")]
        drop = [seq(L("2024", "2024-06", "2.jpg"), "numbered after 1.jpg (2024-05-31 23:00)"),
                seq(L("2024", "2024-05", "3.jpg"), "numbered before 1.jpg.")]
        datecheck.save_private(datecheck.report_path(self.state), dict(version=1, id="r2", checked="x", files=5,
                                                                       findings=keep + drop))
        self.assertEqual(datecheck.forget_paths({gone}, self.state), (2, ""))
        self.assertEqual(datecheck.load_report(self.state)["findings"], keep)

    def test_an_unreadable_report_is_said_not_guessed(self):
        with open(datecheck.report_path(self.state), "w") as fh:
            fh.write("{not json")
        n, err = datecheck.forget_paths({self.a}, self.state)
        self.assertIn("datecheck.json is unreadable", err)
        self.assertEqual(n, 4, "the fixes file is still cleaned")

class CheckDatesE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="datecheck-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state, mode=0o700)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, TZ="UTC",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        hero = ["-Make=GoPro", "-Model=HERO12 Black"]
        # a GoPro's numbered shots, the fourth on the clock-reset default
        self.gopro = [L("2024/2024-05/gopro-hero12-black", f"GOPR{n:04d}.JPG") for n in range(101, 107)]
        for i, (p, w) in enumerate(zip(self.gopro, ("2024:05:12 10:00:00", "2024:05:12 10:10:00", "2024:05:12 10:20:00",
                                                   "2016:01:01 00:03:00", "2024:05:12 10:40:00", "2024:05:12 10:50:00"))):
            jpg(p, i + 1, w, *hero)
        self.reset = self.gopro[3]
        self.reset2 = L("2016/2016-01/gopro-hero12-black/GOPR0301.JPG")  # another reset shot, for --like
        jpg(self.reset2, 20, "2016:01:01 01:00:00", *hero)
        # a phone's numbered shots, one dated months before its neighbours
        iph = ["-Make=Apple", "-Model=iPhone 13"]
        self.phone = [L("2023/2023-03", f"IMG_{n:04d}.JPG") for n in range(201, 206)]
        for i, p in enumerate(self.phone):
            jpg(p, 30 + i, f"2023:03:0{i + 1} 09:00:00" if i != 2 else "2022:11:20 09:00:00", *iph)
        self.outlier = self.phone[2]
        self.era = L("2019/2019-06/DSC_0001.JPG")
        jpg(self.era, 40, "2019:06:01 12:00:00", "-Make=Google", "-Model=Pixel 8")
        self.named = L("2021/2021-03/20240301_101500.jpg")
        jpg(self.named, 41, "2021:03:01 10:15:00")
        self.gps = L("2022/2022-02/trip.jpg")
        jpg(self.gps, 42, "2022:02:02 12:00:00", "-GPSDateStamp=2024:08:08", "-GPSTimeStamp=12:00:00")
        set_side(self.gps, "-XMP-exif:DateTimeOriginal=2022:02:02 12:00:00")
        self.null = L("2024/2024-06/null.jpg")
        jpg(self.null, 43, "2024:06:01 12:00:00", "-GPSLatitude=0", "-GPSLatitudeRef=N", "-GPSLongitude=0",
            "-GPSLongitudeRef=E")
        self.place = L("2024/2024-06/place.jpg")
        jpg(self.place, 44, "2024:06:02 12:00:00", "-GPSLatitude=37.5", "-GPSLatitudeRef=N", "-GPSLongitude=77.4",
            "-GPSLongitudeRef=W")
        set_side(self.place, "-XMP-exif:GPSLatitude=48.85", "-XMP-exif:GPSLongitude=2.35",
                 "-XMP-lr:HierarchicalSubject=Place|Paris, FR")
        self.dup1, self.dup2 = L("2024/2024-07/copy.jpg"), L("2016/2016-07/copy.jpg")
        jpg(self.dup1, 45)
        os.makedirs(os.path.dirname(self.dup2))
        shutil.copyfile(self.dup1, self.dup2)
        set_side(self.dup1, "-XMP-exif:DateTimeOriginal=2024:07:04 12:00:00")
        set_side(self.dup2, "-XMP-exif:DateTimeOriginal=2016:07:04 12:00:00")
        self.video = os.path.join(self.vids, "2000/2000-01/clip.mp4")
        os.makedirs(os.path.dirname(self.video))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                        "-c:v", "mpeg4", "-metadata", "creation_time=2023-07-01T12:00:00Z", self.video], check=True)
        set_side(self.video, "-XMP-exif:DateTimeOriginal=2000:01:01 00:00:00")
        # flagged by scan in an applied batch: never read, never fixed
        self.flagged = L("2016/2016-01/gopro-hero12-black/GOPR0999.JPG")
        jpg(self.flagged, 50, "2016:01:01 00:09:00", *hero)
        b = os.path.join(self.state, "batches", "b1")
        os.makedirs(b)
        with open(os.path.join(b, "plan.json"), "w") as f:
            json.dump([dict(rel="DCIM/GOPR0999.JPG", sha=sha(self.flagged), size=os.path.getsize(self.flagged),
                            dst=self.flagged, action="file")], f)
        with open(os.path.join(b, "scan.json"), "w") as f:
            json.dump({"DCIM/GOPR0999.JPG": ["nudity"]}, f)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({p: dict(sha=sha(p), size=os.path.getsize(p), dhash=0) for p in (self.dup1, self.dup2)}, f)
        self.originals = {p: sha(p) for p in self.all_media()}

    def all_media(self):
        return [os.path.join(d, n) for root in (self.lib, self.vids) for d, _, ns in os.walk(root) for n in ns
                if not n.endswith(".xmp")]

    def run_cli(self, *args, ok=True):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def check(self, *args):
        return json.loads(self.run_cli("check-dates", "--json", *args).stdout)

    def by(self, rep):
        out = {}
        for f in rep["findings"]:
            out.setdefault(f["path"], {})[f["kind"]] = f
        return out

    def assert_originals_unchanged(self):
        for p, h in self.originals.items():
            self.assertEqual(sha(p), h, f"{p} was written")

    def test_every_signal_and_the_flagged_file_left_out(self):
        rep = self.check()
        f = self.by(rep)
        self.assertEqual(rep["refused"], 1)
        self.assertNotIn(self.flagged, f, "a flagged file is never read")
        self.assertEqual(f[self.reset]["reset"]["confidence"], "high")
        self.assertIn("era", f[self.reset])
        self.assertEqual(f[self.reset]["reset"]["suggest"], "2024-05-12T10:30:00")  # its numbered neighbours
        self.assertEqual(f[self.reset]["reset"]["suggest_src"], "neighbours")
        self.assertEqual(set(f[self.reset2]), {"reset", "era"})
        self.assertEqual(f[self.outlier]["sequence"]["suggest"], "2023-03-03T09:00:00")
        self.assertEqual(f[self.outlier]["sequence"]["confidence"], "high")
        self.assertEqual(f[self.era]["era"]["confidence"], "high")
        self.assertEqual(f[self.named]["sources"]["suggest_src"], "filename")
        self.assertEqual(f[self.gps]["sources"]["suggest"], "2024-08-08T12:00:00")
        self.assertIn("null-island", f[self.null])
        self.assertIn("place", f[self.place])
        self.assertIn("duplicate", f[self.dup1])
        self.assertIn("duplicate", f[self.dup2])
        self.assertEqual(f[self.video]["reset"]["confidence"], "high")  # 2000-01-01 in its sidecar
        self.assertIn("sources", f[self.video])  # the video's own date (2023) disagrees
        for p in self.gopro[:3] + self.gopro[4:] + [self.phone[0], self.phone[1]]:
            self.assertNotIn(p, f, p)
        saved = datecheck_report(self.state)
        self.assertEqual(saved["findings"], rep["findings"])
        self.assertEqual(os.stat(os.path.join(self.state, "datecheck.json")).st_mode & 0o777, 0o600)
        self.assert_originals_unchanged()
        text = self.run_cli("check-dates").stdout
        self.assertIn("Clock-reset default date", text)
        self.assertIn("[high]", text)
        self.assertNotIn("GOPR0999", text)

    def test_paths_replace_only_their_findings(self):
        self.check()
        rep = self.check(self.era)
        self.assertEqual(rep["files"], 1)
        saved = self.by(datecheck_report(self.state))
        self.assertIn(self.reset, saved, "a run over some paths keeps the others' findings")
        self.assertIn("era", saved[self.era])

    def test_fix_suggested_writes_the_sidecar_only_and_undo_puts_it_back(self):
        self.check()
        r = self.run_cli("fix-dates", "--suggested", self.reset)
        self.assertIn("dry run", r.stdout)
        self.assertIn("2016-01-01 00:03 -> 2024-05-12 10:30", r.stdout)
        self.assertIsNone(side_date(self.reset), "a dry run writes nothing")
        self.run_cli("fix-dates", "--suggested", self.reset, "--yes", "--reason", "battery swap")
        self.assertEqual(side_date(self.reset), "2024:05:12 10:30:00")
        self.assert_originals_unchanged()
        saved = self.by(datecheck_report(self.state))
        self.assertNotIn(self.reset, saved, "its date findings leave the report")
        with open(os.path.join(self.state, "date-fixes.json")) as fh:
            log = json.load(fh)
        self.assertEqual(log["fixes"][-1]["reason"], "battery swap")
        self.assertIsNone(log["fixes"][-1]["files"][0]["old"])
        self.assertEqual(os.stat(os.path.join(self.state, "date-fixes.json")).st_mode & 0o777, 0o600)
        # a fresh check sees the fixed date, and no longer compares it with the camera clock it replaced
        f = self.by(self.check())
        self.assertNotIn(self.reset, f)
        r = self.run_cli("fix-dates", "--undo")
        self.assertIn("dry run", r.stdout)
        self.assertEqual(side_date(self.reset), "2024:05:12 10:30:00")
        self.run_cli("fix-dates", "--undo", "--yes")
        self.assertIsNone(side_date(self.reset))
        self.assert_originals_unchanged()
        r = self.run_cli("fix-dates", "--undo", "--yes", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing to undo", r.stderr)

    def test_undo_brings_back_the_findings_of_the_same_report(self):
        self.check()
        self.run_cli("fix-dates", "--from-neighbours", self.outlier, "--yes")
        self.assertEqual(side_date(self.outlier), "2023:03:03 09:00:00")
        self.assertNotIn(self.outlier, self.by(datecheck_report(self.state)))
        self.run_cli("fix-dates", "--undo", "--yes")
        self.assertIn("sequence", self.by(datecheck_report(self.state))[self.outlier])

    def test_set_shift_and_like(self):
        self.check()
        self.run_cli("fix-dates", "--set", "2024-08-08T12:00", self.gps, "--yes")
        self.assertEqual(side_date(self.gps), "2024:08:08 12:00:00")
        self.run_cli("fix-dates", "--set", "2024-08-09", self.gps, "--yes")  # a date alone keeps the time
        self.assertEqual(side_date(self.gps), "2024:08:09 12:00:00")
        self.run_cli("fix-dates", "--shift", "+3y", self.named, "--yes")
        self.assertEqual(side_date(self.named), "2024:03:01 10:15:00")
        self.run_cli("fix-dates", "--set", "2024-05-12T10:30+02:00", self.reset, "--yes")
        self.assertEqual(side_date(self.reset), "2024:05:12 10:30:00+02:00")
        # --like: the good file's offset from its camera clock, applied to another file's camera date
        self.run_cli("fix-dates", "--set", "2024-05-12T10:30", self.reset, "--yes")
        r = self.run_cli("fix-dates", "--like", self.reset, self.reset2, "--yes")
        off = D(2024, 5, 12, 10, 30) - D(2016, 1, 1, 0, 3)
        self.assertEqual(side_date(self.reset2), (D(2016, 1, 1, 1, 0) + off).strftime("%Y:%m:%d %H:%M:%S"), r.stdout)
        self.assert_originals_unchanged()
        # undo goes back one fix at a time, newest first
        self.run_cli("fix-dates", "--undo", "--yes")
        self.assertIsNone(side_date(self.reset2))
        self.assertEqual(side_date(self.reset), "2024:05:12 10:30:00")
        self.run_cli("fix-dates", "--undo", "--yes")
        self.assertEqual(side_date(self.reset), "2024:05:12 10:30:00+02:00")

    def test_refusals(self):
        self.check()
        r = self.run_cli("fix-dates", "--set", "2024-01-01", self.flagged, "--yes", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("flagged", r.stdout)
        self.assertFalse(os.path.exists(self.flagged + ".xmp"))
        outside = os.path.join(self.home, "Desktop/x.jpg")
        jpg(outside, 60, "2016:01:01 00:00:00")
        r = self.run_cli("fix-dates", "--set", "2024-01-01", outside, "--yes", ok=False)
        self.assertIn("not in the library", r.stdout)
        self.assertFalse(os.path.exists(outside + ".xmp"))
        r = self.run_cli("fix-dates", "--set", "1999-01-01", self.era, "--yes", ok=False)
        self.assertIn("impossible", r.stdout)
        r = self.run_cli("fix-dates", "--from-neighbours", self.era, "--yes", ok=False)
        self.assertIn("no suggestion from its numbered neighbours", r.stdout)
        r = self.run_cli("fix-dates", "--shift", "8y", self.era, ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--shift wants", r.stderr)
        r = self.run_cli("fix-dates", "--shift", "+999999d999999d999999d", self.era, "--yes", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("impossible date", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        r = self.run_cli("fix-dates", "--like", self.dup1, self.era, ok=False)
        self.assertIn("no camera date", r.stderr)
        self.assert_originals_unchanged()

    def test_undo_refused_after_a_later_change(self):
        self.check()
        self.run_cli("fix-dates", "--set", "2024-01-01T08:00", self.era, "--yes")
        set_side(self.era, "-XMP-lr:HierarchicalSubject+=Trips|Spain")  # someone tags it since
        r = self.run_cli("fix-dates", "--undo", "--yes", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("changed since", r.stdout + r.stderr)
        self.assertEqual(side_date(self.era), "2024:01:01 08:00:00", "nothing was put back")

    def test_tag_rerun_keeps_a_fixed_date(self):
        # the reset shot was filed by a batch whose plan holds the camera's wrong date
        b = os.path.join(self.state, "batches", "gopro-1")
        os.makedirs(b)
        entry = dict(rel="DCIM/100GOPRO/GOPR0104.JPG", sha=sha(self.reset), size=os.path.getsize(self.reset),
                     action="file", dst=self.reset, lib_dst=self.reset, date="2016-01-01T00:03:00", date_tz=None,
                     cat="gopro", device="gopro-hero12-black", device_model="HERO12 Black")
        with open(os.path.join(b, "plan.json"), "w") as f:
            json.dump([entry], f)
        with open(os.path.join(b, "manifest.json"), "w") as f:
            json.dump(dict(kind="gopro", root="/nowhere", files={entry["rel"]: dict(src="/nowhere", size=1)}), f)
        self.run_cli("tag", "gopro-1", "--no-labels")
        self.assertEqual(side_date(self.reset), "2016:01:01 00:03:00")  # tag writes the plan's date
        self.run_cli("fix-dates", "--set", "2024-05-12T10:30", self.reset, "--yes")
        self.run_cli("tag", "gopro-1", "--no-labels")
        self.assertEqual(side_date(self.reset), "2024:05:12 10:30:00", "a rerun of tag never undoes a fix")
        self.assert_originals_unchanged()

    def test_same_answer_in_another_time_zone_and_a_repeat_writes_nothing(self):
        utc = self.by(self.check())
        self.env["TZ"] = "America/New_York"  # EXIF and sidecar dates are wall times: no zone moves them
        ny = self.by(self.check())
        for p in (self.reset, self.outlier, self.era, self.named):
            self.assertEqual({k: (f["date"], f["suggest"]) for k, f in ny[p].items()},
                             {k: (f["date"], f["suggest"]) for k, f in utc[p].items()}, p)
        # GPS time is UTC: its suggestion is local time on this machine, as plan's GPS date is (EDT: -4 h)
        self.assertEqual(utc[self.gps]["sources"]["suggest"], "2024-08-08T12:00:00")
        self.assertEqual(ny[self.gps]["sources"]["suggest"], "2024-08-08T08:00:00")
        self.run_cli("fix-dates", "--set", "2024-03-10T02:30", self.era, "--yes")  # inside the DST gap there
        self.assertEqual(side_date(self.era), "2024:03:10 02:30:00")
        self.run_cli("fix-dates", "--shift", "+1d", self.era, "--yes")
        self.assertEqual(side_date(self.era), "2024:03:11 02:30:00")
        with open(os.path.join(self.state, "date-fixes.json")) as fh:
            n = len(json.load(fh)["fixes"])
        r = self.run_cli("fix-dates", "--set", "2024-03-11T02:30", self.era, "--yes", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already has that date", r.stdout)
        with open(os.path.join(self.state, "date-fixes.json")) as fh:
            self.assertEqual(len(json.load(fh)["fixes"]), n, "a repeat records nothing")
        self.assert_originals_unchanged()

    def test_quarantined_file_never_read_or_fixed(self):
        q = os.path.join(self.state, "quarantine", "b1", "DCIM", "GOPR0777.JPG")
        jpg(q, 70, "2016:01:01 00:00:00", "-Make=GoPro", "-Model=HERO12 Black")
        rep = self.check(q)
        self.assertEqual(rep["files"], 0)
        self.assertEqual(rep["findings"], [])
        r = self.run_cli("fix-dates", "--set", "2024-01-01", q, "--yes", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", r.stdout)
        self.assertFalse(os.path.exists(q + ".xmp"))

    def test_a_write_stopped_part_way_is_recorded_and_undoable(self):
        self.check()
        # a fake exiftool first on PATH: writes the first sidecar it is given, then fails
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake)
        real = shutil.which("exiftool")
        with open(os.path.join(fake, "exiftool"), "w") as f:
            f.write("#!/bin/sh\n"
                    "if [ \"$1\" = \"-@\" ]; then\n"
                    f"  head -n 5 | {real} -@ - >/dev/null 2>&1\n"
                    "  echo 'stopped' >&2; exit 1\nfi\n"
                    f"exec {real} \"$@\"\n")
        os.chmod(os.path.join(fake, "exiftool"), 0o700)
        env = dict(self.env, PATH=fake + os.pathsep + self.env["PATH"])
        r = subprocess.run([PY, "-I", INTAKE, "fix-dates", "--set", "2024-01-01T08:00", self.era, self.named, "--yes"],
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("recorded", r.stderr)
        self.assertEqual(side_date(self.era), "2024:01:01 08:00:00")  # the first block went through
        self.assertIsNone(side_date(self.named))
        self.run_cli("fix-dates", "--undo", "--yes")
        self.assertIsNone(side_date(self.era))
        self.assert_originals_unchanged()

    def test_fix_dates_takes_the_library_lock(self):
        import library_lock  # noqa: F401
        self.assertIn("fix-dates", intake.LOCKED)
        self.assertIn("check-dates", intake.READ_ONLY)


def datecheck_report(state):
    with open(os.path.join(state, "datecheck.json")) as f:
        return json.load(f)


if __name__ == "__main__":
    unittest.main()
