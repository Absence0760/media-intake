# Gallery (local web page)

`media_intake/gallery.py` is a stdlib-only local web page over the library
that intake files: a day-to-day browser in place of digiKam.

```
media-intake gallery [--drive DRIVE] [--port 0] [--share-out DIR]
```

`python3 -I media_intake/gallery.py` takes the same flags. `--share-out` is
where share-safe copies go (below).

The `media-intake` command runs on the system Python, which has no
pillow-heif, so before it prints anything the gallery re-runs itself (same
flags) under `setup.sh`'s venv, which has pillow-heif and reuses the system
packages; HEIC/HEIF photos then show full size. It does this the way the
review page does (`review_ui.venv_reexec`): not when the Python already has
pillow-heif or already is the venv, and not with `MEDIA_REVIEW_NO_VENV=1`.
Without a venv the gallery runs as it is, and a HEIC's large view shows its
thumbnail with "This is a small preview: HEIC photos need setup.sh's venv
(pillow-heif) to show full size." (or "No preview: ..." when there is no
thumbnail either); `media-intake doctor` reports pillow-heif missing from the
venv as `MISSING`.

It prints exactly one `xdg-open 'http://127.0.0.1:<port>/?t=<token>'` line,
then a note with the item count, the start-up time, how many flagged or erased
files were left out and any plugin tabs. A private browser window keeps the
page out of your history, but `xdg-open` opens a normal window of your default
browser. For a private one, give the URL to the browser yourself with its
private-window flag: `--incognito` for Chrome, Chromium and Brave,
`--private-window` for Firefox. A Flatpak browser runs as
`flatpak run <app-id> --incognito '<url>'`. It runs until Ctrl-C or the page's **Stop server** button.
An assistant may start it and hand you the line; only you open the page and
click.

`--drive` is your backup drive (the folder it is mounted on), passed to `forget` when you Erase, so
copies there go too. The Erase dialog says whether it is included: "and on the
backup drive (<mount>)", or "Not on a backup drive: the gallery was started
without one" (`/api/facets` gives `drive`, the mount or null).

## Layout

- **Left sidebar** (its own scroll; the menu button collapses it to an icon
  rail, remembered per browser; at phone width it is a rail by default and
  opens as a drawer): the views with counts (Library, Suggested junk, any
  plugin tab; quick views **Favorites** (below), **Added in the last 30
  days** = filed into the library in the last 30 days, by the file's inode
  change time, Possible child = `Review|child-in-shot`,
  Untagged, **Dates to check** (below); then **Events** with counts, the eight largest and "All N events";
  then **Places** (below); then, only once people and pets recognition has a store, **People & Pets**:
  each person and pet you named with the number of items tagged with the
  name and a pencil for its name menu, and **Name faces & pets** with "N to
  do", the clusters still to name plus the suggestions waiting, below),
  the search box (a one-line hint when semantic search is off, the
  full reason under "why"), then the filters as collapsible sections with
  counts (open / closed remembered per section) and "Clear filters (N)",
  and at its foot **Keyboard shortcuts**, the `?` list below (left out on a
  touch screen with no keyboard). A rule above each heading (Quick views,
  Events) and a heavier one above the search box set the views apart from
  search and filters.
  Counts are for the whole library, not narrowed by the other filters.
  Collapsed to the rail, each icon has a short word under it (Library, Junk,
  Favorites, Recent, Child, No tags, Names, Search, Filters); the full name stays its tooltip
  and the name a screen reader reads.
- **Sidebar width**: 300 px by default; on a desktop, with the sidebar
  expanded, drag its right edge to make it wider or narrower (the grid
  follows), or Tab to the edge and use Left / Right (16 px; with Shift,
  64 px), Home (narrowest) and End (widest). A double-click on the edge puts
  300 px back. It stays between 240 px and the least of 560 px, half the
  window and the window less 480 px (the main column's own minimum), and
  narrows when the window does; the header compacts (a shorter size slider,
  Stop server as an icon) once the main column is 620 px or less, as it does
  in a window of 900 px or less. The width you chose
  comes back when the window widens again. The collapsed rail and the phone's
  rail and drawer keep their own widths and have no edge to drag. A drag
  the browser interrupts (a cancelled pointer, lost capture) puts back the
  width from before it; no edge key acts while a dialog is open.
- **Main area**: a slim header (view, item count, **Select all**,
  thumbnail-size slider, **Stop server**, and while a Trash, Erase or
  Share runs with its dialog hidden, a "Trash running… Show" button
  that brings the dialog back; at phone width the item count is left out and
  Stop server is an icon, so the header fits beside a long title, which
  shortens with an ellipsis), the active filters as removable chips (in an
  event: the event's display name as the title, no chip for it, and the event's
  bar with its slug, **Display name…**, **Rename…** and **Merge into…**), the month grid. The
  selection actions sit in a bar at the bottom that appears only while
  something is selected (Suggested junk keeps its Apply bar there). It
  floats over the grid while you scroll and takes its own room at the end, so
  the last row is never stuck under it, however many rows its buttons wrap to.
  On a small tile (narrower than 150 px, as at phone width) a burst's badge and
  X button give way to a compact **×N** button on the cover (it opens and
  closes the stack, and is named "Burst: N shots, open stack" for screen
  readers) and an open stack's other shots show their badge as "×N"; on a
  wider tile the labels stay. Tile labels (the badge, the stack button, the
  flags) are 12 px and short, and not cut short: one that doesn't fit wraps
  onto a second line, clear of the checkbox. The one exception is a phone,
  where a tile's flags sit above its star and Share in a tile too small for
  more than two lines: a longer one ends in "…" there so it stays inside the
  tile, and keeps its whole text as its tooltip and in the large view's tags. At phone width the burst help above the grid is one
  line ("Burst stacks: tap ×N to open one." on a touch screen, "Bursts are
  stacked; X opens one." with a keyboard) with a **more** link for the whole
  text (open / closed remembered per browser).
- **The grid's own states** (in place of the tiles, `role=status`):
  "Loading your library…" (with `aria-busy` on the grid) until the first page
  arrives; an empty library: "Your library is empty." with the commands that
  fill it (`media-intake stage <folder> --kind folder --batch first`, then
  `plan`, `scan` and `apply`) and a pointer to Getting started; no results:
  "Nothing matches this search." / "these filters." with a **Clear search** /
  **Clear filters** button, or "Nothing in Untagged right now." with **Show
  the whole library** for an empty quick view or event; Find similar without
  the search index: "Find similar needs the search index: run media-intake
  index in a terminal, then try again." with **Back to the library**. A load
  error is an alert in the grid ("Couldn't load the library", the reason, and
  "restart media-intake gallery" if Try again keeps failing) with **Try
  again**; a later page failing says so in the error message, with Try again,
  and the grid stops asking until then. Opening an item that is gone since
  the list loaded says "That file is no longer in the library. The list will
  refresh." and reloads the list.
- **Touch targets**: at phone width or on a touch screen every control the
  grid shows takes a 44 x 44 px tap: buttons, the rail's icons, the filter
  rows, the size slider, a chip's ×, the tile checkbox (a 22 px box in a 44 px
  hit area), the ×N button and text links (a larger hit area around them).
  The selection bar's **Erase** sits apart from Keep and Trash.
- **Colours** follow the system's light or dark scheme. Every label meets
  WCAG 2's 4.5:1 contrast in both: text on the accent colour (burst badges,
  ×N, the rail's filter count, primary buttons) is white in light and near
  black in dark, and key hints and placeholders use the muted text colour.
- **Large view**: the photo or video fills everything left of the info pane.
  A video starts playing on open (muted, with an Unmute button, if the
  browser refuses sound), Space plays / pauses, Prev / Next play the next one,
  Esc stops it and releases the file. An HEVC video plays from an H.264 proxy
  made on first open, starting within seconds while it is converted
  ("Video proxies" below; `media-intake proxies` makes them ahead of time). When the browser still can't
  decode it (no proxy could be made, or another codec), the thumbnail shows
  large with "This browser can't play this video." (it names HEVC only when
  ffprobe read the video as HEVC: `/api/item` gives the codec) and
  **Open in the system player**, which
  runs `xdg-open` on that one file (below; `xdg-open` comes from Fedora's
  `xdg-utils`, which `media-intake doctor` lists as optional).
  At phone width the photo takes most of the screen with the info pane
  below it; the close button stays pinned at the top right however far you
  scroll, a downward swipe on the photo closes the view (not while scrolled
  down to the info pane), and a reopened view starts at the photo.

- **Messages and dialogs**: the page never uses the browser's own `alert`,
  `confirm` or `prompt`. What an action did shows in a message at the bottom
  (`role=status`) that fades after a few seconds; one with **Undo** (below)
  stays 10 s, longer while the pointer or focus is on it. The message sits
  above the selection bar (on a phone the bar wraps to several rows), never
  over its buttons. An error (the server's own message: a refusal, or a 409
  saying in plain words what is running, below) shows below the header as an
  alert (`role=alert`, read out at once) and stays until **Dismiss**. The
  confirm dialogs (Trash, Erase, Set event, a tag, Stop the server, ...) and
  the shortcut list take focus when they open, keep it inside on Tab, close
  on Esc (or Cancel) and give focus back to where it was; the large view
  keeps Tab inside it too (and its messages).
- **Plain words**: what the page shows never carries a tool's tag namespace
  (`Owner|`, `Review|`, `Label|`, ...), a PID or a lock file's path. One map
  in `gallery.py` (`WORDS`, handed to the page) gives every internal name its
  words, and the server's own messages use it too:

  | Internal | Shown as |
  |---|---|
  | the old Trash suggestions view | Suggested junk (the view) |
  | `Review\|maybe-trash` | Tagged as junk (Flags filter, tile flag, Suggested junk's Why, the large view): one of the reasons a file is in Suggested junk, so its count is not the view's |
  | `Review\|child-in-shot` | Possible child (quick view, Flags filter, tile flag) |
  | `Owner\|keep` (tile flag "kept") | Kept on the tile; "Marked Keep" in the large view's Details; the large view lists no `Owner\|` tag |
  | `xmp:Rating` >= 5 (`fav` in the grid's summary) | Favorite: the star (pressed), the Favorites view, Flags: Favorite |
  | `Owner\|trash` on a copy (flag "trashed copy") | Copy of a trashed photo |
  | `Used\|*` (flag "used") | Used in a post |
  | an AI trash verdict's confidence | Junk score, always to 2 decimals as the filters compare it (tile "Junk score 0.90 (blurry)", the filter's "Junk score at least", the chip "Junk score ≥ 0.70", Suggested junk's "Junk score: 0.7 or more (default)") |
  | a vision-check verdict's reason (`.../trash/<reason>`) | Suggested junk's Why and the tile: Blurry, Accidental shot, Too dark, Packaging, Random object, Junk screenshot, Near-copy, Other; a reason another model wrote shows as written |
  | filters Review, AI, Label | Flags; Junk check (Judged junk / Judged fine / Not checked); Looks like |
  | a tool's tag in the large view | "Project: Personal", "Looks like: food", "Junk check: Judged junk (blurry)" under "Added by the tools", apart from "Your tags" |
  | a 409 (library or job lock) | "a Trash is still running; wait for it to finish, then try again" (or an Erase, a Share, a run from the review page, the media-intake apply command, ...) |

The sidebar rail and width, open sections, the When list's "All years", tile
size and the naming view's crop size live in the browser's `localStorage` (a per-browser convenience; the page
works without it).

## Keys

`?` (not while typing in a field), or **Keyboard shortcuts** at the sidebar's foot,
opens the list of every key the page handles; Esc, `?` or a click outside
closes it, and no other key acts while it is open.

- **Grid**: Enter opens the focused tile; Space ticks it (Shift+Space the
  range from the last one); Shift-click a range, Ctrl-click one tile; a mouse
  drag selects every tile its rectangle touches (Shift / Ctrl adds; Esc while
  dragging puts the selection back); Ctrl+A / Cmd+A selects every tile shown;
  K / T / E Keep, Trash, Erase the selection;
  F stars the selection as favorites, or unstars it when every one already
  is (with nothing selected, the focused tile);
  Esc clears it (or closes the drawer at phone width; in the search box it
  clears the search); `/` jumps to the search box; Ctrl+Z / Cmd+Z undoes the
  last Keep, Favorite, tag, Rotate or Set event while its message shows.
- **Sidebar**: on its right edge (Tab to it), ← / → narrower / wider
  (Shift: bigger steps), Home / End narrowest / widest.
- **Bursts**: X opens or closes the focused tile's stack.
- **Name faces and pets**: Space / Enter on a crop takes it out or puts it
  back (in Suggestions marks it "Not _Name_"); Z on a crop opens the zoom,
  where ← / → step through the card's crops and Esc closes it.
- **Large view**: ← / → previous / next, Space plays / pauses, K / T / E
  (in Suggested junk K / T mark and go to the next), F favorite / unfavorite,
  R / Shift+R rotate, Esc closes.
- **Suggested junk**: arrows (and J) move the outline, K / T mark it (as its
  Keep and Trash buttons do; with no outline yet, the tile under the pointer,
  or the first tile is outlined and the page says so), Shift+K / Shift+T mark
  the range from the last tile given that mark, Enter opens it. Entering the
  view puts focus on its first tile.
- **Dialogs**: Esc cancels, Enter in the text field confirms, Tab stays inside.

`tests/browser/test_browser_gallery_dialogs.py` reads the key names from the
page's own handlers and fails when the list leaves one out.

## What it shows

- Every photo and video under the library, videos and project folders
  (`config.toml`, [configuration.md](configuration.md)). Never the
  media-intake state folder (quarantine, staging, review), never a symlink, a
  hidden folder, or a file whose sha256 `scan` flagged, quarantine holds or
  the erase log lists (`label.flagged()`), nor a photo of a document handed
  to the records inbox and still held until `to-records --finish` erases it
  ([records.md](records.md)).
- **Library**: a timeline grid by month, newest first, 240 items per page with
  infinite scroll; thumbnails load lazily. Click (or Enter on a focused tile)
  opens the large view (the image, or `<video controls>`, which starts playing
  by itself; HEIC/DNG get a 1600 px JPEG made in memory, HEIC only with
  pillow-heif: above) with tags, date,
  place, event, project, source, folder, resolution and size. Arrow keys go to
  the previous/next item, Esc closes, `/` jumps to the search box.
- **Suggested junk** (was "Trash suggestions"): the one place for what
  `media-intake vlm-trash` judged junk, beside the other suggestions:
  `Review|maybe-trash` (Tagged as junk), an `AI|vlm/<model>/trash/*` verdict
  with that model's `conf` >= the view's Junk score (0.7, `vlm_trash.TRASH_MIN`,
  read through `vlm_trash.junk_score` as `suggest-trash` does, unless you
  lower it; or the older
  `Review|vlm-trash/*`), or `Owner|trash` (a copy of content you trashed
  elsewhere, flag "Copy of a trashed photo"); never a file with `Owner|not-trash` (or the legacy
  `Review|not-trash`), `Owner|keep`, digiKam Accepted, a favorite (`xmp:Rating` >= 5),
  `Used|*` or a protected project's `Project|<name>`, at any Junk score. Most confident first,
  big tiles, each saying its junk score and reason ("Junk score 0.90
  (blurry)"), with a line under the title saying what the view is and that
  nothing changes until Apply. A bar above the grid narrows it: **Why** (All
  reasons, each vision-check reason, Tagged as junk, Copy of a trashed photo,
  each with its count at the current score and filters) and **Junk score**
  (0.9 / 0.8 / 0.7 (default) / 0.5 / 0.3 or more, or Any score; it applies to
  vision-check verdicts only). When a lower score would add files, the bar
  says so: **Show N more with a lower junk score** sets Any score. The line
  stays at 0.7 by default because a verdict below it is one the model itself
  counts as "low-confidence trash" (`vlm-trash`'s summary); you reach those
  here, not in a second view. Entering the view starts at the default line
  and All reasons. Library filters you set stay applied; the header then
  says "12 suggested, 5 shown with your filters" (the sidebar's count is the
  view's at 0.7, without filters; its tooltip says how many more a lower
  score holds). Every tile has **Keep** and **Trash** buttons
  (pressed when marked; a second tap unmarks), and a marked tile says
  "Keep" or "Trash" in its corner, not by its outline's colour alone. A tap
  or click on the photo opens it. There are no checkboxes or selection bar
  here: the marks are the only choice. With a keyboard, K / T mark the
  outlined tile, arrows move, Enter opens it. Nothing changes until **Apply**, which shows the counts (and the thumbnails
  and names of what it trashes) and needs a confirm ("Apply", or "Apply and
  trash N files"). Marking many at once, all still waiting for Apply:
  - **Shift-click** a tile's Trash (or Keep), or Shift+T / Shift+K on the
    outlined tile: every tile from the last one given that mark gets it.
  - **Drag** from a tile's Trash or Keep (mouse or pen): every tile the
    rectangle touches gets that mark; Esc while dragging puts the marks back.
  - **Mark all shown as Trash** in the Decisions bar: every tile loaded so far
    (the message says how many more match below, as Select all does).
    **Clear marks** takes every mark off, after asking.

  A bulk mark never overrides a tile marked the other way (the message says
  how many kept theirs; change one on its tile), except the tile you pressed.
  In the large view K / T and its **Mark Keep (K)** / **Mark Trash (T)**
  buttons mark the item and go to the next; no Keep is written and Trash
  doesn't ask, since nothing happens until Apply. Marks belong to the items:
  they survive the grid reloading, a filter, and a trip to another view and
  back; only the tiles listed show them and only those reach Apply (the bar
  says how many more are marked but not listed now). Reloading or closing the
  tab while marks wait makes the browser ask first. Starring an item takes
  its mark off (a favorite is never junk), and unstarring doesn't put it
  back. Apply (`POST /api/decide`) re-reads each sidecar marked Trash first:
  one that is a favorite or kept by then (starred or kept since the mark, on
  this page, another one or in digiKam) is skipped, never trashed, and the
  message says "Skipped N: favorite/kept." (the reply's `trashed` lists the
  ids the Trash job got). Plain **Trash** on a favorite stays allowed: you
  ask for it item by item, and its confirm shows what goes.
- **Favorites**: below ("Favorites").
- **Events**: every event in the library, from the sidecars' `Event|<slug>`
  tags (or `XMP-iptcExt:Event` on a file with no `Event|` tag), with its count;
  an item in two events counts and shows in both. Each row shows the event's
  display name, its slug in the row's tooltip. A click shows that event's
  items (the Event filter; the display name is the view's title, the slug its
  tooltip) and a bar with the display name, the slug beside it when they
  differ, and:
  - **Display name…** (`POST /api/event/name`, `{"event": slug, "name": text}`):
    the name the sidebar and the title show for the event, 1-64 characters on
    one line (runs of spaces kept as one; no control or invisible format
    characters such as zero-width or bidi marks); `""` goes back to the
    default. Refused when it looks like another event's slug or display name
    (the same but for case, width, spacing, or a dash or underscore for a
    space). Stored in the state folder's `event-names.json`, never in a
    sidecar ([configuration.md](configuration.md), `state`). The default is
    the slug prettified (`some-trip-2024` shows as "Some trip 2024"), but
    only when nothing else looks like it: beside an event `A b` (another
    tool's `XMP-iptcExt:Event` text), `a-b` stays `a-b`. Every name is put
    on the page as text, never as markup.
  - **Rename…** (`POST /api/event/rename`, `{"from": old, "to": new}`): every
    sidecar with `Event|old` gets `Event|new` instead. The new name is a slug
    (lowercase letters, digits, dashes, like `plan --event`) that no event has
    yet; joining an existing event is a merge.
  - **Merge into…** (`POST /api/event/merge`, `{"from": [a, ...], "into": b}`):
    every item of each `from` event moves to `b`, which must exist.

  A display name goes with Rename to the new slug (and a name left over for
  that slug, from an event emptied earlier, is dropped, not attached); Merge
  keeps `b`'s and drops the merged events'. When `event-names.json` is
  unreadable, both still go through and say the display names were not
  updated (the file is left as it is; Display name… is refused until it is
  fixed or removed). Rename and Merge rewrite only `Event|` tags (and `XMP-iptcExt:Event` when it named the
  old event), in the sidecars that have them, under the library lock: never an
  original, never `Owner|`, your tags or any other namespace, and a removed
  `dc:Subject` leaf only when no other tag uses it. A later `tag` or
  `tag-files` with an event replaces `Event|` again ([intake.md](intake.md),
  "Tags: who writes what").

  A display name lasts only as long as its event: when Trash or Erase takes
  an event's last file, or Set event moves the last one out, the name you
  typed is deleted from `event-names.json` (Erase leaves no text of yours
  behind), so the event made again later by any path (`apply`, `tag-files`,
  Set event) shows its default name. Undo bringing an emptied event back
  (Undo of a Set event) brings it back with its default name: set it again.
  An event emptied while no gallery ran (a CLI `erase` or `trash`) loses its
  name when the gallery next starts, but only when every library folder is
  there, no file was left out as flagged (an unmounted drive's events
  aren't gone), and the scan saw every folder: a subfolder it could not
  read, a symlinked subfolder or a broken symlink in the library keeps every
  name (the start line says how many it could not read). Dot-folders are
  never part of the library (the gallery never shows them), so an event
  whose only files are in one counts as emptied. Until then it would show
  the old name if made again.
- **Places**: your own named places (My places), each with how many items
  carry it; a click shows them (the My place filter). The large view's
  **Add a place here…** (only on an item with GPS) names a new one at that
  item's point, and an item without GPS can show a **Probably at …**
  suggestion to answer Yes or No. The page never gets a coordinate: see
  [places.md](places.md), "In the gallery".
- **Plugin tabs**: any view a plugin adds through `gallery_views()`, with its
  routes under `/plugin/<id>/` ([plugins.md](plugins.md)).

- **Burst stacks** (Library and the filtered timeline): below.

Dates come from the sidecar's `XMP-exif:DateTimeOriginal`, else the file
name, else the `YYYY-MM` folder (with the file's own time when it agrees),
else the file time. Project and source fall back to where the file sits
(`intake.infer`) when the sidecar has none.

## Burst stacks

Shots of one burst (one camera, a moment apart) show as one stack, the same
look and keys as the review page ([review-ui.md](review-ui.md)): the cover
(best-shot's pick when `best-shot` has run, "Best shot" below; else the
burst's first shot) with a stacked edge, a count badge ("4 shots", read
out as "Burst: 4 shots") and a **Show all 4 (X)** button. **X** or `x` on a focused shot, or
the button, opens the stack (every shot follows the cover, each with a badge
giving its place, "2 of 3", read out as "Burst: shot 2 of 3", and the shots sharing a coloured ring)
or closes it, and focus goes back to the cover either way. A line
above the grid says what a stack is and how actions behave. At phone width
the cover shows a compact **×N** button instead ("Close" while the stack is
open, and the other shots "2/3", "3/3"), and the line is shortened
with a **more** link ("Layout" above).

- **A closed stack acts on its cover only.** Selecting (Space, a click, a
  shift-click range across it, Select all), K / T / E and every bar action reach only
  the cover; the hidden shots are never selected, and closing a stack
  unselects any of its shots that were (the page says how many). Open the
  stack to act on the others. Nothing ever acts on a file you can't see,
  so a Trash or Erase never takes a whole burst by surprise. The large view's
  arrows step over a closed stack's hidden shots.
- **Grouping** is `media_intake/bursts.py`, the review page's rules: same
  device, images only, each shot at most 2 s after the one before, dHashes
  at most 10 bits apart, and a camera's own marker (a Pixel / Huawei burst
  file name, or `burst_id`: the BurstUUID or GCamera BurstID `plan`
  records, [intake.md](intake.md), "Burst id") over the clock. A shot's date
  (with seconds), device, dHash and burst id come from the `plan.json` entry
  intake wrote for it (every batch under `<state>/batches/`, by `dst`, or
  `lib_dst` for a file released from quarantine or review; a later batch
  wins; the dHash also from `index.json`). No file is opened.
- **Never stacked**: a file `plan` sent to review as a near-duplicate (a copy
  of another picture, however close in time and look), videos, and a library
  file with no plan entry unless it carries a camera marker (a burst file
  name, or an `XMP-GCamera:BurstID` some tool wrote in its sidecar): its
  device is unknown, so the clock alone never stacks it.
- **Where**: the timeline (Library, events, quick views and every filter).
  Only the listed shots stack: a filter that leaves one shot of a burst
  shows it as a plain tile, and the badge counts the shots listed. Ranked
  lists (search results, Find similar, Suggested junk) are never stacked,
  since their order is not time. A burst's shots sit together at the place
  of the newest one: the cover first, the others in time order.

### Best shot

`media-intake best-shot` (`media_intake/best_shot.py`; `--dry-run` counts
only) picks the best shot of every burst stack and every near-duplicate set
in the library (photos whose dHashes in `index.json` are at most 4 bits
apart; a shot in both joins them into one set). Best is the sharpest frame
(the variance of the Laplacian of a downscaled grey copy) among those not
badly exposed (more than a quarter of the pixels crushed to black or blown
to white loses to any frame under that); a tie goes to the earlier shot. The
photo is read in memory only; RAW and other files Pillow can't decode are
not scored and get no tag.

It writes one tag per shot, in its own namespace in the sidecar:
`AI|bestshot/cover` on the best, `AI|bestshot/not-best` on the others. A
re-run replaces only those tags (a shot no longer in a set loses its tag).
It reads and writes the sidecars under the library lock ([intake.md](intake.md),
"One writer at a time"): while another writer holds it, it waits, then refuses
and changes nothing; `--dry-run` takes no lock.
Nothing is trashed, moved or deleted: the not-best shots are only offered to
Suggested junk with the reason **Not the best shot** (at any Junk score: the
tag has no score; they come after every scored verdict, and the **Why**
filter lists the reason), for you to Keep or Trash. `suggest-trash` lists
them by the same rule. The cover leads its burst stack. Your decisions win: a shot you kept, trashed, starred or gave a pick
label, one with a person or pet you named, one used in a post or in a
protected project gets no tag (it still counts when choosing the best).
Eyes open is not measured yet (a later step, with the face detector).

## Favorites

A star marks a photo or video you love. On a tile it sits at the bottom
right, just left of the tile's Share button (the two side by side; the flag
text narrowed to stay clear of them, or above them on a touch screen): with a mouse it shows while the pointer or focus is on the tile, and
always once the item is a favorite (filled, gold); on a touch screen it
always shows (a 44 x 44 px tap). Not in Suggested junk, whose tiles carry
only Keep and Trash. The large view has a **★ Favorite (F)** button (a
toggle: pressed when it is one), the selection bar **★ Favorite** and
**☆ Unfavorite**, and **F** toggles the selection (with none, the focused
tile; it unstars only when every one is a favorite already) or the item in
the large view. The **Favorites** quick view lists them, with its count, and
**Flags: Favorite** is the same filter, combinable with the others
(`review=favorite` on `/api/list`).

- **Stored as** `XMP-xmp:Rating = 5` in the sidecar
  (`POST /api/favorite`, `{"ids": [...], "on": true|false}`), and nothing
  else: the standard rating field digiKam shows as five stars and reads from
  the sidecar (digiKam set to "Write to XMP sidecar only",
  [intake.md](intake.md), "Browsing with digiKam"), so a star set in either
  shows in the other. A rating of 5 or more is a favorite, from the gallery or
  digiKam (`owner.FAVORITE`); four stars or fewer are not. Unstarring deletes
  the rating, whatever it was; starring a file digiKam already rated 5
  writes nothing. Not an `Owner|` tag: the rating needs no new tag namespace
  and stays meaningful outside this tool.
- **Never Suggested junk**: a favorite counts as kept everywhere
  (`owner.kept`): the gallery's Suggested junk and its count,
  `suggest-trash`, the re-tag never adding `Review|maybe-trash`, `vlm-trash`
  never judging it (owner-decided), a copy of trashed content never marked
  `Owner|trash`, `semantic --clean` keeping it, and a negative for
  `calibrate`'s maybe-trash. The tile keeps
  no "Kept" flag for it: Keep stays its own decision.
- **Undo** works as for Keep: the message has **Undo** (Ctrl+Z), and it puts
  each item's rating back as it was (four stars back as four, none as none),
  refusing after a later change.

## Dates to check

The files whose date looks wrong, as the last `media-intake check-dates` run
found them ([dates.md](dates.md): clock-reset defaults, a camera newer than
its date, a video format newer than its date, file numbers out of date
order, dates that disagree, copies with other dates), with the count in the
sidebar. Its tiles are grouped by the first thing found (that kind's words
as the heading, the groups in [dates.md](dates.md)'s order), each with
"Suggested YYYY-MM-DD HH:MM" (or "No suggested date") as its flag and every
reason in its tooltip; the note above the grid says when the check ran.
Before any run, and once everything is fixed, the view says to run
`media-intake check-dates` in a terminal. `/api/list?dates=all` (or one kind,
`dates=reset`, ...) lists them; each row carries `dk`, `dq`, `dd` (the date
check-dates saw in use), `dr`, `ds` and `dsrc`. The report is re-read when
it changes, so a CLI run or fix shows on the next reload of the grid.

**Fix date…** in the selection bar (only in this view) asks how: **Use the
suggested date** (disabled when none of the selection has one; the others are
left as they are), **Set the date to** (YYYY-MM-DD, or with HH:MM; a date
alone keeps each file's time of day) or **Shift by** (`+8y`, `-3h30m`, `+2d`),
listing up to ten of the files with their date and suggestion.
`POST /api/dates/fix` (`{"ids", "mode": "suggested"|"set"|"shift", "value"}`)
runs fix-dates' own code under the library lock: the date goes to the
sidecar only and the files stay in their folders; a file that can't take it
is left as before and counted in the message, and a request where none can
is refused (400, nothing written). The message has **Undo**, which puts
exactly that fix back through fix-dates' record (`fix-dates --undo` sees it
too), refusing (409) once a sidecar changed again. A fixed file leaves the
view, and its tile shows the new date elsewhere.

## People and pets

Off by default: nothing of it shows until `media-intake people scan` (or
`pets scan`) has made the recognition store ([recognition.md](recognition.md));
the gallery reads that store and never creates one. Words:
[words.md](words.md), "People and pets".

- **Sidebar, People & Pets** (below Places): each name you gave or confirmed,
  with the number of library items whose sidecar has its tag
  (`People|<Name>` or `Pets|<Name>`). A click shows those items, by the tag
  (`who=People|<Name>` on `/api/list`), with the name as the title. A
  suggestion never counts or shows there. Beside each name a pencil (shown on
  hover or focus, always on a touch screen or phone, 44 px there) opens the
  **name menu** (below). Until a name or a pencil is first used (or **Got
  it**), a one-line hint under the heading says "Click a name to see its
  photos; its pencil button renames it" (remembered per browser). Then
  **Name faces & pets**, with "N to do": the clusters with no name yet (a
  face or pet seen once included) plus the suggestions waiting; collapsed to
  the rail it is the face icon with "Names".
- **The grid of a name** has a bar like an event's: "Person: Ann" or "Pet:
  Rex", its files, clusters and suggestions waiting, then **Rename…**,
  **Merge into…** (only when another name of the kind exists), **Review
  suggestions (N)** (only with some waiting: opens Suggestions, the search
  box set to the name) and **Remove name…**. A `People|` / `Pets|` tag the
  store doesn't hold (another tool wrote it) shows the bar without them.
- **The name menu** (the sidebar's pencil, the large view's ⋯ beside a name,
  the ⋯ of a name's group in Named; an in-page dialog titled with the name,
  focus on its first button, Esc closes it back to the button that opened
  it): **Rename…**, **Merge into…**, **Not this one** (large view only),
  **Review suggestions (N)**, **Show its photos** and **Remove name…**. Each
  acts on the name in every cluster that has it, in one action, under the
  library lock, with one dialog; the tags in the sidecars follow at once
  (only the `People|` / `Pets|` tags the tool wrote; your own tags and an
  owner's `People|` tag stay):
  - **Rename…** (`POST /api/rec/rename-name`, `{"kind", "run", "name",
    "to"}`): the new name, as typed (spaces trimmed, never capitalised), on
    every cluster, confirmed face or pet and waiting suggestion with the old
    one; rejections of the old name follow it. Refused in the dialog: an empty
    or bad name, the name itself, and one that looks like another name of
    the kind (the same but for case, width, spacing, or a dash or underscore
    for a space; case or spacing of the name itself may change). The reply's
    `who` is the new tag, so the grid of the name stays on it.
  - **Merge into…** (`merge-name`, `{"into"}`: picked from a list of the
    kind's other names): every face or pet of the name takes the other one;
    a waiting suggestion you rejected that name for is dropped. Refused
    across species.
  - **Remove name…** (`remove-name`): every cluster and confirmed face or pet
    with the name loses it (and its suggestions go); its clusters are back in
    To name.
  - **Not this one** (`not-here`, `{"item"}`: the large view's file): the
    name's faces or pets in that one file are taken out (as a click on a crop
    in the naming view), its tag comes off that sidecar.

  The message after each has **Undo** (Ctrl+Z): every detection (and
  rejection) the action changed is put back as it was, then the tags, and
  refused once any of them changed again or after a re-cluster. Each POST
  names the cluster run of its kind (`run` in `/api/facets`' `rec`); after
  `people cluster` / `pets cluster` it is a 409 that says to reload, the
  page reloads the names, and nothing changes.
- **Name faces and pets** (that entry; in the main column, not a separate
  server): People or Pets (when the store has both), then **To name**,
  **Suggestions**, **Named** and **Not a person** / **Not a pet**, each with
  its count. A cluster is a card with up to 24 crops, largest first, loaded
  as you scroll; 30 cards a page, **More clusters** for the next.
  - **Crop size** (the header's slider, in place of the grid's thumbnail
    size; remembered per browser): about 160 px on a desktop and two a row
    on a phone by default, from 80 to 320 px, never wider than the card.
    Crops are served at 320 px, so they stay sharp at the largest size.
  - **Single photos (N)**: To name lists the clusters of two or more first,
    then a heading and the faces or pets seen only once (one photo, or one
    frame of a video), with the same Name, Merge into…, Not a person / Not
    a pet and "Looks like Ann." hints. They count in the tab and in the
    sidebar; once named (or marked Not a person / Not a pet) one shows under
    Named (or Not a person / Not a pet) like any cluster. A section of their own rather than mixed in: a scan's singles
    often outnumber its clusters and would push the clusters off the first
    pages.
  - **To name**: type a name (the box suggests the names you gave) and press
    Enter or **Name**: every face of the cluster gets it, and its tag is
    written into the sidecars of the files it is in at once (under the
    library lock, like Keep). The message has **Undo** (Ctrl+Z), which takes
    the name and the tags back off. **Merge into…** asks for a name you gave
    or a cluster number; a cluster close to one you named says "Looks like
    Ann." with **Merge into Ann…**. **Not a person** / **Not a pet** ignores
    the cluster: never tagged.
  - A click (or Enter / Space) on a crop takes that one face or pet out of
    the cluster's name (its tag comes off that file; the message has Undo),
    a second one puts it back. A taken-out crop is shown crossed out
    (`aria-pressed`).
  - **Zoom**: the magnifier in a crop's corner (its own button, 32 px, 44 px
    on a phone or touch screen), or Z on a focused crop, opens a dialog with
    the crop large and the whole photo with the face or pet outlined (turned
    as the large view shows it); for a video, the frame it was seen in and
    the video's poster, with the second. Left / Right step through the card's
    crops, its toggle takes that one out or puts it back (in Suggestions:
    "Not Ann") exactly as a click on the crop, and Esc closes it with focus
    back on the crop it showed.
  - **Suggestions**: one card per name, "Looks like Ann: 42", for faces that
    joined a cluster you named since. Click a crop that isn't Ann to mark it
    ("Not Ann"); **Confirm all 42** (or "Confirm 40, reject 2") and
    **Reject all** ask first. Confirmed ones are tagged; rejected ones are
    never suggested that name again.
  - **People** / **Pets**: always shown above the tabs (one kind too), each
    with its icon and "N to do".
  - **Find a name or a cluster number** (a search box beside them, kept
    across the tabs, emptied for the other kind): the clusters whose name
    holds the text (case and spacing aside) or the cluster with that
    number; in Suggestions, the names that hold it. "N clusters with "…" in
    the name (or that number)." says what it found; Esc empties it.
  - **Named**: grouped by name, alphabetically: one heading per name with its
    number of clusters, **Rename all…** (the name everywhere, as the name
    menu's Rename…) and ⋯ (the name menu), then its clusters. A card's own
    Rename (**Rename this cluster** when the name spans several) renames that
    cluster only; Merge into…, Unname… (asks; the tags it wrote come off).
    **Not a person**: **Not ignored** puts a cluster back to name.
  - A card's merge, rename and unname, and confirm / reject, say "This can't
    be undone here" in their dialog: the store has no step back for them (you
    can still unname, take out or name again). The name-level actions above
    (Rename all…, the name menu) can be undone from their message.
  - Every POST names the cluster run the page listed; after `people cluster`
    (or `pets cluster`) ran, it gets 409 "The clusters changed since this
    page loaded…" with **Reload the list**, and nothing changes. An Undo
    from before a re-cluster is refused the same way.
- **Large view**: "People: Ann, Bo" and "Pets: Rex" under Details, from the
  tags you confirmed (only there: not under "Your tags" or "Added by the
  tools", and "+ tag" refuses `People|` / `Pets|`); each name shows the grid
  of that name, and the ⋯ beside it opens the name menu with **Not this
  one**. For a video,
  "Ann appears at 0:12, 1:40" (the times the store saw Ann, one a second at
  most) seeks the player. Each time is the second of the frame
  `media_intake/frames.py` sampled the face or pet in
  ([recognition.md](recognition.md), "Videos").
- **Crops** are biometric: served only as `/r/<detection id>` (no path) and
  the zoom's whole picture as `/r/<detection id>/frame`, made on demand by
  `recog.crop_bytes` and `recog.frame_bytes` into the store's own `crops/`
  (folder `0700`, files `0600`, every name starting with the content's
  sha256, so erase and verify-gone find them all; never the thumbnail
  cache), with `no-store` like
  every response, and never for a file that is flagged, quarantined,
  erased, trashed, in a running Trash or Erase, or not in the library the
  gallery lists. Share copies never include them. A video's crop is cut
  from the same sampled frame again (frames.py); a video that can no longer
  be decoded shows an empty square.
- **Suggested junk**: a file with a name you gave or confirmed (its
  `People|<Name>` or `Pets|<Name>` tag) is never suggested as junk by a
  label or the vision check (`owner.named`); a copy of something you
  trashed still is, since a name is no Keep.
- API: `GET /api/rec?kind=face|pet&show=todo|sugg|named|ignored&after=<cluster>`
  (`&single=1` when that cluster was seen once; each cluster says `single`,
  and `counts.single` how many of To name were seen once; Named is ordered by
  name, `&aname=<name>` with the last card's name, and `spans` says how many
  clusters each name has; `&q=` the search box, `found` how many matched);
  `/api/facets`' `rec.kinds[]`: `names` as `[name, files tagged, clusters,
  suggestions waiting]`, `unnamed`, `suggestions` and `run`;
  `GET /api/rec/det/<detection id>` (the zoom: its item, file name, video
  or not, second, and box on `/r/<id>/frame` as fractions);
  `POST /api/rec/name|ignore|unname|merge|out|back|answer|rename-name|merge-name|remove-name|not-here` with
  `{"kind", "run", ...}` (`cluster`, `name`, `into` a cluster number or a
  name, `ids` detection ids, `confirm` / `reject` id lists; by name: `name`
  and `to`, `into` or `item`); the token, Host,
  Origin and CSRF checks of every POST (403), JSON only (415).

## Filters (all combinable)

Project, Source, Event, Place (the city: a `Place|` tag with a comma), My place
(`myplace=<id>`: one of your named places, from the Places list), year or month, Looks like (any `Label|*`), my
tags, Flags (Tagged as junk = `Review|maybe-trash`, Possible child =
`Review|child-in-shot`, Untagged = `Review|untagged-*` or no tags, Favorite =
`xmp:Rating` >= 5), Junk check
(Judged junk with a junk score >= N, its count following the slider, Judged fine, Not checked), Used (in a
post; or not in the last N days, blank = never), photos / videos, and added
in the last N days (`added=N` on `/api/list`, what Added in the last 30 days
sets).

**When** lists the five most recent years (each opens to its months), then
**All years (N)** for the rest (**Fewer years** shuts it; remembered per
browser); a year or month you picked stays listed either way.

**Added in the last 30 days** (was "Recently added") is named by its window.
It counts files whose inode changed in the last 30 days (`RECENT_DAYS`): the
copy or move into the library, since a file's own dates say when it was
taken, not filed. Right after a first import it holds the whole library, and
the name says why. A "last import" view was not chosen: the gallery knows a
file's batch only through `plan.json`, the batch folders' times change with
later commands (review, purge-review), and one big first import would equal
the library just the same.

In Suggested junk (`view=suggest`), `/api/list` also takes `why` (a reason,
`label` for Tagged as junk, `copy` for a copy of a trashed photo) and `jmin`
(the Junk score floor, 0..1, default 0.7; not a number means the default),
and answers `junk`: `all` (listed without Why), `unfiltered` (the view with
no other filter or search), `why` (`[value, n]` each), `floor`, `line` and
`lower` (how many more `jmin=0` would list). Each item carries `r`, its
verdict's reason.

`GET /api/facets` gives the sidebar its options and counts: `facets` (per
project, source, event, place, label, month and year, my tag), `counts`
(`kind:photo`, `kind:video`, `review:<flag>` (`review:favorite` too), `ai:trash|keep|none`,
`used:used|unused`, `suggest` (Suggested junk at 0.7), `suggest:lower` (how
many more a lower Junk score adds), `recent`), `ai_scores` (`[score, n]`
for every Judged junk score, to 2 decimals: the page sums those at or above
its slider), `recent_days`, the search
status, `share_out` and `drive` (the backup drive Erase includes, or null), `places` (`{id, name, n}` per
named place, most items first), `places_error` (why they can't be read, or "") and `radii` / `radius` (Add a place
here…'s choices and default, metres). Like every route it needs the session cookie or token.

The search box runs `semantic.py search` (local SigLIP text model, through the
media-intake venv). When the venv, the embeddings or the text model are
missing, the page says why and falls back to matching tags, file names and
folders. **Find similar** on any item runs `semantic.py similar`. Both need
`media-intake index` to have run ([search.md](search.md)).

## Actions (one item, or a selection)

Tick the box on a tile (shift-click a second box selects the range between,
in the grid as it is now: after the grid reloads, for another view or filter,
a shift-click starts a new selection; Ctrl-click on a tile ticks or unticks
it; Space on a focused tile ticks it; **Select all** in the header, or Ctrl+A /
Cmd+A, ticks every tile shown; or drag a rectangle, below), then the bottom bar's buttons or keys:
**K** Keep, **T** Trash, **E** Erase, **+ tag** / **− tag** (a field that
suggests your tags as you type, from `/api/facets`' My tags, and refuses the
tools' namespaces before anything is sent, as the server does; it is also how
you add `Calibrate|<label>|yes` or `|no` marks for `media-intake calibrate`,
[configuration.md](configuration.md), "Calibrating label thresholds"), **Set event…**,
**Rotate ⟳**, **Share**; Esc or **Clear selection** clears it. Each
applies to every selected item through the same endpoint as one item, photos
and videos alike (one unknown or removed id refuses the whole request, and so
does a file deleted since the gallery scanned, by a CLI `trash` say: 400,
nothing written, so no sidecar is made for a file that is gone, and it leaves
the grid). The replies to Keep, my tags, Rotate and Set event carry the
items' new grid summaries (`"items"`), so the tiles' flags ("Kept", ...)
change at once; the selection stays for the next action. Keep's message is
"Kept 3. They won't be suggested as junk again." In the large view the keys
act on the item shown; **R** / **Shift+R** rotate (its buttons **Rotate left
(Shift+R)** and **Rotate right (R)**), and its buttons include Set event
and **Find similar**; **Share** sits in its header, beside the name. Each tile
(not in Suggested junk) also has a **Share** button at its bottom right corner (the star beside it), shown
on hover or focus (always on a touch screen), for that one item without
selecting it first.

**Select all** ticks every tile the grid shows now: the current view, filters
and search, and only the pages loaded so far (the grid loads 240 at a time as
you scroll). A closed burst stack adds its cover only. The bar says what it
took: "Selected 240 (all shown)", or, while more match than are loaded,
"Selected 240 (all shown; 1,200 more match below: scroll to load them, then
Select all again)"; with a closed stack, "closed stacks: cover only". It never
reaches a file the page hasn't shown, so a Trash or Erase after it can't take
more than you scrolled past, and they ask exactly as for any selection (the
count and names, then Confirm, or ERASE typed). Ctrl+A in the search box or
any other text field selects that field's text, as usual; Select all is not
offered in Suggested junk (K / T and Apply there) or a plugin tab. Ticking
or unticking a tile afterwards, opening or closing a stack, or a new page
loading turns the line back into a plain count. Esc clears.

**Drag to select** (mouse or pen): press on a gap in the grid or on a tile's
photo (not its checkbox or buttons: the star, Share) and drag; every tile the rectangle touches
is selected, replacing the selection (with Shift, Ctrl or Cmd held at the
press, adding to it). A press that moves less than 6 px is a click as before
(it opens the item, or ticks with Shift / Ctrl). Near the window's top or
bottom edge the page scrolls by itself, and tiles a page load adds inside the
rectangle are taken in too, so it still never reaches a tile the page hasn't
shown. A closed stack gives its cover only. Esc while dragging, or the browser
taking the gesture, puts the selection back; the count is read out
(`#selsay`, a status region). On a touch screen a drag scrolls, as ever:
selecting there stays the checkbox and Select all, since a long-press would
fight the browser's own (image menu, text selection) and a drag can't be both
a scroll and a selection.

| Action | What it writes |
|---|---|
| Favorite | sidecar `XMP-xmp:Rating` = 5, or deleted to unstar (Favorites, above). Nothing else. |
| Keep | sidecar: `Owner\|keep` and `Owner\|not-trash` added; `Review\|maybe-trash`, an older `Owner\|trash` and the legacy `Review\|not-trash` removed (each leaf only when no other tag uses it). Nothing else. The same rule as the review page's Keep (`intake.keep_args`). |
| Trash | `media-intake trash FILE... --yes` (the page has already asked): plain delete of the file, its sidecar, thumbnails (freedesktop and the gallery's) and index entry; the sha goes in `trashed.json` (the durable `Owner\|trash`) so intake never takes it in again, and library copies of it left on disk get `Owner\|trash` in their sidecar. The same command the review page's Trash runs. Summary + confirm. |
| Erase | `media-intake forget FILE... --yes [--drive]`: the erase-everywhere path (quarantine, sweep of HOME, sidecars, thumbnails, recent files, erase log, verify-gone). Summary + type ERASE. For anything private. |
| My tag + / - | sidecar `HierarchicalSubject` + its `dc:Subject` leaf. Refused for the tools' namespaces (`Project\|`, `Source\|`, `Event\|`, `Place\|`, `Label\|`, `Review\|`, `AI\|`, `Used\|`, `Owner\|`, `People\|`, `Pets\|`: names go through the naming view); removal only touches your tags, and keeps a leaf another tag still uses. |
| Rotate | sidecar `XMP-tiff:Orientation` only (1/6/3/8), each item from its own orientation. The original is never re-encoded or written; digiKam reads the same field. |
| Set event | `POST /api/event/set` (`{"ids": [...], "event": slug}`): sidecar `Event\|<slug>` (and `XMP-iptcExt:Event`), replacing the item's other `Event\|` tags; `"event": ""` takes the items out of every event. Nothing else. |
| Share | nothing in the library: `media-intake share-copy` into a new folder (below), then that folder opened in the file manager. |
| Open folder | nothing: `POST /api/share/open` (`{"job": id}`) runs `xdg-open <folder>` (an argument list, no shell, detached) on the folder this server made for that Share job, after it ended (below). |
| Undo | `POST /api/undo` (`{"undo": token}`): puts back what one Keep, Favorite, my tag + / -, Rotate or Set event changed, below. Never offered for Trash, Erase, Share, Suggested junk's Apply, Rename or Merge. |
| Open in the system player | nothing: `POST /api/open` with one id (`{"ids": [id]}`) runs `xdg-open <file>` (an argument list, no shell, detached) for the one item, only on that button's POST, and only for a regular file inside a library root after resolving links, not in a running job and not flagged, quarantined or erased (else 400). |

### Undo

The message after **Keep**, **Favorite** / **Unfavorite**, a tag of yours
added or removed (bar or large view), **Rotate**, **Set event** and **Fix
date** (Dates to check, above: undone through fix-dates' own record) has an
**Undo** button (or Ctrl+Z / Cmd+Z
while it shows). Trash, Erase and Share never offer it: a deleted
file can't come back, and a copy made is out of the library.

- **What it restores**: when the action writes, the server reads each sidecar
  as it was and as it is after, and keeps the difference (the last 50
  actions, in memory, until the gallery stops); the reply carries a token.
  Undo applies the reverse: the same tags (`HierarchicalSubject`) and
  `dc:Subject` leaves as before, `XMP-tiff:Orientation`,
  `XMP-iptcExt:Event` and `XMP-xmp:Rating` set back, or deleted when there
  were none. For Keep
  that means `Owner|keep` / `Owner|not-trash` off again (unless they were
  there) and `Review|maybe-trash`, an older `Owner|trash` and the legacy
  `Review|not-trash` back with their leaves, exactly as each item had them.
  A sidecar the action had to create stays, holding none of it. Tags come
  back as the same set; their order in the sidecar may differ (it is an
  unordered list).
- **Only that action's change**: the record is the server's, never the
  page's, and an undo refuses (400, nothing written) a difference outside
  what that action writes: Keep only its `Owner|` / `Review|` tags, my tags
  only tags of yours (never the tools' namespaces), Set event only `Event|`
  tags and `XMP-iptcExt:Event`, Rotate only the orientation, Favorite only
  the rating.
- **Never over a later change**: it holds the library lock like the action
  (409 while a job, a review run or the CLI holds it; the token stays good),
  and it refuses with 409, writing nothing, when any of the items' sidecars
  changed since (another action, `media-intake tag`, digiKam: their bytes
  are compared), or an item left the library. Each token undoes once.
- The reply carries the items' grid summaries, like the action's own.

Every action holds the library lock ([intake.md](intake.md), "One writer at
a time"): Keep, Favorite, my tags, Rotate, Set event, Undo, Rename, Merge and a plugin view's
POST for the write ([plugins.md](plugins.md)), Trash, Erase and Share
for their whole job. While a CLI command (`apply`, `erase`, ...), a review run or
another gallery's job holds it, the action gets 409 (`{"error": ..., "busy":
true}`) and nothing is written; browsing is never locked. The error says in
plain words what is running and what to do ("a Trash is still running; wait
for it to finish, then try again"; or an Erase, a Share, a run from
the review page, "the media-intake apply command", ...), never a PID or the
lock file's path, and while a gallery job runs the alert has **Show
progress**.

The dialog before Trash or Erase shows up to 12 of the grid's thumbnails ("and
N more"), the names, and in plain words what happens: Trash "Deletes these 2
files from this computer for good, with their tags and thumbnails. They don't
go to the desktop Trash, and this can't be undone. If the same photo is
imported again, it is skipped."; Erase "Erases every copy of these 2 files: in
your library and the rest of your home folder, with their thumbnails and
recent-file entries", and whether the backup drive is in, then "This can't be
undone." It warns when any were used in a post, marked Keep earlier ("2 of
these were marked Keep earlier.") or are favorites ("1 of these is a
favorite."). The button names the action: **Trash 2
files**, **Erase 2 files** (after ERASE is typed). Trash and Erase run one at
a time with their output streamed into the dialog.

Trash, Erase and Share run as a detached job (`media_intake/jobs.py`, the same module
as the review page), so closing the page or stopping the server never stops
one that has started:

- The dialog drops the confirm text and shows what the job is doing from the
  moment it starts, before any output exists (title "Erase running"; status
  "Erasing… looking for copies in your home folder. This can take a few
  minutes; you can close this page, it keeps running.", since the sweep of
  HOME can print nothing for minutes), then polls the job and shows its output
  as it arrives. There is no stop: a half-done erase must finish. So the
  dialog's one button is **Hide (it keeps running)**; hidden, the header's
  "Trash running… Show" button brings it back. When it ends the title says
  "Done" or "Finished with problems", the status line (`role=status`, read
  out) says how it ended ("Done: 2 files trashed.", "Done: 1 file erased
  everywhere."), and the button becomes **Close**. A Trash that went well
  closes its dialog by itself: the end shows as a message ("Done: 2 files
  trashed.") with **Details**, which opens the dialog again on the job's
  output. If the dialog was hidden, an Erase or Share ends in that message
  too (without Details), and any job ends in an alert with **Details** when it
  finished with problems or crashed.
- After a Trash that went well the trashed tiles leave the grid where they
  are, with no reload: the grid keeps its place (the first tile in view stays
  where it was on screen), and a burst that lost its cover takes the next shot
  as its cover. In the large view on a trashed item, it moves on to the next
  item (the one before at the end of the list; it closes when nothing is
  left). In Suggested junk, an Apply that trashed files drops every decided
  tile (kept and trashed) the same way. When none of the trashed items is
  loaded on the page (a Trash from another tab), the list reloads. An Erase, a Share or a Trash with
  problems reloads the list as before. A page opened (or
  reloaded) while one runs, from this server or a restarted one, opens the
  dialog on the running job; the files it acts on stay hidden from thumbnails
  and media until it ends.
- One at a time across every gallery page and server: an `flock` on
  `<state>/jobs/gallery.lock`, so a second Trash, Erase or Share gets 409 and the
  page shows the running one. The job also holds the library lock, so it
  can't start while a review run or a CLI command is changing the library
  (409, naming it), and they can't start while it runs.
- When a job ends, the gallery re-reads the sidecars of the other items that
  could be copies of what it deleted (same size), so a copy `trash` just
  marked `Owner|trash` shows as "Copy of a trashed photo" in Suggested junk
  without a restart, and a copy `forget` deleted drops out.
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

## Share

**Share** (bottom bar, a tile's own button, or the large view's header; its
tooltip "Share: make copies without location (GPS) or other hidden info, in a
new folder you can send from. Your originals don't change.") makes share-safe
copies. It asks first ("Share 1 item?"), with the thumbnails, names and folder
("Makes copies without location (GPS) or other hidden info, in a new folder
under ~/Pictures/share-safe, then opens that folder so you can drag them into
a chat or an email. Your originals are not changed."), then runs `media-intake share-copy FILE... --out DIR`
as a job (`POST /api/share`, `{"ids": [...]}`): every metadata field and the
GPS stripped (exiftool `-all=` for images, keeping orientation and ICC; an
ffmpeg stream copy without data streams for videos, which also drops a video's
cover art, an attached JPEG with its own EXIF, and its subtitles; a motion
photo's copy is the still alone, its appended video cut off, and one that would
still carry a video is removed and the job fails; [intake.md](intake.md)). The stripping is share-copy's own; the gallery only
picks the files and the folder. share-copy also checks everything before it
copies anything: it refuses, writing nothing, an `--out` where a copy would
land on its source or inside a library root or the state folder, two files
with the same name, and a missing file. The dialog then says "Finished with
problems". The gallery's side:

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
- **Open folder.** When the job that Share click started ends cleanly (ok, and
  no `gps=STILL PRESENT` line), the page asks the server to open the new
  folder in the desktop's file manager, so the copies can be dragged into a
  chat or an email: automatic, because the click was yours, and once. The
  dialog (or, when it was hidden, the message) then says "Opened
  ~/Pictures/share-safe/... in the file manager" and offers **Open folder**
  again; a page that re-attached to the job (a reload, another tab) only
  offers the button. `POST /api/share/open` takes the job id, never a path:
  the server keeps the folder it made for each of its last 20 Share jobs and
  opens only that one, only once the job has ended, and only while it is
  still a real folder directly inside `--share-out` after resolving links (a
  symlink swapped in, a moved or deleted folder, an unknown job: 400). It runs
  `xdg-open <folder>` as Open in the system player does (argument list, no
  shell, detached). Without `xdg-open` (Fedora's `xdg-utils`, which
  `media-intake doctor` lists as optional) the reply is 409 "can't open the
  folder: xdg-open is not installed ...", and the page says so with where the
  copies are. A server restarted since the Share has no folder to open: open
  it yourself.

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
`~/.cache/media-intake/thumbs/<sha256>-r<turn>.jpg` (folder 0700, files 0600):
keyed by the content and the clockwise turn baked in (0, 90, 180 or 270: the
sidecar's `XMP-tiff:Orientation` on top of the file's own EXIF orientation,
which is part of the content). So a byte-identical copy rotated in its own
sidecar shows its own rotation, and copies turned alike share one file.
Rotate shows the new turn at once (the next request makes, or reuses, that
turn's thumbnail; a video's frame too: the page turns the player itself),
leaving the other turns for any copy that still shows them
(`media-intake prune-cache`, below, removes the ones none does).
They are copies of photo content, so `trash`, `forget` and `erase` delete
every turn of that sha256 (and `<sha256>.jpg`, the content-only key older
versions wrote), and `verify-gone` reports any one still there as STILL
PRESENT. Previews of formats a browser can't show are made in memory, never on
disk. Safe to delete the whole folder at any time; it rebuilds.

### Pruning the cache

Rotating keeps the old turn, so a photo can hold up to four thumbnails plus
the legacy `<sha256>.jpg`, and content that leaves the library some other way
than `trash`, `forget` or `erase` (moved or deleted by hand) keeps its
thumbnails and proxy. `media-intake prune-cache --dry-run` counts them (and
their size); `media-intake prune-cache` deletes:

- every `<sha256>.jpg`: the content-only key older versions wrote, never
  served now;
- a `<sha256>-r<turn>.jpg` when no library copy of that content shows that
  turn (the sidecar's orientation on top of the file's own, as above);
- every thumbnail, `proxies/<sha256>.mp4` and `proxies/<sha256>.failed` (the
  record that no proxy could be made) of content no longer in the library. A
  proxy or record of content still there is kept.

"In the library" is what the gallery lists: the library, videos and project
folders, never the state folder or a flagged file (so a flagged file's cached
copies go too). Each file's sha256 comes from `index.json` when its size
matches, else it is hashed (slow once for a large folder intake didn't file).
When a library folder isn't there (a project folder on an unplugged drive) or
a file can't be read, what the library holds is unknown: only the legacy
thumbnails go, and the summary says why.

Only names the gallery writes are touched (`<sha256>.jpg`,
`<sha256>-r<0|90|180|270>.jpg`, `<sha256>.mp4`, `<sha256>.failed`), and only
regular files: a symlink under one of those names is never followed or
removed (the summary counts it), and a folder in the cache path that is a symlink is not entered.
Temporary files and the proxy work folders (`proxies/.work-*`) are left alone
(the gallery and `media-intake proxies` sweep stale ones at start, below).
Originals and sidecars are only read. Output is counts and sizes, never file
names. The real run holds the library lock (docs/intake.md, "One writer at a
time"); `--dry-run` writes nothing and doesn't. With the gallery open, a
thumbnail deleted mid-request shows again on a reload. The gallery doesn't
prune on its own: run the command now and then, or after a big reorganisation.

### Free up space

`media-intake space` reports what takes room and how much each clean-up would
free. It is read-only: it deletes, moves and writes nothing and takes no lock.

- **Largest videos**, by size.
- **Exact copies**: library files with the same content (sha256 from
  `index.json` when the size matches, else hashed, and only for sizes two
  files share). Keeping one of each frees the rest.
- **Near-copies kept in the library**: files `plan` set aside as a
  near-duplicate that were kept into the library since (from each batch's
  `plan.json`). Nothing is compared anew.
- **Review folders**: per batch, how many near-duplicates and junk `apply`
  set aside and their size (counts only, no file names);
  `media-intake purge-review BATCH --yes` frees one.
- **Suggested junk**, exactly as the gallery's view shows it at its default
  Junk score (0.70), with each file's Why. Never a file you kept or starred,
  used in a post or in a protected project.
- **Gallery cache**: thumbnails and video proxies, and what `prune-cache`
  would free now (its dry run).

It lists what the gallery lists: never a flagged, quarantined or erased file,
nor a photo held for the records inbox (a trashed file is gone). Sections can
overlap (a Suggested junk file may also be a copy), so their sizes are not
added up. Each list shows the 10 largest; `--top N` changes that (`--top 0`
lists every one) and `--json` prints one object for scripts. Paths under your
home folder show as `~/...`. Acting on it is up to you: the gallery's
Suggested junk view, `trash FILE... --yes`, `purge-review`, `prune-cache`.
Links from the report into the gallery are not done yet (ROADMAP).

## Video proxies (HEVC in the browser)

Chromium on Linux, and often Firefox, can't play HEVC (H.265), which most
phones and GoPros record. When the large view asks for a video whose first
video stream ffprobe reports as `hevc`, the gallery makes an H.264 MP4 proxy
and serves that instead; every other video is served as it is.

- **Made on demand**, on the first open, one transcode at a time, on a
  thread of its own. The page doesn't wait for the whole file: the proxy is
  written as 1 s MP4 fragments, and `/m/` streams them to the player as they
  are written (a `200` with no length and `Accept-Ranges: none`, since the
  size isn't known yet), so playback starts after a second or two while the
  large view says **Converting this video for the browser**; seeking past
  what has arrived works once the proxy is done. The player's other
  requests, another page, or a duplicate file with the same content follow
  that same transcode (it is made once). Opening a *different* HEVC video
  cancels a transcode nobody is watching any more (you moved on; its next
  open starts it again) and converts this one; while another page still
  watches one, the video you opened gets its original at once and the page
  says another video is being converted. Longer side at most 1920 px (never
  scaled up, rotation kept), sound re-encoded to AAC (ffmpeg) or copied
  (GStreamer), no metadata copied. When the transcode ends, the fragments are
  rewritten as a plain MP4 with its index first (`ffmpeg -c copy -movflags
  +faststart`, nothing re-encoded), which is what the cache keeps and what
  every later open gets, with Range. The stream ends as soon as the tool
  has written its last fragment (not after that rewrite). If an attempt fails
  after its first bytes were sent (the GPU encoder giving up mid-file, say),
  that reply simply ends early; the next open follows the next attempt. A
  paused player that stops reading keeps its reply (and so its transcode)
  open, as a watched one.
- **Ahead of time**: `media-intake proxies [--dry-run] [--inhibit]
  [--retry-failed]` makes every missing proxy now, one at a time, with the
  same commands and cache, so the gallery never converts. It reads the
  library as the gallery does (never the state folder, never a flagged,
  quarantined or erased file), checks each video again just before and after
  its turn, deletes a proxy whose video was trashed or erased meanwhile,
  prints counts and the path of each video it converts, and exits 1 when one
  failed this run (the gallery still plays its original; in a `set -e`
  script, run it last or add `|| true` so later steps still run). A video
  that failed before is skipped and counted (`N failed before, skipped`);
  `--retry-failed` tries those again. Stop it any time (Ctrl-C loses only
  the one in progress) and run it again to go on; `--inhibit` keeps the
  laptop awake (`systemd-inhibit`).
- **How**: ffmpeg first, only when it has an HEVC decoder (`libx264`, else
  `libopenh264`); Fedora's `ffmpeg-free` has none, so it isn't run there.
  Then GStreamer's GPU decoder (`vah265dec` or Vulkan, as for thumbnails)
  into `vah264enc` (VA-API: decoded, scaled and encoded on the GPU) and then
  one CPU encoder (`x264enc`, else `openh264enc`), each with the sound when it
  is AAC, then without. An attempt whose output hasn't grown for 2 minutes
  (`PROXY_STALL`: a GPU encoder that hung, using no CPU) is stopped and the
  next command is tried, so a hang ends on the CPU encoder instead of waiting
  out the hour; one still writing after an hour (`PROXY_TIMEOUT`) ends the
  video, since the next command would take as long. `media-intake doctor`
  lists an H.264 encoder as optional (`gstreamer1-plugin-openh264`) and says
  whether it runs on the GPU. Measured on a synthetic 25 s 4K/60 HEVC clip
  (AMD iGPU, VA-API): the first bytes reached the player after 1.7 s, against
  77 s before (the whole transcode, `openh264enc` at 4K, was waited for); the
  whole proxy took 27 s (`vah264enc`, 1080p) against 77 s. Nothing is
  fetched; the original is only read.
- **File names never reach a pipeline.** ffmpeg gets the original as a
  `file:` path. `gst-launch-1.0` re-parses its arguments as one pipeline, so a
  name holding `!`, quotes or spaces could add elements: it reads the
  original through a link `in` in a private work folder
  (`proxies/.work-*`, 0700) and writes `out.mp4` beside it, both paths made
  by the tool; if that folder's path holds anything but letters, digits and
  `._/+-` (an unusual HOME), GStreamer is not tried, and the gallery says so
  once per folder on stderr (`[gst] fallback skipped: ...`, the path only).
- **Cached** in `~/.cache/media-intake/proxies/<sha256>.mp4` (folder 0700,
  files 0600), written in the work folder first (removed afterwards) and kept
  only when ffprobe reads it back as H.264. They are copies of video content, so
  `trash`, `forget` and `erase` delete them by sha256 with the thumbnail and
  the `.failed` record ("When it fails", below), and `verify-gone` reports one
  that is still there as STILL PRESENT. A
  transcode whose video goes into a Trash or Erase job stops (its process
  ends, its stream to the page ends, nothing of it is kept), and one finished
  just after its video was trashed or erased is deleted. Stopping the
  gallery (the page's button, Ctrl-C, or SIGTERM from `kill` or systemd)
  stops its transcode too: the encoder runs in a process group of its own,
  which the gallery kills, and its work folder is removed before the gallery
  exits (130 on Ctrl-C, 143 on SIGTERM). `media-intake proxies` does the
  same on SIGTERM.
- **Left-over work folders.** Each work folder is named
  `.work-<pid>-<random>` after the gallery or `proxies` run that made it,
  and holds the encoder's PID in `pid`. If that process was killed outright
  (SIGKILL, a crash, a power cut), the next gallery or `proxies` start
  removes the folder, first killing an encoder still converting there (its
  PID is checked against its command line, which names the folder, never
  matched by name). A folder whose owner still runs is left alone; one from
  an older version (no PID in its name) goes once it is two hours old.
- **When it fails** (every command failed or stalled, or an hour passed),
  the original is served as before and the page says the browser can't play
  it, with **Open in the system player**. The failure is recorded as
  `proxies/<sha256>.failed` (empty, 0600), so neither the gallery nor
  `media-intake proxies` spends time on that video again until
  `media-intake proxies --retry-failed`; a proxy made later deletes the
  record. A cancelled transcode (you moved on, Ctrl-C) records nothing.
  `trash`, `forget`, `erase` and `prune-cache` delete the record with the
  proxy, and `verify-gone` counts one left behind with the proxies.
- Safe to delete the whole folder at any time; proxies are made again.
- The review page ([review-ui.md](review-ui.md)) serves a batch's quarantine
  and review files as they are, with no proxy: quarantined files can hold
  nudity and IDs, which must not gain a cached copy.

## Security

Same as the review page (`review_ui.py`, whose guards it reuses): stdlib
only, every byte of HTML/CSS/JS inline (no CDN, fonts or scripts), bound to
127.0.0.1 on a random port, a random token swapped for an HttpOnly
SameSite=Strict cookie, a Host check against DNS rebinding, a CSRF value and
an Origin check on every POST, `no-store` and a strict CSP on every response,
media by opaque id only (never a path), Range requests for video. Plugin
routes run only after the same token, Host, Origin and CSRF checks. Face and
pet crops (`/r/<id>`) follow the same rules and the ones under "People and
pets" above.

## Performance

Start-up reads the library tree, `index.json` and every sidecar (stdlib XML,
no exiftool) once and keeps them in memory; files are hashed only when their
size matches a flagged one, or when a thumbnail needs the sha. Measured on a
synthetic 3,000-item library (2,600 JPEGs with sidecars, 400 videos) held in
RAM: URL printed after 0.6 s, 38 MB RSS at start, 68 MB peak after listing all
items and making 240 thumbnails (about 20 ms each for a 1600 x 1200 JPEG). A
cold read from disk will be slower.

## Test

`python3 -I -m unittest discover -s tests -p test_gallery_people.py`: People & Pets on a
synthetic recognition store built through recog's API with a mock detector
and embedder (no model): off without a store (none created), the facet and
filter, name / Undo / ignore / unname / merge (across species refused) / take
out / put back with the sidecars read back by exiftool, "looks like" hints
(people at people.py's threshold, pets at pets.py's `SAME`, never a cat for a
dog), a name only in the large view's People line (not "Your tags"; "+ tag"
refuses `People|` / `Pets|`), suggestions after a re-cluster, 409 for a stale run and a stale Undo, 403 and
415, crops (0600 in 0700, no-store, never for a flagged file) and the large
view's names and a video's times. Its browser counterpart is
`tests/browser/test_browser_gallery_people.py` (also: a junk-flagged photo
leaves Suggested junk once named and comes back on Undo, and the sidebar's
order, Quick views, Events, Places, People & Pets, with no sideways scroll at
1000 px or on a phone). A video's crop through the real frame source (frames.py
on an ffmpeg clip) is in `tests/test_recog.py`.

`python3 -I -m unittest discover -s tests -p test_gallery.py`: synthetic media in a throwaway
HOME, the re-run under the venv with every flag and nothing printed first (`VenvReexecTest`: `os.execv`
mocked; none when opted out, already in the venv, without one, or with pillow-heif), a HEIC without pillow-heif
flagged `heic` with its full size a 404 (`HeicWithoutPillowHeifTest`), the real `intake.py` for trash and forget, and a demo plugin view; the
job tests swap in a fake `intake.py` whose trash waits on a FIFO, then kill
the server or the worker mid-job, and a plugin POST gets 409 while such a job
holds the library lock (a view declared `post_read_only` does not), every 409 then reading "a Trash is still
running; wait for it to finish, then try again" with `"busy": true`; `busy_text` naming each holder plainly
(no PID or path) and `WORDS` holding no tool namespace (`PlainWordsTest`). Also: the sidebar counts against their
filters, the added-in-N-days filter, Favorites (`test_favorites`: the rating written and deleted and
nothing else, digiKam's five stars read as a favorite and four not, a favorite
never in Suggested junk, the Favorites filter and its count, the refusals), a trashed file's copy showing as
flagged `trashed copy` at once, and Open in the system player through a fake
`xdg-open` on PATH (its refusals included). Events: the sidebar counts, the
filter, rename and merge (and their refusals) with `Owner|` and other tags
kept, a display name moving with a rename (a stale one for the new slug
dropped) and the merged events' names dropped (`EventLabelsTest`: never two
events alike, the defaults, the name's checks, `event-names.json` per library,
0600, no temp file left); bulk tags, Set event (and clearing it) and Rotate on a selection;
byte-identical copies rotated differently getting their own thumbnails (and
copies turned alike sharing one), with Trash, `trash`, `forget` and
`verify-gone` reaching every turn and the older `<sha256>.jpg`; a
share-safe copy through exiftool with no GPS or Model left. `ViewsEndpointsTest`
writes its sidecars as plain XMP, so it runs without exiftool: every new POST
(`/api/event/set`, `/api/event/rename`, `/api/event/merge`, `/api/event/name`, `/api/share`)
refused without the token, with a wrong CSRF value, another Origin or Host
(403) or a non-JSON type (415), 409 while a job holds the library lock, and a
share-safe copy (Pillow's re-encode when exiftool is missing) into its own
0700 folder, with its refusals, and Open folder through a fake `xdg-open` on
PATH (only that job's folder; an unknown job, a path in the body, a symlink
swapped in, a deleted folder, no CSRF value or another Origin refused); and
display names: set, refused (too long, a newline, a zero-width character,
another event's slug or name alike), kept across a restart, back to the
default with `""`, a default falling back to the slug beside another tool's
look-alike, and an unreadable `event-names.json` never overwritten (409).
`ShareOpenGuardTest` runs `open_share` in-process with Popen, `shutil.which`
and the job status mocked: the argument list, a folder outside or nested
under `--share-out`, `..` in it, a running job, and no `xdg-open`.

`tests/test_gallery_dates.py`: Dates to check over the real check-dates' report (the sidebar count, the
groups in order, each tile's suggestion, `dates=<kind>`), Fix date by the suggestion, a set date and a shift
writing only the sidecar (the originals' sha256 unchanged), its refusals (a bad date or amount, an impossible
date, no suggestion: 400, no sidecar made), Undo putting it back and refusing after a later change (409), a
gallery fix undone by `fix-dates --undo`, and no report yet. `tests/browser/test_browser_gallery_dates.py`
runs the same in Chromium: the sidebar entry, the groups and flags, Fix date only in this view, the
suggestion and a typed date (a bad one refused in the dialog), Undo, a file name holding HTML shown as text,
and at phone width no sideways scroll in the view or the dialog.

`tests/test_gallery_undo.py`: Undo of Keep (every tag and leaf it removed back,
a file that had no sidecar), Favorite and Unfavorite (each rating back: four
stars, none, five), a tag added and removed, Rotate (an orientation
back, and none where there was none) and Set event (both fields, two events,
cleared), each compared field by field with the sidecar before; refused (409,
nothing written) after another action or another tool changed an item, and
while a job holds the library lock (then working once it ends); a token used
once; no token from Trash, Erase, Share, Apply or an action that
changed nothing; `undo_block` refusing a difference outside the action's
namespaces. `tests/browser/test_browser_gallery_dialogs.py` does it on the
page with a listener that fails on any native dialog: the tag field's
suggestions and refusals, focus kept in a dialog and given back on Esc, an
error message staying past the time a plain one fades (Playwright's clock) until
dismissed, a 409 while a job holds the lock shown as one in plain words with
Show progress, the job dialog's Hide, the header's pill and the end announced
in a message while hidden, the Keep message, no `Owner|` / `Review|` /
`Label|` in any text, tooltip or name the page shows (grid, filters, chips,
the large view, every dialog), the message above the action bar at phone
width, the `?` list naming
every key the handlers check, Undo of Rotate, Keep (Ctrl+Z), Set event and a
tag added in the large view restoring the sidecars, Undo refused after the
item changed, and none after Trash, Erase or a Share.

`tests/test_gallery_bursts.py`: which files stack (planned shots in time
order, a camera burst id over the clock, a near-copy of the same second, a
copy from another device, an unplanned file without a marker and a video
never; a released file found by `lib_dst`; a later batch winning; unplanned
files by a burst name or a sidecar's GCamera BurstID), and `/api/list`
putting a burst's shots after their cover across pages, no stack of one
under a filter, and none in Suggested junk.
`tests/browser/test_browser_gallery_select_all.py`: Select all by the button,
Ctrl+A and Cmd+A, a closed stack adding its cover only (and an open one every
shot), an event filter respected, Ctrl+A in the search box left to the field,
Esc clearing, Trash and Erase asking as before (the count, ERASE typed), and a
grid with only its first 240 loaded saying how many more match.
`tests/browser/test_browser_gallery_drag_select.py`: drag to select (a closed
stack's cover only, an open one's every shot, Shift and Ctrl adding, a plain
drag replacing, Esc putting it back, a 3 px press still a click, the checkbox
and a touch pointer never drawing, the bottom edge scrolling into the next page
and taking in its tiles) and Suggested junk's bulk marks (Shift-click and
Shift+T / Shift+K ranges keeping an opposite mark, a drag from Trash and Esc,
Mark all shown as Trash, Clear marks asking, Apply still asking with thumbnails
and names, marks surviving another view, the page asking before unload, focus
on the first tile on entering, K / T with no outline, and the large view's K /
T marking and stepping on).
`tests/browser/test_browser_gallery_bursts.py` does it on the page: the
cover, badge and X, `x` and `X` with focus back on the cover, a near-copy
never stacked, a closed stack's range selection and Keep reaching only the
cover, closing a stack unselecting its hidden shots, and the large view's
arrows over a closed and an open stack.
`test_suggested_junk_why_and_score` covers Suggested junk's Why and Junk
score on `/api/list` (kept, used and protected files out at any score) and
the facets' `suggest:lower` and `ai_scores`.
`tests/browser/test_browser_gallery_junk.py`: Suggested junk's bar (Why
with its counts, Junk score, Show N more with a lower junk score, back to
the default line on re-entering), the tile's "Junk score 0.90 (blurry)", the
header's "N suggested, M shown with your filters", the Judged junk count
following its slider, and the bar at 390 px (44 px tall, no sideways scroll);
marks with the filters (Mark all shown as Trash, then a narrower Why and a
higher Junk score: "(N more not listed now)" and only the listed marks in
Apply's request), a star taking a Trash mark off (unstarring leaves it off,
Apply sends only the other mark), and junk tiles with no star or Share (a
Share asked and cancelled from Library leaving the marks as they were).
Apply's `/api/decide` is answered by the test there; `test_gallery.py`'s
`test_apply_skips_a_trash_mark_starred_or_kept_since` runs the server side
(a starred and a kept mark skipped, the rest trashed, no job when all are
skipped, plain Trash on a favorite allowed), and
`test_display_name_goes_with_the_last_file_of_its_event` /
`test_display_names_of_emptied_events_dropped_at_start` the display names
(dropped after Erase, Trash and Set event "" empty an event, an Undo and a
Set event making it again with the default, at start only with every library
folder there).
`tests/browser/test_browser_gallery_grid.py`: Suggested junk by tap on a
390 px touch phone (Keep and Trash pressed and unpressed, the mark in words,
a tap on the photo opening it, no checkboxes or selection bar, the view's
note, Apply keeping what was marked) and by mouse and keys; every grid control
on the phone taking a 44 x 44 px tap (probed with `elementFromPoint` 21 px
from its centre); the loading state (the list request held by the test), an
empty library naming the commands, no results for a search, a filter and an
empty quick view, Find similar without the index, an item gone since the list
loaded, a load error with Try again; and the rail's words, with no Keyboard
shortcuts item on a touch screen.
`tests/browser/test_browser_gallery_layout.py` checks the layout at 390 px
(phone) and 800 px (just above the phone breakpoint): no sideways page
scroll, the header's title shown and its controls inside the header, a burst
badge and X button inside their tile and clear of its checkbox (stack closed
and open), Select all's long label wrapping inside the bar, and the last row
of tiles above the selection bar at the end of the page, and a 64-character
event slug and display name with nowhere to break inside the phone's drawer,
title and event bar, and a tile's star and Share side by side at its bottom
right, clear of each other and of a long flag (34 px at 1440 px, 44 px on the
phone). At 1440 px it drags
the sidebar's edge (the grid following mid-drag), clamps it at 240 px and
560 px, to half a 1000 px window and to 320 px in an 800 px one (the header
still fitting in both), keeps it across a reload, resets it on a
double-click, resizes it by Left / Right / Shift / Home / End with a visible
focus line in light and dark and a Highlight one in forced colours, ignores
the keys with Ctrl, Meta or Alt and while the shortcut list is open, falls
back from or clamps a bad stored width, puts the width back after an
interrupted drag (pointercancel, lost capture), starts no drag on a right-button
press, works with `localStorage` throwing, and finds no
edge in the collapsed rail or on a phone; Keyboard shortcuts sits at the
sidebar's foot (not in the Views nav) and opens the list with its Sidebar keys, and the search placeholder fits its box
at the default width with a tenth to spare.

`tests/test_gallery_media_kinds.py` runs every operation on a photo, a video and
a mixed selection (JPEG, PNG, H.264 in .mp4 and an upper-case .MOV): the
listing, kind filter and counts, item detail, media with Range, the photo-only
preview, thumbnails, Keep, my tags, Rotate, events (set, rename, merge, clear),
Trash and Erase (the real `intake.py`), Suggested junk's Apply both ways,
Share (the copies' title and GPS gone, the video a stream copy),
Open in the system player, and the refusal of a selection holding a file
deleted since the scan. `tests/browser/test_browser_gallery_media_kinds.py`
does it on the page: a mixed selection by Space, a shift-click range and
Ctrl-click, Keep by K and by the bar (sidecars and the tiles' "Kept" flags),
a shift-click after the grid reloads, + tag / − tag, Set event, Rotate (the
thumbnails made again turned) and Trash on a mixed selection (a message with
Details, not the Done dialog), Trash in the large view (it moves on to the
next item and the tile leaves the grid with no reload of the list), and the large
view playing an .mp4 and a .MOV and rotating a video, and a HEIC without
pillow-heif showing its thumbnail as "a small preview" and why (never "can't
show" over it), or "No preview" when it has no thumbnail.

`tests/test_gallery_proxy.py` (the HEVC cases skip, naming why, when no
synthetic HEVC clip can be made: ffmpeg's `libx265`, else VA-API's
`hevc_vaapi`; or no real proxy: ffmpeg with an HEVC decoder, else GStreamer's
GPU decoder and an H.264 encoder): a synthetic HEVC .MOV with AAC sound
served as an H.264 + AAC proxy, the cached copy a plain MP4 (moov before
mdat, no fragments) 0600 in a 0700 folder, with Range, made once (a second
request and a restarted server reuse it), `/api/item`'s `conv` saying
`converting` until then, an H.264 .mp4 served as it is with no proxy, and the
originals unchanged; a failing ffmpeg (a fake first on PATH) serves the
original, tries once, leaves no partial file, and Keep still works; no ffmpeg
transcode at all where ffmpeg has no HEVC decoder; the GStreamer fallback's two
`gst-launch-1.0` runs (with and without sound) for a file named with `!`,
spaces and double quotes: two `location=` arguments, the work folder's link
(resolving to the original) and output, no part of the name in any argument,
and the clip's own size in the caps. With the real transcode held by a gated
tool once it has written everything (a FIFO): the reply's headers (`200`, no
length, `Accept-Ranges: none`) and first bytes arrive before the transcode
ends and before any proxy is cached, the whole reply is a fragmented H.264 +
AAC MP4, and afterwards the cache holds the plain one, served with Range;
another HEVC video opened while one is watched is served as its original at
once (`conv` `waiting`) and gets its own proxy on the next open after; a
player that hangs up lets the next video's open cancel that transcode
(nothing of it kept, not marked as failed); a video put in a Trash job (held
by a gated `intake.py`) or trashed for real mid-transcode ends the reply,
stops the transcode and leaves the cache empty; Trash from the gallery
deleting the proxy; and `trash`, `forget` and `verify-gone` on planted
proxies and failure records. `media-intake proxies` through the real `cli.py`: `--dry-run`
counts (a duplicate is one proxy, an erased clip is left out and never named),
a run makes the plain proxy once, a second run finds it made, the gallery then
serves it at once with Range, a starred clip is proxied like any other, a
failing tool exits 1 leaving only its `.failed` record, the next run skips it
(`1 failed before, skipped`, exit 0) and `--retry-failed` tries it again, and
its help says what it touches and deletes. `MakeProxyTest` runs `make_proxy`
in-process with Python stand-ins for the commands: an attempt that hangs
(after its first bytes, or before any) gives way to the next command within
`PROXY_STALL`, one that keeps writing slowly is not stalled, one still writing
at `PROXY_TIMEOUT` ends it without the next command, every command failing
writes the 0600 record and a later success deletes it, a record that can't
be written is a plain failure (no error), a cancel records nothing, and the
gallery serves the original of a recorded failure.
`PlanTest`: `proxies.plan` counts a recorded failure and skips it, or plans it
again with `retry_failed`. `ProxiesRunTest` runs it in-process with the plan
and `make_proxy` stood in for: a video trashed, erased or flagged while its proxy is made
leaves no proxy ("gone meanwhile", exit 0), and one `still_shown` refuses
before its turn (a symlink, a file under the state folder, an erased one) gets
none made. In-process: `App.playable` with
`make_proxy` held on an event (requests for the same content follow one
transcode, another video gets its original while one is watched, an
unwatched one is cancelled for the next, a failed one isn't tried again, one
finished after its video went into a job or went is deleted); `ProxyJob`
driven by hand (a reader waits for the first bytes, follows its attempt and
stops when another begins or the job ends; readers keep it from being idle;
a cancel or a busy video stops it); `proxy_commands` with the lookups patched
(a hostile name kept out, fragments in every command, `vah264enc` first and
scaled on the GPU, Vulkan frames scaled on the CPU, an unsafe work path
skipping GStreamer with one note per folder) and `proxy_size`.
`tests/browser/test_browser_gallery_proxy.py`: in headless Chromium, the
large view saying **Converting this video for the browser** (role
`status`) and playing the HEVC clip while its transcode is still held, then,
after it ends, playing it again from the cache with its duration and Range.

In headless Chromium (`tests/browser/test_browser_gallery.py`, see
CONTRIBUTING.md, Tests): keys, click and shift-click selection, the bottom
selection bar appearing only while something is selected, a sidebar quick
view and its chip, the Trash, Erase (ERASE typed, plain words, thumbnails,
the backup drive named or not) and Stop server confirms (all in the page), the
lightbox video playing by itself, Space pausing and resuming it, seeking by
Range, and the job dialog's progress (no confirm text left, Hide), re-attach
after a reload, its end (Done, the status line, Close) and crash message,
Erase's status line shown while its output is still empty (a
fake `intake.py` that prints nothing until the test opens a FIFO), and the
Events list (filter, Rename with a refused name first; Display name… set as
text, never markup, with a look-alike refused first, kept across a reload and
cleared back to the default, the slug in the tooltips and the bar, a default
that would look like another event's left as the slug), Set event on a
selection and a Share ending in "Done: share-safe copies in ..." with the
folder opened (a fake `xdg-open` on PATH in every test there), and Share from
a tile (shown on hover, selecting nothing) and from the large view's header,
the message after a hidden job with Open folder, and a 409 from the open
shown in the dialog with where the copies are; Favorites (the star on hover
and kept shown once pressed, a click starring with the sidecar's rating 5 and
the original unchanged, Undo by Ctrl+Z, F on a focused tile and on a
selection both ways, the bar's Favorite and Unfavorite, the large view's
button and F, a starred suggestion leaving Suggested junk, the Favorites view
with its chip, and the Added in the last 30 days name); the When list's five
recent years, All years (N), kept across a reload, and a picked older year
staying listed.

## Not verified

- People and pets with the real models (YuNet + SFace, YOLOX + DINOv2) on
  real photos: the gallery tests use a mock detector and embedder (with
  pets.py's real thresholds); a video's times in the gallery come from a stub
  frame source there, and from the real one only in `tests/test_recog.py`.

- The layout by eye (sidebar, drawer and rail at phone width, light and
  dark), infinite scroll and HEVC posters through GStreamer in a real
  browser, the "can't decode" fallback in a browser that really can't play a
  file, a real `xdg-open`, and any browser but Chromium.
- Burst stacks from a real iPhone or Pixel burst: the tests write the plan
  entries and a GCamera BurstID by hand; Apple's MakerNotes BurstUUID is
  read through exiftool's `BurstUUID` tag on no real file here.
- Semantic search and Find similar through the venv: tested only as "unavailable,
  falls back", not with the real model.
- Start-up time on a large real library read from disk.
- Share of HEIC and DNG from the gallery (the gallery tests copy
  JPEGs, an .mp4 and a .MOV), the other operations on an HEVC video (only the
  proxy tests make an HEVC clip), and Rename / Merge on a real digiKam-written
  library.
- Video proxies from a real iPhone or GoPro file (only synthetic clips, up to
  a 25 s 4K/60 one, were timed), and the GStreamer proxy path on CI (it has
  no GStreamer; there the tests run ffmpeg's path, and the GStreamer one only
  where a VA-API GPU is: `vah265dec` into `vah264enc` was run that way;
  Vulkan, `x264enc` and `openh264enc` only as commands with fakes), non-AAC
  sound falling back to no sound, a portrait clip's rotation in a browser
  (ffprobe shows the rotation kept), and a long clip streamed for minutes.
