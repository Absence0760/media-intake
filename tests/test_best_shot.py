#!/usr/bin/env python3
"""Tests for best_shot.py: sharp vs blurred vs over-exposed synthetic frames, the sets it builds, the owner
rules and the sidecar tags it writes. Every image is drawn here; no library is read.

  python3 -I -m unittest discover -s tests -p test_best_shot.py
"""
import io, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config; puts media_intake/ on sys.path)

import best_shot  # noqa: E402
import cli  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
except ImportError:  # pragma: no cover  setup.sh installs Pillow
    Image = None


def scene(size=(320, 240)):
    """A frame with edges: a checker of grey squares and a few lines, mid-tones throughout."""
    im = Image.new("RGB", size, (110, 120, 130))
    d = ImageDraw.Draw(im)
    for y in range(0, size[1], 20):
        for x in range(0, size[0], 20):
            if (x // 20 + y // 20) % 2:
                d.rectangle([x, y, x + 19, y + 19], fill=(150, 140, 120))
    for k in range(0, size[0], 37):
        d.line([k, 0, size[0] - k, size[1]], fill=(60, 70, 80), width=2)
    return im


def blurred(im, r=4):
    return im.filter(ImageFilter.GaussianBlur(r))


def overexposed(im):
    return ImageEnhance.Brightness(im).enhance(3.0)


def item(path, burst=None, tags=(), media="photo", **kw):
    return dict(path=path, media=media, burst=burst, tags=list(tags), pick="", rating="", used=[], project="",
                gone=False, **kw)


@unittest.skipIf(Image is None, "Pillow not installed")
class MeasureTest(unittest.TestCase):
    def test_sharp_beats_blurred(self):
        s, b = best_shot.measure(scene()), best_shot.measure(blurred(scene()))
        self.assertGreater(s[0], b[0] * 2)
        self.assertEqual(best_shot.best([b, s, best_shot.measure(blurred(scene(), 1.5))]), 1)

    def test_overexposed_frame_is_clipped_and_loses(self):
        good, hot = best_shot.measure(blurred(scene(), 1)), best_shot.measure(overexposed(scene()))
        self.assertLess(good[1], 0.05)
        self.assertGreater(hot[1], best_shot.CLIP_MAX)
        # the blown frame keeps some edges, but a well-exposed one wins anyway
        self.assertEqual(best_shot.best([hot, good]), 1)

    def test_dark_frame_is_clipped(self):
        dark = ImageEnhance.Brightness(scene()).enhance(0.02)
        self.assertGreater(best_shot.measure(dark)[1], best_shot.CLIP_MAX)

    def test_large_frame_is_downscaled_first(self):
        big = scene((2000, 1500))
        with mock.patch.object(Image.Image, "filter", autospec=True, side_effect=Image.Image.filter) as f:
            best_shot.measure(big)
        self.assertLessEqual(max(f.call_args[0][0].size), best_shot.MAX_PX)

    def test_best_skips_unscored_and_keeps_the_earlier_on_a_tie(self):
        self.assertIsNone(best_shot.best([None, None]))
        self.assertEqual(best_shot.best([None, (5.0, 0.0)]), 1)
        self.assertEqual(best_shot.best([(5.0, 0.0), (5.0, 0.0)]), 0)

    def test_score_file_reads_and_unreadable_is_none(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "a.jpg")
        scene().save(p, quality=92)
        before = os.stat(p)
        self.assertIsNotNone(best_shot.score_file(p))
        after = os.stat(p)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns), "never written")
        bad = os.path.join(d, "b.dng")
        with open(bad, "wb") as f:
            f.write(b"not an image")
        self.assertIsNone(best_shot.score_file(bad))


class SetsTest(unittest.TestCase):
    def test_bursts_and_near_duplicates_join(self):
        items = [item("/l/a.jpg", burst=0), item("/l/b.jpg", burst=0), item("/l/c.jpg"),
                 item("/l/d.jpg"), item("/l/e.jpg"), item("/l/v.mp4", media="video")]
        index = {"/l/b.jpg": {"dhash": 0b1111}, "/l/c.jpg": {"dhash": 0b0111},          # c: 1 bit from b
                 "/l/d.jpg": {"dhash": (1 << 63) | 0xFFFF0000},                            # d: far from all
                 "/l/e.jpg": {"dhash": ((1 << 63) | 0xFFFF0000) ^ 0b11},                    # e: 2 bits from d
                 "/l/v.mp4": {"dhash": 0b1111}}                                           # a video: never
        self.assertEqual(best_shot.sets(items, index), [[0, 1, 2], [3, 4]])

    def test_near_pairs_threshold(self):
        n = best_shot.NEAR_MAX
        hs = [0, (1 << n) - 1, (1 << (n + 1)) - 1, None]
        self.assertEqual(best_shot.near_pairs(hs), {(0, 1), (1, 2)})

    def test_singletons_and_gone_are_no_set(self):
        items = [item("/l/a.jpg", burst=3), item("/l/b.jpg", burst=3, )]
        items[1]["gone"] = True
        self.assertEqual(best_shot.sets(items, {}), [])


def fake_scores(table):
    return lambda p: table.get(os.path.basename(p))


class PlanTest(unittest.TestCase):
    SCORES = {"a.jpg": (10.0, 0.0), "b.jpg": (90.0, 0.0), "c.jpg": (300.0, 0.6)}

    def burst(self, *extra_tags):
        return [item("/l/a.jpg", burst=0, tags=extra_tags[0] if extra_tags else ()),
                item("/l/b.jpg", burst=0), item("/l/c.jpg", burst=0)]

    def test_cover_and_not_best(self):
        changes, c = best_shot.plan(self.burst(), {}, fake_scores(self.SCORES), protected=set())
        self.assertEqual(changes, {"/l/a.jpg": ([], best_shot.NOT_BEST), "/l/b.jpg": ([], best_shot.COVER),
                                   "/l/c.jpg": ([], best_shot.NOT_BEST)})
        self.assertEqual((c["sets"], c["covers"], c["not_best"]), (1, 1, 2))

    def test_owner_tags_win(self):
        for tags in (["Owner|keep", "Owner|not-trash"], ["Review|not-trash"], ["Owner|trash"], ["People|Ann"],
                     ["Used|post-1"]):
            with self.subTest(tags):
                items = self.burst(tags + [best_shot.NOT_BEST])
                changes, c = best_shot.plan(items, {}, fake_scores(self.SCORES), protected=set())
                self.assertEqual(changes["/l/a.jpg"], ([best_shot.NOT_BEST], None), "its old tag goes, none added")
                self.assertEqual(c["owner"], 1)
        for k, v in (("rating", "5"), ("pick", "3")):
            with self.subTest(k):
                items = self.burst()
                items[0][k] = v
                changes, _ = best_shot.plan(items, {}, fake_scores(self.SCORES), protected=set())
                self.assertNotIn("/l/a.jpg", changes)

    def test_protected_project_is_left_alone(self):
        items = self.burst(["Project|Demo"])
        changes, _ = best_shot.plan(items, {}, fake_scores(self.SCORES), protected={"Project|Demo"})
        self.assertNotIn("/l/a.jpg", changes)

    def test_rerun_is_unchanged_and_stale_tags_go(self):
        items = self.burst([best_shot.NOT_BEST])
        items[1]["tags"] = [best_shot.COVER]
        items[2]["tags"] = [best_shot.NOT_BEST]
        lone = item("/l/z.jpg", tags=["Label|dog", best_shot.COVER])
        changes, c = best_shot.plan(items + [lone], {}, fake_scores(self.SCORES), protected=set())
        self.assertEqual(changes, {"/l/z.jpg": ([best_shot.COVER], None)})
        self.assertEqual(c["unchanged"], 3)

    def test_unreadable_member_is_never_tagged(self):
        scores = dict(self.SCORES)
        del scores["a.jpg"]
        changes, c = best_shot.plan(self.burst(), {}, fake_scores(scores), protected=set())
        self.assertNotIn("/l/a.jpg", changes)
        self.assertEqual(c["unscored"], 1)
        changes, c = best_shot.plan(self.burst(), {}, fake_scores({}), protected=set())
        self.assertEqual((changes, c["sets"], c["unscored"]), ({}, 0, 3))


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def test_blocks_touch_only_the_namespace_and_the_sidecar(self):
        p = os.path.join(self.d, "a.jpg")
        b = best_shot.blocks({p: ([best_shot.COVER], best_shot.NOT_BEST)})
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0][-1], p + ".xmp")
        tags = [x.split("=", 1)[1] for x in b[0] if x.startswith("-XMP-lr:")]
        self.assertTrue(all(t.startswith(best_shot.NS) for t in tags))
        self.assertIn(f"-XMP-lr:HierarchicalSubject+={best_shot.NOT_BEST}", b[0])
        with self.assertRaises(ValueError):
            best_shot.blocks({p: (["Owner|keep"], None)})
        with self.assertRaises(ValueError):
            best_shot.blocks({p + ".xmp": ([], best_shot.COVER)})

    def test_write_makes_a_sidecar_only_for_a_new_tag(self):
        a, b, gone = (os.path.join(self.d, n) for n in ("a.jpg", "b.jpg", "gone.jpg"))
        for p in (a, b):
            with open(p, "wb") as f:
                f.write(b"x")
        with mock.patch("intake.exiftool_batch") as run:
            n = best_shot.write({a: ([], best_shot.NOT_BEST), b: ([best_shot.COVER], None),
                                 gone: ([], best_shot.COVER)})
        self.assertEqual(n, 2)
        self.assertTrue(os.path.isfile(a + ".xmp"))
        self.assertFalse(os.path.exists(b + ".xmp"))
        self.assertFalse(os.path.exists(gone + ".xmp"))
        self.assertEqual([blk[-1] for blk in run.call_args[0][0]], [a + ".xmp", b + ".xmp"])
        with open(a, "rb") as f:
            self.assertEqual(f.read(), b"x", "the original is never written")

    @unittest.skipUnless(shutil.which("exiftool") and Image is not None, "needs exiftool and Pillow")
    def test_real_sidecar_round_trip(self):
        frames = {"a.jpg": blurred(scene()), "b.jpg": scene(), "c.jpg": overexposed(scene())}
        items = []
        for name, im in frames.items():
            p = os.path.join(self.d, name)
            im.save(p, quality=92)
            items.append(item(p, burst=0))
        changes, _ = best_shot.plan(items, {}, protected=set())
        best_shot.write(changes)
        got = {}
        for it in items:
            r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", it["path"] + ".xmp"],
                               capture_output=True, text=True, check=True)
            import json
            v = json.loads(r.stdout)[0].get("HierarchicalSubject")
            got[os.path.basename(it["path"])] = v if isinstance(v, list) else [v]
        self.assertEqual(got, {"a.jpg": [best_shot.NOT_BEST], "b.jpg": [best_shot.COVER],
                               "c.jpg": [best_shot.NOT_BEST]})


class CliTest(unittest.TestCase):
    def test_help_says_what_it_touches_and_deletes(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.main(["best-shot", "--help"])
        self.assertEqual(e.exception.code, 0)
        self.assertRegex(out.getvalue(), r"(?m)^Touches: \S")
        self.assertRegex(out.getvalue(), r"(?m)^Deletes: nothing")
        for line in out.getvalue().splitlines():
            self.assertLessEqual(len(line), 80, line)

    def test_dry_run_writes_nothing(self):
        items = [item("/l/a.jpg", burst=0), item("/l/b.jpg", burst=0)]
        out = io.StringIO()
        with mock.patch("gallery.scan_library", return_value=(items, 0)), \
                mock.patch.object(best_shot, "score_file", fake_scores({"a.jpg": (1.0, 0.0), "b.jpg": (2.0, 0.0)})), \
                mock.patch("intake.exiftool_batch") as run, redirect_stdout(out):
            self.assertEqual(cli.main(["best-shot", "--dry-run"]), 0)
        run.assert_not_called()
        self.assertIn("would change 2 sidecars", out.getvalue())
        self.assertNotIn("/l/", out.getvalue(), "counts only, never a path")


class ReviewPageWordsTest(unittest.TestCase):
    def test_not_best_shows_in_plain_words(self):
        """suggest-trash lists a not-best shot; the review page's Maybe trash tab says why in the gallery's words."""
        import review_ui
        self.assertEqual(review_ui.maybe_reasons([best_shot.NOT_BEST, "Label|food"]), ["Not the best shot", "Label|food"])
        self.assertEqual(review_ui.maybe_reasons([best_shot.COVER]), [])


if __name__ == "__main__":
    unittest.main()
