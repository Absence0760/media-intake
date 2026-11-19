#!/usr/bin/env python3
"""Select all in the gallery, in headless Chromium: the header's Select all button, Ctrl+A and Cmd+A, a
closed burst stack contributing its cover only, a filter respected, Ctrl+A in a text field left to the
field, Esc clearing, Trash and Erase keeping their confirmations on a select-all selection, and a grid
that has loaded only its first page saying how many more match.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_select_all.py

The library is tests/test_gallery_bursts.py's BURST_LIBRARY in a throwaway HOME (a stack of three shots,
a near-copy and two shots of their own), with lone.jpg and early.jpg moved into an event by their
sidecars. No test here writes or deletes anything: Trash and Erase are cancelled at their confirmations,
and the originals' sha256 are checked unchanged. Every test also fails on an uncaught page error.
"""
import os, re, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)
import test_gallery_bursts as GB  # noqa: E402  (the burst library)

COVER, REST = "IMG_0001.jpg", ("IMG_0002.jpg", "IMG_0003.jpg")
SHOWN = sorted(["lone.jpg", "copy.jpg", COVER, "early.jpg"])  # the grid with the stack closed
PICNIC = ["early.jpg", "lone.jpg"]


class GallerySelectAllBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-select-all-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.path = GB.burst_library(self.lib, self.state)
        for n in PICNIC:
            G.raw_xmp(self.path[n], ["Project|Personal", "Event|picnic"])
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

    def selected(self, page):
        """The selected tiles' names, sorted (the event's sidecars carry no date, so grid order isn't asserted)."""
        return sorted(page.locator("[data-item].sel").evaluate_all("els => els.map(e => e.dataset.item)"))

    def bar(self, page):
        return page.get_by_role("toolbar", name="Selection actions")

    def assert_originals(self):
        for n, p in self.path.items():
            self.assertEqual(G.sha(p), self.orig[n], n)

    def test_button_and_keys_select_all_shown_with_a_closed_stack_as_its_cover(self):
        page = self.page()
        bar, ball = self.bar(page), page.get_by_role("button", name="Select all")
        B.expect(bar).to_be_hidden()
        B.expect(ball).to_be_visible()
        ball.click()
        B.expect(bar).to_contain_text("Selected 4 (all shown; closed stacks: cover only)")
        self.assertEqual(self.selected(page), SHOWN)  # the hidden shots are never selected
        page.keyboard.press("Escape")
        B.expect(bar).to_be_hidden()
        self.assertEqual(self.selected(page), [])
        # Ctrl+A from the page (focus on a tile), Cmd+A too
        self.tile(page, "lone.jpg").focus()
        page.keyboard.press("Control+a")
        B.expect(bar).to_contain_text("Selected 4 (all shown; closed stacks: cover only)")
        self.assertEqual(self.selected(page), SHOWN)
        page.keyboard.press("Escape")
        B.expect(bar).to_be_hidden()
        page.keyboard.press("Meta+a")
        B.expect(bar).to_contain_text("Selected 4 (all shown")
        page.keyboard.press("Escape")
        # an open stack: every shot is shown, so every shot is selected
        self.tile(page, COVER).locator(".x").click()
        for n in REST:
            B.expect(self.tile(page, n)).to_be_visible()
        page.keyboard.press("Control+a")
        B.expect(bar).to_contain_text("Selected 6 (all shown)")
        self.assertEqual(self.selected(page), sorted(GB.TIMELINE))
        # closing it unselects the hidden shots, as for any selection; the count is plain again
        self.tile(page, COVER).focus()
        page.keyboard.press("x")
        B.expect(bar).to_contain_text("4 selected")
        self.assertEqual(self.selected(page), SHOWN)
        # unticking one leaves a plain count, not "all shown"
        page.get_by_role("checkbox", name="select copy.jpg", exact=True).click()
        B.expect(bar).to_contain_text("3 selected")
        B.expect(bar).not_to_contain_text("all shown")
        self.assert_originals()

    def test_respects_the_filter(self):
        page = self.page()
        page.locator('#evlist [data-ev="picnic"]').click()
        B.expect(page.locator("#vtitle")).to_have_text("picnic")
        B.expect(page.locator("[data-item]")).to_have_count(2)
        page.get_by_role("button", name="Select all").click()
        B.expect(self.bar(page)).to_contain_text("Selected 2 (all shown)")
        self.assertEqual(self.selected(page), PICNIC)
        # clearing the filter reloads the grid and starts a new (empty) selection
        page.get_by_role("button", name="Library").click()
        B.expect(page.locator("[data-item]")).to_have_count(len(GB.TIMELINE))
        B.expect(self.bar(page)).to_be_hidden()

    def test_ctrl_a_in_a_text_field_stays_the_fields(self):
        page = self.page()
        q = page.locator("#q")
        q.fill("burst")
        q.focus()
        page.keyboard.press("Control+a")
        self.assertEqual(q.evaluate("i => [i.selectionStart, i.selectionEnd]"), [0, 5])  # the field's own
        B.expect(self.bar(page)).to_be_hidden()
        self.assertEqual(self.selected(page), [])

    def test_trash_and_erase_keep_their_confirmations(self):
        page = self.page()
        page.get_by_role("button", name="Select all").click()
        dlg = page.locator("[data-dialog=confirm]")
        page.keyboard.press("t")
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash 4 items?")
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        self.bar(page).get_by_role("button", name="Erase").click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Erase 4 items everywhere?")
        B.expect(dlg.get_by_label("Type ERASE")).to_be_visible()  # still typed, whatever the selection
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        B.expect(self.bar(page)).to_contain_text("Selected 4 (all shown")  # the selection stays
        for p in self.path.values():
            self.assertTrue(os.path.isfile(p), p)
        self.assert_originals()

    def test_a_paged_grid_says_how_many_more_match(self):
        for k in range(250):  # older than the burst library: the first page (240) ends before them
            G.img(os.path.join(self.lib, "2026/2026-10", f"many-{k:03d}.jpg"), (k, 40, 80))
        page = self.page()
        loaded, total = page.evaluate("() => [items.length, total]")
        self.assertEqual((loaded, total), (240, 256))
        page.keyboard.press("Control+a")
        bar = self.bar(page)
        B.expect(bar).to_contain_text(re.compile(r"Selected \d+ \(all shown; 16 more match below: scroll to load "
                                                 r"them, then Select all again; closed stacks: cover only\)"))
        n = page.locator("[data-item].sel").count()
        self.assertEqual(n, 240 - len(REST))  # every loaded tile but the closed stack's hidden shots
        B.expect(bar).to_contain_text(f"Selected {n} (all shown")
