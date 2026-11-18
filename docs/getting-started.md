# Getting started

A first-run guide: install, try the tool on a folder of photos you don't care
about, then the everyday commands. Each step links to the doc with the detail.

## What it does, and what it never does

media-intake copies photos and videos off a phone, camera, drive or laptop
folder into one library sorted by date, drops duplicates, sets aside anything
that looks sensitive (IDs, payment cards, 2FA codes, nudity) for you to
review, tags the rest, and lets you browse and search it.

- **Local only.** Every detector and model runs on your machine; nothing
  leaves it. The only downloads are in `setup.sh`, each checked by sha256.
- **Originals are never written.** Tags go to `<file>.<ext>.xmp` sidecars.
- **Nothing is deleted from a device or drive without an explicit `--yes`**
  (or a typed `ERASE` / `DELETE`), and a laptop folder source is never
  offloaded: you delete it yourself.

The full list: [README, "Privacy guarantees"](../README.md#privacy-guarantees).

## 1. Install

Install the Fedora packages and run `setup.sh` as in the
[README, "Install (Fedora)"](../README.md#install-fedora). `setup.sh` builds a
venv, downloads the pinned SigLIP model and GeoNames data, and writes the
`media-intake` launcher to `~/.local/bin`. It is safe to re-run. It prints a
`==> [n/N]` heading per step, ends with a summary of what it installed or found
already present and `next: media-intake doctor`; if a step fails it prints
`==> FAILED at step [n/N] ...` with the likely fix (section 5).

Then check the install:

```sh
media-intake doctor
```

Its checks are grouped under headings, and its `== Summary ==` opens with a
count line, `N problems, M optional items not available` (`no problems` when
there are none). Each check line starts with a status:

| Status | Meaning |
| --- | --- |
| `ok` | Present (and, for a model file, its sha256 matches the pin). |
| `MISSING` | Required and not there. Doctor's `== Summary ==` ends with `Install the required packages:` and, on its own line, the `sudo dnf install -y ...` to copy; for the venv, models or GeoNames it says `(bash setup.sh)`. |
| `optional` | Not needed for a first run: the GStreamer HEVC decoder (most phone video), an H.264 encoder (the gallery's HEVC playback), the gvfs MTP backend (Android phones), `xdg-open`, the SigLIP text model (`bash setup.sh --build-labels`, for text search) and Ollama (`vlm-trash` only). The summary lists them under `Optional packages:` with their own `sudo dnf install -y ...` line. |
| `absent` | A configured folder (library, videos, state, a project) doesn't exist yet. Normal before the first import. |
| `BAD` | A config error, or a file whose sha256 differs from its pin. Fix it before going on. |

Doctor exits 1 while anything is `MISSING` or `BAD`.

## 2. Configure (optional)

No config file means every default: photos in `~/Pictures/library`, videos in
`~/Videos/personal`, the tool's private state in
`~/.local/share/media-intake`. To change where the library goes, copy
[`config.example.toml`](../config.example.toml) to
`~/.config/media-intake/config.toml` and set only these two keys:

```toml
library = "~/Pictures/library"   # photos, screenshots, messaging media
videos  = "~/Videos/personal"    # videos, screen recordings
```

An unknown key or bad value stops every command with a message, and
`media-intake doctor` prints the file it read and the folders it resolved.
Every other key (phone folders, backup-drive markers, projects, plugins):
[configuration.md](configuration.md).

## 3. A first run on test photos

Put a few dozen photos you don't mind losing in a folder of their own, for
example `~/Pictures/test-photos`. It must be under your home folder, not
hidden, not inside a code or git tree, and not the library folder itself or a
folder that contains it (so not `~/Pictures` with the default library);
[queue.md](queue.md), "Laptop folders", has the full list.

**Which path:** this guide runs the steps one at a time with `stage --kind
folder`, so you see each step's output in the terminal. The folder queue
(`media-intake queue add ... --kind folder`, then `queue run`) does the same
steps in chunks, unattended, with output in log files; use it for big
folders once you know what each step does (section 4).

```sh
media-intake stage ~/Pictures/test-photos --kind folder --batch first-test
media-intake plan first-test
media-intake scan first-test
media-intake apply first-test
media-intake review first-test
media-intake tag first-test
```

| Step | What it does | What you'll see |
| --- | --- | --- |
| `stage` | Copies the folder's media files into the private state folder; your folder is only read. | How many files were staged, and counts of anything skipped. |
| `plan` | Works out each file's date and library folder, and finds exact and near duplicates. Nothing moves yet. | A summary of what will be filed, dropped as an exact duplicate, or set aside for review. |
| `scan` | Runs the local detectors (nudity, text on IDs and cards, QR and barcodes). Flags only. | Progress and counts; never image content. |
| `apply` | Files the clean files in the library, drops exact duplicates, and moves flagged files to the private quarantine. | A summary of counts per outcome. |
| `review` | Starts a local review page for what `apply` set aside. | One `xdg-open 'http://127.0.0.1:<port>/?t=<token>'` line, or `nothing to review for first-test`. |
| `tag` | Writes XMP sidecar tags (source, date, place, content labels) for the filed files. | Counts. `--no-labels` skips the content labels if you want it faster. |

On the review page (open the printed link in a private browser window):
the **Quarantine** tab holds what the detectors flagged, with Keep (it was a
false positive) or Erase only; the **Review** tab holds near-duplicates and
junk, with Keep, Delete or Trash. Submit unlocks once every item is decided
and asks you to type `ERASE`. Closing the tab without submitting changes
nothing. Detail: [review-ui.md](review-ui.md).

At a terminal each step ends with one `next: media-intake <command> <batch>`
line naming what to run next: after `apply`, `review` if anything was set
aside, else `tag`; after `tag`, `backup` then `offload` for a phone or camera,
or `gallery` for a folder source. The long steps (`stage`, `plan`, `apply`,
`tag`) print counts-only progress lines, never a file name
([intake.md](intake.md), "Progress and the `next:` line").

Things worth knowing from the test run:

- `apply` refuses to run before `scan`, and refuses if a document detector
  (`tesseract`, `python3-zxing-cpp`) is missing, rather than filing files
  unchecked.
- A file a detector can't read (an HEVC video without a decoder, a broken
  file) is quarantined as "not checked", never filed as clean.
- **Erase reaches every copy in your home folder**, including the original
  still in `~/Pictures/test-photos`
  ([intake.md](intake.md), "Erasing, and what it can't reach").
- **Trash is remembered by checksum**: a later import of the same file is
  dropped as "trashed before". Use test photos that aren't copies of photos
  you mean to import later.
- The tool never offloads a folder source. Once you're happy with the result,
  delete `~/Pictures/test-photos` yourself.

The same steps for a phone or GoPro (with `--kind phone` / `--kind gopro`,
`--event` for a shoot day, and `offload` to free the device after a backup):
[README, "Commands"](../README.md#commands) and [intake.md](intake.md).

## 4. Everyday use

**Gallery.** Browse, keep, trash, erase, tag, rotate and group events:

```sh
media-intake gallery
```

It prints one `xdg-open 'http://127.0.0.1:<port>/?t=<token>'` line; open it in
a private browser window. It runs until Ctrl-C or the page's **Close**
button. Flagged and quarantined files are never shown there.
[gallery.md](gallery.md).

**Search.** Build the local index once, then again after each import:

```sh
media-intake index
media-intake search "a photo of a dog on a beach" -n 20
media-intake similar <library file> -n 20
media-intake find --event <slug>
```

`index` prints counts-only progress and a summary. At a terminal `search`
and `similar` print a numbered list with each match's score and date; before
the first `index` they print one line naming `media-intake index`. `search`
needs the text model (`bash setup.sh --build-labels`); without it only
`similar` works. `find` searches the sidecar tags and needs neither. The
gallery's search box uses the same index. [search.md](search.md).

**The queue, for a drive or a big folder.** Imports in resumable chunks,
overnight if you like, and stops before every decision that is yours:

```sh
media-intake queue add "/run/media/$USER/<drive>/<folder>" --label old-drive
media-intake queue add ~/Pictures/old-laptop --label old-laptop --kind folder
media-intake queue run --inhibit
media-intake queue status
```

`status` prints a table, one row per source (`SOURCE`, `STATE`, `FILES`,
`QUAR`, `REVIEW`, `CHANGED`), then `Waiting for you:` with each chunk that
has decisions left (and the commands to open them), each failed chunk with
its log and retry command, and drives ready to offload, then one `next:`
line. `--json` gives the same for scripts. A drive that looks like your
backup drive is refused. [queue.md](queue.md), "Status".

**Backup.** Copy the library, videos and sidecars to a mounted external drive;
incremental, and it never deletes anything on the drive:

```sh
media-intake backup /run/media/$USER/<label> --dry-run
media-intake backup /run/media/$USER/<label>
media-intake backup /run/media/$USER/<label> --verify
```

Use a LUKS-encrypted drive. [backup.md](backup.md).

## 5. If something goes wrong

| Situation | What to run or read |
| --- | --- |
| `setup.sh` stops with `==> FAILED at step [n/N]` | Read the hint under it (a `sudo dnf install -y ...` line, no network, or a checksum mismatch), fix that, and re-run `bash setup.sh`. Never skip a checksum: a file that fails its sha256 is deleted, not used. |
| A mistyped command | `media-intake` exits 2 naming the unknown command, with a `did you mean` suggestion when one is close. |
| `doctor` shows `MISSING` | Run the `sudo dnf install ...` line it prints, or `bash setup.sh` where it says so; then `media-intake doctor` again. |
| `doctor` shows `BAD` for a model file | `bash setup.sh` deletes the wrong file; run it again to fetch the pinned one. |
| Every command stops with a config error | Fix or remove the key it names ([configuration.md](configuration.md)). |
| `stage --kind folder` or `queue add` is refused | The folder breaks a source rule (outside HOME, hidden, a git tree, the library itself): [queue.md](queue.md), "Laptop folders". Under `/run/media` use `--kind drive`. |
| `scan` says `no venv` | `bash setup.sh`. |
| `apply` says `run scan first` or `document detectors missing` | Run `scan`, or install the packages it names and `scan` again. |
| Videos are quarantined as `unscanned-video` | No video decoder could read them (most phone video is HEVC): install the GStreamer HEVC decoder `doctor` lists as optional before your next import. A *photo* with this flag is a motion photo whose short video couldn't be found or read ([intake.md](intake.md), "Principles"); open it yourself and release it if it's fine. |
| `[gst] fallback skipped` on stderr | Your home folder's path has characters GStreamer can't take safely ([intake.md](intake.md), "Principles"). |
| A command waits, then exits 75 | Another command or job holds the library lock; it names the holder. Try again when it ends, or set `MEDIA_INTAKE_LOCK_WAIT` ([intake.md](intake.md), "One writer at a time"). |
| A review run shows `crashed` | Run `media-intake review <batch>` again; it shows what is left ([review-ui.md](review-ui.md), "The run is a detached job"). |
| A video won't play in the gallery | It may be HEVC: install an H.264 encoder (`doctor`'s optional line), or use **Open in the system player** ([gallery.md](gallery.md), "Video proxies"). |
| `search` says the text model is missing | `bash setup.sh --build-labels`. |
| The queue stopped | `media-intake queue status` shows the error and the log; `media-intake queue run` resumes at that step ([queue.md](queue.md), "What `run` does"). |
| `backup` says `is the drive mounted?` | Mount the drive and pass its mount point ([backup.md](backup.md), "Rules"). |

Detectors are hints, not guarantees: read [ROADMAP.md](../ROADMAP.md),
"Known limitations", before relying on the tool.
