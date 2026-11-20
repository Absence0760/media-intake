#!/usr/bin/env python3
"""The gallery page's in-page dialogs, messages and Undo in headless Chromium, on synthetic media in a
throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_dialogs.py

No native dialog: every test fails if alert(), confirm() or prompt() fires (page.on("dialog")) or the page
throws. Covered: the tag field (my-tag suggestions from /api/facets, the tools' namespaces refused before
anything is sent, Enter to confirm), focus moving into a dialog, staying in it on Tab and going back on Esc;
an error (a refused Keep, a 409 while a job holds the library lock) shown as role=alert until dismissed, past
the time the ordinary message fades (Playwright's clock, no sleep); the "?" shortcut list, opened by the key
and by the sidebar's Keyboard shortcuts, listing every key the page's handlers check (read from the served
page's own source); Undo of Rotate, Keep (by Ctrl+Z), a tag added in the large view and Set event, each
putting the sidecar back as it was, an Undo refused once the item changed again, and none offered after Trash,
Erase or a Share. Plain words: the job dialog's end state (Hide while it runs, the header's pill,
Close and a role=status line when it ends, a message when it ends while hidden), the busy refusal naming what
runs (no PID, no lock path) with Show progress, the Keep message, and no tool namespace (Owner|, Review|,
Label|) in any text the page shows; on a phone the message sits above the action bar.

Hooks (beside tests/browser/test_browser_gallery.py's): the message as role=status (#toast) with its Undo
button; the error as role=alert (#etoast) with "Dismiss error"; the shortcut list as role=dialog named
"Keyboard shortcuts", its keys as <kbd>; the confirm dialog's text field by its label ("Tag (yours", "Event
name"), its refusal in its <pre>; the job's pill as the header button "<Trash> running… Show". The real intake.py runs Trash, Erase and share-copy; the lock test swaps in
tests/test_gallery.py's GATED fake, held at a FIFO.
"""
import os, re, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper only)

# key names in the page's handlers -> how the shortcut list writes them
KEY_LABEL = {"Escape": "Esc", "ArrowRight": "→", "ArrowLeft": "←", "ArrowUp": "↑", "ArrowDown": "↓", " ": "Space"}


class GalleryDialogsBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-dialogs-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)  # the real intake.py
        B.fake_opener(self.tmp, self.env, G.FAKE_OPENER)  # a Share's folder: never really opened
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        self.path = {"a.jpg": L("2026/2026-11/a.jpg"), "b.jpg": L("2026/2026-11/b.jpg"),
                     "c.png": L("2026/2026-11/c.png"), "d.jpg": L("2026/2026-10/d.jpg")}
        for (n, p), col in zip(self.path.items(), ((200, 30, 30), (30, 200, 30), (30, 30, 200), (90, 90, 90))):
            G.img(p, col, (400, 300))
        G.write_tags(self.path["a.jpg"], ["Project|Personal", "Review|maybe-trash", "Event|picnic"],
                     ["-XMP-iptcExt:Event=picnic"])
        G.write_tags(self.path["d.jpg"], ["Trips|Spain", "Family|Grandma"])  # my tags: the field's suggestions
        self.orig = {n: G.sha(p) for n, p in self.path.items()}
        self.servers = []
        self.native, self.errors = [], []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        return s

    def page_for(self, s, clock=False):
        page = self.new_page()
        page.on("dialog", lambda d: (self.native.append(f"{d.type}: {d.message}"), d.dismiss()))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        if clock:
            page.clock.install()  # timers move only when the test moves them: no sleeps
        self.open(page, s)
        self.addCleanup(lambda: self.assertEqual(self.native, [], "a native dialog fired"))
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "uncaught errors on the page"))
        return page

    def box(self, page, n):
        return page.get_by_role("checkbox", name=f"select {n}", exact=True)

    def bar(self, page):
        return page.get_by_role("toolbar", name="Selection actions")

    def dlg(self, page):
        return page.locator("[data-dialog=confirm]")

    def sidecar(self, n):
        return G.tags_of(self.path[n]) + (self.event_of(n),)

    def event_of(self, n):
        x = self.path[n] + ".xmp"
        if not os.path.exists(x):
            return None
        r = subprocess.run(["exiftool", "-q", "-s3", "-XMP-iptcExt:Event", x], capture_output=True, text=True)
        return r.stdout.strip() or None

    def in_dialog(self, page, sel):
        return page.evaluate(f"() => !!document.activeElement.closest({sel!r})")

    def assert_originals(self):
        for n, p in self.path.items():
            if os.path.exists(p):
                self.assertEqual(G.sha(p), self.orig[n], n)

    # ------------------------------------------------------------ the tag field
    def test_tag_field_suggests_my_tags_and_refuses_the_tools(self):
        s = self.start()
        page = self.page_for(s)
        dlg, bar = self.dlg(page), self.bar(page)
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        self.box(page, "b.jpg").click()
        plus = B.more(page, "+ tag")
        plus.click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Add a tag of yours to 1 file?")
        field = dlg.get_by_label("Tag (yours")
        B.expect(field).to_be_focused()
        # suggestions: the field's list holds every tag of mine in the library, never a tool's
        self.assertEqual(field.get_attribute("list"), "mytags")
        opts = page.locator("#mytags option").evaluate_all("os => os.map(o => o.value)")
        self.assertEqual(sorted(opts), ["Family|Grandma", "Trips|Spain"])
        # Tab stays in the dialog
        for _ in range(6):
            page.keyboard.press("Tab")
            self.assertTrue(self.in_dialog(page, "#modal"))
        page.keyboard.press("Shift+Tab")
        self.assertTrue(self.in_dialog(page, "#modal"))
        # the tools' namespaces (any case) and a bad tag are refused in the dialog; nothing is sent
        tools, shape = "belong to the tools; pick another name", "a tag is 1-64 characters on one line"
        for bad, why in (("Owner|keep", tools), ("-x", shape), ("label|food", tools), ("x" * 65, shape),
                         ("AI|vlm/x", tools)):
            field.fill(bad)
            dlg.get_by_role("button", name="Confirm").click()
            B.expect(dlg.locator("pre")).to_contain_text(why)  # alternating, so each check sees a new message
            B.expect(dlg).to_be_visible()
        self.assertFalse([u for u in posts if u.endswith("/api/tag")])
        # Esc closes it and focus goes back to More…, the bar's button that opened it (+ tag is in its closed popup)
        page.keyboard.press("Escape")
        B.expect(dlg).to_be_hidden()
        B.expect(page.locator("#bmore")).to_be_focused()
        # a suggestion taken, Enter confirms
        B.more(page, "+ tag").click()
        field.fill("Trips|Spain")
        with page.expect_response(lambda r: r.url.endswith("/api/tag")) as resp:
            field.press("Enter")
        self.assertEqual(resp.value.status, 200)
        B.expect(dlg).to_be_hidden()
        self.assertEqual(G.tags_of(self.path["b.jpg"])[:2], (["Trips|Spain"], ["Spain"]))
        B.expect(page.locator("#toast")).to_contain_text("added 'Trips|Spain' on 1")
        # remove offers the same suggestions
        B.more(page, "− tag").click()
        B.expect(dlg.get_by_role("heading")).to_have_text("Remove a tag of yours from 1 file?")
        field.fill("Trips|Spain")
        with page.expect_response(lambda r: r.url.endswith("/api/tag")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(G.tags_of(self.path["b.jpg"])[0], [])
        self.assert_originals()

    # ------------------------------------------------------------ errors
    def test_error_toast_stays_until_dismissed(self):
        s = self.start()
        page = self.page_for(s, clock=True)
        err = page.get_by_role("alert")
        # Keep refused by the server (the file went from disk since the scan): its own message, as an alert
        os.remove(self.path["c.png"])
        self.box(page, "c.png").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            page.keyboard.press("k")
        self.assertEqual(resp.value.status, 400)
        B.expect(err).to_contain_text("Keep refused: 1 of these files are no longer on disk; nothing was changed")
        # an ordinary message fades; the error doesn't
        page.keyboard.press("Escape")
        self.box(page, "b.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")):
            page.keyboard.press("k")
        toast = page.locator("#toast")
        B.expect(toast).to_have_class(re.compile(r"\bon\b"))
        page.clock.run_for("01:00")
        B.expect(toast).not_to_have_class(re.compile(r"\bon\b"))
        B.expect(err).to_be_visible()
        B.expect(err).to_contain_text("no longer on disk")
        err.get_by_role("button", name="Dismiss error").click()
        B.expect(page.locator("#etoast")).to_be_hidden()

    def test_lock_conflict_shows_the_error_toast(self):
        fake, gate = os.path.join(self.tmp, "gated_intake.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(G.GATED)
        os.mkfifo(gate)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s = self.start()
        page = self.page_for(s)
        dlg = self.dlg(page)
        # a rotate first, so there is an Undo to try while the lock is held
        self.box(page, "b.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/rotate")):
            B.more(page, "Rotate ⟳").click()
        B.expect(page.locator("#toast").get_by_role("button", name="Undo")).to_be_visible()
        before = self.sidecar("b.jpg")
        page.keyboard.press("Escape")
        # a trash held at the gate holds the library lock
        self.box(page, "d.jpg").click()
        page.keyboard.press("t")
        dlg.get_by_role("button", name="Trash 1 file").click()
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(dlg.locator("pre")).to_contain_text("trash: waiting at the gate")
        B.expect(dlg.locator("#mb")).to_have_text("")  # the confirm text is gone once it runs
        hide = dlg.get_by_role("button", name="Hide (it keeps running)")
        B.expect(hide).to_be_focused()
        hide.click()  # hides the dialog; the job keeps going (there is no stop)
        B.expect(dlg).to_be_hidden()
        pill = page.get_by_role("button", name="Trash running… Show progress")
        B.expect(pill).to_be_visible()
        self.box(page, "d.jpg").click()  # (off again)
        self.box(page, "a.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")) as resp:
            page.keyboard.press("k")
        self.assertEqual(resp.value.status, 409)
        err = page.get_by_role("alert")
        # plain words: what runs and what to do; never a PID or the lock file's path
        B.expect(err).to_contain_text("Keep refused: a Trash is still running; wait for it to finish, then try again")
        B.expect(err).not_to_contain_text("PID")
        B.expect(err).not_to_contain_text("lock")
        self.assertNotIn("Owner|keep", G.tags_of(self.path["a.jpg"])[0])
        # the alert's Show progress, and the pill, bring the dialog back
        err.get_by_role("button", name="Show progress").click()
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(dlg.locator("pre")).to_contain_text("trash: waiting at the gate")
        B.expect(pill).to_be_hidden()
        page.keyboard.press("Escape")
        B.expect(dlg).to_be_hidden()
        pill.click()
        B.expect(dlg).to_be_visible()
        page.keyboard.press("Escape")
        # it ends while hidden: announced in the message (role=status), the pill gone
        B.open_gate(gate)
        B.expect(page.locator("#toast > span")).to_have_text("Done: 1 file trashed.")
        B.expect(page.locator("#toast").get_by_role("button", name="Details")).to_be_visible()
        B.expect(pill).to_be_hidden()
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(dlg).to_be_hidden()
        self.assertEqual(self.sidecar("b.jpg"), before)

    # ------------------------------------------------------------ the "?" shortcut list
    def test_shortcut_list_names_every_key_the_page_handles(self):
        s = self.start()
        page = self.page_for(s)
        keys = page.get_by_role("dialog", name="Keyboard shortcuts")
        B.expect(keys).to_be_hidden()
        tile = page.locator('[data-item="b.jpg"]')
        tile.focus()
        page.keyboard.press("?")
        B.expect(keys).to_be_visible()
        B.expect(keys.get_by_role("button", name="Close")).to_be_focused()
        # every key a handler checks (k === "...", ev.key === "...") in the page actually served
        _, src = s.req("GET", "/")
        handled = set(re.findall(r'(?:\bk|ev\.key)===(["\'])(.+?)\1', src.decode()))
        # (a key value: one character or a named key; not a filter name like k === "kind")
        handled = {KEY_LABEL.get(k, k.upper() if len(k) == 1 else k) for _, k in handled
                   if len(k) == 1 or k in ("Enter", "Escape", "Tab") or k.startswith("Arrow")}
        listed = set(keys.locator("kbd").all_inner_texts())
        self.assertGreaterEqual(len(handled), 15, handled)
        self.assertEqual(handled - listed, set(), "keys the page handles but the list doesn't name")
        for section in ("Grid", "Bursts", "Large view", "Suggested junk", "Name faces and pets", "Dialogs"):
            B.expect(keys.get_by_role("heading", name=section, exact=True)).to_be_visible()
        # Tab stays in it; a key for the page does nothing while it is open; Esc closes, focus back on the tile
        for _ in range(3):
            page.keyboard.press("Tab")
            self.assertTrue(self.in_dialog(page, "#keys"))
        page.keyboard.press("/")
        B.expect(page.locator("#q")).not_to_be_focused()
        page.keyboard.press("Escape")
        B.expect(keys).to_be_hidden()
        B.expect(tile).to_be_focused()
        # "?" typed in the search box is text, not the list
        page.locator("#q").focus()
        page.keyboard.press("?")
        B.expect(page.locator("#q")).to_have_value("?")
        B.expect(keys).to_be_hidden()
        page.locator("#q").fill("")
        # the sidebar's entry opens it too; a click on the backdrop closes it
        entry = page.get_by_role("button", name="Keyboard shortcuts")
        entry.click()
        B.expect(keys).to_be_visible()
        page.mouse.click(5, 5)
        B.expect(keys).to_be_hidden()
        B.expect(entry).to_be_focused()
        # and "?" in the large view
        tile.click()
        B.expect(page.get_by_role("dialog", name="Item")).to_have_attribute("data-current", "b.jpg")
        page.keyboard.press("?")
        B.expect(keys).to_be_visible()
        page.keyboard.press("?")
        B.expect(keys).to_be_hidden()
        B.expect(page.get_by_role("dialog", name="Item")).to_be_visible()

    # ------------------------------------------------------------ Undo
    def test_undo_puts_each_sidecar_back(self):
        s = self.start()
        page = self.page_for(s)
        bar, dlg, toast = self.bar(page), self.dlg(page), page.locator("#toast")
        undo = toast.get_by_role("button", name="Undo")
        before = {n: self.sidecar(n) for n in self.path}
        # Rotate a selection (one with a sidecar, one without), then Undo: no orientation left on either
        self.box(page, "a.jpg").click()
        self.box(page, "b.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/rotate")):
            B.more(page, "Rotate ⟳").click()
        self.assertEqual((self.sidecar("a.jpg")[2], self.sidecar("b.jpg")[2]), (6, 6))
        with page.expect_response(lambda r: r.url.endswith("/api/undo")) as resp:
            undo.click()
        self.assertEqual(resp.value.status, 200)
        B.expect(toast).to_contain_text("undid Rotate on 2")
        B.expect(undo).to_have_count(0)  # one use
        for n in ("a.jpg", "b.jpg"):
            self.assertEqual(self.sidecar(n), before[n], n)
        # Keep by K, Undo by Ctrl+Z: maybe-trash back, the "kept" flag gone again
        page.keyboard.press("Escape")
        self.box(page, "a.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")):
            page.keyboard.press("k")
        B.expect(page.locator('[data-item="a.jpg"] .flag')).to_have_text("Kept")
        B.expect(toast).to_have_text("Kept 1. It won't be suggested as junk again.Undo")  # plain: no Owner|keep
        with page.expect_response(lambda r: r.url.endswith("/api/undo")):
            page.keyboard.press("Control+z")
        B.expect(page.locator('[data-item="a.jpg"] .flag')).to_have_text("Tagged as junk")
        self.assertEqual(self.sidecar("a.jpg"), before["a.jpg"])
        # Set event on two (one in another event, in both fields), then Undo
        page.keyboard.press("Escape")
        self.box(page, "a.jpg").click()
        self.box(page, "b.jpg").click()
        B.more(page, "Set event…").click()
        dlg.get_by_label("Event name").fill("beach")
        with page.expect_response(lambda r: r.url.endswith("/api/event/set")):
            dlg.get_by_role("button", name="Confirm").click()
        B.expect(page.locator('#evlist [data-ev="beach"]')).to_contain_text("2")
        with page.expect_response(lambda r: r.url.endswith("/api/undo")):
            undo.click()
        B.expect(page.locator('#evlist [data-ev="beach"]')).to_have_count(0)
        B.expect(page.locator('#evlist [data-ev="picnic"]')).to_contain_text("1")
        for n in ("a.jpg", "b.jpg"):
            self.assertEqual(self.sidecar(n), before[n], n)
        # a tag of mine added in the large view, then Undo there
        page.locator('[data-item="c.png"]').click()
        viewer = page.get_by_role("dialog", name="Item")
        B.expect(viewer).to_have_attribute("data-current", "c.png")
        add = viewer.get_by_label("Add my tag")
        self.assertEqual(add.get_attribute("list"), "mytags")
        add.fill("Trips|coast")
        with page.expect_response(lambda r: r.url.endswith("/api/tag")):
            add.press("Enter")
        B.expect(viewer.get_by_text("Trips|coast", exact=True)).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/api/undo")):
            undo.click()
        B.expect(viewer.get_by_text("Trips|coast", exact=True)).to_have_count(0)
        self.assertEqual(self.sidecar("c.png")[:2], ([], []))
        # a tool's namespace typed there is refused before anything is sent: an error, not a dialog
        add.fill("Owner|keep")
        add.press("Enter")
        B.expect(page.get_by_role("alert")).to_contain_text("belong to the tools")
        page.get_by_role("alert").get_by_role("button", name="Dismiss error").click()
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        # an Undo after the item changed again (here, digiKam or the CLI) is refused and writes nothing
        page.keyboard.press("Escape")
        self.box(page, "d.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/rotate")):
            B.more(page, "Rotate ⟳").click()
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-dc:Subject+=elsewhere",
                        self.path["d.jpg"] + ".xmp"], check=True)
        changed = self.sidecar("d.jpg")
        with page.expect_response(lambda r: r.url.endswith("/api/undo")) as resp:
            undo.click()
        self.assertEqual(resp.value.status, 409)
        B.expect(page.get_by_role("alert")).to_contain_text("Undo refused: can't undo: those items changed again")
        self.assertEqual(self.sidecar("d.jpg"), changed)
        self.assert_originals()

    def test_no_undo_after_trash_erase_or_share(self):
        s = self.start()
        page = self.page_for(s)
        bar, dlg = self.bar(page), self.dlg(page)
        undo = page.get_by_role("button", name="Undo")
        replies = []
        page.on("response", lambda r: replies.append(r) if r.request.method == "POST" else None)
        # Share
        self.box(page, "d.jpg").click()
        B.more(page, "Share", exact=True).click()
        dlg.get_by_role("button", name="Confirm").click()
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(undo).to_have_count(0)
        dlg.get_by_role("button", name="Close", exact=True).click()
        # Trash
        self.box(page, "b.jpg").click()
        page.keyboard.press("t")
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash 1 file?")
        B.expect(dlg.get_by_role("button", name="Trash 1 file")).to_have_class(re.compile(r"\bgotrash\b"))
        dlg.get_by_role("button", name="Trash 1 file").click()
        B.expect(dlg).to_have_attribute("data-job", "done")
        B.expect(dlg).to_be_hidden()  # a Trash that went well ends in a message, not the dialog
        self.assertFalse(os.path.exists(self.path["b.jpg"]))
        B.expect(undo).to_have_count(0)
        # Erase: still ERASE typed
        self.box(page, "c.png").click()
        page.keyboard.press("e")
        go = dlg.get_by_role("button", name="Erase 1 file")
        B.expect(go).to_be_disabled()
        B.expect(dlg).to_contain_text("Type ERASE in capital letters to confirm")
        hint = dlg.locator("#mhint")
        B.expect(hint).to_be_hidden()
        dlg.get_by_role("textbox", name="Type ERASE").fill("erase")  # lower case: refused, and the hint says why
        B.expect(hint).to_have_text("Capital letters: type ERASE exactly.")
        B.expect(go).to_be_disabled()
        dlg.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        B.expect(hint).to_be_hidden()
        B.expect(go).to_have_class(re.compile(r"\bgoerase\b"))  # in Erase's own red, not the accent blue
        go.click()
        B.expect(dlg).to_have_attribute("data-job", "done")
        self.assertFalse(os.path.exists(self.path["c.png"]))
        B.expect(undo).to_have_count(0)
        page.keyboard.press("Control+z")  # nothing to undo: no request
        for r in replies:
            self.assertFalse(r.url.endswith("/api/undo"), r.url)
            if re.search(r"/api/(trash|erase|share)$", r.url):
                self.assertNotIn("undo", r.json(), r.url)

    # ------------------------------------------------------------ plain words
    def test_no_tool_namespace_in_any_text_shown(self):
        """Grid flags, filters and chips, the large view's tags, Keep's message and every dialog: no Owner|,
        Review| or Label| in what the page shows (its text, tooltips and accessible names)."""
        G.write_tags(self.path["b.jpg"], ["Project|Personal", "Label|food", "Review|child-in-shot", "Owner|keep",
                                          "Owner|not-trash", "AI|vlm/m1/trash/blurry", "AI|vlm/m1/conf/0.9",
                                          "Used|2026-11-10-demo", "Trips|Spain"])
        s = self.start()
        page = self.page_for(s)
        dlg = self.dlg(page)
        seen = []

        def scan(where):
            text = page.evaluate(r"""() => document.body.innerText + "\n" + [...document.querySelectorAll(
                "[title],[aria-label],img[alt]")].filter(e => e.getClientRects().length)
                .map(e => [e.title, e.getAttribute("aria-label"), e.alt].filter(Boolean).join(" ")).join("\n")""")
            seen.append(where)
            for ns in ("Owner|", "Review|", "Label|"):
                self.assertNotIn(ns, text, where)

        scan("grid")
        B.expect(page.locator('[data-item="b.jpg"] .flag')).to_have_text("Junk score 0.90 (blurry) · Possible child · Kept · "
                                                                       "Used in a post")
        page.locator(".fs-h").evaluate_all(  # every filter section open
            "hs => hs.forEach(h => h.getAttribute('aria-expanded') === 'false' && h.click())")
        B.expect(page.locator(".fs-h[aria-expanded=false]")).to_have_count(0)
        B.expect(page.locator('.opts[data-k=review]')).to_contain_text("Tagged as junk")
        scan("filters")
        page.locator('.opts[data-k=review] .opt[data-val="child-in-shot"]').click()
        page.locator('.opts[data-k=label] .opt[data-val="food"]').click()
        B.expect(page.locator("#chips")).to_contain_text("Flags: Possible child")
        B.expect(page.locator("#chips")).to_contain_text("Looks like: food")
        scan("chips")
        page.locator('.opts[data-k=ai] .opt[data-val="trash"]').click()
        B.expect(page.locator("#chips")).to_contain_text("Junk check: Junk score ≥ 0.70")
        page.locator("#clear").click()
        B.expect(page.locator("[data-item]")).to_have_count(4)
        # the large view: your tags apart, the tools' in plain words, Owner| as "Marked Keep"
        page.locator('[data-item="b.jpg"]').click()
        viewer = page.get_by_role("dialog", name="Item")
        B.expect(viewer).to_have_attribute("data-current", "b.jpg")
        B.expect(viewer.locator("#mytagl")).to_have_text(re.compile(r"^Trips\|Spain×$"))
        auto = viewer.locator("#tooltagl .tag").all_inner_texts()
        self.assertEqual(sorted(auto), ["Junk check: Judged junk (blurry)", "Looks like: food", "Possible child",
                                        "Project: Personal", "Used in: 2026-11-10-demo"])
        B.expect(viewer).to_contain_text("Marked Keep")
        for name in ("Rotate left (Shift+R)", "Rotate right (R)", "Find similar"):
            B.expect(viewer.get_by_role("button", name=name, exact=True)).to_be_visible()
        scan("large view")
        page.keyboard.press("Escape")
        # Keep's message, then the Trash, Erase, tag, Set event and Share dialogs
        self.box(page, "a.jpg").click()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")):
            page.keyboard.press("k")
        B.expect(page.locator("#toast")).to_contain_text("Kept 1. It won't be suggested as junk again.")
        scan("keep message")
        self.box(page, "b.jpg").click()
        for key, title in (("t", "Trash 2 files?"), ("e", "Erase 2 files everywhere?")):
            page.keyboard.press(key)
            B.expect(dlg.get_by_role("heading")).to_have_text(title)
            B.expect(dlg.locator(".mthumbs img")).to_have_count(2)
            B.expect(dlg).to_contain_text("This can't be undone." if key == "e" else "this can't be undone")
            scan(title)
            page.keyboard.press("Escape")
        for button in ("+ tag", "Set event…", "Share"):
            B.more(page, button, exact=True).click()
            B.expect(dlg).to_be_visible()
            scan(button)
            page.keyboard.press("Escape")
        self.assertEqual(len(seen), 10)
        self.assert_originals()

    def test_phone_message_sits_above_the_action_bar(self):
        s = self.start()
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                       is_mobile=True, has_touch=True)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.open(page, s)
        for n in ("a.jpg", "b.jpg"):
            self.box(page, n).tap()
        bar, toast = page.locator("#actionbar"), page.locator("#toast")
        B.expect(bar).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/api/keep")):
            self.bar(page).get_by_role("button", name="Keep").tap()
        B.expect(toast).to_have_class(re.compile(r"\bon\b"))
        t, b = toast.bounding_box(), bar.bounding_box()
        self.assertLessEqual(t["y"] + t["height"], b["y"], (t, b))  # the whole message above the bar's top
        B.expect(toast.get_by_role("button", name="Undo")).to_be_visible()


if __name__ == "__main__":
    unittest.main()
