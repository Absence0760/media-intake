#!/usr/bin/env python3
"""cli.py tests: `media-intake doctor` against a fake probe (no real tools, models or Ollama),
plus guards: the model pins (and their 12-character prefixes) have one source, the version one home,
and nothing kills by command line.

  python3 -I -m unittest discover -s tests -p test_cli.py
"""
import glob, io, json, os, re, shutil, sys, tempfile, unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

import cli  # noqa: E402
import label  # noqa: E402
import semantic  # noqa: E402
import vlm_trash  # noqa: E402

with open(os.path.join(support.PKG, "pins.json")) as _f:
    PINS = json.load(_f)
FILES = PINS["siglip"]["files"]
VISION, TEXT, TOKENIZER = (label.MODEL, os.path.join(semantic.BUILD, "text_model.onnx"),
                           os.path.join(semantic.BUILD, "tokenizer.json"))
OTHER = "f" * 64


class FakeProbe:
    """A machine with everything installed and every pinned file correct; tests knock pieces out."""

    def __init__(self):
        self.tools = set(cli.SYSTEM_TOOLS) | {"ollama", cli.XDG_OPEN}
        self.modules = set(cli.SYSTEM_MODULES)
        self.langs = "List of available languages (2):\neng\nosd\n"
        self.plugins = "va:  vah265dec: VA-API H.265 Decoder\n"
        self.files = {VISION: FILES["onnx/vision_model_quantized.onnx"], TEXT: FILES["onnx/text_model.onnx"],
                      TOKENIZER: FILES["tokenizer.json"], cli.venv_python(): None, cli.MTP_BACKEND: None,
                      os.path.join(os.environ["MEDIA_INTAKE_STATE"], "geo", "cities1000.zip"): None}
        self.tags = {"models": [{"name": PINS["ollama"]["model"], "model": PINS["ollama"]["model"],
                                 "digest": PINS["ollama"]["digest"]}]}
        self.hashed = []

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.tools else None

    def output(self, cmd):
        return {"tesseract": self.langs, "gst-inspect-1.0": self.plugins, "ollama": "ollama version is 0.0.0"}[cmd[0]]

    def importable(self, module):
        return module in self.modules

    def exists(self, path):
        return path in self.files

    def sha256(self, path):
        self.hashed.append(path)
        return self.files[path]

    def ollama_tags(self):
        if isinstance(self.tags, Exception):
            raise self.tags
        return self.tags


class Doctor(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="mi-cli-")
        self.addCleanup(shutil.rmtree, d, True)
        env = mock.patch.dict(os.environ, {k: os.path.join(d, v) for k, v in (
            ("MEDIA_INTAKE_STATE", "state"), ("PHOTO_LIBRARY", "library"), ("PERSONAL_FOOTAGE", "videos"))})
        env.start()
        self.addCleanup(env.stop)
        self.p = FakeProbe()

    def run_doctor(self):
        lines = []
        code = cli.doctor(self.p, out=lines.append)
        return code, lines

    def line(self, lines, text):
        hits = [ln for ln in lines if text in ln]
        self.assertEqual(len(hits), 1, f"{text!r} in {lines}")
        return hits[0]

    def test_everything_present_passes_with_no_install_line(self):
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertFalse([ln for ln in lines if "dnf install" in ln], lines)
        self.assertTrue(self.line(lines, "SigLIP vision model").startswith("ok "))
        self.assertTrue(self.line(lines, "digest").startswith("ok "))
        self.assertEqual(sorted(self.p.hashed), sorted([VISION, TEXT, TOKENIZER]))

    def test_missing_required_prints_the_exact_dnf_line(self):
        self.p.tools -= {"exiftool", "ffmpeg", "ffprobe", "findmnt"}
        self.p.modules -= {"zxingcpp", "yaml"}
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("Install what's missing:  sudo dnf install perl-Image-ExifTool ffmpeg-free "
                      "util-linux-core python3-zxing-cpp python3-pyyaml", lines)
        self.assertTrue(self.line(lines, "exiftool (perl-Image-ExifTool)").startswith("MISSING "))

    def test_tesseract_without_eng(self):
        self.p.langs = "List of available languages (1):\nosd\n"
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("Install what's missing:  sudo dnf install tesseract-langpack-eng", lines)

    def test_optional_missing_passes_and_prints_its_own_line(self):
        self.p.plugins = "nothing useful\n"
        del self.p.files[cli.MTP_BACKEND]
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertIn("Optional:  sudo dnf install gstreamer1-plugins-bad-free gvfs-mtp", lines)
        self.assertFalse([ln for ln in lines if ln.startswith("Install what's missing")], lines)

    def test_no_xdg_open_is_optional(self):
        self.assertTrue(self.line(self.run_doctor()[1], "xdg-open").startswith("ok "))
        self.p.tools.discard("xdg-open")
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.line(lines, "xdg-open"),
                         "optional xdg-open (the gallery's Open in the system player button) (xdg-utils)")
        self.assertIn("Optional:  sudo dnf install xdg-utils", lines)

    def test_no_gst_inspect_is_optional(self):
        self.p.tools.discard("gst-inspect-1.0")
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertIn("Optional:  sudo dnf install gstreamer1", lines)

    def test_vision_model_checksum_mismatch_fails(self):
        self.p.files[VISION] = OTHER
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        bad = self.line(lines, "SigLIP vision model")
        self.assertTrue(bad.startswith("BAD "), bad)
        self.assertIn(f"sha256 {OTHER[:12]}, pinned {FILES['onnx/vision_model_quantized.onnx'][:12]}", bad)

    def test_vision_model_missing_fails(self):
        del self.p.files[VISION]
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertTrue(self.line(lines, "SigLIP vision model").startswith("MISSING "))

    def test_text_model_absent_is_optional_but_wrong_fails(self):
        del self.p.files[TEXT], self.p.files[TOKENIZER]
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertTrue(self.line(lines, "SigLIP text model").startswith("optional "))
        self.p.files[TOKENIZER] = OTHER
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertTrue(self.line(lines, "SigLIP tokenizer").startswith("BAD "))

    def test_ollama_digest_mismatch_fails(self):
        self.p.tags["models"][0]["digest"] = OTHER
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn(f"digest {OTHER[:12]}, pinned {PINS['ollama']['digest'][:12]}", self.line(lines, "digest"))

    def test_ollama_absent_stopped_or_unpulled_is_optional(self):
        for setup, text in ((lambda: self.p.tools.discard("ollama"), "ollama not installed"),
                            (lambda: setattr(self.p, "tags", ConnectionRefusedError()), "not answering on 127.0.0.1"),
                            (lambda: setattr(self.p, "tags", {"models": [{"name": "other:1b", "digest": OTHER}]}),
                             "not pulled")):
            with self.subTest(text):
                self.p = FakeProbe()
                setup()
                code, lines = self.run_doctor()
                self.assertEqual(code, 0, lines)
                self.assertTrue(self.line(lines, text).startswith("optional "))

    def test_real_probe_asks_only_loopback_ollama(self):
        self.assertEqual(vlm_trash.check_host(vlm_trash.HOST), vlm_trash.HOST)
        with mock.patch.object(vlm_trash, "_call", return_value={"models": []}) as call:
            self.assertEqual(cli.Probe().ollama_tags(), {"models": []})
        self.assertEqual(call.call_args.args, ("http://127.0.0.1:11434", "/api/tags"))

    def test_bad_config_stops_early(self):
        with mock.patch.object(cli.config, "load", side_effect=cli.config.ConfigError("nope")):
            code, lines = self.run_doctor()
        self.assertEqual((code, lines), (1, ["BAD      config: nope"]))

    def test_main_dispatches_doctor(self):
        with mock.patch.object(cli, "doctor", return_value=0) as d, mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["doctor"]), 0)
        d.assert_called_once_with()


class Pins(unittest.TestCase):
    """media_intake/pins.json is the only place a model pin is written down."""

    def test_consumers_read_pins_json(self):
        self.assertEqual(label.REV, PINS["siglip"]["revision"])
        self.assertEqual(label.MODEL_SHA, FILES["onnx/vision_model_quantized.onnx"])
        self.assertEqual((semantic.TEXT_SHA, semantic.TOK_SHA), (FILES["onnx/text_model.onnx"], FILES["tokenizer.json"]))
        self.assertEqual((vlm_trash.MODEL, vlm_trash.DIGEST), (PINS["ollama"]["model"], PINS["ollama"]["digest"]))

    PREFIX = 12  # `ollama list` and doctor show 12 characters; a copied prefix goes stale like a full pin

    def test_no_pin_copied_elsewhere(self):
        pins = set(FILES.values()) | {PINS["siglip"]["revision"], PINS["ollama"]["digest"]}
        prefixes = {pin[:self.PREFIX] for pin in pins}
        root = support.ROOT
        paths = [os.path.join(root, "setup.sh"), *glob.glob(os.path.join(support.PKG, "*.py")),
                 *glob.glob(os.path.join(root, "docs", "*.md")),
                 *(os.path.join(root, f) for f in ("README.md", "ROADMAP.md", "CONTRIBUTING.md")),
                 *glob.glob(os.path.join(root, ".github", "workflows", "*.yml"))]
        for p in paths:
            with open(p) as f:
                runs = re.findall(r"(?<![0-9a-f])[0-9a-f]{%d,}" % self.PREFIX, f.read())
            found = sorted({r for r in runs if r[:self.PREFIX] in prefixes})
            self.assertFalse(found, f"{os.path.relpath(p, root)} repeats a pin (or its prefix) from pins.json")

    def test_prefix_check_catches_a_short_copy(self):
        digest = PINS["ollama"]["digest"]
        for text in (digest, f"ID `{digest[:12]}`", f"`{digest[:16]}...`"):
            runs = re.findall(r"(?<![0-9a-f])[0-9a-f]{%d,}" % self.PREFIX, text)
            self.assertIn(digest[:12], {r[:12] for r in runs}, text)


class KillByPid(unittest.TestCase):
    """ROADMAP: kill by PID. `pkill -f` / `pgrep -f` match their own shell's command line.
    Scans code, workflows, .claude/ and the Markdown docs (repo root and docs/)."""

    PATTERN = re.compile(r"\b(?:pkill|pgrep|killall)\b" + r"|kill\s+\$\(")

    @classmethod
    def offends(cls, path, line):
        if path.endswith((".py", ".sh", ".yml")):
            # In code, a comment saying "never pgrep -f" is the rule, not a use: check only what precedes `#`.
            line = line.split("#", 1)[0]
        elif path.endswith(".md") and re.search(r"\bnever\b", line, re.I):
            # In prose, a line that states the rule ("never `pkill -f`") is not an instruction to use it.
            return False
        return bool(cls.PATTERN.search(line))

    def test_no_kill_by_command_line(self):
        root = support.ROOT
        paths = [*glob.glob(os.path.join(root, "**", "*.py"), recursive=True),
                 *glob.glob(os.path.join(root, "**", "*.sh"), recursive=True),
                 *glob.glob(os.path.join(root, ".github", "workflows", "*.yml")),
                 *glob.glob(os.path.join(root, ".claude", "**", "*"), recursive=True),
                 *glob.glob(os.path.join(root, "*.md")), *glob.glob(os.path.join(root, "docs", "*.md"))]
        hits = []
        for p in sorted(set(paths)):
            if not os.path.isfile(p) or os.path.abspath(p) == os.path.abspath(__file__):
                continue
            with open(p, errors="replace") as f:
                hits += [f"{os.path.relpath(p, root)}:{n}" for n, ln in enumerate(f, 1) if self.offends(p, ln)]
        self.assertEqual(hits, [])

    def test_markdown_rule_lines_pass_and_uses_fail(self):
        self.assertFalse(self.offends("ROADMAP.md", "- Kill by PID, never `pkill -f`/`pgrep -f`."))
        self.assertTrue(self.offends("docs/x.md", "Stop it with `pkill -f media-intake`."))
        self.assertFalse(self.offends("a.py", "x = 1  # never pgrep -f"))
        self.assertTrue(self.offends("a.sh", "pgrep -f media-intake  # never"))


class Version(unittest.TestCase):
    """docs/release.md: the version lives only in media_intake/__init__.py."""

    def test_pyproject_reads_the_version_from_init(self):
        import tomllib
        with open(os.path.join(support.ROOT, "pyproject.toml"), "rb") as f:
            project = tomllib.load(f)
        self.assertNotIn("version", project["project"])
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"], {"attr": "media_intake.__version__"})
        with open(os.path.join(support.PKG, "__init__.py")) as f:
            versions = re.findall(r'^__version__ = "([^"]+)"$', f.read(), re.M)
        self.assertEqual(len(versions), 1)
        self.assertRegex(versions[0], r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
