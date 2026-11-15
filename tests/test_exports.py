#!/usr/bin/env python3
"""Cloud exports (exports.py): Google Takeout JSON and iCloud Photo Details CSV dates and places.

  python3 -I -m unittest discover -s tests -p test_exports.py

Synthetic exports only, in a throwaway HOME: fabricated Takeout folders with JSON sidecars
(new and old names, truncated long names, a (1) duplicate, an -edited copy, a Live Photo pair,
0,0 places, a symlinked and a non-Takeout JSON) and an iCloud folder with its CSV, plus the
metadata files scan refuses (symlinked, over the size cap, a non-UTF-8 CSV, a FIFO). Every name
and row is made up here from the naming rules in docs/intake.md ("Cloud exports"). stage is
checked to leave the source tree byte-identical, with the same mtimes and no new entries.
"""
import datetime, hashlib, json, os, shutil, subprocess, sys, tempfile, types, unittest
from unittest import mock

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


def sha_tree(root):
    """{rel: sha256} of every file under root."""
    out = {}
    for r, _, fs in os.walk(root):
        for n in fs:
            p = os.path.join(r, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


def snapshot(root):
    """{rel: (kind, sha256 or link target, size, mtime_ns, mode)} of every entry under root, dirs and
    symlinks included (lstat, never followed): any write, touch, chmod or new file changes it."""
    out = {}
    for r, ds, fs in os.walk(root):
        for n in ds + fs:
            p = os.path.join(r, n)
            st = os.lstat(p)
            if os.path.islink(p):
                kind, body = "link", os.readlink(p)
            elif os.path.isdir(p):
                kind, body = "dir", None
            else:
                with open(p, "rb") as f:
                    kind, body = "file", hashlib.sha256(f.read()).hexdigest()
            out[os.path.relpath(p, root)] = (kind, body, st.st_size, st.st_mtime_ns, st.st_mode)
    return out


def counts(**kw):
    """scan()'s counts: zero for every key not given."""
    return {**dict(google=0, icloud=0, places=0, ambiguous=0, unreadable=0), **dict.fromkeys(exports.REFUSED, 0), **kw}


def jpg(path, seed):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (640, 480), (40 * seed % 255, 90, 160)).save(path, quality=90)  # no EXIF date


class Names(unittest.TestCase):
    """The JSON names Takeout gives a media file, from the rules in docs/intake.md ("Cloud exports")."""

    def test_google_names(self):
        cases = {  # newer form: <name>.supplemental-metadata cut to 46 characters, then (N), then .json
            "IMG_0001.jpg": "IMG_0001.jpg.supplemental-metadata.json",
            "IMG_0001(1).jpg": "IMG_0001.jpg.supplemental-metadata(1).json",
            "Scan_garden_shed_before_paint.jpg": "Scan_garden_shed_before_paint.jpg.supplemental.json",
            "Panorama_lake_shore_morning.jpg": "Panorama_lake_shore_morning.jpg.supplemental-m.json",
            "sample_scan_page_0001.png": "sample_scan_page_0001.png.supplemental-metadat.json",
            "VID_0007(12).mp4": "VID_0007.mp4.supplemental-metadata(12).json",
        }
        for media, js in cases.items():
            self.assertEqual(exports.google_names(media)[0], js, media)
        old = {  # the older form: no suffix, the whole name cut to 46 characters (51 with .json)
            "IMG_0002(1).jpg": "IMG_0002.jpg(1).json",
            "an_old_photo_from_the_attic_box_number_seven.jpg": "an_old_photo_from_the_attic_box_number_seven.j.json",
            "a_family_photo_scanned_from_an_album_page_twelve(2).jpg":
                "a_family_photo_scanned_from_an_album_page_twel(2).json",
        }
        for media, js in old.items():
            self.assertIn(js, exports.google_names(media), media)
            self.assertLessEqual(len(js.replace("(1)", "").replace("(2)", "")), 51)
        # A file without a counter never takes a numbered JSON, and the reverse.
        self.assertNotIn("IMG_0003.jpg.json", exports.google_names("IMG_0003(1).jpg"))
        self.assertNotIn("IMG_0003.jpg(1).json", exports.google_names("IMG_0003.jpg"))

    def test_fallbacks(self):
        self.assertIn("IMG_0010.jpg.supplemental-metadata.json", exports.fallback_names("IMG_0010-edited.jpg", []))
        sib = ["IMG_0042(1).HEIC", "IMG_0042(1).MOV", "IMG_0042(2).HEIC"]
        self.assertEqual(exports.fallback_names("IMG_0042(1).MOV", sib)[0], "IMG_0042.HEIC.supplemental-metadata(1).json")
        self.assertEqual(exports.fallback_names("IMG_0042.jpg", ["IMG_0042.HEIC"]), [], "only a video takes the still's")

    def test_icloud_dates(self):
        self.assertEqual(exports.parse_icloud_date("Tuesday March 3,2015 7:45 PM GMT"),
                         datetime.datetime(2015, 3, 3, 19, 45, tzinfo=UTC))
        self.assertEqual(exports.parse_icloud_date("Wednesday January 09,2013 8:02 AM GMT"),
                         datetime.datetime(2013, 1, 9, 8, 2, tzinfo=UTC))
        for bad in ("", "yesterday", "Monday February 30,2015 1:00 PM GMT"):
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
        touch(os.path.join(y, "IMG_0042.HEIC"))
        touch(os.path.join(y, "IMG_0042.MP4"))
        takeout_json(os.path.join(y, "IMG_0042.HEIC.supplemental-metadata.json"), T2019)
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
        self.assertEqual(out[rel("IMG_0042.MP4")]["time"], "2019-05-10 12:00:00Z", "Live Photo video: the still's")
        for n in (a, b, "IMG_0004.jpg", "IMG_0005.jpg"):
            self.assertNotIn(rel(n), out, n)
        self.assertEqual(c, counts(google=8, places=2, ambiguous=2, refused_link=1),
                         "the symlinked sidecar is refused and counted; the non-Takeout JSONs are not")

    def test_icloud_csv(self):
        d = os.path.join(self.root, "iCloud Photos Part 1 of 1", "Photos")
        for n in ("IMG_0501.HEIC", "IMG_0502.jpg", "IMG_0503.jpg", "IMG_0504.jpg"):
            touch(os.path.join(d, n))
        head = "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
        with open(os.path.join(d, "Photo Details.csv"), "w", encoding="utf-8-sig") as f:
            f.write(head
                    + 'IMG_0501.HEIC,sum-a,yes,no,no,"Tuesday March 3,2015 7:45 PM GMT",3,"Friday March 6,2015 9:00 AM GMT"\n'
                    + 'IMG_0502.jpg,sum-b,no,no,no,"Thursday July 2,2015 6:30 AM GMT",0,"Thursday July 2,2015 6:31 AM GMT"\n'
                    + 'IMG_0503.jpg,sum-c,no,no,no,"sometime",0,"Thursday July 2,2015 6:31 AM GMT"\n')
        with open(os.path.join(d, "Photo Details-1.csv"), "w") as f:  # a second CSV contradicts IMG_0502
            f.write(head + 'IMG_0502.jpg,sum-d,no,no,no,"Friday July 3,2015 6:30 AM GMT",0,"later"\n')
        with open(os.path.join(d, "Other.csv"), "w") as f:  # not a Photo Details file
            f.write('imgName,originalCreationDate\nIMG_0504.jpg,"Tuesday March 3,2015 7:45 PM GMT"\n')
        out, c = exports.scan(self.root, self.files())
        r = os.path.join("iCloud Photos Part 1 of 1", "Photos")
        self.assertEqual(out, {os.path.join(r, "IMG_0501.HEIC"): dict(
            time="2015-03-03 19:45:00Z", via="icloud", sidecar=os.path.join(r, "Photo Details.csv"))})
        self.assertEqual(c, counts(icloud=1, ambiguous=1))


    def test_limits_encoding_and_symlinks(self):
        d = os.path.join(self.root, "export")
        touch(os.path.join(d, "IMG_0101.jpg"))
        takeout_json(os.path.join(d, "IMG_0101.jpg.supplemental-metadata.json"), T2019)
        with open(os.path.join(d, "IMG_0101.jpg.supplemental-metadata.json"), "r+") as f:
            doc = json.load(f)
            doc["description"] = "x" * exports.JSON_MAX  # a valid sidecar, but over the size cap
            f.seek(0)
            json.dump(doc, f)
        head = "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
        row = '{},s,no,no,no,"Tuesday March 3,2015 7:45 PM GMT",0,"x"\n'
        big = os.path.join(self.root, "big")
        touch(os.path.join(big, "IMG_0102.jpg"))
        with open(os.path.join(big, "Photo Details.csv"), "w") as f:
            f.write(head + row.format("IMG_0102.jpg") + "IMG_9999.jpg,pad\n" * 50)
        latin = os.path.join(self.root, "latin")
        touch(os.path.join(latin, "IMG_0103.jpg"))
        with open(os.path.join(latin, "Photo Details.csv"), "wb") as f:
            f.write((head + row.format("IMG_0103.jpg") + row.format("caf\xe9.jpg")).encode("latin-1"))
        link = os.path.join(self.root, "link")
        touch(os.path.join(link, "IMG_0104.jpg"))
        real = os.path.join(self.root, "real-details.csv")
        with open(real, "w") as f:
            f.write(head + row.format("IMG_0104.jpg"))
        os.symlink(real, os.path.join(link, "Photo Details.csv"))
        jl = os.path.join(self.root, "jsonlink")
        touch(os.path.join(jl, "IMG_0105.jpg"))
        takeout_json(os.path.join(self.root, "real.json"), T2019)
        os.symlink(os.path.join(self.root, "real.json"), os.path.join(jl, "IMG_0105.jpg.json"))
        with mock.patch.object(exports, "CSV_MAX", 512):
            out, c = exports.scan(self.root, self.files())
        self.assertEqual(out, {}, "an oversized JSON or CSV, a non-UTF-8 CSV and symlinked CSV/JSON are all ignored")
        self.assertEqual(c, counts(refused_size=2, refused_encoding=1, refused_link=2), "each skip is counted by reason")
        with mock.patch.object(exports, "CSV_MAX", 1 << 20):  # the same CSV under the cap is read
            out, c = exports.scan(self.root, self.files())
        self.assertEqual(list(out), [os.path.join("big", "IMG_0102.jpg")])
        self.assertEqual(c["refused_size"], 1, "only the oversized JSON is left")

    def test_refused_counted_once(self):
        """A symlinked JSON two media files fall back to (an original and its -edited copy) counts once."""
        d = os.path.join(self.root, "export")
        touch(os.path.join(d, "IMG_0301.jpg"))
        touch(os.path.join(d, "IMG_0301-edited.jpg"))
        takeout_json(os.path.join(self.root, "real.json"), T2019)
        os.symlink(os.path.join(self.root, "real.json"), os.path.join(d, "IMG_0301.jpg.supplemental-metadata.json"))
        out, c = exports.scan(self.root, self.files())
        self.assertEqual((out, c), ({}, counts(refused_link=1)))

    def test_read_small_refusals(self):
        """_read_small never blocks on a FIFO and never raises on an I/O error mid-read."""
        fifo = os.path.join(self.root, "x.json")
        os.mkfifo(fifo)
        refused = {}
        self.assertIsNone(exports._read_small(fifo, 10, refused))
        self.assertEqual(refused, {fifo: "refused_unreadable"})
        ok = os.path.join(self.root, "ok.json")
        touch(ok, b"{}")
        refused = {}
        with mock.patch.object(exports.os, "fdopen", side_effect=OSError(5, "I/O error")):
            self.assertIsNone(exports._read_small(ok, 10, refused))
        self.assertEqual(refused, {ok: "refused_unreadable"})
        self.assertEqual(exports._read_small(ok, 10), b"{}", "no refused dict: still works")

    def test_unreadable_folder_counted(self):
        d = os.path.join(self.root, "export")
        touch(os.path.join(d, "IMG_0201.jpg"))
        takeout_json(os.path.join(d, "IMG_0201.jpg.supplemental-metadata.json"), T2019)
        files = self.files()  # listed first: os.walk uses scandir too
        with mock.patch.object(exports.os, "scandir", side_effect=PermissionError(13, "denied")):
            out, c = exports.scan(self.root, files)
        self.assertEqual((out, c), ({}, counts(unreadable=1)))


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

        ic = os.path.join(os.path.dirname(os.path.dirname(gp)), "iCloud Photos", "Photos")
        jpg(os.path.join(ic, "IMG_0601.jpg"), 6)
        with open(os.path.join(ic, "Photo Details.csv"), "w") as f:
            f.write("imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
                    'IMG_0601.jpg,s,no,no,no,"Tuesday March 3,2015 7:45 PM GMT",0,"x"\n')
        src = os.path.dirname(os.path.dirname(gp))
        before, before_snap = sha_tree(src), snapshot(src)
        out = self.intake("stage", src, "--kind", "folder", "--batch", "takeout-1")
        self.assertEqual(snapshot(src), before_snap, "stage leaves the source byte-identical, same mtimes, nothing new")
        self.assertIn("staged 5 media files", out)
        self.assertIn("Google Takeout 4, iCloud 1, with a place 1, ambiguous (ignored) 0", out)
        self.assertNotIn("could not be read", out)
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
        ip = plan[os.path.join("iCloud Photos", "Photos", "IMG_0601.jpg")]
        self.assertEqual((ip["date_src"], ip["date"]), ("export", "2015-03-03T19:45:00"))
        self.assertEqual(sha_tree(src), before, "the export's JSON, CSV and media are never written")
        self.assertEqual(snapshot(src), before_snap, "nor touched, nor added to, by plan")

    def test_refused_metadata_reported(self):
        """stage skips a symlinked JSON, an oversized JSON and a non-UTF-8 CSV, says so, stages their
        media without export dates, and leaves the source as it was."""
        src = os.path.join(self.home, "export")
        jpg(os.path.join(src, "big", "IMG_0701.jpg"), 1)
        takeout_json(os.path.join(src, "big", "IMG_0701.jpg.json"), T2019,
                     title="x" * (exports.JSON_MAX + 1))  # over the 1 MiB cap
        jpg(os.path.join(src, "link", "IMG_0702.jpg"), 2)
        outside = os.path.join(self.tmp, "outside.json")
        takeout_json(outside, T2019)
        os.symlink(outside, os.path.join(src, "link", "IMG_0702.jpg.json"))
        jpg(os.path.join(src, "latin", "IMG_0703.jpg"), 3)
        with open(os.path.join(src, "latin", "Photo Details.csv"), "wb") as f:
            f.write(("imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
                     'IMG_0703.jpg,s,no,no,no,"Tuesday March 3,2015 7:45 PM GMT",0,"caf\xe9"\n').encode("latin-1"))
        jpg(os.path.join(src, "good", "IMG_0704.jpg"), 4)
        takeout_json(os.path.join(src, "good", "IMG_0704.jpg.json"), T2020)
        before = snapshot(src)
        out = self.intake("stage", src, "--kind", "folder", "--batch", "refused-1")
        self.assertIn("staged 4 media files", out)
        self.assertIn("Google Takeout 1, iCloud 0", out, "the good sidecar still counts")
        for k in ("refused_link", "refused_size", "refused_encoding"):
            self.assertIn(f"skipped 1 {exports.REFUSED[k]}", out)
        self.assertNotIn(exports.REFUSED["refused_unreadable"], out)
        with open(os.path.join(self.state, "batches", "refused-1", "manifest.json")) as f:
            man = json.load(f)["files"]
        self.assertEqual(sorted(r for r, e in man.items() if "export" in e), [os.path.join("good", "IMG_0704.jpg")])
        self.assertEqual(snapshot(src), before, "the source is untouched")


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



def gps_jpg(path, lat, lon, seed):
    """A JPEG with its own EXIF GPS (north/east only)."""
    from PIL import Image
    ex = Image.Exif()
    ex[0x8769] = {36867: "2019:05:10 12:00:00"}

    def dms(v):
        d = int(v)
        m = int((v - d) * 60)
        return (float(d), float(m), round(((v - d) * 60 - m) * 60, 3))
    ex[0x8825] = {1: "N", 2: dms(lat), 3: "E", 4: dms(lon)}  # set whole: Pillow 10.2 drops get_ifd() writes
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (640, 480), (40 * seed % 255, 60, 120)).save(path, exif=ex, quality=90)


@unittest.skipUnless(shutil.which("exiftool"), "exiftool missing")
class TagPlaceExiftool(unittest.TestCase):
    """tag_files with real exiftool (read_gps, sidecars) and a faked geocoder (test_tagging's real-geocoder
    cases need the venv, which CI doesn't have): the export place lands in the sidecar only without file GPS."""

    def test_sidecars(self):
        support.check_hermetic()
        import intake
        tmp = tempfile.mkdtemp(prefix="exports-tag-real-")
        self.addCleanup(shutil.rmtree, tmp, True)
        plain, own, legacy = (os.path.join(tmp, n) for n in ("plain.jpg", "own.jpg", "legacy.jpg"))
        jpg(plain, 1)
        gps_jpg(own, 10.5, 20.25, 2)
        jpg(legacy, 3)
        before = sha_tree(tmp)
        fakes = dict(guard_label=lambda: types.SimpleNamespace(refused=lambda paths, state: []),
                     venv_json=lambda script, pts, extra=(): [None] * len(pts))
        saved = {k: getattr(intake, k) for k in fakes}
        self.addCleanup(lambda: [setattr(intake, k, v) for k, v in saved.items()])
        for k, v in fakes.items():
            setattr(intake, k, v)
        intake.tag_files([(plain, "Personal", "photo", None, None, [48.8584, 2.2945]),
                          (own, "Personal", "photo", None, None, [1.0, 2.0]),
                          (legacy, "Personal", "photo", None, None)], labels=False)

        def side(p):
            x = intake.sidecar(p)
            self.assertEqual(x, p + ".xmp")
            r = subprocess.run(["exiftool", "-j", "-n", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude", x],
                               capture_output=True, text=True, check=True)
            row = json.loads(r.stdout)[0]
            return row.get("GPSLatitude"), row.get("GPSLongitude")
        lat, lon = side(plain)
        self.assertAlmostEqual(lat, 48.8584, places=4)
        self.assertAlmostEqual(lon, 2.2945, places=4)
        lat, lon = side(own)
        self.assertAlmostEqual(lat, 10.5, places=3, msg="the file's own GPS wins")
        self.assertAlmostEqual(lon, 20.25, places=3)
        self.assertEqual(side(legacy), (None, None), "a 5-tuple: no export place, no GPS")
        after = sha_tree(tmp)
        self.assertEqual({k: v for k, v in after.items() if not k.endswith(".xmp")}, before, "originals untouched")
        self.assertEqual(sorted(k for k in after if k.endswith(".xmp")),
                         ["legacy.jpg.xmp", "own.jpg.xmp", "plain.jpg.xmp"])


if __name__ == "__main__":
    unittest.main()
