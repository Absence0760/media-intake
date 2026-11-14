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

- **Quarantine** tab: `quarantine/BATCH` and every level of its near-copy
  chain (`near-BATCH`, `near-near-BATCH`, ...; see below), with
  the scanner's labels; Keep (release to the library) or Erase. **Review**
  tab: `batches/BATCH/review` with plan.json's `why`; Keep or Delete. Keys: K
  keep, E (or D) erase/delete, T trash, X opens or closes a burst stack,
  arrows move. Submit unlocks when every
  item is decided, shows a summary and needs ERASE typed.
- **Near-duplicates** (in either tab) show their original beside them:
  "Original (kept)" left, "This copy" right, each with resolution, size, date
  and folder (from plan.json's `why`; display only, served by id only from the
  library roots or this batch's `raw/` or `review/`). A near-copy of an
  erased photo (any `near-` level) is shown beside the **root original** when
  one survives: the library photo the erased one was itself a copy of (an
  xcheck entry's `of`, carried down the chain as `root_of`), same roots check,
  re-checked at start-up. Otherwise it says "Near-copy of a photo you erased on
  <date>" (`erased_on` in its quarantine.json, plus the matched
  `near_of_sha`); the erased file is never looked for and its path is never
  stored.
- **Near-copy chains**: erasing `quarantine/X` moves near-copies of what it
  erased to `near-X`; erasing `near-X` moves theirs to `near-near-X`. One
  review of `BATCH` shows every level that exists, of `BATCH` and of
  `xcheck-BATCH`. The chain stops at depth 3 (`NEAR_DEPTH` in intake.py: each
  level allows 4 dHash bits, so level 3 can be 12 of 64 bits from the photo
  first erased; past that a match is likelier another picture than a copy).
  Erasing a level-3 batch moves nothing: the matches stay where they are and
  erase prints how many. A level that already exists is added to, never
  replaced.
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
  library roots: Keep runs `not-trash` (`Owner|keep` + `Owner|not-trash`, the
  gallery's Keep rule), Trash runs `trash` (the sha256 goes in `trashed.json`,
  the durable `Owner|trash`; copies left on disk get `Owner|trash` in their
  sidecar: [intake.md](intake.md), "Owner decisions"). With no BATCH,
  decisions go to `maybe-trash-decisions.json`. The confirm summary shows
  keep / erase / trash per tab.
- **Burst stacks** (in each tab): shots from one device a moment apart show as
  one stack with a count badge ("Burst: 4 shots, 2 undecided"); "Show all"
  (or X) opens it and every shot follows the first. On an open stack each shot
  has "Keep this one, trash the rest": it sets Keep on that shot and Trash on
  the others, nothing more. They run with the rest at submit, after the same
  ERASE confirm, guards and job as any decision (no endpoint of its own):
  Trash is `media-intake trash` (the sha256 goes in `trashed.json`), Keep is
  the tab's Keep (Maybe trash: `not-trash`, so `Owner|keep`; Quarantine:
  `release`; Review: stays in `review/`). A closed stack's buttons decide the
  first shot only, and Submit stays locked until every shot is decided.
  Grouping (`media_intake/bursts.py`, pure functions, runnable on a JSON list
  of records): same device (an unknown one groups only with unknown ones),
  images only, each shot at most `GAP` (2 s, inclusive) after the one before,
  and, when both have a dHash, at most `DHASH_MAX` (10 of 64) bits apart. A
  burst marker wins over the clock: the same marker is one burst whatever the
  gap, two different markers never are. Markers come from the file name
  (`IMG_20261108_071500_BURST001.jpg`, Pixel's `..._BURST<start time>...jpg`)
  or a `burst_id` in the plan entry (a camera's BurstUUID; intake does not
  record one yet). The date, device and dHash come from `plan.json` (this
  batch's quarantine and review items; a maybe-trash library file by its
  `dst`, plus `index.json` for its dHash), so near- and xcheck- items, and
  library files with no plan entry, are never stacked. Stacks never cross a
  tab or a quarantine batch. No file is opened to group them.
- **On submit** it writes `batches/BATCH/review-decisions.json` (0600,
  relative paths and decisions) first, then runs: review deletions
  (`purge-review` when all are deleted, else file by file plus its
  freedesktop thumbnail; kept review items stay in `review/`, there is no
  command to file them yet); then for each near- level of `BATCH` (deepest
  first), each near- level of `xcheck-BATCH` (deepest first), `xcheck-BATCH`,
  then `BATCH`: `release` for kept files, `erase --yes` with the devices given,
  `verify-gone`. Deepest first because erasing a level writes the next one
  down: a near-copy the run itself finds lands in a level already done, is
  never erased unreviewed, and the log says "N near-duplicates now in
  quarantine/<level>: review BATCH again to decide them". A kept file with no library place (it was quarantined from
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
  `intake.py` step holds the lock too. The run also holds the library lock
  from submit to its last step ([intake.md](intake.md), "One writer at a
  time"), so it never overlaps a gallery Trash or Erase, another batch's run
  or a CLI command that changes the library; each step inherits it instead of
  waiting on its own run. A page checking the job lock (a crashed
  run's step) never makes a submit at that instant fail: both go through a
  short mutex, `<key>.mutex`.
- **A page older than the last run can't submit:** the server records the
  batch's last run when it reads the list. If another page's run has ended
  since (it can have written a fresh `near-BATCH`), submit gets 409 "a run
  finished since this page loaded; open the review again", and nothing runs.
- **Crashes are reported:** a job whose worker PID is gone (checked by PID and
  its `/proc` start time, never by command line) is marked `crashed` in its
  status file and the page says at which step. It is never re-run by itself:
  open the review again and it shows what is left on disk. If the step the
  dead worker started is still running, the page says so and submit stays
  refused until that step ends.

Files, all in the private state dir: `<state>/jobs/` (0700), and in it
`<key>.json` (status), `<key>.log` (the output), `<key>.lock` and
`<key>.mutex`, each 0600. The next run of the same batch replaces them.

**No file name stays there.** The run is an erase, so the paths and decisions
go to the worker on a pipe and are never written (the decisions are in
`review-decisions.json`, by relative path). The status keeps the batch, the
item count, state, step and its label (`erase BATCH`), ok, PIDs and an opaque
id. Command lines are logged without paths (`release BATCH [2 files]`,
`--phone [path]`). The steps' own output can name files, so it streams while
the run goes, and once the run ends (done, or crashed: the next page to look,
or the start-up sweep of any review or gallery, notices) each of those lines is
replaced in place by `[output removed when the run ended: it may name a
file]`. A page that was watching still shows every line (it reads through its
open file); a page opened afterwards sees the scrubbed log.

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
test_jobs.py`. Burst grouping: `python3 -I -m unittest discover -s tests -p
test_bursts.py`.

In headless Chromium (`tests/browser/test_browser_review.py`, see
CONTRIBUTING.md, Tests): the keys and arrows, Submit unlocking, the ERASE
panel, a card's video seeking by Range, and the run's progress and re-attach
after a reload.

Not verified: the crash message in a real browser (the HTTP tests cover the
crash itself), burst stacks in a real browser (opening, X, keep-one; the HTTP
tests cover the grouping, the page code being served and the submit it leads
to), layout, and any browser but Chromium.
