"""Dates and places from a cloud export's own metadata files: Google Takeout JSON, iCloud CSV.

A Google Photos Takeout or an iCloud Photos export is just a folder of photos and videos, so it
comes in as a folder or drive source (sources.py). Their files often lost their own capture date
(an edited copy, a messaging-app image, a scan), and the file's mtime is the download time. The
export keeps the date beside them:

  Google Takeout  one JSON per media file, in the same folder: photoTakenTime.timestamp (UTC
                  epoch seconds), geoDataExif / geoData (latitude, longitude; 0, 0 means none).
                  The JSON's name is the media's original name plus ".supplemental-metadata"
                  (newer exports) or nothing (older ones), cut to 46 characters, then a "(N)"
                  duplicate counter if the media name had one, then ".json":
                    IMG_0001.jpg            IMG_0001.jpg.supplemental-metadata.json
                    IMG_0001(1).jpg         IMG_0001.jpg.supplemental-metadata(1).json
                    <long name>.jpg         <first 46 characters of name + suffix>.json
                  An "-edited" copy uses its original's JSON; a Live Photo's video half uses the
                  still image's JSON (same name, image extension).
  iCloud Photos   "Photo Details.csv" (and numbered variants) beside the media, a header row
                  imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,
                  importDate; originalCreationDate like "Tuesday March 3,2015 7:45 PM GMT".

Neither format is documented by its vendor; the rules above, and where they were checked, are in
docs/intake.md ("Cloud exports"). Nothing here touches the network or writes a file: scan() only
lists the media's own folders and reads these small metadata files (never through a symlink).
Matching is by exact candidate names, never by a loose prefix: two media files that would claim
the same JSON both get none, and an iCloud name listed with two different dates gets none.

The date becomes dates.py's "export" source (above filename and mtime, below the file's own
EXIF / QuickTime / GPS); the place fills in GPS for tag (Place| tag, XMP GPS) only when the
file has none of its own.
"""
import csv, datetime, json, math, os, re, stat, unicodedata

GOOGLE_LIMIT = 46            # characters of "<name><suffix>" kept before "(N).json"
SUPPLEMENTAL = ".supplemental-metadata"
EDITED = "-edited"           # Google Photos' (English) suffix for an edited copy
JSON_MAX = 1 << 20           # a Takeout sidecar is a few KB; anything bigger is not one
CSV_MAX = 64 << 20
INDEX_RX = re.compile(r"(.+)\((\d+)\)")
DETAILS_RX = re.compile(r"photos? details", re.I)
ICLOUD_FORMATS = ("%A %B %d, %Y %I:%M %p", "%A %B %d, %Y %H:%M", "%a %b %d, %Y %I:%M %p",
                  "%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng"}
VIDEO_EXT = {".mp4", ".mov", ".3gp", ".mkv", ".webm"}
UTC = datetime.timezone.utc
FLOOR = datetime.datetime(1900, 1, 1, tzinfo=UTC)


def nfc(s):
    return unicodedata.normalize("NFC", s)


def split_index(name):
    """('IMG_1.jpg', '(1)') for 'IMG_1(1).jpg'; (name, '') when there is no duplicate counter."""
    stem, ext = os.path.splitext(name)
    m = INDEX_RX.fullmatch(stem)
    return (m.group(1) + ext, f"({m.group(2)})") if m else (name, "")


def google_names(name):
    """The JSON names Takeout may have given media file `name`, newest format first."""
    base, idx = split_index(nfc(name))
    out = []
    for suf in (SUPPLEMENTAL, ""):
        j = (base + suf)[:GOOGLE_LIMIT] + idx + ".json"
        if j not in out:
            out.append(j)
    return out


def fallback_names(name, siblings):
    """JSON names for a file without its own: an '-edited' copy's original, a Live Photo video's still."""
    base, idx = split_index(nfc(name))
    stem, ext = os.path.splitext(base)
    out = []
    if stem.endswith(EDITED) and len(stem) > len(EDITED):
        out += google_names(stem[:-len(EDITED)] + idx + ext)
    if ext.lower() in VIDEO_EXT:
        for s in sorted(siblings):
            sb, sidx = split_index(s)
            sstem, sext = os.path.splitext(sb)
            if sstem == stem and sidx == idx and sext.lower() in IMAGE_EXT:
                out += google_names(s)
    return out


def _utc_text(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _read_small(path, limit):
    """The bytes of a regular file of at most limit bytes, never through a symlink; None otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        return f.read(limit + 1)


def _coord(g):
    """(lat, lon) from a Takeout geoData / geoDataExif object, or None (0, 0 is Google's 'no place')."""
    if not isinstance(g, dict):
        return None
    lat, lon = g.get("latitude"), g.get("longitude")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)):
        return None
    if (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return float(lat), float(lon)


def read_google(path):
    """{"time": "YYYY-MM-DD HH:MM:SSZ", "lat", "lon"} from one Takeout JSON, or None if it isn't one."""
    raw = _read_small(path, JSON_MAX)
    try:
        d = json.loads(raw) if raw is not None else None
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("photoTakenTime"), dict):
        return None
    ts = d["photoTakenTime"].get("timestamp")
    if isinstance(ts, str) and ts.strip().isdigit():
        ts = int(ts.strip())
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        return None
    try:
        when = datetime.datetime.fromtimestamp(ts, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    out = {"time": _utc_text(when)}
    ll = _coord(d.get("geoDataExif")) or _coord(d.get("geoData"))
    if ll:
        out["lat"], out["lon"] = ll
    return out


def parse_icloud_date(s):
    """An aware UTC datetime from an iCloud CSV date ("Tuesday March 3,2015 7:45 PM GMT"), else None."""
    t = " ".join(str(s or "").split())
    t = re.sub(r",(?=\d{4}\b)", ", ", t)
    t = re.sub(r"\s*(GMT|UTC|Z)$", "", t, flags=re.I)
    for fmt in ICLOUD_FORMATS:
        try:
            d = datetime.datetime.strptime(t, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return d if d >= FLOOR else None
    return None


def read_icloud(path):
    """({NFC imgName: "YYYY-MM-DD HH:MM:SSZ"}, ambiguous names) from one Photo Details CSV;
    ({}, set()) when it isn't one."""
    raw = _read_small(path, CSV_MAX)
    if raw is None:
        return {}, set()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {}, set()
    rows = csv.reader(text.splitlines())
    header = [h.strip() for h in next(rows, [])]
    if "imgName" not in header or "originalCreationDate" not in header:
        return {}, set()
    i_name, i_date = header.index("imgName"), header.index("originalCreationDate")
    out, bad = {}, set()
    for r in rows:
        if len(r) <= max(i_name, i_date) or not r[i_name].strip():
            continue
        name, d = nfc(r[i_name].strip()), parse_icloud_date(r[i_date])
        if d is None:
            continue
        t = _utc_text(d)
        if out.get(name, t) != t:
            bad.add(name)
        out[name] = t
    return out, bad


def _listing(d):
    """({NFC name: real name} of the regular .json files, [Photo Details CSVs]) in folder d (no symlinks);
    None when d can't be listed."""
    js, cs = {}, []
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.startswith(".") or not e.is_file(follow_symlinks=False):
                    continue
                low = e.name.lower()
                if low.endswith(".json"):
                    js[nfc(e.name)] = e.name
                elif low.endswith(".csv") and DETAILS_RX.match(nfc(e.name)):
                    cs.append(e.name)
    except OSError:
        return None  # the folder can't be listed (permissions, unplugged): scan counts it as unreadable
    return js, sorted(cs)


def scan(root, files):
    """({rel: {"time", "via", "sidecar", ["lat", "lon"]}}, counts) for the walked media files
    [(abs path, rel)] whose export metadata was found. counts: google, icloud, places, ambiguous, and
    unreadable (folders that couldn't be listed, so their files go without export metadata)."""
    by_dir = {}
    for path, rel in files:
        by_dir.setdefault(os.path.dirname(path), []).append((path, rel))
    out, counts = {}, dict(google=0, icloud=0, places=0, ambiguous=0, unreadable=0)
    for d, items in sorted(by_dir.items()):
        listing = _listing(d)
        if listing is None:
            counts["unreadable"] += 1
            continue
        js, cs = listing
        if not js and not cs:
            continue
        names = [os.path.basename(p) for p, _ in items]
        direct = {rel: [j for j in google_names(os.path.basename(p)) if j in js] for p, rel in items}
        claims = {}
        for rel, hits in direct.items():
            if hits:
                claims.setdefault(hits[0], []).append(rel)
        icloud, ibad = {}, set()
        for c in cs:
            rows, bad = read_icloud(os.path.join(d, c))
            ibad |= bad | {n for n, t in rows.items() if icloud.get(n, (t,))[0] != t}
            icloud.update({n: (t, c) for n, t in rows.items()})
        for p, rel in items:
            name = os.path.basename(p)
            hits = direct[rel]
            if hits and len(claims[hits[0]]) > 1:  # two files truncate to one JSON name: trust neither
                counts["ambiguous"] += 1
                hits = []
            elif not hits:
                hits = [j for j in fallback_names(name, names) if j in js and len(claims.get(j, [])) <= 1]
            meta = None
            for j in hits:
                meta = read_google(os.path.join(d, js[j]))
                if meta:
                    meta.update(via="google-takeout", sidecar=os.path.relpath(os.path.join(d, js[j]), root))
                    counts["google"] += 1
                    break
            if meta is None and nfc(name) in icloud:
                if nfc(name) in ibad:
                    counts["ambiguous"] += 1
                else:
                    t, c = icloud[nfc(name)]
                    meta = dict(time=t, via="icloud", sidecar=os.path.relpath(os.path.join(d, c), root))
                    counts["icloud"] += 1
            if meta:
                counts["places"] += "lat" in meta
                out[rel] = meta
    return out, counts


def gps_of(entry):
    """[lat, lon] from a manifest file entry's export metadata, or None."""
    e = (entry or {}).get("export") or {}
    return [e["lat"], e["lon"]] if "lat" in e and "lon" in e else None
