#!/usr/bin/env python3
"""Offline reverse geocoding: GPS -> nearest GeoNames place within MAX_KM.

  venv/bin/python -I media_intake/geocode.py [GEO_DIR] < [[lat, lon], ...] > [place or null, ...]

GeoNames cities1000 (CC-BY 4.0, credit GeoNames, https://www.geonames.org/),
admin1CodesASCII.txt and countryInfo.txt, downloaded once by setup.sh and
checked here against <state>/geo-pins.txt (recorded by setup.sh on first download); never fetched at tag time.
Points are 3D unit vectors in a scipy cKDTree, so the nearest neighbour is the
nearest on the sphere. Neighbourhood, historical and abandoned places are skipped.
"""
import hashlib, io, json, math, os, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

STATE = config.get()["state"]
MAX_KM = 50.0
R_KM = 6371.0
SKIP = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}


def pins(path=os.path.join(STATE, "geo-pins.txt")):
    with open(path) as f:
        return {p[1]: p[0] for p in (l.split() for l in f if l.strip() and not l.startswith("#"))}


def verified(geo, name, pin):
    p = os.path.join(geo, name)
    with open(p, "rb") as f:
        data = f.read()
    if pin is not None and hashlib.sha256(data).hexdigest() != pin:
        sys.exit(f"geocode: {p} does not match geo-pins.txt; run setup.sh (--refresh-geo to accept a new snapshot)")
    return data


def load(geo, check=True):
    pin = pins() if check else {}
    raw = verified(geo, "cities1000.zip", pin.get("cities1000.zip") if check else None)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    text = zf.read("cities1000.txt").decode("utf-8")
    admin = {}
    for l in verified(geo, "admin1CodesASCII.txt", pin.get("admin1CodesASCII.txt") if check else None).decode().splitlines():
        c = l.split("\t")
        if len(c) > 1:
            admin[c[0]] = c[1]
    country = {}
    for l in verified(geo, "countryInfo.txt", pin.get("countryInfo.txt") if check else None).decode().splitlines():
        c = l.split("\t")
        if not l.startswith("#") and len(c) > 4:
            country[c[0]] = c[4]
    rows, xyz = [], []
    for l in text.splitlines():
        c = l.split("\t")
        if len(c) < 11 or c[7] in SKIP:
            continue
        lat, lon = math.radians(float(c[4])), math.radians(float(c[5]))
        xyz.append((math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)))
        rows.append(dict(city=c[1], state=admin.get(f"{c[8]}.{c[10]}", ""), cc=c[8], country=country.get(c[8], c[8])))
    from scipy.spatial import cKDTree
    return cKDTree(xyz), rows


def lookup(tree, rows, points):
    out = []
    for lat, lon in points:
        la, lo = math.radians(lat), math.radians(lon)
        d, i = tree.query((math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)))
        km = 2 * R_KM * math.asin(min(1.0, d / 2))  # chord length -> great-circle distance
        out.append(dict(rows[i], km=round(km, 1)) if km <= MAX_KM else None)
    return out


def main():
    geo = sys.argv[1] if len(sys.argv) > 1 else os.path.join(STATE, "geo")
    tree, rows = load(geo, check=os.environ.get("MEDIA_INTAKE_GEO_NOPIN") != "1")  # tests: fixture files
    json.dump(lookup(tree, rows, json.load(sys.stdin)), sys.stdout)


if __name__ == "__main__":
    main()
