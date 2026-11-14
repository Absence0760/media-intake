"""`media-intake`: one entry point for every tool in this package.

  media-intake <intake command> ...      stage, plan, scan, apply, tag, erase, trash, gallery, ... (intake.py)
  media-intake queue add|run|status|offload ...   external-drive process queue (drive_queue.py)
  media-intake review BATCH ...          local review page for a batch (review_ui.py)
  media-intake index                     build or update the semantic search index  (venv)
  media-intake search "TEXT" ...         semantic search                             (venv)
  media-intake similar FILE ...          nearest neighbours of a file                (venv)
  media-intake vlm-trash ...             local vision-model second opinion on trash  (venv, Ollama)
  media-intake doctor                    check system packages, venv, models, config

`media-intake <command> --help` shows each command's options. Commands marked
(venv) run in the venv setup.sh builds; the rest need only the system Python.
"""
import os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

VENV_SCRIPTS = {"index": ("semantic.py", ["index"]), "search": ("semantic.py", ["search"]),
                "similar": ("semantic.py", ["similar"]), "vlm-trash": ("vlm_trash.py", [])}
SYSTEM_TOOLS = ("exiftool", "tesseract", "ffmpeg", "ffprobe", "gio", "findmnt", "gst-inspect-1.0")


def venv_python():
    return os.path.join(config.get()["state"], "venv", "bin", "python")


def doctor():
    """Report what is installed; exit 1 if something required is missing."""
    ok = True
    try:
        cfg = config.load()
        print(f"ok       config {cfg['path']}" + ("" if os.path.isfile(cfg["path"]) else " (absent: defaults)"))
    except config.ConfigError as e:
        print(f"BAD      config: {e}")
        return 1
    for k in ("library", "videos", "state"):
        print(f"{'ok' if os.path.isdir(cfg[k]) else 'absent':8} {k} {cfg[k]}")
    for p in cfg["projects"].values():
        print(f"{'ok' if os.path.isdir(p['folder']) else 'absent':8} project {p['name']} {p['folder']}")
    for t in SYSTEM_TOOLS:
        have = shutil.which(t)
        ok &= bool(have) or t == "gst-inspect-1.0"
        print(f"{'ok' if have else 'MISSING':8} {t}")
    try:
        import zxingcpp  # noqa: F401
        print("ok       python3-zxing-cpp")
    except ImportError:
        ok = False
        print("MISSING  python3-zxing-cpp")
    if shutil.which("tesseract"):
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
        have = "eng" in r.stdout.split()
        ok &= have
        print(f"{'ok' if have else 'MISSING':8} tesseract eng language data")
    if shutil.which("gst-inspect-1.0"):
        r = subprocess.run(["gst-inspect-1.0"], capture_output=True, text=True)
        dec = "vah265dec" in r.stdout or "vulkanh265" in r.stdout
        print(f"{'ok' if dec else 'optional':8} GStreamer HEVC decoder (vah265dec / vulkanh265dec)")
    vp = venv_python()
    print(f"{'ok' if os.path.exists(vp) else 'MISSING':8} venv {vp}" + ("" if os.path.exists(vp) else " (bash setup.sh)"))
    ok &= os.path.exists(vp)
    import label
    have = os.path.exists(label.MODEL)
    print(f"{'ok' if have else 'MISSING':8} SigLIP vision model" + ("" if have else " (bash setup.sh)"))
    ok &= have
    geo = os.path.join(cfg["state"], "geo", "cities1000.zip")
    print(f"{'ok' if os.path.exists(geo) else 'MISSING':8} GeoNames data" + ("" if os.path.exists(geo) else " (bash setup.sh)"))
    ok &= os.path.exists(geo)
    if shutil.which("ollama"):
        r = subprocess.run(["ollama", "--version"], capture_output=True, text=True)
        print(f"optional {((r.stdout or r.stderr).strip().splitlines() or ['ollama, version unknown'])[-1]}")
    else:
        print("optional ollama not installed (only vlm-trash needs it)")
    return 0 if ok else 1


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
