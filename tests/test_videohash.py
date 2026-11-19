#!/usr/bin/env python3
"""Video near-duplicates: videohash.py on ffmpeg-generated clips, and `plan` routing a near-copy to review.

  python3 -I -m unittest discover -s tests -p test_videohash.py

Every clip is an ffmpeg lavfi pattern made here (mandelbrot zooms, so its frames change over time and a
trimmed copy is a real test; testsrc is the different clip). `plan` and `apply` are the real intake.py in a
throwaway HOME with the hermetic config. No real video, library or state folder is touched.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

support.check_hermetic()
import videohash  # noqa: E402  (support put media_intake on sys.path)

INTAKE = os.path.join(support.PKG, "intake.py")
PY = sys.executable


def h264_encoder():
    """The H.264 encoder to make the clips with: libx264, or libopenh264 (Fedora's ffmpeg-free has only
    that one); None without ffmpeg and ffprobe on PATH or without either encoder."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return None
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    return next((e for e in ("libx264", "libopenh264") if f" {e} " in r.stdout), None)


H264 = h264_encoder()
NEEDS = "ffmpeg/ffprobe missing, or this ffmpeg has no H.264 encoder (libx264 / libopenh264)"


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True, stdin=subprocess.DEVNULL)


def clip(path, src="mandelbrot=size=320x180:rate=25", seconds=6, bitrate="1M"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ff("-f", "lavfi", "-i", src, "-t", str(seconds), "-c:v", H264, "-pix_fmt", "yuv420p", "-b:v", bitrate, path)
    return path


def reencode(src, dst, pre=(), post=()):
    """dst: src through the H.264 encoder again; pre goes before -i (a seek), post after it (bitrate, scale,
    length)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    ff(*pre, "-i", src, *post, "-c:v", H264, "-pix_fmt", "yuv420p", dst)
    return dst


def junk(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8)  # an MP4 header, then nothing decodable
    return path


@unittest.skipUnless(H264, NEEDS)
class Hash(unittest.TestCase):
    """video_hash and near on their own."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="videohash-")
        d = lambda n: os.path.join(cls.tmp, n)  # noqa: E731
        cls.a = clip(d("a.mp4"))
        cls.low = reencode(cls.a, d("low.mp4"), post=("-b:v", "80k"))
        cls.small = reencode(cls.a, d("small.mp4"), post=("-vf", "scale=160:90"))
        cls.trim = reencode(cls.a, d("trim.mp4"), pre=("-ss", "1.3"), post=("-t", "3.5"))
        cls.other = clip(d("other.mp4"), src="testsrc=size=320x180:rate=25")
        cls.hashes = {k: videohash.video_hash(getattr(cls, k), work=d("work"))
                      for k in ("a", "low", "small", "trim", "other")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def h(self, k):
        frames, note = self.hashes[k]
        self.assertIsNone(note, k)
        return frames

    def test_every_clip_is_hashed(self):
        for k in self.hashes:
            self.assertGreaterEqual(len(self.h(k)), videohash.MIN_FRAMES, k)

    def test_reencoded_resized_and_trimmed_copies_match(self):
        for k in ("low", "small", "trim"):
            self.assertTrue(videohash.near(self.h("a"), self.h(k)), k)
            self.assertTrue(videohash.near(self.h(k), self.h("a")), f"{k}, either order")

    def test_a_different_clip_does_not_match(self):
        for k in ("a", "low", "small", "trim"):
            self.assertFalse(videohash.near(self.h("other"), self.h(k)), k)

    def test_unreadable_clip_is_recorded_not_unique(self):
        bad = junk(os.path.join(self.tmp, "bad.mp4"))
        frames, note = videohash.video_hash(bad, work=os.path.join(self.tmp, "work"))
        self.assertEqual((frames, note), ([], videohash.UNREADABLE))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "work")) if os.path.isdir(
            os.path.join(self.tmp, "work")) else [], [], "the fallback's frame folder is removed")

    def test_blank_clip_is_too_short_to_compare(self):
        black = clip(os.path.join(self.tmp, "black.mp4"), src="color=c=black:size=320x180:rate=25", seconds=3)
        self.assertEqual(videohash.video_hash(black), ([], videohash.TOO_SHORT))

    def test_frame_dhash(self):
        self.assertIsNone(videohash.frame_dhash([40] * 72), "a flat frame has no hash")
        self.assertIsNone(videohash.frame_dhash([0, 255] * 30), "a short buffer is no frame")
        self.assertEqual(videohash.frame_dhash(list(range(255, 255 - 72 * 3, -3))), (1 << 64) - 1,
                         "each pixel brighter than its right neighbour sets every bit")

    def test_protocol_like_names_are_read_as_files(self):
        """A relative clip path named like an ffmpeg protocol (and in a folder named like one) is passed as
        file:<absolute path>; no argument ffmpeg or ffprobe gets starts with the protocol."""
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)
        rels = ["concat:x.mp4", "http:x.mp4", os.path.join("concat:x.mp4.d", "http:x.mp4")]
        for rel in rels:
            os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
            shutil.copyfile(self.a, rel)
        calls, real = [], videohash.subprocess.run

        def spy(argv, *a, **kw):
            calls.append(list(argv))
            return real(argv, *a, **kw)

        videohash.subprocess.run = spy
        self.addCleanup(setattr, videohash.subprocess, "run", real)
        for rel in rels:
            del calls[:]
            frames, note = videohash.video_hash(rel, work=os.path.join(self.tmp, "work"))
            self.assertIsNone(note, rel)
            self.assertTrue(videohash.near(frames, self.h("a")), rel)
            self.assertEqual([c[0] for c in calls], ["ffprobe", "ffmpeg"], rel)
            for c in calls:
                self.assertIn("file:" + os.path.abspath(rel), c, rel)
                self.assertFalse([x for x in c if x.startswith(("concat:", "http:"))], c)

    def test_near_needs_enough_frames(self):
        f = self.h("a")
        self.assertFalse(videohash.near(f[:videohash.MIN_FRAMES - 1], f))
        self.assertFalse(videohash.near([], f))


@unittest.skipUnless(H264, NEEDS)
class Plan(unittest.TestCase):
    """intake.py plan: a near-copy video goes to review like a near-copy photo; nothing is deleted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="videohash-plan-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.phone = os.path.join(self.tmp, "phone", "Internal storage")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        MEDIA_INTAKE_NO_LOCALSEARCH="1", TZ="UTC")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def P(self, rel):
        return os.path.join(self.phone, rel)

    def intake(self, *args):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def plan(self, batch):
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        out = self.intake("plan", batch)
        with open(os.path.join(self.state, "batches", batch, "plan.json")) as f:
            return {p["rel"]: p for p in json.load(f)}, out

    def test_messaging_copy_goes_to_review_and_unreadable_is_recorded(self):
        cam = clip(self.P("DCIM/Camera/VID_20261101_100000.mp4"))
        reencode(cam, self.P("DCIM/Camera/VID_20261101_100100.mp4"), post=("-b:v", "300k"))
        reencode(cam, self.P("Pictures/WhatsApp/VID-20261101-WA0001.mp4"), pre=("-ss", "0.7"),
                 post=("-vf", "scale=160:90", "-b:v", "100k"))
        clip(self.P("Pictures/WhatsApp/VID-20261101-WA0002.mp4"), src="testsrc=size=320x180:rate=25")
        junk(self.P("Pictures/WhatsApp/VID-20261101-WA0003.mp4"))
        plan, out = self.plan("2026-11-14-vid")
        wa = "Pictures/WhatsApp/VID-20261101-WA0001.mp4"
        self.assertEqual(plan[wa]["action"], "review")
        self.assertEqual(plan[wa]["why"], "near-duplicate of DCIM/Camera/VID_20261101_100000.mp4",
                         "the larger camera original is the one kept")
        self.assertEqual(plan["DCIM/Camera/VID_20261101_100100.mp4"]["action"], "file",
                         "two camera originals are never merged, as with photos")
        self.assertEqual(plan["Pictures/WhatsApp/VID-20261101-WA0002.mp4"]["action"], "file", "a different clip")
        bad = plan["Pictures/WhatsApp/VID-20261101-WA0003.mp4"]
        self.assertEqual((bad["vhash"], bad["vhash_note"]), ([], videohash.UNREADABLE))
        self.assertIn(f"1 videos not checked for near-duplicates ({videohash.UNREADABLE}): "
                      "Pictures/WhatsApp/VID-20261101-WA0003.mp4", out)
        self.assertTrue(all(p["action"] != "delete" for p in plan.values()), "a near-copy is never deleted")

    def test_plan_twice_and_apply_twice_change_nothing(self):
        cam = clip(self.P("DCIM/Camera/VID_20261101_100000.mp4"))
        batch = "2026-11-14-again"
        rel = "DCIM/Camera/VID_20261101_100000.mp4"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        raw = os.path.join(self.state, "batches", batch, "raw", rel)
        before = sha(raw)
        self.assertEqual(before, sha(cam))
        plans = []
        for _ in range(2):
            self.intake("plan", batch)
            with open(os.path.join(self.state, "batches", batch, "plan.json")) as f:
                plans.append(json.load(f))
            self.assertEqual(sha(raw), before, "plan only reads the staged video")
        self.assertEqual(plans[0], plans[1], "a second plan gives the same plan, vhash included")
        self.assertEqual([p["rel"] for p in plans[1]], [rel], "nothing duplicated")
        vh = plans[1][0]["vhash"]
        self.assertGreaterEqual(len(vh), videohash.MIN_FRAMES)
        with open(os.path.join(self.state, "batches", batch, "scan.json"), "w") as f:
            json.dump({}, f)
        indexes = []
        for _ in range(2):
            self.intake("apply", batch, "--allow-missing-detectors")
            with open(os.path.join(self.state, "index.json")) as f:
                indexes.append(json.load(f))
        self.assertEqual(indexes[0], indexes[1], "a second apply adds nothing")
        (lib_path, entry), = indexes[1].items()
        self.assertEqual(entry["vhash"], vh, "the plan's hashes, stored once")
        self.assertEqual(sha(lib_path), before, "the filed video is the staged one, unchanged")

    def test_messaging_copy_of_a_library_video(self):
        cam = clip(self.P("DCIM/Camera/VID_20261101_100000.mp4"))
        self.plan("2026-11-14-first")
        with open(os.path.join(self.state, "batches", "2026-11-14-first", "scan.json"), "w") as f:
            json.dump({}, f)
        self.intake("apply", "2026-11-14-first", "--allow-missing-detectors")
        with open(os.path.join(self.state, "index.json")) as f:
            index = json.load(f)
        (lib_path, entry), = index.items()
        self.assertGreaterEqual(len(entry["vhash"]), videohash.MIN_FRAMES, "apply keeps the video hash")
        wa = "Pictures/WhatsApp/VID-20261102-WA0001.mp4"
        reencode(cam, self.P(wa), post=("-b:v", "120k"))
        os.remove(cam)
        lib_sha = sha(lib_path)
        plan, _ = self.plan("2026-11-14-second")
        self.assertEqual(sha(lib_path), lib_sha, "plan only reads the library video it compares against")
        staged = os.path.join(self.state, "batches", "2026-11-14-second", "raw", wa)
        self.assertEqual(sha(staged), sha(self.P(wa)), "plan only reads the staged copy")
        self.assertEqual((plan[wa]["action"], plan[wa]["why"]), ("review", f"near-duplicate of library {lib_path}"))

    def flag_and_apply(self, batch, rels):
        """apply batch with rels flagged by the scan: they go to quarantine, the rest is filed."""
        with open(os.path.join(self.state, "batches", batch, "scan.json"), "w") as f:
            json.dump({r: ["nudity"] for r in rels}, f)
        self.intake("apply", batch, "--allow-missing-detectors")

    def index(self):
        with open(os.path.join(self.state, "index.json")) as f:
            return json.load(f)

    def test_released_video_keeps_its_hash_and_catches_a_later_copy(self):
        rel = "DCIM/Camera/VID_20261101_100000.mp4"
        cam = clip(self.P(rel))
        plan, _ = self.plan("2026-11-17-first")
        vh = plan[rel]["vhash"]
        self.flag_and_apply("2026-11-17-first", [rel])
        self.assertEqual(self.index(), {}, "a quarantined video is not in the library yet")
        out = self.intake("release", "2026-11-17-first", rel)
        (dst, entry), = self.index().items()
        self.assertIn(f"released -> {dst}", out)
        self.assertTrue(entry["released"])
        self.assertIsNone(entry["dhash"], "videos have no dHash")
        self.assertEqual(entry["vhash"], vh, "release hashes the video as plan did")
        self.assertNotIn("vhash_note", entry)
        # An index written before videohash has video entries with no vhash key: plan reads past them.
        idx = self.index()
        idx[os.path.join(self.home, "Videos/personal/2025/2025-01/old.mp4")] = dict(sha="0" * 64, size=1, dhash=None)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump(idx, f)
        wa = "Pictures/WhatsApp/VID-20261102-WA0001.mp4"
        reencode(cam, self.P(wa), post=("-b:v", "120k"))
        os.remove(cam)
        plan, _ = self.plan("2026-11-17-second")
        self.assertEqual((plan[wa]["action"], plan[wa]["why"]), ("review", f"near-duplicate of library {dst}"))

    def test_unhashable_video_keeps_its_note_when_filed_or_released(self):
        kept, flagged = "Pictures/WhatsApp/VID-20261101-WA0003.mp4", "Pictures/WhatsApp/VID-20261101-WA0004.mp4"
        junk(self.P(kept))
        with open(self.P(kept), "ab") as f:
            f.write(b"filed")  # a different sha256 from the flagged one
        junk(self.P(flagged))
        self.plan("2026-11-17-junk")
        self.flag_and_apply("2026-11-17-junk", [flagged])
        self.intake("release", "2026-11-17-junk", flagged)
        entries = self.index()
        self.assertEqual(len(entries), 2)
        for path, entry in entries.items():
            self.assertEqual((entry["vhash"], entry["vhash_note"]), ([], videohash.UNREADABLE),
                             f"{path}: not hashed is recorded, never read as unique")
        self.assertEqual(sorted(e.get("released", False) for e in entries.values()), [False, True])


if __name__ == "__main__":
    unittest.main()
