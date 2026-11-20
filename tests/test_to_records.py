#!/usr/bin/env python3
"""`media-intake to-records`: a photo of a document goes to the records tool's inbox, its original held until
--finish erases it once the records backup holds the copy.

  python3 -I -m unittest discover -s tests -p test_to_records.py

Synthetic only: random bytes stand in for photos, in a throwaway HOME with its own records folder and inbox
(tests/support.py's config plus a [records] table), and a plain folder as the backup drive (--no-wipe-free). The
real intake.py runs every step; nothing reads a real records folder.
"""
import hashlib, json, os, shutil, stat, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

INTAKE = os.path.join(support.PKG, "intake.py")
PY = sys.executable


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def mode(p):
    return oct(os.stat(p).st_mode & 0o777)


class ToRecords(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-to-records-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.records = os.path.join(self.home, "Paperwork")
        self.inbox = os.path.join(self.records, "00 Inbox")
        os.makedirs(self.state, mode=0o700)
        os.makedirs(self.records)
        footage = os.path.join(self.home, "Videos/demo-footage")
        cfg = os.path.join(self.tmp, "config.toml")
        with open(cfg, "w") as f:
            f.write(support.config_text(os.path.join(footage, "originals"))
                    + f'\n[records]\nroot = "{self.records}"\ninbox = "00 Inbox"\n'
                      'archive = "backups/records-*.tar.gpg"\n')
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"), MEDIA_INTAKE_CONFIG=cfg)
        self.env.pop("PRIVATE_RECORDS", None)

    def run_cli(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, "to-records", *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def photo(self, name="IMG_0001.jpg", data=None):
        return write(os.path.join(self.lib, "2026/2026-11", name), data or os.urandom(3000))

    def quarantined(self, reasons, name="IMG_0100.jpg", batch="2026-11-19-phone"):
        data = os.urandom(3000)
        quar = os.path.join(self.state, "quarantine", batch)
        p = write(os.path.join(quar, name), data)
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({name: dict(reasons=reasons, sha=hashlib.sha256(data).hexdigest(), size=len(data),
                                  dst=None)}, f)
        return p

    def handoffs(self):
        with open(os.path.join(self.state, "records-handoff.json")) as f:
            return json.load(f)

    def test_dry_run_then_copy_holds_the_library_original_and_hides_it(self):
        p = self.photo()
        out = self.run_cli(p)
        self.assertIn("dry run: would copy 1 file to the records inbox", out)
        self.assertFalse(os.path.exists(self.inbox), "a dry run creates nothing")
        out = self.run_cli(p, "--yes")
        self.assertIn("handed to records: IMG_0001.jpg", out)
        copy = os.path.join(self.inbox, "IMG_0001.jpg")
        with open(p, "rb") as a, open(copy, "rb") as b:
            self.assertEqual(a.read(), b.read(), "bytes unchanged")
        self.assertEqual((mode(self.inbox), mode(copy)), ("0o700", "0o600"))
        self.assertEqual(sorted(os.listdir(self.inbox)), ["IMG_0001.jpg"], "nothing but the document goes in")
        self.assertTrue(os.path.isfile(p), "the original is held, not erased")
        self.assertEqual(mode(os.path.join(self.state, "records-handoff.json")), "0o600")
        self.assertIn("IMG_0001.jpg (library)", self.run_cli("--list"))
        self.run_cli(p, "--yes", code=1)  # handed already: refused, no second copy
        self.assertEqual(os.listdir(self.inbox), ["IMG_0001.jpg"])
        r = subprocess.run([PY, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import gallery; "
                            "items, skipped = gallery.scan_library(); print(len(items), skipped)", support.PKG],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), ["0", "1"], r.stderr)  # hidden from the gallery while held

    def test_a_taken_name_gets_a_number_and_nothing_is_overwritten(self):
        theirs = write(os.path.join(self.inbox, "IMG_0001.jpg"), b"already in the inbox")
        write(os.path.join(self.inbox, "IMG_0001 (2).jpg"), b"and this")
        p = self.photo()
        self.run_cli(p, "--yes")
        with open(theirs, "rb") as f:
            self.assertEqual(f.read(), b"already in the inbox")
        self.assertTrue(os.path.isfile(os.path.join(self.inbox, "IMG_0001 (3).jpg")))
        self.assertEqual(self.handoffs()[0]["name"], "IMG_0001 (3).jpg")

    def test_a_quarantined_id_moves_to_the_held_batch(self):
        p = self.quarantined(["id-barcode"])
        self.run_cli(p, "--yes")
        self.assertFalse(os.path.exists(p))
        held = os.path.join(self.state, "quarantine", "records-held")
        self.assertTrue(os.path.isfile(os.path.join(held, "IMG_0100.jpg")))
        with open(os.path.join(self.state, "quarantine", "2026-11-19-phone", "quarantine.json")) as f:
            self.assertEqual(json.load(f), {}, "out of its batch: that batch's erase no longer waits on it")
        self.assertTrue(os.path.isfile(os.path.join(self.inbox, "IMG_0100.jpg")))

    def test_nudity_2fa_secrets_and_outside_files_never_go(self):
        for reasons in (["nudity"], ["id-barcode", "2fa-qr"], ["secret-words:recovery"], ["unscanned-nudity"], []):
            with self.subTest(reasons=reasons):
                p = self.quarantined(reasons, batch="b-" + "-".join(reasons or ["none"]).replace(":", "-"))
                out = self.run_cli(p, "--yes", code=1)
                self.assertIn("stays in quarantine", out)
                self.assertTrue(os.path.isfile(p))
        outside = write(os.path.join(self.home, "Downloads/scan.jpg"), os.urandom(100))
        self.assertIn("not a library or quarantined file", self.run_cli(outside, "--yes", code=1))
        ok = self.photo()  # one refused file refuses the whole run: nothing copied
        self.run_cli(ok, outside, "--yes", code=1)
        self.assertFalse(os.path.exists(self.inbox))

    def test_finish_needs_proof_then_erases_the_originals_but_never_the_inbox_copy(self):
        data = os.urandom(3000)
        p = self.photo(data=data)
        stray = write(os.path.join(self.home, "Downloads/IMG_0001.jpg"), data)
        q = self.quarantined(["payment-card"])
        self.run_cli(p, q, "--yes")
        out = self.run_cli("--finish", "--yes", code=1)
        self.assertIn("--backup-verified", out)
        self.assertTrue(os.path.isfile(p))
        out = self.run_cli("--finish", "--backup-verified")
        self.assertIn("dry run: would erase 2 held originals", out)
        self.assertTrue(os.path.isfile(p))
        self.run_cli("--finish", "--backup-verified", "--yes")
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(stray), "every other copy goes, as forget does")
        self.assertFalse(os.path.exists(os.path.join(self.state, "quarantine", "records-held", "IMG_0100.jpg")))
        self.assertEqual(sorted(os.listdir(self.inbox)), ["IMG_0001.jpg", "IMG_0100.jpg"], "the inbox copies stay")
        self.assertEqual(self.handoffs(), [])
        self.assertIn("nothing handed to records is waiting", self.run_cli("--list"))

    def test_finish_with_the_archive_on_the_drive_erases_only_what_it_holds(self):
        drive = os.path.join(self.tmp, "drive")
        first, second = self.photo("IMG_0001.jpg"), self.photo("IMG_0002.jpg")
        self.run_cli(first, "--yes")
        self.run_cli(second, "--yes")
        rows = self.handoffs()
        os.makedirs(drive)
        out = self.run_cli("--finish", "--drive", drive, "--yes", "--no-wipe-free", code=1)
        self.assertIn("no records archive", out)
        self.assertTrue(os.path.isfile(first), "no archive: nothing erased")
        arch = write(os.path.join(drive, "backups/records-2026-11-20.tar.gpg"), b"encrypted")
        between = (rows[0]["at"] + rows[1]["at"]) / 2
        if rows[0]["at"] == rows[1]["at"]:  # the same clock tick: move the first handoff back instead
            rows[0]["at"] -= 10
            with open(os.path.join(self.state, "records-handoff.json"), "w") as f:
                json.dump(rows, f)
            between = rows[0]["at"] + 5
        os.utime(arch, (between, between))
        self.run_cli("--finish", "--drive", drive, "--yes", "--no-wipe-free")
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.isfile(second), "handed after the newest archive: still held")
        self.assertEqual([r["name"] for r in self.handoffs()], ["IMG_0002.jpg"])
        self.assertTrue(os.path.isfile(arch))

    def test_a_library_copy_of_a_flagged_file_or_a_non_photo_never_goes(self):
        data = os.urandom(3000)
        p = self.photo(data=data)
        quar = os.path.join(self.state, "quarantine", "2026-11-19-x")
        write(os.path.join(quar, "a.jpg"), data)
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"a.jpg": dict(reasons=["nudity"], sha=hashlib.sha256(data).hexdigest(), size=len(data),
                                     dst=None)}, f)
        self.assertIn("a copy of a file the scan flagged", self.run_cli(p, "--yes", code=1))
        notes = write(os.path.join(self.lib, "2026/2026-11/notes.txt"), b"x")
        self.assertIn("not a photo or video", self.run_cli(notes, "--yes", code=1))
        self.assertFalse(os.path.exists(self.inbox))

    def test_finish_leaves_a_changed_file_and_keeps_a_missing_one_listed(self):
        changed, missing = self.photo("IMG_0001.jpg"), self.quarantined(["id-barcode"])
        self.run_cli(changed, missing, "--yes")
        write(changed, os.urandom(3000))  # replaced at the held place since the handoff
        os.remove(os.path.join(self.state, "quarantine", "records-held", "IMG_0100.jpg"))  # neither erased nor there
        out = self.run_cli("--finish", "--backup-verified", "--yes")
        self.assertIn("changed since it was handed over", out)
        self.assertIn("not where it was held and not erased", out)
        self.assertTrue(os.path.isfile(changed), "only what was handed over is erased")
        self.assertEqual([r["name"] for r in self.handoffs()], ["IMG_0100.jpg"], "the missing one stays listed")

    def test_the_held_batch_leaves_only_through_finish(self):
        p = self.quarantined(["passport-mrz"])
        self.run_cli(p, "--yes")
        held = os.path.join(self.state, "quarantine", "records-held", "IMG_0100.jpg")
        for cmd in (["erase", "records-held", "--yes"], ["release", "records-held", "IMG_0100.jpg"]):
            r = subprocess.run([PY, "-I", INTAKE, *cmd], env=self.env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("to-records --finish", r.stdout + r.stderr)
            self.assertTrue(os.path.isfile(held), cmd)

    def test_no_inbox_configured_or_no_records_folder(self):
        cfg = self.env["MEDIA_INTAKE_CONFIG"]
        with open(cfg) as f:
            text = f.read()
        with open(cfg, "w") as f:
            f.write(text.replace('inbox = "00 Inbox"\n', ""))
        p = self.photo()
        self.assertIn("set records.inbox", self.run_cli(p, "--yes", code=1))
        with open(cfg, "w") as f:
            f.write(text)
        shutil.rmtree(self.records)
        self.assertIn("records folder is not there", self.run_cli(p, "--yes", code=1))


class InboxConfig(unittest.TestCase):

    def test_the_inbox_must_be_inside_the_records_folder(self):
        import config
        d = tempfile.mkdtemp(prefix="intake-to-records-cfg-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "c.toml")
        for inbox, ok in (('"00 Inbox"', True), ('"/srv/paper/in"', True), ('"/srv/elsewhere"', False),
                          ('"../out"', False), ('"/srv/paper"', False)):
            with open(p, "w") as f:
                f.write(f'[records]\nroot = "/srv/paper"\ninbox = {inbox}\n')
            with self.subTest(inbox=inbox):
                if ok:
                    self.assertTrue(config.load(p)["records_inbox"].startswith("/srv/paper/"))
                else:
                    with self.assertRaises(config.ConfigError):
                        config.load(p)
        for arch in ('"/abs/x"', '"../x"', '"~/x"'):
            with open(p, "w") as f:
                f.write(f'[records]\narchive = {arch}\n')
            with self.subTest(archive=arch), self.assertRaises(config.ConfigError):
                config.load(p)


if __name__ == "__main__":
    unittest.main()
