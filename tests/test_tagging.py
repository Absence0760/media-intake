#!/usr/bin/env python3
"""Tagging tests: XMP sidecars, offline geocoding, the SigLIP labeler, find/mark-used,
and sidecars going with erased files. Synthetic media only, throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_tagging.py

Geocoding and labeler cases use the real venv (and the real model, read-only)
and skip when setup.sh hasn't run; geocoding runs on a small fixture of
GeoNames-format lines, not the downloaded file.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
from test_intake import INTAKE, PY, REAL_VENV, pattern, save_jpg, video  # noqa: E402

REAL_STATE = os.path.join(os.path.expanduser("~"), ".local/share/media-intake")
VENV_PY = os.path.join(REAL_VENV, "bin", "python")
HAVE_VENV = os.path.exists(VENV_PY)
MODELS = os.path.join(REAL_STATE, "models")
HAVE_MODEL = HAVE_VENV and os.path.isdir(MODELS) and os.path.exists(os.path.join(HERE, "labels.npz"))
POINT_A = (37.5407, -77.4360)


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def city_line(gid, name, lat, lon, cc, admin1, code="PPLA", pop=1000):
    return "\t".join([str(gid), name, name, "", str(lat), str(lon), "P", code, cc, "", admin1,
                      "", "", "", str(pop), "", "100", "America/New_York", "2026-01-01"])


def write_geo(d):
    os.makedirs(d, exist_ok=True)
    rows = [city_line(1, "Riverton", 37.55376, -77.46026, "US", "EL", pop=226610),
            city_line(2, "Old Hill", 37.5410, -77.4370, "US", "EL", code="PPLX"),  # neighbourhood: skipped
            city_line(3, "Millbrook", 37.22793, -77.40193, "US", "EL"),
            city_line(4, "Bayview", 36.60024, -121.89468, "US", "WL")]
    with zipfile.ZipFile(os.path.join(d, "cities1000.zip"), "w") as z:
        z.writestr("cities1000.txt", "\n".join(rows) + "\n")
    with open(os.path.join(d, "admin1CodesASCII.txt"), "w") as f:
        f.write("US.EL\tEastland\tEastland\t6254928\nUS.WL\tWestland\tWestland\t5332921\n")
    with open(os.path.join(d, "countryInfo.txt"), "w") as f:
        f.write("#ISO\tISO3\tISO-Numeric\tfips\tCountry\n"
                "US\tUSA\t840\tUS\tUnited States\tWashington\n")


def gps_jpg(path, lat, lon, dt="2026:11:08 07:15:02", seed=1):
    from PIL import Image
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = dt
    g = ex.get_ifd(0x8825)
    def dms(v):
        v = abs(v)
        d, m = int(v), int((abs(v) - int(v)) * 60)
        return (float(d), float(m), round(((v - d) * 60 - m) * 60, 3))
    g[1], g[2] = ("N" if lat >= 0 else "S"), dms(lat)
    g[3], g[4] = ("E" if lon >= 0 else "W"), dms(lon)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pattern(seed).save(path, exif=ex, quality=90)


def xmp(path):
    r = subprocess.run(["exiftool", "-j", "-n", "-G1", "-XMP:all", path + ".xmp"], capture_output=True, text=True, check=True)
    row = json.loads(r.stdout)[0]
    for k in ("XMP-lr:HierarchicalSubject", "XMP-dc:Subject"):
        if isinstance(row.get(k), str):
            row[k] = [row[k]]
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-tag-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.footage = os.path.join(self.home, "Videos/demo-footage")
        os.makedirs(self.state)
        os.chmod(self.state, 0o700)
        if HAVE_VENV:
            os.symlink(REAL_VENV, os.path.join(self.state, "venv"))  # read-only use
        if os.path.isdir(MODELS):
            os.symlink(MODELS, os.path.join(self.state, "models"))  # read-only use
        write_geo(os.path.join(self.state, "geo"))
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.footage), TZ="America/New_York",
                        MEDIA_INTAKE_GEO_NOPIN="1", MEDIA_INTAKE_NO_LOCALSEARCH="1",
                        MEDIA_INTAKE_WIPE_ANY_DEST="1", MEDIA_INTAKE_WIPE_MAX_MB="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def venv(self, script, payload, *extra):
        return subprocess.run([VENV_PY, "-I", os.path.join(HERE, script), *extra], env=self.env,
                              input=json.dumps(payload), capture_output=True, text=True)


@unittest.skipUnless(HAVE_VENV and shutil.which("exiftool"), "venv or exiftool missing")
class Sidecars(Base):
    def make(self):
        self.jpg = os.path.join(self.lib, "2026", "2026-11", "20261108_071502.jpg")
        gps_jpg(self.jpg, *POINT_A)
        shoot = os.path.join(self.footage, "originals", "2026-11-08-city-marathon", "gopro")
        self.mp4 = os.path.join(shoot, "GX010123.MP4")
        video(self.mp4, "2026-11-08T12:00:00Z")
        # GPS on the synthetic video (a QuickTime GPSCoordinates atom, as phones write)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-Keys:GPSCoordinates=37.5407, -77.436, 50", self.mp4],
                       check=True)
        self.far = os.path.join(self.lib, "2026", "2026-10", "far.jpg")
        gps_jpg(self.far, 0.5, -140.0, seed=2)  # mid-Pacific: no place
        self.sums = {p: sha(p) for p in (self.jpg, self.mp4, self.far)}

    def test_sidecar_fields_and_originals_untouched(self):
        self.make()
        out = self.intake("tag-files", self.jpg, self.mp4, self.far, "--no-labels")
        self.assertIn("tagged 3 files", out)
        for p, s in self.sums.items():
            self.assertEqual(sha(p), s, f"original changed: {p}")
            self.assertTrue(os.path.isfile(p + ".xmp"), p)
        j = xmp(self.jpg)
        self.assertEqual(set(j["XMP-lr:HierarchicalSubject"]),
                         {"Project|Personal", "Source|phone camera", "Place|Riverton, Eastland, US"})
        self.assertEqual(j["XMP-photoshop:City"], "Riverton")
        self.assertEqual(j["XMP-photoshop:State"], "Eastland")
        self.assertEqual(j["XMP-photoshop:Country"], "United States")
        self.assertAlmostEqual(j["XMP-exif:GPSLatitude"], POINT_A[0], places=3)
        self.assertAlmostEqual(j["XMP-exif:GPSLongitude"], POINT_A[1], places=3)
        self.assertEqual(j["XMP-exif:DateTimeOriginal"], "2026:11:08 07:15:02")
        self.assertIn("Riverton, Eastland, US", j["XMP-dc:Subject"])
        v = xmp(self.mp4)
        self.assertEqual(set(v["XMP-lr:HierarchicalSubject"]),
                         {"Project|Demo", "Source|gopro", "Event|city-marathon", "Place|Riverton, Eastland, US"})
        self.assertEqual(v["XMP-iptcExt:Event"], "city-marathon")
        f = xmp(self.far)
        self.assertFalse([t for t in f["XMP-lr:HierarchicalSubject"] if t.startswith("Place|")])
        self.assertAlmostEqual(f["XMP-exif:GPSLatitude"], 0.5, places=3)
        # re-tag: nothing duplicated, originals still untouched
        self.intake("tag-files", self.jpg, self.mp4, "--no-labels")
        self.intake("mark-used", self.mp4, "--post", "2026-11-09-demo-riverton-recap")
        self.intake("mark-used", self.mp4, "--post", "2026-11-09-demo-riverton-recap")
        v = xmp(self.mp4)
        self.assertEqual(len(v["XMP-lr:HierarchicalSubject"]), len(set(v["XMP-lr:HierarchicalSubject"])))
        self.assertEqual(len(v["XMP-dc:Subject"]), len(set(v["XMP-dc:Subject"])))
        self.assertIn("Used|2026-11-09-demo-riverton-recap", v["XMP-lr:HierarchicalSubject"])
        for p, s in self.sums.items():
            self.assertEqual(sha(p), s, f"original changed: {p}")
        self.intake("mark-used", self.mp4, "--post", "no-date-slug", code=1)

        # find
        def find(*args):
            return self.intake("find", *args).split()
        self.assertEqual(find("--tag", "Project|Demo"), [self.mp4])
        self.assertEqual(sorted(find("--place", "riverton")), sorted([self.jpg, self.mp4]))
        self.assertEqual(find("--event", "city-marathon", "--tag", "Source|gopro"), [self.mp4])
        self.assertEqual(sorted(find("--tag", "Place")), sorted([self.jpg, self.mp4]))
        self.assertEqual(sorted(find("--never-used")), sorted([self.jpg, self.far]))
        # used in a post dated 9 Nov: inside a 30-day window until 9 Dec 2026; never inside a 0-day one
        if time.strftime("%Y-%m-%d") < "2026-12-09":
            self.assertNotIn(self.mp4, find("--unused-since", "30d"))
        self.assertIn(self.mp4, find("--unused-since", "0d"))

    def test_tag_batch_skips_flagged_and_review(self):
        phone = os.path.join(self.tmp, "phone")
        gps_jpg(os.path.join(phone, "DCIM/Camera/20261108_071502.jpg"), *POINT_A)
        save_jpg(pattern(4), os.path.join(phone, "DCIM/Camera/20261023_100000.jpg"), "2026:10:23 10:00:00")
        save_jpg(pattern(5), os.path.join(phone, "DCIM/Camera/20261024_100000.jpg"), "2026:10:24 10:00:00")
        pattern(7, (50, 50)).save(os.path.join(phone, "DCIM/Camera/tiny.png"))
        batch = "tag-test"
        self.intake("stage", phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch, "--event", "birthday-party@2026-10-24")
        bd = os.path.join(self.state, "batches", batch)
        with open(os.path.join(bd, "scan.json"), "w") as f:
            json.dump({"DCIM/Camera/20261023_100000.jpg": ["nudity"]}, f)
        self.intake("apply", batch, "--allow-missing-detectors")
        out = self.intake("tag", batch, "--no-labels")
        self.assertIn("2 filed files to tag", out)
        filed = os.path.join(self.lib, "2026", "2026-11", "20261108_071502.jpg")
        party = os.path.join(self.lib, "2026", "2026-10", "20261024_100000.jpg")
        self.assertIn("Place|Riverton, Eastland, US", xmp(filed)["XMP-lr:HierarchicalSubject"])
        self.assertEqual(set(xmp(party)["XMP-lr:HierarchicalSubject"]),
                         {"Project|Personal", "Source|phone camera", "Event|birthday-party"})
        quar = os.path.join(self.state, "quarantine", batch)
        sidecars = [os.path.join(r, f) for r, _, fs in os.walk(self.state) for f in fs if f.endswith(".xmp")]
        self.assertEqual(sidecars, [], "nothing in quarantine or review gets a sidecar")
        self.assertTrue(os.path.isfile(os.path.join(quar, "DCIM/Camera/20261023_100000.jpg")))
        # a released (scan-flagged) file is still never tagged
        self.intake("release", batch, "DCIM/Camera/20261023_100000.jpg")
        rel = os.path.join(self.lib, "2026", "2026-10", "20261023_100000.jpg")
        self.assertIn("skipped 1 files", self.intake("tag-files", rel, "--no-labels"))
        self.assertFalse(os.path.exists(rel + ".xmp"))

    def test_erase_takes_sidecars(self):
        self.make()
        copy = os.path.join(self.home, "Downloads", "IMG-1.jpg")
        os.makedirs(os.path.dirname(copy))
        shutil.copyfile(self.jpg, copy)
        self.intake("tag-files", self.jpg, copy, self.far, "--no-labels")
        orphan = os.path.join(self.home, "Pictures", "gone.jpg.xmp")  # its photo was deleted by hand
        shutil.copyfile(self.far + ".xmp", orphan)
        out = self.intake("forget", self.jpg, "--yes")
        self.assertIn("home sweep: 1 more copies deleted", out)
        self.assertIn("orphan sidecars deleted", out)
        for p in (self.jpg, self.jpg + ".xmp", copy, copy + ".xmp", orphan):
            self.assertFalse(os.path.exists(p), p)
        self.assertTrue(os.path.exists(self.far + ".xmp"), "other sidecars stay")
        self.assertIn("no copy of 1 erased items", self.intake("verify-gone"))
        shutil.copyfile(self.far + ".xmp", orphan)
        self.assertIn("ORPHAN SIDECARS: 1", self.intake("verify-gone", code=1))
        self.assertTrue(os.path.exists(orphan), "verify-gone is read-only")


@unittest.skipUnless(HAVE_VENV, "venv not set up")
class Geocode(Base):
    def test_nearest_within_50km(self):
        r = self.venv("geocode.py", [list(POINT_A), [37.30, -77.40], [0.5, -140.0], [36.6, -121.9]],
                      os.path.join(self.state, "geo"))
        self.assertEqual(r.returncode, 0, r.stderr)
        rich, pete, none, mont = json.loads(r.stdout)
        self.assertEqual((rich["city"], rich["state"], rich["cc"], rich["country"]),
                         ("Riverton", "Eastland", "US", "United States"))
        self.assertLess(rich["km"], 5)
        self.assertEqual(pete["city"], "Millbrook")
        self.assertIsNone(none)
        self.assertEqual(mont["state"], "Westland")

    def test_pin_mismatch_refused(self):
        env = dict(self.env)
        env.pop("MEDIA_INTAKE_GEO_NOPIN")
        r = subprocess.run([VENV_PY, "-I", os.path.join(HERE, "geocode.py"), os.path.join(self.state, "geo")],
                           env=env, input="[]", capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("geo-pins.txt", r.stderr)


@unittest.skipUnless(HAVE_VENV, "venv not set up")
class LabelerGuard(Base):
    def test_refuses_quarantine_and_flagged(self):
        q = os.path.join(self.state, "quarantine", "b1", "DCIM/Camera/x.jpg")
        save_jpg(pattern(3), q)
        r = self.venv("label.py", [q])
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(r.stdout, "")
        # a library file whose content scan flagged in some batch
        lib = os.path.join(self.lib, "2026", "x.jpg")
        save_jpg(pattern(4), lib)
        bd = os.path.join(self.state, "batches", "b2")
        os.makedirs(bd)
        with open(os.path.join(bd, "plan.json"), "w") as f:
            json.dump([{"rel": "DCIM/a.jpg", "sha": sha(lib), "size": os.path.getsize(lib)}], f)
        with open(os.path.join(bd, "scan.json"), "w") as f:
            json.dump({"DCIM/a.jpg": ["passport-mrz"]}, f)
        self.assertEqual(self.venv("label.py", [lib]).returncode, 3)


@unittest.skipUnless(HAVE_MODEL, "SigLIP model or labels.npz not present (bash tools/media/setup.sh)")
class RealLabeler(Base):
    def test_model_loads_and_returns_json(self):
        img = os.path.join(self.lib, "2026", "a.jpg")
        save_jpg(pattern(1), img)
        vid = os.path.join(self.vids, "2026", "v.mp4")
        video(vid, "2026-11-08T12:00:00Z", seconds=4)
        r = self.venv("label.py", [img, vid], "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(set(out), {img, vid})
        for v in out.values():
            self.assertIn("runner", v["labels"])
            self.assertTrue(all(0.0 <= s <= 1.0 for s in v["labels"].values()))
        r = self.venv("label.py", [img])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsInstance(json.loads(r.stdout)[img]["labels"], dict)


if __name__ == "__main__":
    unittest.main()
