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
                keep = cover.get_by_role("button", name=re.compile(r"^Keep \("))
                B.expect(keep).to_have_attribute("aria-pressed", "true")
                self.assert_contrast(keep, f"{scheme}: pressed Keep")
                submit = page.get_by_role("button", name="Submit")
                B.expect(submit).to_be_enabled()
                self.assert_contrast(submit, f"{scheme}: Submit")
