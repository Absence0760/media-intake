#!/usr/bin/env python3
"""The gallery's layout at phone width (390 px) and just above the phone breakpoint (800 px), in headless
Chromium: no horizontal page scroll, the header's title shown and its controls inside the header, a burst
stack's badge and X button inside their tile and clear of its checkbox, the selection bar's long Select all
label wrapping inside the bar, and the last row of tiles reachable above the selection bar.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_layout.py

The library is tests/test_gallery_bursts.py's BURST_LIBRARY in a throwaway HOME (a stack of three shots, a
near-copy and two shots of their own) plus 30 plain synthetic images, so the grid scrolls. Nothing is written
or deleted. Every test also fails on an uncaught page error.
"""
import os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)
import test_gallery_bursts as GB  # noqa: E402  (the burst library)

COVER = "IMG_0001.jpg"
PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
NARROW = dict(viewport={"width": 800, "height": 700})

# Boxes of the header's children and the page's scroll width, in CSS px.
GEOMETRY = """() => {
  const box = e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.width] };
  const top = document.querySelector('#top');
  return {scroll: document.documentElement.scrollWidth, width: document.documentElement.clientWidth,
          top: box(top), title: box(document.querySelector('#vtitle')),
          controls: [...top.querySelectorAll('button, input, label')].filter(e => !e.closest('[hidden]'))
            .map(e => [e.id || e.className, ...box(e)])}
}"""
# A tile's checkbox, badge and X button boxes [left, top, right, bottom].
TILE = """t => { const b = e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom] };
  return {tile: b(t), ck: b(t.querySelector('.ck')), badge: b(t.querySelector('.badge')), x: b(t.querySelector('.x'))} }"""


class GalleryLayoutBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-layout-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        state = os.path.join(home, ".local/share/media-intake")
        lib = os.path.join(home, "Pictures/library")
        os.makedirs(state)
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=state, PHOTO_LIBRARY=lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        GB.burst_library(lib, state)
        for k in range(30):  # older than the burst library: enough rows that the grid scrolls under the bar
            G.img(os.path.join(lib, "2026/2026-10", f"plain-{k:02d}.jpg"), (8 * k, 120, 90))
        self.server = G.Server(self.env)
        self.addCleanup(self.server.stop)
        self.errors = []
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def page(self, size):
        ctx = self.browser.new_context(**size)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, self.server)
        return page

    def assert_no_hscroll(self, page, what):
        g = page.evaluate(GEOMETRY)
        self.assertLessEqual(g["scroll"], g["width"], f"{what}: the page scrolls sideways")
        return g

    def assert_header_fits(self, page, what):
        g = self.assert_no_hscroll(page, what)
        left, right, _ = g["top"]
        self.assertGreater(g["title"][2], 20, f"{what}: the title is squeezed out of the header")
        for name, cl, cr, cw in g["controls"]:
            self.assertGreater(cw, 0, (what, name))
            self.assertTrue(left - 1 <= cl and cr <= right + 1, f"{what}: {name} {cl}..{cr} outside the header {left}..{right}")

    def assert_stack_badge_fits(self, page, what):
        b = self.page_tile(page).evaluate(TILE)
        tl, tt, tr, tb = b["tile"]
        for k in ("badge", "x"):
            left, top, right, bottom = b[k]
            self.assertTrue(tl - 1 <= left and right <= tr + 1 and tt - 1 <= top and bottom <= tb + 1,
                            f"{what}: the {k} {b[k]} runs out of its tile {b['tile']}")
            ck = b["ck"]
            apart = right <= ck[0] or left >= ck[2] or bottom <= ck[1] or top >= ck[3]
            self.assertTrue(apart, f"{what}: the {k} {b[k]} covers the checkbox {ck}")

    def page_tile(self, page):
        return page.locator(f'[data-item="{COVER}"]')

    def assert_last_row_above_bar(self, page, what):
        page.evaluate("() => scrollTo(0, document.documentElement.scrollHeight)")
        last = page.locator("[data-item]").last
        B.expect(last).to_be_visible()
        tile = last.bounding_box()
        bar = page.locator("#actionbar").bounding_box()
        self.assertLessEqual(tile["y"] + tile["height"], bar["y"] + 1,
                             f"{what}: the last tile ends at {tile['y'] + tile['height']}, under the bar at {bar['y']}")

    def check_size(self, size, what):
        page = self.page(size)
        B.expect(page.locator("[data-item]")).to_have_count(len(GB.TIMELINE) + 30)
        self.assert_header_fits(page, what)
        self.assert_stack_badge_fits(page, what)
        self.page_tile(page).locator(".x").click()  # open: "Close stack (X)" is the longer label
        B.expect(self.page_tile(page).locator(".x")).to_have_text("Close stack (X)")
        self.assert_stack_badge_fits(page, f"{what}, stack open")
        self.page_tile(page).locator(".x").click()
        # Select all on a closed stack: the bar's longest everyday label
        page.get_by_role("button", name="Select all").click()
        bar = page.get_by_role("toolbar", name="Selection actions")
        B.expect(bar).to_contain_text("closed stacks: cover only")
        self.assert_no_hscroll(page, f"{what}, Select all")
        seln, box = page.locator("#seln").bounding_box(), page.locator("#actionbar").bounding_box()
        self.assertLessEqual(seln["x"] + seln["width"], box["x"] + box["width"] + 1, f"{what}: the label runs out of the bar")
        self.assertGreaterEqual(seln["x"], box["x"] - 1, f"{what}: the label runs out of the bar")
        self.assert_last_row_above_bar(page, f"{what}, Select all")

    def test_phone_width(self):
        self.check_size(PHONE, "390 px phone")

    def test_just_above_the_phone_breakpoint(self):
        self.check_size(NARROW, "800 px")
