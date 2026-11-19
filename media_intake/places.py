#!/usr/bin/env python3
"""My places: places you name ("Home", "The cabin"), tagged Place|<name> beside the nearest-city tag.

  media-intake places list [--show-coords]
  media-intake places add NAME (--at LAT,LON | --from FILE | --suggestion N) [--radius M]
  media-intake places rename NAME NEW [--anyway]
  media-intake places remove NAME [--anyway]
  media-intake places suggest [--show-coords] [--min N]
  media-intake places tag [--dry-run]

A place is a name, a point and a radius in metres. A file whose GPS lies inside the circle (great-circle
distance, haversine) gets Place|<name> in its sidecar, next to its Place|<city, state, CC> tag, which stays.
A place name never holds a comma, so the two kinds never mix: a Place| tag with a comma is the city
(intake.py writes it), one without is yours.

Stored only in <state>/places.json (0600, written atomically, per library by a hash of its folders, like
event-names.json): the definitions, and which files this tool tagged with each place or must never tag
with it again (you removed the tag, or said no to a "probably here" suggestion). Never in config.toml, a
sidecar of its own, git, a log or the terminal (only counts are printed; coordinates only with
--show-coords). Rules (docs/places.md):

  - Writes only its own tags: Place|<name> of a place defined here, on files it tagged. A tag you removed
    (in digiKam, or by hand) is never added back; rename and remove change only the files it tagged.
  - "Probably here" (the gallery): a file without GPS taken within PROBABLY_MINUTES of a file from the same
    source that is inside a place. Only a suggestion: nothing is tagged until you say yes.
  - Erase, forget and trash drop the records of the files they delete; verify-gone counts any left.
  - Not backed up: `backup` never copies the state folder (docs/backup.md).

Stdlib only; no network. intake.py imports this lazily (tag_files, erase), the gallery at start.
"""
import argparse, collections, datetime, hashlib, json, math, os, re, secrets, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

FILE = "places.json"  # {"version": 1, "libraries": {<library key>: {"places": {id: {...}}, "tagged": {id: [path]},
#                        "declined": {id: [path]}}}}
VERSION = 1
NAME_MAX = 64
RADIUS_DEFAULT, RADIUS_MIN, RADIUS_MAX = 150, 10, 20000  # metres
RADII = (50, 100, 150, 300, 500, 1000, 2000)  # the gallery's radius picker
R_M = 6371008.8  # mean Earth radius (IUGG), metres
SUGGEST_RADIUS = 150  # metres: files this close together make a spot
SUGGEST_MIN = 10  # files in a spot before suggest names it
EVENING = (18, 6)  # an evening shot: from 18:00 to 05:59 local
PROBABLY_MINUTES = 30
PREFIX = "Place|"


class Unreadable(RuntimeError):
    """places.json is there but not readable as this tool's file: never overwritten."""


# ---------------------------------------------------------------- geometry
def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Works across the antimeridian and at the poles (no planar shortcut)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_M * math.asin(min(1.0, math.sqrt(h)))


def inside(place, lat, lon):
    """True when (lat, lon) is within the place's radius (the boundary itself counts as inside)."""
    return haversine_m(place["lat"], place["lon"], lat, lon) <= place["radius"]


def check_point(lat, lon):
    """(lat, lon) as floats in range, else ValueError. (0, 0) is refused: it is what a camera writes for no fix."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("a point is two numbers: latitude, longitude") from None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("latitude is -90..90 and longitude -180..180")
    if lat == 0 and lon == 0:
        raise ValueError("0, 0 is what a camera writes when it has no GPS fix; give a real point")
    return lat, lon


def parse_point(s):
    """'LAT,LON' -> (lat, lon)."""
    parts = (s or "").replace(" ", "").split(",")
    if len(parts) != 2:
        raise ValueError("--at wants LAT,LON, e.g. --at 12.3456,-65.4321")
    return check_point(*parts)


def check_radius(r):
    try:
        r = float(r)
    except (TypeError, ValueError):
        raise ValueError("the radius is a number of metres") from None
    if not math.isfinite(r) or not RADIUS_MIN <= r <= RADIUS_MAX:
        raise ValueError(f"the radius is {RADIUS_MIN}-{RADIUS_MAX} metres")
    return round(r)


def xmp_coord(v):
    """An XMP GPS coordinate as exiftool writes it ('12,20.7407S', '12,20,44.4S') or a plain number, as a
    signed float; None when it isn't one."""
    if v is None:
        return None
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,3}),(\d{1,2}(?:\.\d+)?)(?:,(\d{1,2}(?:\.\d+)?))?([NSEW])", s)
    if not m:
        return None
    x = int(m.group(1)) + float(m.group(2)) / 60 + (float(m.group(3)) / 3600 if m.group(3) else 0.0)
    return -x if m.group(4) in "SW" else x


def gps_of(lat, lon):
    """(lat, lon) from two sidecar values, or None (missing, unparsable, out of range, or 0, 0)."""
    a, b = xmp_coord(lat), xmp_coord(lon)
    if a is None or b is None:
        return None
    try:
        return check_point(a, b)
    except ValueError:
        return None


# ---------------------------------------------------------------- names
def look_key(s):
    """Two names with one key would look the same: case, width, spacing, a dash or underscore as a space."""
    return " ".join(unicodedata.normalize("NFKC", s).casefold().replace("-", " ").replace("_", " ").split())


def check_name(name, others=()):
    """A place name: 1-64 visible characters on one line (runs of spaces as one), no invisible format
    characters (zero-width, bidi overrides), no '|' (a tag level) and no ',' (a city tag has one), not
    starting with '-'; and not looking like any of others (look_key). Returns it cleaned, else ValueError."""
    if not isinstance(name, str):
        raise ValueError("a place name is text")
    msg = f"a place name is 1-{NAME_MAX} visible characters on one line, without | or ,"
    name = unicodedata.normalize("NFC", name)
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs") for ch in name):
        raise ValueError(msg)
    name = " ".join(name.split())
    if not name or len(name) > NAME_MAX or "|" in name or "," in name or name.startswith("-") or "@@sep@@" in name:
        raise ValueError(msg)
    k = look_key(name)
    for o in others:
        if look_key(o) == k:
            raise ValueError(f"another place already looks like that ({o!r}); pick a name that differs by "
                             "more than case or spacing")
    return name


def tag_of(name):
    return PREFIX + name


def is_mine(tag):
    """A Place| tag of a place you named (no comma), not a city tag (intake.py's, always with a comma)."""
    return tag.startswith(PREFIX) and "," not in tag[len(PREFIX):] and len(tag) > len(PREFIX)


def leaf_drops(tags, remove, add=(), owner_leaves=()):
    """dc:Subject leaves to delete with the tags in remove: only those no remaining tag, added tag or digiKam
    TagsList entry still uses."""
    keep = {t.split("|")[-1] for t in tags if t not in remove} | {t.split("|")[-1] for t in add} | set(owner_leaves)
    return sorted({t.split("|")[-1] for t in remove} - keep)


# ---------------------------------------------------------------- the file
def library_key(roots):
    """Which library: a hash of its folders, as gallery.library_key (never a path in the file)."""
    rs = sorted({os.path.realpath(r) for r in roots})
    return hashlib.sha256("\0".join(rs).encode()).hexdigest()[:16]


def read_all(state):
    """{library key: raw entry}. Missing: {}. Unreadable or another shape: None."""
    try:
        with open(os.path.join(state, FILE), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    libs = d.get("libraries") if isinstance(d, dict) and d.get("version") == VERSION else None
    return libs if isinstance(libs, dict) and all(isinstance(v, dict) for v in libs.values()) else None


def write_private(p, data):
    tmp = f"{p}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _paths(v):
    return {p for p in v if isinstance(p, str) and os.path.isabs(p)} if isinstance(v, list) else set()


class Store:
    """This library's places and records. Callers that save hold the library lock (intake.py main, the
    gallery's writing(), places.main)."""

    def __init__(self, state, roots):
        self.state, self.key = state, library_key(roots)
        libs = read_all(state)
        if libs is None:
            raise Unreadable(f"{FILE} in the state folder is unreadable; fix or move it, then try again")
        raw = libs.get(self.key) or {}
        self.places, self.kept_bad = {}, {}
        for pid, p in (raw.get("places") or {}).items() if isinstance(raw.get("places"), dict) else ():
            try:
                lat, lon = check_point(p["lat"], p["lon"])
                self.places[pid] = dict(name=check_name(p["name"]), lat=lat, lon=lon, radius=check_radius(p["radius"]))
            except (KeyError, TypeError, ValueError):
                self.kept_bad[pid] = p  # edited by hand into something refused: unused, but written back
        self.tagged = collections.defaultdict(set)
        self.declined = collections.defaultdict(set)
        for k, rec in (("tagged", self.tagged), ("declined", self.declined)):
            for pid, v in (raw.get(k) or {}).items() if isinstance(raw.get(k), dict) else ():
                rec[pid] |= _paths(v)

    def empty(self):
        return not self.places and not self.kept_bad and not any(self.tagged.values()) \
            and not any(self.declined.values())

    def save(self):
        libs = read_all(self.state)
        if libs is None:
            raise Unreadable(f"{FILE} in the state folder is unreadable; fix or move it, then try again")
        known = set(self.places) | set(self.kept_bad)
        entry = {"places": dict(sorted({**self.kept_bad, **self.places}.items())),
                 "tagged": {k: sorted(v) for k, v in sorted(self.tagged.items()) if v and k in known},
                 "declined": {k: sorted(v) for k, v in sorted(self.declined.items()) if v and k in known}}
        if self.places or self.kept_bad:
            libs[self.key] = entry
        else:
            libs.pop(self.key, None)
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        write_private(os.path.join(self.state, FILE),
                      json.dumps({"version": VERSION, "libraries": libs}, ensure_ascii=False, indent=1).encode())

    # ---------------------------------------------- definitions
    def find(self, name):
        """The id of the place named name (look_key match), else None."""
        k = look_key(name or "")
        return next((pid for pid, p in self.places.items() if look_key(p["name"]) == k), None)

    def need(self, name):
        pid = self.find(name)
        if pid is None:
            raise ValueError(f"no place named {name!r} (media-intake places list shows them)")
        return pid

    def add(self, name, lat, lon, radius=RADIUS_DEFAULT):
        name = check_name(name, [p["name"] for p in self.places.values()])
        lat, lon = check_point(lat, lon)
        pid = secrets.token_hex(6)
        self.places[pid] = dict(name=name, lat=lat, lon=lon, radius=check_radius(radius))
        return pid

    def rename(self, pid, new):
        return check_name(new, [p["name"] for k, p in self.places.items() if k != pid])

    def at(self, lat, lon):
        return [pid for pid, p in self.places.items() if inside(p, lat, lon)]

    # ---------------------------------------------- one file
    def plan(self, path, gps, tags, only=None):
        """(tags to add, tags to remove) for one file's places, recording what it decides: a place's tag
        goes on a file inside it, unless declined; a tag this tool wrote and you removed since marks the file
        declined for that place (never added back); a file that moved out of a place (the place was moved or
        shrunk by hand) loses this tool's tag. A file with no GPS keeps a tag you confirmed. only: place ids.
        tags None: the sidecar could not be read (missing, or exiftool failed), so nothing is taken as removed
        by you and nothing is taken off."""
        add, remove = [], []
        known = tags is not None
        have = set(tags or ())
        for pid in (only if only is not None else list(self.places)):
            p = self.places[pid]
            t = tag_of(p["name"])
            if path in self.declined[pid]:
                continue
            mine = path in self.tagged[pid]
            if mine and known and t not in have:  # you took it off
                self.tagged[pid].discard(path)
                self.declined[pid].add(path)
                continue
            if gps is not None and inside(p, *gps):
                if t not in have:
                    add.append(t)
                self.tagged[pid].add(path)
            elif mine and gps is not None and known:
                remove.append(t)
                self.tagged[pid].discard(path)
        return add, remove

    def forget(self, paths):
        """Drop every record of these paths (erased, trashed or forgotten files). Returns how many went."""
        paths, n = set(paths), 0
        for rec in (self.tagged, self.declined):
            for pid in rec:
                n += len(rec[pid] & paths)
                rec[pid] -= paths
        return n

    def stale(self, roots):
        """Recorded paths whose file is gone from a library folder that is there (sources.root_present: an
        unplugged drive's files, or its empty mountpoint's, are not gone)."""
        import sources
        live = sources.present_roots(roots)
        out = set()
        for rec in (self.tagged, self.declined):
            for ps in rec.values():
                for p in ps:
                    if sources.file_gone(p, roots, live):
                        out.add(p)
        return out

    # ---------------------------------------------- "probably here"
    def probable(self, path, when, source, others):
        """[(place id, minutes apart)] for a file with no GPS: places that hold a file from the same source
        (a non-empty tuple of Source| tags) taken within PROBABLY_MINUTES of when (a naive datetime).
        others: [(when, source, (lat, lon))]. Never a place this file is tagged with or declined for."""
        if when is None or not source:
            return []
        best = {}
        for w, s, gps in others:
            if w is None or s != source or gps is None:
                continue
            mins = abs((w - when).total_seconds()) / 60
            if mins > PROBABLY_MINUTES:
                continue
            for pid in self.at(*gps):
                if path in self.declined[pid] or path in self.tagged[pid]:
                    continue
                if pid not in best or mins < best[pid]:
                    best[pid] = mins
        return sorted(((pid, round(m)) for pid, m in best.items()), key=lambda x: (x[1], self.places[x[0]]["name"]))


def forget_paths(state, paths):
    """Erase / forget / trash: drop the records of these paths from every library's entry in places.json.
    Returns (records dropped, "" or why the file could not be rewritten). Never raises. The caller holds the
    library lock (erase, forget and trash are intake.LOCKED; the gallery's jobs hold it for their steps)."""
    paths = {p for p in paths if isinstance(p, str)}
    libs = read_all(state)
    if not libs or not paths:
        return 0, "" if libs is not None else f"{FILE} is unreadable: its records were not checked"
    n = 0
    for entry in libs.values():
        for k in ("tagged", "declined"):
            rec = entry.get(k)
            if not isinstance(rec, dict):
                continue
            for pid, v in list(rec.items()):
                if isinstance(v, list):
                    keep = [p for p in v if p not in paths]
                    n += len(v) - len(keep)
                    if keep:
                        rec[pid] = keep
                    else:
                        del rec[pid]
    if not n:
        return 0, ""
    try:
        write_private(os.path.join(state, FILE),
                      json.dumps({"version": VERSION, "libraries": libs}, ensure_ascii=False, indent=1).encode())
    except OSError as e:
        return 0, f"{FILE} could not be rewritten ({type(e).__name__})"
    return n, ""


def stale_count(state, roots):
    """verify-gone: records in places.json of files gone from a library folder that is there."""
    try:
        return len(Store(state, roots).stale(roots))
    except Unreadable:
        return 0


# ---------------------------------------------------------------- suggest: dense GPS spots
def spots(points, radius=SUGGEST_RADIUS, min_n=SUGGEST_MIN):
    """Dense spots among points [(lat, lon, hour or None, city or "")]: greedily, the point with the most
    others within radius, then the next among those left. Points on one ~11 m cell are weighed together, so a
    home with thousands of photos stays fast. Returns [dict(lat, lon, n, evening, city)], biggest first."""
    cells = {}
    for lat, lon, hour, city in points:
        k = (round(lat, 4), round(lon, 4))
        c = cells.setdefault(k, dict(lat=k[0], lon=k[1], n=0, evening=0, cities=collections.Counter()))
        c["n"] += 1
        c["evening"] += hour is not None and (hour >= EVENING[0] or hour < EVENING[1])
        if city:
            c["cities"][city] += 1
    pts = list(cells.values())
    dlat = math.degrees(radius / R_M)
    grid = collections.defaultdict(list)

    def key(lat, lon):
        # Columns as wide as radius at the row's poleward edge (plus one row), so a neighbour is never more than
        # one column away; one column per row near a pole.
        row = math.floor((lat + 90) / dlat)
        edge = min(90.0, max(abs(row * dlat - 90), abs((row + 1) * dlat - 90)) + dlat)
        cos = math.cos(math.radians(edge))
        ncol = max(1, int(360 * cos / dlat)) if cos > 1e-9 else 1
        return row, math.floor((lon + 180) / 360 * ncol) % ncol, ncol

    for i, c in enumerate(pts):
        grid[key(c["lat"], c["lon"])[:2]].append(i)

    def near(c):
        out = []
        for drow in (-1, 0, 1):
            lat = c["lat"] + drow * dlat
            if not -90 <= lat <= 90:
                continue
            row, col, ncol = key(lat, c["lon"])
            for dcol in range(-2, 3) if ncol > 4 else range(ncol):
                for j in grid.get((row, (col + dcol) % ncol), ()):
                    if haversine_m(c["lat"], c["lon"], pts[j]["lat"], pts[j]["lon"]) <= radius:
                        out.append(j)
        return set(out)

    nb = [near(c) for c in pts]
    weight = [sum(pts[j]["n"] for j in s) for s in nb]
    used, out = set(), []
    for i in sorted(range(len(pts)), key=lambda i: (-weight[i], pts[i]["lat"], pts[i]["lon"])):
        if i in used:
            continue
        members = [j for j in nb[i] if j not in used]
        n = sum(pts[j]["n"] for j in members)
        if n < min_n:
            continue
        used.update(members)
        # centre: the mean of unit vectors (right across the antimeridian), weighed by files
        x = y = z = 0.0
        for j in members:
            la, lo, w = math.radians(pts[j]["lat"]), math.radians(pts[j]["lon"]), pts[j]["n"]
            x, y, z = x + w * math.cos(la) * math.cos(lo), y + w * math.cos(la) * math.sin(lo), z + w * math.sin(la)
        lat = math.degrees(math.atan2(z, math.hypot(x, y)))
        lon = math.degrees(math.atan2(y, x)) if math.hypot(x, y) > 1e-12 else 0.0
        cities = sum((pts[j]["cities"] for j in members), collections.Counter())
        out.append(dict(lat=lat, lon=lon, n=n, evening=sum(pts[j]["evening"] for j in members),
                        city=cities.most_common(1)[0][0] if cities else ""))
    out.sort(key=lambda s: (-s["n"], -s["evening"], s["lat"], s["lon"]))
    return out


# ---------------------------------------------------------------- the library's sidecars (CLI)
def library_rows(intake):
    """[dict(path, tags, gps, hour, city, leaves)] for every library file with a sidecar that tagging may
    touch (never one scan flagged, quarantined or erased: label.refused), read with exiftool. A sidecar exiftool
    could not read (empty, junk, cut off: intake.sidecar_unreadable) is left out, as if its drive were away:
    nothing is tagged into it and no tag missing from it is taken as one you removed."""
    import subprocess
    roots = [r for r in intake.LIB_ROOTS if os.path.isdir(r)]
    if not roots:
        return []
    r = subprocess.run(["exiftool", "-j", "-n", "-q", "-q", "-r", "-ext", "xmp", "-HierarchicalSubject",
                        "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude", "-XMP-exif:DateTimeOriginal",
                        "-XMP-photoshop:City", "-XMP-digiKam:TagsList", *intake.READ_CHECK, *roots],
                       capture_output=True, text=True)
    state = os.path.realpath(intake.STATE) + os.sep
    rows = []
    try:
        got = json.loads(r.stdout or "[]")
    except ValueError:  # cut off mid-way: nothing of this run is trusted
        got = []
    for row in got:
        x = row.get("SourceFile", "")
        if intake.sidecar_unreadable(row):
            continue
        media = os.path.abspath(x[:-4])
        if not x.lower().endswith(".xmp") or not os.path.isfile(media) or os.path.islink(media) \
                or os.path.realpath(media).startswith(state):
            continue
        m = re.match(r"\d{4}:\d\d:\d\d (\d\d):", str(row.get("DateTimeOriginal") or ""))
        rows.append(dict(path=media, tags=intake.as_list(row.get("HierarchicalSubject")),
                         gps=gps_of(row.get("GPSLatitude"), row.get("GPSLongitude")),
                         hour=int(m.group(1)) if m else None, city=str(row.get("City") or ""),
                         leaves={t.split("/")[-1] for t in intake.as_list(row.get("TagsList"))}))
    bad = set(intake.guard_label().refused([w["path"] for w in rows], intake.STATE))
    return [w for w in rows if w["path"] not in bad]


def write(intake, changes):
    """changes: [(row, add, remove)] -> sidecars written (intake.write_tags, the one sidecar writer)."""
    entries = [(w["path"], dict(hier=add, remove=remove, drop_leaves=leaf_drops(w["tags"], remove, add, w["leaves"])))
               for w, add, remove in changes if add or remove]
    for i in range(0, len(entries), 200):
        intake.write_tags(entries[i:i + 200])
    return len(entries)


def say_count(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def show_point(lat, lon):
    return f"{lat:.5f}, {lon:.5f}"


def cmd_list(a, intake, store):
    if not store.places:
        print("no places yet: media-intake places add NAME --from FILE (or --at LAT,LON), "
              "or media-intake places suggest")
        return
    for pid, p in sorted(store.places.items(), key=lambda x: look_key(x[1]["name"])):
        n = len(store.tagged[pid])
        print(f"{p['name']}: radius {p['radius']} m, {say_count(n, 'file')} tagged"
              + (f", {len(store.declined[pid]):,} you said no to" if store.declined[pid] else "")
              + (f", at {show_point(p['lat'], p['lon'])}" if a.show_coords else ""))
    if store.kept_bad:
        print(f"{say_count(len(store.kept_bad), 'entry')} in {FILE} could not be read and are ignored")


def suggestions(intake, store, min_n):
    rows = [w for w in library_rows(intake) if w["gps"] and not store.at(*w["gps"])]
    return spots([(w["gps"][0], w["gps"][1], w["hour"], w["city"]) for w in rows], min_n=min_n)


def cmd_suggest(a, intake, store):
    got = suggestions(intake, store, a.min)
    if not got:
        print(f"no spot with {a.min} or more files within {SUGGEST_RADIUS} m outside your places")
        return
    for n, s in enumerate(got[:20], 1):
        print(f"{n}. {say_count(s['n'], 'file')} within {SUGGEST_RADIUS} m, {s['evening']:,} in the evening, "
              + (f"near {s['city']}" if s["city"] else "no city on record nearby")
              + (f", at {show_point(s['lat'], s['lon'])}" if a.show_coords else ""))
    print("name one: media-intake places add NAME --suggestion N (coordinates stay out of the terminal; "
          "--show-coords prints them)")


def point_from(a, intake, store):
    if a.at:
        return parse_point(a.at)
    if a.suggestion is not None:
        got = suggestions(intake, store, a.min)
        if not 1 <= a.suggestion <= min(20, len(got)):
            raise ValueError(f"no suggestion {a.suggestion}: media-intake places suggest lists "
                             f"{min(20, len(got))}")
        s = got[a.suggestion - 1]
        return s["lat"], s["lon"]
    import subprocess
    f = os.path.abspath(a.from_file)
    rp = os.path.realpath(f)
    if not os.path.isfile(f) or os.path.islink(f) or f.lower().endswith(".xmp") \
            or not any(rp.startswith(os.path.realpath(r) + os.sep) for r in intake.LIB_ROOTS):
        raise ValueError("--from wants a photo or video in the library")
    if intake.guard_label().refused([f], intake.STATE):
        raise ValueError("--from: that file is flagged or quarantined; pick another")
    got = None
    if os.path.isfile(intake.sidecar(f)):  # tag wrote the GPS there (possibly a cloud export's)
        r = subprocess.run(["exiftool", "-j", "-n", "-q", "-q", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude",
                            intake.sidecar(f)], capture_output=True, text=True)
        row = (json.loads(r.stdout or "[]") or [{}])[0]
        got = gps_of(row.get("GPSLatitude"), row.get("GPSLongitude"))
    if got is None:
        got = intake.read_gps([f]).get(f)
    if got is None:
        raise ValueError("--from: that file has no GPS")
    return check_point(*got)


def cmd_add(a, intake, store):
    pt = point_from(a, intake, store)
    pid = store.add(a.name, *pt, a.radius)
    store.save()
    p = store.places[pid]
    rows = [w for w in library_rows(intake) if w["gps"] and inside(p, *w["gps"])]
    print(f"added {p['name']!r}: radius {p['radius']} m, {say_count(len(rows), 'file')} with GPS inside it. "
          "Tag them: media-intake places tag")


def reachable(intake, store, pid, a):
    """The library rows of the files tagged with pid. Refused (ValueError) when some of them can't be read now
    (a drive not plugged in, an unreadable sidecar) unless --anyway: their tag would be left behind with no
    record to clean it up later. Records of files gone from a library folder that is there are dropped."""
    rows = {w["path"]: w for w in library_rows(intake) if w["path"] in store.tagged[pid]}
    store.forget(store.stale(intake.LIB_ROOTS) & store.tagged[pid])
    away = store.tagged[pid] - set(rows)
    if away and not a.anyway:
        raise ValueError(f"{say_count(len(away), 'file')} tagged with it can't be read now (a drive not plugged "
                         "in?); plug it in and run this again, or add --anyway to leave their tag as it is")
    return rows


def cmd_rename(a, intake, store):
    pid = store.need(a.name)
    old = store.places[pid]["name"]
    new = store.rename(pid, a.new)
    if new == old:
        print("that is already its name")
        return
    rows = reachable(intake, store, pid, a)
    changes = []
    for path, w in rows.items():
        if tag_of(old) in w["tags"]:
            changes.append((w, [tag_of(new)], [tag_of(old)]))
        else:  # you took it off: stays off, under the new name too
            store.tagged[pid].discard(path)
            store.declined[pid].add(path)
    n = write(intake, changes)
    store.places[pid]["name"] = new
    store.save()
    print(f"renamed {old!r} to {new!r}: {say_count(n, 'sidecar')} rewritten")


def cmd_remove(a, intake, store):
    pid = store.need(a.name)
    name = store.places[pid]["name"]
    rows = [w for w in reachable(intake, store, pid, a).values() if tag_of(name) in w["tags"]]
    n = write(intake, [(w, [], [tag_of(name)]) for w in rows])
    del store.places[pid]
    store.tagged.pop(pid, None)
    store.declined.pop(pid, None)
    store.save()
    print(f"removed {name!r}: its tag taken off {say_count(n, 'file')} (their city tags stay)")


def cmd_tag(a, intake, store):
    """Bring every library sidecar in step with the places: add, take off what moved out, keep your removals."""
    rows = library_rows(intake)
    stale = store.stale(intake.LIB_ROOTS)
    changes, c = [], collections.Counter()
    for w in rows:
        add, remove = store.plan(w["path"], w["gps"], w["tags"])
        if add or remove:
            changes.append((w, add, remove))
        c["added"] += len(add)
        c["taken off"] += len(remove)
    c["declined"] = sum(len(v) for v in store.declined.values())
    if a.dry_run:
        print(f"would add {say_count(c['added'], 'place tag')} and take off {c['taken off']:,} "
              f"(on {say_count(len(changes), 'file')}); {c['declined']:,} you removed stay off; nothing written")
        return
    store.forget(stale)
    n = write(intake, changes)
    store.save()
    print(f"place tags: {c['added']:,} added, {c['taken off']:,} taken off, on {say_count(n, 'file')}; "
          f"{c['declined']:,} you removed stay off"
          + (f"; {say_count(len(stale), 'record')} of files no longer in the library dropped" if stale else ""))


def parser(prog="media-intake places"):
    ap = argparse.ArgumentParser(
        prog=prog, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Your own named places (Home, ...): Place|<name> tags on files whose GPS is inside a\n"
                    "circle you set, beside the city tag. Definitions live only in the state folder\n"
                    "(places.json, 0600); output is counts, never coordinates unless --show-coords.\n\n"
                    "Touches: <state>/places.json; add, rename, remove and tag write sidecars (never\n"
                    "  the photos).\n"
                    "Deletes: nothing (remove takes a place's tag off its files; the files stay).")
    sp = ap.add_subparsers(dest="pcmd", required=True, metavar="<list|add|rename|remove|suggest|tag>")
    s = sp.add_parser("list", help="your places, with how many files each has")
    s.add_argument("--show-coords", action="store_true", help="also print each place's point")
    s = sp.add_parser("add", help="name a new place")
    s.add_argument("name", help="the place's name, e.g. Home (no | or ,)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--at", metavar="LAT,LON", help="its point, in decimal degrees")
    g.add_argument("--from", dest="from_file", metavar="FILE", help="its point from this library photo's GPS")
    g.add_argument("--suggestion", type=int, metavar="N", help="the Nth spot `places suggest` lists")
    s.add_argument("--radius", type=float, default=RADIUS_DEFAULT,
                   help=f"metres around the point ({RADIUS_MIN}-{RADIUS_MAX}, default {RADIUS_DEFAULT})")
    s.add_argument("--min", type=int, default=SUGGEST_MIN, help=argparse.SUPPRESS)
    s = sp.add_parser("rename", help="give a place a new name (its tags follow)")
    s.add_argument("name", help="the place's name now")
    s.add_argument("new", help="its new name")
    s.add_argument("--anyway", action="store_true", help="go ahead when some of its files can't be read now "
                                                          "(they keep the old name)")
    s = sp.add_parser("remove", help="forget a place and take its tag off its files")
    s.add_argument("name", help="the place's name")
    s.add_argument("--anyway", action="store_true", help="go ahead when some of its files can't be read now "
                                                          "(they keep its tag)")
    s = sp.add_parser("suggest", help="spots with many photos, to name (coordinates only with --show-coords)")
    s.add_argument("--show-coords", action="store_true", help="also print each spot's point")
    s.add_argument("--min", type=int, default=SUGGEST_MIN,
                   help=f"files within {SUGGEST_RADIUS} m to make a spot (default {SUGGEST_MIN})")
    s = sp.add_parser("tag", help="tag every library file with the places it is in")
    s.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    import intake
    import library_lock
    run = {"list": cmd_list, "add": cmd_add, "rename": cmd_rename, "remove": cmd_remove,
           "suggest": cmd_suggest, "tag": cmd_tag}[a.pcmd]
    if a.pcmd not in ("list",):
        intake.need_exiftool()
    try:
        with library_lock.hold(f"places {a.pcmd}", wait=library_lock.cli_wait(), out=sys.stderr):
            store = Store(intake.STATE, intake.LIB_ROOTS)
            run(a, intake, store)
    except library_lock.Busy as e:
        print(f"media-intake places {a.pcmd}: refused: {e}", file=sys.stderr)
        return library_lock.EX_TEMPFAIL
    except (ValueError, Unreadable) as e:
        print(f"media-intake places {a.pcmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
