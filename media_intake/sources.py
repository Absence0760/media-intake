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


def check(root, path, exts=MEDIA_EXT):
    """Raise Denied unless path is a media file (exts) under an allowed folder of root."""
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
    if os.path.splitext(parts[-1])[1].lower() not in exts:
        raise Denied(f"not media: {rel}")


def check_preview(root, path):
    """Raise Denied unless path is a camera preview (.LRV/.THM) that check's folder rules allow."""
    check(root, path, CAMERA_JUNK_EXT)


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
    (the batch manifests' src, plus live_case_matches at delete time); a target colliding with any other known path or target
    is refused, whether or not the other name is a target too. Each group in the error
    lists the colliding paths, sorted.
    """
    by_key = {}
    for p in (*known, *targets):
        by_key.setdefault(case_key(p), set()).add(p)
    groups = sorted({tuple(sorted(by_key[case_key(t)])) for t in targets if len(by_key[case_key(t)]) > 1})
    if groups:
        raise CaseCollision([list(g) for g in groups])


class ListingFailed(Denied):
    """The device's live listing could not be taken, so a delete can't be checked against it."""


def list_dir(path):
    """The names a folder on the device lists now (os.listdir: gvfs over MTP or gphoto2, a mounted
    card, drive or folder). The one place the live listing is read, so tests can fake a device."""
    return os.listdir(path)


def live_case_matches(root, targets):
    """Every path the device lists right now that a case-insensitive device takes for one of targets.

    The manifests only know what was there at staging; a file copied on afterwards, or one the
    walk never recorded (outside the allowlist, a hidden folder), is just as much one file with
    a target whose name differs only by case. So for each target this lists the folders on its
    way down from root, following every entry whose name matches the next part case-insensitively
    (case_key), and returns the matching paths: the target itself, plus any case twin of it or of
    a folder above it. A file where a folder is expected counts as a twin. Each folder is listed
    once. Raises ListingFailed if a folder can't be listed, or if the listing doesn't show a target
    at all (an incomplete listing proves nothing): fail closed, never fall back on the manifests.
    """
    cache, found = {}, set()

    def ls(d):
        if d not in cache:
            try:
                cache[d] = list_dir(d)
            except NotADirectoryError:
                cache[d] = None
            except OSError as e:
                raise ListingFailed(f"could not list {d} on the device ({e.strerror or e})") from None
        return cache[d]

    for t in targets:
        parts = os.path.relpath(t, root).split(os.sep)
        level = [root]
        for i, part in enumerate(parts):
            key, below = case_key(part), []
            for d in level:
                for n in ls(d):
                    if case_key(n) == key:
                        below.append(os.path.join(d, n))
            if i == len(parts) - 1:
                found.update(below)
                break
            level = []
            for p in below:
                if ls(p) is None:  # a file named like the folder: one name on the device
                    found.add(os.path.join(p, *parts[i + 1:]))
                else:
                    level.append(p)
        if t not in found:
            raise ListingFailed(f"the device's listing does not show {os.path.relpath(t, root)}")
    return sorted(found)


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
# Signal's "Save" (Android and Desktop) names a file signal-YYYY-MM-DD-HH-mm-ss-SSS[_NNN].ext when the
# attachment had no name of its own (Signal-Android SaveAttachmentUtil.kt, Signal-Desktop
# ts/util/Attachment.std.ts getSuggestedFilename). Its backups (.backup) are encrypted and never media.
SIGNAL_NAME = re.compile(r"^signal-\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d", re.I)
# Telegram Desktop's "Export chat history" / "Export Telegram data" (tdesktop export/): a folder
# ChatExport_<YYYY-MM-DD>[ (n)] or DataExport_<YYYY-MM-DD>[ (n)] holding result.json or messages.html,
# with per-chat media folders. Only TELEGRAM_EXPORT_MEDIA are ever taken from it: stickers/,
# animations/, round_video_messages/, voice_messages/, profile_pictures/, stories/, contacts/ and
# the HTML export's css/, js/ and images/ (its own icons) are not, nor its *_thumb.jpg previews.
TELEGRAM_EXPORT_DIR = re.compile(r"^(ChatExport|DataExport)_\d{4}-\d\d-\d\d(?: \(\d+\))?$")
TELEGRAM_EXPORT_MARKERS = ("result.json", "messages.html")
TELEGRAM_EXPORT_MEDIA = ("photos", "video_files", "files")
# photo_<n>@DD-MM-YYYY_HH-MM-SS.jpg, video_<n>@....mp4, file_<n>@....ext: an export's own names for
# media without a name of its own (dates.py reads the date from it).
TELEGRAM_EXPORT_NAME = re.compile(r"^(photo|video|file)_\d+@\d\d-\d\d-\d{4}_\d\d-\d\d-\d\d\.", re.I)
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
    if telegram_export(root, parts):
        if len(parts) < 2 or parts[-2] not in TELEGRAM_EXPORT_MEDIA:
            raise Denied(f"Telegram export, not a photo or video folder: {rel}")
        if parts[-1].lower().endswith("_thumb.jpg"):
            raise Denied(f"Telegram export preview: {rel}")


def _export_marked(folder):
    """True when folder looks like a renamed Telegram Desktop export: result.json or messages.html (regular
    files) beside a photos/ or video_files/ folder. A web page saved with its files/ folder is not one."""
    j = os.path.join
    return (any(os.path.isfile(j(folder, m)) and not os.path.islink(j(folder, m)) for m in TELEGRAM_EXPORT_MARKERS)
            and any(os.path.isdir(j(folder, d)) for d in ("photos", "video_files")))


def telegram_export(root, parts):
    """True when the file at root/<parts> is inside a Telegram Desktop export: the source root or a folder
    on the way is named ChatExport_<date> / DataExport_<date>, or holds result.json or messages.html."""
    if any(TELEGRAM_EXPORT_DIR.match(p) for p in [os.path.basename(root), *parts[:-1]]):
        return True
    folder = root
    for p in [None, *parts[:-1]]:
        folder = folder if p is None else os.path.join(folder, p)
        if _export_marked(folder):
            return True
    return False


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


def export_app(root, rel):
    """"Telegram" when the file at root/rel is in a Telegram Desktop export (telegram_export: the source
    root itself, a renamed export holding result.json or messages.html, or a ChatExport_ / DataExport_
    folder), else None. stage records it in the manifest, so plan's category sees what rel alone can't."""
    return "Telegram" if telegram_export(root, rel.split(os.sep)) else None


def drive_category(rel, app=None):
    """photo / video / screenshot / messaging:<app> for a file from an external drive or laptop folder.
    app: export_app()'s answer for the file, recorded by stage (the source root is not in rel).
    Recognised without any config: WhatsApp's IMG-YYYYMMDD-WA0001 names, Signal's saved
    signal-YYYY-MM-DD-... names, and a Telegram Desktop export's media (a file in photos/, video_files/
    or files/ under a ChatExport_<date> / DataExport_<date> folder, or named photo_<n>@<date>...)."""
    name = os.path.basename(rel)
    ext = os.path.splitext(name)[1].lower()
    parts = rel.replace(os.sep, "/").split("/")
    if app:
        return f"messaging:{app}"
    if WHATSAPP_NAME.match(name):
        return "messaging:WhatsApp"
    if SIGNAL_NAME.match(name):
        return "messaging:Signal"
    if TELEGRAM_EXPORT_NAME.match(name) or (
            len(parts) > 2 and parts[-2] in TELEGRAM_EXPORT_MEDIA
            and any(TELEGRAM_EXPORT_DIR.match(p) for p in parts[:-2])):
        return "messaging:Telegram"
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


def root_present(root):
    """A library folder (library, videos, a project folder) is there to judge "this file is gone" by: it exists,
    holds at least one entry (an unmounted drive often leaves its empty mountpoint folder behind, and an empty
    folder can't hold the files we have records of), and, when it is where drives mount (FOLDER_DRIVE_PREFIXES),
    the mount holding it is a drive there, not the laptop's own disk under a stale folder (drive_mount: findmnt).
    Recognition's prune, My places' stale records and the date records all ask this before dropping anything."""
    try:
        if not os.path.isdir(root):
            return False
        with os.scandir(root) as it:
            if next(it, None) is None:
                return False
    except OSError:
        return False
    rp = os.path.realpath(root)
    if any(_within(rp, d) for d in FOLDER_DRIVE_PREFIXES):
        try:
            mount = drive_mount(rp)
        except OSError:  # no findmnt: the non-empty folder is all there is to go by
            return True
        return bool(mount) and any(_within(os.path.realpath(mount), d) for d in FOLDER_DRIVE_PREFIXES) \
            and mount.rstrip(os.sep) not in [d.rstrip(os.sep) for d in FOLDER_DRIVE_PREFIXES]
    return True


def present_roots(roots):
    """The realpaths of the library folders that are there (root_present), for "missing means gone"."""
    return [os.path.realpath(r) for r in roots if root_present(r)]


def file_gone(path, roots, present, outside=False):
    """True when a recorded file is gone for good: missing while the innermost library folder holding it
    (roots; present: present_roots(roots)) is there. A project drive inside the library counts on its own.
    outside: what to say for a file under no library folder (recognition: gone once its own folder is there)."""
    if os.path.lexists(path):
        return False
    rp = os.path.realpath(path)
    under = [os.path.realpath(r) for r in roots if _within(rp, os.path.realpath(r)) and rp != os.path.realpath(r)]
    if not under:
        return outside and os.path.isdir(os.path.dirname(path))
    return max(under, key=len) in present


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
