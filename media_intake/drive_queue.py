#!/usr/bin/env python3
"""Import folders of photos and videos, on an external (non-backup) drive or on the laptop, in chunks.

  media-intake queue add PATH --label NAME [--kind drive|folder] [--event [PROJECT/]SLUG@YYYY-MM-DD ...]
                                           register a folder (counts, size, free-space check);
                                           --kind folder = a folder on the laptop's own disk
  media-intake queue run [--chunk 2000] [--inhibit]   process every source in chunks (resumable)
  media-intake queue status [--json]       one row per source, what waits for you, the next command
  media-intake queue offload NAME [--yes]  DELETES the drive originals of finished, reviewed chunks
                                           (refused for --kind folder: you delete the folder yourself)

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
When a step fails, the run stops and says why in one line (the end of the
step's log, without file names) and where the log is.
The run STOPS before every decision that is yours: quarantine review, trash
suggestions and offload (status prints the commands).

--event days are passed to every chunk's plan (plan --event): Event|SLUG tags, and with
a project, that day's camera files go to the project's folder.

Kind folder: a real folder under HOME, never HOME itself, anything outside
HOME, the media-intake state, the library, videos or a project folder,
~/.local, ~/.config, ~/.cache, ~/.var, ~/.ssh, ~/.gnupg, common code and SDK
trees, a hidden folder, a password-manager name or a git repository (nor inside
or containing one); git trees inside it are skipped. Its originals are never
deleted.

Refused for kind drive: a root on the laptop's own disk, a drive that looks like the backup
drive (<mount>/.media-intake-backup, or a [drive] backup_markers folder at its
mount root, unless <mount>/.media-intake-source-ok exists), a drive without a filesystem UUID, a source
that would leave the laptop under 50 GB free once copied, and offload when the
drive's UUID differs, the drive looks like the backup or a batch's quarantine
is not empty. Output is counts only, never file content. Guide: docs/queue.md.
"""
# Named drive_queue.py, not queue.py: media_intake/ goes on sys.path in intake.py, semantic.py and
# vlm_trash.py, and a queue.py there would shadow the standard library's `queue` (concurrent.futures and
# multiprocessing import it). Kind folder's checks are sources.check_folder_root (no mount, no UUID).
# Test: python3 -I -m unittest discover -s tests -p test_drive_queue.py
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
def check_event(t):
    """Exit unless t is plan's --event form, [PROJECT/]SLUG@YYYY-MM-DD, with a known project."""
    spec, _, day = t.partition("@")
    key, _, ev = spec.rpartition("/")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", day) or not config.SLUG.match(ev):
        sys.exit(f"refused: --event wants [PROJECT/]SLUG@YYYY-MM-DD (slug: lowercase, digits, dashes), got {t}")
    if key and key not in CFG["projects"]:
        sys.exit(f"refused: --event {t}: no project {key!r} in {CFG['path']} "
                 f"(known: {', '.join(CFG['projects']) or 'none'})")


def cmd_add(a):
    if not LABEL.match(a.label):
        sys.exit("refused: --label is lowercase letters, digits and dashes (e.g. old-drive)")
    for t in a.event or []:
        check_event(t)
    root = os.path.abspath(a.path)
    try:
        if a.kind == "folder":  # its real path; no mount, no UUID (offload never runs for it)
            root, mount, uuid = sources.check_folder_root(root), None, None
        else:
            mount = sources.check_drive_root(root)
            uuid = sources.drive_uuid(root)
    except sources.Denied as e:
        sys.exit(f"refused: {e}")
    if a.kind == "drive" and not uuid:
        sys.exit(f"refused: {mount} has no filesystem UUID, so offload could not check it is the same drive")
    q = load_q()
    if a.label in q["sources"]:
        sys.exit(f"refused: {a.label} is already queued ({q['sources'][a.label]['root']})")
    for other in q["sources"].values():
        r1, r2 = root + os.sep, other["root"] + os.sep
        if other.get("kind", "drive") == a.kind and other["uuid"] == uuid \
                and (r1.startswith(r2) or r2.startswith(r1)):
            sys.exit(f"refused: overlaps queued source {other['label']} ({other['root']})")
    files = []
    for path, rel in sources.walk(root, a.kind):
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
    q["sources"][a.label] = dict(label=a.label, kind=a.kind, root=root, mount=mount, uuid=uuid, added=now(),
                                 events=list(a.event or []),
                                 files=len(files), bytes=total, chunk=None, chunks={}, offloaded=[])
    save(QFILE, q)
    by = collections.Counter(sources.drive_category(r, sources.export_app(root, r)).split(":")[0] for r, _ in files)
    print(f"queued {a.label}: {len(files)} files, {gb(total)} ({', '.join(f'{k} {v}' for k, v in sorted(by.items()))}); "
          f"{gb(free - total)} free after copying")
    print("next: media-intake queue run --inhibit")


# ---------------------------------------------------------------- run
def _private(path, flags):
    """open() opener: create the file owner-only (0600)."""
    return os.open(path, flags, 0o600)


def run_cmd(cmd, log):
    """Run one step; its output goes to the step log, never the terminal. Returns the exit code."""
    with open(log, "a", opener=_private) as f:
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
        return [PY, "-I", INTAKE, "stage", src["root"], "--kind", src.get("kind", "drive"), "--batch", batch,
                "--only", listfile]
    if step == "plan":
        return [PY, "-I", INTAKE, "plan", batch] + [x for t in src.get("events") or [] for x in ("--event", t)]
    if step in ("scan", "apply", "tag"):
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
    return rc == 0, "done" if rc == 0 else f"exit {rc}"


# Why a step failed, in one line for a person: the last line the step wrote to its log, with every path and
# media file name taken out (a log line may name a user's file; status and the stop message never do).
PATH_TOKEN = re.compile(r"""(?<![\w/.~])(?:~|\.\.?)?/[^\s'"`(),;:]*|[^\s'"`(),;:=/]+(?:/[^\s'"`(),;:/]+){2,}/?""")
MEDIA_NAME = re.compile(r"[^\s'\"`/(),;:=]+(?:%s)\b" % "|".join(
    re.escape(e) for e in sorted(sources.MEDIA_EXT | sources.CAMERA_JUNK_EXT | {".xmp"}, key=len, reverse=True)),
    re.I)
QUOTED_PATH = re.compile(r"""(['"`])([^'"`\n]*/[^'"`\n]*)\1""")  # a quoted path may hold spaces
TOOL_FILE = re.compile(r"[\w.-]+\.(?:sh|py)")
REASON_MAX = 160


def plain_reason(line):
    """line without paths or media file names: a path to one of the tool's own scripts keeps its name
    (setup.sh), any other path becomes "(a path)", a media file name "(a file)"."""
    def path(m):
        tail = m.group(m.lastindex or 0).rstrip("/.").rsplit("/", 1)[-1]
        return tail if TOOL_FILE.fullmatch(tail) else "(a path)"
    line = MEDIA_NAME.sub("(a file)", PATH_TOKEN.sub(path, QUOTED_PATH.sub(path, line)))
    line = re.sub(r"(\(a (?:path|file)\)['\"`]?\s*)+(?=\(a )", "", line)  # runs of them read as one
    line = re.sub(r"['\"`]\((a (?:path|file))\)['\"`]", r"(\1)", line)
    line = " ".join(line.split())
    return line if len(line) <= REASON_MAX else line[:REASON_MAX - 1].rstrip() + "\u2026"


def log_size(log):
    try:
        return os.path.getsize(log)
    except OSError:
        return 0


def log_reason(log, start=0):
    """The last non-empty line written to log after byte start (where the failed step began), made plain;
    "" when the step wrote nothing. run_cmd's own `== <time> <command>` line is never the reason."""
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(start, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    last = next((x for x in reversed(text.splitlines()) if x.strip() and not x.startswith("== ")), "")
    return plain_reason(last.strip())


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
        pass  # lock file unreadable: treat as not held
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
                    if src.get("kind") == "folder":  # checked again: the folder may have moved or changed
                        try:
                            sources.check_folder_root(src["root"])
                        except sources.Denied as e:
                            ch["error"] = f"folder refused now: {e}"
                            save(QFILE, q)
                            sys.exit(f"[{label}] stopped: {ch['error']}")
                    elif not os.path.isdir(src["root"]) or sources.drive_uuid(src["root"]) != src["uuid"]:
                        ch["error"] = "drive not mounted (or a different drive at that path)"
                        save(QFILE, q)
                        sys.exit(f"[{label}] stopped: {ch['error']}: {src['root']}")
                    need = sum(s for _, s in files)
                    if free_bytes() - need < MIN_FREE:
                        ch["error"] = f"low disk: {gb(free_bytes())} free, chunk needs {gb(need)}, keep {gb(MIN_FREE)}"
                        save(QFILE, q)
                        sys.exit(f"[{label}] stopped: {ch['error']}")
                    with open(listfile, "w", opener=_private) as f:
                        f.write("".join(r + "\n" for r, _ in files))
                start = log_size(log)
                ok, note = do_step(step, src, ch, batch, listfile, log)
                if not ok:
                    why = log_reason(log, start)
                    ch["error"] = f"{step} failed ({note})" + (f": {why}" if why else "")
                    ch["log"] = log
                    save(QFILE, q)
                    sys.exit(f"[{label}] stopped at {step}: {why or f'it failed ({note}) with no message'}. "
                             f"Fix it, then run again: {ME} run --inhibit (it resumes at {step}; log: {log})")
                ch["steps"][step] = dict(at=now(), note=note)
                ch.pop("error", None)
                ch.pop("log", None)
                save(QFILE, q)
                print(f"  {step} done" if note == "done" else f"  {step}: {note}", flush=True)
            ch["summary"] = summary(batch)
            ch["done"] = now()
            save(QFILE, q)
            print("  summary: " + ", ".join(f"{k} {v}" for k, v in ch["summary"].items()), flush=True)
        if all(src["chunks"].get(str(n), {}).get("done") for n in range(1, len(chunks) + 1)):
            src["finished"] = src.get("finished") or now()
            save(QFILE, q)
    print("queue run finished; waiting for you: media-intake queue status")


# ---------------------------------------------------------------- status
WIDTH = 80  # status fits an 80-column terminal; commands are never cut (they must stay copyable)
ME = "media-intake queue"
ADD_HINT = f"{ME} add PATH --label NAME  (--kind folder for a folder on the laptop)"


def run_lock_held():
    """True while a `queue run` holds <state>/queue/run.lock. Never creates the lock file or its folder."""
    p = os.path.join(QDIR, "run.lock")
    try:
        f = open(p)
    except OSError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        except BlockingIOError:
            return True
        except OSError:
            return False  # flock unsupported on that filesystem: no run can hold it either, so not held
    return False


def shorten(text, width):
    """Cut text to width in the middle with an ellipsis, keeping the end (the part that tells paths apart)."""
    if len(text) <= width:
        return text
    if width < 2:
        return "\u2026"[:width]
    head = (width - 1) // 4  # a short start, most of the room for the end
    return text[:head] + "\u2026" + text[len(text) - (width - 1 - head):]


def changed_at(src):
    """The newest time recorded for a source: added, a step finished, a chunk done, the source finished."""
    times = [src.get("added"), src.get("finished")]
    for ch in src["chunks"].values():
        times.append(ch.get("done"))
        times += [st.get("at") for st in ch.get("steps", {}).values()]
    return max(t for t in times if t)


def status_model(q):
    """Counts and paths only: one entry per source, what waits for you, and the next command."""
    running = run_lock_held()
    current = next((lb for lb, s in q["sources"].items() if not s.get("finished")), None) if running else None
    rows, waiting = [], []
    for label, src in q["sources"].items():
        kind = src.get("kind", "drive")
        chunks = chunks_of(src)
        detail, quar, review, failed = [], 0, 0, None
        for n in range(1, len(chunks) + 1):
            ch = src["chunks"].get(str(n)) or {}
            d = dict(chunk=n, batch=ch.get("batch") or batch_name(src, n), files=ch.get("files", len(chunks[n - 1])),
                     done=bool(ch.get("done")))
            if ch.get("done"):
                qn, near, rv = pending(d["batch"])
                mt = ch.get("summary", {}).get("maybe_trash", 0)
                d.update(quarantine=qn, near_and_messaging=near, review_folder=rv, trash_suggestions=mt)
                quar, review = quar + qn + near, review + rv + mt
                if qn or near or rv or mt:
                    cmds = [f"media-intake review {d['batch']}"]
                    if qn:
                        cmds.append(f"xdg-open {os.path.join(STATE, 'quarantine', d['batch'])}")
                    if mt:
                        cmds.append(f"media-intake gallery   (or media-intake suggest-trash --batch {d['batch']})")
                    waiting.append(dict(what="review", label=label, chunk=n, batch=d["batch"], quarantine=qn,
                                        near_and_messaging=near, review_folder=rv, trash_suggestions=mt,
                                        commands=cmds))
            else:
                d["step"] = next((s for s in STEPS if s not in ch.get("steps", {})), "-")
                if ch.get("error"):
                    d["error"] = ch["error"]
                    if ch.get("log"):
                        d["log"] = ch["log"]
                    failed = failed or d
            detail.append(d)
        done = sum(1 for d in detail if d["done"])
        offloaded = list(src.get("offloaded") or [])
        all_done = bool(chunks) and done == len(chunks)
        if label == current:
            state = "running"
        elif failed:
            state = "failed"
            waiting.append(dict(what="failed", label=label, chunk=failed["chunk"], batch=failed["batch"],
                                step=failed["step"], reason=failed["error"], log=failed.get("log"),
                                commands=[f"{ME} run --inhibit"]))
        elif any(w["label"] == label and w["what"] == "review" for w in waiting):
            state = "needs your review"
        elif not all_done:
            state = "waiting"
        else:
            state = "done"
        if all_done and not any(w["label"] == label for w in waiting):
            if kind == "folder":
                waiting.append(dict(what="delete-folder", label=label, root=src["root"], commands=[]))
            elif len(offloaded) < done:
                waiting.append(dict(what="offload", label=label, commands=[f"{ME} offload {label} --yes"]))
        rows.append(dict(label=label, kind=kind, root=src["root"], state=state, files=src["files"],
                         bytes=src["bytes"], quarantined=quar, in_review=review, changed=changed_at(src),
                         chunks_total=len(chunks), chunks_done=done, offloaded=offloaded, chunks=detail))
    first = {w["what"]: w for w in reversed(waiting)}  # the first entry of each kind
    if "failed" in first:
        nxt = f"fix {first['failed']['label']} (see above), then {ME} run --inhibit"
    elif "review" in first:
        nxt = first["review"]["commands"][0]
    elif "offload" in first:
        nxt = first["offload"]["commands"][0]
    elif "delete-folder" in first:
        nxt = f"check the library, then delete {first['delete-folder']['root']} yourself"
    elif current:
        nxt = f"nothing yet: the run is going; check again with {ME} status"
    elif any(r["state"] == "waiting" for r in rows):
        nxt = f"{ME} run --inhibit"
    else:
        nxt = f"nothing waits; add another folder: {ME} add PATH --label NAME"
    return dict(sources=rows, waiting=waiting, next=nxt)


def wrap(text, indent=""):
    import textwrap
    return textwrap.fill(text, WIDTH, initial_indent=indent, subsequent_indent=indent + "  ",
                         break_long_words=False, break_on_hyphens=False)


def render_status(m):
    rows = m["sources"]
    head = ("LABEL", "SOURCE", "STATE", "FILES", "QUAR", "REVIEW", "CHANGED")
    cells = [[r["label"], r["root"], r["state"], str(r["files"]), str(r["quarantined"]), str(r["in_review"]),
              r["changed"].replace("T", " ")[:16]] for r in rows]
    widths = [max(len(head[i]), *(len(c[i]) for c in cells)) for i in range(len(head))]
    src = 1  # the one column shortened to fit: the label is what queue offload takes, so it is never cut
    rest = sum(w for i, w in enumerate(widths) if i != src)
    widths[src] = max(len(head[src]), min(widths[src], WIDTH - rest - 2 * (len(head) - 1)))
    right = {3, 4, 5}

    def line(vals):
        out = [(v.rjust(w) if i in right else v.ljust(w)) for i, (v, w) in enumerate(zip(vals, widths))]
        return "  ".join(out).rstrip()
    out = [line(head)] + [line([c[0], shorten(c[src], widths[src]), *c[src + 1:]]) for c in cells]
    out += ["", "Waiting for you:"]
    if not m["waiting"]:
        out.append("  nothing")
    for w in m["waiting"]:
        if w["what"] == "review":
            out.append(wrap(f"{w['label']}, chunk {w['chunk']} ({w['batch']}): quarantine {w['quarantine']}, "
                            f"near and messaging copies {w['near_and_messaging']}, review folder "
                            f"{w['review_folder']}, trash suggestions {w['trash_suggestions']}", "  "))
            names = ["review page", "quarantine", "trash suggestions"]
            if not w["quarantine"]:
                names.remove("quarantine")
            for name, c in zip(names, w["commands"]):
                out.append(f"    {name + ':':<20}{c}")
        elif w["what"] == "failed":
            out.append(wrap(f"{w['label']}, chunk {w['chunk']} ({w['batch']}) failed at {w['step']}: "
                            f"{w['reason']}", "  "))
            if w.get("log"):
                out.append(f"    {'log:':<20}{w['log']}")
            out.append(f"    {'fix it, then retry:':<20}{w['commands'][0]}")
        elif w["what"] == "offload":
            out.append(wrap(f"{w['label']}: every chunk is reviewed; OK to offload (delete the drive originals):",
                            "  "))
            out.append(f"    {w['commands'][0]}")
        else:
            out.append(f"  {w['label']}: everything is reviewed; delete the source folder yourself")
            out.append(f"    (check the library first; the tool never deletes it): {w['root']}")
    out += ["", f"next: {m['next']}"]
    return "\n".join(out)


def cmd_status(a):
    q = load_q()
    if not q["sources"]:
        if a.json:
            print(json.dumps(dict(sources=[], waiting=[], next=f"{ME} add PATH --label NAME"), indent=1))
        else:
            print(f"queue is empty; add a folder: {ADD_HINT}")
        return
    m = status_model(q)
    print(json.dumps(m, indent=1, ensure_ascii=False) if a.json else render_status(m))


# ---------------------------------------------------------------- offload
def cmd_offload(a):
    q = load_q()
    src = q["sources"].get(a.name)
    if not src:
        sys.exit(f"no queued source {a.name}")
    root = src["root"]
    if src.get("kind") == "folder":
        sys.exit(f"refused: {a.name} is a folder on this laptop ({root}); the tool never deletes its originals. "
                 "Check the import, then delete the folder yourself.")
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
    sp = ap.add_subparsers(dest="queue_command", required=True, metavar="add|run|status|offload")
    s = sp.add_parser("add", help="register a folder to import (counts and checks only; copies nothing yet)",
                      description="Register a folder of photos and videos to import: counts its files and checks "
                                  "there is room. Copies nothing yet; `run` does the work.")
    s.add_argument("path", help="the folder: on an external drive, or with --kind folder on this laptop")
    s.add_argument("--label", required=True,
                   help="a short name for this source (lowercase letters, digits, dashes, e.g. old-drive); "
                        "status, run and offload use it")
    s.add_argument("--kind", choices=["drive", "folder"], default="drive",
                   help="drive = an external drive (default); folder = a folder on the laptop's own disk")
    s.add_argument("--event", action="append", metavar="[PROJECT/]SLUG@YYYY-MM-DD",
                   help="an event (shoot day) in this source, passed to every chunk's plan --event")
    s = sp.add_parser("run", help="import every queued source, a chunk at a time (stops when you must decide)",
                      description="Import every queued source, one chunk at a time: copy, check, file and tag. "
                                  "Resumes where it stopped; never deletes anything and stops before every "
                                  "decision that is yours.")
    s.add_argument("--chunk", type=int, default=2000, metavar="N",
                   help="files per chunk (default 2000); fixed for a source at its first run")
    s.add_argument("--inhibit", action="store_true",
                   help="keep the laptop from sleeping while it runs (systemd-inhibit)")
    s = sp.add_parser("status", help="what is queued, what waits for you, and the next command",
                      description="One row per queued source, what waits for you, and the next command. "
                                  "Changes nothing.")
    s.add_argument("--json", action="store_true", help="the same, as JSON for scripts")
    s = sp.add_parser("offload", help="DELETES the drive originals of finished, reviewed chunks (asks for --yes)",
                      description="DELETES the originals on the drive for every finished chunk whose review is "
                                  "done, but only files with a verified copy on this laptop. Without --yes it "
                                  "only says what it would do. Refused for a laptop folder: you delete that "
                                  "yourself.")
    s.add_argument("name", metavar="LABEL", help="the source's label, as status shows it")
    s.add_argument("--yes", action="store_true", help="really delete; without it nothing is deleted")
    a = ap.parse_args(argv)
    if a.queue_command == "run" and a.chunk < 1:
        sys.exit("--chunk must be at least 1")
    if a.queue_command == "run":
        cmd_run(a, argv)
    else:
        globals()["cmd_" + a.queue_command](a)


if __name__ == "__main__":
    main()
