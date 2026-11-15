#!/usr/bin/env python3
"""Undo in the gallery (gallery.py: App.journal, App.undo, /api/undo) on synthetic media in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_gallery_undo.py

Keep, my tags, Rotate and Set event reply with an undo token; /api/undo puts back exactly what that action
changed in each sidecar (tags, dc:Subject leaves, XMP-tiff:Orientation, XMP-iptcExt:Event), under the library
lock, and refuses (409, nothing written) once an item's sidecar changed again or the item left the library
(removed or renamed); a token older than the last UNDO_KEEP actions is refused too (400). Trash, Erase,
Share-safe copy and Trash suggestions' Apply never carry a token. Sidecars are written and read with exiftool (the server's own
writer); UndoBlockTest runs in-process and needs none.
"""
import os, shutil, subprocess, sys, tempfile, unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config; puts media_intake on sys.path)
import test_gallery as G  # noqa: E402  (fixtures and the server wrapper)

import gallery  # noqa: E402

HAVE_EXIFTOOL = bool(shutil.which("exiftool"))


def state(p):
    """The fields an undo restores, as sets (Bag order is not meaningful): tags, dc:Subject, orientation, event."""
    x = p + ".xmp"
    if not os.path.exists(x):
        return (frozenset(), frozenset(), None, None)
    r = subprocess.run(["exiftool", "-j", "-q", "-n", "-HierarchicalSubject", "-XMP-dc:Subject",
                        "-XMP-tiff:Orientation", "-XMP-iptcExt:Event", x], capture_output=True, text=True, check=True)
    row = __import__("json").loads(r.stdout)[0]
    as_s = lambda v: frozenset() if v is None else frozenset(map(str, v if isinstance(v, list) else [v]))  # noqa: E731
    return as_s(row.get("HierarchicalSubject")), as_s(row.get("Subject")), row.get("Orientation"), row.get("Event")


def side(p):
    """A sidecar's bytes, to show nothing wrote it."""
    with open(p + ".xmp", "rb") as f:
        return f.read()


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool is not installed")
class UndoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-undo-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        L = lambda n: os.path.join(self.lib, "2026/2026-11", n)  # noqa: E731
        self.a, self.b, self.c, self.d = L("a.jpg"), L("b.jpg"), L("c.jpg"), L("d.jpg")
        for p, col in ((self.a, (200, 30, 30)), (self.b, (30, 200, 30)), (self.c, (30, 30, 200)),
                       (self.d, (90, 90, 90))):
            G.img(p, col)
        # a: every tag Keep removes, a leaf ("trash") another tag of mine still uses, an event in both fields,
        # an orientation; b: no sidecar at all; c: two events; d: a plain sidecar of mine
        G.write_tags(self.a, ["Project|Personal", "Review|maybe-trash", "Owner|trash", "Review|not-trash",
                              "Label|food", "Bins|trash", "Event|picnic"],
                     ["-XMP-iptcExt:Event=picnic", "-XMP-tiff:Orientation=6", "-n"])
        G.write_tags(self.c, ["Event|hike", "Event|beach", "Trips|Spain"])
        G.write_tags(self.d, ["Trips|Spain"])
        self.originals = {p: G.sha(p) for p in (self.a, self.b, self.c, self.d)}
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def start(self):
        s = G.Server(self.env)
        self.servers.append(s)
        self.ids = {i["n"]: i["id"] for i in s.get("/api/list?limit=500")["items"]}
        return s

    def snap(self):
        return {p: state(p) for p in (self.a, self.b, self.c, self.d)}

    def act(self, s, path, body):
        code, j = s.post(path, body)
        self.assertEqual(code, 200, j)
        self.assertIn("undo", j, j)
        return j

    def undo(self, s, j):
        code, u = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 200, u)
        return u

    def test_keep_undo_restores_every_tag_and_leaf(self):
        s = self.start()
        before = self.snap()
        sel = [self.ids["a.jpg"], self.ids["b.jpg"], self.ids["d.jpg"]]
        flags = {i["id"]: i["f"] for i in s.get("/api/list?limit=500")["items"]}
        j = self.act(s, "/api/keep", {"ids": sel})
        h = state(self.a)[0]
        self.assertLessEqual({"Owner|keep", "Owner|not-trash"}, h)
        self.assertFalse({"Review|maybe-trash", "Owner|trash", "Review|not-trash"} & h)
        self.assertIn("kept", [f for i in j["items"] for f in i["f"]])
        u = self.undo(s, j)
        self.assertEqual(u["result"], "undid Keep on 3")
        self.assertEqual(self.snap(), before)  # tags, leaves (maybe-trash and trash back), event, orientation
        self.assertEqual({i["id"] for i in u["items"]}, set(sel))
        self.assertEqual({i["id"]: i["f"] for i in u["items"]}, {i: flags[i] for i in sel})  # the tiles as before
        self.assertNotIn("kept", flags[self.ids["b.jpg"]])  # (a was kept already: the legacy Review|not-trash)
        # one use per token
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 400, e)
        self.assertIn("nothing to undo", e["error"])
        self.assertEqual(self.snap(), before)
        self.assertEqual({p: G.sha(p) for p in self.originals}, self.originals)  # originals never written

    def test_tag_rotate_and_event_undo(self):
        s = self.start()
        ida, idb, idc, idd = (self.ids[n] for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"))
        before = self.snap()
        # my tag added on three (d already has it: nothing changes there), then undone
        j = self.act(s, "/api/tag", {"ids": [ida, idb, idd], "tag": "Trips|Spain", "op": "add"})
        self.assertIn("Trips|Spain", state(self.b)[0])
        self.undo(s, j)
        self.assertEqual(self.snap(), before)
        # removed (its leaf too, where nothing else uses it), then undone: the tag and its leaf come back
        j = self.act(s, "/api/tag", {"ids": [idc, idd], "tag": "Trips|Spain", "op": "remove"})
        self.assertNotIn("Spain", state(self.d)[1])
        self.undo(s, j)
        self.assertEqual(self.snap(), before)
        # rotate: a's orientation 6 back to 6, b (none before) back to none
        j = self.act(s, "/api/rotate", {"ids": [ida, idb], "dir": "cw"})
        self.assertEqual((state(self.a)[2], state(self.b)[2]), (3, 6))
        self.assertEqual(s.get(f"/api/item/{idb}")["rot"], 90)
        u = self.undo(s, j)
        self.assertTrue(u["thumbs"])
        self.assertEqual(self.snap(), before)
        self.assertEqual(s.get(f"/api/item/{idb}")["rot"], 0)
        self.assertEqual(s.get(f"/api/item/{ida}")["rot"], 90)
        # set event: a (Event|picnic + XMP-iptcExt:Event), c (two events) and d (none) all back as they were
        j = self.act(s, "/api/event/set", {"ids": [ida, idc, idd], "event": "trip"})
        self.assertEqual(state(self.a)[3], "trip")
        self.assertEqual(sorted(n for n, _ in s.get("/api/facets")["facets"]["event"]), ["trip"])
        self.undo(s, j)
        self.assertEqual(self.snap(), before)
        self.assertEqual(sorted(n for n, _ in s.get("/api/facets")["facets"]["event"]), ["beach", "hike", "picnic"])
        # and clearing the event
        j = self.act(s, "/api/event/set", {"ids": [ida, idc], "event": ""})
        self.assertEqual(state(self.a)[3], None)
        self.undo(s, j)
        self.assertEqual(self.snap(), before)
        self.assertEqual({p: G.sha(p) for p in self.originals}, self.originals)

    def test_undo_refuses_after_the_items_changed_again(self):
        s = self.start()
        ida, idd = self.ids["a.jpg"], self.ids["d.jpg"]
        first = self.act(s, "/api/tag", {"ids": [ida, idd], "tag": "Trips|coast", "op": "add"})
        self.act(s, "/api/rotate", {"ids": [idd], "dir": "cw"})  # another action on one of the same items
        after = self.snap()
        code, e = s.post("/api/undo", {"undo": first["undo"]})
        self.assertEqual(code, 409, e)
        self.assertIn("changed again", e["error"])
        self.assertEqual(self.snap(), after)  # nothing written, not even on a, which didn't change
        code, e = s.post("/api/undo", {"undo": first["undo"]})
        self.assertEqual(code, 400, e)  # the refused token is spent
        # a change by another tool (digiKam, the CLI) refuses it too
        j = self.act(s, "/api/keep", {"ids": [ida]})
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-dc:Subject+=elsewhere", self.a + ".xmp"],
                       check=True)
        after = self.snap()
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 409, e)
        self.assertEqual(self.snap(), after)
        # an action on other items doesn't block an undo
        j = self.act(s, "/api/rotate", {"ids": [ida], "dir": "ccw"})
        self.act(s, "/api/tag", {"ids": [idd], "tag": "Trips|coast", "op": "remove"})
        self.undo(s, j)
        for bad in (None, "", "not-a-token", 7):
            code, e = s.post("/api/undo", {"undo": bad})
            self.assertEqual(code, 400, (bad, e))

    def test_undo_refuses_when_an_item_left_the_library(self):
        """An item removed or renamed since the action: 409, no sidecar written (the other items' bytes too),
        and the token is spent."""
        s = self.start()
        ida, idc, idd = self.ids["a.jpg"], self.ids["c.jpg"], self.ids["d.jpg"]
        # removed: a's original deleted (its sidecar left behind)
        j = self.act(s, "/api/tag", {"ids": [ida, idd], "tag": "Trips|coast", "op": "add"})
        os.remove(self.a)
        before = {p: side(p) for p in (self.a, self.c, self.d)}
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 409, e)
        self.assertEqual(e["error"], "can't undo: an item is no longer in the library; nothing was changed")
        self.assertEqual({p: side(p) for p in before}, before)
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 400, e)  # spent
        self.assertIn("nothing to undo", e["error"])
        # renamed: c and its sidecar moved to a new name, as a file manager or digiKam would
        j = self.act(s, "/api/tag", {"ids": [idc, idd], "tag": "Trips|hills", "op": "add"})
        c2 = os.path.join(os.path.dirname(self.c), "c-renamed.jpg")
        os.rename(self.c, c2)
        os.rename(self.c + ".xmp", c2 + ".xmp")
        before = {p: side(p) for p in (c2, self.d)}
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 409, e)
        self.assertIn("no longer in the library", e["error"])
        self.assertEqual({p: side(p) for p in before}, before)
        self.assertFalse(os.path.exists(self.c + ".xmp"))  # nothing written at the old name
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 400, e)
        self.assertEqual({p: G.sha(p) for p in (self.b, self.d)}, {p: self.originals[p] for p in (self.b, self.d)})

    def test_undo_refuses_a_token_older_than_undo_keep(self):
        """The server keeps the last UNDO_KEEP (50) undoable actions; an older token is refused (400, "too old")
        and writes nothing. The server runs with UNDO_KEEP patched to 2 so three actions evict the first."""
        self.assertEqual(gallery.UNDO_KEEP, 50)  # docs/gallery.md says the last 50
        launcher = os.path.join(self.tmp, "gallery_keep2.py")
        with open(launcher, "w") as f:
            f.write(f"import sys\nsys.path.insert(0, {G.HERE!r})\nimport gallery\ngallery.UNDO_KEEP = 2\n"
                    "sys.exit(gallery.main())\n")
        with unittest.mock.patch.object(G, "GALLERY", launcher):
            s = self.start()
        first, second, third = (self.act(s, "/api/rotate", {"ids": [self.ids[n]], "dir": "cw"})
                                for n in ("a.jpg", "b.jpg", "c.jpg"))
        after = self.snap()
        code, e = s.post("/api/undo", {"undo": first["undo"]})
        self.assertEqual(code, 400, e)
        self.assertIn("too old", e["error"])
        self.assertEqual(self.snap(), after)  # nothing written
        self.undo(s, third)  # the last two are still kept
        self.undo(s, second)
        self.assertEqual((state(self.b)[2], state(self.c)[2]), (None, None))
        self.assertEqual(state(self.a), after[self.a])  # a stays rotated

    def test_undo_holds_the_library_lock(self):
        """While a job (a trash held at a FIFO) holds the library lock, an undo gets 409 and writes nothing; it
        works once the job is done."""
        fake, gate = os.path.join(self.tmp, "gated_intake.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(G.GATED)
        os.mkfifo(gate)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s = self.start()
        before = self.snap()
        j = self.act(s, "/api/rotate", {"ids": [self.ids["b.jpg"]], "dir": "cw"})
        code, t = s.post("/api/trash", {"ids": [self.ids["c.jpg"]], "confirm": True})
        self.assertEqual(code, 200, t)
        self.assertNotIn("undo", t)
        G.until(lambda: any("waiting at the gate" in x for x in s.get("/api/job")["lines"]), "the trash to start")
        mid = self.snap()
        code, e = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 409, e)
        self.assertRegex(e["error"], r"the library is busy: job gallery")
        self.assertEqual(self.snap(), mid)
        fd = os.open(gate, os.O_WRONLY)
        os.write(fd, b"go")
        os.close(fd)
        G.until(lambda: s.get("/api/job")["done"], "the trash to end")
        self.undo(s, j)  # the token survived the refusal at the lock
        self.assertEqual(state(self.b), before[self.b])

    def test_no_undo_for_trash_erase_share_or_apply(self):
        s = self.start()
        code, j = s.post("/api/decide", {"keep": [self.ids["a.jpg"]], "trash": [], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertNotIn("undo", j)
        code, j = s.post("/api/share", {"ids": [self.ids["d.jpg"]]})
        self.assertEqual(code, 200, j)
        self.assertNotIn("undo", j)
        G.until(lambda: s.get("/api/job")["done"], "the share-safe copy to end")
        code, j = s.post("/api/trash", {"ids": [self.ids["c.jpg"]], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertNotIn("undo", j)
        G.until(lambda: s.get("/api/job")["done"], "the trash to end")
        code, j = s.post("/api/erase", {"ids": [self.ids["b.jpg"]], "confirm": "ERASE"})
        self.assertEqual(code, 200, j)
        self.assertNotIn("undo", j)
        G.until(lambda: s.get("/api/job")["done"], "the erase to end")
        # an action that changed nothing (the tag is not there) has nothing to undo
        code, j = s.post("/api/tag", {"ids": [self.ids["a.jpg"]], "tag": "nowhere", "op": "remove"})
        self.assertEqual(code, 200, j)
        self.assertNotIn("undo", j)


class UndoBlockTest(unittest.TestCase):
    """App.undo_block in-process: the reverse of a recorded change, and the refusal of anything outside what
    that kind of action writes (a record can't be turned into a write of the tools' namespaces)."""

    def st(self, tags=(), subject=(), orient=None, xevent=""):
        return dict(tags=frozenset(tags), subject=frozenset(subject), orient=orient, xevent=xevent)

    def test_reverse_of_keep(self):
        old = self.st(["Review|maybe-trash", "Label|food"], ["maybe-trash", "food"])
        new = self.st(["Owner|keep", "Owner|not-trash", "Label|food"], ["keep", "not-trash", "food"])
        b = gallery.App.undo_block("keep", old, new, "/x.xmp")
        self.assertEqual(b[-1], "/x.xmp")
        for a in ("-XMP-lr:HierarchicalSubject-=Owner|keep", "-XMP-lr:HierarchicalSubject-=Owner|not-trash",
                  "-XMP-lr:HierarchicalSubject+=Review|maybe-trash", "-XMP-dc:Subject-=keep",
                  "-XMP-dc:Subject-=not-trash", "-XMP-dc:Subject+=maybe-trash"):
            self.assertIn(a, b)
        self.assertFalse([a for a in b if "food" in a or "Orientation" in a or "Event" in a])

    def test_orientation_and_event_put_back_or_deleted(self):
        b = gallery.App.undo_block("rotate", self.st(), self.st(orient="6"), "/x.xmp")
        self.assertIn("-XMP-tiff:Orientation=", b)
        b = gallery.App.undo_block("event", self.st(["Event|a"], ["a"], xevent="a"),
                                   self.st(["Event|b"], ["b"], xevent="b"), "/x.xmp")
        self.assertIn("-XMP-iptcExt:Event=a", b)
        self.assertIn("-XMP-lr:HierarchicalSubject+=Event|a", b)

    def test_refuses_outside_the_actions_scope(self):
        cases = [("tag", self.st(), self.st(["Owner|keep"])),          # a tool namespace from "my tags"
                 ("tag", self.st(), self.st(orient="6")),              # an orientation from a tag action
                 ("rotate", self.st(), self.st(["Trips|x"])),          # a tag from Rotate
                 ("event", self.st(), self.st(["Label|x"])),           # another namespace from Set event
                 ("keep", self.st(), self.st(["Project|x"])),          # Keep never writes Project|
                 ("keep", self.st(), self.st(xevent="x")),
                 ("tag", self.st(), self.st(["-bad"]))]                # never an exiftool option
        for kind, old, new in cases:
            with self.assertRaises(ValueError, msg=(kind, new)):
                gallery.App.undo_block(kind, old, new, "/x.xmp")


if __name__ == "__main__":
    unittest.main()
