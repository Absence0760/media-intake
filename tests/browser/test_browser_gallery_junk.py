#!/usr/bin/env python3
"""The gallery's Suggested junk view in headless Chromium: the bar above its grid (Why, Junk score, "Show N
more with a lower junk score"), the tile's score and reason, the header count with Library filters still set,
and the Junk check's Judged junk count following its slider.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_junk.py

Hooks: body[data-ready], #junkbar, #jwhy, #jmin, #jlower, #count, #c-sug, [data-item] tiles and their .flag,
the Junk check filter's .opt[data-val=trash] and #aimin. Synthetic images in a throwaway HOME, sidecars written
as plain XMP; originals are compared by sha256 at the end. Marks with the view's filters: bulk marks then a
narrower Why or a higher Junk score (what Apply sends, and "N more not listed now"), a mark taken off by a star
and not back on unstar, and Suggested junk's tiles carrying no star or Share (a Share asked from Library
leaving the marks as they were). Apply's /api/decide is answered by the test (page.route), so nothing is
written or deleted by it; the server side of Apply is tests/test_gallery.py's. Every test fails on an uncaught
page error.
"""
import json, os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)

DESKTOP = dict(viewport={"width": 1440, "height": 900})
PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
LIBRARY = {  # name: (colour, tags)
    "hi.jpg": ((200, 40, 40), ["Project|Personal", "AI|vlm/m1/trash/blurry", "AI|vlm/m1/conf/0.9"]),
    "lo.jpg": ((40, 200, 40), ["Project|Personal", "AI|vlm/m1/trash/dark", "AI|vlm/m1/conf/0.4"]),
    "lab.jpg": ((40, 40, 200), ["Project|Personal", "Review|maybe-trash"]),
    "kept.jpg": ((90, 90, 90), ["Project|Personal", "AI|vlm/m1/trash/blurry", "AI|vlm/m1/conf/0.95", "Owner|keep"]),
    "plain.jpg": ((150, 150, 20), ["Project|Personal"]),
}


class GalleryJunkBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-junk-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        state = os.path.join(home, ".local/share/media-intake")
        lib = os.path.join(home, "Pictures/library")
        os.makedirs(state)
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=state, PHOTO_LIBRARY=lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.path = {}
        for n, (colour, tags) in LIBRARY.items():
            p = self.path[n] = os.path.join(lib, "2026/2026-10", n)
            G.img(p, colour)
            G.raw_xmp(p, tags)
        self.orig = {n: G.sha(p) for n, p in self.path.items()}
        self.addCleanup(self.assert_originals)
        self.errors = []
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def assert_originals(self):
        for n, p in self.path.items():
            self.assertEqual(G.sha(p), self.orig[n], f"{n} was written or deleted")

    def page(self, size):
        s = G.Server(self.env)
        self.addCleanup(s.stop)
        ctx = self.browser.new_context(**size)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, s)
        return page

    def shown(self, page):
        return page.locator("#list [data-item]")

    def junk_view(self, page):
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Suggested junk").click()
        B.expect(page.locator("#vtitle")).to_have_text("Suggested junk")

    def test_why_and_score_inside_the_view(self):
        page = self.page(DESKTOP)
        B.expect(page.locator("#c-sug")).to_have_text("2")
        B.expect(page.locator("#junkbar")).to_be_hidden()  # Library: no junk bar
        self.junk_view(page)
        bar = page.get_by_role("group", name="Narrow the suggestions")
        B.expect(bar).to_be_visible()
        # the usual line: the 0.9 verdict and the content label's tag; the kept one never, the 0.4 one below it
        B.expect(self.shown(page)).to_have_count(2)
        B.expect(page.locator('[data-item="hi.jpg"] .flag')).to_have_text("Junk score 0.90 (blurry)")
        B.expect(page.locator('[data-item="lab.jpg"] .flag')).to_have_text("Tagged as junk")
        B.expect(page.locator("#count")).to_have_text("2 items suggested")
        why = bar.get_by_label("Why")
        B.expect(why.locator("option")).to_have_text(["All reasons (2)", "Blurry (1)", "Tagged as junk (1)"])
        B.expect(bar.get_by_label("Junk score")).to_have_value("0.7")
        lower = bar.get_by_role("button", name="Show 1 more with a lower junk score")
        B.expect(lower).to_be_visible()
        # Why narrows the grid; All reasons puts it back
        why.select_option("blurry")
        B.expect(self.shown(page)).to_have_count(1)
        B.expect(page.locator('[data-item="hi.jpg"]')).to_be_visible()
        B.expect(page.locator("#count")).to_have_text("1 item suggested")
        why.select_option("")
        B.expect(self.shown(page)).to_have_count(2)
        # a lower score: the 0.4 verdict joins, last (most confident first); the button goes, the kept one stays out
        lower.click()
        B.expect(self.shown(page)).to_have_count(3)
        B.expect(bar.get_by_label("Junk score")).to_have_value("0")
        B.expect(page.locator("#jlower")).to_be_hidden()
        self.assertEqual([t.get_attribute("data-item") for t in self.shown(page).all()], ["hi.jpg", "lab.jpg", "lo.jpg"])
        B.expect(page.locator('[data-item="lo.jpg"] .flag')).to_have_text("Junk score 0.40 (too dark)")
        B.expect(page.locator('[data-item="kept.jpg"]')).to_have_count(0)
        B.expect(why.locator("option")).to_have_text(["All reasons (3)", "Blurry (1)", "Too dark (1)",
                                                      "Tagged as junk (1)"])
        # the Apply bar is still the view's only way to change anything
        B.expect(page.get_by_role("group", name="Decisions")).to_be_visible()
        # back in Library and in again: the usual line
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Library").click()
        B.expect(page.locator("#junkbar")).to_be_hidden()
        self.junk_view(page)
        B.expect(self.shown(page)).to_have_count(2)
        B.expect(bar.get_by_label("Junk score")).to_have_value("0.7")

    def test_library_filters_and_the_judged_junk_count(self):
        page = self.page(DESKTOP)
        page.locator('.fs[data-s=ai] .fs-h').evaluate("h => h.getAttribute('aria-expanded') === 'false' && h.click()")
        judged = page.locator('.opts[data-k=ai] .opt[data-val=trash]')
        B.expect(judged.locator(".ct")).to_have_text("2")  # 0.70 or more: hi and kept, what the grid lists
        judged.click()
        B.expect(self.shown(page)).to_have_count(2)
        page.locator("#aimin").evaluate("e => { e.value = '0.3'; e.dispatchEvent(new Event('change')) }")
        B.expect(judged.locator(".ct")).to_have_text("3")
        B.expect(self.shown(page)).to_have_count(3)
        page.locator("#aimin").evaluate("e => { e.value = '0.7'; e.dispatchEvent(new Event('change')) }")
        B.expect(judged.locator(".ct")).to_have_text("2")
        # the filter stays in Suggested junk, and the header says so
        self.junk_view(page)
        B.expect(self.shown(page)).to_have_count(1)
        B.expect(page.locator("#count")).to_have_text("2 suggested, 1 shown with your filters")
        B.expect(page.locator("#chips")).to_contain_text("Junk check")

    # ------------------------------------------------------------ marks and the view's filters
    def ids(self, page):
        return dict(page.evaluate("() => items.map(i => [i.n, i.id])"))

    def apply_sends(self, page):
        """Press Apply and confirm; the /api/decide body the page sent (answered here: nothing is trashed)."""
        page.route("**/api/decide", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps({"result": "ok", "trashed": []})))
        page.get_by_role("button", name="Apply…").click()
        with page.expect_request(lambda r: r.url.endswith("/api/decide")) as rq:
            page.locator("#mgo").click()
        page.unroute("**/api/decide")
        return rq.value.post_data_json

    def test_marks_hidden_by_the_filters_are_not_applied(self):
        page = self.page(DESKTOP)
        self.junk_view(page)
        bar = page.get_by_role("group", name="Narrow the suggestions")
        bar.get_by_role("button", name="Show 1 more with a lower junk score").click()
        B.expect(self.shown(page)).to_have_count(3)
        ids = self.ids(page)
        page.get_by_role("button", name="Mark all shown as Trash").click()
        sugn = page.locator("#sugn")
        B.expect(sugn).to_have_text("0 keep / 3 trash marked")
        bar.get_by_label("Why").select_option("blurry")  # only hi.jpg listed: the other two wait
        B.expect(self.shown(page)).to_have_count(1)
        B.expect(sugn).to_have_text("0 keep / 1 trash marked (2 more not listed now)")
        bar.get_by_label("Why").select_option("")
        B.expect(sugn).to_have_text("0 keep / 3 trash marked")
        bar.get_by_label("Junk score").select_option("0.7")  # lo.jpg (0.4) drops out of the list
        B.expect(self.shown(page)).to_have_count(2)
        B.expect(sugn).to_have_text("0 keep / 2 trash marked (1 more not listed now)")
        body = self.apply_sends(page)
        self.assertEqual((body["keep"], sorted(body["trash"])), ([], sorted([ids["hi.jpg"], ids["lab.jpg"]])))
        self.assertNotIn(ids["lo.jpg"], body["trash"])

    def test_a_star_takes_the_trash_mark_off_and_unstarring_leaves_it_off(self):
        page = self.page(DESKTOP)
        self.junk_view(page)
        B.expect(self.shown(page)).to_have_count(2)
        ids = self.ids(page)
        hi = page.locator('[data-item="hi.jpg"]')
        hi.locator("[data-dec=trash]").click()
        B.expect(hi.locator(".mark")).to_have_text("Trash")
        hi.locator("img").click()  # the large view: F stars it
        viewer = page.get_by_role("dialog", name="Item", exact=True)
        B.expect(viewer).to_have_attribute("data-current", "hi.jpg")
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")) as resp:
            page.keyboard.press("f")
        self.assertEqual(resp.value.status, 200)
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        B.expect(hi.locator(".mark")).to_have_count(0)
        B.expect(page.locator("#sugn")).to_have_text("0 keep / 0 trash marked")
        nav = page.get_by_role("navigation", name="Views")
        nav.get_by_role("button", name="Library").click()
        star = page.locator('[data-item="hi.jpg"] .star')
        B.expect(star).to_have_attribute("aria-pressed", "true")
        page.locator('[data-item="hi.jpg"]').hover()
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            star.click()  # unstarred: a suggestion again, with no mark
        B.expect(star).to_have_attribute("aria-pressed", "false")
        self.junk_view(page)
        B.expect(hi).to_be_visible()
        B.expect(hi.locator(".mark")).to_have_count(0)
        page.locator('[data-item="lab.jpg"] [data-dec=trash]').click()
        body = self.apply_sends(page)
        self.assertEqual(body["trash"], [ids["lab.jpg"]])

    def test_junk_tiles_carry_no_star_or_share_and_a_share_leaves_marks(self):
        page = self.page(DESKTOP)
        self.junk_view(page)
        B.expect(self.shown(page)).to_have_count(2)
        for sel in (".star", ".tshare"):  # Keep and Trash are a junk tile's only buttons
            B.expect(page.locator(f"#list [data-item] {sel}")).to_have_count(0)
        page.locator('[data-item="hi.jpg"] [data-dec=trash]').click()
        page.locator('[data-item="lab.jpg"] [data-dec=keep]').click()
        marks = lambda: dict(page.locator("[data-item] .mark").evaluate_all(  # noqa: E731
            "els => els.map(m => [m.closest('[data-item]').dataset.item, m.textContent])"))
        self.assertEqual(marks(), {"hi.jpg": "Trash", "lab.jpg": "Keep"})
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Library").click()
        t = page.locator('[data-item="hi.jpg"]')
        B.expect(t.locator(".star")).to_have_count(1)
        t.hover()
        t.get_by_role("button", name="Share hi.jpg").click()  # asks; cancelled, nothing made
        dlg = page.locator("#modal")
        B.expect(dlg.get_by_role("heading")).to_have_text("Share 1 file?")
        dlg.get_by_role("button", name="Cancel").click()
        self.junk_view(page)
        B.expect(page.locator('[data-item="hi.jpg"] .mark')).to_have_text("Trash")
        self.assertEqual(marks(), {"hi.jpg": "Trash", "lab.jpg": "Keep"})
        B.expect(page.locator("#sugn")).to_have_text("1 keep / 1 trash marked")

    def test_phone_bar_fits(self):
        page = self.page(PHONE)
        self.junk_view(page)
        B.expect(page.locator("#junkbar")).to_be_visible()
        B.expect(self.shown(page)).to_have_count(2)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                             page.evaluate("document.documentElement.clientWidth"), "sideways scroll at 390 px")
        for sel in ("#jwhy", "#jmin", "#jlower"):
            box = page.locator(sel).bounding_box()
            self.assertGreaterEqual(box["height"], 44, sel)
            self.assertLessEqual(box["x"] + box["width"], 390, sel)


if __name__ == "__main__":
    import unittest
    unittest.main()
