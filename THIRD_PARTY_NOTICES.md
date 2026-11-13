# Third-party notices

media-intake itself is licensed under the GNU Affero General Public License
v3.0 (`LICENSE`). It ships no third-party code or model weights in this
repository except the label text embeddings in `media_intake/labels.npz`
(computed locally from `labels.yaml` with the SigLIP text model, below).
Everything else is installed by you: Python wheels by `setup.sh` (hash-pinned
in `requirements.txt`), models and data downloaded by `setup.sh` (sha256-
pinned), system programs from your distribution.

| Component | Use here | Licence | How it arrives |
| --- | --- | --- | --- |
| [NudeNet](https://github.com/notAI-tech/NudeNet) 3.4.2 | Nudity detector in `scan_sensitive.py` (imported) | AGPL-3.0 per the project's repository LICENSE; the PyPI metadata says MIT. This project is AGPL-3.0, which satisfies either. | pip wheel, hash-pinned |
| [SigLIP](https://huggingface.co/google/siglip-base-patch16-224) base patch16-224 (Google), ONNX export by [Xenova](https://huggingface.co/Xenova/siglip-base-patch16-224) | Labels, trash hints, semantic search | Apache-2.0 | downloaded by `setup.sh`, sha256-pinned |
| [Qwen3-VL](https://ollama.com/library/qwen3-vl) 2B (`qwen3-vl:2b`) | Optional trash second opinion in `vlm_trash.py` | Apache-2.0 | pulled by you with Ollama; not shipped; pinned by digest |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | Runs NudeNet and SigLIP | MIT | pip wheel, hash-pinned |
| [OpenCV](https://opencv.org/) (`opencv-python-headless`) | NudeNet image handling | Apache-2.0 | pip wheel, hash-pinned |
| [SciPy](https://scipy.org/) | Nearest-place lookup (`cKDTree`) in `geocode.py` | BSD-3-Clause | pip wheel, hash-pinned |
| [protobuf](https://github.com/protocolbuffers/protobuf), [flatbuffers](https://github.com/google/flatbuffers) | ONNX Runtime dependencies | BSD-3-Clause, Apache-2.0 | pip wheels, hash-pinned |
| [Hugging Face tokenizers](https://github.com/huggingface/tokenizers) | Only for `setup.sh --build-labels` (search queries, rebuilding `labels.npz`) | Apache-2.0 | pip wheel into a build cache, hash-pinned |
| [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) | QR / barcode detector (2FA and payment codes) | Apache-2.0 | `python3-zxing-cpp` from your distribution |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | Text detector for IDs, cards, documents | Apache-2.0 | `tesseract` from your distribution, called as a program |
| [ExifTool](https://exiftool.org/) | Reads metadata, writes XMP sidecars | GPL-1.0-or-later or Artistic License (Perl terms) | `perl-Image-ExifTool` from your distribution, called as a program |
| [FFmpeg](https://ffmpeg.org/), [GStreamer](https://gstreamer.freedesktop.org/) | Video frames, metadata stripping | LGPL-2.1-or-later (with GPL parts depending on the build) | from your distribution, called as programs |
| Pillow, NumPy, PyYAML | Images, arrays, labels.yaml | MIT-CMU (HPND), BSD-3-Clause, MIT | from your distribution |
| [GeoNames](https://www.geonames.org/) `cities1000`, `admin1CodesASCII`, `countryInfo` | Offline reverse geocoding (place tags) | CC BY 4.0 | downloaded by `setup.sh` from download.geonames.org; your first download is pinned in `<state>/geo-pins.txt` |

## GeoNames attribution

Place names in tags come from GeoNames (https://www.geonames.org/), licensed
under a Creative Commons Attribution 4.0 License
(https://creativecommons.org/licenses/by/4.0/). The data is used unmodified
except for selecting the nearest populated place; no warranty is given by
GeoNames as to its accuracy.

## Model licences

Check a model's licence before you swap in another one: `vlm_trash.py` pins
`qwen3-vl:2b` because it is Apache-2.0; some other small vision models carry
non-commercial licences.
