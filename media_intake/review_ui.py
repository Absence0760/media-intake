#!/usr/bin/env python3
"""Local-only review page for a media-intake batch: quarantine + review folder.

  python3 -I media_intake/review_ui.py BATCH [--phone ROOT] [--drive DRIVE] [--port 0]
  python3 -I media_intake/review_ui.py [BATCH] --maybe-trash [--phone ROOT]   (after media-intake suggest-trash)

The quarantine can hold nudity and IDs, so this page never leaves the machine:
stdlib only, everything inline (no CDN, fonts or scripts from anywhere), bound
to 127.0.0.1 on a random port, a random token on every request, a Host check
against DNS rebinding, a CSRF value on every POST, no-store on every response.
Media are served as-is from the quarantine/review files by an opaque id (an
index into the list built at start-up), never by path; nothing is written to
disk except batches/<batch>/review-decisions.json (0600) and the run's job
files in <state>/jobs/ (jobs.py: dir 0700, files 0600). The run is an erase: its
paths and decisions reach the worker on a pipe, and once it ends no file name is
left in <state>/jobs/ (jobs.py redact).

A near-duplicate is shown beside its original (display only, its own opaque id
under /o/): the plan's `why` names it, and it is served only if it resolves to
a file under the library roots or this batch's raw/ or review/. A near-copy of
an erased photo (any level of quarantine/near-<batch>, near-near-<batch>, ...,
all reviewed on this one page) is shown beside the original its erased photo
was a copy of when one survives in the library (`root_of`), else gets a note
with the erase date; the erased file is never looked for. quarantine/xcheck-<batch> (quarantine-only: no
plan or manifest) holds messaging copies of library photos and videos; each entry's `of`
is shown as the original, same roots check, and its erase gets no --phone.
Review and Maybe trash items can also be trashed (`intake.py trash`: plain
delete for junk that isn't private, remembered so it never comes back).
Quarantined items can't: they are flagged sensitive, so they are erased or
kept (submit refuses a trash decision on one). --maybe-trash adds a tab of
library files `suggest-trash` listed (maybe-trash.json): Keep runs `not-trash`.
Bursts (bursts.py: one device, shots a moment apart) show as one stack per tab; "Keep this one, trash the
rest" only sets those decisions (Keep on one, Trash on the others; Erase in Quarantine, where every shot is
flagged), which go through the same submit.

On submit (every item decided, and ERASE typed when anything is removed) it starts a detached job
(jobs.py, key review-<batch>; one at a time per batch, taken with flock) that
runs, as subprocesses of the same Python: deletions of review items, then for
each near- level (deepest first), xcheck-<batch> and <batch>: `release` for kept files,
`erase --yes` for the rest, `verify-gone`. Closing the page or stopping the
server never stops it; the page polls the job's status and log, a restarted
server (or a second one) shows it again, and a worker that died is reported as
crashed. The server exits once the page has read the end. A page whose list is
older than the batch's last run (another page's run finished since it loaded) is
refused at submit. An assistant may start this; only you open the page.
"""
import argparse, datetime, hashlib, html, http.server, json, mimetypes, os, re, secrets, shutil, struct
import subprocess, sys, threading, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bursts  # noqa: E402  (burst groups: pure functions over date, device, dHash)
import config  # noqa: E402
import jobs  # noqa: E402  (detached, re-attachable runs: <state>/jobs/)
import library_lock  # noqa: E402  (plain_holder: who holds the library lock, in words)
import vlm_trash  # noqa: E402  (junk_score, REASON_WORDS: a verdict in plain words; stdlib only at import)

HOME = os.path.expanduser("~")
STATE = config.get()["state"]
BATCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
# What a browser can show; HEIC/HEIF is converted (CONVERT_IMG) when pillow-heif is there; anything else
# (DNG, GPR, or HEIC without pillow-heif) gets a "no preview" card.
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


# HEIC/HEIF (iPhone photos): browsers can't show them, so they are converted to JPEG in memory as they are
# served, never written to disk. Needs Pillow + pillow-heif: setup.sh's venv has both (requirements.txt),
# and main() re-runs this page under that venv's python when the running one lacks them.
CONVERT_IMG = {".heic", ".heif"}
PREVIEW_MAX = 1600
VENV_PY = os.path.join(STATE, "venv", "bin", "python")
try:
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False


def heic_preview(path):
    """JPEG bytes of a HEIC/HEIF, upright and at most PREVIEW_MAX px; None if it can't be decoded."""
    if not HEIF:
        return None
    import io
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def heic_size(path):
    """(w, h) of a HEIC/HEIF, or None (no pillow-heif, or not decodable)."""
    if not HEIF:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in CONVERT_IMG and HEIF:
        return "image", "image/jpeg"
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
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def meta(path, kind, date=None):
    """What you compare: resolution, size, date, folder (relative to HOME)."""
    if os.path.splitext(path)[1].lower() in CONVERT_IMG:
        wh = heic_size(path)
    else:
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


MESSAGING_APP = re.compile(r"(?:^|/)Messaging/([A-Za-z0-9][A-Za-z0-9 ._-]{0,63})/")


def messaging_app(it):
    """'Telegram' for an xcheck copy whose rel is under Messaging/Telegram/ in the library, else 'Messaging'."""
    m = MESSAGING_APP.search(it["rel"].replace(os.sep, "/")) if isinstance(it.get("rel"), str) else None
    return m.group(1) if m else "Messaging"


def attach_originals(batch, items):
    """Give every near-duplicate an `orig` (display only). Returns the served originals list."""
    plan = {p["rel"]: p for p in load(os.path.join(STATE, "batches", batch, "plan.json"), []) or []}
    quar_own = load(os.path.join(STATE, "quarantine", batch, "quarantine.json"), {})
    b = os.path.join(STATE, "batches", batch)
    origs = []
    for it in items:
        it["orig"], it["meta"] = None, None
        if (it["qbatch"] or "").startswith("near-"):  # any level of a near chain: near-, near-near-, ...
            v = load(os.path.join(STATE, "quarantine", it["qbatch"], "quarantine.json"), {}).get(it["rel"], {})
            it["meta"] = meta(it["path"], it["kind"])
            date = v.get("erased_on") if isinstance(v.get("erased_on"), str) else None
            root = v.get("root_of")  # the surviving original the erased photo was a copy of, if any
            if isinstance(root, str) and os.path.isabs(root) and os.path.isfile(root) and original_allowed(batch, root):
                k, mime = kind_of(root)
                origs.append(dict(path=root, kind=k, mime=mime))
                it["orig"] = dict(id=len(origs) - 1, kind=k, meta=meta(root, k),
                                  label="Near-copy of a photo you erased" + (f" on {date}" if date else "")
                                  + "; the original it was copied from is still in your library")
            else:
                it["orig"] = dict(erased=True, date=date)
            continue
        why = it["why"] or ""
        if it.get("of"):  # xcheck-<batch>: a messaging copy of a library photo or video
            it["meta"] = meta(it["path"], it["kind"])
            of = it["of"]
            label = messaging_app(it) + " copy of this " + ("video" if it["kind"] == "video" else "photo")
            if not (isinstance(of, str) and os.path.isabs(of) and original_allowed(batch, of)):
                it["orig"] = dict(missing="the original was not found in the library", label=label)
                continue
            k, mime = kind_of(of)
            origs.append(dict(path=of, kind=k, mime=mime))
            it["orig"] = dict(id=len(origs) - 1, kind=k, meta=meta(of, k), label=label)
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


def near_chain(root):
    """The near-duplicate chain of quarantine/<root> on disk, deepest level first: near-near-<root>, near-<root>.
    Erasing a level writes the next one down (intake.py near_dups, up to its NEAR_DEPTH), so the deepest goes first."""
    try:
        names = os.listdir(os.path.join(STATE, "quarantine"))
    except FileNotFoundError:
        return []
    chain = []
    for n in names:
        b, k = n, 0
        while b.startswith("near-"):
            b, k = b[len("near-"):], k + 1
        if k and b == root and os.path.isdir(os.path.join(STATE, "quarantine", n)):
            chain.append((k, n))
    return [n for _, n in sorted(chain, reverse=True)]


def sub_batches(batch):
    """Quarantine batches reviewed together, in run order: every level of the near-copy chains first, deepest
    first (erasing any batch can write a fresh near-<it>, so each level runs before the one that feeds it),
    then messaging cross-check copies, then the batch. None (a maybe-trash-only page) has none."""
    if not batch:
        return []
    return [*near_chain(batch), *near_chain(f"xcheck-{batch}"), f"xcheck-{batch}", batch]


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
        labels = [str(t) for t in x.get("labels") or [] if isinstance(t, str)]
        items.append(dict(tab="maybe", qbatch=None, rel=rel, path=p, why="", kind=k, mime=mime, releasable=True,
                          reasons=maybe_reasons(labels)))
    return items


def maybe_reasons(labels):
    """A Maybe trash item's labels as the page shows them: the vision check's verdict tags (AI|vlm/<model>/...,
    Review|vlm-*) folded into one "Junk score 0.90 (blurry)" (docs/words.md, the gallery's own wording); every
    other label as it is (the page's FLAG / GROUP give it plain words), best-shot's AI|bestshot/not-best as
    "Not the best shot" (its cover tag is no reason and is left out)."""
    score, why = vlm_trash.junk_score(labels)
    rest = [("Not the best shot" if t == "AI|bestshot/not-best" else t) for t in labels
            if not t.startswith(("AI|vlm/", "Review|vlm-")) and t != "AI|bestshot/cover"]
    if score is None:
        return rest
    word = vlm_trash.REASON_WORDS.get(why or "", (why or "").replace("-", " ")).lower()  # as the gallery's tile
    return [f"Junk score {score:.2f}" + (f" ({word})" if word else "")] + rest


def attach_bursts(batch, items):
    """Give every item `burst`: a group number shared by the shots of one burst (bursts.py), else None.
    Groups never cross a tab or a quarantine batch (each runs its own steps). The date, device and dHash
    come from plan.json (this batch's quarantine and review items by rel; a maybe-trash library file by
    its dst in the plan of maybe-trash.json's batch or this one), the dHash of a library file also from
    index.json. near- and xcheck- items have no plan entry and are never grouped. No file is opened."""
    plans = []
    mt = load(os.path.join(STATE, "maybe-trash.json"), {}) or {}
    for b in dict.fromkeys(x for x in (batch, mt.get("batch") if isinstance(mt, dict) else None)
                           if isinstance(x, str) and BATCH_RE.match(x)):
        plans.append((b, [p for p in load(os.path.join(STATE, "batches", b, "plan.json"), []) or []
                          if isinstance(p, dict)]))
    by_rel = {p.get("rel"): p for b, pl in plans if b == batch for p in pl}
    by_dst = {p.get("dst"): p for _, pl in plans for p in pl if isinstance(p.get("dst"), str)}
    index = load(os.path.join(STATE, "index.json"), {}) or {}
    recs = {}
    for n, it in enumerate(items):
        it["burst"] = None
        if it["tab"] == "maybe":
            p = by_dst.get(it["path"])
        elif it["tab"] == "review" or it["qbatch"] == batch:
            p = by_rel.get(it["rel"])
        else:
            continue
        if not p:
            continue
        dh = p.get("dhash")
        if not isinstance(dh, int) and it["tab"] == "maybe":
            dh = (index.get(it["path"]) or {}).get("dhash") if isinstance(index, dict) else None
        recs.setdefault((it["tab"], it["qbatch"]), []).append(
            dict(path=it["path"], datetime=p.get("date"), device=p.get("device"),
                 dhash=dh if isinstance(dh, int) else None, burst_id=p.get("burst_id"), n=n))
    gid = 0
    for rs in recs.values():
        for g in bursts.groups(rs):
            for r in g:
                items[r["n"]]["burst"] = gid
            gid += 1
    return gid


def busy_words(e):
    """Plain words for a refused submit (jobs.Busy): what is in the way and what to do. The exception's own text
    can name a PID and the lock file's path, so it never reaches the page (the gallery's busy_text does the same)."""
    if isinstance(e, jobs.LibraryBusy):
        who = library_lock.plain_holder(e.holder) or "an import, a gallery Trash or Erase, or another review"
        return f"Another action is using the library ({who}). Wait for it to finish, then apply again."
    return "A run of this review is still going. Wait for it to finish, then open the review again."


def job_key(batch):
    """One job per batch (its quarantine, near- and xcheck- batches, review folder); one for maybe-trash alone."""
    return f"review-{batch}" if batch else "maybe-trash"


class Runner:
    """Runs the decisions inside the detached job worker (jobs.py run -> run_job)."""

    def __init__(self, job, spec):
        self.job = job
        self.batch = spec["batch"]
        self.items = spec["items"]  # [{tab, qbatch, rel, path}], the server's list, same order
        self.dec = {int(k): v for k, v in spec["decisions"].items()}
        self.phone, self.drive = spec.get("phone"), spec.get("drive")
        self.keep_phone_thumbnails, self.no_wipe_free = spec.get("keep_phone_thumbnails"), spec.get("no_wipe_free")

    def log(self, s, safe=True):
        """The runner's own lines: counts, batch names and its decisions file, never a file name (safe)."""
        self.job.log(s, safe=safe)

    def run(self, args, label):
        return self.job.run(args, label)  # a redact job: jobs.py renders the command line without paths

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

    def execute(self):
        ok = True
        try:
            ok = self._execute(self.dec)
        except Exception as e:  # report, never leave the page hanging
            self.log(f"error: {type(e).__name__}: {e}", safe=False)  # an OSError names its file
            ok = False
        where = f"batches/{self.batch}/review-decisions.json" if self.batch else "maybe-trash-decisions.json"
        self.log("done: " + ("all steps succeeded" if ok else f"something failed, see above; decisions are in {where}"))
        return ok

    def phone_args(self, qb=None):
        return ["--phone", self.phone] if self.phone and not (qb or "").startswith("xcheck-") else []

    def _execute(self, dec):
        ok = True
        # 0. Maybe-trash suggestions. Keep: `not-trash` (Owner|keep + Owner|not-trash, as the gallery's
        # Keep). Trash: `trash` (sha256 into trashed.json, the durable Owner|trash; copies marked).
        mb = [(i, it) for i, it in enumerate(self.items) if it["tab"] == "maybe"]
        keep = [it["path"] for i, it in mb if dec[i] == "keep"]
        trash = [it["path"] for i, it in mb if dec[i] == "trash"]
        if keep:
            ok &= self.run(["not-trash"] + keep, "not-trash") == 0
        if trash:
            ok &= self.run(["trash"] + trash + self.phone_args() + ["--yes"], "trash") == 0
        rv_items = [(i, it) for i, it in enumerate(self.items) if it["tab"] == "review"]
        trash = [it["path"] for i, it in rv_items if dec[i] == "trash"]
        if trash:
            ok &= self.run(["trash", "--batch", self.batch] + trash + self.phone_args() + ["--yes"], "trash review") == 0
            rv_items = [(i, it) for i, it in rv_items if dec[i] != "trash"]
        # 1. Review folder first: erase's near-duplicate search may move review files.
        if rv_items:
            kept = [it for i, it in rv_items if dec[i] == "keep"]
            gone = [it for i, it in rv_items if dec[i] == "delete"]
            if gone and not kept:
                ok &= self.run(["purge-review", self.batch, "--yes"], "purge-review") == 0
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
        # 2. Quarantine: the near chains deepest first, then xcheck-, then the batch (erasing a batch can
        # write a fresh near-<it>, which then waits for the next review).
        for qb in sub_batches(self.batch):
            q_items = [(i, it) for i, it in enumerate(self.items) if it["tab"] == "quarantine" and it["qbatch"] == qb]
            if not q_items:
                continue
            quar = os.path.join(STATE, "quarantine", qb)
            kept = [it for i, it in q_items if dec[i] == "keep"]
            erase = [it for i, it in q_items if dec[i] == "erase"]
            if len(kept) + len(erase) != len(q_items):  # submit refuses these; never trash a flagged file
                self.log(f"{qb}: a quarantined item has a decision other than keep or erase; stopping")
                return False
            self.log(f"{qb}: keep {len(kept)}, erase {len(erase)}")
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
        # Near-copies this run's erases found (and any kept file that could not be released): one review of
        # the batch shows every level of its chains.
        for qb in sub_batches(self.batch):
            q = load(os.path.join(STATE, "quarantine", qb, "quarantine.json"), {}) if qb.startswith("near-") else None
            if q:
                self.log(f"{len(q)} near-duplicates now in quarantine/{qb}: review {self.batch} again to decide them")
        return ok



def run_job(job):
    """jobs.py's entry point for runner "review_ui"."""
    return Runner(job, job.spec).execute()


# Decisions each tab takes. Quarantine is Erase-only (no plain trash): its items are flagged sensitive, and only
# erase sweeps their copies (messaging copies, near-copies, the phone and drive) and logs the erase.
ALLOWED = {"quarantine": ("keep", "erase"), "review": ("keep", "delete", "trash"), "maybe": ("keep", "trash")}


class App:
    def __init__(self, batch, items, phone, drive, keep_phone_thumbnails=False, no_wipe_free=False, origs=()):
        self.batch, self.items = batch, items
        self.origs = list(origs)  # display-only originals, served by id under /o/
        self.phone, self.drive = phone, drive
        self.keep_phone_thumbnails, self.no_wipe_free = keep_phone_thumbnails, no_wipe_free
        self.token = secrets.token_urlsafe(24)
        self.csrf = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.key = job_key(batch)
        self.job_id = None  # the job this server shows (started here, or found running)
        self.tail = None  # its log, followed through an open descriptor (jobs.Tail)
        self.ok = None
        self.done_seen = threading.Event()
        self.finished = threading.Event()
        self.port = None
        st = self.current()
        # The batch's last run when this list was read. A run that ends after this (from another page) can
        # change what is on disk, e.g. write a fresh near-<batch>, so this page may no longer submit.
        self.loaded_id = st.get("id") if st else None

    @property
    def submitted(self):
        return self.job_id is not None

    # ------------------------------------------------------------ the job (jobs.py)
    def current(self):
        """The key's job status; a live job started elsewhere (another page or server) is adopted."""
        st = jobs.status(self.key)
        with self.lock:
            if st and not st["done"] and self.job_id is None:
                self._watch(st["id"])
        return st

    def _watch(self, jid, tail=None):
        self.job_id = jid
        self.tail = tail or jobs.Tail(self.key, jid)
        threading.Thread(target=self._wait, args=(jid,), daemon=True).start()

    def _wait(self, jid):
        st = jobs.wait(self.key, jid)
        self.ok = bool(st and st.get("id") == jid and st.get("ok"))
        self.finished.set()

    def progress(self, since):
        st = jobs.status(self.key) if self.job_id else None
        if not st or st.get("id") != self.job_id:
            gone = self.job_id is not None
            return dict(lines=[], next=since, done=gone, ok=None if not gone else False,
                        job=jobs.public(st), replaced=gone)
        ls, nxt = self.tail.lines(since, st["done"])  # after the status: a done job has written every line
        if st["done"] and nxt <= since + len(ls):
            self.done_seen.set()
        return dict(lines=ls, next=nxt, done=st["done"], ok=st.get("ok"), job=jobs.public(st))

    # ------------------------------------------------------------ submit
    def submit(self, body):
        """Validate and start. Returns (http code, message)."""
        raw = body.get("decisions")
        if not isinstance(raw, dict):
            return 400, "no decisions"
        dec = {}
        for i, it in enumerate(self.items):
            d = raw.get(str(i))
            if it["tab"] == "quarantine" and d == "trash":
                # Plain trash moves one file and leaves its messaging copies and near-copies: a flagged
                # (sensitive) item is erased everywhere or kept, never trashed. Refused before anything is written.
                return 400, "a quarantined item can't be trashed: erase or keep it"
            if d not in ALLOWED[it["tab"]]:
                return 409, "every item needs a decision"
            dec[i] = d
        if set(raw) - {str(i) for i in range(len(self.items))}:
            return 400, "unknown item"
        # Typing ERASE is asked for only when something is removed (erase, trash, delete). An all-keep submit
        # deletes nothing, so it needs no confirm word; checked here, after every decision is known and valid.
        if any(d != "keep" for d in dec.values()) and body.get("confirm") != "ERASE":
            return 400, "type ERASE to confirm"
        if self.current() and self.submitted:
            return 409, "already submitted"
        with self.lock:
            if self.submitted:
                return 409, "already submitted"
            try:
                claim = jobs.claim(self.key)  # atomic: a second submit for this batch is refused here
            except jobs.Busy as e:  # this batch's own job, or the library lock held by another writer
                return 409, busy_words(e)
            with claim:
                # Under the claim no run can start or end, so this is the last word on staleness.
                st = jobs.status(self.key)
                if (st.get("id") if st else None) != self.loaded_id:
                    return 409, "a run finished since this page loaded; open the review again"
                self._start(claim, dec)
        return 202, "started"

    def _start(self, claim, dec):
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
        # Paths and decisions go to the worker on a pipe, never into <state>/jobs/ (the run erases).
        private = dict(batch=self.batch, decisions={str(i): d for i, d in dec.items()},
                       items=[dict(tab=it["tab"], qbatch=it["qbatch"], rel=it["rel"], path=it["path"])
                              for it in self.items],
                       phone=self.phone, drive=self.drive, keep_phone_thumbnails=self.keep_phone_thumbnails,
                       no_wipe_free=self.no_wipe_free)
        st = claim.spawn("review_ui", dict(batch=self.batch, items=len(self.items)), private=private, redact=True)
        self._watch(st["id"], claim.tail)

    def public_items(self):
        return [dict(id=i, tab=it["tab"], batch=it["qbatch"] or self.batch, rel=it["rel"], reasons=it["reasons"],
                     why=it["why"], kind=it["kind"], releasable=it["releasable"],
                     orig=it.get("orig"), meta=it.get("meta"), burst=it.get("burst"))
                for i, it in enumerate(self.items)]


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
            st = app.current()
            self.json(200, {"batch": app.batch, "items": app.public_items(), "devices": dev,
                            "job": jobs.public(st), "watching": app.submitted})
        elif path == "/api/job":  # cheap check the page makes while you decide: did a run start elsewhere?
            st = app.current()
            self.json(200, {"job": jobs.public(st), "watching": app.submitted})
        elif path == "/api/progress":
            try:
                since = max(0, int(q.get("since", ["0"])[0]))
            except ValueError:
                since = 0
            self.json(200, app.progress(since))
        elif m := re.fullmatch(r"/m/([0-9]{1,9})", path):
            i = int(m.group(1))
            if i >= len(app.items) or app.submitted:
                self.reply(404, "404\n")
                return
            self.media(app.items[i])
        elif m := re.fullmatch(r"/o/([0-9]{1,9})", path):
            i = int(m.group(1))
            if i >= len(app.origs) or app.submitted:
                self.reply(404, "404\n")
                return
            o = app.origs[i]
            if not original_allowed(app.batch, o["path"]):  # re-checked: it may have moved since start-up
                self.reply(404, "404\n")
                return
            self.media(o)
        else:
            self.reply(404, "404\n")

    def converted(self, p):
        """A HEIC/HEIF as a JPEG made in memory (no Range: the bytes exist only for this reply)."""
        data = heic_preview(p)
        if data is None:
            return self.reply(404, "404\n")
        try:
            self.reply(200, data, "image/jpeg", headers=[("Content-Disposition", "inline")])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def media(self, it):
        p = it["path"]
        if os.path.splitext(p)[1].lower() in CONVERT_IMG:
            return self.converted(p)
        try:
            f = open(p, "rb")
        except OSError:
            self.reply(404, "404\n")
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or (m.group(1) == m.group(2) == ""):
                    self.reply(416, "", headers=[("Content-Range", f"bytes */{size}")])
                    return
                if m.group(1) == "":
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                if start >= size or start > end:
                    self.reply(416, "", headers=[("Content-Range", f"bytes */{size}")])
                    return
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
            self.reply(403, "403\n")
            return
        if path != "/api/submit":
            self.reply(404, "404\n")
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.reply(415, "415\n")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 < n <= 1 << 20:
            self.reply(400, "400\n")
            return
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            self.json(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self.json(400, {"error": "bad json"})
            return
        code, msg = app.submit(body)
        self.json(code, {"result" if code == 202 else "error": msg, "watching": app.submitted})


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


def venv_reexec(args, script=None):
    """Without pillow-heif here (the system python), re-run SCRIPT (default: this page; the gallery passes its
    own) with ARGS under setup.sh's venv so HEIC shows. The venv reuses the system packages
    (--system-site-packages), so everything else the page imports is there too. MEDIA_REVIEW_NO_VENV=1 turns
    this off (for the review page and the gallery); without a venv the page runs as it is."""
    if HEIF or os.environ.get("MEDIA_REVIEW_NO_VENV") or not os.access(VENV_PY, os.X_OK):
        return
    if os.path.realpath(sys.prefix) == os.path.realpath(os.path.dirname(os.path.dirname(VENV_PY))):
        return  # already the venv: it lacks pillow-heif (re-run setup.sh), so no HEIC previews
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(VENV_PY, [VENV_PY, "-I", os.path.abspath(script or __file__), *args])


def main(argv=None):
    def port(v):
        if not v.isdigit() or int(v) > 65535:
            raise argparse.ArgumentTypeError(f"{v}: a port is 0-65535 (0 picks a free one)")
        return int(v)

    ap = argparse.ArgumentParser(
        prog="media-intake review", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="A local page (127.0.0.1 only: open the link it prints) to decide on what an\n"
                    "import set aside:\n"
                    "  Quarantine tab: flagged files. Keep or Erase.\n"
                    "  Review tab: the review folder's near-duplicates and junk. Keep, Delete this\n"
                    "    copy or Trash.\n"
                    "  Maybe trash tab (with --maybe-trash): Suggested junk from the library. Keep\n"
                    "    or Trash.\n"
                    "Your choices are only marks until you apply them (Apply N decisions; you type\n"
                    "ERASE when anything is removed).\n\n"
                    "Touches: quarantine, the review folder, the library, and the phone and backup\n"
                    "  drive you name.\n"
                    "Deletes: only what you mark Erase, Delete this copy or Trash, once you apply.")
    ap.add_argument("batch", nargs="?", metavar="BATCH", help="the batch name: the one `stage` printed (leave it "
                    "out with --maybe-trash)")
    ap.add_argument("--maybe-trash", action="store_true", help="add the Maybe trash tab: the Suggested junk "
                    "that suggest-trash found in the library")
    ap.add_argument("--phone", metavar="PHONE", help="the phone or camera the batch came from (its folder, e.g. "
                    "under /run/user/$UID/gvfs/): Erase and Trash also delete the copies there")
    ap.add_argument("--drive", metavar="DRIVE", help="the backup drive: the folder it is mounted on, e.g. "
                    "/run/media/$USER/<label>. Erase also deletes the copies there")
    ap.add_argument("--keep-phone-thumbnails", action="store_true", help="with --phone: leave the phone's "
                    "DCIM/.thumbnails and Pictures/.thumbnails (Erase removes them by default)")
    ap.add_argument("--no-wipe-free", action="store_true", help="with --drive: don't overwrite the backup "
                    "drive's free space after an Erase")
    ap.add_argument("--port", type=port, default=0, help="the page's port (default 0: a free one)")
    a = ap.parse_args(argv)
    venv_reexec(sys.argv[1:] if argv is None else list(argv))
    if a.batch is None and not a.maybe_trash:
        sys.exit("give a BATCH, --maybe-trash, or both")
    if a.batch is not None and not BATCH_RE.match(a.batch):
        sys.exit(f"bad batch name: {a.batch!r}")
    for flag, v in (("--phone", a.phone), ("--drive", a.drive)):
        if v and not os.path.isdir(v):
            sys.exit(f"{flag} {v}: not a directory")
    jobs.sweep()  # a run that died while no page watched: marked crashed, its output scrubbed
    items = collect(a.batch) if a.batch else []
    origs = attach_originals(a.batch, items) if a.batch else []
    if a.maybe_trash:
        items += collect_maybe()
    attach_bursts(a.batch, items)
    app = App(a.batch, items, os.path.abspath(a.phone) if a.phone else None,
              os.path.abspath(a.drive) if a.drive else None, a.keep_phone_thumbnails, a.no_wipe_free, origs)
    if not items and not app.submitted:  # a run still going is shown even when nothing is left to decide
        print(f"nothing to review for {a.batch or 'maybe-trash'}")
        return 0
    try:
        httpd, url = serve(app, a.port)
    except OSError as e:  # the port is taken, or not ours to use
        sys.exit(f"port {a.port} can't be used ({e.strerror or e}): pick another with --port, or leave it out")
    nq = sum(it["tab"] == "quarantine" for it in items)
    print(f"xdg-open '{url}'", flush=True)
    nm = sum(it["tab"] == "maybe" for it in items)
    if app.submitted:
        print(f"(a run for {a.batch or 'maybe-trash'} is still going; the page shows its progress. "
              "A private browser window keeps it out of history. Ctrl-C stops only the page server.)", flush=True)
    else:
        print(f"({nq} quarantined, {len(items) - nq - nm} in review, {nm} maybe trash. A private browser window "
              "keeps it out of history. Ctrl-C quits without changing anything.)", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        if not app.submitted:
            print("stopped; nothing was changed")
        elif not app.finished.is_set():
            print(f"page server stopped; the run keeps going on its own (<state>/jobs/{app.key}.json). "
                  "Run the same review command again to watch it.")
        return 130
    finally:
        httpd.server_close()
    return 0 if app.ok else 1


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="csrf" content="__CSRF__">
<meta name="color-scheme" content="light dark">
<title>Media review</title>
<style>
:root{--bg:#fff;--fg:#111;--mut:#555;--line:#ccc;--card:#f6f6f6;--keep:#1b7a33;--erase:#b3261e;--trash:#8a5a00;--focus:#1a56db;--on:#fff}
@media (prefers-color-scheme: dark){:root{--bg:#121212;--fg:#eee;--mut:#aaa;--line:#444;--card:#1e1e1e;--keep:#4cc26a;--erase:#ff6b5e;--trash:#e0a030;--focus:#7aa7ff;--on:#111}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:18px/1.4 system-ui,sans-serif}
/* The header: the batch name, the tabs, then one bar (the one counter, which also says why Submit is locked,
   Keys and Submit). On a phone only that bar stays pinned: placeHeader() sets a negative top so the rows above
   it scroll away. */
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center}
header h1{flex:1 0 100%;margin:0;font-size:18px;overflow-wrap:anywhere}
.tab{font:inherit;padding:10px 16px;border:2px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer;white-space:nowrap}
.tab[aria-selected=true]{border-color:var(--focus)}
#bar{display:flex;gap:10px;align-items:center;margin-left:auto}
#done{font-weight:700}
button,#typed{min-height:44px;min-width:44px}
#submit{font:inherit;font-weight:700;padding:10px 18px;border-radius:8px;border:2px solid var(--erase);background:var(--erase);color:var(--on);cursor:pointer}
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
.meta{color:var(--mut);font-size:15px;margin-top:4px;overflow-wrap:anywhere}
.note{background:var(--bg);border:1px dashed var(--line);border-radius:6px;padding:10px;margin-bottom:8px}
@media (max-width:640px){.pair{grid-template-columns:1fr}}
.name{font-weight:600;overflow-wrap:anywhere;margin:8px 0 4px}
.verdict{font-weight:700;margin:8px 0 0}
.verdict[hidden]{display:none}
.labels span{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:2px 10px;margin:2px 4px 2px 0;font-size:15px}
.why{color:var(--mut)}
.notrash{flex:1;align-self:center;text-align:center;color:var(--mut);font-size:15px}
.btns{display:flex;gap:10px;margin-top:10px}
.btns button{flex:1;font:inherit;font-weight:700;padding:14px;border-radius:8px;border:2px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
.btns .k[aria-pressed=true]{background:var(--keep);border-color:var(--keep);color:var(--on)}
.btns .e[aria-pressed=true]{background:var(--erase);border-color:var(--erase);color:var(--on)}
.btns .t[aria-pressed=true]{background:var(--trash);border-color:var(--trash);color:var(--on)}
#run{display:none}
.panel{background:var(--card);border-radius:10px;padding:16px;margin:16px 0}
.panel input{font:inherit;padding:10px;width:12em;max-width:100%;border:2px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.panel button{font:inherit;font-weight:700;padding:10px 18px;border-radius:8px;margin:8px 8px 0 0;cursor:pointer}
#go{background:var(--erase);color:var(--on);border:2px solid var(--erase)}
#go:disabled{opacity:.4}
.card.stack{box-shadow:6px 6px 0 -2px var(--card),6px 6px 0 0 var(--line),12px 12px 0 -2px var(--card),12px 12px 0 0 var(--line)}
.card.stack.keep{box-shadow:inset 8px 0 0 var(--keep),6px 6px 0 -2px var(--card),6px 6px 0 0 var(--line)}
.card.stack.erase{box-shadow:inset 8px 0 0 var(--erase),6px 6px 0 -2px var(--card),6px 6px 0 0 var(--line)}
.card.stack.trash{box-shadow:inset 8px 0 0 var(--trash),6px 6px 0 -2px var(--card),6px 6px 0 0 var(--line)}
.burst{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px}
.badge{font-weight:700;background:var(--focus);color:var(--on);border-radius:999px;padding:2px 12px;font-size:15px}
.burst button,.one{font:inherit;font-size:15px;padding:6px 12px;border-radius:8px;border:2px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
.one{width:100%;margin-top:10px;font-weight:700}
pre{white-space:pre-wrap;word-break:break-all;font-size:15px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:70vh;overflow:auto}
/* Keyboard focus: one outline in --focus (6:1 or more on the page and the cards, light and dark). */
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.card:focus-visible{outline-offset:3px}
.card:focus:not(:focus-visible){outline:none}
#tabs{display:flex;flex-wrap:wrap;gap:10px}
#summary ul{margin:0 0 8px;padding-left:1.2em}
#summary p{margin:0 0 8px}
#keysb{font:inherit;padding:10px 14px;border-radius:8px;border:2px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.modal{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:16px}
.modal.on{display:flex}
.modal .box{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:16px 20px;width:100%;max-width:640px;max-height:90vh;overflow:auto;margin:0}
.modal h2{margin-top:0}
.keys{border-collapse:collapse;width:100%;font-size:16px}
.keys th,.keys td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line)}
kbd{font:inherit;font-weight:700;border:1px solid var(--line);border-radius:4px;padding:0 6px;background:var(--card);white-space:nowrap}
#keysx{font:inherit;font-weight:700;padding:10px 18px;border-radius:8px;margin-top:12px;border:2px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.banner{display:flex;gap:12px;align-items:center;background:var(--card);color:var(--fg);border:2px solid var(--erase);border-left-width:8px;border-radius:8px;padding:10px 12px;margin:0 0 12px}
.banner[hidden]{display:none}
.banner span{flex:1;overflow-wrap:anywhere}
.banner button{font:inherit;font-weight:700;padding:6px 12px;border-radius:8px;border:2px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer;margin:0}
#err{scroll-margin-top:calc(var(--pin,0px) + 8px)}
.card{scroll-margin-top:calc(var(--pin,0px) + 8px)}
/* Phone: a compact header (only its bar stays pinned), tabs on one scrolling row. */
@media (max-width:640px){
 header{padding:8px 12px}
 header h1{font-size:16px}
 #tabs{flex-wrap:nowrap;overflow-x:auto;width:100%}
 .tab{font-size:16px;padding:8px 12px}
 #bar{width:100%;margin:0}
 #done{flex:1;font-size:15px}
 main{padding:12px}
}
/* No keyboard: the key hints and the Keys button go. */
@media (hover:none) and (pointer:coarse){.kb,#keysb{display:none}}
</style></head><body>
<header>
 <h1 id="title">Review</h1>
 <div id="tabs" role="tablist" aria-label="Lists">
  <button type="button" class="tab" role="tab" id="tq" aria-selected="true" aria-controls="lists">Quarantine</button>
  <button type="button" class="tab" role="tab" id="tr" aria-selected="false" aria-controls="lists" tabindex="-1">Review</button>
  <button type="button" class="tab" role="tab" id="tm" aria-selected="false" aria-controls="lists" tabindex="-1" hidden>Maybe trash</button>
 </div>
 <div id="bar">
  <span id="done" role="status"></span>
  <button type="button" id="keysb" aria-haspopup="dialog">Keys (?)</button>
  <button type="button" id="submit" aria-describedby="done" disabled>Submit</button>
 </div>
</header>
<main>
 <div id="err" class="banner" role="alert" hidden><span id="errmsg"></span><button type="button" id="errx">Dismiss</button></div>
 <section id="run" class="panel"><div aria-live="polite" aria-atomic="true"><h2 id="runh" tabindex="-1">Running…</h2><p id="runs" class="help"></p></div>
  <pre id="out" role="log" aria-live="polite" aria-label="Run output" tabindex="0"></pre></section>
 <div id="lists" role="tabpanel" aria-labelledby="tq">
  <p class="note" id="last" hidden></p>
  <p class="help" id="helpq">These were flagged as possibly private. Keep puts one back in your library; Erase removes every copy. No Trash here: only Erase removes the copies too. <span class="kb">Keys: K keep, E erase, ← → or ↑ ↓ move.</span></p>
  <p class="help" id="helpb" hidden>A stack is a burst (shots from one camera a moment apart). "Show all" opens it<span class="kb"> (or X)</span>, and "Keep this one" on a shot decides the whole stack: the rest are erased in Quarantine, trashed elsewhere.</p>
  <p class="help" id="helpr" hidden>Near-duplicates and junk set aside at import. Keep leaves it in the review folder for now. Delete this copy removes only this file. Trash removes it and skips the same photo if it is imported again. <span class="kb">Keys: K keep, D delete this copy, T trash, ← → or ↑ ↓ move.</span></p>
  <p class="help" id="helpm" hidden>Suggested junk from your library. Keep leaves it in the library and stops suggesting it. Trash removes it and skips the same photo if it is imported again. <span class="kb">Keys: K keep, T trash, ← → or ↑ ↓ move.</span></p>
  <p class="help" id="helpall">Your choices are only marks: nothing changes until you submit. Press a pressed button again to clear it.</p>
  <div id="list"></div>
 </div>
</main>
<div id="confirm" class="modal" role="dialog" aria-modal="true" aria-labelledby="confirmh" aria-describedby="summary">
 <section class="panel box">
  <h2 id="confirmh">Apply your decisions?</h2><div id="summary"></div>
  <div id="typedbox"><p>Type <b>ERASE</b> to apply them.</p>
  <input id="typed" autocomplete="off" spellcheck="false" aria-label="Type ERASE"></div>
  <div><button type="button" id="go" disabled>Apply</button><button type="button" id="back">Back</button></div>
 </section>
</div>
<div id="keys" class="modal" role="dialog" aria-modal="true" aria-labelledby="keysh">
 <section class="box">
  <h2 id="keysh">Keys</h2>
  <table class="keys"><thead><tr><th scope="col">Key</th><th scope="col">What it does</th><th scope="col">Where</th></tr></thead><tbody>
   <tr data-key="k"><td><kbd>K</kbd></td><td>Keep the outlined item (press again to clear it, like every mark)</td><td>Every tab</td></tr>
   <tr data-key="e"><td><kbd>E</kbd></td><td>Erase every copy</td><td>Quarantine only</td></tr>
   <tr data-key="d"><td><kbd>D</kbd></td><td>Delete this copy (only this file)</td><td>Review only</td></tr>
   <tr data-key="t"><td><kbd>T</kbd></td><td>Trash (removed, and skipped if imported again)</td><td>Review and Maybe trash; nothing in Quarantine (flagged items are erased or kept)</td></tr>
   <tr data-key="x"><td><kbd>X</kbd></td><td>Open or close the burst stack</td><td>Every tab, on a stack</td></tr>
   <tr data-key="next"><td><kbd>→</kbd> <kbd>↓</kbd> or <kbd>J</kbd></td><td>Next item</td><td>Every tab</td></tr>
   <tr data-key="prev"><td><kbd>←</kbd> <kbd>↑</kbd></td><td>Previous item</td><td>Every tab</td></tr>
   <tr data-key="tabs"><td><kbd>←</kbd> <kbd>→</kbd> <kbd>Home</kbd> <kbd>End</kbd></td><td>Switch list</td><td>On a tab (Quarantine, Review, Maybe trash)</td></tr>
   <tr data-key="tab"><td><kbd>Tab</kbd> <kbd>Shift</kbd>+<kbd>Tab</kbd></td><td>Move between the buttons; stays inside an open dialog</td><td>Everywhere</td></tr>
   <tr data-key="?"><td><kbd>?</kbd></td><td>This list</td><td>Everywhere except while typing</td></tr>
   <tr data-key="escape"><td><kbd>Esc</kbd></td><td>Close this list, or go back from the Apply dialog</td><td>In a dialog</td></tr>
  </tbody></table>
  <p class="help">Letter keys do nothing while you type in a box.</p>
  <button type="button" id="keysx">Close (Esc)</button>
 </section>
</div>
<script>
"use strict";
const CSRF=document.querySelector('meta[name=csrf]').content;
let data=null,tab="quarantine",cur={quarantine:0,review:0,maybe:0};const dec={},open={};
const $=s=>document.querySelector(s);
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function itemsOf(t){return data.items.filter(i=>i.tab===t)}
/* Bursts: the shots of one burst share it.burst (bursts.py, never across tabs). A closed stack shows only
   its first shot (the cover); an open one shows every shot right after the cover. */
const BM={};
function members(it){return it.burst==null?[it]:BM[it.burst]}
function isCover(it){return it.burst==null||BM[it.burst][0]===it}
function visibleOf(t){const v=[];itemsOf(t).forEach(i=>{if(i.burst==null)v.push(i);else if(isCover(i))v.push(...(open[i.burst]?BM[i.burst]:[i]))});return v}
function toggleStack(it){if(it.burst==null)return;const f=inList();open[it.burst]=!open[it.burst];const v=visibleOf(tab);
 cur[tab]=Math.max(0,v.indexOf(v.includes(it)?it:members(it)[0]));render();paint(true);if(f)focusCur()}
/* Keyboard focus follows the outlined card when it was on the list (or nowhere), so a decision, a move or a
   stack opening or closing (which rebuilds the cards) never drops it on the page body. */
function inList(){const a=document.activeElement;return !a||a===document.body||$("#list").contains(a)}
function focusCur(){const it=visibleOf(tab)[cur[tab]],c=it&&$("#c"+it.id);if(c&&c!==document.activeElement)c.focus({preventScroll:true})}
/* The rest of a quarantine stack is flagged too, so it takes the erase-everywhere path, never plain trash. */
function restOf(it){return it.tab==="quarantine"?"erase":"trash"}
function oneLabel(it){return it.tab==="quarantine"?"Keep this one, erase the rest":"Keep this one, trash the rest"}
function keepOne(it){members(it).forEach(m=>{dec[m.id]=m===it?"keep":restOf(it)});const v=visibleOf(tab);cur[tab]=Math.max(0,v.indexOf(it));paint(false)}
/* A file the browser can't decode says so instead of an empty box: a broken image, a video it can't open,
   or one whose picture it can't decode (iPhone HEVC .mov: the sound plays, videoWidth stays 0). */
function cantShow(m,why){m.replaceChildren(el("div","nopreview",why+" Open it in your file manager to see it."));m.dataset.cant="1"}
function mediaEl(kind,src,alt){const m=el("div","media");
 if(kind==="image"){const im=el("img");im.loading="lazy";im.alt=alt;im.onerror=()=>cantShow(m,"This browser can't show this picture.");im.src=src;m.append(im)}
 else if(kind==="video"){const v=el("video");v.controls=true;v.preload="metadata";const bad=()=>cantShow(m,"This browser can't play this video (HEVC, most likely).");
  v.onerror=bad;v.onloadedmetadata=()=>{if(!v.videoWidth)bad()};v.src=src;m.append(v)}
 else m.append(el("div","nopreview","No preview in the browser for this format"));return m}
/* The folder is shown only for a library original: a quarantined or review file (and a batch's raw/ or
   review/ copy) sits in the tool's own state folder, which tells you nothing. */
function internal(f){return !f||/(^|[/])(quarantine|batches)[/]/.test(f)}
function metaEl(x,folder){const p=[x.res,x.size,x.date];if(folder&&!internal(x.folder))p.push(x.folder);return el("div","meta",p.join(" · "))}
function side(title,m,x,folder){const s=el("div","side");s.append(el("h3",null,title),m,metaEl(x,folder));return s}
/* The second button: Erase (every copy) in Quarantine, Delete this copy (this file only) in Review. */
function second(it){return it.tab==="quarantine"?"erase":it.tab==="review"?"delete":null}
/* Quarantine is Erase-only: plain trash would leave a flagged file's copies behind (the server refuses it too). */
function canTrash(it){return it.tab!=="quarantine"}
const TABS=["quarantine","review","maybe"],TID={quarantine:"#tq",review:"#tr",maybe:"#tm"},TNAME={quarantine:"Quarantine",review:"Review",maybe:"Maybe trash"};
/* One set of words for the decisions (the gallery's Keep / Trash / Erase mean the same), and their keys. */
const VERB={keep:"Keep",erase:"Erase",delete:"Delete this copy",trash:"Trash"},KEY={keep:"K",erase:"E",delete:"D",trash:"T"};
/* What a decision will do, shown on the card once it is marked (and as the button's tooltip). */
function verdict(it,d){
 if(d==="keep")return it.tab==="quarantine"?(it.releasable?"it goes back into your library":"it stays in quarantine (its library place is unknown)")
  :it.tab==="review"?"it stays in the review folder for now":"it stays in your library and is not suggested again";
 return d==="erase"?"every copy is removed":d==="delete"?"only this file is removed":"it is removed, and skipped if imported again"}
/* Plain names for the labels: a scanner's flags and the tags' internal groups (Review|, Label|, ...). An unknown
   one keeps its words without the group's punctuation. */
const FLAG={"nudity":"Possible nudity","unscanned-nudity":"Not checked for nudity","unscanned-video":"Video not checked",
 "unscanned-barcode":"Barcodes not checked","unscanned-type":"File type not checked","unreadable":"Couldn't be read",
 "passport-mrz":"Looks like a passport","payment-card":"Looks like a payment card","id-barcode":"Looks like an ID card",
 "2fa-qr":"Two-factor code","wallet-qr":"Wallet code","near-duplicate":"Near-copy",
 "Review|maybe-trash":"Tagged as junk","Review|child-in-shot":"Possible child","Review|vlm-trash":"Judged junk",
 "Review|vlm-keep":"Judged fine","Review|not-trash":"Marked Keep","Owner|keep":"Marked Keep","Owner|not-trash":"Marked Keep",
 "Owner|trash":"Copy of a trashed photo"};
const GROUP={nudity:"Possible nudity",document:"Looks like a document",text:"Private text","secret-words":"Private text",Label:"Looks like",Review:"Flag"};
function plain(r){if(FLAG[r])return FLAG[r];const m=/^([^|:]+)[|:](.+)$/.exec(r);if(!m)return r.replace(/-/g," ");
 const g=GROUP[m[1]]||m[1],v=m[2].replace(/\|/g," / ").replace(/-/g," ");return m[1]==="nudity"?g:`${g}: ${v}`}
function whyText(w){if(w==="junk: tiny or empty")return "Tiny or empty image";
 return w.replace(/^near-duplicate of library /,"Near-copy of ").replace(/^near-duplicate of /,"Near-copy of ")}
function render(){
 const L=$("#list");L.replaceChildren();
 TABS.forEach(t=>{const b=$(TID[t]);b.setAttribute("aria-selected",tab===t);b.tabIndex=tab===t?0:-1});$("#lists").setAttribute("aria-labelledby",TID[tab].slice(1));
 $("#helpq").hidden=tab!=="quarantine";$("#helpr").hidden=tab!=="review";$("#helpm").hidden=tab!=="maybe";
 const its=visibleOf(tab);$("#helpb").hidden=!its.some(i=>i.burst!=null);$("#helpall").hidden=!its.length;
 if(!its.length)L.append(el("p","help","Nothing to decide here."));
 its.forEach((it,n)=>{
  const c=el("div","card");c.id="c"+it.id;c.dataset.n=n;c.dataset.item=it.rel;c.tabIndex=-1;c.setAttribute("role","group");
  if(it.burst!=null){const ms=members(it),bx=el("div","burst"),bd=el("span","badge");c.dataset.burst=it.burst;bd.dataset.count=ms.length;
   if(isCover(it)){const x=el("button","x",open[it.burst]?"Close stack (X)":`Show all ${ms.length} (X)`);x.onclick=()=>{toggleStack(it)};
    if(!open[it.burst])c.classList.add("stack");bx.append(bd,x)}else bx.append(bd);
   c.append(bx)}
  const m=mediaEl(it.kind,"/m/"+it.id,it.rel),o=it.orig;
  if(o&&o.label)c.append(el("div","note",o.label));
  if(o&&o.meta){const p=el("div","pair");
   p.append(side("Original (kept)",mediaEl(o.kind,"/o/"+o.id,"original"),o.meta,true),side("This copy",m,it.meta,false));c.append(p)}
  else{if(o&&o.erased)c.append(el("div","note","Near-copy of a photo you erased"+(o.date?" on "+o.date:"")));
   else if(o&&o.missing)c.append(el("div","note","Original not shown: "+o.missing));
   c.append(m);if(it.meta)c.append(metaEl(it.meta,false))}
  c.append(el("div","name",nameOf(it)));
  if(it.reasons.length){const lb=el("div","labels");[...new Set(it.reasons.map(plain))].forEach(r=>lb.append(el("span",null,r)));c.append(lb)}
  if(it.why)c.append(el("div","why",whyText(it.why)));
  if(it.tab==="quarantine"&&!it.releasable)c.append(el("div","why","Can't go back into the library (its original place is unknown). Keep leaves it in quarantine, and then nothing in this batch is erased."));
  const b=el("div","btns"),btn=(cls,d)=>{const x=el("button",cls,VERB[d]);x.append(el("span","kb",` (${KEY[d]})`));x.title=`${VERB[d]}: ${verdict(it,d)}`;
   x.onclick=()=>{decide(it,d,n,false)};b.append(x)};
  btn("k","keep");
  if(second(it))btn("e",second(it));
  if(canTrash(it))btn("t","trash");
  else b.append(el("span","notrash","Sensitive: erase or keep"));
  c.append(b,el("p","verdict"));
  if(it.burst!=null&&open[it.burst]){const o1=el("button","one",oneLabel(it));o1.onclick=()=>{keepOne(it)};c.append(o1)}
  c.onclick=ev=>{if(ev.target.tagName!=="BUTTON"&&ev.target.tagName!=="VIDEO"){cur[tab]=n;paint(false)}};
  c.addEventListener("focusin",()=>{if(cur[tab]!==n){cur[tab]=n;paint(false)}});  /* Tab onto a card or its buttons outlines it: K acts there */
  L.append(c);
 });
 paint(false);
}
function nameOf(it){return it.rel}
function paint(scroll){
 const vis=visibleOf(tab);
 vis.forEach((it,n)=>{const c=$("#c"+it.id);if(!c)return;
  const d=dec[it.id],e=c.querySelector(".e");
  c.tabIndex=n===cur[tab]?0:-1;c.setAttribute("aria-label",`${nameOf(it)}, item ${n+1} of ${vis.length}: `+(d?"decided "+d:"undecided"));
  const v=c.querySelector(".verdict");v.hidden=!d;const w=d?verdict(it,d):"";setText(v,d?`Decided: ${VERB[d]}. ${w[0].toUpperCase()+w.slice(1)}.`:"");
  c.classList.toggle("focus",n===cur[tab]);c.setAttribute("aria-current",n===cur[tab]);c.classList.toggle("keep",d==="keep");c.classList.toggle("erase",d==="erase"||d==="delete");c.classList.toggle("trash",d==="trash");
  c.querySelector(".k").setAttribute("aria-pressed",d==="keep");if(e)e.setAttribute("aria-pressed",d==="erase"||d==="delete");const t=c.querySelector(".t");if(t)t.setAttribute("aria-pressed",d==="trash");
  const bd=c.querySelector(".badge");if(bd){const ms=members(it),u=ms.filter(m=>!dec[m.id]).length;
   bd.textContent=`Burst: ${ms.length} shots`+(u?`, ${u} undecided`:", all decided")}
  if(scroll&&n===cur[tab])c.scrollIntoView({block:"center",behavior:"smooth"});});
 const left=data.items.filter(i=>!dec[i.id]).length,und=t=>itemsOf(t).filter(i=>!dec[i.id]).length;
 TABS.forEach(t=>{$(TID[t]).textContent=`${TNAME[t]} (${itemsOf(t).length})`});$("#tm").hidden=!itemsOf("maybe").length;
 /* One counter, which is also the reason Submit is locked; it names the tabs still to do unless that is only this one. */
 const todo=TABS.filter(t=>und(t));
 setText($("#done"),left?`Decide ${left} more to submit`+(todo.length>1||todo[0]!==tab?" ("+todo.map(t=>`${und(t)} in ${TNAME[t]}`).join(", ")+")":"")
  :`All decided: ${data.items.length} of ${data.items.length}. Ready to submit.`);
 $("#submit").disabled=left>0;
}
/* Phone: pin only the header's bar (the counter and Submit); the title and tabs above it scroll away. */
function placeHeader(){const h=$("header"),b=$("#bar"),phone=matchMedia("(max-width:640px)").matches,top=phone?b.offsetTop-8:0;
 h.style.top=-top+"px";document.documentElement.style.setProperty("--pin",(h.offsetHeight-top)+"px")}
function setText(e,s){if(e.textContent!==s)e.textContent=s}
/* A second press of the same decision clears it (and stays on the card); anything else sets it. */
function decide(it,d,n,adv){const f=inList();cur[tab]=n;
 if(dec[it.id]===d){delete dec[it.id];adv=false}else dec[it.id]=d;
 if(adv)cur[tab]=Math.min(n+1,visibleOf(tab).length-1);paint(adv);if(adv&&f)focusCur()}
function move(d){const its=visibleOf(tab);if(!its.length)return;const f=inList();cur[tab]=Math.max(0,Math.min(its.length-1,cur[tab]+d));paint(true);if(f)focusCur()}
function setTab(t){tab=t;render();window.scrollTo(0,0)}
TABS.forEach(t=>{$(TID[t]).onclick=()=>setTab(t)});
/* The tab bar is a tablist: on a tab, Left / Right (wrapping), Home and End switch lists and keep the focus on
   the tab; anywhere else the arrows move between items as before. */
function tabKey(b,key){const vis=TABS.filter(t=>!$(TID[t]).hidden),i=vis.findIndex(t=>TID[t]==="#"+b.id);
 const j=key==="Home"?0:key==="End"?vis.length-1:(i+(key==="ArrowRight"?1:-1)+vis.length)%vis.length;setTab(vis[j]);$(TID[vis[j]]).focus()}
/* Dialogs (the ERASE confirm, the keys list): modal, the page behind is inert, Tab stays inside, Esc closes,
   and the focus goes back to where it was. */
let lastFocus=null;
function modal(d,on){d.classList.toggle("on",on);const any=!!document.querySelector(".modal.on");$("header").inert=any;$("main").inert=any;placeErr()}
function trap(d,ev){const f=[...d.querySelectorAll("button,input,[tabindex]")].filter(x=>!x.disabled&&x.tabIndex>=0&&x.getClientRects().length);
 if(!f.length){ev.preventDefault();return}const a=document.activeElement,i=f.indexOf(a);
 if(ev.shiftKey&&(i<=0)){f[f.length-1].focus();ev.preventDefault()}else if(!ev.shiftKey&&(i<0||i===f.length-1)){f[0].focus();ev.preventDefault()}}
function openKeys(){lastFocus=document.activeElement;modal($("#keys"),true);$("#keysx").focus()}
function closeKeys(){modal($("#keys"),false);const f=lastFocus;lastFocus=null;
 if(f&&f.isConnected&&f!==document.body&&!f.disabled)f.focus({preventScroll:true});else focusCur()}
$("#keysb").onclick=openKeys;$("#keysx").onclick=closeKeys;
$("#keys").onclick=ev=>{if(ev.target.id==="keys")closeKeys()};
/* Errors show in a banner (role=alert), never a native dialog: under the Apply dialog's title while it is open,
   else at the top of the list, below the header (scrolled into view, never over the header). */
function placeErr(){const b=$("#err");if($("#confirm").classList.contains("on"))$("#confirmh").after(b);else $("main").prepend(b)}
/* The server's refusals in plain words: never a PID or a lock file's path. */
function plainErr(m){m=String(m);
 if(/library is busy/.test(m))return "Another action is using the library (an import, a gallery Trash or Erase, or another review). Wait for it to finish, then apply again.";
 if(/is still running/.test(m))return "A run of this review is still going. Wait for it to finish, then open the review again.";
 return m.replace(/\s*\(PID \d+\)/g,"").replace(/\s*\([^()]*\.lock\)/g,"")}
function showErr(msg){$("#err").hidden=false;$("#errmsg").replaceChildren(document.createTextNode(msg));
 if(!$("#confirm").classList.contains("on"))$("#err").scrollIntoView({block:"nearest"})}
function hideErr(){$("#err").hidden=true;$("#errmsg").replaceChildren()}
$("#errx").onclick=()=>{hideErr();const t=$("#confirm").classList.contains("on")?($("#typedbox").hidden?$("#go"):$("#typed")):$("#submit");if(!t.disabled)t.focus();else focusCur()};
document.addEventListener("keydown",ev=>{
 const dlg=document.querySelector(".modal.on");
 if(dlg){if(ev.key==="Escape"){ev.preventDefault();if(dlg.id==="keys")closeKeys();else back()}else if(ev.key==="Tab")trap(dlg,ev);return}
 if(!data||$("#lists").hidden||ev.target.tagName==="INPUT"||ev.ctrlKey||ev.metaKey||ev.altKey)return;
 if(ev.key==="?"){openKeys();ev.preventDefault();return}
 if(ev.target.getAttribute&&ev.target.getAttribute("role")==="tab"&&["ArrowLeft","ArrowRight","Home","End"].includes(ev.key)){tabKey(ev.target,ev.key);ev.preventDefault();return}
 const its=visibleOf(tab),it=its[cur[tab]];const k=ev.key.toLowerCase();
 if(k==="k"&&it){decide(it,"keep",cur[tab],true);ev.preventDefault()}
 else if(it&&second(it)&&k===KEY[second(it)].toLowerCase()){decide(it,second(it),cur[tab],true);ev.preventDefault()}
 else if(k==="t"&&it&&canTrash(it)){decide(it,"trash",cur[tab],true);ev.preventDefault()}
 else if(k==="x"&&it&&it.burst!=null){toggleStack(it);ev.preventDefault()}
 else if(k==="arrowright"||k==="arrowdown"||k==="j"){move(1);ev.preventDefault()}
 else if(k==="arrowleft"||k==="arrowup"){move(-1);ev.preventDefault()}
});
/* The Apply dialog: titled and labelled with the action, one line of counts per tab, then what goes where. No
   device path: "your phone" and "the backup drive" are the ones this review was started with. */
const ALLOW={quarantine:["keep","erase"],review:["keep","delete","trash"],maybe:["keep","trash"]};
$("#submit").onclick=()=>{
 const n=(t,d)=>itemsOf(t).filter(i=>dec[i.id]===d).length,N=data.items.length,s=x=>x===1?"":"s",ph=data.devices.phone,dr=data.devices.drive;
 const ul=el("ul"),notes=[];
 TABS.filter(t=>itemsOf(t).length).forEach(t=>ul.append(el("li",null,`${TNAME[t]}: `+ALLOW[t].map(d=>`${VERB[d].toLowerCase()} ${n(t,d)}`).join(", "))));
 const er=n("quarantine","erase"),tr=n("review","trash")+n("maybe","trash"),gone=er+tr+n("review","delete");
 if(er)notes.push(`Erase removes every copy: on this computer`+(ph&&dr?", your phone and the backup drive":ph?" and your phone":dr?" and the backup drive":" (no phone or drive was connected for this review)")+".");
 if(tr&&ph)notes.push("Trashed files are also deleted from your phone.");
 if(n("review","keep"))notes.push("Kept Review items stay in the review folder.");
 notes.push(gone?`${gone} file${s(gone)} will be removed for good. This can't be undone.`:"Nothing will be deleted.");
 $("#summary").replaceChildren(ul,...notes.map(x=>el("p",null,x)));
 setText($("#confirmh"),`Apply ${N} decision${s(N)}?`);setText($("#go"),`Apply ${N} decision${s(N)}`);
 /* ERASE is typed only when something is removed; an all-keep apply deletes nothing, so it asks for no word */
 $("#typedbox").hidden=!gone;$("#submit").disabled=true;$("#typed").value="";$("#go").disabled=!!gone;
 modal($("#confirm"),true);(gone?$("#typed"):$("#go")).focus();
};
function back(){modal($("#confirm"),false);paint(false);$("#submit").focus();if(!$("#err").hidden)$("#err").scrollIntoView({block:"nearest"})}
$("#back").onclick=back;
$("#typed").oninput=()=>{$("#go").disabled=$("#typed").value!=="ERASE"};
$("#go").onclick=async()=>{
 $("#go").disabled=true;const d={};data.items.forEach(i=>d[i.id]=dec[i.id]);let r;
 try{r=await fetch("/api/submit",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF":CSRF},body:JSON.stringify({decisions:d,confirm:$("#typed").value})})}
 catch(e){showErr("No answer from the page server: run the same review command again to see whether the run started.");$("#go").disabled=false;$("#go").focus();return}
 if(r.status!==202){const j=await r.json().catch(()=>({}));if(j.watching){attach();return}showErr("Not applied: "+plainErr(j.error||r.status));$("#go").disabled=false;$("#go").focus();return}
 attach();
};
/* The run is a detached job (jobs.py): closing this tab or stopping the server never stops it, and any
   page of this batch (a reload, a second tab, a restarted server) shows it from the status file. */
let attached=false;
function attach(){if(attached)return;attached=true;hideErr();modal($("#confirm"),false);modal($("#keys"),false);$("#lists").hidden=true;$("#run").style.display="block";
 document.querySelectorAll("header button:not(#keysb)").forEach(b=>b.disabled=true);setText($("#done"),"Applying your decisions");$("#list").replaceChildren();$("#out").textContent="";$("#runh").focus({preventScroll:true});poll(0)}
function stepText(jb){return jb&&jb.step?`Step ${jb.step}${jb.total?" of "+jb.total:""}: ${jb.label}`:""}
async function poll(since){
 let j;try{j=await (await fetch("/api/progress?since="+since,{credentials:"same-origin"})).json()}
 catch(e){setText($("#runh"),"Page server stopped");setText($("#runs"),"If the run had not finished it keeps going on its own; run the same review command again to watch it.");return}
 if(j.lines.length)$("#out").append(j.lines.join("\n")+"\n");$("#out").scrollTop=1e9;
 const jb=j.job||{};$("#run").dataset.job=!j.done?"running":j.replaced?"replaced":jb.state==="crashed"?"crashed":j.ok?"done":"failed";setText($("#runs"),stepText(jb)+(jb.started?` (started ${jb.started})`:""));
 if(j.done){
  if(j.replaced)setText($("#runh"),"Another page started a newer run of this batch; open the review again to see it.");
  else if(jb.state==="crashed"){setText($("#runh"),"The run stopped unexpectedly (crashed)");
   setText($("#runs"),`It died at ${stepText(jb)||"start-up"}. Nothing more will run by itself`+(jb.step_running?"; the step it had started is still running, so a new submit is refused until it ends.":". Open the review again: it shows what is left."))}
  else setText($("#runh"),j.ok?"Done. You can close this tab.":"Finished with problems (see below). You can close this tab.");return}
 setText($("#runh"),"Running… (closing this tab does not stop it)");
 setTimeout(()=>poll(j.next),700);
}
function lastRun(jb){if(!jb||!jb.done)return;const L=$("#last");L.hidden=false;
 L.textContent=`Last run of this batch (started ${jb.started}): `+(jb.state==="crashed"?`crashed at ${stepText(jb)||"start-up"}`+(jb.step_running?"; the step it started is still running, so submit is refused until it ends.":"."):jb.ok?"finished OK.":"finished with problems.")}
addEventListener("resize",placeHeader);if(window.ResizeObserver)new ResizeObserver(placeHeader).observe($("header"));
fetch("/api/items",{credentials:"same-origin"}).then(r=>r.json()).then(j=>{data=j;
 document.title=$("#title").textContent=j.batch?`Review ${j.batch}`:"Review suggested junk";
 j.items.forEach(i=>{if(i.burst!=null)(BM[i.burst]=BM[i.burst]||[]).push(i)});tab=TABS.find(t=>itemsOf(t).length)||"quarantine";
 if(j.watching){attach();document.body.dataset.ready="1";return}render();lastRun(j.job);document.body.dataset.ready="1";
 /* a run started from another page or server of this batch: show it instead of stale items */
 const watch=async()=>{if(attached)return;try{const k=await (await fetch("/api/job",{credentials:"same-origin"})).json();if(k.watching){attach();return}}catch(e){return}setTimeout(watch,4000)};
 setTimeout(watch,4000)});
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
