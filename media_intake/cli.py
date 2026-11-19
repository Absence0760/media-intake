"""`media-intake`: copy photos and videos off a phone, camera, drive or laptop
folder into one library, set private-looking files aside for you to decide on,
and browse, search and back up the library. Everything runs on this machine.

usage: media-intake <command> [options]
       media-intake <command> --help    what it does, touches and deletes
       media-intake --version           the installed version

Commands that delete say DELETES. The words used (batch, source, backup drive,
quarantine, review folder, Keep, Trash, Erase, ...): docs/words.md.

Import, in this order
  stage SOURCE --kind K     copy a phone, camera, drive or folder into a batch
  plan BATCH                find dates, folders and duplicates; moves nothing
  scan BATCH                flag private files (IDs, cards, nudity)
  apply BATCH               file into the library; flagged files to quarantine
  review BATCH              a page to Keep, Trash or Erase what was set aside
  tag BATCH                 write tags (place, event, labels) to sidecar files
  offload BATCH --phone P   DELETES the batch's files from the phone or camera
  quarantine BATCH          count what is in quarantine; changes nothing
  release BATCH FILE...     a false positive: quarantine -> library

Big folders and drives
  queue add|run|status|offload   import in resumable chunks, unattended
  zip-check ROOT            what export zips hold that you lack; changes nothing

Erase and trash
  erase BATCH               DELETES quarantined files and all their copies
  forget FILE...            DELETES library files the same way (found later)
  verify-gone [BATCH]       check erased files left no copy; changes nothing
  sweep-erased [BATCH]      DELETES copies erase missed, with --yes
  trash FILE...             DELETES junk (not private files), with --yes
  purge-review BATCH        DELETES a batch's review folder, with --yes
  suggest-trash             find Suggested junk; opens the review page
  not-trash FILE...         Keep suggested files: never suggest them again
  vlm-trash                 a vision model's opinion on junk (venv, Ollama)

Library
  gallery                   browse the library: Keep, Trash, Erase, tags
  find                      search the tags (event, place, project, tag)
  index                     build or update the search index (venv)
  search "TEXT"             find photos by a description (venv)
  similar FILE              find photos that look like this one (venv)
  tag-files FILE...         write tags again for library files
  places list|add|tag|...   your own named places (Home, ...) as tags
  check-dates [PATHS]       find files whose date looks wrong; changes nothing
  fix-dates FILE...         correct dates in sidecars, with --yes; --undo
  mark-used FILE --post P   record that files were used in a post
  share-copy FILE --out D   copy without GPS or metadata (or social-copy)
  calibrate                 test content labels on your marks; changes nothing
  people                    name the people in photos (venv; off by default)
  pets                      name your pets in photos (venv; off by default)
  migrate-tags              one-off: rename an older tool tag in your sidecars
  proxies                   make the gallery's video copies now, so they play
  prune-cache               DELETES gallery thumbnails nothing shows any more

Backup drive
  backup DRIVE              copy the library to the backup drive (no deletes)
  wipe-free DRIVE           overwrite the backup drive's free space with zeros

Setup
  doctor                    check the install; changes nothing

A mistyped command prints the nearest real one and exits 2. Commands marked
(venv) run in the venv setup.sh builds; the rest need only the system Python.
"""
import difflib, importlib, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

VENV_SCRIPTS = {"index": ("semantic.py", ["index"]), "search": ("semantic.py", ["search"]),
                "similar": ("semantic.py", ["similar"]), "vlm-trash": ("vlm_trash.py", []),
                "people": ("people.py", []), "pets": ("pets.py", [])}
# Recognition kinds (recog.py's core): module -> what doctor calls it. Optional: off until setup.sh --recognition.
RECOGNITION = {"people": "people recognition", "pets": "pets recognition"}
# Each thing doctor checks, with the Fedora package that provides it: the one source for the
# `sudo dnf install ...` lines doctor prints. Required unless listed in OPTIONAL.
SYSTEM_TOOLS = {"exiftool": "perl-Image-ExifTool", "tesseract": "tesseract", "ffmpeg": "ffmpeg-free",
                "ffprobe": "ffmpeg-free", "gio": "glib2", "findmnt": "util-linux-core",
                "gst-inspect-1.0": "gstreamer1"}
SYSTEM_MODULES = {"zxingcpp": "python3-zxing-cpp", "PIL": "python3-pillow", "numpy": "python3-numpy",
                  "yaml": "python3-pyyaml"}
TESSERACT_ENG = "tesseract-langpack-eng"
HEVC_DECODERS, HEVC_PACKAGE = ("vah265dec", "vulkanh265dec"), "gstreamer1-plugins-bad-free"
# The gallery's H.264 proxy of an HEVC video (gallery.proxy_commands): a GStreamer H.264 encoder after the GPU
# decoder, or ffmpeg alone when it has both an HEVC decoder and an H.264 encoder (not Fedora's ffmpeg-free).
# In the order the gallery tries them (gallery.GST_H264_HW + GST_H264_SW): the GPU's first, several times faster.
GST_H264_ENCODERS, H264_PACKAGE = ("vah264enc", "x264enc", "openh264enc"), "gstreamer1-plugin-openh264"
GPU_H264_ENCODERS = ("vah264enc",)
FFMPEG_H264_ENCODERS = ("libx264", "libopenh264")
MTP_BACKEND, MTP_PACKAGE = "/usr/libexec/gvfsd-mtp", "gvfs-mtp"
XDG_OPEN, XDG_PACKAGE = "xdg-open", "xdg-utils"  # the gallery's "Open in the system player" and a Share's "Open folder"
OPTIONAL = {"gstreamer1", HEVC_PACKAGE, H264_PACKAGE, MTP_PACKAGE, XDG_PACKAGE}  # Fedora packages, as in the maps above


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

    def venv_importable(self, venv_py, module):
        """MODULE imports in the venv's python (a subprocess: this python may be the system one)."""
        try:
            return subprocess.run([venv_py, "-I", "-c", f"import {module}"], capture_output=True,
                                  timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
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


# Doctor's line shapes: `<status:8> <text>` under a heading, so `grep '^MISSING'` still finds every problem.
STATUS_COLOURS = {"ok": "32", "MISSING": "1;31", "BAD": "1;31", "optional": "33", "absent": "33"}
PROBLEMS = ("MISSING", "BAD")


def use_colour(stream):
    """Colour only on a terminal, and never when NO_COLOR is set to anything non-empty (no-color.org)."""
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    return bool(tty) and not os.environ.get("NO_COLOR")


def doctor(probe=None, out=print, colour=None):
    """Report what is installed, grouped under headings, then a summary with one `sudo dnf install -y` line
    for missing required packages and one for optional ones. Exit 1 if something required is missing or a
    pinned file is wrong. `colour` defaults to on only when printing to a terminal without NO_COLOR."""
    probe = probe or Probe()
    if colour is None:
        colour = out is print and use_colour(sys.stdout)
    ok, need, want = True, [], []   # need/want: Fedora packages missing (required / optional)
    counts, first = {"problems": 0, "optional": 0, "setup": 0}, True   # setup: problems `bash setup.sh` fixes

    def paint(text, code):
        return f"\033[{code}m{text}\033[0m" if colour and code else text

    def heading(title):
        nonlocal first
        if not first:
            out("")
        first = False
        out(paint(f"== {title} ==", "1"))

    def report(status, text):
        if status in PROBLEMS:
            counts["problems"] += 1
            counts["setup"] += "bash setup.sh" in text
        elif status == "optional":
            counts["optional"] += 1
        out(paint(status, STATUS_COLOURS.get(status)) + " " * max(1, 9 - len(status)) + text)

    def summary():
        n, o = counts["problems"], counts["optional"]
        heading("Summary")
        out(f"{n or 'no'} problem{'' if n == 1 else 's'}, {o or 'no'} optional item{'' if o == 1 else 's'} not available")
        if need:
            out("Install the required packages:")
            out("sudo dnf install -y " + " ".join(dict.fromkeys(need)))
        if counts["setup"]:
            out(("Then run" if need else "Run") + ": bash setup.sh   (venv, SigLIP model, GeoNames data)")
        if want:
            out("Optional packages:")
            out("sudo dnf install -y " + " ".join(dict.fromkeys(want)))

    def missing(package, text):
        nonlocal ok
        optional = package in OPTIONAL
        (want if optional else need).append(package)
        ok &= optional
        report("optional" if optional else "MISSING", f"{text} ({package})")

    heading("Config and folders")
    try:
        cfg = config.load()
        report("ok", f"config {cfg['path']}" + ("" if os.path.isfile(cfg["path"]) else " (absent: defaults)"))
    except config.ConfigError as e:
        report("BAD", f"config: {e}")
        summary()
        return 1
    for k in ("library", "videos", "state"):
        report("ok" if os.path.isdir(cfg[k]) else "absent", f"{k} {cfg[k]}")
    for p in cfg["projects"].values():
        report("ok" if os.path.isdir(p["folder"]) else "absent", f"project {p['name']} {p['folder']}")

    heading("System tools")
    for tool, package in SYSTEM_TOOLS.items():
        if probe.which(tool):
            report("ok", tool)
        else:
            missing(package, tool)
    if probe.which("tesseract"):
        if "eng" in probe.output(["tesseract", "--list-langs"]).split():
            report("ok", "tesseract eng language data")
        else:
            missing(TESSERACT_ENG, "tesseract eng language data")

    heading("Python modules")
    for module, package in SYSTEM_MODULES.items():
        if probe.importable(module):
            report("ok", package)
        else:
            missing(package, f"python module {module}")

    heading("Optional extras")
    plugins = probe.output(["gst-inspect-1.0"]) if probe.which("gst-inspect-1.0") else None
    if plugins is not None:
        if any(d in plugins for d in HEVC_DECODERS):
            report("ok", "GStreamer HEVC decoder")
        else:
            missing(HEVC_PACKAGE, "GStreamer HEVC decoder (vah265dec / vulkanh265dec; most phone video)")
    h264 = None
    if probe.which("ffmpeg"):
        encs = probe.output(["ffmpeg", "-hide_banner", "-encoders"])
        if " hevc " in probe.output(["ffmpeg", "-hide_banner", "-decoders"]):
            h264 = next((f"ffmpeg {e}" for e in FFMPEG_H264_ENCODERS if f" {e} " in encs), None)
    if not h264 and plugins is not None:
        h264 = next((e for e in GST_H264_ENCODERS if e in plugins), None)
    if h264 in GPU_H264_ENCODERS:
        report("ok", f"H.264 encoder for the gallery's HEVC proxies ({h264}, on the GPU)")
    elif h264:
        cpu = "" if h264.startswith("ffmpeg ") else ", on the CPU: vah264enc on the GPU is faster"
        report("ok", f"H.264 encoder for the gallery's HEVC proxies ({h264}{cpu})")
    else:
        missing(H264_PACKAGE, "H.264 encoder for the gallery's HEVC proxies (x264enc / openh264enc / vah264enc, "
                              "or ffmpeg with an HEVC decoder and libx264)")
    if probe.exists(MTP_BACKEND):
        report("ok", "gvfs MTP backend")
    else:
        missing(MTP_PACKAGE, "gvfs MTP backend (Android phones)")
    if probe.which(XDG_OPEN):
        report("ok", XDG_OPEN)
    else:
        missing(XDG_PACKAGE, f"{XDG_OPEN} (the gallery's Open in the system player and Open folder buttons)")

    heading("Venv, models and data")
    vp = venv_python()
    have = probe.exists(vp)
    report("ok" if have else "MISSING", f"venv {vp}" + ("" if have else " (bash setup.sh)"))
    ok &= have
    # The gallery and review page re-run under the venv for HEIC; without pillow-heif there, HEIC photos show
    # only as a small preview (gallery) or "no preview" (review page).
    if have:
        heif = probe.venv_importable(vp, "pillow_heif")
        report("ok" if heif else "MISSING", "pillow-heif in the venv (HEIC photos full size in the gallery and the "
                                            "review page)" + ("" if heif else "; without it a small preview only "
                                                                              "(bash setup.sh)"))
        ok &= heif
    ok &= _check_models(probe, report)
    ok &= _check_recognition(probe, report)
    geo = os.path.join(cfg["state"], "geo", "cities1000.zip")
    have = probe.exists(geo)
    report("ok" if have else "MISSING", "GeoNames data" + ("" if have else " (bash setup.sh)"))
    ok &= have

    heading("Ollama (local, 127.0.0.1)")
    ok &= _check_ollama(probe, report)

    summary()
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


def _check_recognition(probe, report):
    """Each recognition kind's models (people.py: YuNet + SFace; pets.py: YOLOX + DINOv2) against
    media_intake/pins.json. Optional: absent models only mean the command is off; a present file with the
    wrong sha256 fails."""
    ok = True
    for mod, what in RECOGNITION.items():
        if not os.path.isfile(os.path.join(HERE, mod + ".py")):
            continue
        files = importlib.import_module(mod).model_files(config.get()["state"])
        have = [f for f in files if probe.exists(f[1])]
        if not have:
            report("optional", f"{what} off: models not installed (bash setup.sh --recognition; docs/recognition.md)")
            continue
        for name, path, pinned in files:
            if not probe.exists(path):
                ok = False
                report("MISSING", f"{name} ({what}; bash setup.sh --recognition)")
                continue
            got = probe.sha256(path)
            if got == pinned:
                report("ok", f"{name} sha256:{_short(pinned)}")
            else:
                ok = False
                report("BAD", f"{name} {path}: sha256 {_short(got)}, pinned {_short(pinned)} "
                              "(bash setup.sh --recognition deletes it; run it again to fetch the pinned file)")
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


def _queue(rest):
    import drive_queue
    drive_queue.main(rest, prog="media-intake queue")
    return 0


def _review(rest):
    import review_ui
    return review_ui.main(rest)


def _backup(rest):
    import backup
    return backup.main(rest)


def _zip_check(rest):
    import zip_check
    return zip_check.main(rest)


def _places(rest):  # its own parser (list, add, rename, remove, suggest, tag) and the library lock
    import places
    return places.main(rest)


def _gallery(rest):  # here, not in intake.py: gallery imports intake, so intake never imports gallery
    import gallery
    return gallery.main(rest)


def _proxies(rest):  # cli.py's too: it reads the library as the gallery does (proxies imports gallery)
    import proxies
    return proxies.main(rest)


def _prune_cache(rest):  # cli.py's too: it reads the library as the gallery does (cache_prune imports gallery)
    import cache_prune
    return cache_prune.main(rest)


# cli.py's own commands; VENV_SCRIPTS run in the venv; everything else goes to intake.py's parser.
COMMANDS = {"doctor": lambda rest: doctor(), "queue": _queue, "review": _review, "backup": _backup,
            "gallery": _gallery, "proxies": _proxies, "prune-cache": _prune_cache, "zip-check": _zip_check,
            "places": _places}
HELP = ("-h", "--help", "help")


def _calibrate(rest):  # system Python: numpy and PyYAML, which setup.sh installs
    try:
        mod = importlib.import_module("calibrate")
    except ModuleNotFoundError as e:
        print(f"calibrate needs the Python package {e.name}: run bash setup.sh, then media-intake doctor",
              file=sys.stderr)
        return 1
    return mod.main(rest)


COMMANDS["calibrate"] = _calibrate


def intake_commands():
    """intake.py's subcommand names, read from its parser (never a copied list)."""
    import argparse, intake
    return [name for a in intake.build_parser("media-intake")._actions
            if isinstance(a, argparse._SubParsersAction) for name in a.choices]


def command_names():
    return sorted({*COMMANDS, *VENV_SCRIPTS, *intake_commands()})


def package_version():
    """The version, from media_intake/__init__.py (its one home, docs/release.md). Read as text: cli.py runs as
    a script, so the package itself may not be importable."""
    with open(os.path.join(HERE, "__init__.py")) as f:
        for line in f:
            if line.startswith("__version__ = "):
                return line.split("=", 1)[1].strip().strip("\"'")
    return "unknown"


def unknown_command(cmd, err=None):
    """One-line error (with a "did you mean" when a real command is close), the short usage; exit code 2.
    An unknown option (`-x`, `--foo`) gets no guess: a flag is never a misspelt command."""
    err = err or sys.stderr
    names = command_names()
    if cmd.startswith("-"):
        print(f"media-intake: unknown option {cmd!r} (on its own it takes only --help and --version; "
              "other options go after a command)", file=err)
    else:
        close = difflib.get_close_matches(cmd, names, n=1)
        print(f"media-intake: unknown command {cmd!r}" + (f"; did you mean {close[0]!r}?" if close else ""), file=err)
    print("usage: media-intake <command> [options]\ncommands: " + ", ".join(names)
          + "\n'media-intake --help' describes them; 'media-intake <command> --help' shows a command's options",
          file=err)
    return 2


def no_venv(cmd, script, pre, rest, vp):
    """A venv command before setup.sh built the venv. --help still works: the script's parser needs only the
    standard library, so the system Python prints it, then one line saying the command needs the venv. Any
    other use exits 1 with that line."""
    need = f"media-intake {cmd} needs the venv, which is not built yet ({vp}): run bash setup.sh first"
    if not any(x in ("-h", "--help") for x in rest):
        print(need, file=sys.stderr)
        return 1
    r = subprocess.run([sys.executable, "-I", os.path.join(HERE, script), *pre, "--help"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    print(f"\nnote: {need}")
    return r.returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in HELP:
        print(__doc__.strip())  # one list, by task; tests/test_cli.py checks it names every command
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in ("--version", "-V", "version"):
        print(f"media-intake {package_version()}")
        return 0
    if cmd == "doctor" and rest:  # doctor takes no options; only --help is answered
        if rest[0] in HELP:
            print("usage: media-intake doctor\n\nCheck the install: system packages, venv, model checksums, "
                  "Ollama, config.\nChanges nothing. Exits 1 if something required is missing or a pinned file "
                  "is wrong.")
            return 0
        print("media-intake doctor takes no options ('media-intake doctor --help' says what it checks)",
              file=sys.stderr)
        return 2
    if cmd in COMMANDS:
        return COMMANDS[cmd](rest)
    if cmd in VENV_SCRIPTS:
        script, pre = VENV_SCRIPTS[cmd]
        vp = venv_python()
        if not os.path.exists(vp):
            return no_venv(cmd, script, pre, rest, vp)
        os.execv(vp, [vp, "-I", os.path.join(HERE, script), *pre, *rest])
    if cmd not in intake_commands():
        return unknown_command(cmd)
    import intake
    intake.main(argv, prog="media-intake")
    return 0


if __name__ == "__main__":
    sys.exit(main())
