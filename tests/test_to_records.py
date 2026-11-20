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
        out = self.run_cli("--finish", "--drive", drive, "--backup-verified", "--yes", "--no-wipe-free", code=1)
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
        # An archive's date proves it was written, not that its verify passed: the owner's word is still needed.
        out = self.run_cli("--finish", "--drive", drive, "--yes", "--no-wipe-free", code=1)
        self.assertIn("--backup-verified", out)
        self.assertTrue(os.path.isfile(first), "an archive alone erases nothing")
        self.run_cli("--finish", "--drive", drive, "--backup-verified", "--yes", "--no-wipe-free")
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

    def in_intake(self, code, *args):
        """Run code with intake imported under this test's config; returns its stdout parsed as JSON."""
        r = subprocess.run([PY, "-I", "-c", "import json, os, sys; sys.path.insert(0, sys.argv[1]); import intake\n"
                            + code, support.PKG, *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_copy_arrives_whole_in_one_step_and_the_staging_folder_is_left_empty(self):
        p = self.photo(data=os.urandom(3 << 20))  # several copy chunks
        os.makedirs(self.inbox, mode=0o700)
        # While the copy is being written, the inbox must still be empty: only a whole, checked file arrives.
        seen = self.in_intake(
            "seen = []\n"
            "orig = intake.shutil.copyfileobj\n"
            "def spy(i, o, n):\n"
            "    o.write(i.read(1 << 20)); o.flush(); seen.append(os.listdir(sys.argv[3])); orig(i, o, n)\n"
            "intake.shutil.copyfileobj = spy\n"
            "sha = intake.sha256(sys.argv[2])\n"
            "dst = intake.copy_to_inbox(sys.argv[2], sys.argv[3], sha)\n"
            "print(json.dumps(dict(seen=seen, dst=os.path.basename(dst))))", p, self.inbox)
        self.assertEqual(seen["seen"], [[]], "no part-written file is ever visible in the inbox")
        self.assertEqual(seen["dst"], "IMG_0001.jpg")
        staging = os.path.join(self.records, ".00 Inbox.staging")
        self.assertEqual((mode(staging), os.listdir(staging)), ("0o700", []))
        self.assertEqual(mode(os.path.join(self.inbox, "IMG_0001.jpg")), "0o600")

    def test_a_bad_copy_never_reaches_the_inbox_and_a_stopped_runs_leftover_is_removed(self):
        p = self.photo()
        staging = os.path.join(self.records, ".00 Inbox.staging")
        left = write(os.path.join(staging, "0123abcd.jpg"), b"a stopped run's part copy")
        os.makedirs(self.inbox, mode=0o700)
        out = self.in_intake(
            "try:\n"
            "    intake.copy_to_inbox(sys.argv[2], sys.argv[3], '0' * 64)\n"
            "    print(json.dumps('copied'))\n"
            "except OSError as e:\n"
            "    print(json.dumps(str(e)))", p, self.inbox)
        self.assertIn("does not match the original", out)
        self.assertEqual((os.listdir(self.inbox), os.listdir(staging)), ([], []))
        self.assertFalse(os.path.exists(left))

    def test_without_renameat2_a_hard_link_still_never_overwrites(self):
        os.makedirs(self.inbox, mode=0o700)
        theirs = write(os.path.join(self.inbox, "IMG_0001.jpg"), b"theirs")
        p = self.photo()
        out = self.in_intake(
            "intake._renameat2 = lambda: None\n"
            "dst = intake.copy_to_inbox(sys.argv[2], sys.argv[3], intake.sha256(sys.argv[2]))\n"
            "print(json.dumps(os.path.basename(dst)))", p, self.inbox)
        self.assertEqual(out, "IMG_0001 (2).jpg")
        with open(theirs, "rb") as f:
            self.assertEqual(f.read(), b"theirs")
        self.assertEqual(os.listdir(os.path.join(self.records, ".00 Inbox.staging")), [])

    # renameat2 stand-ins: set errno, return -1, as libc does
    FAKE_RENAME = ("import ctypes, errno\n"
                   "def fake(e):\n"
                   "    def f(*a):\n"
                   "        ctypes.set_errno(e); return -1\n"
                   "    return lambda: f\n")

    def test_renameat2_unsupported_falls_back_and_any_other_error_leaves_nothing(self):
        os.makedirs(self.inbox, mode=0o700)
        p = self.photo()
        staging = os.path.join(self.records, ".00 Inbox.staging")
        for name, ok in (("EINVAL", True), ("ENOSYS", True), ("EOPNOTSUPP", True), ("EACCES", False)):
            with self.subTest(errno=name):
                out = self.in_intake(
                    self.FAKE_RENAME + f"intake._renameat2 = fake(errno.{name})\n"
                    "try:\n"
                    "    d = intake.copy_to_inbox(sys.argv[2], sys.argv[3], intake.sha256(sys.argv[2]))\n"
                    "    print(json.dumps(os.path.basename(d)))\n"
                    "except OSError as e:\n"
                    "    print(json.dumps('refused: ' + type(e).__name__))", p, self.inbox)
                if ok:
                    self.assertTrue(out.startswith("IMG_0001"), out)
                else:
                    self.assertEqual(out, "refused: PermissionError")
                self.assertEqual(os.listdir(staging), [])
        self.assertEqual(sorted(os.listdir(self.inbox)), ["IMG_0001 (2).jpg", "IMG_0001 (3).jpg", "IMG_0001.jpg"])

    def test_no_free_name_leaves_the_inbox_and_staging_as_they_were(self):
        os.makedirs(self.inbox, mode=0o700)
        for n in ("IMG_0001.jpg", "IMG_0001 (2).jpg"):
            write(os.path.join(self.inbox, n), b"theirs")
        p = self.photo()
        out = self.in_intake(
            "intake.INBOX_NAMES = 2\n"
            "try:\n"
            "    intake.copy_to_inbox(sys.argv[2], sys.argv[3], intake.sha256(sys.argv[2]))\n"
            "    print(json.dumps('copied'))\n"
            "except OSError as e:\n"
            "    print(json.dumps(str(e)))", p, self.inbox)
        self.assertIn("no free name", out)
        self.assertEqual(sorted(os.listdir(self.inbox)), ["IMG_0001 (2).jpg", "IMG_0001.jpg"])
        self.assertEqual(os.listdir(os.path.join(self.records, ".00 Inbox.staging")), [])

    def test_a_link_that_arrived_counts_even_if_the_staging_name_cannot_be_dropped(self):
        os.makedirs(self.inbox, mode=0o700)
        p = self.photo()
        out = self.in_intake(
            "intake._renameat2 = lambda: None\n"
            "real = intake.os.remove\n"
            "def remove(x):\n"
            "    if '.staging' in x: raise PermissionError(13, 'denied', x)\n"
            "    real(x)\n"
            "intake.os.remove = remove\n"
            "d = intake.copy_to_inbox(sys.argv[2], sys.argv[3], intake.sha256(sys.argv[2]))\n"
            "print(json.dumps(os.path.basename(d)))", p, self.inbox)
        self.assertEqual(out, "IMG_0001.jpg", "the copy arrived, so the handoff is not reported as failed")
        self.assertEqual(os.listdir(self.inbox), ["IMG_0001.jpg"])
        self.run_cli(self.photo("IMG_0002.jpg"), "--yes")  # the next run clears the staging name left behind
        self.assertEqual(os.listdir(os.path.join(self.records, ".00 Inbox.staging")), [])

    def test_a_staging_path_that_is_not_a_plain_folder_or_on_another_filesystem_is_refused(self):
        os.makedirs(self.inbox, mode=0o700)
        p = self.photo()
        staging = os.path.join(self.records, ".00 Inbox.staging")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        try_copy = ("try:\n"
                    "    intake.copy_to_inbox(sys.argv[2], sys.argv[3], intake.sha256(sys.argv[2]))\n"
                    "    print(json.dumps('copied'))\n"
                    "except OSError as e:\n"
                    "    print(json.dumps(str(e)))")
        for make in (lambda: os.symlink(elsewhere, staging), lambda: write(staging, b"a file")):
            make()
            self.assertIn("not a plain folder", self.in_intake(try_copy, p, self.inbox))
            self.assertEqual((os.listdir(self.inbox), os.listdir(elsewhere)), ([], []))
            os.remove(staging)
        other_dev = ("real = intake.os.stat\n"
                     "class S:\n"
                     "    def __init__(s, r): s.r = r\n"
                     "    def __getattr__(s, k): return getattr(s.r, k)\n"
                     "    st_dev = -1\n"
                     "intake.os.stat = lambda x, *a, **k: S(real(x, *a, **k)) if x.endswith('.staging') "
                     "else real(x, *a, **k)\n")
        self.assertIn("another filesystem", self.in_intake(other_dev + try_copy, p, self.inbox))
        self.assertEqual(os.listdir(self.inbox), [])

    def test_an_inbox_in_a_protected_folder_outside_records_is_never_swept(self):
        consume = os.path.join(self.home, "doc-tool/consume")
        storage = os.path.join(self.home, "doc-tool/media")
        os.makedirs(consume)
        cfg = self.env["MEDIA_INTAKE_CONFIG"]
        with open(cfg) as f:
            text = f.read()
        with open(cfg, "w") as f:
            f.write(text.replace('inbox = "00 Inbox"\n', f'inbox = "{consume}"\n'
                                 f'protect = ["{consume}", "{storage}"]\n'))
        data = os.urandom(3000)
        p = self.photo(data=data)
        kept = write(os.path.join(storage, "originals/0001.jpg"), data)  # the document tool's own stored copy
        self.run_cli(p, "--yes")
        self.assertEqual(os.listdir(consume), ["IMG_0001.jpg"])
        staging = os.path.join(self.home, "doc-tool/.consume.staging")
        self.assertEqual(os.listdir(staging), [])
        left = write(os.path.join(staging, "0123abcd.jpg"), data)  # a stopped run's whole copy, outside protect
        self.run_cli("--finish", "--backup-verified", "--yes")
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.isfile(os.path.join(consume, "IMG_0001.jpg")), "the handed copy stays")
        self.assertTrue(os.path.isfile(kept), "no sweep enters a records.protect folder")
        self.assertTrue(os.path.isfile(left), "nor the staging folder beside the inbox")
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            cov = [json.loads(line.split(" coverage ", 1)[1]) for line in f if " coverage " in line][-1]
        self.assertTrue(any("records.protect" in x and "never searched" in x for x in cov["not_checked"]), cov)

    def test_an_inbox_in_a_missing_protect_folder_hands_nothing_over(self):
        consume = os.path.join(self.home, "doc-tool/consume")
        cfg = self.env["MEDIA_INTAKE_CONFIG"]
        with open(cfg) as f:
            text = f.read()
        with open(cfg, "w") as f:
            f.write(text.replace('inbox = "00 Inbox"\n', f'inbox = "{consume}"\nprotect = ["{consume}"]\n'))
        p = self.photo()
        self.assertIn("records folder is not there", self.run_cli(p, "--yes", code=1))
        self.assertFalse(os.path.exists(os.path.join(self.home, "doc-tool")), "nothing created")
        self.assertFalse(os.path.exists(os.path.join(self.state, "records-handoff.json")))

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
        for extra, ok in (('inbox = "/srv/consume"\nprotect = ["/srv/consume"]', True),
                          ('inbox = "/srv/consume/in"\nprotect = ["/srv/consume"]', True),
                          ('inbox = "/srv/other"\nprotect = ["/srv/consume"]', False),
                          ('protect = ["relative/x"]', False), ('protect = "/srv/x"', False),
                          ('protect = ["/"]', False)):
            with open(p, "w") as f:
                f.write(f'[records]\nroot = "/srv/paper"\n{extra}\n')
            with self.subTest(extra=extra):
                if ok:
                    self.assertTrue(config.load(p)["records_inbox"].startswith("/srv/consume"))
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
