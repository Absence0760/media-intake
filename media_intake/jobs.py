#!/usr/bin/env python3
"""Detached, re-attachable jobs for the review page and the gallery (stdlib only).

A long action (release / erase / trash) started from a page runs in its own
process, in its own session, so closing the page or stopping the server never
stops an erase half-way, and any page or a restarted server can show it again.

Each job has a key (`review-<batch>`, `maybe-trash`, `gallery`) and three files
in <state>/jobs/ (dir 0700, files 0600):

  <key>.lock    held with flock(LOCK_EX) by the worker until its last step is
                over, and by every intake.py step it starts, so a second submit
                for the same key is refused atomically (no check-then-create
                race), even while a step outlives a crashed worker
  <key>.json    status: id, state (starting / running / done), pid and its start
                time, step / total / label, ok, spec (what a page needs to know)
  <key>.log     the worker's output, which the pages stream
  <key>.mutex   a short flock around every read-modify-write of the status, the
                log swap at spawn and at the end, and every try of the job lock

What the worker needs but no page does (file paths, decisions) goes to it on a
pipe (spawn(private=...)) and is never written. A job spawned with redact=True
(an erase) keeps file names out of the status file, renders its command lines
without paths, and when it ends (done or crashed) has every log line it did not
write itself (the steps' output, which can name files) replaced in place, so no
name of an erased file stays in <state>/jobs/. A page that was following the log
keeps reading the full output through its open descriptor (Tail).

A job whose status says running but whose worker PID is gone (or was reused:
the /proc start time differs, or it is a zombie) is reported as "crashed",
never as running forever. A crashed job is not re-run by itself: the pages say
so, and you start the review again from what is left on disk.

  python3 -I media_intake/jobs.py run KEY     (the worker; started by start())
"""
import contextlib, datetime, fcntl, json, os, re, secrets, subprocess, sys, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,159}$")
LOCK_FD_ENV = "MEDIA_INTAKE_JOB_LOCK_FD"
LIVE = ("starting", "running")
RUNNERS = ("commands", "review_ui")  # what a status file may name; never an arbitrary module
REDACTED = "  [output removed when the run ended: it may name a file]"


class Busy(Exception):
    """A job with this key is still running (or a step it started still is)."""


def intake_path():
    # Tests point this at a fake that records its argv, so a real erase never runs there.
    return os.environ.get("MEDIA_REVIEW_INTAKE", os.path.join(HERE, "intake.py"))


def state_dir():
    return config.get()["state"]


def jobs_dir():
    st = state_dir()
    for d in (st, os.path.join(st, "jobs")):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    return os.path.join(st, "jobs")


def _paths(key, create=False):
    """The job's files. Only taking the lock creates <state>/jobs/; reading never writes."""
    if not KEY_RE.match(key or ""):
        raise ValueError(f"bad job key: {key!r}")
    base = os.path.join(jobs_dir() if create else os.path.join(state_dir(), "jobs"), key)
    return dict(lock=base + ".lock", status=base + ".json", log=base + ".log", mutex=base + ".mutex")


def _now():
    return f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"


def _write_private(p, obj):
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(p):
    try:
        with open(p) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (FileNotFoundError, ValueError):
        return None


@contextlib.contextmanager
def _mutex(P):
    """The key's short mutex (never held across a step; never taken twice in one call chain)."""
    fd = os.open(P["mutex"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _update_held(P, only_id=None, when=None, **kv):
    st = _read(P["status"]) or {}
    if (only_id is not None and st.get("id") != only_id) or (when is not None and not when(st)):
        return st
    st.update(kv)
    _write_private(P["status"], st)
    return st


def _update(key, only_id=None, when=None, **kv):
    """Read-modify-write the status under the mutex (readers see whole files: os.replace).
    Only for job only_id, and only when when(status) holds, when those are given."""
    P = _paths(key)
    with _mutex(P):
        return _update_held(P, only_id, when, **kv)


def _new_log(P):
    """A fresh log file (a new inode, so a Tail still open on the last job's log never sees this one's)."""
    tmp = f"{P['log']}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    os.replace(tmp, P["log"])
    return fd


def _scrub(key, job_id):
    """An ended redact job: every log line it did not mark safe is replaced in place (same line count, so a
    page's position still fits), and the safe-line list leaves the status. Idempotent; another job's files
    (a newer id) are never touched."""
    P = _paths(key)
    with _mutex(P):
        st = _read(P["status"]) or {}
        if st.get("id") != job_id or not st.get("redact"):
            return
        safe = set(st.get("safe") or [])
        try:
            with open(P["log"], errors="replace") as f:
                ls = f.read().splitlines()
        except FileNotFoundError:
            ls = []
        tmp = f"{P['log']}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines((ln if ln in safe else REDACTED) + "\n" for ln in ls)
        os.chmod(tmp, 0o600)
        os.replace(tmp, P["log"])
        st.pop("safe", None)
        st["scrubbed"] = True
        _write_private(P["status"], st)


def shown_args(args):
    """A command line without names: each run of path arguments becomes a count, an option's path value
    "[path]". Batch names and options stay."""
    out, run = [], 0
    for n, a in enumerate(args):
        if os.sep in a:
            if n and args[n - 1].startswith("--") and not run:
                out.append("[path]")
            else:
                run += 1
            continue
        if run:
            out.append(f"[{run} file{'s' if run > 1 else ''}]")
            run = 0
        out.append(a)
    if run:
        out.append(f"[{run} file{'s' if run > 1 else ''}]")
    return " ".join(out)


# ---------------------------------------------------------------- PID checks (by PID, never pgrep -f)
def proc_start(pid):
    """The process's start time from /proc/<pid>/stat (field 22), or None if it is gone or a zombie."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            s = f.read()
    except (OSError, ValueError):
        return None
    fields = s[s.rfind(")") + 2:].split()
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    return fields[19]


def alive(pid, start=None):
    """pid is running and, when start is given, is the same process (not a reused PID)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.path.isdir("/proc/self"):
        got = proc_start(pid)
        return got is not None and (start is None or got == start)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _try_lock(fd):
    """flock(LOCK_EX | LOCK_NB): True if taken. Only ever called with the key's mutex held."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _held(P):
    """Someone holds the job lock (a worker, or an intake.py step a crashed worker left).
    Testing a flock means taking it for an instant; that happens under the mutex, as does every claim, so a
    submit arriving at that instant waits for the probe instead of being refused (no spurious Busy)."""
    with _mutex(P):
        fd = os.open(P["lock"], os.O_RDWR | os.O_CREAT, 0o600)
        try:
            return not _try_lock(fd)
        finally:
            os.close(fd)  # closing drops the probe's lock, if it got one


# ---------------------------------------------------------------- reading
def status(key):
    """The job's status dict, or None. state is starting / running / done / crashed."""
    P = _paths(key)
    st = _read(P["status"])
    if not st:
        return None
    if st.get("state") in LIVE:
        pid = st.get("pid")
        dead = (not alive(pid, st.get("pstart"))) if pid else not _held(P)
        if dead:  # flip it once, unless the worker wrote its own end meanwhile
            st = _update(key, only_id=st.get("id"), when=lambda s: s.get("state") in LIVE,
                         state="crashed", ok=False, ended=_now())
    if st.get("state") not in LIVE and st.get("redact") and not st.get("scrubbed"):
        _scrub(key, st.get("id"))  # a crashed erase: its dead worker never scrubbed its own output
        st = _read(P["status"]) or st
    if st.get("state") == "crashed":
        st["step_running"] = _held(P)  # a step the dead worker started may still be going
    st["done"] = st.get("state") not in LIVE
    return st


def lines(key, since=0):
    """(new log lines from index since, next index)."""
    try:
        with open(_paths(key)["log"], errors="replace") as f:
            ls = f.read().splitlines()
    except FileNotFoundError:
        ls = []
    since = max(0, min(since, len(ls)))
    return ls[since:], len(ls)


def public(st):
    """What a page may see: no spec, no paths."""
    if not st:
        return None
    return {k: st.get(k) for k in ("id", "state", "done", "ok", "step", "total", "label", "started", "ended",
                                   "step_running")}


def sweep():
    """status() every job in <state>/jobs/: a worker that died while nothing watched is marked crashed and,
    for an erase, its output scrubbed. The pages call this at start-up."""
    d = os.path.join(state_dir(), "jobs")
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return
    for n in names:
        if n.endswith(".json") and KEY_RE.match(n[:-5]):
            status(n[:-5])


class Tail:
    """One job's log, read through a descriptor opened while the status named that job. The end-of-run scrub
    swaps the file (os.replace), so a page that was following keeps every line, the last ones included;
    a page that starts following after the end gets the scrubbed file. Closed once the end is read."""

    def __init__(self, key, job_id, _fd=None):
        self.id, self.fd, self.data, self.ls, self.mu = job_id, _fd, b"", [], threading.Lock()
        if _fd is not None:  # spawn's own, opened with the new log: nothing the worker writes is missed
            return
        P = _paths(key)
        try:
            with _mutex(P):  # spawn swaps the log and writes the status under it: they belong together here
                if (_read(P["status"]) or {}).get("id") == job_id:
                    self.fd = os.open(P["log"], os.O_RDONLY)
        except FileNotFoundError:
            self.fd = None

    def lines(self, since=0, done=False):
        """(lines from index since, next index). done: the status, read before this call, says the job ended,
        so every line is written."""
        with self.mu:
            return self._lines(since, done)

    def _lines(self, since, done):
        if self.fd is not None:
            while True:
                b = os.read(self.fd, 1 << 16)
                if not b:
                    break
                self.data += b
            text = self.data.decode(errors="replace")
            ls = text.splitlines()
            if not done and ls and not text.endswith(("\n", "\r")):
                ls = ls[:-1]  # a line still being written
            self.ls = ls
            if done:
                os.close(self.fd)
                self.fd, self.data = None, b""
        since = max(0, min(since, len(self.ls)))
        return self.ls[since:], len(self.ls)

    def close(self):
        with self.mu:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


def wait(key, job_id, interval=0.25):
    """Block until job_id is no longer starting/running (or another job replaced it). Returns its status."""
    while True:
        st = status(key)
        if not st or st.get("id") != job_id or st["done"]:
            return st
        time.sleep(interval)


# ---------------------------------------------------------------- starting
class Claim:
    """The job lock, taken atomically. spawn() hands it to a detached worker; release() gives it up.
    After spawn(), .tail follows the new job's log from its first byte."""

    def __init__(self, key):
        self.key, self.P, self.tail = key, _paths(key, create=True), None
        self.fd = os.open(self.P["lock"], os.O_RDWR | os.O_CREAT, 0o600)
        with _mutex(self.P):  # never at the instant a status() probe holds the lock (see _held)
            got = _try_lock(self.fd)
        if not got:
            os.close(self.fd)
            self.fd = None
            raise Busy(f"a job for {key} is still running")

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def spawn(self, runner, spec, total=None, private=None, redact=False):
        """Start the worker in its own session; it inherits the lock. Returns the status.
        spec is written to the status file (with redact, it must name no file); private reaches the worker
        on a pipe and is never written. redact: an erase, see the module docstring."""
        if runner not in RUNNERS:
            raise ValueError(f"unknown runner {runner!r}")
        jid = secrets.token_hex(8)
        with _mutex(self.P):  # the new log and the new status together (Tail pairs them the same way)
            fd = _new_log(self.P)
            self.tail = Tail(self.key, jid, os.open(self.P["log"], os.O_RDONLY))
            _update_held(self.P, **dict(id=jid, runner=runner, state="starting", pid=None, pstart=None,
                                        started=_now(), ended=None, ok=None, step=0, total=total, label="",
                                        spec=spec, redact=bool(redact), safe=[], scrubbed=False))
        env = dict(os.environ, **{LOCK_FD_ENV: str(self.fd)})
        try:
            p = subprocess.Popen([sys.executable, "-I", "-u", os.path.abspath(__file__), "run", self.key],
                                 stdin=subprocess.PIPE, stdout=fd, stderr=fd, cwd=HERE, env=env,
                                 start_new_session=True, pass_fds=(self.fd,))
        except OSError as e:
            _update(self.key, only_id=jid, state="crashed", ok=False, ended=_now(), label=f"could not start: {e}")
            self.tail.close()
            raise
        finally:
            os.close(fd)
        try:
            p.stdin.write(json.dumps(private or {}).encode())
        except BrokenPipeError:  # the worker died before reading it: status() reports it as crashed
            pass
        finally:
            with contextlib.suppress(BrokenPipeError):
                p.stdin.close()
        _update(self.key, only_id=jid, when=lambda s: not s.get("pid"), pid=p.pid, pstart=proc_start(p.pid))
        # Reap it when it ends, so a finished worker is never a zombie that looks alive.
        threading.Thread(target=p.wait, daemon=True).start()
        self.release()  # the worker's inherited copy keeps the lock held
        return status(self.key)


def claim(key):
    return Claim(key)


# ---------------------------------------------------------------- the worker
class Job:
    """Handed to a runner inside the worker: log lines and run intake.py steps."""

    def __init__(self, key, st, lock_fd, private=None):
        self.key, self.id, self.lock_fd = key, st["id"], lock_fd
        self.spec = dict(st.get("spec") or {}, **(private or {}))
        self.redact = bool(st.get("redact"))
        self.safe = []
        self.step = 0

    def log(self, s, safe=False):
        """One line (or several) to the log. safe: written by the job itself from counts, step labels and
        batch names, never a file name, so a redact job keeps it after the end."""
        print(s, flush=True)
        if safe and self.redact:
            new = [ln for ln in str(s).splitlines() if ln not in self.safe]
            if new:
                self.safe += new
                _update(self.key, only_id=self.id, safe=list(self.safe))

    def mark(self, label):
        self.step += 1
        _update(self.key, only_id=self.id, step=self.step, label=label)

    def run(self, args, label=None, shown=None):
        """intake.py ARGS as a child holding the job lock too; its output goes to the log. Returns rc."""
        self.mark(label or (args[0] if args else ""))
        # A redact job never logs a caller's rendering: that could carry a name.
        line = shown_args(args) if self.redact else shown if shown is not None else " ".join(args)
        self.log("$ intake.py " + line, safe=True)
        p = subprocess.Popen([sys.executable, "-I", "-u", intake_path(), *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=HERE,
                             pass_fds=(self.lock_fd,) if self.lock_fd is not None else ())
        for line in p.stdout:
            self.log("  " + line.rstrip("\n"))
        rc = p.wait()
        self.log(f"  [{label}: exit {rc}]" if label else f"  [exit {rc}]", safe=True)
        return rc


def run_commands(job):
    """Runner "commands": spec {argsets: [[...]], shown: [str], paths: [...]} (the gallery's trash/erase;
    the gallery passes all three as private, so none is written)."""
    spec = job.spec
    ok = True
    shown = spec.get("shown") or []
    for n, args in enumerate(spec.get("argsets") or []):
        ok &= job.run(list(args), shown=shown[n] if n < len(shown) else None) == 0
    paths = spec.get("paths") or []
    gone = sum(not os.path.lexists(p) for p in paths)
    job.log(f"done: {gone} of {len(paths)} gone from the library" + ("" if ok else "; something failed, see above"),
            safe=True)
    return ok


def worker(key):
    lock_fd = os.environ.get(LOCK_FD_ENV)
    lock_fd = int(lock_fd) if lock_fd and lock_fd.isdigit() else None
    if lock_fd is None:
        sys.exit("jobs.py run: started without the job lock; use jobs.claim(key).spawn()")
    with _mutex(_paths(key)):  # a status() probe may hold the lock for an instant; never collide with it
        if not _try_lock(lock_fd):  # already ours (inherited); fails if not
            sys.exit("jobs.py run: the job lock is not ours")
    try:
        private = json.loads(sys.stdin.read() or "null")
    except ValueError:
        private = None
    if not isinstance(private, dict):  # spawn always sends an object; nothing means the starter died first
        sys.exit("jobs.py run: no spec on stdin; use jobs.claim(key).spawn()")
    st = _read(_paths(key)["status"]) or {}
    if st.get("state") != "starting":
        sys.exit(f"jobs.py run: {key} is not waiting to start")
    st = _update(key, only_id=st["id"], state="running", pid=os.getpid(), pstart=proc_start(os.getpid()))
    job = Job(key, st, lock_fd, private)
    ok = False
    try:
        if st.get("runner") == "commands":
            ok = run_commands(job)
        elif st.get("runner") == "review_ui":
            import review_ui
            ok = review_ui.run_job(job)
        else:
            job.log(f"error: unknown runner {st.get('runner')!r}")
    except Exception as e:  # report it in the log and the status, never leave "running" behind
        job.log(f"error: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stdout)
        ok = False
    if job.redact:  # still holding the lock, so no new job can take the key while its output is scrubbed
        _scrub(key, job.id)
    # Let go of the lock first, so a page that sees "done" can start the next job at once. If a new job
    # takes the key in between, only_id keeps this write from touching its status.
    os.close(lock_fd)
    _update(key, only_id=job.id, state="done", ok=bool(ok), ended=_now())
    return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "run":
        return worker(argv[1])
    sys.exit("usage: jobs.py run KEY   (started by the review page or the gallery)")


if __name__ == "__main__":
    sys.exit(main())
