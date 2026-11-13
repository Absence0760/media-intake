# Gallery (local web page)

`media_intake/gallery.py` is a stdlib-only local web page over the library
that intake files: a day-to-day browser in place of digiKam.

```
media-intake gallery [--drive MOUNT] [--port 0]
```

It prints exactly one `xdg-open 'http://127.0.0.1:<port>/?t=<token>'` line,
then a note with the item count, the start-up time, how many flagged or erased
files were left out and any plugin tabs. A private browser window keeps the
page out of your history. It runs until Ctrl-C or the page's **Close** button.
An assistant may start it and hand you the line; only you open the page and
click.

`--drive` is your backup drive's mount, passed to `forget` when you Erase, so
copies there go too.

## What it shows

- Every photo and video under the library, videos and project folders
  (`config.toml`, [configuration.md](configuration.md)). Never the
  media-intake state folder (quarantine, staging, review), never a symlink, a
  hidden folder, or a file whose sha256 `scan` flagged, quarantine holds or
  the erase log lists (`label.flagged()`).
- **Library**: a timeline grid by month, newest first, 240 items per page with
  infinite scroll; thumbnails load lazily. Click opens the large view (the
  image, or `<video controls>`; HEIC/DNG get a 1600 px JPEG made in memory)
  with tags, date, place, event, project, source, folder, resolution and size.
  Arrow keys go to the previous/next item, Esc closes.
- **Trash suggestions**: `Review|maybe-trash`, or an `AI|vlm/<model>/trash/*`
  verdict with that model's `conf` >= 0.7 (or the older `Review|vlm-trash/*`),
  never a file with `Owner|not-trash`, `Owner|keep`, digiKam Accepted,
  `Used|*` or a protected project's `Project|<name>`. Most confident first,
  big tiles. K / T mark the outlined tile, arrows move, Enter opens it;
  nothing changes until **Apply**, which shows the counts and needs a confirm.
- **Plugin tabs**: any view a plugin adds through `gallery_views()`, with its
  routes under `/plugin/<id>/` ([plugins.md](plugins.md)).

Dates come from the sidecar's `XMP-exif:DateTimeOriginal`, else the file
name, else the `YYYY-MM` folder (with the file's own time when it agrees),
else the file time. Project and source fall back to where the file sits
(`intake.infer`) when the sidecar has none.

## Filters (all combinable)

Project, Source, Event, Place, year or month, any `Label|*`, my tags, Review
(maybe-trash, child-in-shot, untagged = `Review|untagged-*` or no tags), AI
(trash with confidence >= N, keep, not judged), Used (used; or unused in the
last N days, blank = never used), photos / videos.

The search box runs `semantic.py search` (local SigLIP text model, through the
media-intake venv). When the venv, the embeddings or the text model are
missing, the page says why and falls back to matching tags, file names and
folders. **Similar** on any item runs `semantic.py similar`. Both need
`media-intake index` to have run ([search.md](search.md)).

## Actions (one item, or a selection)

Tick the box on a tile (shift-click a second box selects the range between),
then the bar's buttons or keys: **K** Keep, **T** Trash, **E** Erase. In the
large view the keys act on the item shown; **R** / **Shift+R** rotate.

| Action | What it writes |
|---|---|
| Keep | sidecar: `Owner\|keep` and `Owner\|not-trash` added, `Review\|maybe-trash` removed (its `maybe-trash` leaf only when no other tag uses it). Nothing else. |
| Trash | `media-intake trash FILE...`: plain delete of the file, its sidecar, thumbnails (freedesktop and the gallery's) and index entry; the sha goes in `trashed.json` so intake never takes it in again. Summary + confirm. |
| Erase | `media-intake forget FILE... --yes [--drive]`: the erase-everywhere path (quarantine, sweep of HOME, sidecars, thumbnails, recent files, erase log, verify-gone). Summary + type ERASE. For anything private. |
| My tag + / - | sidecar `HierarchicalSubject` + its `dc:Subject` leaf. Refused for the tools' namespaces (`Project\|`, `Source\|`, `Event\|`, `Place\|`, `Label\|`, `Review\|`, `AI\|`, `Used\|`, `Owner\|`); removal only touches your tags, and keeps a leaf another tag still uses. |
| Rotate | sidecar `XMP-tiff:Orientation` only (1/6/3/8). The original is never re-encoded or written; digiKam reads the same field. |

The summary before Trash or Erase lists the names and warns when any were
used or kept before. Trash and Erase run one at a time with their output
streamed into the dialog.

**No crop.** The original's sha256 drives dedupe and erase, so it is never
edited; make a crop as a copy in another editor.

Each writer keeps to its namespace ([intake.md](intake.md), "Tags: who writes
what"): Keep and my tags are yours; AI and SigLIP tags are only read.
`digiKam:TagsList` is not written; if digiKam shows a keyword missing, let it
re-read the sidecars.

## Thumbnails

Made on demand, max 320 px JPEG: Pillow for images (orientation from EXIF and
the sidecar baked in), ffmpeg for a video frame at 1 s (falling back to
`scan_sensitive.video_frames`, GPU HEVC decoding through GStreamer). Cached in
`~/.cache/media-intake/thumbs/<sha256>.jpg` (folder 0700, files 0600). They
are copies of photo content, so `trash`, `forget` and `erase` delete them by
sha256, and `verify-gone` reports one that is still there as STILL PRESENT.
Previews of formats a browser can't show are made in memory, never on disk.
Safe to delete the whole folder at any time; it rebuilds.

## Security

Same as the review page (`review_ui.py`, whose guards it reuses): stdlib
only, every byte of HTML/CSS/JS inline (no CDN, fonts or scripts), bound to
127.0.0.1 on a random port, a random token swapped for an HttpOnly
SameSite=Strict cookie, a Host check against DNS rebinding, a CSRF value and
an Origin check on every POST, `no-store` and a strict CSP on every response,
media by opaque id only (never a path), Range requests for video. Plugin
routes run only after the same token, Host, Origin and CSRF checks.

## Performance

Start-up reads the library tree, `index.json` and every sidecar (stdlib XML,
no exiftool) once and keeps them in memory; files are hashed only when their
size matches a flagged one, or when a thumbnail needs the sha. Measured on a
synthetic 3,000-item library (2,600 JPEGs with sidecars, 400 videos) held in
RAM: URL printed after 0.6 s, 38 MB RSS at start, 68 MB peak after listing all
items and making 240 thumbnails (about 20 ms each for a 1600 x 1200 JPEG). A
cold read from disk will be slower.

## Test

`python3 -I -m unittest discover -s tests -p test_gallery.py`: synthetic media in a throwaway
HOME, the real `intake.py` for trash and forget, and a demo plugin view.

## Not verified

- A full click-through in a real browser (layout, keys, selection, dialogs,
  infinite scroll, video playback, HEVC posters through GStreamer); the tests
  drive the HTTP API. Checked: the inline script parses (`node --check`) and
  headless Chromium renders the first page of the synthetic library (240
  tiles, month headings, filters filled).
- Semantic search and Similar through the venv: tested only as "unavailable,
  falls back", not with the real model.
- Start-up time on a large real library read from disk.
