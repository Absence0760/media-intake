#!/usr/bin/env python3
"""`media-intake prune-cache [--dry-run]`: delete the gallery's cached copies nothing shows any more.

  media-intake prune-cache --dry-run    counts and sizes only; deletes nothing
  media-intake prune-cache              delete them (under the library lock)
  python3 -I media_intake/cache_prune.py [--dry-run]   (same)

The gallery caches a thumbnail per content and turn (intake.gallery_thumb_path: <sha256>-r<turn>.jpg) and
an H.264 proxy per HEVC video (<sha256>.mp4). Rotating makes a new turn and keeps the old one, and content
that leaves the library some other way than trash / forget / erase keeps its files. This deletes:

  - <sha256>.jpg, the content-only thumbnail older versions wrote (never served now);
  - <sha256>-r<turn>.jpg when no library copy of that content shows that turn;
  - every thumbnail and proxy of content no longer in the library.

"In the library" is what the gallery lists (gallery.scan_library: the library roots from config.toml, never
the state folder or a flagged file), each file's sha256 from index.json when its size matches, else hashed
here. When a file can't be read, or a library folder isn't there (a project folder on an unplugged drive),
what the library holds is unknown, so only the legacy thumbnails go and the summary says why. Only names the gallery writes are touched, only regular files (a symlink is never followed or removed),
and only inside the cache folders, opened without following a symlink; originals and sidecars are never
written. It is a cache: anything deleted is made again on the next view. Output is counts, never paths.
"""
import argparse, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gallery  # noqa: E402  (scan_library, thumb_turn: what the gallery shows, and under which key)
import intake  # noqa: E402  (THUMBS, PROXIES, sha256)
import library_lock  # noqa: E402

THUMB_NAME = re.compile(r"([0-9a-f]{64})(?:-r(0|90|180|270))?\.jpg")
PROXY_NAME = re.compile(r"([0-9a-f]{64})\.mp4")
LEGACY, TURN, GONE_THUMB, GONE_PROXY = ("legacy thumbnails (<sha256>.jpg)", "thumbnails of a turn no copy shows",
                                        "thumbnails of content not in the library",
                                        "video proxies of content not in the library")


def in_use(items):
    """({sha256: {turns shown}} over the gallery's items, how many files could not be read): an unreadable
    file's content is unknown, so it may be a copy of anything."""
    used, unread = {}, 0
    for it in items:
        try:
            sha = it.get("sha") or intake.sha256(it["path"])
            turn = gallery.thumb_turn(it)
        except OSError:
            unread += 1
            continue
        used.setdefault(sha, set()).add(turn)
    return used, unread


def thumb_reason(name, used, certain):
    """Why this thumbnail file goes, or None to keep it (and any name the gallery doesn't write)."""
    m = THUMB_NAME.fullmatch(name)
    if not m:
        return None
    sha, turn = m.group(1), m.group(2)
    if turn is None:
        return LEGACY
    if not certain:
        return None
    if sha not in used:
        return GONE_THUMB
    return None if int(turn) in used[sha] else TURN


def proxy_reason(name, used, certain):
    m = PROXY_NAME.fullmatch(name)
    if not m or not certain or m.group(1) in used:
        return None
    return GONE_PROXY


def open_cache_dir(path):
    """A descriptor on a cache folder, or None when it is absent or not a real folder. Neither it nor the
    media-intake folder above it may be a symlink: nothing outside the cache is ever listed or deleted."""
    parent = os.path.dirname(path)
    try:
        if os.path.islink(parent) or not os.path.isdir(parent):
            return None
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None


def sweep(folder, reason, used, certain, dry_run, counts):
    """Delete (or with dry_run, count) each file in folder that reason() names. Returns (bytes, not regular)."""
    fd = open_cache_dir(folder)
    if fd is None:
        return 0, 0
    freed = odd = 0
    try:
        with os.scandir(fd) as entries:
            for e in entries:
                why, size = reason(e.name, used, certain), 0
                if why is None:
                    continue
                try:
                    if not e.is_file(follow_symlinks=False):
                        odd += 1  # a symlink or folder under a cache name: not the tool's, left alone
                        continue
                    size = e.stat(follow_symlinks=False).st_size
                    if not dry_run:
                        os.unlink(e.name, dir_fd=fd)
                except FileNotFoundError:
                    continue  # gone meanwhile (a trash, another prune)
                counts[why] += 1
                freed += size
    finally:
        os.close(fd)
    return freed, odd


def prune(dry_run=False, out=print):
    """Prune the gallery cache; returns {reason: count}."""
    items, _ = gallery.scan_library()
    used, unread = in_use(items)
    # A root that isn't there (a project folder on an unplugged drive) may hold anything: be as careful.
    missing = [r for r in gallery.ROOTS if not os.path.isdir(r)]
    certain = not unread and not missing
    counts = dict.fromkeys((LEGACY, TURN, GONE_THUMB, GONE_PROXY), 0)
    freed = odd = 0
    for folder, reason in ((intake.THUMBS, thumb_reason), (intake.PROXIES, proxy_reason)):
        b, o = sweep(folder, reason, used, certain, dry_run, counts)
        freed, odd = freed + b, odd + o
    verb = "would delete" if dry_run else "deleted"
    for why, n in counts.items():
        if n:
            out(f"{verb} {n} {why}")
    out(f"{verb} {sum(counts.values())} files ({freed / 1e6:.1f} MB) from {os.path.dirname(intake.THUMBS)}; "
        f"{len(items)} library files, {len(used)} distinct contents kept")
    if odd:
        out(f"left {odd} entries that are not regular files (a symlink or folder under a cache name)")
    for r in missing:
        out(f"library folder not found: {r}")
    if unread:
        out(f"{unread} library files could not be read")
    if not certain:
        out("so what the library holds is unknown: only legacy thumbnails were pruned; "
            "run it again once every library folder is there and readable")
    return counts


def main(argv=None, prog="media-intake prune-cache"):
    ap = argparse.ArgumentParser(prog=prog, description="Delete the gallery's cached thumbnails and video "
                                 "proxies that nothing in the library shows any more (docs/gallery.md).")
    ap.add_argument("--dry-run", action="store_true", help="count and size them, delete nothing")
    a = ap.parse_args(argv)
    if a.dry_run:
        prune(True)
        return 0
    try:  # a consistent view of the library while it deletes: no apply, trash or erase in between
        with library_lock.hold("prune-cache", wait=library_lock.cli_wait(), out=sys.stderr):
            prune(False)
    except library_lock.Busy as e:
        print(f"{prog}: refused: {e}", file=sys.stderr)
        return library_lock.EX_TEMPFAIL
    return 0


if __name__ == "__main__":
    sys.exit(main())
