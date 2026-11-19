# Words used here

Each word below has one meaning everywhere: in `media-intake --help`, every
command's `--help`, the review page, the gallery and these docs. The page
buttons are named in **bold**, as they appear on screen.

## Where files are

| Word | Means | Not to be confused with |
| --- | --- | --- |
| **source** | Where an import copies files from: a phone, a camera (or its card), a drive you import from, or a folder on this laptop. `stage SOURCE --kind phone\|gopro\|drive\|folder`. The tool only reads it; only `offload` and `erase --phone` delete from it. | The backup drive. |
| **phone** | A phone or camera plugged in by USB, as a source. A `--phone` option takes its folder (on Fedora, under `/run/user/$UID/gvfs/`, shown while the phone is unlocked and set to File transfer). `offload --phone` also takes a source drive's folder. | |
| **source drive** | An external drive you import old photos from (`stage --kind drive`, the queue). | The backup drive. |
| **backup drive** | The external drive `backup` copies the library to. Every `--drive` option (`erase`, `forget`, `verify-gone`, `gallery`, `review`), `wipe-free DRIVE` and the `<DRIVE>` in intake's `next: media-intake backup <DRIVE>` hint mean this drive. The `backup` command's own help still calls it `MOUNT`; it is the same folder. | A source drive. |
| **mounted on** | The folder a plugged-in drive shows up as, for example `/run/media/$USER/<label>`. You name a drive by this folder, written `DRIVE` in help text. The `backup` command's help (and older help text) still calls it the mount point or `MOUNT`. | |
| **library** | Your photos and videos folders (by default `~/Pictures/library` and `~/Videos/personal`) plus any project folders: where filed files live, and what the gallery shows. | The state folder. |
| **state folder** | The tool's private folder (by default `~/.local/share/media-intake`, readable only by you). Batches, quarantine, the review folders and the logs live here. | The library. |
| **sidecar** | The `<file>.<ext>.xmp` file next to a photo or video that holds its tags. The tool writes tags only there; the photo itself is never changed. | |

## An import

| Word | Means |
| --- | --- |
| **batch** | One import: the files one `stage` copied in, under one name (by default `<device id>-<YYYYMMDD>`, or the `--batch` you gave). Every command that takes `BATCH` wants that name. |
| **stage, plan, scan, apply, tag** | The import's steps, in order: copy in, work out dates and duplicates, look for private files, file into the library, write tags. |
| **flagged** | A file a detector (nudity, ID or card text, codes) marked as possibly private, or could not check. `apply` moves flagged files to quarantine. |
| **quarantine** | Where flagged files wait, in the state folder, for you to decide: **Keep** or **Erase**, never Trash. No assistant or tool ever opens them; you look at them yourself. |
| **exact duplicate** | A file whose content is already in the library. `apply` drops the staged copy. |
| **near-duplicate** | A photo that looks like one already in the library or in the batch (a re-save, a resize, a burst shot). Set aside in the review folder. |
| **review folder** | A batch's near-duplicates and junk (tiny or empty images), set aside by `apply` for a glance. `purge-review` deletes it. |
| **offload** | Delete a batch's files from the source (phone, camera or source drive) once each has a checked copy in the library. Never for a laptop folder: you delete that yourself. |

## Deciding

| Word | Means |
| --- | --- |
| **review page** | `media-intake review BATCH`: a page on this computer only where you decide on what an import set aside. Its tabs are **Quarantine** (flagged files), **Review** (the review folder) and, with `--maybe-trash`, **Maybe trash** (Suggested junk). |
| **mark** | On the review page, a choice is only a mark until you apply it. Pressing a pressed button again clears it. |
| **Submit**, **Apply N decisions** | The review page's **Submit** opens the Apply dialog ("Apply 13 decisions?"); its **Apply N decisions** button runs every mark. When anything will be removed you type `ERASE` first. In the gallery's Suggested junk view, **Apply** does the same for its K / T marks. |
| **Keep** | Keep the file. In Quarantine: put it back in the library (`release`). In Review: leave it in the review folder for now. For Suggested junk: leave it in the library and never suggest it again (`not-trash`). In the gallery: mark it kept (the tile says "Kept"). |
| **Trash** | Delete a file that is junk but not private: the file, its sidecar and thumbnails (and with `--phone` its original on the phone), and remember its content so the same photo is skipped if it is imported again. No search for other copies. Never offered for a quarantined file. Command: `trash`. |
| **Erase** | Delete a private file for good with every copy the tool can reach: quarantine, the library, your whole home folder (your own photo folders included), thumbnails and caches, and the phone and backup drive when you name them. Ends with a check and a report of what it could not reach (cloud, chat apps, other devices). Asks you to type `ERASE`. Commands: `erase` (a batch's quarantine), `forget` (library files). |
| **Delete this copy** | Review tab only: delete just this file from the review folder. Nothing is remembered and no other copy is touched. |
| **DELETES** | In `media-intake --help` and a command's summary: this command removes files. Its `--help` has a `Deletes:` line saying what. |

## Suggestions and labels

| Word | Means |
| --- | --- |
| **Suggested junk** | Library files the tool thinks may be junk (a content label, the optional vision-model check, or a copy of something you trashed). Never a file you kept. The gallery shows them in its **Suggested junk** view; `suggest-trash` opens them on the review page's **Maybe trash** tab. Nothing happens until you Keep or Trash them. |
| **Junk score** | How sure the optional vision-model check is that a file is junk, from 0 to 1. |
| **Junk check** | The gallery filter for the vision model's verdict: Judged junk, Judged fine or Not checked. |
| **Possible child** | A content label for photos that may show a child. A hint only: it misses small or distant children. |
| **Looks like** | A content label (dog, beach, food, ...) from the local SigLIP model. |
| **Flags** | The gallery filter for Suggested junk, Possible child and Untagged. |
| **video proxy**, **converting** | The gallery's H.264 copy of a video this browser can't play (HEVC, most phones), made on this computer and kept in the cache. While one is made the large view says **Converting this video for the browser** and starts playing within seconds; `media-intake proxies` makes them ahead of time. |

The tool's own tag names (`Review|maybe-trash`, `Owner|keep`, `Label|food`)
appear only in sidecars and in the developer docs; the pages map them to the
words above in one place each (`WORDS` in `gallery.py`, the review page's
`plain()`).
