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

search: the query through SigLIP's text model. It lives only in the build cache
(~/.cache/media-intake-build, from setup.sh --build-labels) and is used from
there read-only, sha256-checked; without it only `similar` works.
similar: nearest neighbours of FILE by cosine (FILE is embedded on the fly if new).
Filters read the XMP sidecars: --project P (Project|P), --unused-since Nd (no
Used|<YYYY-MM-DD-slug> in the last N days, like intake.py find), --clean (no
Review|child-in-shot, Review|maybe-trash, AI|vlm/*/trash/*).
"""
import argparse, datetime, json, os, re, shutil, string, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import label  # noqa: E402  (read-only: encoder path/sha, preprocessing, refusals)

HOME = os.path.expanduser("~")
STATE = label.STATE
import config  # noqa: E402
ROOTS = config.library_roots()
IMG, VID = label.IMG, label.VID
STORE = os.path.join(STATE, "embeddings.npz")
BUILD = os.environ.get("MEDIA_INTAKE_BUILD", f"{HOME}/.cache/media-intake-build")
TEXT_SHA = "3aa7fdbd20eaa8740cce17bf82913de641fcb632a768fed59f661cdcd0c32553"  # setup.sh pin
TOK_SHA = "4a17c975210be5ab4c36b47d8dae4eefb866dbfb1e676e394aad85dc30a3ae08"
MAX_LEN, PAD = 64, 1   # as build_label_embeddings.py
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


def embed_text(q, np, ort):
    why = text_available()
    if why:
        sys.exit(f"semantic: SigLIP text model unavailable ({why}: {BUILD}); only `similar` works. "
                 "To enable search: bash setup.sh --build-labels")
    sys.path.insert(0, os.path.join(BUILD, "site"))
    from tokenizers import Tokenizer
    q = re.sub(r"\s+", " ", q.lower().translate(str.maketrans("", "", string.punctuation))).strip()
    ids = np.full((1, MAX_LEN), PAD, dtype=np.int64)
    e = Tokenizer.from_file(os.path.join(BUILD, "tokenizer.json")).encode(q).ids[:MAX_LEN]
    if not e or e[-1] != 1:
        e = (e + [1])[-MAX_LEN:] if len(e) < MAX_LEN else e[:-1] + [1]
    ids[0, :len(e)] = e
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(os.path.join(BUILD, "text_model.onnx"), so, providers=["CPUExecutionProvider"])
    feed = {"input_ids": ids}
    if any(i.name == "attention_mask" for i in sess.get_inputs()):
        feed["attention_mask"] = np.ones_like(ids)
    v = sess.run(["pooler_output"], feed)[0][0].astype(np.float32)
    return v / np.linalg.norm(v)


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
    for p in paths:
        st = os.stat(p)
        old = files.get(p)
        sha = old[2] if old and old[:2] == [st.st_size, st.st_mtime_ns] else label._sha(p)
        if flagged.get(sha) == st.st_size:
            continue
        cur[p] = [st.st_size, st.st_mtime_ns, sha]
    keep_shas = {v[2] for v in cur.values()}
    rows = {s: emb[i] for i, s in enumerate(shas) if s in keep_shas}
    dropped = len(shas) - len(rows)
    new = [p for p in sorted(cur) if cur[p][2] not in rows]
    seen, sess, n_img, n_vid, t_img, t_vid, failed = set(), None, 0, 0, 0.0, 0.0, 0
    for i, p in enumerate(new, 1):
        s = cur[p][2]
        if s in seen:
            continue
        seen.add(s)
        sess = sess or vision(ort)
        t = time.monotonic()
        v = embed_file(sess, p, np)
        dt = time.monotonic() - t
        if v is None:
            failed += 1
            continue
        rows[s] = v
        if os.path.splitext(p)[1].lower() in VID:
            n_vid, t_vid = n_vid + 1, t_vid + dt
        else:
            n_img, t_img = n_img + 1, t_img + dt
        if i % 200 == 0:
            print(f"[index] {i}/{len(new)}", file=sys.stderr, flush=True)
            save_store(np, list(rows), np.stack(list(rows.values())), {p: cur[p] for p in cur if cur[p][2] in rows})
    cur = {p: v for p, v in cur.items() if v[2] in rows}
    order = sorted(rows)
    save_store(np, order, np.stack([rows[s] for s in order]) if order else np.zeros((0, 768), np.float32), cur)
    print(json.dumps({"files": len(cur), "vectors": len(order), "new_images": n_img, "new_videos": n_vid,
                      "undecodable": failed, "refused": len(bad), "dropped": dropped,
                      "s_per_image": round(t_img / n_img, 3) if n_img else None,
                      "s_per_video": round(t_vid / n_vid, 3) if n_vid else None,
                      "seconds": round(time.monotonic() - t0, 1)}))


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
    if a.clean and any(t in ("Review|child-in-shot", "Review|maybe-trash")
                       or re.match(r"AI\|vlm/[^/]+/trash/", t) for t in tags):
        return False
    return True


def rank(np, q, a, exclude=None):
    shas, emb, files = load_store(np)
    if not shas:
        sys.exit("semantic: no embeddings yet; run semantic.py index")
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
    for s, p in out[:a.n]:
        print(f"{s:.3f}  {p}")
    print(f"{min(len(out), a.n)} results", file=sys.stderr)


def cmd_search(a, np, ort):
    rank(np, embed_text(a.query, np, ort), a)


def cmd_similar(a, np, ort):
    f = os.path.abspath(a.file)
    if not os.path.isfile(f):
        sys.exit(f"semantic: no such file {f}")
    if label.refused([f]):
        print("semantic: refusing a quarantined/flagged/staging file", file=sys.stderr)
        sys.exit(3)
    shas, emb, files = load_store(np)
    s = files.get(f, [None, None, None])[2] or label._sha(f)
    if s in shas:
        v = emb[shas.index(s)]
    else:
        v = embed_file(vision(ort), f, np)
        if v is None:
            sys.exit("semantic: could not decode FILE")
    rank(np, v / np.linalg.norm(v), a, exclude=s)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    for name in ("search", "similar"):
        s = sub.add_parser(name)
        s.add_argument("query" if name == "search" else "file")
        s.add_argument("-n", type=int, default=20)
        g = s.add_mutually_exclusive_group()
        g.add_argument("--videos", action="store_true")
        g.add_argument("--photos", action="store_true")
        s.add_argument("--project")
        s.add_argument("--unused-since", metavar="Nd")
        s.add_argument("--clean", action="store_true", help="drop child-in-shot and trash-flagged files")
    a = ap.parse_args(argv)
    import numpy as np
    import onnxruntime as ort
    {"index": cmd_index, "search": cmd_search, "similar": cmd_similar}[a.cmd](a, np, ort)


if __name__ == "__main__":
    main()
