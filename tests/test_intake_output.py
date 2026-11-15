#!/usr/bin/env python3
"""What the main flow prints: counts-only progress on stage, plan, apply and tag, and the closing
`next:` line (only at a terminal, or with MEDIA_INTAKE_NEXT_HINT=1).

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
        self.assertEqual(out["plan"][0], "plan: reading dates, then hashing 12 files (sha256, dHash, video hash)")
        self.assertEqual([ln for ln in out["plan"] if re.fullmatch(r"plan: \d+ of 12 files hashed", ln)],
                         ["plan: 5 of 12 files hashed", "plan: 10 of 12 files hashed"])
        self.assertIn("plan: comparing 12 photos for near-copies (the batch and 0 library entries)", out["plan"])
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
        self.assertTrue(any("'to review': 1" in ln for ln in out["apply"]), out["apply"])
        self.assertEqual(out["apply"][-1], f"next: media-intake review {batch}")

    def test_no_next_line_on_failure(self):
        batch = "2026-11-15-fail"
        self.intake("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        out = self.intake("scan", batch, code=3, MEDIA_INTAKE_NEXT_HINT="1", FAKE_SCAN_FAIL="1")
        self.assertFalse([ln for ln in out if ln.startswith("next:")], out)

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
