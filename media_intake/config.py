"""Settings for every media-intake module, from one TOML file plus env vars.

Stdlib only: label.py, geocode.py and scan_sensitive.py import this inside the
venv, intake.py outside it.

File: $MEDIA_INTAKE_CONFIG, else $XDG_CONFIG_HOME/media-intake/config.toml,
else ~/.config/media-intake/config.toml. A missing file means all defaults.
Env vars win over the file (MEDIA_INTAKE_STATE, PHOTO_LIBRARY, PERSONAL_FOOTAGE).
Schema and defaults: docs/configuration.md and config.example.toml.
"""
import fnmatch, os, re, tomllib

HOME = os.path.expanduser("~")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
TOP_KEYS = {"library", "videos", "state", "phone", "drive", "queue", "projects", "plugins"}


class ConfigError(Exception):
    pass


def config_path():
    p = os.environ.get("MEDIA_INTAKE_CONFIG")
    if p:
        return os.path.expanduser(p)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(base, "media-intake", "config.toml")


def _path(v, what):
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{what}: a path string")
    return os.path.abspath(os.path.expandvars(os.path.expanduser(v)))


def _strs(v, what):
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"{what}: a list of strings")
    return list(v)


def load(path=None):
    """The validated settings as a plain dict. Raises ConfigError on a bad file."""
    path = path or config_path()
    raw = {}
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}")
    c = dict(path=path)
    c["state"] = _path(os.environ.get("MEDIA_INTAKE_STATE") or raw.get("state", "~/.local/share/media-intake"), "state")
    c["library"] = _path(os.environ.get("PHOTO_LIBRARY") or raw.get("library", "~/Pictures/library"), "library")
    c["videos"] = _path(os.environ.get("PERSONAL_FOOTAGE") or raw.get("videos", "~/Videos/personal"), "videos")

    phone = raw.get("phone", {})
    extra = phone.get("extra_folders", {})
    if not isinstance(extra, dict):
        raise ConfigError("phone.extra_folders: a table of \"relative/folder\" = \"App name\"")
    c["phone_extra"] = {}
    for rel, app in extra.items():
        rel = rel.strip("/")
        if not rel or rel.startswith(".") or ".." in rel.split("/") or not isinstance(app, str) or not NAME.match(app):
            raise ConfigError(f"phone.extra_folders: bad entry {rel!r} = {app!r}")
        c["phone_extra"][rel] = app

    drive = raw.get("drive", {})
    c["backup_markers"] = _strs(drive.get("backup_markers", []), "drive.backup_markers")

    queue = raw.get("queue", {})
    c["queue_wrapper"] = _strs(queue.get("wrapper", []), "queue.wrapper")
    lock = queue.get("wrapper_lock", "")
    c["queue_wrapper_lock"] = _path(lock, "queue.wrapper_lock") if lock else ""

    c["projects"] = {}
    for key, p in (raw.get("projects") or {}).items():
        if not SLUG.match(key) or not isinstance(p, dict):
            raise ConfigError(f"projects.{key}: the key is a lowercase slug and the value a table")
        name = p.get("name", key)
        if not isinstance(name, str) or not NAME.match(name) or name.casefold() == "personal":
            raise ConfigError(f"projects.{key}.name: letters, digits, space . _ - (and not 'Personal')")
        if "folder" not in p:
            raise ConfigError(f"projects.{key}.folder is required")
        c["projects"][key] = dict(key=key, name=name, folder=_path(p["folder"], f"projects.{key}.folder"),
                                  events=_strs(p.get("events", []), f"projects.{key}.events"),
                                  protect=bool(p.get("protect", True)))

    plugins = raw.get("plugins", {})
    c["plugin_paths"] = [_path(x, "plugins.paths") for x in _strs(plugins.get("paths", []), "plugins.paths")]
    c["plugin_modules"] = _strs(plugins.get("modules", []), "plugins.modules")
    return c


_cache = {}


def get():
    """Settings, loaded once per process (per config path and env). Exits on a bad config file."""
    key = (config_path(), HOME, *(os.environ.get(k) for k in ("MEDIA_INTAKE_STATE", "PHOTO_LIBRARY",
                                                               "PERSONAL_FOOTAGE", "HOME")))
    if key not in _cache:
        try:
            _cache[key] = load()
        except ConfigError as e:
            raise SystemExit(f"media-intake config: {e}")
    return _cache[key]


def project_for_event(slug, cfg=None):
    """The project dict an unprefixed --event slug routes to (first matching pattern), or None."""
    cfg = cfg or get()
    for p in cfg["projects"].values():
        if any(fnmatch.fnmatchcase(slug, pat) for pat in p["events"]):
            return p
    return None


def project_roots(cfg=None):
    return [p["folder"] for p in (cfg or get())["projects"].values()]


def protected_tags(cfg=None):
    """Project|<name> tags whose files are never trash candidates."""
    return {f"Project|{p['name']}" for p in (cfg or get())["projects"].values() if p["protect"]}


def library_roots(cfg=None):
    """Everything the tools treat as the library: photos, videos, every project folder."""
    cfg = cfg or get()
    return [cfg["library"], cfg["videos"], *project_roots(cfg)]
