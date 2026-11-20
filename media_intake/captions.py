#!/usr/bin/env python3
"""Local captions: a short description of each photo from the local vision model, for `search --text`.

  media-intake captions FILE...                 caption these library photos
  media-intake captions --all [--inhibit]       every library photo (overnight; resumable)
  media-intake captions --all --dry-run         how many an overnight run would caption
  options: --redo (caption again), --host http://127.0.0.1:PORT, --allow-cpu

Off by default: nothing else runs it (not intake, the queue or tag). It uses vlm-trash's client as is
(vlm_trash.py): Ollama on 127.0.0.1 only (any other host refused, proxies bypassed), the model pinned by its
digest in pins.json, the same downscaled in-memory JPEG, the GPU check after the first photo, and keep_alive 0
on the last request. Photos only; videos are not captioned.

The caption goes in the photo's XMP sidecar, in this tool's own XMP namespace (never a field you or another
program writes, never a keyword): XMP-mediaintake:Caption, plus XMP-mediaintake:CaptionModel naming the model.
The model must answer {"caption": "..."}; the text is kept on one line, without control characters, at most
MAX_CHARS characters; anything else (malformed, empty, a truncated prompt, a network error) writes nothing.
The prompt asks it not to copy out text, numbers or names it sees. `search --text` matches the caption's words
as it matches a photo's text (semantic.py cmd_search_text, search() below).

Refused, never opened or sent: anything under the state's quarantine/ or batches/, any file whose size+sha256
scan flagged, quarantined or erased (label.refused()), and anything outside the library, videos and project
folders. An explicit list with a refused file exits 3, sending nothing.

Resumable: <state>/captions-progress.json (0600) records path, size+mtime and model digest (never a caption),
saved with the sidecars every CHUNK photos; a photo is skipped when that record matches and its sidecar holds
this model's caption. Sidecars are written under the library lock. A caption lives only in its sidecar, which
erase, forget and trash delete with the file; the progress file drops paths that are gone on each save.
Output is counts only: progress lines and a summary on stderr, the counts as one JSON line on stdout.
"""
import argparse, collections, json, os, re, shutil, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

NS_URI = "http://ns.media-intake.invalid/captions/1.0/"  # .invalid: a name only, never fetched
GROUP = "XMP-mediaintake"
MAX_CHARS = 200
CHUNK = 25
PROMPT = ("Describe this photo in one short sentence of at most 20 words: what or who is in it, what is "
          "happening, and the kind of place. Plain words only. Do not copy out any text, numbers, names or "
          'addresses you can read in it. Answer only JSON: {"caption": "..."}.')
SCHEMA = {"type": "object", "required": ["caption"], "properties": {"caption": {"type": "string"}}}
# exiftool learns this tool's namespace from a config file (-config must be its first argument)
EXIFTOOL_CONFIG = f"""%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {{
        mediaintake => {{ SubDirectory => {{ TagTable => 'Image::ExifTool::UserDefined::mediaintake' }} }},
    }},
);
%Image::ExifTool::UserDefined::mediaintake = (
    GROUPS => {{ 0 => 'XMP', 1 => '{GROUP}', 2 => 'Image' }},
    NAMESPACE => {{ 'mediaintake' => '{NS_URI}' }},
    WRITABLE => 'string',
    Caption => {{ }},
    CaptionModel => {{ }},
);
1;
"""
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text):
    """One line, no control characters, whitespace collapsed, at most MAX_CHARS (cut at a word); '' if none."""
    if not isinstance(text, str):
        return ""
    t = " ".join(_CTRL.sub(" ", text).split())
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS].rsplit(" ", 1)[0] if " " in t[:MAX_CHARS] else t[:MAX_CHARS]
    return t.strip()


# ---------------------------------------------------------------- reading (stdlib only: search uses it)
def read(path):
    """{"caption", "model"} from a media file's sidecar (path + ".xmp"); {} when it has none or can't be read."""
    x = path + ".xmp"
    try:
        with open(x, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    if NS_URI.encode() not in raw:  # most sidecars: no caption, no XML parse
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    out = {}
    want = {f"{{{NS_URI}}}Caption": "caption", f"{{{NS_URI}}}CaptionModel": "model"}
    for el in root.iter():
        for k, v in el.attrib.items():
            if k in want:
                out[want[k]] = v
        if el.tag in want:
            out[want[el.tag]] = "".join(el.itertext())
    return {k: clean(v) for k, v in out.items() if clean(v)}


def search(query, paths):
    """{path: hits} of the paths whose caption holds every word of query, matched as search --text matches a
    photo's text (ocr_index.words: no case, from the start of a word); hits is how often the words appear."""
    import ocr_index
    ws = ocr_index.words(query)
    if not ws:
        return {}
    rxs = [re.compile(r"\b" + re.escape(w)) for w in ws]
    out = {}
    for p in paths:
        c = read(p).get("caption")
        if not c:
            continue
        low = c.casefold()
        n = [len(rx.findall(low)) for rx in rxs]
        if all(n):
            out[p] = sum(n)
    return out


# ---------------------------------------------------------------- the model (vlm_trash's client)
BAD = None


def parse(resp, num_ctx=None):
    """The caption from an /api/generate reply, or None when anything is off (vlm_trash.parse's checks)."""
    import vlm_trash
    num_ctx = num_ctx or vlm_trash.NUM_CTX
    if not isinstance(resp, dict):
        return BAD
    n = resp.get("prompt_eval_count")
    if not isinstance(n, int) or isinstance(n, bool) or n >= num_ctx - 64:
        return BAD
    text = resp.get("response") or resp.get("thinking") or ""
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return BAD
    if not isinstance(v, dict) or set(v) != {"caption"}:
        return BAD
    return clean(v["caption"]) or BAD


def ask(host, jpeg):
    import base64, urllib.error, vlm_trash
    body = {"model": vlm_trash.MODEL, "prompt": PROMPT, "images": [base64.b64encode(jpeg).decode()],
            "stream": False, "format": SCHEMA, "keep_alive": vlm_trash.KEEP_ALIVE, "think": False,
            "options": {"temperature": 0, "num_ctx": vlm_trash.NUM_CTX, "seed": 0}}
    try:
        return parse(vlm_trash._call(host, "/api/generate", body))
    except (OSError, ValueError, urllib.error.URLError):
        return BAD


# ---------------------------------------------------------------- writing, progress
def write(items, state):
    """items: [(path, caption)]. Sidecars only, under the library lock; only this tool's two fields are set.
    A file that went away meanwhile gets no sidecar."""
    import library_lock
    try:
        with library_lock.hold("captions.py", wait=library_lock.cli_wait(), out=sys.stderr):
            _write([it for it in items if os.path.lexists(it[0])], state)
    except library_lock.Busy as e:
        sys.exit(f"captions: refused: {e}")


def _write(items, state):
    import vlm_trash
    blocks = []
    for path, caption in items:
        x = path + ".xmp"
        if os.path.splitext(path)[1].lower() == ".xmp" or "\n" in x or "\n" in caption:
            raise vlm_trash.Refused(f"refusing to write a non-sidecar: {x}")
        if not os.path.exists(x):
            with open(x, "w", encoding="utf-8") as f:
                f.write(vlm_trash.EMPTY_XMP)
        blocks += ["-q", "-m", "-overwrite_original", f"-{GROUP}:Caption={caption}",
                   f"-{GROUP}:CaptionModel={vlm_trash.MODEL}", x, "-execute"]
    if not blocks:
        return
    work = os.path.join(os.path.expanduser("~"), ".cache", "media-intake")
    os.makedirs(work, mode=0o700, exist_ok=True)
    fd, cfg = tempfile.mkstemp(dir=work, suffix=".config")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(EXIFTOOL_CONFIG)
        r = subprocess.run(["exiftool", "-config", cfg, "-@", "-"],
                           input="\n".join(blocks) + "\n", capture_output=True, text=True)
    finally:
        os.unlink(cfg)
    if r.returncode:
        sys.exit(f"captions: exiftool failed writing sidecars ({r.returncode})")


def progress_path(state):
    return os.path.join(state, "captions-progress.json")


def load_progress(state):
    try:
        with open(progress_path(state)) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_progress(prog, state):
    """Paths whose file is gone are dropped (erase, forget, trash, a move)."""
    prog = {p: e for p, e in prog.items() if os.path.lexists(p)}
    os.makedirs(state, mode=0o700, exist_ok=True)
    tmp = progress_path(state) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(prog, f)
    os.replace(tmp, progress_path(state))
    return prog


def done_already(p, prog):
    import vlm_trash
    e = prog.get(p)
    if not (isinstance(e, dict) and e.get("digest") == vlm_trash.DIGEST and e.get("stamp") == vlm_trash.stamp(p)):
        return False
    r = read(p)
    return bool(r.get("caption")) and r.get("model") == vlm_trash.MODEL


# ---------------------------------------------------------------- main
def build_parser():
    ap = argparse.ArgumentParser(prog="media-intake captions", description=__doc__.split("\n")[0],
                                 epilog="Off by default; docs/search.md, \"Local captions\".")
    ap.add_argument("files", nargs="*", help="library photos to caption")
    ap.add_argument("--all", action="store_true", help="every library photo (resumable; skips captioned ones)")
    ap.add_argument("--redo", action="store_true", help="caption photos this model already captioned")
    ap.add_argument("--host", default="http://127.0.0.1:11434", help="the local Ollama (127.0.0.1 only)")
    ap.add_argument("--inhibit", action="store_true", help="hold a sleep/idle inhibitor while running")
    ap.add_argument("--dry-run", action="store_true", help="count what would be captioned, send nothing")
    ap.add_argument("--allow-cpu", action="store_true",
                    help="keep going when Ollama put the model partly on the CPU (much slower, and RAM-hungry)")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.inhibit:
        if not shutil.which("systemd-inhibit"):
            sys.exit("captions: systemd-inhibit not found")
        args = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--inhibit"]
        os.execvp("systemd-inhibit", ["systemd-inhibit", "--what=sleep:idle", "--who=media-intake",
                                      "--why=media-intake overnight captions", sys.executable, "-I",
                                      os.path.abspath(__file__), *args])
    if a.all == bool(a.files):
        print("captions: name library photos, or --all for every one (off by default: nothing runs it for you)",
              file=sys.stderr)
        return 2
    import label, meter, vlm_trash
    try:
        host = vlm_trash.check_host(a.host)
    except vlm_trash.Refused as e:
        print(f"captions: {e}", file=sys.stderr)
        return 2
    state = label.STATE
    img = vlm_trash.IMG
    counts = collections.Counter()
    if a.files:
        files = [os.path.abspath(f) for f in a.files]
        bad = [f for f in files if not os.path.isfile(f) or not vlm_trash.under_roots(f)]
        bad += label.refused([f for f in files if f not in bad], state)
        if bad:
            print(f"captions: refusing {len(bad)} files (not library files, or quarantined/flagged/staging); "
                  "nothing sent", file=sys.stderr)
            return 3
        counts["not-photo"] = sum(os.path.splitext(f)[1].lower() not in img for f in files)
    else:
        files = vlm_trash.media_files()
        bad = set(label.refused(files, state))
        counts["refused"] = len(bad)
        files = [f for f in files if f not in bad and vlm_trash.under_roots(f)]
    files = [f for f in files if os.path.splitext(f)[1].lower() in img]  # photos only
    unwritable = {f for f in files if not vlm_trash.sidecar_writable(f)}
    counts["unwritable"] = len(unwritable)
    files = [f for f in files if f not in unwritable]
    prog = load_progress(state)
    todo = files if a.redo else [f for f in files if not done_already(f, prog)]
    counts["already"] = len(files) - len(todo)
    counts["todo"] = len(todo)
    if a.dry_run or not todo:
        print(json.dumps(dict(counts)))
        print(summary(counts, dry_run=a.dry_run), file=sys.stderr)
        return 0
    if not shutil.which("exiftool"):
        sys.exit("captions: exiftool missing")
    try:
        vlm_trash.check_digest(host)
    except vlm_trash.Refused as e:
        print(f"captions: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"captions: Ollama not reachable on {host} ({type(e).__name__})", file=sys.stderr)
        return 2

    t0, pending = time.monotonic(), []
    m = meter.Meter("captions", len(todo))
    m.say(f"captions: {len(todo):,} photos to caption")

    def flush():
        nonlocal prog
        write(pending, state)
        for p2, _ in pending:
            prog[p2] = {"digest": vlm_trash.DIGEST, "stamp": vlm_trash.stamp(p2)}
        prog = save_progress(prog, state)
        pending.clear()

    try:
        for i, p in enumerate(todo, 1):
            j = vlm_trash.image_jpeg(p)
            cap = None if j is None else ask(host, j)
            if j is None:
                counts["undecodable"] += 1
            elif cap is None:
                counts["no-caption"] += 1
            else:
                counts["captioned"] += 1
                pending.append((p, cap))
            if i == 1 and not a.allow_cpu and j is not None:
                frac = vlm_trash.on_gpu(host)
                if frac is not None and frac < 0.9:
                    print(f"captions: Ollama loaded {vlm_trash.MODEL} {frac:.0%} on the GPU (the rest on CPU: very "
                          "slow, and it holds most of the RAM); stopping. --allow-cpu to run anyway", file=sys.stderr)
                    counts["stopped-not-on-gpu"] = 1
                    break
            m.update(i)
            if len(pending) >= CHUNK:
                flush()
    finally:
        m.close()
        if pending:  # the end, or interrupted: keep what was captioned
            flush()
        vlm_trash.unload(host)  # keep_alive 0: frees the GPU
    counts["seconds"] = round(time.monotonic() - t0, 1)
    print(json.dumps(dict(counts)))
    m.say(summary(counts))
    return 0


def summary(c, dry_run=False):
    """One counts-only line for a person; the JSON on stdout stays the machine form."""
    import meter
    skipped = c["already"] + c["refused"] + c["unwritable"]
    why = ", ".join(f"{c[k]:,} {name}" for k, name in (("already", "already captioned"), ("refused", "refused"),
                                                        ("unwritable", "sidecar not writable")) if c[k])
    skip = f"skipped {skipped:,}" + (f" ({why})" if why else "")
    if dry_run:
        return f"captions: would caption {c['todo']:,}, {skip}; nothing sent (--dry-run)"
    if not c["todo"]:
        return f"captions: nothing to caption, {skip}"
    out = f"captions: captioned {c['captioned']:,}, {skip}"
    out += f"; {c['no-caption']:,} without a usable answer (tried again next run)" if c["no-caption"] else ""
    out += f"; {c['undecodable']:,} undecodable" if c["undecodable"] else ""
    if c["stopped-not-on-gpu"]:
        out += "; stopped: the model is not fully on the GPU (--allow-cpu to run anyway)"
    if "seconds" in c:
        out += f"; took {meter.duration(c['seconds'])}"
    return out


if __name__ == "__main__":
    sys.exit(main())
