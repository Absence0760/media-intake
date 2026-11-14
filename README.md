# media-intake

Local-only intake for your photos and videos: copy them off a phone, camera or
old drive into one organised library, drop exact and near duplicates, put
anything sensitive (IDs, payment cards, 2FA codes, nudity) in a private
quarantine for you to review, tag the rest, and search and browse it, all on
your own machine. When you erase something, it is erased everywhere the tool
can reach: the library, the phone, the backup drive, thumbnails and caches.

Fedora Linux first. Python, no service, no account.

## Privacy guarantees

- **Everything runs locally.** No cloud API, no telemetry, no remote model.
  The only network traffic is `setup.sh` downloading pinned files once
  (Python wheels, the SigLIP model, GeoNames), each checked by sha256, and
  Ollama on `127.0.0.1`: the optional vision-model check and `media-intake
  doctor`'s digest check. `scripts/update-pins.py` is a maintainer tool, not
  part of the tool you run: it reads metadata (never model weights) from
  PyPI, Hugging Face and the Ollama registry to bump the pins.
- **Every detector is local**: NudeNet (nudity), Tesseract (text on IDs and
  cards), zxing-cpp (QR and barcodes), SigLIP (labels and search), an optional
  Qwen3-VL model through your own Ollama (trash second opinion).
- **No AI service sees your images.** If you drive the tool with a coding
  assistant, it runs the commands and sees counts and paths; the tool never
  prints image content, and the docs and `CLAUDE.md` forbid an assistant from
  opening quarantined or flagged files.
- **Originals are never modified.** Tags live in XMP sidecars
  (`<file>.<ext>.xmp`, digiKam's naming), so sha256 stays stable for dedupe
  and erase.
- **Nothing is deleted from a device or drive without your explicit `--yes`**,
  and only files with a verified copy in the library. A device delete is
  refused when its path differs from another path on the device only by
  letter case (case-insensitive storage may hold them as one file), and
  nothing on a device is ever renamed or moved.
- The state folder (staging, quarantine, index, erase log, `jobs/`: each
  detached job's status, log and lock, `locks/`: the library lock, and
  `device-salt`) is `0700`. So are `jobs/` and `locks/`, whose files are
  `0600`; the lock's holder note names the command and its PID, never a file
  name. `device-salt` is `0600`.
- **Raw camera serial numbers are never written anywhere** (folder names,
  tags, plans, logs). A device id is a salted HMAC pseudonym of make, model
  and serial, keyed by `device-salt`.
- The review page and gallery listen on `127.0.0.1` only, behind a random
  token. The gallery can launch the desktop's default app (`xdg-open`, an
  argument list, no shell) on one library file, for media the browser can't
  play, and only from its "Open in the system player" button's POST (token,
  CSRF and Origin checked); a quarantined or flagged file is refused.

## Install (Fedora)

```sh
sudo dnf install perl-Image-ExifTool tesseract tesseract-langpack-eng python3-zxing-cpp \
  python3-pillow python3-numpy python3-pyyaml ffmpeg-free gstreamer1-plugins-bad-free gvfs-mtp
git clone https://github.com/Absence0760/media-intake.git
cd media-intake
bash setup.sh            # venv, SigLIP vision model, GeoNames, ~/.local/bin/media-intake
media-intake doctor      # what's installed, checksums, and the dnf line for what's missing
```

`setup.sh` is safe to re-run (it only re-verifies checksums). Options:
`--build-labels` (text model for semantic search, and to rebuild
`labels.npz` after editing `labels.yaml` or moving the SigLIP pin), `--refresh-geo` (accept a new
GeoNames snapshot), `--no-launcher`. For HEVC video (most phones) Fedora's
`ffmpeg-free` has no decoder; the tool uses GStreamer's VA-API (`vah265dec`)
or Vulkan decoder when one is installed. The optional trash second opinion
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
media-intake scan phone-1          # local detectors; flags only
media-intake apply phone-1         # file, dedupe, quarantine
media-intake review phone-1        # local page: release false positives, erase the rest
media-intake tag phone-1           # XMP tags: project, source, event, place, labels
media-intake offload phone-1 --phone <device root> --yes   # after a backup: free the phone
```

| Command | What |
| --- | --- |
| `stage`, `plan`, `scan`, `apply` | Copy in, plan (dates, dedupe, folders), detect, file |
| `quarantine`, `release`, `review` | Review what the detectors flagged |
| `erase`, `forget`, `verify-gone`, `wipe-free` | Erase everywhere, check, overwrite free space on a drive |
| `trash`, `suggest-trash`, `not-trash`, `purge-review` | Plain deletes of junk, with suggestions |
| `tag`, `tag-files`, `mark-used`, `find` | Sidecar tags and searching them |
| `migrate-tags` | One-shot: rewrite the legacy `Review\|not-trash` tag as `Owner\|not-trash` |
| `index`, `search`, `similar` | Local semantic search (SigLIP) |
| `vlm-trash` | Local vision-model second opinion on trash candidates |
| `gallery` | Local web gallery: browse, keep, trash, erase, tag, rotate |
| `queue add/run/status/offload` | Process an old external drive or a laptop folder in resumable chunks |
| `share-copy` | Copy files with GPS and metadata stripped, for posting |
| `doctor` | Check the install |

`media-intake <command> --help` for options. Details:
[intake](docs/intake.md), [review page](docs/review-ui.md),
[gallery](docs/gallery.md), [search and VLM](docs/search.md),
[drive and folder queue](docs/queue.md), [configuration](docs/configuration.md),
[plugins](docs/plugins.md), [releases](docs/release.md).

## Tested devices

- Android phones over MTP (GNOME's gvfs mount)
- iPhone over gphoto2 (gvfs), photos and videos under `DCIM/`
- GoPro (SD card or USB mass storage, `DCIM/1xxGOPRO`)
- External drives and laptop folders, through the queue (`--kind folder`)

## Limits

Detectors are hints, never guarantees: NudeNet flags exposed regions only,
OCR misses embossed card numbers, and the zero-shot "child in shot" label
misses small or distant children. Read [ROADMAP.md](ROADMAP.md), "Known
limitations", before relying on the tool.

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
