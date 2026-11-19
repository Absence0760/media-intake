# My places

Tagging already names the nearest city or town within 50 km of a file's GPS
(`Place|<city>, <state>, <CC>`, offline GeoNames: [intake.md](intake.md),
"Place names, offline"). **My places** adds places you name yourself, like
Home or The cabin: a name, a point and a radius. A photo or video whose GPS is
inside the circle gets `Place|<name>` in its sidecar, next to the city tag,
which stays.

## The places file is private

Your places say where you live. They are kept in one file only:
`<state>/places.json` in the state folder (`0600`, written atomically: a temp
file, then a rename; one entry per library, keyed by a hash of its folders, like
`event-names.json`). They are never written to `config.toml`, a repo, a log or
any file the tool shares:

- **The terminal** shows names and counts, never a coordinate, unless you
  pass `--show-coords` (`places list`, `places suggest`).
- **The gallery page** never gets a coordinate: "Add a place here…" sends the
  item's id and the server uses that file's GPS.
- **Share** copies (`share-copy`, the gallery's Share) carry no place name and
  no GPS: the sidecar is never copied and the copy's own metadata is stripped
  ([gallery.md](gallery.md), Share).
- **Backups** don't include it: `backup` never copies the state folder
  ([backup.md](backup.md)). There is no option to back it up. To keep your
  places when you rebuild the machine, copy `places.json` yourself, somewhere
  as private as the state folder; without it, `places add` and `places tag`
  put them back, and the `Place|<name>` tags in the sidecars (which the
  backup does copy) are still there.
- **Erase, forget and trash** delete a file's sidecar, so its `Place|<name>`
  tag goes with it, and they drop the file's records from `places.json`.
  `erase` also drops records of any file gone from a library folder that is
  plugged in, and its coverage report counts them ("your places' records:
  erased files dropped"). `verify-gone` fails with "PLACE RECORDS: N" when
  `places.json` still names a file that is gone (a file deleted by hand);
  `places tag` drops those records. A library folder counts as there only when it exists, is not empty and, under
  /run/media, /media, /mnt or /Volumes, sits on a mounted drive (findmnt): an
  empty mountpoint left by an unplugged drive drops nothing.

`places.json` holds, per place, its name, point and radius, and which files
the tool tagged with it or must never tag with it again (paths: you took the
tag off, or said No to a "probably here" suggestion). Treat it like the rest
of the state folder: never sync, share or index it.

## Commands

```sh
media-intake places add Home --from ~/Pictures/library/2026/2026-11/IMG_1.jpg --radius 150
media-intake places add Home --at 12.3456,-65.4321          # decimal degrees
media-intake places suggest                                  # dense spots, counts only
media-intake places add Home --suggestion 1                  # the first spot suggest lists
media-intake places tag --dry-run                            # what would change
media-intake places tag                                      # tag every library file
media-intake places list                                     # names, radius, counts
media-intake places rename Home "Old flat"
media-intake places remove "Old flat"
```

- `add NAME` takes the point from a library photo's GPS (`--from FILE`, its
  sidecar's GPS, else the file's own), from `--at LAT,LON`, or from a
  suggestion (`--suggestion N`). `--radius` is 10 to 20000 metres, default
  150. It saves the place and says how many files are inside; `places tag`
  tags them.
- `tag` brings every library sidecar in step with your places: it adds the
  tag where a file is inside a place, takes the tool's own tag off a file
  that is no longer inside (a place edited by hand), and keeps every tag you
  removed off. Files scan flagged, quarantined or erased are never touched.
- `rename OLD NEW` rewrites `Place|OLD` to `Place|NEW` on the files the tool
  tagged (a file you took it off stays without it).
- `remove NAME` takes its tag off the files the tool tagged and forgets the
  place. The city tags stay.
- `rename` and `remove` are refused while a file tagged with the place can't
  be read (its drive not plugged in, an unreadable sidecar): its tag would be
  left behind with nothing to clean it up. Plug the drive in and run it
  again, or add `--anyway` to leave those files' tags as they are.
- `suggest` lists spots where many photos were taken (at least 10 files within
  150 m, `--min N` to change it, outside your places), biggest first, each with
  how many were taken in the evening (18:00 to 05:59) and the nearest city on
  record: "1. 240 files within 150 m, 180 in the evening, near Fernvale". It
  never names a spot; you do, with `add NAME --suggestion N`.
- Each command that writes takes the library lock, like `tag`.

New imports get their places with every other tag: `tag BATCH` and
`tag-files FILE...` add `Place|<name>` for files inside a place, and print
only a count ("your place tags added 3").

## Names

1 to 64 visible characters on one line (runs of spaces kept as one), no
control or invisible format characters (zero-width, bidi marks), no `|` (a
tag level) and no `,` (a city tag always has one: that is how the two kinds
are told apart), not starting with `-`. A name that looks like another
place's (the same but for case, width, spacing, or a dash or underscore for a
space) is refused.

## Rules

- **Only its own tags.** The tool adds and removes `Place|<name>` only for a
  place defined in `places.json`, and removes only a tag it wrote. `tag` and
  `tag-files` replace the city tag as before but never touch a place you
  named ([intake.md](intake.md), "Tags: who writes what").
- **A tag you removed stays removed.** When a file the tool tagged no longer
  has the tag (you took it off in digiKam or by hand), the file is recorded as
  declined for that place and never tagged with it again, by any command. A
  sidecar that can't be read (none yet; empty, not XMP or cut off part-way;
  or exiftool failed) is never taken as a removal, and nothing is written
  into it.
- **Inside** means the great-circle distance (haversine, mean Earth radius
  6371008.8 m) from the point is at most the radius. It holds across the
  180th meridian and at the poles.
- **A file with no GPS** is never tagged by distance. In the gallery it can
  get a "probably here" suggestion (below); a Yes tags it, and that tag then
  behaves like any other the tool wrote.

## In the gallery

- **Places** (sidebar, below Events): every place with how many items carry
  its tag. A click shows those items (the My place filter; its name is the
  view's title). With no places yet it says how to add one.
- **Add a place here…** (the large view's tools, only on an item with GPS):
  asks for a name and a radius (50 m to 2 km, default 150 m), then
  `POST /api/place/add` (`{"id": item, "name": text, "radius": metres}`) saves
  the place at that item's point and tags every item inside it, under the
  library lock. Refused: an item without GPS, a bad or look-alike name, a
  radius out of range, an unreadable `places.json` (409).
- **Probably here**: an item without GPS, from the same source (its `Source|`
  tags: kind and camera model) and taken within 30 minutes of an item inside
  one of your places, shows "Probably at Home: a photo from the same camera
  12 min apart was there." with **Yes, Home** and **No**
  (`POST /api/place/probable`, `{"id": item, "place": id, "yes": bool}`).
  Nothing is written until you answer: Yes adds the tag; No records it, and
  that place is never suggested for that item again.
- The large view's Details show **My place** beside **Place** (the city).
  Every name is put on the page as text, never as markup.

## Tests

`python3 -I -m unittest discover -s tests -p test_places.py`: geometry (the
radius boundary, the antimeridian, the poles), names, `places.json` (0600, no
temp file left, per library, an unreadable file never overwritten), tag /
untag / rename / remove, a removed tag never added back (by `places tag` and
by `tag-files`), suggest's spots on synthetic GPS (and nothing but counts in
the terminal), "probably here", share-copy carrying no place, and erase,
forget, trash and verify-gone covering the records; the gallery's Places API.
`tests/browser/test_browser_gallery_places.py` drives the Places section,
Add a place here… and the "probably here" answer in Chromium. Every point in
the tests and these docs is made up.
