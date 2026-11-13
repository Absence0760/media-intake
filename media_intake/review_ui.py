#!/usr/bin/env python3
"""Local-only review page for a media-intake batch: quarantine + review folder.

  python3 -I media_intake/review_ui.py BATCH [--phone ROOT] [--drive MOUNT] [--port 0]
  python3 -I media_intake/review_ui.py [BATCH] --maybe-trash [--phone ROOT]   (after media-intake suggest-trash)

The quarantine can hold nudity and IDs, so this page never leaves the machine:
stdlib only, everything inline (no CDN, fonts or scripts from anywhere), bound
to 127.0.0.1 on a random port, a random token on every request, a Host check
against DNS rebinding, a CSRF value on every POST, no-store on every response.
Media are served as-is from the quarantine/review files by an opaque id (an
index into the list built at start-up), never by path; nothing is written to
disk except batches/<batch>/review-decisions.json (0600).

A near-duplicate is shown beside its original (display only, its own opaque id
under /o/): the plan's `why` names it, and it is served only if it resolves to
a file under the library roots or this batch's raw/ or review/. A near-copy of
an erased photo (quarantine/near-<batch>) gets a note with the erase date; the
erased file is never looked for. quarantine/xcheck-<batch> (quarantine-only: no
plan or manifest) holds messaging copies of library photos; each entry's `of`
is shown as the original, same roots check, and its erase gets no --phone.
Every item can also be trashed (`intake.py trash`: plain delete for junk that
isn't private, remembered so it never comes back). --maybe-trash adds a tab of
library files `suggest-trash` listed (maybe-trash.json): Keep runs `not-trash`.

On submit (every item decided, ERASE typed) it runs, as subprocesses of the
same Python: deletions of review items, then for quarantine/near-<batch> and
quarantine/<batch>: `release` for kept files, `erase --yes` for the rest,
`verify-gone`. Their output is streamed to the page, then the server exits.
An assistant may start this; only you open the page.
"""
import argparse, datetime, hashlib, html, http.server, json, mimetypes, os, re, secrets, shutil, struct
import subprocess, sys, threading, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

HOME = os.path.expanduser("~")
STATE = config.get()["state"]
# Tests point this at a fake that records its argv, so a real erase never runs there.
INTAKE = os.environ.get("MEDIA_REVIEW_INTAKE", os.path.join(HERE, "intake.py"))
BATCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
# What a browser can show; anything else (HEIC, DNG, GPR) gets a "no preview" card.
SHOW_IMG = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp"}
SHOW_VID = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".webm": "video/webm",
            ".mkv": "video/webm", ".3gp": "video/3gpp"}
CSP = ("default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; "
       "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'")
CHUNK = 1 << 16
# Where an original may be served from (config.toml, as intake.py), plus the batch's raw/ and review/.
LIB_ROOTS = config.library_roots()


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_private(p, obj):
    """Write JSON at mode 0600 (created that way, never world-readable for a moment)."""
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def thumb_forget(path):
    """Delete freedesktop thumbnails of path (same as intake.thumb_forget)."""
    uri = "file://" + urllib.parse.quote(os.path.abspath(path), safe="/!$&'()*+,;=:@")
    name = hashlib.md5(uri.encode()).hexdigest() + ".png"
    base = os.path.join(HOME, ".cache", "thumbnails")
    n = 0
    if os.path.isdir(base):
        for sub in os.listdir(base):
            for cand in (os.path.join(base, sub, name), os.path.join(base, sub, "gnome-thumbnail-factory", name)):
                if os.path.exists(cand):
                    os.remove(cand)
                    n += 1
    return n


def inside(root, p):
    """p is a regular file (not a symlink) whose real path is under root."""
    r = os.path.realpath(root)
    return (not os.path.islink(p) and os.path.isfile(p)
            and os.path.realpath(p).startswith(r + os.sep))


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in SHOW_IMG:
        return "image", SHOW_IMG[ext]
    if ext in SHOW_VID:
        return "video", SHOW_VID[ext]
    return "none", mimetypes.guess_type(path)[0] or "application/octet-stream"


def image_size(path):
    """(w, h) from the header of a JPEG/PNG/GIF/BMP/WebP, else None. Stdlib only."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:2] == b"BM" and len(head) >= 26:
                w, h = struct.unpack("<ii", head[18:26])
                return w, abs(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                f.seek(0)
                d = f.read(40)
                c = d[12:16]
                if c == b"VP8 ":
                    w, h = struct.unpack("<HH", d[26:30])
                    return w & 0x3FFF, h & 0x3FFF
                if c == b"VP8L":
                    b = int.from_bytes(d[21:25], "little")
                    return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
                if c == b"VP8X":
                    return int.from_bytes(d[24:27], "little") + 1, int.from_bytes(d[27:30], "little") + 1
                return None
            if head[:2] != b"\xff\xd8":
                return None
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    return None
                m = b[0]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    continue
                n = struct.unpack(">H", f.read(2))[0]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">xHH", f.read(5))
                    return w, h
                f.seek(n - 2, 1)
    except (OSError, struct.error):
        return None


def video_size(path):
    """(w, h) of the first video stream via ffprobe, if installed."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0:s=x", "file:" + path],
                           capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        m = re.match(r"(\d+)x(\d+)", r.stdout.strip())
        return (int(m.group(1)), int(m.group(2))) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def meta(path, kind, date=None):
    """What you compare: resolution, size, date, folder (relative to HOME)."""
    wh = image_size(path) if kind == "image" else video_size(path) if kind == "video" else None
    st = os.stat(path)
    if date:
        date = date.replace("T", " ")[:16]
    else:
        date = f"{datetime.datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}"
    d = os.path.dirname(path)
    folder = "~/" + os.path.relpath(d, HOME) if d.startswith(HOME + os.sep) else d
    return dict(res=f"{wh[0]}x{wh[1]}" if wh else "unknown", size=human(st.st_size), date=date, folder=folder)


def original_allowed(batch, p):
    """An original may be shown only from the library roots or this batch's raw/ and review/."""
    if os.path.realpath(p).startswith(os.path.realpath(os.path.join(STATE, "quarantine")) + os.sep):
        return False
    b = os.path.join(STATE, "batches", batch)
    roots = (*LIB_ROOTS, os.path.join(b, "raw"), os.path.join(b, "review"))
    return any(inside(r, p) for r in roots)


def attach_originals(batch, items):
    """Give every near-duplicate an `orig` (display only). Returns the served originals list."""
    plan = {p["rel"]: p for p in load(os.path.join(STATE, "batches", batch, "plan.json"), []) or []}
    quar_own = load(os.path.join(STATE, "quarantine", batch, "quarantine.json"), {})
    b = os.path.join(STATE, "batches", batch)
    origs = []
    for it in items:
        it["orig"], it["meta"] = None, None
        if it["qbatch"] == f"near-{batch}":
            v = load(os.path.join(STATE, "quarantine", it["qbatch"], "quarantine.json"), {}).get(it["rel"], {})
            it["orig"] = dict(erased=True, date=v.get("erased_on"))
            it["meta"] = meta(it["path"], it["kind"])
            continue
        why = it["why"] or ""
        if it.get("of"):  # xcheck-<batch>: a messaging copy of a library photo
            it["meta"] = meta(it["path"], it["kind"])
            of = it["of"]
            if not (isinstance(of, str) and os.path.isabs(of) and original_allowed(batch, of)):
                it["orig"] = dict(missing="the original was not found in the library",
                                  label="WhatsApp/Messages copy of this photo")
                continue
            k, mime = kind_of(of)
            origs.append(dict(path=of, kind=k, mime=mime))
            it["orig"] = dict(id=len(origs) - 1, kind=k, meta=meta(of, k), label="WhatsApp/Messages copy of this photo")
            continue
        if why.startswith("near-duplicate of library "):
            cands = [why[len("near-duplicate of library "):]]
            odate = None
        elif why.startswith("near-duplicate of "):
            orel = why[len("near-duplicate of "):]
            o = plan.get(orel)
            if o is None:
                it["orig"] = dict(missing="the original is not in this batch's plan")
                continue
            if orel in quar_own:
                it["orig"] = dict(missing=f"the original is in quarantine too: {orel}")
                continue
            cands = [o.get("dst"), o.get("lib_dst"), os.path.join(b, "raw", orel), os.path.join(b, "review", orel)]
            odate = o.get("date")
        else:
            continue
        own = plan.get(it["rel"], {}).get("date")
        it["meta"] = meta(it["path"], it["kind"], own)
        hit = next((c for c in cands if c and os.path.isabs(c) and original_allowed(batch, c)), None)
        if not hit:
            it["orig"] = dict(missing="the original was not found in the library")
            continue
        k, mime = kind_of(hit)
        origs.append(dict(path=hit, kind=k, mime=mime))
        it["orig"] = dict(id=len(origs) - 1, kind=k, meta=meta(hit, k, odate))
    return origs


def sub_batches(batch):
    """Quarantine batches reviewed together, in run order: near-copies of erased photos first
    (erasing the others can write a fresh near-<batch>), then messaging cross-check copies, then the batch."""
    return (f"near-{batch}", f"xcheck-{batch}", batch)


def collect(batch):
    """Every reviewable item: [{tab, qbatch, rel, path, reasons, why, kind, mime}]."""
    items = []
    plan_why = {p["rel"]: p.get("why", "") for p in load(os.path.join(STATE, "batches", batch, "plan.json"), []) or []}
    for qb in sub_batches(batch):
        quar = os.path.join(STATE, "quarantine", qb)
        for rel, v in sorted(load(os.path.join(quar, "quarantine.json"), {}).items()):
            p = os.path.join(quar, rel)
            if not inside(quar, p):
                continue
            k, mime = kind_of(p)
            items.append(dict(tab="quarantine", qbatch=qb, rel=rel, path=p, reasons=list(v.get("reasons") or []),
                              why=plan_why.get(rel, "") if qb == batch else "", kind=k, mime=mime,
                              releasable=bool(v.get("dst")), of=v.get("of")))
    rv = os.path.join(STATE, "batches", batch, "review")
    why = {p["rel"]: p.get("why", "") for p in load(os.path.join(STATE, "batches", batch, "plan.json"), []) or []
           if p.get("action") == "review"}
    if os.path.isdir(rv):
        for r, ds, fs in os.walk(rv):
            ds.sort()
            for f in sorted(fs):
                p = os.path.join(r, f)
                if f.endswith(".xmp") or not inside(rv, p):
                    continue
                rel = os.path.relpath(p, rv)
                k, mime = kind_of(p)
                items.append(dict(tab="review", qbatch=None, rel=rel, path=p, reasons=[], why=why.get(rel, ""),
                                  kind=k, mime=mime, releasable=True))
    return items


def collect_maybe():
    """Library files from suggest-trash's maybe-trash.json, re-checked against the library roots."""
    items = []
    for x in (load(os.path.join(STATE, "maybe-trash.json"), {}) or {}).get("items", []):
        p = x.get("path") if isinstance(x, dict) else None
        if not (isinstance(p, str) and os.path.isabs(p)) or os.path.realpath(p).startswith(
                os.path.realpath(STATE) + os.sep) or not any(inside(r, p) for r in LIB_ROOTS):
            continue
        k, mime = kind_of(p)
        rel = "~/" + os.path.relpath(p, HOME) if p.startswith(HOME + os.sep) else p
        items.append(dict(tab="maybe", qbatch=None, rel=rel, path=p, why="", kind=k, mime=mime, releasable=True,
                          reasons=[str(t) for t in x.get("labels") or [] if isinstance(t, str)]))
    return items


class App:
    def __init__(self, batch, items, phone, drive, keep_phone_thumbnails=False, no_wipe_free=False, origs=()):
        self.batch, self.items = batch, items
        self.origs = list(origs)  # display-only originals, served by id under /o/
        self.phone, self.drive = phone, drive
        self.keep_phone_thumbnails, self.no_wipe_free = keep_phone_thumbnails, no_wipe_free
        self.token = secrets.token_urlsafe(24)
        self.csrf = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.submitted = False
        self.lines, self.done, self.ok = [], False, None
        self.done_seen = threading.Event()
        self.finished = threading.Event()
        self.port = None

    # ------------------------------------------------------------ run
    def log(self, s):
        with self.lock:
            self.lines.append(s)
        print(s, flush=True)

    def run(self, args, label):
        cmd = [sys.executable, "-I", "-u", INTAKE] + args
        self.log(f"$ intake.py {' '.join(a if not a.startswith(STATE) else '<state>' + a[len(STATE):] for a in args)}")
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, cwd=HERE)
        for line in p.stdout:
            self.log("  " + line.rstrip("\n"))
        rc = p.wait()
        self.log(f"  [{label}: exit {rc}]")
        return rc

    def device_args(self, verify=False, phone=True):
        a = []
        if self.phone and phone and not verify:
            a += ["--phone", self.phone]
            if self.keep_phone_thumbnails:
                a.append("--keep-phone-thumbnails")
        if self.drive:
            a += ["--drive", self.drive]
            if self.no_wipe_free and not verify:
                a.append("--no-wipe-free")
        return a

    def execute(self, dec):
        ok = True
        try:
            ok = self._execute(dec)
        except Exception as e:  # report, never leave the page hanging
            self.log(f"error: {type(e).__name__}: {e}")
            ok = False
        with self.lock:
            self.done, self.ok = True, ok
        where = f"batches/{self.batch}/review-decisions.json" if self.batch else "maybe-trash-decisions.json"
        self.log("done: " + ("all steps succeeded" if ok else f"something failed, see above; decisions are in {where}"))
        self.finished.set()

    def phone_args(self, qb=None):
        return ["--phone", self.phone] if self.phone and not (qb or "").startswith("xcheck-") else []

    def _execute(self, dec):
        ok = True
        # 0. Maybe-trash suggestions: Keep marks them not-trash, Trash deletes.
        mb = [(i, it) for i, it in enumerate(self.items) if it["tab"] == "maybe"]
        keep = [it["path"] for i, it in mb if dec[i] == "keep"]
        trash = [it["path"] for i, it in mb if dec[i] == "trash"]
        if keep:
            ok &= self.run(["not-trash"] + keep, "not-trash") == 0
        if trash:
            ok &= self.run(["trash"] + trash + self.phone_args(), "trash") == 0
        rv_items = [(i, it) for i, it in enumerate(self.items) if it["tab"] == "review"]
        trash = [it["path"] for i, it in rv_items if dec[i] == "trash"]
        if trash:
            ok &= self.run(["trash", "--batch", self.batch] + trash + self.phone_args(), "trash review") == 0
            rv_items = [(i, it) for i, it in rv_items if dec[i] != "trash"]
        # 1. Review folder first: erase's near-duplicate search may move review files.
        if rv_items:
            kept = [it for i, it in rv_items if dec[i] == "keep"]
            gone = [it for i, it in rv_items if dec[i] == "delete"]
            if gone and not kept:
                ok &= self.run(["purge-review", self.batch], "purge-review") == 0
            elif gone:
                n = t = 0
                for it in gone:
                    p = it["path"]
                    if os.path.lexists(p):
                        os.remove(p)
                        n += 1
                    if os.path.isfile(p + ".xmp"):
                        os.remove(p + ".xmp")
                    t += thumb_forget(p)
                self.log(f"review: deleted {n} of {len(gone)} files ({t} thumbnails)")
            if kept:
                self.log(f"review: {len(kept)} kept items stay in review/ — no command to file them yet")
        # 2. Quarantine: near-<batch> first (erasing <batch> can write a fresh near-<batch>).
        for qb in sub_batches(self.batch):
            q_items = [(i, it) for i, it in enumerate(self.items) if it["tab"] == "quarantine" and it["qbatch"] == qb]
            if not q_items:
                continue
            quar = os.path.join(STATE, "quarantine", qb)
            kept = [it for i, it in q_items if dec[i] == "keep"]
            erase = [it for i, it in q_items if dec[i] == "erase"]
            trash = [it["path"] for i, it in q_items if dec[i] == "trash"]
            self.log(f"{qb}: keep {len(kept)}, erase {len(erase)}, trash {len(trash)}")
            if trash and self.run(["trash", "--batch", qb] + trash + self.phone_args(qb), f"trash {qb}") != 0:
                self.log(f"{qb}: trash failed; stopping before any erase")
                return False
            if kept:
                if self.run(["release", qb] + [it["path"] for it in kept], f"release {qb}") != 0:
                    self.log(f"{qb}: release failed; stopping before any erase")
                    return False
                left = set(load(os.path.join(quar, "quarantine.json"), {}))
                stuck = [it["rel"] for it in kept if it["rel"] in left]
                if stuck:
                    self.log(f"{qb}: {len(stuck)} kept files have no library place and are still in quarantine; "
                             f"erase of {qb} skipped so nothing you kept is erased")
                    ok = False
                    continue
            if erase:
                # xcheck copies' phone originals are already offloaded: never pass --phone there.
                dev = self.device_args(phone=not qb.startswith("xcheck-"))
                ok &= self.run(["erase", qb, "--yes"] + dev, f"erase {qb}") == 0
                ok &= self.run(["verify-gone", qb] + self.device_args(verify=True), f"verify-gone {qb}") == 0
        for qb, again in ((f"near-{self.batch}", self.batch), (f"near-near-{self.batch}", self.batch),
                          (f"near-xcheck-{self.batch}", f"xcheck-{self.batch}")):
            q = load(os.path.join(STATE, "quarantine", qb, "quarantine.json"), {})
            if q:
                self.log(f"{len(q)} near-duplicates now in quarantine/{qb}: run the review again for {again}")
        return ok

    # ------------------------------------------------------------ submit
    def submit(self, body):
        """Validate and start. Returns (http code, message)."""
        if body.get("confirm") != "ERASE":
            return 400, "type ERASE to confirm"
        raw = body.get("decisions")
        if not isinstance(raw, dict):
            return 400, "no decisions"
        dec = {}
        for i, it in enumerate(self.items):
            d = raw.get(str(i))
            allowed = {"quarantine": ("keep", "erase", "trash"), "review": ("keep", "delete", "trash"),
                       "maybe": ("keep", "trash")}[it["tab"]]
            if d not in allowed:
                return 409, "every item needs a decision"
            dec[i] = d
        if set(raw) - {str(i) for i in range(len(self.items))}:
            return 400, "unknown item"
        with self.lock:
            if self.submitted:
                return 409, "already submitted"
            self.submitted = True
        bd = os.path.join(STATE, "batches", self.batch) if self.batch else STATE
        os.makedirs(bd, mode=0o700, exist_ok=True)
        rec = {"batch": self.batch, "submitted": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
               "quarantine": {}, "review": {}}
        for i, it in enumerate(self.items):
            if it["tab"] == "quarantine":
                rec["quarantine"].setdefault(it["qbatch"], {})[it["rel"]] = dec[i]
            elif it["tab"] == "maybe":
                rec.setdefault("maybe", {})[it["rel"]] = dec[i]
            else:
                rec["review"][it["rel"]] = dec[i]
        write_private(os.path.join(bd, "review-decisions.json" if self.batch else "maybe-trash-decisions.json"), rec)
        threading.Thread(target=self.execute, args=(dec,), daemon=True).start()
        return 202, "started"

    def public_items(self):
        return [dict(id=i, tab=it["tab"], batch=it["qbatch"] or self.batch, rel=it["rel"], reasons=it["reasons"],
                     why=it["why"], kind=it["kind"], releasable=it["releasable"],
                     orig=it.get("orig"), meta=it.get("meta")) for i, it in enumerate(self.items)]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "review"
    sys_version = ""
    app = None  # set by serve()

    def log_message(self, *a):  # no request lines (they would carry the token) in the terminal
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def reply(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.reply(code, f"{code}\n")

    def json(self, code, obj):
        self.reply(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    # ------------------------------------------------------------ guards
    def cookie_name(self):
        return f"review_{self.app.port}"  # cookies ignore ports; one name per server

    def host_ok(self):
        p = self.app.port
        return self.headers.get("Host", "") in (f"127.0.0.1:{p}", f"localhost:{p}")

    def token_ok(self, query):
        tok = self.app.token
        cands = [self.headers.get("X-Token", "")] + query.get("t", [])
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.cookie_name():
                cands.append(v)
        return any(c and secrets.compare_digest(c.encode(), tok.encode()) for c in cands)

    def guard(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self.host_ok() or not self.token_ok(q):
            self.reply(403, "403\n")
            return None
        return u.path, q

    # ------------------------------------------------------------ GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        g = self.guard()
        if not g:
            return
        path, q = g
        app = self.app
        if path == "/" and q.get("t"):
            # Swap the token in the URL for a Strict cookie, so the address bar holds no token.
            self.reply(303, "", headers=[("Location", "/"), ("Set-Cookie", f"{self.cookie_name()}={app.token}; "
                                                                           "Path=/; HttpOnly; SameSite=Strict")])
        elif path == "/":
            self.reply(200, PAGE.replace("__CSRF__", html.escape(app.csrf)), "text/html; charset=utf-8")
        elif path == "/api/items":
            dev = {"phone": app.phone, "drive": app.drive}
            self.json(200, {"batch": app.batch, "items": app.public_items(), "devices": dev})
        elif path == "/api/progress":
            try:
                since = max(0, int(q.get("since", ["0"])[0]))
            except ValueError:
                since = 0
            with app.lock:
                lines, done, ok = app.lines[since:], app.done, app.ok
            if done and since + len(lines) >= len(app.lines):
                app.done_seen.set()
            self.json(200, {"lines": lines, "next": since + len(lines), "done": done, "ok": ok})
        elif m := re.fullmatch(r"/m/([0-9]{1,9})", path):
            i = int(m.group(1))
            if i >= len(app.items) or app.submitted:
                return self.reply(404, "404\n")
            self.media(app.items[i])
        elif m := re.fullmatch(r"/o/([0-9]{1,9})", path):
            i = int(m.group(1))
            if i >= len(app.origs) or app.submitted:
                return self.reply(404, "404\n")
            o = app.origs[i]
            if not original_allowed(app.batch, o["path"]):  # re-checked: it may have moved since start-up
                return self.reply(404, "404\n")
            self.media(o)
        else:
            self.reply(404, "404\n")

    def media(self, it):
        p = it["path"]
        try:
            f = open(p, "rb")
        except OSError:
            return self.reply(404, "404\n")
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or (m.group(1) == m.group(2) == ""):
                    return self.reply(416, "", headers=[("Content-Range", f"bytes */{size}")])
                if m.group(1) == "":
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                if start >= size or start > end:
                    return self.reply(416, "", headers=[("Content-Range", f"bytes */{size}")])
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", it["mime"])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", "inline")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    b = f.read(min(CHUNK, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the video element dropped a range it no longer needs

    # ------------------------------------------------------------ POST
    def do_POST(self):
        g = self.guard()
        if not g:
            return
        path, _ = g
        app, p = self.app, self.app.port
        origin = self.headers.get("Origin")
        csrf = self.headers.get("X-CSRF", "")
        if (origin and origin not in (f"http://127.0.0.1:{p}", f"http://localhost:{p}")) \
                or not secrets.compare_digest(csrf.encode(), app.csrf.encode()):
            return self.reply(403, "403\n")
        if path != "/api/submit":
            return self.reply(404, "404\n")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.reply(415, "415\n")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 < n <= 1 << 20:
            return self.reply(400, "400\n")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self.json(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self.json(400, {"error": "bad json"})
        code, msg = app.submit(body)
        self.json(code, {"result" if code == 202 else "error": msg})


def serve(app, port=0):
    Handler.app = app
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"

    def stopper():
        app.finished.wait()
        app.done_seen.wait(timeout=60)  # let the page read the last lines, then go
        time.sleep(1)
        httpd.shutdown()

    threading.Thread(target=stopper, daemon=True).start()
    return httpd, url


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local-only review page for a batch's quarantine and review folder.")
    ap.add_argument("batch", nargs="?")
    ap.add_argument("--maybe-trash", action="store_true", help="add a tab of suggest-trash's library files")
    ap.add_argument("--phone", help="phone root, passed to erase")
    ap.add_argument("--drive", help="backup drive mount, passed to erase and verify-gone")
    ap.add_argument("--keep-phone-thumbnails", action="store_true", help="passed to erase")
    ap.add_argument("--no-wipe-free", action="store_true", help="passed to erase")
    ap.add_argument("--port", type=int, default=0, help="0 = a random free port (default)")
    a = ap.parse_args(argv)
    if a.batch is None and not a.maybe_trash:
        sys.exit("give a BATCH, --maybe-trash, or both")
    if a.batch is not None and not BATCH_RE.match(a.batch):
        sys.exit(f"bad batch name: {a.batch!r}")
    for flag, v in (("--phone", a.phone), ("--drive", a.drive)):
        if v and not os.path.isdir(v):
            sys.exit(f"{flag} {v}: not a directory")
    items = collect(a.batch) if a.batch else []
    origs = attach_originals(a.batch, items) if a.batch else []
    if a.maybe_trash:
        items += collect_maybe()
    if not items:
        print(f"nothing to review for {a.batch or 'maybe-trash'}")
        return 0
    app = App(a.batch, items, os.path.abspath(a.phone) if a.phone else None,
              os.path.abspath(a.drive) if a.drive else None, a.keep_phone_thumbnails, a.no_wipe_free, origs)
    httpd, url = serve(app, a.port)
    nq = sum(it["tab"] == "quarantine" for it in items)
    print(f"xdg-open '{url}'", flush=True)
    nm = sum(it["tab"] == "maybe" for it in items)
    print(f"({nq} quarantined, {len(items) - nq - nm} in review, {nm} maybe trash. A private browser window keeps it out of history. "
          "Ctrl-C quits without changing anything.)", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        if not app.submitted:
            print("stopped; nothing was changed")
        return 130
    finally:
        httpd.server_close()
    return 0 if app.ok else 1


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="csrf" content="__CSRF__">
<title>Media review</title>
<style>
:root{--bg:#fff;--fg:#111;--mut:#555;--line:#ccc;--card:#f6f6f6;--keep:#1b7a33;--erase:#b3261e;--trash:#8a5a00;--focus:#1a56db}
@media (prefers-color-scheme: dark){:root{--bg:#121212;--fg:#eee;--mut:#aaa;--line:#444;--card:#1e1e1e;--keep:#4cc26a;--erase:#ff6b5e;--trash:#e0a030;--focus:#7aa7ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:18px/1.4 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.tab{font:inherit;padding:10px 16px;border:2px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer}
.tab[aria-selected=true]{border-color:var(--focus)}
#count{font-weight:700;margin-left:auto}
#submit{font:inherit;font-weight:700;padding:10px 18px;border-radius:8px;border:2px solid var(--erase);background:var(--erase);color:#fff;cursor:pointer}
#submit:disabled{opacity:.4;cursor:not-allowed}
main{padding:16px;max-width:1100px;margin:0 auto}
.help{color:var(--mut);margin:0 0 12px}
.card{background:var(--card);border:3px solid transparent;border-radius:10px;padding:12px;margin:0 0 18px}
.card.focus{border-color:var(--focus)}
.card.keep{box-shadow:inset 8px 0 0 var(--keep)}
.card.erase{box-shadow:inset 8px 0 0 var(--erase)}
.card.trash{box-shadow:inset 8px 0 0 var(--trash)}
.media{display:flex;justify-content:center;background:#000;border-radius:6px;overflow:hidden}
.media img,.media video{max-width:100%;max-height:70vh;object-fit:contain;display:block}
.nopreview{color:#ddd;padding:40px 16px;text-align:center}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.pair .media img,.pair .media video{max-height:60vh}
.side h3{margin:0 0 6px;font-size:16px}
.meta{color:var(--mut);font-size:15px;margin-top:4px;word-break:break-all}
.note{background:var(--bg);border:1px dashed var(--line);border-radius:6px;padding:10px;margin-bottom:8px}
@media (max-width:640px){.pair{grid-template-columns:1fr}}
.name{font-weight:600;word-break:break-all;margin:8px 0 4px}
.labels span{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:2px 10px;margin:2px 4px 2px 0;font-size:15px}
.why{color:var(--mut)}
.btns{display:flex;gap:10px;margin-top:10px}
.btns button{flex:1;font:inherit;font-weight:700;padding:14px;border-radius:8px;border:2px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
.btns .k[aria-pressed=true]{background:var(--keep);border-color:var(--keep);color:#fff}
.btns .e[aria-pressed=true]{background:var(--erase);border-color:var(--erase);color:#fff}
.btns .t[aria-pressed=true]{background:var(--trash);border-color:var(--trash);color:#fff}
#confirm,#run{display:none}
.panel{background:var(--card);border-radius:10px;padding:16px;margin:16px 0}
.panel input{font:inherit;padding:10px;width:12em;max-width:100%;border:2px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.panel button{font:inherit;font-weight:700;padding:10px 18px;border-radius:8px;margin:8px 8px 0 0;cursor:pointer}
#go{background:var(--erase);color:#fff;border:2px solid var(--erase)}
#go:disabled{opacity:.4}
pre{white-space:pre-wrap;word-break:break-all;font-size:15px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:70vh;overflow:auto}
</style></head><body>
<header>
 <button class="tab" id="tq" aria-selected="true">Quarantine</button>
 <button class="tab" id="tr" aria-selected="false">Review</button>
 <button class="tab" id="tm" aria-selected="false" hidden>Maybe trash</button>
 <span id="count"></span>
 <button id="submit" disabled>Submit</button>
</header>
<main>
 <section id="confirm" class="panel">
  <h2>Confirm</h2><p id="summary"></p>
  <p>Type <b>ERASE</b> to run it. This cannot be undone.</p>
  <input id="typed" autocomplete="off" spellcheck="false" aria-label="Type ERASE">
  <div><button id="go" disabled>Run</button><button id="back">Back</button></div>
 </section>
 <section id="run" class="panel"><h2 id="runh">Running…</h2><pre id="out"></pre></section>
 <div id="lists">
  <p class="help" id="helpq">K = keep (back to the library), E = erase everywhere, T = trash (plain delete for junk that isn't private). ← → or ↑ ↓ move between items.</p>
  <p class="help" id="helpr" hidden>K = keep, E or D = delete (near-duplicates and junk), T = trash (delete, and never take it in again). ← → or ↑ ↓ move between items.</p>
  <p class="help" id="helpm" hidden>Suggested junk from your library. K = keep (never suggested again), T = trash. Nothing is deleted until you submit. ← → or ↑ ↓ move between items.</p>
  <div id="list"></div>
 </div>
</main>
<script>
"use strict";
const CSRF=document.querySelector('meta[name=csrf]').content;
let data=null,tab="quarantine",cur={quarantine:0,review:0,maybe:0};const dec={};
const $=s=>document.querySelector(s);
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function itemsOf(t){return data.items.filter(i=>i.tab===t)}
function mediaEl(kind,src,alt){const m=el("div","media");
 if(kind==="image"){const im=el("img");im.loading="lazy";im.alt=alt;im.src=src;m.append(im)}
 else if(kind==="video"){const v=el("video");v.controls=true;v.preload="metadata";v.src=src;m.append(v)}
 else m.append(el("div","nopreview","No preview in the browser for this format"));return m}
function metaEl(x){return el("div","meta",`${x.res} · ${x.size} · ${x.date} · ${x.folder}`)}
function side(title,m,x){const s=el("div","side");s.append(el("h3",null,title),m,metaEl(x));return s}
function second(it){return it.tab==="quarantine"?"erase":it.tab==="review"?"delete":null}
const TABS=["quarantine","review","maybe"],TID={quarantine:"#tq",review:"#tr",maybe:"#tm"},TNAME={quarantine:"Quarantine",review:"Review",maybe:"Maybe trash"};
function render(){
 const L=$("#list");L.replaceChildren();
 TABS.forEach(t=>$(TID[t]).setAttribute("aria-selected",tab===t));
 $("#helpq").hidden=tab!=="quarantine";$("#helpr").hidden=tab!=="review";$("#helpm").hidden=tab!=="maybe";
 const its=itemsOf(tab);
 if(!its.length)L.append(el("p","help","Nothing here."));
 its.forEach((it,n)=>{
  const c=el("div","card");c.id="c"+it.id;c.dataset.n=n;
  const m=mediaEl(it.kind,"/m/"+it.id,it.rel),o=it.orig;
  if(o&&o.label)c.append(el("div","note",o.label));
  if(o&&o.meta){const p=el("div","pair");
   p.append(side("Original (kept)",mediaEl(o.kind,"/o/"+o.id,"original"),o.meta),side("This copy",m,it.meta));c.append(p)}
  else{if(o&&o.erased)c.append(el("div","note","Near-copy of a photo you erased"+(o.date?" on "+o.date:"")));
   else if(o&&o.missing)c.append(el("div","note","Original not shown: "+o.missing));
   c.append(m);if(it.meta)c.append(metaEl(it.meta))}
  c.append(el("div","name",(it.batch!==data.batch?it.batch+" / ":"")+it.rel));
  if(it.reasons.length){const lb=el("div","labels");it.reasons.forEach(r=>lb.append(el("span",null,r)));c.append(lb)}
  if(it.why)c.append(el("div","why",it.why));
  if(it.tab==="quarantine"&&!it.releasable)c.append(el("div","why","No library place: Keep leaves it in quarantine and skips this batch's erase."));
  const b=el("div","btns"),k=el("button","k","Keep (K)"),t=el("button","t","Trash (T)");
  k.onclick=()=>{decide(it,"keep",n,false)};t.onclick=()=>{decide(it,"trash",n,false)};b.append(k);
  if(second(it)){const e=el("button","e",(it.tab==="quarantine"?"Erase":"Delete")+" (E)");e.onclick=()=>{decide(it,second(it),n,false)};b.append(e)}
  b.append(t);c.append(b);c.onclick=ev=>{if(ev.target.tagName!=="BUTTON"&&ev.target.tagName!=="VIDEO"){cur[tab]=n;paint(false)}};
  L.append(c);
 });
 paint(false);
}
function paint(scroll){
 itemsOf(tab).forEach((it,n)=>{const c=$("#c"+it.id);if(!c)return;
  const d=dec[it.id],e=c.querySelector(".e");
  c.classList.toggle("focus",n===cur[tab]);c.classList.toggle("keep",d==="keep");c.classList.toggle("erase",d==="erase"||d==="delete");c.classList.toggle("trash",d==="trash");
  c.querySelector(".k").setAttribute("aria-pressed",d==="keep");if(e)e.setAttribute("aria-pressed",d==="erase"||d==="delete");c.querySelector(".t").setAttribute("aria-pressed",d==="trash");
  if(scroll&&n===cur[tab])c.scrollIntoView({block:"center",behavior:"smooth"});});
 const left=data.items.filter(i=>!dec[i.id]).length,und=t=>itemsOf(t).filter(i=>!dec[i.id]).length;
 $("#count").textContent=left?`${left} undecided (`+TABS.filter(t=>itemsOf(t).length).map(t=>TNAME[t].toLowerCase()+" "+und(t)).join(", ")+")":"All decided";
 TABS.forEach(t=>{$(TID[t]).textContent=`${TNAME[t]} (${itemsOf(t).length})`});$("#tm").hidden=!itemsOf("maybe").length;
 $("#submit").disabled=left>0;
}
function decide(it,d,n,adv){dec[it.id]=d;cur[tab]=n;if(adv)cur[tab]=Math.min(n+1,itemsOf(tab).length-1);paint(adv)}
function move(d){const its=itemsOf(tab);if(!its.length)return;cur[tab]=Math.max(0,Math.min(its.length-1,cur[tab]+d));paint(true)}
function setTab(t){tab=t;render();window.scrollTo(0,0)}
TABS.forEach(t=>{$(TID[t]).onclick=()=>setTab(t)});
document.addEventListener("keydown",ev=>{
 if(!data||$("#lists").hidden||ev.target.tagName==="INPUT"||ev.ctrlKey||ev.metaKey||ev.altKey)return;
 const its=itemsOf(tab),it=its[cur[tab]];const k=ev.key.toLowerCase();
 if(k==="k"&&it){decide(it,"keep",cur[tab],true);ev.preventDefault()}
 else if((k==="e"||k==="d")&&it&&second(it)){decide(it,second(it),cur[tab],true);ev.preventDefault()}
 else if(k==="t"&&it){decide(it,"trash",cur[tab],true);ev.preventDefault()}
 else if(k==="arrowright"||k==="arrowdown"||k==="j"){move(1);ev.preventDefault()}
 else if(k==="arrowleft"||k==="arrowup"){move(-1);ev.preventDefault()}
});
$("#submit").onclick=()=>{
 const n=(t,d)=>itemsOf(t).filter(i=>dec[i.id]===d).length;
 const dv=[];if(data.devices.phone)dv.push("phone ("+data.devices.phone+")");if(data.devices.drive)dv.push("backup drive ("+data.devices.drive+")");
 const parts=[];
 if(itemsOf("quarantine").length)parts.push(`Quarantine: ${n("quarantine","keep")} keep / ${n("quarantine","erase")} erase / ${n("quarantine","trash")} trash; erased from this computer`+(dv.length?" and "+dv.join(" and "):" only (no phone or drive passed)"));
 if(itemsOf("review").length)parts.push(`Review: ${n("review","keep")} keep / ${n("review","delete")} delete / ${n("review","trash")} trash`);
 if(itemsOf("maybe").length)parts.push(`Maybe trash: ${n("maybe","keep")} keep / ${n("maybe","trash")} trash`);
 $("#summary").textContent=parts.join(". ")+(data.devices.phone?". Trashed items are also deleted from the phone (not xcheck copies).":".");
 $("#lists").hidden=true;$("#confirm").style.display="block";$("#submit").disabled=true;$("#typed").value="";$("#go").disabled=true;$("#typed").focus();
};
$("#back").onclick=()=>{$("#confirm").style.display="none";$("#lists").hidden=false;paint(false)};
$("#typed").oninput=()=>{$("#go").disabled=$("#typed").value!=="ERASE"};
$("#go").onclick=async()=>{
 $("#go").disabled=true;const d={};data.items.forEach(i=>d[i.id]=dec[i.id]);
 const r=await fetch("/api/submit",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF":CSRF},body:JSON.stringify({decisions:d,confirm:$("#typed").value})});
 if(r.status!==202){const j=await r.json().catch(()=>({}));alert("Refused: "+(j.error||r.status));$("#go").disabled=false;return}
 $("#confirm").style.display="none";$("#run").style.display="block";document.querySelectorAll("header button").forEach(b=>b.disabled=true);
 $("#list").replaceChildren();poll(0);
};
async function poll(since){
 let j;try{j=await (await fetch("/api/progress?since="+since,{credentials:"same-origin"})).json()}catch(e){$("#runh").textContent="Server stopped";return}
 if(j.lines.length)$("#out").textContent+=j.lines.join("\n")+"\n";$("#out").scrollTop=1e9;
 if(j.done){$("#runh").textContent=j.ok?"Done. You can close this tab.":"Finished with problems (see below). You can close this tab.";return}
 setTimeout(()=>poll(j.next),700);
}
fetch("/api/items",{credentials:"same-origin"}).then(r=>r.json()).then(j=>{data=j;tab=TABS.find(t=>itemsOf(t).length)||"quarantine";render()});
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
