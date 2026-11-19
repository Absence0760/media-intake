#!/usr/bin/env python3
"""Motion photos: a still with a short video appended, found from the file's bytes alone. Stdlib only.

  python3 -I media_intake/motion.py FILE...     # prints "<path>  motion=<how>|none" (paths and offsets only)

Kinds read (primary sources, checked 2026-11-17):
  Google Motion Photo 1.0 (Pixel; also written by newer Samsung phones), JPEG or HEIC/AVIF primary:
      https://developer.android.com/media/platform/motion-photo-format
      XMP Camera:MotionPhoto=1, and a Container:Directory whose Item:Semantic="MotionPhoto" item has
      Item:Length bytes, the video being the last item: it starts Length bytes before the end of the
      file. The older MicroVideo V1 fields (Camera:MicroVideo=1, MicroVideoOffset = bytes from the end
      of the file) are still read, since older files carry only those. In HEIC/AVIF the video is the
      payload of a top-level 'mpvd' box after every image box. A MotionPhoto=1 left in the XMP of a file
      whose video was stripped is not a motion photo (the spec says readers must confirm a video).
  Samsung (MotionPhoto_Data in the SEFT trailer of a JPEG, or in a top-level 'sefd' box of a HEIC):
      https://exiftool.org/TagNames/Samsung.html (Samsung Trailer Tags, 0x0a30 EmbeddedVideoFile) and
      the layout as exiftool reads it, ProcessSamsung in
      https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/Samsung.pm
      The trailer ends "SEFH" directory, int32 LE directory length, "SEFT". Each 12-byte directory
      entry is (0, type int16 LE, offset back from the directory int32 LE, size int32 LE); a block is
      4 bytes, a name length int32 LE, the name, then the data. Type 0x0a30 ("MotionPhoto_Data") holds
      the MP4, or 12 bytes "mpv2" + offset + size (int32 BE) pointing at it elsewhere in the file.
  Anything else: an ISO BMFF 'ftyp' box after the JPEG's end of image (EOI).

Nothing here writes a file it was given except strip(), which share-copy runs on its own fresh copy,
never on an original. extract() writes the video into a folder the caller made private.
"""
import contextlib, mmap, os, re, struct, sys

EXTS = {".jpg", ".jpeg", ".heic", ".heif"}  # the kinds a motion photo comes as
XMP_SIG = b"http://ns.adobe.com/xap/1.0/\x00"
# Box types a video container may start with (an MP4 starts with ftyp; a QuickTime .mov may not).
VIDEO_BOXES = {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}
CHUNK = 1 << 20


class MotionError(Exception):
    """The file claims an embedded video that can't be located, or a copy can't be stripped safely."""


class Motion:
    """A motion photo: its video spans [start, end) of the file (both None when it claims a video that
    can't be located: never "no video"); image_end is where the primary image ends (None if unknown)."""

    def __init__(self, how, start, end, image_end):
        self.how, self.start, self.end, self.image_end = how, start, end, image_end

    def __repr__(self):
        return f"Motion({self.how!r}, {self.start}, {self.end}, image_end={self.image_end})"


# ---------------------------------------------------------------- JPEG
def jpeg_layout(mm):
    """(end of the primary image just past its EOI, [XMP packets]); end None if the JPEG can't be walked."""
    n, xmp = len(mm), []
    if mm[:2] != b"\xff\xd8":
        return None, xmp
    i = 2
    while i < n:
        if mm[i] != 0xFF:
            return None, xmp
        while i < n and mm[i] == 0xFF:  # fill bytes before a marker
            i += 1
        if i >= n:
            return None, xmp
        m = mm[i]
        i += 1
        if m == 0xD9:
            return i, xmp
        if m == 0x01 or 0xD0 <= m <= 0xD7:  # standalone markers
            continue
        if i + 2 > n:
            return None, xmp
        ln = int.from_bytes(mm[i:i + 2], "big")
        if ln < 2 or i + ln > n:
            return None, xmp
        if m == 0xE1 and mm[i + 2:i + 2 + len(XMP_SIG)] == XMP_SIG:
            xmp.append(bytes(mm[i + 2 + len(XMP_SIG):i + ln]))
        i += ln
        if m == 0xDA:  # entropy-coded data runs to the next marker that isn't stuffing (FF00) or a restart
            while True:
                j = mm.find(b"\xff", i)
                if j < 0 or j + 1 >= n:
                    return None, xmp
                b = mm[j + 1]
                if b == 0x00 or 0xD0 <= b <= 0xD7:
                    i = j + 2
                elif b == 0xFF:
                    i = j + 1
                else:
                    i = j
                    break
    return None, xmp


def _xmp_value(text, name):
    """An XMP property by local name, as an attribute (ns:Name="v") or an element (<ns:Name>v</ns:Name>)."""
    m = re.search(r"\b(?:[\w-]+:)?%s\s*=\s*([\"'])(.*?)\1" % name, text, re.S) or \
        re.search(r"<(?:[\w-]+:)?%s\s*>(.*?)</" % name, text, re.S)
    if not m:
        return None
    return (m.group(2) if m.lastindex == 2 else m.group(1)).strip()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def google_xmp(packets):
    """(claims a video, bytes from the end of the file where it starts or None) from the XMP."""
    text = b"".join(packets).decode("utf-8", "replace")
    claims, back = False, None
    if _int(_xmp_value(text, "MotionPhoto")) == 1 or _int(_xmp_value(text, "MicroVideo")) == 1:
        claims = True
    # Container:Directory items: each <...:Item .../> or <...:Item>...</...:Item>.
    for m in re.finditer(r"<(?:[\w-]+:)?Item\b(.*?)(?:/>|</(?:[\w-]+:)?Item\s*>)", text, re.S):
        item = m.group(1)
        if (_xmp_value(item, "Semantic") or "") == "MotionPhoto":
            claims = True
            length = _int(_xmp_value(item, "Length"))
            if length and length > 0:
                back = length
    if back is None:
        off = _int(_xmp_value(text, "MicroVideoOffset"))
        if off and off > 0:
            back = off
    return claims, back


def _is_box(mm, at, end, kinds):
    if at < 0 or at + 8 > end:
        return False
    size = int.from_bytes(mm[at:at + 4], "big")
    return bytes(mm[at + 4:at + 8]) in kinds and (size == 1 or 8 <= size <= end - at or size == 0)


def _ftyp_after(mm, lo, hi):
    """Offset of the first plausible 'ftyp' box in [lo, hi) (small size, printable brand), else None."""
    p = mm.find(b"ftyp", lo + 4, hi)
    while p >= 0:
        size = int.from_bytes(mm[p - 4:p], "big")
        brand = bytes(mm[p + 4:p + 8])
        if 8 <= size <= 4096 and p - 4 + size <= hi and len(brand) == 4 \
                and all(0x20 <= c < 0x7F for c in brand):
            return p - 4
        p = mm.find(b"ftyp", p + 1, hi)
    return None


# ---------------------------------------------------------------- Samsung SEFT
def _le32(mm, at):
    return int.from_bytes(mm[at:at + 4], "little")


def samsung_trailer(mm, lo, hi):
    """(trailer start, (video start, end) or None) for a SEFT trailer ending at hi, else None.
    Raises MotionError when the trailer is there but malformed."""
    if hi - lo < 8 or mm[hi - 4:hi] != b"SEFT":
        return None
    ln = _le32(mm, hi - 8)
    dir_pos = hi - 8 - ln
    if ln < 12 or dir_pos < lo or mm[dir_pos:dir_pos + 4] != b"SEFH":
        raise MotionError("SEFT trailer without a readable SEFH directory")
    count = _le32(mm, dir_pos + 8)
    if 12 + 12 * count > ln:
        raise MotionError("SEFH directory longer than its block")
    first, video = dir_pos, None
    for k in range(count):
        e = dir_pos + 12 + 12 * k
        typ = int.from_bytes(mm[e + 2:e + 4], "little")
        noff, size = _le32(mm, e + 4), _le32(mm, e + 8)
        if noff > dir_pos - lo or size > noff or size < 8:
            raise MotionError("SEFH entry points outside the trailer")
        b = dir_pos - noff
        first = min(first, b)
        if typ != 0x0A30 or video:
            continue
        nl = _le32(mm, b + 4)
        if 8 + nl > size:
            raise MotionError("SEFT block name longer than its block")
        s, t = b + 8 + nl, b + size
        if t - s == 12 and mm[s:s + 4] == b"mpv2":  # a pointer to the video elsewhere in the file
            s, size2 = struct.unpack(">II", mm[s + 4:s + 12])
            t = s + size2
            if not 0 <= s < t <= len(mm):
                raise MotionError("SEFT mpv2 pointer outside the file")
        video = (s, t)
    return first, video


# ---------------------------------------------------------------- ISO BMFF (HEIC/HEIF)
def boxes(mm, lo=0, hi=None):
    """[(offset, type, header length, size)] of the top-level boxes in [lo, hi); MotionError if malformed."""
    hi = len(mm) if hi is None else hi
    out, i = [], lo
    while i < hi:
        if i + 8 > hi:
            raise MotionError("truncated box header")
        size, typ, hdr = int.from_bytes(mm[i:i + 4], "big"), bytes(mm[i + 4:i + 8]), 8
        if size == 1:
            if i + 16 > hi:
                raise MotionError("truncated box header")
            size, hdr = int.from_bytes(mm[i + 8:i + 16], "big"), 16
        elif size == 0:
            size = hi - i
        if size < hdr or i + size > hi:
            raise MotionError("box runs past the end of the file")
        out.append((i, typ, hdr, size))
        i += size
    return out


def _probe_bmff(mm):
    try:
        bx = boxes(mm)
    except MotionError:
        if mm.find(b"mpvd") >= 0 or mm.find(b"MotionPhoto_Data") >= 0:
            return Motion("heif-unreadable", None, None, None)
        return None
    first = next((o for o, t, _, _ in bx if t in (b"mpvd", b"sefd")), None)
    for o, t, hdr, size in bx:
        if t == b"mpvd" and size > hdr:
            return Motion("google-mpvd", o + hdr, o + size, first)
    for o, t, hdr, size in bx:
        if t == b"sefd":
            try:
                sam = samsung_trailer(mm, o + hdr, o + size)
            except MotionError:
                return Motion("samsung-sefd", None, None, first)
            if sam and sam[1]:
                return Motion("samsung-sefd", sam[1][0], sam[1][1], first)
            if mm.find(b"MotionPhoto_Data", o, o + size) >= 0:
                return Motion("samsung-sefd", None, None, first)
    return None


# ---------------------------------------------------------------- probe
def _probe_jpeg(mm):
    n = len(mm)
    image_end, xmp = jpeg_layout(mm)
    claims, back = google_xmp(xmp)
    if image_end is None:
        # Can't tell where the still ends: never call it clean if anything says there is a video.
        if claims or mm.find(b"MotionPhoto_Data") >= 0:
            return Motion("jpeg-unreadable", None, None, None)
        return None
    if n - image_end < 8:
        return None  # nothing appended (a MotionPhoto=1 left behind after the video was stripped)
    try:
        sam = samsung_trailer(mm, image_end, n)
    except MotionError:
        sam = ()  # malformed: fall back to the other ways in, and fail closed below
    if sam and sam[1] and _is_box(mm, sam[1][0], sam[1][1], VIDEO_BOXES):
        return Motion("samsung", sam[1][0], sam[1][1], image_end)
    if back is not None and image_end <= n - back and _is_box(mm, n - back, n, VIDEO_BOXES):
        return Motion("google", n - back, n, image_end)
    tail = sam[0] if sam else n  # a Samsung trailer after the video is not part of it
    at = _ftyp_after(mm, image_end, tail)
    if at is not None:
        return Motion("ftyp", at, tail, image_end)
    # A SEFT trailer with no MotionPhoto_Data entry is an ordinary Samsung still (its trailer holds other
    # blocks); only a malformed one (sam == ()) or a video entry that didn't check out claims a video.
    if claims or sam == () or mm.find(b"MotionPhoto_Data", image_end) >= 0:
        return Motion("unlocated", None, None, image_end)
    return None


def probe(path):
    """A Motion if path is a motion photo (or claims to be one), else None. Reads only; OSError if unreadable."""
    if os.path.splitext(path)[1].lower() not in EXTS:
        return None
    with open(path, "rb") as f:
        if os.fstat(f.fileno()).st_size < 12:
            return None
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            if mm[:2] == b"\xff\xd8":
                return _probe_jpeg(mm)
            if mm[4:8] == b"ftyp":
                return _probe_bmff(mm)
    return None


def extract(path, info, folder, name="motion-video.mp4"):
    """Copy the embedded video of `path` (a Motion from probe) to <folder>/<name>, mode 0600; its path.
    folder must already be private. The original is only read. MotionError when there is nothing to copy."""
    if info is None or info.start is None or info.end is None or info.end <= info.start:
        raise MotionError("embedded video not located")
    dst = os.path.join(folder, name)
    with contextlib.suppress(FileNotFoundError):
        os.remove(dst)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            src.seek(info.start)
            left = info.end - info.start
            while left:
                buf = src.read(min(CHUNK, left))
                if not buf:
                    raise MotionError("file ended inside the embedded video")
                out.write(buf)
                left -= len(buf)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(dst)
        raise
    return dst


def strip(copy):
    """Cut the embedded video (and any other trailer) off `copy`, a share-copy's own fresh file, in place.
    JPEG: truncated just past its EOI. HEIC/HEIF: trailing 'mpvd'/'sefd' boxes dropped. Raises MotionError
    when the copy still claims a video that can't be cut off safely. Never call this on an original."""
    if os.path.splitext(copy)[1].lower() not in EXTS:
        return
    with open(copy, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size < 12:
            return
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            cut = None
            if mm[:2] == b"\xff\xd8":
                end, _ = jpeg_layout(mm)
                if end is None:
                    if _probe_jpeg(mm):
                        raise MotionError("can't find where the still image ends")
                elif end < size:
                    cut = end
            elif mm[4:8] == b"ftyp":
                try:
                    bx = boxes(mm)
                except MotionError:
                    if _probe_bmff(mm):
                        raise
                    bx = []
                drop = [k for k, (_, t, _, _) in enumerate(bx) if t in (b"mpvd", b"sefd")]
                if drop:
                    if any(t not in (b"mpvd", b"sefd", b"free", b"skip") for _, t, _, _ in bx[drop[0]:]):
                        raise MotionError("the embedded video is not at the end of the file")
                    cut = bx[drop[0]][0]
    if cut is not None:
        os.truncate(copy, cut)
    if probe(copy):
        raise MotionError("embedded video still present after stripping")


def main():
    for p in sys.argv[1:]:
        info = probe(p)
        print(f"{p}  motion={info.how if info else 'none'}"
              + (f" video={info.start}-{info.end}" if info and info.start is not None else ""))


if __name__ == "__main__":
    main()
