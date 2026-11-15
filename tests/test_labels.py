#!/usr/bin/env python3
"""label.py's labels.npz check: the text embeddings must match labels.yaml's prompts and
record the SigLIP revision pinned in pins.json. Synthetic npz files in a throwaway dir.
label.py's main and `intake.py tag` with a stale labels.npz refuse before the model loads
(onnxruntime is stubbed; tag runs on a copy of the package in a throwaway HOME).

  python3 -I -m unittest discover -s tests -p test_labels.py
"""
import os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

support.check_hermetic()

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



# For the main and tag tests below (kept apart from the import block above).
import io, json, subprocess, types  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
import unittest.mock as mock  # noqa: E402


def stale_npz(path, how):
    """labels.npz gone stale: built with another SigLIP revision, recording none, or for other prompts."""
    with np.load(label.LABELS_NPZ) as z:
        emb, prompts = z["emb"], list(z["prompts"])
    extra = {label.NPZ_REV_KEY: np.array(label.REV)}
    if how == "revision":
        extra = {label.NPZ_REV_KEY: np.array(label.REV[::-1])}
    elif how == "no-revision":
        extra = {}
    elif how == "prompts":
        prompts = prompts + ["a photo of something new"]
        emb = np.vstack([emb, emb[:1]])
    np.savez(path, emb=emb, prompts=np.array(prompts), **extra)
    return path


STALE = {"revision": "built with SigLIP revision", "no-revision": "records no SigLIP revision",
         "prompts": "doesn't match labels.yaml"}


@unittest.skipIf(np is None or yaml is None, "numpy or pyyaml not installed")
class StaleLabelsMain(unittest.TestCase):
    """label.py's main with a stale labels.npz: it refuses before the model loads and labels nothing. The
    current design refuses and names the rebuild (setup.sh --build-labels); it never rebuilds on its own."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="labels-main-test-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state)
        self.photo = os.path.join(self.d, "x.jpg")
        with open(self.photo, "wb") as f:
            f.write(b"not decoded: the refusal comes first")

    def test_main_refuses_a_stale_npz(self):
        loaded = []
        ort = types.ModuleType("onnxruntime")
        ort.SessionOptions = ort.InferenceSession = lambda *a, **k: loaded.append(a)
        for how, want in STALE.items():
            npz = stale_npz(os.path.join(self.d, f"{how}.npz"), how)
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"onnxruntime": ort}), \
                    mock.patch.object(label.text_embeddings, "__defaults__", (npz,)), \
                    mock.patch.object(label.refused, "__defaults__", (self.state,)), \
                    mock.patch.object(sys, "argv", ["label.py"]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps([self.photo]))), \
                    redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                label.main()
            msg = str(cm.exception.code)
            self.assertTrue(msg.startswith("label.py: labels.npz"), (how, msg))
            self.assertIn(want, msg, how)
            self.assertIn(REBUILD, msg, how)
            self.assertEqual(out.getvalue(), "", "no labels written")
            self.assertEqual(loaded, [], "the model is never loaded")


# The venv's python for `intake.py tag`: the real label.py, with onnxruntime stubbed so a run that got past
# the labels.npz check would fail loudly instead of needing the model.
FAKE_VENV_PY = """import runpy, sys, types
ort = types.ModuleType("onnxruntime")
def reached(*a, **k):
    raise SystemExit("onnxruntime reached: the stale labels.npz was not refused")
ort.SessionOptions = ort.InferenceSession = reached
sys.modules["onnxruntime"] = ort
args = [a for a in sys.argv[1:] if a != "-I"]
sys.argv = args
runpy.run_path(args[0], run_name="__main__")
"""


@unittest.skipIf(np is None or yaml is None, "numpy or pyyaml not installed")
@unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
class StaleLabelsTag(unittest.TestCase):
    """`intake.py tag` with a stale labels.npz stops with label.py's message and writes no sidecar. Runs a copy
    of the package (so the stale file never replaces the committed one) in a throwaway HOME."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="labels-tag-test-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pkg = os.path.join(self.d, "media_intake")
        shutil.copytree(support.PKG, self.pkg, ignore=shutil.ignore_patterns("__pycache__"))
        home = os.path.join(self.d, "home")
        self.state = os.path.join(home, ".local/share/media-intake")
        lib = os.path.join(home, "Pictures/library")
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(home, "Videos/demo-footage")),
                        MEDIA_INTAKE_NO_LOCALSEARCH="1")
        venv_py = os.path.join(self.state, "venv/bin/python")
        os.makedirs(os.path.dirname(venv_py))
        with open(venv_py, "w") as f:
            f.write(f"#!{sys.executable} -I\n" + FAKE_VENV_PY)
        os.chmod(venv_py, 0o700)
        self.photo = os.path.join(lib, "2026/2026-11/x.jpg")
        os.makedirs(os.path.dirname(self.photo))
        from PIL import Image
        Image.new("RGB", (64, 48), (120, 60, 30)).save(self.photo)
        self.batch = "phone-2026-11-08"
        b = os.path.join(self.state, "batches", self.batch)
        os.makedirs(b)
        with open(os.path.join(b, "plan.json"), "w") as f:
            json.dump([{"rel": "DCIM/Camera/x.jpg", "action": "file", "dst": self.photo, "cat": "photo",
                        "date": "2026-11-08T07:15:00", "project": "Personal"}], f)

    def test_tag_stops_on_a_stale_npz(self):
        for how, want in STALE.items():
            stale_npz(os.path.join(self.pkg, "labels.npz"), how)
            r = subprocess.run([sys.executable, "-I", os.path.join(self.pkg, "intake.py"), "tag", self.batch],
                               env=self.env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, (how, r.stdout))
            self.assertIn("label.py failed", r.stderr, how)
            self.assertIn(want, r.stderr, how)
            self.assertIn(REBUILD, r.stderr, how)
            self.assertNotIn("onnxruntime reached", r.stderr, how)
            self.assertNotIn("tagged", r.stdout, how)
            self.assertFalse(os.path.exists(self.photo + ".xmp"), f"{how}: no sidecar written")


if __name__ == "__main__":
    unittest.main(verbosity=1)
