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
only). A left sidebar holds the views with counts (/api/facets): Library, Trash
suggestions, any tab a plugin adds (plugins.py gallery_views(), docs/plugins.md) and
the quick views Recently added, Possible child, Untagged; then search and the filters.
Selection actions sit in a bottom bar shown only while something is selected. Media
the browser can't decode opens in the desktop's player (xdg-open, a POST naming one
item id, argument list, library files only). Trash and Erase run as a detached
job (jobs.py, key "gallery", one at a time): closing the page or stopping the
server never stops one, and a restarted server or another page shows it again.
The paths go to the worker on a pipe; the status file names the files only by
(device, inode), and an Erase's output is scrubbed of names when it ends.

Server security is review_ui.py's: stdlib only, everything inline, 127.0.0.1 on a
random port, random token swapped for an HttpOnly SameSite=Strict cookie, Host
check, CSRF value on every POST, no-store + strict CSP, media by opaque id only.
Originals are never written: sidecars (<file>.<ext>.xmp) only. docs/gallery.md.
"""
import argparse, contextlib, datetime, html, http.server, io, json, os, re, secrets, shutil, subprocess, sys, tempfile
import threading, time, urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intake  # noqa: E402  (constants, sidecar helpers, trash_allowed, exiftool_batch)
import jobs  # noqa: E402  (trash / erase run as a detached job: <state>/jobs/gallery.*)
import library_lock  # noqa: E402  (Keep, my tags, Rotate write sidecars under the library lock)
import label  # noqa: E402  (flagged / refused: stdlib only at import)
import owner  # noqa: E402  (owner decisions: Owner|keep / not-trash / trash)
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
OPENER = "xdg-open"  # the desktop's default app, for media the browser can't decode (found on PATH)
RECENT_DAYS = 30  # "Recently added": filed into the library in the last N days

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
    it["kept"] = owner.kept(tags, it.get("pick", ""))
    it["trashed"] = owner.trashed(tags)  # a surviving copy of content you trashed


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
                                  show=k, mime=mime, size=st.st_size, mtime=st.st_mtime, ctime=st.st_ctime, sha=sha,
                                  gone=False, fid=(st.st_dev, st.st_ino)))
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
class LockBusy(RuntimeError):
    """App.writing() could not take the library lock (an HTTP 409; its message names the holder)."""


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
                    + (["kept"] if it["kept"] else []) + (["trashed copy"] if it["trashed"] else [])
                    + (["used"] if it["used"] else []))

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
        counts = Counter()  # the sidebar's view and option counts (whole library, not cross-filtered)
        recent = time.time() - RECENT_DAYS * 86400
        n = 0
        for it in self.items:
            if it["gone"]:
                continue
            n += 1
            t = it["tags"]
            counts["kind:" + it["media"]] += 1
            for r in ("maybe-trash", "child-in-shot"):
                counts[f"review:{r}"] += f"Review|{r}" in t
            counts["review:untagged"] += not t or any(x.startswith("Review|untagged-") for x in t)
            counts["ai:trash"] += it["ai_trash"] is not None
            counts["ai:keep"] += it["ai_keep"]
            counts["ai:none"] += not any(x.startswith("AI|") for x in t)
            counts["used:used" if it["used"] else "used:unused"] += 1
            counts["suggest"] += self.suggested(it)
            counts["recent"] += it.get("ctime", 0) >= recent
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
        search = None if self.text_why is None and self.text_checked.is_set() \
            else (self.text_why or "checking the search model...")
        return dict(total=n, facets=out, counts=dict(counts), recent_days=RECENT_DAYS, search=search)

    def suggested(self, it):
        if it["kept"] or it["used"] or f"Project|{it['project']}" in PROTECTED or PROTECTED.intersection(it["tags"]):
            return False
        return it["trashed"] or "Review|maybe-trash" in it["tags"] or (it["ai_trash"] or 0) >= SUGGEST_MIN

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
        if f.get("added"):  # filed into the library in the last N days (inode change time: a copy or a move)
            try:
                days = int(f["added"])
            except ValueError:
                days = 0
            if days > 0 and it.get("ctime", 0) < time.time() - days * 86400:
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

    @contextlib.contextmanager
    def writing(self, what):
        """The library lock around one sidecar write (taken before self.lock, never waited on: an HTTP request
        gets 409 naming the holder, e.g. a CLI apply or a review run, at once)."""
        try:
            fd = library_lock.acquire(f"gallery {what}")
        except library_lock.Busy as e:
            raise LockBusy(str(e)) from None
        try:
            yield
        finally:
            os.close(fd)

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
        with self.writing("keep"), self.lock:
            ids = self.targets(ids)
            blocks = []
            for i in ids:
                it = self.items[i]
                x = self.ensure_sidecar(it)
                # The same rule as the review page's Keep (intake.keep_args).
                leaves = {t.split("/")[-1] for t in it["tagslist"]}
                blocks.append(["-q", "-m", "-overwrite_original", "-sep", "@@sep@@",
                               *intake.keep_args(it["tags"], leaves), x])
            self.run_exiftool(blocks)
            self.refresh(ids)
            return f"kept {len(ids)}: Owner|keep + Owner|not-trash, Review|maybe-trash and Owner|trash removed"

    def tag(self, ids, tag, op):
        tag = (tag or "").strip()
        if not TAG_RE.match(tag) or tag.startswith("-") or "@@sep@@" in tag:
            raise ValueError("a tag is 1-64 characters on one line")
        if not owner_tag(tag):
            raise ValueError("Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used| and Owner| belong to the "
                             "tools; pick another name")
        leaf = tag.split("|")[-1]
        with self.writing("tag"), self.lock:
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
        with self.writing("rotate"), self.lock:
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
            except jobs.Busy as e:  # this page's last job, another page's, or the library lock (says who)
                raise RuntimeError(f"another action is still running: {e}")
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
                sizes = set()
                for i in self.job_items(st):
                    it = self.items[i]
                    it.pop("busy", None)
                    if not os.path.lexists(it["path"]):
                        self.drop_gone(it)
                        sizes.add(it["size"])
                self.reread_copies(sizes)
                self.search_cache.clear()
        return st

    def drop_gone(self, it):
        it["gone"] = True
        if it.get("sha") and os.path.exists(os.path.join(THUMBS, it["sha"] + ".jpg")):
            intake.gallery_thumb_forget(it["sha"])

    def reread_copies(self, sizes):
        """After a job deleted files: other library copies of that content changed on disk too. trash marks the
        ones it leaves with Owner|trash (intake.mark_trashed_copies) and forget deletes them, so each live item
        that could be one (the same size: a copy of the same sha256 always is) is re-read from disk."""
        if not sizes:
            return
        for it in self.items:
            if it["gone"] or it.get("busy") or it["size"] not in sizes:
                continue
            if not os.path.lexists(it["path"]):
                self.drop_gone(it)
            else:
                load_sidecar(it)

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

    def servable(self, i):
        """The path of item i when the media routes may serve it: live, not in a running job, a regular file
        (no symlink) outside the state folder. Else None."""
        if not self.live(i) or self.items[i].get("busy"):
            return None
        p = self.items[i]["path"]
        if not os.path.isfile(p) or os.path.islink(p) or under_state(p):
            return None
        return p

    def library_file(self, i):
        """servable(i), and also inside a library root after resolving links and never flagged, quarantined or
        erased (label.refused): what may be handed to another program."""
        p = self.servable(i)
        if not p or not os.path.isabs(p):
            return None
        rp = os.path.realpath(p)
        if not any(rp.startswith(os.path.realpath(r) + os.sep) for r in ROOTS) or label.refused([p], STATE):
            return None
        return p

    def open_external(self, ids):
        """Open one library file in the desktop's default app (xdg-open), for media the browser can't decode.
        Only on a POST from the page (token, CSRF, Origin), one id, an argument list (no shell)."""
        if not isinstance(ids, list) or len(ids) != 1:
            raise ValueError("one item")
        with self.lock:
            i = self.targets(ids)[0]
            p = self.library_file(i)
            if not p:
                raise ValueError("not a library file")
        try:
            proc = subprocess.Popen([OPENER, p], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except OSError as e:
            raise RuntimeError(f"{OPENER} failed: {e.strerror or type(e).__name__}") from None
        threading.Thread(target=proc.wait, daemon=True).start()  # reap it; xdg-open hands off and exits
        return f"opened {os.path.basename(p)} in the system player"

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
            if not app.servable(i):
                return self.reply(404, "404\n")
            it = app.items[i]
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
            "/api/open": lambda: app.open_external(body.get("ids")),
            "/api/decide": lambda: app.decide(body.get("keep") or [], body.get("trash") or [], body.get("confirm")),
        }
        if path == "/api/close":
            self.json(200, {"result": "closing"})
            app.stop.set()
            return
        if m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            if not v or not callable(v.get("post")):
                return self.reply(404, "404\n")
            # A plugin POST may write sidecars or library files, so it runs under the library lock like Keep,
            # tags and Rotate (409 naming the holder, at once, while a job or the CLI writes). Only a view that
            # declares post_read_only=True is called without it (docs/plugins.md).
            try:
                with contextlib.nullcontext() if v.get("post_read_only") is True \
                        else app.writing(f"plugin {m.group(1)}"):
                    got = v["post"](m.group(2), body)
            except LockBusy as e:
                return self.json(409, {"error": str(e)})
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
<meta name="color-scheme" content="light dark">
<title>Media gallery</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--fg:#16181d;--mut:#5d6470;--line:#e2e5ea;--card:#e9ecf0;--hover:#eef0f4;--acc:#2456d3;--acc-soft:rgba(36,86,211,.10);
 --keep:#1b7a33;--trash:#8a5a00;--erase:#b3261e;--shadow:0 6px 24px rgba(16,24,40,.14);
 --sbw:280px;--ml:280px;--hh:56px;--tile:168px}
@media (prefers-color-scheme: dark){:root{--bg:#0f1013;--panel:#17191d;--fg:#e8eaed;--mut:#9aa1ad;--line:#2a2d34;--card:#22252b;--hover:#1f2228;--acc:#8ab0ff;--acc-soft:rgba(138,176,255,.14);
 --keep:#4cc26a;--trash:#e0a030;--erase:#ff6b5e;--shadow:0 6px 24px rgba(0,0,0,.5)}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
body.noscroll{overflow:hidden}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}
:focus{outline:none}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:6px}
[hidden]{display:none!important}
.ic{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.btn{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);font-size:14px;white-space:nowrap}
.btn:hover:not(:disabled){background:var(--hover)}
.btn.keep{color:var(--keep);border-color:color-mix(in srgb,var(--keep) 45%,transparent);background:color-mix(in srgb,var(--keep) 9%,var(--panel))}
.btn.trash{color:var(--trash);border-color:color-mix(in srgb,var(--trash) 45%,transparent);background:color-mix(in srgb,var(--trash) 10%,var(--panel))}
.btn.erase{color:var(--erase);border-color:color-mix(in srgb,var(--erase) 45%,transparent);background:color-mix(in srgb,var(--erase) 9%,var(--panel))}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff}
kbd{font:600 11px/1 ui-monospace,monospace;border:1px solid currentColor;border-radius:4px;padding:2px 4px;opacity:.7}
.icon{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:0;border-radius:8px;background:none;color:var(--mut);position:relative}
.icon:hover{background:var(--hover);color:var(--fg)}
.link{border:0;background:none;color:var(--acc);padding:2px 0;font-size:13px}
.link:hover{text-decoration:underline}
.note{color:var(--mut);font-size:14px;margin:6px 0}
.warn{color:var(--erase)}
.empty{color:var(--mut);font-size:13px;padding:4px 8px}
.empty.big{font-size:15px;padding:48px 0;text-align:center}

/* ---------------------------------------------------------------- sidebar */
#sidebar{position:fixed;top:0;left:0;bottom:0;width:var(--sbw);background:var(--panel);border-right:1px solid var(--line);overflow-y:auto;overflow-x:hidden;z-index:15;display:flex;flex-direction:column;overscroll-behavior:contain}
.sb-top{display:flex;align-items:center;gap:8px;padding:10px 12px 6px;height:var(--hh)}
.brand{font-weight:650;font-size:15px;white-space:nowrap}
#views{padding:4px 10px 8px;display:flex;flex-direction:column;gap:2px}
#ptabs{display:contents}
.nvhead{font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);padding:12px 10px 4px}
.nv{display:flex;align-items:center;gap:10px;height:36px;padding:0 10px;border:0;border-radius:8px;background:none;text-align:left;width:100%;color:var(--fg)}
.nv:hover{background:var(--hover)}
.nv[aria-current=page]{background:var(--acc-soft);color:var(--acc);font-weight:600}
.nv .lbl{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ct{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums;font-weight:400}
.nv[aria-current=page] .ct{color:var(--acc)}
.railonly{display:none}
.sb-search{padding:8px 16px 4px;border-top:1px solid var(--line)}
.field{position:relative}
.field .ic{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--mut);pointer-events:none}
#q{width:100%;height:36px;padding:0 10px 0 34px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
#q:focus-visible{outline-offset:0}
.hint{font-size:12px;color:var(--mut);margin:6px 2px 0}
.hint summary{cursor:pointer;list-style:none}
.hint summary::-webkit-details-marker{display:none}
.hint summary::after{content:" (why)";color:var(--acc)}
.hint .why{margin-top:4px;word-break:break-word;font-family:ui-monospace,monospace;font-size:11px}
#filters{padding:6px 0 24px}
.fhead{display:flex;align-items:baseline;justify-content:space-between;padding:10px 16px 4px}
.fhead b{font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.fs{border-top:1px solid var(--line)}
.fs:first-of-type{border-top:0}
.fs h3{margin:0;font-size:inherit}
.fs-h{display:flex;align-items:center;gap:8px;width:100%;height:40px;padding:0 16px;border:0;background:none;text-align:left;font-weight:600;font-size:14px}
.fs-h:hover{background:var(--hover)}
.fs-t{flex:none}
.fs-v{flex:1;min-width:0;color:var(--acc);font-weight:500;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:right}
.chev{width:14px;height:14px;color:var(--mut);transition:transform .15s}
.fs-h[aria-expanded=true] .chev{transform:rotate(180deg)}
.fs-b{padding:0 12px 12px}
.olist{display:flex;flex-direction:column;gap:1px;max-height:232px;overflow-y:auto}
.opt{display:flex;align-items:center;gap:8px;width:100%;min-height:30px;padding:4px 8px;border:0;border-radius:6px;background:none;text-align:left;font-size:14px;color:var(--fg)}
.opt:hover{background:var(--hover)}
.opt .on{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opt[aria-pressed=true]{background:var(--acc-soft);color:var(--acc);font-weight:600}
.opt[aria-pressed=true] .ct{color:var(--acc)}
.ofind{width:100%;height:30px;margin:0 0 6px;padding:0 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);font-size:13px}
.seg{display:grid;grid-template-columns:repeat(3,1fr);gap:2px;padding:3px;background:var(--card);border-radius:9px}
.seg button{border:0;border-radius:7px;background:none;padding:5px 4px;font-size:13px;display:flex;flex-direction:column;align-items:center;line-height:1.2;color:var(--mut)}
.seg button[aria-pressed=true]{background:var(--panel);color:var(--fg);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.15)}
.seg .ct{font-size:11px}
.yr{display:flex;align-items:center}
.yr .opt{flex:1}
.caret{width:26px;height:30px;border:0;background:none;color:var(--mut);border-radius:6px;font-size:11px;flex:none;transition:transform .15s}
.caret[aria-expanded=true]{transform:rotate(90deg)}
.caret:hover{background:var(--hover)}
.mos{padding-left:26px;display:flex;flex-direction:column;gap:1px}
.sub{margin-top:10px;padding:10px 8px 2px;border-top:1px dashed var(--line);font-size:13px;color:var(--mut)}
.sub label{display:flex;justify-content:space-between;align-items:center;gap:8px}
.sub input[type=range]{width:100%;accent-color:var(--acc);margin-top:6px}
.sub input[type=number]{width:5.5em;height:30px;padding:0 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
output{font-variant-numeric:tabular-nums;color:var(--fg);font-weight:600}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:14;display:none}
.overlay #scrim{display:block}
/* collapsed to an icon rail */
.collapsed{--sbw:60px;--ml:60px}
.collapsed #sidebar .lbl,.collapsed #sidebar .nv .ct,.collapsed #sidebar .brand,.collapsed #sidebar .nvhead,
.collapsed .sb-search,.collapsed #filters{display:none}
.collapsed #views{padding:4px 8px}
.collapsed .nv{justify-content:center;padding:0;height:40px}
.collapsed .railonly{display:flex}
.badge-n{position:absolute;top:3px;right:3px;min-width:16px;height:16px;border-radius:8px;background:var(--acc);color:#fff;font-size:10px;line-height:16px;text-align:center;padding:0 4px}
.overlay{--ml:60px;--sbw:min(304px,86vw)}
.overlay #sidebar{box-shadow:var(--shadow)}

/* ---------------------------------------------------------------- main */
#app{margin-left:var(--ml);min-height:100vh;min-width:0}
#top{position:sticky;top:0;z-index:10;height:var(--hh);display:flex;align-items:center;gap:12px;padding:0 24px;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
#vtitle{font-size:17px;font-weight:650;margin:0;white-space:nowrap}
#count{color:var(--mut);font-size:14px;white-space:nowrap;font-variant-numeric:tabular-nums}
.grow{flex:1}
.size{display:flex;align-items:center;gap:8px;color:var(--mut)}
#tsize{width:120px;accent-color:var(--acc)}
#chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:12px 24px 0}
.chip{display:inline-flex;align-items:center;gap:2px;height:28px;padding:0 4px 0 10px;border-radius:999px;background:var(--acc-soft);color:var(--acc);font-size:13px;max-width:100%}
.chip>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip .x{border:0;background:none;color:inherit;width:22px;height:22px;border-radius:50%;font-size:15px;line-height:1}
.chip .x:hover{background:var(--acc-soft)}
#note{padding:0 24px;margin:10px 0 0}
main{padding:4px 24px 48px}
.barup main{padding-bottom:110px}
h2.month{position:sticky;top:var(--hh);z-index:2;margin:0;padding:16px 0 8px;font-size:14px;font-weight:650;background:var(--bg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--tile),1fr));gap:6px}
[data-view=sug] .grid{grid-template-columns:repeat(auto-fill,minmax(max(var(--tile),220px),1fr));gap:10px}
.tile{position:relative;aspect-ratio:1;background:var(--card);border-radius:8px;overflow:hidden;cursor:pointer;outline:3px solid transparent;outline-offset:-3px;transition:outline-color .1s}
.tile img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .2s}
.tile:hover img{transform:scale(1.03)}
.tile:focus-visible{outline:3px solid var(--acc);outline-offset:-3px;border-radius:8px}
.tile .ph{display:flex;align-items:center;justify-content:center;height:100%;color:var(--mut);font-size:12px;padding:8px;text-align:center;word-break:break-all}
.tile.sel{outline-color:var(--acc)}
.tile.sel::after{content:"";position:absolute;inset:0;background:var(--acc-soft);pointer-events:none}
.tile.cur{box-shadow:inset 0 0 0 3px var(--fg)}
.tile.dk{outline-color:var(--keep)}.tile.dt{outline-color:var(--trash)}
.tile .ck{position:absolute;top:6px;left:6px;z-index:2;width:22px;height:22px;margin:0;accent-color:var(--acc);cursor:pointer}
@media (hover:hover){.tile .ck{opacity:0;transition:opacity .1s}.tile:hover .ck,.tile:focus-within .ck,.selecting .tile .ck{opacity:1}}
.play{position:absolute;right:6px;top:6px;width:24px;height:24px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;padding-left:2px}
.flag{position:absolute;left:6px;bottom:6px;width:max-content;max-width:calc(100% - 12px);background:rgba(0,0,0,.62);color:#fff;font-size:11px;border-radius:4px;padding:1px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sentinel{height:40px}
/* bottom action bar: only while something is selected (or the Trash suggestions view's Apply) */
#actionbar{position:fixed;left:calc(var(--ml) + 16px);right:16px;bottom:16px;z-index:12;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:center;padding:10px 14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.grp{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
#seln,#sugn{font-weight:650;font-size:14px;margin-right:4px;white-space:nowrap}
.sep{width:1px;height:22px;background:var(--line)}
#toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:40;background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:14px;max-width:min(560px,90vw);box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:opacity .2s}
#toast.on{opacity:1}

/* ---------------------------------------------------------------- lightbox */
#viewer{position:fixed;inset:0;background:#0b0b0d;z-index:20;display:none;color:#e9e9ec}
#viewer.on{display:flex}
#stage{flex:1;position:relative;overflow:hidden;min-width:0}
#media{position:absolute;inset:16px}
#media>img,#media>video{position:absolute;left:50%;top:50%;width:100%;height:100%;object-fit:contain;transform:translate(-50%,-50%);background:transparent;transition:transform .15s}
#media .nop{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;margin:0}
#media .nop img{max-width:100%;max-height:calc(100% - 120px);object-fit:contain;border-radius:6px;min-height:40%}
.cant{max-width:640px;font-size:14px;color:#d7d7dc;background:rgba(255,255,255,.07);border-radius:8px;padding:10px 14px;display:flex;flex-direction:column;gap:8px;align-items:center}
.cant button{border:1px solid #4a4b52;background:#202127;color:#fff;border-radius:8px;height:32px;padding:0 12px}
.arrow{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:64px;border:0;border-radius:10px;background:rgba(255,255,255,.08);color:#fff;font-size:28px;z-index:3}
.arrow:hover{background:rgba(255,255,255,.18)}
#vprev{left:12px}#vnext{right:12px}
#unmute{position:absolute;left:16px;top:16px;z-index:3;border:0;border-radius:8px;background:rgba(0,0,0,.6);color:#fff;padding:6px 12px;font-size:14px}
#info{width:360px;flex:none;overflow-y:auto;background:#16171b;border-left:1px solid #2a2b31;padding:16px 18px 24px;font-size:14px;display:flex;flex-direction:column;gap:16px}
#info .ihead{display:flex;gap:8px;align-items:flex-start}
#info h2{flex:1;margin:4px 0 0;font-size:15px;font-weight:650;word-break:break-all}
#info .icon{color:#bbb}#info .icon:hover{background:#26272d;color:#fff}
#info .btn{background:#202127;border-color:#34353c;color:#e9e9ec;height:32px}
#info .btn:hover{background:#2a2b31}
#info .btn.keep{color:#5fd07c;border-color:#2f6b40}#info .btn.trash{color:#e8ad3c;border-color:#6e5420}#info .btn.erase{color:#ff7a6e;border-color:#7a2e29}
.acts{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.acts .btn{justify-content:center;padding:0 6px}
.tools{display:flex;flex-wrap:wrap;gap:6px}
dl.dt{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0}
dl.dt dt{color:#9a9ba3}
dl.dt dd{margin:0;word-break:break-word}
.sh{font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:#9a9ba3;margin:0 0 6px}
.tags{display:flex;flex-wrap:wrap;gap:4px}
.tag{display:inline-flex;align-items:center;gap:2px;border:1px solid #3a3b42;border-radius:999px;padding:1px 4px 1px 9px;font-size:12px;min-height:24px}
.tag:not(:has(button)){padding-right:9px}
.tag button{border:0;background:none;color:#f99;width:20px;height:20px;border-radius:50%}
#info input{width:100%;height:32px;margin-top:8px;padding:0 10px;border:1px solid #34353c;border-radius:8px;background:#0f1013;color:#eee}
#info .fine{color:#8d8e96;font-size:12px;margin:0}
@media (max-width:760px){#viewer.on{display:block;overflow-y:auto;overscroll-behavior:contain}#stage{height:88vh;height:88dvh}#info{width:auto;overflow:visible;border-left:0;border-top:1px solid #2a2b31}#media{inset:8px}.arrow{width:36px;height:52px}}

/* ---------------------------------------------------------------- confirm / job dialog */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:30;display:none;align-items:center;justify-content:center;padding:16px}
.modal.on{display:flex}
.box{background:var(--panel);color:var(--fg);border-radius:14px;padding:20px 22px;max-width:640px;width:100%;max-height:85vh;overflow:auto;box-shadow:var(--shadow)}
.box h3{margin:0 0 8px;font-size:17px}
.box pre{white-space:pre-wrap;word-break:break-all;font-size:12px;background:var(--card);padding:10px;border-radius:8px;max-height:50vh;overflow:auto}
.box input{height:34px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.mbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
#mstatus{margin:12px 0 0;padding:10px 12px;border-radius:8px;background:var(--acc-soft)}

/* ---------------------------------------------------------------- phone */
@media (max-width:760px){
 #top{padding:0 12px;gap:8px}
 #vtitle{font-size:16px;overflow:hidden;text-overflow:ellipsis}
 #tsize{width:72px}
 #close .lbl2{display:none}#close{padding:0 8px}
 #chips{padding:10px 12px 0}#note{padding:0 12px}
 main{padding:4px 12px 40px}
 .grid{gap:4px}
 #actionbar{left:calc(var(--ml) + 8px);right:8px;bottom:8px;padding:8px;gap:6px}
 #actionbar .btn{padding:0 9px}
 #actionbar kbd,#actionbar .sep{display:none}
}
</style></head><body data-view="lib">
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
 <symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></symbol>
 <symbol id="i-lib" viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></symbol>
 <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v5M14 11v5"/></symbol>
 <symbol id="i-plug" viewBox="0 0 24 24"><path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/></symbol>
 <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></symbol>
 <symbol id="i-child" viewBox="0 0 24 24"><circle cx="12" cy="6.5" r="2.5"/><path d="M8 21v-6l-2-1 2-5h8l2 5-2 1v6M12 15v6"/></symbol>
 <symbol id="i-tag" viewBox="0 0 24 24"><path d="M3.5 12.5V4h8.5l8.5 8.5-8.5 8.5z"/><circle cx="8" cy="8.5" r="1.4"/></symbol>
 <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3"/></symbol>
 <symbol id="i-filter" viewBox="0 0 24 24"><path d="M4 5h16l-6 7.5V19l-4 1.5v-8z"/></symbol>
 <symbol id="i-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></symbol>
 <symbol id="i-size" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 14l4-4 5 5 2-2 5 5"/></symbol>
 <symbol id="i-power" viewBox="0 0 24 24"><path d="M12 3.5v8M7 6.5a7.5 7.5 0 1 0 10 0"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
</svg>
<aside id="sidebar" aria-label="Views and filters">
 <div class="sb-top">
  <button type="button" class="icon" id="rail" aria-expanded="true" aria-controls="sidebar"><svg class="ic"><use href="#i-menu"/></svg></button>
  <span class="brand">Media gallery</span>
 </div>
 <nav id="views" aria-label="Views">
  <button type="button" class="nv" data-v="lib" title="Library"><svg class="ic"><use href="#i-lib"/></svg><span class="lbl">Library</span><span class="ct" id="c-lib"></span></button>
  <button type="button" class="nv" data-v="sug" title="Trash suggestions"><svg class="ic"><use href="#i-trash"/></svg><span class="lbl">Trash suggestions</span><span class="ct" id="c-sug"></span></button>
  <div id="ptabs"></div>
  <div class="nvhead">Quick views</div>
  <button type="button" class="nv" data-v="recent" title="Recently added"><svg class="ic"><use href="#i-clock"/></svg><span class="lbl">Recently added</span><span class="ct" id="c-recent"></span></button>
  <button type="button" class="nv" data-v="child" title="Possible child (Review|child-in-shot)"><svg class="ic"><use href="#i-child"/></svg><span class="lbl">Possible child</span><span class="ct" id="c-child"></span></button>
  <button type="button" class="nv" data-v="untagged" title="Untagged"><svg class="ic"><use href="#i-tag"/></svg><span class="lbl">Untagged</span><span class="ct" id="c-untagged"></span></button>
  <button type="button" class="nv railonly" id="rsearch" title="Search (/)"><svg class="ic"><use href="#i-search"/></svg><span class="vh">Search</span></button>
  <button type="button" class="nv railonly icon" id="rfilter" title="Filters" style="width:100%"><svg class="ic"><use href="#i-filter"/></svg><span class="vh">Filters</span><span class="badge-n" id="rbadge" hidden></span></button>
 </nav>
 <div class="sb-search">
  <div class="field"><svg class="ic"><use href="#i-search"/></svg><input id="q" type="search" placeholder="Search photos and videos" aria-label="Search" autocomplete="off"></div>
  <div id="qhint" class="hint" hidden></div>
 </div>
 <div id="filters" role="region" aria-label="Filters">
  <div class="fhead"><b>Filters</b><button type="button" class="link" id="clear" hidden></button></div>
  <div id="fsecs"></div>
 </div>
</aside>
<div id="scrim"></div>
<div id="app">
 <header id="top">
  <h1 id="vtitle">Library</h1><span id="count"></span><span class="grow"></span>
  <label class="size" id="tsizew" title="Thumbnail size"><svg class="ic"><use href="#i-size"/></svg><span class="vh">Thumbnail size</span><input id="tsize" type="range" min="80" max="320" step="8"></label>
  <button type="button" class="btn" id="close" title="Stop the gallery server"><svg class="ic"><use href="#i-power"/></svg><span class="lbl2">Close</span></button>
 </header>
 <div id="chips" aria-label="Active filters" hidden></div>
 <p id="note" class="note" hidden></p>
 <main id="main"><div id="list"></div><div id="sentinel"></div><div id="plugin" hidden></div></main>
 <div id="actionbar" hidden>
  <div class="grp" id="selgrp" role="toolbar" aria-label="Selection actions" hidden><span id="seln"></span>
   <button type="button" class="btn keep" id="bkeep">Keep <kbd>K</kbd></button><button type="button" class="btn trash" id="btrash">Trash <kbd>T</kbd></button><button type="button" class="btn erase" id="berase">Erase <kbd>E</kbd></button>
   <span class="sep"></span>
   <button type="button" class="btn" id="btag">+ tag</button><button type="button" class="btn" id="buntag">− tag</button><button type="button" class="btn" id="brot">Rotate ⟳</button><button type="button" class="btn" id="bnone">Clear selection <kbd>Esc</kbd></button>
  </div>
  <div class="grp" id="suggrp" role="group" aria-label="Decisions" hidden><span id="sugn"></span><span class="note" style="margin:0">K / T on the outlined tile, arrows move, Enter opens</span><button type="button" class="btn primary" id="sugapply">Apply…</button></div>
 </div>
</div>
<div id="toast" role="status" aria-live="polite"></div>
<div id="viewer" role="dialog" aria-modal="true" aria-label="Item" tabindex="-1">
 <div id="stage"><div id="media"></div>
  <button type="button" class="arrow" id="vprev" aria-label="Previous (Left arrow)">‹</button><button type="button" class="arrow" id="vnext" aria-label="Next (Right arrow)">›</button></div>
 <aside id="info" aria-label="Details"></aside>
</div>
<div class="modal" id="modal" role="dialog" aria-modal="true" aria-labelledby="mh" data-dialog="confirm"><div class="box"><h3 id="mh"></h3><div id="mb"></div>
 <p id="mtype" hidden>Type <b>ERASE</b> to confirm: <input id="mtyped" autocomplete="off" spellcheck="false" aria-label="Type ERASE"></p>
 <div class="mbtns"><button type="button" class="btn" id="mno">Cancel</button><button type="button" class="btn primary" id="mgo">Confirm</button></div><p id="mstatus" role="status" hidden></p><pre id="mout" hidden></pre></div></div>
<script>
"use strict";
const CSRF=document.querySelector('meta[name=csrf]').content;
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
const fmt=n=>(+n||0).toLocaleString();
// per-browser conveniences only (sidebar rail, open sections, tile size); the page works without them
const LS={get(k,d){try{const v=localStorage.getItem("gallery."+k);return v===null?d:v}catch(e){return d}},set(k,v){try{localStorage.setItem("gallery."+k,v)}catch(e){}}};
let view="lib",items=[],total=0,loading=false,done=false,sel=new Set(),last=null,cur=-1,ranked=false,simId=null,simName="",dec={},vi=-1,facets=null,gen=0,lastFocus=null;
let F={aimin:"0.7"};
const ACTIVE=["kind","ym","project","source","place","event","label","mine","review","ai","used","added"];
const NAMES={kind:"Kind",ym:"When",project:"Project",source:"Source",place:"Place",event:"Event",label:"Label",mine:"My tag",review:"Review",ai:"AI",used:"Used",added:"Added"};
const REVIEW={"maybe-trash":"Maybe trash","child-in-shot":"Child in shot",untagged:"Untagged"};
const AIV={trash:"Trash",keep:"Keep",none:"Not judged"};
const VIEWS=__VIEWS__;window.mediaIntakeViews=window.mediaIntakeViews||{};
const TITLES={lib:"Library",sug:"Trash suggestions",recent:"Recently added",child:"Possible child",untagged:"Untagged"};
VIEWS.forEach(v=>{TITLES["p-"+v.id]=v.title});
const activeKeys=()=>ACTIVE.filter(k=>F[k]);
const plugView=()=>view.startsWith("p-");
async function api(path,body){const o={credentials:"same-origin"};if(body!==undefined){o.method="POST";o.headers={"Content-Type":"application/json","X-CSRF":CSRF};o.body=JSON.stringify(body)}
 const r=await fetch(path,o);let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||("HTTP "+r.status));return j}
function filt(){const f={};for(const k of ACTIVE.concat(["aimin","days"]))if(F[k])f[k]=F[k];
 if(f.ai!=="trash")delete f.aimin;if(f.used!=="unused")delete f.days;const q=$("#q").value.trim();if(q)f.q=q;if(simId!==null){f.similar=simId;delete f.q}
 if(view==="sug")f.view="suggest";return f}
function qs(o){return Object.entries(o).map(([k,v])=>encodeURIComponent(k)+"="+encodeURIComponent(v)).join("&")}
function monthName(k){const [y,m]=k.split("-");return new Date(+y,+m-1,1).toLocaleString(undefined,{month:"long",year:"numeric"})}
function monthOnly(k){const [y,m]=k.split("-");return new Date(+y,+m-1,1).toLocaleString(undefined,{month:"long"})}
function chipVal(k){const v=F[k];if(k==="kind")return v==="photo"?"Photos":"Videos";if(k==="ym")return v.length===4?v:monthName(v);if(k==="review")return REVIEW[v]||v;
 if(k==="ai")return v==="trash"?"trash ≥ "+(+F.aimin||0).toFixed(2):AIV[v]||v;if(k==="used")return v==="used"?"used":(F.days?"not in "+F.days+" days":"never used");
 if(k==="added")return "last "+v+" days";return v}
// ---------------------------------------------------------------- sidebar: layout
const mqPhone=matchMedia("(max-width:760px)");let railPref=LS.get("rail","0")==="1",drawer=false;
function layout(){const ph=mqPhone.matches,col=ph?!drawer:railPref;document.body.classList.toggle("collapsed",col);document.body.classList.toggle("overlay",ph&&drawer);
 const b=$("#rail");b.setAttribute("aria-expanded",String(!col));b.setAttribute("aria-label",col?"Expand sidebar":"Collapse sidebar");b.title=col?"Expand sidebar":"Collapse sidebar"}
function expandSidebar(){if(mqPhone.matches)drawer=true;else{railPref=false;LS.set("rail","0")}layout()}
$("#rail").onclick=()=>{if(mqPhone.matches)drawer=!drawer;else{railPref=!railPref;LS.set("rail",railPref?"1":"0")}layout()};
$("#scrim").onclick=()=>{drawer=false;layout()};
mqPhone.addEventListener("change",()=>{drawer=false;layout()});
$("#rsearch").onclick=()=>{expandSidebar();$("#q").focus()};
$("#rfilter").onclick=()=>{expandSidebar();const f=$("#filters");if(!f.hidden){f.scrollIntoView({block:"start"});const h=f.querySelector(".fs-h");if(h)h.focus()}};
// ---------------------------------------------------------------- sidebar: filter sections
const SECS=[["kind","Kind","1"],["when","When","1"],["project","Project","1"],["source","Source","1"],["place","Place","0"],["event","Event","0"],
 ["label","Label","1"],["mine","My tags","0"],["review","Review flags","1"],["ai","AI verdict","0"],["used","Used","0"]];
const SECKEY={when:"ym"};
function buildSections(){const box=$("#fsecs");
 for(const [s,title,open0] of SECS){const sec=el("section","fs");sec.dataset.s=s;const h3=el("h3"),h=el("button","fs-h");h.type="button";const bid="fsb-"+s;h.setAttribute("aria-controls",bid);
  h.append(el("span","fs-t",title),el("span","fs-v"));h.insertAdjacentHTML("beforeend",'<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg>');h3.append(h);
  const b=el("div","fs-b");b.id=bid;sec.append(h3,b);box.append(sec);
  if(s==="kind"){const g=el("div","seg");g.setAttribute("role","group");g.setAttribute("aria-label","Kind");
   [["","All"],["photo","Photos"],["video","Videos"]].forEach(([v,l])=>{const x=el("button");x.type="button";x.dataset.k="kind";x.dataset.val=v;const c=el("span","ct");c.id="ck-"+(v||"all");x.append(el("span",null,l),c);x.onclick=()=>setF("kind",v);g.append(x)});b.append(g)}
  else if(s==="when"){const w=el("div");w.id="when";b.append(w)}
  else{const o=el("div","opts");o.dataset.k=s;o.dataset.name=title.toLowerCase();b.append(o)}
  if(s==="ai"){const d=el("div","sub");d.id="aiconf";d.innerHTML='<label for="aimin">Confidence at least <output id="aiv">0.70</output></label><input type="range" id="aimin" min="0" max="1" step="0.05">';b.append(d)}
  if(s==="used"){const d=el("div","sub");d.id="usedays";d.innerHTML='<label for="days">Not used in the last <input type="number" id="days" min="0" step="1" placeholder="ever"> days</label>';b.append(d)}
  const open=LS.get("sec."+s,open0)==="1";setSec(sec,open);h.onclick=()=>{const o=h.getAttribute("aria-expanded")!=="true";setSec(sec,o);LS.set("sec."+s,o?"1":"0")}}
 $("#aimin").oninput=()=>{$("#aiv").textContent=(+$("#aimin").value).toFixed(2)};
 $("#aimin").onchange=()=>{F.aimin=$("#aimin").value;simId=null;syncUI();reset()};
 let dt=null;$("#days").oninput=()=>{clearTimeout(dt);dt=setTimeout(()=>{F.days=$("#days").value.trim();simId=null;syncUI();reset()},500)}}
function setSec(s,o){s.querySelector(".fs-h").setAttribute("aria-expanded",String(o));s.querySelector(".fs-b").hidden=!o}
function optBtn(k,v,label,c){const b=el("button","opt");b.type="button";b.dataset.k=k;b.dataset.val=v;b.append(el("span","on",label),el("span","ct",fmt(c)));b.title=label;b.onclick=()=>setF(k,F[k]===v?"":v);return b}
const finds={};
function renderOpts(k,entries,lab){const box=$(`.opts[data-k=${k}]`);if(!box)return;box.replaceChildren();
 if(entries.length>8){const f=el("input","ofind");f.type="search";f.placeholder="Find "+box.dataset.name+"…";f.setAttribute("aria-label","Find "+box.dataset.name);f.value=finds[k]||"";f.oninput=()=>{finds[k]=f.value;applyFind(box,k)};box.append(f)}
 const ul=el("div","olist");if(!entries.length)ul.append(el("div","empty","None yet"));
 entries.forEach(([v,c])=>ul.append(optBtn(k,v,lab?lab(v):v,c)));box.append(ul);applyFind(box,k)}
function applyFind(box,k){const q=(finds[k]||"").trim().toLowerCase();box.querySelectorAll(".opt").forEach(b=>{b.hidden=!!q&&!b.textContent.toLowerCase().includes(q)&&F[k]!==b.dataset.val})}
const openYears=new Set();
function renderWhen(){const box=$("#when");box.replaceChildren();const ent=facets.facets.month||[],yrs=ent.filter(([n])=>n.length===4);
 if(!yrs.length)box.append(el("div","empty","No dates"));if(F.ym)openYears.add(F.ym.slice(0,4));
 yrs.forEach(([y,c])=>{const row=el("div","yr"),cb=el("button","caret","▶");cb.type="button";const mos=el("div","mos");
  const setO=o=>{mos.hidden=!o;cb.setAttribute("aria-expanded",String(o));cb.setAttribute("aria-label",(o?"Hide":"Show")+" the months of "+y);o?openYears.add(y):openYears.delete(y)};
  ent.filter(([n])=>n.length===7&&n.startsWith(y)).forEach(([m,mc])=>mos.append(optBtn("ym",m,monthOnly(m),mc)));
  setO(openYears.has(y));cb.onclick=()=>setO(mos.hidden);row.append(cb,optBtn("ym",y,y,c));box.append(row,mos)})}
async function loadFacets(tries){facets=await api("/api/facets");const fc=facets.facets,c=facets.counts||{};
 ["project","source","place","event","label","mine"].forEach(k=>renderOpts(k,fc[k]||[]));
 renderOpts("review",Object.keys(REVIEW).map(v=>[v,c["review:"+v]||0]),v=>REVIEW[v]);
 renderOpts("ai",Object.keys(AIV).map(v=>[v,c["ai:"+v]||0]),v=>AIV[v]);
 renderOpts("used",[["used",c["used:used"]||0],["unused",c["used:unused"]||0]],v=>v==="used"?"Used in a post":"Unused");
 renderWhen();
 $("#ck-all").textContent=fmt(facets.total);$("#ck-photo").textContent=fmt(c["kind:photo"]);$("#ck-video").textContent=fmt(c["kind:video"]);
 $("#c-lib").textContent=fmt(facets.total);$("#c-sug").textContent=fmt(c.suggest);$("#c-recent").textContent=fmt(c.recent);
 $("#c-child").textContent=fmt(c["review:child-in-shot"]);$("#c-untagged").textContent=fmt(c["review:untagged"]);
 $('.nv[data-v=recent]').title="Recently added (filed in the last "+facets.recent_days+" days)";
 const h=$("#qhint");h.replaceChildren();h.hidden=!facets.search;
 if(facets.search){const checking=/^checking/.test(facets.search);const d=el("details"),s=el("summary",null,checking?"Checking the search model…":"Semantic search off: tags and names only");
  d.append(s,el("div","why",facets.search));h.append(d);h.title=facets.search;if(checking&&(tries||0)<10)setTimeout(()=>loadFacets((tries||0)+1).catch(()=>{}),1500)}
 syncUI()}
// ---------------------------------------------------------------- filter state
function setF(k,v){if(v)F[k]=v;else delete F[k];simId=null;if(plugView()){setView("lib");return}syncUI();reset()}
function clearAll(){F={aimin:F.aimin||"0.7"};$("#q").value="";simId=null;syncUI();reset()}
function navActive(){if(view!=="lib")return view;const a=activeKeys();if(a.length===1&&!$("#q").value.trim()&&simId===null){if(F.added)return "recent";if(F.review==="child-in-shot")return "child";if(F.review==="untagged")return "untagged"}return "lib"}
function syncUI(){$$("button[data-k]").forEach(b=>b.setAttribute("aria-pressed",String((F[b.dataset.k]||"")===b.dataset.val)));
 $$(".fs").forEach(s=>{const k=SECKEY[s.dataset.s]||s.dataset.s;s.querySelector(".fs-v").textContent=F[k]?chipVal(k):""});
 const n=activeKeys().length;$("#clear").hidden=!n;$("#clear").textContent=`Clear filters (${n})`;$("#rbadge").hidden=!n;$("#rbadge").textContent=n;
 $("#aiconf").hidden=F.ai!=="trash";$("#aimin").value=F.aimin;$("#aiv").textContent=(+F.aimin).toFixed(2);
 $("#usedays").hidden=F.used!=="unused";if(document.activeElement!==$("#days"))$("#days").value=F.days||"";
 const C=$("#chips");C.replaceChildren();
 const chip=(text,fn)=>{const c=el("span","chip"),x=el("button","x","×");c.append(el("span",null,text));x.type="button";x.setAttribute("aria-label","Remove "+text);x.onclick=fn;c.append(x);C.append(c)};
 if(simId!==null)chip("Similar to "+simName,()=>{simId=null;syncUI();reset()});
 const q=$("#q").value.trim();if(q&&simId===null)chip("Search: "+q,()=>{$("#q").value="";syncUI();reset()});
 activeKeys().forEach(k=>chip(NAMES[k]+": "+chipVal(k),()=>setF(k,"")));
 if(C.children.length>1){const a=el("button","link","Clear all");a.type="button";a.onclick=clearAll;C.append(a)}
 C.hidden=!C.children.length||plugView();
 const na=navActive();$$(".nv[data-v]").forEach(b=>{if(b.dataset.v===na)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current")});
 $("#vtitle").textContent=TITLES[na]||""}
$("#clear").onclick=clearAll;
// ---------------------------------------------------------------- listing
function setNote(n){n=n||"";if(/^Semantic search unavailable/.test(n))n="Semantic search is off: showing tag and file-name matches.";$("#note").textContent=n;$("#note").hidden=!n}
function reset(){gen++;items=[];total=0;done=false;cur=-1;$("#list").replaceChildren();sel.clear();dec={};paintSel();more()}
async function more(){if(loading||done||plugView())return;loading=true;const g=gen;let stale=false;
 try{const j=await api("/api/list?"+qs(Object.assign(filt(),{offset:items.length,limit:240})));
  if(g!==gen)stale=true;else{total=j.total;ranked=j.ranked;setNote(j.note);const start=items.length;items=items.concat(j.items);done=items.length>=total;render(start);
   $("#count").textContent=fmt(total)+(total===1?" item":" items")+(view==="sug"?" suggested":"");
   if(!items.length)$("#list").replaceChildren(el("p","empty big",view==="sug"?"No trash suggestions here.":"Nothing matches."));
   document.body.dataset.ready="1"}}
 catch(e){if(g===gen)setNote("Error: "+e.message)}finally{loading=false}
 if(stale){more();return}
 if(!done&&$("#sentinel").getBoundingClientRect().top<innerHeight+600)more()}
function groupKey(it){return ranked?(view==="sug"?"Most likely junk first":"Best matches first"):it.mo}
function render(start){const L=$("#list");let sec=L.lastElementChild,g=sec&&sec.querySelector(".grid"),gk=sec?sec.dataset.k:null;
 for(let n=start;n<items.length;n++){const it=items[n],k=groupKey(it);
  if(k!==gk){sec=el("section");sec.dataset.k=k;sec.append(el("h2","month",ranked?k:monthName(k)));g=el("div","grid");sec.append(g);L.append(sec);gk=k}
  g.append(tile(it,n))}paintSel()}
function tile(it,n){const t=el("div","tile");t.id="t"+it.id;t.dataset.n=n;t.dataset.item=it.n;t.tabIndex=0;t.title=it.n;
 const im=el("img");im.loading="lazy";im.decoding="async";im.alt=it.n;im.src="/t/"+it.id;im.onerror=()=>{im.replaceWith(el("div","ph",(it.m==="video"?"video: ":"")+it.n))};t.append(im);
 const cb=el("input","ck");cb.type="checkbox";cb.tabIndex=-1;cb.setAttribute("aria-label","select "+it.n);cb.onclick=ev=>{ev.stopPropagation();pick(n,ev.shiftKey)};t.append(cb);
 if(it.m==="video")t.append(el("span","play","▶"));
 const fl=[];if(it.c!=null)fl.push("AI "+it.c.toFixed(1));fl.push(...it.f);if(fl.length)t.append(el("span","flag",fl.join(" · ")));
 t.onclick=ev=>{if(ev.shiftKey||ev.ctrlKey||ev.metaKey){pick(n,ev.shiftKey);return}if(view==="sug"){cur=n;paintSel();return}openViewer(n)};
 // Enter opens, Space selects (on the focused tile, never while the large view is open: Space plays / pauses there)
 t.onkeydown=ev=>{if(vi>=0||ev.target!==t)return;if(ev.key==="Enter"&&view!=="sug"){ev.preventDefault();ev.stopPropagation();openViewer(n)}else if(ev.key===" "){ev.preventDefault();ev.stopPropagation();pick(n,ev.shiftKey)}};return t}
function pick(n,range){if(range&&last!==null){const [a,b]=[Math.min(last,n),Math.max(last,n)];for(let i=a;i<=b;i++)sel.add(items[i].id)}
 else{const id=items[n].id;sel.has(id)?sel.delete(id):sel.add(id)}last=n;paintSel()}
function paintSel(){$$(".tile").forEach(t=>{const id=+t.id.slice(1),n=+t.dataset.n;t.classList.toggle("sel",sel.has(id));t.querySelector("input").checked=sel.has(id);
  t.classList.toggle("cur",n===cur);t.classList.toggle("dk",dec[id]==="keep");t.classList.toggle("dt",dec[id]==="trash")});
 const selOn=sel.size>0&&!plugView(),sugOn=view==="sug";$("#selgrp").hidden=!selOn;$("#suggrp").hidden=!sugOn;$("#actionbar").hidden=!(selOn||sugOn);
 document.body.classList.toggle("barup",selOn||sugOn);document.body.classList.toggle("selecting",sel.size>0);$("#seln").textContent=fmt(sel.size)+" selected";
 const nk=Object.values(dec).filter(d=>d==="keep").length,nt=Object.values(dec).filter(d=>d==="trash").length;
 $("#sugn").textContent=`${nk} keep / ${nt} trash marked`;$("#sugapply").disabled=!(nk+nt)}
function selIds(){return [...sel]}
function nameOf(id){const it=items.find(i=>i.id===id);return it?it.n:"#"+id}
// ---------------------------------------------------------------- actions
let tt=null;function toast(s){const t=$("#toast");t.textContent=s;t.classList.add("on");clearTimeout(tt);tt=setTimeout(()=>t.classList.remove("on"),4500)}
async function doKeep(ids){try{const j=await api("/api/keep",{ids});toast(j.result);refreshAfter(ids,false);loadFacets()}catch(e){alert("Keep refused: "+e.message)}}
function confirmBox(title,lines,needType,go){$("#mh").textContent=title;const b=$("#mb");b.replaceChildren();lines.forEach(l=>b.append(el("p",l.startsWith("!")?"warn":null,l.replace(/^!/,""))));
 $("#mtype").hidden=!needType;$("#mtyped").value="";$("#mgo").disabled=!!needType;$("#mgo").hidden=false;$("#mout").hidden=true;$("#mout").textContent="";$("#mstatus").hidden=true;delete $("#modal").dataset.job;$("#modal").classList.add("on");
 $("#mtyped").oninput=()=>{$("#mgo").disabled=$("#mtyped").value!=="ERASE"};if(needType)$("#mtyped").focus();else $("#mno").focus();
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
 progressMode(path==="/api/erase"?"forget":"trash");poll(0,ids)}
/* Shown from the moment a job starts, before its first output line: an erase sweeps HOME for copies and can
   print nothing for minutes, which looked like a hang. Hidden once the job has finished. */
const STATUS={forget:"Erasing… looking for copies in your home folder. This can take a few minutes; you can close this page, it keeps running.",
 trash:"Trashing… you can close this page, it keeps running."};
function jobStatus(label){const t=STATUS[label]||"Working… you can close this page, it keeps running.";const m=$("#mstatus");if(m.textContent!==t)m.textContent=t;m.hidden=false}
function progressMode(label){$("#mgo").hidden=true;$("#mtype").hidden=true;$("#mout").hidden=false;$("#mout").textContent="";jobStatus(label);$("#modal").classList.add("on")}
function stepText(j){return j.step?` (step ${j.step}${j.total?" of "+j.total:""}: ${j.label})`:""}
async function poll(since,ids){let j;try{j=await api("/api/job?since="+since)}catch(e){$("#mout").textContent+="\n(server stopped; an action already started keeps going on its own)";return}
 if(j.lines.length){$("#mout").textContent+=j.lines.join("\n")+"\n";$("#mout").scrollTop=1e9}$("#modal").dataset.job=j.done?(j.state==="crashed"?"crashed":j.ok?"done":"failed"):"running";
 if(!j.done){if(j.label)jobStatus(j.label);$("#mh").textContent="Running"+stepText(j)+"; closing the page does not stop it";setTimeout(()=>poll(j.next,ids),600);return}
 $("#mh").textContent=j.state==="crashed"?"The action stopped unexpectedly (crashed)"+stepText(j)+(j.step_running?"; the step it started is still running":"")+". Nothing more runs by itself."
  :j.ok?"Done":"Finished with problems (see below)";
 $("#mstatus").hidden=true;ids.forEach(id=>sel.delete(id));closeViewer();loadFacets();reset()}
async function attachJob(){let j;try{j=await api("/api/job")}catch(e){return}if(j.done)return;$("#mh").textContent="An action is running";$("#mb").replaceChildren();progressMode(j.label);poll(0,j.ids||[])}
async function tagAsk(op){const ids=vi>=0&&!sel.size?[items[vi].id]:selIds();if(!ids.length)return;
 const t=prompt((op==="add"?"Add":"Remove")+" a tag of yours on "+ids.length+" item(s). Not Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used|, Owner| (the tools' own):");
 if(!t)return;try{const j=await api("/api/tag",{ids,tag:t,op});toast(j.result);loadFacets();refreshAfter(ids,false)}catch(e){alert("Refused: "+e.message)}}
async function rot(ids,dir){try{const j=await api("/api/rotate",{ids,dir});toast(j.result);refreshAfter(ids,true)}catch(e){alert("Refused: "+e.message)}}
function refreshAfter(ids,thumbs){if(thumbs)ids.forEach(id=>{const t=$("#t"+id);if(t){const im=t.querySelector("img");if(im)im.src="/t/"+id+"?r="+Date.now()}});
 if(vi>=0)openViewer(vi,{keep:true});else if(view==="sug")reset()}
$("#bkeep").onclick=()=>doKeep(selIds());$("#btrash").onclick=()=>askTrash(selIds());$("#berase").onclick=()=>askErase(selIds());
$("#btag").onclick=()=>tagAsk("add");$("#buntag").onclick=()=>tagAsk("remove");$("#brot").onclick=()=>rot(selIds(),"cw");$("#bnone").onclick=()=>{sel.clear();paintSel()};
$("#sugapply").onclick=()=>{const keep=Object.keys(dec).filter(k=>dec[k]==="keep").map(Number),trash=Object.keys(dec).filter(k=>dec[k]==="trash").map(Number);
 confirmBox(`Apply: ${keep.length} keep, ${trash.length} trash?`,[keep.length?"Keep: Owner|keep + Owner|not-trash in the sidecar (never suggested again).":"",trash.length?"Trash ("+names(trash)+"): plain delete, never taken in again (Owner|trash: the sha256 in trashed.json; copies left on disk are marked).":""].filter(Boolean),false,
  async()=>{const j=await api("/api/decide",{keep,trash,confirm:true});if(trash.length){progressMode("trash");$("#mout").textContent=j.result+"\n";poll(0,trash)}else{$("#modal").classList.remove("on");toast(j.result);loadFacets();reset()}})};
// ---------------------------------------------------------------- large view
function curVideo(){return $("#media video")}
function stopVideo(){const v=curVideo();if(v){v.pause();v.removeAttribute("src");v.load()}}  // releases the file
function fitMedia(){const m=$("#media>img,#media>video");if(!m)return;const r=$("#media").getBoundingClientRect(),q=+(m.dataset.rot||0)%180!==0;
 m.style.width=q?r.height+"px":"100%";m.style.height=q?r.width+"px":"100%";m.style.transform=`translate(-50%,-50%) rotate(${m.dataset.rot||0}deg)`}
addEventListener("resize",fitMedia);
// the browser can't show it: the thumbnail, large, and a button that opens the file in the desktop's own player
function cantPlay(d,why){stopVideo();const M=$("#media");M.replaceChildren();const box=el("div","nop"),im=el("img");im.alt=d.name;im.src="/t/"+d.id;im.onerror=()=>im.remove();
 const c=el("div","cant");c.append(el("span",null,why));const o=el("button",null,"Open in the system player");o.type="button";
 o.onclick=async()=>{try{toast((await api("/api/open",{ids:[d.id]})).result)}catch(e){alert("Refused: "+e.message)}};c.append(o);
 box.append(im,c);M.append(box);const u=$("#unmute");if(u)u.remove()}
function startVideo(v){const ok=()=>{const u=$("#unmute");if(u)u.remove()};
 v.play().then(ok).catch(()=>{if(curVideo()!==v)return;v.muted=true;v.play().then(()=>{if(curVideo()!==v||$("#unmute"))return;const u=el("button",null,"Unmute");u.id="unmute";u.type="button";
  u.onclick=()=>{v.muted=false;u.remove()};$("#stage").append(u)}).catch(()=>{})})}
async function openViewer(n,opt){opt=opt||{};if(n<0||n>=items.length)return;if(vi<0)lastFocus=document.activeElement;
 const v0=curVideo(),tKeep=opt.keep&&v0&&v0.dataset.id==items[n].id?v0.currentTime:null;stopVideo();vi=n;cur=n;
 const pend=api("/api/item/"+items[n].id);$("#viewer").classList.add("on");document.body.classList.add("noscroll");
 const M=$("#media");const u0=$("#unmute");if(u0)u0.remove();
 // a video is made and started inside the click's handler (the user gesture), before the details arrive
 if(items[n].m==="video"){const v=el("video");v.controls=true;v.preload="auto";v.playsInline=true;v.autoplay=true;v.poster="/t/"+items[n].id;v.dataset.id=items[n].id;
  v.src="/m/"+items[n].id;M.replaceChildren(v);fitMedia();startVideo(v);if(tKeep)v.currentTime=tKeep}
 else M.replaceChildren();
 let d;try{d=await pend}catch(e){toast("gone");return}if(vi!==n)return;
 const v=curVideo();
 if(d.video&&d.src&&v){v.dataset.rot=d.rot;fitMedia();
  const bad=()=>{if(curVideo()===v)cantPlay(d,"This browser can't play this video (HEVC, most likely).")};
  const nopic=()=>{if(curVideo()===v&&v.readyState>=1&&!v.videoWidth)bad()};
  v.addEventListener("error",bad);v.addEventListener("loadedmetadata",nopic);if(v.error)bad();else nopic()}
 else if(d.kind==="video")cantPlay(d,"No browser preview for this format.");
 else if(d.src){const im=el("img");im.alt=d.name;im.dataset.rot=d.rot;im.src=d.src;im.onerror=()=>cantPlay(d,"This browser can't show this picture.");M.replaceChildren(im);fitMedia()}
 else cantPlay(d,"No browser preview for this format.");
 $("#vprev").disabled=n<=0;$("#vnext").disabled=n>=items.length-1&&done;
 const s=$("#info");s.replaceChildren();
 const head=el("div","ihead"),x=el("button","icon");x.type="button";x.setAttribute("aria-label","Close (Esc)");x.title="Close (Esc)";x.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-x"/></svg>');x.onclick=closeViewer;
 head.append(el("h2",null,d.name),x);s.append(head);
 const acts=el("div","acts"),mk=(box,t,f,c)=>{const b=el("button","btn"+(c?" "+c:""),t);b.type="button";b.onclick=f;box.append(b);return b};
 mk(acts,"Keep (K)",()=>doKeep([d.id]),"keep");mk(acts,"Trash (T)",()=>askTrash([d.id]),"trash");mk(acts,"Erase (E)",()=>askErase([d.id]),"erase");s.append(acts);
 const tools=el("div","tools");mk(tools,"⟲ Rotate",()=>rot([d.id],"ccw")).title="Shift+R";mk(tools,"⟳ Rotate",()=>rot([d.id],"cw")).title="R";
 mk(tools,"Similar",()=>{simId=d.id;simName=d.name;closeViewer();$("#q").value="";syncUI();reset()});s.append(tools);
 const dl=el("dl","dt"),row=(k,v)=>{if(v){dl.append(el("dt",null,k),el("dd",null,v))}};
 row("Date",d.date);row("Place",d.place);row("Event",d.event);row("Project",d.project);row("Source",d.source);row("Folder",d.folder);row("Resolution",d.res);row("Size",d.size);
 if(d.ai!=null)row("AI trash",d.ai.toFixed(2));if(d.used.length)row("Used in",d.used.join(", "));if(d.kept)row("Owner","kept");
 const det=el("div");det.append(el("p","sh","Details"),dl);s.append(det);
 const tg=el("div"),tl=el("div","tags");tg.append(el("p","sh","Tags"),tl);
 d.tags.forEach(t=>{const c=el("span","tag");c.append(el("span",null,t));if(d.mine.includes(t)){const b=el("button",null,"×");b.type="button";b.title="Remove my tag";b.setAttribute("aria-label","Remove my tag "+t);
  b.onclick=async()=>{try{await api("/api/tag",{ids:[d.id],tag:t,op:"remove"});openViewer(n,{keep:true});loadFacets()}catch(e){alert(e.message)}};c.append(b)}tl.append(c)});
 if(!d.tags.length)tl.append(el("span","tag","none"));
 const add=el("input");add.placeholder="Add my tag, Enter";add.setAttribute("aria-label","Add my tag");
 add.onkeydown=async ev=>{if(ev.key==="Enter"&&add.value.trim()){ev.stopPropagation();try{await api("/api/tag",{ids:[d.id],tag:add.value.trim(),op:"add"});loadFacets();openViewer(n,{keep:true})}catch(e){alert(e.message)}}};
 tg.append(add);s.append(tg);
 s.append(el("p","fine","Rotate writes XMP-tiff:Orientation in the sidecar only; the original is never touched. No crop here: make crops as copies in an editor."));
 if(!opt.keep)$("#viewer").focus({preventScroll:true});
 $("#viewer").dataset.current=d.name;  // last: the keys (Space, arrows) act on this item from here on
 if(n>=items.length-20)more()}
function closeViewer(){if(vi<0)return;const id=items[vi]&&items[vi].id;stopVideo();vi=-1;$("#viewer").classList.remove("on");document.body.classList.remove("noscroll");$("#media").replaceChildren();
 delete $("#viewer").dataset.current;const u=$("#unmute");if(u)u.remove();paintSel();const t=$("#t"+id)||lastFocus;if(t&&t.focus)t.focus({preventScroll:true})}
$("#vprev").onclick=()=>openViewer(vi-1);$("#vnext").onclick=()=>openViewer(vi+1);
// ---------------------------------------------------------------- keys
document.addEventListener("keydown",ev=>{const tg=ev.target,modal=$("#modal").classList.contains("on");
 // a ticked checkbox (or a slider) keeps focus but takes no letters, so the keys still act on the selection
 const typing=(tg.tagName==="INPUT"&&!["checkbox","radio","range"].includes(tg.type))||tg.tagName==="SELECT"||tg.tagName==="TEXTAREA";
 if(typing||ev.ctrlKey||ev.metaKey||ev.altKey||modal){
  if(ev.key==="Escape"){if(modal){$("#modal").classList.remove("on");tg.blur()}  // blur: the next key goes to the page, not the hidden input
   else if(tg.id==="q"&&tg.value){tg.value="";syncUI();reset()}}
  return}
 const k=ev.key;
 if(vi>=0){const id=items[vi].id;
  if(k==="Escape")closeViewer();else if(k==="ArrowRight")openViewer(vi+1);else if(k==="ArrowLeft")openViewer(vi-1);
  else if(k===" "){if(tg.tagName==="VIDEO"||tg.tagName==="BUTTON")return;const v=curVideo();if(!v)return;v.paused?v.play().catch(()=>{}):v.pause()}
  else if(k==="k"||k==="K")doKeep([id]);else if(k==="t"||k==="T")askTrash([id]);else if(k==="e"||k==="E")askErase([id]);
  else if(k==="r")rot([id],"cw");else if(k==="R")rot([id],"ccw");else return;ev.preventDefault();return}
 if(k==="/"){ev.preventDefault();expandSidebar();$("#q").focus();return}
 if(view==="sug"){if(k==="ArrowRight"||k==="ArrowDown"||k==="j"){cur=Math.min(items.length-1,cur+1)}else if(k==="ArrowLeft"||k==="ArrowUp"){cur=Math.max(0,cur-1)}
  else if((k==="k"||k==="K"||k==="t"||k==="T")&&cur>=0){dec[items[cur].id]=k.toLowerCase()==="k"?"keep":"trash";cur=Math.min(items.length-1,cur+1)}
  else if(k==="Enter"&&cur>=0){openViewer(cur)}else return;ev.preventDefault();paintSel();const t=$("#t"+(items[cur]||{}).id);if(t)t.scrollIntoView({block:"nearest"});if(cur>=items.length-10)more();return}
 if(k==="Escape"&&drawer){drawer=false;layout();return}
 if(!sel.size)return;if(k==="k"||k==="K")doKeep(selIds());else if(k==="t"||k==="T")askTrash(selIds());else if(k==="e"||k==="E")askErase(selIds());else if(k==="Escape"){sel.clear();paintSel()}else return;ev.preventDefault()});
// ---------------------------------------------------------------- views (plugin tabs go in the sidebar, after the built-in ones)
VIEWS.forEach(v=>{const b=el("button","nv");b.type="button";b.dataset.v="p-"+v.id;b.title=v.title;b.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-plug"/></svg>');b.append(el("span","lbl",v.title));$("#ptabs").append(b)});
function setView(v){view=v;simId=null;document.body.dataset.view=v;const plug=plugView();
 $("#list").hidden=plug;$("#sentinel").hidden=plug;$("#plugin").hidden=!plug;$("#filters").hidden=plug;$(".sb-search").hidden=plug;$("#tsizew").hidden=plug;
 if(plug){sel.clear();dec={};paintSel();$("#count").textContent="";setNote("");syncUI();const id=v.slice(2),P=$("#plugin"),pv=window.mediaIntakeViews[id];P.replaceChildren();
  if(!pv||typeof pv.render!=="function"){P.append(el("p","warn","Plugin view "+id+" has no render()"));return}
  pv.render(P,{el,api:(p,b)=>api("/plugin/"+id+"/"+p,b),media:p=>"/plugin/"+id+"/media/"+p})}
 else{if(v==="sug")setNote("K = keep, T = trash on the outlined tile; nothing changes until Apply.");syncUI();reset()}}
$$(".nv[data-v]").forEach(b=>b.onclick=()=>{const v=b.dataset.v;
 if(v==="lib"){if(["recent","child","untagged"].includes(navActive()))F={aimin:F.aimin};setView("lib")}
 else if(v==="sug"||v.startsWith("p-"))setView(v);
 else{F={aimin:F.aimin};if(v==="recent")F.added=String((facets&&facets.recent_days)||30);if(v==="child")F.review="child-in-shot";if(v==="untagged")F.review="untagged";$("#q").value="";setView("lib")}
 if(mqPhone.matches&&drawer){drawer=false;layout()}});
let qt=null;$("#q").oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{simId=null;syncUI();reset()},500)};
$("#close").onclick=async()=>{if(!confirm("Stop the gallery server?"))return;try{await api("/api/close",{})}catch(e){}document.body.replaceChildren(el("p","empty big","Gallery stopped. You can close this tab."))};
new IntersectionObserver(es=>{if(es.some(e=>e.isIntersecting))more()},{rootMargin:"800px"}).observe($("#sentinel"));
// thumbnail size (a per-browser convenience)
let ts=+LS.get("tile",mqPhone.matches?"92":"168");if(!(ts>=80&&ts<=320))ts=168;$("#tsize").value=ts;document.documentElement.style.setProperty("--tile",ts+"px");
$("#tsize").oninput=()=>{document.documentElement.style.setProperty("--tile",$("#tsize").value+"px");LS.set("tile",$("#tsize").value)};
// ---------------------------------------------------------------- start
layout();buildSections();syncUI();
loadFacets().then(()=>reset()).then(attachJob).catch(e=>setNote("Error: "+e.message));
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
