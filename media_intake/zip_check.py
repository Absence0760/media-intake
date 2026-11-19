#!/usr/bin/env python3
"""`media-intake zip-check ROOT [--known DIR]... [--out JSON]`: what the zips under a folder hold that this
machine doesn't have yet (a Google Takeout or any other export), before you import any of it.

Every member of every .zip under ROOT is classified by sha256:

  library     already in the library (index.json: the library, videos and project folders)
  known       a file under a --known DIR (a folder outside the library you also keep, e.g. documents)
  trashed     content you trashed before (trashed.json): it would come back
  erased      content on the erase log: it must not come back
  json        a metadata file (Takeout writes one per photo)
  dir         a folder entry
  new-media / new-doc / new-other   none of the above, by extension
  unreadable member / unreadable zip

Read-only: members are streamed and hashed, never extracted; ROOT and the zips are never written. Output
is counts only (per top-level folder of ROOT, then a total with sizes), never a member name. --out writes
{zip path relative to ROOT: {member name: class}} at mode 0600 for a later import step; it names files,
so keep it out of shared folders. The sha256 of --known files is cached in <state>/zipcheck/sha-cache.json
(dir 0700, file 0600), keyed by path, size and mtime, so a second run hashes only what changed.
"""
import argparse, collections, hashlib, json, os, re, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import intake  # noqa: E402
import sources  # noqa: E402

# What an export holds beyond what intake takes in (scans, older camera and phone formats).
MEDIA = sources.MEDIA_EXT | {".m4v", ".avi", ".mts", ".tif", ".tiff", ".bmp"}
DOCS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".txt", ".rtf", ".csv"}
CHUNK = 1 << 20


def cache_path():
    return os.path.join(intake.STATE, "zipcheck", "sha-cache.json")


def shas_under(root, cache):
    """sha256 of every regular file under root (symlinks skipped); cache is {path\\0size\\0mtime: sha}."""
    out = set()
    for r, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            st = os.stat(p)
            k = f"{p}\0{st.st_size}\0{st.st_mtime}"
            if k not in cache:
                cache[k] = intake.sha256(p)
            out.add(cache[k])
    return out


def erased_shas():
    """Every sha256 on the erase log, with or without a size (older lines have none)."""
    out = set()
    try:
        with open(os.path.join(intake.STATE, "erase-log.txt")) as f:
            for line in f:
                m = re.search(r"^\S+ \S+ \S+ sha256:([0-9a-f]{64})", line)
                if m:
                    out.add(m.group(1))
    except FileNotFoundError:
        pass
    return out


def known_shas(known_dirs):
    """{sha256: class} of what this machine has: the library index, then each --known folder, then trash."""
    known = {}
    for v in (intake.load(os.path.join(intake.STATE, "index.json"), {}) or {}).values():
        if isinstance(v, dict) and v.get("sha"):
            known.setdefault(v["sha"], "library")
    if known_dirs:
        cache = intake.load(cache_path(), {}) or {}
        for d in known_dirs:
            for s in shas_under(d, cache):
                known.setdefault(s, "known")
        write_private(cache_path(), cache)
    for s in (intake.load(intake.TRASHED, {}) or {}):
        known.setdefault(s, "trashed")
    return known


def write_private(p, obj):
    """JSON at mode 0600 (created that way), its folder 0700 when it is under the state folder."""
    d = os.path.dirname(os.path.abspath(p))
    if d.startswith(os.path.abspath(intake.STATE) + os.sep):
        intake.private_dir(d)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def member_class(zf, m, known, erased):
    ext = os.path.splitext(m.filename)[1].lower()
    if m.is_dir():
        return "dir"
    if ext == ".json":
        return "json"
    h = hashlib.sha256()
    try:
        with zf.open(m) as f:
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
    except Exception:  # bad CRC, an encrypted member, an unsupported compression method
        return "unreadable member"
    s = h.hexdigest()
    if s in erased:
        return "erased"
    return known.get(s) or ("new-media" if ext in MEDIA else "new-doc" if ext in DOCS else "new-other")


def check(root, known_dirs=(), out=print):
    """Classify every zip member under root. Returns (total Counter, bytes Counter, detail dict)."""
    known, erased = known_shas(known_dirs), erased_shas()
    total, nbytes, by_group, detail = collections.Counter(), collections.Counter(), {}, {}
    zips = sorted(os.path.join(r, f) for r, _, fs in os.walk(root) for f in fs
                  if f.lower().endswith(".zip") and not os.path.islink(os.path.join(r, f)))
    for i, z in enumerate(zips, 1):
        rel = os.path.relpath(z, root)
        group = by_group.setdefault(rel.split(os.sep)[0] if os.sep in rel else ".", collections.Counter())
        detail[rel] = {}
        try:
            zf = zipfile.ZipFile(z)
        except (zipfile.BadZipFile, OSError):
            group["unreadable zip"] += 1
            total["unreadable zip"] += 1
            continue
        with zf:
            for m in zf.infolist():
                cls = member_class(zf, m, known, erased)
                group[cls] += 1
                total[cls] += 1
                nbytes[cls] += m.file_size
                detail[rel][m.filename] = cls
        out(f"[{i}/{len(zips)}] checked")
    for g in sorted(by_group):
        out(f"{g}: " + ", ".join(f"{k} {v}" for k, v in sorted(by_group[g].items())))
    out("TOTAL: " + (", ".join(f"{k} {v} ({nbytes[k] / 1e9:.1f} GB)" for k, v in sorted(total.items()))
                     or f"no zip files under {root}"))
    return total, nbytes, detail


def build_parser(prog="media-intake zip-check"):
    ap = argparse.ArgumentParser(prog=prog, description="Classify every file inside the zips under ROOT by "
                                 "sha256 against what this machine has. Read-only; prints counts only.")
    ap.add_argument("root", help="the folder holding the zips (searched recursively)")
    ap.add_argument("--known", action="append", default=[], metavar="DIR",
                    help="a folder outside the library whose files count as known (repeatable)")
    ap.add_argument("--out", metavar="JSON", help="write {zip: {member: class}} here (mode 0600; names files)")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        sys.exit(f"not a folder: {a.root}")
    for d in a.known:
        if not os.path.isdir(d):
            sys.exit(f"--known {d}: not a folder")
    if a.out and os.path.abspath(a.out).startswith(root + os.sep):
        sys.exit(f"--out {a.out}: inside ROOT; zip-check never writes there")
    _, _, detail = check(root, [os.path.abspath(d) for d in a.known], out=lambda s: print(s, flush=True))
    if a.out:
        write_private(a.out, detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
