#!/usr/bin/env python3
"""`media-intake backup MOUNT [--verify] [--dry-run] [--skip PART]...`: copy the library to a mounted drive.

  <MOUNT>/media-intake-backup/library/...          the photo library   (config `library`)
  <MOUNT>/media-intake-backup/videos/...           personal footage    (config `videos`)
  <MOUNT>/media-intake-backup/projects/<key>/...   each project folder (config `projects`)
  <MOUNT>/.media-intake-backup                     marker: stage --kind drive and queue add refuse this drive
  <state>/backup/<drive id>.json                   what each run copied (size, mtime, sha256), laptop only

What is copied: every media file (sources.MEDIA_EXT) under the library roots and its `<file>.<ext>.xmp`
sidecar. Never: the state folder (quarantine, staging, review copies, index.json, jobs), the thumbnail and
proxy caches, hidden files and folders (digiKam's .dtrash, .thumbnails), symlinks, orphaned sidecars or
anything else in the library folders.

Rules (docs/backup.md):
  - MOUNT must be a mount point of a drive that is not the laptop's own disk (findmnt, sources.drive_mount),
    so an unmounted drive's empty mount folder is refused; nor inside the library, the state folder or the
    caches, nor holding a library root.
  - Incremental: a file whose size and sha256 already match at the destination is not copied again. The sha
    is taken from the manifest when the source's and the copy's size and mtime still match what it recorded,
    and computed otherwise.
  - Atomic per file: written to a hidden temporary name in the destination folder, fsync'd, then renamed
    over the final name; an interrupted run never leaves a half-written file under a real name.
  - Nothing on the drive is deleted: a file gone from the library stays on the backup (erase --drive removes
    erased content by checksum). A changed library file replaces its own older copy.
  - Reader of the library: no library lock (a backup can take hours); a file that changes or vanishes while it
    is copied is skipped and counted, and the next run picks it up.
  - --skip PART leaves one library root out of this run (`library`, `videos` or `projects/<key>`; e.g. the
    drive has no room for it yet): it is neither copied nor verified, and its copy on the drive and its
    manifest entries stay as they were.
  - Output is counts and paths only, never file contents.
"""
import argparse, hashlib, os, shutil, stat, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import intake  # noqa: E402
import sources  # noqa: E402

DEST_DIR = "media-intake-backup"
TMP_SUFFIX = ".media-intake-partial"
CHUNK = 1 << 20
SKIP_NAMES = {"node_modules", "__pycache__", "lost+found"}
MANIFEST_VERSION = 1


class Refused(Exception):
    pass


class ChangedError(OSError):
    """The source file changed while it was being copied."""


def _within(p, base):
    p, base = os.path.realpath(p), os.path.realpath(base)
    return p == base or p.startswith(base.rstrip(os.sep) + os.sep)


def roots(cfg=None):
    """[(source folder, destination subfolder)] for every library root."""
    cfg = cfg or config.get()
    out = [(cfg["library"], "library"), (cfg["videos"], "videos")]
    out += [(p["folder"], f"projects/{key}") for key, p in sorted(cfg["projects"].items())]
    return out


def check_dest(mount, cfg=None):
    """The backup folder under mount, or raise Refused (not mounted, laptop disk, inside the library ...)."""
    cfg = cfg or config.get()
    mount = os.path.abspath(mount)
    if os.path.islink(mount) or not os.path.isdir(mount):
        raise Refused(f"not a folder (or a symlink): {mount}")
    target = sources.drive_mount(mount)
    if not target:
        raise Refused(f"cannot tell which drive holds {mount} (findmnt found nothing)")
    if os.path.realpath(target) != os.path.realpath(mount):
        raise Refused(f"{mount} is not a mount point (its files are on {target}): is the drive mounted? "
                      "Pass the drive's mount point, e.g. /run/media/$USER/<label>")
    if "MEDIA_INTAKE_TEST_MOUNT" not in os.environ and sources.LAPTOP_MOUNTS.match(target):
        raise Refused(f"{mount} is on the laptop's own disk ({target}), not an external drive")
    caches = (intake.THUMBS, intake.PROXIES, os.path.dirname(intake.THUMBS))
    for what, base in [("the state folder", cfg["state"])] + [("a cache folder", c) for c in caches] \
            + [(f"the library ({src})", src) for src, _ in roots(cfg)]:
        if _within(mount, base):
            raise Refused(f"{mount} is inside {what}: back up to a separate drive")
    for src, _ in roots(cfg):
        if _within(src, mount):
            raise Refused(f"the library folder {src} is on {mount} itself: back up to a separate drive")
    if os.path.isfile(os.path.join(mount, sources.SOURCE_OK_MARKER)):
        raise Refused(f"{mount} has {sources.SOURCE_OK_MARKER} (you marked it as not your backup drive); "
                      "remove that file to back up to it")
    dest = os.path.join(mount, DEST_DIR)
    if os.path.islink(dest) or (os.path.lexists(dest) and not os.path.isdir(dest)):
        raise Refused(f"{dest} is not a plain folder")
    return dest


def part_names(cfg=None):
    """What --skip takes: each root's destination subfolder (library, videos, projects/<key>)."""
    return [sub for _, sub in roots(cfg)]


def _skipped(rel, skip):
    return any(rel == s or rel.startswith(s + "/") for s in skip)


def walk_library(cfg=None, skip=()):
    """Yield (source path, destination relative path) for each media file and its sidecar, roots in order.
    A root nested in another is walked once, under its own name; the state folder is never entered.
    Roots whose subfolder is in skip are left out (their folders are still never entered from another root)."""
    cfg = cfg or config.get()
    rs = roots(cfg)
    real_roots = {os.path.realpath(s) for s, _ in rs}
    never = {os.path.realpath(p) for p in (cfg["state"], intake.THUMBS, intake.PROXIES)}
    for src, sub in rs:
        if sub in skip or not os.path.isdir(src) or os.path.islink(src):
            continue
        top = os.path.realpath(src)
        for r, dirs, files in os.walk(src, followlinks=False):
            keep = []
            for d in sorted(dirs):
                p = os.path.realpath(os.path.join(r, d))
                if d.startswith(".") or d in SKIP_NAMES or p in never or (p in real_roots and p != top) \
                        or os.path.islink(os.path.join(r, d)):
                    continue
                keep.append(d)
            dirs[:] = keep
            names = set(files)
            for f in sorted(files):
                if f.startswith("."):
                    continue
                p = os.path.join(r, f)
                try:
                    if not stat.S_ISREG(os.lstat(p).st_mode):
                        continue
                except OSError:
                    continue
                ext = os.path.splitext(f)[1].lower()
                media = ext in sources.MEDIA_EXT
                side = ext == ".xmp" and os.path.splitext(f[:-4])[1].lower() in sources.MEDIA_EXT \
                    and f[:-4] in names
                if media or side:
                    yield p, os.path.join(sub, os.path.relpath(p, src))


def drive_id(mount):
    """Which manifest a drive uses: its filesystem UUID, else the mount's real path (hashed)."""
    key = sources.drive_uuid(mount) or os.path.realpath(mount)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def manifest_path(mount, cfg=None):
    return os.path.join((cfg or config.get())["state"], "backup", f"{drive_id(mount)}.json")


def load_manifest(path, strict=False):
    """The record's files ({} when there is none yet). A record that can't be read, isn't JSON or isn't this
    version's also gives {} (every file is compared by content again, and the run rewrites it), or None with
    strict, so a caller that must know (erase's prune) can tell it from an empty one."""
    try:
        m = intake.load(path)
    except (OSError, ValueError):
        return None if strict else {}
    if m is None:
        return {}  # no record yet
    if not isinstance(m, dict) or m.get("version") != MANIFEST_VERSION or not isinstance(m.get("files"), dict):
        return None if strict else {}
    return m["files"]


def save_manifest(path, files):
    intake.private_dir(os.path.dirname(path))
    intake.save(path, dict(version=MANIFEST_VERSION, files=files))
    os.chmod(path, 0o600)


def _stat(p):
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # exFAT and FUSE mounts may refuse a directory fsync; the file's own fsync already ran
    finally:
        os.close(fd)


def _safe_dir(dest, rel_dir):
    """Create dest/rel_dir one folder at a time (0700), refusing any part that is a symlink or not a folder:
    nothing on the drive may send a write elsewhere (onto the laptop, say)."""
    d = dest
    for part in [""] + (rel_dir.split(os.sep) if rel_dir else []):
        d = os.path.join(d, part) if part else d
        if os.path.islink(d) or (os.path.lexists(d) and not os.path.isdir(d)):
            raise OSError(f"not a plain folder on the drive: {os.path.relpath(d, dest)}")
        if not os.path.lexists(d):
            os.mkdir(d, 0o700)
    return d


def copy_one(src, dest, rel):
    """Copy src to dest/rel atomically. Returns (sha256, size, src stat, dst stat) or raises OSError
    (ChangedError when src changed during the copy)."""
    before = _stat(src)
    if before is None:
        raise FileNotFoundError(src)
    d = _safe_dir(dest, os.path.dirname(rel))
    final = os.path.join(d, os.path.basename(rel))
    if os.path.islink(final) or os.path.isdir(final):
        raise OSError(f"not a plain file at the destination: {rel}")
    tmp = os.path.join(d, "." + os.path.basename(rel) + TMP_SUFFIX)
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fo:
                for b in iter(lambda: fi.read(CHUNK), b""):
                    h.update(b)
                    fo.write(b)
                fo.flush()
                try:
                    os.fsync(fo.fileno())
                except OSError:
                    pass  # some filesystems (exFAT over FUSE) reject fsync; the write itself succeeded
        if _stat(src) != before:
            raise ChangedError(src)
        os.utime(tmp, ns=(before[1], before[1]))
        os.replace(tmp, final)
    except BaseException:
        if os.path.lexists(tmp):
            os.remove(tmp)  # our own partial write, never a user's file
        raise
    _fsync_dir(d)
    return h.hexdigest(), before[0], before, _stat(final)


def _known(entry, src_st, dst_st):
    """The recorded sha if both sides still look as recorded (size, mtime), else None."""
    if not entry or src_st is None or dst_st is None:
        return None
    if list(src_st) == entry.get("src") and list(dst_st) == entry.get("dst"):
        return entry.get("sha")
    return None


def backup(mount, verify=False, dry_run=False, out=print, skip=()):
    """One backup run. Returns the exit code: 0 when everything is on the drive (and verified), else 1."""
    cfg = config.get()
    skip = tuple(dict.fromkeys(skip))
    bad = [s for s in skip if s not in part_names(cfg)]
    if bad:
        out(f"backup: refused: --skip {bad[0]}: not a library root (one of: {', '.join(part_names(cfg))})")
        return 2
    try:
        dest = check_dest(mount, cfg)
    except Refused as e:
        out(f"backup: refused: {e}")
        return 2
    for s in skip:
        out(f"backup: {s}: skipped this run (--skip); its copy on the drive, if any, is left as it was")
    mpath = manifest_path(mount, cfg)
    manifest = load_manifest(mpath)
    todo, same, seen_case, clashes, files = [], 0, set(), [], []
    for src, rel in walk_library(cfg, skip):
        k = sources.case_key(rel)
        if k in seen_case:  # exFAT/FAT ignore case: the two would be one file there
            clashes.append(rel)
            continue
        seen_case.add(k)
        files.append((src, rel))
        src_st, dst_st = _stat(src), _stat(os.path.join(dest, rel))
        if src_st is None:
            continue
        if dst_st is not None and dst_st[0] == src_st[0]:
            sha = _known(manifest.get(rel), src_st, dst_st)
            if sha is None:  # not recorded, or one side changed: compare the content itself
                try:
                    s_sha, d_sha = intake.sha256(src), intake.sha256(os.path.join(dest, rel))
                except OSError:
                    s_sha, d_sha = None, ""
                if s_sha == d_sha:
                    sha = s_sha
                    if not dry_run:
                        manifest[rel] = dict(sha=sha, src=list(src_st), dst=list(dst_st))
            if sha is not None:
                same += 1
                continue
        todo.append((src, rel, src_st[0], dst_st is not None))
    need = sum(size for _, _, size, _ in todo)
    out(f"backup: {len(files) + len(clashes):,} library files (media and sidecars), {same:,} already on the drive, "
        f"{len(todo):,} to copy ({need / 1e6:,.1f} MB)" + (" -- dry run, nothing written" if dry_run else ""))
    ok = True
    if clashes:
        ok = False
        out(f"backup: {len(clashes)} not copied: their path differs from another only by letter case, and the "
            "drive may not tell them apart (rename one in the library):")
        for rel in clashes:
            out(f"  {rel}")
    copied = updated = 0
    changed, failed, vanished = [], [], 0
    if not dry_run and todo:
        free = shutil.disk_usage(mount).free
        if need > free:
            out(f"backup: refused: {need / 1e6:,.1f} MB to copy, {free / 1e6:,.1f} MB free on {mount}"
                + ("" if len(skip) + 1 >= len(part_names(cfg)) else
                   f" (--skip PART backs up the rest first; parts: {', '.join(part_names(cfg))})"))
            return 1
        _safe_dir(dest, "")
        marker = os.path.join(mount, sources.BACKUP_MARKER)
        if not os.path.lexists(marker):  # empty: stage --kind drive and queue add now refuse this drive as a source
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        for src, rel, _, existed in todo:
            try:
                sha, _, src_st, dst_st = copy_one(src, dest, rel)
            except ChangedError:
                changed.append(rel)
                continue
            except OSError as e:
                if not os.path.lexists(src):  # gone from the library since the listing: nothing to back up
                    vanished += 1
                    continue
                failed.append((rel, e.strerror or str(e) or type(e).__name__))
                continue
            manifest[rel] = dict(sha=sha, src=list(src_st), dst=list(dst_st))
            updated += existed
            copied += not existed
    if not dry_run:
        live = {rel for _, rel in files}
        # bookkeeping only (the drive keeps them); a skipped root's entries stay for its next run
        manifest = {rel: e for rel, e in manifest.items() if rel in live or _skipped(rel, skip)}
        save_manifest(mpath, manifest)
        out(f"backup: {copied:,} copied, {updated:,} replaced (changed in the library)")
    if vanished:
        out(f"backup: {vanished} gone from the library since it was listed, skipped")
    if changed:
        out(f"backup: {len(changed)} changed while being copied, skipped (run backup again):")
        for rel in changed:
            out(f"  {rel}")
    if failed:
        ok = False
        out(f"backup: {len(failed)} could not be copied:")
        for rel, why in failed:
            out(f"  {rel}: {why}")
    extra = _extra_on_drive(dest, {rel for _, rel in files} | set(clashes), skip)
    if extra:
        out(f"backup: {extra:,} files on the drive are no longer in the library; left alone "
            "(nothing on the drive is deleted; erase --drive removes erased files by checksum)")
    if verify:
        ok &= _verify(dest, files, manifest, dry_run, out)
        if not dry_run:
            save_manifest(mpath, manifest)
    return 0 if ok and not changed else 1


def _extra_on_drive(dest, live, skip=()):
    n = 0
    if not os.path.isdir(dest):
        return 0
    for r, dirs, fs in os.walk(dest, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in fs:
            if f.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(r, f), dest)
            if rel not in live and not _skipped(rel, skip):
                n += 1
    return n


def _verify(dest, files, manifest, dry_run, out):
    """Re-read every destination copy and compare its sha256 with the source's. A mismatch is reported and
    dropped from the manifest, so the next backup run copies it again."""
    good, bad, missing = 0, [], []
    for src, rel in files:
        d = os.path.join(dest, rel)
        if _stat(d) is None:
            missing.append(rel)
            continue
        src_st = _stat(src)
        e = manifest.get(rel)
        want = e.get("sha") if e and src_st is not None and list(src_st) == e.get("src") else None
        try:
            want = want or intake.sha256(src)
            got = intake.sha256(d)
        except OSError:
            missing.append(rel)
            continue
        if got == want:
            good += 1
        else:
            bad.append(rel)
            if not dry_run:
                manifest.pop(rel, None)
    out(f"verify: {good:,} copies match their source (sha256), {len(bad):,} differ, {len(missing):,} missing")
    for rel in bad:
        out(f"  differs: {rel}")
    for rel in missing:
        out(f"  missing: {rel}")
    if bad:
        out("verify: run `media-intake backup MOUNT` again to copy the files that differ")
    return not bad and not missing


def build_parser(prog="media-intake backup"):
    ap = argparse.ArgumentParser(prog=prog, description="Copy the library (photos, videos, their .xmp sidecars) "
                                 "to a mounted drive: incremental, atomic per file, never deletes on the drive.")
    ap.add_argument("mount", help="the drive's mount point (e.g. /run/media/$USER/<label>)")
    ap.add_argument("--verify", action="store_true", help="then re-read every copy and compare sha256")
    ap.add_argument("--dry-run", action="store_true", help="say what would be copied; write nothing")
    ap.add_argument("--skip", action="append", default=[], metavar="PART",
                    help="leave this root out of the run (library, videos or projects/<key>; repeatable), "
                         "e.g. when the drive has no room for it yet")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    return backup(a.mount, verify=a.verify, dry_run=a.dry_run, out=lambda s: print(s, flush=True), skip=a.skip)


if __name__ == "__main__":
    sys.exit(main())
