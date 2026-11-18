#!/usr/bin/env python3
"""The library-side commands on photos AND videos (mp4 and .mov), one at a time and mixed.

  python3 -I -m unittest discover -s tests -p test_library_ops_media_kinds.py

trash (with and without a batch, --phone, --from-digikam), forget, not-trash,
suggest-trash, quarantine, release, purge-review, mark-used, tag, tag-files,
share-copy, find, migrate-tags, and Keep (Owner|keep) semantics. Each command gets
a photo under ~/Pictures/library, a video under ~/Videos/personal and a .mov in a
project folder where that makes sense, plus mixed argument lists. Videos are
ffmpeg test patterns, photos Pillow drawings; everything runs through the real
intake.py in a throwaway HOME. A video's sidecar is <clip>.mp4.xmp, and its bytes
(sha256) must never change: tags go to the sidecar only.

No test opens a quarantined or flagged file's content; quarantine checks are
paths, counts and checksums.
"""
import argparse, builtins, hashlib, io, json, os, shutil, stat, subprocess, sys, tempfile, unittest, urllib.parse
import unittest.mock as mock
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
from test_intake import INTAKE, PY, pattern, save_jpg  # noqa: E402

EMPTY = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
         "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n</rdf:RDF>\n</x:xmpmeta>\n"
         "<?xpacket end='w'?>\n")
TOOLS = all(shutil.which(t) for t in ("exiftool", "ffmpeg", "ffprobe"))


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def jload(p):
    with open(p) as f:
        return json.load(f)


def clip(path, created="2026-11-08T15:00:00Z", seed=0, gps=False, extra=()):
    """A 1 s ffmpeg test pattern (mpeg4 + aac) in path's container (.mp4, .mov); seed makes the bytes differ.
    gps: a QuickTime Keys:GPSCoordinates atom, as phones write."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=96x64:rate=10:duration=1",
                    "-f", "lavfi", "-i", f"sine=frequency={300 + 37 * seed}:duration=1", "-shortest",
                    "-c:v", "mpeg4", "-c:a", "aac", "-metadata", f"creation_time={created}", *extra, path], check=True)
    if gps:
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-Keys:GPSCoordinates=37.5407, -77.436, 50", path],
                       check=True)
    return path


def photo(path, seed, dt=None, gps=False):
    save_jpg(pattern(seed, (320, 240)), path, dt, gps)
    return path


def fd_thumb(home, path):
    """The freedesktop thumbnail path GNOME would use for path (made empty here)."""
    uri = "file://" + urllib.parse.quote(os.path.abspath(path), safe="/!$&'()*+,;=:@")
    t = os.path.join(home, ".cache/thumbnails/normal", hashlib.md5(uri.encode()).hexdigest() + ".png")
    os.makedirs(os.path.dirname(t), exist_ok=True)
    open(t, "wb").close()
    return t


def streams(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-of", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


@unittest.skipUnless(TOOLS, "exiftool, ffmpeg or ffprobe missing")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-kinds-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.footage = os.path.join(self.home, "Videos/demo-footage")
        self.shoot = os.path.join(self.footage, "originals", "2026-11-08-city-marathon", "phone")
        self.phone = os.path.join(self.tmp, "phone", "Internal storage")
        self.gthumbs = os.path.join(self.home, ".cache/media-intake/thumbs")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.footage),
                        TZ="America/New_York", MEDIA_INTAKE_NO_LOCALSEARCH="1",
                        MEDIA_INTAKE_WIPE_ANY_DEST="1", MEDIA_INTAKE_WIPE_MAX_MB="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0, stdin=None):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r

    def out(self, *args, **kw):
        return self.intake(*args, **kw).stdout

    # ---- the three kinds, in their usual library places
    def trio(self, seed=0):
        """(photo in ~/Pictures/library, mp4 in ~/Videos/personal, .mov in the demo project's shoot folder)."""
        return (photo(os.path.join(self.lib, "2026/2026-11", f"p{seed}.jpg"), 40 + seed, "2026:11:08 07:15:02"),
                clip(os.path.join(self.vids, "2026/2026-11", f"v{seed}.mp4"), seed=2 * seed),
                clip(os.path.join(self.shoot, f"m{seed}.mov"), seed=2 * seed + 1))

    def xmp(self, media, *hier, extra=()):
        with open(media + ".xmp", "w") as f:
            f.write(EMPTY)
        args = [a for h in hier for a in (f"-XMP-lr:HierarchicalSubject+={h}", f"-XMP-dc:Subject+={h.split('|')[-1]}")]
        if args or extra:
            subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", *args, *extra, media + ".xmp"], check=True)
        return media + ".xmp"

    def tags(self, media):
        r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", media + ".xmp"],
                           capture_output=True, text=True, check=True)
        v = json.loads(r.stdout)[0].get("HierarchicalSubject", [])
        return sorted(v if isinstance(v, list) else [v])

    def sidecar_field(self, media, tag):
        r = subprocess.run(["exiftool", "-j", "-q", "-n", f"-{tag}", media + ".xmp"], capture_output=True, text=True,
                           check=True)
        return json.loads(r.stdout)[0].get(tag.split(":")[-1])

    def gthumb(self, media):
        """A gallery thumbnail of media's content as gallery.py caches it: keyed by sha256 and the turn baked in
        (a turned one here, so cleanup by sha reaches every rotation, not only the upright file)."""
        os.makedirs(self.gthumbs, exist_ok=True)
        t = os.path.join(self.gthumbs, sha(media) + "-r90.jpg")
        open(t, "wb").close()
        return t

    def index(self, *paths, extra=None):
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        idx = {p: dict(sha=sha(p), size=os.path.getsize(p), dhash=None) for p in paths}
        idx.update(extra or {})
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump(idx, f)

    def suggested(self, *args):
        self.out("suggest-trash", "--no-serve", *args)
        return {x["path"]: x["labels"] for x in jload(os.path.join(self.state, "maybe-trash.json"))["items"]}

    def no_sidecar_named_without_ext(self, media):
        """digiKam naming: clip.mp4.xmp, never clip.xmp."""
        self.assertFalse(os.path.exists(os.path.splitext(media)[0] + ".xmp"), media)

    # ---- a phone batch with a photo, an mp4 and a .mov (videos go to ~/Videos/personal)
    def phone_batch(self, batch, flags=()):
        P = lambda rel: os.path.join(self.phone, rel)  # noqa: E731
        self.src = {"jpg": photo(P("DCIM/Camera/20261020_120000.jpg"), 61, "2026:10:20 12:00:00"),
                    "jpg2": photo(P("DCIM/Camera/20261020_130000.jpg"), 62, "2026:10:20 13:00:00"),
                    "mp4": clip(P("DCIM/Camera/20261021_090000.mp4"), "2026-10-21T13:00:00Z", seed=3),
                    "mov": clip(P("DCIM/Camera/20261022_100000.mov"), "2026-10-22T14:00:00Z", seed=4)}
        self.out("stage", self.phone, "--kind", "phone", "--batch", batch)
        self.out("plan", batch)
        bd = os.path.join(self.state, "batches", batch)
        if flags:
            with open(os.path.join(bd, "scan.json"), "w") as f:
                json.dump({os.path.relpath(self.src[k], self.phone): ["nudity"] for k in flags}, f)
            self.out("apply", batch, "--allow-missing-detectors")
        else:
            self.out("apply", batch, "--no-scan")
        self.filed = {"jpg": os.path.join(self.lib, "2026/2026-10/20261020_120000.jpg"),
                      "jpg2": os.path.join(self.lib, "2026/2026-10/20261020_130000.jpg"),
                      "mp4": os.path.join(self.vids, "2026/2026-10/20261021_090000.mp4"),
                      "mov": os.path.join(self.vids, "2026/2026-10/20261022_100000.mov")}
        return bd


class Trash(Base):
    def test_trash_photo_and_videos_mixed_without_batch(self):
        p, v, m = self.trio()
        side = [self.xmp(x, "Review|maybe-trash") for x in (p, v, m)]
        fdt = [fd_thumb(self.home, x) for x in (p, v, m)]
        gt = [self.gthumb(x) for x in (p, v, m)]
        sums = {sha(x): os.path.getsize(x) for x in (p, v, m)}
        keeper = clip(os.path.join(self.vids, "2026/2026-11/keep.mp4"), seed=9)
        self.index(p, v, m, keeper)
        out = self.out("trash", p, v, m, "--yes")
        self.assertIn("trashed 3 files; sidecars 3; thumbnails 6; phone 0", out)
        for x in (p, v, m, *side, *fdt, *gt):
            self.assertFalse(os.path.lexists(x), x)
        self.assertEqual(jload(os.path.join(self.state, "trashed.json")), sums)
        self.assertEqual(list(jload(os.path.join(self.state, "index.json"))), [keeper])
        self.assertTrue(os.path.isfile(keeper))
        self.assertFalse(os.path.exists(os.path.join(self.state, "erase-log.txt")), "trash is not erase")

    def test_trash_photo_and_videos_with_batch_and_phone(self):
        batch = "2026-11-13-kinds"
        self.phone_batch(batch)
        f = self.filed
        for k in ("jpg", "mp4", "mov"):
            self.assertTrue(os.path.isfile(f[k]), k)
            self.xmp(f[k], "Label|food")
        # Without --yes: a dry run; every file, sidecar and phone original stays.
        out = self.out("trash", batch, f["mp4"], f["mov"], f["jpg"], "--phone", self.phone)
        self.assertIn("would trash 3 files; phone originals 3", out)
        self.assertIn("dry run: nothing deleted", out)
        for k in ("jpg", "mp4", "mov"):
            self.assertTrue(os.path.isfile(f[k]) and os.path.isfile(f[k] + ".xmp") and os.path.isfile(self.src[k]), k)
        self.assertFalse(os.path.exists(os.path.join(self.state, "trashed.json")))
        out = self.out("trash", batch, f["mp4"], f["mov"], f["jpg"], "--phone", self.phone, "--yes")
        self.assertIn("trashed 3 files; sidecars 3;", out)
        self.assertIn("phone 3", out)
        for k in ("jpg", "mp4", "mov"):
            self.assertFalse(os.path.exists(f[k]) or os.path.exists(f[k] + ".xmp") or os.path.exists(self.src[k]), k)
        self.assertTrue(os.path.isfile(f["jpg2"]) and os.path.isfile(self.src["jpg2"]), "only the named files")
        self.assertEqual(len(jload(os.path.join(self.state, "trashed.json"))), 3)

    def test_trashed_videos_from_a_backup_are_dropped_by_plan(self):
        v = clip(os.path.join(self.vids, "2026/2026-10/20261021_090000.mp4"), "2026-10-21T13:00:00Z", seed=5)
        m = clip(os.path.join(self.vids, "2026/2026-10/20261022_100000.mov"), "2026-10-22T14:00:00Z", seed=6)
        backup = os.path.join(self.tmp, "backup", "Internal storage", "DCIM/Camera")
        os.makedirs(backup)
        for x in (v, m):
            shutil.copyfile(x, os.path.join(backup, os.path.basename(x)))
        self.index(v, m)
        self.out("trash", v, m, "--yes")
        self.out("stage", os.path.dirname(os.path.dirname(backup)), "--kind", "phone", "--batch", "back")
        self.out("plan", "back")
        plan = jload(os.path.join(self.state, "batches/back/plan.json"))
        self.assertEqual(sorted((p["rel"], p["action"], p["why"]) for p in plan),
                         [("DCIM/Camera/20261021_090000.mp4", "delete", "trashed before"),
                          ("DCIM/Camera/20261022_100000.mov", "delete", "trashed before")])

    def test_trash_refuses_quarantined_and_takes_review_videos(self):
        quar = os.path.join(self.state, "quarantine", "b1")
        qv = clip(os.path.join(quar, "DCIM/Camera/q.mp4"), seed=7)
        qp = photo(os.path.join(quar, "DCIM/Camera/q.jpg"), 70)
        with open(os.path.join(quar, "quarantine.json"), "w") as f:
            json.dump({"DCIM/Camera/q.mp4": dict(reasons=["x"], sha=sha(qv), size=os.path.getsize(qv), dst=None),
                       "DCIM/Camera/q.jpg": dict(reasons=["x"], sha=sha(qp), size=os.path.getsize(qp), dst=None)}, f)
        rv = clip(os.path.join(self.state, "batches/b1/review/DCIM/Camera/r.mov"), seed=8)
        before = jload(os.path.join(quar, "quarantine.json"))
        out = self.out("trash", "b1", qv, rv, "--yes", code=1)
        self.assertIn("refused 1 quarantined", out)
        self.assertTrue(os.path.isfile(qv), "a quarantined video is erased or kept, never trashed")
        self.assertFalse(os.path.exists(rv))
        self.assertEqual(jload(os.path.join(quar, "quarantine.json")), before)

    def test_trash_refuses_a_video_outside_the_library_and_a_sidecar(self):
        dl = clip(os.path.join(self.home, "Downloads/x.mp4"), seed=1)
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=2)
        side = self.xmp(v, "Label|food")
        out = self.out("trash", dl, side, v, "--yes", code=1)
        self.assertIn("trashed 1 files", out)
        self.assertIn("refused 2", out)
        self.assertTrue(os.path.isfile(dl))
        self.assertFalse(os.path.exists(v) or os.path.exists(side))

    def test_trash_marks_surviving_video_copies_owner_trash(self):
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=3)
        copy = os.path.join(self.shoot, "v-copy.mp4")
        kept = os.path.join(self.vids, "2025/2025-01/v-kept.mp4")
        for c in (copy, kept):
            os.makedirs(os.path.dirname(c), exist_ok=True)
            shutil.copyfile(v, c)
        self.xmp(kept, "Owner|keep")
        s = sha(v)
        self.index(v, copy, kept)
        out = self.out("trash", v, "--yes")
        self.assertIn("copies left on disk marked Owner|trash 1", out)
        self.assertIn("copies you kept left alone 1", out)
        self.assertEqual(self.tags(copy), ["Owner|trash"])
        self.no_sidecar_named_without_ext(copy)
        self.assertEqual((sha(copy), sha(kept)), (s, s), "Owner|trash goes to the sidecar; videos are never written")
        self.assertEqual(self.tags(kept), ["Owner|keep"])
        self.assertEqual(self.suggested(), {copy: ["Owner|trash"]})
        self.out("not-trash", copy)  # Keep supersedes the trash decision
        self.assertEqual(self.tags(copy), ["Owner|keep", "Owner|not-trash"])
        self.assertEqual(self.suggested(), {})

    def test_from_digikam_rejected_photo_and_videos(self):
        p, v, m = self.trio()
        ok = clip(os.path.join(self.vids, "2026/2026-11/ok.mp4"), seed=11)
        for x in (p, v, m):
            self.xmp(x, extra=["-XMP-digiKam:PickLabel=1"])
        self.xmp(ok, extra=["-XMP-digiKam:PickLabel=3"])
        out = self.out("trash", "--from-digikam")
        self.assertIn("3 library files marked Rejected", out)
        self.assertIn("dry run", out)
        self.assertTrue(all(os.path.isfile(x) for x in (p, v, m)))
        self.out("trash", "--from-digikam", "--yes")
        for x in (p, v, m):
            self.assertFalse(os.path.exists(x) or os.path.exists(x + ".xmp"), x)
        self.assertTrue(os.path.isfile(ok) and os.path.isfile(ok + ".xmp"))
        self.assertEqual(len(jload(os.path.join(self.state, "trashed.json"))), 3)


class Forget(Base):
    def test_forget_photo_and_videos_everywhere(self):
        p, v, m = self.trio()
        for x in (p, v, m):
            self.xmp(x, "Label|food")
        gt = [self.gthumb(x) for x in (p, v, m)]
        fdt = [fd_thumb(self.home, x) for x in (p, v, m)]
        stray = os.path.join(self.home, "Downloads/sent.mp4")  # a copy elsewhere in HOME
        os.makedirs(os.path.dirname(stray))
        shutil.copyfile(v, stray)
        keeper = photo(os.path.join(self.lib, "2026/2026-11/keep.jpg"), 90)
        sums = [sha(x) for x in (p, v, m)]
        self.index(p, v, m, keeper)
        out = self.out("forget", p, v, m, "--yes")
        self.assertIn(": 3 from your library, 0 more in your library, 1 in your home folder", out)
        for x in (p, v, m, p + ".xmp", v + ".xmp", m + ".xmp", stray, *gt, *fdt):
            self.assertFalse(os.path.lexists(x), x)
        self.assertEqual(list(jload(os.path.join(self.state, "index.json"))), [keeper])
        self.assertEqual([d for d in os.listdir(os.path.join(self.state, "quarantine")) if d.startswith("forget-")], [])
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            log = f.read()
        for s in sums:
            self.assertIn(s, log)
        self.assertNotIn("v0.mp4", log, "the erase log holds checksums only")

    def test_forget_with_a_missing_file_moves_nothing(self):
        # Regression: forget moved the first file into quarantine and dropped its sidecar, then crashed on the
        # missing one before quarantine.json was written; the video was stranded, untracked, in quarantine.
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=4)
        side = self.xmp(v, "Label|food")
        s = sha(v)
        r = self.intake("forget", v, os.path.join(self.vids, "2026/2026-11/missing.mov"), "--yes", code=1)
        self.assertIn("not forgetting", r.stderr)
        self.assertTrue(os.path.isfile(v) and os.path.isfile(side))
        self.assertEqual(sha(v), s)
        qd = os.path.join(self.state, "quarantine")
        self.assertEqual([d for d in os.listdir(qd) if d.startswith("forget-")] if os.path.isdir(qd) else [], [])

    def test_forget_declined_touches_nothing(self):
        # Regression: without --yes, forget moved the files into quarantine and deleted their sidecars before
        # asking; a "no" left them stranded in quarantine with their tags gone.
        p, v, _ = self.trio()
        sides = [self.xmp(x, "Label|food") for x in (p, v)]
        self.index(p, v)
        for answer in ("no\n", "erase\n", "", None):  # None: stdin closed (cron, the queue)
            r = self.intake("forget", p, v, code=1, stdin=answer)
            self.assertIn("nothing erased", r.stderr, repr(answer))
            self.assertNotIn("Traceback", r.stdout + r.stderr, repr(answer))
            # The prompt says what goes: the two files, and every identical copy in HOME.
            self.assertIn("Erase 2 library files for good.\nThis also deletes:\n  - every identical copy anywhere "
                          "in your home folder (your photo folders and any folder you imported from included)\n",
                          r.stdout, repr(answer))
            self.assertIn("It cannot be undone. Type ERASE to go ahead: ", r.stdout, repr(answer))
        for x in (p, v, *sides):
            self.assertTrue(os.path.isfile(x), x)
        self.assertEqual(sorted(jload(os.path.join(self.state, "index.json"))), sorted([p, v]))
        qd = os.path.join(self.state, "quarantine")
        self.assertEqual([d for d in os.listdir(qd) if d.startswith("forget-")] if os.path.isdir(qd) else [], [])

    def test_forget_confirmed_at_the_prompt(self):
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mov"), seed=5)
        self.xmp(v, "Label|food")
        out = self.out("forget", v, stdin="ERASE\n")
        self.assertIn("erased 1 file: 1 from your library, 0 more in your library, 0 in your home folder", out)
        self.assertFalse(os.path.exists(v) or os.path.exists(v + ".xmp"))

    def test_forget_the_same_video_twice_on_the_line(self):
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=6)
        out = self.out("forget", v, os.path.join(self.vids, "2026/2026-11/../2026-11/v.mp4"), "--yes")
        self.assertIn("erased 1 file: 1 from your library, 0 more in your library, 0 in your home folder", out)
        self.assertFalse(os.path.exists(v))


class CtrlCAtThePrompt(unittest.TestCase):
    """intake.ask() in-process: Ctrl-C at a delete prompt is no answer, so nothing is deleted and no
    KeyboardInterrupt traceback escapes. input() is patched to raise it; STATE points at a temp folder."""

    def setUp(self):
        import intake
        self.intake = intake
        self.tmp = tempfile.mkdtemp(prefix="ask-ctrl-c-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, name, value in ((intake, "STATE", os.path.join(self.tmp, "state")),
                                    (builtins, "input", mock.Mock(side_effect=KeyboardInterrupt))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_ask_returns_no_answer_on_ctrl_c(self):
        answer = None
        with redirect_stdout(io.StringIO()):
            try:
                answer = self.intake.ask("Type ERASE: ")
            except KeyboardInterrupt:  # caught here, or it would stop the whole test run
                self.fail("Ctrl-C at the prompt escaped ask()")
        self.assertEqual(answer, "")

    def test_forget_interrupted_at_the_prompt_erases_nothing(self):
        f = os.path.join(self.tmp, "lib", "v.mp4")
        os.makedirs(os.path.dirname(f))
        with open(f, "wb") as fh:
            fh.write(b"synthetic video bytes")
        a = argparse.Namespace(files=[f], drive=None, yes=False)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            try:
                self.intake.cmd_forget(a)
            except KeyboardInterrupt:  # caught here, or it would stop the whole test run
                self.fail("Ctrl-C at forget's prompt escaped as a traceback")
        self.assertEqual(e.exception.code, "nothing erased")
        self.assertTrue(os.path.isfile(f))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state")), "nothing moved into quarantine")


class KeepAndSuggest(Base):
    def test_suggest_and_not_trash_on_photo_and_videos(self):
        p, v, m = self.trio()
        for x, t in ((p, "Review|maybe-trash"), (v, "Review|vlm-trash/dark"), (m, "AI|vlm/m/trash/blurry")):
            self.xmp(x, t, "Label|food")
        kept = clip(os.path.join(self.vids, "2026/2026-11/kept.mov"), seed=12)
        self.xmp(kept, "Review|maybe-trash", "Owner|keep")
        used = clip(os.path.join(self.vids, "2026/2026-11/used.mp4"), seed=13)
        self.xmp(used, "Review|maybe-trash", "Used|2026-11-09-demo-x")
        sums = {x: sha(x) for x in (p, v, m)}
        self.assertEqual(sorted(self.suggested()), sorted([p, v, m]))
        out = self.out("not-trash", v, m, p)
        self.assertIn("kept 3 files", out)
        for x in (p, v, m):
            t = self.tags(x)
            self.assertIn("Owner|keep", t)
            self.assertIn("Owner|not-trash", t)
            self.assertNotIn("Review|maybe-trash", t)
            self.assertEqual(sha(x), sums[x], "Keep writes the sidecar only")
            self.no_sidecar_named_without_ext(x)
        self.assertEqual(self.suggested(), {})

    def test_not_trash_refuses_a_video_without_a_sidecar_and_changes_nothing(self):
        p = photo(os.path.join(self.lib, "2026/2026-11/p.jpg"), 14)
        self.xmp(p, "Review|maybe-trash")
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=14)
        r = self.intake("not-trash", p, v, code=1)
        self.assertIn("1 files are not library files with a sidecar", r.stderr)
        self.assertEqual(self.tags(p), ["Review|maybe-trash"])
        self.assertFalse(os.path.exists(v + ".xmp"))

    def test_retag_never_brings_maybe_trash_back_on_a_kept_video(self):
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=15)
        self.xmp(v, "Review|maybe-trash", "Label|food")
        self.out("not-trash", v)
        fake_venv(self, {v: {"labels": {"kitchen": 0.3}, "review": ["maybe-trash"]}})
        self.out("tag-files", v)
        t = self.tags(v)
        self.assertIn("Owner|keep", t)
        self.assertIn("Label|kitchen", t)
        self.assertNotIn("Review|maybe-trash", t)
        self.assertEqual(self.suggested(), {})

    def test_suggest_trash_batch_filter_includes_videos(self):
        batch = "2026-11-13-sug"
        self.phone_batch(batch)
        f = self.filed
        outside = clip(os.path.join(self.vids, "2026/2026-11/other.mp4"), seed=16)
        for x in (f["jpg"], f["mp4"], f["mov"], outside):
            self.xmp(x, "Review|maybe-trash")
        self.assertEqual(sorted(self.suggested("--batch", batch)), sorted([f["jpg"], f["mp4"], f["mov"]]))
        self.assertEqual(len(self.suggested()), 4)

    def test_migrate_tags_on_video_sidecars(self):
        p, v, m = self.trio()
        for x in (p, v, m):
            self.xmp(x, "Review|not-trash", "Review|maybe-trash")
        sums = {x: sha(x) for x in (p, v, m)}
        self.assertEqual(self.suggested(), {}, "the legacy keep is honoured before migration")
        self.assertIn("3 sidecars have the legacy", self.out("migrate-tags", "--dry-run"))
        self.assertIn("migrated 3 sidecars", self.out("migrate-tags"))
        for x in (p, v, m):
            self.assertEqual(self.tags(x), ["Owner|not-trash", "Review|maybe-trash"])
            self.assertEqual(sha(x), sums[x])
        self.assertIn("migrated 0 sidecars", self.out("migrate-tags"))
        self.assertEqual(self.suggested(), {})


def fake_venv(t, labels):
    """A stand-in venv interpreter: label.py answers with canned labels, geocode.py finds no place."""
    py = os.path.join(t.state, "venv/bin/python")
    os.makedirs(os.path.dirname(py), exist_ok=True)
    lj = os.path.join(t.tmp, "labels.json")
    with open(lj, "w") as fh:
        json.dump(labels, fh)
    with open(py, "w") as fh:
        fh.write("#!/bin/sh\n"
                 "case \"$2\" in\n"
                 f"  *label.py) cat >/dev/null; cat '{lj}' ;;\n"
                 "  *geocode.py) exec /usr/bin/env python3 -I -c "
                 "'import json,sys; print(json.dumps([None for _ in json.load(sys.stdin)]))' ;;\n"
                 "  *) exit 3 ;;\n"
                 "esac\n")
    os.chmod(py, 0o700)


class Quarantine(Base):
    def test_quarantine_and_release_videos_to_their_video_place(self):
        batch = "2026-11-13-quar"
        self.phone_batch(batch, flags=("jpg", "mp4", "mov"))
        quar = os.path.join(self.state, "quarantine", batch)
        q = jload(os.path.join(quar, "quarantine.json"))
        self.assertEqual(sorted(q), ["DCIM/Camera/20261020_120000.jpg", "DCIM/Camera/20261021_090000.mp4",
                                     "DCIM/Camera/20261022_100000.mov"])
        self.assertEqual(q["DCIM/Camera/20261021_090000.mp4"]["dst"], self.filed["mp4"])
        for k in ("jpg", "mp4", "mov"):
            self.assertFalse(os.path.exists(self.filed[k]), k)
        out = self.out("quarantine", batch)
        self.assertIn("3 files in quarantine", out)
        self.assertIn("nudity", out)
        # The video's place is taken meanwhile by another file: the released one goes beside it.
        other = clip(self.filed["mov"], "2026-10-22T14:00:00Z", seed=20)
        qsums = {r: v["sha"] for r, v in q.items()}
        out = self.out("release", batch, "DCIM/Camera/20261021_090000.mp4",
                       os.path.join(quar, "DCIM/Camera/20261022_100000.mov"))
        mov_dst = os.path.join(self.vids, "2026/2026-10/20261022_100000_1.mov")
        self.assertIn(f"released -> {self.filed['mp4']}", out)
        self.assertIn(f"released -> {mov_dst}", out)
        self.assertIn("kept both", out)
        self.assertEqual(sha(self.filed["mp4"]), qsums["DCIM/Camera/20261021_090000.mp4"])
        self.assertEqual(sha(mov_dst), qsums["DCIM/Camera/20261022_100000.mov"])
        self.assertNotEqual(sha(other), sha(mov_dst))
        um = os.umask(0)
        os.umask(um)
        for x in (self.filed["mp4"], mov_dst):
            self.assertEqual(stat.S_IMODE(os.stat(x).st_mode), 0o666 & ~um)
        idx = jload(os.path.join(self.state, "index.json"))
        self.assertEqual(idx[self.filed["mp4"]]["dhash"], None, "videos have no dHash")
        self.assertTrue(idx[mov_dst]["released"])
        self.assertEqual(list(jload(os.path.join(quar, "quarantine.json"))), ["DCIM/Camera/20261020_120000.jpg"])
        self.assertIn("1 file in quarantine", self.out("quarantine", batch))
        # A released false positive stays untagged (docs/intake.md, "Never tagged"), video or photo...
        r = self.intake("mark-used", self.filed["mp4"], "--post", "2026-11-09-demo-x", code=1)
        self.assertIn("1 quarantined/flagged", r.stderr)
        self.assertFalse(os.path.exists(self.filed["mp4"] + ".xmp"))
        # ...and releasing it twice says so.
        self.assertIn("not in quarantine", self.out("release", batch, "DCIM/Camera/20261021_090000.mp4"))

    def test_purge_review_photo_and_videos(self):
        rv = os.path.join(self.state, "batches/b2/review")
        files = [photo(os.path.join(rv, "Pictures/WhatsApp/a.jpg"), 21),
                 clip(os.path.join(rv, "DCIM/Camera/b.mp4"), seed=21),
                 clip(os.path.join(rv, "DCIM/Camera/c.mov"), seed=22)]
        fdt = [fd_thumb(self.home, x) for x in files]
        out = self.out("purge-review", "b2")  # without --yes: a dry run
        self.assertIn("would delete 3 reviewed near-duplicates/junk from batch b2", out)
        self.assertTrue(all(os.path.isfile(x) for x in files + fdt))
        out = self.out("purge-review", "b2", "--yes")
        self.assertIn("deleted 3 reviewed", out)
        self.assertFalse(os.path.exists(rv))
        for t in fdt:
            self.assertFalse(os.path.exists(t), t)

    def test_purge_review_takes_a_batch_name_never_a_path(self):
        # Regression: the batch went into the path unchecked, so `purge-review ../..` deleted a folder named
        # review outside the state folder (here ~/.local/share/review, beside the state folder).
        outside = os.path.join(os.path.dirname(self.state), "review")
        v = clip(os.path.join(outside, "mine.mp4"), seed=23)
        for bad in ("../..", "../../../../Videos", "b2/../.."):
            self.intake("purge-review", bad, code=1)  # the name check comes before the dry run too
            r = self.intake("purge-review", bad, "--yes", code=1)
            self.assertIn("not a batch name", r.stderr)
        self.assertTrue(os.path.isfile(v))


class Tagging(Base):
    def test_tag_files_photo_and_videos_no_labels(self):
        p, v, m = self.trio()
        clip(m, seed=1, gps=True)  # the .mov carries GPS (Keys:GPSCoordinates)
        fake_venv(self, {})
        sums = {x: sha(x) for x in (p, v, m)}
        out = self.out("tag-files", p, v, m, "--no-labels")
        self.assertIn("tagged 3 files", out)
        for x in (p, v, m):
            self.assertTrue(os.path.isfile(x + ".xmp"), x)
            self.no_sidecar_named_without_ext(x)
            self.assertEqual(sha(x), sums[x], f"original changed: {x}")
        self.assertEqual(self.tags(p), ["Project|Personal", "Source|SyntheticCam", "Source|phone camera"])
        self.assertEqual(self.tags(v), ["Project|Personal", "Source|phone camera"])
        self.assertEqual(self.tags(m), ["Event|city-marathon", "Project|Demo", "Source|phone camera"])
        self.assertTrue(str(self.sidecar_field(v, "XMP-exif:DateTimeOriginal")).startswith("2026:11:08 10:00:00"),
                        "a video's UTC creation_time, in local time")
        self.assertAlmostEqual(self.sidecar_field(m, "XMP-exif:GPSLatitude"), 37.5407, places=3)
        self.assertEqual(self.sidecar_field(m, "XMP-iptcExt:Event"), "city-marathon")
        # re-tag: nothing duplicated, originals still untouched
        self.out("tag-files", v, m, "--no-labels", "--event", "city-marathon")
        self.assertEqual(self.tags(v), ["Event|city-marathon", "Project|Personal", "Source|phone camera"])
        for x in (p, v, m):
            self.assertEqual(sha(x), sums[x])

    def test_tag_files_with_labels_on_videos(self):
        p, v, m = self.trio()
        fake_venv(self, {p: {"labels": {"food": 0.4}, "review": []},
                         v: {"labels": {"runner": 0.5}, "review": []},
                         m: {"labels": {}, "review": ["untagged-video"]}})
        self.xmp(v, "pasta", "Label|old")
        self.out("tag-files", p, v, m)
        self.assertIn("Label|food", self.tags(p))
        self.assertEqual([t for t in self.tags(v) if not t.startswith(("Project|", "Source|"))],
                         ["Label|runner", "pasta"])
        self.assertIn("Review|untagged-video", self.tags(m))

    def test_tag_files_skips_a_flagged_video(self):
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=30)
        p = photo(os.path.join(self.lib, "2026/2026-11/p.jpg"), 30)
        q = os.path.join(self.state, "quarantine", "b3")
        os.makedirs(q)
        with open(os.path.join(q, "quarantine.json"), "w") as f:
            json.dump({"x.mp4": dict(reasons=["x"], sha=sha(v), size=os.path.getsize(v), dst=None)}, f)
        out = self.out("tag-files", v, p, "--no-labels")
        self.assertIn("skipped 1 file:", out)
        self.assertFalse(os.path.exists(v + ".xmp"))
        self.assertTrue(os.path.exists(p + ".xmp"))

    def test_tag_batch_with_videos(self):
        batch = "2026-11-13-tag"
        self.phone_batch(batch, flags=("jpg2",))
        fake_venv(self, {})
        sums = {k: sha(self.filed[k]) for k in ("jpg", "mp4", "mov")}
        out = self.out("tag", batch, "--no-labels")
        self.assertIn("3 filed files to tag", out)
        self.assertIn("1 flagged by scan are never tagged", out)
        for k in ("jpg", "mp4", "mov"):
            f = self.filed[k]
            want = ["Project|Personal"] + (["Source|SyntheticCam"] if k == "jpg" else []) + ["Source|phone camera"]
            self.assertEqual(self.tags(f), want, k)  # the photo's EXIF Make is its device
            self.assertEqual(sha(f), sums[k], k)
            self.no_sidecar_named_without_ext(f)
        self.assertTrue(str(self.sidecar_field(self.filed["mp4"], "XMP-exif:DateTimeOriginal"))
                        .startswith("2026:10:21 09:00:00"))
        self.assertTrue(str(self.sidecar_field(self.filed["mov"], "XMP-exif:DateTimeOriginal"))
                        .startswith("2026:10:22 10:00:00"))

    def test_mark_used_photo_and_videos(self):
        p, v, m = self.trio()
        sums = {x: sha(x) for x in (p, v, m)}
        out = self.out("mark-used", p, v, m, "--post", "2026-11-09-demo-recap")
        self.assertIn("marked 3 files Used|2026-11-09-demo-recap", out)
        for x in (p, v, m):
            self.assertEqual(self.tags(x), ["Used|2026-11-09-demo-recap"])
            self.assertEqual(sha(x), sums[x])
            self.no_sidecar_named_without_ext(x)
        self.out("mark-used", v, "--post", "2026-11-09-demo-recap")
        self.assertEqual(self.tags(v), ["Used|2026-11-09-demo-recap"], "idempotent")

    def test_mark_used_refuses_a_flagged_or_missing_video_and_writes_nothing(self):
        p, v, _ = self.trio()
        q = os.path.join(self.state, "quarantine", "b4")
        os.makedirs(q)
        with open(os.path.join(q, "quarantine.json"), "w") as f:
            json.dump({"x.mp4": dict(reasons=["x"], sha=sha(v), size=os.path.getsize(v), dst=None)}, f)
        r = self.intake("mark-used", p, v, "--post", "2026-11-09-demo-recap", code=1)
        self.assertIn("1 quarantined/flagged, 0 missing", r.stderr)
        r = self.intake("mark-used", p, os.path.join(self.vids, "gone.mov"), "--post", "2026-11-09-demo-recap", code=1)
        self.assertIn("0 quarantined/flagged, 1 missing", r.stderr)
        self.assertFalse(os.path.exists(p + ".xmp") or os.path.exists(v + ".xmp"))

    def test_find_returns_videos(self):
        p, v, m = self.trio()
        self.out("tag-files", p, v, m, "--no-labels")
        self.out("mark-used", v, "--post", "2026-11-09-demo-recap")
        self.xmp(clip(os.path.join(self.vids, "2026/2026-11/untagged.mov"), seed=40))  # empty sidecar

        def find(*args):
            return sorted(self.out("find", *args).split())
        self.assertEqual(find("--project", "Demo"), [m])
        self.assertEqual(find("--event", "city-marathon"), [m])
        self.assertEqual(find("--tag", "Source|phone camera"), sorted([p, v, m]))
        self.assertEqual(find("--tag", "Used"), [v])
        self.assertEqual(find("--tag", "Project|Personal", "--never-used"), [p])
        self.assertEqual(find("--unused-since", "0d", "--tag", "Project"), sorted([p, v, m]))
        self.assertEqual(find("--root", self.vids, "--tag", "Project"), [v])
        os.remove(v)  # a sidecar whose video is gone is not a hit
        self.assertEqual(find("--tag", "Used"), [])


class ShareCopy(Base):
    def test_share_copy_strips_photo_and_video_metadata(self):
        p = photo(os.path.join(self.lib, "2026/2026-10/g.jpg"), 50, "2026:10:20 12:00:00", gps=True)
        v = clip(os.path.join(self.vids, "2026/2026-11/v.mp4"), seed=50, gps=True,
                 extra=["-metadata", "title=secret", "-metadata", "location=+37.5407-077.4360/"])
        m = clip(os.path.join(self.shoot, "m.mov"), seed=51, gps=True, extra=["-metadata", "title=secret"])
        for x in (p, v, m):
            self.xmp(x, "Label|food")
        sums = {x: sha(x) for x in (p, v, m)}
        out_dir = os.path.join(self.tmp, "share-out")
        out = self.out("share-copy", p, v, m, "--out", out_dir)
        self.assertNotIn("STILL PRESENT", out)
        self.assertEqual(out.count("gps=none"), 3)
        self.assertEqual(sorted(os.listdir(out_dir)), ["g.jpg", "m.mov", "v.mp4"], "no sidecar goes with a copy")
        for x in (p, v, m):
            self.assertEqual(sha(x), sums[x], f"original changed: {x}")
        for name in ("g.jpg", "m.mov", "v.mp4"):
            r = subprocess.run(["exiftool", "-j", "-ee", "-q", "-G1", "-GPS*", "-*GPS*", "-Title", "-Location*",
                                "-CreateDate", "-XMP:all", os.path.join(out_dir, name)],
                               capture_output=True, text=True, check=True)
            row = json.loads(r.stdout)[0]
            row.pop("SourceFile")
            leaked = {k: val for k, val in row.items() if not (k.endswith(":CreateDate")
                                                             and str(val).startswith("0000:00:00"))}
            self.assertEqual(leaked, {}, name)
        for name in ("m.mov", "v.mp4"):
            info = streams(os.path.join(out_dir, name))
            self.assertEqual(sorted(s["codec_type"] for s in info["streams"]), ["audio", "video"], name)
            tags = info["format"].get("tags", {})
            self.assertEqual(set(tags) - {"major_brand", "minor_version", "compatible_brands", "encoder"}, set(),
                             f"{name}: {tags}")

    def test_share_copy_drops_a_video_cover_picture_and_subtitles(self):
        # Regression: -map 0:v kept an attached picture (mp4 cover art), a JPEG that may carry its own EXIF GPS.
        base = clip(os.path.join(self.tmp, "src/base.mp4"), seed=52)
        cover = photo(os.path.join(self.tmp, "src/cover.jpg"), 52, "2026:10:20 12:00:00", gps=True)
        srt = os.path.join(self.tmp, "src/s.srt")
        with open(srt, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
        v = os.path.join(self.vids, "2026/2026-11/covered.mp4")
        os.makedirs(os.path.dirname(v), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", base, "-i", cover, "-i", srt, "-map", "0", "-map", "1",
                        "-map", "2", "-c", "copy", "-c:s", "mov_text", "-disposition:v:1", "attached_pic", v], check=True)
        self.assertIn(1, [s.get("disposition", {}).get("attached_pic") for s in streams(v)["streams"]])
        out_dir = os.path.join(self.tmp, "share-out")
        self.out("share-copy", v, "--out", out_dir)
        got = streams(os.path.join(out_dir, "covered.mp4"))["streams"]
        self.assertEqual(sorted(s["codec_type"] for s in got), ["audio", "video"])
        self.assertEqual([s.get("disposition", {}).get("attached_pic") for s in got if s["codec_type"] == "video"], [0])

    def test_share_copy_video_with_a_colon_in_its_name(self):
        # Regression: run from the video's folder, ffmpeg read "a:b.mp4" as protocol "a" and the copy failed.
        v = clip(os.path.join(self.vids, "2026/2026-11/a:b.mp4"), seed=54)
        before = sha(v)
        out_dir = os.path.join(self.tmp, "share-out")
        r = subprocess.run([PY, "-I", INTAKE, "share-copy", "a:b.mp4", "--out", out_dir], env=self.env,
                           cwd=os.path.dirname(v), capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = streams(os.path.join(out_dir, "a:b.mp4"))["streams"]
        self.assertEqual(sorted(s["codec_type"] for s in got), ["audio", "video"])
        self.assertEqual(sha(v), before)

    def test_share_copy_never_replaces_its_own_source(self):
        # Regression: --out the source's own folder removed the original (exiftool -o refuses to overwrite,
        # so share-copy deleted "the old copy" first, which was the file itself).
        dl = photo(os.path.join(self.home, "Downloads/a.jpg"), 53, "2026:10:20 12:00:00", gps=True)
        vd = clip(os.path.join(self.home, "Downloads/b.mp4"), seed=53)
        sums = {x: sha(x) for x in (dl, vd)}
        r = self.intake("share-copy", dl, vd, "--out", os.path.join(self.home, "Downloads"), code=1)
        self.assertIn("not copying", r.stderr)
        self.assertEqual({x: sha(x) for x in (dl, vd)}, sums)

    def test_share_copy_refuses_an_out_folder_in_the_library(self):
        # Regression: --out another library folder replaced a different original of the same name there.
        a = photo(os.path.join(self.lib, "2026/2026-10/x.jpg"), 54)
        b = photo(os.path.join(self.lib, "2026/2026-11/x.jpg"), 55)
        v = clip(os.path.join(self.vids, "2026/2026-10/c.mp4"), seed=54)
        w = clip(os.path.join(self.vids, "2026/2026-11/c.mp4"), seed=55)
        sums = {x: sha(x) for x in (a, b, v, w)}
        for out_dir in (os.path.join(self.lib, "2026/2026-11"), os.path.join(self.vids, "2026/2026-11"),
                        os.path.join(self.state, "share")):
            r = self.intake("share-copy", a, v, "--out", out_dir, code=1)
            self.assertIn("inside the library or the state folder", r.stderr)
        self.assertEqual({x: sha(x) for x in sums}, sums)
        self.assertFalse(os.path.exists(os.path.join(self.state, "share")))

    def test_share_copy_refuses_two_files_with_one_name(self):
        # Regression: the second same-named file silently replaced the first one's copy.
        a = photo(os.path.join(self.lib, "2026/2026-10/x.jpg"), 56)
        b = photo(os.path.join(self.lib, "2026/2026-11/x.jpg"), 57)
        v = clip(os.path.join(self.vids, "2026/2026-10/c.mp4"), seed=56)
        w = clip(os.path.join(self.vids, "2026/2026-11/c.mp4"), seed=57)
        out_dir = os.path.join(self.tmp, "share-out")
        for pair in ((a, b), (v, w)):
            r = self.intake("share-copy", *pair, "--out", out_dir, code=1)
            self.assertIn("same file name", r.stderr)
        self.assertFalse(os.path.exists(out_dir) and os.listdir(out_dir))


if __name__ == "__main__":
    unittest.main()
