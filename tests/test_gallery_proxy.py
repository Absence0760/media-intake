#!/usr/bin/env python3
"""The gallery's H.264 proxy for a video the browser can't play (HEVC), and its clean-up.

  python3 -I -m unittest discover -s tests -p test_gallery_proxy.py

Synthetic clips in a throwaway HOME (tests/support.py): an HEVC .MOV with AAC sound made with
ffmpeg's libx265 (the HEVC cases skip, naming the reason, when this ffmpeg has no libx265 or no
H.264 encoder) and an H.264 .mp4. The real gallery server serves them; trash, forget and
verify-gone run the real intake.py. Failure paths put fake ffmpeg / GStreamer tools first on the
server's PATH (ffprobe stays the real one), which record their arguments and fail.
"""
import json, os, shutil, stat, subprocess, sys, tempfile, threading, unittest
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


def mkclip(path, codec, seconds=1):
    """A small test pattern with a sine tone: codec "hevc" (libx265, tagged hvc1 as phones do) or "h264"."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    v = ["-c:v", "libx265", "-x265-params", "log-level=none", "-tag:v", "hvc1"] if codec == "hevc" \
        else ["-c:v", H264]
    fmt = ["-f", "mov"] if path.lower().endswith(".mov") else ["-f", "mp4"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-f", "lavfi", "-i",
                    f"testsrc=size=320x240:rate=10:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-shortest", *v, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", *fmt, "file:" + path], check=True)


def streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", "file:" + path],
                       capture_output=True, text=True, check=True)
    return {s["codec_type"]: s["codec_name"] for s in json.loads(r.stdout)["streams"]}


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
sys.exit(1)
'''

# ffmpeg whose transcode is held before it exits: it runs the real ffmpeg (the proxy is written, the original
# read), says so (a file, renamed into place), then blocks opening the FIFO at FFMPEG_GATE until the test opens
# it, and exits as the real one did. So the video can be trashed after it was read and before the gallery keeps
# the proxy. Other runs go straight to the real ffmpeg.
GATED_FFMPEG = r'''#!/usr/bin/env python3
import os, subprocess, sys
real = os.environ["REAL_FFMPEG"]
if "-movflags" not in sys.argv:
    os.execv(real, [real, *sys.argv[1:]])
rc = subprocess.run([real, *sys.argv[1:]]).returncode
with open(os.environ["FFMPEG_STARTED"] + ".tmp", "w") as f:
    f.write(str(rc))
os.replace(os.environ["FFMPEG_STARTED"] + ".tmp", os.environ["FFMPEG_STARTED"])
with open(os.environ["FFMPEG_GATE"]) as g:
    g.read()
sys.exit(rc)
'''


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe missing")
class ProxyTest(unittest.TestCase):
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

    def need_hevc(self):
        if not HAVE_X265:
            self.skipTest("this ffmpeg has no libx265 encoder, so no synthetic HEVC clip can be made")
        if not H264:
            self.skipTest("this ffmpeg has no H.264 encoder (libx264 / libopenh264) to make a proxy with")

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

    def assert_no_partial(self):
        if os.path.isdir(self.proxies):
            self.assertEqual([f for f in os.listdir(self.proxies) if not f.endswith(".mp4")], [])

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
        r, body = s.req("GET", f"/m/{ids['IMG_0001.MOV']}")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "video/mp4"))
        px = self.proxy_of(hv)
        with open(px, "rb") as f:
            self.assertEqual(body, f.read())  # the proxy is what the browser got
        self.assertEqual(streams(px), {"video": "h264", "audio": "aac"})  # sound kept
        self.assertEqual(oct(os.stat(px).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.proxies).st_mode & 0o777), "0o700")
        self.assert_no_partial()
        first = os.stat(px)
        # the player's Range requests get the cached proxy, not a new transcode
        r, part = s.req("GET", f"/m/{ids['IMG_0001.MOV']}", headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status, len(part), r.getheader("Content-Range")), (206, 100, f"bytes 0-99/{first.st_size}"))
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
        self.need_hevc()
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
        transcodes = [a for a in runs("ffmpeg") if "-movflags" in a]
        self.assertEqual(len(transcodes), 1, runs("ffmpeg"))  # tried once, not on every request
        a = transcodes[0]
        self.assertEqual(a[a.index("-i") + 1], "file:" + hv)
        self.assertTrue(a[-1].startswith("file:" + self.proxies + os.sep), a[-1])
        self.assertFalse(os.path.exists(os.path.join(self.proxies, h + ".mp4")))
        self.assert_no_partial()
        self.assertEqual(G.sha(hv), h)
        # the item and its actions still answer: the page shows "can't play" with Open in the system player
        self.assertEqual(s.get(f"/api/item/{i}")["name"], "IMG_0002.MOV")
        code, j = s.post("/api/keep", {"ids": [i]})
        self.assertEqual(code, 200, j)

    def test_gstreamer_fallback_never_puts_the_file_name_in_the_pipeline(self):
        """Where ffmpeg can't decode HEVC (Fedora's ffmpeg-free), GStreamer's GPU decoder is tried, with sound and
        then without; when that fails too the original is served. gst-launch-1.0 re-parses its arguments as one
        pipeline, so a name holding "!", a space and a double quote must not reach it: it reads a link in a work
        folder under the cache, which resolves to the original, and writes beside it."""
        self.need_hevc()
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
        self.assertIn("aacparse", launches[0])
        self.assertNotIn("aacparse", launches[1])
        self.assertEqual(os.listdir(self.proxies), [])  # work folder gone
        self.assertEqual(G.sha(hv), orig)

    # ------------------------------------------------------------ trashed or erased while its proxy is made
    def held_transcode(self, name):
        """A server whose transcode of <name> (an HEVC clip) is held by the gated ffmpeg once it has written it.
        Returns (server, id, the clip, its proxy path, release(), result()): release lets the transcode run on,
        result() waits for the /m/ request and gives (status, body)."""
        self.need_hevc()
        hv = os.path.join(self.vids, "2026/2026-11", name)
        mkclip(hv, "hevc")
        px = self.proxy_of(hv)
        gate, started = os.path.join(self.tmp, "ffmpeg-gate"), os.path.join(self.tmp, "ffmpeg-started")
        os.mkfifo(gate)
        real = shutil.which("ffmpeg")
        self.fakes("ffmpeg", script=GATED_FFMPEG)
        self.env.update(REAL_FFMPEG=real, FFMPEG_GATE=gate, FFMPEG_STARTED=started)
        s = self.start()
        i = self.ids(s)[name]
        got = {}
        t = threading.Thread(target=lambda: got.update(r=s.req("GET", f"/m/{i}")))
        t.start()
        self.addCleanup(t.join, 120)
        G.until(lambda: os.path.exists(started), "the transcode to be written")
        with open(started) as f:
            self.assertEqual(f.read(), "0")  # the real ffmpeg made it

        def release():
            with open(gate, "w") as g:  # the gated ffmpeg is blocked opening it: this pairs at once
                g.write("go")

        def result():
            t.join(120)
            self.assertFalse(t.is_alive(), "the /m/ request never returned")
            r, body = got["r"]
            return r.status, body
        return s, i, hv, px, release, result

    def test_proxy_finished_while_its_video_is_in_a_job_is_dropped(self):
        """The video's trash job is still running (a gated fake intake.py) when the transcode ends: the fresh
        proxy is deleted at once and the original is served, as before proxies."""
        gate = os.path.join(self.tmp, "intake-gate")
        os.mkfifo(gate)
        fake = os.path.join(self.tmp, "gated_intake.py")
        with open(fake, "w") as f:
            f.write(G.GATED)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s, i, hv, px, release, result = self.held_transcode("IMG_0006.MOV")
        code, j = s.post("/api/trash", {"ids": [i], "confirm": True})
        self.assertEqual(code, 200, j)
        G.until(lambda: any("waiting at the gate" in x for x in s.get("/api/job")["lines"]), "the trash to start")
        release()
        status, body = result()
        with open(hv, "rb") as f:
            self.assertEqual((status, body), (200, f.read()))
        self.assertFalse(os.path.lexists(px))
        self.assertEqual(os.listdir(self.proxies), [])
        with open(gate, "w") as g:  # let the trash finish
            g.write("go")
        G.until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the trash to end")
        self.assertFalse(os.path.lexists(hv))
        self.assertEqual(os.listdir(self.proxies), [])

    def test_proxy_finished_after_its_video_was_trashed_is_dropped(self):
        """The real trash ends while the transcode is held (it found no proxy to delete yet): when the transcode
        ends, the proxy it wrote is deleted, nothing is left in the cache, and nothing is served."""
        s, i, hv, px, release, result = self.held_transcode("IMG_0007.MOV")
        code, j = s.post("/api/trash", {"ids": [i], "confirm": True})
        self.assertEqual(code, 200, j)
        j = G.until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the trash to end")
        self.assertTrue(j["ok"], j["lines"])
        self.assertFalse(os.path.lexists(hv))
        release()
        status, _ = result()
        self.assertEqual(status, 404)  # the original is gone, and its proxy is never served
        self.assertFalse(os.path.lexists(px))
        self.assertEqual(os.listdir(self.proxies), [])

    # ------------------------------------------------------------ clean-up: trash, forget, erase, verify-gone
    def test_trash_from_the_gallery_drops_the_proxy(self):
        self.need_hevc()
        hv = os.path.join(self.vids, "2026/2026-11/IMG_0004.MOV")
        mkclip(hv, "hevc")
        s = self.start()
        i = self.ids(s)["IMG_0004.MOV"]
        self.assertEqual(s.req("GET", f"/m/{i}")[0].status, 200)
        px = self.proxy_of(hv)
        self.assertTrue(os.path.isfile(px))
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
        other = os.path.join(self.proxies, "f" * 64 + ".mp4")
        for p in (pa, pk, other):
            open(p, "wb").close()
        r = subprocess.run([G.PY, "-I", G.INTAKE, "trash", a], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(pa))
        r = subprocess.run([G.PY, "-I", G.INTAKE, "forget", k, "--yes"], env=self.env, capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(pk))
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


class ProxyCommandsTest(unittest.TestCase):
    """gallery.proxy_commands in-process, with the tool lookups patched: no tool runs."""

    def setUp(self):
        import gallery
        import scan_sensitive
        self.g = gallery
        for target, value in ((gallery, ("ffmpeg_encoder", lambda names: "libx264")),
                              (gallery, ("gst_element", lambda names: "x264enc")),
                              (scan_sensitive, ("_gst_hevc_decoder", lambda: "vah265dec"))):
            p = mock.patch.object(target, value[0], value[1])
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

    def test_an_unsafe_work_path_skips_gstreamer(self):
        for work in ('/home/a b/.cache/media-intake/proxies/.work-1', "/home/a!b/.work-1", '/h/"q"/.work-1'):
            self.assertEqual([c[0] for c in self.g.proxy_commands("/lib/v.MOV", work)], ["ffmpeg"], work)


if __name__ == "__main__":
    unittest.main()
