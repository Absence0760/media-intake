#!/usr/bin/env python3
"""Tests for jobs.py: detached, re-attachable jobs with an flock and a status file.

  python3 -I -m unittest discover -s tests -p test_jobs.py

intake.py is replaced (MEDIA_REVIEW_INTAKE) by a fake whose "block" step waits on
a FIFO the test opens, so a job can be held mid-step without any sleep; every wait
is on the status file, the log or the lock. State lives in a throwaway dir.
"""
import errno, os, shutil, signal, stat, subprocess, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config; puts media_intake on sys.path)
import jobs  # noqa: E402

PY = sys.executable

FAKE = r'''
import os, sys
cmd = sys.argv[1]
if cmd == "block":
    print("started", *sys.argv[3:], flush=True)
    with open(sys.argv[2]) as f:
        f.read()
    print("released", flush=True)
elif cmd == "ok":
    print("hi " + " ".join(sys.argv[2:]))
elif cmd == "fail":
    print("failing")
    sys.exit(3)
'''


def until(pred, what, timeout=30):
    """Poll a real condition (status file, log, lock) until it holds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jobs-test-")
        self.state = os.path.join(self.tmp, "state")
        self.fake = os.path.join(self.tmp, "fake_intake.py")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        self.fifo = os.path.join(self.tmp, "gate")
        os.mkfifo(self.fifo)
        self.saved = {k: os.environ.get(k) for k in ("MEDIA_INTAKE_STATE", "MEDIA_REVIEW_INTAKE")}
        os.environ["MEDIA_INTAKE_STATE"] = self.state
        os.environ["MEDIA_REVIEW_INTAKE"] = self.fake
        self.gates = 0  # fakes waiting on the FIFO that the test has not released yet

    def tearDown(self):
        while self.gates:  # never leave a fake blocked on the FIFO
            try:
                self.open_gate(timeout=5)
            except AssertionError:
                break
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def open_gate(self, timeout=30):
        def w():
            try:
                fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno == errno.ENXIO:  # the fake has not opened it yet
                    return False
                raise
            os.write(fd, b"go")
            os.close(fd)
            return True
        until(w, "the fake to open the FIFO", timeout)
        self.gates -= 1

    def start(self, key, argsets):
        if any(a[0] == "block" for a in argsets):
            self.gates += 1
        with jobs.claim(key) as c:
            return c.spawn("commands", dict(argsets=argsets, paths=[]), total=len(argsets))

    def start_erase(self, key, argsets, paths):
        """A redact job, as the gallery and the review page start an erase: paths only in private."""
        if any(a[0] == "block" for a in argsets):
            self.gates += 1
        c = jobs.claim(key)
        st = c.spawn("commands", dict(count=len(paths)), total=len(argsets),
                     private=dict(argsets=argsets, paths=paths), redact=True)
        c.release()
        return st, c.tail

    def names_left(self, names):
        """Every file under <state>/jobs/ (names and contents) that still holds one of names."""
        d = os.path.join(self.state, "jobs")
        hits = []
        for f in os.listdir(d):
            with open(os.path.join(d, f), "rb") as fh:
                data = fh.read()
            hits += [(f, n) for n in names if n.encode() in data or n in f]
        return hits

    def log_has(self, key, text):
        return any(text in ln for ln in jobs.lines(key)[0])

    # ------------------------------------------------------------ tests
    def test_runs_to_done_with_private_files(self):
        st = self.start("t1", [["ok", "a"], ["fail"]])
        self.assertIn(st["state"], ("starting", "running"))
        end = jobs.wait("t1", st["id"], interval=0.02)
        self.assertEqual((end["state"], end["ok"], end["done"]), ("done", False, True))
        self.assertEqual((end["step"], end["total"]), (2, 2))
        ls, nxt = jobs.lines("t1")
        self.assertEqual(ls, ["$ intake.py ok a", "  hi a", "  [exit 0]", "$ intake.py fail", "  failing",
                              "  [exit 3]", "done: 0 of 0 gone from the library; something failed, see above"])
        self.assertEqual(jobs.lines("t1", 5), (ls[5:], nxt))
        d = os.path.join(self.state, "jobs")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        for f in ("t1.json", "t1.log", "t1.lock"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600, f)
        pub = jobs.public(end)
        self.assertNotIn("spec", pub)
        self.assertNotIn("pid", pub)
        # the lock was released with the worker: the next job may start
        jobs.claim("t1").release()

    def test_erase_leaves_no_names(self):
        """A redact job: paths only on the worker's pipe, command lines without them, and once it ends every
        line of the steps' output (which names files) is replaced in place; counts, labels and ok stay."""
        lib = os.path.join(self.tmp, "Pictures")
        paths = [os.path.join(lib, "secret-one.jpg"), os.path.join(lib, "secret-two.jpg")]
        st, tail = self.start_erase("e1", [["ok", *paths], ["fail", "--drive", lib]], paths)
        end = jobs.wait("e1", st["id"], interval=0.02)
        self.assertEqual((end["state"], end["ok"], end["step"], end["total"]), ("done", False, 2, 2))
        self.assertEqual(end["label"], "fail")
        self.assertEqual(end["spec"], {"count": 2})
        self.assertNotIn("safe", end)
        ls, n = jobs.lines("e1")
        self.assertEqual(ls, ["$ intake.py ok [2 files]", jobs.REDACTED, "  [exit 0]", "$ intake.py fail --drive [path]",
                              jobs.REDACTED, "  [exit 3]",
                              "done: 2 of 2 gone from the library; something failed, see above"])
        self.assertEqual(self.names_left(["secret-one", "secret-two", lib, self.tmp]), [])
        # A page that followed from the start still has every line, the steps' output included.
        full, m = tail.lines(0, done=True)
        self.assertEqual(m, n)
        self.assertEqual(full[1], "  hi " + " ".join(paths))
        self.assertEqual(full[4], "  failing")
        # A page that starts following after the end gets only the scrubbed log.
        self.assertEqual(jobs.Tail("e1", st["id"]).lines(0, done=True), (ls, n))

    def test_crashed_erase_is_scrubbed(self):
        """A worker killed mid-erase cannot scrub its own output: the next status() (or sweep()) does."""
        secret = os.path.join(self.tmp, "Pictures", "secret-three.jpg")
        st, tail = self.start_erase("e2", [["block", self.fifo, secret], ["ok", secret]], [secret])
        until(lambda: self.log_has("e2", "started"), "the step to start")
        with open(os.path.join(self.state, "jobs", "e2.json")) as f:
            self.assertNotIn("secret-three", f.read())  # never written to the status, even mid-run
        self.assertTrue(self.log_has("e2", "secret-three"))  # the step's output streams while it runs
        pid = jobs.status("e2")["pid"]
        os.kill(pid, signal.SIGKILL)  # by PID, never pkill -f
        until(lambda: jobs.proc_start(pid) is None, "the worker to go")
        jobs.sweep()  # nothing watched it die: the start-up sweep of a page finds it
        cr = jobs.status("e2")
        self.assertEqual((cr["state"], cr["step"], cr["label"], cr["step_running"]), ("crashed", 1, "block", True))
        self.assertEqual(self.names_left(["secret-three", self.tmp]), [])
        self.assertEqual(jobs.lines("e2")[0], ["$ intake.py block [2 files]", jobs.REDACTED])
        self.open_gate()
        until(lambda: not jobs.status("e2")["step_running"], "the orphaned step to end")
        self.assertEqual(self.names_left(["secret-three", self.tmp]), [])
        tail.close()

    def test_status_probe_never_refuses_a_claim(self):
        """status() tests the job lock by taking it for an instant (a crashed job's orphaned step, a job not yet
        started). A claim at that instant waits for the probe instead of getting Busy (a spurious 409)."""
        if not os.path.exists("/proc/locks"):
            self.skipTest("needs /proc/locks to see the claim wait")
        os.makedirs(os.path.join(self.state, "jobs"))
        jobs._write_private(os.path.join(self.state, "jobs", "p1.json"), dict(id="x", state="crashed", ok=False))
        P = jobs._paths("p1")
        real, holding, go = jobs._try_lock, threading.Event(), threading.Event()
        prober = []

        def held_probe(fd):
            got = real(fd)
            if threading.current_thread() in prober:
                holding.set()
                go.wait(30)  # the probe holds the job lock right now
            return got

        res = []
        jobs._try_lock = held_probe
        try:
            t1 = threading.Thread(target=jobs.status, args=("p1",))
            prober.append(t1)
            t1.start()
            self.assertTrue(holding.wait(30))

            def claim():
                try:
                    res.append(jobs.claim("p1"))
                except jobs.Busy as e:
                    res.append(e)
            t2 = threading.Thread(target=claim)
            t2.start()
            mutex_ino = str(os.stat(P["mutex"]).st_ino)

            def claim_waits():  # a blocked flock on the mutex: the claim is queued behind the probe
                if res:
                    return True
                with open("/proc/locks") as f:
                    return any("->" in ln and ln.split()[6].split(":")[-1] == mutex_ino for ln in f)
            until(claim_waits, "the claim to wait for the probe")
            self.assertEqual(res, [])  # not refused while the probe held the lock
            go.set()
            t1.join(30)
            t2.join(30)
        finally:
            go.set()
            jobs._try_lock = real
        self.assertEqual(len(res), 1)
        self.assertIsInstance(res[0], jobs.Claim)
        res[0].release()

    def test_claim_is_atomic(self):
        """Many concurrent submits for one key: exactly one gets the lock (flock, no check-then-create)."""
        n = 12
        barrier = threading.Barrier(n)
        got, busy, lock = [], [], threading.Lock()

        def go():
            barrier.wait()
            try:
                c = jobs.claim("race")
            except jobs.Busy:
                with lock:
                    busy.append(1)
                return
            with lock:
                got.append(c)

        ts = [threading.Thread(target=go) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual((len(got), len(busy)), (1, n - 1))
        got[0].release()
        jobs.claim("race").release()

    def test_second_submit_refused_while_running(self):
        st = self.start("t2", [["block", self.fifo]])
        until(lambda: self.log_has("t2", "started"), "the step to start")
        cur = jobs.status("t2")
        self.assertEqual(cur["state"], "running")
        self.assertTrue(jobs.alive(cur["pid"], cur["pstart"]))
        with self.assertRaises(jobs.Busy):
            jobs.claim("t2")
        self.assertIsNone(jobs.status("t3"))  # other keys are independent
        jobs.claim("t3").release()
        self.open_gate()
        end = jobs.wait("t2", st["id"], interval=0.02)
        self.assertEqual((end["state"], end["ok"]), ("done", True))
        self.assertTrue(self.log_has("t2", "released"))
        jobs.claim("t2").release()

    def test_crashed_worker_is_reported_not_running(self):
        st = self.start("t4", [["block", self.fifo], ["ok", "never"]])
        until(lambda: self.log_has("t4", "started"), "the step to start")
        pid = jobs.status("t4")["pid"]
        os.kill(pid, signal.SIGKILL)  # by PID, never pkill -f
        cr = until(lambda: (lambda s: s if s["state"] == "crashed" else None)(jobs.status("t4")), "crashed")
        self.assertEqual((cr["ok"], cr["done"], cr["step"], cr["label"]), (False, True, 1, "block"))
        # the step it started still holds the lock, so a new submit is still refused
        self.assertTrue(cr["step_running"])
        with self.assertRaises(jobs.Busy):
            jobs.claim("t4")
        self.open_gate()
        until(lambda: not jobs.status("t4")["step_running"], "the orphaned step to end")
        self.assertEqual(jobs.status("t4")["state"], "crashed")  # stays crashed; never re-run by itself
        self.assertFalse(self.log_has("t4", "never"))
        jobs.claim("t4").release()

    def test_pid_checks(self):
        me = os.getpid()
        self.assertTrue(jobs.alive(me, jobs.proc_start(me)))
        self.assertFalse(jobs.alive(me, "1"))  # same PID, other start time: a reused PID is not our worker
        self.assertFalse(jobs.alive(0))
        self.assertFalse(jobs.alive(None))
        p = subprocess.Popen([PY, "-c", "pass"])
        p.wait()
        self.assertFalse(jobs.alive(p.pid))
        # a status file that says running with a reused PID is reported as crashed
        os.makedirs(os.path.join(self.state, "jobs"))
        jobs._write_private(os.path.join(self.state, "jobs", "t5.json"),
                            dict(id="x", state="running", pid=me, pstart="1", step=2, label="erase b"))
        st = jobs.status("t5")
        self.assertEqual((st["state"], st["ok"], st["done"]), ("crashed", False, True))
        self.assertEqual(jobs.status("t5")["state"], "crashed")  # written back, sticky

    def test_worker_outlives_the_process_that_started_it(self):
        """The server can die (or be Ctrl-C'd) right after starting the job; the job carries on."""
        self.gates += 1
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import jobs\n"
                "c = jobs.claim('t6'); st = c.spawn('commands', dict(argsets=[['block', sys.argv[2]], ['ok', 'b']]))\n"
                "print(st['id'])\n")
        r = subprocess.run([PY, "-I", "-c", code, support.PKG, self.fifo], capture_output=True, text=True,
                           timeout=60, env=os.environ)
        self.assertEqual(r.returncode, 0, r.stderr)
        jid = r.stdout.strip()
        until(lambda: self.log_has("t6", "started"), "the step to start")
        st = jobs.status("t6")
        self.assertEqual((st["id"], st["state"]), (jid, "running"))
        self.assertTrue(jobs.alive(st["pid"], st["pstart"]))
        self.open_gate()
        end = jobs.wait("t6", jid, interval=0.02)
        self.assertEqual((end["state"], end["ok"]), ("done", True))
        self.assertTrue(self.log_has("t6", "hi b"))

    def test_refusals(self):
        for bad in ("", "../x", "a/b", ".hidden", None):
            with self.assertRaises(ValueError):
                jobs.claim(bad)
        with jobs.claim("t7") as c, self.assertRaises(ValueError):
            c.spawn("os", {})  # only the known runners, never a module named in a file
        self.assertFalse(os.path.exists(os.path.join(self.state, "jobs", "t7.json")))
        r = subprocess.run([PY, "-I", os.path.join(support.PKG, "jobs.py"), "run", "t7"], capture_output=True,
                           text=True, timeout=60, env={k: v for k, v in os.environ.items() if k != jobs.LOCK_FD_ENV})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("without the job lock", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
