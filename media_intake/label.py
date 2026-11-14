#!/usr/bin/env python3
"""Content labels for library files, fully offline (SigLIP zero-shot, CPU).

  venv/bin/python -I media_intake/label.py [--all] < FILES.json > LABELS.json

FILES.json is a JSON list of paths. The output maps each path to
{"labels": {label: score}, "review": [flag, ...]}: label names and sigmoid
scores only, never pixels or text. Thresholds and prompts: labels.yaml; text
embeddings: labels.npz (build_label_embeddings.py). --all reports every score
(for calibrating thresholds), not just those over the threshold.

Model: google/siglip-base-patch16-224 (Apache-2.0), Xenova's ONNX export,
vision encoder only (onnx/vision_model_quantized.onnx), downloaded and
sha256-checked by setup.sh and checked again here. Score per label:
sigmoid(cos(image, text) * exp(logit_scale) + logit_bias), max over the
label's prompts; videos: max over up to 30 frames sampled evenly.

Refuses (exit 3, nothing labelled) any path under the quarantine or staging
folders, and any file whose size+sha256 matches something scan.json flagged,
anything quarantined, or anything in the erase log.
"""
import glob, hashlib, json, math, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

STATE = config.get()["state"]
REV = "4649052661e53c7000355844105f8a1792088239"
MODEL = os.path.join(STATE, "models", f"siglip-base-patch16-224-{REV[:7]}", "vision_model_quantized.onnx")
MODEL_SHA = "ef14a954f3d57e1806666432bd9785004c1dc27100aa260eee0cb0f10a5de058"
# google/siglip-base-patch16-224 model.safetensors at 7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed
LOGIT_SCALE, LOGIT_BIAS = 4.7649970054626465, -12.9324369430542
SIZE, MEAN, STD = 224, 0.5, 0.5  # preprocessor_config.json at REV (bicubic resize, no crop)
IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif"}
VID = {".mp4", ".mov", ".3gp", ".mkv", ".webm"}
MAX_FRAMES = 30
MAX_LABELS = 8
# Cheap junk check on the SIZE x SIZE frame (images only). Conservative: only very blurry or
# near-black frames get Review|maybe-trash; a soft or moody photo should not. Uncalibrated.
BLUR_VAR_MAX = 8.0     # variance of the 4-neighbour Laplacian of the grey frame (0..255 scale)
DARK_MEAN_MAX = 10.0   # mean grey level, 0..255


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _load(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def flagged(state=STATE):
    """{sha256: size} of everything scan flagged, quarantined or erased, in any batch."""
    sig = {}
    for b in glob.glob(os.path.join(state, "batches", "*")):
        scan = _load(os.path.join(b, "scan.json"), {}) or {}
        for p in _load(os.path.join(b, "plan.json"), []) or []:
            if p.get("rel") in scan:
                sig[p["sha"]] = p["size"]
    for q in glob.glob(os.path.join(state, "quarantine", "*", "quarantine.json")):
        for v in (_load(q, {}) or {}).values():
            sig[v["sha"]] = v["size"]
    try:
        with open(os.path.join(state, "erase-log.txt")) as f:
            for m in re.finditer(r"sha256:([0-9a-f]{64}) size:(\d+)", f.read()):
                sig[m.group(1)] = int(m.group(2))
    except FileNotFoundError:
        pass  # no erase log yet: nothing erased
    return sig


def refused(paths, state=STATE):
    """Paths that must never be tagged or labelled."""
    roots = [os.path.realpath(os.path.join(state, d)) + os.sep for d in ("quarantine", "batches")]
    sig = flagged(state)
    sizes = set(sig.values())
    out = []
    for p in paths:
        rp = os.path.realpath(p)
        if any(rp.startswith(r) for r in roots):
            out.append(p)
            continue
        try:
            if os.path.getsize(p) in sizes and sig.get(_sha(p)) == os.path.getsize(p):
                out.append(p)
        except OSError:
            pass  # vanished since listing: not a match
    return out


def label_config():
    import yaml
    with open(os.path.join(HERE, "labels.yaml")) as f:
        cfg = yaml.safe_load(f)
    names, prompts, owner, thr, review = [], [], [], {}, set()
    for name, v in cfg["labels"].items():
        names.append(name)
        thr[name] = float(v.get("threshold", cfg.get("default_threshold", 0.05)))
        if v.get("review"):
            review.add(name)
        for t in v["prompts"]:
            prompts.append(t)
            owner.append(name)
    return names, prompts, owner, thr, review


def frames_video(path, np):
    """Up to MAX_FRAMES RGB frames at SIZE x SIZE, spread over the whole video, or None."""
    r = subprocess.run(["ffprobe", "-v", "quiet", "-of", "json", "-show_entries", "format=duration", path],
                       capture_output=True, text=True)
    try:
        dur = float(json.loads(r.stdout or "{}")["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        dur = 60.0
    fps = min(0.5, MAX_FRAMES / max(dur, 1.0))
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an", "-sn", "-dn",
                        "-vf", f"fps={fps:.5f},scale={SIZE}:{SIZE}:flags=bicubic", "-frames:v", str(MAX_FRAMES),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=1800)
    n = len(r.stdout) // (SIZE * SIZE * 3)
    if r.returncode or n == 0:
        # one frame at the start (very short clips round fps down to no frame)
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an", "-vf", f"scale={SIZE}:{SIZE}",
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=600)
        n = len(r.stdout) // (SIZE * SIZE * 3)
        if r.returncode or n == 0:
            return gst_frames(path, np)
    return np.frombuffer(r.stdout[: n * SIZE * SIZE * 3], dtype=np.uint8).reshape(n, SIZE, SIZE, 3)


def gst_frames(path, np):
    """HEVC (Fedora's ffmpeg-free can't decode it): GPU decode via scan_sensitive.video_frames."""
    import shutil, tempfile
    from PIL import Image
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scan_sensitive
    work = os.path.expanduser("~/.cache/media-intake")
    os.makedirs(work, mode=0o700, exist_ok=True)
    d = tempfile.mkdtemp(dir=work)
    try:
        names = scan_sensitive.video_frames(path, d, max_frames=MAX_FRAMES)
        if not names:
            return None
        arr = [np.asarray(Image.open(os.path.join(d, n)).convert("RGB").resize((SIZE, SIZE), Image.BICUBIC))
               for n in names]
        return np.stack(arr).astype(np.uint8)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def frame_image(path, np):
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass  # optional: HEIC support only; other formats still open
    try:
        with Image.open(path) as im:
            im.draft("RGB", (SIZE * 2, SIZE * 2))
            up = ImageOps.exif_transpose(im).convert("RGB").resize((SIZE, SIZE), Image.BICUBIC)
            return np.asarray(up, dtype=np.uint8)[None]
    except Exception:
        return None


def quality_flags(frame, np):
    """["maybe-trash"] for a very blurry or near-black RGB uint8 frame (H x W x 3), else []."""
    g = frame.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
    return ["maybe-trash"] if float(lap.var()) < BLUR_VAR_MAX or float(g.mean()) < DARK_MEAN_MAX else []


def main():
    import numpy as np
    import onnxruntime as ort
    all_scores = "--all" in sys.argv[1:]
    paths = json.load(sys.stdin)
    bad = refused(paths)
    if bad:
        print(f"label.py: refusing {len(bad)} quarantined/flagged/staging files; nothing labelled", file=sys.stderr)
        sys.exit(3)
    names, prompts, owner, thr, review = label_config()
    emb = np.load(os.path.join(HERE, "labels.npz"))
    if list(emb["prompts"]) != prompts:
        sys.exit("label.py: labels.npz doesn't match labels.yaml; run bash setup.sh --build-labels")
    txt = emb["emb"].astype(np.float32)
    if not os.path.exists(MODEL) or _sha(MODEL) != MODEL_SHA:
        sys.exit(f"label.py: model missing or checksum wrong ({MODEL}); run bash setup.sh")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(MODEL, so, providers=["CPUExecutionProvider"])
    col = {n: [i for i, o in enumerate(owner) if o == n] for n in names}
    out = {}
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        fr = frames_video(p, np) if ext in VID else frame_image(p, np) if ext in IMG else None
        if fr is None:
            out[p] = {"labels": {}, "review": ["untagged-video" if ext in VID else "untagged-image"]}
            continue
        x = (fr.astype(np.float32) / 255.0 - MEAN) / STD
        x = np.ascontiguousarray(x.transpose(0, 3, 1, 2))
        e = np.concatenate([sess.run(["pooler_output"], {"pixel_values": x[i:i + 8]})[0] for i in range(0, len(x), 8)])
        e /= np.linalg.norm(e, axis=1, keepdims=True)
        logits = e @ txt.T * math.exp(LOGIT_SCALE) + LOGIT_BIAS
        prob = (1 / (1 + np.exp(-logits))).max(axis=0)  # max over frames
        score = {n: float(prob[col[n]].max()) for n in names}
        if all_scores:
            keep = score
        else:
            keep = dict(sorted(((n, s) for n, s in score.items() if s >= thr[n] and n not in review),
                               key=lambda kv: -kv[1])[:MAX_LABELS])
        flags = [n for n in names if n in review and score[n] >= thr[n]]
        if ext in IMG:  # videos: skipped
            flags += [f for f in quality_flags(fr[0], np) if f not in flags]
        out[p] = {"labels": {n: round(s, 4) for n, s in keep.items()}, "review": flags}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
