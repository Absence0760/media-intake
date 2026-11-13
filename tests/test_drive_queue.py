#!/usr/bin/env python3
"""Tests for drive_queue.py on synthetic media in a throwaway HOME, state and fake drive.

  python3 -I -m unittest discover -s tests -p test_drive_queue.py

stage, plan, apply and offload are the real intake.py; scan, tag, the semantic
index and the VLM are simulated (scan.json, a sidecar, exit codes), so no model
runs. MEDIA_INTAKE_TEST_MOUNT/UUID stand in for findmnt. Never touches a real drive.
"""
import importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
from contextlib import redirect_stdout

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
from PIL import Image, ImageDraw  # noqa: E402

KEYS = ("HOME", "MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG",
        "MEDIA_INTAKE_TEST_MOUNT", "MEDIA_INTAKE_TEST_UUID", "MEDIA_QUEUE_WRAPPER", "MEDIA_INTAKE_NO_LOCALSEARCH")


def jload(p):
    with open(p) as f:
        return json.load(f)


def jsave(p, obj):
    with open(p, "w") as f:
        json.dump(obj, f)


def picture(path, seed, dt):
    im = Image.new("RGB", (640, 480), (30 * seed % 255, 80, 150))
    d = ImageDraw.Draw(im)
    for i in range(10):
        x, y = (i * 83 * (seed + 3)) % 640, (i * 47 * (seed + 5)) % 480
        d.rectangle([x, y, x + 90, y + 70], fill=((i * 40 + seed * 70) % 255, (i * 29) % 255, (seed * 50) % 255))
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = dt
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, quality=90, exif=ex)


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drive-queue-test-")
        t = self.tmp
        self.home = os.path.join(t, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.mount = os.path.join(t, "drive")
        self.root = os.path.join(self.mount, "Old laptop photos")
        os.makedirs(self.home)
        self.saved = {k: os.environ.get(k) for k in KEYS}
        os.environ.update(HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                          PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                          MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                          MEDIA_INTAKE_TEST_MOUNT=self.mount, MEDIA_INTAKE_TEST_UUID="1234-ABCD",
                          MEDIA_QUEUE_WRAPPER="fake-heavy", MEDIA_INTAKE_NO_LOCALSEARCH="1")
        for i in range(5):
            picture(os.path.join(self.root, "2019", f"IMG_{i:04d}.jpg"), i + 1, f"2019:0{i + 1}:10 12:00:00")
        for x in ("$RECYCLE.BIN/r.jpg", "System Volume Information/s.jpg", ".Trashes/t.jpg", "notes.txt"):
            p = os.path.join(self.root, x)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            picture(p, 9, "2019:09:09 09:09:09") if p.endswith(".jpg") else open(p, "w").close()
        spec = importlib.util.spec_from_file_location("drive_queue_t", os.path.join(HERE, "drive_queue.py"))
        self.q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.q)
        self.q.free_bytes = lambda: 10**15
        self.calls, self.fails, self.kill = [], {}, None
        self.q.run_cmd = self.fake_run
        self.flag_first = False

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # A stand-in for run_cmd: real intake for stage/plan/apply, simulated scan/tag/index/vlm.
    def fake_run(self, cmd, log):
        if cmd[0] == "fake-heavy":
            cmd = cmd[1:]
        q = self.q
        step = cmd[cmd.index(q.INTAKE) + 1] if q.INTAKE in cmd else "index" if q.SEMANTIC in cmd else "vlm"
        batch = cmd[cmd.index(q.INTAKE) + 2] if q.INTAKE in cmd else None
        self.calls.append((step, batch))
        if self.kill == (step, len([c for c in self.calls if c[0] == step])):
            raise KeyboardInterrupt  # the run is killed mid-step
        if step in self.fails:
            return self.fails[step]
        b = os.path.join(self.state, "batches", batch or "")
        if step == "scan":
            plan = jload(os.path.join(b, "plan.json"))
            flags = {plan[0]["rel"]: ["test-label"]} if self.flag_first and batch.endswith("-1") else {}
            jsave(os.path.join(b, "scan.json"), flags)
            return 0
        if step == "tag":
            plan = jload(os.path.join(b, "plan.json"))
            filed = [p["dst"] for p in plan if p["action"] == "file" and os.path.isfile(p["dst"])]
            if filed:
                with open(filed[0] + ".xmp", "w") as f:
                    f.write("<rdf:li>Review|maybe-trash</rdf:li>")
            return 0
        if step == "index":
            return 0
        if step == "vlm":
            return 2  # Ollama not running
        if step == "apply":
            cmd = cmd + ["--allow-missing-detectors"]
        with open(log, "a") as f:
            return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode

    def qrun(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            self.q.main(list(args))
        return out.getvalue()

    def state_q(self):
        return jload(os.path.join(self.state, "queue.json"))

    # ------------------------------------------------------------ add
    def test_add_counts_and_skips_system_dirs(self):
        out = self.qrun("add", self.root, "--label", "old-laptop")
        self.assertIn("5 files", out)
        src = self.state_q()["sources"]["old-laptop"]
        self.assertEqual((src["files"], src["uuid"], src["mount"]), (5, "1234-ABCD", self.mount))
        self.assertEqual(os.stat(os.path.join(self.state, "queue.json")).st_mode & 0o777, 0o600)
        with self.assertRaises(SystemExit):
            self.qrun("add", self.root, "--label", "old-laptop")  # same label
        with self.assertRaises(SystemExit):
            self.qrun("add", os.path.join(self.root, "2019"), "--label", "inner")  # overlaps

    def test_backup_layout_drive_refused(self):
        for folder in ("demo-footage", "personal", "photos"):
            os.makedirs(os.path.join(self.mount, folder))
            with self.assertRaises(SystemExit) as e:
                self.qrun("add", self.root, "--label", "x")
            self.assertIn("backup drive", str(e.exception))
            os.rmdir(os.path.join(self.mount, folder))
        self.assertFalse(os.path.exists(os.path.join(self.state, "queue.json")))
        os.makedirs(os.path.join(self.mount, "photos"))
        open(os.path.join(self.mount, ".media-intake-source-ok"), "w").close()
        self.qrun("add", self.root, "--label", "x")  # the owner's marker overrides

    def test_laptop_disk_refused(self):
        os.environ.pop("MEDIA_INTAKE_TEST_MOUNT")
        with self.assertRaises(SystemExit) as e:
            self.qrun("add", self.root, "--label", "x")
        self.assertIn("laptop", str(e.exception))

    def test_stage_refuses_backup_drive_directly(self):
        os.makedirs(os.path.join(self.mount, "demo-footage"))
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "intake.py"), "stage", self.root,
                            "--kind", "drive", "--batch", "b"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("backup drive", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "batches", "b", "manifest.json")))

    def test_free_space_guard(self):
        self.q.free_bytes = lambda: self.q.MIN_FREE + 1000  # the 5 pictures are bigger than 1000 bytes
        with self.assertRaises(SystemExit) as e:
            self.qrun("add", self.root, "--label", "big")
        self.assertIn("in parts", str(e.exception))
        self.q.free_bytes = lambda: 10**15
        self.qrun("add", self.root, "--label", "big")
        self.q.free_bytes = lambda: self.q.MIN_FREE + 1000  # space ran out before the run
        with self.assertRaises(SystemExit) as e:
            self.qrun("run")
        self.assertIn("low disk", str(e.exception))
        self.assertEqual(self.calls, [])

    # ------------------------------------------------------------ run
    def test_chunks_end_to_end(self):
        os.makedirs(os.path.join(self.state, "venv", "bin"))
        open(self.q.VENV_PY, "w").close()  # a venv exists; the VLM step then finds Ollama down
        self.qrun("add", self.root, "--label", "old-laptop")
        out = self.qrun("run", "--chunk", "2")
        src = self.state_q()["sources"]["old-laptop"]
        day = src["added"][:10]
        self.assertEqual(sorted(c["batch"] for c in src["chunks"].values()),
                         [f"{day}-old-laptop-{n}" for n in (1, 2, 3)])
        self.assertTrue(src.get("finished"))
        self.assertEqual([s for s, _ in self.calls[:7]], ["stage", "plan", "scan", "apply", "tag", "index", "vlm"])
        man = jload(os.path.join(self.state, "batches", f"{day}-old-laptop-1", "manifest.json"))
        self.assertEqual((man["kind"], man["uuid"], len(man["files"])), ("drive", "1234-ABCD", 2))
        filed = [os.path.join(r, f) for r, _, fs in os.walk(self.lib) for f in fs if f.endswith(".jpg")]
        self.assertEqual(len(filed), 5)
        self.assertIn("skipped: VLM unavailable", src["chunks"]["1"]["steps"]["vlm"]["note"])
        self.assertEqual(src["chunks"]["1"]["summary"]["files"], 2)
        self.assertIn("summary:", out)
        st = self.qrun("status")
        self.assertIn("chunks done 3/3", st)
        self.assertIn("trash suggestions 1", st)
        self.assertIn("media-intake review", st)
        n = len(self.calls)
        self.qrun("run")  # nothing left: no step runs again
        self.assertEqual(len(self.calls), n)

    def test_resume_after_kill_never_redoes_a_step(self):
        self.qrun("add", self.root, "--label", "d")
        self.kill = ("tag", 1)
        with self.assertRaises(KeyboardInterrupt):
            self.qrun("run", "--chunk", "3")
        b1 = self.state_q()["sources"]["d"]["chunks"]["1"]
        self.assertEqual(list(b1["steps"]), ["stage", "plan", "scan", "apply"])
        self.kill, self.calls = None, []
        self.qrun("run", "--chunk", "999")  # the chunk size is fixed at the first run
        first = [s for s, b in self.calls if b and b.endswith("-d-1")]
        self.assertEqual(first, ["tag"])
        self.assertEqual(len(self.state_q()["sources"]["d"]["chunks"]), 2)

    def test_killed_after_apply_before_marked(self):
        self.qrun("add", self.root, "--label", "d")
        self.kill = ("tag", 1)
        with self.assertRaises(KeyboardInterrupt):
            self.qrun("run", "--chunk", "5")
        q = self.state_q()
        del q["sources"]["d"]["chunks"]["1"]["steps"]["apply"]  # apply finished, the mark was lost
        jsave(os.path.join(self.state, "queue.json"), q)
        self.kill, self.calls = None, []
        self.qrun("run")
        self.assertEqual([s for s, _ in self.calls][:1], ["tag"])
        self.assertEqual(self.state_q()["sources"]["d"]["chunks"]["1"]["steps"]["apply"]["note"], "already applied")

    def test_failed_step_stops_then_resumes(self):
        self.qrun("add", self.root, "--label", "d")
        self.fails = {"scan": 1}
        with self.assertRaises(SystemExit) as e:
            self.qrun("run")
        self.assertIn("scan failed", str(e.exception))
        self.assertIn("ERROR scan failed", self.qrun("status"))
        self.fails, self.calls = {}, []
        self.qrun("run")
        self.assertEqual(self.calls[0][0], "scan")
        self.assertNotIn("error", self.state_q()["sources"]["d"]["chunks"]["1"])

    def test_drive_unplugged_stops(self):
        self.qrun("add", self.root, "--label", "d")
        os.environ["MEDIA_INTAKE_TEST_UUID"] = "OTHER-0000"
        with self.assertRaises(SystemExit) as e:
            self.qrun("run")
        self.assertIn("not mounted", str(e.exception))
        self.assertEqual(self.calls, [])

    # ------------------------------------------------------------ offload
    def test_offload_guards(self):
        self.flag_first = True
        self.qrun("add", self.root, "--label", "d")
        self.qrun("run", "--chunk", "3")
        on_drive = lambda: sorted(f for f in os.listdir(os.path.join(self.root, "2019")))  # noqa: E731
        self.assertEqual(len(on_drive()), 5)
        with self.assertRaises(SystemExit) as e:
            self.qrun("offload", "d", "--yes")
        self.assertIn("quarantine", str(e.exception))
        self.assertIn("quarantine 1", self.qrun("status"))
        batch = self.state_q()["sources"]["d"]["chunks"]["1"]["batch"]
        qj = os.path.join(self.state, "quarantine", batch, "quarantine.json")
        jsave(qj, {})  # the owner reviewed and erased it
        os.environ["MEDIA_INTAKE_TEST_UUID"] = "OTHER-0000"
        with self.assertRaises(SystemExit) as e:
            self.qrun("offload", "d", "--yes")
        self.assertIn("UUID", str(e.exception))
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "intake.py"), "offload", batch,
                            "--phone", self.root, "--yes"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "intake.py offload checks the UUID too")
        self.assertIn("UUID", r.stderr)
        os.environ["MEDIA_INTAKE_TEST_UUID"] = "1234-ABCD"
        os.makedirs(os.path.join(self.mount, "photos"))
        with self.assertRaises(SystemExit) as e:
            self.qrun("offload", "d", "--yes")
        self.assertIn("backup drive", str(e.exception))
        os.rmdir(os.path.join(self.mount, "photos"))
        self.assertIn("add --yes", self.qrun("offload", "d"))
        self.assertEqual(len(on_drive()), 5, "nothing deleted without --yes")
        out = self.qrun("offload", "d", "--yes")
        self.assertIn("from the drive", out)
        self.assertEqual(len(on_drive()), 1, "the quarantined (erased) file's original stays on the drive")
        self.assertTrue(os.path.exists(os.path.join(self.root, "$RECYCLE.BIN/r.jpg")))
        self.assertEqual(self.state_q()["sources"]["d"]["offloaded"], [1, 2])
        with self.assertRaises(SystemExit):
            self.qrun("offload", "d", "--yes")  # nothing left


if __name__ == "__main__":
    unittest.main()
