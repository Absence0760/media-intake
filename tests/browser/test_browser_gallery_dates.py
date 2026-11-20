#!/usr/bin/env python3
"""The gallery's Dates to check view and Fix date, in headless Chromium.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_dates.py

The library is tests/test_gallery_dates.py's (a GoPro shot on the clock-reset default between numbered
neighbours, a phone photo dated before the phone was sold, a plain photo) plus a photo whose name holds HTML.
The real check-dates writes the report; the sidebar entry opens the view, its count follows each fix, the tiles
are grouped by reason with the suggested date, and Fix date (the suggestion, or a date typed in) writes the
sidecar only, with Undo putting it back. The name is shown as text, never as markup; at phone width neither the
view nor the dialog scrolls sideways. Every test fails on an uncaught page error, and checks the originals'
sha256 unchanged.
"""
import os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (the server wrapper only)
import test_gallery_dates as GD  # noqa: E402  (the library, and check-dates over it)
from test_datecheck import side_date  # noqa: E402

ODD = "<b>odd&amp;<b> x.JPG"  # a legal Linux file name (no slash), which must show as text
NO_HSCROLL = "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
INSIDE = """sel => { const r = document.querySelector(sel).getBoundingClientRect();
  return [r.left, r.right, document.documentElement.clientWidth] }"""


class GalleryDatesBrowserTest(B.BrowserCase):
    def setUp(self):
        if not shutil.which("exiftool"):
            B.unavailable("exiftool is not installed")
        self.tmp = tempfile.mkdtemp(prefix="gallery-dates-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        state = os.path.join(home, ".local/share/media-intake")
        self.lib = os.path.join(home, "Pictures/library")
        os.makedirs(state, mode=0o700)
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"), TZ="UTC",
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.path = GD.dates_library(self.lib, extra=(ODD,))
        self.orig = {p: G.sha(p) for p in [*self.path["shots"], self.path["era"], self.path["plain"], self.path[ODD]]}
        GD.run_check(self, self.env)
        self.server = G.Server(self.env)
        self.addCleanup(self.server.stop)
        self.errors = []
        self.addCleanup(self.unchanged)
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def unchanged(self):
        for p, h in self.orig.items():
            self.assertEqual(G.sha(p), h, f"{p} was written")

    def page(self, size=None):
        ctx = self.browser.new_context(**(size or {}))
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, self.server)
        return page

    def open_view(self, page):
        page.locator('.nv[data-v="dates"]').click()
        B.expect(page.locator("#vtitle")).to_have_text("Dates to check")
        B.expect(page.locator('.nv[data-v="dates"]')).to_have_attribute("aria-current", "page")

    def tile(self, page, name):
        return page.locator(".tile").filter(has=page.locator(f'img[alt="{name}"]'))

    def select(self, page, name):
        self.tile(page, name).locator(".ckw").click()

    def test_view_groups_fix_and_undo(self):
        page = self.page()
        B.expect(page.locator("#c-dates")).to_have_text("3")
        self.open_view(page)
        heads = page.locator("#list h2.month")
        B.expect(heads).to_have_text(["Clock-reset default date", "Camera newer than its date"])
        B.expect(page.locator("#count")).to_have_text("3 items")
        B.expect(self.tile(page, "GOPR0003.JPG").locator(".flag")).to_have_text("Suggested 2024-05-12 10:20")
        B.expect(self.tile(page, "DSC_0001.JPG").locator(".flag")).to_have_text("No suggested date")
        self.assertIn("GoPro", self.tile(page, "GOPR0003.JPG").get_attribute("title"))
        B.expect(page.locator("#note")).to_contain_text("From check-dates on")
        self.select(page, "GOPR0003.JPG")
        B.expect(page.locator("#bfixdate")).to_be_visible()
        page.locator("#bfixdate").click()
        B.expect(page.locator("#modal")).to_have_class("modal on")
        B.expect(page.locator("#mh")).to_have_text("Fix the date of 1 file?")
        B.expect(page.locator("#fix-suggested")).to_be_checked()
        B.expect(page.locator(".fixlist li")).to_have_text(["GOPR0003.JPG: 2016-01-01 00:01 → 2024-05-12 10:20 suggested"])
        page.locator("#mgo").click()
        B.expect(page.locator("#toast")).to_contain_text("Fixed the date of 1 file")
        B.expect(page.locator("#c-dates")).to_have_text("2")
        B.expect(self.tile(page, "GOPR0003.JPG")).to_have_count(0)
        self.assertEqual(side_date(self.path["reset"]), "2024:05:12 10:20:00")
        page.locator("#tundo").click()
        B.expect(page.locator("#toast")).to_contain_text("undid Fix date on 1")
        B.expect(page.locator("#c-dates")).to_have_text("3")
        B.expect(self.tile(page, "GOPR0003.JPG")).to_have_count(1)
        self.assertIsNone(side_date(self.path["reset"]))

    def test_set_a_typed_date_and_the_button_only_in_this_view(self):
        page = self.page()
        self.select(page, "plain.jpg")
        B.expect(page.locator("#selgrp")).to_be_visible()
        B.expect(page.locator("#bfixdate")).to_be_hidden()
        page.keyboard.press("Escape")
        self.open_view(page)
        self.select(page, "DSC_0001.JPG")
        page.locator("#bfixdate").click()
        B.expect(page.locator("#fix-suggested")).to_be_disabled()
        B.expect(page.locator("#fix-set")).to_be_checked()
        page.locator("#fixv-set").fill("next week")
        page.locator("#mgo").click()
        B.expect(page.locator("#mout")).to_contain_text("Date wants YYYY-MM-DD")
        self.assertIsNone(side_date(self.path["era"]), "a refused date writes nothing")
        page.locator("#fixv-set").fill("2024-01-02 08:00")
        page.locator("#mgo").click()
        B.expect(page.locator("#toast")).to_contain_text("Fixed the date of 1 file")
        self.assertEqual(side_date(self.path["era"]), "2024:01:02 08:00:00")
        B.expect(page.locator("#c-dates")).to_have_text("2")

    def test_names_are_text_and_phone_width_fits(self):
        page = self.page(dict(viewport={"width": 390, "height": 844}))
        self.open_view(page)
        t = self.tile(page, ODD)
        B.expect(t).to_have_count(1)
        self.assertEqual(page.locator("#list b").count(), 0, "a file name never becomes markup")
        scroll, width = page.evaluate(NO_HSCROLL)
        self.assertLessEqual(scroll, width, "the view scrolls sideways at phone width")
        t.locator(".ckw").click()
        page.locator("#bfixdate").click()
        B.expect(page.locator(".fixlist li")).to_have_text([f"{ODD}: 2019-07-01 12:00 → no suggestion"])
        self.assertEqual(page.locator("#mb b").count(), 0, "nor in the dialog")
        page.locator("#fix-shift").check()
        page.locator("#fixv-shift").fill("+5y")
        scroll, width = page.evaluate(NO_HSCROLL)
        self.assertLessEqual(scroll, width, "the dialog scrolls sideways at phone width")
        left, right, vw = page.evaluate(INSIDE, "#modal .box")
        self.assertTrue(0 <= left and right <= vw, (left, right, vw))
        for sel in ("#fixv-set", "#fixv-shift"):
            left, right, vw = page.evaluate(INSIDE, sel)
            self.assertTrue(0 <= left and right <= vw, (sel, left, right, vw))
        page.locator("#mgo").click()
        B.expect(page.locator("#toast")).to_contain_text("Fixed the date of 1 file")
        self.assertEqual(side_date(self.path[ODD]), "2024:07:01 12:00:00")


if __name__ == "__main__":
    import unittest
    unittest.main()
