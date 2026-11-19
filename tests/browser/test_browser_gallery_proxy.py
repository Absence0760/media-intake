#!/usr/bin/env python3
"""An HEVC video in the gallery's large view, in headless Chromium: it plays while its H.264 proxy is still
being made, and from the cache after.

  .venv-browser/bin/python -I -m unittest discover -s tests/browser -t tests -p test_browser_gallery_proxy.py

The transcode runs the real tool (ffmpeg where it decodes HEVC, as on CI; else GStreamer's GPU decoder) behind
tests/test_gallery_proxy.py's GATED_TOOL, which holds it on a FIFO once it has written the whole proxy: the
page is checked to say it is converting and to play the video while the transcode has not ended (no proxy in
the cache yet), so it can't be passing on a finished file. Skips (fails with MEDIA_INTAKE_BROWSER_REQUIRED=1)
when no synthetic HEVC clip or no proxy can be made here.
"""
import os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_support as B  # noqa: E402  (puts tests/ on sys.path, hermetic config)
import test_gallery as G  # noqa: E402  (the server wrapper, sha, until)
import test_gallery_proxy as P  # noqa: E402  (mkclip, hevc_encoder, can_proxy, GATED_TOOL, open_gate)


class GalleryProxyBrowserTest(B.BrowserCase):
    def setUp(self):
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            B.unavailable("ffmpeg/ffprobe missing")
        if not P.hevc_encoder() or not P.H264:
            B.unavailable("no HEVC encoder (libx265 or VA-API hevc_vaapi) to make a synthetic clip with")
        if not P.can_proxy():
            B.unavailable("no way to make a proxy: ffmpeg can't decode HEVC and GStreamer has no GPU HEVC decoder "
                          "with an H.264 encoder")
        self.tmp = tempfile.mkdtemp(prefix="gallery-proxy-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        state = os.path.join(home, ".local/share/media-intake")
        os.makedirs(state)
        self.clip = os.path.join(home, "Videos/personal/2026/2026-11/IMG_0100.MOV")
        P.mkclip(self.clip, "hevc", seconds=6, size="640x360")
        bin_ = os.path.join(self.tmp, "fakebin")
        os.makedirs(bin_)
        for n in ("ffmpeg", "gst-launch-1.0"):
            p = os.path.join(bin_, n)
            with open(p, "w") as f:
                f.write(P.GATED_TOOL)
            os.chmod(p, 0o700)
        self.gate, self.flag = os.path.join(self.tmp, "tool-gate"), os.path.join(self.tmp, "tool-started")
        os.mkfifo(self.gate)
        self.addCleanup(P.open_gate, self.gate)  # never leave the gated tool blocked
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=state,
                        PHOTO_LIBRARY=os.path.join(home, "Pictures/library"),
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=B.write_config(os.path.join(home, "Videos/demo-footage")),
                        PATH=bin_ + os.pathsep + os.environ.get("PATH", ""), REAL_FFMPEG=shutil.which("ffmpeg"),
                        REAL_GST_LAUNCH=shutil.which("gst-launch-1.0") or "", TOOL_GATE=self.gate,
                        TOOL_STARTED=self.flag)
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.proxy = os.path.join(home, ".cache/media-intake/proxies", G.sha(self.clip) + ".mp4")
        self.server = G.Server(self.env)
        self.addCleanup(self.server.stop)

    def test_plays_while_converting_then_from_the_cache(self):
        page = self.new_page()
        self.open(page, self.server)
        # the "converting" note may be up only a second: record it as it is added, not by looking later
        page.evaluate("""() => { window.convSeen = []; new MutationObserver(ms => ms.forEach(m => m.addedNodes
          .forEach(n => { if (n.id === "conv") window.convSeen.push([n.getAttribute("role"), n.textContent]) })))
          .observe(document.getElementById("stage"), {childList: true}) }""")
        page.locator('[data-item="IMG_0100.MOV"]').first.click()
        viewer = page.get_by_role("dialog", name="Item")
        B.expect(viewer).to_have_attribute("data-current", "IMG_0100.MOV")
        status = page.locator("#conv")
        page.wait_for_function("window.convSeen.length > 0")
        self.assertEqual(page.evaluate("window.convSeen"),
                         [["status", "Converting this video for the browser. It starts in a few seconds."]])
        video = viewer.locator("video")
        # it plays while the transcode is still held: what the player got was streamed as it was written
        self.assertGreater(video.evaluate(B.PLAYING), 0)
        G.until(lambda: os.path.exists(self.flag), "the transcode to be written")
        self.assertFalse(os.path.exists(self.proxy), "the proxy is finished: this would not show streaming")
        B.expect(status).to_have_count(0)  # gone once it plays
        with open(self.gate, "w") as g:  # let the transcode end
            g.write("go")
        G.until(lambda: os.path.isfile(self.proxy), "the proxy to be cached")
        # the large view again: the cached proxy, with a duration and Range, and no "converting"
        page.keyboard.press("Escape")
        B.expect(viewer).to_be_hidden()
        media = B.MediaRequests(page)
        page.locator('[data-item="IMG_0100.MOV"]').first.click()
        video = viewer.locator("video")
        self.assertGreater(video.evaluate(B.PLAYING), 0)
        self.assertAlmostEqual(video.evaluate("v => v.duration"), 6, delta=0.5)
        B.expect(page.locator("#conv")).to_have_count(0)
        src = video.evaluate("v => new URL(v.src).pathname")
        seen = media.of(src)
        self.assertTrue(seen and all(r["status"] == 206 and r["accept"] == "bytes" for r in seen), seen)


if __name__ == "__main__":
    import unittest
    unittest.main()
