"""Shared set-up for the real-browser tests: Playwright, headless Chromium, a synthetic H.264 clip.

Every test here skips with a reason when Playwright (requirements-browser.txt) or its Chromium
is missing, so `python3 -I -m unittest discover -s tests` is unaffected. With
MEDIA_INTAKE_BROWSER_REQUIRED=1 (the `browser` CI job) a missing piece fails instead of skipping,
so the job can never go green on skips.

Nothing here downloads a browser: `python -m playwright install chromium` is an explicit step
(CONTRIBUTING.md, Tests). The page under test is served by the real gallery or review server
on 127.0.0.1 with synthetic media in a throwaway HOME; the browser never leaves localhost.
"""
import errno, os, re, shutil, subprocess, sys, tempfile, time, unittest, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
import support  # noqa: E402  (imported for its side effect too: a hermetic MEDIA_INTAKE_CONFIG for every server)

# The per-test config (its demo project folder inside the test's throwaway HOME), for the browser tests.
write_config = support.write_config

try:
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
except ImportError:  # requirements-browser.txt not installed
    sync_playwright = None

REQUIRED = os.environ.get("MEDIA_INTAKE_BROWSER_REQUIRED") == "1"
# Upper bound for one UI step that waits on a server-side subprocess (a job, a thumbnail).
# Every wait below is on a real signal (an attribute, an event, a response); this only bounds it.
STEP_MS = 20_000
H264_ENCODERS = ("libx264", "libopenh264")


def unavailable(reason):
    """Skip, or fail when the CI job requires the browser tests to run."""
    if REQUIRED:
        raise AssertionError(f"MEDIA_INTAKE_BROWSER_REQUIRED=1 but {reason}")
    raise unittest.SkipTest(reason)


def h264_encoder():
    """The first H.264 encoder this ffmpeg has, or None."""
    if not shutil.which("ffmpeg"):
        return None
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    have = set(re.findall(r"^\s*V\S*\s+(\S+)", r.stdout, re.M))
    return next((e for e in H264_ENCODERS if e in have), None)


def make_clip(path, seconds=30):
    """An H.264 test pattern (640x480, ~3 Mbit/s, ~11 MB), moov atom at the end (no faststart), a
    keyframe every second.

    Sized so a seek near the end must fetch a new byte range: Chromium reads only a bounded
    distance ahead of the play position (seen here: 8 s buffered of a 12 s, 4.6 MB clip), so a
    seek from the start to the last seconds of a 30 s clip lands outside what it has fetched.
    The tests check that precondition (BUFFERED) before they seek, so a change in Chromium's
    read-ahead shows up as a fixture failure, not a false pass.
    """
    enc = h264_encoder()
    if not enc:
        unavailable("no H.264 encoder: install ffmpeg with libx264 or libopenh264 for the video tests")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=size=640x480:rate=25:duration={seconds}",
                    "-c:v", enc, "-b:v", "3M", "-g", "25", "-pix_fmt", "yuv420p", "-an", path], check=True)
    return path


def until(pred, what, timeout=30):
    """Poll a real condition (a FIFO's reader, a process) until it holds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def open_gate(gate):
    """Release a fake intake.py that waits on the FIFO `gate` (once it has opened it)."""
    def w():
        try:
            fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENXIO:  # the fake has not opened it yet
                return False
            raise
        os.write(fd, b"go")
        os.close(fd)
        return True
    until(w, "the fake intake.py to wait at the gate")


class MediaRequests:
    """Every response to a media URL (/m/<id>) with the Range it was asked for."""

    def __init__(self, page, pattern=r"/m/\d+$"):
        self.seen = []
        self.rx = re.compile(pattern)
        page.on("response", self._on)

    def _on(self, resp):
        path = urllib.parse.urlsplit(resp.url).path
        if self.rx.search(path):
            self.seen.append(dict(path=path, range=resp.request.headers.get("range"), status=resp.status,
                                  content_range=resp.headers.get("content-range"),
                                  accept=resp.headers.get("accept-ranges")))

    def of(self, path):
        """The responses for one media path (/m/<id>)."""
        return [r for r in self.seen if r["path"] == path]


# JS run on a <video>, each bounded by STEP_MS (a page-side evaluate has no timeout of its own):
# resolves once it is really playing (not paused, time moving).
PLAYING = """v => new Promise((res, rej) => {
  setTimeout(() => rej(new Error("not playing: paused=" + v.paused + " t=" + v.currentTime)), %d);
  const ok = () => !v.paused && v.currentTime > 0 && v.readyState >= 2;
  if (ok()) return res(v.currentTime);
  const f = () => { if (ok()) { v.removeEventListener("timeupdate", f); res(v.currentTime) } };
  v.addEventListener("timeupdate", f);
})""" % STEP_MS
# seek to the time given and resolve on `seeked`.
SEEK = """(v, t) => new Promise((res, rej) => {
  setTimeout(() => rej(new Error("no seeked event")), %d);
  v.addEventListener("seeked", () => res(v.currentTime), {once: true});
  v.addEventListener("error", () => rej(new Error("media error " + (v.error && v.error.code))), {once: true});
  v.currentTime = t;
})""" % STEP_MS
# resolve once the metadata (duration) is known.
METADATA = """v => new Promise((res, rej) => {
  setTimeout(() => rej(new Error("no loadedmetadata event")), %d);
  if (v.readyState >= 1) return res(v.duration);
  v.addEventListener("loadedmetadata", () => res(v.duration), {once: true});
})""" % STEP_MS
# is the time given inside what it has buffered?
BUFFERED = """(v, t) => { for (let i = 0; i < v.buffered.length; i++)
  if (v.buffered.start(i) <= t && t <= v.buffered.end(i)) return true; return false }"""


class BrowserCase(unittest.TestCase):
    """One headless Chromium per class, one fresh context (cookies) per test."""

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            unavailable("Playwright is not installed (pip install --require-hashes --no-deps "
                        "-r requirements-browser.txt in a venv; see CONTRIBUTING.md, Tests)")
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch()
        except PlaywrightError as e:
            cls._pw.stop()
            first = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            unavailable(f"Playwright's Chromium is not installed (python -m playwright install chromium): {first}")
        expect.set_options(timeout=STEP_MS)
        cls.clipdir = tempfile.mkdtemp(prefix="browser-clip-")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        shutil.rmtree(cls.clipdir, True)

    def clip(self):
        """A path to the shared synthetic H.264 clip (made once per class)."""
        p = os.path.join(self.clipdir, "clip.mp4")
        if not os.path.exists(p):
            make_clip(p)
        return p

    def assert_ranges(self, seen, size, after):
        """Every request in `seen` was a Range request answered 206 with the Content-Range asked
        for, and those from index `after` on (the seek) include one starting past byte 0."""
        self.assertTrue(seen, "no media request seen")
        for r in seen:
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", r["range"] or "")
            cr = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", r["content_range"] or "")
            self.assertTrue(m and cr, r)
            self.assertEqual((r["status"], r["accept"]), (206, "bytes"), r)
            self.assertEqual((int(cr.group(1)), int(cr.group(3))), (int(m.group(1)), size), r)
            if m.group(2):
                self.assertEqual(int(cr.group(2)), min(int(m.group(2)), size - 1), r)
        self.assertTrue(any(int(re.match(r"bytes=(\d+)", r["range"]).group(1)) > 0 for r in seen[after:]), seen)

    def new_page(self):
        ctx = self.browser.new_context()
        ctx.set_default_timeout(STEP_MS)
        self.addCleanup(ctx.close)
        return ctx.new_page()

    def open(self, page, server):
        """Load the server's xdg-open URL (token -> cookie) and wait for the page's data-ready."""
        page.goto(f"http://127.0.0.1:{server.port}/?t={server.token}")
        expect(page.locator("body")).to_have_attribute("data-ready", "1")
