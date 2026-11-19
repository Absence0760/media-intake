#!/usr/bin/env python3
"""The gallery's Dates to check view and Fix date (gallery.py over datecheck.py) on synthetic media.

  python3 -I -m unittest discover -s tests -p test_gallery_dates.py

A throwaway HOME holds a GoPro's numbered shots (one on the clock-reset default), a phone photo dated before the
phone was sold and a plain photo. The real `intake.py check-dates` writes the report; a real gallery server then
lists the flagged files with their suggested dates (/api/list?dates=...), counts them in the sidebar
(/api/facets), fixes them through fix-dates' own code (/api/dates/fix: sidecars only, the originals' bytes
unchanged) and undoes a fix (/api/undo), refusing once a sidecar changed again. Needs exiftool.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import test_gallery as G  # noqa: E402  (the server wrapper)
from test_datecheck import jpg, set_side, sha, side_date  # noqa: E402

HAVE = bool(shutil.which("exiftool"))


def dates_library(lib, extra=()):
    """A GoPro's four numbered shots (the third on the clock-reset default, its neighbours' bracket 10:20), a phone
    photo dated before the phone was sold, a plain photo, and an era photo per name in extra. Returns their paths."""
    L = lambda *p: os.path.join(lib, *p)  # noqa: E731
    hero = ["-Make=GoPro", "-Model=HERO12 Black"]
    shots = [L("2024/2024-05/gopro-hero12-black", f"GOPR{n:04d}.JPG") for n in range(1, 5)]
    for i, (p, w) in enumerate(zip(shots, ("2024:05:12 10:00:00", "2024:05:12 10:10:00", "2016:01:01 00:01:00",
                                           "2024:05:12 10:30:00"))):
        jpg(p, i + 1, w, *hero)
    out = dict(shots=shots, reset=shots[2], era=L("2019/2019-06/DSC_0001.JPG"), plain=L("2024/2024-06/plain.jpg"))
    jpg(out["era"], 9, "2019:06:01 12:00:00", "-Make=Google", "-Model=Pixel 8")
    jpg(out["plain"], 10, "2024:06:01 12:00:00")
    for k, n in enumerate(extra):
        out[n] = L("2019/2019-07", n)
        jpg(out[n], 20 + k, "2019:07:01 12:00:00", "-Make=Google", "-Model=Pixel 8")
    return out


def run_check(case, env):
    """The real `intake.py check-dates` over the throwaway library: it writes the report the gallery reads."""
    r = subprocess.run([G.PY, "-I", G.INTAKE, "check-dates"], env=env, capture_output=True, text=True)
    case.assertEqual(r.returncode, 0, r.stdout + r.stderr)


@unittest.skipUnless(HAVE, "exiftool is not installed")
class GalleryDatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-dates-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state, mode=0o700)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"), TZ="UTC",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.path = dates_library(self.lib)
        self.shots, self.reset, self.era, self.plain = (self.path["shots"], self.path["reset"], self.path["era"],
                                                        self.path["plain"])
        self.originals = {p: sha(p) for p in [*self.shots, self.era, self.plain]}
        run_check(self, self.env)
        self.s = G.Server(self.env)
        self.addCleanup(self.s.stop)

    def listing(self, **f):
        return self.s.get("/api/list?" + urllib.parse.urlencode(dict(f, limit=500)))

    def ids(self):
        return {i["n"]: i["id"] for i in self.listing()["items"]}

    def unchanged(self):
        for p, h in self.originals.items():
            self.assertEqual(sha(p), h, f"{p} was written")

    def test_view_counts_groups_and_suggestions(self):
        page = self.s.req("GET", "/")[1].decode()
        self.assertIn('data-v="dates"', page)
        self.assertIn('id="bfixdate"', page)
        self.assertEqual(self.s.get("/api/facets")["counts"]["dates"], 2)
        j = self.listing(dates="all")
        self.assertEqual([i["n"] for i in j["items"]], ["GOPR0003.JPG", "DSC_0001.JPG"])  # reset first, then era
        a, b = j["items"]
        self.assertEqual((a["dk"], a["dq"], a["ds"], a["dsrc"]),
                         ("reset", "Clock-reset default date", "2024-05-12 10:20", "neighbours"))
        self.assertIn("GoPro", a["dr"])
        self.assertEqual((b["dk"], b["ds"]), ("era", None))
        self.assertTrue(j["ranked"], "no month headings or burst stacks: grouped by reason")
        self.assertIn("From check-dates on", j["note"])
        self.assertEqual([i["n"] for i in self.listing(dates="era")["items"]], ["GOPR0003.JPG", "DSC_0001.JPG"])
        self.assertEqual([i["n"] for i in self.listing(dates="reset")["items"]], ["GOPR0003.JPG"])
        self.assertNotIn("dq", next(i for i in self.listing()["items"] if i["n"] == "plain.jpg"))

    def test_fix_suggested_then_undo(self):
        rid = self.ids()["GOPR0003.JPG"]
        st, j = self.s.post("/api/dates/fix", {"ids": [rid], "mode": "suggested"})
        self.assertEqual(st, 200, j)
        self.assertIn("Fixed the date of 1 file", j["result"])
        self.assertIn("undo", j)
        self.assertEqual(side_date(self.reset), "2024:05:12 10:20:00")
        self.unchanged()
        self.assertEqual(self.s.get("/api/facets")["counts"]["dates"], 1)
        self.assertEqual([i["n"] for i in self.listing(dates="all")["items"]], ["DSC_0001.JPG"])
        tile = next(i for i in j["items"] if i["id"] == rid)
        self.assertEqual(tile["d"], "2024-05-12 10:20", "the tile shows the fixed date")
        st, u = self.s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(st, 200, u)
        self.assertEqual(u["kind"], "date")
        self.assertIsNone(side_date(self.reset))
        self.assertEqual(self.s.get("/api/facets")["counts"]["dates"], 2)
        st, u = self.s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(st, 400)  # one use per token
        self.unchanged()

    def test_set_shift_and_refusals(self):
        ids = self.ids()
        st, j = self.s.post("/api/dates/fix", {"ids": [ids["DSC_0001.JPG"]], "mode": "set", "value": "2024-01-02 08:00"})
        self.assertEqual(st, 200, j)
        self.assertEqual(side_date(self.era), "2024:01:02 08:00:00")
        st, j = self.s.post("/api/dates/fix", {"ids": [ids["DSC_0001.JPG"]], "mode": "shift", "value": "+1d"})
        self.assertEqual(st, 200, j)
        self.assertEqual(side_date(self.era), "2024:01:03 08:00:00")
        for body, words in (({"mode": "set", "value": "next tuesday"}, "Date wants YYYY-MM-DD"),
                            ({"mode": "shift", "value": "8y"}, "Shift wants a sign"),
                            ({"mode": "erase"}, "set, shift or suggested"),
                            ({"mode": "set", "value": "1999-01-01"}, "impossible"),
                            ({"mode": "suggested"}, "nothing was changed")):
            st, j = self.s.post("/api/dates/fix", dict(body, ids=[ids["plain.jpg"]]))
            self.assertEqual(st, 400, (body, j))
            self.assertIn(words, j["error"])
        self.assertFalse(os.path.exists(self.plain + ".xmp"), "a refused fix writes no sidecar")
        st, j = self.s.post("/api/dates/fix", {"ids": [999], "mode": "suggested"})
        self.assertEqual(st, 400)
        self.unchanged()

    def test_undo_refused_after_a_later_change_and_shared_with_the_cli(self):
        rid = self.ids()["GOPR0003.JPG"]
        st, j = self.s.post("/api/dates/fix", {"ids": [rid], "mode": "set", "value": "2024-05-12 10:20"})
        self.assertEqual(st, 200, j)
        set_side(self.reset, "-XMP-lr:HierarchicalSubject+=Trips|Spain")
        st, u = self.s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(st, 409, u)
        self.assertIn("changed since", u["error"])
        self.assertEqual(side_date(self.reset), "2024:05:12 10:20:00")
        # a gallery fix is in fix-dates' record: the CLI lists it as the last one
        st, j = self.s.post("/api/dates/fix", {"ids": [self.ids()["DSC_0001.JPG"]], "mode": "set", "value": "2024-01-02"})
        self.assertEqual(st, 200, j)
        r = subprocess.run([G.PY, "-I", G.INTAKE, "fix-dates", "--undo", "--yes"], env=self.env, capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(side_date(self.era))
        with open(os.path.join(self.state, "date-fixes.json")) as f:
            self.assertEqual(json.load(f)["fixes"][-1]["reason"], "gallery Fix date")

    def test_fix_needs_the_token_and_the_csrf_value(self):
        rid = self.ids()["GOPR0003.JPG"]
        body = {"ids": [rid], "mode": "suggested"}
        r, _ = self.s.req("POST", "/api/dates/fix", token=False, headers={"X-CSRF": self.s.csrf()}, body=body)
        self.assertIn(r.status, (401, 403))
        r, _ = self.s.req("POST", "/api/dates/fix", headers={"X-CSRF": "wrong"}, body=body)
        self.assertEqual(r.status, 403)
        self.assertIsNone(side_date(self.reset), "a refused request writes nothing")

    def test_no_report_yet(self):
        os.remove(os.path.join(self.state, "datecheck.json"))
        self.assertEqual(self.s.get("/api/facets")["counts"].get("dates", 0), 0)
        self.assertIsNone(self.s.get("/api/facets")["dates_checked"])
        self.assertEqual(self.listing(dates="all")["items"], [])


if __name__ == "__main__":
    unittest.main()
