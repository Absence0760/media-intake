#!/usr/bin/env python3
"""calibrate.py tests: the precision / recall / F1 maths, the threshold sweep and suggestion, the minimum-sample
refusal, the ground-truth rules (Calibrate| tags, Keep, Owner|trash, trashed.json), and a whole run on a
synthetic library: placeholder files that are never opened, hand-made index vectors whose label scores are
known, a fake labels.npz built against labels.yaml's prompts, and plain-text XMP sidecars.

  python3 -I -m unittest discover -s tests -p test_calibrate.py
"""
import contextlib, hashlib, io, json, math, os, shutil, sys, tempfile, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

support.check_hermetic()

import calibrate  # noqa: E402
import label  # noqa: E402

try:
    import numpy as np
except ImportError:
    np = None
try:
    import yaml  # noqa: F401  (label.label_config)
except ImportError:
    yaml = None

NAMES = ["dog", "child-in-shot", "maybe-trash", "road race"]


class Maths(unittest.TestCase):
    POS = [0.9, 0.8, 0.6, 0.3]
    NEG = [0.7, 0.2, 0.1, 0.05]

    def test_metrics_at_a_threshold(self):
        m = calibrate.metrics(self.POS, self.NEG, 0.5)  # flags 0.9 0.8 0.6 (pos) and 0.7 (neg)
        self.assertEqual((m["tp"], m["fp"], m["fn"], m["tn"]), (3, 1, 1, 3))
        self.assertAlmostEqual(m["precision"], 3 / 4)
        self.assertAlmostEqual(m["recall"], 3 / 4)
        self.assertAlmostEqual(m["f1"], 2 * 3 / (2 * 3 + 1 + 1))

    def test_a_score_equal_to_the_threshold_is_flagged(self):  # label.py: score >= threshold
        m = calibrate.metrics([0.5], [0.5], 0.5)
        self.assertEqual((m["tp"], m["fp"]), (1, 1))

    def test_nothing_flagged_has_no_precision_and_f1_zero(self):
        m = calibrate.metrics(self.POS, self.NEG, 0.95)
        self.assertIsNone(m["precision"])
        self.assertEqual((m["recall"], m["f1"]), (0.0, 0.0))

    def test_sweep_cuts_halfway_between_scores(self):
        cuts = calibrate.sweep(self.POS, self.NEG)
        scores = sorted(set(self.POS + self.NEG), reverse=True)
        self.assertEqual(len(cuts), len(scores))
        for i, (t, m) in enumerate(cuts):
            with self.subTest(cut=scores[i]):
                want = (scores[i] + scores[i + 1]) / 2 if i + 1 < len(scores) else scores[i]
                self.assertAlmostEqual(t, want)
                self.assertEqual(m, calibrate.metrics(self.POS, self.NEG, scores[i]))
                self.assertEqual(m, calibrate.metrics(self.POS, self.NEG, t))  # same items each side
        # by hand, from the top: flag 0.9 (tp 1), +0.8 (tp 2), +0.7 (fp 1), +0.6 (tp 3), +0.3 (tp 4) ...
        self.assertEqual([m["tp"] for _, m in cuts], [1, 2, 2, 3, 4, 4, 4, 4])
        self.assertEqual([m["fp"] for _, m in cuts], [0, 0, 1, 1, 1, 2, 3, 4])

    def test_best_f1(self):
        t, m = calibrate.suggest(self.POS, self.NEG)
        # F1 per cut: 2/5, 4/6, 4/7, 6/8, 8/9, 8/10, 8/11, 8/12: the best flags down to 0.3 (cut halfway to 0.2)
        self.assertAlmostEqual(t, 0.25)
        self.assertAlmostEqual(m["f1"], 8 / 9)
        self.assertAlmostEqual(m["precision"], 4 / 5)
        self.assertEqual(m["recall"], 1.0)

    def test_target_precision_takes_the_most_recall(self):
        t, m = calibrate.suggest(self.POS, self.NEG, precision=1.0)
        self.assertAlmostEqual(t, 0.75)  # 0.9 and 0.8 only; adding 0.7 (a negative) drops precision
        self.assertEqual((m["precision"], m["recall"]), (1.0, 0.5))
        t, m = calibrate.suggest(self.POS, self.NEG, precision=0.75)  # precision 1, 1, 2/3, 3/4, 4/5, ...
        self.assertAlmostEqual(t, 0.25)
        self.assertAlmostEqual(m["precision"], 0.8)
        self.assertEqual(m["recall"], 1.0)
        t, m = calibrate.suggest(self.POS, self.NEG, precision=0.85)
        self.assertAlmostEqual(t, 0.75)

    def test_ties_go_to_the_higher_threshold(self):
        pos, neg = [0.9, 0.6], [0.8, 0.7]
        f1 = [m["f1"] for _, m in calibrate.sweep(pos, neg)]
        self.assertEqual([round(x, 4) for x in f1], [round(2 / 3, 4), 0.5, 0.4, round(2 / 3, 4)])
        t, _ = calibrate.suggest(pos, neg)
        self.assertAlmostEqual(t, 0.85)  # not 0.6, the other cut with F1 2/3
        pos, neg = [0.9, 0.1], [0.7, 0.6, 0.5]
        t, m = calibrate.suggest(pos, neg, precision=0.5)  # recall 1/2 at 0.8 (P 1) and at 0.65 (P 1/2)
        self.assertAlmostEqual(t, 0.8)
        self.assertEqual((m["precision"], m["recall"]), (1.0, 0.5))

    def test_unreachable_precision(self):
        self.assertIsNone(calibrate.suggest([0.1], [0.9, 0.8], precision=0.9))
        r = calibrate.report_label([0.1] * 3, [0.9] * 3, 0.5, min_each=3, precision=0.9)
        self.assertIsNone(r["suggested"])
        self.assertEqual(r["why_not"], "no threshold reaches precision 0.9")

    def test_minimum_sample_refusal(self):
        for pos, neg in (([0.9] * 19, [0.1] * 20), ([0.9] * 20, [0.1] * 19), ([], [])):
            with self.subTest(pos=len(pos), neg=len(neg)):
                r = calibrate.report_label(pos, neg, 0.5)
                self.assertIsNone(r["suggested"])
                self.assertEqual(r["why_not"], f"too few marked items: {len(pos)} positive, {len(neg)} negative; "
                                               "need at least 20 of each")
                self.assertEqual((r["positive"], r["negative"]), (len(pos), len(neg)))
        r = calibrate.report_label([0.9] * 20, [0.1] * 20, 0.5)
        self.assertIsNone(r["why_not"])
        self.assertAlmostEqual(r["suggested"]["threshold"], 0.5)
        self.assertEqual(r["suggested"]["f1"], 1.0)
        r = calibrate.report_label([0.9] * 2, [0.1] * 2, 0.5, min_each=2)
        self.assertIsNotNone(r["suggested"])


class GroundTruth(unittest.TestCase):
    def truth(self, *copies, trashed=False):
        return calibrate.truth([(list(t), p) for t, p in copies], NAMES, trashed)

    def test_calibrate_tags(self):
        t, unknown = self.truth((["Calibrate|dog|yes", "calibrate|Road Race|No", "Calibrate|child-in-shot| yes "], ""))
        self.assertEqual(t, {"dog": True, "road race": False, "child-in-shot": True})
        self.assertEqual(unknown, 0)

    def test_unusable_tags_are_counted_not_used(self):
        t, unknown = self.truth((["Calibrate|unicorn|yes", "Calibrate|dog", "Calibrate|dog|maybe", "Label|dog",
                                  "dog", "Calibrate|dog|yes|no"], ""))
        self.assertEqual(t, {})
        self.assertEqual(unknown, 4)

    def test_copies_pool_and_conflict(self):
        t, _ = self.truth((["Calibrate|dog|yes"], ""), (["Calibrate|dog|yes"], ""))
        self.assertEqual(t, {"dog": True})
        t, _ = self.truth((["Calibrate|dog|yes"], ""), (["Calibrate|dog|no"], ""))
        self.assertEqual(t, {"dog": "conflict"})

    def test_maybe_trash_from_owner_decisions(self):
        cases = [
            ((["Owner|keep"], ""), False, False),
            ((["Owner|not-trash"], ""), False, False),
            ((["Review|not-trash"], ""), False, False),  # the legacy spelling
            (([], "3"), False, False),  # digiKam Accepted
            ((["Owner|trash"], ""), False, True),
            (([], ""), True, True),  # trashed.json
            ((["Owner|keep", "Owner|trash"], ""), True, False),  # a Keep wins (owner.trashed)
            ((["Owner|keep", "Calibrate|maybe-trash|yes"], ""), False, True),  # your mark wins
            ((["Owner|trash", "Calibrate|maybe-trash|no"], ""), True, False),
        ]
        for copy, trashed, want in cases:
            with self.subTest(copy=copy, trashed=trashed):
                self.assertEqual(self.truth(copy, trashed=trashed)[0], {"maybe-trash": want})
        self.assertEqual(self.truth(([], ""), (["Owner|keep"], ""), trashed=True)[0], {"maybe-trash": False})
        self.assertEqual(self.truth(([], "1"))[0], {})  # a pick other than Accepted is no decision on trash
        self.assertEqual(self.truth()[0], {})


def xmp(path, hier, pick=None):
    li = "".join(f"<rdf:li>{h}</rdf:li>" for h in hier)
    leaves = "".join(f"<rdf:li>{h.split('|')[-1]}</rdf:li>" for h in hier)
    pk = f" xmlns:digiKam='http://www.digikam.org/ns/1.0/' digiKam:PickLabel='{pick}'" if pick else ""
    with open(path + ".xmp", "w", encoding="utf-8") as f:
        f.write("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
                "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
                "<rdf:Description rdf:about='' xmlns:lr='http://ns.adobe.com/lightroom/1.0/' "
                f"xmlns:dc='http://purl.org/dc/elements/1.1/'{pk}>\n"
                f"<lr:hierarchicalSubject><rdf:Bag>{li}</rdf:Bag></lr:hierarchicalSubject>\n"
                f"<dc:subject><rdf:Bag>{leaves}</rdf:Bag></dc:subject>\n"
                "</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")


LABELS_YAML = os.path.join(os.path.dirname(os.path.abspath(label.__file__)), "labels.yaml")


def fingerprint(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree(*roots):
    """{path: sha256} of every file under roots: what a run must leave as it was."""
    out = {}
    for r in roots:
        for d, _, names in os.walk(r):
            for n in names:
                with open(os.path.join(d, n), "rb") as f:
                    out[os.path.join(d, n)] = hashlib.sha256(f.read()).hexdigest()
    return out


@unittest.skipIf(np is None or yaml is None, "numpy or yaml not installed")
class Library(unittest.TestCase):
    """A whole run: index vectors made so each label's score is what the test says, owner marks in sidecars."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="calibrate-test-")
        self.state = os.path.join(self.d, "state")
        self.lib = os.path.join(self.d, "library")
        os.makedirs(self.state, mode=0o700)
        os.makedirs(self.lib)
        self.names, prompts, owner_of, self.thr, _ = label.label_config()
        self.dim = len(self.names) + 1
        txt = np.zeros((len(prompts), self.dim), np.float32)
        for i, o in enumerate(owner_of):
            txt[i, self.names.index(o)] = 1.0  # every prompt of label k points along axis k
        self.npz = os.path.join(self.d, "labels.npz")
        np.savez(self.npz, prompts=np.array(prompts), emb=txt, **{label.NPZ_REV_KEY: np.array(label.REV)})
        self.rows, self.files, self.n = [], {}, 0
        self.trashed = {}

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def vec(self, want):
        """A unit vector whose label.py score for each label in want is that score (others ~0)."""
        v = np.zeros(self.dim)
        for name, s in want.items():
            v[self.names.index(name)] = (math.log(s / (1 - s)) - label.LOGIT_BIAS) / math.exp(label.LOGIT_SCALE)
        v[-1] = math.sqrt(1 - float((v ** 2).sum()))
        return v

    def item(self, scores, tags=(), pick=None, ext=".jpg", copies=1, on_disk=True, trashed=False, copy_tags=None):
        """One content (a made-up sha256) with copies library files (copy_tags: each copy's own tags); returns
        its sha."""
        self.n += 1
        sha = f"{self.n:064x}"
        self.rows.append((sha, self.vec(scores)))
        for c in range(copies):
            p = os.path.join(self.lib, f"item-{self.n:03d}-{c}{ext}")
            self.files[p] = [1, 1, sha]
            if on_disk:
                with open(p, "wb") as f:
                    f.write(b"placeholder, never opened")
                mine = copy_tags[c] if copy_tags else tags
                if mine or pick:
                    xmp(p, list(mine), pick)
        if trashed:
            self.trashed[sha] = 1
        return sha

    def save(self):
        with open(os.path.join(self.state, "embeddings.npz"), "wb") as f:
            np.savez(f, sha=np.array([s for s, _ in self.rows], dtype="U64"),
                     emb=np.stack([v for _, v in self.rows]).astype(np.float16), files=np.array(json.dumps(self.files)))
        with open(os.path.join(self.state, "trashed.json"), "w") as f:
            json.dump(self.trashed, f)

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MEDIA_INTAKE_STATE": self.state}), \
                mock.patch.object(label, "LABELS_NPZ", self.npz), contextlib.redirect_stdout(out):
            self.assertEqual(calibrate.main(list(argv)), 0)
        return out.getvalue()

    def build(self):
        # dog (threshold 0.05): 20 yes at 0.60..0.79, 20 no at 0.01..0.029 plus 2 no at 0.2 (flagged at 0.05)
        for i in range(20):
            self.item({"dog": 0.60 + i / 100}, ["Calibrate|dog|yes"])
            self.item({"dog": 0.01 + i / 1000}, ["Calibrate|dog|no"])
        for _ in range(2):
            self.item({"dog": 0.2}, ["Calibrate|dog|no"])
        # maybe-trash (threshold 0.1): yes from Owner|trash, trashed.json (file gone) and a mark; no from Keep,
        # Owner|not-trash and digiKam Accepted; a Keep with a "yes" mark counts as yes
        self.item({"maybe-trash": 0.5}, ["Owner|trash"])
        self.item({"maybe-trash": 0.4}, on_disk=False, trashed=True)
        self.item({"maybe-trash": 0.05}, ["Calibrate|maybe-trash|yes", "Owner|keep"])
        self.item({"maybe-trash": 0.02}, ["Owner|keep", "Owner|not-trash"])
        self.item({"maybe-trash": 0.3}, ["Owner|not-trash"])
        self.item({"maybe-trash": 0.01}, pick="3")
        # left out: a video, a flagged item, a conflict between two copies; ignored: unusable tags
        self.item({"dog": 0.9}, ["Calibrate|dog|yes"], ext=".mp4")
        flagged = self.item({"dog": 0.9}, ["Calibrate|dog|yes"])
        qdir = os.path.join(self.state, "quarantine", "b1")
        os.makedirs(qdir)
        with open(os.path.join(qdir, "quarantine.json"), "w") as f:
            json.dump({"x": {"sha": flagged, "size": 1}}, f)
        self.item({"dog": 0.9}, copies=2, copy_tags=[["Calibrate|dog|yes"], ["Calibrate|dog|no"]])
        self.item({}, ["Calibrate|unicorn|yes", "Calibrate|dog"])
        self.trashed[f"{999:064x}"] = 1  # trashed and re-indexed since: no vector
        self.save()

    def test_no_network(self):
        """calibrate reads stored numbers and tags only: any socket connect fails the test."""
        self.build()
        with mock.patch("socket.socket.connect", side_effect=AssertionError("calibrate opened a connection")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("calibrate opened a connection")):
            json.loads(self.main("--json"))

    def test_report(self):
        self.build()
        before = tree(self.state, self.lib), fingerprint(LABELS_YAML), fingerprint(support.CONFIG)
        res = json.loads(self.main("--json"))
        self.assertEqual((tree(self.state, self.lib), fingerprint(LABELS_YAML), fingerprint(support.CONFIG)),
                         before)  # reports only: no sidecar, state file, labels.yaml or config written
        self.assertEqual(res["counts"], {"scored": 50, "videos_left_out": 1, "conflicts": 1, "unknown_tags": 2,
                                         "flagged_left_out": 1, "trashed_without_scores": 1, "marked": 49})
        dog = res["labels"]["dog"]
        self.assertEqual((dog["positive"], dog["negative"], dog["threshold"]), (20, 22, 0.05))
        cur = dog["current"]
        self.assertEqual((cur["tp"], cur["fp"], cur["fn"], cur["tn"]), (20, 2, 0, 20))
        self.assertAlmostEqual(cur["precision"], round(20 / 22, 4))
        self.assertEqual(cur["recall"], 1.0)
        self.assertAlmostEqual(cur["f1"], round(40 / 42, 4))
        s = dog["suggested"]
        self.assertEqual((s["precision"], s["recall"], s["f1"]), (1.0, 1.0, 1.0))
        self.assertAlmostEqual(s["threshold"], 0.4, delta=0.01)  # halfway from 0.60 down to 0.2
        mt = res["labels"]["maybe-trash"]
        self.assertEqual((mt["positive"], mt["negative"], mt["threshold"]), (3, 3, 0.1))
        cur = mt["current"]
        self.assertEqual((cur["tp"], cur["fp"], cur["fn"], cur["tn"]), (2, 1, 1, 2))
        self.assertIsNone(mt["suggested"])
        self.assertEqual(mt["why_not"], "too few marked items: 3 positive, 3 negative; need at least 20 of each")
        self.assertEqual(res["labels"]["child-in-shot"]["positive"] + res["labels"]["child-in-shot"]["negative"], 0)

    def test_options(self):
        self.build()
        res = json.loads(self.main("--json", "--min", "3"))
        mt = res["labels"]["maybe-trash"]
        # yes 0.5 0.4 0.05, no 0.3 0.02 0.01; F1 per cut: 1/2, 4/5, 4/6, 6/7, 6/8, 6/9
        self.assertIsNone(mt["why_not"])
        self.assertAlmostEqual(mt["suggested"]["threshold"], 0.035, delta=0.002)  # halfway from 0.05 to 0.02
        self.assertEqual(mt["suggested"]["f1"], round(6 / 7, 4))
        res = json.loads(self.main("--json", "--min", "3", "--precision", "0.8"))
        self.assertEqual(res["target_precision"], 0.8)
        mt = res["labels"]["maybe-trash"]["suggested"]  # precision 1, 1, 2/3, 3/4, ...: the most recall is 2/3
        self.assertEqual((mt["precision"], mt["recall"]), (1.0, round(2 / 3, 4)))
        self.assertAlmostEqual(mt["threshold"], 0.35, delta=0.002)
        res = json.loads(self.main("--json", "--with-videos"))
        self.assertEqual(res["counts"]["videos_left_out"], 0)
        self.assertEqual(res["labels"]["dog"]["positive"], 21)

    def test_text_is_counts_only(self):
        self.build()
        out = self.main()
        self.assertNotIn("item-", out)
        self.assertNotIn(self.d, out)
        self.assertIn("calibrate: 50 items with stored scores (photos), 49 with a mark", out)
        for line in ("videos left out: 1 (--with-videos counts them)", "trashed items no longer in the index: 1",
                     "marks in conflict (yes and no), not counted: 1",
                     "Calibrate| tags naming no label or no yes/no, ignored: 2", "flagged items, never counted: 1",
                     "suggested threshold: best F1; only with at least 20 positive and 20 negative marks"):
            self.assertIn(line, out)
        dog = next(ln for ln in out.splitlines() if ln.startswith("dog "))
        self.assertRegex(dog, r"^dog +20 +22 +0\.0500 0\.909  1\.000 0\.952  \|  0\.[34]\d{3} 1\.000  1\.000 1\.000$")
        mt = next(ln for ln in out.splitlines() if ln.startswith("maybe-trash "))
        self.assertTrue(mt.endswith("too few marked items: 3 positive, 3 negative; need at least 20 of each"), mt)
        self.assertIn(f"labels with no marks yet (Calibrate|<label>|yes or |no): {len(self.names) - 2}", out)

    def test_no_index(self):
        out = json.loads(self.main("--json"))
        self.assertEqual(out["counts"]["scored"], 0)
        self.assertTrue(all(r["positive"] == r["negative"] == 0 for r in out["labels"].values()))

    def test_bad_options(self):
        for argv in (["--precision", "0"], ["--precision", "1.5"], ["--min", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as e, \
                    contextlib.redirect_stderr(io.StringIO()):
                calibrate.main(argv)
            self.assertEqual(e.exception.code, 2)


class Command(unittest.TestCase):
    """`media-intake calibrate` is cli.py's (system Python), with its arguments passed on as given."""

    def test_args_passed_on(self):
        import cli
        with mock.patch.object(calibrate, "main", return_value=0) as m:
            self.assertEqual(cli.main(["calibrate", "--json", "--precision", "0.9"]), 0)
        self.assertEqual(m.call_args[0][0], ["--json", "--precision", "0.9"])

    def test_missing_package_says_run_setup(self):
        import cli
        err = io.StringIO()
        with mock.patch.object(cli.importlib, "import_module", side_effect=ModuleNotFoundError("x", name="numpy")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["calibrate"]), 1)
        self.assertIn("numpy", err.getvalue())
        self.assertIn("bash setup.sh", err.getvalue())
        self.assertIn("calibrate", cli.command_names())
        self.assertIn("media-intake calibrate", cli.__doc__)


if __name__ == "__main__":
    unittest.main()
