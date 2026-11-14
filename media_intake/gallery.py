#!/usr/bin/env python3
"""Local web gallery for the media library (a day-to-day browser, in place of digiKam).

  media-intake gallery [--drive MOUNT] [--port 0]
  python3 -I media_intake/gallery.py         (same flags)

Shows everything under the library, videos and project folders (config.toml)
(never the media-intake state folder: quarantine, staging, review; never a file
scan flagged, quarantined or erased: label.flagged()). Timeline grid by month,
thumbnails made on demand (max 320 px JPEG, ~/.cache/media-intake/thumbs/<sha256>.jpg,
dir 0700, files 0600; intake.py erase/forget/trash delete them by sha and
verify-gone checks them), filters, semantic search (semantic.py, local SigLIP),
and your actions: Keep, Trash (intake.py trash), Erase (intake.py forget, the
erase-everywhere path), my tags, Rotate (XMP-tiff:Orientation in the sidecar
only). Views: Library, Trash suggestions, plus any tab a plugin adds
(plugins.py gallery_views(), docs/plugins.md). Trash and Erase run as a detached
job (jobs.py, key "gallery", one at a time): closing the page or stopping the
server never stops one, and a restarted server or another page shows it again.
The paths go to the worker on a pipe; the status file names the files only by
(device, inode), and an Erase's output is scrubbed of names when it ends.

Server security is review_ui.py's: stdlib only, everything inline, 127.0.0.1 on a
random port, random token swapped for an HttpOnly SameSite=Strict cookie, Host
check, CSRF value on every POST, no-store + strict CSP, media by opaque id only.
Originals are never written: sidecars (<file>.<ext>.xmp) only. docs/gallery.md.
"""
import argparse, datetime, html, http.server, io, json, os, re, secrets, shutil, subprocess, sys, tempfile
import threading, time, urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intake  # noqa: E402  (constants, sidecar helpers, trash_allowed, exiftool_batch)
import jobs  # noqa: E402  (trash / erase run as a detached job: <state>/jobs/gallery.*)
import label  # noqa: E402  (flagged / refused: stdlib only at import)
import plugins  # noqa: E402  (extra tabs: gallery_views())
import review_ui as R  # noqa: E402  (server guards, media with Range, sizes)

HOME, STATE = intake.HOME, intake.STATE
LIBRARY, VIDEOS = intake.LIBRARY, intake.VIDEOS
ROOTS = list(intake.LIB_ROOTS)
PROTECTED = intake.config.protected_tags(intake.CFG)
THUMBS = intake.THUMBS
CACHE = os.path.dirname(THUMBS)
JOB_KEY = "gallery"  # one trash/erase at a time across every gallery page and server
IMG, VID = intake.IMG, intake.VID
THUMB_PX, PREVIEW_PX = 320, 1600
# Namespaces "my tag" never writes or removes (docs/intake.md, "Tags: who writes what").
TOOL_PREFIXES = ("project|", "source|", "event|", "place|", "label|", "review|", "ai|", "used|", "owner|")
TAG_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
DEG = {1: 0, 6: 90, 3: 180, 8: 270}
ORIENT = {0: 1, 90: 6, 180: 3, 270: 8}
AI_TRASH = re.compile(r"^AI\|vlm/([^/]+)/trash(?:/|$)")
AI_CONF = re.compile(r"^AI\|vlm/([^/]+)/conf/([0-9.]+)$")
SUGGEST_MIN = 0.7

NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_WANT = {
    ("http://ns.adobe.com/lightroom/1.0/", "hierarchicalSubject"): "tags",
    ("http://ns.adobe.com/photoshop/1.0/", "City"): "city",
    ("http://ns.adobe.com/photoshop/1.0/", "State"): "state",
    ("http://ns.adobe.com/photoshop/1.0/", "Country"): "country",
    ("http://ns.adobe.com/exif/1.0/", "DateTimeOriginal"): "date",
    ("http://ns.adobe.com/tiff/1.0/", "Orientation"): "orient",
    ("http://iptc.org/std/Iptc4xmpExt/2008-02-29/", "Event"): "event",
    ("http://www.digikam.org/ns/1.0/", "PickLabel"): "pick",
    ("http://www.digikam.org/ns/1.0/", "TagsList"): "tagslist",
}
LISTS = {"tags", "tagslist"}
DATE_RX = re.compile(r"(\d{4})[-:](\d\d)[-:](\d\d)(?:[T ](\d\d):(\d\d))?")
PATH_DATE = re.compile(r"(?:^|/)((?:19|20)\d\d)-(\d\d)(?:-(\d\d))?")
NAME_DATE = re.compile(r"((?:19|20)\d\d)(\d\d)(\d\d)[_-]?(\d\d)?(\d\d)?")


# ---------------------------------------------------------------- sidecars (read: stdlib XML, fast)
def _split(tag):
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def read_xmp(path):
    """{tags, tagslist, city, state, country, date, orient, event, pick} from an XMP sidecar."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, ValueError):
        return {}
    out = {}
    for d in root.iter(f"{{{NS_RDF}}}Description"):
        for k, v in d.attrib.items():
            name = XMP_WANT.get(_split(k))
            if name:
                out[name] = [v] if name in LISTS else v
        for ch in d:
            name = XMP_WANT.get(_split(ch.tag))
            if not name:
                continue
            lis = [li.text or "" for li in ch.iter(f"{{{NS_RDF}}}li")]
            if name in LISTS:
                out[name] = lis or ([ch.text.strip()] if (ch.text or "").strip() else [])
            else:
                out[name] = (lis[0] if lis else (ch.text or "")).strip()
    return out


def norm_date(s):
    m = DATE_RX.search(s or "")
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d} {h or '00'}:{mi or '00'}"


def guess_date(path, root, mtime):
    rel = os.path.relpath(path, root)
    m = NAME_DATE.search(os.path.basename(path))
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4) or '00'}:{m.group(5) or '00'}"
    m = None
    for m in PATH_DATE.finditer(rel):
        pass
    mt = f"{datetime.datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}"
    if m and 1 <= int(m.group(2)) <= 12:
        ym = f"{m.group(1)}-{m.group(2)}"
        if mt.startswith(ym) and (not m.group(3) or mt.startswith(f"{ym}-{m.group(3)}")):
            return mt  # the file's own time agrees with its folder: keep the day and hour
        return f"{ym}-{m.group(3) or '01'} 00:00"
    return mt


def owner_tag(t):
    return not t.casefold().startswith(TOOL_PREFIXES)


def facts(it):
    """Derived fields from an item's tags (recomputed after every sidecar change)."""
    tags = it["tags"]
    pre = lambda p: [t[len(p):] for t in tags if t.startswith(p)]  # noqa: E731
    proj, src, _ = intake.infer(it["path"])
    it["project"] = (pre("Project|") or [proj])[0]
    it["source"] = (pre("Source|") or [src or ""])[0]
    ev = pre("Event|") or ([it["xevent"]] if it.get("xevent") else [])
    it["event"] = ev[0] if ev else ""
    it["place"] = (pre("Place|") or [", ".join(x for x in it.get("xplace", ()) if x)])[0]
    it["labels"] = pre("Label|")
    it["mine"] = [t for t in tags if owner_tag(t)]
    it["used"] = sorted(t[5:] for t in tags if t.startswith("Used|"))
    trash_models = {m.group(1) for t in tags if (m := AI_TRASH.match(t))}
    confs = {}
    for t in tags:
        m = AI_CONF.match(t)
        if m:
            try:
                confs[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    ai = max((confs.get(mo, 0.0) for mo in trash_models), default=None)
    if any(t.startswith("Review|vlm-trash/") for t in tags):
        ai = max(ai or 0.0, SUGGEST_MIN)
    it["ai_trash"] = ai
    it["ai_keep"] = any(t == "Review|vlm-keep" or (t.startswith("AI|vlm/") and "/keep" in t) for t in tags)
    it["kept"] = intake.owner_kept(tags, it.get("pick", ""))


# ---------------------------------------------------------------- library scan
def under_state(p):
    return os.path.realpath(p).startswith(os.path.realpath(STATE) + os.sep)


def scan_library():
    """[item] for every library media file, newest first. Never quarantine/staging/flagged/erased."""
    index = intake.load(os.path.join(STATE, "index.json"), {}) or {}
    flagged = label.flagged(STATE)
    sizes = set(flagged.values())
    items, skipped = [], 0
    seen = set()
    for root in ROOTS:
        if not os.path.isdir(root) or under_state(root):
            continue
        rroot = os.path.realpath(root)
        for d, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext not in IMG | VID:
                    continue
                p = os.path.join(d, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                rp = os.path.realpath(p)
                if os.path.islink(p) or not os.path.isfile(p) or not rp.startswith(rroot + os.sep) \
                        or under_state(p) or rp in seen:
                    continue
                seen.add(rp)
                ix = index.get(p) or {}
                sha = ix.get("sha") if ix.get("size") == st.st_size else None
                if st.st_size in sizes:
                    sha = sha or intake.sha256(p)
                    if flagged.get(sha) == st.st_size:
                        skipped += 1
                        continue
                k, mime = R.kind_of(p)
                items.append(dict(path=p, root=root, ext=ext, media="video" if ext in VID else "photo",
                                  show=k, mime=mime, size=st.st_size, mtime=st.st_mtime, sha=sha, gone=False,
                                  fid=(st.st_dev, st.st_ino)))
    for it in items:
        load_sidecar(it)
    items.sort(key=lambda it: (it["date"], it["path"]), reverse=True)
    return items, skipped


def load_sidecar(it):
    x = read_xmp(intake.sidecar(it["path"])) if os.path.isfile(intake.sidecar(it["path"])) else {}
    it["has_xmp"] = bool(x) or os.path.isfile(intake.sidecar(it["path"]))
    it["tags"] = [t for t in x.get("tags", []) if t]
    it["tagslist"] = x.get("tagslist", [])
    it["pick"] = str(x.get("pick", "")).strip()
    it["xevent"] = x.get("event") or ""
    it["xplace"] = (x.get("city"), x.get("state"), x.get("country"))
    try:
        it["orient"] = int(x["orient"]) if x.get("orient") else None
    except ValueError:
        it["orient"] = None
    it["date"] = norm_date(x.get("date")) or guess_date(it["path"], it["root"], it["mtime"])
    facts(it)


# ---------------------------------------------------------------- thumbnails / previews
def private_cache():
    for d in (os.path.dirname(CACHE), CACHE, THUMBS):
        os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(CACHE, 0o700)
    os.chmod(THUMBS, 0o700)


def write_private_bytes(p, data):
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def file_orientation(it):
    """EXIF orientation in the original (images; the browser applies it). Videos: 1 (players rotate)."""
    if "forient" not in it:
        o = 1
        if it["media"] == "photo":
            try:
                from PIL import Image
                with Image.open(it["path"]) as im:
                    o = int(im.getexif().get(274) or 1)
            except Exception:
                o = 1
        it["forient"] = o if o in DEG else 1
    return it["forient"]


def effective_orientation(it):
    o = it.get("orient")
    return o if o in DEG else file_orientation(it)


def css_delta(it):
    """Extra clockwise rotation, in degrees, on top of what the browser already applies."""
    return (DEG[effective_orientation(it)] - DEG[file_orientation(it)]) % 360


def _pil_jpeg(im, px, delta, quality=80):
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((px, px), Image.LANCZOS)
    if delta:
        im = im.rotate(-delta, expand=True)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality)
    return b.getvalue()


def image_jpeg(path, px, delta):
    from PIL import Image
    try:
        with Image.open(path) as im:
            im.draft("RGB", (px * 2, px * 2))
            return _pil_jpeg(im, px, delta)
    except Exception:
        pass
    return ffmpeg_frame(path, px, delta, seek=None)


def ffmpeg_frame(path, px, delta, seek=1.0):
    vf = f"scale={px}:{px}:force_original_aspect_ratio=decrease"
    for ss in ([] if seek is None else [["-ss", str(seek)]]) + [[]]:
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", *ss, "-i", "file:" + path, "-frames:v", "1", "-vf", vf,
                                "-f", "image2pipe", "-c:v", "mjpeg", "-"], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
            if not delta:
                return r.stdout
            from PIL import Image
            return _pil_jpeg(Image.open(io.BytesIO(r.stdout)), px, delta)
    return None


def video_jpeg(path, px, delta):
    out = ffmpeg_frame(path, px, delta)
    if out:
        return out
    import scan_sensitive  # HEVC: GPU decode via GStreamer
    from PIL import Image
    private_cache()
    d = tempfile.mkdtemp(dir=CACHE)
    try:
        names = scan_sensitive.video_frames(path, d, max_frames=1)
        if not names:
            return None
        with Image.open(os.path.join(d, names[0])) as im:
            return _pil_jpeg(im, px, delta)
    except Exception:
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- app
class App:
    def __init__(self, items, drive=None, views=()):
        self.items = items
        self.by_path = {it["path"]: i for i, it in enumerate(items)}
        # (device, inode): how a job's status file names its files, so no path is written (see start_job)
        self.by_fid = {tuple(it["fid"]): i for i, it in enumerate(items) if it.get("fid")}
        self.drive = drive
        self.views = {v["id"]: v for v in views}  # plugin tabs (plugins.py)
        self.token, self.csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.thumb_sem = threading.BoundedSemaphore(3)
        self.port = None
        self.reconciled = None  # id of the last finished job whose results are applied to items
        self.tail = None  # the shown job's log, through an open descriptor (jobs.Tail)
        self.search_cache = {}
        self.text_why = None
        self.text_checked = threading.Event()
        self.stop = threading.Event()

    # ---------------------------------------------- semantic search availability
    def check_text(self):
        try:
            if not os.path.exists(intake.VENV_PY):
                self.text_why = f"no venv ({intake.VENV_PY}): run bash {intake.SETUP}"
            elif not os.path.exists(os.path.join(STATE, "embeddings.npz")):
                self.text_why = "no embeddings yet: media-intake index"
            else:
                import semantic
                why = semantic.text_available()
                self.text_why = f"SigLIP text model {why}: bash {intake.SETUP} --build-labels" if why else None
        except Exception as e:
            self.text_why = f"semantic search unavailable ({type(e).__name__})"
        self.text_checked.set()

    def semantic(self, args):
        """[(score, item id)] from semantic.py, or raise RuntimeError."""
        r = subprocess.run([intake.VENV_PY, "-I", os.path.join(HERE, "semantic.py"), *args], capture_output=True,
                           text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=HERE)
        if r.returncode:
            raise RuntimeError((r.stderr.strip().splitlines() or ["semantic.py failed"])[-1][:300])
        out = []
        for ln in r.stdout.splitlines():
            m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s+(/.*)$", ln)
            if m and m.group(2) in self.by_path:
                out.append((float(m.group(1)), self.by_path[m.group(2)]))
        return out

    # ---------------------------------------------- items
    def live(self, i):
        return 0 <= i < len(self.items) and not self.items[i]["gone"]

    def sha_of(self, it):
        if not it.get("sha"):
            it["sha"] = intake.sha256(it["path"])
        return it["sha"]

    def thumb(self, it):
        """Path of the cached thumbnail, made on demand; None when the file can't be decoded."""
        sha = self.sha_of(it)
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        dst = os.path.join(THUMBS, sha + ".jpg")
        if os.path.isfile(dst):
            return dst
        if it.get("nothumb"):
            return None
        with self.thumb_sem:
            if os.path.isfile(dst):
                return dst
            delta = DEG[effective_orientation(it)] if it["media"] == "video" else css_delta(it)
            data = video_jpeg(it["path"], THUMB_PX, delta) if it["media"] == "video" else \
                image_jpeg(it["path"], THUMB_PX, delta)
            if not data:
                it["nothumb"] = True
                return None
            private_cache()
            write_private_bytes(dst, data)
        return dst

    def drop_thumb(self, it):
        if it.get("sha"):
            intake.gallery_thumb_forget(it["sha"])
        it.pop("nothumb", None)

    def summary(self, i, it):
        return dict(id=i, m=it["media"], d=it["date"], mo=it["date"][:7], n=os.path.basename(it["path"]),
                    c=it["ai_trash"], f=[t[7:] for t in it["tags"] if t in ("Review|maybe-trash", "Review|child-in-shot")]
                    + (["kept"] if it["kept"] else []) + (["used"] if it["used"] else []))

    def detail(self, i):
        it = self.items[i]
        if "res" not in it:
            try:
                it["res"] = R.meta(it["path"], it["show"] if it["show"] != "none" else "image")["res"]
            except OSError:
                it["res"] = "unknown"
        d = os.path.dirname(it["path"])
        folder = "~/" + os.path.relpath(d, HOME) if d.startswith(HOME + os.sep) else d
        src = f"/m/{i}" if it["show"] in ("image", "video") else (f"/p/{i}" if it["media"] == "photo" else None)
        rot = css_delta(it) if it["media"] == "photo" else DEG[effective_orientation(it)]
        return dict(id=i, name=os.path.basename(it["path"]), kind=it["media"], src=src,
                    video=it["show"] == "video", date=it["date"], place=it["place"], event=it["event"],
                    project=it["project"], source=it["source"], folder=folder, res=it["res"],
                    size=R.human(it["size"]), tags=it["tags"], mine=it["mine"], rot=rot, ai=it["ai_trash"],
                    used=it["used"], kept=it["kept"], has_xmp=it["has_xmp"])

    def facets(self):
        from collections import Counter
        c = {k: Counter() for k in ("project", "source", "event", "place", "label", "month", "mine")}
        n = 0
        for it in self.items:
            if it["gone"]:
                continue
            n += 1
            for k in ("project", "source", "event", "place"):
                if it[k]:
                    c[k][it[k]] += 1
            c["month"][it["date"][:7]] += 1
            c["month"][it["date"][:4]] += 1
            for t in it["labels"]:
                c["label"][t] += 1
            for t in it["mine"]:
                c["mine"][t] += 1
        out = {k: sorted(v.items(), key=lambda x: (-x[1], x[0])) for k, v in c.items()}
        out["month"] = sorted(c["month"].items(), reverse=True)
        self.text_checked.wait(timeout=0.05)
        return dict(total=n, facets=out, search=None if self.text_why is None and self.text_checked.is_set()
                    else (self.text_why or "checking the search model..."))

    def suggested(self, it):
        if it["kept"] or it["used"] or f"Project|{it['project']}" in PROTECTED or PROTECTED.intersection(it["tags"]):
            return False
        return "Review|maybe-trash" in it["tags"] or (it["ai_trash"] or 0) >= SUGGEST_MIN

    def matches(self, it, f):
        if it["gone"]:
            return False
        t = it["tags"]
        if f.get("view") == "suggest" and not self.suggested(it):
            return False
        if f.get("kind") in ("photo", "video") and it["media"] != f["kind"]:
            return False
        for k in ("project", "source", "event", "place"):
            if f.get(k) and it[k] != f[k]:
                return False
        if f.get("label") and f["label"] not in it["labels"]:
            return False
        if f.get("mine") and f["mine"] not in it["mine"]:
            return False
        if f.get("ym") and not it["date"].startswith(f["ym"]):
            return False
        rv = f.get("review")
        if rv == "maybe-trash" and "Review|maybe-trash" not in t:
            return False
        if rv == "child-in-shot" and "Review|child-in-shot" not in t:
            return False
        if rv == "untagged" and t and not any(x.startswith("Review|untagged-") for x in t):
            return False
        ai = f.get("ai")
        if ai == "trash":
            try:
                lo = float(f.get("aimin") or 0)
            except ValueError:
                lo = 0.0
            if it["ai_trash"] is None or it["ai_trash"] < lo:
                return False
        if ai == "keep" and not it["ai_keep"]:
            return False
        if ai == "none" and any(x.startswith("AI|") for x in t):
            return False
        used = f.get("used")
        if used == "used" and not it["used"]:
            return False
        if used == "unused":
            try:
                days = int(f.get("days") or 0)
            except ValueError:
                days = 0
            if days <= 0 and it["used"]:
                return False
            if days > 0:
                cut = datetime.date.today() - datetime.timedelta(days=days)
                for u in it["used"]:
                    m = intake.POST_SLUG.match(u)
                    if m and datetime.date.fromisoformat(m.group(1)) >= cut:
                        return False
        return True

    def text_match(self, it, words):
        hay = " ".join([os.path.basename(it["path"]), os.path.dirname(it["path"]), it["place"], it["event"]]
                       + it["tags"]).casefold()
        return all(w in hay for w in words)

    def listing(self, f):
        note, ranked = None, False
        ids = None
        q = (f.get("q") or "").strip()[:200]
        sim = f.get("similar")
        if sim not in (None, ""):
            try:
                si = int(sim)
            except ValueError:
                si = -1
            if not self.live(si):
                return dict(total=0, items=[], note="that item is gone", ranked=True)
            self.text_checked.wait(timeout=30)
            key = ("similar", si)
            if key not in self.search_cache:
                if not os.path.exists(intake.VENV_PY) or not os.path.exists(os.path.join(STATE, "embeddings.npz")):
                    return dict(total=0, items=[], ranked=True,
                                note="Similar needs the semantic index (semantic.py index); not available here")
                try:
                    self.search_cache[key] = self.semantic(["similar", self.items[si]["path"], "-n", "300"])
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    return dict(total=0, items=[], ranked=True, note=f"Similar failed: {e}")
            ids, ranked = [i for _, i in self.search_cache[key]], True
            note = f"Similar to {os.path.basename(self.items[si]['path'])}"
        elif q:
            self.text_checked.wait(timeout=30)
            if self.text_why is None:
                key = ("search", q)
                if key not in self.search_cache:
                    try:
                        self.search_cache[key] = self.semantic(["search", q, "-n", "400"])
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        self.search_cache.pop(key, None)
                        note = f"semantic search failed ({e}); showing tag and file-name matches"
                if key in self.search_cache:
                    ids, ranked = [i for _, i in self.search_cache[key]], True
                    note = f"Semantic search: {q}"
            else:
                note = f"Semantic search unavailable ({self.text_why}); showing tag and file-name matches"
            if len(self.search_cache) > 16:
                self.search_cache.pop(next(iter(self.search_cache)))
        with self.lock:
            if ids is None:
                words = q.casefold().split() if q else []
                ids = [i for i, it in enumerate(self.items) if (not words or self.text_match(it, words))]
            ids = [i for i in ids if self.live(i) and self.matches(self.items[i], f)]
            if f.get("view") == "suggest":
                ids.sort(key=lambda i: -(self.items[i]["ai_trash"] if self.items[i]["ai_trash"] is not None else 0.5))
                ranked = True
            try:
                off = max(0, int(f.get("offset") or 0))
                lim = max(1, min(500, int(f.get("limit") or 200)))
            except ValueError:
                off, lim = 0, 200
            page = [self.summary(i, self.items[i]) for i in ids[off:off + lim]]
        return dict(total=len(ids), items=page, note=note, ranked=ranked)

    # ---------------------------------------------- your actions (sidecars only)
    def targets(self, ids):
        if not isinstance(ids, list) or not ids or len(ids) > 5000:
            raise ValueError("no items")
        out = []
        for i in ids:
            if not isinstance(i, int) or isinstance(i, bool) or not self.live(i):
                raise ValueError("unknown or removed item")
            out.append(i)
        return list(dict.fromkeys(out))

    def ensure_sidecar(self, it):
        x = intake.sidecar(it["path"])
        if not x.endswith(".xmp") or it["path"].lower().endswith(".xmp"):
            raise ValueError("not a sidecar")
        if not os.path.exists(x):
            with open(x, "w", encoding="utf-8") as fh:
                fh.write(intake.EMPTY_XMP)
        return x

    def run_exiftool(self, blocks):
        try:
            intake.exiftool_batch(blocks)
        except SystemExit as e:
            raise RuntimeError(str(e))

    def refresh(self, ids):
        for i in ids:
            load_sidecar(self.items[i])

    def leaf_free(self, it, leaf, removing):
        """True when no remaining tag (or digiKam TagsList entry) uses this dc:Subject leaf."""
        rest = [t for t in it["tags"] if t not in removing]
        return leaf not in {t.split("|")[-1] for t in rest} | {t.split("/")[-1] for t in it["tagslist"]}

    def keep(self, ids):
        with self.lock:
            ids = self.targets(ids)
            blocks = []
            for i in ids:
                it = self.items[i]
                x = self.ensure_sidecar(it)
                b = ["-q", "-m", "-overwrite_original", "-sep", "@@sep@@",
                     "-XMP-lr:HierarchicalSubject-=Review|maybe-trash"]
                if self.leaf_free(it, "maybe-trash", {"Review|maybe-trash"}):
                    b.append("-XMP-dc:Subject-=maybe-trash")
                for h in ("Owner|keep", "Owner|not-trash"):
                    leaf = h.split("|")[-1]
                    b += [f"-XMP-lr:HierarchicalSubject-={h}", f"-XMP-lr:HierarchicalSubject+={h}",
                          f"-XMP-dc:Subject-={leaf}", f"-XMP-dc:Subject+={leaf}"]
                blocks.append(b + [x])
            self.run_exiftool(blocks)
            self.refresh(ids)
            return f"kept {len(ids)}: Owner|keep + Owner|not-trash, Review|maybe-trash removed"

    def tag(self, ids, tag, op):
        tag = (tag or "").strip()
        if not TAG_RE.match(tag) or tag.startswith("-") or "@@sep@@" in tag:
            raise ValueError("a tag is 1-64 characters on one line")
        if not owner_tag(tag):
            raise ValueError("Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used| and Owner| belong to the "
                             "tools; pick another name")
        leaf = tag.split("|")[-1]
        with self.lock:
            ids = self.targets(ids)
            blocks = []
            for i in ids:
                it = self.items[i]
                if op == "add":
                    x = self.ensure_sidecar(it)
                    blocks.append(["-q", "-m", "-overwrite_original", "-sep", "@@sep@@",
                                   f"-XMP-lr:HierarchicalSubject-={tag}", f"-XMP-lr:HierarchicalSubject+={tag}",
                                   f"-XMP-dc:Subject-={leaf}", f"-XMP-dc:Subject+={leaf}", x])
                elif op == "remove":
                    if tag not in it["tags"]:
                        continue
                    b = ["-q", "-m", "-overwrite_original", "-sep", "@@sep@@", f"-XMP-lr:HierarchicalSubject-={tag}"]
                    if self.leaf_free(it, leaf, {tag}):
                        b.append(f"-XMP-dc:Subject-={leaf}")
                    blocks.append(b + [intake.sidecar(it["path"])])
                else:
                    raise ValueError("op is add or remove")
            self.run_exiftool(blocks)
            self.refresh(ids)
            return f"{'added' if op == 'add' else 'removed'} {tag!r} on {len(blocks)}"

    def rotate(self, ids, way):
        if way not in ("cw", "ccw"):
            raise ValueError("cw or ccw")
        with self.lock:
            ids = self.targets(ids)
            blocks, new = [], {}
            for i in ids:
                it = self.items[i]
                cur = DEG[effective_orientation(it)]
                o = ORIENT[(cur + (90 if way == "cw" else 270)) % 360]
                new[i] = o
                blocks.append(["-q", "-m", "-overwrite_original", "-n", f"-XMP-tiff:Orientation={o}",
                               self.ensure_sidecar(it)])
            self.run_exiftool(blocks)
            self.refresh(ids)
            for i in ids:
                self.drop_thumb(self.items[i])
            return f"rotated {len(ids)} (sidecar XMP-tiff:Orientation only; originals untouched)"

    # ---------------------------------------------- trash / erase (a detached jobs.py job, one at a time)
    def start_job(self, label_, argsets, ids):
        """Paths reach the worker on a pipe (private); the status file gets only (device, inode) pairs and a
        count. An erase is a redact job: its command lines carry no path and its output is scrubbed at the end."""
        redact = label_ == "erase"
        shown = [" ".join(a if a.startswith("--") else "'" + os.path.basename(a) + "'" if os.path.isabs(a) else a
                          for a in args) for args in argsets]
        with self.lock:
            paths = [self.items[i]["path"] for i in ids]
            files = [list(self.items[i]["fid"]) for i in ids if self.items[i].get("fid")]
            try:
                claim = jobs.claim(JOB_KEY)  # atomic across pages and servers; the worker inherits it
            except jobs.Busy:
                raise RuntimeError("another action is still running")
            with claim:
                st = claim.spawn("commands", dict(label=label_, files=files, count=len(ids)), total=len(argsets),
                                 private=dict(argsets=argsets, paths=paths, **({} if redact else {"shown": shown})),
                                 redact=redact)
            self._follow(st["id"], claim.tail)
            self.mark_busy(st)

    def _follow(self, jid, tail=None):
        """Point self.tail at job jid's log (with self.lock held)."""
        if self.tail is not None and self.tail.id == jid:
            return
        if self.tail is not None:
            self.tail.close()
        self.tail = tail or jobs.Tail(JOB_KEY, jid)

    def job_items(self, st):
        """Item ids of the files a job acts on (by device and inode, as its status file names them)."""
        out = []
        for f in (st.get("spec") or {}).get("files") or []:
            i = self.by_fid.get(tuple(f)) if isinstance(f, list) and len(f) == 2 else None
            if i is not None:
                out.append(i)
        return out

    def mark_busy(self, st):
        for i in self.job_items(st):
            self.items[i]["busy"] = True

    def sync(self):
        """The job's status, applied to the items: busy while it runs (whichever page or server started it),
        gone / not busy once it has finished. Survives a restart: everything comes from the status file."""
        st = jobs.status(JOB_KEY)
        if not st:
            return None
        with self.lock:
            if not st["done"] or st.get("step_running"):  # a crashed job's orphaned step may still delete them
                self.mark_busy(st)
            elif st.get("id") != self.reconciled:
                self.reconciled = st.get("id")
                for i in self.job_items(st):
                    it = self.items[i]
                    it.pop("busy", None)
                    if not os.path.lexists(it["path"]):
                        it["gone"] = True
                        if it.get("sha") and os.path.exists(os.path.join(THUMBS, it["sha"] + ".jpg")):
                            intake.gallery_thumb_forget(it["sha"])
                self.search_cache.clear()
        return st

    def job_view(self, since):
        st = self.sync()
        if not st:
            return dict(id=None, lines=[], next=0, done=True, ok=None, ids=[])
        with self.lock:
            self._follow(st["id"])
            tail = self.tail
        ls, nxt = tail.lines(since, st["done"])  # read after the status: a finished job has written every line
        return dict(jobs.public(st), lines=ls, next=nxt, ids=self.job_items(st))

    def trash(self, ids, confirm):
        if confirm is not True:
            raise ValueError("confirm the trash")
        with self.lock:
            ids = self.targets(ids)
            paths = [self.items[i]["path"] for i in ids]
            bad = [p for p in paths if not intake.trash_allowed(p) or under_state(p)]
            if bad:
                raise ValueError(f"{len(bad)} items are not library files")
        self.start_job("trash", [["trash", *paths]], ids)
        return f"trashing {len(ids)}"

    def erase(self, ids, confirm):
        if confirm != "ERASE":
            raise ValueError("type ERASE to confirm")
        with self.lock:
            ids = self.targets(ids)
            paths = [self.items[i]["path"] for i in ids]
            roots = [os.path.realpath(r) + os.sep for r in ROOTS]
            bad = [p for p in paths if under_state(p) or os.path.islink(p)
                   or not any(os.path.realpath(p).startswith(r) for r in roots)]
            if bad:
                raise ValueError(f"{len(bad)} items are not library files")
        args = ["forget", *paths, "--yes"] + (["--drive", self.drive] if self.drive else [])
        self.start_job("erase", [args], ids)
        return f"erasing {len(ids)} everywhere"

    def decide(self, keep, trash, confirm):
        """Trash suggestions: Keep (sidecar, now) then Trash (job)."""
        msg = []
        if keep:
            msg.append(self.keep(keep))
        if trash:
            msg.append(self.trash(trash, confirm))
        if not msg:
            raise ValueError("nothing decided")
        return "; ".join(msg)

def view_list(views):
    """Plugin tab dicts, checked: id a slug, title text, script a string."""
    out = []
    for v in views:
        if not isinstance(v, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", str(v.get("id", ""))) \
                or not isinstance(v.get("title"), str) or not isinstance(v.get("script", ""), str):
            raise SystemExit(f"plugin gallery view refused (needs id slug, title, script): {v!r:.120}")
        out.append(v)
    return out


def page(app):
    tabs = json.dumps([dict(id=v["id"], title=v["title"]) for v in app.views.values()]).replace("</", "<\\/")
    scripts = "".join(f"<script>\n{v.get('script', '')}\n</script>\n" for v in app.views.values())
    return (PAGE.replace("__CSRF__", html.escape(app.csrf)).replace("__VIEWS__", tabs)
            .replace("</body>", scripts + "</body>"))


class Handler(R.Handler):
    server_version = "gallery"
    app = None

    def cookie_name(self):
        return f"gallery_{self.app.port}"

    def do_GET(self):
        g = self.guard()
        if not g:
            return
        path, q = g
        app = self.app
        f = {k: v[0] for k, v in q.items() if k != "t"}
        if path == "/" and q.get("t"):
            self.reply(303, "", headers=[("Location", "/"), ("Set-Cookie", f"{self.cookie_name()}={app.token}; "
                                                                           "Path=/; HttpOnly; SameSite=Strict")])
        elif path == "/":
            self.reply(200, page(app), "text/html; charset=utf-8")
        elif path == "/api/facets":
            self.json(200, app.facets())
        elif path == "/api/list":
            app.sync()
            self.json(200, app.listing(f))
        elif path == "/api/job":
            try:
                since = max(0, int(f.get("since") or 0))
            except ValueError:
                since = 0
            self.json(200, app.job_view(since))
        elif m := re.fullmatch(r"/api/item/([0-9]{1,9})", path):
            app.sync()
            i = int(m.group(1))
            if not app.live(i):
                return self.reply(404, "404\n")
            with app.lock:
                self.json(200, app.detail(i))
        elif m := re.fullmatch(r"/([tmp])/([0-9]{1,9})", path):
            i = int(m.group(2))
            if not app.live(i) or app.items[i].get("busy"):
                return self.reply(404, "404\n")
            it = app.items[i]
            if not os.path.isfile(it["path"]) or os.path.islink(it["path"]) or under_state(it["path"]):
                return self.reply(404, "404\n")
            if m.group(1) == "t":
                try:
                    t = app.thumb(it)
                except OSError:
                    t = None
                if not t:
                    return self.reply(404, "404\n")
                self.media(dict(path=t, mime="image/jpeg"))
            elif m.group(1) == "m":
                if it["show"] not in ("image", "video"):
                    return self.reply(404, "404\n")
                self.media(dict(path=it["path"], mime=it["mime"]))
            else:
                data = image_jpeg(it["path"], PREVIEW_PX, 0) if it["media"] == "photo" else None
                if not data:
                    return self.reply(404, "404\n")
                self.reply(200, data, "image/jpeg")
        elif m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/media/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            got = v["media"](m.group(2)) if v and callable(v.get("media")) else None
            if not got or not os.path.isfile(got["path"]) or os.path.islink(got["path"]) \
                    or under_state(got["path"]) or label.refused([got["path"]], STATE):
                return self.reply(404, "404\n")
            self.media(dict(path=got["path"], mime=got["mime"]))
        elif m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            got = v["get"](m.group(2), f) if v and callable(v.get("get")) else None
            if got is None:
                return self.reply(404, "404\n")
            self.json(*got)
        else:
            self.reply(404, "404\n")

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
        acts = {
            "/api/keep": lambda: app.keep(body.get("ids")),
            "/api/tag": lambda: app.tag(body.get("ids"), body.get("tag"), body.get("op")),
            "/api/rotate": lambda: app.rotate(body.get("ids"), body.get("dir")),
            "/api/trash": lambda: app.trash(body.get("ids"), body.get("confirm")),
            "/api/erase": lambda: app.erase(body.get("ids"), body.get("confirm")),
            "/api/decide": lambda: app.decide(body.get("keep") or [], body.get("trash") or [], body.get("confirm")),
        }
        if path == "/api/close":
            self.json(200, {"result": "closing"})
            app.stop.set()
            return
        if m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            got = v["post"](m.group(2), body) if v and callable(v.get("post")) else None
            return self.json(*got) if got is not None else self.reply(404, "404\n")
        if path not in acts:
            return self.reply(404, "404\n")
        try:
            msg = acts[path]()
        except ValueError as e:
            return self.json(400, {"error": str(e)})
        except RuntimeError as e:
            return self.json(409, {"error": str(e)})
        self.json(200, {"result": msg})


def serve(app, port=0):
    Handler.app = app
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]

    def stopper():
        app.stop.wait()
        time.sleep(0.3)
        httpd.shutdown()

    threading.Thread(target=stopper, daemon=True).start()
    return httpd, f"http://127.0.0.1:{app.port}/?t={app.token}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local web gallery for the media library.")
    ap.add_argument("--drive", help="backup drive mount, passed to forget (Erase)")
    ap.add_argument("--port", type=int, default=0, help="0 = a random free port (default)")
    a = ap.parse_args(argv)
    if a.drive and not os.path.isdir(a.drive):
        sys.exit(f"--drive {a.drive}: not a directory")
    t0 = time.monotonic()
    jobs.sweep()  # a trash/erase that died while no page watched: marked crashed, an erase's output scrubbed
    items, skipped = scan_library()
    views = view_list([v for vs in plugins.call("gallery_views") for v in vs])
    app = App(items, os.path.abspath(a.drive) if a.drive else None, views)
    running = app.sync()  # a trash/erase started before a restart: its files stay busy until it ends
    threading.Thread(target=app.check_text, daemon=True).start()
    httpd, url = serve(app, a.port)
    print(f"xdg-open '{url}'", flush=True)
    reg = f" Plugin tabs: {', '.join(v['title'] for v in views)}." if views else ""
    print(f"({len(items)} items in {time.monotonic() - t0:.1f} s, {skipped} flagged/erased left out.{reg} "
          "A private browser window keeps it out of history. "
          "Ctrl-C or the Close button stops it.)", flush=True)
    if running and not running["done"]:
        print("(a trash/erase started earlier is still running; the page shows its progress)", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        st = jobs.status(JOB_KEY)
        print("stopped" + ("; the trash/erase keeps going on its own (<state>/jobs/gallery.json)"
                           if st and not st["done"] else ""))
        return 130
    finally:
        httpd.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="csrf" content="__CSRF__">
<title>Media gallery</title>
<style>
:root{--bg:#fff;--fg:#111;--mut:#5a5a5a;--line:#d0d0d0;--card:#f3f3f3;--acc:#1a56db;--keep:#1b7a33;--erase:#b3261e;--trash:#8a5a00}
@media (prefers-color-scheme: dark){:root{--bg:#121212;--fg:#eee;--mut:#aaa;--line:#3a3a3a;--card:#1e1e1e;--acc:#7aa7ff;--keep:#4cc26a;--erase:#ff6b5e;--trash:#e0a030}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button,select,input{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
.tab[aria-selected=true]{border-color:var(--acc);box-shadow:inset 0 -3px 0 var(--acc)}
#q{flex:1;min-width:12em}
#filters{display:flex;flex-wrap:wrap;gap:6px;padding:8px 16px;border-bottom:1px solid var(--line)}
#filters label{display:flex;gap:4px;align-items:center;color:var(--mut);font-size:14px}
#filters select,#filters input{font-size:14px;padding:4px 6px;max-width:14em}
#bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:6px 16px;color:var(--mut);font-size:14px}
#selbar{display:none;gap:6px;align-items:center}
#selbar.on{display:flex}
.k{border-color:var(--keep)}.t{border-color:var(--trash)}.e{border-color:var(--erase);color:var(--erase)}
main{padding:0 16px 40px}
h2.month{font-size:16px;margin:18px 0 8px;position:sticky;top:52px;background:var(--bg);padding:4px 0;z-index:2}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}
.big .grid{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.tile{position:relative;aspect-ratio:1;background:var(--card);border-radius:6px;overflow:hidden;cursor:pointer;outline:3px solid transparent}
.tile img{width:100%;height:100%;object-fit:cover;display:block}
.tile .ph{display:flex;align-items:center;justify-content:center;height:100%;color:var(--mut);font-size:13px;padding:6px;text-align:center;word-break:break-all}
.tile.sel{outline-color:var(--acc)}.tile.cur{box-shadow:0 0 0 3px var(--fg)}
.tile.dk{outline-color:var(--keep)}.tile.dt{outline-color:var(--trash)}
.tile input{position:absolute;top:6px;left:6px;width:22px;height:22px;z-index:2}
.badge{position:absolute;right:4px;bottom:4px;background:rgba(0,0,0,.65);color:#fff;font-size:12px;border-radius:4px;padding:1px 5px}
.flag{position:absolute;left:4px;bottom:4px;background:rgba(0,0,0,.65);color:#fff;font-size:11px;border-radius:4px;padding:1px 5px}
#sentinel{height:40px}
#viewer{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:20;display:none;color:#eee}
#viewer.on{display:flex}
#stage{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden;min-width:0}
#stage img,#stage video{max-width:100%;max-height:100vh;object-fit:contain;transition:transform .15s}
#side{width:340px;max-width:45vw;overflow:auto;padding:14px;background:#1b1b1b;font-size:14px}
#side h3{margin:0 0 6px;word-break:break-all;font-size:15px}
#side .row{margin:4px 0;color:#bbb;word-break:break-all}
#side button,#side input{background:#2a2a2a;color:#eee;border-color:#444;margin:3px 3px 3px 0}
.chip{display:inline-block;border:1px solid #555;border-radius:999px;padding:1px 8px;margin:2px;font-size:12px}
.chip button{border:0;background:none;padding:0 0 0 4px;margin:0;color:#f99}
@media (max-width:700px){#viewer.on{flex-direction:column}#side{width:auto;max-width:none;max-height:45vh}}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:30;display:none;align-items:center;justify-content:center;padding:16px}
.modal.on{display:flex}
.box{background:var(--bg);color:var(--fg);border-radius:10px;padding:18px;max-width:640px;width:100%;max-height:85vh;overflow:auto}
.box pre{white-space:pre-wrap;word-break:break-all;font-size:13px;background:var(--card);padding:8px;border-radius:6px;max-height:50vh;overflow:auto}
.note{color:var(--mut);font-size:14px;margin:6px 0}
.warn{color:var(--erase)}
@media (max-width:700px){</style></head><body>
<header>
 <button class="tab" id="vlib" aria-selected="true">Library</button>
 <button class="tab" id="vsug" aria-selected="false">Trash suggestions</button>
 <span id="ptabs"></span>
 <input id="q" type="search" placeholder="Search (semantic; tags and names if the model is missing)" aria-label="Search">
 <button id="close" title="Stop the gallery server">Close</button>
</header>
<div id="filters">
 <label>Project <select data-f="project"></select></label>
 <label>Source <select data-f="source"></select></label>
 <label>Event <select data-f="event"></select></label>
 <label>Place <select data-f="place"></select></label>
 <label>When <select data-f="ym"></select></label>
 <label>Label <select data-f="label"></select></label>
 <label>My tag <select data-f="mine"></select></label>
 <label>Review <select data-f="review"><option value="">any</option><option>maybe-trash</option><option>child-in-shot</option><option>untagged</option></select></label>
 <label>AI <select data-f="ai"><option value="">any</option><option value="trash">trash</option><option value="keep">keep</option><option value="none">not judged</option></select></label>
 <label>conf ≥ <input data-f="aimin" type="number" min="0" max="1" step="0.1" value="0.7" style="width:4.5em"></label>
 <label>Used <select data-f="used"><option value="">any</option><option value="used">used</option><option value="unused">unused</option></select></label>
 <label>for N days <input data-f="days" type="number" min="0" step="1" placeholder="ever" style="width:5em"></label>
 <label>Kind <select data-f="kind"><option value="">all</option><option value="photo">photos</option><option value="video">videos</option></select></label>
 <button id="clear">Clear</button>
</div>
<div id="bar"><span id="count"></span><span id="note"></span>
 <span id="selbar"><b id="seln"></b>
  <button class="k" id="bkeep">Keep (K)</button><button class="t" id="btrash">Trash (T)</button><button class="e" id="berase">Erase (E)</button>
  <button id="btag">+ tag</button><button id="buntag">− tag</button><button id="brot">Rotate ⟳</button><button id="bnone">Clear selection</button></span>
 <span id="sugbar" hidden><b id="sugn"></b> <button id="sugapply">Apply…</button></span>
</div>
<main id="main"><div id="list"></div><div id="sentinel"></div><div id="plugin" hidden></div></main>
<div id="viewer" role="dialog" aria-label="Item"><div id="stage"></div><div id="side"></div></div>
<div class="modal" id="modal" role="dialog" aria-modal="true" aria-labelledby="mh" data-dialog="confirm"><div class="box"><h3 id="mh"></h3><div id="mb"></div>
 <p id="mtype" hidden>Type <b>ERASE</b> to confirm: <input id="mtyped" autocomplete="off" spellcheck="false" aria-label="Type ERASE"></p>
 <div><button id="mgo">Confirm</button> <button id="mno">Cancel</button></div><pre id="mout" hidden></pre></div></div>
<script>
"use strict";
const CSRF=document.querySelector('meta[name=csrf]').content;
const $=s=>document.querySelector(s);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
let view="lib",items=[],total=0,loading=false,done=false,sel=new Set(),last=null,cur=-1,ranked=false,simId=null,dec={},vi=-1,facets=null;
async function api(path,body){const o={credentials:"same-origin"};if(body!==undefined){o.method="POST";o.headers={"Content-Type":"application/json","X-CSRF":CSRF};o.body=JSON.stringify(body)}
 const r=await fetch(path,o);let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||("HTTP "+r.status));return j}
function filt(){const f={};document.querySelectorAll("[data-f]").forEach(e=>{if(e.value)f[e.dataset.f]=e.value});
 if(f.ai!=="trash")delete f.aimin;if(f.used!=="unused")delete f.days;const q=$("#q").value.trim();if(q)f.q=q;if(simId!==null){f.similar=simId;delete f.q}
 if(view==="sug")f.view="suggest";return f}
function qs(o){return Object.entries(o).map(([k,v])=>encodeURIComponent(k)+"="+encodeURIComponent(v)).join("&")}
async function loadFacets(){facets=await api("/api/facets");
 const fill=(k,key,lab)=>{const s=document.querySelector(`[data-f=${k}]`),v=s.value;s.replaceChildren(el("option",null,"any"));s.firstChild.value="";
  (facets.facets[key]||[]).forEach(([n,c])=>{const o=el("option",null,(lab?lab(n):n)+" ("+c+")");o.value=n;s.append(o)});s.value=v};
 fill("project","project");fill("source","source");fill("event","event");fill("place","place");fill("label","label");fill("mine","mine");fill("ym","month",n=>n.length===4?n:"  "+n);
 $("#q").placeholder=facets.search?"Search tags and file names (semantic search off: "+facets.search+")":"Search (semantic, local SigLIP)"}
function reset(){items=[];total=0;done=false;cur=-1;$("#list").replaceChildren();sel.clear();dec={};paintSel();more()}
async function more(){if(loading||done||view.startsWith("p-"))return;loading=true;
 try{const j=await api("/api/list?"+qs(Object.assign(filt(),{offset:items.length,limit:240})));
  total=j.total;ranked=j.ranked;$("#note").textContent=j.note||"";const start=items.length;items=items.concat(j.items);done=items.length>=total;render(start);document.body.dataset.ready="1"}
 catch(e){$("#note").textContent="Error: "+e.message}finally{loading=false}
 $("#count").textContent=total+" items"+(view==="sug"?" suggested":"");
 if(!done&&$("#sentinel").getBoundingClientRect().top<innerHeight+600)more()}
function groupKey(it){return ranked?(view==="sug"?"Most likely junk first":"Best matches first"):it.mo}
function render(start){const L=$("#list");let g=L.lastElementChild&&L.lastElementChild.querySelector(".grid"),gk=L.lastElementChild?L.lastElementChild.dataset.k:null;
 for(let n=start;n<items.length;n++){const it=items[n],k=groupKey(it);
  if(k!==gk){const sec=el("section");sec.dataset.k=k;sec.append(el("h2","month",ranked?k:monthName(k)));g=el("div","grid");sec.append(g);L.append(sec);gk=k}
  g.append(tile(it,n))}paintSel()}
function monthName(k){const [y,m]=k.split("-");return new Date(+y,+m-1,1).toLocaleString(undefined,{month:"long",year:"numeric"})}
function tile(it,n){const t=el("div","tile");t.id="t"+it.id;t.dataset.n=n;t.dataset.item=it.n;
 const im=el("img");im.loading="lazy";im.decoding="async";im.alt=it.n;im.src="/t/"+it.id;im.onerror=()=>{im.replaceWith(el("div","ph",(it.m==="video"?"video: ":"")+it.n))};t.append(im);
 const cb=el("input");cb.type="checkbox";cb.setAttribute("aria-label","select "+it.n);cb.onclick=ev=>{ev.stopPropagation();pick(n,ev.shiftKey)};t.append(cb);
 if(it.m==="video")t.append(el("span","badge","▶"));
 const fl=[];if(it.c!=null)fl.push("AI "+it.c.toFixed(1));fl.push(...it.f);if(fl.length)t.append(el("span","flag",fl.join(" · ")));
 t.onclick=ev=>{if(ev.shiftKey||ev.ctrlKey){pick(n,ev.shiftKey);return}if(view==="sug"){cur=n;paintSel();return}openViewer(n)};return t}
function pick(n,range){if(range&&last!==null){const [a,b]=[Math.min(last,n),Math.max(last,n)];const on=!sel.has(items[n].id)||true;for(let i=a;i<=b;i++)on?sel.add(items[i].id):sel.delete(items[i].id)}
 else{const id=items[n].id;sel.has(id)?sel.delete(id):sel.add(id)}last=n;paintSel()}
function paintSel(){document.querySelectorAll(".tile").forEach(t=>{const id=+t.id.slice(1),n=+t.dataset.n;t.classList.toggle("sel",sel.has(id));t.querySelector("input").checked=sel.has(id);
  t.classList.toggle("cur",n===cur);t.classList.toggle("dk",dec[id]==="keep");t.classList.toggle("dt",dec[id]==="trash")});
 $("#selbar").classList.toggle("on",sel.size>0&&!view.startsWith("p-"));$("#seln").textContent=sel.size+" selected";
 const nk=Object.values(dec).filter(d=>d==="keep").length,nt=Object.values(dec).filter(d=>d==="trash").length;
 $("#sugbar").hidden=view!=="sug";$("#sugn").textContent=`${nk} keep / ${nt} trash marked (K / T, arrows move)`;$("#sugapply").disabled=!(nk+nt)}
function selIds(){return [...sel]}
function nameOf(id){const it=items.find(i=>i.id===id);return it?it.n:"#"+id}
// ---------------------------------------------------------------- actions
async function doKeep(ids){try{const j=await api("/api/keep",{ids});toast(j.result);refreshAfter(ids,false)}catch(e){alert("Keep refused: "+e.message)}}
function confirmBox(title,lines,needType,go){$("#mh").textContent=title;const b=$("#mb");b.replaceChildren();lines.forEach(l=>b.append(el("p",l.startsWith("!")?"warn":null,l.replace(/^!/,""))));
 $("#mtype").hidden=!needType;$("#mtyped").value="";$("#mgo").disabled=!!needType;$("#mgo").hidden=false;$("#mout").hidden=true;$("#mout").textContent="";delete $("#modal").dataset.job;$("#modal").classList.add("on");
 $("#mtyped").oninput=()=>{$("#mgo").disabled=$("#mtyped").value!=="ERASE"};if(needType)$("#mtyped").focus();
 $("#mgo").onclick=async()=>{$("#mgo").disabled=true;try{await go()}catch(e){$("#mout").hidden=false;$("#mout").textContent="Refused: "+e.message;$("#mgo").disabled=false}}}
$("#mno").onclick=()=>{$("#modal").classList.remove("on")};
function names(ids){const n=ids.map(nameOf);return n.slice(0,15).join(", ")+(n.length>15?` … and ${n.length-15} more`:"")}
function warnings(ids){const its=ids.map(id=>items.find(i=>i.id===id)).filter(Boolean),w=[];
 const u=its.filter(i=>i.f.includes("used")).length;if(u)w.push(`!${u} were used in posts (Used|…).`);
 const k=its.filter(i=>i.f.includes("kept")).length;if(k)w.push(`!${k} you marked Keep before.`);return w}
function askTrash(ids){confirmBox(`Trash ${ids.length} item${ids.length>1?"s":""}?`,[names(ids),"Plain delete (not erase): the file, its sidecar and thumbnails go, and intake never takes the same file in again. Use Erase for anything private.",...warnings(ids)],false,
 ()=>runJob("/api/trash",{ids,confirm:true},ids))}
function askErase(ids){confirmBox(`Erase ${ids.length} item${ids.length>1?"s":""} everywhere?`,[names(ids),"For sensitive files: intake.py forget moves them to quarantine and erases every copy in HOME (and the drive, if the gallery was started with --drive), with the sidecars, thumbnails and recent-file entries. This cannot be undone.",...warnings(ids)],true,
 ()=>runJob("/api/erase",{ids,confirm:$("#mtyped").value},ids))}
/* Trash and Erase run as a detached job (jobs.py): closing the page or stopping the server never stops
   them, and any page (a reload, a second tab, a restarted server) shows the running one from its status file. */
async function runJob(path,body,ids){try{await api(path,body)}catch(e){if(/still running/.test(e.message)){attachJob();return}throw e}
 progressMode();poll(0,ids)}
function progressMode(){$("#mgo").hidden=true;$("#mtype").hidden=true;$("#mout").hidden=false;$("#mout").textContent="";$("#modal").classList.add("on")}
function stepText(j){return j.step?` (step ${j.step}${j.total?" of "+j.total:""}: ${j.label})`:""}
async function poll(since,ids){let j;try{j=await api("/api/job?since="+since)}catch(e){$("#mout").textContent+="\n(server stopped; an action already started keeps going on its own)";return}
 if(j.lines.length){$("#mout").textContent+=j.lines.join("\n")+"\n";$("#mout").scrollTop=1e9}$("#modal").dataset.job=j.done?(j.state==="crashed"?"crashed":j.ok?"done":"failed"):"running";
 if(!j.done){$("#mh").textContent="Running"+stepText(j)+"; closing the page does not stop it";setTimeout(()=>poll(j.next,ids),600);return}
 $("#mh").textContent=j.state==="crashed"?"The action stopped unexpectedly (crashed)"+stepText(j)+(j.step_running?"; the step it started is still running":"")+". Nothing more runs by itself."
  :j.ok?"Done":"Finished with problems (see below)";
 ids.forEach(id=>sel.delete(id));closeViewer();loadFacets();reset()}
async function attachJob(){let j;try{j=await api("/api/job")}catch(e){return}if(j.done)return;$("#mh").textContent="An action is running";$("#mb").replaceChildren();progressMode();poll(0,j.ids||[])}
async function tagAsk(op){const ids=vi>=0&&!sel.size?[items[vi].id]:selIds();if(!ids.length)return;
 const t=prompt((op==="add"?"Add":"Remove")+" a tag of yours on "+ids.length+" item(s). Not Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used|, Owner| (the tools' own):");
 if(!t)return;try{const j=await api("/api/tag",{ids,tag:t,op});toast(j.result);loadFacets();refreshAfter(ids,false)}catch(e){alert("Refused: "+e.message)}}
async function rot(ids,dir){try{const j=await api("/api/rotate",{ids,dir});toast(j.result);refreshAfter(ids,true)}catch(e){alert("Refused: "+e.message)}}
function refreshAfter(ids,thumbs){if(thumbs)ids.forEach(id=>{const t=$("#t"+id);if(t){const im=t.querySelector("img");if(im)im.src="/t/"+id+"?r="+Date.now()}});
 if(vi>=0)openViewer(vi);else if(view==="sug")reset()}
function toast(s){$("#note").textContent=s}
$("#bkeep").onclick=()=>doKeep(selIds());$("#btrash").onclick=()=>askTrash(selIds());$("#berase").onclick=()=>askErase(selIds());
$("#btag").onclick=()=>tagAsk("add");$("#buntag").onclick=()=>tagAsk("remove");$("#brot").onclick=()=>rot(selIds(),"cw");$("#bnone").onclick=()=>{sel.clear();paintSel()};
$("#sugapply").onclick=()=>{const keep=Object.keys(dec).filter(k=>dec[k]==="keep").map(Number),trash=Object.keys(dec).filter(k=>dec[k]==="trash").map(Number);
 confirmBox(`Apply: ${keep.length} keep, ${trash.length} trash?`,[keep.length?"Keep: Owner|keep + Owner|not-trash in the sidecar (never suggested again).":"",trash.length?"Trash ("+names(trash)+"): plain delete, never taken in again.":""].filter(Boolean),false,
  async()=>{const j=await api("/api/decide",{keep,trash,confirm:true});if(trash.length){$("#mgo").hidden=true;$("#mout").hidden=false;$("#mout").textContent=j.result+"\n";poll(0,trash)}else{$("#modal").classList.remove("on");toast(j.result);reset()}})};
// ---------------------------------------------------------------- viewer
async function openViewer(n){if(n<0||n>=items.length)return;vi=n;cur=n;let d;try{d=await api("/api/item/"+items[n].id)}catch(e){toast("gone");return}
 const st=$("#stage");st.replaceChildren();
 if(d.src&&d.video){const v=el("video");v.controls=true;v.autoplay=true;v.preload="metadata";v.poster="/t/"+d.id;v.src=d.src;v.style.transform=`rotate(${d.rot}deg)`;st.append(v)}
 else if(d.src){const im=el("img");im.alt=d.name;im.src=d.src;im.style.transform=`rotate(${d.rot}deg)`;im.onerror=()=>im.replaceWith(el("p",null,"No preview for this format in the browser"));st.append(im)}
 else st.append(el("p",null,"No preview for this format in the browser"));
 $("#viewer").dataset.current=d.name;const s=$("#side");s.replaceChildren(el("h3",null,d.name));
 const row=(k,v)=>{if(v)s.append(el("div","row",k+": "+v))};
 row("Date",d.date);row("Place",d.place);row("Event",d.event);row("Project",d.project);row("Source",d.source);row("Folder",d.folder);row("Resolution",d.res);row("Size",d.size);
 if(d.ai!=null)row("AI trash confidence",d.ai.toFixed(1));if(d.used.length)row("Used in",d.used.join(", "));if(d.kept)row("Owner","kept");
 if(d.video)s.append(el("div","row","If it doesn't play, the browser can't decode this codec (often HEVC); the thumbnail is still right."));
 const tg=el("div");tg.append(el("div","row","Tags:"));d.tags.forEach(t=>{const c=el("span","chip",t);if(d.mine.includes(t)){const x=el("button",null,"×");x.title="remove my tag";x.onclick=async()=>{try{await api("/api/tag",{ids:[d.id],tag:t,op:"remove"});openViewer(n);loadFacets()}catch(e){alert(e.message)}};c.append(x)}tg.append(c)});
 if(!d.tags.length)tg.append(el("span","chip","none"));s.append(tg);
 const add=el("input");add.placeholder="add my tag, Enter";add.onkeydown=async ev=>{if(ev.key==="Enter"&&add.value.trim()){ev.stopPropagation();try{await api("/api/tag",{ids:[d.id],tag:add.value.trim(),op:"add"});loadFacets();openViewer(n)}catch(e){alert(e.message)}}};s.append(add);
 const b=el("div");const mk=(t,f,c)=>{const x=el("button",c,t);x.onclick=f;b.append(x)};
 mk("Keep (K)",()=>doKeep([d.id]),"k");mk("Trash (T)",()=>askTrash([d.id]),"t");mk("Erase (E)",()=>askErase([d.id]),"e");
 mk("⟲ Rotate (Shift+R)",()=>rot([d.id],"ccw"));mk("⟳ Rotate (R)",()=>rot([d.id],"cw"));mk("Similar",()=>{simId=d.id;closeViewer();$("#q").value="";reset()});
 mk("◀ Prev",()=>openViewer(vi-1));mk("Next ▶",()=>openViewer(vi+1));mk("Close (Esc)",closeViewer);s.append(b);
 s.append(el("div","note","Rotate writes XMP-tiff:Orientation in the sidecar only; the original is never touched. No crop here: make crops as copies in an editor."));
 $("#viewer").classList.add("on");if(n>=items.length-20)more()}
function closeViewer(){vi=-1;$("#viewer").classList.remove("on");$("#stage").replaceChildren();paintSel()}
// ---------------------------------------------------------------- keys
document.addEventListener("keydown",ev=>{if((ev.target.tagName==="INPUT"&&ev.target.type!=="checkbox")||ev.target.tagName==="SELECT"||ev.ctrlKey||ev.metaKey||ev.altKey||$("#modal").classList.contains("on")){if(ev.key==="Escape"&&$("#modal").classList.contains("on")){$("#modal").classList.remove("on");ev.target.blur()}return}
 const k=ev.key;
 if(vi>=0){const id=items[vi].id;
  if(k==="Escape")closeViewer();else if(k==="ArrowRight")openViewer(vi+1);else if(k==="ArrowLeft")openViewer(vi-1);
  else if(k==="k"||k==="K")doKeep([id]);else if(k==="t"||k==="T")askTrash([id]);else if(k==="e"||k==="E")askErase([id]);
  else if(k==="r")rot([id],"cw");else if(k==="R")rot([id],"ccw");else return;ev.preventDefault();return}
 if(view==="sug"){if(k==="ArrowRight"||k==="ArrowDown"||k==="j"){cur=Math.min(items.length-1,cur+1)}else if(k==="ArrowLeft"||k==="ArrowUp"){cur=Math.max(0,cur-1)}
  else if((k==="k"||k==="K"||k==="t"||k==="T")&&cur>=0){dec[items[cur].id]=k.toLowerCase()==="k"?"keep":"trash";cur=Math.min(items.length-1,cur+1)}
  else if(k==="Enter"&&cur>=0){openViewer(cur)}else return;ev.preventDefault();paintSel();const t=$("#t"+(items[cur]||{}).id);if(t)t.scrollIntoView({block:"nearest"});if(cur>=items.length-10)more();return}
 if(!sel.size)return;if(k==="k"||k==="K")doKeep(selIds());else if(k==="t"||k==="T")askTrash(selIds());else if(k==="e"||k==="E")askErase(selIds());else if(k==="Escape"){sel.clear();paintSel()}else return;ev.preventDefault()});
// ---------------------------------------------------------------- views
const VIEWS=__VIEWS__;window.mediaIntakeViews=window.mediaIntakeViews||{};
VIEWS.forEach(v=>{const b=el("button","tab",v.title);b.id="vp-"+v.id;b.setAttribute("aria-selected","false");b.onclick=()=>setView("p-"+v.id);$("#ptabs").append(b)});
function setView(v){view=v;simId=null;const plug=v.startsWith("p-");
 ["lib","sug",...VIEWS.map(x=>"p-"+x.id)].forEach(x=>$("#v"+x).setAttribute("aria-selected",x===v));
 $("#main").classList.toggle("big",v==="sug");$("#list").hidden=plug;$("#sentinel").hidden=plug;$("#plugin").hidden=!plug;$("#filters").hidden=plug;
 if(plug){$("#selbar").classList.remove("on");$("#sugbar").hidden=true;const id=v.slice(2),P=$("#plugin"),pv=window.mediaIntakeViews[id];P.replaceChildren();
  if(!pv||typeof pv.render!=="function"){P.append(el("p","warn","Plugin view "+id+" has no render()"));return}
  pv.render(P,{el,api:(p,b)=>api("/plugin/"+id+"/"+p,b),media:p=>"/plugin/"+id+"/media/"+p})}
 else{if(v==="sug")$("#note").textContent="K = keep, T = trash on the outlined tile; nothing changes until Apply.";reset()}}
$("#vlib").onclick=()=>setView("lib");$("#vsug").onclick=()=>setView("sug");
document.querySelectorAll("[data-f]").forEach(e=>e.onchange=()=>{simId=null;reset()});
$("#clear").onclick=()=>{document.querySelectorAll("[data-f]").forEach(e=>{e.value=e.dataset.f==="aimin"?"0.7":""});$("#q").value="";simId=null;reset()};
let qt=null;$("#q").oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{simId=null;reset()},500)};
$("#close").onclick=async()=>{if(!confirm("Stop the gallery server?"))return;try{await api("/api/close",{})}catch(e){}document.body.replaceChildren(el("p",null,"Gallery stopped. You can close this tab."))};
new IntersectionObserver(es=>{if(es.some(e=>e.isIntersecting))more()},{rootMargin:"800px"}).observe($("#sentinel"));
loadFacets().then(()=>reset()).then(attachJob).catch(e=>{$("#note").textContent="Error: "+e.message});
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
