# Drive queue: an external drive of old photos and videos

For a separate external drive (not your backup drive) full of photos and
videos: tag them, classify them, set junk aside, then file the rest in the
library, in chunks, unattended. Tool: `media_intake/drive_queue.py`, run as
`media-intake queue`.

It is named `drive_queue.py`, not `queue.py`: `media_intake/` goes on
`sys.path` in intake.py, semantic.py and vlm_trash.py, and a `queue.py` there
would shadow Python's standard `queue` module (`concurrent.futures` and
`multiprocessing` import it).

## Commands

```
media-intake queue add "/run/media/$USER/<drive>/<folder>" --label <name>
media-intake queue run --inhibit            # overnight; --chunk 2000 is the default
media-intake queue status
media-intake queue offload <name> --yes     # deletes the drive originals: only when you mean it
```

## What `add` checks

- **The source root is exactly the folder you name**, walked recursively
  (`sources.py`, kind `drive`): media extensions only; hidden files and
  folders, OS folders (`$RECYCLE.BIN`, `System Volume Information`,
  `.Trashes`, `.Spotlight-V100`, `.fseventsd`, `lost+found`,
  `.DocumentRevisions-V100`, and a few more), symlinks and password-manager or
  recovery-code names are skipped. The whole drive only if you name the mount
  itself.
- **Refused on the laptop's own disk** (`findmnt -T`).
- **Refused on a drive that looks like your backup drive**: a
  `.media-intake-backup` file at its mount root, or any folder listed in the
  config's `[drive] backup_markers` there (the folders your backup writes;
  [configuration.md](configuration.md)). If a non-backup drive happens to have
  one of those folders, create an empty `<mount>/.media-intake-source-ok` to
  override.
- **Refused without a filesystem UUID** (`findmnt -no UUID -T`): offload
  compares it.
- **Free space**: it counts files and bytes first and refuses if the laptop
  would have under 50 GB free once all of it is copied in; queue a subfolder
  at a time instead (run and offload each before adding the next). The run
  checks again before staging each chunk.
- Overlapping sources on the same drive and duplicate labels are refused.

## What `run` does

Each source is split into chunks (`--chunk`, fixed at the source's first
run); each chunk is an ordinary intake batch `<added date>-<label>-<n>`:

stage (`media-intake stage --kind drive --only <chunk list>`; the manifest
records the drive's UUID and mount) -> plan -> scan -> apply -> tag ->
semantic index -> `vlm_trash.py` on the batch's `Review|maybe-trash` files.
Steps run one at a time. The VLM step is skipped with a note when Ollama or
the pinned model is not available, or the venv is missing; the candidates wait
for a later `media-intake vlm-trash` run ([search.md](search.md)).

Drive files are categorised by name: `Screenshot_*`, `Screen Shot *` ->
Screenshots; `IMG-YYYYMMDD-WA*` / `VID-YYYYMMDD-WA*` -> Messaging/WhatsApp
(no config needed); else photo or video by extension. No `--event` routing:
drive files file by date.

**Wrapper.** plan, scan, tag, index and the VLM run behind the config's
`[queue] wrapper`, a command prefix such as a memory-capping or locking
wrapper (default none; `MEDIA_QUEUE_WRAPPER`, space-separated, overrides). If
the wrapper takes a lock, set `[queue] wrapper_lock` to its lock file and
`run` says when another job holds it. Never start the queue itself under a
locking wrapper: its inner calls would wait on the same lock forever.

**State** is `<state>/queue.json` (0600) plus `<state>/queue/<label>/` (the
frozen file list, chunk lists, one log per batch; step output goes to the
log, never the terminal). A step is marked done only when it exits 0, so a
killed run picks up at the step it was in, and a finished step is never run
again. A failed step stops the run (`status` shows the error and the log);
`run` again resumes there. It stops if the drive is unplugged or a different
drive sits at that path. `--inhibit` holds `systemd-inhibit
--what=sleep:idle` for the whole run. Each chunk ends with a counts-only
summary.

**It never makes your decisions**: quarantine review, trash suggestions and
offload all wait. `status` lists, per finished chunk, the quarantine,
near-copy, review-folder and trash-suggestion counts with the commands to
open them (`media-intake review <batch>`, the quarantine `xdg-open` line,
`media-intake gallery` or `media-intake suggest-trash --batch <batch>`).

To run it overnight in the background, on AC power:

```
nohup media-intake queue run --inhibit > ~/.local/share/media-intake/queue-run.log 2>&1 &
```

## Offload

`offload <name> --yes` runs `media-intake offload <batch> --phone <root>
--yes` for each finished, not yet offloaded chunk. Refused when any of those
batches still has files in `quarantine/<batch>` or any level of
`quarantine/near-<batch>` (`near-near-<batch>`, ...), when the drive's UUID differs from the one queued, or when the drive now looks
like the backup. `media-intake offload` itself checks the UUID and the backup
markers again for a drive batch. It deletes only originals whose copy is filed
in the library (or that `plan` dropped as exact duplicates of library files
or of something trashed before); files in quarantine, the review folder, or
trashed by you from this batch stay on the drive. Without `--yes` it
prints what it would do.

## Tests

`python3 -I -m unittest discover -s tests -p test_drive_queue.py` (fake drive, fake backup
markers, simulated kills, chunking, free-space guard, UUID mismatch) and
`-p test_sources.py`.
