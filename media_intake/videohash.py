#!/usr/bin/env python3
"""Perceptual video hash: near-duplicate videos (re-encoded, resized or trimmed copies of one clip).

  python3 -I media_intake/videohash.py CLIP [CLIP...]   (prints each clip's frame count or why it has none)

A clip's hash is the 64-bit dHash (the same 9x8 grey difference hash intake.dhash takes of a photo) of a frame
every STEP seconds, spread over at most MAX_FRAMES for a long clip. ffmpeg scales each sampled frame to 9x8
grey and writes the raw pixels to a pipe: no frame of the clip is written anywhere. Frames with almost no
contrast (black, a fade, a flat colour) are dropped: their dHash is noise and would match any other blank.

Two clips match (near) when at least COVER of the shorter clip's frames are each within FRAME_MAX bits of some
frame of the longer one. The frames are compared as a set, ignoring order and time, as Meta's vPDQ does
(docs/intake.md, Duplicates): a trimmed copy's frames are all in the original, a re-encode or resize moves each
frame's dHash by a few bits.

Where ffmpeg can't decode a clip (HEVC on Fedora's ffmpeg-free), scan_sensitive.video_frames samples it,
GStreamer's GPU decoder included, with the same safety rules (argv lists, `file:` paths, no pipeline built
from a name); its frames go to a private work folder that is removed at once.

A clip that can't be hashed gets a note instead ("unreadable", "too short or blank to compare"), never an
empty hash that would read as "no near-copy".
"""
import os, shutil, subprocess, sys, tempfile

STEP = 0.5  # seconds between sampled frames (2 a second): a trim moves the sampling by at most STEP / 2
MAX_FRAMES = 120  # a longer clip is sampled every duration / MAX_FRAMES seconds instead
FLAT = 8  # grey levels: a 9x8 frame whose brightest and darkest pixels are closer than this is blank
FRAME_MAX = 10  # dHash bits: the same moment after a re-encode, resize or a sampling shift of STEP / 2
COVER = 0.8  # share of the shorter clip's frames that must each match some frame of the other
MIN_FRAMES = 3  # fewer usable frames than this: too little to say two clips are the same
PROBES = 6  # frames of the shorter clip tried first; a pair failing on these is not compared in full
TIMEOUT = 1800

UNREADABLE = "unreadable"
TOO_SHORT = "too short or blank to compare"


def frame_dhash(px):
    """dHash of one 9x8 grey frame (72 pixel values, rows of 9), or None when the frame is blank."""
    if len(px) != 72 or max(px) - min(px) < FLAT:
        return None
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def _duration(src):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return float(r.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def _ffmpeg_frames(path):
    """The 9x8 grey frames ffmpeg samples, as lists of 72 values; None when ffmpeg can't decode the clip."""
    # file: + an absolute path: a name like "concat:..." or "http:..." is a file, never a protocol to follow.
    src = "file:" + os.path.abspath(path)
    dur = _duration(src)
    every = max(STEP, dur / MAX_FRAMES) if dur else STEP
    try:
        # eof_action=pass keeps the only frame of a clip shorter than the sampling period (scan_sensitive).
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn",
                            "-vf", f"fps=1/{every:.3f}:eof_action=pass,scale=9:8,format=gray",
                            "-frames:v", str(MAX_FRAMES), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                           capture_output=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    out = r.stdout
    return [list(out[i:i + 72]) for i in range(0, len(out) - 71, 72)]


def _fallback_frames(path, work):
    """Frames through scan_sensitive.video_frames (ffmpeg again, then GStreamer's GPU HEVC decoder), each
    shrunk to 9x8 grey; None when that fails too. The JPEGs live in a private folder under work and go at once."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import scan_sensitive
        from PIL import Image
    except ImportError:
        return None
    os.makedirs(work, mode=0o700, exist_ok=True)
    fd = tempfile.mkdtemp(prefix="vhash-", dir=work)  # mkdtemp makes it 0700
    try:
        names = scan_sensitive.video_frames(path, fd, max_frames=MAX_FRAMES)
        frames = []
        for n in names:
            try:
                with Image.open(os.path.join(fd, n)) as im:
                    frames.append(list(im.convert("L").resize((9, 8), Image.BOX).getdata()))
            except OSError:
                continue
        return frames or None
    finally:
        shutil.rmtree(fd, ignore_errors=True)


def video_hash(path, work=None):
    """(frame dHashes, note) for one clip. note is None when the clip was hashed, else why it wasn't:
    UNREADABLE (no decoder read it) or TOO_SHORT (fewer than MIN_FRAMES frames with any contrast).
    The hashes are then [] and must not be read as "no near-copy"."""
    frames = _ffmpeg_frames(path)
    if frames is None:
        frames = _fallback_frames(path, work or os.path.expanduser("~/.cache/media-intake"))
    if frames is None:
        return [], UNREADABLE
    hashes = [h for h in map(frame_dhash, frames) if h is not None]
    if len(hashes) < MIN_FRAMES:
        return [], TOO_SHORT
    return hashes, None


def _hits(frames, other):
    return sum(1 for x in frames if any((x ^ y).bit_count() <= FRAME_MAX for y in other))


def near(a, b):
    """True when two clips' hashes (video_hash) say one is a re-encoded, resized or trimmed copy of the other."""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < MIN_FRAMES:
        return False
    step = max(1, len(short) // PROBES)
    probe = short[::step][:PROBES]
    if _hits(probe, long) < COVER * len(probe):
        return False
    return _hits(short, long) >= COVER * len(short)


def main(argv):
    for p in argv:
        h, note = video_hash(p)
        print(f"{p}: {len(h)} frames" if note is None else f"{p}: {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
