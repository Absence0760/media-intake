#!/usr/bin/env bash
# One-time setup for media-intake. Safe to re-run; offline once everything is
# downloaded (each run re-verifies the checksums).
#   bash setup.sh                 venv, SigLIP vision model, GeoNames, ~/.local/bin/media-intake launcher
#   bash setup.sh --refresh-geo   accept a new GeoNames snapshot (rewrites <state>/geo-pins.txt)
#   bash setup.sh --build-labels  also fetch the text model + tokenizer (semantic search, and
#                                 regenerating media_intake/labels.npz when labels.yaml changes)
#   bash setup.sh --no-launcher   don't write ~/.local/bin/media-intake
# System packages need sudo, so install them yourself first (README, Install). The last
# step runs `media-intake doctor`, which prints the exact `sudo dnf install` line for
# anything still missing.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
pkg=$here/media_intake
# The state dir from config.toml (or MEDIA_INTAKE_STATE), via the same parser the tools use.
state=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.get()["state"])' "$pkg")
refresh_geo=0 build_labels=0 launcher=1
for a in "$@"; do
  case $a in
    --refresh-geo) refresh_geo=1 ;;
    --build-labels) build_labels=1 ;;
    --no-launcher) launcher=0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
mkdir -p "$state" && chmod 700 "$state"
# --system-site-packages: reuse Fedora's Pillow, numpy, PyYAML and python3-zxing-cpp.
[ -x "$state/venv/bin/python" ] || python3 -m venv --system-site-packages "$state/venv"
"$state/venv/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps -r "$here/requirements.txt"
"$state/venv/bin/python" -I -c "import nudenet, onnxruntime, scipy; print('nudenet ok, onnxruntime', onnxruntime.__version__, 'scipy', scipy.__version__)"

# fetch URL DEST SHA256: download once over https, verify, keep. Re-runs only verify.
fetch() {
  local url=$1 dest=$2 want=$3
  if [ ! -f "$dest" ]; then
    mkdir -p "$(dirname "$dest")"
    curl -fsSL --proto '=https' --tlsv1.2 -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  local got
  got=$(sha256sum "$dest" | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    echo "CHECKSUM MISMATCH $dest: got $got, want $want (deleted)" >&2
    rm -f -- "$dest"
    return 1
  fi
  echo "ok  $(basename "$dest")  sha256:$want"
}

# Model pins live only in media_intake/pins.json (scripts/update-pins.py regenerates it).
# pin KEY...: the value under those keys, e.g. `pin siglip revision`.
pin() {
  python3 -I -c 'import functools, json, sys
print(functools.reduce(lambda v, k: v[k], sys.argv[2:], json.load(open(sys.argv[1]))))' "$pkg/pins.json" "$@"
}

# SigLIP vision encoder (label.py). Apache-2.0, google/siglip-base-patch16-224 via Xenova's ONNX export.
# Assignments, so a broken pins.json stops the script (set -e) before fetch() compares anything.
rev=$(pin siglip revision)
repo=$(pin siglip repo)
sha_vision=$(pin siglip files onnx/vision_model_quantized.onnx)
sha_text=$(pin siglip files onnx/text_model.onnx)
sha_tokenizer=$(pin siglip files tokenizer.json)
hf=https://huggingface.co/$repo/resolve/$rev
models=$state/models/siglip-base-patch16-224-${rev:0:7}
fetch "$hf/onnx/vision_model_quantized.onnx" "$models/vision_model_quantized.onnx" "$sha_vision"

# GeoNames (geocode.py), CC-BY 4.0. GeoNames publishes no checksums and updates the
# files daily, so your first download is recorded in <state>/geo-pins.txt (trust on first
# use) and every later run, and geocode.py at tag time, verify it.
geo=$state/geo
pins=$state/geo-pins.txt
[ -f "$pins" ] || printf '# sha256  file  downloaded  (GeoNames snapshot; setup.sh --refresh-geo replaces it)\n' > "$pins"
for f in cities1000.zip admin1CodesASCII.txt countryInfo.txt; do
  url=https://download.geonames.org/export/dump/$f
  pin=$(awk -v f="$f" '$2 == f {print $1}' "$pins")
  if [ "$refresh_geo" = 1 ] || [ -z "$pin" ]; then
    rm -f -- "$geo/$f"
    mkdir -p "$geo"
    curl -fsSL --proto '=https' --tlsv1.2 -o "$geo/$f.part" "$url"
    mv "$geo/$f.part" "$geo/$f"
    sha=$(sha256sum "$geo/$f" | cut -d' ' -f1)
    awk -v f="$f" '$2 != f' "$pins" > "$pins.tmp" && mv "$pins.tmp" "$pins"
    echo "$sha  $f  $(date +%F)" >> "$pins"
    echo "RECORDED $f sha256:$sha in $pins"
  elif [ ! -f "$geo/$f" ]; then
    fetch "$url" "$geo/$f" "$pin" || { echo "GeoNames changed since the pin: review, then setup.sh --refresh-geo" >&2; exit 1; }
  else
    fetch "$url" "$geo/$f" "$pin"
  fi
done

if [ "$build_labels" = 1 ]; then
  build=${MEDIA_INTAKE_BUILD:-$HOME/.cache/media-intake-build}
  "$state/venv/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps \
    --target "$build/site" --upgrade -r "$here/requirements-labels.txt"
  fetch "$hf/onnx/text_model.onnx" "$build/text_model.onnx" "$sha_text"
  fetch "$hf/tokenizer.json" "$build/tokenizer.json" "$sha_tokenizer"
  "$state/venv/bin/python" -I "$pkg/build_label_embeddings.py" "$build"
fi

if [ "$launcher" = 1 ]; then
  bin=$HOME/.local/bin/media-intake
  want=$(printf '#!/bin/sh\n# media-intake launcher (written by %s)\nexec python3 -I %s "$@"\n' "$here/setup.sh" "$pkg/cli.py")
  if [ -e "$bin" ] && [ "$(cat "$bin")" != "$want" ]; then
    echo "NOT WRITTEN $bin: a different file is there (remove it, or use --no-launcher)" >&2
  elif [ ! -e "$bin" ]; then
    mkdir -p "$(dirname "$bin")"
    printf '%s\n' "$want" > "$bin" && chmod 755 "$bin"
    echo "ok  launcher $bin"
  else
    echo "ok  launcher $bin"
  fi
fi

# The full check (system packages, model checksums, Ollama digest), with a dnf line for gaps.
python3 -I "$pkg/cli.py" doctor || echo "setup.sh finished; doctor lists what is still missing (above)" >&2
