# People and pets recognition

`media-intake people` finds faces in your photos, groups the ones that look
alike and lets you name the groups. It never names anyone by itself: a face
that later joins a group you named is a **suggestion** until you confirm it,
and only names you gave or confirmed become tags (`People|<Name>` in the XMP
sidecar). `media-intake pets` does the same for your own animals on the same
core (`Pets|<Name>`, cats and dogs by default; [Pets](#pets) below). Words:
[words.md](words.md), "People and pets". Design and model choices:
[research-pets.md](research-pets.md).

Everything runs on this laptop's CPU. Nothing is downloaded at run time and no
photo, face or vector leaves the machine.

## Turning it on

It is off by default: every `people` and `pets` command refuses with one line
until its models are installed.

```sh
bash setup.sh --recognition     # fetches YuNet + SFace (39 MB) and YOLOX + DINOv2 (60 MB), sha256-checked
media-intake doctor             # "YuNet face detector", "SFace face recogniser", "YOLOX animal
                                # detector" and "DINOv2 pet recogniser": ok
```

YuNet, SFace and YOLOX come from OpenCV's model zoo at the commit pinned in
`media_intake/pins.json`, DINOv2 from Hugging Face at the revision pinned
there; `setup.sh --recognition` checks their sha256 on every run, `doctor`
reports them (absent: optional; wrong checksum: BAD) and `people scan` /
`pets scan` check them again before loading. Licences:
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

## Pets

`media-intake pets` has the same commands as `people` (`scan`, `cluster`,
`clusters`, `name`, `merge`, `ignore`, `unname`, `suggestions`, `confirm`,
`reject`, `take-out`, `put-back`, `tag`, `find`, `eval`); `pets pets` lists
names with file counts. Tags are `Pets|<Name>`.

```sh
media-intake pets scan                # cats and dogs in the library, videos and project folders
media-intake pets cluster
media-intake pets clusters            # number, pets, files, species (cat or dog), name
media-intake pets name 2 "Rex"
media-intake pets merge 2 9           # the same dog lying down, running, from behind
media-intake pets tag --dry-run
media-intake pets tag
```

Expect one pet to start as several clusters: a body changes with pose far
more than a face does, so a dog lying down and the same dog running may not
link. Merge them once; the merge is kept. Ignore strangers' dogs, toys,
statues and pets on a TV. A kitten and the grown cat may need a merge too
(preferring photos close in date when growing a name is not built yet).

How it decides: YOLOX-s finds whole animals (from behind and the side too,
where a face would be hidden), keeping the classes in
`recognition.pet_classes` (default `cat`, `dog`; bird, horse and the other
COCO animals by config, [configuration.md](configuration.md)), scored 0.5 or
more and at least 96 px on the short side in the photo decoded at 1600 px.
Each animal is cut out with a 10 % margin and padded to a square (no tail or
ear cut off), and DINOv2 ViT-S/14 turns it into a 384-number vector. `cluster`
links animals whose cosine is at least 0.8 and merges groups whose average
pair passes 0.75. These are uncalibrated first guesses that lean towards
splitting (an extra cluster costs a merge; a wrong merge would tag the wrong
pet); `pets eval` measures them. A cat is never in a cluster with a dog, two
animals in one photo (or one video frame) are never one pet, and two
different names are never joined. Littermates or two black dogs may stay one
cluster: name it "the dogs", ignore it, or tag by hand.

Videos are scanned only once a frame source is installed (until then `pets
scan` counts them as skipped). A video gets `Pets|<Name>` only when that pet
is in at least `recognition.pet_min_frames` (default 3) of its sampled
frames, so one passing glimpse doesn't tag it.

A file with a person or pet you named (`People|<Name>`, `Pets|<Name>`) is
never suggested as junk and never judged by the vision check
([intake.md](intake.md), "Owner decisions").

## Checking how well it works

`media-intake people eval` reads only: tag some photos `Calibrate|people|<Name>`
in the gallery ("+ tag"; one face in the photo), then it prints, per vector
model, ROC AUC, the equal error rate, the median same-person cosine against
the closest different pair, nearest-neighbour accuracy, and at the link
threshold how many clusters each person splits into and how many clusters
mix two people. Numbers only; no image is shown or copied. (`calibrate`
counts these tags as naming no label and ignores them.)

`media-intake pets eval` reads only, too, and uses your own names as truth:
every pet in a cluster you named or a suggestion you confirmed, plus
`Pets|<Name>` and `Calibrate|pets|<Name>` tags on files with one pet in them
(a pet given two different names is left out). It compares the two vector
models `pets scan` stores side by side: DINOv2 (what `cluster` uses) and
SigLIP on the same crop (the model the tool already has, no extra download).
DINOv2 is scored at the link threshold (or `--link`), SigLIP at its own equal
error threshold, since its cosines sit on another scale. Names you gave to
DINOv2's clusters favour DINOv2 a little: for a fair comparison, tag a few
dozen photos per pet `Calibrate|pets|<Name>` by hand, spread over years and
poses. If SigLIP scores as well and mixes nothing, DINOv2 is not earning its
download; if DINOv2 mixes two of your pets at every usable threshold, the
stock model is not enough ([research-pets.md](research-pets.md), Evaluation
plan). At most 300 pets per name are scored (evenly spread), so it stays
quick on a large library.

## Your data: where it is and what removes it

Faces, vectors and names are biometric data. Pets' are treated the same:
the crops often show people too, and with the dates they describe your home
life. They stay on this laptop.

| What | Where | Kept |
| --- | --- | --- |
| Store: files (path, sha256, size, mtime), detections (box, score, kind, species, frame time), vectors (DINOv2 and SigLIP for pets), names, merges, rejections, the tags it wrote | `recognition.dir/recognition.db` (default `<state>/recognition/`; [configuration.md](configuration.md)) | folder `0700`, file `0600` |
| 160 px crops for the naming page | `recognition.dir/crops/`, made only when a page asks for one | `0600` |
| Names you confirmed | your sidecars, as `People\|<Name>` and `Pets\|<Name>` | as your sidecars |

- Never in git, never in a backup: `backup` never enters the state folder or
  `recognition.dir`, and the tool refuses a `recognition.dir` inside the
  library, videos or a project folder.
- `erase`, `forget`, `trash` and `sweep-erased --yes` delete the content's
  detections, vectors, file rows and crops, for people and pets; erase's
  coverage report counts them ("people and pets recognition data").
  `verify-gone` fails while any of them is left.
- To turn it off and drop everything: delete `recognition.dir`. The
  `People|` and `Pets|` tags already written stay in your sidecars.

## For lane authors: the core

`media_intake/recog.py` holds everything the two commands share; its module
docstring documents the plug-in interface: a detector
(`detect(image) -> [(box, score, species[, crop])]`), an embedder
(`embed(crop) -> vector`), a `recog.Kind` naming the command, tag prefix,
thresholds and models (optionally `min_frames` for videos and `eval_labels`,
eval's truth), and frame sources for videos
(`frames.sample(path) -> iterator of (t_seconds, RGB array)`). The gallery
calls `clusters`, `suggestions`, `cluster_hints`, `confirm`, `reject`,
`name_cluster`, `merge_clusters`, `name_cluster(..., recog.IGNORED)`,
`take_out` and `crop_bytes` on a `recog.connect(threads=True)` connection.
