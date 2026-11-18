#!/usr/bin/env python3
"""What the main flow prints: counts-only progress on stage, plan, apply and tag, the summary lines in
words (no Python reprs), the closing `next:` line (only at a terminal, or with MEDIA_INTAKE_NEXT_HINT=1), and
the guards: a wrong batch name, a missing or empty source, apply before scan or without detectors, and the
quarantine hint by batch kind.

  python3 -I -m unittest discover -s tests -p test_intake_output.py

Synthetic photos in a throwaway HOME; `scan` runs a fake venv python (no detectors, no model).
"""
import hashlib, json, os, pty, re, shlex, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
from test_intake import INTAKE, PY, pattern, save_jpg  # noqa: E402

FAKE_SCAN = """#!/bin/sh
# Stands in for the venv python running scan_sensitive.py RAW OUT: flags nothing, prints scan's summary line.
[ -n "$FAKE_SCAN_FAIL" ] && exit 3
printf '{}\\n' > "$4"
echo '{"scanned": 0, "flagged": 0, "by_reason": {}, "unavailable": []}'
"""
def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def sha256(p):
    return hashlib.sha256(read_bytes(p)).hexdigest()


PROGRESS = re.compile(r"^(stage|plan|apply|tag): ")


class IntakeOutput(unittest.TestCase):
    N = 12

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-output-")
        t = self.tmp
        self.home = os.path.join(t, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.phone = os.path.join(t, "phone", "Internal storage")
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        MEDIA_INTAKE_NO_LOCALSEARCH="1", MEDIA_INTAKE_PROGRESS_EVERY="5",
                        MEDIA_INTAKE_PROGRESS_SECS="inf")
        self.env.pop("MEDIA_INTAKE_NEXT_HINT", None)
        self.names = []
        for i in range(self.N):  # distinct pictures: no duplicates, no near-copies, all filed
            name = f"20261020_1200{i:02d}.jpg"
            save_jpg(pattern(10 + i), os.path.join(self.phone, "DCIM/Camera", name), f"2026:10:20 12:00:{i:02d}")
            self.names.append(name)
        venv = os.path.join(self.state, "venv/bin/python")
        os.makedirs(os.path.dirname(venv))
        with open(venv, "w") as f:
            f.write(FAKE_SCAN)
        os.chmod(venv, 0o700)  # owner-only: only this test runs it

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0, script=INTAKE, **env):
        r = subprocess.run([PY, "-I", script, *args], env=dict(self.env, **env), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout.splitlines()

    def call(self, *args, script=INTAKE, **env):
        return subprocess.run([PY, "-I", script, *args], env=dict(self.env, **env), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def refused(self, *args, script=INTAKE, **env):
        """args exit 1 with no traceback; returns stderr."""
        r = self.call(*args, script=script, **env)
        self.assertEqual(r.returncode, 1, f"{args}\n{r.stdout}\n{r.stderr}")
        self.assertNotIn("Traceback", r.stderr)
        return r.stderr

    def batches(self):
        d = os.path.join(self.state, "batches")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def flow(self, batch, **env):
        """stage, plan, scan, apply: {step: stdout lines}."""
        out = {"stage": self.intake("stage", self.phone, "--kind", "phone", "--batch", batch, **env),
               "plan": self.intake("plan", batch, **env),
               "scan": self.intake("scan", batch, **env)}
        out["apply"] = self.intake("apply", batch, "--allow-missing-detectors", **env)
        return out

    def assert_no_names(self, lines):
        for ln in lines:
            if PROGRESS.match(ln):
                self.assertNotIn("/", ln)
                for n in self.names:
                    self.assertNotIn(os.path.splitext(n)[0], ln)

    def test_progress_lines_by_count_and_never_a_name(self):
        out = self.flow("2026-11-15-progress")
        self.assertEqual(out["stage"][:2], ["stage: copying 12 media files into the batch (size-checked)",
                                            "stage: 5 of 12 files staged"], out["stage"])
        self.assertIn("stage: 10 of 12 files staged", out["stage"])
        self.assertTrue(out["stage"][-1].startswith("staged 12 media files into "), "the summary still closes stage")
        self.assertEqual(out["plan"][0], "plan: reading dates, then comparing files to find duplicates (12 files)")
        self.assertEqual([ln for ln in out["plan"] if re.fullmatch(r"plan: \d+ of 12 files read", ln)],
                         ["plan: 5 of 12 files read", "plan: 10 of 12 files read"])
        self.assertIn("plan: looking for look-alike photos (12 in this batch, 0 already in the library)", out["plan"])
        self.assertIn("plan: 10 of 12 photos compared", out["plan"])
        self.assertEqual(out["apply"][0], "apply: moving 12 planned files (library, review folder, quarantine)")
        self.assertEqual([ln for ln in out["apply"] if ln.endswith("files done")],
                         ["apply: 5 of 12 files done", "apply: 10 of 12 files done"])
        for lines in out.values():
            self.assert_no_names(lines)
            self.assertFalse([ln for ln in lines if ln.startswith("next:")], "no next: line off a terminal")
        # scan's own last line stays its JSON summary (test_intake.RealScan and tools parse it).
        self.assertTrue(out["scan"][-1].startswith('{"scanned"'), out["scan"])

    @unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
    def test_tag_progress_and_next(self):
        batch = "2026-11-15-tag"
        self.flow(batch)
        out = self.intake("tag", batch, "--no-labels")
        self.assertIn("tag: 12 files: GPS, place names, then sidecars", out)
        self.assertIn("tag: 12 of 12 sidecars written", out)
        self.assert_no_names(out)
        self.assertFalse([ln for ln in out if ln.startswith("next:")])
        out = self.intake("tag", batch, "--no-labels", MEDIA_INTAKE_NEXT_HINT="1")
        self.assertEqual(out[-1], f"next: media-intake backup <MOUNT>, then media-intake offload {batch} "
                                  f"--phone {shlex.quote(self.phone)}")

    @unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
    def test_tag_writes_in_chunks(self):
        """intake.WRITE_CHUNK patched to 5 against 12 files: three exiftool runs (5, 5, 2) cross the chunk
        boundaries; every sidecar holds its tags, the originals are unchanged, the count line is right, and a
        second run writes the same sidecars."""
        launcher = os.path.join(self.tmp, "intake_chunk5.py")
        with open(launcher, "w") as f:
            f.write(f"import sys\nsys.path.insert(0, {os.path.dirname(INTAKE)!r})\nimport intake\n"
                    "intake.WRITE_CHUNK = 5\nintake.main()\n")
        batch = "2026-11-15-chunks"
        self.flow(batch)
        lib = os.path.join(self.home, "Pictures/library")
        files = sorted(os.path.join(d, n) for d, _, ns in os.walk(lib) for n in ns if n.endswith(".jpg"))
        self.assertEqual(len(files), self.N)
        originals = {p: sha256(p) for p in files}
        # A line prints when the count crosses a multiple of MEDIA_INTAKE_PROGRESS_EVERY; at 4, each chunk's tick
        # (5, 10, 12) crosses one, so every chunk boundary shows, the last as "12 of 12".
        out = self.intake("tag", batch, "--no-labels", script=launcher, MEDIA_INTAKE_PROGRESS_EVERY="4")
        self.assertEqual([ln for ln in out if ln.endswith("sidecars written")],
                         ["tag: 5 of 12 sidecars written", "tag: 10 of 12 sidecars written",
                          "tag: 12 of 12 sidecars written"], out)
        self.assertTrue(out[-1].startswith("tagged 12 files (sidecars <file>.xmp)"), out)
        self.assert_no_names(out)
        first = {p: read_bytes(p + ".xmp") for p in files}
        r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", "-XMP-exif:DateTimeOriginal",
                            *(p + ".xmp" for p in files)], capture_output=True, text=True, check=True)
        rows = {row["SourceFile"][:-4]: row for row in json.loads(r.stdout)}
        self.assertEqual(set(rows), set(files), "every file has its sidecar")
        for p, row in rows.items():
            tags = row.get("HierarchicalSubject")
            tags = tags if isinstance(tags, list) else [tags]
            self.assertTrue([t for t in tags if str(t).startswith("Project|")], (p, tags))
            self.assertTrue([t for t in tags if str(t).startswith("Source|")], (p, tags))
            self.assertTrue(row.get("DateTimeOriginal", "").startswith("2026:10:20 12:00:"), (p, row))
        self.assertEqual({p: sha256(p) for p in files}, originals, "originals never written")
        self.intake("tag", batch, "--no-labels", script=launcher)
        self.assertEqual({p: read_bytes(p + ".xmp") for p in files}, first, "a second run writes the same")
        self.assertEqual({p: sha256(p) for p in files}, originals)

    def test_next_line_clean_batch(self):
        batch = "2026-11-15-clean"
        out = self.flow(batch, MEDIA_INTAKE_NEXT_HINT="1")
        self.assertEqual(out["stage"][-1], f"next: media-intake plan {batch}")
        self.assertEqual(out["plan"][-1], f"next: media-intake scan {batch}")
        self.assertEqual(out["scan"][-1], f"next: media-intake apply {batch}")
        self.assertEqual(out["apply"][-1], f"next: media-intake tag {batch}", "nothing quarantined or in review")
        for lines in out.values():
            self.assertEqual(sum(ln.startswith("next:") for ln in lines), 1, lines)

    def test_next_line_quarantined_batch(self):
        batch = "2026-11-15-quar"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        with open(os.path.join(self.state, "batches", batch, "scan.json"), "w") as f:
            f.write('{"DCIM/Camera/%s": ["nudity"]}' % self.names[0])
        out = self.intake("apply", batch, "--allow-missing-detectors", MEDIA_INTAKE_NEXT_HINT="1")
        self.assertEqual(out[-1], f"next: media-intake review {batch}")
        self.assert_no_names(out)
        again = self.intake("apply", batch, "--allow-missing-detectors", MEDIA_INTAKE_NEXT_HINT="1")
        self.assertEqual(again[-1], f"next: media-intake review {batch}", "still due on a second apply")

    def test_next_line_review_batch(self):
        batch = "2026-11-15-review"
        pattern(3, (64, 64)).save(os.path.join(self.phone, "DCIM/Camera/20261020_130000.png"))  # junk: review
        out = self.flow(batch, MEDIA_INTAKE_NEXT_HINT="1")
        self.assertIn("apply: 12 files filed, 1 set aside for review", out["apply"])
        self.assertEqual(out["apply"][-1], f"next: media-intake review {batch}")

    def test_no_next_line_on_failure(self):
        batch = "2026-11-15-fail"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        out = self.intake("scan", batch, code=3, MEDIA_INTAKE_NEXT_HINT="1", FAKE_SCAN_FAIL="1")
        self.assertFalse([ln for ln in out if ln.startswith("next:")], out)

    # ------------------------------------------------------------ summary lines in words
    def test_plan_and_apply_print_counts_in_words(self):
        batch = "2026-11-15-words"
        pattern(3, (64, 64)).save(os.path.join(self.phone, "DCIM/Camera/sticker.png"))  # no date at all: mtime
        out = self.flow(batch)
        self.assertIn("plan: 13 files: 12 to file, 0 exact duplicates to drop, 1 to review. Nothing has moved yet.",
                      out["plan"])
        self.assertIn("dates: 12 from the camera, 1 from file times (no camera date found)", out["plan"])
        i = out["plan"].index("will be filed in:")
        self.assertEqual(out["plan"][i + 1], "  ~/Pictures/library/2026/2026-10  (12)")
        self.assertIn("apply: 12 files filed, 1 set aside for review", out["apply"])
        for step in ("plan", "apply"):
            self.assertFalse([ln for ln in out[step] if "{" in ln or "}" in ln], out[step])

    # ------------------------------------------------------------ a wrong batch name
    def test_wrong_batch_name_on_each_command(self):
        cmds = (["plan"], ["scan"], ["apply"], ["apply", "--no-scan"], ["quarantine"], ["release", "x.jpg"],
                ["tag", "--no-labels"])
        for c in cmds:  # nothing staged yet
            err = self.refused(c[0], "nosuch", *c[1:])
            self.assertEqual(err.strip(), "no batch named 'nosuch': nothing is staged yet. "
                                          "Start with: media-intake stage SRC --kind phone|gopro|drive|folder", c)
        self.intake("stage", self.phone, "--kind", "phone", "--batch", "b-one")
        for c in cmds:
            err = self.refused(c[0], "nosuch", *c[1:])
            self.assertEqual(err.strip(), "no batch named 'nosuch'. Batches, newest first: b-one. "
                                          "List them all with: ls ~/.local/share/media-intake/batches", c)
        self.assertEqual(self.batches(), ["b-one"], "a wrong name creates nothing")
        self.assertFalse(os.path.exists(os.path.join(self.state, "quarantine", "nosuch")))
        self.assertIn("no batch named '../b-one'", self.refused("plan", "../b-one"), "a path is never a batch")
        # A quarantine-only batch (near-<batch>): quarantine and release take it, plan and tag explain.
        near = os.path.join(self.state, "quarantine", "near-b-one")
        os.makedirs(near)
        with open(os.path.join(near, "quarantine.json"), "w") as f:
            f.write("{}")
        self.assertEqual(self.intake("quarantine", "near-b-one"), ["nothing in quarantine for near-b-one."])
        self.assertIn("'near-b-one' has no staged files, only quarantined ones", self.refused("plan", "near-b-one"))

    def test_steps_out_of_order_name_the_step(self):
        batch = "2026-11-15-order"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.assertEqual(self.refused("apply", batch).strip(),
                         f"plan has not run for {batch}. Run: media-intake plan {batch}")
        self.assertIn(f"plan and apply have not run for {batch}", self.refused("tag", batch, "--no-labels"))
        self.intake("plan", batch)
        err = self.refused("apply", batch)
        self.assertEqual(err.strip(), f"scan has not run for {batch}. Run: media-intake scan {batch}")
        self.assertNotIn("--no-scan", err, "the unsafe bypass is not offered in an error")
        helps = " ".join(self.call("apply", "--help").stdout.split())  # unwrapped
        self.assertIn("--no-scan file everything WITHOUT the sensitive-file check", helps)
        self.assertIn("--allow-missing-detectors file even though the ID and card detectors are not installed", helps)

    def test_event_values_are_checked_first(self):
        self.intake("stage", self.phone, "--kind", "phone", "--batch", "b-event")
        self.assertEqual(self.refused("plan", "b-event", "--event", "park@2026-13-01").strip(),
                         "--event park@2026-13-01: 2026-13-01 is not a date")
        self.assertIn("--event 'Bad': a slug is", self.refused("tag-files", self.phone, "--event", "Bad"))

    def test_release_after_erase_is_a_sentence(self):
        batch = "2026-11-15-erased"
        self.flow(batch)
        shutil.rmtree(os.path.join(self.state, "quarantine", batch))  # as erase leaves it
        self.assertEqual(self.refused("release", batch, "DCIM/Camera/x.jpg").strip(),
                         f"nothing in quarantine for {batch}: it is empty or already erased")
        self.assertFalse(os.path.exists(os.path.join(self.state, "quarantine", batch)), "nothing is recreated")

    def test_detectors_missing_names_only_what_is_missing(self):
        batch = "2026-11-15-detect"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        self.intake("scan", batch)
        for missing in (["tesseract", "python3-zxing-cpp"], ["python3-zxing-cpp"]):
            launcher = os.path.join(self.tmp, "intake_missing.py")
            with open(launcher, "w") as f:
                f.write(f"import sys\nsys.path.insert(0, {os.path.dirname(INTAKE)!r})\nimport intake\n"
                        f"intake.missing_detectors = lambda: {missing!r}\nintake.main()\n")
            err = self.refused("apply", batch, script=launcher).strip().splitlines()
            self.assertLessEqual(len(err), 4, err)
            self.assertLessEqual(max(map(len, err)), 100, err)
            self.assertIn("Install:  sudo dnf install -y " + " ".join(missing), err)
            self.assertIn(f"Then run: media-intake scan {batch}", err)
            self.assertTrue(any("media-intake doctor" in ln for ln in err), err)
            text = "\n".join(err)
            self.assertNotIn("perl-Image-ExifTool", text, "only what is missing")
            self.assertNotIn("--allow-missing-detectors", text)
            if "tesseract" not in missing:
                self.assertNotIn("tesseract", text)
        self.assertTrue(os.path.isdir(os.path.join(self.state, "batches", batch, "raw")), "nothing was filed")

    # ------------------------------------------------------------ a missing or empty source
    def test_missing_source_is_refused_before_any_batch(self):
        for kind in ("phone", "gopro", "drive", "folder"):
            err = self.refused("stage", os.path.join(self.tmp, "no-such-phone"), "--kind", kind)
            self.assertIn("no-such-phone: no such folder.", err, kind)
        self.assertIn("File transfer", self.refused("stage", os.path.join(self.tmp, "gone"), "--kind", "phone"))
        self.assertEqual(self.batches(), [], "no batch folder is left behind")

    def test_empty_source_says_so_and_leaves_no_batch(self):
        empty = os.path.join(self.tmp, "locked-phone")  # what a locked or charge-only phone shows
        os.makedirs(empty)
        err = self.refused("stage", empty, "--kind", "phone")
        self.assertIn("nothing to stage in", err)
        self.assertIn("no photos or videos found for --kind phone", err)
        self.assertIn("unlocked and set to File transfer", err)
        self.assertIn("DCIM/", err)
        folder = os.path.join(self.home, "Pictures", "empty-folder")
        os.makedirs(folder)
        err = self.refused("stage", folder, "--kind", "folder", "--batch", "empty-1")
        self.assertIn("no photos or videos found for --kind folder", err)
        self.assertNotIn("File transfer", err, "the phone tip only for a phone")
        listfile = os.path.join(self.tmp, "chunk-1.txt")  # a queue chunk whose files are gone
        with open(listfile, "w") as f:
            f.write("DCIM/Camera/gone.jpg\n")
        err = self.refused("stage", self.phone, "--kind", "phone", "--batch", "chunk-1", "--only", listfile)
        self.assertIn("none of the 1 file in", err)
        self.assertEqual(self.batches(), [], "no empty batch is created")

    # ------------------------------------------------------------ quarantine's hint fits the batch kind
    def flag_one(self, batch, rel):
        with open(os.path.join(self.state, "batches", batch, "scan.json"), "w") as f:
            json.dump({rel: ["nudity"]}, f)

    def test_quarantine_hint_by_batch_kind(self):
        batch = "2026-11-15-hint"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        self.flag_one(batch, "DCIM/Camera/" + self.names[0])
        self.intake("apply", batch, "--allow-missing-detectors")
        out = self.intake("quarantine", batch)
        self.assertEqual(out[0], f"1 file in quarantine: {os.path.join(self.state, 'quarantine', batch)}")
        self.assertIn(f"  media-intake erase {batch} --phone {shlex.quote(self.phone)}", out)
        # Laptop folder batches: erase --phone is refused there, so the hint never offers it; an empty
        # quarantine gets no advice at all.
        pics = os.path.join(self.home, "Pictures")
        save_jpg(pattern(40), os.path.join(pics, "test-photos", "a.jpg"), "2026:10:21 09:00:00")
        save_jpg(pattern(41), os.path.join(pics, "more", "b.jpg"), "2026:10:21 10:00:00")
        self.intake("stage", os.path.join(pics, "test-photos"), "--kind", "folder", "--batch", "first-test")
        self.intake("plan", "first-test")
        self.intake("apply", "first-test", "--no-scan")
        self.assertEqual(self.intake("quarantine", "first-test"), ["nothing in quarantine for first-test."])
        self.intake("stage", os.path.join(pics, "more"), "--kind", "folder", "--batch", "f2")
        self.intake("plan", "f2")
        self.flag_one("f2", "b.jpg")
        self.intake("apply", "f2", "--allow-missing-detectors")
        out = self.intake("quarantine", "f2")
        self.assertEqual(out[0], f"1 file in quarantine: {os.path.join(self.state, 'quarantine', 'f2')}")
        self.assertEqual(out[-1], "  media-intake erase f2")
        self.assertFalse([ln for ln in out if "--phone" in ln], out)

    def run_on_tty(self, *args, **env):
        """intake.py with stdout on a pseudo-terminal (as a person runs it). Returns its output lines."""
        master, slave = pty.openpty()
        try:
            p = subprocess.Popen([PY, "-I", INTAKE, *args], env=dict(self.env, **env), stdout=slave,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            os.close(slave)
            data = b""
            while True:
                try:
                    chunk = os.read(master, 1 << 16)
                except OSError:  # EIO: the child closed the terminal
                    break
                if not chunk:
                    break
                data += chunk
            self.assertEqual(p.wait(), 0, data)
        finally:
            os.close(master)
        return data.decode().replace("\r\n", "\n").splitlines()

    def test_terminal_gets_the_next_line(self):
        out = self.run_on_tty("stage", self.phone, "--kind", "phone", "--batch", "2026-11-15-tty")
        self.assertEqual(out[-1], "next: media-intake plan 2026-11-15-tty")
        self.assertIn("stage: 5 of 12 files staged", out, "one line per report on a terminal too")
        off = self.run_on_tty("plan", "2026-11-15-tty", MEDIA_INTAKE_NEXT_HINT="0")
        self.assertFalse([ln for ln in off if ln.startswith("next:")], off)


if __name__ == "__main__":
    unittest.main()
