#!/usr/bin/env python3
"""The gallery's People & Pets (gallery.py's Rec over recog.py) through its HTTP API, on a synthetic store.

  python3 -I -m unittest discover -s tests -p test_gallery_people.py

The recognition store is built in this process through recog's own API (scan, recluster) with
tests/test_recog.py's mock detector and embedder: each synthetic image's pixel (0, 0) names the made-up
individuals in it, so no model is ever loaded. The gallery then runs as a subprocess on a throwaway HOME,
as in tests/test_gallery.py, and writes the People| / Pets| tags with the real exiftool. Covered: the
feature off without a store (no section, every route off or 404, no store created), the sidebar facet and
the ?who= filter, naming, Undo, ignore, unname, merge (by number and by name; across species refused),
take out and put back, suggestions confirmed and rejected after a re-cluster, 409 for a page listed before
that re-cluster, 403 / 415 on a POST without CSRF or JSON, crops served by detection id only (0600 in a
0700 folder, no-store) and never for a flagged file, and the large view's names and a video's times.
"""
import os, shutil, stat, sys, tempfile, unittest, urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import test_gallery as G  # noqa: E402  (the server wrapper, sidecar reader, synthetic video)
import test_recog as TR  # noqa: E402  (mock detector / embedder, synthetic scenes)

support.check_hermetic()
import config  # noqa: E402
import people  # noqa: E402
import recog  # noqa: E402

PET_MODEL = "mock-pet"


class RecLibrary:
    """A throwaway HOME with a library and a recognition store built through recog's API.

    Individuals: ann (people; 3 photos alone, 1 with bo, a video at 1.5 s and 12 s), bo (2 alone + that
    one), a look-alike of ann (2 photos; cosine about 0.43: its own cluster at the mock's 0.5, but "looks like"
    ann at SFace's 0.363), dogs rex and rex2 (2 photos each, unlike vectors: two clusters to merge) and a cat (2 photos).
    flag.png shows ann too but is in the erase log: never listed, never a crop."""

    def __init__(self, tmp, video=True):
        self.tmp = tmp
        self.home = os.path.join(tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.foot = os.path.join(self.home, "Videos/demo-footage")
        os.makedirs(self.state, mode=0o700)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.foot))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.world = TR.World()
        w = self.world
        self.scenes = dict(ann=w.scene(1, ("ann", None)), bo=w.scene(2, ("bo", None)),
                           both=w.scene(3, ("ann", None), ("bo", None)), rex=w.scene(4, ("rex", "dog")),
                           rex2=w.scene(5, ("rex2", "dog")), cat=w.scene(6, ("tom", "cat")),
                           like=w.scene(7, ("annlike", None)))
        a = w.vec("ann")
        o = w.rng.normal(size=TR.DIM)
        o -= (o @ a) * a
        w.base["annlike"] = 0.45 * a + (1 - 0.45 ** 2) ** 0.5 * o / TR.np.linalg.norm(o)
        L = lambda *p: os.path.join(self.lib, "2026", *p)  # noqa: E731
        self.ann = [TR.image(L("2026-11", f"ann{i}.png"), 1) for i in range(3)]
        self.bo = [TR.image(L("2026-10", f"bo{i}.png"), 2) for i in range(2)]
        self.both = TR.image(L("2026-10", "both.png"), 3)
        self.rex = [TR.image(L("2026-09", f"rex{i}.png"), 4) for i in range(2)]
        self.rex2 = [TR.image(L("2026-09", f"rexb{i}.png"), 5) for i in range(2)]
        self.cat = [TR.image(L("2026-08", f"cat{i}.png"), 6) for i in range(2)]
        self.like = [TR.image(L("2026-06", f"like{i}.png"), 7) for i in range(2)]
        self.flag = TR.image(L("2026-11", "flag.png"), 1)
        self.video = None
        if video:
            self.video = os.path.join(self.vids, "2026/2026-07/clip.mp4")
            G.vid(self.video)
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.store = config.get()["recognition_dir"]
        self.c = recog.connect()
        self.face = recog.Kind(kind="face", command="people", prefix="People|", model=people.MODEL, link=0.6,
                               same=0.5, models=[], noun="face", max_side=400,
                               engine=lambda: (TR.MockDetector(w), {people.MODEL: TR.MockEmbedder(w)}))
        self.pet = recog.Kind(kind="pet", command="pets", prefix="Pets|", model=PET_MODEL, link=0.6, same=0.5,
                              models=[], noun="pet", max_side=400,
                              engine=lambda: (TR.MockDetector(w), {PET_MODEL: TR.MockEmbedder(w)}))
        rgb = TR.np.zeros((100, 200, 3), dtype=TR.np.uint8)
        rgb[0, 0, 0] = 1
        frames = (lambda p: [(1.5, rgb), (12.0, rgb)]) if video else None
        faces = self.ann + self.bo + self.like + [self.both, self.flag] + ([self.video] if video else [])
        recog.scan(self.c, self.face, faces, frames=frames, log=lambda *a: None, state=self.state)
        recog.scan(self.c, self.pet, self.rex + self.rex2 + self.cat, frames=None, log=lambda *a: None,
                   state=self.state)
        recog.recluster(self.c, self.face)
        recog.recluster(self.c, self.pet)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:  # flagged after the scan: never shown
            f.write(f"2026-11-01 10:00 b0 sha256:{G.sha(self.flag)} size:{os.path.getsize(self.flag)}\n")

    def close(self):
        self.c.close()
        self.patch.stop()

    def cluster_of(self, path, kind="face"):
        """The cluster number of the (first) detection in path."""
        return self.c.execute("SELECT cluster FROM detections WHERE sha=? AND kind=? ORDER BY x",
                              (G.sha(path), kind)).fetchone()[0]

    def dets(self, path, kind="face"):
        return [r[0] for r in self.c.execute("SELECT id FROM detections WHERE sha=? AND kind=? ORDER BY x",
                                             (G.sha(path), kind))]

    def more_ann(self, n=2):
        """New photos of ann, scanned and re-clustered: they join the named cluster as suggestions."""
        new = [TR.image(os.path.join(self.lib, "2026/2026-12", f"new{i}.png"), 1) for i in range(n)]
        recog.scan(self.c, self.face, new, frames=None, log=lambda *a: None, state=self.state)
        recog.recluster(self.c, self.face)
        return new


def hier(p):
    return G.tags_of(p)[0]


@unittest.skipUnless(TR.np, "numpy or Pillow missing (python3-numpy, python3-pillow)")
class PeopleApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-people-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def start(self, env):
        s = G.Server(env)
        self.servers.append(s)
        return s

    def world(self, **kw):
        w = RecLibrary(self.tmp, **kw)
        self.addCleanup(w.close)
        return w

    def rec(self, s, **q):
        return s.get("/api/rec?" + urllib.parse.urlencode(q))

    def post(self, s, act, body):
        return s.post("/api/rec/" + act, body)

    def ids_named(self, s, tag):
        j = s.get("/api/list?" + urllib.parse.urlencode(dict(who=tag, limit=500)))
        return sorted(i["n"] for i in j["items"])

    def test_off_without_a_store(self):
        w = self.world(video=False)
        w.c.close()
        shutil.rmtree(w.store)
        s = self.start(w.env)
        self.assertIsNone(s.get("/api/facets")["rec"])
        self.assertEqual(self.rec(s), {"on": False})
        self.assertEqual(s.req("GET", "/r/1")[0].status, 404)
        code, j = self.post(s, "name", dict(kind="face", run=1, cluster=1, name="Ann"))
        self.assertEqual(code, 400)
        self.assertIn("off", j["error"])
        self.assertFalse(os.path.exists(w.store), "the gallery never creates a recognition store")
        _, page = s.req("GET", "/")
        self.assertIn(b'id="recsec" hidden', page)

    def test_name_undo_ignore_merge_take_out(self):
        w = self.world(video=False)
        s = self.start(w.env)
        fc = s.get("/api/facets")["rec"]
        self.assertEqual([k["kind"] for k in fc["kinds"]], ["face", "pet"])
        face = fc["kinds"][0]
        self.assertEqual((face["names"], face["unnamed"], face["suggestions"]), ([], 3, 0))
        j = self.rec(s, kind="face")
        run, ann = j["run"], w.cluster_of(w.ann[0])
        self.assertEqual(j["counts"], dict(todo=3, named=0, ignored=0, sugg=0))
        self.assertFalse(any(d.get("like") for d in j["clusters"]), "no hint before anyone is named")
        card = next(d for d in j["clusters"] if d["id"] == ann)
        self.assertEqual(card["count"], 4, "flag.png's face was dropped (prune) once it was flagged")
        self.assertEqual(len(card["crops"]), 4)

        code, r = self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="  Ann  "))
        self.assertEqual(code, 200, r)
        self.assertIn("named Ann", r["result"])
        for p in w.ann + [w.both]:
            self.assertIn("People|Ann", hier(p))
        self.assertNotIn("People|Ann", hier(w.bo[0]) if os.path.exists(w.bo[0] + ".xmp") else [])
        fc = s.get("/api/facets")["rec"]["kinds"][0]
        self.assertEqual(fc["names"], [["Ann", 4]])
        self.assertEqual(self.ids_named(s, "People|Ann"), ["ann0.png", "ann1.png", "ann2.png", "both.png"])
        like = next(d for d in self.rec(s, kind="face")["clusters"] if d["id"] == w.cluster_of(w.like[0]))
        self.assertEqual([(x["cluster"], x["name"]) for x in like["like"]], [(ann, "Ann")])
        self.assertTrue(0.363 <= like["like"][0]["score"] < 0.5, like)
        # Undo: the name and its tags go again
        code, u = s.post("/api/undo", dict(undo=r["undo"]))
        self.assertEqual(code, 200, u)
        self.assertEqual(u["kind"], "rec")
        self.assertNotIn("People|Ann", hier(w.ann[0]))
        self.assertEqual(self.ids_named(s, "People|Ann"), [])
        self.assertEqual(s.post("/api/undo", dict(undo=r["undo"]))[0], 400, "one use per token")
        code, r = self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Ann"))
        # a rename since: that Undo is refused and the new name stays
        self.assertEqual(self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Anna"))[0], 200)
        self.assertEqual(s.post("/api/undo", dict(undo=r["undo"]))[0], 409)
        self.assertIn("People|Anna", hier(w.ann[0]))
        self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Ann"))
        # an answer naming a detection that is gone writes nothing at all
        sugg_ok = w.dets(w.ann[0])[0]
        code, r2 = self.post(s, "answer", dict(kind="face", run=run, confirm=[sugg_ok], reject=[10 ** 9]))
        self.assertEqual(code, 400, r2)

        # take out one face of ann1.png: its tag goes; put back: it comes back
        det = w.dets(w.ann[1])[0]
        code, r = self.post(s, "out", dict(kind="face", run=run, ids=[det]))
        self.assertEqual(code, 200, r)
        self.assertNotIn("People|Ann", hier(w.ann[1]))
        self.assertIn("People|Ann", hier(w.ann[0]))
        crops = {x["id"]: x["out"] for x in next(d for d in self.rec(s, kind="face", show="named")["clusters"]
                                                   if d["id"] == ann)["crops"]}
        self.assertTrue(crops[det])
        code, r = self.post(s, "back", dict(kind="face", run=run, ids=[det]))
        self.assertEqual(code, 200, r)
        self.assertIn("People|Ann", hier(w.ann[1]))

        # ignore bo's cluster: never tagged; unname puts it back to name
        bo = w.cluster_of(w.bo[0])
        code, r = self.post(s, "ignore", dict(kind="face", run=run, cluster=bo))
        self.assertEqual(code, 200, r)
        self.assertEqual(self.rec(s, kind="face", show="ignored")["counts"]["ignored"], 1)
        self.assertFalse(any(t.startswith("People|") for t in hier(w.bo[0])))
        self.assertEqual(self.post(s, "unname", dict(kind="face", run=run, cluster=bo))[0], 200)
        self.assertEqual(self.rec(s, kind="face")["counts"]["todo"], 2)

        # pets: merge rex2 into rex by the name, never across species
        prun = self.rec(s, kind="pet")["run"]
        rex, rex2, cat = (w.cluster_of(p, "pet") for p in (w.rex[0], w.rex2[0], w.cat[0]))
        self.assertEqual(self.post(s, "name", dict(kind="pet", run=prun, cluster=rex, name="Rex"))[0], 200)
        self.assertIn("Pets|Rex", hier(w.rex[0]))
        code, r = self.post(s, "merge", dict(kind="pet", run=prun, cluster=cat, into="rex"))
        self.assertEqual(code, 400)
        self.assertIn("different species", r["error"])
        code, r = self.post(s, "merge", dict(kind="pet", run=prun, cluster=rex2, into="rex"))
        self.assertEqual(code, 200, r)
        self.assertIn("(Rex)", r["result"])
        self.assertIn("Pets|Rex", hier(w.rex2[1]))
        self.assertEqual(self.ids_named(s, "Pets|Rex"), ["rex0.png", "rex1.png", "rexb0.png", "rexb1.png"])
        code, r = self.post(s, "merge", dict(kind="pet", run=prun, cluster=cat, into="Nobody"))
        self.assertEqual(code, 400)
        # a bad name, a bad kind, a missing cluster: 400, nothing written
        for body in (dict(kind="face", run=run, cluster=ann, name="a|b"),
                     dict(kind="face", run=run, cluster=ann, name=recog.IGNORED),
                     dict(kind="face", run=run, cluster=ann, name=7),
                     dict(kind="bird", run=run, cluster=ann, name="X"),
                     dict(kind=["face"], run=run, cluster=ann, name="X"),
                     dict(kind="face", run=True, cluster=ann, name="X"),
                     dict(kind="face", run=run, cluster=999, name="X"),
                     dict(kind="face", run=run, cluster="1", name="X")):
            self.assertEqual(self.post(s, "name", body)[0], 400, body)
        self.assertEqual(self.post(s, "out", dict(kind="face", run=run, ids=["1"]))[0], 400)
        self.assertEqual(self.post(s, "out", dict(kind="face", run=run, ids=[10 ** 9]))[0], 400)
        self.assertEqual(s.req("POST", "/api/rec/nope", headers={"X-CSRF": s.csrf()}, body={})[0].status, 404)

    def test_rename_unname_and_the_library_lock(self):
        w = self.world(video=False)
        G.write_tags(w.ann[0], ["Trips|Spain"])  # the owner's own tag: never touched
        originals = {p: G.sha(p) for p in w.ann + [w.both]}
        s = self.start(w.env)
        run, ann = self.rec(s, kind="face")["run"], w.cluster_of(w.ann[0])
        self.assertEqual(self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Ann"))[0], 200)
        code, r = self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Annie"))
        self.assertEqual(code, 200, r)
        self.assertNotIn("undo", r, "a rename can't be undone here (the dialog says so)")
        self.assertEqual(sorted(hier(w.ann[0])), ["People|Annie", "Trips|Spain"])
        self.assertEqual(self.ids_named(s, "People|Ann"), [])
        # while another command holds the library lock: 409 busy, nothing changes
        import library_lock
        fd = library_lock.acquire("test")
        try:
            code, r = self.post(s, "unname", dict(kind="face", run=run, cluster=ann))
            self.assertEqual((code, r.get("busy")), (409, True), r)
        finally:
            os.close(fd)
        self.assertIn("People|Annie", hier(w.ann[0]))
        code, r = self.post(s, "unname", dict(kind="face", run=run, cluster=ann))
        self.assertEqual(code, 200, r)
        self.assertNotIn("undo", r)
        self.assertEqual(hier(w.ann[0]), ["Trips|Spain"])
        self.assertFalse(any(t.startswith("People|") for t in hier(w.both)))
        self.assertEqual({p: G.sha(p) for p in originals}, originals, "originals are never written")

    def test_suggestions_and_a_stale_run(self):
        w = self.world(video=False)
        s = self.start(w.env)
        run, ann = self.rec(s, kind="face")["run"], w.cluster_of(w.ann[0])
        self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Ann"))
        new = w.more_ann(3)  # a re-cluster while the page is open
        code, r = self.post(s, "name", dict(kind="face", run=run, cluster=w.cluster_of(w.bo[0]), name="Bo"))
        self.assertEqual(code, 409)
        self.assertIn("reload", r["error"])
        self.assertFalse(any(t.startswith("People|") for t in hier(w.bo[0])) if os.path.exists(w.bo[0] + ".xmp")
                         else False)
        j = self.rec(s, kind="face", show="sugg")
        self.assertEqual(j["counts"]["sugg"], 3)
        self.assertEqual([(g["name"], len(g["ids"])) for g in j["suggestions"]], [("Ann", 3)])
        self.assertEqual(s.get("/api/facets")["rec"]["kinds"][0]["suggestions"], 3)
        for p in new:
            self.assertFalse(os.path.exists(p + ".xmp"), "a suggestion is never a tag")
        yes, no = w.dets(new[0]) + w.dets(new[1]), w.dets(new[2])
        code, r = self.post(s, "answer", dict(kind="face", run=j["run"], confirm=yes, reject=no))
        self.assertEqual(code, 200, r)
        self.assertEqual(r["result"], "2 confirmed, 1 rejected.")
        self.assertIn("People|Ann", hier(new[0]))
        self.assertFalse(os.path.exists(new[2] + ".xmp"))
        self.assertEqual(self.rec(s, kind="face", show="sugg")["counts"]["sugg"], 0)
        for body in (dict(confirm=yes, reject=yes), dict(), dict(confirm="x")):
            self.assertEqual(self.post(s, "answer", dict(kind="face", run=j["run"], **body))[0], 400, body)
        # an undo recorded before a re-cluster is refused afterwards
        code, r = self.post(s, "out", dict(kind="face", run=j["run"], ids=w.dets(w.ann[2])))
        self.assertEqual(code, 200, r)
        w.more_ann(1)
        code, u = s.post("/api/undo", dict(undo=r["undo"]))
        self.assertEqual(code, 409)
        self.assertNotIn("People|Ann", hier(w.ann[2]))

    def test_guards_and_crops(self):
        w = self.world(video=False)
        s = self.start(w.env)
        run, ann = self.rec(s, kind="face")["run"], w.cluster_of(w.ann[0])
        body = dict(kind="face", run=run, cluster=ann, name="Ann")
        r, _ = s.req("POST", "/api/rec/name", body=body)  # no CSRF
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/rec/name", headers={"X-CSRF": s.csrf(), "Content-Type": "text/plain"}, body=body)
        self.assertEqual(r.status, 415)
        r, _ = s.req("POST", "/api/rec/name", headers={"X-CSRF": s.csrf(), "Origin": "http://evil.example"}, body=body)
        self.assertEqual(r.status, 403)
        r, _ = s.req("GET", "/api/rec", token=False)
        self.assertNotEqual(r.status, 200)
        self.assertFalse(os.path.exists(w.ann[0] + ".xmp"), "nothing was named")
        # a crop by detection id: a JPEG, no-store, cached 0600 in a 0700 folder
        det = w.dets(w.ann[0])[0]
        r, data = s.req("GET", f"/r/{det}")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "image/jpeg"))
        self.assertIn("no-store", r.getheader("Cache-Control"))
        self.assertEqual(data[:2], b"\xff\xd8")
        crops = recog.crops_dir(w.store)
        self.assertEqual(stat.S_IMODE(os.stat(crops).st_mode), 0o700)
        made = os.listdir(crops)
        self.assertEqual(len(made), 1)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(crops, made[0])).st_mode), 0o600)
        self.assertFalse(os.path.exists(os.path.join(w.home, ".cache/media-intake/thumbs", made[0])))
        # never for a flagged file, an unknown id or anything but a number
        for path in [f"/r/{d}" for d in w.dets(w.flag)] + ["/r/999999", "/r/x", "/r/-1", f"/r/{det}/../1"]:
            self.assertEqual(s.req("GET", path)[0].status, 404, path)
        self.assertEqual(len(os.listdir(crops)), 1, "no crop was made for the flagged file")

    def test_large_view_names_and_video_times(self):
        w = self.world()
        s = self.start(w.env)
        run, ann = self.rec(s, kind="face")["run"], w.cluster_of(w.ann[0])
        code, r = self.post(s, "name", dict(kind="face", run=run, cluster=ann, name="Ann"))
        self.assertEqual(code, 200, r)
        items = {i["n"]: i["id"] for i in s.get("/api/list?limit=500")["items"]}
        d = s.get(f"/api/item/{items['clip.mp4']}")
        self.assertEqual(d["who"], [dict(kind="face", name="Ann", tag="People|Ann", times=[1.5, 12.0])])
        d = s.get(f"/api/item/{items['both.png']}")
        self.assertEqual([(x["name"], x["times"]) for x in d["who"]], [("Ann", [])])
        self.assertEqual(s.get(f"/api/item/{items['bo0.png']}")["who"], [])
        self.assertNotIn("flag.png", items)


if __name__ == "__main__":
    unittest.main()
