"""`media-intake`: one entry point for every tool in this package.

  media-intake <intake command> ...      stage, plan, scan, apply, tag, erase, trash, gallery, ... (intake.py)
  media-intake queue add|run|status|offload ...   external-drive process queue (drive_queue.py)
  media-intake review BATCH ...          local review page for a batch (review_ui.py)
  media-intake index                     build or update the semantic search index  (venv)
  media-intake search "TEXT" ...         semantic search                             (venv)
  media-intake similar FILE ...          nearest neighbours of a file                (venv)
  media-intake vlm-trash ...             local vision-model second opinion on trash  (venv, Ollama)
  media-intake doctor                    check system packages, venv, model checksums, Ollama digest, config

`media-intake <command> --help` shows each command's options. Commands marked
(venv) run in the venv setup.sh builds; the rest need only the system Python.
"""
import importlib, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

VENV_SCRIPTS = {"index": ("semantic.py", ["index"]), "search": ("semantic.py", ["search"]),
                "similar": ("semantic.py", ["similar"]), "vlm-trash": ("vlm_trash.py", [])}
# Each thing doctor checks, with the Fedora package that provides it: the one source for the
# `sudo dnf install ...` lines doctor prints. Required unless listed in OPTIONAL.
SYSTEM_TOOLS = {"exiftool": "perl-Image-ExifTool", "tesseract": "tesseract", "ffmpeg": "ffmpeg-free",
                "ffprobe": "ffmpeg-free", "gio": "glib2", "findmnt": "util-linux-core",
                "gst-inspect-1.0": "gstreamer1"}
SYSTEM_MODULES = {"zxingcpp": "python3-zxing-cpp", "PIL": "python3-pillow", "numpy": "python3-numpy",
                  "yaml": "python3-pyyaml"}
TESSERACT_ENG = "tesseract-langpack-eng"
HEVC_DECODERS, HEVC_PACKAGE = ("vah265dec", "vulkanh265dec"), "gstreamer1-plugins-bad-free"
MTP_BACKEND, MTP_PACKAGE = "/usr/libexec/gvfsd-mtp", "gvfs-mtp"
XDG_OPEN, XDG_PACKAGE = "xdg-open", "xdg-utils"  # the gallery's "Open in the system player" (POST /api/open)
OPTIONAL = {"gstreamer1", HEVC_PACKAGE, MTP_PACKAGE, XDG_PACKAGE}  # Fedora packages, as in the maps above


def venv_python():
    return os.path.join(config.get()["state"], "venv", "bin", "python")


class Probe:
    """What doctor looks at on this machine. Tests pass a fake with the same methods."""

    def which(self, tool):
        return shutil.which(tool)

    def output(self, cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout + r.stderr

    def importable(self, module):
        try:
            importlib.import_module(module)
            return True
        except ImportError:
            return False

    def exists(self, path):
        return os.path.exists(path)

    def sha256(self, path):
        import label
        return label._sha(path)

    def ollama_tags(self):
        """GET /api/tags from the local Ollama: loopback only, no proxy (vlm_trash's client)."""
        import vlm_trash
        return vlm_trash._call(vlm_trash.HOST, "/api/tags", timeout=5)


def _short(h):
    return str(h)[:12]


def doctor(probe=None, out=print):
    """Report what is installed; exit 1 if something required is missing or a pinned file is wrong."""
    probe = probe or Probe()
    ok, need, want = True, [], []   # need/want: Fedora packages missing (required / optional)

    def report(status, text):
        out(f"{status:8} {text}")

    def missing(package, text):
        nonlocal ok
        optional = package in OPTIONAL
        (want if optional else need).append(package)
        ok &= optional
        report("optional" if optional else "MISSING", f"{text} ({package})")

    try:
        cfg = config.load()
        report("ok", f"config {cfg['path']}" + ("" if os.path.isfile(cfg["path"]) else " (absent: defaults)"))
    except config.ConfigError as e:
        report("BAD", f"config: {e}")
        return 1
    for k in ("library", "videos", "state"):
        report("ok" if os.path.isdir(cfg[k]) else "absent", f"{k} {cfg[k]}")
    for p in cfg["projects"].values():
        report("ok" if os.path.isdir(p["folder"]) else "absent", f"project {p['name']} {p['folder']}")

    for tool, package in SYSTEM_TOOLS.items():
        if probe.which(tool):
            report("ok", tool)
        else:
            missing(package, tool)
    for module, package in SYSTEM_MODULES.items():
        if probe.importable(module):
            report("ok", package)
        else:
            missing(package, f"python module {module}")
    if probe.which("tesseract"):
        if "eng" in probe.output(["tesseract", "--list-langs"]).split():
            report("ok", "tesseract eng language data")
        else:
            missing(TESSERACT_ENG, "tesseract eng language data")
    if probe.which("gst-inspect-1.0"):
        plugins = probe.output(["gst-inspect-1.0"])
        if any(d in plugins for d in HEVC_DECODERS):
            report("ok", "GStreamer HEVC decoder")
        else:
            missing(HEVC_PACKAGE, "GStreamer HEVC decoder (vah265dec / vulkanh265dec; most phone video)")
    if probe.exists(MTP_BACKEND):
        report("ok", "gvfs MTP backend")
    else:
        missing(MTP_PACKAGE, "gvfs MTP backend (Android phones)")
    if probe.which(XDG_OPEN):
        report("ok", XDG_OPEN)
    else:
        missing(XDG_PACKAGE, f"{XDG_OPEN} (the gallery's Open in the system player button)")

    vp = venv_python()
    have = probe.exists(vp)
    report("ok" if have else "MISSING", f"venv {vp}" + ("" if have else " (bash setup.sh)"))
    ok &= have
    ok &= _check_models(probe, report)
    geo = os.path.join(cfg["state"], "geo", "cities1000.zip")
    have = probe.exists(geo)
    report("ok" if have else "MISSING", "GeoNames data" + ("" if have else " (bash setup.sh)"))
    ok &= have
    ok &= _check_ollama(probe, report)

    if need:
        out("Install what's missing:  sudo dnf install " + " ".join(dict.fromkeys(need)))
    if want:
        out("Optional:  sudo dnf install " + " ".join(dict.fromkeys(want)))
    return 0 if ok else 1


def _check_models(probe, report):
    """Each pinned SigLIP file against media_intake/pins.json. The vision encoder is required;
    the text model and tokenizer (setup.sh --build-labels, semantic search) only when present.
    A present file with the wrong sha256 always fails."""
    import label, semantic
    files = label.PINS["siglip"]["files"]
    checks = [("SigLIP vision model", label.MODEL, files["onnx/vision_model_quantized.onnx"], True),
              ("SigLIP text model", os.path.join(semantic.BUILD, "text_model.onnx"), files["onnx/text_model.onnx"], False),
              ("SigLIP tokenizer", os.path.join(semantic.BUILD, "tokenizer.json"), files["tokenizer.json"], False)]
    ok = True
    for name, path, pinned, required in checks:
        if not probe.exists(path):
            ok &= not required
            report("MISSING" if required else "optional",
                   f"{name}" + (" (bash setup.sh)" if required else " not built (bash setup.sh --build-labels: semantic search)"))
            continue
        got = probe.sha256(path)
        if got == pinned:
            report("ok", f"{name} sha256:{_short(pinned)}")
        else:
            ok = False
            report("BAD", f"{name} {path}: sha256 {_short(got)}, pinned {_short(pinned)} "
                          "(bash setup.sh deletes it; run it again to fetch the pinned file)")
    return ok


def _check_ollama(probe, report):
    """The pinned vision model in the local Ollama (127.0.0.1 only). Optional: absent Ollama,
    stopped Ollama or a model not pulled is reported, not failed; a different digest fails."""
    import label
    model, pinned = label.PINS["ollama"]["model"], label.PINS["ollama"]["digest"]
    if not probe.which("ollama"):
        report("optional", "ollama not installed (only vlm-trash needs it; https://ollama.com/)")
        return True
    version = (probe.output(["ollama", "--version"]).strip().splitlines() or ["ollama, version unknown"])[-1]
    report("ok", version)
    try:
        tags = probe.ollama_tags()
    except (OSError, ValueError) as e:
        report("optional", f"ollama not answering on 127.0.0.1 ({type(e).__name__}); can't check the {model} digest")
        return True
    for m in tags.get("models", []):
        if m.get("name") == model or m.get("model") == model:
            if m.get("digest") == pinned:
                report("ok", f"ollama {model} digest {_short(pinned)}")
                return True
            report("BAD", f"ollama {model} digest {_short(m.get('digest'))}, pinned {_short(pinned)} "
                          "(vlm-trash refuses it; see CONTRIBUTING.md, Pins)")
            return False
    report("optional", f"ollama {model} not pulled (ollama pull {model}; only vlm-trash needs it)")
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        import intake
        print(__doc__.strip() + "\n\nIntake commands:\n")
        intake.build_parser("media-intake").print_help()
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "doctor":
        return doctor()
    if cmd == "queue":
        import drive_queue
        drive_queue.main(rest, prog="media-intake queue")
        return 0
    if cmd == "review":
        import review_ui
        return review_ui.main(rest)
    if cmd in VENV_SCRIPTS:
        script, pre = VENV_SCRIPTS[cmd]
        vp = venv_python()
        if not os.path.exists(vp):
            sys.exit(f"no venv at {vp}: run bash setup.sh first")
        os.execv(vp, [vp, "-I", os.path.join(HERE, script), *pre, *rest])
    import intake
    intake.main(argv, prog="media-intake")
    return 0


if __name__ == "__main__":
    sys.exit(main())
