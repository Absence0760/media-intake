#!/usr/bin/env python3
"""The private text index (ocr_index.py): what the scan keeps, search --text, and erase, trash and verify-gone.

  python3 -I -m unittest discover -s tests -p test_ocr_index.py

Synthetic only: a fake `tesseract` first on PATH prints fixed words (the real one, when installed, reads a
Pillow-drawn image in RealTesseract), a stub nudenet module stands in for the detector, random bytes stand in
for media the erase path never decodes. Everything runs in a throwaway HOME and state folder.
"""
import hashlib, json, os, shutil, stat, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import ocr_index  # noqa: E402

PY = sys.executable
INTAKE = os.path.join(support.PKG, "intake.py")
SEMANTIC = os.path.join(support.PKG, "semantic.py")
SCAN = os.path.join(support.PKG, "scan_sensitive.py")

# Prints the words of the first FAKE_OCR_<n> whose marker colour matches the image's top-left pixel; tesseract
# itself is never needed. The image is the grey copy scan_sensitive writes; its first pixel tells which file.
FAKE_TESSERACT = '''#!{py} -I
import os, sys
from PIL import Image
g = Image.open(sys.argv[1]).getpixel((0, 0))
print(os.environ.get("FAKE_OCR_%d" % g, ""))
'''
# The detector scan_sensitive imports: finds nothing.
FAKE_NUDENET = "class NudeDetector:\n    def detect(self, path):\n        return []\n"


def has_pil():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


class Store(unittest.TestCase):
    """add, search, forget, leftovers and prune on a throwaway state folder."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ocr-index-")
        self.a, self.b, self.c = (hashlib.sha256(x).hexdigest() for x in (b"a", b"b", b"c"))

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def test_add_search_and_private_file(self):
        n = ocr_index.add([(self.a, 10, "Coffee  Shop\nRECEIPT total 4.20"), (self.b, 20, "Platform 9 receipt receipt"),
                           (self.c, 30, "   "), ("not-a-sha", 1, "receipt")], self.state)
        self.assertEqual(n, 2, "no text and a bad sha are left out")
        self.assertEqual(stat.S_IMODE(os.stat(ocr_index.db_path(self.state)).st_mode), 0o600)
        self.assertEqual(ocr_index.search("receipt", self.state), {self.a: 1, self.b: 2})
        self.assertEqual(ocr_index.search("rec coffee", self.state), {self.a: 2}, "every word, prefix of a word")
        self.assertEqual(ocr_index.search("ceipt", self.state), {}, "a word matches from its start")
        self.assertEqual(ocr_index.search("  ", self.state), {})

    def test_flagged_and_trashed_never_kept_and_pruned_later(self):
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-20 10:00 b1 sha256:{self.a} size:10\n")
        with open(os.path.join(self.state, "trashed.json"), "w") as f:
            json.dump({self.b: 20}, f)
        self.assertEqual(ocr_index.add([(self.a, 10, "erased"), (self.b, 20, "trashed"), (self.c, 30, "kept")],
                                       self.state), 1)
        self.assertEqual(ocr_index.known(self.state), {self.c})
        # quarantined after it was kept: search drops it and never returns it
        qd = os.path.join(self.state, "quarantine", "forget-1")
        os.makedirs(qd)
        with open(os.path.join(qd, "quarantine.json"), "w") as f:
            json.dump({"x.png": {"sha": self.c, "size": 30, "reasons": ["owner"]}}, f)
        self.assertEqual(ocr_index.search("kept", self.state), {})
        self.assertEqual(ocr_index.known(self.state), set())

    def test_forget_and_leftovers(self):
        self.assertEqual(ocr_index.forget([self.a], self.state), 0)
        self.assertFalse(os.path.exists(ocr_index.db_path(self.state)), "forget never creates the store")
        ocr_index.add([(self.a, 1, "one"), (self.b, 2, "two")], self.state)
        self.assertEqual(ocr_index.leftovers([self.a, self.c], self.state), 1)
        self.assertEqual(ocr_index.forget([self.a, self.c], self.state), 1)
        self.assertEqual(ocr_index.leftovers([self.a], self.state), 0)
        self.assertEqual(ocr_index.known(self.state), {self.b})
        with open(ocr_index.db_path(self.state), "wb") as f:
            f.write(b"not a database" * 100)
        self.assertEqual(ocr_index.forget([self.b], self.state), -1)
        self.assertEqual(ocr_index.leftovers([self.b], self.state), -1)


@unittest.skipUnless(has_pil(), "Pillow not installed")
class ScanAndSearch(unittest.TestCase):
    """scan_sensitive keeps the text of clean photos only; search --text finds library files by it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ocr-scan-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.bin = os.path.join(self.tmp, "bin")
        self.stub = os.path.join(self.tmp, "stub")
        for d in (self.bin, self.stub, self.state):
            os.makedirs(d)
        with open(os.path.join(self.bin, "tesseract"), "w") as f:
            f.write(FAKE_TESSERACT.format(py=PY))
        os.chmod(os.path.join(self.bin, "tesseract"), 0o700)
        with open(os.path.join(self.stub, "nudenet.py"), "w") as f:
            f.write(FAKE_NUDENET)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_OCR_10="Corner cafe receipt total", FAKE_OCR_20="Your recovery code 1234",
                        FAKE_OCR_30="")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def image(self, path, grey):
        from PIL import Image
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Image.new("L", (64, 48), grey).save(path)
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def scan(self, raw):
        code = ("import sys, runpy; sys.path.insert(0, sys.argv[1]); sys.argv = sys.argv[2:]; "
                "runpy.run_path(sys.argv[0], run_name='__main__')")
        out = os.path.join(self.tmp, "scan.json")
        r = subprocess.run([PY, "-I", "-c", code, self.stub, SCAN, raw, out], env=self.env, capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("receipt", r.stdout + r.stderr, "the text is never printed")
        with open(out) as f:
            return json.loads(r.stdout.strip().splitlines()[-1]), json.load(f)

    def search(self, *args, code=0):
        r = subprocess.run([PY, "-I", SEMANTIC, "search", "--text", *args, "--format", "machine"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, code, r.stderr)
        return r.stdout.splitlines(), r.stderr

    def test_clean_text_kept_flagged_text_never(self):
        raw = os.path.join(self.tmp, "raw")
        receipt = self.image(os.path.join(raw, "receipt.png"), 10)
        codes = self.image(os.path.join(raw, "codes.png"), 20)
        self.image(os.path.join(raw, "blank.png"), 30)
        summary, flags = self.scan(raw)
        self.assertIn("secret-words:credentials", flags["codes.png"])
        self.assertEqual(summary["text_indexed"], 1)
        self.assertEqual(ocr_index.known(self.state), {receipt})
        self.assertNotIn(codes, ocr_index.known(self.state), "a flagged photo's text is never kept")
        with open(ocr_index.db_path(self.state), "rb") as f:
            self.assertNotIn(b"recovery", f.read())

        # filed into the library: search --text finds it by its words
        lib = os.path.join(self.home, "Pictures/library/2026/2026-11")
        os.makedirs(lib)
        dst = os.path.join(lib, "receipt.png")
        shutil.copy(os.path.join(raw, "receipt.png"), dst)
        size = os.path.getsize(dst)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({dst: {"sha": receipt, "size": size}}, f)
        out, err = self.search("Receipt cafe")
        self.assertEqual(out, [f"2.000  {dst}"])
        self.assertIn("1 results", err)
        self.assertEqual(self.search("receipt", "--videos")[0], [])
        self.assertEqual(self.search("bakery")[0], [])
        # erased since: never found again, and its row is dropped
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-20 10:00 b1 sha256:{receipt} size:{size}\n")
        self.assertEqual(self.search("receipt")[0], [])
        self.assertEqual(ocr_index.known(self.state), set())

    def test_index_text_backfills_library_photos(self):
        lib = os.path.join(self.home, "Pictures/library/2026/2026-11")
        a = os.path.join(lib, "a.png")
        b = os.path.join(lib, "b.png")
        sa, sb = self.image(a, 10), self.image(b, 20)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({a: {"sha": sa, "size": os.path.getsize(a)}, b: {"sha": sb, "size": os.path.getsize(b)}}, f)
        r = subprocess.run([PY, "-I", SEMANTIC, "index", "--text"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = json.loads(r.stdout)
        self.assertEqual((counts["kept"], counts["sensitive, not kept"]), (1, 1))
        self.assertEqual(ocr_index.known(self.state), {sa})
        r = subprocess.run([PY, "-I", SEMANTIC, "index", "--text"], env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)["read"], 1, "a kept photo is not read again")


class EraseTrashVerify(unittest.TestCase):
    """erase, trash and verify-gone cover the text index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ocr-erase-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.data = os.urandom(4096)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def row(self, sha):
        """A row kept before the content was flagged (add refuses flagged content, so straight into the store)."""
        c = ocr_index.connect(ocr_index.db_path(self.state))
        c.execute("INSERT INTO text VALUES (?, ?, ?)", (sha, len(self.data), "words"))
        c.commit()
        c.close()

    def write(self, p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(self.data)
        return p

    def test_erase_drops_rows_and_reports_them(self):
        batch = "2026-11-20-ocr"
        quar = os.path.join(self.state, "quarantine", batch)
        self.write(os.path.join(quar, "DCIM/Camera/shot.png"))
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"DCIM/Camera/shot.png": {"reasons": ["secret-words:banking"], "sha": self.sha,
                                                "size": len(self.data), "dst": None}}, f)
        self.row(self.sha)
        self.intake("erase", batch, "--yes")
        self.assertEqual(ocr_index.leftovers([self.sha], self.state), 0)
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            cov = [ln for ln in f.read().splitlines() if " coverage " in ln]
        reached = json.loads(cov[-1].split(" coverage ", 1)[1])["reached"]
        self.assertEqual(reached[ocr_index.PLACE], 1)
        # a row put back afterwards: verify-gone reports it
        self.row(self.sha)
        out = self.intake("verify-gone", batch, code=1)
        self.assertIn(f"{ocr_index.PLACE} 1", out)

    def test_trash_drops_rows(self):
        lib = self.write(os.path.join(self.home, "Pictures/library/2026/2026-11/junk.png"))
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({lib: {"sha": self.sha, "size": len(self.data)}}, f)
        self.row(self.sha)
        out = self.intake("trash", lib, "--yes")
        self.assertIn("text index rows 1", out)
        self.assertEqual(ocr_index.leftovers([self.sha], self.state), 0)
        self.assertEqual(ocr_index.known(self.state), set())


@unittest.skipUnless(shutil.which("tesseract") and has_pil(), "tesseract or Pillow not installed")
class RealTesseract(unittest.TestCase):
    """The real tesseract: ocr_flags hands the text of a drawn sign to keep, and flags nothing in it."""

    def test_sign_text_reaches_keep(self):
        import scan_sensitive
        from PIL import Image, ImageDraw, ImageFont
        im = Image.new("RGB", (1400, 300), "white")
        ImageDraw.Draw(im).text((40, 100), "Harbour Street Bakery", fill="black",
                                font=ImageFont.load_default(size=80))
        tmp = tempfile.mkdtemp(prefix="ocr-real-")
        try:
            keep = []
            self.assertEqual(scan_sensitive.ocr_flags(im, tmp, keep), [])
            self.assertIn("bakery", " ".join(keep).casefold())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
