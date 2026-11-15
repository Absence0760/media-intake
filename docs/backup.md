# Backup to a mounted drive

```sh
media-intake backup /run/media/$USER/<label> --dry-run   # what would be copied; writes nothing
media-intake backup /run/media/$USER/<label>             # copy what is new or changed
media-intake backup /run/media/$USER/<label> --verify    # then re-read every copy and compare sha256
```

`media_intake/backup.py`; tests: `python3 -I -m unittest discover -s tests -p test_backup.py`.

## What goes to the drive

```
<mount>/media-intake-backup/library/...          photos     (config `library`)
<mount>/media-intake-backup/videos/...           videos     (config `videos`)
<mount>/media-intake-backup/projects/<key>/...   each configured project folder
<mount>/.media-intake-backup                     empty marker file
```

- **Copied:** every media file (the photo and video extensions intake files)
  under those folders, and its `<file>.<ext>.xmp` sidecar, under the same
  relative path.
- **Never copied:** the state folder (`quarantine/`, `batches/` with staging
  and review copies, `index.json`, jobs), the thumbnail and proxy caches,
  hidden files and folders (digiKam's `.dtrash`, `.thumbnails`), symlinks,
  sidecars whose file is gone, and anything else that sits in the library
  folders (databases, notes). Even a state folder configured inside the
  library is skipped.
- **The marker** at the mount root makes `stage --kind drive` and `queue add`
  refuse this drive as a source ([queue.md](queue.md)).

## Rules

- **The destination must be an external drive's mount point.** `findmnt -T`
  must name the path itself as the mount, so the empty folder an unmounted
  drive leaves behind is refused ("is the drive mounted?"). A mount of `/`,
  `/home`, `/var`, `/tmp`, `/boot*`, `/usr`, `/opt`, `/srv` or `/root` (the
  laptop's own disk) is refused, and so is a mount inside the library, the
  state folder or the caches, or one that holds a library folder. A drive
  with `.media-intake-source-ok` at its root (you said it is not your backup
  drive) is refused until you remove that file.
- **Incremental.** A file whose size and sha256 already match at the
  destination is not copied again. Each run records, per file, the sha256
  and both sides' size and modification time in
  `<state>/backup/<drive id>.json` (on the laptop, 0600; the id is a hash of
  the drive's filesystem UUID). While both sides still look as recorded the
  recorded sha256 is trusted; otherwise both are hashed again. Like
  `index.json`, that record names library files; each run drops the entries
  of files no longer in the library, and `erase` / `forget` drop an erased
  file's entry at once (matched by path and sha256). A plain run
  therefore does not notice bit rot that leaves size and time alone:
  `--verify` does.
- **Atomic per file.** Each copy is written to a hidden
  `.<name>.media-intake-partial` in its destination folder, fsync'd, given
  the source's modification time and renamed over the final name, so an
  interrupted run never leaves a half-written file under a real name (the
  next run overwrites a leftover partial). exFAT and FUSE mounts that refuse
  fsync are tolerated. A file that changes while it is copied is skipped
  and counted ("run backup again").
- **Nothing on the drive is deleted.** A file removed from the library stays
  on the backup; the run only counts them. There is no `--prune`: erasing
  goes through `erase --drive` / `forget --drive`, which delete matching
  content anywhere on the drive by checksum ([intake.md](intake.md)). A file
  that changed in the library (an edit in another tool) replaces its own
  older copy, as any mirror does.
- **Never outside the backup folder.** Folders on the drive are created one
  at a time; a symlink or a file where a folder belongs fails that file
  ("could not be copied") instead of writing through it.
- **Letter case.** exFAT and FAT ignore case, so two library paths that
  differ only by case would be one file there: the second is not copied and
  is listed (rename one in the library).
- **Free space** is checked before the first copy.
- **Output is counts and relative paths only**, never file contents.

## `--verify`

After the copy pass, every destination copy is read again and its sha256
compared with the source's (the recorded one when the source is unchanged,
else hashed). Copies that differ or are missing are listed by path and the
command exits 1; a differing copy is dropped from the record, so the next
`backup` run copies it again. `--dry-run --verify` checks the existing
copies and writes nothing.

## The library lock

`backup` is a reader and does **not** take the library lock (docs/intake.md,
"One writer at a time"). A full first backup can take hours, and holding the
lock that long would refuse every gallery Keep, tag edit and intake step.
Instead each file is checked before and after its copy: one that changed or
vanished meanwhile is skipped and counted, and the next run copies it. Run
`backup` after an intake has finished for a backup that matches the library
at one point in time.

## Use an encrypted drive

Use a LUKS-encrypted drive (GNOME Disks: Format partition, "Password protect
volume (LUKS)", ext4 inside). On exFAT a delete only frees the clusters, so an
erased photo stays readable until something overwrites them (`wipe-free`
helps, but flash wear-levelling keeps spare blocks, so it is no guarantee).
With LUKS, a lost drive gives nothing away, and a drive you retire is
unreadable once its LUKS header is wiped (`cryptsetup erase`).

## Not tested here

The tests use a folder standing in for the drive (`MEDIA_INTAKE_TEST_MOUNT`)
and a faked `findmnt`. Real exFAT, ext4-on-LUKS and NTFS drives (fsync
refusals, name rules, timestamps) are not yet exercised in CI.
