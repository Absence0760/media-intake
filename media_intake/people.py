#!/usr/bin/env python3
"""`media-intake people`: recognise people's faces in the library, fully offline (OpenCV YuNet + SFace, CPU).

  media-intake people scan [--root DIR]... [--limit N] [FILE...]
  media-intake people cluster [--link 0.45]
  media-intake people clusters [--min 2] [--unnamed]
  media-intake people name CLUSTER "Name" | merge CLUSTER OTHER... | ignore CLUSTER... | unname CLUSTER...
  media-intake people suggestions | confirm CLUSTER... [--det ID] [--all] | reject ...
  media-intake people take-out DET... | put-back DET...
  media-intake people tag [--dry-run]           (People|<Name> into XMP sidecars)
  media-intake people find "Name" [--alone] [--min-box 0.02] [--suggested]
  media-intake people people                    (names and file counts)
  media-intake people eval                      (Calibrate|people|<Name> tags; read-only)

Runs in the venv (cv2, numpy). Off until `bash setup.sh --recognition` fetches the two models: YuNet
(face_detection_yunet_2023mar.onnx, MIT) and SFace (face_recognition_sface_2021dec.onnx, Apache-2.0), both
from opencv/opencv_zoo at the commit pinned in media_intake/pins.json, sha256-checked by setup.sh, doctor
and here before they load. Nothing is downloaded at run time.

scan: YuNet at two scales (long side 640 and 1600 px: it is trained on faces of about 10 to 300 px), faces
with score >= 0.9 and a side >= 48 px; SFace turns each aligned face into a 128-d vector. cluster links at
cosine 0.45 (stricter than SFace's 0.363 same-person threshold, so one ambiguous face can't chain two people)
and merges clusters whose average pair passes 0.363. Store, names, suggestions, tags and erase: recog.py.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config  # noqa: E402
import recog  # noqa: E402

with open(os.path.join(HERE, "pins.json")) as _f:
    ZOO = json.load(_f)["opencv_zoo"]  # the single source of the pins (setup.sh, doctor)
YUNET_FILE = "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
SFACE_FILE = "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
MODEL = "sface-2021dec"       # the embeddings' model name in the store
SCALES = (640, 1600)          # long side of each detection pass
SCORE_MIN = 0.9               # YuNet score (OpenCV's face_detect.py sample default)
NMS_IOU = 0.3                 # same sample default, inside YuNet; across scales below
MIN_FACE_PX = 48              # in the detection image: smaller faces give weak vectors (SFace input is 112 px)
SAME = 0.363                  # SFace cosine threshold (OpenCV dnn_face tutorial, LFW 99.60 %)
LINK = 0.45                   # stage-1 link, stricter than SAME (uncalibrated)


def models_dir(state=None):
    return os.path.join(state or config.get()["state"], "models", f"opencv-zoo-{ZOO['commit'][:7]}")


def model_files(state=None):
    """[(what, path, sha256)] of the two models, for setup.sh's layout, doctor and the refusal."""
    d = models_dir(state)
    return [(what, os.path.join(d, os.path.basename(f)), ZOO["files"][f]["sha256"])
            for what, f in (("YuNet face detector", YUNET_FILE), ("SFace face recogniser", SFACE_FILE))]


def check_models(files=None):
    import label
    for what, p, want in files or model_files():
        if not os.path.isfile(p) or label._sha(p) != want:
            sys.exit(f"media-intake people: {what} missing or its checksum is wrong ({p}); "
                     "run bash setup.sh --recognition")


def iou(a, b):
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter) if inter else 0.0


def nms(found, thr=NMS_IOU):
    """Across scales: keep the higher-scored of two boxes overlapping by IoU > thr. found: (box, score, ...)."""
    keep = []
    for f in sorted(found, key=lambda f: -f[1]):
        if all(iou(f[0], k[0]) <= thr for k in keep):
            keep.append(f)
    return keep


class FaceDetector:
    """YuNet through cv2.FaceDetectorYN. detect(rgb) -> [(box, score, None, aligned 112 px face)]."""

    def __init__(self, cv2, yunet, recogniser):
        self.cv2, self.rec = cv2, recogniser
        self.det = cv2.FaceDetectorYN.create(yunet, "", (320, 320), SCORE_MIN, NMS_IOU, 5000,
                                             cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)

    def detect(self, rgb):
        import numpy as np
        cv2 = self.cv2
        H, W = rgb.shape[:2]
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        found = []
        for side in SCALES:
            s = min(1.0, side / max(H, W))
            img = bgr if s == 1.0 else cv2.resize(bgr, (max(1, round(W * s)), max(1, round(H * s))),
                                                  interpolation=cv2.INTER_AREA)
            self.det.setInputSize((img.shape[1], img.shape[0]))
            _, det = self.det.detect(img)
            for row in (det if det is not None else []):
                x, y, w, h = (float(v) for v in row[:4])
                score = float(row[14])  # row: x, y, w, h, 5 landmarks (x, y), score
                if score < SCORE_MIN or min(w, h) < MIN_FACE_PX:
                    continue
                found.append(((x / s, y / s, w / s, h / s), score, None, self.rec.alignCrop(img, row)))
            if s == 1.0:
                break  # the image is already smaller than this pass; larger passes add nothing
        return nms(found)


class FaceEmbedder:
    """SFace through cv2.FaceRecognizerSF: embed(aligned face) -> 128-d vector."""

    def __init__(self, recogniser):
        self.rec = recogniser

    def embed(self, crop):
        return self.rec.feature(crop).reshape(-1)


def engine():
    """(FaceDetector, {MODEL: FaceEmbedder}), after checking both models' sha256. CPU only."""
    import cv2
    files = model_files()
    check_models(files)
    cv2.setNumThreads(max(1, min(4, (os.cpu_count() or 2) // 2)))
    rec = cv2.FaceRecognizerSF.create(files[1][1], "", cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
    return FaceDetector(cv2, files[0][1], rec), {MODEL: FaceEmbedder(rec)}


def spec(state=None):
    return recog.Kind(kind="face", command="people", prefix="People|", model=MODEL, link=LINK, same=SAME,
                      models=model_files(state), engine=engine, noun="face", max_side=max(SCALES))


def main(argv=None, prog="media-intake people"):
    return recog.main(spec(), argv, prog)


if __name__ == "__main__":
    sys.exit(main())
