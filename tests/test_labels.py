#!/usr/bin/env python3
"""label.py's labels.npz check: the text embeddings must match labels.yaml's prompts and
record the SigLIP revision pinned in pins.json. Synthetic npz files in a throwaway dir.

  python3 -I -m unittest discover -s tests -p test_labels.py
"""
import os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

import label  # noqa: E402

try:
    import numpy as np
except ImportError:
    np = None
try:
    import yaml  # noqa: F401  (label_config)
except ImportError:
    yaml = None

PROMPTS = ["a photo of a dog", "a photo of a runner"]
REBUILD = "bash setup.sh --build-labels"


@unittest.skipIf(np is None, "numpy not installed")
class TextEmbeddings(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="labels-test-")
        self.emb = np.eye(len(PROMPTS), 4, dtype=np.float32)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def npz(self, prompts=PROMPTS, **extra):
        p = os.path.join(self.d, "labels.npz")
        np.savez(p, emb=self.emb, prompts=np.array(prompts), **extra)
        return p

    def refused(self, path, prompts=PROMPTS):
        with self.assertRaises(SystemExit) as cm:
            label.text_embeddings(prompts, np, path)
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith("label.py: labels.npz"), msg)
        self.assertIn(REBUILD, msg)
        return msg

    def test_matching_revision_loads(self):
        p = self.npz(**{label.NPZ_REV_KEY: np.array(label.REV)})
        got = label.text_embeddings(PROMPTS, np, p)
        self.assertEqual(got.dtype, np.float32)
        np.testing.assert_array_equal(got, self.emb)

    def test_wrong_revision_refused(self):
        other = label.REV[::-1]
        self.assertNotEqual(other, label.REV)
        msg = self.refused(self.npz(**{label.NPZ_REV_KEY: np.array(other)}))
        self.assertIn(f"built with SigLIP revision {other[:12]}", msg)
        self.assertIn(f"pins {label.REV[:12]}", msg)

    def test_missing_revision_refused(self):
        msg = self.refused(self.npz())
        self.assertIn("records no SigLIP revision", msg)

    def test_prompts_mismatch_refused(self):
        p = self.npz(**{label.NPZ_REV_KEY: np.array(label.REV)})
        msg = self.refused(p, prompts=PROMPTS + ["a photo of a cat"])
        self.assertIn("doesn't match labels.yaml", msg)

    @unittest.skipIf(yaml is None, "pyyaml not installed")
    def test_committed_file_matches_yaml_and_pin(self):
        # A SigLIP pin move or a labels.yaml edit without `setup.sh --build-labels` fails here.
        _, prompts, _, _, _ = label.label_config()
        txt = label.text_embeddings(prompts, np)
        self.assertEqual(txt.shape[0], len(prompts))


if __name__ == "__main__":
    unittest.main(verbosity=1)
