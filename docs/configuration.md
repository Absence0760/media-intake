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
| `state` | `~/.local/share/media-intake` | Staging, quarantine, index, erase log, `jobs/` (each detached job's status, log and lock), `locks/` (the library lock), `device-salt` (the key of the device-id pseudonyms, `0600`), venv, models, GeoNames. Kept `0700` (`jobs/` and `locks/` too, their files `0600`); never sync or index it. Keep `device-salt` when you move or rebuild the state folder: a new salt gives every camera a new device id, so its next batches and folders no longer match the old ones. |
| `phone.extra_folders` | none | `{ "relative/folder" = "App" }`: extra exact folders on a phone that may be read, each filed under `Messaging/<App>/`. Everything else outside `DCIM/` and `Pictures/` stays unlisted. |
| `drive.backup_markers` | `[]` | Folder names at a drive's mount root that mark your backup drive; `stage --kind drive` and `queue add` refuse it. A `.media-intake-backup` file at the root does the same; `.media-intake-source-ok` overrides both. |
| `queue.wrapper` | `[]` | Command prefix for the queue's heavy steps (plan, scan, tag, index, vlm). |
| `queue.wrapper_lock` | none | That wrapper's lock file, so `queue run` can say when it will wait. |
| `projects.<key>` | none | See below. |
| `plugins.paths`, `plugins.modules` | none | Plugin modules and where to import them from: [plugins.md](plugins.md). |

Environment variables win over the file: `MEDIA_INTAKE_STATE`, `PHOTO_LIBRARY`,
`PERSONAL_FOOTAGE`, and `MEDIA_QUEUE_WRAPPER` (space-separated) for
`queue.wrapper`. `MEDIA_INTAKE_LOCK_WAIT` (seconds, default 10, `0` to refuse
at once, at most 86400) is how long a CLI command waits for the library lock
before it exits 75 ([intake.md](intake.md), "One writer at a time").

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
