# Gallery (local web page)

`media_intake/gallery.py` is a stdlib-only local web page over the library
that intake files: a day-to-day browser in place of digiKam.

```
media-intake gallery [--drive MOUNT] [--port 0]
```

`python3 -I media_intake/gallery.py` takes the same flags and `--share-out DIR`
(where share-safe copies go, below; `media-intake gallery` does not pass it on
yet, so it always uses the default).

It prints exactly one `xdg-open 'http://127.0.0.1:<port>/?t=<token>'` line,
then a note with the item count, the start-up time, how many flagged or erased
files were left out and any plugin tabs. A private browser window keeps the
page out of your history. It runs until Ctrl-C or the page's **Close** button.
An assistant may start it and hand you the line; only you open the page and
click.

`--drive` is your backup drive's mount, passed to `forget` when you Erase, so
copies there go too.

## Layout

- **Left sidebar** (its own scroll; the menu button collapses it to an icon
  rail, remembered per browser; at phone width it is a rail by default and
  opens as a drawer): the views with counts (Library, Trash suggestions, any
  plugin tab; quick views Recently added = filed in the last 30 days, by the
  file's inode change time, Possible child = `Review|child-in-shot`,
  Untagged; then **Events** with counts, the eight largest and "All N events"),
  the search box (a one-line hint when semantic search is off, the
  full reason under "why"), then the filters as collapsible sections with
  counts (open / closed remembered per section) and "Clear filters (N)".
  Counts are for the whole library, not narrowed by the other filters.
- **Main area**: a slim header (view, item count, thumbnail-size slider,
  Close), the active filters as removable chips (in an event: the event's
  **Rename…** and **Merge into…** bar), the month grid. The
  selection actions sit in a bar at the bottom that appears only while
  something is selected (Trash suggestions keeps its Apply bar there).
- **Large view**: the photo or video fills everything left of the info pane.
  A video starts playing on open (muted, with an Unmute button, if the
  browser refuses sound), Space plays / pauses, Prev / Next play the next one,
  Esc stops it and releases the file. When the browser can't decode it (often
  HEVC), the thumbnail shows large with **Open in the system player**, which
  runs `xdg-open` on that one file (below; `xdg-open` comes from Fedora's
  `xdg-utils`, which `media-intake doctor` lists as optional).

The sidebar rail, open sections and tile size live in the browser's
`localStorage` (a per-browser convenience; the page works without it).

## What it shows

- Every photo and video under the library, videos and project folders
  (`config.toml`, [configuration.md](configuration.md)). Never the
  media-intake state folder (quarantine, staging, review), never a symlink, a
  hidden folder, or a file whose sha256 `scan` flagged, quarantine holds or
  the erase log lists (`label.flagged()`).
- **Library**: a timeline grid by month, newest first, 240 items per page with
  infinite scroll; thumbnails load lazily. Click (or Enter on a focused tile)
  opens the large view (the image, or `<video controls>`, which starts playing
  by itself; HEIC/DNG get a 1600 px JPEG made in memory) with tags, date,
  place, event, project, source, folder, resolution and size. Arrow keys go to
  the previous/next item, Esc closes, `/` jumps to the search box.
- **Trash suggestions**: `Review|maybe-trash`, an `AI|vlm/<model>/trash/*`
  verdict with that model's `conf` >= 0.7 (or the older `Review|vlm-trash/*`),
  or `Owner|trash` (a copy of content you trashed elsewhere, chip "trashed
  copy"); never a file with `Owner|not-trash` (or the legacy
  `Review|not-trash`), `Owner|keep`, digiKam Accepted,
  `Used|*` or a protected project's `Project|<name>`. Most confident first,
  big tiles. K / T mark the outlined tile, arrows move, Enter opens it;
  nothing changes until **Apply**, which shows the counts and needs a confirm.
- **Events**: every event in the library, from the sidecars' `Event|<slug>`
  tags (or `XMP-iptcExt:Event` on a file with no `Event|` tag), with its count;
  an item in two events counts and shows in both. A click shows that event's
  items (the Event filter, as a chip) and a bar with:
  - **Rename…** (`POST /api/event/rename`, `{"from": old, "to": new}`): every
    sidecar with `Event|old` gets `Event|new` instead. The new name is a slug
    (lowercase letters, digits, dashes, like `plan --event`) that no event has
    yet; joining an existing event is a merge.
  - **Merge into…** (`POST /api/event/merge`, `{"from": [a, ...], "into": b}`):
    every item of each `from` event moves to `b`, which must exist.

  Both rewrite only `Event|` tags (and `XMP-iptcExt:Event` when it named the
  old event), in the sidecars that have them, under the library lock: never an
  original, never `Owner|`, your tags or any other namespace, and a removed
  `dc:Subject` leaf only when no other tag uses it. A later `tag` or
  `tag-files` with an event replaces `Event|` again ([intake.md](intake.md),
  "Tags: who writes what").
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
last N days, blank = never used), photos / videos, and added in the last N
days (`added=N` on `/api/list`, what Recently added sets).

`GET /api/facets` gives the sidebar its options and counts: `facets` (per
project, source, event, place, label, month and year, my tag), `counts`
(`kind:photo`, `kind:video`, `review:<flag>`, `ai:trash|keep|none`,
`used:used|unused`, `suggest`, `recent`), `recent_days` and the search
status. Like every route it needs the session cookie or token.

The search box runs `semantic.py search` (local SigLIP text model, through the
media-intake venv). When the venv, the embeddings or the text model are
missing, the page says why and falls back to matching tags, file names and
folders. **Similar** on any item runs `semantic.py similar`. Both need
`media-intake index` to have run ([search.md](search.md)).

## Actions (one item, or a selection)

Tick the box on a tile (shift-click a second box selects the range between;
Space on a focused tile ticks it), then the bottom bar's buttons or keys:
**K** Keep, **T** Trash, **E** Erase, **+ tag** / **− tag**, **Set event…**,
**Rotate ⟳**, **Share-safe copy**; Esc or **Clear selection** clears it. Each
applies to every selected item through the same endpoint as one item (one
unknown or removed id refuses the whole request). In the large view the keys
act on the item shown; **R** / **Shift+R** rotate, and its buttons include Set
event and Share-safe copy.

| Action | What it writes |
|---|---|
| Keep | sidecar: `Owner\|keep` and `Owner\|not-trash` added; `Review\|maybe-trash`, an older `Owner\|trash` and the legacy `Review\|not-trash` removed (each leaf only when no other tag uses it). Nothing else. The same rule as the review page's Keep (`intake.keep_args`). |
| Trash | `media-intake trash FILE...`: plain delete of the file, its sidecar, thumbnails (freedesktop and the gallery's) and index entry; the sha goes in `trashed.json` (the durable `Owner\|trash`) so intake never takes it in again, and library copies of it left on disk get `Owner\|trash` in their sidecar. The same command the review page's Trash runs. Summary + confirm. |
| Erase | `media-intake forget FILE... --yes [--drive]`: the erase-everywhere path (quarantine, sweep of HOME, sidecars, thumbnails, recent files, erase log, verify-gone). Summary + type ERASE. For anything private. |
| My tag + / - | sidecar `HierarchicalSubject` + its `dc:Subject` leaf. Refused for the tools' namespaces (`Project\|`, `Source\|`, `Event\|`, `Place\|`, `Label\|`, `Review\|`, `AI\|`, `Used\|`, `Owner\|`); removal only touches your tags, and keeps a leaf another tag still uses. |
| Rotate | sidecar `XMP-tiff:Orientation` only (1/6/3/8), each item from its own orientation. The original is never re-encoded or written; digiKam reads the same field. |
| Set event | `POST /api/event/set` (`{"ids": [...], "event": slug}`): sidecar `Event\|<slug>` (and `XMP-iptcExt:Event`), replacing the item's other `Event\|` tags; `"event": ""` takes the items out of every event. Nothing else. |
| Share-safe copy | nothing in the library: `media-intake share-copy` into a new folder (below). |
| Open in the system player | nothing: `POST /api/open` with one id (`{"ids": [id]}`) runs `xdg-open <file>` (an argument list, no shell, detached) for the one item, only on that button's POST, and only for a regular file inside a library root after resolving links, not in a running job and not flagged, quarantined or erased (else 400). |

Every action holds the library lock ([intake.md](intake.md), "One writer at
a time"): Keep, my tags, Rotate, Set event, Rename, Merge and a plugin view's
POST for the write ([plugins.md](plugins.md)), Trash, Erase and Share-safe copy
for their whole job. While a CLI command (`apply`, `erase`, ...), a review run or
another gallery's job holds it, the action gets 409 naming the holder and
nothing is written; browsing is never locked.

The summary before Trash or Erase lists the names and warns when any were
used or kept before. Trash and Erase run one at a time with their output
streamed into the dialog.

Trash and Erase run as a detached job (`media_intake/jobs.py`, the same module
as the review page), so closing the page or stopping the server never stops
one that has started:

- The dialog shows what the job is doing from the moment it starts, before
  any output exists (Erase: "Erasing… looking for copies in your home folder.
  This can take a few minutes; you can close this page, it keeps running.",
  since the sweep of HOME can print nothing for minutes), then polls the job
  and shows its step and output as it arrives. When it ends the status goes
  and the title says "Done" or "Finished with problems", with the summary
  line last in the output. A page opened (or
  reloaded) while one runs, from this server or a restarted one, opens the
  dialog on the running job; the files it acts on stay hidden from thumbnails
  and media until it ends.
- One at a time across every gallery page and server: an `flock` on
  `<state>/jobs/gallery.lock`, so a second Trash or Erase gets 409 and the
  page shows the running one. The job also holds the library lock, so it
  can't start while a review run or a CLI command is changing the library
  (409, naming it), and they can't start while it runs.
- When a job ends, the gallery re-reads the sidecars of the other items that
  could be copies of what it deleted (same size), so a copy `trash` just
  marked `Owner|trash` shows as "trashed copy" in Trash suggestions without a
  restart, and a copy `forget` deleted drops out.
- A job whose worker PID is gone (checked by PID and its `/proc` start time) is
  reported as crashed, at its step, not as running; its files stay busy while
  the step it started is still running, and nothing re-runs by itself.

Status `<state>/jobs/gallery.json`, output `gallery.log`, lock `gallery.lock`
(and `gallery.mutex`): files 0600 in a 0700 folder, replaced by the next job.
The paths go to the worker on a pipe and are never written; the status names
the files only by device and inode, with a count, the step and ok. An Erase
also logs its command without paths (`forget [2 files] --yes --drive [path]`),
and when it ends (done or crashed) every line of `forget`'s own output is
replaced in place by `[output removed when the run ended: it may name a file]`,
so, as with `forget` on the command line, no name of an erased file is left in
the state folder (the erase log keeps sha256 and size). The dialog that was
watching still has the whole output; a page opened later sees the scrubbed
log. Trash is for junk that isn't private and keeps its output, names
included, until the next job.

## Share-safe copy

**Share-safe copy** (bottom bar, or the large view) asks first, naming the
items and the folder, then runs `media-intake share-copy FILE... --out DIR`
as a job (`POST /api/share`, `{"ids": [...]}`): every metadata field and the
GPS stripped (exiftool `-all=` for images, keeping orientation and ICC; an
ffmpeg stream copy without data streams for videos; [intake.md](intake.md)).
The stripping is share-copy's own; the gallery only picks the files and the
folder:

- `DIR` is a new folder per export, `<share-out>/<YYYY-MM-DD-HHMMSS>` (0700;
  `-2`, `-3` if that exists), under `--share-out`, default
  `~/Pictures/share-safe`. A `--share-out` inside a library root or the state
  folder is refused, so a copy is never taken for a library file.
- The reply and the dialog give the folder (`~/Pictures/share-safe/...`) and
  share-copy's own lines (each copy's path and `gps=none`); the page never
  shows a copy. If a line says `gps=STILL PRESENT`, the title says so: don't
  share that one.
- Only library files the media routes may serve (not flagged, quarantined,
  erased or a symlink, as for Open in the system player), and never two with
  the same file name in one export (share-copy names copies by file name; the
  second would overwrite the first): 400.
- It shares the job slot with Trash and Erase (one at a time, 409 while one
  runs) and holds the library lock while it copies, so nothing deletes or
  rewrites a file mid-copy; the items stay viewable meanwhile. The job's last
  line is jobs.py's summary, `done: 0 of N gone from the library`. Sidecars are
  not copied.

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
HOME, the real `intake.py` for trash and forget, and a demo plugin view; the
job tests swap in a fake `intake.py` whose trash waits on a FIFO, then kill
the server or the worker mid-job, and a plugin POST gets 409 while such a job
holds the library lock (a view declared `post_read_only` does not). Also: the sidebar counts against their
filters, the added-in-N-days filter, a trashed file's copy showing as
"trashed copy" at once, and Open in the system player through a fake
`xdg-open` on PATH (its refusals included). Events: the sidebar counts, the
filter, rename and merge (and their refusals) with `Owner|` and other tags
kept; bulk tags, Set event (and clearing it) and Rotate on a selection; a
share-safe copy through exiftool with no GPS or Model left. `ViewsEndpointsTest`
writes its sidecars as plain XMP, so it runs without exiftool: every new POST
(`/api/event/set`, `/api/event/rename`, `/api/event/merge`, `/api/share`)
refused without the token, with a wrong CSRF value, another Origin or Host
(403) or a non-JSON type (415), 409 while a job holds the library lock, and a
share-safe copy (Pillow's re-encode when exiftool is missing) into its own
0700 folder, with its refusals.

In headless Chromium (`tests/browser/test_browser_gallery.py`, see
CONTRIBUTING.md, Tests): keys, click and shift-click selection, the bottom
selection bar appearing only while something is selected, a sidebar quick
view and its chip, the Trash, Erase (ERASE typed) and Close confirms, the
lightbox video playing by itself, Space pausing and resuming it, seeking by
Range, and the job dialog's progress, re-attach after a reload and crash
message, Erase's status line shown while its output is still empty (a
fake `intake.py` that prints nothing until the test opens a FIFO), and the
Events list (filter, Rename with a refused name first), Set event on a
selection and a Share-safe copy ending in "Done: share-safe copies in ...".

## Not verified

- The layout by eye (sidebar, drawer and rail at phone width, light and
  dark), infinite scroll and HEVC posters through GStreamer in a real
  browser, the "can't decode" fallback in a browser that really can't play a
  file, a real `xdg-open`, and any browser but Chromium.
- Semantic search and Similar through the venv: tested only as "unavailable,
  falls back", not with the real model.
- Start-up time on a large real library read from disk.
- Share-safe copy of videos, HEIC and DNG from the gallery (share-copy's own
  test covers a video; the gallery tests copy JPEGs), and Rename / Merge on a
  real digiKam-written library.
