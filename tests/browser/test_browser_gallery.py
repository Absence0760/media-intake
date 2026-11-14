#!/usr/bin/env python3
"""The gallery page in headless Chromium, on synthetic media in a throwaway HOME.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery.py

Covers what the HTTP tests in tests/test_gallery.py can't: keys, tile selection (click and
shift-click), the bottom selection bar (shown only while something is selected), the sidebar's
views, the confirm dialogs (Trash; Erase with ERASE typed; the Close button's native confirm),
the lightbox video playing by itself, Space pausing and resuming it, HTTP Range while the video
loads and seeks, and the detached-job dialog, including re-attaching to a running job after a reload.

Trash and Erase run a fake intake.py (MEDIA_REVIEW_INTAKE, tests/test_gallery.py's GATED) that
deletes the files it is given; with a FIFO at FAKE_GATE it first waits until the test opens it,
so a job is held mid-step without a sleep.

Hooks the tests rely on (keep them when the layout changes): body[data-ready] once a page of
items has rendered; each grid item's [data-item] (its file name) and its checkbox named
"select <name>"; the lightbox as role=dialog named "Item" with [data-current] = the name of the item shown;
the confirm/job dialog as [data-dialog=confirm] (role=dialog) with [data-job] =
running/done/failed/crashed, its job status as role=status (shown while a job runs, hidden when it ends)
and its output as its <pre>; the textbox named "Type ERASE"; the selection bar as role=toolbar
named "Selection actions"; buttons by their names (Confirm, Cancel, Apply…, Trash suggestions,
Possible child, Library, Clear selection, Close).
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
        self.assertEqual(G.tags_of(self.c), ([], []))  # untouched: no sidecar written
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
        # Trash suggestions: arrows move the outline, K / T mark, nothing changes until Apply + Confirm
        page.get_by_role("button", name="Trash suggestions").click()
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
        with page.expect_response(lambda r: r.url.endswith("/api/decide")) as resp:
            dlg.get_by_role("button", name="Confirm").click()
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
        for name in ("Keep", "Trash", "Erase", "+ tag", "Rotate", "Clear selection"):
            B.expect(bar.get_by_role("button", name=name)).to_be_visible()
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
        page.get_by_role("button", name="Possible child").click()
        B.expect(page.locator("[data-item]")).to_have_count(1)
        B.expect(page.locator("[data-item]")).to_have_attribute("data-item", "c.png")
        B.expect(page.get_by_role("button", name="Remove Review: Child in shot")).to_be_visible()
        page.get_by_role("button", name="Library").click()
        B.expect(page.locator("[data-item]")).to_have_count(4)

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
        B.expect(dlg.get_by_role("heading")).to_have_text("Trash 1 item?")
        B.expect(dlg.get_by_text("b.jpg", exact=True)).to_be_visible()
        B.expect(dlg.get_by_role("textbox", name="Type ERASE")).to_be_hidden()
        dlg.get_by_role("button", name="Cancel").click()
        B.expect(dlg).to_be_hidden()
        self.assertTrue(os.path.exists(self.b))
        # E needs ERASE typed exactly
        page.keyboard.press("e")
        B.expect(dlg.get_by_role("heading")).to_have_text("Erase 1 item everywhere?")
        typed, go = dlg.get_by_role("textbox", name="Type ERASE"), dlg.get_by_role("button", name="Confirm")
        B.expect(typed).to_be_focused()
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
        B.expect(dlg.locator("pre")).to_contain_text("$ intake.py forget [1 file] --yes")
        B.expect(dlg.locator("pre")).to_contain_text("done: 1 of 1 gone from the library")
        self.assertFalse(os.path.exists(self.b))
        B.expect(page.locator('[data-item="b.jpg"]')).to_have_count(0)
        # the Close button's native confirm: Cancel keeps the server, OK stops it
        dlg.get_by_role("button", name="Cancel").click()
        page.once("dialog", lambda d: d.dismiss())
        page.get_by_role("button", name="Close").click()
        page.evaluate("1")  # a round trip: the click handler has run past its confirm()
        s.get("/api/facets")  # still serving (raises unless 200)
        self.assertIsNone(s.p.poll())
        B.expect(page.get_by_role("button", name="Close")).to_be_visible()
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Close").click()
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
        dlg.get_by_role("button", name="Confirm").click()
        # progress: the step and the output so far, while the fake waits at the gate
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(dlg.get_by_role("heading")).to_have_text(re.compile(r"^Running \(step 1 of 1: trash\)"))
        out = dlg.locator("pre")
        B.expect(out).to_contain_text("$ intake.py trash 'c.png'")
        B.expect(out).to_contain_text("trash: waiting at the gate")
        B.expect(dlg.get_by_role("button", name="Confirm")).to_be_hidden()
        # a reload (the page closed and opened again) attaches to the running job from its status file
        page.reload()
        B.expect(page.locator("body")).to_have_attribute("data-ready", "1")
        B.expect(dlg).to_be_visible()
        B.expect(dlg).to_have_attribute("data-job", "running")
        B.expect(dlg.get_by_role("heading")).to_have_text(re.compile(r"^Running \(step 1 of 1: trash\)"))
        B.expect(out).to_contain_text("trash: waiting at the gate")
        # a second page opened meanwhile shows the running job too
        other = self.new_page()
        self.open(other, s)
        odlg = self.confirm(other)
        B.expect(odlg).to_have_attribute("data-job", "running")
        self.assertTrue(os.path.exists(self.c))
        B.open_gate(self.gate)
        for p, d in ((page, dlg), (other, odlg)):
            B.expect(d).to_have_attribute("data-job", "done")
            B.expect(d.get_by_role("heading")).to_have_text("Done")
            B.expect(d.locator("pre")).to_contain_text("done: 1 of 1 gone from the library")
            B.expect(p.locator('[data-item="c.png"]')).to_have_count(0)
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
        dlg.get_by_role("button", name="Confirm").click()
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
        B.expect(status).to_be_hidden()
        self.assertFalse(os.path.exists(self.b))

    def test_crashed_job_is_shown(self):
        os.mkfifo(self.gate)
        s = self.start()
        page = self.new_page()
        self.open(page, s)
        dlg = self.confirm(page)
        page.get_by_role("checkbox", name="select d.jpg").click()
        page.keyboard.press("t")
        dlg.get_by_role("button", name="Confirm").click()
        B.expect(dlg.locator("pre")).to_contain_text("trash: waiting at the gate")
        with open(os.path.join(self.state, "jobs", "gallery.json")) as f:
            os.kill(json.load(f)["pid"], signal.SIGKILL)
        B.expect(dlg).to_have_attribute("data-job", "crashed")
        B.expect(dlg.get_by_role("heading")).to_contain_text("The action stopped unexpectedly (crashed)")
        B.expect(dlg.get_by_role("heading")).to_contain_text("the step it started is still running")
        B.open_gate(self.gate)  # let the orphaned step end so nothing outlives the test
        B.until(lambda: not s.get("/api/job")["step_running"], "the orphaned step to end")


if __name__ == "__main__":
    unittest.main(verbosity=2)
