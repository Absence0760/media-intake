#!/usr/bin/env python3
"""My places in the gallery, in headless Chromium, on synthetic media and made-up points in a throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_places.py

Covered: the sidebar's Places section (its empty hint, then each place with its count, a click showing its
items with the place's name as the view's title and the row marked current), Add a place here… from the large
view (only on an item with GPS; the name field and the radius picker; a refused look-alike shown in the dialog;
the tags written to the sidecars, the city tag kept), the "probably here" question on an item without GPS
(Yes writes the tag, No writes nothing and the question is gone), a place name that looks like markup put on
the page as text, and no sideways scroll on a phone with the dialog open. No native dialog, no page error, and
the page never receives a coordinate (every reply is checked).
"""
import json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (the server wrapper)
import test_places as T  # noqa: E402  (sidecars with GPS, made-up points)

PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
CITY = "Place|Fernvale, Eastmarch, XA"


class GalleryPlacesBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-places-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        L = lambda n: os.path.join(self.lib, "2026/2026-11", n)  # noqa: E731
        self.path = {n: L(n) for n in ("a.jpg", "b.jpg", "far.jpg", "n.jpg")}
        cam = ["Project|Personal", "Source|phone camera"]
        T.sidecar(self.path["a.jpg"], cam + [CITY], gps=T.north(T.P, 10), date="2026:11:08 19:00:00")
        T.sidecar(self.path["b.jpg"], cam + [CITY], gps=T.north(T.P, 80), date="2026:11:08 18:00:00")
        T.sidecar(self.path["far.jpg"], cam, gps=T.north(T.P, 4000), date="2026:11:08 12:00:00")
        T.sidecar(self.path["n.jpg"], cam, date="2026:11:08 19:09:00")  # no GPS, 9 min after a.jpg
        self.native, self.errors, self.replies = [], [], []
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        return s

    def page_for(self, s, **ctx):
        c = self.browser.new_context(**ctx)
        c.set_default_timeout(B.STEP_MS)
        self.addCleanup(c.close)
        self.addCleanup(self.no_coordinates)  # before the context closes: the replies are still readable
        page = c.new_page()
        page.on("dialog", lambda d: (self.native.append(d.message), d.dismiss()))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("response", lambda r: self.replies.append(r) if "/api/" in r.url else None)
        self.open(page, s)
        self.addCleanup(lambda: self.assertEqual(self.native, [], "a native dialog fired"))
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))
        return page

    def no_coordinates(self):
        for r in self.replies:
            try:
                body = r.text()
            except B.PlaywrightError:
                continue  # a reply the page dropped (navigation): nothing reached it
            for v in T.P:
                self.assertNotIn(f"{abs(v)}", body, f"{r.url} carried a coordinate")

    def tile(self, page, name):
        return page.locator(f'[data-item="{name}"]').first

    def dlg(self, page):
        return page.locator("[data-dialog=confirm]")

    def add_place(self, page, name, radius="150 m"):
        viewer = page.get_by_role("dialog", name="Item")
        viewer.get_by_role("button", name="Add a place here…").click()
        dlg = self.dlg(page)
        B.expect(dlg.get_by_role("heading")).to_have_text("Add a place here?")
        field = dlg.get_by_label("Place name")
        B.expect(field).to_be_focused()
        field.fill(name)
        dlg.get_by_label("Radius").select_option(label=radius)
        dlg.get_by_role("button", name="Confirm").click()
        return dlg

    def test_add_a_place_then_browse_it(self):
        s = self.start()
        page = self.page_for(s)
        places = page.locator("#pllist")
        B.expect(places).to_contain_text("No places yet")
        # an item with GPS offers Add a place here…, one without doesn't
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, "far.jpg").click()
        B.expect(viewer).to_have_attribute("data-current", "far.jpg")
        B.expect(viewer.get_by_role("button", name="Add a place here…")).to_be_visible()
        page.keyboard.press("Escape")
        self.tile(page, "n.jpg").click()
        B.expect(viewer).to_have_attribute("data-current", "n.jpg")
        B.expect(viewer.get_by_role("button", name="Add a place here…")).to_have_count(0)
        page.keyboard.press("Escape")
        # from a.jpg's large view, 150 m: a.jpg and b.jpg get it, far.jpg doesn't
        self.tile(page, "a.jpg").click()
        B.expect(viewer).to_have_attribute("data-current", "a.jpg")
        dlg = self.add_place(page, T.NAME)
        B.expect(dlg).to_be_hidden()
        B.expect(page.locator("#toast")).to_contain_text(f"Added the place {T.NAME}: 2 items within 150 m tagged.")
        for n in ("a.jpg", "b.jpg"):
            self.assertIn(f"Place|{T.NAME}", T.hier(self.path[n]), n)
            self.assertIn(CITY, T.hier(self.path[n]), "the city tag stays")
        self.assertNotIn(f"Place|{T.NAME}", T.hier(self.path["far.jpg"]))
        dd = viewer.locator("dl.dt")
        B.expect(dd).to_contain_text("My place")
        B.expect(dd).to_contain_text(T.NAME)
        B.expect(dd).to_contain_text("Fernvale, Eastmarch, XA")
        # a look-alike is refused in the dialog, which stays open
        dlg = self.add_place(page, T.NAME.upper(), "300 m")
        B.expect(dlg.locator("#mout")).to_contain_text("already looks like that")
        B.expect(dlg).to_be_visible()
        dlg.get_by_role("button", name="Cancel").click()
        page.keyboard.press("Escape")
        # the sidebar row, its count, and a click showing its items
        row = places.locator(".nv")
        B.expect(row).to_have_count(1)
        B.expect(row.locator(".lbl")).to_have_text(T.NAME)
        B.expect(row.locator(".ct")).to_have_text("2")
        row.click()
        B.expect(row).to_have_attribute("aria-current", "page")
        B.expect(page.locator("#vtitle")).to_have_text(T.NAME)
        B.expect(page.locator("[data-item]")).to_have_count(2)
        self.assertEqual(sorted(page.locator("[data-item]").evaluate_all("e => e.map(x => x.dataset.item)")),
                         ["a.jpg", "b.jpg"])
        page.locator('.nv[data-v="lib"]').click()
        B.expect(page.locator("[data-item]")).to_have_count(4)
        B.expect(row).not_to_have_attribute("aria-current", "page")

    def test_probably_here_yes_and_no(self):
        s = self.start()
        code, j = s.post("/api/place/add", {"id": next(i["id"] for i in s.get("/api/list")["items"]
                                                       if i["n"] == "a.jpg"), "name": T.NAME, "radius": 100})
        self.assertEqual(code, 200, j)
        T.sidecar(self.path["far.jpg"], ["Project|Personal", "Source|phone camera"], date="2026:11:08 19:20:00")
        s.stop()
        s = self.start()  # far.jpg now has no GPS either: two questions to answer
        page = self.page_for(s)
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, "n.jpg").click()
        B.expect(viewer).to_have_attribute("data-current", "n.jpg")
        q = viewer.get_by_role("group", name=f"Probably at {T.NAME}")
        B.expect(q).to_contain_text("a photo from the same camera 9 min apart was there")
        self.assertNotIn(f"Place|{T.NAME}", T.hier(self.path["n.jpg"]), "a question writes nothing")
        q.get_by_role("button", name=f"Yes, {T.NAME}").click()
        B.expect(page.locator("#toast")).to_contain_text(f"Tagged as {T.NAME}.")
        B.expect(q).to_have_count(0)
        self.assertIn(f"Place|{T.NAME}", T.hier(self.path["n.jpg"]))
        B.expect(page.locator("#pllist .ct")).to_have_text("3")
        page.keyboard.press("Escape")
        self.tile(page, "far.jpg").click()
        B.expect(viewer).to_have_attribute("data-current", "far.jpg")
        q = viewer.get_by_role("group", name=f"Probably at {T.NAME}")
        B.expect(q).to_contain_text("20 min apart")
        q.get_by_role("button", name="No").click()
        B.expect(page.locator("#toast")).to_contain_text("won't be suggested again")
        B.expect(q).to_have_count(0)
        self.assertNotIn(f"Place|{T.NAME}", T.hier(self.path["far.jpg"]))

    def test_markup_name_is_text_and_a_phone_does_not_scroll_sideways(self):
        s = self.start()
        page = self.page_for(s, **PHONE)
        viewer = page.get_by_role("dialog", name="Item")
        self.tile(page, "a.jpg").tap()
        B.expect(viewer).to_have_attribute("data-current", "a.jpg")
        viewer.get_by_role("button", name="Add a place here…").tap()
        dlg = self.dlg(page)
        B.expect(dlg.get_by_label("Radius")).to_be_visible()
        g = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        self.assertLessEqual(g[0], g[1], "the page scrolls sideways with Add a place here… open")
        name = "<img src=x onerror=alert(1)>" + "W" * 30
        dlg.get_by_label("Place name").fill(name)
        dlg.get_by_role("button", name="Confirm").tap()
        B.expect(dlg).to_be_hidden()
        page.keyboard.press("Escape")
        page.locator("#rail").tap()
        row = page.locator("#pllist .nv")
        B.expect(row.locator(".lbl")).to_have_text(name)
        self.assertEqual(page.locator("#pllist img").count(), 0, "a name is text, never markup")
        g = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        self.assertLessEqual(g[0], g[1], "the drawer's long place row scrolls the page sideways")
        box, side = row.bounding_box(), page.locator("#sidebar").bounding_box()
        self.assertLessEqual(box["x"] + box["width"], side["x"] + side["width"] + 1,
                             "the place row runs out of the drawer")
        with open(os.path.join(self.state, "places.json")) as f:
            self.assertEqual(next(iter(json.load(f)["libraries"].values()))["places"].popitem()[1]["name"], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
