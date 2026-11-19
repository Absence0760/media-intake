# Wrong dates: check-dates and fix-dates

A camera whose clock was never set, or lost its battery, writes a default
date: a GoPro's footage from 2024 can be filed under 2016. `plan` already
replaces such a date when the file has a GPS time ([intake.md](intake.md),
"Dates"), but a file without GPS keeps it (flagged at the time), and files
filed before today's checks existed were never looked at. `check-dates` finds
them across the whole library; `fix-dates` (or the gallery's **Fix date**)
corrects them, in the sidecar only, and can undo every fix.

```sh
media-intake check-dates                      # the whole library; changes nothing
media-intake check-dates ~/Videos/personal    # only these files, compared with each other
media-intake check-dates --json               # the report as JSON
media-intake fix-dates --suggested FILE...    # a dry run: each old and new date
media-intake fix-dates --suggested FILE... --yes
media-intake fix-dates --undo --yes           # put the last fix back
```

`media_intake/datecheck.py`; tests: `tests/test_datecheck.py`,
`tests/test_gallery_dates.py`, `tests/browser/test_browser_gallery_dates.py`.

## What check-dates reads and writes

- **Every library file**: photos and videos under the library, videos and
  project folders, as the gallery shows them (no hidden folders, symlinks or
  state folder). With PATHS, only those files or folders, and the sequence
  check compares them only with each other (so check the whole library to
  find a file out of order with its neighbours in another month's folder).
- **Never** a file scan flagged, quarantined or erased (`label.refused`):
  such a file is not read at all, only counted ("left out").
- **The date in use** is the sidecar's `XMP-exif:DateTimeOriginal` (what `tag`
  wrote from the plan, a GPS or `--clock-offset` correction, a fix), else what
  the file itself gives by the rules in [intake.md](intake.md) ("Dates").
  Beside it, the file's own camera date, its GPS time (a GoPro's GPMF track
  with `exiftool -ee`, which reads the whole clip: `--quick` skips it), a date
  in its name, its batch's cloud export date and iPhone `DCIM/YYYYMM__`
  folder (from the batch's plan and manifest), its modification time, its
  `index.json` sha256, its GPS position and its sidecar's.
- **Writes only its report**, `<state>/datecheck.json` (0600), for the
  gallery's **Dates to check** view. A run over PATHS replaces only those
  files' findings. It never takes the library lock (it can run for long on a
  big video library, and the gallery stays usable); run it again after a
  batch of fixes. Output is paths, dates and reasons, never file contents.

Each finding has a kind, a confidence (`high`, `medium`, `low`), a reason in
plain words and, when the tool can tell, a suggested date and where it came
from (`gps`, `filename`, `export`, `neighbours`, `camera`).

## The checks

### Clock-reset default (`reset`)

The date in use falls on a day a clock starts from when it was never set.
The clock counts up from there, so the default *day* is the tell; a reset
clock that ran for days is caught by the device era check instead.

| Day | Applies to | Confidence | Why |
|---|---|---|---|
| 2015-12-31, 2016-01-01 | GoPro (any make with "gopro"); no make recorded: medium; another make: skipped (a real New Year's photo) | high | A GoPro with an unset clock starts at 2016-01-01 00:00; QuickTime dates are UTC, so west of UTC it shows as the evening of 2015-12-31 |
| 1904-01-01 | any | high | the QuickTime epoch: an unset video date |
| 1970-01-01 | any | high | the Unix epoch |
| 1980-01-01 | any | high | the DOS/FAT epoch |
| 2000-01-01 | any | high | the date many cameras and phones reset to |
| 2004-01-01, 2005-01-01 | any, in the midnight hour only | low | a start some older compact cameras use (not tied to a vendor: treat as a hint) |

`plan` already treats a date before 2000-01-02 as impossible, so those epoch
days turn up only in sidecars written by older versions or other tools.
Sources: the GoPro default is reported in GoPro's community forum
([camera keeps asking to update date/time, files dated
2016](https://community.gopro.com/s/question/0D53b00008BtWt0CAF/camera-keeps-asking-to-update-datetime-and-all-files-are-dated-2016));
the exact 00:00 and the models that do it are not documented by GoPro, and
older HERO3-era cameras reset to other dates. The suggestion is the file's
GPS time, a full timestamp in its name or its export date when one disagrees,
else its numbered neighbours' (sequence, below).

### Device era (`era`)

The camera or phone model was first sold after the date in use: a HERO12
can't have filmed in January 2016. A small table in `datecheck.ERAS` maps the
EXIF `Make` / `Model` (the same values as the `Source|<model>` tag) to the
first month any model of that family was on sale, minus three months of
slack (review units, time zones). A model it doesn't name (anything not
below, a plain "HERO") is never checked. Each family's first month is the
earliest of its variants, so a later variant (iPhone 12 mini, Pixel 9 Pro,
Galaxy S21 FE) is only ever checked against an earlier date, never a later
one. Confidence: high.

| Family | First on sale (year-month) | Source |
|---|---|---|
| GoPro HERO3 to HERO13 | 2012-10, 2014-10 (HERO4), 2016-10, 2017-09, 2018-09, 2019-10, 2020-09, 2021-09, 2022-09, 2023-09, 2024-09; HERO3+ counts as HERO3 | [GoPro investor press releases](https://investor.gopro.com/press-releases/default.aspx) (HERO4, HERO8, HERO13 and MAX checked there; HERO3 and HERO5 on PR Newswire) |
| GoPro MAX | 2019-10 | same |
| iPhone 6 to 17, 6s, X, XS, XR, 16e, Air, SE (2nd and 3rd generation) | 2014-09, 2015-09, 2016-09, 2017-09, 2017-11, 2018-09, 2018-10, 2019-09, 2020-10, 2021-09, 2022-09, 2023-09, 2024-09, 2025-02, 2025-09, 2020-04, 2022-03 | [Apple: Identify your iPhone model](https://support.apple.com/en-us/108044) (years), Apple Newsroom (months) |
| Google Pixel to Pixel 10 (a models too) | 2016-10 (Pixel, Pixel XL), 2017-10, 2018-10, 2019-05 (3a), 2019-10, 2020-08 (4a), 2020-10, 2021-08 (5a), 2021-10, 2022-07 (6a), 2022-10, 2023-05 (7a), 2023-10, 2024-05 (8a), 2024-08, 2025-04 (9a), 2025-08 | [Google: Identify your Pixel phone](https://support.google.com/pixelphone/answer/7158570) (years), the Google blog (months) |
| Samsung Galaxy S8 to S25 (model codes `SM-G95…` to `SM-S93…`) | 2017-04, 2018-03, 2019-03, 2020-03, 2021-01, 2022-02, 2023-02, 2024-01, 2025-02 | [Samsung Newsroom](https://news.samsung.com/global/) |

Checked 2026-11-19 against those pages; some months are from the makers'
launch posts rather than the support pages, which give years only.

### Video format (`codec`)

A video can't be older than its format: QuickTime `CompressorID` `hvc1` /
`hev1` (HEVC, first approved 2013-04, [ITU-T H.265](https://www.itu.int/rec/T-REC-H.265)),
`vp09` (VP9, bitstream frozen 2013-06) and `av01` (AV1, first released
2018-03, [AOMedia](https://aomedia.org/press%20releases/the-alliance-for-open-media-kickstarts-video-innovation-era-with-av1-release/)).
A HEIC / HEIF photo is an HEVC still, so it gets the HEVC floor. Confidence:
high. It rarely fires (a 2016 reset date is newer than all three); later
idea, not done because it isn't reliable from metadata alone: 10-bit HEVC or
HDR (HLG, Dolby Vision) newer than a phone model that could shoot it, and an
encoder or firmware string newer than the date.

### Sequence (`sequence`)

A camera numbers its files: GoPro `GXccnnnn` / `GHccnnnn` (recording
`nnnn`, chapter `cc`), `GOPRnnnn` / `GPccnnnn`, and the usual `IMG_1234`,
`DSC01234`, `DJI_0042`, `MVI_0042`, ... (2 to 5 letters, an optional `_`, 4
or 5 digits). Within one device (its device id, [intake.md](intake.md),
"Devices") and one naming family, sorted by number, the dates should rise.
The files the checks above already flagged are left out of that order (their
date is the question); of the rest, one longest run in date order stands,
and any other file whose date falls more than two days outside its nearest
standing neighbours is an outlier. Confidence: high with neighbours on both
sides and more than 30 days off, else medium.

Every outlier, and every file the other checks flagged, gets its
neighbours' bracket as the suggestion: the date in between, in proportion to
its number (`GOPR0104` halfway between `GOPR0103` at 10:20 and `GOPR0105`
at 10:40 gets 10:30), or the one neighbour's date at the end of a run.
Not compared: files with no device id (no Make / Model), groups of fewer than
three, and a group whose numbers repeat (a counter that wrapped past 9999, or
two cameras sharing an id), counted in the summary. Names with a timestamp
(`PXL_20241108_…`, `20241108_071502.jpg`) are compared by the next check.

### Sources disagree (`sources`)

The date in use against every other date the file holds, beyond what time
zones explain (a day plus 14 h, as `plan`'s GPS rule):

- **GPS time**: high when more than 7 days off, else medium. Suggested.
- **The file name's date** (a full timestamp: high past 7 days; a date-only
  name: medium, over its whole day). A messaging app's name (`-WA0001`, a
  Telegram export's `@DD-MM-YYYY_…`, anything under `Messaging/`) is when it
  arrived, so only a date *after* it counts. Suggested (a full timestamp).
- **A cloud export's date** (Takeout, iCloud, Telegram). Suggested.
- **The camera's own date**, when the sidecar holds another date that no
  correction explains (not plan's GPS, `--clock-offset` or export date, not a
  fix): medium.
- **The iPhone export's `DCIM/YYYYMM__` folder**, more than a month off:
  medium.
- **The file's modification time**, when the date is more than two days
  after it (taken after its last change?): low, and never for a corrected
  date (the wrong clock wrote that time too).

### Copies with other dates (`duplicate`)

The same content (sha256 in `index.json`) in the library twice, with dates
more than two days apart. Medium, no suggestion: one of them is wrong.

### GPS at 0, 0 (`null-island`) and place (`place`)

Not dates, listed by `check-dates` (not in the gallery's view):

- the file's own GPS or its sidecar's is exactly 0, 0 (within about 11 m): a
  receiver that had no fix. High.
- the sidecar's GPS (where `tag` took its `Place|` tag from, possibly a cloud
  export's place) is more than 50 km from the file's own GPS. Medium.

## Fixing: fix-dates and Fix date

Nothing is fixed by itself. Name the files and how:

| Option | New date |
|---|---|
| `--set YYYY-MM-DD[THH:MM[:SS]][+HH:MM]` | that date; a date alone keeps each file's time of day; an offset is written with it |
| `--shift ±N(y\|d\|h\|m)` | the date in use moved by that much: `+8y`, `-3h30m`, `+1y2d` (m: minutes; a year keeps the month and day, 29 February goes to the 28th) |
| `--suggested` | the last check-dates report's suggestion for each file (GPS first, then file name, export, neighbours, camera) |
| `--from-neighbours` | only the numbered neighbours' suggestion |
| `--like GOOD` | GOOD's offset from its own camera clock (its date in use minus its camera date: fix one clip by hand, then copy that to the rest of the card), applied to each file's own camera date |

- **A dry run until `--yes`**: it prints each file's old and new date and
  why, and every file it refuses (not a library file, not a photo or video,
  quarantined or flagged, no suggestion, an impossible date before 2000-01-02
  or in the future, already that date). Exit 1 when it refused everything.
- **Sidecars only**: the new date goes to `XMP-exif:DateTimeOriginal` in
  `<file>.<ext>.xmp` (made when missing); the photo or video is never written,
  so its sha256 (dedupe, erase, backup) stays. It runs under the library lock.
- **Recorded**: `<state>/date-fixes.json` (0600) keeps each write: an id, the
  time, `--reason`, and per file the old value (or none), the new one and the
  sidecar's sha256 after. The file's date findings leave the report.
- **`--undo [ID]`** (a dry run too, `--yes` to write) puts back exactly what
  one fix wrote, the last one by default: the old date, or none. It refuses,
  writing nothing, when a file is gone or any of its sidecars changed since
  (a later tag, Keep, digiKam). The findings it took out of the report come
  back if the report is still the same run.
- **Erase, forget, trash and sweep-erased clear both files**: every record of
  the files they delete goes from `datecheck.json` (their findings, and a
  numbered neighbour's or a copy's finding whose reason names them) and from
  `date-fixes.json` (their entries with the old and new dates, and the
  findings a fix set aside for them; a fix left with no file goes), with any
  recorded file gone from a library folder that is there. Both files are
  rewritten atomically, 0600, and erase's coverage report counts the records
  dropped. A file under a folder that is not there (an unplugged drive) is
  not gone. `verify-gone` fails, printing only a count ("DATE RECORDS: N
  entries ..."), while a record of a gone file remains.
- **`tag BATCH` keeps a fix**: a rerun writes the fixed date, not the plan's,
  for a file a fix covers (`datecheck.fixed_paths`). `tag-files` already
  keeps any sidecar date.

The gallery's **Dates to check** quick view ([gallery.md](gallery.md)) lists
the report's date findings grouped by reason, each tile with its suggested
date; select and **Fix date…** offers the suggestion, a date or a shift, runs
the same code, and its message has **Undo** (the same record: `fix-dates
--undo` can undo a gallery fix too).

## Limits

- **Files are not moved.** A fixed file stays in its old `YYYY/YYYY-MM`
  folder: nothing in media-intake moves a filed file when its date changes
  (`plan` picks the folder once, `apply` files it), so fix-dates doesn't
  either. The gallery, `find` and digiKam go by the sidecar date, so it shows
  in the right month there. Moving would also leave the backup drive's copy
  under the old path (`backup` never deletes) and needs `index.json`, the
  batches' plans and the backup record rekeyed; if it is wanted, it belongs
  in an explicit step of its own with a dry run, not in fix-dates.
- **The report is a snapshot**: the gallery shows the last run's findings; a
  file changed since (by another tool) is shown as it was found. Run
  `check-dates` again after a batch of fixes.
- **Without exiftool** check-dates and fix-dates stop (`need_exiftool`).
- **A file with no sidecar date** shows in the gallery by its folder or file
  time (gallery.md, "What it shows"), while check-dates and fix-dates use its
  own EXIF / QuickTime date; the Fix date dialog shows check-dates' date.
- **The model table is small** (four families) and grows by hand, with a
  source per row; unknown models are skipped, never guessed.
- **Sequence** needs a device id, and phone counters wrap at 9999: a group
  that wrapped is skipped rather than compared across cycles.
- **Place** compares coordinates, not place names (no geocoding here).
