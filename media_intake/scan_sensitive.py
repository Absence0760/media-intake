#!/usr/bin/env python3
"""Flag sensitive photos/videos, fully offline. Output holds labels, never content.

  <state>/venv/bin/python -I media_intake/scan_sensitive.py DIR OUT.json

For each file it records reason labels only, such as "nudity", "passport-mrz",
"id-barcode", "payment-card", "2fa-qr", "secret-words:<category>",
"unscanned-video", "unscanned-nudity", "unscanned-barcode", "unscanned-type"
(an extension no detector reads) or "unscanned-error" (a check crashed on that
file; the rest of the batch is still scanned). "Not checked" is never "clean".
It never writes or prints any OCR text, barcode contents or image data, so the
report can be read in an AI assistant session without leaking what the image shows. An assistant must never open the flagged files themselves;
you review them in your own image viewer.

Detectors (docs/intake.md has the research and pins):
  nudity    NudeNet 3.4.2 (bundled 320n ONNX model, CPU). Only exposed
            genitalia, anus, female breast and buttocks count, so shirtless
            runners and swimwear don't trip it.
  documents Tesseract OCR: passport MRZ lines, card numbers (Luhn + issuer
            prefix + an expiry/network word nearby), keyword families.
  barcodes  zxing-cpp: PDF417 with an AAMVA header (back of a US licence/ID),
            QR codes holding otpauth:// (2FA setup).
Videos: one frame every 2 s (max 90, at least one for a clip under 2 s) through
the nudity detector; a detector error on any frame flags "unscanned-nudity".
"""
import contextlib, json, os, re, shutil, subprocess, sys, tempfile

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener  # optional
    register_heif_opener()
except Exception:
    pass  # optional: without it HEIC files open as "unreadable" and are flagged

# Every kind stage copies (sources.MEDIA_EXT) must be here or in VID: a kind left out was
# filed unchecked (.gpr was). Anything else is flagged "unscanned-type".
IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".dng", ".gpr"}
VID = {".mp4", ".mov", ".3gp", ".mkv", ".webm"}
NUDE = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
        "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"}
NUDE_MIN = 0.5

WORDS = {
    "identity": r"passport|driver'?s? licen[cs]e|identification card|social security|date of birth|\bDOB\b|green card|permanent resident",
    "banking": r"routing (number|no)|account (number|no)|pay to the order of|iban|sort code|available balance|direct deposit",
    "credentials": r"recovery (code|key|phrase)|backup codes?|seed phrase|secret key|one[- ]time (password|code)|verification code|password|passcode|\bvault\b|bitwarden|1password|keepass|authenticator",
    "medical": r"patient (name|id)|diagnosis|prescription|\bRx\b|medical record|insurance (id|member)",
    "tax": r"\bW-?2\b|\b1099\b|tax return|\bSSN\b|\bEIN\b",
}
WORDS = {k: re.compile(v, re.I) for k, v in WORDS.items()}
MRZ = re.compile(r"(P[<A-Z][A-Z<]{3}[A-Z<]{20,}|[A-Z0-9<]{9}[0-9][A-Z<]{3}[0-9]{6}[0-9][MF<][0-9]{6})")
CARD = re.compile(r"(?<!\d)(\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,7}|\d{4}[ -]?\d{6}[ -]?\d{5})(?!\d)")
CARD_HINT = re.compile(r"valid|thru|good thru|expires|exp\b|\b\d\d/\d\d\b|visa|mastercard|amex|american express|discover|debit|credit", re.I)


def luhn(d):
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        s, alt = s + n, not alt
    return s % 10 == 0


def issuer(d):
    return (d[0] == "4" or d[:2] in {"34", "37"} or 51 <= int(d[:2]) <= 55
            or 2221 <= int(d[:4]) <= 2720 or d[0] == "6")


def ocr_flags(im, tmpdir):
    if not shutil.which("tesseract"):
        return []  # reported once in the summary ("unavailable"), not per file
    g = ImageOps.exif_transpose(im).convert("L")
    g.thumbnail((2000, 2000))
    p = os.path.join(tmpdir, "ocr.png")
    g.save(p)
    r = subprocess.run(["tesseract", p, "-", "--psm", "11", "-l", "eng"],
                       capture_output=True, text=True, timeout=120)
    os.remove(p)
    text = r.stdout
    out = []
    flat = "\n".join(l.replace(" ", "") for l in text.splitlines())
    if MRZ.search(flat):
        out.append("passport-mrz")
    for m in CARD.finditer(text):
        d = re.sub(r"\D", "", m.group(1))
        if 13 <= len(d) <= 19 and issuer(d) and luhn(d) and CARD_HINT.search(text):
            out.append("payment-card")
            break
    out += [f"secret-words:{k}" for k, rx in WORDS.items() if rx.search(text)]
    del text, flat
    return out


def have_zxing():
    try:
        import zxingcpp  # noqa: F401
        return True
    except ImportError:
        return False


def barcode_flags(im):
    try:
        import zxingcpp
    except ImportError:
        return []
    out = []
    try:
        for b in zxingcpp.read_barcodes(ImageOps.exif_transpose(im).convert("RGB")):
            fmt, txt = str(b.format), b.text or ""
            if "PDF417" in fmt and (txt.startswith("@") or "ANSI " in txt or "AAMVA" in txt):
                out.append("id-barcode")
            elif "QR" in fmt and txt.lower().startswith("otpauth"):
                out.append("2fa-qr")
            elif "QR" in fmt and re.search(r"bitcoin:|ethereum:|wallet", txt, re.I):
                out.append("wallet-qr")
    except Exception:
        # Fail closed like the nudity and video checks: a decoder error means
        # "not checked", not "no barcode", so the file goes to review.
        out.append("unscanned-barcode")
    return out


def nude_flags(det, path):
    try:
        hits = det.detect(path)
    except Exception:
        return None
    return ["nudity"] if any(h["class"] in NUDE and h["score"] >= NUDE_MIN for h in hits) else []


def _gst_hevc_decoder():
    """GPU HEVC decoder for GStreamer (Fedora's ffmpeg-free has none)."""
    cands = ["vah265dec"]
    try:
        out = subprocess.run(["gst-inspect-1.0"], capture_output=True, text=True, timeout=60).stdout
        cands += sorted(set(re.findall(r"vulkanh265device\d*dec", out)))[:1]
    except Exception:
        return None
    for d in cands:
        if subprocess.run(["gst-inspect-1.0", d], capture_output=True).returncode == 0:
            return d
    return None


def video_frames(path, fd, max_frames=90):
    """Sample frames (every 2 s, spread over at most max_frames) into fd; [] if undecodable.

    A decoder that is missing, crashes or hangs past its timeout also gives [], so every
    caller treats the clip as "not checked" instead of losing the whole run to one file."""
    try:
        return _video_frames(path, fd, max_frames)
    except (OSError, subprocess.SubprocessError):
        with contextlib.suppress(OSError):  # partial frames go; a vanished one is no error
            for f in os.listdir(fd):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(fd, f))
        return []


def _video_frames(path, fd, max_frames):
    # file: + an absolute path: a name (or a relative source) like "concat:..." or "http:..." is a file, never
    # a protocol ffmpeg would follow.
    src = "file:" + os.path.abspath(path)
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", src], capture_output=True, text=True, timeout=60).stdout or 0)
    except ValueError:
        dur = 0
    every = max(2.0, dur / max_frames) if dur else 2.0
    # eof_action=pass: without it the fps filter drops the only frame of a clip shorter than
    # about half the sampling period (a 0.5 s clip gave no frame at all).
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", src,
                        "-vf", f"fps=1/{every:.3f}:eof_action=pass,scale=960:-2",
                        "-frames:v", str(max_frames), "file:" + os.path.join(os.path.abspath(fd), "%03d.jpg")],
                       capture_output=True, timeout=900)
    frames = sorted(os.listdir(fd))
    if r.returncode == 0 and frames:
        return frames
    for f in frames:
        os.remove(os.path.join(fd, f))
    dec = _gst_hevc_decoder()
    if not dec:
        return []
    # gst-launch-1.0 joins its arguments and parses them as one pipeline, so a "!" or a quote in the clip's
    # name (or in fd's path) could add elements, a sink writing anywhere among them. GStreamer reads the
    # clip through a link in a private (0700) work folder inside fd and writes the frames there; that
    # folder's path must pass SAFE_GST_PATH, else GStreamer is not tried ([]: "not checked", never clean).
    work = os.path.abspath(tempfile.mkdtemp(prefix=".gst-", dir=fd))
    try:
        if not SAFE_GST_PATH.fullmatch(work):
            return []
        os.symlink(os.path.abspath(path), os.path.join(work, "in"))
        r = subprocess.run(gst_frames_command(dec, work, every), capture_output=True, timeout=1800,
                           stdin=subprocess.DEVNULL)
        frames = sorted(f for f in os.listdir(work) if f.endswith(".jpg"))[:max_frames]
        if r.returncode != 0 or not frames:
            return []
        for f in frames:
            os.replace(os.path.join(work, f), os.path.join(fd, f))
        return frames
    finally:
        shutil.rmtree(work, ignore_errors=True)


SAFE_GST_PATH = re.compile(r"[A-Za-z0-9._/+-]+")  # what may stand as a location= value (as gallery.SAFE_GST_PATH)


def gst_frames_command(dec, work, every):
    """gst-launch-1.0 argv that decodes <work>/in (a link to the clip) with the GPU HEVC decoder dec and writes
    a 960 px wide JPEG every `every` seconds to <work>/NNN.jpg. Built as a list, never split from a string;
    work must already match SAFE_GST_PATH. No path but work's reaches it."""
    post = "vapostproc" if dec == "vah265dec" else "vulkandownload"
    num, den = 1000, int(round(every * 1000))
    return ["gst-launch-1.0", "-q", "filesrc", "location=" + os.path.join(work, "in"), "!", "qtdemux", "!",
            "h265parse", "!", dec, "!", post, "!", "videoconvert", "!", "videoscale", "!",
            "video/x-raw,width=960,pixel-aspect-ratio=1/1", "!", "videorate", "!",
            f"video/x-raw,framerate={num}/{den}", "!", "jpegenc", "!",
            "multifilesink", "location=" + os.path.join(work, "%03d.jpg")]


def scan_file(det, path, tmpdir):
    """Reason labels for one file ([] = checked and clean)."""
    ext = os.path.splitext(path)[1].lower()
    reasons = []
    if ext in IMG:
        try:
            im = Image.open(path)
            im.load()
        except Exception:
            return ["unreadable"]
        with im:  # a GIF keeps its file open after load(): close it per file, not at GC
            jpg = path
            if ext in {".heic", ".heif", ".dng", ".gpr", ".webp", ".gif"}:
                jpg = os.path.join(tmpdir, "n.jpg")
                ImageOps.exif_transpose(im).convert("RGB").save(jpg)
            try:
                r = nude_flags(det, jpg)
            finally:
                if jpg != path:
                    os.remove(jpg)
            reasons += ["unscanned-nudity"] if r is None else r
            reasons += barcode_flags(im)
            reasons += ocr_flags(im, tmpdir)
    elif ext in VID:
        fd = os.path.join(tmpdir, "frames")
        shutil.rmtree(fd, ignore_errors=True)  # never judge an earlier clip's frames
        os.makedirs(fd)
        try:
            frames = video_frames(path, fd)
            if not frames:
                reasons.append("unscanned-video")
            for f in frames:
                r = nude_flags(det, os.path.join(fd, f))
                if r is None:  # the detector failed on this frame: not checked, never clean
                    reasons.append("unscanned-nudity")
                elif r:
                    reasons.append("nudity")
                    break
        finally:
            shutil.rmtree(fd, ignore_errors=True)
    else:
        reasons.append("unscanned-type")  # staged as media, but no detector here reads it
    return reasons


def main():
    videos_only = "--videos-only" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--videos-only"]
    src, out_path = args[0], args[1]
    from nudenet import NudeDetector
    det = NudeDetector()
    work = os.path.expanduser("~/.cache/media-intake")
    os.makedirs(work, mode=0o700, exist_ok=True)
    tmpdir = tempfile.mkdtemp(dir=work)
    os.chmod(tmpdir, 0o700)
    flags, n = {}, 0
    files = sorted(os.path.join(r, f) for r, _, fs in os.walk(src) for f in fs)
    if videos_only:
        # Re-check only the videos and merge into an earlier scan.json for the images.
        files = [f for f in files if os.path.splitext(f)[1].lower() in VID]
        try:
            with open(out_path) as fh:
                flags = json.load(fh)
        except FileNotFoundError:
            pass  # no earlier scan to merge into: start empty
        for f in files:
            flags.pop(os.path.relpath(f, src), None)
    try:
        for path in files:
            rel = os.path.relpath(path, src)
            try:
                reasons = scan_file(det, path, tmpdir)
            except Exception:
                # Fail closed and keep going: one file a check crashes on must not lose the
                # batch's scan.json (apply refuses without it) nor be filed as clean.
                reasons = ["unscanned-error"]
            if reasons:
                flags[rel] = sorted(set(reasons))
            n += 1
            if n % 100 == 0:
                print(f"[scan] {n}/{len(files)}", file=sys.stderr, flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    with open(out_path, "w") as f:
        json.dump(flags, f, indent=1)
    counts = {}
    for rs in flags.values():
        for r in rs:
            counts[r] = counts.get(r, 0) + 1
    # A missing detector is a tool problem, not a property of each photo: if it
    # were a per-file label, every image would be quarantined.
    missing = [k for k, ok in (("ocr", shutil.which("tesseract")), ("barcodes", have_zxing())) if not ok]
    if missing:
        print(f"[scan] WARNING: {' and '.join(missing)} not checked (not installed): IDs, cards and "
              "2FA codes are not detected. Install: sudo dnf install tesseract tesseract-langpack-eng python3-zxing-cpp",
              file=sys.stderr)
    print(json.dumps({"scanned": n, "flagged": len(flags), "by_reason": counts, "unavailable": missing}))


if __name__ == "__main__":
    main()
