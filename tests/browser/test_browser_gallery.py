#!/usr/bin/env python3
"""The gallery page in headless Chromium, on synthetic media in a throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery.py

Covers what the HTTP tests in tests/test_gallery.py can't: keys, tile selection (click and
shift-click), the bottom selection bar (shown only while something is selected), the sidebar's
views, the confirm dialogs (Trash; Erase with ERASE typed; the Stop server button's in-page confirm),
the lightbox video playing by itself, Space pausing and resuming it, HTTP Range while the video
loads and seeks, the detached-job dialog, including re-attaching to a running job after a reload,
and the Events list (filter, Rename, Display name), Set event on a selection and a Share (the real
intake.py share-copy, which only reads the originals), and Share from a tile and from the large view: its
dialog, the folder opened when the job ends (a fake xdg-open on PATH that only records its arguments, in every
test here), Open folder again from the dialog and from the message, and a refused open saying why; Favorites
(the tile's star, F on a focused tile, a selection and in the large view, the bar's Favorite / Unfavorite, the
Favorites view and its count, Undo) and the When list's recent years with "All years (N)" remembered across a
reload.

Trash and Erase run a fake intake.py (MEDIA_REVIEW_INTAKE, tests/test_gallery.py's GATED) that
deletes the files it is given; with a FIFO at FAKE_GATE it first waits until the test opens it,
so a job is held mid-step without a sleep.

Hooks the tests rely on (keep them when the layout changes): body[data-ready] once a page of
items has rendered; each grid item's [data-item] (its file name) and its checkbox named
"select <name>"; the lightbox as role=dialog named "Item" with [data-current] = the name of the item shown;
the confirm/job dialog as [data-dialog=confirm] (role=dialog) with [data-job] =
running/done/failed/crashed, its job status as role=status (what runs, then how it ended) and its
output as its <pre>; the textbox named "Type ERASE"; the selection bar as role=toolbar
named "Selection actions"; the event bar as role=toolbar named "Event"; each sidebar event as
#evlist [data-ev=<name>]; the dialog's text field by its label; buttons by their names (Confirm,
Cancel, Trash N file(s), Erase N file(s), Hide (it keeps running), Close, Apply…, Suggested junk, Possible
child, Library, Clear selection, Stop server, Rename…, Display name…, Set event…, Share, Open folder,
Favorites); a tile's star as .star (aria-pressed, named "Favorite <name>"), the bar's #bfav / #bunfav, the large
view's .favb; a tile's Share button named "Share <name>"; the job dialog's Open folder result as #mopened
(role=status); an event's display name as its row's .lbl, its slug in the row's title and the event bar's
#evslug.
"""
import json, os, re, shutil, signal, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)


# An erase that prints nothing until the test opens FAKE_GATE (as the real one sweeping HOME for copies can
# for minutes), then one progress line, then waits on FAKE_GATE2 before it deletes the files it was given.
SLOW_ERASE = r'''
import os, sys
with open(os.environ["FAKE_GATE"]) as f:
    f.read()
print("erase: looked in 1 of 2 folders", flush=True)
with open(os.environ["FAKE_GATE2"]) as f:
    f.read()
for p in sys.argv[2:]:
    if os.path.isabs(p) and os.path.lexists(p):
        os.remove(p)
'''


# A share-copy that waits until the test opens FAKE_GATE, then copies each file into --out and prints its line as
# the real one does (no metadata stripped here: the HTTP tests and test_events_rename_bulk_event_and_share run the
# real one). It only reads the files it is given.
GATED_SHARE = r'''
import os, shutil, sys
args = sys.argv[2:]
out = args[args.index("--out") + 1]
with open(os.environ["FAKE_GATE"]) as f:
    f.read()
for p in args[:args.index("--out")]:
    shutil.copyfile(p, os.path.join(out, os.path.basename(p)))
    print(f"  {os.path.join(out, os.path.basename(p))}  gps=none", flush=True)
'''


# resolve once the <video> is paused (at once if it already is), bounded by STEP_MS.
PAUSED = """v => new Promise((res, rej) => {
  setTimeout(() => rej(new Error("still playing")), %d);
  if (v.paused) return res(true);
  v.addEventListener("pause", () => res(true), {once: true});
})""" % B.STEP_MS


class GalleryBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state)
        fake = os.path.join(self.tmp, "gated_intake.py")
        with open(fake, "w") as f:
            f.write(G.GATED)
        self.gate = os.path.join(self.tmp, "gate")  # a FIFO only in the tests that hold a job
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=self.gate,
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(self.home, "Videos/demo-footage")))
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        self.a, self.b, self.c = L("2026/2026-11/a.jpg"), L("2026/2026-11/b.jpg"), L("2026/2026-11/c.png")
        self.d = L("2026/2026-10/d.jpg")
        G.img(self.a, (200, 30, 30), (400, 300))
        G.img(self.b, (30, 200, 30))
        G.img(self.c, (30, 30, 200))
        G.img(self.d, (90, 90, 90))
        G.write_tags(self.a, ["Project|Personal", "Review|maybe-trash"])  # a trash suggestion
        self.opened = B.fake_opener(self.tmp, self.env, G.FAKE_OPENER)  # a Share's folder: never really opened
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        return s

    def add_video(self):
        self.v = os.path.join(self.vids, "2026/2026-09/v.mp4")
        os.makedirs(os.path.dirname(self.v), exist_ok=True)
        shutil.copyfile(self.clip(), self.v)

    def tile(self, page, name):
        return page.locator(f'[data-item="{name}"]').first

    def tiles(self, page):
        return page.locator("[data-item]").evaluate_all("els => els.map(e => e.dataset.item)")

    def tiles_selected(self, page):
        return page.locator("[data-item] input:checked").count()

    def confirm(self, page):
        return page.locator("[data-dialog=confirm]")

    # ------------------------------------------------------------ keys and selection
    def test_selection_and_keys(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        names = self.tiles(page)
        self.assertEqual(sorted(names), ["a.jpg", "b.jpg", "c.png", "d.jpg"])
        box = lambda n: page.get_by_role("checkbox", name=f"select {n}")  # noqa: E731
        # click one box, shift-click another: the range between them is selected
        box(names[0]).click()
        box(names[2]).click(modifiers=["Shift"])
        for n in names[:3]:
            B.expect(box(n)).to_be_checked()
        B.expect(box(names[3])).not_to_be_checked()
        B.expect(page.get_by_text("3 selected")).to_be_visible()
        # a plain click on a box toggles just that one
        box(names[1]).click()
        B.expect(box(names[1])).not_to_be_checked()
        B.expect(page.get_by_text("2 selected")).to_be_visible()
        # Esc clears the selection
        page.keyboard.press("Escape")
        for n in names:
            B.expect(box(n)).not_to_be_checked()
        # K keeps the selection: the sidecar gets Owner|keep, nothing else is touched
        box("b.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            page.keyboard.press("k")
        self.assertEqual(resp.value.status, 200)
        self.assertIn("Owner|keep", G.tags_of(self.b)[0])
        self.assertEqual(G.tags_of(self.c), ([], [], None))  # untouched: no sidecar written
        # a Keep that went through clears the selection (K here; the bar's Keep the same)
        B.expect(box("b.jpg")).not_to_be_checked()
        B.expect(page.get_by_role("toolbar", name="Selection actions")).to_be_hidden()
        box("c.png").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")):
            page.get_by_role("toolbar", name="Selection actions").get_by_role("button", name="Keep").click()
        B.expect(box("c.png")).not_to_be_checked()
        B.expect(page.get_by_role("toolbar", name="Selection actions")).to_be_hidden()
        # the lightbox: click opens it; arrows move in grid order; R rotates (sidecar only); Esc closes
        page.keyboard.press("Escape")
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, names[1]).click()
        B.expect(viewer).to_be_visible()
        B.expect(viewer).to_have_attribute("data-current", names[1])
        page.keyboard.press("ArrowRight")
        B.expect(viewer).to_have_attribute("data-current", names[2])
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        B.expect(viewer).to_have_attribute("data-current", names[0])
        path = {"a.jpg": self.a, "b.jpg": self.b, "c.png": self.c, "d.jpg": self.d}[names[0]]
        before = G.sha(path)
        with page.expect_response(lambda r: r.url.endswith("/api/rotate")) as resp:
            page.keyboard.press("r")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(G.tags_of(path)[2], 6)
        self.assertEqual(G.sha(path), before)  # the original is never written
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        # Suggested junk: arrows move the outline, K / T mark, nothing changes until Apply + Apply
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Suggested junk").click()
        B.expect(page.locator("[data-item]")).to_have_count(1)
        B.expect(page.locator("[data-item]")).to_have_attribute("data-item", "a.jpg")
        page.keyboard.press("ArrowRight")
        page.keyboard.press("k")
        B.expect(page.get_by_text("1 keep / 0 trash marked")).to_be_visible()
        self.assertNotIn("Owner|keep", G.tags_of(self.a)[0])
        page.get_by_role("button", name="Apply…").click()
        dlg = self.confirm(page)
        B.expect(dlg).to_be_visible()
        B.expect(dlg.get_by_role("heading")).to_have_text("Apply: 1 keep, 0 trash?")
        B.expect(dlg).to_contain_text("Keep 1 file: it won't be suggested as junk again.")
        with page.expect_response(lambda r: r.url.endswith("/api/decide")) as resp:
            dlg.get_by_role("button", name="Apply", exact=True).click()
        self.assertEqual(resp.value.status, 200)
        B.expect(dlg).to_be_hidden()
        self.assertIn("Owner|keep", G.tags_of(self.a)[0])
        self.assertNotIn("Review|maybe-trash", G.tags_of(self.a)[0])

    # ------------------------------------------------------------ the bottom selection bar and the sidebar views
    def test_selection_bar_only_while_selected(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        bar = page.get_by_role("toolbar", name="Selection actions")
        box = lambda n: page.get_by_role("checkbox", name=f"select {n}")  # noqa: E731
        B.expect(bar).to_be_hidden()
        box("b.jpg").click()
        B.expect(bar).to_be_visible()
        B.expect(bar).to_contain_text("1 selected")
        for name in ("Keep", "Trash", "Erase", "More…", "Clear selection"):
            B.expect(bar.get_by_role("button", name=name)).to_be_visible()
        # the rest sit behind More…: a popup above the bar, Esc closes it and gives focus back to More…
        more, pop = bar.get_by_role("button", name="More…"), page.locator("#selmore")
        B.expect(bar.get_by_role("button", name="+ tag")).to_be_hidden()
        B.expect(more).to_have_attribute("aria-expanded", "false")
        more.click()
        B.expect(more).to_have_attribute("aria-expanded", "true")
        for name in ("★ Favorite", "+ tag", "− tag", "Set event…", "Rotate ⟳", "Share"):
            B.expect(pop.get_by_role("button", name=name)).to_be_visible()
        B.expect(pop.get_by_role("button", name="★ Favorite")).to_be_focused()  # focus on its first button
        page.keyboard.press("ArrowDown")
        B.expect(pop.get_by_role("button", name="☆ Unfavorite")).to_be_focused()
        page.keyboard.press("Escape")
        B.expect(pop).to_be_hidden()
        B.expect(more).to_be_focused()
        B.expect(more).to_have_attribute("aria-expanded", "false")
        B.expect(box("b.jpg")).to_be_checked()  # that Esc closed the popup only, the selection stays
        more.press("ArrowDown")  # the keyboard opens it too
        B.expect(pop).to_be_visible()
        page.locator("#vtitle").click()  # a click elsewhere closes it
        B.expect(pop).to_be_hidden()
        box("c.png").click()
        B.expect(bar).to_contain_text("2 selected")
        bar.get_by_role("button", name="Clear selection").click()  # the bar's own Clear
        B.expect(bar).to_be_hidden()
        B.expect(box("b.jpg")).not_to_be_checked()
        box("d.jpg").click()
        B.expect(bar).to_be_visible()
        page.keyboard.press("Escape")  # and Esc
        B.expect(bar).to_be_hidden()
        box("d.jpg").click()
        box("d.jpg").click()  # unticking the last one hides it too
        B.expect(bar).to_be_hidden()
        # a quick view in the sidebar filters the grid and shows as a removable chip; Library clears it
        G.write_tags(self.c, ["Review|child-in-shot"])  # read at start-up: a fresh server
        s2 = self.start()
        self.open(page, s2)
        page.get_by_role("navigation", name="Views").get_by_role("button", name="Possible child").click()
        B.expect(page.locator("[data-item]")).to_have_count(1)
        B.expect(page.locator("[data-item]")).to_have_attribute("data-item", "c.png")
        B.expect(page.get_by_role("button", name="Remove Flags: Possible child")).to_be_visible()
        page.get_by_role("button", name="Library").click()
        B.expect(page.locator("[data-item]")).to_have_count(4)

    # ------------------------------------------------------------ favorites
    def rating(self, p):
        return G.gallery_read(p).get("rating")

    def test_favorites_star_keys_bar_and_view(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        views = page.get_by_role("navigation", name="Views")
        fav_view = views.get_by_role("button", name="Favorites")
        B.expect(fav_view.locator(".ct")).to_have_text("0")
        star = lambda n: self.tile(page, n).locator(".star")  # noqa: E731
        # the star shows on hover (and focus), not on an idle tile
        page.mouse.move(1, 1)
        B.expect(star("b.jpg")).to_have_css("opacity", "0")
        self.tile(page, "b.jpg").hover()
        B.expect(star("b.jpg")).to_have_css("opacity", "1")
        B.expect(star("b.jpg")).to_have_attribute("aria-pressed", "false")
        # a click stars it (xmp:Rating 5 in the sidecar only; the large view stays shut), and it stays shown
        before = G.sha(self.b)
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")) as resp:
            star("b.jpg").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(star("b.jpg")).to_have_attribute("aria-pressed", "true")
        B.expect(page.get_by_role("dialog", name="Item")).to_be_hidden()
        self.assertEqual(self.rating(self.b), "5")
        self.assertEqual(G.sha(self.b), before)
        page.mouse.move(1, 1)
        B.expect(star("b.jpg")).to_have_css("opacity", "1")
        B.expect(fav_view.locator(".ct")).to_have_text("1")
        # Undo on its message (Ctrl+Z) puts the sidecar back
        B.expect(page.locator("#tundo")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/api/undo")) as resp:
            page.keyboard.press("Control+z")
        self.assertEqual(resp.value.status, 200)
        B.expect(star("b.jpg")).to_have_attribute("aria-pressed", "false")
        self.assertIsNone(self.rating(self.b))
        # F on the focused tile toggles it; F on a selection stars them all, again unstars them all
        self.tile(page, "c.png").focus()
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            page.keyboard.press("f")
        B.expect(star("c.png")).to_have_attribute("aria-pressed", "true")
        box = lambda n: page.get_by_role("checkbox", name=f"select {n}")  # noqa: E731
        box("c.png").click()
        box("d.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            page.keyboard.press("f")  # c is one already, d isn't: both starred
        B.expect(star("d.jpg")).to_have_attribute("aria-pressed", "true")
        self.assertEqual((self.rating(self.c), self.rating(self.d)), ("5", "5"))
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            page.keyboard.press("F")  # every one is: both unstarred
        B.expect(star("c.png")).to_have_attribute("aria-pressed", "false")
        B.expect(star("d.jpg")).to_have_attribute("aria-pressed", "false")
        # the bar's buttons
        bar = page.get_by_role("toolbar", name="Selection actions")
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            B.more(page, "#bfav").click()
        B.expect(star("d.jpg")).to_have_attribute("aria-pressed", "true")
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            B.more(page, "#bunfav").click()
        B.expect(star("d.jpg")).to_have_attribute("aria-pressed", "false")
        self.assertIsNone(self.rating(self.d))
        page.keyboard.press("Escape")
        # the large view: its Favorite button and F
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, "a.jpg").click()
        B.expect(viewer).to_have_attribute("data-current", "a.jpg")
        fb = viewer.locator(".favb")
        B.expect(fb).to_have_attribute("aria-pressed", "false")
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            page.keyboard.press("f")
        B.expect(viewer.locator(".favb")).to_have_attribute("aria-pressed", "true")
        self.assertEqual(self.rating(self.a), "5")
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            viewer.locator(".favb").click()
        B.expect(viewer.locator(".favb")).to_have_attribute("aria-pressed", "false")
        with page.expect_response(lambda r: r.url.endswith("/api/favorite")):
            page.keyboard.press("f")
        B.expect(viewer.locator(".favb")).to_have_attribute("aria-pressed", "true")
        page.keyboard.press("Escape")
        # a.jpg was Suggested junk; as a favorite it isn't any more. The Favorites view lists it, with a chip
        B.expect(views.get_by_role("button", name="Suggested junk").locator(".ct")).to_have_text("0")
        fav_view.click()
        B.expect(page.locator("[data-item]")).to_have_count(1)
        B.expect(page.locator("[data-item]")).to_have_attribute("data-item", "a.jpg")
        B.expect(page.locator("#vtitle")).to_have_text("Favorites")
        B.expect(fav_view).to_have_attribute("aria-current", "page")
        B.expect(page.get_by_role("button", name="Remove Flags: Favorite")).to_be_visible()
        # Trash asks as always, and says it is a favorite
        page.get_by_role("checkbox", name="select a.jpg").click()
        page.get_by_role("toolbar", name="Selection actions").get_by_role("button", name="Trash").click()
        B.expect(self.confirm(page)).to_contain_text("This one is a favorite.")
        self.confirm(page).get_by_role("button", name="Cancel").click()
        self.assertTrue(os.path.exists(self.a))
        # Added in the last 30 days: its name says the window
        B.expect(views.get_by_role("button", name="Added in the last 30 days")).to_be_visible()

    def test_when_lists_recent_years_then_all(self):
        for y in range(2018, 2026):  # eight more years, 2018-2025, beside 2026's
            G.img(os.path.join(self.lib, f"{y}/{y}-06/y{y}.jpg"), (y % 255, 90, 90))
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        when = page.locator("#when")
        if when.is_hidden():
            page.locator('.fs[data-s=when] .fs-h').click()
        years = lambda: when.locator(".yr .opt").evaluate_all("els => els.map(e => e.dataset.val)")  # noqa: E731
        B.expect(when.locator(".yr")).to_have_count(5)
        self.assertEqual(years(), ["2026", "2025", "2024", "2023", "2022"])
        more = when.get_by_role("button", name="All years (9)")
        B.expect(more).to_have_attribute("aria-expanded", "false")
        more.click()
        B.expect(when.locator(".yr")).to_have_count(9)
        B.expect(when.get_by_role("button", name="Fewer years")).to_be_focused()
        # remembered per browser: a reload keeps every year shown
        self.open(page, s)
        if when.is_hidden():
            page.locator('.fs[data-s=when] .fs-h').click()
        B.expect(when.locator(".yr")).to_have_count(9)
        # an older year chosen stays listed when the list is short again
        when.locator('.yr .opt[data-val="2018"]').click()
        when.get_by_role("button", name="Fewer years").click()
        B.expect(when.locator(".yr")).to_have_count(6)
        B.expect(page.locator("[data-item]")).to_have_count(1)

    # ------------------------------------------------------------ confirm dialogs
    def test_trash_and_erase_dialogs(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        dlg = self.confirm(page)
        page.get_by_role("checkbox", name="select b.jpg").click()
        # T asks first; Cancel changes nothing
        page.keyboard.press("t")
        B.expect(dlg).to_be_visible()
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash 1 file?")
        B.expect(dlg.get_by_text("b.jpg", exact=True)).to_be_visible()
        B.expect(dlg.locator(".mthumbs img")).to_have_count(1)  # the grid's own thumbnail of what it deletes
        B.expect(dlg).to_contain_text("Deletes this file from this computer for good")
        B.expect(dlg).to_contain_text("this can't be undone")
        B.expect(dlg.get_by_role("button", name="Trash 1 file")).to_be_visible()
        B.expect(dlg.get_by_role("textbox", name="Type ERASE")).to_be_hidden()
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        self.assertTrue(os.path.exists(self.b))
        # E needs ERASE typed exactly
        page.keyboard.press("e")
        B.expect(dlg.get_by_role("heading")).to_have_text("Erase 1 file everywhere?")
        typed, go = dlg.get_by_role("textbox", name="Type ERASE"), dlg.get_by_role("button", name="Erase 1 file")
        B.expect(typed).to_be_focused()
        # plain words: what, from where, and whether the backup drive is in (no --drive here); no command names
        B.expect(dlg).to_contain_text("Erases every copy of this file: in your library and the rest of your home folder")
        B.expect(dlg).to_contain_text("Not on a backup drive: the gallery was started without one.")
        B.expect(dlg.locator("#mb")).not_to_contain_text("intake")
        B.expect(dlg.locator("#mb")).not_to_contain_text("HOME")
        B.expect(go).to_be_disabled()
        for wrong in ("erase", "ERAS", "ERASE ", " ERASE"):
            typed.fill(wrong)
            B.expect(go).to_be_disabled()
        page.keyboard.press("Escape")  # Esc closes the dialog, nothing runs
        B.expect(dlg).to_be_hidden()
        self.assertTrue(os.path.exists(self.b))
        page.keyboard.press("e")
        B.expect(typed).to_have_value("")  # a fresh dialog: the old text is gone
        typed.fill("ERASE")
        B.expect(go).to_be_enabled()
        go.click()
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(dlg.get_by_role("heading")).to_have_text("Done")
        B.expect(dlg.get_by_role("status")).to_have_text("Done: 1 file erased everywhere.")
        B.expect(dlg.locator("pre")).to_contain_text("$ intake.py forget [1 file] --yes")
        B.expect(dlg.locator("pre")).to_contain_text("done: 1 of 1 gone from the library")
        self.assertFalse(os.path.exists(self.b))
        B.expect(page.locator('[data-item="b.jpg"]')).to_have_count(0)
        # the Stop server button's in-page confirm: Cancel keeps the server, Stop the server stops it
        dlg.get_by_role("button", name="Close", exact=True).click()
        close = page.get_by_role("button", name="Stop server", exact=True)
        close.click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Stop the gallery server?")
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        B.expect(close).to_be_focused()  # focus back on the button that opened it
        s.get("/api/facets")  # still serving (raises unless 200)
        self.assertIsNone(s.p.poll())
        close.click()
        dlg.get_by_role("button", name="Stop the server").click()
        B.expect(page.get_by_text("Gallery stopped. You can close this tab.")).to_be_visible()
        self.assertEqual(s.p.wait(timeout=10), 0)

    # ------------------------------------------------------------ video: autoplay and Range
    def test_lightbox_video_autoplays_and_seeks_by_range(self):
        self.add_video()
        size = os.path.getsize(self.v)
        s = self.start()
        page = self.new_page()
        media = B.MediaRequests(page)
        self.open(page, s)
        self.tile(page, "v.mp4").click()
        viewer = page.get_by_role("dialog", name="Item")
        B.expect(viewer).to_have_attribute("data-current", "v.mp4")
        video = viewer.locator("video")
        src = video.evaluate("v => new URL(v.src).pathname")
        # it plays by itself: the test never calls play()
        self.assertTrue(video.evaluate("v => v.autoplay"))
        t0 = video.evaluate(B.PLAYING)
        self.assertGreater(t0, 0)
        self.assertAlmostEqual(video.evaluate("v => v.duration"), 30, delta=0.2)
        # seek to near the end, outside what the browser has fetched: a new Range request, and it plays on
        self.assertFalse(video.evaluate(B.BUFFERED, 28.0), "fixture: the seek target is already buffered")
        before = len(media.of(src))
        self.assertAlmostEqual(video.evaluate(B.SEEK, 28.0), 28.0, delta=0.5)
        self.assertGreaterEqual(video.evaluate(B.PLAYING), 27.5)
        self.assert_ranges(media.of(src), size, after=before)
        # Space pauses and plays it again (the large view has the focus, not a button or the video's controls)
        B.expect(viewer).to_be_focused()
        page.keyboard.press(" ")
        self.assertTrue(video.evaluate(PAUSED))
        page.keyboard.press(" ")
        self.assertGreater(video.evaluate(B.PLAYING), 0)
        self.assertEqual(self.tiles_selected(page), 0)  # Space in the large view never ticks a tile
        # Esc closes the lightbox and drops the video (nothing keeps playing behind it)
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        B.expect(viewer.locator("video")).to_have_count(0)

    # ------------------------------------------------------------ the detached job's dialog
    def test_job_progress_and_reattach_after_reload(self):
        os.mkfifo(self.gate)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        dlg = self.confirm(page)
        page.get_by_role("checkbox", name="select c.png").click()
        page.keyboard.press("t")
        dlg.get_by_role("button", name="Trash 1 file").click()
        # progress: what runs and the output so far, while the fake waits at the gate
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash running")
        out = dlg.locator("pre")
        B.expect(out).to_contain_text("$ intake.py trash 'c.png'")
        B.expect(out).to_contain_text("trash: waiting at the gate")
        B.expect(dlg.get_by_role("button", name="Trash 1 file")).to_be_hidden()
        B.expect(dlg.get_by_role("button", name="Hide (it keeps running)")).to_be_visible()
        # a reload (the page closed and opened again) attaches to the running job from its status file
        page.reload()
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        B.expect(dlg).to_be_visible()
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash running")
        B.expect(out).to_contain_text("trash: waiting at the gate")
        # a second page opened meanwhile shows the running job too
        other = self.new_page()
        self.open(other, s)
        odlg = self.confirm(other)
        B.expect(odlg).to_have_attribute("data-job", "running")
        self.assertTrue(os.path.exists(self.c))
        B.open_gate(self.gate)
        for p, d in ((page, dlg), (other, odlg)):
            # a Trash that went well closes its dialog: a message says so, its output behind Details
            B.expect(d).to_have_attribute("data-job", "done")
            B.expect(d).to_be_hidden()
            B.expect(p.locator("#toast > span")).to_have_text("Done: 1 file trashed.")
            B.expect(p.locator('[data-item="c.png"]')).to_have_count(0)
            p.locator("#toast").get_by_role("button", name="Details").click()
            B.expect(d).to_be_visible()
            B.expect(d.get_by_role("heading")).to_have_text("Done")
            B.expect(d.get_by_role("button", name="Close", exact=True)).to_be_visible()
            B.expect(d.locator("pre")).to_contain_text("done: 1 of 1 gone from the library")
        self.assertFalse(os.path.exists(self.c))

    def test_erase_status_before_any_output(self):
        """Erase shows what it is doing at once, while its output is still empty, then streams the output."""
        fake, gate2 = os.path.join(self.tmp, "slow_erase.py"), os.path.join(self.tmp, "gate2")
        with open(fake, "w") as f:
            f.write(SLOW_ERASE)
        os.mkfifo(self.gate)
        os.mkfifo(gate2)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE2=gate2)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        dlg = self.confirm(page)
        page.get_by_role("checkbox", name="select b.jpg").click()
        page.keyboard.press("e")
        dlg.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        dlg.get_by_role("button", name="Erase 1 file").click()
        status, out = dlg.get_by_role("status"), dlg.locator("pre")
        B.expect(status).to_have_text("Erasing… looking for copies in your home folder. This can take a few "
                                      "minutes; you can close this page, it keeps running.")
        B.expect(dlg).to_have_attribute("data-job", "running")
        # the fake has printed nothing yet: the output is the command line alone
        B.expect(out).to_have_text(re.compile(r"^\$ intake\.py forget \[1 file\] --yes\s*$"))
        B.open_gate(self.gate)
        B.expect(out).to_contain_text("erase: looked in 1 of 2 folders")  # streamed while it runs
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(status).to_be_visible()
        B.open_gate(gate2)
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(dlg.get_by_role("heading")).to_have_text("Done")
        B.expect(out).to_contain_text("done: 1 of 1 gone from the library")
        B.expect(status).to_have_text("Done: 1 file erased everywhere.")  # the end, read out (role=status)
        self.assertFalse(os.path.exists(self.b))

    # ------------------------------------------------------------ events, a bulk event and a share-safe copy
    def test_events_rename_bulk_event_and_share(self):
        self.env.pop("MEDIA_REVIEW_INTAKE")  # share-copy runs the real intake.py, which only reads the originals
        G.write_tags(self.b, ["Event|picnic"])
        G.write_tags(self.c, ["Event|picnic"])
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        ev = lambda n: page.locator(f'#evlist [data-ev="{n}"]')  # noqa: E731
        dlg = self.confirm(page)
        # the sidebar's Events list: one click filters to the event and shows its Rename / Merge bar
        B.expect(ev("picnic")).to_contain_text("2")
        ev("picnic").click()
        B.expect(page.locator("[data-item]")).to_have_count(2)
        self.assertEqual(sorted(self.tiles(page)), ["b.jpg", "c.png"])
        B.expect(page.locator("#vtitle")).to_have_text("Picnic")  # the event's default display name
        B.expect(page.locator("#chips")).to_be_hidden()  # the event is the title and the bar, not a chip too
        evbar = page.get_by_role("toolbar", name="Event", exact=True)
        evbar.get_by_role("button", name="Rename…").click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Rename the event Picnic?")  # its display name; the slug in the text
        field = dlg.get_by_label("New name")
        B.expect(field).to_have_value("picnic")
        field.fill("Not A Slug")  # refused: the dialog stays open with the reason, nothing written
        dlg.get_by_role("button", name="Confirm").click()
        B.expect(dlg.locator("pre")).to_contain_text("Refused:")
        self.assertIn("Event|picnic", G.tags_of(self.b)[0])
        field.fill("park-day")
        with page.expect_response(lambda r: r.url.endswith("/api/event/rename")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(dlg).to_be_hidden()
        B.expect(page.locator("#vtitle")).to_have_text("Park day")
        B.expect(ev("park-day")).to_contain_text("2")
        B.expect(ev("picnic")).to_have_count(0)
        for p in (self.b, self.c):
            self.assertEqual([t for t in G.tags_of(p)[0] if t.startswith("Event|")], ["Event|park-day"], p)
        # bulk: two items picked in the Library get the event through the selection bar
        page.get_by_role("button", name="Library").click()
        B.expect(page.locator("[data-item]")).to_have_count(4)
        page.get_by_role("checkbox", name="select a.jpg").click()
        page.get_by_role("checkbox", name="select d.jpg").click()
        bar = page.get_by_role("toolbar", name="Selection actions")
        B.more(page, "Set event…").click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Set the event on 2 files?")
        dlg.get_by_label("Event name").fill("park-day")
        with page.expect_response(lambda r: r.url.endswith("/api/event/set")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(ev("park-day")).to_contain_text("4")
        for p in (self.a, self.d):
            self.assertIn("Event|park-day", G.tags_of(p)[0], p)
        self.assertIn("Review|maybe-trash", G.tags_of(self.a)[0])  # every other tag stays
        # Share: the dialog names the destination folder and shows share-copy's own lines; the folder opens
        before = G.sha(self.d)
        page.get_by_role("checkbox", name="select d.jpg").click()
        B.more(page, "Share", exact=True).click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Share 1 file?")
        B.expect(dlg).to_contain_text("~/Pictures/share-safe")
        B.expect(dlg).to_contain_text("opens that folder")
        dlg.get_by_role("button", name="Confirm").click()
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(dlg.get_by_role("heading")).to_have_text(
            re.compile(r"^Done: share-safe copies in ~/Pictures/share-safe/\d{4}-\d\d-\d\d-\d{6}$"))
        B.expect(dlg.locator("pre")).to_contain_text("d.jpg  gps=none")
        share = os.path.join(self.home, "Pictures/share-safe")
        (sub,) = os.listdir(share)
        self.assertEqual(os.listdir(os.path.join(share, sub)), ["d.jpg"])
        self.assertEqual(G.sha(self.d), before)  # the original is only read
        B.expect(dlg.locator("#mopened")).to_have_text(re.compile(
            r"^Opened ~/Pictures/share-safe/\d{4}-\d\d-\d\d-\d{6} in the file manager: drag the copies"))
        self.assertEqual(B.opened(self.opened), [os.path.join(share, sub)])  # that folder, once, by itself

    # ------------------------------------------------------------ Share from a tile and from the large view
    def test_share_from_a_tile_and_the_large_view(self):
        os.mkfifo(self.gate)
        fake = os.path.join(self.tmp, "share_intake.py")
        with open(fake, "w") as f:
            f.write(GATED_SHARE)
        self.env["MEDIA_REVIEW_INTAKE"] = fake
        s = self.start()
        page = self.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "uncaught errors on the page"))
        self.open(page, s)
        dlg = self.confirm(page)
        share = os.path.join(self.home, "Pictures/share-safe")
        # a tile's Share: shown on hover, asks about that one item, selects nothing and opens nothing else
        t = self.tile(page, "d.jpg")
        btn = t.get_by_role("button", name="Share d.jpg")
        B.expect(btn).to_have_css("opacity", "0")
        t.hover()
        B.expect(btn).to_have_css("opacity", "1")
        B.expect(btn).to_have_attribute("title", re.compile(r"without location \(GPS\) or other hidden info.*"
                                                             r"originals don't change"))
        btn.click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Share 1 file?")
        B.expect(dlg.locator(".mnames")).to_have_text("d.jpg")
        B.expect(page.get_by_role("dialog", name="Item", exact=True)).to_be_hidden()
        self.assertEqual(self.tiles_selected(page), 0)
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        self.assertFalse(os.path.exists(share))  # nothing made on Cancel
        # the large view: Share in its header, the tools row without it
        self.tile(page, "b.jpg").click()
        viewer = page.get_by_role("dialog", name="Item", exact=True)
        B.expect(viewer).to_have_attribute("data-current", "b.jpg")
        vs = viewer.locator(".ihead").get_by_role("button", name="Share", exact=True)
        B.expect(vs).to_be_visible()
        B.expect(viewer.locator(".tools").get_by_role("button", name=re.compile("Share"))).to_have_count(0)
        vs.click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Share 1 file?")
        B.expect(dlg.locator(".mnames")).to_have_text("b.jpg")
        dlg.get_by_role("button", name="Confirm").click()
        B.expect(dlg).to_have_attribute("data-job", "running")
        dlg.get_by_role("button", name="Hide (it keeps running)").click()
        B.expect(dlg).to_be_hidden()
        # it ends while hidden: the folder opens by itself, and the message names it and offers Open folder
        B.open_gate(self.gate)
        toast = page.locator("#toast")
        B.expect(toast).to_contain_text(re.compile(r"^Opened ~/Pictures/share-safe/\d{4}-\d\d-\d\d-\d{6} in the file "
                                                   r"manager: drag the copies into a chat or an email\."))
        (sub,) = os.listdir(share)
        out = os.path.join(share, sub)
        self.assertEqual(os.listdir(out), ["b.jpg"])
        B.until(lambda: B.opened(self.opened) == [out], "the folder to open after the Share click")
        os.remove(self.opened)
        with page.expect_response(lambda r: r.url.endswith("/api/share/open")) as resp:
            toast.get_by_role("button", name="Open folder").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(toast).to_contain_text("Opened ~/Pictures/share-safe/" + sub + " in the file manager")
        B.until(lambda: B.opened(self.opened) == [out], "Open folder to open it again")
        os.remove(self.opened)
        # no xdg-open on the server (its 409, the server's words): a Share watched to its end says so in the
        # dialog with where the copies are, and Open folder there asks again
        missing = ("can't open the folder: xdg-open is not installed (Fedora package xdg-utils; "
                   "media-intake doctor checks it)")
        asked = []
        page.route("**/api/share/open", lambda r: (asked.append(r.request.post_data_json), r.fulfill(
            status=409, content_type="application/json", body=json.dumps({"error": missing}))))
        page.keyboard.press("Escape")  # the large view closed when the job ended; nothing else open
        self.tile(page, "c.png").hover()
        self.tile(page, "c.png").get_by_role("button", name="Share c.png").click()
        dlg.get_by_role("button", name="Confirm").click()
        B.open_gate(self.gate)
        B.expect(dlg).to_have_attribute("data-job", "done")
        dest = dlg.locator("#mdest").inner_text().removeprefix("Destination: ")
        self.assertRegex(dest, r"^~/Pictures/share-safe/\d{4}-")
        B.expect(dlg.locator("#mopened")).to_have_text(f"Couldn't open the folder: {missing} The copies are in {dest}.")
        dlg.get_by_role("button", name="Open folder").click()
        B.expect(dlg.locator("#mopened")).to_have_text(f"Couldn't open the folder: {missing}")
        self.assertEqual(len(asked), 2)
        self.assertEqual(set(asked[0]), {"job"})  # the page names the job, never a folder
        self.assertIsNone(B.opened(self.opened))

    def test_event_display_name(self):
        """The sidebar and the title show an event's display name (escaped, never markup), its slug in the
        tooltip and the event bar; Display name… sets and clears it; a default that would look like another
        event's falls back to the slug; no sidecar changes."""
        G.write_tags(self.b, ["Event|some-trip-2024"])
        G.write_tags(self.c, ["Event|a-b"])
        G.write_tags(self.d, [], extra=["-XMP-iptcExt:Event=A b"])  # another tool's text: "a-b" would look alike
        tags = {p: G.tags_of(p) for p in (self.b, self.c, self.d)}
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        ev = lambda n: page.locator(f'#evlist [data-ev="{n}"]')  # noqa: E731
        B.expect(ev("some-trip-2024").locator(".lbl")).to_have_text("Some trip 2024")
        B.expect(ev("some-trip-2024")).to_have_attribute("title", "Event: some-trip-2024")
        B.expect(ev("a-b").locator(".lbl")).to_have_text("a-b")  # never "A b": that is the other event
        B.expect(ev("A b").locator(".lbl")).to_have_text("A b")
        ev("some-trip-2024").click()
        title, evbar = page.locator("#vtitle"), page.get_by_role("toolbar", name="Event", exact=True)
        B.expect(title).to_have_text("Some trip 2024")
        B.expect(title).to_have_attribute("title", "Event: some-trip-2024")
        B.expect(evbar.locator("#evname")).to_have_text("Event: Some trip 2024")
        B.expect(evbar.locator("#evslug")).to_have_text("some-trip-2024")
        B.expect(evbar.locator("#evslug")).to_be_visible()
        dlg = self.confirm(page)
        evbar.get_by_role("button", name="Display name…").click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Display name for the event some-trip-2024?")
        field = dlg.get_by_label("Display name")
        B.expect(field).to_have_value("")
        field.fill("a_B")  # looks like another event's: refused, the dialog stays with the reason
        dlg.get_by_role("button", name="Confirm").click()
        B.expect(dlg.locator("pre")).to_contain_text("Refused:")
        name = '<img src=x onerror="window.__evx=1"> Road & <b>trip</b>'
        field.fill(name)
        with page.expect_response(lambda r: r.url.endswith("/api/event/name")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(dlg).to_be_hidden()
        B.expect(ev("some-trip-2024").locator(".lbl")).to_have_text(name)  # as text
        B.expect(title).to_have_text(name)
        B.expect(evbar.locator("#evname")).to_have_text("Event: " + name)
        B.expect(evbar.locator("#evslug")).to_have_text("some-trip-2024")
        B.expect(page.locator("#evlist img, #vtitle img, #evbar img, #evlist b, #vtitle b")).to_have_count(0)
        self.assertIsNone(page.evaluate("window.__evx"))
        page.reload()  # kept by the server, not the page
        B.expect(ev("some-trip-2024").locator(".lbl")).to_have_text(name)
        ev("some-trip-2024").click()
        evbar.get_by_role("button", name="Display name…").click()
        B.expect(field).to_have_value(name)
        field.fill("")  # empty: back to the default
        with page.expect_response(lambda r: r.url.endswith("/api/event/name")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        B.expect(ev("some-trip-2024").locator(".lbl")).to_have_text("Some trip 2024")
        B.expect(title).to_have_text("Some trip 2024")
        ev("A b").click()  # an event shown by its own text: no second line in the bar, no tooltip on the title
        B.expect(title).to_have_text("A b")
        B.expect(evbar.locator("#evslug")).to_be_hidden()
        self.assertEqual(title.get_attribute("title"), "")
        self.assertEqual({p: G.tags_of(p) for p in tags}, tags)  # never a sidecar

    def test_crashed_job_is_shown(self):
        os.mkfifo(self.gate)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        dlg = self.confirm(page)
        page.get_by_role("checkbox", name="select d.jpg").click()
        page.keyboard.press("t")
        dlg.get_by_role("button", name="Trash 1 file").click()
        B.expect(dlg.locator("pre")).to_contain_text("trash: waiting at the gate")
        with open(os.path.join(self.state, "jobs", "gallery.json")) as f:
            os.kill(json.load(f)["pid"], signal.SIGKILL)
        B.expect(dlg).to_have_attribute("data-job", "crashed")
        B.expect(dlg.get_by_role("heading")).to_contain_text("The trash stopped unexpectedly")
        B.expect(dlg.get_by_role("heading")).to_contain_text("the step it started is still running")
        B.open_gate(self.gate)  # let the orphaned step end so nothing outlives the test
        B.until(lambda: not s.get("/api/job")["step_running"], "the orphaned step to end")


if __name__ == "__main__":
    unittest.main(verbosity=2)
