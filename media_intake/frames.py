#!/usr/bin/env python3
"""Video frame sampler: a few upright RGB frames per clip, one per scene, for recognition.

  python3 -I media_intake/frames.py CLIP --list     (the chosen timestamps only; no frame is fetched)
  python3 -I media_intake/frames.py CLIP --info     (duration, fps, size, rotation, codec as JSON)
  python3 -I media_intake/frames.py CLIP            (fetches the frames; prints each one's time and size)

    import frames
    with frames.sample(path) as s:
        for t, rgb in s:          # t: seconds from the clip's start; rgb: numpy uint8 H x W x 3, upright
            ...

Needs numpy (Fedora's python3-numpy: the system Python has it and setup.sh's venv reuses it); no OpenCV.

Two passes over the clip, both through a pipe (no frame is written to disk):

1. Analyse: the clip decoded at ANALYSE_FPS, scaled to ANALYSE_PX on its long side. Each frame is scored
   against the one before it (half the colour-histogram distance, half the mean absolute pixel difference,
   0..1). A frame scoring over `scene` starts a new scene and is picked; in a stretch without cuts a frame is
   picked every `every` seconds unless it is a near-copy (mean difference under DUP) of the last pick. Picks
   are at least `min_gap` seconds apart (a cut just after a timed pick takes its place). The first frame that
   isn't near-black or near-white is always picked (a fade-in is skipped). Over `max_frames` picks, they are
   spread evenly (the first kept).
2. Fetch: only the picked frames, scaled to SAMPLE_PX on the long side: with ffmpeg one seek per pick when the
   picks are few for the clip's length (SEEK_SPAN), else the clip decoded again and only those frames kept.

Rotation: the stream's display matrix (what phones write) is applied, so frames come out upright. Decoders:
ffmpeg when it has a decoder for the codec; else, for HEVC, GStreamer's GPU decoder (scan_sensitive's
_gst_hevc_decoder: VA-API, or Vulkan), as the gallery's proxies use it. GStreamer re-parses its arguments as one
pipeline, so it reads the clip through a link `in` in a private 0700 work folder under ~/.cache/media-intake,
and that folder's path must match SAFE_GST_PATH (else GStreamer is not tried). The work folder holds only that
link and is removed when the sampler closes. Every command is an argv list (never a shell), ffmpeg reads
`file:<absolute path>`, each pass has a deadline (DECODE_TIMEOUT), and the decoder is killed and reaped when
the sampler is closed early (close(), `with`, or the iterator being dropped).

Refused (Refused, before anything is probed or decoded; info() too): any path under the state folder (quarantine, staging, the
private store) and any file label.refused() names (scan flagged, quarantined or erased).
"""
import json, os, re, select, shutil, subprocess, sys, tempfile, time

# -I leaves the script's folder off sys.path; appended, so a sibling never shadows a venv package.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.append(HERE)
import config  # noqa: E402
import label  # noqa: E402  (refused: stdlib only at import)

import numpy as np  # noqa: E402

STATE = config.get()["state"]
CACHE = os.path.join(config.HOME, ".cache", "media-intake")  # the gallery's cache (thumbs, proxies) holds the work folder
SAMPLE_PX = 1280  # long side of a handed-out frame, at most (never scaled up)
ANALYSE_PX = 160  # long side of the frames the analysis pass compares
ANALYSE_FPS = 4.0  # frames a second the analysis pass looks at
ANALYSE_MAX = 3600  # analysis frames per clip at most: a clip over 15 min is looked at less often
DUP = 0.02  # mean absolute difference (0..1) under which a timed pick is a near-copy of the last pick
DARK, BRIGHT = 12, 245  # mean grey level (0..255) outside which a frame is too dark or too bright to start on
PROBE_TIMEOUT = 60  # seconds, ffprobe and the decoder lists
DECODE_TIMEOUT = 1800  # seconds, each pass over one clip
SEEK_SPAN = 2.0  # seconds: ffmpeg fetches each pick by a seek when picks x SEEK_SPAN < the clip's length (a seek
# decodes from the keyframe before the pick: phones put one every 1-2 s), else in one more pass
SAFE_GST_PATH = re.compile(r"[A-Za-z0-9._/+-]+")  # what may stand as a location= value (as gallery.SAFE_GST_PATH)
GST_FLIP = {90: "90r", 180: "180", 270: "90l"}  # videoflip video-direction for a clockwise turn
_DECODERS = {}
REFUSED = "refused: under the state folder, or flagged, quarantined or erased"


class FramesError(Exception):
    """The clip can't be sampled: unreadable, no decoder for its codec, the decoder failed or timed out."""


class Refused(FramesError):
    """The path must never be sampled: under the state folder, or flagged, quarantined or erased."""


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)


def _num(v, default=0.0):
    try:
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/", 1)
            return float(a) / float(b) if float(b) else default
        return float(v)
    except (TypeError, ValueError):
        return default


def info(path):
    """dict(duration, fps, w, h, rotation, codec) of the clip's first video stream (ffprobe). w and h: the
    stored size; rotation: the clockwise turn (0, 90, 180, 270) that shows it upright (a phone's portrait clip:
    90); duration in seconds, 0.0 when unknown. Raises Refused for a path that must never be read (refused())
    and FramesError when there is no readable video stream."""
    if refused(path):
        raise Refused(REFUSED)
    return {k: v for k, v in _probe(path).items() if k != "start"}


def _probe(path):
    """info() without the refused check (the caller made it), plus start: seconds from the container's start to
    the first video frame (an audio-first file starts its video later; ffmpeg's -ss counts from the container)."""
    try:
        r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_format", "-of", "json",
                  "file:" + os.path.abspath(path)])
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        data = {}
    streams = data.get("streams") or []
    if not streams or not streams[0].get("codec_name"):
        raise FramesError("no readable video stream")
    s = streams[0]
    ccw = 0.0
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            ccw = _num(sd["rotation"])
    if not ccw and "rotate" in (s.get("tags") or {}):  # an old-style tag: clockwise already
        ccw = -_num(s["tags"]["rotate"])
    rotation = int(round(-ccw / 90.0)) * 90 % 360
    duration = _num(s.get("duration")) or _num((data.get("format") or {}).get("duration"))
    fps = _num(s.get("avg_frame_rate")) or _num(s.get("r_frame_rate"))
    start = max(0.0, _num(s.get("start_time")) - _num((data.get("format") or {}).get("start_time")))
    return dict(duration=duration, fps=fps, w=int(s.get("width") or 0), h=int(s.get("height") or 0),
                rotation=rotation, codec=s["codec_name"].lower(), start=start)


def ffmpeg_decodes(codec):
    """True when this ffmpeg has a decoder for codec (by name, or one naming it as "(codec X)")."""
    if "set" not in _DECODERS:
        names = set()
        try:
            for line in _run(["ffmpeg", "-hide_banner", "-decoders"]).stdout.splitlines():
                bits = line.split()
                if len(bits) >= 2 and bits[0].startswith("V") and len(bits[0]) == 6:
                    names.add(bits[1])
                    m = re.search(r"\(codec (\w+)\)\s*$", line)
                    if m:
                        names.add(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
        _DECODERS["set"] = names
    return codec in _DECODERS["set"]


def fit(w, h, px):
    """(w, h) scaled so the long side is at most px (never up), both even and at least 2."""
    k = min(1.0, px / max(w, h, 1))
    return max(2, int(round(w * k / 2)) * 2), max(2, int(round(h * k / 2)) * 2)


def refused(path):
    """True when path must never be sampled (see Refused)."""
    rp = os.path.realpath(path)
    return rp.startswith(os.path.realpath(STATE) + os.sep) or bool(label.refused([path], STATE))


def _stats(a):
    """(grey mean 0..255, 16-bin histogram per channel as a 3 x 16 array summing to 1 per row) of an RGB frame."""
    q = (a >> 4).reshape(-1, 3)
    n = q.shape[0]
    hist = np.stack([np.bincount(q[:, c], minlength=16) for c in range(3)]) / n
    return float(a.mean()), hist


def _mad(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()) / 255.0


def _score(a, ha, b, hb):
    """How different frame b is from frame a, 0..1: half the histogram distance, half the mean pixel difference."""
    return 0.5 * float(np.abs(ha - hb).sum(axis=1).mean()) / 2.0 + 0.5 * _mad(a, b)


def spread(picks, cap):
    """At most cap of picks, spread evenly over them (the first and last kept)."""
    if len(picks) <= cap:
        return list(picks)
    if cap <= 1:
        return list(picks[:1])
    return [picks[int(i)] for i in np.round(np.linspace(0, len(picks) - 1, cap)).astype(int)]


class _Decoder:
    """One decoding process writing fixed-size raw frames to a pipe. read() gives one frame or None at the end;
    past the deadline it kills the process and raises FramesError. close() kills and reaps it."""

    def __init__(self, argv, w, h, ch):
        self.w, self.h, self.ch, self.size = w, h, ch, w * h * ch
        self.deadline = time.monotonic() + DECODE_TIMEOUT
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.count = 0

    def read(self):
        buf = bytearray(self.size)
        view, got, fd = memoryview(buf), 0, self.proc.stdout.fileno()
        while got < self.size:
            left = self.deadline - time.monotonic()
            if left <= 0:
                self.close()
                raise FramesError("decoder timed out")
            if not select.select([fd], [], [], min(left, 1.0))[0]:
                continue
            k = os.readv(fd, [view[got:]])
            if not k:
                return None  # the end (a short last frame is dropped)
            got += k
        self.count += 1
        a = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, self.ch)
        return a if self.ch == 3 else np.ascontiguousarray(a[:, :, :3])

    def finish(self):
        """The exit code once the output has ended (killed past the deadline: None)."""
        try:
            return self.proc.wait(timeout=max(1.0, self.deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.close()
            return None

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


class Sampler:
    """The iterator sample() returns: (t, rgb) pairs, t ascending. Also a context manager; close() stops the
    decoder at once and removes the work folder. After the analysis pass (plan(), or the first next()):
    .picks is [(t, why)] with why "first", "cut" or "every"; .backend is "ffmpeg" or "gst"; .analysed counts
    the frames compared. .decoder is the running decoding process (a Popen), or None."""

    def __init__(self, path, max_frames=48, min_gap=1.0, every=4.0, scene=0.3):
        if max_frames < 1:
            raise ValueError("max_frames must be at least 1")
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise FramesError("not a file")
        if refused(self.path):
            raise Refused(REFUSED)
        self.max_frames, self.min_gap, self.every, self.scene = max_frames, min_gap, every, scene
        self.meta = _probe(self.path)
        self.backends = []
        if ffmpeg_decodes(self.meta["codec"]):
            self.backends.append("ffmpeg")
        if self.meta["codec"] == "hevc":
            import scan_sensitive  # the GPU HEVC decoder thumbnails, scans and proxies use
            self.gst_dec = scan_sensitive._gst_hevc_decoder()
            if self.gst_dec:
                self.backends.append("gst")
        if not self.backends:
            raise FramesError(f"no decoder for {self.meta['codec']}")
        w, h = self.meta["w"], self.meta["h"]
        if self.meta["rotation"] in (90, 270):
            w, h = h, w
        if not (w and h):
            raise FramesError("unknown frame size")
        self.upright = (w, h)
        d = self.meta["duration"]
        self.afps = min(ANALYSE_FPS, ANALYSE_MAX / d) if d > 0 else ANALYSE_FPS
        self.picks, self.backend, self.analysed, self.decoder, self.work = None, None, 0, None, None
        self._index = []  # the analysis frame number of each pick
        self._it = self._iterate()

    # ------------------------------------------------------------ commands
    def _workdir(self):
        if self.work is None:
            os.makedirs(CACHE, mode=0o700, exist_ok=True)
            self.work = os.path.abspath(tempfile.mkdtemp(prefix=".frames-", dir=CACHE))  # 0700
            os.symlink(self.path, os.path.join(self.work, "in"))
        return self.work

    def _command(self, backend, size, pick=None):
        """(argv, channels) decoding the clip at self.afps frames a second, scaled to size (upright w, h). pick:
        the analysis frame numbers to hand out (ffmpeg selects them; GStreamer hands out all, skipped here)."""
        w, h = size
        if backend == "ffmpeg":
            vf = f"fps={self.afps:.6g}:eof_action=pass"
            if pick is not None:
                vf += ",select='" + "+".join(f"eq(n,{i})" for i in pick) + "'"
            vf += f",scale={w}:{h}:flags=area,format=rgb24"
            until = ["-t", f"{(pick[-1] + 2) / self.afps + 1:.3f}"] if pick else []
            return ["ffmpeg", "-v", "error", "-nostdin", *until, "-i", "file:" + self.path, "-map", "0:v:0",
                    "-an", "-sn", "-dn", "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", "pipe:1"], 3
        work = self._workdir()
        if not SAFE_GST_PATH.fullmatch(work):
            import scan_sensitive
            scan_sensitive.note_gst_skipped(os.path.dirname(work), "clip not sampled with GStreamer")
            return None, 0
        rot = self.meta["rotation"]
        sw, sh = (h, w) if rot in (90, 270) else (w, h)  # scaled before the turn: the stored orientation
        num, den = int(round(self.afps * 1000)), 1000
        demux = "matroskademux" if self.path.lower().endswith((".mkv", ".webm")) else "qtdemux"
        rate = f"framerate={num}/{den}"
        if self.gst_dec == "vah265dec":  # frames dropped to the analysis rate on the GPU, then scaled and fetched
            post = ["video/x-raw(memory:VAMemory)", "!", "videorate", "!", f"video/x-raw(memory:VAMemory),{rate}",
                    "!", "vapostproc", "!", f"video/x-raw,width={sw},height={sh}", "!"]
        else:  # Vulkan: frames come back to memory, scaled on the CPU
            post = ["vulkandownload", "!", "videorate", "!", f"video/x-raw,{rate}", "!", "videoconvert", "!",
                    "videoscale", "!", f"video/x-raw,width={sw},height={sh}", "!"]
        flip = ["videoflip", f"video-direction={GST_FLIP[rot]}", "!"] if rot in GST_FLIP else []
        return ["gst-launch-1.0", "-q", "filesrc", "location=" + os.path.join(work, "in"), "!", demux, "!",
                "h265parse", "!", self.gst_dec, "!", *post, *flip, "videoconvert", "!",
                f"video/x-raw,format=RGBx,width={w},height={h}", "!",
                "fdsink", "fd=1"], 4

    def _start(self, backend, size, pick=None):
        argv, ch = self._command(backend, size, pick)
        if not argv:
            return None
        self.decoder = _Decoder(argv, size[0], size[1], ch)
        return self.decoder

    def _stop(self):
        if self.decoder:
            self.decoder.close()
            self.decoder = None

    def _cleanup(self):
        self._stop()
        if self.work:
            shutil.rmtree(self.work, ignore_errors=True)
            self.work = None

    # ------------------------------------------------------------ pass 1: what to take
    def _analyse(self, backend):
        """[(n, t, why)] from one analysis pass with backend; None when it gave no frame."""
        dec = self._start(backend, fit(*self.upright, ANALYSE_PX))
        if dec is None:
            return None
        picks, prev, last, mark, start, n = [], None, None, 0.0, None, 0
        try:
            while (a := dec.read()) is not None:
                t = n / self.afps
                mean, hist = _stats(a)
                if start is None:
                    if DARK < mean < BRIGHT or n == 0:
                        first = (a, hist, n, t)
                    if DARK < mean < BRIGHT:
                        start = n
                        picks, last, mark = [(n, t, "first")], (a, hist, t), t
                else:
                    cut = _score(prev[0], prev[1], a, hist) > self.scene
                    gap = t - last[2]
                    if cut and gap >= self.min_gap:
                        picks.append((n, t, "cut"))
                        last, mark = (a, hist, t), t
                    elif cut and picks[-1][2] == "every":  # a cut just after a timed pick takes its place
                        picks[-1] = (n, t, "cut")
                        last, mark = (a, hist, t), t
                    elif not cut and t - mark >= self.every and gap >= self.min_gap:
                        mark = t
                        if _mad(last[0], a) >= DUP:
                            picks.append((n, t, "every"))
                            last = (a, hist, t)
                prev, n = (a, hist), n + 1
            rc = dec.finish()
        finally:
            self._stop()
        if rc is None:
            raise FramesError("decoder timed out")
        if n == 0 or rc != 0:  # nothing, or the decoder failed part way: the picks would miss the rest
            return None
        if start is None:  # every frame too dark or too bright: the first one stands
            picks = [(first[2], first[3], "first")]
        self.analysed = n
        return picks

    def plan(self):
        """[(t, why)] of the frames that will be handed out (the analysis pass, run once)."""
        if self.picks is None:
            try:
                self._plan()
            finally:
                self._cleanup()
        return self.picks

    def _plan(self):
        if self.picks is not None:
            return
        for backend in self.backends:
            got = self._analyse(backend)
            if got:
                got = spread(got, self.max_frames)
                self.backend, self._index = backend, [p[0] for p in got]
                self.picks = [(round(p[1], 3), p[2]) for p in got]
                return
        raise FramesError("no decoder could read the clip")

    # ------------------------------------------------------------ pass 2: the frames
    def _iterate(self):
        try:
            self._plan()
            want = dict(zip(self._index, (p[0] for p in self.picks)))
            size = fit(*self.upright, SAMPLE_PX)
            if self.backend == "ffmpeg" and len(self.picks) * SEEK_SPAN < self.meta["duration"]:
                yield from self._seeks(size)
                return
            dec = self._start(self.backend, fit(*self.upright, SAMPLE_PX), self._index)
            if dec is None:
                raise FramesError("decoder unavailable")
            order, n, out = iter(self._index), 0, 0
            while True:
                a = dec.read()
                if a is None:
                    break
                if self.backend == "ffmpeg":  # only the picked frames come out, in order
                    i = next(order, None)
                    if i is None:
                        break
                    out += 1
                    yield want[i], a
                else:
                    if n in want:
                        out += 1
                        yield want[n], a
                    n += 1
                    if n > self._index[-1]:
                        break
            if out < len(self._index):
                raise FramesError(f"decoder gave {out} of {len(self._index)} frames")
        finally:
            self._cleanup()

    def _seeks(self, size):
        """The picked frames, one ffmpeg seek each (fewer picks than the clip has SEEK_SPAN stretches: cheaper
        than decoding it all again). Each lands on the frame the analysis pass picked."""
        w, h = size
        half = 0.5 / self.meta["fps"] if self.meta["fps"] > 0 else 0.0
        for t, _ in self.picks:
            # The analysis pass took the frame nearest t (fps rounds to nearest), counted from the first video
            # frame; a seek gives the first frame at or after its time, counted from the container's start.
            at = max(0.0, t + self.meta["start"] - half)
            self.decoder = _Decoder(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{at:.6f}", "-i", "file:" + self.path,
                                     "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1", "-vf",
                                     f"scale={w}:{h}:flags=area,format=rgb24", "-f", "rawvideo", "-pix_fmt",
                                     "rgb24", "pipe:1"], w, h, 3)
            a = self.decoder.read()
            self._stop()
            if a is None:
                raise FramesError("decoder failed")
            yield t, a

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        """Stop at once: the decoder is killed and reaped, the work folder removed."""
        self._it.close()
        self._cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def sample(path, *, max_frames=48, min_gap=1.0, every=4.0, scene=0.3):
    """Iterator of (t_seconds, rgb) for a few frames of the clip at path: one at each scene change, one every
    `every` seconds in between (near-copies skipped), at least `min_gap` s apart, at most `max_frames` (spread
    evenly), the first good frame always among them. rgb is numpy uint8 H x W x 3, upright, the long side at
    most SAMPLE_PX. A Sampler (close() / `with` stop the decoder). Raises Refused for a path that must never be
    sampled and FramesError for one that can't be (here, or from the iteration when decoding fails)."""
    return Sampler(path, max_frames=max_frames, min_gap=min_gap, every=every, scene=scene)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Sample frames of a video (prints timestamps and sizes only).")
    ap.add_argument("path")
    ap.add_argument("--list", action="store_true", help="the chosen timestamps only (no frame fetched)")
    ap.add_argument("--info", action="store_true", help="duration, fps, size, rotation and codec as JSON")
    ap.add_argument("--max-frames", type=int, default=48)
    ap.add_argument("--min-gap", type=float, default=1.0)
    ap.add_argument("--every", type=float, default=4.0)
    ap.add_argument("--scene", type=float, default=0.3)
    a = ap.parse_args(argv)
    try:
        if a.info:
            print(json.dumps(info(a.path)))
            return 0
        s = sample(a.path, max_frames=a.max_frames, min_gap=a.min_gap, every=a.every, scene=a.scene)
        t0 = time.monotonic()
        if a.list:
            for t, why in s.plan():
                print(f"{t:.3f}\t{why}")
        else:
            with s:
                for t, rgb in s:
                    print(f"{t:.3f}\t{rgb.shape[1]}x{rgb.shape[0]}")
        print(f"[frames] {len(s.picks)} frame(s) of {s.analysed} analysed, {s.backend}, "
              f"{time.monotonic() - t0:.2f} s", file=sys.stderr)
    except FramesError as e:
        print(f"[frames] {e}", file=sys.stderr)
        return 3 if isinstance(e, Refused) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
