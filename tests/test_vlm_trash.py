#!/usr/bin/env python3
"""vlm_trash.py tests: a local stub stands in for Ollama. Synthetic images only, throwaway dirs.

  python3 -I -m unittest discover -s tests -p test_vlm_trash.py
  VLM_REAL=1 python3 -I -m unittest discover -s tests -p test_vlm_trash.py   (+ real qwen3-vl calls, timed)

The real call is skipped unless VLM_REAL=1 and Ollama answers on 127.0.0.1:11434.
"""
import base64, hashlib, http.server, io, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
VENV_PY = os.path.expanduser("~/.local/share/media-intake/venv/bin/python")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable
SCRIPT = os.path.join(HERE, "vlm_trash.py")
sys.path.insert(0, HERE)


class Stub(http.server.BaseHTTPRequestHandler):
    digest = None
    reply = None          # dict for /api/generate, or a callable(body) -> dict
    requests = []

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):  # the kill test drops connections
            pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    vram = 1.0  # fraction of the model /api/ps reports in VRAM

    def do_GET(self):
        if self.path == "/api/ps":
            self._send({"models": [{"name": "qwen3-vl:2b", "size": 1000, "size_vram": int(1000 * Stub.vram)}]})
        elif self.path == "/api/tags":
            self._send({"models": [{"name": "qwen3-vl:2b", "model": "qwen3-vl:2b", "digest": Stub.digest}]})
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.requests.append(body)
        if "prompt" not in body:  # unload
            self._send({"done": True})
            return
        r = Stub.reply(body) if callable(Stub.reply) else Stub.reply
        self._send(r)


def reply(obj, n=900, raw=None):
    return {"response": raw if raw is not None else json.dumps(obj), "prompt_eval_count": n, "done": True}


def thinking_reply(obj, n=900):
    """What Ollama 0.21.2 returns for a thinking model under a format schema."""
    return {"response": "", "thinking": json.dumps(obj), "prompt_eval_count": n, "done": True}


def img(path, color=(200, 30, 30), size=(1600, 1200)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, (240, 240, 240))
    ImageDraw.Draw(im).rectangle([size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4], fill=color)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, quality=90)
    return path


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tags(p):
    if not os.path.exists(p + ".xmp"):
        return []
    r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", p + ".xmp"], capture_output=True, text=True)
    v = json.loads(r.stdout)[0].get("HierarchicalSubject", [])
    return v if isinstance(v, list) else [v]


NS = "AI|vlm/qwen3-vl-2b/"


def ai(p):
    return sorted(t for t in tags(p) if t.startswith("AI|"))


def add_tag(p, t):
    if not os.path.exists(p + ".xmp"):
        import vlm_trash
        with open(p + ".xmp", "w") as f:
            f.write(vlm_trash.EMPTY_XMP)
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-XMP-lr:HierarchicalSubject+={t}", p + ".xmp"],
                   check=True)


class VlmTrash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import vlm_trash
        cls.DIGEST = vlm_trash.DIGEST
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.host = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vlm-test-")
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state, mode=0o700)
        self.lib = os.path.join(self.d, "Pictures", "library")
        self.env = dict(os.environ, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.d, "Videos", "personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.d, "footage")), HOME=self.d,
                        http_proxy="http://203.0.113.1:9", HTTP_PROXY="http://203.0.113.1:9",
                        no_proxy="", NO_PROXY="")  # a proxy would break the run: it must be bypassed
        Stub.digest, Stub.requests, Stub.vram = self.DIGEST, [], 1.0
        Stub.reply = reply({"verdict": "TRASH", "reason": "blurry", "confidence": 0.9})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_v(self, *args, host=None):
        r = subprocess.run([PY, "-I", SCRIPT, "--host", host or self.host, *args], env=self.env,
                           capture_output=True, text=True, timeout=300)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
        return r.returncode, json.loads(last) if last.startswith("{") else {}, r.stderr

    def gens(self):
        return [b for b in Stub.requests if "prompt" in b]

    # ---- refusals
    def test_refuses_non_loopback_hosts(self):
        p = img(os.path.join(self.lib, "2026", "a.jpg"))
        for h in ("http://192.168.1.5:11434", "http://localhost:11434", "https://127.0.0.1:11434",
                  "http://example.com", "http://127.0.0.1.evil.example:11434", "http://u:p@127.0.0.1:11434"):
            code, _, err = self.run_v(p, host=h)
            self.assertEqual(code, 2, h)
            self.assertIn("127.0.0.1", err)
        self.assertEqual(Stub.requests, [])

    def test_refuses_wrong_digest(self):
        Stub.digest = "0" * 64
        p = img(os.path.join(self.lib, "2026", "a.jpg"))
        code, _, err = self.run_v(p)
        self.assertEqual(code, 2)
        self.assertIn("digest", err)
        self.assertEqual(self.gens(), [])
        self.assertEqual(tags(p), [])

    def test_refuses_flagged_quarantine_and_outside(self):
        ok = img(os.path.join(self.lib, "2026", "ok.jpg"))
        erased = img(os.path.join(self.lib, "2026", "erased.jpg"), color=(1, 2, 3))
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-13 sha256:{sha(erased)} size:{os.path.getsize(erased)}\n")
        quar = img(os.path.join(self.state, "quarantine", "b1", "q.jpg"))
        staged = img(os.path.join(self.state, "batches", "b1", "raw", "s.jpg"))
        outside = img(os.path.join(self.d, "elsewhere", "o.jpg"))
        for bad in (erased, quar, staged, outside):
            code, _, _ = self.run_v(ok, bad)
            self.assertEqual(code, 3, bad)
        self.assertEqual(self.gens(), [])
        # candidate scan: flagged file tagged maybe-trash is skipped, not sent
        add_tag(ok, "Review|maybe-trash")
        add_tag(erased, "Review|maybe-trash")
        code, c, _ = self.run_v()
        self.assertEqual(code, 0)
        self.assertEqual((c["refused"], c["todo"]), (1, 1))
        self.assertEqual(len(self.gens()), 1)
        self.assertEqual(ai(erased), [])

    def test_stops_when_model_not_on_gpu(self):
        for i in range(3):
            img(os.path.join(self.lib, "2026", f"{i}.jpg"))
        Stub.vram = 0.0
        code, c, _ = self.run_v("--all")
        self.assertEqual((code, c["stopped-not-on-gpu"], len(self.gens())), (0, 1, 1))
        self.assertEqual(Stub.requests[-1], {"model": "qwen3-vl:2b", "keep_alive": 0})
        code, c, _ = self.run_v("--all", "--allow-cpu")
        self.assertEqual((c["todo"], c.get("keep", 0) + c.get("trash", 0) + c.get("trash-low", 0)), (2, 2))

    # ---- request shape
    def test_request_shape_and_unload(self):
        p = img(os.path.join(self.lib, "2026", "a.jpg"), size=(4000, 3000))
        code, c, _ = self.run_v(p)
        self.assertEqual(code, 0)
        g = self.gens()[0]
        self.assertEqual(g["model"], "qwen3-vl:2b")
        self.assertEqual(g["options"]["temperature"], 0)
        self.assertEqual(g["options"]["num_ctx"], 8192)
        self.assertFalse(g["stream"])
        self.assertIsInstance(g["format"], dict)
        self.assertIs(g["think"], False)
        self.assertNotEqual(g["keep_alive"], 0)
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(g["images"][0])))
        self.assertEqual(im.format, "JPEG")
        self.assertEqual(max(im.size), 768)
        self.assertEqual(Stub.requests[-1], {"model": "qwen3-vl:2b", "keep_alive": 0})
        self.assertEqual(c["trash/blurry"], 1)

    # ---- validation
    def test_malformed_replies_are_keep(self):
        BAD = ("KEEP", None, None)
        bad = [reply(None, raw="not json"), reply(None, raw=""), reply([1, 2]),
               reply({"verdict": "trash", "reason": "blurry", "confidence": 0.9}),
               reply({"verdict": "TRASH", "reason": "ugly", "confidence": 0.9}),
               reply({"verdict": "TRASH", "reason": "blurry", "confidence": 1.5}),
               reply({"verdict": "TRASH", "reason": "blurry", "confidence": "0.9"}),
               reply({"verdict": "TRASH", "reason": "blurry", "confidence": True}),
               reply({"verdict": "TRASH", "reason": "blurry", "confidence": 0.9, "description": "a dog"}),
               reply({"verdict": "TRASH", "reason": "blurry"}),
               reply({"verdict": "TRASH", "reason": "blurry", "confidence": 0.9}, n=8190),  # truncated
               {"response": json.dumps({"verdict": "TRASH", "reason": "dark", "confidence": 0.9})},  # no count
               "a string",
               {"response": "", "thinking": "Let me look. The image is dark.", "prompt_eval_count": 900},
               thinking_reply({"verdict": "TRASH", "reason": "dark", "confidence": 0.9}, n=8190)]
        import vlm_trash
        for b in bad:
            self.assertEqual(vlm_trash.parse(b), BAD, b)
        self.assertEqual(vlm_trash.parse(reply({"verdict": "TRASH", "reason": "dark", "confidence": 0.7})),
                         ("TRASH", "dark", 0.7))
        self.assertEqual(vlm_trash.parse(reply({"verdict": "TRASH", "reason": "dark", "confidence": 0.69})),
                         ("TRASH", "dark", 0.69))  # raw verdict; consumers threshold at 0.7
        self.assertEqual(vlm_trash.parse(reply({"verdict": "KEEP", "reason": "other", "confidence": 0.99})),
                         ("KEEP", None, 0.99))
        # thinking model: the schema-constrained JSON arrives in "thinking"; same checks apply
        self.assertEqual(vlm_trash.parse(thinking_reply({"verdict": "TRASH", "reason": "dark", "confidence": 0.9})),
                         ("TRASH", "dark", 0.9))
        # end to end: a non-JSON reply is KEEP on the sidecar
        Stub.reply = reply(None, raw="The photo shows a red box")
        p = img(os.path.join(self.lib, "2026", "a.jpg"))
        code, c, out = self.run_v(p)
        self.assertEqual(code, 0)
        self.assertEqual(ai(p), [NS + "keep"])
        with open(p + ".xmp") as f:
            self.assertNotIn("red box", f.read())

    # ---- sidecars, dedupe, resume
    def dc(self, p):
        r = subprocess.run(["exiftool", "-j", "-q", "-XMP-dc:Subject", p + ".xmp"], capture_output=True, text=True)
        v = json.loads(r.stdout)[0].get("Subject", [])
        return sorted(v if isinstance(v, list) else [v])

    def test_sidecar_own_namespace_only(self):
        p = img(os.path.join(self.lib, "2026", "a.jpg"))
        before = sha(p)
        others = ["Review|maybe-trash", "Review|child-in-shot", "Label|runner", "Place|Riverton, Eastland, US",
                  "AI|vlm/llava-13b/trash/dark", "AI|other-tool/x"]
        for t in others:
            add_tag(p, t)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-dc:Subject+=runner", p + ".xmp"], check=True)
        self.assertEqual(self.run_v()[0], 0)
        self.assertEqual(sorted(tags(p)), sorted(others + [NS + "trash/blurry", NS + "conf/0.9"]))
        # already judged with this digest: skipped
        code, c, _ = self.run_v()
        self.assertEqual((c["already"], c["todo"]), (1, 0))
        # --redo with a new verdict replaces the old vlm tag, no duplicates
        Stub.reply = reply({"verdict": "KEEP", "reason": "other", "confidence": 0.8})
        self.run_v("--redo")
        self.run_v("--redo")
        self.assertEqual(sorted(tags(p)), sorted(others + [NS + "keep", NS + "conf/0.8"]))
        self.assertEqual(self.dc(p), sorted(["runner", "vlm/qwen3-vl-2b/keep", "vlm/qwen3-vl-2b/conf/0.8"]))
        self.assertEqual(sha(p), before)
        self.assertEqual(os.stat(os.path.join(self.state, "vlm-progress.json")).st_mode & 0o777, 0o600)

    def test_owner_decisions_skipped(self):
        decided = []
        for i, t in enumerate(["Owner|not-trash", "Owner|keep", "Owner|trash", "Review|not-trash", None]):
            p = img(os.path.join(self.lib, "2026", f"o{i}.jpg"))
            add_tag(p, "Review|maybe-trash")
            if t:
                add_tag(p, t)
            else:  # digiKam pick label (1 = rejected)
                subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-digiKam:PickLabel=1", p + ".xmp"],
                               check=True)
            decided.append(p)
        # candidate scan never lists Owner|not-trash / Review|not-trash files; --all and explicit lists see all 5
        for mode, n in (((), 3), (("--all",), 5), (tuple(decided), 5)):
            code, c, _ = self.run_v(*mode)
            self.assertEqual((code, c["owner-decided"], c["todo"]), (0, n, 0), mode)
        self.assertEqual(self.gens(), [])
        for p in decided:
            self.assertEqual(ai(p), [])

    def test_used_and_demo_never_trash(self):
        u = img(os.path.join(self.lib, "2026", "u.jpg"))
        add_tag(u, "Used|2026-11-10-some-post")
        t = img(os.path.join(self.d, "footage", "originals", "2026-11-08-race", "phone", "t.jpg"))
        add_tag(t, "Project|Demo")
        code, c, _ = self.run_v("--all")
        self.assertEqual((code, c["protected"], c.get("trash", 0)), (0, 2, 0))
        self.assertEqual(ai(u), [NS + "keep"])
        self.assertEqual(ai(t), [NS + "keep"])
        self.assertEqual(self.gens(), [])

    def test_candidates_skip_not_trash_and_untagged(self):
        a = img(os.path.join(self.lib, "2026", "a.jpg"))
        b = img(os.path.join(self.lib, "2026", "b.jpg"))
        img(os.path.join(self.lib, "2026", "c.jpg"))
        add_tag(a, "Review|maybe-trash")
        add_tag(b, "Review|maybe-trash")
        add_tag(b, "Review|not-trash")  # legacy spelling
        d = img(os.path.join(self.lib, "2026", "d.jpg"))
        add_tag(d, "Review|maybe-trash")
        add_tag(d, "Owner|not-trash")
        code, c, _ = self.run_v()
        self.assertEqual((c["todo"], c["owner-decided"]), (1, 0))  # b and d never become candidates
        self.assertEqual(len(self.gens()), 1)

    def test_all_is_incremental_and_resumable(self):
        files = [img(os.path.join(self.lib, "2026", f"{i}.jpg"), color=(i * 20, 10, 10)) for i in range(5)]
        add_tag(files[4], "Review|not-trash")
        Stub.reply = reply({"verdict": "KEEP", "reason": "other", "confidence": 0.9})
        code, c, _ = self.run_v("--all", "--dry-run")
        self.assertEqual((c["todo"], c["owner-decided"]), (4, 1))
        self.assertEqual(self.gens(), [])
        code, c, _ = self.run_v("--all")
        self.assertEqual((code, c["keep"]), (0, 4))
        img(os.path.join(self.lib, "2026", "new.jpg"))
        code, c, _ = self.run_v("--all")
        self.assertEqual((c["already"], c["todo"], c["keep"]), (4, 1, 1))

    def test_kill_midway_keeps_progress(self):
        for i in range(30):
            img(os.path.join(self.lib, "2026", f"{i:02}.jpg"), color=(i * 9, 10, 10))
        n = [0]

        def slow(body):
            n[0] += 1
            time.sleep(0.05)
            return reply({"verdict": "KEEP", "reason": "other", "confidence": 0.9})
        Stub.reply = slow
        proc = subprocess.Popen([PY, "-I", SCRIPT, "--host", self.host, "--all"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while n[0] < 27:  # past the first 25-file checkpoint
            time.sleep(0.02)
        proc.kill()
        proc.wait()
        code, c, _ = self.run_v("--all")
        self.assertGreaterEqual(c["already"], 25)
        self.assertEqual(c["already"] + c["todo"], 30)

    def test_video_majority(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg missing")
        v = os.path.join(self.d, "Videos", "personal", "2026", "v.mp4")
        os.makedirs(os.path.dirname(v))
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=8",
                        "-pix_fmt", "yuv420p", v], check=True)
        votes = iter([reply({"verdict": "TRASH", "reason": "dark", "confidence": 0.9}),
                      reply({"verdict": "KEEP", "reason": "other", "confidence": 0.9}),
                      reply({"verdict": "TRASH", "reason": "dark", "confidence": 0.8})])
        Stub.reply = lambda b: next(votes)
        code, c, _ = self.run_v(v)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.gens()), 3)
        self.assertEqual(ai(v), [NS + "conf/0.8", NS + "trash/dark"])
        Stub.reply = lambda b: reply({"verdict": "TRASH" if len(self.gens()) % 3 == 1 else "KEEP",
                                      "reason": "dark", "confidence": 0.9})
        Stub.requests = []
        self.run_v("--redo", v)
        self.assertEqual(ai(v), [NS + "conf/0.9", NS + "keep"])


@unittest.skipUnless(os.environ.get("VLM_REAL") == "1", "set VLM_REAL=1 for the real Ollama call")
class RealOllama(unittest.TestCase):
    def test_real_call_synthetic(self):
        import urllib.request
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                "http://127.0.0.1:11434/api/version", timeout=3)
        except OSError:
            self.skipTest("Ollama not reachable")
        import vlm_trash
        from PIL import Image, ImageFilter
        vlm_trash.check_digest(vlm_trash.HOST)
        d = tempfile.mkdtemp(prefix="vlm-real-")
        try:
            times, verdicts = [], []
            for i, (name, im) in enumerate([
                    ("dark", Image.new("RGB", (1600, 1200), (4, 4, 6))),
                    ("blur", Image.open(img(os.path.join(d, "b.jpg"))).filter(ImageFilter.GaussianBlur(40))),
                    ("shape", Image.open(img(os.path.join(d, "s.jpg"), color=(30, 90, 200)))),
                    ("shape2", Image.open(img(os.path.join(d, "s2.jpg"), color=(200, 160, 20))))]):
                j = vlm_trash.jpeg_bytes(im)
                t = time.monotonic()
                verdicts.append((name, vlm_trash.ask(vlm_trash.HOST, j)))
                times.append(time.monotonic() - t)
            vlm_trash.unload(vlm_trash.HOST)
            print(f"\n[real] first (load) {times[0]:.2f} s, then {sum(times[1:]) / len(times[1:]):.2f} s/image;"
                  f" verdicts {verdicts}", file=sys.stderr)
            for _, v in verdicts:
                self.assertIn(v[0], ("TRASH", "KEEP"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
