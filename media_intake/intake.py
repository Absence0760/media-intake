#!/usr/bin/env python3
"""Media intake: phone/GoPro -> organised library, junk and duplicates out,
sensitive files quarantined for you to review, erased everywhere on confirm.
Run as `media-intake <command>` (cli.py); `python3 -I media_intake/intake.py` works too.

  media-intake stage   SRC_ROOT --kind phone|gopro|drive --batch NAME [--only LISTFILE]
  media-intake plan    BATCH [--event [PROJECT/]SLUG@YYYY-MM-DD ...] [--clock-offset DEVICE=±Nh ...]
  media-intake scan    BATCH                     (local detectors; labels only)
  media-intake apply   BATCH                     (moves files; prints a summary)
  media-intake quarantine BATCH                  (counts + the folder to open)
  media-intake release BATCH FILE...             (false positive -> library)
  media-intake erase   BATCH [--phone ROOT [--keep-phone-thumbnails]] [--drive MOUNT [--no-wipe-free]]
  media-intake forget  LIBRARY_FILE... [--drive MOUNT]   (later finds)
  media-intake verify-gone [BATCH] [--drive MOUNT]  (read-only sweep; exit 1 if a copy remains)
  media-intake wipe-free MOUNT                   (zero a backup drive's free space)
  media-intake purge-review BATCH                (empty the review folder)
  media-intake trash   [BATCH] FILE... [--phone ROOT]   (plain delete of non-sensitive junk; never comes back)
  media-intake trash   --from-digikam [--yes]    (library files with digiKam's Rejected pick label)
  media-intake suggest-trash [--batch B] [--phone ROOT]  (Review|maybe-trash files -> review page)
  media-intake not-trash FILE...                 (keep a suggestion: Owner|not-trash)
  media-intake offload BATCH --phone ROOT        (after a drive backup: free the phone; a drive batch: its source root)
  media-intake share-copy FILE... --out DIR      (copy without GPS/metadata; alias social-copy)
  media-intake tag     BATCH [--no-labels]       (after apply: XMP sidecar tags for filed files)
  media-intake tag-files FILE... [--event SLUG] [--no-labels]   (re-tag library files)
  media-intake mark-used FILE... --post SLUG     (adds Used|<post slug>)
  media-intake gallery [--drive MOUNT]           (local web gallery: browse, keep, trash, erase)
  media-intake find    [--tag T] [--event E] [--place P] [--project P] [--unused-since 30d]

Tags live in <file>.<ext>.xmp sidecars next to each file (digiKam's default
naming); originals are never written (their sha256 drives dedupe and erase).

Layout (docs/intake.md; every path comes from config.toml, docs/configuration.md):
  ~/Pictures/library/<YYYY>/<YYYY-MM>/                photos, original names
  ~/Pictures/library/Screenshots/<YYYY>/
  ~/Videos/personal/Screen recordings/<YYYY>/
  ~/Pictures/library/Messaging/<app>/<YYYY>/
  ~/Videos/personal/<YYYY>/<YYYY-MM>/                 videos (GoPro included)
  <project folder>/<date>-<event>/{phone,gopro}/     events routed to a configured project
  ~/.local/share/media-intake/  (0700, never synced or indexed)
    batches/<batch>/raw/          staged copy, deleted once applied
    batches/<batch>/review/       near-duplicates + junk for a glance
    quarantine/<batch>/           sensitive files; no assistant or tool ever opens these
    index.json                    sha256 + dHash of everything in the library

Test: python3 -I -m unittest discover -s tests (synthetic media, throwaway HOME).

An AI assistant may run these commands for you, but must never read, view,
montage or describe a file under quarantine/ or a file scan.json flags.
"""
import argparse, collections, datetime, glob, hashlib, json, os, re, shutil, stat, subprocess, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import dates  # noqa: E402
import sources  # noqa: E402

CFG = config.get()
HOME = os.path.expanduser("~")
STATE, LIBRARY, VIDEOS = CFG["state"], CFG["library"], CFG["videos"]
PROJECTS = CFG["projects"]  # {key: {name, folder, events, protect}}
LIB_ROOTS = config.library_roots(CFG)  # library, videos, every project folder
VENV_PY = f"{STATE}/venv/bin/python"
SETUP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "setup.sh")
DEPS = ("sudo dnf install perl-Image-ExifTool tesseract tesseract-langpack-eng python3-zxing-cpp "
        "ffmpeg-free (README, Install)")
# gallery.py's thumbnails (copies of photo content): erase, forget and trash delete them by sha256.
THUMBS = os.path.join(HOME, ".cache", "media-intake", "thumbs")
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


def bdir(batch):
    return os.path.join(STATE, "batches", batch)


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


def gallery_thumb_forget(sha):
    """Delete gallery.py's cached thumbnail of this content (keyed by sha256). Returns 1 if one went."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        return 0
    p = os.path.join(THUMBS, sha + ".jpg")
    if os.path.lexists(p):
        os.remove(p)
        return 1
    return 0


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


def category(rel, kind):
    p = rel.split("/")
    sub = p[1] if len(p) > 2 else ""
    ext = os.path.splitext(rel)[1].lower()
    if kind == "gopro":
        return "gopro"
    if kind == "drive":
        return sources.drive_category(rel)
    app = sources.extra_app(rel)
    if app:
        return f"messaging:{app}"
    if p[0] == "DCIM":
        return {"Screenshots": "screenshot", "Videocaptures": "screenrec",
                "Screen recordings": "screenrec"}.get(sub, "video" if ext in VID else "photo")
    if p[0] == "Pictures" and sub == "Screenshots":  # Pixel and stock Android
        return "screenshot"
    if p[0] == "Pictures" and sub:
        return f"messaging:{sub}"
    return "photo"


# ---------------------------------------------------------------- stage
def cmd_stage(a):
    root = os.path.abspath(a.src)
    raw = private_dir(os.path.join(private_dir(bdir(a.batch)), "raw"))
    man = {}
    skipped = collections.Counter()
    extra = {}
    if a.kind == "drive":  # refuses the laptop's disk and the backup drive; uuid lets offload check it
        try:
            extra = dict(mount=sources.check_drive_root(root), uuid=sources.drive_uuid(root))
        except sources.Denied as e:
            sys.exit(f"refused: {e}")
    only = None
    if a.only:  # drive_queue.py chunks: one relative path per line
        with open(a.only) as f:
            only = {l.rstrip("\n") for l in f if l.strip()}
    for path, rel in sources.walk(root, a.kind):
        if only is not None and rel not in only:
            continue
        dst = os.path.join(raw, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        st = os.stat(path)
        if not (os.path.exists(dst) and os.path.getsize(dst) == st.st_size):
            shutil.copyfile(path, dst)
            os.utime(dst, (st.st_mtime, st.st_mtime))
        if os.path.getsize(dst) != st.st_size:
            sys.exit(f"size mismatch after copy: {rel}")
        origin = os.path.join(os.path.abspath(a.origin), rel) if a.origin else path
        man[rel] = dict(src=origin, size=st.st_size)
    for dirpath, _, names in os.walk(root) if a.kind == "gopro" else []:
        for n in names:
            if os.path.splitext(n)[1].lower() in sources.CAMERA_JUNK_EXT:
                skipped["gopro preview (.lrv/.thm)"] += 1
    save(os.path.join(bdir(a.batch), "manifest.json"), dict(kind=a.kind, root=os.path.abspath(a.origin or root), files=man, **extra))
    print(f"staged {len(man)} media files into {raw}")
    for k, v in skipped.items():
        print(f"  skipped {v} {k}")


# ---------------------------------------------------------------- plan
def cmd_plan(a):
    b = bdir(a.batch)
    man = load(os.path.join(b, "manifest.json"))
    raw = os.path.join(b, "raw")
    kind = man["kind"]
    routed, personal = {}, {}  # day -> (project, slug) / day -> slug
    for t in a.event or []:
        spec, _, day = t.partition("@")
        key, _, ev = spec.rpartition("/")
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day) or not config.SLUG.match(ev):
            sys.exit(f"--event wants [PROJECT/]SLUG@YYYY-MM-DD (slug: lowercase, digits, dashes), got {t}")
        if key and key not in PROJECTS:
            sys.exit(f"--event {t}: no project {key!r} in {CFG['path']} (known: {', '.join(PROJECTS) or 'none'})")
        proj = PROJECTS[key] if key else config.project_for_event(ev, CFG)
        if proj:
            routed[day] = (proj, ev)  # camera files of that day go to the project's folder
        else:
            personal[day] = ev  # tags only, files still go by date
    index = load(os.path.join(STATE, "index.json"), {})
    by_sha = {v["sha"]: k for k, v in index.items()}

    try:
        dated = dates.resolve_all([os.path.join(raw, rel) for rel in man["files"]],
                                  dates.clock_offsets(a.clock_offset))
    except ValueError as e:
        sys.exit(str(e))
    rows = []
    for rel in sorted(man["files"]):
        p = os.path.join(raw, rel)
        dd = dated[p]
        ext = os.path.splitext(rel)[1].lower()
        dh, w, h = dhash(p) if ext in IMG else (None, 0, 0)
        rows.append(dict(rel=rel, sha=sha256(p), size=os.path.getsize(p), date=dd.dt.isoformat(), date_src=dd.src,
                         date_tz=dd.tz, date_from=dd.base, date_note=dd.note, date_suspect=dd.suspect,
                         cat=category(rel, kind), dhash=dh, w=w, h=h, ext=ext))

    pref = {"photo": 5, "video": 5, "gopro": 5, "screenshot": 3, "screenrec": 3}
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
    imgs = [r for r in rows if r["dhash"] is not None and r["rel"] not in action]
    lib_imgs = [(k, v) for k, v in index.items() if v.get("dhash") is not None]
    for i, x in enumerate(imgs):
        if x["rel"] in action:
            continue
        for y in imgs[i + 1:]:
            if y["rel"] in action or bin(x["dhash"] ^ y["dhash"]).count("1") > 2:
                continue
            if (x["w"] >= x["h"]) != (y["w"] >= y["h"]) or (x["cat"] == y["cat"] == "photo"):
                continue  # burst shots in the camera roll are never merged
            keep, drop = (x, y) if rank(x) >= rank(y) else (y, x)
            action[drop["rel"]] = ("review", f"near-duplicate of {keep['rel']}")
            if drop is x:
                break
        if x["rel"] not in action and x["cat"] != "photo":
            for k, v in lib_imgs:
                if bin(x["dhash"] ^ v["dhash"]).count("1") <= 2:
                    action[x["rel"]] = ("review", f"near-duplicate of library {k}")
                    break
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
    dests = collections.Counter(os.path.relpath(p["dst"], HOME).split("/")[:3][-1] if p["action"] == "file" else p["action"]
                                for p in plan)
    print(f"plan: {len(plan)} files  file {c['file']}  delete(exact dup) {c['delete']}  review {c['review']}")
    print("dates from", dict(collections.Counter(p["date_src"] for p in plan)))
    for label, sel in (("camera dates replaced by GPS", lambda p: p["date_src"] == "gps" and p["date_note"]),
                       ("dates shifted by --clock-offset", lambda p: p["date_src"] == "offset"),
                       ("dates to check (clock-reset default, camera and GPS disagree, or nothing plausible; "
                        "--clock-offset fixes a wrong clock)", lambda p: p["date_suspect"])):
        hit = sorted(p["rel"] for p in plan if sel(p))
        if hit:
            print(f"  {len(hit)} {label}: " + ", ".join(hit[:5]) + (" ..." if len(hit) > 5 else ""))
    for k in sorted({os.path.dirname(os.path.relpath(p["dst"], HOME)) for p in plan if p["action"] == "file"}):
        print("  ~/" + k)


# ---------------------------------------------------------------- scan
def cmd_scan(a):
    b = bdir(a.batch)
    if not os.path.exists(VENV_PY):
        sys.exit(f"no venv: run bash {SETUP} first")
    out = os.path.join(b, "scan.json")
    r = subprocess.run([VENV_PY, "-I", os.path.join(HERE, "scan_sensitive.py"), os.path.join(b, "raw"), out])
    sys.exit(r.returncode)


# ---------------------------------------------------------------- apply
def cmd_apply(a):
    b = bdir(a.batch)
    plan = load(os.path.join(b, "plan.json"))
    flags = load(os.path.join(b, "scan.json"))
    if flags is None and not a.no_scan:
        sys.exit("run `scan` first (or --no-scan to file everything without the sensitive check)")
    flags = flags or {}
    missing = [t for t in ("tesseract",) if not shutil.which(t)]
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        missing.append("python3-zxing-cpp")
    if missing and not a.no_scan and not a.allow_missing_detectors:
        sys.exit(f"document detectors missing ({', '.join(missing)}): IDs, cards and 2FA codes were not "
                 f"checked. Install them ({DEPS}), then `scan` again "
                 "(or --allow-missing-detectors to file anyway).")
    raw = os.path.join(b, "raw")
    quar = private_dir(os.path.join(private_dir(os.path.join(STATE, "quarantine")), a.batch))
    index = load(os.path.join(STATE, "index.json"), {})
    qman = load(os.path.join(quar, "quarantine.json"), {})
    c = collections.Counter()
    for p in plan:
        src = os.path.join(raw, p["rel"])
        if not os.path.exists(src):
            continue
        if p["rel"] in flags and p["action"] != "delete":
            dst = os.path.join(quar, p["rel"])
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
            os.makedirs(os.path.dirname(p["dst"]), exist_ok=True)
            shutil.move(src, p["dst"])
            if p["action"] == "file":
                index[p["dst"]] = dict(sha=p["sha"], size=p["size"], dhash=p["dhash"])
                c["filed"] += 1
            else:
                c["to review"] += 1
    save(os.path.join(quar, "quarantine.json"), qman)
    save(os.path.join(STATE, "index.json"), index)
    left = [os.path.join(r, f) for r, _, fs in os.walk(raw) for f in fs]
    if not left:
        shutil.rmtree(raw)
    print(dict(c), f"left in raw: {len(left)}")
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


# ---------------------------------------------------------------- quarantine review
def cmd_quarantine(a):
    quar = os.path.join(STATE, "quarantine", a.batch)
    q = load(os.path.join(quar, "quarantine.json"), {})
    c = collections.Counter(r for v in q.values() for r in v["reasons"])
    print(f"{len(q)} files in quarantine: {quar}")
    for k, v in c.most_common():
        print(f"  {k:28} {v}")
    print(f"Open it yourself:\n  xdg-open {os.path.abspath(quar)}\nFor any false positive, run\n"
          f"  media-intake release {a.batch} <file>...\n"
          f"then erase the rest with\n  media-intake erase {a.batch} --phone <device root>")


def cmd_release(a):
    quar = os.path.join(STATE, "quarantine", a.batch)
    q = load(os.path.join(quar, "quarantine.json"), {})
    index = load(os.path.join(STATE, "index.json"), {})
    for f in a.files:
        rel = os.path.relpath(os.path.abspath(f), quar) if os.path.isabs(f) or f.startswith(".") else f
        if rel not in q or not q[rel]["dst"]:
            print(f"not in quarantine (or was a near-duplicate): {f}")
            continue
        dst = q[rel]["dst"]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(quar, rel), dst)
        os.chmod(dst, 0o644)
        if os.path.isfile(sidecar(os.path.join(quar, rel))):  # a near-duplicate's own tags; none are created
            shutil.move(sidecar(os.path.join(quar, rel)), sidecar(dst))
        dh = dhash(dst)[0] if os.path.splitext(dst)[1].lower() in IMG else None
        index[dst] = dict(sha=q[rel]["sha"], size=q[rel]["size"], dhash=dh, released=True)
        thumb_forget(os.path.join(quar, rel))
        del q[rel]
        print(f"released -> {dst}")
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


def near_roots():
    """The media library locations near-duplicates are looked for in, and nowhere else: app
    icon caches, map tiles, render frames and repo assets look alike and aren't your photos."""
    roots = list(LIB_ROOTS)
    roots += glob.glob(os.path.join(STATE, "batches", "*", "raw")) + glob.glob(os.path.join(STATE, "batches", "*", "review"))
    return [r for r in roots if os.path.isdir(r)]


def where(path, base):
    """Top-level location of path for a count line: 'Pictures', '.cache/some-app', 'Trash'."""
    rel = os.path.relpath(path, base).split(os.sep)
    if rel[:3] == [".local", "share", "Trash"]:
        return "Trash"
    if rel[0].startswith(".") and len(rel) > 2:
        return "/".join(rel[:2]) if rel[0] != ".local" else "/".join(rel[:3])
    return rel[0] if len(rel) > 1 else "(home)"


def sweep(sig, delete=True, drive=None):
    """Find files anywhere in HOME (and on drive) with an erased item's size+sha256.

    sig is {sha: size}. Only same-size files are hashed. With delete, matches go
    (a Trash match takes its .trashinfo, any match its .xmp sidecar), and so do
    orphaned sidecars (<file>.<ext>.xmp whose file is gone: they hold its place
    and labels). Matches inside a git working tree are never deleted (that leaves the
    repo dirty and git history keeps the content): they're returned in in_git.
    Returns (Counter by location, paths, Counter of orphan sidecars by location, in_git paths).
    """
    sizes = set(sig.values())
    found, paths, orphans, in_git = collections.Counter(), [], collections.Counter(), []
    roots = [(HOME, SWEEP_SKIP)] + ([(os.path.abspath(drive), (WIPE_NAME,))] if drive else [])
    for root, skip in roots:
        for p, git in walk_files(root, skip, git=True):
            try:
                if git:
                    st = os.lstat(p)
                    if stat.S_ISREG(st.st_mode) and st.st_size in sizes and sig.get(sha256(p)) == st.st_size:
                        in_git.append(p)
                    continue
                if p.endswith((".xmp", ".XMP")) and orphan_sidecar(p):
                    orphans[("drive:" if root != HOME else "") + where(p, root)] += 1
                    if delete:
                        os.remove(p)
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
                os.remove(p)
                drop_sidecar(p)
                trash = os.path.join(HOME, ".local/share/Trash/files")
                if os.path.dirname(p) == trash:
                    info = os.path.join(HOME, ".local/share/Trash/info", os.path.basename(p) + ".trashinfo")
                    if os.path.exists(info):
                        os.remove(info)
    return found, paths, orphans, in_git


def drive_purge(mount, shas):
    """Delete files anywhere on a backup drive whose sha256 is in shas (size prefilter)."""
    sizes = {s for s, _ in shas.values()}
    n = 0
    for p in walk_files(os.path.abspath(mount), (WIPE_NAME,)):
        try:
            if os.path.getsize(p) in sizes and sha256(p) in shas:
                os.remove(p)
                drop_sidecar(p)
                n += 1
        except OSError:
            pass
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


def near_dups(refs, kept, index, batch, spare=()):
    """Move images within NEAR_MAX of an erased image into quarantine/near-<batch>/ (never deleted).

    refs: [(dhash, landscape, sha256)] of the erased images. Each moved file records the
    sha256 of the erased image it matched and the erase date (never its path or content),
    so the review page can say "near-copy of a photo you erased on <date>". Looks only in near_roots() (the
    library, videos, project folders, every batch's raw/ and review/), never inside a git working tree. spare: paths never moved (the
    full-quality originals an xcheck batch's messaging copies point at with `of`).
    """
    if not refs:
        return 0, None
    near = f"near-{batch}"
    qroot = os.path.join(STATE, "quarantine")
    cands = set()
    for root in near_roots():
        for p, git in walk_files(root, git=True):
            if not git and os.path.splitext(p)[1].lower() in IMG and not p.startswith(qroot + os.sep):
                cands.add(p)
    lib_dh = {k: v.get("dhash") for k, v in index.items()}
    moved, q, quar = 0, {}, os.path.join(qroot, near)
    spare = {os.path.abspath(x) for x in spare}
    for p in sorted(cands - spare):
        pre = lib_dh.get(p)
        if pre is not None and all(bin(pre ^ d).count("1") > NEAR_MAX + 2 for d, *_ in refs):
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
        private_dir(quar)
        rel = os.path.relpath(p, HOME)
        dst = os.path.join(quar, rel)
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        shutil.move(p, dst)
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
        if os.path.isfile(sidecar(p)) and not os.path.islink(sidecar(p)):
            shutil.move(sidecar(p), sidecar(dst))  # goes with the file; erased with the quarantine
        q[rel] = dict(reasons=["near-duplicate"], sha=s, size=os.path.getsize(dst), dst=p,
                      near_of_sha=hit[2], erased_on=f"{datetime.date.today():%Y-%m-%d}")
        thumb_forget(p)
        index.pop(p, None)
        moved += 1
    if q:
        save(os.path.join(quar, "quarantine.json"), q)
    return moved, near


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
    # `reset --file` forces it for each erased path inside an indexed folder.
    if shutil.which("localsearch") and os.environ.get("MEDIA_INTAKE_NO_LOCALSEARCH") != "1":  # tests: off
        r = subprocess.run(["localsearch", "index"], capture_output=True, text=True)
        rows = re.findall(r"^(/.*?)\s+([*-])\s*$", re.sub(r"\x1b\[[0-9;]*m", "", r.stdout), re.M)
        for u in uris:
            if not u.startswith("file://"):
                continue
            path = urllib.parse.unquote(u[7:])
            if any(os.path.dirname(path) == d or (rec == "*" and path.startswith(d + os.sep)) for d, rec in rows):
                if subprocess.run(["localsearch", "reset", "--file", path], capture_output=True).returncode == 0:
                    n["localsearch resets"] += 1
    return n


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


def device_targets(root, items, known, check=sources.check, skip_denied=True, hint=""):
    """The device files a command may delete, checked before anything is deleted.

    items: (path, size) pairs the command wants gone from the device at root (size
    NO_SIZE_CHECK for the thumbnail folders, else the staged size); known: every path the
    device listed (the batch manifests' src), so a target is compared with names that are
    not targets too. Skipped, a line each: a path outside root or outside the allowlist
    (check; with skip_denied False that refuses the whole command instead, hint appended).
    Skipped quietly: a file already gone or no longer the staged size. Refused, nothing
    deleted: a file still there with no recorded size, and any target whose path differs
    only by letter case from another known path or target (sources.check_case). Returns
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
    try:
        sources.check_case(out, known)
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


def wipe_free(mount):
    """Overwrite a backup drive's free space with zeros (exFAT deletes only free clusters).

    Returns (MB written, seconds, note) or raises SystemExit if mount is the laptop's disk.
    """
    import errno, time
    mount = os.path.abspath(mount)
    if not os.path.isdir(mount):
        sys.exit(f"no such directory: {mount}")
    target = subprocess.run(["findmnt", "-n", "-o", "TARGET", "-T", mount], capture_output=True, text=True).stdout.strip()
    override = os.environ.get("MEDIA_INTAKE_WIPE_ANY_DEST") == "1"  # tests only
    if not override and (not target or LAPTOP_MOUNTS.match(target)):
        sys.exit(f"{mount} is on the laptop's own disk ({target or 'unknown'}), not a backup drive: not wiping")
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
                    pass
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
    if src.startswith("/dev/") and os.path.exists(rot) and open(rot).read().strip() == "0":
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
        pass
    return sig, nosize


def verify_gone(sig, drive=None):
    found, _, orphans, in_git = sweep(sig, delete=False, drive=drive)
    gthumbs = sum(os.path.lexists(os.path.join(THUMBS, s + ".jpg")) for s in sig)
    if gthumbs:
        found["gallery thumbnails (~/.cache/media-intake/thumbs)"] += gthumbs
    if in_git:
        print(f"{len(in_git)} exact copies inside git repos (erase leaves them): " + ", ".join(in_git))
    if found:
        print(f"STILL PRESENT: {sum(found.values())} copies: " + ", ".join(f"{k} {v}" for k, v in sorted(found.items())))
    else:
        print(f"verify-gone: no copy of {len(sig)} erased items in HOME" + (" or on the drive" if drive else ""))
    if orphans:
        print(f"ORPHAN SIDECARS: {sum(orphans.values())} .xmp files whose photo/video is gone (erase removes them): "
              + ", ".join(f"{k} {v}" for k, v in sorted(orphans.items())))
    return not found and not orphans


def cmd_verify_gone(a):
    sig, nosize = erase_log_sigs(a.batch)
    if nosize:
        print(f"{nosize} older erase-log entries have no size and can't be swept")
    if not sig:
        sys.exit(f"no erase-log entries with sizes{' for ' + a.batch if a.batch else ''}")
    sys.exit(0 if verify_gone(sig, a.drive) else 1)


def cmd_erase(a):
    quar = os.path.join(STATE, "quarantine", a.batch)
    q = load(os.path.join(quar, "quarantine.json"), {})
    man = load(os.path.join(bdir(a.batch), "manifest.json"), {"files": {}})
    plan = load(os.path.join(bdir(a.batch), "plan.json"), [])
    if not q:
        sys.exit("quarantine is empty")
    if not a.yes:
        ans = input(f"Erase {len(q)} quarantined files for good{' and from the phone' if a.phone else ''}"
                    f"{' and the drive' if a.drive else ''}? Type ERASE: ")
        if ans.strip() != "ERASE":
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
                refs.append((dh, land, v["sha"]))
    gone = set()  # every path erased or moved: for recent files, app state and the index
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
    # Anything unexpected in a thumbnail folder (a symlink, a path resolving elsewhere) refuses erase:
    # the yes was given for folders of thumbnails, so a folder that isn't one is for you to look at.
    pthumb_todo = device_targets(root, pthumb_items, known, phone_thumbnail_check, skip_denied=False,
                                 hint=" (look at the folder on the phone, or pass --keep-phone-thumbnails)") \
        if do_pthumbs else []
    thumbs = phone = 0
    for rel in list(q):
        p = os.path.join(quar, rel)
        if os.path.exists(p):
            os.remove(p)
        gone |= {p, os.path.join(bdir(a.batch), "raw", rel)}
        if q[rel].get("dst"):
            gone.add(q[rel]["dst"])  # a near-duplicate's original place
        thumbs += thumb_forget(p) + thumb_forget(os.path.join(bdir(a.batch), "raw", rel))
        if q[rel]["sha"] in shas:
            thumbs += gallery_thumb_forget(q[rel]["sha"])
    if root:
        for src, _ in phone_items:
            thumbs += thumb_forget(src)
            gone.add(src)
    phone = len(device_remove(phone_todo))
    pthumbs, prefused = phone_thumbnails(pthumb_todo)
    # Identical copies already in the library (filed by an earlier batch) are the
    # same picture: erase them too.
    lib = 0
    for path, v in list(index.items()):
        if v["sha"] in shas:
            if os.path.exists(path):
                os.remove(path)
                lib += 1
            drop_sidecar(path)
            thumbs += thumb_forget(path)
            gone.add(path)
            del index[path]
    # Every other copy in HOME (imports made outside this tool, Downloads, Trash, caches).
    found, paths, orphans, in_git = sweep(sig)
    for p in paths:
        thumbs += thumb_forget(p)
        gone.add(p)
        index.pop(p, None)
    drive = drive_purge(a.drive, shas) if a.drive else 0
    spare = [v["of"] for v in q.values() if isinstance(v.get("of"), str)]
    near, near_batch = near_dups(refs, kept, index, a.batch, spare)
    save(os.path.join(STATE, "index.json"), index)
    if near_batch and near:
        gone |= {v["dst"] for v in load(os.path.join(STATE, "quarantine", near_batch, "quarantine.json"), {}).values()}
    uris = {u for p in gone for u in file_uris(p)}
    prefixes = [file_uris(STATE)[0] + "/"]  # quarantine and staging, any batch
    recent = prune_recent(uris, prefixes)
    traces = prune_app_traces(uris, prefixes)
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
    print(f"erased {len(q)} files; library copies {lib}; phone {phone}; drive {drive}; thumbnail cache cleared ({thumbs} files)")
    print(f"home sweep: {sum(found.values())} more copies deleted"
          + "".join(f"\n  {k:28} {v}" for k, v in sorted(found.items())))
    print("  (not swept: toolchains, package/build caches, container storage, .git, node_modules)")
    if in_git:
        print(f"{len(in_git)} exact copies inside git repos, not deleted: " + ", ".join(in_git))
    if orphans:
        print(f"orphan sidecars deleted (.xmp left by a deleted file): {sum(orphans.values())}")
    if do_pthumbs:
        print(f"phone thumbnail folders removed ({pthumbs} files; Android rebuilds them)"
              + (f"; {prefused} the phone refused to delete (held open by Android's media service)" if prefused else ""))
    print(f"recent files: {recent} entries removed; " + (", ".join(f"{k} {v}" for k, v in sorted(traces.items())) or "no app traces"))
    if near:
        print(f"{near} near-duplicates (resized/recompressed copies) moved to quarantine/{near_batch}, not deleted.\n"
              f"  Open:   xdg-open {os.path.abspath(os.path.join(STATE, 'quarantine', near_batch))}\n"
              f"  Review: media-intake quarantine {near_batch}\n"
              f"  then:   media-intake erase {near_batch}")
    print("videos: only exact copies are found; re-encoded or trimmed copies are not matched")
    if a.drive and not a.no_wipe_free:
        try:
            mb, secs, note = wipe_free(a.drive)
            print(f"drive free space wiped: {mb} MB of zeros in {secs:.0f} s" + (f"\n  {note}" if note else ""))
        except SystemExit as e:
            print(f"drive free space NOT wiped: {e}")
    if not verify_gone(sig, a.drive):
        sys.exit(1)


def cmd_forget(a):
    """A sensitive file found later in the library: same erase path as intake."""
    batch = datetime.datetime.now().strftime("forget-%Y%m%d-%H%M%S")
    quar = private_dir(os.path.join(private_dir(os.path.join(STATE, "quarantine")), batch))
    index = load(os.path.join(STATE, "index.json"), {})
    q = {}
    for f in a.files:
        f = os.path.abspath(f)
        stem, ext = os.path.splitext(os.path.basename(f))
        rel, i = stem + ext, 1
        while rel in q:  # same name from two library folders
            rel, i = f"{stem}_{i}{ext}", i + 1
        s = sha256(f)
        q[rel] = dict(reasons=["owner"], sha=s, size=os.path.getsize(f), dst=None)
        shutil.move(f, os.path.join(quar, rel))
        os.chmod(os.path.join(quar, rel), stat.S_IRUSR | stat.S_IWUSR)
        drop_sidecar(f)  # its tags, place and labels go too
        thumb_forget(f)
        gallery_thumb_forget(s)
        index.pop(f, None)
    save(os.path.join(quar, "quarantine.json"), q)
    save(os.path.join(STATE, "index.json"), index)
    a.batch, a.phone = batch, None
    cmd_erase(a)


def cmd_offload(a):
    """Free phone space: delete phone originals whose copy is filed (or deliberately removed).

    Only after a drive backup. Checks each phone file is still the same size as
    the staged copy and sits inside the allowlist; never deletes a folder.
    """
    b = bdir(a.batch)
    man = load(os.path.join(b, "manifest.json"))
    plan = {p["rel"]: p for p in load(os.path.join(b, "plan.json"))}
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
    print(f"{len(todo)} of {len(man['files'])} phone files have a verified copy here")
    if not a.yes:
        if input("Delete them from the phone? Type DELETE: ").strip() != "DELETE":
            sys.exit("nothing deleted")
    done = device_remove(todo)
    print(f"deleted {len(done)} files from the phone; quarantined and review files stay until erase/purge")


def cmd_purge_review(a):
    rv = os.path.join(bdir(a.batch), "review")
    n = sum(len(fs) for _, _, fs in os.walk(rv))
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
    return (len(rel) > 2 and rel[0] == "quarantine") or (len(rel) > 3 and rel[0] == "batches" and rel[2] == "review")


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


def trash_files(files, batch=None, phone=None):
    """Delete each file, its sidecar, thumbnails, index entry and (with phone) its phone original."""
    private_dir(STATE)
    index = load(os.path.join(STATE, "index.json"), {})
    trashed = load(TRASHED, {})
    root = os.path.abspath(phone) if phone else None
    batches = [batch] if batch else sorted(os.path.basename(d) for d in glob.glob(os.path.join(STATE, "batches", "*")))
    mans = {b: load(os.path.join(bdir(b), "manifest.json"), {"files": {}}) or {"files": {}} for b in batches} \
        if root else {}
    refused, todo = [], []
    for f in files:  # every phone delete is checked before anything is trashed
        p = os.path.abspath(f)
        if any(p == x for x, _ in todo):
            continue
        if not trash_allowed(p):
            refused.append(f)
            continue
        srcs = [(m["src"], m.get("size")) for b, man in mans.items() for rel in phone_rels(b, p)
                for m in [man["files"].get(rel) or {}] if m.get("src")]
        todo.append((p, srcs))
    known = [m["src"] for man in mans.values() for m in man["files"].values() if m.get("src")]
    phone_ok = set(device_targets(root, [x for _, srcs in todo for x in srcs], known)) if root else set()
    n = sc = th = ph = 0
    for p, srcs in todo:
        s, size = sha256(p), os.path.getsize(p)
        trashed[s] = size
        save(TRASHED, trashed)  # first, so a crash mid-way still keeps it from coming back
        os.remove(p)
        n += 1
        sc += drop_sidecar(p)
        th += thumb_forget(p) + gallery_thumb_forget(s)
        index.pop(p, None)
        rel = os.path.relpath(os.path.realpath(p), os.path.realpath(STATE)).split(os.sep)
        if rel[0] == "quarantine" and len(rel) > 2:  # keep quarantine.json in step
            qj = os.path.join(STATE, "quarantine", rel[1], "quarantine.json")
            q = load(qj, {})
            if q.pop(os.path.join(*rel[2:]), None) is not None:
                save(qj, q)
        for src, _ in srcs:
            th += thumb_forget(src)
            if src in phone_ok:
                phone_ok.discard(src)
                ph += len(device_remove([src]))
    save(os.path.join(STATE, "index.json"), index)
    print(f"trashed {n} files; sidecars {sc}; thumbnails {th}; phone {ph}"
          + (f"; refused {len(refused)} (not a library, review or quarantine file)" if refused else ""))
    return not refused


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
            print("dry run: nothing deleted (--yes to trash them)")
            return
        sys.exit(0 if trash_files(hits, phone=a.phone) else 1)
    args = list(a.args)
    batch = a.batch
    if batch is None and len(args) >= 2 and BATCH_NAME.match(args[0]) and not os.path.exists(args[0]) \
            and (os.path.isdir(bdir(args[0])) or os.path.isdir(os.path.join(STATE, "quarantine", args[0]))):
        batch = args.pop(0)
    if not args:
        sys.exit("trash wants FILE... (or --from-digikam)")
    sys.exit(0 if trash_files(args, batch, a.phone) else 1)


def cmd_not_trash(a):
    """You kept a suggested file: Review|maybe-trash off, Owner|not-trash on (never suggested again)."""
    need_exiftool()
    files = [os.path.abspath(f) for f in a.files]
    bad = [f for f in files if not trash_allowed(f) or not os.path.exists(sidecar(f))]
    if bad:
        sys.exit(f"not changing: {len(bad)} files are not library files with a sidecar")
    blocks = [["-q", "-m", "-overwrite_original", "-XMP-lr:HierarchicalSubject-=Review|maybe-trash",
               "-XMP-dc:Subject-=maybe-trash", "-XMP-lr:HierarchicalSubject-=Owner|not-trash",
               "-XMP-lr:HierarchicalSubject+=Owner|not-trash", "-XMP-dc:Subject-=not-trash",
               "-XMP-dc:Subject+=not-trash", sidecar(f)] for f in files]
    exiftool_batch(blocks)
    print(f"kept {len(files)} files: Owner|not-trash")


def cmd_suggest_trash(a):
    """Library files the VLM called trash (Review|vlm-trash/*, written only at confidence >= 0.7, or
    AI|vlm/<model>/trash...), or SigLIP's Review|maybe-trash without a VLM keep. Never one you
    kept (Owner|not-trash, Owner|keep, digiKam Accepted), Used|* or a protected project's Project| tag."""
    need_exiftool()
    roots = [r for r in LIB_ROOTS if os.path.isdir(r)]
    protected = config.protected_tags(CFG)
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-r", "-ext", "xmp", "-HierarchicalSubject",
                        "-XMP-digiKam:PickLabel", *roots], capture_output=True, text=True) if roots else None
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
        if not (vlm_trash or ("Review|maybe-trash" in tags and not vlm_keep)):
            continue
        if owner_kept(tags, str(row.get("PickLabel", "")).strip()) or protected.intersection(tags) \
                or any(t.startswith("Used|") for t in tags):
            continue
        if not trash_allowed(media) or (only is not None and os.path.realpath(media) not in only):
            continue
        items.append(dict(path=media, labels=[t for t in tags if t.startswith(("Label|", "Review|", "AI|"))]))
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
# (Project|, Source|, Event|, Place|, Label|, Review|, Used|), XMP-dc:Subject
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
# namespace; your own tags (Owner|, plain keywords) win.
FACT_PREFIXES = ("Project|", "Source|", "Event|", "Place|")  # intake facts (tag, tag-files)
OWNER_KEEP = ("Owner|not-trash", "Owner|keep", "Review|not-trash")  # Review|not-trash: legacy spelling


def ai_owned(t):
    """SigLIP's namespace, replaced on every labelled re-tag. Review|child-in-shot is not in it:
    AI may add it, only you clear it."""
    return t.startswith("Label|") or t == "Review|maybe-trash" or t.startswith("Review|untagged-")


def read_sidecar_tags(paths):
    """{media path: ([HierarchicalSubject...], pick label, {owner leaves})} for paths with a sidecar.
    Owner leaves: digiKam's own TagsList ("a/b") leaves, which a dc:Subject removal must spare."""
    xs = [sidecar(p) for p in paths if os.path.isfile(sidecar(p))]
    if not xs:
        return {}
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-HierarchicalSubject", "-XMP-digiKam:PickLabel",
                        "-XMP-digiKam:TagsList", "-@", "-"], input="\n".join(xs) + "\n", capture_output=True, text=True)
    out = {}
    for row in json.loads(r.stdout or "[]"):
        out[row["SourceFile"][:-4]] = (as_list(row.get("HierarchicalSubject")), str(row.get("PickLabel", "")).strip(),
                                       {t.split("/")[-1] for t in as_list(row.get("TagsList"))})
    return out


def owner_kept(tags, pick):
    return any(t in tags for t in OWNER_KEEP) or pick == "3"  # digiKam Accepted


def merge_tags(old, new, pick="", relabelled=True, owner_leaves=()):
    """(hier to add, tags to remove, dc:Subject leaves to remove) for a re-tag.

    Facts replace only the prefixes this run has a value for; AI labels replace SigLIP's
    namespace (only when it ran); everything else (owner keywords, Owner|, Used|, the VLM's
    Review|vlm-*, Review|child-in-shot) is never removed."""
    if owner_kept(old, pick):
        new = [t for t in new if t != "Review|maybe-trash"]
    have = {t.split("|", 1)[0] + "|" for t in new if t.startswith(FACT_PREFIXES)}
    remove = [t for t in old if t not in new and ((relabelled and ai_owned(t)) or
                                                  any(t.startswith(p) for p in have))]
    keep_leaves = ({t.split("|")[-1] for t in old if t not in remove} | {t.split("|")[-1] for t in new}
                   | set(owner_leaves))
    leaves = sorted({t.split("|")[-1] for t in remove} - keep_leaves)
    return new, remove, leaves


def tag_files(items, labels=True):
    """items: [(path, project, source, event, datetime or None)]. Writes sidecars; prints counts only."""
    need_exiftool()
    lab = guard_label()
    bad = set(lab.refused([i[0] for i in items], STATE))
    if bad:
        print(f"skipped {len(bad)} files: quarantined, staged or flagged by scan (never tagged)")
    items = [i for i in items if i[0] not in bad and os.path.isfile(i[0])]
    if not items:
        print("nothing to tag")
        return
    paths = [i[0] for i in items]
    gps = read_gps(paths)
    pts = list(gps)
    places = dict(zip(pts, venv_json("geocode.py", [list(gps[p]) for p in pts]))) if pts else {}
    lbl = {}
    if labels:
        if not os.path.exists(VENV_PY):
            sys.exit(f"no venv: run bash {SETUP} first (or --no-labels)")
        lbl = venv_json("label.py", paths)
    entries, c, lc = [], collections.Counter(), collections.Counter()
    existing = read_sidecar_tags(paths)
    for path, project, source, event, dt in items:
        hier = [f"Project|{project}"]
        if source:
            hier.append(f"Source|{source}")
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
        old, pick, oleaves = existing.get(path, ([], "", set()))
        hier, remove, leaves = merge_tags(old, hier, pick, relabelled=path in lbl, owner_leaves=oleaves)
        for h in hier:
            if h.startswith("Review|"):
                lc[h] += 1
        if remove:
            c["stale tags replaced"] += len(remove)
        entries.append((path, dict(t, hier=hier, remove=remove, drop_leaves=leaves)))
    write_tags(entries)
    print(f"tagged {len(entries)} files (sidecars <file>.xmp); " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
    if lc:
        print("labels: " + ", ".join(f"{k} {v}" for k, v in lc.most_common()))
    if lc.get("Review|child-in-shot"):
        print(f"{lc['Review|child-in-shot']} files may show a child: check them before any use "
              "(media-intake find --tag 'Review|child-in-shot')")
    if lc.get("Review|maybe-trash"):
        print(f"{lc['Review|maybe-trash']} files look like junk (blurry, dark, packaging...): "
              "media-intake suggest-trash shows them for Keep or Trash")


def cmd_tag(a):
    b = bdir(a.batch)
    plan = load(os.path.join(b, "plan.json"))
    if plan is None:
        sys.exit("no plan.json: run plan and apply first")
    flags = load(os.path.join(b, "scan.json"), {}) or {}
    items = []
    for p in plan:
        if p["action"] != "file" or p["rel"] in flags or not os.path.isfile(p["dst"]):
            continue
        project = p.get("project") or infer(p["dst"])[0]
        cat = p["cat"]
        src = SOURCE.get(cat) or (cat.split(":", 1)[1].lower() if cat.startswith("messaging:") else None)
        items.append((p["dst"], project, src, p.get("event"),
                      dates.aware(datetime.datetime.fromisoformat(p["date"]), p.get("date_tz"))))
    print(f"{len(items)} filed files to tag" + (f"; {sum(1 for p in plan if p['rel'] in flags)} flagged by scan are never tagged" if flags else ""))
    tag_files(items, labels=not a.no_labels)
    # Plugins (docs/plugins.md) get the filed files, e.g. to register a project's clips.
    import plugins
    if not plugins.modules():
        return
    live = [i for i in items if os.path.isfile(i[0])]
    bad = set(guard_label().refused([i[0] for i in live], STATE))
    plugins.call("after_tag", [dict(path=os.path.abspath(i[0]), project=i[1], event=i[3],
                                    date=i[4].strftime("%Y-%m-%d") if i[4] else None)
                               for i in live if i[0] not in bad])


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
    files = [os.path.abspath(f) for f in a.files]
    keep = sidecar_dates(files)
    fresh = dates.resolve_all([f for f in files if os.path.isfile(f) and f not in keep])
    items = []
    for f in files:
        project, src, event = infer(f)
        items.append((f, project, src, a.event or event, fresh[f].aware() if f in fresh else None))
    tag_files(items, labels=not a.no_labels)


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
def cmd_share_copy(a):
    os.makedirs(a.out, exist_ok=True)
    for f in a.files:
        dst = os.path.join(a.out, os.path.basename(f))
        ext = os.path.splitext(f)[1].lower()
        if ext in VID:
            # Drop every data stream (GoPro GPMF GPS track) and all metadata.
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f, "-map", "0:v", "-map", "0:a?", "-c", "copy",
                            "-map_metadata", "-1", "-map_chapters", "-1", dst], check=True)
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
        left = has_gps(dst)
        print(f"{dst}  gps={'STILL PRESENT' if left else 'none'}")


cmd_social_copy = cmd_share_copy  # older name


def cmd_gallery(a):
    import gallery
    argv = (["--drive", a.drive] if a.drive else []) + ["--port", str(a.port)]
    sys.exit(gallery.main(argv))


def clock_offset_arg(v):
    try:
        dates.parse_clock_offset(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return v


def build_parser(prog=None):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("stage"); s.add_argument("src"); s.add_argument("--kind", choices=["phone", "gopro", "drive"], required=True); s.add_argument("--batch", required=True)
    s.add_argument("--only", metavar="LISTFILE", help="stage only these relative paths (one per line; drive_queue.py chunks)")
    s.add_argument("--origin", help="device root the files really came from, if SRC is an earlier copy (for erase --phone)")
    s = sp.add_parser("plan"); s.add_argument("batch")
    s.add_argument("--event", action="append", metavar="[PROJECT/]SLUG@YYYY-MM-DD",
                   help="an event on that day: Event|SLUG tags; with a PROJECT/ prefix, or a slug matching a "
                        "project's events patterns in config.toml, that day's camera files go to the project's folder")
    s.add_argument("--clock-offset", action="append", metavar="DEVICE=+-Nh", type=clock_offset_arg,
                   help="shift dates from a camera whose clock was wrong (DEVICE: its Make, Model, 'Make Model' or "
                        "all; e.g. 'HERO11 Black=+3h'); GPS dates are never shifted; sidecars only")
    s = sp.add_parser("scan"); s.add_argument("batch")
    s = sp.add_parser("apply"); s.add_argument("batch"); s.add_argument("--no-scan", action="store_true"); s.add_argument("--allow-missing-detectors", action="store_true")
    s = sp.add_parser("quarantine"); s.add_argument("batch")
    s = sp.add_parser("release"); s.add_argument("batch"); s.add_argument("files", nargs="+")
    s = sp.add_parser("erase"); s.add_argument("batch"); s.add_argument("--phone"); s.add_argument("--drive"); s.add_argument("--yes", action="store_true")
    s.add_argument("--keep-phone-thumbnails", action="store_true", help="don't remove DCIM/.thumbnails and Pictures/.thumbnails (removed by default with --phone)")
    s.add_argument("--no-wipe-free", action="store_true", help="skip overwriting the drive's free space")
    s = sp.add_parser("forget"); s.add_argument("files", nargs="+"); s.add_argument("--drive"); s.add_argument("--yes", action="store_true")
    s.add_argument("--no-wipe-free", action="store_true")
    s = sp.add_parser("verify-gone"); s.add_argument("batch", nargs="?", help="one batch (default: every erase-log entry)"); s.add_argument("--drive")
    s = sp.add_parser("wipe-free"); s.add_argument("mount")
    s = sp.add_parser("offload"); s.add_argument("batch"); s.add_argument("--phone", required=True); s.add_argument("--yes", action="store_true")
    s = sp.add_parser("purge-review"); s.add_argument("batch")
    s = sp.add_parser("trash", help="plain delete of unwanted, non-sensitive files (no sweep, no erase log)")
    s.add_argument("args", nargs="*", metavar="[BATCH] FILE"); s.add_argument("--batch", help="batch the files came from (for --phone)")
    s.add_argument("--phone"); s.add_argument("--from-digikam", action="store_true"); s.add_argument("--yes", action="store_true")
    s = sp.add_parser("not-trash"); s.add_argument("files", nargs="+")
    s = sp.add_parser("suggest-trash"); s.add_argument("--batch"); s.add_argument("--phone")
    s.add_argument("--no-serve", action="store_true", help="write maybe-trash.json and stop")
    for name in ("share-copy", "social-copy"):
        s = sp.add_parser(name, help="copy without GPS or metadata" + (" (alias of share-copy)" if name != "share-copy" else ""))
        s.add_argument("files", nargs="+"); s.add_argument("--out", required=True)
    s = sp.add_parser("tag", help="after apply: sidecar tags for the batch's filed files (CPU-heavy: labels)"); s.add_argument("batch")
    s.add_argument("--no-labels", action="store_true", help="project/source/event/place/date only, no SigLIP labels")
    s = sp.add_parser("tag-files", help="(re)tag library files; project/source/event from the path"); s.add_argument("files", nargs="+")
    s.add_argument("--event", help="event slug for all of them"); s.add_argument("--no-labels", action="store_true")
    s = sp.add_parser("mark-used"); s.add_argument("files", nargs="+"); s.add_argument("--post", required=True, help="post slug, YYYY-MM-DD-...")
    s = sp.add_parser("find", help="search sidecars; prints matching media paths")
    s.add_argument("--tag", action="append", help="hierarchical tag, e.g. 'Label|runner' or 'Label' (any label); repeat = all of them")
    s.add_argument("--event"); s.add_argument("--place", help="substring of city, state or country"); s.add_argument("--project")
    s.add_argument("--unused-since", metavar="Nd", help="not used in a post in the last N days")
    s.add_argument("--never-used", action="store_true"); s.add_argument("--root", action="append", help="search here instead of the library folders")
    s = sp.add_parser("gallery", help="local web gallery of the library (gallery.py)")
    s.add_argument("--drive", help="backup drive mount, passed to forget by Erase"); s.add_argument("--port", type=int, default=0)
    return ap


# The library lock (library_lock.py; docs/intake.md, "One writer at a time"): every subcommand is in exactly one
# of these (tests/test_library_lock.py checks), and main() is the only place that takes it for the CLI.
LOCKED = {"stage", "plan", "scan", "apply", "release", "erase", "forget", "offload", "purge-review", "trash",
          "not-trash", "suggest-trash", "tag", "tag-files", "mark-used"}
READ_ONLY = {"quarantine", "verify-gone", "wipe-free", "share-copy", "social-copy", "find", "gallery"}


def main(argv=None, prog=None):
    a = build_parser(prog).parse_args(argv)
    run = globals()["cmd_" + a.cmd.replace("-", "_")]
    if a.cmd in READ_ONLY:  # gallery takes the lock itself, per write
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
