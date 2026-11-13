#!/usr/bin/env python3
"""Regenerate the hash-pinned requirements files and media_intake/pins.json, nothing newer than 7 days.

  python3 -I scripts/update-pins.py                  rewrite requirements*.txt and pins.json in place
  python3 -I scripts/update-pins.py --check          show what would change; exit 1 if anything would
  python3 -I scripts/update-pins.py --cutoff 2026-11-06T00:00:00Z   a fixed cutoff instead of now - 7 days
  python3 -I scripts/update-pins.py --only requirements   (or --only models)

Run it with the system python3 on Fedora: the requirements are resolved for that Python
(setup.sh's venv uses it) against the system's numpy and packaging, which the venv reuses
(--system-site-packages) and which are therefore never pinned here. Then run
`bash setup.sh`, the tests and `media-intake doctor`, and commit the result with its diff.

Maintainer tool, never run by the app or the tests. It talks to pypi.org (resolution and
per-file metadata), huggingface.co (commit list, file tree, and the one small non-LFS file,
tokenizer.json, to hash it) and registry.ollama.ai (the manifest, ~1 KB). It never downloads
model weights: LFS files are pinned by the sha256 Hugging Face reports for them.

Minimum age, per kind of pin:
- Python wheels: `uv pip compile --exclude-newer CUTOFF`, which compares each distribution
  file's upload time; the files hashed here are filtered by the same cutoff.
- SigLIP: the newest commit on the repo's main branch dated before the cutoff. A newer
  commit that leaves the pinned files' sha256 unchanged keeps the old revision.
- Ollama: the registry publishes no dates, so the cooldown is first-seen: a new manifest
  digest is recorded as ollama.candidate with today's date and becomes the pin only once a
  run finds it unchanged and first seen before the cutoff.

Tooling research (2026-11-13), primary sources:
- uv `exclude-newer`: an RFC 3339 timestamp, or (uv >= 0.9.17, "Add support for relative
  durations in exclude-newer", astral-sh/uv#16814) a duration such as "7 days" / "P7D";
  checked against each file's upload time.
  https://docs.astral.sh/uv/reference/settings/#exclude-newer
  https://github.com/astral-sh/uv/blob/main/changelogs/0.9.x.md (0.9.17, 2025-12-09)
- pip `--uploaded-prior-to`: added in pip 26.0 (2026-01-30, pypa/pip#13625) for a datetime;
  a `P<n>D` duration since pip 26.1 (pypa/pip#13674). pip has no resolver-to-file mode
  that writes hashes, so it is not used here.
  https://pip.pypa.io/en/stable/cli/pip_install/  https://pip.pypa.io/en/stable/news/
- pip-tools: `pip-compile --uploaded-prior-to` is a passthrough to pip since pip-tools 7.6.0
  (needs pip >= 26.0). https://github.com/jazzband/pip-tools/blob/main/CHANGELOG.md
Chosen: uv (MIN_UV below; tested with 0.11.30, latest 0.13.8 on 2026-11-13), given an
absolute cutoff computed here so wheels, the model commit and the Ollama cooldown share one.
"""
import argparse, datetime, difflib, hashlib, importlib.metadata, json, os, re, subprocess, sys, tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINS = os.path.join(ROOT, "media_intake", "pins.json")
MIN_AGE = datetime.timedelta(days=7)
MIN_UV = (0, 9, 17)
INDEX = "https://pypi.org/simple"
# Fedora packages the venv reuses (setup.sh: --system-site-packages): resolved against the
# installed version, never listed in a requirements file.
SYSTEM = ("numpy", "packaging")
# Deliberate holds, each with its reason. Lift one only after testing what it holds back.
HOLDS = {"opencv-python-headless<5": "the pinned line is 4.x; move to 5.x only after testing NudeNet on it"}
REQUIREMENTS = {
    "requirements.txt": {
        "inputs": ["nudenet", "scipy"],
        "about": "Hash-pinned deps for scan_sensitive.py (NudeNet), label.py and geocode.py. Install with setup.sh.",
        "notes": {"scipy": "for geocode.py (cKDTree)"},
        "no_deps": False,
    },
    "requirements-labels.txt": {
        "inputs": ["tokenizers"],
        "about": ("Only for build_label_embeddings.py (regenerating labels.npz), not the runtime.\n"
                  "# Installed by `bash setup.sh --build-labels` into ~/.cache/media-intake-build/site."),
        "notes": {},
        # Only Tokenizer.from_file is used; huggingface-hub and the rest of tokenizers'
        # dependencies are never imported, so they are not pinned or installed.
        "no_deps": True,
    },
}
NON_LFS_MAX = 16 << 20   # a non-LFS file this script may download to hash (tokenizer.json is ~2.4 MB)


class PinError(Exception):
    pass


# ---------------------------------------------------------------- network and uv (tests replace these)
def fetch(url, accept=None):
    if not url.startswith("https://"):
        raise PinError(f"refusing a non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "media-intake-update-pins",
                                               **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(NON_LFS_MAX + 1)


def run_uv(args):
    r = subprocess.run(["uv", *args], capture_output=True, text=True)
    if r.returncode:
        raise PinError(f"uv {' '.join(args[:2])} failed:\n{r.stderr.strip()}")
    return r.stdout


def uv_version():
    out = run_uv(["--version"])
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        raise PinError(f"can't read the uv version from {out!r}")
    return tuple(int(x) for x in m.groups())


def system_versions():
    try:
        return {p: importlib.metadata.version(p) for p in SYSTEM}
    except importlib.metadata.PackageNotFoundError as e:
        raise PinError(f"system package {e} is not installed (sudo dnf install python3-{e})") from None


# ---------------------------------------------------------------- wheels
def wheel_fits(filename, py):
    """True for a wheel setup.sh's venv can install: CPython `py` (e.g. "3.14", the GIL build
    Fedora's python3 is, so no free-threaded "t" ABI) or pure Python, on manylinux x86_64 or
    any platform."""
    m = re.match(r"^(.+?)-(.+?)(?:-\d[^-]*)?-([^-]+)-([^-]+)-([^-]+)\.whl$", filename)
    if not m:
        return False
    pys, abis, plats = (set(t.split(".")) for t in m.group(3, 4, 5))
    major, minor = (int(x) for x in py.split("."))
    cp = f"cp{major}{minor}"
    if not ("any" in plats or any(p.startswith("manylinux") and p.endswith("_x86_64") for p in plats)):
        return False
    if cp in pys and abis & {cp, "abi3", "none"}:
        return True
    stable = {int(t[3:]) for t in pys if re.fullmatch(rf"cp{major}\d+", t)}
    if "abi3" in abis and any(n <= minor for n in stable):
        return True
    return bool(pys & {f"py{major}", f"py{major}{minor}"}) and "none" in abis


def wheel_files(name, version, py, cutoff, get=fetch):
    """[(filename, sha256, upload datetime)] for `name==version`, fitting wheels uploaded before cutoff."""
    meta = json.loads(get(f"https://pypi.org/pypi/{name}/{version}/json"))
    files = []
    for u in meta.get("urls", []):
        up = parse_time(u["upload_time_iso_8601"])
        if (u.get("packagetype") == "bdist_wheel" and not u.get("yanked") and up < cutoff
                and wheel_fits(u["filename"], py)):
            files.append((u["filename"], u["digests"]["sha256"], up))
    if not files:
        raise PinError(f"{name}=={version}: no Linux x86_64 or pure wheel for Python {py} uploaded before {iso(cutoff)}")
    return sorted(files)


def resolve(inputs, py, cutoff, system, no_deps=False):
    """uv's resolution of `inputs` at the cutoff: [(name, version)], system packages and holds applied."""
    with tempfile.TemporaryDirectory(prefix="update-pins-") as d:
        req, con = os.path.join(d, "requirements.in"), os.path.join(d, "constraints.txt")
        with open(req, "w") as f:
            f.write("".join(f"{x}\n" for x in inputs))
        with open(con, "w") as f:
            f.write("".join(f"{p}=={v}\n" for p, v in sorted(system.items())) + "".join(f"{h}\n" for h in HOLDS))
        out = run_uv(["pip", "compile", req, "--constraint", con, "--no-config", "--quiet", "--no-header",
                      "--no-annotate", "--default-index", INDEX, "--exclude-newer", iso(cutoff),
                      "--python-version", py, "--python-platform", "x86_64-manylinux_2_28",
                      "--only-binary", ":all:", *(["--no-deps"] if no_deps else []), *(a for p in system for a in ("--no-emit-package", p))])
    pins = []
    for line in out.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)", line)
            if not m:
                raise PinError(f"unexpected uv output line {line!r}")
            pins.append(m.groups())
    return pins


def render_requirements(spec, pins, py, cutoff, system, get=fetch):
    hold = [f"{h} ({why})" for h, why in HOLDS.items()
            if any(h.startswith(n) for n, _ in pins)]
    out = [f"# {spec['about']}",
           "# Generated by scripts/update-pins.py; don't hand-edit (CONTRIBUTING.md, Pins).",
           f"# Cutoff {iso(cutoff)}: every file was uploaded at least 7 days before the run;",
           f"# pre-releases skipped. Linux x86_64 and pure wheels for CPython {py} only. Resolved",
           "# against the system's " + ", ".join(f"{p} {v}" for p, v in sorted(system.items()))
           + " (Fedora, reused by the venv, not listed).",
           *(f"# Held: {h}." for h in hold)]
    for name, version in pins:
        files = wheel_files(name, version, py, cutoff, get)
        note = spec["notes"].get(name)
        out.append(f"# uploaded {max(f[2] for f in files).date().isoformat()}" + (f"; {note}" if note else ""))
        out.append(f"{name}=={version} \\")
        out += [f"    --hash=sha256:{sha}" + (" \\" if i < len(files) - 1 else "") for i, (_, sha, _) in enumerate(files)]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- models
def update_siglip(pin, cutoff, get=fetch, log=print):
    """pin: pins.json's "siglip" entry. Returns the new entry (the same object if nothing moved)."""
    repo = pin["repo"]
    commits = json.loads(get(f"https://huggingface.co/api/models/{repo}/commits/main"))
    old = [c for c in commits if parse_time(c["date"]) < cutoff]
    if not old:
        raise PinError(f"{repo}: no commit on main older than {iso(cutoff)}")
    newest = old[0]
    if newest["id"] == pin["revision"]:
        log(f"siglip   {repo} at {pin['revision'][:12]}: current")
        return pin
    tree = {e["path"]: e for e in json.loads(get(f"https://huggingface.co/api/models/{repo}/tree/{newest['id']}?recursive=true"))}
    files = {}
    for path in pin["files"]:
        e = tree.get(path)
        if e is None:
            raise PinError(f"{repo}@{newest['id'][:12]}: {path} is gone; choose a new file deliberately")
        if e.get("lfs"):
            files[path] = e["lfs"]["oid"]
        else:
            body = get(f"https://huggingface.co/{repo}/resolve/{newest['id']}/{path}")
            if len(body) > NON_LFS_MAX:
                raise PinError(f"{path}: non-LFS file over {NON_LFS_MAX >> 20} MB; not downloading it to hash")
            files[path] = hashlib.sha256(body).hexdigest()
    if files == pin["files"]:
        log(f"siglip   {repo}: {newest['id'][:12]} changes none of the pinned files; kept {pin['revision'][:12]}")
        return pin
    log(f"siglip   {repo}: {pin['revision'][:12]} -> {newest['id'][:12]} ({newest['date'][:10]}). "
        "Then: bash setup.sh --build-labels (labels.npz), and check label.py's LOGIT_SCALE/LOGIT_BIAS "
        "and preprocessing against the new revision")
    return {**pin, "revision": newest["id"], "revision_date": newest["date"][:10], "files": files}


def update_ollama(pin, cutoff, today, get=fetch, log=print):
    """pin: pins.json's "ollama" entry. Returns the new entry (the same object if nothing changed)."""
    name, _, tag = pin["model"].partition(":")
    body = get(f"https://registry.ollama.ai/v2/library/{name}/manifests/{tag or 'latest'}",
               accept="application/vnd.docker.distribution.manifest.v2+json")
    digest = hashlib.sha256(body).hexdigest()
    cand = pin.get("candidate")
    if digest == pin["digest"]:
        log(f"ollama   {pin['model']} {digest[:12]}: current")
        return pin if cand is None else {**pin, "candidate": None}
    if cand and cand["digest"] == digest:
        seen = datetime.datetime.fromisoformat(cand["first_seen"]).replace(tzinfo=datetime.timezone.utc)
        if seen < cutoff:
            log(f"ollama   {pin['model']}: {pin['digest'][:12]} -> {digest[:12]} (first seen {cand['first_seen']}). "
                f"Then: ollama pull {pin['model']}; vlm-trash re-judges files (their progress names the old digest)")
            return {**pin, "digest": digest, "candidate": None}
        log(f"ollama   {pin['model']}: candidate {digest[:12]} first seen {cand['first_seen']}; pinned after 7 days")
        return pin
    log(f"ollama   {pin['model']}: new registry digest {digest[:12]}, recorded as the candidate (first seen {today})")
    return {**pin, "candidate": {"digest": digest, "first_seen": today}}


# ---------------------------------------------------------------- main
def parse_time(s):
    t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def iso(t):
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(only, cutoff, today, py, get=fetch, log=print):
    """{path: new text} for every file this run regenerates."""
    out = {}
    if only in (None, "requirements"):
        if uv_version() < MIN_UV:
            raise PinError(f"uv >= {'.'.join(map(str, MIN_UV))} needed (exclude-newer cooldowns)")
        system = system_versions()
        for fname, spec in REQUIREMENTS.items():
            pins = resolve(spec["inputs"], py, cutoff, system, spec["no_deps"])
            out[os.path.join(ROOT, fname)] = render_requirements(spec, pins, py, cutoff, system, get)
    if only in (None, "models"):
        with open(PINS) as f:
            pins = json.load(f)
        pins["siglip"] = update_siglip(pins["siglip"], cutoff, get, log)
        pins["ollama"] = update_ollama(pins["ollama"], cutoff, today, get, log)
        out[PINS] = json.dumps(pins, indent=2) + "\n"
    return out


def pins_of(text):
    """The lines that pin something: a requirements file whose comments alone differ (a newer
    cutoff, nothing newer to pin) is left as it is, so --check means "something to bump"."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="print the diff, write nothing, exit 1 if anything changes")
    ap.add_argument("--cutoff", help="RFC 3339 UTC time (default: now - 7 days); never later than that")
    ap.add_argument("--only", choices=("requirements", "models"))
    a = ap.parse_args(argv)
    now = datetime.datetime.now(datetime.timezone.utc)
    latest = now - MIN_AGE
    cutoff = parse_time(a.cutoff) if a.cutoff else latest
    if cutoff > latest:
        sys.exit(f"update-pins: --cutoff {iso(cutoff)} is less than 7 days ago")
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        new = plan(a.only, cutoff, now.date().isoformat(), py)
    except PinError as e:
        sys.exit(f"update-pins: {e}")
    changed = 0
    for path, text in new.items():
        try:
            with open(path) as f:
                cur = f.read()
        except FileNotFoundError:
            cur = ""
        if cur == text or (path != PINS and pins_of(cur) == pins_of(text)):
            continue
        changed += 1
        rel = os.path.relpath(path, ROOT)
        if a.check:
            sys.stdout.writelines(difflib.unified_diff(cur.splitlines(True), text.splitlines(True), rel, rel + " (new)"))
        else:
            with open(path + ".tmp", "w") as f:
                f.write(text)
            os.replace(path + ".tmp", path)
            print(f"wrote    {rel}")
    print(f"update-pins: cutoff {iso(cutoff)}, {changed} file(s) {'would change' if a.check else 'changed'}")
    return 1 if a.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
