#!/usr/bin/env python3
"""The gallery page in headless Chromium on a library of photos and videos: bulk selection and every
selection action on a mix of both, and the large view on each kind.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_media_kinds.py

Synthetic media in a throwaway HOME: Pillow photos (JPEG, PNG) and short H.264 clips (an .mp4 and an
upper-case .MOV, QuickTime). The real intake.py runs Trash. Each test checks the page (the selection,
the bar, the tiles' flags, the dialog) and what really changed on disk: the XMP sidecars
(<file>.<ext>.xmp), the files, the thumbnail cache and the originals' sha256 (never written). Every
test also fails on an uncaught page error. Hooks: as tests/browser/test_browser_gallery.py, plus each
tile's flag text (.flag) and image.
"""
import os, re, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)


def clip(path, seconds=4):
    """A small H.264 test pattern the page's <video> can play; the container follows the extension."""
    enc = B.h264_encoder()
    if not enc:
        B.unavailable("no H.264 encoder: install ffmpeg with libx264 or libopenh264 for the video tests")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = "mov" if path.lower().endswith(".mov") else "mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=320x240:rate=25:duration={seconds}", "-c:v", enc, "-g", "25",
                    "-pix_fmt", "yuv420p", "-an", "-f", fmt, path], check=True)


THUMBS_LOADED = "() => [...document.querySelectorAll('[data-item] img')].every(i => i.complete && i.naturalWidth > 0)"


class GalleryMediaKindsBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-kinds-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids,
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)  # the real intake.py
        self.thumbs = os.path.join(self.home, ".cache/media-intake/thumbs")
        self.path = {"a.jpg": os.path.join(self.lib, "2026/2026-11/a.jpg"),
                     "b.png": os.path.join(self.lib, "2026/2026-10/b.png"),
                     "v.mp4": os.path.join(self.vids, "2026/2026-11/v.mp4"),
                     "w.MOV": os.path.join(self.vids, "2026/2026-10/w.MOV")}
        G.img(self.path["a.jpg"], (200, 30, 30), (400, 300))
        G.img(self.path["b.png"], (30, 200, 30), (400, 300))
        clip(self.path["v.mp4"])
        clip(self.path["w.MOV"])
        G.write_tags(self.path["a.jpg"], ["Project|Personal", "Review|maybe-trash"])
        G.write_tags(self.path["v.mp4"], ["Project|Personal", "Review|maybe-trash", "Trips|coast"])
        self.orig = {n: G.sha(p) for n, p in self.path.items()}
        self.servers = []
        self.errors = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        return s

    def page_for(self, s):
        page = self.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, s)
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))
        return page

    def tiles(self, page):
        return page.locator("[data-item]").evaluate_all("els => els.map(e => e.dataset.item)")

    def box(self, page, n):
        return page.get_by_role("checkbox", name=f"select {n}", exact=True)

    def tile(self, page, n):
        return page.locator(f'[data-item="{n}"]')

    def bar(self, page):
        return page.get_by_role("toolbar", name="Selection actions")

    def select_all_by_range(self, page):
        names = self.tiles(page)
        self.box(page, names[0]).click()
        self.box(page, names[-1]).click(modifiers=["Shift"])
        B.expect(self.bar(page)).to_contain_text(f"{len(names)} selected")
        return names

    def assert_originals(self):
        for n, p in self.path.items():
            if os.path.exists(p):
                self.assertEqual(G.sha(p), self.orig[n], n)

    # ------------------------------------------------------------ bulk select + Keep
    def test_bulk_select_mixed_and_keep(self):
        s = self.start()
        page = self.page_for(s)
        names = self.tiles(page)
        self.assertEqual(sorted(names), ["a.jpg", "b.png", "v.mp4", "w.MOV"])
        bar = self.bar(page)
        # Space on a focused tile ticks it; K keeps that selection (one photo, one video)
        for n in ("b.png", "w.MOV"):
            self.tile(page, n).focus()
            page.keyboard.press(" ")
        B.expect(bar).to_contain_text("2 selected")
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            page.keyboard.press("k")
        self.assertEqual(resp.value.status, 200)
        for n in ("b.png", "w.MOV"):
            self.assertEqual(sorted(G.tags_of(self.path[n])[0]), ["Owner|keep", "Owner|not-trash"], n)
            B.expect(self.tile(page, n).locator(".flag")).to_have_text("Kept")  # the tile shows it at once
        for n in ("a.jpg", "v.mp4"):
            self.assertNotIn("Owner|keep", G.tags_of(self.path[n])[0], n)
            B.expect(self.tile(page, n).locator(".flag")).to_have_text("Suggested junk")  # not kept yet
        B.expect(bar).to_contain_text("2 selected")  # the selection stays for the next action
        page.keyboard.press("Escape")
        B.expect(bar).to_be_hidden()
        # the whole grid by a shift-click range, a Ctrl-click on a tile takes one out and puts it back
        self.select_all_by_range(page)
        self.tile(page, "v.mp4").click(modifiers=["Control"])
        B.expect(bar).to_contain_text("3 selected")
        B.expect(self.box(page, "v.mp4")).not_to_be_checked()
        self.tile(page, "v.mp4").click(modifiers=["Control"])
        B.expect(bar).to_contain_text("4 selected")
        for n in names:
            B.expect(self.box(page, n)).to_be_checked()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            bar.get_by_role("button", name="Keep").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(page.locator("#toast")).to_have_text("Kept 4. They won't be suggested as junk again.Undo")
        for n, p in self.path.items():
            h, dc, _ = G.tags_of(p)
            self.assertIn("Owner|keep", h, n)
            self.assertIn("Owner|not-trash", h, n)
            self.assertNotIn("Review|maybe-trash", h, n)
            self.assertNotIn("maybe-trash", dc, n)
            self.assertEqual(h.count("Owner|keep"), 1, n)
            B.expect(self.tile(page, n).locator(".flag")).to_have_text("Kept")  # Suggested junk gone too
        self.assertIn("Trips|coast", G.tags_of(self.path["v.mp4"])[0])
        B.expect(bar).to_contain_text("4 selected")
        # nothing is suggested for the trash any more
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Suggested junk").click()
        B.expect(page.get_by_text("No trash suggestions here.")).to_be_visible()
        self.assert_originals()

    def test_shift_click_after_the_grid_reloads(self):
        """A shift-click in a reloaded grid (another view, a filter) starts no range from the old grid's anchor."""
        G.write_tags(self.path["w.MOV"], ["Review|child-in-shot"])
        s = self.start()
        page = self.page_for(s)
        names = self.tiles(page)
        self.box(page, names[-1]).click()  # the anchor: the last of four
        B.expect(self.bar(page)).to_contain_text("1 selected")
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Possible child").click()
        B.expect(page.locator("[data-item]")).to_have_count(1)
        B.expect(self.bar(page)).to_be_hidden()
        self.box(page, "w.MOV").click(modifiers=["Shift"])
        B.expect(self.bar(page)).to_be_visible()
        B.expect(self.bar(page)).to_contain_text("1 selected")
        B.expect(self.box(page, "w.MOV")).to_be_checked()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            page.keyboard.press("k")
        self.assertEqual(resp.value.status, 200)
        self.assertIn("Owner|keep", G.tags_of(self.path["w.MOV"])[0])
        for n in ("a.jpg", "b.png", "v.mp4"):
            self.assertNotIn("Owner|keep", G.tags_of(self.path[n])[0], n)

    # ------------------------------------------------------------ bulk tags, event and Rotate
    def test_bulk_tag_event_and_rotate_mixed(self):
        s = self.start()
        page = self.page_for(s)
        bar = self.bar(page)
        self.select_all_by_range(page)
        dlg = page.locator("[data-dialog=confirm]")
        bar.get_by_role("button", name="+ tag").click()
        dlg.get_by_label("Tag (yours").fill("Trips|Spain")
        with page.expect_response(lambda r: r.url.endswith("/api/tag")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(dlg).to_be_hidden()
        for n, p in self.path.items():
            h, dc, _ = G.tags_of(p)
            self.assertIn("Trips|Spain", h, n)
            self.assertIn("Spain", dc, n)
        # remove it from a photo and a video only
        page.keyboard.press("Escape")
        for n in ("a.jpg", "v.mp4"):
            self.box(page, n).click()
        bar.get_by_role("button", name="− tag").click()
        dlg.get_by_label("Tag (yours").fill("Trips|Spain")
        with page.expect_response(lambda r: r.url.endswith("/api/tag")) as resp:
            dlg.get_by_label("Tag (yours").press("Enter")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual([n for n, p in self.path.items() if "Trips|Spain" in G.tags_of(p)[0]], ["b.png", "w.MOV"])
        self.assertIn("Trips|coast", G.tags_of(self.path["v.mp4"])[0])  # the video's other tag of mine stays
        # Set event on the whole mixed grid
        page.keyboard.press("Escape")
        self.select_all_by_range(page)
        bar.get_by_role("button", name="Set event…").click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Set the event on 4 items?")
        dlg.get_by_label("Event name").fill("beach")
        with page.expect_response(lambda r: r.url.endswith("/api/event/set")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(page.locator('#evlist [data-ev="beach"]')).to_contain_text("4")
        for n, p in self.path.items():
            self.assertEqual([t for t in G.tags_of(p)[0] if t.startswith("Event|")], ["Event|beach"], n)
        # Rotate the mixed selection: every sidecar turned once, the thumbnails made again turned
        page.wait_for_function(THUMBS_LOADED)
        self.select_all_by_range(page)
        with page.expect_response(lambda r: r.url.endswith("/api/rotate")) as resp:
            bar.get_by_role("button", name="Rotate ⟳").click()
        self.assertEqual(resp.value.status, 200)
        for n, p in self.path.items():
            self.assertEqual(G.tags_of(p)[2], 6, n)
        for n in self.path:  # 4:3 frames, now taller than wide
            img = self.tile(page, n).locator("img")
            B.expect(img).to_have_attribute("src", re.compile(r"^/t/\d+\?r=\d+$"))
            img.evaluate("""i => new Promise((res, rej) => { setTimeout(() => rej(new Error("not loaded")), %d);
                const ok = () => i.complete && i.naturalHeight > i.naturalWidth;
                if (ok()) return res(); i.addEventListener("load", () => ok() ? res() : rej(new Error("not turned")),
                {once: true}) })""" % B.STEP_MS)
        self.assert_originals()

    # ------------------------------------------------------------ bulk Trash (the real intake.py)
    def test_bulk_trash_mixed(self):
        s = self.start()
        page = self.page_for(s)
        page.wait_for_function(THUMBS_LOADED)
        th = {n: os.path.join(self.thumbs, self.orig[n] + "-r0.jpg") for n in self.path}
        for n, t in th.items():
            self.assertTrue(os.path.isfile(t), n)
        for n in ("a.jpg", "w.MOV"):
            self.box(page, n).click()
        page.keyboard.press("t")
        dlg = page.locator("[data-dialog=confirm]")
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash 2 items?")
        B.expect(dlg).to_contain_text("a.jpg")
        B.expect(dlg).to_contain_text("w.MOV")
        dlg.get_by_role("button", name="Trash 2 files").click()
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(dlg.get_by_role("heading")).to_have_text("Done")
        B.expect(dlg.locator("pre")).to_contain_text("trashed 2 files")
        B.expect(dlg.locator("pre")).to_contain_text("done: 2 of 2 gone from the library")
        for n in ("a.jpg", "w.MOV"):
            p = self.path[n]
            for gone in (p, p + ".xmp", th[n]):
                self.assertFalse(os.path.lexists(gone), gone)
            B.expect(self.tile(page, n)).to_have_count(0)
        for n in ("b.png", "v.mp4"):
            self.assertTrue(os.path.isfile(th[n]), n)
        B.expect(dlg.get_by_role("status")).to_have_text("Done: 2 files trashed.")
        dlg.get_by_role("button", name="Close", exact=True).click()
        self.assertEqual(sorted(self.tiles(page)), ["b.png", "v.mp4"])
        B.expect(self.bar(page)).to_be_hidden()  # the trashed ones left the selection
        self.assert_originals()

    # ------------------------------------------------------------ the large view: each kind
    def test_large_view_plays_mp4_and_mov_and_rotates_a_video(self):
        s = self.start()
        page = self.page_for(s)
        viewer = page.get_by_role("dialog", name="Item")
        for n in ("v.mp4", "w.MOV"):
            self.tile(page, n).click()
            B.expect(viewer).to_have_attribute("data-current", n)
            video = viewer.locator("video")
            self.assertGreater(video.evaluate(B.PLAYING), 0, n)
            self.assertAlmostEqual(video.evaluate("v => v.duration"), 4, delta=0.2)
            self.assertEqual(video.evaluate("v => [v.videoWidth, v.videoHeight]"), [320, 240])
            page.keyboard.press("Escape")
            B.expect(viewer).to_be_hidden()
            B.expect(viewer.locator("video")).to_have_count(0)
        self.tile(page, "b.png").click()
        B.expect(viewer).to_have_attribute("data-current", "b.png")
        B.expect(viewer.locator("#media > img")).to_have_count(1)
        B.expect(viewer.locator("video")).to_have_count(0)
        page.keyboard.press("Escape")
        # R in the large view on a video: sidecar only, the player turned by the page
        self.tile(page, "v.mp4").click()
        B.expect(viewer).to_have_attribute("data-current", "v.mp4")
        with page.expect_response(lambda r: r.url.endswith("/api/rotate")) as resp:
            page.keyboard.press("r")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(G.tags_of(self.path["v.mp4"])[2], 6)
        video = viewer.locator("video")
        B.expect(video).to_have_attribute("data-rot", "90")
        self.assertIn("rotate(90deg)", video.evaluate("v => v.style.transform"))
        self.assertGreater(video.evaluate(B.PLAYING), 0)  # still plays
        self.assert_originals()


if __name__ == "__main__":
    unittest.main(verbosity=2)
