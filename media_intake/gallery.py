#!/usr/bin/env python3
"""Local web gallery for the media library (a day-to-day browser, in place of digiKam).

  media-intake gallery [--drive DRIVE] [--port 0] [--share-out DIR]
  python3 -I media_intake/gallery.py         (same flags)

Shows everything under the library, videos and project folders (config.toml)
(never the media-intake state folder: quarantine, staging, review; never a file
scan flagged, quarantined or erased: label.flagged()). Timeline grid by month,
thumbnails made on demand (max 320 px JPEG, ~/.cache/media-intake/thumbs/<sha256>-r<turn>.jpg,
dir 0700, files 0600; intake.py erase/forget/trash delete them by sha and
verify-gone checks them), an H.264 proxy made on demand for a video the browser
can't play (HEVC: ~/.cache/media-intake/proxies/<sha256>.mp4, cleaned the same way; streamed to the
player while it is made, and made ahead of time by proxies.py),
filters, semantic search (semantic.py, local SigLIP),
and your actions: Keep, Trash (intake.py trash), Erase (intake.py forget, the
erase-everywhere path), my tags, Rotate (XMP-tiff:Orientation in the sidecar
only), Set event, all on one item or a selection, and Share (share-safe copies: intake.py
share-copy into a new folder under --share-out, default ~/Pictures/share-safe, as a
job, then that folder opened in the file manager; also on each tile and in the large view). A left sidebar holds the views with counts (/api/facets): Library, Trash
suggestions, any tab a plugin adds (plugins.py gallery_views(), docs/plugins.md),
the quick views Favorites, Added in the last 30 days, Possible child, Untagged, Dates to check (check-dates'
report, datecheck.py; Fix date writes through fix-dates' code, undoable), and the Events (Event|
tags; rename and merge rewrite only those, in the sidecars that have them; each shown by
a display name kept in <state>/event-names.json, never two alike), and Places (your named places,
places.py: Add a place here… in the large view, "probably here" for a file without GPS; the page
never gets a coordinate, docs/places.md); then
search, the filters and Keyboard shortcuts; its right edge drags (or arrow keys) to a
width kept per browser. Bursts (bursts.py over the plan entries intake wrote) show in
the timeline as one stack per burst: a cover with a count badge, X opens or closes it,
and on a closed stack every action acts on the cover only.
Selection actions sit in a bottom bar shown only while something is selected; Select all
(header, Ctrl+A / Cmd+A) ticks every tile shown (loaded pages; a closed stack's cover). Media
the browser can't decode opens in the desktop's player (xdg-open, a POST naming one
item id, argument list, library files only). Trash and Erase run as a detached
job (jobs.py, key "gallery", one at a time): closing the page or stopping the
server never stops one, and a restarted server or another page shows it again.
The paths go to the worker on a pipe; the status file names the files only by
(device, inode), and an Erase's output is scrubbed of names when it ends.
Favorites: a star on each tile and in the large view (F), xmp:Rating = 5 in the sidecar (digiKam's five
stars; owner.favorite: rating >= 5 is a favorite, kept from Suggested junk like Keep), and a Favorites quick view.
Keep, Favorite, my tags, Rotate, Set event and Fix date can be undone (/api/undo: the server records each sidecar's
fields before and after and puts back exactly that change, refusing once a sidecar changed
again); Trash, Erase and Share can't. No native alert/confirm/prompt: in-page
dialogs, an error message that stays until dismissed, and a "?" list of every key.

Server security is review_ui.py's: stdlib only, everything inline, 127.0.0.1 on a
random port, random token swapped for an HttpOnly SameSite=Strict cookie, Host
check, CSRF value on every POST, no-store + strict CSP, media by opaque id only.
Originals are never written: sidecars (<file>.<ext>.xmp) only. docs/gallery.md.
"""
import argparse, collections, contextlib, datetime, hashlib, html, http.server, io, json, os, re, secrets, select
import shutil, subprocess, sys, tempfile, threading, time, unicodedata
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bursts  # noqa: E402  (burst groups: pure functions over date, device, dHash, burst id)
import datecheck  # noqa: E402  (Dates to check: check-dates' report; Fix date writes through fix-dates' code)
import intake  # noqa: E402  (constants, sidecar helpers, trash_allowed, exiftool_batch)
import jobs  # noqa: E402  (trash / erase run as a detached job: <state>/jobs/gallery.*)
import library_lock  # noqa: E402  (Keep, my tags, Rotate write sidecars under the library lock)
import label  # noqa: E402  (flagged / refused: stdlib only at import)
import owner  # noqa: E402  (owner decisions: Owner|keep / not-trash / trash)
import places  # noqa: E402  (My places: Place|<name>, <state>/places.json)
import plugins  # noqa: E402  (extra tabs: gallery_views())
import review_ui as R  # noqa: E402  (server guards, media with Range, sizes)

HOME, STATE = intake.HOME, intake.STATE
LIBRARY, VIDEOS = intake.LIBRARY, intake.VIDEOS
ROOTS = list(intake.LIB_ROOTS)
PROTECTED = intake.config.protected_tags(intake.CFG)
THUMBS = intake.THUMBS
PROXIES = intake.PROXIES
CACHE = os.path.dirname(THUMBS)
JOB_KEY = "gallery"  # one trash/erase at a time across every gallery page and server
IMG, VID = intake.IMG, intake.VID
THUMB_PX, PREVIEW_PX = 320, 1600
# Video codecs browsers can't play (Chromium on Linux, often Firefox): /m/ serves an H.264 proxy instead.
PROXY_CODECS = {"hevc"}
PROXY_PX = 1920  # the proxy's longer side at most (never scaled up)
PROXY_TIMEOUT = 3600  # one transcode, in seconds; past it the original is served as before
PROXY_REMUX_TIMEOUT = 600  # rewriting a finished proxy as a plain MP4 (a copy, nothing re-encoded), in seconds
PROXY_KBPS = 8000  # the proxy's video bitrate (H.264, longer side at most PROXY_PX)
PROXY_FRAG_MS = 1000  # the proxy is written as fragments this long, so the player can start before it is done
PROXY_POLL = 0.2  # seconds between looks at a proxy being written (its size, a cancel) while it is streamed
# Namespaces "my tag" never writes or removes (docs/intake.md, "Tags: who writes what").
TOOL_PREFIXES = ("project|", "source|", "event|", "place|", "label|", "review|", "ai|", "used|", "owner|")
TAG_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
DEG = {1: 0, 6: 90, 3: 180, 8: 270}
ORIENT = {0: 1, 90: 6, 180: 3, 270: 8}
AI_TRASH = re.compile(r"^AI\|vlm/([^/]+)/trash(?:/([^|]*))?$")
AI_CONF = re.compile(r"^AI\|vlm/([^/]+)/conf/([0-9.]+)$")
SUGGEST_MIN = 0.7  # Suggested junk's line for a vision-check verdict (vlm_trash.TRASH_MIN); the view can go lower
# Suggested junk's Why (?why=): a vision-check reason (vlm_trash.REASONS, or any other a model wrote), or these two
WHY_LABEL, WHY_COPY = "label", "copy"
OPENER = "xdg-open"  # the desktop's default app (found on PATH): media the browser can't decode, a Share's folder
RECENT_DAYS = 30  # "Added in the last 30 days": filed into the library in the last N days (inode change time)
UNDO_KEEP = 50  # the last N undoable actions (Keep, Favorite, my tags, Rotate, Set event) this server can still undo
# Share-safe export (intake.py share-copy --out): each export gets a new dated folder under this one.
SHARE_OUT = os.path.join(HOME, "Pictures", "share-safe")
SHARE_KEEP = 20  # the last N Share folders this server made, by job id, that Open folder may still open

NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_WANT = {
    ("http://ns.adobe.com/lightroom/1.0/", "hierarchicalSubject"): "tags",
    ("http://purl.org/dc/elements/1.1/", "subject"): "subject",  # the dc:Subject leaves (Undo restores them)
    ("http://ns.adobe.com/photoshop/1.0/", "City"): "city",
    ("http://ns.adobe.com/photoshop/1.0/", "State"): "state",
    ("http://ns.adobe.com/photoshop/1.0/", "Country"): "country",
    ("http://ns.adobe.com/exif/1.0/", "DateTimeOriginal"): "date",
    ("http://ns.adobe.com/tiff/1.0/", "Orientation"): "orient",
    ("http://iptc.org/std/Iptc4xmpExt/2008-02-29/", "Event"): "event",
    ("http://www.digikam.org/ns/1.0/", "PickLabel"): "pick",
    ("http://ns.adobe.com/xap/1.0/", "Rating"): "rating",  # XMP-xmp:Rating, 0-5 stars: >= 5 a favorite (owner.py)
    ("http://www.digikam.org/ns/1.0/", "TagsList"): "tagslist",
    ("http://ns.google.com/photos/1.0/camera/", "BurstID"): "burst_id",  # XMP-GCamera:BurstID, if a tool wrote it
    ("http://ns.adobe.com/exif/1.0/", "GPSLatitude"): "gpslat",  # tag wrote them: My places, never sent to the page
    ("http://ns.adobe.com/exif/1.0/", "GPSLongitude"): "gpslon",
}
LISTS = {"tags", "tagslist", "subject"}
DATE_RX = re.compile(r"(\d{4})[-:](\d\d)[-:](\d\d)(?:[T ](\d\d):(\d\d))?")
PATH_DATE = re.compile(r"(?:^|/)((?:19|20)\d\d)-(\d\d)(?:-(\d\d))?")
NAME_DATE = re.compile(r"((?:19|20)\d\d)(\d\d)(\d\d)[_-]?(\d\d)?(\d\d)?")
FULL_DATE = re.compile(r"(\d{4})[-:](\d\d)[-:](\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?")


# ---------------------------------------------------------------- sidecars (read: stdlib XML, fast)
def _split(tag):
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def read_xmp(path):
    """{tags, subject, tagslist, city, state, country, date, orient, event, pick, rating} from an XMP sidecar (a path, or
    a binary file object)."""
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
    # the city (a Place| tag with a comma, intake.py's); a place you named is in myplaces (places.py)
    it["place"] = ([t for t in pre("Place|") if "," in t] or [", ".join(x for x in it.get("xplace", ()) if x)])[0]
    it["myplaces"] = [t[len(places.PREFIX):] for t in tags if places.is_mine(t)]
    it["labels"] = pre("Label|")
    it["mine"] = [t for t in tags if owner_tag(t)]
    it["used"] = sorted(t[5:] for t in tags if t.startswith("Used|"))
    trash_why = {}  # model -> the reason it gave ("blurry", ...; "" when none)
    for t in tags:
        m = AI_TRASH.match(t)
        if m:
            trash_why[m.group(1)] = m.group(2) or trash_why.get(m.group(1), "")
    trash_models = set(trash_why)
    confs = {}
    for t in tags:
        m = AI_CONF.match(t)
        if m:
            try:
                confs[m.group(1)] = float(m.group(2))
            except ValueError:
                pass  # malformed confidence in a sidecar: ignore that one tag
    ai = max((confs.get(mo, 0.0) for mo in trash_models), default=None)
    why = trash_why[max(trash_models, key=lambda mo: (confs.get(mo, 0.0), mo))] if trash_models else None
    old = [t[len("Review|vlm-trash/"):] for t in tags if t.startswith("Review|vlm-trash/")]
    if old:  # the older verdict tag, written only at confidence >= 0.7
        why = why or old[0].split("/")[0]
        ai = max(ai or 0.0, SUGGEST_MIN)
    it["ai_trash"] = ai
    it["ai_why"] = why or None  # the reason of the most confident trash verdict, if it gave one
    it["ai_keep"] = any(t == "Review|vlm-keep" or (t.startswith("AI|vlm/") and "/keep" in t) for t in tags)
    it["kept"] = owner.kept(tags, it.get("pick", ""))  # your Keep (the tile's "Kept"); a favorite is it["fav"]
    it["fav"] = owner.favorite(it.get("rating"))
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
    attach_bursts(items, index)
    return items, skipped


# ---------------------------------------------------------------- burst stacks
def iso_seconds(s):
    """'YYYY-MM-DDTHH:MM:SS' from an XMP / EXIF date string (seconds 00 when it has none), else None."""
    m = FULL_DATE.search(s or "")
    if not m:
        return None
    y, mo, d, h, mi, sec = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{sec or '00'}"


def plan_entries(state=None):
    """{library path: plan entry} from every batch's plan.json under the state folder: a filed file by its
    dst, a released one (quarantine or review, kept) by its lib_dst. A later batch (by name) wins."""
    out = {}
    bdir = os.path.join(state or STATE, "batches")
    try:
        names = sorted(os.listdir(bdir))
    except OSError:
        return out
    for b in names:
        if not R.BATCH_RE.match(b):
            continue
        try:
            plan = intake.load(os.path.join(bdir, b, "plan.json"), []) or []
        except (OSError, ValueError):
            continue  # a broken plan.json stacks nothing of its batch; it never stops the gallery
        if not isinstance(plan, list):
            continue
        for p in plan:
            if not isinstance(p, dict):
                continue
            for k in ("lib_dst", "dst"):  # dst last: a filed file's own place wins
                v = p.get(k)
                if isinstance(v, str) and v:
                    out[v] = p
    return out


def attach_bursts(items, index=None, state=None):
    """Give every item `burst` (a group number shared by the shots of one burst, bursts.py) and `burst_pos`
    (its place in the burst, in time order), else None. No file is opened.

    A shot's date (with seconds), device, dHash and camera burst id come from the plan entry intake wrote
    for it (plan_entries; the dHash also from index.json), as on the review page. A library file with no
    plan entry is stacked only by a camera's own marker (an XMP-GCamera:BurstID in its sidecar, or a burst
    file name: bursts.MARKERS), never by the clock alone, since its device is unknown. A file intake planned
    as a near-duplicate (a copy, not a shot) is never stacked, whatever its time. Videos never are."""
    if index is None:
        index = intake.load(os.path.join(state or STATE, "index.json"), {}) or {}
    plans = plan_entries(state)
    recs = []
    for n, it in enumerate(items):
        it["burst"] = it["burst_pos"] = None
        if it.get("media") != "photo":
            continue
        p = plans.get(it["path"])
        if p is not None:
            if str(p.get("why") or "").startswith("near-duplicate"):
                continue
            dh = p.get("dhash")
            if not isinstance(dh, int):
                dh = (index.get(it["path"]) or {}).get("dhash") if isinstance(index, dict) else None
            bid = p.get("burst_id") if isinstance(p.get("burst_id"), str) else None
            recs.append(dict(path=it["path"], datetime=p.get("date"), device=p.get("device"),
                             dhash=dh if isinstance(dh, int) else None, burst_id=bid or it.get("xburst") or None, n=n))
            continue
        r = dict(path=it["path"], burst_id=it.get("xburst") or None)
        if bursts.marker(r) is None:
            continue
        # Their own device bucket, so they never join planned shots of an unknown device on the clock alone;
        # with a marker each, same_burst groups them by marker only.
        r.update(datetime=iso_seconds(it.get("xdate")) or iso_seconds(it.get("date")), device="-marker-",
                 dhash=None, n=n)
        recs.append(r)
    gs = bursts.groups(recs)
    for g, members in enumerate(gs):
        for k, r in enumerate(members):
            items[r["n"]]["burst"], items[r["n"]]["burst_pos"] = g, k
    return len(gs)


def load_sidecar(it):
    x = read_xmp(intake.sidecar(it["path"])) if os.path.isfile(intake.sidecar(it["path"])) else {}
    it["has_xmp"] = bool(x) or os.path.isfile(intake.sidecar(it["path"]))
    it["tags"] = [t for t in x.get("tags", []) if t]
    it["tagslist"] = x.get("tagslist", [])
    it["pick"] = str(x.get("pick", "")).strip()
    it["rating"] = str(x.get("rating", "")).strip()
    it["xevent"] = x.get("event") or ""
    it["xdate"] = x.get("date") or ""  # with its seconds, for burst grouping (it["date"] keeps minutes)
    it["xburst"] = x.get("burst_id") or ""
    it["xplace"] = (x.get("city"), x.get("state"), x.get("country"))
    it["gps"] = places.gps_of(x.get("gpslat"), x.get("gpslon"))  # server side only: never in a reply
    it["xmp_read"] = bool(x)  # False: no sidecar, or one that didn't parse (places.Store.plan infers nothing)
    try:
        it["orient"] = int(x["orient"]) if x.get("orient") else None
    except ValueError:
        it["orient"] = None
    it["date"] = norm_date(x.get("date")) or guess_date(it["path"], it["root"], it["mtime"])
    facts(it)
    it["when"], it["src"] = App.when_of(it), App.source_of(it)  # "probably here" (App.probably)


# ---------------------------------------------------------------- thumbnails / previews
def private_cache():
    for d in (os.path.dirname(CACHE), CACHE, THUMBS, PROXIES):
        os.makedirs(d, mode=0o700, exist_ok=True)
    for d in (CACHE, THUMBS, PROXIES):
        os.chmod(d, 0o700)


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


def thumb_turn(it):
    """The clockwise turn baked into this item's thumbnail (its cache key, intake.gallery_thumb_path): a video's
    sidecar orientation (players ignore it), a photo's sidecar orientation on top of its own EXIF one (the
    browser applies that). A photo with no sidecar orientation is 0 without opening the file."""
    if it["media"] == "video":
        return DEG[effective_orientation(it)]
    return css_delta(it) if it.get("orient") in DEG else 0


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


# ---------------------------------------------------------------- H.264 proxies (HEVC in the browser)
def video_info(path):
    """dict(codec, width, height) of the first video stream (ffprobe: codec "h264", "hevc", ...; the stored size,
    before any rotation the file asks for), or None when it can't be read. A size ffprobe doesn't give is 0."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height", "-of", "csv=p=0", "file:" + path], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.split() if r.returncode == 0 else []
    if not lines:
        return None
    bits = lines[0].split(",")
    dims = [int(b) if b.isdigit() else 0 for b in bits[1:3]] + [0, 0]
    return dict(codec=bits[0].strip().lower(), width=dims[0], height=dims[1]) if bits[0].strip() else None


def video_codec(path):
    """The first video stream's codec name (ffprobe: "h264", "hevc", ...), or None when it can't be read."""
    return (video_info(path) or {}).get("codec")


def proxy_size(width, height):
    """(width, height) of the proxy of a video stored at width x height: the longer side at most PROXY_PX, never
    scaled up, both even (H.264 4:2:0). None when the size is unknown (the pipeline then keeps the original's)."""
    if not (width and height and width > 0 and height > 0):
        return None
    k = min(1.0, PROXY_PX / max(width, height))
    return max(2, int(width * k) // 2 * 2), max(2, int(height * k) // 2 * 2)


def ffmpeg_encoder(names):
    """The first of names that this ffmpeg can encode with, or None."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return next((n for n in names if f" {n} " in out), None)


def ffmpeg_decodes_hevc():
    """True when this ffmpeg has an HEVC decoder (Fedora's ffmpeg-free has none: its transcode would only fail)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return " hevc " in out


def gst_element(names):
    """The first of names that GStreamer has (gst-inspect-1.0 NAME), or None."""
    for n in names:
        try:
            if subprocess.run(["gst-inspect-1.0", n], capture_output=True, timeout=60,
                              stdin=subprocess.DEVNULL).returncode == 0:
                return n
        except (OSError, subprocess.SubprocessError):
            return None
    return None


SAFE_GST_PATH = re.compile(r"[A-Za-z0-9._/+-]+")  # what may stand as a location= value in a gst-launch pipeline
# GStreamer H.264 encoders for the proxy, in the order tried: on the GPU (VA-API, a few times faster than the
# CPU ones on a 4K clip), then one on the CPU. cli.py's doctor reports the same names (GST_H264_ENCODERS).
GST_H264_HW = ("vah264enc",)
GST_H264_SW = ("x264enc", "openh264enc")
GST_ENC_PROPS = {"vah264enc": [f"bitrate={PROXY_KBPS}", "key-int-max=60"],
                 "x264enc": ["speed-preset=veryfast", f"bitrate={PROXY_KBPS}", "key-int-max=60"],
                 "openh264enc": [f"bitrate={PROXY_KBPS * 1000}", "gop-size=60"]}


def proxy_commands(src, work, size=None):
    """Commands that each write an H.264 MP4 of src to <work>/out.mp4, in the order to try them. The MP4 is
    fragmented (a fragment every PROXY_FRAG_MS) so /m/ can stream it to the player while it is being written;
    make_proxy then rewrites the finished file as a plain one (remux_proxy). size: the proxy's (width, height)
    (proxy_size), or None to keep the original's.

    ffmpeg first, only when it has an HEVC decoder (Fedora's ffmpeg-free has none), src as a file: path; then
    GStreamer's GPU HEVC decoder into an H.264 encoder, the GPU's (vah264enc, scaled on the GPU) before one on the
    CPU, each with the sound when it is AAC, then without. gst-launch-1.0 re-parses its arguments as one pipeline,
    so a file name with "!", quotes or spaces could add elements: it reads <work>/in, a link to src that
    make_proxy makes, and both of its paths are checked against SAFE_GST_PATH (else GStreamer is not tried, said
    once per folder on stderr). The argv is built as a list, never split from a string. Nothing is fetched."""
    cmds, out, link = [], os.path.join(work, "out.mp4"), os.path.join(work, "in")
    enc = ffmpeg_encoder(("libx264", "libopenh264")) if ffmpeg_decodes_hevc() else None
    if enc:
        q = ["-preset", "veryfast", "-crf", "23"] if enc == "libx264" else ["-b:v", f"{PROXY_KBPS}k"]
        vf = (f"scale='min({PROXY_PX},iw)':'min({PROXY_PX},ih)':force_original_aspect_ratio=decrease:"
              "force_divisible_by=2,format=yuv420p")
        cmds.append(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "file:" + src, "-map", "0:v:0", "-map", "0:a:0?",
                     "-map_metadata", "-1", "-vf", vf, "-c:v", enc, *q, "-force_key_frames", "expr:gte(t,n_forced*1)",
                     "-c:a", "aac", "-b:a", "160k", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                     "-frag_duration", str(PROXY_FRAG_MS * 1000), "-f", "mp4", "file:" + out])
    import scan_sensitive  # the GPU HEVC decoder thumbnails and scans use
    dec = scan_sensitive._gst_hevc_decoder()
    if not dec:
        return cmds
    if not SAFE_GST_PATH.fullmatch(work):
        scan_sensitive.note_gst_skipped(os.path.dirname(work), "HEVC clips get no GStreamer proxy")
        return cmds
    encs = list(dict.fromkeys(e for e in (gst_element(GST_H264_HW), gst_element(GST_H264_SW)) if e))
    wh = f",width={size[0]},height={size[1]}" if size else ""
    for genc in encs:
        if dec == "vah265dec" and genc in GST_H264_HW:  # decoded, scaled and encoded without leaving the GPU
            conv = ["vapostproc", "!", *([f"video/x-raw(memory:VAMemory){wh}", "!"] if wh else [])]
        elif dec == "vah265dec":
            conv = ["vapostproc", "!", *([f"video/x-raw{wh}", "!"] if wh else []), "videoconvert", "!"]
        else:  # Vulkan: frames come back to memory, scaled on the CPU
            conv = ["vulkandownload", "!", "videoconvert", "!", "videoscale", "!",
                    *([f"video/x-raw{wh}", "!"] if wh else [])]
        video = ["filesrc", "location=" + link, "!", "qtdemux", "name=d", "d.video_0", "!", "queue", "!", "h265parse",
                 "!", dec, "!", *conv, genc, *GST_ENC_PROPS.get(genc, []), "!", "h264parse", "!", "mp4mux",
                 "name=mux", f"fragment-duration={PROXY_FRAG_MS}", "streamable=true", "!", "filesink",
                 "location=" + out]
        for sound in (["d.audio_0", "!", "queue", "!", "aacparse", "!", "mux."], []):
            cmds.append(["gst-launch-1.0", "-q", *video, *sound])
    return cmds


def remux_proxy(src, dst):
    """Rewrite the fragmented MP4 src as a plain MP4 with its index first (ffmpeg -c copy +faststart: no decoder,
    nothing re-encoded) at dst, made 0600 before ffmpeg writes it. True when ffmpeg made a non-empty file."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(dst)
    os.close(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "file:" + src, "-map", "0", "-c", "copy",
                            "-movflags", "+faststart", "-f", "mp4", "file:" + dst], capture_output=True,
                           timeout=PROXY_REMUX_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and os.path.getsize(dst) > 0


class ProxyJob:
    """One video's proxy being made (App.playable starts it on a thread, make_proxy runs it). While an attempt
    runs, its fragmented MP4 grows at .out and /m/ streams it (Handler.stream_proxy); .dst gets the finished
    plain MP4. readers: the /m/ replies following it; a job nobody follows is cancelled when another video is
    opened, and one whose video goes into a Trash or Erase job stops. Every field is read and written under
    cond."""

    def __init__(self, sha, it, dst):
        self.sha, self.it, self.dst = sha, it, dst
        self.cond = threading.Condition()
        self.out = None  # the running attempt's growing file
        self.attempt = -1  # which of proxy_commands is running (a reader of an earlier one stops)
        self.size = 0  # bytes in .out so far
        self.readers = 0
        self.done = self.ok = False
        self.written = False  # the last attempt's tool exited with its whole file written (remux may follow)
        self.cancel = threading.Event()
        self.thread = None

    def started(self, attempt, out):
        with self.cond:
            self.attempt, self.out, self.size = attempt, out, 0
            self.cond.notify_all()

    def grew(self, size):
        with self.cond:
            if size > self.size:
                self.size = size
                self.cond.notify_all()

    def wrote(self):
        with self.cond:
            self.written = True
            self.cond.notify_all()

    def end(self, ok):
        with self.cond:
            self.done, self.ok = True, ok
            self.cond.notify_all()

    def join(self):
        """Take a reader's place (App.playable, under its lock): the job is not cancelled while one follows it."""
        with self.cond:
            self.readers += 1

    def leave(self):
        with self.cond:
            self.readers -= 1

    def stopped(self):
        """Cancelled, or its video went into a Trash or Erase job (or is gone): the transcode stops."""
        return self.cancel.is_set() or bool(self.it.get("busy") or self.it.get("gone"))

    def idle(self):
        with self.cond:
            return self.readers == 0

    def wait_done(self, timeout=None):
        with self.cond:
            self.cond.wait_for(lambda: self.done, timeout)
            return self.done

    def open_stream(self):
        """(open file, attempt) of the running attempt once it has bytes; None when the job ended first (or its
        file went before it could be opened: the caller waits for the end and serves what it made)."""
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.done or self.size > 0)
                if self.done:
                    return None
                attempt, path = self.attempt, self.out
            try:
                return open(path, "rb"), attempt
            except OSError:  # that attempt failed and its file went: follow the next one, or the end
                with self.cond:
                    self.cond.wait_for(lambda: self.done or self.attempt != attempt)

    def more(self, attempt):
        """Wait up to PROXY_POLL for the attempt being read to grow or end. False once it is over (its tool exited
        with the file written, the job ended, or that attempt failed and another began): what is left in its file
        is then all there will be, so the reply ends without waiting for the remux."""
        with self.cond:
            if self.done or self.written or self.attempt != attempt:
                return False
            size = self.size
            self.cond.wait_for(lambda: self.done or self.written or self.attempt != attempt or self.size > size,
                               PROXY_POLL)
            return not (self.done or self.written or self.attempt != attempt)


def _run_watched(argv, out, job):
    """Run one proxy command; while it runs, tell job how far out has grown. Its exit code, or None when it was
    stopped: job cancelled, or past PROXY_TIMEOUT. The process never outlives this call."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + PROXY_TIMEOUT
    try:
        while True:
            try:
                rc = proc.wait(timeout=PROXY_POLL)
                break
            except subprocess.TimeoutExpired:
                pass
            if job:
                with contextlib.suppress(OSError):
                    job.grew(os.path.getsize(out))
            if (job and job.stopped()) or time.monotonic() > deadline:
                return None
        if job:
            with contextlib.suppress(OSError):
                job.grew(os.path.getsize(out))
        return rc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def make_proxy(src, dst, job=None):
    """Write an H.264 MP4 of src to dst (0600, in the 0700 cache), trying each of proxy_commands in turn, in a
    private work folder beside dst (the output, and the link GStreamer reads src through). The command writes a
    fragmented MP4 that job (a ProxyJob, when /m/ streams it) follows as it grows; the finished file is rewritten
    as a plain MP4 (remux_proxy; the fragmented one is kept when that fails) and kept only when ffprobe reads it
    as H.264. True once one did; False, with nothing left behind, when none did or job was cancelled. The
    original is only read."""
    work = tempfile.mkdtemp(prefix=".work-", dir=os.path.dirname(dst))  # 0700
    out, plain = os.path.join(work, "out.mp4"), os.path.join(work, "plain.mp4")
    try:
        os.symlink(os.path.abspath(src), os.path.join(work, "in"))
        info = video_info(src) or {}
        size = proxy_size(info.get("width"), info.get("height"))
        for n, argv in enumerate(proxy_commands(src, work, size)):
            if job and job.stopped():
                return False
            with contextlib.suppress(FileNotFoundError):
                os.remove(out)  # a reader of the last attempt keeps its own open copy
            os.close(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))  # the tools keep its mode
            if job:
                job.started(n, out)
            try:
                rc = _run_watched(argv, out, job)
            except (OSError, subprocess.SubprocessError):
                continue
            if rc is None:
                return False  # cancelled or timed out: the next command would take as long
            if rc == 0 and os.path.getsize(out) and video_codec(out) == "h264":
                if job:
                    job.wrote()
                keep = plain if remux_proxy(out, plain) and video_codec(plain) == "h264" else out
                os.chmod(keep, 0o600)
                os.replace(keep, dst)
                return True
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- app
class LockBusy(RuntimeError):
    """The library lock or the gallery's job lock is held by someone else (an HTTP 409 with "busy": true). Its
    message is busy_text(): plain words for what is running, never a PID or the lock file's path."""


class Changed(RuntimeError):
    """Undo refused: an item's sidecar changed since the action (another action, the CLI, digiKam); an HTTP 409."""


def sidecar_state(path):
    """(sha256 of the sidecar's bytes or None when it doesn't exist, the fields an undoable action writes): read
    fresh from the file, so a sidecar some other tool changed since the gallery's scan is seen as it is now."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None, dict(tags=frozenset(), subject=frozenset(), orient=None, xevent="", rating=None)
    x = read_xmp(io.BytesIO(data))
    return hashlib.sha256(data).hexdigest(), dict(
        tags=frozenset(t for t in x.get("tags", []) if t), subject=frozenset(t for t in x.get("subject", []) if t),
        orient=(x.get("orient") or "").strip() or None, xevent=(x.get("event") or "").strip(),
        rating=(x.get("rating") or "").strip() or None)


# What each undoable action may change in a sidecar, so an undo can only reverse that (see App.undo): Keep its
# Owner| / Review| tags, my tags only tags of yours, Set event only Event| tags, Rotate no tag at all, Favorite
# only xmp:Rating.
KEEP_TAGS = frozenset(("Review|maybe-trash", owner.TRASH, *owner.LEGACY, owner.KEEP, owner.NOT_TRASH))
UNDO_SCOPE = {
    "keep": dict(tags=lambda t: t in KEEP_TAGS, orient=False, xevent=False, rating=False),
    "tag": dict(tags=owner_tag, orient=False, xevent=False, rating=False),
    "event": dict(tags=lambda t: t.startswith("Event|"), orient=False, xevent=True, rating=False),
    "rotate": dict(tags=lambda t: False, orient=True, xevent=False, rating=False),
    "favorite": dict(tags=lambda t: False, orient=False, xevent=False, rating=True),
}
RATING_RE = re.compile(r"^-?\d{1,2}(?:\.\d{1,3})?$")  # an xmp:Rating an undo may write back


# Internal names -> the words a user reads. The one place: the page gets it as WORDS (page()), and the server's
# own messages (busy_text, Keep, the tag refusal) use it too. No tool namespace (Owner|, Review|, Label|, ...),
# PID or lock path reaches the page's text (tests/browser/test_browser_gallery_dialogs.py scans for them).
WORDS = {
    # tile flags (summary()["f"]) and the Flags filter's values (?review=)
    "flag": {"maybe-trash": "Tagged as junk", "child-in-shot": "Possible child", "untagged": "Untagged",
             "favorite": "Favorite", "kept": "Kept", "trashed copy": "Copy of a trashed photo",
             "used": "Used in a post"},
    # the Junk check filter's values (?ai=), and the AI| / Review|vlm-* verdict tags in the large view
    "ai": {"trash": "Judged junk", "keep": "Judged fine", "none": "Not checked"},
    "score": "Junk score",  # an AI trash verdict's confidence, 0-1
    # Suggested junk's Why (?why=, summary()["r"]): the vision check's reasons (vlm_trash.REASONS), then the
    # other two reasons a file is suggested; a reason not listed here shows as the model wrote it
    "why": {"blurry": "Blurry", "accidental": "Accidental shot", "dark": "Too dark", "packaging": "Packaging",
            "random-object": "Random object", "screenshot-junk": "Junk screenshot", "duplicate-ish": "Near-copy",
            "other": "Other", WHY_LABEL: "Tagged as junk", WHY_COPY: "Copy of a trashed photo"},
    # filter sections and their chips
    "filter": {"kind": "Kind", "ym": "When", "project": "Project", "source": "Source", "place": "Place",
               "event": "Event", "label": "Looks like", "mine": "My tag", "review": "Flags", "ai": "Junk check",
               "used": "Used", "added": "Added", "myplace": "My place", "dates": "Date check"},
    # a tool tag's namespace, shown as "<word>: <value>" in the large view (Owner| is not shown there:
    # the Details row says "Marked Keep")
    "ns": {"Project": "Project", "Source": "Source", "Event": "Event", "Place": "Place", "Label": "Looks like",
           "Used": "Used in", "AI": "Junk check", "Review": "Flag"},
    # whole tags with their own words
    "tag": {"Review|maybe-trash": "Tagged as junk", "Review|child-in-shot": "Possible child",
            "Review|vlm-keep": "Junk check: Judged fine"},
    # tool namespaces "my tag" refuses, as a user reads them (the refusal and the tag dialog)
    "tools": "Project, Source, Event, Place, Label, Review, AI, Used and Owner",
    # a finished Share's button: its folder in the file manager (POST /api/share/open)
    "openFolder": "Open folder",
    # a job, by what started it (its spec's label) and by its step (intake.py's subcommand)
    "job": {"trash": "Trash", "erase": "Erase", "share": "Share",
            "forget": "Erase", "share-copy": "Share"},
    # the sidebar views
    "view": {"lib": "Library", "sug": "Suggested junk", "fav": "Favorites",
             "recent": f"Added in the last {RECENT_DAYS} days", "child": "Possible child", "untagged": "Untagged",
             "dates": "Dates to check"},
    # Dates to check's groups: check-dates' kinds (datecheck.TITLES)
    "dates": {k: datecheck.TITLES[k] for k in datecheck.DATE_KINDS},
}


def busy_text(holder=None):
    """Plain words for a 409 "the library is busy": what is running and what to do, for the page. holder: the
    library lock's note when known (library_lock.Busy.holder), else read now. Never a PID or a path."""
    st = jobs.status(JOB_KEY)
    if st and not st["done"]:
        job = WORDS["job"].get((st.get("spec") or {}).get("label"), "")
        what = (f"an {job}" if job[:1] in "AEIOU" else f"a {job}") if job else "a gallery action"
    else:
        h = holder if holder is not None else library_lock.read_holder()
        what = library_lock.plain_holder(h) or "another media-intake command"
    return f"{what} is still running; wait for it to finish, then try again"


# ---------------------------------------------------------------- event display names (docs/gallery.md, Events)
# The name the sidebar and the title show for an event, in place of its slug. One small file in the state folder,
# never the sidecars: a display name belongs to the event, not to each of its files, so it is one atomic write
# (never one per file, never two files disagreeing) and other XMP readers never see it.
EVENT_NAMES = "event-names.json"  # {"version": 1, "libraries": {<library key>: {<event>: <display name>}}}
EVENT_NAME_MAX = 64


def library_key(roots=None):
    """Which library a display name belongs to: a hash of its folders (the library, videos, project folders),
    so two configs sharing one state folder keep their names apart. Never a path in the file."""
    rs = sorted({os.path.realpath(r) for r in (ROOTS if roots is None else roots)})
    return hashlib.sha256("\0".join(rs).encode()).hexdigest()[:16]


def read_event_names(state=None):
    """The whole file: {library key: {event: display name}}. Missing: {}. Unreadable: None (then never
    overwritten: a write refuses, so another library's names aren't lost)."""
    p = os.path.join(state or STATE, EVENT_NAMES)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    libs = d.get("libraries") if isinstance(d, dict) else None
    if not isinstance(libs, dict):
        return None
    return {k: {e: n for e, n in v.items() if isinstance(e, str) and isinstance(n, str)}
            for k, v in libs.items() if isinstance(k, str) and isinstance(v, dict)}


def load_event_names(state=None, roots=None):
    """{event: display name} for this library ({} when the file is missing or unreadable). A name edited by hand
    into something the page would refuse is left out."""
    out = {}
    for e, n in (read_event_names(state) or {}).get(library_key(roots), {}).items():
        try:
            if check_display_name(n) == n and n:
                out[e] = n
        except ValueError:
            pass
    return out


def save_event_names(names, state=None, roots=None):
    """Write this library's display names (0600, atomically: a temp file then rename), leaving every other
    library's as they are."""
    state = state or STATE
    libs = read_event_names(state)
    if libs is None:
        raise RuntimeError(f"{EVENT_NAMES} in the state folder is unreadable; fix or remove it, then try again")
    key = library_key(roots)
    if names:
        libs[key] = dict(sorted(names.items()))
    else:
        libs.pop(key, None)
    os.makedirs(state, mode=0o700, exist_ok=True)
    write_private_bytes(os.path.join(state, EVENT_NAMES),
                        json.dumps({"version": 1, "libraries": libs}, ensure_ascii=False, indent=1).encode())


def prune_event_names(live, state=None, roots=None):
    """Drop this library's display names for events not in live (no file of the library is in them any more), so
    a name you typed never outlives its event, and an event made again later (apply, tag-files, Set event) starts
    with its default name. Returns the events dropped. An unreadable file is left alone (nothing is written)."""
    state = state or STATE
    libs = read_event_names(state)
    if not libs:
        return set()
    key = library_key(roots)
    mine = libs.get(key) or {}
    stale = {e for e in mine if e not in live}
    if stale:
        names = {e: n for e, n in mine.items() if e not in stale}
        if names:
            libs[key] = names
        else:
            libs.pop(key)
        write_private_bytes(os.path.join(state, EVENT_NAMES),
                            json.dumps({"version": 1, "libraries": libs}, ensure_ascii=False, indent=1).encode())
    return stale


def check_display_name(name):
    """A display name: 1-64 characters on one line, runs of spaces as one, no invisible format characters
    (zero-width, bidi overrides) that would let two names look alike. "" clears it."""
    if not isinstance(name, str):
        raise ValueError("the display name is text, or empty to go back to the default")
    msg = f"a display name is 1-{EVENT_NAME_MAX} visible characters on one line"
    name = unicodedata.normalize("NFC", name)
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs") for ch in name):  # newline, tab, zero-width, ...
        raise ValueError(msg)
    name = " ".join(name.split())
    if name and (len(name) > EVENT_NAME_MAX or not TAG_RE.match(name)):
        raise ValueError(msg)
    return name


def look_key(s):
    """Two labels with the same key would look the same, or too alike: case, width, spacing, and a dash or an
    underscore read as a space (so some-trip, Some trip and some_trip are one key)."""
    return " ".join(unicodedata.normalize("NFKC", s).casefold().replace("-", " ").replace("_", " ").split())


def pretty_event(slug):
    """some-trip-2024 -> Some trip 2024; anything that isn't a plain slug stays as it is."""
    if not intake.config.SLUG.match(slug) or "--" in slug or slug.endswith("-"):
        return slug
    s = slug.replace("-", " ")
    return s[0].upper() + s[1:]


def event_labels(events, names):
    """{event: what the page shows for it}. Never two events that look alike: a display name you set is shown
    unless it looks like another event's slug or name; otherwise the prettified slug, only when nothing else
    looks like it; otherwise the slug itself (slugs are distinct)."""
    raw = collections.defaultdict(set)
    for e in events:
        raw[look_key(e)].add(e)
    mine = collections.defaultdict(set)
    for e in events:
        if names.get(e):
            mine[look_key(names[e])].add(e)
    shown = {}
    for e in events:
        n = names.get(e)
        if n and mine[look_key(n)] == {e} and raw[look_key(n)] <= {e}:
            shown[e] = n
    taken = {look_key(n) for n in shown.values()}
    pretty = collections.defaultdict(set)
    for e in events:
        if e not in shown:
            pretty[look_key(pretty_event(e))].add(e)
    for e in events:
        if e not in shown:
            p, k = pretty_event(e), look_key(pretty_event(e))
            shown[e] = p if pretty[k] == {e} and raw[k] <= {e} and k not in taken else e
    return shown


class App:
    def __init__(self, items, drive=None, views=(), share_out=None):
        self.items = items
        self.share_out = os.path.abspath(share_out or SHARE_OUT)
        self.by_path = {it["path"]: i for i, it in enumerate(items)}
        # (device, inode): how a job's status file names its files, so no path is written (see start_job)
        self.by_fid = {tuple(it["fid"]): i for i, it in enumerate(items) if it.get("fid")}
        self.drive = drive
        self.views = {v["id"]: v for v in views}  # plugin tabs (plugins.py)
        self.token, self.csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.thumb_sem = threading.BoundedSemaphore(3)
        self.proxy_lock = threading.Lock()  # guards proxy_job
        self.proxy_job = None  # the ProxyJob being made: one transcode at a time
        self.port = None
        self.reconciled = None  # id of the last finished job whose results are applied to items
        self.tail = None  # the shown job's log, through an open descriptor (jobs.Tail)
        self.search_cache = {}
        self.undos = collections.OrderedDict()  # token -> what an undoable action changed (App.journal)
        self.shares = collections.OrderedDict()  # job id -> the folder its Share made (share_dir), for Open folder
        self.dc_key, self.dc_report, self.dc = None, None, {}  # check-dates' report, as last read (date_findings)
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
        # Keyed by content and the turn baked in (a copy rotated in its sidecar shows its own rotation; copies
        # turned alike share one file). A photo's EXIF orientation is part of its content, so it needs no key.
        delta = thumb_turn(it)
        dst = intake.gallery_thumb_path(sha, delta)
        if os.path.isfile(dst):
            return dst
        if it.get("nothumb"):
            return None
        with self.thumb_sem:
            if os.path.isfile(dst):
                return dst
            data = video_jpeg(it["path"], THUMB_PX, delta) if it["media"] == "video" else \
                image_jpeg(it["path"], THUMB_PX, delta)
            if not data:
                it["nothumb"] = True
                return None
            private_cache()
            write_private_bytes(dst, data)
        return dst

    def proxy_needed(self, it):
        """The cache path of the H.264 proxy /m/ serves for this item (a video whose codec browsers can't play:
        HEVC), or None when the original is served as it is (not such a video, or no proxy could be made)."""
        if it["media"] != "video" or it.get("noproxy"):
            return None
        if "vcodec" not in it:
            it["vcodec"] = video_codec(it["path"])
        if it["vcodec"] not in PROXY_CODECS:
            return None
        sha = self.sha_of(it)
        return os.path.join(PROXIES, sha + ".mp4") if re.fullmatch(r"[0-9a-f]{64}", sha) else None

    def proxy_state(self, it):
        """What the large view says while it waits: "converting" when this video's proxy is still to be made or
        being made (it plays as soon as the first seconds are), "waiting" when another video's is being made and
        watched (so this one gets its original), else None."""
        dst = self.proxy_needed(it)
        if not dst or os.path.isfile(dst):
            return None
        with self.proxy_lock:
            job = self.proxy_job
            return "waiting" if job and job.dst != dst and not job.idle() else "converting"

    def playable(self, it):
        """What /m/ serves: (path, mime) of the original, or of the cached H.264 proxy of a video whose codec
        browsers can't play (HEVC, cached by sha256 in PROXIES); or (ProxyJob, "video/mp4") while that proxy is
        being made, with a reader's place taken in it (the caller streams it, Handler.stream_proxy, and leaves).
        When no proxy can be made the original is served as before (the page then says the browser can't play
        it) and this item isn't tried again.

        One transcode at a time, on a thread of its own, so no request waits for a whole file: the player starts
        on the first fragments. Requests for the sha being converted (the player's Range requests, a second
        page, a duplicate file) follow that one transcode. Opening another video cancels a transcode nobody is
        watching any more (you moved on) and starts its own; one still being watched elsewhere keeps going and
        the other video gets its original now, and its proxy on a later open."""
        orig = (it["path"], it["mime"])
        dst = self.proxy_needed(it)
        if not dst:
            return orig
        if os.path.isfile(dst):
            return dst, "video/mp4"
        with self.proxy_lock:
            job = self.proxy_job
            if job and job.dst == dst:
                job.join()
                return job, "video/mp4"
            if os.path.isfile(dst):  # finished since the look above
                return dst, "video/mp4"
            if job and not job.idle():
                return orig
            if job:
                job.cancel.set()  # nobody follows it any more: this video goes first
            job = self.proxy_job = ProxyJob(os.path.basename(dst)[:-4], it, dst)
            job.join()
            job.thread = threading.Thread(target=self._make_proxy, args=(job,), daemon=True)
            job.thread.start()
        return job, "video/mp4"

    def _make_proxy(self, job):
        """Run job (App.playable's thread). A proxy finished after its video was trashed or erased, or while a
        job holds it, is deleted at once: no copy of that content stays in the cache."""
        made = False
        try:
            private_cache()
            made = make_proxy(job.it["path"], job.dst, job)
        except OSError:
            made = False
        finally:
            it = job.it
            if made and (it.get("busy") or it["gone"] or not os.path.lexists(it["path"])):
                intake.gallery_thumb_forget(job.sha)
                made = False
            if not made and not job.stopped():
                it["noproxy"] = True  # it failed: not tried again (a cancelled one is, on the next open)
            with self.proxy_lock:
                if self.proxy_job is job:
                    self.proxy_job = None
            job.end(made)

    def close_proxies(self):
        """Stop the transcode still running (the server is stopping): its process ends, its work folder goes."""
        with self.proxy_lock:
            job = self.proxy_job
        if job:
            job.cancel.set()
            if job.thread:
                job.thread.join(30)

    def drop_thumb(self, it):
        """After a rotate: the item's new turn has its own cache key, so its next /t/ request shows it at once.
        The other variants stay (a same-content copy may still show them; trash, forget and erase remove all,
        and `media-intake prune-cache` removes the turns no copy shows any more)."""
        it.pop("nothumb", None)

    def summary(self, i, it):
        return dict(id=i, m=it["media"], d=it["date"], mo=it["date"][:7], n=os.path.basename(it["path"]),
                    c=it["ai_trash"], r=it["ai_why"], f=[t[7:] for t in it["tags"] if t in ("Review|maybe-trash", "Review|child-in-shot")]
                    + (["kept"] if it["kept"] else []) + (["trashed copy"] if it["trashed"] else [])
                    + (["used"] if it["used"] else []), fav=it["fav"])

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
                    used=it["used"], kept=it["kept"], fav=it["fav"], has_xmp=it["has_xmp"],
                    heic=it["ext"] in R.CONVERT_IMG and it["show"] == "none",  # no pillow-heif: a thumbnail only
                    gps=it["gps"] is not None, myplaces=it["myplaces"], probably=self.probably(i))

    def facets(self):
        c = {k: collections.Counter() for k in ("project", "source", "event", "place", "label", "month", "mine")}
        counts = collections.Counter()  # the sidebar's view and option counts (whole library, not cross-filtered)
        scores = collections.Counter()  # Judged junk by junk score: the page counts those at or above its slider
        recent = time.time() - RECENT_DAYS * 86400
        dated = self.date_findings()
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
            counts["review:favorite"] += it["fav"]
            counts["ai:trash"] += it["ai_trash"] is not None
            if it["ai_trash"] is not None:
                scores[round(it["ai_trash"], 2)] += 1
            counts["ai:keep"] += it["ai_keep"]
            counts["ai:none"] += not any(x.startswith("AI|") for x in t)
            counts["used:used" if it["used"] else "used:unused"] += 1
            counts["suggest"] += self.suggested(it)
            counts["suggest:lower"] += self.suggested(it, 0.0) and not self.suggested(it)
            counts["recent"] += it.get("ctime", 0) >= recent
            counts["dates"] += it["path"] in dated
            for k in ("project", "source", "place"):
                if it[k]:
                    c[k][it[k]] += 1
            for e in self.events_of(it):  # an item in two events counts in both (the sidebar's Events list)
                c["event"][e] += 1
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
        share = "~/" + os.path.relpath(self.share_out, HOME) if self.share_out.startswith(HOME + os.sep) \
            else self.share_out
        drive = None if not self.drive else "~/" + os.path.relpath(self.drive, HOME) \
            if self.drive.startswith(HOME + os.sep) else self.drive
        names = load_event_names()  # read each time: another gallery on this library may have set one
        shown = event_labels(c["event"], names)
        event_names = {e: dict(label=shown[e], name=names.get(e, "")) for e in c["event"]}
        mine, mine_err = self.my_places()
        return dict(total=n, facets=out, counts=dict(counts), ai_scores=sorted(scores.items()), recent_days=RECENT_DAYS,
                    search=search, share_out=share, drive=drive, event_names=event_names,
                    places=mine, places_error=mine_err, radii=list(places.RADII), radius=places.RADIUS_DEFAULT,
                    dates_checked=(self.dc_report or {}).get("checked"))

    def suggested(self, it, floor=SUGGEST_MIN):
        """Suggested junk: a copy of something you trashed, a content label's junk flag, or a vision-check trash
        verdict scoring at least `floor` (the view's Junk score; SUGGEST_MIN unless you lower it). Never a file you
        kept or starred, one used in a post or one in a protected project, whatever the floor."""
        if it["kept"] or it["fav"] or it["used"] or f"Project|{it['project']}" in PROTECTED or PROTECTED.intersection(it["tags"]):
            return False
        return it["trashed"] or "Review|maybe-trash" in it["tags"] or \
            (it["ai_trash"] is not None and it["ai_trash"] >= floor)

    @staticmethod
    def junk_floor(f):
        """Suggested junk's Junk score floor (?jmin=, 0..1); SUGGEST_MIN when absent or not a number."""
        try:
            v = float(f.get("jmin"))
        except (TypeError, ValueError):
            return SUGGEST_MIN
        return min(1.0, max(0.0, v)) if v == v else SUGGEST_MIN

    def junk_why(self, it, floor):
        """The Why values an item is suggested for at this floor (a reason, WHY_LABEL, WHY_COPY)."""
        out = set()
        if it["ai_trash"] is not None and it["ai_trash"] >= floor:
            out.add(it["ai_why"] or "other")
        if "Review|maybe-trash" in it["tags"]:
            out.add(WHY_LABEL)
        if it["trashed"]:
            out.add(WHY_COPY)
        return out

    def matches(self, it, f):
        if it["gone"]:
            return False
        t = it["tags"]
        if f.get("view") == "suggest":
            floor = self.junk_floor(f)
            if not self.suggested(it, floor) or (f.get("why") and f["why"] not in self.junk_why(it, floor)):
                return False
        if f.get("dates"):
            fs = self.date_findings().get(it["path"])
            if not fs or (f["dates"] != "all" and f["dates"] not in {x.get("kind") for x in fs}):
                return False
        if f.get("kind") in ("photo", "video") and it["media"] != f["kind"]:
            return False
        for k in ("project", "source", "place"):
            if f.get(k) and it[k] != f[k]:
                return False
        if f.get("event") and f["event"] not in self.events_of(it):
            return False
        if f.get("myplace") and f.get("_myplace") not in it["myplaces"]:  # listing() resolved the id to its name
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
        if rv == "favorite" and not it["fav"]:
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
        f = {k: v for k, v in f.items() if not k.startswith("_")}
        if f.get("myplace"):  # a place by its id (it survives a rename); an unknown id matches nothing
            st = self.place_store(quiet=True)
            p = st.places.get(f["myplace"]) if st else None
            f["_myplace"] = p["name"] if p else None
        ids = None
        q = (f.get("q") or "").strip()[:200]
        sim = f.get("similar")
        if sim not in (None, ""):
            try:
                si = int(sim)
            except ValueError:
                si = -1
            if not self.live(si):
                return dict(total=0, items=[], note="That photo is no longer in the library.", ranked=True)
            self.text_checked.wait(timeout=30)
            key = ("similar", si)
            if key not in self.search_cache:
                if not os.path.exists(intake.VENV_PY) or not os.path.exists(os.path.join(STATE, "embeddings.npz")):
                    return dict(total=0, items=[], ranked=True,
                                note="Find similar needs the search index: run media-intake index in a terminal, "
                                     "then try again.")
                try:
                    self.search_cache[key] = self.semantic(["similar", self.items[si]["path"], "-n", "300", "--format", "machine"])
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    return dict(total=0, items=[], ranked=True, note=f"Find similar didn't work: {e}")
            ids, ranked = [i for _, i in self.search_cache[key]], True
            note = f"Similar to {os.path.basename(self.items[si]['path'])}"
        elif q:
            self.text_checked.wait(timeout=30)
            if self.text_why is None:
                key = ("search", q)
                if key not in self.search_cache:
                    try:
                        self.search_cache[key] = self.semantic(["search", q, "-n", "400", "--format", "machine"])
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
            junk = None
            if f.get("view") == "suggest":
                junk = self.junk_summary([i for i in ids if self.live(i)], f)
            ids = [i for i in ids if self.live(i) and self.matches(self.items[i], f)]
            if f.get("view") == "suggest":
                ids.sort(key=lambda i: -(self.items[i]["ai_trash"] if self.items[i]["ai_trash"] is not None else 0.5))
                ranked = True
            dated = self.date_findings() if f.get("dates") else {}
            if f.get("dates"):  # grouped by what check-dates found (its first kind), then by date
                ids.sort(key=lambda i: (self.date_info(self.items[i])["rank"], self.items[i]["date"], i))
                ranked = True
                checked = (self.dc_report or {}).get("checked")
                note = note or (f"From check-dates on {checked.replace('T', ' ')[:16]}. A fix goes to the sidecar "
                                "only; the files stay in their folders." if checked else None)
            try:
                off = max(0, int(f.get("offset") or 0))
                lim = max(1, min(500, int(f.get("limit") or 200)))
            except ValueError:
                off, lim = 0, 200
            info = {}
            if not ranked:
                ids, info = self.stacked(ids)
            page = []
            for i in ids[off:off + lim]:
                row = self.summary(i, self.items[i])
                row.update(info.get(i, {}))
                if self.items[i]["path"] in dated:
                    row.update({k: v for k, v in self.date_info(self.items[i]).items() if k != "rank"})
                page.append(row)
        out = dict(total=len(ids), items=page, note=note, ranked=ranked)
        if junk is not None:
            out["junk"] = junk
        return out

    def junk_summary(self, ids, f):
        """Suggested junk's header: how many each Why holds at this floor (the other filters and the search
        applied, Why itself not), how many more a lower Junk score would add (Why applied), and how many the view
        holds with no other filter or search (the header's "N suggested, M shown with your filters")."""
        floor = self.junk_floor(f)
        base, every = dict(f, why=""), dict(f, jmin="0")
        bare = dict(view="suggest", jmin=str(floor), why=f.get("why") or "")
        unfiltered = sum(1 for it in self.items if self.matches(it, bare))
        counts = collections.Counter()
        n = lower = 0
        for i in ids:
            it = self.items[i]
            if self.matches(it, base):
                n += 1
                counts.update(self.junk_why(it, floor))
            lower += floor > 0 and self.matches(it, every) and not self.matches(it, f)
        return dict(all=n, unfiltered=unfiltered, why=sorted(counts.items(), key=lambda x: (-x[1], x[0])),
                    floor=floor, lower=lower, line=SUGGEST_MIN)

    def stacked(self, ids):
        """The timeline with each burst as one run: when two or more of a burst's shots are listed (after the
        filters), they follow one another in time order at the place of the first one listed, the earliest
        shot first (the cover). Returns (ids, {id: {b: group, bn: shots listed, bc: cover?, mo: the cover's
        month}}). A burst with one shot listed is a plain tile. Ranked lists (search, Similar, Trash
        suggestions) are never stacked: their order is not time."""
        shots = {}
        for i in ids:
            g = self.items[i].get("burst")
            if g is not None:
                shots.setdefault(g, []).append(i)
        out, info, placed = [], {}, set()
        for i in ids:
            g = self.items[i].get("burst")
            ms = shots.get(g) if g is not None else None
            if not ms or len(ms) < 2:
                out.append(i)
                continue
            if g in placed:
                continue
            placed.add(g)
            ms = sorted(ms, key=lambda j: self.items[j]["burst_pos"])
            out.extend(ms)
            mo = self.items[ms[0]]["date"][:7]
            for k, j in enumerate(ms):
                info[j] = dict(b=g, bn=len(ms), bc=k == 0, mo=mo)
        return out, info

    # ---------------------------------------------- your actions (sidecars only)
    # ---------------------------------------------- Dates to check (check-dates' report; fixes: datecheck.py)
    def date_findings(self):
        """{library path: [date findings]} from the last check-dates report (datecheck.visible), re-read when the
        report file changes (a check-dates run, a fix-dates or Fix date write, an undo)."""
        try:
            st = os.stat(datecheck.report_path())
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            key = None
        if key != self.dc_key:
            self.dc_report = datecheck.load_report() if key else None
            self.dc, self.dc_key = datecheck.visible(self.dc_report), key
        return self.dc

    def date_info(self, it):
        """A tile's Dates to check fields: dk its first kind, dq that kind's words, dd the date check-dates saw in
        use (the sidecar's, else the file's own: the tile's d may be a folder guess), dr every reason, ds the
        suggested date ("YYYY-MM-DD HH:MM", or None) and dsrc where it came from; rank orders the groups."""
        fs = self.date_findings().get(it["path"]) or []
        if not fs:
            return dict(rank=99)
        first = min(fs, key=lambda x: datecheck.KINDS.index(x["kind"]))
        s, src = datecheck.best_suggestion(fs)
        d = datecheck.from_iso(first.get("date"))
        return dict(rank=datecheck.KINDS.index(first["kind"]), dk=first["kind"], dq=WORDS["dates"][first["kind"]],
                    dd=d.strftime("%Y-%m-%d %H:%M") if d else None,
                    dr="; ".join(str(x.get("reason") or "") for x in fs), dc=first.get("confidence"),
                    ds=s.strftime("%Y-%m-%d %H:%M") if s else None, dsrc=src)

    def fix_dates(self, ids, mode, value=None):
        """Fix date: the same code as fix-dates (datecheck.plan_fix and write_fix), under the library lock. mode
        set (value a date), shift (value like +8y) or suggested (check-dates' suggestion). Sidecars only; a file
        that can't take the date is left as it is and counted. Undoable (/api/undo, datecheck.write_undo)."""
        if mode not in ("set", "shift", "suggested"):
            raise ValueError("set, shift or suggested")
        if mode != "suggested" and (not isinstance(value, str) or not value.strip() or len(value) > 40):
            raise ValueError("a date (YYYY-MM-DD or YYYY-MM-DD HH:MM)" if mode == "set" else "an amount, like +8y")
        with self.writing("fix date"), self.lock:
            ids = self.targets(ids)
            paths = {self.items[i]["path"]: i for i in ids}
            changes, refused = datecheck.plan_fix(list(paths), mode, value, what="Date" if mode == "set" else "Shift",
                                                  report=self.dc_report if mode == "suggested" else None)
            if not changes:
                why = next(iter(refused.values()), "nothing to change")
                raise ValueError(f"nothing was changed: {why}")
            fid = datecheck.write_fix(changes, "gallery Fix date")
            done = [paths[os.path.abspath(c["path"])] for c in changes if os.path.abspath(c["path"]) in paths]
            self.refresh(ids)
            self.date_findings()
            n = len(changes)
            msg = f"Fixed the date of {intake.plural(n, 'file')}" + (f"; {intake.plural(len(refused), 'file')} left as before "
                                                                f"({next(iter(refused.values()))})" if refused else "") \
                + ". In the sidecar only: the files stay in their folders."
            token = secrets.token_urlsafe(12)
            self.undos[token] = dict(kind="date", fix=fid, ids=done)
            while len(self.undos) > UNDO_KEEP:
                self.undos.popitem(last=False)
            return dict(result=msg, undo=token)

    def targets(self, ids):
        if not isinstance(ids, list) or not ids or len(ids) > 5000:
            raise ValueError("no items")
        out = []
        for i in ids:
            if not isinstance(i, int) or isinstance(i, bool) or not self.live(i):
                raise ValueError("unknown or removed item")
            out.append(i)
        out = list(dict.fromkeys(out))
        # Deleted (or replaced by something else) since the scan, e.g. by a CLI trash or a job no page watched:
        # refuse the whole request before anything is written, so no sidecar is made for a file that is gone,
        # and drop it from the grid.
        missing = [i for i in out if not os.path.isfile(self.items[i]["path"])]
        if missing:
            for i in missing:
                self.drop_gone(self.items[i])
            raise ValueError(f"{len(missing)} of these files are no longer on disk; nothing was changed")
        return out

    def summaries(self, ids):
        """The grid's summary of each live item in ids (after an edit, so the page can redraw those tiles)."""
        with self.lock:
            ok = [i for i in dict.fromkeys(ids or []) if type(i) is int and self.live(i)]
            return [self.summary(i, self.items[i]) for i in ok]

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
            raise LockBusy(busy_text(e.holder)) from None
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
            before = self.snapshots(ids)
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
            # Owner|keep + Owner|not-trash added; Review|maybe-trash and Owner|trash removed (docs/gallery.md)
            n = len(ids)
            return self.journal("keep", before, f"Kept {n}. {'It' if n == 1 else 'They'} won't be suggested "
                                                "as junk again.")

    def favorite(self, ids, on):
        """Star (on=True) or unstar items: XMP-xmp:Rating = owner.FAVORITE (5) in the sidecar, or the rating
        deleted, and nothing else. An item already a favorite (rating >= 5, from digiKam too) isn't written
        again on a star; unstarring deletes its rating, whatever it was. Undoable (/api/undo)."""
        if not isinstance(on, bool):
            raise ValueError("on is true or false")
        with self.writing("favorite"), self.lock:
            ids = self.targets(ids)
            before = self.snapshots(ids)
            blocks = []
            for i in ids:
                it = self.items[i]
                if it["fav"] == on:
                    continue
                x = self.ensure_sidecar(it) if on else intake.sidecar(it["path"])
                blocks.append(["-q", "-m", "-overwrite_original", "-n",
                               f"-XMP-xmp:Rating={owner.FAVORITE if on else ''}", x])
            self.run_exiftool(blocks)
            self.refresh(ids)
            n, word = len(blocks), WORDS["view"]["fav"]
            if not n:
                return f"{'Already' if on else 'Not'} in {word}: nothing changed."
            msg = f"Added {n} to {word}. {'It' if n == 1 else 'They'} won't be suggested as junk." if on \
                else f"Removed {n} from {word}."
            return self.journal("favorite", before, msg)

    def tag_block(self, it, add=(), remove=(), extra=()):
        """One exiftool block for one sidecar: HierarchicalSubject tags added / removed with their dc:Subject
        leaves (a removed leaf only when no other tag still uses it), then extra arguments. The one place that
        edits a sidecar's tag list: my tags, events (set, rename, merge) all go through here."""
        b = ["-q", "-m", "-overwrite_original", "-sep", "@@sep@@"]
        adding = {t.split("|")[-1] for t in add}
        for t in remove:
            b.append(f"-XMP-lr:HierarchicalSubject-={t}")
        for leaf in sorted({t.split("|")[-1] for t in remove} - adding):
            if self.leaf_free(it, leaf, set(remove)):
                b.append(f"-XMP-dc:Subject-={leaf}")
        for t in add:
            leaf = t.split("|")[-1]
            b += [f"-XMP-lr:HierarchicalSubject-={t}", f"-XMP-lr:HierarchicalSubject+={t}",
                  f"-XMP-dc:Subject-={leaf}", f"-XMP-dc:Subject+={leaf}"]
        return b + list(extra) + [self.ensure_sidecar(it) if add else intake.sidecar(it["path"])]

    def tag(self, ids, tag, op):
        tag = (tag or "").strip()
        if not TAG_RE.match(tag) or tag.startswith("-") or "@@sep@@" in tag:
            raise ValueError("a tag is 1-64 characters on one line")
        if not owner_tag(tag):
            raise ValueError(f"{WORDS['tools']} tags belong to the tools; pick another name")
        if op not in ("add", "remove"):
            raise ValueError("op is add or remove")
        with self.writing("tag"), self.lock:
            ids = self.targets(ids)
            before = self.snapshots(ids)
            blocks = []
            for i in ids:
                it = self.items[i]
                if op == "add":
                    blocks.append(self.tag_block(it, add=[tag]))
                elif tag in it["tags"]:
                    blocks.append(self.tag_block(it, remove=[tag]))
            self.run_exiftool(blocks)
            self.refresh(ids)
            return self.journal("tag", before, f"{'added' if op == 'add' else 'removed'} {tag!r} on {len(blocks)}")

    # ---------------------------------------------- My places (places.py: Place|<name>, <state>/places.json)
    @staticmethod
    def place_store(quiet=False):
        """This library's places (read each time: the CLI or another gallery may have changed them). quiet: None
        when places.json is unreadable, else RuntimeError (a 409 on the page)."""
        try:
            return places.Store(STATE, ROOTS)
        except places.Unreadable as e:
            if quiet:
                return None
            raise RuntimeError(str(e)) from None

    def my_places(self):
        """([dict(id, name, n)] for the sidebar, most files first; "" or why they can't be read)."""
        st = self.place_store(quiet=True)
        if st is None:
            return [], f"{places.FILE} in the state folder is unreadable"
        n = collections.Counter(p for it in self.items if not it["gone"] for p in it["myplaces"])
        rows = [dict(id=pid, name=p["name"], n=n[p["name"]]) for pid, p in st.places.items()]
        return sorted(rows, key=lambda r: (-r["n"], places.look_key(r["name"]))), ""

    @staticmethod
    def when_of(it):
        m = FULL_DATE.search(it.get("xdate") or "") or FULL_DATE.search(it.get("date") or "")
        if not m:
            return None
        y, mo, d, h, mi, sec = m.groups()
        try:
            return datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
        except ValueError:
            return None

    @staticmethod
    def source_of(it):
        return tuple(sorted(t for t in it["tags"] if t.startswith("Source|")))

    def probably(self, i, st=None):
        """"Probably here" for an item with no GPS and none of your places: [dict(id, name, minutes)] from
        places.Store.probable over the library's GPS'd items. A suggestion only; nothing is written."""
        it = self.items[i]
        if it["gps"] is not None or it["myplaces"]:
            return []
        st = st or self.place_store(quiet=True)
        if not st or not st.places:
            return []
        src, when = it["src"], it["when"]
        if not src or when is None:
            return []
        span = datetime.timedelta(minutes=places.PROBABLY_MINUTES)
        others = [(o["when"], o["src"], o["gps"]) for o in self.items  # cheap tests first: one pass, no parsing
                  if o["src"] == src and o["gps"] is not None and not o["gone"] and o is not it
                  and o["when"] is not None and abs(o["when"] - when) <= span]
        got = st.probable(it["path"], when, src, others)
        return [dict(id=pid, name=st.places[pid]["name"], minutes=m) for pid, m in got]

    def place_add(self, item, name, radius):
        """Add a place at a GPS'd item's point (the page never sees the coordinates), then tag every library
        item inside it (places.Store.plan: never one you removed it from)."""
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError("which item?")
        try:
            radius = places.check_radius(radius if radius is not None else places.RADIUS_DEFAULT)
        except ValueError as e:
            raise ValueError(str(e)) from None
        with self.writing("add place"), self.lock:
            i = self.targets([item])[0]
            it = self.items[i]
            if it["gps"] is None:
                raise ValueError("this item has no location (GPS), so it can't mark a place")
            st = self.place_store()
            pid = st.add(name, it["gps"][0], it["gps"][1], radius)
            ids, blocks = self.place_blocks(st, [pid])
            self.run_exiftool(blocks)
            self.refresh(ids)
            st.save()  # after the sidecars: a record never says a tag is there before it is
            p = st.places[pid]
            return dict(result=f"Added the place {p['name']}: {len(blocks)} item{'' if len(blocks) == 1 else 's'} "
                               f"within {p['radius']} m tagged.", place=pid, ids=ids)

    def place_blocks(self, st, only):
        """(item ids, exiftool blocks) bringing the live items in step with the places in only."""
        ids, blocks = [], []
        for i, it in enumerate(self.items):
            if it["gone"] or not os.path.isfile(it["path"]):
                continue
            add, remove = st.plan(it["path"], it["gps"], it["tags"] if it["xmp_read"] else None, only=only)
            if add or remove:
                ids.append(i)
                blocks.append(self.tag_block(it, add=add, remove=remove))
        return ids, blocks

    def place_probable(self, item, pid, yes):
        """Your answer to "probably at <place>": yes tags the item with it (a tag of this tool's, like the ones
        it adds by GPS); no records that it never asks about this item and place again."""
        if not isinstance(item, int) or isinstance(item, bool) or not isinstance(pid, str):
            raise ValueError("which item and place?")
        if not isinstance(yes, bool):
            raise ValueError("yes is true or false")
        with self.writing("place"), self.lock:
            i = self.targets([item])[0]
            it = self.items[i]
            st = self.place_store()
            if pid not in st.places:
                raise ValueError("no such place (it may have been removed)")
            name = st.places[pid]["name"]
            if not any(x["id"] == pid for x in self.probably(i, st)):
                raise ValueError(f"{os.path.basename(it['path'])} is not suggested for {name} any more")
            if yes:
                self.run_exiftool([self.tag_block(it, add=[places.tag_of(name)])])
                self.refresh([i])
                st.tagged[pid].add(it["path"])
            else:
                st.declined[pid].add(it["path"])
            st.save()
            return dict(result=f"Tagged as {name}." if yes else f"Not {name}: it won't be suggested again.", ids=[i])

    # ---------------------------------------------- events (Event|<slug> tags; sidecars only)
    def events_of(self, it):
        """Every event an item is in: its Event| tags, and XMP-iptcExt:Event when it has no Event| tag."""
        ev = [t[6:] for t in it["tags"] if t.startswith("Event|")]
        return ev or ([it["xevent"]] if it.get("xevent") else [])

    def event_names(self):
        return {e for it in self.items if not it["gone"] for e in self.events_of(it)}

    def prune_names(self, emptied=None):
        """(With self.lock held.) Display names of events this server saw lose their last file (Trash, Erase,
        Set event "", Undo), or with emptied=None of every event no listed file is in (at start, only when every
        library folder is there and no file was left out as flagged: an unmounted drive's events aren't gone).
        Never raises: a name that can't be dropped now is dropped the next time."""
        live = self.event_names()
        if emptied is None:
            gone = None
        else:
            gone = {e for e in emptied if e not in live}
            if not gone:
                return
        try:
            names = (read_event_names() or {}).get(library_key(), {})
            prune_event_names(live if gone is None else live | (set(names) - gone))
        except OSError:
            pass

    def event_block(self, it, new, drop):
        """The block that moves one item out of the events in drop (all of its events when drop is None) and
        into new ("" = no event): Event| tags through tag_block, and XMP-iptcExt:Event kept in step. Only
        Event| tags are ever removed, so Owner|, my tags and every other namespace stay as they are."""
        old = [t for t in it["tags"] if t.startswith("Event|") and (drop is None or t[6:] in drop)]
        add = [f"Event|{new}"] if new else []
        remove = [t for t in old if t not in add]
        xev = it.get("xevent") or ""
        extra = []
        if new and (not xev or drop is None or xev in drop):
            if xev != new:
                extra.append(f"-XMP-iptcExt:Event={new}")
        elif not new and xev and (drop is None or xev in drop):
            extra.append("-XMP-iptcExt:Event=")
        changed = bool(remove or extra or (add and add[0] not in it["tags"]))
        if not changed:
            return None  # already in that event: nothing to write
        return self.tag_block(it, add=add, remove=remove, extra=extra)

    @staticmethod
    def check_event_name(name, what="event"):
        name = (name or "").strip() if isinstance(name, str) else None
        if not name or not intake.config.SLUG.match(name):
            raise ValueError(f"the {what} is a slug: lowercase letters, digits and dashes, up to 64")
        return name

    def set_event(self, ids, event):
        """Bulk: every selected item into one event (replacing its others), or out of every event (event "")."""
        if event is None or not isinstance(event, str):
            raise ValueError("event is a slug, or empty to clear")
        event = event.strip()
        if event:
            event = self.check_event_name(event)
        with self.writing("set event"), self.lock:
            ids = self.targets(ids)
            before = self.snapshots(ids)
            had = self.event_names()
            new = event and event not in had
            blocks = [b for b in (self.event_block(self.items[i], event, None) for i in ids) if b]
            self.run_exiftool(blocks)
            self.refresh(ids)
            self.prune_names(had - self.event_names())  # moved or cleared out of an event's last file
            if new and blocks:  # a new event never picks up a display name left from one emptied earlier
                names = load_event_names()
                if names.pop(event, None) is not None:
                    self._save_names_after(names, True)
            return self.journal("event", before, f"event {event!r} set on {len(blocks)}" if event
                                else f"event cleared on {len(blocks)}")

    def rename_event(self, old, new):
        """Rename an event in every sidecar that has it (Event|old -> Event|new). new must not exist yet: joining
        an existing event is a merge."""
        if not isinstance(old, str) or not old.strip():
            raise ValueError("which event?")
        old = old.strip()
        new = self.check_event_name(new, "new name")
        if new == old:
            raise ValueError("that is already its name")
        with self.writing("rename event"), self.lock:
            names = self.event_names()
            if old not in names:
                raise ValueError("no such event")
            if new in names:
                raise ValueError(f"{new!r} already exists: use Merge to join the two")
            out = self._move_events({old}, new, f"renamed {old!r} to {new!r}")
            # the display name goes with the event; a name left over for the new slug (an event that was
            # emptied earlier) never attaches to it
            names = load_event_names()
            kept = names.pop(old, None)
            had = names.pop(new, None)
            if kept:
                names[new] = kept
            return out + self._save_names_after(names, kept or had)

    def merge_events(self, sources, into):
        """Merge events into an existing one: every item of each source event moves to into."""
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list) or not sources or len(sources) > 200 \
                or not all(isinstance(s, str) and s.strip() for s in sources):
            raise ValueError("which events?")
        sources = {s.strip() for s in sources}
        if not isinstance(into, str) or not into.strip():
            raise ValueError("merge into which event?")
        into = into.strip()
        if into in sources:
            raise ValueError("an event can't be merged into itself")
        with self.writing("merge events"), self.lock:
            names = self.event_names()
            if into not in names or not sources <= names:
                raise ValueError("no such event")
            out = self._move_events(sources, into, f"merged {', '.join(sorted(map(repr, sources)))} into {into!r}")
            names = load_event_names()  # into keeps its display name; the merged events' names go with them
            return out + self._save_names_after({e: n for e, n in names.items() if e not in sources},
                                                sources & set(names))

    @staticmethod
    def _save_names_after(names, changed):
        """After a rename or merge (its sidecars already written): the display names saved, or why not (an
        unreadable file is said even when no name was known to move: it may hold one)."""
        if read_event_names() is None:
            return f"; display names not updated: {EVENT_NAMES} in the state folder is unreadable"
        if not changed:
            return ""
        try:
            save_event_names(names)
        except RuntimeError as e:
            return f"; display names not updated: {e}"
        except OSError as e:
            return f"; display names not updated ({type(e).__name__})"
        return ""

    def name_event(self, event, name):
        """Set (or, with "", clear) the display name an event is shown by. Only event-names.json in the state
        folder changes, never a sidecar. Refused when it would look like another event's slug or name."""
        if not isinstance(event, str) or not event.strip():
            raise ValueError("which event?")
        event = event.strip()
        name = check_display_name(name)
        with self.writing("name event"), self.lock:
            events = self.event_names()
            if event not in events:
                raise ValueError("no such event")
            names = load_event_names()
            if name:
                for o in sorted(events - {event}):
                    if look_key(name) in (look_key(o), look_key(names.get(o) or "")):
                        shown = names.get(o) or o
                        raise ValueError(f"another event already looks like that ({shown!r}, event {o!r}); "
                                         "pick a name that differs by more than case or spacing")
                names[event] = name
            elif names.pop(event, None) is None:
                return f"{event!r} already shows its default name"
            save_event_names(names)
            return f"{event!r} shown as {name!r}" if name else f"{event!r} back to its default name"

    def _move_events(self, drop, new, what):
        """(Under the library lock and self.lock.) Every live item in one of the drop events, moved to new."""
        for name in drop:
            if not TAG_RE.match(name) or name.startswith("-") or "@@sep@@" in name:
                raise ValueError("that event's name can't be rewritten here")
        ids = [i for i, it in enumerate(self.items) if not it["gone"] and drop & set(self.events_of(it))]
        blocks = [b for b in (self.event_block(self.items[i], new, drop) for i in ids) if b]
        self.run_exiftool(blocks)
        self.refresh(ids)
        return f"{what}: {len(blocks)} sidecars"

    def rotate(self, ids, way):
        if way not in ("cw", "ccw"):
            raise ValueError("cw or ccw")
        with self.writing("rotate"), self.lock:
            ids = self.targets(ids)
            before = self.snapshots(ids)
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
            return self.journal("rotate", before,
                                f"rotated {len(ids)} (sidecar XMP-tiff:Orientation only; originals untouched)")

    # ---------------------------------------------- undo (Keep, Favorite, my tags, Rotate, Set event; never Trash or Erase)
    def snapshots(self, ids):
        """(Under the library lock.) Each item's sidecar as it is before an undoable action writes it."""
        return {i: sidecar_state(intake.sidecar(self.items[i]["path"])) for i in ids}

    def journal(self, kind, before, msg):
        """(Under the library lock and self.lock, right after the write.) Record what the action changed: per
        item, the sidecar's fields before and after and the after-bytes' sha256. The reply carries a token for
        /api/undo when anything changed. Only the server holds the record, so an undo can never write anything
        the page sends: it reverses exactly this action's own change."""
        rec = {}
        for i, (_, old) in before.items():
            sha, new = sidecar_state(intake.sidecar(self.items[i]["path"]))
            if new != old:
                rec[i] = (old, new, sha)
        if not rec:
            return msg
        token = secrets.token_urlsafe(12)
        self.undos[token] = dict(kind=kind, items=rec)
        while len(self.undos) > UNDO_KEEP:
            self.undos.popitem(last=False)
        return dict(result=msg, undo=token)

    @staticmethod
    def undo_block(kind, old, new, path):
        """The exiftool block that puts one sidecar's fields back from new to old: tags and dc:Subject leaves
        added or removed, orientation and XMP-iptcExt:Event set back (or deleted when there was none). Refuses
        (ValueError) anything outside what that kind of action may write (UNDO_SCOPE)."""
        scope = UNDO_SCOPE[kind]
        drop, add = new["tags"] - old["tags"], old["tags"] - new["tags"]
        rating = old.get("rating") != new.get("rating")
        if not all(scope["tags"](t) for t in drop | add) or (old["orient"] != new["orient"] and not scope["orient"]) \
                or (old["xevent"] != new["xevent"] and not scope["xevent"]) or (rating and not scope.get("rating")):
            raise ValueError(f"that undo would write outside what {kind} writes; nothing was changed")
        back = set(drop | add | (new["subject"] ^ old["subject"]))
        if old["xevent"] != new["xevent"]:
            back.add(old["xevent"])
        for v in back - {""}:
            if "@@sep@@" in v or v.startswith("-") or not TAG_RE.match(v):
                raise ValueError("a value in that sidecar can't be written back here; nothing was changed")
        if rating and old.get("rating") and not RATING_RE.match(old["rating"]):
            raise ValueError("a value in that sidecar can't be written back here; nothing was changed")
        b = ["-q", "-m", "-overwrite_original", "-n", "-sep", "@@sep@@"]
        b += [f"-XMP-lr:HierarchicalSubject-={t}" for t in sorted(drop)]
        b += [f"-XMP-lr:HierarchicalSubject+={t}" for t in sorted(add)]
        b += [f"-XMP-dc:Subject-={t}" for t in sorted(new["subject"] - old["subject"])]
        b += [f"-XMP-dc:Subject+={t}" for t in sorted(old["subject"] - new["subject"])]
        if old["orient"] != new["orient"]:
            b.append(f"-XMP-tiff:Orientation={old['orient'] or ''}")
        if old["xevent"] != new["xevent"]:
            b.append(f"-XMP-iptcExt:Event={old['xevent']}")
        if rating:
            b.append(f"-XMP-xmp:Rating={old.get('rating') or ''}")
        return b + [path]

    def undo(self, token):
        """Put back what one undoable action changed, item by item, under the library lock. Refused, writing
        nothing, when the token is unknown (already undone, or older than the last UNDO_KEEP actions), an item
        is gone, or any of its sidecars changed since (another action, the CLI, digiKam): an undo never
        clobbers a later change. One use per token."""
        if not isinstance(token, str) or not token:
            raise ValueError("which action?")
        with self.writing("undo"), self.lock:
            rec = self.undos.get(token)
            if rec is None:
                raise ValueError("nothing to undo: it was undone already, or is too old")
            if rec["kind"] == "date":  # a Fix date: fix-dates' own undo (all its sidecars, or none)
                del self.undos[token]
                try:
                    datecheck.write_undo(rec["fix"])
                except datecheck.Changed as e:
                    raise Changed(str(e)) from None
                ids = [i for i in rec["ids"] if self.live(i)]
                self.refresh(ids)
                self.date_findings()
                return dict(result=f"undid Fix date on {len(rec['ids'])}", ids=ids, kind="date", thumbs=False)
            ids = list(rec["items"])
            for i in ids:
                if not self.live(i) or not os.path.isfile(self.items[i]["path"]):
                    del self.undos[token]
                    raise Changed("can't undo: an item is no longer in the library; nothing was changed")
            if any(sidecar_state(intake.sidecar(self.items[i]["path"]))[0] != rec["items"][i][2] for i in ids):
                del self.undos[token]
                raise Changed("can't undo: those items changed again since; nothing was changed")
            kind = rec["kind"]
            blocks = [self.undo_block(kind, old, new, intake.sidecar(self.items[i]["path"]))
                      for i, (old, new, _) in rec["items"].items()]
            del self.undos[token]
            had = self.event_names()
            self.run_exiftool(blocks)
            self.refresh(ids)
            self.prune_names(had - self.event_names())  # an undone Set event can empty the event it made
            if kind == "rotate":
                for i in ids:
                    self.drop_thumb(self.items[i])
            what = {"keep": "Keep", "tag": "the tag", "rotate": "Rotate", "event": "Set event",
                    "favorite": "Favorite"}[kind]
            return dict(result=f"undid {what} on {len(ids)}", ids=ids, kind=kind, thumbs=kind == "rotate")

    # ---------------------------------------------- trash / erase (a detached jobs.py job, one at a time)
    def start_job(self, label_, argsets, ids, hold=True):
        """Paths reach the worker on a pipe (private); the status file gets only (device, inode) pairs and a
        count. An erase is a redact job: its command lines carry no path and its output is scrubbed at the end.
        hold=False (a share-safe copy, which deletes nothing): the files stay viewable while it runs."""
        redact = label_ == "erase"
        shown = [" ".join(a if a.startswith("--") else "'" + os.path.basename(a) + "'" if os.path.isabs(a) else a
                          for a in args) for args in argsets]
        with self.lock:
            paths = [self.items[i]["path"] for i in ids]
            files = [list(self.items[i]["fid"]) for i in ids if self.items[i].get("fid")] if hold else []
            try:
                claim = jobs.claim(JOB_KEY)  # atomic across pages and servers; the worker inherits it
            except jobs.Busy:  # this page's last job, another page's, or the library lock (says who, plainly)
                raise LockBusy(busy_text()) from None
            with claim:
                st = claim.spawn("commands", dict(label=label_, files=files, count=len(ids)), total=len(argsets),
                                 private=dict(argsets=argsets, paths=paths, **({} if redact else {"shown": shown})),
                                 redact=redact)
            self._follow(st["id"], claim.tail)
            self.mark_busy(st)
            return st["id"]

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
                had = self.event_names()
                sizes = set()
                for i in self.job_items(st):
                    it = self.items[i]
                    it.pop("busy", None)
                    if not os.path.lexists(it["path"]):
                        self.drop_gone(it)
                        sizes.add(it["size"])
                self.reread_copies(sizes)
                self.search_cache.clear()
                self.prune_names(had - self.event_names())  # a Trash or Erase took an event's last file
        return st

    def drop_gone(self, it):
        it["gone"] = True
        if it.get("sha"):
            intake.gallery_thumb_forget(it["sha"])  # its thumbnail and video proxy

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
        out = dict(jobs.public(st), lines=ls, next=nxt, ids=self.job_items(st),
                   kind=(st.get("spec") or {}).get("label"))  # trash, erase or share: what started it
        with self.lock:
            d = self.shares.get(st["id"])
        if d:
            out["dest"] = self.shown(d)  # a Share this server started: its folder, for a page that re-attached
        return out

    def trash(self, ids, confirm):
        if confirm is not True:
            raise ValueError("confirm the trash")
        with self.lock:
            ids = self.targets(ids)
            paths = [self.items[i]["path"] for i in ids]
            bad = [p for p in paths if not intake.trash_allowed(p) or under_state(p)]
            if bad:
                raise ValueError(f"{len(bad)} items are not library files")
        self.start_job("trash", [["trash", *paths, "--yes"]], ids)
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

    def share_dir(self):
        """A new, empty folder for one share-safe export: <share_out>/<date>-<time>[-N], 0700. Never inside a
        library root or the state folder (the copies would be taken for library files)."""
        base, real = self.share_out, os.path.realpath(self.share_out)
        for r in ROOTS + [STATE]:
            rr = os.path.realpath(r)
            if real == rr or real.startswith(rr + os.sep):
                raise ValueError(f"the share-safe folder {self.share_out} is inside the library or the state "
                                 "folder; start the gallery with --share-out DIR")
        os.makedirs(base, mode=0o700, exist_ok=True)
        stamp = f"{datetime.datetime.now():%Y-%m-%d-%H%M%S}"
        for n in range(1, 100):
            d = os.path.join(base, stamp if n == 1 else f"{stamp}-{n}")
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                continue
            return d
        raise RuntimeError("could not make a new share-safe folder")

    def share(self, ids):
        """Share-safe copies of the selection: intake.py share-copy (all metadata and GPS stripped) as a job, into
        a new folder. Returns the folder, never the copies' contents. Originals and sidecars are only read."""
        with self.lock:
            ids = self.targets(ids)
            paths = []
            for i in ids:
                p = self.library_file(i)
                if not p:
                    raise ValueError("not a library file")
                paths.append(p)
            names = [os.path.basename(p) for p in paths]
            if len(set(names)) != len(names):
                raise ValueError("two of these have the same file name; share them one at a time")
        out = self.share_dir()
        try:
            jid = self.start_job("share", [["share-copy", *paths, "--out", out]], ids, hold=False)
        except Exception:
            with contextlib.suppress(OSError):  # nothing was started: drop the empty folder (kept if not empty)
                os.rmdir(out)
            raise
        with self.lock:
            self.shares[jid] = out
            while len(self.shares) > SHARE_KEEP:
                self.shares.popitem(last=False)
        shown = self.shown(out)
        return dict(result=f"making {len(ids)} share-safe copies in {shown}", dest=shown, job=jid)

    @staticmethod
    def shown(path):
        """A path as the page shows it: ~/... under HOME."""
        return "~/" + os.path.relpath(path, HOME) if path.startswith(HOME + os.sep) else path

    def open_share(self, jid):
        """Open the folder one finished Share made in the desktop's file manager (xdg-open), so the copies can be
        dragged into a chat or an email. The page names the job, never a path: the folder is the one this server
        made for that job (share_dir), and it must still be a real folder directly inside --share-out after
        resolving links (not a symlink swapped in since, nor anything else), and the job must have ended."""
        with self.lock:
            d = self.shares.get(jid) if isinstance(jid, str) else None
        if not d:
            raise ValueError("no Share folder for that job here (only one this gallery started can be opened)")
        st = jobs.status(JOB_KEY)
        if st and st.get("id") == jid and not st["done"]:
            raise ValueError("the copies are still being made")
        base = os.path.realpath(self.share_out)
        if os.path.islink(d) or not os.path.isdir(d) or os.path.dirname(os.path.realpath(d)) != base:
            raise ValueError("that Share folder is gone or was moved")
        self.launch(d, "the folder")
        return dict(result=f"opened {self.shown(d)}", dest=self.shown(d))

    @staticmethod
    def launch(path, what):
        """xdg-open PATH: an argument list (no shell), detached, its output dropped, reaped in the background."""
        if not shutil.which(OPENER):
            raise RuntimeError(f"can't open {what}: {OPENER} is not installed (Fedora package xdg-utils; "
                               "media-intake doctor checks it)")
        try:
            proc = subprocess.Popen([OPENER, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except OSError as e:
            raise RuntimeError(f"{OPENER} failed: {e.strerror or type(e).__name__}") from None
        threading.Thread(target=proc.wait, daemon=True).start()  # reap it; xdg-open hands off and exits

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
        self.launch(p, "it")
        return f"opened {os.path.basename(p)} in the system player"

    def decide(self, keep, trash, confirm):
        """Suggested junk's Apply: Keep (sidecar, now) then Trash (job). A file marked Trash that is a favorite or
        kept by the time Apply runs (starred or kept since the mark, on this page, another one or in digiKam:
        its sidecar is re-read here) is skipped, never trashed, and the reply says so ("trashed": the ids the
        job got). Plain Trash (/api/trash) isn't held back: it is asked for item by item and confirmed."""
        if trash and confirm is not True:
            raise ValueError("confirm the trash")
        msg, sent = [], []
        if keep:
            r = self.keep(keep)
            msg.append(r["result"] if isinstance(r, dict) else r)  # Apply offers no Undo (it may trash too)
        if trash:
            with self.lock:
                ids = self.targets(trash)
                self.refresh(ids)
                skipped = [i for i in ids if self.items[i]["fav"] or self.items[i]["kept"]]
                sent = [i for i in ids if i not in skipped]
                if sent:
                    self.trash(sent, confirm)  # under the same lock: no star lands between the check and the job
                    msg.append(f"Trashing {len(sent)}.")
                if skipped:
                    msg.append(f"Skipped {len(skipped)}: favorite/kept.")
        if not msg:
            raise ValueError("nothing decided")
        return dict(result=" ".join(msg), trashed=sent)

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
    words = json.dumps(WORDS).replace("</", "<\\/")
    return (PAGE.replace("__CSRF__", html.escape(app.csrf)).replace("__VIEWS__", tabs).replace("__WORDS__", words)
            .replace("</body>", scripts + "</body>"))


# Sidecar edits on a selection whose reply carries the items' new grid summaries ("items").
REDRAW = ("/api/keep", "/api/favorite", "/api/tag", "/api/rotate", "/api/event/set", "/api/dates/fix")


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
                self.reply(404, "404\n")
                return
            try:  # before the lock: it may probe the video once (ffprobe)
                conv = app.proxy_state(app.items[i])
            except OSError:
                conv = None
            with app.lock:
                self.json(200, dict(app.detail(i), conv=conv))
        elif m := re.fullmatch(r"/([tmp])/([0-9]{1,9})", path):
            i = int(m.group(2))
            if not app.servable(i):
                self.reply(404, "404\n")
                return
            it = app.items[i]
            if m.group(1) == "t":
                try:
                    t = app.thumb(it)
                except OSError:
                    t = None
                if not t:
                    self.reply(404, "404\n")
                    return
                self.media(dict(path=t, mime="image/jpeg"))
            elif m.group(1) == "m":
                if it["show"] not in ("image", "video"):
                    self.reply(404, "404\n")
                    return
                try:
                    src, mime = app.playable(it)
                except OSError:
                    src, mime = it["path"], it["mime"]
                if isinstance(src, ProxyJob):
                    self.stream_proxy(src, it)
                else:
                    self.media(dict(path=src, mime=mime))
            else:
                data = image_jpeg(it["path"], PREVIEW_PX, 0) if it["media"] == "photo" else None
                if not data:
                    self.reply(404, "404\n")
                    return
                self.reply(200, data, "image/jpeg")
        elif m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/media/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            got = v["media"](m.group(2)) if v and callable(v.get("media")) else None
            if not got or not os.path.isfile(got["path"]) or os.path.islink(got["path"]) \
                    or under_state(got["path"]) or label.refused([got["path"]], STATE):
                self.reply(404, "404\n")
                return
            self.media(dict(path=got["path"], mime=got["mime"]))
        elif m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            got = v["get"](m.group(2), f) if v and callable(v.get("get")) else None
            if got is None:
                self.reply(404, "404\n")
                return
            self.json(*got)
        else:
            self.reply(404, "404\n")

    def hung_up(self):
        """True once the client closed its end (a readable socket with nothing to read), so a reply waiting on a
        slow transcode lets go of it (App.playable then may cancel it for another video)."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.rfile.peek(1) == b""  # readable: peek won't block
        except OSError:
            return True

    def stream_proxy(self, job, it):
        """/m/ while job (holding a reader's place, App.playable) makes this video's proxy: the fragmented MP4 as
        it is written, from its first byte, in a 200 with no length and no Range (the size isn't known yet; HTTP
        1.0, so the reply ends when the connection closes), so the player starts within seconds instead of after
        the whole transcode. It ends with the transcode, when that attempt fails, when the player hangs up, or
        when the video goes into a Trash or Erase job. A job that ended before its first byte is answered as
        /m/ answers after it: the finished proxy, or the original."""
        try:
            got = job.open_stream()
            if got is None:
                job.wait_done()
                ok = job.ok and os.path.isfile(job.dst) and not (it.get("busy") or it["gone"])
                self.media(dict(path=job.dst, mime="video/mp4") if ok else dict(path=it["path"], mime=it["mime"]))
                return
            f, attempt = got
            with f:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "none")
                self.send_header("Content-Disposition", "inline")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    while not (it.get("busy") or it["gone"]):
                        b = f.read(R.CHUNK)
                        if b:
                            self.wfile.write(b)
                        elif not job.more(attempt):
                            while b := f.read(R.CHUNK):  # the rest of what that attempt wrote
                                self.wfile.write(b)
                            break
                        elif self.hung_up():  # nothing new to write, so no failed write would tell
                            break
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the player closed it (the large view moved on, or it re-asked)
        finally:
            job.leave()

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
        acts = {
            "/api/keep": lambda: app.keep(body.get("ids")),
            "/api/favorite": lambda: app.favorite(body.get("ids"), body.get("on")),
            "/api/tag": lambda: app.tag(body.get("ids"), body.get("tag"), body.get("op")),
            "/api/rotate": lambda: app.rotate(body.get("ids"), body.get("dir")),
            "/api/trash": lambda: app.trash(body.get("ids"), body.get("confirm")),
            "/api/erase": lambda: app.erase(body.get("ids"), body.get("confirm")),
            "/api/open": lambda: app.open_external(body.get("ids")),
            "/api/share": lambda: app.share(body.get("ids")),
            "/api/share/open": lambda: app.open_share(body.get("job")),
            "/api/event/set": lambda: app.set_event(body.get("ids"), body.get("event")),
            "/api/event/rename": lambda: app.rename_event(body.get("from"), body.get("to")),
            "/api/event/merge": lambda: app.merge_events(body.get("from"), body.get("into")),
            "/api/event/name": lambda: app.name_event(body.get("event"), body.get("name")),
            "/api/place/add": lambda: app.place_add(body.get("id"), body.get("name"), body.get("radius")),
            "/api/place/probable": lambda: app.place_probable(body.get("id"), body.get("place"), body.get("yes")),
            "/api/decide": lambda: app.decide(body.get("keep") or [], body.get("trash") or [], body.get("confirm")),
            "/api/undo": lambda: app.undo(body.get("undo")),
            "/api/dates/fix": lambda: app.fix_dates(body.get("ids"), body.get("mode"), body.get("value")),
        }
        if path == "/api/close":
            self.json(200, {"result": "closing"})
            app.stop.set()
            return
        if m := re.fullmatch(r"/plugin/([a-z0-9-]{1,32})/([^?#]{1,512})", path):
            v = app.views.get(m.group(1))
            if not v or not callable(v.get("post")):
                self.reply(404, "404\n")
                return
            # A plugin POST may write sidecars or library files, so it runs under the library lock like Keep,
            # tags and Rotate (409 naming the holder, at once, while a job or the CLI writes). Only a view that
            # declares post_read_only=True is called without it (docs/plugins.md).
            try:
                with contextlib.nullcontext() if v.get("post_read_only") is True \
                        else app.writing(f"plugin {m.group(1)}"):
                    got = v["post"](m.group(2), body)
            except LockBusy as e:
                self.json(409, {"error": str(e), "busy": True})
                return
            if got is not None:
                self.json(*got)
            else:
                self.reply(404, "404\n")
            return
        if path not in acts:
            self.reply(404, "404\n")
            return
        try:
            msg = acts[path]()
        except ValueError as e:
            self.json(400, {"error": str(e)})
            return
        except LockBusy as e:
            self.json(409, {"error": str(e), "busy": True})
            return
        except RuntimeError as e:
            self.json(409, {"error": str(e)})
            return
        out = msg if isinstance(msg, dict) else {"result": msg}
        if path in REDRAW and isinstance(body.get("ids"), list):
            out["items"] = app.summaries(body["ids"])  # the page redraws those tiles' flags (kept, ...) in place
        elif path in ("/api/undo", "/api/place/add", "/api/place/probable"):
            out["items"] = app.summaries(out["ids"])
        self.json(200, out)


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
    def port(v):
        if not v.isdigit() or int(v) > 65535:
            raise argparse.ArgumentTypeError(f"{v}: a port is 0-65535 (0 picks a free one)")
        return int(v)

    ap = argparse.ArgumentParser(
        prog="media-intake gallery", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Browse the library in a local web page (127.0.0.1 only: open the link it\n"
                    "prints). Keep, Trash, Erase, tag, rotate, set events and make share-safe copies,\n"
                    "on one photo or a selection. Quarantined and flagged files are never shown.\n\n"
                    "Touches: reads the library; Keep, tags, Rotate and events write sidecars (never\n"
                    "  the photos); Share writes share-safe copies to --share-out.\n"
                    "Deletes: only what you Trash or Erase on the page, after you confirm.")
    ap.add_argument("--drive", metavar="DRIVE", help="the backup drive: the folder it is mounted on, e.g. "
                    "/run/media/$USER/<label>. Erase in the gallery then deletes the copies there too")
    ap.add_argument("--port", type=port, default=0, help="the page's port (default 0: a free one)")
    ap.add_argument("--share-out", metavar="DIR", help=f"folder for share-safe copies, one dated subfolder per "
                                                       f"export (default {SHARE_OUT}); never inside the library")
    a = ap.parse_args(argv)
    # The system python has no pillow-heif: HEIC would show only as its thumbnail. Re-run under setup.sh's venv.
    R.venv_reexec(sys.argv[1:] if argv is None else list(argv), __file__)
    if a.drive and not os.path.isdir(a.drive):
        sys.exit(f"--drive {a.drive}: not a directory")
    t0 = time.monotonic()
    jobs.sweep()  # a trash/erase that died while no page watched: marked crashed, an erase's output scrubbed
    items, skipped = scan_library()
    views = view_list([v for vs in plugins.call("gallery_views") for v in vs])
    app = App(items, os.path.abspath(a.drive) if a.drive else None, views, a.share_out)
    if not skipped and all(os.path.isdir(r) for r in ROOTS):  # names of events emptied while no gallery ran
        with app.lock:
            app.prune_names()
    running = app.sync()  # a trash/erase started before a restart: its files stay busy until it ends
    threading.Thread(target=app.check_text, daemon=True).start()
    try:
        httpd, url = serve(app, a.port)
    except OSError as e:  # the port is taken, or not ours to use
        sys.exit(f"port {a.port} can't be used ({e.strerror or e}): pick another with --port, or leave it out")
    print(f"xdg-open '{url}'", flush=True)
    reg = f" Plugin tabs: {', '.join(v['title'] for v in views)}." if views else ""
    print(f"({len(items)} items in {time.monotonic() - t0:.1f} s, {skipped} flagged/erased left out.{reg} "
          "A private browser window keeps it out of history. "
          "Ctrl-C or the page's Stop server button stops it.)", flush=True)
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
        app.close_proxies()
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
 --on-acc:#fff;--sbw:300px;--ml:300px;--hh:56px;--tile:168px}
@media (prefers-color-scheme: dark){:root{--bg:#0f1013;--panel:#17191d;--fg:#e8eaed;--mut:#9aa1ad;--line:#2a2d34;--card:#22252b;--hover:#1f2228;--acc:#8ab0ff;--acc-soft:rgba(138,176,255,.14);
 --keep:#4cc26a;--trash:#e0a030;--erase:#ff6b5e;--shadow:0 6px 24px rgba(0,0,0,.5);--on-acc:#0b1020}}
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
.btn.primary{background:var(--acc);border-color:var(--acc);color:var(--on-acc)}
::placeholder{color:var(--mut);opacity:1}
kbd{font:600 11px/1 ui-monospace,monospace;border:1px solid currentColor;border-radius:4px;padding:2px 4px}
.icon{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:0;border-radius:8px;background:none;color:var(--mut);position:relative}
.icon:hover{background:var(--hover);color:var(--fg)}
.link{border:0;background:none;color:var(--acc);padding:2px 0;font-size:13px}
.link:hover{text-decoration:underline}
.note{color:var(--mut);font-size:14px;margin:6px 0}
.warn{color:var(--erase)}
.empty{color:var(--mut);font-size:13px;padding:4px 8px}
.empty.big{font-size:15px;padding:48px 0;text-align:center}
/* the grid's own states (listState): loading, an empty library, no results, a load error; each says what to do */
.state{max-width:560px;margin:40px auto;padding:0 8px;text-align:center;color:var(--mut);font-size:15px}
.state h2{margin:0 0 8px;font-size:17px;color:var(--fg)}
.state p{margin:6px 0}
.state pre{text-align:left;font-size:13px;background:var(--card);color:var(--fg);padding:10px 12px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}
.state .btn{margin-top:10px}
.state.err h2{color:var(--erase)}
.spin{display:inline-block;width:18px;height:18px;margin-right:8px;vertical-align:-3px;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}

/* ---------------------------------------------------------------- sidebar */
#sidebar{position:fixed;top:0;left:0;bottom:0;width:var(--sbw);background:var(--panel);border-right:1px solid var(--line);overflow-y:auto;overflow-x:hidden;z-index:15;display:flex;flex-direction:column;overscroll-behavior:contain}
.sb-top{display:flex;align-items:center;gap:8px;padding:10px 12px 6px;height:var(--hh)}
.brand{font-weight:650;font-size:15px;white-space:nowrap}
#views{padding:4px 10px 8px;display:flex;flex-direction:column;gap:2px}
#ptabs{display:contents}
/* a section's heading: a rule above it, so Quick views and Events read as groups of their own */
.nvhead{font-size:12px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);padding:14px 10px 6px;margin-top:6px;border-top:1px solid var(--line)}
.nv{display:flex;align-items:center;gap:10px;height:38px;padding:0 10px;border:0;border-radius:8px;background:none;text-align:left;width:100%;color:var(--fg)}
.nv:hover{background:var(--hover)}
.nv[aria-current=page]{background:var(--acc-soft);color:var(--acc);font-weight:600}
.nv .lbl{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ct{color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums;font-weight:400}
.nv[aria-current=page] .ct{color:var(--acc)}
.railonly{display:none}
.rl{display:none}
#evlist,#pllist{display:flex;flex-direction:column;gap:2px}
#plsec .empty{padding:4px 10px}
.probably{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:10px 0}
.probably p{margin:0 0 6px}
.probably .btn{margin:0 6px 0 0}
.plrad select{height:34px;max-width:100%;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:0 8px}
.evmore{align-self:flex-start;margin:2px 10px}
.yrmore{margin:4px 10px}
#evbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:12px 24px 0}
#junkbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;padding:12px 24px 0}
#junkbar label{font-size:14px;color:var(--mut)}
#junkbar select{height:34px;max-width:100%;min-width:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:0 8px}
#junkbar .jf{display:inline-flex;align-items:center;gap:6px;min-width:0;max-width:100%}
#evbar .evt{font-weight:600;margin-right:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
#evbar .evslug{color:var(--mut);font-family:ui-monospace,monospace;font-size:13px;margin-right:4px;min-width:0;overflow-wrap:anywhere}
#mfieldp label{display:block;font-size:13px;color:var(--mut);margin-bottom:4px}
#mfield{width:100%}
/* search and filters: apart from the views above by a heavier rule */
.sb-search{padding:12px 16px 4px;border-top:4px solid var(--line)}
.field{position:relative}
.field .ic{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--mut);pointer-events:none}
#q{width:100%;height:36px;padding:0 10px 0 34px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
#q:focus-visible{outline-offset:0}
.hint{font-size:12px;color:var(--mut);margin:6px 2px 0}
.hint summary{cursor:pointer;list-style:none}
.hint summary::-webkit-details-marker{display:none}
.hint summary::after{content:" (why)";color:var(--acc)}
.hint .why{margin-top:4px;word-break:break-word;font-family:ui-monospace,monospace;font-size:11px}
#filters{padding:6px 0 16px}
/* the foot: page help, not a view; pushed to the bottom when the sidebar is short */
.sb-foot{margin-top:auto;padding:8px 10px;border-top:1px solid var(--line)}
/* the drag handle on the sidebar's right edge (desktop, expanded only) */
#sbresize{position:fixed;top:0;bottom:0;left:calc(var(--sbw) - 4px);width:8px;z-index:16;cursor:col-resize;touch-action:none;background:none}
#sbresize::after{content:"";position:absolute;top:0;bottom:0;left:3px;width:2px;background:transparent;transition:background .1s}
#sbresize:hover::after,#sbresize:focus-visible::after,.sbdrag #sbresize::after{background:var(--acc)}
#sbresize:focus-visible{outline:none}
#sbresize:focus-visible::after{left:2px;width:4px}
/* high contrast: the line in the system's highlight colour (a forced background would hide it) */
@media (forced-colors:active){#sbresize::after{forced-color-adjust:none}#sbresize:hover::after,#sbresize:focus-visible::after,.sbdrag #sbresize::after{background:Highlight}}
.sbdrag,.sbdrag *{cursor:col-resize!important;user-select:none}
.fhead{display:flex;align-items:baseline;justify-content:space-between;padding:10px 16px 4px}
.fhead b{font-size:12px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
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
.opt{display:flex;align-items:center;gap:8px;width:100%;min-height:32px;padding:4px 8px;border:0;border-radius:6px;background:none;text-align:left;font-size:14px;color:var(--fg)}
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
.collapsed .sb-search,.collapsed #filters,.collapsed #evsec,.collapsed #sbresize,.overlay #sbresize{display:none}
.collapsed .sb-foot{padding:4px 8px}
.collapsed #views{padding:4px 8px}
.collapsed .nv{justify-content:center;padding:0;height:40px}
/* the rail names each icon in a word under it (the full name stays the button's title and accessible name) */
.collapsed .nv:has(.rl){flex-direction:column;gap:1px;height:50px}
.collapsed .rl{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;line-height:1.1;color:var(--mut)}
.collapsed .nv[aria-current=page] .rl{color:var(--acc)}
.collapsed .railonly{display:flex}
.badge-n{position:absolute;top:3px;right:3px;min-width:16px;height:16px;border-radius:8px;background:var(--acc);color:var(--on-acc);font-size:10px;line-height:16px;text-align:center;padding:0 4px}
.overlay{--ml:60px;--sbw:min(304px,86vw)}
.overlay #sidebar{box-shadow:var(--shadow)}

/* ---------------------------------------------------------------- main */
/* a container: the header compacts by the main column's own width, which the sidebar's width changes, not only the window's */
#app{margin-left:var(--ml);min-height:100vh;min-width:0;display:flex;flex-direction:column;container:app/inline-size}
#top{position:sticky;top:0;z-index:10;height:var(--hh);display:flex;align-items:center;gap:12px;padding:0 24px;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
#vtitle{font-size:17px;font-weight:650;margin:0;white-space:nowrap;min-width:0;overflow:hidden;text-overflow:ellipsis}
#count{color:var(--mut);font-size:14px;white-space:nowrap;font-variant-numeric:tabular-nums;flex:none}
#top>.btn,#top>.size{flex:none}
.grow{flex:1}
.size{display:flex;align-items:center;gap:8px;color:var(--mut)}
#tsize{width:120px;accent-color:var(--acc)}
#chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:12px 24px 0}
.chip{display:inline-flex;align-items:center;gap:2px;height:28px;padding:0 4px 0 10px;border-radius:999px;background:var(--acc-soft);color:var(--acc);font-size:13px;max-width:100%}
.chip>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip .x{border:0;background:none;color:inherit;width:22px;height:22px;border-radius:50%;font-size:15px;line-height:1}
.chip .x:hover{background:var(--acc-soft)}
#note,#helpb{padding:0 24px}
#helpb .hshort,#hmore{display:none}
#note{margin:10px 0 0}
main{padding:4px 24px 48px;flex:1 0 auto}
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
/* the tile's checkbox: a 22 px box in a larger hit area (.ckw: 34 px, 44 px on a touch screen or a phone) */
.tile .ckw{position:absolute;top:0;left:0;z-index:2;width:34px;height:34px;display:flex;align-items:center;justify-content:center;cursor:pointer}
.tile .ck{width:22px;height:22px;margin:0;accent-color:var(--acc);cursor:pointer}
@media (hover:hover){.tile .ckw{opacity:0;transition:opacity .1s}.tile:hover .ckw,.tile:focus-within .ckw,.selecting .tile .ckw{opacity:1}}
/* Suggested junk: Keep and Trash on every tile (aria-pressed), the mark in words too, never by colour alone */
.tile .dec{position:absolute;left:0;right:0;bottom:0;z-index:2;display:flex;gap:6px;padding:6px;background:linear-gradient(transparent,rgba(0,0,0,.45))}
.tile .dec .btn{flex:1;justify-content:center;height:40px}
.tile .dec .btn[aria-pressed=true]{font-weight:700;box-shadow:inset 0 0 0 2px currentColor}
.tile .dec .btn.keep[aria-pressed=true]{background:var(--keep);border-color:var(--keep);color:var(--on-acc)}
.tile .dec .btn.trash[aria-pressed=true]{background:var(--trash);border-color:var(--trash);color:var(--on-acc)}
[data-view=sug] .tile .flag{bottom:58px}
.tile .mark{position:absolute;left:6px;top:6px;z-index:2;font-size:12px;font-weight:700;letter-spacing:.04em;border-radius:4px;padding:2px 8px;color:var(--on-acc)}
.tile .mark.keep{background:var(--keep)}.tile .mark.trash{background:var(--trash)}
/* Favorites: the tile's star (bottom right, left of the tile's Share; shown on hover / focus, always when starred or with no hover), and
   the large view's Favorite button (a toggle, aria-pressed) */
.tile .star{position:absolute;right:42px;bottom:4px;z-index:2;width:34px;height:34px;padding:0;border:0;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:19px;line-height:1;display:flex;align-items:center;justify-content:center;cursor:pointer}
.tile .star[aria-pressed=true]{color:#ffc83d}
@media (hover:hover){.tile .star{opacity:0;transition:opacity .1s}.tile:hover .star,.tile:focus-within .star,.tile .star[aria-pressed=true]{opacity:1}}
@media (forced-colors:active){.tile .star{border:1px solid ButtonText}.tile .star[aria-pressed=true]{color:Highlight}}
.acts .favb{grid-column:1/-1}
#info .btn.favb[aria-pressed=true]{color:#ffc83d;border-color:#7a6220}
/* drag to select (or, from a Keep / Trash mark in Suggested junk, to mark): the rectangle, in the mark's colour */
#marq{position:fixed;z-index:11;pointer-events:none;border:2px solid var(--acc);background:var(--acc-soft);border-radius:2px;box-shadow:0 0 0 1px var(--panel)}
#marq.keep{border-color:var(--keep);background:color-mix(in srgb,var(--keep) 14%,transparent)}
#marq.trash{border-color:var(--trash);background:color-mix(in srgb,var(--trash) 14%,transparent)}
body.marqing,body.marqing *{user-select:none;-webkit-user-select:none}
@media (forced-colors:active){#marq,#marq.keep,#marq.trash{forced-color-adjust:none;border:2px dashed Highlight;background:transparent}}
.play{position:absolute;right:6px;top:6px;width:24px;height:24px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;padding-left:2px}
/* Burst stacks: the review page's look (a stacked card, a count badge, an X button) on a tile */
.tile.stack{box-shadow:4px 4px 0 -1px var(--bg),4px 4px 0 0 var(--mut),8px 8px 0 -1px var(--bg),8px 8px 0 0 var(--mut)}
.tile.stack.cur{box-shadow:inset 0 0 0 3px var(--fg),4px 4px 0 -1px var(--bg),4px 4px 0 0 var(--mut),8px 8px 0 -1px var(--bg),8px 8px 0 0 var(--mut)}
.tile.inb{box-shadow:0 0 0 2px var(--acc)}  /* the shots of an open stack: one ring colour, one group */
.tile .badge{position:absolute;right:6px;top:6px;z-index:2;background:var(--acc);color:var(--on-acc);font-size:11px;font-weight:700;border-radius:999px;padding:1px 8px;white-space:nowrap}
.tile .x{position:absolute;right:6px;top:28px;z-index:2;font-size:11px;padding:2px 8px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--fg);cursor:pointer;white-space:nowrap}
/* on a small tile they shorten (ellipsis) rather than run over the checkbox or out of the tile */
.tile .badge,.tile .x{max-width:calc(100% - 40px);overflow:hidden;text-overflow:ellipsis}
/* a small tile (phone) shows a compact "×N" instead: on the cover a button that opens / closes the stack,
   on an open stack's other shots the badge drawn as "×N" (its full text stays for screen readers) */
.tile{container:tile/inline-size}
.tile .bx{display:none;position:absolute;right:4px;top:4px;z-index:2;min-width:32px;height:28px;padding:0 8px;align-items:center;justify-content:center;border:0;border-radius:999px;background:var(--acc);color:var(--on-acc);font-size:13px;font-weight:700;cursor:pointer}
@container tile (max-width:149px){
 .tile .bx{display:flex}
 .tile .bx~.badge,.tile .bx~.x{display:none}
 .tile .badge{font-size:0;padding:2px 7px}
 .tile .badge::before{content:"×" attr(data-count);font-size:12px}
 .tile .badge[data-pos]::before{content:attr(data-pos) "/" attr(data-count)}
 .tile .bx.open{background:var(--panel);color:var(--fg);border:1px solid var(--acc)}
}
/* a tile's Share button (bottom right): on hover or focus with a mouse, always on a touch screen */
.tile .tshare{position:absolute;right:4px;bottom:4px;z-index:2;width:34px;height:34px;padding:0;display:flex;align-items:center;justify-content:center;border:0;border-radius:999px;background:rgba(0,0,0,.62);color:#fff;cursor:pointer}
.tile .tshare:hover{background:rgba(0,0,0,.82)}
.tile:has(.tshare) .flag{max-width:calc(100% - 50px)}
.tile:has(.star) .flag{max-width:calc(100% - 88px)}  /* clear of the corner group: the star, then Share */
@media (hover:hover){.tile .tshare{opacity:0;transition:opacity .1s}.tile:hover .tshare,.tile:focus-within .tshare{opacity:1}}
@media (forced-colors:active){.tile .tshare{border:1px solid ButtonText}}
.flag{position:absolute;left:6px;bottom:6px;width:max-content;max-width:calc(100% - 12px);background:rgba(0,0,0,.62);color:#fff;font-size:11px;border-radius:4px;padding:1px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sentinel{height:40px}
/* bottom action bar: only while something is selected (or the Trash suggestions view's Apply) */
/* sticky, last in #app: it floats over the grid while scrolling, but takes its own room at the end, so no tile
   stays under it however many rows its buttons wrap to */
#actionbar{position:sticky;margin:0 16px;bottom:16px;z-index:12;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:center;padding:10px 14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.grp{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.fixopt{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin:8px 0}.fixopt input:not([type=radio]){flex:1 1 12em;min-width:0;max-width:100%}.fixopt input[type=radio]{height:auto;padding:0;margin:0}
.fixlist{margin:4px 0 8px;padding-left:1.2em;max-height:9em;overflow:auto;overflow-wrap:anywhere}
#seln,#sugn{font-weight:650;font-size:14px;margin-right:4px;white-space:nowrap}
.sep{width:1px;height:22px;background:var(--line)}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:40;background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:14px;max-width:min(560px,90vw);box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:opacity .2s;display:flex;align-items:center;gap:12px}
#toast.on{opacity:1;pointer-events:auto}
#toast button,#etoast button{flex:none;border:0;background:none;color:inherit;font-weight:650;text-decoration:underline;padding:4px 2px;border-radius:4px}
#toast button:focus-visible{outline-color:var(--bg)}
/* an error stays until dismissed (role=alert: read out at once) */
#etoast{position:fixed;left:50%;top:calc(var(--hh) + 8px);transform:translateX(-50%);z-index:41;display:none;align-items:flex-start;gap:12px;background:var(--panel);color:var(--erase);border:1px solid var(--erase);padding:10px 14px;border-radius:8px;font-size:14px;width:max-content;max-width:min(640px,calc(100vw - 32px));box-shadow:var(--shadow)}
#etoast.on{display:flex}
#etoast span{flex:1;min-width:0;word-break:break-word;color:var(--fg)}
#etoast b{color:var(--erase)}
#etoast .act{color:var(--acc)}
/* the keyboard-shortcut list */
#keys .box{max-width:720px}
#keys h4{margin:14px 0 6px;font-size:12px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
#keys dl{display:grid;grid-template-columns:minmax(120px,auto) 1fr;gap:6px 14px;margin:0;font-size:14px}
#keys dt{white-space:nowrap}
#keys dd{margin:0}

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
#conv{position:absolute;left:50%;bottom:72px;transform:translateX(-50%);z-index:3;max-width:calc(100% - 32px);border-radius:8px;background:rgba(0,0,0,.72);color:#fff;padding:8px 14px;font-size:14px;text-align:center;pointer-events:none}
#unmute{position:absolute;left:16px;top:16px;z-index:3;border:0;border-radius:8px;background:rgba(0,0,0,.6);color:#fff;padding:6px 12px;font-size:14px}
#info{width:360px;flex:none;overflow-y:auto;background:#16171b;border-left:1px solid #2a2b31;padding:16px 18px 24px;font-size:14px;display:flex;flex-direction:column;gap:16px}
#info .ihead{display:flex;gap:8px;align-items:flex-start}
#info h2{flex:1;margin:4px 0 0;font-size:15px;font-weight:650;word-break:break-all}
#info .icon{color:#bbb}#info .icon:hover{background:#26272d;color:#fff}
#info .btn{background:#202127;border-color:#34353c;color:#e9e9ec;height:32px}
#info .btn:hover{background:#2a2b31}
#info .btn.share{flex:none;gap:6px;background:#2456d3;border-color:#2456d3;color:#fff;font-weight:600}#info .btn.share:hover{background:#1d47b0}
#modal .mopenp{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
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
#info input::placeholder{color:#9a9ba3}
#info .fine{color:#8d8e96;font-size:12px;margin:0}
@media (max-width:760px){#viewer.on{display:block;overflow-y:auto;overscroll-behavior:contain}#stage{height:88vh;height:88dvh}#info{width:auto;overflow:visible;border-left:0;border-top:1px solid #2a2b31}#media{inset:8px}.arrow{width:36px;height:52px}
 #vclose{position:fixed;top:8px;right:8px;z-index:6;width:44px;height:44px;background:rgba(0,0,0,.6);color:#fff}}

/* ---------------------------------------------------------------- confirm / job dialog */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:30;display:none;align-items:center;justify-content:center;padding:16px}
.modal.on{display:flex}
.box{background:var(--panel);color:var(--fg);border-radius:14px;padding:20px 22px;max-width:640px;width:100%;max-height:85vh;overflow:auto;box-shadow:var(--shadow)}
.box h3{margin:0 0 8px;font-size:17px}
.box pre{white-space:pre-wrap;word-break:break-all;font-size:12px;background:var(--card);padding:10px;border-radius:8px;max-height:50vh;overflow:auto}
.box input{height:34px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.mbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
#mstatus{margin:12px 0 0;padding:10px 12px;border-radius:8px;background:var(--acc-soft)}
#mstatus[data-state=done]{background:color-mix(in srgb,var(--keep) 12%,var(--panel));font-weight:600}
#mstatus[data-state=failed],#mstatus[data-state=crashed]{background:color-mix(in srgb,var(--erase) 12%,var(--panel));font-weight:600}
.mthumbs{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:4px 0 8px}
.mthumbs img{width:56px;height:56px;object-fit:cover;border-radius:6px;background:var(--card)}
.mthumbs span{color:var(--mut);font-size:13px;margin-left:4px}
.mnames{color:var(--mut);font-size:13px;word-break:break-word}
#jobpill{flex:0 1 auto;min-width:0;overflow:hidden;border-color:var(--acc);color:var(--acc)}

/* ---------------------------------------------------------------- narrow: the header's controls fit beside the title */
@media (max-width:900px){
 #tsize{width:72px}
 #close .lbl2{display:none}#close{padding:0 8px}
 #jobpill{padding:0 8px}#jobpill .pr{display:none}body.jobpill #tsizew{display:none}
 #etoast{flex-wrap:wrap}#etoast span{flex-basis:100%}
}
/* the same when a wide sidebar leaves the main column as narrow as a 900 px window used to (900 - 280) */
@container app (max-width:620px){
 #tsize{width:72px}
 #close .lbl2{display:none}#close{padding:0 8px}
 #jobpill{padding:0 8px}#jobpill .pr{display:none}body.jobpill #tsizew{display:none}
}
/* ---------------------------------------------------------------- phone */
@media (max-width:760px){
 #top{padding:0 12px;gap:8px}
 #vtitle{font-size:16px}
 #count,.size .ic{display:none}
 #ball{padding:0 9px}
 #chips,#evbar,#junkbar{padding:10px 12px 0}#note,#helpb{padding:0 12px}
 /* the burst help: one line, "more" for the whole text */
 #helpb .hshort,#hmore{display:inline}#helpb .hfull{display:none}
 #helpb.more .hshort{display:none}#helpb.more .hfull{display:inline}
 #hmore{margin-left:6px}
 main{padding:4px 12px 40px}
 .grid{gap:4px}
 #actionbar{margin:0 8px;bottom:8px;padding:8px;gap:6px}
 #actionbar .btn{padding:0 9px}
 #actionbar kbd,#actionbar .sep{display:none}
 #seln{white-space:normal;min-width:0}
}
/* ---------------------------------------------------------------- touch targets: 44 px on a phone or a touch screen */
@media (max-width:760px),(pointer:coarse){
 .btn,#info .btn{height:44px;min-width:44px;justify-content:center}
 .icon,#rail{width:44px;height:44px;flex:none}
 .collapsed .sb-top{padding:6px 7px;justify-content:center}
 .collapsed #views{padding:4px 6px}
 .nv,.collapsed .nv{min-height:44px}
 .fs-h,.opt,.yr .caret{min-height:44px}
 .caret{width:44px}
 #tsize{height:44px}
 .chip{height:44px}.chip .x{width:44px;height:44px}
 #junkbar select{height:44px}
 .tile .ckw{width:44px;height:44px}
 .tile .star{width:44px;height:44px;right:44px;bottom:0}
 .tile .tshare{width:44px;height:44px;right:0;bottom:0}
 .tile:has(.star) .flag{bottom:50px;max-width:calc(100% - 12px)}  /* a phone tile is too narrow beside two 44 px taps: above them */
 .tile .bx{top:8px;right:8px}
 .tile .bx::after{content:"";position:absolute;inset:-8px}
 .link::after{content:"";position:absolute;inset:-11px -8px}
 .link{position:relative;z-index:3}  /* its hit area over the month heading below */
 #helpb{margin-top:12px}  /* room for "more"'s hit area under the header */
 .tile .dec .btn{height:44px}
 .arrow{width:44px}
 /* Erase sits apart from Keep and Trash, so a slip of the thumb doesn't land on it */
 #berase{margin-left:14px}
}
/* no keyboard: the shortcut list and key hints go; the burst help says "tap" */
@media (hover:none){
 .sb-foot,.kbonly{display:none!important}
 #helpb .hshort .kb{display:none}
}
@media (hover:hover){#helpb .hshort .tch{display:none}}
</style></head><body data-view="lib">
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
 <symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></symbol>
 <symbol id="i-lib" viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></symbol>
 <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v5M14 11v5"/></symbol>
 <symbol id="i-plug" viewBox="0 0 24 24"><path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/></symbol>
 <symbol id="i-star" viewBox="0 0 24 24"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.3-4.1 5.9-.8z"/></symbol>
 <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></symbol>
 <symbol id="i-child" viewBox="0 0 24 24"><circle cx="12" cy="6.5" r="2.5"/><path d="M8 21v-6l-2-1 2-5h8l2 5-2 1v6M12 15v6"/></symbol>
 <symbol id="i-tag" viewBox="0 0 24 24"><path d="M3.5 12.5V4h8.5l8.5 8.5-8.5 8.5z"/><circle cx="8" cy="8.5" r="1.4"/></symbol>
 <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3"/></symbol>
 <symbol id="i-filter" viewBox="0 0 24 24"><path d="M4 5h16l-6 7.5V19l-4 1.5v-8z"/></symbol>
 <symbol id="i-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></symbol>
 <symbol id="i-size" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 14l4-4 5 5 2-2 5 5"/></symbol>
 <symbol id="i-power" viewBox="0 0 24 24"><path d="M12 3.5v8M7 6.5a7.5 7.5 0 1 0 10 0"/></symbol>
 <symbol id="i-pin" viewBox="0 0 24 24"><path d="M12 21s-6.5-6.2-6.5-11a6.5 6.5 0 0 1 13 0c0 4.8-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/></symbol>
 <symbol id="i-cal" viewBox="0 0 24 24"><rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
 <symbol id="i-share" viewBox="0 0 24 24"><path d="M12 15V4M8 8l4-4 4 4M5 12v7.5h14V12"/></symbol>
 <symbol id="i-keys" viewBox="0 0 24 24"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></symbol>
</svg>
<aside id="sidebar" aria-label="Views and filters">
 <div class="sb-top">
  <button type="button" class="icon" id="rail" aria-expanded="true" aria-controls="sidebar"><svg class="ic"><use href="#i-menu"/></svg></button>
  <span class="brand">Media gallery</span>
 </div>
 <nav id="views" aria-label="Views">
  <button type="button" class="nv" data-v="lib" title="Library"><svg class="ic"><use href="#i-lib"/></svg><span class="lbl">Library</span><span class="rl" aria-hidden="true">Library</span><span class="ct" id="c-lib"></span></button>
  <button type="button" class="nv" data-v="sug" title="Suggested junk"><svg class="ic"><use href="#i-trash"/></svg><span class="lbl">Suggested junk</span><span class="rl" aria-hidden="true">Junk</span><span class="ct" id="c-sug"></span></button>
  <div id="ptabs"></div>
  <div class="nvhead">Quick views</div>
  <button type="button" class="nv" data-v="fav" title="Favorites"><svg class="ic"><use href="#i-star"/></svg><span class="lbl">Favorites</span><span class="rl" aria-hidden="true">Favorites</span><span class="ct" id="c-fav"></span></button>
  <button type="button" class="nv" data-v="recent" title="Added in the last 30 days"><svg class="ic"><use href="#i-clock"/></svg><span class="lbl">Added in the last 30 days</span><span class="rl" aria-hidden="true">Recent</span><span class="ct" id="c-recent"></span></button>
  <button type="button" class="nv" data-v="child" title="Possible child"><svg class="ic"><use href="#i-child"/></svg><span class="lbl">Possible child</span><span class="rl" aria-hidden="true">Child</span><span class="ct" id="c-child"></span></button>
  <button type="button" class="nv" data-v="untagged" title="Untagged"><svg class="ic"><use href="#i-tag"/></svg><span class="lbl">Untagged</span><span class="rl" aria-hidden="true">No tags</span><span class="ct" id="c-untagged"></span></button>
  <button type="button" class="nv" data-v="dates" title="Dates to check"><svg class="ic"><use href="#i-cal"/></svg><span class="lbl">Dates to check</span><span class="rl" aria-hidden="true">Dates</span><span class="ct" id="c-dates"></span></button>
  <div id="evsec"><div class="nvhead">Events</div><div id="evlist"></div></div>
  <div id="plsec"><div class="nvhead">Places</div><div id="pllist"></div></div>
  <button type="button" class="nv railonly" id="rsearch" title="Search (/)"><svg class="ic"><use href="#i-search"/></svg><span class="vh">Search</span><span class="rl" aria-hidden="true">Search</span></button>
  <button type="button" class="nv railonly icon" id="rfilter" title="Filters" style="width:100%"><svg class="ic"><use href="#i-filter"/></svg><span class="vh">Filters</span><span class="rl" aria-hidden="true">Filters</span><span class="badge-n" id="rbadge" hidden></span></button>
 </nav>
 <div class="sb-search">
  <div class="field"><svg class="ic"><use href="#i-search"/></svg><input id="q" type="search" placeholder="Search photos &amp; videos" aria-label="Search" autocomplete="off"></div>
  <div id="qhint" class="hint" hidden></div>
 </div>
 <div id="filters" role="region" aria-label="Filters">
  <div class="fhead"><b>Filters</b><button type="button" class="link" id="clear" hidden></button></div>
  <div id="fsecs"></div>
 </div>
 <div class="sb-foot">
  <button type="button" class="nv" id="bkeys" title="Keyboard shortcuts (?)" aria-keyshortcuts="?" aria-haspopup="dialog"><svg class="ic"><use href="#i-keys"/></svg><span class="lbl">Keyboard shortcuts</span><span class="ct"><kbd>?</kbd></span></button>
 </div>
</aside>
<div id="sbresize" role="separator" aria-orientation="vertical" aria-controls="sidebar" aria-label="Sidebar width" tabindex="0" title="Drag to resize the sidebar (or Left and Right arrows); double-click for the default width"></div>
<div id="scrim"></div>
<div id="app">
 <header id="top">
  <h1 id="vtitle">Library</h1><span id="count"></span><span class="grow"></span>
  <button type="button" class="btn" id="ball" aria-keyshortcuts="Control+A Meta+A" title="Select every tile shown (Ctrl+A, or Cmd+A on a Mac); Esc clears" hidden>Select all</button>
  <label class="size" id="tsizew" title="Thumbnail size"><svg class="ic"><use href="#i-size"/></svg><span class="vh">Thumbnail size</span><input id="tsize" type="range" min="80" max="320" step="8"></label>
  <button type="button" class="btn" id="jobpill" aria-haspopup="dialog" hidden></button>
  <button type="button" class="btn" id="close" title="Stop the gallery server" aria-label="Stop server"><svg class="ic"><use href="#i-power"/></svg><span class="lbl2">Stop server</span></button>
 </header>
 <div id="chips" aria-label="Active filters" hidden></div>
 <div id="evbar" role="toolbar" aria-label="Event" hidden><span class="evt" id="evname"></span><span class="evslug" id="evslug" title="The event's name in the sidecars" hidden></span><button type="button" class="btn" id="evlabel">Display name…</button><button type="button" class="btn" id="evren">Rename…</button><button type="button" class="btn" id="evmerge">Merge into…</button></div>
 <div id="junkbar" role="group" aria-label="Narrow the suggestions" hidden>
  <span class="jf"><label for="jwhy">Why</label><select id="jwhy"></select></span>
  <span class="jf"><label for="jmin">Junk score</label><select id="jmin" title="The vision check's junk score (0 to 1). Files tagged as junk and copies of photos you trashed show at any score."></select></span>
  <button type="button" class="btn" id="jlower" hidden></button>
 </div>
 <p id="note" class="note" hidden></p>
 <p id="helpb" class="note" hidden><span class="hshort"><span class="kb">Bursts are stacked; X opens one.</span><span class="tch">Burst stacks: tap ×N to open one.</span></span><span class="hfull">A stack is a burst (shots from one camera a moment apart): its Show all button (×N on a small tile) or the X key opens or closes it; the shots of an open stack share a coloured ring. On a closed stack, selecting and every action (Keep, Trash, Erase, tags, Rotate, Set event, Share) act on the cover shot only; open the stack to act on the others.</span><button type="button" class="link" id="hmore" aria-controls="helpb" aria-expanded="false">more</button></p>
 <main id="main" aria-busy="true"><div id="list"><div class="state" role="status"><p><span class="spin" aria-hidden="true"></span>Loading your library…</p></div></div><div id="sentinel"></div><div id="plugin" hidden></div></main>
 <div id="actionbar" hidden>
  <div class="grp" id="selgrp" role="toolbar" aria-label="Selection actions" hidden><span id="seln"></span>
   <button type="button" class="btn keep" id="bkeep">Keep <kbd>K</kbd></button><button type="button" class="btn trash" id="btrash">Trash <kbd>T</kbd></button><button type="button" class="btn erase" id="berase">Erase <kbd>E</kbd></button>
   <span class="sep"></span>
   <button type="button" class="btn" id="bfav" title="Star the selection as favorites (F toggles)">★ Favorite <kbd>F</kbd></button><button type="button" class="btn" id="bunfav" title="Take the selection out of Favorites">☆ Unfavorite</button>
   <button type="button" class="btn" id="btag">+ tag</button><button type="button" class="btn" id="buntag">− tag</button><button type="button" class="btn" id="bevent">Set event…</button><button type="button" class="btn" id="bfixdate" hidden>Fix date…</button><button type="button" class="btn" id="brot">Rotate ⟳</button><button type="button" class="btn" id="bshare">Share</button><button type="button" class="btn" id="bnone">Clear selection <kbd>Esc</kbd></button>
  </div>
  <div class="grp" id="suggrp" role="group" aria-label="Decisions" hidden><span id="sugn"></span><span class="note" style="margin:0">Mark Keep or Trash on each, then Apply<span class="kbonly"> (keys: K, T, arrows)</span></span><button type="button" class="btn" id="bmarkall" title="Mark every tile shown Trash (a tile marked Keep keeps its mark); nothing changes until Apply">Mark all shown as Trash</button><button type="button" class="btn" id="bclearm" title="Take every Keep and Trash mark off (asks first)">Clear marks</button><button type="button" class="btn primary" id="sugapply">Apply…</button></div>
 </div>
</div>
<div id="toast" role="status" aria-live="polite"></div>
<p id="selsay" class="vh" role="status" aria-live="polite"></p>
<div id="etoast" role="alert" aria-atomic="true"></div>
<div id="viewer" role="dialog" aria-modal="true" aria-label="Item" tabindex="-1">
 <div id="stage"><div id="media"></div>
  <button type="button" class="arrow" id="vprev" aria-label="Previous (Left arrow)">‹</button><button type="button" class="arrow" id="vnext" aria-label="Next (Right arrow)">›</button></div>
 <aside id="info" aria-label="Details"></aside>
</div>
<div class="modal" id="modal" role="dialog" aria-modal="true" aria-labelledby="mh" data-dialog="confirm"><div class="box"><h3 id="mh"></h3><div id="mb"></div>
 <p id="mfieldp" hidden><label for="mfield" id="mflabel"></label><input id="mfield" list="evnames" autocomplete="off" spellcheck="false"><datalist id="evnames"></datalist><datalist id="mytags"></datalist></p>
 <p id="mtype" hidden>Type <b>ERASE</b> to confirm: <input id="mtyped" autocomplete="off" spellcheck="false" aria-label="Type ERASE"></p>
 <p id="mstatus" role="status" hidden></p><pre id="mout" hidden></pre>
 <div class="mbtns"><button type="button" class="btn" id="mno">Cancel</button><button type="button" class="btn primary" id="mgo">Confirm</button></div></div></div>
<div class="modal" id="keys" role="dialog" aria-modal="true" aria-labelledby="kh"><div class="box"><h3 id="kh">Keyboard shortcuts</h3>
 <p class="note">Keys act on the page, never while you type in a field. Esc closes this list.</p>
 <h4>Grid</h4><dl>
  <dt><kbd>Enter</kbd></dt><dd>Open the focused tile in the large view</dd>
  <dt><kbd>Space</kbd></dt><dd>Select or unselect the focused tile (<kbd>Shift</kbd>+<kbd>Space</kbd>: the range from the last one)</dd>
  <dt>Shift+click, Ctrl+click</dt><dd>Select a range; tick or untick one tile</dd>
  <dt><kbd>Ctrl</kbd>+<kbd>A</kbd> / <kbd>Cmd</kbd>+<kbd>A</kbd></dt><dd>Select every tile shown</dd>
  <dt>Drag</dt><dd>With the mouse, from a gap or a photo: select every tile the rectangle touches (<kbd>Shift</kbd> or <kbd>Ctrl</kbd>: add to the selection); <kbd>Esc</kbd> while dragging puts the selection back</dd>
  <dt><kbd>K</kbd> <kbd>T</kbd> <kbd>E</kbd></dt><dd>Keep, Trash (asks first), Erase (asks, ERASE typed) the selection</dd>
  <dt><kbd>F</kbd></dt><dd>Favorite or unfavorite the selection (with none, the focused tile): unstars when every one is a favorite</dd>
  <dt><kbd>Esc</kbd></dt><dd>Clear the selection; close the sidebar drawer (phone); in the search box, clear the search</dd>
  <dt><kbd>/</kbd></dt><dd>Jump to the search box</dd>
  <dt><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Cmd</kbd>+<kbd>Z</kbd></dt><dd>Undo the last Keep, Favorite, tag, Rotate or Set event while its message shows</dd>
  <dt><kbd>?</kbd></dt><dd>This list</dd>
 </dl>
 <h4>Sidebar</h4><dl>
  <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>On the sidebar's edge (Tab to it): narrower / wider; with <kbd>Shift</kbd>, in bigger steps; <kbd>Home</kbd> / <kbd>End</kbd> the narrowest / widest. A double-click on the edge puts the default width back</dd>
 </dl>
 <h4>Bursts</h4><dl>
  <dt><kbd>X</kbd></dt><dd>Open or close the focused tile's burst stack (focus goes back to the cover)</dd>
 </dl>
 <h4>Large view</h4><dl>
  <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>Previous / next item (over a closed stack's hidden shots)</dd>
  <dt><kbd>Space</kbd></dt><dd>Play / pause a video</dd>
  <dt><kbd>K</kbd> <kbd>T</kbd> <kbd>E</kbd></dt><dd>Keep, Trash, Erase the item shown (in Suggested junk, K and T mark it and go to the next)</dd>
  <dt><kbd>F</kbd></dt><dd>Favorite or unfavorite the item shown</dd>
  <dt><kbd>R</kbd> / <kbd>Shift</kbd>+<kbd>R</kbd></dt><dd>Rotate clockwise / anticlockwise (the sidecar only)</dd>
  <dt><kbd>Esc</kbd></dt><dd>Close the large view</dd>
 </dl>
 <h4>Suggested junk</h4><dl>
  <dt><kbd>←</kbd> <kbd>↑</kbd> / <kbd>→</kbd> <kbd>↓</kbd> <kbd>J</kbd></dt><dd>Move the outline</dd>
  <dt><kbd>K</kbd> <kbd>T</kbd></dt><dd>Mark the outlined tile Keep / Trash, like its Keep and Trash buttons (nothing changes until Apply); with no outline yet, the tile under the pointer, or the first is outlined</dd>
  <dt><kbd>Shift</kbd>+<kbd>K</kbd> / <kbd>Shift</kbd>+<kbd>T</kbd></dt><dd>Mark every tile from the last one given that mark to the outlined one (a Shift-click on Keep or Trash does too; a tile marked the other way keeps its mark)</dd>
  <dt>Drag</dt><dd>From a tile's Keep or Trash: give that mark to every tile the rectangle touches</dd>
  <dt><kbd>Enter</kbd></dt><dd>Open the outlined tile (a click on the photo does too)</dd>
 </dl>
 <h4>Dialogs</h4><dl>
  <dt><kbd>Esc</kbd></dt><dd>Cancel and close (a running Trash, Erase or Share keeps going)</dd>
  <dt><kbd>Enter</kbd></dt><dd>Confirm, in a dialog's text field</dd>
  <dt><kbd>Tab</kbd></dt><dd>Move between the dialog's controls (focus stays in the dialog)</dd>
 </dl>
 <div class="mbtns"><button type="button" class="btn primary" id="kclose">Close</button></div></div></div>
<script>
"use strict";
const CSRF=document.querySelector('meta[name=csrf]').content;
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
const fmt=n=>(+n||0).toLocaleString();
// per-browser conveniences only (sidebar rail and width, open sections, tile size); the page works without them
const LS={get(k,d){try{const v=localStorage.getItem("gallery."+k);return v===null?d:v}catch(e){return d}},set(k,v){try{localStorage.setItem("gallery."+k,v)}catch(e){}}};
let view="lib",items=[],total=0,loading=false,done=false,sel=new Set(),last=null,cur=-1,ranked=false,simId=null,simName="",dec={},vi=-1,facets=null,gen=0,lastFocus=null,openB={},allSel=null;
let F={aimin:"0.7"};
// Suggested junk's own narrowing (the bar above its grid): Why (a reason) and the vision check's Junk score floor
const JLINE="0.7",JMINS=[["0.9","0.9 or more"],["0.8","0.8 or more"],[JLINE,"0.7 or more (default)"],["0.5","0.5 or more"],["0.3","0.3 or more"],["0","Any score"]];
let J={why:"",min:JLINE};
const ACTIVE=["kind","ym","project","source","place","myplace","event","label","mine","review","ai","used","added","dates"];
// internal names -> the words shown: gallery.py WORDS, the one place (no Owner|, Review|, Label| in the page's text)
const WORDS=__WORDS__;
const NAMES=WORDS.filter;
const REVIEW={"maybe-trash":WORDS.flag["maybe-trash"],"child-in-shot":WORDS.flag["child-in-shot"],untagged:WORDS.flag.untagged,favorite:WORDS.flag.favorite};
const AIV=WORDS.ai;
const VIEWS=__VIEWS__;window.mediaIntakeViews=window.mediaIntakeViews||{};
const TITLES=Object.assign({},WORDS.view);
const plural=(n,w)=>fmt(n)+" "+w+(n===1?"":"s");
// a tool's tag as a user reads it ("Project: Personal", "Suggested junk"); null: not shown (Owner|, a model's confidence)
function plainTag(t){if(Object.prototype.hasOwnProperty.call(WORDS.tag,t))return WORDS.tag[t];const i=t.indexOf("|");if(i<0)return t;const ns=t.slice(0,i),v=t.slice(i+1);
 if(ns==="Owner")return null;
 if(ns==="Review"){if(v.startsWith("untagged-"))return WORDS.flag.untagged;const m=/^vlm-trash\/?(.*)$/.exec(v);if(m)return WORDS.ns.AI+": "+WORDS.ai.trash+(m[1]?" ("+m[1].replace(/[/|]/g," ")+")":"")}
 if(ns==="AI"){const m=/^vlm\/[^/]+\/(trash|keep)(?:\/(.*))?$/.exec(v);if(m)return WORDS.ns.AI+": "+WORDS.ai[m[1]]+(m[2]?" ("+m[2].replace(/[/|]/g," ")+")":"");return null}
 return (WORDS.ns[ns]||ns)+": "+v.split("|").join(" › ")}
VIEWS.forEach(v=>{TITLES["p-"+v.id]=v.title});
const activeKeys=()=>ACTIVE.filter(k=>F[k]);
const plugView=()=>view.startsWith("p-");
async function api(path,body){const o={credentials:"same-origin"};if(body!==undefined){o.method="POST";o.headers={"Content-Type":"application/json","X-CSRF":CSRF};o.body=JSON.stringify(body)}
 const r=await fetch(path,o);let j={};try{j=await r.json()}catch(e){}
 if(!r.ok){const er=new Error(j.error||("HTTP "+r.status));er.busy=!!j.busy;er.status=r.status;throw er}return j}
function filt(){const f={};for(const k of ACTIVE.concat(["aimin","days"]))if(F[k])f[k]=F[k];
 if(f.ai!=="trash")delete f.aimin;if(f.used!=="unused")delete f.days;const q=$("#q").value.trim();if(q)f.q=q;if(simId!==null){f.similar=simId;delete f.q}
 if(view==="sug"){f.view="suggest";if(J.why)f.why=J.why;f.jmin=J.min}return f}
function qs(o){return Object.entries(o).map(([k,v])=>encodeURIComponent(k)+"="+encodeURIComponent(v)).join("&")}
function monthName(k){const [y,m]=k.split("-");return new Date(+y,+m-1,1).toLocaleString(undefined,{month:"long",year:"numeric"})}
function monthOnly(k){const [y,m]=k.split("-");return new Date(+y,+m-1,1).toLocaleString(undefined,{month:"long"})}
function chipVal(k){const v=F[k];if(k==="kind")return v==="photo"?"Photos":"Videos";if(k==="ym")return v.length===4?v:monthName(v);if(k==="review")return REVIEW[v]||v;
 if(k==="ai")return v==="trash"?WORDS.score+" ≥ "+(+F.aimin||0).toFixed(2):AIV[v]||v;if(k==="used")return v==="used"?"in a post":(F.days?"not in "+F.days+" days":"never");
 if(k==="added")return "last "+v+" days";if(k==="myplace")return plName(v);if(k==="dates")return v==="all"?"all":WORDS.dates[v]||v;return v}
// ---------------------------------------------------------------- sidebar: layout
const mqPhone=matchMedia("(max-width:760px)");let railPref=LS.get("rail","0")==="1",drawer=false;
function layout(){const ph=mqPhone.matches,col=ph?!drawer:railPref;document.body.classList.toggle("collapsed",col);document.body.classList.toggle("overlay",ph&&drawer);
 const b=$("#rail");b.setAttribute("aria-expanded",String(!col));b.setAttribute("aria-label",col?"Expand sidebar":"Collapse sidebar");b.title=col?"Expand sidebar":"Collapse sidebar"}
function expandSidebar(){if(mqPhone.matches)drawer=true;else{railPref=false;LS.set("rail","0")}layout()}
$("#rail").onclick=()=>{if(mqPhone.matches)drawer=!drawer;else{railPref=!railPref;LS.set("rail",railPref?"1":"0")}layout()};
$("#scrim").onclick=()=>{drawer=false;layout()};
// the burst help at phone width: one line, "more" for the whole text (remembered per browser)
function helpMore(on){$("#helpb").classList.toggle("more",on);$("#hmore").textContent=on?"less":"more";$("#hmore").setAttribute("aria-expanded",String(on))}
helpMore(LS.get("helpmore","0")==="1");
$("#hmore").onclick=()=>{const on=!$("#helpb").classList.contains("more");helpMore(on);LS.set("helpmore",on?"1":"0")};
mqPhone.addEventListener("change",()=>{drawer=false;layout()});
$("#rsearch").onclick=()=>{expandSidebar();$("#q").focus()};
// ---------------------------------------------------------------- sidebar: width (its right edge; desktop, expanded only)
// drag the edge, or Left / Right arrows on it (Shift: bigger steps, Home / End: the ends); a double-click puts the default back.
// Clamped to 240 px .. min(560 px, half the window, the window less 480 px), again when the window resizes; the chosen
// width is kept per browser. 480 px: the narrowest main column the smallest desktop window (761 px) had beside the old
// fixed 280 px sidebar, where the compact header fits.
const SBW0=300,SBWMIN=240,MAINMIN=480,sbMax=()=>Math.max(SBWMIN,Math.min(560,Math.floor(innerWidth/2),innerWidth-MAINMIN));
let sbwPref=+LS.get("sbw",String(SBW0));if(!(sbwPref>=SBWMIN&&sbwPref<=2000))sbwPref=SBW0;
function sbWidth(w,save){const h=$("#sbresize"),mx=sbMax(),v=Math.round(Math.min(mx,Math.max(SBWMIN,w))),r=document.documentElement.style;
 r.setProperty("--sbw",v+"px");r.setProperty("--ml",v+"px");
 h.setAttribute("aria-valuemin",String(SBWMIN));h.setAttribute("aria-valuemax",String(mx));h.setAttribute("aria-valuenow",String(v));h.setAttribute("aria-valuetext",v+" pixels");
 if(save){sbwPref=v;LS.set("sbw",String(v))}return v}
sbWidth(sbwPref,false);
addEventListener("resize",()=>sbWidth(sbwPref,false));
{const h=$("#sbresize");let drag=null,raf=0,nx=0;
 h.addEventListener("pointerdown",ev=>{if(ev.button!==0)return;ev.preventDefault();h.setPointerCapture(ev.pointerId);
  drag={x:ev.clientX,w:parseFloat(h.getAttribute("aria-valuenow"))||SBW0,id:ev.pointerId};document.body.classList.add("sbdrag")});
 // one width per frame, however many pointer events arrive: the grid reflows at most once a frame
 h.addEventListener("pointermove",ev=>{if(!drag||ev.pointerId!==drag.id)return;nx=ev.clientX;
  if(!raf)raf=requestAnimationFrame(()=>{raf=0;if(drag)sbWidth(drag.w+nx-drag.x,false)})});
 const end=ev=>{if(!drag||ev.pointerId!==drag.id)return;if(raf){cancelAnimationFrame(raf);raf=0}
  // a drag the browser interrupts (pointercancel, or the capture lost before pointerup) puts the width from before it back
  if(ev.type==="pointerup")sbWidth(drag.w+ev.clientX-drag.x,true);else sbWidth(drag.w,false);
  drag=null;document.body.classList.remove("sbdrag")};
 h.addEventListener("pointerup",end);h.addEventListener("pointercancel",end);h.addEventListener("lostpointercapture",end);
 h.addEventListener("dblclick",()=>sbWidth(SBW0,true));
 h.addEventListener("keydown",ev=>{const now=parseFloat(h.getAttribute("aria-valuenow"))||SBW0,step=ev.shiftKey?64:16;let w=null;
  if(ev.key==="ArrowLeft")w=now-step;else if(ev.key==="ArrowRight")w=now+step;else if(ev.key==="Home")w=SBWMIN;else if(ev.key==="End")w=sbMax();
  // no edge key while a dialog is open (the shortcut list, a confirm, the large view): the page's own rule
  if(w===null||ev.ctrlKey||ev.metaKey||ev.altKey||topDialog())return;ev.preventDefault();ev.stopPropagation();sbWidth(w,true)})}
$("#rfilter").onclick=()=>{expandSidebar();const f=$("#filters");if(!f.hidden){f.scrollIntoView({block:"start"});const h=f.querySelector(".fs-h");if(h)h.focus()}};
// ---------------------------------------------------------------- sidebar: filter sections
const SECS=[["kind","Kind","1"],["when","When","1"],["project","Project","1"],["source","Source","1"],["place","Place","0"],["event","Event","0"],
 ["label",NAMES.label,"1"],["mine","My tags","0"],["review",NAMES.review,"1"],["ai",NAMES.ai,"0"],["used","Used","0"]];
const SECKEY={when:"ym"};
function buildSections(){const box=$("#fsecs");
 for(const [s,title,open0] of SECS){const sec=el("section","fs");sec.dataset.s=s;const h3=el("h3"),h=el("button","fs-h");h.type="button";const bid="fsb-"+s;h.setAttribute("aria-controls",bid);
  h.append(el("span","fs-t",title),el("span","fs-v"));h.insertAdjacentHTML("beforeend",'<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg>');h3.append(h);
  const b=el("div","fs-b");b.id=bid;sec.append(h3,b);box.append(sec);
  if(s==="kind"){const g=el("div","seg");g.setAttribute("role","group");g.setAttribute("aria-label","Kind");
   [["","All"],["photo","Photos"],["video","Videos"]].forEach(([v,l])=>{const x=el("button");x.type="button";x.dataset.k="kind";x.dataset.val=v;const c=el("span","ct");c.id="ck-"+(v||"all");x.append(el("span",null,l),c);x.onclick=()=>setF("kind",v);g.append(x)});b.append(g)}
  else if(s==="when"){const w=el("div");w.id="when";b.append(w)}
  else{const o=el("div","opts");o.dataset.k=s;o.dataset.name=title.toLowerCase();b.append(o)}
  if(s==="ai"){const d=el("div","sub");d.id="aiconf";d.innerHTML='<label for="aimin">'+WORDS.score+' at least <output id="aiv">0.70</output></label><input type="range" id="aimin" min="0" max="1" step="0.05">';b.append(d)}
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
// the most recent YRSHOW years, then "All years (N)" for the rest (open or not remembered per browser); the
// chosen year always shows
const YRSHOW=5;let yrAll=LS.get("whenall","0")==="1";
function renderWhen(){const box=$("#when");box.replaceChildren();const ent=facets.facets.month||[],yrs=ent.filter(([n])=>n.length===4);
 if(!yrs.length)box.append(el("div","empty","No dates"));if(F.ym)openYears.add(F.ym.slice(0,4));
 const show=yrAll?yrs.slice():yrs.slice(0,YRSHOW);if(F.ym&&!show.some(([y])=>y===F.ym.slice(0,4))){const x=yrs.find(([y])=>y===F.ym.slice(0,4));if(x)show.push(x)}
 show.forEach(([y,c])=>{const row=el("div","yr"),cb=el("button","caret","▶");cb.type="button";const mos=el("div","mos");
  const setO=o=>{mos.hidden=!o;cb.setAttribute("aria-expanded",String(o));cb.setAttribute("aria-label",(o?"Hide":"Show")+" the months of "+y);o?openYears.add(y):openYears.delete(y)};
  ent.filter(([n])=>n.length===7&&n.startsWith(y)).forEach(([m,mc])=>mos.append(optBtn("ym",m,monthOnly(m),mc)));
  setO(openYears.has(y));cb.onclick=()=>setO(mos.hidden);row.append(cb,optBtn("ym",y,y,c));box.append(row,mos)});
 if(yrs.length>YRSHOW){const m=el("button","link yrmore",yrAll?"Fewer years":"All years ("+fmt(yrs.length)+")");m.type="button";m.setAttribute("aria-expanded",String(yrAll));
  m.onclick=()=>{yrAll=!yrAll;LS.set("whenall",yrAll?"1":"0");renderWhen();syncUI();const b=$("#when .yrmore");if(b)b.focus()};box.append(m)}}
async function loadFacets(tries){facets=await api("/api/facets");const fc=facets.facets,c=facets.counts||{};
 ["project","source","place","event","label","mine"].forEach(k=>renderOpts(k,fc[k]||[]));
 renderOpts("review",Object.keys(REVIEW).map(v=>[v,c["review:"+v]||0]),v=>REVIEW[v]);
 renderOpts("ai",Object.keys(AIV).map(v=>[v,v==="trash"?judgedAt(F.aimin):c["ai:"+v]||0]),v=>AIV[v]);
 renderOpts("used",[["used",c["used:used"]||0],["unused",c["used:unused"]||0]],v=>v==="used"?WORDS.flag.used:"Not used");
 renderWhen();renderEvents();renderPlaces();renderMyTags();
 $("#ck-all").textContent=fmt(facets.total);$("#ck-photo").textContent=fmt(c["kind:photo"]);$("#ck-video").textContent=fmt(c["kind:video"]);
 $("#c-lib").textContent=fmt(facets.total);$("#c-sug").textContent=fmt(c.suggest);$("#c-recent").textContent=fmt(c.recent);$("#c-fav").textContent=fmt(c["review:favorite"]);
 $('.nv[data-v=sug]').title=WORDS.view.sug+": the vision check's junk verdicts, files tagged as junk and copies of photos you trashed"+(c["suggest:lower"]?" ("+fmt(c["suggest:lower"])+" more with a lower junk score inside)":"");
 $("#c-child").textContent=fmt(c["review:child-in-shot"]);$("#c-untagged").textContent=fmt(c["review:untagged"]);$("#c-dates").textContent=fmt(c.dates||0);
 $('.nv[data-v=recent]').title=WORDS.view.recent+" (by when each file was filed, not when it was taken)";
 const h=$("#qhint");h.replaceChildren();h.hidden=!facets.search;
 if(facets.search){const checking=/^checking/.test(facets.search);const d=el("details"),s=el("summary",null,checking?"Checking the search model…":"Semantic search off: tags and names only");
  d.append(s,el("div","why",facets.search));h.append(d);h.title=facets.search;if(checking&&(tries||0)<10)setTimeout(()=>loadFacets((tries||0)+1).catch(()=>{}),1500)}
 syncUI()}
// ---------------------------------------------------------------- events (Event| tags): the sidebar list, rename, merge
let evAll=false;const EVSHOW=8;
// an event's display name (/api/facets event_names: never two alike, else the slug itself); the slug stays in tooltips
function evLabel(n){const m=facets&&facets.event_names,x=m&&Object.hasOwn(m,n)?m[n]:null;return x&&typeof x.label==="string"&&x.label?x.label:n}
// the tag field's suggestions: every tag of yours in the library (the My tags facet)
function renderMyTags(){$("#mytags").replaceChildren(...((facets&&facets.facets.mine)||[]).map(([n])=>{const o=el("option");o.value=n;return o}))}
function renderEvents(){const L=$("#evlist");L.replaceChildren();const ev=(facets&&facets.facets.event)||[];
 $("#evnames").replaceChildren(...ev.map(([n])=>{const o=el("option");o.value=n;return o}));
 if(!ev.length){L.append(el("div","empty","No events yet"));return}
 const show=evAll?ev.slice():ev.slice(0,EVSHOW);if(F.event&&!show.some(([n])=>n===F.event)){const x=ev.find(([n])=>n===F.event);if(x)show.push(x)}
 show.forEach(([n,c])=>{const b=el("button","nv");b.type="button";b.dataset.ev=n;b.title="Event: "+n;b.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-cal"/></svg>');b.append(el("span","lbl",evLabel(n)),el("span","ct",fmt(c)));
  b.onclick=()=>{F={aimin:F.aimin,event:n};$("#q").value="";setView("lib");if(mqPhone.matches&&drawer){drawer=false;layout()}};L.append(b)});
 if(ev.length>EVSHOW){const m=el("button","link evmore",evAll?"Fewer events":"All "+fmt(ev.length)+" events");m.type="button";m.onclick=()=>{evAll=!evAll;renderEvents();syncUI()};L.append(m)}}
// ---------------------------------------------------------------- My places (Place|<name>, places.json on this computer)
// a place by its id (/api/facets places: id, name, n); the page never gets a place's or an item's coordinates
function plName(id){const p=((facets&&facets.places)||[]).find(x=>x.id===id);return p?p.name:"a removed place"}
function renderPlaces(){const L=$("#pllist");L.replaceChildren();const ps=(facets&&facets.places)||[];
 if(facets&&facets.places_error){L.append(el("div","empty",facets.places_error));return}
 if(!ps.length){const e=el("div","empty","No places yet. Open a photo that has a location and choose Add a place here…");L.append(e);return}
 ps.forEach(p=>{const b=el("button","nv");b.type="button";b.dataset.pl=p.id;b.title=WORDS.filter.myplace+": "+p.name;b.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-pin"/></svg>');b.append(el("span","lbl",p.name),el("span","ct",fmt(p.n)));
  b.onclick=()=>{F={aimin:F.aimin,myplace:p.id};$("#q").value="";setView("lib");if(mqPhone.matches&&drawer){drawer=false;layout()}};L.append(b)})}
function radiusText(r){return r>=1000?(r/1000)+" km":r+" m"}
function askPlace(d,n){const sl=el("select");sl.id="plradius";((facets&&facets.radii)||[150]).forEach(r=>{const o=el("option",null,radiusText(r));o.value=String(r);sl.append(o)});sl.value=String((facets&&facets.radius)||150);
 const lab=el("label",null,"Radius ");lab.htmlFor="plradius";const p=el("p","plrad");p.append(lab,sl);
 askField("Add a place here?",["Every photo and video taken within the radius of where "+d.name+" was taken gets this place, beside its city. Ones you took the place off stay off.","The place's name and location stay on this computer only (the state folder), never inside the photos, and share-safe copies never carry them.",p],"Place name (like Home)","",
  async()=>{const j=await api("/api/place/add",{id:d.id,name:$("#mfield").value,radius:+sl.value});closeModal();patchTiles(j.items);toast(j.result);await loadFacets();if(vi===n)openViewer(n,{keep:true})},null)}
async function answerProbable(d,n,pl,yes){try{const j=await api("/api/place/probable",{id:d.id,place:pl.id,yes});patchTiles(j.items);toast(j.result);loadFacets();if(vi===n)openViewer(n,{keep:true})}catch(e){refused("",e)}}
function askField(title,lines,label,value,go,list){confirmBox(title,lines,false,go);$("#mfieldp").hidden=false;$("#mflabel").textContent=label;const f=$("#mfield");if(list===null)f.removeAttribute("list");else f.setAttribute("list",list||"evnames");f.value=value||"";f.focus();f.select()}
async function afterEvent(j,ev){closeModal();toast(j.result,j.undo);if(ev!==undefined){if(ev)F.event=ev;else delete F.event}await loadFacets();reset()}
$("#evlabel").onclick=()=>{const ev=F.event,m=facets&&facets.event_names,x=m&&Object.hasOwn(m,ev)?m[ev]:null;askField("Display name for the event "+ev+"?",["The sidebar and the title show this name instead of "+ev+". The sidecars keep "+ev+": your files and tags don't change.","Leave it empty for the default. A name that looks like another event's (the same but for case or spacing) is refused."],"Display name",x?x.name:"",
 async()=>{const j=await api("/api/event/name",{event:ev,name:$("#mfield").value});closeModal();toast(j.result);await loadFacets()},null)};
$("#evren").onclick=()=>{const old=F.event;askField("Rename the event "+old+"?",["Every photo and video in "+old+" moves to the new name. Only the event changes: the files and your other tags stay as they are.","Use lowercase letters, digits and dashes, like summer-2026. To join an event that already exists, use Merge."],"New name",old,
 async()=>afterEvent(await api("/api/event/rename",{from:old,to:$("#mfield").value.trim()}),$("#mfield").value.trim()))};
$("#evmerge").onclick=()=>{const src=F.event;askField("Merge "+src+" into another event?",["Every photo and video in "+src+" moves to the event you name, and "+src+" is gone afterwards. The files and your other tags don't change."],"Merge into (an event that exists)","",
 async()=>afterEvent(await api("/api/event/merge",{from:[src],into:$("#mfield").value.trim()}),$("#mfield").value.trim()))};
// ---------------------------------------------------------------- filter state
function setF(k,v){if(v)F[k]=v;else delete F[k];simId=null;if(plugView()){setView("lib");return}syncUI();reset()}
function clearAll(){F={aimin:F.aimin||"0.7"};$("#q").value="";simId=null;syncUI();reset()}
function navActive(){if(view!=="lib")return view;const a=activeKeys();if(a.length===1&&!$("#q").value.trim()&&simId===null){if(F.event)return "ev";if(F.myplace)return "pl";if(F.added)return "recent";if(F.review==="favorite")return "fav";if(F.review==="child-in-shot")return "child";if(F.review==="untagged")return "untagged";if(F.dates)return "dates"}return "lib"}
function syncUI(){$$("button[data-k]").forEach(b=>b.setAttribute("aria-pressed",String((F[b.dataset.k]||"")===b.dataset.val)));
 $$(".fs").forEach(s=>{const k=SECKEY[s.dataset.s]||s.dataset.s;s.querySelector(".fs-v").textContent=F[k]?chipVal(k):""});
 const n=activeKeys().length;$("#clear").hidden=!n;$("#clear").textContent=`Clear filters (${n})`;$("#rbadge").hidden=!n;$("#rbadge").textContent=n;
 const jt=$('.opts[data-k=ai] .opt[data-val=trash]');if(jt&&facets){jt.querySelector(".ct").textContent=fmt(judgedAt(F.aimin));jt.title=AIV.trash+", "+WORDS.score+" "+(+F.aimin||0).toFixed(2)+" or more"}
 $("#aiconf").hidden=F.ai!=="trash";$("#aimin").value=F.aimin;$("#aiv").textContent=(+F.aimin).toFixed(2);
 $("#usedays").hidden=F.used!=="unused";if(document.activeElement!==$("#days"))$("#days").value=F.days||"";
 const C=$("#chips");C.replaceChildren();
 const chip=(text,fn)=>{const c=el("span","chip"),x=el("button","x","×");c.append(el("span",null,text));x.type="button";x.setAttribute("aria-label","Remove "+text);x.onclick=fn;c.append(x);C.append(c)};
 if(simId!==null)chip("Similar to "+simName,()=>{simId=null;syncUI();reset()});
 const q=$("#q").value.trim();if(q&&simId===null)chip("Search: "+q,()=>{$("#q").value="";syncUI();reset()});
 const na=navActive();activeKeys().forEach(k=>{if((k==="event"&&na==="ev")||(k==="myplace"&&na==="pl"))return;chip(NAMES[k]+": "+chipVal(k),()=>setF(k,""))});
 if(C.children.length>1){const a=el("button","link","Clear all");a.type="button";a.onclick=clearAll;C.append(a)}
 C.hidden=!C.children.length||plugView();
 $$(".nv[data-v]").forEach(b=>{if(b.dataset.v===na)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current")});
 $$(".nv[data-ev]").forEach(b=>{if(na==="ev"&&b.dataset.ev===F.event)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current")});
 $$(".nv[data-pl]").forEach(b=>{if(na==="pl"&&b.dataset.pl===F.myplace)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current")});
 $("#evbar").hidden=na!=="ev";$("#junkbar").hidden=view!=="sug";const evl=na==="ev"?evLabel(F.event):"";
 if(na==="ev"){$("#evname").textContent="Event: "+evl;$("#evslug").textContent=F.event;$("#evslug").hidden=evl===F.event}
 $("#vtitle").textContent=na==="ev"?evl:na==="pl"?plName(F.myplace):TITLES[na]||"";$("#vtitle").title=na==="ev"&&evl!==F.event?"Event: "+F.event:""}
$("#clear").onclick=clearAll;
// ---------------------------------------------------------------- listing
// the view's note above the grid; Suggested junk always says what the view is and that nothing changes until Apply
const SUGNOTE="Photos the tools suggest as junk (the vision check's verdicts, files tagged as junk, copies of photos you trashed), most likely first. Mark each Keep or Trash, then Apply. Nothing changes until you Apply.";
const whyWord=v=>WORDS.why[v]||v;
// Judged junk at or above a junk score (the Junk check slider): the count matches what the grid lists
const judgedAt=lo=>((facets&&facets.ai_scores)||[]).reduce((a,[sc,n])=>a+(sc>=(+lo||0)-1e-9?n:0),0);
// the bar above Suggested junk: Why with a count each, the Junk score floor, and how many more a lower score holds
function renderJunk(jk){const w=$("#jwhy"),o=(v,l)=>{const x=el("option",null,l);x.value=v;return x};
 const ws=jk.why.slice();if(J.why&&!ws.some(([v])=>v===J.why))ws.push([J.why,0]);
 w.replaceChildren(o("","All reasons ("+fmt(jk.all)+")"),...ws.map(([v,n])=>o(v,whyWord(v)+" ("+fmt(n)+")")));w.value=J.why;
 const m=$("#jmin");if(!m.options.length)m.replaceChildren(...JMINS.map(([v,l])=>o(v,l)));m.value=J.min;
 const b=$("#jlower");b.hidden=!(jk.lower>0);if(jk.lower>0)b.textContent="Show "+fmt(jk.lower)+" more with a lower junk score"}
$("#jwhy").onchange=()=>{J.why=$("#jwhy").value;reset()};
$("#jmin").onchange=()=>{J.min=$("#jmin").value;reset()};
$("#jlower").onclick=()=>{J.min="0";$("#jmin").value="0";reset()};
function plainNote(n){n=n||"";return /^Semantic search unavailable/.test(n)?"Semantic search is off: showing tag and file-name matches.":n}
function setNote(n){n=plainNote(n);$("#note").textContent=n;$("#note").hidden=!n}
/* The grid's own states, in #list: loading, an empty library, no results, a load error. Each says what is going on
   and what to do next (role=status; a load error role=alert). o: {lines, cmd (a terminal command), after, act} */
function listState(kind,head,o){o=o||{};const d=el("div","state"+(kind==="error"?" err":""));d.dataset.state=kind;d.setAttribute("role",kind==="error"?"alert":"status");
 if(head)d.append(el("h2",null,head));(o.lines||[]).forEach(l=>d.append(el("p",null,l)));if(o.cmd)d.append(el("pre",null,o.cmd));(o.after||[]).forEach(l=>d.append(el("p",null,l)));
 if(o.act){const b=el("button","btn",o.act.label);b.type="button";b.onclick=o.act.fn;d.append(b)}$("#list").replaceChildren(d);return d}
function loadingState(){const d=listState("loading"),p=el("p"),sp=el("span","spin");sp.setAttribute("aria-hidden","true");p.append(sp,"Loading your library…");d.append(p);
 $("#main").setAttribute("aria-busy","true")}
const QUICK=["fav","recent","child","untagged","dates","ev","pl"];
function emptyState(note){const q=$("#q").value.trim(),nf=activeKeys().length,na=navActive();
 if(simId!==null)return listState("none",note||"No photos look like this one.",{act:{label:"Back to the "+TITLES.lib.toLowerCase(),fn:()=>{simId=null;syncUI();reset()}}});
 if(facets&&facets.total===0)return listState("none","Your library is empty.",{lines:["To add photos and videos, run these in a terminal, with the folder they are in:"],
  cmd:"media-intake stage <folder> --kind folder --batch first\nmedia-intake plan first\nmedia-intake scan first\nmedia-intake apply first",
  after:["Then reload this page. Getting started (docs/getting-started.md) explains each step."]});
 if(view==="lib"&&na==="dates")return listState("none","No dates to check right now.",{lines:[facets&&facets.dates_checked?"The last check found nothing more to look at. To check the library again, run this in a terminal, then reload this page:":"To look for photos and videos with a wrong date, run this in a terminal, then reload this page:"],cmd:"media-intake check-dates",act:{label:"Show the whole "+TITLES.lib.toLowerCase(),fn:clearAll}});
 if(view==="lib"&&QUICK.includes(na))return listState("none","Nothing in "+(na==="ev"?evLabel(F.event):na==="pl"?plName(F.myplace):TITLES[na])+" right now.",{act:{label:"Show the whole "+TITLES.lib.toLowerCase(),fn:clearAll}});
 if(q||nf)return listState("none","Nothing matches "+(q&&nf?"this search and these filters":q?"this search":"these filters")+".",
  {act:{label:q&&nf?"Clear search and filters":q?"Clear search":"Clear filters",fn:clearAll}});
 if(view==="sug")return listState("none","No "+WORDS.view.sug.toLowerCase()+" right now.",{lines:["When the tools flag a photo as possible junk, it shows up here for you to mark Keep or Trash."]});
 return listState("none","Nothing to show here.")}
function retryLoad(){loadingState();loadFacets().then(()=>reset()).catch(loadError)}
function loadError(e){$("#main").removeAttribute("aria-busy");const why=e.message||"no answer from the gallery server";
 if(items.length){errorToast("Couldn't load more of the library: "+why+".",{label:"Try again",fn:()=>{failed=false;more()}});return}
 $("#count").textContent="";
 listState("error","Couldn't load the library",{lines:[why+".","Try again. If it keeps failing, restart media-intake gallery in the terminal and open the new link it prints."],act:{label:"Try again",fn:retryLoad}})}
let failed=false;  // the last page failed to load: no retry by itself (scrolling would loop), Try again does
function reset(){gen++;items=[];total=0;done=false;failed=false;cur=-1;last=null;openB={};loadingState();sel.clear();paintSel();more()}
async function more(){if(loading||done||failed||plugView())return;loading=true;const g=gen;let stale=false;
 try{const j=await api("/api/list?"+qs(Object.assign(filt(),{offset:items.length,limit:240})));
  if(g!==gen)stale=true;else{total=j.total;ranked=j.ranked;const start=items.length;items=items.concat(j.items);done=items.length>=total;
   if(!start)$("#list").replaceChildren();render(start);$("#main").removeAttribute("aria-busy");
   const n=plainNote(j.note);setNote(view==="sug"?SUGNOTE+(n?" "+n:""):n);if(!start&&j.junk)renderJunk(j.junk);
   $("#count").textContent=view==="sug"&&j.junk&&j.junk.unfiltered!==total?fmt(j.junk.unfiltered)+" suggested, "+fmt(total)+" shown with your filters"
    :fmt(total)+(total===1?" item":" items")+(view==="sug"?" suggested":"");
   if(!items.length){if(simId!==null)setNote("");emptyState(n)}
   if(!start&&sugFocus&&view==="sug"&&items.length){sugFocus=false;const a=document.activeElement;if(!a||a===document.body||a.closest(".nv"))$("#t"+items[0].id).focus({preventScroll:true})}
   document.body.dataset.ready="1"}}
 catch(e){if(g===gen){failed=true;loadError(e)}}finally{loading=false}
 if(stale){more();return}
 if(!done&&$("#sentinel").getBoundingClientRect().top<innerHeight+600)more()}
function groupKey(it){if(F.dates&&it.dq&&view==="lib")return it.dq;return ranked?(view==="sug"?"Most likely junk first":"Best matches first"):it.mo}
function render(start){const L=$("#list");let sec=L.lastElementChild,g=sec&&sec.querySelector(".grid"),gk=sec?sec.dataset.k:null;
 for(let n=start;n<items.length;n++){const it=items[n],k=groupKey(it);
  if(k!==gk){sec=el("section");sec.dataset.k=k;sec.append(el("h2","month",ranked?k:monthName(k)));g=el("div","grid");sec.append(g);L.append(sec);gk=k}
  g.append(tile(it,n))}$("#helpb").hidden=!items.some(i=>i.b!=null);allSel=null;paintSel();if(mq)mqFrame()}
/* Bursts (bursts.py, server side): the listed shots of one burst follow one another, the cover (it.bc) first.
   A closed stack shows only its cover; a shot of a closed stack is never selected, shown or stepped to. */
function hiddenAt(n){const it=items[n];return !!it&&it.b!=null&&!it.bc&&!openB[it.b]}
function stepVis(n,d){n+=d;while(n>=0&&n<items.length&&hiddenAt(n))n+=d;return n}
function stackLabel(it){return openB[it.b]?"Close stack (X)":`Show all ${it.bn} (X)`}
function bxLabel(it){return `Burst: ${it.bn} shots, ${openB[it.b]?"close":"open"} stack`}
// the compact cover button: "×N" on a closed stack, "Close" on an open one
function paintBx(bx,it){const o=!!openB[it.b];bx.textContent=o?"Close":"×"+it.bn;bx.classList.toggle("open",o);bx.setAttribute("aria-label",bxLabel(it))}
function toggleStack(n){const it=items[n];if(!it||it.b==null)return;const b=it.b;allSel=null;openB[b]=!openB[b];let c=-1,dropped=0;
 items.forEach((m,i)=>{if(m.b!==b)return;if(m.bc)c=i;const t=$("#t"+m.id);if(t){t.hidden=hiddenAt(i);t.classList.toggle("inb",!!openB[b])}if(hiddenAt(i)&&sel.has(m.id)){sel.delete(m.id);dropped++}});
 if(last!==null&&hiddenAt(last))last=null;if(cur>=0&&hiddenAt(cur))cur=c;
 const ct=c>=0?$("#t"+items[c].id):null;if(ct){ct.classList.toggle("stack",!openB[b]);const x=ct.querySelector(".x");if(x)x.textContent=stackLabel(items[c]);const bx=ct.querySelector(".bx");if(bx)paintBx(bx,items[c]);ct.focus({preventScroll:false})}
 if(dropped)toast(`Stack closed: ${dropped} hidden shot${dropped>1?"s":""} unselected (actions act on the cover only).`);paintSel()}
function tile(it,n){const t=el("div","tile"),sug=view==="sug";t.id="t"+it.id;t.dataset.n=n;t.dataset.item=it.n;t.tabIndex=0;t.title=it.n+(it.dr?"\n"+it.dr:"");
 const im=el("img");im.loading="lazy";im.decoding="async";im.alt=it.n;im.src="/t/"+it.id;im.onerror=()=>{im.replaceWith(el("div","ph",(it.m==="video"?"video: ":"")+it.n))};t.append(im);
 // the checkbox in a larger hit area (a tap beside the 22 px box still ticks it); none in Suggested junk, whose Keep / Trash marks are the only choice there
 if(!sug){const w=el("span","ckw"),cb=el("input","ck");cb.type="checkbox";cb.tabIndex=-1;cb.setAttribute("aria-label","select "+it.n);
  cb.onclick=ev=>{ev.stopPropagation();pick(n,ev.shiftKey)};w.onclick=ev=>{ev.stopPropagation();if(ev.target===w)pick(n,ev.shiftKey)};w.append(cb);t.append(w)}
 if(it.m==="video")t.append(el("span","play","▶"));
 if(it.b!=null){t.dataset.burst=it.b;if(openB[it.b])t.classList.add("inb");
  let pos=1;for(let i=n-1;i>=0&&items[i].b===it.b;i--)pos++;
  const bd=el("span","badge",it.bc?`Burst: ${it.bn} shots`:`Burst: shot ${pos} of ${it.bn}`);bd.dataset.count=it.bn;if(!it.bc)bd.dataset.pos=pos;t.append(bd);
  if(it.bc){const bx=el("button","bx");bx.type="button";bx.tabIndex=-1;paintBx(bx,it);bx.onclick=ev=>{ev.stopPropagation();toggleStack(n)};bd.before(bx);
   const x=el("button","x",stackLabel(it));x.type="button";x.tabIndex=-1;x.onclick=ev=>{ev.stopPropagation();toggleStack(n)};t.append(x);if(!openB[it.b])t.classList.add("stack")}
  else t.hidden=hiddenAt(n)}
 const fl=flagsOf(it);if(fl)t.append(el("span","flag",fl));
 // the tile's corner group, bottom right: the star, then Share (this one item without selecting it first)
 if(!sug){t.append(starBtn(it));const sh=el("button","tshare");sh.type="button";sh.tabIndex=-1;sh.title=SHARE_TIP;sh.setAttribute("aria-label","Share "+it.n);
  sh.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-share"/></svg>');sh.onclick=ev=>{ev.stopPropagation();askShare([it.id])};t.append(sh)}
 // Suggested junk: Keep and Trash on the tile (a tap marks it, a second tap unmarks; nothing changes until Apply)
 if(sug){const d=el("div","dec");d.setAttribute("role","group");d.setAttribute("aria-label","Mark "+it.n);
  [["keep","Keep"],["trash","Trash"]].forEach(([k,l])=>{const b=el("button","btn "+k,l);b.type="button";b.dataset.dec=k;b.setAttribute("aria-pressed","false");
   b.onclick=ev=>{ev.stopPropagation();ev.shiftKey?markRange(n,k):mark(n,k)};d.append(b)});t.append(d);
  t.onpointerenter=()=>{hov=n};t.onpointerleave=()=>{if(hov===n)hov=-1}}
 // a click or tap opens the large view (Shift / Ctrl / Cmd+click selects, not in Suggested junk)
 t.onclick=ev=>{if(!sug&&(ev.shiftKey||ev.ctrlKey||ev.metaKey)){pick(n,ev.shiftKey);return}openViewer(n)};
 t.onfocus=()=>{if(view==="sug"&&cur!==n){cur=n;paintSel()}};
 // Enter opens, Space selects (on the focused tile, never while the large view is open: Space plays / pauses there)
 t.onkeydown=ev=>{if(vi>=0||ev.target!==t)return;if(ev.key==="Enter"){ev.preventDefault();ev.stopPropagation();openViewer(n)}else if(ev.key===" "&&!sug){ev.preventDefault();ev.stopPropagation();pick(n,ev.shiftKey)}
  else if((ev.key==="x"||ev.key==="X")&&it.b!=null&&!ev.ctrlKey&&!ev.metaKey&&!ev.altKey){ev.preventDefault();ev.stopPropagation();toggleStack(n)}};return t}
function mark(n,k){const id=items[n].id;if(dec[id]===k)delete dec[id];else{dec[id]=k;lastBy[k]=id}cur=n;paintSel()}
/* Marking many at once (Suggested junk; still nothing changes until Apply, which asks): a Shift-click on a tile's
   Keep or Trash (or Shift+K / Shift+T) marks every tile from the last one given that mark; a drag from a mark marks
   every tile its rectangle touches; Mark all shown as Trash every tile loaded. A tile marked the other way keeps its
   mark (change it on the tile), except the one pressed. Marks are kept by item through a reload of the grid or a
   trip to another view; only the tiles listed show them and only those reach Apply. */
const MARKW={keep:"Keep",trash:"Trash"};let lastBy={},hov=-1,sugFocus=false;
function bulkMark(ns,k,own){let did=0,kept=0;ns.forEach(i=>{const id=items[i].id;if(dec[id]&&dec[id]!==k&&i!==own){kept++;return}if(dec[id]!==k)did++;dec[id]=k});return {did,kept}}
function markWords(k,r){return `Marked ${fmt(r.did)} more ${MARKW[k]}`+(r.kept?`; ${fmt(r.kept)} marked ${MARKW[k==="keep"?"trash":"keep"]} kept that mark`:"")+". Nothing changes until Apply."}
function markRange(n,k){const a=items.findIndex(i=>i.id===lastBy[k]);const ns=[];
 if(a<0)ns.push(n);else for(let i=Math.min(a,n);i<=Math.max(a,n);i++)if(!hiddenAt(i))ns.push(i);
 const r=bulkMark(ns,k,n);lastBy[k]=items[n].id;cur=n;paintSel();say(markWords(k,r))}
function markAll(){const ns=[];items.forEach((_,i)=>{if(!hiddenAt(i))ns.push(i)});const r=bulkMark(ns,"trash",-1),left=total-items.length;paintSel();
 toast(`Marked ${fmt(r.did)} more Trash (all shown`+(left>0?`; ${fmt(left)} more match below: scroll to load them, then Mark all again`:"")+")"
  +(r.kept?`; ${fmt(r.kept)} marked Keep kept that mark`:"")+". Nothing changes until Apply.")}
function clearMarks(){const n=Object.keys(dec).length;if(!n)return;
 confirmBox(`Clear ${plural(n,"mark")}?`,["Every Keep and Trash mark comes off. Nothing has been changed yet, so only the marks are lost."],false,
  ()=>{dec={};lastBy={};closeModal();paintSel();toast("Marks cleared.")},"Clear marks")}
function say(s){$("#selsay").textContent=s}
// marks the page would lose on a reload or close: the browser asks first
addEventListener("beforeunload",ev=>{if(Object.keys(dec).length){ev.preventDefault();ev.returnValue=""}});
function flagsOf(it){const fl=[];if(it.dq)fl.push(it.ds?"Suggested "+it.ds:"No suggested date");if(it.c!=null)fl.push(WORDS.score+" "+it.c.toFixed(2)+(it.r?" ("+whyWord(it.r).toLowerCase()+")":""));fl.push(...it.f.map(f=>WORDS.flag[f]||f));return fl.join(" · ")}
// after an edit: the reply's summaries replace those items and their tiles' flags (kept, ...);
// the tile, its focus and the selection stay
function patchTiles(fresh){(fresh||[]).forEach(s=>{const n=items.findIndex(i=>i.id===s.id);if(n<0)return;const o=items[n];items[n]=o.b!=null?Object.assign(s,{b:o.b,bn:o.bn,bc:o.bc,mo:o.mo}):s;const t=$("#t"+s.id);if(!t)return;
  const old=t.querySelector(".flag"),fl=flagsOf(s);if(old)old.remove();if(fl)t.append(el("span","flag",fl));paintStar(t.querySelector(".star"),s)})}
/* Favorites (xmp:Rating 5 in the sidecar): the tile's star toggles one item, F and the bar's Favorite / Unfavorite
   the selection; F stars them all unless every one already is a favorite, then it unstars them */
function paintStar(b,it){if(!b)return;b.setAttribute("aria-pressed",String(!!it.fav));b.textContent=it.fav?"★":"☆";
 b.title=it.fav?"Favorite (F): click to unstar":"Add to "+WORDS.view.fav+" (F)"}
function starBtn(it){const b=el("button","star");b.type="button";b.tabIndex=-1;b.setAttribute("aria-label","Favorite "+it.n);paintStar(b,it);
 b.onclick=ev=>{ev.stopPropagation();const cur=items.find(i=>i.id===it.id)||it;doFav([it.id],!cur.fav)};return b}
const isFav=id=>{const it=items.find(i=>i.id===id);return !!(it&&it.fav)};
function favToggle(ids){if(ids.length)doFav(ids,!ids.every(isFav))}
// a star takes the item's pending Keep / Trash mark off (a favorite is never junk); unstarring doesn't bring it back
async function doFav(ids,on){try{const j=await api("/api/favorite",{ids,on});if(on)ids.forEach(id=>delete dec[id]);toast(j.result,j.undo);refreshAfter(ids,false,j.items);loadFacets()}catch(e){refused(on?"Favorite":"Unfavorite",e)}}
function pick(n,range){if(view==="sug")return;allSel=null;if(range&&last!==null){const [a,b]=[Math.min(last,n),Math.max(last,n)];for(let i=a;i<=b;i++)if(!hiddenAt(i))sel.add(items[i].id)}
 else{const id=items[n].id;sel.has(id)?sel.delete(id):sel.add(id)}last=n;paintSel()}
function paintSel(){const sugOn=view==="sug";$$(".tile").forEach(t=>{const id=+t.id.slice(1),n=+t.dataset.n,d=sugOn?dec[id]:undefined;t.classList.toggle("sel",sel.has(id));const cb=t.querySelector("input.ck");if(cb)cb.checked=sel.has(id);
  t.classList.toggle("cur",n===cur);t.classList.toggle("dk",d==="keep");t.classList.toggle("dt",d==="trash");
  // a mark in words too (top left), not by the outline's colour alone
  t.querySelectorAll("[data-dec]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.dec===d)));
  let m=t.querySelector(".mark");if(d){if(!m){m=el("span");t.append(m)}m.className="mark "+d;m.textContent=d==="keep"?"Keep":"Trash"}else if(m)m.remove()});
 const selOn=sel.size>0&&!plugView()&&view!=="sug";$("#selgrp").hidden=!selOn;$("#bfixdate").hidden=navActive()!=="dates";$("#suggrp").hidden=!sugOn;$("#actionbar").hidden=!(selOn||sugOn);
 document.body.classList.toggle("selecting",sel.size>0);
 $("#seln").textContent=allSel&&allSel.n===sel.size?allSel.text:fmt(sel.size)+" selected";$("#ball").hidden=!canSelectAll();
 // the counts (and Apply) are the listed tiles' marks; marks on items not listed now wait, and the line says so
 const mk=sugMarks(),nk=mk.keep.length,nt=mk.trash.length,away=Object.keys(dec).length-nk-nt;
 $("#sugn").textContent=`${nk} keep / ${nt} trash marked`+(away>0?` (${fmt(away)} more not listed now)`:"");$("#sugapply").disabled=!(nk+nt);
 $("#bmarkall").disabled=!items.length;$("#bclearm").disabled=!Object.keys(dec).length}
function sugMarks(){const m={keep:[],trash:[]};items.forEach((it,i)=>{const d=dec[it.id];if(d&&!hiddenAt(i))m[d].push(it.id)});return m}
function selIds(){return [...sel]}
/* Select all: every tile the grid shows now (the active view, filters and search; the pages loaded so far, since
   the grid loads 240 at a time as you scroll), and on a closed burst stack its cover only (hidden shots are never
   selected). Not in Trash suggestions (K / T and Apply there) or a plugin tab. Trash and Erase ask as always. */
function canSelectAll(){return view!=="sug"&&!plugView()&&items.length>0}
function selectAll(){if(!canSelectAll())return;let hid=0;items.forEach((it,i)=>{if(hiddenAt(i))hid++;else sel.add(it.id)});
 const left=total-items.length,n=sel.size;
 allSel={n,text:`Selected ${fmt(n)} (all shown`+(left>0?`; ${fmt(left)} more match below: scroll to load them, then Select all again`:"")
  +(hid?`; closed stacks: cover only`:"")+")"};paintSel()}
/* Drag to select, with a mouse or a pen (a finger's drag scrolls the page, as ever): a press on the grid's empty
   space or on a tile (not its checkbox or buttons) that moves MQ_MIN px draws a rectangle, and every tile shown that
   it touches is selected (with Shift, Ctrl or Cmd, added to the selection); a closed stack's hidden shots are never
   reached, so a stack gives its cover only. Less movement is a click, as before. In Suggested junk the press is on a
   tile's Keep or Trash and the rectangle marks instead (bulkMark's rule). Near the top or bottom edge the page
   scrolls; tiles a page load adds inside the rectangle join in. Esc, or the browser taking the gesture, puts the
   selection (or the marks) back as they were. */
const MQ_MIN=6,MQ_EDGE=48;let press=null,mq=null,mqSwallow=false;
function mqStartAt(ev){if(ev.button!==0||(ev.pointerType!=="mouse"&&ev.pointerType!=="pen")||vi>=0||isOn("#modal")||isOn("#keys")||plugView()||!items.length)return null;
 const tg=ev.target;if(!(tg instanceof Element))return null;
 if(view==="sug"){const b=tg.closest(".dec [data-dec]"),t=b&&b.closest(".tile");return t?{k:b.dataset.dec,n:+t.dataset.n}:null}
 if(tg.closest("button,input,label,a,select,textarea,video,.ckw"))return null;return {k:null,n:-1}}
$("#main").addEventListener("pointerdown",ev=>{const s=mqStartAt(ev);if(!s)return;
 press={id:ev.pointerId,x:ev.clientX,y:ev.clientY,k:s.k,n:s.n,add:ev.shiftKey||ev.ctrlKey||ev.metaKey}});
document.addEventListener("pointerdown",()=>{mqSwallow=false},true);
document.addEventListener("pointermove",ev=>{if(!press||ev.pointerId!==press.id)return;if(!(ev.buttons&1)){mqEnd(true);return}
 if(!mq){if(Math.hypot(ev.clientX-press.x,ev.clientY-press.y)<MQ_MIN)return;mqStart()}
 mq.cx=ev.clientX;mq.cy=ev.clientY;mqFrame()});
document.addEventListener("pointerup",ev=>{if(press&&ev.pointerId===press.id)mqEnd(true)});
document.addEventListener("pointercancel",ev=>{if(press&&ev.pointerId===press.id)mqEnd(false)});
// the click that ends a drag is the drag's, not a click on the tile under it (a keyboard's click, detail 0, is never taken)
document.addEventListener("click",ev=>{if(mqSwallow&&ev.detail>0){mqSwallow=false;ev.preventDefault();ev.stopPropagation()}},true);
$("#list").addEventListener("dragstart",ev=>ev.preventDefault());  // no native image drag from a tile
document.addEventListener("selectstart",ev=>{if(press)ev.preventDefault()});
function mqStart(){const p=press;try{$("#main").setPointerCapture(p.id)}catch(e){}
 const box=el("div",p.k||null);box.id="marq";box.setAttribute("aria-hidden","true");document.body.append(box);document.body.classList.add("marqing");
 const s=getSelection();if(s)s.removeAllRanges();
 mq={id:p.id,g:gen,k:p.k,n0:p.n,x0:p.x+scrollX,y0:p.y+scrollY,cx:p.x,cy:p.y,box,rects:null,len:-1,raf:0,hit:null,
  selB:new Set(sel),lastB:last,base:p.k?null:new Set(p.add?sel:[]),decB:Object.assign({},dec),lastByB:Object.assign({},lastBy),r:{did:0,kept:0}}}
function mqFrame(){if(mq&&!mq.raf)mq.raf=requestAnimationFrame(mqTick)}
// the tiles' boxes in page coordinates; the layout holds still during a drag except when a page of tiles loads
function mqRects(){if(mq.len!==items.length){mq.len=items.length;mq.rects=[];
  $$("#list .tile").forEach(t=>{const n=+t.dataset.n;if(t.hidden||hiddenAt(n))return;const r=t.getBoundingClientRect();mq.rects.push([n,r.left+scrollX,r.top+scrollY,r.right+scrollX,r.bottom+scrollY])})}
 return mq.rects}
function mqTick(){if(!mq)return;mq.raf=0;if(mq.g!==gen){mqEnd(false);return}
 // near the window's top (below the sticky header) or bottom edge: scroll, faster the further in
 const top=$("#top").getBoundingClientRect().bottom,up=top+MQ_EDGE-mq.cy,down=mq.cy-(innerHeight-MQ_EDGE);
 const edge=up>0?-Math.min(40,2+up/2):down>0?Math.min(40,2+down/2):0;if(edge)scrollBy(0,edge);
 const px=mq.cx+scrollX,py=mq.cy+scrollY,x1=Math.min(mq.x0,px),x2=Math.max(mq.x0,px),y1=Math.min(mq.y0,py),y2=Math.max(mq.y0,py);
 const b=mq.box.style,cl=(v,m)=>Math.max(0,Math.min(m,v));  // drawn inside the window only (no overflow)
 b.left=cl(x1-scrollX,innerWidth)+"px";b.top=cl(y1-scrollY,innerHeight)+"px";b.width=(cl(x2-scrollX,innerWidth)-cl(x1-scrollX,innerWidth))+"px";b.height=(cl(y2-scrollY,innerHeight)-cl(y1-scrollY,innerHeight))+"px";
 const hits=mqRects().filter(r=>r[1]<x2&&r[3]>x1&&r[2]<y2&&r[4]>y1).map(r=>r[0]),key=hits.join(",");
 if(key!==mq.hit){mq.hit=key;mqApply(hits)}
 if(edge)mq.raf=requestAnimationFrame(mqTick)}  // keep scrolling while the pointer stays at the edge
function mqApply(hits){if(mq.k){dec=Object.assign({},mq.decB);if(!hits.includes(mq.n0))hits.push(mq.n0);mq.r=bulkMark(hits,mq.k,mq.n0);
  const hi=Math.max(...hits);lastBy[mq.k]=items[hi].id;cur=hi}
 else{allSel=null;sel.clear();mq.base.forEach(id=>sel.add(id));hits.forEach(n=>sel.add(items[n].id));last=hits.length?Math.max(...hits):mq.lastB}
 paintSel()}
function mqEnd(ok){const m=mq;press=null;mq=null;if(!m)return;if(m.raf)cancelAnimationFrame(m.raf);m.box.remove();document.body.classList.remove("marqing");
 try{$("#main").releasePointerCapture(m.id)}catch(e){}mqSwallow=true;if(m.g!==gen)return;
 if(!ok){if(m.k){dec=m.decB;lastBy=m.lastByB}else{allSel=null;sel.clear();m.selB.forEach(id=>sel.add(id));last=m.lastB}paintSel();say(m.k?"Marking cancelled.":"Selection put back.");return}
 say(m.k?markWords(m.k,m.r):fmt(sel.size)+" selected")}
function nameOf(id){const it=items.find(i=>i.id===id);return it?it.n:"#"+id}
// ---------------------------------------------------------------- actions
/* Messages: the toast (role=status) fades; one with Undo stays 10 s, longer while the pointer or focus is on it.
   An error goes to #etoast (role=alert, read out at once) and stays until dismissed. No native dialogs. */
let tt=null,undoTok=null;
function toast(s,undo){const t=$("#toast");t.replaceChildren(el("span",null,s));undoTok=undo||null;
 if(undo){const b=el("button",null,"Undo");b.type="button";b.id="tundo";b.title="Undo (Ctrl+Z)";b.setAttribute("aria-keyshortcuts","Control+Z Meta+Z");b.onclick=doUndo;t.append(b)}
 placeToast();t.classList.add("on");clearTimeout(tt);tt=setTimeout(hideToast,undo?10000:4500)}
// the message sits above the bottom action bar (which wraps to several rows on a phone), never over its buttons
function placeToast(){const b=$("#actionbar");let y=24;if(vi<0&&!b.hidden){const r=b.getBoundingClientRect();if(r.height&&r.top<innerHeight)y=Math.max(y,innerHeight-r.top+12)}
 $("#toast").style.bottom=y+"px"}
new ResizeObserver(placeToast).observe($("#actionbar"));addEventListener("resize",placeToast);
addEventListener("scroll",()=>{if(isOn("#toast"))placeToast()},{passive:true});
function hideToast(){const t=$("#toast");if(t.matches(":hover,:focus-within")){tt=setTimeout(hideToast,1500);return}
 t.classList.remove("on");undoTok=null;const b=$("#tundo");if(b)b.remove()}
// act: {label, fn}, a button before Dismiss (e.g. "Show progress" when a Trash, Erase or Share is running)
function errorToast(s,act){const t=$("#etoast"),x=el("button",null,"Dismiss"),off=()=>{t.classList.remove("on");t.replaceChildren()};x.type="button";x.setAttribute("aria-label","Dismiss error");
 x.onclick=off;t.replaceChildren(el("span",null,s));if(act){const a=el("button","act",act.label);a.type="button";a.onclick=()=>{off();act.fn()};t.append(a)}t.append(x);t.classList.add("on")}
/* A refusal: the server's own words. A 409 "busy" (the library lock or the gallery's job) already says in plain words
   what is running; while a gallery job runs, the alert offers its progress. */
async function refused(what,e){let act=null;if(e.busy){try{const j=await api("/api/job");if(!j.done)act={label:"Show progress",fn:attachJob}}catch(x){}}
 errorToast((what?what+" refused: ":"Refused: ")+e.message,act)}
/* Undo: the server recorded what Keep, a tag, Rotate or Set event changed and answers with a token; /api/undo puts
   exactly that back, under the library lock, and refuses (nothing written) when those items changed again since.
   Trash, Erase and Share never offer it. */
async function doUndo(){const tok=undoTok;if(!tok)return;undoTok=null;const b=$("#tundo");if(b)b.remove();
 try{const j=await api("/api/undo",{undo:tok});toast(j.result);loadFacets();if((j.kind==="event"||j.kind==="date")&&vi<0){patchTiles(j.items);reset()}else refreshAfter(j.ids,j.thumbs,j.items)}
 catch(e){refused("Undo",e)}}
async function doKeep(ids){try{const j=await api("/api/keep",{ids});toast(j.result,j.undo);refreshAfter(ids,false,j.items);loadFacets()}catch(e){refused("Keep",e)}}
/* In-page dialogs (the confirm / job dialog, the shortcut list): focus moves in, Tab stays in, Esc closes, and
   focus goes back to where it was. The large view is a dialog too (Tab stays in it, and its messages). */
let modalReturn=null,keysReturn=null;
const isOn=s=>$(s).classList.contains("on");
function refocus(r){if(r&&r.isConnected&&r.focus&&r.getClientRects().length)r.focus({preventScroll:true})}
function openModal(){if(!isOn("#modal"))modalReturn=document.activeElement;$("#modal").classList.add("on");paintPill()}
function closeModal(){if(!isOn("#modal"))return;$("#modal").classList.remove("on");paintPill();const r=modalReturn;modalReturn=null;refocus(r);
 const a=document.activeElement;if(a&&a.closest&&a.closest("#modal"))a.blur()}  // never left on a hidden field: the next key goes to the page
function openKeys(){if(isOn("#keys"))return;keysReturn=document.activeElement;$("#keys").classList.add("on");$("#kclose").focus()}
function closeKeys(){if(!isOn("#keys"))return;$("#keys").classList.remove("on");const r=keysReturn;keysReturn=null;refocus(r)}
function topDialog(){return isOn("#keys")?$("#keys"):isOn("#modal")?$("#modal"):vi>=0?$("#viewer"):null}
function trapTab(ev,d){const vis=e=>!e.disabled&&e.getClientRects().length>0;
 let f=[...d.querySelectorAll('button,input,select,textarea,video[controls],a[href],[tabindex]:not([tabindex="-1"])')].filter(vis);
 if(d.id==="viewer")f=f.concat([...$$("#toast button,#etoast button")].filter(vis));
 if(!f.length){ev.preventDefault();return}
 const a=f[0],z=f[f.length-1],at=document.activeElement,inside=f.includes(at);
 if(!inside){ev.preventDefault();(ev.shiftKey?z:a).focus()}else if(ev.shiftKey&&at===a){ev.preventDefault();z.focus()}else if(!ev.shiftKey&&at===z){ev.preventDefault();a.focus()}}
/* lines: text ("!..." a warning) or an element (the thumbnails); goLabel names the action ("Trash 2 files"), else Confirm */
function confirmBox(title,lines,needType,go,goLabel){$("#mh").textContent=title;$("#mgo").textContent=goLabel||"Confirm";$("#mno").textContent="Cancel";const b=$("#mb");b.replaceChildren();
 lines.forEach(l=>b.append(typeof l==="string"?el("p",l.startsWith("!")?"warn":null,l.replace(/^!/,"")):l));
 $("#mtype").hidden=!needType;$("#mfieldp").hidden=true;$("#mtyped").value="";$("#mgo").disabled=!!needType;$("#mgo").hidden=false;$("#mout").hidden=true;$("#mout").textContent="";$("#mstatus").hidden=true;delete $("#mstatus").dataset.state;delete $("#modal").dataset.job;openModal();
 $("#mtyped").oninput=()=>{$("#mgo").disabled=$("#mtyped").value!=="ERASE"};if(needType)$("#mtyped").focus();else $("#mno").focus();
 $("#mgo").onclick=async()=>{$("#mgo").disabled=true;try{await go()}catch(e){$("#mout").hidden=false;$("#mout").textContent="Refused: "+e.message;$("#mgo").disabled=false}}}
$("#mno").onclick=closeModal;
$("#kclose").onclick=closeKeys;$("#bkeys").onclick=openKeys;
$("#keys").onclick=ev=>{if(ev.target===$("#keys"))closeKeys()};  // a click on the backdrop
$("#mfield").onkeydown=ev=>{if(ev.key==="Enter"&&!$("#mgo").disabled){ev.preventDefault();$("#mgo").click()}};
function names(ids){const n=ids.map(nameOf);const p=el("p","mnames",n.slice(0,15).join(", ")+(n.length>15?` … and ${n.length-15} more`:""));return p}
// what the dialog acts on, as the grid's own thumbnails (/t/<id>): up to 12, then "and N more"
const THUMBS=12;
function thumbs(ids){const d=el("div","mthumbs");d.setAttribute("aria-hidden","true");ids.slice(0,THUMBS).forEach(id=>{const im=el("img");im.alt="";im.loading="lazy";im.src="/t/"+id;im.title=nameOf(id);im.onerror=()=>im.remove();d.append(im)});
 if(ids.length>THUMBS)d.append(el("span",null,`and ${fmt(ids.length-THUMBS)} more`));return d}
function warnings(ids){const its=ids.map(id=>items.find(i=>i.id===id)).filter(Boolean),w=[],one=ids.length===1;
 const u=its.filter(i=>i.f.includes("used")).length;if(u)w.push(one?"!This one was used in a post.":`!${fmt(u)} of these ${u===1?"was":"were"} used in a post.`);
 const k=its.filter(i=>i.f.includes("kept")).length;if(k)w.push(one?"!This one was marked Keep earlier.":`!${fmt(k)} of these ${k===1?"was":"were"} marked Keep earlier.`);
 const fv=its.filter(i=>i.fav).length;if(fv)w.push(one?"!This one is a favorite.":`!${fmt(fv)} of these ${fv===1?"is a favorite":"are favorites"}.`);return w}
const these=n=>n===1?"this file":"these "+plural(n,"file");
function trashText(n){return `Deletes ${these(n)} from this computer for good, with ${n===1?"its":"their"} tags and thumbnails. ${n===1?"It doesn't":"They don't"} go to the desktop Trash, and this can't be undone. If the same photo is imported again, it is skipped.`}
function askTrash(ids){const n=ids.length;confirmBox(`Trash ${plural(n,"item")}?`,[thumbs(ids),names(ids),trashText(n)+" Use Erase for anything private.",...warnings(ids)],false,
 ()=>runJob("/api/trash",{ids,confirm:true},ids),"Trash "+plural(n,"file"))}
// the backup drive is included only when the gallery was started with one (/api/facets "drive")
function askErase(ids){const n=ids.length,dr=facets&&facets.drive;confirmBox(`Erase ${plural(n,"item")} everywhere?`,[thumbs(ids),names(ids),
  `For private files. Erases every copy of ${these(n)}: in your library and the rest of your home folder, with ${n===1?"its":"their"} thumbnails and recent-file entries`
  +(dr?`, and on the backup drive (${dr}).`:". Not on a backup drive: the gallery was started without one.")+" This can't be undone.",...warnings(ids)],true,
 ()=>runJob("/api/erase",{ids,confirm:$("#mtyped").value},ids),"Erase "+plural(n,"file"))}
function askSetEvent(ids){if(!ids.length)return;askField("Set the event on "+plural(ids.length,"item")+"?",[names(ids),"Each one goes into this event and out of any other. The photos and your other tags don't change. Leave it empty to take them out of every event."],"Event name (lowercase letters, digits and dashes, like summer-2026)",F.event||"",
 async()=>{const ev=$("#mfield").value.trim(),j=await api("/api/event/set",{ids,event:ev});if(vi>=0){closeModal();toast(j.result,j.undo);loadFacets();openViewer(vi,{keep:true})}else afterEvent(j)})}
/* Fix date (Dates to check): check-dates' suggested date, a date you type, or a shift, on the selection. The server runs
   fix-dates' own code (sidecars only; files stay in their folders) and answers with an Undo token. */
function askFixDate(ids){if(!ids.length)return;const its=ids.map(id=>items.find(i=>i.id===id)).filter(Boolean),ns=its.filter(i=>i.ds).length,n=ids.length;
 const opt=(v,l,field)=>{const p=el("p","fixopt"),r=el("input"),lab=el("label",null,l);r.type="radio";r.name="fixmode";r.value=v;r.id="fix-"+v;lab.htmlFor=r.id;p.append(r,lab);
  if(field){const f=el("input");f.id="fixv-"+v;f.autocomplete="off";f.spellcheck=false;f.placeholder=field;f.setAttribute("aria-label",l+" ("+field+")");f.onfocus=()=>{r.checked=true};p.append(f)}return [p,r]};
 const [ps,rs]=opt("suggested",ns===n?"Use the suggested date":`Use the suggested date (${fmt(ns)} of ${fmt(n)} have one; the others stay as they are)`);rs.disabled=!ns;
 const [pd,rd]=opt("set","Set the date to","YYYY-MM-DD or YYYY-MM-DD HH:MM"),[ph]=opt("shift","Shift by","+8y, -3h30m, +2d");(ns?rs:rd).checked=true;
 const list=el("ul","fixlist");its.slice(0,10).forEach(i=>list.append(el("li",null,i.n+": "+(i.dd||i.d)+" → "+(i.ds?i.ds+" suggested":"no suggestion"))));if(its.length>10)list.append(el("li",null,`and ${fmt(its.length-10)} more`));
 confirmBox("Fix the date of "+plural(n,"item")+"?",[list,"The new date goes to each file's sidecar only: the photo or video itself and its folder don't change. A date alone keeps each file's time of day; a shift uses y (years), d (days), h (hours) and m (minutes). Undo puts it back.",ps,pd,ph],false,
  async()=>{const mode=($('input[name=fixmode]:checked')||rs).value,value=mode==="suggested"?null:$("#fixv-"+mode).value.trim();
   const j=await api("/api/dates/fix",{ids,mode,value});closeModal();toast(j.result,j.undo);sel.clear();await loadFacets();reset()},"Fix dates");
 $("#mno").focus()}
$("#bfixdate").onclick=()=>askFixDate(selIds());
/* Share: share-safe copies (intake.py share-copy as a job, one at a time with Trash and Erase) into a new folder; the
   page shows the folder and share-copy's own lines (each copy's path and "gps=none"), never the copies themselves.
   When the job this click started ends cleanly, the server opens that folder in the file manager (POST
   /api/share/open names the job, never a path), so the copies can be dragged into a chat or an email; the dialog and
   the message offer Open folder again, also on a page that re-attached (no automatic open there: not its click). */
function askShare(ids){if(!ids.length)return;confirmBox("Share "+plural(ids.length,"item")+"?",[thumbs(ids),names(ids),"Makes copies without location (GPS) or other hidden info, in a new folder under "+((facets&&facets.share_out)||"the share-safe folder")+", then opens that folder so you can drag them into a chat or an email. Your originals are not changed."],false,
 ()=>runShare(ids))}
async function runShare(ids){let j;try{j=await api("/api/share",{ids})}catch(e){if(e.busy&&await attachJob())return;throw e}
 jobDest=j.dest;startJob("share",ids);job.sid=j.job;job.auto=true}
const shareReady=()=>!!job&&job.kind==="share"&&job.state==="done"&&!!job.sid&&!/gps=STILL PRESENT/.test(job.log);
// Open folder: the button (in the job dialog and in the message), and the line saying what happened
function openBtn(id,cls){const b=el("button",cls||null,WORDS.openFolder);b.type="button";b.id=id;b.title="Open "+jobDest+" in the file manager";b.onclick=()=>openShare(false);return b}
function shareBits(){const b=$("#mb");if(!shareReady()||!jobInDialog()||$("#mopen"))return;const p=el("p","mopenp");p.append(openBtn("mopen","btn"));const m=el("span",null,"");m.id="mopened";m.setAttribute("role","status");p.append(m);b.append(p)}
// the message for a finished Share (the dialog hidden): what happened, and Open folder (it stays 10 s, and while hovered)
function shareToast(s){toast(s);$("#toast").append(openBtn("topen"));clearTimeout(tt);tt=setTimeout(hideToast,10000)}
async function openShare(auto){const me=job;if(!shareReady())return;const say=(s,bad)=>{const m=$("#mopened");if(m&&jobInDialog()&&job===me){m.textContent=s;m.classList.toggle("warn",!!bad)}else if(bad)errorToast(s);else shareToast(s)};
 try{const j=await api("/api/share/open",{job:me.sid});say("Opened "+j.dest+" in the file manager: drag the copies into a chat or an email.")}
 catch(e){say((auto&&!jobInDialog()?me.status+" ":"")+"Couldn't open the folder: "+e.message+(auto?" The copies are in "+jobDest+".":""),true)}}
/* Trash, Erase and Share run as a detached job (jobs.py): closing the page or stopping the server never
   stops them, and any page (a reload, a second tab, a restarted server) shows the running one from its status file.
   There is no stop (a half-done erase must finish), so the dialog's button says what it does: "Hide (it keeps
   running)", then "Close" once the job has ended. While it is hidden the header's pill brings it back, and its end
   is announced: a message (role=status) when it went well, an alert with Details when it did not. */
let job=null,jobDest="";
const SHARE_TIP="Share: make copies without location (GPS) or other hidden info, in a new folder you can send from. Your originals don't change.";
const JOBRUN={erase:"Erasing… looking for copies in your home folder. This can take a few minutes; you can close this page, it keeps running.",
 trash:"Trashing… you can close this page, it keeps running.",
 share:"Copying without location or hidden info… you can close this page, it keeps running."};
const jobWord=k=>WORDS.job[k]||"The action";
function jobKind(j){return j.kind||({forget:"erase","share-copy":"share"})[j.label]||j.label||""}
async function runJob(path,body,ids){jobDest="";try{await api(path,body)}catch(e){if(e.busy&&await attachJob())return;throw e}
 startJob(path==="/api/erase"?"erase":"trash",ids)}
function startJob(kind,ids,log){job={kind,ids:ids||[],log:log||"",state:"running",head:jobWord(kind)+" running",status:JOBRUN[kind]||""};showJob();poll(0)}
const jobInDialog=()=>isOn("#modal")&&$("#modal").dataset.job!==undefined;
// (re)open the dialog on the job: no confirm text, buttons or fields of the dialog that started it
function showJob(){if(!job)return;const b=$("#mb");b.replaceChildren();if(job.kind==="share"&&jobDest){const p=el("p",null,"Destination: "+jobDest);p.id="mdest";b.append(p)}
 $("#mgo").hidden=true;$("#mtype").hidden=true;$("#mfieldp").hidden=true;$("#mout").hidden=false;$("#mout").textContent="";$("#modal").dataset.job=job.state;openModal();paintJob();shareBits();$("#mno").focus()}
function paintJob(){paintPill();if(!job)return;const d=$("#modal");if(d.dataset.job!==undefined)d.dataset.job=job.state;  // also while hidden
 if(!jobInDialog())return;$("#mh").textContent=job.head;
 const o=$("#mout");if(o.textContent!==job.log){o.textContent=job.log;o.scrollTop=1e9}
 const m=$("#mstatus");if(m.textContent!==job.status)m.textContent=job.status;m.hidden=!job.status;m.dataset.state=job.state;
 $("#mno").textContent=job.state==="running"?"Hide (it keeps running)":"Close"}
function paintPill(){const p=$("#jobpill"),on=!!job&&job.state==="running"&&!jobInDialog();p.hidden=!on;document.body.classList.toggle("jobpill",on);
 if(on){const t=jobWord(job.kind)+" running…";const w=el("span",null,jobWord(job.kind));w.append(el("span","pr"," running"),"…");p.replaceChildren(w,el("b",null,"Show"));p.title=t+" Show its progress";p.setAttribute("aria-label",t+" Show progress")}}
$("#jobpill").onclick=showJob;
function stepText(j){return j.step&&j.total>1?` (step ${j.step} of ${j.total})`:""}
function jobEnd(j){const n=job.ids.length,k=job.kind;
 if(j.state==="crashed")return [`The ${jobWord(k).toLowerCase()} stopped unexpectedly`+stepText(j)+(j.step_running?"; the step it started is still running":"")+". Nothing more runs by itself.",
  jobWord(k)+" stopped unexpectedly. Nothing more runs by itself; see the details."];
 if(/gps=STILL PRESENT/.test(job.log))return ["Finished, but GPS is still in some copies (see below): don't share those","Share finished, but GPS is still in some copies: don't share those."];
 if(!j.ok)return ["Finished with problems (see below)",jobWord(k)+" finished with problems; see the details."];
 const what=k==="trash"?`${plural(n,"file")} trashed.`:k==="erase"?`${plural(n,"file")} erased everywhere.`:k==="share"&&jobDest?`share-safe copies in ${jobDest}.`:"";
 return ["Done"+(k==="share"&&jobDest?": share-safe copies in "+jobDest:""),"Done"+(what&&n?": "+what:".")]}
async function poll(since){const me=job;let j;try{j=await api("/api/job?since="+since)}catch(e){if(job!==me)return;
  job.log+="\n(server stopped; an action already started keeps going on its own)";job.state="unknown";job.status="The server stopped. The action keeps going on its own.";paintJob();return}
 if(job!==me)return;if(j.lines.length)job.log+=j.lines.join("\n")+"\n";const k=jobKind(j);if(k)job.kind=k;
 if(job.kind==="share"&&j.dest){jobDest=j.dest;job.sid=j.id}  // a Share this server started: its folder, by job
 if(!j.done){job.state="running";job.head=jobWord(job.kind)+" running"+stepText(j);job.status=JOBRUN[job.kind]||"Working… you can close this page, it keeps running.";paintJob();setTimeout(()=>poll(j.next),600);return}
 job.state=j.state==="crashed"?"crashed":j.ok?"done":"failed";[job.head,job.status]=jobEnd(j);
 const seen=jobInDialog();paintJob();shareBits();job.ids.forEach(id=>sel.delete(id));closeViewer();loadFacets();reset();
 const auto=shareReady()&&job.auto;job.auto=false;
 // a Share this click started opens its folder now (the message then says so); one this page re-attached to offers it
 if(!seen&&!auto){if(job.state!=="done")errorToast(job.status,{label:"Details",fn:showJob});else if(shareReady())shareToast(job.status);else toast(job.status)}
 if(auto)openShare(true)}
// a job this page didn't start (a reload, another tab, a restarted server): true when one is running and shown
async function attachJob(){if(job&&job.state==="running"){showJob();return true}let j;try{j=await api("/api/job")}catch(e){return false}if(j.done)return false;
 job={kind:jobKind(j),ids:j.ids||[],log:"",state:"running",head:"",status:""};job.head=jobWord(job.kind)+" running";showJob();poll(0);return true}
/* Your tags: the same refusals as the server (gallery.py tag(): TAG_RE, TOOL_PREFIXES), checked first so the field
   says why at once; the server checks again. Suggestions: every tag of yours in the library (#mytags). */
const TOOL_NS=["project|","source|","event|","place|","label|","review|","ai|","used|","owner|"];
function tagProblem(t){if(!t||[...t].length>64||/[\x00-\x1f\x7f]/.test(t)||t.startsWith("-")||t.includes("@@sep@@"))return "a tag is 1-64 characters on one line";
 if(TOOL_NS.some(p=>t.toLowerCase().startsWith(p)))return WORDS.tools+" tags belong to the tools; pick another name";return ""}
function tagAsk(op){const ids=vi>=0&&!sel.size?[items[vi].id]:selIds();if(!ids.length)return;const n=plural(ids.length,"item");
 askField(op==="add"?"Add a tag of yours to "+n+"?":"Remove a tag of yours from "+n+"?",[names(ids),"Your own tags only (not "+WORDS.tools.replace(" and "," or ")+", which belong to the tools). Use | for a group, like Trips|Spain. The photos themselves don't change."],
  "Tag (yours: suggestions as you type)","",async()=>{const t=$("#mfield").value.trim(),why=tagProblem(t);if(why)throw new Error(why);
   const j=await api("/api/tag",{ids,tag:t,op});closeModal();toast(j.result,j.undo);loadFacets();refreshAfter(ids,false,j.items)},"mytags")}
async function rot(ids,dir){try{const j=await api("/api/rotate",{ids,dir});toast(j.result,j.undo);refreshAfter(ids,true,j.items)}catch(e){refused("Rotate",e)}}
function refreshAfter(ids,thumbs,fresh){patchTiles(fresh);if(thumbs)ids.forEach(id=>{const t=$("#t"+id);if(t){const im=t.querySelector("img");if(im)im.src="/t/"+id+"?r="+Date.now()}});
 if(vi>=0)openViewer(vi,{keep:true});else if(view==="sug")reset()}
$("#bkeep").onclick=()=>doKeep(selIds());$("#btrash").onclick=()=>askTrash(selIds());$("#berase").onclick=()=>askErase(selIds());
$("#bfav").onclick=()=>doFav(selIds(),true);$("#bunfav").onclick=()=>doFav(selIds(),false);
$("#btag").onclick=()=>tagAsk("add");$("#buntag").onclick=()=>tagAsk("remove");$("#brot").onclick=()=>rot(selIds(),"cw");
$("#bshare").title=SHARE_TIP;$("#ball").onclick=selectAll;$("#bevent").onclick=()=>askSetEvent(selIds());$("#bshare").onclick=()=>askShare(selIds());$("#bnone").onclick=()=>{sel.clear();paintSel()};
$("#bmarkall").onclick=markAll;$("#bclearm").onclick=clearMarks;
$("#sugapply").onclick=()=>{const {keep,trash}=sugMarks();
 const nk=keep.length,nt=trash.length;
 confirmBox(`Apply: ${nk} keep, ${nt} trash?`,[nk?`Keep ${plural(nk,"item")}: ${nk===1?"it":"they"} won't be suggested as junk again.`:"",...(nt?[thumbs(trash),names(trash),"Trash: "+trashText(nt)]:[])].filter(Boolean),false,
  async()=>{const j=await api("/api/decide",{keep,trash,confirm:true});keep.concat(trash).forEach(id=>delete dec[id]);
   // a file starred or kept since its mark is skipped by the server (j.trashed: what the job got)
   const sent=j.trashed||[];if(sent.length){jobDest="";startJob("trash",sent,j.result+"\n")}else{closeModal();toast(j.result);loadFacets();reset()}},nt?"Apply and trash "+plural(nt,"file"):"Apply")};
// ---------------------------------------------------------------- large view
function curVideo(){return $("#media video")}
function stopVideo(){const v=curVideo();if(v){v.pause();v.removeAttribute("src");v.load()}}  // releases the file
function fitMedia(){const m=$("#media>img,#media>video");if(!m)return;const r=$("#media").getBoundingClientRect(),q=+(m.dataset.rot||0)%180!==0;
 m.style.width=q?r.height+"px":"100%";m.style.height=q?r.width+"px":"100%";m.style.transform=`translate(-50%,-50%) rotate(${m.dataset.rot||0}deg)`}
addEventListener("resize",fitMedia);
// the browser can't show it: the thumbnail, large, and a button that opens the file in the desktop's own player
// (bare: what to say when there is no thumbnail either; without it, WHY stays)
function cantPlay(d,why,bare){stopVideo();const M=$("#media");M.replaceChildren();const box=el("div","nop"),im=el("img"),w=el("span",null,why);
 im.alt=d.name;im.src="/t/"+d.id;im.onerror=()=>{im.remove();if(bare)w.textContent=bare};
 const c=el("div","cant");c.append(w);const o=el("button",null,"Open in the system player");o.type="button";
 o.onclick=async()=>{try{toast((await api("/api/open",{ids:[d.id]})).result)}catch(e){errorToast("Refused: "+e.message)}};c.append(o);
 box.append(im,c);M.append(box);const u=$("#unmute");if(u)u.remove();dropConv()}
// an HEVC video's H.264 copy is being made: say so until it plays (it starts on the first seconds converted)
function dropConv(){const c=$("#conv");if(c)c.remove()}
function converting(v){if(v.readyState>=3||curVideo()!==v)return;dropConv();const c=el("div",null,"Converting this video for the browser. It starts in a few seconds.");c.id="conv";c.setAttribute("role","status");
 $("#stage").append(c);const end=()=>{if($("#conv")===c)c.remove()};["playing","canplay","error","emptied"].forEach(e=>v.addEventListener(e,end,{once:true}))}
function startVideo(v){const ok=()=>{const u=$("#unmute");if(u)u.remove()};
 v.play().then(ok).catch(()=>{if(curVideo()!==v)return;v.muted=true;v.play().then(()=>{if(curVideo()!==v||$("#unmute"))return;const u=el("button",null,"Unmute");u.id="unmute";u.type="button";
  u.onclick=()=>{v.muted=false;u.remove()};$("#stage").append(u)}).catch(()=>{})})}
async function openViewer(n,opt){opt=opt||{};if(n<0||n>=items.length)return;if(vi<0)lastFocus=document.activeElement;
 const v0=curVideo(),tKeep=opt.keep&&v0&&v0.dataset.id==items[n].id?v0.currentTime:null;stopVideo();vi=n;cur=n;
 const pend=api("/api/item/"+items[n].id);$("#viewer").classList.add("on");document.body.classList.add("noscroll");
 const M=$("#media");const u0=$("#unmute");if(u0)u0.remove();dropConv();
 // a video is made and started inside the click's handler (the user gesture), before the details arrive
 if(items[n].m==="video"){const v=el("video");v.controls=true;v.preload="auto";v.playsInline=true;v.autoplay=true;v.poster="/t/"+items[n].id;v.dataset.id=items[n].id;
  v.src="/m/"+items[n].id;M.replaceChildren(v);fitMedia();startVideo(v);if(tKeep)v.currentTime=tKeep}
 else M.replaceChildren();
 let d;try{d=await pend}catch(e){if(vi!==n)return;closeViewer();
  if(e.status===404){toast("That file is no longer in the library. The list will refresh.");loadFacets().catch(()=>{});reset()}else errorToast("Couldn't open it: "+e.message);return}
 if(vi!==n)return;
 const v=curVideo();
 if(d.video&&d.src&&v){v.dataset.rot=d.rot;fitMedia();
  const bad=()=>{if(curVideo()===v)cantPlay(d,d.conv==="waiting"?"Another video is being converted for this browser. Open this one again once that one plays, or open it in the system player.":"This browser can't play this video (HEVC, most likely).")};
  if(d.conv==="converting")converting(v);
  const nopic=()=>{if(curVideo()===v&&v.readyState>=1&&!v.videoWidth)bad()};
  v.addEventListener("error",bad);v.addEventListener("loadedmetadata",nopic);if(v.error)bad();else nopic()}
 else if(d.kind==="video")cantPlay(d,"No browser preview for this format.");
 else if(d.src){const im=el("img");im.alt=d.name;im.dataset.rot=d.rot;im.src=d.src;im.onerror=()=>cantPlay(d,d.heic?"This is a small preview: HEIC photos need setup.sh's venv (pillow-heif) to show full size.":"This is a small preview: this browser can't show the full picture.",d.heic?"No preview: HEIC photos need setup.sh's venv (pillow-heif).":"This browser can't show this picture.");M.replaceChildren(im);fitMedia()}
 else cantPlay(d,"No browser preview for this format.");
 $("#vprev").disabled=n<=0;$("#vnext").disabled=n>=items.length-1&&done;
 const s=$("#info");s.replaceChildren();
 const head=el("div","ihead"),x=el("button","icon");x.id="vclose";x.type="button";x.setAttribute("aria-label","Close (Esc)");x.title="Close (Esc)";x.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-x"/></svg>');x.onclick=closeViewer;
 head.append(el("h2",null,d.name),x);s.append(head);
 const vs=el("button","btn share");vs.id="vshare";vs.type="button";vs.title=SHARE_TIP;vs.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-share"/></svg>');vs.append("Share");vs.onclick=()=>askShare([d.id]);x.before(vs);
 const acts=el("div","acts"),mk=(box,t,f,c)=>{const b=el("button","btn"+(c?" "+c:""),t);b.type="button";b.onclick=f;box.append(b);return b};
 if(view==="sug")[["keep","Mark Keep (K)"],["trash","Mark Trash (T)"]].forEach(([k,l])=>{const b=mk(acts,l,()=>viewMark(k),k);b.setAttribute("aria-pressed",String(dec[d.id]===k))});
 else{mk(acts,"Keep (K)",()=>doKeep([d.id]),"keep");mk(acts,"Trash (T)",()=>askTrash([d.id]),"trash")}mk(acts,"Erase (E)",()=>askErase([d.id]),"erase");s.append(acts);
 {const fb=mk(acts,"★ Favorite (F)",()=>doFav([d.id],!d.fav),"favb");fb.setAttribute("aria-pressed",String(!!d.fav));fb.setAttribute("aria-keyshortcuts","F");fb.title=d.fav?"A favorite: press to unstar":"Add to "+WORDS.view.fav}
 const tools=el("div","tools");mk(tools,"Rotate left (Shift+R)",()=>rot([d.id],"ccw")).setAttribute("aria-keyshortcuts","Shift+R");mk(tools,"Rotate right (R)",()=>rot([d.id],"cw")).setAttribute("aria-keyshortcuts","R");
 mk(tools,"Set event…",()=>askSetEvent([d.id]));
 if(d.gps)mk(tools,"Add a place here…",()=>askPlace(d,n)).title="Name the spot where this was taken (Home, …)";
 mk(tools,"Find similar",()=>{simId=d.id;simName=d.name;closeViewer();$("#q").value="";syncUI();reset()}).title="Photos that look like this one";s.append(tools);
 const dl=el("dl","dt"),row=(k,v)=>{if(v){dl.append(el("dt",null,k),el("dd",null,v))}};
 row("Date",d.date);row("Place",d.place);row(WORDS.filter.myplace,(d.myplaces||[]).join(", "));row("Event",d.event);row("Project",d.project);row("Source",d.source);row("Folder",d.folder);row("Resolution",d.res);row("Size",d.size);
 if(d.ai!=null)row(WORDS.score,d.ai.toFixed(2));if(d.used.length)row("Used in",d.used.join(", "));if(d.kept)row("Your decision","Marked Keep");
 const det=el("div");det.append(el("p","sh","Details"),dl);s.append(det);
 // "probably here": this has no location, but a shot from the same camera close in time was in one of your places
 (d.probably||[]).forEach(pl=>{const box=el("div","probably");box.setAttribute("role","group");box.setAttribute("aria-label","Probably at "+pl.name);
  box.append(el("p",null,"Probably at "+pl.name+": a photo from the same camera "+(pl.minutes?pl.minutes+" min apart":"at the same time")+" was there. This one has no location of its own."));
  const y=el("button","btn","Yes, "+pl.name),no=el("button","btn","No");y.type=no.type="button";y.onclick=()=>answerProbable(d,n,pl,true);no.onclick=()=>answerProbable(d,n,pl,false);box.append(y,no);s.append(box)});
 // your tags (removable) apart from the tools' (in plain words: plainTag; Owner| is the Details row's "Marked Keep")
 const tg=el("div"),tl=el("div","tags"),tt2=el("div","tags");tl.id="mytagl";tt2.id="tooltagl";
 d.mine.forEach(t=>{const c=el("span","tag");c.append(el("span",null,t));const b=el("button",null,"×");b.type="button";b.title="Remove my tag";b.setAttribute("aria-label","Remove my tag "+t);
  b.onclick=async()=>{try{const j=await api("/api/tag",{ids:[d.id],tag:t,op:"remove"});patchTiles(j.items);toast(j.result,j.undo);openViewer(n,{keep:true});loadFacets()}catch(e){refused("",e)}};c.append(b);tl.append(c)});
 if(!d.mine.length)tl.append(el("span","tag","none"));
 const auto=[...new Set(d.tags.filter(t=>!d.mine.includes(t)).map(plainTag).filter(Boolean))];auto.forEach(t=>tt2.append(el("span","tag",t)));
 tg.append(el("p","sh","Your tags"),tl);
 const add=el("input");add.placeholder="Add my tag, Enter";add.setAttribute("aria-label","Add my tag");add.setAttribute("list","mytags");add.autocomplete="off";
 add.onkeydown=async ev=>{if(ev.key==="Enter"&&add.value.trim()){ev.stopPropagation();const t=add.value.trim(),why=tagProblem(t);if(why){errorToast("Refused: "+why);return}
  try{const j=await api("/api/tag",{ids:[d.id],tag:t,op:"add"});patchTiles(j.items);toast(j.result,j.undo);loadFacets();openViewer(n,{keep:true})}catch(e){refused("",e)}}};
 tg.append(add);if(auto.length){const h=el("p","sh","Added by the tools");h.style.marginTop="14px";tg.append(h,tt2)}s.append(tg);
 s.append(el("p","fine","Rotate only changes how the photo is shown; the original file is never touched. To crop, make a copy in an editor."));
 if(!opt.keep)$("#viewer").focus({preventScroll:true});
 $("#viewer").dataset.current=d.name;  // last: the keys (Space, arrows) act on this item from here on
 if(n>=items.length-20)more()}
// Suggested junk's large view marks (nothing changes until Apply), then steps to the next item
function viewMark(k){if(vi<0)return;const id=items[vi].id;dec[id]=k;lastBy[k]=id;paintSel();toast(`Marked ${MARKW[k]}: ${items[vi].n}. Nothing changes until Apply.`);
 const nx=stepVis(vi,1);if(nx<items.length)openViewer(nx);else openViewer(vi,{keep:true})}
function closeViewer(){if(vi<0)return;const id=items[vi]&&items[vi].id;stopVideo();vi=-1;$("#viewer").scrollTop=0;$("#viewer").classList.remove("on");document.body.classList.remove("noscroll");$("#media").replaceChildren();
 delete $("#viewer").dataset.current;const u=$("#unmute");if(u)u.remove();dropConv();paintSel();const t=$("#t"+id)||lastFocus;if(t&&t.focus)t.focus({preventScroll:true})}
// swipe down on the photo or video closes the large view (phone); Esc and the close button still do
let swipe=null;
$("#stage").addEventListener("touchstart",ev=>{swipe=ev.touches.length===1&&$("#viewer").scrollTop<=0?[ev.touches[0].clientX,ev.touches[0].clientY]:null},{passive:true});
$("#stage").addEventListener("touchend",ev=>{if(!swipe||vi<0)return;const t=ev.changedTouches[0],dx=t.clientX-swipe[0],dy=t.clientY-swipe[1];swipe=null;if(dy>80&&Math.abs(dx)<dy/2)closeViewer()},{passive:true});
$("#vprev").onclick=()=>openViewer(stepVis(vi,-1));$("#vnext").onclick=()=>openViewer(stepVis(vi,1));
// ---------------------------------------------------------------- keys
document.addEventListener("keydown",ev=>{const tg=ev.target,modal=isOn("#modal");
 if(ev.key==="Tab"){const d=topDialog();if(d)trapTab(ev,d);return}
 if(mq&&ev.key==="Escape"){ev.preventDefault();mqEnd(false);return}
 // the shortcut list: Esc or ? closes it; no other page key acts while it is open
 if(isOn("#keys")){if(ev.key==="Escape"||ev.key==="?"){ev.preventDefault();closeKeys()}return}
 // a ticked checkbox (or a slider) keeps focus but takes no letters, so the keys still act on the selection
 const typing=(tg.tagName==="INPUT"&&!["checkbox","radio","range"].includes(tg.type))||tg.tagName==="SELECT"||tg.tagName==="TEXTAREA";
 // Ctrl+A / Cmd+A selects every tile shown; in a text field it stays the field's own select-all
 if((ev.key==="a"||ev.key==="A")&&(ev.ctrlKey||ev.metaKey)&&!ev.altKey&&!ev.shiftKey&&!typing&&!modal&&vi<0&&canSelectAll()){
  ev.preventDefault();selectAll();return}
 // Ctrl+Z / Cmd+Z: the Undo on the message shown (a text field keeps its own undo)
 if((ev.key==="z"||ev.key==="Z")&&(ev.ctrlKey||ev.metaKey)&&!ev.altKey&&!ev.shiftKey&&!typing&&!modal&&undoTok){ev.preventDefault();doUndo();return}
 if(typing||ev.ctrlKey||ev.metaKey||ev.altKey||modal){
  if(ev.key==="Escape"){if(modal)closeModal()
   else if(tg.id==="q"&&tg.value){tg.value="";syncUI();reset()}}
  return}
 const k=ev.key;
 if(k==="?"){ev.preventDefault();openKeys();return}
 if(vi>=0){const id=items[vi].id;
  if(k==="Escape")closeViewer();else if(k==="ArrowRight")openViewer(stepVis(vi,1));else if(k==="ArrowLeft")openViewer(stepVis(vi,-1));
  else if(k===" "){if(tg.tagName==="VIDEO"||tg.tagName==="BUTTON")return;const v=curVideo();if(!v)return;v.paused?v.play().catch(()=>{}):v.pause()}
  else if((k==="k"||k==="K"||k==="t"||k==="T")&&view==="sug")viewMark(k.toLowerCase()==="k"?"keep":"trash");
  else if(k==="k"||k==="K")doKeep([id]);else if(k==="t"||k==="T")askTrash([id]);else if(k==="e"||k==="E")askErase([id]);
  else if(k==="f"||k==="F")favToggle([id]);
  else if(k==="r")rot([id],"cw");else if(k==="R")rot([id],"ccw");else return;ev.preventDefault();return}
 if(k==="/"){ev.preventDefault();expandSidebar();$("#q").focus();return}
 if(view==="sug"){if((k==="Enter"||k===" ")&&tg.tagName==="BUTTON")return;if(k==="ArrowRight"||k==="ArrowDown"||k==="j"){cur=Math.min(items.length-1,cur+1)}else if(k==="ArrowLeft"||k==="ArrowUp"){cur=Math.max(0,cur-1)}
  else if(k==="k"||k==="K"||k==="t"||k==="T"){const m=k.toLowerCase()==="k"?"keep":"trash";
   // no outline yet: the tile under the pointer, else outline the first and say how to go on
   if(cur<0&&hov>=0)cur=hov;else if(cur<0){if(!items.length)return;cur=0;ev.preventDefault();paintSel();toast("The first tile is outlined: K or T marks it, the arrows move the outline.");$("#t"+items[0].id).scrollIntoView({block:"nearest"});return}
   if(ev.shiftKey)markRange(cur,m);else{dec[items[cur].id]=m;lastBy[m]=items[cur].id}cur=Math.min(items.length-1,cur+1)}
  else if(k==="Enter"&&cur>=0){openViewer(cur)}else return;ev.preventDefault();paintSel();const t=$("#t"+(items[cur]||{}).id);if(t)t.scrollIntoView({block:"nearest"});if(cur>=items.length-10)more();return}
 if(k==="Escape"&&drawer){drawer=false;layout();return}
 // F: the selection, or with none the focused tile
 if(k==="f"||k==="F"){const t=sel.size?null:tg.closest&&tg.closest(".tile");const ids=sel.size?selIds():t&&!t.hidden?[+t.id.slice(1)]:[];if(!ids.length)return;ev.preventDefault();favToggle(ids);return}
 if(!sel.size)return;if(k==="k"||k==="K")doKeep(selIds());else if(k==="t"||k==="T")askTrash(selIds());else if(k==="e"||k==="E")askErase(selIds());else if(k==="Escape"){sel.clear();paintSel()}else return;ev.preventDefault()});
// ---------------------------------------------------------------- views (plugin tabs go in the sidebar, after the built-in ones)
VIEWS.forEach(v=>{const b=el("button","nv");b.type="button";b.dataset.v="p-"+v.id;b.title=v.title;b.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-plug"/></svg>');const rl=el("span","rl",v.title);rl.setAttribute("aria-hidden","true");b.append(el("span","lbl",v.title),rl);$("#ptabs").append(b)});
function setView(v){view=v;simId=null;sugFocus=v==="sug";document.body.dataset.view=v;const plug=plugView();
 $("#list").hidden=plug;$("#sentinel").hidden=plug;$("#plugin").hidden=!plug;$("#filters").hidden=plug;$(".sb-search").hidden=plug;$("#tsizew").hidden=plug;
 if(plug){sel.clear();paintSel();$("#count").textContent="";setNote("");$("#helpb").hidden=true;syncUI();const id=v.slice(2),P=$("#plugin"),pv=window.mediaIntakeViews[id];P.replaceChildren();
  if(!pv||typeof pv.render!=="function"){P.append(el("p","warn","Plugin view "+id+" has no render()"));return}
  pv.render(P,{el,api:(p,b)=>api("/plugin/"+id+"/"+p,b),media:p=>"/plugin/"+id+"/media/"+p})}
 else{syncUI();reset()}}
$$(".nv[data-v]").forEach(b=>b.onclick=()=>{const v=b.dataset.v;
 if(v==="lib"){if(QUICK.includes(navActive()))F={aimin:F.aimin};setView("lib")}
 else if(v==="sug"||v.startsWith("p-")){if(v==="sug"&&view!=="sug")J={why:"",min:JLINE};setView(v)}
 else{F={aimin:F.aimin};if(v==="recent")F.added=String((facets&&facets.recent_days)||30);if(v==="child")F.review="child-in-shot";if(v==="untagged")F.review="untagged";if(v==="fav")F.review="favorite";if(v==="dates")F.dates="all";$("#q").value="";setView("lib")}
 if(mqPhone.matches&&drawer){drawer=false;layout()}});
let qt=null;$("#q").oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{simId=null;syncUI();reset()},500)};
$("#close").onclick=()=>{confirmBox("Stop the gallery server?",["The page stops working until you run media-intake gallery again. A Trash, Erase or Share that is already running keeps going on its own."],false,
 async()=>{try{await api("/api/close",{})}catch(e){}document.body.replaceChildren(el("p","empty big","Gallery stopped. You can close this tab."))});$("#mgo").textContent="Stop the server"};
new IntersectionObserver(es=>{if(es.some(e=>e.isIntersecting))more()},{rootMargin:"800px"}).observe($("#sentinel"));
// thumbnail size (a per-browser convenience)
let ts=+LS.get("tile",mqPhone.matches?"92":"168");if(!(ts>=80&&ts<=320))ts=168;$("#tsize").value=ts;document.documentElement.style.setProperty("--tile",ts+"px");
$("#tsize").oninput=()=>{document.documentElement.style.setProperty("--tile",$("#tsize").value+"px");LS.set("tile",$("#tsize").value)};
// ---------------------------------------------------------------- start
layout();buildSections();syncUI();
loadFacets().then(()=>reset()).then(attachJob).catch(loadError);
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
