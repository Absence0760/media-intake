# Intake: design decisions and research

`media-intake` brings photos and videos in from a phone, a GoPro, an
external drive or a folder on the laptop (`--kind folder`, [queue.md](queue.md)), files them, drops duplicates and junk, and quarantines
sensitive images for you to confirm and erase. Every path below is the
default; all of them come from `~/.config/media-intake/config.toml`
([configuration.md](configuration.md)).

## Principles

- **Sensitive files are quarantined, you confirm**, then one command erases
  them everywhere. Nothing is auto-deleted: detectors misfire (sports photos,
  swimwear, screenshots of forms).
- **Library by date, events optionally by project.** Photos go to
  `~/Pictures/library/<YYYY>/<YYYY-MM>/`, videos (GoPro included) to
  `~/Videos/personal/<YYYY>/<YYYY-MM>/` (a dedicated camera's files in a
  `<device id>/` folder there, see "Devices" below), screenshots to
  `~/Pictures/library/Screenshots/<YYYY>/`, screen recordings to
  `~/Videos/personal/Screen recordings/<YYYY>/` (a video in a phone's
  `Screenshots/` folder is one too), messaging-app media to
  `~/Pictures/library/Messaging/<app>/<YYYY>/`. An event routed to a configured
  project goes to `<project folder>/<date>-<slug>/{phone,gopro}/` (see
  "Events" below). Original file names are kept: GoPro chapters (`GX01xxxx`,
  `GX02xxxx`) only line up by name, and are dated by their first chapter.
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
  app's chats, so think before you `offload` a batch that has them. Telegram
  and Signal: see "Messaging apps" below.
- **No assistant views photos for sensitivity.** Anything an AI assistant
  views goes to its provider and its transcript on disk, which is exactly where
  a passport scan or a nude must not end up. All detection is local and
  reports labels only. An assistant may run the commands for you, but must
  never read, view, montage or describe a file under `quarantine/` or a file
  `scan.json` flags.
- **A file name is data, never part of a command.** Names come from devices
  and are untrusted. When ffmpeg can't decode a video (HEVC on Fedora's
  `ffmpeg-free`), `scan` samples its frames through GStreamer, and
  `gst-launch-1.0` re-parses its arguments as one pipeline description, so a
  `!` or a quote in a name could add elements. It reads the clip through a link
  (`in`) in a private (0700) `.gst-*` work folder inside the frames folder,
  writes the frames there, and its argv is built as a list
  (`scan_sensitive.gst_frames_command`). A work folder whose path has anything
  but letters, digits and `._/+-` (an unusual HOME) means GStreamer is not
  tried, so the clip is `unscanned-video`, never clean, and `scan` says why
  once per folder on stderr (`[gst] fallback skipped: ...`, the folder's path
  only), so you can tell it from a missing decoder. The same sampling
  serves `label`, `semantic`, `vlm_trash` and gallery thumbnails; the gallery's
  HEVC proxy does the same (docs/gallery.md).
  ffmpeg and ffprobe get every path as `file:` plus the absolute path, so a
  name or relative source like `concat:...` or `http:...` is never followed as
  a protocol.

## Messaging apps

Media from a messaging app is a **messaging copy**: filed under
`~/Pictures/library/Messaging/<App>/<YYYY>/`, tagged with the app's name in
lower case, ranked below a camera original or a screenshot when two copies are
exact duplicates, and checked against the library for near-copies (see
"Duplicates"). On a phone it comes only from folders the config names
(`[phone] extra_folders`, never a whole `Android/` tree) or from `Pictures/<App>/`,
which is read anyway. From a drive or laptop folder it is recognised by name
or layout, with no config. Layouts checked against each app's source code on
2026-11-14 (links below); real phones and exports were not.

| App | Where | How it is read |
| --- | --- | --- |
| WhatsApp | `Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images`, `.../WhatsApp Video` | config entries; on a drive or folder, `IMG-YYYYMMDD-WA*` / `VID-YYYYMMDD-WA*` names |
| Telegram (Android 11+) | `Android/media/org.telegram.messenger/Telegram/Telegram Images`, `.../Telegram Video` | config entries |
| Telegram (Android 10 and older) | `Telegram/Telegram Images`, `Telegram/Telegram Video` at the storage root | config entries |
| Telegram "Save to gallery" | `Pictures/Telegram`, `Movies/Telegram` | `Pictures/` always; `Movies/Telegram` by config entry |
| Signal "Save" (Android) | `Pictures/Signal`, `Movies/Signal`, named `signal-YYYY-MM-DD-HH-mm-ss-SSS` | `Pictures/` always; `Movies/Signal` by config entry |
| Signal "Save" (Desktop) | wherever you saved it (usually Downloads), same `signal-...` names when the attachment had no name | a laptop folder or drive source: `signal-...` names (the phone's `Download/` denial doesn't apply to a folder you stage) |
| Telegram Desktop export | `ChatExport_<date>/` or `DataExport_<date>/` (or a renamed one holding `result.json` or `messages.html` beside `photos/` or `video_files/`) | a laptop folder or drive source: only `photos/`, `video_files/` and `files/` |

Example config entries (copy the ones for apps you use; a folder that isn't
there is skipped):

```toml
[phone.extra_folders]
"Android/media/org.telegram.messenger/Telegram/Telegram Images" = "Telegram"
"Android/media/org.telegram.messenger/Telegram/Telegram Video" = "Telegram"
"Movies/Telegram" = "Telegram"
"Movies/Signal" = "Signal"
# Android 10 and older keep Telegram's folders at the storage root:
# "Telegram/Telegram Images" = "Telegram"
# "Telegram/Telegram Video" = "Telegram"
```

Never taken, by design:

- **Telegram's other folders**: `Telegram Documents`, `Telegram Files`,
  `Telegram Audio`, `Telegram Stories` (each also has a `.nomedia`), the
  cache under `Android/data/org.telegram.messenger/`. Name them in the config
  only if you really want them read.
- **In a Telegram Desktop export**: `stickers/`, `animations/` (GIFs),
  `round_video_messages/`, `voice_messages/`, `profile_pictures/`,
  `stories/`, `contacts/`, the HTML export's own `css/`, `js/` and
  `images/` (its icons), and every `*_thumb.jpg` preview. A file in `files/`
  is taken only when it is a photo or video by extension.
- **Signal's chats**: Signal keeps attachments inside its encrypted database,
  not in a folder; Android backups (`signal-*.backup`) and Desktop's backups are
  encrypted. Only files you saved out of Signal can be read. Restore a backup
  into Signal and save what you want, or stage the folder you saved them to.

Dates: an export's own names (`photo_<n>@DD-MM-YYYY_HH-MM-SS.jpg`,
`video_<n>@...`, `file_<n>@...`, the message's time on the exporting
computer) and Signal's `signal-YYYY-MM-DD-HH-mm-ss` names are `filename`
dates ("Dates" below). A file an export keeps under its own name (a video
or file sent with a name) is dated by its metadata, else its mtime: reading
`result.json`'s per-message dates for those is a ROADMAP follow-up.

**Stage the folder that holds the export, not the export itself.** A file is
labelled `messaging:Telegram` by a `ChatExport_<date>` / `DataExport_<date>`
folder in its path under the staged folder, or by its own `photo_<n>@...`,
`video_<n>@...`, `file_<n>@...` name. When the staged folder *is* the export
(or a renamed one), the same files are taken (the export rules still apply),
but its files kept under their own names are filed as ordinary photos and
videos; making the category see the source root is a ROADMAP follow-up.

Sources (checked 2026-11-14):
[Telegram Android `ImageLoader.java`](https://github.com/DrKLO/Telegram/blob/master/TMessagesProj/src/main/java/org/telegram/messenger/ImageLoader.java)
(media folders), [`MediaController.java`](https://github.com/DrKLO/Telegram/blob/master/TMessagesProj/src/main/java/org/telegram/messenger/MediaController.java)
(save to gallery), [tdesktop `export_data_types.cpp`](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/export/data/export_data_types.cpp)
(export folders, names, `_thumb.jpg`), [`export_output_abstract.cpp`](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/export/output/export_output_abstract.cpp)
(`ChatExport_`/`DataExport_` folder), [`export_output_html.cpp`](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/export/output/export_output_html.cpp)
(`css/`, `images/`), [Signal-Android `SaveAttachmentUtil.kt`](https://github.com/signalapp/Signal-Android/blob/main/app/src/main/java/org/thoughtcrime/securesms/util/SaveAttachmentUtil.kt)
(save folders and names), [Signal-Desktop `ts/util/Attachment.std.ts`](https://github.com/signalapp/Signal-Desktop/blob/main/ts/util/Attachment.std.ts)
(`getSuggestedFilename`).

## Commands

```
media-intake stage SRC_ROOT --kind phone|gopro|drive|folder [--batch NAME] [--only LISTFILE]
media-intake plan BATCH [--event [PROJECT/]SLUG@YYYY-MM-DD ...] [--clock-offset DEVICE=±Nh ...]
media-intake scan BATCH                 # local detectors; labels only
media-intake apply BATCH                # moves files; prints a summary
media-intake quarantine BATCH           # counts + the folder to open
media-intake release BATCH FILE...      # false positive -> library
media-intake erase BATCH [--phone ROOT [--keep-phone-thumbnails]] [--drive MOUNT [--no-wipe-free]] [--yes]
media-intake forget LIBRARY_FILE... [--drive MOUNT] [--yes]   # asks (type ERASE) before anything moves
media-intake verify-gone [BATCH] [--drive MOUNT]   # with BATCH, also its last erase coverage report
media-intake wipe-free MOUNT
media-intake offload BATCH --phone ROOT [--yes]   # asks (type DELETE); refused for a --kind folder batch
media-intake purge-review BATCH        # a batch name, never a path
media-intake trash [BATCH] FILE... [--phone ROOT]
media-intake trash --from-digikam [--yes]
media-intake suggest-trash [--batch B] [--phone ROOT]
media-intake not-trash FILE...
media-intake migrate-tags [--dry-run]
media-intake share-copy FILE... --out DIR     # alias: social-copy
media-intake tag BATCH [--no-labels]
media-intake tag-files FILE... [--event SLUG] [--no-labels]
media-intake mark-used FILE... --post SLUG
media-intake find [--tag T] [--event E] [--place P] [--project P] [--unused-since 30d] [--never-used]
media-intake gallery [--drive MOUNT] [--share-out DIR]
media-intake prune-cache [--dry-run]          # gallery thumbnails and proxies nothing shows any more
media-intake backup MOUNT [--verify] [--dry-run]   # library, videos, sidecars to a mounted drive (backup.md)
```

`media-intake <command> --help` shows every option. The review page is
`media-intake review BATCH` ([review-ui.md](review-ui.md)), the web gallery
[gallery.md](gallery.md), the drive and laptop-folder queue [queue.md](queue.md),
the backup to a mounted drive [backup.md](backup.md),
semantic search and the VLM trash check [search.md](search.md). `media-intake
doctor` checks system packages (and prints the `sudo dnf install` line for
what's missing), the venv, the model files' sha256 and the local Ollama
model's digest against `media_intake/pins.json`, and the config.

A typical phone batch: `stage` the phone's storage root, `plan` (add
`--event` for a shoot), `scan`, `apply`, open the review page, `tag`, then,
once the library is backed up, `offload` to free the phone.

### Progress and the `next:` line

The long steps print counts-only progress, so a big batch never sits
silent: one line when the step starts, then one every 10,000 files or 2
seconds, whichever comes first (`MEDIA_INTAKE_PROGRESS_EVERY` and
`MEDIA_INTAKE_PROGRESS_SECS` override both; the HOME sweep and the
near-duplicate search use the same lines):

- `stage: 5,000 of 12,000 files staged` while it copies;
- `plan: 5,000 of 12,000 files hashed` (sha256, dHash, video hash), then
  `plan: ... photos compared` / `videos compared` in the near-copy search;
- `apply: 5,000 of 12,000 files done`;
- `tag` (and `tag-files`): a line per phase (place names, content labels: one
  pass with no per-file lines), then `tag: 400 of 12,000 sidecars written`
  as exiftool writes them 200 at a time.

`scan` keeps its own `[scan] N/TOTAL` lines on stderr. A progress line never
names a file, and each is a whole line ending in a newline, on a terminal
too: the review page and gallery jobs and the drive queue's step logs read
them line by line.

At a terminal, each step of the flow ends with one line naming the command to
run next, with the batch's real name:

```
next: media-intake plan BATCH          # after stage (nothing staged: no line)
next: media-intake scan BATCH          # after plan
next: media-intake apply BATCH         # after scan
next: media-intake review BATCH        # after apply, while anything is in quarantine or the review folder
next: media-intake tag BATCH           # after apply, when nothing is
next: media-intake backup <MOUNT>, then media-intake offload BATCH --phone ROOT   # after tag
next: media-intake gallery             # after tag, for a laptop folder batch (no offload)
```

`apply` decides from what is there, not from that run's counts, so a second
`apply` still points at the review. A failed step prints no `next:` line. With
stdout not a terminal (a job log, the drive queue, a pipe, the tests) there
is no `next:` line at all, so `scan`'s last line stays its JSON summary;
`MEDIA_INTAKE_NEXT_HINT=1` turns it on anyway and `=0` off at a terminal too.

### Confirmation prompts

Without `--yes`, `erase` and `forget` ask you to type `ERASE`, and `offload`
to type `DELETE`, before anything is deleted. Any other answer, or none at
all, deletes nothing: with no terminal (a cron job, the folder queue, stdin
closed) or Ctrl-C at the prompt, the command prints `nothing erased` /
`nothing deleted` and exits 1, with no traceback. Every such prompt goes
through one helper, `intake.ask()`; `--yes` skips the prompt only, never the
checks on what may be deleted.

### One writer at a time: the library lock

Anything that moves, deletes or rewrites library files, sidecars or
`index.json` holds one lock per library first, so a gallery Erase, a review
run and a CLI `apply` never interleave (`media_intake/library_lock.py`):

- **The lock:** an `flock` on `<state>/locks/library-<id>.lock` (folder 0700,
  file 0600), `<id>` from the sha256 of the library root's real path. It dies
  with its holder, so a crashed or killed process never leaves the library
  locked. The file notes who holds it (a command or job key and a PID, never a
  file name).
- **CLI:** `intake.py main()` takes it for every subcommand in
  `intake.LOCKED` (stage, plan, scan, apply, release, erase, forget, offload,
  purge-review, trash, not-trash, suggest-trash, tag, tag-files, mark-used,
  migrate-tags).
  The read-only ones in `intake.READ_ONLY` (quarantine, verify-gone, wipe-free,
  share-copy, find) never do; a new subcommand counts as locked until
  it is listed there, and `tests/test_library_lock.py` fails on one in neither.
  `media-intake gallery` is cli.py's, not intake.py's (gallery.py imports
  intake.py, never the reverse); it takes the lock itself, per write.
  `media-intake prune-cache` is cli.py's too (it reads the library as the
  gallery does) and holds the lock while it deletes, for a steady view of the
  library; with `--dry-run` it writes nothing and doesn't take it.
  `media-intake backup` is cli.py's too and takes no library lock: it only
  reads the library, and checks each file before and after copying it, so one
  that changes mid-copy is skipped for the next run ([backup.md](backup.md)).
  `vlm-trash` takes it around each batch of verdicts it writes.
- **Jobs** (the review page's run, the gallery's Trash, Erase and Share-safe copy) take it with
  their job lock at submit and hold it until their last step is over; each
  `intake.py` step inherits it from the worker (descriptor plus
  `MEDIA_INTAKE_LIBRARY_LOCK_FD`) and checks it really is this library's lock,
  already held through that descriptor, so a step never waits on its own job
  and an unrelated process can't set the variable to skip the lock. A review
  run and a gallery job lock each other out, whatever their batch.
- **The gallery's own writes** (Keep, my tags, Rotate, Set event, event Rename
  and Merge) take it per request.
- **When it is busy:** the one refused is told who holds it ("job gallery
  (PID 4242)", "intake.py apply (PID 77)", and when a job's worker has died
  while its step still runs, that too). The CLI waits up to
  `MEDIA_INTAKE_LOCK_WAIT` seconds (default 10, `0` refuses at once), printing
  that it waits, then exits 75 (try again later); 10 s covers a gallery Keep,
  and a job can run for an hour, so waiting longer is opt-in. The pages never
  wait: Submit, Trash, Erase, Share-safe copy, Keep, a tag, Rotate, Set event,
  Rename or Merge get 409 with the holder.

`share-copy` writes copies with no GPS or metadata, for sending or posting:
exiftool `-all=` for images (orientation and ICC profile kept), an ffmpeg
stream copy without data streams or metadata for videos. exiftool cannot write
GoPro GPMF tags, so the GPS track is removed by dropping the stream; a video's
attached picture (cover art, a JPEG with its own EXIF) and subtitle streams
are dropped too, so a copy holds only its video and audio streams. It checks
everything before copying anything, and refuses (writing nothing) when `--out`
is inside a library root or the state folder (the gallery's Share-safe copy
has the same rule: a copy there would be taken for a library file, and could
replace an original of the same name), when a copy would land on its own
source, when two files share a name (the second copy would replace the
first), or when a file is missing. Sidecars never go with a copy.

## Dates

Each file's date picks its library folder and goes into its sidecar
(`XMP-exif:DateTimeOriginal`, with the UTC offset when it is known). The rules
live in `media_intake/dates.py`, tests in `tests/test_dates.py`. Sources, in
order (`date_src` in `plan.json`; `plan` prints a count per source):

| Source | From |
|---|---|
| `exif` | `DateTimeOriginal`, else EXIF `CreateDate`, else `ModifyDate`; local time, with `OffsetTimeOriginal` (else `OffsetTime`) as its offset |
| `video` | Apple's `Keys:CreationDate` (local time with its own offset), else QuickTime `CreateDate` / ffprobe `creation_time`, which is UTC: converted to local time with `OffsetTimeOriginal` when the file has one, else this machine's time zone for that date |
| `gps` | `GPSDateTime` (EXIF, or a GoPro's GPMF track read with `exiftool -ee`; the start of the locked run, below), else `GPSDateStamp` alone (noon UTC of that day). UTC, converted like `video` |
| `export` | a cloud export's own record of the date, from the file's Google Takeout JSON (`photoTakenTime`) or iCloud `Photo Details.csv` row (`originalCreationDate`): UTC, converted like `video` ("Cloud exports", below) |
| `filename` | a date in the name (`20261108_071502`, `2026-11-08 07.15.02`, `20261108`; a Telegram export's day-first `@08-11-2026_07-15-02`) |
| `folder` | an iPhone export's `DCIM/YYYYMM__/` folder: the 1st of that month (day unknown, so `--event` days don't match it) |
| `mtime` | the file's modification time, last resort |
| `offset` | any of the above except `gps` and `export`, shifted by `--clock-offset`; `date_from` keeps which |

A camera date (`exif`, `video`) is used unless it is **impossible** or **GPS
overrules it**; then the GPS date wins, and only without one do `export`,
`filename`, `folder` and `mtime` get a turn, in that order.

- **Impossible**: before 2000-01-02 (the QuickTime 1904, Unix 1970 and DOS
  1980 epochs, and the 2000-01-01 many cameras reset to), or more than a day
  in the future. Applies to every source. The clock was never set, so a GPS
  date replaces it without a flag.
- **Contradicted by GPS**: the file has a GPS date and the camera date is more
  than a day away from it once time zones are allowed for: 24 h between the
  two instants when the camera's offset is known, else 24 h + 14 h (the widest
  UTC offset), plus 12 h for a date-only GPS stamp. Smaller errors (a wrong
  time zone, DST) are left to `--clock-offset`.

A contradiction alone doesn't prove the camera wrong: a GPS stamp is the time
of the receiver's *last fix*, and a phone's can be days old. So GPS replaces a
contradicted camera date only when:

- the camera date is a **clock-reset default** (2015-12-31 or 2016-01-01, a
  GoPro with an unset clock) and no `--clock-offset` applies: the clock was
  never set, so the GPS date is used without a flag; or
- **nothing backs the camera date**: the file name carries no date agreeing
  with it (within 14 h, so a name in UTC agrees; a date-only name agrees over
  its whole day) and the camera recorded no UTC offset (`OffsetTimeOriginal`
  / `OffsetTime`, or Apple's `CreationDate` offset; QuickTime's UTC
  `CreateDate` doesn't count). A camera that records its offset has a
  network-synced clock. The GPS date is used and the file is **flagged**.

Otherwise the camera date is kept and `date_note` says GPS disagreed: not
flagged when the file name agrees (two sources against one), flagged when only
the recorded offset backed it (a camera can record the offset of a clock set
by hand).

A camera date on a clock-reset default with no GPS date to check it is kept
but flagged, as is a file where nothing was plausible (it falls back to
`mtime`). Flagged files have `date_suspect` set in `plan.json`. `plan` lists
the files whose camera date GPS replaced, the files `--clock-offset` shifted
and the files to check (first five names each); `date_note` says why.

A GoPro video's GPMF track holds many GPS samples, and those before the
receiver locks can carry a stale time, so the earliest sample is the one most
likely to be wrong. The date is the first sample of the unbroken run (no gap
over 10 minutes) that holds the median sample: the locked run covers nearly
all of a clip, so the median falls inside it, and its first sample is the
start of recording. A plain median would land mid-clip.

**`--clock-offset DEVICE=±Nh`** (repeatable) is for a camera whose clock was
simply wrong: it shifts every date read from that device's own clock (never a
GPS date) by N hours (a sign is required; decimals up to two places, e.g.
`-1.5h`; non-zero; at most 30 years). `DEVICE` is the file's EXIF/QuickTime
`Make`, `Model` or `Make Model`, case-insensitive (`GoPro`, `'HERO11 Black'`),
its device id ("Devices" below, e.g. `gopro-hero11-black-3fa9c2`, which
tells two cameras of one model apart), or `all` for every file in the batch.
Bad syntax exits 2; the same device twice, `all` with another device, two offsets matching one file, or a device
no file in the batch has, stop `plan` before it writes anything (the message
lists the devices seen, with their ids). Corrected dates go to the XMP sidecar only: the
original keeps its own EXIF and its sha256. `tag-files` keeps a
`DateTimeOriginal` already in a sidecar (it may be a correction from the
plan) and only fills in a missing one; to recompute it, clear it first
(`exiftool -XMP-exif:DateTimeOriginal= FILE.xmp`).

Without exiftool, Pillow (images) and ffprobe (videos) read the same tags,
except a GoPro video's GPMF GPS time and any serial number: ffprobe can't
read a video's serial, so neither fallback reads one, and a camera's photos
and videos get the same Make/Model device id (Devices, below).

### Cloud exports (Google Takeout, iCloud)

A Google Photos Takeout or an iCloud Photos export ("Request a copy of your
data" at privacy.apple.com) is a folder of photos and videos: unzip it and
queue or stage it as a folder or drive source ([queue.md](queue.md)). Many of
its files have lost their own capture date (edited copies, messaging images,
scans, screenshots) and their mtime is the download time, but the export
keeps the date beside them. `stage` (`media_intake/exports.py`) reads it, for
`--kind folder` and `--kind drive` only, into the batch manifest
(`files[rel].export`: `time` in UTC, `via` `google-takeout` or `icloud`, the
`sidecar` it came from, and `lat`/`lon` when there is a place), and prints
how many files matched. `plan` turns it into the `export` date source above:
below the file's own EXIF, QuickTime and GPS dates (Google and Apple usually
copied those from the file anyway), above the file name, folder and mtime.
`--clock-offset` never shifts it (it is Google's or Apple's record, not the
camera's clock).

- **Google Takeout**: one JSON per media file, in the same folder.
  `photoTakenTime.timestamp` (UTC epoch seconds; `0` or missing: no date) and
  `geoDataExif`, else `geoData` (`latitude`, `longitude`; `0, 0` means no
  place). The JSON's name is the media's original name plus
  `.supplemental-metadata` (exports since about late 2024) or nothing (older
  exports), cut to 46 characters, then the media's `(N)` duplicate counter if
  it has one, then `.json`: `IMG_0001.jpg.supplemental-metadata.json`,
  `IMG_0001.jpg.supplemental-metadata(1).json` for `IMG_0001(1).jpg`,
  `Panorama_lake_shore_morning.jpg.supplemental-m.json`, or
  `IMG_0001.jpg(1).json` in the older form. An `-edited` copy (English
  exports only) uses its original's JSON, and a Live Photo's video half
  (`IMG_0042.MOV`) the still image's (`IMG_0042.HEIC...json`). Matching is
  by these exact candidate names, compared in Unicode NFC, never by a loose
  prefix: when two media files would claim the same JSON (two long names
  with the same first 46 characters), neither gets it (counted as
  ambiguous). A JSON without a valid `photoTakenTime` is not used. A folder
  that can't be listed when `stage` reads the metadata is skipped and
  reported (`skipped N folders whose cloud export metadata could not be
  read`); its files are staged without export dates.
- **iCloud Photos**: `Photo Details.csv` (or `Photos Details*.csv`,
  numbered variants too) in the same folder as the media, header
  `imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate`;
  `originalCreationDate` looks like `Tuesday March 3,2015 7:45 PM GMT`
  (minutes only, GMT). Rows match by `imgName`; a name listed with two
  different dates in that folder gets none. `importDate` is never used (it
  is when the file reached iCloud, not when it was taken). A file with both
  kinds of record takes the Google one.
- **Places**: the export's place is stored only when the file has no GPS of
  its own: `plan.json` keeps it as `export_gps`, and `tag` geocodes it
  offline like file GPS (a `Place|` tag, `XMP-exif:GPSLatitude/Longitude` in
  the sidecar). Nothing is ever looked up online.
- **Duplicates**: the JSON and CSV files are never staged or filed (only
  media extensions are walked). A Takeout puts the same photo in its year
  folder and in each album folder; `plan` drops the copies by sha256 against
  the batch and the library like any other duplicate ("Duplicates", below).
- Only these small files (at most 1 MiB for a JSON, `JSON_MAX`; 64 MiB for a
  CSV, `CSV_MAX`) are read, never through a symlink, and a CSV must be UTF-8;
  nothing is written to the export (its files, mtimes and folders are left
  exactly as they were). A metadata file that breaks one of these rules, or
  isn't a regular file, or fails to read, is skipped without stopping
  `stage`, its media are staged without an export date, and `stage` reports
  each reason it hit: `skipped N symlinked cloud export metadata files (never
  followed)`, `... over the size cap`, `skipped N cloud export CSV files that
  are not UTF-8`, `... that are not regular files or could not be read`
  (`exports.REFUSED`; each file counted once). Staging the same folder
  again gives the same files, dates and report.
- **Offload** of a drive deletes only the media originals: the JSON and CSV
  files stay on the drive; delete the export folder yourself once it is filed.
- **Erase** doesn't reach them either: an erased file's Takeout JSON or iCloud
  CSV row (which can hold its date, place and description) stays beside where
  it was, and the coverage report lists it under `cant_reach`.

Neither format is documented by Google or Apple. The rules above follow the
open-source importers that parse them, checked 2026-11-14:
[immich-go](https://github.com/simulot/immich-go) (`adapters/googlePhotos/matchers.go`
and its test cases, `adapters/googlePhotos/json.go`, `adapters/folder/icloud.go`
and its `Photo Details.csv` fixture; commit `bc54ad2`),
[GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper)
(`lib/date_extractors/json_extractor.dart`: the 51-character `<name>.json`
limit of the older form) and
[PhotoMigrator](https://github.com/jaimetur/PhotoMigrator)
(`help/05-icloud-takeout.md`, `src/Features/ICloudTakeout/ClassICloudTakeoutFolder.py`:
the CSV header and date formats; commit `5a6b770`). No code was copied; the
names and fields are facts of the format. Apple's
[data request page](https://support.apple.com/en-us/HT208502) describes the
export but not the CSV's columns.

### GoPro chapters

A GoPro splits a long recording into chapter files of one recording number:
HERO6 and later `GHccnnnn.MP4` (H.264) or `GXccnnnn.MP4` (HEVC), chapter
`cc` from `01`; HERO5 and earlier `GOPRnnnn.MP4`, then `GP01nnnn.MP4`,
`GP02nnnn.MP4`, ... Every later chapter takes the date (and so the folder
and the `--event` day) of the first chapter of its recording in the batch:
same folder, same device, same family (`GH`, `GX`, or `GOPR`/`GP`), same
number. A recording that crosses midnight, an hour or a month stays
together. `chapter_of` in `plan.json` names the first chapter, `date_note`
says so, and `plan` lists them. Names are matched exactly as the camera
writes them (upper case, `.MP4`): chapter `00`, a lower-case copy, other
digit counts, `.LRV`/`.THM` previews, `.360` files and look-alikes such as
`DJI_0042.MP4` or `MVI_0042.MP4` are not chapters (`tests/test_devices.py`).
When the first chapter isn't in the batch, the lowest one present dates the
rest.

### Burst id

`plan.json` also records a camera's own burst id as `burst_id` (else `null`),
read with the date (exiftool only; without it, `null`): Apple's MakerNotes
`BurstUUID` ("unique ID for all images in a burst") or Google Camera's
`XMP-GCamera:BurstID`, Apple's first when a file has both. Only a plain token
(letters, digits and `._:{}-`, at most 128) counts. No other camera documents
one; their bursts are found by file name and clock. `bursts.py` takes it as
the burst marker, so the review page ([review-ui.md](review-ui.md)) and the
gallery ([gallery.md](gallery.md), "Burst stacks") stack a burst whose shots
are further apart than the 2 s gap, and never join two bursts with different
ids. It is only read: nothing writes it to a sidecar.

### Devices

Each file's device comes from its EXIF/QuickTime `Make`, `Model` and serial
number (`media_intake/devices.py`): the **device id** is the slug of "Make
Model" (`gopro-hero11-black`, `google-pixel-8`), plus a short pseudonym of
the serial when the file records one (`gopro-hero11-black-3fa9c2`).

- **Tag**: `tag` and `tag-files` add `Source|<model>` (`Source|HERO11
  Black`, `Source|Pixel 8`) beside `Source|gopro` / `phone camera`.
- **Folders**: a dedicated camera's files (`--kind gopro`, or a GoPro file
  name from any source) go to a `<device id>/` folder inside their month
  (`~/Videos/personal/2026/2026-11/gopro-hero11-black-3fa9c2/GX010042.MP4`)
  or inside a project event's `gopro/`. Camera file numbers repeat, so two
  GoPros' `GX010042.MP4` land in two folders, never as `GX010042_1.MP4`.
  Phone files keep the plain month folder: their names carry a timestamp.
  Two cameras of one model that record no serial can't be told apart and
  share a folder (the second file still gets `_1`), as do files with no
  `Make`/`Model`.
- **Batches**: `stage` without `--batch` names the batch
  `<device id>-<YYYYMMDD>` (today) from the most common device among the
  first 200 files (read only), or `<kind>-<YYYYMMDD>` when none has a
  device. Re-staging the same root resumes that batch; another root gets
  `-2`, `-3`, ... `plan` prints the devices it saw.
- **`--clock-offset`** takes the device id as `DEVICE` (above).

**Serial numbers are personal identifiers** (they tie a file to one
owner's camera; vendors register them to a name), so no raw serial is ever
written to a folder or batch name, a tag, `plan.json`, the manifest or any
output: `read_meta` drops it from the row as soon as the id is made. The
pseudonym is the first 6 hex digits of HMAC-SHA256 over make, model and
serial, keyed with a random 32-byte salt made on first use at
`<state>/device-salt` (0600, in the 0700 state folder; never in the library
or a backup of it). Without the salt nobody can check a guessed serial
against a folder name, which an unsalted hash of a short serial would allow.
An owner-chosen alias in the config was the alternative; the hash needs no
setup and still separates cameras. Keep the salt with the rest of the state
folder: a new salt gives every camera a new id (new folders from then on;
filed files don't move). A corrupt salt file stops `plan` and `stage`
instead of being replaced. On a filesystem without hard links the salt is
made with an exclusive create instead of a link (never replacing another
run's salt). Serials are read from EXIF `SerialNumber`
(`BodySerialNumber`), `InternalSerialNumber` and GoPro's
`CameraSerialNumber`, through exiftool only; blank, all-zero and "Unknown"
values count as none. Without exiftool no serial is read at all, from
photos or videos (Pillow could read a photo's, ffprobe never a video's, and
one camera must not get two ids), so ids are Make/Model only and two
cameras of one model share a folder, as above.

## Duplicates

- **Exact**: sha256. `plan` marks an exact copy of a library file, of a file
  trashed before, or of another file in the same batch "delete"; it is never
  filed twice. Within a batch the best copy is kept: camera photo or video
  over screenshot or screen recording over a messaging copy, then not from a
  `Restored/` folder, then larger pixel count, then larger file.
- **Near**: a 64-bit dHash on Pillow (EXIF-rotated, 9x8 grey). Images within
  2 bits of another in the batch, with the same orientation, go to the batch's
  `review/` folder with `why` naming the one kept, so you glance before
  anything is deleted. Two camera photos (phone or GoPro) are never merged
  (burst shots and time-lapse frames look alike). A screenshot or messaging copy is also checked against the library
  index and goes to review when it is near a library image.
- **Near videos** (`videohash.py`): a re-encoded, resized or trimmed copy of
  a clip goes to review exactly as a near-copy photo does, never deleted. ffmpeg
  samples a frame every 0.5 s (at most 120, spread over a longer clip), scaled
  to 9x8 grey and piped as raw pixels, so no frame is written to disk; each
  gets the same dHash as a photo, and blank frames (black, fades) are dropped.
  Two clips match when at least 80% of the shorter one's frames are each
  within 10 bits of some frame of the other, compared as a set, ignoring
  order and time, so a trimmed copy matches its original. Camera videos (phone, GoPro, a
  drive's or folder's videos) are camera originals like camera photos: two are
  never merged, and only a messaging copy or screen recording is checked
  against the library. A clip ffmpeg can't decode (HEVC on Fedora's
  `ffmpeg-free`) is sampled through `scan_sensitive.video_frames`, GStreamer
  included, into a private folder removed at once. A clip that still can't be
  read, or has fewer than 3 frames with any contrast, gets `vhash_note`
  (`unreadable`, `too short or blank to compare`) in `plan.json` and is
  counted in plan's output: "not checked", never "unique". `plan` decodes
  every video once for this; expect roughly the clip's length or less per
  clip on CPU.
  - Research (2026-11-14): Meta's [vPDQ](https://github.com/facebook/ThreatExchange/tree/main/vpdq)
    hashes a frame a second with PDQ and matches frame sets by the share of
    each video's frames that match, ignoring timestamps, which is what makes
    it trim-tolerant; its C++ build needs CMake and the libav* libraries, so
    its method is used here with the dHash the photos already use, and no
    new dependency. [videohash](https://github.com/akamhy/videohash) (MIT,
    last PyPI release 3.0.1 on 2022-05-29, pulls yt-dlp) makes one 64-bit
    hash of a frame collage and says it can't tell whether one video is part
    of another: rejected. Thresholds were set on lavfi patterns (a
    re-encode, a resize and a 1.3 s trim were within 5 bits a frame; unlike
    clips 15 or more), not on real phone video: see Unverified.
- Images under 200 px and empty files go to review as junk.
- `index.json` in the state folder keeps sha256 and dHash of everything in
  the library (and each filed video's frame hashes, `vhash`), so later
  batches are checked against it.
- **A move never replaces a file** (`intake.free_path`). `release` puts a
  file back at its old place only while that place and its sidecar are free.
  If another file was filed there since (erase moved `2026-08/IMG_1.jpg` to
  `near-<batch>`, a later apply filed a different `IMG_1.jpg` there), the
  released file goes to the first free `IMG_1_<n>.jpg` beside it, with its
  own sidecar and `index.json` entry, and `release` prints where it went;
  the file already there keeps its content, sidecar and index entry. `apply`
  does the same for a planned place filled after `plan` (and rewrites
  `plan.json`, so `tag` and `offload` find the file), and leaves a flagged
  file in `raw/` (counted) when its quarantine place is taken. Near-copies
  moved into quarantine, and `forget`, never land on an existing file or
  sidecar either.

## Trash vs erase

`trash [BATCH] FILE... [--phone ROOT]` is a plain delete for junk that isn't
private: the file (library or review copy), its `.xmp` sidecar,
its thumbnails (freedesktop and the gallery's) and the gallery's H.264 proxy
of a video, its `index.json` entry, and
with `--phone` its phone original (the manifest's `src`, checked by
`sources.check`). No home sweep, near-duplicate search, erase log or
verify-gone. Its sha256 and size (no name) go to `trashed.json`, and `plan`
marks any later copy "delete" with why "trashed before", so it never comes
back from another device or a backup. Anything private goes through `erase`
(or `forget` for files already in the library). The review page offers Trash
(T) on Review and Maybe trash items, never on a quarantined one (Erase or
Keep only: [review-ui.md](review-ui.md)), and `trash` itself refuses a file
under `quarantine/`: it leaves that file, its sidecar and `quarantine.json`
untouched, reports "refused N quarantined" and exits 1 (other files named with
it are trashed as usual).

`forget FILE...` checks every file first: one missing (or a symlink, or not a
regular file) refuses the whole command, and without `--yes` it asks (type
ERASE) before anything moves. A refusal or a "no" leaves every file, sidecar
and `index.json` entry where it was; nothing is left half-forgotten in
quarantine. A file named twice is forgotten once.

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
- **The gallery's caches.** Its thumbnails
  (`~/.cache/media-intake/thumbs/<sha256>-r<turn>.jpg`, one per rotation, and
  `<sha256>.jpg` from older versions) and H.264 video proxies
  (`~/.cache/media-intake/proxies/<sha256>.mp4`) are not exact copies, so the
  sweep can't find them: `erase`, `forget` and `trash` delete them by the
  erased item's sha256 (every rotation), and `verify-gone` reports any left.
- **verify-gone.** The erase log keeps checksum and size, no names, so
  `verify-gone [BATCH]` repeats the sweep read-only and exits 1 if anything is
  left (including orphaned sidecars, gallery thumbnails and gallery video
  proxies). `erase` runs it
  last. Given a batch, it then prints that batch's last coverage report in
  full (below), or says the batch was erased before reports were kept.
- **Coverage report.** Every `erase` (and so every `forget`) ends with a
  report of what it reached and what it can't, and appends it to
  `erase-log.txt` as one line, `<date> <time> <batch> coverage <json>`, beside
  the batch's `sha256:` lines (which `verify-gone` and `label.py` read; the
  coverage line never matches them). Counts and fixed place names only, never
  a file name. The JSON has four keys:
  - `reached`: a count for every place it looked, zeros included: quarantined
    files, library copies, other copies in HOME (the sweep), sidecars, Trash
    entries, freedesktop thumbnails, the gallery's thumbnails and proxies,
    recent files, Showtime's positions and log lines, localsearch's error
    reports, near-duplicates moved to quarantine (not deleted), gvfs-metadata
    entries cleared, localsearch resets and files checked forgotten, and with
    `--phone` the device originals, camera previews and phone thumbnails, with
    `--drive` the drive's copies and the MB the free-space wipe wrote, and the
    entries dropped from the backup records (`<state>/backup/*.json`, which
    keep library names: an entry goes when its path was erased or moved, or
    its sha256 is an erased item's, with its `.xmp` entry, whatever its path;
    only bookkeeping, so a file still in the library is hashed again by the
    next `backup`; `erase --drive`
    removes the copies on the drive by checksum, so the next `backup` has
    nothing to bring back).
  - `left`: what it found but did not remove (exact copies inside git repos,
    near-duplicates left in place, thumbnails the phone refused, files
    localsearch still has indexed after the reset or could not check,
    gvfs-metadata entries gio could not clear, backup records it could not
    rewrite, backup records it could not read (not JSON, another version: left
    as they are, they may keep names until the next `backup` to that drive
    starts them afresh), anything verify-gone found).
    A check that got no answer lands here, never under `reached`. Empty when
    nothing was left.
  - `not_checked`: what it was not asked or able to look at (no `--phone`,
    no `--drive`, `--keep-phone-thumbnails`, `--no-wipe-free`, `gio` or
    `localsearch` not installed).
  - `cant_reach`: the fixed list nothing on this machine can reach: cloud
    services and sync, chat apps, email and the people it was sent to,
    app-private storage on a phone or camera (a gallery app's own trash),
    other computers and backup drives not named with `--drive`, filesystem
    snapshots and the blocks an SSD keeps aside, re-encoded or trimmed video
    copies, and a cloud export's Takeout JSON or iCloud CSV row left beside an
    erased file on its source drive.

  `erase` prints a short form (places with a count, what was left, what was
  not checked, what it can't reach); `verify-gone BATCH` prints the full one.
- **Progress while it searches.** The HOME sweep, the drive sweep, the
  near-duplicate search and verify-gone can take minutes on a big HOME, so
  each prints a start line (what it searches, how many erased items), then a
  line every 10,000 files or every 2 s, whichever comes first (`home sweep:
  12,000 files checked, 0 copies so far`), then its summary. Counts only,
  never a file name. stdout is line-buffered (and jobs run `python3 -u`), so
  a gallery Erase or `forget` streams these to its job log as they happen.
  `MEDIA_INTAKE_PROGRESS_EVERY` / `MEDIA_INTAKE_PROGRESS_SECS` change the
  two intervals (the tests set a count and `inf` seconds).
- **Near-duplicates** (a messaging-app recompress, a resized edit) don't match
  by checksum. `erase` takes the dHash of each erased image before deleting it
  and moves any image within 4 bits and with the same orientation to
  `quarantine/near-<batch>`, never deleting it: you look, `release` false
  matches and erase the rest (`media-intake review <batch>` shows every
  level). Each record keeps the matched image's sha256, the erase date and,
  when the erased image was a copy of a surviving library original (an xcheck
  `of`), that original as `root_of`, never the erased file's path. Erasing a
  near batch can find near-copies again (`near-near-<batch>`); the chain stops
  at depth 3 (`NEAR_DEPTH`, 12 dHash bits from the first erased photo), past
  which matches stay where they are and erase prints the count. An existing
  level is added to, never replaced, and `of`/`root_of` originals are never
  moved. It looks only in the media library (library,
  videos and project folders, plus batches' `raw/` and `review/`), never in
  caches, app icons, map tiles or git repos, which hold look-alikes that
  aren't your photos. Images under 200 px and featureless hashes are ignored.
  Videos: erase finds exact copies only; a re-encoded, resized or trimmed copy
  of an erased video is neither found nor moved. (The near-video check in
  "Duplicates", above, runs only in `plan`, on a new batch.)
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
    --file` on each erased path inside an indexed folder, then `localsearch
    info` on it to check the index forgot it (a stored file has a `nie:url`),
    and deletes its failure reports
    (`~/.cache/tracker3/files/errors/<md5(uri)>`). A file still indexed is
    counted under `left` in the coverage report, and so is one `localsearch`
    could not answer for: each call gets 20 s (`GIO_TIMEOUT`), and after one
    times out (a hung miner) the rest of that erase skips localsearch. The
    whole index is never reset. verify-gone can't repeat the check: the erase
    log keeps no paths.
  - Thumbnails: `erase` clears `~/.cache/thumbnails` entirely (rebuildable),
    since thumbnails are keyed by URI and a phone browsed in Files gets
    `mtp://` keys.
  - GNOME's per-file metadata (`~/.local/share/gvfs-metadata`: emblems, icon
    positions, custom icons that Files and other apps set through gio; names
    only, no image data) is keyed by path in a binary database that only
    `gvfsd-metadata` writes, and is never edited by hand. GLib drops a file's
    entry itself when the file is deleted through gio (`g_file_delete` calls
    the VFS's `local_file_removed`, which asks the daemon to `Remove` it), so
    device deletes (`gio remove`) need nothing more. Files on this machine and
    the `--drive` are deleted with a plain unlink, so before each one (and
    before `forget` or the near-duplicate search moves a library file) `erase`
    lists its keys with `gio info -n -a metadata FILE` and unsets each with
    `gio set -n -t unset FILE metadata::KEY`, which works only while the file
    exists. A gio call that fails, or leaves a key, counts the file under
    `left`; each call gets 20 s, and after one times out (a hung
    `gvfsd-metadata`) the rest of that erase skips gio. Not covered: entries
    for files deleted earlier outside gio (no
    documented route clears an entry once its file is gone), and entries
    under a folder rather than a file. Without `gio`, the coverage report
    lists gvfs-metadata as not checked. Checked 2026-11-14 against GLib's
    [glocalfile.c](https://gitlab.gnome.org/GNOME/glib/-/blob/main/gio/glocalfile.c),
    [gio-tool-info.c](https://gitlab.gnome.org/GNOME/glib/-/blob/main/gio/gio-tool-info.c)
    and the [gio manual](https://gitlab.gnome.org/GNOME/glib/-/blob/main/docs/reference/gio/gio.rst),
    and gvfs's [gdaemonvfs.c](https://gitlab.gnome.org/GNOME/gvfs/-/blob/master/client/gdaemonvfs.c).
    `MEDIA_INTAKE_NO_GVFS_METADATA=1` switches it off (the tests' default).
- **The state folder.** Quarantine and staging live in
  `~/.local/share/media-intake/` (0700), not under `~/Pictures`, so desktop
  search and thumbnailers don't pick them up.
- **Backup drive (exFAT).** `erase --drive` deletes matching files anywhere on
  the drive by checksum, then `wipe-free` fills the free space with zeros
  (64 MiB writes to `.media-intake-wipe.N` until the drive is full, fsync,
  delete, sync), since exFAT deletes only free the clusters. It refuses any
  path whose mount (`findmnt -T`) is `/`, `/home`, `/var`, `/tmp` or `/boot*`
  (`backup` refuses a wider list of the laptop's own mounts,
  `sources.LAPTOP_MOUNTS`: see [backup.md](backup.md)).
  On flash (`rotational` 0) it also tries `fstrim`, but wear-levelling keeps
  spare blocks, so an overwrite is no guarantee there. The robust fix is a
  LUKS-encrypted backup drive, or a drive wiped whole. `media-intake backup`
  writes that drive ([backup.md](backup.md)).
- **Phone.** `erase --phone` deletes the originals over MTP and, by default,
  `DCIM/.thumbnails` and `Pictures/.thumbnails` (Android rebuilds them;
  `--keep-phone-thumbnails` leaves them alone). Exactly those two paths: a
  symlink inside either, or anything in them that isn't a plain thumbnail
  path, **refuses the whole erase** before anything is deleted, naming the
  path. Your yes covered folders of thumbnails, so one that holds something
  else is for you to look at on the phone, not for erase to work around.
- **GoPro card.** Beside each chapter a GoPro writes a low-resolution copy
  (`GL010042.LRV` for `GX010042.MP4`; `GOPR0042.LRV` for `GOPR0042.MP4`) and
  a thumbnail (`GX010042.THM`). `stage` never copies them, so they are not in
  the batch, but they hold the same footage: `erase --phone` deletes the
  previews of each chapter it deletes (one it skips, say because its size
  changed, keeps them) by name, from the same folder, through the same gate (`sources.check_preview`: allowlisted folders, `.LRV`/`.THM` only,
  never a symlink), and prints `camera previews removed: N`. Other chapters'
  previews stay; `offload` leaves previews on the card. Previews go before
  their chapter, so an erase stopped by a refused delete leaves the chapter
  on the card and running erase again finishes the job; once it has, a
  second run finds the quarantine empty and touches nothing. Without `--yes`
  erase asks (type ERASE); any other answer, or none (no terminal), erases
  nothing.

## Device deletes and case-insensitive storage

Every delete on a phone, camera or source drive (`erase --phone` originals, a
GoPro's previews of them and phone thumbnails, `offload`, `trash --phone`, the
gallery's and review page's versions of those, which run the same commands)
goes through one gate in `intake.py`: `device_targets` checks every path
first, then `device_remove` runs `gio remove`. It is the only place media-intake changes a device, and it
never renames or moves a file there. `release`, `forget` and the HOME/drive
sweep never touch a phone; the sweep and `erase --drive` delete only files
they have just hashed as an erased item, so the name they delete is the
content they matched.

Before any delete, the gate checks each target: inside the `--phone` root,
inside the allowlist (`sources.check`: `erase` and `trash` skip a phone
original outside it with a line; `offload`, and anything amiss in the phone
thumbnail folders, refuse the whole command), and the same size as the staged copy
(a file whose size changed is skipped quietly). A phone original whose
manifest row records no size refuses the whole command, listing the paths:
its copy can't be checked, so it is never deleted unchecked; stage the batch
again. Only the phone thumbnail folders, which have no staged copy, skip the
size check, and they say so with an explicit marker (`NO_SIZE_CHECK`).

Android's emulated storage, and the FAT/exFAT of cameras and drives, ignore
letter case. After a case-only rename (`IMG_0001.JPG` to `img_0001.jpg`) a
listing over MTP or gphoto2 can show both names; on the device they are one
file, so deleting the "duplicate" deletes the real one. So the gate refuses
the whole command, before anything is deleted, when any target's path
differs only by case (Unicode NFC, then case-folded) from another path in the
batch manifests (every batch's, for `trash` without a batch), from another
target, or from anything the device lists right now, whether or not the other
name is being deleted too. The manifests only know what was there at staging,
so the gate also takes the device's live listing at delete time
(`sources.live_case_matches`): for each target it lists the folders on the way
down from the `--phone` root and follows every name that matches
case-insensitively, so a file copied onto the device after staging, one the
manifest never recorded (outside the allowlist, in a hidden folder), or a
case twin of a folder above the target, blocks the delete just the same. If
that listing can't be taken (the device was unplugged or locked, a folder
can't be read) or doesn't show a target at all, the whole command is refused
before anything is deleted; it never falls back on the manifests alone. The
refusal lists each group of colliding paths. Look on the device itself, rename or delete by
hand, then stage the batch again. A collision inside the thumbnail folders
refuses `erase` too; `--keep-phone-thumbnails` skips them.
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
  `screen recording` / a messaging app's name in lower case, plus
  `Source|<model>` (see "Devices" above), `Event|<slug>`,
  `Place|<city>, <state>, <country code>`, `Label|<label>`,
  `Review|child-in-shot`, `Review|untagged-video`, `Used|<slug>`),
  `XMP-dc:Subject` with each leaf, `XMP-iptcExt:Event`,
  `XMP-photoshop:City/State/Country`, `XMP-iptcCore:CountryCode`,
  `XMP-exif:GPSLatitude/GPSLongitude` (read from the file: EXIF, QuickTime,
  GoPro GPMF with `-ee`) and `XMP-exif:DateTimeOriginal` (the plan's date,
  with its UTC offset when known; see "Dates" above).
- **Events**: `plan --event [PROJECT/]SLUG@YYYY-MM-DD`. With a `PROJECT/`
  prefix, or a slug matching one of a project's `events` fnmatch patterns,
  that day's camera files (photos, videos, GoPro; not screenshots or messaging
  copies) go to `<project folder>/<date>-<slug>/{phone,gopro}/` (a GoPro's
  files in `gopro/<device id>/`, see "Devices" above) and get
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
- **Released files get the normal library mode**: the same umask-based mode an
  ordinary intake copy gets, not a fixed world-readable `0644`.
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
ONNX export at the revision pinned in `media_intake/pins.json` (with every
file's sha256; `scripts/update-pins.py` regenerates it). Runtime file
`onnx/vision_model_quantized.onnx`, downloaded by `setup.sh` into
`<state>/models/` and checked again on every run and by `media-intake
doctor`; CPU, onnxruntime.

- Preprocessing from `preprocessor_config.json` at that revision: bicubic
  resize to 224x224 (no crop), rescale 1/255, mean 0.5, std 0.5. Output
  `pooler_output` is the image embedding.
- `logit_scale` 4.7649970054626465 and `logit_bias` -12.9324369430542 are not
  in `config.json`; they were read from `google/siglip-base-patch16-224`'s
  `model.safetensors` at `7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed`. Score
  per label: `sigmoid(cos * exp(logit_scale) + logit_bias)`, max over the
  label's prompts, multi-label.
- Labels and prompts: `media_intake/labels.yaml`; text embeddings:
  `media_intake/labels.npz` (embeddings plus the prompt list and the SigLIP
  revision from `pins.json` they were built with, so `label.py` refuses a file
  that doesn't match `labels.yaml`, records another revision or records none),
  generated by `build_label_embeddings.py` with
  `onnx/text_model.onnx` and `tokenizer.json` from the same revision
  (sha256-pinned in `pins.json`), and `tokenizers` (hash-pinned in `requirements-labels.txt`,
  installed into `~/.cache/media-intake-build/site`, never into the runtime).
  Regenerate after editing `labels.yaml` or moving the SigLIP pin:
  `bash setup.sh --build-labels`.
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
  ones you kept (`Owner|not-trash`, `Owner|keep`, the legacy
  `Review|not-trash`, digiKam Accepted), `Used|*` or a protected project's
  `Project|<name>`; plus copies marked `Owner|trash`. They show in the review
  page's "Maybe trash" tab: Keep runs `not-trash` (`Owner|keep` +
  `Owner|not-trash` on; `Review|maybe-trash`, `Owner|trash` and the legacy
  spelling off), Trash runs `trash`. Nothing is deleted without a decision.
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
| Your keywords (digiKam), digiKam pick labels, `Owner\|keep`, `Owner\|not-trash`, `Owner\|trash` | you (digiKam; Keep and Trash in the gallery and on the review page) | never touched by any tool except your next decision on the same file (Keep drops `Owner\|trash`); `trash --from-digikam` acts on Rejected (PickLabel 1) only when you run it |
| `Project\|`, `Source\|`, `Event\|`, `Place\|`, dates | `tag`, `tag-files` | a prefix is replaced only when the run has a value for it |
| `Label\|*`, `Review\|maybe-trash`, `Review\|untagged-*` | SigLIP (`label.py` via `tag`/`tag-files`) | replaced as a set on every labelled run (`--no-labels` leaves them) |
| `Review\|child-in-shot` | SigLIP | added, never removed by a tool; only you clear it |
| `AI\|vlm/<model>/...` (and the older `Review\|vlm-trash/*`, `Review\|vlm-keep`) | the VLM (`vlm_trash.py`) | its own; tagging never touches them |
| `Used\|<slug>` | `mark-used` | never removed |

- `Review|maybe-trash` is never added to a file with `Owner|not-trash`,
  `Owner|keep`, `Owner|trash` (or the legacy `Review|not-trash`) or digiKam
  Accepted.

#### Owner decisions

One definition (`media_intake/owner.py`) that every tool reads: tag, tag-files,
suggest-trash, vlm-trash, semantic `--clean`, the gallery and the review page.

- **Keep** (`not-trash`, the review page's Keep, the gallery's Keep):
  `Owner|keep` + `Owner|not-trash` in the sidecar; removes `Review|maybe-trash`,
  an older `Owner|trash` and the legacy `Review|not-trash`. Never suggested as
  trash again; the VLM never judges it.
- **Trash** (`trash`, the review page's Trash, the gallery's Trash) deletes the
  file and its sidecar, so the decision can't live there. Its durable home is
  the sha256 in `<state>/trashed.json`, written before the delete: `plan`
  drops that content if it ever comes back. Library copies of the same sha256
  that `index.json` knows and that stay on disk (`trash` deletes only the paths
  you name) get `Owner|trash` in their sidecar (only copies `index.json`
  knows: a copy made outside the tool is not looked for and is not marked), so suggest-trash and the
  gallery offer them for a confirm and the VLM leaves them alone. A copy with
  its own keep decision is left as it is. Keep on such a copy later wins (it
  drops `Owner|trash`; the sha stays in `trashed.json`, where it changes
  nothing while a library copy exists).
- **Legacy `Review|not-trash`**: read as `Owner|not-trash` until it is
  rewritten, never written. `media-intake migrate-tags` rewrites every library
  sidecar once (idempotent; `--dry-run` counts), and any re-tag of a file
  (`tag`, `tag-files`) or Keep rewrites it on the way. Reading it stays because
  a sidecar can come back unmigrated (a restored backup, a digiKam copy), and
  dropping the read would turn a kept file into a trash suggestion.
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
| Video near-duplicates | dHash of frames sampled by ffmpeg, matched as sets (vPDQ's method) | Built in; see Duplicates. |

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
- **phockup / elodie**: quiet repos; the date logic here (`dates.py`) is small and
  needs rules they don't have (GPS checks, iPhone folders, per-device offsets).
- Not searched: whether a maintained offline "sensitive documents in a photo
  library" scanner exists. The design doesn't depend on one.

## Unverified

- Devices on real cameras: which tag a real GoPro's serial is in (photo and
  video), whether exiftool reads it without `-ee`, and the Make/Model a real
  GoPro MP4 reports. Tested on synthetic EXIF only. A camera whose serial
  isn't read gets the model-only id.
- GoPro chapter naming follows GoPro's documented file naming; it was not
  checked against a real card, nor were a real long recording's per-chapter
  timestamps.
- Dates on real devices: the date rules were tested on synthetic files only. Not
  checked against a real GoPro (that its GPMF `GPSDateTime` reads as expected
  with `exiftool -ee`, and whether a GoPro writes UTC or local time into
  QuickTime `CreateDate`; this code assumes UTC as the spec says, and
  `--clock-offset` corrects a camera that doesn't) or a real iPhone export's
  `DCIM/YYYYMM__` tree.
- Video near-duplicate thresholds (`videohash.py`: 10 bits a frame, 80% of
  the shorter clip) on real phone video: tested on synthetic clips only. A
  static scene filmed twice, or a clip that is mostly one title card, may
  match: it only goes to review.
- Cloud exports: the Google Takeout and iCloud rules ("Cloud exports") were
  tested on synthetic folders written from the naming rules above, not on a
  real Takeout or iCloud export. Google localises the `-edited` suffix
  (`-modifié`, ...); only the English one is recognised.
- Erase on a real GNOME session: `gio info` / `gio set -t unset`
  (gvfs-metadata) and `localsearch index`, `reset --file` and `info` (the
  search-index check) were checked against their documentation and source and
  run against fakes only, not against a real `gvfsd-metadata` or localsearch
  miner. Unconfirmed: the `nie:url` test for "still indexed", whether `reset`
  has finished before `info` runs, how a hung `gvfsd-metadata` or miner shows
  up (the fakes simply never answer, and the 20 s `GIO_TIMEOUT` is a guess),
  and the `localsearch index` output format the indexed-folder check parses.
- Messaging-app layouts (Telegram, Signal; "Messaging apps" above): read from
  each app's source code, not checked on a real phone or a real export.
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
  used for frames where available; `media-intake doctor` reports it). An
  empty, truncated or audio-only video, or a decoder that is missing or hangs,
  gets the same label. "Not checked" is never "clean", so these go to
  quarantine too: a barcode decoder error on a readable image gives
  `unscanned-barcode`, a NudeNet error on a photo or any frame of a video
  `unscanned-nudity`, a staged kind no detector reads `unscanned-type`, and a
  check that crashes on one file `unscanned-error` (the rest of the batch is
  still scanned and `scan.json` still written). GoPro `.gpr` raw files are
  read through Pillow; one it can't open is `unreadable`.
- `backup` on real exFAT, ext4-on-LUKS and NTFS drives (fsync refusals, name
  rules, timestamps): tested on a folder standing in for the drive and a faked
  `findmnt` only ([backup.md](backup.md), "Not tested here").

## Tests

`python3 -I -m unittest discover -s tests` (synthetic media, a throwaway HOME,
a hermetic config), or one module: `python3 -I -m unittest discover -s tests -p test_intake.py`
(also `test_intake_output`: the progress lines of stage, plan, apply and tag by count, none naming a file, and the `next:` line for a clean, quarantined and review batch, on a terminal and off it; `test_dates`, `test_devices`, `test_sources`, `test_exports` (Google Takeout and iCloud export metadata), `test_tagging`, `test_config`, and `test_device_case`: offload,
erase and trash on fake MTP, gphoto2 and drive trees with case-only collisions, in the manifests or only in the live listing, and a listing that fails; `test_device_ops_media_kinds`:
stage, plan, apply, erase, offload and verify-gone with photos, videos and a mix from a phone, a GoPro card,
a drive, a laptop folder and the drive queue, and erase's and offload's prompts (a wrong answer or closed
stdin deletes nothing, with no traceback; the typed word does); `test_library_lock`: the
CLI, a gallery write and a job contending for the library lock; `test_videohash`: the video hash on
ffmpeg patterns (re-encoded, resized and trimmed copies match, a different, blank or unreadable clip
doesn't, a name like `concat:x.mp4` is read as a file), `plan` routing a near-copy video to review, and
plan and apply run twice storing the same hashes once; `test_backup`: `backup` and `--verify` on a fake
drive folder (media and sidecars only, reruns, a corrupted, missing, changed or vanished copy, the
laptop's own disk, the library or state folder, a symlink or a marker refused, originals unchanged, a
private record); `test_cache_prune`: `prune-cache` keeping the library's thumbnails and proxies and
dropping the rest (dry run, unreadable files, symlinked cache folders, the library lock);
`test_erase_coverage`: erase's coverage report, gvfs-metadata and localsearch against fake `gio` and
`localsearch` (failed, hung, vanished and symlinked cases), and the backup records after `backup`, `erase --drive`,
`forget` and `verify-gone` (an erase outside the backed-up folders, a shared sha256, a record it can't rewrite or read); `test_library_ops_media_kinds`: every
library-side command on a photo, an mp4 and a .mov, alone and mixed; `test_detectors_media_kinds`: scan,
vlm_trash, semantic, label and the plugin hook on photos, videos and mixed batches, broken videos that
must be flagged "not checked", never clean, and the GStreamer fallback with a hostile file name and its one-line skip note for an unsafe work folder (fake tools); `test_offline_guard`). Run long jobs under your
own memory cap if you like.
