# Review page (local only)

```
media-intake review BATCH [--phone ROOT] [--drive MOUNT] [--port 0]
media-intake review [BATCH] --maybe-trash [--phone ROOT]
```

Pass-throughs to `erase`: `--keep-phone-thumbnails`, `--no-wipe-free`.
`--port 0` (a random free port) is the default. It prints one `xdg-open
'http://127.0.0.1:<port>/?t=<token>'` line; open it in a private browser
window to keep it out of your history. If the batch has no quarantined or
review files it prints `nothing to review for BATCH` and exits 0.

- **Quarantine** tab: `quarantine/BATCH` and `quarantine/near-BATCH`, with
  the scanner's labels; Keep (release to the library) or Erase. **Review**
  tab: `batches/BATCH/review` with plan.json's `why`; Keep or Delete. Keys: K
  keep, E (or D) erase/delete, T trash, arrows move. Submit unlocks when every
  item is decided, shows a summary and needs ERASE typed.
- **Near-duplicates** (in either tab) show their original beside them:
  "Original (kept)" left, "This copy" right, each with resolution, size, date
  and folder (from plan.json's `why`; display only, served by id only from the
  library roots or this batch's `raw/` or `review/`). A `near-BATCH` item
  instead says "Near-copy of a photo you erased on <date>" (`erased_on` in its
  quarantine.json, plus the matched `near_of_sha`); the erased file is never
  looked for.
- **Messaging copies**: a `quarantine/xcheck-BATCH` folder (quarantine-only,
  no plan or manifest) is shown with `BATCH` too: messaging-app copies of
  library photos, each beside its `of` original. Keep releases to `dst`; its
  erase runs without `--phone` (the device originals are already offloaded)
  and never moves an `of` original to near-quarantine.
- **Trash** (T) on every item: `media-intake trash` (plain delete for junk
  that isn't private, remembered by checksum so `plan` drops it next time;
  with `--phone` when the page has it, except xcheck items). It runs first in
  each tab. `--maybe-trash` (or `media-intake suggest-trash`, which opens it)
  adds a "Maybe trash" tab from `maybe-trash.json`, re-checked against the
  library roots: Keep runs `not-trash`, Trash runs `trash`. With no BATCH,
  decisions go to `maybe-trash-decisions.json`. The confirm summary shows
  keep / erase / trash per tab.
- **On submit** it writes `batches/BATCH/review-decisions.json` (0600,
  relative paths and decisions) first, then runs: review deletions
  (`purge-review` when all are deleted, else file by file plus its
  freedesktop thumbnail; kept review items stay in `review/`, there is no
  command to file them yet); then for `near-BATCH`, `xcheck-BATCH`, then
  `BATCH`: `release` for kept files, `erase --yes` with the devices given,
  `verify-gone`. A kept file with no library place (it was quarantined from
  review) stays put and that batch's erase is skipped. Closing the tab
  without submitting changes nothing (Ctrl-C the server).

## The run is a detached job

Submit starts the steps above as a detached job (`media_intake/jobs.py`), not
inside the page server, so an erase that has started always runs to the end
of its steps:

- **Closing the tab or stopping the server never stops it.** The job runs in
  its own process session; Ctrl-C on the server says the run keeps going.
- **Progress:** the page polls the job and shows the current step ("Step 3:
  erase BATCH") and its output from the first line. The server exits once the
  page has read the end.
- **Picked up again:** `media-intake review BATCH` while a run of that batch
  is going (even with nothing left to decide) serves a page that shows the
  run, not stale items; a page already open for the batch switches to it
  within a few seconds, and media are no longer served.
- **One run per batch:** the job lock is an `flock` on
  `<state>/jobs/review-BATCH.lock` (`maybe-trash` without a batch), taken
  before `review-decisions.json` is written, so a second submit from any page
  or server gets 409 and the page shows the running job instead. Each
  `intake.py` step holds the lock too.
- **Crashes are reported:** a job whose worker PID is gone (checked by PID and
  its `/proc` start time, never by command line) is marked `crashed` in its
  status file and the page says at which step. It is never re-run by itself:
  open the review again and it shows what is left on disk. If the step the
  dead worker started is still running, the page says so and submit stays
  refused until that step ends.

Files, all in the private state dir: `<state>/jobs/` (0700), and in it
`<key>.json` (status: state, step, pid, the run's plan), `<key>.log` (the
output) and `<key>.lock`, each 0600. The next run of the same batch
replaces them.

## Why it is local

The quarantine can hold nudity and IDs. stdlib only, no external URL anywhere
in the page, bound to 127.0.0.1, a random token (swapped for a Strict
HttpOnly cookie after the first load), a Host check (DNS rebinding), a CSRF
header and an Origin check on POST, `no-store` and a strict CSP on every
response. Media are served by opaque id from the files themselves (no
thumbnails written), with Range for video seeking. HEIC/DNG show "no
preview"; open those in your file manager.

An AI assistant may start the server for you but must never open the page or
its media.

## Test

`python3 -I -m unittest discover -s tests -p test_review_ui.py` (synthetic media; a fake
`intake.py` via `MEDIA_REVIEW_INTAKE` records its argv, so a real erase never
runs; its erase can wait on a FIFO so the tests kill the server or the worker
mid-run). The job module itself: `python3 -I -m unittest discover -s tests -p
test_jobs.py`.

Not verified: the page's progress, attach and crash messages in a real
browser (the tests drive the HTTP API; the inline script is checked with
`node --check`).
