# media-intake

Local-only intake for your photos and videos: copy them off a phone, camera or
old drive into one organised library, drop exact and near duplicates, put
anything sensitive (IDs, payment cards, 2FA codes, nudity) in quarantine
for you to decide on, tag the rest, and search and browse it, all on
your own machine. When you erase something, it is erased everywhere the tool
can reach: the library, the phone, the backup drive, thumbnails and caches.

Fedora Linux first. Python, no service, no account.

New here? [docs/getting-started.md](docs/getting-started.md) walks through a
first run on a folder of test photos, from install to the gallery.
[docs/words.md](docs/words.md) says what each word means (batch, source,
backup drive, quarantine, review folder, Keep, Trash, Erase, ...).

## Privacy guarantees

- **Everything runs locally.** No cloud API, no telemetry, no remote model.
  The only network traffic is `setup.sh` downloading pinned files once
  (Python wheels, the SigLIP model, GeoNames, and with `--recognition` the
  YuNet and SFace face models and the YOLOX and DINOv2 pet models), each
  checked by sha256, and
  Ollama on `127.0.0.1`: the optional vision-model check and `media-intake
  doctor`'s digest check. `scripts/update-pins.py` is a maintainer tool, not
  part of the tool you run: it reads metadata (never model weights) from
  PyPI, Hugging Face and the Ollama registry to bump the pins.
- **Every detector is local**: NudeNet (nudity), Tesseract (text on IDs and
  cards), zxing-cpp (QR and barcodes), SigLIP (labels and search), an optional
  Qwen3-VL model through your own Ollama (trash second opinion), and, off
  until you install them, YuNet and SFace (finding and grouping faces) and
  YOLOX and DINOv2 (finding and telling apart your pets).
- **No AI service sees your images.** If you drive the tool with a coding
  assistant, it runs the commands and sees counts and paths; the tool never
  prints image content, and the docs and `CLAUDE.md` forbid an assistant from
  opening quarantined or flagged files.
- **Originals are never modified.** Tags live in XMP sidecars
  (`<file>.<ext>.xmp`, digiKam's naming), so sha256 stays stable for dedupe
  and erase.
- **Nothing is deleted from a device or drive without your explicit `--yes`**
  (a laptop folder source is never offloaded: you delete it yourself),
  and only files with a verified copy in the library. A device delete is
  refused when its path differs from another path on the device only by
  letter case (case-insensitive storage may hold them as one file; checked
  against the device's live listing, and refused if it can't be listed), and
  nothing on a device is ever renamed or moved. Erasing a GoPro chapter
  from the card also removes its `.LRV`/`.THM` previews (unstaged copies of
  the same footage), behind the same `--yes` and path checks.
- The state folder (staging, quarantine, index, erase log, `backup/`: the
  backup records, `jobs/`: each detached job's status, log and lock, `locks/`:
  the library lock, and `device-salt`) is `0700`. So are `backup/`, `jobs/`
  and `locks/`, whose files are `0600`; the lock's holder note names the
  command and its PID, never a file name. `device-salt` is `0600`, and so is
  `event-names.json` (the gallery's event display names, never in a sidecar),
  `places.json` (your named places and their points: sensitive, never backed up
  or printed; [docs/places.md](docs/places.md)), and `datecheck.json` and
  `date-fixes.json` (check-dates' last report and every fix-dates write, with
  paths and dates; erase, forget, trash and sweep-erased drop their files'
  records; [docs/dates.md](docs/dates.md)). `records-handoff.json` (`0600`)
  lists photos of documents handed to your records inbox and held until
  `to-records --finish`: names, checksums and times, never content; held
  quarantined ones wait in `quarantine/records-held/`, and `--finish` erases
  only with `--yes` and proof the records backup holds them, never the inbox
  copy ([docs/records.md](docs/records.md)).
  Face and pet detections, vectors, names and crops (biometric) live in
  `recognition.dir` (default `<state>/recognition`, `0700`, files `0600`),
  are never backed up, and go with every `erase`, `forget`, `trash` and
  `sweep-erased` ([docs/recognition.md](docs/recognition.md)).
- **Raw camera serial numbers are never written anywhere** (folder names,
  tags, plans, logs). A device id is a salted HMAC pseudonym of make, model
  and serial, keyed by `device-salt`.
- The review page and gallery listen on `127.0.0.1` only, behind a random
  token. The gallery can launch the desktop's default app (`xdg-open`, an
  argument list, no shell) on one library file, for media the browser can't
  play, and only from its "Open in the system player" button's POST (token,
  CSRF and Origin checked); a quarantined or flagged file is refused. After
  a Share it also opens the new share-safe folder it made in the file
  manager: the page names the job, never a path, and the folder must still
  sit directly inside the share-safe folder.
- The gallery's H.264 copies of HEVC videos live in
  `~/.cache/media-intake/proxies` (`0700`, files `0600`); `erase`, `forget`
  and `trash` delete them and the record of a failed one, `verify-gone`
  reports any left, and quarantine and
  review files never get one.
- `erase` and `forget` also drop the erased files' entries from the backup
  records in the state folder (`backup/`, which name library files), so no
  name outlives the file there; a record they can't read or rewrite is
  counted in the coverage report as left.
- Every erase ends with a coverage report (counts and place names, never a
  file name) of what it reached, including GNOME's per-file metadata and the
  desktop search index, and what nothing on the machine can reach (cloud
  services, chat apps and recipients, a device's app-private storage,
  backups, and a cloud export's Takeout JSON or iCloud CSV row (or a Telegram
  export's `result.json`) left beside an erased file on its source drive). It is kept in the erase log;
  `verify-gone BATCH` shows it.

## Install (Fedora)

```sh
sudo dnf install perl-Image-ExifTool tesseract tesseract-langpack-eng python3-zxing-cpp \
  python3-pillow python3-numpy python3-pyyaml ffmpeg-free gstreamer1-plugins-bad-free gvfs-mtp
git clone https://github.com/jaredhoward-dev/media-intake.git
cd media-intake
bash setup.sh            # venv, SigLIP vision model, GeoNames, ~/.local/bin/media-intake
media-intake doctor      # what's installed, checksums, and the dnf line for what's missing
```

`doctor` groups its checks under headings (config and folders, system tools,
Python modules, optional extras, venv/models/data, Ollama). Every line starts
with its status (`ok`, `MISSING`, `BAD`, `optional`, `absent`), so `media-intake
doctor | grep -E '^(MISSING|BAD)'` lists the problems. It ends with a count
and, when something is missing, one `sudo dnf install -y ...` line for the
required packages and a separate one for the optional ones. It exits 1 only
when something required is missing or a pinned file is wrong. Colour is used
only on a terminal and never when `NO_COLOR` is set.

`setup.sh` prints a `==> [n/N]` heading per step and ends with a summary of
what it installed or found already present, and where, how to find a phone
(unlock it, choose "File transfer", then `ls /run/user/$UID/gvfs/`), and a
`next:` line: the first import, or fixing what `doctor` (its last step) found.
When `~/.local/bin` isn't on your `PATH` it says so and prints the `export
PATH=...` line to add. If a step fails it names the step and the likely fix:
the exact `sudo dnf install -y ...` line for a missing package, no network, or
a download that didn't match its sha256 pin (that file is deleted and nothing
is installed from it; re-run, and never skip the check). Colour only on a
terminal, and never with `NO_COLOR` set.
It is safe to re-run (it only re-verifies checksums). Options:
`--build-labels` (text model for semantic search, and to rebuild
`labels.npz` after editing `labels.yaml` or moving the SigLIP pin), `--refresh-geo` (accept a new
GeoNames snapshot), `--no-launcher`, `--recognition` (the face and pet models for
`media-intake people` and `pets`, off without it: [docs/recognition.md](docs/recognition.md)). For HEVC video (most phones) Fedora's
`ffmpeg-free` has no decoder; the tool uses GStreamer's VA-API (`vah265dec`)
or Vulkan decoder when one is installed, also to make the H.264 copy the
gallery plays HEVC from (with an H.264 encoder: `vah264enc` on the GPU, else
`x264enc` or `openh264enc`, which `doctor` reports as optional; the player starts
within seconds while it converts, and `media-intake proxies` makes them all ahead
of time; [docs/gallery.md](docs/gallery.md), "Video proxies"). The optional trash second opinion
needs [Ollama](https://ollama.com/) and `ollama pull qwen3-vl:2b`. Every model
pin (SigLIP file sha256s, the Ollama model digest) lives in
`media_intake/pins.json`; `doctor` checks the files on disk and the local
Ollama (on `127.0.0.1` only) against it.

Configuration is optional: copy [`config.example.toml`](config.example.toml) to
`~/.config/media-intake/config.toml` to change folders, allow a messaging
app's folder on the phone, or route events to project folders
([docs/configuration.md](docs/configuration.md)).

## Commands

A phone import, start to finish:

```sh
media-intake stage /run/user/$UID/gvfs/mtp:host=.../Internal\ shared\ storage --kind phone --batch phone-1
                                   # --batch is optional; default <device id>-<YYYYMMDD>
media-intake plan phone-1 [--event park-race@2026-05-01] [--clock-offset DEVICE=±Nh]
media-intake scan phone-1          # local detectors; flags private files
media-intake apply phone-1         # file into the library; flagged files to quarantine
media-intake review phone-1        # local page: Keep false positives, Erase the rest
media-intake tag phone-1           # XMP tags: project, source, event, place, labels
media-intake offload phone-1 --phone <phone folder>   # after a backup: free the phone (asks you to type DELETE)
```

| Command | What |
| --- | --- |
| `stage`, `plan`, `scan`, `apply` | Copy in, plan (dates, dedupe, folders), detect, file |
| `quarantine`, `release`, `review` | Decide on what the detectors flagged (quarantine) and the review folder |
| `erase`, `forget`, `verify-gone`, `sweep-erased`, `wipe-free` | Erase everywhere, check, catch copies on a backup drive that was unplugged at erase time, overwrite the backup drive's free space |
| `to-records` | Hand photos of documents (IDs, passports, payment cards, receipts) to your records tool's inbox, holding the originals until `--finish` erases them once your records backup holds the copies; never nudity, 2FA codes or secrets ([docs/records.md](docs/records.md)) |
| `trash`, `suggest-trash`, `not-trash`, `purge-review` | Trash junk, find Suggested junk, Keep a suggestion, empty a review folder (`trash` and `purge-review` delete only with `--yes`; without it, a dry run) |
| `tag`, `tag-files`, `mark-used`, `find` | Sidecar tags and searching them |
| `places list/add/rename/remove/suggest/tag` | My places: your own named places (Home, ...) as `Place\|<name>` tags beside the city; kept only in the state folder ([docs/places.md](docs/places.md)) |
| `check-dates`, `fix-dates` | Find library files whose date looks wrong (clock-reset defaults, a camera newer than its date, numbering out of order, disagreeing dates; changes nothing), and correct them in their sidecars (a dry run until `--yes`; `--undo`) ([docs/dates.md](docs/dates.md)) |
| `migrate-tags` | One-shot: rewrite the legacy `Review\|not-trash` tag as `Owner\|not-trash` |
| `index`, `search`, `similar` | Local semantic search (SigLIP) |
| `vlm-trash` | Local vision-model second opinion on Suggested junk |
| `people` | Recognise people in photos, offline: you name them, it suggests and tags `People\|<Name>` once you confirm (off until `setup.sh --recognition`; [docs/recognition.md](docs/recognition.md)) |
| `pets` | The same for your own pets (cats and dogs by default), tagged `Pets\|<Name>` (off until `setup.sh --recognition`) |
| `calibrate` | Report precision and recall of each label's threshold against your own marks; changes nothing |
| `gallery` | Local web gallery: browse, Keep, Favorite, Trash, Erase, tag, rotate, events, bulk edits, share-safe copies, naming people and pets (once recognition is on) |
| `proxies` | Make the gallery's H.264 copies of HEVC videos now, so they play at once (`--dry-run`, `--inhibit`, `--retry-failed`) |
| `prune-cache` | Delete gallery thumbnails and video proxies nothing in the library shows any more (`--dry-run` first) |
| `backup` | Copy the library (photos, videos, sidecars) to the backup drive: incremental, never deletes there |
| `queue add/run/status/offload` | Import a source drive or a big laptop folder in resumable chunks |
| `share-copy` | Copy files with GPS and metadata stripped, for posting |
| `doctor` | Check the install |

`media-intake <command> --help` says what a command does, what it touches,
whether it deletes, and its options; `media-intake` alone (or `--help`) lists
the commands by task, and a mistyped command exits 2 with the nearest real
one. Details:
[getting started](docs/getting-started.md), [words used](docs/words.md), [intake](docs/intake.md), [wrong dates](docs/dates.md), [review page](docs/review-ui.md),
[gallery](docs/gallery.md), [my places](docs/places.md), [search and VLM](docs/search.md), [recognition](docs/recognition.md),
[drive and folder queue](docs/queue.md), [backup](docs/backup.md), [records inbox](docs/records.md), [configuration](docs/configuration.md),
[plugins](docs/plugins.md), [releases](docs/release.md).

## Tested devices

- Android phones over MTP (GNOME's gvfs mount)
- iPhone over gphoto2 (gvfs), photos and videos under `DCIM/`
- GoPro (SD card or USB mass storage, `DCIM/1xxGOPRO`)
- External drives and laptop folders, through the queue (`--kind folder`)

## Limits

Detectors are hints, never guarantees: NudeNet flags exposed regions only,
OCR misses embossed card numbers, and the zero-shot "child in shot" label
misses small or distant children. "Not checked" is never "clean": a file a
detector can't read (an HEVC video without a decoder, a broken or audio-only
video, a motion photo whose embedded video can't be located or copied out, a type
no detector reads) or that crashes a check is flagged and
quarantined for you to look at, never filed as clean. Read
[ROADMAP.md](ROADMAP.md), "Known limitations", before relying on the tool.

## Roadmap

What's next and what's rough: [ROADMAP.md](ROADMAP.md).

## Development

```sh
python3 -I -m unittest discover -s tests      # synthetic media in a throwaway HOME
```

Tests that need the venv, the models or Ollama skip themselves when those are
missing. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Licence

Copyright (C) 2026 the media-intake contributors.

GNU Affero General Public License v3.0 only ([LICENSE](LICENSE)); it matches
NudeNet, which the scanner imports. Third-party components and the GeoNames
credit: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
