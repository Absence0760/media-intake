#!/usr/bin/env python3
"""Motion photos: the video appended to a still is found, scanned and kept out of share copies.

  python3 -I -m unittest discover -s tests -p test_motion.py

motion.py (detection of the Google Motion Photo / MicroVideo XMP, Samsung's SEFT
MotionPhoto_Data trailer, a bare 'ftyp' box after the JPEG's end, and HEIC 'mpvd'
boxes; extraction; strip), scan_sensitive.scan_file / main() on motion photos (the
embedded video's flags merged into the photo's, "unscanned-video" when it can't be
copied out, the temp copy deleted, the original never written), and share-copy on
both its exiftool path and its Pillow fallback.

Synthetic media only: a small Pillow JPEG, a tiny ffmpeg colour clip (cases that
need ffmpeg or exiftool skip without them; the CI image has both), or raw 'ftyp'
bytes for detection. Assertions are labels, offsets, paths and checksums only.
"""
import argparse, hashlib, io, json, os, shutil, struct, subprocess, sys, tempfile, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config; puts media_intake/ on sys.path)
support.check_hermetic()

from PIL import Image  # noqa: E402

import motion  # noqa: E402
import scan_sensitive  # noqa: E402
from test_detectors_media_kinds import BLUE, RED, FakeNudeDetector, video  # noqa: E402

PY = sys.executable
INTAKE = os.path.join(support.PKG, "intake.py")
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
HAVE_EXIFTOOL = bool(shutil.which("exiftool"))

# A minimal MP4 header: enough for detection, not a playable clip.
FAKE_MP4 = struct.pack(">I", 24) + b"ftypmp42" + b"\0\0\0\0" + b"mp42isom" + struct.pack(">I", 16) + b"mdat" + b"\0" * 8


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def jpeg_bytes(color=BLUE, xmp=None, size=(64, 48)):
    """A small baseline JPEG; with xmp, an APP1 XMP packet right after SOI."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    if xmp is None:
        return data
    body = motion.XMP_SIG + xmp.encode()
    return data[:2] + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + data[2:]


def google_xmp(length=None, micro_offset=None, flag=1):
    items = ""
    if length is not None:
        items = ('<Container:Directory><rdf:Seq>'
                 '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"'
                 ' Item:Length="0" Item:Padding="0"/></rdf:li>'
                 '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4"'
                 f' Item:Semantic="MotionPhoto" Item:Length="{length}" Item:Padding="0"/></rdf:li>'
                 '</rdf:Seq></Container:Directory>')
    micro = f' GCamera:MicroVideo="1" GCamera:MicroVideoOffset="{micro_offset}"' if micro_offset else ""
    return ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:Camera="http://ns.google.com/photos/1.0/camera/"'
            ' xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"'
            ' xmlns:Container="http://ns.google.com/photos/1.0/container/"'
            ' xmlns:Item="http://ns.google.com/photos/1.0/container/item/"'
            f' Camera:MotionPhoto="{flag}" Camera:MotionPhotoVersion="1"{micro}>'
            f'{items}</rdf:Description></rdf:RDF></x:xmpmeta>')


def samsung_trailer(mp4, pointer_at=None):
    """A SEFT trailer (as exiftool's Samsung.pm reads it) holding mp4 as its MotionPhoto_Data block; with
    pointer_at, the block is an 'mpv2' pointer to mp4 at that absolute offset instead."""
    name = b"MotionPhoto_Data"
    data = mp4 if pointer_at is None else b"mpv2" + struct.pack(">II", pointer_at, len(mp4))
    block = b"\0\0\x30\x0a" + struct.pack("<I", len(name)) + name + data
    other = b"\0\0\x01\x0a" + struct.pack("<I", 15) + b"Image_UTC_Data" + b"_" + b"1700000000000"
    blob = other + block
    # Offsets are counted back from the start of the SEFH directory.
    entries = [(0x0A01, len(blob), len(other)), (0x0A30, len(block), len(block))]
    sefh = b"SEFH" + struct.pack("<II", 107, len(entries))
    sefh += b"".join(struct.pack("<HHII", 0, t, noff, size) for t, noff, size in entries)
    return blob + sefh + struct.pack("<I", len(sefh)) + b"SEFT"


def bmff_box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def heic_bytes(mp4=None, after=b""):
    """An ISO BMFF stand-in for a HEIC (ftyp, meta, mdat), with a trailing 'mpvd' box holding mp4."""
    data = bmff_box(b"ftyp", b"heic\0\0\0\0mif1heic") + bmff_box(b"meta", b"\0" * 24) + bmff_box(b"mdat", b"\1" * 64)
    if mp4 is not None:
        data += bmff_box(b"mpvd", mp4)
    return data + after


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def read(path):
    with open(path, "rb") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="motion-test-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def P(self, rel):
        return os.path.join(self.d, rel)

    def clip(self, color, seconds=2.0):
        """A tiny mp4 made with ffmpeg; its bytes."""
        return read(video(self.P(f"clips/{color[0]}-{seconds}.mp4"), color, seconds=seconds))


# ---------------------------------------------------------------- detection
class Detect(Base):
    def test_plain_jpeg_is_not_a_motion_photo(self):
        self.assertIsNone(motion.probe(write(self.P("a.jpg"), jpeg_bytes())))
        self.assertIsNone(motion.probe(write(self.P("b.jpg"), jpeg_bytes(xmp=google_xmp(flag=0)))))

    def test_google_motion_photo_by_item_length(self):
        still = jpeg_bytes(xmp=google_xmp(length=len(FAKE_MP4)))
        info = motion.probe(write(self.P("PXL_MP.jpg"), still + FAKE_MP4))
        self.assertEqual((info.how, info.start, info.end, info.image_end),
                         ("google", len(still), len(still) + len(FAKE_MP4), len(still)))

    def test_google_microvideo_offset(self):
        still = jpeg_bytes(xmp=google_xmp(micro_offset=len(FAKE_MP4)))
        info = motion.probe(write(self.P("MVIMG.jpg"), still + FAKE_MP4))
        self.assertEqual((info.how, info.start), ("google", len(still)))

    def test_google_xmp_as_elements(self):
        xmp = google_xmp().replace(' Camera:MotionPhoto="1"', "").replace(
            "</rdf:Description>", "<Camera:MotionPhoto>1</Camera:MotionPhoto></rdf:Description>")
        info = motion.probe(write(self.P("e.jpg"), jpeg_bytes(xmp=xmp) + FAKE_MP4))
        self.assertEqual(info.how, "ftyp")  # no Length: located by its ftyp box

    def test_samsung_trailer(self):
        still = jpeg_bytes()
        info = motion.probe(write(self.P("s.jpg"), still + samsung_trailer(FAKE_MP4)))
        self.assertEqual(info.how, "samsung")
        self.assertEqual(read(self.P("s.jpg"))[info.start:info.end], FAKE_MP4)

    def test_samsung_trailer_without_video_is_a_still(self):
        # Samsung stills carry a SEFT trailer too (here only Image_UTC_Data): not a motion photo.
        other = b"\0\0\x01\x0a" + struct.pack("<I", 14) + b"Image_UTC_Data" + b"1700000000000"
        sefh = b"SEFH" + struct.pack("<II", 107, 1) + struct.pack("<HHII", 0, 0x0A01, len(other), len(other))
        trailer = other + sefh + struct.pack("<I", len(sefh)) + b"SEFT"
        self.assertIsNone(motion.probe(write(self.P("still.jpg"), jpeg_bytes() + trailer)))

    def test_samsung_mpv2_pointer(self):
        still = jpeg_bytes()
        data = still + FAKE_MP4 + samsung_trailer(FAKE_MP4, pointer_at=len(still))
        info = motion.probe(write(self.P("p.jpg"), data))
        self.assertEqual((info.how, info.start, info.end), ("samsung", len(still), len(still) + len(FAKE_MP4)))

    def test_bare_ftyp_after_eoi(self):
        still = jpeg_bytes()
        info = motion.probe(write(self.P("f.jpg"), still + FAKE_MP4))
        self.assertEqual((info.how, info.start, info.end), ("ftyp", len(still), len(still) + len(FAKE_MP4)))

    def test_other_trailer_without_video_is_a_still(self):
        # A camera's preview JPEG (or an Ultra HDR gain map) after EOI is not a video.
        self.assertIsNone(motion.probe(write(self.P("t.jpg"), jpeg_bytes() + jpeg_bytes(RED))))

    def test_residual_flag_without_video_is_a_still(self):
        # The spec: MotionPhoto=1 survives editors that drop the video; readers must confirm a video.
        self.assertIsNone(motion.probe(write(self.P("r.jpg"), jpeg_bytes(xmp=google_xmp(length=500)))))

    def test_claimed_but_unlocated_fails_closed(self):
        info = motion.probe(write(self.P("u.jpg"), jpeg_bytes(xmp=google_xmp(length=10 ** 6)) + b"\x07" * 400))
        self.assertEqual((info.how, info.start), ("unlocated", None))
        info = motion.probe(write(self.P("m.jpg"), jpeg_bytes() + b"MotionPhoto_Data" + b"\x07" * 64))
        self.assertEqual(info.start, None)

    def test_malformed_samsung_trailer_fails_closed(self):
        info = motion.probe(write(self.P("bad.jpg"), jpeg_bytes() + b"\x07" * 40 + struct.pack("<I", 9999) + b"SEFT"))
        self.assertEqual((info.how, info.start), ("unlocated", None))

    def test_heic_mpvd_box(self):
        data = heic_bytes(FAKE_MP4)
        info = motion.probe(write(self.P("h.heic"), data))
        self.assertEqual((info.how, data[info.start:info.end]), ("google-mpvd", FAKE_MP4))
        self.assertIsNone(motion.probe(write(self.P("plain.heic"), heic_bytes())))

    def test_heic_samsung_sefd_box(self):
        data = heic_bytes() + bmff_box(b"sefd", samsung_trailer(FAKE_MP4))
        info = motion.probe(write(self.P("s.heic"), data))
        self.assertEqual((info.how, data[info.start:info.end]), ("samsung-sefd", FAKE_MP4))

    def test_other_extensions_are_not_probed(self):
        self.assertIsNone(motion.probe(write(self.P("a.png"), jpeg_bytes() + FAKE_MP4)))

    def test_cli_prints_paths_and_offsets_only(self):
        p = write(self.P("f.jpg"), jpeg_bytes() + FAKE_MP4)
        r = subprocess.run([PY, "-I", os.path.join(support.PKG, "motion.py"), p], capture_output=True, text=True,
                           check=True)
        self.assertRegex(r.stdout.strip(), r"^.*f\.jpg  motion=ftyp video=\d+-\d+$")


# ---------------------------------------------------------------- extraction
class Extract(Base):
    def test_extract_copies_exact_bytes_privately_and_never_writes_the_original(self):
        src = write(self.P("s.jpg"), jpeg_bytes() + samsung_trailer(FAKE_MP4))
        before, mtime = sha(src), os.stat(src).st_mtime_ns
        work = tempfile.mkdtemp(dir=self.d)
        out = motion.extract(src, motion.probe(src), work)
        self.assertEqual(read(out), FAKE_MP4)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertEqual((sha(src), os.stat(src).st_mtime_ns), (before, mtime))

    def test_extract_refuses_an_unlocated_video(self):
        src = write(self.P("u.jpg"), jpeg_bytes() + b"MotionPhoto_Data" + b"\x07" * 64)
        work = tempfile.mkdtemp(dir=self.d)
        with self.assertRaises(motion.MotionError):
            motion.extract(src, motion.probe(src), work)
        self.assertEqual(os.listdir(work), [])

    def test_extract_removes_a_partial_copy(self):
        src = write(self.P("f.jpg"), jpeg_bytes() + FAKE_MP4)
        info = motion.probe(src)
        info.end += 1000  # the file ends inside the video
        work = tempfile.mkdtemp(dir=self.d)
        with self.assertRaises(motion.MotionError):
            motion.extract(src, info, work)
        self.assertEqual(os.listdir(work), [])


# ---------------------------------------------------------------- scanning
@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class Scan(Base):
    def scan(self, path, mode="red"):
        FakeNudeDetector.mode, FakeNudeDetector.calls = mode, []
        tmp = tempfile.mkdtemp(dir=self.d)
        got = scan_sensitive.scan_file(FakeNudeDetector(), path, tmp)
        self.assertEqual(os.listdir(tmp), [], "the extracted video or its frames were left behind")
        return got

    def test_embedded_video_flags_merge_into_the_photo(self):
        red = self.clip(RED)
        for name, data in (("google_MP.jpg", jpeg_bytes(xmp=google_xmp(length=len(red))) + red),
                           ("samsung.jpg", jpeg_bytes() + samsung_trailer(red)),
                           ("bare.jpg", jpeg_bytes() + red)):
            p = write(self.P("src/" + name), data)
            before = sha(p)
            self.assertEqual(self.scan(p), ["nudity"], name)  # the still is blue: the video is what's flagged
            self.assertEqual(sha(p), before, f"original written: {name}")
            self.assertTrue(any(os.path.basename(os.path.dirname(c)) == "frames" for c in FakeNudeDetector.calls))

    def test_clean_embedded_video_is_clean(self):
        blue = self.clip(BLUE)
        self.assertEqual(self.scan(write(self.P("src/ok.jpg"), jpeg_bytes() + samsung_trailer(blue))), [])

    def test_extraction_failure_is_not_checked(self):
        p = write(self.P("src/x.jpg"), jpeg_bytes() + samsung_trailer(self.clip(RED)))
        with mock.patch.object(motion, "extract", side_effect=OSError("disk full")):
            self.assertEqual(self.scan(p), ["unscanned-video"])

    def test_probe_failure_is_not_checked(self):
        p = write(self.P("src/r.jpg"), jpeg_bytes())
        with mock.patch.object(motion, "probe", side_effect=OSError("read error")):
            self.assertEqual(self.scan(p), ["unscanned-video"])
            self.assertTrue(scan_sensitive.is_motion(p), "--videos-only must re-check a file it can't probe")

    def test_unlocated_or_undecodable_video_is_not_checked(self):
        u = write(self.P("src/u.jpg"), jpeg_bytes(xmp=google_xmp(length=10 ** 6)) + b"\x07" * 400)
        self.assertEqual(self.scan(u), ["unscanned-video"])
        g = write(self.P("src/g.jpg"), jpeg_bytes() + FAKE_MP4 + os.urandom(3000))
        self.assertEqual(self.scan(g), ["unscanned-video"])

    def test_frame_detector_error_is_not_checked(self):
        p = write(self.P("src/n.jpg"), jpeg_bytes() + self.clip(BLUE))
        self.assertEqual(self.scan(p, mode="raise-frames"), ["unscanned-nudity"])

    def test_main_merges_and_videos_only_rechecks_motion_photos(self):
        src = self.P("batch")
        write(os.path.join(src, "p/motion.jpg"), jpeg_bytes() + samsung_trailer(self.clip(RED)))
        write(os.path.join(src, "p/still.jpg"), jpeg_bytes())
        out = self.P("scan.json")
        home = self.P("home")
        os.makedirs(home)
        FakeNudeDetector.mode, FakeNudeDetector.calls = "red", []
        fake_nudenet = mock.MagicMock(NudeDetector=FakeNudeDetector)
        for extra in ([], ["--videos-only"]):
            with mock.patch.dict(sys.modules, {"nudenet": fake_nudenet}), \
                    mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(sys, "argv", ["scan_sensitive.py", src, out, *extra]), \
                    mock.patch.object(scan_sensitive, "ocr_flags", lambda im, tmp, keep=None: []), \
                    mock.patch("sys.stdout", new_callable=io.StringIO), \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                FakeNudeDetector.calls = []
                scan_sensitive.main()
            with open(out) as f:
                self.assertEqual(json.load(f), {"p/motion.jpg": ["nudity"]}, extra)
            if extra:
                stills = [c for c in FakeNudeDetector.calls if c.endswith("still.jpg")]
                self.assertEqual(stills, [], "--videos-only re-scanned a plain still")


# ---------------------------------------------------------------- share-copy
class ShareCopy(Base):
    def setUp(self):
        super().setUp()
        self.home = self.P("home")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=os.path.join(self.home, ".local/share/mi"),
                        PHOTO_LIBRARY=os.path.join(self.home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"))

    def share(self, *files, path=None, code=0):
        env = dict(self.env, PATH=path) if path is not None else self.env
        out_dir = self.P("share-out")
        r = subprocess.run([PY, "-I", INTAKE, "share-copy", *files, "--out", out_dir], env=env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return out_dir, r

    def motion_photos(self):
        still = jpeg_bytes()
        return [write(self.P("src/google_MP.jpg"), jpeg_bytes(xmp=google_xmp(length=len(FAKE_MP4))) + FAKE_MP4),
                write(self.P("src/samsung.jpg"), still + samsung_trailer(FAKE_MP4)),
                write(self.P("src/micro.jpg"), jpeg_bytes(xmp=google_xmp(micro_offset=len(FAKE_MP4))) + FAKE_MP4)]

    def check_copies(self, srcs, out_dir):
        for s in srcs:
            dst = os.path.join(out_dir, os.path.basename(s))
            data = read(dst)
            self.assertIsNone(motion.probe(dst), s)
            self.assertNotIn(b"ftyp", data, s)
            self.assertNotIn(b"MotionPhoto", data, s)
            self.assertNotIn(b"SEFT", data, s)
            with Image.open(dst) as im:
                im.load()
                self.assertEqual(im.size, (64, 48))

    @unittest.skipUnless(HAVE_EXIFTOOL, "needs exiftool")
    def test_exiftool_path_drops_the_embedded_video(self):
        srcs = self.motion_photos()
        sums = {s: sha(s) for s in srcs}
        out_dir, _ = self.share(*srcs)
        self.check_copies(srcs, out_dir)
        self.assertEqual({s: sha(s) for s in srcs}, sums, "an original was written")

    @unittest.skipUnless(HAVE_EXIFTOOL, "needs exiftool")
    def test_exiftool_all_drops_a_jpeg_trailer_itself(self):
        # strip() is the backstop; exiftool -all= alone already drops the JPEG trailer (its Trailer group).
        os.makedirs(self.P("et"))
        for src in self.motion_photos():
            dst = self.P("et/" + os.path.basename(src))
            subprocess.run(["exiftool", "-q", "-q", "-all=", "-tagsfromfile", "@", "-Orientation", "-ICC_Profile",
                            "-o", dst, src], check=True)
            self.assertIsNone(motion.probe(dst), src)
            self.assertNotIn(b"ftyp", read(dst), src)

    def test_pillow_fallback_drops_the_embedded_video(self):
        srcs = self.motion_photos()
        sums = {s: sha(s) for s in srcs}
        empty = self.P("empty-path")
        os.makedirs(empty)
        out_dir, _ = self.share(*srcs, path=empty)  # no exiftool on PATH: the Pillow re-encode
        self.check_copies(srcs, out_dir)
        self.assertEqual({s: sha(s) for s in srcs}, sums, "an original was written")

    def test_a_copy_that_cannot_be_stripped_is_removed(self):
        import intake  # in-process, so strip() can be made to fail
        src = self.motion_photos()[1]
        before = sha(src)
        out_dir = self.P("share-out")
        for exc in (motion.MotionError("not at the end"), OSError("read error")):
            with mock.patch.object(motion, "strip", side_effect=exc), \
                    mock.patch.object(intake.shutil, "which", return_value=None), \
                    self.assertRaises(SystemExit) as cm:
                intake.cmd_share_copy(argparse.Namespace(files=[src], out=out_dir))
            self.assertIn("embedded video could not be removed", str(cm.exception.code))
            self.assertEqual(os.listdir(out_dir), [], f"a copy that may hold the video was left ({exc!r})")
        self.assertEqual(sha(src), before)


class Strip(Base):
    def test_jpeg_truncated_at_eoi(self):
        still = jpeg_bytes()
        for data in (still + FAKE_MP4, still + samsung_trailer(FAKE_MP4), still):
            p = write(self.P("c.jpg"), data)
            motion.strip(p)
            self.assertEqual(read(p), still)

    def test_heic_trailing_boxes_dropped(self):
        p = write(self.P("c.heic"), heic_bytes(FAKE_MP4, after=bmff_box(b"free", b"")))
        motion.strip(p)
        self.assertEqual(read(p), heic_bytes())
        p = write(self.P("s.heic"), heic_bytes() + bmff_box(b"sefd", samsung_trailer(FAKE_MP4)))
        motion.strip(p)
        self.assertEqual(read(p), heic_bytes())

    def test_heic_video_not_at_the_end_is_refused(self):
        p = write(self.P("c.heic"), heic_bytes(FAKE_MP4, after=bmff_box(b"meta", b"\0" * 8)))
        with self.assertRaises(motion.MotionError):
            motion.strip(p)

    def test_unwalkable_copies_that_claim_a_video_are_refused(self):
        p = write(self.P("j.jpg"), b"\xff\xd8\xff\xe1\x00" + b"MotionPhoto_Data" + b"\0" * 32)
        with self.assertRaises(motion.MotionError):
            motion.strip(p)
        p = write(self.P("h.heic"), heic_bytes() + struct.pack(">I", 9999) + b"mpvd" + FAKE_MP4)
        with self.assertRaises(motion.MotionError):
            motion.strip(p)

    def test_other_kinds_untouched(self):
        p = write(self.P("c.png"), b"\x89PNG" + FAKE_MP4)
        motion.strip(p)
        self.assertEqual(read(p), b"\x89PNG" + FAKE_MP4)


if __name__ == "__main__":
    unittest.main()
