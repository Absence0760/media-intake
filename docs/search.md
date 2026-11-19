# Semantic search and AI trash opinions (local only)

Two tools beside intake, both offline:

- `media_intake/semantic.py` (`media-intake index|search|similar`): find
  library photos and videos by a text query or by example (SigLIP
  embeddings).
- `media_intake/vlm_trash.py` (`media-intake vlm-trash`): a second opinion on
  trash from the local vision model `qwen3-vl:2b` (Ollama, Apache-2.0), as
  tags in the AI's own namespace.

Both run in the venv `bash setup.sh` builds (`<state>/venv`); the
`media-intake` launcher starts them there. Run long jobs under your own memory
cap if you like. Before `setup.sh` has built the venv, `media-intake index
--help` (and `search`, `similar`, `vlm-trash`) still prints the options, then
one line saying the command needs the venv: `run bash setup.sh first`.

## Privacy guarantees

- **Images never leave the machine.** SigLIP runs on CPU through onnxruntime.
  `vlm_trash.py` talks only to `http://127.0.0.1[:port]`; any other host
  (`localhost` included, since it can be remapped) is refused before a file is
  opened, and the HTTP client ignores `http_proxy`/`HTTPS_PROXY`.
- **Refused files are never opened**: anything under the state's
  `quarantine/` or `batches/` (staging), and any file whose size+sha256
  `scan.json` flagged, was quarantined or erased (`label.refused()` and
  `label.flagged()`, the same checks the tagger uses). `vlm-trash FILE...`
  with such a file exits 3 and sends nothing; scans skip them. `semantic.py`
  never indexes them, drops vectors of files flagged later, and filters
  flagged shas at query time too.
- **No descriptions are stored.** `semantic.py` stores only float16 vectors,
  sha256s and paths in `<state>/embeddings.npz` (0600, in the 0700 state
  dir). `vlm_trash.py` asks for a fixed JSON shape (verdict, a reason from a
  fixed list, confidence) and writes only tags from that shape; free text from
  the model is discarded. Output is counts only.
- Frames sampled from videos go to a 0700 temp dir in `~/.cache/media-intake`
  and are deleted straight after.
- Originals are never written; only `<file>.<ext>.xmp` sidecars.

## Semantic search

```
media-intake index
media-intake search "dog on a beach at sunrise" -n 20 --videos --project NAME --unused-since 30d --clean
media-intake similar ~/Videos/personal/2026/2026-05/GX010123.MP4 -n 20
```

- `index` walks the library, videos and project folders
  ([configuration.md](configuration.md)). One vector per file: label.py's
  vision encoder (`google/siglip-base-patch16-224`, quantized ONNX,
  sha256-checked) and its 224 px preprocessing; videos average up to 8 frames
  spread over the clip, sampled by `scan_sensitive.video_frames` (ffmpeg, then
  GPU HEVC decode through GStreamer, which Fedora's ffmpeg can't do).
  A clip shorter than the sampling period still gives its one frame. Raw
  photos (`.dng`, GoPro `.gpr`) are indexed from what Pillow can read of them.
  A file nothing decodes (an empty, truncated or audio-only video, a raw Pillow
  can't open) counts as `undecodable`, gets no vector and is tried again on the
  next `index`.
  Incremental: a `(path, size, mtime)` cache avoids re-hashing, and only new
  sha256s are embedded; copies share one vector. Files that left the library
  lose their vector.
- `search` embeds the query with SigLIP's text model (`text_model.onnx` +
  `tokenizer.json`, sha256-pinned as in `setup.sh`). That model exists only
  in the build cache `~/.cache/media-intake-build` (made by `bash setup.sh
  --build-labels`), used read-only. Without it `search` says so and only
  `similar` works. Queries are lower-cased and stripped of punctuation as the
  labels are.
- **Prompt ensemble** (the default): `search` embeds the query through three
  templates, `a photo of {}`, `a picture of {}` and the query as typed, in one
  batch through the text model, averages the unit-length embeddings and
  renormalises the mean (the standard CLIP/SigLIP zero-shot technique; a single
  word otherwise sits far from how captions are phrased). A query that already
  starts with a template's prefix is stripped of it first, so `a photo of a
  dog` gives `a photo of a dog`, `a picture of a dog`, `a dog`. It is
  deterministic: the same query gives the same vector. `--no-ensemble` embeds
  the query alone, as before, to compare the two; scores of the two runs are
  not comparable with each other. The `--format machine` output the gallery
  reads is the same with or without it. Text handling (canonical form, 64
  tokens, `</s>`) is `build_label_embeddings.py`'s, shared with the labels.
- Videos have one vector each, the mean of their sampled frames (above), so a
  query matches a video by its average look, and a scene that fills only part
  of a long clip ranks it lower than a clip that is all that scene. Ranking by
  the best frame would need a vector per frame in the index; the index format
  is unchanged for now. (`label.py`'s content labels already take the max over
  up to 30 frames.)
- `similar FILE` ranks by cosine against FILE's vector (embedded on the fly
  if it isn't in the index; refused files exit 3).
- Filters (read from sidecars via exiftool): `--photos` / `--videos`,
  `--project P` (`Project|P`), `--unused-since Nd` (no
  `Used|<YYYY-MM-DD-slug>` in the last N days, same rule as `media-intake
  find`), `--clean` (drops `Review|child-in-shot`, `Review|maybe-trash` and
  `AI|vlm/*/trash/*` unless you kept the file: `Owner|keep`, `Owner|not-trash`,
  the legacy `Review|not-trash` or a favorite (`xmp:Rating` >= 5) outrank a
  trash flag; `Owner|trash` is dropped unless the file is a favorite, and
  `Review|child-in-shot` is never cleared by a keep or a favorite).
- Output, best first; the score is the cosine (SigLIP text-image cosines
  are small, about 0.05-0.2; compare within one query only). Two forms
  (`--format`):
  - **human** (the default on a terminal): a numbered list with the score
    to 2 decimals, the date the path carries (a day in the file or folder
    name such as `20261108` or `2026-11-08-race`, else intake's
    `YYYY/YYYY-MM` library folder as `YYYY-MM`; blank when neither, and no
    file is opened for it), and the path, then a count line. No hits says
    so, and suggests dropping the filters when any were given.

    ```
    1. 0.14  2026-11-08  /home/you/Pictures/library/2026/2026-11/IMG_20261108_071502.jpg
    2. 0.12  2026-10     /home/you/Pictures/library/2026/2026-10/IMG_0042.jpg
    2 results, best first (score: cosine, compare within one query)
    ```
  - **machine** (the default when stdout is piped or captured): `score  path`
    lines, score to 3 decimals, and `N results` on stderr. The gallery's
    search box and Find similar read this form (they run `semantic.py` with
    `--format machine`, never relying on the default), so it stays byte for
    byte as it is; `--format machine` forces it on a terminal too.
- Nothing to search yet: `search` and `similar` stop with one line naming
  the fix instead of a traceback: no index (or an empty one) says `build it
  with: media-intake index`; no text model says `bash setup.sh
  --build-labels`; run outside the venv (numpy or onnxruntime missing) says
  to go through `media-intake search` / `similar` or run `bash setup.sh`.
  The index is checked before the text model is loaded.
- Progress (`index`): counts only, on stderr, never a file name. A
  start line (`index: N files to embed, M already indexed`), then
  `index: done/total files (pct), rate, about <time> left` every 100 files
  or 10 s, whichever comes first (on a terminal one line rewritten in place
  every second); hashing on a first run shows as `index (checking): ...`
  every 1,000 files. It ends with a summary: `index: indexed N new (photos,
  videos), M unchanged, K failed (undecodable)[, C copies of new files];
  R refused, D dropped; took T`. On a terminal it then prints
  `next: media-intake search "a photo of a dog" -n 20`; piped or captured
  (the queue's step log, scripts), stdout instead ends with the counts as
  one JSON line.
  `MEDIA_INTAKE_PROGRESS_EVERY` / `MEDIA_INTAKE_PROGRESS_SECS` override the
  cadence, as for intake's sweeps.

A workflow for finding unused, cleared footage: `search "..." --videos
--unused-since 30d --clean`, pick a hit, `similar <hit>` with the same filters
for alternative takes, and after using one, `media-intake mark-used FILE...
--post <YYYY-MM-DD-slug>` so the next search skips it. A missing
`Review|child-in-shot` flag means "not checked", not "no child": watch every
clip before you publish it.

## VLM trash opinion

A second opinion on Suggested junk from a local vision model; its verdict
shows in the gallery as the Junk check and Junk score ([words.md](words.md)).

```
media-intake vlm-trash                    # Suggested junk (files tagged Review|maybe-trash)
media-intake vlm-trash FILE...            # an explicit list
media-intake vlm-trash --all --dry-run    # how many an overnight run would judge
media-intake vlm-trash --all --inhibit    # the whole library, holding a sleep inhibitor
```

Other options: `--redo` (judge again), `--host http://127.0.0.1:PORT`,
`--allow-cpu` (see below).

- Model `qwen3-vl:2b` (Qwen3-VL-2B-Thinking, Q4_K_M, 1.9 GB on disk), pinned
  by its local digest (read from `/api/tags`; `ollama list` shows its first
  12 characters as the ID). The digest is written only in
  `media_intake/pins.json` (`ollama.digest`); a different one refuses to run.
  Install with `ollama pull qwen3-vl:2b`, then `media-intake doctor` checks
  the pulled digest against the pin. Licence
  Apache-2.0 (commercial use allowed): `ollama show qwen3-vl:2b` and
  <https://huggingface.co/Qwen/Qwen3-VL-2B-Thinking>.
- Sent with `"think": false`. It is a thinking model, and under a `format`
  schema Ollama 0.21.2 puts the constrained JSON in the reply's `thinking`
  field with an empty `response`; `parse()` reads `response`, else
  `thinking`, with the same strict checks.
- Per photo: EXIF-rotated, downscaled to at most 768 px, JPEG in memory,
  `POST /api/generate` with `format` as a JSON schema, `temperature 0`,
  `seed 0`, `num_ctx 8192` (Ollama's 4096 default truncates silently; a
  768 px image is at most 24 x 24 = 576 qwen3-vl image tokens, and a reply
  whose `prompt_eval_count` comes within 64 of `num_ctx` counts as
  truncated), `keep_alive 2m`; the final request sends `keep_alive 0`, which
  unloads the model from the GPU, also after an error or Ctrl-C.
- Validation: the reply must be JSON with exactly `verdict` (`TRASH`/`KEEP`),
  `reason` (blurry, accidental, dark, packaging, random-object,
  screenshot-junk, duplicate-ish, other) and `confidence` (a number 0..1).
  Anything else, a network error, or a missing or near-limit token count is
  KEEP with no confidence.
- Videos: 3 frames (`scan_sensitive.video_frames`; a clip under about 4 s
  gives 1 or 2); trash only when most of them (2 of 3) say TRASH with
  confidence >= 0.7 (conf: the lowest of those), else keep. A video nothing
  decodes counts as `undecodable`: no tag, and it is judged again next run.
- GPU check: after the first photo it reads `/api/ps` and stops (exit 0,
  `stopped-not-on-gpu` in the counts, model unloaded) when under 90% of the
  model is in VRAM, since a CPU fallback is very slow and holds most of the
  RAM; `--allow-cpu` overrides.

### Tags (AI never overrides a tag)

Only in its own namespace, in `XMP-lr:HierarchicalSubject` (leaf mirrored in
`XMP-dc:Subject` like every other tag):

```
AI|vlm/qwen3-vl-2b/trash/<reason>     or   AI|vlm/qwen3-vl-2b/keep
AI|vlm/qwen3-vl-2b/conf/<0.0-1.0>     the model's confidence, one decimal
```

- The raw verdict is written; `media-intake suggest-trash` and the gallery
  take `AI|vlm/*/trash/*` with `conf` >= 0.7.
- A re-run (`--redo`) removes only this model's `AI|vlm/qwen3-vl-2b/` tags and
  adds the new ones; another model would write `AI|vlm/<its tag>/` beside
  them. No other keyword is ever added or removed (`Review|child-in-shot`
  included).
- Skipped entirely (not sent, not tagged): any owner decision
  (`media_intake/owner.py`): `Owner|not-trash`, `Owner|keep`, `Owner|trash`,
  the legacy `Review|not-trash`, a digiKam pick label or a favorite
  (`xmp:Rating` >= 5, the gallery's star). The default candidate
  scan (no `--all`, no FILE) lists only `Review|maybe-trash` files you haven't
  kept.
- Files tagged `Used|*` or a protected project's `Project|<name>`
  ([configuration.md](configuration.md)) are not sent and get `.../keep`
  (never a trash verdict).
- Files outside the library, videos and project folders are refused.

### Overnight run (`--all`)

- Incremental and resumable: `<state>/vlm-progress.json` (0600) records path,
  size, mtime, model digest and verdict, written with the sidecars every 25
  files; a file is skipped when that record matches and its sidecar has this
  model's verdict tag. A kill loses at most 25 judgements.
- `--inhibit` re-execs under `systemd-inhibit --what=sleep:idle`.
- A file whose `.xmp` sidecar can't be written (a read-only folder or
  sidecar) is skipped before anything is sent and counted as `unwritable`,
  so one such folder no longer stops the run. Fix the permissions and run
  again to judge it.
- Progress on stderr, counts only, never a file name: a start line
  (`vlm-trash: N files to judge`), then `vlm-trash: done/total files (pct),
  rate, about <time> left` every 100 files or 10 s (on a terminal one line
  rewritten in place every second; `MEDIA_INTAKE_PROGRESS_EVERY` /
  `_SECS` override), from the wall-clock rate so far. Verdicts are still
  written every 25 files.
- A summary line on stderr at the end: `vlm-trash: judged N (T trash, L
  low-confidence trash, K keep), skipped M (already judged, your decision,
  refused, sidecar not writable)`, plus protected, undecodable and a GPU stop when they happen,
  and the time taken; `--dry-run` says `would judge N`. stdout still ends
  with the counts as one JSON line (the machine form).

Start it in the background (the log has counts only):

```
nohup media-intake vlm-trash --all --inhibit > ~/.local/share/media-intake/vlm-all.log 2>&1 &
```

Check first that `media-intake doctor` reports the `qwen3-vl:2b` digest as
`ok` (it matches the pin in `media_intake/pins.json`), and that `--all --dry-run` gives a sensible count. At about 0.6 s a synthetic
photo, 10,000 photos is under 2 hours of model time; real photos decode
slower.

## Speeds measured (synthetic images, laptop with an 8 GB GPU)

| Step | Measured |
|---|---|
| `index`, photo (640x480 JPEG, CPU) | 0.15 s an image |
| `index`, video (6 s clip, 3 frames sampled) | 0.7 s a video |
| `search` (text model load + query, a few hundred vectors) | 1.5 s |
| `vlm-trash`, qwen3-vl:2b, model load (first request) | 15.7 s |
| `vlm-trash`, qwen3-vl:2b, per photo (model loaded) | 0.61 s, 100% GPU |
| qwen3-vl:2b memory | VRAM 3.5 GB of 8 GB (`ollama ps`: 3.8 GB, ctx 8192); Ollama runner RSS 1.4 GB peak; VRAM freed after the run |
| (rejected) qwen2.5vl:7b, model load / per photo | 29 s / 54 s, on the CPU |

qwen3-vl:2b on the synthetic set: blurred scene TRASH/blurry 0.9, near-black
TRASH/dark 1.0, product-box render TRASH/packaging 1.0, clear landscape KEEP
0.9; all four well-formed.

## Model choice

Constraints: fits an 8 GB laptop GPU fully, commercial use allowed, pullable
and runnable by Ollama 0.21.2, released at least 7 days before it was chosen.
Checked November 2026.

| Model (Ollama tag, size) | Licence | Result |
|---|---|---|
| **qwen3-vl:2b** (1.9 GB, digest pinned in `media_intake/pins.json`) | Apache-2.0 | **Chosen.** 100% GPU, 0.6 s/photo |
| qwen2.5vl:7b (6.0 GB) | Apache-2.0 | 7.35 GB vision buffer; CPU fallback, 54 s/photo, about 12.5 GB RAM (below) |
| qwen2.5vl:3b (3.2 GB) | Qwen Research License, non-commercial | Blocked by the licence |
| qwen3-vl:4b-instruct / 2b-instruct (3.3 / 1.9 GB) | Apache-2.0 | The Ollama registry refuses 0.21.2 (HTTP 412 "requires a newer version of Ollama") |
| gemma3:4b, gemma3:4b-it-qat (3.4 / 4.0 GB) | Gemma Terms of Use (commercial use allowed, Prohibited Use Policy applies) | Same 412 on 0.21.2 |
| gemma4:e2b / e4b (4.6-9.5 GB) | separate Gemma 4 licence, not checked | Bigger than needed; not tried |
| minicpm-v:8b | MiniCPM Model License: commercial use only after registration | Not chosen |
| moondream:1.8b (1.7 GB) | not checked | 2K context, about 2 years old; weaker fallback |
| granite3.2-vision:2b (2.4 GB) | not checked | Built for documents and charts, not photos |

On a newer Ollama, qwen3-vl:4b-instruct (Apache-2.0, no thinking) is the next
candidate to measure; it needs a new digest pin and its own namespace
(`AI|vlm/qwen3-vl-4b-instruct/`).

Sources: <https://ollama.com/library/qwen2.5vl/tags>,
<https://huggingface.co/Qwen/Qwen2.5-VL-3B-Instruct/blob/main/LICENSE>,
<https://ollama.com/library/qwen3-vl/tags>, <https://ollama.com/library/qwen3-vl>
(requires Ollama 0.12.7), <https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct>,
<https://huggingface.co/Qwen/Qwen3-VL-2B-Thinking>,
<https://ollama.com/library/gemma3/tags>, <https://ai.google.dev/gemma/terms>,
<https://ollama.com/library/gemma4/tags>,
<https://huggingface.co/openbmb/MiniCPM-V-2_6>,
<https://ollama.com/library/moondream>,
<https://ollama.com/library/granite3.2-vision>.

### Why not qwen2.5vl:7b

It does not fit an 8 GB GPU under Ollama 0.21.2. Ollama reserves a 7.35 GB
vision compute graph (sized for the model's largest image, so neither the
768 px cap nor `num_ctx` 4096/8192 nor `num_batch` changes it); every GPU
split fails `cudaMalloc` and it falls back to 0/29 layers on the GPU. On the
CPU the runner needs about 12.5 GB of system RAM (weights 5.6 GB + graph
6.7 GB), outside any per-job memory cap because the Ollama service owns it.
At 54 s a photo an overnight run covers only about 600 photos. This is why
`vlm_trash.py` has the GPU check above.

## Tests

`python3 -I -m unittest discover -s tests -p test_semantic.py` and `-p test_vlm_trash.py` (a stub
Ollama server; it doesn't measure model speed). `VLM_REAL=1` adds real calls
against the local Ollama. The progress, summary and output-format cases in
`test_semantic.py` (`Meter`, `Ui`) fake the encoders and need only numpy; they
also check that the gallery still parses the machine form. The prompt-ensemble
cases (`TextEnsemble`) use a fake tokenizer and text model, also numpy only.
`-p test_calibrate.py` checks `media-intake calibrate`'s precision, recall and threshold
sweep on fake scores ([configuration.md](configuration.md), "Calibrating label thresholds").

## Unverified

- Verdict quality on real photos: tested on synthetic images only. Treat
  `AI|vlm/*` as a hint for review, never as a decision.
- SigLIP text search quality on real footage (the base 224 px model is small;
  checked only on synthetic shapes), and how much the prompt ensemble helps on
  it: compare `search "..."` with `search "..." --no-ensemble`.
- The quantized vision encoder with the float text model: their embeddings
  line up well enough for `labels.npz`, so search uses the same pair.
