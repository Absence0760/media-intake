#!/usr/bin/env python3
"""Near-duplicate chains: bounded depth, one review for every level, the matched original shown.

  python3 -I -m unittest discover -s tests -p test_near_chains.py

`erase` is the real intake.py in a throwaway HOME, on Pillow-drawn pictures. The review page's
collect/attach_originals/Runner and drive_queue's pending count run in-process, loaded fresh against
the same kind of throwaway state; the Runner's job is a fake that records each command and runs none.
No real photo, library or state folder is touched.
"""
import hashlib, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
from test_intake import jload, pattern, save_jpg  # noqa: E402  (helpers only; no test class is imported)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
INTAKE = os.path.join(HERE, "intake.py")
PY = sys.executable
B = "2026-11-13-chain"
KEYS = ("HOME", "MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG",
        "MEDIA_INTAKE_NO_LOCALSEARCH")


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="near-chains-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.drive = os.path.join(self.tmp, "drive")
        os.makedirs(self.drive)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        MEDIA_INTAKE_NO_LOCALSEARCH="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def H(self, *p):
        return os.path.join(self.home, *p)

    def Q(self, qb, *p):
        return os.path.join(self.state, "quarantine", qb, *p)

    def quarantined(self, qb, rel, im, **extra):
        """A hand-made quarantine entry: the file under quarantine/<qb>/<rel> and its quarantine.json record."""
        p = self.Q(qb, rel)
        save_jpg(im, p, quality=50)
        qf = self.Q(qb, "quarantine.json")
        q = jload(qf) if os.path.exists(qf) else {}
        q[rel] = dict(reasons=extra.pop("reasons", ["near-duplicate"]), sha=sha(p), size=os.path.getsize(p),
                      dst=self.H(rel), **extra)
        with open(qf, "w") as f:
            json.dump(q, f)
        return p

    def erase(self, qb):
        r = subprocess.run([PY, "-I", INTAKE, "erase", qb, "--yes", "--drive", self.drive, "--no-wipe-free"],
                           env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
        return r.stdout


class EraseChains(Base):
    """intake.py erase: depth bound, adding to an existing level, root_of carried down the chain."""

    def test_chain_stops_at_the_depth_limit_and_counts_what_stays(self):
        deepest = "near-" * 3 + B  # NEAR_DEPTH
        erased = self.quarantined(deepest, "Pictures/library/a/c.jpg", pattern(6).resize((800, 600)),
                                  near_of_sha="0" * 64, erased_on="2026-11-12")
        copy = self.H("Pictures/library/b/copy.jpg")
        save_jpg(pattern(6).resize((900, 675)), copy, quality=60)
        out = self.erase(deepest)
        self.assertFalse(os.path.exists(erased))
        self.assertTrue(os.path.isfile(copy), "past the limit a near-copy stays in the library")
        self.assertFalse(os.path.exists(self.Q("near-" + deepest)), "no level past NEAR_DEPTH")
        self.assertIn("1 near-duplicates of erased photos left where they are, not moved "
                      "(near-duplicate chains stop at depth 3)", out)
        self.assertNotIn(copy, out, "the count, not the file")

    def test_within_the_limit_the_next_level_is_written(self):
        lvl2 = "near-near-" + B
        self.quarantined(lvl2, "Pictures/library/a/c.jpg", pattern(6).resize((800, 600)),
                         near_of_sha="0" * 64, erased_on="2026-11-12")
        copy = self.H("Pictures/library/b/copy.jpg")
        save_jpg(pattern(6).resize((900, 675)), copy, quality=60)
        out = self.erase(lvl2)
        self.assertFalse(os.path.exists(copy))
        self.assertIn("Pictures/library/b/copy.jpg", jload(self.Q("near-" + lvl2, "quarantine.json")))
        self.assertIn(f"media-intake review {B}", out, "one review of the root batch shows every level")
        self.assertNotIn("left where they are", out)

    def test_an_existing_level_is_added_to_never_replaced(self):
        waiting = self.quarantined(f"near-{B}", "Downloads/old.jpg", pattern(9).resize((700, 525)),
                                   near_of_sha="1" * 64, erased_on="2026-11-01")
        self.quarantined(B, "DCIM/s.jpg", pattern(6).resize((800, 600)), reasons=["nudity"])
        copy = self.H("Pictures/library/b/copy.jpg")
        save_jpg(pattern(6).resize((900, 675)), copy, quality=60)
        self.erase(B)
        q = jload(self.Q(f"near-{B}", "quarantine.json"))
        self.assertEqual(sorted(q), ["Downloads/old.jpg", "Pictures/library/b/copy.jpg"])
        self.assertTrue(os.path.isfile(waiting), "the waiting near-copy keeps its file and its record")

    def test_root_original_is_carried_down_and_never_moved(self):
        orig = self.H("Pictures/library/2025/IMG_1.jpg")
        save_jpg(pattern(6), orig, "2025:06:01 10:00:00")
        xb = f"xcheck-{B}"
        wa_rel = "Pictures/library/Messaging/WhatsApp/w1.jpg"
        self.quarantined(xb, wa_rel, pattern(6).resize((800, 600)), reasons=["messaging copy"], of=orig)
        n1 = self.H("Pictures/library/x/n1.jpg")
        save_jpg(pattern(6).resize((900, 675)), n1, quality=60)
        self.erase(xb)
        rec = jload(self.Q(f"near-{xb}", "quarantine.json"))["Pictures/library/x/n1.jpg"]
        self.assertEqual(rec["root_of"], orig)
        self.assertNotIn("Messaging", json.dumps(rec), "never the erased file's path")
        # Erasing that level: the root original is spared again (it is a near-copy of n1 too), the next copy moves.
        n2 = self.H("Pictures/library/y/n2.jpg")
        save_jpg(pattern(6).resize((1000, 750)), n2, quality=55)
        self.erase(f"near-{xb}")
        self.assertTrue(os.path.isfile(orig), "the root original stays in the library at every level")
        rec2 = jload(self.Q(f"near-near-{xb}", "quarantine.json"))["Pictures/library/y/n2.jpg"]
        self.assertEqual(rec2["root_of"], orig)
        self.assertEqual(rec2["near_of_sha"], rec["sha"])
        self.assertNotIn("x/n1.jpg", json.dumps(rec2), "never the erased file's path")


class ReleaseNeverReplaces(Base):
    """release puts a kept near-copy back at its old place only while that place is free."""

    def test_release_onto_a_refilled_place_keeps_both(self):
        rel = "Pictures/library/2026/2026-08/IMG_1.jpg"
        lib = self.H(rel)
        # erase moved IMG_1.jpg (with its tags) into near-<b>; a later apply filed a different IMG_1.jpg there
        near = self.quarantined(f"near-{B}", rel, pattern(6).resize((800, 600)),
                                near_of_sha="0" * 64, erased_on="2026-11-12")
        with open(near + ".xmp", "w") as f:
            f.write("near-copy's own tags")
        save_jpg(pattern(8), lib, "2026:08:01 10:00:00")
        with open(lib + ".xmp", "w") as f:
            f.write("new photo's tags")
        new_sha, near_sha = sha(lib), sha(near)
        idx = os.path.join(self.state, "index.json")
        with open(idx, "w") as f:
            json.dump({lib: dict(sha=new_sha, size=os.path.getsize(lib), dhash=None)}, f)
        r = subprocess.run([PY, "-I", INTAKE, "release", f"near-{B}", rel], env=self.env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        moved = self.H("Pictures/library/2026/2026-08/IMG_1_1.jpg")
        self.assertIn(f"released -> {moved}", r.stdout)
        self.assertIn("IMG_1.jpg is taken there: kept both", r.stdout)
        self.assertEqual(sha(lib), new_sha, "the photo filed there is untouched")
        with open(lib + ".xmp") as f:
            self.assertEqual(f.read(), "new photo's tags")
        self.assertEqual(sha(moved), near_sha)
        with open(moved + ".xmp") as f:
            self.assertEqual(f.read(), "near-copy's own tags")
        self.assertFalse(os.path.exists(near) or os.path.exists(near + ".xmp"))
        index = jload(idx)
        self.assertEqual(index[lib]["sha"], new_sha, "its index entry is not replaced")
        self.assertEqual((index[moved]["sha"], index[moved]["released"]), (near_sha, True))
        self.assertEqual(jload(self.Q(f"near-{B}", "quarantine.json")), {})


def fresh(name, env):
    """Load media_intake/<name>.py anew under env (its STATE and roots are read at import)."""
    old = {k: os.environ.get(k) for k in KEYS}
    os.environ.update({k: env[k] for k in KEYS})
    try:
        spec = importlib.util.spec_from_file_location(f"near_chains_{name}", os.path.join(HERE, f"{name}.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeJob:
    def __init__(self, state):
        self.state, self.calls, self.lines = state, [], []

    def log(self, s, safe=True):
        self.lines.append(s)

    def run(self, args, label):
        self.calls.append(args)
        if args[0] == "erase":  # what a real erase leaves: its own quarantine gone
            shutil.rmtree(os.path.join(self.state, "quarantine", args[1]))
        return 0


class ReviewChains(Base):
    """review_ui: every level in one review, deepest erased first, the root original shown."""

    def setUp(self):
        super().setUp()
        self.ui = fresh("review_ui", self.env)
        self.orig = self.H("Pictures/library/2025/IMG_1.jpg")
        save_jpg(pattern(6), self.orig, "2025:06:01 10:00:00")
        self.quarantined(B, "DCIM/s.jpg", pattern(4), reasons=["nudity"])
        self.quarantined(f"xcheck-{B}", "Pictures/library/Messaging/w.jpg", pattern(6).resize((800, 600)),
                         reasons=["messaging copy"], of=self.orig)
        self.quarantined(f"near-{B}", "Downloads/n1.jpg", pattern(4).resize((600, 450)),
                         near_of_sha="0" * 64, erased_on="2026-11-10")
        self.quarantined(f"near-near-{B}", "Downloads/n2.jpg", pattern(4).resize((500, 375)),
                         near_of_sha="1" * 64, erased_on="2026-11-11")
        self.quarantined(f"near-near-xcheck-{B}", "Downloads/n3.jpg", pattern(6).resize((500, 375)),
                         near_of_sha="2" * 64, erased_on="2026-11-12", root_of=self.orig)
        os.makedirs(self.Q(f"near-other-{B}"))  # another batch's chain is not this one's

    def test_every_level_in_one_review_deepest_first(self):
        self.assertEqual(self.ui.sub_batches(B), [f"near-near-{B}", f"near-{B}", f"near-near-xcheck-{B}",
                                                  f"xcheck-{B}", B])
        items = self.ui.collect(B)
        self.assertEqual([it["qbatch"] for it in items],
                         [f"near-near-{B}", f"near-{B}", f"near-near-xcheck-{B}", f"xcheck-{B}", B])

    def test_root_original_shown_where_it_survives_else_the_erase_date(self):
        items = self.ui.collect(B)
        origs = self.ui.attach_originals(B, items)
        by = {it["qbatch"]: it["orig"] for it in items}
        self.assertEqual(by[f"near-{B}"], dict(erased=True, date="2026-11-10"))
        self.assertEqual(by[f"near-near-{B}"], dict(erased=True, date="2026-11-11"))
        o = by[f"near-near-xcheck-{B}"]
        self.assertEqual(origs[o["id"]]["path"], self.orig)
        self.assertIn("erased on 2026-11-12", o["label"])
        self.assertEqual(o["meta"]["res"], "1200x900")
        # The root gone (erased or moved since): the date note, never a dangling original.
        os.remove(self.orig)
        items = self.ui.collect(B)
        self.ui.attach_originals(B, items)
        self.assertEqual({it["qbatch"]: it["orig"] for it in items}[f"near-near-xcheck-{B}"],
                         dict(erased=True, date="2026-11-12"))

    def test_root_outside_the_library_is_not_served(self):
        outside = os.path.join(self.tmp, "elsewhere.jpg")
        save_jpg(pattern(6), outside)
        q = jload(self.Q(f"near-near-xcheck-{B}", "quarantine.json"))
        q["Downloads/n3.jpg"]["root_of"] = outside
        with open(self.Q(f"near-near-xcheck-{B}", "quarantine.json"), "w") as f:
            json.dump(q, f)
        items = self.ui.collect(B)
        origs = self.ui.attach_originals(B, items)
        self.assertNotIn(outside, [o["path"] for o in origs])
        self.assertEqual({it["qbatch"]: it["orig"] for it in items}[f"near-near-xcheck-{B}"],
                         dict(erased=True, date="2026-11-12"))

    def test_runner_erases_deepest_first_and_names_one_review(self):
        items = self.ui.collect(B)
        spec = dict(batch=B, items=[{k: it[k] for k in ("tab", "qbatch", "rel", "path")} for it in items],
                    decisions={str(i): "erase" for i in range(len(items))})
        job = FakeJob(self.state)
        runner = self.ui.Runner(job, spec)

        def erase_feeds_next(args, label, run=job.run):  # erasing near-<B> writes a fresh near-near-<B>
            r = run(args, label)
            if args[:2] == ["erase", f"near-{B}"]:
                self.quarantined(f"near-near-{B}", "Downloads/fresh.jpg", pattern(4).resize((450, 338)),
                                 near_of_sha="3" * 64, erased_on="2026-11-13")
            return r
        job.run = erase_feeds_next
        self.assertTrue(runner.execute())
        self.assertEqual([c[1] for c in job.calls if c[0] == "erase"],
                         [f"near-near-{B}", f"near-{B}", f"near-near-xcheck-{B}", f"xcheck-{B}", B])
        self.assertIn(f"1 near-duplicates now in quarantine/near-near-{B}: review {B} again to decide them",
                      job.lines)
        self.assertTrue(os.path.isfile(self.Q(f"near-near-{B}", "Downloads/fresh.jpg")),
                        "a near-copy found during the run is never erased unreviewed")


class QueuePending(Base):
    def test_pending_counts_every_level(self):
        self.quarantined(f"near-{B}", "Downloads/n1.jpg", pattern(4).resize((600, 450)))
        self.quarantined(f"near-near-{B}", "Downloads/n2.jpg", pattern(4).resize((500, 375)))
        self.quarantined(f"near-near-{B}", "Downloads/n3.jpg", pattern(5).resize((500, 375)))
        self.quarantined(f"near-other-{B}", "Downloads/n4.jpg", pattern(5).resize((500, 375)))
        dq = fresh("drive_queue", self.env)
        self.assertEqual(dq.pending(B), (0, 3, 0))


if __name__ == "__main__":
    unittest.main()
