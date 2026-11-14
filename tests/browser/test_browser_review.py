#!/usr/bin/env python3
"""The review page in headless Chromium, on synthetic media in a throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_review.py

Covers what the HTTP tests in tests/test_review_ui.py can't: the K / E / D / T keys and the
arrows on the focused card, Submit unlocking only when every item is decided, the confirm
panel with ERASE typed, HTTP Range while a card's video loads and seeks, and the run's
progress, including re-attaching to a running job after a reload.

intake.py is tests/test_review_ui.py's fake (MEDIA_REVIEW_INTAKE): it records its argv and
mimics release / erase / purge-review on the fake state. With a FIFO at FAKE_GATE its first
erase waits until the test opens it, so a run is held mid-step without a sleep.

Hooks the tests rely on: body[data-ready] once the items (or a running job) are shown; each
card's [data-item] (its relative path) and aria-current on the focused one; the decision
buttons' aria-pressed; the run panel's [data-job] = running/done/failed/crashed/replaced;
the textbox named "Type ERASE"; buttons by their names (Submit, Run, Back, the tabs).
"""
import json, os, re, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_review_ui as R  # noqa: E402  (fixtures and the server wrapper only)

BATCH = R.BATCH


class ReviewBrowserTest(B.BrowserCase):
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
        """Expect exactly the decision button `which` (Keep / Erase / Delete / Trash) pressed on the card."""
        c = self.card(page, rel)
        for name in ("Keep", "Erase", "Delete", "Trash"):
            btn = c.get_by_role("button", name=re.compile(rf"^{name} \("))
            if btn.count():
                B.expect(btn).to_have_attribute("aria-pressed", "true" if name == which else "false")

    def focused(self, page, rel):
        B.expect(page.locator('[data-item][aria-current="true"]')).to_have_attribute("data-item", rel)

    def decide_all(self, page):
        """Keys only: K, E, T in Quarantine; D, K in Review. Returns the decisions by path."""
        q = self.cards(page)
        self.assertEqual(sorted(q), ["DCIM/Camera/a.jpg", "DCIM/Camera/b.png", "DCIM/Camera/c.mp4"])
        for k in ("k", "e", "t"):
            page.keyboard.press(k)
        page.get_by_role("button", name=re.compile(r"^Review \(")).click()
        r = self.cards(page)
        self.assertEqual(sorted(r), ["DCIM/Camera/r1.jpg", "Pictures/Screenshots/r2.png"])
        page.keyboard.press("d")
        page.keyboard.press("k")
        return dict(zip(q, ("keep", "erase", "trash"))), dict(zip(r, ("delete", "keep")))

    # ------------------------------------------------------------ keys, Submit and the ERASE panel
    def test_keys_and_confirm(self):
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        B.expect(page.get_by_role("button", name="Quarantine (3)")).to_have_attribute("aria-selected", "true")
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
        page.keyboard.press("t")
        self.pressed(page, q[2], "Trash")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("k")
        self.pressed(page, q[1], "Keep")
        B.expect(page.get_by_text("2 undecided (quarantine 0, review 2)")).to_be_visible()
        B.expect(submit).to_be_disabled()
        # Review tab: D (or E) deletes, K keeps
        page.get_by_role("button", name="Review (2)").click()
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
        B.expect(page.get_by_text("Quarantine: 1 keep / 1 erase / 1 trash")).to_be_visible()
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
        cmds = [c[0] for c in self.calls()]
        self.assertIn("trash", cmds)
        self.assertIn("release", cmds)
        self.assertIn("erase", cmds)
        with open(os.path.join(self.state, "batches", BATCH, "review-decisions.json")) as f:
            saved = json.load(f)
        self.assertIn("keep", json.dumps(saved))
        self.assertIn("erase", json.dumps(saved))

    # ------------------------------------------------------------ video: Range while loading and seeking
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
        for name in ("Submit", re.compile(r"^Quarantine"), re.compile(r"^Review")):
            B.expect(page.get_by_role("button", name=name)).to_be_disabled()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
