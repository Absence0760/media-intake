"""setup.sh: step headings, failure messages, summary, and the sha256 pin rule.

The static cases read setup.sh as text. The run cases execute a copy of it in a
throwaway tree with every network and install step stubbed: `curl` is a script
on PATH that writes a few bytes (or fails), the venv's python and pip are stubs,
pins.json pins the stub bytes, and cli.py's doctor is a stub. Nothing is
downloaded or installed, and HOME and the state dir are temp dirs.
"""
import hashlib, json, os, pty, re, select, shutil, subprocess, tempfile, unittest

import support

SETUP = os.path.join(support.ROOT, "setup.sh")
VISION = "onnx/vision_model_quantized.onnx"

CURL_STUB = """#!/bin/sh
out=; prev=; url=
for a; do [ "$prev" = -o ] && out=$a; prev=$a; url=$a; done
case ${STUB_CURL:-ok} in
  offline) exit 6 ;;
  bad) printf 'tampered' > "$out" ;;
  *) printf 'stub:%s' "${url##*/}" > "$out" ;;
esac
"""
PIP_STUB = """#!/bin/sh
[ "${STUB_PIP:-ok}" = hash ] && { echo "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE." >&2; exit 1; }
exit 0
"""
PY_STUB = "#!/bin/sh\necho venv-python-stub\n"
DOCTOR_STUB = "import os, sys\nprint('doctor stub')\nsys.exit(1 if os.environ.get('STUB_DOCTOR') == 'fail' else 0)\n"


def stub_sha(name):
    return hashlib.sha256(f"stub:{name}".encode()).hexdigest()


def read_setup():
    with open(SETUP) as f:
        return f.read()


class Static(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", SETUP], check=True)

    def test_keeps_strict_mode_and_err_trap(self):
        text = read_setup()
        self.assertRegex(text, r"(?m)^set -Eeuo pipefail$")
        self.assertIn("trap 'on_err $? $LINENO' ERR", text)

    def test_curl_only_in_download_helper(self):
        lines = read_setup().splitlines()
        curls = [i for i, l in enumerate(lines) if re.search(r"(?<![\w-])(curl|wget|aria2c)\s+(-|\"|\$|https?:)", l.split("#", 1)[0])]
        self.assertEqual(len(curls), 1, "one downloader call, inside download()")
        start = max(i for i, l in enumerate(lines[:curls[0]]) if l.startswith("download()"))
        self.assertLess(curls[0] - start, 5)
        self.assertNotRegex(lines[curls[0]], r"\s(-k|--insecure)\b")

    def test_every_download_is_hashed_right_after(self):
        lines = read_setup().splitlines()
        calls = [i for i, l in enumerate(lines) if re.match(r"\s*download \"", l)]
        self.assertGreaterEqual(len(calls), 2, "fetch() and the GeoNames first-use path")
        for i in calls:
            window = "\n".join(lines[i + 1:i + 6])
            self.assertIn("sha256sum", window, f"line {i + 1}: download not followed by a sha256 check")

    def test_fetch_is_always_given_a_pin(self):
        for line in read_setup().splitlines():
            if re.match(r"\s*fetch \"", line):
                self.assertEqual(len(re.findall(r'"[^"]*"', line)), 3, line)

    def test_pip_always_requires_hashes(self):
        installs = [l for l in read_setup().splitlines() if re.search(r"\bpip\"? install\b", l)]
        self.assertEqual(len(installs), 1, "one pip install, inside pip_install()")
        self.assertIn("--require-hashes", installs[0])

    def test_failure_messages_exist(self):
        text = read_setup()
        for msg in ("FAILED at step", "sudo dnf install -y", "didn't match its sha256 pin",
                    "nothing was installed", "never skip", "No network", "setup.sh --refresh-geo",
                    "Never drop --require-hashes", "then run: media-intake doctor", "NO_COLOR"):
            self.assertIn(msg, text)


class Run(unittest.TestCase):
    """setup.sh copied into a temp tree, all network and installs stubbed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="media-intake-setup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        pkg = os.path.join(self.repo, "media_intake")
        os.makedirs(pkg)
        shutil.copy(SETUP, self.repo)
        shutil.copy(os.path.join(support.PKG, "config.py"), pkg)
        pins = {"siglip": {"repo": "example/model", "revision": "0123456789abcdef",
                           "files": {VISION: stub_sha("vision_model_quantized.onnx"),
                                     "onnx/text_model.onnx": stub_sha("text_model.onnx"),
                                     "tokenizer.json": stub_sha("tokenizer.json")}}}
        self.pins = os.path.join(pkg, "pins.json")
        with open(self.pins, "w") as f:
            json.dump(pins, f)
        with open(os.path.join(pkg, "cli.py"), "w") as f:
            f.write(DOCTOR_STUB)
        open(os.path.join(self.repo, "requirements.txt"), "w").close()
        self.bin = os.path.join(self.tmp, "bin")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        venv = os.path.join(self.state, "venv", "bin")
        for path, body in ((os.path.join(self.bin, "curl"), CURL_STUB), (os.path.join(venv, "pip"), PIP_STUB),
                           (os.path.join(venv, "python"), PY_STUB)):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o700)  # owner-only: only this test runs them
        os.makedirs(self.home)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        env.update(HOME=self.home, MEDIA_INTAKE_STATE=self.state, PATH=self.bin + os.pathsep + env["PATH"])
        env.update(extra)
        return env

    def run_setup(self, *args, **extra):
        return subprocess.run(["bash", os.path.join(self.repo, "setup.sh"), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def test_fresh_run_then_rerun(self):
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        for n, name in enumerate(("System packages and state dir", "Python venv", "SigLIP vision model",
                                  "GeoNames", "Launcher", "media-intake doctor"), 1):
            self.assertIn(f"==> [{n}/6] {name}", r.stdout)
        self.assertNotIn("\x1b[", r.stdout + r.stderr, "no colour without a terminal")
        summary = r.stdout.split("==> Summary", 1)[1]
        self.assertRegex(summary, r"SigLIP vision model\s+downloaded")
        self.assertRegex(summary, r"GeoNames\s+downloaded")
        self.assertRegex(summary, r"launcher\s+written\s+" + re.escape(os.path.join(self.home, ".local/bin/media-intake")))
        self.assertTrue(r.stdout.rstrip().endswith("next: media-intake stage <folder> --kind folder   "
                                                   "(guide: docs/getting-started.md)"), r.stdout[-300:])
        self.assertNotIn("next: media-intake doctor", r.stdout, "doctor just ran; don't send the user back to it")

        again = self.run_setup()
        self.assertEqual(again.returncode, 0, again.stderr)
        summary = again.stdout.split("==> Summary", 1)[1]
        for what in ("state dir", "venv", "SigLIP vision model", "GeoNames", "launcher"):
            self.assertRegex(summary, re.escape(what) + r"\s+already present", what)

    def test_step_count_follows_options(self):
        r = self.run_setup("--no-launcher")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("==> [5/5] media-intake doctor", r.stdout)
        self.assertNotIn("Launcher", r.stdout)

    def test_doctor_gaps_change_the_next_line(self):
        r = self.run_setup(STUB_DOCTOR="fail")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.split("==> Summary", 1)[1], r"doctor\s+gaps listed above")
        self.assertTrue(r.stdout.rstrip().endswith("next: fix what doctor listed above, then run: media-intake doctor"))

    def test_path_hint_when_local_bin_is_not_on_path(self):
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Your PATH lacks ~/.local/bin, so the shell won't find media-intake yet", r.stderr)
        tail = r.stdout.split("==> Summary", 1)[1]
        self.assertIn('  export PATH="$HOME/.local/bin:$PATH"\n', tail)
        self.assertIn("~/.bashrc for bash, ~/.zshrc for zsh", tail)
        self.assertIn("python3 -I " + os.path.join(self.repo, "media_intake", "cli.py"), tail)
        self.assertIn("The launcher runs this copy (" + self.repo + ")", tail)

    def test_no_path_hint_when_local_bin_is_on_path(self):
        local_bin = os.path.join(self.home, ".local", "bin")
        for entry in (local_bin, local_bin + "/"):
            with self.subTest(entry=entry):
                r = self.run_setup(PATH=os.pathsep.join([self.bin, entry, os.environ["PATH"]]))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Your PATH lacks", r.stdout + r.stderr)
                self.assertNotIn("export PATH=", r.stdout)

    def test_no_launcher_no_path_hint(self):
        r = self.run_setup("--no-launcher")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Your PATH lacks", r.stdout + r.stderr)
        self.assertNotIn("The launcher runs this copy", r.stdout)

    def test_phone_hint_in_the_closing_lines(self):
        tail = self.run_setup().stdout.split("==> Summary", 1)[1]
        self.assertIn('choose "File transfer"', tail)
        self.assertIn("ls /run/user/$UID/gvfs/", tail, "printed literally, for the user to type")
        self.assertIn("--kind phone", tail)
        phone = tail[tail.index("To find a phone"):].split("\n\n", 1)[0]
        self.assertEqual(len(phone.splitlines()), 3, "one paragraph, no blank lines inside")

    def test_checksum_mismatch_names_step_and_installs_nothing(self):
        r = self.run_setup(STUB_CURL="bad")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED at step [3/6] SigLIP vision model", r.stderr)
        self.assertIn("didn't match its sha256 pin", r.stderr)
        self.assertIn("never skip", r.stderr)
        self.assertEqual(r.stderr.count("FAILED at step"), 1)
        models = os.path.join(self.state, "models")
        self.assertEqual([fs for _, _, fs in os.walk(models) if fs], [], "the bad file was deleted")
        self.assertNotIn("Summary", r.stdout)

    def test_offline_says_so(self):
        r = self.run_setup(STUB_CURL="offline")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED at step [3/6] SigLIP vision model", r.stderr)
        self.assertIn("No network", r.stderr)

    def test_pip_hash_mismatch(self):
        r = self.run_setup(STUB_PIP="hash")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED at step [2/6] Python venv", r.stderr)
        self.assertIn("didn't match its sha256 pin", r.stderr)

    def test_broken_pins_json_reported_once(self):
        with open(self.pins, "w") as f:
            f.write("{}")
        r = self.run_setup()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stderr.count("FAILED at step"), 1, r.stderr)
        self.assertIn("FAILED at step [3/6] SigLIP vision model", r.stderr)
        self.assertIn("pins.json", r.stderr)

    def test_geonames_changed_since_pin(self):
        self.assertEqual(self.run_setup().returncode, 0)
        os.remove(os.path.join(self.state, "geo", "countryInfo.txt"))
        r = self.run_setup(STUB_CURL="bad")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED at step [4/6] GeoNames", r.stderr)
        self.assertIn("setup.sh --refresh-geo", r.stderr)

    def test_missing_command_prints_dnf_line(self):
        # A PATH with only what the first step needs, minus curl.
        only = os.path.join(self.tmp, "only")
        os.makedirs(only)
        for tool in ("dirname", "python3", "sha256sum", "awk", "sed"):
            os.symlink(shutil.which(tool), os.path.join(only, tool))
        shutil.rmtree(self.state)
        r = subprocess.run([shutil.which("bash"), os.path.join(self.repo, "setup.sh")],
                           env={**self.env(), "PATH": only}, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED at step [1/6] System packages and state dir", r.stderr)
        self.assertIn("sudo dnf install -y curl", r.stderr)
        self.assertFalse(os.path.exists(self.state), "stopped before creating anything")

    def run_on_tty(self, **extra):
        master, slave = pty.openpty()
        p = subprocess.Popen(["bash", os.path.join(self.repo, "setup.sh")], env=self.env(**extra),
                             stdout=slave, stderr=subprocess.DEVNULL)
        os.close(slave)
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], 60)
            if not ready:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.close(master)
        self.assertEqual(p.wait(timeout=60), 0)
        return out.decode()

    def test_colour_only_on_tty_without_no_color(self):
        self.assertIn("\x1b[", self.run_on_tty())
        self.assertNotIn("\x1b[", self.run_on_tty(NO_COLOR="1"))


if __name__ == "__main__":
    unittest.main()
