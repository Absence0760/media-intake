#!/usr/bin/env python3
"""The review page in headless Chromium, on synthetic media in a throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_review.py

Covers what the HTTP tests in tests/test_review_ui.py can't: the K / E (Quarantine) / D (Review) / T keys and the
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
the textbox named "Type ERASE"; buttons by their names (Submit, "Apply N decisions", Back) and the tabs (role tab).
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
        """Expect exactly the decision button `which` (Keep / Erase / Delete this copy / Trash; None: no
        decision) pressed on the card."""
        c = self.card(page, rel)
        for name in ("Keep", "Erase", "Delete this copy", "Trash"):
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
    # ------------------------------------------------------------ all keep: no ERASE word
    def test_all_keep_asks_for_no_erase_word(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        for _ in range(3):
            page.keyboard.press("k")
        page.get_by_role("tab", name="Review (2)").click()
        page.keyboard.press("k")
        page.keyboard.press("k")
        page.get_by_role("button", name="Submit").click()
        B.expect(page.get_by_text("Nothing will be deleted.")).to_be_visible()
        B.expect(page.get_by_role("textbox", name="Type ERASE")).to_be_hidden()
        run = page.get_by_role("button", name="Apply 5 decisions")
        B.expect(run).to_be_enabled()
        B.expect(run).to_be_focused()
        run.click()
        B.expect(page.get_by_role("heading", name="Done. You can close this tab.")).to_be_visible(timeout=20000)
        self.assertFalse(any(c[0] in ("erase", "trash", "purge-review") for c in self.calls()), self.calls())

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
        B.expect(page.locator("#done")).to_have_text("Decide 2 more to submit (2 in Review)")
        B.expect(submit).to_be_disabled()
        # Review tab: D deletes this copy, E (Erase, Quarantine only) decides nothing here, K keeps
        page.get_by_role("tab", name="Review (2)").click()
        r = self.cards(page)
        self.focused(page, r[0])
        page.keyboard.press("d")
        self.pressed(page, r[0], "Delete this copy")
        page.keyboard.press("e")
        self.pressed(page, r[1], None)
        page.keyboard.press("d")
        self.pressed(page, r[1], "Delete this copy")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("k")
        self.pressed(page, r[0], "Keep")
        B.expect(page.get_by_text("All decided")).to_be_visible()
        B.expect(submit).to_be_enabled()
        # Submit: a summary and ERASE typed exactly; Back changes nothing
        submit.click()
        typed, run = page.get_by_role("textbox", name="Type ERASE"), page.get_by_role("button", name="Apply 5 decisions")
        B.expect(typed).to_be_focused()
        B.expect(page.get_by_text("Quarantine: keep 1, erase 2", exact=True)).to_be_visible()
        B.expect(page.get_by_text("Review: keep 1, delete this copy 1, trash 0", exact=True)).to_be_visible()
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
        B.expect(page.locator("#done")).to_have_text("Decide 5 more to submit (3 in Quarantine, 2 in Review)")
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
        B.expect(page.get_by_text("Quarantine: keep 0, erase 3", exact=True)).to_be_visible()
        page.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        page.get_by_role("button", name="Apply 5 decisions").click()
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
        page.get_by_role("button", name="Apply 5 decisions").click()
        panel = page.locator("[data-job]")
        B.expect(panel).to_have_attribute("data-job", "running")
        B.expect(page.get_by_role("heading", name=re.compile(r"^Running… \(closing this tab does not stop it\)"))) \
            .to_be_visible()
        B.expect(page.locator("pre")).to_contain_text("erase: waiting at the gate")
        B.expect(page.get_by_text(re.compile(rf"^Step \d+( of \d+)?: erase {BATCH} \(started "))).to_be_visible()
        B.expect(page.get_by_role("button", name="Submit")).to_be_disabled()
        B.expect(page.get_by_role("button", name="Keys (?)")).to_be_enabled()  # the keys list stays open to read
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
        dlg = page.get_by_role("dialog", name="Apply 5 decisions?")
        B.expect(dlg).to_be_visible()
        B.expect(dlg).to_have_attribute("aria-modal", "true")
        B.expect(dlg).to_have_attribute("aria-describedby", "summary")
        typed = dlg.get_by_role("textbox", name="Type ERASE")
        B.expect(typed).to_be_focused()
        self.assertTrue(page.evaluate("() => document.querySelector('main').inert && "
                                      "document.querySelector('header').inert"))
        # Tab stays inside: Apply is disabled until ERASE is typed, so it cycles the textbox and Back.
        page.keyboard.press("Tab")
        B.expect(dlg.get_by_role("button", name="Back")).to_be_focused()
        page.keyboard.press("Tab")
        B.expect(typed).to_be_focused()
        page.keyboard.press("Shift+Tab")
        B.expect(dlg.get_by_role("button", name="Back")).to_be_focused()
        typed.fill("ERASE")
        B.expect(typed).to_be_focused()
        page.keyboard.press("Tab")
        B.expect(dlg.get_by_role("button", name="Apply 5 decisions")).to_be_focused()
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
                         ["k", "e", "d", "t", "x", "next", "prev", "tabs", "tab", "?", "escape"])
        B.expect(keys.locator('tr[data-key="t"]')).to_contain_text("nothing in Quarantine")
        B.expect(keys.locator('tr[data-key="e"]')).to_contain_text("Quarantine only")
        B.expect(keys.locator('tr[data-key="d"]')).to_contain_text("Review only")
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
        role=alert banner under the Apply dialog's title, in plain words (no PID or lock path), never alert();
        Dismiss hides it, and a retry once free runs."""
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
        dlg = page.get_by_role("dialog", name="Apply 5 decisions?")
        dlg.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        run = dlg.get_by_role("button", name="Apply 5 decisions")
        run.click()
        banner = dlg.get_by_role("alert")
        B.expect(banner).to_be_visible()
        B.expect(banner).to_contain_text(re.compile(r"^Not applied: Another action is using the library"))
        self.assertNotRegex(banner.inner_text(), r"PID|\.lock|test holder")
        # under the title, above the summary
        self.assertEqual(dlg.locator("h2 + *").get_attribute("id"), "err")
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
        B.expect(page_banner).to_contain_text("Not applied: Another action is using the library")
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
        """One counter (role=status), which is also why Submit is locked: how many are left, and in which tabs
        unless only the open one; it ignores a changed decision and counts a cleared one again."""
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        done = page.locator("#done")
        B.expect(done).to_have_attribute("role", "status")
        B.expect(done).to_have_text("Decide 5 more to submit (3 in Quarantine, 2 in Review)")
        self.assertEqual(page.locator("header [role=status]").count(), 1, "one counter in the header")
        q = self.cards(page)
        page.keyboard.press("k")
        B.expect(done).to_have_text("Decide 4 more to submit (2 in Quarantine, 2 in Review)")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("e")  # a changed decision is still one
        B.expect(done).to_have_text("Decide 4 more to submit (2 in Quarantine, 2 in Review)")
        self.pressed(page, q[0], "Erase")
        page.keyboard.press("e")
        page.keyboard.press("e")
        B.expect(done).to_have_text("Decide 2 more to submit (2 in Review)")
        page.get_by_role("tab", name="Review (2)").click()
        B.expect(done).to_have_text("Decide 2 more to submit")  # only this tab is left: no list
        page.keyboard.press("d")
        page.keyboard.press("t")
        B.expect(done).to_have_text("All decided: 5 of 5. Ready to submit.")
        B.expect(page.get_by_role("button", name="Submit")).to_be_enabled()


class ReviewUxBrowserTest(ReviewCase):
    """The page's words and phone layout: the locked Submit's reason, marks you can see and clear, the Apply
    dialog named for its action, plain labels (no internal tag groups, scanner codes or state folders), and
    44 px touch targets with a compact header at 390 px."""

    def visible_text(self, page):
        return page.evaluate("() => document.body.innerText")

    def test_locked_submit_says_why(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        submit = page.get_by_role("button", name="Submit")
        B.expect(submit).to_be_disabled()
        B.expect(submit).to_have_accessible_description("Decide 5 more to submit (3 in Quarantine, 2 in Review)")
        B.expect(page.get_by_text("Decide 5 more to submit", exact=False)).to_be_visible()
        q = self.cards(page)
        keep = self.card(page, q[0]).get_by_role("button", name="Keep (K)")
        keep.click()
        self.pressed(page, q[0], "Keep")
        B.expect(self.card(page, q[0]).locator(".verdict")).to_have_text(
            "Decided: Keep. It goes back into your library.")
        B.expect(submit).to_have_accessible_description(re.compile(r"^Decide 4 more"))
        keep.click()  # a second press clears the mark
        self.pressed(page, q[0], None)
        B.expect(self.card(page, q[0]).locator(".verdict")).to_be_hidden()
        B.expect(submit).to_have_accessible_description(re.compile(r"^Decide 5 more"))
        # K on a kept card clears it too, and stays there
        self.card(page, q[0]).focus()
        page.keyboard.press("k")
        self.focused(page, q[1])
        page.keyboard.press("ArrowUp")
        page.keyboard.press("k")
        self.pressed(page, q[0], None)
        self.focused(page, q[0])
        # Keep in Review shows what it does: the file stays in the review folder.
        page.get_by_role("tab", name="Review (2)").click()
        r = self.cards(page)
        self.card(page, r[0]).get_by_role("button", name="Keep (K)").click()
        B.expect(self.card(page, r[0]).locator(".verdict")).to_have_text(
            "Decided: Keep. It stays in the review folder for now.")
        B.expect(self.card(page, r[0]).get_by_role("button", name="Delete this copy (D)")).to_have_attribute(
            "title", "Delete this copy: only this file is removed")
        B.expect(self.card(page, r[0]).get_by_role("button", name="Trash (T)")).to_have_attribute(
            "title", "Trash: it is removed, and skipped if imported again")

    def test_apply_dialog_names_the_action(self):
        phone = os.path.join(self.tmp, "phone-root")
        os.makedirs(phone)
        s = R.Server(self.env, "--phone", phone)
        self.servers.append(s)
        page = self.new_page()
        self.open(page, s)
        self.decide_all(page)  # Quarantine: keep 1, erase 2; Review: delete this copy 1, keep 1
        page.get_by_role("button", name="Submit").click()
        dlg = page.get_by_role("dialog", name="Apply 5 decisions?")
        B.expect(dlg).to_be_visible()
        B.expect(dlg.get_by_role("heading", name="Apply 5 decisions?")).to_be_visible()
        B.expect(dlg.get_by_role("button", name="Apply 5 decisions")).to_be_disabled()
        items = dlg.get_by_role("listitem")
        B.expect(items).to_have_text(["Quarantine: keep 1, erase 2", "Review: keep 1, delete this copy 1, trash 0"])
        B.expect(dlg).to_contain_text("Erase removes every copy: on this computer and your phone.")
        B.expect(dlg).to_contain_text("Kept Review items stay in the review folder.")
        B.expect(dlg).to_contain_text("3 files will be removed for good. This can't be undone.")
        text = dlg.inner_text()
        for bad in (phone, self.tmp, "xcheck", "Confirm", "Run"):
            self.assertNotIn(bad, text)
        self.assertEqual(dlg.get_by_role("button", name=re.compile(r"^(Run|Confirm)$")).count(), 0)

    def test_no_internal_names_in_visible_text(self):
        """Tags' groups (Review|, Label|), scanner codes (nudity, id-barcode), the plan's raw `why`, a sub-batch
        prefix and the state folder never reach the page's text: each has a plain name."""
        quar = os.path.join(self.state, "quarantine", BATCH)
        with open(os.path.join(quar, "quarantine.json")) as f:
            q = json.load(f)
        q["DCIM/Camera/a.jpg"]["reasons"] = ["nudity", "id-barcode", "secret-words:banking"]
        R.write_json(os.path.join(quar, "quarantine.json"), q)
        near = os.path.join(self.state, "quarantine", "near-" + BATCH)
        R.img(os.path.join(near, "DCIM/Camera/n1.jpg"), (90, 90, 200))
        R.write_json(os.path.join(near, "quarantine.json"), {
            "DCIM/Camera/n1.jpg": {"reasons": [], "sha": "a" * 64, "size": 1, "erased_on": "2026-11-10"}})
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        pasta = os.path.join(lib, "pasta.jpg")
        R.img(pasta, (200, 180, 90))
        R.write_json(os.path.join(self.state, "maybe-trash.json"), {"batch": None, "items": [
            {"path": pasta, "labels": ["Review|maybe-trash", "Label|food"]}]})
        s = R.Server(self.env, "--maybe-trash")
        self.servers.append(s)
        page = self.new_page()
        self.open(page, s)
        B.expect(page).to_have_title(f"Review {BATCH}")
        seen = []
        for name in ("Quarantine", "Review", "Maybe trash"):
            page.get_by_role("tab", name=re.compile("^" + name + r" \(")).click()
            text = self.visible_text(page)
            seen.append(text)
            for bad in ("|", "near-" + BATCH + " /", ".local/share", "/quarantine/", "/batches/", "id-barcode",
                        "secret-words", "junk: tiny", "near-duplicate of", "No library place"):
                self.assertNotIn(bad, text, f"{name} tab")
            for chip in page.locator(".labels span").all_inner_texts():  # a raw code is one lowercase word
                self.assertNotRegex(chip, r"^[a-z0-9-]+$", f"{name} tab")
        allt = "\n".join(seen)
        for good in ("Possible nudity", "Looks like an ID card", "Private text: banking", "Tagged as junk",
                     "Looks like: food", "Tiny or empty image", "Near-copy of DCIM/x.png", "DCIM/Camera/n1.jpg"):
            self.assertIn(good, allt)

    def phone_page(self, scheme="light"):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                       device_scale_factor=2, color_scheme=scheme)
        ctx.set_default_timeout(B.STEP_MS)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.on("dialog", lambda d: (self.native.append(f"{d.type}: {d.message}"), d.dismiss()))
        return page

    def small_targets(self, page):
        """Every visible button, tab and text box under 44 x 44 CSS px, as (label, w, h)."""
        return page.evaluate("""() => [...document.querySelectorAll('button, [role=tab], input')]
          .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
          .map(e => { const r = e.getBoundingClientRect(); return [e.textContent.trim() || e.id, r.width, r.height] })
          .filter(([, w, h]) => w < 44 || h < 44)""")

    def test_phone_targets_and_compact_header(self):
        plan = os.path.join(self.state, "batches", BATCH, "plan.json")
        with open(plan) as f:
            rows = json.load(f)
        rows += [{"rel": "DCIM/Camera/a.jpg", "action": "file", "why": "", "date": "2026-11-08T07:15:00",
                  "device": "cam-a"},
                 {"rel": "DCIM/Camera/b.png", "action": "file", "why": "", "date": "2026-11-08T07:15:01",
                  "device": "cam-a"}]
        R.write_json(plan, rows)  # a burst, for its stack buttons
        s = self.start()
        page = self.phone_page()
        self.open(page, s)
        self.assertEqual(self.small_targets(page), [])
        B.expect(page.get_by_role("button", name="Keys (?)")).to_be_hidden()  # no keyboard on a phone
        self.card(page, "DCIM/Camera/a.jpg").locator(".burst .x").click()  # open the stack: "Keep this one"
        B.expect(page.get_by_role("button", name=re.compile("Keep this one")).first).to_be_visible()
        self.assertEqual(self.small_targets(page), [])
        # Scrolled down, only the header's bar (the counter and Submit) stays: a small part of the screen.
        page.evaluate("() => window.scrollTo(0, 1500)")
        page.wait_for_function("() => document.querySelector('header').getBoundingClientRect().bottom < 130")
        hb = page.locator("header").bounding_box()
        self.assertLess(hb["y"] + hb["height"], 130, hb)
        B.expect(page.get_by_role("button", name="Submit")).to_be_in_viewport()
        B.expect(page.locator("#done")).to_be_in_viewport()
        # A refused submit's alert sits below the header, never over it.
        page.evaluate("() => window.scrollTo(0, 0)")
        self.decide_all(page)
        page.route("**/api/submit", lambda r: r.fulfill(status=409, content_type="application/json", body=json.dumps(
            {"error": "the library is busy: job gallery (PID 510) holds its lock (/tmp/x/locks/library-1.lock); "
                      "try again when it has finished"})))
        page.get_by_role("button", name="Submit").click()
        dlg = page.get_by_role("dialog", name="Apply 5 decisions?")
        dlg.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        dlg.get_by_role("button", name="Apply 5 decisions").click()
        B.expect(dlg.get_by_role("alert")).to_contain_text("Another action is using the library")
        self.assertEqual(self.small_targets(page), [])
        page.keyboard.press("Escape")
        alert = page.locator("main").get_by_role("alert")
        B.expect(alert).to_be_visible()
        B.expect(alert).to_be_in_viewport()
        self.assertNotIn("PID", alert.inner_text())
        a, h = alert.bounding_box(), page.locator("header").bounding_box()
        self.assertGreaterEqual(a["y"], h["y"] + h["height"] - 1, (a, h))


if __name__ == "__main__":
    unittest.main(verbosity=2)
