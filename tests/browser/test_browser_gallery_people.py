#!/usr/bin/env python3
"""The gallery's People & Pets in headless Chromium, on a synthetic recognition store.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_people.py

The store is tests/test_gallery_people.py's RecLibrary: built through recog's own API with
tests/test_recog.py's mock detector and embedder (no model), in a throwaway HOME; the gallery writes the tags
with the real exiftool. Covered: no People & Pets section without a store; with one, the section's "Name faces
& pets" entry and its "N to do", the naming view (To name, Suggestions, Named, Not a person; People / Pets):
naming a cluster by Enter (a junk-flagged photo in it leaving Suggested junk), its Undo (and back), merging through a "Looks like" hint, Not a person, a crop taken out by click
and put back by Space (aria-pressed), the sidebar's name and its filtered grid, suggestions confirmed and
rejected (a crop marked "Not <Name>", the confirm dialog saying it can't be undone) and the error after a
re-cluster with Reload the list; a name with markup shown as text everywhere (no element made from it); the
large view's People line filtering the grid and a video's "appears at" link seeking the player; the view at
390 px with no sideways scroll and the rail's word; the pressed tab's contrast in light and dark; the crops about
160 px on a desktop and two a row on a phone, the Crop size slider remembered after a reload; the zoom (its corner
button, Z on a crop) showing the crop and the box on the whole photo, the arrows, its toggle taking a face out and
Space putting it back, Tab kept inside, Esc back on the crop, a click on a crop still taking it out, and no sideways
scroll at 390 px; a pet seen once listed under "Single photos (1)" after the clusters and named by Enter; the bar over
a name's grid (Rename… on two clusters with a look-alike refused and Undo, Merge into…, Review suggestions (N), Remove
name…), the sidebar's pencil (hover, Tab, Esc back to it, 44 px and always shown on a phone) and the first-time hint,
Named grouped by name with Rename all…, the search box (focus kept, Esc, a cluster number), the large view's ⋯ with
Not this one and Rename…, and the bar, merge dialog and Named at 390 px in light and dark.

Hooks: #recsec / #reclist (the sidebar section, a .nv[data-who] per name), .nv[data-v=names] with #c-names,
#plugin[data-ready] after each render of the naming view, #whobar (role=toolbar "Person or pet": #whoname, #whosub,
#whoren, #whomerge, #whosugg, #whorm), .nvpen[data-whomenu] beside each name, #rechint, the name menu (the #modal
dialog named after the name, its .wmenu buttons), .rgroup headings in Named, #rq the search box, #who .wmore, a cluster as role=region named "cluster N" (or
"<Name>, cluster N"), a suggestion card as role=region "Looks like <Name>", crops as buttons (.rcrop) with
aria-pressed, each in a .rcropw with its zoom button (.rzb), the zoom #rzoom (role=dialog named "<card>: face N
of M"; #rzwhole, #rzbox, #rzout, #rzdone), #csize, #rsingle, #who in the large view. Every test also fails on an uncaught page error.
"""
import os, re, shutil, sys, tempfile, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (the server wrapper, sidecar reader)
import test_gallery_people as GP  # noqa: E402  (the synthetic recognition store)
import test_browser_gallery_layout as L  # noqa: E402  (the contrast measure)

PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
DESKTOP = dict(viewport={"width": 1440, "height": 900})
EVIL = '<img src=x onerror="window.__pwned=1">Ann'


class PeopleBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-people-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.errors = []
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))

    def world(self, video=False):
        w = GP.RecLibrary(self.tmp, video=False)
        self.addCleanup(w.close)
        return w

    def server(self, w):
        s = G.Server(w.env)
        self.addCleanup(s.stop)
        return s

    def page(self, s, size=DESKTOP, scheme="light"):
        ctx = self.browser.new_context(color_scheme=scheme, **size)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, s)
        return page

    def names_view(self, page, tab=None):
        page.locator(".nv[data-v=names]").click()
        B.expect(page.locator("#vtitle")).to_have_text("Name faces and pets")
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
        if tab:
            self.tab(page, tab)

    def tab(self, page, label):
        page.locator("#names .rseg button", has_text=re.compile("^" + re.escape(label))).click()
        B.expect(page.locator("#names .rseg button[aria-pressed=true]", has_text=re.compile("^" + re.escape(label)))) \
            .to_have_count(1)
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")

    def card(self, page, cl, name=None):
        return page.get_by_role("region", name=f"{name + ', ' if name else ''}cluster {cl}", exact=True)

    def toast(self, page, text):
        B.expect(page.locator("#toast")).to_contain_text(text)

    def api_name(self, s, w, cl, name, kind="face"):
        run = s.get("/api/rec?" + urllib.parse.urlencode(dict(kind=kind)))["run"]
        code, j = s.post("/api/rec/name", dict(kind=kind, run=run, cluster=cl, name=name))
        self.assertEqual(code, 200, j)

    def test_no_section_without_a_store(self):
        w = self.world()
        w.c.close()
        shutil.rmtree(w.store)
        page = self.page(self.server(w))
        B.expect(page.locator("#recsec")).to_be_hidden()
        B.expect(page.get_by_text("People & Pets")).to_be_hidden()
        self.assertFalse(os.path.exists(w.store))

    def test_name_undo_merge_ignore_take_out(self):
        w = self.world()
        G.write_tags(w.ann[2], ["Review|maybe-trash"])  # a content label's junk flag on a photo of ann
        s = self.server(w)
        page = self.page(s)
        B.expect(page.locator("#c-sug")).to_have_text("1")
        B.expect(page.locator("#recsec")).to_be_visible()
        B.expect(page.locator("#recsec .nvhead")).to_have_text("People & Pets")
        B.expect(page.locator("#c-names")).to_have_text("6 to do")  # 3 people clusters and 3 pets to name
        B.expect(page.locator(".nv[data-who]")).to_have_count(0)
        self.names_view(page)
        B.expect(page.locator("#names .rseg[aria-label='People or pets'] button")).to_have_text(["People3 to do", "Pets3 to do"])
        ann, bo, like = w.cluster_of(w.ann[0]), w.cluster_of(w.bo[0]), w.cluster_of(w.like[0])
        card = self.card(page, ann)
        B.expect(card.locator(".rcrop")).to_have_count(4)
        B.expect(card.locator(".rcrop img").first).to_have_js_property("naturalWidth", GP.recog.CROP_PX)
        # name by Enter; Undo; name again
        card.get_by_role("combobox", name=f"Name cluster {ann}").fill("Ann")
        card.get_by_role("combobox", name=f"Name cluster {ann}").press("Enter")
        self.toast(page, "named Ann")
        B.expect(page.locator(".nv[data-who='People|Ann'] .ct")).to_have_text("4")
        self.assertIn("People|Ann", GP.hier(w.ann[0]))
        B.expect(page.locator("#c-sug")).to_have_text("0")  # a confirmed name: never Suggested junk (owner.named)
        page.locator("#tundo").click()
        self.toast(page, "undid naming the cluster")
        B.expect(page.locator(".nv[data-who]")).to_have_count(0)
        B.expect(page.locator("#c-sug")).to_have_text("1")  # the name is gone, so is the protection
        self.assertNotIn("People|Ann", GP.hier(w.ann[0]))
        card = self.card(page, ann)
        card.get_by_role("combobox", name=f"Name cluster {ann}").fill("Ann")
        card.get_by_role("button", name="Name", exact=True).click()
        self.toast(page, "named Ann")
        # the look-alike: "Looks like Ann", merged through its hint (the dialog says it can't be undone)
        lk = self.card(page, like)
        B.expect(lk.locator(".rlike")).to_contain_text("Looks like Ann.")
        lk.get_by_role("button", name="Merge into Ann…").click()
        dlg = page.get_by_role("dialog", name=f"Merge cluster {like} into Ann?")
        B.expect(dlg).to_contain_text("This can't be undone here.")
        dlg.get_by_role("button", name="Merge").click()
        self.toast(page, f"Cluster {like} joined cluster {ann} (Ann).")
        B.expect(page.locator(".nv[data-who='People|Ann'] .ct")).to_have_text("6")
        # Not a person
        self.card(page, bo).get_by_role("button", name="Not a person").click()
        self.toast(page, "ignored")
        B.expect(self.card(page, bo)).to_have_count(0)
        self.tab(page, "Not a person")
        B.expect(self.card(page, bo).get_by_role("button", name="Not ignored")).to_be_visible()
        # take a face out by click, put it back by Space
        self.tab(page, "Named")
        named = self.card(page, ann, "Ann")
        det = w.dets(w.ann[1])[0]
        crop = named.locator(f".rcrop[data-det='{det}']")
        B.expect(crop).to_have_attribute("aria-label", "Take this face out of Ann")
        crop.click()
        B.expect(crop).to_have_attribute("aria-pressed", "true")
        B.expect(crop).to_have_attribute("aria-label", "Put this face back into Ann")
        self.assertNotIn("People|Ann", GP.hier(w.ann[1]))
        crop.focus()
        page.keyboard.press("Space")
        B.expect(crop).to_have_attribute("aria-pressed", "false")
        self.toast(page, "put back")
        self.assertIn("People|Ann", GP.hier(w.ann[1]))
        # the sidebar's name filters the grid by the confirmed tag
        page.locator(".nv[data-who='People|Ann']").click()
        B.expect(page.locator("#vtitle")).to_have_text("Ann")
        B.expect(page.locator(".nv[data-who='People|Ann']")).to_have_attribute("aria-current", "page")
        B.expect(page.locator("[data-item]")).to_have_count(6)
        B.expect(page.locator("#chips")).to_be_hidden()
        # a tile named here never shows in Suggested junk, whatever its label said
        self.assertIn("Review|maybe-trash", GP.hier(w.ann[2]))
        page.locator(".nv[data-v=sug]").click()
        B.expect(page.locator("#c-sug")).to_have_text("0")
        B.expect(page.locator(f"[data-item]:has-text('{os.path.basename(w.ann[2])}')")).to_have_count(0)
        B.expect(page.locator("[data-item]")).to_have_count(0)

    def test_suggestions_confirm_reject_and_a_stale_page(self):
        w = self.world()
        s = self.server(w)
        self.api_name(s, w, w.cluster_of(w.ann[0]), "Ann")
        new = w.more_ann(3)
        page = self.page(s)
        B.expect(page.locator("#c-names")).to_have_text("8 to do")  # 2 + 3 to name, 3 suggestions
        self.names_view(page, "Suggestions (3)")
        card = page.get_by_role("region", name="Looks like Ann")
        B.expect(card.locator("h3")).to_have_text("Looks like Ann: 3")
        B.expect(card.locator(".rcrop")).to_have_count(3)
        confirm = card.locator(".racts .btn.primary")
        B.expect(confirm).to_have_text("Confirm all 3")
        no = w.dets(new[2])[0]
        card.locator(f".rcrop[data-det='{no}']").click()
        B.expect(card.locator(f".rcrop[data-det='{no}']")).to_have_attribute("aria-pressed", "true")
        B.expect(confirm).to_have_text("Confirm 2, reject 1")
        confirm.click()
        dlg = page.get_by_role("dialog", name="Confirm 2 faces as Ann?")
        B.expect(dlg).to_contain_text("is never suggested for it again")
        B.expect(dlg).to_contain_text("This can't be undone here")
        dlg.get_by_role("button", name="Confirm").click()
        self.toast(page, "2 confirmed, 1 rejected.")
        B.expect(page.locator("#names .empty")).to_contain_text("No suggestions waiting.")
        self.assertIn("People|Ann", GP.hier(new[0]))
        self.assertNotIn("People|Ann", GP.hier(new[2]))
        # a page listed before a re-cluster: the server refuses (409) and the error offers to reload
        w.more_ann(1)
        self.tab(page, "To name")
        bo = w.cluster_of(w.bo[0])
        w.more_ann(1)
        self.card(page, bo).get_by_role("button", name="Not a person").click()
        err = page.locator("#etoast")
        B.expect(err).to_contain_text("The clusters changed since this page loaded")
        err.get_by_role("button", name="Reload the list").click()
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
        self.card(page, w.cluster_of(w.bo[0])).get_by_role("button", name="Not a person").click()
        self.toast(page, "ignored")

    def test_names_are_text_everywhere(self):
        w = self.world()
        s = self.server(w)
        page = self.page(s)
        self.names_view(page)
        ann = w.cluster_of(w.ann[0])
        box = self.card(page, ann).get_by_role("combobox", name=f"Name cluster {ann}")
        box.fill(EVIL)
        box.press("Enter")
        self.toast(page, "named " + EVIL)
        B.expect(page.locator(f".nv[data-who='People|{EVIL}'] .lbl")).to_have_text(EVIL)
        self.tab(page, "Named")
        B.expect(page.locator("#names .rcard h3 .rname")).to_have_text(EVIL)
        page.locator(f".nv[data-who='People|{EVIL}']").click()
        B.expect(page.locator("#vtitle")).to_have_text(EVIL)
        page.locator("[data-item]").first.click()
        who = page.locator("#who")
        B.expect(who).to_contain_text("People: " + EVIL)
        B.expect(page.locator("#mytagl")).to_have_text("none")  # the name is in the People line only
        self.assertEqual(page.locator("#tooltagl").count(), 0, "no tool tags shown either")
        self.assertEqual(page.evaluate("() => [document.querySelectorAll('img[src=x]').length, window.__pwned]"),
                         [0, None])
        who.get_by_role("button", name=EVIL, exact=True).click()
        B.expect(page.locator("#viewer")).not_to_have_class(re.compile(r"\bon\b"))
        B.expect(page.locator("#vtitle")).to_have_text(EVIL)
        # the filtered view's bar and the name menu: text too
        B.expect(page.locator("#whoname")).to_have_text("Person: " + EVIL)
        page.locator("#whoren").click()
        dlg = page.get_by_role("dialog", name=f"Rename {EVIL}?")
        B.expect(page.locator("#mfield")).to_have_value(EVIL)
        dlg.get_by_role("button", name="Cancel").click()
        page.locator(f".nvpen[data-whomenu='People|{EVIL}']").click()
        B.expect(page.get_by_role("dialog", name=EVIL, exact=True)).to_be_visible()
        B.expect(page.locator("#mh")).to_have_text(EVIL)
        page.keyboard.press("Escape")
        self.assertEqual(page.evaluate("() => [document.querySelectorAll('img[src=x]').length, window.__pwned]"),
                         [0, None])

    def two_names(self, s, w):
        """Ann on two clusters (ann's and the look-alike's, named "ann": the existing spelling wins), Bo on one."""
        for cl, name in ((w.cluster_of(w.ann[0]), "Ann"), (w.cluster_of(w.like[0]), "ann"), (w.cluster_of(w.bo[0]), "Bo")):
            self.api_name(s, w, cl, name)

    def test_the_names_bar_renames_merges_reviews_and_removes(self):
        """A name's grid (the sidebar's name) has the bar: Person, its counts, Rename… (every cluster at once, a
        look-alike refused in the dialog, Undo), Merge into…, Review suggestions (N) and Remove name…."""
        w = self.world()
        s = self.server(w)
        self.two_names(s, w)
        page = self.page(s)
        page.locator(".nv[data-who='People|Ann']").click()
        bar = page.get_by_role("toolbar", name="Person or pet")
        B.expect(bar).to_be_visible()
        B.expect(page.locator("#whoname")).to_have_text("Person: Ann")
        B.expect(page.locator("#whosub")).to_have_text("6 files · 2 clusters")
        B.expect(page.locator("#whosugg")).to_be_hidden()
        B.expect(page.locator("[data-item]")).to_have_count(6)
        # Rename…: a look-alike of Bo refused in the dialog, then a new name for both clusters
        bar.get_by_role("button", name="Rename…").click()
        dlg = page.get_by_role("dialog", name="Rename Ann?")
        B.expect(dlg).to_contain_text("2 clusters in 6 files")
        B.expect(page.locator("#mfield")).to_have_value("Ann")
        page.locator("#mfield").fill(" BO ")
        dlg.get_by_role("button", name="Rename").click()
        B.expect(page.locator("#mout")).to_contain_text("looks like Bo")
        page.locator("#mfield").fill("annie")  # as typed: never capitalised
        page.locator("#mfield").press("Enter")
        self.toast(page, "Ann is now annie")
        B.expect(page.locator("#vtitle")).to_have_text("annie")
        B.expect(page.locator("#whoname")).to_have_text("Person: annie")
        B.expect(page.locator(".nv[data-who='People|annie'] .ct")).to_have_text("6")
        B.expect(page.locator(".nv[data-who='People|Ann']")).to_have_count(0)
        B.expect(page.locator("[data-item]")).to_have_count(6)
        for p in w.ann + w.like:
            self.assertIn("People|annie", GP.hier(p))
            self.assertNotIn("People|Ann", GP.hier(p))
        page.locator("#tundo").click()
        self.toast(page, "undid renaming Ann")
        B.expect(page.locator("#vtitle")).to_have_text("Ann")
        B.expect(page.locator("[data-item]")).to_have_count(6)
        self.assertIn("People|Ann", GP.hier(w.like[0]))
        # Merge into…: the other names of the kind, in a list
        bar.get_by_role("button", name="Merge into…").click()
        dlg = page.get_by_role("dialog", name="Merge Ann into another name?")
        B.expect(page.locator("#mergeto option")).to_have_text(["Bo"])
        dlg.get_by_role("button", name="Merge").click()
        self.toast(page, "Ann joined Bo")
        B.expect(page.locator("#vtitle")).to_have_text("Bo")
        B.expect(page.locator("[data-item]")).to_have_count(8)
        self.assertIn("People|Bo", GP.hier(w.like[1]))
        page.locator("#tundo").click()
        self.toast(page, "undid merging Ann into Bo")
        B.expect(page.locator("#vtitle")).to_have_text("Ann")
        # suggestions waiting: Review suggestions (N) opens them in the naming view, found by the name
        w.more_ann(2)
        page.reload()
        page.locator(".nv[data-who='People|Ann']").click()
        B.expect(page.locator("#whosub")).to_have_text("6 files · 2 clusters · 2 suggestions waiting")
        B.expect(page.locator("#whosugg")).to_have_text("Review suggestions (2)")
        page.locator("#whosugg").click()
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
        B.expect(page.locator("#names .rseg[aria-label=Show] button[aria-pressed=true]")).to_have_text("Suggestions (2)")
        B.expect(page.locator("#rq")).to_have_value("Ann")
        B.expect(page.get_by_role("region", name="Looks like Ann")).to_be_visible()
        # Remove name…: asks, the tags come off, the clusters go back to To name
        page.locator(".nv[data-who='People|Ann']").click()
        bar.get_by_role("button", name="Remove name…").click()
        dlg = page.get_by_role("dialog", name="Remove the name Ann?")
        B.expect(dlg).to_contain_text("Your own tags stay")
        dlg.get_by_role("button", name="Remove name").click()
        self.toast(page, "Ann removed")
        B.expect(page.locator(".nv[data-who='People|Ann']")).to_have_count(0)
        B.expect(page.locator("#vtitle")).to_have_text("Library")  # the name is gone: back to the whole library
        B.expect(page.locator("#whobar")).to_be_hidden()
        for p in w.ann + w.like:
            self.assertFalse(any(t.startswith("People|Ann") for t in GP.hier(p)), p)
        self.assertIn("People|Bo", GP.hier(w.bo[0]))

    def test_sidebar_pencil_hint_and_menu(self):
        w = self.world()
        s = self.server(w)
        page = self.page(s)
        B.expect(page.locator("#rechint")).to_be_hidden()  # nothing named yet: no hint
        self.api_name(s, w, w.cluster_of(w.ann[0]), "Ann")
        page.reload()
        B.expect(page.locator(".nv[data-v=names] .lbl")).to_have_text("Name faces & pets")
        B.expect(page.locator("#rechint")).to_contain_text("Click a name to see its photos")
        pen = page.locator(".nvpen[data-whomenu='People|Ann']")
        B.expect(pen).to_have_attribute("aria-label", "Rename, merge or remove: Ann")
        self.assertEqual(pen.evaluate("e => getComputedStyle(e).opacity"), "0")  # shown on hover or focus
        page.locator(".nv[data-who='People|Ann']").hover()
        page.wait_for_function("() => getComputedStyle(document.querySelector('.nvpen')).opacity === '1'")
        # by keyboard: Tab from the name reaches its pencil, Enter opens the menu, Esc gives focus back
        page.locator(".nv[data-who='People|Ann']").focus()
        page.keyboard.press("Tab")
        B.expect(pen).to_be_focused()
        self.assertEqual(pen.evaluate("e => getComputedStyle(e).opacity"), "1")
        page.keyboard.press("Enter")
        menu = page.get_by_role("dialog", name="Ann", exact=True)
        B.expect(menu).to_contain_text("Person · 4 files · 1 cluster")
        B.expect(menu.locator(".wmenu button")).to_have_text(["Rename…", "Show its photos", "Remove name…"])  # no one to merge into
        B.expect(menu.locator(".wmenu button").first).to_be_focused()
        B.expect(page.locator("#rechint")).to_be_hidden()
        page.keyboard.press("Escape")
        B.expect(menu).to_be_hidden()
        B.expect(pen).to_be_focused()
        page.locator(".nv[data-who='People|Ann']").click()
        B.expect(page.locator("#whomerge")).to_be_hidden()  # no other person named yet
        pen.click()
        menu.get_by_role("button", name="Rename…").click()
        page.locator("#mfield").fill("Annie")
        page.get_by_role("dialog", name="Rename Ann?").get_by_role("button", name="Rename").click()
        self.toast(page, "Ann is now Annie")
        B.expect(page.locator(".nv[data-who='People|Annie']")).to_be_visible()
        B.expect(page.locator(".nvpen[data-whomenu='People|Annie']")).to_be_focused()  # the rebuilt pencil
        self.assertIn("People|Annie", GP.hier(w.ann[0]))
        page.reload()
        B.expect(page.locator(".nv[data-who='People|Annie']")).to_be_visible()
        B.expect(page.locator("#rechint")).to_be_hidden()  # seen once: not shown again
        # on a touch phone: the pencil always shown, a 44 px target
        phone = self.page(s, PHONE)
        phone.locator("#rail").tap()  # the drawer
        pen = phone.locator(".nvpen[data-whomenu='People|Annie']")
        B.expect(pen).to_be_visible()
        self.assertEqual(pen.evaluate("e => getComputedStyle(e).opacity"), "1")
        bb = pen.bounding_box()
        self.assertGreaterEqual(min(bb["width"], bb["height"]), 44)

    def test_named_grouped_by_name_and_the_search_box(self):
        w = self.world()
        s = self.server(w)
        self.two_names(s, w)
        page = self.page(s)
        self.names_view(page)
        kinds = page.locator("#names .rseg[aria-label='People or pets'] button")
        B.expect(kinds).to_have_text(["People", "Pets3 to do"])
        B.expect(kinds.first).to_have_attribute("aria-pressed", "true")
        self.tab(page, "Named (3)")
        heads = page.locator("#rcards .rgroup")
        B.expect(heads.locator("h2")).to_have_text(["Ann", "Bo"])
        B.expect(heads.locator(".rsub")).to_have_text(["2 clusters", "1 cluster"])
        order = page.evaluate("""() => [...document.querySelectorAll('#rcards > .rgroup, #rcards > .rcard')]
            .map(e => e.classList.contains('rgroup') ? 'H:' + e.dataset.name : e.querySelector('.rname').textContent)""")
        self.assertEqual(order, ["H:Ann", "Ann", "Ann", "H:Bo", "Bo"])
        ann = self.card(page, w.cluster_of(w.ann[0]), "Ann")
        B.expect(ann.get_by_role("button", name="Rename this cluster")).to_be_visible()
        B.expect(self.card(page, w.cluster_of(w.bo[0]), "Bo").get_by_role("button", name="Rename", exact=True)).to_be_visible()
        # Rename all…: the name on both clusters at once
        page.get_by_role("group", name="Ann", exact=True).get_by_role("button", name="Rename all…").click()
        page.locator("#mfield").fill("Annie")
        page.locator("#mfield").press("Enter")
        self.toast(page, "Ann is now Annie")
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
        B.expect(heads.locator("h2")).to_have_text(["Annie", "Bo"])
        self.assertIn("People|Annie", GP.hier(w.like[0]))
        # the search box: typing keeps the focus; a name (any case) or a cluster number; Esc clears it
        q = page.get_by_role("searchbox", name="Find a name or a cluster number")
        q.fill("bO")
        B.expect(page.locator("#names .rfound")).to_have_text("1 cluster with “bo” in the name (or that number).")
        B.expect(page.locator("#rcards .rcard")).to_have_count(1)
        B.expect(q).to_be_focused()
        B.expect(q).to_have_value("bO")
        q.press("Escape")
        B.expect(page.locator("#rcards .rcard")).to_have_count(3)
        B.expect(page.locator("#names .rfound")).to_have_count(0)
        kinds.nth(1).click()  # Pets: its clusters still to name; the search box is emptied for the other kind
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
        B.expect(page.locator("#rq")).to_have_value("")
        self.tab(page, "To name")
        cl = page.locator("#rcards .rcard").first.get_attribute("data-cluster")
        page.locator("#rq").fill(cl)
        B.expect(page.locator("#rcards .rcard")).to_have_count(1)
        B.expect(page.locator("#rcards .rcard")).to_have_attribute("data-cluster", cl)

    def test_large_view_name_menu(self):
        w = self.world()
        s = self.server(w)
        self.api_name(s, w, w.cluster_of(w.ann[0]), "Ann")
        page = self.page(s)
        page.locator(".nv[data-who='People|Ann']").click()
        page.locator("[data-item='ann1.png']").click()
        who = page.locator("#who")
        more = who.get_by_role("button", name="Rename, merge or remove: Ann")
        more.click()
        menu = page.get_by_role("dialog", name="Ann", exact=True)
        B.expect(menu.locator(".wmenu button")).to_have_text(["Rename…", "Not this one", "Show its photos", "Remove name…"])
        menu.get_by_role("button", name="Not this one").click()
        dlg = page.get_by_role("dialog", name="Not Ann in ann1.png?")
        B.expect(dlg).to_contain_text("Other files keep it")
        dlg.get_by_role("button", name="Not Ann").click()
        self.toast(page, "Ann taken out of this file")
        B.expect(page.locator("#who")).to_have_count(0)
        self.assertNotIn("People|Ann", GP.hier(w.ann[1]))
        self.assertIn("People|Ann", GP.hier(w.ann[0]))
        page.locator("#tundo").click()
        self.toast(page, "undid taking out")
        B.expect(page.locator("#who")).to_contain_text("People: Ann")
        self.assertIn("People|Ann", GP.hier(w.ann[1]))
        # Rename… from there: the line shows the new name
        page.locator("#who").get_by_role("button", name="Rename, merge or remove: Ann").click()
        menu.get_by_role("button", name="Rename…").click()
        page.locator("#mfield").fill("Annie")
        page.locator("#mfield").press("Enter")
        self.toast(page, "Ann is now Annie")
        B.expect(page.locator("#who")).to_contain_text("People: Annie")
        B.expect(page.locator("#viewer")).to_have_class(re.compile(r"\bon\b"))

    def test_names_bar_and_menu_on_a_phone(self):
        w = self.world()
        s = self.server(w)
        self.api_name(s, w, w.cluster_of(w.ann[0]), "A" * 64)
        self.api_name(s, w, w.cluster_of(w.bo[0]), "Bo")
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                page = self.page(s, PHONE, scheme)
                page.locator("#rail").tap()
                page.locator(f".nv[data-who='People|{'A' * 64}']").tap()
                B.expect(page.locator("#whobar")).to_be_visible()
                for q in ("#whoren", "#whomerge", "#whorm"):
                    bb = page.locator(q).bounding_box()
                    self.assertLessEqual(bb["x"] + bb["width"], 390.5, q)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                                     page.evaluate("document.documentElement.clientWidth"), "bar")
                self.assertGreaterEqual(page.locator("#whosub").evaluate(L.CONTRAST), 4.5)
                page.locator("#whomerge").tap()
                B.expect(page.locator("#mergeto")).to_be_visible()
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                                     page.evaluate("document.documentElement.clientWidth"), "merge dialog")
                page.keyboard.press("Escape")
                entry = page.locator(".nv[data-v=names]")
                page.locator("#rail").tap()
                entry.tap()
                B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
                self.tab(page, "Named")
                B.expect(page.locator("#rq")).to_be_visible()
                for q in ("#rq", "#rcards .rgroup .btn >> nth=0"):
                    bb = page.locator(q).bounding_box()
                    self.assertLessEqual(bb["x"] + bb["width"], 390.5, q)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                                     page.evaluate("document.documentElement.clientWidth"), "named")
                self.assertGreaterEqual(page.locator("#rcards .rgroup .rsub").first.evaluate(L.CONTRAST), 4.5)

    def test_crop_size_zoom_and_click_still_takes_out(self):
        w = self.world()
        s = self.server(w)
        page = self.page(s)
        self.names_view(page)
        B.expect(page.locator("#csize")).to_be_visible()
        B.expect(page.locator("#tsize")).to_be_hidden()
        ann = w.cluster_of(w.ann[0])
        card = self.card(page, ann)
        crop = card.locator(".rcrop").first
        width = crop.bounding_box()["width"]
        self.assertTrue(150 <= width <= 260, width)  # about 160 px on a desktop
        # the slider: bigger crops, remembered after a reload
        page.locator("#csize").fill("240")
        page.wait_for_function("() => document.querySelector('#names .rcrop').getBoundingClientRect().width >= 240")
        page.reload()
        self.names_view(page)
        B.expect(page.locator("#csize")).to_have_value("240")
        self.assertGreaterEqual(self.card(page, ann).locator(".rcrop").first.bounding_box()["width"], 240)
        page.locator("#csize").fill("160")
        # the zoom button opens the zoom; the crop is not taken out
        card = self.card(page, ann)
        cells = card.locator(".rcropw")
        first = cells.nth(0).locator(".rcrop")
        zb = cells.nth(0).locator(".rzb")
        self.assertGreaterEqual(zb.bounding_box()["width"], 32)
        B.expect(zb).to_have_attribute("aria-label", "Zoom in on this face (Z)")
        zb.click()
        dlg = page.get_by_role("dialog", name=f"Cluster {ann}: face 1 of 4")
        B.expect(dlg).to_be_visible()
        B.expect(first).to_have_attribute("aria-pressed", "false")
        B.expect(page.locator("#rzcrop")).to_have_js_property("naturalWidth", GP.recog.CROP_PX)
        B.expect(page.locator("#rzbox")).to_be_visible()
        B.expect(page.locator("#rzsub")).to_contain_text("In ")
        # the box sits where the detection is on the whole picture
        det = int(first.get_attribute("data-det"))
        x, y, bw, bh = w.c.execute("SELECT x, y, w, h FROM detections WHERE id=?", (det,)).fetchone()
        im, bx = page.locator("#rzwhole").bounding_box(), page.locator("#rzbox").bounding_box()
        self.assertAlmostEqual(bx["x"], im["x"] + x * im["width"], delta=2)
        self.assertAlmostEqual(bx["width"], bw * im["width"], delta=2)
        self.assertAlmostEqual(bx["y"], im["y"] + y * im["height"], delta=2)
        # arrows step through the cluster; the toggle takes that face out, as a click on it does
        B.expect(page.locator("#rzprev")).to_be_disabled()
        page.keyboard.press("ArrowRight")
        B.expect(page.locator("#rzh")).to_have_text(f"Cluster {ann}: face 2 of 4")
        second = cells.nth(1).locator(".rcrop")
        out = page.locator("#rzout")
        B.expect(out).to_have_text("Take this face out")
        out.click()
        B.expect(out).to_have_attribute("aria-pressed", "true")
        B.expect(second).to_have_attribute("aria-pressed", "true")
        B.expect(out).to_have_text("Put this face back")
        self.toast(page, "taken out")
        page.keyboard.press("Space")  # the toggle keeps focus: Space puts it back
        B.expect(second).to_have_attribute("aria-pressed", "false")
        for _ in range(3):  # Tab stays in the zoom
            page.keyboard.press("Tab")
            self.assertTrue(page.evaluate("() => !!document.activeElement.closest('#rzoom, #toast, #etoast')"))
        page.keyboard.press("Escape")
        B.expect(dlg).to_be_hidden()
        B.expect(second).to_be_focused()
        # Z on the focused crop opens it at that crop; a click on a crop still takes it out
        page.keyboard.press("z")
        B.expect(page.locator("#rzh")).to_have_text(f"Cluster {ann}: face 2 of 4")
        page.locator("#rzdone").click()
        B.expect(page.locator("#rzoom")).to_be_hidden()
        first.click(position={"x": 20, "y": 20})
        B.expect(first).to_have_attribute("aria-pressed", "true")
        B.expect(page.locator("#rzoom")).to_be_hidden()

    def test_single_photo_pets_are_nameable(self):
        w = self.world()
        w.world.scene(8, ("solo", "dog"))
        solo = GP.TR.image(os.path.join(w.lib, "2026/2026-05/solo.png"), 8)
        GP.recog.scan(w.c, w.pet, [solo], frames=None, log=lambda *a: None, state=w.state)
        GP.recog.recluster(w.c, w.pet)
        s = self.server(w)
        page = self.page(s)
        B.expect(page.locator("#c-names")).to_have_text("7 to do")  # 3 people, 3 pets of two, 1 pet seen once
        self.names_view(page)
        page.locator("#names .rseg[aria-label='People or pets'] button", has_text="Pets").click()
        B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
        B.expect(page.locator("#names .rseg[aria-label=Show] button[aria-pressed=true]")).to_have_text("To name (4)")
        head = page.locator("#rsingle")
        B.expect(head).to_have_text("Single photos (1)")
        cl = w.cluster_of(solo, "pet")
        card = self.card(page, cl)
        self.assertTrue(page.evaluate("""cl => { const h = document.querySelector('#rsingle'), c = [...document
            .querySelectorAll('#rcards .rcard')]; return c.every(x => (x.dataset.cluster == cl) ===
            !!(h.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_FOLLOWING)) }""", cl))
        B.expect(card.locator(".rcrop")).to_have_count(1)
        B.expect(card.get_by_role("button", name="Merge into…")).to_be_visible()
        B.expect(card.get_by_role("button", name="Not a pet")).to_be_visible()
        box = card.get_by_role("combobox", name=f"Name cluster {cl}")
        box.fill("Solo")
        box.press("Enter")
        self.toast(page, "named Solo")
        self.assertIn("Pets|Solo", GP.hier(solo))
        B.expect(page.locator(".nv[data-who='Pets|Solo'] .ct")).to_have_text("1")
        B.expect(page.locator("#rsingle")).to_have_count(0)

    def test_phone_layout_and_contrast(self):
        w = self.world()
        s = self.server(w)
        self.api_name(s, w, w.cluster_of(w.ann[0]), "A" * 64)
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                page = self.page(s, PHONE, scheme)
                entry = page.locator(".nv[data-v=names]")
                B.expect(entry.locator(".rl")).to_have_text("Names")  # the rail's word
                entry.tap()
                B.expect(page.locator("#plugin")).to_have_attribute("data-ready", "1")
                for tab in ("To name", "Named", "Suggestions"):
                    self.tab(page, tab)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                                         page.evaluate("document.documentElement.clientWidth"), tab)
                pressed = page.locator("#names .rseg[aria-label=Show] button[aria-pressed=true]")
                self.assertGreaterEqual(pressed.evaluate(L.CONTRAST), 4.5)
                self.assertGreaterEqual(page.locator("#names > .note").evaluate(L.CONTRAST), 4.5)
                self.tab(page, "Named")
                card = page.locator("#names .rcard").first
                inp = card.get_by_role("combobox")
                box, cbox = inp.bounding_box(), card.bounding_box()
                self.assertLessEqual(box["x"] + box["width"], cbox["x"] + cbox["width"] + 0.5)
                cols = card.locator(".rcrops").evaluate("g => getComputedStyle(g).gridTemplateColumns.split(' ').length")
                self.assertEqual(cols, 2, "two crops a row on a phone")
                self.assertGreaterEqual(card.locator(".rcrop").first.bounding_box()["width"], 100)
                zb = card.locator(".rzb").first
                self.assertGreaterEqual(zb.bounding_box()["width"], 44)
                zb.tap()
                B.expect(page.locator("#rzoom")).to_be_visible()
                B.expect(page.locator("#rzbox")).to_be_visible()
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                                     page.evaluate("document.documentElement.clientWidth"), "zoom")
                for q in ("#rzwhole", "#rzcrop", "#rzout"):
                    bb = page.locator(q).bounding_box()
                    self.assertLessEqual(bb["x"] + bb["width"], 390.5, q)
                page.keyboard.press("Escape")
                B.expect(page.locator("#rzoom")).to_be_hidden()

    def test_sidebar_order_and_no_sideways_scroll(self):
        """Quick views (Dates to check last), then Events, Places and People & Pets, at 1000 px and on a phone."""
        w = self.world()
        s = self.server(w)
        self.api_name(s, w, w.cluster_of(w.ann[0]), "Ann")
        for size in (dict(viewport={"width": 1000, "height": 800}), PHONE):
            with self.subTest(width=size["viewport"]["width"]):
                page = self.page(s, size)
                B.expect(page.locator("#recsec")).to_be_attached()
                order = page.evaluate("""() => ['.nv[data-v=fav]', '.nv[data-v=untagged]', '.nv[data-v=dates]',
                    '#evsec', '#plsec', '#recsec', '.nv[data-v=names]'].map(q => document.querySelector(q))
                    .every((e, i, a) => e && (i === 0 || (a[i - 1].compareDocumentPosition(e)
                    & Node.DOCUMENT_POSITION_FOLLOWING)))""")
                self.assertTrue(order, "sidebar sections out of order")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                                     page.evaluate("document.documentElement.clientWidth"))
                B.expect(page.locator(".nv[data-who='People|Ann']")).to_be_attached()

    def test_video_appears_at_seeks_the_player(self):
        w = GP.RecLibrary(self.tmp, video=False)
        self.addCleanup(w.close)
        clip = os.path.join(w.vids, "2026/2026-07/clip.mp4")
        os.makedirs(os.path.dirname(clip), exist_ok=True)
        shutil.copy(self.clip(), clip)
        rgb = GP.TR.np.zeros((100, 200, 3), dtype=GP.TR.np.uint8)
        rgb[0, 0, 0] = 1
        GP.recog.scan(w.c, w.face, [clip], frames=lambda p: [(2.0, rgb), (25.0, rgb)], log=lambda *a: None,
                      state=w.state)
        GP.recog.recluster(w.c, w.face)
        s = self.server(w)
        self.api_name(s, w, w.cluster_of(w.ann[0]), "Ann")
        page = self.page(s)
        page.locator(".nv[data-who='People|Ann']").click()
        B.expect(page.locator("[data-item]")).to_have_count(5)
        page.locator("[data-item='clip.mp4']").click()
        who = page.locator("#who")
        B.expect(who.locator(".appear")).to_have_text("Ann appears at 0:02, 0:25")
        video = page.locator("#media video")
        video.evaluate(B.METADATA)
        who.get_by_role("button", name="Ann at 0:25: play from there").click()
        page.wait_for_function("() => { const v = document.querySelector('#media video');"
                               " return v && v.currentTime >= 25 && !v.seeking }")


if __name__ == "__main__":
    import unittest
    unittest.main()
