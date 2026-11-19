# Research: recognising pets, and bringing people tags in

Status: **decided, not built**. Researched 2026-11-19; the owner accepted the
recommendations the same day ("Decisions" below). Nothing here changes the tool
yet.

Goal: tag each of a household's own pets by name (`Pets|<Name>`), the same way a
face pipeline tags people (`People|<Name>`), and move that face pipeline into
media-intake so both share one scan, one naming page, one erase path. Fully
offline, CPU-only by default, models downloaded only by `setup.sh` and pinned by
sha256, off unless the owner turns it on.

## Recommendation in short

- **Find animals** with YOLOX-s from OpenCV's model zoo (Apache-2.0, 36 MB,
  runs in the `cv2` wheel already installed). COCO classes `cat` and `dog` by
  default (bird, horse and the rest by config). Whole-body boxes, not faces:
  pets are photographed from behind and from the side far more than people.
- **Tell pets apart** with DINOv2 ViT-S/14 (Meta, Apache-2.0), starting with the
  int8 ONNX file (24 MB; the 88 MB fp32 file only if the owner's `pets eval`
  shows confusion), run in the `onnxruntime` wheel already installed: a 384-d
  vector per animal crop. SigLIP (already installed) on the same crops is kept as the zero-download
  baseline that the evaluation compares against.
- **Group and name** exactly like the face prototype: union-find clustering with
  "two animals in one photo are never the same pet" and "a cat is never a dog",
  the owner names clusters, later photos that join a named cluster are
  **suggestions until the owner confirms them**, tags go to the sidecar only.
  Nothing is named without the owner.
- **Port the face prototype** (YuNet + SFace, already pinned there) into one
  shared `identities` core with two kinds, `person` and `pet`; same state folder
  rules (0700/0600, never backed up), and erase / forget / trash / make-private /
  verify-gone cover both. Two commands, `media-intake people …` and
  `media-intake pets …`, on that one core.
- Videos: scene-change frame sampling on the GPU decode path, linked across
  frames (see "Videos"); it doubles as the roadmap's per-frame video index.
- No torch, no new Python wheel. New downloads: 60 MB for pets, 39 MB for faces.

## Decisions (owner, 2026-11-19)

1. **No automatic naming.** A new face or pet that joins a named cluster is a
   suggestion until the owner confirms it; only confirmed ones are tagged. The
   ported faces code changes accordingly (the prototype's `how=auto`
   propagation becomes `how=suggested`), matching the roadmap's "never names
   faces automatically".
2. **Tags:** `Pets|<Name>` (plural, matching `People|<Name>`).
3. **No fallback model trained on research-only or scraped data** (so not the
   AvitoTech fine-tune, nor anything derived from PetFace).
4. **Classes:** cats and dogs by default; other COCO animals (bird, horse,
   sheep, cow, …) opt-in by config.
5. **Commands:** `media-intake people …` and `media-intake pets …`, two
   commands on one shared core.
6. **Model size:** start with the int8 DINOv2 (24 MB); move to fp32 only if
   the owner's `pets eval` shows confusion.

## Decision table

| Job | Choice | Pin | Why |
|---|---|---|---|
| Animal detection | YOLOX-s `object_detection_yolox_2022nov.onnx` ([opencv_zoo](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/object_detection_yolox)), Apache-2.0 (its `LICENSE`; upstream [Megvii YOLOX](https://github.com/Megvii-BaseDetection/YOLOX), Apache-2.0) | opencv_zoo commit `48b10fa505aeb51fbd127fc60a5c660040e823b7` (main, 2026-10-20; file last changed 2022-11-13), 35,858,002 bytes, sha256 `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` | `cv2.dnn`, already in the venv: no new wheel. COCO val per-class AP (0.50:0.95, the zoo README): cat 66.4, dog 60.1, horse 58.1, bird 33.3. Same source and download pattern as the face models. |
| Pet embedding | DINOv2 ViT-S/14 ([facebook/dinov2-small](https://huggingface.co/facebook/dinov2-small), Apache-2.0; code [facebookresearch/dinov2](https://github.com/facebookresearch/dinov2), Apache-2.0), ONNX export [Xenova/dinov2-small](https://huggingface.co/Xenova/dinov2-small), **int8** `onnx/model_quantized.onnx` (fp32 `onnx/model.onnx` only if `pets eval` shows confusion) | revision `c2bb04a51fab207c420665f1946016107bffc701` (2025-07-10). int8: 24,451,943 bytes, sha256 `3afdc8bc63b50558d6e5770f5b799bb82455c2311183a2de43803f343a29d917`. fp32 (escalation): 88,459,888 bytes, sha256 `83141175ec78b4ff9a2bb58a4c7c264ba0054d1c2e122e5a8114b79a8d4179ea` | `onnxruntime`, already in the venv. Self-supervised features keep instance detail (coat pattern, markings) that text-aligned models like CLIP/SigLIP partly throw away; in the WildlifeDatasets benchmark (below) zero-shot DINOv2 beat CLIP on most of 29 animal re-ID datasets. Same exporter as the SigLIP files already pinned. |
| Baseline embedding (evaluation only) | SigLIP vision encoder already pinned in `media_intake/pins.json` (`onnx/vision_model_quantized.onnx`) | as pinned today | Zero new download; run on the same crops so the owner's evaluation shows whether DINOv2 earns its download. |
| Face detection (port) | YuNet `face_detection_yunet_2023mar.onnx` (opencv_zoo), MIT | same commit, 232,589 bytes, sha256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | `cv2.FaceDetectorYN`; present in the pinned `opencv-python-headless==5.0.0.93` (checked, see "Measured"). |
| Face embedding (port) | SFace `face_recognition_sface_2021dec.onnx` (opencv_zoo), Apache-2.0 | same commit, 38,696,353 bytes, sha256 `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | `cv2.FaceRecognizerSF`, same wheel; 128-d, LFW 99.60 % at cosine 0.363 (OpenCV's dnn_face tutorial). |
| Clustering, naming, tags | Shared numpy union-find core (from the face prototype) | no dependency | Constraints, owner names, propagation, `written` table for tags the tool may remove: one implementation for both kinds. |

Every sha256 above equals the Git LFS pointer's `oid` (opencv_zoo) or the
Hugging Face tree API's `lfs.oid` at that revision, and the downloaded file in
the scratch test matched it. Licences were read from each repository's `LICENSE`
file or model card on 2026-11-19.

## Animal detection: candidates

| Candidate | Licence | Size | Verdict |
|---|---|---|---|
| YOLOX-s (opencv_zoo) | Apache-2.0 | 35.9 MB | **Chosen.** Best COCO cat/dog AP of the permissive small ONNX models found; 86 ms an image here. |
| NanoDet-Plus-m 1.5x (opencv_zoo `object_detection_nanodet_2022nov.onnx`, 3,800,954 bytes, sha256 `4b82da99…720186`) | Apache-2.0 | 3.8 MB | Runner-up: 3x faster (28 ms) but dog AP 51.4 vs 60.1 and smaller boxes found less often (AP small 10.7 vs 23.2). Keep as a `pets.detector: fast` option if scan time matters. Note: the export has three strides (8/16/32), not the four the zoo wrapper lists. |
| Ultralytics YOLOv8/11 | AGPL-3.0, with a paid "Enterprise" licence for closed use | 6-50 MB | Rejected. Licence-compatible with this AGPL project, but weights come from Ultralytics' release assets, the export path is their torch package, and anyone reusing this code commercially inherits their licensing terms. YOLOX gives comparable cat/dog accuracy without that. |
| RT-DETR r18 ([PekingU/rtdetr_r18vd](https://huggingface.co/PekingU/rtdetr_r18vd), [lyuwenyu/RT-DETR](https://github.com/lyuwenyu/RT-DETR)) | Apache-2.0 | 82.6 MB fp32 (21.7 MB int8, onnx-community) | Rejected for now: more accurate overall, but 2.3x the download (fp32) and, as a transformer decoder, probably slower on CPU than YOLOX-s (not measured). Revisit if YOLOX misses too many pets in the evaluation. |
| OWLv2 base ([google/owlv2-base-patch16-ensemble](https://huggingface.co/google/owlv2-base-patch16-ensemble)) | Apache-2.0 | 614 MB fp32 ONNX | Rejected: open-vocabulary ("a hamster") is nice, but 960 px input and 17x the download; seconds an image on CPU. |
| Grounding DINO tiny ([IDEA-Research](https://huggingface.co/IDEA-Research/grounding-dino-tiny)) | Apache-2.0 | 719 MB fp32 ONNX | Rejected: same reasons as OWLv2, plus a text tokenizer at run time. |
| MegaDetector ([agentmorris/MegaDetector](https://github.com/agentmorris/MegaDetector)) | MIT (code) | not checked | Rejected: only `animal / person / vehicle` (no cat vs dog), trained on camera-trap images, and v5 is an Ultralytics YOLOv5 model run through torch. |
| YuNet, NudeNet, SigLIP (already installed) | – | – | Can't do it: YuNet finds human faces only, NudeNet finds body parts, SigLIP gives one vector per image with no boxes. SigLIP can still say "this photo probably shows a dog" (see the scan pre-filter below). |
| Dedicated dog/cat **face** detectors | – | – | None found that is maintained, permissively licensed and shipped as ONNX. Not needed: body crops work when the head is turned away, which faces don't. |

## Telling individual pets apart: candidates

| Candidate | Licence | Weights | Evidence | Verdict |
|---|---|---|---|---|
| DINOv2 ViT-S/14 | Apache-2.0 (weights and code) | ONNX ready (Xenova, onnx-community), 88 MB fp32 / 24 MB int8 | WildlifeDatasets, Table 4 (zero-shot, ViT-L): above CLIP on most of 29 animal datasets. AvitoTech's pet benchmark (fine-tuned ViT-S, below): DogFaceNet top-1 0.558, cats top-1 0.855. | **Chosen.** |
| SigLIP base (already installed) | Apache-2.0 | already pinned | No zero-shot pet re-ID numbers found. Fine-tuned SigLIP-Base and DINOv2-Small score about the same in the AvitoTech paper, but that says nothing about the off-the-shelf model here. | Evaluation baseline. |
| MegaDescriptor ([BVRA/MegaDescriptor-L-384](https://huggingface.co/BVRA/MegaDescriptor-L-384), T/B/L) | **CC-BY-NC-4.0** (model card tag) | timm/torch safetensors | Best zero-shot wildlife re-ID ([arXiv 2311.09118](https://arxiv.org/abs/2311.09118)); on DogFaceNet only top-1 0.461 per the AvitoTech paper (Table 26). | Rejected: non-commercial, and the owner's photos may be used for business. |
| MiewID-msv3 ([conservationxlabs/miewid-msv3](https://huggingface.co/conservationxlabs/miewid-msv3)) | **none stated** (card has no licence; [code repo](https://github.com/WildMeOrg/wbia-plugin-miew-id) has no LICENSE file) | torch, `trust_remote_code`, 440 px, 24 GMACs | DogFaceNet top-1 0.557, cats 0.872 (AvitoTech paper, zero-shot) | Rejected: no licence means no permission to redistribute or use; heavy; needs remote code. |
| PetFace models ([mapooon/PetFace](https://github.com/mapooon/PetFace), ECCV 2024) | README: dataset, code and pretrained models "for non-commercial research purpose only" | torch | Face-aligned ArcFace models on 257k animal identities | Rejected: non-commercial; needs a pet-face aligner. |
| AvitoTech fine-tuned DINOv2-small ([AvitoTech/DINO-v2-small-for-animal-identification](https://huggingface.co/AvitoTech/DINO-v2-small-for-animal-identification), rev `a3ea88f20537d9c605da65894243ab68469db1bd`, `model.safetensors` 88,249,960 bytes, sha256 `059fb5e9…554a1be`) | Apache-2.0 on the card | safetensors only (an ONNX export would be ours to make, with torch, once) | Kudryavtsev et al., *J. Imaging* 12(1):30, 2026 ([arXiv 2603.02270](https://arxiv.org/abs/2603.02270)): cats top-1 0.855, DogFaceNet 0.558, ROC AUC 0.98-0.99, on 3,001 test animals | **Rejected (owner, 2026-11-19).** Purpose-built for cats and dogs, same size and runtime as the chosen model, but trained on PetFace (research-only terms) and scraped listings, so the Apache-2.0 label may not cover how the weights were made. |
| DINOv3 ViT-S/16 ([facebook/dinov3-vits16-pretrain-lvd1689m](https://huggingface.co/facebook/dinov3-vits16-pretrain-lvd1689m)) | **DINOv3 License** (custom: commercial use allowed, but gated download, an indemnity clause, Meta may amend it "effective immediately") | gated on Hugging Face | Newer, likely stronger | Rejected: `setup.sh` can't fetch a gated file without a token, and the terms are not a standard open licence. |
| CLIP ViT-L | MIT | – | Below DINOv2 on most WildlifeDatasets sets | Rejected: SigLIP already covers the "text-aligned" baseline. |

Reported numbers, for scale. They are from large galleries (hundreds to
thousands of animals) and are not what a household sees: with 1-5 pets the job
is mostly verification (same pet or not), where ROC AUC around 0.98 is the more
relevant figure, and the owner confirms every cluster anyway.

## Can the SigLIP vectors already stored do it?

**Not on their own.** `semantic.py` stores one vector per photo (and one
averaged vector per video), made from the whole frame. That vector is mostly
the scene: a dog on the sofa is closer to a cat on the sofa than to the same dog
on the beach. It can't separate two pets in one photo and can't say where a pet
is. `media-intake similar` already offers "photos like this one", which is the
most these vectors can do.

**SigLIP on crops** needs a detector anyway (so not zero new models), and then
becomes a fair candidate. On the one public sample tested (two tabby cats side by
side, COCO val2017 image 39769), SigLIP put the two different cats at cosine
0.927 against 0.987 for a cat and its own mirror image; DINOv2 gave 0.648 against
0.951. One pair proves nothing, but it matches the expectation that a
text-aligned model maps "a tabby cat" to nearly one point. A household with a
black dog and a ginger cat may be fine on SigLIP; two littermates probably not.
The evaluation below settles it on the owner's own pets.

**Still useful as a pre-filter:** the stored vector, scored against "a photo of
a dog / a cat" with the existing label machinery, can order or skip photos
before detection. Not the default: a false "no animal" would hide a pet.

## Measured (scratch test, 2026-11-19)

All on this laptop's CPU (Ryzen 9 5900HX, 4 threads), in a throwaway venv with
the repo's own pins (`onnxruntime==1.31.1`, `opencv-python-headless==5.0.0.93`).
Inputs: a synthetic 4000x3000 noise image and one public COCO sample
(val2017 `000000039769.jpg`, two cats). No library photo was used.

| Step | Result |
|---|---|
| YOLOX-s, 12 MP image (resize to 640 included) | 86 ms; on the COCO sample both cats found, scores 0.93 and 0.92 |
| NanoDet, same | 28 ms; both cats found, scores 0.61 and 0.52 |
| DINOv2-S fp32, one 224 px crop | 33 ms (int8: 25 ms, cosines within 0.03 of fp32) |
| SigLIP vision int8, one 224 px crop | 61 ms |
| Peak RSS, YOLOX + DINOv2 loaded and run | 304 MB |
| cv2 5.0 face APIs | `FaceDetectorYN.create`, `FaceRecognizerSF.create`, `FaceRecognizerSF_FR_COSINE` all present, same argument order as 4.x |

Not measured: real JPEG/HEIC decode (synthetic JPEG at half scale took 14 ms;
real phone photos decode slower, HEIC much slower), accuracy on any real pet.

## Proposed pipeline

Mirrors the face prototype step for step; `media-intake people …` and
`media-intake pets …` (decision 5), one shared core.

1. **scan** (incremental by path + size + mtime, same content at another path
   reuses its rows). One decode per photo feeds both kinds: YuNet + SFace for
   faces, YOLOX for animals. Keep boxes with class in `pets.classes` (default
   `cat`, `dog`), score >= 0.5 (uncalibrated), short side >= 96 px in the
   decoded image (DINOv2 reads 224 px; smaller crops give weak vectors).
   `pets.classes` holds COCO class names; other animals are opt-in. Crop
   with 10 % margin, pad to square (no centre crop: it cuts tails and ears),
   DINOv2 CLS token, L2-normalised. Skipped exactly as faces: `label.refused()`
   (quarantine, staging, private store, anything flagged, erased or kept
   private), screenshots; videos not in the first version.
2. **cluster**: the face prototype's two stages with its constraints, plus one
   more: two animals in one photo are never one pet; a `cat` box never joins a
   `dog` cluster; owner names and "ignored" never merge. Link and merge
   thresholds for DINOv2 are unknown and come from the evaluation; until then
   they lean to splitting (an extra cluster costs one click, a wrong merge tags
   the wrong pet).
3. **name / merge / ignore / take out** on the naming page or the CLI. "Ignore"
   covers strangers' dogs, toys, statues, pets on a TV or a poster.
4. **grow**: later crops that cluster with exactly one named pet are shown as
   suggestions for that name (`how=suggested`); the owner confirms them in bulk
   on the page ("these 40 are Max") or one by one, and only confirmed crops are
   tagged. Faces work the same way after the port.
5. **tag**: `Pets|<Name>` (confirmed crops only) into `lr:HierarchicalSubject` (leaf in `dc:Subject`),
   with the face prototype's rules: remove only tags it wrote, never re-add one
   the owner deleted from a sidecar.
6. **find**: `pets find "Name" [--alone] [--min-box 0.05]`, and `find --tag
   'Pets|Name'` on sidecars.

What differs from faces:

- **Body, not face.** Pose changes the crop far more than for a face; expect a
  pet to start as several clusters (lying down, running, from behind) that the
  owner merges. The page should suggest merges ("looks like Max", average cosine
  above the merge threshold) the way it suggests names for faces.
- **Growing up.** A kitten and the adult cat may never link directly. Union-find
  chains help when the library has photos in between; otherwise the owner merges
  once. Optional later: prefer neighbours close in date when propagating.
- **Several pets per photo** is common (and pets with people): one photo can
  carry `Pets|A`, `Pets|B` and `People|C`.
- **Look-alikes** (littermates, two black labs): the case the evaluation must
  test; if DINOv2 can't separate them, they stay one cluster the owner can
  ignore or name "the dogs", and tags fall back to manual.

## Data handling

- **Is it personal data?** An animal's appearance is not personal data in
  itself, but these files are tied to one household, the crops often contain
  people (hands, laps, children holding a pet), and the boxes and timestamps
  together describe the owner's home life. Treat pet data exactly like face data.
- **Where:** one folder `<state>/identities/` (0700) with `identities.db`
  (0600: files, boxes, vectors, names, merges, `written`, all with a `kind`
  column) and `crops/` (0600, made on demand for the naming page only). Outside
  every repo and library root.
- **Backups:** `backup` already never enters the state folder
  (`media_intake/backup.py`); add a test that a backup run leaves no
  `identities.db` or crop on the drive, as the prototype has.
- **Erase, forget, trash, keep-private, make-private** drop every row and crop
  of that content for both kinds (one `identities.forget(sha)`, called from the
  same place as `gallery_thumb_forget`). Every identities command first drops
  rows of content flagged, made private or deleted since.
- **verify-gone** fails if any identities row or crop for an erased sha
  remains, for either kind.
- **Turning it off:** a `media-intake identities purge` (or `people purge` /
  `pets purge`) that deletes the folder and lists, but does not remove, the
  `People|`/`Pets|` tags it wrote (removing them is a separate `untag --yes`).
- Only names leave the folder, and only into sidecars.

## Porting the face prototype

The prototype (a sibling tool's `faces.py`, about 890 lines, and its naming page,
about 335) was written against the same module layout this repo grew from
(`intake`, `label`, `gallery`, `review_ui`), so most of it moves as is.

Moves: scan, the two-stage clustering, name / merge / ignore / unname / take
out, tag with the `written` table, find, people, clusters, suggestions, crops,
the naming page and its tests (synthetic, mocked detector).

Must change:

- **Generic code.** No default library path, screenshot folder or tag names
  baked in beyond the existing config keys: roots come from `config.get()`
  (`library`, `videos`, project folders), the state folder from config (no
  separate env var, or one documented in `docs/configuration.md`). Tag
  namespaces `People|` and `Pets|` documented in `docs/words.md`.
- **Off by default.** `identities.people: false`, `identities.pets: false` in
  config; commands refuse with a one-line hint when off. `setup.sh` downloads the
  four models only when asked (`--with-people`, `--with-pets`), pins live in
  `media_intake/pins.json` (regenerated by `scripts/update-pins.py`), each
  model's sha256 is re-checked on every run, and `media-intake doctor` reports
  them as optional.
- **Entry points** through `media_intake/cli.py` (venv scripts like `index`),
  one module per concern: `identities.py` (store, clustering, naming, tags),
  `people.py` and `pets.py` (detector + embedder per kind), the page in the
  gallery server rather than a second web server (it already has the token,
  CSRF and CSP handling).
- **OpenCV 5.0** instead of 4.14: the face APIs exist with the same signatures
  (checked); the detector wrappers need their own output handling (NanoDet's
  output order and stride count differ from the zoo wrapper, as found above).
- **No automatic naming** (decision 1): the prototype's propagation
  (`how=auto`, tagged at once) becomes `how=suggested`, shown on the page with a
  Confirm action, and `tag` writes only owner-named or owner-confirmed faces.
  A face suggested for a name the owner later removes or renames drops its
  suggestion.
- **Erase/verify-gone/backup hooks** as in "Data handling", with tests.
- **Docs and notices:** a section in `docs/intake.md` "Chosen tools", rows in
  `THIRD_PARTY_NOTICES.md` (YOLOX, DINOv2, YuNet, SFace, all downloaded by
  `setup.sh`), `docs/gallery.md` for the new sidebar section.

## Gallery: "People & Pets"

A sidebar section listing named people and pets with photo counts; a click
filters the grid (`People|Name` / `Pets|Name`, combinable with every other
filter). An unnamed-clusters view replaces the separate naming page: a grid of
crops per cluster with Name, Merge, Ignore, and a click on a crop to take it out.
Crops are served by id only, from the identities folder, never cached in the
browser (`no-store`). Shown only when the kind is turned on.

## Evaluation plan (the owner runs it later; nothing needs user data now)

Now, in CI: synthetic tests only. Unit tests feed made-up vectors (two or three
tight Gaussian blobs on the unit sphere, a drifting blob for "growing up", two
overlapping blobs for look-alikes) into the clustering, and check the
constraints (same photo, cat vs dog, owner names) hold. Detector and embedder
are mocked, as in the face tests.

Later, by the owner, on the laptop:

1. Turn pets on and run `pets scan` (detections and both vectors: DINOv2 and
   SigLIP crop vectors are stored side by side for the evaluation only).
2. In the gallery, tag 20-50 photos per pet by hand with `Calibrate|pet|<Name>`,
   spread over years and poses, plus 20 photos with other animals (`Calibrate|pet|other`).
3. `media-intake pets eval` (read-only, like `calibrate`) prints, per embedder:
   ROC AUC and equal error rate over every same-pet / different-pet pair of
   labelled crops; the closest different-pet pair's cosine against the median
   same-pet cosine; leave-one-out nearest-neighbour accuracy; and, at the
   proposed link threshold, how many clusters each pet splits into and whether
   any cluster mixes two pets. Counts and numbers only; no image is shown or
   copied.
4. Decision rules: pick the threshold where no cluster mixes two labelled pets;
   if SigLIP-crop matches DINOv2 within 0.01 AUC and mixes nothing, the owner may
   drop DINOv2 (`pets.embedder: siglip`, 24 MB saved); if int8 DINOv2 mixes two
   pets at any usable threshold, rerun with fp32 (`pets.precision: fp32`,
   +88 MB) and compare. No research-only or scraped-data model is a fallback
   (decision 3); if fp32 also confuses two pets, they stay a cluster the owner
   tags by hand.

## Videos

Research only; nothing built. One pass serves pets, people and the roadmap's
"search across video frames by the best-matching frame" item: the same sampled
frames give per-frame SigLIP vectors for that index.

**Sampling.** Decode on the GPU through the path thumbnails, scans and gallery
proxies already use (`scan_sensitive._gst_hevc_decoder()`: `vah265dec`, else a
Vulkan decoder; `vah264dec` for H.264 when present; ffmpeg for what neither
takes), scaled on the GPU (`vapostproc`) to 960x540, dropped to 2 frames a
second, piped to Python as raw BGR. Nothing is written to disk: the only file is
the 0700 work folder's link to the clip, as today. Keep a frame when its 32x32
grey thumbnail differs from the last kept one by more than a threshold (a scene
change), or when 5 s passed without one; cap at 30-60 frames a video, spread
evenly when there are more.

**Skipping near-duplicates.** Within a track (below), a crop whose vector is
within cosine 0.97 of the track's last stored vector is not stored; it only
adds a timestamp. Static shots cost one vector, not forty.

**Temporal linking.** Boxes of the same class in consecutive kept frames with
IoU > 0.5 are one track: the same individual. A track enters clustering as a
must-link group (its vectors averaged, or its best few kept), so a pet seen from
the side and then from the front in one clip ties those poses together, which
still photos can't do. Two tracks visible in the same frame are never one
individual.

**Tags.** A video gets `Pets|<Name>` or `People|<Name>` only when that
individual's confirmed tracks cover at least `k` kept frames (first guess 3),
so a dog crossing the background for half a second doesn't tag the clip.
Suggestions and confirmation work as for photos.

**Timestamps.** Each track keeps its frame times (seconds into the clip) in
`identities.db`, so the gallery can later offer "jump to where Max appears".
Crops for the naming page are re-decoded on demand at that timestamp (one
seek), never stored.

**Erase and verify-gone.** Video tracks, timestamps and per-frame vectors are
keyed by the clip's sha256 like photo rows, so the same `identities.forget(sha)`
drops them and `verify-gone` checks them.

**Overnight runs.** `media-intake pets scan --videos --inhibit` (the queue's
`systemd-inhibit --what=sleep:idle` pattern), resumable per clip: a clip is done
when its rows are written, so stopping loses at most the clip in progress.

### Measured (synthetic clips, 2026-11-19)

Three 40 s, 30 fps clips made locally with ffmpeg from the public COCO sample
(two cats, panned and mirrored) and ffmpeg's `testsrc2`/`mandelbrot` patterns
(four 10 s scenes); same CPU, 4 threads, int8 DINOv2, YOLOX-s, VA-API on the
AMD iGPU. No user media.

| Clip | GPU decode to 2 fps (GStreamer) | CPU decode (ffmpeg, OpenH264) | Detect, 40 kept frames | Embed |
|---|---|---|---|---|
| 4K HEVC, 40 Mbit/s | 9.1 s | not possible (ffmpeg-free has no HEVC) | 3.8 s (95 ms a frame) | 52 crops, 1.35 s; 28 near-duplicates not stored |
| 4K H.264 | 5.0 s | 18.0 s | 3.7 s | 50 crops, 1.32 s |
| 1080p H.264 | 1.9 s | 5.8 s | 3.7 s | 50 crops, 1.32 s |

Scene selection took 0.09 s a clip. Temporal linking turned the 2 cats x 2
scenes into 4 tracks (17, 17, 9 and 9 frames): linking is per scene, joining
scenes is clustering's job. Totals: 14.3 s (4K HEVC), 10.9 s (1080p) for a
40 s clip. Decode time grows with clip length (every frame is decoded, then
dropped); detection and embedding are capped by the frame limit.

**Estimate for ~2,000 videos** (inferred, not measured on real clips): at a
mix of 30-60 s phone clips, about 8-20 s a clip for pets, so 4.5-11 hours;
adding faces on the same frames (YuNet + SFace, about 0.1-0.2 s a frame)
brings it to roughly 7-15 hours. That is an overnight job or two, resumable.
Cheaper options if needed: cap at 30 frames, NanoDet instead of YOLOX for
video (28 ms against 86 ms a frame), or decode keyframes only (not measured).

## Costs

| | Pets | People | Both |
|---|---|---|---|
| Download | 60.3 MB (YOLOX 35.9 + int8 DINOv2 24.5; +88.5 MB if fp32 is needed) | 38.9 MB | 99 MB |
| RAM while scanning | ~300 MB measured | small (YuNet + SFace) | under 1 GB (estimate) |
| CPU per photo | ~90 ms detect + ~33 ms per animal found (measured, synthetic), plus decode | 0.21 s (the prototype's own 20-photo smoke test) | one shared decode |

For ~9,000 photos, 4 threads: pets alone about 15-30 minutes (detection 13
minutes plus decode and crops; real decode not measured); with people in the
same pass about 45-75 minutes. Videos: see "Videos". Resumable with `--limit N` or by stopping and
re-running. The NVIDIA GPU is not needed; CUDA could be an opt-in later
(onnxruntime-gpu is a different, much larger wheel).

## Build plan

| Phase | What | Size |
|---|---|---|
| 1 | Port the face prototype as `identities.py` + `people.py`, off by default; config keys, `setup.sh --with-people`, pins, doctor, erase / forget / trash / private / verify-gone hooks, backup test, synthetic tests, docs | M |
| 2 | `pets.py`: YOLOX detection and int8 DINOv2 vectors in the same scan pass; species and same-photo constraints; suggestions and confirm; `Pets|` tags; `setup.sh --with-pets` | M |
| 3 | `pets eval` / `people eval` (read-only, `Calibrate|pet|…` labels), threshold notes in `docs/configuration.md` | S |
| 4 | Gallery "People & Pets": sidebar section, filters, the naming view in the gallery server | M |
| 5 | Videos (section "Videos"): scene-change sampling on the GPU decode path, temporal linking, per-video tags with timestamps; the per-frame search index from the same frames | L |
| 6 (only if `pets eval` says so) | Switch the pet embedder to fp32 DINOv2 (`setup.sh` fetches the second pinned file) | S |

## Still open

- Date-aware suggestions (prefer neighbours close in date, for kittens and
  puppies): worth the complexity, or leave it to owner merges? Decide after the
  first real clusters.
- How many frames a video must show a pet or person in before it gets the tag
  (`k` in "Videos"); a first guess of 3 is calibrated by `pets eval`.

## Unverified

- Any accuracy on real household pets; every threshold above is a starting
  guess, settled by the evaluation.
- Scan time on real photos (decode, HEIC) and on another CPU.
- The AvitoTech paper's numbers (read from its arXiv HTML and model card, not
  reproduced); the MegaDescriptor/MiewID zero-shot pet numbers come from that
  same paper's tables.
- NanoDet and YOLOX wrappers and the video sampler were written for the
  scratch test, not reviewed as product code.
- Video numbers are from synthetic clips; real phone clips (variable frame
  rate, rotation, 10-bit HDR, audio) are not measured.

## Sources (read 2026-11-19)

- opencv_zoo at `48b10fa505aeb51fbd127fc60a5c660040e823b7`: `models/object_detection_yolox/{README.md,LICENSE}`, `models/object_detection_nanodet/{README.md,LICENSE,nanodet.py}`, `models/face_detection_yunet/LICENSE`, `models/face_recognition_sface/LICENSE`, and the LFS pointers of each `.onnx`.
- Hugging Face API (`/api/models/<repo>` and `/tree/<rev>`): facebook/dinov2-small, Xenova/dinov2-small, onnx-community/dinov2-small, BVRA/MegaDescriptor-{T-224,B-224,L-384}, conservationxlabs/miewid-msv3, facebook/dinov3-vits16-pretrain-lvd1689m, google/owlv2-base-patch16-ensemble, Xenova/owlv2-base-patch16-ensemble, PekingU/rtdetr_r18vd, onnx-community/rtdetr_r18vd, IDEA-Research/grounding-dino-tiny, onnx-community/grounding-dino-tiny-ONNX, AvitoTech/*-for-animal-identification.
- GitHub licence metadata: facebookresearch/dinov2 (Apache-2.0), facebookresearch/dinov3 (`LICENSE.md`, DINOv3 License, 2025-08-19), Megvii-BaseDetection/YOLOX, RangiLyu/nanodet, lyuwenyu/RT-DETR, IDEA-Research/GroundingDINO (Apache-2.0), ultralytics/ultralytics (AGPL-3.0), agentmorris/MegaDetector (MIT), WildMeOrg/wbia-plugin-miew-id (none), mapooon/PetFace (README terms).
- Čermák, Picek, Adam, Papafitsoros, "WildlifeDatasets: An open-source toolkit for animal re-identification", [arXiv 2311.09118v2](https://arxiv.org/abs/2311.09118) (2023-12-14), Table 4.
- Shinoda, Shiohara, "PetFace: A Large-Scale Dataset and Benchmark for Animal Identification", ECCV 2024, [arXiv 2407.13555](https://arxiv.org/abs/2407.13555); [project page](https://dahlian00.github.io/PetFacePage/).
- Kudryavtsev et al., "From Visual to Multimodal: Systematic Ablation of Encoders and Fusion Strategies in Animal Identification", *J. Imaging* 12(1):30 (2026-01-07), [arXiv 2603.02270](https://arxiv.org/abs/2603.02270), Tables 25-26 and the AvitoTech model cards.
- Oquab et al., "DINOv2: Learning Robust Visual Features without Supervision", [arXiv 2304.07193](https://arxiv.org/abs/2304.07193).
