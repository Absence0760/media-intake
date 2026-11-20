#!/usr/bin/env python3
"""captions.py tests: a local stub stands in for Ollama. Synthetic images only, throwaway dirs.

  python3 -I -m unittest discover -s tests -p test_captions.py

The cases that write sidecars need exiftool (CI has it); the rest (the reply checks, reading a caption,
refusals, --dry-run, search --text over a sidecar's caption) need only the standard library and Pillow.
"""
import hashlib, http.server, json, os, shutil, subprocess, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import captions  # noqa: E402

PY = sys.executable
SCRIPT = os.path.join(support.PKG, "captions.py")
SEMANTIC = os.path.join(support.PKG, "semantic.py")
NS = captions.NS_URI
HAVE_EXIFTOOL = bool(shutil.which("exiftool"))
NEEDS_EXIFTOOL = unittest.skipUnless(HAVE_EXIFTOOL, "exiftool missing (captions are written with it; CI has it)")


def sidecar(path, caption=None, model="qwen3-vl:2b", attr=False, keyword=None):
    """A sidecar as exiftool writes one (element form), or with the fields as attributes (attr)."""
    body = ""
    if keyword:
        body += (f"<lr:hierarchicalSubject xmlns:lr='http://ns.adobe.com/lightroom/1.0/'><rdf:Bag>"
                 f"<rdf:li>{keyword}</rdf:li></rdf:Bag></lr:hierarchicalSubject>")
    attrs = ""
    if caption is not None and attr:
        attrs = f" mi:Caption='{caption}' mi:CaptionModel='{model}'"
    elif caption is not None:
        body += f"<mi:Caption>{caption}</mi:Caption><mi:CaptionModel>{model}</mi:CaptionModel>"
    with open(path + ".xmp", "w") as f:
        f.write("<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
                "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
                f"<rdf:Description rdf:about='' xmlns:mi='{NS}'{attrs}>{body}</rdf:Description>"
                "</rdf:RDF></x:xmpmeta>\n<?xpacket end='w'?>\n")


def img(path, color=(200, 30, 30), size=(320, 240)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, quality=90)
    return path


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def reply(obj, n=900, raw=None):
    return {"response": raw if raw is not None else json.dumps(obj), "prompt_eval_count": n, "done": True}


class Reply(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(captions.clean("  a dog\n on\tthe beach\x07 "), "a dog on the beach")
        self.assertEqual(captions.clean(None), "")
        long = " ".join(["word"] * 100)
        c = captions.clean(long)
        self.assertLessEqual(len(c), captions.MAX_CHARS)
        self.assertTrue(c.endswith("word"))

    def test_parse(self):
        p = captions.parse
        self.assertEqual(p(reply({"caption": "A dog on a beach."})), "A dog on a beach.")
        self.assertEqual(p({"response": "", "thinking": json.dumps({"caption": "a cat"}), "prompt_eval_count": 9}),
                         "a cat")  # Ollama 0.21.2 puts a thinking model's constrained JSON in "thinking"
        for bad in (reply(None, raw="not json"), reply([1]), reply({"caption": "x", "extra": 1}),
                    reply({"caption": 3}), reply({"caption": "   "}), reply({"verdict": "KEEP"}),
                    reply({"caption": "x"}, n=8192 - 10), {"response": json.dumps({"caption": "x"})}, None, "x",
                    reply({"caption": "x"}, n=True)):
            self.assertIsNone(p(bad), bad)


class Read(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cap-read-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_read_element_attribute_and_none(self):
        a, b, c, e = (os.path.join(self.d, n) for n in ("a.jpg", "b.jpg", "c.jpg", "e.jpg"))
        sidecar(a, "A red bicycle by a wall")
        sidecar(b, "Two dogs in snow", attr=True)
        sidecar(c, keyword="Label|food")
        with open(e + ".xmp", "w") as f:
            f.write(f"<broken {NS}")
        self.assertEqual(captions.read(a), {"caption": "A red bicycle by a wall", "model": "qwen3-vl:2b"})
        self.assertEqual(captions.read(b)["caption"], "Two dogs in snow")
        self.assertEqual(captions.read(c), {})
        self.assertEqual(captions.read(e), {})
        self.assertEqual(captions.read(os.path.join(self.d, "none.jpg")), {})

    def test_search_words(self):
        a, b = os.path.join(self.d, "a.jpg"), os.path.join(self.d, "b.jpg")
        sidecar(a, "A red bicycle by a red wall")
        sidecar(b, "A bicycle race")
        self.assertEqual(captions.search("RED bicy", [a, b]), {a: 3})
        self.assertEqual(captions.search("bicycle", [a, b]), {a: 1, b: 1})
        self.assertEqual(captions.search("icycle", [a, b]), {})  # from the start of a word only
        self.assertEqual(captions.search("", [a, b]), {})


class Stub(http.server.BaseHTTPRequestHandler):
    digest = None
    reply = None
    requests = []
    vram = 1.0

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
        self._send(Stub.reply(body) if callable(Stub.reply) else Stub.reply)


class Run(unittest.TestCase):
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
        cls.srv.server_close()

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cap-test-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state, mode=0o700)
        self.lib = os.path.join(self.d, "Pictures", "library")
        self.env = dict(os.environ, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.d, "Videos", "personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.d, "footage")), HOME=self.d,
                        http_proxy="http://203.0.113.1:9", HTTP_PROXY="http://203.0.113.1:9",
                        no_proxy="", NO_PROXY="")  # a proxy would break the run: it must be bypassed
        Stub.digest, Stub.requests, Stub.vram = self.DIGEST, [], 1.0
        Stub.reply = reply({"caption": "A red square on a grey wall"})

    def run_c(self, *args, host=None):
        r = subprocess.run([PY, "-I", SCRIPT, "--host", host or self.host, *args], env=self.env,
                           capture_output=True, text=True, timeout=300)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
        return r.returncode, json.loads(last) if last.startswith("{") else {}, r.stderr

    def gens(self):
        return [b for b in Stub.requests if "prompt" in b]

    # ---- no exiftool needed
    def test_off_by_default_needs_files_or_all(self):
        img(os.path.join(self.lib, "2026", "a.jpg"))
        code, _, err = self.run_c()
        self.assertEqual(code, 2)
        self.assertIn("--all", err)
        self.assertEqual(Stub.requests, [])

    def test_refuses_non_loopback_hosts(self):
        p = img(os.path.join(self.lib, "2026", "a.jpg"))
        for h in ("http://192.168.1.5:11434", "http://localhost:11434", "https://127.0.0.1:11434",
                  "http://u:p@127.0.0.1:11434"):
            code, _, err = self.run_c(p, host=h)
            self.assertEqual(code, 2, h)
            self.assertIn("127.0.0.1", err)
        self.assertEqual(Stub.requests, [])

    def test_refuses_flagged_quarantine_and_outside(self):
        ok = img(os.path.join(self.lib, "2026", "ok.jpg"))
        erased = img(os.path.join(self.lib, "2026", "erased.jpg"), color=(1, 2, 3))
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-13 sha256:{sha(erased)} size:{os.path.getsize(erased)}\n")
        quar = img(os.path.join(self.state, "quarantine", "b1", "q.jpg"))
        staged = img(os.path.join(self.state, "batches", "b1", "raw", "s.jpg"))
        outside = img(os.path.join(self.d, "elsewhere", "o.jpg"))
        for bad in (erased, quar, staged, outside):
            code, _, err = self.run_c(ok, bad)
            self.assertEqual(code, 3, bad)
            self.assertIn("nothing sent", err)
        self.assertEqual(Stub.requests, [])
        code, c, _ = self.run_c("--all", "--dry-run")
        self.assertEqual((code, c["refused"], c["todo"]), (0, 1, 1))
        self.assertEqual(Stub.requests, [])

    def test_dry_run_counts_photos_only_and_skips_captioned(self):
        import vlm_trash
        a = img(os.path.join(self.lib, "2026", "a.jpg"))
        b = img(os.path.join(self.lib, "2026", "b.jpg"), color=(9, 9, 9))
        os.makedirs(os.path.join(self.lib, "2026"), exist_ok=True)
        with open(os.path.join(self.lib, "2026", "clip.mp4"), "wb") as f:
            f.write(b"\0" * 64)
        sidecar(a, "A red square")
        with open(os.path.join(self.state, "captions-progress.json"), "w") as f:
            json.dump({a: {"digest": vlm_trash.DIGEST, "stamp": vlm_trash.stamp(a)}}, f)
        code, c, err = self.run_c("--all", "--dry-run")
        self.assertEqual((code, c["todo"], c["already"]), (0, 1, 1))
        self.assertIn("would caption 1", err)
        self.assertNotIn(b, err)  # counts only, never a path
        code, c, _ = self.run_c("--all", "--dry-run", "--redo")
        self.assertEqual(c["todo"], 2)
        self.assertEqual(Stub.requests, [])

    def test_search_text_finds_captions_never_flagged(self):
        a = img(os.path.join(self.lib, "2026", "a.jpg"))
        b = img(os.path.join(self.lib, "2026", "b.jpg"), color=(1, 2, 3))
        sidecar(a, "A red bicycle against a wall")
        sidecar(b, "A red bicycle in the rain")
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({p: {"sha": sha(p), "size": os.path.getsize(p)} for p in (a, b)}, f)

        def search(q):
            r = subprocess.run([PY, "-I", SEMANTIC, "search", "--text", q, "--format", "machine"], env=self.env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.splitlines()

        self.assertEqual(search("red bicycle"), [f"2.000  {a}", f"2.000  {b}"])
        self.assertEqual(search("rain"), [f"1.000  {b}"])
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-20 sha256:{sha(b)} size:{os.path.getsize(b)}\n")
        self.assertEqual(search("red bicycle"), [f"2.000  {a}"])
        self.assertEqual(search("rain"), [])

    # ---- these write sidecars: exiftool (CI)
    def caption_of(self, p):
        return captions.read(p).get("caption")

    @NEEDS_EXIFTOOL
    def test_captions_written_to_own_field_and_resumable(self):
        a = img(os.path.join(self.lib, "2026", "a.jpg"), size=(2000, 1500))
        b = img(os.path.join(self.lib, "2026", "b.jpg"), color=(9, 9, 9))
        subprocess.run(["exiftool", "-q", "-o", a + ".xmp", "-XMP-lr:HierarchicalSubject=Label|food",
                        "-XMP-dc:Description=my own words", a], check=True)
        before = {p: sha(p) for p in (a, b)}
        code, c, err = self.run_c("--all")
        self.assertEqual((code, c["captioned"], c["todo"]), (0, 2, 2), err)
        self.assertNotIn("red square", err.lower())  # counts only
        self.assertEqual(self.caption_of(a), "A red square on a grey wall")
        self.assertEqual(self.caption_of(b), "A red square on a grey wall")
        self.assertEqual({p: sha(p) for p in (a, b)}, before, "the photos themselves are never written")
        r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", "-Description", "-Subject", a + ".xmp"],
                           capture_output=True, text=True)
        row = json.loads(r.stdout)[0]
        self.assertEqual(row.get("HierarchicalSubject"), "Label|food")  # no keyword added or removed
        self.assertEqual(row.get("Description"), "my own words")  # your fields untouched
        g = self.gens()[0]
        self.assertEqual((g["model"], g["think"], g["options"]["temperature"]), ("qwen3-vl:2b", False, 0))
        self.assertEqual(g["format"], captions.SCHEMA)
        self.assertEqual(Stub.requests[-1], {"model": "qwen3-vl:2b", "keep_alive": 0})
        with open(os.path.join(self.state, "captions-progress.json")) as f:
            self.assertNotIn("red square", f.read().lower())  # the progress file holds no caption
        self.assertEqual(os.stat(os.path.join(self.state, "captions-progress.json")).st_mode & 0o777, 0o600)
        # a second run sends nothing; --redo captions again
        Stub.requests = []
        code, c, _ = self.run_c("--all")
        self.assertEqual((code, c["todo"], c["already"]), (0, 0, 2))
        self.assertEqual(self.gens(), [])
        Stub.reply = reply({"caption": "A dark square"})
        code, c, _ = self.run_c("--all", "--redo")
        self.assertEqual(c["captioned"], 2)
        self.assertEqual(self.caption_of(a), "A dark square")
        self.assertEqual({p: sha(p) for p in (a, b)}, before, "nor by --redo")

    @NEEDS_EXIFTOOL
    def test_bad_reply_writes_nothing_and_retries(self):
        a = img(os.path.join(self.lib, "2026", "a.jpg"))
        Stub.reply = reply(None, raw="not json")
        code, c, _ = self.run_c(a)
        self.assertEqual((code, c["no-caption"], c.get("captioned", 0)), (0, 1, 0))
        self.assertFalse(os.path.exists(a + ".xmp"))
        Stub.reply = reply({"caption": "A red square"})
        code, c, _ = self.run_c("--all")
        self.assertEqual((c["todo"], c["captioned"]), (1, 1))

    @NEEDS_EXIFTOOL
    def test_stops_when_model_not_on_gpu(self):
        for i in range(3):
            img(os.path.join(self.lib, "2026", f"{i}.jpg"), color=(i, i, i))
        Stub.vram = 0.0
        code, c, _ = self.run_c("--all")
        self.assertEqual((code, c["stopped-not-on-gpu"], len(self.gens())), (0, 1, 1))
        self.assertEqual(Stub.requests[-1], {"model": "qwen3-vl:2b", "keep_alive": 0})
        code, c, _ = self.run_c("--all", "--allow-cpu")
        self.assertEqual((c["todo"], c["captioned"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
