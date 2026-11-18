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
        self.plugins = "va:  vah265dec: VA-API H.265 Decoder\nopenh264:  openh264enc: OpenH264 video encoder\n"
        # Fedora's ffmpeg-free: an H.264 encoder but no HEVC decoder, so the gallery's proxies need GStreamer
        self.ffmpeg = {"-encoders": " V....D libopenh264          OpenH264 H.264\n",
                       "-decoders": " V....D h264                 H.264\n"}
        self.files = {VISION: FILES["onnx/vision_model_quantized.onnx"], TEXT: FILES["onnx/text_model.onnx"],
                      TOKENIZER: FILES["tokenizer.json"], cli.venv_python(): None, cli.MTP_BACKEND: None,
                      os.path.join(os.environ["MEDIA_INTAKE_STATE"], "geo", "cities1000.zip"): None}
        self.tags = {"models": [{"name": PINS["ollama"]["model"], "model": PINS["ollama"]["model"],
                                 "digest": PINS["ollama"]["digest"]}]}
        self.hashed = []

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.tools else None

    def output(self, cmd):
        if cmd[0] == "ffmpeg":
            return self.ffmpeg[cmd[-1]]
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

    def summary(self, lines):
        """The lines after the `== Summary ==` heading."""
        return lines[lines.index("== Summary ==") + 1:]

    def dnf(self, lines):
        return [ln for ln in lines if "dnf install" in ln]

    def test_everything_present_passes_with_no_install_line(self):
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertFalse([ln for ln in lines if "dnf install" in ln], lines)
        self.assertEqual(self.summary(lines), ["no problems, no optional items not available"])
        self.assertTrue(self.line(lines, "SigLIP vision model").startswith("ok "))
        self.assertTrue(self.line(lines, "digest").startswith("ok "))
        self.assertEqual(sorted(self.p.hashed), sorted([VISION, TEXT, TOKENIZER]))

    def test_missing_required_prints_the_exact_dnf_line(self):
        self.p.tools -= {"exiftool", "ffmpeg", "ffprobe", "findmnt"}
        self.p.modules -= {"zxingcpp", "yaml"}
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertEqual(self.dnf(lines), ["sudo dnf install -y perl-Image-ExifTool ffmpeg-free "
                                           "util-linux-core python3-zxing-cpp python3-pyyaml"])
        self.assertEqual(self.summary(lines)[0], "6 problems, no optional items not available")
        self.assertTrue(self.line(lines, "exiftool (perl-Image-ExifTool)").startswith("MISSING "))

    def test_tesseract_without_eng(self):
        self.p.langs = "List of available languages (1):\nosd\n"
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertEqual(self.dnf(lines), ["sudo dnf install -y tesseract-langpack-eng"])

    def test_optional_missing_passes_and_prints_its_own_line(self):
        self.p.plugins = "nothing useful\n"
        del self.p.files[cli.MTP_BACKEND]
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.dnf(lines),
                         ["sudo dnf install -y gstreamer1-plugins-bad-free gstreamer1-plugin-openh264 gvfs-mtp"])
        self.assertEqual(self.summary(lines), ["no problems, 3 optional items not available", "Optional packages:",
                                               self.dnf(lines)[0]])

    def test_no_xdg_open_is_optional(self):
        self.assertTrue(self.line(self.run_doctor()[1], "xdg-open").startswith("ok "))
        self.p.tools.discard("xdg-open")
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.line(lines, "xdg-open"),
                         "optional xdg-open (the gallery's Open in the system player button) (xdg-utils)")
        self.assertEqual(self.dnf(lines), ["sudo dnf install -y xdg-utils"])

    def test_h264_encoder_for_gallery_proxies(self):
        """An H.264 encoder for the gallery's HEVC proxies is optional: GStreamer's, or ffmpeg's only when that
        ffmpeg can also decode HEVC (Fedora's ffmpeg-free can't)."""
        what = "H.264 encoder for the gallery's HEVC proxies"
        self.assertEqual(self.line(self.run_doctor()[1], what), f"ok       {what} (openh264enc)")
        self.p.plugins = "va:  vah265dec: VA-API H.265 Decoder\n"  # no GStreamer encoder
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertTrue(self.line(lines, what).startswith("optional "), lines)
        self.assertEqual(self.dnf(lines), ["sudo dnf install -y gstreamer1-plugin-openh264"])
        self.p.ffmpeg["-decoders"] += " VFS..D hevc                 HEVC\n"  # ffmpeg alone can do it now
        self.assertEqual(self.line(self.run_doctor()[1], what), f"ok       {what} (ffmpeg libopenh264)")
        self.p.tools -= {"gst-inspect-1.0", "ffmpeg"}
        code, lines = self.run_doctor()
        self.assertEqual(code, 1, lines)  # ffmpeg itself is required
        self.assertTrue(self.line(lines, what).startswith("optional "), lines)
        for x264 in ("x264:  x264enc: x264 H.264 Encoder\n", "va:  vah264enc: VA-API H.264 Encoder\n"):
            self.p.tools |= {"gst-inspect-1.0"}
            self.p.plugins = x264
            self.assertTrue(self.line(self.run_doctor()[1], what).startswith("ok "))

    def test_no_gst_inspect_is_optional(self):
        self.p.tools.discard("gst-inspect-1.0")
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.dnf(lines), ["sudo dnf install -y gstreamer1 gstreamer1-plugin-openh264"])

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
        self.assertEqual((code, lines), (1, ["== Config and folders ==", "BAD      config: nope", "",
                                             "== Summary ==", "1 problem, no optional items not available"]))

    HEADINGS = ["== Config and folders ==", "== System tools ==", "== Python modules ==", "== Optional extras ==",
                "== Venv, models and data ==", "== Ollama (local, 127.0.0.1) ==", "== Summary =="]
    STATUS = re.compile(r"^(ok|MISSING|optional|BAD|absent) +\S")

    def test_grouped_under_headings_in_order(self):
        code, lines = self.run_doctor()
        self.assertEqual([ln for ln in lines if ln.startswith("== ")], self.HEADINGS)
        sections = {}
        for ln in lines:
            if ln.startswith("== "):
                current = ln
            elif ln:
                sections.setdefault(current, []).append(ln)
        self.assertIn("exiftool", " ".join(sections["== System tools =="]))
        self.assertIn("tesseract eng language data", " ".join(sections["== System tools =="]))
        self.assertIn("python3-pyyaml", " ".join(sections["== Python modules =="]))
        self.assertIn("gvfs MTP backend", " ".join(sections["== Optional extras =="]))
        self.assertIn("SigLIP vision model", " ".join(sections["== Venv, models and data =="]))
        self.assertIn("digest", " ".join(sections["== Ollama (local, 127.0.0.1) =="]))

    def test_all_ok_prints_only_ok_lines_headings_and_summary(self):
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, lines)
        body = lines[:lines.index("== Summary ==")]
        for ln in body:
            if ln and not ln.startswith("== "):
                self.assertRegex(ln, r"^(ok|absent) +\S", ln)  # absent: the throwaway HOME's folders
        self.assertFalse([ln for ln in body if ln.startswith(("MISSING", "BAD", "optional"))], lines)
        self.assertEqual(self.summary(lines), ["no problems, no optional items not available"])

    def test_every_status_line_keeps_its_greppable_shape(self):
        self.p.tools -= {"exiftool", "ollama"}
        self.p.files[VISION] = OTHER
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        body = lines[:lines.index("== Summary ==")]
        for ln in body:
            if ln and not ln.startswith("== "):
                self.assertRegex(ln, self.STATUS)
                self.assertEqual(ln[8], " ", ln)  # status padded to 8, then a space, as before
        self.assertEqual(self.summary(lines)[0], "2 problems, 1 optional item not available")

    def test_required_and_optional_dnf_lines_each_once_deduplicated(self):
        """ffmpeg and ffprobe share ffmpeg-free; it appears once. Required and optional lines are separate."""
        self.p.tools -= {"ffmpeg", "ffprobe", "exiftool", "xdg-open"}
        del self.p.files[cli.MTP_BACKEND]
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertEqual(self.dnf(lines), ["sudo dnf install -y perl-Image-ExifTool ffmpeg-free",
                                           "sudo dnf install -y gvfs-mtp xdg-utils"])
        self.assertEqual(self.summary(lines), ["3 problems, 2 optional items not available",
                                               "Install the required packages:", self.dnf(lines)[0],
                                               "Optional packages:", self.dnf(lines)[1]])

    def test_dnf_line_has_exactly_the_missing_packages(self):
        self.p.modules -= {"PIL"}
        _, lines = self.run_doctor()
        (line,) = self.dnf(lines)
        self.assertEqual(line.split()[:4], ["sudo", "dnf", "install", "-y"])
        self.assertEqual(line.split()[4:], ["python3-pillow"])

    def test_exit_code_unaffected_by_optional_only(self):
        self.p.tools -= {"ollama", "xdg-open"}
        self.assertEqual(self.run_doctor()[0], 0)
        self.p.files.pop(os.path.join(os.environ["MEDIA_INTAKE_STATE"], "geo", "cities1000.zip"))
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertFalse([ln for ln in lines if "sudo dnf install -y" in ln and "geo" in ln.lower()])

    def test_no_colour_unless_asked(self):
        _, lines = self.run_doctor()
        self.assertFalse([ln for ln in lines if "\033[" in ln])
        lines = []
        self.p.tools.discard("exiftool")
        cli.doctor(self.p, out=lines.append, colour=True)
        self.assertTrue(self.line(lines, "exiftool (perl-Image-ExifTool)").startswith("\033[1;31mMISSING\033[0m "))
        self.assertIn("\033[1m== Summary ==\033[0m", lines)
        plain = [re.sub(r"\033\[[0-9;]*m", "", ln) for ln in lines]
        self.assertEqual(plain, self.run_doctor()[1])  # colour adds escapes, nothing else

    def test_default_colour_follows_the_terminal_and_no_color(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        for stream, env, want in ((io.StringIO(), {}, False), (Tty(), {}, True),
                                  (Tty(), {"NO_COLOR": "1"}, False), (Tty(), {"NO_COLOR": ""}, True)):
            with self.subTest(tty=stream.isatty(), env=env):
                environ = {k: v for k, v in os.environ.items() if k != "NO_COLOR"} | env
                with mock.patch.dict(os.environ, environ, clear=True), mock.patch("sys.stdout", stream):
                    self.assertEqual(cli.use_colour(stream), want)
                    cli.doctor(self.p)
                self.assertEqual("\033[" in stream.getvalue(), want)
        self.assertFalse(cli.use_colour(object()))

    def test_main_dispatches_doctor(self):
        with mock.patch.object(cli, "doctor", return_value=0) as d, mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["doctor"]), 0)
        d.assert_called_once_with()

    def test_doctor_help_prints_help_and_runs_nothing(self):
        for flag in ("--help", "-h", "help"):
            with self.subTest(flag=flag):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(cli, "doctor") as d, mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                    self.assertEqual(cli.main(["doctor", flag]), 0)
                d.assert_not_called()
                self.assertEqual(err.getvalue(), "")
                self.assertTrue(out.getvalue().startswith("usage: media-intake doctor\n"), out.getvalue())
                self.assertIn("Changes nothing. Exits 1 if something required is missing", out.getvalue())

    def test_doctor_rejects_other_arguments(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "doctor") as d, mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["doctor", "--bogus"]), 2)
        d.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertIn("media-intake doctor takes no options", err.getvalue())

    def test_setup_problems_add_a_run_setup_line(self):
        del self.p.files[cli.venv_python()]
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertEqual(self.summary(lines), ["1 problem, no optional items not available",
                                               "Run: bash setup.sh   (venv, SigLIP model, GeoNames data)"])
        self.p.modules -= {"PIL"}
        _, lines = self.run_doctor()
        self.assertEqual(self.summary(lines)[1:], ["Install the required packages:", "sudo dnf install -y python3-pillow",
                                                   "Then run: bash setup.sh   (venv, SigLIP model, GeoNames data)"])

    def test_no_run_setup_line_for_packages_or_optional_only(self):
        self.p.modules -= {"PIL"}
        del self.p.files[TEXT], self.p.files[TOKENIZER]
        _, lines = self.run_doctor()
        self.assertFalse([ln for ln in self.summary(lines) if "setup.sh" in ln], lines)


class Commands(unittest.TestCase):
    """`media-intake` with no command or --help prints the usage (exit 0); an unknown one names the nearest
    real command and exits 2, as argparse does."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_args_and_help_print_usage_and_exit_0(self):
        for argv in ([], ["--help"], ["-h"], ["help"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(argv)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("usage: media-intake <command> [options]", out)
                self.assertIn("docs/words.md", out)

    def test_names_include_every_intake_subcommand(self):
        import argparse, intake
        sub = next(a for a in intake.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        names = cli.command_names()
        self.assertLessEqual(set(sub.choices), set(names))
        self.assertLessEqual(intake.LOCKED | intake.READ_ONLY, set(names))
        self.assertLessEqual({*cli.COMMANDS, *cli.VENV_SCRIPTS}, set(names))
        self.assertEqual(names, sorted(set(names)))

    def test_unknown_command_suggests_and_exits_2(self):
        for typo, near in (("stag", "stage"), ("doctr", "doctor"), ("galery", "gallery"), ("prune-cach", "prune-cache"),
                           ("vlm-trsh", "vlm-trash"), ("suggest-trsh", "suggest-trash")):
            with self.subTest(typo):
                code, out, err = self.run_main([typo, "x"])
                self.assertEqual((code, out), (2, ""))
                first = err.splitlines()[0]
                self.assertEqual(first, f"media-intake: unknown command {typo!r}; did you mean {near!r}?")
                self.assertIn("usage: media-intake <command> [options]", err)

    def test_version(self):
        with open(os.path.join(support.PKG, "__init__.py")) as f:
            (want,) = re.findall(r'^__version__ = "([^"]+)"$', f.read(), re.M)
        for argv in (["--version"], ["-V"], ["version"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_main(argv), (0, f"media-intake {want}\n", ""))

    def test_unknown_option_gets_no_command_guess(self):
        for flag in ("--verison", "--verbose", "-x"):
            with self.subTest(flag=flag):
                code, out, err = self.run_main([flag])
                self.assertEqual((code, out), (2, ""))
                first = err.splitlines()[0]
                self.assertTrue(first.startswith(f"media-intake: unknown option {flag!r} "), first)
                self.assertNotIn("did you mean", err)
                self.assertIn("--version", first)

    def test_unknown_command_without_a_close_match(self):
        code, out, err = self.run_main(["zzzzzz"])
        self.assertEqual(code, 2)
        self.assertEqual(err.splitlines()[0], "media-intake: unknown command 'zzzzzz'")
        self.assertIn("commands: ", err)

    def test_unknown_command_never_reaches_intake(self):
        import intake
        with mock.patch.object(intake, "main") as m:
            self.assertEqual(self.run_main(["nope"])[0], 2)
        m.assert_not_called()

    def test_known_intake_command_still_forwarded(self):
        import intake
        with mock.patch.object(intake, "main") as m:
            self.assertEqual(self.run_main(["find", "--event", "x"])[0], 0)
        m.assert_called_once_with(["find", "--event", "x"], prog="media-intake")


class HelpText(unittest.TestCase):
    """Help a non-technical owner can read: `media-intake --help` lists every command once, by task, with no
    argparse `{a,b,...}` line; every intake command's --help says what it does, what it touches and whether it
    deletes; and every option and positional (intake, gallery, review) has help text."""

    @staticmethod
    def intake_sub():
        import argparse, intake
        return next(a for a in intake.build_parser("media-intake")._actions
                    if isinstance(a, argparse._SubParsersAction))

    def top_help(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["--help"]), 0)
        return out.getvalue()

    def test_top_level_help_lists_each_command_once(self):
        out = self.top_help()
        listed = re.findall(r"^  ([a-z][a-z-]+)", out, re.M)
        for name in cli.command_names():
            if name == "social-copy":  # share-copy's older name, given on share-copy's line
                self.assertIn("(or social-copy)", out)
                continue
            with self.subTest(name):
                self.assertEqual(listed.count(name), 1, f"{name} listed once in media-intake --help")
        self.assertLessEqual(set(listed), set(cli.command_names()), "no command listed that doesn't exist")
        self.assertNotIn("{", out, "no argparse {a,b,...} usage line")
        self.assertNotIn("positional arguments", out)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 80, line)

    def test_deleting_commands_say_so_in_the_list(self):
        out = self.top_help()
        for name in ("erase", "forget", "offload", "trash", "purge-review", "prune-cache"):
            with self.subTest(name):
                self.assertRegex(out, rf"(?m)^  {name}\b.*DELETES")

    def test_every_intake_command_says_what_it_touches_and_deletes(self):
        for name, p in self.intake_sub().choices.items():
            with self.subTest(name):
                desc = p.description or ""
                self.assertRegex(desc, r"(?m)^Touches: \S", "says what it touches")
                self.assertRegex(desc, r"(?m)^Deletes: \S", "says whether it deletes")
                self.assertTrue(desc.split("\n\n")[0].strip(), "says what it does")
                deletes = desc.split("Deletes: ", 1)[1]
                summary = next(a.help for a in self.intake_sub()._choices_actions if a.dest == name)
                self.assertEqual(deletes.startswith("YES"), summary.startswith("DELETES"),
                                 "the one-line summary says DELETES exactly when the command deletes")
                for line in p.format_help().splitlines():
                    self.assertLessEqual(len(line), 80, line)

    def assert_every_argument_has_help(self, parser):
        for a in parser._actions:
            if a.help == "show this help message and exit":
                continue
            with self.subTest(prog=parser.prog, arg=a.option_strings or a.dest):
                self.assertTrue(a.help and len(a.help) > 8, "has help text")

    def test_every_intake_option_and_positional_has_help(self):
        for p in self.intake_sub().choices.values():
            self.assert_every_argument_has_help(p)

    def parser_of(self, module):
        """gallery's or review_ui's parser, as main() builds it: captured from --help's parse."""
        import argparse
        seen = []
        real = argparse.ArgumentParser.parse_args

        def grab(ap, args=None, namespace=None):
            seen.append(ap)
            return real(ap, args, namespace)
        with mock.patch.object(argparse.ArgumentParser, "parse_args", grab), \
                mock.patch("sys.stdout", io.StringIO()) as out, self.assertRaises(SystemExit) as e:
            module.main(["--help"])
        self.assertEqual(e.exception.code, 0)
        return seen[0], out.getvalue()

    def test_gallery_and_review_help(self):
        import gallery, review_ui
        for module, prog in ((gallery, "media-intake gallery"), (review_ui, "media-intake review")):
            with self.subTest(prog):
                ap, out = self.parser_of(module)
                self.assertTrue(out.startswith(f"usage: {prog} "), "names the command, not cli.py")
                self.assertIn("\nTouches: ", out)
                self.assertIn("\nDeletes: ", out)
                self.assertIn("the backup drive", out, "--drive is the backup drive")
                self.assertNotIn("passed to", out, "no internal wording")
                self.assert_every_argument_has_help(ap)

    def test_bad_port_is_a_usage_error(self):
        import gallery, review_ui
        for module in (gallery, review_ui):
            for bad in ("99999", "-1", "x"):
                with self.subTest(module=module.__name__, port=bad), mock.patch("sys.stderr", io.StringIO()) as err, \
                        self.assertRaises(SystemExit) as e:
                    module.main([f"--port={bad}"])
                self.assertEqual(e.exception.code, 2)
                self.assertIn("a port is 0-65535", err.getvalue())


class VenvCommands(unittest.TestCase):
    """index, search, similar (and vlm-trash) run in the venv; before setup.sh built it, --help still prints the
    command's options with the system Python, plus one line saying the venv is needed."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        missing = os.path.join(tempfile.mkdtemp(prefix="cli-novenv-"), "venv", "bin", "python")
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(os.path.dirname(missing))), True)
        with mock.patch.object(cli, "venv_python", return_value=missing), mock.patch.object(cli.os, "execv") as ex, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(argv)
        ex.assert_not_called()
        return code, out.getvalue(), err.getvalue()

    def test_help_without_the_venv(self):
        for cmd in ("index", "search", "similar"):
            for flag in ("--help", "-h"):
                with self.subTest(cmd=cmd, flag=flag):
                    code, out, err = self.run_main([cmd, flag])
                    self.assertEqual((code, err), (0, ""))
                    self.assertIn(f"usage: media-intake {cmd} ", out)
                    last = out.rstrip("\n").splitlines()[-1]
                    self.assertTrue(last.startswith(f"note: media-intake {cmd} needs the venv"), last)
                    self.assertTrue(last.endswith("run bash setup.sh first"), last)
        code, out, _ = self.run_main(["search", "dog", "--clean", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--unused-since Nd", out)

    def test_running_without_the_venv_says_run_setup(self):
        for argv in (["index"], ["search", "dog"], ["similar", "x.jpg", "-n", "3"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(argv)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(len(err.splitlines()), 1)
                self.assertIn(f"media-intake {argv[0]} needs the venv", err)
                self.assertIn("run bash setup.sh first", err)

    def test_with_the_venv_help_goes_to_the_venv(self):
        d = tempfile.mkdtemp(prefix="cli-venv-")
        self.addCleanup(shutil.rmtree, d, True)
        vp = os.path.join(d, "python")
        open(vp, "w").close()
        with mock.patch.object(cli, "venv_python", return_value=vp), mock.patch.object(cli.os, "execv") as ex, \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            cli.main(["search", "--help"])  # execv never returns; the mock does, so the rest is ignored
        ex.assert_called_once_with(vp, [vp, "-I", os.path.join(cli.HERE, "semantic.py"), "search", "--help"])


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


class GalleryArgs(unittest.TestCase):
    """`media-intake gallery` is cli.py's: its flags go to gallery.main as given, and intake.py never
    imports gallery.py (gallery imports intake; the other way round is an import cycle)."""

    def argv_for(self, *args):
        fake = type(sys)("gallery")
        fake.main = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"gallery": fake}):
            self.assertEqual(cli.main(["gallery", *args]), 0)
        return fake.main.call_args[0][0]

    def test_share_out_passed_on(self):
        self.assertEqual(self.argv_for("--share-out", "/tmp/x", "--port", "5"),
                         ["--share-out", "/tmp/x", "--port", "5"])

    def test_default_omits_share_out(self):
        self.assertEqual(self.argv_for(), [])

    def test_gallery_flags_parse(self):
        import gallery
        with self.assertRaises(SystemExit) as e, mock.patch("sys.stdout", io.StringIO()) as out:
            gallery.main(["--help"])
        self.assertEqual(e.exception.code, 0)
        for flag in ("--drive", "--port", "--share-out"):
            self.assertIn(flag, out.getvalue())

    def test_intake_never_imports_gallery(self):
        import intake
        with open(intake.__file__) as f:
            src = f.read()
        self.assertIsNone(re.search(r"^\s*(import gallery|from gallery )", src, re.M))
        self.assertNotIn("gallery", intake.LOCKED | intake.READ_ONLY)


class PruneCacheArgs(unittest.TestCase):
    """`media-intake prune-cache` is cli.py's (cache_prune imports gallery), with its flags passed on as given."""

    def test_flags_passed_on(self):
        fake = type(sys)("cache_prune")
        fake.main = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"cache_prune": fake}):
            self.assertEqual(cli.main(["prune-cache", "--dry-run"]), 0)
        self.assertEqual(fake.main.call_args[0][0], ["--dry-run"])

    def test_not_an_intake_subcommand(self):
        import intake
        self.assertNotIn("prune-cache", intake.LOCKED | intake.READ_ONLY)


class BackupArgs(unittest.TestCase):
    """`media-intake backup` is cli.py's (backup.py imports intake), with its arguments passed on as given."""

    def test_args_passed_on(self):
        fake = type(sys)("backup")
        fake.main = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"backup": fake}):
            self.assertEqual(cli.main(["backup", "/mnt/drive", "--verify", "--dry-run"]), 0)
        self.assertEqual(fake.main.call_args[0][0], ["/mnt/drive", "--verify", "--dry-run"])

    def test_exit_code_passed_back(self):
        fake = type(sys)("backup")
        fake.main = mock.Mock(return_value=2)
        with mock.patch.dict(sys.modules, {"backup": fake}):
            self.assertEqual(cli.main(["backup", "/mnt/drive"]), 2)

    def test_not_an_intake_subcommand(self):
        import intake
        self.assertNotIn("backup", intake.LOCKED | intake.READ_ONLY)


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
