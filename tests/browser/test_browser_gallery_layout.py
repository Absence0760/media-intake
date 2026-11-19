#!/usr/bin/env python3
"""The gallery's and the review page's layout in headless Chromium, at phone width (390 px) and just above
the phone breakpoint (800 px), in light and dark.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_layout.py

Gallery: no horizontal page scroll; the header's title shown and its controls inside the header; a burst stack's
badge and X button inside their tile and clear of its checkbox, and on a phone tile a compact "×N" button
that opens and closes the stack; the selection bar's long Select all label wrapping inside the bar; the last
row of tiles reachable above the selection bar; the burst help one line on a phone with "more" (remembered);
the large view's close button pinned at the top right of a 390x844 phone however far the view is
scrolled, and a downward swipe on the photo closing it (not a sideways or short one, nor one while scrolled down); and every
label on an accent colour, the key hints and the placeholders at 4.5:1 or more in both colour schemes.
The sidebar's width: its right edge (role=separator named "Sidebar width") dragged wider and narrower with the
grid following, clamped to 240 px .. min(560 px, half the window, the window less 480 px) and again when the window shrinks
(the header compacting and still fitting), kept
across a reload, a double-click putting the default 300 px back, Left / Right / Shift / Home / End on it, a
visible focus line in both schemes and in forced colours, no edge key with a modifier or while the shortcut
list is open, a bad stored width falling back or clamped, an interrupted drag (pointercancel, lost capture)
putting the width back, a right-button press not dragging, working with localStorage throwing, and no edge in
the collapsed rail or at phone width (rail or drawer); Keyboard shortcuts at the sidebar's foot, not in the Views nav among the
events, its list naming the Sidebar keys; a 64-character event slug and display name fitting a phone's drawer, title and
event bar; a tile's star and Share side by side at its bottom right, clear of each other and of a long flag (34 px
at 1440 px, 44 px taps on a phone); the search placeholder fitting its box at the default width, a tenth to spare.
Review page: the burst badge, a pressed Keep and Submit at 4.5:1 or more in both schemes.

The gallery's library is tests/test_gallery_bursts.py's BURST_LIBRARY in a throwaway HOME (a stack of three
shots, a near-copy and two shots of their own) plus 30 plain synthetic images, so the grid scrolls. The
review page's batch is three synthetic shots of one burst in quarantine, run by tests/test_review_ui.py's
fake intake.py (never started: nothing is submitted). Nothing is written or deleted. Every test also fails
on an uncaught page error.
"""
import os, re, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)
import test_gallery_bursts as GB  # noqa: E402  (the burst library)
import test_review_ui as R  # noqa: E402  (fixtures and the server wrapper only)

COVER, REST = "IMG_0001.jpg", ("IMG_0002.jpg", "IMG_0003.jpg")
PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
NARROW = dict(viewport={"width": 800, "height": 700})
DESKTOP = dict(viewport={"width": 1440, "height": 900})
SCHEMES = ("light", "dark")
SBW0, SBWMIN, SBWMAX = 300, 240, 560  # the sidebar's default and its clamp at 1440 px (min(560, 1440 / 2))
# The sidebar's and the main column's left edges and the first tile's, in CSS px.
SIDEBAR = """() => ({sidebar: document.querySelector('#sidebar').getBoundingClientRect().width,
  app: document.querySelector('#app').getBoundingClientRect().left,
  tile: document.querySelector('[data-item]').getBoundingClientRect().left})"""
# How wide the search box's placeholder is drawn, and the room its content box has.
PLACEHOLDER = """q => { const cs = getComputedStyle(q), c = document.createElement('canvas').getContext('2d');
  c.font = cs.font; return {text: c.measureText(q.placeholder).width,
  room: q.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)} }"""

# Boxes of the header's children and the page's scroll width, in CSS px.
GEOMETRY = """() => {
  const box = e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.width] };
  const top = document.querySelector('#top');
  return {scroll: document.documentElement.scrollWidth, width: document.documentElement.clientWidth,
          top: box(top), title: box(document.querySelector('#vtitle')),
          controls: [...top.querySelectorAll('button, input, label')].filter(e => !e.closest('[hidden]'))
            .map(e => [e.id || e.className, ...box(e)])}
}"""
# A tile's checkbox and its shown burst controls (badge, X, ×N), as [left, top, right, bottom].
TILE = """t => { const b = e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom] };
  const shown = e => e && getComputedStyle(e).display !== 'none';
  const out = {tile: b(t), ck: b(t.querySelector('.ck'))};
  for (const k of ['badge', 'x', 'bx']) { const e = t.querySelector('.' + k); if (shown(e)) out[k] = b(e) }
  return out }"""
# The contrast ratio (WCAG 2) of an element's text, or of its ::placeholder, over the backgrounds behind it:
# the element's and its ancestors' background colours composited, its colour composited over that with the
# opacity of it and its ancestors.
CONTRAST = """(e, pseudo) => {
  const parse = s => { let m = s.match(/^rgba?\\(([^)]+)\\)$/);
    if (m) { const p = m[1].split(/[\\s,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1] }
    m = s.match(/^color\\(srgb\\s+([^)]+)\\)$/);
    if (m) { const p = m[1].split(/[\\s/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1] }
    if (s === 'transparent') return [0, 0, 0, 0];
    throw new Error('colour not parsed: ' + s) };
  const over = (t, b) => { const a = t[3] + b[3] * (1 - t[3]);
    return [0, 1, 2].map(k => (t[k] * t[3] + b[k] * b[3] * (1 - t[3])) / a).concat([a]) };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]) };
  const chain = [];
  for (let n = e; n; n = n.parentElement) { const c = parse(getComputedStyle(n).backgroundColor); chain.push(c); if (c[3] >= 1) break }
  let bg = [255, 255, 255, 1];
  for (let i = chain.length - 1; i >= 0; i--) bg = over(chain[i], bg);
  let op = 1;
  for (let n = e; n; n = n.parentElement) op *= parseFloat(getComputedStyle(n).opacity);
  const fg = parse(getComputedStyle(e, pseudo || null).color);
  const c = over([fg[0], fg[1], fg[2], fg[3] * op], bg);
  const x = lum(c), y = lum(bg);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05) }"""


class LayoutCase(B.BrowserCase):
    def context_page(self, server, size, scheme="light"):
        ctx = self.browser.new_context(color_scheme=scheme, **size)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, server)
        return page

    def assert_contrast(self, loc, what, pseudo=None):
        with self.subTest(label=what):  # every label reports, not only the first below 4.5:1
            B.expect(loc).to_be_visible()
            ratio = loc.evaluate(CONTRAST, pseudo)
            self.assertGreaterEqual(ratio, 4.5, f"{what}: contrast {ratio:.2f}:1")


class GalleryLayoutBrowserTest(LayoutCase):
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

    def page(self, size, scheme="light"):
        return self.context_page(self.server, size, scheme)

    def cover(self, page):
        return page.locator(f'[data-item="{COVER}"]')

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
        b = self.cover(page).evaluate(TILE)
        tl, tt, tr, tb = b["tile"]
        shown = [k for k in ("badge", "x", "bx") if k in b]
        self.assertTrue(shown, f"{what}: no burst control shown on the cover")
        for k in shown:
            left, top, right, bottom = b[k]
            self.assertTrue(tl - 1 <= left and right <= tr + 1 and tt - 1 <= top and bottom <= tb + 1,
                            f"{what}: the {k} {b[k]} runs out of its tile {b['tile']}")
            ck = b["ck"]
            apart = right <= ck[0] or left >= ck[2] or bottom <= ck[1] or top >= ck[3]
            self.assertTrue(apart, f"{what}: the {k} {b[k]} covers the checkbox {ck}")
        return shown

    def assert_last_row_above_bar(self, page, what):
        page.evaluate("() => scrollTo(0, document.documentElement.scrollHeight)")
        last = page.locator("[data-item]").last
        B.expect(last).to_be_visible()
        tile = last.bounding_box()
        bar = page.locator("#actionbar").bounding_box()
        self.assertLessEqual(tile["y"] + tile["height"], bar["y"] + 1,
                             f"{what}: the last tile ends at {tile['y'] + tile['height']}, under the bar at {bar['y']}")

    def check_size(self, size, what, toggle):
        page = self.page(size)
        B.expect(page.locator("[data-item]")).to_have_count(len(GB.TIMELINE) + 30)
        self.assert_header_fits(page, what)
        self.assertIn(toggle, self.assert_stack_badge_fits(page, what))
        self.cover(page).locator("." + toggle).click()  # open: "Close stack (X)" is the longer label
        B.expect(page.locator(f'[data-item="{REST[0]}"]')).to_be_visible()
        self.assert_stack_badge_fits(page, f"{what}, stack open")
        self.cover(page).locator("." + toggle).click()
        B.expect(page.locator(f'[data-item="{REST[0]}"]')).to_be_hidden()
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
        self.check_size(PHONE, "390 px phone", "bx")

    def test_just_above_the_phone_breakpoint(self):
        self.check_size(NARROW, "800 px", "x")

    def test_phone_compact_burst_button(self):
        page = self.page(PHONE)
        cover = self.cover(page)
        bx = cover.get_by_role("button", name="Burst: 3 shots, open stack")
        B.expect(bx).to_be_visible()
        B.expect(bx).to_have_text("×3")
        B.expect(cover.locator(".x")).to_be_hidden()  # the long labels give way to it
        B.expect(cover.locator(".badge")).to_be_hidden()
        bx.tap()
        for n in REST:
            B.expect(page.locator(f'[data-item="{n}"]')).to_be_visible()
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()  # never opens the large view
        bx = cover.get_by_role("button", name="Burst: 3 shots, close stack")
        B.expect(bx).to_be_visible()
        B.expect(bx).to_have_text("Close")  # the cover's button says what a tap does now
        # an open stack's other shots show their place compactly ("2/3"); the full text stays for screen readers
        rest = page.locator(f'[data-item="{REST[0]}"] .badge')
        B.expect(rest).to_have_text("Burst: shot 2 of 3")
        self.assertEqual(rest.evaluate("e => getComputedStyle(e, '::before').content"), '"2/3"')
        self.assertLess(rest.bounding_box()["width"], 50)
        # one group: the cover and its shots share the ring
        for n in (COVER, *REST):
            B.expect(page.locator(f'[data-item="{n}"]')).to_have_class(re.compile(r"(^|\s)inb(\s|$)"))
        bx.tap()
        for n in REST:
            B.expect(page.locator(f'[data-item="{n}"]')).to_be_hidden()
        B.expect(cover.locator(".bx")).to_have_text("×3")
        B.expect(cover).not_to_have_class(re.compile(r"(^|\s)inb(\s|$)"))
        cover.focus()  # x still works
        page.keyboard.press("x")
        B.expect(page.locator(f'[data-item="{REST[0]}"]')).to_be_visible()
        B.expect(cover.get_by_role("button", name="Burst: 3 shots, close stack")).to_be_visible()

    def test_desktop_keeps_the_full_burst_labels(self):
        page = self.page(DESKTOP)
        cover = self.cover(page)
        B.expect(cover.locator(".badge")).to_be_visible()
        B.expect(cover.locator(".x")).to_have_text("Show all 3 (X)")
        B.expect(cover.locator(".bx")).to_be_hidden()

    def test_burst_help_is_one_line_on_a_phone(self):
        page = self.page(PHONE)
        helpb, more = page.locator("#helpb"), page.get_by_role("button", name="more", exact=True)
        B.expect(helpb.locator(".tch")).to_have_text("Burst stacks: tap ×N to open one.")  # no X key on a phone
        B.expect(helpb.locator(".kb")).to_be_hidden()
        B.expect(helpb.locator(".hfull")).to_be_hidden()
        self.assertLess(helpb.bounding_box()["height"], 40, "the phone help is more than one line")
        B.expect(more).to_have_attribute("aria-expanded", "false")
        more.click()
        B.expect(helpb.locator(".hfull")).to_be_visible()
        B.expect(helpb.locator(".hshort")).to_be_hidden()
        B.expect(page.get_by_role("button", name="less", exact=True)).to_have_attribute("aria-expanded", "true")
        page.reload()  # remembered in the browser
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        B.expect(helpb.locator(".hfull")).to_be_visible()
        page.get_by_role("button", name="less", exact=True).click()
        B.expect(helpb.locator(".hfull")).to_be_hidden()
        # the desktop keeps the full paragraph and no toggle
        desk = self.page(DESKTOP)
        B.expect(desk.locator("#helpb .hfull")).to_be_visible()
        B.expect(desk.locator("#hmore")).to_be_hidden()

    def open_lone(self, page):
        viewer = page.get_by_role("dialog", name="Item")
        page.locator('[data-item="lone.jpg"] img').click()
        B.expect(viewer).to_have_attribute("data-current", "lone.jpg")
        return viewer

    def test_phone_viewer_close_pinned_at_the_top(self):
        page = self.page(PHONE)
        viewer = self.open_lone(page)
        close = viewer.get_by_role("button", name="Close (Esc)")
        for scroll in ("0", "200", "v.scrollHeight"):  # the photo, part way, the info pane's end
            page.locator("#viewer").evaluate(f"v => {{ v.scrollTop = {scroll} }}")
            if scroll != "0":
                self.assertGreater(page.locator("#viewer").evaluate("v => v.scrollTop"), 0)
            box = close.bounding_box()
            self.assertTrue(0 <= box["y"] <= 16 and 390 - 16 <= box["x"] + box["width"] <= 390,
                            f"scrolled to {scroll}: the close button is not pinned at the top right: {box}")
            self.assertGreaterEqual(min(box["width"], box["height"]), 44, "a phone touch target")
            self.assertTrue(close.evaluate("b => { const r = b.getBoundingClientRect();"
                                           " return b.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }"),
                            f"scrolled to {scroll}: the close button is covered")
        self.assert_contrast(close.locator("svg"), "the pinned close icon")
        close.tap()
        B.expect(viewer).to_be_hidden()
        viewer = self.open_lone(page)
        self.assertEqual(page.locator("#viewer").evaluate("v => v.scrollTop"), 0, "reopened scrolled down")
        page.keyboard.press("Escape")  # Esc still closes it
        B.expect(viewer).to_be_hidden()

    def test_phone_viewer_swipe_down_closes(self):
        page = self.page(PHONE)
        viewer = self.open_lone(page)
        swipe = """([dx, dy]) => { const s = document.querySelector('#stage');
          const t = (x, y) => new Touch({identifier: 1, target: s, clientX: x, clientY: y});
          s.dispatchEvent(new TouchEvent('touchstart', {touches: [t(200, 200)], changedTouches: [t(200, 200)], bubbles: true}));
          s.dispatchEvent(new TouchEvent('touchend', {touches: [], changedTouches: [t(200 + dx, 200 + dy)], bubbles: true})) }"""
        page.evaluate(swipe, [150, 20])  # sideways: stays open
        B.expect(viewer).to_be_visible()
        page.evaluate(swipe, [10, 30])  # too short: stays open
        B.expect(viewer).to_be_visible()
        page.evaluate(swipe, [10, 160])
        B.expect(viewer).to_be_hidden()
        # scrolled down to the info pane, a downward swipe scrolls back rather than closing
        viewer = self.open_lone(page)
        page.locator("#viewer").evaluate("v => { v.scrollTop = 200 }")
        page.evaluate(swipe, [10, 160])
        B.expect(viewer).to_be_visible()

    # ------------------------------------------------------------ the sidebar's width
    def edge(self, page):
        return page.get_by_role("separator", name="Sidebar width")

    def drag_edge(self, page, to_x, up=True):
        box = self.edge(page).bounding_box()
        y = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + box["width"] / 2, y)
        page.mouse.down()
        page.mouse.move(to_x, y, steps=6)
        if up:
            page.mouse.up()

    def assert_width(self, page, w, what):
        B.expect(self.edge(page)).to_have_attribute("aria-valuenow", str(w))
        g = page.evaluate(SIDEBAR)
        self.assertAlmostEqual(g["sidebar"], w, delta=0.5, msg=f"{what}: the sidebar is {g['sidebar']} px")
        self.assertAlmostEqual(g["app"], w, delta=0.5, msg=f"{what}: the main column starts at {g['app']} px")
        self.assertGreaterEqual(g["tile"], w, f"{what}: the first tile at {g['tile']} px is under the sidebar")
        self.assert_no_hscroll(page, what)

    def test_sidebar_edge_drags_clamps_and_is_remembered(self):
        page = self.page(DESKTOP)
        edge = self.edge(page)
        B.expect(edge).to_be_visible()
        B.expect(edge).to_have_attribute("aria-orientation", "vertical")
        B.expect(edge).to_have_attribute("aria-valuemin", str(SBWMIN))
        B.expect(edge).to_have_attribute("aria-valuemax", str(SBWMAX))
        self.assertEqual(edge.evaluate("e => getComputedStyle(e).cursor"), "col-resize")
        self.assert_width(page, SBW0, "default")
        # mid-drag the sidebar and the grid follow the pointer, before the button is let go
        self.drag_edge(page, 420, up=False)
        B.expect(page.locator("body")).to_have_class(re.compile(r"(^|\s)sbdrag(\s|$)"))
        self.assert_width(page, 420, "mid-drag")
        page.mouse.up()
        B.expect(page.locator("body")).not_to_have_class(re.compile(r"(^|\s)sbdrag(\s|$)"))
        self.assert_width(page, 420, "dragged wider")
        self.drag_edge(page, 1300)
        self.assert_width(page, SBWMAX, "dragged past the widest")
        self.drag_edge(page, 40)
        self.assert_width(page, SBWMIN, "dragged past the narrowest")
        self.drag_edge(page, 380)
        self.assert_width(page, 380, "dragged back")
        page.reload()  # remembered in the browser
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        self.assert_width(page, 380, "after a reload")
        # a smaller window clamps it to half its width; a bigger one gives the chosen width back
        self.drag_edge(page, SBWMAX)
        self.assert_width(page, SBWMAX, "widest")
        page.set_viewport_size({"width": 1000, "height": 900})
        B.expect(edge).to_have_attribute("aria-valuemax", "500")
        self.assert_width(page, 500, "a 1000 px window")
        self.assert_header_fits(page, "a 1000 px window, the widest sidebar")  # the header compacts, the title stays
        page.set_viewport_size({"width": 800, "height": 900})  # 800 - 480: the main column keeps 480 px
        B.expect(edge).to_have_attribute("aria-valuemax", "320")
        self.assert_width(page, 320, "an 800 px window")
        self.assert_header_fits(page, "an 800 px window, the widest sidebar")
        page.set_viewport_size(DESKTOP["viewport"])
        self.assert_width(page, SBWMAX, "the window wide again")
        # a double-click puts the default back, remembered too
        edge.dblclick()
        self.assert_width(page, SBW0, "double-clicked")
        page.reload()
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        self.assert_width(page, SBW0, "default after a reload")

    def test_sidebar_edge_keys_and_focus_line(self):
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                page = self.page(DESKTOP, scheme)
                edge = self.edge(page)
                # the line's colour once its fade-in (a CSS transition) has finished: a real signal, no sleep
                line = ("async e => { await Promise.all(e.getAnimations({subtree: true}).map(a => a.finished));"
                        " return getComputedStyle(e, '::after').backgroundColor }")
                self.assertEqual(edge.evaluate(line), "rgba(0, 0, 0, 0)", "the line shows before hover or focus")
                edge.focus()
                for key, w in (("ArrowRight", 316), ("Shift+ArrowRight", 380), ("ArrowLeft", 364),
                               ("Home", SBWMIN), ("ArrowLeft", SBWMIN), ("End", SBWMAX), ("ArrowRight", SBWMAX)):
                    page.keyboard.press(key)
                    self.assert_width(page, w, f"{scheme}: {key}")
                B.expect(edge).to_be_focused()  # the keys resized, not moved the grid's focus
                B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()
                acc = page.evaluate("() => { const d = document.createElement('div'); d.style.color = 'var(--acc)';"
                                    " document.body.append(d); const c = getComputedStyle(d).color; d.remove(); return c }")
                self.assertEqual(edge.evaluate(line), acc, f"{scheme}: no accent focus line on the edge")
                self.assertGreaterEqual(edge.evaluate("e => parseFloat(getComputedStyle(e, '::after').width)"), 3)
                page.reload()
                B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
                self.assert_width(page, SBWMAX, f"{scheme}: kept after a reload")
                edge.dblclick()
                self.assert_width(page, SBW0, f"{scheme}: reset")

    def test_sidebar_edge_ignores_modified_keys_and_keys_behind_a_dialog(self):
        page = self.page(DESKTOP)
        edge = self.edge(page)
        edge.focus()
        for key in ("Control+ArrowRight", "Meta+ArrowRight", "Alt+ArrowRight", "Control+ArrowLeft", "Alt+ArrowLeft"):
            page.keyboard.press(key)
            self.assert_width(page, SBW0, f"{key} on the edge")
        page.keyboard.press("?")  # the shortcut list opens over the page
        keys = page.get_by_role("dialog", name="Keyboard shortcuts")
        B.expect(keys).to_be_visible()
        edge.evaluate("e => e.focus()")  # focus left on the edge behind it
        for key in ("ArrowRight", "Shift+ArrowRight", "End", "Home"):
            page.keyboard.press(key)
            self.assert_width(page, SBW0, f"{key} while the shortcut list is open")
        B.expect(keys).to_be_visible()
        page.keyboard.press("Escape")
        B.expect(keys).to_be_hidden()
        edge.focus()
        page.keyboard.press("ArrowRight")  # and acts again once it is closed
        self.assert_width(page, SBW0 + 16, "ArrowRight after the list closed")

    def test_sidebar_edge_bad_stored_widths(self):
        for stored, want in (("abc", SBW0), ("100", SBW0), ("9999", SBW0), ("600", SBWMAX), ("", SBW0)):
            with self.subTest(stored=stored):
                ctx = self.browser.new_context(**DESKTOP)
                ctx.set_default_timeout(B.STEP_MS)
                self.addCleanup(ctx.close)
                ctx.add_init_script(f"try {{ localStorage.setItem('gallery.sbw', {stored!r}) }} catch (e) {{}}")
                page = ctx.new_page()
                page.on("pageerror", lambda e: self.errors.append(str(e)))
                self.open(page, self.server)
                self.assert_width(page, want, f"stored {stored!r}")

    def test_sidebar_edge_interrupted_drag_puts_the_width_back(self):
        page = self.page(DESKTOP)
        edge = self.edge(page)
        drag = re.compile(r"(^|\s)sbdrag(\s|$)")
        page.evaluate("() => document.querySelector('#sbresize').addEventListener('pointerdown',"
                      " e => { window.__pid = e.pointerId })")
        for how in ("pointercancel", "lost capture"):
            with self.subTest(how=how):
                self.drag_edge(page, 450, up=False)
                B.expect(page.locator("body")).to_have_class(drag)
                self.assert_width(page, 450, f"{how}: mid-drag")
                if how == "pointercancel":
                    edge.evaluate("e => e.dispatchEvent(new PointerEvent('pointercancel', {pointerId: window.__pid, bubbles: true}))")
                else:
                    edge.evaluate("e => e.releasePointerCapture(window.__pid)")
                # the pointer goes on moving: the browser delivers a released capture's lostpointercapture before
                # the pointer's next event (Pointer Events, "process pending pointer capture"), not at once
                page.mouse.move(500, 300)
                B.expect(page.locator("body")).not_to_have_class(drag)
                self.assert_width(page, SBW0, f"{how}: put back")
                page.mouse.up()
                self.assert_width(page, SBW0, f"{how}: after the button is let go")
        page.reload()  # the interrupted width was never kept
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        self.assert_width(page, SBW0, "after a reload")
        # a right-button press on the edge starts no drag
        box = edge.bounding_box()
        y = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + box["width"] / 2, y)
        page.mouse.down(button="right")
        page.mouse.move(450, y, steps=6)
        B.expect(page.locator("body")).not_to_have_class(drag)
        self.assert_width(page, SBW0, "right-button drag")
        page.mouse.up(button="right")
        page.keyboard.press("Escape")  # a context menu, if any

    def test_sidebar_edge_line_in_forced_colours(self):
        page = self.page(DESKTOP)
        page.emulate_media(forced_colors="active")
        edge = self.edge(page)
        edge.focus()
        page.keyboard.press("ArrowRight")
        hl = page.evaluate("() => { const d = document.createElement('div'); d.style.cssText = 'forced-color-adjust:none;color:Highlight';"
                           " document.body.append(d); const c = getComputedStyle(d).color; d.remove(); return c }")
        got = edge.evaluate("async e => { await Promise.all(e.getAnimations({subtree: true}).map(a => a.finished));"
                            " return getComputedStyle(e, '::after').backgroundColor }")
        self.assertEqual(got, hl, "no Highlight line on the focused edge in forced colours")

    def test_sidebar_edge_without_storage(self):
        ctx = self.browser.new_context(**DESKTOP)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        ctx.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('storage blocked') }})")
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, self.server)
        self.assert_width(page, SBW0, "no storage")
        self.drag_edge(page, 450)
        self.assert_width(page, 450, "no storage, dragged")
        page.reload()
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        self.assert_width(page, SBW0, "no storage, after a reload")

    def test_sidebar_edge_absent_in_the_rail_and_on_a_phone(self):
        page = self.page(DESKTOP)
        edge = self.edge(page)
        B.expect(edge).to_be_visible()
        page.locator("#rail").click()  # collapsed to the rail
        B.expect(page.locator("#rail")).to_have_attribute("aria-expanded", "false")
        B.expect(edge).to_be_hidden()
        self.assertAlmostEqual(page.evaluate(SIDEBAR)["sidebar"], 60, delta=0.5)
        page.locator("#rail").click()
        B.expect(edge).to_be_visible()
        phone = self.page(PHONE)
        B.expect(self.edge(phone)).to_be_hidden()  # the phone's rail
        phone.locator("#rail").tap()
        B.expect(phone.locator("#rail")).to_have_attribute("aria-expanded", "true")
        B.expect(self.edge(phone)).to_be_hidden()  # the phone's drawer

    def test_long_event_names_fit_a_phone(self):
        """A 64-character slug and display name, neither with a place to break: the sidebar row, the title and
        the event bar (name and slug) stay inside a 390 px phone, with no sideways scroll."""
        slug, name = "trip2024" * 8, "W" * 64
        G.img(os.path.join(self.env["PHOTO_LIBRARY"], "2026/2026-08/ev.jpg"), (10, 90, 90))
        G.raw_xmp(os.path.join(self.env["PHOTO_LIBRARY"], "2026/2026-08/ev.jpg"), [f"Event|{slug}"])
        s = G.Server(self.env)
        self.addCleanup(s.stop)
        code, j = s.post("/api/event/name", {"event": slug, "name": name})
        self.assertEqual(code, 200, j)
        page = self.context_page(s, PHONE)
        page.locator("#rail").tap()
        row = page.locator(f'#evlist [data-ev="{slug}"]')
        B.expect(row.locator(".lbl")).to_have_text(name)
        self.assert_no_hscroll(page, "390 px, the drawer's event row")
        box, side = row.bounding_box(), page.locator("#sidebar").bounding_box()
        self.assertLessEqual(box["x"] + box["width"], side["x"] + side["width"] + 1, "the event row runs out of the drawer")
        row.tap()
        B.expect(page.locator("#vtitle")).to_have_text(name)
        bar = page.get_by_role("toolbar", name="Event", exact=True)
        B.expect(bar.locator("#evslug")).to_have_text(slug)
        self.assert_header_fits(page, "390 px, an event with a long display name")
        width = page.evaluate("() => document.documentElement.clientWidth")
        for sel in ("#evname", "#evslug"):
            b = bar.locator(sel).bounding_box()
            self.assertLessEqual(b["x"] + b["width"], width + 1, f"{sel} runs out of the page")

    def test_tile_star_and_share_side_by_side(self):
        """A tile's star and Share sit side by side in its bottom-right corner, apart from each other and from a
        long flag, inside the tile: 34 px with a mouse at 1440 px (shown on hover), 44 px taps on a touch phone."""
        p = os.path.join(self.env["PHOTO_LIBRARY"], "2026/2026-08/flagged.jpg")
        G.img(p, (10, 90, 90))
        G.raw_xmp(p, ["Used|post-1", "Review|maybe-trash", "Review|child-in-shot"])  # a flag long enough to clip
        s = G.Server(self.env)
        self.addCleanup(s.stop)
        box = lambda loc: (lambda b: [b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]])(loc.bounding_box())  # noqa: E731
        apart = lambda a, b: a[2] <= b[0] + 0.5 or b[2] <= a[0] + 0.5 or a[3] <= b[1] + 0.5 or b[3] <= a[1] + 0.5  # noqa: E731
        for size, what, side in ((DESKTOP, "1440 px", 34), (PHONE, "390 px phone", 44)):
            with self.subTest(size=what):
                page = self.context_page(s, size)
                t = page.locator('[data-item="flagged.jpg"]')
                t.scroll_into_view_if_needed()
                if size is DESKTOP:
                    t.hover()
                star, share, flag = t.locator(".star"), t.get_by_role("button", name="Share flagged.jpg"), t.locator(".flag")
                for loc in (star, share, flag):
                    B.expect(loc).to_be_visible()
                B.expect(share).to_have_css("opacity", "1")
                B.expect(star).to_have_css("opacity", "1")
                tb, sb, hb, fb = box(t), box(star), box(share), box(flag)
                for name, b in (("star", sb), ("Share", hb), ("flag", fb)):
                    self.assertTrue(tb[0] - 1 <= b[0] and b[2] <= tb[2] + 1 and tb[1] - 1 <= b[1] and b[3] <= tb[3] + 1,
                                    f"{what}: the {name} {b} runs out of its tile {tb}")
                self.assertTrue(apart(sb, hb), f"{what}: the star {sb} overlaps Share {hb}")
                self.assertLess(sb[2], hb[0] + 0.5, f"{what}: the star is not left of Share")
                self.assertTrue(apart(fb, sb) and apart(fb, hb), f"{what}: the flag {fb} runs under the star {sb} or Share {hb}")
                for name, b in (("star", sb), ("Share", hb)):
                    self.assertAlmostEqual(b[2] - b[0], side, delta=0.5, msg=f"{what}: the {name}'s width")
                    self.assertAlmostEqual(b[3] - b[1], side, delta=0.5, msg=f"{what}: the {name}'s height")

    def test_keyboard_shortcuts_at_the_sidebar_foot(self):
        page = self.page(DESKTOP)
        entry = page.get_by_role("button", name="Keyboard shortcuts")
        B.expect(entry).to_be_visible()
        B.expect(page.get_by_role("navigation", name="Views").get_by_role("button", name="Keyboard shortcuts")).to_have_count(0)
        B.expect(page.locator("#evsec #bkeys")).to_have_count(0)
        # after the search box and the filters, at the bottom of the sidebar
        self.assertTrue(entry.evaluate("b => !!(document.querySelector('#filters').compareDocumentPosition(b)"
                                       " & Node.DOCUMENT_POSITION_FOLLOWING) && b.closest('#sidebar') !== null"))
        # it opens the shortcut list, which names the sidebar's keys; Esc closes it and focus comes back
        entry.click()
        keys = page.get_by_role("dialog", name="Keyboard shortcuts")
        B.expect(keys).to_be_visible()
        B.expect(keys.get_by_role("heading", name="Sidebar", exact=True)).to_be_visible()
        B.expect(keys).to_contain_text("On the sidebar's edge")
        page.keyboard.press("Escape")
        B.expect(keys).to_be_hidden()
        B.expect(entry).to_be_focused()
        page.locator("#rail").click()  # the rail keeps its icon
        B.expect(entry).to_be_visible()

    def test_search_placeholder_fits_at_the_default_width(self):
        for size in (DESKTOP, NARROW):
            with self.subTest(width=size["viewport"]["width"]):
                page = self.page(size)
                self.assert_width(page, SBW0, "default")
                m = page.locator("#q").evaluate(PLACEHOLDER)
                # with a tenth to spare: a wider system font (a Flatpak browser's own) must not cut it either
                self.assertLessEqual(m["text"], 0.9 * m["room"], f"the placeholder is cut, or nearly: {m}")

    def test_label_contrast_in_both_schemes(self):
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                page = self.page(DESKTOP, scheme)
                self.assert_contrast(self.cover(page).locator(".badge"), f"{scheme}: burst badge")
                self.assert_contrast(page.locator("#q"), f"{scheme}: search placeholder", "::placeholder")
                page.locator(f'[data-item="{COVER}"] .ck').click()
                for b in ("bkeep", "btrash", "berase"):
                    self.assert_contrast(page.locator(f"#{b} kbd"), f"{scheme}: #{b} key hint")
                page.keyboard.press("Escape")
                # the rail's filter count: a Kind filter on, then the sidebar collapsed to its rail
                page.get_by_role("group", name="Kind").get_by_role("button", name="Photos").click()
                page.locator("#rail").click()
                self.assert_contrast(page.locator("#rbadge"), f"{scheme}: rail filter count")
                page.locator("#rail").click()
                page.locator('[data-item="lone.jpg"] img').click()
                tag = page.locator("#info input[placeholder]").first
                self.assert_contrast(tag, f"{scheme}: tag placeholder", "::placeholder")
                phone = self.page(PHONE, scheme)
                self.assert_contrast(self.cover(phone).locator(".bx"), f"{scheme}: phone ×N button")


class ReviewLayoutBrowserTest(LayoutCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-layout-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        state = os.path.join(home, ".local/share/media-intake")
        os.makedirs(home)
        fake = os.path.join(self.tmp, "fake_intake.py")
        with open(fake, "w") as f:
            f.write(R.FAKE)
        env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=state, MEDIA_REVIEW_INTAKE=fake,
                   FAKE_LOG=os.path.join(self.tmp, "argv.log"))
        env.pop("FAKE_GATE", None)
        for k in ("PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG"):
            env[k] = os.path.join(home, k.lower())
        quar = os.path.join(state, "quarantine", R.BATCH)
        lib = os.path.join(home, "photo_library/2026/2026-11")
        q, plan = {}, []
        for n in range(1, 4):
            rel = f"DCIM/Camera/IMG_000{n}.jpg"
            R.img(os.path.join(quar, rel), (60 * n, 30, 30))
            q[rel] = {"reasons": ["document:id-card"], "sha": f"{n}" * 64, "size": 1, "dst": f"{lib}/IMG_000{n}.jpg"}
            plan.append({"rel": rel, "action": "file", "why": "", "date": f"2026-11-08T07:15:0{n - 1}.400000",
                         "device": "cam-a", "dhash": 0})
        R.write_json(os.path.join(quar, "quarantine.json"), q)
        R.write_json(os.path.join(state, "batches", R.BATCH, "plan.json"), plan)
        self.server = R.Server(env)
        self.addCleanup(self.server.stop)
        self.errors = []
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def test_label_contrast_in_both_schemes(self):
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                page = self.context_page(self.server, PHONE, scheme)
                B.expect(page.locator('meta[name="color-scheme"]')).to_have_attribute("content", "light dark")
                cover = page.locator('[data-item="DCIM/Camera/IMG_0001.jpg"]')
                self.assert_contrast(cover.locator(".badge"), f"{scheme}: burst badge")
                cover.locator(".burst .x").click()
                cover.get_by_role("button", name="Keep this one, erase the rest").click()  # decides the stack
                # on a touch phone the key hint " (K)" is hidden, so the name is just "Keep"
                keep = cover.get_by_role("button", name=re.compile(r"^Keep( \(K\))?$"))
                B.expect(keep).to_have_attribute("aria-pressed", "true")
                self.assert_contrast(keep, f"{scheme}: pressed Keep")
                submit = page.get_by_role("button", name="Submit")
                B.expect(submit).to_be_enabled()
                self.assert_contrast(submit, f"{scheme}: Submit")
