"""Capture dates: which clock to trust for each file, and where its date came from.

Every file gets one date (naive local wall time, which picks its library folder) and a
source, tried in this order:

  exif      the camera's DateTimeOriginal (else EXIF CreateDate, else IFD0 ModifyDate)
  video     Apple's Keys:CreationDate (local time with its offset), else QuickTime
            CreateDate / ffprobe creation_time, which is UTC and is converted to local time
  gps       GPSDateTime (EXIF, or GoPro's GPMF track with exiftool -ee), else GPSDateStamp
  filename  a date in the file name (20261108_071502, 2026-11-08 07.15.02, 20261108)
  folder    an iPhone export's DCIM/YYYYMM__ folder: the first of that month
  mtime     the file's modification time, the last resort

A camera date (exif, video) is used unless it is impossible or GPS overrules it; then the
GPS date wins, and only without one do the weaker sources get a turn.

  - impossible: before 2000-01-02 (QuickTime 1904, Unix 1970 and DOS 1980 epochs, and
    the 2000-01-01 many cameras reset to) or more than a day after now. The clock was
    never set, so GPS replaces it without a flag.
  - contradicted: a GPS date exists and the two differ by more than a day once time
    zones are allowed for (24 h when the camera's UTC offset is known, else 24 h + 14 h,
    the widest offset on Earth). A GPS stamp is the time of the receiver's last fix,
    which on a phone can be days old, so a disagreement alone doesn't prove the camera
    wrong. GPS replaces the camera date only when:
      - the camera date is a clock-reset default (below) with no --clock-offset: the
        clock was never set, so no flag; or
      - nothing backs the camera: the file name carries no agreeing date (within 14 h,
        so a UTC file name agrees) and the camera recorded no UTC offset (a camera that
        records one has a network-synced clock). The file is flagged "suspect".
    Otherwise the camera date is kept with a note; flagged "suspect" when only its
    recorded offset backed it.

A camera date on a known clock-reset default (2015-12-31 or 2016-01-01, what a GoPro with
an unset clock writes) is kept when no GPS date can check it, but flagged "suspect" so
`plan` asks you to look (or pass --clock-offset).

A video's GPMF track has many GPS samples; the ones before the receiver locks can carry a
stale time. The date is the first sample of the unbroken run (no gap over GPS_GAP) that
holds the median sample: the median sits inside the locked run, which covers almost all
of a clip, and walking back to the run's start gives the recording's start, not a stale
pre-lock sample (which the earliest sample would be).

`--clock-offset DEVICE=+-Nh` shifts every date that came from a device's own clock
(everything but GPS) for the files whose Make, Model, "Make Model" or device id
(devices.py, e.g. gopro-hero11-black-3fa9c2) is DEVICE (or every file, for DEVICE `all`);
the source becomes "offset" and `base` keeps the clock it shifted. Corrected dates are
written to the XMP sidecar only; originals are never written.

GoPro chapters (GX01nnnn, GX02nnnn, ...; devices.chapter) take the date of the first
chapter of their recording in the batch, so a recording that crosses midnight or an hour
is filed together; `chapter_of` names that first chapter.
"""
import dataclasses, datetime, json, os, re, shutil, statistics, subprocess
from dataclasses import dataclass

import config
import devices

# Mirrors intake.VID (tests/test_dates.py checks they agree).
VIDEO_EXT = {".mp4", ".mov", ".3gp", ".mkv", ".webm"}
SOURCES = ("exif", "video", "gps", "filename", "folder", "mtime", "offset")

FLOOR = datetime.datetime(2000, 1, 2)
FUTURE_SLACK = datetime.timedelta(days=1)
GPS_AGREE = datetime.timedelta(hours=24)
MAX_TZ = datetime.timedelta(hours=14)
GPS_GAP = datetime.timedelta(minutes=10)  # wider than any sampling gap in one clip (timelapse included)
DAY = datetime.timedelta(days=1)
# Dates cameras fall back to when their clock was never set. Without GPS they are kept, flagged.
RESET_DAYS = {datetime.date(2015, 12, 31), datetime.date(2016, 1, 1)}  # GoPro (2016-01-01 00:00 UTC)
MAX_OFFSET_H = 24 * 366 * 30  # a clock can be years out; more than 30 is a typo
UTC = datetime.timezone.utc

TAGS = ("DateTimeOriginal", "CreateDate", "ModifyDate", "OffsetTimeOriginal", "OffsetTime",
        "GPSDateTime", "GPSDateStamp", "CreationDate", "Make", "Model", *devices.SERIAL_TAGS)
DT_RX = re.compile(r"(\d{4})[:-](\d\d)[:-](\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")
DAY_RX = re.compile(r"(\d{4})[:-](\d\d)[:-](\d\d)$")
FOLDER_RX = re.compile(r"(?:^|/)DCIM/(\d{4})(\d\d)__/")
NAME_RX = (r"(20\d\d)(\d\d)(\d\d)[_-](\d\d)(\d\d)(\d\d)", r"(20\d\d)-(\d\d)-(\d\d)[ _-](\d\d)[.:-]?(\d\d)[.:-]?(\d\d)",
           r"(20\d\d)(\d\d)(\d\d)")
DEVICE_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
OFFSET_RX = re.compile(r"([+-])(\d{1,6}(?:\.\d{1,2})?)h")


@dataclass
class Dated:
    dt: datetime.datetime      # naive local wall time
    src: str                   # one of SOURCES
    tz: str = None             # "+HH:MM" when the UTC offset is known
    base: str = None           # with src "offset": the source that was shifted
    offset: str = None         # the --clock-offset DEVICE matching the file (a GPS date is never shifted)
    note: str = None           # why a camera date was not used, or what to check
    suspect: bool = False      # needs a look: clock-reset default, a GPS conflict or nothing plausible
    device: str = None         # devices.Device.id (never a raw serial)
    model: str = None          # devices.Device.model, for the Source|<model> tag
    chapter_of: str = None     # a later GoPro chapter: the first chapter's file name, whose date it took

    def aware(self):
        return aware(self.dt, self.tz)


# ---------------------------------------------------------------- parsing
def tz_of(s):
    """A datetime.timezone from "+05:30", "-0500" or "Z"; None for anything else."""
    if not s:
        return None
    s = str(s).strip()
    if s == "Z":
        return UTC
    m = re.fullmatch(r"([+-])(\d\d):?(\d\d)", s)
    if not m or int(m.group(2)) > 14 or int(m.group(3)) >= 60:
        return None
    d = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return datetime.timezone(-d if m.group(1) == "-" else d)


def tz_str(tz):
    if tz is None:
        return None
    m = int(tz.utcoffset(None).total_seconds() // 60)
    return f"{'-' if m < 0 else '+'}{abs(m) // 60:02d}:{abs(m) % 60:02d}"


def parse(s):
    """(naive datetime, timezone or None) from an EXIF, QuickTime or ISO string; None if unset."""
    m = DT_RX.match(str(s or "").strip())
    if not m:
        return None
    try:
        d = datetime.datetime(*map(int, m.groups()[:6]))
    except ValueError:  # 0000:00:00 00:00:00, an unset QuickTime date
        return None
    return d, tz_of(m.group(7))


def aware(dt, tz):
    """dt with its offset attached when tz ("+HH:MM") is known, else dt unchanged."""
    z = tz_of(tz)
    return dt.replace(tzinfo=z) if z and dt.tzinfo is None else dt


def exif_string(dt):
    """'YYYY:MM:DD HH:MM:SS', plus '+HH:MM' for an aware datetime (XMP-exif:DateTimeOriginal)."""
    z = tz_str(dt.tzinfo) if dt.tzinfo else None
    return dt.strftime("%Y:%m:%d %H:%M:%S") + (z or "")


# ---------------------------------------------------------------- --clock-offset
def parse_clock_offset(s):
    """'DEVICE=+-Nh' -> (device key, timedelta). Raises ValueError with the reason."""
    dev, sep, off = str(s).rpartition("=")
    dev = " ".join(dev.split())
    if not sep or not DEVICE_RX.fullmatch(dev):
        raise ValueError(f"--clock-offset wants DEVICE=+Nh or DEVICE=-Nh (DEVICE: the camera's Make, Model, "
                         f"'Make Model' or all), got {s!r}")
    m = OFFSET_RX.fullmatch(off.strip())
    if not m:
        raise ValueError(f"--clock-offset {s!r}: the offset must be a signed number of hours, e.g. +3h, -1.5h")
    h = float(m.group(2))
    if h == 0 or h > MAX_OFFSET_H:
        raise ValueError(f"--clock-offset {s!r}: hours must be non-zero and at most {MAX_OFFSET_H}")
    return dev.casefold(), datetime.timedelta(hours=-h if m.group(1) == "-" else h)


def clock_offsets(specs):
    """{device key: timedelta} from repeated --clock-offset values. Raises ValueError."""
    out = {}
    for s in specs or []:
        k, d = parse_clock_offset(s)
        if k in out:
            raise ValueError(f"--clock-offset {k!r} given twice")
        out[k] = d
    if "all" in out and len(out) > 1:
        raise ValueError("--clock-offset all=... can't be combined with a per-device offset")
    return out


def device_names(meta):
    """Every DEVICE a --clock-offset may name this file by: Make, Model, "Make Model", device id."""
    make = " ".join(str(meta.get("Make") or "").split())
    model = " ".join(str(meta.get("Model") or "").split())
    names = {make, model, f"{make} {model}".strip(), meta.get("Device") or ""} - {""}
    return {n.casefold() for n in names}


def device_label(meta):
    label = " ".join(f"{meta.get('Make') or ''} {meta.get('Model') or ''}".split()) or "(no make/model)"
    dev = meta.get("Device")
    return f"{label} ({dev})" if dev and dev != devices.slug(label) else label


def offset_for(meta, offsets):
    """(device key, timedelta) of the --clock-offset matching this file, or (None, None)."""
    if not offsets:
        return None, None
    names = device_names(meta)
    hits = [k for k in offsets if k == "all" or k in names]
    if len(hits) > 1:
        raise ValueError(f"--clock-offset {', '.join(sorted(hits))} all match {device_label(meta)}; keep one")
    return (hits[0], offsets[hits[0]]) if hits else (None, None)


# ---------------------------------------------------------------- reading metadata
def _exiftool(paths, extra):
    r = subprocess.run(["exiftool", "-j", "-q", "-q", *extra, "-@", "-"], input="\n".join(paths) + "\n",
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "[]")
    except ValueError:
        return []


def _pil(path):
    from PIL import Image
    try:
        with Image.open(path) as im:
            ex = im.getexif()
    except Exception:
        return {}
    e, g = ex.get_ifd(0x8769), ex.get_ifd(0x8825)
    m = {k: v for k, v in (("DateTimeOriginal", e.get(36867)), ("CreateDate", e.get(36868)),
                           ("ModifyDate", ex.get(306)), ("OffsetTimeOriginal", e.get(36881)),
                           ("OffsetTime", e.get(36880)), ("Make", ex.get(271)), ("Model", ex.get(272)),
                           ("SerialNumber", e.get(0xA431))) if v}
    day, hms = g.get(29), g.get(7)
    if day:
        m["GPSDateStamp"] = str(day)
        try:
            h, mi, s = (float(x) for x in hms)
            m["GPSDateTime"] = f"{day} {int(h):02d}:{int(mi):02d}:{int(s):02d}Z"
        except (TypeError, ValueError):
            pass  # a malformed GPS time: keep the date stamp alone
    return m


def _ffprobe(path):
    if not shutil.which("ffprobe"):
        return {}
    r = subprocess.run(["ffprobe", "-v", "quiet", "-of", "json", "-show_entries", "format_tags", path],
                       capture_output=True, text=True)
    try:
        t = json.loads(r.stdout or "{}").get("format", {}).get("tags", {})
    except ValueError:
        return {}
    m = {"CreateDate": t.get("creation_time"), "CreationDate": t.get("com.apple.quicktime.creationdate"),
         "Make": t.get("com.apple.quicktime.make") or t.get("make"),
         "Model": t.get("com.apple.quicktime.model") or t.get("model")}
    return {k: v for k, v in m.items() if v}


def read_meta(paths, use_exiftool=None, state=None, gpmf=True):
    """{path: {tag: value}} for the TAGS above plus "mtime", and Device / DeviceModel / DeviceLabel
    (devices.identify) in place of any serial number, which is dropped from the row. exiftool when
    installed (one run, plus, with gpmf, an -ee run over videos for GoPro's GPMF GPS time), else
    Pillow / ffprobe. state: the folder holding the device salt (default: the configured state)."""
    paths = [os.path.abspath(p) for p in paths]
    use = shutil.which("exiftool") if use_exiftool is None else use_exiftool
    out = {p: {} for p in paths}
    if use and paths:
        for row in _exiftool(paths, [f"-{t}" for t in TAGS]):
            if row.get("SourceFile") in out:
                out[row["SourceFile"]] = {k: v for k, v in row.items() if k in TAGS}
        vids = [p for p in paths if gpmf and os.path.splitext(p)[1].lower() in VIDEO_EXT
                and "GPSDateTime" not in out[p]]
        for row in _exiftool(vids, ["-ee", "-G3", "-api", "LargeFileSupport=1", "-GPSDateTime"]) if vids else []:
            ts = [v for k, v in row.items() if k.split(":")[-1] == "GPSDateTime"]
            if ts and row.get("SourceFile") in out:
                out[row["SourceFile"]]["GPSDateTimes"] = ts
    else:
        for p in paths:
            out[p] = _ffprobe(p) if os.path.splitext(p)[1].lower() in VIDEO_EXT else _pil(p)
    key = None
    if any(devices.serial_of(m) for m in out.values()):
        key = devices.salt(state or config.get()["state"])
    for p in paths:
        dev = devices.identify(out[p], key)
        devices.strip_serials(out[p])
        if dev:
            out[p].update(Device=dev.id, DeviceModel=dev.model, DeviceLabel=dev.label)
        try:
            out[p]["mtime"] = os.path.getmtime(p)
        except OSError:
            pass  # the file vanished while being read: no mtime candidate
    return out


# ---------------------------------------------------------------- choosing
@dataclass
class _Cand:
    src: str
    local: datetime.datetime
    utc: datetime.datetime = None  # aware, when the instant is known
    tz: object = None              # timezone the wall time is in, when known
    recorded: bool = False         # camera: the file itself recorded the UTC offset (a synced clock)
    day_only: bool = False         # filename: a date without a time (midnight stands in)


def _local(utc, tz, local_tz):
    z = tz or local_tz
    d = utc.astimezone(z) if z else utc.astimezone()
    return d.replace(tzinfo=None), datetime.timezone(d.utcoffset())  # fixed offset of that instant


def impossible(dt, now):
    return dt < FLOOR or dt > now + FUTURE_SLACK


def _camera(path, meta, tz, local_tz):
    if os.path.splitext(path)[1].lower() not in VIDEO_EXT:
        for k in ("DateTimeOriginal", "CreateDate", "ModifyDate"):
            p = parse(meta.get(k))
            if p:
                z = p[1] or tz
                return _Cand("exif", p[0], p[0].replace(tzinfo=z).astimezone(UTC) if z else None, z, z is not None)
        return None
    p = parse(meta.get("CreationDate"))  # Apple: local wall time with its own offset
    if p and p[1]:
        return _Cand("video", p[0], p[0].replace(tzinfo=p[1]).astimezone(UTC), p[1], True)
    p = parse(meta.get("CreateDate"))  # QuickTime: UTC by the spec
    if p:
        utc = p[0].replace(tzinfo=UTC)
        loc, z = _local(utc, tz, local_tz)
        return _Cand("video", loc, utc, z, tz is not None)  # UTC by the spec; only OffsetTime* is recorded
    return None


def _gps(meta, tz, now, local_tz):
    """(candidate, slack): the GPS time (see the module docstring for a video's many samples);
    slack 12 h for a date-only stamp."""
    utcs = []
    for s in [meta.get("GPSDateTime"), *meta.get("GPSDateTimes", [])]:
        p = parse(s)
        if p:
            u = p[0].replace(tzinfo=UTC)  # GPS time is UTC whatever the suffix
            if not impossible(p[0], now):
                utcs.append(u)
    if utcs:
        u = _locked_start(utcs)
        loc, z = _local(u, tz, local_tz)
        return _Cand("gps", loc, u, z), datetime.timedelta(0)
    m = DAY_RX.match(str(meta.get("GPSDateStamp") or "").strip())
    if m:
        try:
            d = datetime.datetime(*map(int, m.groups()), 12)  # noon UTC is the same date almost everywhere
        except ValueError:
            return None, None
        if not impossible(d, now):
            return _Cand("gps", d, d.replace(tzinfo=UTC)), datetime.timedelta(hours=12)
    return None, None


def _locked_start(utcs):
    """The first sample of the unbroken run (gaps <= GPS_GAP) holding the median sample."""
    utcs = sorted(utcs)
    i = utcs.index(statistics.median_low(utcs))
    while i and utcs[i] - utcs[i - 1] <= GPS_GAP:
        i -= 1
    return utcs[i]


def _weak(path, meta, local_tz):
    out = []
    n = os.path.basename(path)
    for rx in NAME_RX:
        m = re.search(rx, n)
        if m:
            g = [int(x) for x in m.groups()] + [0, 0, 0]
            try:
                out.append(_Cand("filename", datetime.datetime(*g[:6]), day_only=len(m.groups()) == 3))
                break
            except ValueError:
                pass  # digits that aren't a real date: try the next pattern
    m = FOLDER_RX.search(path.replace(os.sep, "/"))
    if m and 1 <= int(m.group(2)) <= 12:
        out.append(_Cand("folder", datetime.datetime(int(m.group(1)), int(m.group(2)), 1)))
    if "mtime" in meta:
        d = datetime.datetime.fromtimestamp(meta["mtime"], local_tz) if local_tz else \
            datetime.datetime.fromtimestamp(meta["mtime"])
        out.append(_Cand("mtime", d.replace(tzinfo=None)))
    return out


def _contradicts(c, gps, slack):
    if c.utc is not None:
        return abs(c.utc - gps.utc) > GPS_AGREE + slack
    return abs(c.local - gps.utc.replace(tzinfo=None)) > GPS_AGREE + MAX_TZ + slack


def _backs(cam, weak):
    """True when the file name's date agrees with the camera date: within MAX_TZ (a name may be
    in UTC), plus the whole day for a date-only name."""
    for w in weak:
        if w.src == "filename":
            lo, hi = w.local - MAX_TZ, w.local + (DAY if w.day_only else datetime.timedelta(0)) + MAX_TZ
            return lo <= cam.local <= hi
    return False


def resolve(path, meta, offsets=None, now=None, local_tz=None):
    """The Dated for one file from its read_meta() row. Raises ValueError on an ambiguous offset."""
    now = now or datetime.datetime.now()
    tz = tz_of(meta.get("OffsetTimeOriginal") or meta.get("OffsetTime"))
    key, shift = offset_for(meta, offsets)
    gps, slack = _gps(meta, tz, now, local_tz)
    cam = _camera(path, meta, tz, local_tz)
    weak = _weak(path, meta, local_tz)
    for c in [cam, *weak]:
        if c and shift:
            c.local += shift
            c.utc = c.utc + shift if c.utc else None

    def done(c, note=None, suspect=False):
        shifted = bool(shift) and c.src != "gps"
        return Dated(c.local, "offset" if shifted else c.src, tz_str(c.tz), c.src if shifted else None,
                     key, note, suspect, meta.get("Device"), meta.get("DeviceModel"))

    def why(c):
        if impossible(c.local, now):
            return f"{c.src} date {c.local:%Y-%m-%d %H:%M:%S} is impossible"
        if gps and _contradicts(c, gps, slack):
            return f"{c.src} date {c.local:%Y-%m-%d %H:%M:%S} disagrees with GPS by more than a day"
        return None

    rejected, suspect = None, False
    if cam:
        reset = not shift and cam.local.date() in RESET_DAYS
        rejected = why(cam)
        if not rejected:
            return done(cam, "clock-reset default date and no GPS to check it" if reset and not gps else None,
                        reset and not gps)
        if gps and not impossible(cam.local, now) and not reset:
            gps_at = f"GPS date {gps.local:%Y-%m-%d %H:%M:%S} disagrees by more than a day (a stale fix?)"
            if _backs(cam, weak):
                return done(cam, f"{gps_at}; kept the {cam.src} date, the file name agrees")
            if cam.recorded:
                return done(cam, f"{gps_at}; kept the {cam.src} date, its clock recorded a UTC offset", True)
            suspect = True  # GPS replaces a working-looking clock on its word alone
    if gps:
        return done(gps, rejected, suspect)
    for c in weak:
        if not why(c):
            return done(c, rejected)
    last = weak[-1] if weak else _Cand("mtime", now)
    return done(last, "no plausible date: " + (rejected or why(last) or "nothing to read"), True)


def resolve_all(paths, offsets=None, now=None, local_tz=None, use_exiftool=None, metas=None, state=None):
    """{path (as given): Dated}. Raises ValueError for an ambiguous --clock-offset or one that
    matched no file (so a typo never silently leaves a wrong clock uncorrected). metas: read_meta()
    rows already read (keyed by absolute path), else they are read here. A later GoPro chapter
    takes its first chapter's Dated (chapter_of set, the note says so)."""
    if metas is None:
        metas = read_meta(paths, use_exiftool, state)
    out = {p: resolve(os.path.abspath(p), metas[os.path.abspath(p)], offsets, now, local_tz) for p in paths}
    firsts = devices.chapter_groups(paths, lambda p: metas[os.path.abspath(p)].get("Device"))
    for p, first in firsts.items():
        f = out[first]
        name = os.path.basename(first)
        out[p] = dataclasses.replace(f, chapter_of=name, note=f"dated by its first chapter {name}"
                                     + (f"; {f.note}" if f.note else ""))
    unused = set(offsets or {}) - {d.offset for d in out.values()}
    if unused:
        seen = sorted({device_label(m) for m in metas.values()})
        raise ValueError(f"--clock-offset {', '.join(sorted(unused))}: no file in this batch has that device "
                         f"(Make, Model or 'Make Model'; seen: {', '.join(seen) or 'none'})")
    return out
