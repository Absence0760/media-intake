#!/usr/bin/env python3
"""`media-intake pets`: recognise your own pets in the library, fully offline (YOLOX + DINOv2, CPU).

  media-intake pets scan [--root DIR]... [--limit N] [FILE...]
  media-intake pets cluster [--link 0.8]
  media-intake pets clusters [--min 2] [--unnamed]
  media-intake pets name CLUSTER "Name" | merge CLUSTER OTHER... | ignore CLUSTER... | unname CLUSTER...
  media-intake pets suggestions | confirm CLUSTER... [--det ID] [--all] | reject ...
  media-intake pets take-out DET... | put-back DET...
  media-intake pets tag [--dry-run]             (Pets|<Name> into XMP sidecars)
  media-intake pets find "Name" [--alone] [--min-box 0.05] [--suggested]
  media-intake pets pets                        (names and file counts)
  media-intake pets eval                        (your Pets|<Name> names and tags as truth; read-only)

Runs in the venv (cv2, onnxruntime, numpy). Off until `bash setup.sh --recognition` fetches the two models,
each pinned in media_intake/pins.json and sha256-checked by setup.sh, doctor and here before it loads:
  YOLOX-s object_detection_yolox_2022nov.onnx from opencv/opencv_zoo (Apache-2.0; upstream Megvii YOLOX,
    Apache-2.0), run by cv2.dnn: COCO's 80 classes, of which recognition.pet_classes are kept (default cat
    and dog; docs/configuration.md).
  DINOv2 ViT-S/14 (facebook/dinov2-small, Apache-2.0), Xenova's INT8 ONNX export onnx/model_quantized.onnx,
    run by onnxruntime: one 384-d vector per animal (the CLS token of last_hidden_state; the export has no
    pooler output, and config.json's apply_layernorm means that token is already layer-normed).
Nothing is downloaded at run time. Design and evidence: docs/research-pets.md.

scan: YOLOX on the photo letterboxed to 640 x 640 (RGB, 0..255, grey 114 padding: opencv_zoo's
object_detection_yolox/demo.py at the pinned commit), score = objectness x class probability >= 0.5, NMS
at IoU 0.5 across the kept classes (one animal, one box), and a box whose short side is at least 96 px in
the decoded photo (long side 1600; DINOv2 reads 224 px, smaller crops give weak vectors). Whole-body
boxes: pets are photographed from behind and the side far more than people. The crop is the box with a
10 % margin padded to a square (the core's square_crop: no tail or ear cut off), then DINOv2's own
preprocessing (Xenova/dinov2-small preprocessor_config.json at the pinned revision: bicubic resize of the
shortest edge to 256, centre crop 224, x / 255, ImageNet mean and std; on a square crop that centre crop
trims only margin). The SigLIP
vision model setup.sh already installs embeds the same crop as a second vector (label.py's preprocessing),
stored beside DINOv2's for `pets eval` only; clustering uses DINOv2's.

cluster: one species at a time (a cat never joins a dog), two pets in one photo or video frame are never one
pet, names only from you (later matches are suggestions until you confirm them). LINK and SAME below are
uncalibrated first guesses that lean to splitting (an extra cluster costs a merge; a wrong merge tags the
wrong pet): on one public sample (two tabby cats side by side, COCO val2017 000000039769) this INT8 model
puts the two cats at cosine 0.72 and each cat against its own mirror image at 0.92, so both thresholds sit
between. `pets eval` measures them on your pets.

Date-aware propagation (recognition.pet_date_neighbours, default DATE_K; 0 turns it off): a kitten and the
grown cat may never link, so after clustering a cluster holding no name is compared with each name where the
two meet in time (the DATE_K detections on each side nearest in capture date, the day each path carries) and
gets that name as a suggestion when the average cosine passes SAME for that name alone; the name then grows
on from there (recog.date_suggest). `pets eval` reports what it would add (date_aware).

Videos: a few sampled frames each (frames.sample, via recog.default_frames); a video gets Pets|<Name> only
when that pet is in at least recognition.pet_min_frames of its sampled frames (default 3). Store, names,
suggestions, tags and erase: recog.py.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config  # noqa: E402
import recog  # noqa: E402

with open(os.path.join(HERE, "pins.json")) as _f:
    _PINS = json.load(_f)  # the single source of the pins (setup.sh, doctor)
ZOO, DINO = _PINS["opencv_zoo"], _PINS["dinov2"]
YOLOX_FILE = "models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
DINO_FILE = "onnx/model_quantized.onnx"
MODEL = "dinov2-small-int8"   # the embeddings' model name in the store (clustering)
SIGLIP_MODEL = "siglip-base-int8"  # the evaluation's baseline, on the same crops
# COCO's class order (YOLOX's 80 outputs; opencv_zoo object_detection_yolox/demo.py). Only animals may be pets.
COCO = ("person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
        "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe")
ANIMALS = COCO[COCO.index("bird"):]  # == config.PET_CLASSES (tests check)
DEFAULT_CLASSES = ("cat", "dog")
INPUT = 640                   # YOLOX-s input side
STRIDES = (8, 16, 32)         # the export's three heads (opencv_zoo yolox.py)
PAD = 114.0                   # letterbox grey (opencv_zoo demo.py)
SCORE_MIN = 0.5               # objectness x class probability (uncalibrated)
NMS_IOU = 0.5                 # opencv_zoo yolox.py default
MIN_BOX_PX = 96               # short side, in the decoded photo
MAX_SIDE = 1600               # the decoded photo's long side
DINO_RESIZE, DINO_CROP = 256, 224
DINO_MEAN, DINO_STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
LINK = 0.80                   # stage-1 link (uncalibrated first guess, leaning to splitting)
SAME = 0.75                   # stage-2 merge and "looks like" (uncalibrated first guess)
DATE_K = 5                    # date-aware propagation: detections compared on each side (recog.date_suggest)


def classes(cfg=None):
    """The COCO classes kept as pets: recognition.pet_classes (default cat, dog)."""
    return tuple((cfg or config.get()).get("pet_classes") or DEFAULT_CLASSES)


def models_dirs(state=None):
    state = state or config.get()["state"]
    return (os.path.join(state, "models", f"opencv-zoo-{ZOO['commit'][:7]}"),
            os.path.join(state, "models", f"dinov2-small-{DINO['revision'][:7]}"))


def model_files(state=None):
    """[(what, path, sha256)] of the two models, for setup.sh's layout, doctor and the refusal."""
    zoo, dino = models_dirs(state)
    return [("YOLOX animal detector", os.path.join(zoo, os.path.basename(YOLOX_FILE)),
             ZOO["files"][YOLOX_FILE]["sha256"]),
            ("DINOv2 pet recogniser", os.path.join(dino, os.path.basename(DINO_FILE)),
             DINO["files"][DINO_FILE]["sha256"])]


def check_models(files=None):
    import label
    for what, p, want in files or model_files():
        if not os.path.isfile(p) or label._sha(p) != want:
            sys.exit(f"media-intake pets: {what} missing or its checksum is wrong ({p}); "
                     "run bash setup.sh --recognition")


def letterbox(rgb, cv2, side=INPUT):
    """(side x side float32 RGB, scale): resized to fit, top-left, grey padding (opencv_zoo demo.py)."""
    import numpy as np
    H, W = rgb.shape[:2]
    s = min(side / H, side / W)
    out = np.full((side, side, 3), PAD, dtype=np.float32)
    w, h = max(1, int(W * s)), max(1, int(H * s))
    out[:h, :w] = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_LINEAR)
    return out, s


def grids(side=INPUT, strides=STRIDES):
    """(N x 2 grid cells, N x 1 strides) of YOLOX's decoded outputs, in the export's order."""
    import numpy as np
    g, st = [], []
    for s in strides:
        n = side // s
        xv, yv = np.meshgrid(np.arange(n), np.arange(n))
        g.append(np.stack((xv, yv), 2).reshape(-1, 2))
        st.append(np.full((n * n, 1), s))
    return np.concatenate(g).astype(np.float32), np.concatenate(st).astype(np.float32)


def nms(boxes, scores, thr=NMS_IOU):
    """Indices kept by greedy non-maximum suppression (highest score first; a box overlapping a kept one by
    IoU > thr goes). boxes: N x 4 (x, y, w, h). numpy only, as cv2.dnn.NMSBoxes."""
    import numpy as np
    x0, y0 = boxes[:, 0], boxes[:, 1]
    x1, y1 = x0 + boxes[:, 2], y0 + boxes[:, 3]
    area = boxes[:, 2] * boxes[:, 3]
    order, keep = list(np.argsort(-scores, kind="stable")), []
    while order:
        i, rest = order[0], np.array(order[1:], dtype=int)
        keep.append(int(i))
        if not len(rest):
            break
        iw = np.clip(np.minimum(x1[i], x1[rest]) - np.maximum(x0[i], x0[rest]), 0, None)
        ih = np.clip(np.minimum(y1[i], y1[rest]) - np.maximum(y0[i], y0[rest]), 0, None)
        inter = iw * ih
        order = list(rest[inter / np.maximum(area[i] + area[rest] - inter, 1e-9) <= thr])
    return keep


def decode(out, scale, keep_ids, score_min=SCORE_MIN, nms_iou=NMS_IOU, min_px=MIN_BOX_PX):
    """YOLOX's raw N x 85 rows -> [(box in the original image, score, class id)] for the kept classes, after
    NMS across those classes (one animal, one box: a cat also scored as a dog stays one detection, the
    likelier class) and the minimum box size."""
    import numpy as np
    g, st = grids()
    d = np.asarray(out, dtype=np.float32).reshape(-1, 85)
    xy = (d[:, :2] + g) * st
    wh = np.exp(np.clip(d[:, 2:4], -20, 20)) * st
    sc = d[:, 4:5] * d[:, 5:]
    cls = sc.argmax(1)
    best = sc.max(1)
    pick = np.isin(cls, list(keep_ids)) & (best >= score_min)
    if not pick.any():
        return []
    boxes = np.concatenate([xy - wh / 2, wh], 1)[pick] / scale
    best, cls = best[pick], cls[pick]
    found = []
    for i in nms(boxes, best, nms_iou):
        x, y, w, h = (float(v) for v in boxes[i])
        if min(w, h) >= min_px:
            found.append(((x, y, w, h), float(best[i]), int(cls[i])))
    return found


class AnimalDetector:
    """YOLOX through cv2.dnn. detect(rgb) -> [(box, score, species)]; the core cuts the square crop."""

    def __init__(self, cv2, path, keep=DEFAULT_CLASSES):
        self.cv2 = cv2
        self.net = cv2.dnn.readNet(path)  # OpenCV's own CPU backend, the default (5.0 warns on setPreferableTarget)
        self.keep = {COCO.index(k): k for k in keep}

    def detect(self, rgb):
        import numpy as np
        img, s = letterbox(rgb, self.cv2)
        self.net.setInput(np.ascontiguousarray(img.transpose(2, 0, 1))[None])
        out = self.net.forward(self.net.getUnconnectedOutLayersNames())[0]
        H, W = rgb.shape[:2]
        found = []
        for (x, y, w, h), score, cid in decode(out, s, self.keep):
            x0, y0 = max(0.0, x), max(0.0, y)  # clipped to the image, so the box stored is the animal's
            w, h = min(W, x + w) - x0, min(H, y + h) - y0
            if min(w, h) >= MIN_BOX_PX:
                found.append(((x0, y0, w, h), score, self.keep[cid]))
        return found


def _pil(crop):
    from PIL import Image
    import numpy as np
    return Image.fromarray(np.ascontiguousarray(crop))


def dino_input(crop):
    """DINOv2's preprocessing (preprocessor_config.json): shortest edge 256 bicubic, centre crop 224, x / 255,
    ImageNet mean and std; 1 x 3 x 224 x 224 float32."""
    import numpy as np
    from PIL import Image
    im = _pil(crop)
    w, h = im.size
    s = DINO_RESIZE / min(w, h)
    im = im.resize((max(DINO_CROP, round(w * s)), max(DINO_CROP, round(h * s))), Image.BICUBIC)
    w, h = im.size
    l, t = (w - DINO_CROP) // 2, (h - DINO_CROP) // 2
    a = np.asarray(im.crop((l, t, l + DINO_CROP, t + DINO_CROP)), dtype=np.float32) / 255.0
    a = (a - np.array(DINO_MEAN, np.float32)) / np.array(DINO_STD, np.float32)
    return np.ascontiguousarray(a.transpose(2, 0, 1))[None]


def siglip_input(crop):
    """label.py's SigLIP preprocessing: bicubic resize to 224 x 224 (no crop), (x / 255 - 0.5) / 0.5."""
    import numpy as np
    from PIL import Image
    import label
    a = np.asarray(_pil(crop).resize((label.SIZE, label.SIZE), Image.BICUBIC), dtype=np.float32) / 255.0
    return np.ascontiguousarray(((a - label.MEAN) / label.STD).transpose(2, 0, 1))[None]


class OnnxEmbedder:
    """embed(crop) -> vector: one onnxruntime session, its preprocessing and how to read the output."""

    def __init__(self, ort, path, prep, output, cls_token):
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        self.sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name
        self.prep, self.output, self.cls = prep, output, cls_token

    def embed(self, crop):
        out = self.sess.run([self.output], {self.input: self.prep(crop)})[0]
        return out[0, 0] if self.cls else out[0]


def engine():
    """(AnimalDetector, {MODEL: DINOv2, SIGLIP_MODEL: SigLIP when installed}), after checking the sha256s. CPU."""
    import cv2
    import onnxruntime as ort
    import label
    files = model_files()
    check_models(files)
    cv2.setNumThreads(max(1, min(4, (os.cpu_count() or 2) // 2)))
    embs = {MODEL: OnnxEmbedder(ort, files[1][1], dino_input, "last_hidden_state", cls_token=True)}
    if os.path.isfile(label.MODEL) and label._sha(label.MODEL) == label.MODEL_SHA:
        embs[SIGLIP_MODEL] = OnnxEmbedder(ort, label.MODEL, siglip_input, "pooler_output", cls_token=False)
    return AnimalDetector(cv2, files[0][1], classes()), embs


def truth_labels(c, spec, reader=None):
    """`pets eval`'s truth, {detection id: name}: your names in the store (named clusters and confirmed
    suggestions; never a suggestion), plus Pets|<Name> and Calibrate|pets|<Name> sidecar tags on files with
    exactly one pet. A detection given two different names is left out."""
    out, bad = {}, set()
    for part in (recog.owner_labels(c, spec), recog.tag_labels(c, spec, spec.prefix, reader),
                 recog.calibration_labels(c, spec, reader)):
        for det, name in part.items():
            if out.get(det, name).casefold() != name.casefold():
                bad.add(det)
            out.setdefault(det, name)
    return {d: n for d, n in out.items() if d not in bad}


def spec(state=None, cfg=None):
    cfg = cfg or config.get()
    return recog.Kind(kind="pet", command="pets", prefix="Pets|", model=MODEL, link=LINK, same=SAME,
                      models=model_files(state), engine=engine, noun="pet", max_side=MAX_SIDE,
                      min_frames=cfg.get("pet_min_frames", 3), eval_labels=truth_labels,
                      date_k=cfg.get("pet_date_neighbours", DATE_K))


def main(argv=None, prog="media-intake pets"):
    return recog.main(spec(), argv, prog)


if __name__ == "__main__":
    sys.exit(main())
