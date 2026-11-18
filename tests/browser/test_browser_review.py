#!/usr/bin/env python3
"""The review page in headless Chromium, on synthetic media in a throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_review.py

Covers what the HTTP tests in tests/test_review_ui.py can't: the K / E / D / T keys and the
arrows on the focused card, no Trash (button or T) on a quarantined card, Submit unlocking only when every item is decided, the confirm
panel with ERASE typed, HTTP Range while a card's video loads and seeks, and the run's
progress, including re-attaching to a running job after a reload.

intake.py is tests/test_review_ui.py's fake (MEDIA_REVIEW_INTAKE): it records its argv and
mimics release / erase / purge-review on the fake state. With a FIFO at FAKE_GATE its first
erase waits until the test opens it, so a run is held mid-step without a sleep.

Accessibility (the ReviewA11y tests below): the tab bar as a tablist with arrow keys, a visible
:focus-visible outline (3:1 or more in light and dark), the focus kept on a card after a decision or a
stack opening or closing, the ERASE confirm as a modal dialog (Tab stays inside, Esc goes back, the focus
returns to Submit), the "?" keys list, a refused submit shown in a role=alert banner, the "N of M decided"
counter, and the run's output as a live log. Every page in this file fails its test if a native alert(),
confirm() or prompt() ever opens.

Hooks the tests rely on: body[data-ready] once the items (or a running job) are shown; each
card's [data-item] (its relative path) and aria-current on the focused one; the decision
buttons' aria-pressed; the run panel's [data-job] = running/done/failed/crashed/replaced;
the textbox named "Type ERASE"; buttons by their names (Submit, Run, Back) and the tabs (role tab).
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_review_ui as R  # noqa: E402  (fixtures and the server wrapper only)

BATCH = R.BATCH


class ReviewCase(B.BrowserCase):
    """The fixture and helpers; the tests are in the classes below."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        os.makedirs(self.home)
        fake = os.path.join(self.tmp, "fake_intake.py")
        with open(fake, "w") as f:
            f.write(R.FAKE)
        self.log = os.path.join(self.tmp, "argv.log")
        self.gate = os.path.join(self.tmp, "gate")  # a FIFO only in the test that holds a run
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, MEDIA_REVIEW_INTAKE=fake,
                        FAKE_LOG=self.log, FAKE_GATE=self.gate)
        for k in ("PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG"):
            self.env[k] = os.path.join(self.home, k.lower())
        # Quarantine: two images and an H.264 video; Review: two near-duplicates / junk.
        quar = os.path.join(self.state, "quarantine", BATCH)
        R.img(os.path.join(quar, "DCIM/Camera/a.jpg"), (200, 30, 30))
        R.img(os.path.join(quar, "DCIM/Camera/b.png"), (30, 200, 30))
        self.video = os.path.join(quar, "DCIM/Camera/c.mp4")
        os.makedirs(os.path.dirname(self.video), exist_ok=True)
        shutil.copyfile(self.clip(), self.video)
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        R.write_json(os.path.join(quar, "quarantine.json"), {
            "DCIM/Camera/a.jpg": {"reasons": ["nudity:exposed"], "sha": "0" * 64, "size": 1, "dst": f"{lib}/a.jpg"},
            "DCIM/Camera/b.png": {"reasons": ["document:id-card"], "sha": "1" * 64, "size": 1, "dst": f"{lib}/b.png"},
            "DCIM/Camera/c.mp4": {"reasons": ["nudity:exposed"], "sha": "2" * 64, "size": 1, "dst": f"{lib}/c.mp4"},
        })
        b = os.path.join(self.state, "batches", BATCH)
        R.img(os.path.join(b, "review/DCIM/Camera/r1.jpg"), (10, 10, 10), (20, 20))
        R.img(os.path.join(b, "review/Pictures/Screenshots/r2.png"), (250, 250, 250))
        R.write_json(os.path.join(b, "plan.json"), [
            {"rel": "DCIM/Camera/r1.jpg", "action": "review", "why": "junk: tiny or empty"},
            {"rel": "Pictures/Screenshots/r2.png", "action": "review", "why": "near-duplicate of DCIM/x.png"}])
        self.servers = []
        self.native = []  # native dialogs (alert, confirm, prompt): the page must never open one
        self.addCleanup(lambda: self.assertEqual(self.native, [], "a native dialog opened"))

    def new_page(self, scheme=None):
        if scheme is None:
            page = super().new_page()
        else:
            ctx = self.browser.new_context(color_scheme=scheme)
            ctx.set_default_timeout(B.STEP_MS)
            self.addCleanup(ctx.close)
            page = ctx.new_page()
        page.on("dialog", lambda d: (self.native.append(f"{d.type}: {d.message}"), d.dismiss()))
        return page

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def start(self):
        s = R.Server(self.env)
        self.servers.append(s)
        return s

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def cards(self, page):
        return page.locator("[data-item]").evaluate_all("els => els.map(e => e.dataset.item)")

    def card(self, page, rel):
        return page.locator(f'[data-item="{rel}"]')

    def pressed(self, page, rel, which):
        """Expect exactly the decision button `which` (Keep / Erase / Delete / Trash; None: no decision)
        pressed on the card."""
        c = self.card(page, rel)
        for name in ("Keep", "Erase", "Delete", "Trash"):
            btn = c.get_by_role("button", name=re.compile(rf"^{name} \("))
            if btn.count():
                B.expect(btn).to_have_attribute("aria-pressed", "true" if name == which else "false")

    def focused(self, page, rel):
        B.expect(page.locator('[data-item][aria-current="true"]')).to_have_attribute("data-item", rel)

    def decide_all(self, page):
        """Keys only: K, E, E in Quarantine (no Trash there); D, K in Review. Returns the decisions by path."""
        q = self.cards(page)
        self.assertEqual(sorted(q), ["DCIM/Camera/a.jpg", "DCIM/Camera/b.png", "DCIM/Camera/c.mp4"])
        for k in ("k", "e", "e"):
            page.keyboard.press(k)
        page.get_by_role("tab", name=re.compile(r"^Review \(")).click()
        r = self.cards(page)
        self.assertEqual(sorted(r), ["DCIM/Camera/r1.jpg", "Pictures/Screenshots/r2.png"])
        page.keyboard.press("d")
        page.keyboard.press("k")
        return dict(zip(q, ("keep", "erase", "erase"))), dict(zip(r, ("delete", "keep")))


class ReviewBrowserTest(ReviewCase):
    # ------------------------------------------------------------ keys, Submit and the ERASE panel
    def test_keys_and_confirm(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        B.expect(page.get_by_role("tab", name="Quarantine (3)")).to_have_attribute("aria-selected", "true")
        submit = page.get_by_role("button", name="Submit")
        B.expect(submit).to_be_disabled()
        q = self.cards(page)
        self.focused(page, q[0])
        # K decides the focused card and moves on; arrows move; a second key replaces a decision
        page.keyboard.press("k")
        self.pressed(page, q[0], "Keep")
        self.focused(page, q[1])
        page.keyboard.press("ArrowLeft")
        self.focused(page, q[0])
        page.keyboard.press("e")
        self.pressed(page, q[0], "Erase")
        self.focused(page, q[1])
        page.keyboard.press("ArrowDown")
        self.focused(page, q[2])
        page.keyboard.press("ArrowDown")  # stops at the last card
        self.focused(page, q[2])
        page.keyboard.press("t")  # Quarantine is Erase-only: T decides nothing here
        self.pressed(page, q[2], None)
        self.focused(page, q[2])
        page.keyboard.press("e")
        self.pressed(page, q[2], "Erase")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("k")
        self.pressed(page, q[1], "Keep")
        B.expect(page.get_by_text("2 undecided (quarantine 0, review 2)")).to_be_visible()
        B.expect(submit).to_be_disabled()
        # Review tab: D (or E) deletes, K keeps
        page.get_by_role("tab", name="Review (2)").click()
        r = self.cards(page)
        self.focused(page, r[0])
        page.keyboard.press("d")
        self.pressed(page, r[0], "Delete")
        page.keyboard.press("e")
        self.pressed(page, r[1], "Delete")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("k")
        self.pressed(page, r[0], "Keep")
        B.expect(page.get_by_text("All decided")).to_be_visible()
        B.expect(submit).to_be_enabled()
        # Submit: a summary and ERASE typed exactly; Back changes nothing
        submit.click()
        typed, run = page.get_by_role("textbox", name="Type ERASE"), page.get_by_role("button", name="Run")
        B.expect(typed).to_be_focused()
        B.expect(page.get_by_text("Quarantine: 1 keep / 2 erase;", exact=False)).to_be_visible()
        B.expect(page.get_by_text("Review: 1 keep / 1 delete / 0 trash", exact=False)).to_be_visible()
        B.expect(run).to_be_disabled()
        page.keyboard.press("k")  # keys do nothing while the panel is up (the focus is in the textbox)
        B.expect(typed).to_have_value("k")
        for wrong in ("erase", "ERAS", "ERASE ", " ERASE"):
            typed.fill(wrong)
            B.expect(run).to_be_disabled()
        page.get_by_role("button", name="Back").click()
        B.expect(typed).to_be_hidden()
        self.pressed(page, r[0], "Keep")  # decisions kept
        self.assertEqual(self.calls(), [])
        submit.click()
        B.expect(typed).to_have_value("")
        typed.fill("ERASE")
        B.expect(run).to_be_enabled()
        run.click()
        panel = page.locator("[data-job]")
        B.expect(panel).to_have_attribute("data-job", "done")
        B.expect(page.get_by_role("heading", name="Done. You can close this tab.")).to_be_visible()
        B.expect(page.locator("pre")).to_contain_text("verify-gone: no copies remain")
        B.expect(page.get_by_role("log", name="Run output")).to_have_attribute("aria-live", "polite")
        B.expect(page.locator("#runh").locator("xpath=..")).to_have_attribute("aria-live", "polite")
        cmds = [c[0] for c in self.calls()]
        self.assertNotIn("trash", cmds)
        self.assertIn("release", cmds)
        self.assertIn("erase", cmds)
        with open(os.path.join(self.state, "batches", BATCH, "review-decisions.json")) as f:
            saved = json.load(f)
        self.assertIn("keep", json.dumps(saved))
        self.assertIn("erase", json.dumps(saved))

    def test_quarantined_items_offer_no_trash_and_erase_still_runs(self):
        """Quarantine is Erase-only: no Trash button or T key on a quarantined card, a hint says why, and the
        server refuses a trash decision sent anyway. Review keeps Trash. Erase runs as before."""
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        trash = re.compile(r"^Trash \(")
        q = self.cards(page)
        for rel in q:
            c = self.card(page, rel)
            B.expect(c.get_by_role("button", name=trash)).to_have_count(0)
            B.expect(c.get_by_text("Sensitive: erase or keep")).to_be_visible()
            B.expect(c.get_by_role("button", name=re.compile(r"^Erase \("))).to_be_visible()
        B.expect(page.get_by_text("No Trash here", exact=False)).to_be_visible()
        self.focused(page, q[0])
        page.keyboard.press("t")
        self.pressed(page, q[0], None)
        self.focused(page, q[0])  # T neither decides nor moves on
        B.expect(page.get_by_text("5 undecided (quarantine 3, review 2)")).to_be_visible()
        # The server refuses it too: a trash decision posted straight to the API is a 400, nothing runs.
        status = page.evaluate("""async () => {
          const d = {}; data.items.forEach(i => d[i.id] = i.tab === "quarantine" ? "trash" : "keep");
          const r = await fetch("/api/submit", {method: "POST", credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRF": CSRF},
            body: JSON.stringify({decisions: d, confirm: "ERASE"})});
          return [r.status, (await r.json()).error];
        }""")
        self.assertEqual(status, [400, "a quarantined item can't be trashed: erase or keep it"])
        self.assertEqual(self.calls(), [])
        # Erase (button and key) decides as before.
        self.card(page, q[0]).get_by_role("button", name=re.compile(r"^Erase \(")).click()
        self.pressed(page, q[0], "Erase")
        self.card(page, q[1]).click()
        page.keyboard.press("e")
        page.keyboard.press("e")
        for rel in q:
            self.pressed(page, rel, "Erase")
        # Review keeps Trash, by button and by T.
        page.get_by_role("tab", name="Review (2)").click()
        r = self.cards(page)
        for rel in r:
            B.expect(self.card(page, rel).get_by_role("button", name=trash)).to_be_visible()
            B.expect(self.card(page, rel).get_by_text("Sensitive: erase or keep")).to_have_count(0)
        page.keyboard.press("t")
        self.pressed(page, r[0], "Trash")
        page.keyboard.press("k")
        page.get_by_role("button", name="Submit").click()
        B.expect(page.get_by_text("Quarantine: 0 keep / 3 erase;", exact=False)).to_be_visible()
        page.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        page.get_by_role("button", name="Run").click()
        B.expect(page.locator("[data-job]")).to_have_attribute("data-job", "done")
        calls = self.calls()
        self.assertIn(["erase", BATCH, "--yes"], calls)
        self.assertEqual([c for c in calls if c[0] == "trash"],
                         [["trash", "--batch", BATCH,
                           os.path.join(self.state, "batches", BATCH, "review", r[0]), "--yes"]], "only the review item")
        self.assertFalse(os.path.exists(os.path.join(self.state, "quarantine", BATCH)))

    # ------------------------------------------------------------ video: Range while loading and seeking
    def test_quarantine_burst_keep_one_erases_the_rest(self):
        """In Quarantine every shot is flagged, so "keep one" erases the rest (erase everywhere), never Trash."""
        plan = os.path.join(self.state, "batches", BATCH, "plan.json")
        with open(plan) as f:
            rows = json.load(f)
        rows += [{"rel": "DCIM/Camera/a.jpg", "action": "file", "why": "", "date": "2026-11-08T07:15:00",
                  "device": "cam-a"},
                 {"rel": "DCIM/Camera/b.png", "action": "file", "why": "", "date": "2026-11-08T07:15:01",
                  "device": "cam-a"}]
        R.write_json(plan, rows)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        q = self.cards(page)
        self.assertIn("DCIM/Camera/a.jpg", q)
        self.assertNotIn("DCIM/Camera/b.png", q, "a closed stack shows only its cover")
        cover = self.card(page, "DCIM/Camera/a.jpg")
        cover.click()
        page.keyboard.press("x")
        B.expect(self.card(page, "DCIM/Camera/b.png")).to_be_visible()
        cover.get_by_role("button", name="Keep this one, erase the rest").click()
        self.pressed(page, "DCIM/Camera/a.jpg", "Keep")
        self.pressed(page, "DCIM/Camera/b.png", "Erase")

    def test_video_seeks_by_range(self):
        size = os.path.getsize(self.video)
        s = self.start()
        page = self.new_page()
        media = B.MediaRequests(page)
        self.open(page, s)
        video = self.card(page, "DCIM/Camera/c.mp4").locator("video")
        src = video.evaluate("v => new URL(v.src).pathname")
        video.scroll_into_view_if_needed()
        self.assertAlmostEqual(video.evaluate(B.METADATA), 30, delta=0.2)
        self.assertFalse(video.evaluate(B.BUFFERED, 28.0), "fixture: the seek target is already buffered")
        before = len(media.of(src))
        self.assertAlmostEqual(video.evaluate(B.SEEK, 28.0), 28.0, delta=0.5)
        self.assert_ranges(media.of(src), size, after=before)
        # the page never starts a video by itself
        self.assertTrue(video.evaluate("v => v.paused"))

    # ------------------------------------------------------------ the detached run: progress and re-attach
    def test_run_progress_and_reattach_after_reload(self):
        os.mkfifo(self.gate)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        self.decide_all(page)
        page.get_by_role("button", name="Submit").click()
        page.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        page.get_by_role("button", name="Run").click()
        panel = page.locator("[data-job]")
        B.expect(panel).to_have_attribute("data-job", "running")
        B.expect(page.get_by_role("heading", name=re.compile(r"^Running… \(closing this tab does not stop it\)"))) \
            .to_be_visible()
        B.expect(page.locator("pre")).to_contain_text("erase: waiting at the gate")
        B.expect(page.get_by_text(re.compile(rf"^Step \d+( of \d+)?: erase {BATCH} \(started "))).to_be_visible()
        B.expect(page.get_by_role("button", name="Submit")).to_be_disabled()
        for name in (re.compile(r"^Quarantine"), re.compile(r"^Review")):
            B.expect(page.get_by_role("tab", name=name)).to_be_disabled()
        B.expect(page.locator("[data-item]")).to_have_count(0)  # no stale items under the run
        # reload: the page attaches to the run from its status file, output from the first line
        page.reload()
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        B.expect(panel).to_have_attribute("data-job", "running")
        B.expect(page.locator("pre")).to_contain_text("erase: waiting at the gate")
        B.expect(page.get_by_text(re.compile(rf"^Step \d+( of \d+)?: erase {BATCH} \(started "))).to_be_visible()
        B.expect(page.locator("[data-item]")).to_have_count(0)
        B.open_gate(self.gate)
        B.expect(panel).to_have_attribute("data-job", "done")
        B.expect(page.get_by_role("heading", name="Done. You can close this tab.")).to_be_visible()
        B.expect(page.locator("pre")).to_contain_text("verify-gone: no copies remain")
        self.assertEqual(s.p.wait(timeout=30), 0)  # the server exits once the page has read the end


# The focus outline's colour and the background behind it, and its style and width (the focused element).
OUTLINE = """e => { const s = getComputedStyle(e), card = e.closest('.card') && e !== e.closest('.card');
  return {style: s.outlineStyle, width: parseFloat(s.outlineWidth), color: s.outlineColor,
          bg: getComputedStyle(card ? e.closest('.card') : document.body).backgroundColor} }"""
FOCUSED = "() => { const a = document.activeElement; return a ? (a.dataset.item || a.id || a.textContent.trim()) : null }"


def contrast(a, b):
    """WCAG 2 contrast ratio of two computed "rgb(r, g, b)" colours."""
    def lum(c):
        r, g, b = (int(x) / 255 for x in re.findall(r"[\d.]+", c)[:3])
        f = lambda v: v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class ReviewA11yBrowserTest(ReviewCase):
    """Keyboard and screen-reader behaviour of the review page (same fixture as above)."""

    def active(self, page):
        return page.evaluate(FOCUSED)

    def assert_outline(self, loc, what):
        """A solid outline at least 2 px wide, 3:1 or more against what is behind it."""
        B.expect(loc).to_be_focused()
        o = loc.evaluate(OUTLINE)
        self.assertEqual(o["style"], "solid", what)
        self.assertGreaterEqual(o["width"], 2, what)
        self.assertGreaterEqual(contrast(o["color"], o["bg"]), 3, f"{what}: {o}")
        self.assertTrue(loc.evaluate("e => e.matches(':focus-visible')"), what)

    def test_tabs_are_a_tablist(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        tablist = page.get_by_role("tablist", name="Lists")
        B.expect(tablist.get_by_role("tab")).to_have_count(2)  # Maybe trash stays hidden without its items
        q, r = page.get_by_role("tab", name="Quarantine (3)"), page.get_by_role("tab", name="Review (2)")
        panel = page.get_by_role("tabpanel")
        B.expect(q).to_have_attribute("aria-selected", "true")
        B.expect(r).to_have_attribute("aria-selected", "false")
        B.expect(panel).to_have_attribute("aria-labelledby", "tq")
        B.expect(q).to_have_attribute("tabindex", "0")  # one tab stop for the bar: the selected tab
        B.expect(r).to_have_attribute("tabindex", "-1")
        page.keyboard.press("Tab")
        B.expect(q).to_be_focused()
        page.keyboard.press("ArrowRight")
        B.expect(r).to_be_focused()
        B.expect(r).to_have_attribute("aria-selected", "true")
        B.expect(q).to_have_attribute("aria-selected", "false")
        B.expect(panel).to_have_attribute("aria-labelledby", "tr")
        self.assertEqual(sorted(self.cards(page)), ["DCIM/Camera/r1.jpg", "Pictures/Screenshots/r2.png"])
        page.keyboard.press("ArrowRight")  # wraps
        B.expect(q).to_be_focused()
        B.expect(q).to_have_attribute("aria-selected", "true")
        page.keyboard.press("End")
        B.expect(r).to_have_attribute("aria-selected", "true")
        page.keyboard.press("Home")
        B.expect(q).to_have_attribute("aria-selected", "true")
        B.expect(q).to_be_focused()
        # Up / Down on a tab still move between items, and K still decides the outlined one.
        cards = self.cards(page)
        page.keyboard.press("ArrowDown")
        self.focused(page, cards[1])
        page.keyboard.press("k")
        self.pressed(page, cards[1], "Keep")
        B.expect(q).to_be_focused()  # the focus stays on the tab it was on

    def test_focus_outline_and_focus_kept(self):
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                s = self.start()
                page = self.new_page(scheme)
                self.open(page, s)
                q = self.cards(page)
                page.keyboard.press("Tab")
                self.assert_outline(page.get_by_role("tab", name="Quarantine (3)"), f"{scheme}: tab")
                page.keyboard.press("Tab")
                self.assert_outline(page.get_by_role("button", name="Keys (?)"), f"{scheme}: Keys button")
                page.keyboard.press("Tab")  # Submit is disabled: the next stop is the outlined card
                self.assert_outline(self.card(page, q[0]), f"{scheme}: card")
                page.keyboard.press("Tab")
                keep = self.card(page, q[0]).get_by_role("button", name=re.compile(r"^Keep \("))
                self.assert_outline(keep, f"{scheme}: Keep button")
                # K on the focused card: the focus moves on with the outline, never to the page body
                self.card(page, q[0]).focus()
                page.keyboard.press("k")
                self.focused(page, q[1])
                B.expect(self.card(page, q[1])).to_be_focused()
                page.keyboard.press("e")
                B.expect(self.card(page, q[2])).to_be_focused()
                page.keyboard.press("ArrowUp")
                B.expect(self.card(page, q[1])).to_be_focused()
                self.assertIn("decided erase", self.card(page, q[1]).get_attribute("aria-label"))
                self.assertIn("undecided", self.card(page, q[2]).get_attribute("aria-label"))
                # The ERASE textbox shows the outline too.
                page.keyboard.press("ArrowDown")
                page.keyboard.press("e")
                page.get_by_role("tab", name="Review (2)").click()
                page.keyboard.press("t")
                page.keyboard.press("t")
                page.get_by_role("button", name="Submit").click()
                typed = page.get_by_role("textbox", name="Type ERASE")
                page.keyboard.press("Shift+Tab")
                page.keyboard.press("Tab")
                self.assert_outline(typed, f"{scheme}: ERASE textbox")

    def test_stack_open_and_close_keep_the_focus(self):
        plan = os.path.join(self.state, "batches", BATCH, "plan.json")
        with open(plan) as f:
            rows = json.load(f)
        rows += [{"rel": "DCIM/Camera/a.jpg", "action": "file", "why": "", "date": "2026-11-08T07:15:00",
                  "device": "cam-a"},
                 {"rel": "DCIM/Camera/b.png", "action": "file", "why": "", "date": "2026-11-08T07:15:01",
                  "device": "cam-a"}]
        R.write_json(plan, rows)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        cover = self.card(page, "DCIM/Camera/a.jpg")
        cover.focus()
        page.keyboard.press("x")  # opens: the cards are rebuilt, the focus stays on the cover
        B.expect(self.card(page, "DCIM/Camera/b.png")).to_be_visible()
        B.expect(cover).to_be_focused()
        page.keyboard.press("ArrowDown")
        B.expect(self.card(page, "DCIM/Camera/b.png")).to_be_focused()
        page.keyboard.press("x")  # x on a shot closes the stack: the focus goes to the cover
        B.expect(self.card(page, "DCIM/Camera/b.png")).to_have_count(0)
        B.expect(cover).to_be_focused()
        # the stack's button, by keyboard: Enter on it opens and closes, the focus never lands on the body
        btn = cover.locator(".burst .x")
        btn.focus()
        page.keyboard.press("Enter")
        B.expect(self.card(page, "DCIM/Camera/b.png")).to_be_visible()
        B.expect(cover).to_be_focused()
        btn.focus()
        page.keyboard.press("Enter")
        B.expect(self.card(page, "DCIM/Camera/b.png")).to_have_count(0)
        B.expect(cover).to_be_focused()

    def test_confirm_is_a_modal_dialog(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        self.decide_all(page)
        submit = page.get_by_role("button", name="Submit")
        submit.click()
        dlg = page.get_by_role("dialog", name="Confirm")
        B.expect(dlg).to_be_visible()
        B.expect(dlg).to_have_attribute("aria-modal", "true")
        B.expect(dlg).to_have_attribute("aria-describedby", "summary")
        typed = dlg.get_by_role("textbox", name="Type ERASE")
        B.expect(typed).to_be_focused()
        self.assertTrue(page.evaluate("() => document.querySelector('main').inert && "
                                      "document.querySelector('header').inert"))
        # Tab stays inside: Run is disabled until ERASE is typed, so it cycles the textbox and Back.
        page.keyboard.press("Tab")
        B.expect(dlg.get_by_role("button", name="Back")).to_be_focused()
        page.keyboard.press("Tab")
        B.expect(typed).to_be_focused()
        page.keyboard.press("Shift+Tab")
        B.expect(dlg.get_by_role("button", name="Back")).to_be_focused()
        typed.fill("ERASE")
        B.expect(typed).to_be_focused()
        page.keyboard.press("Tab")
        B.expect(dlg.get_by_role("button", name="Run")).to_be_focused()
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        B.expect(typed).to_be_focused()
        # "?" while typing is a character, not the keys list
        page.keyboard.press("?")
        B.expect(typed).to_have_value("ERASE?")
        B.expect(page.get_by_role("dialog", name="Keys")).to_be_hidden()
        # Esc goes back: nothing ran, decisions kept, the focus returns to Submit
        page.keyboard.press("Escape")
        B.expect(dlg).to_be_hidden()
        B.expect(submit).to_be_focused()
        self.assertFalse(page.evaluate("() => document.querySelector('main').inert"))
        self.assertEqual(self.calls(), [])
        B.expect(page.get_by_text("All decided")).to_be_visible()

    def test_keys_overlay(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        q = self.cards(page)
        self.card(page, q[1]).focus()
        keys = page.get_by_role("dialog", name="Keys")
        B.expect(keys).to_be_hidden()
        page.keyboard.press("?")
        B.expect(keys).to_be_visible()
        B.expect(keys).to_have_attribute("aria-modal", "true")
        close = keys.get_by_role("button", name="Close (Esc)")
        B.expect(close).to_be_focused()
        # every key the page handles, with where it applies (T does nothing in Quarantine)
        rows = keys.locator("tr[data-key]")
        self.assertEqual(rows.evaluate_all("rs => rs.map(r => r.dataset.key)"),
                         ["k", "e", "t", "x", "next", "prev", "tabs", "tab", "?", "escape"])
        B.expect(keys.locator('tr[data-key="t"]')).to_contain_text("nothing in Quarantine")
        B.expect(keys.locator('tr[data-key="e"]')).to_contain_text("nothing in Maybe trash")
        for k in ("K", "E", "D", "T", "X", "J", "→", "←", "↑", "↓", "Home", "End", "?", "Esc"):
            B.expect(keys.locator("kbd", has_text=re.compile("^" + re.escape(k) + "$")).first).to_be_attached()
        # keys don't reach the list behind it, and Tab stays on its one button
        page.keyboard.press("k")
        self.pressed(page, q[1], None)
        page.keyboard.press("Tab")
        B.expect(close).to_be_focused()
        page.keyboard.press("Escape")
        B.expect(keys).to_be_hidden()
        B.expect(self.card(page, q[1])).to_be_focused()  # back where it was
        # the header's Keys button opens it too, and the Close button returns the focus to it
        kb = page.get_by_role("button", name="Keys (?)")
        kb.click()
        B.expect(keys).to_be_visible()
        close.click()
        B.expect(keys).to_be_hidden()
        B.expect(kb).to_be_focused()

    def test_refused_submit_shows_a_banner(self):
        """A submit the server refuses (here: the library lock held by another process) shows its message in a
        role=alert banner inside the confirm, never alert(); Dismiss hides it, and a retry once free runs."""
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        self.decide_all(page)
        hold = ("import sys; sys.path.insert(0, sys.argv[1]); import library_lock as L; "
                "L.acquire('test holder'); print('held', flush=True); sys.stdin.read()")  # one -c program
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c", hold, os.path.join(B.TESTS, os.pardir, "media_intake")],
            env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 30)
        self.addCleanup(holder.stdin.close)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        page.get_by_role("button", name="Submit").click()
        dlg = page.get_by_role("dialog", name="Confirm")
        dlg.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        run = dlg.get_by_role("button", name="Run")
        run.click()
        banner = dlg.get_by_role("alert")
        B.expect(banner).to_be_visible()
        B.expect(banner).to_contain_text(re.compile(r"^Refused: the library is busy: test holder \(PID \d+\)"))
        B.expect(run).to_be_enabled()
        B.expect(run).to_be_focused()
        self.assertEqual(self.calls(), [])
        banner.get_by_role("button", name="Dismiss").click()
        B.expect(banner).to_be_hidden()
        B.expect(dlg.get_by_role("textbox", name="Type ERASE")).to_be_focused()
        # Back keeps a banner that is still up: it moves above the list with the server's message.
        run.click()
        B.expect(banner).to_be_visible()
        page.keyboard.press("Escape")
        page_banner = page.locator("main").get_by_role("alert")
        B.expect(page_banner).to_contain_text("Refused: the library is busy")
        page_banner.get_by_role("button", name="Dismiss").click()
        B.expect(page_banner).to_be_hidden()
        # free the lock: the same page submits and runs
        holder.stdin.close()
        holder.wait(30)
        page.get_by_role("button", name="Submit").click()
        dlg.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        run.click()
        B.expect(page.locator("[data-job]")).to_have_attribute("data-job", "done")
        B.expect(page.get_by_role("alert")).to_have_count(0)

    def test_decided_counter(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        done = page.locator("#done")
        B.expect(done).to_have_attribute("role", "status")
        B.expect(done).to_have_text("0 of 5 decided")
        B.expect(page.get_by_text("5 undecided (quarantine 3, review 2)")).to_be_visible()  # the per-tab count stays
        q = self.cards(page)
        page.keyboard.press("k")
        B.expect(done).to_have_text("1 of 5 decided")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("e")  # a changed decision is still one
        B.expect(done).to_have_text("1 of 5 decided")
        self.pressed(page, q[0], "Erase")
        page.keyboard.press("e")
        page.keyboard.press("e")
        B.expect(done).to_have_text("3 of 5 decided")
        page.get_by_role("tab", name="Review (2)").click()
        page.keyboard.press("d")
        page.keyboard.press("t")
        B.expect(done).to_have_text("5 of 5 decided")
        B.expect(page.get_by_text("All decided")).to_be_visible()


if __name__ == "__main__":
    unittest.main(verbosity=2)
