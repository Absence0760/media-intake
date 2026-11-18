#!/usr/bin/env python3
"""Tests for drive_queue.py on synthetic media in a throwaway HOME, state and fake drive.

  python3 -I -m unittest discover -s tests -p test_drive_queue.py

stage, plan, apply and offload are the real intake.py; scan, tag, the semantic
index and the VLM are simulated (scan.json, a sidecar, exit codes), so no model
runs. MEDIA_INTAKE_TEST_MOUNT/UUID stand in for findmnt. Never touches a real drive.
"""
import contextlib, datetime, hashlib, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

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
    ex[0x8769] = {36867: dt}  # Pillow 10.2 (Ubuntu 24.04) drops writes made through get_ifd()
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
        # pending() imports review_ui, which reads STATE at import: never one cached under another test's state.
        sys.modules.pop("review_ui", None)
        spec = importlib.util.spec_from_file_location("drive_queue_t", os.path.join(HERE, "drive_queue.py"))
        self.q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.q)
        self.q.free_bytes = lambda: 10**15
        self.calls, self.fails, self.kill, self.kill_after = [], {}, None, None
        self.q.run_cmd = self.fake_run
        self.flag_first = False
        self.fail_line = None

    def tearDown(self):
        sys.modules.pop("review_ui", None)
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
            if self.fail_line:
                with open(log, "a") as f:
                    f.write(f"== 2026-01-01T00:00:00 {' '.join(cmd[:3])}\nworking\n{self.fail_line}\n\n")
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
            rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
        if self.kill_after == (step, len([c for c in self.calls if c[0] == step])):
            raise KeyboardInterrupt  # killed once the step's work is done, before the queue marks it
        return rc

    def qrun(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.q.main(list(args))
        return out.getvalue()

    def status_json(self):
        return json.loads(self.qrun("status", "--json"))

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

    # ------------------------------------------------------------ Telegram Desktop exports
    TG_TAKEN = ("photos/photo_1@05-03-2019_14-22-01.jpg", "files/IMG_0007.jpg")
    TG_DENIED = ("photos/photo_1@05-03-2019_14-22-01_thumb.jpg", "stickers/sticker.webp", "animations/a.mp4",
                 "round_video_messages/r.mp4", "voice_messages/audio_1@05-03-2019_14-23-00.ogg",
                 "profile_pictures/p.jpg", "stories/s.mp4", "css/style.css", "js/script.js", "images/back.png",
                 "files/report.pdf", "x.jpg")

    def telegram_export(self, base):
        """A synthetic export under base: two media files queue_add may take, the rest it never may."""
        for i, rel in enumerate(self.TG_TAKEN + self.TG_DENIED):
            p = os.path.join(base, rel)
            if rel.endswith(".jpg"):
                picture(p, 30 + i, "2019:03:05 14:22:01")
            else:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").close()
        for n in ("result.json", "messages.html"):
            open(os.path.join(base, n), "w").close()

    def test_telegram_export_queues_only_its_media_folders(self):
        """queue add lists through sources.walk (check_drive), and a chunk's stage re-walks: an export's
        stickers, round videos, voice, profile pictures, HTML assets and previews are never queued or staged,
        even when files.json is edited to name them."""
        os.environ.pop("MEDIA_INTAKE_TEST_MOUNT")
        f = os.path.join(self.home, "Chats")
        chat = "ChatExport_2019-03-06"
        self.telegram_export(os.path.join(f, chat))
        out = self.qrun("add", f, "--label", "chats", "--kind", "folder")
        self.assertIn("2 files", out)
        self.assertIn("messaging 2", out)
        files_json = os.path.join(self.state, "queue", "chats", "files.json")
        want = sorted(f"{chat}/{r}" for r in self.TG_TAKEN)
        self.assertEqual(sorted(r for r, _ in jload(files_json)), want)
        # An edited (or stale) files.json naming denied files: stage still takes only what walk allows.
        jsave(files_json, jload(files_json) + [[f"{chat}/{r}", 0] for r in self.TG_DENIED])
        self.qrun("run", "--chunk", "100")
        src = self.state_q()["sources"]["chats"]
        man = jload(os.path.join(self.state, "batches", src["chunks"]["1"]["batch"], "manifest.json"))
        self.assertEqual(sorted(man["files"]), want)
        filed = sorted(os.path.relpath(os.path.join(r, x), self.lib) for r, _, fs in os.walk(self.lib)
                       for x in fs if not x.endswith(".xmp"))
        self.assertEqual(filed, [os.path.join("Messaging", "Telegram", "2019", os.path.basename(r)) for r in
                                 sorted(self.TG_TAKEN, key=os.path.basename)])

    def test_telegram_export_as_a_drive_root(self):
        """A drive source that is the export itself (renamed: known by result.json beside photos/)."""
        root = os.path.join(self.mount, "chat backup")
        self.telegram_export(root)
        os.makedirs(os.path.join(root, "video_files"))
        open(os.path.join(root, "video_files", "video_1@05-03-2019_14-30-00.mp4"), "w").close()
        out = self.qrun("add", root, "--label", "chat-drive")
        self.assertIn("3 files", out)
        self.assertIn("(messaging 3)", out, "files/IMG_0007.jpg too: the source root is the export")
        rels = sorted(r for r, _ in jload(os.path.join(self.state, "queue", "chat-drive", "files.json")))
        self.assertEqual(rels, sorted([*self.TG_TAKEN, "video_files/video_1@05-03-2019_14-30-00.mp4"]))

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
        self.assertEqual(self.status_json()["sources"][0]["chunks_done"], 3)
        self.assertIn("needs your review", st)
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
        self.assertIn("[d] stopped at scan: it failed (exit 1) with no message. Fix it, then run again: "
                      "media-intake queue run --inhibit (it resumes at scan; log: ", str(e.exception))
        self.assertIn("failed at scan: scan failed (exit 1)", self.qrun("status"))
        self.fails, self.calls = {}, []
        self.qrun("run")
        self.assertEqual(self.calls[0][0], "scan")
        self.assertNotIn("error", self.state_q()["sources"]["d"]["chunks"]["1"])

    def test_failed_step_says_why_without_file_names(self):
        """The stop message and status give the step log's last line, with paths and media names taken out."""
        self.qrun("add", self.root, "--label", "d")
        self.fails = {"scan": 1}
        self.fail_line = (f"no venv at {self.state}/venv: run bash /opt/media-intake/setup.sh first "
                          f"(checked '{self.root}/2019/IMG_0003.jpg' and IMG_0004.jpg)")
        with self.assertRaises(SystemExit) as e:
            self.qrun("run")
        msg = str(e.exception)
        self.assertIn("[d] stopped at scan: no venv at (a path): run bash setup.sh first (checked (a path) and "
                      "(a file)). Fix it, then run again: media-intake queue run --inhibit (it resumes at scan; log: ",
                      msg)
        log = msg.rsplit("log: ", 1)[1].rstrip(")")
        self.assertTrue(os.path.isfile(log), "the log path is given as is")
        self.assertEqual(msg.count("\n"), 0, "one line")
        reason, _ = msg.split(". Fix it")
        for leak in ("IMG_", self.root, self.state, "Old laptop"):
            self.assertNotIn(leak, reason)
        st = self.qrun("status")
        waiting = " ".join(x.strip() for x in st.split("Waiting for you:")[1].splitlines())
        self.assertIn("d, chunk 1 (", waiting)
        self.assertIn(") failed at scan: scan failed (exit 1): no venv at (a path): run bash setup.sh first", waiting)
        self.assertIn(f"    log:                {log}\n", st)
        self.assertNotIn("IMG_", st)
        failed = self.status_json()["waiting"][0]
        self.assertEqual((failed["what"], failed["log"]), ("failed", log))
        self.fails, self.fail_line = {}, None
        self.qrun("run")
        ch = self.state_q()["sources"]["d"]["chunks"]["1"]
        self.assertNotIn("error", ch)
        self.assertNotIn("log", ch)

    def test_plain_reason(self):
        r = self.q.plain_reason
        self.assertEqual(r("OSError: [Errno 28] No space left on device: '/home/u/My Photos/x.jpg'"),
                         "OSError: [Errno 28] No space left on device: (a path)")
        self.assertEqual(r("copying DSC 0001.MOV and IMG_1.heic failed, 3 of 12 files"),
                         "copying DSC (a file) and (a file) failed, 3 of 12 files")
        self.assertEqual(r("drive gone and/or unplugged"), "drive gone and/or unplugged")
        self.assertEqual(r("see ~/Pictures/old"), "see (a path)")
        self.assertEqual(r("run bash /x/y/setup.sh first"), "run bash setup.sh first")
        long = r("x " * 200)
        self.assertEqual(len(long), self.q.REASON_MAX)
        self.assertTrue(long.endswith("\u2026"))
        self.assertEqual(self.q.log_reason(os.path.join(self.tmp, "no-such.log")), "")
        log = os.path.join(self.tmp, "step.log")
        with open(log, "w") as f:
            f.write("== 2026-01-01T00:00:00 python3 -I intake.py plan b\nplan: 3 files in /a/b/c\n")
        start = os.path.getsize(log)
        self.assertEqual(self.q.log_reason(log), "plan: 3 files in (a path)")
        self.assertEqual(self.q.log_reason(log, start), "", "only what the failed step wrote")
        with open(log, "a") as f:
            f.write("== 2026-01-01T00:01:00 python3 -I intake.py scan b\n")
        self.assertEqual(self.q.log_reason(log, start), "", "the header line is never the reason")

    def test_run_output_says_done_not_exit_codes(self):
        self.qrun("add", self.root, "--label", "d")
        out = self.qrun("run")
        self.assertIn("  stage done\n", out)
        self.assertIn("  plan done\n", out)
        self.assertNotIn("exit 0", out)

    def test_help_is_for_the_user(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            self.q.main(["--help"], prog="media-intake queue")
        self.assertEqual(e.exception.code, 0)
        h = out.getvalue()
        self.assertIn("media-intake queue add PATH --label NAME", h)
        for maintainer in ("drive_queue.py add", "shadow", "unittest", "Test:"):
            self.assertNotIn(maintainer, h)
        for sub in ("run", "offload", "add"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                self.q.main([sub, "--help"], prog="media-intake queue")
            self.assertNotRegex(out.getvalue(), r"\n  (--inhibit|--yes|path|LABEL)\s*\n", "every option has help")

    def test_no_subcommand_names_the_choices(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as e:
            self.q.main([], prog="media-intake queue")
        self.assertEqual(e.exception.code, 2)
        self.assertIn("add|run|status|offload", err.getvalue())
        self.assertNotIn("cmd", err.getvalue().replace("command", ""))

    def test_drive_unplugged_stops(self):
        self.qrun("add", self.root, "--label", "d")
        os.environ["MEDIA_INTAKE_TEST_UUID"] = "OTHER-0000"
        with self.assertRaises(SystemExit) as e:
            self.qrun("run")
        self.assertIn("not mounted", str(e.exception))
        self.assertEqual(self.calls, [])

    # ------------------------------------------------------------ status (synthetic queue state only)
    def synth(self, sources):
        """Write queue.json and each source's files.json; sources: (label, root, kind, files, chunks) where chunks
        maps "n" to a chunk record. No media, no run: status reads only this state and the quarantine counts."""
        q = {"sources": {}}
        for label, root, kind, nfiles, chunks in sources:
            d = os.path.join(self.state, "queue", label)
            os.makedirs(d, exist_ok=True)
            jsave(os.path.join(d, "files.json"), [[f"f{i}.jpg", 10] for i in range(nfiles)])
            src = dict(label=label, kind=kind, root=root, mount=None, uuid=None, added="2026-01-02T03:04:05",
                       events=[], files=nfiles, bytes=10 * nfiles, chunk=2, chunks=chunks, offloaded=[])
            if chunks and all(c.get("done") for c in chunks.values()) and len(chunks) * 2 >= nfiles:
                src["finished"] = max(c["done"] for c in chunks.values())
            q["sources"][label] = src
        jsave(os.path.join(self.state, "queue.json"), q)

    @staticmethod
    def done_chunk(batch, at, maybe_trash=0):
        steps = {s: dict(at=at, note="exit 0") for s in ("stage", "plan", "scan", "apply", "tag", "index", "vlm")}
        return dict(batch=batch, files=2, steps=steps, summary=dict(maybe_trash=maybe_trash), done=at)

    def quarantine(self, batch, n):
        jsave_dir = os.path.join(self.state, "quarantine", batch)
        os.makedirs(jsave_dir, exist_ok=True)
        jsave(os.path.join(jsave_dir, "quarantine.json"), {f"q{i}.jpg": {} for i in range(n)})

    def every_state(self):
        long_root = "/run/media/someone/A drive with a long name/" + "deep/" * 6 + "Old photos 2009"
        self.synth([
            ("busy", "/mnt/d/busy", "drive", 4,
             {"1": self.done_chunk("b-busy-1", "2026-01-03T10:00:00"),
              "2": dict(batch="b-busy-2", files=2, steps={"stage": dict(at="2026-01-03T11:30:00", note="exit 0")})}),
            ("idle", "/mnt/d/idle", "drive", 3, {}),
            ("broken", "/mnt/d/broken", "drive", 2,
             {"1": dict(batch="b-broken-1", files=2, steps={"stage": dict(at="2026-01-04T08:00:00", note="exit 0"),
                                                            "plan": dict(at="2026-01-04T08:05:00", note="exit 0")},
                        error="scan failed (exit 1); log /state/queue/broken/b-broken-1.log")}),
            ("review", long_root, "drive", 2, {"1": self.done_chunk("b-review-1", "2026-01-05T09:00:00", 2)}),
            ("clean", "/mnt/d/clean", "drive", 2, {"1": self.done_chunk("b-clean-1",
                                                                                          "2026-01-06T12:00:00")}),
            ("pics", os.path.join(self.home, "Pictures", "From old laptop"), "folder", 2,
             {"1": self.done_chunk("b-pics-1", "2026-01-07T13:00:00")}),
        ])
        self.quarantine("b-review-1", 3)
        self.quarantine("near-b-review-1", 1)
        return long_root

    @contextlib.contextmanager
    def held_run_lock(self):
        """Hold <state>/queue/run.lock as a `queue run` does, for the with-block."""
        os.makedirs(os.path.join(self.state, "queue"), exist_ok=True)
        import fcntl
        with open(os.path.join(self.state, "queue", "run.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def test_status_table_rows_for_every_state(self):
        long_root = self.every_state()
        with self.held_run_lock():  # a run is going: the first unfinished source is the one it works on
            out = self.qrun("status")
        lines = out.splitlines()
        self.assertTrue(all(len(x) <= 80 for x in lines[:7]), "the table fits 80 columns")
        self.assertEqual(lines[0].split(), ["LABEL", "SOURCE", "STATE", "FILES", "QUAR", "REVIEW", "CHANGED"])
        src = lines[0].index("SOURCE")
        self.assertEqual([x[:src].strip() for x in lines[1:7]], ["busy", "idle", "broken", "review", "clean", "pics"],
                         "the label queue offload takes, first and never cut")
        cols = [lines[0].index(h) for h in ("STATE", "CHANGED")]
        rows = {}
        for x in lines[1:7]:
            self.assertEqual(x[cols[1]:cols[1] + 16].count(":"), 1, x)  # CHANGED column aligned
            rows[x[cols[0]:].split("  ")[0].strip()] = x
        self.assertEqual(sorted(rows), ["done", "failed", "needs your review", "running", "waiting"])
        busy = lines[1]
        self.assertTrue(busy.startswith("busy  "))
        self.assertTrue(busy[src:].startswith("/mnt/d/busy  "))
        self.assertEqual(busy[cols[0]:].split()[:1] + busy[cols[0]:].split()[1:4],
                         ["running", "4", "0", "0"])
        self.assertTrue(busy.endswith("2026-01-03 11:30"), "the newest recorded time")
        self.assertIn("waiting", lines[2])
        self.assertTrue(lines[2].endswith("2026-01-02 03:04"), "never run: the time it was added")
        self.assertIn("failed", lines[3])
        review = lines[4]
        self.assertEqual(review[cols[0]:].split()[:5], ["needs", "your", "review", "2", "4"])
        self.assertEqual(review[cols[0]:].split()[5], "2")  # 2 trash suggestions, 0 in the review folder
        self.assertIn("\u2026", review, "a long path is shortened in the middle")
        self.assertIn("hotos 2009  ", review, "and keeps its end (the label column takes some of its room)")
        self.assertTrue(review[src:].startswith("/ru\u2026"), "a short start")
        self.assertNotIn(long_root, out.split("Waiting for you:")[0])
        self.assertEqual([x[cols[0]:].split()[0] for x in lines[5:7]], ["done", "done"])

    def skew_tz(self, tz):
        """Run under TZ=tz (time.tzset), put back for whatever the runner set (CI: America/New_York)."""
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = tz
        time.tzset()

    def test_status_changed_is_local_time_under_a_skewed_tz(self):
        """CHANGED shows the time the queue recorded as local wall-clock time, whatever TZ the runner has: here
        UTC+13:45 (a POSIX zone, no tzdata needed), which never shares a minute with UTC."""
        self.skew_tz("XST-13:45")
        before = time.time()
        self.qrun("add", self.root, "--label", "d")
        after = time.time()
        minute = lambda t, tz=None: datetime.datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M")  # noqa: E731
        local = {minute(before), minute(after)}  # the add may straddle a minute
        lines = self.qrun("status").splitlines()
        self.assertEqual(lines[0].split()[-1], "CHANGED")
        changed = lines[1][lines[0].index("CHANGED"):].strip()
        self.assertIn(changed, local)
        self.assertNotIn(changed, {minute(before, datetime.timezone.utc), minute(after, datetime.timezone.utc)})
        self.assertEqual(self.status_json()["sources"][0]["changed"][:16].replace("T", " "), changed)

    def test_status_waiting_section_and_next(self):
        self.every_state()
        out = self.qrun("status")
        table, rest = out.split("\nWaiting for you:\n")
        self.assertIn("waiting", table.splitlines()[1], "no run is going: nothing shows as running")
        self.assertNotIn("running", table)
        waiting, nxt = rest.rstrip("\n").split("\n\n")
        self.assertIn("review, chunk 1 (b-review-1): quarantine 3, near and messaging copies 1, review folder 0, "
                      "trash suggestions 2", " ".join(x.strip() for x in waiting.splitlines()))
        self.assertIn("review page:        media-intake review b-review-1", waiting)
        self.assertIn(f"quarantine:         xdg-open {os.path.join(self.state, 'quarantine', 'b-review-1')}", waiting)
        self.assertIn("media-intake suggest-trash --batch b-review-1", waiting)
        self.assertIn("broken, chunk 1 (b-broken-1) failed at scan: scan failed (exit 1); log "
                      "/state/queue/broken/b-broken-1.log", " ".join(x.strip() for x in waiting.splitlines()))
        self.assertIn("fix it, then retry: media-intake queue run --inhibit", waiting)
        self.assertIn("media-intake queue offload clean --yes", waiting)
        self.assertNotIn("offload busy", waiting, "an unfinished source is never offered for offload")
        self.assertNotIn("offload review", waiting, "nor one with decisions left")
        self.assertNotIn("offload pics", waiting, "nor a laptop folder")
        self.assertIn("pics: everything is reviewed; delete the source folder yourself", waiting)
        self.assertNotIn("delete the source folder", waiting.split("pics:")[0])
        for x in waiting.splitlines():
            if "media-intake" not in x and "/" not in x:
                self.assertLessEqual(len(x), 80, x)
        self.assertEqual(nxt.splitlines(), ["next: fix broken (see above), then media-intake queue run --inhibit"])
        # Each fix moves the next line on to the following decision.
        q = self.state_q()
        del q["sources"]["broken"]
        jsave(os.path.join(self.state, "queue.json"), q)
        self.assertTrue(self.qrun("status").endswith("next: media-intake review b-review-1\n"))
        del q["sources"]["review"]
        jsave(os.path.join(self.state, "queue.json"), q)
        self.assertTrue(self.qrun("status").endswith("next: media-intake queue offload clean --yes\n"))
        del q["sources"]["clean"], q["sources"]["busy"]["chunks"]["1"]
        jsave(os.path.join(self.state, "queue.json"), q)
        self.assertIn("next: check the library, then delete", self.qrun("status"))
        del q["sources"]["pics"]
        jsave(os.path.join(self.state, "queue.json"), q)
        self.assertTrue(self.qrun("status").endswith("next: media-intake queue run --inhibit\n"))
        with self.held_run_lock():
            self.assertIn("next: nothing yet: the run is going", self.qrun("status"))

    def test_status_nothing_waits(self):
        self.synth([("one", "/mnt/d/one", "drive", 2,
                     {"1": self.done_chunk("b-one-1", "2026-01-06T12:00:00")})])
        q = self.state_q()
        q["sources"]["one"]["offloaded"] = [1]
        jsave(os.path.join(self.state, "queue.json"), q)
        out = self.qrun("status")
        self.assertIn("Waiting for you:\n  nothing\n", out)
        self.assertTrue(out.endswith("next: nothing waits; add another folder: media-intake queue add PATH --label NAME\n"))

    def test_shorten_keeps_the_end(self):
        s = self.q.shorten
        self.assertEqual(s("/short", 20), "/short")
        cut = s("/run/media/someone/Drive/a/b/c/Holiday 2011", 20)
        self.assertEqual(len(cut), 20)
        self.assertTrue(cut.startswith("/run") and cut.endswith("/c/Holiday 2011"))
        self.assertEqual(cut.count("\u2026"), 1)

    def test_status_empty_queue(self):
        out = self.qrun("status")
        self.assertEqual(out.count("\n"), 1, "one line")
        self.assertIn("queue is empty", out)
        self.assertIn("media-intake queue add PATH --label NAME", out)
        self.assertEqual(self.status_json(), dict(sources=[], waiting=[], next="media-intake queue add PATH --label NAME"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "queue")), "status creates nothing")

    def test_status_json_shape(self):
        self.every_state()
        m = self.status_json()
        self.assertEqual(sorted(m), ["next", "sources", "waiting"])
        self.assertEqual([r["label"] for r in m["sources"]], ["busy", "idle", "broken", "review", "clean", "pics"])
        self.assertEqual([r["state"] for r in m["sources"]],
                         ["waiting", "waiting", "failed", "needs your review", "done", "done"])
        r = m["sources"][3]
        self.assertEqual(sorted(r), ["bytes", "changed", "chunks", "chunks_done", "chunks_total", "files", "in_review",
                                     "kind", "label", "offloaded", "quarantined", "root", "state"])
        self.assertEqual((r["files"], r["quarantined"], r["in_review"], r["changed"]),
                         (2, 4, 2, "2026-01-05T09:00:00"))
        self.assertEqual(r["chunks"], [dict(chunk=1, batch="b-review-1", files=2, done=True, quarantine=3,
                                            near_and_messaging=1, review_folder=0, trash_suggestions=2)])
        self.assertEqual(m["sources"][2]["chunks"][0]["step"], "scan")
        self.assertEqual([(w["what"], w["label"]) for w in m["waiting"]],
                         [("failed", "broken"), ("review", "review"), ("offload", "clean"), ("delete-folder", "pics")])
        self.assertEqual(m["waiting"][0]["commands"], ["media-intake queue run --inhibit"])
        self.assertEqual(m["waiting"][1]["commands"][0], "media-intake review b-review-1")
        self.assertTrue(m["next"].startswith("fix broken"))

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
        # A messaging cross-check (or any near- level of it) waits on the same review page: offload waits too.
        xj = os.path.join(self.state, "quarantine", f"near-xcheck-{batch}", "quarantine.json")
        os.makedirs(os.path.dirname(xj))
        jsave(xj, {"Downloads/n.jpg": dict(reasons=["near-duplicate"], sha="0" * 64, size=1, dst=None)})
        with self.assertRaises(SystemExit) as e:
            self.qrun("offload", "d", "--yes")
        self.assertIn("chunk 1 (quarantine 0, near and messaging copies 1)", str(e.exception))
        self.assertIn("near and messaging copies 1", self.qrun("status"))
        jsave(xj, {})
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

    # ------------------------------------------------------------ kind folder (a laptop folder)
    def make_folder(self):
        f = os.path.join(self.home, "Pictures", "From old laptop")
        for i in range(3):
            picture(os.path.join(f, "2018", f"P_{i:04d}.jpg"), i + 20, f"2018:0{i + 1}:10 12:00:00")
        picture(os.path.join(f, ".hidden", "h.jpg"), 40, "2018:05:05 05:05:05")
        picture(os.path.join(f, "site", "img", "logo.jpg"), 41, "2018:06:06 06:06:06")
        os.makedirs(os.path.join(f, "site", ".git"))  # a git tree inside the folder: skipped
        os.symlink(os.path.join(self.root, "2019", "IMG_0000.jpg"), os.path.join(f, "2018", "link.jpg"))
        return f

    def test_folder_end_to_end_and_never_offloaded(self):
        os.environ.pop("MEDIA_INTAKE_TEST_MOUNT")  # no drive anywhere: a folder needs no mount or UUID
        f = self.make_folder()
        out = self.qrun("add", f, "--label", "old-folder", "--kind", "folder", "--event", "demo/test-shoot@2018-01-10")
        self.assertIn("3 files", out)
        src = self.state_q()["sources"]["old-folder"]
        self.assertEqual((src["kind"], src["uuid"], src["mount"], src["events"]),
                         ("folder", None, None, ["demo/test-shoot@2018-01-10"]))
        self.qrun("run", "--chunk", "2")
        src = self.state_q()["sources"]["old-folder"]
        self.assertTrue(src.get("finished"))
        self.assertIn(("plan", src["chunks"]["1"]["batch"]), self.calls)
        man = jload(os.path.join(self.state, "batches", src["chunks"]["1"]["batch"], "manifest.json"))
        self.assertEqual(man["kind"], "folder")
        shoot = os.path.join(self.home, "Videos/demo-footage/originals/2018-01-10-test-shoot/phone")
        self.assertEqual([x for x in os.listdir(shoot) if x.endswith(".jpg")], ["P_0000.jpg"],
                         "an --event day with a project goes to the project's folder")
        filed = [x for r, _, fs in os.walk(self.lib) for x in fs if x.endswith(".jpg")]
        self.assertEqual(sorted(filed), ["P_0001.jpg", "P_0002.jpg"])
        st = self.qrun("status")
        self.assertIn("trash suggestions 1", st)
        self.assertNotIn("delete the source folder", st, "not while you still have decisions")
        q = self.state_q()
        for ch in q["sources"]["old-folder"]["chunks"].values():
            ch["summary"]["maybe_trash"] = 0  # the owner went through the suggestions
        jsave(os.path.join(self.state, "queue.json"), q)
        st = self.qrun("status")
        self.assertIn("delete the source folder yourself", st)
        self.assertNotIn("offload old-folder", st)
        before = sorted(os.listdir(os.path.join(f, "2018")))
        with self.assertRaises(SystemExit) as e:
            self.qrun("offload", "old-folder", "--yes")
        self.assertIn("never deletes", str(e.exception))
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "intake.py"), "offload",
                            src["chunks"]["1"]["batch"], "--phone", f, "--yes"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never deletes", r.stderr)
        # erase --phone and trash --phone reach the same device-delete path: a folder laid out
        # like a phone must not pass the phone allowlist into deleting its originals.
        intake_py = os.path.join(HERE, "intake.py")
        b1 = src["chunks"]["1"]["batch"]
        one = next(os.path.join(r_, x) for r_, _, fs in os.walk(self.lib) for x in fs if x.endswith(".jpg"))
        for args in (["erase", b1, "--phone", f, "--yes"], ["trash", "--batch", b1, one, "--phone", f, "--yes"]):
            r = subprocess.run([sys.executable, "-I", intake_py, *args], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("never deletes", r.stderr, args)
        self.assertTrue(os.path.exists(one), "a refused trash trashes nothing")
        # Without a named batch, the folder batch is simply not a device source.
        r = subprocess.run([sys.executable, "-I", intake_py, "trash", one, "--phone", f, "--yes"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("phone 0", r.stdout)
        self.assertEqual(sorted(os.listdir(os.path.join(f, "2018"))), before, "originals untouched")

    def folder_library(self):
        """{file name: sha256} of every photo filed from the folder (library and project folders)."""
        out = {}
        for top in (self.lib, os.path.join(self.home, "Videos")):
            for r, _, fs in os.walk(top):
                for x in fs:
                    if x.endswith(".jpg"):
                        self.assertNotIn(x, out, f"{x} filed twice")
                        with open(os.path.join(r, x), "rb") as fh:
                            out[x] = hashlib.sha256(fh.read()).hexdigest()
        return out

    def test_folder_rerun_after_completion_does_nothing(self):
        """A finished folder source run again (with another --chunk): no step runs, nothing is filed twice, the
        recorded chunks and the finished time stay as they were, and the folder's originals are untouched."""
        os.environ.pop("MEDIA_INTAKE_TEST_MOUNT")
        f = self.make_folder()
        before = sorted(os.listdir(os.path.join(f, "2018")))
        self.qrun("add", f, "--label", "old-folder", "--kind", "folder")
        self.qrun("run", "--chunk", "2")
        q1, lib1 = self.state_q(), self.folder_library()
        self.assertEqual(sorted(lib1), ["P_0000.jpg", "P_0001.jpg", "P_0002.jpg"])
        self.assertTrue(q1["sources"]["old-folder"].get("finished"))
        batches1 = sorted(os.listdir(os.path.join(self.state, "batches")))
        self.calls = []
        out = self.qrun("run", "--chunk", "1")
        self.assertEqual(self.calls, [], "no step of a finished source runs again")
        self.assertNotIn("chunk 1/", out)
        self.assertIn("queue run finished", out)
        self.assertEqual(self.state_q(), q1, "chunk size, steps, summaries and finished time unchanged")
        self.assertEqual(self.folder_library(), lib1)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "batches"))), batches1)
        self.assertEqual(self.status_json()["sources"][0]["chunks_done"], 2)
        self.assertEqual(sorted(os.listdir(os.path.join(f, "2018"))), before)

    def test_folder_kill_mid_chunk_then_resume(self):
        """Killed in chunk 2 after its stage copied the files but before the queue marked it: the next run
        redoes only that stage (it copies nothing twice) and finishes the chunk; chunk 1 is never touched
        again, and every folder photo is filed exactly once."""
        os.environ.pop("MEDIA_INTAKE_TEST_MOUNT")
        f = self.make_folder()
        self.qrun("add", f, "--label", "old-folder", "--kind", "folder")
        self.kill_after = ("stage", 2)
        with self.assertRaises(KeyboardInterrupt):
            self.qrun("run", "--chunk", "2")
        src = self.state_q()["sources"]["old-folder"]
        self.assertTrue(src["chunks"]["1"].get("done"))
        self.assertFalse(src["chunks"].get("2", {}).get("steps"), "the killed stage was never marked")
        self.assertNotIn("finished", src)
        self.assertEqual((self.status_json()["sources"][0]["chunks_done"], self.status_json()["sources"][0]["state"]),
                         (1, "needs your review"), "chunk 1's trash suggestion waits for you while chunk 2 is left")
        b2 = self.q.batch_name(src, 2)
        raw2 = os.path.join(self.state, "batches", b2, "raw")
        staged = [x for _, _, fs in os.walk(raw2) for x in fs]
        self.assertEqual(len(staged), 1, "chunk 2 (one file) was copied before the kill")
        ch1 = src["chunks"]["1"]
        self.kill_after, self.calls = None, []
        self.qrun("run")
        steps = [(s, b) for s, b in self.calls if b is not None]  # stage records its root, not the batch
        self.assertEqual([s for s, _ in steps], ["stage", "plan", "scan", "apply", "tag"], "chunk 1 is never redone")
        self.assertEqual({b for s, b in steps if s != "stage"}, {b2})
        src = self.state_q()["sources"]["old-folder"]
        self.assertTrue(src.get("finished"))
        self.assertEqual(src["chunks"]["1"], ch1)
        self.assertEqual(sum(c["summary"]["files"] for c in src["chunks"].values()), 3)
        self.assertEqual(sorted(self.folder_library()), ["P_0000.jpg", "P_0001.jpg", "P_0002.jpg"])
        self.assertEqual(sorted(os.listdir(os.path.join(f, "2018"))), ["P_0000.jpg", "P_0001.jpg", "P_0002.jpg",
                                                                        "link.jpg"], "originals untouched")

    def test_folder_refused_roots(self):
        f = self.make_folder()
        os.makedirs(os.path.join(self.home, "repos", "x"))
        for bad in (self.home, self.state, os.path.join(self.state, "quarantine"), self.lib,
                    os.path.join(self.home, "Pictures"), os.path.join(self.home, ".var/app/x"),
                    os.path.join(self.home, "repos/x"), os.path.join(self.home, ".ssh"),
                    os.path.join(self.home, "Videos/demo-footage/originals"), os.path.join(f, "site"), self.root):
            os.makedirs(bad, exist_ok=True)
            with self.assertRaises(SystemExit, msg=bad) as e:
                self.qrun("add", bad, "--label", "x", "--kind", "folder")
            self.assertIn("refused", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.state, "queue.json")))
        for ev in ("Bad Event", "shoot@2018-1-1", "nope/shoot@2018-01-10"):
            with self.assertRaises(SystemExit, msg=ev) as e:
                self.qrun("add", f, "--label", "x", "--kind", "folder", "--event", ev)
            self.assertIn("refused: --event", str(e.exception))
        self.qrun("add", f, "--label", "x", "--kind", "folder")
        with self.assertRaises(SystemExit) as e:
            self.qrun("add", os.path.join(f, "2018"), "--label", "y", "--kind", "folder")
        self.assertIn("overlaps", str(e.exception))

    def test_folder_refused_at_run_time(self):
        f = self.make_folder()
        self.qrun("add", f, "--label", "x", "--kind", "folder")
        os.makedirs(os.path.join(f, ".git"))  # it became a repository after it was queued
        with self.assertRaises(SystemExit) as e:
            self.qrun("run")
        self.assertIn("folder refused now", str(e.exception))
        self.assertEqual(self.calls, [])

    def test_stage_kind_folder_refuses_directly(self):
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "intake.py"), "stage", self.home,
                            "--kind", "folder", "--batch", "b"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("home folder", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "batches", "b", "manifest.json")))


if __name__ == "__main__":
    unittest.main()
