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
                time, step / total / label, ok, spec (what to run)
  <key>.log     the worker's output, which the pages stream

A job whose status says running but whose worker PID is gone (or was reused:
the /proc start time differs, or it is a zombie) is reported as "crashed",
never as running forever. A crashed job is not re-run by itself: the pages say
so, and you start the review again from what is left on disk.

  python3 -I media_intake/jobs.py run KEY     (the worker; started by start())
"""
import datetime, fcntl, json, os, re, secrets, subprocess, sys, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,159}$")
LOCK_FD_ENV = "MEDIA_INTAKE_JOB_LOCK_FD"
LIVE = ("starting", "running")
RUNNERS = ("commands", "review_ui")  # what a status file may name; never an arbitrary module


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


def _update(key, only_id=None, when=None, **kv):
    """Read-modify-write the status under its own flock (readers see whole files: os.replace).
    Only for job only_id, and only when when(status) holds, when those are given."""
    P = _paths(key)
    fd = os.open(P["mutex"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = _read(P["status"]) or {}
        if (only_id is not None and st.get("id") != only_id) or (when is not None and not when(st)):
            return st
        st.update(kv)
        _write_private(P["status"], st)
        return st
    finally:
        os.close(fd)


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


def _held(P):
    """Someone holds the job lock (a worker, or an intake.py step a crashed worker left)."""
    fd = os.open(P["lock"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # closing drops the probe's lock, if it got one
    return False


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


def wait(key, job_id, interval=0.25):
    """Block until job_id is no longer starting/running (or another job replaced it). Returns its status."""
    while True:
        st = status(key)
        if not st or st.get("id") != job_id or st["done"]:
            return st
        time.sleep(interval)


# ---------------------------------------------------------------- starting
class Claim:
    """The job lock, taken atomically. spawn() hands it to a detached worker; release() gives it up."""

    def __init__(self, key):
        self.key, self.P = key, _paths(key, create=True)
        self.fd = os.open(self.P["lock"], os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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

    def spawn(self, runner, spec, total=None):
        """Start the worker in its own session; it inherits the lock. Returns the status."""
        if runner not in RUNNERS:
            raise ValueError(f"unknown runner {runner!r}")
        jid = secrets.token_hex(8)
        fd = os.open(self.P["log"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        _update(self.key, **dict(id=jid, runner=runner, state="starting", pid=None, pstart=None,
                                 started=_now(), ended=None, ok=None, step=0, total=total, label="", spec=spec))
        env = dict(os.environ, **{LOCK_FD_ENV: str(self.fd)})
        try:
            p = subprocess.Popen([sys.executable, "-I", "-u", os.path.abspath(__file__), "run", self.key],
                                 stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, cwd=HERE, env=env,
                                 start_new_session=True, pass_fds=(self.fd,))
        except OSError as e:
            _update(self.key, only_id=jid, state="crashed", ok=False, ended=_now(), label=f"could not start: {e}")
            raise
        finally:
            os.close(fd)
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

    def __init__(self, key, st, lock_fd):
        self.key, self.id, self.spec, self.lock_fd = key, st["id"], st.get("spec") or {}, lock_fd
        self.step = 0

    def log(self, s):
        print(s, flush=True)

    def mark(self, label):
        self.step += 1
        _update(self.key, only_id=self.id, step=self.step, label=label)

    def run(self, args, label=None, shown=None):
        """intake.py ARGS as a child holding the job lock too; its output goes to the log. Returns rc."""
        self.mark(label or (args[0] if args else ""))
        self.log("$ intake.py " + (shown if shown is not None else " ".join(args)))
        p = subprocess.Popen([sys.executable, "-I", "-u", intake_path(), *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=HERE,
                             pass_fds=(self.lock_fd,) if self.lock_fd is not None else ())
        for line in p.stdout:
            self.log("  " + line.rstrip("\n"))
        rc = p.wait()
        self.log(f"  [{label}: exit {rc}]" if label else f"  [exit {rc}]")
        return rc


def run_commands(job):
    """Runner "commands": spec {argsets: [[...]], shown: [str], paths: [...]} (the gallery's trash/erase)."""
    spec = job.spec
    ok = True
    shown = spec.get("shown") or []
    for n, args in enumerate(spec.get("argsets") or []):
        ok &= job.run(list(args), shown=shown[n] if n < len(shown) else None) == 0
    paths = spec.get("paths") or []
    gone = sum(not os.path.lexists(p) for p in paths)
    job.log(f"done: {gone} of {len(paths)} gone from the library" + ("" if ok else "; something failed, see above"))
    return ok


def worker(key):
    lock_fd = os.environ.get(LOCK_FD_ENV)
    lock_fd = int(lock_fd) if lock_fd and lock_fd.isdigit() else None
    if lock_fd is None:
        sys.exit("jobs.py run: started without the job lock; use jobs.claim(key).spawn()")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # already ours (inherited); raises if not
    st = _read(_paths(key)["status"]) or {}
    if st.get("state") != "starting":
        sys.exit(f"jobs.py run: {key} is not waiting to start")
    st = _update(key, only_id=st["id"], state="running", pid=os.getpid(), pstart=proc_start(os.getpid()))
    job = Job(key, st, lock_fd)
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
