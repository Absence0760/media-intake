# People and pets recognition

`media-intake people` finds faces in your photos, groups the ones that look
alike and lets you name the groups. It never names anyone by itself: a face
that later joins a group you named is a **suggestion** until you confirm it,
and only names you gave or confirmed become tags (`People|<Name>` in the XMP
sidecar). Pets are planned on the same core (`media-intake pets`,
`Pets|<Name>`, cats and dogs by default; not in this version yet). Words: [words.md](words.md),
"People and pets". Design and model choices: [research-pets.md](research-pets.md).

Everything runs on this laptop's CPU. Nothing is downloaded at run time and no
photo, face or vector leaves the machine.

## Turning it on

It is off by default: every `people` command refuses with one line until its
models are installed.

```sh
bash setup.sh --recognition     # fetches YuNet + SFace (about 39 MB), sha256-checked
media-intake doctor             # "YuNet face detector" and "SFace face recogniser": ok
```

Both models come from OpenCV's model zoo at the commit pinned in
`media_intake/pins.json`; `setup.sh --recognition` checks their sha256 on
every run, `doctor` reports them (absent: optional; wrong checksum: BAD) and
`people scan` checks them again before loading. Licences:
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The commands run in the
venv `setup.sh` builds.

## Naming people

```sh
media-intake people scan              # library, videos and project folders; --limit N to go in parts
media-intake people cluster           # group faces that look alike
media-intake people clusters          # number, faces, files, name
media-intake people name 3 "Ann"      # your name for cluster 3
media-intake people merge 3 7         # cluster 7 is Ann too (kept across runs)
media-intake people ignore 5          # strangers, posters, statues: never tagged
media-intake people tag               # write People|Ann into the sidecars (--dry-run first)
```

Later, after new photos: `scan`, then `cluster`. Faces that join a cluster
you named are suggested, not named:

```sh
media-intake people suggestions       # detection id, cluster, suggested name
media-intake people confirm 3         # every suggestion in cluster 3 (or --det ID, or --all)
media-intake people reject --det 412  # not Ann; never suggested as Ann again
media-intake people tag
```

`take-out ID` takes one face out of a name for good (`put-back ID` undoes it);
removing a `People|` tag the tool wrote from a sidecar (in digiKam, say) does
the same, and `tag` never adds it back. `unname N` undoes `name` or `ignore`.
`tag` writes `People|<Name>` into `lr:HierarchicalSubject` and the leaf into
`dc:Subject`, and removes only tags it wrote itself; your own `People|` tags
are never touched. Cluster numbers change on every `cluster` run.

`find "Ann"` lists Ann's files, largest face first (`--alone`: no other face
in the file; `--min-box 0.02`: the face's share of the frame; `--suggested`:
suggestions count too). `people people` lists names with file counts and the
suggestions waiting.

Never scanned, and dropped from the store before every command: anything in
quarantine or staging, anything `scan` flagged, quarantined or erased,
anything trashed, and screenshots and screen recordings (`Screenshots/`,
`Screen recordings/` or a `Source|screenshot` tag). Videos are scanned only
once a frame source is installed; until then `scan` counts them as skipped.

How it decides: YuNet finds faces at two scales (long side 640 and 1600 px),
keeping those scored 0.9 or more and at least 48 px wide; SFace turns each
into a 128-number vector. `cluster` links faces whose cosine is at least 0.45
(stricter than SFace's 0.363 same-person threshold) and then merges groups
whose average pair passes 0.363. Two faces in one photo, or one video frame,
are never one person; two different names are never joined. These thresholds
are uncalibrated starting points.

## Checking how well it works

`media-intake people eval` reads only: tag some photos `Calibrate|people|<Name>`
in the gallery ("+ tag"; one face in the photo), then it prints, per vector
model, ROC AUC, the equal error rate, the median same-person cosine against
the closest different pair, nearest-neighbour accuracy, and at the link
threshold how many clusters each person splits into and how many clusters
mix two people. Numbers only; no image is shown or copied. (`calibrate`
counts these tags as naming no label and ignores them.)

## Your data: where it is and what removes it

Faces, vectors and names are biometric data. They stay on this laptop.

| What | Where | Kept |
| --- | --- | --- |
| Store: files (path, sha256, size, mtime), detections (box, score, kind, frame time), vectors, names, merges, rejections, the tags it wrote | `recognition.dir/recognition.db` (default `<state>/recognition/`; [configuration.md](configuration.md)) | folder `0700`, file `0600` |
| 160 px crops for the naming page | `recognition.dir/crops/`, made only when a page asks for one | `0600` |
| Names you confirmed | your sidecars, as `People\|<Name>` | as your sidecars |

- Never in git, never in a backup: `backup` never enters the state folder or
  `recognition.dir`, and the tool refuses a `recognition.dir` inside the
  library, videos or a project folder.
- `erase`, `forget`, `trash` and `sweep-erased --yes` delete the content's
  detections, vectors, file rows and crops, for people and pets; erase's
  coverage report counts them ("people and pets recognition data").
  `verify-gone` fails while any of them is left.
- To turn it off and drop everything: delete `recognition.dir`. The
  `People|` tags already written stay in your sidecars.

## For lane authors: the core

`media_intake/recog.py` holds everything the two commands share; its module
docstring documents the plug-in interface: a detector
(`detect(image) -> [(box, score, species[, crop])]`), an embedder
(`embed(crop) -> vector`), a `recog.Kind` naming the command, tag prefix,
thresholds and models, and frame sources for videos
(`frames.sample(path) -> iterator of (t_seconds, RGB array)`). The gallery
calls `clusters`, `suggestions`, `cluster_hints`, `confirm`, `reject`,
`name_cluster`, `merge_clusters`, `name_cluster(..., recog.IGNORED)`,
`take_out` and `crop_bytes` on a `recog.connect(threads=True)` connection.
