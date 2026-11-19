#!/usr/bin/env python3
"""My places (places.py): geometry, names, the private places.json, tagging and its records, suggest's
spots, "probably here", and the privacy edges (share-copy, erase, forget, trash, verify-gone), plus the
gallery's Places API. Synthetic media and made-up points (mid-ocean) only, throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_places.py

The pure parts need nothing; the CLI and gallery cases need exiftool, and the ones that run tag-files (the
city tag beside your place) the venv for geocode.py, on a fixture of GeoNames-format lines.
"""
import json, math, os, shutil, stat, subprocess, sys, tempfile, unittest, zipfile, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
import places  # noqa: E402

CLI = os.path.join(HERE, "cli.py")
INTAKE = os.path.join(HERE, "intake.py")
PY = sys.executable
REAL_VENV = os.path.join(os.path.expanduser("~"), ".local/share/media-intake/venv")
HAVE_VENV = os.path.exists(os.path.join(REAL_VENV, "bin", "python"))
HAVE_EXIFTOOL = bool(shutil.which("exiftool"))
P = (12.5, -40.5)  # a made-up spot in the middle of the Atlantic
NAME = "Zebracove"  # a place name no JPEG byte stream or tool word holds by chance


def north(pt, metres):
    """pt moved metres due north (along a meridian: exact for the haversine)."""
    return pt[0] + math.degrees(metres / places.R_M), pt[1]


def city_line(gid, name, lat, lon, cc, admin1):
    return "\t".join([str(gid), name, name, "", str(lat), str(lon), "P", "PPLA", cc, "", admin1,
                      "", "", "", "5000", "", "10", "Atlantic/Azores", "2026-01-01"])


def write_geo(d):
    os.makedirs(d, exist_ok=True)
    with zipfile.ZipFile(os.path.join(d, "cities1000.zip"), "w") as z:
        z.writestr("cities1000.txt", city_line(1, "Fernvale", 12.52, -40.52, "XA", "EM") + "\n")
    with open(os.path.join(d, "admin1CodesASCII.txt"), "w") as f:
        f.write("XA.EM\tEastmarch\tEastmarch\t1\n")
    with open(os.path.join(d, "countryInfo.txt"), "w") as f:
        f.write("#ISO\tISO3\tISO-Numeric\tfips\tCountry\nXA\tXAA\t999\tXA\tAtlantis\tFernvale\n")


def gps_jpg(path, lat, lon, dt="2026:11:08 19:15:02", seed=1):
    from PIL import Image
    ex = Image.Exif()
    ex[0x8769] = {36867: dt}
    g = ex.get_ifd(0x8825)

    def dms(v):
        v = abs(v)
        d, m = int(v), int((v - int(v)) * 60)
        return (float(d), float(m), round(((v - d) * 60 - m) * 60, 4))
    g[1], g[2] = ("N" if lat >= 0 else "S"), dms(lat)
    g[3], g[4] = ("E" if lon >= 0 else "W"), dms(lon)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (64, 48), (seed * 40 % 255, 90, 160)).save(path, exif=ex, quality=90)


EMPTY = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
         "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n</rdf:RDF>\n</x:xmpmeta>\n"
         "<?xpacket end='w'?>\n")


def sidecar(path, hier, gps=None, date=None):
    """A sidecar written by hand (as tag would): tags, and the GPS and date tag copies there."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        from PIL import Image
        c = zlib.crc32(os.path.basename(path).encode())  # a colour of its own: never another file's copy
        Image.new("RGB", (40, 30), (c & 255, (c >> 8) & 255, (c >> 16) & 255)).save(path)
    with open(path + ".xmp", "w") as f:
        f.write(EMPTY)
    args = []
    for h in hier:
        args += [f"-XMP-lr:HierarchicalSubject+={h}", f"-XMP-dc:Subject+={h.split('|')[-1]}"]
    if gps:
        args += [f"-XMP-exif:GPSLatitude={gps[0]}", f"-XMP-exif:GPSLongitude={gps[1]}"]
    if date:
        args.append(f"-XMP-exif:DateTimeOriginal={date}")
    subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", *args, path + ".xmp"], check=True)


def hier(path):
    if not os.path.exists(path + ".xmp"):
        return []
    r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", "-XMP-dc:Subject", path + ".xmp"],
                       capture_output=True, text=True, check=True)
    row = json.loads(r.stdout)[0]
    v = row.get("HierarchicalSubject")
    return [] if v is None else [str(x) for x in v] if isinstance(v, list) else [str(v)]


def subject(path):
    r = subprocess.run(["exiftool", "-j", "-q", "-XMP-dc:Subject", path + ".xmp"], capture_output=True, text=True)
    v = json.loads(r.stdout)[0].get("Subject")
    return [] if v is None else [str(x) for x in v] if isinstance(v, list) else [str(v)]


# ---------------------------------------------------------------- pure parts
class GeometryTest(unittest.TestCase):
    def test_one_degree_of_latitude(self):
        self.assertAlmostEqual(places.haversine_m(10, 20, 11, 20), 111195.08, delta=0.5)
        self.assertEqual(places.haversine_m(*P, *P), 0.0)

    def test_radius_boundary(self):
        home = dict(lat=P[0], lon=P[1], radius=150)
        self.assertTrue(places.inside(home, *north(P, 149.9)))
        self.assertTrue(places.inside(home, *north(P, 150.0 - 1e-6)))  # the edge itself counts as inside
        self.assertFalse(places.inside(home, *north(P, 150.5)))
        self.assertFalse(places.inside(home, *north(P, -150.5)))

    def test_antimeridian(self):
        home = dict(lat=-20.0, lon=179.9995, radius=150)
        across = (-20.0, -179.9995)  # ~105 m east, on the other side of the date line
        self.assertLess(places.haversine_m(home["lat"], home["lon"], *across), 110)
        self.assertTrue(places.inside(home, *across))
        self.assertFalse(places.inside(home, -20.0, -179.99))
        self.assertTrue(places.inside(dict(lat=0.0, lon=180.0, radius=50), 0.0, -180.0))

    def test_poles(self):
        top = dict(lat=89.9995, lon=0.0, radius=150)
        self.assertTrue(places.inside(top, 89.9995, 180.0))  # ~111 m across the pole
        self.assertTrue(places.inside(top, 90.0, 77.0))  # the pole itself, any longitude
        self.assertFalse(places.inside(top, -89.9995, 0.0))
        self.assertTrue(places.inside(dict(lat=-90.0, lon=0.0, radius=10), -90.0, 123.0))

    def test_points_and_radii_checked(self):
        self.assertEqual(places.parse_point("12.5, -40.5"), P)
        for bad in ("12.5", "91,0", "0,181", "nan,1", "0,0", "a,b", "1,2,3"):
            with self.assertRaises(ValueError, msg=bad):
                places.parse_point(bad)
        self.assertEqual(places.check_radius("150"), 150)
        for bad in (0, 9, 20001, "x", float("inf")):
            with self.assertRaises(ValueError, msg=bad):
                places.check_radius(bad)

    def test_xmp_coordinates(self):
        self.assertAlmostEqual(places.xmp_coord("12,30.0N"), 12.5)
        self.assertAlmostEqual(places.xmp_coord("40,30,0W"), -40.5)
        self.assertAlmostEqual(places.xmp_coord("12,20.7407S"), -12.345678, places=5)
        self.assertEqual(places.xmp_coord("-3.25"), -3.25)
        self.assertIsNone(places.xmp_coord("north"))
        self.assertIsNone(places.gps_of("0,0.0N", "0,0.0E"))  # no fix
        self.assertIsNone(places.gps_of("95,0.0N", "1,0.0E"))
        self.assertEqual(places.gps_of("12,30.0N", "40,30.0W"), P)


class NameTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(places.check_name("  The   cabin "), "The cabin")
        for bad in ("", " ", "a|b", "Fernvale, XA", "line\nbreak", "zero​width", "bidi‮flip",
                    "-dash", "x" * 65, "a@@sep@@b", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                places.check_name(bad)
        self.assertEqual(len(places.check_name("x" * 64)), 64)

    def test_look_alikes_refused(self):
        for alike in ("home", "HOME", "Home ", "ｈｏｍｅ"):
            with self.assertRaises(ValueError, msg=alike):
                places.check_name(alike, ["Home"])
        self.assertEqual(places.check_name("Home 2", ["Home"]), "Home 2")
        with self.assertRaises(ValueError):
            places.check_name("the-cabin", ["The cabin"])

    def test_mine_versus_city(self):
        self.assertTrue(places.is_mine("Place|Home"))
        self.assertFalse(places.is_mine("Place|Fernvale, Eastmarch, XA"))
        self.assertFalse(places.is_mine("Place|"))
        self.assertFalse(places.is_mine("Event|home"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="places-store-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.roots = [os.path.join(self.tmp, "lib")]
        os.makedirs(self.roots[0])
        self.f = os.path.join(self.roots[0], "a.jpg")

    def store(self, roots=None):
        return places.Store(self.state, roots or self.roots)

    def test_private_atomic_per_library(self):
        st = self.store()
        self.assertTrue(st.empty())
        pid = st.add("Home", *P, 150)
        st.save()
        p = os.path.join(self.state, places.FILE)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.state)), [places.FILE], "no temp file left")
        other = [os.path.join(self.tmp, "other-lib")]
        o = self.store(other)
        self.assertEqual(o.places, {}, "another library sees none of this one's places")
        o.add("Home", 1.0, 2.0)
        o.save()
        again = self.store()
        self.assertEqual(again.places[pid]["name"], "Home")
        with open(p) as f:
            raw = f.read()
        self.assertNotIn(self.tmp, raw.split('"tagged"')[0], "the library key is a hash, never a path")
        self.assertEqual(len(json.loads(raw)["libraries"]), 2)

    def test_unreadable_never_overwritten(self):
        os.makedirs(self.state)
        p = os.path.join(self.state, places.FILE)
        with open(p, "w") as f:
            f.write("{not json")
        with self.assertRaises(places.Unreadable):
            self.store()
        self.assertEqual(places.forget_paths(self.state, [self.f])[0], 0)
        with open(p) as f:
            self.assertEqual(f.read(), "{not json")

    def test_hand_edited_bad_entry_kept_but_unused(self):
        os.makedirs(self.state)
        key = places.library_key(self.roots)
        with open(os.path.join(self.state, places.FILE), "w") as f:
            json.dump({"version": 1, "libraries": {key: {"places": {
                "bad": {"name": "a|b", "lat": 1, "lon": 2, "radius": 100},
                "ok": {"name": "Home", "lat": P[0], "lon": P[1], "radius": 100}}}}}, f)
        st = self.store()
        self.assertEqual(list(st.places), ["ok"])
        st.save()
        with open(os.path.join(self.state, places.FILE)) as f:
            self.assertIn("bad", json.load(f)["libraries"][key]["places"])

    def test_plan_tags_untags_and_never_readds(self):
        st = self.store()
        pid = st.add("Home", *P, 100)
        tag = "Place|Home"
        inside, outside = north(P, 50), north(P, 500)
        self.assertEqual(st.plan(self.f, inside, ["Place|Fernvale, Eastmarch, XA"]), ([tag], []))
        self.assertIn(self.f, st.tagged[pid])
        self.assertEqual(st.plan(self.f, inside, [tag]), ([], []), "already there: nothing to do")
        # the place moved away (edited by hand): this tool's tag comes off
        self.assertEqual(st.plan(self.f, outside, [tag]), ([], [tag]))
        self.assertNotIn(self.f, st.tagged[pid])
        # tagged again, then you took it off: never added back, by GPS or anything else
        st.plan(self.f, inside, [])
        self.assertEqual(st.plan(self.f, inside, []), ([], []))
        self.assertIn(self.f, st.declined[pid])
        self.assertEqual(st.plan(self.f, inside, []), ([], []))
        # a tag you put on yourself, outside the circle, is not this tool's: left alone
        g = os.path.join(self.roots[0], "g.jpg")
        self.assertEqual(st.plan(g, outside, [tag]), ([], []))
        # a file with no GPS keeps a tag you confirmed
        h = os.path.join(self.roots[0], "h.jpg")
        st.tagged[pid].add(h)
        self.assertEqual(st.plan(h, None, [tag]), ([], []))
        # a sidecar that couldn't be read (tags None) says nothing about a removal: never declined, never untagged
        self.assertEqual(st.plan(h, inside, None), ([tag], []))
        self.assertEqual(st.plan(h, outside, None), ([], []))
        self.assertIn(h, st.tagged[pid])
        self.assertNotIn(h, st.declined[pid])

    def test_merge_tags_spares_only_named_places(self):
        import intake
        city, old_city = "Place|Fernvale, Eastmarch, XA", "Place|XA"  # a one-field city tag has no comma
        hier, remove, _ = intake.merge_tags([old_city, "Place|Home"], ["Project|Personal", city],
                                            named={"Place|Home"})
        self.assertEqual(remove, [old_city], "the old city goes; your place stays")
        hier, remove, _ = intake.merge_tags(["Place|Home"], ["Project|Personal", city])
        self.assertEqual(remove, ["Place|Home"], "without places nothing changes from before")

    def test_forget_and_stale(self):
        st = self.store()
        pid = st.add("Home", *P)
        live = os.path.join(self.roots[0], "live.jpg")
        open(live, "w").close()
        gone = os.path.join(self.roots[0], "gone.jpg")
        unplugged = os.path.join(self.tmp, "drive", "v.mp4")  # its root is not there: not gone
        st.tagged[pid] |= {live, gone}
        st.declined[pid] |= {unplugged}
        st.save()
        self.assertEqual(st.stale(self.roots + [os.path.dirname(unplugged)]), {gone})
        self.assertEqual(places.stale_count(self.state, self.roots), 1)
        n, err = places.forget_paths(self.state, [gone, unplugged])
        self.assertEqual((n, err), (2, ""))
        again = self.store()
        self.assertEqual(again.tagged[pid], {live})
        self.assertEqual(again.declined[pid], set())
        self.assertEqual(places.stale_count(self.state, self.roots), 0)

    def test_probable(self):
        import datetime
        st = self.store()
        pid = st.add("Home", *P, 100)
        t0 = datetime.datetime(2026, 11, 8, 19, 0)
        src = ("Source|phone camera",)
        others = [(t0 + datetime.timedelta(minutes=12), src, north(P, 20)),
                  (t0, ("Source|gopro",), north(P, 20)),  # another camera: no help
                  (t0 - datetime.timedelta(minutes=5), src, north(P, 5000))]  # same camera, not in a place
        self.assertEqual(st.probable(self.f, t0, src, others), [(pid, 12)])
        late = [(t0 + datetime.timedelta(minutes=places.PROBABLY_MINUTES + 1), src, P)]
        self.assertEqual(st.probable(self.f, t0, src, late), [])
        self.assertEqual(st.probable(self.f, t0, (), others), [], "no source: no guess")
        self.assertEqual(st.probable(self.f, None, src, others), [], "no date: no guess")
        st.declined[pid].add(self.f)
        self.assertEqual(st.probable(self.f, t0, src, others), [], "you said no once")


class SpotsTest(unittest.TestCase):
    def test_dense_spots_found_and_ranked(self):
        pts = []
        for k in range(30):  # 30 files within ~60 m, 20 of them in the evening
            lat, lon = north(P, (k % 6) * 10)
            pts.append((lat, lon + (k // 6) * 0.0001, 20 if k < 20 else 9, "Fernvale"))
        b = (-33.3, 100.1)
        for k in range(12):
            pts.append((b[0] + k * 1e-5, b[1], 13, ""))
        for k in range(5):  # scattered: never a spot
            pts.append((k * 7.0 + 1, 10.0 + k, None, ""))
        got = places.spots(pts)
        self.assertEqual([s["n"] for s in got], [30, 12])
        self.assertEqual(got[0]["evening"], 20)
        self.assertEqual(got[0]["city"], "Fernvale")
        self.assertEqual(got[1]["city"], "")
        self.assertLess(places.haversine_m(got[0]["lat"], got[0]["lon"], *P), 80)
        self.assertEqual(places.spots(pts, min_n=31), [])

    def test_spot_across_the_antimeridian(self):
        pts = [(-20.0, 179.9996, None, "")] * 6 + [(-20.0, -179.9996, None, "")] * 6
        got = places.spots(pts, min_n=10)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["n"], 12)
        self.assertGreater(abs(got[0]["lon"]), 179.99, "the centre is on the date line, not at 0")

    def test_spot_at_a_pole(self):
        got = places.spots([(89.9999, lon, None, "") for lon in range(-180, 180, 30)], min_n=10)
        self.assertEqual([s["n"] for s in got], [12])


# ---------------------------------------------------------------- the CLI on a throwaway library
@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool missing")
class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="places-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        os.chmod(self.state, 0o700)
        if HAVE_VENV:
            os.symlink(REAL_VENV, os.path.join(self.state, "venv"))  # read-only use (geocode.py)
        write_geo(os.path.join(self.state, "geo"))
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"), TZ="UTC",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        MEDIA_INTAKE_GEO_NOPIN="1", MEDIA_INTAKE_NO_LOCALSEARCH="1")
        L = lambda n: os.path.join(self.lib, "2026/2026-11", n)  # noqa: E731
        self.a, self.b, self.far = L("a.jpg"), L("b.jpg"), L("far.jpg")

    def run_cli(self, *args, code=0):
        r = subprocess.run([PY, "-I", CLI, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def no_coords(self, out):
        for v in (P[0], P[1]):
            self.assertNotIn(f"{abs(v):.3f}", out, "a coordinate reached the terminal")
            self.assertNotIn(f"{abs(v):.1f}", out, "a coordinate reached the terminal")

    def stored(self):
        with open(os.path.join(self.state, places.FILE)) as f:
            return json.load(f)

    def make(self):
        sidecar(self.a, ["Project|Personal", "Source|phone camera", "Place|Fernvale, Eastmarch, XA"],
                gps=north(P, 10), date="2026:11:08 19:00:00")
        sidecar(self.b, ["Project|Personal", "Source|phone camera", "Place|Fernvale, Eastmarch, XA"],
                gps=north(P, 90), date="2026:11:08 20:00:00")
        sidecar(self.far, ["Project|Personal", "Place|Fernvale, Eastmarch, XA"], gps=north(P, 900))

    def test_add_tag_rename_remove(self):
        self.make()
        out = self.run_cli("places", "add", NAME, "--from", self.a, "--radius", "150")
        self.assertIn("2 files with GPS inside it", out)
        self.no_coords(out)
        self.assertEqual(self.run_cli("places", "add", NAME.lower(), "--at", "1,2", code=1).count("looks like"), 1)
        self.assertIn("would add 2 place tags", self.run_cli("places", "tag", "--dry-run"))
        self.assertNotIn(f"Place|{NAME}", hier(self.a), "--dry-run writes nothing")
        out = self.run_cli("places", "tag")
        self.no_coords(out)
        for f in (self.a, self.b):
            self.assertEqual(set(hier(f)) & {f"Place|{NAME}", "Place|Fernvale, Eastmarch, XA"},
                             {f"Place|{NAME}", "Place|Fernvale, Eastmarch, XA"}, "the city tag stays beside it")
        self.assertNotIn(f"Place|{NAME}", hier(self.far))
        st = os.path.join(self.state, places.FILE)
        self.assertEqual(stat.S_IMODE(os.stat(st).st_mode), 0o600)
        # list: counts, never the point unless asked
        out = self.run_cli("places", "list")
        self.assertIn(f"{NAME}: radius 150 m, 2 files tagged", out)
        self.no_coords(out)
        self.assertIn(f"{P[0]:.3f}", self.run_cli("places", "list", "--show-coords"))
        # you take it off b: it stays off
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-XMP-lr:HierarchicalSubject-=Place|{NAME}",
                        f"-XMP-dc:Subject-={NAME}", self.b + ".xmp"], check=True)
        self.assertIn("1 you removed stay off", self.run_cli("places", "tag"))
        self.assertNotIn(f"Place|{NAME}", hier(self.b))
        # rename: the tag follows on the files it tagged; b stays without it
        out = self.run_cli("places", "rename", NAME, "Quillmere")
        self.assertIn("1 sidecar rewritten", out)
        self.assertIn("Place|Quillmere", hier(self.a))
        self.assertNotIn(f"Place|{NAME}", hier(self.a))
        self.assertEqual(set(subject(self.a)) & {NAME, "Quillmere"}, {"Quillmere"})
        self.assertFalse([t for t in hier(self.b) if places.is_mine(t)])
        self.run_cli("places", "rename", "Nowhere", "X", code=1)
        # remove: only its tags go; the city stays
        self.assertIn("taken off 1 file", self.run_cli("places", "remove", "Quillmere"))
        for f in (self.a, self.b, self.far):
            self.assertFalse([t for t in hier(f) if places.is_mine(t)], f)
            self.assertIn("Place|Fernvale, Eastmarch, XA", hier(f))
        self.assertEqual(self.stored()["libraries"], {})

    def test_suggest_counts_only_then_add_by_number(self):
        for k in range(12):
            sidecar(os.path.join(self.lib, "2026/2026-10", f"s{k}.jpg"), ["Project|Personal"],
                    gps=north(P, k * 5), date=f"2026:10:0{1 + k % 9} 21:00:00")
        sidecar(self.far, ["Project|Personal"], gps=north(P, 5000))
        out = self.run_cli("places", "suggest")
        self.assertIn("1. 12 files within 150 m, 12 in the evening", out)
        self.assertNotIn("2.", out)
        self.no_coords(out)
        self.assertIn(f"{P[0]:.2f}", self.run_cli("places", "suggest", "--show-coords"))
        out = self.run_cli("places", "add", "Porch", "--suggestion", "1")
        self.assertIn("12 files with GPS inside it", out)
        self.no_coords(out)
        self.run_cli("places", "add", "Other", "--suggestion", "2", code=1)
        self.assertIn("no spot", self.run_cli("places", "suggest"), "a spot inside a place is not suggested again")

    def test_rename_and_remove_refused_while_a_tagged_file_is_unreachable(self):
        self.make()
        self.run_cli("places", "add", NAME, "--at", f"{P[0]},{P[1]}")
        self.run_cli("places", "tag")
        data = self.stored()
        key = next(iter(data["libraries"]))
        pid = next(iter(data["libraries"][key]["places"]))
        away = os.path.join(self.env["PERSONAL_FOOTAGE"], "2026/v.mp4")  # its folder (a drive) is not there
        data["libraries"][key]["tagged"][pid].append(away)
        places.write_private(os.path.join(self.state, places.FILE), json.dumps(data).encode())
        for args in (("rename", NAME, "Quillmere"), ("remove", NAME)):
            self.assertIn("can't be read now", self.run_cli("places", *args, code=1))
            self.assertIn(f"Place|{NAME}", hier(self.a), "nothing written on a refusal")
        self.assertIn(f"{NAME}: radius 150 m, 3 files tagged", self.run_cli("places", "list"))
        self.assertIn("2 sidecars rewritten", self.run_cli("places", "rename", NAME, "Quillmere", "--anyway"))
        self.assertIn("Place|Quillmere", hier(self.a))

    def test_from_refuses_outside_the_library_and_no_gps(self):
        outside = os.path.join(self.home, "Downloads", "x.jpg")
        gps_jpg(outside, *P)
        self.assertIn("in the library", self.run_cli("places", "add", "Home", "--from", outside, code=1))
        sidecar(self.a, ["Project|Personal"])
        self.assertIn("no GPS", self.run_cli("places", "add", "Home", "--from", self.a, code=1))
        self.assertFalse(os.path.exists(os.path.join(self.state, places.FILE)), "nothing saved on a refusal")

    def test_unreadable_file_refused_not_overwritten(self):
        with open(os.path.join(self.state, places.FILE), "w") as f:
            f.write("[]")
        self.assertIn("unreadable", self.run_cli("places", "add", "Home", "--at", "12.5,-40.5", code=1))
        with open(os.path.join(self.state, places.FILE)) as f:
            self.assertEqual(f.read(), "[]")

    @unittest.skipUnless(HAVE_VENV, "venv not set up (geocode.py)")
    def test_tag_files_adds_keeps_and_never_readds(self):
        """New imports get their place through tag / tag-files; a re-tag keeps it beside the city and never puts
        back one you removed; the city fact is still replaced as before."""
        gps_jpg(self.a, *north(P, 10), seed=1)
        gps_jpg(self.b, *north(P, 60), seed=2)
        gps_jpg(self.far, *north(P, 5000), seed=3)
        self.run_cli("places", "add", NAME, "--at", f"{P[0]},{P[1]}")
        out = self.run_cli("tag-files", self.a, self.b, self.far, "--no-labels")
        self.assertIn("your place tags added 2", out)
        self.no_coords(out)
        self.assertNotIn(NAME, out)
        city = "Place|Fernvale, Eastmarch, XA"
        self.assertEqual({t for t in hier(self.a) if t.startswith("Place|")}, {city, f"Place|{NAME}"})
        self.assertEqual({t for t in hier(self.far) if t.startswith("Place|")}, {city})
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-XMP-lr:HierarchicalSubject-=Place|{NAME}",
                        self.b + ".xmp"], check=True)
        out = self.run_cli("tag-files", self.a, self.b, self.far, "--no-labels")
        self.assertNotIn("your place tags added", out)
        self.assertEqual({t for t in hier(self.a) if t.startswith("Place|")}, {city, f"Place|{NAME}"})
        self.assertEqual({t for t in hier(self.b) if t.startswith("Place|")}, {city}, "your removal stays")
        self.assertEqual(len(hier(self.a)), len(set(hier(self.a))))

    # ------------------------------------------------------------ privacy edges
    def test_share_copy_carries_no_place(self):
        gps_jpg(self.a, *north(P, 10))
        sidecar(self.a, ["Project|Personal", f"Place|{NAME}", "Place|Fernvale, Eastmarch, XA"], gps=north(P, 10))
        out = os.path.join(self.home, "Pictures/share-safe/one")
        r = subprocess.run([PY, "-I", INTAKE, "share-copy", self.a, "--out", out], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gps=none", r.stdout)
        self.assertEqual(os.listdir(out), ["a.jpg"], "the sidecar (and its Place tags) is never copied")
        with open(os.path.join(out, "a.jpg"), "rb") as f:
            data = f.read()
        for needle in (NAME.encode(), b"Fernvale", b"Place|"):
            self.assertNotIn(needle, data)
        g = subprocess.run(["exiftool", "-j", "-n", "-GPS:all", "-XMP:all", os.path.join(out, "a.jpg")],
                           capture_output=True, text=True)
        row = json.loads(g.stdout)[0]
        self.assertFalse([k for k in row if "GPS" in k or k in ("HierarchicalSubject", "Subject", "City")], row)

    def test_forget_and_trash_drop_records_and_verify_gone_checks(self):
        self.make()
        self.run_cli("places", "add", NAME, "--at", f"{P[0]},{P[1]}")
        self.run_cli("places", "tag")
        key = next(iter(self.stored()["libraries"]))
        pid = next(iter(self.stored()["libraries"][key]["places"]))
        self.assertEqual(sorted(self.stored()["libraries"][key]["tagged"][pid]), sorted([self.a, self.b]))
        r = subprocess.run([PY, "-I", INTAKE, "forget", self.a, "--yes"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.a + ".xmp"), "the place tag went with the sidecar")
        self.assertEqual(self.stored()["libraries"][key]["tagged"][pid], [self.b])
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            cov = json.loads([ln for ln in f if " coverage " in ln][-1].split(" coverage ", 1)[1])
        self.assertEqual(cov["reached"]["your places' records: erased files dropped (state folder, places.json)"], 1)
        self.assertNotIn(os.path.basename(self.a), json.dumps(cov))
        r = subprocess.run([PY, "-I", INTAKE, "trash", self.b, "--yes"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.stored()["libraries"][key].get("tagged", {}).get(pid, []), [],
                         "trash drops its records too")
        self.assertIn("no copy of 1 erased items", self.run_cli("verify-gone"))
        # a record of a file deleted by hand: verify-gone says so and fails; places tag drops it
        self.run_cli("places", "add", "Second", "--at", f"{P[0]},{P[1] + 0.01}")
        data = self.stored()
        data["libraries"][key].setdefault("declined", {})[pid] = [os.path.join(self.lib, "2026/gone-by-hand.jpg")]
        places.write_private(os.path.join(self.state, places.FILE), json.dumps(data).encode())
        out = self.run_cli("verify-gone", code=1)
        self.assertIn("PLACE RECORDS: 1", out)
        self.assertNotIn("gone-by-hand", out)
        self.assertIn("1 record of files no longer in the library dropped", self.run_cli("places", "tag"))
        self.run_cli("verify-gone")


# ---------------------------------------------------------------- the gallery's Places API
@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool missing")
class GalleryPlacesTest(unittest.TestCase):
    def setUp(self):
        import test_gallery as G
        self.G = G
        self.tmp = tempfile.mkdtemp(prefix="places-gallery-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        L = lambda n: os.path.join(self.lib, "2026/2026-11", n)  # noqa: E731
        self.a, self.b, self.far, self.nogps, self.gopro = L("a.jpg"), L("b.jpg"), L("far.jpg"), L("n.jpg"), L("g.jpg")
        cam = ["Project|Personal", "Source|phone camera"]
        sidecar(self.a, cam + ["Place|Fernvale, Eastmarch, XA"], gps=north(P, 10), date="2026:11:08 19:00:00")
        sidecar(self.b, cam, gps=north(P, 120), date="2026:11:08 19:30:00")
        sidecar(self.far, cam, gps=north(P, 3000), date="2026:11:08 12:00:00")
        sidecar(self.nogps, cam, date="2026:11:08 19:12:00")  # same camera, 12 min after a: probably there
        sidecar(self.gopro, ["Project|Personal", "Source|gopro"], date="2026:11:08 19:05:00")  # another camera
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def start(self):
        s = self.G.Server(self.env)
        self.servers.append(s)
        return s

    def ids(self, s):
        return {i["n"]: i["id"] for i in s.get("/api/list?limit=500")["items"]}

    def test_add_place_from_an_item_then_filter(self):
        s = self.start()
        ids = self.ids(s)
        f = s.get("/api/facets")
        self.assertEqual(f["places"], [])
        self.assertIn(150, f["radii"])
        d = s.get(f"/api/item/{ids['a.jpg']}")
        self.assertTrue(d["gps"])
        self.assertEqual(d["place"], "Fernvale, Eastmarch, XA")
        r, raw = s.req("GET", f"/api/item/{ids['a.jpg']}")
        for v in (P[0], P[1]):
            self.assertNotIn(f"{abs(v)}".encode(), raw, "the page never gets coordinates")
        # refusals: no GPS, a bad name, a bad radius
        for body in ({"id": ids["n.jpg"], "name": "Home"}, {"id": ids["a.jpg"], "name": "a,b"},
                     {"id": ids["a.jpg"], "name": "Home", "radius": 5}, {"id": "x", "name": "Home"}):
            code, j = s.post("/api/place/add", body)
            self.assertEqual(code, 400, (body, j))
        self.assertFalse(os.path.exists(os.path.join(self.state, places.FILE)))
        code, j = s.post("/api/place/add", {"id": ids["a.jpg"], "name": NAME, "radius": 150})
        self.assertEqual(code, 200, j)
        self.assertIn("2 items within 150 m tagged", j["result"])
        self.assertEqual({i["n"] for i in j["items"]}, {"a.jpg", "b.jpg"})
        self.assertIn(f"Place|{NAME}", hier(self.a))
        self.assertIn("Place|Fernvale, Eastmarch, XA", hier(self.a), "the city tag stays")
        self.assertIn(f"Place|{NAME}", hier(self.b))
        self.assertNotIn(f"Place|{NAME}", hier(self.far))
        st = os.path.join(self.state, places.FILE)
        self.assertEqual(stat.S_IMODE(os.stat(st).st_mode), 0o600)
        f = s.get("/api/facets")
        self.assertEqual([(p["name"], p["n"]) for p in f["places"]], [(NAME, 2)])
        pid = f["places"][0]["id"]
        self.assertEqual(sorted(i["n"] for i in s.get(f"/api/list?myplace={pid}")["items"]), ["a.jpg", "b.jpg"])
        self.assertEqual(s.get("/api/list?myplace=nope")["total"], 0)
        d = s.get(f"/api/item/{ids['a.jpg']}")
        self.assertEqual((d["myplaces"], d["place"]), ([NAME], "Fernvale, Eastmarch, XA"))
        code, j = s.post("/api/place/add", {"id": ids["far.jpg"], "name": NAME.upper()})
        self.assertEqual(code, 400, "a look-alike name is refused")

    def test_probably_here_confirm_and_reject(self):
        s = self.start()
        ids = self.ids(s)
        code, j = s.post("/api/place/add", {"id": ids["a.jpg"], "name": NAME, "radius": 100})
        self.assertEqual(code, 200, j)
        pid = s.get("/api/facets")["places"][0]["id"]
        d = s.get(f"/api/item/{ids['n.jpg']}")
        self.assertEqual(d["probably"], [dict(id=pid, name=NAME, minutes=12)])
        self.assertEqual(hier(self.nogps), ["Project|Personal", "Source|phone camera"], "a suggestion writes nothing")
        self.assertEqual(s.get(f"/api/item/{ids['g.jpg']}")["probably"], [], "another camera: no guess")
        self.assertEqual(s.get(f"/api/item/{ids['far.jpg']}")["probably"], [], "a file with GPS is never guessed")
        code, j = s.post("/api/place/probable", {"id": ids["n.jpg"], "place": pid, "yes": "yes"})
        self.assertEqual(code, 400)
        code, j = s.post("/api/place/probable", {"id": ids["g.jpg"], "place": pid, "yes": True})
        self.assertEqual(code, 400, "only what is suggested can be confirmed")
        code, j = s.post("/api/place/probable", {"id": ids["n.jpg"], "place": pid, "yes": True})
        self.assertEqual(code, 200, j)
        self.assertIn(f"Place|{NAME}", hier(self.nogps))
        self.assertEqual(s.get(f"/api/item/{ids['n.jpg']}")["probably"], [])
        self.assertEqual(s.get("/api/facets")["places"][0]["n"], 2)
        # a "no" is remembered: never suggested again, nothing written
        sidecar(self.gopro, ["Project|Personal", "Source|phone camera"], date="2026:11:08 19:20:00")
        s2 = self.start()
        ids2 = self.ids(s2)
        self.assertEqual(s2.get(f"/api/item/{ids2['g.jpg']}")["probably"][0]["id"], pid)
        code, j = s2.post("/api/place/probable", {"id": ids2["g.jpg"], "place": pid, "yes": False})
        self.assertEqual(code, 200, j)
        self.assertEqual(s2.get(f"/api/item/{ids2['g.jpg']}")["probably"], [])
        self.assertNotIn(f"Place|{NAME}", hier(self.gopro))
        with open(os.path.join(self.state, places.FILE)) as f:
            rec = next(iter(json.load(f)["libraries"].values()))
        self.assertEqual(rec["declined"][pid], [self.gopro])
        self.assertIn(self.nogps, rec["tagged"][pid])

    def test_unreadable_places_file(self):
        with open(os.path.join(self.state, places.FILE), "w") as f:
            f.write("{oops")
        s = self.start()
        f = s.get("/api/facets")
        self.assertEqual(f["places"], [])
        self.assertIn("unreadable", f["places_error"])
        code, j = s.post("/api/place/add", {"id": self.ids(s)["a.jpg"], "name": "Home"})
        self.assertEqual(code, 409, j)
        with open(os.path.join(self.state, places.FILE)) as f:
            self.assertEqual(f.read(), "{oops")


if __name__ == "__main__":
    unittest.main(verbosity=2)
