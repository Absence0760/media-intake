#!/usr/bin/env python3
"""Tag precedence: who writes what, and owner decisions (owner.py) winning everywhere.

  python3 -I -m unittest discover -s tests -p test_tag_precedence.py

Every tool that writes tags replaces only its own namespace and never an Owner| tag or
your own keywords: intake's re-tag (merge_tags, tag-files), Keep (not-trash, the gallery),
trash (Owner|trash on surviving copies) and migrate-tags. vlm_trash's own-namespace writes
are in test_vlm_trash.py. Synthetic media, throwaway HOME; needs exiftool.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
from test_intake import INTAKE, PY, jload, pattern, save_jpg  # noqa: E402

import intake  # noqa: E402
import owner  # noqa: E402

EMPTY = intake.EMPTY_XMP


class OwnerRules(unittest.TestCase):
    def test_kept_trashed_decided(self):
        self.assertTrue(owner.kept(["Owner|keep"]))
        self.assertTrue(owner.kept(["Owner|not-trash"]))
        self.assertTrue(owner.kept(["Review|not-trash"]))  # legacy, read until migrated
        self.assertTrue(owner.kept([], 3) and owner.kept([], "3"))  # digiKam Accepted, number or text
        self.assertFalse(owner.kept(["Owner|trash", "Review|maybe-trash"], "1"))
        self.assertTrue(owner.trashed(["Owner|trash"]))
        self.assertFalse(owner.trashed(["Owner|trash", "Owner|keep"]))  # a keep wins: nothing is deleted on a tie
        for t, pick in ((["Owner|trash"], ""), ([], "1"), ([], 2), (["Review|not-trash"], "")):
            self.assertTrue(owner.decided(t, pick), (t, pick))
        self.assertFalse(owner.decided(["Review|maybe-trash", "AI|vlm/m/trash/dark", "Label|x"], "0"))
        self.assertEqual(owner.normalise(["a", "Review|not-trash"]), ["a", "Owner|not-trash"])


class MergeTags(unittest.TestCase):
    """The re-tag (tag, tag-files): SigLIP replaces only its namespace, facts only their prefix."""

    OWNER = ["Owner|keep", "Owner|not-trash", "Owner|trash", "pasta", "Used|2026-11-14-demo-x",
             "AI|vlm/qwen3-vl-2b/trash/dark", "AI|vlm/qwen3-vl-2b/conf/0.9", "Review|vlm-keep", "Review|child-in-shot"]

    def test_relabel_replaces_only_siglip_namespace(self):
        old = ["Label|food", "Review|maybe-trash", "Review|untagged-video", "Project|Personal", "Source|gopro",
               "Event|old", "Place|Riverton, EL, US"] + self.OWNER
        new = ["Project|Personal", "Source|phone camera", "Label|kitchen"]
        hier, remove, leaves = intake.merge_tags(old, new, relabelled=True)
        self.assertEqual(sorted(remove), sorted(["Label|food", "Review|maybe-trash", "Review|untagged-video",
                                                 "Source|gopro"]))
        for t in self.OWNER + ["Event|old", "Place|Riverton, EL, US"]:  # no value this run: kept
            self.assertNotIn(t, remove)
        self.assertEqual(sorted(leaves), ["food", "gopro", "maybe-trash", "untagged-video"])
        self.assertEqual(hier, new)

    def test_no_labels_run_leaves_siglip_tags(self):
        old = ["Label|food", "Review|maybe-trash"] + self.OWNER
        _, remove, _ = intake.merge_tags(old, ["Project|Personal"], relabelled=False)
        self.assertEqual(remove, [])

    def test_owner_decision_blocks_maybe_trash(self):
        for old, pick in ((["Owner|keep"], ""), (["Owner|not-trash"], ""), (["Owner|trash"], ""),
                          (["Review|not-trash"], ""), ([], "3")):
            hier, _, _ = intake.merge_tags(old, ["Project|Personal", "Review|maybe-trash"], pick)
            self.assertNotIn("Review|maybe-trash", hier, old)
        hier, _, _ = intake.merge_tags([], ["Review|maybe-trash"], "2")  # digiKam Pending is not a decision
        self.assertIn("Review|maybe-trash", hier)

    def test_legacy_rewritten_on_write(self):
        hier, remove, leaves = intake.merge_tags(["Review|not-trash", "Label|food"], ["Label|food"], relabelled=False)
        self.assertIn("Owner|not-trash", hier)
        self.assertEqual(remove, ["Review|not-trash"])
        self.assertEqual(leaves, [])  # "not-trash" stays: Owner|not-trash uses the same leaf
        hier, remove, _ = intake.merge_tags(["Review|not-trash", "Owner|not-trash"], [], relabelled=True)
        self.assertEqual((hier, remove), (["Owner|not-trash"], ["Review|not-trash"]))

    def test_keep_args(self):
        b = intake.keep_args(["Review|maybe-trash", "Owner|trash", "Label|food", "food"], owner_leaves={"x"})
        for t in ("Review|maybe-trash", "Owner|trash", "Review|not-trash"):
            self.assertIn(f"-XMP-lr:HierarchicalSubject-={t}", b)
        self.assertIn("-XMP-dc:Subject-=maybe-trash", b)
        self.assertIn("-XMP-dc:Subject-=trash", b)
        self.assertIn("-XMP-lr:HierarchicalSubject+=Owner|keep", b)
        self.assertIn("-XMP-lr:HierarchicalSubject+=Owner|not-trash", b)
        self.assertFalse([x for x in b if "food" in x or "Label|" in x])  # nothing else touched
        # a leaf your own keyword (or digiKam's TagsList) still uses stays
        b = intake.keep_args(["Review|maybe-trash", "Owner|trash"], owner_leaves={"maybe-trash", "trash"})
        self.assertFalse([x for x in b if x.startswith("-XMP-dc:Subject-=") and x[-5:] == "trash"
                          and not x.endswith("not-trash")])


class Semantic(unittest.TestCase):
    def test_clean_honours_owner(self):
        import semantic
        a = argparse.Namespace(project=None, clean=True)
        ok = lambda t: semantic.passes(t, a, None)  # noqa: E731
        self.assertFalse(ok(["Review|maybe-trash"]))
        self.assertFalse(ok(["AI|vlm/m/trash/dark"]))
        self.assertTrue(ok(["AI|vlm/m/trash/dark", "Owner|not-trash"]))  # you kept it: the AI flag is moot
        self.assertTrue(ok(["Review|maybe-trash", "Review|not-trash"]))  # legacy spelling too
        self.assertFalse(ok(["Owner|trash"]))
        self.assertFalse(ok(["Owner|keep", "Review|child-in-shot"]))  # a child flag is never cleared by a keep
        self.assertTrue(ok(["Label|food"]))


class Base(unittest.TestCase):
    """A throwaway HOME with an empty library; helpers for sidecars and the real intake.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-owner-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        TZ="America/New_York", MEDIA_INTAKE_NO_LOCALSEARCH="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def photo(self, name, seed, hier=(), extra=()):
        p = os.path.join(self.lib, "2026/2026-11", name)
        save_jpg(pattern(seed), p)
        with open(p + ".xmp", "w") as f:
            f.write(EMPTY)
        args = [a for h in hier for a in (f"-XMP-lr:HierarchicalSubject+={h}", f"-XMP-dc:Subject+={h.split('|')[-1]}")]
        if args or extra:
            subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", *args, *extra, p + ".xmp"], check=True)
        return p

    def read(self, p):
        r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", "-XMP-dc:Subject", p + ".xmp"],
                           capture_output=True, text=True, check=True)
        row = json.loads(r.stdout)[0]
        lst = lambda v: sorted([] if v is None else v if isinstance(v, list) else [v])  # noqa: E731
        return lst(row.get("HierarchicalSubject")), lst(row.get("Subject"))

    def suggested(self):
        self.intake("suggest-trash", "--no-serve")
        return {x["path"]: x["labels"] for x in jload(os.path.join(self.state, "maybe-trash.json"))["items"]}


class Sidecars(Base):
    """not-trash, trash, suggest-trash, migrate-tags and tag-files through the real intake.py."""

    def test_not_trash_is_keep_and_supersedes_trash(self):
        p = self.photo("k.jpg", 21, ["Review|maybe-trash", "Owner|trash", "Review|not-trash", "Label|food", "pasta"],
                       ["-XMP-dc:Subject+=maybe-trash"])
        out = self.intake("not-trash", p)
        self.assertIn("Owner|keep + Owner|not-trash", out)
        h, dc = self.read(p)
        self.assertEqual(h, ["Label|food", "Owner|keep", "Owner|not-trash", "pasta"])
        self.assertEqual(dc, ["food", "keep", "not-trash", "pasta"])
        self.intake("not-trash", p)  # idempotent
        self.assertEqual(self.read(p)[0], h)

    def test_trash_marks_surviving_copies(self):
        a = self.photo("a.jpg", 22, ["Review|maybe-trash"])
        copy = os.path.join(self.lib, "2025/2025-01/a-copy.jpg")
        kept = os.path.join(self.lib, "2025/2025-02/a-kept.jpg")
        for c in (copy, kept):
            os.makedirs(os.path.dirname(c), exist_ok=True)
            shutil.copyfile(a, c)
        with open(kept + ".xmp", "w") as f:
            f.write(EMPTY)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-lr:HierarchicalSubject+=Owner|keep",
                        kept + ".xmp"], check=True)
        s = intake.sha256(a)
        os.makedirs(self.state, mode=0o700)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({p: {"sha": s, "size": os.path.getsize(a), "dhash": None} for p in (a, copy, kept)}, f)
        out = self.intake("trash", a)
        self.assertIn("copies left on disk marked Owner|trash 1", out)
        self.assertIn("copies you kept left alone 1", out)
        self.assertFalse(os.path.exists(a) or os.path.exists(a + ".xmp"))
        self.assertEqual(jload(os.path.join(self.state, "trashed.json")), {s: os.path.getsize(copy)})
        self.assertEqual(self.read(copy), (["Owner|trash"], ["trash"]))
        self.assertEqual(self.read(kept)[0], ["Owner|keep"])  # its own decision stands
        self.assertTrue(os.path.isfile(copy) and os.path.isfile(kept), "only the named path is deleted")
        # the copy is offered for confirmation; the kept one never is
        self.assertEqual(self.suggested(), {copy: ["Owner|trash"]})
        # Keep on the copy supersedes the older trash decision
        self.intake("not-trash", copy)
        self.assertEqual(self.read(copy)[0], ["Owner|keep", "Owner|not-trash"])
        self.assertEqual(self.suggested(), {})

    def test_suggest_trash_owner_precedence(self):
        f = {n: self.photo(f"{n}.jpg", 30 + i, ["Review|maybe-trash"] + extra) for i, (n, extra) in enumerate((
            ("plain", []), ("keep", ["Owner|keep"]), ("not-trash", ["Owner|not-trash"]),
            ("legacy", ["Review|not-trash"]), ("ai-kept", ["AI|vlm/m/trash/dark", "AI|vlm/m/conf/0.9",
                                                           "Owner|not-trash"])))}
        f["accepted"] = self.photo("accepted.jpg", 40, ["AI|vlm/m/trash/dark"], ["-XMP-digiKam:PickLabel=3"])
        self.assertEqual(list(self.suggested()), [f["plain"]])

    def test_migrate_tags(self):
        leg = self.photo("leg.jpg", 23, ["Review|not-trash", "Review|maybe-trash", "Label|food", "pasta"],
                         ["-XMP-digiKam:PickLabel=2"])
        both = self.photo("both.jpg", 24, ["Review|not-trash", "Owner|not-trash"])
        other = self.photo("other.jpg", 25, ["Label|food"])
        before = self.read(other)
        self.assertEqual(self.suggested(), {})  # unmigrated, the legacy decision is still honoured
        self.assertIn("2 sidecars have the legacy", self.intake("migrate-tags", "--dry-run"))
        self.assertIn("Review|not-trash", self.read(leg)[0])
        self.assertIn("migrated 2 sidecars", self.intake("migrate-tags"))
        self.assertEqual(self.read(leg), (["Label|food", "Owner|not-trash", "Review|maybe-trash", "pasta"],
                                          ["food", "maybe-trash", "not-trash", "pasta"]))
        self.assertEqual(self.read(both)[0], ["Owner|not-trash"])
        self.assertEqual(self.read(other), before)
        self.assertIn("migrated 0 sidecars", self.intake("migrate-tags"))  # idempotent
        self.assertEqual(self.suggested(), {})  # still kept after the migration

    def fake_venv(self, labels):
        py = os.path.join(self.state, "venv/bin/python")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        lj = os.path.join(self.tmp, "labels.json")
        with open(lj, "w") as fh:
            json.dump(labels, fh)
        with open(py, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\ncat '{lj}'\n")
        os.chmod(py, 0o755)

    def test_retag_never_touches_owner_tags(self):
        mine = ["Owner|keep", "Owner|not-trash", "pasta", "Used|2026-11-14-demo-x", "AI|vlm/m/keep"]
        k = self.photo("k.jpg", 26, ["Label|food", "Review|not-trash"] + mine)
        t = self.photo("t.jpg", 27, ["Label|food", "Owner|trash"])
        n = self.photo("n.jpg", 28, ["Label|food", "Review|maybe-trash", "mine"])
        self.fake_venv({p: {"labels": {"kitchen": 0.3}, "review": ["maybe-trash"]} for p in (k, t, n)})
        self.intake("tag-files", k, t, n)
        hk, dk = self.read(k)
        for want in mine + ["Label|kitchen", "Project|Personal"]:
            self.assertIn(want, hk)
        for gone in ("Label|food", "Review|maybe-trash", "Review|not-trash"):  # replaced; legacy migrated
            self.assertNotIn(gone, hk)
        self.assertIn("not-trash", dk)
        ht, _ = self.read(t)
        self.assertIn("Owner|trash", ht)
        self.assertNotIn("Review|maybe-trash", ht)  # you already decided
        hn, _ = self.read(n)
        self.assertEqual(sorted(x for x in hn if not x.startswith(("Project|", "Source|"))),
                         ["Label|kitchen", "Review|maybe-trash", "mine"])


GALLERY_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import gallery
app = gallery.App(gallery.scan_library()[0])
by = {it["path"]: i for i, it in enumerate(app.items)}
before = {p: app.suggested(app.items[i]) for p, i in by.items()}
app.keep([by[p] for p in sys.argv[2:]])
print(json.dumps(dict(before=before, after={p: app.suggested(app.items[i]) for p, i in by.items()})))
'''


class Gallery(Base):
    """The gallery's Keep is the same rule as the review page's (intake.keep_args)."""

    def test_gallery_keep_and_trashed_copy(self):
        a = self.photo("a.jpg", 50, ["Review|maybe-trash", "Owner|trash", "Review|not-trash", "Label|food", "pasta"])
        b = self.photo("b.jpg", 51, ["Owner|trash"])
        c = self.photo("c.jpg", 52, ["Owner|trash", "Owner|keep"])  # a keep wins
        r = subprocess.run([PY, "-I", "-c", GALLERY_PROBE, HERE, a], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        j = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(j["before"], {a: False, b: True, c: False})  # a: legacy keep still read
        self.assertEqual(j["after"], {a: False, b: True, c: False})
        self.assertEqual(self.read(a)[0], ["Label|food", "Owner|keep", "Owner|not-trash", "pasta"])
        self.assertEqual(self.read(b)[0], ["Owner|trash"])


if __name__ == "__main__":
    unittest.main()
