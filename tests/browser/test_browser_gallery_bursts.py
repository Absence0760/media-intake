#!/usr/bin/env python3
"""Burst stacks in the gallery, in headless Chromium: the cover and its count badge, X and `x` to open and
close, focus back on the cover, a closed stack's actions on the cover only, the large view stepping over
hidden shots, and a near-copy from the same second never stacked.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_bursts.py

The library is tests/test_gallery_bursts.py's BURST_LIBRARY in a throwaway HOME: three shots of one burst
(IMG_0001-0003, from the plan.json intake would have written), a near-copy of the same second (copy.jpg),
and two shots of their own (lone.jpg, newest; early.jpg, oldest). Keep runs the real gallery endpoint and
the test reads the sidecars back; originals are never written. Every test also fails on an uncaught page
error.
"""
import os, re, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)
import test_gallery_bursts as GB  # noqa: E402  (the burst library)

COVER, REST = "IMG_0001.jpg", ("IMG_0002.jpg", "IMG_0003.jpg")
STACK = re.compile(r"(^|\s)stack(\s|$)")  # the closed stack's look on the cover


class GalleryBurstsBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-bursts-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.path = GB.burst_library(lib, self.state)
        self.orig = {n: G.sha(p) for n, p in self.path.items()}
        self.servers = []
        self.errors = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def page(self):
        s = G.Server(self.env)
        self.servers.append(s)
        page = self.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, s)
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))
        return page

    def tile(self, page, n):
        return page.locator(f'[data-item="{n}"]')

    def visible(self, page):
        return page.locator("[data-item]").evaluate_all("els => els.filter(e => !e.hidden).map(e => e.dataset.item)")

    def bar(self, page):
        return page.get_by_role("toolbar", name="Selection actions")

    def assert_closed(self, page):
        B.expect(self.tile(page, COVER)).to_have_class(STACK)
        B.expect(self.tile(page, COVER).locator(".x")).to_have_text("Show all 3 (X)")
        for n in REST:
            B.expect(self.tile(page, n)).to_be_hidden()
        self.assertEqual(self.visible(page), ["lone.jpg", "copy.jpg", COVER, "early.jpg"])

    def assert_open(self, page):
        B.expect(self.tile(page, COVER).locator(".x")).to_have_text("Close stack (X)")
        for n in REST:
            B.expect(self.tile(page, n)).to_be_visible()
        B.expect(self.tile(page, COVER)).not_to_have_class(STACK)
        self.assertEqual(self.visible(page), GB.TIMELINE)

    def test_cover_badge_x_and_focus(self):
        page = self.page()
        # every tile is in the page in timeline order, the burst's shots right after their cover
        self.assertEqual(page.locator("[data-item]").evaluate_all("els => els.map(e => e.dataset.item)"),
                         GB.TIMELINE)
        self.assert_closed(page)
        B.expect(page.locator("#helpb")).to_be_visible()
        bd = self.tile(page, COVER).locator(".badge")
        B.expect(bd).to_have_text("Burst: 3 shots")
        B.expect(bd).to_have_attribute("data-count", "3")
        # a near-copy from the same second, and the shots of their own: no badge, no X, x does nothing
        for n in ("copy.jpg", "lone.jpg", "early.jpg"):
            B.expect(self.tile(page, n).locator(".badge, .x")).to_have_count(0)
        self.tile(page, "copy.jpg").focus()
        page.keyboard.press("x")
        B.expect(self.tile(page, "copy.jpg")).to_be_focused()
        self.assert_closed(page)
        # x on the focused cover opens the stack; focus stays on the cover
        self.tile(page, COVER).focus()
        page.keyboard.press("x")
        self.assert_open(page)
        B.expect(self.tile(page, COVER)).to_be_focused()
        for n in REST:  # each shot of an open stack carries the badge, only the cover the X
            B.expect(self.tile(page, n).locator(".badge")).to_have_text("Burst: 3 shots")
            B.expect(self.tile(page, n).locator(".x")).to_have_count(0)
        # X (upper case) on another shot closes it, and focus goes back to the cover
        self.tile(page, REST[1]).focus()
        page.keyboard.press("X")
        self.assert_closed(page)
        B.expect(self.tile(page, COVER)).to_be_focused()
        # the cover's button opens it too, focus on the cover
        self.tile(page, COVER).locator(".x").click()
        self.assert_open(page)
        B.expect(self.tile(page, COVER)).to_be_focused()
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()  # the button never opens the large view
        self.tile(page, COVER).locator(".x").click()
        self.assert_closed(page)

    def test_closed_stack_acts_on_the_cover_only(self):
        page = self.page()
        bar = self.bar(page)
        # a shot selected in an open stack is unselected when the stack closes: nothing hidden is acted on
        self.tile(page, COVER).locator(".x").click()
        self.tile(page, REST[0]).focus()
        page.keyboard.press(" ")
        B.expect(bar).to_contain_text("1 selected")
        self.tile(page, COVER).focus()
        page.keyboard.press("x")
        self.assert_closed(page)
        B.expect(bar).to_be_hidden()
        B.expect(page.locator("#toast")).to_contain_text("1 hidden shot unselected")
        # a shift-click range over the closed stack takes its cover, not the hidden shots
        page.get_by_role("checkbox", name="select lone.jpg", exact=True).click()
        page.get_by_role("checkbox", name="select early.jpg", exact=True).click(modifiers=["Shift"])
        B.expect(bar).to_contain_text("4 selected")
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            page.keyboard.press("k")
        self.assertEqual(resp.value.status, 200)
        B.expect(page.locator("#toast")).to_contain_text("kept 4:")
        for n in ("lone.jpg", "copy.jpg", COVER, "early.jpg"):
            self.assertIn("Owner|keep", G.tags_of(self.path[n])[0], n)
        for n in REST:
            self.assertNotIn("Owner|keep", G.tags_of(self.path[n])[0], n)
        B.expect(self.tile(page, COVER).locator(".flag")).to_have_text("kept")
        B.expect(self.tile(page, COVER).locator(".badge")).to_have_text("Burst: 3 shots")  # still a stack
        B.expect(self.tile(page, COVER)).to_have_class(STACK)
        for n, p in self.path.items():
            self.assertEqual(G.sha(p), self.orig[n], n)

    def test_large_view_steps_over_a_closed_stack(self):
        page = self.page()
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, COVER).click()
        B.expect(viewer).to_have_attribute("data-current", COVER)
        page.keyboard.press("ArrowRight")  # the hidden shots are skipped
        B.expect(viewer).to_have_attribute("data-current", "early.jpg")
        page.keyboard.press("ArrowLeft")
        B.expect(viewer).to_have_attribute("data-current", COVER)
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        B.expect(self.tile(page, COVER)).to_be_focused()
        page.keyboard.press("x")  # open: now Next goes through every shot
        self.assert_open(page)
        self.tile(page, COVER).click()
        B.expect(viewer).to_have_attribute("data-current", COVER)
        for n in (*REST, "early.jpg"):
            page.keyboard.press("ArrowRight")
            B.expect(viewer).to_have_attribute("data-current", n)
