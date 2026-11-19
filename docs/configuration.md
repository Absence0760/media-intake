# Configuration

media-intake reads one TOML file: `$MEDIA_INTAKE_CONFIG`, else
`$XDG_CONFIG_HOME/media-intake/config.toml`, else
`~/.config/media-intake/config.toml`. A missing file means every default. An
unknown key or a bad value stops every command with a message (nothing runs
on a half-understood config). `media-intake doctor` prints the file it read and
the folders it resolved. A commented example: [`config.example.toml`](../config.example.toml).

Paths take `~` and `$VARS`.

| Key | Default | What it is |
| --- | --- | --- |
| `library` | `~/Pictures/library` | Photos, `Screenshots/`, `Messaging/<app>/` |
| `videos` | `~/Videos/personal` | Videos (GoPro included), `Screen recordings/` |
| `state` | `~/.local/share/media-intake` | Staging, quarantine, index, erase log, `jobs/` (each detached job's status, log and lock), `locks/` (the library lock), `device-salt` (the key of the device-id pseudonyms, `0600`), `event-names.json` (the gallery's event display names, per library by a hash of its folders, `0600`, written atomically; [gallery.md](gallery.md), Events), venv, models, GeoNames. Kept `0700` (`jobs/` and `locks/` too, their files `0600`); never sync or index it. Keep `device-salt` when you move or rebuild the state folder: a new salt gives every camera a new device id, so its next batches and folders no longer match the old ones. |
| `phone.extra_folders` | none | `{ "relative/folder" = "App" }`: extra exact folders on a phone that may be read, each filed under `Messaging/<App>/`. Everything else outside `DCIM/` and `Pictures/` stays unlisted. Entries for WhatsApp, Telegram and Signal: [intake.md](intake.md), "Messaging apps". |
| `drive.backup_markers` | `[]` | Folder names at a drive's mount root that mark your backup drive; `stage --kind drive` and `queue add` refuse it. A `.media-intake-backup` file at the root does the same; `.media-intake-source-ok` overrides both. |
| `queue.wrapper` | `[]` | Command prefix for the queue's heavy steps (plan, scan, tag, index, vlm). |
| `queue.wrapper_lock` | none | That wrapper's lock file, so `queue run` can say when it will wait. |
| `projects.<key>` | none | See below. |
| `plugins.paths`, `plugins.modules` | none | Plugin modules and where to import them from: [plugins.md](plugins.md). |
| `recognition.dir` | `<state>/recognition` | People and pets recognition data (detections, vectors, names, crops; biometric). Kept `0700`, its files `0600`; refused inside the library, videos or a project folder, never backed up: [recognition.md](recognition.md). |
| `recognition.pet_classes` | `["cat", "dog"]` | The animals `media-intake pets` looks for, from the detector's COCO animals: `bird`, `cat`, `dog`, `horse`, `sheep`, `cow`, `elephant`, `bear`, `zebra`, `giraffe`. A cat is never grouped with a dog. |
| `recognition.pet_min_frames` | `3` | A video gets `Pets\|<Name>` only when that pet is in at least this many of its sampled frames (1 to 100). Photos need one. |

`state`, `library`, `videos` and every project's `folder` (as resolved, env
vars included) are never accepted as a laptop folder source, nor any folder
inside or containing one: `stage --kind folder` and `queue add --kind folder`
refuse them ([queue.md](queue.md), "Laptop folders").

Environment variables win over the file: `MEDIA_INTAKE_STATE`, `PHOTO_LIBRARY`,
`PERSONAL_FOOTAGE`, and `MEDIA_QUEUE_WRAPPER` (space-separated) for
`queue.wrapper`. `MEDIA_INTAKE_LOCK_WAIT` (seconds, default 10, `0` to refuse
at once, at most 86400) is how long a CLI command waits for the library lock
before it exits 75 ([intake.md](intake.md), "One writer at a time").
`MEDIA_INTAKE_PROGRESS_EVERY` (files, default 10000) and
`MEDIA_INTAKE_PROGRESS_SECS` (seconds, default 2) set how often the long
sweeps in erase, forget and verify-gone print a counts-only progress line;
the same two set it for `media-intake index` and `vlm-trash` (defaults there
100 files and 10 s, 1 s on a terminal: [search.md](search.md)).
`MEDIA_INTAKE_NEXT_HINT` sets the `next:` line that ends each main-flow step:
`1` always prints it, `0` never does, and unset it prints only at a terminal
([intake.md](intake.md), "Progress and the `next:` line").
`MEDIA_INTAKE_NO_GVFS_METADATA=1` stops erase clearing GNOME's per-file
metadata through `gio`, and `MEDIA_INTAKE_NO_LOCALSEARCH=1` stops it resetting
and checking the desktop search index; the tests set both, and the erase
coverage report then lists those places as not checked ([intake.md](intake.md),
"Erasing, and what it can't reach").

`MEDIA_INTAKE_TEST_MOUNT` (and `MEDIA_INTAKE_TEST_UUID`) are for tests only:
they stand in for `findmnt`'s mount point (and filesystem UUID) and turn off
the laptop-disk check in `stage --kind drive`, `queue add` and `backup`. Never
set them for a real run.

`MEDIA_INTAKE_LIBRARY_LOCK_FD` is internal, not a setting: a detached job's
worker sets it for the steps it starts, so they share the library lock it
already holds. Don't set it yourself: a step accepts it only when that
descriptor is open on this library's lock file and an flock on it succeeds,
so a made-up value never skips the lock (library_lock.py, "Inherited, not
re-taken").

## Projects and events

Every `plan` takes `--event SLUG@YYYY-MM-DD` (repeatable). On its own that
tags the day's files `Event|SLUG`; they are still filed by date. A project
routes an event's camera files (photos, videos, GoPro) to its own folder:

```toml
[projects.club]
name = "Run Club"            # the Project| tag; default: the key
folder = "~/Videos/run-club" # files go to <folder>/<date>-<slug>/{phone,gopro}/
events = ["*-race"]          # fnmatch patterns routed here without a prefix
protect = true               # default: never a trash suggestion or a VLM trash verdict
```

- `--event club/park-race@2026-05-01` routes to `club` explicitly.
- `--event park-race@2026-05-01` routes to the first project whose `events`
  pattern matches, else it is tags only.
- Screenshots and messaging media of that day are never routed; they go to
  the library as usual.
- `tag`, `tag-files` and `find --project` read the project from the folder a
  file sits in, so files filed under a project folder keep `Project|<name>`.
- The name `Personal` is reserved for everything else.

## Calibrating label thresholds

The thresholds in [`media_intake/labels.yaml`](../media_intake/labels.yaml)
(each content label, `child-in-shot`, `maybe-trash`) are first guesses.
`media-intake calibrate` measures them against your own marks, on this
machine: it reads stored numbers and sidecar tags only, opens no photo or
video, and writes nothing (not `labels.yaml`, not this config, not a sidecar).
Changing a threshold stays your edit to `labels.yaml`.
`Calibrate|people|<Name>` and `Calibrate|pets|<Name>` tags are not labels:
`media-intake people eval` and `pets eval` read them
([recognition.md](recognition.md)) and `calibrate` ignores them.

1. **Build or update the search index**: `media-intake index`. Without it,
   calibrate stops with `calibrate: no search index yet. Build it first:
   media-intake index` (exit 1). `tag` keeps
   only the labels over each threshold, so calibrate works each label's score
   out again from the index's vectors (`<state>/embeddings.npz`) and
   `labels.npz`, with `label.py`'s formula. Photos only by default: a video's
   index vector is a mean of 8 frames where `tag` takes the best of up to 30,
   so `--with-videos` counts them as an approximation.
2. **Mark a sample in the gallery** with **+ tag** (on one item or a whole
   selection): `Calibrate|<label>|yes` when the item shows the label,
   `Calibrate|<label>|no` when it doesn't, with the label's name as in
   `labels.yaml` (case doesn't matter), e.g. `Calibrate|dog|yes`,
   `Calibrate|child-in-shot|no`. Mark items whatever their current tags say,
   including ones the label missed, or recall means nothing. Aim for a few
   hundred items, with at least 20 yes and 20 no per label you want a
   suggestion for.
3. **`maybe-trash` needs fewer marks**: your trash decisions count already.
   A file you kept (Keep: `Owner|keep` / `Owner|not-trash`, digiKam
   Accepted, or a favorite: `xmp:Rating` >= 5) is a "no"; `Owner|trash` on a copy, or its sha256 in
   `<state>/trashed.json`, is a "yes". Trash deletes the file, so a trashed
   item counts only until the next `media-intake index` drops its vector;
   run `calibrate` before re-indexing. A `Calibrate|maybe-trash|...` tag wins
   over these, and a Keep wins over a trashed copy.
4. **Run `media-intake calibrate`.** For each label with marks: the yes and
   no counts, then precision (of the items the label flags, how many you said
   yes to), recall (of your yes items, how many it flags) and F1 (one number
   for both) at the current threshold, and a suggested threshold: the one with
   a balance of both, or with
   `--precision 0.9` the one that flags the most of your yes items while at
   least 90% of what it flags is a yes (sensible for `maybe-trash`, where a
   false flag costs you a look). The suggestion sits halfway between two
   marked scores. Under `--min` (default 20) yes or no marks it says so
   instead of suggesting anything. Under the table, a short legend says what
   each column means in plain words; with no marks yet, calibrate says how to
   add them instead of printing an empty table. `--json` prints the same as
   one object.

   ```
   calibrate: 50 photos have a stored score; you marked 49 of them
   suggested cut-off: the best balance of precision and recall (best F1)
     (only for a label with at least 20 yes and 20 no marks)
   label          pos   neg   thresh  prec recall    F1  | suggest  prec recall    F1
   dog             20    22   0.0500 0.909  1.000 0.952  |  0.3994 1.000  1.000 1.000
   maybe-trash      3     3   0.1000 0.667  0.667 0.667  | too few marked items: 3 positive, 3 negative; need at least 20 of each
   what the columns mean:
     pos / neg  items you marked yes / no for that label
     thresh     the cut-off in labels.yaml now: a score at or above it adds the label
     prec       precision: of the items it flagged, how many you agree with
     recall     of the items you marked yes, how many it flagged
     F1         one number for both (1.000 is perfect, higher is better)
     suggest    a better cut-off, with the prec, recall and F1 it would give
   this command changes nothing: to use a suggestion, edit the threshold in labels.yaml yourself
   ```

Output is counts and numbers, never file names. Copies of one file pool their
tags; an item marked both yes and no for a label is reported as a conflict
and left out; flagged, quarantined or erased content is never counted.
`maybe-trash` also comes from a pixel check (very blurry or near-black
photos) that has no threshold here; only the SigLIP score is calibrated. A
small or one-sided sample (only the items a label already flagged) gives
numbers that look better than they are.
