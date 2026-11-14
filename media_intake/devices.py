"""Device identity (EXIF/QuickTime Make, Model and serial number) and GoPro chapters.

A device id names batches (`stage` without --batch) and a dedicated camera's folders
(`<YYYY-MM>/<device id>/`), so two GoPros of one model, whose file numbers repeat, never
collide. The id is the slug of "Make Model" (`gopro-hero11-black`), plus, when the file
records a serial number, a short pseudonym of it (`gopro-hero11-black-3fa9c2`). Serials are read
only through exiftool: the Pillow / ffprobe fallbacks (dates.read_meta) read none, because ffprobe
can't see a video's serial and one camera's photos and videos must get one id.

Serial numbers are personal identifiers (they tie a file to one owner's camera, and some
vendors register them to a name), so a raw serial is never written to a folder name, a
tag, plan.json or a log. The pseudonym is the first SHORT hex digits of
HMAC-SHA256(salt, "make\\0model\\0serial"); the salt is 32 random bytes made on first use
in the state folder (`device-salt`, 0600, inside the 0700 state dir), never in the repo
or the library. Without the salt the pseudonym can't be checked against a guessed serial
(an unsalted hash of a short serial could be brute-forced). Losing the salt only gives
the same camera a new id from then on; a corrupt salt file stops the run instead.

GoPro chapters: a long recording is split into files of one recording number. HERO6 and
later name them GHccnnnn.MP4 (H.264) or GXccnnnn.MP4 (HEVC), chapter cc from 01; HERO5 and
earlier GOPRnnnn.MP4 for the first chapter, then GP01nnnn.MP4, GP02nnnn.MP4, ... Names are
matched exactly (upper case, .MP4, as the camera writes them); anything else is not a
chapter.
"""
import collections, hashlib, hmac, os, re, secrets
from dataclasses import dataclass

SERIAL_TAGS = ("SerialNumber", "CameraSerialNumber", "InternalSerialNumber", "BodySerialNumber")
SALT_FILE = "device-salt"
SALT_RX = re.compile(r"[0-9a-f]{64}")
SHORT = 6  # 24 bits: two of one owner's cameras colliding is a one-in-millions chance
SLUG_MAX = 40
NOT_A_SERIAL = {"unknown", "none", "n/a", "na", "null", "serial", "serialnumber"}

_CHAPTER = (re.compile(r"(G[HX])(\d\d)(\d{4})\.MP4"),  # HERO6+: chapter 01, 02, ...
            re.compile(r"(GOPR)(\d{4})\.MP4"),           # HERO5 and earlier: the first chapter
            re.compile(r"(GP)(\d\d)(\d{4})\.MP4"))       # ... and the later ones, 01 = the second
_GOPRO_PHOTO = re.compile(r"(GOPR\d{4}|G\d{7})\.JPG")  # single shots; burst/time-lapse G<group><n>


@dataclass(frozen=True)
class Device:
    id: str      # folder- and batch-safe slug, with the serial's pseudonym when one is recorded
    model: str   # for the Source|<model> tag: the Model, else the Make
    label: str   # "Make Model" for messages (no serial)


def clean(s):
    """A tag-safe string: whitespace collapsed, no '|' (the tag hierarchy separator), no control chars."""
    s = "".join(c for c in str(s or "") if c.isprintable() and c != "|")
    return " ".join(s.split())


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", clean(s).casefold()).strip("-")[:SLUG_MAX].strip("-")


def serial_of(meta):
    """The first real serial number in a metadata row, else None (blank, all zeros, 'Unknown')."""
    for k in SERIAL_TAGS:
        s = clean(meta.get(k))
        if s and re.search(r"[1-9A-Za-z]", s) and s.casefold() not in NOT_A_SERIAL:
            return s
    return None


def salt(state):
    """The hex salt in <state>/device-salt, made on first use. Raises ValueError if it is corrupt."""
    p = os.path.join(state, SALT_FILE)
    try:
        with open(p) as f:
            v = f.read().strip()
    except FileNotFoundError:
        os.makedirs(state, mode=0o700, exist_ok=True)
        new = secrets.token_hex(32) + "\n"
        tmp = f"{p}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new)
        try:
            os.link(tmp, p)  # atomic, and never replaces a salt another run just made
        except FileExistsError:
            pass  # another run made the salt first; use theirs (read below)
        except OSError:  # no hard links on this filesystem (vfat, some FUSE and network mounts)
            _create_once(p, new)
        finally:
            os.remove(tmp)
        return salt(state)
    if not SALT_RX.fullmatch(v):
        raise ValueError(f"{p} is not a device salt (64 hex digits). Restore it from a backup, or delete it "
                         "to start over (cameras then get new folder names)")
    return v


def _create_once(p, text):
    """Create p with text, 0600, only if it does not exist yet (O_EXCL: never replaces another run's salt).
    One small write, fsynced, so a reader sees the whole salt."""
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # another run made the salt first; use theirs
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def identify(meta, key=None):
    """The Device of a metadata row (Make, Model, serial), or None without a Make or Model.
    key: the salt; needed only when the row records a serial."""
    make, model = clean(meta.get("Make")), clean(meta.get("Model"))
    if not (make or model):
        return None
    name = model if model.casefold().startswith(make.casefold()) else f"{make} {model}".strip()
    base = slug(name)
    if not base:
        return None
    ser = serial_of(meta)
    if ser:
        if not key:
            raise ValueError("a serial number needs the device salt to be turned into an id")
        msg = f"{make.casefold()}\0{model.casefold()}\0{ser}".encode()
        base += "-" + hmac.new(bytes.fromhex(key), msg, hashlib.sha256).hexdigest()[:SHORT]
    return Device(base, model or make, name)


def strip_serials(meta):
    """Drop every serial tag from a metadata row (after identify), so nothing later can print one."""
    for k in SERIAL_TAGS:
        meta.pop(k, None)
    return meta


# ---------------------------------------------------------------- GoPro names
def chapter(name):
    """(family, recording number, chapter index from 0) for a GoPro chapter file name, else None.
    family is GH, GX or GP (GOPRnnnn and GPccnnnn are one family)."""
    m = _CHAPTER[0].fullmatch(name)
    if m:
        return (m.group(1), m.group(3), int(m.group(2)) - 1) if m.group(2) != "00" else None
    m = _CHAPTER[1].fullmatch(name)
    if m:
        return "GP", m.group(2), 0
    m = _CHAPTER[2].fullmatch(name)
    if m and m.group(2) != "00":
        return "GP", m.group(3), int(m.group(2))
    return None


def gopro_name(name):
    """True for a name a GoPro writes (a chapter, a single shot or a burst frame), matched exactly."""
    return chapter(name) is not None or bool(_GOPRO_PHOTO.fullmatch(name))


def chapter_groups(paths, device_of=lambda p: None):
    """{path: first chapter's path} for every later chapter of a recording with more than one
    chapter in paths. One recording: same folder, same device, same family and number."""
    groups = collections.defaultdict(list)
    for p in paths:
        c = chapter(os.path.basename(p))
        if c:
            groups[(os.path.dirname(p), device_of(p), c[0], c[1])].append((c[2], p))
    out = {}
    for g in groups.values():
        if len(g) > 1:
            g.sort()
            out.update({p: g[0][1] for _, p in g[1:]})
    return out


# ---------------------------------------------------------------- batch names
def batch_name(devices, kind, day):
    """'<device id>-<YYYYMMDD>' for the most common device among devices (Device or None), else
    '<kind>-<YYYYMMDD>'. Ties go to the alphabetically first id, so a re-run names it the same."""
    c = collections.Counter(d.id for d in devices if d)
    best = min(c, key=lambda k: (-c[k], k)) if c else kind
    return f"{best}-{day:%Y%m%d}"
