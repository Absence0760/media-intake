#!/usr/bin/env python3
"""Process queue for an external (non-backup) drive of photos and videos.

  drive_queue.py add PATH --label NAME     register a folder on a drive (counts, size, free-space check)
  drive_queue.py run [--chunk 2000] [--inhibit]   process every source in chunks (resumable)
  drive_queue.py status                    progress, and what waits for you
  drive_queue.py offload NAME [--yes]      delete the drive originals of finished, reviewed chunks

Run as `media-intake queue <add|run|status|offload>`.

Named drive_queue.py, not queue.py: media_intake/ goes on sys.path in intake.py,
semantic.py and vlm_trash.py, and a queue.py there would shadow the standard
library's `queue` (concurrent.futures and multiprocessing import it).

Each chunk is an ordinary intake batch `<added date>-<label>-<n>` and goes
through stage -> plan -> scan -> apply -> tag -> semantic index -> VLM trash
opinion (on that batch's Review|maybe-trash files; skipped, with a note, when
Ollama or the pinned model is not available). Steps run one at a time; plan,
scan, tag, index and vlm run behind the config's [queue] wrapper (a command
prefix such as a memory-capping or locking wrapper; default none). Never start
the queue itself under a locking wrapper (its inner calls would wait forever).

State: <state>/queue.json (0600) plus <state>/queue/<label>/ (the frozen file
list, chunk lists, step logs). A step is marked done only after it exits 0, so a
killed run continues at the step it was in; a finished step is never redone.
The run STOPS before every decision that is yours: quarantine review, trash
suggestions and offload (status prints the commands).

Refused: a root on the laptop's own disk, a drive that looks like the backup
drive (<mount>/.media-intake-backup, or a [drive] backup_markers folder at its
mount root, unless <mount>/.media-intake-source-ok exists), a drive without a filesystem UUID, a source
that would leave the laptop under 50 GB free once copied, and offload when the
drive's UUID differs, the drive looks like the backup or a batch's quarantine
is not empty. Output is counts only, never file content.

Test: python3 -I -m unittest discover -s tests -p test_drive_queue.py
"""
import argparse, collections, datetime, fcntl, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)  # after the stdlib: never shadow a stdlib module
import config  # noqa: E402
import sources  # noqa: E402

CFG = config.get()
HOME = os.path.expanduser("~")
STATE, LIBRARY, VIDEOS = CFG["state"], CFG["library"], CFG["videos"]
QFILE = os.path.join(STATE, "queue.json")
QDIR = os.path.join(STATE, "queue")
VENV_PY = os.path.join(STATE, "venv", "bin", "python")
PY = sys.executable
INTAKE = os.path.join(HERE, "intake.py")
SEMANTIC = os.path.join(HERE, "semantic.py")
VLM = os.path.join(HERE, "vlm_trash.py")
REVIEW_UI = os.path.join(HERE, "review_ui.py")
# Command prefix for the heavy steps ([queue] wrapper); MEDIA_QUEUE_WRAPPER (space-separated) overrides.
WRAPPER = os.environ["MEDIA_QUEUE_WRAPPER"].split() if "MEDIA_QUEUE_WRAPPER" in os.environ else CFG["queue_wrapper"]
WRAPPER_LOCK = CFG["queue_wrapper_lock"]  # the wrapper's flock file, if it has one: run warns when busy
MIN_FREE = 50 * 10**9
STEPS = ("stage", "plan", "scan", "apply", "tag", "index", "vlm")
HEAVY_STEPS = {"plan", "scan", "tag", "index", "vlm"}
LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


# ---------------------------------------------------------------- state
def private_dir(p):
    os.makedirs(p, mode=0o700, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(p, obj):
    private_dir(os.path.dirname(p))
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def load_q():
    return load(QFILE, None) or {"sources": {}}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def gb(n):
    return f"{n / 1e9:.1f} GB"


def free_bytes():
    """Free space where the copies land (state and library); the smallest if they differ."""
    out = []
    for p in (STATE, LIBRARY, VIDEOS):
        while not os.path.exists(p):
            p = os.path.dirname(p)
        out.append(shutil.disk_usage(p).free)
    return min(out)


def bdir(batch):
    return os.path.join(STATE, "batches", batch)


def sdir(label):
    return private_dir(os.path.join(private_dir(QDIR), label))


def batch_name(src, n):
    return f"{src['added'][:10]}-{src['label']}-{n}"


def chunks_of(src):
    files = load(os.path.join(QDIR, src["label"], "files.json"), [])
    size = src.get("chunk") or len(files) or 1
    return [files[i:i + size] for i in range(0, len(files), size)]


# ---------------------------------------------------------------- add
def cmd_add(a):
    if not LABEL.match(a.label):
        sys.exit("refused: --label is lowercase letters, digits and dashes (e.g. old-drive)")
    root = os.path.abspath(a.path)
    try:
        mount = sources.check_drive_root(root)
    except sources.Denied as e:
        sys.exit(f"refused: {e}")
    uuid = sources.drive_uuid(root)
    if not uuid:
        sys.exit(f"refused: {mount} has no filesystem UUID, so offload could not check it is the same drive")
    q = load_q()
    if a.label in q["sources"]:
        sys.exit(f"refused: {a.label} is already queued ({q['sources'][a.label]['root']})")
    for other in q["sources"].values():
        r1, r2 = root + os.sep, other["root"] + os.sep
        if other["uuid"] == uuid and (r1.startswith(r2) or r2.startswith(r1)):
            sys.exit(f"refused: overlaps queued source {other['label']} ({other['root']})")
    files = []
    for path, rel in sources.walk(root, "drive"):
        files.append([rel, os.path.getsize(path)])
    total = sum(s for _, s in files)
    free = free_bytes()
    if free - total < MIN_FREE:
        sys.exit(f"refused: {len(files)} files, {gb(total)}; the laptop has {gb(free)} free and must keep "
                 f"{gb(MIN_FREE)}. Queue it in parts: one subfolder at a time "
                 f"(media-intake queue add \"{root}/<subfolder>\" --label {a.label}-1), running and offloading "
                 f"each before the next, or free space first.")
    if not files:
        sys.exit(f"refused: no photos or videos under {root}")
    save(os.path.join(sdir(a.label), "files.json"), files)
    q["sources"][a.label] = dict(label=a.label, root=root, mount=mount, uuid=uuid, added=now(),
                                 files=len(files), bytes=total, chunk=None, chunks={}, offloaded=[])
    save(QFILE, q)
    by = collections.Counter(sources.drive_category(r).split(":")[0] for r, _ in files)
    print(f"queued {a.label}: {len(files)} files, {gb(total)} ({', '.join(f'{k} {v}' for k, v in sorted(by.items()))}); "
          f"{gb(free - total)} free after copying")
    print("next: media-intake queue run --inhibit")


# ---------------------------------------------------------------- run
def run_cmd(cmd, log):
    """Run one step; its output goes to the step log, never the terminal. Returns the exit code."""
    with open(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(f"== {now()} {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}\n")
        f.flush()
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode


def maybe_trash(batch):
    """The batch's filed files whose sidecar says Review|maybe-trash (scan-flagged files are never filed)."""
    plan = load(os.path.join(bdir(batch), "plan.json"), []) or []
    out = []
    for p in plan:
        x = (p.get("dst") or "") + ".xmp"
        if p.get("action") == "file" and os.path.isfile(p["dst"]) and os.path.isfile(x):
            with open(x, errors="replace") as f:
                if "Review|maybe-trash" in f.read():
                    out.append(p["dst"])
    return out


def step_cmd(step, src, batch, listfile):
    if step == "stage":
        return [PY, "-I", INTAKE, "stage", src["root"], "--kind", "drive", "--batch", batch, "--only", listfile]
    if step in ("plan", "scan", "apply", "tag"):
        return [PY, "-I", INTAKE, step, batch]
    if step == "index":
        return [VENV_PY, "-I", SEMANTIC, "index"]
    raise ValueError(step)


def do_step(step, src, ch, batch, listfile, log):
    """Run one step. Returns (ok, note)."""
    b = bdir(batch)
    if step == "apply" and os.path.exists(os.path.join(b, "plan.json")) \
            and os.path.exists(os.path.join(b, "scan.json")) and not os.path.isdir(os.path.join(b, "raw")):
        return True, "already applied"  # killed after apply finished, before it was marked
    if step == "vlm":
        files = maybe_trash(batch)
        if not files:
            return True, "no maybe-trash candidates"
        if not os.path.exists(VENV_PY):
            return True, f"skipped: no venv; {len(files)} candidates wait for vlm_trash.py"
        rc = run_cmd([*WRAPPER, VENV_PY, "-I", VLM, *files], log)
        if rc:
            return True, f"skipped: VLM unavailable (Ollama or the pinned model, exit {rc}); {len(files)} candidates"
        return True, f"{len(files)} judged"
    cmd = step_cmd(step, src, batch, listfile)
    rc = run_cmd((WRAPPER if step in HEAVY_STEPS else []) + cmd, log)
    return rc == 0, f"exit {rc}"


def summary(batch):
    """Counts only: what the chunk did and what waits for you."""
    plan = load(os.path.join(bdir(batch), "plan.json"), []) or []
    flags = load(os.path.join(bdir(batch), "scan.json"), {}) or {}
    c = collections.Counter(p["action"] for p in plan)
    q = load(os.path.join(STATE, "quarantine", batch, "quarantine.json"), {}) or {}
    rv = os.path.join(bdir(batch), "review")
    return dict(files=len(plan), filed=sum(1 for p in plan if p["action"] == "file" and p["rel"] not in flags),
                exact_duplicates=c["delete"], review=sum(len(fs) for _, _, fs in os.walk(rv)),
                quarantined=len(q), maybe_trash=len(maybe_trash(batch)))


def pending(batch):
    """What in this batch waits for you: (its own quarantine, every other quarantine its review page shows,
    review folder files). The second is review_ui.sub_batches, the one definition of what one review covers:
    each near- level of <batch> and of xcheck-<batch>, and xcheck-<batch> itself."""
    import review_ui  # the review page's module; imported here, as jobs.py does, only when it is needed
    qd = os.path.join(STATE, "quarantine")
    count = lambda n: len(load(os.path.join(qd, n, "quarantine.json"), {}) or {})  # noqa: E731
    q = count(batch)
    near = sum(count(n) for n in review_ui.sub_batches(batch) if n != batch)
    rv = sum(len(fs) for _, _, fs in os.walk(os.path.join(bdir(batch), "review")))
    return q, near, rv


def wrapper_lock_busy():
    if not WRAPPER_LOCK:
        return False
    try:
        with open(WRAPPER_LOCK, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
    except BlockingIOError:
        return True
    except OSError:
        pass
    return False


def cmd_run(a, argv=None):
    if a.inhibit:
        if not shutil.which("systemd-inhibit"):
            sys.exit("systemd-inhibit not found")
        args = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--inhibit"]
        os.execvp("systemd-inhibit", ["systemd-inhibit", "--what=sleep:idle", "--who=media-intake",
                                      "--why=media-intake drive queue", PY, "-I", os.path.abspath(__file__), *args])
    lockf = open(os.path.join(private_dir(QDIR), "run.lock"), "a")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lockf.close()
        sys.exit("another queue run is going")
    try:
        run_queue(a)
    finally:
        lockf.close()


def run_queue(a):
    if wrapper_lock_busy():
        print("another job holds the wrapper's lock; steps will wait for it "
              "(the queue must not itself run under the wrapper)", flush=True)
    q = load_q()
    if not q["sources"]:
        sys.exit("queue is empty: media-intake queue add PATH --label NAME")
    for label, src in q["sources"].items():
        if src.get("chunk") is None:
            src["chunk"] = a.chunk
            save(QFILE, q)
        chunks = chunks_of(src)
        for n, files in enumerate(chunks, 1):
            batch = batch_name(src, n)
            ch = src["chunks"].setdefault(str(n), dict(batch=batch, steps={}, files=len(files)))
            if ch.get("done"):
                continue
            print(f"[{label}] chunk {n}/{len(chunks)} ({batch}): {len(files)} files", flush=True)
            listfile = os.path.join(sdir(label), f"chunk-{n}.txt")
            log = os.path.join(sdir(label), f"{batch}.log")
            for step in STEPS:
                if step in ch["steps"]:
                    continue
                if step == "stage":
                    if not os.path.isdir(src["root"]) or sources.drive_uuid(src["root"]) != src["uuid"]:
                        ch["error"] = "drive not mounted (or a different drive at that path)"
                        save(QFILE, q)
                        sys.exit(f"[{label}] stopped: {ch['error']}: {src['root']}")
                    need = sum(s for _, s in files)
                    if free_bytes() - need < MIN_FREE:
                        ch["error"] = f"low disk: {gb(free_bytes())} free, chunk needs {gb(need)}, keep {gb(MIN_FREE)}"
                        save(QFILE, q)
                        sys.exit(f"[{label}] stopped: {ch['error']}")
                    with open(os.open(listfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                        f.write("".join(r + "\n" for r, _ in files))
                ok, note = do_step(step, src, ch, batch, listfile, log)
                if not ok:
                    ch["error"] = f"{step} failed ({note}); log {log}"
                    save(QFILE, q)
                    sys.exit(f"[{label}] stopped: {ch['error']}. Fix it, then run again: it resumes at {step}.")
                ch["steps"][step] = dict(at=now(), note=note)
                ch.pop("error", None)
                save(QFILE, q)
                print(f"  {step}: {note}", flush=True)
            ch["summary"] = summary(batch)
            ch["done"] = now()
            save(QFILE, q)
            print("  summary: " + ", ".join(f"{k} {v}" for k, v in ch["summary"].items()), flush=True)
        if all(src["chunks"].get(str(n), {}).get("done") for n in range(1, len(chunks) + 1)):
            src["finished"] = src.get("finished") or now()
            save(QFILE, q)
    print("queue run finished; waiting for you: media-intake queue status")


# ---------------------------------------------------------------- status
def cmd_status(a):
    q = load_q()
    if not q["sources"]:
        print("queue is empty")
        return
    me = "media-intake queue"
    for label, src in q["sources"].items():
        chunks = chunks_of(src)
        done = [n for n in range(1, len(chunks) + 1) if src["chunks"].get(str(n), {}).get("done")]
        print(f"{label}: {src['root']}  {src['files']} files, {gb(src['bytes'])}; "
              f"chunks done {len(done)}/{len(chunks)}" + ("; offloaded " + ",".join(map(str, src["offloaded"]))
                                                           if src["offloaded"] else ""))
        for n in range(1, len(chunks) + 1):
            ch = src["chunks"].get(str(n))
            if ch and not ch.get("done"):
                nxt = next((s for s in STEPS if s not in ch["steps"]), "-")
                print(f"  chunk {n}: at {nxt}" + (f"  ERROR {ch['error']}" if ch.get("error") else ""))
        wait = []
        for n in done:
            batch = src["chunks"][str(n)]["batch"]
            qn, near, rv = pending(batch)
            mt = src["chunks"][str(n)].get("summary", {}).get("maybe_trash", 0)
            if qn or near or rv or mt:
                wait.append((n, batch, qn, near, rv, mt))
        if not wait and done and len(done) == len(chunks) and len(src["offloaded"]) < len(done):
            print(f"  waiting for you: OK to offload (delete the drive originals): {me} offload {label} --yes")
        for n, batch, qn, near, rv, mt in wait:
            print(f"  waiting for you, chunk {n} ({batch}): quarantine {qn}, near and messaging copies {near}, "
                  f"review folder {rv}, trash suggestions {mt}")
            print(f"    review page: media-intake review {batch}")
            if qn:
                print(f"    quarantine:  xdg-open {os.path.join(STATE, 'quarantine', batch)}")
            if mt:
                print(f"    trash suggestions: media-intake gallery   (or media-intake suggest-trash --batch {batch})")


# ---------------------------------------------------------------- offload
def cmd_offload(a):
    q = load_q()
    src = q["sources"].get(a.name)
    if not src:
        sys.exit(f"no queued source {a.name}")
    root = src["root"]
    try:
        sources.check_drive_root(root)
    except sources.Denied as e:
        sys.exit(f"refused: {e}")
    if sources.drive_uuid(root) != src["uuid"]:
        sys.exit(f"refused: the drive at {root} is not the one queued (filesystem UUID differs)")
    done = [n for n, ch in sorted(src["chunks"].items(), key=lambda x: int(x[0]))
            if ch.get("done") and int(n) not in src["offloaded"]]
    if not done:
        sys.exit("no finished chunk left to offload")
    blocked = [(n, *pending(src["chunks"][n]["batch"])[:2]) for n in done]
    blocked = [b for b in blocked if b[1] or b[2]]
    if blocked:
        sys.exit("refused: the quarantine review is not finished for "
                 + ", ".join(f"chunk {n} (quarantine {x}, near and messaging copies {y})" for n, x, y in blocked))
    if not a.yes:
        print(f"{len(done)} finished chunks of {a.name} would have their drive originals deleted "
              "(only files with a verified copy here). Only if you are sure about this drive: add --yes")
        return
    for n in done:
        batch = src["chunks"][n]["batch"]
        r = subprocess.run([PY, "-I", INTAKE, "offload", batch, "--phone", root, "--yes"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode:
            sys.exit(f"chunk {n}: offload failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        src["offloaded"].append(int(n))
        save(QFILE, q)
        print(f"chunk {n}: {last.replace('from the phone', 'from the drive')}")


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("add"); s.add_argument("path"); s.add_argument("--label", required=True)
    s = sp.add_parser("run"); s.add_argument("--chunk", type=int, default=2000); s.add_argument("--inhibit", action="store_true")
    sp.add_parser("status")
    s = sp.add_parser("offload"); s.add_argument("name"); s.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "run" and a.chunk < 1:
        sys.exit("--chunk must be at least 1")
    if a.cmd == "run":
        cmd_run(a, argv)
    else:
        globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
