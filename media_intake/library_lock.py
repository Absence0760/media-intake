#!/usr/bin/env python3
"""One lock per library, so nothing moves or rewrites library files (or index.json) while
something else does (stdlib only: vlm_trash.py imports it inside the venv).

  <state>/locks/library-<id>.lock    dir 0700, file 0600; <id> is the first 16 hex digits of the
                                     sha256 of the library root's real path (config `library`)

Who takes it (docs/intake.md, "One writer at a time: the library lock"):

  - intake.py main(), for every subcommand in intake.LOCKED (read-only ones, intake.READ_ONLY, never);
  - a detached job (jobs.py): jobs.claim() takes it next to the job lock and hands it to the worker,
    which holds it until its last step is over; each intake.py step it starts inherits it;
  - the gallery's direct sidecar writes (Keep, my tags, Rotate) and vlm_trash.py's verdict writes.

The lock is an flock(LOCK_EX), so it dies with its holder: a crashed or killed process never leaves the
library locked (only its stale "who" note, which nobody trusts without the flock).

**Inherited, not re-taken.** A job worker exports the descriptor number in MEDIA_INTAKE_LIBRARY_LOCK_FD
and passes that descriptor to its steps. A step accepts it only if that descriptor is open on this
library's lock file (same device and inode) *and* flock(LOCK_EX | LOCK_NB) on it succeeds: flock locks
belong to an open file description, so that succeeds when the description is the parent's (already
locked) and fails when someone else holds the lock. An unrelated process can't use the variable to skip
the lock: a number it made up is not open, or is its own fresh open of the file, which the holder's lock
refuses (or, with no holder, takes the lock for real, held until that descriptor closes). An inherited lock is never unlocked by the
child (that would drop the parent's), only left to close with it.

**When busy:** say who holds it (what and its PID, and a job's key) and wait up to `wait` seconds, polling
the flock; then refuse (Busy). The CLI waits MEDIA_INTAKE_LOCK_WAIT seconds (default 10: enough for a
gallery Keep or tag edit; a job can run for an hour, so it is never waited on for long); the pages
never wait (an HTTP request gets 409 with the holder at once).
"""
import contextlib, fcntl, hashlib, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config  # noqa: E402

FD_ENV = "MEDIA_INTAKE_LIBRARY_LOCK_FD"
WAIT_ENV = "MEDIA_INTAKE_LOCK_WAIT"
CLI_WAIT = 10.0
MAX_WAIT = 24 * 3600.0
POLL = 0.1
EX_TEMPFAIL = 75  # sysexits.h: try again later


class Busy(Exception):
    """Someone else holds the library lock. .holder is its note (a dict, maybe empty)."""

    def __init__(self, holder, path):
        self.holder, self.path = holder or {}, path
        super().__init__(f"the library is busy: {describe(self.holder)} holds its lock ({path}); "
                         "try again when it has finished")


def lock_path(cfg=None):
    cfg = cfg or config.get()
    lib = os.path.realpath(cfg["library"])
    return os.path.join(cfg["state"], "locks", f"library-{hashlib.sha256(lib.encode()).hexdigest()[:16]}.lock")


def _open(path):
    d = os.path.dirname(path)
    for p in (os.path.dirname(d), d):  # the state dir and locks/, both private
        os.makedirs(p, mode=0o700, exist_ok=True)
        os.chmod(p, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _try(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _proc_start(pid):
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            s = f.read()
    except (OSError, ValueError):
        return None
    fields = s[s.rfind(")") + 2:].split()
    return fields[19] if len(fields) >= 20 and fields[0] not in ("Z", "X") else None


def read_holder(path=None):
    """The holder's note, or {} (none written yet, or unreadable). Only meaningful while the flock is held."""
    try:
        with open(path or lock_path()) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def describe(h):
    if not h.get("what"):
        return "another media-intake process"
    s = str(h["what"])
    pid = h.get("pid")
    if isinstance(pid, int):
        gone = os.path.isdir("/proc/self") and _proc_start(pid) != h.get("pstart")
        s += f" (PID {pid}" + (", now gone: a step it started is still running" if gone else "") + ")"
    return s


def note(fd, what, pid=None):
    """Write who holds the lock into the lock file (no file names: a command, a job key, a PID)."""
    pid = os.getpid() if pid is None else pid
    data = json.dumps(dict(what=what, pid=pid, pstart=_proc_start(pid))).encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, data, 0)


def inherited(path=None):
    """The library lock's descriptor if this process got it, held, from its parent (see the docstring), else None."""
    v = os.environ.get(FD_ENV, "")
    if not v.isdigit():
        return None
    fd = int(v)
    try:
        st = os.fstat(fd)
        want = os.stat(path or lock_path())
    except OSError:
        return None
    if (st.st_dev, st.st_ino) != (want.st_dev, want.st_ino):
        return None
    return fd if _try(fd) else None


def acquire(what, wait=0.0, out=None, path=None):
    """Take the lock (fresh descriptor, close-on-exec) and note the holder. Returns the fd; raises Busy after
    wait seconds. out: where to say that it waits (a file, e.g. sys.stderr), or None for silence."""
    path = path or lock_path()
    fd = _open(path)
    try:
        if not _try(fd):
            end = time.monotonic() + max(0.0, min(float(wait), MAX_WAIT))
            if wait > 0 and out is not None:
                print(f"media-intake: waiting up to {wait:g} s: {describe(read_holder(path))} holds the "
                      "library lock", file=out, flush=True)
            while True:
                if time.monotonic() >= end:
                    raise Busy(read_holder(path), path)
                time.sleep(POLL)
                if _try(fd):
                    break
        note(fd, what)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def hold(what, wait=0.0, out=None):
    """The library lock for the block: the inherited one if the parent passed it (left alone at the end), else
    a fresh one, released when the block ends (or the process dies)."""
    path = lock_path()
    if inherited(path) is not None:
        yield
        return
    fd = acquire(what, wait, out, path)
    try:
        yield
    finally:
        os.close(fd)


def cli_wait():
    """MEDIA_INTAKE_LOCK_WAIT seconds (0 = refuse at once), default CLI_WAIT, at most MAX_WAIT."""
    v = os.environ.get(WAIT_ENV, "")
    try:
        w = float(v) if v.strip() else CLI_WAIT
    except ValueError:
        sys.exit(f"{WAIT_ENV}={v!r}: a number of seconds")
    if not 0 <= w <= MAX_WAIT:
        sys.exit(f"{WAIT_ENV}={v!r}: between 0 and {MAX_WAIT:g} seconds")
    return w
