#!/usr/bin/env python3
"""Tests for library_lock.py: one lock per library, taken by every mutating CLI command (intake.py main()),
by every detached job (jobs.py, inherited by its steps) and by the gallery's direct sidecar writes.

  python3 -I -m unittest discover -s tests -p test_library_lock.py

No sleeps: a holder is held mid-action on a FIFO the test opens (a gated exiftool for the gallery, an
erase's own ERASE prompt for the CLI, a fake intake.py step for a job), and every wait is on a real
signal (the lock's holder note plus a failing probe, a marker file, a status file, a stderr line).
State, library and HOME live in a throwaway dir.
"""
import errno, fcntl, json, os, shutil, signal, subprocess, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config; puts media_intake on sys.path)
import intake  # noqa: E402  (LOCKED / READ_ONLY, build_parser)
import jobs  # noqa: E402
import library_lock  # noqa: E402
from test_jobs import until  # noqa: E402

PY = sys.executable
INTAKE = os.path.join(support.PKG, "intake.py")
ENV_KEYS = ("HOME", "MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_REVIEW_INTAKE",
            library_lock.WAIT_ENV, library_lock.FD_ENV, "FAKE_GATE")

# A process that takes the lock as `what`, says so, and holds it until its stdin closes.
HOLDER = r'''
import sys
sys.path.insert(0, sys.argv[1])
import library_lock
fd = library_lock.acquire(sys.argv[2])
print("held", flush=True)
sys.stdin.read()
'''

# A job step (MEDIA_REVIEW_INTAKE) that takes the library lock the way intake.py main() does, with no wait,
# says whether it was inherited, then waits on the FIFO in FAKE_GATE.
STEP = r'''
import os, sys
sys.path.insert(0, os.environ["PKG"])
import library_lock
inh = library_lock.inherited() is not None
with library_lock.hold("fake step", wait=0):
    print("step holds the library lock, inherited=%s" % inh, flush=True)
    with open(os.environ["FAKE_GATE"]) as f:
        f.read()
print("step done", flush=True)
'''

# exiftool that, while FAKE_EXIFTOOL_GATE exists, marks that it started and waits on that FIFO once.
EXIFTOOL = r'''#!{py} -I
import os, sys
gate = os.environ.get("FAKE_EXIFTOOL_GATE", "")
if gate and os.path.exists(gate) and "-@" in sys.argv:
    open(gate + ".waiting", "w").close()
    with open(gate) as f:
        f.read()
    os.remove(gate)
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
'''


def read(p):
    with open(p) as f:
        return f.read()


def open_gate(fifo):
    def w():
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENXIO:  # the reader has not opened it yet
                return False
            raise
        os.write(fd, b"go")
        os.close(fd)
        return True
    until(w, "the gated process to open the FIFO")


class LibraryLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="liblock-test-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        os.makedirs(self.lib)
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in (library_lock.FD_ENV, "MEDIA_REVIEW_INTAKE", library_lock.WAIT_ENV):
            os.environ.pop(k, None)
        os.environ.update(HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                          PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"))
        self.env = dict(os.environ, **{library_lock.WAIT_ENV: "0"})
        self.path = library_lock.lock_path()
        self.procs = []
        # A quarantined batch and a review folder: what erase and purge-review act on.
        q = os.path.join(self.state, "quarantine", "q1")
        os.makedirs(q)
        self.qfile = os.path.join(q, "x.jpg")
        with open(self.qfile, "wb") as f:
            f.write(b"not really a jpeg")
        with open(os.path.join(q, "quarantine.json"), "w") as f:
            json.dump({"x.jpg": dict(sha="0" * 64, size=17, reasons=["test"], dst=None)}, f)
        self.review = os.path.join(self.state, "batches", "q1", "review")
        os.makedirs(self.review)
        with open(os.path.join(self.review, "r.jpg"), "wb") as f:
            f.write(b"r")

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for s in (p.stdin, p.stdout, p.stderr):
                if s:
                    s.close()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    # ------------------------------------------------------------ helpers
    def popen(self, args, env=None, **kw):
        p = subprocess.Popen(args, env=env or self.env, text=True, **kw)
        self.procs.append(p)
        return p

    def intake(self, *args, env=None):
        return subprocess.run([PY, "-I", INTAKE, *args], env=env or self.env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)

    def held_by(self, what):
        """The lock is held and its note names what. The note is written only once the holder has the flock,
        so the probe (which takes the lock for an instant if it is free) runs only then, and never races the
        holder's own non-waiting acquire."""
        if library_lock.read_holder(self.path).get("what") != what:
            return False
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def hold_in_child(self, what):
        p = self.popen([PY, "-I", "-c", HOLDER, support.PKG, what], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.readline(), "held\n")
        return p

    # ------------------------------------------------------------ tests
    def test_every_subcommand_is_classified(self):
        """Each subcommand is locked or read-only, never both and never forgotten (a new one fails here)."""
        ap = intake.build_parser()
        cmds = set(next(a for a in ap._actions if a.dest == "cmd").choices)
        self.assertEqual(intake.LOCKED & intake.READ_ONLY, set())
        self.assertEqual(intake.LOCKED | intake.READ_ONLY, cmds)
        for must in ("stage", "plan", "scan", "apply", "release", "erase", "forget", "trash", "not-trash", "tag",
                     "tag-files", "offload", "purge-review", "mark-used"):
            self.assertIn(must, intake.LOCKED)

    def test_lock_file_is_private_and_per_library(self):
        fd = library_lock.acquire("me")
        try:
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(self.path)).st_mode & 0o777, 0o700)
            self.assertTrue(self.path.startswith(os.path.join(self.state, "locks", "library-")))
            h = library_lock.read_holder(self.path)
            self.assertEqual((h["what"], h["pid"]), ("me", os.getpid()))
        finally:
            os.close(fd)
        other = dict(library_lock.config.get(), library=os.path.join(self.tmp, "other-lib"))
        self.assertNotEqual(library_lock.lock_path(other), self.path)

    def test_cli_erase_vs_gallery_action(self):
        """A CLI erase and a gallery Keep never run at once, whichever starts first; the one refused names the
        holder, and a CLI command given a wait goes ahead once the gallery's write ends."""
        real = shutil.which("exiftool")
        if not real:
            self.skipTest("needs exiftool (perl-Image-ExifTool)")
        from test_gallery import Server, img
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        with open(os.path.join(bin_, "exiftool"), "w") as f:
            f.write(EXIFTOOL.replace("{py}", PY).replace("{real!r}", repr(real)))
        os.chmod(os.path.join(bin_, "exiftool"), 0o700)
        gate = os.path.join(self.tmp, "exif-gate")
        a = os.path.join(self.lib, "2026/2026-11/a.jpg")
        img(a, (200, 30, 30))
        env = dict(self.env, PATH=bin_ + os.pathsep + os.environ.get("PATH", ""), FAKE_EXIFTOOL_GATE=gate)
        s = Server(env)
        try:
            ida = next(i["id"] for i in s.get("/api/list?limit=50")["items"] if i["n"] == "a.jpg")
            # 1. A CLI erase is at its ERASE prompt, holding the lock: the gallery's Keep gets 409 naming it.
            er = self.popen([PY, "-I", INTAKE, "erase", "q1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
            until(lambda: self.held_by("intake.py erase"), "the CLI erase to hold the lock")
            code, j = s.post("/api/keep", {"ids": [ida]})
            self.assertEqual(code, 409, j)
            self.assertIn(f"intake.py erase (PID {er.pid})", j["error"])
            self.assertFalse(os.path.exists(a + ".xmp"))  # nothing written
            self.assertEqual(s.get("/api/list?limit=50")["total"], 1)  # reading is never locked
            er.stdin.write("no\n")
            er.stdin.close()
            self.assertEqual(er.wait(60), 1)
            self.assertIn("nothing erased", er.stderr.read())
            self.assertTrue(os.path.exists(self.qfile))
            # 2. The gallery's Keep is mid-write (exiftool waits on the gate): a CLI erase is refused at once.
            os.mkfifo(gate)
            got = {}
            t = threading.Thread(target=lambda: got.update(r=s.post("/api/keep", {"ids": [ida]})))
            t.start()
            until(lambda: os.path.exists(gate + ".waiting") and self.held_by("gallery keep"), "Keep to hold the lock")
            r = self.intake("erase", "q1", "--yes")
            self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
            self.assertIn(f"refused: the library is busy: gallery keep (PID {s.p.pid})", r.stderr)
            self.assertTrue(os.path.exists(self.qfile))  # erase never started
            # With a wait it says so, then runs once Keep is done.
            pr = self.popen([PY, "-I", INTAKE, "purge-review", "q1"], env=dict(self.env, **{library_lock.WAIT_ENV: "120"}),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertIn("waiting up to 120 s: gallery keep", pr.stderr.readline())
            self.assertTrue(os.path.isdir(self.review))
            open_gate(gate)
            t.join(60)
            self.assertEqual(got["r"][0], 200, got)
            self.assertEqual(pr.wait(60), 0)
            self.assertFalse(os.path.exists(self.review))
            self.assertIn("Owner|keep", read(a + ".xmp"))
        finally:
            s.stop()

    def test_job_step_inherits_the_lock(self):
        """A job holds the library lock from claim() to its last step; each step sees it as inherited (no
        deadlock with its own worker), everything else is refused naming the job, also after the worker dies
        while a step it started still runs."""
        fake, gate = os.path.join(self.tmp, "step.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(STEP)
        os.mkfifo(gate)
        os.environ.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate, PKG=support.PKG)
        try:
            with jobs.claim("gallery") as c:
                self.assertTrue(self.held_by("job gallery"))  # taken with the claim, before any worker
                c.spawn("commands", dict(argsets=[["x"]], paths=[]), total=1)
            until(lambda: any("inherited=True" in ln for ln in jobs.lines("gallery")[0]), "the step to hold it")
            pid = jobs.status("gallery")["pid"]
            self.assertTrue(self.held_by("job gallery"))
            # another job key (a review run) is refused: the pages lock each other out
            with self.assertRaises(jobs.Busy) as cm:
                jobs.claim("review-b1")
            self.assertIn(f"job gallery (PID {pid})", str(cm.exception))
            self.assertIsNone(jobs.status("review-b1"))
            # the CLI is refused, naming the job
            r = self.intake("tag-files", os.path.join(self.lib, "none.jpg"))
            self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
            self.assertIn(f"job gallery (PID {pid})", r.stderr)
            # the worker dies; the step it started still holds the lock
            os.kill(pid, signal.SIGKILL)
            until(lambda: jobs.status("gallery")["state"] == "crashed", "the job to be reported crashed")
            r = self.intake("purge-review", "q1")
            self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
            self.assertIn("now gone: a step it started is still running", r.stderr)
            self.assertTrue(os.path.isdir(self.review))
            open_gate(gate)
            until(lambda: not jobs.status("gallery")["step_running"], "the orphaned step to end")
            r = self.intake("purge-review", "q1")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(self.review))
        finally:
            for k in ("MEDIA_REVIEW_INTAKE", "FAKE_GATE", "PKG"):
                os.environ.pop(k, None)

    def test_real_intake_step_runs_under_its_job(self):
        """The real intake.py, run as a job step with no wait allowed, takes the inherited lock and does its work."""
        os.environ[library_lock.WAIT_ENV] = "0"  # a step that failed to inherit would be refused, not wait
        with jobs.claim("review-q1") as c:
            st = c.spawn("commands", dict(argsets=[["purge-review", "q1"]], paths=[]), total=1)
        end = jobs.wait("review-q1", st["id"], interval=0.02)
        self.assertTrue(end["ok"], jobs.lines("review-q1")[0])
        self.assertFalse(os.path.exists(self.review))
        self.assertFalse(any("refused" in ln for ln in jobs.lines("review-q1")[0]))
        os.close(library_lock.acquire("after"))  # the worker let go at the end

    def test_crashed_holder_releases_the_lock(self):
        """flock dies with its process: a SIGKILLed holder leaves only a stale note, which blocks nothing."""
        p = self.hold_in_child("intake.py apply")
        self.assertTrue(self.held_by("intake.py apply"))
        r = self.intake("not-trash", os.path.join(self.lib, "none.jpg"))
        self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
        self.assertIn(f"intake.py apply (PID {p.pid})", r.stderr)
        p.kill()  # by PID
        p.wait()
        self.assertEqual(library_lock.read_holder(self.path)["what"], "intake.py apply")  # stale, and harmless
        r = self.intake("purge-review", "q1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.review))

    def test_inherit_marker_cannot_be_spoofed(self):
        """Setting the variable is not enough: the descriptor must be this library's lock file, already
        holding the lock (inherited from the holder)."""
        p = self.hold_in_child("intake.py apply")
        own = os.open(self.path, os.O_RDWR)  # this process's own open of the lock file
        other = os.open(os.path.join(self.state, "quarantine", "q1", "quarantine.json"), os.O_RDONLY)
        try:
            for fd in (own, other, 987, "x"):
                os.environ[library_lock.FD_ENV] = str(fd)
                self.assertIsNone(library_lock.inherited())
                with self.assertRaises(library_lock.Busy):
                    with library_lock.hold("spoof"):
                        self.fail("ran without the lock")
            env = dict(self.env, **{library_lock.FD_ENV: "0"})  # a child's stdin is no lock either
            r = self.intake("purge-review", "q1", env=env)
            self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
            self.assertTrue(os.path.isdir(self.review))
        finally:
            os.environ.pop(library_lock.FD_ENV, None)
            os.close(own)
            os.close(other)
            p.stdin.close()
            p.wait()

    def test_read_only_commands_are_unaffected(self):
        p = self.hold_in_child("intake.py apply")
        before = read(self.path)
        r = self.intake("quarantine", "q1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("busy", r.stderr)
        r = self.intake("verify-gone", "q1")  # no erase-log entries: its own exit, not the lock's
        self.assertNotEqual(r.returncode, library_lock.EX_TEMPFAIL)
        self.assertNotIn("busy", r.stderr)
        r = self.intake("find", "--root", self.lib)
        self.assertNotEqual(r.returncode, library_lock.EX_TEMPFAIL)
        self.assertNotIn("busy", r.stderr)
        self.assertEqual(read(self.path), before)  # nobody re-noted it
        self.assertTrue(self.held_by("intake.py apply"))
        p.stdin.close()
        p.wait()

    def test_vlm_trash_writes_under_the_lock(self):
        """vlm_trash.py's verdicts (sidecar writes, a CLI command run in the venv) wait for the lock, then
        refuse; a file that went away meanwhile gets no sidecar."""
        import vlm_trash
        a, gone = os.path.join(self.lib, "a.jpg"), os.path.join(self.lib, "gone.jpg")
        with open(a, "wb") as f:
            f.write(b"x")
        os.environ[library_lock.WAIT_ENV] = "0"
        p = self.hold_in_child("intake.py apply")
        with self.assertRaises(SystemExit) as cm:
            vlm_trash.write_verdicts([(a, "trash", "blurry", 0.9)], {})
        self.assertIn("refused: the library is busy: intake.py apply", str(cm.exception))
        self.assertFalse(os.path.exists(a + ".xmp"))
        p.stdin.close()
        p.wait()
        if not shutil.which("exiftool"):
            return
        vlm_trash.write_verdicts([(a, "trash", "blurry", 0.9), (gone, "trash", "dark", 0.9)], {})
        self.assertIn("AI|vlm/", read(a + ".xmp"))
        self.assertFalse(os.path.exists(gone + ".xmp"))

    def test_bad_wait_setting(self):
        r = self.intake("purge-review", "q1", env=dict(self.env, **{library_lock.WAIT_ENV: "soon"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(library_lock.WAIT_ENV, r.stderr)
        self.assertTrue(os.path.isdir(self.review))

    def test_wait_setting_edges(self):
        """Negative and over a day are refused before anything runs; 0 and exactly a day are accepted."""
        for bad in ("-1", "-0.5", "86400.5", "86401", "1e9"):
            with self.subTest(wait=bad):
                r = self.intake("purge-review", "q1", env=dict(self.env, **{library_lock.WAIT_ENV: bad}))
                self.assertNotEqual(r.returncode, 0)
                self.assertNotEqual(r.returncode, library_lock.EX_TEMPFAIL, "a bad setting, not a busy lock")
                self.assertIn(f"{library_lock.WAIT_ENV}={bad!r}: between 0 and 86400 seconds", r.stderr)
                self.assertTrue(os.path.isdir(self.review))
        for ok, want in (("0", 0.0), ("86400", 86400.0), ("", library_lock.CLI_WAIT)):
            os.environ[library_lock.WAIT_ENV] = ok
            self.assertEqual(library_lock.cli_wait(), want)
        os.environ.pop(library_lock.WAIT_ENV)

    def test_zero_wait_refuses_at_once(self):
        """0: a busy lock is refused on the first try, with no wait announced and nothing done."""
        p = self.hold_in_child("intake.py apply")
        r = self.intake("purge-review", "q1", env=dict(self.env, **{library_lock.WAIT_ENV: "0"}))
        self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
        self.assertNotIn("waiting", r.stderr)
        self.assertIn(f"refused: the library is busy: intake.py apply (PID {p.pid})", r.stderr)
        self.assertTrue(os.path.isdir(self.review))
        p.stdin.close()
        p.wait()

    def test_review_submit_refused_while_another_writer_holds_the_lock(self):
        """The review page never waits: Submit gets 409 naming the holder, and no run starts."""
        from test_review_ui import Server
        p = self.hold_in_child("intake.py apply")
        s = Server(self.env, batch="q1")
        try:
            r, b = s.req("GET", "/api/items")
            self.assertEqual(r.status, 200, b)
            items = json.loads(b)["items"]
            self.assertTrue(items)
            pick = {"quarantine": "erase", "review": "delete", "maybe": "keep"}
            dec = {str(i): pick[it["tab"]] for i, it in enumerate(items)}  # submit takes the list index
            r, b = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()}, body={"decisions": dec, "confirm": "ERASE"})
            self.assertEqual(r.status, 409, b)
            self.assertIn(f"intake.py apply (PID {p.pid})", json.loads(b)["error"])
            self.assertIsNone(jobs.status("review-q1"))
            self.assertTrue(os.path.exists(self.qfile) and os.path.isdir(self.review), "nothing was run")
        finally:
            s.stop()
            p.stdin.close()
            p.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
