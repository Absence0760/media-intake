---
name: media-folders
description: Import one or more folders of photos and videos, on the laptop or on an external (non-backup) drive, through the media-intake queue so they are deduped, scanned for sensitive content, filed, tagged and classified like a phone batch. Use when the user says "import this folder", "add these folders", "queue the drive at <path>", "run the queue overnight", "what's waiting in the queue", or hands over a folder path full of pictures.
---

# /media-folders

Tool: `media-intake queue` (`media_intake/drive_queue.py`). Detail and guards:
`docs/queue.md`. Each chunk of a source is an ordinary intake batch, so the
review page, quarantine, erase and gallery in `docs/intake.md` all apply to it.
Never start the queue itself under the config's `[queue] wrapper` if that
wrapper takes a lock: the queue runs its heavy steps behind it already, and a
nested call would wait on its own lock forever.

## Hard rules

- **Never look at the user's photos for sensitivity.** Do not Read, view,
  montage, contact-sheet or describe any file under the state folder's
  `quarantine/` or any file a batch's `scan.json` flags. Report flagged files
  as counts and reason labels only.
- **Nothing is deleted without the user's explicit yes in this chat**: no
  erase, no trash, no offload, no deleting a source folder. The run stops
  before every decision that is the user's; it never makes one.
- **Every review request carries its open command** (absolute path or URL) in
  its own fenced block: `xdg-open <absolute path or url>`.
- Only the exact folder the user names. Never `ls`, `find` or `du` above it,
  never widen it to its parent, and never the backup drive.

## Which kind

- **A folder on the laptop** (`~/Pictures/<old computer>`, `~/Downloads/<trip>`):
  `--kind folder`. Refused, with a clear message and nothing queued: HOME
  itself or anything containing it, anything outside HOME, the media-intake
  state, the library, the videos folder and every project folder from the
  config (so `~/Pictures` and `~/Videos` themselves too, with the defaults),
  `~/.local`, `~/.config`, `~/.cache`, `~/.var`, `~/.ssh`, `~/.gnupg`, common
  code and SDK trees, any hidden folder, password-manager names, and a git
  repository. Git trees inside an accepted folder are skipped. If it refuses,
  say why and ask for a narrower folder; don't work around it.
- **A folder on an external drive** (`/run/media/$USER/<drive>/<folder>`):
  `--kind drive` (the default). Refused on the laptop's own disk, on a drive
  that looks like the backup drive, and without a filesystem UUID.
- A phone or a GoPro: `media-intake stage --kind phone|gopro`, not the queue.

## Steps

1. **Ask only what the user didn't say**: are any days in it events (a shoot,
   a trip) that should be tagged, or routed to a project folder from the
   config? Each one is `--event [PROJECT/]SLUG@YYYY-MM-DD`, exactly as for
   `media-intake plan` (`docs/intake.md`). Everything else files by date.
2. **Add each folder** (several folders = one `add` each, each with its own
   short label: lowercase, digits, dashes):

   ```
   media-intake queue add "<folder>" --label <name> --kind folder [--event <project>/<slug>@<YYYY-MM-DD> ...]
   ```

   Report its counts and size. It refuses if the laptop would drop under 50 GB
   free once copied (queue a subfolder at a time instead).
3. **Run.**
   - **Small** (a few hundred files): run `media-intake queue run` in the
     background and report each chunk's summary line. It is resumable: if it
     stops or is killed, run it again and it continues at the step it was in.
   - **Big, or "run it overnight"**: check the laptop is on AC power, the
     local Ollama has nothing else loaded and no other heavy job runs; then
     start
     `nohup media-intake queue run --inhibit > <state>/queue-run.log 2>&1 &`
     and report progress from the log.
4. **"What's waiting in the queue?"**: `media-intake queue status`; relay its
   counts and commands one step at a time, and wait for "done" after each.
5. **The user's decisions**, one at a time, each with its `xdg-open` line:
   - the review page for a batch: `media-intake review <batch>` (in the
     background; give its URL as `xdg-open <url>`);
   - the quarantine (sensitive; counts only from you):

     ```
     xdg-open <state>/quarantine/<batch>
     ```

   - trash suggestions: `media-intake gallery` in the background, then its
     `xdg-open` line; the user picks Keep or Trash on each. Never trash on the
     user's behalf.
   - erasing confirmed sensitive files: `media-intake erase <batch>` only after
     the user says so here. Its HOME sweep also deletes a copy still sitting
     in a laptop source folder (same size and sha256): a folder source can
     never be inside a tree the sweep skips.
6. **The source afterwards**:
   - **Laptop folder**: the tool never deletes it (`offload` refuses kind
     folder). When `status` says everything is reviewed, the user checks the
     library and deletes the folder themselves:

     ```
     xdg-open <folder>
     ```

     Files the user trashed in the gallery are gone from the library only;
     their originals stay in the source folder until it is deleted.
   - **Drive folder**: `media-intake queue offload <name> --yes` only after the
     user says so in chat for that drive (refused while a quarantine is not
     empty, the drive's UUID differs or it looks like the backup drive).
     Without `--yes` it prints what it would do.
