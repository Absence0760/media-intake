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
- [ ] `media-intake doctor`: also verify model checksums and the pinned Ollama model digest,
      and print the exact `dnf install` line for what's missing.
- [x] `config.toml` schema with validation and documented defaults (library, videos,
      project folders, state dir, phone allowlist extras); env vars still override
      (`media_intake/config.py`, `docs/configuration.md`).
- [ ] One script that regenerates hash-pinned requirements and model pins (7-day minimum
      age), so pins are bumped, not hand-edited.
- [x] GitHub Actions: unit tests on synthetic data with model-dependent tests skipped
      (`.github/workflows/ci.yml`, `CI gate`).
- [ ] A nightly or manual job that runs the model tests (needs the venv and model cache).
- [ ] Release process: tagged releases; consumers pin by tag + commit SHA.

### Safety gaps found on real devices
- [x] **Case-insensitive device storage:** Android's emulated storage ignores case, so a
      case-only rename leaves what looks like a duplicate and deleting it deletes the real
      file. Refuse any device delete whose path differs from another manifest path only
      by case; never rename by case on a device.
- [x] **Long actions survive the page:** release/erase/trash started from the review page
      or gallery must run as a detached, resumable job with a status file. Today stopping
      the page does not stop an erase already started, and a second page can show stale
      state. Show progress in the page; refuse a second submit for the same batch.
- [ ] **Near-duplicate chains:** each erase can surface near-copies of the erased item,
      then near-copies of those (`near-`, `near-near-`, ...). Bound the depth, merge them
      into one review, and show the matched original whenever it still exists.
- [ ] **Kill by PID, never `pkill -f`/`pgrep -f`:** they match their own shell's command
      line. Audit scripts and docs.
- [ ] Don't move library files while a scan/tag/apply job runs: take a lock per library.

### Dates and devices
- [x] Implausible camera dates (GoPro with an unset clock writes 2015-12-31/2016-01-01):
      use the GPS date (`GPSDateTime` / `GPSDateStamp`) automatically, record the source.
- [x] iPhone exports without EXIF dates: use the `DCIM/YYYYMM__` folder month, not mtime.
- [x] QuickTime UTC vs local time; EXIF `OffsetTimeOriginal`; a `--clock-offset DEVICE=±Nh`
      option for cameras that were simply wrong. Corrected dates go to the sidecar only.
- [ ] Device identity from Make/Model/serial: tag `Source|<model>` and name batches by it;
      two GoPros with the same file numbers go to separate device folders, never `_1`.
- [ ] GoPro chapters (GX01xxxx, GX02xxxx...) dated and grouped by the first chapter.

### Tag precedence (who writes what)
- [ ] Review page writes `Owner|keep` and `Owner|trash` (the gallery's Keep already writes
      `Owner|keep`); nothing writes `Owner|trash` yet.
- [ ] All tools honour `Owner|not-trash`; drop the legacy `Review|not-trash`.
- [ ] Tests that a re-tag replaces only its own namespace and never an owner tag.

### Tests that still don't exist
- [ ] Real-browser tests (Playwright, synthetic media) for the gallery and review page:
      keys, selection, dialogs, video autoplay, Range seeking.
- [x] End-to-end on a fake MTP and gphoto2 tree with case-only collisions.

---

## Milestone 0.2: the gallery as the everyday UI

- [ ] Layout polish after real use (left panel, sticky actions, lightbox).
- [ ] **HEVC in the browser:** Firefox/Chromium often can't play HEVC. Make an on-demand
      H.264 proxy (GStreamer GPU decode → x264), cached by sha256 in the private cache
      and cleaned by erase/trash/forget/verify-gone like thumbnails.
- [ ] Burst groups: show bursts as one stack, pick the best, trash the rest.
- [ ] Albums/events as first-class views (from `Event|` tags), with rename and merge.
- [ ] Map view without leaking locations: offline vector tiles (or none); never fetch
      tiles for private photos from a third-party server.
- [ ] Bulk edits on a selection: add/remove tags, set event, rotate.
- [ ] A "share-safe export" button in the gallery (the CLI has `media-intake share-copy`:
      copy with all metadata and GPS stripped).

## Milestone 0.3: better classification

- [ ] Calibrate SigLIP thresholds (maybe-trash, child-in-shot, labels) with an
      sample you label yourself, computed locally: you mark a few hundred items in the
      gallery, a script reports precision/recall. Nobody else looks at the photos.
- [ ] Vision LLM: support newer local models when Ollama allows (e.g. a 4B Qwen3-VL needs
      a newer Ollama than 0.21); licence check (commercial use) before adopting any model.
- [ ] Optional local face grouping (off by default): face data counts as personal data,
      so erase/verify-gone must cover it; never names faces automatically.
- [ ] Video near-duplicates (perceptual video hash) for re-encoded or trimmed copies.
- [ ] Text search quality: prompt ensembles, per-label thresholds, search across video
      frames.

## Milestone 0.4: more sources

- [ ] HEIC/HEIF (pillow-heif), DNG/RAW previews, iPhone Live Photos (HEIC/JPG + MOV pairs),
      Samsung motion photos (embedded video).
- [ ] Google Takeout and iCloud exports (JSON sidecars for dates/places), with dedupe
      against the library.
- [ ] External drive queue: tested on real exFAT/NTFS/HFS+ drives; checksum-verified,
      resumable copies; faster parallel copy.
- [ ] Messaging media beyond WhatsApp (Signal, Telegram exports) behind explicit allowlist
      entries, never a whole `Android/` tree.

## Milestone 0.5: backups and erase everywhere

- [ ] Built-in `backup` to a mounted drive (photos + videos + sidecars, never quarantine or
      staging), with `--verify`. Recommend LUKS-encrypted drives: exFAT deletes leave
      data readable until overwritten.
- [ ] Optional S3/offsite module: uploads only reviewed batches, no sidecars versioned
      forever, never Object Lock; erase deletes every object version and delete marker.
- [ ] Erase coverage report: what each erase reached (laptop, device, drive, caches,
      thumbnails, search index) and what it can't (cloud services, chat apps, recipients,
      device app-private storage such as gallery trash).
- [ ] Clean `~/.local/share/gvfs-metadata` entries for erased files; verify the desktop
      search index forgets them.

## Later

- Plugin hooks for project-specific steps: started (`after_tag`, `gallery_views`,
  `docs/plugins.md`). Next: a hook to move library files into a project folder
  (promote), and hooks on erase/trash so a plugin can drop its own records.
- Index portability between machines (paths relative to roots); one importing machine,
  others read-only.
- An always-on home-server mode (still single-user, still local network only).

---

## Known limitations (documented, not bugs)

- Android's media service holds many `DCIM/.thumbnails` files open; MTP can't delete them.
- Without USB debugging, a phone's app-private storage (gallery trash, app caches) is out
  of reach; the tool doesn't ask for ADB.
- Detector misses: NudeNet only flags exposed regions, OCR misses embossed card numbers,
  zero-shot "child in shot" misses small or distant children. Every flag is a hint for
  you, never a guarantee.
- Fedora's `ffmpeg-free` has no HEVC decoder; video scanning and labels need GStreamer's
  VA-API or Vulkan decoders.
