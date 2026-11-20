# Roadmap and follow-ups

Where media-intake is, what's rough, and the order to make it solid. The first
real imports (Android phones over MTP, an iPhone over gphoto2, GoPro exports,
messaging-app media) shaped most of this list.

**Principles that don't change:** everything runs locally; no AI service or
assistant ever sees a photo; originals are never modified (tags live in XMP
sidecars); sensitive files are quarantined for you, never auto-deleted;
nothing is deleted from a device without your explicit OK.

---

## Milestone 0.1: solid core (do first)

The goal: someone else can install it on Fedora, import a phone, and trust it.

### Packaging, config, CI
- [x] `pyproject.toml` entry point `media-intake` with every subcommand (`media_intake/cli.py`).
- [x] `media-intake doctor`: system tools, tesseract `eng`, zxing-cpp, GStreamer HEVC decoder,
      venv, model files, GeoNames, Ollama version.
- [x] `media-intake doctor`: also verify model checksums and the pinned Ollama model digest,
      and print the exact `dnf install` line for what's missing (pins in `media_intake/pins.json`).
- [x] `config.toml` schema with validation and documented defaults (library, videos,
      project folders, state dir, phone allowlist extras); env vars still override
      (`media_intake/config.py`, `docs/configuration.md`).
- [x] One script that regenerates hash-pinned requirements and model pins (7-day minimum
      age), so pins are bumped, not hand-edited (`scripts/update-pins.py`).
- [x] GitHub Actions: unit tests on synthetic data with model-dependent tests skipped
      (`.github/workflows/ci.yml`, `CI gate`).
- [x] A nightly or manual job that runs the model tests (needs the venv and model cache)
      (`.github/workflows/models.yml`, Fedora 43 container; not a required check).
- [x] Release process: tagged releases; consumers pin by tag + commit SHA ([docs/release.md](docs/release.md)).

### Safety gaps found on real devices
- [x] **Case-insensitive device storage:** Android's emulated storage ignores case, so a
      case-only rename leaves what looks like a duplicate and deleting it deletes the real
      file. Refuse any device delete whose path differs from another manifest path only
      by case; never rename by case on a device.
- [x] **Case guard against the live listing:** the same check also runs against what the
      device lists at delete time (`sources.live_case_matches`), so a file copied on after
      staging, or one no manifest recorded, still blocks a case-colliding delete; a device
      that can't be listed refuses the delete instead of trusting the manifests.
- [x] **Long actions survive the page:** release/erase/trash started from the review page
      or gallery must run as a detached, resumable job with a status file. Today stopping
      the page does not stop an erase already started, and a second page can show stale
      state. Show progress in the page; refuse a second submit for the same batch.
- [x] **Near-duplicate chains:** each erase can surface near-copies of the erased item,
      then near-copies of those (`near-`, `near-near-`, ...). Bound the depth, merge them
      into one review, and show the matched original whenever it still exists.
- [x] **Kill by PID, never `pkill -f`/`pgrep -f`:** they match their own shell's command
      line. Audited scripts and docs: none found. `tests/test_cli.py` guards every `.py`,
      `.sh`, workflow, `.claude/` file and the Markdown at the repo root and in `docs/`
      (code comments and Markdown lines that say "never" state the rule, so they pass).
- [x] Don't move library files while a scan/tag/apply job runs: take a lock per library.
- [x] **No file name in a GStreamer pipeline:** `gst-launch-1.0` re-parses its arguments as one
      pipeline, so a "!" or a quote in a device file's name could add elements (a sink writing
      anywhere). Video frame sampling (scans, labels, search, thumbnails) now reads the clip through
      a link in a private work folder whose path is checked, with the argv built as a list; an unsafe
      work path skips GStreamer and the clip stays "not checked" (`scan_sensitive.gst_frames_command`).
- [x] **Quarantine is Erase-only on the review page:** plain Trash on a quarantined item moved
      that one file and left its messaging copies and near-copies behind. The page offers no
      Trash (button or T) there and says why; the server refuses a trash decision on one (400,
      nothing written). The `trash` command refuses a quarantine path too (exit 1, nothing deleted).

### Dates and devices
- [x] Implausible camera dates (GoPro with an unset clock writes 2015-12-31/2016-01-01):
      use the GPS date (`GPSDateTime` / `GPSDateStamp`) automatically, record the source.
- [x] iPhone exports without EXIF dates: use the `DCIM/YYYYMM__` folder month, not mtime.
- [x] QuickTime UTC vs local time; EXIF `OffsetTimeOriginal`; a `--clock-offset DEVICE=±Nh`
      option for cameras that were simply wrong. Corrected dates go to the sidecar only.
- [x] Device identity from Make/Model/serial: tag `Source|<model>` and name batches by it;
      two GoPros with the same file numbers go to separate device folders, never `_1`.
- [x] GoPro chapters (GX01xxxx, GX02xxxx...) dated and grouped by the first chapter.
- [x] Wrong dates already in the library: `check-dates` (clock-reset defaults, device era, video
      format, numbering order, disagreeing sources, copies, GPS 0,0, place), `fix-dates` and the
      gallery's Dates to check / Fix date, sidecars only, undoable (`docs/dates.md`).
- [x] Re-file a fixed file into its right YYYY/YYYY-MM folder: `refile`, a dry run until `--yes`, file and
      sidecar renamed together, every path record rekeyed, undoable (`docs/dates.md`).
- [ ] Device era for more makers (DJI, Insta360, Sony, Canon), and HEVC 10-bit / HDR vs the model.
- [x] Source kind `folder` for laptop folders (refuse HOME itself, hidden/config dirs, repos,
      the state dir, library roots, password-manager paths; offload refuses folder sources).

### Tag precedence (who writes what)
- [x] Review page and gallery record owner decisions: Keep writes `Owner|keep`; trash
      records the sha256 in `trashed.json` (the durable `Owner|trash`, so it never comes
      back) and marks surviving library copies `Owner|trash` in their sidecars.
- [x] All tools honour `Owner|not-trash`; drop the legacy `Review|not-trash`.
- [x] Tests that a re-tag replaces only its own namespace and never an owner tag.

### Tests that still don't exist
- [x] Real-browser tests (Playwright, synthetic media) for the gallery and review page:
      keys, selection, dialogs, video autoplay, Range seeking, job progress and re-attach
      (`tests/browser/`, the `browser` CI job).
- [x] End-to-end on a fake MTP and gphoto2 tree with case-only collisions.
- [x] Every operation (keep, trash, erase, release, tag, rotate, share, events, bulk select,
      and the device-side stage, plan, apply, erase, offload and verify-gone) on photos,
      videos (mp4 and .mov) and mixed selections, for phone, GoPro, drive and folder
      sources (the `*_media_kinds` test files and their browser tests).

---

## Milestone 0.2: the gallery as the everyday UI

- [ ] Layout polish after real use: the gallery's left panel and the lightbox, once they have
      been used on a real device. (Done: the review page's sticky Submit, the gallery's phone
      layout, 44 px touch targets and the grid's empty, loading and error states.)
- [x] UX audit for a non-technical owner: `trash` and `purge-review` are dry runs without
      `--yes`; plain words in the pages and help (no tool tag names, PIDs or lock paths); one
      word list, docs/words.md.
- [x] **HEVC in the browser:** Firefox/Chromium often can't play HEVC. Make an on-demand
      H.264 proxy (GStreamer GPU decode → x264), cached by sha256 in the private cache
      and cleaned by erase/trash/forget/verify-gone like thumbnails (gallery only; the
      review page keeps serving batch files as they are: docs/gallery.md, "Video proxies").
- [x] Fast HEVC playback: the proxy is streamed to the player as fragments while it is made
      (starts in ~2 s instead of after the whole transcode), `vah264enc` on the GPU first and
      scaled there, an unwatched transcode cancelled for the next video, a "converting" note
      in the large view, and `media-intake proxies` to make them all ahead of time.
- [x] Burst groups: show bursts as one stack, pick the best, trash the rest (review page
      and gallery; docs/gallery.md, "Burst stacks").
- [x] Albums/events as first-class views (from `Event|` tags), with rename and merge, and display
      names you can set (docs/gallery.md, "What it shows", its Events item).
- [x] Burst stacks in the gallery (the review page has them; `media_intake/bursts.py` is shared;
      docs/gallery.md, "Burst stacks").
- [x] Record a camera's `BurstUUID` as `burst_id` in plan entries (Apple `BurstUUID`, Google
      `XMP-GCamera:BurstID`; docs/intake.md, "Burst id").
- [ ] Map view without leaking locations: offline vector tiles (or none); never fetch
      tiles for private photos from a third-party server.
- [ ] Best shot per burst and near-duplicate set: pick the sharpest, best-exposed frame
      (eyes open where faces are found) as the cover, and offer the rest to Suggested junk
      with the reason "Not the best shot"; never trash on its own. (Done: `media-intake
      best-shot` picks it by sharpness and exposure and writes `AI|bestshot/` tags;
      docs/gallery.md, "Best shot"; Suggested junk, `suggest-trash` and the stack's cover
      read them. Left: eyes open.)
- [ ] "On this day": a sidebar view of photos and videos taken on today's date in past
      years, grouped by year; hides flagged, trashed and erased items like every view.
- [ ] Albums: hand-picked collections that cross dates and events (stored in sidecars like
      `Event|`, or a private state file), with rename and merge; **Share album** makes one
      stripped share-safe folder.
- [ ] Free up space: a read-only report of what takes room (largest videos, duplicate sets,
      Suggested junk, cached proxies) with the space each would free, and links into the
      gallery to act on it. Changes nothing itself. The report is done (`media-intake space`,
      docs/gallery.md "Free up space"); left: the links into the gallery (a view per section,
      or the report as a gallery page).
- [ ] Backup health in the gallery: when the last `backup` ran, to which drive, and how many
      library files are not on it yet; a link to the command to run. Read-only.
- [x] Bulk edits on a selection: add/remove tags, set event, rotate.
- [x] Select all in the gallery (header button, Ctrl+A / Cmd+A): every tile shown, a closed
      stack's cover only, the pages loaded so far (docs/gallery.md, "Actions").
- [x] Thumbnail cache keyed by content and rotation: rotating one copy no longer turns its
      byte-identical twin's thumbnail; copies turned alike share one; trash, forget, erase and
      verify-gone cover every variant (docs/gallery.md, "Thumbnails").
- [x] Prune the gallery cache: `media-intake prune-cache [--dry-run]` deletes legacy
      `<sha256>.jpg` thumbnails, turns no copy shows any more, and thumbnails and proxies of
      content no longer in the library (docs/gallery.md, "Pruning the cache").
- [x] A "share-safe export" button in the gallery (the CLI has `media-intake share-copy`:
      copy with all metadata and GPS stripped).

## Milestone 0.3: better classification

- [x] Calibrate SigLIP thresholds (maybe-trash, child-in-shot, labels) with a
      sample you label yourself, computed locally: `media-intake calibrate` reports
      precision/recall per label against your `Calibrate|<label>|yes/no` tags and Keep/Trash
      decisions, from the stored index vectors; it changes nothing (docs/configuration.md,
      "Calibrating label thresholds"). Nobody else looks at the photos.
- [ ] Vision LLM: support newer local models when Ollama allows (e.g. a 4B Qwen3-VL needs
      a newer Ollama than 0.21); licence check (commercial use) before adopting any model.
- [ ] Optional local face grouping (off by default): face data counts as personal data,
      so erase/verify-gone must cover it; never names faces automatically. Core and CLI built
      (`recog.py`, `media-intake people`; names propagate only as suggestions until confirmed;
      docs/recognition.md); the gallery's People & Pets section and naming view are built too
      (docs/gallery.md, "People and pets").
- [x] Pet recognition (`Pets|<Name>`) alongside face grouping, sharing one store, naming and
      erase path; suggestions only until the owner confirms: core and CLI (`pets.py`,
      `media-intake pets`, YOLOX + DINOv2, `pets eval` against your own names;
      docs/recognition.md, decisions in docs/research-pets.md) and the gallery's naming view.
- [ ] Pets: date-aware propagation (prefer photos close in date when growing a name, so a
      kitten and the grown cat need fewer merges; docs/research-pets.md).
- [x] Search the text in photos: keep the OCR text the sensitive-content scan already reads
      (screenshots, receipts, signs) in a private local index and let `search` find files by
      what they say (`search --text`, `index --text` for older photos); flagged and erased
      files never indexed, erase and verify-gone cover the index (docs/search.md, "Text in
      photos").
- [ ] The gallery's search box for the text in photos (`search --text --format machine`
      gives the same lines it parses today).
- [ ] Local captions: a short description of each photo from the local vision model
      (Ollama, already used by `vlm-trash`), kept in the sidecar as the tool's own field and
      searchable; off by default, resumable overnight like `vlm-trash --all`.
- [x] Video near-duplicates (perceptual video hash) for re-encoded or trimmed copies
      (`videohash.py`; docs/intake.md, Duplicates).
- [x] Text search quality: prompt ensembles for queries (`--no-ensemble` to compare) and
      per-label thresholds (docs/search.md).
- [ ] Search across video frames by the best-matching frame: the index keeps one averaged
      vector per video today, so this needs a per-frame index.
      The pets/people video pass would provide it (docs/research-pets.md, "Videos").

## Milestone 0.4: more sources

- [ ] HEIC/HEIF (pillow-heif), DNG/RAW previews, iPhone Live Photos (HEIC/JPG + MOV pairs).
- [x] Samsung and Google motion photos: the embedded video is scanned with the photo (one
      it can't extract is "not checked", never clean) and dropped from share copies
      (`motion.py`; docs/intake.md). Not yet checked on real phone files.
- [x] Google Takeout and iCloud exports (JSON sidecars for dates/places), with dedupe
      against the library: `media_intake/exports.py`, the `export` date source
      (docs/intake.md, "Cloud exports"). Not yet checked on a real export.
- [ ] External drive queue: tested on real exFAT/NTFS/HFS+ drives; checksum-verified,
      resumable copies; faster parallel copy.
- [x] Messaging media beyond WhatsApp (Signal, Telegram exports) behind explicit allowlist
      entries, never a whole `Android/` tree. Telegram's Android folders and Signal's
      `Movies/Signal` by config entry, Telegram Desktop exports (media folders only) and
      Signal's saved `signal-...` files from a drive or laptop folder; Signal's own chats
      and backups are encrypted and out of reach (docs/intake.md, "Messaging apps").
- [x] Telegram export `result.json` dates for files an export keeps under their own name
      (its `photo_<n>@<date>` names are dated already), and `messaging:Telegram` for those
      files when the staged folder is the export itself (docs/intake.md, "Cloud exports").
      Not yet checked on a real export.

## Milestone 0.5: backups and erase everywhere

- [x] Built-in `backup` to a mounted drive (photos + videos + sidecars, never quarantine or
      staging), with `--verify` ([docs/backup.md](docs/backup.md)). Recommend LUKS-encrypted drives: exFAT deletes leave
      data readable until overwritten.
- [ ] Optional S3/offsite module: uploads only reviewed batches, no sidecars versioned
      forever, never Object Lock; erase deletes every object version and delete marker.
- [x] Erase coverage report: what each erase reached (laptop, device, drive, caches,
      thumbnails, search index) and what it can't (cloud services, chat apps, recipients,
      device app-private storage such as gallery trash). Kept in `erase-log.txt`;
      `verify-gone BATCH` shows the last one (docs/intake.md, "Coverage report").
- [x] Clean `~/.local/share/gvfs-metadata` entries for erased files; verify the desktop
      search index forgets them. Cleared through gio before each delete; entries of files
      deleted earlier outside gio stay (no documented route once the file is gone). Each
      gio and localsearch call gets 20 s (`GIO_TIMEOUT`); after one hangs, that erase skips
      the tool and counts what it skipped as left.
- [x] Backup records pruned on erase/forget: entries for erased paths and sha256s (and their
      `.xmp`) leave `<state>/backup/*.json` at once; a record that can't be read or rewritten
      is counted as left (docs/backup.md, "Incremental").
- [x] Refused cloud-export metadata reported: `stage` counts the symlinked, oversized and
      non-UTF-8 Takeout JSON / iCloud CSV it skipped, and stages their media without export
      dates (docs/intake.md, "Cloud exports").

## Later

- Plugin hooks for project-specific steps: started (`after_tag`, `gallery_views`,
  `docs/plugins.md`). Next: a hook to move library files into a project folder
  (promote), and hooks on erase/trash so a plugin can drop its own records.
- Index portability between machines (paths relative to roots); one importing machine,
  others read-only.
- An always-on home-server mode (still single-user, still local network only).

---

## Known limitations (documented, not bugs)

- Face grouping (`media-intake people`) is a hint: its thresholds are uncalibrated starting points
  (`people eval` measures them on your own `Calibrate|people|` tags); videos are looked at through a
  few sampled frames each (`frames.py`, docs/recognition.md, "Videos").

- Motion photos: only JPEG and HEIC ones are read. A Samsung PNG or AVIF motion photo is
  scanned as a still, so its short video is not checked. HEIC stills need `pillow-heif`.
  None of it is checked on real Samsung or Pixel files yet.

- Android's media service holds many `DCIM/.thumbnails` files open; MTP can't delete them.
- Without USB debugging, a phone's app-private storage (gallery trash, app caches) is out
  of reach; the tool doesn't ask for ADB.
- Detector misses: NudeNet only flags exposed regions, OCR misses embossed card numbers,
  zero-shot "child in shot" misses small or distant children. Every flag is a hint for
  you, never a guarantee.
- Fedora's `ffmpeg-free` has no HEVC decoder; video scanning and labels need GStreamer's
  VA-API or Vulkan decoders.
- Tested on synthetic media and fake device trees only: not yet on real HEVC files, real
  GoPro `.gpr` files and GPS tracks, or real phones over MTP and gphoto2. The real detector
  models run only in the `models.yml` job.
- Erase's gio (`info`, `set -t unset`) and `localsearch index|reset|info` handling, and the
  20 s `GIO_TIMEOUT`, are tested against fakes only, not a real `gvfsd-metadata` or miner
  (docs/intake.md, "Unverified").
- A GoPro's QuickTime creation time is read as UTC, as the spec says; forum reports say
  some write local time. Unconfirmed on a real file; `--clock-offset` corrects a camera
  that is off.
