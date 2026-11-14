#!/usr/bin/env python3
"""Burst groups: consecutive shots from one device a moment apart, shown as one stack.

  python3 -I media_intake/bursts.py RECORDS.json     (prints the groups as JSON: ids/paths and counts)

Pure functions, stdlib only. Input is item records, dicts with:

  path      the file (only its name is read, for a burst marker; the file is never opened)
  datetime  a datetime or an ISO 8601 string (naive local wall time, as plan.json's `date`)
  device    a device id (plan.json's `device`), or None when unknown
  dhash     the 64-bit dHash as an int (plan.json / index.json), or None
  burst_id  optional: a camera's own burst id (EXIF/XMP BurstUUID), when the caller has one

Any other keys (an `id`, say) are carried through untouched. Output is a list of groups, each a list
of at least two records in time order; a shot that belongs to no burst is in no group.

A burst is:
- the same device (an unknown device groups only with other unknown ones), images only;
- each shot within GAP of the one before it (a chain: a 30-frame burst can span several seconds);
- when both shots have a dHash, at most DHASH_MAX bits apart (a near-identical frame), else no burst;
- a camera's own marker wins over the clock: two shots with the same marker (burst_id, or one read
  from the file name by MARKERS) are one burst whatever the gap, and two different markers are never
  one burst. A shot with a marker next to one without falls back to the gap and dHash rule.
"""
import datetime, json, os, re, sys

# Phones fire burst frames 3-30 per second and a hand-held "spray" a few per second; 2 s between
# neighbours keeps those together while two deliberate shots of the same scene (usually 3 s or more
# apart, after a look at the screen) stay separate. Inclusive: exactly GAP apart is still one burst.
GAP = datetime.timedelta(seconds=2)
# dHash bits (of 64) between neighbouring frames. intake.py calls 2 bits a near-duplicate and 4
# (NEAR_MAX) a recompressed copy; burst frames move a little between shots, so allow more, but well
# under the ~32 bits two unrelated pictures differ by.
DHASH_MAX = 10
# Videos are never bursts (a burst is stills); everything else is taken as an image.
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".3gp", ".mkv", ".webm", ".avi", ".mts", ".m2ts"}
# Burst markers in file names. Each pattern's group 1 is the burst key: shots sharing it are one burst.
MARKERS = (
    # Pixel / Android camera: 00001IMG_00001_BURST20261108071503123[_COVER].jpg: the burst's start time.
    re.compile(r"_BURST(\d{14})\d*(?:_COVER)?\.[^.]+$", re.I),
    # Huawei / Honor and others: IMG_20261108_071503_BURST001[_COVER].jpg, IMG_1234_BURST002.JPG.
    re.compile(r"^(.+?)_BURST\d+(?:_COVER)?\.[^.]+$", re.I),
)


def parse_dt(v):
    """A naive datetime from a datetime or an ISO string, else None. An aware time keeps its wall time,
    as plan.json's dates are wall times too."""
    if isinstance(v, datetime.datetime):
        dt = v
    elif isinstance(v, str) and v:
        try:
            dt = datetime.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=None)


def marker(rec):
    """The burst key a camera wrote: the record's burst_id, else one from its file name, else None."""
    b = rec.get("burst_id")
    if isinstance(b, str) and b.strip():
        return "id:" + b.strip().lower()
    name = os.path.basename(str(rec.get("path") or ""))
    for rx in MARKERS:
        m = rx.search(name)
        if m:
            return "name:" + m.group(1).lower()
    return None


def is_image(rec):
    return os.path.splitext(str(rec.get("path") or ""))[1].lower() not in VIDEO_EXT


def bits(a, b):
    return bin(a ^ b).count("1")


def same_burst(prev, cur, gap=GAP, dhash_max=DHASH_MAX):
    """Whether cur continues the burst prev is in (both from one device, cur not earlier)."""
    mp, mc = prev["_marker"], cur["_marker"]
    if mp and mc:
        return mp == mc
    if cur["_dt"] - prev["_dt"] > gap:
        return False
    dp, dc = prev.get("dhash"), cur.get("dhash")
    if isinstance(dp, int) and isinstance(dc, int) and bits(dp, dc) > dhash_max:
        return False
    return True


def groups(records, gap=GAP, dhash_max=DHASH_MAX):
    """Burst groups of records: a list of lists (two or more records each, in time order), ordered by
    each group's first shot. Records without a usable date, and videos, are never grouped."""
    by_dev = {}
    for n, r in enumerate(records):
        dt = parse_dt(r.get("datetime"))
        if dt is None or not is_image(r):
            continue
        dev = r.get("device") if isinstance(r.get("device"), str) and r.get("device") else None
        by_dev.setdefault(dev, []).append((dt, str(r.get("path") or ""), n, r))
    out = []
    for rows in by_dev.values():
        rows.sort(key=lambda x: (x[0], x[1], x[2]))
        cur = []
        prev = None
        for dt, _, n, r in rows:
            w = dict(_dt=dt, _marker=marker(r), _n=n, dhash=r.get("dhash"))
            if prev is not None and same_burst(prev, w, gap, dhash_max):
                cur.append((w, r))
            else:
                if len(cur) > 1:
                    out.append(cur)
                cur = [(w, r)]
            prev = w
        if len(cur) > 1:
            out.append(cur)
    out.sort(key=lambda g: (g[0][0]["_dt"], g[0][0]["_n"]))
    return [[r for _, r in g] for g in out]


def index(records, gap=GAP, dhash_max=DHASH_MAX):
    """{position in records: group number} for every record in a burst (same groups as groups())."""
    pos = {id(r): n for n, r in enumerate(records)}
    return {pos[id(r)]: g for g, members in enumerate(groups(records, gap, dhash_max)) for r in members}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit("usage: bursts.py RECORDS.json  (a JSON list of {path, datetime, device, dhash})")
    with open(argv[0]) as f:
        recs = json.load(f)
    if not isinstance(recs, list) or not all(isinstance(r, dict) for r in recs):
        sys.exit("RECORDS.json must be a JSON list of objects")
    gs = groups(recs)
    print(json.dumps([{"count": len(g), "members": [r.get("id", r.get("path")) for r in g]} for g in gs], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
