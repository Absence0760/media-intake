#!/usr/bin/env python3
"""semantic.py tests: synthetic images and videos in throwaway dirs, the real SigLIP models read-only.

  python3 -I -m unittest discover -s tests -p test_semantic.py

Skips when setup.sh hasn't run (no venv or vision model); the text-search cases
also skip when the text model isn't in ~/.cache/media-intake-build.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
REAL_STATE = os.path.expanduser("~/.local/share/media-intake")
VENV_PY = os.path.join(REAL_STATE, "venv", "bin", "python")
MODELS = os.path.join(REAL_STATE, "models")
HAVE = os.path.exists(VENV_PY) and os.path.isdir(MODELS)
BUILD = os.path.expanduser("~/.cache/media-intake-build")
HAVE_TEXT = os.path.isfile(os.path.join(BUILD, "text_model.onnx"))
SCRIPT = os.path.join(HERE, "semantic.py")


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def shape(path, color, kind="square", seed=0, bg=(235, 235, 235)):
    from PIL import Image, ImageDraw
    import random
    rnd = random.Random(seed)
    im = Image.new("RGB", (640, 480), bg)
    d = ImageDraw.Draw(im)
    x, y, r = 320 + rnd.randint(-60, 60), 240 + rnd.randint(-40, 40), 140 + rnd.randint(-20, 20)
    (d.rectangle if kind == "square" else d.ellipse)([x - r, y - r, x + r, y + r], fill=color)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, quality=92)
    return path


def video(path, color):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=10:duration=6",
                    "-pix_fmt", "yuv420p", path], check=True)
    return path


@unittest.skipUnless(HAVE, "setup.sh not run (no venv/model)")
class Semantic(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="semantic-test-")
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state, mode=0o700)
        os.symlink(MODELS, os.path.join(self.state, "models"))  # read-only use of the real encoder
        self.lib = os.path.join(self.d, "Pictures", "library")
        self.vids = os.path.join(self.d, "Videos", "personal")
        self.foot = os.path.join(self.d, "footage")
        self.env = dict(os.environ, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.foot), HOME=self.d,
                        MEDIA_INTAKE_BUILD=BUILD)
        self.red = [shape(os.path.join(self.lib, "2026", "2026-11", f"red{i}.jpg"), (210, 20, 20), seed=i)
                    for i in range(3)]
        self.blue = [shape(os.path.join(self.lib, "2026", "2026-11", f"blue{i}.jpg"), (20, 40, 210), "circle", seed=i)
                     for i in range(3)]
        self.green = shape(os.path.join(self.foot, "originals", "2026-11-08-race", "phone", "green.jpg"),
                           (20, 170, 40), "circle", seed=9)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def sem(self, *args, ok=True):
        r = subprocess.run([VENV_PY, "-I", SCRIPT, *args], env=self.env, capture_output=True, text=True, timeout=900)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return r

    def index(self):
        return json.loads(self.sem("index").stdout.strip().splitlines()[-1])

    def hits(self, r):
        return [l.split("  ", 1)[1] for l in r.stdout.splitlines() if l.strip()]

    def test_similar_and_incremental(self):
        c = self.index()
        self.assertEqual((c["files"], c["new_images"]), (7, 7))
        print(f"\n[speed] index {c['s_per_image']} s/image", file=sys.stderr)
        self.assertEqual(os.stat(os.path.join(self.state, "embeddings.npz")).st_mode & 0o777, 0o600)
        top = self.hits(self.sem("similar", self.red[0], "-n", "2"))
        self.assertEqual(set(top), set(self.red[1:]))
        top = self.hits(self.sem("similar", self.blue[0], "-n", "2"))
        self.assertEqual(set(top), set(self.blue[1:]))
        c = self.index()
        self.assertEqual((c["new_images"], c["vectors"]), (0, 7))
        new = shape(os.path.join(self.lib, "2026", "2026-11", "red9.jpg"), (200, 30, 25), seed=7)
        shutil.copy(new, os.path.join(self.lib, "2026", "2026-11", "red9-copy.jpg"))  # same sha: one vector
        c = self.index()
        self.assertEqual((c["new_images"], c["files"], c["vectors"]), (1, 9, 8))
        os.remove(self.blue[2])
        c = self.index()
        self.assertEqual((c["files"], c["vectors"], c["dropped"]), (8, 7, 1))
        # --photos/--videos and unindexed FILE
        self.assertEqual(self.hits(self.sem("similar", self.red[0], "--videos")), [])
        outside = shape(os.path.join(self.d, "elsewhere", "r.jpg"), (215, 15, 15), seed=3)
        self.assertIn(self.hits(self.sem("similar", outside, "-n", "1"))[0], self.red + [new, new.replace("red9", "red9-copy")])

    def test_refused_never_indexed(self):
        bad = shape(os.path.join(self.lib, "2026", "2026-11", "flagged.jpg"), (210, 20, 20), seed=5)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"x sha256:{sha(bad)} size:{os.path.getsize(bad)}\n")
        c = self.index()
        self.assertEqual((c["files"], c["refused"]), (7, 1))
        import numpy as np
        z = np.load(os.path.join(self.state, "embeddings.npz"))
        self.assertNotIn(sha(bad), list(z["sha"]))
        self.assertNotIn(bad, json.loads(str(z["files"])))
        self.assertEqual(self.sem("similar", bad, ok=False).returncode, 3)
        q = os.path.join(self.state, "quarantine", "b1", "q.jpg")
        shape(q, (1, 1, 1))
        self.assertEqual(self.sem("similar", q, ok=False).returncode, 3)
        # flagged after indexing: dropped from results at once, and from the store on the next index
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"x sha256:{sha(self.red[1])} size:{os.path.getsize(self.red[1])}\n")
        self.assertNotIn(self.red[1], self.hits(self.sem("similar", self.red[0])))
        c = self.index()
        self.assertEqual(c["vectors"], 6)

    def test_filters_from_sidecars(self):
        if not shutil.which("exiftool"):
            self.skipTest("exiftool missing")
        self.index()

        def tag(p, *ts):
            with open(p + ".xmp", "w") as f:
                f.write("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
                        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'></rdf:RDF></x:xmpmeta>\n"
                        "<?xpacket end='w'?>\n")
            subprocess.run(["exiftool", "-q", "-overwrite_original", *[f"-XMP-lr:HierarchicalSubject+={t}" for t in ts],
                            p + ".xmp"], check=True)
        tag(self.green, "Project|Demo")
        tag(self.red[1], "Project|Demo", "Used|" + time.strftime("%Y-%m-%d") + "-today-post")
        tag(self.red[2], "Project|Demo", "Used|2020-01-01-old-post")
        tag(self.blue[1], "Project|Demo", "Review|child-in-shot")
        got = set(self.hits(self.sem("similar", self.red[0], "--project", "demo", "-n", "50")))
        self.assertEqual(got, {self.green, self.red[1], self.red[2], self.blue[1]})
        got = set(self.hits(self.sem("similar", self.red[0], "--project", "demo", "--unused-since", "30d",
                                     "--clean", "-n", "50")))
        self.assertEqual(got, {self.green, self.red[2]})

    def test_video_mean_embedding(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg missing")
        v = video(os.path.join(self.vids, "2026", "2026-11", "red.mp4"), "0xD21414")
        video(os.path.join(self.vids, "2026", "2026-11", "blue.mp4"), "0x1428D2")
        c = self.index()
        self.assertEqual(c["new_videos"], 2)
        print(f"\n[speed] index {c['s_per_video']} s/video (6 s clip)", file=sys.stderr)
        top = self.hits(self.sem("similar", self.red[0], "--videos", "-n", "1"))
        self.assertEqual(top, [v])

    @unittest.skipUnless(HAVE_TEXT, "SigLIP text model not in the build cache")
    def test_text_search(self):
        self.index()
        t = time.monotonic()
        r = self.sem("search", "a red square", "-n", "7")
        dt = time.monotonic() - t
        print(f"\n[speed] search {dt:.2f} s (text model load + query)", file=sys.stderr)
        top = self.hits(r)
        self.assertEqual(set(top[:3]), set(self.red))
        top = self.hits(self.sem("search", "a blue circle", "-n", "3"))
        self.assertEqual(set(top), set(self.blue))
        top = self.hits(self.sem("search", "a green circle", "--project", "demo"))
        self.assertEqual(top, [])  # no sidecar -> no project
        self.assertEqual(self.hits(self.sem("search", "a green circle", "-n", "1")), [self.green])

    def test_no_text_model_says_so(self):
        self.env["MEDIA_INTAKE_BUILD"] = os.path.join(self.d, "nobuild")
        self.index()
        r = self.sem("search", "red", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only `similar` works", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
