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
inside `root`: no sweep ever enters the records folder (erase, forget,
sweep-erased and verify-gone skip it), so the copy handed over is never
erased with its original.

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
  password manager. One refused file refuses the whole run, and nothing is
  copied.
- **The copy**: the file's bytes, unchanged (no conversion), under its own
  name in the inbox, or `<name> (2).<ext>`, `(3)`, ... when that name is
  taken. A file in the inbox is never overwritten. The inbox is created
  `0700` when missing, each copy `0600`. The copy is fsynced and checked
  against the original's sha256, and nothing else (no sidecar, tags or
  manifest) goes in: the records tool computes its own checksum.
- **The original is held, not erased.** A library file stays where it is,
  so your backups still hold it, and the gallery hides it. A quarantined one
  moves to `quarantine/records-held/`, so its batch's erase of the other
  files no longer waits for it. `<state>/records-handoff.json` (`0600`)
  lists what is held: names, checksums and times, never content.

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
- A quarantined document's original on the phone is not deleted by
  `--finish` (the phone is long gone by then): `offload` handles the phone.

## Tests

`tests/test_to_records.py`, in a throwaway HOME with its own records folder:
a dry run creates nothing; a copy is byte-identical, `0700`/`0600`, alone in
the inbox, its original held and hidden from the gallery, and a second
handoff of it refused; a taken name gets ` (3)` and nothing is overwritten; a
quarantined ID moves to `records-held` and out of its batch; nudity, 2FA,
secret words, unscanned and reason-less files and files outside the library
are refused, and one refusal copies nothing; `--finish` refuses without
proof, dry-runs, then erases the originals and their stray copies but never
the inbox copies; with an archive on the drive only what it holds is erased;
and the `records.inbox` / `records.archive` config rules.
