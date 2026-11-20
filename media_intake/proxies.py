#!/usr/bin/env python3
"""Make the gallery's video proxies ahead of time, so every video plays at once.

  media-intake proxies [--dry-run] [--inhibit] [--retry-failed]
  python3 -I media_intake/proxies.py         (same flags)

For each library video the gallery shows whose codec browsers can't play (HEVC), makes the H.264 proxy the
gallery would make on first open: the same commands (gallery.make_proxy), the same cache
(~/.cache/media-intake/proxies/<sha256>.mp4, folder 0700, files 0600), one at a time, skipping any already
made, so stopping it (Ctrl-C) loses only the one in progress and running it again goes on from there. A video
no proxy could be made of (every command failed or stalled: gallery.proxy_failed) is skipped on later runs, and
said so, until --retry-failed. Exit 1 when one failed this run (the gallery still plays its original). The
library is read as the gallery reads it (gallery.scan_library: never the state folder, never a file scan
flagged, quarantined or erased); each video is checked again just before its turn and after its proxy is made
(label.refused), and a proxy whose video was trashed or erased meanwhile is deleted at once. Originals are only
read; nothing is fetched. docs/gallery.md, "Video proxies".
"""
import argparse, contextlib, os, shutil, signal, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gallery  # noqa: E402  (scan_library, App.proxy_needed, make_proxy, private_cache)
import label  # noqa: E402  (refused: flagged, quarantined or erased since the scan)


def still_shown(path):
    """The video may still have a proxy: a regular file (no symlink) outside the state folder, not flagged,
    quarantined or erased (label.refused, read fresh)."""
    return (os.path.isfile(path) and not os.path.islink(path) and not gallery.under_state(path)
            and not label.refused([path], gallery.STATE))


def plan(items, retry_failed=False):
    """([(item, proxy path)] still to make, one per content; how many are made already; how many play as they
    are; how many failed before and are skipped (none with retry_failed: they are in the list again)). Probes
    each video's codec (ffprobe) and hashes the ones that need a proxy."""
    app = gallery.App(items)
    todo, have, plain, failed = {}, set(), 0, set()
    for it in items:
        if it["media"] != "video" or it["show"] != "video":  # one the gallery's player never gets
            continue
        dst = app.proxy_needed(it, failed=True)
        if not dst:
            plain += 1
        elif os.path.isfile(dst):
            have.add(dst)
        elif gallery.proxy_failed(dst) and not retry_failed:
            failed.add(dst)
        else:
            todo.setdefault(dst, it)  # copies with the same content share one proxy
    return [(it, dst) for dst, it in todo.items()], len(have), plain, len(failed)


def main(argv=None, prog="media-intake proxies"):
    ap = argparse.ArgumentParser(
        prog=prog, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,  # --inhibit re-execs
        description="Make the gallery's H.264 copies of videos the browser can't play (HEVC, most\n"
                    "phones) now, one at a time, so the gallery plays them at once instead of\n"
                    "converting on first open. Run it again to go on after a stop. A video that\n"
                    "can't be converted is remembered and skipped next time (--retry-failed).\n"
                    "Exits 1 when one failed this run; the gallery still plays its original.\n\n"
                    "Touches: reads the library; writes ~/.cache/media-intake/proxies only. Never\n"
                    "  a flagged, quarantined or erased file.\n"
                    "Deletes: only a copy it just made of a video trashed or erased meanwhile, and\n"
                    "  a video's failure record once its copy is made.")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be made; make nothing")
    ap.add_argument("--inhibit", action="store_true",
                    help="keep the laptop from sleeping while it runs (systemd-inhibit)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="try again the videos an earlier run (or the gallery) could not convert")
    a = ap.parse_args(argv)
    if a.inhibit:
        if not shutil.which("systemd-inhibit"):
            sys.exit(f"{prog}: systemd-inhibit not found")
        args = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--inhibit"]
        os.execvp("systemd-inhibit", ["systemd-inhibit", "--what=sleep:idle", "--who=media-intake",
                                      "--why=media-intake video proxies", sys.executable, "-I",
                                      os.path.abspath(__file__), *args])
    items, skipped = gallery.scan_library()
    todo, have, plain, failed_before = plan(items, a.retry_failed)
    print(f"{len(todo)} to make, {have} made already, {plain} play as they are"
          f" ({skipped} flagged or erased left out)", flush=True)
    if failed_before:
        print(f"{failed_before} failed before, skipped (--retry-failed tries them again)", flush=True)
    if a.dry_run or not todo:
        return 0
    gallery.private_cache()
    gallery.sweep_proxy_work()  # what a killed gallery or run left: its encoder ends, its work folder goes
    made = failed = gone = 0
    prev = gallery.stop_on_signals([])  # SIGTERM stops it as Ctrl-C does: make_proxy ends its encoder and folder
    try:
        for n, (it, dst) in enumerate(todo, 1):
            p = it["path"]
            if os.path.isfile(dst):  # the gallery (or another run) made it meanwhile
                have += 1
                continue
            if not still_shown(p):
                gone += 1
                continue
            t0 = time.monotonic()
            print(f"[{n}/{len(todo)}] {p}: ", end="", flush=True)
            ok = gallery.make_proxy(p, dst)
            if ok and not still_shown(p):  # trashed or erased while it was made: no copy stays
                with contextlib.suppress(FileNotFoundError):  # trash / erase may have deleted it already
                    os.remove(dst)
                ok, gone = False, gone + 1
                print("gone meanwhile, copy deleted", flush=True)
                continue
            made, failed = made + ok, failed + (not ok)
            print(f"made in {time.monotonic() - t0:.0f} s" if ok else "failed (the gallery serves it as it is)",
                  flush=True)
    except KeyboardInterrupt:
        print(f"\nstopped: {made} made; run it again to go on", flush=True)
        return 130
    finally:
        for sig, h in prev.items():
            signal.signal(sig, h)
    print(f"done: {made} made, {failed} failed, {gone} gone meanwhile, {have} made already", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
