#!/usr/bin/env python3
"""Local web gallery for the media library (a day-to-day browser, in place of digiKam).

  media-intake gallery [--drive MOUNT] [--port 0] [--share-out DIR]
  python3 -I media_intake/gallery.py         (same flags)

Shows everything under the library, videos and project folders (config.toml)
(never the media-intake state folder: quarantine, staging, review; never a file
scan flagged, quarantined or erased: label.flagged()). Timeline grid by month,
thumbnails made on demand (max 320 px JPEG, ~/.cache/media-intake/thumbs/<sha256>-r<turn>.jpg,
dir 0700, files 0600; intake.py erase/forget/trash delete them by sha and
verify-gone checks them), an H.264 proxy made on demand for a video the browser
can't play (HEVC: ~/.cache/media-intake/proxies/<sha256>.mp4, cleaned the same way),
filters, semantic search (semantic.py, local SigLIP),
and your actions: Keep, Trash (intake.py trash), Erase (intake.py forget, the
erase-everywhere path), my tags, Rotate (XMP-tiff:Orientation in the sidecar
only), Set event, all on one item or a selection, and Share-safe copy (intake.py
share-copy into a new folder under --share-out, default ~/Pictures/share-safe, as a
job). A left sidebar holds the views with counts (/api/facets): Library, Trash
suggestions, any tab a plugin adds (plugins.py gallery_views(), docs/plugins.md),
the quick views Recently added, Possible child, Untagged, and the Events (Event|
tags; rename and merge rewrite only those, in the sidecars that have them); then
search and the filters. Bursts (bursts.py over the plan entries intake wrote) show in
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
Keep, my tags, Rotate and Set event can be undone (/api/undo: the server records each sidecar's
fields before and after and puts back exactly that change, refusing once a sidecar changed
again); Trash, Erase and Share-safe copy can't. No native alert/confirm/prompt: in-page
dialogs, an error message that stays until dismissed, and a "?" list of every key.

Server security is review_ui.py's: stdlib only, everything inline, 127.0.0.1 on a
random port, random token swapped for an HttpOnly SameSite=Strict cookie, Host
check, CSRF value on every POST, no-store + strict CSP, media by opaque id only.
Originals are never written: sidecars (<file>.<ext>.xmp) only. docs/gallery.md.
"""
import argparse, collections, contextlib, datetime, hashlib, html, http.server, io, json, os, re, secrets, shutil, subprocess, sys, tempfile
import threading, time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bursts  # noqa: E402  (burst groups: pure functions over date, device, dHash, burst id)
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
PROXIES = intake.PROXIES
CACHE = os.path.dirname(THUMBS)
JOB_KEY = "gallery"  # one trash/erase at a time across every gallery page and server
IMG, VID = intake.IMG, intake.VID
THUMB_PX, PREVIEW_PX = 320, 1600
# Video codecs browsers can't play (Chromium on Linux, often Firefox): /m/ serves an H.264 proxy instead.
PROXY_CODECS = {"hevc"}
PROXY_PX = 1920  # the proxy's longer side at most (never scaled up)
PROXY_TIMEOUT = 3600  # one transcode, in seconds; past it the original is served as before
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
UNDO_KEEP = 50  # the last N undoable actions (Keep, my tags, Rotate, Set event) this server can still undo
# Share-safe export (intake.py share-copy --out): each export gets a new dated folder under this one.
SHARE_OUT = os.path.join(HOME, "Pictures", "share-safe")

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
    ("http://www.digikam.org/ns/1.0/", "TagsList"): "tagslist",
    ("http://ns.google.com/photos/1.0/camera/", "BurstID"): "burst_id",  # XMP-GCamera:BurstID, if a tool wrote it
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
    """{tags, subject, tagslist, city, state, country, date, orient, event, pick} from an XMP sidecar (a path, or
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
                pass  # malformed confidence in a sidecar: ignore that one tag
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
    it["xevent"] = x.get("event") or ""
    it["xdate"] = x.get("date") or ""  # with its seconds, for burst grouping (it["date"] keeps minutes)
    it["xburst"] = x.get("burst_id") or ""
    it["xplace"] = (x.get("city"), x.get("state"), x.get("country"))
    try:
        it["orient"] = int(x["orient"]) if x.get("orient") else None
    except ValueError:
        it["orient"] = None
    it["date"] = norm_date(x.get("date")) or guess_date(it["path"], it["root"], it["mtime"])
    facts(it)


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
def video_codec(path):
    """The first video stream's codec name (ffprobe: "h264", "hevc", ...), or None when it can't be read."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", "file:" + path], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    words = r.stdout.split() if r.returncode == 0 else []
    return words[0].strip(",").lower() if words else None


def ffmpeg_encoder(names):
    """The first of names that this ffmpeg can encode with, or None."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return next((n for n in names if f" {n} " in out), None)


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


def proxy_commands(src, work):
    """Commands that each write an H.264 MP4 of src to <work>/out.mp4, in the order to try them. ffmpeg first
    (where it has an HEVC decoder), src as a file: path; then GStreamer's GPU HEVC decoder (Fedora's ffmpeg-free
    has none) into an H.264 encoder, with the sound when it is AAC, then without. gst-launch-1.0 re-parses its
    arguments as one pipeline, so a file name with "!", quotes or spaces could add elements: it reads
    <work>/in, a link to src that make_proxy makes, and both of its paths are checked against SAFE_GST_PATH
    (else GStreamer is not tried, said once per folder on stderr). The argv is built as a list, never split
    from a string. Nothing is fetched."""
    cmds, out, link = [], os.path.join(work, "out.mp4"), os.path.join(work, "in")
    enc = ffmpeg_encoder(("libx264", "libopenh264"))
    if enc:
        q = ["-preset", "veryfast", "-crf", "23"] if enc == "libx264" else ["-b:v", "6M"]
        vf = (f"scale='min({PROXY_PX},iw)':'min({PROXY_PX},ih)':force_original_aspect_ratio=decrease:"
              "force_divisible_by=2,format=yuv420p")
        cmds.append(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "file:" + src, "-map", "0:v:0", "-map", "0:a:0?",
                     "-map_metadata", "-1", "-vf", vf, "-c:v", enc, *q, "-c:a", "aac", "-b:a", "160k",
                     "-movflags", "+faststart", "-f", "mp4", "file:" + out])
    import scan_sensitive  # the GPU HEVC decoder thumbnails and scans use
    dec = scan_sensitive._gst_hevc_decoder()
    if dec and not SAFE_GST_PATH.fullmatch(work):
        scan_sensitive.note_gst_skipped(os.path.dirname(work), "HEVC clips get no GStreamer proxy")
        return cmds
    genc = gst_element(("x264enc", "openh264enc", "vah264enc")) if dec else None
    if dec and genc:
        post = ["vapostproc"] if dec == "vah265dec" else ["vulkandownload"]
        video = ["filesrc", "location=" + link, "!", "qtdemux", "name=d", "d.video_0", "!", "queue", "!", "h265parse",
                 "!", dec, "!", *post, "!", "videoconvert", "!", genc, "!", "h264parse", "!", "mp4mux", "name=mux",
                 "!", "filesink", "location=" + out]
        for sound in (["d.audio_0", "!", "queue", "!", "aacparse", "!", "mux."], []):
            cmds.append(["gst-launch-1.0", "-q", *video, *sound])
    return cmds


def make_proxy(src, dst):
    """Write an H.264 MP4 of src to dst (0600, in the 0700 cache), trying each of proxy_commands in turn, in a
    private work folder beside dst (the output, and the link GStreamer reads src through). True once one gave a
    file ffprobe reads as H.264; False, with nothing left behind, when none did. The original is only read."""
    work = tempfile.mkdtemp(prefix=".work-", dir=os.path.dirname(dst))  # 0700
    out = os.path.join(work, "out.mp4")
    try:
        os.symlink(os.path.abspath(src), os.path.join(work, "in"))
        for argv in proxy_commands(src, work):
            with contextlib.suppress(FileNotFoundError):
                os.remove(out)
            os.close(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))  # the tools keep its mode
            try:
                r = subprocess.run(argv, capture_output=True, timeout=PROXY_TIMEOUT, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and os.path.getsize(out) and video_codec(out) == "h264":
                os.chmod(out, 0o600)
                os.replace(out, dst)
                return True
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- app
class LockBusy(RuntimeError):
    """App.writing() could not take the library lock (an HTTP 409; its message names the holder)."""


class Changed(RuntimeError):
    """Undo refused: an item's sidecar changed since the action (another action, the CLI, digiKam); an HTTP 409."""


def sidecar_state(path):
    """(sha256 of the sidecar's bytes or None when it doesn't exist, the fields an undoable action writes): read
    fresh from the file, so a sidecar some other tool changed since the gallery's scan is seen as it is now."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None, dict(tags=frozenset(), subject=frozenset(), orient=None, xevent="")
    x = read_xmp(io.BytesIO(data))
    return hashlib.sha256(data).hexdigest(), dict(
        tags=frozenset(t for t in x.get("tags", []) if t), subject=frozenset(t for t in x.get("subject", []) if t),
        orient=(x.get("orient") or "").strip() or None, xevent=(x.get("event") or "").strip())


# What each undoable action may change in a sidecar, so an undo can only reverse that (see App.undo): Keep its
# Owner| / Review| tags, my tags only tags of yours, Set event only Event| tags, Rotate no tag at all.
KEEP_TAGS = frozenset(("Review|maybe-trash", owner.TRASH, *owner.LEGACY, owner.KEEP, owner.NOT_TRASH))
UNDO_SCOPE = {
    "keep": dict(tags=lambda t: t in KEEP_TAGS, orient=False, xevent=False),
    "tag": dict(tags=owner_tag, orient=False, xevent=False),
    "event": dict(tags=lambda t: t.startswith("Event|"), orient=False, xevent=True),
    "rotate": dict(tags=lambda t: False, orient=True, xevent=False),
}


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
        self.proxy_lock = threading.Lock()  # guards proxy_making
        self.proxy_making = {}  # sha -> Event set when its transcode ends; one transcode at a time (CPU)
        self.port = None
        self.reconciled = None  # id of the last finished job whose results are applied to items
        self.tail = None  # the shown job's log, through an open descriptor (jobs.Tail)
        self.search_cache = {}
        self.undos = collections.OrderedDict()  # token -> what an undoable action changed (App.journal)
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

    def playable(self, it):
        """(path, mime) that /m/ serves: the original, or for a video whose codec browsers can't play (HEVC) an
        H.264 proxy, made on demand and cached by sha256 in PROXIES. When no proxy can be made the original is
        served as before (the page then says the browser can't play it) and this item isn't tried again.

        One transcode at a time, for the CPU. A request for the sha being converted (the player's Range
        requests, a second open) waits for that one transcode and then serves its proxy; a request for another
        video while one is being made is not held for up to PROXY_TIMEOUT: it gets the original now, and a later
        open, once the conversion is done, makes that video's proxy."""
        orig = (it["path"], it["mime"])
        if it["media"] != "video" or it.get("noproxy"):
            return orig
        if "vcodec" not in it:
            it["vcodec"] = video_codec(it["path"])
        if it["vcodec"] not in PROXY_CODECS:
            return orig
        sha = self.sha_of(it)
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return orig
        dst = os.path.join(PROXIES, sha + ".mp4")
        if not os.path.isfile(dst):
            with self.proxy_lock:
                done = self.proxy_making.get(sha)
                mine = done is None and not self.proxy_making and not os.path.isfile(dst)
                if mine:
                    done = self.proxy_making[sha] = threading.Event()
            if mine:
                try:
                    private_cache()
                    made = make_proxy(it["path"], dst)
                except OSError:
                    made = False
                finally:
                    with self.proxy_lock:
                        del self.proxy_making[sha]
                    done.set()
                if not made:
                    it["noproxy"] = True
                    return orig
            elif done is not None:
                done.wait()  # the same video's transcode: wait for it, then serve what it made
            if not os.path.isfile(dst):
                return orig  # another video's transcode is running, or this one's failed
            if it.get("busy") or it["gone"] or not os.path.lexists(it["path"]):
                intake.gallery_thumb_forget(sha)  # trashed or erased while it was being made: no copy stays
                return orig
        return dst, "video/mp4"

    def drop_thumb(self, it):
        """After a rotate: the item's new turn has its own cache key, so its next /t/ request shows it at once.
        The other variants stay (a same-content copy may still show them; trash, forget and erase remove all,
        and `media-intake prune-cache` removes the turns no copy shows any more)."""
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
        return dict(total=n, facets=out, counts=dict(counts), recent_days=RECENT_DAYS, search=search, share_out=share)

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
        for k in ("project", "source", "place"):
            if f.get(k) and it[k] != f[k]:
                return False
        if f.get("event") and f["event"] not in self.events_of(it):
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
            info = {}
            if not ranked:
                ids, info = self.stacked(ids)
            page = []
            for i in ids[off:off + lim]:
                row = self.summary(i, self.items[i])
                row.update(info.get(i, {}))
                page.append(row)
        return dict(total=len(ids), items=page, note=note, ranked=ranked)

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
            return self.journal("keep", before,
                                f"kept {len(ids)}: Owner|keep + Owner|not-trash, Review|maybe-trash and Owner|trash removed")

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
            raise ValueError("Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used| and Owner| belong to the "
                             "tools; pick another name")
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

    # ---------------------------------------------- events (Event|<slug> tags; sidecars only)
    def events_of(self, it):
        """Every event an item is in: its Event| tags, and XMP-iptcExt:Event when it has no Event| tag."""
        ev = [t[6:] for t in it["tags"] if t.startswith("Event|")]
        return ev or ([it["xevent"]] if it.get("xevent") else [])

    def event_names(self):
        return {e for it in self.items if not it["gone"] for e in self.events_of(it)}

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
            blocks = [b for b in (self.event_block(self.items[i], event, None) for i in ids) if b]
            self.run_exiftool(blocks)
            self.refresh(ids)
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
            return self._move_events({old}, new, f"renamed {old!r} to {new!r}")

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
            return self._move_events(sources, into, f"merged {', '.join(sorted(map(repr, sources)))} into {into!r}")

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

    # ---------------------------------------------- undo (Keep, my tags, Rotate, Set event; never Trash or Erase)
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
        if not all(scope["tags"](t) for t in drop | add) or (old["orient"] != new["orient"] and not scope["orient"]) \
                or (old["xevent"] != new["xevent"] and not scope["xevent"]):
            raise ValueError(f"that undo would write outside what {kind} writes; nothing was changed")
        back = set(drop | add | (new["subject"] ^ old["subject"]))
        if old["xevent"] != new["xevent"]:
            back.add(old["xevent"])
        for v in back - {""}:
            if "@@sep@@" in v or v.startswith("-") or not TAG_RE.match(v):
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
            self.run_exiftool(blocks)
            self.refresh(ids)
            if kind == "rotate":
                for i in ids:
                    self.drop_thumb(self.items[i])
            what = {"keep": "Keep", "tag": "the tag", "rotate": "Rotate", "event": "Set event"}[kind]
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
            self.start_job("share", [["share-copy", *paths, "--out", out]], ids, hold=False)
        except Exception:
            with contextlib.suppress(OSError):  # nothing was started: drop the empty folder (kept if not empty)
                os.rmdir(out)
            raise
        shown = "~/" + os.path.relpath(out, HOME) if out.startswith(HOME + os.sep) else out
        return dict(result=f"making {len(ids)} share-safe copies in {shown}", dest=shown)

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
            r = self.keep(keep)
            msg.append(r["result"] if isinstance(r, dict) else r)  # Apply offers no Undo (it may trash too)
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


# Sidecar edits on a selection whose reply carries the items' new grid summaries ("items").
REDRAW = ("/api/keep", "/api/tag", "/api/rotate", "/api/event/set")


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
            with app.lock:
                self.json(200, app.detail(i))
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
            "/api/tag": lambda: app.tag(body.get("ids"), body.get("tag"), body.get("op")),
            "/api/rotate": lambda: app.rotate(body.get("ids"), body.get("dir")),
            "/api/trash": lambda: app.trash(body.get("ids"), body.get("confirm")),
            "/api/erase": lambda: app.erase(body.get("ids"), body.get("confirm")),
            "/api/open": lambda: app.open_external(body.get("ids")),
            "/api/share": lambda: app.share(body.get("ids")),
            "/api/event/set": lambda: app.set_event(body.get("ids"), body.get("event")),
            "/api/event/rename": lambda: app.rename_event(body.get("from"), body.get("to")),
            "/api/event/merge": lambda: app.merge_events(body.get("from"), body.get("into")),
            "/api/decide": lambda: app.decide(body.get("keep") or [], body.get("trash") or [], body.get("confirm")),
            "/api/undo": lambda: app.undo(body.get("undo")),
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
                self.json(409, {"error": str(e)})
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
        except RuntimeError as e:
            self.json(409, {"error": str(e)})
            return
        out = msg if isinstance(msg, dict) else {"result": msg}
        if path in REDRAW and isinstance(body.get("ids"), list):
            out["items"] = app.summaries(body["ids"])  # the page redraws those tiles' flags (kept, ...) in place
        elif path == "/api/undo":
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
    ap = argparse.ArgumentParser(description="Local web gallery for the media library.")
    ap.add_argument("--drive", help="backup drive mount, passed to forget (Erase)")
    ap.add_argument("--port", type=int, default=0, help="0 = a random free port (default)")
    ap.add_argument("--share-out", help=f"folder for share-safe copies, one dated subfolder per export "
                                        f"(default {SHARE_OUT}); never inside the library")
    a = ap.parse_args(argv)
    if a.drive and not os.path.isdir(a.drive):
        sys.exit(f"--drive {a.drive}: not a directory")
    t0 = time.monotonic()
    jobs.sweep()  # a trash/erase that died while no page watched: marked crashed, an erase's output scrubbed
    items, skipped = scan_library()
    views = view_list([v for vs in plugins.call("gallery_views") for v in vs])
    app = App(items, os.path.abspath(a.drive) if a.drive else None, views, a.share_out)
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
 --on-acc:#fff;--sbw:280px;--ml:280px;--hh:56px;--tile:168px}
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
#evlist{display:flex;flex-direction:column;gap:2px}
.evmore{align-self:flex-start;margin:2px 10px}
#evbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:12px 24px 0}
#evbar .evt{font-weight:600;margin-right:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
#mfieldp label{display:block;font-size:13px;color:var(--mut);margin-bottom:4px}
#mfield{width:100%}
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
.collapsed .sb-search,.collapsed #filters,.collapsed #evsec{display:none}
.collapsed #views{padding:4px 8px}
.collapsed .nv{justify-content:center;padding:0;height:40px}
.collapsed .railonly{display:flex}
.badge-n{position:absolute;top:3px;right:3px;min-width:16px;height:16px;border-radius:8px;background:var(--acc);color:var(--on-acc);font-size:10px;line-height:16px;text-align:center;padding:0 4px}
.overlay{--ml:60px;--sbw:min(304px,86vw)}
.overlay #sidebar{box-shadow:var(--shadow)}

/* ---------------------------------------------------------------- main */
#app{margin-left:var(--ml);min-height:100vh;min-width:0;display:flex;flex-direction:column}
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
.tile .ck{position:absolute;top:6px;left:6px;z-index:2;width:22px;height:22px;margin:0;accent-color:var(--acc);cursor:pointer}
@media (hover:hover){.tile .ck{opacity:0;transition:opacity .1s}.tile:hover .ck,.tile:focus-within .ck,.selecting .tile .ck{opacity:1}}
.play{position:absolute;right:6px;top:6px;width:24px;height:24px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;padding-left:2px}
/* Burst stacks: the review page's look (a stacked card, a count badge, an X button) on a tile */
.tile.stack{box-shadow:4px 4px 0 -1px var(--bg),4px 4px 0 0 var(--mut),8px 8px 0 -1px var(--bg),8px 8px 0 0 var(--mut)}
.tile.stack.cur{box-shadow:inset 0 0 0 3px var(--fg),4px 4px 0 -1px var(--bg),4px 4px 0 0 var(--mut),8px 8px 0 -1px var(--bg),8px 8px 0 0 var(--mut)}
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
}
.flag{position:absolute;left:6px;bottom:6px;width:max-content;max-width:calc(100% - 12px);background:rgba(0,0,0,.62);color:#fff;font-size:11px;border-radius:4px;padding:1px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sentinel{height:40px}
/* bottom action bar: only while something is selected (or the Trash suggestions view's Apply) */
/* sticky, last in #app: it floats over the grid while scrolling, but takes its own room at the end, so no tile
   stays under it however many rows its buttons wrap to */
#actionbar{position:sticky;margin:0 16px;bottom:16px;z-index:12;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:center;padding:10px 14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.grp{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
#seln,#sugn{font-weight:650;font-size:14px;margin-right:4px;white-space:nowrap}
.sep{width:1px;height:22px;background:var(--line)}
#toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:40;background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:14px;max-width:min(560px,90vw);box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:opacity .2s;display:flex;align-items:center;gap:12px}
#toast.on{opacity:1;pointer-events:auto}
#toast button,#etoast button{flex:none;border:0;background:none;color:inherit;font-weight:650;text-decoration:underline;padding:4px 2px;border-radius:4px}
#toast button:focus-visible{outline-color:var(--bg)}
/* an error stays until dismissed (role=alert: read out at once) */
#etoast{position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:41;display:none;align-items:flex-start;gap:12px;background:var(--panel);color:var(--erase);border:1px solid var(--erase);padding:10px 14px;border-radius:8px;font-size:14px;width:max-content;max-width:min(640px,calc(100vw - 32px));box-shadow:var(--shadow)}
#etoast.on{display:flex}
#etoast span{flex:1;min-width:0;word-break:break-word;color:var(--fg)}
#etoast b{color:var(--erase)}
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

/* ---------------------------------------------------------------- narrow: the header's controls fit beside the title */
@media (max-width:900px){
 #tsize{width:72px}
 #close .lbl2{display:none}#close{padding:0 8px}
}
/* ---------------------------------------------------------------- phone */
@media (max-width:760px){
 #top{padding:0 12px;gap:8px}
 #vtitle{font-size:16px}
 #count,.size .ic{display:none}
 #ball{padding:0 9px}
 #chips,#evbar{padding:10px 12px 0}#note,#helpb{padding:0 12px}
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
 <symbol id="i-cal" viewBox="0 0 24 24"><rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
 <symbol id="i-keys" viewBox="0 0 24 24"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></symbol>
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
  <div id="evsec"><div class="nvhead">Events</div><div id="evlist"></div></div>
  <button type="button" class="nv" id="bkeys" title="Keyboard shortcuts (?)" aria-keyshortcuts="?" aria-haspopup="dialog"><svg class="ic"><use href="#i-keys"/></svg><span class="lbl">Keyboard shortcuts</span><span class="ct"><kbd>?</kbd></span></button>
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
  <button type="button" class="btn" id="ball" aria-keyshortcuts="Control+A Meta+A" title="Select every tile shown (Ctrl+A, or Cmd+A on a Mac); Esc clears" hidden>Select all</button>
  <label class="size" id="tsizew" title="Thumbnail size"><svg class="ic"><use href="#i-size"/></svg><span class="vh">Thumbnail size</span><input id="tsize" type="range" min="80" max="320" step="8"></label>
  <button type="button" class="btn" id="close" title="Stop the gallery server"><svg class="ic"><use href="#i-power"/></svg><span class="lbl2">Close</span></button>
 </header>
 <div id="chips" aria-label="Active filters" hidden></div>
 <div id="evbar" role="toolbar" aria-label="Event" hidden><span class="evt" id="evname"></span><button type="button" class="btn" id="evren">Rename…</button><button type="button" class="btn" id="evmerge">Merge into…</button></div>
 <p id="note" class="note" hidden></p>
 <p id="helpb" class="note" hidden><span class="hshort">Bursts are stacked; X opens one.</span><span class="hfull">A stack is a burst (shots from one camera a moment apart): X opens or closes it. On a closed stack, selecting and every action (Keep, Trash, Erase, tags, Rotate, Set event, Share-safe copy) act on the cover shot only; open the stack to act on the others.</span><button type="button" class="link" id="hmore" aria-controls="helpb" aria-expanded="false">more</button></p>
 <main id="main"><div id="list"></div><div id="sentinel"></div><div id="plugin" hidden></div></main>
 <div id="actionbar" hidden>
  <div class="grp" id="selgrp" role="toolbar" aria-label="Selection actions" hidden><span id="seln"></span>
   <button type="button" class="btn keep" id="bkeep">Keep <kbd>K</kbd></button><button type="button" class="btn trash" id="btrash">Trash <kbd>T</kbd></button><button type="button" class="btn erase" id="berase">Erase <kbd>E</kbd></button>
   <span class="sep"></span>
   <button type="button" class="btn" id="btag">+ tag</button><button type="button" class="btn" id="buntag">− tag</button><button type="button" class="btn" id="bevent">Set event…</button><button type="button" class="btn" id="brot">Rotate ⟳</button><button type="button" class="btn" id="bshare">Share-safe copy</button><button type="button" class="btn" id="bnone">Clear selection <kbd>Esc</kbd></button>
  </div>
  <div class="grp" id="suggrp" role="group" aria-label="Decisions" hidden><span id="sugn"></span><span class="note" style="margin:0">K / T on the outlined tile, arrows move, Enter opens</span><button type="button" class="btn primary" id="sugapply">Apply…</button></div>
 </div>
</div>
<div id="toast" role="status" aria-live="polite"></div>
<div id="etoast" role="alert" aria-atomic="true"></div>
<div id="viewer" role="dialog" aria-modal="true" aria-label="Item" tabindex="-1">
 <div id="stage"><div id="media"></div>
  <button type="button" class="arrow" id="vprev" aria-label="Previous (Left arrow)">‹</button><button type="button" class="arrow" id="vnext" aria-label="Next (Right arrow)">›</button></div>
 <aside id="info" aria-label="Details"></aside>
</div>
<div class="modal" id="modal" role="dialog" aria-modal="true" aria-labelledby="mh" data-dialog="confirm"><div class="box"><h3 id="mh"></h3><div id="mb"></div>
 <p id="mfieldp" hidden><label for="mfield" id="mflabel"></label><input id="mfield" list="evnames" autocomplete="off" spellcheck="false"><datalist id="evnames"></datalist><datalist id="mytags"></datalist></p>
 <p id="mtype" hidden>Type <b>ERASE</b> to confirm: <input id="mtyped" autocomplete="off" spellcheck="false" aria-label="Type ERASE"></p>
 <div class="mbtns"><button type="button" class="btn" id="mno">Cancel</button><button type="button" class="btn primary" id="mgo">Confirm</button></div><p id="mstatus" role="status" hidden></p><pre id="mout" hidden></pre></div></div>
<div class="modal" id="keys" role="dialog" aria-modal="true" aria-labelledby="kh"><div class="box"><h3 id="kh">Keyboard shortcuts</h3>
 <p class="note">Keys act on the page, never while you type in a field. Esc closes this list.</p>
 <h4>Grid</h4><dl>
  <dt><kbd>Enter</kbd></dt><dd>Open the focused tile in the large view</dd>
  <dt><kbd>Space</kbd></dt><dd>Select or unselect the focused tile (<kbd>Shift</kbd>+<kbd>Space</kbd>: the range from the last one)</dd>
  <dt>Shift+click, Ctrl+click</dt><dd>Select a range; tick or untick one tile</dd>
  <dt><kbd>Ctrl</kbd>+<kbd>A</kbd> / <kbd>Cmd</kbd>+<kbd>A</kbd></dt><dd>Select every tile shown</dd>
  <dt><kbd>K</kbd> <kbd>T</kbd> <kbd>E</kbd></dt><dd>Keep, Trash (asks first), Erase (asks, ERASE typed) the selection</dd>
  <dt><kbd>Esc</kbd></dt><dd>Clear the selection; close the sidebar drawer (phone); in the search box, clear the search</dd>
  <dt><kbd>/</kbd></dt><dd>Jump to the search box</dd>
  <dt><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Cmd</kbd>+<kbd>Z</kbd></dt><dd>Undo the last Keep, tag, Rotate or Set event while its message shows</dd>
  <dt><kbd>?</kbd></dt><dd>This list</dd>
 </dl>
 <h4>Bursts</h4><dl>
  <dt><kbd>X</kbd></dt><dd>Open or close the focused tile's burst stack (focus goes back to the cover)</dd>
 </dl>
 <h4>Large view</h4><dl>
  <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>Previous / next item (over a closed stack's hidden shots)</dd>
  <dt><kbd>Space</kbd></dt><dd>Play / pause a video</dd>
  <dt><kbd>K</kbd> <kbd>T</kbd> <kbd>E</kbd></dt><dd>Keep, Trash, Erase the item shown</dd>
  <dt><kbd>R</kbd> / <kbd>Shift</kbd>+<kbd>R</kbd></dt><dd>Rotate clockwise / anticlockwise (the sidecar only)</dd>
  <dt><kbd>Esc</kbd></dt><dd>Close the large view</dd>
 </dl>
 <h4>Trash suggestions</h4><dl>
  <dt><kbd>←</kbd> <kbd>↑</kbd> / <kbd>→</kbd> <kbd>↓</kbd> <kbd>J</kbd></dt><dd>Move the outline</dd>
  <dt><kbd>K</kbd> <kbd>T</kbd></dt><dd>Mark the outlined tile keep / trash (nothing changes until Apply)</dd>
  <dt><kbd>Enter</kbd></dt><dd>Open the outlined tile</dd>
 </dl>
 <h4>Dialogs</h4><dl>
  <dt><kbd>Esc</kbd></dt><dd>Cancel and close</dd>
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
// per-browser conveniences only (sidebar rail, open sections, tile size); the page works without them
const LS={get(k,d){try{const v=localStorage.getItem("gallery."+k);return v===null?d:v}catch(e){return d}},set(k,v){try{localStorage.setItem("gallery."+k,v)}catch(e){}}};
let view="lib",items=[],total=0,loading=false,done=false,sel=new Set(),last=null,cur=-1,ranked=false,simId=null,simName="",dec={},vi=-1,facets=null,gen=0,lastFocus=null,openB={},allSel=null;
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
// the burst help at phone width: one line, "more" for the whole text (remembered per browser)
function helpMore(on){$("#helpb").classList.toggle("more",on);$("#hmore").textContent=on?"less":"more";$("#hmore").setAttribute("aria-expanded",String(on))}
helpMore(LS.get("helpmore","0")==="1");
$("#hmore").onclick=()=>{const on=!$("#helpb").classList.contains("more");helpMore(on);LS.set("helpmore",on?"1":"0")};
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
 renderWhen();renderEvents();renderMyTags();
 $("#ck-all").textContent=fmt(facets.total);$("#ck-photo").textContent=fmt(c["kind:photo"]);$("#ck-video").textContent=fmt(c["kind:video"]);
 $("#c-lib").textContent=fmt(facets.total);$("#c-sug").textContent=fmt(c.suggest);$("#c-recent").textContent=fmt(c.recent);
 $("#c-child").textContent=fmt(c["review:child-in-shot"]);$("#c-untagged").textContent=fmt(c["review:untagged"]);
 $('.nv[data-v=recent]').title="Recently added (filed in the last "+facets.recent_days+" days)";
 const h=$("#qhint");h.replaceChildren();h.hidden=!facets.search;
 if(facets.search){const checking=/^checking/.test(facets.search);const d=el("details"),s=el("summary",null,checking?"Checking the search model…":"Semantic search off: tags and names only");
  d.append(s,el("div","why",facets.search));h.append(d);h.title=facets.search;if(checking&&(tries||0)<10)setTimeout(()=>loadFacets((tries||0)+1).catch(()=>{}),1500)}
 syncUI()}
// ---------------------------------------------------------------- events (Event| tags): the sidebar list, rename, merge
let evAll=false;const EVSHOW=8;
// the tag field's suggestions: every tag of yours in the library (the My tags facet)
function renderMyTags(){$("#mytags").replaceChildren(...((facets&&facets.facets.mine)||[]).map(([n])=>{const o=el("option");o.value=n;return o}))}
function renderEvents(){const L=$("#evlist");L.replaceChildren();const ev=(facets&&facets.facets.event)||[];
 $("#evnames").replaceChildren(...ev.map(([n])=>{const o=el("option");o.value=n;return o}));
 if(!ev.length){L.append(el("div","empty","No events yet"));return}
 const show=evAll?ev.slice():ev.slice(0,EVSHOW);if(F.event&&!show.some(([n])=>n===F.event)){const x=ev.find(([n])=>n===F.event);if(x)show.push(x)}
 show.forEach(([n,c])=>{const b=el("button","nv");b.type="button";b.dataset.ev=n;b.title="Event: "+n;b.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-cal"/></svg>');b.append(el("span","lbl",n),el("span","ct",fmt(c)));
  b.onclick=()=>{F={aimin:F.aimin,event:n};$("#q").value="";setView("lib");if(mqPhone.matches&&drawer){drawer=false;layout()}};L.append(b)});
 if(ev.length>EVSHOW){const m=el("button","link evmore",evAll?"Fewer events":"All "+fmt(ev.length)+" events");m.type="button";m.onclick=()=>{evAll=!evAll;renderEvents();syncUI()};L.append(m)}}
function askField(title,lines,label,value,go,list){confirmBox(title,lines,false,go);$("#mfieldp").hidden=false;$("#mflabel").textContent=label;const f=$("#mfield");f.setAttribute("list",list||"evnames");f.value=value||"";f.focus();f.select()}
async function afterEvent(j,ev){closeModal();toast(j.result,j.undo);if(ev!==undefined){if(ev)F.event=ev;else delete F.event}await loadFacets();reset()}
$("#evren").onclick=()=>{const old=F.event;askField("Rename the event "+old+"?",["Every sidecar with Event|"+old+" gets the new name instead: the Event| tag and XMP-iptcExt:Event only. The files and every other tag stay as they are.","A slug: lowercase letters, digits and dashes. To join an event that already exists, use Merge."],"New name",old,
 async()=>afterEvent(await api("/api/event/rename",{from:old,to:$("#mfield").value.trim()}),$("#mfield").value.trim()))};
$("#evmerge").onclick=()=>{const src=F.event;askField("Merge "+src+" into another event?",["Every item of "+src+" moves to the event you name (the Event| tag and XMP-iptcExt:Event in its sidecar only); "+src+" is gone afterwards."],"Merge into (an event that exists)","",
 async()=>afterEvent(await api("/api/event/merge",{from:[src],into:$("#mfield").value.trim()}),$("#mfield").value.trim()))};
// ---------------------------------------------------------------- filter state
function setF(k,v){if(v)F[k]=v;else delete F[k];simId=null;if(plugView()){setView("lib");return}syncUI();reset()}
function clearAll(){F={aimin:F.aimin||"0.7"};$("#q").value="";simId=null;syncUI();reset()}
function navActive(){if(view!=="lib")return view;const a=activeKeys();if(a.length===1&&!$("#q").value.trim()&&simId===null){if(F.event)return "ev";if(F.added)return "recent";if(F.review==="child-in-shot")return "child";if(F.review==="untagged")return "untagged"}return "lib"}
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
 $$(".nv[data-ev]").forEach(b=>{if(na==="ev"&&b.dataset.ev===F.event)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current")});
 $("#evbar").hidden=na!=="ev";if(na==="ev")$("#evname").textContent="Event: "+F.event;
 $("#vtitle").textContent=na==="ev"?"Event: "+F.event:TITLES[na]||""}
$("#clear").onclick=clearAll;
// ---------------------------------------------------------------- listing
function setNote(n){n=n||"";if(/^Semantic search unavailable/.test(n))n="Semantic search is off: showing tag and file-name matches.";$("#note").textContent=n;$("#note").hidden=!n}
function reset(){gen++;items=[];total=0;done=false;cur=-1;last=null;openB={};$("#list").replaceChildren();sel.clear();dec={};paintSel();more()}
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
  g.append(tile(it,n))}$("#helpb").hidden=!items.some(i=>i.b!=null);allSel=null;paintSel()}
/* Bursts (bursts.py, server side): the listed shots of one burst follow one another, the cover (it.bc) first.
   A closed stack shows only its cover; a shot of a closed stack is never selected, shown or stepped to. */
function hiddenAt(n){const it=items[n];return !!it&&it.b!=null&&!it.bc&&!openB[it.b]}
function stepVis(n,d){n+=d;while(n>=0&&n<items.length&&hiddenAt(n))n+=d;return n}
function stackLabel(it){return openB[it.b]?"Close stack (X)":`Show all ${it.bn} (X)`}
function bxLabel(it){return `Burst: ${it.bn} shots, ${openB[it.b]?"close":"open"} stack`}
function toggleStack(n){const it=items[n];if(!it||it.b==null)return;const b=it.b;allSel=null;openB[b]=!openB[b];let c=-1,dropped=0;
 items.forEach((m,i)=>{if(m.b!==b)return;if(m.bc)c=i;const t=$("#t"+m.id);if(t)t.hidden=hiddenAt(i);if(hiddenAt(i)&&sel.has(m.id)){sel.delete(m.id);dropped++}});
 if(last!==null&&hiddenAt(last))last=null;if(cur>=0&&hiddenAt(cur))cur=c;
 const ct=c>=0?$("#t"+items[c].id):null;if(ct){ct.classList.toggle("stack",!openB[b]);const x=ct.querySelector(".x");if(x)x.textContent=stackLabel(items[c]);const bx=ct.querySelector(".bx");if(bx)bx.setAttribute("aria-label",bxLabel(items[c]));ct.focus({preventScroll:false})}
 if(dropped)toast(`Stack closed: ${dropped} hidden shot${dropped>1?"s":""} unselected (actions act on the cover only).`);paintSel()}
function tile(it,n){const t=el("div","tile");t.id="t"+it.id;t.dataset.n=n;t.dataset.item=it.n;t.tabIndex=0;t.title=it.n;
 const im=el("img");im.loading="lazy";im.decoding="async";im.alt=it.n;im.src="/t/"+it.id;im.onerror=()=>{im.replaceWith(el("div","ph",(it.m==="video"?"video: ":"")+it.n))};t.append(im);
 const cb=el("input","ck");cb.type="checkbox";cb.tabIndex=-1;cb.setAttribute("aria-label","select "+it.n);cb.onclick=ev=>{ev.stopPropagation();pick(n,ev.shiftKey)};t.append(cb);
 if(it.m==="video")t.append(el("span","play","▶"));
 if(it.b!=null){t.dataset.burst=it.b;const bd=el("span","badge",`Burst: ${it.bn} shots`);bd.dataset.count=it.bn;t.append(bd);
  if(it.bc){const bx=el("button","bx","×"+it.bn);bx.type="button";bx.tabIndex=-1;bx.setAttribute("aria-label",bxLabel(it));bx.onclick=ev=>{ev.stopPropagation();toggleStack(n)};bd.before(bx);
   const x=el("button","x",stackLabel(it));x.type="button";x.tabIndex=-1;x.onclick=ev=>{ev.stopPropagation();toggleStack(n)};t.append(x);if(!openB[it.b])t.classList.add("stack")}
  else t.hidden=hiddenAt(n)}
 const fl=flagsOf(it);if(fl)t.append(el("span","flag",fl));
 t.onclick=ev=>{if(ev.shiftKey||ev.ctrlKey||ev.metaKey){pick(n,ev.shiftKey);return}if(view==="sug"){cur=n;paintSel();return}openViewer(n)};
 // Enter opens, Space selects (on the focused tile, never while the large view is open: Space plays / pauses there)
 t.onkeydown=ev=>{if(vi>=0||ev.target!==t)return;if(ev.key==="Enter"&&view!=="sug"){ev.preventDefault();ev.stopPropagation();openViewer(n)}else if(ev.key===" "){ev.preventDefault();ev.stopPropagation();pick(n,ev.shiftKey)}
  else if((ev.key==="x"||ev.key==="X")&&it.b!=null&&!ev.ctrlKey&&!ev.metaKey&&!ev.altKey){ev.preventDefault();ev.stopPropagation();toggleStack(n)}};return t}
function flagsOf(it){const fl=[];if(it.c!=null)fl.push("AI "+it.c.toFixed(1));fl.push(...it.f);return fl.join(" · ")}
// after an edit: the reply's summaries replace those items and their tiles' flags (kept, ...);
// the tile, its focus and the selection stay
function patchTiles(fresh){(fresh||[]).forEach(s=>{const n=items.findIndex(i=>i.id===s.id);if(n<0)return;const o=items[n];items[n]=o.b!=null?Object.assign(s,{b:o.b,bn:o.bn,bc:o.bc,mo:o.mo}):s;const t=$("#t"+s.id);if(!t)return;
  const old=t.querySelector(".flag"),fl=flagsOf(s);if(old)old.remove();if(fl)t.append(el("span","flag",fl))})}
function pick(n,range){allSel=null;if(range&&last!==null){const [a,b]=[Math.min(last,n),Math.max(last,n)];for(let i=a;i<=b;i++)if(!hiddenAt(i))sel.add(items[i].id)}
 else{const id=items[n].id;sel.has(id)?sel.delete(id):sel.add(id)}last=n;paintSel()}
function paintSel(){$$(".tile").forEach(t=>{const id=+t.id.slice(1),n=+t.dataset.n;t.classList.toggle("sel",sel.has(id));t.querySelector("input").checked=sel.has(id);
  t.classList.toggle("cur",n===cur);t.classList.toggle("dk",dec[id]==="keep");t.classList.toggle("dt",dec[id]==="trash")});
 const selOn=sel.size>0&&!plugView(),sugOn=view==="sug";$("#selgrp").hidden=!selOn;$("#suggrp").hidden=!sugOn;$("#actionbar").hidden=!(selOn||sugOn);
 document.body.classList.toggle("selecting",sel.size>0);
 $("#seln").textContent=allSel&&allSel.n===sel.size?allSel.text:fmt(sel.size)+" selected";$("#ball").hidden=!canSelectAll();
 const nk=Object.values(dec).filter(d=>d==="keep").length,nt=Object.values(dec).filter(d=>d==="trash").length;
 $("#sugn").textContent=`${nk} keep / ${nt} trash marked`;$("#sugapply").disabled=!(nk+nt)}
function selIds(){return [...sel]}
/* Select all: every tile the grid shows now (the active view, filters and search; the pages loaded so far, since
   the grid loads 240 at a time as you scroll), and on a closed burst stack its cover only (hidden shots are never
   selected). Not in Trash suggestions (K / T and Apply there) or a plugin tab. Trash and Erase ask as always. */
function canSelectAll(){return view!=="sug"&&!plugView()&&items.length>0}
function selectAll(){if(!canSelectAll())return;let hid=0;items.forEach((it,i)=>{if(hiddenAt(i))hid++;else sel.add(it.id)});
 const left=total-items.length,n=sel.size;
 allSel={n,text:`Selected ${fmt(n)} (all shown`+(left>0?`; ${fmt(left)} more match below: scroll to load them, then Select all again`:"")
  +(hid?`; closed stacks: cover only`:"")+")"};paintSel()}
function nameOf(id){const it=items.find(i=>i.id===id);return it?it.n:"#"+id}
// ---------------------------------------------------------------- actions
/* Messages: the toast (role=status) fades; one with Undo stays 10 s, longer while the pointer or focus is on it.
   An error goes to #etoast (role=alert, read out at once) and stays until dismissed. No native dialogs. */
let tt=null,undoTok=null;
function toast(s,undo){const t=$("#toast");t.replaceChildren(el("span",null,s));undoTok=undo||null;
 if(undo){const b=el("button",null,"Undo");b.type="button";b.id="tundo";b.title="Undo (Ctrl+Z)";b.setAttribute("aria-keyshortcuts","Control+Z Meta+Z");b.onclick=doUndo;t.append(b)}
 t.classList.add("on");clearTimeout(tt);tt=setTimeout(hideToast,undo?10000:4500)}
function hideToast(){const t=$("#toast");if(t.matches(":hover,:focus-within")){tt=setTimeout(hideToast,1500);return}
 t.classList.remove("on");undoTok=null;const b=$("#tundo");if(b)b.remove()}
function errorToast(s){const t=$("#etoast"),x=el("button",null,"Dismiss");x.type="button";x.setAttribute("aria-label","Dismiss error");
 x.onclick=()=>{t.classList.remove("on");t.replaceChildren()};t.replaceChildren(el("span",null,s),x);t.classList.add("on")}
/* Undo: the server recorded what Keep, a tag, Rotate or Set event changed and answers with a token; /api/undo puts
   exactly that back, under the library lock, and refuses (nothing written) when those items changed again since.
   Trash, Erase and Share-safe copy never offer it. */
async function doUndo(){const tok=undoTok;if(!tok)return;undoTok=null;const b=$("#tundo");if(b)b.remove();
 try{const j=await api("/api/undo",{undo:tok});toast(j.result);loadFacets();if(j.kind==="event"&&vi<0){patchTiles(j.items);reset()}else refreshAfter(j.ids,j.thumbs,j.items)}
 catch(e){errorToast("Undo refused: "+e.message)}}
async function doKeep(ids){try{const j=await api("/api/keep",{ids});toast(j.result,j.undo);refreshAfter(ids,false,j.items);loadFacets()}catch(e){errorToast("Keep refused: "+e.message)}}
/* In-page dialogs (the confirm / job dialog, the shortcut list): focus moves in, Tab stays in, Esc closes, and
   focus goes back to where it was. The large view is a dialog too (Tab stays in it, and its messages). */
let modalReturn=null,keysReturn=null;
const isOn=s=>$(s).classList.contains("on");
function refocus(r){if(r&&r.isConnected&&r.focus&&r.getClientRects().length)r.focus({preventScroll:true})}
function openModal(){if(!isOn("#modal"))modalReturn=document.activeElement;$("#modal").classList.add("on")}
function closeModal(){if(!isOn("#modal"))return;$("#modal").classList.remove("on");const r=modalReturn;modalReturn=null;refocus(r);
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
function confirmBox(title,lines,needType,go){$("#mh").textContent=title;$("#mgo").textContent="Confirm";const b=$("#mb");b.replaceChildren();lines.forEach(l=>b.append(el("p",l.startsWith("!")?"warn":null,l.replace(/^!/,""))));
 $("#mtype").hidden=!needType;$("#mfieldp").hidden=true;$("#mtyped").value="";$("#mgo").disabled=!!needType;$("#mgo").hidden=false;$("#mout").hidden=true;$("#mout").textContent="";$("#mstatus").hidden=true;delete $("#modal").dataset.job;openModal();
 $("#mtyped").oninput=()=>{$("#mgo").disabled=$("#mtyped").value!=="ERASE"};if(needType)$("#mtyped").focus();else $("#mno").focus();
 $("#mgo").onclick=async()=>{$("#mgo").disabled=true;try{await go()}catch(e){$("#mout").hidden=false;$("#mout").textContent="Refused: "+e.message;$("#mgo").disabled=false}}}
$("#mno").onclick=closeModal;
$("#kclose").onclick=closeKeys;$("#bkeys").onclick=openKeys;
$("#keys").onclick=ev=>{if(ev.target===$("#keys"))closeKeys()};  // a click on the backdrop
$("#mfield").onkeydown=ev=>{if(ev.key==="Enter"&&!$("#mgo").disabled){ev.preventDefault();$("#mgo").click()}};
function names(ids){const n=ids.map(nameOf);return n.slice(0,15).join(", ")+(n.length>15?` … and ${n.length-15} more`:"")}
function warnings(ids){const its=ids.map(id=>items.find(i=>i.id===id)).filter(Boolean),w=[];
 const u=its.filter(i=>i.f.includes("used")).length;if(u)w.push(`!${u} were used in posts (Used|…).`);
 const k=its.filter(i=>i.f.includes("kept")).length;if(k)w.push(`!${k} you marked Keep before.`);return w}
function askTrash(ids){confirmBox(`Trash ${ids.length} item${ids.length>1?"s":""}?`,[names(ids),"Plain delete (not erase): the file, its sidecar and thumbnails go, and intake never takes the same file in again. Use Erase for anything private.",...warnings(ids)],false,
 ()=>runJob("/api/trash",{ids,confirm:true},ids))}
function askErase(ids){confirmBox(`Erase ${ids.length} item${ids.length>1?"s":""} everywhere?`,[names(ids),"For sensitive files: intake.py forget moves them to quarantine and erases every copy in HOME (and the drive, if the gallery was started with --drive), with the sidecars, thumbnails and recent-file entries. This cannot be undone.",...warnings(ids)],true,
 ()=>runJob("/api/erase",{ids,confirm:$("#mtyped").value},ids))}
function askSetEvent(ids){if(!ids.length)return;askField("Set the event on "+ids.length+" item"+(ids.length>1?"s":"")+"?",[names(ids),"Each one goes into this event and out of any other: the Event| tag and XMP-iptcExt:Event in its sidecar. The originals and every other tag stay as they are. Leave it empty to take them out of every event."],"Event (a slug: lowercase letters, digits, dashes)",F.event||"",
 async()=>{const ev=$("#mfield").value.trim(),j=await api("/api/event/set",{ids,event:ev});if(vi>=0){closeModal();toast(j.result,j.undo);loadFacets();openViewer(vi,{keep:true})}else afterEvent(j)})}
/* Share-safe copies: intake.py share-copy as a job (one at a time, with Trash and Erase), into a new folder; the page
   shows the folder and share-copy's own lines (each copy's path and "gps=none"), never the copies themselves. */
let jobDest="";
function askShare(ids){if(!ids.length)return;confirmBox("Make share-safe copies of "+ids.length+" item"+(ids.length>1?"s":"")+"?",[names(ids),"Copies with all metadata and GPS stripped (media-intake share-copy) go into a new folder under "+((facets&&facets.share_out)||"the share-safe folder")+". The originals and their sidecars are only read."],false,
 ()=>runShare(ids))}
async function runShare(ids){let j;try{j=await api("/api/share",{ids})}catch(e){if(/still running/.test(e.message)){attachJob();return}throw e}
 jobDest=j.dest;const b=$("#mb");b.replaceChildren(el("p",null,"Destination: "+j.dest));b.firstChild.id="mdest";progressMode("share-copy");poll(0,[])}
/* Trash and Erase run as a detached job (jobs.py): closing the page or stopping the server never stops
   them, and any page (a reload, a second tab, a restarted server) shows the running one from its status file. */
async function runJob(path,body,ids){jobDest="";try{await api(path,body)}catch(e){if(/still running/.test(e.message)){attachJob();return}throw e}
 progressMode(path==="/api/erase"?"forget":"trash");poll(0,ids)}
/* Shown from the moment a job starts, before its first output line: an erase sweeps HOME for copies and can
   print nothing for minutes, which looked like a hang. Hidden once the job has finished. */
const STATUS={forget:"Erasing… looking for copies in your home folder. This can take a few minutes; you can close this page, it keeps running.",
 trash:"Trashing… you can close this page, it keeps running.",
 "share-copy":"Copying without metadata or GPS… you can close this page, it keeps running."};
function jobStatus(label){const t=STATUS[label]||"Working… you can close this page, it keeps running.";const m=$("#mstatus");if(m.textContent!==t)m.textContent=t;m.hidden=false}
function progressMode(label){$("#mgo").hidden=true;$("#mtype").hidden=true;$("#mfieldp").hidden=true;$("#mout").hidden=false;$("#mout").textContent="";jobStatus(label);openModal()}
function stepText(j){return j.step?` (step ${j.step}${j.total?" of "+j.total:""}: ${j.label})`:""}
async function poll(since,ids){let j;try{j=await api("/api/job?since="+since)}catch(e){$("#mout").textContent+="\n(server stopped; an action already started keeps going on its own)";return}
 if(j.lines.length){$("#mout").textContent+=j.lines.join("\n")+"\n";$("#mout").scrollTop=1e9}$("#modal").dataset.job=j.done?(j.state==="crashed"?"crashed":j.ok?"done":"failed"):"running";
 if(!j.done){if(j.label)jobStatus(j.label);$("#mh").textContent="Running"+stepText(j)+"; closing the page does not stop it";setTimeout(()=>poll(j.next,ids),600);return}
 $("#mh").textContent=j.state==="crashed"?"The action stopped unexpectedly (crashed)"+stepText(j)+(j.step_running?"; the step it started is still running":"")+". Nothing more runs by itself."
  :/gps=STILL PRESENT/.test($("#mout").textContent)?"Finished, but GPS is still in some copies (see below): don't share those"
  :j.ok?"Done"+(j.label==="share-copy"&&jobDest?": share-safe copies in "+jobDest:""):"Finished with problems (see below)";
 $("#mstatus").hidden=true;ids.forEach(id=>sel.delete(id));closeViewer();loadFacets();reset()}
async function attachJob(){let j;try{j=await api("/api/job")}catch(e){return}if(j.done)return;$("#mh").textContent="An action is running";$("#mb").replaceChildren();progressMode(j.label);poll(0,j.ids||[])}
/* Your tags: the same refusals as the server (gallery.py tag(): TAG_RE, TOOL_PREFIXES), checked first so the field
   says why at once; the server checks again. Suggestions: every tag of yours in the library (#mytags). */
const TOOL_NS=["project|","source|","event|","place|","label|","review|","ai|","used|","owner|"];
function tagProblem(t){if(!t||[...t].length>64||/[\x00-\x1f\x7f]/.test(t)||t.startsWith("-")||t.includes("@@sep@@"))return "a tag is 1-64 characters on one line";
 if(TOOL_NS.some(p=>t.toLowerCase().startsWith(p)))return "Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used| and Owner| belong to the tools; pick another name";return ""}
function tagAsk(op){const ids=vi>=0&&!sel.size?[items[vi].id]:selIds();if(!ids.length)return;const n=ids.length+" item"+(ids.length>1?"s":"");
 askField(op==="add"?"Add a tag of yours to "+n+"?":"Remove a tag of yours from "+n+"?",[names(ids),"Your own tags only, not the tools' (Project|, Source|, Event|, Place|, Label|, Review|, AI|, Used|, Owner|). A | makes a hierarchy, like Trips|Spain. Only the sidecars change."],
  "Tag (yours: suggestions as you type)","",async()=>{const t=$("#mfield").value.trim(),why=tagProblem(t);if(why)throw new Error(why);
   const j=await api("/api/tag",{ids,tag:t,op});closeModal();toast(j.result,j.undo);loadFacets();refreshAfter(ids,false,j.items)},"mytags")}
async function rot(ids,dir){try{const j=await api("/api/rotate",{ids,dir});toast(j.result,j.undo);refreshAfter(ids,true,j.items)}catch(e){errorToast("Rotate refused: "+e.message)}}
function refreshAfter(ids,thumbs,fresh){patchTiles(fresh);if(thumbs)ids.forEach(id=>{const t=$("#t"+id);if(t){const im=t.querySelector("img");if(im)im.src="/t/"+id+"?r="+Date.now()}});
 if(vi>=0)openViewer(vi,{keep:true});else if(view==="sug")reset()}
$("#bkeep").onclick=()=>doKeep(selIds());$("#btrash").onclick=()=>askTrash(selIds());$("#berase").onclick=()=>askErase(selIds());
$("#btag").onclick=()=>tagAsk("add");$("#buntag").onclick=()=>tagAsk("remove");$("#brot").onclick=()=>rot(selIds(),"cw");
$("#ball").onclick=selectAll;$("#bevent").onclick=()=>askSetEvent(selIds());$("#bshare").onclick=()=>askShare(selIds());$("#bnone").onclick=()=>{sel.clear();paintSel()};
$("#sugapply").onclick=()=>{const keep=Object.keys(dec).filter(k=>dec[k]==="keep").map(Number),trash=Object.keys(dec).filter(k=>dec[k]==="trash").map(Number);
 confirmBox(`Apply: ${keep.length} keep, ${trash.length} trash?`,[keep.length?"Keep: Owner|keep + Owner|not-trash in the sidecar (never suggested again).":"",trash.length?"Trash ("+names(trash)+"): plain delete, never taken in again (Owner|trash: the sha256 in trashed.json; copies left on disk are marked).":""].filter(Boolean),false,
  async()=>{const j=await api("/api/decide",{keep,trash,confirm:true});if(trash.length){progressMode("trash");$("#mout").textContent=j.result+"\n";poll(0,trash)}else{closeModal();toast(j.result);loadFacets();reset()}})};
// ---------------------------------------------------------------- large view
function curVideo(){return $("#media video")}
function stopVideo(){const v=curVideo();if(v){v.pause();v.removeAttribute("src");v.load()}}  // releases the file
function fitMedia(){const m=$("#media>img,#media>video");if(!m)return;const r=$("#media").getBoundingClientRect(),q=+(m.dataset.rot||0)%180!==0;
 m.style.width=q?r.height+"px":"100%";m.style.height=q?r.width+"px":"100%";m.style.transform=`translate(-50%,-50%) rotate(${m.dataset.rot||0}deg)`}
addEventListener("resize",fitMedia);
// the browser can't show it: the thumbnail, large, and a button that opens the file in the desktop's own player
function cantPlay(d,why){stopVideo();const M=$("#media");M.replaceChildren();const box=el("div","nop"),im=el("img");im.alt=d.name;im.src="/t/"+d.id;im.onerror=()=>im.remove();
 const c=el("div","cant");c.append(el("span",null,why));const o=el("button",null,"Open in the system player");o.type="button";
 o.onclick=async()=>{try{toast((await api("/api/open",{ids:[d.id]})).result)}catch(e){errorToast("Refused: "+e.message)}};c.append(o);
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
 const head=el("div","ihead"),x=el("button","icon");x.id="vclose";x.type="button";x.setAttribute("aria-label","Close (Esc)");x.title="Close (Esc)";x.insertAdjacentHTML("beforeend",'<svg class="ic"><use href="#i-x"/></svg>');x.onclick=closeViewer;
 head.append(el("h2",null,d.name),x);s.append(head);
 const acts=el("div","acts"),mk=(box,t,f,c)=>{const b=el("button","btn"+(c?" "+c:""),t);b.type="button";b.onclick=f;box.append(b);return b};
 mk(acts,"Keep (K)",()=>doKeep([d.id]),"keep");mk(acts,"Trash (T)",()=>askTrash([d.id]),"trash");mk(acts,"Erase (E)",()=>askErase([d.id]),"erase");s.append(acts);
 const tools=el("div","tools");mk(tools,"⟲ Rotate",()=>rot([d.id],"ccw")).title="Shift+R";mk(tools,"⟳ Rotate",()=>rot([d.id],"cw")).title="R";
 mk(tools,"Set event…",()=>askSetEvent([d.id]));mk(tools,"Share-safe copy",()=>askShare([d.id]));
 mk(tools,"Similar",()=>{simId=d.id;simName=d.name;closeViewer();$("#q").value="";syncUI();reset()});s.append(tools);
 const dl=el("dl","dt"),row=(k,v)=>{if(v){dl.append(el("dt",null,k),el("dd",null,v))}};
 row("Date",d.date);row("Place",d.place);row("Event",d.event);row("Project",d.project);row("Source",d.source);row("Folder",d.folder);row("Resolution",d.res);row("Size",d.size);
 if(d.ai!=null)row("AI trash",d.ai.toFixed(2));if(d.used.length)row("Used in",d.used.join(", "));if(d.kept)row("Owner","kept");
 const det=el("div");det.append(el("p","sh","Details"),dl);s.append(det);
 const tg=el("div"),tl=el("div","tags");tg.append(el("p","sh","Tags"),tl);
 d.tags.forEach(t=>{const c=el("span","tag");c.append(el("span",null,t));if(d.mine.includes(t)){const b=el("button",null,"×");b.type="button";b.title="Remove my tag";b.setAttribute("aria-label","Remove my tag "+t);
  b.onclick=async()=>{try{const j=await api("/api/tag",{ids:[d.id],tag:t,op:"remove"});patchTiles(j.items);toast(j.result,j.undo);openViewer(n,{keep:true});loadFacets()}catch(e){errorToast("Refused: "+e.message)}};c.append(b)}tl.append(c)});
 if(!d.tags.length)tl.append(el("span","tag","none"));
 const add=el("input");add.placeholder="Add my tag, Enter";add.setAttribute("aria-label","Add my tag");add.setAttribute("list","mytags");add.autocomplete="off";
 add.onkeydown=async ev=>{if(ev.key==="Enter"&&add.value.trim()){ev.stopPropagation();const t=add.value.trim(),why=tagProblem(t);if(why){errorToast("Refused: "+why);return}
  try{const j=await api("/api/tag",{ids:[d.id],tag:t,op:"add"});patchTiles(j.items);toast(j.result,j.undo);loadFacets();openViewer(n,{keep:true})}catch(e){errorToast("Refused: "+e.message)}}};
 tg.append(add);s.append(tg);
 s.append(el("p","fine","Rotate writes XMP-tiff:Orientation in the sidecar only; the original is never touched. No crop here: make crops as copies in an editor."));
 if(!opt.keep)$("#viewer").focus({preventScroll:true});
 $("#viewer").dataset.current=d.name;  // last: the keys (Space, arrows) act on this item from here on
 if(n>=items.length-20)more()}
function closeViewer(){if(vi<0)return;const id=items[vi]&&items[vi].id;stopVideo();vi=-1;$("#viewer").scrollTop=0;$("#viewer").classList.remove("on");document.body.classList.remove("noscroll");$("#media").replaceChildren();
 delete $("#viewer").dataset.current;const u=$("#unmute");if(u)u.remove();paintSel();const t=$("#t"+id)||lastFocus;if(t&&t.focus)t.focus({preventScroll:true})}
// swipe down on the photo or video closes the large view (phone); Esc and the close button still do
let swipe=null;
$("#stage").addEventListener("touchstart",ev=>{swipe=ev.touches.length===1&&$("#viewer").scrollTop<=0?[ev.touches[0].clientX,ev.touches[0].clientY]:null},{passive:true});
$("#stage").addEventListener("touchend",ev=>{if(!swipe||vi<0)return;const t=ev.changedTouches[0],dx=t.clientX-swipe[0],dy=t.clientY-swipe[1];swipe=null;if(dy>80&&Math.abs(dx)<dy/2)closeViewer()},{passive:true});
$("#vprev").onclick=()=>openViewer(stepVis(vi,-1));$("#vnext").onclick=()=>openViewer(stepVis(vi,1));
// ---------------------------------------------------------------- keys
document.addEventListener("keydown",ev=>{const tg=ev.target,modal=isOn("#modal");
 if(ev.key==="Tab"){const d=topDialog();if(d)trapTab(ev,d);return}
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
 if(plug){sel.clear();dec={};paintSel();$("#count").textContent="";setNote("");$("#helpb").hidden=true;syncUI();const id=v.slice(2),P=$("#plugin"),pv=window.mediaIntakeViews[id];P.replaceChildren();
  if(!pv||typeof pv.render!=="function"){P.append(el("p","warn","Plugin view "+id+" has no render()"));return}
  pv.render(P,{el,api:(p,b)=>api("/plugin/"+id+"/"+p,b),media:p=>"/plugin/"+id+"/media/"+p})}
 else{if(v==="sug")setNote("K = keep, T = trash on the outlined tile; nothing changes until Apply.");syncUI();reset()}}
$$(".nv[data-v]").forEach(b=>b.onclick=()=>{const v=b.dataset.v;
 if(v==="lib"){if(["recent","child","untagged","ev"].includes(navActive()))F={aimin:F.aimin};setView("lib")}
 else if(v==="sug"||v.startsWith("p-"))setView(v);
 else{F={aimin:F.aimin};if(v==="recent")F.added=String((facets&&facets.recent_days)||30);if(v==="child")F.review="child-in-shot";if(v==="untagged")F.review="untagged";$("#q").value="";setView("lib")}
 if(mqPhone.matches&&drawer){drawer=false;layout()}});
let qt=null;$("#q").oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{simId=null;syncUI();reset()},500)};
$("#close").onclick=()=>{confirmBox("Stop the gallery server?",["The page stops working until you run media-intake gallery again. A Trash, Erase or Share-safe copy that is already running keeps going on its own."],false,
 async()=>{try{await api("/api/close",{})}catch(e){}document.body.replaceChildren(el("p","empty big","Gallery stopped. You can close this tab."))});$("#mgo").textContent="Stop the server"};
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
