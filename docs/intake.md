# Intake: design decisions and research

`media-intake` brings photos and videos in from a phone, a GoPro or an
external drive, files them, drops duplicates and junk, and quarantines
sensitive images for you to confirm and erase. Every path below is the
default; all of them come from `~/.config/media-intake/config.toml`
([configuration.md](configuration.md)).

## Principles

- **Sensitive files are quarantined, you confirm**, then one command erases
  them everywhere. Nothing is auto-deleted: detectors misfire (sports photos,
  swimwear, screenshots of forms).
- **Library by date, events optionally by project.** Photos go to
  `~/Pictures/library/<YYYY>/<YYYY-MM>/`, videos (GoPro included) to
  `~/Videos/personal/<YYYY>/<YYYY-MM>/`, screenshots to
  `~/Pictures/library/Screenshots/<YYYY>/`, screen recordings to
  `~/Videos/personal/Screen recordings/<YYYY>/`, messaging-app media to
  `~/Pictures/library/Messaging/<app>/<YYYY>/`. An event routed to a configured
  project goes to `<project folder>/<date>-<slug>/{phone,gopro}/` (see
  "Events" below). Original file names are kept: GoPro chapters (`GX01xxxx`,
  `GX02xxxx`) only line up by name.
- **Only camera folders are read on a device** (`DCIM/`, `Pictures/`, media
  extensions only; `media_intake/sources.py` and `tests/test_sources.py`).
  App data (`Android/`), downloads, documents and anything named like a
  password manager, authenticator, recovery codes or a wallet are denied even
  if a path is passed by hand. Tested with Android over MTP, iPhone over
  gphoto2, GoPro and external drives.
- **Extra phone folders are opt-in.** `[phone] extra_folders` in the config
  names exact folders (for example WhatsApp's
  `Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images` and
  `.../WhatsApp Video`) and the app they are filed under
  (`Messaging/WhatsApp/`). Only those folders and their subfolders are read
  under a denied tree; nothing else of that app (statuses, databases,
  backups). Default: none. Offloading such files also removes them from the
  app's chats, so think before you `offload` a batch that has them.
- **No assistant views photos for sensitivity.** Anything an AI assistant
  views goes to its provider and its transcript on disk, which is exactly where
  a passport scan or a nude must not end up. All detection is local and
  reports labels only. An assistant may run the commands for you, but must
  never read, view, montage or describe a file under `quarantine/` or a file
  `scan.json` flags.

## Commands

```
media-intake stage SRC_ROOT --kind phone|gopro|drive --batch NAME [--only LISTFILE]
media-intake plan BATCH [--event [PROJECT/]SLUG@YYYY-MM-DD ...]
media-intake scan BATCH                 # local detectors; labels only
media-intake apply BATCH                # moves files; prints a summary
media-intake quarantine BATCH           # counts + the folder to open
media-intake release BATCH FILE...      # false positive -> library
media-intake erase BATCH [--phone ROOT [--keep-phone-thumbnails]] [--drive MOUNT [--no-wipe-free]]
media-intake forget LIBRARY_FILE... [--drive MOUNT]
media-intake verify-gone [BATCH] [--drive MOUNT]
media-intake wipe-free MOUNT
media-intake offload BATCH --phone ROOT [--yes]
media-intake purge-review BATCH
media-intake trash [BATCH] FILE... [--phone ROOT]
media-intake trash --from-digikam [--yes]
media-intake suggest-trash [--batch B] [--phone ROOT]
media-intake not-trash FILE...
media-intake share-copy FILE... --out DIR     # alias: social-copy
media-intake tag BATCH [--no-labels]
media-intake tag-files FILE... [--event SLUG] [--no-labels]
media-intake mark-used FILE... --post SLUG
media-intake find [--tag T] [--event E] [--place P] [--project P] [--unused-since 30d] [--never-used]
media-intake gallery [--drive MOUNT]
```

`media-intake <command> --help` shows every option. The review page is
`media-intake review BATCH` ([review-ui.md](review-ui.md)), the web gallery
[gallery.md](gallery.md), the external-drive queue [queue.md](queue.md),
semantic search and the VLM trash check [search.md](search.md). `media-intake
doctor` checks system packages, the venv, models and the config.

A typical phone batch: `stage` the phone's storage root, `plan` (add
`--event` for a shoot), `scan`, `apply`, open the review page, `tag`, then,
once the library is backed up, `offload` to free the phone.

`share-copy` writes copies with no GPS or metadata, for sending or posting:
exiftool `-all=` for images (orientation and ICC profile kept), an ffmpeg
stream copy without data streams or metadata for videos. exiftool cannot write
GoPro GPMF tags, so the GPS track is removed by dropping the stream.

## Duplicates

- **Exact**: sha256. `plan` marks an exact copy of a library file, of a file
  trashed before, or of another file in the same batch "delete"; it is never
  filed twice. Within a batch the best copy is kept: camera photo or video
  over screenshot or screen recording over a messaging copy, then not from a
  `Restored/` folder, then larger pixel count, then larger file.
- **Near**: a 64-bit dHash on Pillow (EXIF-rotated, 9x8 grey). Images within
  2 bits of another in the batch, with the same orientation, go to the batch's
  `review/` folder with `why` naming the one kept, so you glance before
  anything is deleted. Two camera photos are never merged (burst shots look
  alike). A screenshot or messaging copy is also checked against the library
  index and goes to review when it is near a library image.
- Images under 200 px and empty files go to review as junk.
- `index.json` in the state folder keeps sha256 and dHash of everything in
  the library, so later batches are checked against it.

## Trash vs erase

`trash [BATCH] FILE... [--phone ROOT]` is a plain delete for junk that isn't
private: the file (library, quarantine or review copy), its `.xmp` sidecar,
its thumbnails (freedesktop and the gallery's), its `index.json` entry, and
with `--phone` its phone original (the manifest's `src`, checked by
`sources.check`). No home sweep, near-duplicate search, erase log or
verify-gone. Its sha256 and size (no name) go to `trashed.json`, and `plan`
marks any later copy "delete" with why "trashed before", so it never comes
back from another device or a backup. Anything private goes through `erase`
(or `forget` for files already in the library). The review page offers Trash
(T) on every item.

## Erasing, and what it can't reach

- **The disk.** `erase` deletes and relies on TRIM plus full-disk encryption.
  `shred` does nothing useful on copy-on-write filesystems (btrfs) or SSDs. On
  a Fedora install with btrfs in LUKS, `discard=async`, discards passed
  through LUKS and `fstrim.timer` on
  ([EnableTrimOnDmCrypt](https://fedoraproject.org/wiki/Changes/EnableTrimOnDmCrypt),
  [EnableFSTrimTimer](https://fedoraproject.org/wiki/Changes/EnableFSTrimTimer)),
  freed blocks are TRIMmed by the timer, not at erase time. Filesystem
  snapshots are not handled: if you keep btrfs snapshots of `/home`, erase
  cannot reach the copies in them.
- **Every copy in HOME.** After erasing, `erase` sweeps all of `$HOME` (and
  the whole `--drive`) for files with an erased item's size and sha256 (only
  same-size files are hashed) and deletes them, Trash entries with their
  `.trashinfo` too, printing a count per top-level folder. That covers copies
  made outside the tool (an old manual import, Downloads, Desktop, app caches,
  `~/.var/app`). Not swept, because they can't hold your media: toolchains and
  SDKs (`.cargo`, `.rustup`, `.nvm`, `.pyenv`, `.tfenv`, `development`,
  `Android/Sdk`, `.android/avd`), package and build caches
  (`.cache/{pip,pnpm,go-build,ms-playwright,mesa_shader_cache,nvidia,flutter-sdks}`,
  `.gradle`, `.m2`, `.pub-cache`, `.local/share/{pnpm,uv,flatpak}`,
  `go/pkg`), container storage (`.local/share/containers`,
  `.cache/containers`), the media-intake venv, and any `.git`, `node_modules`
  or `__pycache__` folder (`SWEEP_SKIP` in `intake.py`). Exact copies inside a
  git working tree (any folder holding `.git`) are listed, not deleted: the
  repo would go dirty and git history keeps the content anyway.
- **verify-gone.** The erase log keeps checksum and size, no names, so
  `verify-gone [BATCH]` repeats the sweep read-only and exits 1 if anything is
  left (including orphaned sidecars and gallery thumbnails). `erase` runs it
  last.
- **Near-duplicates** (a messaging-app recompress, a resized edit) don't match
  by checksum. `erase` takes the dHash of each erased image before deleting it
  and moves any image within 4 bits and with the same orientation to
  `quarantine/near-<batch>`, never deleting it: you look, `release` false
  matches and erase the rest. It looks only in the media library (library,
  videos and project folders, plus batches' `raw/` and `review/`), never in
  caches, app icons, map tiles or git repos, which hold look-alikes that
  aren't your photos. Images under 200 px and featureless hashes are ignored.
  Videos: exact copies only; a re-encoded or trimmed video isn't found.
- **App traces** (GNOME apps, checked against Loupe 49, Showtime 49 and
  localsearch 3.10 as distro packages, not Flatpaks):
  - Recent files (`~/.local/share/recently-used.xbel`): bookmarks for erased
    paths and anything under the state folder are dropped, the rest kept byte
    for byte; written atomically, and only if the result still parses.
  - Loupe keeps no cache or state of its own.
  - Showtime keeps `~/.local/state/showtime/playback_history.json`
    (sha256(uri) to play position) and `showtime.log` (logs every played URI).
    `erase` drops the matching keys and lines.
  - localsearch indexes Desktop, Documents, Music, Pictures and Videos
    recursively, and only the top of `$HOME` and Downloads, so the state folder
    is never indexed. It stores metadata (names, EXIF, location), not pixels,
    and drops deleted files itself; `erase` also runs `localsearch reset
    --file` on each erased path inside an indexed folder and deletes its
    failure reports (`~/.cache/tracker3/files/errors/<md5(uri)>`). The whole
    index is never reset.
  - Thumbnails: `erase` clears `~/.cache/thumbnails` entirely (rebuildable),
    since thumbnails are keyed by URI and a phone browsed in Files gets
    `mtp://` keys.
  - Not covered: `~/.local/share/gvfs-metadata` (per-file Files attributes,
    names only, no image data) keeps entries for files deleted outside `gio`.
- **The state folder.** Quarantine and staging live in
  `~/.local/share/media-intake/` (0700), not under `~/Pictures`, so desktop
  search and thumbnailers don't pick them up.
- **Backup drive (exFAT).** `erase --drive` deletes matching files anywhere on
  the drive by checksum, then `wipe-free` fills the free space with zeros
  (64 MiB writes to `.media-intake-wipe.N` until the drive is full, fsync,
  delete, sync), since exFAT deletes only free the clusters. It refuses any
  path whose mount (`findmnt -T`) is `/`, `/home`, `/var`, `/tmp` or `/boot*`.
  On flash (`rotational` 0) it also tries `fstrim`, but wear-levelling keeps
  spare blocks, so an overwrite is no guarantee there. The robust fix is a
  LUKS-encrypted backup drive, or a drive wiped whole.
- **Phone.** `erase --phone` deletes the originals over MTP and, by default,
  `DCIM/.thumbnails` and `Pictures/.thumbnails` (exactly those two paths, no
  symlinks; Android rebuilds them; `--keep-phone-thumbnails` skips).
- **What erase can't reach**, and doesn't list: cloud photo services, chat
  apps' own copies and their cloud backups, anyone a file was sent to, a phone
  gallery app's own trash (some keep deleted items for 30 days; whether an MTP
  delete bypasses it is unconfirmed) and its cache, and any versioned cloud
  archive of your library (every object version would have to go).

## Tagging

`tag BATCH` (after `apply`) and `tag-files FILE...` write searchable tags;
`mark-used` and `find` use them without any app; digiKam or the
[gallery](gallery.md) browses them. After `tag` writes the sidecars it calls
any plugin's `after_tag` hook ([plugins.md](plugins.md)).

- **Originals are never modified.** Their sha256 drives dedupe, `erase` and
  `verify-gone`, so a tag written into a JPEG or MP4 would break all three.
  Tags go into an XMP sidecar named `<file>.<ext>.xmp` (`GX010123.MP4.xmp`),
  digiKam's and darktable's default naming
  ([digiKam metadata settings](https://docs.digikam.org/en/setup_application/metadata_settings.html),
  [darktable sidecar files](https://docs.darktable.org/usermanual/stable/en/overview/sidecar-files/sidecar/)).
  `intake.py` creates an empty XMP packet when none exists, then edits it with
  exiftool, `-overwrite_original` pointed only at the `.xmp`; keywords are
  deduplicated with `-TAG-=x -TAG+=x`. `tests/test_tagging.py` checks the
  originals' sha256 are unchanged after tagging and re-tagging.
- **Fields**: `XMP-lr:HierarchicalSubject` with `|` (`Project|Personal` or
  `Project|<name>`, `Source|gopro` / `phone camera` / `screenshot` /
  `screen recording` / a messaging app's name in lower case, `Event|<slug>`,
  `Place|<city>, <state>, <country code>`, `Label|<label>`,
  `Review|child-in-shot`, `Review|untagged-video`, `Used|<slug>`),
  `XMP-dc:Subject` with each leaf, `XMP-iptcExt:Event`,
  `XMP-photoshop:City/State/Country`, `XMP-iptcCore:CountryCode`,
  `XMP-exif:GPSLatitude/GPSLongitude` (read from the file: EXIF, QuickTime,
  GoPro GPMF with `-ee`) and `XMP-exif:DateTimeOriginal`.
- **Events**: `plan --event [PROJECT/]SLUG@YYYY-MM-DD`. With a `PROJECT/`
  prefix, or a slug matching one of a project's `events` fnmatch patterns,
  that day's camera files (photos, videos, GoPro; not screenshots or messaging
  copies) go to `<project folder>/<date>-<slug>/{phone,gopro}/` and get
  `Project|<name>`. Otherwise the event is tags only (`Event|<slug>`) and the
  files still go by date. `tag-files` reads the project and event from
  `<project folder>/<date>-<slug>/`, or takes `--event`.
- **Protected projects**: files tagged with a project's `Project|<name>`
  (`protect = true`, the default) are never trash suggestions and never get a
  VLM trash verdict.
- **Used**: `mark-used FILE... --post <slug>` adds `Used|<slug>`; the slug's
  leading date is the date of use, so `find --unused-since 30d` needs slugs
  that start `YYYY-MM-DD-`.
- **Never tagged**: anything under `quarantine/` or `batches/`, and any file
  whose size+sha256 matches a file `scan.json` flagged in any batch, anything
  quarantined, or anything in the erase log (`label.refused()`, used by
  `label.py` itself, exit 3, and by every tagging command). A released false
  positive therefore stays untagged; tag it by hand if you want.
- **Sidecars travel with their file**: `release` creates none (tag later);
  `erase`, `forget` and the HOME/drive sweep delete the sidecar of every file
  they delete, and delete orphaned sidecars (`<file>.<media ext>.xmp` whose
  file is gone: they still hold its place and labels); near-duplicates moved
  to quarantine take their sidecar along (`release` brings it back);
  `purge-review` removes the whole folder; `offload` touches only the device;
  `verify-gone` also fails on orphaned sidecars. A backup that copies whole
  trees (rsync) carries sidecars with their files.

### Place names, offline

`media_intake/geocode.py`: [GeoNames](https://www.geonames.org/)
`cities1000.zip` (places with population >= 1000), `admin1CodesASCII.txt`
(state names) and `countryInfo.txt` (country names), CC-BY 4.0: credit
"GeoNames". Points become 3D unit vectors in a scipy `cKDTree`; the nearest
place within 50 km wins, else no place (GPS is still recorded).
Neighbourhood, historical and abandoned entries (`PPLX`, `PPLH`, `PPLQ`,
`PPLW`, `PPLCH`) are skipped.

GeoNames publishes no checksums and regenerates the files daily, so the pin is
trust on first use: `bash setup.sh` downloads once into `<state>/geo/` and
records each file's sha256 and date in `<state>/geo-pins.txt`; every later
setup run and every geocode verifies against it, and nothing is fetched at tag
time. `bash setup.sh --refresh-geo` accepts a new snapshot.

- scipy is hash-pinned in `requirements.txt`; the venv uses the system numpy
  (`--system-site-packages`).
- Rejected: `reverse_geocoder` (unmaintained since 2016), `reverse-geocode`
  (downloads its data over plain http with no hash).

### Content labels, offline

`media_intake/label.py`: SigLIP
[`google/siglip-base-patch16-224`](https://huggingface.co/google/siglip-base-patch16-224)
(Apache-2.0) through
[`Xenova/siglip-base-patch16-224`](https://huggingface.co/Xenova/siglip-base-patch16-224)'s
ONNX export at revision `4649052661e53c7000355844105f8a1792088239`. Runtime
file `onnx/vision_model_quantized.onnx`, sha256
`ef14a954f3d57e1806666432bd9785004c1dc27100aa260eee0cb0f10a5de058`,
downloaded by `setup.sh` into `<state>/models/` and checked again on every
run; CPU, onnxruntime.

- Preprocessing from `preprocessor_config.json` at that revision: bicubic
  resize to 224x224 (no crop), rescale 1/255, mean 0.5, std 0.5. Output
  `pooler_output` is the image embedding.
- `logit_scale` 4.7649970054626465 and `logit_bias` -12.9324369430542 are not
  in `config.json`; they were read from `google/siglip-base-patch16-224`'s
  `model.safetensors` at `7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed`. Score
  per label: `sigmoid(cos * exp(logit_scale) + logit_bias)`, max over the
  label's prompts, multi-label.
- Labels and prompts: `media_intake/labels.yaml`; text embeddings:
  `media_intake/labels.npz` (embeddings plus the prompt list, so `label.py`
  refuses a stale file), generated by `build_label_embeddings.py` with
  `onnx/text_model.onnx` (sha256
  `3aa7fdbd20eaa8740cce17bf82913de641fcb632a768fed59f661cdcd0c32553`) and
  `tokenizer.json` (sha256
  `4a17c975210be5ab4c36b47d8dae4eefb866dbfb1e676e394aad85dc30a3ae08`) from the
  same revision, and `tokenizers` (hash-pinned in `requirements-labels.txt`,
  installed into `~/.cache/media-intake-build/site`, never into the runtime).
  Regenerate after editing `labels.yaml`: `bash setup.sh --build-labels`.
  Text is lower-cased, punctuation-free, `</s>`-terminated and padded to 64
  tokens as transformers' `SiglipTokenizer` does.
- `child-in-shot` has several prompts and a low threshold (0.01) and becomes
  `Review|child-in-shot`: a flag for you to look, not a guarantee that a file
  without it shows no child.
- `maybe-trash` ("random photos of nothing"): review-type prompts (blurry,
  pocket shot, floor, ceiling, very dark, packaging, random object; threshold
  0.1, uncalibrated) plus a pixel check on the 224 px frame (variance of the
  Laplacian < 8, or mean grey < 10 of 255); images only. Either gives
  `Review|maybe-trash`.
- `suggest-trash [--batch B]` lists files the VLM called trash
  (`AI|vlm/<model>/trash/*` with that model's confidence >= 0.7, or the older
  `Review|vlm-trash/*`), or with `Review|maybe-trash` and no VLM keep; never
  ones with `Owner|not-trash`, `Owner|keep`, digiKam Accepted, `Used|*` or a
  protected project's `Project|<name>`. They show in the review page's "Maybe
  trash" tab: Keep runs `not-trash` (`Review|maybe-trash` off,
  `Owner|not-trash` on), Trash runs `trash`. Nothing is deleted without a
  decision.
- Videos: up to 30 frames spread over the clip (at most 0.5 fps), max score
  per label. A video the installed ffmpeg can't decode gets
  `Review|untagged-video`.
- Rejected: CLIP ViT-B/32 (softmax-style scores are weaker for multi-label),
  MobileCLIP (research-only licence), `open_clip_torch` (pulls torch), torch
  itself (CUDA dependencies, several GB).

### Tags: who writes what

Does AI classification override your tags? No. Each writer replaces only its
own namespace; your tags win.

| Namespace | Written by | Re-tag behaviour |
|---|---|---|
| Your keywords (digiKam), digiKam pick labels, `Owner\|keep`, `Owner\|not-trash` | you (digiKam, the gallery, Keep on a suggestion) | never touched by any tool; `trash --from-digikam` acts on Rejected (PickLabel 1) only when you run it |
| `Project\|`, `Source\|`, `Event\|`, `Place\|`, dates | `tag`, `tag-files` | a prefix is replaced only when the run has a value for it |
| `Label\|*`, `Review\|maybe-trash`, `Review\|untagged-*` | SigLIP (`label.py` via `tag`/`tag-files`) | replaced as a set on every labelled run (`--no-labels` leaves them) |
| `Review\|child-in-shot` | SigLIP | added, never removed by a tool; only you clear it |
| `AI\|vlm/<model>/...` (and the older `Review\|vlm-trash/*`, `Review\|vlm-keep`) | the VLM (`vlm_trash.py`) | its own; tagging never touches them |
| `Used\|<slug>` | `mark-used` | never removed |

- `Review|maybe-trash` is never added to a file with `Owner|not-trash`,
  `Owner|keep` (or the legacy `Review|not-trash`) or digiKam Accepted.
- `dc:Subject` leaves go only with a removed tag, and never a leaf another
  kept tag or digiKam's `TagsList` still uses (your keyword "food" survives
  `Label|food` being replaced).
- A keyword you write under `Label|` would be treated as SigLIP's: keep your
  own keywords out of the `Label|` and `Review|` trees.

### Browsing with digiKam (optional)

The [gallery](gallery.md) is the built-in browser. digiKam (Fedora `digikam`,
GPL-2.0-or-later) reads the same sidecars: Settings, Configure digiKam,
Metadata, Sidecars: tick "Read from sidecar files", choose "Write to XMP
sidecar only", and leave "Sidecar file names are compatible with commercial
programs" off (that would name them `file.xmp` instead of `file.jpg.xmp`).
Add the library, videos and project folders as collections. Tags appear under
Project / Source / Event / Place / Label / Review / Used.

- Pick labels: digiKam writes `Xmp.digiKam.PickLabel` (exiftool
  `XMP-digiKam:PickLabel`): 0 none, 1 rejected, 2 pending, 3 accepted
  ([Exiv2 digiKam schema](https://exiv2.org/tags-xmp-digiKam.html),
  [digiKam Labels View](https://docs.digikam.org/en/left_sidebar/labels_view.html)).
  `trash --from-digikam` lists library files whose sidecar has 1 (dry run);
  `--yes` trashes them. Needs "Write to XMP sidecar" above.
- digiKam may download face-recognition models or fetch map tiles on first
  use (network calls); check its settings before pointing it at the library.
- Rejected: Immich (needs at least 6 GB RAM and Docker; reconsider on an
  always-on machine), Shotwell (sidecar behaviour unconfirmed).

## Chosen tools

| Job | Choice | Why |
|---|---|---|
| Nudity | [NudeNet](https://github.com/notAI-tech/NudeNet), hash-pinned in `requirements.txt` | The only option with exposed-vs-covered body-part labels, so swimwear and shirtless sports shots don't count; the ONNX model ships in the wheel, no runtime download; CPU. Repo LICENSE says AGPL-3.0, PyPI says MIT: check which applies to your use. |
| ONNX runtime | onnxruntime, hash-pinned | Has cp314 wheels. Every pin is at least 7 days old when chosen. |
| OCR (passport MRZ, card numbers, keywords) | Tesseract (`tesseract` + `tesseract-langpack-eng`) | Signed distro package, offline, Apache-2.0. MRZ and card checks are about 30 lines each in `scan_sensitive.py`. |
| Barcodes (PDF417 on the back of US driving licences, AAMVA header; 2FA `otpauth://` QR codes) | zxing-cpp (`python3-zxing-cpp`) | Reads PDF417 and QR; zbar doesn't do PDF417. |
| Dates, GPS | exiftool (`perl-Image-ExifTool`) | Standard; falls back to Pillow/ffprobe when missing. |
| Duplicates | sha256 (exact) + 64-bit dHash on Pillow (near) | Built in; see czkawka below. |

Rejected:

- **czkawka**: the CLI is only a GitHub binary with no checksum or signature
  files, Fedora doesn't package it, and the Flathub app is GUI-only. An
  unverifiable binary for what sha256 plus a 20-line dHash does wasn't worth
  it.
- **PassportEye** (MIT): its MRZ step is Tesseract plus a regex, and it pulls
  scikit-learn, matplotlib and pdfminer. `fastmrz` (AGPL, Python <= 3.12) and
  `mrz` (abandoned 2020) add nothing.
- **Falconsai nsfw_image_detection** (Apache-2.0): only normal/nsfw for the
  whole image, so swimwear would flag; needs torch. **OpenNSFW2**: downloads
  weights at first run with no hash check.
- **phockup / elodie**: quiet repos; the date logic here is about 40 lines.
- Not searched: whether a maintained offline "sensitive documents in a photo
  library" scanner exists. The design doesn't depend on one.

## Unverified

- NudeNet false-positive rate on real photo libraries (no published numbers).
- Tesseract on embossed card numbers and cheque MICR lines.
- `scan` time: about 1-2 s an image on CPU (OCR dominates), estimated.
- Label accuracy on real photos, and the thresholds in `labels.yaml` (first
  guesses; `label.py --all` prints every score for calibrating).
- How reliably `child-in-shot` catches children, especially small or in the
  background: treat a missing flag as "not checked", not "no child".
- Label speed: measured on synthetic images only. Estimate 0.1-0.3 s an image
  on CPU, plus up to 30 frames a video.
- Whether the tokenizer matches transformers' `SiglipTokenizer` exactly
  (lower-casing and punctuation handling were reimplemented).
- HEVC videos: Fedora's `ffmpeg-free` can't decode them, so `scan` labels such
  videos `unscanned-video` for you to check (a GStreamer hardware decoder is
  used for frames where available; `media-intake doctor` reports it).

## Tests

`python3 -I -m unittest discover -s tests` (synthetic media, a throwaway HOME,
a hermetic config), or one module: `python3 -I -m unittest discover -s tests -p test_intake.py`
(also `test_sources`, `test_tagging`, `test_config`). Run long jobs under your
own memory cap if you like.
