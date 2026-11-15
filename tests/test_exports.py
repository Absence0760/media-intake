#!/usr/bin/env python3
"""Cloud exports (exports.py): Google Takeout JSON and iCloud Photo Details CSV dates and places.

  python3 -I -m unittest discover -s tests -p test_exports.py

Synthetic exports only, in a throwaway HOME: fabricated Takeout folders with JSON sidecars
(new and old names, truncated long names, a (1) duplicate, an -edited copy, a Live Photo pair,
0,0 places, a symlinked and a non-Takeout JSON) and an iCloud folder with its CSV. The name
cases follow the open-source importers' own test cases (docs/intake.md, "Cloud exports").
"""
import datetime, hashlib, json, os, shutil, subprocess, sys, tempfile, types, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
import dates  # noqa: E402
import exports  # noqa: E402

PY = sys.executable
INTAKE = os.path.join(HERE, "intake.py")
UTC = datetime.timezone.utc
T2019 = 1557489600  # 2019-05-10 12:00:00 UTC
T2020 = 1592222400  # 2020-06-15 12:00:00 UTC


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def takeout_json(path, ts, geo=(0.0, 0.0), geo_exif=(0.0, 0.0), title=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d = {"title": title or os.path.basename(path), "description": "",
         "creationTime": {"timestamp": str(T2020 + 999), "formatted": "x"},
         "photoTakenTime": {"timestamp": str(ts), "formatted": "x"},
         "geoData": {"latitude": geo[0], "longitude": geo[1], "altitude": 0.0},
         "geoDataExif": {"latitude": geo_exif[0], "longitude": geo_exif[1], "altitude": 0.0}}
    with open(path, "w") as f:
        json.dump(d, f)


def jpg(path, seed):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (640, 480), (40 * seed % 255, 90, 160)).save(path, quality=90)  # no EXIF date


class Names(unittest.TestCase):
    """The JSON names Takeout gives a media file (cases from immich-go's matcher tests)."""

    def test_google_names(self):
        cases = {
            "IMG_0001.jpg": "IMG_0001.jpg.supplemental-metadata.json",
            "IMG_0001(1).jpg": "IMG_0001.jpg.supplemental-metadata(1).json",
            "Screenshot_20231027_123303_Facebook.jpg": "Screenshot_20231027_123303_Facebook.jpg.supple.json",
            "MVIMG_20191230_232926.jpg": "MVIMG_20191230_232926.jpg.supplemental-metadat.json",
            "Screenshot_20200301-161151.png": "Screenshot_20200301-161151.png.supplemental-me.json",
            "CLIP0001(10).AVI": "CLIP0001.AVI.supplemental-metadata(10).json",
        }
        for media, js in cases.items():
            self.assertEqual(exports.google_names(media)[0], js, media)
        old = {  # the older form: no suffix, the whole name cut to 46 characters
            "DSC_0101(1).JPG": "DSC_0101.JPG(1).json",
            "PXL_20230809_203449253.LONG_EXPOSURE-02.ORIGINA.jpg": "PXL_20230809_203449253.LONG_EXPOSURE-02.ORIGIN.json",
            "05yqt21kruxwwlhhgrwrdyb6chhwszi9bqmzu16w0 2.jpg": "05yqt21kruxwwlhhgrwrdyb6chhwszi9bqmzu16w0 2.jp.json",
            "13039_327707840323_537645323_9470255_27214_n(1).jpg": "13039_327707840323_537645323_9470255_27214_n.j(1).json",
        }
        for media, js in old.items():
            self.assertIn(js, exports.google_names(media), media)
        # A file without a counter never takes a numbered JSON, and the reverse.
        self.assertNotIn("DSC_0104.JPG.json", exports.google_names("DSC_0104(1).JPG"))
        self.assertNotIn("DSC_0103.JPG(1).json", exports.google_names("DSC_0103.JPG"))

    def test_fallbacks(self):
        self.assertIn("IMAG0061.JPG.supplemental-metadata.json", exports.fallback_names("IMAG0061-edited.JPG", []))
        sib = ["IMG_4488(1).HEIC", "IMG_4488(1).MP4", "IMG_4488(2).HEIC"]
        self.assertEqual(exports.fallback_names("IMG_4488(1).MP4", sib)[0], "IMG_4488.HEIC.supplemental-metadata(1).json")
        self.assertEqual(exports.fallback_names("IMG_4488.jpg", ["IMG_4488.HEIC"]), [], "only a video takes the still's")

    def test_icloud_dates(self):
        self.assertEqual(exports.parse_icloud_date("Saturday June 4,2022 12:11 PM GMT"),
                         datetime.datetime(2022, 6, 4, 12, 11, tzinfo=UTC))
        self.assertEqual(exports.parse_icloud_date("Friday October 06,2023 9:05 AM GMT"),
                         datetime.datetime(2023, 10, 6, 9, 5, tzinfo=UTC))
        for bad in ("", "yesterday", "Saturday June 31,2022 12:11 PM GMT"):
            self.assertIsNone(exports.parse_icloud_date(bad), bad)


class Scan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exports-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def files(self):
        out = []
        for r, _, fs in os.walk(self.root):
            for n in fs:
                if os.path.splitext(n)[1].lower() in (".jpg", ".heic", ".mp4", ".png"):
                    p = os.path.join(r, n)
                    out.append((p, os.path.relpath(p, self.root)))
        return sorted(out)

    def test_takeout_tree(self):
        y = os.path.join(self.root, "Takeout", "Google Photos", "Photos from 2019")
        touch(os.path.join(y, "IMG_0001.jpg"))
        takeout_json(os.path.join(y, "IMG_0001.jpg.supplemental-metadata.json"), T2019,
                     geo=(48.8584, 2.2945))  # geoDataExif 0,0: falls back to geoData
        touch(os.path.join(y, "IMG_0001(1).jpg"))
        takeout_json(os.path.join(y, "IMG_0001.jpg.supplemental-metadata(1).json"), T2020)
        touch(os.path.join(y, "IMG_0002.jpg"))
        takeout_json(os.path.join(y, "IMG_0002.jpg.json"), T2019, geo_exif=(10.5, 20.25), geo=(1.0, 2.0))
        long_name = "Screenshot_20190510_120000_com.example.app.jpg"  # 46 + .jpg: the suffix is cut away
        touch(os.path.join(y, long_name))
        takeout_json(os.path.join(y, (long_name + exports.SUPPLEMENTAL)[:46] + ".json"), T2019)
        touch(os.path.join(y, "IMG_0003.jpg"))
        touch(os.path.join(y, "IMG_0003-edited.jpg"))
        takeout_json(os.path.join(y, "IMG_0003.jpg.supplemental-metadata.json"), T2020)
        touch(os.path.join(y, "IMG_4488.HEIC"))
        touch(os.path.join(y, "IMG_4488.MP4"))
        takeout_json(os.path.join(y, "IMG_4488.HEIC.supplemental-metadata.json"), T2019)
        # Two long names with the same first 46 characters claim one JSON: neither gets it.
        a, b = "A" * 50 + "_one.jpg", "A" * 50 + "_two.jpg"
        touch(os.path.join(y, a))
        touch(os.path.join(y, b))
        takeout_json(os.path.join(y, "A" * 46 + ".json"), T2019)
        # Not Takeout: no photoTakenTime; and a symlinked sidecar, never followed.
        touch(os.path.join(y, "IMG_0004.jpg"))
        with open(os.path.join(y, "IMG_0004.jpg.supplemental-metadata.json"), "w") as f:
            json.dump({"title": "x"}, f)
        touch(os.path.join(y, "IMG_0005.jpg"))
        elsewhere = os.path.join(self.root, "elsewhere.json")
        takeout_json(elsewhere, T2019)
        os.symlink(elsewhere, os.path.join(y, "IMG_0005.jpg.supplemental-metadata.json"))
        with open(os.path.join(y, "metadata.json"), "w") as f:  # an album's own JSON
            json.dump({"title": "Photos from 2019"}, f)

        out, c = exports.scan(self.root, self.files())
        rel = lambda n: os.path.join("Takeout", "Google Photos", "Photos from 2019", n)  # noqa: E731
        self.assertEqual(out[rel("IMG_0001.jpg")]["time"], "2019-05-10 12:00:00Z")
        self.assertEqual((out[rel("IMG_0001.jpg")]["lat"], out[rel("IMG_0001.jpg")]["lon"]), (48.8584, 2.2945))
        self.assertEqual(out[rel("IMG_0001.jpg")]["via"], "google-takeout")
        self.assertEqual(out[rel("IMG_0001.jpg")]["sidecar"], rel("IMG_0001.jpg.supplemental-metadata.json"))
        self.assertEqual(out[rel("IMG_0001(1).jpg")]["time"], "2020-06-15 12:00:00Z", "the (1) copy has its own JSON")
        self.assertNotIn("lat", out[rel("IMG_0001(1).jpg")], "0,0 is no place")
        self.assertEqual((out[rel("IMG_0002.jpg")]["lat"], out[rel("IMG_0002.jpg")]["lon"]), (10.5, 20.25),
                         "geoDataExif first")
        self.assertEqual(out[rel(long_name)]["time"], "2019-05-10 12:00:00Z")
        self.assertEqual(out[rel("IMG_0003-edited.jpg")]["time"], "2020-06-15 12:00:00Z")
        self.assertEqual(out[rel("IMG_0003.jpg")]["time"], "2020-06-15 12:00:00Z")
        self.assertEqual(out[rel("IMG_4488.MP4")]["time"], "2019-05-10 12:00:00Z", "Live Photo video: the still's")
        for n in (a, b, "IMG_0004.jpg", "IMG_0005.jpg"):
            self.assertNotIn(rel(n), out, n)
        self.assertEqual(c, dict(google=8, icloud=0, places=2, ambiguous=2))

    def test_icloud_csv(self):
        d = os.path.join(self.root, "iCloud Photos Part 1 of 1", "Photos")
        for n in ("IMG_7938.HEIC", "IMG_7939.jpg", "IMG_7940.jpg", "IMG_7941.jpg"):
            touch(os.path.join(d, n))
        with open(os.path.join(d, "Photo Details.csv"), "w", encoding="utf-8-sig") as f:
            f.write("imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
                    'IMG_7938.HEIC,c1,no,no,no,"Saturday June 4,2022 12:11 PM GMT",10,"Monday June 6,2022 8:00 AM GMT"\n'
                    'IMG_7939.jpg,c2,no,no,no,"Friday October 06,2023 9:05 PM GMT",1,"Friday October 06,2023 9:05 PM GMT"\n'
                    'IMG_7940.jpg,c3,no,no,no,"not a date",1,"Friday October 06,2023 9:05 PM GMT"\n')
        with open(os.path.join(d, "Photo Details-1.csv"), "w") as f:  # a second CSV contradicts IMG_7939
            f.write("imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
                    'IMG_7939.jpg,c9,no,no,no,"Saturday June 4,2022 12:11 PM GMT",1,"x"\n')
        with open(os.path.join(d, "Other.csv"), "w") as f:  # not a Photo Details file
            f.write('imgName,originalCreationDate\nIMG_7941.jpg,"Saturday June 4,2022 12:11 PM GMT"\n')
        out, c = exports.scan(self.root, self.files())
        r = os.path.join("iCloud Photos Part 1 of 1", "Photos")
        self.assertEqual(out, {os.path.join(r, "IMG_7938.HEIC"): dict(
            time="2022-06-04 12:11:00Z", via="icloud", sidecar=os.path.join(r, "Photo Details.csv"))})
        self.assertEqual(c, dict(google=0, icloud=1, places=0, ambiguous=1))


class Dates(unittest.TestCase):
    """The export date's rank: below the file's own clock and GPS, above the name and mtime; never shifted."""
    NOW = datetime.datetime(2026, 1, 1)
    EXP = "2019-05-10 12:00:00Z"

    def r(self, name, meta, offsets=None):
        return dates.resolve(os.path.join("/x", name), dict(meta, mtime=1700000000), offsets, self.NOW, UTC)

    def test_rank(self):
        d = self.r("IMG_20180101_101010.jpg", {"ExportTime": self.EXP})
        self.assertEqual((d.src, d.dt, d.tz), ("export", datetime.datetime(2019, 5, 10, 12), "+00:00"),
                         "above the file name and mtime")
        d = self.r("x.jpg", {"ExportTime": self.EXP, "DateTimeOriginal": "2019:05:09 08:00:00"})
        self.assertEqual(d.src, "exif", "the file's own EXIF wins")
        d = self.r("x.jpg", {"ExportTime": self.EXP, "GPSDateTime": "2019:05:01 08:00:00Z"})
        self.assertEqual(d.src, "gps")
        d = self.r("IMG_20180101_101010.jpg", {"ExportTime": "1970-01-01 00:00:00Z"})
        self.assertEqual(d.src, "filename", "an impossible export date gives way")
        self.assertIn("export", dates.SOURCES)

    def test_never_shifted(self):
        offs = dates.clock_offsets(["all=+2h"])
        d = self.r("x.jpg", {"ExportTime": self.EXP}, offs)
        self.assertEqual((d.src, d.dt), ("export", datetime.datetime(2019, 5, 10, 12)))


class StagePlan(unittest.TestCase):
    """stage reads the export into the manifest; plan dates by it and drops album copies and library dups."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exports-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"), TZ="UTC",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        MEDIA_INTAKE_NO_LOCALSEARCH="1")
        for k in ("MEDIA_INTAKE_TEST_MOUNT", "MEDIA_INTAKE_TEST_UUID"):
            self.env.pop(k, None)

    def intake(self, *args):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def test_takeout_folder(self):
        gp = os.path.join(self.home, "Downloads-unzipped", "Takeout", "Google Photos")
        year, album = os.path.join(gp, "Photos from 2019"), os.path.join(gp, "Trip")
        jpg(os.path.join(year, "IMG_0001.jpg"), 1)
        takeout_json(os.path.join(year, "IMG_0001.jpg.supplemental-metadata.json"), T2019, geo=(48.8584, 2.2945))
        jpg(os.path.join(year, "IMG_0001(1).jpg"), 2)
        takeout_json(os.path.join(year, "IMG_0001.jpg.supplemental-metadata(1).json"), T2020)
        os.makedirs(album)
        shutil.copyfile(os.path.join(year, "IMG_0001.jpg"), os.path.join(album, "IMG_0001.jpg"))  # the album's copy
        takeout_json(os.path.join(album, "IMG_0001.jpg.supplemental-metadata.json"), T2019)
        jpg(os.path.join(year, "IMG_0009.jpg"), 9)  # already in the library: an exact duplicate
        takeout_json(os.path.join(year, "IMG_0009.jpg.json"), T2019)
        os.makedirs(self.state)
        with open(os.path.join(year, "IMG_0009.jpg"), "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({os.path.join(self.lib, "2019/2019-05/IMG_0009.jpg"): {"sha": sha, "size": 1}}, f)

        out = self.intake("stage", os.path.dirname(os.path.dirname(gp)), "--kind", "folder", "--batch", "takeout-1")
        self.assertIn("staged 4 media files", out)
        self.assertIn("Google Takeout 4, iCloud 0, with a place 1, ambiguous (ignored) 0", out)
        b = os.path.join(self.state, "batches", "takeout-1")
        with open(os.path.join(b, "manifest.json")) as f:
            man = json.load(f)
        self.assertFalse([r for r in man["files"] if r.endswith(".json")], "a sidecar is never staged")
        r1 = os.path.join("Takeout", "Google Photos", "Photos from 2019", "IMG_0001.jpg")
        self.assertEqual(man["files"][r1]["export"]["time"], "2019-05-10 12:00:00Z")

        self.intake("plan", "takeout-1")
        with open(os.path.join(b, "plan.json")) as f:
            plan = {p["rel"]: p for p in json.load(f)}
        p1 = plan[r1]
        self.assertEqual((p1["date_src"], p1["date"], p1["action"]), ("export", "2019-05-10T12:00:00", "file"))
        self.assertEqual(p1["export_gps"], [48.8584, 2.2945])
        self.assertTrue(p1["dst"].endswith(os.path.join("2019", "2019-05", "IMG_0001.jpg")))
        dup = plan[r1.replace("IMG_0001.jpg", "IMG_0001(1).jpg")]
        self.assertEqual((dup["date_src"], dup["date"][:10]), ("export", "2020-06-15"))
        self.assertIsNone(dup["export_gps"])
        alb = plan[os.path.join("Takeout", "Google Photos", "Trip", "IMG_0001.jpg")]
        self.assertEqual(alb["action"], "delete")
        self.assertIn("duplicate of", alb["why"])
        lib = plan[os.path.join("Takeout", "Google Photos", "Photos from 2019", "IMG_0009.jpg")]
        self.assertEqual((lib["action"], lib["why"].split(" ")[:3]), ("delete", ["duplicate", "of", "library"]))


class TagPlace(unittest.TestCase):
    """tag uses the export's place only when the file has no GPS of its own."""

    def test_fallback_place(self):
        support.check_hermetic()
        import intake
        tmp = tempfile.mkdtemp(prefix="exports-tag-")
        self.addCleanup(shutil.rmtree, tmp, True)
        own, plain = os.path.join(tmp, "own.jpg"), os.path.join(tmp, "plain.jpg")
        touch(own)
        touch(plain)
        written, asked = [], []
        fakes = dict(need_exiftool=lambda: None, read_sidecar_tags=lambda paths: {},
                     guard_label=lambda: types.SimpleNamespace(refused=lambda paths, state: []),
                     read_gps=lambda paths: {own: (1.0, 2.0)},
                     venv_json=lambda script, pts, extra=(): asked.extend(pts) or [None] * len(pts),
                     write_tags=written.extend)
        saved = {k: getattr(intake, k) for k in fakes}
        self.addCleanup(lambda: [setattr(intake, k, v) for k, v in saved.items()])
        for k, v in fakes.items():
            setattr(intake, k, v)
        intake.tag_files([(own, "Personal", "photo", None, None, [48.0, 2.0]),
                          (plain, "Personal", "photo", None, None, [48.8584, 2.2945])], labels=False)
        got = {p: (t.get("lat"), t.get("lon")) for p, t in written}
        self.assertEqual(got, {own: (1.0, 2.0), plain: (48.8584, 2.2945)}, "the file's own GPS wins")
        self.assertEqual(sorted(asked), [[1.0, 2.0], [48.8584, 2.2945]], "both geocoded offline")


if __name__ == "__main__":
    unittest.main()
