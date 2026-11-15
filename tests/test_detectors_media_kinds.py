#!/usr/bin/env python3
"""Detectors, suggestions and search on photos AND videos: nothing is silently skipped.

  python3 -I -m unittest discover -s tests -p test_detectors_media_kinds.py

scan_sensitive.py (scan.json flags, then quarantine through `apply`), vlm_trash.py
(maybe-trash second opinion), semantic.py (index and search), label.py (labels) and
the plugin after_tag hook, each on photos, videos and mixed batches, plus broken
videos (zero-length, garbage, truncated, audio only) that must fail safe: flagged
"not checked", never filed or reported as clean, never a crash that loses the run.

Synthetic media only (Pillow drawings, ffmpeg colour sources) in a throwaway HOME.
The models are faked the way the other tests fake them: NudeNet by a stand-in that
calls a mostly red picture a hit, Ollama by a loopback stub, SigLIP by a session
that embeds the mean colour. The real models are CI's models.yml job. Assertions
are counts, paths and labels only: no test opens or describes a flagged file's
content beyond the fake detector's own colour test. Cases needing ffmpeg or
exiftool skip when the tool is missing (the CI image has both).
"""
import base64, contextlib, hashlib, http.server, io, json, os, shutil, stat, subprocess, sys, tempfile, threading
import importlib.util, types, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config; puts media_intake/ on sys.path)

from PIL import Image, ImageDraw  # noqa: E402

import scan_sensitive  # noqa: E402

PKG = support.PKG
PY = sys.executable
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
HAVE_EXIFTOOL = bool(shutil.which("exiftool"))
HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_YAML = importlib.util.find_spec("yaml") is not None

RED, BLUE, GREEN = (220, 20, 20), (20, 40, 210), (20, 170, 40)
EMPTY_XMP = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
             "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'></rdf:RDF></x:xmpmeta>\n"
             "<?xpacket end='w'?>\n")


# ---------------------------------------------------------------- synthetic media
def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def photo(path, color, seed=0, dt=None, size=(1200, 900)):
    """A picture mostly of `color`, with a little structure (so it is not "empty")."""
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    w, h = size
    for i in range(6):
        x, y = (i * 97 * (seed + 3)) % w, (i * 61 * (seed + 5)) % h
        d.rectangle([x, y, x + w // 20, y + h // 20], fill=((i * 40) % 255, 200, (seed * 50) % 255))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kw = {"quality": 90}
    if dt:
        ex = Image.Exif()
        ex[0x8769] = {36867: dt}
        kw["exif"] = ex
    if os.path.splitext(path)[1].lower() in (".gpr", ".dng"):
        kw["format"] = "TIFF"  # a raw stand-in: DNG and GPR are TIFF containers Pillow may read
    im.save(path, **kw)
    return path


def _hex(c):
    return "0x%02X%02X%02X" % c


def video(path, color, seconds=4.0, created=None, then=None):
    """An ffmpeg colour clip; `then` appends a second colour for as long again (late content)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    codec = {".webm": ["-c:v", "libvpx", "-b:v", "200k"], ".3gp": ["-c:v", "h263", "-s", "176x144"]}.get(
        ext, ["-c:v", "mpeg4"])
    src = ["-f", "lavfi", "-i", f"color=c={_hex(color)}:size=320x240:rate=10:duration={seconds}"]
    filt = []
    if then:
        src += ["-f", "lavfi", "-i", f"color=c={_hex(then)}:size=320x240:rate=10:duration={seconds}"]
        filt = ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0"]
    meta = ["-metadata", f"creation_time={created}"] if created else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *src, *filt, *codec, "-pix_fmt", "yuv420p",
                    *meta, path], check=True)
    return path


def audio_only(path, seconds=3):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-c:a", "aac", path], check=True)
    return path


def broken(path, kind, good=None):
    """zero: empty file; garbage: random bytes; truncated: the first half of `good`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        if kind == "garbage":
            f.write(os.urandom(6000))
        elif kind == "truncated":
            with open(good, "rb") as g:
                data = g.read()
            f.write(data[: len(data) // 2])
    return path


def mostly_red(path):
    with Image.open(path) as im:
        px = list(im.convert("RGB").resize((24, 24)).getdata())
    return sum(1 for r, g, b in px if r > 150 and g < 90 and b < 90) * 2 > len(px)


# ---------------------------------------------------------------- fake NudeNet
class FakeNudeDetector:
    """Stand-in for nudenet.NudeDetector: a mostly red picture is a hit. mode "raise" fails on
    every call, "raise-frames" only on video frames (scan_sensitive samples them into frames/)."""
    mode = "red"
    calls = []

    def detect(self, path):
        FakeNudeDetector.calls.append(path)
        is_frame = os.path.basename(os.path.dirname(path)) == "frames"
        if self.mode == "raise" or (self.mode == "raise-frames" and is_frame):
            raise RuntimeError("model crashed")
        if mostly_red(path):
            return [{"class": "BUTTOCKS_EXPOSED", "score": 0.9, "box": [0, 0, 1, 1]}]
        return [{"class": "FACE_FEMALE", "score": 0.9, "box": [0, 0, 1, 1]}]


def frame_calls():
    return [c for c in FakeNudeDetector.calls if os.path.basename(os.path.dirname(c)) == "frames"]


def run_scan(home, src, out, *extra, mode="red"):
    """scan_sensitive.main() in-process with the fake detector. Returns (scan.json, summary)."""
    FakeNudeDetector.mode, FakeNudeDetector.calls = mode, []
    fake = types.ModuleType("nudenet")
    fake.NudeDetector = FakeNudeDetector
    buf, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(sys.modules, {"nudenet": fake}), mock.patch.dict(os.environ, {"HOME": home}), \
            mock.patch.object(sys, "argv", ["scan_sensitive.py", src, out, *extra]), \
            contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        scan_sensitive.main()
    with open(out) as f:
        flags = json.load(f)
    return flags, json.loads(buf.getvalue().strip().splitlines()[-1])


def env_for(d, state, lib, vids, footage):
    return dict(os.environ, HOME=d, MEDIA_INTAKE_STATE=state, PHOTO_LIBRARY=lib, PERSONAL_FOOTAGE=vids,
                MEDIA_INTAKE_CONFIG=support.write_config(footage), TZ="America/New_York",
                MEDIA_INTAKE_NO_LOCALSEARCH="1", http_proxy="http://203.0.113.1:9", HTTP_PROXY="http://203.0.113.1:9",
                no_proxy="", NO_PROXY="")


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="detectors-kinds-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, ".local", "share", "media-intake")
        os.makedirs(self.state, mode=0o700)
        self.lib = os.path.join(self.d, "Pictures", "library")
        self.vids = os.path.join(self.d, "Videos", "personal")
        self.footage = os.path.join(self.d, "Videos", "demo-footage")
        self.env = env_for(self.d, self.state, self.lib, self.vids, self.footage)


# ================================================================ scan
@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe missing")
class ScanMediaKinds(Base):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.d, "raw")
        self.out = os.path.join(self.d, "scan.json")

    def S(self, rel):
        return os.path.join(self.src, rel)

    def test_mixed_batch_every_kind_checked(self):
        photo(self.S("p/red.jpg"), RED)
        photo(self.S("p/blue.jpg"), BLUE, seed=1)
        photo(self.S("p/blue.png"), BLUE, seed=2)
        video(self.S("v/red.mp4"), RED)
        video(self.S("v/blue.mp4"), BLUE)
        video(self.S("v/late-red.mp4"), BLUE, seconds=3, then=RED)  # red only in the second half
        video(self.S("v/RED-UPPER.MOV"), RED)
        video(self.S("v/short-red.mp4"), RED, seconds=0.5)  # under one sampling period
        for ext in (".mov", ".mkv", ".webm", ".3gp"):
            video(self.S(f"v/blue{ext}"), BLUE, seconds=2)
        flags, summary = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"p/red.jpg": ["nudity"], "v/red.mp4": ["nudity"], "v/late-red.mp4": ["nudity"],
                                 "v/RED-UPPER.MOV": ["nudity"], "v/short-red.mp4": ["nudity"]})
        self.assertEqual((summary["scanned"], summary["flagged"]), (12, 5))
        # Every clean video was really sampled (frames reached the detector), not passed over.
        sampled = len(frame_calls())
        self.assertGreaterEqual(sampled, 8, "each of the 9 videos needs >= 1 frame; red.mp4 stops at its first")
        self.assertEqual(os.listdir(os.path.join(self.d, ".cache", "media-intake")), [], "frames left behind")

    def test_broken_videos_fail_closed(self):
        good = video(self.S("v/good.mp4"), BLUE, seconds=8)
        broken(self.S("v/zero.mp4"), "zero")
        broken(self.S("v/garbage.mov"), "garbage")
        broken(self.S("v/truncated.mp4"), "truncated", good)
        audio_only(self.S("v/audio-only.mp4"))
        photo(self.S("p/blue.jpg"), BLUE)
        broken(self.S("p/garbage.jpg"), "garbage")
        flags, summary = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"v/zero.mp4": ["unscanned-video"], "v/garbage.mov": ["unscanned-video"],
                                 "v/truncated.mp4": ["unscanned-video"], "v/audio-only.mp4": ["unscanned-video"],
                                 "p/garbage.jpg": ["unreadable"]})
        self.assertEqual(summary["scanned"], 7)

    def test_staged_kinds_no_detector_reads_are_flagged(self):
        # .gpr is staged as media (sources.MEDIA_EXT, intake.IMG) but was outside scan's IMG and VID,
        # so it went through scan unchecked and was filed as clean.
        broken(self.S("DCIM/100GOPRO/GOPR0001.GPR"), "garbage")
        video(self.S("v/clip.m4v"), RED, seconds=2)  # any other kind: not checked, so not clean
        photo(self.S("p/blue.jpg"), BLUE)
        flags, _ = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"DCIM/100GOPRO/GOPR0001.GPR": ["unreadable"], "v/clip.m4v": ["unscanned-type"]})
        for ext in {".gpr", ".dng"} | scan_sensitive.VID:
            self.assertIn(ext, scan_sensitive.IMG | scan_sensitive.VID)
        import sources
        self.assertEqual(sources.MEDIA_EXT - (scan_sensitive.IMG | scan_sensitive.VID), set(),
                         "every kind stage copies has a detector path")

    def test_readable_raw_photos_are_scanned(self):
        # Synthetic TIFFs named like raws (a real DNG/GPR is a TIFF container): Pillow opens them, so
        # they take the convert-then-detect path instead of "unreadable".
        photo(self.S("DCIM/100GOPRO/GOPR0002.GPR"), RED)
        photo(self.S("DCIM/100GOPRO/GOPR0003.gpr"), BLUE, seed=1)
        photo(self.S("DCIM/Camera/raw.dng"), RED, seed=2)
        flags, summary = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"DCIM/100GOPRO/GOPR0002.GPR": ["nudity"], "DCIM/Camera/raw.dng": ["nudity"]})
        self.assertEqual(summary["scanned"], 3)
        self.assertTrue(all(os.path.basename(c) == "n.jpg" for c in FakeNudeDetector.calls), "converted first")

    def test_converted_image_detector_error_leaves_no_temp(self):
        tmp = tempfile.mkdtemp(dir=self.d)
        photo(self.S("p/red.webp"), RED)
        Image.new("RGB", (64, 48), BLUE).save(self.S("p/blue.gif"))
        photo(self.S("p/raw.gpr"), BLUE)

        class Crashing:
            def detect(self, path):
                raise RuntimeError("model crashed")
        for name in ("red.webp", "blue.gif", "raw.gpr"):
            self.assertEqual(scan_sensitive.scan_file(Crashing(), self.S("p/" + name), tmp), ["unscanned-nudity"], name)
            self.assertEqual(os.listdir(tmp), [], f"temp conversion left behind for {name}")

        def boom(det, path):  # nude_flags itself failing (not just the detector inside it)
            raise MemoryError("decoder")
        with mock.patch.object(scan_sensitive, "nude_flags", boom):
            with self.assertRaises(MemoryError):
                scan_sensitive.scan_file(Crashing(), self.S("p/red.webp"), tmp)
        self.assertEqual(os.listdir(tmp), [], "temp conversion left behind after a crash")

    def test_detector_error_on_video_frames_is_not_clean(self):
        video(self.S("v/blue.mp4"), BLUE)
        video(self.S("v/red.mp4"), RED)
        photo(self.S("p/red.jpg"), RED)
        photo(self.S("p/blue.jpg"), BLUE, seed=1)
        flags, _ = run_scan(self.d, self.src, self.out, mode="raise-frames")
        self.assertEqual(flags, {"v/blue.mp4": ["unscanned-nudity"], "v/red.mp4": ["unscanned-nudity"],
                                 "p/red.jpg": ["nudity"]})
        flags, _ = run_scan(self.d, self.src, self.out, mode="raise")
        self.assertEqual(set(flags), {"v/blue.mp4", "v/red.mp4", "p/red.jpg", "p/blue.jpg"})
        self.assertTrue(all(v == ["unscanned-nudity"] for v in flags.values()), flags)

    def test_one_crashing_file_keeps_the_batch(self):
        photo(self.S("p/a.jpg"), BLUE)
        photo(self.S("p/b.jpg"), RED, seed=1)
        video(self.S("v/red.mp4"), RED)
        real = scan_sensitive.ocr_flags

        def ocr(im, tmpdir):  # Tesseract hanging past its timeout on one picture
            if im.filename.endswith("a.jpg"):
                raise subprocess.TimeoutExpired(["tesseract"], 120)
            return real(im, tmpdir)
        with mock.patch.object(scan_sensitive, "ocr_flags", ocr):
            flags, summary = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"p/a.jpg": ["unscanned-error"], "p/b.jpg": ["nudity"], "v/red.mp4": ["nudity"]})
        self.assertEqual(summary["scanned"], 3)

    def test_decoder_missing_or_hanging_gives_no_frames(self):
        v = video(self.S("v/red.mp4"), RED)
        real = subprocess.run
        for exc in (subprocess.TimeoutExpired(["ffmpeg"], 900), FileNotFoundError("ffmpeg")):
            def run(cmd, *a, **k):
                if cmd[0] == "ffmpeg":
                    raise exc
                return real(cmd, *a, **k)
            fd = tempfile.mkdtemp(dir=self.d)
            with mock.patch.object(scan_sensitive.subprocess, "run", run):
                self.assertEqual(scan_sensitive.video_frames(v, fd), [], type(exc).__name__)
            self.assertEqual(os.listdir(fd), [])
        photo(self.S("p/red.jpg"), RED)

        def run(cmd, *a, **k):
            if cmd[0] == "ffmpeg":
                raise subprocess.TimeoutExpired(cmd, 900)
            return real(cmd, *a, **k)
        with mock.patch.object(scan_sensitive.subprocess, "run", run):
            flags, _ = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"v/red.mp4": ["unscanned-video"], "p/red.jpg": ["nudity"]})

    def test_partial_frames_and_missing_ffprobe_cleaned_up(self):
        v = video(self.S("v/red.mp4"), RED)
        real = subprocess.run
        fd = tempfile.mkdtemp(dir=self.d)

        def hang_after_two_frames(cmd, *a, **k):
            if cmd[0] == "ffmpeg":
                for n in ("001.jpg", "002.jpg"):
                    Image.new("RGB", (8, 8), RED).save(os.path.join(fd, n))
                raise subprocess.TimeoutExpired(cmd, 900)
            return real(cmd, *a, **k)
        with mock.patch.object(scan_sensitive.subprocess, "run", hang_after_two_frames):
            self.assertEqual(scan_sensitive.video_frames(v, fd), [])
        self.assertEqual(os.listdir(fd), [], "partial frames must not be judged later")

        def no_ffprobe(cmd, *a, **k):
            if cmd[0] == "ffprobe":
                raise FileNotFoundError("ffprobe")
            return real(cmd, *a, **k)
        with mock.patch.object(scan_sensitive.subprocess, "run", no_ffprobe):
            self.assertEqual(scan_sensitive.video_frames(v, fd), [])
        self.assertEqual(os.listdir(fd), [])

    def test_crash_in_video_loop_removes_frames(self):
        v = video(self.S("v/red.mp4"), RED)
        tmp = tempfile.mkdtemp(dir=self.d)

        def boom(det, path):
            raise MemoryError("detector")
        with mock.patch.object(scan_sensitive, "nude_flags", boom):
            with self.assertRaises(MemoryError):
                scan_sensitive.scan_file(FakeNudeDetector(), v, tmp)
        self.assertEqual(os.listdir(tmp), [], "frames of a crashed clip left behind")

    def test_scan_twice_same_result(self):
        photo(self.S("p/red.jpg"), RED)
        photo(self.S("p/blue.jpg"), BLUE, seed=1)
        video(self.S("v/red.mp4"), RED)
        video(self.S("v/short.mp4"), RED, seconds=0.5)
        broken(self.S("v/zero.mp4"), "zero")
        broken(self.S("p/raw.gpr"), "garbage")
        video(self.S("v/clip.m4v"), BLUE, seconds=2)
        flags1, s1 = run_scan(self.d, self.src, self.out, mode="raise-frames")
        with open(self.out, "rb") as f:
            first = f.read()
        flags2, s2 = run_scan(self.d, self.src, self.out, mode="raise-frames")
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), first)
        self.assertEqual(s1, s2)
        self.assertEqual(flags1, {"p/red.jpg": ["nudity"], "v/red.mp4": ["unscanned-nudity"],
                                  "v/short.mp4": ["unscanned-nudity"], "v/zero.mp4": ["unscanned-video"],
                                  "p/raw.gpr": ["unreadable"],
                                  "v/clip.m4v": ["unscanned-type"]})

    def test_frame_sampling_spreads_and_caps(self):
        v8 = video(self.S("v/eight.mp4"), BLUE, seconds=8)
        short = video(self.S("v/short.mp4"), BLUE, seconds=0.5)
        for path, cap, want in ((v8, 90, 4), (v8, 3, 3), (v8, 8, 4), (short, 90, 1), (short, 3, 1)):
            fd = tempfile.mkdtemp(dir=self.d)
            got = scan_sensitive.video_frames(path, fd, max_frames=cap)
            self.assertEqual(len(got), want, (path, cap))
            self.assertEqual(sorted(os.listdir(fd)), got)

    def test_videos_only_rescan_keeps_photo_flags(self):
        photo(self.S("p/red.jpg"), RED)
        video(self.S("v/blue.mp4"), BLUE)
        v = self.S("v/red.mp4")
        broken(v, "zero")  # first scan: undecodable
        flags, _ = run_scan(self.d, self.src, self.out)
        self.assertEqual(flags, {"p/red.jpg": ["nudity"], "v/red.mp4": ["unscanned-video"]})
        os.remove(v)
        video(v, RED)  # e.g. a decoder became available: re-check the videos only
        flags, summary = run_scan(self.d, self.src, self.out, "--videos-only")
        self.assertEqual(flags, {"p/red.jpg": ["nudity"], "v/red.mp4": ["nudity"]})
        self.assertEqual(summary["scanned"], 2)
        self.assertFalse(any(c.endswith(".jpg") and "/p/" in c for c in FakeNudeDetector.calls))


# ================================================================ GStreamer fallback: no name in the pipeline
# A stand-in tool: records its arguments (one JSON line per run). ffprobe and ffmpeg fail (so the GStreamer
# fallback runs); gst-inspect-1.0 says yes only to vah265dec; gst-launch-1.0 also records where each
# location= path resolves to and its folder's mode while it runs, and writes two frames where its
# multifilesink points.
FAKE_GST = r'''#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_TOOL_LOG"], "a") as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + "\n")
    if name == "gst-launch-1.0":
        f.write(json.dumps(["gst-where"] + [os.path.realpath(a[9:]) for a in sys.argv[1:]
                                            if a.startswith("location=")]) + "\n")
        f.write(json.dumps(["gst-mode"] + [oct(os.stat(os.path.dirname(a[9:])).st_mode & 0o777)
                                           for a in sys.argv[1:] if a.startswith("location=")]) + "\n")
if name == "gst-inspect-1.0":
    sys.exit(0 if len(sys.argv) == 1 or sys.argv[1] == "vah265dec" else 1)
if name == "gst-launch-1.0":
    sink = [a[9:] for a in sys.argv[1:] if a.startswith("location=")][-1]
    for i in (1, 2):
        with open(sink % i, "wb") as f:
            f.write(b"frame")
    sys.exit(0)
sys.exit(1)
'''


class ScanGstPipeline(Base):
    """scan_sensitive.video_frames' GStreamer fallback (HEVC where ffmpeg has no decoder) on a hostile name.
    gst-launch-1.0 re-parses its arguments as one pipeline, so "!" or a quote in the clip's name or folder
    could add elements (a sink writing anywhere): it must read a link in a private work folder instead.
    The tools are fakes on PATH (the CI image has no GStreamer); the clip is a few synthetic bytes."""
    def setUp(self):
        super().setUp()
        b = os.path.join(self.d, "fakebin")
        os.makedirs(b)
        for n in ("ffprobe", "ffmpeg", "gst-inspect-1.0", "gst-launch-1.0"):
            with open(os.path.join(b, n), "w") as f:
                f.write(FAKE_GST)
            os.chmod(os.path.join(b, n), 0o700)
        self.log = os.path.join(self.d, "fake-tools.log")
        p = mock.patch.dict(os.environ, {"PATH": b + os.pathsep + os.environ.get("PATH", ""),
                                         "FAKE_TOOL_LOG": self.log})
        p.start()
        self.addCleanup(p.stop)
        self.clip = os.path.join(self.d, "incoming", "b ! c", 'a ! fakesink "x" y.MOV')
        os.makedirs(os.path.dirname(self.clip))
        with open(self.clip, "wb") as f:
            f.write(b"not really hevc")
        scan_sensitive._GST_SKIP_NOTED.clear()  # the skip note is once per folder per process
        self.addCleanup(scan_sensitive._GST_SKIP_NOTED.clear)

    def runs(self, name):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [r[1:] for r in map(json.loads, f) if r[0] == name]

    def test_hostile_name_never_reaches_the_pipeline(self):
        fd = tempfile.mkdtemp(dir=self.d)
        orig = sha(self.clip)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(scan_sensitive.video_frames(self.clip, fd), ["001.jpg", "002.jpg"])
        self.assertEqual(err.getvalue(), "")  # a safe work folder: no skip note
        self.assertEqual(sorted(os.listdir(fd)), ["001.jpg", "002.jpg"])  # the work folder is gone
        self.assertEqual(sha(self.clip), orig)  # removing the work folder (and its link) left the original
        self.assertEqual(self.runs("gst-mode"), [["0o700", "0o700"]])  # private while GStreamer ran
        (a,), ((src, sink),) = self.runs("gst-launch-1.0"), self.runs("gst-where")
        self.assertEqual(a[0], "-q")
        locs = [x[9:] for x in a if x.startswith("location=")]
        self.assertEqual(len(locs), 2, a)  # filesrc and multifilesink, nothing more
        work = os.path.dirname(locs[0])
        self.assertEqual(os.path.dirname(work), os.path.abspath(fd))
        self.assertTrue(os.path.basename(work).startswith(".gst-"), work)
        self.assertEqual(locs, [os.path.join(work, "in"), os.path.join(work, "%03d.jpg")])
        self.assertEqual(src, os.path.realpath(self.clip))  # the link reads the original
        self.assertEqual(sink, os.path.realpath(os.path.join(work, "%03d.jpg")))
        for x in a:  # no part of the name or its folder in any argument
            for bit in ("! fakesink", '"', "y.MOV", "b ! c", "incoming"):
                self.assertNotIn(bit, x, a)
        self.assertEqual(a.count("fakesink"), 0, a)
        for el in ("filesrc", "vah265dec", "vapostproc", "jpegenc", "multifilesink"):
            self.assertEqual(a.count(el), 1, (el, a))
        self.assertTrue(all(x == "!" or "!" not in x for x in a), a)

    def test_unsafe_work_folder_skips_gstreamer_and_is_not_clean(self):
        tmp = os.path.join(self.d, "work dir ! x")
        os.makedirs(tmp, mode=0o700)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):  # a batch of clips: one note for the folder, not one per clip
                self.assertEqual(scan_sensitive.scan_file(FakeNudeDetector(), self.clip, tmp), ["unscanned-video"])
        self.assertEqual(self.runs("gst-launch-1.0"), [])
        self.assertEqual(os.listdir(tmp), [])  # nothing left behind
        frames = os.path.join(tmp, "frames")
        self.assertEqual(err.getvalue().splitlines(), [
            "[gst] fallback skipped: work folder path has characters GStreamer can't take safely "
            f"(only letters, digits and ._/+- are): {frames}; clip left not checked"])
        self.assertNotIn("y.MOV", err.getvalue())  # the folder, never the clip's name
        fd = tempfile.mkdtemp(prefix='q"', dir=self.d)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(scan_sensitive.video_frames(self.clip, fd), [])
        self.assertEqual((os.listdir(fd), self.runs("gst-launch-1.0")), ([], []))
        self.assertIn(f": {os.path.abspath(fd)}; clip left not checked", err.getvalue())  # another folder: its own note


class FfmpegInputIsAFile(Base):
    """A relative path like "concat:x/clip.mov" must reach ffmpeg and ffprobe as file:<absolute path>, never as
    a protocol (concat:, http:, ...) they would follow. subprocess.run is a recorder: no tool runs."""
    def setUp(self):
        super().setUp()
        cwd = os.getcwd()
        os.chdir(self.d)
        self.addCleanup(os.chdir, cwd)
        p = mock.patch.dict(os.environ, {"HOME": self.d})  # label's GStreamer fallback works under ~/.cache
        p.start()
        self.addCleanup(p.stop)
        os.makedirs("concat:x")
        self.rel = os.path.join("concat:x", "clip.mov")
        with open(self.rel, "wb") as f:
            f.write(b"not a video")
        self.cmds = []

    def run_recorded(self, call):
        def rec(cmd, *a, **k):
            self.cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "" if k.get("text") else b"", b"")
        with mock.patch.object(subprocess, "run", rec):
            call()
        tools = [c for c in self.cmds if c[0] in ("ffmpeg", "ffprobe")]
        self.assertTrue({"ffmpeg", "ffprobe"} <= {c[0] for c in tools}, self.cmds)
        want = "file:" + os.path.abspath(self.rel)
        for c in tools:
            self.assertIn(want, c)
            for x in c:
                self.assertFalse(x.startswith("concat:"), c)

    def test_scan_frames(self):
        fd = tempfile.mkdtemp(dir=".")
        self.run_recorded(lambda: self.assertEqual(scan_sensitive.video_frames(self.rel, fd), []))
        out = [c[-1] for c in self.cmds if c[0] == "ffmpeg"]
        self.assertEqual(out, ["file:" + os.path.join(os.path.abspath(fd), "%03d.jpg")])

    def test_label_frames(self):
        import label
        self.run_recorded(lambda: self.assertIsNone(label.frames_video(self.rel, None)))


# ================================================================ scan -> apply -> tag (+ plugin) end to end
PLUGIN = '''import json, os
def after_tag(files):
    with open(os.environ["PLUGIN_OUT"], "w") as f:
        json.dump(sorted(x["path"] for x in files), f)
'''


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe missing")
class ScanApplyTag(Base):
    def intake(self, *args):
        r = subprocess.run([PY, "-I", os.path.join(PKG, "intake.py"), *args], env=self.env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=600)
        self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def test_unscanned_type_and_error_reach_quarantine(self):
        phone = os.path.join(self.d, "phone", "Internal storage")
        cam = os.path.join(phone, "DCIM", "Camera")
        ok_p = photo(os.path.join(cam, "20261020_120000.jpg"), BLUE, dt="2026:10:20 12:00:00")
        photo(os.path.join(cam, "20261020_121000.jpg"), GREEN, seed=3, dt="2026:10:20 12:10:00")  # check crashes
        photo(os.path.join(cam, "20261020_122000.gpr"), BLUE, seed=4)  # a kind the scan is made not to know
        video(os.path.join(cam, "20261021_090000.mp4"), BLUE, created="2026-10-21T13:00:00Z")
        batch = "2026-11-14-unscanned"
        bd = os.path.join(self.state, "batches", batch)
        self.intake("stage", phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        real = scan_sensitive.ocr_flags

        def ocr(im, tmpdir):
            if im.filename.endswith("121000.jpg"):
                raise subprocess.TimeoutExpired(["tesseract"], 120)
            return real(im, tmpdir)
        with mock.patch.object(scan_sensitive, "ocr_flags", ocr), \
                mock.patch.object(scan_sensitive, "IMG", scan_sensitive.IMG - {".gpr"}):
            flags, _ = run_scan(self.d, os.path.join(bd, "raw"), os.path.join(bd, "scan.json"))
        want = {"DCIM/Camera/20261020_121000.jpg": ["unscanned-error"],
                "DCIM/Camera/20261020_122000.gpr": ["unscanned-type"]}
        self.assertEqual(flags, want)
        out = self.intake("apply", batch, "--allow-missing-detectors")
        self.assertIn("'quarantined': 2", out)
        quar = os.path.join(self.state, "quarantine", batch)
        self.assertEqual(stat.S_IMODE(os.stat(quar).st_mode), 0o700)
        with open(os.path.join(quar, "quarantine.json")) as f:
            self.assertEqual({k: v["reasons"] for k, v in json.load(f).items()}, want)
        moved = [os.path.join(quar, rel) for rel in want]
        for p in moved:
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)
        filed = [os.path.join(r, n) for root in (self.lib, self.vids) for r, _, ns in os.walk(root) for n in ns]
        self.assertEqual(sorted(os.path.basename(p) for p in filed), ["20261020_120000.jpg", "20261021_090000.mp4"])
        # label, semantic and vlm_trash all gate on label.refused: the quarantined files and any copy
        # of them back in the library are refused, the clean filed ones are not.
        import label
        twins = []
        for p in moved:
            t = os.path.join(self.lib, "copies", os.path.basename(p))
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copyfile(p, t)
            twins.append(t)
        self.assertEqual(label.refused(moved + twins + filed, self.state), moved + twins)
        self.assertNotIn(ok_p, label.refused([ok_p], self.state))

    def test_flagged_video_reaches_quarantine_and_never_the_plugin(self):
        phone = os.path.join(self.d, "phone", "Internal storage")
        cam = os.path.join(phone, "DCIM", "Camera")
        photo(os.path.join(cam, "20261020_120000.jpg"), BLUE, dt="2026:10:20 12:00:00")
        photo(os.path.join(cam, "20261020_120500.jpg"), RED, seed=1, dt="2026:10:20 12:05:00")
        video(os.path.join(cam, "20261021_090000.mp4"), BLUE, created="2026-10-21T13:00:00Z")
        video(os.path.join(cam, "20261021_091000.mp4"), BLUE, seconds=3, then=RED, created="2026-10-21T13:10:00Z")
        broken(os.path.join(cam, "20261021_092000.mp4"), "garbage")
        batch = "2026-11-14-kinds"
        bd = os.path.join(self.state, "batches", batch)
        self.intake("stage", phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        flags, _ = run_scan(self.d, os.path.join(bd, "raw"), os.path.join(bd, "scan.json"))
        self.assertEqual(flags, {"DCIM/Camera/20261020_120500.jpg": ["nudity"],
                                 "DCIM/Camera/20261021_091000.mp4": ["nudity"],
                                 "DCIM/Camera/20261021_092000.mp4": ["unscanned-video"]})
        out = self.intake("apply", batch, "--allow-missing-detectors")
        self.assertIn("'quarantined': 3", out)
        quar = os.path.join(self.state, "quarantine", batch)
        with open(os.path.join(quar, "quarantine.json")) as f:
            q = json.load(f)
        self.assertEqual({k: v["reasons"] for k, v in q.items()}, flags)
        for rel in flags:
            self.assertTrue(os.path.isfile(os.path.join(quar, rel)), rel)
        filed_v = os.path.join(self.vids, "2026", "2026-10", "20261021_090000.mp4")
        filed_p = os.path.join(self.lib, "2026", "2026-10", "20261020_120000.jpg")
        self.assertTrue(os.path.isfile(filed_v) and os.path.isfile(filed_p))
        for name in ("20261021_091000.mp4", "20261021_092000.mp4", "20261020_120500.jpg"):
            hits = [os.path.join(r, n) for root in (self.lib, self.vids) for r, _, ns in os.walk(root)
                    for n in ns if n == name]
            self.assertEqual(hits, [], f"a flagged file was filed: {name}")
        # Every detector refuses the quarantined video and its filed twin by content (sha + size).
        import label
        qv = os.path.join(quar, "DCIM/Camera/20261021_091000.mp4")
        twin = os.path.join(self.vids, "elsewhere", "copy.mp4")
        os.makedirs(os.path.dirname(twin))
        shutil.copyfile(qv, twin)
        self.assertEqual(label.refused([qv, twin, filed_v, filed_p], self.state), [qv, twin])
        if not HAVE_EXIFTOOL:
            return  # tag writes sidecars with exiftool
        pdir = os.path.join(self.d, "plugins")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "kinds_plugin.py"), "w") as f:
            f.write(PLUGIN)
        cfg = os.path.join(self.d, "plugin-config.toml")
        with open(self.env["MEDIA_INTAKE_CONFIG"]) as f:
            base = f.read()
        with open(cfg, "w") as f:
            f.write(base + f'\n[plugins]\npaths = ["{pdir}"]\nmodules = ["kinds_plugin"]\n')
        os.remove(twin)
        self.env.update(MEDIA_INTAKE_CONFIG=cfg, PLUGIN_OUT=os.path.join(self.d, "plugin-out.json"))
        before = sha(filed_v)
        self.intake("tag", batch, "--no-labels")
        with open(self.env["PLUGIN_OUT"]) as f:
            self.assertEqual(json.load(f), sorted([filed_p, filed_v]))
        self.assertTrue(os.path.isfile(filed_v + ".xmp"), "the video's sidecar is <clip>.mp4.xmp")
        self.assertEqual(sha(filed_v), before, "originals are never written")


# ================================================================ vlm_trash (fake Ollama)
class Ollama(http.server.BaseHTTPRequestHandler):
    digest, requests, votes = None, [], None

    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        m = {"name": "qwen3-vl:2b", "model": "qwen3-vl:2b", "digest": Ollama.digest, "size": 10, "size_vram": 10}
        self._send({"models": [m]} if self.path in ("/api/tags", "/api/ps") else {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Ollama.requests.append(body)
        if "prompt" not in body:
            self._send({"done": True})
            return
        # Judge by the picture sent: mostly red is junk. The stub sees only synthetic frames.
        im = Image.open(io.BytesIO(base64.b64decode(body["images"][0])))
        px = list(im.convert("RGB").resize((16, 16)).getdata())
        junk = sum(1 for r, g, b in px if r > 150 and g < 90 and b < 90) * 2 > len(px)
        v = {"verdict": "TRASH", "reason": "dark", "confidence": 0.9} if junk else \
            {"verdict": "KEEP", "reason": "other", "confidence": 0.8}
        self._send({"response": json.dumps(v), "prompt_eval_count": 900, "done": True})


@unittest.skipUnless(HAVE_FFMPEG and HAVE_EXIFTOOL, "ffmpeg or exiftool missing")
class VlmTrashMediaKinds(Base):
    @classmethod
    def setUpClass(cls):
        import vlm_trash
        cls.vt = vlm_trash
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
        cls.host = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        super().setUp()
        Ollama.digest, Ollama.requests = self.vt.DIGEST, []
        self.ns = self.vt.NS

    def run_v(self, *args):
        r = subprocess.run([PY, "-I", os.path.join(PKG, "vlm_trash.py"), "--host", self.host, *args], env=self.env,
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def gens(self):
        return [b for b in Ollama.requests if "prompt" in b]

    def tag(self, p, t):
        if not os.path.exists(p + ".xmp"):
            with open(p + ".xmp", "w") as f:
                f.write(EMPTY_XMP)
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-XMP-lr:HierarchicalSubject+={t}", p + ".xmp"],
                       check=True)

    def tags(self, p):
        if not os.path.exists(p + ".xmp"):
            return []
        r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", p + ".xmp"], capture_output=True,
                           text=True)
        v = json.loads(r.stdout)[0].get("HierarchicalSubject", [])
        return sorted(v if isinstance(v, list) else [v])

    def test_maybe_trash_video_and_photo_candidates(self):
        V = lambda *p: os.path.join(self.vids, "2026", "2026-10", *p)  # noqa: E731
        clip = video(V("clip.mp4"), RED, seconds=8)
        keep_clip = video(V("keep.mov"), BLUE, seconds=8)
        pic = photo(os.path.join(self.lib, "2026", "2026-10", "pic.jpg"), RED)
        short = video(V("short.mp4"), RED, seconds=0.5)
        untagged = video(V("untagged.mp4"), RED, seconds=4)
        shas = {p: sha(p) for p in (clip, keep_clip, pic, short, untagged)}
        for p in (clip, keep_clip, pic, short):
            self.tag(p, "Review|maybe-trash")
        c = self.run_v()  # default: only files tagged Review|maybe-trash
        self.assertEqual((c["todo"], c.get("trash"), c.get("keep"), c.get("undecodable", 0)), (4, 3, 1, 0))
        self.assertEqual(self.tags(clip), sorted(["Review|maybe-trash", self.ns + "trash/dark", self.ns + "conf/0.9"]))
        self.assertEqual(self.tags(keep_clip), sorted(["Review|maybe-trash", self.ns + "keep", self.ns + "conf/0.8"]))
        self.assertEqual(self.tags(short), sorted(["Review|maybe-trash", self.ns + "trash/dark", self.ns + "conf/0.9"]))
        self.assertEqual(self.tags(pic), sorted(["Review|maybe-trash", self.ns + "trash/dark", self.ns + "conf/0.9"]))
        self.assertFalse(os.path.exists(untagged + ".xmp"))
        # 3 sampled frames for each long clip, 1 for the short clip, 1 for the photo; each a JPEG <= 768 px.
        gens = self.gens()
        self.assertEqual(len(gens), 3 + 3 + 1 + 1)
        for b in gens:
            self.assertEqual(len(b["images"]), 1)
            with Image.open(io.BytesIO(base64.b64decode(b["images"][0]))) as im:
                self.assertEqual(im.format, "JPEG")
                self.assertLessEqual(max(im.size), self.vt.MAX_PX)
        self.assertEqual({p: sha(p) for p in shas}, shas, "originals are never written")
        self.assertEqual(os.listdir(os.path.join(self.d, ".cache", "media-intake")), [], "frames left behind")

    def test_raw_photo_candidates_judged(self):
        P = lambda *p: os.path.join(self.lib, "2026", "2026-10", *p)  # noqa: E731
        gpr, dng = photo(P("GOPR0001.GPR"), RED), photo(P("raw.dng"), BLUE, seed=1)
        bad = broken(P("GOPR0002.gpr"), "garbage")
        for p in (gpr, dng, bad):
            self.tag(p, "Review|maybe-trash")
        c = self.run_v()
        self.assertEqual((c["todo"], c.get("trash"), c.get("keep"), c.get("undecodable")), (3, 1, 1, 1))
        self.assertIn(self.ns + "trash/dark", self.tags(gpr))
        self.assertIn(self.ns + "keep", self.tags(dng))
        self.assertEqual(self.tags(bad), ["Review|maybe-trash"])

    def test_broken_videos_are_undecodable_not_judged(self):
        V = lambda *p: os.path.join(self.vids, "2026", "2026-10", *p)  # noqa: E731
        good = video(V("good.mp4"), RED, seconds=8)
        bad = [broken(V("zero.mp4"), "zero"), broken(V("garbage.mp4"), "garbage"),
               broken(V("truncated.mp4"), "truncated", good), audio_only(V("audio.mp4"))]
        pic = photo(os.path.join(self.lib, "2026", "2026-10", "pic.jpg"), BLUE)
        c = self.run_v("--all")
        self.assertEqual((c["todo"], c["undecodable"], c.get("trash"), c.get("keep")), (6, 4, 1, 1))
        for p in bad:
            self.assertEqual([t for t in self.tags(p) if t.startswith("AI|")], [], p)
        self.assertIn(self.ns + "keep", self.tags(pic))
        c = self.run_v("--all")  # undecodable ones are tried again, never recorded as judged
        self.assertEqual((c["already"], c["todo"], c["undecodable"]), (2, 4, 4))

    def test_flagged_video_never_sent(self):
        V = lambda *p: os.path.join(self.vids, "2026", "2026-10", *p)  # noqa: E731
        flagged = video(V("flagged.mp4"), RED, seconds=4)
        ok = video(V("ok.mp4"), BLUE, seconds=4)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"x sha256:{sha(flagged)} size:{os.path.getsize(flagged)}\n")
        r = subprocess.run([PY, "-I", os.path.join(PKG, "vlm_trash.py"), "--host", self.host, flagged, ok],
                           env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.gens(), [])
        c = self.run_v("--all")
        self.assertEqual((c["refused"], c["todo"]), (1, 1))
        self.assertFalse(os.path.exists(flagged + ".xmp"))


# ================================================================ semantic (fake SigLIP)
SEM_DRIVER = r'''
import json, sys, types
sys.path.insert(0, sys.argv[1])
sys.modules["onnxruntime"] = types.ModuleType("onnxruntime")  # never used: the session is faked
import numpy as np
import semantic

class Sess:
    """Embeds the mean colour of each preprocessed frame: red near red, blue near blue."""
    def run(self, names, feed):
        x = feed["pixel_values"]
        out = np.zeros((len(x), 768), np.float32)
        out[:, :3] = x.mean(axis=(2, 3))
        out[:, 3] = 0.05
        return [out]

QUERIES = {"red": [1, -1, -1], "blue": [-1, -0.7, 1]}
def embed_text(q, np, ort):
    v = np.zeros(768, np.float32); v[:3] = QUERIES[q]; v[3] = 0.05
    return v / np.linalg.norm(v)

semantic.vision = lambda ort: Sess()
semantic.embed_text = embed_text
semantic.main(sys.argv[2:])
'''


@unittest.skipUnless(HAVE_FFMPEG and HAVE_NUMPY, "ffmpeg or numpy missing")
class SemanticMediaKinds(Base):
    def sem(self, *args, code=0):
        r = subprocess.run([PY, "-I", "-c", SEM_DRIVER, PKG, *args], env=self.env, capture_output=True, text=True,
                           timeout=600)
        self.assertEqual(r.returncode, code, r.stderr[-800:])
        return r

    def index(self):
        return json.loads(self.sem("index").stdout.strip().splitlines()[-1])

    def hits(self, r):
        return [l.split("  ", 1)[1] for l in r.stdout.splitlines() if l.strip()]

    def test_index_and_search_photos_and_videos(self):
        V = lambda *p: os.path.join(self.vids, "2026", "2026-10", *p)  # noqa: E731
        red_v = video(V("red.mp4"), RED, seconds=6)
        blue_v = video(V("blue.webm"), BLUE, seconds=6)
        short_v = video(V("short.mov"), RED, seconds=0.5)
        late_v = video(V("late.mkv"), BLUE, seconds=3, then=RED)
        red_p = photo(os.path.join(self.lib, "2026", "2026-10", "red.jpg"), RED)
        blue_p = photo(os.path.join(self.lib, "2026", "2026-10", "blue.png"), BLUE, seed=1)
        proj_v = video(os.path.join(self.footage, "originals", "2026-11-08-x-shoot", "cam", "GX010001.MP4"),
                       BLUE, seconds=4)
        good = video(V("good.mp4"), GREEN, seconds=8)
        bad = [broken(V("zero.mp4"), "zero"), broken(V("garbage.mp4"), "garbage"),
               broken(V("truncated.mp4"), "truncated", good), audio_only(V("audio.mp4"))]
        c = self.index()
        self.assertEqual((c["new_images"], c["new_videos"], c["undecodable"]), (2, 6, 4))
        self.assertEqual((c["files"], c["vectors"]), (8, 8))
        top = self.hits(self.sem("search", "red", "-n", "3"))
        self.assertEqual(set(top), {red_v, red_p, short_v})
        self.assertEqual(set(self.hits(self.sem("search", "red", "--videos", "-n", "2"))), {red_v, short_v})
        self.assertEqual(set(self.hits(self.sem("search", "blue", "--videos", "-n", "2"))), {blue_v, proj_v})
        self.assertEqual(self.hits(self.sem("search", "blue", "--photos", "-n", "1")), [blue_p])
        got = set(self.hits(self.sem("search", "red", "-n", "50")))
        self.assertEqual(got, {red_v, blue_v, short_v, late_v, red_p, blue_p, proj_v, good})
        self.assertFalse(got & set(bad), "an undecodable video must never be a result")
        # similar on a video file finds the other red clip and the red photo first
        self.assertEqual(set(self.hits(self.sem("similar", red_v, "-n", "2"))), {short_v, red_p})
        c = self.index()  # incremental; the broken ones are retried, never stored
        self.assertEqual((c["new_images"], c["new_videos"], c["undecodable"], c["vectors"]), (0, 0, 4, 8))

    def test_raw_photos_indexed(self):
        # .dng and .gpr were outside semantic's kinds, so raw photos were never indexed or found.
        P = lambda *p: os.path.join(self.lib, "2026", "2026-10", *p)  # noqa: E731
        red_dng, red_gpr = photo(P("red.dng"), RED), photo(P("GOPR0001.GPR"), RED, seed=1)
        blue_gpr = photo(P("GOPR0002.gpr"), BLUE, seed=2)
        broken(P("GOPR0003.GPR"), "garbage")
        blue_v = video(os.path.join(self.vids, "2026", "2026-10", "blue.mp4"), BLUE, seconds=4)
        c = self.index()
        self.assertEqual((c["new_images"], c["new_videos"], c["undecodable"], c["vectors"]), (3, 1, 1, 4))
        self.assertEqual(set(self.hits(self.sem("search", "red", "--photos", "-n", "2"))), {red_dng, red_gpr})
        self.assertEqual(self.hits(self.sem("search", "blue", "--photos", "-n", "1")), [blue_gpr])
        self.assertEqual(self.hits(self.sem("search", "blue", "--videos", "-n", "1")), [blue_v])

    def test_flagged_video_dropped_from_index_and_results(self):
        V = lambda *p: os.path.join(self.vids, "2026", "2026-10", *p)  # noqa: E731
        red_v = video(V("red.mp4"), RED, seconds=4)
        red2 = video(V("red2.mp4"), RED, seconds=6)
        photo(os.path.join(self.lib, "2026", "2026-10", "blue.jpg"), BLUE)
        self.assertEqual(self.index()["vectors"], 3)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"x sha256:{sha(red2)} size:{os.path.getsize(red2)}\n")
        self.assertNotIn(red2, self.hits(self.sem("search", "red", "-n", "10")))
        self.assertEqual(self.sem("similar", red2, code=3).returncode, 3)
        c = self.index()
        self.assertEqual((c["refused"], c["vectors"]), (1, 2))
        self.assertEqual(self.hits(self.sem("search", "red", "--videos")), [red_v])


# ================================================================ label (fake SigLIP vision session)
LABEL_DRIVER = r'''
import json, os, sys, tempfile, types
sys.path.insert(0, sys.argv[1])
import numpy as np
import label

class Sess:
    def __init__(self, *a, **k):
        pass
    def run(self, names, feed):
        x = feed["pixel_values"]
        out = np.zeros((len(x), 768), np.float32)
        out[:, :3] = x.mean(axis=(2, 3))
        out[:, 3] = 0.05
        return [out]

ort = types.ModuleType("onnxruntime")
ort.SessionOptions = lambda: types.SimpleNamespace(log_severity_level=0)
ort.InferenceSession = Sess
sys.modules["onnxruntime"] = ort
fd, label.MODEL = tempfile.mkstemp()
os.close(fd)
label.MODEL_SHA = label._sha(label.MODEL)
try:
    label.main()
finally:
    os.remove(label.MODEL)
'''


@unittest.skipUnless(HAVE_FFMPEG and HAVE_NUMPY and HAVE_YAML, "ffmpeg, numpy or pyyaml missing")
class LabelMediaKinds(Base):
    def label(self, paths, code=0):
        r = subprocess.run([PY, "-I", "-c", LABEL_DRIVER, PKG], input=json.dumps(paths), env=self.env,
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, code, r.stderr[-800:])
        return json.loads(r.stdout) if code == 0 else None

    def test_photos_videos_and_broken_videos(self):
        V = lambda *p: os.path.join(self.vids, "2026", "2026-10", *p)  # noqa: E731
        vids = [video(V("blue.mp4"), BLUE, seconds=6), video(V("short.mov"), BLUE, seconds=0.5),
                video(V("late.webm"), BLUE, seconds=3, then=RED)]
        good = video(V("good.mp4"), GREEN, seconds=8)
        bad = [broken(V("zero.mp4"), "zero"), broken(V("garbage.mkv"), "garbage"),
               broken(V("truncated.mp4"), "truncated", good), audio_only(V("audio.mp4"))]
        pic = photo(os.path.join(self.lib, "a.jpg"), BLUE)
        dark = os.path.join(self.lib, "dark.jpg")
        Image.new("RGB", (640, 480), (2, 2, 2)).save(dark)
        dark_v = os.path.join(V("dark.mp4"))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-f", "lavfi", "-i",
                        "color=c=0x020202:size=320x240:rate=10:duration=4", "-c:v", "mpeg4", "-pix_fmt", "yuv420p",
                        dark_v], check=True)
        paths = vids + bad + [pic, dark, dark_v]
        out = self.label(paths)
        self.assertEqual(sorted(out), sorted(paths), "every path gets an answer")
        for p in vids + [pic, dark, dark_v]:
            self.assertIsInstance(out[p]["labels"], dict, p)
            self.assertNotIn("untagged-video", out[p]["review"], p)
        for p in bad:
            self.assertEqual(out[p], {"labels": {}, "review": ["untagged-video"]}, p)
        self.assertIn("maybe-trash", out[dark]["review"])
        # Documented: the cheap blur/dark junk check is images only; a dark clip is not flagged by it.
        self.assertNotIn("maybe-trash", out[dark_v]["review"])

    def test_flagged_video_refused(self):
        v = video(os.path.join(self.vids, "v.mp4"), BLUE, seconds=2)
        pic = photo(os.path.join(self.lib, "a.jpg"), BLUE)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"x sha256:{sha(v)} size:{os.path.getsize(v)}\n")
        self.label([pic, v], code=3)


if __name__ == "__main__":
    unittest.main()
