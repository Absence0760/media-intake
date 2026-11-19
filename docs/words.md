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
| **Added in the last 30 days** | The gallery's view of files filed into the library in the last 30 days, whatever their date. Right after a first import it holds the whole library. Was "Recently added". |
| **offload** | Delete a batch's files from the source (phone, camera or source drive) once each has a checked copy in the library. Never for a laptop folder: you delete that yourself. |

## Deciding

| Word | Means |
| --- | --- |
| **review page** | `media-intake review BATCH`: a page on this computer only where you decide on what an import set aside. Its tabs are **Quarantine** (flagged files), **Review** (the review folder) and, with `--maybe-trash`, **Maybe trash** (Suggested junk). |
| **mark** | On the review page, and in the gallery's Suggested junk, a choice is only a mark until you apply it. Pressing a pressed button again clears it. **Mark all shown as Trash** marks every tile loaded, **Clear marks** takes every mark off; neither changes a file. |
| **drag to select** | In the gallery, drawing a rectangle with the mouse over the grid: every tile it touches is selected (in Suggested junk, from a Keep or Trash button: marked). Nothing moves; no file is dragged anywhere. |
| **Submit**, **Apply N decisions** | The review page's **Submit** opens the Apply dialog ("Apply 13 decisions?"); its **Apply N decisions** button runs every mark. When anything will be removed you type `ERASE` first. In the gallery's Suggested junk view, **Apply** does the same for its K / T marks. |
| **Favorite** | A photo or video you starred: the gallery's star on a tile or in the large view, **F**, or five stars set in digiKam (the sidecar's rating). The gallery's **Favorites** view lists them. A favorite is never Suggested junk. **Unfavorite** takes the star off. |
| **Keep** | Keep the file. In Quarantine: put it back in the library (`release`). In Review: leave it in the review folder for now. For Suggested junk: leave it in the library and never suggest it again (`not-trash`). In the gallery: mark it kept (the tile says "Kept"). |
| **Trash** | Delete a file that is junk but not private: the file, its sidecar and thumbnails (and with `--phone` its original on the phone), and remember its content so the same photo is skipped if it is imported again. No search for other copies. Never offered for a quarantined file. Command: `trash`. |
| **Erase** | Delete a private file for good with every copy the tool can reach: quarantine, the library, your whole home folder (your own photo folders included), thumbnails and caches, and the phone and backup drive when you name them. Ends with a check and a report of what it could not reach (cloud, chat apps, other devices). Asks you to type `ERASE`. Commands: `erase` (a batch's quarantine), `forget` (library files). |
| **Share** | In the gallery: make share-safe copies (no location or other hidden info) in a new folder under `~/Pictures/share-safe`, then open that folder so you can drag them into a chat or an email. Your originals and their tags don't change. Command: `share-copy`. |
| **Open folder** | After a Share: open its new folder in the desktop's file manager again. |
| **Delete this copy** | Review tab only: delete just this file from the review folder. Nothing is remembered and no other copy is touched. |
| **DELETES** | In `media-intake --help` and a command's summary: this command removes files. Its `--help` has a `Deletes:` line saying what. |

## Suggestions and labels

| Word | Means |
| --- | --- |
| **Suggested junk** | Library files the tool thinks may be junk: the optional vision check's verdicts (`vlm-trash`), files Tagged as junk, and copies of something you trashed. Never a file you kept or made a favorite, used in a post or in a protected project. The gallery shows them in its **Suggested junk** view, most likely first; `suggest-trash` opens them on the review page's **Maybe trash** tab. Nothing happens until you Keep or Trash them and Apply. |
| **Tagged as junk** | A file a content label marked as possible junk: one of the reasons a file is in Suggested junk (the gallery's Flags filter, its tile flag and Suggested junk's **Why**). Not the whole view, so its count is smaller. |
| **Why** | Suggested junk's reason filter: the vision check's reason (Blurry, Accidental shot, Too dark, Packaging, Random object, Junk screenshot, Near-copy, Other), Tagged as junk, or Copy of a trashed photo. A tile shows its reason after its score: "Junk score 0.90 (blurry)". |
| **Junk score** | How sure the optional vision check is that a file is junk, from 0 to 1, always shown to 2 decimals. Suggested junk lists verdicts of 0.70 or more by default (below that the model itself calls it low-confidence); its **Junk score** box goes lower, and **Show N more with a lower junk score** says how many that adds. |
| **Junk check** | The gallery filter for the vision model's verdict: Judged junk (its count follows the "Junk score at least" slider), Judged fine or Not checked. |
| **Possible child** | A content label for photos that may show a child. A hint only: it misses small or distant children. |
| **Dates to check** | The gallery's view of the files `check-dates` found with a date that looks wrong (a clock-reset default, a camera newer than its date, out of order with the camera's numbering, dates that disagree), grouped by reason. Nothing changes until you **Fix date**. |
| **suggested date** | The date `check-dates` thinks a file was really taken, and where it came from: its GPS time, a date in its name, a cloud export, or its numbered neighbours (the camera's files before and after it). |
| **Fix date** | Give library files a corrected date, in their sidecars only (the photo or video and its folder don't change): the suggested date, a date you type, or a shift (`+8y`). Undo puts it back. Commands: `fix-dates` (a dry run until `--yes`; `--undo`). |
| **clock-reset default** | A date a camera starts from when its clock was never set, such as a GoPro's 2016-01-01: files filed under it were taken some other time. |
| **Looks like** | A content label (dog, beach, food, ...) from the local SigLIP model. |
| **Flags** | The gallery filter for Tagged as junk, Possible child and Untagged. |
| **video proxy**, **converting** | The gallery's H.264 copy of a video this browser can't play (HEVC, most phones), made on this computer and kept in the cache. While one is made the large view says **Converting this video for the browser** and starts playing within seconds; `media-intake proxies` makes them ahead of time. |

## People and pets

Off until `bash setup.sh --recognition`; [recognition.md](recognition.md).

| Word | Means | Not to be confused with |
| --- | --- | --- |
| **People** | Recognising faces (`media-intake people`): the tool finds faces, groups the ones that look alike and you name the groups. A name you gave or confirmed goes into the sidecar as `People\|<Name>`. | Content labels ("Looks like"). |
| **Pets** | The same for your own animals (`media-intake pets`, planned; cats and dogs by default), tagged `Pets\|<Name>`. A cat is never in the same group as a dog. | Looks like: dog (a content label for any dog). |
| **cluster** | A group of faces (or pets) that look alike, numbered by size (1 = largest); the numbers change every time `cluster` runs. You **name**, **merge**, **ignore** or **unname** a cluster. | An event. |
| **suggestion** | A name the tool proposes for a face or pet that joined a cluster you named. Never a tag, and never counted by `find`, until you confirm it. | A name you gave. |
| **confirm**, **reject** | Your answer to a suggestion: confirm makes it your name (written as a tag by `tag`); reject drops it, and that name is never suggested for that face or pet again. | Take out (a name you gave, undone for one face or pet). |
| **take out** | "Not this one": one face or pet leaves its cluster's name for good (until **put back**). Removing a `People\|` or `Pets\|` tag the tool wrote from a sidecar does the same. | Reject. |
| **ignore** | A cluster of strangers, posters or statues: never named, never tagged. | Trash. |

## Events

| Word | Means | Not to be confused with |
| --- | --- | --- |
| **event** | A named group of photos and videos (a trip, a shoot), held in each file's sidecar as `Event\|<slug>`. **Set event…**, **Rename…** and **Merge into…** change it. | A batch. |
| **slug** | An event's name in the sidecars: lowercase letters, digits and dashes, like `summer-2026` (`plan --event`, Set event, Rename). Always in the tooltip and the event bar. | The display name. |
| **display name** | What the gallery shows for an event in the sidebar and the title: one you set with **Display name…**, else the slug made readable ("Summer 2026") when no other event looks like it, else the slug. Kept by the gallery in the state folder, never in a sidecar; two events never show alike. | The slug. |

## Places

| Word | Means | Not to be confused with |
| --- | --- | --- |
| **Place** | The city or town nearest a file's GPS (within 50 km), added by `tag` from offline GeoNames, like "Fernvale, Eastmarch, XA". The gallery's **Place** filter and Details row. | My places. |
| **My places**, **My place** | Places you name yourself (Home, The cabin): a name, a point and a radius, kept only in the state folder's `places.json`. A file whose GPS is inside one gets it as a tag beside its Place. `media-intake places`, the gallery's **Places** list and **Add a place here…** ([places.md](places.md)). | Place (the city). |
| **Probably here** | The gallery's guess for a file without GPS: a photo from the same camera, close in time, was in one of your places. Only a suggestion: **Yes** tags it, **No** stops it being suggested again. | |

The tool's own tag names (`Review|maybe-trash`, `Owner|keep`, `Label|food`)
appear only in sidecars and in the developer docs; the pages map them to the
words above in one place each (`WORDS` in `gallery.py`, the review page's
`plain()`).
