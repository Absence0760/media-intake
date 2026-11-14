"""Plugin hooks: project-specific steps without forking the core (docs/plugins.md).

A plugin is a Python module named in the config's [plugins] modules list,
importable from [plugins] paths. Every hook is optional:

  after_tag(files)    called by `media-intake tag BATCH` once the sidecars are
                      written. files: [{"path", "project", "event", "date"}] for
                      the batch's filed, unflagged files (date "YYYY-MM-DD").
  gallery_views()     a list of extra gallery tabs, each a dict:
                        {"id": "slug", "title": "Tab label",
                         "script": "JS source defining window.mediaIntakeViews[id] =
                                    {render(el, api)}",
                         "get":  callable(path, query) -> (status, json-able) | None,
                         "post": callable(path, body) -> (status, json-able) | None,
                         "post_read_only": True (optional: post never writes the library),
                         "media": callable(path) -> {"path", "mime"} | None}
                      Its routes live under /plugin/<id>/...; the gallery's own
                      token, Host, Origin and CSRF checks run before any of them.
                      A post runs inside the library lock (409 at once while a job
                      or the CLI holds it) unless the view sets post_read_only.

A plugin runs with your user's rights: list only modules you wrote or read.
"""
import importlib, sys

import config

_mods = None


def modules():
    global _mods
    if _mods is None:
        cfg = config.get()
        for p in cfg["plugin_paths"]:
            if p not in sys.path:
                sys.path.append(p)  # after the stdlib and the package: a plugin never shadows them
        _mods = []
        for name in cfg["plugin_modules"]:
            try:
                _mods.append(importlib.import_module(name))
            except ImportError as e:
                raise SystemExit(f"plugin {name}: {e}")
    return _mods


def call(hook, *args):
    """Call hook on every plugin that defines it; returns their results in order."""
    return [getattr(m, hook)(*args) for m in modules() if callable(getattr(m, hook, None))]
