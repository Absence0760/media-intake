#!/usr/bin/env python3
"""Burst stacks in the gallery (gallery.py over bursts.py): which library files stack, and how /api/list
orders and marks them. Synthetic only.

  python3 -I -m unittest discover -s tests -p test_gallery_bursts.py

attach_bursts() runs in-process on hand-made items and a throwaway state folder holding the plan.json
intake would have written (dates with seconds, device, dHash, a camera's burst id). /api/list runs on a
real gallery server over a throwaway HOME. Sidecars are hand-written XMP, so nothing here needs exiftool.
"""
import json, os, shutil, sys, tempfile, unittest, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config; puts media_intake on sys.path)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper)

import gallery  # noqa: E402

GCAMERA = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
           "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
           "<rdf:Description rdf:about='' xmlns:GCamera='http://ns.google.com/photos/1.0/camera/'\n"
           " xmlns:exif='http://ns.adobe.com/exif/1.0/'>\n"
           " <GCamera:BurstID>{bid}</GCamera:BurstID>\n <exif:DateTimeOriginal>{dt}</exif:DateTimeOriginal>\n"
           "</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")


def entry(dst, date, device="phone-a", dhash=0, why="", burst_id=None, action="file", lib_dst=None):
    p = dict(rel=os.path.basename(dst or lib_dst), dst=dst, lib_dst=lib_dst or dst, date=date, device=device, dhash=dhash,
             why=why, action=action)
    if burst_id is not None:
        p["burst_id"] = burst_id
    return p


# The burst library (also tests/browser/test_browser_gallery_bursts.py): name -> (sidecar date, plan date,
# extra tags, plan why). IMG_0001-0003 are one burst (one device, 0.5 s apart); copy.jpg is a near-copy
# from the same second (planned as a near-duplicate: never stacked); lone and early are shots of their own.
# The sidecars' dates (minutes) sort the timeline, newest first, then by path: lone, copy, the burst's
# shots in reverse (which the stack puts back in time order), early.
BURST_LIBRARY = {
    "lone.jpg": ("2026-11-08T09:00:00", "2026-11-08T09:00:00", [], ""),
    "copy.jpg": ("2026-11-08T07:15:00", "2026-11-08T07:15:00.300000", [], "near-duplicate of DCIM/x.jpg"),
    "IMG_0001.jpg": ("2026-11-08T07:15:00", "2026-11-08T07:15:00.100000", ["Review|maybe-trash"], ""),
    "IMG_0002.jpg": ("2026-11-08T07:15:00", "2026-11-08T07:15:00.600000", ["Review|child-in-shot"], ""),
    "IMG_0003.jpg": ("2026-11-08T07:15:01", "2026-11-08T07:15:01.100000", ["Review|maybe-trash"], ""),
    "early.jpg": ("2026-11-08T06:00:00", "2026-11-08T06:00:00", [], ""),
}
TIMELINE = ["lone.jpg", "copy.jpg", "IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg", "early.jpg"]


def burst_library(lib, state):
    """Write BURST_LIBRARY under lib (Pillow images, plain-XMP sidecars) and the plan.json intake would have
    written for it under state. Returns {name: path}."""
    d = os.path.join(lib, "2026/2026-11")
    paths, plan = {}, []
    for k, (n, (xdate, pdate, extra, why)) in enumerate(BURST_LIBRARY.items()):
        p = paths[n] = os.path.join(d, n)
        G.img(p, (40 * k, 90, 160))
        G.raw_xmp(p, ["Project|Personal"] + extra)
        with open(p + ".xmp") as f:
            x = f.read()
        with open(p + ".xmp", "w") as f:
            f.write(x.replace("<rdf:Description rdf:about=''", "<rdf:Description rdf:about='' "
                              f"xmlns:exif='http://ns.adobe.com/exif/1.0/' exif:DateTimeOriginal='{xdate}'", 1))
        plan.append(entry(p, pdate, why=why, action="review" if why else "file"))
    b = os.path.join(state, "batches", "b1")
    os.makedirs(b, exist_ok=True)
    with open(os.path.join(b, "plan.json"), "w") as f:
        json.dump(plan, f)
    return paths


class AttachBurstsTest(unittest.TestCase):
    """Which items get a burst: in-process, no file is read but the plans and sidecars."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-bursts-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.lib = os.path.join(self.tmp, "lib")

    def path(self, name):
        return os.path.join(self.lib, "2026/2026-11", name)

    def plan(self, batch, entries):
        d = os.path.join(self.state, "batches", batch)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "plan.json"), "w") as f:
            json.dump(entries, f)

    def item(self, name, media="photo", date="2026-11-08 07:15", xdate="", xburst=""):
        return dict(path=self.path(name), media=media, date=date, xdate=xdate, xburst=xburst)

    def groups(self, items):
        gallery.attach_bursts(items, index={}, state=self.state)
        out = {}
        for it in items:
            if it["burst"] is not None:
                out.setdefault(it["burst"], []).append((it["burst_pos"], os.path.basename(it["path"])))
        return sorted([n for _, n in sorted(g)] for g in out.values())

    def test_planned_shots_stack_in_time_order(self):
        self.plan("b1", [entry(self.path("c.jpg"), "2026-11-08T07:15:01.200000"),
                         entry(self.path("a.jpg"), "2026-11-08T07:15:00"),
                         entry(self.path("b.jpg"), "2026-11-08T07:15:00.600000", dhash=0b111),
                         entry(self.path("later.jpg"), "2026-11-08T07:15:09")])
        items = [self.item(n) for n in ("later.jpg", "c.jpg", "b.jpg", "a.jpg")]
        self.assertEqual(self.groups(items), [["a.jpg", "b.jpg", "c.jpg"]])
        self.assertIsNone(items[0]["burst"])  # 8 s later: a shot of its own

    def test_a_near_copy_from_the_same_second_is_never_stacked(self):
        """A file intake planned as a near-duplicate is a copy, not a shot: never stacked, even on the same
        device and second with a near-identical dHash. Nor is a copy from another device, or one with no plan
        entry and no burst marker (its device is unknown)."""
        self.plan("b1", [entry(self.path("a.jpg"), "2026-11-08T07:15:00"),
                         entry(self.path("b.jpg"), "2026-11-08T07:15:00.500000"),
                         entry(self.path("a-copy.jpg"), "2026-11-08T07:15:00.200000", dhash=0b1,
                               why="near-duplicate of DCIM/a.jpg", action="review",
                               lib_dst=self.path("a-copy.jpg")),
                         entry(self.path("sent.jpg"), "2026-11-08T07:15:00.300000", device=None, dhash=0b11)])
        items = [self.item(n) for n in ("a.jpg", "b.jpg", "a-copy.jpg", "sent.jpg", "loose.jpg")]
        items[-1]["xdate"] = "2026:11:08 07:15:00"
        self.assertEqual(self.groups(items), [["a.jpg", "b.jpg"]])
        for it in items[2:]:
            self.assertIsNone(it["burst"], it["path"])

    def test_released_file_found_by_lib_dst_and_videos_never_stack(self):
        self.plan("b1", [entry(self.path("a.jpg"), "2026-11-08T07:15:00"),
                         entry(None, "2026-11-08T07:15:00.400000", action="quarantine", lib_dst=self.path("b.jpg")),
                         entry(self.path("v.mp4"), "2026-11-08T07:15:00.800000", dhash=None)])
        items = [self.item("a.jpg"), self.item("b.jpg"), self.item("v.mp4", media="video")]
        self.assertEqual(self.groups(items), [["a.jpg", "b.jpg"]])
        self.assertIsNone(items[2]["burst"])

    def test_camera_burst_id_wins_over_the_clock(self):
        """The plan entry's burst_id (BurstUUID): one burst whatever the gap; two ids never one burst."""
        self.plan("b1", [entry(self.path("a.jpg"), "2026-11-08T07:15:00", burst_id="UUID-1"),
                         entry(self.path("b.jpg"), "2026-11-08T07:15:06", burst_id="UUID-1"),
                         entry(self.path("c.jpg"), "2026-11-08T07:15:06.300000", burst_id="UUID-2"),
                         entry(self.path("d.jpg"), "2026-11-08T07:15:06.600000", burst_id="UUID-2")])
        items = [self.item(n) for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg")]
        self.assertEqual(self.groups(items), [["a.jpg", "b.jpg"], ["c.jpg", "d.jpg"]])

    def test_a_later_batch_wins_and_bad_plans_are_skipped(self):
        self.plan("b1", [entry(self.path("a.jpg"), "2026-11-08T07:15:00", device="phone-b")])
        self.plan("b2", [entry(self.path("a.jpg"), "2026-11-08T07:15:00"),
                         entry(self.path("b.jpg"), "2026-11-08T07:15:01"), "not an entry"])
        os.makedirs(os.path.join(self.state, "batches", "b3"))
        with open(os.path.join(self.state, "batches", "b3", "plan.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(self.groups([self.item("a.jpg"), self.item("b.jpg")]), [["a.jpg", "b.jpg"]])

    def test_unplanned_files_stack_only_by_a_marker(self):
        """No plan entry: a Pixel burst name or a sidecar's GCamera:BurstID stacks; the clock alone never does."""
        names = ["00000IMG_00000_BURST20261108071500123_COVER.jpg", "00001IMG_00001_BURST20261108071500123.jpg"]
        items = [self.item(n, xdate="2026:11:08 07:15:00") for n in names]
        items += [self.item("g1.jpg", xdate="2026:11:08 07:20:00", xburst="pixel-77"),
                  self.item("g2.jpg", xdate="2026:11:08 07:20:09", xburst="pixel-77"),
                  self.item("plain1.jpg", xdate="2026:11:08 07:30:00"),
                  self.item("plain2.jpg", xdate="2026:11:08 07:30:00")]
        self.assertEqual(self.groups(items), [names, ["g1.jpg", "g2.jpg"]])

    def test_sidecar_burst_id_and_seconds_are_read(self):
        p = self.path("g.jpg")
        os.makedirs(os.path.dirname(p))
        with open(p + ".xmp", "w") as f:
            f.write(GCAMERA.format(bid="pixel-77", dt="2026-11-08T07:15:03"))
        x = gallery.read_xmp(p + ".xmp")
        self.assertEqual((x["burst_id"], x["date"]), ("pixel-77", "2026-11-08T07:15:03"))
        self.assertEqual(gallery.iso_seconds(x["date"]), "2026-11-08T07:15:03")
        self.assertEqual(gallery.iso_seconds("2026:11:08 07:15"), "2026-11-08T07:15:00")
        self.assertIsNone(gallery.iso_seconds("2026-11"))


class ListingTest(unittest.TestCase):
    """/api/list on a real server: a burst's listed shots follow one another, the cover first, and a filter or
    a ranked list never makes a stack of one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-bursts-list-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.p = burst_library(self.lib, self.state)
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def listing(self, **f):
        s = G.Server(self.env)
        self.servers.append(s)
        return s.get("/api/list?" + urllib.parse.urlencode(dict(f, limit=500)))["items"]

    def test_stack_runs_cover_first(self):
        rows = self.listing()
        self.assertEqual([r["n"] for r in rows],
                         TIMELINE)
        burst = [r for r in rows if r.get("b") is not None]
        self.assertEqual([(r["n"], r["bn"], r["bc"]) for r in burst],
                         [("IMG_0001.jpg", 3, True), ("IMG_0002.jpg", 3, False), ("IMG_0003.jpg", 3, False)])
        self.assertEqual(len({r["b"] for r in burst}), 1)
        for r in rows:
            if r["n"] in ("lone.jpg", "copy.jpg", "early.jpg"):
                self.assertNotIn("b", r, r["n"])  # the near-copy from the same second is never stacked

    def test_paging_keeps_the_run(self):
        s = G.Server(self.env)
        self.servers.append(s)
        names = []
        for off in range(0, 6, 2):
            names += [r["n"] for r in s.get(f"/api/list?offset={off}&limit=2")["items"]]
        self.assertEqual(names, TIMELINE)

    def test_a_filter_leaving_one_shot_makes_no_stack(self):
        rows = self.listing(review="child-in-shot")
        self.assertEqual([r["n"] for r in rows], ["IMG_0002.jpg"])
        self.assertNotIn("b", rows[0])

    def test_ranked_lists_never_stack(self):
        rows = self.listing(view="suggest")
        self.assertEqual(sorted(r["n"] for r in rows), ["IMG_0001.jpg", "IMG_0003.jpg"])  # two shots of the burst
        self.assertFalse([r for r in rows if "b" in r])


if __name__ == "__main__":
    unittest.main()
