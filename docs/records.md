# Photos of documents: the records inbox

media-intake keeps photos and videos. A photo of a document (an ID card, a
passport, a payment card, a receipt or a form) belongs with your other
records, which a separate tool keeps in its own folder (`records.root`,
[configuration.md](configuration.md)). `media-intake to-records` hands such a
photo to that tool's inbox and, once your records backup holds it, erases the
original here.

## Set up

In `~/.config/media-intake/config.toml`:

```toml
[records]
# root = "~/Documents/Records"        # the default ($PRIVATE_RECORDS wins)
inbox = "00 Inbox"                    # the folder inside root that your records tool reads
# archive = "backups/records-*.tar.gpg"   # optional: where your records backup lands on the backup drive
```

`inbox` has no default, so `to-records` is off until you set it. It must be
inside `root`, or be (or be inside) a `protect` folder: no sweep ever enters
those (erase, forget, sweep-erased, verify-gone and the backup-drive sweep
skip them), so the copy handed over is never erased with its original.

### An inbox outside the records folder

A document tool that keeps its own storage, such as a document management
server with a consume folder, can take the handoff directly. List its consume
folder **and every folder where it stores what it took in** under
`protect`; then point `inbox` at the consume folder:

```toml
[records]
inbox = "~/doc-tool/consume"
protect = ["~/doc-tool/consume", "~/doc-tool/media"]
```

The storage folder matters as much as the consume folder. The tool keeps the
document's bytes unchanged, and `to-records --finish` erases every copy of an
original it finds, so a stored copy outside `protect` would be erased with
it. Each `protect` folder is held to `root`'s rules: an absolute path (or
`~/...`), never your home folder or one above it, and apart from `state`,
`library`, `videos` and the project folders. `media-intake stage` refuses it
as a source, and the erase coverage report lists it as not searched.

### Recipes

- **A plain folder** you sort by hand: `inbox = "00 Inbox"` inside `root`
  (the default layout above). Nothing watches it, so nothing else is needed.
- **A document tool that watches a folder** (any tool that picks up new
  files from a folder): the folder you give it as `inbox`, inside `root` or
  in `protect`, with the tool's own storage in `protect` too when it is
  outside `root`. A copy appears in one rename, so a watcher sees one whole
  file arriving and never a part-written one.
- **Paperless-ngx's consume folder**: `inbox` is the host folder bound to its
  consume folder (`PAPERLESS_CONSUMPTION_DIR` inside the container), and
  `protect` lists that folder and the host folder bound to its media folder,
  where it keeps originals. It deletes a file from the consume folder once it
  has taken it in, and from then on the copy is its to keep. The copy is
  `0600`, so the container's user (`USERMAP_UID`) must be you. Paperless-ngx
  watches with inotify; a rename into the folder is one arrival it sees. On a
  network share, where inotify sees nothing, set its polling option instead.

## Hand photos over

```
media-intake to-records FILE...            # a dry run: what would be copied
media-intake to-records FILE... --yes      # copy them and hold the originals
media-intake to-records --list             # what is handed over and still held
```

`FILE` is a library file, or a quarantined file (under the state folder's
`quarantine/<batch>/`) that the scan flagged only as an ID (`id-barcode`), a
passport (`passport-mrz`) or a payment card (`payment-card`).

- **Never handed over**: a file flagged for nudity, a 2FA code (`2fa-qr`),
  secret words, or as not checked (`unscanned-*`). It stays in quarantine,
  for you to erase as before. A 2FA code or a secret belongs in your
  password manager. A library file goes only if it is a photo or video and
  not a copy of a file the scan flagged, quarantine holds or erase removed.
  One refused file refuses the whole run, and nothing is copied.
- **The copy**: the file's bytes, unchanged (no conversion), under its own
  name in the inbox, or `<name> (2).<ext>`, `(3)`, ... when that name is
  taken. A file in the inbox is never overwritten. The inbox is created
  `0700` when missing, each copy `0600`. The copy is written in a staging
  folder beside the inbox (`.<inbox name>.staging`, `0700`, on the same
  filesystem), fsynced and checked against the original's sha256, and only
  then renamed into the inbox in one step that never replaces a file
  (`renameat2` with `RENAME_NOREPLACE`; a hard link where the filesystem
  lacks it). The inbox only ever holds whole, checked files. A copy that
  fails its check never reaches the inbox, and a stopped run's leftover in
  the staging folder is removed by the next run; until then no sweep enters
  the staging folder, as for `protect`, so such a leftover is never erased
  with the original. The staging folder needs you to be able to write to the
  inbox's parent folder. An inbox that is a mount point of its own is
  refused, since its staging folder would be on another filesystem. Nothing
  else (no sidecar, tags or manifest) goes in: the
  records tool computes its own checksum. Once a copy is in the inbox, it
  belongs to the tool that reads it.
- **The original is held, not erased.** A library file stays where it is,
  so your backups still hold it, and the gallery hides it. A quarantined one
  moves to `quarantine/records-held/`, so its batch's erase of the other
  files no longer waits for it. `erase records-held` and `release
  records-held` are refused: held files leave only through `--finish`.
  `<state>/records-handoff.json` (`0600`) lists what is held: names,
  checksums and times, never content. Each step is recorded before a file
  moves, so a run stopped midway never leaves a held file unlisted.

## Finish: erase the originals

Only once your records backup holds the copies, because until then the inbox
copy is the only one outside this tool:

```
media-intake to-records --finish --drive DRIVE                # dry run
media-intake to-records --finish --drive DRIVE --yes --no-wipe-free
media-intake to-records --finish --backup-verified --yes      # no archive check
```

- With `records.archive` set and `--drive`, the newest file on the drive
  matching that glob decides what is due: only photos handed over before it
  was written are erased, and later ones stay held. No match: nothing is
  erased.
- Without it, `--backup-verified` says you checked the records backup holds
  every copy handed over so far.
- The erase is forget's and erase's own: every copy in your home folder, the
  sidecars, thumbnails, gallery caches and recognition data, and with
  `--drive` the copies on the backup drive, with the usual coverage report
  and `verify-gone`. The inbox copy, inside the records folder, stays.
- Run the records backup after the handoff, not during it: the archive's
  date is when it finished, so a photo handed over while it was being
  written would count as in it.
- Only what was handed over is erased: a held file whose size or sha256
  changed since is left where it is and no longer held (hand it over again).
  One no longer where it was held, and not in the erase log, stays listed
  and is named: a `--finish` stopped midway leaves it in
  `quarantine/records-<date>/`, and erasing that batch finishes it.
- A quarantined document's original on the phone is not deleted by
  `--finish` (the phone is long gone by then): `offload` handles the phone.

## Tests

`tests/test_to_records.py`, in a throwaway HOME with its own records folder:
a dry run creates nothing; while a copy is written the inbox stays empty
(it arrives whole, in one rename) and the staging folder is `0700` and left
empty; a copy that fails its sha256 check never reaches the inbox, and a
stopped run's leftover is removed; without `renameat2` (or when it answers
"not supported") the hard-link fallback still never overwrites, any other
error leaves nothing behind, and a link that arrived counts even if its
staging name can't be dropped; no free name leaves the inbox as it was; a
staging path that is a symlink, a file or on another filesystem is refused;
an inbox in a `protect` folder outside `root` is handed to, and `--finish`
erases the original but not the handed copy, the tool's stored copy in
another `protect` folder or a leftover in the staging folder, and its
coverage lists the `protect` folder as not searched; an inbox in a missing
`protect` folder hands nothing over; a copy
is byte-identical, `0700`/`0600`, alone in
the inbox, its original held and hidden from the gallery, and a second
handoff of it refused; a taken name gets ` (3)` and nothing is overwritten; a
quarantined ID moves to `records-held` and out of its batch; nudity, 2FA,
secret words, unscanned and reason-less files and files outside the library
are refused, and one refusal copies nothing; a library copy of a flagged
file and a non-photo are refused; `erase` and `release` of `records-held`
are refused; `--finish` leaves a changed file and keeps a missing one
listed; `--finish` refuses without
proof, dry-runs, then erases the originals and their stray copies but never
the inbox copies; with an archive on the drive only what it holds is erased;
and the `records.inbox` / `records.archive` config rules.
