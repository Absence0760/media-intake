#!/usr/bin/env python3
"""Every gallery operation on a photo, on a video and on a mixed photo + video selection.

  python3 -I -m unittest discover -s tests -p test_gallery_media_kinds.py

Synthetic media in a throwaway HOME (tests/support.py): Pillow photos (JPEG, PNG) and ffmpeg
H.264 clips in an .mp4 and an upper-case .MOV (QuickTime container), served by the real gallery
server. Trash, Erase and Share run the real intake.py against the throwaway HOME; Open
runs a fake xdg-open first on PATH that only records its arguments. Each test checks what really
changed: the XMP sidecar (<file>.<ext>.xmp, read back with exiftool), files and sidecars gone,
trashed.json, the erase log, index.json, the thumbnail cache, the job's status, the originals'
sha256 (never written) and the API's replies.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import test_gallery as G  # noqa: E402  (Server, img, sha, tags_of, write_tags, FAKE_OPENER, until)

from PIL import Image  # noqa: E402

H264 = ("libx264", "libopenh264")
TITLE = "synthetic-title-to-strip"


def h264():
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return next((e for e in H264 if f" {e} " in r.stdout), None)


def mkvid(path, seconds=2):
    """A small H.264 test pattern (160x120, yuv420p) with a title tag share-copy must strip. The
    container follows the extension (.mp4 = MP4, .mov/.MOV = QuickTime)."""
    enc = h264()
    if not enc:
        raise unittest.SkipTest("no H.264 encoder in this ffmpeg")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = ["-f", "mov"] if path.lower().endswith(".mov") else ["-f", "mp4"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=160x120:rate=10:duration={seconds}", "-c:v", enc, "-pix_fmt", "yuv420p",
                    "-metadata", f"title={TITLE}", *fmt, path], check=True)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def orientation_of(p):
    return G.tags_of(p)[2]


class MediaKindsTest(unittest.TestCase):
    """Photos p1.jpg (a trash suggestion) and p2.png (no sidecar); videos v1.mp4 (a trash suggestion, in an
    event) and IMG_0042.MOV (no sidecar), in the photo library and the personal videos folder."""

    def setUp(self):
        for tool in ("ffmpeg", "ffprobe", "exiftool"):
            if not shutil.which(tool):
                self.skipTest(f"{tool} is not installed")
        self.tmp = tempfile.mkdtemp(prefix="gallery-kinds-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids,
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.thumbs = os.path.join(self.home, ".cache/media-intake/thumbs")
        self.p1 = os.path.join(self.lib, "2026/2026-11/p1.jpg")
        self.p2 = os.path.join(self.lib, "2026/2026-10/p2.png")
        self.v1 = os.path.join(self.vids, "2026/2026-11/v1.mp4")
        self.v2 = os.path.join(self.vids, "2026/2026-10/IMG_0042.MOV")
        G.img(self.p1, (200, 30, 30), (400, 300))
        G.img(self.p2, (30, 200, 30), (400, 300))
        mkvid(self.v1)
        mkvid(self.v2)
        G.write_tags(self.p1, ["Project|Personal", "Review|maybe-trash", "Label|food"])
        G.write_tags(self.v1, ["Project|Personal", "Review|maybe-trash", "Event|picnic", "Trips|coast"])
        self.photos, self.videos = [self.p1, self.p2], [self.v1, self.v2]
        self.all = self.photos + self.videos
        self.orig = {p: G.sha(p) for p in self.all}
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        return s

    def ids(self, s):
        j = s.get("/api/list?limit=500")
        by = {i["n"]: i["id"] for i in j["items"]}
        return {p: by[os.path.basename(p)] for p in self.all if os.path.basename(p) in by}

    def listed(self, s, **f):
        j = s.get("/api/list?" + urllib.parse.urlencode(dict(f, limit=500)))
        return sorted(i["n"] for i in j["items"])

    def thumb(self, s, i, path, turn=0):
        """The cached thumbnail /t/ made, keyed by content and the turn baked in: <sha256>-r<turn>.jpg."""
        r, b = s.req("GET", f"/t/{i}")
        self.assertEqual((r.status, r.getheader("Content-Type"), b[:2]), (200, "image/jpeg", b"\xff\xd8"), path)
        t = os.path.join(self.thumbs, f"{self.orig[path]}-r{turn}.jpg")
        self.assertTrue(os.path.isfile(t), path)
        self.assertEqual(oct(os.stat(t).st_mode & 0o777), "0o600")
        return t

    def wait_job(self, s):
        return G.until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the job to end")

    def assert_originals(self, paths=None):
        for p in paths or self.all:
            self.assertEqual(G.sha(p), self.orig[p], p)

    def assert_kept(self, p):
        h, dc, _ = G.tags_of(p)
        self.assertIn("Owner|keep", h, p)
        self.assertIn("Owner|not-trash", h, p)
        self.assertNotIn("Review|maybe-trash", h, p)
        self.assertNotIn("maybe-trash", dc, p)
        return h

    # ------------------------------------------------------------ listing, facets, detail, media routes
    def test_listing_facets_and_kind_filter(self):
        s = self.start()
        names = sorted(os.path.basename(p) for p in self.all)
        self.assertEqual(self.listed(s), names)
        self.assertEqual(self.listed(s, kind="photo"), ["p1.jpg", "p2.png"])
        self.assertEqual(self.listed(s, kind="video"), ["IMG_0042.MOV", "v1.mp4"])
        c = s.get("/api/facets")["counts"]
        self.assertEqual((c["kind:photo"], c["kind:video"]), (2, 2))
        # the trash suggestions hold one of each kind
        self.assertEqual(self.listed(s, view="suggest"), ["p1.jpg", "v1.mp4"])
        self.assertEqual(c["suggest"], 2)
        self.assertEqual(self.listed(s, event="picnic"), ["v1.mp4"])
        self.assertEqual(self.listed(s, mine="Trips|coast"), ["v1.mp4"])
        m = {i["n"]: i["m"] for i in s.get("/api/list?limit=500")["items"]}
        self.assertEqual(m, {"p1.jpg": "photo", "p2.png": "photo", "v1.mp4": "video", "IMG_0042.MOV": "video"})

    def test_detail_media_preview_and_thumbnails(self):
        s = self.start()
        ids = self.ids(s)
        for p, kind, mime in ((self.p1, "photo", "image/jpeg"), (self.p2, "photo", "image/png"),
                              (self.v1, "video", "video/mp4"), (self.v2, "video", "video/mp4")):
            i = ids[p]
            d = s.get(f"/api/item/{i}")
            self.assertEqual((d["kind"], d["video"], d["src"], d["rot"]), (kind, kind == "video", f"/m/{i}", 0), p)
            self.assertEqual(d["res"], "400x300" if kind == "photo" else "160x120", p)
            r, b = s.req("GET", f"/m/{i}")
            self.assertEqual((r.status, r.getheader("Content-Type"), len(b)), (200, mime, os.path.getsize(p)), p)
            # Range (how a <video> loads and seeks; served the same for a photo)
            r, b = s.req("GET", f"/m/{i}", headers={"Range": "bytes=10-19"})
            self.assertEqual((r.status, r.getheader("Content-Range"), len(b)),
                             (206, f"bytes 10-19/{os.path.getsize(p)}", 10), p)
            with open(p, "rb") as f:
                f.seek(10)
                self.assertEqual(b, f.read(10), p)
            # the 1600 px preview is for photos only
            r, b = s.req("GET", f"/p/{i}")
            self.assertEqual(r.status, 200 if kind == "photo" else 404, p)
            t = self.thumb(s, i, p)
            with Image.open(t) as im:
                self.assertEqual(im.size, (320, 240), p)  # max 320 px, a video frame too
        self.assert_originals()

    # ------------------------------------------------------------ Keep
    def test_keep_photo_video_and_mixed(self):
        s = self.start()
        ids = self.ids(s)
        # one photo
        code, j = s.post("/api/keep", {"ids": [ids[self.p1]]})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "Kept 1. It won't be suggested as junk again.")
        self.assertIn("Label|food", self.assert_kept(self.p1))
        self.assertEqual([(x["id"], x["f"]) for x in j["items"]], [(ids[self.p1], ["kept"])])
        # one video
        code, j = s.post("/api/keep", {"ids": [ids[self.v1]]})
        self.assertEqual(code, 200, j)
        h = self.assert_kept(self.v1)
        for t in ("Event|picnic", "Trips|coast", "Project|Personal"):
            self.assertIn(t, h)
        self.assertEqual(G.tags_of(self.v1)[1].count("coast"), 1)
        self.assertEqual([(x["id"], x["m"], x["f"]) for x in j["items"]], [(ids[self.v1], "video", ["kept"])])
        self.assertTrue(s.get(f"/api/item/{ids[self.v1]}")["kept"])
        self.assertEqual(self.listed(s, view="suggest"), [])
        # a mixed selection with no sidecars yet (a PNG and a .MOV) plus one already kept: one request
        for p in (self.p2, self.v2):
            self.assertFalse(os.path.exists(p + ".xmp"))
        code, j = s.post("/api/keep", {"ids": [ids[self.p2], ids[self.v2], ids[self.v1], ids[self.v2]]})
        self.assertEqual(code, 200, j)
        self.assertTrue(j["result"].startswith("Kept 3. They "), j)  # a repeated id counts once
        for p in (self.p2, self.v2):
            self.assertTrue(os.path.isfile(p + ".xmp"), p)  # <file>.<ext>.xmp, the extension's case kept
            self.assertEqual(sorted(G.tags_of(p)[0]), ["Owner|keep", "Owner|not-trash"], p)
        self.assertEqual(G.tags_of(self.v1)[0].count("Owner|keep"), 1)  # Keep again: no duplicate
        self.assertEqual(sorted(x["id"] for x in j["items"]), sorted([ids[self.p2], ids[self.v2], ids[self.v1]]))
        self.assertTrue(all("kept" in x["f"] for x in j["items"]), j["items"])
        self.assertEqual(sorted(t for t, _ in s.get("/api/facets")["facets"]["mine"]), ["Trips|coast"])
        self.assertFalse(os.path.exists(self.v2[:-4] + ".mov.xmp"))  # never a second, lower-case sidecar
        self.assert_originals()

    # ------------------------------------------------------------ my tags
    def test_tags_photo_video_and_mixed(self):
        s = self.start()
        ids = self.ids(s)
        for sel in ([self.p2], [self.v2], self.all):
            code, j = s.post("/api/tag", {"ids": [ids[p] for p in sel], "tag": "Trips|Spain", "op": "add"})
            self.assertEqual(code, 200, j)
            self.assertEqual(j["result"], f"added 'Trips|Spain' on {len(sel)}")
            for p in sel:
                h, dc, _ = G.tags_of(p)
                self.assertEqual(h.count("Trips|Spain"), 1, p)
                self.assertIn("Spain", dc, p)
            self.assertEqual(sorted(x["id"] for x in j["items"]), sorted(ids[p] for p in sel))
        self.assertIn(["Trips|Spain", 4], s.get("/api/facets")["facets"]["mine"])
        self.assertEqual(self.listed(s, mine="Trips|Spain"), sorted(os.path.basename(p) for p in self.all))
        self.assertIn("Trips|Spain", s.get(f"/api/item/{ids[self.v2]}")["mine"])
        # remove on the mixed selection: only my tag goes; the video's other tags stay
        code, j = s.post("/api/tag", {"ids": [ids[p] for p in self.all], "tag": "Trips|Spain", "op": "remove"})
        self.assertEqual(code, 200, j)
        for p in self.all:
            self.assertNotIn("Trips|Spain", G.tags_of(p)[0], p)
        self.assertEqual(sorted(G.tags_of(self.v1)[0]),
                         ["Event|picnic", "Project|Personal", "Review|maybe-trash", "Trips|coast"])
        # a tool namespace is refused for a video as for a photo, and nothing is written
        code, _ = s.post("/api/tag", {"ids": [ids[self.v1], ids[self.p1]], "tag": "Owner|keep", "op": "add"})
        self.assertEqual(code, 400)
        self.assertNotIn("Owner|keep", G.tags_of(self.v1)[0])
        self.assert_originals()

    # ------------------------------------------------------------ Rotate
    def test_rotate_photo_video_and_mixed(self):
        G.write_tags(self.v2, [], extra=["-XMP-tiff:Orientation#=6"])  # already turned once
        s = self.start()
        ids = self.ids(s)
        tv = self.thumb(s, ids[self.v1], self.v1)
        tp = self.thumb(s, ids[self.p1], self.p1)
        # one video
        code, j = s.post("/api/rotate", {"ids": [ids[self.v1]], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(orientation_of(self.v1), 6)
        self.assertTrue(os.path.exists(tp))  # another item's thumbnail stays
        tv90 = self.thumb(s, ids[self.v1], self.v1, 90)  # its own cache key, made turned on the next request
        self.assertTrue(os.path.exists(tv))  # the upright one stays for a copy that isn't turned
        with Image.open(tv90) as im:
            self.assertEqual(im.size, (240, 320))
        self.assertEqual(s.get(f"/api/item/{ids[self.v1]}")["rot"], 90)  # the player is turned by the page
        # one photo
        code, j = s.post("/api/rotate", {"ids": [ids[self.p1]], "dir": "ccw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(orientation_of(self.p1), 8)
        self.assertEqual(s.get(f"/api/item/{ids[self.p1]}")["rot"], 270)
        # a mixed selection: each from its own orientation (1, 8, 6, 6)
        sel = [self.p2, self.p1, self.v1, self.v2]
        code, j = s.post("/api/rotate", {"ids": [ids[p] for p in sel], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "rotated 4 (sidecar XMP-tiff:Orientation only; originals untouched)")
        self.assertEqual([orientation_of(p) for p in sel], [6, 1, 3, 3])
        self.assertEqual([s.get(f"/api/item/{ids[p]}")["rot"] for p in sel], [90, 0, 180, 180])
        self.assertEqual(sorted(G.tags_of(self.v1)[0]),
                         ["Event|picnic", "Project|Personal", "Review|maybe-trash", "Trips|coast"])  # tags kept
        self.assert_originals()

    # ------------------------------------------------------------ events
    def test_events_on_photos_and_videos(self):
        s = self.start()
        ids = self.ids(s)
        code, j = s.post("/api/event/set", {"ids": [ids[self.v2]], "event": "beach"})
        self.assertEqual(code, 200, j)
        self.assertIn("Event|beach", G.tags_of(self.v2)[0])
        r = subprocess.run(["exiftool", "-s3", "-XMP-iptcExt:Event", self.v2 + ".xmp"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "beach")
        # mixed: the video leaves picnic, the photos join
        code, j = s.post("/api/event/set", {"ids": [ids[p] for p in (self.p1, self.p2, self.v1)], "event": "beach"})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "event 'beach' set on 3")
        for p in self.all:
            self.assertEqual([t for t in G.tags_of(p)[0] if t.startswith("Event|")], ["Event|beach"], p)
        self.assertIn("Trips|coast", G.tags_of(self.v1)[0])
        self.assertEqual(dict(map(tuple, s.get("/api/facets")["facets"]["event"])), {"beach": 4})
        # rename and merge reach both kinds
        code, j = s.post("/api/event/rename", {"from": "beach", "to": "coast-trip"})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "renamed 'beach' to 'coast-trip': 4 sidecars")
        code, j = s.post("/api/event/set", {"ids": [ids[self.v2], ids[self.p2]], "event": "hike"})
        self.assertEqual(code, 200, j)
        code, j = s.post("/api/event/merge", {"from": ["hike"], "into": "coast-trip"})
        self.assertEqual(code, 200, j)
        self.assertEqual(self.listed(s, event="coast-trip"), sorted(os.path.basename(p) for p in self.all))
        # clear on a mixed selection
        code, j = s.post("/api/event/set", {"ids": [ids[self.p1], ids[self.v1]], "event": ""})
        self.assertEqual(code, 200, j)
        for p in (self.p1, self.v1):
            self.assertFalse([t for t in G.tags_of(p)[0] if t.startswith("Event|")], p)
            self.assertEqual(s.get(f"/api/item/{ids[p]}")["event"], "")
        self.assert_originals()

    # ------------------------------------------------------------ Trash (the real intake.py trash)
    def test_trash_photo_video_and_mixed(self):
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({p: {"sha": self.orig[p], "size": os.path.getsize(p)} for p in self.all}, f)
        s = self.start()
        ids = self.ids(s)
        th = {p: self.thumb(s, ids[p], p) for p in self.all}
        size = {p: os.path.getsize(p) for p in self.all}

        def trash(paths):
            code, j = s.post("/api/trash", {"ids": [ids[p] for p in paths], "confirm": True})
            self.assertEqual(code, 200, j)
            self.assertEqual(j["result"], f"trashing {len(paths)}")
            j = self.wait_job(s)
            self.assertTrue(j["ok"], j["lines"])
            self.assertTrue(any(f"trashed {len(paths)} files" in ln for ln in j["lines"]), j["lines"])
            with open(os.path.join(self.state, "trashed.json")) as f:
                trashed = json.load(f)
            with open(os.path.join(self.state, "index.json")) as f:
                index = json.load(f)
            for p in paths:
                for gone in (p, p + ".xmp", th[p]):
                    self.assertFalse(os.path.lexists(gone), gone)
                self.assertEqual(trashed.get(self.orig[p]), size[p], p)  # never taken in again
                self.assertNotIn(p, index)
                self.assertEqual(s.req("GET", f"/m/{ids[p]}")[0].status, 404)
                self.assertEqual(s.req("GET", f"/t/{ids[p]}")[0].status, 404)
                self.assertEqual(s.req("GET", f"/api/item/{ids[p]}")[0].status, 404)

        code, _ = s.post("/api/trash", {"ids": [ids[self.v1]]})
        self.assertEqual(code, 400)  # no confirm, nothing runs
        self.assertTrue(os.path.exists(self.v1))
        trash([self.v1])  # a video (with a sidecar)
        self.assertEqual(self.listed(s), ["IMG_0042.MOV", "p1.jpg", "p2.png"])
        trash([self.p2])  # a photo (no sidecar)
        trash([self.p1, self.v2])  # mixed
        self.assertEqual(self.listed(s), [])
        c = s.get("/api/facets")["counts"]
        self.assertEqual((c.get("kind:photo", 0), c.get("kind:video", 0)), (0, 0))
        with open(os.path.join(self.state, "trashed.json")) as f:
            self.assertEqual(set(json.load(f)), set(self.orig.values()))

    # ------------------------------------------------------------ Erase (the real intake.py forget)
    def test_erase_mixed_selection(self):
        s = self.start()
        ids = self.ids(s)
        th = {p: self.thumb(s, ids[p], p) for p in (self.p1, self.v1, self.v2)}
        # every variant of the content goes: another turn and the older content-only key
        variants = [os.path.join(self.thumbs, self.orig[self.p1] + n) for n in ("-r180.jpg", ".jpg")]
        for v in variants:
            open(v, "wb").close()
        sel = [self.p1, self.v1, self.v2]
        code, _ = s.post("/api/erase", {"ids": [ids[p] for p in sel], "confirm": True})
        self.assertEqual(code, 400)  # ERASE typed, nothing less
        code, j = s.post("/api/erase", {"ids": [ids[p] for p in sel], "confirm": "ERASE"})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "erasing 3 everywhere")
        j = self.wait_job(s)
        self.assertTrue(j["ok"], j["lines"])
        self.assertIn("$ intake.py forget [3 files] --yes", j["lines"])
        self.assertEqual(j["lines"][-1], "done: 3 of 3 gone from the library")
        for p in sel:
            for gone in (p, p + ".xmp", th[p]):
                self.assertFalse(os.path.lexists(gone), gone)
        for v in variants:
            self.assertFalse(os.path.lexists(v), v)
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            log = f.read()
        for p in sel:
            self.assertIn(self.orig[p], log, p)
        self.assertEqual(self.listed(s), ["p2.png"])
        self.assert_originals([self.p2])
        jd = os.path.join(self.state, "jobs")
        for f in os.listdir(jd):  # no name of an erased photo or video left in the job's files
            with open(os.path.join(jd, f), "rb") as fh:
                data = fh.read()
            for p in sel:
                self.assertNotIn(os.path.basename(p).encode(), data, (f, p))

    # ------------------------------------------------------------ Trash suggestions: decide
    def test_decide_keeps_a_video_and_trashes_a_photo(self):
        s = self.start()
        ids = self.ids(s)
        code, j = s.post("/api/decide", {"keep": [ids[self.v1]], "trash": [ids[self.p1]], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "Kept 1. It won't be suggested as junk again. Trashing 1.")
        self.assert_kept(self.v1)  # written at once, before the job
        j = self.wait_job(s)
        self.assertTrue(j["ok"], j["lines"])
        self.assertFalse(os.path.exists(self.p1) or os.path.exists(self.p1 + ".xmp"))
        self.assertEqual(self.listed(s, view="suggest"), [])
        # and the other way round: keep a photo, trash a video
        G.write_tags(self.p2, ["Review|maybe-trash"])
        s2 = self.start()
        ids = self.ids(s2)
        code, j = s2.post("/api/decide", {"keep": [ids[self.p2]], "trash": [ids[self.v2]], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assert_kept(self.p2)
        self.assertTrue(self.wait_job(s2)["ok"])
        self.assertFalse(os.path.exists(self.v2))
        self.assert_originals([self.v1, self.p2])

    # ------------------------------------------------------------ Share (the real intake.py share-copy)
    def test_share_copy_mixed_selection(self):
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-GPSLatitude=37.5", "-GPSLatitudeRef=N",
                        "-GPSLongitude=77.4", "-GPSLongitudeRef=W", self.p1], check=True)
        self.orig[self.p1] = G.sha(self.p1)
        self.assertEqual(probe(self.v1)["format"]["tags"].get("title"), TITLE)  # the fixture has one to strip
        side = {p: G.sha(p + ".xmp") for p in (self.p1, self.v1)}
        s = self.start()
        ids = self.ids(s)
        sel = [self.p1, self.v1, self.v2]
        code, j = s.post("/api/share", {"ids": [ids[p] for p in sel]})
        self.assertEqual(code, 200, j)
        out = os.path.join(self.home, j["dest"][2:])
        self.assertTrue(j["result"].startswith("making 3 share-safe copies in ~/Pictures/share-safe/"), j)
        j = self.wait_job(s)
        self.assertTrue(j["ok"], j["lines"])
        self.assertEqual(sorted(os.listdir(out)), ["IMG_0042.MOV", "p1.jpg", "v1.mp4"])  # no sidecars
        for p in sel:
            self.assertTrue(any(ln.strip() == f"{out}/{os.path.basename(p)}  gps=none" for ln in j["lines"]), p)
        for p in self.videos:
            cp = probe(os.path.join(out, os.path.basename(p)))
            self.assertNotIn("title", cp["format"].get("tags", {}), p)
            self.assertEqual([st["codec_name"] for st in cp["streams"]], ["h264"], p)  # a stream copy
        r = subprocess.run(["exiftool", "-j", "-n", "-GPSLatitude", os.path.join(out, "p1.jpg")],
                           capture_output=True, text=True)
        self.assertNotIn("GPSLatitude", json.loads(r.stdout)[0])
        self.assert_originals()
        self.assertEqual({p: G.sha(p + ".xmp") for p in side}, side)
        self.assertEqual(self.listed(s), sorted(os.path.basename(p) for p in self.all))  # nothing left the grid

    # ------------------------------------------------------------ Open in the system player
    def test_open_photo_and_video(self):
        fdir = os.path.join(self.tmp, "fakebin")
        os.makedirs(fdir)
        opener = os.path.join(fdir, "xdg-open")
        with open(opener, "w") as f:
            f.write(G.FAKE_OPENER)
        os.chmod(opener, 0o700)
        log = os.path.join(self.tmp, "opened.json")
        self.env.update(PATH=fdir + os.pathsep + self.env["PATH"], FAKE_OPEN_LOG=log)
        s = self.start()
        ids = self.ids(s)

        def opened():
            if not os.path.exists(log):
                return None
            with open(log) as f:
                return json.load(f)
        for p in (self.v2, self.v1, self.p1):
            code, j = s.post("/api/open", {"ids": [ids[p]]})
            self.assertEqual(code, 200, j)
            self.assertEqual(j["result"], f"opened {os.path.basename(p)} in the system player")
            self.assertEqual(G.until(opened, "the fake opener"), [p])
            os.remove(log)
        code, _ = s.post("/api/open", {"ids": [ids[self.p1], ids[self.v1]]})
        self.assertEqual(code, 400)  # one item only, never a mixed selection
        self.assertFalse(os.path.exists(log))

    # ------------------------------------------------------------ a file deleted since the scan
    def test_edit_on_a_file_gone_since_the_scan_writes_nothing(self):
        """Keep, tags, Rotate and Set event refuse a selection holding a file deleted since the gallery scanned
        (a CLI trash, a job no page watched): nothing is written, so no orphan sidecar appears, and it leaves
        the grid."""
        s = self.start()
        ids = self.ids(s)
        os.remove(self.v2)  # a video with no sidecar
        os.remove(self.p1)  # a photo with one: its sidecar stays (as a CLI delete outside intake could leave it)
        x1 = G.sha(self.p1 + ".xmp")
        for path, body in (("/api/keep", {}), ("/api/tag", {"tag": "Trips|Spain", "op": "add"}),
                           ("/api/rotate", {"dir": "cw"}), ("/api/event/set", {"event": "beach"})):
            for gone in (self.v2, self.p1):
                sel = [ids[self.p2], ids[gone], ids[self.v1]]
                code, j = s.post(path, dict(body, ids=sel))
                self.assertEqual(code, 400, (path, gone, j))
                self.assertFalse(os.path.exists(self.v2 + ".xmp"), path)  # no orphan sidecar
                self.assertFalse(os.path.exists(self.p2 + ".xmp"), path)  # the whole request refused
                self.assertEqual(G.sha(self.p1 + ".xmp"), x1, path)
                self.assertNotIn("Trips|Spain", G.tags_of(self.v1)[0], path)
                self.assertEqual(orientation_of(self.v1), None, path)
        self.assertEqual(self.listed(s), ["p2.png", "v1.mp4"])
        self.assertEqual(s.req("GET", f"/api/item/{ids[self.v2]}")[0].status, 404)
        # the rest still works
        code, j = s.post("/api/keep", {"ids": [ids[self.p2], ids[self.v1]]})
        self.assertEqual(code, 200, j)
        self.assert_kept(self.p2)
        self.assert_kept(self.v1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
