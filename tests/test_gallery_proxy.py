#!/usr/bin/env python3
"""The gallery's H.264 proxy for a video the browser can't play (HEVC), and its clean-up.

  python3 -I -m unittest discover -s tests -p test_gallery_proxy.py

Synthetic clips in a throwaway HOME (tests/support.py): an HEVC .MOV with AAC sound made with
ffmpeg's libx265 (the HEVC cases skip, naming the reason, when this ffmpeg has no libx265 or no
H.264 encoder) and an H.264 .mp4. The real gallery server serves them; trash, forget and
verify-gone run the real intake.py. Failure paths put fake ffmpeg / GStreamer tools first on the
server's PATH (ffprobe stays the real one), which record their arguments and fail. ProxiesRunTest runs
proxies.main in-process (a video trashed, erased or flagged mid-make; still_shown's refusals). PlayableTest
runs App.playable in-process with make_proxy held on an event (who waits for which transcode). MakeProxyTest
runs make_proxy with Python stand-ins for the commands (a stalled attempt, the deadline, the failure record);
PlanTest runs proxies.plan with the App stood in for (a recorded failure skipped, or retried).
"""
import contextlib, glob, http.client, io, json, os, shutil, stat, subprocess, sys, tempfile, threading, time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import test_gallery as G  # noqa: E402  (Server, sha, until, PY, INTAKE)


def encoders():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


ENC = encoders()
HAVE_X265 = " libx265 " in ENC
H264 = next((e for e in ("libx264", "libopenh264") if f" {e} " in ENC), None)
_HEVC = {}


def hevc_encoder():
    """How this machine can make a synthetic HEVC clip: ["-c:v", "libx265", ...] (CI's Ubuntu ffmpeg), else
    ffmpeg's VA-API encoder on the first render node that takes a one-frame test (["-vaapi_device", node, ...];
    Fedora's ffmpeg-free has no libx265), else None. Worked out once."""
    if "args" not in _HEVC:
        _HEVC["args"] = None
        if HAVE_X265:
            _HEVC["args"] = (["-c:v", "libx265", "-x265-params", "log-level=none", "-pix_fmt", "yuv420p"], [])
        elif " hevc_vaapi " in ENC:
            for node in sorted(glob.glob("/dev/dri/renderD*")):
                pre = ["-vaapi_device", node]
                enc = ["-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi"]
                r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *pre, "-f", "lavfi", "-i",
                                    "testsrc=size=320x240:rate=10:duration=0.1", *enc, "-f", "null", "-"],
                                   capture_output=True)
                if r.returncode == 0:
                    _HEVC["args"] = (enc, pre)
                    break
    return _HEVC["args"]


def can_proxy():
    """This machine can make a real proxy: ffmpeg with an HEVC decoder and an H.264 encoder, or GStreamer's GPU
    HEVC decoder and an H.264 encoder (what gallery.proxy_commands would try)."""
    import gallery
    import scan_sensitive
    if gallery.ffmpeg_decodes_hevc() and H264:
        return True
    return bool(scan_sensitive._gst_hevc_decoder() and gallery.gst_element(gallery.GST_H264_HW + gallery.GST_H264_SW))


def mkclip(path, codec, seconds=1, size="320x240"):
    """A small test pattern with a sine tone: codec "hevc" (hevc_encoder(), tagged hvc1 as phones do) or
    "h264"."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pre, v = [], ["-c:v", H264, "-pix_fmt", "yuv420p"]
    if codec == "hevc":
        enc, pre = hevc_encoder()
        v = [*enc, "-tag:v", "hvc1"]
    fmt = ["-f", "mov"] if path.lower().endswith(".mov") else ["-f", "mp4"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *pre, "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=10:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-shortest", *v,
                    "-c:a", "aac", *fmt, "file:" + path], check=True)


def streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", "file:" + path],
                       capture_output=True, text=True, check=True)
    return {s["codec_type"]: s["codec_name"] for s in json.loads(r.stdout)["streams"]}


def top_boxes(path):
    """The MP4's top-level box types, in file order (a plain proxy has moov before mdat; a fragmented one moof)."""
    out = []
    with open(path, "rb") as f:
        while (h := f.read(8)) and len(h) == 8:
            size, kind = int.from_bytes(h[:4], "big"), h[4:].decode("latin-1")
            if size == 1:
                size = int.from_bytes(f.read(8), "big") - 8
            out.append(kind)
            if size < 8:
                break
            f.seek(size - 8, os.SEEK_CUR)
    return out


# A stand-in tool: records its arguments (one JSON line per run) and fails.
# gst-inspect-1.0 says yes only to the element names in FAKE_GST_HAS; gst-launch-1.0 also records
# where each location= path resolves to (["gst-where", ...]), while its work folder still exists.
FAKE_TOOL = r'''#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_TOOL_LOG"], "a") as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + "\n")
    if name == "gst-launch-1.0":
        f.write(json.dumps(["gst-where"] + [os.path.realpath(a[9:]) for a in sys.argv[1:]
                                            if a.startswith("location=")]) + "\n")
if name == "gst-inspect-1.0":
    sys.exit(0 if len(sys.argv) == 1 or sys.argv[1] in os.environ.get("FAKE_GST_HAS", "").split() else 1)
if name == "ffmpeg" and "-encoders" in sys.argv:  # it claims an H.264 encoder, then fails every transcode
    print(" V....D libx264              fake")
    sys.exit(0)
if name == "ffmpeg" and "-decoders" in sys.argv:  # and an HEVC decoder, so the gallery tries it
    print(" VFS..D hevc                 fake")
    sys.exit(0)
sys.exit(1)
'''

# The transcode held before it exits, whichever tool makes it (ffmpeg where it decodes HEVC, else gst-launch-1.0):
# it runs the real tool (the whole proxy is written, the original read), says so (a file, renamed into place),
# then blocks opening the FIFO at TOOL_GATE until the test opens it, and exits as the real one did. So the test
# holds a transcode that has written everything but hasn't ended. ffmpeg's other runs (-encoders, -decoders,
# the remux, which copies with -c copy) go straight to the real ffmpeg.
GATED_TOOL = r'''#!/usr/bin/env python3
import os, subprocess, sys
name = os.path.basename(sys.argv[0])
real = os.environ["REAL_" + {"ffmpeg": "FFMPEG", "gst-launch-1.0": "GST_LAUNCH"}[name]]
if name == "ffmpeg" and "-c:v" not in sys.argv:
    os.execv(real, [real, *sys.argv[1:]])
rc = subprocess.run([real, *sys.argv[1:]]).returncode
with open(os.environ["TOOL_STARTED"] + ".tmp", "w") as f:
    f.write(str(rc))
os.replace(os.environ["TOOL_STARTED"] + ".tmp", os.environ["TOOL_STARTED"])
with open(os.environ["TOOL_GATE"]) as g:
    g.read()
sys.exit(rc)
'''


def open_gate(gate):
    """Let a gated tool that is blocked on the FIFO go; nothing when none is (no reader: ENXIO)."""
    try:
        fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.write(fd, b"go")
    os.close(fd)
    return True


class ProxyBase(unittest.TestCase):
    """A throwaway HOME with a library, the real gallery server and helpers; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-proxy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids,
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.proxies = os.path.join(self.home, ".cache/media-intake/proxies")
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def need_hevc(self, proxy=True):
        """Skip unless a synthetic HEVC clip can be made here and (proxy) a real proxy of it."""
        if not hevc_encoder():
            self.skipTest("no HEVC encoder (ffmpeg's libx265, or VA-API's hevc_vaapi) to make a synthetic clip with")
        if not H264:
            self.skipTest("this ffmpeg has no H.264 encoder (libx264 / libopenh264) to make a clip with")
        if proxy and not can_proxy():
            self.skipTest("no way to make a proxy here: ffmpeg can't decode HEVC and GStreamer has no GPU HEVC "
                          "decoder with an H.264 encoder")

    def fakes(self, *names, has=(), script=FAKE_TOOL):
        """Fake tools first on the server's PATH; returns a reader of their recorded runs."""
        d = os.path.join(self.tmp, "fakebin")
        os.makedirs(d, exist_ok=True)
        for n in names:
            p = os.path.join(d, n)
            with open(p, "w") as f:
                f.write(script)
            os.chmod(p, 0o700)
        log = os.path.join(self.tmp, "fake-tools.log")
        self.env.update(PATH=d + os.pathsep + self.env.get("PATH", ""), FAKE_TOOL_LOG=log, FAKE_GST_HAS=" ".join(has))

        def runs(name):
            if not os.path.exists(log):
                return []
            with open(log) as f:
                return [r[1:] for r in map(json.loads, f) if r[0] == name]
        return runs

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        return s

    def ids(self, s):
        return {i["n"]: i["id"] for i in s.get("/api/list?limit=500")["items"]}

    def proxy_of(self, path):
        return os.path.join(self.proxies, G.sha(path) + ".mp4")

    def cached(self, px):
        """Wait for the plain proxy to be in the cache: the streamed reply ends once the tool has written the last
        fragment, a moment before the remux puts the finished file there."""
        G.until(lambda: os.path.isfile(px), "the proxy to be cached")
        G.until(lambda: not [f for f in os.listdir(self.proxies) if f.startswith(".work-")], "the work folder to go")
        return px

    def assert_no_partial(self):
        if os.path.isdir(self.proxies):
            self.assertEqual([f for f in os.listdir(self.proxies) if not f.endswith((".mp4", ".failed"))], [])

    def stream(self, s, i):
        """Open GET /m/<i> as the player does (Range: bytes=0-) and return (connection, response) once its
        headers are in; the body is read by the caller."""
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=120)
        self.addCleanup(c.close)
        c.request("GET", f"/m/{i}", headers={"Host": f"127.0.0.1:{s.port}", "X-Token": s.token, "Range": "bytes=0-"})
        return c, c.getresponse()

    def body_is_a_playable_proxy(self, body):
        """The bytes a reply gave are an H.264 + AAC MP4 (ffprobe reads them)."""
        p = os.path.join(self.tmp, "got.mp4")
        with open(p, "wb") as f:
            f.write(body)
        self.assertEqual(streams(p), {"video": "h264", "audio": "aac"})



@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe missing")
class ProxyTest(ProxyBase):
    # ------------------------------------------------------------ made, cached, private, served
    def test_hevc_proxy_is_made_cached_private_and_served(self):
        self.need_hevc()
        hv = os.path.join(self.vids, "2026/2026-11/IMG_0001.MOV")
        av = os.path.join(self.vids, "2026/2026-11/clip.mp4")
        mkclip(hv, "hevc")
        mkclip(av, "h264")
        self.assertEqual(streams(hv)["video"], "hevc")
        orig = {p: G.sha(p) for p in (hv, av)}
        s = self.start()
        ids = self.ids(s)
        self.assertEqual(s.get(f"/api/item/{ids['IMG_0001.MOV']}")["conv"], "converting")  # the page says so
        self.assertIsNone(s.get(f"/api/item/{ids['clip.mp4']}")["conv"])
        r, body = s.req("GET", f"/m/{ids['IMG_0001.MOV']}")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "video/mp4"))
        self.body_is_a_playable_proxy(body)  # streamed while it was made, or the finished file: playable either way
        px = self.cached(self.proxy_of(hv))
        self.assertEqual(streams(px), {"video": "h264", "audio": "aac"})  # sound kept
        self.assertNotIn("moof", top_boxes(px))  # the cached copy is a plain MP4, not the fragments
        self.assertLess(top_boxes(px).index("moov"), top_boxes(px).index("mdat"))  # its index first
        self.assertEqual(oct(os.stat(px).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.proxies).st_mode & 0o777), "0o700")
        self.assert_no_partial()
        self.assertIsNone(s.get(f"/api/item/{ids['IMG_0001.MOV']}")["conv"])
        first = os.stat(px)
        # the player's Range requests get the cached proxy, not a new transcode
        r, part = s.req("GET", f"/m/{ids['IMG_0001.MOV']}", headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status, len(part), r.getheader("Content-Range")), (206, 100, f"bytes 0-99/{first.st_size}"))
        r, whole = s.req("GET", f"/m/{ids['IMG_0001.MOV']}")
        with open(px, "rb") as f:
            self.assertEqual((r.status, whole), (200, f.read()))  # the proxy is what the browser gets now
        self.assertEqual((os.stat(px).st_ino, os.stat(px).st_mtime_ns), (first.st_ino, first.st_mtime_ns))
        # an H.264 video is served as it is, with no proxy
        r, body = s.req("GET", f"/m/{ids['clip.mp4']}")
        with open(av, "rb") as f:
            self.assertEqual((r.status, body), (200, f.read()))
        self.assertFalse(os.path.exists(self.proxy_of(av)))
        self.assertEqual(sorted(os.listdir(self.proxies)), [orig[hv] + ".mp4"])
        for p, h in orig.items():  # originals are never written
            self.assertEqual(G.sha(p), h, p)
        # a restarted server finds the proxy in the cache
        s.stop()
        s2 = self.start()
        r, _ = s2.req("GET", f"/m/{self.ids(s2)['IMG_0001.MOV']}")
        self.assertEqual(r.status, 200)
        self.assertEqual(os.stat(px).st_mtime_ns, first.st_mtime_ns)

    # ------------------------------------------------------------ a failed transcode: today's behaviour
    def test_failed_transcode_serves_the_original(self):
        self.need_hevc(proxy=False)
        hv = os.path.join(self.vids, "2026/2026-11/IMG_0002.MOV")
        mkclip(hv, "hevc")
        h = G.sha(hv)
        runs = self.fakes("ffmpeg", "gst-inspect-1.0")  # ffmpeg fails; GStreamer has no HEVC decoder
        s = self.start()
        i = self.ids(s)["IMG_0002.MOV"]
        for _ in range(2):
            r, body = s.req("GET", f"/m/{i}")
            with open(hv, "rb") as f:
                self.assertEqual((r.status, r.getheader("Content-Type"), body), (200, "video/mp4", f.read()))
        transcodes = [a for a in runs("ffmpeg") if "-c:v" in a]
        self.assertEqual(len(transcodes), 1, runs("ffmpeg"))  # tried once, not on every request
        a = transcodes[0]
        self.assertEqual(a[a.index("-i") + 1], "file:" + hv)
        self.assertTrue(a[-1].startswith("file:" + self.proxies + os.sep), a[-1])
        self.assertFalse(os.path.exists(os.path.join(self.proxies, h + ".mp4")))
        self.assert_no_partial()
        self.assertEqual(G.sha(hv), h)
        self.assertIsNone(s.get(f"/api/item/{i}")["conv"])  # not "converting" again: it failed
        # the item and its actions still answer: the page shows "can't play" with Open in the system player
        self.assertEqual(s.get(f"/api/item/{i}")["name"], "IMG_0002.MOV")
        code, j = s.post("/api/keep", {"ids": [i]})
        self.assertEqual(code, 200, j)

    def test_no_ffmpeg_attempt_without_an_hevc_decoder(self):
        """Fedora's ffmpeg-free has an H.264 encoder but no HEVC decoder: its transcode could only fail, so it
        is not run at all (it was, before GStreamer, on every first open)."""
        self.need_hevc(proxy=False)
        hv = os.path.join(self.vids, "2026/2026-11/IMG_0003.MOV")
        mkclip(hv, "hevc")
        runs = self.fakes("ffmpeg", "gst-inspect-1.0")
        script = os.path.join(self.tmp, "fakebin", "ffmpeg")
        with open(script) as f:
            text = f.read()
        with open(script, "w") as f:  # the same fake, but its -decoders lists no HEVC
            f.write(text.replace('" VFS..D hevc                 fake"', '" V....D h264                 fake"'))
        s = self.start()
        r, body = s.req("GET", f"/m/{self.ids(s)['IMG_0003.MOV']}")
        self.assertEqual(r.status, 200)
        self.assertEqual([a for a in runs("ffmpeg") if "-c:v" in a], [])
        self.assertIn(["-hide_banner", "-decoders"], runs("ffmpeg"))

    def test_gstreamer_fallback_never_puts_the_file_name_in_the_pipeline(self):
        """Where ffmpeg can't decode HEVC (Fedora's ffmpeg-free), GStreamer's GPU decoder is tried, with sound and
        then without; when that fails too the original is served. gst-launch-1.0 re-parses its arguments as one
        pipeline, so a name holding "!", a space and a double quote must not reach it: it reads a link in a work
        folder under the cache, which resolves to the original, and writes beside it."""
        self.need_hevc(proxy=False)
        name = 'a ! fakesink "x" y.MOV'
        hv = os.path.join(self.vids, "2026/2026-11/b ! c", name)
        mkclip(hv, "hevc")
        orig = G.sha(hv)
        runs = self.fakes("ffmpeg", "gst-inspect-1.0", "gst-launch-1.0", has=("vah265dec", "x264enc"))
        s = self.start()
        r, body = s.req("GET", f"/m/{self.ids(s)[name]}")
        with open(hv, "rb") as f:
            self.assertEqual((r.status, body), (200, f.read()))
        launches, where = runs("gst-launch-1.0"), runs("gst-where")
        self.assertEqual(len(launches), 2, launches)
        for a, (src, out) in zip(launches, where):
            locs = [x[9:] for x in a if x.startswith("location=")]
            self.assertEqual(len(locs), 2, a)  # filesrc and filesink, nothing more
            work = os.path.dirname(locs[0])
            self.assertEqual(os.path.dirname(work), self.proxies)
            self.assertTrue(os.path.basename(work).startswith(".work-"), work)
            self.assertEqual(locs, [os.path.join(work, "in"), os.path.join(work, "out.mp4")])
            self.assertEqual(src, os.path.realpath(hv))  # the link reads the original
            self.assertEqual(out, os.path.realpath(os.path.join(work, "out.mp4")))
            for x in a:  # no part of the name in any argument
                for bit in ("!" + " fakesink", '"', "y.MOV", "b ! c"):
                    self.assertNotIn(bit, x, a)
            self.assertEqual(a.count("fakesink"), 0, a)
            for el in ("vah265dec", "vapostproc", "x264enc", "mp4mux", "filesink"):
                self.assertEqual(a.count(el), 1, (el, a))
            self.assertIn("video/x-raw,width=320,height=240", a)  # its own size: smaller than PROXY_PX
        self.assertIn("aacparse", launches[0])
        self.assertNotIn("aacparse", launches[1])
        self.assertEqual(os.listdir(self.proxies), [G.sha(hv) + ".failed"])  # work folder gone; the failure recorded
        self.assertEqual(G.sha(hv), orig)

    # ------------------------------------------------------------ a transcode held before it ends
    def held(self, *names):
        """HEVC clips <names> (each a second longer: another sha) and a server whose transcodes are held by the
        gated tool once each has written its whole proxy. Returns (server, {name: id}, {name: clip},
        started(), release()): started() waits for the next held transcode to have written everything,
        release() lets it end."""
        self.need_hevc()
        clips = {n: os.path.join(self.vids, "2026/2026-11", n) for n in names}
        for k, p in enumerate(clips.values()):
            mkclip(p, "hevc", seconds=2 + k)
        gate, flag = os.path.join(self.tmp, "tool-gate"), os.path.join(self.tmp, "tool-started")
        os.mkfifo(gate)
        real = dict(REAL_FFMPEG=shutil.which("ffmpeg"), REAL_GST_LAUNCH=shutil.which("gst-launch-1.0") or "")
        self.fakes("ffmpeg", "gst-launch-1.0", script=GATED_TOOL)
        self.env.update(real, TOOL_GATE=gate, TOOL_STARTED=flag)
        s = self.start()
        self.addCleanup(open_gate, gate)  # a failed assertion must not leave a tool blocked on it
        ids = self.ids(s)

        def started():
            G.until(lambda: os.path.exists(flag), "the transcode to be written")
            with open(flag) as f:
                self.assertEqual(f.read(), "0")  # the real tool made it
            os.remove(flag)

        def release():
            with open(gate, "w") as g:  # the gated tool is blocked opening it: this pairs at once
                g.write("go")
        return s, {n: ids[n] for n in names}, clips, started, release

    def test_the_player_gets_bytes_before_the_transcode_ends(self):
        """The reply starts on the first fragments, not after the whole transcode: its headers and bytes arrive
        while the transcode is still held, as a 200 with no length and no Range (the size isn't known yet).
        After it ends the cache holds the plain MP4, served with Range from then on."""
        s, ids, clips, started, release = self.held("IMG_0020.MOV")
        i, px = ids["IMG_0020.MOV"], self.proxy_of(clips["IMG_0020.MOV"])
        c, r = self.stream(s, i)
        self.assertEqual((r.status, r.getheader("Content-Type"), r.getheader("Content-Length"),
                          r.getheader("Accept-Ranges")), (200, "video/mp4", None, "none"))
        head = r.read(16)
        self.assertEqual(head[4:8], b"ftyp")
        started()
        self.assertFalse(os.path.exists(px))  # still being made: what the player got came first
        self.assertEqual(s.get(f"/api/item/{i}")["conv"], "converting")
        release()
        body = head + r.read()  # it ends when the transcode does
        self.body_is_a_playable_proxy(body)
        self.assertIn("moof", top_boxes(os.path.join(self.tmp, "got.mp4")))  # the fragments, as they were written
        self.cached(px)
        self.assertNotIn("moof", top_boxes(px))
        self.assert_no_partial()
        r, part = s.req("GET", f"/m/{i}", headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status, r.getheader("Content-Range")), (206, f"bytes 0-99/{os.path.getsize(px)}"))

    def test_another_video_is_served_at_once_while_a_proxy_is_watched(self):
        """While one video's proxy is being made and watched, opening another HEVC video is not held behind it:
        its original is served at once with no transcode started, and the page is told why. Once the first
        proxy is done, the next open of the other video makes its own."""
        s, ids, clips, started, release = self.held("IMG_0010.MOV", "IMG_0011.MOV")
        c, ra = self.stream(s, ids["IMG_0010.MOV"])
        started()
        self.assertEqual(s.get(f"/api/item/{ids['IMG_0011.MOV']}")["conv"], "waiting")
        r, body = s.req("GET", f"/m/{ids['IMG_0011.MOV']}")
        with open(clips["IMG_0011.MOV"], "rb") as f:
            self.assertEqual((r.status, body), (200, f.read()))
        self.assertFalse(os.path.lexists(self.proxy_of(clips["IMG_0011.MOV"])))
        release()
        self.body_is_a_playable_proxy(ra.read())  # the first video gets its proxy
        self.cached(self.proxy_of(clips["IMG_0010.MOV"]))
        got = {}
        t = threading.Thread(target=lambda: got.update(r=s.req("GET", f"/m/{ids['IMG_0011.MOV']}")))
        t.start()
        self.addCleanup(t.join, 120)
        started()
        release()
        t.join(120)
        self.assertFalse(t.is_alive(), "the /m/ request never returned")
        r, body = got["r"]
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "video/mp4"))
        self.body_is_a_playable_proxy(body)
        self.cached(self.proxy_of(clips["IMG_0011.MOV"]))
        self.assertEqual(sorted(os.listdir(self.proxies)), sorted(self.proxy_of(p)[-68:] for p in clips.values()))

    def test_a_transcode_nobody_watches_gives_way_to_the_next_video(self):
        """The player hung up (the large view moved on) while its video was still being converted: opening
        another video cancels that transcode (its process ends, nothing of it is kept) and converts this one."""
        s, ids, clips, started, release = self.held("IMG_0030.MOV", "IMG_0031.MOV")
        c, ra = self.stream(s, ids["IMG_0030.MOV"])
        started()
        self.assertEqual(s.get(f"/api/item/{ids['IMG_0031.MOV']}")["conv"], "waiting")
        ra.close()
        c.close()
        G.until(lambda: s.get(f"/api/item/{ids['IMG_0031.MOV']}")["conv"] == "converting",
                "the server to see the player hang up")
        c2, rb = self.stream(s, ids["IMG_0031.MOV"])
        self.assertEqual((rb.status, rb.getheader("Accept-Ranges")), (200, "none"))
        started()  # the second video's transcode: the first one's was stopped, not let through the gate
        release()
        self.body_is_a_playable_proxy(rb.read())
        self.cached(self.proxy_of(clips["IMG_0031.MOV"]))
        self.assertFalse(os.path.lexists(self.proxy_of(clips["IMG_0030.MOV"])))
        self.assertEqual(os.listdir(self.proxies), [self.proxy_of(clips["IMG_0031.MOV"])[-68:]])
        # the cancelled one is not marked as failed: its next open converts it
        self.assertEqual(s.get(f"/api/item/{ids['IMG_0030.MOV']}")["conv"], "converting")

    # ------------------------------------------------------------ trashed or erased while its proxy is made
    def test_a_transcode_whose_video_goes_into_a_trash_job_stops(self):
        """The video's trash job is running (a gated fake intake.py) while its proxy is being made and watched:
        the reply ends, the transcode is stopped and nothing of it stays in the cache; after the trash, nothing."""
        gate = os.path.join(self.tmp, "intake-gate")
        os.mkfifo(gate)
        fake = os.path.join(self.tmp, "gated_intake.py")
        with open(fake, "w") as f:
            f.write(G.GATED)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s, ids, clips, started, release = self.held("IMG_0006.MOV")
        i, hv = ids["IMG_0006.MOV"], clips["IMG_0006.MOV"]
        c, r = self.stream(s, i)
        started()
        code, j = s.post("/api/trash", {"ids": [i], "confirm": True})
        self.assertEqual(code, 200, j)
        G.until(lambda: any("waiting at the gate" in x for x in s.get("/api/job")["lines"]), "the trash to start")
        r.read()  # ends without the gate opening: the server stopped following it
        G.until(lambda: not os.listdir(self.proxies), "the stopped transcode's work folder to go")
        self.assertFalse(os.path.lexists(self.proxy_of(hv)))
        self.assertEqual(s.req("GET", f"/m/{i}")[0].status, 404)  # in a job: not served
        with open(gate, "w") as g:  # let the trash finish
            g.write("go")
        G.until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the trash to end")
        self.assertFalse(os.path.lexists(hv))
        self.assertEqual(os.listdir(self.proxies), [])

    def test_a_transcode_whose_video_was_trashed_leaves_nothing(self):
        """The real trash ends while the transcode is held (it found no proxy to delete yet): the transcode is
        stopped, nothing is left in the cache, and nothing is served."""
        s, ids, clips, started, release = self.held("IMG_0007.MOV")
        i, hv = ids["IMG_0007.MOV"], clips["IMG_0007.MOV"]
        c, r = self.stream(s, i)
        started()
        code, j = s.post("/api/trash", {"ids": [i], "confirm": True})
        self.assertEqual(code, 200, j)
        j = G.until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the trash to end")
        self.assertTrue(j["ok"], j["lines"])
        self.assertFalse(os.path.lexists(hv))
        r.read()
        G.until(lambda: not os.listdir(self.proxies), "the stopped transcode's work folder to go")
        self.assertEqual(s.req("GET", f"/m/{i}")[0].status, 404)  # the original is gone, and no proxy is served

    # ------------------------------------------------------------ clean-up: trash, forget, erase, verify-gone
    def test_trash_from_the_gallery_drops_the_proxy(self):
        self.need_hevc()
        hv = os.path.join(self.vids, "2026/2026-11/IMG_0004.MOV")
        mkclip(hv, "hevc")
        s = self.start()
        i = self.ids(s)["IMG_0004.MOV"]
        self.assertEqual(s.req("GET", f"/m/{i}")[0].status, 200)
        px = self.cached(self.proxy_of(hv))
        code, j = s.post("/api/trash", {"ids": [i], "confirm": True})
        self.assertEqual(code, 200, j)
        j = G.until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the trash to end")
        self.assertTrue(j["ok"], j["lines"])
        self.assertFalse(os.path.lexists(hv))
        self.assertFalse(os.path.lexists(px))
        self.assertEqual(s.req("GET", f"/m/{i}")[0].status, 404)

    def test_intake_cli_drops_proxies_and_verify_gone_reports_them(self):
        """trash and forget (and so erase) delete ~/.cache/media-intake/proxies/<sha>.mp4 without the gallery,
        and verify-gone counts one left behind as STILL PRESENT. Planted proxies: no transcode needed."""
        a = os.path.join(self.vids, "2026/2026-11/a.mp4")
        k = os.path.join(self.vids, "2026/2026-11/k.mp4")
        for n, p in enumerate((a, k)):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypmp42 synthetic %d" % n)
        sa, sk = G.sha(a), G.sha(k)
        os.makedirs(self.proxies)
        pa, pk = (os.path.join(self.proxies, h + ".mp4") for h in (sa, sk))
        fa, fk = (os.path.join(self.proxies, h + ".failed") for h in (sa, sk))  # no proxy could be made: recorded
        other = os.path.join(self.proxies, "f" * 64 + ".mp4")
        for p in (pa, pk, fa, fk, other):
            open(p, "wb").close()
        r = subprocess.run([G.PY, "-I", G.INTAKE, "trash", a, "--yes"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(pa) or os.path.exists(fa))
        r = subprocess.run([G.PY, "-I", G.INTAKE, "forget", k, "--yes"], env=self.env, capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(pk) or os.path.exists(fk))
        self.assertTrue(os.path.exists(other))  # only those shas
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            self.assertIn(sk, f.read())
        r = subprocess.run([G.PY, "-I", G.INTAKE, "verify-gone"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        open(pk, "wb").close()  # a proxy of erased content left behind
        os.chmod(pk, stat.S_IRUSR | stat.S_IWUSR)
        r = subprocess.run([G.PY, "-I", G.INTAKE, "verify-gone"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("gallery video proxies", r.stdout)
        os.remove(pk)  # and a failure record of erased content left behind, alone
        open(fk, "wb").close()
        os.chmod(fk, stat.S_IRUSR | stat.S_IWUSR)
        r = subprocess.run([G.PY, "-I", G.INTAKE, "verify-gone"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("gallery video proxies", r.stdout)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe missing")
class ProxiesCommandTest(ProxyBase):
    """media-intake proxies: the gallery's proxies made ahead of time, through the real cli.py, in the same
    throwaway HOME as ProxyTest."""

    CLI = os.path.join(os.path.dirname(G.INTAKE), "cli.py")

    def run_cli(self, *args):
        return subprocess.run([G.PY, "-I", self.CLI, "proxies", *args], env=self.env, capture_output=True,
                              text=True, timeout=600)

    def clips(self):
        """An HEVC clip, a copy of it (same content: one proxy), an erased HEVC clip and an H.264 clip."""
        d = os.path.join(self.vids, "2026/2026-11")
        hv, erased, av = (os.path.join(d, n) for n in ("IMG_0040.MOV", "IMG_0041.MOV", "clip.mp4"))
        mkclip(hv, "hevc")
        mkclip(erased, "hevc", seconds=2)
        mkclip(av, "h264")
        copy = os.path.join(self.vids, "2026/2026-10/IMG_0040.MOV")
        os.makedirs(os.path.dirname(copy))
        shutil.copyfile(hv, copy)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:  # erased: never shown, never converted
            f.write(f"2026-11-01 10:00 b1 sha256:{G.sha(erased)} size:{os.path.getsize(erased)}\n")
        return hv, erased, av

    def test_dry_run_counts_and_makes_nothing(self):
        self.need_hevc(proxy=False)
        self.clips()
        r = self.run_cli("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 to make, 0 made already, 1 play as they are (1 flagged or erased left out)", r.stdout)
        self.assertFalse(os.path.exists(self.proxies))

    def test_makes_each_proxy_once_and_resumes(self):
        self.need_hevc()
        hv, erased, av = self.clips()
        before = {p: G.sha(p) for p in (hv, erased, av)}
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("done: 1 made, 0 failed, 0 gone meanwhile, 0 made already", r.stdout)
        self.assertNotIn("IMG_0041", r.stdout)  # the erased clip is never named, nor converted
        px = self.proxy_of(hv)
        self.assertEqual(os.listdir(self.proxies), [os.path.basename(px)])
        self.assertEqual(streams(px), {"video": "h264", "audio": "aac"})
        self.assertNotIn("moof", top_boxes(px))
        self.assertEqual(oct(os.stat(px).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.proxies).st_mode & 0o777), "0o700")
        for p, h in before.items():  # originals are never written
            self.assertEqual(G.sha(p), h, p)
        r = self.run_cli()  # a second run finds it made
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 to make, 1 made already", r.stdout)
        # and the gallery serves it at once, as a plain file with Range
        s = self.start()
        i = self.ids(s)["IMG_0040.MOV"]
        self.assertIsNone(s.get(f"/api/item/{i}")["conv"])
        r, part = s.req("GET", f"/m/{i}", headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status, r.getheader("Content-Range")), (206, f"bytes 0-99/{os.path.getsize(px)}"))

    def test_a_favorite_is_proxied_like_any_video(self):
        """A starred HEVC clip (xmp:Rating 5 in its sidecar) gets its proxy too: favorites are shown, not held back."""
        self.need_hevc()
        hv, _, _ = self.clips()
        with open(hv + ".xmp", "w") as f:
            f.write(G.EMPTY)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-xmp:Rating=5", hv + ".xmp"], check=True)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("done: 1 made, 0 failed, 0 gone meanwhile, 0 made already", r.stdout)
        self.assertTrue(os.path.isfile(self.proxy_of(hv)))

    def test_a_failed_proxy_says_so_leaves_only_its_record_and_is_skipped_next_time(self):
        self.need_hevc(proxy=False)
        hv, _, _ = self.clips()
        self.fakes("ffmpeg", "gst-inspect-1.0")  # ffmpeg fails; GStreamer has no HEVC decoder
        r = self.run_cli()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("failed (the gallery serves it as it is)", r.stdout)
        self.assertIn("done: 0 made, 1 failed", r.stdout)
        record = self.proxy_of(hv)[:-4] + ".failed"
        self.assertEqual(os.listdir(self.proxies), [os.path.basename(record)])
        self.assertEqual((os.path.getsize(record), oct(os.stat(record).st_mode & 0o777)), (0, "0o600"))
        r = self.run_cli()  # not tried again: no hour lost on it every run
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 to make, 0 made already", r.stdout)
        self.assertIn("1 failed before, skipped (--retry-failed tries them again)", r.stdout)
        r = self.run_cli("--retry-failed")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 to make, 0 made already", r.stdout)
        self.assertIn("done: 0 made, 1 failed", r.stdout)

    def test_help_says_what_it_touches_and_deletes(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("usage: media-intake proxies "))
        self.assertIn("\nTouches: ", r.stdout)
        self.assertIn("\nDeletes: ", r.stdout)
        for line in r.stdout.splitlines():
            self.assertLessEqual(len(line), 80, line)


class ProxiesRunTest(unittest.TestCase):
    """proxies.main in-process with the library scan, the plan and make_proxy stood in for (no tool runs): a video
    trashed, erased or flagged while its proxy is made leaves no proxy and counts as gone meanwhile; one that
    still_shown refuses before its turn (a symlink, a file under the state folder, label.refused) gets none
    made at all."""

    def setUp(self):
        import gallery
        import proxies
        self.g, self.px = gallery, proxies
        self.tmp = tempfile.mkdtemp(prefix="gallery-proxies-run-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.lib = os.path.join(self.tmp, "lib")
        self.cache = os.path.join(self.tmp, "proxies")
        for d in (self.state, self.lib, self.cache):
            os.makedirs(d)
        self.made = []
        for target, name, value in ((gallery, "STATE", self.state), (gallery, "private_cache", lambda: None),
                                    (gallery, "scan_library", lambda: ([], 0))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def clip(self, path, data=b"an HEVC clip, as far as this test goes"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return dict(path=path, media="video", show="video")

    def erase_log(self, path):
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"2026-11-01 10:00 b1 sha256:{G.sha(path)} size:{os.path.getsize(path)}\n")

    def run_main(self, todo, meanwhile=None):
        """main() over todo [(item, proxy path)]; make_proxy writes the proxy, then meanwhile(src) runs (as if
        the video were trashed / erased while it was made). Returns (exit code, output)."""
        def make(src, dst, job=None):
            self.made.append(src)
            with open(dst, "wb") as f:
                f.write(b"proxy")
            if meanwhile:
                meanwhile(src)
            return True
        out = io.StringIO()
        with mock.patch.object(self.px, "plan", lambda items, retry_failed=False: (todo, 0, 0, 0)), \
                mock.patch.object(self.g, "make_proxy", make), contextlib.redirect_stdout(out):
            code = self.px.main([])
        return code, out.getvalue()

    def test_a_video_gone_while_its_proxy_is_made_leaves_no_proxy(self):
        def flag(src):  # scan flagged it meanwhile (a batch's scan.json names it)
            b = os.path.join(self.state, "batches", "b1")
            os.makedirs(b, exist_ok=True)
            with open(os.path.join(b, "plan.json"), "w") as f:
                json.dump([{"rel": "x", "sha": G.sha(src), "size": os.path.getsize(src)}], f)
            with open(os.path.join(b, "scan.json"), "w") as f:
                json.dump({"x": ["nudity"]}, f)
        for why, meanwhile in (("trashed", os.remove), ("erased", self.erase_log), ("flagged", flag)):
            with self.subTest(why=why):
                shutil.rmtree(os.path.join(self.state, "batches"), True)
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(self.state, "erase-log.txt"))
                it = self.clip(os.path.join(self.lib, f"{why}.MOV"), why.encode())
                dst = os.path.join(self.cache, why + ".mp4")
                code, out = self.run_main([(it, dst)], meanwhile)
                self.assertEqual(code, 0, out)
                self.assertIn("gone meanwhile, copy deleted", out)
                self.assertIn("done: 0 made, 0 failed, 1 gone meanwhile, 0 made already", out)
                self.assertFalse(os.path.exists(dst), "a proxy of a video that went stays")

    def test_still_shown_refusals_make_no_proxy(self):
        real = self.clip(os.path.join(self.tmp, "outside", "real.MOV"))
        link = os.path.join(self.lib, "link.MOV")
        os.symlink(real["path"], link)
        under = self.clip(os.path.join(self.state, "batches", "b1", "raw", "u.MOV"))
        erased = self.clip(os.path.join(self.lib, "erased.MOV"), b"erased before its turn")
        self.erase_log(erased["path"])
        for name, it in (("a symlink", dict(real, path=link)), ("under the state folder", under),
                         ("label.refused (erased)", erased)):
            with self.subTest(refused=name):
                self.assertFalse(self.px.still_shown(it["path"]))
                dst = os.path.join(self.cache, "x.mp4")
                code, out = self.run_main([(it, dst)])
                self.assertEqual(code, 0, out)
                self.assertIn("done: 0 made, 0 failed, 1 gone meanwhile, 0 made already", out)
                self.assertFalse(os.path.exists(dst))
        self.assertEqual(self.made, [])  # never even started
        self.assertTrue(self.px.still_shown(self.clip(os.path.join(self.lib, "plain.MOV"))["path"]))


class PlayableTest(unittest.TestCase):
    """gallery.App.playable in-process, with make_proxy replaced by a stand-in held on a threading.Event (and
    let go early by a cancel): which request starts, follows or cancels which transcode. No tool runs."""

    def setUp(self):
        import gallery
        import intake
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-playable-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proxies = os.path.join(self.tmp, "proxies")
        self.made, self.go, self.ok = [], threading.Event(), True
        self.started = {}
        self.addCleanup(self.go.set)  # a failed assertion must not leave a thread blocked

        def fake_make(src, dst, job=None):
            self.made.append(src)
            self.started[src].set()
            while not self.go.wait(0.05):  # the real one stops within PROXY_POLL of a cancel, too
                if job.cancel.is_set():
                    return False
            if self.ok:
                with open(dst, "wb") as f:
                    f.write(b"proxy of " + src.encode())
            return self.ok
        for target, name, value in ((gallery, "PROXIES", self.proxies), (gallery, "make_proxy", fake_make),
                                    (gallery, "private_cache", lambda: os.makedirs(self.proxies, exist_ok=True)),
                                    (intake, "PROXIES", self.proxies),  # gallery_thumb_forget's
                                    (intake, "THUMBS", os.path.join(self.tmp, "thumbs"))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def item(self, name, sha):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"original " + name.encode())
        self.started.setdefault(path, threading.Event())
        return dict(media="video", path=path, mime="video/quicktime", vcodec="hevc", sha=sha, gone=False)

    def px(self, sha):
        return os.path.join(self.proxies, sha + ".mp4")

    def job_of(self, app, it):
        """playable(it), which must hand back a running transcode (with a reader's place taken)."""
        got, mime = app.playable(it)
        self.assertIsInstance(got, self.g.ProxyJob)
        self.assertEqual(mime, "video/mp4")
        return got

    def test_requests_for_the_same_video_follow_its_one_transcode(self):
        """A second request for the sha being converted (the player's Range request, another page, or a
        duplicate file with the same content) follows that transcode: one transcode, not two, and the cached
        proxy for every request after it."""
        a, a2 = self.item("a.MOV", "a" * 64), self.item("a-copy.MOV", "a" * 64)
        app = self.g.App([a, a2])
        job = self.job_of(app, a)
        self.assertTrue(self.started[a["path"]].wait(60))
        self.assertIs(self.job_of(app, a), job)
        self.assertIs(self.job_of(app, a2), job)
        self.assertEqual(job.readers, 3)
        self.go.set()
        self.assertTrue(job.wait_done(60))
        self.assertTrue(job.ok)
        self.assertEqual(self.made, [a["path"]])
        self.assertIsNone(app.proxy_job)
        self.assertEqual(app.playable(a), (self.px("a" * 64), "video/mp4"))
        self.assertEqual(app.playable(a2), (self.px("a" * 64), "video/mp4"))

    def test_another_video_gets_its_original_while_one_is_watched(self):
        """One transcode at a time: while one is being made and followed, a request for another video is
        answered at once with its original (not marked as failed); once the first is done, the other's proxy
        is made on its next request."""
        a, b = self.item("a.MOV", "a" * 64), self.item("b.MOV", "b" * 64)
        app = self.g.App([a, b])
        job = self.job_of(app, a)
        self.assertTrue(self.started[a["path"]].wait(60))
        self.assertEqual(app.proxy_state(b), "waiting")
        self.assertEqual(app.playable(b), (b["path"], "video/quicktime"))
        self.assertNotIn("noproxy", b)
        self.assertFalse(job.cancel.is_set())
        self.go.set()
        self.assertTrue(job.wait_done(60) and job.ok)
        job.leave()
        self.assertEqual(app.proxy_state(b), "converting")
        jb = self.job_of(app, b)
        self.assertTrue(jb.wait_done(60) and jb.ok)
        self.assertEqual(self.made, [a["path"], b["path"]])

    def test_a_transcode_nobody_follows_is_cancelled_for_another_video(self):
        """The player left a's transcode (it hung up): opening b cancels it, so b is converted now, and a is not
        marked as failed (its next open converts it again)."""
        a, b = self.item("a.MOV", "a" * 64), self.item("b.MOV", "b" * 64)
        app = self.g.App([a, b])
        ja = self.job_of(app, a)
        self.assertTrue(self.started[a["path"]].wait(60))
        ja.leave()
        self.assertEqual(app.proxy_state(b), "converting")
        jb = self.job_of(app, b)
        self.assertIsNot(jb, ja)
        self.assertTrue(ja.cancel.is_set())
        self.assertTrue(ja.wait_done(60))
        self.assertFalse(ja.ok)
        self.assertNotIn("noproxy", a)
        self.assertFalse(os.path.exists(self.px("a" * 64)))
        self.assertIs(app.proxy_job, jb)
        self.go.set()
        self.assertTrue(jb.wait_done(60) and jb.ok)
        self.assertEqual(self.made, [a["path"], b["path"]])

    def test_a_failed_transcode_is_not_tried_again(self):
        """When the transcode fails, its followers see it end without a proxy, the item gets its original from
        then on (the page says the browser can't play it), and the slot is free for another video."""
        self.ok = False
        a, b = self.item("a.MOV", "a" * 64), self.item("b.MOV", "b" * 64)
        app = self.g.App([a, b])
        job = self.job_of(app, a)
        self.go.set()
        self.assertTrue(job.wait_done(60))
        self.assertFalse(job.ok)
        self.assertTrue(a.get("noproxy"))
        self.assertIsNone(app.proxy_job)
        self.assertEqual(app.playable(a), (a["path"], "video/quicktime"))
        self.assertIsNone(app.proxy_state(a))
        jb = self.job_of(app, b)
        self.assertTrue(jb.wait_done(60))
        self.assertEqual(self.made, [a["path"], b["path"]])
        self.assertEqual(os.listdir(self.proxies), [])

    def test_a_proxy_finished_after_its_video_went_into_a_job_is_deleted(self):
        """The transcode ends just after its video went into a Trash or Erase job (busy) or went: the proxy it
        made is deleted at once, and none is served."""
        for flag in ("busy", "gone"):
            with self.subTest(flag):
                self.go.clear()
                sha = ("c" if flag == "busy" else "d") * 64
                it = self.item(flag + ".MOV", sha)
                app = self.g.App([it])
                job = self.job_of(app, it)
                self.assertTrue(self.started[it["path"]].wait(60))
                it[flag] = True
                self.go.set()
                self.assertTrue(job.wait_done(60))
                self.assertFalse(job.ok)
                self.assertFalse(os.path.exists(self.px(sha)))


class ProxyJobTest(unittest.TestCase):
    """gallery.ProxyJob, what a streaming /m/ reply follows, driven by hand: no tool, no server."""

    def setUp(self):
        import gallery
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-proxyjob-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.job = gallery.ProxyJob("a" * 64, dict(path="/x", gone=False), os.path.join(self.tmp, "a.mp4"))

    def later(self, fn):
        t = threading.Thread(target=fn)
        t.start()
        self.addCleanup(t.join, 60)
        return t

    def test_a_reader_waits_for_the_first_bytes_and_follows_its_attempt(self):
        out = os.path.join(self.tmp, "out.mp4")
        got = {}
        t = self.later(lambda: got.update(r=self.job.open_stream()))
        self.job.started(0, out)
        self.assertTrue(t.is_alive())  # no bytes yet: still waiting
        with open(out, "wb") as f:
            f.write(b"first")
        self.job.grew(5)
        t.join(60)
        f, attempt = got["r"]
        self.addCleanup(f.close)
        self.assertEqual((attempt, f.read()), (0, b"first"))
        with open(out, "ab") as w:
            w.write(b" more")
        self.later(lambda: self.job.grew(10))
        self.assertTrue(self.job.more(0))  # it grew (or PROXY_POLL passed): read on
        self.assertEqual(f.read(), b" more")
        # that attempt failed and the next one began: the reader of the first one stops
        os.remove(out)
        self.job.started(1, out)
        self.assertFalse(self.job.more(0))

    def test_an_ended_job_stops_its_readers_and_opens_no_more(self):
        out = os.path.join(self.tmp, "out.mp4")
        with open(out, "wb") as f:
            f.write(b"x")
        self.job.started(0, out)
        self.job.grew(1)
        f, attempt = self.job.open_stream()
        f.close()
        self.job.end(True)
        self.assertFalse(self.job.more(attempt))
        self.assertIsNone(self.job.open_stream())
        self.assertTrue(self.job.wait_done(0))

    def test_a_reader_whose_file_went_follows_the_next_attempt(self):
        """Attempt 0 wrote bytes, failed and its file went before the reader could open it: the reader waits for
        attempt 1 instead of for the whole job (which would pin it, uncancellable, for the transcode)."""
        gone, out = os.path.join(self.tmp, "gone.mp4"), os.path.join(self.tmp, "out1.mp4")
        self.job.started(0, gone)
        self.job.grew(5)  # it never exists: open fails
        got = {}
        t = self.later(lambda: got.update(r=self.job.open_stream()))
        with open(out, "wb") as f:
            f.write(b"second")
        self.job.started(1, out)
        self.job.grew(6)
        t.join(60)
        self.assertFalse(t.is_alive())
        f, attempt = got["r"]
        self.addCleanup(f.close)
        self.assertEqual((attempt, f.read()), (1, b"second"))

    def test_the_reply_ends_once_the_tool_has_written_everything(self):
        """The remux after the tool exits may take a while: the reader doesn't wait for it."""
        out = os.path.join(self.tmp, "out.mp4")
        with open(out, "wb") as f:
            f.write(b"x")
        self.job.started(0, out)
        self.job.grew(1)
        self.job.wrote()
        self.assertFalse(self.job.more(0))
        self.assertFalse(self.job.done)

    def test_readers_keep_it_from_being_idle(self):
        self.assertTrue(self.job.idle())
        self.job.join()
        self.job.join()
        self.job.leave()
        self.assertFalse(self.job.idle())
        self.job.leave()
        self.assertTrue(self.job.idle())

    def test_it_stops_when_cancelled_or_its_video_goes_into_a_job(self):
        self.assertFalse(self.job.stopped())
        self.job.it["busy"] = True
        self.assertTrue(self.job.stopped())
        self.job.it["busy"] = False
        self.job.it["gone"] = True
        self.assertTrue(self.job.stopped())
        self.job.it["gone"] = False
        self.job.cancel.set()
        self.assertTrue(self.job.stopped())


class ProxyCommandsTest(unittest.TestCase):
    """gallery.proxy_commands in-process, with the tool lookups patched: no tool runs."""

    def setUp(self):
        import gallery
        import scan_sensitive
        self.g = gallery
        self.patch(gallery, "ffmpeg_encoder", lambda names: "libx264")
        self.patch(gallery, "ffmpeg_decodes_hevc", lambda: True)
        self.patch(gallery, "gst_element", lambda names: "x264enc")
        self.patch(scan_sensitive, "_gst_hevc_decoder", lambda: "vah265dec")

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def test_a_hostile_name_stays_out_of_the_pipeline(self):
        src = '/lib/a ! filesink location=/tmp/x "q" b.MOV'
        work = "/home/u/.cache/media-intake/proxies/.work-abc123"
        cmds = self.g.proxy_commands(src, work)
        self.assertEqual([c[0] for c in cmds], ["ffmpeg", "gst-launch-1.0", "gst-launch-1.0"])
        ff = cmds[0]
        self.assertEqual((ff[ff.index("-i") + 1], ff[-1]), ("file:" + src, "file:" + work + "/out.mp4"))
        for c in cmds[1:]:
            self.assertEqual([x for x in c if x.startswith("location=")],
                             ["location=" + work + "/in", "location=" + work + "/out.mp4"])
            self.assertEqual(c.count("filesink"), 1)
            self.assertFalse([x for x in c if '"' in x or " " in x or "b.MOV" in x], c)

    def test_every_command_writes_fragments_the_player_can_start_on(self):
        cmds = self.g.proxy_commands("/lib/v.MOV", "/home/u/.cache/media-intake/proxies/.work-1", (1920, 1080))
        ff = cmds[0]
        self.assertEqual(ff[ff.index("-movflags") + 1], "+frag_keyframe+empty_moov+default_base_moof")
        self.assertEqual(ff[ff.index("-frag_duration") + 1], str(self.g.PROXY_FRAG_MS * 1000))
        self.assertNotIn("+faststart", ff)  # that needs the whole file: the remux does it afterwards
        for c in cmds[1:]:
            mux = c.index("mp4mux")
            self.assertIn(f"fragment-duration={self.g.PROXY_FRAG_MS}", c[mux:c.index("filesink")])

    def test_the_gpu_encoder_comes_first_and_scales_on_the_gpu(self):
        """vah264enc (VA-API) before a CPU encoder, each with and without the sound; with vah265dec the frames
        are scaled (vapostproc) and encoded without leaving the GPU, while a CPU encoder gets them in memory."""
        self.patch(self.g, "gst_element", lambda names: names[0])
        self.patch(self.g, "ffmpeg_decodes_hevc", lambda: False)
        cmds = self.g.proxy_commands("/lib/v.MOV", "/home/u/.cache/media-intake/proxies/.work-1", (1920, 1080))
        encs = [next(e for e in ("vah264enc", "x264enc") if e in c) for c in cmds]
        self.assertEqual(encs, ["vah264enc", "vah264enc", "x264enc", "x264enc"])
        self.assertEqual(["aacparse" in c for c in cmds], [True, False, True, False])
        for c in cmds[:2]:
            self.assertIn("video/x-raw(memory:VAMemory),width=1920,height=1080", c)
            self.assertNotIn("videoconvert", c)
        for c in cmds[2:]:
            self.assertIn("video/x-raw,width=1920,height=1080", c)
            self.assertLess(c.index("vapostproc"), c.index("videoconvert"))
            self.assertLess(c.index("videoconvert"), c.index("x264enc"))

    def test_vulkan_frames_are_scaled_on_the_cpu(self):
        import scan_sensitive
        self.patch(scan_sensitive, "_gst_hevc_decoder", lambda: "vulkanh265device1dec")
        self.patch(self.g, "ffmpeg_decodes_hevc", lambda: False)
        cmds = self.g.proxy_commands("/lib/v.MOV", "/home/u/.cache/media-intake/proxies/.work-1", (1280, 720))
        for c in cmds:
            self.assertEqual(c[c.index("vulkanh265device1dec") + 2:c.index("x264enc")],
                             ["vulkandownload", "!", "videoconvert", "!", "videoscale", "!",
                              "video/x-raw,width=1280,height=720", "!"])

    def test_no_ffmpeg_command_without_an_hevc_decoder(self):
        self.patch(self.g, "ffmpeg_decodes_hevc", lambda: False)
        cmds = self.g.proxy_commands("/lib/v.MOV", "/home/u/.cache/media-intake/proxies/.work-1")
        self.assertEqual([c[0] for c in cmds], ["gst-launch-1.0", "gst-launch-1.0"])
        self.assertFalse([x for c in cmds for x in c if x.startswith("video/x-raw")])  # size unknown: kept

    def test_an_unsafe_work_path_skips_gstreamer(self):
        import scan_sensitive
        scan_sensitive._GST_SKIP_NOTED.clear()
        self.addCleanup(scan_sensitive._GST_SKIP_NOTED.clear)
        works = ('/home/a b/.cache/media-intake/proxies/.work-1', "/home/a!b/.work-1", '/h/"q"/.work-1',
                 "/home/a!b/.work-2")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for work in works:
                self.assertEqual([c[0] for c in self.g.proxy_commands("/lib/v.MOV", work)], ["ffmpeg"], work)
        # once per proxies folder (the second "/home/a!b" clip adds none), the folder only, never the clip
        self.assertEqual([ln.split("are): ", 1)[1] for ln in err.getvalue().splitlines()],
                         [f"{os.path.dirname(w)}; HEVC clips get no GStreamer proxy" for w in works[:3]])
        self.assertNotIn("v.MOV", err.getvalue())

    def test_no_skip_note_without_a_gstreamer_decoder(self):
        import scan_sensitive
        scan_sensitive._GST_SKIP_NOTED.clear()
        self.addCleanup(scan_sensitive._GST_SKIP_NOTED.clear)
        err = io.StringIO()
        with mock.patch.object(scan_sensitive, "_gst_hevc_decoder", lambda: None), contextlib.redirect_stderr(err):
            self.assertEqual([c[0] for c in self.g.proxy_commands("/lib/v.MOV", "/home/a b/.work-1")], ["ffmpeg"])
        self.assertEqual(err.getvalue(), "")  # nothing would have been tried, so nothing to explain

    def test_proxy_size(self):
        size = self.g.proxy_size
        self.assertEqual(size(3840, 2160), (1920, 1080))  # 4K: halved
        self.assertEqual(size(2160, 3840), (1080, 1920))  # stored portrait
        self.assertEqual(size(1280, 720), (1280, 720))  # never scaled up
        self.assertEqual(size(1921, 1081), (1920, 1080))  # both even
        self.assertEqual(size(4032, 3024), (1920, 1440))
        for w, h in ((0, 0), (None, 1080), (1920, 0)):
            self.assertIsNone(size(w, h))


# Stand-in proxy commands for MakeProxyTest: each is `python3 -I -c CODE OUT LOG NAME`; it logs NAME when it
# starts, then does what its NAME says to OUT (the work folder's out.mp4).
STAND_IN = r"""
import sys, time
out, log, name = sys.argv[1:4]
with open(log, "a") as f:
    f.write(name + "\n")
if name == "hang-after-bytes":  # a GPU encoder that writes its first fragment, then stops for good
    with open(out, "ab") as f:
        f.write(b"x" * 100)
    time.sleep(120)
elif name == "hang-silent":  # one that never writes a byte
    time.sleep(120)
elif name == "slow":  # a slow encoder that keeps writing: never stalled
    for _ in range(8):
        with open(out, "ab") as f:
            f.write(b"x")
        time.sleep(0.4)
    with open(out, "ab") as f:
        f.write(b"h264")
elif name == "endless":  # one still writing when the deadline comes
    while True:
        with open(out, "ab") as f:
            f.write(b"x")
        time.sleep(0.1)
elif name == "ok":
    with open(out, "wb") as f:
        f.write(b"h264 proxy")
else:  # "fail"
    sys.exit(1)
"""


class StubJob:
    """What make_proxy asks of a ProxyJob; stop_after: how many polls until it reads as cancelled."""
    def __init__(self, stop_after=None):
        self.polls, self.stop_after = 0, stop_after

    def stopped(self):
        self.polls += 1
        return self.stop_after is not None and self.polls > self.stop_after

    def started(self, n, out):
        pass

    def grew(self, size):
        pass

    def wrote(self):
        pass


class MakeProxyTest(unittest.TestCase):
    """gallery.make_proxy in-process with proxy_commands, the codec probe and the remux stood in for (small
    Python stand-ins, no media tool runs): a stalled attempt gives way to the next command within PROXY_STALL;
    one still writing at PROXY_TIMEOUT ends it; a failure is recorded (<sha>.failed, 0600) and a later success
    clears it; a cancel records nothing."""

    def setUp(self):
        import gallery
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-make-proxy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "proxies")
        os.makedirs(self.cache, mode=0o700)
        self.src = os.path.join(self.tmp, "clip.MOV")
        with open(self.src, "wb") as f:
            f.write(b"an HEVC clip, as far as this test goes")
        self.dst = os.path.join(self.cache, "a" * 64 + ".mp4")
        self.record = os.path.join(self.cache, "a" * 64 + ".failed")
        self.log = os.path.join(self.tmp, "ran.log")
        self.names = []

        def codec(path):
            with open(path, "rb") as f:
                return "h264" if b"h264" in f.read() else None
        commands = lambda src, work, size=None: [  # noqa: E731
            [sys.executable, "-I", "-c", STAND_IN, os.path.join(work, "out.mp4"), self.log, n] for n in self.names]
        for name, value in (("proxy_commands", commands), ("video_codec", codec), ("video_info", lambda p: {}),
                            ("remux_proxy", lambda src, dst: False), ("PROXY_STALL", 1.5), ("PROXY_POLL", 0.05)):
            p = mock.patch.object(gallery, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_make(self, *names, job=None):
        self.names = list(names)
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.log)
        t0 = time.monotonic()
        ok = self.g.make_proxy(self.src, self.dst, job)
        took = time.monotonic() - t0
        with open(self.log) as f:
            ran = f.read().split()
        self.assertEqual([f for f in os.listdir(self.cache) if f.startswith(".work-")], [])  # work folder gone
        return ok, ran, took

    def test_a_stalled_attempt_gives_way_to_the_next_command(self):
        for hang in ("hang-after-bytes", "hang-silent"):
            with self.subTest(hang=hang):
                ok, ran, took = self.run_make(hang, "ok")
                self.assertTrue(ok)
                self.assertEqual(ran, [hang, "ok"])
                self.assertLess(took, 30)  # PROXY_STALL (1.5 s here), not the 120 s the stand-in hangs for
                with open(self.dst, "rb") as f:
                    self.assertEqual(f.read(), b"h264 proxy")
                self.assertFalse(os.path.exists(self.record))
                os.remove(self.dst)

    def test_an_attempt_that_keeps_writing_is_not_stalled(self):
        ok, ran, _ = self.run_make("slow", "ok")  # about 3 s in all, with no gap over PROXY_STALL
        self.assertTrue(ok)
        self.assertEqual(ran, ["slow"])

    def test_every_command_failing_is_recorded_and_a_later_success_clears_it(self):
        ok, ran, _ = self.run_make("hang-silent", "fail")
        self.assertFalse(ok)
        self.assertEqual(ran, ["hang-silent", "fail"])
        self.assertFalse(os.path.exists(self.dst))
        self.assertEqual((os.path.getsize(self.record), oct(os.stat(self.record).st_mode & 0o777)), (0, "0o600"))
        self.assertTrue(self.g.proxy_failed(self.dst))
        ok, _, _ = self.run_make("fail")  # failing again keeps the one record
        self.assertFalse(ok)
        self.assertEqual(sorted(os.listdir(self.cache)), [os.path.basename(self.record)])
        ok, _, _ = self.run_make("ok")  # --retry-failed, and it worked this time
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.record))

    def test_a_record_that_cannot_be_written_is_still_a_plain_failure(self):
        nowhere = os.path.join(self.tmp, "no-such-folder", "x.failed")
        with mock.patch.object(self.g, "proxy_failed_path", lambda dst: nowhere):
            ok, ran, _ = self.run_make("fail")
        self.assertFalse(ok)  # no OSError out of make_proxy: proxies goes on to the next video
        self.assertEqual(ran, ["fail"])
        self.assertEqual(os.listdir(self.cache), [])

    def test_still_writing_at_the_deadline_ends_it_without_the_next_command(self):
        with mock.patch.object(self.g, "PROXY_TIMEOUT", 1.0):
            ok, ran, took = self.run_make("endless", "ok")
        self.assertFalse(ok)
        self.assertEqual(ran, ["endless"])  # the next command would take as long
        self.assertLess(took, 30)
        self.assertTrue(os.path.exists(self.record))

    def test_a_cancel_records_nothing(self):
        ok, ran, _ = self.run_make("hang-silent", "ok", job=StubJob(stop_after=3))
        self.assertFalse(ok)
        self.assertEqual(ran, ["hang-silent"])
        self.assertEqual(os.listdir(self.cache), [])

    def test_the_gallery_serves_the_original_of_a_recorded_failure(self):
        it = dict(path=self.src, media="video", mime="video/quicktime", vcodec="hevc", sha="a" * 64)
        with mock.patch.object(self.g, "PROXIES", self.cache):
            app = self.g.App([it])
            self.assertEqual(app.proxy_needed(it), self.dst)
            open(self.record, "wb").close()
            self.assertIsNone(app.proxy_needed(it))
            self.assertEqual(app.proxy_needed(it, failed=True), self.dst)
            self.assertEqual(app.playable(it), (self.src, "video/quicktime"))
            self.assertIsNone(app.proxy_state(it))


class PlanTest(unittest.TestCase):
    """proxies.plan with the gallery's App stood in for (no ffprobe): a video whose proxy failed before is
    counted and skipped, or planned again with retry_failed; a made one is counted as made."""

    def test_recorded_failures_are_skipped_unless_retried(self):
        import gallery
        import proxies
        tmp = tempfile.mkdtemp(prefix="gallery-proxies-plan-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dsts = {n: os.path.join(tmp, h * 64 + ".mp4") for n, h in (("made", "a"), ("failed", "b"), ("new", "c"))}
        open(dsts["made"], "wb").close()
        open(dsts["failed"][:-4] + ".failed", "wb").close()
        items = [dict(path=n, media="video", show="video") for n in dsts]

        class App:
            def __init__(self, items):
                pass

            def proxy_needed(self, it, failed=False):
                return dsts[it["path"]]
        with mock.patch.object(gallery, "App", App):
            todo, have, plain, failed = proxies.plan(items)
            self.assertEqual(([it["path"] for it, _ in todo], have, plain, failed), (["new"], 1, 0, 1))
            todo, have, plain, failed = proxies.plan(items, retry_failed=True)
            self.assertEqual(([it["path"] for it, _ in todo], have, plain, failed), (["failed", "new"], 1, 0, 0))


if __name__ == "__main__":
    unittest.main()
