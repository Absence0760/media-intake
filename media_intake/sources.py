"""Which files on a phone, camera, drive or laptop folder the media-intake tools may touch.

Allowlist first: only the camera/gallery folders below are ever listed, and only
photo/video extensions inside them are copied. A deny list then rejects any path
that looks like app data, downloads, documents or a password manager, even if
someone passes it in by hand. Nothing outside the allowlist is ever listed, not
even the storage root, so app data (Android/), Download/ and Documents/ are
never read.
"""
import os
import re
import unicodedata

import config

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".gpr",
    ".mp4", ".mov", ".3gp", ".mkv", ".webm",
}
# GoPro low-res previews and thumbnails: rebuilt by the camera, never kept.
CAMERA_JUNK_EXT = {".lrv", ".thm"}

# Relative to the device's storage root ("Internal storage" on Android over MTP).
PHONE_ALLOW = ("DCIM", "Pictures")
GOPRO_ALLOW = ("DCIM",)  # DCIM/100GOPRO, 101GOPRO, ...
# Extra exact folders from the config ([phone] extra_folders, e.g. a messaging app's
# media folder under Android/media/): with their subfolders, the only paths under a
# denied folder ever listed or touched. Default: none. {relative folder: app name}.
PHONE_EXTRA = config.get()["phone_extra"]
PHONE_ALLOW_EXTRA = tuple(PHONE_EXTRA)


def extra_app(rel):
    """The messaging app name for a path under one of the configured extra folders, else None."""
    return next((app for x, app in PHONE_EXTRA.items() if rel.startswith(x + "/")), None)

DENY_PART = re.compile(
    r"^(android|download|downloads|documents|\..*|data|obb|log|recordings|"
    r"ringtones|notifications|alarms|podcasts|audiobooks)$", re.I)
DENY_ANYWHERE = re.compile(
    r"bitwarden|1password|keepass|keepassdx|lastpass|dashlane|enpass|proton ?pass|"
    r"authy|aegis|authenticator|andotp|2fas|freeotp|"
    r"backup[-_ ]?codes?|recovery[-_ ]?codes?|seed[-_ ]?phrase|vault|keystore|wallet",
    re.I)


class Denied(Exception):
    pass


def check(root, path):
    """Raise Denied unless path is a media file under an allowed folder of root."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if rel.startswith("..") or os.path.isabs(rel):
        raise Denied(f"outside the device: {path}")
    if os.path.islink(path):
        raise Denied(f"symlink: {rel}")
    extra = next((x for x in PHONE_ALLOW_EXTRA if rel.startswith(x + os.sep)), None)
    if extra:  # case-sensitive exact prefix; the whole chain must be real folders
        if os.path.realpath(path) != os.path.join(os.path.realpath(root), rel):
            raise Denied(f"resolves elsewhere: {rel}")
        parts = parts[len(extra.split("/")):]
    for p in parts[:-1]:
        if DENY_PART.match(p) and p not in PHONE_ALLOW:
            raise Denied(f"denied folder: {rel}")
    if DENY_ANYWHERE.search(rel):
        raise Denied(f"denied name: {rel}")
    if os.path.splitext(parts[-1])[1].lower() not in MEDIA_EXT:
        raise Denied(f"not media: {rel}")


def walk(root, kind="phone"):
    """Yield (abs_path, rel) for every allowed media file. Lists allowed folders only."""
    if kind == "drive":
        yield from walk_drive(root)
        return
    if kind == "folder":
        yield from walk_folder(root)
        return
    allow = PHONE_ALLOW + PHONE_ALLOW_EXTRA if kind == "phone" else GOPRO_ALLOW
    for top in allow:
        base = os.path.join(root, top)
        if os.path.islink(base) or not os.path.isdir(base):
            continue
        for dirpath, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".") and not DENY_ANYWHERE.search(d)
                             and not os.path.islink(os.path.join(dirpath, d)))
            if kind == "gopro" and dirpath == base:
                dirs[:] = [d for d in dirs if re.fullmatch(r"\d{3}GOPRO", d)]
            for n in sorted(names):
                path = os.path.join(dirpath, n)
                try:
                    check(root, path)
                except Denied:
                    continue
                yield path, os.path.relpath(path, root)


def case_key(path):
    """What a case-insensitive device sees in path: Unicode NFC, then case-folded."""
    return unicodedata.normalize("NFC", path).casefold()


class CaseCollision(Denied):
    """Device paths that differ only by letter case: on the device they may be one file."""

    def __init__(self, groups):
        self.groups = groups
        super().__init__("paths differ only by letter case: "
                         + "; ".join(" | ".join(g) for g in groups))


def check_case(targets, known):
    """Raise CaseCollision unless every target's path is unique on a case-insensitive device.

    Android's emulated storage (and exFAT/FAT on cameras and drives) ignores case: after
    a case-only rename the listing can show both names, and they are one file there, so
    deleting the "duplicate" deletes the real one. known is every path the device listed
    (the batch manifests' src); a target colliding with any other known path or target
    is refused, whether or not the other name is a target too. Each group in the error
    lists the colliding paths, sorted.
    """
    by_key = {}
    for p in (*known, *targets):
        by_key.setdefault(case_key(p), set()).add(p)
    groups = sorted({tuple(sorted(by_key[case_key(t)])) for t in targets if len(by_key[case_key(t)]) > 1})
    if groups:
        raise CaseCollision([list(g) for g in groups])


# The only folders erase --phone-thumbnails may remove (Android rebuilds them).
PHONE_THUMBNAIL_DIRS = ("DCIM/.thumbnails", "Pictures/.thumbnails")


def thumbnail_dirs(root):
    """The existing hidden thumbnail folders on a phone: those two exact paths, never a symlink."""
    out = []
    for rel in PHONE_THUMBNAIL_DIRS:
        p = os.path.join(root, rel)
        parent = os.path.dirname(p)
        if os.path.islink(parent) or os.path.islink(p) or not os.path.isdir(p):
            continue
        if os.path.realpath(p) != os.path.join(os.path.realpath(root), rel):
            raise Denied(f"thumbnail folder resolves elsewhere: {rel}")
        out.append(p)
    return out


def check_thumbnail(root, path):
    """Raise Denied unless path sits inside one of PHONE_THUMBNAIL_DIRS (no symlinks)."""
    rel = os.path.relpath(path, root)
    if rel.startswith("..") or os.path.isabs(rel):
        raise Denied(f"outside the device: {path}")
    if not any(rel.startswith(d + os.sep) for d in PHONE_THUMBNAIL_DIRS):
        raise Denied(f"not a thumbnail folder: {rel}")
    if os.path.islink(path):
        raise Denied(f"symlink: {rel}")
    if rel.split(os.sep)[0].lower() == "android":
        raise Denied(f"denied folder: {rel}")


# ---------------------------------------------------------------- external drives (kind "drive")
# A non-backup external drive. The root is exactly the folder the user
# names; everything under it is walked, media extensions only, minus OS/system folders,
# hidden folders and files, symlinks and DENY_ANYWHERE names.
DRIVE_SYSTEM_DIRS = {"$recycle.bin", "system volume information", ".trashes", ".spotlight-v100",
                     ".fseventsd", "lost+found", ".documentrevisions-v100", ".temporaryitems",
                     "recycler", "found.000"}
# A drive is treated as your backup drive (never a source) when its mount root holds
# BACKUP_MARKER, or any folder named in the config's [drive] backup_markers (the folders
# your backup script writes there), unless SOURCE_OK_MARKER is there too.
BACKUP_LAYOUT = tuple(config.get()["backup_markers"])
BACKUP_MARKER = ".media-intake-backup"  # user-made, at the mount root: "this is my backup drive"
SOURCE_OK_MARKER = ".media-intake-source-ok"  # user-made, at the mount root: "not my backup drive"
LAPTOP_MOUNTS = re.compile(r"^(/|/home|/var|/tmp|/boot.*|/usr|/opt|/srv|/root)$")
SCREENSHOT_NAME = re.compile(r"^(Screenshot[_ ]|Screen Shot )", re.I)
WHATSAPP_NAME = re.compile(r"^(IMG|VID)-\d{8}-WA\d+", re.I)
_IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".gpr"}


def _findmnt(field, path):
    import subprocess
    r = subprocess.run(["findmnt", "-n", "-o", field, "-T", path], capture_output=True, text=True)
    return r.stdout.strip()


def drive_mount(path):
    """Mount point holding path. MEDIA_INTAKE_TEST_MOUNT (tests only) stands in for findmnt."""
    return os.environ.get("MEDIA_INTAKE_TEST_MOUNT") or _findmnt("TARGET", path)


def drive_uuid(path):
    """Filesystem UUID of the drive holding path ('' if none). MEDIA_INTAKE_TEST_UUID for tests."""
    if "MEDIA_INTAKE_TEST_MOUNT" in os.environ:
        return os.environ.get("MEDIA_INTAKE_TEST_UUID", "")
    return _findmnt("UUID", path)


def backup_like(mount):
    """What marks mount as the backup drive (marker file, configured folders), unless SOURCE_OK_MARKER is there."""
    if os.path.isfile(os.path.join(mount, SOURCE_OK_MARKER)):
        return []
    found = [BACKUP_MARKER] if os.path.exists(os.path.join(mount, BACKUP_MARKER)) else []
    return found + [d + "/" for d in BACKUP_LAYOUT if os.path.isdir(os.path.join(mount, d))]


def check_drive_root(root):
    """Raise Denied unless root is a real folder on an external, non-backup drive."""
    if os.path.islink(root) or not os.path.isdir(root):
        raise Denied(f"not a folder (or a symlink): {root}")
    mount = drive_mount(root)
    if "MEDIA_INTAKE_TEST_MOUNT" not in os.environ and (not mount or LAPTOP_MOUNTS.match(mount)):
        raise Denied(f"{root} is on the laptop's own disk ({mount or 'unknown'}), not an external drive")
    rp = os.path.realpath(root)
    if rp != os.path.realpath(mount) and not rp.startswith(os.path.realpath(mount) + os.sep):
        raise Denied(f"{root} is not under its mount {mount}")
    found = backup_like(mount)
    if found:
        raise Denied(f"{mount} looks like the backup drive (has {', '.join(found)} at its root). "
                     f"If it is not, create {mount}/{SOURCE_OK_MARKER}")
    return mount


def check_drive(root, path):
    """Raise Denied unless path is a media file under root, outside system/hidden folders."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if rel.startswith("..") or os.path.isabs(rel):
        raise Denied(f"outside the source folder: {path}")
    if os.path.islink(path):
        raise Denied(f"symlink: {rel}")
    if os.path.realpath(path) != os.path.join(os.path.realpath(root), rel):
        raise Denied(f"resolves elsewhere: {rel}")
    for p in parts:
        if p.startswith(".") or p.lower() in DRIVE_SYSTEM_DIRS:
            raise Denied(f"hidden or system: {rel}")
    if DENY_ANYWHERE.search(rel):
        raise Denied(f"denied name: {rel}")
    if os.path.splitext(parts[-1])[1].lower() not in MEDIA_EXT:
        raise Denied(f"not media: {rel}")


def walk_drive(root):
    """Yield (abs_path, rel) for every media file under root (check_drive_root first)."""
    root = os.path.abspath(root)
    check_drive_root(root)
    yield from _walk_tree(root)


def walk_folder(root):
    """Yield (abs_path, rel) for every media file under a laptop folder (check_folder_root first).
    A git working tree found inside it (a subfolder holding .git) is skipped whole."""
    root = os.path.abspath(root)
    check_folder_root(root)
    yield from _walk_tree(root, skip_repos=True)


def _walk_tree(root, skip_repos=False):
    """Every file check_drive accepts under root; hidden, system, denied and symlinked folders are not entered."""
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d.lower() not in DRIVE_SYSTEM_DIRS
                         and not DENY_ANYWHERE.search(d) and not os.path.islink(os.path.join(dirpath, d))
                         and not (skip_repos and os.path.lexists(os.path.join(dirpath, d, ".git"))))
        for n in sorted(names):
            path = os.path.join(dirpath, n)
            try:
                check_drive(root, path)
            except Denied:
                continue
            yield path, os.path.relpath(path, root)


def drive_category(rel):
    """photo / video / screenshot / messaging:WhatsApp for a file from an external drive or laptop folder
    (WhatsApp's IMG-YYYYMMDD-WA0001 names are recognised without any config)."""
    name = os.path.basename(rel)
    ext = os.path.splitext(name)[1].lower()
    if WHATSAPP_NAME.match(name):
        return "messaging:WhatsApp"
    if SCREENSHOT_NAME.match(name) and ext in _IMG_EXT:
        return "screenshot"
    return "photo" if ext in _IMG_EXT else "video"


# ---------------------------------------------------------------- laptop folders (kind "folder")
# A folder of photos and videos on the laptop's own disk. Same per-file rules as a drive
# (check_drive, _walk_tree), plus git working trees inside it skipped. The tool never deletes
# a folder source's originals: offload refuses kind folder; you delete the folder yourself.
# Folders directly under HOME a folder source may not be, be inside, or contain: app data,
# keys, and common code and SDK trees. Hidden folders are refused separately (any of them).
FOLDER_DENY_HOME = (".local", ".config", ".cache", ".var", ".ssh", ".gnupg",
                    "github", "gitlab", "repos", "src", "code", "development", "go", "Android",
                    "sdk", "flutter", "snap")  # compared case-insensitively
# Where external drives mount: a folder there is a drive (--kind drive), not a laptop folder.
FOLDER_DRIVE_PREFIXES = ("/run/media", "/media", "/mnt", "/Volumes")


def _home():
    return os.path.realpath(os.path.expanduser("~"))


def _within(p, base, fold=False):
    """p is base or under it. fold: compare case-insensitively (stricter on a case-insensitive disk)."""
    if fold:
        p, base = p.casefold(), base.casefold()
    return p == base or p.startswith(base.rstrip(os.sep) + os.sep)


def folder_deny_roots():
    """(path, why) pairs a folder source may not be, be inside, or contain, from config and HOME."""
    home, cfg = _home(), config.get()
    out = [(cfg["state"], "the media-intake state (batches, quarantine)"),
           (cfg["library"], "the photo library"), (cfg["videos"], "the videos library")]
    out += [(p["folder"], f"the {p['name']} project folder") for p in cfg["projects"].values()]
    for var, why in (("XDG_CONFIG_HOME", "app settings"), ("XDG_DATA_HOME", "app data"),
                     ("XDG_CACHE_HOME", "app caches"), ("XDG_STATE_HOME", "app state")):
        if os.environ.get(var):
            out.append((os.environ[var], f"{why} (${var})"))
    out += [(os.path.join(home, d), f"~/{d}") for d in FOLDER_DENY_HOME]
    return [(os.path.realpath(p), why) for p, why in out]


def check_folder_root(root):
    """Raise Denied unless root is a real folder under HOME that is safe to read photos from.

    Fails closed: anything it cannot check is refused. Returns the folder's real path.
    """
    try:
        return _check_folder_root(root)
    except Denied:
        raise
    except (OSError, ValueError) as e:
        raise Denied(f"{root}: could not check it ({e})") from None


def _check_folder_root(root):
    if os.path.islink(root) or not os.path.isdir(root):
        raise Denied(f"not a folder (or a symlink): {root}")
    rp, home = os.path.realpath(root), _home()
    if home in ("", os.sep) or not os.path.isdir(home):
        raise Denied(f"cannot tell your home folder (HOME={home!r}), so no laptop folder is accepted")
    if any(_within(rp, d, fold=True) for d in FOLDER_DRIVE_PREFIXES):
        raise Denied(f"{root} is on an external drive: use --kind drive")
    if _within(home, rp, fold=True):
        raise Denied(f"{root} is (or contains) your whole home folder: name the photo folder itself")
    if not _within(rp, home):
        raise Denied(f"{root} is outside your home folder; only folders under {home} (or a drive: --kind drive)")
    for d, why in folder_deny_roots():
        if _within(rp, d, fold=True) or _within(d, rp, fold=True):
            raise Denied(f"{root} is, is inside, or contains {why} ({d}): not a source folder")
    rel = os.path.relpath(rp, home)
    if any(p.startswith(".") for p in rel.split(os.sep)):
        raise Denied(f"{root} is a hidden folder (app data): copy the photos somewhere visible first")
    if DENY_ANYWHERE.search(rel):
        raise Denied(f"denied name (password manager or keys): {root}")
    up = rp
    while True:  # erase's home sweep never looks inside a git working tree, so a source never sits in one
        if os.path.lexists(os.path.join(up, ".git")):
            raise Denied(f"{root} is inside a git repository ({up}): not a source folder")
        if up == home:
            break
        up = os.path.dirname(up)
    return rp
