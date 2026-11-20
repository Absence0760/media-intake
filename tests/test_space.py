#!/usr/bin/env python3
"""Tests for `media-intake space` (space.py) on a synthetic library in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_space.py

The library holds two videos (fake bytes: only sizes matter), an exact copy pair, a near-copy kept after
apply set it aside, a Suggested junk photo (Review|maybe-trash), a kept one with the same tag, and a flagged
file (in a batch's scan.json) that must never be listed. Sidecars are hand-written XMP (no exiftool). The
command runs as a subprocess with HOME set, so the real library and cache are never looked at; every file is
compared byte for byte before and after, since the report must change nothing.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

support.check_hermetic()
CLI = os.path.join(support.PKG, "cli.py")
SPACE = os.path.join(support.PKG, "space.py")


def xmp(*tags, rating=None):
    li = "".join(f"<rdf:li>{t}</rdf:li>" for t in tags)
    rate = f" xmlns:xmp='http://ns.adobe.com/xap/1.0/' xmp:Rating='{rating}'" if rating else ""
    return ("<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
            "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
            f"<rdf:Description xmlns:lr='http://ns.adobe.com/lightroom/1.0/'{rate}>"
            f"<lr:hierarchicalSubject><rdf:Bag>{li}</rdf:Bag></lr:hierarchicalSubject></rdf:Description>\n"
            "</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")


def snapshot(*folders):
    out = {}
    for folder in folders:
        for d, _, names in os.walk(folder):
            for n in names:
                p = os.path.join(d, n)
                with open(p, "rb") as f:
                    out[p] = f.read()
    return out


class SpaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="space-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.foot = os.path.join(self.home, "Videos/demo-footage")
        for d in (self.state, os.path.join(self.foot, "originals"), os.path.join(self.lib, "2026/2026-11"),
                  os.path.join(self.vids, "2026/2026-11")):
            os.makedirs(d)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.foot),
                        MEDIA_INTAKE_LOCK_WAIT="0")
        P = lambda n: os.path.join(self.lib, "2026/2026-11", n)  # noqa: E731
        V = lambda n: os.path.join(self.vids, "2026/2026-11", n)  # noqa: E731
        self.big, self.small = V("big.mp4"), V("small.mp4")
        self.write(self.big, b"B" * 300_000)
        self.write(self.small, b"S" * 100_000)
        self.copy_a, self.copy_b = P("copy-a.jpg"), P("copy-b.jpg")
        self.write(self.copy_a, b"C" * 50_000)
        self.write(self.copy_b, b"C" * 50_000)
        self.near = P("near.jpg")
        self.write(self.near, b"N" * 40_000)
        self.junk = P("junk.jpg")
        self.write(self.junk, b"J" * 30_000)
        self.write(self.junk + ".xmp", xmp("Review|maybe-trash").encode())
        self.kept = P("kept.jpg")
        self.write(self.kept, b"K" * 35_000)
        self.write(self.kept + ".xmp", xmp("Review|maybe-trash", "Owner|keep").encode())
        self.fav = P("fav.jpg")
        self.write(self.fav, b"F" * 36_000)
        self.write(self.fav + ".xmp", xmp("Review|maybe-trash", rating=5).encode())
        self.flagged = P("flagged.jpg")
        flagged_bytes = b"X" * 900_000  # bigger than anything: it would head every list if it were not hidden
        self.write(self.flagged, flagged_bytes)
        self.write(self.flagged + ".xmp", xmp("Review|maybe-trash").encode())
        flagged_copy = P("flagged-copy.jpg")
        self.write(flagged_copy, flagged_bytes)
        # One batch: the near-copy filed after review, a flagged file, and a review folder with two files.
        b = os.path.join(self.state, "batches", "phone-20261101")
        os.makedirs(os.path.join(b, "review", "DCIM"))
        self.write(os.path.join(b, "review", "DCIM", "r1.jpg"), b"R" * 20_000)
        self.write(os.path.join(b, "review", "DCIM", "r2.jpg"), b"Q" * 10_000)
        sha = hashlib.sha256(flagged_bytes).hexdigest()
        plan = [dict(rel="DCIM/near.jpg", action="review", why="near-duplicate of library " + self.copy_a,
                     dst=None, lib_dst=self.near, sha="0" * 64, size=40_000),
                dict(rel="DCIM/flagged.jpg", action="file", why="", dst=self.flagged, sha=sha, size=900_000)]
        self.write(os.path.join(b, "plan.json"), json.dumps(plan).encode())
        self.write(os.path.join(b, "scan.json"), json.dumps({"DCIM/flagged.jpg": ["nudity"]}).encode())
        # The gallery cache: a thumbnail of content in the library and one of content gone from it.
        self.thumbs = os.path.join(self.home, ".cache/media-intake/thumbs")
        self.proxies = os.path.join(self.home, ".cache/media-intake/proxies")
        os.makedirs(self.thumbs, mode=0o700)
        os.makedirs(self.proxies, mode=0o700)
        with open(self.junk, "rb") as f:
            live = hashlib.sha256(f.read()).hexdigest()
        self.write(os.path.join(self.thumbs, f"{live}-r0.jpg"), b"t" * 1_000)
        self.write(os.path.join(self.thumbs, f"{'a' * 64}-r0.jpg"), b"t" * 2_000)
        self.write(os.path.join(self.proxies, f"{'b' * 64}.mp4"), b"p" * 5_000)

    def write(self, p, data):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def run_space(self, *args, script=CLI):
        pre = ["space"] if script == CLI else []
        before = snapshot(self.home)
        r = subprocess.run([sys.executable, "-I", script, *pre, *args], env=self.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snapshot(self.home), before, "space changed a file")
        return r.stdout

    def report(self, *args):
        return json.loads(self.run_space("--json", *args))

    def test_largest_videos_first(self):
        r = self.report()
        self.assertEqual([x["path"] for x in r["videos"]["list"]], [self.big, self.small])
        self.assertEqual((r["videos"]["files"], r["videos"]["bytes"]), (2, 400_000))

    def test_exact_copies_free_all_but_one(self):
        c = self.report()["copies"]
        self.assertEqual((c["sets"], c["extra"], c["frees"]), (1, 1, 50_000))
        self.assertEqual(c["list"][0]["paths"], [self.copy_a, self.copy_b])

    def test_near_copy_kept_after_review(self):
        n = self.report()["near"]
        self.assertEqual([x["path"] for x in n["list"]], [self.near])
        self.assertEqual(n["frees"], 40_000)

    def test_review_folder_counts_only(self):
        rv = self.report()["review"]
        self.assertEqual(rv["list"], [dict(batch="phone-20261101", files=2, bytes=30_000)])
        self.assertEqual((rv["batches"], rv["files"], rv["frees"]), (1, 2, 30_000))

    def test_suggested_junk_as_the_gallery_suggests_it(self):
        j = self.report()["junk"]
        self.assertEqual([x["path"] for x in j["list"]], [self.junk])  # never the kept one or the favorite
        self.assertEqual(j["list"][0]["why"], ["Tagged as junk"])
        self.assertEqual(j["frees"], 30_000)

    def test_vision_verdict_at_the_default_junk_score(self):
        self.write(self.copy_a + ".xmp", xmp("AI|vlm/m/trash/blurry", "AI|vlm/m/conf/0.90").encode())
        self.write(self.copy_b + ".xmp", xmp("AI|vlm/m/trash/blurry", "AI|vlm/m/conf/0.50").encode())
        j = self.report()["junk"]
        self.assertIn(self.copy_a, [x["path"] for x in j["list"]])
        self.assertNotIn(self.copy_b, [x["path"] for x in j["list"]])  # below gallery.SUGGEST_MIN
        self.assertEqual(next(x["why"] for x in j["list"] if x["path"] == self.copy_a), ["Blurry"])

    def test_flagged_content_never_listed(self):
        out = self.run_space("--json", "--top", "0")
        self.assertNotIn("flagged", out)
        txt = self.run_space("--top", "0")
        self.assertNotIn("flagged", txt)

    def test_cache_sizes_and_prune_dry_run(self):
        ca = self.report()["cache"]
        self.assertEqual(ca["thumbnails"], dict(files=2, bytes=3_000))
        self.assertEqual(ca["proxies"], dict(files=1, bytes=5_000))
        self.assertEqual(ca["prune"], dict(files=2, bytes=7_000, certain=True))
        self.assertTrue(os.path.exists(os.path.join(self.proxies, f"{'b' * 64}.mp4")))  # a report, not a prune

    def test_top_limits_lists_not_totals(self):
        r = self.report("--top", "1")
        self.assertEqual(len(r["videos"]["list"]), 1)
        self.assertEqual(r["videos"]["bytes"], 400_000)
        txt = self.run_space("--top", "1")
        self.assertIn("and 1 more (--top 0 lists every video)", txt)

    def test_text_report_in_plain_words(self):
        txt = self.run_space()
        for words in ("nothing was changed", "Largest videos: 2 videos", "Exact copies: 1 set,",
                      "Near-copies kept in the library: 1 file,", "Review folders: 2 files (near-duplicates and junk)",
                      "media-intake purge-review phone-20261101 --yes", "Suggested junk: 1 file,",
                      "(Tagged as junk)", "Gallery cache: 2 thumbnails", "prune-cache would free",
                      "~/Videos/personal/2026/2026-11/big.mp4"):
            self.assertIn(words, txt)
        self.assertNotIn(self.home, txt)  # paths under HOME are shown as ~/...

    def test_runs_as_a_script(self):
        self.assertIn("Largest videos", self.run_space(script=SPACE))

    def test_negative_top_refused(self):
        r = subprocess.run([sys.executable, "-I", CLI, "space", "--top", "-1"], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
