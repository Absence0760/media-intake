#!/usr/bin/env python3
"""Media intake: copy photos and videos from a source (a phone, camera, drive or laptop folder) into
one library, drop duplicates, set private-looking files aside in quarantine for you to decide on,
and erase them everywhere when you say so.

Run as `media-intake <command>` (cli.py); `python3 -I media_intake/intake.py` works too.
`media-intake --help` lists every command by task, and `media-intake <command> --help` says what
one does, what it touches and whether it deletes. The words used here (batch, source, backup
drive, quarantine, review folder, Keep, Trash, Erase): docs/words.md.

An import, in order:
  media-intake stage SOURCE --kind phone|gopro|drive|folder [--batch NAME]
  media-intake plan BATCH          then scan, apply, review and tag, each with the BATCH
  media-intake backup DRIVE        then, for a phone or camera: media-intake offload BATCH --phone SOURCE

Tags live in <file>.<ext>.xmp sidecars next to each file (digiKam's default
naming); originals are never written (their sha256 drives dedupe and erase).

Layout (docs/intake.md; every path comes from config.toml, docs/configuration.md):
  ~/Pictures/library/<YYYY>/<YYYY-MM>/                photos, original names
  ~/Pictures/library/Screenshots/<YYYY>/
  ~/Videos/personal/Screen recordings/<YYYY>/
  ~/Pictures/library/Messaging/<app>/<YYYY>/
  ~/Videos/personal/<YYYY>/<YYYY-MM>/                 videos (GoPro in <YYYY-MM>/<device id>/)
  <project folder>/<date>-<event>/{phone,gopro/<device id>}/   events routed to a configured project
  ~/.local/share/media-intake/  (0700, never synced or indexed)
    batches/<batch>/raw/          the batch's staged copy, deleted once applied
    batches/<batch>/review/       the review folder: near-duplicates and junk, for a glance
    quarantine/<batch>/           quarantine: flagged files; no assistant or tool ever opens these
    index.json                    sha256 + dHash (videos: vhash) of everything in the library

An AI assistant may run these commands for you, but must never read, view,
montage or describe a file under quarantine/ or a file scan.json flags.
"""
import argparse, collections, datetime, glob, hashlib, json, os, re, shlex, shutil, stat, subprocess, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import dates  # noqa: E402
import devices  # noqa: E402
import exports  # noqa: E402
import owner  # noqa: E402
import sources  # noqa: E402
import videohash  # noqa: E402

CFG = config.get()
HOME = os.path.expanduser("~")
STATE, LIBRARY, VIDEOS = CFG["state"], CFG["library"], CFG["videos"]
PROJECTS = CFG["projects"]  # {key: {name, folder, events, protect}}
LIB_ROOTS = config.library_roots(CFG)  # library, videos, every project folder
VENV_PY = f"{STATE}/venv/bin/python"
SETUP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "setup.sh")
DEPS = ("sudo dnf install perl-Image-ExifTool tesseract tesseract-langpack-eng python3-zxing-cpp "
        "ffmpeg-free (README, Install)")
# gallery.py's thumbnails and H.264 video proxies (copies of photo and video content): erase, forget and
# trash delete them by sha256 (gallery_thumb_forget), and verify-gone reports any left. A thumbnail is keyed by
# content and the rotation baked into it (gallery_thumb_path), so a sha has up to THUMB_TURNS variants.
THUMBS = os.path.join(HOME, ".cache", "media-intake", "thumbs")
PROXIES = os.path.join(HOME, ".cache", "media-intake", "proxies")
HERE = os.path.dirname(os.path.abspath(__file__))
IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".gpr"}
VID = {".mp4", ".mov", ".3gp", ".mkv", ".webm"}


# ---------------------------------------------------------------- helpers
def private_dir(p):
    # makedirs applies mode to the leaf only; keep the state root itself 0700 too.
    if p != STATE and os.path.abspath(p).startswith(os.path.abspath(STATE) + os.sep):
        private_dir(STATE)
    os.makedirs(p, mode=0o700, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _umask():
    """The process umask (os has no read-only getter, so set and restore it)."""
    u = os.umask(0)
    os.umask(u)
    return u


def bdir(batch):
    return os.path.join(STATE, "batches", batch)


def shown_path(p):
    """p for a line a person copies: ~/... under HOME, shell-quoted."""
    rel = os.path.relpath(p, HOME)
    return "~/" + shlex.quote(rel) if not rel.startswith("..") else shlex.quote(p)


def batch_names():
    """The staged batches (folders under batches/), most recently changed first."""
    d = os.path.join(STATE, "batches")
    try:
        names = [n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))]
    except FileNotFoundError:
        return []
    return sorted(names, key=lambda n: (-os.path.getmtime(os.path.join(d, n)), n))


def need_batch(name, staged=True):
    """Exit with one plain sentence unless name is a batch. Every command that takes a BATCH calls it first,
    before it reads or writes anything of the batch. staged=True wants a staged batch (batches/<name>);
    staged=False also takes one that only has a quarantine folder (near-<batch>, xcheck-<batch>), as quarantine
    and release do. The sentence names the batches that exist and the
    command that lists them all."""
    if BATCH_NAME.match(name or ""):
        if os.path.isdir(bdir(name)):
            return
        if os.path.isdir(os.path.join(STATE, "quarantine", name)):
            if not staged:
                return
            sys.exit(f"{name!r} has no staged files, only quarantined ones: see them with "
                     f"media-intake review {shlex.quote(name)}")
    have = batch_names()
    if not have:
        sys.exit(f"no batch named {name!r}: nothing is staged yet. "
                 "Start with: media-intake stage SRC --kind phone|gopro|drive|folder")
    shown = ", ".join(have[:10]) + (f" (and {len(have) - 10} more)" if len(have) > 10 else "")
    sys.exit(f"no batch named {name!r}. Batches, newest first: {shown}. "
             f"List them all with: ls {shown_path(os.path.join(STATE, 'batches'))}")


def plural(n, word, many=None):
    """'1 file', '2 files', '1,200 files'."""
    return f"{n:,} {word if n == 1 else many or word + 's'}"


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def dhash(p):
    from PIL import Image, ImageOps
    try:
        im = ImageOps.exif_transpose(Image.open(p)).convert("L").resize((9, 8), Image.LANCZOS)
    except Exception:
        return None, 0, 0
    px = list(im.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    w, h = Image.open(p).size
    return bits, w, h


def thumb_forget(path):
    """Delete freedesktop thumbnails of path (GNOME Files, image viewers)."""
    # Same escaping as GLib's g_filename_to_uri (reserved sub-delims stay literal).
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


THUMB_TURNS = (0, 90, 180, 270)  # the clockwise turns a gallery thumbnail can have baked in


def gallery_thumb_path(sha, turn):
    """gallery.py's thumbnail of this content with `turn` degrees (clockwise) of rotation baked in on top of
    the file's own EXIF orientation: <sha256>-r<turn>.jpg. Byte-identical copies rotated alike share it; a copy
    rotated differently (its sidecar's XMP-tiff:Orientation) gets its own."""
    return os.path.join(THUMBS, f"{sha}-r{turn % 360}.jpg")


def gallery_cache_paths(sha):
    """gallery.py's cached copies of this content (keyed by sha256): every thumbnail variant (each rotation,
    and <sha256>.jpg, the content-only key before rotation was part of it) and its H.264 video proxy."""
    return ([os.path.join(THUMBS, sha + ".jpg")] + [gallery_thumb_path(sha, t) for t in THUMB_TURNS]
            + [os.path.join(PROXIES, sha + ".mp4")])


def gallery_thumb_forget(sha):
    """Delete gallery.py's cached thumbnails (every rotation, and the older content-only key) and video proxy
    of this content (keyed by sha256). Returns how many went."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        return 0
    n = 0
    for p in gallery_cache_paths(sha):
        if os.path.lexists(p):
            os.remove(p)
            n += 1
    return n


def sidecar(path):
    """The XMP sidecar of a media file: <file>.<ext>.xmp (digiKam and darktable naming)."""
    return path + ".xmp"


def drop_sidecar(path):
    """Delete path's sidecar (tags, place, labels) when path itself is deleted. Returns 1 if one went."""
    x = sidecar(path)
    if os.path.lexists(x) and not os.path.isdir(x):
        os.remove(x)
        return 1
    return 0


def free_path(dst):
    """dst, else the first <stem>_<n><ext> where neither a file nor its sidecar exists. Every move onto a
    library or quarantine path goes through this (or skips): a move never replaces a file someone filed
    there meanwhile, nor its sidecar. Callers hold the library lock, so nothing else fills the place
    between this check and the move."""
    stem, ext = os.path.splitext(dst)
    p, i = dst, 1
    while os.path.lexists(p) or os.path.lexists(sidecar(p)):
        p, i = f"{stem}_{i}{ext}", i + 1
    return p


def orphan_sidecar(p):
    """A <file>.<media ext>.xmp whose media file no longer exists."""
    base, ext = os.path.splitext(p)
    return (ext.lower() == ".xmp" and os.path.splitext(base)[1].lower() in IMG | VID
            and not os.path.lexists(base))


def has_gps(path):
    if shutil.which("exiftool"):
        r = subprocess.run(["exiftool", "-q", "-n", "-ee", "-GPSLatitude", "-GPSCoordinates", "-s3", path],
                           capture_output=True, text=True)
        return bool(r.stdout.strip())
    from PIL import Image
    try:
        return bool(Image.open(path).getexif().get_ifd(34853))
    except Exception:
        return False


def category(rel, kind, app=None):
    """app: the messaging app stage found the file's source folder to be an export of (manifest "app")."""
    p = rel.split("/")
    sub = p[1] if len(p) > 2 else ""
    ext = os.path.splitext(rel)[1].lower()
    if kind == "gopro":
        return "gopro"
    if kind in ("drive", "folder"):
        return sources.drive_category(rel, app)
    app = sources.extra_app(rel)
    if app:
        return f"messaging:{app}"
    if p[0] in ("DCIM", "Pictures") and sub == "Screenshots":  # Pixel and stock Android use Pictures/
        return "screenrec" if ext in VID else "screenshot"  # some phones save screen recordings there too
    if p[0] == "DCIM":
        return {"Videocaptures": "screenrec", "Screen recordings": "screenrec"}.get(sub, "video" if ext in VID else "photo")
    if p[0] == "Pictures" and sub:
        return f"messaging:{sub}"
    return "photo"


# ---------------------------------------------------------------- stage
# stage without --batch reads Make/Model/serial from this many files (walk order) to name the batch.
NAME_SAMPLE = 200


def default_batch(root, kind, files, origin):
    """'<device id>-<YYYYMMDD>' (devices.batch_name) from a sample of the source files, read only.
    A batch of that name staged from another root gets -2, -3, ...; one from the same root is resumed."""
    sample = [p for p, _ in files[:NAME_SAMPLE]]
    try:
        metas = dates.read_meta(sample, gpmf=False, state=STATE)
    except ValueError as e:  # a corrupt device salt
        sys.exit(str(e))
    base = devices.batch_name([devices.Device(m["Device"], m["DeviceModel"], m["DeviceLabel"])
                               if m.get("Device") else None for m in metas.values()], kind, datetime.date.today())
    name, n = base, 1
    while True:
        man = load(os.path.join(bdir(name), "manifest.json"))
        if man is None or man.get("root") == origin:
            return name
        n += 1
        name = f"{base}-{n}"


PHONE_TIP = ("A phone shows up only while it is unlocked and set to File transfer (pull down the USB notification); "
             "then find it with: ls /run/user/$UID/gvfs/")
# Why stage found no source folder, or no media in it, by --kind.
STAGE_WHERE = {"phone": PHONE_TIP, "gopro": "Is the camera on and connected, or its card mounted?",
               "drive": "Is the drive mounted? Its folder is under /run/media/$USER/."}
STAGE_EMPTY = {
    "phone": "It looks only under " + ", ".join(f"{d}/" for d in sources.PHONE_ALLOW + sources.PHONE_ALLOW_EXTRA)
             + ". Pass the phone's storage folder (the one holding DCIM). " + PHONE_TIP,
    "gopro": "It looks only under DCIM/<nnn>GOPRO/. Pass the card's or camera's top folder (the one holding DCIM).",
    "drive": "It skips system, app and hidden folders, and files that are not photos or videos.",
    "folder": "It skips hidden folders, code (git) folders, and files that are not photos or videos.",
}


def cmd_stage(a):
    root = os.path.abspath(a.src)
    if a.batch is not None and not BATCH_NAME.match(a.batch):
        sys.exit(f"--batch {a.batch!r}: letters, digits, '.', '_' and '-' only")
    if not os.path.isdir(root):  # before any batch folder exists: a typo leaves nothing behind
        sys.exit(f"{a.src}: {'not a folder' if os.path.exists(root) else 'no such folder'}. "
                 + STAGE_WHERE.get(a.kind, ""))
    man = {}
    skipped = collections.Counter()
    extra = {}
    if a.kind == "drive":  # refuses the laptop's disk and the backup drive; uuid lets offload check it
        try:
            extra = dict(mount=sources.check_drive_root(root), uuid=sources.drive_uuid(root))
        except sources.Denied as e:
            sys.exit(f"refused: {e}")
    if a.kind == "folder":  # a laptop folder: refuses HOME, state, library, app data, keys, repos, outside HOME
        try:
            sources.check_folder_root(root)
        except sources.Denied as e:
            sys.exit(f"refused: {e}")
    only = None
    if a.only:  # drive_queue.py chunks: one relative path per line
        with open(a.only) as f:
            only = {l.rstrip("\n") for l in f if l.strip()}
    files = [(path, rel) for path, rel in sources.walk(root, a.kind) if only is None or rel in only]
    if not files:  # no batch is created for nothing
        if only is not None:  # a queue chunk whose listed files are gone from the source
            sys.exit(f"nothing to stage in {a.src}: none of the {plural(len(only), 'file')} in {a.only} is there now")
        sys.exit(f"nothing to stage in {a.src}: no photos or videos found for --kind {a.kind}. "
                 + STAGE_EMPTY[a.kind])
    if a.batch is None:
        a.batch = default_batch(root, a.kind, files, os.path.abspath(a.origin or root))
        print(f"batch {a.batch}")
    raw = private_dir(os.path.join(private_dir(bdir(a.batch)), "raw"))
    prog = Progress("stage", f"copying {len(files):,} media files into the batch (size-checked)",
                    unit="files staged", hits=None, total=len(files))
    # Google Takeout JSON / iCloud CSV beside the files, a Telegram export's result.json (exports.py): their
    # dates and places ride in the manifest.
    exported, ec = exports.scan(root, files) if a.kind in ("drive", "folder") else ({}, {})
    for path, rel in files:
        dst = os.path.join(raw, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        st = os.stat(path)
        if not (os.path.exists(dst) and os.path.getsize(dst) == st.st_size):
            shutil.copyfile(path, dst)
            os.utime(dst, (st.st_mtime, st.st_mtime))
        if os.path.getsize(dst) != st.st_size:
            sys.exit(f"size mismatch after copy: {rel}")
        origin = os.path.join(os.path.abspath(a.origin), rel) if a.origin else path
        man[rel] = dict(src=origin, size=st.st_size, **({"export": exported[rel]} if rel in exported else {}))
        app = sources.export_app(root, rel) if a.kind in ("drive", "folder") else None
        if app:  # the staged folder may be the export itself: plan's category can't see that from rel
            man[rel]["app"] = app
        prog.tick()
    for dirpath, _, names in os.walk(root) if a.kind == "gopro" else []:
        for n in names:
            if os.path.splitext(n)[1].lower() in sources.CAMERA_JUNK_EXT:
                skipped["gopro preview (.lrv/.thm)"] += 1
    save(os.path.join(bdir(a.batch), "manifest.json"), dict(kind=a.kind, root=os.path.abspath(a.origin or root), files=man, **extra))
    print(f"staged {plural(len(man), 'media file')} into {raw}")
    if exported or ec.get("ambiguous"):  # only the export kinds this source had
        got = ", ".join(f"{name} {ec[k]}" for k, name in (("google", "Google Takeout"), ("icloud", "iCloud"),
                                                          ("telegram", "Telegram")) if ec[k])
        print("  dates from cloud export files: " + (got or "none")
              + (f" ({ec['places']} with a place)" if ec["places"] else "")
              + (f"; {ec['ambiguous']} ambiguous, ignored" if ec["ambiguous"] else ""))
    if ec.get("unreadable"):
        skipped["folders whose cloud export metadata could not be read"] = ec["unreadable"]
    for k, what in exports.REFUSED.items():  # a symlinked, oversized or non-UTF-8 JSON/CSV: its media get no export date
        if ec.get(k):
            skipped[what] = ec[k]
    for k, v in skipped.items():
        print(f"  skipped {v} {k}")
    if man:
        next_hint(["plan", a.batch])


# ---------------------------------------------------------------- plan
# plan's "dates:" line: where each file's date came from (dates.SOURCES), in words.
DATE_FROM = {"exif": "from the camera", "video": "from the video's own date", "gps": "from GPS time",
             "export": "from a cloud export", "filename": "from the file name", "folder": "from the folder name",
             "mtime": "from file times (no camera date found)", "offset": "shifted by --clock-offset"}


def cmd_plan(a):
    need_batch(a.batch)
    b = bdir(a.batch)
    man = load(os.path.join(b, "manifest.json"))
    if man is None:  # stage stopped before it wrote the manifest
        sys.exit(f"{a.batch} was never fully staged: run media-intake stage again with --batch {a.batch}")
    raw = os.path.join(b, "raw")
    kind = man["kind"]
    routed, personal = {}, {}  # day -> (project, slug) / day -> slug
    for t in a.event or []:
        spec, _, day = t.partition("@")
        key, _, ev = spec.rpartition("/")
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day) or not config.SLUG.match(ev):
            sys.exit(f"--event wants [PROJECT/]SLUG@YYYY-MM-DD (slug: lowercase, digits, dashes), got {t}")
        try:
            datetime.date.fromisoformat(day)
        except ValueError:
            sys.exit(f"--event {t}: {day} is not a date")
        if key and key not in PROJECTS:
            sys.exit(f"--event {t}: no project {key!r} in {CFG['path']} (known: {', '.join(PROJECTS) or 'none'})")
        proj = PROJECTS[key] if key else config.project_for_event(ev, CFG)
        if proj:
            routed[day] = (proj, ev)  # camera files of that day go to the project's folder
        else:
            personal[day] = ev  # tags only, files still go by date
    index = load(os.path.join(STATE, "index.json"), {})
    by_sha = {v["sha"]: k for k, v in index.items()}

    paths = [os.path.join(raw, rel) for rel in man["files"]]
    prog = Progress("plan", f"reading dates, then comparing files to find duplicates ({plural(len(paths), 'file')})",
                    unit="files read", hits=None, total=len(paths))
    exp = {os.path.abspath(os.path.join(raw, rel)): f["export"] for rel, f in man["files"].items() if f.get("export")}
    try:
        metas = None
        if exp:  # a cloud export's date (exports.py) is dates.py's "export" source
            metas = dates.read_meta(paths)
            for p, e in exp.items():
                metas[p]["ExportTime"] = e["time"]
        dated = dates.resolve_all(paths, dates.clock_offsets(a.clock_offset), metas=metas)
    except ValueError as e:
        sys.exit(str(e))
    rows = []
    for rel in sorted(man["files"]):
        p = os.path.join(raw, rel)
        dd = dated[p]
        ext = os.path.splitext(rel)[1].lower()
        dh, w, h = dhash(p) if ext in IMG else (None, 0, 0)
        vh, vnote = videohash.video_hash(p) if ext in VID else (None, None)
        rows.append(dict(rel=rel, sha=sha256(p), size=os.path.getsize(p), date=dd.dt.isoformat(), date_src=dd.src,
                         date_tz=dd.tz, date_from=dd.base, date_note=dd.note, date_suspect=dd.suspect,
                         chapter_of=dd.chapter_of, device=dd.device, device_model=dd.model,
                         burst_id=dd.burst_id,  # the camera's BurstUUID / GCamera BurstID, for bursts.py
                         export_gps=exports.gps_of(man["files"][rel]),  # tag's place when the file has no GPS
                         cat=category(rel, kind, man["files"][rel].get("app")), dhash=dh, w=w, h=h, ext=ext,
                         vhash=vh, vhash_note=vnote))  # videohash frame dHashes ([] + a note: not compared)
        prog.tick()

    pref = {"photo": 5, "video": 5, "gopro": 5, "screenshot": 3, "screenrec": 3}
    CAMERA = ("photo", "gopro")  # camera originals: one is never a near-duplicate of another
    def rank(r):
        return (pref.get(r["cat"], 1), "/Restored/" not in r["rel"], r["w"] * r["h"], r["size"])

    action = {}
    trashed = load(os.path.join(STATE, "trashed.json"), {}) or {}
    for r in rows:  # duplicates of something already in the library
        if trashed.get(r["sha"]) == r["size"]:
            action[r["rel"]] = ("delete", "trashed before")
        elif r["sha"] in by_sha:
            action[r["rel"]] = ("delete", f"duplicate of library {by_sha[r['sha']]}")
    groups = collections.defaultdict(list)
    for r in rows:
        if r["rel"] not in action:
            groups[r["sha"]].append(r)
    for g in groups.values():
        g.sort(key=rank, reverse=True)
        for r in g[1:]:
            action[r["rel"]] = ("delete", f"duplicate of {g[0]['rel']}")
    def near_pass(key, camera, same, lib_same):
        """Route near-copies to review: within the batch (same(x, y)), then a non-camera file against the
        library index (lib_same(x, entry)). Photos by dHash, videos by videohash; never deleted here. Two
        camera originals (cats in camera) are never merged. A video that couldn't be hashed has vhash []
        (near nothing) and its vhash_note says why; plan prints the count."""
        items = [r for r in rows if r.get(key) is not None and r["rel"] not in action]
        lib = [(k, v) for k, v in index.items() if v.get(key) is not None]
        what = "photos" if key == "dhash" else "videos"
        near = Progress("plan", f"looking for look-alike {what} ({len(items):,} in this batch, "
                                f"{len(lib):,} already in the library)", unit=f"{what} compared", hits=None,
                        total=len(items)) if items else None
        for i, x in enumerate(items):
            if near:
                near.tick()
            if x["rel"] in action:
                continue
            for y in items[i + 1:]:
                if y["rel"] in action or (x["cat"] in camera and y["cat"] in camera) or not same(x, y):
                    continue  # burst shots and time-lapse frames (phone or GoPro) are never merged
                keep, drop = (x, y) if rank(x) >= rank(y) else (y, x)
                action[drop["rel"]] = ("review", f"near-duplicate of {keep['rel']}")
                if drop is x:
                    break
            if x["rel"] not in action and x["cat"] not in camera:
                for k, v in lib:
                    if lib_same(x, v):
                        action[x["rel"]] = ("review", f"near-duplicate of library {k}")
                        break

    near_pass("dhash", CAMERA, lambda x, y: bin(x["dhash"] ^ y["dhash"]).count("1") <= 2
              and (x["w"] >= x["h"]) == (y["w"] >= y["h"]),
              lambda x, v: bin(x["dhash"] ^ v["dhash"]).count("1") <= 2)
    near_pass("vhash", CAMERA + ("video",), lambda x, y: videohash.near(x["vhash"], y["vhash"]),
              lambda x, v: videohash.near(x["vhash"], v["vhash"]))
    for r in rows:
        if r["rel"] not in action and (r["size"] == 0 or (r["w"] and max(r["w"], r["h"]) < 200)):
            action[r["rel"]] = ("review", "junk: tiny or empty")

    plan, used = [], set()
    for r in rows:
        d = datetime.datetime.fromisoformat(r["date"])
        base = os.path.basename(r["rel"])
        # Every file gets a library place (lib_dst), even near-duplicates and junk:
        # if one is also flagged and you keep it, `release` needs somewhere to put it.
        day = d.strftime("%Y-%m-%d")
        if day in routed and r["cat"] in ("photo", "video", "gopro"):
            folder = os.path.join(routed[day][0]["folder"], f"{day}-{routed[day][1]}",
                                  "gopro" if r["cat"] == "gopro" else "phone")
        elif r["cat"] in ("video", "gopro") or (r["cat"] == "screenrec"):
            folder = os.path.join(VIDEOS, "Screen recordings" if r["cat"] == "screenrec" else "",
                                  f"{d:%Y}", "" if r["cat"] == "screenrec" else f"{d:%Y-%m}")
        elif r["cat"] == "screenshot":
            folder = os.path.join(LIBRARY, "Screenshots", f"{d:%Y}")
        elif r["cat"].startswith("messaging:"):
            folder = os.path.join(LIBRARY, "Messaging", r["cat"].split(":", 1)[1], f"{d:%Y}")
        else:
            folder = os.path.join(LIBRARY, f"{d:%Y}", f"{d:%Y-%m}")
        if r["device"] and (r["cat"] == "gopro" or devices.gopro_name(base)):
            # A dedicated camera's numbering repeats across cameras: one folder per device, never _1.
            folder = os.path.join(folder, r["device"])
        lib_dst = os.path.normpath(os.path.join(folder, base))
        stem, ext = os.path.splitext(lib_dst)
        i = 1
        while lib_dst.casefold() in used or (os.path.exists(lib_dst) and sha256(lib_dst) != r["sha"]):
            lib_dst, i = f"{stem}_{i}{ext}", i + 1
        if r["rel"] in action:
            act, why = action[r["rel"]]
            dst = None if act == "delete" else os.path.join(b, "review", r["rel"])
            if act == "delete":
                lib_dst = None
            else:
                used.add(lib_dst.casefold())
        else:
            act, why = "file", ""
            dst = lib_dst
            used.add(dst.casefold())
        plan.append(dict(r, action=act, why=why, dst=dst, lib_dst=lib_dst,
                         event=(routed[day][1] if day in routed else personal.get(day)),
                         project=(routed[day][0]["name"] if day in routed and r["cat"] in ("photo", "video", "gopro")
                                  else None)))
    save(os.path.join(b, "plan.json"), plan)
    c = collections.Counter(p["action"] for p in plan)
    print(f"plan: {plural(len(plan), 'file')}: {c['file']:,} to file, {plural(c['delete'], 'exact duplicate')} "
          f"to drop, {c['review']:,} to review. Nothing has moved yet.")
    print("dates: " + ", ".join(f"{n:,} {DATE_FROM.get(k, 'from ' + str(k))}"
                                for k, n in collections.Counter(p["date_src"] for p in plan).most_common()))
    devs = collections.Counter(p["device"] for p in plan if p["device"])
    if devs:
        print("devices: " + ", ".join(f"{k} {v:,}" for k, v in sorted(devs.items())))
    for label, sel in (("camera dates replaced by GPS", lambda p: p["date_src"] == "gps" and p["date_note"]
                        and not p["chapter_of"]),
                       ("GoPro chapters dated by their first chapter", lambda p: p["chapter_of"]),
                       ("dates shifted by --clock-offset", lambda p: p["date_src"] == "offset"),
                       ("dates to check (clock-reset default, camera and GPS disagree, or nothing plausible; "
                        "--clock-offset fixes a wrong clock)", lambda p: p["date_suspect"])):
        hit = sorted(p["rel"] for p in plan if sel(p))
        if hit:
            print(f"  {len(hit)} {label}: " + ", ".join(hit[:5]) + (" ..." if len(hit) > 5 else ""))
    unhashed = collections.defaultdict(list)
    for p in plan:
        if p.get("vhash_note"):
            unhashed[p["vhash_note"]].append(p["rel"])
    for note, rels in sorted(unhashed.items()):
        print(f"  {len(rels)} videos not checked for near-duplicates ({note}): "
              + ", ".join(sorted(rels)[:5]) + (" ..." if len(rels) > 5 else ""))
    folders = collections.Counter(os.path.dirname(p["dst"]) for p in plan if p["action"] == "file")
    if folders:
        print("will be filed in:")
    for k, n in sorted(folders.items()):
        print(f"  {shown_path(k)}  ({n:,})")
    next_hint(["scan", a.batch])


# ---------------------------------------------------------------- scan
def cmd_scan(a):
    need_batch(a.batch)
    b = bdir(a.batch)
    if not os.path.isdir(os.path.join(b, "raw")):  # apply removes raw/ once everything has moved
        sys.exit(f"nothing to scan in {a.batch}: its staged files have already been moved by apply"
                 if os.path.exists(os.path.join(b, "plan.json")) else
                 f"nothing to scan in {a.batch}: no staged files (run media-intake stage again with --batch {a.batch})")
    if not os.path.exists(VENV_PY):
        sys.exit(f"no venv: run bash {SETUP} first")
    out = os.path.join(b, "scan.json")
    r = subprocess.run([VENV_PY, "-I", os.path.join(HERE, "scan_sensitive.py"), os.path.join(b, "raw"), out])
    if r.returncode:
        sys.exit(r.returncode)
    next_hint(["apply", a.batch])  # flagged files go to quarantine there; the review page comes after


def index_entry(sha, size, dh, vh=None, vnote=None, **extra):
    """One index.json entry, built the same way by apply and release: sha256, size and dHash (None for a
    video), and for a video its videohash frame dHashes (vhash) as plan stores them. A video that couldn't
    be hashed keeps vhash [] and its vhash_note (why), so it is never read as "no near-copy". Readers
    take an entry with no vhash key (an image, or an index written before videohash) as not a video."""
    e = dict(sha=sha, size=size, dhash=dh)
    if vh is not None:
        e["vhash"] = vh
        if vnote:
            e["vhash_note"] = vnote
    e.update(extra)
    return e


# ---------------------------------------------------------------- apply
def missing_detectors():
    """The Fedora packages of the document detectors scan needs and can't find (tesseract, zxing-cpp), named
    as doctor names them (cli.SYSTEM_TOOLS / SYSTEM_MODULES: one table for both)."""
    import cli
    missing = [] if shutil.which("tesseract") else [cli.SYSTEM_TOOLS["tesseract"]]
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        missing.append(cli.SYSTEM_MODULES["zxingcpp"])
    return missing


def cmd_apply(a):
    need_batch(a.batch)
    b = bdir(a.batch)
    plan = load(os.path.join(b, "plan.json"))
    if plan is None:
        sys.exit(f"plan has not run for {a.batch}. Run: media-intake plan {a.batch}")
    flags = load(os.path.join(b, "scan.json"))
    if flags is None and not a.no_scan:
        sys.exit(f"scan has not run for {a.batch}. Run: media-intake scan {a.batch}")
    flags = flags or {}
    missing = missing_detectors()
    if missing and not a.no_scan and not a.allow_missing_detectors:
        sys.exit(f"Nothing was filed: the ID, card and 2FA-code checks need {', '.join(missing)}.\n"
                 f"Install:  sudo dnf install -y {' '.join(missing)}\n"
                 f"Then run: media-intake scan {a.batch}\n"
                 "(media-intake doctor checks everything else the tool needs.)")
    raw = os.path.join(b, "raw")
    quar = private_dir(os.path.join(private_dir(os.path.join(STATE, "quarantine")), a.batch))
    index = load(os.path.join(STATE, "index.json"), {})
    qman = load(os.path.join(quar, "quarantine.json"), {})
    c = collections.Counter()
    moved = 0  # plan entries whose place was taken since `plan`: filed under a free name instead
    prog = Progress("apply", f"moving {len(plan):,} planned files (library, review folder, quarantine)",
                    unit="files done", hits=None, total=len(plan))
    for p in plan:
        prog.tick()
        src = os.path.join(raw, p["rel"])
        if not os.path.exists(src):
            continue
        if p["rel"] in flags and p["action"] != "delete":
            dst = os.path.join(quar, p["rel"])
            if os.path.lexists(dst):  # quarantine.json is keyed by rel: never replace the file it describes
                c["left in raw (its quarantine place is taken)"] += 1
                continue
            os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
            os.replace(src, dst)
            os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
            qman[p["rel"]] = dict(reasons=flags[p["rel"]], sha=p["sha"], size=p["size"],
                                 dst=p["dst"] if p["action"] == "file" else p.get("lib_dst"))
            c["quarantined"] += 1
        elif p["action"] == "delete":
            os.remove(src)
            c["deleted (exact duplicate)"] += 1
        else:
            dst = free_path(p["dst"])
            if dst != p["dst"]:  # something was filed there after `plan`: keep both
                p["dst"], moved = dst, moved + 1
                if p["action"] == "file":
                    p["lib_dst"] = dst
            os.makedirs(os.path.dirname(p["dst"]), exist_ok=True)
            shutil.move(src, p["dst"])
            if p["action"] == "file":
                index[p["dst"]] = index_entry(p["sha"], p["size"], p["dhash"], p.get("vhash"), p.get("vhash_note"))
                c["filed"] += 1
            else:
                c["to review"] += 1
    save(os.path.join(quar, "quarantine.json"), qman)
    save(os.path.join(STATE, "index.json"), index)
    if moved:  # tag, offload and the review page read each file's place from the plan
        save(os.path.join(b, "plan.json"), plan)
        print(f"{plural(moved, 'file')} went to a free name (<name>_<n>): their planned place was taken since `plan`")
    left = [os.path.join(r, f) for r, _, fs in os.walk(raw) for f in fs]
    if not left and os.path.isdir(raw):  # a second apply finds raw/ already removed: nothing to do
        shutil.rmtree(raw)
    done = [plural(c["filed"], "file") + " filed" if c["filed"] else "",
            plural(c["deleted (exact duplicate)"], "exact duplicate") + " dropped" if c["deleted (exact duplicate)"] else "",
            f"{c['quarantined']:,} quarantined" if c["quarantined"] else "",
            f"{c['to review']:,} set aside for review" if c["to review"] else ""]
    print("apply: " + (", ".join(x for x in done if x) or "nothing to move (already applied)"))
    if left:
        taken = c["left in raw (its quarantine place is taken)"]
        print(f"{plural(len(left), 'file')} left in the batch folder {os.path.abspath(raw)}"
              + (f" ({taken:,} because their quarantine place is taken)" if taken else ""))
    if c["quarantined"]:
        print(f"quarantine (you open it, nobody else): xdg-open {os.path.abspath(quar)}")
    if c["to review"]:
        print(f"review folder (a glance): xdg-open {os.path.abspath(os.path.join(b, 'review'))}")
    gps = [p["dst"] for p in plan if p["action"] == "file" and p["rel"] not in flags
           and os.path.exists(p["dst"]) and has_gps(p["dst"])]
    with open(os.path.join(b, "gps.txt"), "w") as f:
        f.write("\n".join(gps) + "\n")
    print(f"{len(gps)} filed files carry GPS location (list: {os.path.join(b, 'gps.txt')}); "
          f"use `share-copy` before posting any of them")
    # From what is there now, not this run's counts: a second apply moves nothing but the review is still due.
    reviewing = os.path.isdir(os.path.join(b, "review")) and any(fs for _, _, fs in os.walk(os.path.join(b, "review")))
    next_hint(["review" if qman or reviewing else "tag", a.batch])


# ---------------------------------------------------------------- quarantine review
def cmd_quarantine(a):
    need_batch(a.batch, staged=False)
    quar = os.path.join(STATE, "quarantine", a.batch)
    q = load(os.path.join(quar, "quarantine.json"), {}) or {}
    if not q:
        print(f"nothing in quarantine for {a.batch}.")
        return
    c = collections.Counter(r for v in q.values() for r in v["reasons"])
    print(f"{plural(len(q), 'file')} in quarantine: {quar}")
    for k, v in c.most_common():
        print(f"  {k:28} {v}")
    man = load(os.path.join(bdir(a.batch), "manifest.json"), {}) or {}
    erase = ["media-intake", "erase", a.batch]
    if man.get("kind") in ("phone", "gopro") and man.get("root"):
        # erase --phone also deletes the originals on the phone or camera the batch came from. A laptop folder
        # batch is refused there (folder_refusal), erase's device checks are the phone layout's (not a source
        # drive's), and a quarantine-only batch has no source: plain erase for those.
        erase += ["--phone", man["root"]]
    elif man.get("kind") == "drive" and man.get("root"):
        # The originals are on the drive the batch was staged from: erase --drive deletes identical copies anywhere
        # under that folder (checked to be a mounted drive before anything is asked or deleted).
        erase += ["--drive", man["root"]]
    print(f"Open it yourself:\n  xdg-open {shlex.quote(os.path.abspath(quar))}\nFor any false positive, run\n"
          f"  media-intake release {shlex.quote(a.batch)} <file>...\n"
          f"then erase the rest with\n  {' '.join(shlex.quote(x) for x in erase)}"
          + ("\n  (--phone also deletes them from the phone or camera)" if "--phone" in erase else "")
          + ("\n  (--drive also deletes them from the drive they came from, then overwrites its free space)"
             if "--drive" in erase else ""))


def cmd_release(a):
    need_batch(a.batch, staged=False)
    quar = os.path.join(STATE, "quarantine", a.batch)
    if not os.path.isdir(quar):  # nothing went to quarantine, or erase has removed it
        sys.exit(f"nothing in quarantine for {a.batch}: it is empty or already erased")
    q = load(os.path.join(quar, "quarantine.json"), {}) or {}
    index = load(os.path.join(STATE, "index.json"), {})
    for f in a.files:
        rel = os.path.relpath(os.path.abspath(f), quar) if os.path.isabs(f) or f.startswith(".") else f
        if rel not in q or not q[rel]["dst"]:
            print(f"not in quarantine (or was a near-duplicate): {f}")
            continue
        want = q[rel]["dst"]
        # Its old place may hold another file by now (a later apply filed a different IMG_1.jpg there):
        # never replace it or its sidecar; the released file takes a free <name>_<n> beside it.
        dst = free_path(want)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(quar, rel), dst)
        # Same mode a normal intake copy gets (shutil.copyfile honours the
        # umask), not a fixed world-readable 0644.
        os.chmod(dst, 0o666 & ~_umask())
        if os.path.isfile(sidecar(os.path.join(quar, rel))):  # a near-duplicate's own tags; none are created
            shutil.move(sidecar(os.path.join(quar, rel)), sidecar(dst))
        # Hashed from the released file, as plan hashes a staged one: quarantine.json keeps no hashes.
        ext = os.path.splitext(dst)[1].lower()
        dh = dhash(dst)[0] if ext in IMG else None
        vh, vnote = videohash.video_hash(dst) if ext in VID else (None, None)
        index[dst] = index_entry(q[rel]["sha"], q[rel]["size"], dh, vh, vnote, released=True)
        thumb_forget(os.path.join(quar, rel))
        del q[rel]
        print(f"released -> {dst}" + (f"   ({os.path.basename(want)} is taken there: kept both)" if dst != want else ""))
    save(os.path.join(quar, "quarantine.json"), q)
    save(os.path.join(STATE, "index.json"), index)


# ---------------------------------------------------------------- erase
# Trees under HOME that can't hold user media (toolchains, package and build
# caches, container storage, browser test builds). Everything else in HOME is
# swept, hidden folders, ~/.var/app and the Trash included.
SWEEP_SKIP = (".local/share/containers", ".cache/containers", ".cache/ms-playwright", ".cache/go-build",
              ".cache/pip", ".cache/pnpm", ".cache/mesa_shader_cache", ".cache/nvidia", ".cache/thumbnails",
              ".cache/flutter-sdks", ".cargo", ".rustup", ".nvm", ".pyenv", ".tfenv", ".gradle", ".m2",
              ".pub-cache", ".local/share/flatpak", ".local/share/pnpm", ".local/share/uv", "go/pkg",
              "development", "Android/Sdk", ".android/avd", ".local/share/media-intake/venv",
              os.path.relpath(os.path.join(STATE, "venv"), HOME))
SWEEP_SKIP_NAMES = {".git", "node_modules", "__pycache__"}
NEAR_MAX = 4  # dHash bits: recompressed or resized copies of the same picture
WIPE_NAME = ".media-intake-wipe"
LAPTOP_MOUNTS = re.compile(r"^(/|/home|/var|/tmp|/boot.*)$")


def file_uris(path):
    """The URIs apps record for path: file://, and mtp:// for a phone browsed through gvfs."""
    q = lambda x: urllib.parse.quote(x, safe="/!$&'()*+,;=:@")  # noqa: E731  GLib's escaping
    path = os.path.abspath(path)
    uris = ["file://" + q(path)]
    m = re.match(r"^/run/user/\d+/gvfs/mtp:host=([^/]+)(/.*)$", path)
    if m:
        uris.append(f"mtp://{m.group(1)}{q(m.group(2))}")
    return uris


# GNOME's per-file metadata (~/.local/share/gvfs-metadata: emblems, icon positions, custom icons, set through
# gio by Files and other apps) is keyed by path, in a binary database only gvfsd-metadata writes. GLib clears an
# entry itself when a file is deleted *through gio* (g_file_delete -> GVfs local_file_removed -> the daemon's
# Remove), which is why device_remove's `gio remove` needs nothing more; a plain os.remove leaves the entry. So
# before erase deletes (or moves out of place) a file on this machine or a drive, it lists the file's
# metadata:: keys with `gio info` and unsets each with `gio set -t unset`, which only works while the file
# still exists. The database is never edited by hand. Checked 2026-11-14 against
# https://gitlab.gnome.org/GNOME/glib/-/raw/main/gio/glocalfile.c, .../gio/gio-tool-info.c,
# .../docs/reference/gio/gio.rst and https://gitlab.gnome.org/GNOME/gvfs/-/raw/master/client/gdaemonvfs.c.
GIO_TIMEOUT = 20  # seconds per gio or localsearch call; after one times out the rest of the erase skips that tool
# Which tools timed out in this process (a hung gvfsd-metadata or localsearch miner). A dict, not a module flag
# set through `global`: one shared object both the setter and the readers see, and the tests reset.
HUNG = {"gio": False, "localsearch": False}


def gio_metadata_on():
    """gio's path when GNOME's metadata can be cleared: gio installed, not switched off (the tests' default)."""
    return None if os.environ.get("MEDIA_INTAKE_NO_GVFS_METADATA") == "1" else shutil.which("gio")


def gvfs_meta_forget(path, stats=None):
    """Unset every metadata:: attribute GNOME keeps for path (an existing file about to be deleted or moved).
    Returns 1 when it had an entry and every key went. stats (a Counter) gets "gvfs-metadata cleared" for
    that, and "gvfs-metadata not cleared" when gio failed, timed out or left a key: the coverage report then
    says so instead of reading as clean. Never fatal: the erase goes on either way."""
    gio = gio_metadata_on()
    if not gio or not os.path.lexists(path) or os.path.islink(path):
        return 0
    stats = collections.Counter() if stats is None else stats
    if HUNG["gio"]:
        stats["gvfs-metadata not cleared"] += 1
        return 0
    path, env = os.path.abspath(path), dict(os.environ, LC_ALL="C")
    try:
        r = subprocess.run([gio, "info", "-n", "-a", "metadata", path], capture_output=True, text=True,
                           env=env, timeout=GIO_TIMEOUT)
        if r.returncode != 0:
            stats["gvfs-metadata not cleared"] += 1
            return 0
        keys = re.findall(r"^  (metadata::.+?): ", r.stdout, re.M)
        done = sum(subprocess.run([gio, "set", "-n", "-t", "unset", path, k], capture_output=True, env=env,
                                  timeout=GIO_TIMEOUT).returncode == 0 for k in keys)
    except (OSError, subprocess.SubprocessError) as e:
        if isinstance(e, subprocess.TimeoutExpired):
            HUNG["gio"] = True
        stats["gvfs-metadata not cleared"] += 1
        return 0
    if keys and done == len(keys):
        stats["gvfs-metadata cleared"] += 1
        return 1
    if keys:
        stats["gvfs-metadata not cleared"] += 1
    return 0


def walk_files(root, skip_rel=(), git=False):
    """Regular files under root, no symlinks followed, skip lists applied.

    With git, yields (path, in_git): in_git when a folder at or above it (under root) holds .git.
    """
    skip = {os.path.join(root, s) for s in skip_rel}
    repo = set()  # folders inside a git working tree
    for r, dirs, fs in os.walk(root, followlinks=False):
        in_git = r in repo or ".git" in dirs or ".git" in fs
        dirs[:] = [d for d in dirs if d not in SWEEP_SKIP_NAMES and os.path.join(r, d) not in skip]
        if in_git:
            repo.update(os.path.join(r, d) for d in dirs)
        for f in fs:
            yield (os.path.join(r, f), in_git) if git else os.path.join(r, f)


# How deep a near-duplicate chain goes: erasing quarantine/<b> moves near-copies to near-<b>, erasing those to
# near-near-<b>, and so on. Each hop allows NEAR_MAX bits, so level k can be k * NEAR_MAX bits from the photo
# first erased; 3 levels (12 of 64 bits) is as far as a match still means "a copy of a copy". Past it nothing
# moves: the matches stay where they are and erase prints how many.
NEAR_DEPTH = 3


def near_level(batch):
    """(depth, root): near-near-b -> (2, "b"); a batch that is not a near- batch is depth 0."""
    n = 0
    while batch.startswith("near-"):
        batch, n = batch[len("near-"):], n + 1
    return n, batch


def near_roots():
    """The media library locations near-duplicates are looked for in, and nowhere else: app
    icon caches, map tiles, render frames and repo assets look alike and aren't your photos."""
    roots = list(LIB_ROOTS)
    roots += glob.glob(os.path.join(STATE, "batches", "*", "raw")) + glob.glob(os.path.join(STATE, "batches", "*", "review"))
    return [r for r in roots if os.path.isdir(r)]


# Progress of a long walk or step (the HOME sweep, the near-duplicate search, stage, plan, apply, tag): a line
# every PROGRESS_EVERY files or PROGRESS_SECS seconds, whichever comes first, so a gallery Erase, a forget or a
# big batch never sits silent for minutes. MEDIA_INTAKE_PROGRESS_EVERY / _SECS override them (tests drive the
# lines by count with SECS=inf). One line per report, newline-terminated, on a terminal too: job logs stream them.
PROGRESS_EVERY, PROGRESS_SECS = 10000, 2.0


class Progress:
    """Counts-only progress lines, flushed at once so a job log streams them. Never a file name.

    With total, a tick line reads "label: N of TOTAL unit"; with hits=None it has no ", F hits so far" part."""

    def __init__(self, label, start, unit="files checked", hits="copies", total=None):
        self.label, self.unit, self.hits, self.total, self.n = label, unit, hits, total, 0
        self.every = max(1, int(os.environ.get("MEDIA_INTAKE_PROGRESS_EVERY") or PROGRESS_EVERY))
        self.secs = float(os.environ.get("MEDIA_INTAKE_PROGRESS_SECS") or PROGRESS_SECS)
        self.last = time.monotonic()
        print(f"{label}: {start}", flush=True)

    def say(self, what):
        """A phase line under the label (counts and step names only)."""
        print(f"{self.label}: {what}", flush=True)

    def tick(self, found=0, by=1):
        before = self.n
        self.n += by
        now = time.monotonic()
        if self.n // self.every > before // self.every or now - self.last >= self.secs:
            self.last = now
            done = f"{self.n:,}" + (f" of {self.total:,}" if self.total is not None else "")
            print(f"{self.label}: {done} {self.unit}" + (f", {found:,} {self.hits} so far" if self.hits else ""),
                  flush=True)


def next_hint(*cmds):
    """End a main-flow step (stage, plan, scan, apply, tag) with one "next: media-intake ..." line naming the
    command(s) to run next; each of cmds is an argument list, "<NAME>" a placeholder left unquoted. Only for a
    person at a terminal: a job log, the drive queue, a pipe or a test sees nothing new unless
    MEDIA_INTAKE_NEXT_HINT=1 (=0 turns it off on a terminal too)."""
    v = os.environ.get("MEDIA_INTAKE_NEXT_HINT")
    if v == "1" if v in ("0", "1") else sys.stdout.isatty():
        print("next: " + ", then ".join(" ".join(x if re.fullmatch(r"<[A-Z]+>", x) else shlex.quote(x)
                                                  for x in ("media-intake", *c)) for c in cmds), flush=True)


def where(path, base):
    """Top-level location of path for a count line: 'Pictures', '.cache/some-app', 'Trash'."""
    rel = os.path.relpath(path, base).split(os.sep)
    if rel[:3] == [".local", "share", "Trash"] or rel[0] in (".Trashes", ".Trash") or rel[0].startswith(".Trash-"):
        return "Trash"  # HOME's freedesktop Trash, or a drive's (.Trash-<uid>, .Trash/<uid>, macOS .Trashes)
    if rel[0].startswith(".") and len(rel) > 2:
        return "/".join(rel[:2]) if rel[0] != ".local" else "/".join(rel[:3])
    return rel[0] if len(rel) > 1 else "(home)"


def trash_info(p):
    """The .trashinfo of a file in a freedesktop Trash, which holds the file's original path: HOME's
    (~/.local/share/Trash/files) or a drive's (<mount>/.Trash-<uid>/files, <mount>/.Trash/<uid>/files).
    None when p is not in one (macOS .Trashes keeps no such file)."""
    d = os.path.dirname(p)
    if os.path.basename(d) != "files":
        return None
    top = os.path.dirname(d)
    if (top == os.path.join(HOME, ".local/share/Trash") or os.path.basename(top).startswith(".Trash-")
            or os.path.basename(os.path.dirname(top)) == ".Trash"):
        return os.path.join(top, "info", os.path.basename(p) + ".trashinfo")
    return None


def drop_trash_info(p):
    """Delete p's .trashinfo (trash_info) if there is one, never through a symlink. Returns 1 or 0."""
    info = trash_info(p)
    if info and os.path.isfile(info) and not os.path.islink(info):
        os.remove(info)
        return 1
    return 0


def sweep(sig, delete=True, drive=None, label="home sweep", stats=None, home=True):
    """Find files anywhere in HOME (and on drive) with an erased item's size+sha256.

    sig is {sha: size}. Only same-size files are hashed. With delete, matches go
    (a Trash match takes its .trashinfo, HOME's or the drive's, any match its .xmp sidecar), and so do
    orphaned sidecars (<file>.<ext>.xmp whose file is gone: they hold its place
    and labels). Matches inside a git working tree are never deleted (that leaves the
    repo dirty and git history keeps the content): they're returned in in_git.
    Prints a start line and counts-only progress (Progress) under label while it walks.
    stats (a Counter, with delete): adds the sidecars, Trash entries (HOME's under "trash entries", the
    drive's under "drive trash entries") and gvfs-metadata entries that went.
    home=False walks only the drive (sweep-erased --drive-only). A drive mounted inside HOME is walked once,
    as the drive. Returns (Counter by location, paths, Counter of orphan sidecars by location, in_git paths).
    """
    stats = collections.Counter() if stats is None else stats
    sizes = set(sig.values())
    found, paths, orphans, in_git = collections.Counter(), [], collections.Counter(), []
    dpath = os.path.abspath(drive) if drive else None
    home_skip = SWEEP_SKIP + ((os.path.relpath(dpath, HOME),) if dpath and dpath.startswith(HOME + os.sep) else ())
    roots = ([(HOME, home_skip)] if home else []) + ([(dpath, (WIPE_NAME,))] if drive else [])
    where_ = " and ".join((["HOME"] if home else []) + (["on the drive"] if drive else []))
    prog = Progress(label, f"searching every file {'in ' if home else ''}{where_} for copies of "
                           f"{len(sig)} erased items (same size, then sha256)")
    for root, skip in roots:
        for p, git in walk_files(root, skip, git=True):
            prog.tick(len(paths) + len(in_git))
            try:
                if git:
                    st = os.lstat(p)
                    if stat.S_ISREG(st.st_mode) and st.st_size in sizes and sig.get(sha256(p)) == st.st_size:
                        in_git.append(p)
                    continue
                if p.endswith((".xmp", ".XMP")) and orphan_sidecar(p):
                    # os.walk listed this folder before its media file matched below and took the sidecar
                    # with it (stats["sidecars"]): the listing still names the .xmp, which is gone now. Count
                    # an orphan only once it is really there to delete, so readdir order never counts it twice.
                    if delete:
                        os.remove(p)  # already gone: FileNotFoundError, skipped below, not counted
                    elif not os.path.lexists(p):
                        continue
                    orphans[("drive:" if root != HOME else "") + where(p, root)] += 1
                    continue
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode) or st.st_size not in sizes or sig.get(sha256(p)) != st.st_size:
                    continue
            except OSError:
                continue
            loc = ("drive:" if root != HOME else "") + where(p, root)
            found[loc] += 1
            paths.append(p)
            if delete:
                gvfs_meta_forget(p, stats)  # before the delete: gio can only clear an existing file's entry
                os.remove(p)
                stats["sidecars"] += drop_sidecar(p)
                stats["trash entries" if root == HOME else "drive trash entries"] += drop_trash_info(p)
    return found, paths, orphans, in_git


def drive_purge(mount, shas, stats=None):
    """Delete files anywhere on a backup drive whose sha256 is in shas (size prefilter).
    stats (a Counter): adds the sidecars and gvfs-metadata entries that went."""
    stats = collections.Counter() if stats is None else stats
    sizes = {s for s, _ in shas.values()}
    n = 0
    prog = Progress("drive sweep", f"searching every file on the drive for copies of {len(shas)} erased items "
                                   "(same size, then sha256)")
    for p in walk_files(os.path.abspath(mount), (WIPE_NAME,)):
        prog.tick(n)
        try:
            if os.path.getsize(p) in sizes and sha256(p) in shas:
                gvfs_meta_forget(p, stats)
                os.remove(p)
                stats["sidecars"] += drop_sidecar(p)
                stats["drive trash entries"] += drop_trash_info(p)  # it names the file's original path
                n += 1
        except OSError:
            pass  # vanished or unreadable since listing: not counted, left alone
    return n


def dhash_orient(p):
    """(64-bit dHash, landscape?) on the upright image, or (None, None) when unreadable, under
    200 px or featureless. Fast JPEG draft decode."""
    from PIL import Image, ImageOps
    try:
        with Image.open(p) as im:
            im.draft("RGB", (256, 256))
            if max(im.size) < 200:
                return None, None  # icons and stickers, never a copy of a photo
            up = ImageOps.exif_transpose(im)
            landscape = up.size[0] >= up.size[1]
            px = list(up.convert("L").resize((9, 8), Image.LANCZOS).getdata())
    except Exception:
        return None, None
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    if not 8 <= bin(bits).count("1") <= 56:
        return None, None  # a featureless image matches too much to compare
    return bits, landscape


def near_dups(refs, kept, index, batch, spare=(), stats=None):
    """Move images within NEAR_MAX of an erased image into quarantine/near-<batch>/ (never deleted).

    refs: [(dhash, landscape, sha256, root)] of the erased images; root is the surviving original the
    erased image was a copy of (an xcheck entry's `of`, or a near entry's `root_of`), else None. Each moved
    file records the sha256 of the erased image it matched, the erase date and that root (never the erased
    image's path or content), so the review page can show the root or say "near-copy of a photo you erased
    on <date>". Looks only in near_roots() (the library, videos, project folders, every batch's raw/ and
    review/), never inside a git working tree. spare: paths never moved (the originals an xcheck batch's
    messaging copies, or a near batch's entries, point at). An existing near-<batch> is added to, never
    replaced. Past NEAR_DEPTH, or where a quarantined file already holds the place, a match stays where it is
    and is only counted. Each file's gvfs-metadata entry is cleared before it moves (counted in stats).
    Returns (moved, near batch name or None, matches left in place).
    """
    if not refs:
        return 0, None, 0
    near = f"near-{batch}"
    room = near_level(near)[0] <= NEAR_DEPTH
    qroot = os.path.join(STATE, "quarantine")
    cands = set()
    for root in near_roots():
        for p, git in walk_files(root, git=True):
            if not git and os.path.splitext(p)[1].lower() in IMG and not p.startswith(qroot + os.sep):
                cands.add(p)
    lib_dh = {k: v.get("dhash") for k, v in index.items()}
    quar = os.path.join(qroot, near)
    qfile = os.path.join(quar, "quarantine.json")
    q = load(qfile, {}) or {}
    moved = left = 0
    spare = {os.path.abspath(x) for x in spare}
    todo = sorted(cands - spare)
    prog = Progress("near-duplicate search", f"comparing {len(todo):,} images in the library folders and batches "
                    f"with {len(refs)} erased images", unit="images checked", hits="near-copies")
    for p in todo:
        prog.tick(moved + left)
        pre = lib_dh.get(p)
        if pre is not None and all(bin(pre ^ r[0]).count("1") > NEAR_MAX + 2 for r in refs):
            continue  # library entry: the stored hash rules it out without decoding
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            dh, land = dhash_orient(p)
            hit = None if dh is None else next(
                (r for r in refs if bin(dh ^ r[0]).count("1") <= NEAR_MAX and land == r[1]), None)
            if hit is None:
                continue
            s = sha256(p)
        except OSError:
            continue
        if s in kept:
            continue  # you released this exact picture
        rel = os.path.relpath(p, HOME)
        dst = os.path.join(quar, rel)
        if not room or rel in q or os.path.lexists(dst) or os.path.lexists(sidecar(dst)):
            left += 1
            continue
        private_dir(quar)
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        gvfs_meta_forget(p, stats)  # its entry is keyed by this path, which the move leaves behind
        shutil.move(p, dst)
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
        if os.path.isfile(sidecar(p)) and not os.path.islink(sidecar(p)):
            shutil.move(sidecar(p), sidecar(dst))  # goes with the file; erased with the quarantine
        q[rel] = dict(reasons=["near-duplicate"], sha=s, size=os.path.getsize(dst), dst=p,
                      near_of_sha=hit[2], erased_on=f"{datetime.date.today():%Y-%m-%d}")
        if hit[3]:
            q[rel]["root_of"] = hit[3]
        thumb_forget(p)
        index.pop(p, None)
        moved += 1
    if moved:
        save(qfile, q)
    return moved, near, left


def prune_recent(uris, prefixes):
    """Drop recently-used.xbel bookmarks for uris or under prefixes; keep the rest byte for byte."""
    p = os.path.join(HOME, ".local/share/recently-used.xbel")
    if not os.path.exists(p):
        return 0
    import html, xml.etree.ElementTree as ET
    with open(p, encoding="utf-8") as f:
        text = f.read()
    n = 0
    def drop(m):
        nonlocal n
        href = html.unescape(m.group(2))
        if href in uris or any(href.startswith(x) for x in prefixes):
            n += 1
            return ""
        return m.group(0)
    rx = re.compile(r'([ \t]*)<bookmark\b[^>]*?\bhref="([^"]*)"[^>]*?(?:/>|>.*?</bookmark>)[ \t]*\n?', re.S)
    out = rx.sub(drop, text)
    if n:
        ET.fromstring(out.encode("utf-8"))  # never write a file GTK can't parse
        tmp = p + ".media-intake.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(os.stat(p).st_mode))
        os.replace(tmp, p)
    return n


def prune_app_traces(uris, prefixes):
    """Showtime's playback positions and log, localsearch's error reports and index."""
    n = collections.Counter()
    st = os.path.join(HOME, ".local/state/showtime")
    hist = os.path.join(st, "playback_history.json")
    keys = {hashlib.sha256(u.encode()).hexdigest() for u in uris}
    h = load(hist)
    if isinstance(h, dict) and keys & h.keys():
        n["showtime positions"] = len(keys & h.keys())
        save(hist, {k: v for k, v in h.items() if k not in keys})
    for log in (os.path.join(st, x) for x in ("showtime.log", "showtime.log.1", "showtime.log.2")):
        if not os.path.exists(log):
            continue
        with open(log, encoding="utf-8", errors="surrogateescape") as f:
            lines = f.readlines()
        keep = [l for l in lines if not any(u in l for u in list(uris) + prefixes)]
        if len(keep) != len(lines):
            n["showtime log lines"] += len(lines) - len(keep)
            with open(log + ".tmp", "w", encoding="utf-8", errors="surrogateescape") as f:
                f.writelines(keep)
            os.replace(log + ".tmp", log)
    errs = os.path.join(HOME, ".cache/tracker3/files/errors")
    for u in uris:  # localsearch names each failure report after md5(uri)
        e = os.path.join(errs, hashlib.md5(u.encode()).hexdigest())
        if os.path.isfile(e):
            os.remove(e)
            n["localsearch error reports"] += 1
    # The miner drops deleted files itself (it watches the indexed folders);
    # `reset --file` forces it for each erased path inside an indexed folder, and `localsearch info` then
    # checks the index holds nothing for it (a stored file has a nie:url; none prints "No metadata").
    if localsearch_on():
        r = localsearch("index")
        if r is None:
            n["localsearch could not check"] += 1  # no list of indexed folders: nothing could be reset or checked
            return n
        rows = re.findall(r"^(/.*?)\s+([*-])\s*$", re.sub(r"\x1b\[[0-9;]*m", "", r.stdout), re.M)
        for u in uris:
            if not u.startswith("file://"):
                continue
            path = urllib.parse.unquote(u[7:])
            if any(os.path.dirname(path) == d or (rec == "*" and path.startswith(d + os.sep)) for d, rec in rows):
                reset = localsearch("reset", "--file", path)
                if reset is not None and reset.returncode == 0:
                    n["localsearch resets"] += 1
                info = localsearch("info", path)
                if info is None or info.returncode != 0:  # hung, no miner, a D-Bus error: no answer, not "forgotten"
                    n["localsearch could not check"] += 1
                    continue
                n["localsearch checked"] += 1
                n["localsearch still indexed"] += "nie:url" in info.stdout
    return n


def prune_backup_records(gone, shas):
    """Drop erased content from every backup record (<state>/backup/*.json, backup.py's manifest: files keyed
    by the path under media-intake-backup/, each with its sha256): the record keeps names, which must not
    outlive the file. An entry goes when its path is an erased or moved library path, or its sha256 an erased
    item's (a copy filed under another name), and so does the .xmp sidecar entry of a media entry that went.
    Only bookkeeping: a dropped entry that still has a file is hashed again by the next backup. The copies on
    the drive are erase --drive's job (by checksum). A record it can't read (not JSON, another version) is
    left as it is and counted: it may keep names until the next backup to that drive rewrites it. Returns
    (entries dropped, records it could not rewrite, records it could not read)."""
    recs = sorted(glob.glob(os.path.join(STATE, "backup", "*.json")))
    if not recs:
        return 0, 0, 0
    import backup  # lazily: backup imports this module
    rels = set()
    for p in gone:
        for src, sub in backup.roots(CFG):
            if backup._within(p, src):
                rels.add(os.path.join(sub, os.path.relpath(os.path.realpath(p), os.path.realpath(src))))
    n = failed = unread = 0
    for rec in recs:
        files = backup.load_manifest(rec, strict=True)
        if files is None:
            unread += 1  # never rewritten here: whatever it holds is not this tool's to guess at
            continue
        drop = {r for r, e in files.items() if r in rels or (isinstance(e, dict) and e.get("sha") in shas)}
        drop |= {r for r in files if r.lower().endswith(".xmp") and r[:-4] in drop}
        if not drop:
            continue
        try:
            backup.save_manifest(rec, {r: e for r, e in files.items() if r not in drop})
        except OSError:
            failed += 1  # the names stay in it: counted under left, never fatal (the erase is done)
            continue
        n += len(drop)
    return n, failed, unread


def localsearch(*args):
    """Run localsearch (C locale, GIO_TIMEOUT). None when it timed out, or an earlier call in this erase did
    (HUNG): the caller counts that as could not check, never as done."""
    if HUNG["localsearch"]:
        return None
    try:
        return subprocess.run(["localsearch", *args], capture_output=True, text=True,
                              env=dict(os.environ, LC_ALL="C"), timeout=GIO_TIMEOUT)
    except subprocess.TimeoutExpired:
        HUNG["localsearch"] = True
        return None
    except OSError:
        return None


def localsearch_on():
    """The desktop search index can be reached: localsearch installed, not switched off (tests)."""
    return bool(shutil.which("localsearch")) and os.environ.get("MEDIA_INTAKE_NO_LOCALSEARCH") != "1"


# ---------------------------------------------------------------- device deletes
# Every delete on a phone, camera or source drive goes through device_targets, then
# device_remove; nothing else in media-intake writes to a device. It never renames or
# moves a file there: on case-insensitive storage a case-only rename is what makes one
# file show up under two names.
class _NoSizeCheck:
    def __repr__(self):
        return "NO_SIZE_CHECK"


# The size of a device_targets item that has no staged copy to compare with (the phone
# thumbnail folders). Only this marker skips the size check: a missing size (None) is a
# manifest that lost it, and that target is refused, never deleted unchecked.
NO_SIZE_CHECK = _NoSizeCheck()


def folder_refusal(man):
    """Why a device delete is refused for a laptop folder batch (kind folder), or None.

    offload, erase --phone and trash --phone all ask this: a folder that happens to be laid
    out like a phone (DCIM/...) would pass the phone allowlist, so the kind decides."""
    if (man or {}).get("kind") != "folder":
        return None
    return (f"refused: {man.get('root')} is a folder on this laptop; the tool never deletes its originals. "
            "Check the import, then delete the folder yourself.")


def device_targets(root, items, known, check=sources.check, skip_denied=True, hint=""):
    """The device files a command may delete, checked before anything is deleted.

    items: (path, size) pairs the command wants gone from the device at root (size
    NO_SIZE_CHECK for the thumbnail folders, else the staged size); known: every path the
    device listed (the batch manifests' src), so a target is compared with names that are
    not targets too. Skipped, a line each: a path outside root or outside the allowlist
    (check; with skip_denied False that refuses the whole command instead, hint appended).
    Skipped quietly: a file already gone or no longer the staged size. Refused, nothing
    deleted: a file still there with no recorded size, and any target whose path differs
    only by letter case from another known path, another target, or any path the device
    lists right now (sources.live_case_matches, then sources.check_case); a device whose
    live listing can't be taken refuses too, never falling back on the manifests. Returns
    the paths for device_remove.
    """
    out, unsized = [], []
    for path, size in items:
        if not path.startswith(root + os.sep):
            print(f"skip phone delete, path outside --phone root: {path}")
            continue
        try:
            check(root, path)  # never delete outside the allowlist
        except sources.Denied as e:
            if not skip_denied:
                sys.exit(f"refused: {e}; nothing deleted from the device{hint}")
            print(f"skip phone delete, {e}")
            continue
        if not os.path.lexists(path) or path in out:
            continue
        if size is NO_SIZE_CHECK:
            out.append(path)
        elif type(size) is not int:
            unsized.append(path)
        elif os.path.getsize(path) == size:
            out.append(path)
    if unsized:
        sys.exit("refused: nothing deleted from the device. The batch manifest records no size for "
                 "these, so they can't be checked against the staged copy:\n"
                 + "\n".join("  " + os.path.relpath(p, root) for p in unsized)
                 + f"\n(under {root}). Stage the batch again.")
    try:  # the device as it is now, not only as staged: fail closed if it can't be listed
        live = sources.live_case_matches(root, out)
    except sources.ListingFailed as e:
        sys.exit(f"refused: nothing deleted from the device. {e}, so a name that differs only by letter "
                 "case can't be ruled out. Check the device is still connected and unlocked, then run "
                 "the command again.")
    try:
        sources.check_case(out, [*known, *live])
    except sources.CaseCollision as e:
        sys.exit("refused: nothing deleted from the device. These paths on it differ only by letter "
                 "case; its storage ignores case, so each group may be one file, and deleting the "
                 "\"duplicate\" would delete the real one:\n"
                 + "\n".join("  " + "  =  ".join(os.path.relpath(p, root) for p in g) for g in e.groups)
                 + f"\n(under {root}). Look on the device itself, rename or delete by hand, then stage "
                   "the batch again.")
    return out


def device_remove(paths, tolerate=False):
    """gio remove each path from device_targets. A refused delete is fatal unless tolerate
    (Android's media service holds thumbnails open). Returns the paths it removed."""
    done = []
    for p in paths:
        r = subprocess.run(["gio", "remove", p], capture_output=tolerate)
        if r.returncode == 0:
            done.append(p)
        elif not tolerate:
            sys.exit(f"gio remove failed ({r.returncode}): {p}")
    return done


def phone_thumbnail_items(root):
    """(path, NO_SIZE_CHECK) for DCIM/.thumbnails and Pictures/.thumbnails and everything in them,
    contents before their folder, for device_targets with phone_thumbnail_check."""
    items = []
    for d in sources.thumbnail_dirs(root):
        for r, dirs, fs in os.walk(d, topdown=False, followlinks=False):
            items += [(os.path.join(r, x), NO_SIZE_CHECK) for x in fs + dirs]
        items.append((d, NO_SIZE_CHECK))
    return items


def phone_thumbnail_check(root, path):
    """sources.check_thumbnail, plus the two thumbnail folders themselves (thumbnail_dirs checked them)."""
    if os.path.relpath(path, root) not in sources.PHONE_THUMBNAIL_DIRS:
        sources.check_thumbnail(root, path)


def phone_thumbnails(targets):
    """Delete the checked thumbnail targets (device_targets of phone_thumbnail_items), and nothing else.

    Android's media service holds some thumbnails open and MTP then refuses the
    delete ("could not delete object"); those are counted and skipped, never
    fatal, so the rest of erase still runs. Returns (files deleted, refused).
    """
    files = {p for p in targets if not os.path.isdir(p)}
    done = device_remove(targets, tolerate=True)
    return len(files.intersection(done)), len(targets) - len(done)


DRIVE_HINT = "Mount the drive and pass its folder, e.g. /run/media/$USER/<label>."


def not_a_drive(mount):
    """Why mount is not a backup drive's folder (on the laptop's own disk, or findmnt can't tell), or None."""
    try:
        target = subprocess.run(["findmnt", "-n", "-o", "TARGET", "-T", mount], capture_output=True,
                                text=True).stdout.strip()
    except OSError:  # no findmnt: can't tell, so not a drive
        target = ""
    if not target or LAPTOP_MOUNTS.match(target):
        return f"is on the laptop's own disk ({target or 'unknown'}), not a backup drive"
    return None


def need_device(a):
    """Exit with one plain line unless --phone and --drive name what they should. Called before any prompt or
    delete, so a typo'd path is refused up front instead of found out after the erase: --phone an existing
    folder; --drive an existing folder and, when its free space will be wiped, a mounted drive (wipe_free's
    own check)."""
    phone = getattr(a, "phone", None)
    if phone and not os.path.isdir(phone):
        sys.exit(f"--phone {phone}: no such folder. Plug the phone in, unlock it, set USB to File transfer, "
                 "and pass the folder it shows (e.g. .../Internal storage).")
    drive = getattr(a, "drive", None)
    if not drive:
        return
    if not os.path.isdir(drive):
        sys.exit(f"--drive {drive}: no such folder, so not a mounted drive. {DRIVE_HINT}")
    if (not getattr(a, "no_wipe_free", True) and os.environ.get("MEDIA_INTAKE_WIPE_ANY_DEST") != "1"
            and (why := not_a_drive(os.path.abspath(drive)))):
        sys.exit(f"--drive {drive}: {why}. {DRIVE_HINT}")


def wipe_free(mount):
    """Overwrite a backup drive's free space with zeros (exFAT deletes only free clusters).

    Returns (MB written, seconds, note) or raises SystemExit if mount is the laptop's disk.
    """
    import errno, time
    mount = os.path.abspath(mount)
    if not os.path.isdir(mount):
        sys.exit(f"no such folder: {mount}. {DRIVE_HINT}")
    override = os.environ.get("MEDIA_INTAKE_WIPE_ANY_DEST") == "1"  # tests only
    if not override and (why := not_a_drive(mount)):
        sys.exit(f"{mount} {why}: not wiping. {DRIVE_HINT}")
    cap = int(os.environ.get("MEDIA_INTAKE_WIPE_MAX_MB", "0")) << 20
    chunk = bytes(64 << 20)
    t0, total, files, i = time.monotonic(), 0, [], 0
    try:
        while True:
            p = os.path.join(mount, f"{WIPE_NAME}.{i}")
            files.append(p)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                full = False
                while not (cap and total >= cap):
                    want = len(chunk) if not cap else min(len(chunk), cap - total)
                    try:
                        total += os.write(fd, chunk[:want])
                    except OSError as e:
                        if e.errno == errno.EFBIG:
                            break  # FAT32-style file size limit: next file
                        if e.errno in (errno.ENOSPC, errno.EDQUOT):
                            full = True
                            break
                        raise
                try:
                    os.fsync(fd)
                except OSError:
                    pass  # some filesystems (exFAT, FUSE) reject fsync; the write itself succeeded
            finally:
                os.close(fd)
            if full or (cap and total >= cap):
                break
            i += 1
    finally:
        for p in files:
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["sync"])
    note = ""
    src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "-T", mount], capture_output=True, text=True).stdout.strip()
    dev = os.path.basename(src)
    pk = subprocess.run(["lsblk", "-no", "PKNAME", src], capture_output=True, text=True).stdout.strip() if src.startswith("/dev/") else ""
    rot = os.path.join("/sys/block", pk or dev, "queue", "rotational")
    flash = False
    if src.startswith("/dev/") and os.path.exists(rot):
        with open(rot) as fh:
            flash = fh.read().strip() == "0"
    if flash:
        note = "flash drive: wear-levelling keeps spare blocks, so an overwrite is not a guarantee"
        if not override:
            r = subprocess.run(["fstrim", mount], capture_output=True, text=True)
            note += "; fstrim " + ("done" if r.returncode == 0 else f"not run ({(r.stderr or '').strip()[:80]})")
    return total >> 20, time.monotonic() - t0, note


def cmd_wipe_free(a):
    mb, secs, note = wipe_free(a.mount)
    print(f"wiped free space on {a.mount}: {mb} MB of zeros in {secs:.0f} s" + (f"\n  {note}" if note else ""))


def erase_log_sigs(batch=None):
    """{sha: size} from the erase log (all batches, or one). Old lines without a size are counted."""
    sig, nosize = {}, 0
    try:
        with open(os.path.join(STATE, "erase-log.txt")) as f:
            for line in f:
                m = re.search(r"^\S+ \S+ (\S+) sha256:([0-9a-f]{64})(?: size:(\d+))?", line)
                if not m or (batch and m.group(1) != batch):
                    continue
                if m.group(3) is None:
                    nosize += 1
                else:
                    sig[m.group(2)] = int(m.group(3))
    except FileNotFoundError:
        pass  # no erase log yet: nothing erased
    return sig, nosize


def verify_gone(sig, drive=None, left=None, home=True, names=True):
    """Sweep read-only for anything of sig left; print it. left (a Counter) gets what was found.
    home=False checks only the drive. names=False prints a count of the copies inside git repos, not their paths."""
    found, _, orphans, in_git = sweep(sig, delete=False, drive=drive, label="verify-gone", home=home)
    cached = [p for s in sig for p in gallery_cache_paths(s) if os.path.lexists(p)] if home else []
    gthumbs = sum(p.endswith(".jpg") for p in cached)
    if gthumbs:
        found["gallery thumbnails (~/.cache/media-intake/thumbs)"] += gthumbs
    gproxies = len(cached) - gthumbs
    if gproxies:
        found["gallery video proxies (~/.cache/media-intake/proxies)"] += gproxies
    if in_git:
        print(f"{len(in_git)} exact copies inside git repos (erase leaves them)" + (": " + ", ".join(in_git) if names else ""))
    if found:
        print(f"STILL PRESENT: {sum(found.values())} copies: " + ", ".join(f"{k} {v}" for k, v in sorted(found.items())))
    else:
        print(f"verify-gone: no copy of {len(sig)} erased items "
              + " or ".join((["in HOME"] if home else []) + (["on the drive"] if drive else [])))
    if orphans:
        print(f"ORPHAN SIDECARS: {sum(orphans.values())} .xmp files whose photo/video is gone (erase removes them): "
              + ", ".join(f"{k} {v}" for k, v in sorted(orphans.items())))
    if left is not None:
        left["copies still present at verify-gone"] += sum(found.values())
        left["orphan sidecars at verify-gone"] += sum(orphans.values())
    return not found and not orphans


def cmd_verify_gone(a):
    need_device(a)
    sig, nosize = erase_log_sigs(a.batch)
    if a.batch and not sig and not nosize and not erase_log_coverage(a.batch):
        need_batch(a.batch, staged=False)  # an erased batch keeps no folder (forget-*): the log says it's real
        sys.exit(f"nothing erased yet for {a.batch}: erase it first (media-intake erase {a.batch})")
    if nosize:
        print(f"{nosize} older erase-log entries have no size and can't be swept")
    if not sig:
        sys.exit(f"no erase-log entries with sizes{' for ' + a.batch if a.batch else ''}")
    ok = verify_gone(sig, a.drive)
    if a.batch:
        cov = erase_log_coverage(a.batch)
        if cov:
            print(f"last erase coverage report for {a.batch} ({cov[0]}):")
            print_coverage(cov[1], full=True)
        else:
            print(f"no erase coverage report for {a.batch} (erased before reports were kept)")
    sys.exit(0 if ok else 1)


def cmd_sweep_erased(a):
    """Delete the copies of erased items an erase could not reach: on a backup drive that was unplugged at erase
    time, or made in HOME since. Same size + sha256 match, skips and Trash handling as erase's sweep (sweep()).
    Never content whose checksum is in the library index now (released from quarantine, or filed since: that
    is yours to forget). Without --yes a dry run prints counts per folder and deletes nothing; with it, it
    deletes, zeroes the drive's free space if it deleted anything there, and ends with verify-gone.
    Output is counts only, never a file name."""
    if a.drive_only and not a.drive:
        sys.exit("sweep-erased: --drive-only needs --drive DRIVE")
    if a.drive and not os.path.isdir(a.drive):
        sys.exit(f"sweep-erased: --drive {a.drive}: not a directory (is the drive plugged in?)")
    sig, nosize = erase_log_sigs(a.batch)
    if nosize:
        print(f"{nosize} older erase-log entries have no size and can't be swept")
    index = load(os.path.join(STATE, "index.json"), {}) or {}
    in_index = {v.get("sha") for v in index.values() if isinstance(v, dict)}
    spared = in_index & set(sig)
    if spared:
        print(f"{len(spared)} erased checksums are in the library index now (released, or filed since): never swept")
    sig = {s: n for s, n in sig.items() if s not in spared}
    if not sig:
        print(f"nothing to sweep: no erase-log entries with sizes{' for ' + a.batch if a.batch else ''}")
        return
    home = not a.drive_only
    where_ = " and ".join((["HOME"] if home else []) + (["the drive"] if a.drive else []))
    stats = collections.Counter()
    found, paths, orphans, in_git = sweep(sig, delete=a.yes, drive=a.drive, label="sweep-erased", stats=stats,
                                          home=home)
    counts = "".join(f"\n  {k:28} {v}" for k, v in sorted(found.items()))
    if not a.yes:
        print(f"dry run: {sum(found.values())} copies of {len(sig)} erased items in {where_}" + counts
              + (f"\n  orphan sidecars {sum(orphans.values())}" if orphans else "")
              + "\nnothing deleted (--yes deletes them)")
        if in_git:
            print(f"{len(in_git)} exact copies inside git repos (left as they are)")
        return
    thumbs = gthumbs = 0
    for p in paths:
        thumbs += thumb_forget(p)
        index.pop(p, None)
    if home:
        for s in sig:
            gthumbs += gallery_thumb_forget(s)
    save(os.path.join(STATE, "index.json"), index)
    uris = {u for p in paths for u in file_uris(p)}
    recent = prune_recent(uris, []) if home else 0
    traces = prune_app_traces(uris, []) if home else collections.Counter()
    backups, _, _ = prune_backup_records(paths, set(sig))
    on_drive = sum(v for k, v in found.items() if k.startswith("drive:"))
    print(f"sweep-erased: {sum(found.values())} copies deleted in {where_}" + counts
          + (f"\n  sidecars (.xmp)              {stats['sidecars'] + sum(orphans.values())}"
             if stats["sidecars"] or orphans else "")
          + (f"\n  Trash entries                {stats['trash entries'] + stats['drive trash entries']}"
             if stats["trash entries"] or stats["drive trash entries"] else "")
          + (f"\n  thumbnails                   {thumbs + gthumbs}" if thumbs + gthumbs else "")
          + (f"\n  recent files entries         {recent}" if recent else "")
          + (f"\n  app traces                   {sum(traces.values())}" if sum(traces.values()) else "")
          + (f"\n  backup record entries        {backups}" if backups else ""))
    if home:
        print("  (not swept: toolchains, package/build caches, container storage, .git, node_modules)")
    if in_git:
        print(f"{len(in_git)} exact copies inside git repos, not deleted")
    if a.drive and on_drive and not a.no_wipe_free:
        try:
            mb, secs, note = wipe_free(a.drive)
            print(f"drive free space wiped: {mb} MB of zeros in {secs:.0f} s" + (f"\n  {note}" if note else ""))
        except SystemExit as e:
            print(f"drive free space NOT wiped: {e}")
    if not verify_gone(sig, a.drive, home=home, names=False):
        sys.exit(1)


# What no erase can reach: nothing on this machine can see or delete these, so every coverage report lists them.
CANT_REACH = (
    "cloud services and sync (anything uploaded, synced or backed up online)",
    "chat apps, email and the people it was sent to",
    "app-private storage on a phone or camera (a gallery app's own trash or cache, other apps' data)",
    "other computers and devices, and backup drives not named with --drive",
    "filesystem snapshots (btrfs snapshots of /home) and blocks an SSD or flash drive keeps aside",
    "re-encoded or trimmed copies of a video (only exact copies are found)",
    "a cloud export's Google Takeout JSON or iCloud CSV row left beside an erased file on its source drive "
    "(it can hold the date, place and description), and a Telegram export's result.json (it holds the "
    "chat's messages)",
)
# CANT_REACH in a few words, for erase's own short report (verify-gone BATCH prints the full list).
CANT_REACH_SHORT = ("cloud and sync, chat apps and email, apps' own storage on a phone, other devices and drives, "
                    "snapshots and SSD spare blocks, re-encoded videos, cloud-export notes on a source drive")
COVERAGE_RX = re.compile(r"^(\S+ \S+) (\S+) coverage (\{.*\})$")


def erase_coverage(reached, left, not_checked):
    """The coverage record of one erase: counts per place reached (zeros included, so a reader sees it
    looked), what it found but left, what it was not asked or able to check, and CANT_REACH. No file names."""
    return {"reached": dict(reached), "left": {k: v for k, v in left.items() if v},
            "not_checked": list(not_checked), "cant_reach": list(CANT_REACH)}


def write_coverage(batch, cov):
    """Append the record to erase-log.txt as one `<date> <time> <batch> coverage <json>` line. erase_log_sigs
    (and label.py) read only `sha256:` lines, so this never counts as an erased item."""
    with open(os.path.join(STATE, "erase-log.txt"), "a") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M} {batch} coverage "
                f"{json.dumps(cov, sort_keys=True, ensure_ascii=True)}\n")


def erase_log_coverage(batch):
    """(when, record) of batch's last coverage line in the erase log, or None."""
    last = None
    try:
        with open(os.path.join(STATE, "erase-log.txt")) as f:
            for line in f:
                m = COVERAGE_RX.match(line.rstrip("\n"))
                if m and m.group(2) == batch:
                    try:
                        last = (m.group(1), json.loads(m.group(3)))
                    except ValueError:
                        continue  # a damaged line: an older report still shows
    except FileNotFoundError:
        pass  # no erase log yet
    return last


def print_coverage(cov, full=False):
    """A coverage record: erase prints the short form (what was left, not checked, and CANT_REACH in a few words:
    its own summary line has the counts), verify-gone the full one."""
    reached, left = cov.get("reached", {}), cov.get("left", {})
    if full:
        print("  reached:" + "".join(f"\n    {k:62} {v}" for k, v in reached.items()))
        print("  left:" + ("".join(f"\n    {k:62} {v}" for k, v in left.items()) or " nothing"))
        if cov.get("not_checked"):
            print("  not checked:" + "".join(f"\n    {x}" for x in cov["not_checked"]))
        print("  can't reach (nothing on this machine can):" + "".join(f"\n    {x}" for x in cov.get("cant_reach", [])))
        return
    print("  left: " + (", ".join(f"{k} {v}" for k, v in left.items()) or "nothing"))
    if cov.get("not_checked"):
        print("  not checked: " + "; ".join(cov["not_checked"]))
    print("  can't reach: " + CANT_REACH_SHORT)


def ask(prompt):
    """The answer typed at a confirmation prompt, stripped. No terminal (stdin closed or at its end: a cron
    job, the folder queue) or Ctrl-C is no answer, "", so the caller's "type ERASE/DELETE" check says no
    and exits cleanly instead of a traceback. Every yes/no prompt before a delete goes through here."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()  # end the prompt's line
        return ""


def erase_warning(first, a, lib=0, phone=0):
    """What an erase deletes, said before its ERASE prompt (erase, forget): first is the line naming what was
    asked for; then every place it also deletes from, with counts where known, and what it can't reach."""
    also = [f"  - {plural(lib, 'identical copy', 'identical copies')} in your library"] if lib else []
    also.append("  - every identical copy anywhere in your home folder (your photo folders and any folder you "
                "imported from included)")
    if a.phone:
        also.append(f"  - {plural(phone, 'matching file')} on the phone or camera at {a.phone}"
                    + ("" if getattr(a, "keep_phone_thumbnails", False) else ", and its thumbnail folders"))
    if a.drive:
        also.append(f"  - every identical copy on the drive at {a.drive}"
                    + ("" if a.no_wipe_free else ", then its free space is overwritten"))
    print(f"{first}\nThis also deletes:\n" + "\n".join(also) + "\n"
          "It can't reach copies in the cloud, in chat apps or email, on other devices, or on a drive not named "
          "with --drive.")


def cmd_erase(a):
    need_batch(a.batch, staged=False)
    quar = os.path.join(STATE, "quarantine", a.batch)
    q = load(os.path.join(quar, "quarantine.json"), {})
    man = load(os.path.join(bdir(a.batch), "manifest.json"), {"files": {}})
    plan = load(os.path.join(bdir(a.batch), "plan.json"), [])
    if a.phone and (refusal := folder_refusal(man)):
        sys.exit(refusal + " (run erase without --phone)")
    if not q:
        sys.exit("quarantine is empty (copies left on a drive that was unplugged at erase time: "
                 f"media-intake sweep-erased {a.batch} --drive DRIVE)")
    need_device(a)
    if not a.yes:
        index = load(os.path.join(STATE, "index.json"), {}) or {}
        erased = {v["sha"] for v in q.values()} - {v["sha"] for v in index.values() if v.get("released")}
        rels = [r for r in q if q[r]["sha"] in erased] + [p["rel"] for p in plan if p["sha"] in erased
                                                          and p["rel"] not in q]
        erase_warning(f"Erase {plural(len(q), 'quarantined file')} for good.", a,
                      lib=sum(v["sha"] in erased for v in index.values()),
                      phone=sum(bool(man["files"].get(r, {}).get("src")) for r in rels))
        if ask("It cannot be undone. Type ERASE to go ahead: ") != "ERASE":
            sys.exit("nothing erased")
    # A checksum that is also in the library belongs to a released (or library)
    # copy: erase the quarantined file here, but never that content elsewhere.
    index = load(os.path.join(STATE, "index.json"), {})
    kept = {v["sha"] for v in index.values() if v.get("released")}
    shas = {v["sha"]: (v["size"], rel) for rel, v in q.items() if v["sha"] not in kept}
    sig = {s: v[0] for s, v in shas.items()}
    # dHash of erased images before they go, for the near-duplicate search (images only).
    refs = []
    for rel, v in q.items():
        p = os.path.join(quar, rel)
        if v["sha"] in shas and os.path.splitext(rel)[1].lower() in IMG and os.path.exists(p):
            dh, land = dhash_orient(p)
            if dh is not None:
                root = v.get("of") if isinstance(v.get("of"), str) else v.get("root_of")
                refs.append((dh, land, v["sha"], root if isinstance(root, str) else None))
    gone = set()  # every path erased or moved: for recent files, app state, the index and backup records
    gone |= set(getattr(a, "forget_paths", ()))  # forget's library paths (their quarantine entries keep no dst)
    # Exact duplicates of a quarantined file were dropped by `apply`, but their
    # phone originals (e.g. DCIM/Restored copies) are just as sensitive.
    phone_rels = [rel for rel in q if q[rel]["sha"] in shas]
    phone_rels += [p["rel"] for p in plan if p["sha"] in shas and p["rel"] not in q]
    # Every device delete is checked before anything is erased: a refusal leaves it all as it was.
    root = os.path.abspath(a.phone) if a.phone else None
    phone_items = [(man["files"][rel]["src"], man["files"][rel].get("size"))
                   for rel in phone_rels if man["files"].get(rel, {}).get("src")]
    do_pthumbs = bool(root) and not getattr(a, "keep_phone_thumbnails", False)
    pthumb_items = phone_thumbnail_items(root) if do_pthumbs else []
    known = [m["src"] for m in man["files"].values() if m.get("src")] + [p for p, _ in pthumb_items]
    phone_todo = device_targets(root, phone_items, known) if root else []
    # A GoPro's .LRV (a low-resolution copy) and .THM beside an erased chapter hold the same footage. Only
    # beside a chapter the gate passed: one skipped (size changed, denied, gone) keeps its previews too.
    prev_items = [(os.path.join(os.path.dirname(src), n), NO_SIZE_CHECK)
                  for src in phone_todo for n in devices.previews(os.path.basename(src))]
    listed = {os.path.join(d, n) for d in {os.path.dirname(p) for p, _ in prev_items} if os.path.isdir(d)
              for n in os.listdir(d)}  # never staged, so not in the manifest: the folder's own listing
    prev_todo = device_targets(root, prev_items, known + sorted(listed), sources.check_preview) if root else []
    # Anything unexpected in a thumbnail folder (a symlink, a path resolving elsewhere) refuses erase:
    # the yes was given for folders of thumbnails, so a folder that isn't one is for you to look at.
    pthumb_todo = device_targets(root, pthumb_items, known, phone_thumbnail_check, skip_denied=False,
                                 hint=" (look at the folder on the phone, or pass --keep-phone-thumbnails)") \
        if do_pthumbs else []
    thumbs = phone = 0
    # What forget already reached before handing over (sidecars, caches, gvfs-metadata of the library paths).
    stats = collections.Counter(getattr(a, "forget_stats", None) or {})
    gthumbs = stats.pop("gallery", 0)
    thumbs += stats.pop("thumbnails", 0)
    for rel in list(q):
        p = os.path.join(quar, rel)
        if os.path.exists(p):
            gvfs_meta_forget(p, stats)
            os.remove(p)
        gone |= {p, os.path.join(bdir(a.batch), "raw", rel)}
        if q[rel].get("dst"):
            gone.add(q[rel]["dst"])  # a near-duplicate's original place
        thumbs += thumb_forget(p) + thumb_forget(os.path.join(bdir(a.batch), "raw", rel))
        if q[rel]["sha"] in shas:
            gthumbs += gallery_thumb_forget(q[rel]["sha"])
    if root:
        for src, _ in phone_items + prev_items:
            thumbs += thumb_forget(src)
            gone.add(src)
    # Previews before their chapters: a run stopped by a refused delete keeps the chapter, so the next
    # run finds the previews beside it again. Chapter first would strand them: no chapter, no lookup.
    previews = len(device_remove(prev_todo))
    phone = len(device_remove(phone_todo))
    pthumbs, prefused = phone_thumbnails(pthumb_todo)
    # Identical copies already in the library (filed by an earlier batch) are the
    # same picture: erase them too.
    lib = 0
    for path, v in list(index.items()):
        if v["sha"] in shas:
            if os.path.exists(path):
                gvfs_meta_forget(path, stats)
                os.remove(path)
                lib += 1
            stats["sidecars"] += drop_sidecar(path)
            thumbs += thumb_forget(path)
            gone.add(path)
            del index[path]
    # Every other copy in HOME (imports made outside this tool, Downloads, Trash, caches).
    found, paths, orphans, in_git = sweep(sig, stats=stats)
    for p in paths:
        thumbs += thumb_forget(p)
        gone.add(p)
        index.pop(p, None)
    drive = drive_purge(a.drive, shas, stats) if a.drive else 0
    # The originals this quarantine's entries are copies of stay: an xcheck `of`, a near chain's `root_of`.
    spare = [v[k] for v in q.values() for k in ("of", "root_of") if isinstance(v.get(k), str)]
    near, near_batch, near_left = near_dups(refs, kept, index, a.batch, spare, stats)
    save(os.path.join(STATE, "index.json"), index)
    if near_batch and near:
        gone |= {v["dst"] for v in load(os.path.join(STATE, "quarantine", near_batch, "quarantine.json"), {}).values()}
    uris = {u for p in gone for u in file_uris(p)}
    prefixes = [file_uris(STATE)[0] + "/"]  # quarantine and staging, any batch
    recent = prune_recent(uris, prefixes)
    traces = prune_app_traces(uris, prefixes)
    backups, backups_failed, backups_unread = prune_backup_records(gone, set(shas))
    with open(os.path.join(STATE, "erase-log.txt"), "a") as f:
        for s, (size, _) in shas.items():
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M} {a.batch} sha256:{s} size:{size}\n")
    shutil.rmtree(quar)
    # Thumbnails are keyed by URI (file://, and mtp:// when browsed on the phone in
    # Files), so a per-file delete can miss some; the cache is rebuildable, clear it all.
    tdir = os.path.join(HOME, ".cache", "thumbnails")
    if os.path.isdir(tdir):
        thumbs += sum(len(fs) for _, _, fs in os.walk(tdir))
        shutil.rmtree(tdir)
    subprocess.run(["sync"])
    home, forgot = sum(found.values()), bool(getattr(a, "forget_paths", None))
    print(f"erased {plural(len(q) + lib + home + phone + drive, 'file')}: {len(q)} "
          + (f"from your library, {lib} more in your library" if forgot else f"quarantined, {lib} in your library")
          + f", {home} in your home folder"
          + (f", {phone} on the phone or camera" if root else "") + (f", {drive} on the drive" if a.drive else ""))
    if found:
        print("  in your home folder: " + ", ".join(f"{k} {v}" for k, v in sorted(found.items())))
    print("  (not swept: toolchains, package/build caches, container storage, .git, node_modules)")
    if in_git:
        print(f"{len(in_git)} exact copies inside git repos, not deleted: " + ", ".join(in_git))
    if orphans:
        print(f"orphan sidecars deleted (.xmp left by a deleted file): {sum(orphans.values())}")
    if previews:
        print(f"camera previews removed: {previews} (.LRV/.THM beside the erased videos)")
    if do_pthumbs:
        print(f"phone thumbnail folders removed ({pthumbs} files; Android rebuilds them)"
              + (f"; {prefused} the phone refused to delete (held open by Android's media service)" if prefused else ""))
    if near:
        rb = near_level(near_batch)[1]
        rb = rb[len("xcheck-"):] if rb.startswith("xcheck-") else rb
        print(f"{near} near-duplicates (resized/recompressed copies) moved to quarantine/{near_batch}, not deleted.\n"
              f"  Open:   xdg-open {os.path.abspath(os.path.join(STATE, 'quarantine', near_batch))}\n"
              f"  Review: media-intake review {rb}   (one page shows every near- level)\n"
              f"  or:     media-intake quarantine {near_batch}, then media-intake erase {near_batch}")
    if near_left:
        why = (f"near-duplicate chains stop at depth {NEAR_DEPTH}" if near_level(near_batch)[0] > NEAR_DEPTH
               else "a file already quarantined holds the same place")
        print(f"{near_left} near-duplicates of erased photos left where they are, not moved ({why})")
    wiped = None
    if a.drive and not a.no_wipe_free:
        try:
            mb, secs, note = wipe_free(a.drive)
            wiped = mb
            print(f"drive free space wiped: {mb} MB of zeros in {secs:.0f} s" + (f"\n  {note}" if note else ""))
        except SystemExit as e:
            print(f"drive free space NOT wiped: {e}")
    left = collections.Counter({"exact copies inside git repos (listed, not deleted)": len(in_git),
                                "near-duplicates left where they are": near_left,
                                "phone thumbnails the phone refused to delete": prefused,
                                "files localsearch still has indexed": traces["localsearch still indexed"],
                                "files localsearch could not check": traces["localsearch could not check"],
                                "gvfs-metadata entries gio could not clear": stats["gvfs-metadata not cleared"],
                                "backup records that could not be rewritten (they keep the names)": backups_failed,
                                "backup records that could not be read (they may keep names until the next backup "
                                "to that drive)": backups_unread})
    ok = verify_gone(sig, a.drive, left)
    # The coverage report: every place this erase reached (counts), what it left, what it could not check.
    reached = {"quarantined files (state folder, quarantine/)": len(q),
               "library copies (library, videos, project folders)": lib,
               "other copies in HOME (home sweep)": sum(found.values()),
               "sidecars (.xmp)": stats["sidecars"] + sum(orphans.values()),
               "Trash entries (~/.local/share/Trash/info)": stats["trash entries"],
               "freedesktop thumbnails (~/.cache/thumbnails)": thumbs,
               "gallery thumbnails and video proxies (~/.cache/media-intake)": gthumbs,
               "recent files entries (~/.local/share/recently-used.xbel)": recent,
               "Showtime playback positions and log lines (~/.local/state/showtime)":
                   traces["showtime positions"] + traces["showtime log lines"],
               "localsearch error reports (~/.cache/tracker3/files/errors)": traces["localsearch error reports"],
               "near-duplicates moved to quarantine (not deleted)": near,
               "backup records: erased files dropped (state folder, backup/)": backups}
    not_checked = []
    if gio_metadata_on():
        reached["gvfs-metadata entries cleared (~/.local/share/gvfs-metadata)"] = stats["gvfs-metadata cleared"]
    else:
        not_checked.append("gvfs-metadata (~/.local/share/gvfs-metadata): gio not installed or switched off")
    if localsearch_on():
        reached["localsearch index entries reset"] = traces["localsearch resets"]
        reached["localsearch index checked: file forgotten"] = (traces["localsearch checked"]
                                                               - traces["localsearch still indexed"])
    else:
        not_checked.append("desktop search index (localsearch): not installed or switched off")
    if root:
        reached["phone or camera originals (gio remove)"] = phone
        reached["camera previews (.LRV/.THM)"] = previews
        if do_pthumbs:
            reached["phone thumbnail folders (files)"] = pthumbs
        else:
            not_checked.append("phone thumbnail folders: --keep-phone-thumbnails")
    else:
        not_checked.append("phone or camera: no --phone")
    if a.drive:
        reached["backup drive copies"] = drive
        reached["backup drive Trash entries (.Trash-<uid>/info)"] = stats["drive trash entries"]
        if wiped is not None:
            reached["backup drive unused space overwritten (wipe-free, MB)"] = wiped
        else:
            not_checked.append("backup drive unused space: not overwritten"
                               + (" (--no-wipe-free)" if a.no_wipe_free else " (see above)"))
    else:
        not_checked.append("backup drive: no --drive")
    cov = erase_coverage(reached, left, not_checked)
    write_coverage(a.batch, cov)
    print(f"coverage report saved (the full report: media-intake verify-gone {a.batch}):")
    print_coverage(cov)
    if not ok:
        sys.exit(1)


def cmd_forget(a):
    """A sensitive file found later in the library: same erase path as intake.

    Every file is checked, and the yes given, before anything moves: a missing file or a "no" leaves
    every file, sidecar and index entry where it was (nothing half-forgotten in quarantine)."""
    files = list(dict.fromkeys(os.path.abspath(f) for f in a.files))
    bad = [f for f in files if os.path.islink(f) or not os.path.isfile(f)]
    if bad:
        sys.exit(f"not forgetting anything: {len(bad)} of {len(files)} are missing or not regular files")
    a.phone = None  # forget never deletes from a phone: erase_warning and need_device read it
    need_device(a)
    if not a.yes:
        erase_warning(f"Erase {plural(len(files), 'library file')} for good.", a)
        if ask("It cannot be undone. Type ERASE to go ahead: ") != "ERASE":
            sys.exit("nothing erased")
        a.yes = True
    batch = datetime.datetime.now().strftime("forget-%Y%m%d-%H%M%S")
    quar = private_dir(os.path.join(private_dir(os.path.join(STATE, "quarantine")), batch))
    index = load(os.path.join(STATE, "index.json"), {})
    q = load(os.path.join(quar, "quarantine.json"), {}) or {}  # a forget in the same second adds to it
    stats = collections.Counter()
    for f in files:
        stem, ext = os.path.splitext(os.path.basename(f))
        rel, i = stem + ext, 1
        while rel in q or os.path.lexists(os.path.join(quar, rel)):  # same name from two library folders
            rel, i = f"{stem}_{i}{ext}", i + 1
        s = sha256(f)
        q[rel] = dict(reasons=["owner"], sha=s, size=os.path.getsize(f), dst=None)
        gvfs_meta_forget(f, stats)  # keyed by the library path, which the move leaves behind
        shutil.move(f, os.path.join(quar, rel))
        os.chmod(os.path.join(quar, rel), stat.S_IRUSR | stat.S_IWUSR)
        stats["sidecars"] += drop_sidecar(f)  # its tags, place and labels go too
        stats["thumbnails"] += thumb_forget(f)
        stats["gallery"] += gallery_thumb_forget(s)
        index.pop(f, None)
    save(os.path.join(quar, "quarantine.json"), q)
    save(os.path.join(STATE, "index.json"), index)
    a.batch, a.phone, a.forget_stats = batch, None, stats  # erase's coverage report counts these too
    a.forget_paths = files
    cmd_erase(a)


def cmd_offload(a):
    """Free phone space: delete phone originals whose copy is filed (or deliberately removed).

    Only after a drive backup. Checks each phone file is still the same size as
    the staged copy and sits inside the allowlist; never deletes a folder. Refused
    for a laptop folder batch (kind folder): you delete that folder yourself.
    """
    need_batch(a.batch)
    b = bdir(a.batch)
    man = load(os.path.join(b, "manifest.json"))
    if refusal := folder_refusal(man):
        sys.exit(refusal)
    need_device(a)
    rows = load(os.path.join(b, "plan.json"))
    if rows is None:
        sys.exit(f"nothing filed yet for {a.batch}, so nothing to delete: run media-intake plan {a.batch}, "
                 f"then media-intake apply {a.batch}")
    plan = {p["rel"]: p for p in rows}
    root = os.path.abspath(a.phone)
    check = sources.check
    if man.get("kind") == "drive":  # same drive (filesystem UUID) and never the backup drive
        try:
            sources.check_drive_root(root)
        except sources.Denied as e:
            sys.exit(f"refused: {e}")
        if not man.get("uuid") or sources.drive_uuid(root) != man["uuid"]:
            sys.exit("refused: this is not the drive the batch was staged from (filesystem UUID differs)")
        check = sources.check_drive
    items = []
    for rel, m in man["files"].items():
        p = plan.get(rel)
        # A row with no size goes on to device_targets, which refuses it rather than skip it quietly.
        ok = p and (p["action"] == "delete" or (p["action"] == "file" and os.path.exists(p["dst"])
                                                 and m.get("size") in (None, os.path.getsize(p["dst"]))))
        if ok and m["src"].startswith(root + os.sep):
            items.append((m["src"], m.get("size")))
    todo = device_targets(root, items, [m["src"] for m in man["files"].values()], check, skip_denied=False)
    dev = "drive" if man.get("kind") == "drive" else "phone"
    if not todo:
        print(f"nothing to delete from the {dev}: no file of {a.batch} left there has a checked copy in your library.")
        return
    print(f"{len(todo)} of {plural(len(man['files']), dev + ' file')} have a checked copy in your library")
    if not a.yes:
        if ask(f"Delete {plural(len(todo), 'file')} from the {dev} for good? Back the library up to a drive "
               "first (media-intake backup). Type DELETE to go ahead: ") != "DELETE":
            sys.exit("nothing deleted")
    done = device_remove(todo)
    print(f"deleted {plural(len(done), 'file')} from the {dev}; quarantined and review files stay until "
          "erase or purge-review")


def rerun_with_yes(argv, ok=True):
    """The dry-run footer of a delete command: nothing went, and the exact command that deletes. ok=False (the
    dry run would refuse a file): no command to copy, since it would refuse the same files; what to fix first."""
    if not ok:
        print("dry run: nothing deleted. Some files would be refused (see above). Fix that first: take them "
              "off the command (erase or keep a quarantined one on the review page, never trash it); then run "
              "it again with --yes")
        return
    print("dry run: nothing deleted. To delete for good, run:")
    print("  " + shlex.join(["media-intake", *argv, "--yes"]))


def cmd_purge_review(a):
    if not BATCH_NAME.match(a.batch):  # a batch name, never a path: rmtree stays under batches/<batch>/review
        sys.exit(f"purge-review {a.batch!r}: not a batch name (letters, digits, '.', '_' and '-' only)")
    rv = os.path.join(bdir(a.batch), "review")
    n = sum(len(fs) for _, _, fs in os.walk(rv))
    if not a.yes:  # a delete needs an explicit --yes; without it, say what would go
        print(f"would delete {n} reviewed near-duplicates/junk from batch {a.batch}")
        rerun_with_yes(["purge-review", a.batch])
        return
    for r, _, fs in os.walk(rv):
        for f in fs:
            thumb_forget(os.path.join(r, f))
    shutil.rmtree(rv, ignore_errors=True)
    print(f"deleted {n} reviewed near-duplicates/junk")


# ---------------------------------------------------------------- trash (plain delete, non-sensitive junk)
TRASHED = os.path.join(STATE, "trashed.json")  # {sha256: size}: plan drops these if they come back
BATCH_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def trash_roots():
    """Where trash may delete: the library roots (LIB_ROOTS), then quarantine and review folders."""
    return [*LIB_ROOTS, os.path.join(STATE, "quarantine"), os.path.join(STATE, "batches")]


def trash_allowed(p):
    if os.path.islink(p) or not os.path.isfile(p) or p.endswith(".xmp"):
        return False
    rp = os.path.realpath(p)
    if any(rp.startswith(os.path.realpath(r) + os.sep) for r in LIB_ROOTS):
        return True
    rel = os.path.relpath(rp, os.path.realpath(STATE)).split(os.sep)
    return len(rel) > 3 and rel[0] == "batches" and rel[2] == "review"


def in_quarantine(p):
    """p is a quarantined (flagged sensitive) file: erase or keep it, never plain trash (the desktop trash
    would keep the sensitive content and the sweep of its copies would be skipped)."""
    rel = os.path.relpath(os.path.realpath(p), os.path.realpath(STATE)).split(os.sep)
    return len(rel) > 2 and rel[0] == "quarantine"


def phone_rels(batch, path):
    """Manifest rels of a batch whose copy is path (filed, review or quarantine), plus exact duplicates."""
    b = bdir(batch)
    plan = load(os.path.join(b, "plan.json"), []) or []
    rp = os.path.realpath(path)
    qd = os.path.realpath(os.path.join(STATE, "quarantine", batch))
    rels = set()
    for p in plan:
        locs = [p.get("dst"), p.get("lib_dst"), os.path.join(b, "review", p["rel"]), os.path.join(qd, p["rel"])]
        if any(x and os.path.realpath(x) == rp for x in locs):
            rels.add(p["rel"])
    shas = {p["sha"] for p in plan if p["rel"] in rels}
    rels |= {p["rel"] for p in plan if p["sha"] in shas}  # DCIM/Restored copies and the like
    return rels


def mark_trashed_copies(shas, index):
    """Owner|trash on library copies (by index.json) of content just trashed that are still on disk.
    trash deletes only the paths named; a copy is marked so suggest-trash and the gallery offer it, and
    the VLM leaves it alone. A copy with its own keep decision (owner.kept, a favorite too) or a scan flag is
    skipped.
    Returns (marked, kept copies left alone, an error or None)."""
    copies = sorted(p for p, e in index.items() if isinstance(e, dict) and e.get("sha") in shas
                    and os.path.isfile(p) and not os.path.islink(p)
                    and any(os.path.realpath(p).startswith(os.path.realpath(r) + os.sep) for r in LIB_ROOTS))
    bad = set(guard_label().refused(copies, STATE)) if copies else set()
    copies = [p for p in copies if p not in bad]
    if not copies:
        return 0, 0, None
    if not shutil.which("exiftool"):
        return 0, 0, f"{len(copies)} copies not marked Owner|trash: exiftool missing ({DEPS})"
    old = read_sidecar_tags(copies)
    todo = [p for p in copies if not (p in old and owner.kept(old[p][0], old[p][1], old[p][3]))]
    write_tags([(p, dict(hier=[owner.TRASH])) for p in todo])
    return len(todo), len(copies) - len(todo), None


def trash_refusals(refused, quarantined):
    return ((f"; refused {len(refused)} (not a library or review file)" if refused else "")
            + (f"; refused {len(quarantined)} quarantined (sensitive: erase or keep them, never trash)"
               if quarantined else ""))


def trash_files(files, batch=None, phone=None, dry=False):
    """Delete each file, its sidecar, thumbnails, index entry and (with phone) its phone original.
    The sha256 goes in trashed.json first: the durable Owner|trash (never taken in again); library
    copies of it left on disk get Owner|trash in their sidecar (mark_trashed_copies).
    dry: every check runs (folder, case and quarantine refusals included), then only the counts of
    what would go are printed and nothing is written or deleted."""
    private_dir(STATE)
    index = load(os.path.join(STATE, "index.json"), {})
    trashed = load(TRASHED, {})
    root = os.path.abspath(phone) if phone else None
    batches = [batch] if batch else sorted(os.path.basename(d) for d in glob.glob(os.path.join(STATE, "batches", "*")))
    mans = {b: load(os.path.join(bdir(b), "manifest.json"), {"files": {}}) or {"files": {}} for b in batches} \
        if root else {}
    if batch and (refusal := folder_refusal(mans.get(batch))):
        sys.exit(refusal + " (trash without --phone)")
    # Without a named batch, a folder batch's originals are never device targets.
    mans = {b: man for b, man in mans.items() if not folder_refusal(man)}
    refused, quarantined, todo = [], [], []
    for f in files:  # every phone delete is checked before anything is trashed
        p = os.path.abspath(f)
        if any(p == x for x, _ in todo):
            continue
        if in_quarantine(p):
            quarantined.append(f)
            continue
        if not trash_allowed(p):
            refused.append(f)
            continue
        srcs = [(m["src"], m.get("size")) for b, man in mans.items() for rel in phone_rels(b, p)
                for m in [man["files"].get(rel) or {}] if m.get("src")]
        todo.append((p, srcs))
    known = [m["src"] for man in mans.values() for m in man["files"].values() if m.get("src")]
    phone_ok = set(device_targets(root, [x for _, srcs in todo for x in srcs], known)) if root else set()
    if dry:
        print(f"would trash {len(todo)} files" + (f"; phone originals {len(phone_ok)}" if root else "")
              + trash_refusals(refused, quarantined))
        return not refused and not quarantined
    n = sc = th = ph = 0
    gone = set()
    for p, srcs in todo:
        s, size = sha256(p), os.path.getsize(p)
        gone.add(s)
        trashed[s] = size
        save(TRASHED, trashed)  # first, so a crash mid-way still keeps it from coming back
        os.remove(p)
        n += 1
        sc += drop_sidecar(p)
        th += thumb_forget(p) + gallery_thumb_forget(s)
        index.pop(p, None)
        for src, _ in srcs:
            th += thumb_forget(src)
            if src in phone_ok:
                phone_ok.discard(src)
                ph += len(device_remove([src]))
    save(os.path.join(STATE, "index.json"), index)
    marked, spared, err = mark_trashed_copies(gone, index)
    print(f"trashed {n} files; sidecars {sc}; thumbnails {th}; phone {ph}"
          + (f"; copies left on disk marked Owner|trash {marked}" if marked else "")
          + (f"; copies you kept left alone {spared}" if spared else "")
          + trash_refusals(refused, quarantined))
    if err:
        print(err)
    return not refused and not quarantined and not err


def digikam_rejected(roots):
    """Media files whose sidecar has digiKam's Rejected pick label (XMP-digiKam:PickLabel = 1)."""
    need_exiftool()
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        return []
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-r", "-ext", "xmp", "-XMP-digiKam:PickLabel", *roots],
                       capture_output=True, text=True)
    out = []
    for row in json.loads(r.stdout or "[]"):
        media = row["SourceFile"][:-4]
        if str(row.get("PickLabel", "")).strip() == "1" and os.path.isfile(media) and not os.path.islink(media):
            out.append(media)
    return sorted(out)


def cmd_trash(a):
    if a.from_digikam:
        if a.args:
            sys.exit("--from-digikam takes no files")
        hits = digikam_rejected(LIB_ROOTS)
        print(f"{len(hits)} library files marked Rejected in digiKam")
        for h in hits:
            print("  " + os.path.relpath(h, HOME))
        if not a.yes:
            rerun_with_yes(["trash", "--from-digikam", *(["--phone", a.phone] if a.phone else [])])
            return
        sys.exit(0 if trash_files(hits, phone=a.phone) else 1)
    args = list(a.args)
    batch = a.batch
    if batch is None and len(args) >= 2 and BATCH_NAME.match(args[0]) and not os.path.exists(args[0]) \
            and (os.path.isdir(bdir(args[0])) or os.path.isdir(os.path.join(STATE, "quarantine", args[0]))):
        batch = args.pop(0)
    if not args:
        sys.exit("trash wants FILE... (or --from-digikam)")
    # A delete (and a phone delete above all) needs an explicit --yes; without it, a dry run that says what
    # would go. It exits 1, as the real run would, when a file would be refused.
    ok = trash_files(args, batch, a.phone, dry=not a.yes)
    if not a.yes:
        rerun_with_yes(["trash", *(["--batch", batch] if batch else []), *args,
                        *(["--phone", a.phone] if a.phone else [])], ok)
    sys.exit(0 if ok else 1)


def keep_args(tags, owner_leaves=()):
    """exiftool arguments (no target) for Keep, the one rule the review page (via not-trash) and the
    gallery share: Owner|keep + Owner|not-trash on; Review|maybe-trash, Owner|trash (an older trash
    decision) and the legacy Review|not-trash off. A dc:Subject leaf goes only with a tag that was
    there, and never one a remaining tag or digiKam's TagsList (owner_leaves) still uses."""
    drop = ("Review|maybe-trash", owner.TRASH, *owner.LEGACY)
    add = (owner.KEEP, owner.NOT_TRASH)
    used = {t.split("|")[-1] for t in tags if t not in drop} | {h.split("|")[-1] for h in add} | set(owner_leaves)
    b = [f"-XMP-lr:HierarchicalSubject-={t}" for t in drop]
    b += [f"-XMP-dc:Subject-={leaf}" for leaf in dict.fromkeys(t.split("|")[-1] for t in drop if t in tags)
          if leaf not in used]
    for h in add:
        leaf = h.split("|")[-1]
        b += [f"-XMP-lr:HierarchicalSubject-={h}", f"-XMP-lr:HierarchicalSubject+={h}",
              f"-XMP-dc:Subject-={leaf}", f"-XMP-dc:Subject+={leaf}"]
    return b


def cmd_not_trash(a):
    """You kept a suggested file (the review page's Keep): keep_args() on its sidecar, so it is
    never suggested again."""
    need_exiftool()
    files = [os.path.abspath(f) for f in a.files]
    bad = [f for f in files if not trash_allowed(f) or not os.path.exists(sidecar(f))]
    if bad:
        sys.exit(f"not changing: {len(bad)} files are not library files with a sidecar")
    old = read_sidecar_tags(files)
    blocks = []
    for f in files:
        tags, _, leaves, _ = old.get(f, ([], "", set(), None))
        blocks.append(["-q", "-m", "-overwrite_original", *keep_args(tags, leaves), sidecar(f)])
    exiftool_batch(blocks)
    print(f"kept {len(files)} files: Owner|keep + Owner|not-trash")


def cmd_migrate_tags(a):
    """One-shot, idempotent: rewrite the legacy Review|not-trash to Owner|not-trash in every library
    sidecar (through merge_tags and write_tags, the normal sidecar-write path). Nothing else changes."""
    need_exiftool()
    roots = [r for r in LIB_ROOTS if os.path.isdir(r)]
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-r", "-ext", "xmp", "-HierarchicalSubject",
                        "-XMP-digiKam:PickLabel", *roots], capture_output=True, text=True) if roots else None
    entries = []
    for row in json.loads(r.stdout or "[]") if r else []:
        tags = as_list(row.get("HierarchicalSubject"))
        if not owner.LEGACY.keys() & set(tags):
            continue
        hier, remove, leaves = merge_tags(tags, [], owner.pick_label(row.get("PickLabel")), relabelled=False)
        entries.append((row["SourceFile"][:-4], dict(hier=hier, remove=remove, drop_leaves=leaves)))
    if a.dry_run:
        print(f"{len(entries)} sidecars have the legacy Review|not-trash (dry run: nothing written)")
        return
    write_tags(entries)
    print(f"migrated {len(entries)} sidecars: Review|not-trash -> Owner|not-trash")


def cmd_suggest_trash(a):
    """Library files the VLM called trash (Review|vlm-trash/*, written only at confidence >= 0.7, or
    AI|vlm/<model>/trash...), SigLIP's Review|maybe-trash without a VLM keep, or a surviving copy of
    content you trashed (Owner|trash). Never one you kept (owner.kept: Owner|keep, Owner|not-trash,
    digiKam Accepted, a favorite: xmp:Rating >= 5), Used|* or a protected project's Project| tag."""
    need_exiftool()
    roots = [r for r in LIB_ROOTS if os.path.isdir(r)]
    protected = config.protected_tags(CFG)
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-r", "-ext", "xmp", "-HierarchicalSubject",
                        "-XMP-digiKam:PickLabel", "-XMP-xmp:Rating", *roots], capture_output=True,
                       text=True) if roots else None
    only = None
    if a.batch:
        only = {os.path.realpath(p["dst"]) for p in load(os.path.join(bdir(a.batch), "plan.json"), []) or []
                if p.get("action") == "file" and p.get("dst")}
    items = []
    for row in json.loads(r.stdout or "[]") if r else []:
        media = row["SourceFile"][:-4]
        tags = as_list(row.get("HierarchicalSubject"))
        vlm_trash = any(t.startswith("Review|vlm-trash/") or (t.startswith("AI|vlm/") and "/trash" in t) for t in tags)
        vlm_keep = any(t == "Review|vlm-keep" or (t.startswith("AI|vlm/") and "/keep" in t) for t in tags)
        if not (vlm_trash or ("Review|maybe-trash" in tags and not vlm_keep) or owner.TRASH in tags):
            continue
        if owner.kept(tags, owner.pick_label(row.get("PickLabel")), row.get("Rating")) \
                or protected.intersection(tags) or any(t.startswith("Used|") for t in tags):
            continue
        if not trash_allowed(media) or (only is not None and os.path.realpath(media) not in only):
            continue
        items.append(dict(path=media, labels=[t for t in tags if t == owner.TRASH or t.startswith(("Label|", "Review|", "AI|"))]))
    items.sort(key=lambda x: x["path"])
    out = os.path.join(private_dir(STATE), "maybe-trash.json")
    fd = os.open(out + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"batch": a.batch, "items": items}, f, indent=1)
    os.replace(out + ".tmp", out)
    print(f"{len(items)} library files suggested as maybe-trash")
    if not items or a.no_serve:
        return
    cmd = [sys.executable, "-I", os.path.join(HERE, "review_ui.py"), "--maybe-trash"]
    if a.phone:
        cmd += ["--phone", a.phone]
    os.execv(sys.executable, cmd)


# ---------------------------------------------------------------- tags (XMP sidecars)
# Fields (docs/intake.md, "Tagging"): XMP-lr:HierarchicalSubject with "|"
# (Project|, Source|, Event|, Place|, Label|, Review|, Used|, Owner|), XMP-dc:Subject
# with each leaf, XMP-iptcExt:Event, XMP-photoshop:City/State/Country,
# XMP-iptcCore:CountryCode, XMP-exif:GPSLatitude/GPSLongitude, XMP-exif:DateTimeOriginal.
EMPTY_XMP = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
             "<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
             "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
             "</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")
SOURCE = {"photo": "phone camera", "video": "phone camera", "gopro": "gopro",
          "screenshot": "screenshot", "screenrec": "screen recording"}
GOPRO_NAME = re.compile(r"^(G[HXP]\d{6}|GOPR\d{4}|GP\d{6})\.", re.I)
POST_SLUG = re.compile(r"^(\d{4}-\d\d-\d\d)-[a-z0-9][a-z0-9-]*$")


def need_exiftool():
    if not shutil.which("exiftool"):
        sys.exit(f"exiftool missing: {DEPS}")


def guard_label():
    import label  # stdlib-only at import time; the model loads in the venv subprocess
    return label


def exiftool_batch(blocks):
    """Run exiftool once over many argument blocks (one per sidecar), via an argfile on stdin."""
    if not blocks:
        return
    lines = []
    for b in blocks:
        for x in b:
            if "\n" in x:
                sys.exit("newline in a path or tag; not writing")
        lines += b + ["-execute"]
    r = subprocess.run(["exiftool", "-@", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"exiftool failed writing sidecars: {r.stderr.strip()[:400]}")


def write_tags(entries):
    """entries: [(media_path, {"hier": [...], "event", "city", "state", "country", "cc", "lat", "lon", "date"})].

    Creates <file>.<ext>.xmp when missing, then adds tags to it. Only the sidecar is
    ever named as exiftool's target; keywords are deduplicated (-=x then +=x).
    """
    blocks = []
    for path, t in entries:
        x = sidecar(path)
        if not x.endswith(".xmp") or os.path.splitext(path)[1].lower() == ".xmp":
            sys.exit(f"refusing to write a non-sidecar: {x}")
        if not os.path.exists(x):
            with open(x, "w", encoding="utf-8") as f:
                f.write(EMPTY_XMP)
        # -sep: a list item never contains it. Not "#...": argfile lines starting with # are comments.
        b = ["-q", "-m", "-overwrite_original", "-sep", "@@sep@@"]
        for h in t.get("remove", []):  # a namespace this writer owns, being replaced
            b.append(f"-XMP-lr:HierarchicalSubject-={h}")
        for leaf in t.get("drop_leaves", []):
            b.append(f"-XMP-dc:Subject-={leaf}")
        for h in t.get("hier", []):
            leaf = h.split("|")[-1]
            b += [f"-XMP-lr:HierarchicalSubject-={h}", f"-XMP-lr:HierarchicalSubject+={h}",
                  f"-XMP-dc:Subject-={leaf}", f"-XMP-dc:Subject+={leaf}"]
        if t.get("event"):
            b.append(f"-XMP-iptcExt:Event={t['event']}")
        for k, tag in (("city", "XMP-photoshop:City"), ("state", "XMP-photoshop:State"),
                       ("country", "XMP-photoshop:Country"), ("cc", "XMP-iptcCore:CountryCode")):
            if t.get(k):
                b.append(f"-{tag}={t[k]}")
        if t.get("lat") is not None:
            b += [f"-XMP-exif:GPSLatitude={t['lat']}", f"-XMP-exif:GPSLongitude={t['lon']}"]
        if t.get("date"):
            b.append(f"-XMP-exif:DateTimeOriginal={t['date']}")
        blocks.append(b + [x])
    exiftool_batch(blocks)


def read_gps(paths):
    """{path: (lat, lon)} from each file's own metadata (EXIF, QuickTime, GoPro GPMF)."""
    def run(ps, extra):
        if not ps:
            return []
        r = subprocess.run(["exiftool", "-j", "-n", "-q", "-q", *extra, "-GPSLatitude", "-GPSLongitude", "-@", "-"],
                           input="\n".join(ps) + "\n", capture_output=True, text=True)
        try:
            return json.loads(r.stdout or "[]")
        except ValueError:
            return []
    out = {}
    for row in run(paths, []):
        if isinstance(row.get("GPSLatitude"), (int, float)) and isinstance(row.get("GPSLongitude"), (int, float)):
            out[row["SourceFile"]] = (row["GPSLatitude"], row["GPSLongitude"])
    vids = [p for p in paths if p not in out and os.path.splitext(p)[1].lower() in VID]
    for row in run(vids, ["-ee", "-api", "LargeFileSupport=1"]):  # GoPro: GPS lives in the GPMF track
        if isinstance(row.get("GPSLatitude"), (int, float)) and isinstance(row.get("GPSLongitude"), (int, float)):
            out[row["SourceFile"]] = (row["GPSLatitude"], row["GPSLongitude"])
    return {p: ll for p, ll in out.items() if not (ll[0] == 0 and ll[1] == 0)}


def venv_json(script, payload, extra=()):
    r = subprocess.run([VENV_PY, "-I", os.path.join(HERE, script), *extra], input=json.dumps(payload),
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"{script} failed ({r.returncode}): {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout)


def infer(path):
    """(project, source, event) from where a library file sits."""
    ap = os.path.abspath(path)
    name = os.path.basename(ap)
    def under(root):
        root = os.path.abspath(root)
        return os.path.relpath(ap, root).split(os.sep) if ap.startswith(root + os.sep) else None
    for proj in PROJECTS.values():  # <folder>/<date>-<event>/{phone,gopro}/
        r = under(proj["folder"])
        if r is None:
            continue
        if len(r) >= 2:
            m = re.match(r"^\d{4}-\d\d-\d\d-(.+)$", r[0])
            src = "gopro" if r[1] == "gopro" else "phone camera" if r[1] == "phone" else None
            return proj["name"], src, m.group(1) if m else None
        return proj["name"], None, None
    r = under(LIBRARY)
    if r:
        if r[0] == "Screenshots":
            return "Personal", "screenshot", None
        if r[0] == "Messaging" and len(r) > 2:
            return "Personal", r[1].lower(), None
        return "Personal", "gopro" if GOPRO_NAME.match(name) else "phone camera", None
    r = under(VIDEOS)
    if r:
        if r[0] == "Screen recordings":
            return "Personal", "screen recording", None
        return "Personal", "gopro" if GOPRO_NAME.match(name) else "phone camera", None
    return "Personal", None, None


# Tags: who writes what (docs/intake.md). AI never overrides; each writer replaces only its own
# namespace; your own tags (Owner|, plain keywords) win. Owner decisions: owner.py.
FACT_PREFIXES = ("Project|", "Source|", "Event|", "Place|")  # intake facts (tag, tag-files)


def ai_owned(t):
    """SigLIP's namespace, replaced on every labelled re-tag. Review|child-in-shot is not in it:
    AI may add it, only you clear it."""
    return t.startswith("Label|") or t == "Review|maybe-trash" or t.startswith("Review|untagged-")


def read_sidecar_tags(paths):
    """{media path: ([HierarchicalSubject...], pick label, {owner leaves}, xmp:Rating or None)} for paths with a
    sidecar. Owner leaves: digiKam's own TagsList ("a/b") leaves, which a dc:Subject removal must spare. The
    rating makes a favorite (owner.favorite) count as kept."""
    xs = [sidecar(p) for p in paths if os.path.isfile(sidecar(p))]
    if not xs:
        return {}
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-HierarchicalSubject", "-XMP-digiKam:PickLabel",
                        "-XMP-digiKam:TagsList", "-XMP-xmp:Rating", "-@", "-"], input="\n".join(xs) + "\n",
                       capture_output=True, text=True)
    out = {}
    for row in json.loads(r.stdout or "[]"):
        out[row["SourceFile"][:-4]] = (as_list(row.get("HierarchicalSubject")), str(row.get("PickLabel", "")).strip(),
                                       {t.split("/")[-1] for t in as_list(row.get("TagsList"))}, row.get("Rating"))
    return out


def owner_kept(tags, pick):
    return owner.kept(tags, pick)


def merge_tags(old, new, pick="", relabelled=True, owner_leaves=(), stars=None):
    """(hier to add, tags to remove, dc:Subject leaves to remove) for a re-tag.

    Facts replace only the prefixes this run has a value for; AI labels replace SigLIP's
    namespace (only when it ran); everything else (owner keywords, Owner|, Used|, the VLM's
    Review|vlm-*, Review|child-in-shot) is never removed. A file you kept (a favorite too: stars, its
    xmp:Rating) or trashed (owner.py) never gets Review|maybe-trash. A legacy Review|not-trash is
    rewritten to Owner|not-trash (migration on write; the decision is never dropped)."""
    if owner.kept(old, pick, stars) or owner.TRASH in old:
        new = [t for t in new if t != "Review|maybe-trash"]
    legacy = [t for t in old if t in owner.LEGACY]
    new = new + [h for h in dict.fromkeys(owner.LEGACY[t] for t in legacy) if h not in new]
    have = {t.split("|", 1)[0] + "|" for t in new if t.startswith(FACT_PREFIXES)}
    remove = [t for t in old if t not in new and ((relabelled and ai_owned(t)) or t in owner.LEGACY or
                                                  any(t.startswith(p) for p in have))]
    keep_leaves = ({t.split("|")[-1] for t in old if t not in remove} | {t.split("|")[-1] for t in new}
                   | set(owner_leaves))
    leaves = sorted({t.split("|")[-1] for t in remove} - keep_leaves)
    return new, remove, leaves


WRITE_CHUNK = 200  # sidecars per exiftool run in tag_files, so its progress moves while they are written


def tag_files(items, labels=True, label="tag"):
    """items: [(path, project, source, event, datetime or None[, fallback (lat, lon)])]; source a string or a
    tuple of them (the kind, then the device model: Source|gopro, Source|HERO11 Black). The fallback place (a
    cloud export's, exports.py) is used only when the file has no GPS. Writes sidecars; prints counts only,
    progress (Progress) under label."""
    need_exiftool()
    lab = guard_label()
    bad = set(lab.refused([i[0] for i in items], STATE))
    if bad:
        print(f"skipped {plural(len(bad), 'file')}: quarantined, staged or flagged by scan (never tagged)")
    items = [i for i in items if i[0] not in bad and os.path.isfile(i[0])]
    if not items:
        print("nothing to tag")
        return
    paths = [i[0] for i in items]
    prog = Progress(label, f"{plural(len(items), 'file')}: GPS, place names{', content labels' if labels else ''}, "
                           "then sidecars", unit="sidecars written", hits=None, total=len(items))
    gps = read_gps(paths)
    gps.update({i[0]: tuple(i[5]) for i in items if len(i) > 5 and i[5] and i[0] not in gps})
    pts = list(gps)
    if pts:
        prog.say(f"place names for {len(pts):,} files with GPS (offline)")
    places = dict(zip(pts, venv_json("geocode.py", [list(gps[p]) for p in pts]))) if pts else {}
    lbl = {}
    if labels:
        if not os.path.exists(VENV_PY):
            sys.exit(f"no venv: run bash {SETUP} first (or --no-labels)")
        prog.say(f"content labels for {len(paths):,} files (CPU-heavy; one pass, no per-file lines)")
        lbl = venv_json("label.py", paths)
    entries, c, lc = [], collections.Counter(), collections.Counter()
    existing = read_sidecar_tags(paths)
    for path, project, source, event, dt, *_ in items:
        hier = [f"Project|{project}"]
        for s in (source if isinstance(source, (list, tuple)) else [source]):  # e.g. ("gopro", "HERO11 Black")
            if s and not any(h.casefold() == f"source|{s}".casefold() for h in hier):  # Make-only "GoPro"
                hier.append(f"Source|{s}")
        if event:
            hier.append(f"Event|{event}")
        t = dict(event=event, date=dates.exif_string(dt) if dt else None)
        if path in gps:
            t["lat"], t["lon"] = gps[path]
            pl = places.get(path)
            if pl:
                hier.append("Place|" + ", ".join(x for x in (pl["city"], pl["state"], pl["cc"]) if x))
                t.update(city=pl["city"], state=pl["state"], country=pl["country"], cc=pl["cc"])
                c["with place"] += 1
            else:
                c["gps, no place within 50 km"] += 1
        for name in lbl.get(path, {}).get("labels", {}):
            hier.append(f"Label|{name}")
            lc[name] += 1
        for flag in lbl.get(path, {}).get("review", []):
            hier.append(f"Review|{flag}")
        old, pick, oleaves, stars = existing.get(path, ([], "", set(), None))
        hier, remove, leaves = merge_tags(old, hier, pick, relabelled=path in lbl, owner_leaves=oleaves, stars=stars)
        for h in hier:
            if h.startswith("Review|"):
                lc[h] += 1
        if remove:
            c["stale tags replaced"] += len(remove)
        entries.append((path, dict(t, hier=hier, remove=remove, drop_leaves=leaves)))
    for i in range(0, len(entries), WRITE_CHUNK):  # one exiftool run per chunk, each a progress tick
        write_tags(entries[i:i + WRITE_CHUNK])
        prog.tick(by=len(entries[i:i + WRITE_CHUNK]))
    print(f"tagged {plural(len(entries), 'file')} (sidecars <file>.xmp)"
          + ("; " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) if c else ""))
    if lc:
        print("labels: " + ", ".join(f"{k} {v}" for k, v in lc.most_common()))
    if lc.get("Review|child-in-shot"):
        print(f"{lc['Review|child-in-shot']} files may show a child: check them before any use "
              "(media-intake find --tag 'Review|child-in-shot')")
    if lc.get("Review|maybe-trash"):
        print(f"{lc['Review|maybe-trash']} files look like junk (blurry, dark, packaging...): "
              "media-intake suggest-trash shows them for Keep or Trash")


def cmd_tag(a):
    need_batch(a.batch)
    b = bdir(a.batch)
    plan = load(os.path.join(b, "plan.json"))
    if plan is None:
        sys.exit(f"plan and apply have not run for {a.batch}. Run: media-intake plan {a.batch}")
    flags = load(os.path.join(b, "scan.json"), {}) or {}
    items = []
    for p in plan:
        if p["action"] != "file" or p["rel"] in flags or not os.path.isfile(p["dst"]):
            continue
        project = p.get("project") or infer(p["dst"])[0]
        cat = p["cat"]
        src = SOURCE.get(cat) or (cat.split(":", 1)[1].lower() if cat.startswith("messaging:") else None)
        if p.get("device_model"):
            src = (src, p["device_model"])
        items.append((p["dst"], project, src, p.get("event"),
                      dates.aware(datetime.datetime.fromisoformat(p["date"]), p.get("date_tz")), p.get("export_gps")))
    nflag = sum(1 for p in plan if p["rel"] in flags)
    print(f"{plural(len(items), 'filed file')} to tag" + (f"; {nflag:,} flagged by scan are never tagged" if nflag else ""))
    tag_files(items, labels=not a.no_labels)
    # Plugins (docs/plugins.md) get the filed files, e.g. to register a project's clips.
    import plugins
    if plugins.modules():
        live = [i for i in items if os.path.isfile(i[0])]
        bad = set(guard_label().refused([i[0] for i in live], STATE))
        plugins.call("after_tag", [dict(path=os.path.abspath(i[0]), project=i[1], event=i[3],
                                        date=i[4].strftime("%Y-%m-%d") if i[4] else None)
                                   for i in live if i[0] not in bad])
    man = load(os.path.join(b, "manifest.json"), {}) or {}
    if man.get("kind") == "folder":  # offload is refused for a laptop folder: browse what was filed
        next_hint(["gallery"])
    else:  # the end of the flow: back the library up, then free the device
        next_hint(["backup", "<DRIVE>"], ["offload", a.batch, "--phone", man.get("root") or "<ROOT>"])


def sidecar_dates(paths):
    """Media paths whose sidecar already has XMP-exif:DateTimeOriginal (written by `tag` from the plan,
    possibly GPS- or --clock-offset-corrected): tag-files keeps it rather than re-reading the original."""
    xs = [sidecar(p) for p in paths if os.path.isfile(sidecar(p))]
    if not xs or not shutil.which("exiftool"):
        return set()
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-XMP-exif:DateTimeOriginal", "-@", "-"],
                       input="\n".join(xs) + "\n", capture_output=True, text=True)
    return {row["SourceFile"][:-4] for row in json.loads(r.stdout or "[]") if row.get("DateTimeOriginal")}


def cmd_tag_files(a):
    if a.event and not config.SLUG.match(a.event):  # before anything is read: same rule as plan --event
        sys.exit(f"--event {a.event!r}: a slug is lowercase letters, digits and dashes")
    files = [os.path.abspath(f) for f in a.files]
    keep = sidecar_dates(files)
    live = [f for f in files if os.path.isfile(f)]
    try:
        fresh = dates.resolve_all([f for f in live if f not in keep])
        model = {f: m.get("DeviceModel") for f, m in dates.read_meta([f for f in live if f in keep], gpmf=False).items()}
    except ValueError as e:  # a corrupt device salt
        sys.exit(str(e))
    model.update({f: d.model for f, d in fresh.items()})
    items = []
    for f in files:
        project, src, event = infer(f)
        if model.get(f):
            src = (src, model[f])
        items.append((f, project, src, a.event or event, fresh[f].aware() if f in fresh else None))
    tag_files(items, labels=not a.no_labels, label="tag-files")


def cmd_mark_used(a):
    need_exiftool()
    if not POST_SLUG.match(a.post):
        sys.exit("--post wants a slug starting with the post date, e.g. 2026-11-14-city-marathon-recap")
    files = [os.path.abspath(f) for f in a.files]
    bad = set(guard_label().refused(files, STATE))
    missing = [f for f in files if not os.path.isfile(f)]
    if bad or missing:
        sys.exit(f"not marking: {len(bad)} quarantined/flagged, {len(missing)} missing")
    write_tags([(f, {"hier": [f"Used|{a.post}"]}) for f in files])
    print(f"marked {len(files)} files Used|{a.post}")


def as_list(v):
    return [] if v is None else [str(x) for x in v] if isinstance(v, list) else [str(v)]


def cmd_find(a):
    need_exiftool()
    roots = a.root or LIB_ROOTS
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        sys.exit("no library folders found")
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-r", "-ext", "xmp", "-HierarchicalSubject", "-Event",
                        "-City", "-State", "-Country", *roots], capture_output=True, text=True)
    rows = json.loads(r.stdout or "[]")
    m = re.fullmatch(r"(\d+)d", a.unused_since or "0d")
    if not m:
        sys.exit("--unused-since wants Nd, e.g. 30d")
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(m.group(1)))) if a.unused_since else None
    hits = []
    for row in rows:
        x = row["SourceFile"]
        media = x[:-4]
        if not os.path.isfile(media):
            continue
        tags = as_list(row.get("HierarchicalSubject"))
        low = [t.casefold() for t in tags]
        if any(t.casefold() not in low and not any(l.startswith(t.casefold() + "|") for l in low) for t in a.tag or []):
            continue
        if a.event and not any(a.event.casefold() in s.casefold() for s in as_list(row.get("Event"))
                               + [t[6:] for t in tags if t.startswith("Event|")]):
            continue
        if a.place and not any(a.place.casefold() in s.casefold() for s in
                               [t[6:] for t in tags if t.startswith("Place|")]
                               + as_list(row.get("City")) + as_list(row.get("State")) + as_list(row.get("Country"))):
            continue
        if a.project and f"project|{a.project.casefold()}" not in low:
            continue
        used = [POST_SLUG.match(t[5:]) for t in tags if t.startswith("Used|")]
        if cutoff and any(u and datetime.date.fromisoformat(u.group(1)) >= cutoff for u in used):
            continue
        if a.never_used and used:
            continue
        hits.append(media)
    for h in sorted(hits):
        print(h)
    print(f"{len(hits)} files", file=sys.stderr)


# ---------------------------------------------------------------- share copy
def share_copy_refusal(files, out):
    """Why share-copy must not run (None if it may): every check is made before any copy, so a refusal
    writes nothing. A copy never lands in a library root or the state folder (it would be taken for a
    library file, and could replace an original of the same name there), never on a source file, and two
    sources never share one name in --out (the second would replace the first one's copy)."""
    real = os.path.realpath(out)
    for r in LIB_ROOTS + [STATE]:
        rr = os.path.realpath(r)
        if real == rr or real.startswith(rr + os.sep):
            return f"not copying: --out {out} is inside the library or the state folder"
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        return f"not copying: {len(missing)} files are missing"
    names = [os.path.basename(f) for f in files]
    if len(set(names)) != len(names):
        return "not copying: two of these have the same file name; share them one at a time"
    if any(os.path.realpath(os.path.join(out, n)) == os.path.realpath(f) for f, n in zip(files, names)):
        return f"not copying: --out {out} holds a source file itself; pick another folder"
    return None


def cmd_share_copy(a):
    import motion  # sibling module (sys.path set above); here so only share-copy loads it
    if refusal := share_copy_refusal(a.files, a.out):
        sys.exit(refusal)
    os.makedirs(a.out, exist_ok=True)
    for f in a.files:
        dst = os.path.join(a.out, os.path.basename(f))
        ext = os.path.splitext(f)[1].lower()
        if ext in VID:
            # Drop every data stream (GoPro GPMF GPS track), attached pictures (cover art: a JPEG with its
            # own EXIF; 0:V, not 0:v) and all metadata.
            # file: so a name with a colon (a:b.mp4) is never read as an ffmpeg protocol.
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "file:" + f, "-map", "0:V", "-map", "0:a?",
                            "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1", "file:" + dst], check=True)
        elif shutil.which("exiftool"):
            if os.path.exists(dst):
                os.remove(dst)  # exiftool -o refuses to overwrite
            subprocess.run(["exiftool", "-q", "-all=", "-tagsfromfile", "@", "-Orientation", "-ICC_Profile",
                            "-o", dst, f], check=True)
        else:
            # No exiftool yet: re-encode with Pillow, rotation baked in, no EXIF/XMP.
            from PIL import Image, ImageOps
            im = Image.open(f)
            icc = im.info.get("icc_profile")
            out = ImageOps.exif_transpose(im)
            out.info.pop("exif", None)
            out.info.pop("xmp", None)
            kw = dict(exif=b"", icc_profile=icc) if icc else dict(exif=b"")
            if ext in (".jpg", ".jpeg"):
                kw["quality"] = 95
            out.save(dst, **kw)
        if ext not in VID:
            # A motion photo's appended video (Samsung, Google) must not ride along with the still. exiftool
            # -all= drops a JPEG's trailer (its Trailer group; tests/test_motion.py checks) but may keep a
            # HEIC's top-level mpvd/sefd box, and Pillow writes a fresh file; strip cuts whatever is left off
            # this copy, never the original, and a copy it can't make safe is removed.
            try:
                motion.strip(dst)
            except (motion.MotionError, OSError) as e:
                if os.path.lexists(dst):
                    os.remove(dst)
                sys.exit(f"not copying {f}: its embedded video could not be removed from the copy ({e})")
        left = has_gps(dst)
        print(f"{dst}  gps={'STILL PRESENT' if left else 'none'}")


cmd_social_copy = cmd_share_copy  # older name


def clock_offset_arg(v):
    try:
        dates.parse_clock_offset(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return v


def build_parser(prog=None):
    """intake.py's commands. Every command's description says what it does, what it touches and whether it
    deletes ("Touches:" and "Deletes:" lines, tests/test_cli.py checks), and every argument has help text,
    in the words of docs/words.md. `media-intake --help` (cli.py) lists them by task."""
    # The words (docs/words.md): batch, source, phone, backup drive, library, quarantine, review folder.
    h_batch = "the batch name: the one `stage` printed (or you gave with --batch)"
    h_files = "library files (paths)"
    h_phone = "its folder, e.g. under /run/user/$UID/gvfs/ (connected and unlocked)"
    h_drive = "the backup drive: the folder it is mounted on, e.g. /run/media/$USER/<label>"
    h_yes = "don't ask: delete now (without it you are asked to type {word})"
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>", title="commands")

    def cmd(name, summary, what, touches, deletes):
        """One subcommand: summary for the command list; its --help opens with what it does, then its
        Touches: and Deletes: lines, each wrapped for an 80-column terminal."""
        import textwrap
        para = [textwrap.fill(" ".join(s.split()), 78, subsequent_indent="  " if i else "")
                for i, s in enumerate((what, f"Touches: {touches}", f"Deletes: {deletes}"))]
        desc = "\n".join([para[0], "", *para[1:]])
        return sp.add_parser(name, help=summary, formatter_class=argparse.RawDescriptionHelpFormatter,
                             description=desc)

    s = cmd("stage", "copy a source (phone, camera, drive, folder) into a new batch",
            "Copy the photos and videos from a source (a phone, a camera, a drive you import from, or a folder on\n"
            "this laptop) into a new batch, checking each copy's size. The first step of an import.",
            "reads the source; writes the batch in the state folder (by default ~/.local/share/media-intake).",
            "no. The source is only read.")
    s.add_argument("src", metavar="SOURCE", help="the source's folder: the phone's storage folder (the one holding "
                   "DCIM), a camera's card, a drive's folder, or a laptop folder")
    s.add_argument("--kind", choices=["phone", "gopro", "drive", "folder"], required=True,
                   help="what the source is: phone, gopro (a GoPro or its card), drive (a drive you import from, "
                        "not the backup drive) or folder (a folder on the laptop's own disk, under HOME)")
    s.add_argument("--batch", help="batch name (default: <device id>-<YYYYMMDD> from the files' camera details)")
    s.add_argument("--only", metavar="LISTFILE", help="stage only these paths, relative to SOURCE, one per line "
                   "(the queue uses it for its chunks)")
    s.add_argument("--origin", metavar="PHONE", help="the phone or camera the files really came from, when SOURCE "
                   "is an earlier copy of it (so erase --phone can reach it)")

    s = cmd("plan", "work out dates, library folders and duplicates; moves nothing",
            "Work out each staged file's date and library folder, and find exact and near duplicates.\n"
            "Nothing moves yet: apply does that.",
            "reads the batch and the library's index; writes the batch's plan.",
            "no.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("--event", action="append", metavar="[PROJECT/]SLUG@YYYY-MM-DD",
                   help="an event on that day: Event|SLUG tags; with a PROJECT/ prefix, or a slug matching a "
                        "project's events patterns in config.toml, that day's camera files go to the project's folder")
    s.add_argument("--clock-offset", action="append", metavar="DEVICE=+-Nh", type=clock_offset_arg,
                   help="shift dates from a camera whose clock was wrong (DEVICE: its Make, Model, 'Make Model' or "
                        "all; e.g. 'HERO11 Black=+3h'); GPS dates are never shifted; sidecars only")

    s = cmd("scan", "look for private files (IDs, cards, codes, nudity) with local detectors; flags only",
            "Run the local detectors (nudity, text on IDs and cards, QR codes and barcodes) on the batch.\n"
            "It only flags files; apply moves the flagged ones to quarantine. Needs the venv (bash setup.sh).",
            "reads the batch; writes the batch's scan results.",
            "no.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)

    s = cmd("apply", "file the batch into the library; flagged files go to quarantine",
            "Carry out the plan: file each clean file into the library, drop exact duplicates, set\n"
            "near-duplicates and junk aside in the review folder, and move flagged files to quarantine.\n"
            "Refuses until scan has run.",
            "the batch, the library, the review folder and quarantine.",
            "only the batch's staged copies: exact duplicates are dropped, and the staged folder goes once "
            "everything has moved. The source and the library lose nothing.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("--no-scan", action="store_true", help="file everything WITHOUT the sensitive-file check (IDs, "
                   "cards, nudity); only for photos you already know are safe")
    s.add_argument("--allow-missing-detectors", action="store_true", help="file even though the ID and card "
                   "detectors are not installed (those files will not be checked)")

    s = cmd("quarantine", "how many of a batch's files are in quarantine, and why; changes nothing",
            "Count a batch's quarantined files by reason and print the folder to open and the next commands.",
            "reads quarantine.",
            "no.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)

    s = cmd("release", "a false positive: move files from quarantine into the library",
            "Move quarantined files you checked and found fine into the library, where apply would have\n"
            "filed them. Same as Keep on the review page's Quarantine tab.",
            "quarantine and the library.",
            "no.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("files", nargs="+", metavar="FILE", help="quarantined files, as paths or names inside the "
                   "batch's quarantine folder")

    s = cmd("erase", "DELETES a batch's quarantined files and every copy, everywhere it can reach",
            "Erase a batch's quarantined files for good, with every identical copy the tool can reach: your\n"
            "home folder (your own photo folders included), thumbnails and caches, and with --phone and\n"
            "--drive the phone and the backup drive. Ends with a check and a report of what it could not\n"
            "reach. Asks you to type ERASE unless --yes.",
            "quarantine, the library, your home folder, and the phone and backup drive you name.",
            "YES, for good, every copy it finds.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("--phone", metavar="PHONE", help="also delete the matching files on this phone or camera: "
                   + h_phone)
    s.add_argument("--drive", metavar="DRIVE", help="also delete the matching files on " + h_drive
                   + ", then overwrite its free space (checked before anything is asked)")
    s.add_argument("--yes", action="store_true", help=h_yes.format(word="ERASE"))
    s.add_argument("--keep-phone-thumbnails", action="store_true", help="don't remove DCIM/.thumbnails and "
                   "Pictures/.thumbnails on the phone (removed by default with --phone)")
    s.add_argument("--no-wipe-free", action="store_true", help="skip overwriting the backup drive's free space")

    s = cmd("forget", "DELETES library files and every copy, as erase does (a private file found later)",
            "Erase library files for good, the same way erase does for a batch: every identical copy in your\n"
            "home folder, thumbnails and caches, and with --drive on the backup drive. For a private photo\n"
            "found after its import. Asks you to type ERASE unless --yes.",
            "the library, your home folder, and the backup drive you name.",
            "YES, for good, every copy it finds.")
    s.add_argument("files", nargs="+", metavar="FILE", help=h_files)
    s.add_argument("--drive", metavar="DRIVE", help="also delete the matching files on " + h_drive
                   + ", then overwrite its free space (checked before anything is asked)")
    s.add_argument("--yes", action="store_true", help=h_yes.format(word="ERASE"))
    s.add_argument("--no-wipe-free", action="store_true", help="skip overwriting the backup drive's free space")

    s = cmd("verify-gone", "check that erased files left no copy behind; changes nothing (exit 1 if one is left)",
            "Search your home folder (and the backup drive with --drive) for any copy of what was erased, and\n"
            "show the last erase's report. Exits 1 if a copy is still there.",
            "reads your home folder, the erase log and the backup drive you name.",
            "no.")
    s.add_argument("batch", nargs="?", metavar="BATCH", help="one batch (default: everything ever erased)")
    s.add_argument("--drive", metavar="DRIVE", help="also search " + h_drive)

    s = cmd("sweep-erased", "DELETES copies an erase missed (a backup drive unplugged then); a dry run without --yes",
            "Find copies of erased files that erase could not reach, such as on a backup drive that was\n"
            "unplugged at erase time, by size and checksum from the erase log. Never touches anything in\n"
            "the library. Without --yes it only counts them. With --yes it deletes them, zeroes the drive's\n"
            "free space if it deleted anything there, and ends with verify-gone.",
            "your home folder (unless --drive-only), the backup drive you name, the library index and the "
            "backup records.",
            "YES with --yes, every copy it finds; without it, nothing.")
    s.add_argument("batch", nargs="?", metavar="BATCH", help="one batch (default: everything ever erased)")
    s.add_argument("--drive", metavar="DRIVE", help="also sweep " + h_drive + ", every folder (its Trash too)")
    s.add_argument("--drive-only", action="store_true", help="sweep only the backup drive, not your home folder")
    s.add_argument("--no-wipe-free", action="store_true", help="skip overwriting the backup drive's free space")
    s.add_argument("--yes", action="store_true", help="delete now; without it, a dry run")

    s = cmd("wipe-free", "overwrite a backup drive's free space with zeros",
            "Fill the backup drive's free space with zeros, so files deleted from it can't be recovered.\n"
            "Refused on the laptop's own disk.",
            "the backup drive's free space (a temporary file, removed at the end).",
            "no file; it overwrites space that deleted files left.")
    s.add_argument("mount", metavar="DRIVE", help=h_drive)

    s = cmd("offload", "DELETES a batch's files from the phone, camera or source drive once they are in the library",
            "Free space on the source: delete each file of the batch that has a checked copy in the library\n"
            "(or was dropped as a duplicate). Run it after a backup. Refused for a laptop folder batch:\n"
            "delete that folder yourself. Asks you to type DELETE unless --yes.",
            "the phone, camera or source drive you name.",
            "YES, from the source, only files with a checked copy in the library.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("--phone", required=True, metavar="SOURCE", help="the source the batch was staged from: the "
                   "phone's or camera's folder, or the source drive's folder (not the backup drive)")
    s.add_argument("--yes", action="store_true", help=h_yes.format(word="DELETE"))

    s = cmd("purge-review", "DELETES a batch's review folder (near-duplicates and junk); a dry run without --yes",
            "Delete the batch's review folder: the near-duplicates and junk apply set aside.\n"
            "Without --yes it only says what would go.",
            "the batch's review folder.",
            "YES with --yes; without it, nothing.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("--yes", action="store_true", help="delete now; without it, a dry run")

    s = cmd("trash", "DELETES junk files (not private ones) and skips them on later imports; a dry run without --yes",
            "Trash library or review-folder files: delete them with their sidecars and thumbnails (with\n"
            "--phone their originals on the phone too), and remember them so the same photo is skipped\n"
            "if it is imported again. Not for quarantined files: erase those. Without --yes it only says\n"
            "what would go.",
            "the library, the review folder, and the phone you name.",
            "YES with --yes; without it, nothing.")
    s.add_argument("args", nargs="*", metavar="[BATCH] FILE", help="the files; a batch name first says which batch "
                   "they came from (for --phone)")
    s.add_argument("--batch", metavar="BATCH", help="the batch the files came from (for --phone)")
    s.add_argument("--phone", metavar="PHONE", help="also delete their originals from this phone or camera: "
                   + h_phone)
    s.add_argument("--from-digikam", action="store_true", help="trash the library files marked Rejected in digiKam")
    s.add_argument("--yes", action="store_true", help="delete now; without it, a dry run")

    s = cmd("not-trash", "Keep a suggested file: it is never suggested as junk again",
            "Mark library files as kept (the review page's and gallery's Keep), so they are never suggested\n"
            "as junk again.",
            "the files' sidecars (tags).",
            "no.")
    s.add_argument("files", nargs="+", metavar="FILE", help=h_files)

    s = cmd("migrate-tags", "one-off: rename an older tool tag in your sidecars",
            "One-off and safe to repeat: rewrite the older Review|not-trash tag as Owner|not-trash in every\n"
            "library sidecar. Nothing else changes.",
            "the library's sidecars (tags).",
            "no.")
    s.add_argument("--dry-run", action="store_true", help="count the sidecars, write nothing")

    s = cmd("suggest-trash", "list library files that look like junk (Suggested junk) and open them on the review page",
            "Find library files the tool thinks are junk (Suggested junk) and open them on the review page's\n"
            "Maybe trash tab, where you Keep or Trash each. Never suggests one you kept.",
            "reads the library's sidecars; writes the list to the state folder.",
            "no (Trash on the review page does).")
    s.add_argument("--batch", metavar="BATCH", help="only files this batch filed")
    s.add_argument("--phone", metavar="PHONE", help="passed to the review page, so Trash there also deletes the "
                   "originals from this phone or camera")
    s.add_argument("--no-serve", action="store_true", help="write the list and stop (no review page)")

    for name in ("share-copy", "social-copy"):
        s = cmd(name, "copy files without GPS or other metadata, for sharing"
                + (" (older name of share-copy)" if name != "share-copy" else ""),
                "Copy files into a folder with their GPS position and other metadata removed, for posting\n"
                "or sending. The originals are not changed.",
                "reads the files; writes copies to --out.",
                "no.")
        s.add_argument("files", nargs="+", metavar="FILE", help="the photos or videos to copy")
        s.add_argument("--out", required=True, metavar="DIR", help="folder for the copies (not inside the library "
                       "or the state folder)")

    s = cmd("tag", "after apply: write tags (source, date, place, event, labels) for the batch's filed files",
            "Write each filed file's tags (project, source, event, place, date and content labels) to its\n"
            "sidecar (<file>.<ext>.xmp). Labels are CPU-heavy: --no-labels skips them.",
            "the sidecars next to the batch's library files; never the photos themselves.",
            "no.")
    s.add_argument("batch", metavar="BATCH", help=h_batch)
    s.add_argument("--no-labels", action="store_true", help="project, source, event, place and date only; "
                   "no content labels")

    s = cmd("tag-files", "write tags again for library files (project, source and event from their folder)",
            "Write tags again for library files, as tag does for a batch; project, source and event come\n"
            "from each file's folder.",
            "the files' sidecars; never the photos themselves.",
            "no.")
    s.add_argument("files", nargs="+", metavar="FILE", help=h_files)
    s.add_argument("--event", metavar="SLUG", help="event slug for all of them (lowercase letters, digits and dashes)")
    s.add_argument("--no-labels", action="store_true", help="no content labels (faster)")

    s = cmd("mark-used", "record that library files were used in a post (a Used|<post> tag)",
            "Tag library files as used in a post, so find --unused-since and --never-used can skip them.",
            "the files' sidecars.",
            "no.")
    s.add_argument("files", nargs="+", metavar="FILE", help=h_files)
    s.add_argument("--post", required=True, metavar="SLUG", help="post slug, starting with its date: YYYY-MM-DD-...")

    s = cmd("find", "search the tags; prints the matching files' paths",
            "Search the library's sidecar tags and print each matching file's path (the count goes to\n"
            "stderr). Every option given must match.",
            "reads the library's sidecars.",
            "no.")
    s.add_argument("--tag", action="append", help="hierarchical tag, e.g. 'Label|runner' or 'Label' (any label); "
                   "repeat = all of them")
    s.add_argument("--event", metavar="SLUG", help="part of an event's name")
    s.add_argument("--place", help="part of a city, state or country name")
    s.add_argument("--project", help="a project's name, as in config.toml")
    s.add_argument("--unused-since", metavar="Nd", help="not used in a post in the last N days")
    s.add_argument("--never-used", action="store_true", help="never used in a post")
    s.add_argument("--root", action="append", metavar="DIR", help="search this folder instead of the library "
                   "(repeat for more)")
    return ap


# The library lock (library_lock.py; docs/intake.md, "One writer at a time"): every subcommand is in exactly one
# of these (tests/test_library_lock.py checks), and main() is the only place that takes it for the CLI.
LOCKED = {"stage", "plan", "scan", "apply", "release", "erase", "forget", "sweep-erased", "offload", "purge-review",
          "trash", "not-trash", "suggest-trash", "tag", "tag-files", "mark-used", "migrate-tags"}
READ_ONLY = {"quarantine", "verify-gone", "wipe-free", "share-copy", "social-copy", "find"}


def main(argv=None, prog=None):
    a = build_parser(prog).parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # a job's log or a pipe gets each line as it is printed
        sys.stdout.reconfigure(line_buffering=True)
    run = globals()["cmd_" + a.cmd.replace("-", "_")]
    if a.cmd in READ_ONLY:
        run(a)
        return
    import library_lock
    try:
        with library_lock.hold(f"intake.py {a.cmd}", wait=library_lock.cli_wait(), out=sys.stderr):
            run(a)
    except library_lock.Busy as e:
        print(f"media-intake {a.cmd}: refused: {e}", file=sys.stderr)
        sys.exit(library_lock.EX_TEMPFAIL)


if __name__ == "__main__":
    main()
