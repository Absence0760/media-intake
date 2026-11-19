#!/usr/bin/env python3
"""`media-intake pets` (pets.py) on the shared recognition core.

  python3 -I -m unittest discover -s tests -p test_pets.py

Synthetic and hermetic, like test_recog.py (whose made-up World, mock detector and embedder, drawings and fake
sidecars it reuses): a throwaway HOME, no model and no exiftool. YOLOX's output decoding, the NMS, the letterbox
and both embedders' preprocessing run on made-up arrays. The real-model case runs the venv's cv2 and
onnxruntime on the pinned YOLOX + DINOv2 and skips with a reason without them; with a public sample photo
(PETS_SAMPLE=<path to a photo of two cats>) it also checks both are found.
"""
import os, shutil, subprocess, sys, tempfile, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

support.check_hermetic()
import config  # noqa: E402
import pets  # noqa: E402
import recog  # noqa: E402
from test_recog import (DIM, REAL_STATE, REAL_VENV_PY, FakeSidecars, MockDetector, MockEmbedder,  # noqa: E402
                        World, image, np, sha_of, write)


def pet_spec(world, **kw):
    """pets.spec() with the mock engine: DINOv2 and SigLIP stand-ins (the SigLIP one noisier)."""
    s = pets.spec()
    s.models = []
    s.engine = lambda: (MockDetector(world), {pets.MODEL: MockEmbedder(world),
                                              pets.SIGLIP_MODEL: MockEmbedder(world, noise=0.3)})
    s.max_side = 400
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class Config(unittest.TestCase):
    def cfg(self, text):
        d = tempfile.mkdtemp(prefix="pets-cfg-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "config.toml")
        with open(p, "w") as f:
            f.write(text)
        return config.load(p)

    def test_defaults_and_choices(self):
        c = self.cfg("")
        self.assertEqual((c["pet_classes"], c["pet_min_frames"]), (["cat", "dog"], 3))
        c = self.cfg('[recognition]\npet_classes = ["dog", "horse", "dog"]\npet_min_frames = 5\n')
        self.assertEqual((c["pet_classes"], c["pet_min_frames"]), (["dog", "horse"], 5))
        self.assertEqual(pets.classes(c), ("dog", "horse"))
        self.assertEqual(pets.ANIMALS, config.PET_CLASSES, "the config's choices are YOLOX's COCO animals")
        for bad in ('pet_classes = ["person"]', "pet_classes = []", 'pet_classes = "cat"', "pet_min_frames = 0",
                    "pet_min_frames = true", "pet_min_frames = 2.5"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                self.cfg(f"[recognition]\n{bad}\n")

    def test_spec_and_pins(self):
        s = pets.spec()
        self.assertEqual((s.kind, s.command, s.prefix, s.noun, s.model), ("pet", "pets", "Pets|", "pet", pets.MODEL))
        self.assertEqual(s.min_frames, 3)
        self.assertEqual([w for w, _, _ in s.models], ["YOLOX animal detector", "DINOv2 pet recogniser"])
        zoo, dino = pets.models_dirs(config.get()["state"])
        self.assertEqual([os.path.dirname(p) for _, p, _ in s.models], [zoo, dino])
        self.assertTrue(dino.endswith("dinov2-small-" + pets.DINO["revision"][:7]))


class Decode(unittest.TestCase):
    """YOLOX's raw output: grid + stride decoding, the kept classes, NMS across classes, the minimum size."""

    @staticmethod
    def raw(rows):
        """An 8400 x 85 output that predicts nothing, except rows: (index, (cx, cy) offset, log w/h, obj, class, p)."""
        out = np.zeros((8400, 85), np.float32)
        out[:, 4] = 0.01
        for i, off, logwh, obj, cid, p in rows:
            out[i, :2], out[i, 2:4], out[i, 4], out[i, 5 + cid] = off, logwh, obj, p
        return out[None]

    @unittest.skipUnless(np, "numpy missing")
    def test_decode(self):
        g, st = pets.grids()
        self.assertEqual((len(g), int(st[0, 0]), int(st[-1, 0])), (80 * 80 + 40 * 40 + 20 * 20, 8, 32))
        cat, dog, person = pets.COCO.index("cat"), pets.COCO.index("dog"), pets.COCO.index("person")
        i = 80 * 80 + 40 * 40 + 20 * 5 + 7      # stride 32, cell (x 7, y 5): centre (7.5 * 32, 5.5 * 32)
        big = np.log(200 / 32)                  # 200 px in the letterboxed image
        out = self.raw([(i, (0.5, 0.5), (big, big), 0.95, cat, 0.98),
                        (i + 1, (0.4, 0.5), (big, big), 0.9, dog, 0.8),    # the same animal, scored as a dog
                        (i + 5, (0.5, 0.5), (big, big), 0.99, person, 0.99),  # not a pet class
                        (10, (0.5, 0.5), (np.log(4), np.log(4)), 0.99, dog, 0.99),  # 32 px: too small
                        (i + 9, (0.5, 0.5), (big, big), 0.3, dog, 0.9)])  # score 0.27: below SCORE_MIN
        found = pets.decode(out, 0.5, {cat: "cat", dog: "dog"})  # the photo was halved to fit 640
        self.assertEqual(len(found), 1, found)
        (x, y, w, h), score, cid = found[0]
        self.assertEqual(cid, cat)
        self.assertAlmostEqual(score, 0.95 * 0.98, places=5)
        np.testing.assert_allclose([x, y, w, h], [(7.5 * 32 - 100) * 2, (5.5 * 32 - 100) * 2, 400, 400], atol=1e-3)
        dogs = pets.decode(out, 0.5, {dog: "dog"})
        self.assertEqual([d[2] for d in dogs], [dog], "with cats off, the dog score of that box stands alone")
        self.assertEqual(pets.decode(self.raw([]), 1.0, {cat: "cat"}), [])

    @unittest.skipUnless(np, "numpy missing")
    def test_nms(self):
        boxes = np.array([[0, 0, 100, 100], [10, 0, 100, 100], [300, 300, 50, 50], [0, 0, 100, 100]], np.float32)
        self.assertEqual(pets.nms(boxes, np.array([0.5, 0.9, 0.4, 0.1], np.float32)), [1, 2])
        self.assertEqual(pets.nms(boxes[:2], np.array([0.5, 0.9], np.float32), thr=0.95), [1, 0])

    @unittest.skipUnless(np, "numpy missing")
    def test_letterbox(self):
        class CV2:  # nearest-neighbour stand-in for cv2.resize
            INTER_LINEAR = 1

            @staticmethod
            def resize(a, size, interpolation=None):
                w, h = size
                return a[(np.arange(h) * a.shape[0] // h)[:, None], (np.arange(w) * a.shape[1] // w)[None, :]]
        rgb = np.full((300, 1280, 3), 200, np.uint8)
        out, s = pets.letterbox(rgb, CV2)
        self.assertEqual((out.shape, s), ((640, 640, 3), 0.5))
        self.assertEqual((float(out[0, 0, 0]), float(out[149, 639, 2]), float(out[150, 0, 0])), (200.0, 200.0, 114.0))


class Preprocessing(unittest.TestCase):
    @unittest.skipUnless(np, "numpy or Pillow missing")
    def test_dinov2_and_siglip_inputs(self):
        grey = np.full((300, 300, 3), 128, np.uint8)
        x = pets.dino_input(grey)
        self.assertEqual((x.shape, x.dtype), ((1, 3, 224, 224), np.float32))
        for ch in range(3):
            self.assertAlmostEqual(float(x[0, ch].mean()), (128 / 255 - pets.DINO_MEAN[ch]) / pets.DINO_STD[ch], 4)
        wide = np.zeros((100, 400, 3), np.uint8)
        wide[:, 150:250] = 255   # the middle quarter: what a centre crop of the resized image keeps
        x = pets.dino_input(wide)
        self.assertEqual(x.shape, (1, 3, 224, 224))
        self.assertAlmostEqual(float(x[0, 0, 112, 112]), (1 - pets.DINO_MEAN[0]) / pets.DINO_STD[0], 4)
        s = pets.siglip_input(grey)
        self.assertEqual(s.shape, (1, 3, 224, 224))
        self.assertAlmostEqual(float(s.mean()), (128 / 255 - 0.5) / 0.5, 4)

    @unittest.skipUnless(np, "numpy missing")
    def test_embedder_reads_the_cls_token(self):
        class Sess:
            def __init__(self, out):
                self.out = out

            def run(self, names, feeds):
                return [self.out]
        e = pets.OnnxEmbedder.__new__(pets.OnnxEmbedder)
        e.sess, e.input, e.prep, e.output = Sess(np.arange(2 * 257 * 4).reshape(2, 257, 4)), "pixel_values", \
            (lambda c: c), "last_hidden_state"
        e.cls = True
        self.assertEqual(e.embed(None).tolist(), [0, 1, 2, 3])  # batch 0, token 0 (CLS)
        e.sess, e.cls = Sess(np.arange(8).reshape(1, 8)), False
        self.assertEqual(e.embed(None).tolist(), list(range(8)))


@unittest.skipUnless(np, "numpy or Pillow missing (python3-numpy, python3-pillow)")
class PetsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pets-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state, mode=0o700)
        env = {"HOME": self.home, "MEDIA_INTAKE_STATE": self.state, "PHOTO_LIBRARY": self.lib,
               "PERSONAL_FOOTAGE": os.path.join(self.home, "Videos/personal"),
               "MEDIA_INTAKE_CONFIG": support.write_config(os.path.join(self.home, "Videos/demo-footage"))}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.world = World(seed=11)
        self.spec = pet_spec(self.world)
        self.c = recog.connect()
        self.addCleanup(lambda: self.c.close())
        self.side = FakeSidecars()

    def photo(self, rel, scene):
        return image(os.path.join(self.lib, rel), scene)

    def scan(self, paths, **kw):
        return recog.scan(self.c, self.spec, paths, log=lambda *a, **k: None, state=self.state, **kw)

    def tag(self):
        return recog.write_tags(self.c, self.spec, reader=self.side.read, writer=self.side.write, state=self.state)

    def det_of(self, path):
        return [dict(r) for r in self.c.execute("SELECT * FROM detections WHERE sha=? ORDER BY x", (sha_of(path),))]


class Pets(PetsBase):
    def test_species_photo_rule_suggestions_and_tags(self):
        both = self.world.scene(1, ("rex", "dog"), ("tom", "cat"))
        twins = self.world.scene(2, ("ace", "dog"), ("ace-twin", "dog"))  # look-alikes, one photo
        self.world.base["ace-twin"] = self.world.vec("ace")  # same vector: only the one-photo rule splits them
        rex = self.world.scene(3, ("rex", "dog"))
        catdog = self.world.scene(4, ("tom", "dog"))  # a cat the detector called a dog, once
        p = [self.photo(f"2026/2026-11/{n}.png", s) for n, s in (("a", both), ("b", twins), ("c", rex), ("d", rex),
                                                                  ("e", catdog))]
        cnt = self.scan(p)
        self.assertEqual((cnt["photos scanned"], cnt["pets found"]), (5, 7))
        vecs = self.c.execute("SELECT model, COUNT(*) n FROM embeddings GROUP BY model").fetchall()
        self.assertEqual({r["model"]: r["n"] for r in vecs}, {pets.MODEL: 7, pets.SIGLIP_MODEL: 7},
                         "the SigLIP crop vector is stored beside DINOv2's, for eval")
        recog.recluster(self.c, self.spec)
        species = {}
        for r in self.c.execute("SELECT cluster, species FROM detections"):
            species.setdefault(r["cluster"], set()).add(r["species"])
        self.assertTrue(all(len(v) == 1 for v in species.values()), "a cat never shares a cluster with a dog")
        tom = [r["cluster"] for r in self.c.execute("SELECT cluster FROM detections WHERE sha IN (?, ?) AND x > 0.2 "
                                                    "OR sha=?", (sha_of(p[0]), sha_of(p[0]), sha_of(p[4])))]
        self.assertEqual(len(set(tom)), 2, "the same animal as a cat and as a dog: two clusters")
        for f in p[:2]:
            ds = self.det_of(f)
            self.assertNotEqual(ds[0]["cluster"], ds[1]["cluster"], "two pets in one photo are never one pet")
        rex_cl = self.det_of(p[2])[0]["cluster"]
        recog.name_cluster(self.c, "pet", rex_cl, "Rex")
        new = self.photo("2026/2026-11/f.png", rex)
        self.scan([new])
        recog.recluster(self.c, self.spec)
        d = self.det_of(new)[0]
        self.assertEqual((d["name"], d["how"]), ("Rex", "suggested"), "a later match is only a suggestion")
        self.tag()
        self.assertNotIn(new, self.side.tags, "a suggestion is never a tag")
        self.assertIn("Pets|Rex", self.side.tags[p[2]])
        recog.confirm(self.c, "pet", [d["id"]])
        self.tag()
        self.assertEqual(self.side.tags[new], ["Pets|Rex"])
        self.assertFalse([t for v in self.side.tags.values() for t in v if t.startswith("People|")])

    def test_video_needs_min_frames(self):
        rex = self.world.scene(5, ("rex", "dog"))
        empty = self.world.scene(6)
        frame = lambda s: np.full((60, 80, 3), s, np.uint8)  # noqa: E731  (pixel 0,0 is the scene number)
        two = write(os.path.join(self.lib, "2026/2026-11/two.mp4"), b"clip two")  # the stub below is the frames
        three = write(os.path.join(self.lib, "2026/2026-11/three.mp4"), b"clip three")
        clips = {two: [rex, rex, empty, empty], three: [rex, empty, rex, rex]}
        self.scan([two, three], frames=lambda path: [(float(i), frame(s)) for i, s in enumerate(clips[path])])
        recog.recluster(self.c, self.spec)
        cl = {r["cluster"] for r in self.c.execute("SELECT cluster FROM detections")}
        self.assertEqual(len(cl), 1, "one pet across frames and videos")
        recog.name_cluster(self.c, "pet", cl.pop(), "Rex")
        self.tag()
        self.assertEqual(self.side.tags.get(three), ["Pets|Rex"])
        self.assertNotIn(two, self.side.tags, "in 2 of its frames: under pet_min_frames (3)")
        self.spec.min_frames = 2
        self.tag()
        self.assertEqual(self.side.tags.get(two), ["Pets|Rex"])

    def test_eval_truth_and_both_models(self):
        rex, tom = self.world.scene(7, ("rex", "dog")), self.world.scene(8, ("tom", "cat"))
        fido = self.world.scene(9, ("fido", "dog"))
        p = {n: self.photo(f"2026/2026-11/{n}.png", s) for n, s in
             (("r1", rex), ("r2", rex), ("r3", rex), ("t1", tom), ("t2", tom), ("f1", fido), ("f2", fido))}
        self.scan(list(p.values()))
        recog.recluster(self.c, self.spec)
        recog.name_cluster(self.c, "pet", self.det_of(p["r1"])[0]["cluster"], "Rex")   # your names in the store
        self.side.tags[p["t1"]] = ["Pets|Tom"]               # your own tag, by hand
        self.side.tags[p["t2"]] = ["Calibrate|pets|Tom"]
        self.side.tags[p["f1"]] = ["Pets|Fido"]
        self.side.tags[p["f2"]] = ["Pets|Fido", "Pets|Rex"]  # two names on one pet: ambiguous, left out
        labels = pets.truth_labels(self.c, self.spec, reader=self.side.read)
        self.assertEqual(sorted(labels.values()), ["Fido", "Rex", "Rex", "Rex", "Tom", "Tom"])
        d = self.det_of(p["r1"])[0]["id"]
        self.side.tags[p["r1"]] = ["Pets|Max"]  # disagrees with your name in the store: left out
        self.assertNotIn(d, pets.truth_labels(self.c, self.spec, reader=self.side.read))
        before = self.c.execute("SELECT id, cluster, name, how FROM detections ORDER BY id").fetchall()
        res = recog.evaluate(self.c, self.spec, labels)
        self.assertEqual(sorted(res), sorted([pets.MODEL, pets.SIGLIP_MODEL]))
        dino = res[pets.MODEL]
        self.assertEqual((dino["labelled"], dino["individuals"], dino["roc_auc"], dino["mixed_clusters"]),
                         (6, 3, 1.0, 0))
        self.assertEqual(dino["link"], pets.LINK)
        self.assertEqual(dino["clusters_per_individual"], {"Fido": 1, "Rex": 1, "Tom": 1})
        self.assertEqual(res[pets.SIGLIP_MODEL]["link"], res[pets.SIGLIP_MODEL]["eer_threshold"],
                         "the other model is clustered at its own threshold")
        self.assertEqual(before, self.c.execute("SELECT id, cluster, name, how FROM detections ORDER BY id").fetchall(),
                         "eval changes nothing")

    def test_eval_bounds_pairs_and_eer(self):
        rng = np.random.default_rng(3)
        vecs = {n: rng.normal(size=DIM) for n in ("a", "b")}
        ids = []
        for i in range(2 * recog.EVAL_MAX + 10):
            n = "a" if i % 2 else "b"
            cur = self.c.execute("INSERT INTO detections(sha, kind, species, x, y, w, h, score) "
                                 "VALUES (?, 'pet', 'dog', 0, 0, 1, 1, 1)", (f"{i:064x}",))
            v = recog.unit(vecs[n] + rng.normal(scale=1.5, size=DIM))
            self.c.execute("INSERT INTO embeddings(det, model, vec) VALUES (?, ?, ?)",
                           (cur.lastrowid, pets.MODEL, v.tobytes()))
            ids.append((cur.lastrowid, n.upper()))
        res = recog.evaluate(self.c, self.spec, dict(ids), models=[pets.MODEL])[pets.MODEL]
        self.assertEqual((res["labelled"], res["scored"]), (2 * recog.EVAL_MAX + 10, 2 * recog.EVAL_MAX))
        self.assertGreater(res["roc_auc"], 0.5)
        self.assertLess(res["eer"], 0.5)
        self.assertLessEqual(res["median_same"], 1.0)

    def test_eer_matches_a_scan_of_every_threshold(self):
        """recog.eer equals the plain loop over thresholds (lowest first, first minimum wins), ties included."""
        def loop(pos, neg):
            best = (2.0, None, None)
            for th in np.unique(np.concatenate([pos, neg])):
                frr, far = float((pos < th).mean()), float((neg >= th).mean())
                if abs(frr - far) < best[0]:
                    best = (abs(frr - far), (frr + far) / 2, float(th))
            return round(best[1], 4), round(best[2], 4)
        rng = np.random.default_rng(5)
        cases = [([0.9, 0.8, 0.7, 0.6], [0.65, 0.5, 0.4, 0.3]),   # one crossing
                 ([0.5, 0.5, 0.7], [0.5, 0.2, 0.5]),              # ties across same and different pairs
                 ([0.9, 0.3], [0.6, 0.1]),                        # the same |frr - far| at two thresholds
                 (rng.normal(0.7, 0.1, 40), rng.normal(0.4, 0.1, 61))]
        for pos, neg in cases:
            pos, neg = np.asarray(pos, np.float32), np.asarray(neg, np.float32)
            with self.subTest(pos=pos[:4].tolist()):
                self.assertEqual(recog.eer(pos, neg), loop(pos, neg))

    def test_cli_off_and_help(self):
        with self.assertRaises(SystemExit) as e:
            pets.main(["clusters"])
        self.assertIn("media-intake pets is off", str(e.exception))
        self.assertIn("bash setup.sh --recognition", str(e.exception))
        out = recog.build_parser(pets.spec()).format_help()
        self.assertIn("\nTouches: ", out)
        self.assertIn("\nDeletes: ", out)
        self.assertIn("Pets|<Name>", out)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 80, line)
        self.assertEqual(recog.build_parser(pets.spec()).parse_args(["pets"]).cmd, "pets",
                         "the names list is `pets pets`, as `people people`")


@unittest.skipUnless(os.path.exists(REAL_VENV_PY), "setup.sh not run (no venv)")
class RealModels(unittest.TestCase):
    """YOLOX + DINOv2 (and SigLIP when installed) through the venv, on synthetic images: the sha256 checks, the
    models load and run. With PETS_SAMPLE set to a public photo of two cats, both are found."""

    def test_engine_runs(self):
        missing = [p for _, p, _ in pets.model_files(REAL_STATE) if not os.path.isfile(p)]
        if missing:
            self.skipTest("pets recognition models not installed (bash setup.sh --recognition)")
        d = tempfile.mkdtemp(prefix="pets-real-")
        self.addCleanup(shutil.rmtree, d, True)
        state = os.path.join(d, "state")
        os.makedirs(state, mode=0o700)
        os.symlink(os.path.join(REAL_STATE, "models"), os.path.join(state, "models"))  # read-only use
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import numpy as np, pets, recog\n"
                "det, embs = pets.engine()\n"
                "grey = np.full((480, 640, 3), 128, np.uint8)\n"
                "print(len(det.detect(grey)), len(embs[pets.MODEL].embed(grey)), pets.SIGLIP_MODEL in embs)\n"
                "if len(sys.argv) > 2:\n"
                "    rgb = recog.load_rgb(sys.argv[2], pets.MAX_SIDE)\n"
                "    found = det.detect(rgb)\n"
                "    v = [recog.unit(embs[pets.MODEL].embed(recog.square_crop(rgb, f[0]))) for f in found]\n"
                "    print(' '.join(sorted(f[2] for f in found)), round(float(v[0] @ v[1]), 3) if len(v) == 2 else '')\n")
        sample = os.environ.get("PETS_SAMPLE")
        r = subprocess.run([REAL_VENV_PY, "-I", "-c", code, support.PKG, *([sample] if sample else [])],
                           capture_output=True, text=True, env=dict(os.environ, HOME=d, MEDIA_INTAKE_STATE=state),
                           timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["0", "384", "True"])
        if sample:
            kinds, cos = lines[1].rsplit(" ", 1)
            self.assertEqual(kinds, "cat cat")
            self.assertLess(float(cos), pets.LINK, "two different cats stay apart at the link threshold")


if __name__ == "__main__":
    unittest.main()
