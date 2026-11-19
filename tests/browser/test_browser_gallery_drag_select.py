#!/usr/bin/env python3
"""Drag to select and faster junk marking in the gallery, in headless Chromium.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_drag_select.py

Library: a mouse drag from a photo draws a rectangle (#marq) and selects every tile shown that it touches, a
closed burst stack giving its cover only (an open one every shot); Shift and Ctrl add to the selection, a plain
drag replaces it; a press that barely moves is still a click (the large view opens), a press on the checkbox,
the star or the Share button never starts a drag, a touch pointer never does (it scrolls), Esc during a drag puts the selection back, and a
drag held at the window's bottom edge scrolls the grid, loads the next page and takes in its tiles.

Suggested junk: a Shift-click on a tile's Trash or Keep marks the range from the last tile given that mark (a tile
marked the other way keeps its mark), Shift+T / Shift+K the same from the keys, a drag from a mark marks what its
rectangle touches (Esc puts the marks back), Mark all shown as Trash and Clear marks (which asks), and Apply still
asking with the thumbnails and names. Marks survive a trip to another view and back, the page asks before an
unload while marks wait, entering the view puts focus on the first tile (Enter there opens it, not the view
again), K / T with no outline outline the first tile or take the one under the pointer, and in the large view K
and T mark (no Keep is written, no Trash confirmation opens) and step to the next item.

Hooks: data-item and data-n on tiles, .sel, .cur, .mark, #marq, #selsay (role=status), #sugn, the confirm
dialog's data-dialog=confirm and data-current on the large view. Synthetic images in a throwaway HOME
(tests/test_gallery_bursts.py's BURST_LIBRARY plus some suggested as junk); no test writes or deletes a file,
checked by sha256 and by the sidecars left as they were. Every test also fails on an uncaught page error.
"""
import os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)
import test_gallery_bursts as GB  # noqa: E402  (the burst library)

DESKTOP = dict(viewport={"width": 1440, "height": 900})
PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
COVER, REST = "IMG_0001.jpg", ("IMG_0002.jpg", "IMG_0003.jpg")
JUNK = tuple(f"junk-{c}.jpg" for c in "abcdef")
PRESSED = "aria-pressed"


class GalleryDragSelectBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-drag-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        self.state = os.path.join(home, ".local/share/media-intake")
        self.lib = os.path.join(home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.path = GB.burst_library(self.lib, self.state)
        self.errors = []
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def junk(self):
        for k, n in enumerate(JUNK):
            p = self.path[n] = os.path.join(self.lib, "2026/2026-10", n)
            G.img(p, (200, 20 + 30 * k, 40))
            G.raw_xmp(p, ["Project|Personal", "Review|maybe-trash"])

    def snapshot(self):
        """sha256 of every original and its sidecar's text: checked unchanged at the end."""
        def side(p):
            with open(p + ".xmp") as f:
                return f.read()
        self.orig = {n: (G.sha(p), side(p)) for n, p in self.path.items()}
        self.addCleanup(lambda: [self.assertEqual((G.sha(p), side(p)), self.orig[n], f"{n} was changed")
                                 for n, p in self.path.items()])

    def page(self, size=DESKTOP):
        self.snapshot()
        s = G.Server(self.env)
        self.addCleanup(s.stop)
        ctx = self.browser.new_context(**size)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, s)
        return page

    def tile(self, page, n):
        return page.locator(f'[data-item="{n}"]')

    def centre(self, loc):
        b = loc.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    def drag(self, page, a, b, mods=(), up=True):
        """A mouse drag from point a to point b, in steps (with keys held)."""
        for m in mods:
            page.keyboard.down(m)
        page.mouse.move(*a)
        page.mouse.down()
        page.mouse.move(*b, steps=8)
        if up:
            page.mouse.up()
            for m in mods:
                page.keyboard.up(m)

    def selected(self, page):
        return sorted(page.locator("[data-item].sel").evaluate_all("els => els.map(e => e.dataset.item)"))

    def order(self, page):
        return page.evaluate("() => items.map(i => i.n)")

    def marks(self, page):
        return dict(page.locator("[data-item] .mark").evaluate_all(
            "els => els.map(m => [m.closest('[data-item]').dataset.item, m.textContent])"))

    def sug(self, page):
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Suggested junk").click()
        B.expect(page.locator("#vtitle")).to_have_text("Suggested junk")
        B.expect(page.locator("[data-item]")).to_have_count(len(JUNK) + 2)  # the junk and the burst's two
        return self.order(page)

    # ------------------------------------------------------------ Library
    def test_drag_selects_what_it_touches_a_closed_stack_by_its_cover(self):
        page = self.page()
        bar = page.get_by_role("toolbar", name="Selection actions")
        marq = page.locator("#marq")
        # the grid with the stack closed: lone, copy, the cover, early, in one row
        a, c = self.centre(self.tile(page, "lone.jpg")), self.centre(self.tile(page, COVER))
        self.drag(page, a, c, up=False)
        B.expect(marq).to_be_visible()  # the rectangle, while dragging
        page.mouse.up()
        B.expect(marq).to_have_count(0)
        self.assertEqual(self.selected(page), sorted(["lone.jpg", "copy.jpg", COVER]))  # never a hidden shot
        B.expect(bar).to_contain_text("3 selected")
        B.expect(page.locator("#selsay")).to_have_text("3 selected")
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()  # the release opened nothing
        # a plain drag starts a new selection; Shift and Ctrl add to it
        e = self.centre(self.tile(page, "early.jpg"))
        self.drag(page, e, (e[0] + 20, e[1] + 20))
        self.assertEqual(self.selected(page), ["early.jpg"])
        self.drag(page, a, (a[0] + 20, a[1] + 20), mods=["Shift"])
        self.assertEqual(self.selected(page), ["early.jpg", "lone.jpg"])
        cp = self.centre(self.tile(page, "copy.jpg"))
        self.drag(page, cp, (cp[0] + 10, cp[1] + 15), mods=["Control"])
        self.assertEqual(self.selected(page), ["copy.jpg", "early.jpg", "lone.jpg"])
        # Esc during a drag puts the selection back as it was
        self.drag(page, c, e, up=False)
        self.assertEqual(self.selected(page), sorted([COVER, "early.jpg"]))
        page.keyboard.press("Escape")
        B.expect(marq).to_have_count(0)
        page.mouse.up()
        self.assertEqual(self.selected(page), ["copy.jpg", "early.jpg", "lone.jpg"])
        # an open stack: the rectangle reaches every shot it shows
        self.tile(page, COVER).focus()
        page.keyboard.press("x")
        B.expect(self.tile(page, REST[1])).to_be_visible()
        r1 = self.tile(page, REST[1]).bounding_box()
        self.drag(page, c, (r1["x"] + 5, r1["y"] + 5))
        self.assertEqual(self.selected(page), sorted([COVER, *REST]))

    def test_a_click_is_not_a_drag_and_the_checkbox_never_drags(self):
        page = self.page()
        viewer = page.get_by_role("dialog", name="Item")
        # a press that moves 3 px is still a click: the large view opens, nothing is selected
        x, y = self.centre(self.tile(page, "copy.jpg"))
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 3, y + 2)
        B.expect(page.locator("#marq")).to_have_count(0)
        page.mouse.up()
        B.expect(viewer).to_have_attribute("data-current", "copy.jpg")
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        self.assertEqual(self.selected(page), [])
        # a shift-click on a box still selects the range (no drag in the way)
        self.tile(page, "lone.jpg").hover()
        self.tile(page, "lone.jpg").locator(".ck").click()
        self.tile(page, COVER).hover()
        self.tile(page, COVER).locator(".ck").click(modifiers=["Shift"])
        self.assertEqual(self.selected(page), sorted(["lone.jpg", "copy.jpg", COVER]))
        # a press on the checkbox's hit area that moves draws nothing
        self.tile(page, "early.jpg").hover()
        ck = self.centre(self.tile(page, "early.jpg").locator(".ckw"))
        page.mouse.move(*ck)
        page.mouse.down()
        page.mouse.move(ck[0] + 60, ck[1] + 40, steps=6)
        B.expect(page.locator("#marq")).to_have_count(0)
        page.mouse.up()
        self.assertEqual(self.selected(page), sorted(["lone.jpg", "copy.jpg", COVER]))
        # (released on the same tile, so it was a click on it: the large view opened)
        B.expect(viewer).to_have_attribute("data-current", "early.jpg")
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        # nor one on the tile's star or Share button (each keeps its own click)
        for name, loc in (("star", lambda t: t.locator(".star")), ("Share", lambda t: t.get_by_role("button", name="Share early.jpg"))):
            with self.subTest(button=name):
                t = self.tile(page, "early.jpg")
                t.hover()
                x, y = self.centre(loc(t))
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x - 80, y - 60, steps=6)
                B.expect(page.locator("#marq")).to_have_count(0)
                page.mouse.move(x - 200, y - 200)  # released away from the button: its click never fires
                page.mouse.up()
                self.assertEqual(self.selected(page), sorted(["lone.jpg", "copy.jpg", COVER]))
                B.expect(page.locator("#modal")).to_be_hidden()
                B.expect(viewer).to_be_hidden()

    def test_a_touch_drag_never_draws_a_rectangle(self):
        page = self.page(PHONE)
        # a finger's press and move (the browser scrolls with it): no rectangle, no selection
        drew = self.tile(page, "lone.jpg").evaluate("""t => {
          const r = t.getBoundingClientRect(), x = r.left + 10, y = r.top + 10;
          const ev = (type, dx, dy) => new PointerEvent(type, {bubbles: true, cancelable: true, pointerId: 7,
            pointerType: 'touch', isPrimary: true, button: type === 'pointermove' ? -1 : 0, buttons: type === 'pointerup' ? 0 : 1,
            clientX: x + dx, clientY: y + dy});
          t.dispatchEvent(ev('pointerdown', 0, 0));
          t.dispatchEvent(ev('pointermove', 40, 120));
          const on = !!document.getElementById('marq');
          t.dispatchEvent(ev('pointerup', 40, 120));
          return on }""")
        self.assertFalse(drew)
        self.assertEqual(self.selected(page), [])

    def test_a_drag_at_the_bottom_edge_scrolls_and_takes_in_the_next_page(self):
        for k in range(250):  # older than the burst library: the first page (240) ends before them
            G.img(os.path.join(self.lib, "2026/2026-10", f"many-{k:03d}.jpg"), (k, 40, 80))
        page = self.page()
        loaded, total = page.evaluate("() => [items.length, total]")
        self.assertEqual((loaded, total), (240, 256))
        a = self.centre(self.tile(page, "lone.jpg"))
        vw, vh = DESKTOP["viewport"]["width"], DESKTOP["viewport"]["height"]
        # to the right of every column and held at the window's bottom edge: the page scrolls by itself
        self.drag(page, a, (vw - 8, vh - 4), up=False)
        B.until(lambda: page.evaluate("() => done && items.length === total && sel.size === total - 2"),
                "the drag to scroll to the end, load the last page and select its tiles")
        self.assertGreater(page.evaluate("() => scrollY"), 0)
        page.mouse.up()
        # every tile shown, the new page's too; the closed stack's two hidden shots never
        n = page.locator("[data-item].sel").count()
        self.assertEqual(n, total - len(REST))
        self.assertEqual(page.locator("[data-item].sel[hidden]").count(), 0)

    # ------------------------------------------------------------ Suggested junk
    def test_shift_click_and_shift_keys_mark_a_range(self):
        self.junk()
        page = self.page()
        o = self.sug(page)
        bar = page.get_by_role("group", name="Decisions")
        btn = lambda n, k: self.tile(page, n).get_by_role("group", name=f"Mark {n}").get_by_role("button", name=k)
        btn(o[2], "Keep").click()
        btn(o[0], "Trash").click()
        btn(o[4], "Trash").click(modifiers=["Shift"])
        # 0..4 Trash (3 more than the one clicked, 2 keeping its Keep mark); nothing changes until Apply
        self.assertEqual(self.marks(page), {o[0]: "Trash", o[1]: "Trash", o[2]: "Keep", o[3]: "Trash", o[4]: "Trash"})
        B.expect(btn(o[3], "Trash")).to_have_attribute(PRESSED, "true")
        B.expect(page.locator("#selsay")).to_have_text("Marked 3 more Trash; 1 marked Keep kept that mark. Nothing changes until Apply.")
        B.expect(bar).to_contain_text("1 keep / 4 trash marked")
        # the keys: the outline is on 4 (the one pressed); → then Shift+K marks from the last Keep (2) to 5
        page.keyboard.press("ArrowRight")
        page.keyboard.press("Shift+K")
        self.assertEqual(self.marks(page), {o[0]: "Trash", o[1]: "Trash", o[2]: "Keep", o[3]: "Trash", o[4]: "Trash", o[5]: "Keep"})
        # Shift+T from the last Trash (4) to the outline (now 6): 5 kept its Keep, 6 marked
        page.keyboard.press("Shift+T")
        self.assertEqual(self.marks(page)[o[6]], "Trash")
        self.assertEqual(self.marks(page)[o[5]], "Keep")
        B.expect(bar).to_contain_text("2 keep / 5 trash marked")

    def test_a_drag_from_a_mark_marks_what_it_touches(self):
        self.junk()
        page = self.page()
        o = self.sug(page)
        btn = lambda n, k: self.tile(page, n).get_by_role("group", name=f"Mark {n}").get_by_role("button", name=k)
        btn(o[1], "Keep").click()
        a, z = self.centre(btn(o[0], "Trash")), self.centre(self.tile(page, o[-1]))
        self.drag(page, a, z, up=False)
        B.expect(page.locator("#marq")).to_have_class("trash")
        page.keyboard.press("Escape")  # puts the marks back
        page.mouse.up()
        self.assertEqual(self.marks(page), {o[1]: "Keep"})
        self.drag(page, a, z)
        want = {n: "Trash" for n in o}
        want[o[1]] = "Keep"  # marked the other way: kept
        self.assertEqual(self.marks(page), want)
        B.expect(btn(o[0], "Trash")).to_have_attribute(PRESSED, "true")  # the release didn't toggle it off
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()

    def test_mark_all_clear_marks_and_apply_still_asks(self):
        self.junk()
        page = self.page()
        o = self.sug(page)
        B.expect(page.get_by_role("button", name="Select all")).to_be_hidden()
        self.tile(page, o[3]).get_by_role("group", name=f"Mark {o[3]}").get_by_role("button", name="Keep").click()
        page.get_by_role("button", name="Mark all shown as Trash").click()
        B.expect(page.locator("#toast")).to_contain_text(f"Marked {len(o) - 1} more Trash (all shown); 1 marked Keep kept that mark.")
        bar = page.get_by_role("group", name="Decisions")
        B.expect(bar).to_contain_text(f"1 keep / {len(o) - 1} trash marked")
        # Apply asks with the thumbnails and the names, and Cancel changes nothing
        bar.get_by_role("button", name="Apply…").click()
        dlg = page.locator("[data-dialog=confirm]")
        B.expect(dlg.get_by_role("heading")).to_have_text(f"Apply: 1 keep, {len(o) - 1} trash?")
        B.expect(dlg.locator(".mthumbs img")).to_have_count(len(o) - 1)
        B.expect(dlg.locator(".mnames")).to_contain_text(o[0])
        B.expect(dlg.get_by_role("button", name=f"Apply and trash {len(o) - 1} files")).to_be_visible()
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        # Clear marks asks first; Cancel keeps them, Clear marks takes them off
        page.get_by_role("button", name="Clear marks").click()
        B.expect(dlg.get_by_role("heading")).to_have_text(f"Clear {len(o)} marks?")
        dlg.get_by_role("button", name="Cancel").click()
        self.assertEqual(len(self.marks(page)), len(o))
        page.get_by_role("button", name="Clear marks").click()
        dlg.get_by_role("button", name="Clear marks").click()
        self.assertEqual(self.marks(page), {})
        B.expect(bar).to_contain_text("0 keep / 0 trash marked")
        B.expect(page.get_by_role("button", name="Clear marks")).to_be_disabled()
        # neither button outside Suggested junk (they sit in its Decisions bar)
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Library").click()
        B.expect(page.get_by_role("button", name="Mark all shown as Trash")).to_be_hidden()
        B.expect(page.get_by_role("button", name="Clear marks")).to_be_hidden()

    def test_marks_survive_another_view_and_the_page_asks_before_unload(self):
        self.junk()
        page = self.page()
        o = self.sug(page)
        unload = "() => { const e = new Event('beforeunload', {cancelable: true}); dispatchEvent(e); return e.defaultPrevented }"
        self.assertFalse(page.evaluate(unload))  # nothing marked: no question
        # entering the view puts focus on the first tile, so Enter opens it (never the view again, wiping marks)
        B.expect(self.tile(page, o[0])).to_be_focused()
        page.keyboard.press("t")
        page.keyboard.press("k")
        self.assertEqual(self.marks(page), {o[0]: "Trash", o[1]: "Keep"})
        self.assertTrue(page.evaluate(unload))
        self.tile(page, o[0]).focus()
        page.keyboard.press("Enter")
        viewer = page.get_by_role("dialog", name="Item")
        B.expect(viewer).to_have_attribute("data-current", o[0])
        page.keyboard.press("Escape")
        # to the Library (no marks shown there) and back: the marks are still there
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Library").click()
        B.expect(page.locator("#vtitle")).to_have_text("Library")
        B.expect(page.locator("[data-item] .mark")).to_have_count(0)
        self.sug(page)
        self.assertEqual(self.marks(page), {o[0]: "Trash", o[1]: "Keep"})
        B.expect(page.get_by_role("group", name="Decisions")).to_contain_text("1 keep / 1 trash marked")

    def test_keys_with_no_outline_and_in_the_large_view(self):
        self.junk()
        page = self.page()
        o = self.sug(page)
        # no outline: T outlines the first tile and says so, marking nothing; T again marks it
        page.evaluate("() => { cur = -1; paintSel() }")
        page.locator("#vtitle").click()  # focus off the tiles
        page.keyboard.press("t")
        B.expect(page.locator("#toast")).to_contain_text("The first tile is outlined")
        B.expect(self.tile(page, o[0])).to_have_class("tile cur")
        self.assertEqual(self.marks(page), {})
        page.keyboard.press("t")
        self.assertEqual(self.marks(page), {o[0]: "Trash"})
        # no outline but the pointer on a tile: K marks that one
        page.evaluate("() => { cur = -1; paintSel() }")
        self.tile(page, o[3]).locator(".flag, img").first.hover()
        page.keyboard.press("k")
        self.assertEqual(self.marks(page)[o[3]], "Keep")
        # the large view: K / T mark and step on; its buttons say Mark; nothing asks, nothing is written
        self.tile(page, o[4]).locator("img").click()
        viewer = page.get_by_role("dialog", name="Item")
        B.expect(viewer).to_have_attribute("data-current", o[4])
        B.expect(viewer.get_by_role("button", name="Mark Trash (T)")).to_be_visible()
        B.expect(viewer.get_by_role("button", name="Keep (K)", exact=True)).to_have_count(0)
        page.keyboard.press("t")
        B.expect(viewer).to_have_attribute("data-current", o[5])
        B.expect(page.locator("[data-dialog=confirm]")).to_be_hidden()
        viewer.get_by_role("button", name="Mark Keep (K)").click()
        B.expect(viewer).to_have_attribute("data-current", o[6])
        page.keyboard.press("Escape")
        m = self.marks(page)
        self.assertEqual((m[o[4]], m[o[5]]), ("Trash", "Keep"))


if __name__ == "__main__":
    import unittest
    unittest.main()
