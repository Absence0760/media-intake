#!/usr/bin/env python3
"""People and pets recognition: the shared core (recog.py) and the people command (people.py).

  python3 -I -m unittest discover -s tests -p test_recog.py

Synthetic and hermetic: Pillow drawings in a throwaway HOME and state folder; a mock detector and embedder
stand in for the models (each drawing says which made-up individuals it holds), and vectors are tight
blobs around random unit vectors. exiftool is never needed: sidecars are a dict behind a fake reader and
writer, except in the erase / trash / verify-gone cases, which run intake.py as a subprocess. The one
real-model case runs the venv's cv2 on the pinned YuNet + SFace and skips with a reason without them.
"""
import collections, hashlib, io, json, os, shutil, sqlite3, stat, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

support.check_hermetic()
import config  # noqa: E402
import recog  # noqa: E402
import people  # noqa: E402
import test_frames as TF  # noqa: E402  (ffmpeg-made clips for the real frame source)

try:
    import numpy as np
    from PIL import Image
except ImportError:  # the system Python's python3-numpy / python3-pillow (README, Install)
    np = None

PY = sys.executable
INTAKE = os.path.join(support.PKG, "intake.py")
DIM = 32
REAL_STATE = os.path.join(os.path.expanduser("~"), ".local/share/media-intake")
REAL_VENV_PY = os.path.join(REAL_STATE, "venv", "bin", "python")


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def sha_of(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class World:
    """Made-up individuals: a unit vector each. An image's red channel at (0, 0) is its scene number; SCENES
    maps it to the individuals in it, as (identity, species, box)."""

    def __init__(self, seed=7):
        rng = np.random.default_rng(seed)
        self.rng = rng
        self.base = {}
        self.scenes = {}
        self.calls = collections.Counter()

    def vec(self, ident):
        if ident not in self.base:
            v = self.rng.normal(size=DIM)
            self.base[ident] = v / np.linalg.norm(v)
        return self.base[ident]

    def scene(self, n, *who):
        """Scene n holds `who`: (identity, species) pairs, boxed left to right."""
        self.scenes[n] = [(ident, sp, (10 + 60 * i, 20, 50, 50)) for i, (ident, sp) in enumerate(who)]
        return n


class MockDetector:
    def __init__(self, world):
        self.world = world

    def detect(self, rgb):
        self.world.calls["detect"] += 1
        n = int(rgb[0, 0, 0])
        out = []
        for ident, sp, box in self.world.scenes.get(n, []):
            out.append((box, 0.95, sp, ("crop", ident)))  # the "crop" names the individual for MockEmbedder
        return out


class MockEmbedder:
    def __init__(self, world, noise=0.03):
        self.world, self.noise = world, noise

    def embed(self, crop):
        self.world.calls["embed"] += 1
        _, ident = crop
        return self.world.vec(ident) + self.world.rng.normal(scale=self.noise, size=DIM)


def image(path, scene, size=(200, 100)):
    """A PNG whose pixel (0, 0) carries the scene number; the rest noise seeded by the path."""
    seed = int(hashlib.sha256(path.encode()).hexdigest()[:8], 16)  # noise unlike any other file's: no near-copies
    a = np.random.default_rng(seed).integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
    a[0, 0, 0] = scene
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.fromarray(a).save(path)
    return path


def kind(world, k="face", models=()):
    return recog.Kind(kind=k, command="people" if k == "face" else "pets", prefix="People|" if k == "face" else "Pets|",
                      model="mock", link=0.6, same=0.5, models=list(models),
                      engine=lambda: (MockDetector(world), {"mock": MockEmbedder(world)}),
                      noun="face" if k == "face" else "pet", max_side=400)


class FakeSidecars:
    """intake.read_sidecar_tags / write_tags over a dict: {path: [HierarchicalSubject...]}."""

    def __init__(self):
        self.tags = collections.defaultdict(list)
        self.writes = 0

    def read(self, paths):
        return {p: (list(self.tags[p]), "", set(), None) for p in paths if p in self.tags}

    def write(self, entries):
        self.writes += 1
        for p, t in entries:
            cur = [x for x in self.tags[p] if x not in t.get("remove", [])]
            self.tags[p] = cur + [h for h in t.get("hier", []) if h not in cur]


@unittest.skipUnless(np, "numpy or Pillow missing (python3-numpy, python3-pillow)")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recog-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state, mode=0o700)
        self.env = {"HOME": self.home, "MEDIA_INTAKE_STATE": self.state, "PHOTO_LIBRARY": self.lib,
                    "PERSONAL_FOOTAGE": self.vids,
                    "MEDIA_INTAKE_CONFIG": support.write_config(os.path.join(self.home, "Videos/demo-footage"))}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        self.store = config.get()["recognition_dir"]
        self.world = World()
        self.spec = kind(self.world)
        self.c = recog.connect()
        self.addCleanup(lambda: self.c.close())
        self.side = FakeSidecars()

    def photo(self, rel, scene):
        return image(os.path.join(self.lib, rel), scene)

    def scan(self, paths, spec=None, **kw):
        return recog.scan(self.c, spec or self.spec, paths, log=lambda *a, **k: None, state=self.state, **kw)

    def tag(self, spec=None, dry=False):
        return recog.write_tags(self.c, spec or self.spec, dry=dry, reader=self.side.read, writer=self.side.write,
                                state=self.state)

    def det_of(self, path):
        sha = sha_of(path)
        return [dict(r) for r in self.c.execute("SELECT * FROM detections WHERE sha=? ORDER BY x", (sha,))]


class Clustering(unittest.TestCase):
    @unittest.skipUnless(np, "numpy missing")
    def test_constraints(self):
        rng = np.random.default_rng(1)
        a, b = (v / np.linalg.norm(v) for v in rng.normal(size=(2, DIM)))
        near = lambda v: (v + rng.normal(scale=0.02, size=DIM)) / np.linalg.norm(v)  # noqa: E731
        emb = np.stack([near(a), near(a), near(a), near(b), near(b)]).astype(np.float32)
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        comp = recog.cluster_vectors(emb, [None] * 5, photos=list("pqrst"), link=0.6, same=0.5)
        self.assertEqual(len({comp[0], comp[1], comp[2]}), 1)
        self.assertEqual(len({comp[3], comp[4]}), 1)
        self.assertNotEqual(comp[0], comp[3])
        # two in one photo are two individuals, however alike
        comp = recog.cluster_vectors(emb, [None] * 5, photos=list("ppqst"), link=0.6, same=0.5)
        self.assertNotEqual(comp[0], comp[1])
        # two owner names never join; nor a name and ignored
        comp = recog.cluster_vectors(emb, ["Ann", "Bo", None, None, None], photos=list("pqrst"), link=0.6, same=0.5)
        self.assertNotEqual(comp[0], comp[1])
        comp = recog.cluster_vectors(emb, ["Ann", recog.IGNORED, None, None, None], photos=list("pqrst"))
        self.assertNotEqual(comp[0], comp[1])
        # an owner merge joins what the vectors would not
        comp = recog.cluster_vectors(emb, [None] * 5, merges=[(0, 3)], photos=list("pqrst"), link=0.6, same=0.5)
        self.assertEqual(comp[0], comp[3])


class Store(Base):
    def test_scan_perms_reuse_unchanged_and_refusals(self):
        s1 = self.world.scene(1, ("ann", None), ("bo", None))
        p1 = self.photo("2026/2026-11/a.png", s1)
        copy = os.path.join(self.lib, "2026/copies/a.png")
        os.makedirs(os.path.dirname(copy))
        shutil.copy(p1, copy)
        shot = self.photo("Screenshots/s.png", s1)
        quar = image(os.path.join(self.state, "quarantine", "b1", "q.png"), s1)
        cnt = self.scan([p1, copy, shot, quar])
        self.assertEqual(cnt["photos scanned"], 1)
        self.assertEqual(cnt["same content as a scanned file"], 1)
        self.assertEqual(cnt["screenshots skipped"], 1)
        self.assertEqual(cnt["refused (quarantine, flagged, erased)"], 1)
        self.assertEqual(cnt["faces found"], 2)
        self.assertEqual(self.world.calls["detect"], 1, "the copy reused the first file's rows")
        # store permissions: 0700 folder, 0600 database
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(recog.db_path()).st_mode), 0o600)
        again = self.scan([p1, copy, shot])
        self.assertEqual(again["unchanged"], 3)
        self.assertEqual(self.world.calls["detect"], 1)
        # boxes are fractions of the image; nothing scanned for quarantine
        d = self.det_of(p1)
        self.assertAlmostEqual(d[0]["x"], 10 / 200)
        self.assertFalse(self.det_of(quar))
        self.assertIsNone(d[0]["t"])

    def test_store_refused_inside_the_library(self):
        with self.assertRaises(ValueError):
            recog.check_location(os.path.join(self.lib, "faces"))
        with self.assertRaises(ValueError):
            recog.check_location(self.home)  # contains the library
        recog.check_location(os.path.join(self.state, "recognition"))

    def test_config_dir_and_unknown_key(self):
        self.assertEqual(self.store, os.path.join(self.state, "recognition"))
        cfg = os.path.join(self.tmp, "c.toml")
        with open(cfg, "w") as f:
            f.write('[recognition]\ndir = "~/elsewhere/rec"\n')
        self.assertEqual(config.load(cfg)["recognition_dir"], os.path.join(self.home, "elsewhere/rec"))
        with open(cfg, "w") as f:
            f.write('[recognition]\nbogus = 1\n')
        with self.assertRaises(config.ConfigError):
            config.load(cfg)

    def test_video_frames_and_one_frame_rule(self):
        self.world.scene(3, ("ann", None), ("ann2", None))
        vid = write(os.path.join(self.vids, "2026/clip.mp4"), os.urandom(256))
        rgb = np.zeros((100, 200, 3), dtype=np.uint8)
        rgb[0, 0, 0] = 3
        frames = lambda p: [(0.0, rgb), (2.5, rgb)]  # noqa: E731  (lane "frames" gives frames.sample)
        self.assertEqual(self.scan([vid], frames=None)["videos skipped (no frame source yet)"], 1)
        cnt = self.scan([vid], frames=frames)
        self.assertEqual((cnt["videos scanned"], cnt["faces found"]), (1, 4))
        self.assertEqual(sorted({d["t"] for d in self.det_of(vid)}), [0.0, 2.5])
        # ann and ann2 are the same made-up vector: alike, but in one frame, so never one person
        self.world.base["ann2"] = self.world.vec("ann")
        self.c.execute("DELETE FROM scans")
        self.scan([vid], frames=frames)
        recog.recluster(self.c, self.spec)
        by_t = collections.defaultdict(set)
        for d in self.det_of(vid):
            by_t[d["t"]].add(d["cluster"])
        self.assertEqual([len(v) for v in by_t.values()], [2, 2])

    def test_a_video_that_cant_be_sampled_is_unreadable_and_the_scan_goes_on(self):
        import frames as frames_mod
        self.world.scene(5, ("ann", None))
        rgb = np.zeros((100, 200, 3), dtype=np.uint8)
        rgb[0, 0, 0] = 5
        bad = write(os.path.join(self.vids, "2026/bad.mp4"), os.urandom(256))       # fails after one frame
        refused = write(os.path.join(self.vids, "2026/refused.mp4"), os.urandom(256))
        good = write(os.path.join(self.vids, "2026/good.mp4"), os.urandom(256))
        photo = self.photo("2026/2026-11/p.png", 5)
        opened = []

        class Sampler:  # frames.sample's shape: an iterable context manager
            def __init__(self, path):
                self.path, self.closed = path, False
                opened.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.closed = True
                return False

            def __iter__(self):
                yield 0.0, rgb
                if self.path == bad:
                    raise frames_mod.FramesError("decoder failed")
                yield 1.0, rgb

        def sample(path):
            if path == refused:
                raise frames_mod.Refused(frames_mod.REFUSED)
            return Sampler(path)
        cnt = self.scan([bad, refused, good, photo], frames=sample)
        self.assertEqual((cnt["unreadable"], cnt["videos scanned"], cnt["photos scanned"]), (2, 1, 1), cnt)
        self.assertEqual(self.det_of(bad), [], "the frames read before the failure are dropped")
        self.assertEqual(len(self.det_of(good)), 2)
        self.assertTrue(opened and all(s.closed for s in opened), "every sampler is closed")
        status = {r["sha"]: r["status"] for r in self.c.execute("SELECT sha, status FROM scans")}
        self.assertEqual((status[sha_of(bad)], status[sha_of(refused)]), ("unreadable", "unreadable"))

    def test_crop_of_a_video_frame_closes_the_sampler(self):
        self.world.scene(6, ("ann", None))
        rgb = np.zeros((100, 200, 3), dtype=np.uint8)
        rgb[0, 0, 0] = 6
        vid = write(os.path.join(self.vids, "2026/long.mp4"), os.urandom(256))
        self.scan([vid], frames=lambda p: [(0.0, rgb)])
        det = self.det_of(vid)[0]["id"]
        state = {"read": 0, "closed": False}

        class Sampler:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                state["closed"] = True
                return False

            def __iter__(self):
                for i in range(100):
                    state["read"] += 1
                    yield float(i), rgb
        out = recog.crop_bytes(self.c, det, frames=lambda p: Sampler(), state=self.state)
        self.assertTrue(out and out[:2] == b"\xff\xd8", "a JPEG")
        self.assertEqual(state, {"read": 1, "closed": True}, "stops at the frame and closes the decoder")
        os.remove(os.path.join(recog.crops_dir(), f"{sha_of(vid)}-{det}.jpg"))
        import frames as frames_mod

        def broken(p):
            raise frames_mod.FramesError("no decoder")
        self.assertIsNone(recog.crop_bytes(self.c, det, frames=broken, state=self.state))

    def test_crop_of_a_real_video_frame(self):
        """frames.py end to end: a scan through the real sampler keeps each frame's own second, and the naming
        view's crop finds that frame again (no empty square). An ffmpeg testsrc clip; only the detector is a mock,
        told the scene by pixel (0, 0) of a copy of each real frame."""
        if not TF.H264:
            self.skipTest(TF.NEEDS)
        import frames as frames_mod
        self.world.scene(8, ("ann", None))
        vid = os.path.join(self.vids, "2026/real.mp4")
        os.makedirs(os.path.dirname(vid))
        TF.h264(vid, "testsrc=size=320x240:rate=10:duration=6")
        with frames_mod.sample(vid) as s:
            real = [(t, rgb.copy()) for t, rgb in s]
        self.assertTrue(real)

        def marked(p):  # the real frames, each with the mock detector's scene pixel
            with frames_mod.sample(p) as s:
                for t, rgb in s:
                    rgb = rgb.copy()
                    rgb[0, 0, 0] = 8
                    yield t, rgb
        cnt = self.scan([vid], frames=marked)
        self.assertEqual(cnt["videos scanned"], 1, cnt)
        dets = self.det_of(vid)
        self.assertEqual(sorted(d["t"] for d in dets)[:1], [real[0][0]], "the seconds the sampler gave")
        self.assertTrue({d["t"] for d in dets} <= {t for t, _ in real})
        d = dets[0]
        out = recog.crop_bytes(self.c, d["id"], state=self.state)  # the default frame source: frames.sample
        self.assertTrue(out and out[:2] == b"\xff\xd8", "a real crop, not None")
        im = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"))
        self.assertEqual(im.shape, (recog.CROP_PX, recog.CROP_PX, 3))
        frame = dict(real)[d["t"]]
        H, W = frame.shape[:2]
        want = recog.square_crop(frame, (d["x"] * W, d["y"] * H, d["w"] * W, d["h"] * H), margin=0.35)
        want = np.asarray(Image.fromarray(np.ascontiguousarray(want)).resize((recog.CROP_PX,) * 2, Image.BILINEAR))
        self.assertLess(float(np.abs(im.astype(int) - want.astype(int)).mean()), 12, "the box of that frame")
        self.assertGreater(float(im.std()), 10, "a picture, not an empty square")

    def test_species_never_mix(self):
        spec = kind(self.world, "pet")
        self.world.scene(4, ("rex", "dog"))
        self.world.scene(5, ("rex-cat", "cat"))
        self.world.base["rex-cat"] = self.world.vec("rex")  # same vector, other species
        paths = [self.photo(f"2026/p{i}.png", 4 + i % 2) for i in range(4)]
        self.scan(paths, spec=spec)
        recog.recluster(self.c, spec)
        cl = {r["species"]: set() for r in self.c.execute("SELECT species FROM detections")}
        for r in self.c.execute("SELECT species, cluster FROM detections"):
            cl[r["species"]].add(r["cluster"])
        self.assertTrue(cl["dog"].isdisjoint(cl["cat"]))
        with self.assertRaises(ValueError):
            recog.merge_clusters(self.c, "pet", min(cl["dog"]), min(cl["cat"]))


class NamesAndTags(Base):
    def setUp(self):
        super().setUp()
        self.s_ann = self.world.scene(1, ("ann", None))
        self.s_pair = self.world.scene(2, ("ann", None), ("bo", None))
        self.p = [self.photo(f"2026/a{i}.png", self.s_ann) for i in range(2)]
        self.scan(self.p)
        recog.recluster(self.c, self.spec)
        (self.ann,) = {d["cluster"] for p in self.p for d in self.det_of(p)}
        recog.name_cluster(self.c, "face", self.ann, "Ann")

    def grow(self, name, scene):
        p = self.photo(f"2026/{name}.png", scene)
        self.scan([p])
        recog.recluster(self.c, self.spec)
        return p

    def test_suggestion_confirm_tag(self):
        self.tag()
        for p in self.p:
            self.assertEqual(self.side.tags[p], ["People|Ann"])
        new = self.grow("later", self.s_ann)
        (d,) = self.det_of(new)
        self.assertEqual((d["name"], d["how"]), ("Ann", "suggested"))
        sug = recog.suggestions(self.c, "face")
        self.assertEqual([s["det"] for s in sug], [d["id"]])
        self.assertEqual(sug[0]["path"], new)
        self.tag()
        self.assertNotIn(new, self.side.tags, "a suggestion is never a tag")
        self.assertEqual(recog.find(self.c, "face", "ann", state=self.state), sorted(
            [(0.125, 1, p) for p in self.p], key=lambda o: o[2]))
        self.assertEqual(len(recog.find(self.c, "face", "Ann", suggested=True, state=self.state)), 3)
        self.assertEqual(recog.confirm(self.c, "face", recog.suggested_ids(self.c, "face", [d["cluster"]])), 1)
        self.tag()
        self.assertEqual(self.side.tags[new], ["People|Ann"])
        self.assertEqual(recog.names(self.c, "face"), [("Ann", 3, 0)])
        cl = recog.clusters(self.c, "face")[0]
        self.assertEqual((cl["name"], cl["how"], cl["count"], cl["suggested"]), ("Ann", "owner", 3, 0))
        self.assertLessEqual(set(cl["crops"]), {x["id"] for x in self.c.execute("SELECT id FROM detections")})

    def test_tag_only_some_paths(self):
        """write_tags(only=...): the gallery writes the files one naming action touched, and reads no other."""
        seen = []
        reader = lambda paths: (seen.extend(paths), self.side.read(paths))[1]  # noqa: E731
        cnt = recog.write_tags(self.c, self.spec, reader=reader, writer=self.side.write, state=self.state,
                               only={self.p[0]})
        self.assertEqual((cnt["sidecars changed"], seen), (1, [self.p[0]]))
        self.assertEqual(self.side.tags[self.p[0]], ["People|Ann"])
        self.assertNotIn(self.p[1], self.side.tags)
        self.assertEqual(self.tag()["sidecars changed"], len(self.p) - 1, "the rest on the next full run")

    def test_reject_is_never_suggested_again(self):
        new = self.grow("other", self.s_ann)
        (d,) = self.det_of(new)
        self.assertEqual(recog.reject(self.c, "face", [d["id"]]), 1)
        self.assertEqual(self.det_of(new)[0]["how"], None)
        recog.recluster(self.c, self.spec)
        self.assertEqual(self.det_of(new)[0]["how"], None, "a rejected name is not suggested again")
        recog.name_cluster(self.c, "face", d["cluster"], "Ann")
        self.assertIsNone(self.det_of(new)[0]["name"], "naming the cluster skips the rejected one too")
        with self.assertRaises(ValueError):
            recog.confirm(self.c, "face", [999999])

    def test_prune_keeps_an_unplugged_library_and_drops_a_gone_file(self):
        """An unmounted library folder (a drive) is not a deletion: names, rejects and take-outs stay. A file
        missing while its folder is there is gone."""
        new = self.grow("other", self.s_ann)
        (d,) = self.det_of(new)
        recog.reject(self.c, "face", [d["id"]])
        out_id = self.det_of(self.p[1])[0]["id"]
        recog.take_out(self.c, "face", [out_id])
        before = sorted(tuple(r) for r in self.c.execute("SELECT id, sha, name, how FROM detections"))
        files = self.c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        away = os.path.join(self.tmp, "unplugged")
        os.rename(self.lib, away)  # the library's drive is not mounted
        self.assertEqual(recog.prune(self.c, state=self.state)["gone or moved"], 0)
        self.assertEqual(sorted(tuple(r) for r in self.c.execute("SELECT id, sha, name, how FROM detections")), before)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM files").fetchone()[0], files)
        os.rename(away, self.lib)  # back again
        recog.recluster(self.c, self.spec)
        self.assertEqual(self.det_of(self.p[0])[0]["name"], "Ann")
        self.assertEqual(self.det_of(self.p[1])[0]["how"], "out", "the take-out survived")
        self.assertIsNone(self.det_of(new)[0]["how"], "the reject survived: not suggested again")
        sha = sha_of(new)
        os.remove(new)  # truly gone, its folder there
        self.assertEqual(recog.prune(self.c, state=self.state)["gone or moved"], 1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM detections WHERE sha=?", (sha,)).fetchone()[0], 0)
        self.assertEqual(self.det_of(self.p[0])[0]["name"], "Ann")

    def test_an_unread_sidecar_is_not_the_owner_removing_a_name(self):
        """A sidecar that is missing, or there but unreadable, never turns a name into how='out'."""
        import intake
        self.tag()
        p0, p1 = self.p
        del self.side.tags[p0]  # the sidecar was deleted
        del self.side.tags[p1]  # ...and this one is there, but exiftool could not read it
        with open(intake.sidecar(p1), "w") as f:
            f.write("not xmp")
        cnt = self.tag()
        self.assertNotIn("left off: the owner removed it", cnt)
        self.assertEqual(cnt["left alone: sidecar unreadable"], 1)
        self.assertEqual([d["how"] for p in self.p for d in self.det_of(p)], ["cluster", "cluster"])
        self.assertEqual(self.side.tags[p0], ["People|Ann"], "a missing sidecar gets the name again")
        self.assertNotIn(p1, self.side.tags, "an unreadable one is not written")
        self.assertEqual(self.c.execute("SELECT state FROM written WHERE path=?", (p1,)).fetchall()[0][0], "on")
        os.remove(intake.sidecar(p1))  # readable again (here: gone, so written anew)
        self.tag()
        self.assertEqual(self.side.tags[p1], ["People|Ann"])
        self.assertEqual(self.det_of(p1)[0]["name"], "Ann")

    def test_owner_removed_tag_never_re_added(self):
        self.tag()
        self.side.tags[self.p[0]] = ["Owner|keep"]  # the owner deleted People|Ann in digiKam
        cnt = self.tag()
        self.assertEqual(cnt["left off: the owner removed it"], 1)
        self.assertEqual(self.side.tags[self.p[0]], ["Owner|keep"])
        self.assertEqual(self.det_of(self.p[0])[0]["how"], "out")
        recog.recluster(self.c, self.spec)
        recog.name_cluster(self.c, "face", self.ann, "Ann")
        self.tag()
        self.assertEqual(self.side.tags[self.p[0]], ["Owner|keep"], "never re-added")

    def test_rename_removes_only_its_own_tags(self):
        self.side.tags[self.p[1]] = ["People|Someone"]  # the owner's own People| tag
        self.tag()
        recog.unname_cluster(self.c, "face", self.ann)
        recog.name_cluster(self.c, "face", self.ann, "Annie")
        self.tag()
        self.assertEqual(self.side.tags[self.p[0]], ["People|Annie"])
        self.assertEqual(sorted(self.side.tags[self.p[1]]), ["People|Annie", "People|Someone"])
        self.assertEqual(self.tag()["sidecars changed"], 0, "idempotent")

    def test_merge_take_out_put_back_ignore_and_hints(self):
        pair = self.grow("pair", self.s_pair)
        bo = next(d for d in self.det_of(pair) if d["x"] > 0.2)
        with self.assertRaises(ValueError):
            recog.merge_clusters(self.c, "face", self.ann, self.ann)
        recog.name_cluster(self.c, "face", bo["cluster"], "Bo")
        with self.assertRaises(ValueError):
            recog.merge_clusters(self.c, "face", self.ann, bo["cluster"])  # two owner names
        a0 = self.det_of(self.p[0])[0]["id"]
        recog.take_out(self.c, "face", [a0])
        self.assertEqual(self.det_of(self.p[0])[0]["how"], "out")
        recog.recluster(self.c, self.spec)
        self.assertEqual(self.det_of(self.p[0])[0]["how"], "out", "a tool never changes a take-out")
        self.assertEqual(recog.put_back(self.c, "face", [a0]), 1)
        self.assertEqual(self.det_of(self.p[0])[0]["name"], "Ann")
        recog.unname_cluster(self.c, "face", self.det_of(pair)[1]["cluster"])
        recog.name_cluster(self.c, "face", self.det_of(pair)[1]["cluster"], recog.IGNORED)
        self.tag()
        self.assertNotIn("People|Bo", self.side.tags.get(pair, []))
        self.assertFalse(any("\x00" in t for ts in self.side.tags.values() for t in ts))
        with self.assertRaises(ValueError):
            recog.check_name("People|x")

    def test_refused_and_trashed_content_dropped(self):
        p0 = self.p[0]
        s0 = sha_of(p0)
        crop = recog.crop_bytes(self.c, self.det_of(p0)[0]["id"], state=self.state)
        self.assertTrue(crop.startswith(b"\xff\xd8"))
        (cp,) = [os.path.join(recog.crops_dir(), f) for f in os.listdir(recog.crops_dir())]
        self.assertEqual(stat.S_IMODE(os.stat(cp).st_mode), 0o600)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-19 10:00 forget-x sha256:{s0} size:{os.path.getsize(p0)}\n")
        out = recog.prune(self.c, state=self.state)
        self.assertEqual(out["flagged, erased, private or trashed"], 1)
        self.assertFalse(self.det_of(p0))
        self.assertFalse(os.path.exists(cp))
        self.assertIsNone(recog.crop_bytes(self.c, 1, state=self.state))
        s1 = sha_of(self.p[1])
        with open(os.path.join(self.state, "trashed.json"), "w") as f:
            json.dump({s1: os.path.getsize(self.p[1])}, f)
        recog.prune(self.c, state=self.state)
        self.assertFalse(self.det_of(self.p[1]))
        self.assertEqual(self.scan([self.p[1]])["trashed content skipped"], 1)

    def test_forget_and_leftovers(self):
        s0 = sha_of(self.p[0])
        recog.crop_bytes(self.c, self.det_of(self.p[0])[0]["id"], state=self.state)
        self.assertEqual(recog.leftovers({s0}), {"detections": 1, "vectors": 1, "file rows": 1, "crops": 1})
        self.c.close()
        out = recog.forget({s0, "not-a-sha"})
        self.assertEqual(out, {"detections": 1, "vectors": 1, "file rows": 1, "crops": 1})
        self.assertEqual(recog.leftovers({s0}), {})
        self.c = recog.connect()
        self.assertEqual(len(self.det_of(self.p[1])), 1, "other content stays")

    def test_stray_temp_crop_and_unreadable_store(self):
        s0 = sha_of(self.p[0])
        tmp = write(os.path.join(recog.crops_dir(), f"{s0}-1.jpg.123.tmp"), b"half a crop")
        self.assertEqual(recog.leftovers({s0})["crops"], 1)
        self.c.close()
        with open(recog.db_path(), "wb") as f:
            f.write(b"not a database" * 100)
        out = recog.forget({s0})
        self.assertEqual(out, {"store unreadable": 1, "crops": 1}, "the crops still go")
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(recog.leftovers({s0}), {"store unreadable": 1})
        os.remove(recog.db_path())
        self.c = recog.connect()

    @unittest.skipUnless(shutil.which("exiftool"), "exiftool missing (perl-Image-ExifTool)")
    def test_real_sidecar_write_leaves_the_original(self):
        before = sha_of(self.p[0])
        recog.write_tags(self.c, self.spec, state=self.state)
        self.assertEqual(sha_of(self.p[0]), before, "the original is never written")
        import intake
        self.assertIn("People|Ann", intake.read_sidecar_tags([self.p[0]])[self.p[0]][0])
        self.assertTrue(os.path.isfile(self.p[0] + ".xmp"))

    def test_eval_numbers_only(self):
        pair = self.grow("pair", self.s_pair)
        bo = sha_of(self.grow("bo", self.world.scene(3, ("bo", None))))
        labels = {r["id"]: "Bo" if r["sha"] == bo or (r["sha"] == sha_of(pair) and r["x"] > 0.2) else "Ann"
                  for r in self.c.execute("SELECT id, sha, x FROM detections")}
        res = recog.evaluate(self.c, self.spec, labels)["mock"]
        self.assertEqual((res["labelled"], res["individuals"]), (5, 2))
        self.assertEqual(res["roc_auc"], 1.0)
        self.assertEqual(res["mixed_clusters"], 0)
        self.assertEqual(res["nearest_neighbour_accuracy"], 1.0)
        self.side.tags[self.p[0]] = ["Calibrate|people|Ann"]
        self.assertEqual(list(recog.calibration_labels(self.c, self.spec, reader=self.side.read).values()), ["Ann"])


class Cli(Base):
    def test_off_until_the_models_exist(self):
        spec = kind(self.world, models=[("YuNet", os.path.join(self.tmp, "none.onnx"), "0" * 64)])
        with self.assertRaises(SystemExit) as e:
            recog.main(spec, ["clusters"])
        self.assertIn("is off", str(e.exception))
        self.assertIn("bash setup.sh --recognition", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "none.onnx")))
        self.assertEqual([p for _, p, _ in people.model_files()], people.spec().missing())

    def test_help_says_touches_and_deletes(self):
        out = recog.build_parser(people.spec()).format_help()
        self.assertIn("\nTouches: ", out)
        self.assertIn("\nDeletes: ", out)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 80, line)


class EraseCoverage(Base):
    """erase (via forget), trash, sweep-erased and verify-gone reach the recognition store."""

    def setUp(self):
        super().setUp()
        self.s = self.world.scene(1, ("ann", None))
        self.p = self.photo("2026/2026-11/a.png", self.s)
        self.keep = self.photo("2026/2026-11/b.png", self.world.scene(2, ("bo", None)))
        self.scan([self.p, self.keep])
        recog.crop_bytes(self.c, self.det_of(self.p)[0]["id"], state=self.state)
        self.sha = sha_of(self.p)
        self.c.close()

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=dict(os.environ, **self.env), capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def coverage(self):
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            lines = [ln for ln in f.read().splitlines() if " coverage " in ln]
        return json.loads(lines[-1].split(" coverage ", 1)[1])

    def test_forget_drops_rows_and_crops_and_verify_gone_checks(self):
        self.intake("forget", self.p, "--yes")
        self.assertEqual(recog.leftovers({self.sha}), {})
        self.assertEqual(self.coverage()["reached"][
            "people and pets recognition data (detections, vectors, crops; recognition.dir)"], 4)
        self.assertEqual(recog.leftovers({sha_of(self.keep)})["detections"], 1, "other content stays")
        self.intake("verify-gone")
        c = sqlite3.connect(recog.db_path())  # a row put back by hand: verify-gone must say so
        c.execute("INSERT INTO detections(sha, kind) VALUES (?, 'pet')", (self.sha,))
        c.commit()
        c.close()
        out = self.intake("verify-gone", code=1)
        self.assertIn("STILL PRESENT", out)
        self.assertIn("people and pets recognition data", out)

    def test_one_erase_clears_detections_place_and_date_records(self):
        """ONE file with detections, a place record and a date fix: forget clears all three stores (and a
        check-dates finding that names it), the coverage report counts them, verify-gone passes, and a record put
        back by hand fails it with a count, never the name."""
        add_place = ("import sys, intake, places; st = places.Store(intake.STATE, intake.LIB_ROOTS); "
                     "pid = st.add('Home', 1.0, 2.0); [st.tagged[pid].add(p) for p in sys.argv[1:]]; st.save()")
        r = subprocess.run([PY, "-I", "-c", f"import sys; sys.path.insert(0, {support.PKG!r}); " + add_place,
                            self.p, self.keep], env=dict(os.environ, **self.env), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.intake("fix-dates", "--set", "2024-05-12T10:00", self.p, self.keep, "--yes")
        rp = os.path.join(self.state, "datecheck.json")
        with open(rp, "w") as f:
            json.dump(dict(version=1, id="r1", checked="2026-11-19T10:00:00", files=2, findings=[
                dict(path=self.p, kind="reset", confidence="high", reason="x", date="2016-01-01T00:00:00"),
                dict(path=self.keep, kind="duplicate", confidence="medium", reason="the same content is in the "
                     "library as a.png dated 2016-01-01 00:00", date="2024-05-12T10:00:00")]), f)
        name = os.path.basename(self.p)

        def stores():
            out = ""
            for x in ("places.json", "date-fixes.json", "datecheck.json"):
                with open(os.path.join(self.state, x)) as f:
                    out += f.read()
            return out
        self.assertIn(name, stores())
        self.intake("forget", self.p, "--yes")
        self.assertEqual(recog.leftovers({self.sha}), {})
        self.assertNotIn(name, stores(), "no record of it in places.json, date-fixes.json or datecheck.json")
        self.assertIn(os.path.basename(self.keep), stores(), "the other file's records stay")
        reached = self.coverage()["reached"]
        self.assertEqual(reached["date records: erased files dropped (state folder, datecheck.json and "
                                 "date-fixes.json)"], 3, "its fix entry, its finding, the copy's naming it")
        self.assertEqual(reached["your places' records: erased files dropped (state folder, places.json)"], 1)
        self.assertNotIn("DATE RECORDS", self.intake("verify-gone"))
        with open(rp) as f:
            rep = json.load(f)
        rep["findings"].append(dict(path=self.p, kind="reset", confidence="high", reason="x", date="2016"))
        with open(rp, "w") as f:
            json.dump(rep, f)
        out = self.intake("verify-gone", code=1)
        self.assertIn("DATE RECORDS: 1 entries", out)
        self.assertNotIn(name, out, "counts only")

    def date_records(self):
        p = os.path.join(self.state, "date-fixes.json")
        with open(p) as f:
            return f.read()

    def test_trash_drops_rows(self):
        self.intake("fix-dates", "--set", "2024-05-12T10:00", self.p, "--yes")
        self.intake("trash", self.p)  # a dry run without --yes: nothing goes
        self.assertEqual(recog.leftovers({self.sha})["detections"], 1)
        self.assertIn(os.path.basename(self.p), self.date_records())
        out = self.intake("trash", self.p, "--yes")
        self.assertIn("recognition rows and crops 4", out)
        self.assertEqual(recog.leftovers({self.sha}), {})
        self.assertNotIn(os.path.basename(self.p), self.date_records(), "its date fix record went too")

    def test_sweep_erased_drops_rows(self):
        self.intake("fix-dates", "--set", "2024-05-12T10:00", self.p, "--yes")
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-19 10:00 b1 sha256:{self.sha} size:{os.path.getsize(self.p)}\n")
        copy = self.photo("2026/2026-10/copy.png", 9)
        shutil.copy(self.p, copy)  # a copy erase missed, which sweep-erased deletes
        os.remove(self.p)
        out = self.intake("sweep-erased", "--yes")
        self.assertIn("recognition rows and crops", out)
        self.assertIn("date records", out)
        self.assertEqual(recog.leftovers({self.sha}), {})
        self.assertNotIn(os.path.basename(self.p), self.date_records(), "the gone file's date fix record went")

    def test_backup_never_walks_the_store(self):
        import backup
        cfg = dict(config.get(), recognition_dir=os.path.join(self.lib, "rec"))  # misconfigured into the library
        crop = self.photo("rec/crops/x.png", 9)
        walked = [src for src, _ in backup.walk_library(cfg)]
        self.assertIn(self.p, walked)
        self.assertNotIn(crop, walked)

    def test_no_store_is_never_created(self):
        shutil.rmtree(self.store)
        self.assertEqual(recog.forget({self.sha}), {})
        self.assertEqual(recog.leftovers({self.sha}), {})
        self.assertFalse(os.path.exists(self.store))


@unittest.skipUnless(os.path.exists(REAL_VENV_PY), "setup.sh not run (no venv)")
class RealModels(unittest.TestCase):
    """YuNet + SFace through the venv's cv2, on a synthetic image: loads, checks the sha256, runs."""

    def test_engine_runs(self):
        files = people.model_files(REAL_STATE)
        missing = [p for _, p, _ in files if not os.path.isfile(p)]
        if missing:
            self.skipTest("recognition models not installed (bash setup.sh --recognition)")
        d = tempfile.mkdtemp(prefix="recog-real-")
        self.addCleanup(shutil.rmtree, d, True)
        state = os.path.join(d, "state")
        os.makedirs(state, mode=0o700)
        os.symlink(os.path.join(REAL_STATE, "models"), os.path.join(state, "models"))  # read-only use
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import numpy as np, people\n"
                "det, embs = people.engine()\n"
                "img = np.full((480, 640, 3), 128, np.uint8)\n"
                "print(len(det.detect(img)), len(embs[people.MODEL].embed(np.zeros((112, 112, 3), np.uint8))))\n")
        r = subprocess.run([REAL_VENV_PY, "-I", "-c", code, support.PKG], capture_output=True, text=True,
                           env=dict(os.environ, HOME=d, MEDIA_INTAKE_STATE=state), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["0", "128"])


if __name__ == "__main__":
    unittest.main()
