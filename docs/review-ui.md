# Review page (local only)

```
media-intake review BATCH [--phone PHONE] [--drive DRIVE] [--port 0]
media-intake review [BATCH] --maybe-trash [--phone PHONE]
```

Pass-throughs to `erase`: `--keep-phone-thumbnails`, `--no-wipe-free`.
`--phone` is the phone or camera the batch came from (its folder) and
`--drive` the backup drive (the folder it is mounted on); the words are in
[words.md](words.md). `--port 0` (a random free port) is the default. It prints one `xdg-open
'http://127.0.0.1:<port>/?t=<token>'` line; open it in a private browser
window to keep it out of your history (`xdg-open` opens a normal one; the
private-window command is in [gallery.md](gallery.md)). If the batch has no quarantined or
review files it prints `nothing to review for BATCH` and exits 0.

- **Quarantine** tab: `quarantine/BATCH` and every level of its near-copy
  chain (`near-BATCH`, `near-near-BATCH`, ...; see below), with
  the scanner's labels; Keep (release to the library) or Erase, nothing else
  (see "Quarantine is Erase-only" below). **Review** tab:
  `batches/BATCH/review` with plan.json's `why`; Keep, Delete this copy or
  Trash. Keys: K keep, E erase (Quarantine only), D delete this copy (Review
  only), T trash (not in Quarantine), X opens or closes a burst stack,
  arrows (and J) move. Submit unlocks when every item is decided, shows a
  summary and needs ERASE typed when anything will be removed (an all-keep
  apply asks for no word).
- **The words** match the gallery's: **Keep**, **Trash** (removed, and
  skipped if the same photo is imported again: `media-intake trash`) and
  **Erase** (every copy removed). Review adds **Delete this copy**: only that
  file goes, with nothing remembered. Every choice is only a mark until you
  submit; pressing a pressed button (or its key) again clears it. Once
  marked, a card says what will happen ("Decided: Keep. It stays in the
  review folder for now."), and each button's tooltip says the same before
  you press it.
- **Plain labels.** The page maps the tools' own names to plain ones in one
  place (`FLAG`, `GROUP`, `plain()` in the page script): scanner flags
  (`nudity` is "Possible nudity", `id-barcode` "Looks like an ID card",
  `secret-words:banking` "Private text: banking"), tag groups
  (`Review|maybe-trash` is "Tagged as junk", `Label|food` "Looks like:
  food"; any other `Group|value` shows as "Group: value"), and the plan's
  `why` ("Tiny or empty image", "Near-copy of ..."). A card shows its path in
  the batch without the sub-batch prefix (`near-BATCH`, `xcheck-BATCH`), and
  the meta line names a folder only for a library original, never the
  tool's own state folder.
- **Header.** The title names the batch ("Review BATCH", or "Review
  suggested junk" for `--maybe-trash` alone). One counter says what is left
  and so why Submit is locked: "Decide 3 more to submit (1 in Quarantine, 2
  in Review)" (the tabs are named unless only the open one is left), then
  "All decided: 13 of 13. Ready to submit.". On a phone (640 px or less) the
  tabs sit on one scrolling row and only the bar with the counter and
  Submit stays pinned while you scroll; on a touch screen without a mouse
  the Keys button and the key hints are hidden. Every button and the ERASE
  box are at least 44 by 44 px.
- **The Apply dialog** is titled and labelled with the action ("Apply 13
  decisions?", button "Apply 13 decisions") and lists one line of counts per
  tab ("Quarantine: keep 8, erase 1", "Review: keep 1, delete this copy 0,
  trash 1"), then what goes where in words ("Erase removes every copy: on
  this computer and your phone."; no device path), and how many files will
  be removed for good, or "Nothing will be deleted.". ERASE is typed only
  when something is removed (erase, trash or delete this copy); an all-keep
  apply deletes nothing, so the box is hidden and the server needs no word.
- **Keyboard and screen readers.**
  - `?` (or the header's "Keys (?)") opens a list of every key the page
    handles and where each applies: K everywhere; E in Quarantine only; D in
    Review only; T in Review and Maybe trash (nothing in Quarantine); X on a
    stack; → ↓ J next, ← ↑ previous; on a tab, ← → Home End switch lists;
    Esc closes a dialog. Letter keys and `?` do nothing while you type.
  - The tabs are a tablist (`role="tab"`, `aria-selected`, one Tab stop:
    the selected tab); ↑ ↓ on a tab still move between items. Each card is
    a labelled group ("<name>, item 2 of 5: decided erase" or "undecided"),
    and the Keep / Erase / Delete this copy / Trash buttons carry
    `aria-pressed`.
  - Keyboard focus shows a 3 px outline in the focus colour (6:1 or more
    against the page and the cards, light and dark). The focus follows the
    outlined card after a key decision or move, stays on the stack's cover
    when it opens or closes, and Tab onto a card or its buttons outlines
    that card, so K acts there.
  - The Apply dialog is a modal dialog (`aria-modal`, labelled by its
    title, described by the summary): the page behind is inert, Tab stays
    inside, Esc is Back, and the focus returns to Submit.
  - The header's counter is `role="status"` (a screen reader hears it) and
    describes Submit (`aria-describedby`), so a locked Submit says why.
  - A refused submit (409 or 400 from the server, or no answer) shows the
    reason in a dismissible `role="alert"` banner, under the dialog's title
    while it is open and at the top of the list, below the header, after
    Back; Apply stays available to retry. A busy library reads "Not
    applied: Another action is using the library (...). Wait for it to
    finish, then apply again.", the brackets naming what holds it in the
    gallery's words ("the media-intake apply command", a run from the
    review page, ...); a run of the same batch reads "A run of this review
    is still going. ...". The server writes these words
    (`review_ui.busy_words`, with `library_lock.plain_holder`), so neither
    the 409 nor the page ever carries a PID or a lock file's path. The page opens no native `alert()`, `confirm()` or `prompt()`.
  - The Keys button stays usable while a run goes.
  - While a run goes, its step line is a polite live region and its output
    a `role="log"`, so new lines are announced as they arrive.
- **Colours** follow the system's light or dark scheme (the page declares
  `color-scheme: light dark`, so form controls and scroll bars follow too).
  Every label meets WCAG 2's 4.5:1 contrast in both: the burst badge, a
  pressed Keep / Erase / Trash and Submit carry white text in light and
  near-black text in dark.
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
  library photos and videos, each beside its `of` original ("Telegram copy of
  this photo", or "... video", the app taken from the copy's
  `Messaging/<App>/` folder; "Messaging copy" when its path names none). Keep releases to `dst`; its
  erase runs without `--phone` (the device originals are already offloaded)
  and never moves an `of` original to near-quarantine.
- **Quarantine is Erase-only.** A quarantined item (any level, `xcheck-`
  included) was flagged sensitive, and plain trash would move only that one
  file to the desktop trash, where it lingers, and leave its messaging copies
  and near-copies behind. So its card has no Trash button, T does nothing on
  it, and it says "Sensitive: erase or keep" instead. The server refuses it
  too: a submit with `trash` for a quarantined item gets 400 "a quarantined
  item can't be trashed: erase or keep it", before `review-decisions.json` is
  written or any job starts. The run itself also stops if one ever reaches it.
  `media-intake trash` refuses a path under `quarantine/` the same way, from
  the command line.
- **Trash** (T) on Review and Maybe trash items: `media-intake trash --yes`
  (the page's ERASE confirm is the question; plain delete for junk that isn't
  private, remembered by checksum so `plan` drops it next time; with `--phone` when the page has it). It runs first in each
  of those tabs. `--maybe-trash` (or `media-intake suggest-trash`, which opens it)
  adds a "Maybe trash" tab from `maybe-trash.json`, re-checked against the
  library roots: Keep runs `not-trash` (`Owner|keep` + `Owner|not-trash`, the
  gallery's Keep rule), Trash runs `trash` (the sha256 goes in `trashed.json`,
  the durable `Owner|trash`; copies left on disk get `Owner|trash` in their
  sidecar: [intake.md](intake.md), "Owner decisions"). With no BATCH,
  decisions go to `maybe-trash-decisions.json`. The Apply dialog's summary
  shows keep / erase in Quarantine, keep / delete this copy / trash in
  Review and keep / trash in Maybe trash.
- **Burst stacks** (in each tab): shots from one device a moment apart show as
  one stack with a count badge ("Burst: 4 shots, 2 undecided"); "Show all"
  (or X) opens it and every shot follows the first. On an open stack each shot
  has "Keep this one, trash the rest" ("... erase the rest" in Quarantine): it
  sets Keep on that shot and Trash on the others, or Erase in Quarantine,
  where every shot is flagged and so takes the erase-everywhere path, never
  plain trash. Nothing more: they run with the rest at submit, after the same
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
  or a `burst_id` in the plan entry (a camera's BurstUUID or GCamera BurstID,
  which `plan` records: [intake.md](intake.md), "Burst id"). The date, device and dHash come from `plan.json` (this
  batch's quarantine and review items; a maybe-trash library file by its
  `dst`, plus `index.json` for its dHash), so near- and xcheck- items, and
  library files with no plan entry, are never stacked. Stacks never cross a
  tab or a quarantine batch. No file is opened to group them.
- **On submit** it writes `batches/BATCH/review-decisions.json` (0600,
  relative paths and decisions) first, then runs: review deletions
  (`purge-review --yes` when all are deleted, else file by file plus its
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
thumbnails written), with Range for video seeking. HEIC/HEIF photos are
converted to a JPEG of at most 1600 px in memory as they are served (never
written to disk); that needs pillow-heif, which `setup.sh`'s venv has, so a
page started with a Python that lacks it re-runs itself under the venv's
Python (`MEDIA_REVIEW_NO_VENV=1` turns that off). Without the venv, HEIC shows
"no preview", and so do DNG and GPR; open those in your file manager. A card whose file the browser can't
decode says so instead of an empty box: a picture it can't show, or a video it
can't play, including one whose sound plays but whose picture doesn't (an
iPhone HEVC `.mov` in Chromium); open those in your file manager too. This
page makes no H.264 proxy (quarantined files must not gain a cached copy);
the gallery makes one once a video is filed ([gallery.md](gallery.md), "Video
proxies").

An AI assistant may start the server for you but must never open the page or
its media.

## Test

`python3 -I -m unittest discover -s tests -p test_review_ui.py` (synthetic media; a fake
`intake.py` via `MEDIA_REVIEW_INTAKE` records its argv, so a real erase never
runs; its erase can wait on a FIFO so the tests kill the server or the worker
mid-run). The job module itself: `python3 -I -m unittest discover -s tests -p
test_jobs.py`. Burst grouping: `python3 -I -m unittest discover -s tests -p
test_bursts.py`.

Every decision on a photo, a video and a mix of both, run by the real
`intake.py` (no fake) in a throwaway HOME and checked by what is left on disk:
`python3 -I -m unittest discover -s tests -p test_review_media_kinds.py`. Keep
releases the file with its sidecar and sha256 unchanged, Erase removes it and
its copies, index entries and thumbnails, Trash (Review) records its sha256,
a Trash on a quarantined file is refused and leaves it untouched, and a
burst's kept shot survives the erase's near-duplicate search; Maybe trash Keep
needs exiftool and skips without it.

In headless Chromium (`tests/browser/test_browser_review.py`, see
CONTRIBUTING.md, Tests): the keys and arrows, no Trash button or T on a
quarantined card (and the 400 for a trash posted anyway) while Erase still
runs, Submit unlocking, the ERASE
panel, a card's video seeking by Range, and the run's progress and re-attach
after a reload. Its `ReviewA11yBrowserTest` covers the tablist and its arrow
keys, the focus outline at 3:1 or more in light and dark, the focus kept
after a decision and a stack opening or closing, the confirm as a modal
dialog (Tab trapped, Esc, focus back on Submit), the `?` keys list, a
refused submit (the library lock held) shown in the banner and then retried,
and the "N of M decided" counter; every test there fails if a native dialog
opens. `tests/browser/test_browser_gallery_layout.py` checks the
burst badge, a pressed Keep and Submit at 4.5:1 or more in light and dark,
and the `color-scheme` declaration. `tests/browser/test_browser_review_media_kinds.py`: burst
stacks (K on a closed stack, X, "Keep this one" in Quarantine and Review) with
a video beside each that is never stacked, the cards for files the browser
can't decode, and a messaging copy of a video beside its original.

Not verified: a real screen reader (the tests check roles, names and
`aria-*` attributes, not what one announces), the crash message in a real browser (the HTTP tests cover the
crash itself), a real HEVC file (the test stands in a file with sound and no
picture), the layout beyond those colours, and any browser but Chromium.
