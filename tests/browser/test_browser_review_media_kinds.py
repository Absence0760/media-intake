#!/usr/bin/env python3
"""The review page in headless Chromium with photos and videos side by side.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_review_media_kinds.py

Covers what test_browser_review.py and the HTTP tests leave out: burst stacks in a real browser (a
closed stack's K decides only its cover, X opens it, "Keep this one, ..." decides every shot: Erase
the rest in Quarantine, Trash the rest in Review), with a video of the same moment beside each stack
that is never stacked and is decided on its own; the stack's cover, its X (the button and the key, on
any shot) opening and closing it, the count badge on every shot, and a near-copy never stacked; what a card says when the browser can't decode a
file (a broken video, a video whose picture it can't decode, a broken picture, HEIC); and a
messaging copy of a video shown as a video beside its library original.

intake.py is tests/test_review_ui.py's fake (MEDIA_REVIEW_INTAKE): it records its argv and mimics
release / erase / trash on the fake state. The real run of the same decisions is in
tests/test_review_media_kinds.py.

Hooks the tests rely on: body[data-ready]; each card's [data-item] (its relative path),
[data-burst] on a stack's shots and aria-current on the focused card; the decision buttons'
aria-pressed; a stack's badge (.badge) text and data-count, the cover's class stack while closed
and its X (button.x); .media[data-cant] once a card has given up on
showing its file; the run panel's [data-job]; the textbox named "Type ERASE".
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_review_ui as R  # noqa: E402  (fixtures and the server wrapper only)

BATCH = R.BATCH


class ReviewMediaKindsBrowserTest(B.BrowserCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-kinds-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        os.makedirs(self.home)
        fake = os.path.join(self.tmp, "fake_intake.py")
        with open(fake, "w") as f:
            f.write(R.FAKE)
        self.log = os.path.join(self.tmp, "argv.log")
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, MEDIA_REVIEW_INTAKE=fake,
                        FAKE_LOG=self.log)
        self.env.pop("FAKE_GATE", None)
        for k in ("PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG"):
            self.env[k] = os.path.join(self.home, k.lower())
        self.quar = os.path.join(self.state, "quarantine", BATCH)
        self.bd = os.path.join(self.state, "batches", BATCH)
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

    def video(self, p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copyfile(self.clip(), p)
        return p

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

    def focus(self, page, rel):
        """Focus a card by clicking its name line (not a button or the video)."""
        self.card(page, rel).locator(".name").click()
        B.expect(page.locator('[data-item][aria-current="true"]')).to_have_attribute("data-item", rel)

    def badge(self, page, rel):
        return self.card(page, rel).locator(".badge")

    def assert_plays_picture(self, page, rel):
        """The card's video has loaded its metadata with a picture (not the can't-play note)."""
        v = self.card(page, rel).locator("video").first
        v.scroll_into_view_if_needed()
        self.assertGreater(v.evaluate(B.METADATA), 0)
        self.assertGreater(v.evaluate("v => v.videoWidth"), 0)

    # ------------------------------------------------------------ bursts with a video beside them
    def test_burst_keep_one_with_a_video_in_each_tab(self):
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        vids = os.path.join(self.home, "personal_footage/2026/2026-11")
        q, plan = {}, []
        for n in range(1, 4):
            rel = f"DCIM/Camera/IMG_000{n}.jpg"
            R.img(os.path.join(self.quar, rel), (60 * n, 30, 30))
            q[rel] = {"reasons": ["nudity:exposed"], "sha": f"{n}" * 64, "size": 1, "dst": f"{lib}/IMG_000{n}.jpg"}
            plan.append({"rel": rel, "action": "file", "why": "", "date": f"2026-11-08T07:15:0{n - 1}.400000",
                         "device": "cam-a", "dhash": 0})
        qv = self.video(os.path.join(self.quar, "DCIM/Camera/VID_0001.mp4"))
        q["DCIM/Camera/VID_0001.mp4"] = {"reasons": ["nudity:exposed"], "sha": "9" * 64, "size": 1,
                                         "dst": f"{vids}/VID_0001.mp4"}
        plan.append({"rel": "DCIM/Camera/VID_0001.mp4", "action": "file", "why": "", "date": "2026-11-08T07:15:01",
                     "device": "cam-a", "dhash": None})
        rv = os.path.join(self.bd, "review")
        for n in (1, 2):
            rel = f"DCIM/Camera/IMG_100{n}.jpg"
            R.img(os.path.join(rv, rel), (30, 60 * n, 30))
            plan.append({"rel": rel, "action": "review", "why": "junk: tiny or empty",
                         "date": f"2026-11-08T08:00:0{n}", "device": "cam-a", "dhash": 0})
        rvid = self.video(os.path.join(rv, "DCIM/Camera/VID_1001.mp4"))
        plan.append({"rel": "DCIM/Camera/VID_1001.mp4", "action": "review", "why": "junk: tiny or empty",
                     "date": "2026-11-08T08:00:01", "device": "cam-a", "dhash": None})
        R.write_json(os.path.join(self.quar, "quarantine.json"), q)
        R.write_json(os.path.join(self.bd, "plan.json"), plan)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        submit = page.get_by_role("button", name="Submit")
        # Quarantine: a closed stack shows its cover; the video beside it is a card of its own.
        self.assertEqual(self.cards(page), ["DCIM/Camera/IMG_0001.jpg", "DCIM/Camera/VID_0001.mp4"])
        cover = self.card(page, "DCIM/Camera/IMG_0001.jpg")
        B.expect(cover).to_have_class(re.compile(r"\bstack\b"))
        B.expect(self.badge(page, "DCIM/Camera/IMG_0001.jpg")).to_have_text("Burst: 3 shots, 3 undecided")
        vcard = self.card(page, "DCIM/Camera/VID_0001.mp4")
        self.assertIsNone(vcard.get_attribute("data-burst"))
        B.expect(vcard.locator(".badge")).to_have_count(0)
        self.assert_plays_picture(page, "DCIM/Camera/VID_0001.mp4")
        # K on a closed stack decides its cover only: Submit stays locked.
        self.focus(page, "DCIM/Camera/IMG_0001.jpg")
        page.keyboard.press("k")
        self.pressed(page, "DCIM/Camera/IMG_0001.jpg", "Keep")
        B.expect(self.badge(page, "DCIM/Camera/IMG_0001.jpg")).to_have_text("Burst: 3 shots, 2 undecided")
        # X opens it: every shot follows the cover, each with its keep-one button; the video has none.
        self.focus(page, "DCIM/Camera/IMG_0001.jpg")
        page.keyboard.press("x")
        self.assertEqual(self.cards(page), ["DCIM/Camera/IMG_0001.jpg", "DCIM/Camera/IMG_0002.jpg",
                                            "DCIM/Camera/IMG_0003.jpg", "DCIM/Camera/VID_0001.mp4"])
        B.expect(page.get_by_role("button", name="Keep this one, erase the rest")).to_have_count(3)
        B.expect(vcard.get_by_role("button", name=re.compile("Keep this one"))).to_have_count(0)
        self.card(page, "DCIM/Camera/IMG_0002.jpg").get_by_role("button", name="Keep this one, erase the rest").click()
        self.pressed(page, "DCIM/Camera/IMG_0001.jpg", "Erase")
        self.pressed(page, "DCIM/Camera/IMG_0002.jpg", "Keep")
        self.pressed(page, "DCIM/Camera/IMG_0003.jpg", "Erase")
        B.expect(self.badge(page, "DCIM/Camera/IMG_0001.jpg")).to_have_text("Burst: 3 shots, all decided")
        self.pressed(page, "DCIM/Camera/VID_0001.mp4", None)
        B.expect(submit).to_be_disabled()
        self.focus(page, "DCIM/Camera/VID_0001.mp4")
        page.keyboard.press("k")
        self.pressed(page, "DCIM/Camera/VID_0001.mp4", "Keep")
        # Review: the same, with Trash for the rest, and the video deleted with D.
        page.get_by_role("tab", name=re.compile(r"^Review \(")).click()
        self.assertEqual(self.cards(page), ["DCIM/Camera/IMG_1001.jpg", "DCIM/Camera/VID_1001.mp4"])
        self.card(page, "DCIM/Camera/IMG_1001.jpg").get_by_role("button", name="Show all 2 (X)").click()
        self.assertEqual(self.cards(page), ["DCIM/Camera/IMG_1001.jpg", "DCIM/Camera/IMG_1002.jpg",
                                            "DCIM/Camera/VID_1001.mp4"])
        self.card(page, "DCIM/Camera/IMG_1002.jpg").get_by_role("button", name="Keep this one, trash the rest").click()
        self.pressed(page, "DCIM/Camera/IMG_1001.jpg", "Trash")
        self.pressed(page, "DCIM/Camera/IMG_1002.jpg", "Keep")
        self.assert_plays_picture(page, "DCIM/Camera/VID_1001.mp4")
        self.focus(page, "DCIM/Camera/VID_1001.mp4")
        page.keyboard.press("d")
        self.pressed(page, "DCIM/Camera/VID_1001.mp4", "Delete")
        B.expect(submit).to_be_enabled()
        submit.click()
        B.expect(page.locator("#summary")).to_contain_text("Quarantine: 2 keep / 2 erase;")
        B.expect(page.locator("#summary")).to_contain_text("Review: 1 keep / 1 delete / 1 trash")
        page.get_by_role("textbox", name="Type ERASE").fill("ERASE")
        page.get_by_role("button", name="Run").click()
        B.expect(page.locator("#run")).to_have_attribute("data-job", "done")
        self.assertEqual(self.calls(), [
            ["trash", "--batch", BATCH, os.path.join(rv, "DCIM/Camera/IMG_1001.jpg"), "--yes"],
            ["release", BATCH, os.path.join(self.quar, "DCIM/Camera/IMG_0002.jpg"), qv],
            ["erase", BATCH, "--yes"],
            ["verify-gone", BATCH]])
        self.assertFalse(os.path.exists(rvid), "a deleted review video is gone")
        self.assertTrue(os.path.isfile(os.path.join(rv, "DCIM/Camera/IMG_1002.jpg")), "the kept shot stays")
        with open(os.path.join(self.bd, "review-decisions.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["quarantine"][BATCH], {"DCIM/Camera/IMG_0001.jpg": "erase", "DCIM/Camera/IMG_0002.jpg": "keep",
                                                    "DCIM/Camera/IMG_0003.jpg": "erase",
                                                    "DCIM/Camera/VID_0001.mp4": "keep"})
        self.assertEqual(rec["review"], {"DCIM/Camera/IMG_1001.jpg": "trash", "DCIM/Camera/IMG_1002.jpg": "keep",
                                         "DCIM/Camera/VID_1001.mp4": "delete"})

    def test_stack_cover_close_and_count_badge(self):
        """A closed stack is its cover alone (class stack, a "Show all N (X)" button, the count badge); opened,
        every shot carries the badge and only the cover has the X, now "Close stack (X)"; closing it (the
        button, or x on any shot) hides the rest, moves the focus to the cover and keeps their decisions. A
        near-copy of an erased photo taken the same second is never stacked: no badge, no X, x does nothing."""
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        q, plan = {}, []
        for n in range(1, 4):
            rel = f"DCIM/Camera/IMG_000{n}.jpg"
            R.img(os.path.join(self.quar, rel), (60 * n, 30, 30))
            q[rel] = {"reasons": ["nudity:exposed"], "sha": f"{n}" * 64, "size": 1, "dst": f"{lib}/IMG_000{n}.jpg"}
            plan.append({"rel": rel, "action": "file", "why": "", "date": f"2026-11-08T07:15:0{n - 1}.400000",
                         "device": "cam-a", "dhash": 0})
        R.write_json(os.path.join(self.quar, "quarantine.json"), q)
        R.write_json(os.path.join(self.bd, "plan.json"), plan)
        near = os.path.join(self.state, "quarantine", f"near-{BATCH}")
        R.img(os.path.join(near, "Downloads/n.jpg"), (90, 90, 200))
        R.write_json(os.path.join(near, "quarantine.json"), {
            "Downloads/n.jpg": {"reasons": ["near-duplicate"], "sha": "7" * 64, "size": 1,
                                "dst": os.path.join(self.home, "Downloads/n.jpg"),
                                "near_of_sha": "1" * 64, "erased_on": "2026-11-12"}})
        shots = [f"DCIM/Camera/IMG_000{n}.jpg" for n in range(1, 4)]
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        cover, ncard = self.card(page, shots[0]), self.card(page, "Downloads/n.jpg")
        B.expect(page.get_by_role("tab", name="Quarantine (4)")).to_be_visible()  # every shot counts
        # Closed: the cover alone, with the stack look, the count badge and the X that opens it.
        self.assertEqual(sorted(self.cards(page)), ["DCIM/Camera/IMG_0001.jpg", "Downloads/n.jpg"])
        B.expect(cover).to_have_class(re.compile(r"\bstack\b"))
        B.expect(self.badge(page, shots[0])).to_have_text("Burst: 3 shots, 3 undecided")
        B.expect(self.badge(page, shots[0])).to_have_attribute("data-count", "3")
        B.expect(cover.locator("button.x")).to_have_text("Show all 3 (X)")
        B.expect(cover.get_by_role("button", name=re.compile("Keep this one"))).to_have_count(0)
        self.assertIsNone(ncard.get_attribute("data-burst"))
        B.expect(ncard.locator(".badge")).to_have_count(0)
        B.expect(ncard.locator("button.x")).to_have_count(0)
        # x on the near-copy does nothing.
        self.focus(page, "Downloads/n.jpg")
        page.keyboard.press("x")
        self.assertEqual(sorted(self.cards(page)), ["DCIM/Camera/IMG_0001.jpg", "Downloads/n.jpg"])
        # Open with the cover's X button: every shot follows the cover, each with the badge; one X only.
        cover.get_by_role("button", name="Show all 3 (X)").click()
        order = self.cards(page)
        i = order.index(shots[0])
        self.assertEqual(order[i:i + 3], shots)
        B.expect(cover).not_to_have_class(re.compile(r"\bstack\b"))
        B.expect(cover.locator("button.x")).to_have_text("Close stack (X)")
        for rel in shots:
            B.expect(self.badge(page, rel)).to_have_text("Burst: 3 shots, 3 undecided")
            B.expect(self.badge(page, rel)).to_have_attribute("data-count", "3")
            self.assertEqual(self.card(page, rel).get_attribute("data-burst"), cover.get_attribute("data-burst"))
        for rel in shots[1:]:
            B.expect(self.card(page, rel).locator("button.x")).to_have_count(0)
            B.expect(self.card(page, rel)).not_to_have_class(re.compile(r"\bstack\b"))
        B.expect(page.locator("button.x")).to_have_count(1)
        # A decision on one shot counts on every badge of the stack.
        self.focus(page, shots[2])
        page.keyboard.press("e")
        self.pressed(page, shots[2], "Erase")
        for rel in shots:
            B.expect(self.badge(page, rel)).to_have_text("Burst: 3 shots, 2 undecided")
        # x on a shot that is not the cover closes the stack: the focus goes to the cover.
        self.focus(page, shots[2])
        page.keyboard.press("x")
        self.assertEqual(sorted(self.cards(page)), ["DCIM/Camera/IMG_0001.jpg", "Downloads/n.jpg"])
        B.expect(page.locator('[data-item][aria-current="true"]')).to_have_attribute("data-item", shots[0])
        B.expect(cover).to_have_class(re.compile(r"\bstack\b"))
        B.expect(cover.locator("button.x")).to_have_text("Show all 3 (X)")
        B.expect(self.badge(page, shots[0])).to_have_text("Burst: 3 shots, 2 undecided")
        # Open with x on the cover, close with its "Close stack (X)" button; the decision is still there.
        page.keyboard.press("x")
        self.pressed(page, shots[2], "Erase")
        B.expect(self.card(page, shots[1])).to_be_visible()
        cover.get_by_role("button", name="Close stack (X)").click()
        B.expect(self.card(page, shots[1])).to_have_count(0)
        B.expect(self.card(page, shots[2])).to_have_count(0)
        B.expect(cover).to_have_class(re.compile(r"\bstack\b"))
        B.expect(page.get_by_role("button", name="Submit")).to_be_disabled()
        self.assertEqual(self.calls(), [])

    # ------------------------------------------------------------ files the browser can't show
    def test_a_file_the_browser_cannot_decode_says_so(self):
        rv = os.path.join(self.bd, "review/DCIM/Camera")
        os.makedirs(rv)
        self.video(os.path.join(rv, "a-plays.mp4"))
        with open(os.path.join(rv, "b-broken.mp4"), "wb") as f:
            f.write(bytes(range(256)) * 16)  # not a video at all
        # A picture the browser can't decode while the rest of the file plays (an HEVC .mov from an
        # iPhone in Chromium): stood in for by a file with sound and no picture.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-c:a", "aac", os.path.join(rv, "c-no-picture.mp4")], check=True)
        with open(os.path.join(rv, "d-broken.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + bytes(200))
        with open(os.path.join(rv, "e-photo.heic"), "wb") as f:
            f.write(b"\x00" * 64)
        R.img(os.path.join(rv, "f-shows.jpg"), (30, 90, 150))
        names = ["a-plays.mp4", "b-broken.mp4", "c-no-picture.mp4", "d-broken.jpg", "e-photo.heic", "f-shows.jpg"]
        R.write_json(os.path.join(self.bd, "plan.json"), [
            {"rel": f"DCIM/Camera/{n}", "action": "review", "why": "junk: tiny or empty"} for n in names])
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        self.assertEqual(self.cards(page), [f"DCIM/Camera/{n}" for n in names])
        media = {n: self.card(page, f"DCIM/Camera/{n}").locator(".media") for n in names}
        for n in names:
            media[n].scroll_into_view_if_needed()
        for n, text in (("b-broken.mp4", "can't play this video"), ("c-no-picture.mp4", "can't play this video"),
                        ("d-broken.jpg", "can't show this picture")):
            B.expect(media[n]).to_have_attribute("data-cant", "1")
            B.expect(media[n]).to_contain_text(text)
            B.expect(media[n]).to_contain_text("Open it in your file manager")
        B.expect(media["e-photo.heic"]).to_contain_text("No preview in the browser for this format")
        self.assert_plays_picture(page, "DCIM/Camera/a-plays.mp4")
        self.assertGreater(media["f-shows.jpg"].locator("img").evaluate(
            "im => im.complete ? im.naturalWidth : new Promise(r => im.onload = () => r(im.naturalWidth))"), 0)
        for n in ("a-plays.mp4", "f-shows.jpg"):
            self.assertIsNone(media[n].get_attribute("data-cant"), n)
        # A card that gave up on its preview is still decided like any other.
        c = self.card(page, "DCIM/Camera/b-broken.mp4")
        c.get_by_role("button", name="Delete (E)").click()
        self.pressed(page, "DCIM/Camera/b-broken.mp4", "Delete")

    def test_messaging_copy_of_a_video_beside_its_original(self):
        orig = self.video(os.path.join(self.home, "personal_footage/2026/2026-10/VID_orig.mp4"))
        xq = os.path.join(self.state, "quarantine", f"xcheck-{BATCH}")
        self.video(os.path.join(xq, "WhatsApp Video/VID-20261108-WA0001.mp4"))
        R.write_json(os.path.join(xq, "quarantine.json"), {"WhatsApp Video/VID-20261108-WA0001.mp4": {
            "reasons": ["messaging-copy"], "sha": "7" * 64, "size": 1, "dst": None, "of": orig}})
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        rel = "WhatsApp Video/VID-20261108-WA0001.mp4"
        c = self.card(page, rel)
        B.expect(c.locator(".note")).to_have_text("Messaging copy of this video")
        B.expect(c.locator(".side h3")).to_have_text(["Original (kept)", "This copy"])
        vs = c.locator("video")
        B.expect(vs).to_have_count(2)
        for i in range(2):
            self.assertGreater(vs.nth(i).evaluate(B.METADATA), 0)
            self.assertGreater(vs.nth(i).evaluate("v => v.videoWidth"), 0)


if __name__ == "__main__":
    unittest.main()
