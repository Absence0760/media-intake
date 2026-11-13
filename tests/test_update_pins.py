#!/usr/bin/env python3
"""scripts/update-pins.py with fake PyPI / Hugging Face / Ollama answers and a fake uv: no network,
no downloads, nothing written outside a throwaway dir.

  python3 -I -m unittest discover -s tests -p test_update_pins.py
"""
import datetime, hashlib, importlib.util, io, json, os, shutil, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("update_pins", os.path.join(ROOT, "scripts", "update-pins.py"))
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

UTC = datetime.timezone.utc
CUTOFF = datetime.datetime(2026, 11, 6, tzinfo=UTC)
A, B, C = "a" * 64, "b" * 64, "c" * 64


def fake_get(answers):
    def get(url, accept=None):
        if url not in answers:
            raise AssertionError(f"unexpected fetch {url}")
        v = answers[url]
        return v if isinstance(v, bytes) else json.dumps(v).encode()
    return get


def wheel(name, sha, when, yanked=False, kind="bdist_wheel"):
    return {"filename": name, "digests": {"sha256": sha}, "upload_time_iso_8601": when,
            "packagetype": kind, "yanked": yanked}


class Wheels(unittest.TestCase):
    def test_wheel_fits(self):
        fits = ["onnxruntime-1.31.1-cp314-cp314-manylinux_2_28_x86_64.whl",
                "opencv_python_headless-4.14.0.94-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                "flatbuffers-25.12.19-py2.py3-none-any.whl", "protobuf-7.36.2-py3-none-any.whl",
                "tokenizers-0.23.3-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                "scipy-1.18.1-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"]
        not_fits = ["onnxruntime-1.31.1-cp314-cp314t-manylinux_2_28_x86_64.whl",   # free-threaded
                    "scipy-1.18.1-cp313-cp313-manylinux_2_28_x86_64.whl",          # other CPython
                    "scipy-1.18.1-cp314-cp314-musllinux_1_2_x86_64.whl",            # musl
                    "scipy-1.18.1-cp314-cp314-manylinux_2_28_aarch64.whl",          # arm
                    "scipy-1.18.1-cp314-cp314-win_amd64.whl",
                    "foo-1.0-cp315-abi3-manylinux_2_28_x86_64.whl",                 # newer stable ABI
                    "foo-1.0.tar.gz"]
        for f in fits:
            self.assertTrue(up.wheel_fits(f, "3.14"), f)
        for f in not_fits:
            self.assertFalse(up.wheel_fits(f, "3.14"), f)

    def test_wheel_files_filters_by_cutoff_yank_and_platform(self):
        get = fake_get({"https://pypi.org/pypi/foo/1.0/json": {"urls": [
            wheel("foo-1.0-py3-none-any.whl", A, "2026-10-01T00:00:00Z"),
            wheel("foo-1.0-cp314-cp314-manylinux_2_28_x86_64.whl", B, "2026-11-07T00:00:00Z"),  # after cutoff
            wheel("foo-1.0-cp314-cp314-manylinux_2_17_x86_64.whl", C, "2026-10-01T00:00:00Z", yanked=True),
            wheel("foo-1.0.tar.gz", C, "2026-10-01T00:00:00Z", kind="sdist")]}})
        files = up.wheel_files("foo", "1.0", "3.14", CUTOFF, get)
        self.assertEqual([(f, s) for f, s, _ in files], [("foo-1.0-py3-none-any.whl", A)])

    def test_no_fitting_wheel_is_an_error(self):
        get = fake_get({"https://pypi.org/pypi/foo/1.0/json": {"urls": [
            wheel("foo-1.0-cp314-cp314-win_amd64.whl", A, "2026-10-01T00:00:00Z")]}})
        with self.assertRaises(up.PinError):
            up.wheel_files("foo", "1.0", "3.14", CUTOFF, get)

    def test_resolve_passes_cutoff_system_and_holds_to_uv(self):
        seen = {}

        def run_uv(args):
            seen["args"] = args
            with open(args[args.index("--constraint") + 1]) as f:
                seen["constraints"] = f.read()
            return "bar==2.0\nfoo==1.0\n    # via bar\n"
        with mock.patch.object(up, "run_uv", run_uv):
            pins = up.resolve(["foo"], "3.14", CUTOFF, {"numpy": "2.3.5"}, no_deps=True)
        self.assertEqual(pins, [("bar", "2.0"), ("foo", "1.0")])
        a = seen["args"]
        self.assertEqual(a[a.index("--exclude-newer") + 1], "2026-11-06T00:00:00Z")
        self.assertEqual(a[a.index("--no-emit-package") + 1], "numpy")
        self.assertIn("--no-deps", a)
        self.assertIn("--no-config", a)
        self.assertEqual(a[a.index("--default-index") + 1], "https://pypi.org/simple")
        self.assertIn("numpy==2.3.5\n", seen["constraints"])
        for h in up.HOLDS:
            self.assertIn(h + "\n", seen["constraints"])

    def test_render_requirements(self):
        get = fake_get({"https://pypi.org/pypi/foo/1.0/json": {"urls": [
            wheel("foo-1.0-cp314-cp314-manylinux_2_28_x86_64.whl", A, "2026-10-01T00:00:00Z"),
            wheel("foo-1.0-cp310-abi3-manylinux_2_28_x86_64.whl", B, "2026-10-02T00:00:00Z")]}})
        text = up.render_requirements({"about": "About.", "notes": {"foo": "why"}}, [("foo", "1.0")],
                                      "3.14", CUTOFF, {"numpy": "2.3.5"}, get)
        self.assertTrue(text.startswith("# About.\n# Generated by scripts/update-pins.py"), text)
        self.assertIn("# against the system's numpy 2.3.5", text)
        self.assertTrue(text.endswith(f"# uploaded 2026-10-02; why\nfoo==1.0 \\\n"
                                      f"    --hash=sha256:{B} \\\n    --hash=sha256:{A}\n"), text)
        self.assertEqual(up.pins_of(text), ["foo==1.0 \\", f"    --hash=sha256:{B} \\", f"    --hash=sha256:{A}"])


REPO = "Org/model"
OLD, NEW, NEWER = "1" * 40, "2" * 40, "3" * 40
SIGLIP = {"repo": REPO, "revision": OLD, "revision_date": "2025-01-01",
          "files": {"onnx/model.onnx": A, "tokenizer.json": hashlib.sha256(b"tok").hexdigest()}}
COMMITS = f"https://huggingface.co/api/models/{REPO}/commits/main"


class Siglip(unittest.TestCase):
    def commits(self, *ids):
        dates = {NEWER: "2026-11-10T00:00:00.000Z", NEW: "2026-10-01T00:00:00.000Z", OLD: "2025-01-01T00:00:00.000Z"}
        return [{"id": i, "date": dates[i]} for i in ids]

    def tree(self, onnx_sha):
        return {f"https://huggingface.co/api/models/{REPO}/tree/{NEW}?recursive=true": [
            {"path": "onnx/model.onnx", "lfs": {"oid": onnx_sha}}, {"path": "tokenizer.json"}],
            f"https://huggingface.co/{REPO}/resolve/{NEW}/tokenizer.json": b"tok"}

    def test_current_revision(self):
        log = []
        out = up.update_siglip(SIGLIP, CUTOFF, fake_get({COMMITS: self.commits(OLD)}), log.append)
        self.assertIs(out, SIGLIP)
        self.assertIn("current", log[0])

    def test_newer_than_cutoff_is_ignored(self):
        out = up.update_siglip(SIGLIP, CUTOFF, fake_get({COMMITS: self.commits(NEWER, OLD)}), [].append)
        self.assertIs(out, SIGLIP)

    def test_new_commit_with_same_files_keeps_revision(self):
        log = []
        get = fake_get({COMMITS: self.commits(NEWER, NEW, OLD), **self.tree(A)})
        self.assertIs(up.update_siglip(SIGLIP, CUTOFF, get, log.append), SIGLIP)
        self.assertIn("changes none of the pinned files", log[0])

    def test_new_commit_with_new_files_moves_revision(self):
        log = []
        get = fake_get({COMMITS: self.commits(NEWER, NEW, OLD), **self.tree(B)})
        out = up.update_siglip(SIGLIP, CUTOFF, get, log.append)
        self.assertEqual(out, {**SIGLIP, "revision": NEW, "revision_date": "2026-10-01",
                               "files": {"onnx/model.onnx": B, "tokenizer.json": SIGLIP["files"]["tokenizer.json"]}})
        self.assertIn("setup.sh --build-labels", log[0])

    def test_file_gone_is_an_error(self):
        get = fake_get({COMMITS: self.commits(NEW, OLD),
                        f"https://huggingface.co/api/models/{REPO}/tree/{NEW}?recursive=true": []})
        with self.assertRaises(up.PinError):
            up.update_siglip(SIGLIP, CUTOFF, get, [].append)


MANIFEST_URL = "https://registry.ollama.ai/v2/library/vlm/manifests/2b"
OLD_MANIFEST, NEW_MANIFEST = b'{"old": 1}', b'{"new": 2}'
OLD_D, NEW_D = hashlib.sha256(OLD_MANIFEST).hexdigest(), hashlib.sha256(NEW_MANIFEST).hexdigest()
OLLAMA = {"model": "vlm:2b", "digest": OLD_D, "candidate": None}


class Ollama(unittest.TestCase):
    def run_update(self, pin, manifest, today="2026-11-13"):
        log = []
        return up.update_ollama(pin, CUTOFF, today, fake_get({MANIFEST_URL: manifest}), log.append), log

    def test_current(self):
        out, _ = self.run_update(OLLAMA, OLD_MANIFEST)
        self.assertIs(out, OLLAMA)

    def test_new_digest_becomes_a_candidate_not_the_pin(self):
        out, _ = self.run_update(OLLAMA, NEW_MANIFEST)
        self.assertEqual(out, {**OLLAMA, "candidate": {"digest": NEW_D, "first_seen": "2026-11-13"}})

    def test_candidate_seen_too_recently_waits(self):
        pin = {**OLLAMA, "candidate": {"digest": NEW_D, "first_seen": "2026-11-06"}}  # not before the cutoff
        out, log = self.run_update(pin, NEW_MANIFEST)
        self.assertIs(out, pin)
        self.assertIn("pinned after 7 days", log[0])

    def test_candidate_old_enough_is_promoted(self):
        pin = {**OLLAMA, "candidate": {"digest": NEW_D, "first_seen": "2026-11-01"}}
        out, log = self.run_update(pin, NEW_MANIFEST)
        self.assertEqual(out, {**OLLAMA, "digest": NEW_D})
        self.assertIn("ollama pull vlm:2b", log[0])

    def test_candidate_replaced_by_a_different_digest_restarts_the_clock(self):
        pin = {**OLLAMA, "candidate": {"digest": "e" * 64, "first_seen": "2026-10-01"}}
        out, _ = self.run_update(pin, NEW_MANIFEST)
        self.assertEqual(out["candidate"], {"digest": NEW_D, "first_seen": "2026-11-13"})
        self.assertEqual(out["digest"], OLD_D)

    def test_back_to_the_pin_drops_the_candidate(self):
        out, _ = self.run_update({**OLLAMA, "candidate": {"digest": NEW_D, "first_seen": "2026-11-01"}}, OLD_MANIFEST)
        self.assertEqual(out, OLLAMA)


class Main(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mi-pins-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_cutoff_inside_seven_days_refused(self):
        late = (datetime.datetime.now(UTC) - datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            up.main(["--cutoff", late])
        self.assertIn("less than 7 days ago", str(e.exception.code))

    def test_check_writes_nothing_and_comment_only_changes_are_ignored(self):
        req = os.path.join(self.d, "requirements.txt")
        pins = os.path.join(self.d, "pins.json")
        with open(req, "w") as f:
            f.write("# old header\nfoo==1.0 \\\n    --hash=sha256:" + A + "\n")
        with open(pins, "w") as f:
            f.write("{}\n")
        new = {req: "# new header, newer cutoff\nfoo==1.0 \\\n    --hash=sha256:" + A + "\n", pins: '{"x": 1}\n'}
        with mock.patch.object(up, "PINS", pins), mock.patch.object(up, "plan", return_value=new), \
                redirect_stdout(io.StringIO()) as out:
            code = up.main(["--check", "--cutoff", "2026-01-01T00:00:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("1 file(s) would change", out.getvalue())
        self.assertNotIn("requirements.txt", out.getvalue().split("update-pins:")[0])
        with open(pins) as f:
            self.assertEqual(f.read(), "{}\n")

    def test_old_uv_refused(self):
        with mock.patch.object(up, "uv_version", return_value=(0, 9, 16)), self.assertRaises(up.PinError):
            up.plan("requirements", CUTOFF, "2026-11-13", "3.14")

    def test_non_https_fetch_refused(self):
        with self.assertRaises(up.PinError):
            up.fetch("http://pypi.org/simple")

    def test_pins_json_is_what_the_script_writes(self):
        """The committed pins.json is in the script's own format (json.dumps, indent 2)."""
        with open(up.PINS) as f:
            text = f.read()
        self.assertEqual(json.dumps(json.loads(text), indent=2) + "\n", text)


if __name__ == "__main__":
    unittest.main()
