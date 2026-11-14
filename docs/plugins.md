# Plugins

A plugin adds project-specific steps without forking the tool: registering
filed clips in your own catalogue, an extra review tab in the gallery, and so
on. It is a Python module you list in the config:

```toml
[plugins]
paths = ["~/src/my-media-plugin"]   # appended to sys.path (after the stdlib and the package)
modules = ["my_media_plugin"]
```

A plugin runs in-process with your user's rights. List only code you wrote or
read. Every hook is optional; the tool calls each hook on every listed module
that defines it (`media_intake/plugins.py`).

## `after_tag(files)`

Called by `media-intake tag BATCH` after the sidecars are written.
`files` is a list of dicts for the batch's filed files, never a file the
sensitive scan flagged, quarantined or erased:

```python
{"path": "/abs/path/clip.mp4", "project": "Run Club", "event": "park-race", "date": "2026-05-01"}
```

`project` is `Personal` for files outside every project folder; `event` and
`date` may be `None`. Use it to write your own registry rows for a project's
clips, for example.

## `gallery_views()`

Returns a list of extra gallery tabs, shown in the sidebar's views after Trash
suggestions. Each is a dict:

| Key | Type | What |
| --- | --- | --- |
| `id` | slug (`[a-z0-9-]`, up to 32) | The tab id; its routes live under `/plugin/<id>/` |
| `title` | str | The tab label |
| `script` | str | JavaScript, inlined into the page, that sets `window.mediaIntakeViews[id] = {render(el, ctx)}` |
| `get` | `callable(rest, query) -> (status, json) or None` | `GET /plugin/<id>/<rest>`; `None` is a 404 |
| `post` | `callable(rest, body) -> (status, json) or None` | `POST /plugin/<id>/<rest>` with a JSON object body |
| `media` | `callable(rest) -> {"path", "mime"} or None` | `GET /plugin/<id>/media/<rest>`, served with Range support |

`render(el, ctx)` gets the tab's container element and `ctx` with
`ctx.api(path, body)` (GET without a body, POST with one, relative to the
plugin's routes), `ctx.media(path)` (a URL for the plugin's media route) and
`ctx.el(tag, class, text)`.

The gallery's own guards run before any plugin route: the session cookie,
the Host and Origin checks and, for POST, the CSRF header and a JSON body of
at most 1 MiB. A `media` file is served only if it is a regular file (no
symlink) outside the state folder and not flagged, quarantined or erased;
otherwise the route answers 404. The page's Content-Security-Policy still
applies: no external scripts, styles or connections.

A minimal plugin is in `tests/test_gallery.py` (`TOY_PLUGIN`).
