#!/usr/bin/env python3
"""Local semantic search over the media library (SigLIP embeddings, CPU, offline).

  venv/bin/python -I media_intake/semantic.py index
  venv/bin/python -I media_intake/semantic.py search "dog on a beach" [-n 20]
        [--videos|--photos] [--project NAME] [--unused-since 30d] [--clean]
  venv/bin/python -I media_intake/semantic.py similar FILE [-n 20] [same filters]

index: one image embedding per library file under the library, videos and
project folders (config.toml) (videos: mean of up to 8 sampled frames, via
scan_sensitive.video_frames, which falls back to GPU HEVC decoding), with
label.py's vision encoder and preprocessing. Stored as float16 in
<state>/embeddings.npz (0600) keyed by sha256; incremental: only new shas are
embedded ((path, size, mtime) caches the sha). Embeddings of files that left
the library, or that scan flagged, quarantined or erased, are dropped.
Refused, never read: label.refused() files (quarantine/, batches/ staging,
flagged/quarantined/erased sha256). Nothing but vectors, shas and paths is stored.

search: the query through SigLIP's text model, averaged over a few prompt templates
("a photo of {}", "a picture of {}", "{}"; --no-ensemble: the query alone). It lives only in the build cache
(~/.cache/media-intake-build, from setup.sh --build-labels) and is used from
there read-only, sha256-checked; without it only `similar` works.
similar: nearest neighbours of FILE by cosine (FILE is embedded on the fly if new).
Output (--format): on a terminal a numbered list (rank, score to 2 decimals, the date
the path carries, path); piped or captured, `score  path` lines (3 decimals) and
"N results" on stderr, the form gallery.py parses. index prints counts-only progress
and a summary on stderr (meter.py) and its counts as one JSON line on stdout.
No index or no model: one line naming the fix (media-intake index, bash setup.sh).
Filters read the XMP sidecars: --project P (Project|P), --unused-since Nd (no
Used|<YYYY-MM-DD-slug> in the last N days, like intake.py find), --clean (no
Review|child-in-shot, no Owner|trash, and no Review|maybe-trash or AI|vlm/*/trash/*
unless you kept the file: owner.kept, so Owner|not-trash outranks a trash flag).
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import label  # noqa: E402  (read-only: encoder path/sha, preprocessing, refusals)
import owner  # noqa: E402  (owner decisions outrank AI trash flags)
import meter  # noqa: E402  (counts-only progress, stdlib)
from build_label_embeddings import MAX_LEN, PAD, canonical  # noqa: E402  (the labels' text handling)

HOME = os.path.expanduser("~")
STATE = label.STATE
import config  # noqa: E402
ROOTS = config.library_roots()
IMG, VID = label.IMG | {".dng", ".gpr"}, label.VID  # raw photos too: Pillow reads what it can, the rest count as undecodable
STORE = os.path.join(STATE, "embeddings.npz")
BUILD = os.environ.get("MEDIA_INTAKE_BUILD", f"{HOME}/.cache/media-intake-build")
TEXT_SHA = label.PINS["siglip"]["files"]["onnx/text_model.onnx"]  # media_intake/pins.json
TOK_SHA = label.PINS["siglip"]["files"]["tokenizer.json"]
VIDEO_FRAMES = 8
POST_SLUG = re.compile(r"^(\d{4}-\d\d-\d\d)-[a-z0-9][a-z0-9-]*$")


# ---------------------------------------------------------------- store
def load_store(np):
    """(shas list, emb float32 N x D, files {path: [size, mtime_ns, sha]})."""
    try:
        z = np.load(STORE, allow_pickle=False)
        return list(z["sha"]), z["emb"].astype(np.float32), json.loads(str(z["files"]))
    except (FileNotFoundError, KeyError, ValueError):
        return [], np.zeros((0, 768), np.float32), {}


def save_store(np, shas, emb, files):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    tmp = STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        np.savez(f, sha=np.array(shas, dtype="U64"), emb=emb.astype(np.float16), files=np.array(json.dumps(files)))
    os.replace(tmp, STORE)


# ---------------------------------------------------------------- encoders
def vision(ort):
    if not os.path.exists(label.MODEL) or label._sha(label.MODEL) != label.MODEL_SHA:
        sys.exit(f"semantic: vision model missing or checksum wrong ({label.MODEL}); run bash setup.sh")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    return ort.InferenceSession(label.MODEL, so, providers=["CPUExecutionProvider"])


def video_frames(path, np):
    """Up to VIDEO_FRAMES RGB frames at 224 x 224 spread over the video, or None."""
    import scan_sensitive
    from PIL import Image
    work = os.path.join(HOME, ".cache", "media-intake")
    os.makedirs(work, mode=0o700, exist_ok=True)
    d = tempfile.mkdtemp(dir=work)
    try:
        names = scan_sensitive.video_frames(path, d, max_frames=VIDEO_FRAMES)[:VIDEO_FRAMES]
        if not names:
            return None
        return np.stack([np.asarray(Image.open(os.path.join(d, n)).convert("RGB")
                                    .resize((label.SIZE, label.SIZE), Image.BICUBIC)) for n in names]).astype(np.uint8)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def embed_file(sess, path, np):
    ext = os.path.splitext(path)[1].lower()
    fr = video_frames(path, np) if ext in VID else label.frame_image(path, np) if ext in IMG else None
    if fr is None:
        return None
    x = (fr.astype(np.float32) / 255.0 - label.MEAN) / label.STD
    x = np.ascontiguousarray(x.transpose(0, 3, 1, 2))
    e = np.concatenate([sess.run(["pooler_output"], {"pixel_values": x[i:i + 8]})[0] for i in range(0, len(x), 8)])
    e /= np.linalg.norm(e, axis=1, keepdims=True)
    m = e.mean(axis=0)
    return m / np.linalg.norm(m)


def text_available():
    t, k = os.path.join(BUILD, "text_model.onnx"), os.path.join(BUILD, "tokenizer.json")
    if not (os.path.isfile(t) and os.path.isfile(k) and os.path.isdir(os.path.join(BUILD, "site", "tokenizers"))):
        return "not in the build cache"
    if label._sha(k) != TOK_SHA or label._sha(t) != TEXT_SHA:
        return "checksum mismatch in the build cache"
    return None


# Prompt ensemble for text queries (the CLIP/SigLIP zero-shot technique): the query is embedded
# through each template, the normalised embeddings are averaged and renormalised. A query that
# already starts with a template's prefix ("a photo of ...") is stripped of it first, so no
# prompt reads "a photo of a photo of ...". --no-ensemble embeds the query as typed.
TEMPLATES = ("a photo of {}", "a picture of {}", "{}")
PREFIXES = tuple(t.split("{}")[0] for t in TEMPLATES if t != "{}")


def query_prompts(q, ensemble=True):
    """The prompts a query is embedded through, canonical as the labels' (build_label_embeddings)."""
    q = canonical(q)
    if not ensemble or not q:
        return [q]
    core = next((q[len(p):] for p in PREFIXES if q.startswith(p) and len(q) > len(p)), q)
    out = []
    for t in TEMPLATES:
        p = canonical(t.format(core))
        if p not in out:
            out.append(p)
    return out


def encode_texts(texts, np, ort):
    """Unit-length SigLIP text embeddings (len(texts) x D, float32), one batch through the model."""
    why = text_available()
    if why:
        sys.exit(f"semantic: SigLIP text model unavailable ({why}: {BUILD}); only `similar` works. "
                 "To enable search: bash setup.sh --build-labels, then media-intake search again")
    site = os.path.join(BUILD, "site")
    if site not in sys.path:
        sys.path.insert(0, site)
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(os.path.join(BUILD, "tokenizer.json"))
    ids = np.full((len(texts), MAX_LEN), PAD, dtype=np.int64)
    for i, t in enumerate(texts):
        e = tok.encode(t).ids[:MAX_LEN]
        if not e or e[-1] != 1:
            e = (e + [1])[-MAX_LEN:] if len(e) < MAX_LEN else e[:-1] + [1]
        ids[i, :len(e)] = e
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(os.path.join(BUILD, "text_model.onnx"), so, providers=["CPUExecutionProvider"])
    feed = {"input_ids": ids}
    if any(i.name == "attention_mask" for i in sess.get_inputs()):
        feed["attention_mask"] = np.ones_like(ids)
    v = np.asarray(sess.run(["pooler_output"], feed)[0], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def embed_text(q, np, ort, ensemble=True):
    """The query's unit vector: the renormalised mean over query_prompts (one prompt without the ensemble)."""
    m = encode_texts(query_prompts(q, ensemble), np, ort).mean(axis=0)
    return m / np.linalg.norm(m)


# ---------------------------------------------------------------- index
def library_files():
    out = []
    for r in ROOTS:
        if not os.path.isdir(r):
            continue
        for d, dirs, names in os.walk(r):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            out += [os.path.join(d, n) for n in sorted(names)
                    if os.path.splitext(n)[1].lower() in IMG | VID and not os.path.islink(os.path.join(d, n))]
    return out


def cmd_index(a, np, ort):
    t0 = time.monotonic()
    shas, emb, files = load_store(np)
    paths = library_files()
    bad = set(label.refused(paths))
    paths = [p for p in paths if p not in bad]
    flagged = label.flagged(STATE)
    cur = {}
    hm = meter.Meter("index (checking)", len(paths), every=1000)  # a first run hashes every file
    try:
        for i, p in enumerate(paths, 1):
            st = os.stat(p)
            old = files.get(p)
            sha = old[2] if old and old[:2] == [st.st_size, st.st_mtime_ns] else label._sha(p)
            hm.update(i)
            if flagged.get(sha) == st.st_size:
                continue
            cur[p] = [st.st_size, st.st_mtime_ns, sha]
    finally:
        hm.close()
    keep_shas = {v[2] for v in cur.values()}
    rows = {s: emb[i] for i, s in enumerate(shas) if s in keep_shas}
    dropped = len(shas) - len(rows)
    new = [p for p in sorted(cur) if cur[p][2] not in rows]
    seen, sess, n_img, n_vid, t_img, t_vid, failed = set(), None, 0, 0, 0.0, 0.0, 0
    unchanged = len(cur) - len(new)
    m = meter.Meter("index", len(new))
    m.say(f"index: {len(new):,} files to embed, {unchanged:,} already indexed")
    try:
        for i, p in enumerate(new, 1):
            s = cur[p][2]
            if s not in seen:  # a copy of a file embedded this run shares its vector
                seen.add(s)
                sess = sess or vision(ort)
                t = time.monotonic()
                v = embed_file(sess, p, np)
                dt = time.monotonic() - t
                if v is None:
                    failed += 1
                else:
                    rows[s] = v
                    if os.path.splitext(p)[1].lower() in VID:
                        n_vid, t_vid = n_vid + 1, t_vid + dt
                    else:
                        n_img, t_img = n_img + 1, t_img + dt
            m.update(i)
            if i % 200 == 0 and rows:
                save_store(np, list(rows), np.stack(list(rows.values())),
                           {p: cur[p] for p in cur if cur[p][2] in rows})
    finally:
        m.close()
    cur = {p: v for p, v in cur.items() if v[2] in rows}
    order = sorted(rows)
    save_store(np, order, np.stack([rows[s] for s in order]) if order else np.zeros((0, 768), np.float32), cur)
    counts = {"files": len(cur), "vectors": len(order), "new_images": n_img, "new_videos": n_vid,
              "undecodable": failed, "refused": len(bad), "dropped": dropped,
              "s_per_image": round(t_img / n_img, 3) if n_img else None,
              "s_per_video": round(t_vid / n_vid, 3) if n_vid else None,
              "seconds": round(time.monotonic() - t0, 1)}
    person = on_terminal()
    if not person:  # the queue's step log and scripts read this one JSON line
        print(json.dumps(counts))
    copies = sum(1 for p in new if p in cur) - (n_img + n_vid)  # same sha as a file embedded this run
    m.say(f"index: indexed {n_img + n_vid:,} new ({n_img:,} photos, {n_vid:,} videos), "
          f"{unchanged:,} unchanged, {failed:,} failed (undecodable)"
          + (f", {copies:,} copies of new files" if copies else "")
          + f"; {len(bad):,} refused, {dropped:,} dropped; took {meter.duration(time.monotonic() - t0)}")
    if person:
        print(INDEX_NEXT)


INDEX_NEXT = 'next: media-intake search "a photo of a dog" -n 20'


def on_terminal():
    """True when stdout is a terminal (a person), False when piped or captured (the queue, the gallery)."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------- query
def sidecar_tags(paths):
    xs = [p + ".xmp" for p in paths if os.path.isfile(p + ".xmp")]
    out = {}
    if xs and shutil.which("exiftool"):
        r = subprocess.run(["exiftool", "-j", "-q", "-q", "-HierarchicalSubject", "-@", "-"],
                           input="\n".join(xs) + "\n", capture_output=True, text=True)
        for row in json.loads(r.stdout or "[]"):
            v = row.get("HierarchicalSubject")
            out[row["SourceFile"][:-4]] = [] if v is None else [str(x) for x in (v if isinstance(v, list) else [v])]
    return out


def passes(tags, a, cutoff):
    low = [t.casefold() for t in tags]
    if a.project and f"project|{a.project.casefold()}" not in low:
        return False
    if cutoff:
        for t in tags:
            m = POST_SLUG.match(t[5:]) if t.startswith("Used|") else None
            if m and datetime.date.fromisoformat(m.group(1)) >= cutoff:
                return False
    if a.clean and ("Review|child-in-shot" in tags or owner.trashed(tags) or not owner.kept(tags) and any(
            t == "Review|maybe-trash" or re.match(r"AI\|vlm/[^/]+/trash/", t) for t in tags)):
        return False
    return True


NO_INDEX = "semantic: no search index yet ({}); build it with: media-intake index"


def need_index(np):
    """The store, or exit with one line naming the command that builds it (the gallery shows the
    last stderr line as is)."""
    shas, emb, files = load_store(np)
    if not shas:
        sys.exit(NO_INDEX.format(f"{STORE} is missing" if not os.path.exists(STORE) else "it holds no files"))
    return shas, emb, files


def rank(np, q, a, exclude=None, store=None):
    shas, emb, files = store or need_index(np)
    m = re.fullmatch(r"(\d+)d", a.unused_since or "0d")
    if not m:
        sys.exit("--unused-since wants Nd, e.g. 30d")
    cutoff = datetime.date.today() - datetime.timedelta(days=int(m.group(1))) if a.unused_since else None
    flagged = label.flagged(STATE)  # anything flagged since indexing never comes back
    score = emb @ q
    by_sha = {}
    for p, (size, _, s) in files.items():
        if os.path.isfile(p) and flagged.get(s) != size and s != exclude:
            ext = os.path.splitext(p)[1].lower()
            if (a.videos and ext not in VID) or (a.photos and ext not in IMG):
                continue
            by_sha.setdefault(s, []).append(p)
    idx = {s: i for i, s in enumerate(shas)}
    cands = sorted(((float(score[idx[s]]), p) for s, ps in by_sha.items() if s in idx for p in ps), reverse=True)
    need = a.project or cutoff or a.clean
    out = []
    for i in range(0, len(cands), 500):
        chunk = cands[i:i + 500]
        tags = sidecar_tags([p for _, p in chunk]) if need else {}
        out += [(s, p) for s, p in chunk if not need or passes(tags.get(p, []), a, cutoff)]
        if len(out) >= a.n:
            break
    out = out[:a.n]
    if human(a):
        show(out, a)
        return
    for s, p in out:  # the machine form: the gallery parses `score  /path` (gallery.py, App.semantic)
        print(f"{s:.3f}  {p}")
    print(f"{len(out)} results", file=sys.stderr)


def human(a):
    """--format human, or auto on a terminal. Piped or captured output (the gallery) stays machine."""
    if a.format == "auto":
        return on_terminal()
    return a.format == "human"


DAY = re.compile(r"(?<!\d)((?:19|20)\d\d)(-?)(\d\d)\2(\d\d)(?!\d)")
MONTH = re.compile(r"(?:^|/)((?:19|20)\d\d)/\1-(\d\d)(?:/|$)")


def path_date(p):
    """The date a path carries, without opening the file: a day in the file or folder name
    (2026-11-08, 20261108), else intake's YYYY/YYYY-MM library folder; '' when neither."""
    for part in reversed(p.split(os.sep)):
        for y, _, mo, d in DAY.findall(part):
            try:
                return datetime.date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                continue
    m = MONTH.search(os.path.dirname(p))
    return f"{m.group(1)}-{m.group(2)}" if m and 1 <= int(m.group(2)) <= 12 else ""


def show(out, a, file=None):
    """Numbered list for a person: rank, score (2 decimals), date if the path has one, path."""
    file = file or sys.stdout
    if not out:
        print("No results." + (" Try without --project/--unused-since/--clean/--photos/--videos."
                               if a.project or a.unused_since or a.clean or a.photos or a.videos else ""), file=file)
        return
    w = len(str(len(out)))
    for i, (s, p) in enumerate(out, 1):
        print(f"{i:>{w}}. {s:.2f}  {path_date(p) or '':<10}  {p}", file=file)
    print(f"{len(out)} result{'s' if len(out) != 1 else ''}, best first (score: cosine, compare within one query)",
          file=file)


def cmd_search(a, np, ort):
    store = need_index(np)  # before the text model: say the cheaper fix first
    kw = {"ensemble": False} if a.no_ensemble else {}  # the default call keeps embed_text(q, np, ort)
    rank(np, embed_text(a.query, np, ort, **kw), a, store=store)


def cmd_similar(a, np, ort):
    f = os.path.abspath(a.file)
    if not os.path.isfile(f):
        sys.exit(f"semantic: no such file {f}")
    if label.refused([f]):
        print("semantic: refusing a quarantined/flagged/staging file", file=sys.stderr)
        sys.exit(3)
    store = need_index(np)
    shas, emb, files = store
    s = files.get(f, [None, None, None])[2] or label._sha(f)
    if s in shas:
        v = emb[shas.index(s)]
    else:
        v = embed_file(vision(ort), f, np)
        if v is None:
            sys.exit("semantic: could not decode FILE")
    rank(np, v / np.linalg.norm(v), a, exclude=s, store=store)


def main(argv=None, prog="media-intake"):
    ap = argparse.ArgumentParser(
        prog=prog, description="Search your library by what is in the pictures, offline on this laptop. Needs the "
                               "venv and model that bash setup.sh installs (docs/search.md).")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="index|search|similar")
    sub.add_parser("index", help="build or update the search index",
                   description="Build or update the search index: reads every photo and video in the library "
                               "once (later runs only new ones) and stores a short summary of each in the "
                               "media-intake state folder. Changes no photo, video or tag. Run it before search, "
                               "similar or calibrate.")
    filters = ("Filters: --photos or --videos, --project NAME, --unused-since Nd, --clean. Output on a terminal is a "
               "numbered list, best match first; piped, it is `score  path` lines.")
    for name in ("search", "similar"):
        what = ("Find photos and videos that match a description, e.g. \"dog on a beach\"." if name == "search"
                else "Find photos and videos that look like FILE.")
        s = sub.add_parser(name, help="find files matching a description" if name == "search"
                           else "find files that look like a given one",
                           description=f"{what} Read-only: changes nothing. Needs the search index "
                                       f"(media-intake index). {filters}")
        if name == "search":
            s.add_argument("query", help="what to look for, in quotes, e.g. \"dog on a beach\"")
        else:
            s.add_argument("file", help="a photo or video; its look-alikes are listed")
        s.add_argument("-n", type=int, default=20, metavar="N", help="how many results to show (default 20)")
        g = s.add_mutually_exclusive_group()
        g.add_argument("--videos", action="store_true", help="videos only")
        g.add_argument("--photos", action="store_true", help="photos only")
        s.add_argument("--project", metavar="NAME", help="only files tagged with this project (Project|NAME)")
        s.add_argument("--unused-since", metavar="Nd",
                       help="only files not used in a post in the last N days, e.g. 30d")
        s.add_argument("--clean", action="store_true",
                       help="leave out files with a child in shot, and ones marked or suggested as trash "
                            "(unless you kept them)")
        s.add_argument("--format", choices=("auto", "human", "machine"), default="auto",
                       help="human: numbered list with dates; machine: `score  path` lines (what the gallery "
                            "reads); auto (default): human on a terminal, machine otherwise")
        if name == "search":
            s.add_argument("--no-ensemble", action="store_true",
                           help="embed the query as typed, not averaged over prompt templates "
                                "(\"a photo of ...\", \"a picture of ...\", as typed); to compare results")
    a = ap.parse_args(argv)
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError as e:
        sys.exit(f"semantic: {e.name or 'a module'} is missing; run it through `media-intake {a.cmd}` "
                 "(the venv bash setup.sh builds), or run bash setup.sh first")
    {"index": cmd_index, "search": cmd_search, "similar": cmd_similar}[a.cmd](a, np, ort)


if __name__ == "__main__":
    main()
