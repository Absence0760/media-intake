#!/usr/bin/env python3
"""The gallery's grid in headless Chromium: Suggested junk by tap and click, phone touch targets, the grid's
own states, the phone's sidebar rail.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_grid.py

Suggested junk: every tile carries Keep and Trash buttons (aria-pressed; a second tap unmarks), a mark in words
on the tile, a tap or click on the photo opens the large view, no checkboxes and no selection bar there, the
view's note under the title, and Apply keeping what was marked (the sidecar read back; nothing deleted). On a
390 px phone with touch, every control the grid shows (rail, header, tile checkbox, a tile's Share, ×N, "more", the selection
bar, Keep / Trash, Apply, a filter chip's ×, the dialog's buttons) takes a 44 x 44 px tap: the points 21 px
from its centre each way all land on it. The states: loading (the list request held by the test, so no sleep),
an empty library naming the commands that fill it, no results naming what to clear (a search, a filter, an
empty quick view), Find similar without the search index, an item gone since the list loaded, and a load
error with Try again. The rail: a word under each icon and no Keyboard shortcuts item on a touch screen.

Synthetic images in a throwaway HOME (tests/test_gallery_bursts.py's BURST_LIBRARY plus two more suggested as
junk); originals are compared by sha256 at the end. Every test also fails on an uncaught page error.
"""
import os, re, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)
import test_gallery_bursts as GB  # noqa: E402  (the burst library)

PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
DESKTOP = dict(viewport={"width": 1440, "height": 900})
COVER = "IMG_0001.jpg"
JUNK = ("junk-a.jpg", "junk-b.jpg")
PRESSED = "aria-pressed"

# The points 21 px from an element's centre (left, right, up, down) and 15 px each way (the corners, inside a
# rounded or round 44 px target) that don't land on it or inside it: [] for a 44 x 44 px target (its own box, or
# a hit area drawn by a wrapper or ::after).
MISSES = """e => { e.scrollIntoView({block: 'center', inline: 'center'});
  const r = e.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2, d = 21, c = 15;
  const pts = [[-d, 0], [d, 0], [0, -d], [0, d], [-c, -c], [c, -c], [-c, c], [c, c]].map(([x, y]) => [cx + x, cy + y]);
  return pts.filter(([x, y]) => { const h = document.elementFromPoint(x, y); return !(h && (h === e || e.contains(h))) })
    .map(p => p.map(Math.round)) }"""


class GalleryGridBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-grid-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        self.state = os.path.join(home, ".local/share/media-intake")
        self.lib = os.path.join(home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.errors = []
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def library(self):
        self.path = GB.burst_library(self.lib, self.state)
        for k, n in enumerate(JUNK):
            p = self.path[n] = os.path.join(self.lib, "2026/2026-10", n)
            G.img(p, (200, 40 + 60 * k, 40))
            G.raw_xmp(p, ["Project|Personal", "Review|maybe-trash"])
        self.orig = {n: G.sha(p) for n, p in self.path.items()}
        self.addCleanup(self.assert_originals)

    def assert_originals(self):
        for n, p in self.path.items():
            self.assertEqual(G.sha(p), self.orig[n], f"{n} was written or deleted")

    def server(self):
        s = G.Server(self.env)
        self.addCleanup(s.stop)
        return s

    def page(self, s, size, ready=True):
        ctx = self.browser.new_context(**size)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        if ready:
            self.open(page, s)
        return page

    def url(self, s):
        return f"http://127.0.0.1:{s.port}/?t={s.token}"

    def tile(self, page, n):
        return page.locator(f'[data-item="{n}"]')

    def grid_state(self, page):
        return page.locator("#list .state")

    def views(self, page):
        return page.get_by_role("navigation", name="Views")

    def assert_target(self, loc, what):
        with self.subTest(target=what):
            B.expect(loc).to_be_visible()
            self.assertEqual(loc.evaluate(MISSES), [], f"{what}: a tap 21 px from its centre misses it")

    # ------------------------------------------------------------ Suggested junk
    def test_suggested_junk_by_tap_on_a_phone(self):
        self.library()
        page = self.page(self.server(), PHONE)
        self.views(page).get_by_role("button", name="Suggested junk").tap()  # the rail
        B.expect(page.locator("#vtitle")).to_have_text("Suggested junk")
        B.expect(page.locator("[data-item]")).to_have_count(4)
        # the view says what it is and that nothing changes until Apply (not overwritten by the list's note)
        B.expect(page.locator("#note")).to_have_text(re.compile(r"^Photos the tools suggest as junk.*Nothing changes until you Apply\.$"))
        # marks only: no checkbox, no selection bar
        B.expect(page.locator("[data-item] .ck")).to_have_count(0)
        B.expect(page.get_by_role("toolbar", name="Selection actions")).to_be_hidden()
        bar = page.get_by_role("group", name="Decisions")
        B.expect(bar).to_contain_text("Mark Keep or Trash on each, then Apply")
        B.expect(bar.locator(".kbonly")).to_be_hidden()  # no key hints on a touch screen
        B.expect(bar.get_by_role("button", name="Apply…")).to_be_disabled()
        a, b = (self.tile(page, n).get_by_role("group", name=f"Mark {n}") for n in JUNK)
        a.get_by_role("button", name="Keep").tap()
        B.expect(a.get_by_role("button", name="Keep")).to_have_attribute(PRESSED, "true")
        B.expect(a.get_by_role("button", name="Trash")).to_have_attribute(PRESSED, "false")
        B.expect(self.tile(page, JUNK[0]).locator(".mark")).to_have_text("Keep")  # in words, not colour alone
        b.get_by_role("button", name="Trash").tap()
        B.expect(self.tile(page, JUNK[1]).locator(".mark")).to_have_text("Trash")
        B.expect(bar).to_contain_text("1 keep / 1 trash marked")
        b.get_by_role("button", name="Trash").tap()  # a second tap unmarks
        B.expect(b.get_by_role("button", name="Trash")).to_have_attribute(PRESSED, "false")
        B.expect(self.tile(page, JUNK[1]).locator(".mark")).to_have_count(0)
        B.expect(bar).to_contain_text("1 keep / 0 trash marked")
        # a tap on the photo opens it
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, JUNK[1]).locator("img").tap()
        B.expect(viewer).to_have_attribute("data-current", JUNK[1])
        viewer.get_by_role("button", name="Close (Esc)").tap()
        B.expect(viewer).to_be_hidden()
        B.expect(bar).to_contain_text("1 keep / 0 trash marked")  # the marks survive the large view
        # Apply: keeps what was marked, nothing deleted
        bar.get_by_role("button", name="Apply…").tap()
        dlg = page.locator("[data-dialog=confirm]")
        B.expect(dlg.get_by_role("heading")).to_have_text("Apply: 1 keep, 0 trash?")
        with page.expect_response(lambda r: r.url.endswith("/api/decide")) as resp:
            dlg.get_by_role("button", name="Apply", exact=True).tap()
        self.assertEqual(resp.value.status, 200)
        B.expect(page.locator("[data-item]")).to_have_count(3)
        with open(self.path[JUNK[0]] + ".xmp") as f:
            x = f.read()
        self.assertIn("Owner|keep", x)
        self.assertNotIn("Review|maybe-trash", x)

    def test_suggested_junk_by_mouse_and_keys(self):
        self.library()
        page = self.page(self.server(), DESKTOP)
        self.views(page).get_by_role("button", name="Suggested junk").click()
        B.expect(page.locator("[data-item]")).to_have_count(4)
        B.expect(page.locator("#note")).to_be_visible()
        B.expect(page.get_by_role("group", name="Decisions").locator(".kbonly")).to_be_visible()  # a keyboard: K, T
        t = self.tile(page, JUNK[0])
        t.get_by_role("button", name="Trash").click()
        B.expect(t.get_by_role("button", name="Trash")).to_have_attribute(PRESSED, "true")
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()  # a button never opens the large view
        # K on the outlined tile (the one just marked) changes the mark; the buttons follow
        page.keyboard.press("k")
        B.expect(t.get_by_role("button", name="Keep")).to_have_attribute(PRESSED, "true")
        B.expect(t.get_by_role("button", name="Trash")).to_have_attribute(PRESSED, "false")
        # Enter on a focused button presses it and opens nothing
        btn = t.get_by_role("button", name="Keep")
        btn.focus()
        page.keyboard.press("Enter")
        B.expect(btn).to_have_attribute(PRESSED, "false")
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()
        # Shift+click selects nothing here; a click on the photo opens it
        t.locator("img").click(modifiers=["Shift"])
        B.expect(page.get_by_role("toolbar", name="Selection actions")).to_be_hidden()
        page.keyboard.press("Escape")
        self.tile(page, JUNK[1]).locator("img").click()
        B.expect(page.get_by_role("dialog", name="Item")).to_have_attribute("data-current", JUNK[1])

    # ------------------------------------------------------------ touch targets
    def test_phone_touch_targets(self):
        self.library()
        page = self.page(self.server(), PHONE)
        self.assert_target(page.locator("#rail"), "the sidebar button")
        for b in page.locator("#views .nv:visible").all():
            self.assert_target(b, "rail: " + (b.get_attribute("title") or ""))
        self.assert_target(page.locator("#ball"), "Select all")
        self.assert_target(page.locator("#close"), "Stop server")
        self.assert_target(page.locator("#tsize"), "the size slider")
        self.assert_target(self.tile(page, "lone.jpg").locator(".ckw"), "a tile's checkbox")
        self.assert_target(self.tile(page, "lone.jpg").get_by_role("button", name="Share lone.jpg"), "a tile's Share")
        self.assert_target(self.tile(page, COVER).locator(".bx"), "the ×N button")
        self.assert_target(page.locator("#hmore"), "the burst help's more")
        # a tap beside the 22 px box, inside the hit area, still ticks it
        w = self.tile(page, "lone.jpg").locator(".ckw")
        box = w.bounding_box()
        page.touchscreen.tap(box["x"] + 3, box["y"] + 3)
        B.expect(self.tile(page, "lone.jpg").locator(".ck")).to_be_checked()
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()
        sel = page.get_by_role("toolbar", name="Selection actions")
        B.expect(sel).to_be_visible()
        for b in sel.get_by_role("button").all():
            self.assert_target(b, "selection bar: " + b.inner_text())
        # Erase sits apart from Trash
        tr, er = page.locator("#btrash").bounding_box(), page.locator("#berase").bounding_box()
        if abs(tr["y"] - er["y"]) < 1:
            self.assertGreaterEqual(er["x"] - (tr["x"] + tr["width"]), 16, "Erase right beside Trash")
        page.locator("#bnone").tap()
        # a filter chip's ×
        page.locator("#rail").tap()
        page.locator("#q").fill("lone")
        page.locator("#scrim").tap(position={"x": 370, "y": 400})
        chip = page.get_by_role("button", name="Remove Search: lone")
        self.assert_target(chip, "a chip's ×")
        chip.tap()
        # Suggested junk's buttons and the Apply dialog
        self.views(page).get_by_role("button", name="Suggested junk").tap()
        B.expect(page.locator("[data-item]")).to_have_count(4)
        t = self.tile(page, JUNK[0])
        for name in ("Keep", "Trash"):
            self.assert_target(t.get_by_role("button", name=name), "Suggested junk: " + name)
        t.get_by_role("button", name="Keep").tap()
        self.assert_target(page.locator("#sugapply"), "Apply…")
        page.locator("#sugapply").tap()
        for b in ("#mno", "#mgo"):
            self.assert_target(page.locator(b), "dialog " + b)
        page.locator("#mno").tap()

    # ------------------------------------------------------------ the grid's states
    def test_loading_then_the_grid(self):
        self.library()
        s = self.server()
        page = self.page(s, DESKTOP, ready=False)
        held = []
        page.route("**/api/list*", lambda r: held.append(r))  # held until the test lets it through
        page.goto(self.url(s))
        st = self.grid_state(page)
        B.expect(st).to_have_text("Loading your library…")
        B.expect(st).to_have_attribute("role", "status")
        B.expect(page.locator("#main")).to_have_attribute("aria-busy", "true")
        for _ in range(200):  # each evaluate round trip delivers the routed request, if it has come
            if held:
                break
            page.evaluate("0")
        self.assertEqual(len(held), 1, "the list request was not held")
        held[0].continue_()
        page.unroute("**/api/list*")
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        B.expect(st).to_have_count(0)
        B.expect(page.locator("#main")).not_to_have_attribute("aria-busy", "true")
        B.expect(page.locator("[data-item]")).to_have_count(len(GB.TIMELINE) + len(JUNK))

    def test_load_error_says_what_to_do(self):
        self.library()
        s = self.server()
        page = self.page(s, DESKTOP, ready=False)
        page.route("**/api/list*", lambda r: r.fulfill(status=500, content_type="application/json",
                                                         body='{"error": "the list broke"}'))
        page.goto(self.url(s))
        err = page.locator("#list").get_by_role("alert")
        B.expect(err.get_by_role("heading")).to_have_text("Couldn't load the library")
        B.expect(err).to_contain_text("the list broke.")
        B.expect(err).to_contain_text("restart media-intake gallery")
        B.expect(page.locator("#note")).to_be_hidden()  # no bare "Error: ..." line
        page.unroute("**/api/list*")
        err.get_by_role("button", name="Try again").click()
        B.expect(page.locator("[data-item]")).to_have_count(len(GB.TIMELINE) + len(JUNK))
        B.expect(err).to_have_count(0)

    def test_empty_library_names_the_commands(self):
        os.makedirs(self.lib)
        page = self.page(self.server(), DESKTOP)
        st = self.grid_state(page)
        B.expect(st.get_by_role("heading")).to_have_text("Your library is empty.")
        B.expect(st.locator("pre")).to_have_text(re.compile(r"^media-intake stage <folder> --kind folder --batch first\n"
                                                            r"media-intake plan first\nmedia-intake scan first\n"
                                                            r"media-intake apply first$"))
        B.expect(st).to_contain_text("Then reload this page.")

    def test_no_results_say_what_to_clear(self):
        self.library()
        page = self.page(self.server(), DESKTOP)
        st = self.grid_state(page)
        n = len(GB.TIMELINE) + len(JUNK)
        # a search
        page.locator("#q").fill("zzzz")
        B.expect(st.get_by_role("heading")).to_have_text("Nothing matches this search.")
        st.get_by_role("button", name="Clear search").click()
        B.expect(page.locator("[data-item]")).to_have_count(n)
        B.expect(page.locator("#q")).to_have_value("")
        # a filter
        page.get_by_role("group", name="Kind").get_by_role("button", name="Videos").click()
        B.expect(st.get_by_role("heading")).to_have_text("Nothing matches these filters.")
        st.get_by_role("button", name="Clear filters").click()
        B.expect(page.locator("[data-item]")).to_have_count(n)
        # an empty quick view
        self.views(page).get_by_role("button", name="Untagged").click()
        B.expect(st.get_by_role("heading")).to_have_text("Nothing in Untagged right now.")
        st.get_by_role("button", name="Show the whole library").click()
        B.expect(page.locator("[data-item]")).to_have_count(n)
        # Find similar without the search index says how to build it, and goes back
        self.tile(page, "lone.jpg").locator("img").click()
        page.get_by_role("dialog", name="Item").get_by_role("button", name="Find similar").click()
        B.expect(st.get_by_role("heading")).to_have_text(
            "Find similar needs the search index: run media-intake index in a terminal, then try again.")
        B.expect(page.locator("#note")).to_be_hidden()  # said once
        st.get_by_role("button", name="Back to the library").click()
        B.expect(page.locator("[data-item]")).to_have_count(n)

    def test_suggested_junk_empty(self):
        os.makedirs(os.path.join(self.lib, "2026/2026-11"))
        G.img(os.path.join(self.lib, "2026/2026-11/plain.jpg"), (10, 120, 10))
        page = self.page(self.server(), DESKTOP)
        self.views(page).get_by_role("button", name="Suggested junk").click()
        B.expect(self.grid_state(page).get_by_role("heading")).to_have_text("No suggested junk right now.")
        B.expect(page.locator("#note")).to_contain_text("Nothing changes until you Apply.")

    def test_item_gone_since_the_list_loaded(self):
        self.library()
        page = self.page(self.server(), DESKTOP)
        page.route("**/api/item/*", lambda r: r.fulfill(status=404, body="404\n"))
        with page.expect_request(lambda r: "/api/list" in r.url):  # the list refreshes
            self.tile(page, "lone.jpg").locator("img").click()
        B.expect(page.locator("#toast")).to_have_text("That file is no longer in the library. The list will refresh.")
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()

    # ------------------------------------------------------------ the phone's rail
    def test_phone_rail_words_and_no_keyboard_item(self):
        self.library()
        page = self.page(self.server(), PHONE)
        views = self.views(page)
        for name, word in (("Library", "Library"), ("Suggested junk", "Junk"), ("Recently added", "Recent"),
                           ("Possible child", "Child"), ("Untagged", "No tags")):
            b = views.get_by_role("button", name=name)
            B.expect(b.locator(".rl")).to_have_text(word)
            B.expect(b.locator(".rl")).to_be_visible()
            box = b.locator(".rl").bounding_box()
            rail = b.bounding_box()
            self.assertLessEqual(box["x"] + box["width"], rail["x"] + rail["width"] + 1, f"{word} runs out of the rail")
        B.expect(page.locator("#bkeys")).to_be_hidden()  # no keyboard on a phone
        # the drawer shows the full names, not the rail's words
        page.locator("#rail").tap()
        B.expect(views.get_by_role("button", name="Suggested junk").locator(".lbl")).to_be_visible()
        B.expect(views.get_by_role("button", name="Suggested junk").locator(".rl")).to_be_hidden()
        B.expect(page.locator("#bkeys")).to_be_hidden()
        # a desktop keeps the shortcut list's entry
        desk = self.page(self.server(), DESKTOP)
        B.expect(desk.locator("#bkeys")).to_be_visible()
