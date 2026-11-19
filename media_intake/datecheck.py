#!/usr/bin/env python3
"""Wrong-metadata checks for library files, dates first: `media-intake check-dates` and `fix-dates`.

check-dates reads every library file (photos and videos under the library, videos and project folders;
never the state folder, and never a file scan flagged, quarantined or erased: label.refused) and lists the
ones whose date looks wrong, each with a confidence, a reason in plain words and, when it can tell, a
suggested date. It only reads: the library, its sidecars and the batches' plans; it writes its report to
<state>/datecheck.json (0600) for the gallery's "Dates to check" view. docs/dates.md.

Each file's date is the one the library uses: its sidecar's XMP-exif:DateTimeOriginal (written by tag, a
GPS or --clock-offset correction, or fix-dates), else what dates.resolve() picks from the file itself.

The checks (KINDS), each rule in docs/dates.md:
  reset      the date is a clock-reset default (RESETS: a GoPro's 2015-12-31 / 2016-01-01, the 1904, 1970,
             1980 and 2000 epochs, and 2004 / 2005 midnights some older cameras start from)
  era        the camera or phone model was first sold after the date (ERAS; unknown models skip this)
  codec      the video format did not exist yet (CODECS: HEVC, VP9, AV1; a HEIC photo is HEVC too)
  sequence   one camera's file numbers (GX010042, IMG_1234, DSC01234, ...) rise with time; a file whose date
             breaks the order of its numbered neighbours gets their bracketing dates as the suggestion
  sources    the date disagrees with GPS time, a date in the file name, a cloud export's date, the camera's
             own date (when the sidecar holds another), an iPhone export's DCIM/YYYYMM__ folder, or it is
             after the file's own modification time
  duplicate  the same content (sha256 in index.json) in the library under another date
  null-island  GPS exactly 0, 0 (a receiver with no fix)
  place      the sidecar's GPS (where its Place| tag came from) is far from the file's own GPS

fix-dates never runs by itself: you name the files, it shows each old and new date (a dry run), and writes
only with --yes. A corrected date goes to the sidecar only (originals are never written, so their sha256
stays), with the old value and the reason recorded in <state>/date-fixes.json, so `fix-dates --undo` (and
the gallery's Undo) puts exactly that back, refusing once a sidecar changed again. Files are not moved:
nothing in media-intake moves a filed file when its date changes, so a fixed file stays in its old
YYYY/YYYY-MM folder (docs/dates.md, Limits).
"""
import bisect, collections, contextlib, datetime, hashlib, json, math, os, re, secrets, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import dates  # noqa: E402
import intake  # noqa: E402
import label  # noqa: E402

REPORT = "datecheck.json"   # in the state folder: the last check-dates run
FIXES = "date-fixes.json"   # in the state folder: every fix-dates write, for --undo
VERSION = 1
KINDS = ("reset", "era", "codec", "sequence", "sources", "duplicate", "null-island", "place")
DATE_KINDS = KINDS[:6]      # the ones a date fix answers (the gallery's Dates to check view)
TITLES = {"reset": "Clock-reset default date", "era": "Camera newer than its date",
          "codec": "Video format newer than its date", "sequence": "Out of order with the camera's numbering",
          "sources": "Dates disagree", "duplicate": "Copies with other dates", "null-island": "GPS at 0, 0",
          "place": "Place doesn't match GPS"}
CONF = ("high", "medium", "low")

DAY = datetime.timedelta(days=1)
AGREE = dates.GPS_AGREE + dates.MAX_TZ   # 38 h: two clocks agree within a day plus the widest UTC offset
FAR = datetime.timedelta(days=7)         # a disagreement this large is no time-zone or DST slip
SEQ_SLACK = datetime.timedelta(days=2)   # a numbered neighbour may differ by a travel day
ERA_SLACK = datetime.timedelta(days=92)  # pre-release units and review samples: three months before sale
DUP_SLACK = datetime.timedelta(days=2)
NULL_EPS = 1e-4                          # degrees, about 11 m: "exactly" 0, 0 as receivers write it
PLACE_KM = 50.0                          # tag's place lookup itself snaps to a city within 50 km

# Clock-reset defaults: (date, makes it applies to (casefold substrings) or None for any, confidence, words,
# midnight-hour only). A camera whose clock was never set (or lost its battery) starts from its default and
# counts up, so the default day itself is the tell. Sources: docs/dates.md, "Clock-reset defaults".
RESETS = (
    (datetime.date(2015, 12, 31), ("gopro",), "high", "what a GoPro with an unset clock writes "
     "(2016-01-01 00:00 UTC, seen a day earlier west of UTC)", False),
    (datetime.date(2016, 1, 1), ("gopro",), "high", "what a GoPro with an unset clock writes", False),
    (datetime.date(1904, 1, 1), None, "high", "the QuickTime epoch: an unset video date", False),
    (datetime.date(1970, 1, 1), None, "high", "the Unix epoch: a clock that was never set", False),
    (datetime.date(1980, 1, 1), None, "high", "the DOS/FAT epoch: a clock that was never set", False),
    (datetime.date(2000, 1, 1), None, "high", "the date many cameras and phones reset to", False),
    (datetime.date(2004, 1, 1), None, "low", "a midnight some older compact cameras start their clock from", True),
    (datetime.date(2005, 1, 1), None, "low", "a midnight some older compact cameras start their clock from", True),
)

# Device era: (make substring (casefold) or None, model regex (casefold, on the EXIF Model), {group: (year,
# month)} or a fixed (year, month), family name). First match wins; a model no rule names is never checked.
# First-sale months from the makers' own pages (docs/dates.md, "Device era", lists each source).
_GOPRO = {"3": (2012, 10), "4": (2014, 10), "5": (2016, 10), "6": (2017, 9), "7": (2018, 9), "8": (2019, 10),
          "9": (2020, 9), "10": (2021, 9), "11": (2022, 9), "12": (2023, 9), "13": (2024, 9)}
_IPHONE = {"6": (2014, 9), "6s": (2015, 9), "7": (2016, 9), "8": (2017, 9), "x": (2017, 11), "xs": (2018, 9),
           "xr": (2018, 10), "11": (2019, 9), "12": (2020, 10), "13": (2021, 9), "14": (2022, 9), "15": (2023, 9),
           "16": (2024, 9), "16e": (2025, 2), "17": (2025, 9), "air": (2025, 9)}
_PIXEL = {"": (2016, 10), "2": (2017, 10), "3": (2018, 10), "3a": (2019, 5), "4": (2019, 10), "4a": (2020, 8),
          "5": (2020, 10), "5a": (2021, 8), "6": (2021, 10), "6a": (2022, 7), "7": (2022, 10), "7a": (2023, 5),
          "8": (2023, 10), "8a": (2024, 5), "9": (2024, 8), "9a": (2025, 4), "10": (2025, 8)}
_GALAXY_S = {"g95": (2017, 4, "S8"), "g96": (2018, 3, "S9"), "g97": (2019, 3, "S10"), "g98": (2020, 3, "S20"),
             "g99": (2021, 1, "S21"), "s90": (2022, 2, "S22"), "s91": (2023, 2, "S23"), "s92": (2024, 1, "S24"),
             "s93": (2025, 2, "S25")}
ERAS = (
    ("gopro", re.compile(r"^hero(\d{1,2})(?:\+)?\b"), _GOPRO, "GoPro HERO{0}"),
    ("gopro", re.compile(r"^(?:gopro )?max$"), (2019, 10), "GoPro MAX"),
    ("apple", re.compile(r"^iphone (se) \((2nd|3rd) generation\)$"), {"2nd": (2020, 4), "3rd": (2022, 3)},
     "iPhone SE ({0} generation)"),
    ("apple", re.compile(r"^iphone (\d{1,2}e?|\d{1,2}s|xs|xr|x|air)\b"), _IPHONE, "iPhone {0}"),
    ("google", re.compile(r"^pixel(?: (\d{1,2}a?))?(?: (?:pro|xl|pro xl|pro fold))?$"), _PIXEL, "Pixel {0}"),
    ("samsung", re.compile(r"^sm-([gs]9\d)\d"), _GALAXY_S, "Galaxy {0}"),
)

# Video formats that can't be older than their standard: QuickTime CompressorID -> (first year, month, name).
CODECS = {"hvc1": (2013, 4, "HEVC (H.265)"), "hev1": (2013, 4, "HEVC (H.265)"), "vp09": (2013, 6, "VP9"),
          "av01": (2018, 3, "AV1")}
HEIF_EXT = {".heic", ".heif"}  # a HEIC photo is an HEVC still

# Camera file numbers: (regex on the name, family). GoPro: the recording number, then the chapter.
SEQ_RX = ((re.compile(r"^G([HX])(\d\d)(\d{4})\.MP4$"), lambda m: ("gopro-g" + m[1].lower(), int(m[3]), int(m[2]))),
          (re.compile(r"^GOPR(\d{4})\.(?:MP4|JPG)$"), lambda m: ("gopro", int(m[1]), 0)),
          (re.compile(r"^GP(\d\d)(\d{4})\.MP4$"), lambda m: ("gopro", int(m[2]), int(m[1]))),
          (re.compile(r"^([A-Za-z]{2,5})_?(\d{4,5})(?:_\d{1,3})?\.[A-Za-z0-9]{2,5}$"),
           lambda m: (m[1].upper(), int(m[2]), 0)))
MESSAGING_RX = re.compile(r"-WA\d{4}\b|@\d\d-\d\d-20\d\d_")  # a name holding when it was received, not taken


class Changed(RuntimeError):
    """An undo refused: a sidecar changed since the fix (another action, the CLI, digiKam)."""


# ---------------------------------------------------------------- small helpers
def state_dir():
    return intake.STATE


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def from_iso(s):
    try:
        return datetime.datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def month_name(y, m):
    return datetime.date(y, m, 1).strftime("%B %Y")


def km(a, b):
    """Great-circle distance in km between two (lat, lon)."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def save_private(p, obj):
    """Write obj as JSON to p atomically, 0600 from the first byte (a temp file in the same folder, then a
    rename): a crash leaves the old file or the new one, never half of one."""
    tmp = f"{p}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    os.chmod(p, 0o600)


def _exiftool_rows(paths, args):
    if not paths:
        return {}
    r = subprocess.run(["exiftool", "-j", "-q", "-q", *args, "-@", "-"], input="\n".join(paths) + "\n",
                       capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return {}
    return {row.get("SourceFile"): row for row in rows if isinstance(row, dict)}


# ---------------------------------------------------------------- which files
def under(p, root):
    rp, rr = os.path.realpath(p), os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def in_library(p):
    return any(under(p, r) for r in intake.LIB_ROOTS) and not under(p, intake.STATE)


def media(p):
    return os.path.splitext(p)[1].lower() in intake.IMG | intake.VID


def library_files(paths=None):
    """Media files to check: every one under the library roots, or under/at the given paths. Hidden folders,
    symlinks and the state folder are skipped, as in the gallery."""
    out, seen = [], set()
    tops = [os.path.abspath(p) for p in paths] if paths else [r for r in intake.LIB_ROOTS if os.path.isdir(r)]
    for top in tops:
        if os.path.isfile(top) and not os.path.islink(top):
            if media(top) and not under(top, intake.STATE) and os.path.realpath(top) not in seen:
                seen.add(os.path.realpath(top))
                out.append(top)
            continue
        if not os.path.isdir(top) or under(top, intake.STATE):
            continue
        for d, dirs, names in os.walk(top, followlinks=False):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and not under(os.path.join(d, x), intake.STATE))
            for n in sorted(names):
                p = os.path.join(d, n)
                if n.startswith(".") or not media(p) or os.path.islink(p) or not os.path.isfile(p):
                    continue
                rp = os.path.realpath(p)
                if rp not in seen:
                    seen.add(rp)
                    out.append(p)
    return out


def plans_by_path(state=None):
    """{library path: (plan entry, batch name)} from every batch's plan.json (a later batch wins)."""
    out = {}
    bd = os.path.join(state or state_dir(), "batches")
    try:
        names = sorted(os.listdir(bd))
    except OSError:
        return out
    for b in names:
        try:
            plan = intake.load(os.path.join(bd, b, "plan.json"), []) or []
        except (OSError, ValueError):
            continue
        for p in plan if isinstance(plan, list) else []:
            if isinstance(p, dict):
                for k in ("lib_dst", "dst"):
                    v = p.get(k)
                    if isinstance(v, str) and v:
                        out[v] = (p, b)
    return out


def _export_time(entry, batch, manifests, state):
    """A cloud export's date (naive local) for a planned file, from its batch's manifest, else None."""
    if batch not in manifests:
        try:
            m = intake.load(os.path.join(state, "batches", batch, "manifest.json"), {}) or {}
        except (OSError, ValueError):
            m = {}
        manifests[batch] = m.get("files", {}) if isinstance(m, dict) else {}
    e = manifests[batch].get(entry.get("rel"), {}) if isinstance(manifests[batch], dict) else {}
    t = dates.parse((e.get("export") or {}).get("time")) if isinstance(e, dict) else None
    if not t:
        return None
    return t[0].replace(tzinfo=dates.UTC).astimezone().replace(tzinfo=None)


# ---------------------------------------------------------------- reading
def sidecar_rows(paths):
    """{media path: exiftool row} of each existing sidecar: DateTimeOriginal (as written), GPS, tags."""
    xs = [intake.sidecar(p) for p in paths if os.path.isfile(intake.sidecar(p))]
    raw = _exiftool_rows(xs, ["-XMP-exif:DateTimeOriginal", "-HierarchicalSubject"])
    num = _exiftool_rows(xs, ["-n", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude"])
    out = {}
    for x in xs:
        row = dict(raw.get(x, {}))
        for k in ("GPSLatitude", "GPSLongitude"):
            if k in num.get(x, {}):
                row[k] = num[x][k]
        out[x[:-4]] = row
    return out


def gather(paths, quick=False, now=None, state=None):
    """One record per file: the date the library uses and every other date the file or its plan holds."""
    state = state or state_dir()
    now = now or datetime.datetime.now()
    metas = dates.read_meta(paths, gpmf=not quick, state=state)
    extra = _exiftool_rows(paths, ["-n", "-GPSLatitude", "-GPSLongitude", "-CompressorID"])
    side = sidecar_rows(paths)
    plans = plans_by_path(state)
    index = intake.load(os.path.join(state, "index.json"), {}) or {}
    manifests, recs = {}, []
    for p in paths:
        ap = os.path.abspath(p)
        meta = metas.get(ap, {})
        ext = os.path.splitext(p)[1].lower()
        tz = dates.tz_of(meta.get("OffsetTimeOriginal") or meta.get("OffsetTime"))
        cam = dates._camera(ap, meta, tz, None)
        gps, gslack = dates._gps(meta, tz, now, None)
        weak = dates._weak(ap, meta, None)
        name_c = next((w for w in weak if w.src == "filename"), None)
        sx = side.get(p, {})
        sd = dates.parse(sx.get("DateTimeOriginal"))
        resolved = None
        if sd:
            used, used_tz, used_src = sd[0], dates.tz_str(sd[1]), "sidecar"
        else:
            resolved = dates.resolve(ap, meta, now=now)
            used, used_tz, used_src = resolved.dt, resolved.tz, resolved.src
        plan, batch = plans.get(p, (None, None))
        dcim = None
        if plan and isinstance(plan.get("rel"), str):
            m = dates.FOLDER_RX.search(plan["rel"].replace(os.sep, "/"))
            if m and 1 <= int(m.group(2)) <= 12:
                dcim = (int(m.group(1)), int(m.group(2)))
        try:
            st = os.stat(p)
        except OSError:
            continue
        ix = index.get(p) if isinstance(index, dict) else None
        sha = ix.get("sha") if isinstance(ix, dict) and ix.get("size") == st.st_size else None
        tags = sx.get("HierarchicalSubject") or []
        tags = tags if isinstance(tags, list) else [tags]
        e = extra.get(ap, {})
        recs.append(dict(
            path=p, name=os.path.basename(p), ext=ext, video=ext in intake.VID,
            make=" ".join(str(meta.get("Make") or "").split()), model=" ".join(str(meta.get("Model") or "").split()),
            device=meta.get("Device") or "", used=used, used_tz=used_tz, used_src=used_src,
            camera=cam.local if cam else None, camera_src=cam.src if cam else None,
            gps=gps.local if gps else None, gps_slack=gslack or datetime.timedelta(0),
            name_date=name_c.local if name_c else None, name_day_only=bool(name_c and name_c.day_only),
            received=bool(MESSAGING_RX.search(os.path.basename(p))) or f"{os.sep}Messaging{os.sep}" in p,
            export=_export_time(plan, batch, manifests, state) if plan else None, dcim=dcim,
            mtime=datetime.datetime.fromtimestamp(st.st_mtime), size=st.st_size, sha=sha,
            plan_src=(plan or {}).get("date_src"), codec=str(e.get("CompressorID") or "").strip().lower(),
            lat=_num(e.get("GPSLatitude")), lon=_num(e.get("GPSLongitude")),
            x_lat=_num(sx.get("GPSLatitude")), x_lon=_num(sx.get("GPSLongitude")),
            place=next((str(t)[6:] for t in tags if str(t).startswith("Place|")), None),
            resolved_suspect=bool(resolved and resolved.suspect)))
    return recs


# ---------------------------------------------------------------- the checks
def finding(r, kind, conf, reason, suggest=None, suggest_src=None, **extra):
    return dict(path=r["path"], kind=kind, confidence=conf, reason=reason, date=iso(r["used"]),
                date_src=r["used_src"], suggest=iso(suggest), suggest_src=suggest_src if suggest else None, **extra)


def best_other(r):
    """(date, source) of the strongest date the file holds that is not the one in use, for a suggestion."""
    for src, d in (("gps", r["gps"]), ("filename", None if r["name_day_only"] or r["received"] else r["name_date"]),
                   ("export", r["export"])):
        if d and abs(d - r["used"]) > AGREE:
            return d, src
    return None, None


def check_reset(r):
    d = r["used"]
    make = r["make"].casefold()
    for day, makes, conf, words, midnight in RESETS:
        if d.date() != day or (midnight and d.hour):
            continue
        if makes and not any(m in make for m in makes):
            if make:
                continue  # another maker's file on that day: a real New Year's photo, most likely
            conf = "medium"  # no make recorded: it may be one of those cameras
        s, src = best_other(r)
        return finding(r, "reset", conf, f"{d:%Y-%m-%d} is {words}", s, src)
    return None


def first_sale(make, model):
    """((year, month), family) of the first model of this family on sale, else None (unknown model)."""
    mk, md = make.casefold(), " ".join(model.casefold().split())
    for maker, rx, when, family in ERAS:
        if maker and maker not in mk and maker not in md:
            continue
        m = rx.match(md)
        if not m:
            continue
        if isinstance(when, tuple):
            return when, family
        key = m.groups()[-1] or ""
        hit = when.get(key)
        if hit is None:
            return None
        name = hit[2] if len(hit) > 2 else key.upper() if key in ("x", "xs", "xr") else "Air" if key == "air" else key
        return hit[:2], family.format(name).strip()
    return None


def check_era(r):
    got = first_sale(r["make"], r["model"])
    if not got:
        return None
    (y, m), family = got
    if r["used"] >= datetime.datetime(y, m, 1) - ERA_SLACK:
        return None
    s, src = best_other(r)
    return finding(r, "era", "high", f"taken with a {family}, first sold in {month_name(y, m)}, but dated "
                                     f"{r['used']:%Y-%m-%d}", s, src)


def check_codec(r):
    hit = CODECS.get(r["codec"]) if r["video"] else (CODECS["hvc1"] if r["ext"] in HEIF_EXT else None)
    if not hit or r["used"] >= datetime.datetime(hit[0], hit[1], 1):
        return None
    s, src = best_other(r)
    what = "a HEIC photo (HEVC)" if not r["video"] else f"{hit[2]} video"
    return finding(r, "codec", "high", f"{what} dated {r['used']:%Y-%m-%d}, but {hit[2]} only exists since "
                                       f"{month_name(hit[0], hit[1])}", s, src)


def check_sources(r):
    """Every source that disagrees with the date in use, with the confidence the strongest one gives."""
    u, bad, conf = r["used"], [], None

    def note(src, d, c, words=None):
        nonlocal conf
        bad.append(words or f"{src} {d:%Y-%m-%d %H:%M}")
        conf = c if conf is None or CONF.index(c) < CONF.index(conf) else conf

    if r["gps"] and abs(r["gps"] - u) > AGREE + r["gps_slack"]:
        note("GPS time", r["gps"], "high" if abs(r["gps"] - u) > FAR else "medium",
             f"GPS time {r['gps']:%Y-%m-%d %H:%M}")
    n = r["name_date"]
    if n:
        lo, hi = n - dates.MAX_TZ - DAY, n + (DAY if r["name_day_only"] else datetime.timedelta(0)) + dates.MAX_TZ + DAY
        if r["received"]:
            lo = u  # received: taken any time before it arrived, never after
        if not lo <= u <= hi:
            far = u < lo - FAR or u > hi + FAR
            note("the file name", n, "high" if far and not r["name_day_only"] else "medium",
                 f"the file name's date {n:%Y-%m-%d}" + ("" if r["name_day_only"] else f" {n:%H:%M}"))
    if r["export"] and abs(r["export"] - u) > AGREE:
        note("export", r["export"], "high" if abs(r["export"] - u) > FAR else "medium",
             f"the cloud export's date {r['export']:%Y-%m-%d %H:%M}")
    # A date someone corrected on purpose (plan's GPS, --clock-offset or export date, or fix-dates) is meant to
    # differ from the camera's own clock, and from the file times that clock wrote.
    corrected = r["plan_src"] in ("gps", "offset", "export") or r.get("fixed") or r["used_src"] in ("gps", "export")
    if r["camera"] and r["used_src"] == "sidecar" and not corrected and abs(r["camera"] - u) > AGREE:
        note("camera", r["camera"], "medium", f"the camera's own date {r['camera']:%Y-%m-%d %H:%M}")
    if r["dcim"]:
        y, m = r["dcim"]
        if abs((u.year * 12 + u.month) - (y * 12 + m)) > 1:
            note("folder", None, "medium", f"its DCIM folder's month {month_name(y, m)}")
    if u > r["mtime"] + SEQ_SLACK and not corrected:
        note("mtime", r["mtime"], "low", f"the file's last change {r['mtime']:%Y-%m-%d} (before it was taken?)")
    if not bad:
        return None
    s, src = best_other(r)
    if not s and r["camera"] and "camera" in " ".join(bad):
        s, src = r["camera"], "camera"
    used = "from its sidecar" if r["used_src"] == "sidecar" else intake.DATE_FROM.get(r["used_src"],
                                                                                        "from " + r["used_src"])
    return finding(r, "sources", conf, f"dated {u:%Y-%m-%d %H:%M} ({used}), but " + "; ".join(bad), s, src)


def check_gps(r):
    out = []
    for lat, lon, where in ((r["lat"], r["lon"], "the file's own GPS"), (r["x_lat"], r["x_lon"], "the sidecar's GPS")):
        if lat is not None and lon is not None and abs(lat) < NULL_EPS and abs(lon) < NULL_EPS:
            out.append(finding(r, "null-island", "high", f"{where} is exactly 0, 0 (a receiver with no fix; "
                                                         "no place should come from it)"))
            break
    if None not in (r["lat"], r["lon"], r["x_lat"], r["x_lon"]) and not out:
        d = km((r["lat"], r["lon"]), (r["x_lat"], r["x_lon"]))
        if d > PLACE_KM:
            out.append(finding(r, "place", "medium", f"the sidecar's GPS{' (Place: ' + r['place'] + ')' if r['place'] else ''}"
                                                     f" is {d:,.0f} km from the file's own GPS"))
    return out


def seq_key(name):
    for rx, f in SEQ_RX:
        m = rx.match(name)
        if m:
            return f(m)
    return None


def lnds(values):
    """Indices of one longest non-decreasing subsequence of values."""
    tails, tails_i, prev = [], [], [None] * len(values)
    for i, v in enumerate(values):
        k = bisect.bisect_right(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_i.append(i)
        else:
            tails[k], tails_i[k] = v, i
        prev[i] = tails_i[k - 1] if k else None
    out, i = [], tails_i[-1] if tails_i else None
    while i is not None:
        out.append(i)
        i = prev[i]
    return out[::-1]


def check_sequences(recs, flagged):
    """Sequence outliers, and a neighbours' bracket for files other checks already flagged (flagged: {path}).

    Files of one camera (device id) and one naming family (GX, GOPR/GP, IMG, DSC, ...) sorted by their number
    should be in date order. Files flagged by reset / era / codec are left out of that order (their date is
    the question); of the rest, one longest run in date order stands, and every other file is an outlier
    when its date falls outside its nearest standing neighbours by more than SEQ_SLACK. A group whose numbers
    repeat (a counter that wrapped, two cameras with one id) is not checked. Returns (findings,
    {path: (date, words)} suggestions for flagged files, groups skipped)."""
    groups = collections.defaultdict(list)
    for r in recs:
        k = seq_key(r["name"])
        if k and r["device"]:
            groups[(r["device"], k[0])].append((k[1], k[2], r))
    out, hints, skipped = [], {}, 0
    for (_, family), g in groups.items():
        if len(g) < 3:
            continue
        g.sort(key=lambda x: (x[0], x[1], x[2]["name"]))
        if len({(n, c) for n, c, _ in g}) < len(g):
            skipped += 1
            continue
        anchors = [i for i, (_, _, r) in enumerate(g) if r["path"] not in flagged]
        keep = {anchors[j] for j in lnds([g[i][2]["used"] for i in anchors])}
        kept = sorted(keep)
        for i, (n, _, r) in enumerate(g):
            if i in keep:
                continue
            j = bisect.bisect_left(kept, i)
            lo = g[kept[j - 1]] if j else None
            hi = g[kept[j]] if j < len(kept) else None
            if not lo and not hi:
                continue
            d = r["used"]
            if lo and hi:
                span = hi[0] - lo[0]
                frac = (n - lo[0]) / span if span else 0.0
                guess = lo[2]["used"] + (hi[2]["used"] - lo[2]["used"]) * frac
            else:
                guess = (lo or hi)[2]["used"]
            guess = guess.replace(microsecond=0)
            between = (f"between {lo[2]['name']} ({lo[2]['used']:%Y-%m-%d %H:%M}) and {hi[2]['name']} "
                       f"({hi[2]['used']:%Y-%m-%d %H:%M})" if lo and hi else
                       f"after {lo[2]['name']} ({lo[2]['used']:%Y-%m-%d %H:%M})" if lo else
                       f"before {hi[2]['name']} ({hi[2]['used']:%Y-%m-%d %H:%M})")
            bracket = [iso(lo[2]["used"]) if lo else None, iso(hi[2]["used"]) if hi else None]
            if r["path"] in flagged:
                hints[r["path"]] = (guess, f"numbered {between}", bracket)
                continue
            if (lo and d < lo[2]["used"] - SEQ_SLACK) or (hi and d > hi[2]["used"] + SEQ_SLACK):
                off = min(abs(d - x[2]["used"]) for x in (lo, hi) if x)
                conf = "high" if lo and hi and off > datetime.timedelta(days=30) else "medium"
                out.append(finding(r, "sequence", conf, f"dated {d:%Y-%m-%d %H:%M}, but its number puts it "
                                                        f"{between}", guess, "neighbours", bracket=bracket))
    return out, hints, skipped


def check_duplicates(recs):
    by = collections.defaultdict(list)
    for r in recs:
        if r["sha"]:
            by[r["sha"]].append(r)
    out = []
    for g in by.values():
        if len(g) < 2:
            continue
        for r in g:
            others = [o for o in g if o is not r and abs(o["used"] - r["used"]) > DUP_SLACK]
            if others:
                o = others[0]
                out.append(finding(r, "duplicate", "medium", f"the same content is in the library as {o['name']} "
                                   f"dated {o['used']:%Y-%m-%d %H:%M}" + (f" (and {len(others) - 1} more)"
                                                                          if len(others) > 1 else "")))
    return out


def check(recs):
    """Every finding for these records, sorted by kind, then confidence, then path."""
    out, first = [], {}
    for r in recs:
        for f in (check_reset(r), check_era(r), check_codec(r)):
            if f:
                out.append(f)
                first.setdefault(r["path"], f)
    seq, hints, skipped = check_sequences(recs, set(first))
    for p, (guess, words, bracket) in hints.items():
        f = first[p]
        f["reason"] += f"; its file number puts it {words.split(' ', 1)[1]}"
        if not f["suggest"]:
            f.update(suggest=iso(guess), suggest_src="neighbours", bracket=bracket)
        else:
            f["bracket"] = bracket
    out += seq
    for r in recs:
        f = check_sources(r)
        if f:
            out.append(f)
        out += check_gps(r)
    out += check_duplicates(recs)
    out.sort(key=lambda f: (KINDS.index(f["kind"]), CONF.index(f["confidence"]), f["path"]))
    return out, skipped


# ---------------------------------------------------------------- the report
def report_path(state=None):
    return os.path.join(state or state_dir(), REPORT)


def load_report(state=None):
    """The last check-dates report ({"version", "id", "checked", "files", "findings"}), or None."""
    try:
        r = intake.load(report_path(state))
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict) or r.get("version") != VERSION or not isinstance(r.get("findings"), list):
        return None
    return r


def audit(paths=None, quick=False, now=None, state=None, save=True):
    """Run every check over the library (or the given paths). Returns (report, refused count). With save, the
    report goes to <state>/datecheck.json; a run over some paths replaces only those paths' findings."""
    state = state or state_dir()
    started = iso(datetime.datetime.now())
    files = library_files(paths)
    bad = set(label.refused(files, state))
    files = [f for f in files if f not in bad]
    recs = gather(files, quick=quick, now=now, state=state)
    fixed = {f["path"] for fx in load_fixes(state)["fixes"] if not fx.get("undone") for f in fx["files"]}
    for r in recs:
        r["fixed"] = r["path"] in fixed
    findings, skipped = check(recs)
    rep = dict(version=VERSION, id=secrets.token_hex(8), checked=iso(now or datetime.datetime.now()),
               files=len(recs), refused=len(bad), groups_skipped=skipped, findings=findings,
               scope="paths" if paths else "library")
    if save:
        import library_lock
        with library_lock.hold("check-dates report", wait=library_lock.cli_wait(), out=sys.stderr):
            _save_report(rep, paths, recs, bad, started, state)
    return rep, len(bad)


def _save_report(rep, paths, recs, bad, started, state):
    """(Under the library lock: a fix-dates write rewrites the report too.) A file fixed while this run read the
    library leaves its date findings out: they describe the date before the fix."""
    since = {f["path"] for fx in load_fixes(state)["fixes"] if not fx.get("undone") and (fx.get("at") or "") >= started
             for f in fx["files"]}
    rep["findings"] = [f for f in rep["findings"] if not (f["path"] in since and f["kind"] in DATE_KINDS)]
    old = load_report(state)
    if paths and old:
        mine = {r["path"] for r in recs} | bad
        rep["findings"] = [f for f in old["findings"] if f.get("path") not in mine] + rep["findings"]
        rep["findings"].sort(key=lambda f: (KINDS.index(f["kind"]) if f.get("kind") in KINDS else 99,
                                            CONF.index(f["confidence"]) if f.get("confidence") in CONF else 9,
                                            f.get("path", "")))
        rep["scope"] = old.get("scope", "library")
    intake.private_dir(state)
    save_private(report_path(state), rep)


def visible(report, kinds=DATE_KINDS):
    """{path: [findings]} of these kinds (the gallery's Dates to check view)."""
    out = collections.defaultdict(list)
    for f in (report or {}).get("findings", []):
        if isinstance(f, dict) and f.get("kind") in kinds and isinstance(f.get("path"), str):
            out[f["path"]].append(f)
    return dict(out)


def best_suggestion(fs):
    """(date, source) of the strongest suggestion among one file's findings, else (None, None)."""
    rank = ("gps", "filename", "export", "neighbours", "camera")
    got = [(rank.index(f["suggest_src"]) if f.get("suggest_src") in rank else 9, f["suggest"], f.get("suggest_src"))
           for f in fs if f.get("suggest")]
    if not got:
        return None, None
    got.sort()
    return from_iso(got[0][1]), got[0][2]


# ---------------------------------------------------------------- fixing
WHEN_RX = re.compile(r"(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d))?)?\s*(Z|[+-]\d\d:?\d\d)?")
SHIFT_RX = re.compile(r"([+-])((?:\d{1,6}[ydhm])+)")
SHIFT_PART = re.compile(r"(\d{1,6})([ydhm])")


def parse_when(s, what="--set"):
    """'YYYY-MM-DD[THH:MM[:SS]][+HH:MM]' -> (datetime, has a time, tz text or None). Raises ValueError; what
    names the value in the message (the CLI's option, or the gallery's field)."""
    m = WHEN_RX.fullmatch(str(s or "").strip())
    if not m:
        raise ValueError(f"{what} wants YYYY-MM-DD or YYYY-MM-DD HH:MM, got {s!r}")
    g = m.groups()
    try:
        d = datetime.datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3] or 0), int(g[4] or 0), int(g[5] or 0))
    except ValueError:
        raise ValueError(f"{what} {s!r} is not a date") from None
    tz = None
    if g[6]:
        z = dates.tz_of(g[6])
        if z is None:
            raise ValueError(f"{what} {s!r}: the UTC offset must be like +02:00")
        tz = dates.tz_str(z)
    return d, g[3] is not None, tz


def parse_shift(s, what="--shift"):
    """'+8y', '-3h30m', '+2d' -> (sign, {unit: n}). Raises ValueError."""
    m = SHIFT_RX.fullmatch(str(s or "").strip())
    if not m:
        raise ValueError(f"{what} wants a sign and amounts in y, d, h or m (minutes), like +8y, -3h30m or +2d; "
                         f"got {s!r}")
    parts = collections.Counter()
    for n, u in SHIFT_PART.findall(m.group(2)):
        parts[u] += int(n)
    if not any(parts.values()):
        raise ValueError(f"{what} {s!r} moves nothing")
    if parts["y"] > 100:
        raise ValueError(f"{what} {s!r}: at most 100 years")
    return (-1 if m.group(1) == "-" else 1), dict(parts)


def shifted(d, shift):
    sign, parts = shift
    y = d.year + sign * parts.get("y", 0)
    try:
        d = d.replace(year=y)
    except ValueError:  # 29 February into a year without one
        d = d.replace(year=y, day=28)
    return d + sign * datetime.timedelta(days=parts.get("d", 0), hours=parts.get("h", 0), minutes=parts.get("m", 0))


def current(paths, state=None):
    """{path: (date in use, tz text, old sidecar value or None, camera date or None)} for files to fix."""
    side = sidecar_rows(paths)
    metas = dates.read_meta(paths, gpmf=False, state=state or state_dir())
    out = {}
    for p in paths:
        ap = os.path.abspath(p)
        meta = metas.get(ap, {})
        raw = side.get(p, {}).get("DateTimeOriginal")
        sd = dates.parse(raw)
        tz = dates.tz_of(meta.get("OffsetTimeOriginal") or meta.get("OffsetTime"))
        cam = dates._camera(ap, meta, tz, None)
        if sd:
            out[p] = (sd[0], dates.tz_str(sd[1]), str(raw), cam)
        else:
            r = dates.resolve(ap, meta)
            out[p] = (r.dt, r.tz, None, cam)
    return out


def refusals(paths, state=None):
    """{path: why} for files fix-dates must not touch: outside the library, not a media file, refused."""
    state = state or state_dir()
    out = {}
    for p in paths:
        if not os.path.isfile(p) or os.path.islink(p):
            out[p] = "not a file"
        elif not media(p):
            out[p] = "not a photo or video"
        elif not in_library(p):
            out[p] = "not in the library (fix-dates writes library sidecars only)"
    for p in label.refused([p for p in paths if p not in out], state):
        out[p] = "quarantined, staged or flagged by scan (never touched)"
    return out


def plan_fix(paths, how, value=None, like=None, report=None, now=None, state=None, what=None):
    """What a fix would write. how: set | shift | neighbours | suggested | like. Returns (changes, refused):
    changes [dict(path, old, old_date, new, tz, why)], refused {path: why}. Writes nothing."""
    state = state or state_dir()
    now = now or datetime.datetime.now()
    paths = list(dict.fromkeys(os.path.abspath(p) for p in paths))
    refused = refusals(paths, state)
    ok = [p for p in paths if p not in refused]
    shift = None  # set only for how == "shift"
    if how == "set":
        when, has_time, set_tz = parse_when(value, what or "--set")
    elif how == "shift":
        shift = parse_shift(value, what or "--shift")
    elif how in ("neighbours", "suggested"):
        report = report if report is not None else load_report(state)
        if report is None:
            raise ValueError("no check-dates report yet: run media-intake check-dates first")
        found = visible(report)
    elif how == "like":
        good = os.path.abspath(like or "")
        why = refusals([good], state).get(good)
        if why:
            raise ValueError(f"--like {like}: {why}")
        g = current([good], state)[good]
        if not g[3]:
            raise ValueError(f"--like {like}: it has no camera date of its own to measure an offset from")
        offset = g[0] - g[3].local
        if abs(offset) < datetime.timedelta(minutes=1):
            raise ValueError(f"--like {like}: its date is the camera's own; there is no offset to copy")
    else:
        raise ValueError(f"unknown fix {how!r}")
    cur = current(ok, state) if ok else {}
    changes = []
    for p in ok:
        old_d, old_tz, old_raw, cam = cur[p]
        tz = old_tz
        if how == "set":
            new = when if has_time else datetime.datetime.combine(when.date(), old_d.time())
            tz = set_tz if set_tz else (old_tz if not has_time else None)
            why = "set by hand"
        elif how == "shift":
            try:
                new = shifted(old_d, shift)
            except (ValueError, OverflowError):
                refused[p] = f"shifting {old_d:%Y-%m-%d} by {value} gives an impossible date"
                continue
            why = f"shifted {value}"
        elif how == "like":
            if not cam:
                refused[p] = "no camera date of its own to apply the offset to"
                continue
            new = (cam.local + offset).replace(microsecond=0)
            tz = None
            why = f"the offset of {os.path.basename(good)} ({_span(offset)})"
        else:
            fs = found.get(p, [])
            if how == "neighbours":
                fs = [f for f in fs if f.get("suggest_src") == "neighbours"]
            new, src = best_suggestion(fs)
            if not new:
                refused[p] = ("no suggestion from its numbered neighbours" if how == "neighbours"
                              else "no suggested date") + " in the last check-dates report"
                continue
            tz = None
            why = f"suggested by check-dates ({src})"
        if dates.impossible(new, now):
            refused[p] = f"the new date {new:%Y-%m-%d %H:%M} is impossible (before 2000-01-02 or in the future)"
            continue
        if new == old_d and tz == old_tz:
            refused[p] = "already has that date"
            continue
        changes.append(dict(path=p, old=old_raw, old_date=iso(old_d), new=iso(new), tz=tz, why=why))
    return changes, refused


def _span(td):
    s = "-" if td < datetime.timedelta(0) else "+"
    td = abs(td)
    days, rest = td.days, td.seconds
    h, m = rest // 3600, rest % 3600 // 60
    return s + "".join(f"{v}{u}" for v, u in ((days, "d"), (h, "h"), (m, "m")) if v) or "0m"


def fixes_path(state=None):
    return os.path.join(state or state_dir(), FIXES)


def load_fixes(state=None):
    try:
        f = intake.load(fixes_path(state), None)
    except (OSError, ValueError):
        f = None
    if not isinstance(f, dict) or f.get("version") != VERSION or not isinstance(f.get("fixes"), list):
        return dict(version=VERSION, fixes=[])
    return f


def fixed_paths(state=None):
    """{library path: aware datetime} of the date a fix-dates write (or the gallery's Fix date) set and no undo took
    back, the latest per file: `tag BATCH` writes that date, not the plan's, so a rerun never undoes a fix."""
    out = {}
    for fx in load_fixes(state)["fixes"]:
        for f in fx.get("files", []):
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                continue
            if not fx.get("undone") and from_iso(f.get("new")):  # an undo put back the fix before it, if any
                out[f["path"]] = dates.aware(from_iso(f["new"]), f.get("tz"))
    return out


def _sidecar_sha(path):
    try:
        with open(intake.sidecar(path), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def _date_arg(new, tz):
    return "-XMP-exif:DateTimeOriginal=" + dates.exif_string(dates.aware(from_iso(new), tz))


def write_fix(changes, reason=None, state=None, now=None):
    """Write the changes (plan_fix) to the sidecars and record them for --undo. The caller holds the library
    lock. Date findings of those files leave the report (and come back on an undo). Returns the fix id."""
    state = state or state_dir()
    if not changes:
        raise ValueError("nothing to fix")
    intake.need_exiftool()
    blocks = []
    for c in changes:
        x = intake.sidecar(c["path"])
        if not x.endswith(".xmp") or c["path"].lower().endswith(".xmp") or not in_library(c["path"]):
            raise ValueError(f"refusing to write a non-sidecar: {x}")
        if not os.path.exists(x):
            with open(x, "w", encoding="utf-8") as f:
                f.write(intake.EMPTY_XMP)
        blocks.append(["-q", "-m", "-overwrite_original", _date_arg(c["new"], c["tz"]), x])
    failed = None
    try:
        intake.exiftool_batch(blocks)
    except SystemExit as e:  # exiftool stopped part-way: record what it may have written, so --undo still can
        failed = str(e)
    fid = secrets.token_hex(4)
    rep = load_report(state)
    paths = {c["path"] for c in changes}
    dropped = [f for f in (rep or {}).get("findings", []) if f.get("path") in paths and f.get("kind") in DATE_KINDS]
    if rep and dropped:
        rep["findings"] = [f for f in rep["findings"] if f not in dropped]
        save_private(report_path(state), rep)
    log = load_fixes(state)
    log["fixes"].append(dict(id=fid, at=iso(now or datetime.datetime.now()), reason=reason or "",
                             report=(rep or {}).get("id"), dropped=dropped, undone=None,
                             files=[dict(c, sha=_sidecar_sha(c["path"])) for c in changes]))
    intake.private_dir(state)
    save_private(fixes_path(state), log)
    if failed:
        raise RuntimeError(f"{failed} (fix {fid} recorded: fix-dates --undo {fid} --yes puts back what was written)")
    return fid


def find_fix(fid=None, state=None):
    """The fix record to undo: by id, else the last one not undone. Raises ValueError."""
    log = load_fixes(state)
    live = [f for f in log["fixes"] if not f.get("undone")]
    if fid in (None, "", "last"):
        if not live:
            raise ValueError("nothing to undo: no fix-dates write is left to undo")
        return live[-1]
    for f in log["fixes"]:
        if f.get("id") == fid:
            if f.get("undone"):
                raise ValueError(f"fix {fid} was undone already")
            return f
    raise ValueError(f"no fix {fid!r} (fix-dates --undo with no id undoes the last one)")


def undo_problems(fx):
    """Why this fix can't be undone now ([] when it can): a file gone, or its sidecar changed since."""
    out = []
    for f in fx["files"]:
        if not os.path.isfile(f["path"]):
            out.append(f"{f['path']}: no longer in the library")
        elif _sidecar_sha(f["path"]) != f.get("sha"):
            out.append(f"{f['path']}: its sidecar changed since the fix")
    return out


def write_undo(fid=None, state=None, now=None):
    """Put back exactly what one fix wrote (all its files or none). The caller holds the library lock. Raises
    Changed when a sidecar changed since (nothing is written), ValueError when there is nothing to undo."""
    state = state or state_dir()
    fx = find_fix(fid, state)
    bad = undo_problems(fx)
    if bad:
        raise Changed("can't undo: " + "; ".join(bad[:3]) + ("; ..." if len(bad) > 3 else "") + ". Nothing was changed")
    intake.need_exiftool()
    blocks = [["-q", "-m", "-overwrite_original", "-XMP-exif:DateTimeOriginal=" + (f["old"] or ""),
               intake.sidecar(f["path"])] for f in fx["files"]]
    for f in fx["files"]:
        if f["old"] and ("\n" in f["old"] or not dates.parse(f["old"])):
            raise ValueError("a recorded old date can't be written back; nothing was changed")
    try:
        intake.exiftool_batch(blocks)
    except SystemExit as e:  # stopped part-way: the record follows the sidecars as they are, so a retry can finish
        log = load_fixes(state)
        for f in log["fixes"]:
            if f.get("id") == fx["id"]:
                for x in f["files"]:
                    x["sha"] = _sidecar_sha(x["path"])
        save_private(fixes_path(state), log)
        raise RuntimeError(f"{e} (run the undo again to finish it)") from None
    log = load_fixes(state)
    for f in log["fixes"]:
        if f.get("id") == fx["id"]:
            f["undone"] = iso(now or datetime.datetime.now())
    save_private(fixes_path(state), log)
    rep = load_report(state)
    if rep and fx.get("dropped") and rep.get("id") == fx.get("report"):
        have = {json.dumps(f, sort_keys=True) for f in rep["findings"]}
        rep["findings"] += [f for f in fx["dropped"] if json.dumps(f, sort_keys=True) not in have]
        rep["findings"].sort(key=lambda f: (KINDS.index(f["kind"]) if f.get("kind") in KINDS else 99,
                                            CONF.index(f["confidence"]) if f.get("confidence") in CONF else 9,
                                            f.get("path", "")))
        save_private(report_path(state), rep)
    return fx


# ---------------------------------------------------------------- erase, forget, trash (intake.py calls these)
def _names(paths):
    """{base name: {the folders above it that a finding naming it may be in}}: its own folder's parent, so the
    file's folder and its sibling month folders (YYYY/YYYY-MM: a numbered neighbour a month on)."""
    out = collections.defaultdict(set)
    for p in paths:
        out[os.path.basename(p)].add(os.path.dirname(os.path.dirname(os.path.abspath(p))))
    return out


def _mentions(f, paths, names):
    """A finding about one of paths, or one whose reason names one of them (a numbered neighbour, a copy): the
    whole name (1.jpg never matches in 11.jpg or 1.jpg.bak), and only from a file in the same folder or a
    sibling one (a same-named file elsewhere is someone else)."""
    if f.get("path") in paths:
        return True
    if f.get("kind") not in ("sequence", "duplicate") or not isinstance(f.get("path"), str):
        return False
    reason, up = f.get("reason") or "", os.path.dirname(os.path.dirname(os.path.abspath(f["path"])))
    return any(n and up in ups and re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-]|\.\w)", reason)
               for n, ups in names.items())


def forget_paths(paths, state=None):
    """Erase / forget / trash / sweep-erased: drop every record of these paths from datecheck.json (their
    findings, and a neighbour's finding that names them) and date-fixes.json (their entries, old and new
    dates, and the findings a fix set aside). A fix left with no file goes. Both files are rewritten
    atomically, 0600. The caller holds the library lock. Returns (records dropped, "" or why a file could not
    be rewritten). Never raises."""
    state = state or state_dir()
    paths = {p for p in paths if isinstance(p, str)}
    if not paths:
        return 0, ""
    names, n, err = _names(paths), 0, []
    rep = load_report(state)
    if rep:
        keep = [f for f in rep["findings"] if not (isinstance(f, dict) and _mentions(f, paths, names))]
        if len(keep) != len(rep["findings"]):
            n += len(rep["findings"]) - len(keep)
            rep["findings"] = keep
            try:
                save_private(report_path(state), rep)
            except OSError as e:
                err.append(f"{REPORT}: {e.strerror or e}")
    elif os.path.exists(report_path(state)):
        err.append(f"{REPORT} is unreadable: its records were not checked")
    log = load_fixes(state)
    changed, fixes = False, []
    for fx in log["fixes"]:
        files = [f for f in fx.get("files", []) if not (isinstance(f, dict) and f.get("path") in paths)]
        dropped = [f for f in fx.get("dropped", []) if not (isinstance(f, dict) and _mentions(f, paths, names))]
        if len(files) != len(fx.get("files", [])) or len(dropped) != len(fx.get("dropped", [])):
            n += len(fx.get("files", [])) - len(files) + len(fx.get("dropped", [])) - len(dropped)
            changed = True
            fx = dict(fx, files=files, dropped=dropped)
        if fx.get("files"):
            fixes.append(fx)
        else:
            changed = True
    if changed:
        try:
            save_private(fixes_path(state), dict(log, fixes=fixes))
        except OSError as e:
            err.append(f"{FIXES}: {e.strerror or e}")
    return n, "; ".join(err)


def recorded_paths(state=None):
    """Every library path datecheck.json or date-fixes.json names (findings, fixes, set-aside findings)."""
    out = {f["path"] for f in (load_report(state) or {}).get("findings", [])
           if isinstance(f, dict) and isinstance(f.get("path"), str)}
    for fx in load_fixes(state)["fixes"]:
        for f in list(fx.get("files", [])) + list(fx.get("dropped", [])):
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                out.add(f["path"])
    return out


def stale(roots, state=None):
    """Recorded paths whose file is gone from a library folder that is there (sources.root_present: an unplugged
    drive's files, or its empty mountpoint's, are not gone)."""
    import sources
    live = sources.present_roots(roots)
    return {p for p in recorded_paths(state) if sources.file_gone(p, roots, live)}


# ---------------------------------------------------------------- the commands (intake.py's parser calls these)
def _shown_date(s):
    d = from_iso(s)
    return f"{d:%Y-%m-%d %H:%M}" if d else "-"


def cmd_check_dates(a):
    intake.need_exiftool()
    for p in a.paths or []:
        if not os.path.exists(p):
            sys.exit(f"check-dates: {p}: no such file or folder")
    rep, refused = audit(a.paths or None, quick=a.quick)
    if a.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        return
    c = collections.Counter(f["kind"] for f in rep["findings"])
    print(f"check-dates: {intake.plural(rep['files'], 'file')} read"
          + (f", {refused:,} quarantined or flagged left out (never read)" if refused else "")
          + f"; {intake.plural(len({f['path'] for f in rep['findings']}), 'file')} to check")
    for k in KINDS:
        if c[k]:
            print(f"  {c[k]:,} {TITLES[k].lower()}")
    if rep["groups_skipped"]:
        print(f"  ({intake.plural(rep['groups_skipped'], 'camera numbering group')} not compared: its numbers repeat)")
    for k in KINDS:
        fs = [f for f in rep["findings"] if f["kind"] == k]
        if not fs:
            continue
        print(f"\n{TITLES[k]} ({len(fs):,}):")
        for f in fs:
            sug = f"  suggest {_shown_date(f['suggest'])} ({f['suggest_src']})" if f.get("suggest") else ""
            print(f"  [{f['confidence']}] {intake.shown_path(f['path'])}{sug}\n      {f['reason']}")
    if any(f.get("suggest") for f in rep["findings"]):
        print("\nTo fix, name the files (a dry run until --yes): media-intake fix-dates --suggested FILE...")
    print(f"report: {intake.shown_path(report_path())} (the gallery's Dates to check view reads it)")


def cmd_fix_dates(a):
    if a.undo is not None:
        if a.files:
            sys.exit("fix-dates --undo takes no files: it undoes one earlier fix (the last, or the id given)")
        try:
            fx = find_fix(a.undo)
        except ValueError as e:
            sys.exit(f"fix-dates: {e}")
        bad = undo_problems(fx)
        print(f"fix-dates --undo {fx['id']} ({fx['at']}, {intake.plural(len(fx['files']), 'file')}):")
        for f in fx["files"]:
            print(f"  {intake.shown_path(f['path'])}  {_shown_date(f['new'])} -> "
                  + (_shown_date(dates.parse(f['old'])[0].isoformat()) if f["old"] else "(no date in the sidecar)"))
        if bad:
            sys.exit("can't undo: " + "; ".join(bad) + ". Nothing was changed")
        if not a.yes:
            print("dry run: nothing written. Run again with --yes to put these back.")
            return
        try:
            write_undo(fx["id"])
        except (Changed, ValueError, RuntimeError) as e:
            sys.exit(f"fix-dates: {e}")
        print(f"undid fix {fx['id']}: {intake.plural(len(fx['files']), 'sidecar')} put back")
        return
    if not a.files:
        sys.exit("fix-dates: name the files to fix (FILE...)")
    how, value = ("set", a.set) if a.set else ("shift", a.shift) if a.shift else \
        ("neighbours", None) if a.from_neighbours else ("suggested", None) if a.suggested else ("like", None)
    try:
        changes, refused = plan_fix(a.files, how, value, like=a.like)
    except ValueError as e:
        sys.exit(f"fix-dates: {e}")
    print(f"fix-dates: {intake.plural(len(changes), 'file')} to change"
          + ("" if a.yes else " (dry run: nothing written)") + ":")
    for c in changes:
        print(f"  {intake.shown_path(c['path'])}  {_shown_date(c['old_date'])} -> {_shown_date(c['new'])}"
              + (c["tz"] or "") + f"  ({c['why']})")
    for p, why in refused.items():
        print(f"  refused {intake.shown_path(p)}: {why}")
    if not changes:
        sys.exit(1 if refused else 0)
    if not a.yes:
        print("Run again with --yes to write the sidecars (originals are never written; files stay in their "
              "folders). fix-dates --undo puts them back.")
        return
    try:
        fid = write_fix(changes, a.reason)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"fix-dates: {e}")
    print(f"fixed {intake.plural(len(changes), 'file')} (fix {fid}): sidecars only. Undo: "
          f"media-intake fix-dates --undo {fid} --yes")
