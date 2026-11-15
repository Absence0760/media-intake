#!/usr/bin/env bash
# One-time setup for media-intake. Safe to re-run; offline once everything is
# downloaded (each run re-verifies the checksums).
#   bash setup.sh                 venv, SigLIP vision model, GeoNames, ~/.local/bin/media-intake launcher
#   bash setup.sh --refresh-geo   accept a new GeoNames snapshot (rewrites <state>/geo-pins.txt)
#   bash setup.sh --build-labels  also fetch the text model + tokenizer (semantic search, and
#                                 regenerating media_intake/labels.npz when labels.yaml or the SigLIP pin changes)
#   bash setup.sh --no-launcher   don't write ~/.local/bin/media-intake
# System packages need sudo, so install them yourself first (README, Install). The first
# step checks the few this script itself needs and prints the exact `sudo dnf install -y`
# line; the last step runs `media-intake doctor`, which does the same for everything else.
# Each step prints a `==> [n/N]` heading; a failure names its step and the likely fix,
# and the run ends with a summary. Colour only on a terminal and without NO_COLOR.
# -E: the ERR trap (on_err) also fires inside functions, so a failure anywhere names its step.
set -Eeuo pipefail
here=$(cd "$(dirname "$0")" && pwd)
pkg=$here/media_intake

refresh_geo=0 build_labels=0 launcher=1
for a in "$@"; do
  case $a in
    --refresh-geo) refresh_geo=1 ;;
    --build-labels) build_labels=1 ;;
    --no-launcher) launcher=0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

# Colour: stdout and stderr decide separately (one may be piped); https://no-color.org.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  c_head=$'\e[1;34m' c_ok=$'\e[32m' c_warn=$'\e[33m' c_off=$'\e[0m'
else
  c_head='' c_ok='' c_warn='' c_off=''
fi
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then e_err=$'\e[1;31m' e_off=$'\e[0m'; else e_err='' e_off=''; fi

step_no=0 step_total=5 step_name='start' step_hint='' fail_hint=''
[ "$build_labels" = 1 ] && step_total=$((step_total + 1))
[ "$launcher" = 1 ] && step_total=$((step_total + 1))
summary=()

# step NAME HINT: print the heading; HINT is what on_err suggests if this step fails.
step() {
  step_no=$((step_no + 1)) step_name=$1 step_hint=$2 fail_hint=''
  printf '\n%s==> [%d/%d] %s%s\n' "$c_head" "$step_no" "$step_total" "$step_name" "$c_off"
}
# note WHAT STATUS WHERE: one summary line.
note() { summary+=("$(printf '%-24s %-16s %s' "$1" "$2" "${3:-}")"); }
warn() { printf '%s%s%s\n' "$c_warn" "$1" "$c_off" >&2; }

# Report the failed step with its fix, and stop. Command substitutions run in subshells,
# which inherit the trap: only the main shell reports, once.
main_pid=$BASHPID
report() {
  printf '\n%s==> FAILED at step [%d/%d] %s%s\n' "$e_err" "$step_no" "$step_total" "$step_name" "$e_off" >&2
  printf '%s\n' "${fail_hint:-${step_hint:-see the error above}}" | sed 's/^/    /' >&2
  printf '    Nothing after this step ran; fix the above and re-run: bash %s\n' "$here/setup.sh" >&2
}
on_err() {
  local rc=$1 line=$2
  [ "$BASHPID" = "$main_pid" ] || return 0
  trap - ERR
  echo "setup.sh: command failed (exit $rc) at line $line" >&2
  report
  exit "$rc"
}
trap 'on_err $? $LINENO' ERR
# die HINT: fail the current step on purpose.
die() { trap - ERR; fail_hint=$1; report; exit 1; }

# download URL DEST: the one place this script uses the network. Every caller hashes DEST
# right after: fetch() against a pin, the GeoNames first-use path by recording it.
download() {
  local url=$1 dest=$2 rc=0
  curl -fsSL --proto '=https' --tlsv1.2 -o "$dest.part" "$url" || rc=$?
  if [ "$rc" != 0 ]; then
    rm -f -- "$dest.part"
    case $rc in
      5|6|7|28|35|52|56) fail_hint="No network: couldn't reach $url (curl exit $rc). Connect and re-run; nothing was installed from this download." ;;
      22) fail_hint="The server refused $url (HTTP error, curl exit 22). Nothing was installed from it; re-run later, and if it persists the pinned URL may have moved (CONTRIBUTING.md, Pins)." ;;
      *) fail_hint="Download of $url failed (curl exit $rc). Nothing was installed from it; re-run when the network is back." ;;
    esac
    return "$rc"
  fi
  mv "$dest.part" "$dest"
}

# fetch URL DEST SHA256: download once over https, verify, keep. Re-runs only verify.
# Sets fetched to "downloaded" or "already present" for the summary, fetch_err=mismatch on a bad hash.
fetch() {
  local url=$1 dest=$2 want=$3
  fetched='already present' fetch_err=''
  if [ ! -f "$dest" ]; then
    mkdir -p "$(dirname "$dest")"
    download "$url" "$dest" || return
    fetched=downloaded
  fi
  local got
  got=$(sha256sum "$dest" | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    echo "CHECKSUM MISMATCH $dest: got $got, want $want (deleted)" >&2
    rm -f -- "$dest"
    fetch_err=mismatch
    fail_hint="$(basename "$dest") didn't match its sha256 pin, so it was deleted and nothing was installed.
Re-run to download it again (a cut-off or altered download). If it keeps failing, the
file upstream changed: open an issue; never skip or edit the check."
    return 1
  fi
  printf '%sok%s  %s  sha256:%s  (%s)\n' "$c_ok" "$c_off" "$(basename "$dest")" "$want" "$fetched"
}

# pip_install ARGS...: venv pip, always --require-hashes; on failure name the likely cause.
pip_install() {
  local log rc=0
  log=$(mktemp)
  "$state/venv/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps "$@" 2>"$log" || rc=$?
  cat "$log" >&2
  if [ "$rc" != 0 ]; then
    if grep -qi 'do not match the hashes' "$log"; then
      fail_hint="A wheel didn't match its sha256 pin in the requirements file, so pip installed nothing from it.
Re-run to download it again; if it keeps failing, open an issue. Never drop --require-hashes."
    elif grep -qiE 'network|connection|resolve|temporary failure|timed out' "$log"; then
      fail_hint="No network: pip couldn't reach PyPI. Connect and re-run; the pinned wheels are only fetched here."
    fi
  fi
  rm -f -- "$log"
  return "$rc"
}

# Model pins live only in media_intake/pins.json (scripts/update-pins.py regenerates it).
# pin KEY...: the value under those keys, e.g. `pin siglip revision`.
pin() {
  python3 -I -c 'import functools, json, sys
print(functools.reduce(lambda v, k: v[k], sys.argv[2:], json.load(open(sys.argv[1]))))' "$pkg/pins.json" "$@"
}

step 'System packages and state dir' 'Check the error above.'
need=()
for c in python3:python3 curl:curl sha256sum:coreutils awk:gawk; do
  command -v "${c%%:*}" >/dev/null || need+=("${c#*:}")
done
[ "${#need[@]}" = 0 ] || die "Missing commands this script needs. Install them, then re-run:
sudo dnf install -y ${need[*]}"
# The venv reuses these from Fedora (--system-site-packages); same names doctor uses.
need=()
for m in PIL:python3-pillow numpy:python3-numpy yaml:python3-pyyaml; do
  python3 -I -c "import ${m%%:*}" 2>/dev/null || need+=("${m#*:}")
done
[ "${#need[@]}" = 0 ] || die "Missing Python packages the venv reuses from Fedora. Install them, then re-run:
sudo dnf install -y ${need[*]}"
# The state dir from config.toml (or MEDIA_INTAKE_STATE), via the same parser the tools use.
step_hint="Couldn't read the state dir from your config: check config.toml (docs/configuration.md) or MEDIA_INTAKE_STATE."
state=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.get()["state"])' "$pkg")
if [ -d "$state" ]; then note 'state dir' 'already present' "$state"; else note 'state dir' 'created' "$state"; fi
mkdir -p "$state" && chmod 700 "$state"
echo "state dir $state"

step 'Python venv' "Re-run; if it fails again, \`media-intake doctor\` (after setup) or the error above names what's missing."
# --system-site-packages: reuse Fedora's Pillow, numpy, PyYAML and python3-zxing-cpp.
if [ -x "$state/venv/bin/python" ]; then
  echo "venv already present: $state/venv"
  note 'venv' 'already present' "$state/venv"
else
  python3 -m venv --system-site-packages "$state/venv"
  note 'venv' 'created' "$state/venv"
fi
pip_install -r "$here/requirements.txt"
step_hint="The pinned wheels installed but don't import; delete $state/venv and re-run. If it persists, open an issue with the error above."
"$state/venv/bin/python" -I -c "import nudenet, onnxruntime, scipy; print('nudenet ok, onnxruntime', onnxruntime.__version__, 'scipy', scipy.__version__)"

# SigLIP vision encoder (label.py). Apache-2.0, google/siglip-base-patch16-224 via Xenova's ONNX export.
step 'SigLIP vision model' "media_intake/pins.json is missing or broken: restore it from git (scripts/update-pins.py regenerates it)."
# Assignments, so a broken pins.json stops the script (set -e) before fetch() compares anything.
rev=$(pin siglip revision)
repo=$(pin siglip repo)
sha_vision=$(pin siglip files onnx/vision_model_quantized.onnx)
sha_text=$(pin siglip files onnx/text_model.onnx)
sha_tokenizer=$(pin siglip files tokenizer.json)
hf=https://huggingface.co/$repo/resolve/$rev
models=$state/models/siglip-base-patch16-224-${rev:0:7}
fetch "$hf/onnx/vision_model_quantized.onnx" "$models/vision_model_quantized.onnx" "$sha_vision"
note 'SigLIP vision model' "$fetched" "$models"

# GeoNames (geocode.py), CC-BY 4.0. GeoNames publishes no checksums and updates the
# files daily, so your first download is recorded in <state>/geo-pins.txt (trust on first
# use) and every later run, and geocode.py at tag time, verify it.
step 'GeoNames' "Check the error above; geo-pins.txt is in $state."
geo=$state/geo
pins=$state/geo-pins.txt
[ -f "$pins" ] || printf '# sha256  file  downloaded  (GeoNames snapshot; setup.sh --refresh-geo replaces it)\n' > "$pins"
geo_status=()
for f in cities1000.zip admin1CodesASCII.txt countryInfo.txt; do
  url=https://download.geonames.org/export/dump/$f
  pin=$(awk -v f="$f" '$2 == f {print $1}' "$pins")
  if [ "$refresh_geo" = 1 ] || [ -z "$pin" ]; then
    rm -f -- "$geo/$f"
    mkdir -p "$geo"
    download "$url" "$geo/$f"
    sha=$(sha256sum "$geo/$f" | cut -d' ' -f1)
    awk -v f="$f" '$2 != f' "$pins" > "$pins.tmp" && mv "$pins.tmp" "$pins"
    echo "$sha  $f  $(date +%F)" >> "$pins"
    echo "RECORDED $f sha256:$sha in $pins"
    geo_status+=("$f recorded")
  elif [ ! -f "$geo/$f" ]; then
    fetch "$url" "$geo/$f" "$pin" || {
      [ "$fetch_err" = mismatch ] || die "$fail_hint"
      die "GeoNames changed since the pin in $pins, so nothing was installed.
Review the change, then accept the new snapshot: bash setup.sh --refresh-geo"
    }
    geo_status+=("$f $fetched")
  else
    fetch "$url" "$geo/$f" "$pin"
    geo_status+=("$f $fetched")
  fi
done
case " ${geo_status[*]} " in
  *recorded*|*downloaded*) note 'GeoNames' 'downloaded' "$geo" ;;
  *) note 'GeoNames' 'already present' "$geo" ;;
esac

if [ "$build_labels" = 1 ]; then
  step 'Text model and label embeddings' 'Check the error above; the build dir is $MEDIA_INTAKE_BUILD or ~/.cache/media-intake-build.'
  build=${MEDIA_INTAKE_BUILD:-$HOME/.cache/media-intake-build}
  pip_install --target "$build/site" --upgrade -r "$here/requirements-labels.txt"
  fetch "$hf/onnx/text_model.onnx" "$build/text_model.onnx" "$sha_text"
  fetch "$hf/tokenizer.json" "$build/tokenizer.json" "$sha_tokenizer"
  "$state/venv/bin/python" -I "$pkg/build_label_embeddings.py" "$build"
  note 'label embeddings' 'built' "$pkg/labels.npz"
fi

if [ "$launcher" = 1 ]; then
  step 'Launcher' 'Check that ~/.local/bin is writable.'
  bin=$HOME/.local/bin/media-intake
  want=$(printf '#!/bin/sh\n# media-intake launcher (written by %s)\nexec python3 -I %s "$@"\n' "$here/setup.sh" "$pkg/cli.py")
  if [ -e "$bin" ] && [ "$(cat "$bin")" != "$want" ]; then
    warn "NOT WRITTEN $bin: a different file is there (remove it, or use --no-launcher)"
    note 'launcher' 'NOT WRITTEN' "$bin (a different file is there)"
  elif [ ! -e "$bin" ]; then
    mkdir -p "$(dirname "$bin")"
    printf '%s\n' "$want" > "$bin" && chmod 755 "$bin"
    printf '%sok%s  launcher %s\n' "$c_ok" "$c_off" "$bin"
    note 'launcher' 'written' "$bin"
  else
    printf '%sok%s  launcher %s (already present)\n' "$c_ok" "$c_off" "$bin"
    note 'launcher' 'already present' "$bin"
  fi
fi

# The full check (system packages, model checksums, Ollama digest), with a dnf line for gaps.
step 'media-intake doctor' ''
if python3 -I "$pkg/cli.py" doctor; then
  note 'doctor' 'all checks passed'
else
  warn "doctor lists what is still missing (above)"
  note 'doctor' 'gaps listed above'
fi

trap - ERR
printf '\n%s==> Summary%s\n' "$c_head" "$c_off"
printf '  %s\n' "${summary[@]}"
printf '\nnext: media-intake doctor\n'
