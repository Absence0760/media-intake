#!/usr/bin/env python3
"""semantic.py tests: synthetic images and videos in throwaway dirs, the real SigLIP models read-only.

  python3 -I -m unittest discover -s tests -p test_semantic.py

Skips when setup.sh hasn't run (no venv or vision model); the text-search cases
also skip when the text model isn't in ~/.cache/media-intake-build. The UI cases
(progress, summary, output formats, missing index/model messages) use fakes in
place of the models and need only numpy, as do the query prompt-ensemble cases
(TextEnsemble: a fake tokenizer and text model).
"""
import contextlib, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile, time, types, unittest
import unittest.mock as mock

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


try:
    import numpy
except ImportError:
    numpy = None

import label  # noqa: E402
import meter  # noqa: E402
import semantic  # noqa: E402

GALLERY_PARSE = r're.match(r"^\s*(-?\d+(?:\.\d+)?)\s+(/.*)$", ln)'  # gallery.py, App.semantic


class Clock:
    def __init__(self, step=0.0):
        self.t, self.step = 100.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class Tty(io.StringIO):
    def isatty(self):
        return True


class Meter(unittest.TestCase):
    """meter.py: cadence, content, terminal rewrite."""

    def env(self, every, secs):
        return mock.patch.dict(os.environ, {"MEDIA_INTAKE_PROGRESS_EVERY": str(every),
                                            "MEDIA_INTAKE_PROGRESS_SECS": str(secs)})

    def test_every_n_items(self):
        out, clock = io.StringIO(), Clock()
        with self.env(3, "inf"):
            m = meter.Meter("index", 7, out=out, tty=False, clock=clock)
        for i in range(1, 8):
            clock.t += 2.0  # 0.5 files/s
            m.update(i)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines, ["index: 3/7 files (42%), 2.0 s per file, about 8s left",
                                 "index: 6/7 files (85%), 2.0 s per file, about 2s left"])

    def test_every_n_seconds(self):
        out, clock = io.StringIO(), Clock()
        with self.env(1000, 5):
            m = meter.Meter("vlm-trash", 100, out=out, tty=False, clock=clock)
        for i in range(1, 11):
            clock.t += 2.0
            m.update(i)
        lines = out.getvalue().splitlines()
        self.assertEqual([l.split(",")[0] for l in lines], ["vlm-trash: 3/100 files (3%)", "vlm-trash: 6/100 files (6%)",
                                                            "vlm-trash: 9/100 files (9%)"])

    def test_rate_and_time_left(self):
        out, clock = io.StringIO(), Clock()
        with self.env(10, "inf"):
            m = meter.Meter("index", 4000, out=out, tty=False, clock=clock)
        clock.t += 4.0
        m.update(10)  # 2.5 files/s, 3990 left: 1596 s
        self.assertEqual(out.getvalue(), "index: 10/4,000 files (0%), 2.5 files/s, about 26m 36s left\n")

    def test_terminal_rewrites_one_line(self):
        out, clock = Tty(), Clock()
        with self.env(1, "inf"):
            m = meter.Meter("index", 3, out=out, clock=clock)  # tty detected from the stream
        self.assertTrue(m.tty)
        m.update(1)
        m.update(2)
        self.assertNotIn("\n", out.getvalue())
        self.assertEqual(out.getvalue().count("\r"), 2)
        m.say("index: done")  # overwrites the progress line, padded over its tail, then ends the line
        last = out.getvalue().split("\r")[-1]
        self.assertEqual((last.rstrip(), last.endswith("\n"), len(last)), ("index: done", True,
                                                                           len(out.getvalue().split("\r")[-2]) + 1))
        m.update(3)
        m.close()
        self.assertTrue(out.getvalue().endswith("\n"))
        m.close()  # nothing pending: no extra newline
        self.assertFalse(out.getvalue().endswith("\n\n"))

    def test_default_terminal_cadence_is_faster(self):
        with mock.patch.dict(os.environ, {"MEDIA_INTAKE_PROGRESS_EVERY": "", "MEDIA_INTAKE_PROGRESS_SECS": ""}):
            self.assertEqual(meter.Meter("x", 1, out=io.StringIO(), tty=True).secs, meter.TTY_SECS)
            self.assertEqual(meter.Meter("x", 1, out=io.StringIO(), tty=False).secs, meter.SECS)
            self.assertEqual(meter.Meter("x", 1, out=io.StringIO(), tty=False, every=1000).every, 1000)

    def test_duration(self):
        self.assertEqual([meter.duration(x) for x in (0, 45.4, 60, 1596, 3600, 3725)],
                         ["0s", "45s", "1m 00s", "26m 36s", "1h 00m", "1h 02m"])


@unittest.skipUnless(numpy, "numpy missing")
class Ui(unittest.TestCase):
    """Index progress and summary, search/similar output, missing index/model messages, with fakes for
    the encoders: nothing loads a model."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="semantic-ui-test-")
        self.state = os.path.join(self.d, "state")
        self.lib = os.path.join(self.d, "Pictures", "library")
        self.patches = [mock.patch.object(semantic, "STATE", self.state),
                        mock.patch.object(semantic, "STORE", os.path.join(self.state, "embeddings.npz")),
                        mock.patch.object(semantic, "BUILD", os.path.join(self.d, "nobuild")),
                        mock.patch.object(label, "flagged", lambda state: {}),
                        mock.patch.object(label, "refused", lambda paths: []),
                        mock.patch.dict(sys.modules, {"onnxruntime": types.ModuleType("onnxruntime")}),
                        mock.patch.dict(os.environ, {"MEDIA_INTAKE_PROGRESS_EVERY": "2",
                                                     "MEDIA_INTAKE_PROGRESS_SECS": "inf"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def file(self, rel, content):
        p = os.path.join(self.lib, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    @staticmethod
    def vec(*xs):
        v = numpy.zeros(768, numpy.float32)
        v[:len(xs)] = xs
        return v / numpy.linalg.norm(v)

    def run_main(self, *argv, stdout=None):
        out, err = stdout or io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                semantic.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    # ---- index
    def index_fixture(self):
        names = {"secretname-a.jpg": b"a", "secretname-b.jpg": b"b", "secretname-copy.jpg": b"a",
                 "secretname-bad.jpg": b"bad", "secretname-c.jpg": b"c"}
        paths = [self.file(os.path.join("2026", "2026-11", n), c) for n, c in names.items()]

        def embed(sess, path, np):
            with open(path, "rb") as f:
                c = f.read()
            return None if c == b"bad" else self.vec(*(c[0], 1.0))
        return paths, embed

    def index(self, paths, embed):
        with mock.patch.object(semantic, "library_files", lambda: sorted(paths)), \
                mock.patch.object(semantic, "vision", lambda ort: "session"), \
                mock.patch.object(semantic, "embed_file", embed):
            return self.run_main("index")

    def test_index_progress_and_summary(self):
        paths, embed = self.index_fixture()
        code, out, err = self.index(paths, embed)
        self.assertEqual(code, 0)
        c = json.loads(out.strip().splitlines()[-1])  # the machine form, unchanged
        self.assertEqual((c["files"], c["vectors"], c["new_images"], c["undecodable"]), (4, 3, 3, 1))
        lines = err.splitlines()
        self.assertNotIn("secretname", err)  # never a file name
        self.assertTrue(all(l.startswith("index") for l in lines), lines)
        self.assertEqual(lines[2], "index: 5 files to embed, 0 already indexed")  # after the hashing lines
        prog = [l for l in lines if re.match(r"index: \d+/5 files", l)]
        head = lambda l: re.match(r"^.*? \d+/\d+ files", l).group(0)  # noqa: E731
        self.assertEqual([head(l) for l in prog], ["index: 2/5 files", "index: 4/5 files"])
        self.assertEqual([head(l) for l in lines if l.startswith("index (checking)")],
                         ["index (checking): 2/5 files", "index (checking): 4/5 files"])
        self.assertRegex(lines[-1], r"^index: indexed 3 new \(3 photos, 0 videos\), 0 unchanged, 1 failed "
                                    r"\(undecodable\), 1 copies of new files; 0 refused, 0 dropped; took \d+s$")
        # again: the undecodable file is tried again, the rest is unchanged
        code, out, err = self.index(paths, embed)
        self.assertIn("index: 1 files to embed, 4 already indexed", err)
        self.assertRegex(err.splitlines()[-1], r"^index: indexed 0 new \(0 photos, 0 videos\), 4 unchanged, 1 failed")

    def test_index_on_a_terminal_ends_with_next_not_json(self):
        """A person at a terminal gets the summary and a next: line; the JSON line is for the queue's log."""
        paths, embed = self.index_fixture()
        with mock.patch.object(semantic, "library_files", lambda: sorted(paths)), \
                mock.patch.object(semantic, "vision", lambda ort: "session"), \
                mock.patch.object(semantic, "embed_file", embed):
            code, out, err = self.run_main("index", stdout=Tty())
        self.assertEqual(code, 0)
        self.assertEqual(out, semantic.INDEX_NEXT + "\n")
        self.assertEqual(out, 'next: media-intake search "a photo of a dog" -n 20\n')
        self.assertNotIn("{", out)
        self.assertRegex(err.splitlines()[-1], r"^index: indexed 3 new")

    def test_help_is_plain_and_names_media_intake(self):
        for cmd, words in (("index", "Build or update the search index"),
                           ("search", 'Find photos and videos that match a description, e.g. "dog on a beach".'),
                           ("similar", "Find photos and videos that look like FILE.")):
            with self.subTest(cmd):
                code, out, err = self.run_main(cmd, "--help")
                self.assertEqual((code, err), (0, ""))
                h = " ".join(out.split())
                self.assertIn(f"usage: media-intake {cmd} ", h)
                self.assertIn(words, h)
                self.assertNotIn("semantic.py", h)
        code, out, _ = self.run_main("search", "--help")
        for opt in ("-n N ", "--videos ", "--photos ", "--project NAME ", "--unused-since Nd ", "--clean "):
            line = next(ln for ln in out.splitlines() if ln.strip().startswith(opt.strip()))
            self.assertGreater(len(line.strip()), len(opt.strip()) + 4, f"{opt} has a help text")

    # ---- search / similar output
    def store(self):
        a = self.file(os.path.join("2026", "2026-11", "IMG_20261108_071502.jpg"), b"1")
        b = self.file(os.path.join("2026", "2026-10", "b.jpg"), b"2")
        c = self.file(os.path.join("misc", "c.jpg"), b"3")
        files = {p: [os.path.getsize(p), 0, s] for p, s in ((a, "sa"), (b, "sb"), (c, "sc"))}
        semantic.save_store(numpy, ["sa", "sb", "sc"],
                            numpy.stack([self.vec(1, 0), self.vec(0.8, 0.6), self.vec(0, 1)]), files)
        return a, b, c

    def test_search_human_list(self):
        a, b, c = self.store()
        with mock.patch.object(semantic, "text_available", lambda: None), \
                mock.patch.object(semantic, "embed_text", lambda q, np, ort: self.vec(1, 0)):
            code, out, err = self.run_main("search", "anything", "--format", "human")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            f"1. 1.00  2026-11-08  {a}",
            f"2. 0.80  2026-10     {b}",
            f"3. 0.00              {c}",
            "3 results, best first (score: cosine, compare within one query)"])
        self.assertEqual(err, "")

    def test_auto_is_human_on_a_terminal_only(self):
        a, b, c = self.store()
        code, out, _ = self.run_main("similar", a, "-n", "1", stdout=Tty())
        self.assertEqual(out.splitlines()[0], f"1. 0.80  2026-10     {b}")
        code, out, err = self.run_main("similar", a, "-n", "1")  # captured, as the gallery runs it
        self.assertEqual(out, f"0.800  {b}\n")

    def test_machine_format_is_what_the_gallery_parses(self):
        a, b, c = self.store()
        with open(os.path.join(os.path.dirname(semantic.__file__), "gallery.py")) as f:
            self.assertIn(GALLERY_PARSE, f.read())  # gallery parsing changed: re-check this format
        for argv in (("similar", a), ("similar", a, "--format", "machine")):
            code, out, err = self.run_main(*argv)
            self.assertEqual(code, 0)
            self.assertEqual(out, f"0.800  {b}\n0.000  {c}\n")  # byte for byte: `%.3f  path`
            self.assertEqual(err, "2 results\n")
            got = [re.match(r"^\s*(-?\d+(?:\.\d+)?)\s+(/.*)$", ln) for ln in out.splitlines()]
            self.assertEqual([(float(m.group(1)), m.group(2)) for m in got], [(0.8, b), (0.0, c)])

    def test_no_results_says_so(self):
        a, b, c = self.store()
        code, out, _ = self.run_main("similar", a, "--videos", "--format", "human")
        self.assertEqual(out, "No results. Try without --project/--unused-since/--clean/--photos/--videos.\n")
        code, out, err = self.run_main("similar", a, "--videos")
        self.assertEqual((out, err), ("", "0 results\n"))

    # ---- missing index or model: one line naming the fix, no traceback
    def test_missing_index_names_the_command(self):
        boom = mock.Mock(side_effect=AssertionError("must not load the text model first"))
        with mock.patch.object(semantic, "embed_text", boom):
            code, out, err = self.run_main("search", "dog")
        self.assertIn("no search index yet", code)
        self.assertIn("media-intake index", code)
        self.assertEqual(len(code.splitlines()), 1)
        f = self.file("x.jpg", b"x")
        code, _, _ = self.run_main("similar", f)
        self.assertIn("media-intake index", code)
        semantic.save_store(numpy, [], numpy.zeros((0, 768), numpy.float32), {})
        code, _, _ = self.run_main("search", "dog")
        self.assertIn("it holds no files", code)

    def test_missing_text_model_names_setup(self):
        self.store()
        code, out, err = self.run_main("search", "dog")
        self.assertIn("only `similar` works", code)
        self.assertIn("bash setup.sh --build-labels", code)
        self.assertEqual(out, "")

    def test_missing_venv_modules_name_setup(self):
        with mock.patch.dict(sys.modules, {"onnxruntime": None}):
            code, _, _ = self.run_main("search", "dog")
        self.assertIn("onnxruntime is missing", code)
        self.assertIn("media-intake search", code)
        self.assertIn("setup.sh", code)

    def test_path_date(self):
        for p, want in (("/l/2026/2026-11/IMG_20261108_071502.jpg", "2026-11-08"), ("/l/2026/2026-11/red.jpg", "2026-11"),
                        ("/f/originals/2026-11-08-race/phone/g.jpg", "2026-11-08"), ("/v/GX010123.MP4", ""),
                        ("/l/2026/2026-13/a.jpg", ""), ("/l/2026-1108.jpg", ""), ("/l/20261340.jpg", "")):
            self.assertEqual(semantic.path_date(p), want, p)


class FakeTokenizer:
    """Stands in for tokenizers.Tokenizer: one id per word (2..51), then </s> (1), as SigLIP's appends it."""
    @classmethod
    def from_file(cls, path):
        return cls()

    def encode(self, text):
        return types.SimpleNamespace(ids=[2 + sum(map(ord, w)) % 50 for w in text.split()] + [1])


class FakeTextSession:
    """Stands in for the SigLIP text model: a bag of token ids per row (padding and </s> left out)."""
    feeds = []

    def __init__(self, path, so=None, providers=None):
        pass

    def get_inputs(self):
        return [types.SimpleNamespace(name="input_ids"), types.SimpleNamespace(name="attention_mask")]

    def run(self, names, feed):
        FakeTextSession.feeds.append(feed)
        out = numpy.zeros((len(feed["input_ids"]), 768), numpy.float32)
        for i, row in enumerate(feed["input_ids"]):
            for t in row:
                if t != semantic.PAD:
                    out[i, t] += 1.0
        return [out]


def fake_vec(text):
    """What FakeTextSession gives for one prompt, unit length."""
    v = numpy.zeros(768, numpy.float32)
    for t in FakeTokenizer().encode(text).ids[:-1]:
        v[t] += 1.0
    return v / numpy.linalg.norm(v)


@unittest.skipIf(numpy is None, "numpy missing")
class TextEnsemble(unittest.TestCase):
    """The query's prompt ensemble, with a fake tokenizer and text model: nothing loads a model."""

    def setUp(self):
        FakeTextSession.feeds = []
        self.ort = types.SimpleNamespace(SessionOptions=types.SimpleNamespace, InferenceSession=FakeTextSession)
        self.patches = [mock.patch.object(semantic, "text_available", lambda: None),
                        mock.patch.dict(sys.modules, {"tokenizers": types.SimpleNamespace(Tokenizer=FakeTokenizer)})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_prompts(self):
        self.assertEqual(semantic.query_prompts("Dog, on a  BEACH!"),
                         ["a photo of dog on a beach", "a picture of dog on a beach", "dog on a beach"])
        # a query already phrased as a template is not wrapped twice
        for q in ("a photo of a dog", "A picture of a dog."):
            self.assertEqual(semantic.query_prompts(q), ["a photo of a dog", "a picture of a dog", "a dog"], q)
        self.assertEqual(semantic.query_prompts("a photo of"), ["a photo of a photo of", "a picture of a photo of",
                                                                "a photo of"])
        self.assertEqual(semantic.query_prompts("Dog!", ensemble=False), ["dog"])
        self.assertEqual(semantic.query_prompts("a photo of a dog", ensemble=False), ["a photo of a dog"])
        self.assertEqual(semantic.query_prompts("!!"), [""])

    def test_ensemble_is_the_renormalised_mean(self):
        v = semantic.embed_text("dog on a beach", numpy, self.ort)
        want = numpy.mean([fake_vec(p) for p in semantic.query_prompts("dog on a beach")], axis=0)
        numpy.testing.assert_allclose(v, want / numpy.linalg.norm(want), rtol=1e-6, atol=1e-7)
        self.assertAlmostEqual(float(numpy.linalg.norm(v)), 1.0, places=6)
        (feed,) = FakeTextSession.feeds  # one batch through the model, a row per prompt, padded to 64
        self.assertEqual(feed["input_ids"].shape, (3, semantic.MAX_LEN))
        self.assertEqual(feed["input_ids"].dtype, numpy.int64)
        self.assertEqual(feed["attention_mask"].shape, (3, semantic.MAX_LEN))
        self.assertEqual(list(feed["input_ids"][2][:5]), FakeTokenizer().encode("dog on a beach").ids)
        self.assertTrue((feed["input_ids"][2][5:] == semantic.PAD).all())

    def test_no_ensemble_is_the_query_alone(self):
        v = semantic.embed_text("Dog on a beach!", numpy, self.ort, ensemble=False)
        numpy.testing.assert_allclose(v, fake_vec("dog on a beach"), rtol=1e-6, atol=1e-7)
        self.assertEqual(FakeTextSession.feeds[0]["input_ids"].shape, (1, semantic.MAX_LEN))
        self.assertFalse(numpy.allclose(v, semantic.embed_text("Dog on a beach!", numpy, self.ort)))

    def test_deterministic(self):
        a = semantic.embed_text("a red square", numpy, self.ort)
        b = semantic.embed_text("a red square", numpy, self.ort)
        self.assertTrue(numpy.array_equal(a, b))

    def test_long_query_keeps_its_end_token(self):
        q = " ".join(f"w{i}" for i in range(100))
        semantic.embed_text(q, numpy, self.ort)
        ids = FakeTextSession.feeds[0]["input_ids"]
        self.assertTrue((ids[:, -1] == 1).all())  # truncated to 64 with </s> last, as the labels

    def test_search_uses_the_ensemble_and_keeps_the_machine_form(self):
        ui = Ui("test_path_date")
        ui.setUp()
        self.addCleanup(ui.tearDown)
        a, b, c = ui.store()
        seen = []
        real = semantic.embed_text

        def spy(q, np, ort, **kw):
            seen.append(kw)
            return real(q, np, ort, **kw)
        with mock.patch.object(semantic, "embed_text", spy), mock.patch.dict(sys.modules, {"onnxruntime": self.ort}):
            outs = [ui.run_main("search", "dog", "--format", "machine"),
                    ui.run_main("search", "dog", "--format", "machine", "--no-ensemble")]
        self.assertEqual(seen, [{}, {"ensemble": False}])  # the default call stays embed_text(q, np, ort)
        for code, out, err in outs:
            self.assertEqual(code, 0)
            self.assertEqual(err, "3 results\n")
            got = [re.match(r"^\s*(-?\d+(?:\.\d+)?)\s+(/.*)$", ln) for ln in out.splitlines()]
            self.assertTrue(all(got), out)
            self.assertEqual(sorted(m.group(2) for m in got), sorted([a, b, c]))
            self.assertTrue(all(re.fullmatch(r"-?\d+\.\d{3}  /.*", ln) for ln in out.splitlines()), out)

if __name__ == "__main__":
    unittest.main(verbosity=1)
