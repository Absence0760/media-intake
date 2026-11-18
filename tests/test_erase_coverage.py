#!/usr/bin/env python3
"""erase's coverage report, GNOME's per-file metadata (gvfs-metadata) and the desktop search index check.

  python3 -I -m unittest discover -s tests -p test_erase_coverage.py

Synthetic only: random bytes stand in for media, a fake `gio` and a fake `localsearch` first on PATH record
every call (and whether the file still existed when it came), in a throwaway HOME.
"""
import collections, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import intake  # noqa: E402  (in-process: the hung-tool and symlink cases)

INTAKE = os.path.join(support.PKG, "intake.py")
PY = sys.executable

# gio info lists metadata:: keys for the paths in FAKE_GIO_META; gio set records the call and whether the file
# still existed; gio remove deletes (device_remove). FAKE_GIO_FAIL=info fails every info, =<key> that key's set.
FAKE_GIO = '''#!{py} -I
import os, sys, time
args = sys.argv[1:]
p = args[-1] if args[0] in ("info", "remove") else args[-2]
state = "exists" if os.path.lexists(p) else "missing"
if os.environ.get("FAKE_GIO_HANG"):  # a hung gvfsd-metadata: log the call, then never answer
    with open(os.environ["FAKE_GIO_LOG"], "a") as f:
        f.write(f"hang {{args[0]}} {{p}}\\n")
    time.sleep(60)
try:
    with open(os.environ["FAKE_GIO_META"]) as f:
        meta = p in f.read().splitlines()
except FileNotFoundError:
    meta = False
with open(os.environ["FAKE_GIO_LOG"], "a") as f:
    fail = os.environ.get("FAKE_GIO_FAIL")
    if args[0] == "info":
        f.write(f"info {{state}} {{p}}\\n")
        if fail == "info":
            sys.exit(1)
        print("attributes:\\n  standard::type: 1")
        if meta:
            print("  metadata::emblems: [important]\\n  metadata::custom-icon-name: folder-pictures")
    elif args[0] == "set":
        f.write(f"set {{state}} {{p}} {{args[-1]}} {{' '.join(args[1:-2])}}\\n")
        if fail == args[-1]:
            sys.exit(1)
    elif args[0] == "remove":
        os.remove(p)
        f.write(f"remove {{p}}\\n")
    else:
        sys.exit(2)
'''

# localsearch: Downloads is indexed (not recursively); reset --file forgets a path unless FAKE_LS_STUCK;
# info prints a nie:url only for a path still in FAKE_LS_INDEXED, and fails with FAKE_LS_INFO_FAIL.
FAKE_LS = '''#!{py} -I
import os, sys, time
args, idx = sys.argv[1:], os.environ["FAKE_LS_INDEXED"]
with open(idx) as f:
    have = f.read().splitlines()
with open(os.environ["FAKE_LS_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
if os.environ.get("FAKE_LS_HANG") == args[0]:  # a hung miner: never answers this command
    time.sleep(60)
if args[0] == "index":
    print(os.path.join(os.environ["HOME"], "Downloads") + "  -")
elif args[:2] == ["reset", "--file"] and not os.environ.get("FAKE_LS_STUCK"):
    with open(idx, "w") as f:
        f.writelines(x + "\\n" for x in have if x != args[2])
elif args[0] == "info":
    if os.environ.get("FAKE_LS_INFO_FAIL"):
        sys.exit(1)
    if args[1] in have:
        print("'file://" + args[1] + "'\\n  nie:url = file://" + args[1])
    else:
        print("No metadata available for that URI")
'''


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def read(p):
    with open(p) as f:
        return f.read()


def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def sha256_file(p):
    return hashlib.sha256(read_bytes(p)).hexdigest()


class EraseCoverage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-coverage-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.drive = os.path.join(self.tmp, "drive")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.drive)
        os.makedirs(self.bin)
        for name, body in (("gio", FAKE_GIO), ("localsearch", FAKE_LS)):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(body.format(py=PY))
            os.chmod(os.path.join(self.bin, name), 0o700)
        self.gio_log, self.meta = os.path.join(self.tmp, "gio.log"), os.path.join(self.tmp, "gio-meta")
        self.ls_log, self.indexed = os.path.join(self.tmp, "ls.log"), os.path.join(self.tmp, "ls-indexed")
        open(self.indexed, "w").close()
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_GIO_LOG=self.gio_log, FAKE_GIO_META=self.meta,
                        FAKE_LS_LOG=self.ls_log, FAKE_LS_INDEXED=self.indexed)
        for k in ("MEDIA_INTAKE_NO_GVFS_METADATA", "MEDIA_INTAKE_NO_LOCALSEARCH", "FAKE_LS_STUCK",
                  "FAKE_LS_INFO_FAIL", "FAKE_GIO_FAIL"):
            self.env.pop(k, None)
        self.data = os.urandom(4096)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def H(self, *p):
        return os.path.join(self.home, *p)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        self.stderr = r.stderr
        return r.stdout

    def quarantine(self, batch):
        """A quarantined clip as `apply` leaves one (random bytes; the erase path never decodes a video)."""
        quar = os.path.join(self.state, "quarantine", batch)
        q = write(os.path.join(quar, "DCIM/Camera/clip.mp4"), self.data)
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"DCIM/Camera/clip.mp4": {"reasons": ["nudity"], "sha": self.sha, "size": len(self.data),
                                                "dst": None}}, f)
        return q

    def with_meta(self, *paths):
        with open(self.meta, "a") as f:
            f.writelines(p + "\n" for p in paths)

    def log_lines(self, p):
        return read(p).splitlines() if os.path.exists(p) else []

    def coverage(self):
        lines = [ln for ln in read(os.path.join(self.state, "erase-log.txt")).splitlines() if " coverage " in ln]
        self.assertTrue(lines)
        return json.loads(lines[-1].split(" coverage ", 1)[1])

    def test_erase_reports_every_place_reached_and_what_it_cannot(self):
        batch = "2026-11-14-cov"
        q = self.quarantine(batch)
        dl = write(self.H("Downloads/IMG-77.mp4"), self.data)
        write(dl + ".xmp", b"tags")
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), self.data)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({lib: {"sha": self.sha, "size": len(self.data)}}, f)
        dcopy = write(os.path.join(self.drive, "old", "clip.mp4"), self.data)
        gthumb = write(self.H(".cache/media-intake/thumbs", f"{self.sha}-r0.jpg"), b"x")
        write(self.H(".cache/thumbnails/normal/0123.png"), b"x")
        self.with_meta(dl, lib, dcopy)  # q has no entry: checked, nothing to clear
        with open(self.indexed, "w") as f:
            f.write(dl + "\n")

        out = self.intake("erase", batch, "--yes", "--drive", self.drive, "--no-wipe-free")
        for p in (q, dl, dl + ".xmp", lib, dcopy, gthumb):
            self.assertFalse(os.path.lexists(p), p)
        # gvfs-metadata: every key of every erased file unset while the file still existed, nothing after
        sets = [ln.split() for ln in self.log_lines(self.gio_log) if ln.startswith("set ")]
        self.assertEqual(sorted((s[2], s[3]) for s in sets),
                         sorted((p, k) for p in (dl, lib, dcopy)
                                for k in ("metadata::emblems", "metadata::custom-icon-name")))
        self.assertEqual({s[1] for s in sets}, {"exists"}, "cleared before the delete")
        self.assertTrue(all(s[4:] == ["-n", "-t", "unset"] for s in sets), sets)
        self.assertIn(f"info exists {q}", self.log_lines(self.gio_log))
        # localsearch: the indexed copy reset, then checked forgotten
        ls = self.log_lines(self.ls_log)
        self.assertIn(f"reset --file {dl}", ls)
        self.assertLess(ls.index(f"reset --file {dl}"), ls.index(f"info {dl}"))

        cov = self.coverage()
        r = cov["reached"]
        self.assertEqual(r["quarantined files (state folder, quarantine/)"], 1)
        self.assertEqual(r["library copies (library, videos, project folders)"], 1)
        self.assertEqual(r["other copies in HOME (home sweep)"], 1)
        self.assertEqual(r["sidecars (.xmp)"], 1)
        self.assertEqual(r["backup drive copies"], 1)
        self.assertEqual(r["gallery thumbnails and video proxies (~/.cache/media-intake)"], 1)
        self.assertGreaterEqual(r["freedesktop thumbnails (~/.cache/thumbnails)"], 1)
        self.assertEqual(r["gvfs-metadata entries cleared (~/.local/share/gvfs-metadata)"], 3)
        self.assertEqual(r["localsearch index entries reset"], 1)
        self.assertEqual(r["localsearch index checked: file forgotten"], 1)
        for k in ("recent files entries (~/.local/share/recently-used.xbel)", "Trash entries (~/.local/share/Trash/info)",
                  "Showtime playback positions and log lines (~/.local/state/showtime)",
                  "localsearch error reports (~/.cache/tracker3/files/errors)"):
            self.assertEqual(r[k], 0, k)  # looked, nothing there: still listed
        self.assertEqual(cov["left"], {})
        self.assertIn("phone or camera: no --phone", cov["not_checked"])
        self.assertIn("backup drive unused space: not overwritten (--no-wipe-free)", cov["not_checked"])
        self.assertEqual(len(cov["cant_reach"]), 7)
        for want in ("cloud services", "chat apps", "app-private storage", "backup drives not named",
                     "filesystem snapshots", "Google Takeout JSON or iCloud CSV row left beside an erased file",
                     "Telegram export's result.json"):
            self.assertTrue(any(want in x for x in cov["cant_reach"]), want)
        log = read(os.path.join(self.state, "erase-log.txt"))
        for name in ("IMG-77", "clip.mp4", "Downloads", self.home):
            self.assertNotIn(name, log, "the erase log keeps no names")
        # the short summary
        self.assertIn(f"coverage report (saved in the erase log; media-intake verify-gone {batch} shows it):", out)
        self.assertIn("  reached: quarantined files (state folder, quarantine/) 1, ", out)
        self.assertIn("  left: nothing", out)
        self.assertIn("  can't reach: cloud services and sync", out)
        for todo in ("Gallery", "Google Photos", "OneDrive", "WhatsApp"):
            self.assertNotIn(todo, out, "no product names, no manual steps")

        # verify-gone still reads only the sha256 lines, and shows the last report
        out = self.intake("verify-gone", batch, "--drive", self.drive)
        self.assertIn("no copy of 1 erased items", out)
        self.assertIn(f"last erase coverage report for {batch} (", out)
        self.assertRegex(out, r"\n    gvfs-metadata entries cleared \(~/\.local/share/gvfs-metadata\) +3\n")
        self.assertRegex(out, r"\n    recent files entries \(~/\.local/share/recently-used\.xbel\) +0\n")
        self.assertIn("  left: nothing", out)
        self.assertIn("\n    other computers and devices, and backup drives not named with --drive\n", out)
        self.assertNotIn("last erase coverage report", self.intake("verify-gone"), "per batch only")

    def test_left_behind_goes_in_the_report(self):
        """What erase found but left (a copy in a git repo, a file the index still holds) is reported as left."""
        batch = "2026-11-14-left"
        self.quarantine(batch)
        dl = write(self.H("Downloads/IMG-78.mp4"), self.data)
        write(self.H("code/site/static/clip.mp4"), self.data)
        os.makedirs(self.H("code/site/.git"))
        with open(self.indexed, "w") as f:
            f.write(dl + "\n")
        self.env["FAKE_LS_STUCK"] = "1"  # the index keeps the file after reset
        out = self.intake("erase", batch, "--yes")
        left = self.coverage()["left"]
        self.assertEqual(left, {"exact copies inside git repos (listed, not deleted)": 1,
                                "files localsearch still has indexed": 1})
        self.assertIn("  left: exact copies inside git repos (listed, not deleted) 1, "
                      "files localsearch still has indexed 1", out)
        self.assertEqual(self.coverage()["reached"]["localsearch index checked: file forgotten"], 0)
        self.assertIn("backup drive: no --drive", self.coverage()["not_checked"])

    def test_failed_gio_or_localsearch_check_is_left_not_reached(self):
        """A key gio could not unset, or an index check that got no answer, is reported as left: never as done."""
        batch = "2026-11-14-fail"
        self.quarantine(batch)
        dl = write(self.H("Downloads/IMG-79.mp4"), self.data)
        self.with_meta(dl)
        with open(self.indexed, "w") as f:
            f.write(dl + "\n")
        self.env.update(FAKE_GIO_FAIL="metadata::custom-icon-name", FAKE_LS_INFO_FAIL="1")
        out = self.intake("erase", batch, "--yes")
        self.assertFalse(os.path.lexists(dl), "a gio failure never stops the erase")
        cov = self.coverage()
        self.assertEqual(cov["reached"]["gvfs-metadata entries cleared (~/.local/share/gvfs-metadata)"], 0)
        self.assertEqual(cov["reached"]["localsearch index checked: file forgotten"], 0)
        self.assertEqual(cov["left"], {"gvfs-metadata entries gio could not clear": 1,
                                       "files localsearch could not check": 1})
        self.assertIn("gvfs-metadata entries gio could not clear 1", out)
        self.env.update(FAKE_GIO_FAIL="info")
        batch = "2026-11-14-fail2"
        self.quarantine(batch)
        self.with_meta(write(self.H("Downloads/IMG-80.mp4"), self.data))
        self.intake("erase", batch, "--yes")
        self.assertEqual(self.coverage()["left"]["gvfs-metadata entries gio could not clear"], 2,
                         "the quarantined file and the copy: neither could be looked at")

    def test_trash_entry_and_a_rerun_and_a_damaged_line(self):
        batch = "2026-11-14-trash"
        self.quarantine(batch)
        t = write(self.H(".local/share/Trash/files/IMG-81.mp4"), self.data)
        write(self.H(".local/share/Trash/info/IMG-81.mp4.trashinfo"), b"[Trash Info]\n")
        self.intake("erase", batch, "--yes")
        self.assertFalse(os.path.lexists(t))
        self.assertEqual(self.coverage()["reached"]["Trash entries (~/.local/share/Trash/info)"], 1)
        # the same batch erased again: verify-gone shows the newer report; a damaged line after it is skipped
        self.quarantine(batch)
        self.intake("erase", batch, "--yes")
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"2026-11-14 23:59 {batch} coverage {{damaged}}\n")
        out = self.intake("verify-gone", batch)
        self.assertIn("no copy of 1 erased items", out)
        self.assertRegex(out, r"\n    Trash entries \(~/\.local/share/Trash/info\) +0\n", "the second, newer report")
        self.assertNotIn("23:59", out)

    def test_near_duplicate_is_cleared_at_its_library_path_before_it_moves(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (800, 600), (30, 90, 160))
        d = ImageDraw.Draw(im)
        for i in range(10):
            d.rectangle([i * 70, (i * 53) % 500, i * 70 + 120, (i * 53) % 500 + 90], fill=(i * 25, 200 - i * 15, 40))
        batch = "2026-11-14-near"
        quar = os.path.join(self.state, "quarantine", batch)
        os.makedirs(quar)
        q = os.path.join(quar, "IMG_1.jpg")
        im.save(q, quality=92)
        with open(q, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"IMG_1.jpg": {"reasons": ["nudity"], "sha": sha, "size": os.path.getsize(q), "dst": None}}, f)
        near = self.H("Pictures/library/2026/IMG_1-small.jpg")
        os.makedirs(os.path.dirname(near))
        im.resize((400, 300)).save(near, quality=50)
        self.with_meta(near)
        self.intake("erase", batch, "--yes")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "quarantine", f"near-{batch}",
                                                    "Pictures/library/2026/IMG_1-small.jpg")))
        sets = [ln.split() for ln in self.log_lines(self.gio_log) if ln.startswith("set ")]
        self.assertEqual({(s[1], s[2]) for s in sets}, {("exists", near)})
        r = self.coverage()["reached"]
        self.assertEqual(r["near-duplicates moved to quarantine (not deleted)"], 1)
        self.assertEqual(r["gvfs-metadata entries cleared (~/.local/share/gvfs-metadata)"], 1)

    def test_drive_wipe_is_in_the_report(self):
        batch = "2026-11-14-wipe"
        self.quarantine(batch)
        self.env.update(MEDIA_INTAKE_WIPE_ANY_DEST="1", MEDIA_INTAKE_WIPE_MAX_MB="1")
        self.intake("erase", batch, "--yes", "--drive", self.drive)
        cov = self.coverage()
        self.assertEqual(cov["reached"]["backup drive unused space overwritten (wipe-free, MB)"], 1)
        self.assertEqual(cov["reached"]["backup drive copies"], 0)
        self.assertFalse([x for x in cov["not_checked"] if "backup drive" in x])

    def test_no_yes_clears_and_reports_nothing(self):
        batch = "2026-11-14-no"
        q = self.quarantine(batch)
        self.with_meta(q)
        self.intake("erase", batch, code=1)
        self.assertIn("nothing erased", self.stderr)
        lib = write(self.H("Pictures/library/x.mp4"), self.data)
        self.with_meta(lib)
        self.intake("forget", lib, code=1)
        self.assertIn("nothing erased", self.stderr)
        self.assertTrue(os.path.isfile(q) and os.path.isfile(lib))
        self.assertEqual(self.log_lines(self.gio_log), [], "no gio call before the yes")
        self.assertEqual(self.log_lines(self.ls_log), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "erase-log.txt")))

    def test_forget_clears_the_library_path_before_it_moves(self):
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), self.data)
        write(lib + ".xmp", b"tags")
        write(self.H(".cache/media-intake/proxies", f"{self.sha}.mp4"), b"x")
        self.with_meta(lib)
        self.intake("forget", lib, "--yes")
        self.assertFalse(os.path.lexists(lib))
        sets = [ln.split() for ln in self.log_lines(self.gio_log) if ln.startswith("set ")]
        self.assertEqual([(s[1], s[2]) for s in sets], [("exists", lib)] * 2, "cleared at the library path, "
                         "before the move into quarantine")
        log = read(os.path.join(self.state, "erase-log.txt"))
        batch = log.split()[2]
        self.assertTrue(batch.startswith("forget-"))
        r = self.coverage()["reached"]
        self.assertEqual(r["gvfs-metadata entries cleared (~/.local/share/gvfs-metadata)"], 1)
        self.assertEqual(r["sidecars (.xmp)"], 1)
        self.assertEqual(r["gallery thumbnails and video proxies (~/.cache/media-intake)"], 1)
        self.assertIn(f"last erase coverage report for {batch}", self.intake("verify-gone", batch))

    def test_without_gio_or_localsearch_the_report_says_not_checked(self):
        batch = "2026-11-14-off"
        self.quarantine(batch)
        self.env.update(MEDIA_INTAKE_NO_GVFS_METADATA="1", MEDIA_INTAKE_NO_LOCALSEARCH="1")
        self.intake("erase", batch, "--yes")
        cov = self.coverage()
        self.assertIn("gvfs-metadata (~/.local/share/gvfs-metadata): gio not installed or switched off",
                      cov["not_checked"])
        self.assertIn("desktop search index (localsearch): not installed or switched off", cov["not_checked"])
        self.assertNotIn("gvfs-metadata entries cleared (~/.local/share/gvfs-metadata)", cov["reached"])
        self.assertEqual(self.log_lines(self.gio_log), [])
        self.assertEqual(self.log_lines(self.ls_log), [])

    def test_label_reads_only_the_sha256_lines(self):
        """label.py refuses erased content by its own reading of the erase log: a coverage line adds nothing."""
        import label
        os.makedirs(self.state)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-01-01 10:00 b sha256:{self.sha} size:{len(self.data)}\n")
            f.write("2026-01-01 10:00 b coverage " + json.dumps({"reached": {"sidecars (.xmp)": 3}, "left": {},
                                                                "not_checked": [], "cant_reach": []}) + "\n")
        self.assertEqual(label.flagged(self.state), {self.sha: len(self.data)})

    def backup(self):
        r = subprocess.run([PY, "-I", os.path.join(support.PKG, "cli.py"), "backup", self.drive], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def record(self):
        recs = [os.path.join(self.state, "backup", f) for f in os.listdir(os.path.join(self.state, "backup"))]
        self.assertEqual(len(recs), 1)
        with open(recs[0]) as f:
            return json.load(f)["files"]

    def test_erase_and_forget_drop_backup_records_and_drive_copies(self):
        """backup, then erase --drive and forget: the record forgets the names, the drive copies go by checksum,
        verify-gone finds none, and the next backup doesn't bring them back."""
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=self.drive, MEDIA_INTAKE_TEST_UUID="TEST-UUID")
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), self.data)
        write(lib + ".xmp", b"tags")  # its sidecar is backed up too, under the erased file's name
        other = os.urandom(2048)
        lib2 = write(self.H("Pictures/library/2026/2026-11/later.mp4"), other)
        keep_data = os.urandom(1024)
        keep = write(self.H("Pictures/library/2026/2026-11/keep.mp4"), keep_data)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({lib: {"sha": self.sha, "size": len(self.data)}}, f)
        self.backup()
        dest = os.path.join(self.drive, "media-intake-backup", "library", "2026", "2026-11")
        rels = {"library/2026/2026-11/" + n for n in ("clip.mp4", "clip.mp4.xmp", "later.mp4", "keep.mp4")}
        self.assertEqual(set(self.record()), rels)
        # A stale entry for the same content under a name no longer in the library (renamed since the
        # backup): not in the erase's paths, dropped by its sha256.
        rec = os.path.join(self.state, "backup", os.listdir(os.path.join(self.state, "backup"))[0])
        with open(rec) as f:
            m = json.load(f)
        m["files"]["library/2026/old-name.mp4"] = dict(m["files"]["library/2026/2026-11/clip.mp4"])
        with open(rec, "w") as f:
            json.dump(m, f)
        batch = "2026-11-14-backup"
        self.quarantine(batch)
        self.intake("erase", batch, "--yes", "--drive", self.drive, "--no-wipe-free")
        self.assertFalse(os.path.lexists(lib))
        self.assertFalse(os.path.lexists(os.path.join(dest, "clip.mp4")), "the drive copy goes by checksum")
        self.assertEqual(set(self.record()), rels - {"library/2026/2026-11/clip.mp4", "library/2026/2026-11/clip.mp4.xmp"})
        self.assertNotIn(self.sha, json.dumps(self.record()))
        self.assertEqual(self.coverage()["reached"]["backup records: erased files dropped (state folder, backup/)"], 3)
        self.intake("forget", lib2, "--yes", "--drive", self.drive, "--no-wipe-free")
        self.assertEqual(set(self.record()), {"library/2026/2026-11/keep.mp4"})
        self.assertFalse(os.path.lexists(os.path.join(dest, "later.mp4")))
        self.assertIn("no copy of 1 erased items in HOME or on the drive",
                      self.intake("verify-gone", batch, "--drive", self.drive))
        self.backup()
        self.assertEqual(sorted(os.listdir(dest)), ["keep.mp4"], "the next backup brings nothing back")
        self.assertEqual(set(self.record()), {"library/2026/2026-11/keep.mp4"})
        self.assertEqual(sha256_file(keep), hashlib.sha256(keep_data).hexdigest(), "the kept original is unchanged")
        self.assertEqual(self.record()["library/2026/2026-11/keep.mp4"]["sha"], hashlib.sha256(keep_data).hexdigest())

    def test_a_record_it_cannot_rewrite_is_counted_as_left(self):
        """A backup record erase cannot rewrite (its temporary name taken by a folder) keeps the names: the
        report says so under left, and the erase itself still finishes."""
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=self.drive, MEDIA_INTAKE_TEST_UUID="TEST-UUID")
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), self.data)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({lib: {"sha": self.sha, "size": len(self.data)}}, f)
        self.backup()
        rdir = os.path.join(self.state, "backup")
        rec = os.path.join(rdir, os.listdir(rdir)[0])
        os.mkdir(rec + ".tmp")  # intake.save writes <record>.tmp first
        batch = "2026-11-14-ro"
        self.quarantine(batch)
        self.intake("erase", batch, "--yes", "--no-wipe-free")
        self.assertFalse(os.path.lexists(lib))
        cov = self.coverage()
        self.assertEqual(cov["left"]["backup records that could not be rewritten (they keep the names)"], 1)
        self.assertEqual(cov["reached"]["backup records: erased files dropped (state folder, backup/)"], 0)
        with open(rec) as f:
            self.assertEqual(set(json.load(f)["files"]), {"library/2026/2026-11/clip.mp4"})

    def record_path(self):
        rdir = os.path.join(self.state, "backup")
        recs = os.listdir(rdir)
        self.assertEqual(len(recs), 1)
        return os.path.join(rdir, recs[0])

    def test_an_erase_outside_the_backed_up_folders_leaves_the_record_byte_identical(self):
        """An erased copy outside every backup root (a sibling folder whose name starts like the library's,
        under the same relative path as a recorded file) and content the record doesn't hold: the record is not
        rewritten at all. An erase that matches nothing anywhere leaves it as it was too."""
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=self.drive, MEDIA_INTAKE_TEST_UUID="TEST-UUID")
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), os.urandom(2048))  # other content
        self.backup()
        rec = self.record_path()
        before, mtime = read_bytes(rec), os.stat(rec).st_mtime_ns
        outside = write(self.H("Pictures/library-old/2026/2026-11/clip.mp4"), self.data)
        self.quarantine("2026-11-14-outside")
        self.intake("erase", "2026-11-14-outside", "--yes", "--no-wipe-free")
        self.assertFalse(os.path.lexists(outside), "the copy outside the library is still erased")
        self.assertEqual(read_bytes(rec), before)
        self.assertEqual(os.stat(rec).st_mtime_ns, mtime, "not rewritten")
        self.assertTrue(os.path.isfile(lib))
        cov = self.coverage()
        self.assertEqual(cov["reached"]["backup records: erased files dropped (state folder, backup/)"], 0)
        self.assertFalse([k for k in cov["left"] if k.startswith("backup records")], cov["left"])
        # The same erase again, with no copy anywhere but the quarantine: nothing matches, nothing changes.
        self.quarantine("2026-11-14-nothing")
        self.intake("erase", "2026-11-14-nothing", "--yes", "--no-wipe-free")
        self.assertEqual(read_bytes(rec), before)
        self.assertEqual(self.coverage()["reached"]["backup records: erased files dropped (state folder, backup/)"], 0)

    def test_an_entry_with_an_erased_sha256_goes_even_for_a_kept_path(self):
        """The record drops every entry whose sha256 is an erased item's, also one at a path the erase did not
        touch (in a real erase that file is an exact copy and goes too; here prune runs alone, in process). It
        is bookkeeping only: the file at that path is untouched, and the next backup hashes it and records it
        again."""
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=self.drive, MEDIA_INTAKE_TEST_UUID="TEST-UUID")
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), self.data)
        twin = write(self.H("Pictures/library/2026/2026-10/twin.mp4"), self.data)
        other = write(self.H("Pictures/library/2026/2026-10/other.mp4"), os.urandom(1024))
        self.backup()
        self.assertEqual(set(self.record()), {"library/2026/2026-11/clip.mp4", "library/2026/2026-10/twin.mp4",
                                              "library/2026/2026-10/other.mp4"})
        os.remove(lib)  # erased
        cfg = dict(intake.CFG, library=self.H("Pictures/library"), videos=self.H("Videos/personal"), projects={})
        with mock.patch.object(intake, "STATE", self.state), mock.patch.object(intake, "CFG", cfg):
            self.assertEqual(intake.prune_backup_records({lib}, {self.sha}), (2, 0, 0))
        self.assertEqual(set(self.record()), {"library/2026/2026-10/other.mp4"})
        self.assertEqual(read_bytes(twin), self.data, "the kept file is not touched")
        self.assertTrue(os.path.isfile(os.path.join(self.drive, "media-intake-backup", "library/2026/2026-10/twin.mp4")),
                        "nor its copy on the drive")
        self.backup()
        self.assertEqual(self.record()["library/2026/2026-10/twin.mp4"]["sha"], self.sha, "hashed and recorded again")
        self.assertNotIn("library/2026/2026-11/clip.mp4", self.record())

    def test_a_record_it_cannot_read_is_counted_as_left(self):
        """A backup record that is not JSON, or is another version's, can't be pruned: erase leaves it byte for
        byte, counts it under left (it may keep names), and still finishes."""
        import backup
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=self.drive, MEDIA_INTAKE_TEST_UUID="TEST-UUID")
        lib = write(self.H("Pictures/library/2026/2026-11/clip.mp4"), self.data)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({lib: {"sha": self.sha, "size": len(self.data)}}, f)
        self.backup()
        rec = self.record_path()
        write(rec, b'{"version": 1, "files": {"library/2026/2026-11/clip.mp4": ')  # cut short
        later = write(os.path.join(os.path.dirname(rec), "later.json"), json.dumps(
            {"version": 99, "files": {"library/2026/2026-11/clip.mp4": {"sha": self.sha}}}).encode())
        before = {p: read_bytes(p) for p in (rec, later)}
        for p in before:
            self.assertEqual(backup.load_manifest(p), {}, "backup itself starts such a record afresh")
            self.assertIsNone(backup.load_manifest(p, strict=True))
        missing = os.path.join(self.tmp, "none.json")
        self.assertEqual((backup.load_manifest(missing), backup.load_manifest(missing, strict=True)), ({}, {}))
        self.quarantine("2026-11-14-unread")
        self.intake("erase", "2026-11-14-unread", "--yes", "--no-wipe-free")
        self.assertFalse(os.path.lexists(lib))
        self.assertEqual({p: read_bytes(p) for p in before}, before)
        cov = self.coverage()
        self.assertEqual(cov["left"]["backup records that could not be read (they may keep names until the next "
                                     "backup to that drive)"], 2)
        self.assertNotIn("backup records that could not be rewritten (they keep the names)", cov["left"])
        self.assertEqual(cov["reached"]["backup records: erased files dropped (state folder, backup/)"], 0)

    def test_verify_gone_without_a_report(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-01-01 10:00 old-batch sha256:{self.sha} size:{len(self.data)}\n"
                    "2026-01-01 10:00 old-batch coverage {not json\n")
        out = self.intake("verify-gone", "old-batch")
        self.assertIn("no copy of 1 erased items", out)
        self.assertIn("no erase coverage report for old-batch (erased before reports were kept)", out)



class HungTools(unittest.TestCase):
    """In process: a gio or localsearch that never answers costs one GIO_TIMEOUT, then the rest of the erase
    skips that tool and counts what it skipped as not done. A symlink is never handed to gio."""

    def setUp(self):
        intake.HUNG.update(gio=False, localsearch=False)
        self.addCleanup(intake.HUNG.update, gio=False, localsearch=False)
        self.tmp = tempfile.mkdtemp(prefix="intake-hung-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        b = os.path.join(self.tmp, "bin")
        os.makedirs(b)
        for name, body in (("gio", FAKE_GIO), ("localsearch", FAKE_LS)):
            with open(os.path.join(b, name), "w") as f:
                f.write(body.format(py=PY))
            os.chmod(os.path.join(b, name), 0o700)
        self.gio_log, self.ls_log = os.path.join(self.tmp, "gio.log"), os.path.join(self.tmp, "ls.log")
        self.home = os.path.join(self.tmp, "home")
        self.indexed = write(os.path.join(self.tmp, "ls-indexed"), b"")
        env = mock.patch.dict(os.environ, PATH=b + os.pathsep + os.environ.get("PATH", ""), HOME=self.home,
                              FAKE_GIO_LOG=self.gio_log, FAKE_GIO_META=os.path.join(self.tmp, "none"),
                              FAKE_LS_LOG=self.ls_log, FAKE_LS_INDEXED=self.indexed)
        env.start()
        self.addCleanup(env.stop)
        for k in ("MEDIA_INTAKE_NO_GVFS_METADATA", "MEDIA_INTAKE_NO_LOCALSEARCH", "FAKE_GIO_HANG", "FAKE_LS_HANG"):
            os.environ.pop(k, None)  # patch.dict puts them back
        for name, value in (("GIO_TIMEOUT", 0.5), ("HOME", self.home)):
            p = mock.patch.object(intake, name, value)
            p.start()
            self.addCleanup(p.stop)

    def lines(self, p):
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def test_hung_gio_sets_the_flag_and_later_files_skip(self):
        a = write(os.path.join(self.home, "a.mp4"), b"a")
        b = write(os.path.join(self.home, "b.mp4"), b"b")
        os.environ["FAKE_GIO_HANG"] = "1"
        stats = collections.Counter()
        self.assertEqual(intake.gvfs_meta_forget(a, stats), 0)
        self.assertTrue(intake.HUNG["gio"], "the timeout in gvfs_meta_forget sets the flag")
        self.assertEqual(intake.gvfs_meta_forget(b, stats), 0)
        self.assertEqual(stats["gvfs-metadata not cleared"], 2)
        self.assertEqual(self.lines(self.gio_log), [f"hang info {a}"], "after the timeout gio is not called again")

    def test_a_symlink_is_never_passed_to_gio(self):
        target = write(os.path.join(self.home, "target.mp4"), b"t")
        link, dangling = os.path.join(self.home, "link.mp4"), os.path.join(self.home, "dangling.mp4")
        os.symlink(target, link)
        os.symlink(os.path.join(self.home, "nowhere.mp4"), dangling)
        stats = collections.Counter()
        for p in (link, dangling):
            self.assertEqual(intake.gvfs_meta_forget(p, stats), 0)
        self.assertEqual(self.lines(self.gio_log), [])
        self.assertEqual(stats, collections.Counter())
        self.assertEqual(intake.gvfs_meta_forget(target, stats), 0)  # a plain file still is
        self.assertEqual(self.lines(self.gio_log), [f"info exists {target}"])

    def test_hung_localsearch_sets_the_flag_and_later_files_skip(self):
        d = os.path.join(self.home, "Downloads")  # the fake's indexed folder
        uris = sorted(intake.file_uris(os.path.join(d, n))[0] for n in ("a.mp4", "b.mp4"))
        os.environ["FAKE_LS_HANG"] = "reset"
        n = intake.prune_app_traces(set(uris), [])
        self.assertTrue(intake.HUNG["localsearch"])
        self.assertEqual(n["localsearch could not check"], 2)
        self.assertEqual(n["localsearch checked"], 0)
        self.assertEqual(len([ln for ln in self.lines(self.ls_log) if ln.startswith("reset ")]), 1,
                         "one hung reset, then no more localsearch calls")
        self.assertFalse([ln for ln in self.lines(self.ls_log) if ln.startswith("info ")])

    def test_localsearch_that_vanishes_after_the_check_is_no_answer(self):
        """localsearch found on PATH (localsearch_on), then gone when it is run (uninstalled mid-erase): the
        OSError is no answer, counted as could not check, not as a hang, and the rest of the erase's traces are
        still pruned. subprocess.run raises as it would, so no real localsearch later on PATH is ever run."""
        uri = intake.file_uris(os.path.join(self.home, "Downloads", "a.mp4"))[0]
        hist = write(os.path.join(self.home, ".local/state/showtime/playback_history.json"),
                     json.dumps({hashlib.sha256(uri.encode()).hexdigest(): 12}).encode())
        real_run, calls = subprocess.run, []

        def gone(cmd, *a, **kw):
            if cmd[0] == "localsearch":
                calls.append(cmd[1])
                raise FileNotFoundError(2, "No such file or directory", cmd[0])
            return real_run(cmd, *a, **kw)

        self.assertTrue(intake.localsearch_on())
        with mock.patch.object(intake.subprocess, "run", gone):
            n = intake.prune_app_traces({uri}, [])
            self.assertIsNone(intake.localsearch("info", uri[7:]))
        self.assertEqual(n["showtime positions"], 1)
        self.assertEqual(json.loads(read(hist)), {})
        self.assertEqual(n["localsearch could not check"], 1)
        self.assertEqual((n["localsearch resets"], n["localsearch checked"]), (0, 0))
        self.assertFalse(intake.HUNG["localsearch"], "a missing binary is not a hang")
        self.assertEqual(calls, ["index", "info"], "each call tried once, none hung")

    def test_hung_localsearch_index_skips_every_reset_and_check(self):
        uris = {intake.file_uris(os.path.join(self.home, "Downloads", "a.mp4"))[0]}
        os.environ["FAKE_LS_HANG"] = "index"
        n = intake.prune_app_traces(uris, [])
        self.assertTrue(intake.HUNG["localsearch"])
        self.assertEqual(n["localsearch could not check"], 1)
        self.assertEqual((n["localsearch resets"], n["localsearch checked"]), (0, 0))
        self.assertEqual(self.lines(self.ls_log), ["index"])


if __name__ == "__main__":
    unittest.main()
