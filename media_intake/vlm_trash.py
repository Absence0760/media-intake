#!/usr/bin/env python3
"""Second opinion on trash candidates from a local vision model (Ollama, qwen3-vl:2b, Apache-2.0).

  venv/bin/python -I media_intake/vlm_trash.py                 files tagged Review|maybe-trash
  venv/bin/python -I media_intake/vlm_trash.py FILE...         an explicit list
  venv/bin/python -I media_intake/vlm_trash.py --all [--inhibit]   the whole library (overnight)
  options: --redo (judge again), --host http://127.0.0.1:PORT, --dry-run (count only)

Each photo is downscaled in memory to at most 768 px, JPEG-encoded and sent to
Ollama on 127.0.0.1 only (any other host is refused, proxies are bypassed).
The model answers {"verdict", "reason", "confidence"}; anything malformed, a
reason outside REASONS, a confidence outside 0..1 or a truncated prompt counts
as KEEP with no confidence. Tags go only into this tool's own namespace in the
file's XMP sidecar (AI never overrides a tag):
  AI|vlm/qwen3-vl-2b/trash/<reason>  or  AI|vlm/qwen3-vl-2b/keep
  AI|vlm/qwen3-vl-2b/conf/<0.0-1.0>  (the model's confidence, one decimal)
The raw verdict is written; consumers (suggest-trash) take trash with conf >= 0.7.
A re-run replaces only tags under AI|vlm/<this model>/; another model writes
beside them; no other keyword is added or removed. Videos: 3 sampled frames,
trash only when at least 2 of the 3 say TRASH >= 0.7 (conf: the lowest of those).
Skipped, never sent or tagged: files whose sidecar has Owner|not-trash,
Owner|keep, Owner|trash, legacy Review|not-trash or a digiKam pick label. Files tagged
Used|* or a protected project's Project|<name> (config.toml) are not sent either and get .../keep (never trash).
Nothing else is stored: no description, no model text. Output is counts only.

Refused, never sent: anything under the state's quarantine/ or batches/
(staging), any file whose size+sha256 scan flagged, quarantined or erased
(label.refused()), and anything outside the library, videos and project folders (config.toml). An explicit list with a refused file exits 3.

The model is pinned by its local digest; a different digest refuses to run.
Progress (path, size, mtime, digest, verdict) goes to vlm-progress.json in the
state dir every CHUNK files, so a killed run continues where it stopped. The
last request uses keep_alive 0, which unloads the model from the GPU.
"""
import argparse, base64, collections, datetime, io, json, os, shutil, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import label  # noqa: E402  (read-only: refused(), STATE, IMG/VID)

HOME = os.path.expanduser("~")
STATE = label.STATE
import config  # noqa: E402
import library_lock  # noqa: E402  (stdlib only)
ROOTS = config.library_roots()
PROTECTED = config.protected_tags()
IMG = label.IMG | {".dng"}
VID = label.VID

# qwen3-vl:2b = Qwen3-VL-2B-Thinking Q4_K_M, Apache-2.0. Not qwen2.5vl:7b (too big for an 8 GB GPU),
# not qwen2.5vl:3b (Qwen Research License: non-commercial). Ollama 0.21.2 can't pull the newer
# qwen3-vl:*-instruct / gemma3 manifests (registry answers 412). docs/search.md.
MODEL = label.PINS["ollama"]["model"]    # media_intake/pins.json
DIGEST = label.PINS["ollama"]["digest"]  # `ollama list` shows its first 12 hex digits as the ID
HOST = "http://127.0.0.1:11434"
NUM_CTX = 8192          # Ollama's 4096 default truncates silently
KEEP_ALIVE = "2m"       # between requests; the last one sends 0
MAX_PX = 768            # qwen3-vl: one token per 32x32 px (24*24 = 576); the fit check below assumes 28 px
TRASH_MIN = 0.7
CHUNK = 25
VIDEO_FRAMES = 3
REASONS = ["blurry", "accidental", "dark", "packaging", "random-object", "screenshot-junk", "duplicate-ish", "other"]
PROMPT = (
    "You help clean up a personal photo library. Decide whether this photo is worth keeping. "
    "TRASH means clearly junk: badly blurred, an accidental shot (pocket, floor, finger over the lens), "
    "almost black, a picture of packaging or a receipt-like throwaway, a random object with no subject, "
    "a junk screenshot, or a near-copy of a better shot. Anything with people, places, events, pets, "
    "documents someone may need, or any doubt is KEEP. "
    'Answer only JSON: {"verdict": "TRASH" or "KEEP", "reason": one of '
    + ", ".join(f'"{r}"' for r in REASONS) + ', "confidence": a number from 0 to 1}.')
SCHEMA = {"type": "object", "required": ["verdict", "reason", "confidence"],
          "properties": {"verdict": {"type": "string", "enum": ["TRASH", "KEEP"]},
                         "reason": {"type": "string", "enum": REASONS},
                         "confidence": {"type": "number"}}}
EMPTY_XMP = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
             "<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
             "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
             "</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")
NS = "AI|vlm/" + MODEL.replace(":", "-") + "/"   # AI|vlm/qwen3-vl-2b/
NOT_TRASH = {"Owner|not-trash", "Review|not-trash"}  # Review|not-trash: legacy spelling
OWNER_SKIP = NOT_TRASH | {"Owner|keep", "Owner|trash"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy


class Refused(Exception):
    pass


# ---------------------------------------------------------------- Ollama (loopback only)
def check_host(host):
    u = urllib.parse.urlsplit(host)
    if u.scheme != "http" or u.hostname != "127.0.0.1" or u.username or u.password or u.path not in ("", "/"):
        raise Refused(f"Ollama host must be http://127.0.0.1[:port], got {host!r}; images never leave this machine")
    return f"http://127.0.0.1:{u.port or 11434}"


def _call(host, path, body=None, timeout=600):
    req = urllib.request.Request(host + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def check_digest(host):
    tags = _call(host, "/api/tags", timeout=30)
    for m in tags.get("models", []):
        if m.get("name") == MODEL or m.get("model") == MODEL:
            if m.get("digest") != DIGEST:
                raise Refused(f"{MODEL} digest is {str(m.get('digest'))[:12]}, pinned {DIGEST[:12]}; "
                              "re-check the model and update media_intake/pins.json with scripts/update-pins.py")
            return
    raise Refused(f"{MODEL} not installed in the local Ollama")


def on_gpu(host):
    """Fraction of the loaded model in VRAM (None if not loaded)."""
    for m in _call(host, "/api/ps", timeout=30).get("models", []):
        if m.get("name") == MODEL or m.get("model") == MODEL:
            return (m.get("size_vram") or 0) / max(1, m.get("size") or 1)
    return None


def unload(host):
    try:
        _call(host, "/api/generate", {"model": MODEL, "keep_alive": 0}, timeout=60)
    except (OSError, ValueError):
        pass


BAD = ("KEEP", None, None)


def parse(resp, num_ctx=NUM_CTX):
    """(verdict, reason, confidence) from an /api/generate reply; anything off -> ("KEEP", None, None)."""
    if not isinstance(resp, dict):
        return BAD
    n = resp.get("prompt_eval_count")
    if not isinstance(n, int) or n >= num_ctx - 64:  # missing count or prompt may be truncated
        return BAD
    # A thinking model under a `format` schema: Ollama 0.21.2 returns the constrained JSON in
    # "thinking" and an empty "response". Same strict shape check either way.
    text = resp.get("response") or resp.get("thinking") or ""
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return BAD
    if not isinstance(v, dict) or set(v) != {"verdict", "reason", "confidence"}:
        return BAD
    verdict, reason, conf = v["verdict"], v["reason"], v["confidence"]
    if verdict not in ("TRASH", "KEEP") or reason not in REASONS:
        return BAD
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return BAD
    return verdict, reason if verdict == "TRASH" else None, float(conf)


def ask(host, jpeg):
    body = {"model": MODEL, "prompt": PROMPT, "images": [base64.b64encode(jpeg).decode()], "stream": False,
            "format": SCHEMA, "keep_alive": KEEP_ALIVE, "think": False,
            "options": {"temperature": 0, "num_ctx": NUM_CTX, "seed": 0}}
    try:
        return parse(_call(host, "/api/generate", body))
    except (OSError, ValueError, urllib.error.URLError):
        return BAD


# ---------------------------------------------------------------- images
def _pil():
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass
    return Image, ImageOps


def jpeg_bytes(im):
    """RGB, at most MAX_PX on the long side, JPEG in memory; nothing written to disk."""
    Image, ImageOps = _pil()
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((MAX_PX, MAX_PX), Image.BICUBIC)
    w, h = im.size
    if -(-w // 28) * -(-h // 28) + 400 > NUM_CTX:  # image tokens + prompt must fit the context
        raise ValueError("image does not fit num_ctx")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def image_jpeg(path):
    Image, _ = _pil()
    try:
        with Image.open(path) as im:
            im.draft("RGB", (MAX_PX * 2, MAX_PX * 2))
            return jpeg_bytes(im)
    except Exception:
        return None


def video_jpegs(path):
    import scan_sensitive
    Image, _ = _pil()
    work = os.path.join(HOME, ".cache", "media-intake")
    os.makedirs(work, mode=0o700, exist_ok=True)
    d = tempfile.mkdtemp(dir=work)
    try:
        out = []
        for n in scan_sensitive.video_frames(path, d, max_frames=VIDEO_FRAMES)[:VIDEO_FRAMES]:
            with Image.open(os.path.join(d, n)) as im:
                out.append(jpeg_bytes(im))
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def judge(host, path):
    """(verdict, reason, confidence) or None when the file can't be decoded."""
    if os.path.splitext(path)[1].lower() in VID:
        frames = video_jpegs(path)
        if not frames:
            return None
        votes = [ask(host, j) for j in frames]
        trash = [(r, c) for v, r, c in votes if v == "TRASH" and c >= TRASH_MIN]
        if len(trash) * 2 > len(votes):  # majority (2 of 3)
            return "TRASH", collections.Counter(r for r, _ in trash).most_common(1)[0][0], min(c for _, c in trash)
        keep = [c for v, _, c in votes if v == "KEEP" and c is not None]
        return "KEEP", None, min(keep) if keep else None
    j = image_jpeg(path)
    return None if j is None else ask(host, j)


# ---------------------------------------------------------------- files, sidecars, progress
def under_roots(p):
    rp = os.path.realpath(p)
    return any(rp.startswith(os.path.realpath(r) + os.sep) for r in ROOTS)


def media_files():
    out = []
    for r in ROOTS:
        if not os.path.isdir(r):
            continue
        for d, dirs, names in os.walk(r):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            out += [os.path.join(d, n) for n in sorted(names)
                    if os.path.splitext(n)[1].lower() in IMG | VID and not os.path.islink(os.path.join(d, n))]
    return out


def read_tags(paths):
    """{media path: [HierarchicalSubject...]} for those with a sidecar; a digiKam pick label
    shows up as "digiKam|pick/<n>"."""
    xs = [p + ".xmp" for p in paths if os.path.isfile(p + ".xmp")]
    if not xs:
        return {}
    r = subprocess.run(["exiftool", "-j", "-n", "-q", "-q", "-HierarchicalSubject", "-XMP-digiKam:PickLabel",
                        "-@", "-"], input="\n".join(xs) + "\n", capture_output=True, text=True)
    out = {}
    for row in json.loads(r.stdout or "[]"):
        v = row.get("HierarchicalSubject")
        t = [] if v is None else [str(x) for x in (v if isinstance(v, list) else [v])]
        if row.get("PickLabel") not in (None, 0, "0", ""):
            t.append(f"digiKam|pick/{row['PickLabel']}")
        out[row["SourceFile"][:-4]] = t
    return out


def owner_decided(t):
    return any(x in OWNER_SKIP or x.startswith("digiKam|pick/") for x in t)


def protected(t):
    """Used in a post or in a protected project: never a trash verdict."""
    return any(x.startswith("Used|") or x in PROTECTED for x in t)


def candidates():
    roots = [r for r in ROOTS if os.path.isdir(r)]
    if not roots:
        return []
    r = subprocess.run(["exiftool", "-j", "-q", "-q", "-r", "-ext", "xmp", "-HierarchicalSubject", *roots],
                       capture_output=True, text=True)
    out = []
    for row in json.loads(r.stdout or "[]"):
        v = row.get("HierarchicalSubject")
        tags = [] if v is None else [str(x) for x in (v if isinstance(v, list) else [v])]
        media = row["SourceFile"][:-4]
        if "Review|maybe-trash" in tags and not NOT_TRASH & set(tags) and os.path.isfile(media):
            out.append(media)
    return sorted(out)


def verdict_tags(verdict, reason, conf):
    out = [NS + ("trash/" + reason if verdict == "TRASH" else "keep")]
    if conf is not None:
        out.append(NS + f"conf/{round(conf, 1):.1f}")
    return out


def write_verdicts(items, tags):
    """items: [(path, verdict, reason, conf)]. Sidecar only. Removes this model's previous
    AI|vlm/<model>/ tags, adds the new ones; no other keyword is touched. Under the library lock
    (library_lock.py), so a gallery erase or a CLI apply never runs in between; a file that went
    away while it was judged gets no sidecar."""
    try:
        with library_lock.hold("vlm_trash.py", wait=library_lock.cli_wait(), out=sys.stderr):
            _write_verdicts([it for it in items if os.path.lexists(it[0])], tags)
    except library_lock.Busy as e:
        sys.exit(f"vlm_trash: refused: {e}")


def _write_verdicts(items, tags):
    blocks = []
    for path, verdict, reason, conf in items:
        x = path + ".xmp"
        if os.path.splitext(path)[1].lower() == ".xmp" or "\n" in x:
            raise Refused(f"refusing to write a non-sidecar: {x}")
        if not os.path.exists(x):
            with open(x, "w", encoding="utf-8") as f:
                f.write(EMPTY_XMP)
        new = verdict_tags(verdict, reason, conf)
        b = ["-q", "-m", "-overwrite_original"]
        for old in [t for t in tags.get(path, []) if t.startswith(NS)] + new:
            if not old.startswith(NS):
                raise Refused(f"refusing to touch a tag outside {NS}")
            b += [f"-XMP-lr:HierarchicalSubject-={old}", f"-XMP-dc:Subject-={old.split('|')[-1]}"]
        for t in new:
            b += [f"-XMP-lr:HierarchicalSubject+={t}", f"-XMP-dc:Subject+={t.split('|')[-1]}"]
        blocks += b + [x, "-execute"]
    if not blocks:
        return
    r = subprocess.run(["exiftool", "-@", "-"], input="\n".join(blocks) + "\n", capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"vlm_trash: exiftool failed writing sidecars ({r.returncode})")


def progress_path():
    return os.path.join(STATE, "vlm-progress.json")


def load_progress():
    try:
        with open(progress_path()) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_progress(prog):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    tmp = progress_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(prog, f)
    os.replace(tmp, progress_path())


def stamp(p):
    st = os.stat(p)
    return [st.st_size, st.st_mtime_ns]


def done_already(p, prog, tags):
    e = prog.get(p)
    return bool(e and e.get("digest") == DIGEST and e.get("stamp") == stamp(p)
                and any(t == NS + "keep" or t.startswith(NS + "trash/") for t in tags.get(p, [])))


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true", help="every library photo and video")
    ap.add_argument("--redo", action="store_true", help="judge files already judged by this model")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--inhibit", action="store_true", help="hold a sleep/idle inhibitor while running")
    ap.add_argument("--dry-run", action="store_true", help="count what would be judged, send nothing")
    ap.add_argument("--allow-cpu", action="store_true",
                    help="keep going when Ollama put the model partly on the CPU (much slower, and RAM-hungry)")
    a = ap.parse_args(argv)
    if a.inhibit:
        if not shutil.which("systemd-inhibit"):
            sys.exit("vlm_trash: systemd-inhibit not found")
        args = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--inhibit"]
        os.execvp("systemd-inhibit", ["systemd-inhibit", "--what=sleep:idle", "--who=media-intake",
                                      "--why=media-intake overnight classify", sys.executable, "-I",
                                      os.path.abspath(__file__), *args])
    if a.all and a.files:
        sys.exit("vlm_trash: --all or FILE..., not both")
    if not shutil.which("exiftool"):
        sys.exit("vlm_trash: exiftool missing")
    try:
        host = check_host(a.host)
    except Refused as e:
        print(f"vlm_trash: {e}", file=sys.stderr)
        return 2

    counts = collections.Counter()
    if a.files:
        files = [os.path.abspath(f) for f in a.files]
        bad = [f for f in files if not os.path.isfile(f) or not under_roots(f)]
        bad += label.refused([f for f in files if f not in bad])
        if bad:
            print(f"vlm_trash: refusing {len(bad)} files (not library files, or quarantined/flagged/staging); "
                  "nothing sent", file=sys.stderr)
            return 3
    else:
        files = media_files() if a.all else candidates()
        bad = set(label.refused(files))
        counts["refused"] = len(bad)
        files = [f for f in files if f not in bad and under_roots(f)]
    files = [f for f in files if os.path.splitext(f)[1].lower() in IMG | VID]
    tags = read_tags(files)
    prog = load_progress()
    decided = {f for f in files if owner_decided(tags.get(f, []))}  # you decided: hands off
    counts["owner-decided"] = len(decided)
    files = [f for f in files if f not in decided]
    todo = files if a.redo else [f for f in files if not done_already(f, prog, tags)]
    counts["already"] = len(files) - len(todo)
    counts["todo"] = len(todo)
    if a.dry_run or not todo:
        print(json.dumps(dict(counts)))
        return 0
    try:
        check_digest(host)
    except Refused as e:
        print(f"vlm_trash: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"vlm_trash: Ollama not reachable on {host} ({type(e).__name__})", file=sys.stderr)
        return 2

    t0, pending, timed = time.monotonic(), [], 0
    try:
        for i, p in enumerate(todo, 1):
            t = time.monotonic()
            res = ("KEEP", None, None) if protected(tags.get(p, [])) else judge(host, p)
            if protected(tags.get(p, [])):
                counts["protected"] += 1
            if res is None:
                counts["undecodable"] += 1
            else:
                verdict, reason, conf = res
                strong = verdict == "TRASH" and conf is not None and conf >= TRASH_MIN
                counts["trash" if strong else "trash-low" if verdict == "TRASH" else "keep"] += 1
                if strong:
                    counts[f"trash/{reason}"] += 1
                pending.append((p, verdict, reason, conf))
            timed += time.monotonic() - t
            if i == 1 and not a.allow_cpu and res is not None and not protected(tags.get(p, [])):
                frac = on_gpu(host)
                if frac is not None and frac < 0.9:
                    print(f"vlm_trash: Ollama loaded {MODEL} {frac:.0%} on the GPU (the rest on CPU: very slow, "
                          "and it holds most of the RAM); stopping. --allow-cpu to run anyway", file=sys.stderr)
                    counts["stopped-not-on-gpu"] = 1
                    break
            if len(pending) >= CHUNK or i == len(todo):
                write_verdicts(pending, tags)
                for p2, verdict, _, _ in pending:
                    prog[p2] = {"digest": DIGEST, "stamp": stamp(p2), "verdict": verdict}
                save_progress(prog)
                pending = []
                per = timed / i
                eta = datetime.timedelta(seconds=int(per * (len(todo) - i)))
                print(f"[vlm] {i}/{len(todo)}  {per:.2f} s/file  ETA {eta}", file=sys.stderr, flush=True)
    finally:
        if pending:  # interrupted: keep what was judged
            write_verdicts(pending, tags)
            for p2, verdict, _, _ in pending:
                prog[p2] = {"digest": DIGEST, "stamp": stamp(p2), "verdict": verdict}
            save_progress(prog)
        unload(host)  # keep_alive 0: frees the GPU
    counts["seconds"] = round(time.monotonic() - t0, 1)
    counts["s_per_file"] = round(timed / max(1, len(todo)), 2)
    print(json.dumps(dict(counts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
