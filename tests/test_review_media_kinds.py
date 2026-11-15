#!/usr/bin/env python3
"""The review page's decisions on photos, videos and a mix, run by the REAL intake.py.

  python3 -I -m unittest discover -s tests -p test_review_media_kinds.py

tests/test_review_ui.py drives the page against a fake intake.py that records its argv. Here the
page's run goes through the real one (no MEDIA_REVIEW_INTAKE), in a throwaway HOME, so each
decision is checked by what is left on disk afterwards:

- Keep in Quarantine (`release`): the file is at its library place with its sidecar, its sha256
  unchanged, index.json marks it released, and the run's own near-duplicate search never takes it
  back (a burst's kept shot is a near-copy of the erased ones);
- Erase (`erase --yes`, `verify-gone`): gone from quarantine, from the library and index.json, from
  a copy elsewhere in HOME, from the freedesktop and gallery thumbnail caches, and in erase-log.txt;
- Trash (`trash`, Review and Maybe trash only): gone with its sidecar and thumbnails, its sha256 in
  trashed.json. A trash decision on a quarantined item is refused (400) and leaves it untouched;
- Review Keep stays in review/ untouched; Delete is gone with its sidecar and thumbnail;
- Maybe trash Keep (`not-trash`): Owner|keep in the sidecar (needs exiftool; skipped without it).

Every operation is run on a photo, a video and both together, and burst stacks ("keep this one")
are run with a video taken in the same second, which must never join the stack.

Media are synthetic (Pillow noise grids, ffmpeg test patterns). Quarantined files are never
looked at: the tests check paths, existence, sizes and sha256 only.
"""
import hashlib, json, os, random, shutil, subprocess, sys, tempfile, unittest, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)
import test_review_ui as R  # noqa: E402  (the server wrapper and write_json only)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
import bursts  # noqa: E402
import intake  # noqa: E402  (its extension sets only)
import review_ui  # noqa: E402  (kind_of only)

from PIL import Image  # noqa: E402

BATCH = R.BATCH
HAVE_EXIFTOOL = bool(shutil.which("exiftool"))
KIND = {"photo": "image", "video": "video"}


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def grid_photo(path, seed, tweak=0, size=(288, 256)):
    """A JPEG of a 9x8 random grid, scaled up: its dHash is the grid's, so two seeds are ~32 bits apart
    and the same seed with another tweak is the same picture (a burst frame) with another sha256."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rnd = random.Random(seed)
    g = Image.new("L", (9, 8))
    g.putdata([rnd.randrange(256) for _ in range(72)])
    im = g.resize(size, Image.NEAREST).convert("RGB")
    if tweak:
        im.putpixel((size[0] - 1, size[1] - 1), (tweak, 255 - tweak, tweak))
    im.save(path, quality=95)
    return path


def clip(path, seed, seconds=1):
    """A short MPEG-4 test pattern; the seed goes in its metadata so every clip has its own sha256."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
                    "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-metadata", f"comment=clip-{seed}", path], check=True)
    return path


def make(kind, path, seed, tweak=0):
    return grid_photo(path, seed, tweak) if kind == "photo" else clip(path, seed)


def thumb_of(home, path):
    """The freedesktop thumbnail path of `path` (same URI rule as intake.thumb_forget)."""
    uri = "file://" + urllib.parse.quote(os.path.abspath(path), safe="/!$&'()*+,;=:@")
    return os.path.join(home, ".cache", "thumbnails", "normal", hashlib.md5(uri.encode()).hexdigest() + ".png")


def plant(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def jload(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def keep_one(items, kept):
    """What the page's "Keep this one, ..." button sets (keepOne/restOf in review_ui.PAGE): Keep on the
    shot, and on every other shot of its stack Erase in Quarantine (each is flagged), Trash elsewhere."""
    rest = "erase" if kept["tab"] == "quarantine" else "trash"
    return {str(i["id"]): ("keep" if i is kept else rest) for i in items
            if i["burst"] is not None and i["burst"] == kept["burst"]}


class RealRun(unittest.TestCase):
    """A review server whose submit runs the real intake.py in a throwaway HOME."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-kinds-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.home)
        self.env = {k: v for k, v in os.environ.items() if k not in ("MEDIA_REVIEW_INTAKE", "FAKE_LOG", "FAKE_GATE")}
        self.env.update(HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_NO_LOCALSEARCH="1",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.quar = os.path.join(self.state, "quarantine", BATCH)
        self.bd = os.path.join(self.state, "batches", BATCH)
        self.qman, self.plan, self.index = {}, [], {}
        self.servers = []
        self.seed = 0

    def tearDown(self):
        for s in self.servers:
            s.stop()

    # ------------------------------------------------------------ fixtures
    def next_seed(self):
        self.seed += 1
        return self.seed

    def ext(self, kind):
        return ".jpg" if kind == "photo" else ".mp4"

    def home_of(self, kind):
        return self.lib if kind == "photo" else self.vids

    def quarantined(self, kind, name, *, qbatch=BATCH, seed=None, tweak=0, sidecar=False, **extra):
        """A flagged file in quarantine/<qbatch> with a library place; returns (path, entry)."""
        rel = f"DCIM/Camera/{name}{self.ext(kind)}"
        q = os.path.join(self.state, "quarantine", qbatch)
        p = make(kind, os.path.join(q, rel), seed or self.next_seed(), tweak)
        e = dict(reasons=["nudity:exposed"], sha=sha(p), size=os.path.getsize(p),
                 dst=os.path.join(self.home_of(kind), "2026/2026-11", os.path.basename(rel)), **extra)
        if qbatch == BATCH:
            self.qman[rel] = e
        else:
            man = jload(os.path.join(q, "quarantine.json"), {})
            man[rel] = e
            R.write_json(os.path.join(q, "quarantine.json"), man)
        if sidecar:
            plant(p + ".xmp", f"<x:xmpmeta>{name}</x:xmpmeta>".encode())
        return p, e

    def in_review(self, kind, name, why="junk: tiny or empty", seed=None, tweak=0, **plan_extra):
        rel = f"DCIM/Camera/{name}{self.ext(kind)}"
        p = make(kind, os.path.join(self.bd, "review", rel), seed or self.next_seed(), tweak)
        self.plan.append(dict(rel=rel, action="review", why=why, sha=sha(p), size=os.path.getsize(p),
                              dst=p, lib_dst=os.path.join(self.home_of(kind), "2026/2026-11", os.path.basename(rel)),
                              **plan_extra))
        return p

    def copies_of(self, p, kind, name):
        """An erase target's other copies: one filed in the library (index.json, with a sidecar), one in
        ~/Downloads, and its freedesktop and gallery thumbnails. Returns every path that must go."""
        s = sha(p)
        lib_copy = os.path.join(self.home_of(kind), "2025/2025-01", f"old-{name}{self.ext(kind)}")
        os.makedirs(os.path.dirname(lib_copy), exist_ok=True)
        shutil.copyfile(p, lib_copy)
        plant(lib_copy + ".xmp", b"<x:xmpmeta/>")
        self.index[lib_copy] = dict(sha=s, size=os.path.getsize(p), dhash=None)
        dl = os.path.join(self.home, "Downloads", f"sent-{name}{self.ext(kind)}")
        os.makedirs(os.path.dirname(dl), exist_ok=True)
        shutil.copyfile(p, dl)
        return [lib_copy, lib_copy + ".xmp", dl, plant(thumb_of(self.home, p)),
                plant(os.path.join(self.home, ".cache/media-intake/thumbs", s + ".jpg"))]

    def write_state(self):
        R.write_json(os.path.join(self.quar, "quarantine.json"), self.qman)
        R.write_json(os.path.join(self.bd, "plan.json"), self.plan)
        R.write_json(os.path.join(self.state, "index.json"), self.index)

    # ------------------------------------------------------------ the server and its run
    def start(self, *extra, batch=BATCH):
        s = R.Server(self.env, *extra, batch=batch)
        self.servers.append(s)
        return s

    def items(self, s):
        r, b = s.req("GET", "/api/items")
        self.assertEqual(r.status, 200)
        return json.loads(b)["items"]

    def by_name(self, items):
        return {os.path.basename(i["rel"]): i for i in items}

    def submit(self, s, dec):
        r, b = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()}, body={"decisions": dec, "confirm": "ERASE"})
        self.assertEqual(r.status, 202, b)
        since, out = 0, []

        def step():
            nonlocal since
            r, b = s.req("GET", f"/api/progress?since={since}")
            j = json.loads(b)
            out.extend(j["lines"])
            since = j["next"]
            return j if j["done"] else None
        j = R.until(step, "the run to finish", timeout=120)
        return j, "\n".join(out)

    def assert_served(self, s, it, path):
        """The page serves the file as its kind, whole and by Range, and writes no thumbnail for it."""
        r, b = s.req("GET", f"/m/{it['id']}")
        self.assertEqual((r.status, len(b)), (200, os.path.getsize(path)))
        want = "image/jpeg" if it["kind"] == "image" else "video/mp4"
        self.assertEqual(r.getheader("Content-Type"), want)
        r, b = s.req("GET", f"/m/{it['id']}", headers={"Range": "bytes=4-11"})
        self.assertEqual((r.status, len(b), r.getheader("Content-Range")),
                         (206, 8, f"bytes 4-11/{os.path.getsize(path)}"))

    def cache_files(self):
        c = os.path.join(self.home, ".cache")
        return sorted(os.path.join(r, f) for r, _, fs in os.walk(c) for f in fs)

    # ------------------------------------------------------------ outcomes
    def assert_released(self, before, entry, sidecar_text=None):
        dst = entry["dst"]
        self.assertTrue(os.path.isfile(dst), dst)
        self.assertEqual(sha(dst), before, "a kept original is moved, never rewritten")
        self.assertTrue(jload(os.path.join(self.state, "index.json"))[dst].get("released"))
        if sidecar_text is not None:
            with open(dst + ".xmp", "rb") as f:
                self.assertEqual(f.read(), sidecar_text, "its sidecar goes with it")

    def assert_erased(self, s_, paths):
        for p in paths:
            self.assertFalse(os.path.lexists(p), p)
        idx = jload(os.path.join(self.state, "index.json"), {})
        self.assertFalse([k for k, v in idx.items() if v.get("sha") == s_], "no index entry keeps its content")
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            self.assertIn(f"sha256:{s_}", f.read())

    def assert_trashed(self, s_, paths):
        for p in paths:
            self.assertFalse(os.path.lexists(p), p)
        self.assertIn(s_, jload(os.path.join(self.state, "trashed.json"), {}))


class QuarantineAndReviewByKind(RealRun):
    """Keep / Erase in Quarantine (Trash refused) and Keep / Delete / Trash in Review, per media kind."""

    def run_kinds(self, kinds):
        q, r = {}, {}
        for k in kinds:
            keep, ke = self.quarantined(k, f"keep-{k}", sidecar=True)
            erase, ee = self.quarantined(k, f"erase-{k}")
            # Decided trash first: refused, untouched; then erased.
            trash, te = self.quarantined(k, f"trash-{k}", sidecar=True)
            q[k] = dict(keep=(keep, ke, sha(keep)), erase=(erase, ee, self.copies_of(erase, k, f"erase-{k}")),
                        trash=(trash, te, plant(os.path.join(self.home, ".cache/media-intake/thumbs",
                                                             te["sha"] + ".jpg"))))
            rk, rd, rt = (self.in_review(k, f"r{d}-{k}") for d in ("keep", "delete", "trash"))
            plant(rd + ".xmp", b"<x:xmpmeta/>")
            r[k] = dict(keep=(rk, sha(rk)), delete=rd, trash=(rt, sha(rt)))
        self.write_state()
        s = self.start()
        items = self.by_name(self.items(s))
        self.assertEqual(len(items), 6 * len(kinds))
        cache = self.cache_files()
        for k in kinds:
            for d in ("keep", "erase", "trash"):
                it = items[f"{d}-{k}{self.ext(k)}"]
                self.assertEqual((it["tab"], it["kind"], it["burst"], it["releasable"]),
                                 ("quarantine", KIND[k], None, True))
                self.assert_served(s, it, q[k][d][0])
            for d in ("keep", "delete", "trash"):
                it = items[f"r{d}-{k}{self.ext(k)}"]
                self.assertEqual((it["tab"], it["kind"]), ("review", KIND[k]))
                self.assert_served(s, it, r[k][d][0] if d != "delete" else r[k][d])
        self.assertEqual(self.cache_files(), cache, "showing media writes no thumbnail")
        dec = {}
        for k in kinds:
            for d in ("keep", "erase", "trash"):
                dec[str(items[f"{d}-{k}{self.ext(k)}"]["id"])] = d
            for d in ("keep", "delete", "trash"):
                dec[str(items[f"r{d}-{k}{self.ext(k)}"]["id"])] = d
        # Quarantine is Erase-only: the trash decisions are refused before anything runs or is written.
        resp, b = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()}, body={"decisions": dec, "confirm": "ERASE"})
        self.assertEqual(resp.status, 400, b)
        self.assertFalse(os.path.exists(os.path.join(self.bd, "review-decisions.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "trashed.json")))
        for k in kinds:
            trash, te, gthumb = q[k]["trash"]
            self.assertEqual(sha(trash), te["sha"])
            self.assertTrue(os.path.exists(trash + ".xmp") and os.path.exists(gthumb))
            dec[str(items[f"trash-{k}{self.ext(k)}"]["id"])] = "erase"
        j, out = self.submit(s, dec)
        self.assertTrue(j["ok"], out)
        self.assertIn("verify-gone: no copy of", out)
        for k in kinds:
            keep, ke, before = q[k]["keep"]
            self.assert_released(before, ke, f"<x:xmpmeta>keep-{k}</x:xmpmeta>".encode())
            erase, ee, copies = q[k]["erase"]
            self.assert_erased(ee["sha"], [erase, *copies])
            trash, te, gthumb = q[k]["trash"]
            self.assert_erased(te["sha"], [trash, trash + ".xmp", gthumb])
            self.assertNotIn(te["sha"], jload(os.path.join(self.state, "trashed.json"), {}), "erased, not trashed")
            rk, before = r[k]["keep"]
            self.assertEqual(sha(rk), before, "a kept review file stays in review/, untouched")
            self.assertFalse(os.path.lexists(r[k]["delete"]))
            self.assertFalse(os.path.lexists(r[k]["delete"] + ".xmp"))
            rt, rs = r[k]["trash"]
            self.assert_trashed(rs, [rt])
        self.assertFalse(os.path.exists(self.quar), "the erase removes the batch's quarantine")
        self.assertFalse(os.path.exists(os.path.join(self.state, "quarantine", f"near-{BATCH}")),
                         "nothing kept was taken for a near-copy")
        rec = jload(os.path.join(self.bd, "review-decisions.json"))
        self.assertEqual(sorted(rec["quarantine"][BATCH].values()), sorted(["keep", "erase", "erase"] * len(kinds)))
        self.assertEqual(sorted(rec["review"].values()), sorted(["keep", "delete", "trash"] * len(kinds)))
        self.assertEqual(s.p.wait(timeout=60), 0)

    def test_photo(self):
        self.run_kinds(["photo"])

    def test_video(self):
        self.run_kinds(["video"])

    def test_mixed(self):
        self.run_kinds(["photo", "video"])


class OneWayRuns(RealRun):
    """Runs where one decision covers a whole tab: these take other code paths (purge-review, no erase)."""

    def test_all_review_deleted_purges_photos_and_videos(self):
        ps = [self.in_review(k, f"r-{k}") for k in ("photo", "video")]
        plant(ps[1] + ".xmp", b"<x:xmpmeta/>")
        self.write_state()
        s = self.start()
        items = self.items(s)
        self.assertEqual(sorted(i["kind"] for i in items), ["image", "video"])
        j, out = self.submit(s, {str(i["id"]): "delete" for i in items})
        self.assertTrue(j["ok"], out)
        self.assertIn("purge-review", out)
        self.assertFalse(os.path.exists(os.path.join(self.bd, "review")))

    def test_keep_and_trash_without_erase_leave_other_thumbnails(self):
        """No erase in the run (so the thumbnail cache is not cleared): trash and delete forget exactly their
        own thumbnails, and a kept review file's thumbnail stays. (Trash is a Review decision: Quarantine is
        Erase-only.)"""
        keep_p, ke = self.quarantined("photo", "keep-photo")
        keep_v, kv = self.quarantined("video", "keep-video", sidecar=True)
        tr_p = self.in_review("photo", "trash-photo")
        tr_v = self.in_review("video", "trash-video")
        rk = self.in_review("video", "rkeep-video")
        rd = self.in_review("photo", "rdelete-photo")
        self.in_review("video", "rdelete-video")
        th = {p: plant(thumb_of(self.home, p)) for p in (keep_p, keep_v, tr_p, tr_v, rk, rd)}
        befores = {p: sha(p) for p in (keep_p, keep_v, rk, tr_p, tr_v)}
        self.write_state()
        s = self.start()
        items = self.by_name(self.items(s))
        dec = {str(i["id"]): d for n, d in (("keep-photo.jpg", "keep"), ("keep-video.mp4", "keep"),
                                            ("trash-photo.jpg", "trash"), ("trash-video.mp4", "trash"),
                                            ("rkeep-video.mp4", "keep"), ("rdelete-photo.jpg", "delete"),
                                            ("rdelete-video.mp4", "delete")) for i in [items[n]]}
        j, out = self.submit(s, dec)
        self.assertTrue(j["ok"], out)
        self.assertNotIn("$ intake.py erase", out)
        self.assert_released(befores[keep_p], ke)
        self.assert_released(befores[keep_v], kv, b"<x:xmpmeta>keep-video</x:xmpmeta>")
        self.assert_trashed(befores[tr_p], [tr_p, th[tr_p]])
        self.assert_trashed(befores[tr_v], [tr_v, th[tr_v]])
        for p in (keep_p, keep_v, rd):
            self.assertFalse(os.path.exists(th[p]), p)
        self.assertTrue(os.path.exists(th[rk]), "a kept review file's thumbnail is left alone")
        self.assertEqual(sha(rk), befores[rk])
        self.assertEqual(jload(os.path.join(self.quar, "quarantine.json")), {}, "every entry was decided")


class NearAndMessagingCopies(RealRun):
    """Near-copies of an erased photo (near-<batch>) and messaging copies (xcheck-<batch>), photo and video."""

    def test_near_level_keep_photo_erase_video(self):
        """Keep on a near-copy puts it back where it was found; Erase removes it everywhere. A video in a
        near- level (intake's own search only moves images, but a level is just a quarantine batch) gets
        the same card and the same erase."""
        near = f"near-{BATCH}"
        dl = os.path.join(self.home, "Downloads")
        pk, pke = self.quarantined("photo", "n-keep", qbatch=near, near_of_sha="0" * 64, erased_on="2026-11-12")
        pke["dst"] = os.path.join(dl, "n-keep.jpg")
        ve, vee = self.quarantined("video", "n-erase", qbatch=near, near_of_sha="0" * 64, erased_on="2026-11-12")
        vee["dst"] = os.path.join(dl, "n-erase.mp4")
        R.write_json(os.path.join(self.state, "quarantine", near, "quarantine.json"),
                     {"DCIM/Camera/n-keep.jpg": pke, "DCIM/Camera/n-erase.mp4": vee})
        copies = self.copies_of(ve, "video", "n-erase")
        before = sha(pk)
        self.write_state()
        os.remove(os.path.join(self.quar, "quarantine.json"))  # only the near level is left to review
        os.rmdir(self.quar)
        s = self.start()
        items = self.by_name(self.items(s))
        self.assertEqual(sorted(items), ["n-erase.mp4", "n-keep.jpg"])
        for n, k in (("n-keep.jpg", "image"), ("n-erase.mp4", "video")):
            it = items[n]
            self.assertEqual((it["batch"], it["kind"], it["burst"]), (near, k, None))
            self.assertEqual(it["orig"], {"erased": True, "date": "2026-11-12"})
            self.assertEqual(it["meta"]["res"], "288x256" if k == "image" else "160x120")
        j, out = self.submit(s, {str(items["n-keep.jpg"]["id"]): "keep", str(items["n-erase.mp4"]["id"]): "erase"})
        self.assertTrue(j["ok"], out)
        self.assert_released(before, pke)
        self.assert_erased(vee["sha"], [ve, *copies])

    def test_xcheck_photo_and_video_beside_their_library_originals(self):
        xb = f"xcheck-{BATCH}"
        origs = {k: make(k, os.path.join(self.home_of(k), "2026/2026-10", f"orig-{k}{self.ext(k)}"),
                         self.next_seed()) for k in ("photo", "video")}
        obefore = {k: sha(p) for k, p in origs.items()}
        xp, xpe = self.quarantined("photo", "x-keep", qbatch=xb, of=origs["photo"])
        xv, xve = self.quarantined("video", "x-erase", qbatch=xb, of=origs["video"])
        self.write_state()
        os.remove(os.path.join(self.quar, "quarantine.json"))
        os.rmdir(self.quar)
        s = self.start()
        items = self.by_name(self.items(s))
        p, v = items["x-keep.jpg"], items["x-erase.mp4"]
        self.assertEqual((p["orig"]["kind"], v["orig"]["kind"]), ("image", "video"))
        self.assertEqual(p["orig"]["label"], "Messaging copy of this photo")
        self.assertEqual(v["orig"]["label"], "Messaging copy of this video")
        r, b = s.req("GET", f"/o/{v['orig']['id']}", headers={"Range": "bytes=0-15"})
        self.assertEqual((r.status, r.getheader("Content-Type"), len(b)), (206, "video/mp4", 16))
        before = sha(xp)
        j, out = self.submit(s, {str(p["id"]): "keep", str(v["id"]): "erase"})
        self.assertTrue(j["ok"], out)
        self.assert_released(before, xpe)
        self.assert_erased(xve["sha"], [xv])
        for k, o in origs.items():
            self.assertEqual(sha(o), obefore[k], "the library original a messaging copy points at stays")


class MessagingCopyLabel(unittest.TestCase):
    """An xcheck copy's label names the app its library place is filed under (Messaging/<App>/)."""

    def test_app_from_the_messaging_folder(self):
        app = review_ui.messaging_app
        for rel, want in (("Pictures/library/Messaging/Telegram/2026/photo_1@14-11-2026_10-20-30.jpg", "Telegram"),
                          ("library/Messaging/Signal/2026/signal-2026-11-14-10-20-30-123.mp4", "Signal"),
                          ("Messaging/WhatsApp/2025/w1.jpg", "WhatsApp"),
                          ("DCIM/Camera/x.jpg", "Messaging"), ("Telegram Images/x.jpg", "Messaging"),
                          ("Messaging/../x.jpg", "Messaging"), ("Messaging/x.jpg", "Messaging")):
            self.assertEqual(app({"rel": rel}), want, rel)
        self.assertEqual(app({"rel": None}), "Messaging")


class BurstsWithVideos(RealRun):
    """"Keep this one" on a burst stack, with a video from the same camera in the same second."""

    def at(self, secs, device="cam-a", dhash=0):
        """plan.json fields bursts.py groups by: one camera, this many seconds after 07:15:00."""
        return dict(date=f"2026-11-08T07:15:{secs:06.3f}", device=device, dhash=dhash)

    def test_quarantine_burst_keep_one_erases_the_rest_and_spares_the_video(self):
        """Burst frames are near-copies of each other: the kept, released frame must survive the erase's
        near-duplicate search over the library."""
        shots = []
        for n in range(3):
            p, e = self.quarantined("photo", f"IMG_000{n}", seed=4242, tweak=n + 1)
            shots.append((p, e))
        vp, ve = self.quarantined("video", "VID_0001")
        for n, (p, e) in enumerate(shots):
            self.plan.append(dict(rel=os.path.relpath(p, self.quar), action="file", why="", sha=e["sha"],
                                  size=e["size"], dst=e["dst"], **self.at(0.4 * n)))
        self.plan.append(dict(rel="DCIM/Camera/VID_0001.mp4", action="file", why="", sha=ve["sha"], size=ve["size"],
                              dst=ve["dst"], **self.at(0.5, dhash=None)))
        self.write_state()
        s = self.start()
        items = self.by_name(self.items(s))
        g = items["IMG_0000.jpg"]["burst"]
        self.assertIsInstance(g, int)
        self.assertEqual([items[f"IMG_000{n}.jpg"]["burst"] for n in range(3)], [g, g, g])
        self.assertIsNone(items["VID_0001.mp4"]["burst"], "a video is never a burst shot")
        dec = keep_one(list(items.values()), items["IMG_0001.jpg"])
        self.assertEqual(sorted(dec.values()), ["erase", "erase", "keep"])
        dec[str(items["VID_0001.mp4"]["id"])] = "keep"
        kept_before, vbefore = sha(shots[1][0]), sha(vp)
        copies = [c for n in (0, 2) for c in self.copies_of(shots[n][0], "photo", f"burst-{n}")]
        # copies_of wrote index entries: put them on disk before the run
        R.write_json(os.path.join(self.state, "index.json"), self.index)
        j, out = self.submit(s, dec)
        self.assertTrue(j["ok"], out)
        self.assert_released(kept_before, shots[1][1])
        self.assert_released(vbefore, ve)
        for n in (0, 2):
            self.assert_erased(shots[n][1]["sha"], [shots[n][0]])
        for c in copies:
            self.assertFalse(os.path.lexists(c), c)
        self.assertFalse(os.path.exists(os.path.join(self.state, "quarantine", f"near-{BATCH}")),
                         "the kept frame is a near-copy of the erased ones and stays in the library")

    def test_review_burst_keep_one_trashes_the_rest_video_deleted(self):
        ps = [self.in_review("photo", f"IMG_100{n}", seed=777, tweak=n + 1, **self.at(0.3 * n))
              for n in range(3)]
        v = self.in_review("video", "VID_1001", **self.at(0.2, dhash=None))
        self.write_state()
        s = self.start()
        items = self.by_name(self.items(s))
        g = items["IMG_1000.jpg"]["burst"]
        self.assertEqual([items[f"IMG_100{n}.jpg"]["burst"] for n in range(3)], [g, g, g])
        self.assertIsNone(items["VID_1001.mp4"]["burst"])
        dec = keep_one(list(items.values()), items["IMG_1002.jpg"])
        self.assertEqual(sorted(dec.values()), ["keep", "trash", "trash"])
        dec[str(items["VID_1001.mp4"]["id"])] = "delete"
        befores = [sha(p) for p in ps]
        j, out = self.submit(s, dec)
        self.assertTrue(j["ok"], out)
        self.assertEqual(sha(ps[2]), befores[2])
        for n in (0, 1):
            self.assert_trashed(befores[n], [ps[n]])
        self.assertFalse(os.path.lexists(v))

    @unittest.skipUnless(HAVE_EXIFTOOL, "exiftool missing (not-trash writes the sidecar with it)")
    def test_maybe_trash_burst_keep_one_and_videos(self):
        """Maybe trash: a library burst (keep one, trash the rest) and two library videos of the same moment
        (one kept, one trashed). Keep is not-trash: Owner|keep in the sidecar."""
        names = {"photo": [f"IMG_200{n}" for n in range(3)], "video": ["VID_2001", "VID_2002"]}
        paths, sums = {}, {}
        for k, ns in names.items():
            for n, name in enumerate(ns):
                p = os.path.join(self.home_of(k), "2026/2026-11", name + self.ext(k))
                make(k, p, 555 if k == "photo" else self.next_seed(), tweak=n + 1)
                plant(p + ".xmp", b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
                      b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf='
                      b'"http://www.w3.org/1999/02/22-rdf-syntax-ns#"></rdf:RDF></x:xmpmeta>\n<?xpacket end="w"?>')
                paths[name], sums[name] = p, sha(p)
                self.index[p] = dict(sha=sums[name], size=os.path.getsize(p), dhash=None)
                self.plan.append(dict(rel=f"DCIM/Camera/{name}{self.ext(k)}", action="file", why="", sha=sums[name],
                                      size=os.path.getsize(p), dst=p, lib_dst=p,
                                      **self.at(0.3 * n, dhash=0 if k == "photo" else None)))
        self.write_state()
        R.write_json(os.path.join(self.state, "maybe-trash.json"), {"batch": BATCH, "items": [
            {"path": p, "labels": ["Review|maybe-trash"]} for p in paths.values()]})
        s = self.start("--maybe-trash", batch=None)
        items = self.by_name(self.items(s))
        self.assertEqual({n: i["kind"] for n, i in items.items()},
                         {"IMG_2000.jpg": "image", "IMG_2001.jpg": "image", "IMG_2002.jpg": "image",
                          "VID_2001.mp4": "video", "VID_2002.mp4": "video"})
        g = items["IMG_2000.jpg"]["burst"]
        self.assertIsInstance(g, int)
        self.assertEqual({items[f"IMG_200{n}.jpg"]["burst"] for n in range(3)}, {g})
        self.assertEqual([items[n]["burst"] for n in ("VID_2001.mp4", "VID_2002.mp4")], [None, None])
        dec = keep_one(list(items.values()), items["IMG_2000.jpg"])
        dec[str(items["VID_2001.mp4"]["id"])] = "keep"
        dec[str(items["VID_2002.mp4"]["id"])] = "trash"
        j, out = self.submit(s, dec)
        self.assertTrue(j["ok"], out)
        for n in ("IMG_2000", "VID_2001"):
            p = paths[n]
            self.assertEqual(sha(p), sums[n], "Keep writes the sidecar, never the original")
            with open(p + ".xmp") as f:
                x = f.read()
            self.assertIn("Owner|keep", x)
            self.assertIn("Owner|not-trash", x)
        for n in ("IMG_2001", "IMG_2002", "VID_2002"):
            self.assert_trashed(sums[n], [paths[n], paths[n] + ".xmp"])


class Kinds(unittest.TestCase):
    """What a card shows for each extension intake takes in, and that bursts never take a video."""

    def test_every_intake_extension_has_a_card_kind(self):
        for ext in intake.VID:
            for e in (ext, ext.upper()):
                self.assertEqual(review_ui.kind_of(f"/x/a{e}")[0], "video", e)
        for ext in intake.IMG:
            for e in (ext, ext.upper()):
                k, mime = review_ui.kind_of(f"/x/a{e}")
                # HEIC/HEIF and raw files get "no preview"; the rest are shown as images.
                want = "none" if ext in (".heic", ".heif", ".dng", ".gpr") else "image"
                self.assertEqual(k, want, e)
                if k == "image":
                    self.assertTrue(mime.startswith("image/"), (e, mime))

    def test_every_intake_video_extension_is_never_a_burst_shot(self):
        for ext in intake.VID:
            for e in (ext, ext.upper()):
                self.assertFalse(bursts.is_image({"path": f"/x/VID_1{e}"}), e)
        for ext in intake.IMG:
            self.assertTrue(bursts.is_image({"path": f"/x/IMG_1{ext.upper()}"}), ext)

    def test_a_video_neither_joins_nor_splits_a_burst(self):
        t = "2026-11-08T07:15:0"

        def r(name, s, dev="cam-a"):
            return dict(path=f"/x/DCIM/Camera/{name}", datetime=f"{t}{s}", device=dev, dhash=0)
        # Two frames 2 s apart with a clip between them: still one burst of the two frames.
        g = bursts.groups([r("a.jpg", 0), r("v.mp4", 1), r("b.jpg", 2)])
        self.assertEqual([[os.path.basename(x["path"]) for x in grp] for grp in g], [["a.jpg", "b.jpg"]])
        # A clip next to one frame never makes a pair; a video carrying a burst marker in its name neither.
        self.assertEqual(bursts.groups([r("a.jpg", 0), r("v.mp4", 0)]), [])
        self.assertEqual(bursts.groups([r("IMG_1_BURST001.jpg", 0), r("IMG_1_BURST002.mp4", 9)]), [])
        # An iPhone Live Photo (HEIC + MOV of the same instant) is one still, no stack.
        self.assertEqual(bursts.groups([r("IMG_0001.HEIC", 0), r("IMG_0001.MOV", 0)]), [])


if __name__ == "__main__":
    unittest.main()
