#!/usr/bin/env python3
"""The video frame sampler (frames.py) on ffmpeg-generated clips.

  python3 -I -m unittest discover -s tests -p test_frames.py

Every clip is an ffmpeg lavfi pattern made here in a throwaway folder: a static clip (SMPTE bars), a clip with
hard cuts at 3, 6 and 9 s, one that opens on 1 s of black, slow-changing testsrc, red-over-blue clips carrying a
display rotation, and an HEVC clip (VA-API's hevc_vaapi or libx265; those cases skip, naming the reason, when
this machine has neither, or no way to decode HEVC). The state folder and the cache are temp folders patched
into frames; no real video, library or state folder is touched.
"""
import contextlib, glob, hashlib, io, json, os, shutil, stat, subprocess, sys, tempfile, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

support.check_hermetic()
import frames  # noqa: E402  (support put media_intake on sys.path)


def encoders():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout


ENC = encoders()
H264 = next((e for e in ("libx264", "libopenh264") if f" {e} " in ENC), None)
NEEDS = "ffmpeg/ffprobe missing, or this ffmpeg has no H.264 encoder (libx264 / libopenh264)"


def hevc_encoder():
    """(encode args, input args) to make an HEVC clip: libx265, else ffmpeg's VA-API encoder on the first render
    node that takes a one-frame test; None when neither works here (Fedora's ffmpeg-free has no libx265)."""
    if " libx265 " in ENC:
        return ["-c:v", "libx265", "-x265-params", "log-level=none", "-pix_fmt", "yuv420p"], []
    if " hevc_vaapi " in ENC:
        for node in sorted(glob.glob("/dev/dri/renderD*")):
            pre, enc = ["-vaapi_device", node], ["-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi"]
            if subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *pre, "-f", "lavfi", "-i",
                               "testsrc=size=320x240:rate=10:duration=0.1", *enc, "-f", "null", "-"],
                              capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
                return enc, pre
    return None


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True, stdin=subprocess.DEVNULL)


def h264(path, graph, *extra):
    ff("-filter_complex", graph, *extra, "-c:v", H264, "-pix_fmt", "yuv420p", "-b:v", "2M", path)
    return path


def rotated(src, dst, ccw):
    """dst: src remuxed (nothing re-encoded) with a display matrix turning it ccw degrees counter-clockwise."""
    ff("-display_rotation", str(ccw), "-i", src, "-c", "copy", dst)
    return dst


CUTS = (3.0, 6.0, 9.0)
CUT_GRAPH = ("testsrc=d=3:s=640x360:r=30[a];smptebars=d=3:s=640x360:r=30[b];"
             "mandelbrot=s=640x360:r=30,trim=duration=3[c];"
             "color=c=0x205080:s=640x360:r=30:d=3,noise=alls=40:allf=t[d];[a][b][c][d]concat=n=4:v=1:a=0")
RED_BLUE = "color=red:s=320x90:d=2:r=30[a];color=blue:s=320x90:d=2:r=30[b];[a][b]vstack"  # red on top


def red_side(rgb):
    """Which edge of an upright frame is red ("top", "bottom", "left", "right")."""
    h, w, _ = rgb.shape
    parts = {"top": rgb[: h // 4], "bottom": rgb[-h // 4:], "left": rgb[:, : w // 4], "right": rgb[:, -w // 4:]}
    return max(parts, key=lambda k: float(parts[k][..., 0].mean()) - float(parts[k][..., 2].mean()))


@unittest.skipUnless(H264, NEEDS)
class FramesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="frames-test-")
        d = lambda n: os.path.join(cls.dir, n)  # noqa: E731
        cls.static = h264(d("static.mp4"), "smptebars=d=20:s=640x360:r=30")
        cls.cuts = h264(d("cuts.mp4"), CUT_GRAPH)
        cls.slow = h264(d("slow.mp4"), "testsrc=d=20:s=640x360:r=30")
        cls.long = h264(d("long.mp4"), "testsrc=d=60:s=640x360:r=30")
        cls.dark = h264(d("dark.mp4"), "color=black:s=640x360:r=30:d=1[a];testsrc=d=3:s=640x360:r=30[b];"
                                       "[a][b]concat=n=2:v=1:a=0")
        cls.big = h264(d("big.mp4"), "testsrc=d=1:s=1920x1080:r=30")
        cls.small = h264(d("small.mp4"), "testsrc=d=1:s=320x240:r=30")
        flat = h264(d("redblue.mp4"), RED_BLUE)
        cls.ccw90 = rotated(flat, d("ccw90.mp4"), 90)  # shown turned 90 deg counter-clockwise: red on the left
        cls.cw90 = rotated(flat, d("cw90.mp4"), -90)  # a phone's portrait clip: red on the right

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="frames-case-")
        self.state = os.path.join(self.tmp, "state")
        self.cache = os.path.join(self.tmp, "cache", "media-intake")
        os.makedirs(self.state)
        for p in (mock.patch.object(frames, "STATE", self.state), mock.patch.object(frames, "CACHE", self.cache)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def picks(self, path, **kw):
        return frames.sample(path, **kw).plan()

    def leftovers(self):
        return glob.glob(os.path.join(self.cache, ".frames-*"))

    # ------------------------------------------------------------ info
    def test_info_reads_size_rate_codec_and_rotation(self):
        i = frames.info(self.cuts)
        self.assertEqual((i["w"], i["h"], i["codec"], i["rotation"]), (640, 360, "h264", 0))
        self.assertAlmostEqual(i["duration"], 12.0, delta=0.1)
        self.assertAlmostEqual(i["fps"], 30.0, delta=0.01)
        self.assertEqual(frames.info(self.ccw90)["rotation"], 270)  # clockwise turn that shows it upright
        self.assertEqual(frames.info(self.cw90)["rotation"], 90)
        with open(os.path.join(self.tmp, "notes.mp4"), "w") as f:
            f.write("not a video")
        with self.assertRaises(frames.FramesError):
            frames.info(f.name)
        with self.assertRaises(frames.FramesError):
            frames.sample(f.name)

    # ------------------------------------------------------------ what is picked
    def test_static_clip_gives_one_frame(self):
        self.assertEqual(self.picks(self.static), [(0.0, "first")])

    def test_hard_cuts_found_near_their_times(self):
        p = self.picks(self.cuts)
        self.assertEqual(p[0], (0.0, "first"))
        cuts = [t for t, why in p if why == "cut"]
        self.assertEqual(len(cuts), len(CUTS), p)
        for got, want in zip(cuts, CUTS):
            self.assertTrue(want <= got <= want + 0.5, (got, want))

    def test_every_seconds_in_a_changing_stretch(self):
        p = self.picks(self.slow)
        self.assertEqual([why for _, why in p], ["first"] + ["every"] * (len(p) - 1))
        self.assertEqual([t for t, _ in p], [0.0, 4.0, 8.0, 12.0, 16.0])
        self.assertEqual(len(self.picks(self.slow, every=10.0)), 2)

    def test_fade_in_skipped_for_the_first_frame(self):
        t, why = self.picks(self.dark)[0]
        self.assertEqual(why, "first")
        self.assertTrue(1.0 <= t <= 1.25, t)

    def test_min_gap_respected(self):
        p = self.picks(self.slow, every=0.25, min_gap=2.0)
        gaps = [b[0] - a[0] for a, b in zip(p, p[1:])]
        self.assertTrue(gaps and min(gaps) >= 2.0, gaps)

    def test_cap_spreads_evenly_and_keeps_the_first(self):
        p = self.picks(self.long, every=1.0, max_frames=5)
        self.assertEqual(len(p), 5)
        self.assertEqual(p[0], (0.0, "first"))
        ts = [t for t, _ in p]
        self.assertTrue(ts[-1] >= 55.0, ts)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        self.assertTrue(max(gaps) - min(gaps) <= 1.0, gaps)
        self.assertEqual(len(self.picks(self.cuts, max_frames=1)), 1)
        self.assertEqual(frames.spread(list(range(10)), 3), [0, 4, 9])

    # ------------------------------------------------------------ the frames handed out
    def test_frames_match_the_plan_scaled_to_the_long_side(self):
        with frames.sample(self.cuts) as s:
            got = list(s)
        self.assertEqual([t for t, _ in got], [t for t, _ in s.picks])
        for _, rgb in got:
            self.assertEqual((rgb.dtype.name, rgb.shape), ("uint8", (360, 640, 3)))  # never scaled up
        # the frame at each cut is from the new scene: the bars' top-left is grey-white, mandelbrot's dark
        self.assertGreater(got[1][1][:40, :40].mean(), 150)
        with frames.sample(self.big) as s:
            self.assertEqual(next(s)[1].shape, (720, 1280, 3))
        with frames.sample(self.small) as s:
            self.assertEqual(next(s)[1].shape, (240, 320, 3))

    def test_seeks_and_a_second_pass_hand_out_the_same_frames(self):
        got = {}
        for span in (0.0, 1e9):  # every pick by a seek; the whole clip decoded again
            with mock.patch.object(frames, "SEEK_SPAN", span), frames.sample(self.cuts) as s:
                got[span] = list(s)
        a, b = got[0.0], got[1e9]
        self.assertEqual([t for t, _ in a], [t for t, _ in b])
        for (t, x), (_, y) in zip(a, b):  # the same scene; the last one is temporal noise, alike only on average
            self.assertLess(frames._mad(x, y) if t < 9 else abs(float(x.mean()) - float(y.mean())) / 255, 0.03, t)

    def test_seek_lands_on_the_analysed_frame_when_video_starts_late(self):
        late = os.path.join(self.tmp, "late.mp4")  # sound from 0 s, the first video frame at 0.5 s
        ff("-itsoffset", "0.5", "-i", self.cuts, "-f", "lavfi", "-i", "sine=duration=13", "-map", "0:v", "-map",
           "1:a", "-c:v", "copy", "-c:a", "aac", late)
        self.assertAlmostEqual(frames._probe(late)["start"], 0.5, delta=0.05)
        got = {}
        for span in (0.0, 1e9):
            with mock.patch.object(frames, "SEEK_SPAN", span), frames.sample(late) as s:
                got[span] = list(s)
        self.assertEqual([t for t, _ in got[0.0]], [t for t, _ in got[1e9]])
        for (t, x), (_, y) in zip(got[0.0], got[1e9]):
            if t < 9:  # the noise scene is alike only on average
                self.assertLess(frames._mad(x, y), 0.03, t)

    def test_decoder_failing_part_way_is_an_error(self):
        with mock.patch.object(frames._Decoder, "finish", return_value=1), \
                self.assertRaisesRegex(frames.FramesError, "no decoder could read"):
            frames.sample(self.cuts).plan()
        for span in (0.0, 1e9):  # a pick the fetch never reaches: fewer frames than planned
            with mock.patch.object(frames, "SEEK_SPAN", span):
                s = frames.sample(self.cuts)
                s.plan()
                s.picks.append((999.0, "every"))
                s._index.append(999 * 4)
                with self.assertRaises(frames.FramesError):
                    list(s)
                self.assertIsNone(s.decoder)

    def test_rotation_applied(self):
        for span in (0.0, 1e9):
            for path, side in ((self.ccw90, "left"), (self.cw90, "right")):
                with mock.patch.object(frames, "SEEK_SPAN", span), frames.sample(path) as s:
                    t, rgb = next(s)
                self.assertEqual(rgb.shape, (320, 180, 3), path)
                self.assertEqual(red_side(rgb), side, path)

    # ------------------------------------------------------------ the decoder's life
    def test_close_kills_and_reaps_the_decoder(self):
        self.enterContext(mock.patch.object(frames, "SEEK_SPAN", 1e9))  # the second pass: one long-running decoder
        s = frames.sample(self.long, every=0.5)
        next(s)
        proc = s.decoder.proc
        self.assertIsNone(proc.poll())  # still decoding the rest
        s.close()
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(s.decoder)
        with self.assertRaises(ChildProcessError):  # reaped: no zombie left behind
            os.waitpid(proc.pid, os.WNOHANG)
        with self.assertRaises(StopIteration):
            next(s)
        with frames.sample(self.long, every=0.5) as s:  # leaving a with block early does the same
            for _ in s:
                proc = s.decoder.proc
                break
        self.assertIsNotNone(proc.returncode)

    def test_timeout_kills_the_decoder(self):
        s = frames.sample(self.cuts)
        with mock.patch.object(frames, "DECODE_TIMEOUT", 0):
            with self.assertRaisesRegex(frames.FramesError, "timed out"):
                s.plan()
        self.assertIsNone(s.decoder)

    def test_ffmpeg_path_writes_nothing(self):
        with frames.sample(self.cuts) as s:
            list(s)
        self.assertEqual(s.backend, "ffmpeg")
        self.assertFalse(os.path.exists(self.cache))

    # ------------------------------------------------------------ refused
    def test_refused_paths_never_decoded(self):
        inside = os.path.join(self.state, "quarantine", "b1")
        os.makedirs(inside)
        q = shutil.copy(self.static, os.path.join(inside, "clip.mp4"))
        erased = shutil.copy(self.cuts, os.path.join(self.tmp, "erased.mp4"))
        with open(erased, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"x sha256:{digest} size:{os.path.getsize(erased)}\n")
        link = os.path.join(self.tmp, "link.mp4")
        os.symlink(q, link)
        with mock.patch.object(frames, "info", side_effect=AssertionError("probed")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("decoded")):
            for p in (q, erased, link):
                with self.assertRaises(frames.Refused, msg=p):
                    frames.sample(p)
        self.assertFalse(frames.refused(self.slow))  # a copy of cuts is in the erase log; cuts is too
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("probed")):
            for p in (q, erased):
                with self.assertRaises(frames.Refused, msg=p):
                    frames.info(p)
        for flag in ("--list", "--info", None):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(frames.main([erased, *([flag] if flag else [])]), 3)
            self.assertNotIn(erased, err.getvalue())
            self.assertEqual(out.getvalue(), "")

    # ------------------------------------------------------------ CLI
    def test_cli_list_prints_timestamps_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(frames.main([self.cuts, "--list"]), 0)
        rows = [r.split("\t") for r in out.getvalue().splitlines()]
        self.assertEqual([r[1] for r in rows], ["first", "cut", "cut", "cut"])
        self.assertTrue(all(float(r[0]) >= 0 for r in rows))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(frames.main([self.cuts, "--info"]), 0)
        self.assertEqual(json.loads(out.getvalue())["codec"], "h264")


@unittest.skipUnless(H264, NEEDS)
class HevcFramesTest(unittest.TestCase):
    """An HEVC clip, decoded by ffmpeg where it can, else by GStreamer's GPU decoder through the work folder."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="frames-hevc-")
        cls.enc = hevc_encoder()
        if not cls.enc:
            return
        enc, pre = cls.enc
        up = ""
        if enc[0] == "-vf":  # VA-API: the upload joins the graph (one filter graph per output)
            up, enc = "," + enc[1], enc[2:]
        flat = os.path.join(cls.dir, "flat.mov")
        ff(*pre, "-filter_complex", RED_BLUE + up, *enc, "-tag:v", "hvc1", flat)
        cls.clip = rotated(flat, os.path.join(cls.dir, "clip.mov"), 90)
        cls.cuts = os.path.join(cls.dir, "cuts.mov")
        ff(*pre, "-filter_complex", CUT_GRAPH + ",format=yuv420p" + up, *enc, "-tag:v", "hvc1", cls.cuts)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        if not self.enc:
            self.skipTest("no HEVC encoder (ffmpeg's libx265, or VA-API's hevc_vaapi) to make a synthetic clip with")
        self.tmp = tempfile.mkdtemp(prefix="frames-hevc-case-")
        self.state = os.path.join(self.tmp, "state")
        self.cache = os.path.join(self.tmp, "cache", "media-intake")
        os.makedirs(self.state)
        for p in (mock.patch.object(frames, "STATE", self.state), mock.patch.object(frames, "CACHE", self.cache)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        try:
            frames.sample(self.clip)
        except frames.FramesError as e:
            self.skipTest(f"no way to decode HEVC here ({e}): ffmpeg has no HEVC decoder and GStreamer no GPU one")

    def gst(self, path, **kw):
        s = frames.sample(path, **kw)
        if "gst" not in s.backends:
            self.skipTest("GStreamer has no GPU HEVC decoder (vah265dec or Vulkan) here")
        s.backends = ["gst"]
        return s

    def test_hevc_upright_and_cuts_found(self):
        with frames.sample(self.clip) as s:
            t, rgb = next(s)
        self.assertEqual(rgb.shape, (320, 180, 3))
        self.assertEqual(red_side(rgb), "left")
        cuts = [t for t, why in frames.sample(self.cuts).plan() if why == "cut"]
        self.assertEqual(len(cuts), len(CUTS), cuts)
        for got, want in zip(cuts, CUTS):
            self.assertTrue(want <= got <= want + 0.5, (got, want))

    def test_gst_work_folder_private_and_removed(self):
        s = self.gst(self.clip)
        t, rgb = next(s)
        self.assertEqual((s.backend, rgb.shape, red_side(rgb)), ("gst", (320, 180, 3), "left"))
        self.assertEqual(stat.S_IMODE(os.stat(s.work).st_mode), 0o700)
        self.assertEqual(os.listdir(s.work), ["in"])
        self.assertTrue(os.path.islink(os.path.join(s.work, "in")))
        list(s)
        self.assertEqual(glob.glob(os.path.join(self.cache, ".frames-*")), [])
        s = self.gst(self.cuts, every=0.5)
        next(s)
        proc, work = s.decoder.proc, s.work
        s.close()
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(os.path.exists(work))

    def test_gst_refused_on_an_unsafe_work_folder(self):
        with mock.patch.object(frames, "CACHE", os.path.join(self.tmp, "a b", "media-intake")):
            s = self.gst(self.clip)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(frames.FramesError):
                s.plan()
            self.assertIn("GStreamer", err.getvalue())
            self.assertEqual(glob.glob(os.path.join(self.tmp, "a b", "media-intake", ".frames-*")), [])


if __name__ == "__main__":
    unittest.main()
