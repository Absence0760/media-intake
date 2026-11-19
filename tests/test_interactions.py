#!/usr/bin/env python3
"""Where the features meet: names (People| / Pets|), My places (Place|), dates (fix-dates, Dates to check) and
favorites on one file, and the share copy.

  python3 -I -m unittest discover -s tests -p test_interactions.py

Synthetic media in a throwaway HOME, made-up names and mid-ocean points (tests/test_places.py's helpers), the
real intake.py and gallery as subprocesses. A recognition store, where one is needed, is built in-process with
tests/test_recog.py's mock detector and embedder (no model). Covered: a share copy (CLI and the gallery's
share with --share-out) of a file whose sidecar holds names and a place has no name byte and no GPS and no
sidecar; fix-dates and --undo on a named, placed favorite keep the original's sha256, every tag and the rating;
a fixed file that is flagged later is refused by a recognition scan; the gallery's Fix date leaves the tags and
the favorite; "probably here" Yes and No change only Place|.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest, urllib.parse
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

support.check_hermetic()
import test_gallery as G  # noqa: E402  (the server wrapper)
import test_places as TP  # noqa: E402  (gps_jpg, sidecar, hier, NAME, P, north)
import test_recog as TR  # noqa: E402  (mock detector / embedder, synthetic scenes)
import recog  # noqa: E402

HAVE = bool(shutil.which("exiftool"))
PERSON, PET = "Zebrann", "Quokkarex"  # names no JPEG byte stream holds by chance
NAMES = [f"People|{PERSON}", f"Pets|{PET}", f"Place|{TP.NAME}"]


def rating(p):
    r = subprocess.run(["exiftool", "-j", "-q", "-n", "-XMP-xmp:Rating", "-XMP-exif:DateTimeOriginal", p + ".xmp"],
                       capture_output=True, text=True, check=True)
    row = json.loads(r.stdout)[0]
    return row.get("Rating"), str(row.get("DateTimeOriginal", ""))


def star(p):
    subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", "-XMP-xmp:Rating=5", p + ".xmp"], check=True)


@unittest.skipUnless(HAVE and TR.np, "exiftool, numpy or Pillow missing")
class InteractionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="interactions-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state, mode=0o700)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"), TZ="UTC",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def cli(self, *args, code=0):
        r = subprocess.run([TP.PY, "-I", TP.INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def start(self, *extra):
        s = G.Server(self.env, *extra)
        self.servers.append(s)
        return s

    def ids(self, s, **f):
        return {i["n"]: i["id"] for i in s.get("/api/list?" + urllib.parse.urlencode(dict(f, limit=500)))["items"]}

    def named_photo(self, rel, date="2016:01:01 00:01:00"):
        """A PNG ann is in (detected, in a recognition store) with names, a place you named (in places.json)
        and a favorite's rating in its sidecar."""
        p = TR.image(os.path.join(self.lib, rel), 1)
        TP.sidecar(p, ["Project|Personal", *NAMES], date=date)
        star(p)
        world = TR.World()
        world.scene(1, ("ann", None))
        c = recog.connect()
        recog.scan(c, TR.kind(world), [p], log=lambda *a, **k: None, state=self.state)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM detections").fetchone()[0], 1)
        c.close()
        add_place = ("import sys; sys.path.insert(0, sys.argv[1]); import intake, places; "
                     "st = places.Store(intake.STATE, intake.LIB_ROOTS); pid = st.add(sys.argv[2], 1.0, 2.0); "
                     "st.tagged[pid].add(sys.argv[3]); st.save()")
        r = subprocess.run([TP.PY, "-I", "-c", add_place, support.PKG, TP.NAME, p], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return p

    def assert_kept(self, p, sha0):
        self.assertEqual(TR.sha_of(p), sha0, "the original is never written")
        self.assertEqual(sorted(t for t in TP.hier(p) if t.split("|")[0] in ("People", "Pets", "Place")),
                         sorted(NAMES))
        self.assertEqual(rating(p)[0], 5, "still a favorite")
        c = recog.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM detections WHERE sha=?", (sha0,)).fetchone()[0], 1)
        c.close()

    # ------------------------------------------------------------ 5. share copies
    def check_copy(self, out, name):
        self.assertEqual(os.listdir(out), [name], "the sidecar (and its names and places) is never copied")
        with open(os.path.join(out, name), "rb") as f:
            data = f.read()
        for needle in (PERSON.encode(), PET.encode(), TP.NAME.encode(), b"People|", b"Pets|", b"Place|"):
            self.assertNotIn(needle, data)
        g = subprocess.run(["exiftool", "-j", "-n", "-GPS:all", "-XMP:all", os.path.join(out, name)],
                           capture_output=True, text=True)
        row = json.loads(g.stdout)[0]
        self.assertFalse([k for k in row if "GPS" in k or k in ("HierarchicalSubject", "Subject")], row)

    def test_share_copy_has_no_names_no_place_no_gps(self):
        a = os.path.join(self.lib, "2026/2026-11/a.jpg")
        TP.gps_jpg(a, *TP.north(TP.P, 10))
        TP.sidecar(a, ["Project|Personal", *NAMES], gps=TP.north(TP.P, 10))
        out = os.path.join(self.home, "Pictures/share-safe/one")
        self.assertIn("gps=none", self.cli("share-copy", a, "--out", out))
        self.check_copy(out, "a.jpg")
        root = os.path.join(self.home, "Exports/safe")
        s = self.start("--share-out", root)
        code, j = s.post("/api/share", {"ids": [self.ids(s)["a.jpg"]]})
        self.assertEqual(code, 200, j)
        job = G.until(lambda: (lambda x: x if x["done"] else None)(s.get("/api/job")), "the share job")
        self.assertTrue(job["ok"], job)
        self.check_copy(os.path.join(self.home, j["dest"][2:]), "a.jpg")

    # ------------------------------------------------------------ 6. fix-dates on a named, placed favorite
    def test_fix_dates_and_undo_keep_names_place_and_favorite(self):
        p = self.named_photo("2016/2016-01/IMG_0001.PNG")
        sha0 = TR.sha_of(p)
        self.cli("fix-dates", "--set", "2024-05-12T10:00", p, "--yes")
        self.assertTrue(rating(p)[1].startswith("2024:05:12 10:00"), rating(p))
        self.assert_kept(p, sha0)
        self.cli("fix-dates", "--undo", "--yes")
        self.assertTrue(rating(p)[1].startswith("2016:01:01 00:01"), rating(p))
        self.assert_kept(p, sha0)

    def test_a_fixed_file_flagged_later_is_refused_by_a_scan(self):
        p = self.named_photo("2016/2016-01/IMG_0002.PNG")
        self.cli("fix-dates", "--set", "2024-05-12T10:00", p, "--yes")
        b = os.path.join(self.state, "batches", "2026-11-19-x")  # scan flagged it in a later batch
        os.makedirs(b)
        with open(os.path.join(b, "plan.json"), "w") as f:
            json.dump([dict(rel="DCIM/x.png", sha=TR.sha_of(p), size=os.path.getsize(p))], f)
        with open(os.path.join(b, "scan.json"), "w") as f:
            json.dump({"DCIM/x.png": {"reasons": ["nudity"]}}, f)
        world = TR.World()
        world.scene(1, ("ann", None))
        c = recog.connect()
        self.addCleanup(c.close)
        # as every people / pets command does: prune first (its rows go), then scan (it is refused)
        self.assertEqual(recog.prune(c, state=self.state)["flagged, erased, private or trashed"], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM detections WHERE sha=?", (TR.sha_of(p),)).fetchone()[0], 0,
                         "its earlier rows are dropped, never kept for a flagged file")
        cnt = recog.scan(c, TR.kind(world), [p], log=lambda *a, **k: None, state=self.state)
        self.assertEqual(cnt["refused (quarantine, flagged, erased)"], 1, cnt)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM detections").fetchone()[0], 0)

    # ------------------------------------------------------------ 7. Dates to check: Fix date
    def test_gallery_fix_date_keeps_tags_and_favorite(self):
        p = self.named_photo("2016/2016-01/IMG_0003.PNG")
        sha0 = TR.sha_of(p)
        with open(os.path.join(self.state, "datecheck.json"), "w") as f:
            json.dump(dict(version=1, id="r1", checked="2026-11-19T10:00:00", files=1, findings=[
                dict(path=p, kind="reset", confidence="high", reason="2016-01-01 is a clock-reset default",
                     date="2016-01-01T00:01:00", date_src="sidecar", suggest="2024-05-12T10:00:00",
                     suggest_src="gps")]), f)
        s = self.start()
        ids = self.ids(s, dates="all")
        self.assertEqual(list(ids), ["IMG_0003.PNG"])
        code, j = s.post("/api/dates/fix", {"ids": [ids["IMG_0003.PNG"]], "mode": "suggested"})
        self.assertEqual(code, 200, j)
        self.assertTrue(rating(p)[1].startswith("2024:05:12 10:00"), rating(p))
        self.assert_kept(p, sha0)
        self.assertEqual(self.ids(s, dates="all"), {}, "fixed: it leaves the view")
        item = s.get(f"/api/item/{ids['IMG_0003.PNG']}")
        self.assertTrue(item["fav"])
        self.assertEqual([(w["kind"], w["name"]) for w in item["who"]], [("face", PERSON), ("pet", PET)])

    # ------------------------------------------------------------ 8. "probably here" on a named file
    def test_probably_here_changes_only_place(self):
        L = lambda n: os.path.join(self.lib, "2026/2026-11", n)  # noqa: E731
        a, yes, no = L("a.jpg"), L("n.jpg"), L("m.jpg")
        cam = ["Project|Personal", "Source|phone camera"]
        TP.sidecar(a, cam, gps=TP.north(TP.P, 10), date="2026:11:08 19:00:00")
        TP.sidecar(yes, cam + [f"People|{PERSON}"], date="2026:11:08 19:12:00")
        TP.sidecar(no, cam + [f"People|{PERSON}", f"Pets|{PET}"], date="2026:11:08 19:20:00")
        star(no)
        s = self.start()
        ids = self.ids(s)
        code, j = s.post("/api/place/add", {"id": ids["a.jpg"], "name": TP.NAME, "radius": 100})
        self.assertEqual(code, 200, j)
        pid = s.get("/api/facets")["places"][0]["id"]
        before = {p: TP.hier(p) for p in (yes, no)}
        code, j = s.post("/api/place/probable", {"id": ids["n.jpg"], "place": pid, "yes": True})
        self.assertEqual(code, 200, j)
        self.assertEqual(TP.hier(yes), before[yes] + [f"Place|{TP.NAME}"], "only the place tag is added")
        code, j = s.post("/api/place/probable", {"id": ids["m.jpg"], "place": pid, "yes": False})
        self.assertEqual(code, 200, j)
        self.assertEqual(TP.hier(no), before[no], "a No writes nothing")
        self.assertEqual(rating(no)[0], 5)


if __name__ == "__main__":
    unittest.main()
