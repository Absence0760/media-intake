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
`Screen recordings/` or a `Source|screenshot` tag). Videos are scanned
through a few sampled frames each (Videos, below).

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

## Videos

`media_intake/frames.py` picks the frames of a video that recognition looks
at: one at each scene change, one every few seconds in between, never two
near-copies, so a static shot costs one frame and a busy one up to 48.

```python
import frames
with frames.sample(path) as s:      # max_frames=48, min_gap=1.0, every=4.0, scene=0.3
    for t, rgb in s:                # seconds from the start; numpy uint8 H x W x 3, upright
        ...
frames.info(path)                   # duration, fps, w, h, rotation, codec
```

```sh
python3 -I media_intake/frames.py CLIP --list   # the chosen timestamps only (prints no image data)
python3 -I media_intake/frames.py CLIP --info
```

**How it picks.** A first pass decodes the clip at 4 frames a second, 160 px
on the long side, and scores each frame against the one before it (half the
colour-histogram distance, half the mean pixel difference, 0 to 1). Over
`scene` (0.3) is a cut: that frame is taken. In a stretch without cuts a frame
is taken every `every` seconds (4) unless it barely differs from the last one
taken (mean difference under 2 %). Taken frames are at least `min_gap` (1 s)
apart; a cut just after a timed frame replaces it. The first frame that is
neither near-black nor near-white is always taken (a fade-in is skipped). Over
`max_frames` (48), the frames are spread evenly, the first kept. A clip over
15 minutes is looked at less often (3,600 analysis frames at most). Only then
are the chosen frames fetched, long side at most 1280 px: with ffmpeg by one
seek each when they are few for the clip's length, else by a second pass.
Near-copies of one person or pet across the chosen frames are skipped later,
by their vectors.

**Decoding.** ffmpeg when it has a decoder for the codec (Fedora's
ffmpeg-free decodes H.264 with OpenH264, not HEVC); else, for HEVC, the GPU
decoder thumbnails, scans and gallery proxies use (`vah265dec`, or Vulkan),
frames dropped to the analysis rate and scaled on the GPU. The rotation a
phone records is applied, so frames come out upright. Frames reach Python
through a pipe and are never written to disk; GStreamer reads the clip through
a link in a private (`0700`) work folder under `~/.cache/media-intake/`,
removed when the sampler closes, and is not tried when that folder's path has
characters other than letters, digits and `._/+-`. Every command is an argument
list (never a shell), each pass stops after 30 minutes, and closing the sampler
early (`close()`, leaving the `with`, or dropping it) kills and reaps the
decoder.

**Refused.** Anything under the state folder, and any file `scan` flagged,
quarantined or erased (`label.refused`), raises `frames.Refused` before
anything is decoded. A clip nothing can decode raises `frames.FramesError`.

Needs numpy (Fedora's `python3-numpy`, which the system Python has and the
venv reuses); no OpenCV.

**Cost (measured 2026-11-19, synthetic 1-minute clips with a cut every 20 s,
7 frames chosen each; Ryzen 9 5900HX, Radeon iGPU for VA-API, while the
gallery was transcoding a proxy on the same GPU):**

| Clip | Decoder | Analysis pass | Fetch | Total |
| --- | --- | --- | --- | --- |
| 1080p30 HEVC | GStreamer `vah265dec` | 3.9 s (457 decoded frames/s) | 3.3 s | 7.2 s |
| 4K60 HEVC | GStreamer `vah265dec` | 23.8 s (151 decoded frames/s) | 16.3 s | 40.1 s |
| 1080p30 H.264 | ffmpeg OpenH264 (CPU) | 8.6 s (209 decoded frames/s) | 6.9 s (7 seeks) | 15.5 s |

The time is the decoding: it grows with the clip's length and resolution, not
with how many frames are chosen. For about 2,000 phone videos averaging 30 s
(assumed: 60 % 1080p HEVC, 20 % 4K60 HEVC, 20 % 1080p H.264) that is about
2,000 x 7.7 s, roughly 4 hours of sampling, before detection and embedding.
All 1080p HEVC would be about 2 hours; all 4K60 about 11.
