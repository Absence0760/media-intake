#!/usr/bin/env python3
"""People and pets recognition: the shared core (store, clustering, names, suggestions, tags, erase hooks).

Two commands sit on this one core: `media-intake people ...` (people.py: YuNet faces, SFace vectors) and
`media-intake pets ...` (pets.py). Off by default: each command refuses until its models are installed
(`bash setup.sh --recognition`). Fully offline; nothing is named without the owner. docs/recognition.md.

Plug-in interface (what a kind module gives the core)
-----------------------------------------------------
  Detector.detect(image) -> [(box, score, species)] or [(box, score, species, crop)]
      image: an H x W x 3 uint8 RGB numpy array (a photo, or one video frame).
      box: (x, y, w, h) in that image's pixels. score: the detector's confidence.
      species: None for faces; "cat", "dog", ... for pets (two species never share an individual).
      crop (optional): the input the embedder wants for that box (e.g. an aligned face); without it the
      core cuts square_crop(image, box): a 10 % margin, padded to a square, never centre-cropped.
  Embedder.embed(crop) -> vector (1-D, any length); the core L2-normalises and stores it as float32.
  Kind(kind=..., command=..., prefix=..., model=..., link=..., same=..., models=..., engine=...)
      engine() -> (detector, {model name: embedder}); called once per scan and only when there is
      something new to scan. Several embedders may run (stored side by side for `eval`); clustering
      uses Kind.model's vectors. models: [(what, path, sha256)] that must exist for the command to run.

Frames (videos): ingest() takes any iterable of (t_seconds, rgb) frames; a photo is the one frame
(None, rgb). Videos are scanned only when a frame source exists: frames.sample(path) -> iterator of
(t_seconds, numpy RGB array) from media_intake/frames.py when that module is present, or the frames=
argument of scan() (tests pass a stub). Without one, videos are counted as skipped. A detection keeps
its frame's t; two detections in one photo or one frame are never one individual.

Store (biometric: laptop only, never in git, never backed up or synced):
  <recognition.dir> (config; default <state>/recognition), 0700, refused inside a library root
    recognition.db   sqlite, 0600: files (path, sha, size, mtime), scans (sha, kind), detections
                     (box as fractions of the image, score, kind, species, t, cluster, name, how),
                     embeddings (det, model, vector), merges, rejects, written, meta
    crops/           160 px JPEG crops made on demand for the naming page, 0600
Files are keyed by path + size + mtime; the same content (sha256) at another path reuses its rows.

how (who decided a detection's name):
  NULL         no name
  suggested    the tool: the detection's cluster carries exactly one owner name. A suggestion, never a
               tag, until the owner confirms it (confirm) or rejects it (reject: never suggested that name
               again)
  cluster      the owner named (or ignored, or merged into) the cluster it was in
  confirmed    the owner confirmed a suggestion
  out          the owner took it out of a name (take-out, or removed the tag from a sidecar); final
Only cluster and confirmed names are tags (People|<Name>, Pets|<Name>: lr:HierarchicalSubject, the leaf
in dc:Subject). tag removes only tags it wrote and never re-adds one the owner removed.

Never scanned, and dropped (rows and crops) before every command: label.refused() content (quarantine,
staging, anything scan flagged, quarantined or erased), trashed content (trashed.json), screenshots and
screen recordings. erase, forget, trash and sweep-erased call forget(); verify-gone calls leftovers().
"""
import collections, datetime, glob, io, json, os, re, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config  # noqa: E402

DB_NAME = "recognition.db"
IGNORED = "\x00ignored"       # the name of an ignored detection (never a real name)
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f|/\\,;]{1,64}$")
SHA_RE = re.compile(r"[0-9a-f]{64}")
CROP_PX = 160
NEIGHBOURS = 30
MARGIN = 0.10                 # square_crop's margin around a box, each side
IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VID = {".mp4", ".mov", ".3gp", ".mkv", ".webm"}
SCREEN_DIRS = ("Screenshots", "Screen recordings")
SCREEN_SOURCES = ("Source|screenshot", "Source|screen recording")
OWNER_HOW = ("cluster", "confirmed")   # owner names: the only ones written as tags


class Kind:
    """What one command (people, pets) plugs into the core. See the module docstring."""

    def __init__(self, kind, command, prefix, model, link, same, models, engine, noun, max_side=1600):
        self.kind, self.command, self.prefix, self.model = kind, command, prefix, model
        self.link, self.same, self.models, self.engine = link, same, models, engine
        self.noun, self.max_side = noun, max_side

    def missing(self):
        """The model files not installed (path), for the refusal and doctor."""
        return [p for _, p, _ in self.models if not os.path.isfile(p)]


# ---------------------------------------------------------------- storage
def state_dir():
    return config.get()["state"]


def store_dir(cfg=None):
    return (cfg or config.get())["recognition_dir"]


def check_location(d, cfg=None):
    """Refuse a store inside (or containing) the library, videos or a project folder: those are backed up."""
    rd = os.path.realpath(d)
    for root in config.library_roots(cfg or config.get()):
        rr = os.path.realpath(root)
        if rd == rr or rd.startswith(rr + os.sep) or rr.startswith(rd + os.sep):
            raise ValueError(f"recognition.dir {d} is inside or contains {root}: keep it out of the library "
                             "(it is never backed up); docs/configuration.md")
    return d


def private_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def db_path(d=None):
    return os.path.join(d or store_dir(), DB_NAME)


def crops_dir(d=None):
    return os.path.join(d or store_dir(), "crops")


SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha TEXT, size INTEGER, mtime REAL, skip TEXT);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha);
CREATE TABLE IF NOT EXISTS scans(sha TEXT, kind TEXT, status TEXT, w INTEGER, h INTEGER, scanned TEXT,
    PRIMARY KEY(sha, kind));
CREATE TABLE IF NOT EXISTS detections(id INTEGER PRIMARY KEY, sha TEXT, kind TEXT, species TEXT, t REAL,
    x REAL, y REAL, w REAL, h REAL, score REAL, cluster INTEGER, name TEXT, how TEXT);
CREATE INDEX IF NOT EXISTS detections_sha ON detections(sha);
CREATE INDEX IF NOT EXISTS detections_cluster ON detections(kind, cluster);
CREATE TABLE IF NOT EXISTS embeddings(det INTEGER, model TEXT, vec BLOB, PRIMARY KEY(det, model));
CREATE TABLE IF NOT EXISTS merges(a INTEGER, b INTEGER, PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS rejects(det INTEGER, name TEXT, PRIMARY KEY(det, name));
CREATE TABLE IF NOT EXISTS written(path TEXT, tag TEXT, state TEXT, PRIMARY KEY(path, tag));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=None, threads=False):
    """The store (created 0700 / 0600 when missing). threads: one connection shared by a server's threads."""
    path = path or db_path()
    private_dir(os.path.dirname(path))
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)
    c = sqlite3.connect(path, check_same_thread=not threads, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
    except sqlite3.DatabaseError:
        c.close()
        raise
    return c


def meta(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(c, k, v):
    c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, str(v)))


def today():
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------- forgetting (erase, trash, prune)
def drop_crops(sha, d=None):
    n = 0
    if SHA_RE.fullmatch(sha or ""):
        for p in glob.glob(os.path.join(crops_dir(d), f"{sha}-*")):  # a crop, or one a crash left half-written
            os.remove(p)
            n += 1
    return n


def delete_detections(c, where, args):
    """Delete detections matching `where` and everything hanging off them. Returns (detections, vectors)."""
    ids = [r[0] for r in c.execute(f"SELECT id FROM detections WHERE {where}", args)]
    vecs = 0
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        vecs += c.execute(f"DELETE FROM embeddings WHERE det IN ({q})", part).rowcount
        c.execute(f"DELETE FROM rejects WHERE det IN ({q})", part)
        c.execute(f"DELETE FROM merges WHERE a IN ({q}) OR b IN ({q})", part + part)
        c.execute(f"DELETE FROM detections WHERE id IN ({q})", part)
    return len(ids), vecs


def forget_sha(c, sha, d=None):
    """Delete every row and crop of this content, both kinds. Returns a Counter."""
    out = collections.Counter()
    out["detections"], out["vectors"] = delete_detections(c, "sha=?", (sha,))
    paths = [r[0] for r in c.execute("SELECT path FROM files WHERE sha=?", (sha,))]
    for p in paths:
        c.execute("DELETE FROM written WHERE path=?", (p,))
    out["file rows"] = c.execute("DELETE FROM files WHERE sha=?", (sha,)).rowcount
    c.execute("DELETE FROM scans WHERE sha=?", (sha,))
    out["crops"] = drop_crops(sha, d)
    return out


def forget(shas, d=None):
    """erase, forget, trash and sweep-erased: drop every detection, vector, file row and crop of these
    sha256s, for people and pets. Never creates the store. Returns a Counter (counts only); an unreadable
    store counts as {"store unreadable": 1}, so the coverage report says so instead of passing silently."""
    d = d or store_dir()
    out = collections.Counter()
    shas = sorted({s for s in shas if SHA_RE.fullmatch(s or "")})
    if not shas:
        return out
    if os.path.isfile(db_path(d)):
        try:
            c = connect(db_path(d))
            try:
                for s in shas:
                    out.update(forget_sha(c, s, d))
                c.commit()
            finally:
                c.close()
        except sqlite3.DatabaseError:
            out["store unreadable"] += 1
            for s in shas:  # the crops can still go
                out["crops"] += drop_crops(s, d)
    else:
        for s in shas:
            out["crops"] += drop_crops(s, d)
    return +out


def leftovers(shas, d=None):
    """verify-gone: how many rows and crops of these sha256s are still in the store (read-only)."""
    d = d or store_dir()
    out = collections.Counter()
    shas = sorted({s for s in shas if SHA_RE.fullmatch(s or "")})
    for s in shas:
        out["crops"] += len(glob.glob(os.path.join(crops_dir(d), f"{s}-*")))
    p = db_path(d)
    if shas and os.path.isfile(p):
        try:
            import urllib.parse
            c = sqlite3.connect("file:" + urllib.parse.quote(p) + "?mode=ro", uri=True, timeout=30)
            try:
                for i in range(0, len(shas), 500):
                    part = shas[i:i + 500]
                    q = ",".join("?" * len(part))
                    for what, sql in (("detections", f"SELECT COUNT(*) FROM detections WHERE sha IN ({q})"),
                                      ("vectors", "SELECT COUNT(*) FROM embeddings WHERE det IN "
                                                  f"(SELECT id FROM detections WHERE sha IN ({q}))"),
                                      ("file rows", f"SELECT COUNT(*) FROM files WHERE sha IN ({q})")):
                        try:
                            out[what] += c.execute(sql, part).fetchone()[0]
                        except sqlite3.OperationalError:
                            pass  # an older or empty store without that table: nothing of it there
            finally:
                c.close()
        except sqlite3.DatabaseError:
            out["store unreadable"] += 1
    return +out


def trashed_shas(state=None):
    try:
        with open(os.path.join(state or state_dir(), "trashed.json")) as f:
            return set(json.load(f) or {})
    except (FileNotFoundError, ValueError):
        return set()


def prune(c, state=None, d=None, flagged=None):
    """Drop content flagged, quarantined, erased or trashed since, and file rows gone from disk or now under
    quarantine/ or batches/. Returns {reason: count}."""
    import label
    state = state or state_dir()
    flagged = label.flagged(state) if flagged is None else flagged
    gone = set(flagged) | trashed_shas(state)
    out = collections.Counter()
    for (sha,) in c.execute("SELECT DISTINCT sha FROM files WHERE sha IS NOT NULL "
                            "UNION SELECT DISTINCT sha FROM detections").fetchall():
        if sha in gone:
            forget_sha(c, sha, d)
            out["flagged, erased, private or trashed"] += 1
    roots = [os.path.realpath(os.path.join(state, x)) + os.sep for x in ("quarantine", "batches")]
    for r in c.execute("SELECT path FROM files").fetchall():
        rp = os.path.realpath(r["path"])
        if any(rp.startswith(x) for x in roots) or not os.path.isfile(r["path"]):
            c.execute("DELETE FROM files WHERE path=?", (r["path"],))
            c.execute("DELETE FROM written WHERE path=?", (r["path"],))
            out["gone or moved"] += 1
    for (sha,) in c.execute("SELECT DISTINCT sha FROM detections WHERE sha NOT IN "
                            "(SELECT sha FROM files WHERE sha IS NOT NULL) UNION SELECT DISTINCT sha FROM scans "
                            "WHERE sha NOT IN (SELECT sha FROM files WHERE sha IS NOT NULL)").fetchall():
        forget_sha(c, sha, d)
    c.commit()
    return out


# ---------------------------------------------------------------- what may be scanned
def is_screen(path, tags=(), library=None):
    lib = os.path.abspath(library or config.get()["library"]) + os.sep
    ap = os.path.abspath(path)
    if ap.startswith(lib) and ap[len(lib):].split(os.sep)[0] in SCREEN_DIRS:
        return True
    vids = os.path.abspath(config.get()["videos"]) + os.sep
    if ap.startswith(vids) and ap[len(vids):].split(os.sep)[0] in SCREEN_DIRS:
        return True
    return any(t in SCREEN_SOURCES for t in tags)


def sidecar_tags(path):
    """The sidecar's lr:HierarchicalSubject tags ([] without a sidecar or exiftool)."""
    import intake
    if not os.path.isfile(intake.sidecar(path)):
        return []
    try:
        return list(intake.read_sidecar_tags([path]).get(path, ([],))[0])
    except (OSError, ValueError):
        return []


def candidates(roots, files, videos=True):
    """Photos (and videos) under roots plus the given files, absolute, sorted, no duplicates, no symlinks."""
    exts = IMG | (VID if videos else set())
    out = []
    for f in files:
        if os.path.splitext(f)[1].lower() in exts and os.path.isfile(f):
            out.append(os.path.abspath(f))
    for root in roots:
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for n in sorted(names):
                p = os.path.join(d, n)
                if os.path.splitext(n)[1].lower() in exts and os.path.isfile(p) and not os.path.islink(p):
                    out.append(os.path.abspath(p))
    return list(dict.fromkeys(out))


def default_frames():
    """frames.sample (lane "frames": media_intake/frames.py) when present, else None (videos skipped)."""
    if not os.path.isfile(os.path.join(HERE, "frames.py")):
        return None
    import frames
    return getattr(frames, "sample", None)


# ---------------------------------------------------------------- images and crops
def _pillow():
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass  # no HEIC support in this Python: HEIC files count as unreadable
    return Image, ImageOps


def load_rgb(path, max_side=1600):
    """(rgb array at most max_side on the long side, after EXIF rotation), or None when unreadable."""
    import numpy as np
    Image, ImageOps = _pillow()
    try:
        with Image.open(path) as im:
            im.draft("RGB", (max_side, max_side))
            im = ImageOps.exif_transpose(im).convert("RGB")
            s = min(1.0, max_side / max(im.size))
            if s < 1.0:
                im = im.resize((max(1, round(im.size[0] * s)), max(1, round(im.size[1] * s))), Image.BILINEAR)
            return np.asarray(im, dtype=np.uint8)
    except Exception:
        return None


def photo_frames(path, max_side=1600):
    """A photo as a frame source: [(None, rgb)], or [] when unreadable."""
    rgb = load_rgb(path, max_side)
    return [] if rgb is None else [(None, rgb)]


def square_crop(rgb, box, margin=MARGIN):
    """The box with `margin` on each side, padded (black) to a square: nothing of the animal cut off."""
    import numpy as np
    H, W = rgb.shape[:2]
    x, y, w, h = box
    side = max(w, h) * (1 + 2 * margin)
    cx, cy = x + w / 2, y + h / 2
    x0, y0 = int(round(cx - side / 2)), int(round(cy - side / 2))
    n = max(1, int(round(side)))
    out = np.zeros((n, n, 3), dtype=np.uint8)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(W, x0 + n), min(H, y0 + n)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = rgb[sy0:sy1, sx0:sx1]
    return out


def unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v / max(float(np.linalg.norm(v)), 1e-9)


# ---------------------------------------------------------------- scan
def ingest(c, kind, sha, source, detector, embedders):
    """Detect and embed every frame of source (iterable of (t, rgb); t None for a photo) as this content's
    detections of `kind`, replacing earlier ones. Returns (detections, (w, h) of the first frame or None)."""
    delete_detections(c, "sha=? AND kind=?", (sha, kind))
    n, size = 0, None
    for t, rgb in source:
        H, W = rgb.shape[:2]
        size = size or (W, H)
        for d in detector.detect(rgb) or ():
            box, score, species = d[0], d[1], d[2]
            crop = d[3] if len(d) > 3 and d[3] is not None else square_crop(rgb, box)
            x, y, w, h = (float(v) for v in box)
            cur = c.execute("INSERT INTO detections(sha, kind, species, t, x, y, w, h, score) VALUES (?,?,?,?,?,?,?,?,?)",
                            (sha, kind, species, None if t is None else float(t), x / W, y / H, w / W, h / H,
                             float(score)))
            for model, emb in embedders.items():
                c.execute("INSERT INTO embeddings(det, model, vec) VALUES (?,?,?)",
                          (cur.lastrowid, model, unit(emb.embed(crop)).tobytes()))
            n += 1
    return n, size


def file_sha(path, size, index):
    ix = index.get(path) or {}
    if ix.get("size") == size and SHA_RE.fullmatch(ix.get("sha") or ""):
        return ix["sha"]
    import intake
    return intake.sha256(path)


def scan(c, spec, paths, engine=None, frames="default", limit=None, log=print, state=None, d=None):
    """Detect and embed spec.kind in paths: skips refused, trashed, screenshots and unchanged files (path,
    size and mtime as last time); the same content at another path reuses its rows. frames: a callable
    path -> iterable of (t, rgb) for videos ("default": default_frames(); None: videos skipped).
    engine: () -> (detector, {model: embedder}), default spec.engine. Returns counts."""
    import label
    state = state or state_dir()
    if frames == "default":
        frames = default_frames()
    cnt = collections.Counter()
    bad = set(label.refused(paths, state))
    trashed = trashed_shas(state)
    cnt["refused (quarantine, flagged, erased)"] = len(bad)
    index = {}
    try:
        with open(os.path.join(state, "index.json")) as f:
            index = json.load(f) or {}
    except (FileNotFoundError, ValueError):
        pass  # no index yet: every sha is computed
    todo = []
    for p in paths:
        if p in bad:
            continue
        is_vid = os.path.splitext(p)[1].lower() in VID
        if is_vid and frames is None:
            cnt["videos skipped (no frame source yet)"] += 1
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        r = c.execute("SELECT sha, size, mtime, skip FROM files WHERE path=?", (p,)).fetchone()
        if r and r["size"] == st.st_size and abs(r["mtime"] - st.st_mtime) < 1e-3 and (
                r["skip"] or c.execute("SELECT 1 FROM scans WHERE sha=? AND kind=?", (r["sha"], spec.kind)).fetchone()):
            cnt["unchanged"] += 1
            continue
        if is_screen(p, sidecar_tags(p)):
            c.execute("INSERT OR REPLACE INTO files(path, sha, size, mtime, skip) VALUES (?,?,?,?,?)",
                      (p, None, st.st_size, st.st_mtime, "screenshot"))
            cnt["screenshots skipped"] += 1
            continue
        todo.append((p, st, is_vid))
    c.commit()
    if limit is not None:
        todo = todo[:limit]
    det = embs = None
    t0 = time.monotonic()
    for i, (p, st, is_vid) in enumerate(todo, 1):
        sha = file_sha(p, st.st_size, index)
        old = c.execute("SELECT sha FROM files WHERE path=?", (p,)).fetchone()
        if sha in trashed:
            cnt["trashed content skipped"] += 1
            continue
        if c.execute("SELECT 1 FROM scans WHERE sha=? AND kind=?", (sha, spec.kind)).fetchone():
            c.execute("INSERT OR REPLACE INTO files(path, sha, size, mtime, skip) VALUES (?,?,?,?,NULL)",
                      (p, sha, st.st_size, st.st_mtime))
            cnt["same content as a scanned file"] += 1
        else:
            if det is None:
                det, embs = (engine or spec.engine)()
            source = frames(p) if is_vid else photo_frames(p, spec.max_side)
            n, size = ingest(c, spec.kind, sha, source, det, embs)
            if size is None:
                c.execute("INSERT OR REPLACE INTO scans(sha, kind, status, scanned) VALUES (?,?,?,?)",
                          (sha, spec.kind, "unreadable", today()))
                cnt["unreadable"] += 1
            else:
                c.execute("INSERT OR REPLACE INTO scans(sha, kind, status, w, h, scanned) VALUES (?,?,?,?,?,?)",
                          (sha, spec.kind, "ok", size[0], size[1], today()))
                cnt["videos scanned" if is_vid else "photos scanned"] += 1
                cnt[f"{spec.noun}s found"] += n
            c.execute("INSERT OR REPLACE INTO files(path, sha, size, mtime, skip) VALUES (?,?,?,?,NULL)",
                      (p, sha, st.st_size, st.st_mtime))
        if old and old["sha"] and old["sha"] != sha and not c.execute(
                "SELECT 1 FROM files WHERE sha=? LIMIT 1", (old["sha"],)).fetchone():
            forget_sha(c, old["sha"], d)  # the file's content changed: its old rows go
        if i % 50 == 0:
            c.commit()
            dt = time.monotonic() - t0
            log(f"  {i}/{len(todo)}  {dt / i:.2f} s a file, about {dt / i * (len(todo) - i) / 60:.0f} min left")
    c.commit()
    if todo:
        cnt["seconds a file"] = round((time.monotonic() - t0) / len(todo), 3)
    return +cnt


# ---------------------------------------------------------------- clustering (numpy only)
class UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, i):
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i


def owner_label(row):
    """The owner's name (or IGNORED) a detection carries, or None. Suggestions don't count."""
    return row["name"] if row["how"] in OWNER_HOW and row["name"] else None


def cluster_vectors(emb, labels, merges=(), photos=None, link=0.45, same=0.363, k=NEIGHBOURS, chunk=1024):
    """Component id per vector (numpy int array): two stages, numpy only.

    emb: N x D unit vectors. labels: the owner label of each (name, IGNORED or None); two different labels
    never end up together. photos: the photo or frame of each; two detections of one photo or frame are
    two individuals, so they never end up together either (owner merges, (i, j) index pairs, may). (1)
    links >= link among each vector's k nearest, strongest first, by union-find; (2) clusters merge while
    their average pairwise cosine is >= same. Callers cluster one species at a time."""
    import numpy as np
    n = len(emb)
    if n == 0:
        return np.zeros(0, dtype=int)
    uf = UF(n)
    lab = [{x} if x else set() for x in labels]
    pics = [{p} for p in photos] if photos is not None else [set() for _ in range(n)]

    def union(i, j, force=False):
        a, b = uf.find(i), uf.find(j)
        if a == b:
            return True
        if lab[a] and lab[b] and lab[a] != lab[b]:
            return False
        if not force and not pics[a].isdisjoint(pics[b]):
            return False
        if len(pics[a]) < len(pics[b]):
            a, b = b, a
        uf.p[b] = a
        lab[a] = lab[a] | lab[b]
        pics[a] |= pics[b]
        pics[b] = set()
        return True

    for i, j in merges:
        union(i, j, force=True)
    edges = set()
    kk = min(k, n - 1)
    for s in range(0, n if kk > 0 else 0, chunk):
        sim = emb[s:s + chunk] @ emb.T
        for r in range(sim.shape[0]):
            sim[r, s + r] = -1.0  # not its own neighbour
        idx = np.argpartition(-sim, kk - 1, axis=1)[:, :kk]
        for r in range(sim.shape[0]):
            for j in idx[r]:
                v = float(sim[r, j])
                if v >= link:
                    edges.add((v, min(s + r, int(j)), max(s + r, int(j))))
    for v, i, j in sorted(edges, reverse=True):
        union(i, j)
    roots = sorted({uf.find(i) for i in range(n)})
    pos = {r: t for t, r in enumerate(roots)}
    sums = np.zeros((len(roots), emb.shape[1]), dtype=np.float64)
    cnt = np.zeros(len(roots))
    for i in range(n):
        t = pos[uf.find(i)]
        sums[t] += emb[i]
        cnt[t] += 1
    cands = []
    for s in range(0, len(roots), chunk):
        avg = (sums[s:s + chunk] @ sums.T) / np.outer(cnt[s:s + chunk], cnt)
        rr, cc = np.nonzero(avg >= same)
        for a, b in zip(rr, cc):
            if s + a < b:
                cands.append((float(avg[a, b]), roots[s + a], roots[b]))
    S = {r: sums[pos[r]].copy() for r in roots}
    N = {r: cnt[pos[r]] for r in roots}
    for _, ra, rb in sorted(cands, reverse=True):
        a, b = uf.find(ra), uf.find(rb)
        if a == b or float(S[a] @ S[b]) / (N[a] * N[b]) < same:
            continue
        if union(a, b):
            r = uf.find(a)
            o = b if r == a else a
            S[r], N[r] = S[a] + S[b], N[a] + N[b]
            S.pop(o, None)
            N.pop(o, None)
    return np.array([uf.find(i) for i in range(n)])


def load_vectors(c, kind, model):
    """(rows, N x D float32 array) of kind's detections that have model's vector, by id."""
    import numpy as np
    rows = c.execute("SELECT d.*, e.vec FROM detections d JOIN embeddings e ON e.det=d.id AND e.model=? "
                     "WHERE d.kind=? ORDER BY d.id", (model, kind)).fetchall()
    if not rows:
        return rows, np.zeros((0, 1), np.float32)
    return rows, np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])


def frame_key(r):
    return f"{r['sha']}@{'' if r['t'] is None else repr(float(r['t']))}"


def rejected(c, ids):
    out = collections.defaultdict(set)
    for r in c.execute("SELECT det, name FROM rejects"):
        if r["det"] in ids:
            out[r["det"]].add(r["name"])
    return out


def recluster(c, spec, link=None):
    """Re-run clustering for spec.kind (each species on its own), renumber clusters by size (1 = largest)
    and suggest names: a detection in a cluster holding exactly one owner name gets it as how=suggested,
    unless the owner rejected that name for it. Owner decisions are never changed. Returns counts."""
    import numpy as np
    link = spec.link if link is None else link
    rows, emb = load_vectors(c, spec.kind, spec.model)
    ids = [r["id"] for r in rows]
    at = {fid: i for i, fid in enumerate(ids)}
    merges = [(at[a], at[b]) for a, b in c.execute("SELECT a, b FROM merges") if a in at and b in at]
    labels = [owner_label(r) for r in rows]
    comp = np.zeros(len(rows), dtype=int)
    by_species = collections.defaultdict(list)
    for i, r in enumerate(rows):
        by_species[r["species"] or ""].append(i)
    base = 0
    for sp, idx in sorted(by_species.items()):
        sub = {g: t for t, g in enumerate(idx)}
        part = cluster_vectors(emb[idx], [labels[i] for i in idx],
                               [(sub[a], sub[b]) for a, b in merges if a in sub and b in sub],
                               photos=[frame_key(rows[i]) for i in idx], link=link, same=spec.same)
        comp[idx] = part + base
        base += len(idx)
    groups = collections.defaultdict(list)
    for i, g in enumerate(comp):
        groups[int(g)].append(i)
    order = sorted(groups.values(), key=lambda g: (-len(g), ids[g[0]]))
    rej = rejected(c, set(ids))
    out = collections.Counter()
    for num, g in enumerate(order, 1):
        names = {labels[i] for i in g if labels[i]}
        name = names.pop() if len(names) == 1 else None
        for i in g:
            r = rows[i]
            nm, how = r["name"], r["how"]
            if how in OWNER_HOW or how == "out":
                pass  # the owner's: never changed by a tool
            elif name and name not in rej.get(ids[i], ()):
                nm, how = name, "suggested"
            else:
                nm, how = None, None
            c.execute("UPDATE detections SET cluster=?, name=?, how=? WHERE id=?", (num, nm, how, ids[i]))
            if how == "suggested" and nm != IGNORED:
                out["suggestions"] += 1
        out["clusters"] += 1
        if len(g) >= 2:
            out["clusters of 2+"] += 1
    set_meta(c, f"run:{spec.kind}", int(meta(c, f"run:{spec.kind}", 0)) + 1)
    set_meta(c, f"link:{spec.kind}", link)
    c.commit()
    out[f"{spec.noun}s"] = len(rows)
    return out


# ---------------------------------------------------------------- owner actions
def cluster_rows(c, kind, cl):
    rows = c.execute("SELECT * FROM detections WHERE kind=? AND cluster=?", (kind, cl)).fetchall()
    if not rows:
        raise ValueError(f"no cluster {cl} (run cluster first; numbers change on every run)")
    return rows


def check_name(name, prefixes=("People", "Pets")):
    name = " ".join((name or "").split())
    if not NAME_RE.match(name) or name.startswith(prefixes) or name.startswith("\x00"):
        raise ValueError("a name: 1-64 characters, no | / \\ , ; or control characters")
    return name


def same_case(c, kind, name):
    """An existing spelling wins over case and spacing variants ("ann" -> "Ann")."""
    for (p,) in c.execute("SELECT DISTINCT name FROM detections WHERE kind=? AND name IS NOT NULL AND name != ?",
                          (kind, IGNORED)):
        if p.casefold() == name.casefold():
            return p
    return name


def name_cluster(c, kind, cl, name):
    """The owner names (or ignores) a cluster: every detection in it but those taken out, or rejected for
    that name, gets it (how=cluster). Returns (named, in the cluster)."""
    name = name if name == IGNORED else same_case(c, kind, check_name(name))
    rows = cluster_rows(c, kind, cl)
    n = 0
    rej = rejected(c, {r["id"] for r in rows})
    for r in rows:
        if r["how"] == "out" or name in rej.get(r["id"], ()):
            continue
        c.execute("UPDATE detections SET name=?, how='cluster' WHERE id=?", (name, r["id"]))
        n += 1
    c.commit()
    return n, len(rows)


def unname_cluster(c, kind, cl):
    """Undo name or ignore: every name in the cluster but the taken-out ones goes (suggestions too)."""
    cluster_rows(c, kind, cl)
    n = c.execute("UPDATE detections SET name=NULL, how=NULL WHERE kind=? AND cluster=? AND "
                  "how IN ('cluster', 'confirmed', 'suggested')", (kind, cl)).rowcount
    c.commit()
    return n


def merge_clusters(c, kind, a, b):
    """The owner says cluster b is the same individual as cluster a. Kept across re-clustering. Refused
    across species and between two different owner names. Returns the name the merged cluster carries."""
    if a == b:
        raise ValueError("same cluster")
    ra, rb = cluster_rows(c, kind, a), cluster_rows(c, kind, b)
    if {r["species"] for r in ra} != {r["species"] for r in rb}:
        raise ValueError(f"cluster {a} and {b} are different species; they are never one individual")
    la = {owner_label(r) for r in ra} - {None}
    lb = {owner_label(r) for r in rb} - {None}
    if la and lb and la != lb:
        raise ValueError(f"cluster {a} and {b} carry different owner names; unname one first")
    name = (la or lb or {None}).pop()
    c.execute("INSERT OR IGNORE INTO merges(a, b) VALUES (?, ?)", (ra[0]["id"], rb[0]["id"]))
    c.execute("UPDATE detections SET cluster=? WHERE kind=? AND cluster=?", (a, kind, b))
    if name:
        rej = rejected(c, {r["id"] for r in ra + rb})
        for r in ra + rb:
            if r["how"] != "out" and name not in rej.get(r["id"], ()):
                c.execute("UPDATE detections SET name=?, how='cluster' WHERE id=?", (name, r["id"]))
    c.commit()
    return name


def _ids(c, kind, ids):
    ids = [int(i) for i in ids]
    have = {r[0] for r in c.execute(f"SELECT id FROM detections WHERE kind=? AND id IN ({','.join('?' * len(ids))})",
                                    (kind, *ids))} if ids else set()
    missing = [i for i in ids if i not in have]
    if missing:
        raise ValueError(f"no such detection: {', '.join(map(str, missing))}")
    return ids


def take_out(c, kind, ids):
    """The owner takes detections out of their name ("not this one"). Final: how=out."""
    ids = _ids(c, kind, ids)
    for i in ids:
        c.execute("UPDATE detections SET name=NULL, how='out' WHERE id=?", (i,))
    c.commit()
    return len(ids)


def put_back(c, kind, ids):
    """Undo take-out: the detection rejoins its cluster's owner name, if it has one. Returns how many."""
    n = 0
    for i in _ids(c, kind, ids):
        r = c.execute("SELECT cluster, how FROM detections WHERE id=?", (i,)).fetchone()
        if r["how"] != "out":
            continue
        named = c.execute("SELECT name FROM detections WHERE kind=? AND cluster=? AND how='cluster' AND "
                          "name IS NOT NULL LIMIT 1", (kind, r["cluster"])).fetchone()
        c.execute("UPDATE detections SET name=?, how=? WHERE id=?",
                  (named["name"], "cluster", i) if named else (None, None, i))
        n += 1
    c.commit()
    return n


def suggested_ids(c, kind, clusters=None, name=None):
    """Detection ids suggested a real name, optionally only in these clusters or for this name."""
    sql, args = "SELECT id FROM detections WHERE kind=? AND how='suggested' AND name != ?", [kind, IGNORED]
    if clusters:
        sql += f" AND cluster IN ({','.join('?' * len(clusters))})"
        args += list(clusters)
    if name:
        sql += " AND name=?"
        args.append(name)
    return [r[0] for r in c.execute(sql + " ORDER BY id", args)]


def confirm(c, kind, ids):
    """The owner confirms suggestions (bulk): they become owner names (how=confirmed), tagged by tag.
    Ids not currently suggested are left alone. Returns how many were confirmed."""
    ids = _ids(c, kind, ids)
    n = 0
    for i in ids:
        n += c.execute("UPDATE detections SET how='confirmed' WHERE id=? AND how='suggested' AND name != ?",
                       (i, IGNORED)).rowcount
    c.commit()
    return n


def reject(c, kind, ids):
    """The owner rejects suggestions (bulk): the name goes and is never suggested for that detection again.
    Returns how many were rejected."""
    ids = _ids(c, kind, ids)
    n = 0
    for i in ids:
        r = c.execute("SELECT name, how FROM detections WHERE id=?", (i,)).fetchone()
        if r["how"] != "suggested" or not r["name"]:
            continue
        c.execute("INSERT OR IGNORE INTO rejects(det, name) VALUES (?, ?)", (i, r["name"]))
        c.execute("UPDATE detections SET name=NULL, how=NULL WHERE id=?", (i,))
        n += 1
    c.commit()
    return n


# ---------------------------------------------------------------- tags into sidecars
def desired_tags(c, spec):
    """{path: {<prefix><Name>}} for every scanned file, from its owner-named detections of spec.kind."""
    want = collections.defaultdict(set)
    for r in c.execute("SELECT f.path FROM files f JOIN scans s ON s.sha=f.sha AND s.kind=? WHERE s.status='ok'",
                       (spec.kind,)):
        want[r["path"]]
    for r in c.execute("SELECT f.path, d.name FROM files f JOIN detections d ON d.sha=f.sha "
                       f"WHERE d.kind=? AND d.how IN ({','.join('?' * len(OWNER_HOW))}) AND d.name IS NOT NULL "
                       "AND d.name != ?", (spec.kind, *OWNER_HOW, IGNORED)):
        want[r["path"]].add(spec.prefix + r["name"])
    return want


def write_tags(c, spec, dry=False, reader=None, writer=None, state=None):
    """Bring spec.prefix tags in sidecars in line with the store. Idempotent; returns counts.

    Adds an owner name's tag (suggestions are never tags); removes only tags this tool wrote earlier (table
    `written`) that no longer apply; never re-adds a tag the owner removed from the sidecar (those
    detections become how=out); never removes a tag it didn't write (the owner's, digiKam's)."""
    import intake, label
    state = state or state_dir()
    reader = reader or intake.read_sidecar_tags
    writer = writer or intake.write_tags
    want = desired_tags(c, spec)
    written = collections.defaultdict(dict)
    for r in c.execute("SELECT path, tag, state FROM written WHERE substr(tag, 1, ?)=?", (len(spec.prefix), spec.prefix)):
        written[r["path"]][r["tag"]] = r["state"]
    paths = sorted(p for p in set(want) | set(written) if os.path.isfile(p))
    bad = set(label.refused(paths, state))
    paths = [p for p in paths if p not in bad]
    existing = reader(paths) if paths else {}
    entries, cnt, record = [], collections.Counter(), []
    for p in paths:
        got = existing.get(p) or ([], "", set())
        old, oleaves = got[0], (got[2] if len(got) > 2 else set())
        oldset = set(old)
        mine = written.get(p, {})
        add, rm = [], []
        for t in sorted(want.get(p, ())):
            st = mine.get(t)
            if t in oldset:
                continue  # already there: ours ("on") or the owner's own, which is never recorded or removed
            if st == "on":  # written, and the sidecar lost it: the owner removed it, i.e. "not them"
                record.append((p, t, "owner-removed"))
                if not dry:
                    c.execute("UPDATE detections SET name=NULL, how='out' WHERE kind=? AND name=? AND sha IN "
                              "(SELECT sha FROM files WHERE path=?)", (spec.kind, t[len(spec.prefix):], p))
                cnt["left off: the owner removed it"] += 1
                continue
            if st == "owner-removed":
                continue
            add.append(t)
            record.append((p, t, "on"))
        for t, st in mine.items():
            if t not in want.get(p, ()) and st != "owner-removed":
                if st == "on" and t in oldset:
                    rm.append(t)
                record.append((p, t, None))
        if not add and not rm:
            continue
        keep_leaves = ({t.split("|")[-1] for t in oldset if t not in rm} | {t.split("|")[-1] for t in add}
                       | set(oleaves))
        leaves = sorted({t.split("|")[-1] for t in rm} - keep_leaves)
        entries.append((p, {"hier": add, "remove": rm, "drop_leaves": leaves}))
        cnt["tags added"] += len(add)
        cnt["tags removed (renamed or unnamed)"] += len(rm)
    cnt["sidecars to change" if dry else "sidecars changed"] = len(entries)
    if bad:
        cnt["refused (flagged, erased, private)"] = len(bad)
    if dry:
        return cnt
    if entries:
        writer(entries)
    for p, t, st in record:
        if st is None:
            c.execute("DELETE FROM written WHERE path=? AND tag=?", (p, t))
        else:
            c.execute("INSERT OR REPLACE INTO written(path, tag, state) VALUES (?,?,?)", (p, t, st))
    c.commit()
    return cnt


# ---------------------------------------------------------------- read: find, names, clusters, suggestions
def find(c, kind, name, alone=False, min_box=0.0, suggested=False, state=None):
    """[(largest box share of the frame, detections of kind in the file, path)] of that individual's files,
    largest first. Owner names only, unless suggested (then suggestions count too)."""
    import label
    want = name.casefold()
    hows = OWNER_HOW + (("suggested",) if suggested else ())
    rows = c.execute("SELECT f.path, d.name, d.how, d.w * d.h AS area FROM files f JOIN detections d ON d.sha=f.sha "
                     "WHERE d.kind=?", (kind,)).fetchall()
    per = collections.defaultdict(list)
    for r in rows:
        per[r["path"]].append(r)
    out = []
    for p, ds in per.items():
        mine = [d for d in ds if (d["name"] or "").casefold() == want and d["how"] in hows]
        if not mine:
            continue
        big = max(d["area"] for d in mine)
        if big < min_box or (alone and len(ds) > 1):
            continue
        out.append((big, len(ds), p))
    bad = set(label.refused([o[2] for o in out], state or state_dir()))
    out = [o for o in out if o[2] not in bad and os.path.isfile(o[2])]
    return sorted(out, key=lambda o: (-o[0], o[2]))


def names(c, kind):
    """[(name, files with an owner name, suggestions waiting)] by file count."""
    out = collections.defaultdict(lambda: [set(), 0])
    for r in c.execute("SELECT d.name, d.how, f.path FROM detections d JOIN files f ON f.sha=d.sha "
                       "WHERE d.kind=? AND d.name IS NOT NULL AND d.name != ?", (kind, IGNORED)):
        if r["how"] in OWNER_HOW:
            out[r["name"]][0].add(r["path"])
        elif r["how"] == "suggested":
            out[r["name"]][1] += 1
    return sorted(((n, len(v[0]), v[1]) for n, v in out.items()), key=lambda x: (-x[1], x[0]))


def clusters(c, kind, min_size=2, unnamed=False, samples=6):
    """[dict(id, species, count, photos, name, ignored, how, suggested, crops)] by cluster number. how: "owner"
    when an owner name is in it, "suggested" when only suggestions; crops: up to `samples` detection ids
    (highest score first) for crop_bytes()."""
    out = []
    for r in c.execute("SELECT cluster, COUNT(*) n, COUNT(DISTINCT sha) photos, MIN(species) species FROM detections "
                       "WHERE kind=? AND cluster IS NOT NULL GROUP BY cluster ORDER BY cluster", (kind,)).fetchall():
        if r["n"] < min_size:
            continue
        ds = c.execute("SELECT id, name, how FROM detections WHERE kind=? AND cluster=? ORDER BY score DESC, id",
                       (kind, r["cluster"])).fetchall()
        owner = [d["name"] for d in ds if d["how"] in OWNER_HOW and d["name"]]
        sugg = [d["name"] for d in ds if d["how"] == "suggested" and d["name"]]
        name = (owner or sugg or [None])[0]
        if unnamed and name:
            continue
        out.append(dict(id=r["cluster"], species=r["species"], count=r["n"], photos=r["photos"],
                        name=None if name == IGNORED else name, ignored=name == IGNORED,
                        how="owner" if owner else "suggested" if sugg else None,
                        suggested=sum(1 for d in ds if d["how"] == "suggested" and d["name"] != IGNORED),
                        crops=[d["id"] for d in ds[:samples]]))
    return out


def suggestions(c, kind, cluster=None):
    """[dict(det, cluster, name, sha, t, score, path)] of detections waiting for confirm / reject."""
    sql = ("SELECT d.id, d.cluster, d.name, d.sha, d.t, d.score, (SELECT path FROM files f WHERE f.sha=d.sha "
           "ORDER BY path LIMIT 1) path FROM detections d WHERE d.kind=? AND d.how='suggested' AND d.name != ?")
    args = [kind, IGNORED]
    if cluster is not None:
        sql += " AND d.cluster=?"
        args.append(cluster)
    return [dict(det=r["id"], cluster=r["cluster"], name=r["name"], sha=r["sha"], t=r["t"], score=r["score"],
                 path=r["path"]) for r in c.execute(sql + " ORDER BY d.cluster, d.id", args)]


def cluster_hints(c, spec, min_size=2, top=3):
    """{unnamed cluster: [(named cluster, name, average cosine)]} for clusters close to a named one (>= same):
    "looks like <Name>", for the owner to merge. Same species only."""
    rows, emb = load_vectors(c, spec.kind, spec.model)
    if not rows:
        return {}
    info = {d["id"]: d for d in clusters(c, spec.kind, min_size=1)}
    named = [k for k, d in info.items() if d["name"] and d["how"] == "owner"]
    todo = [k for k, d in info.items() if not d["name"] and not d["ignored"] and d["count"] >= min_size]
    if not named or not todo:
        return {}
    import numpy as np
    cl = np.array([r["cluster"] or 0 for r in rows])
    S = {k: emb[cl == k].sum(0) for k in set(named) | set(todo)}
    N = {k: max(1, int((cl == k).sum())) for k in S}
    out = {}
    for u in todo:
        sc = sorted(((float(S[u] @ S[k]) / (N[u] * N[k]), k) for k in named
                     if info[k]["species"] == info[u]["species"]), reverse=True)
        best = [(k, info[k]["name"], round(v, 3)) for v, k in sc[:top] if v >= spec.same]
        if best:
            out[u] = best
    return out


# ---------------------------------------------------------------- crops (the gallery's naming view)
def crop_bytes(c, det_id, frames="default", d=None, state=None):
    """A 160 px JPEG of detection det_id (bytes), made on demand and cached 0600 under crops/, or None
    (no such detection, refused or trashed content, unreadable file)."""
    import label
    r = c.execute("SELECT d.*, (SELECT path FROM files f WHERE f.sha=d.sha AND f.skip IS NULL ORDER BY path LIMIT 1) "
                  "path FROM detections d WHERE d.id=?", (int(det_id),)).fetchone()
    if not r or not r["path"] or not SHA_RE.fullmatch(r["sha"] or ""):
        return None
    state = state or state_dir()
    if r["sha"] in trashed_shas(state) or label.refused([r["path"]], state):
        return None
    dst = os.path.join(crops_dir(d), f"{r['sha']}-{int(det_id)}.jpg")
    if os.path.isfile(dst):
        with open(dst, "rb") as f:
            return f.read()
    if r["t"] is None:
        rgb = load_rgb(r["path"], max_side=4096)
    else:
        rgb = None
        frames = default_frames() if frames == "default" else frames
        for t, img in (frames(r["path"]) if frames else ()):
            if t is not None and abs(float(t) - r["t"]) < 1e-3:
                rgb = img
                break
    if rgb is None:
        return None
    import numpy as np
    Image, _ = _pillow()
    H, W = rgb.shape[:2]
    box = (r["x"] * W, r["y"] * H, r["w"] * W, r["h"] * H)
    sq = square_crop(rgb, box, margin=0.35 if r["kind"] == "face" else MARGIN)
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(sq)).resize((CROP_PX, CROP_PX), Image.BILINEAR).save(buf, "JPEG", quality=82)
    private_dir(os.path.dirname(dst))
    tmp = f"{dst}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    os.replace(tmp, dst)
    return buf.getvalue()


# ---------------------------------------------------------------- eval (read-only)
CALIBRATE = "Calibrate|"


def calibration_labels(c, spec, reader=None):
    """{detection id: name} from Calibrate|<command>|<Name> sidecar tags: only files with exactly one
    detection of spec.kind (more would be ambiguous), and one such name."""
    import intake
    reader = reader or intake.read_sidecar_tags
    pre = f"{CALIBRATE}{spec.command}|".casefold()
    paths = [r[0] for r in c.execute("SELECT DISTINCT f.path FROM files f JOIN detections d ON d.sha=f.sha "
                                     "WHERE d.kind=?", (spec.kind,)) if os.path.isfile(r[0])]
    tags = reader(paths) if paths else {}
    out = {}
    for p in paths:
        ns = {t[len(pre):] for t in (tags.get(p) or ([],))[0] if t.casefold().startswith(pre) and t[len(pre):]}
        if len(ns) != 1:
            continue
        ds = c.execute("SELECT d.id FROM detections d JOIN files f ON f.sha=d.sha WHERE f.path=? AND d.kind=?",
                       (p, spec.kind)).fetchall()
        if len(ds) == 1:
            out[ds[0][0]] = ns.pop()
    return out


def evaluate(c, spec, labels, models=None, link=None):
    """Per embedding model: pairs, ROC AUC and equal error rate over same / different pairs of labelled
    detections, median same-individual cosine, the closest different pair, leave-one-out nearest-neighbour
    accuracy, and at the link threshold how many clusters each individual splits into and how many clusters
    mix two. Numbers only; nothing is changed or shown."""
    import numpy as np
    link = spec.link if link is None else link
    models = models or [r[0] for r in c.execute("SELECT DISTINCT e.model FROM embeddings e JOIN detections d "
                                                "ON d.id=e.det WHERE d.kind=? ORDER BY e.model", (spec.kind,))]
    out = {}
    for model in models:
        rows, emb = load_vectors(c, spec.kind, model)
        keep = [i for i, r in enumerate(rows) if r["id"] in labels]
        res = dict(labelled=len(keep), individuals=len({labels[rows[i]["id"]] for i in keep}))
        if len(keep) < 2:
            out[model] = res
            continue
        X = emb[keep]
        y = [labels[rows[i]["id"]] for i in keep]
        sim = X @ X.T
        iu = np.triu_indices(len(keep), 1)
        same = np.array([y[a] == y[b] for a, b in zip(*iu)])
        s = sim[iu]
        pos, neg = s[same], s[~same]
        res.update(same_pairs=int(same.sum()), different_pairs=int((~same).sum()))
        if len(pos) and len(neg):
            order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
            ranks = np.empty(len(order))
            ranks[order] = np.arange(1, len(order) + 1)
            res["roc_auc"] = round(float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2)
                                         / (len(pos) * len(neg))), 4)
            best = (2.0, None)
            for th in np.unique(np.concatenate([pos, neg])):
                frr, far = float((pos < th).mean()), float((neg >= th).mean())
                if abs(frr - far) < best[0]:
                    best = (abs(frr - far), (frr + far) / 2, float(th))
            res["eer"], res["eer_threshold"] = round(best[1], 4), round(best[2], 4)
            res["median_same"], res["closest_different"] = round(float(np.median(pos)), 4), round(float(neg.max()), 4)
        np.fill_diagonal(sim, -2.0)
        nn = sim.argmax(1)
        res["nearest_neighbour_accuracy"] = round(float(np.mean([y[i] == y[j] for i, j in enumerate(nn)])), 4)
        comp = cluster_vectors(X, [None] * len(keep), photos=[frame_key(rows[i]) for i in keep], link=link,
                               same=spec.same)
        per = collections.defaultdict(set)
        mixed = collections.defaultdict(set)
        for i, g in enumerate(comp):
            per[y[i]].add(int(g))
            mixed[int(g)].add(y[i])
        res["clusters_per_individual"] = {k: len(v) for k, v in sorted(per.items())}
        res["mixed_clusters"] = sum(1 for v in mixed.values() if len(v) > 1)
        out[model] = res
    return out


# ---------------------------------------------------------------- CLI (people.py, pets.py)
def _help(what, touches, deletes):
    return f"{what}\n\nTouches: {touches}\nDeletes: {deletes}"


def build_parser(spec, prog=None):
    import argparse
    prog = prog or f"media-intake {spec.command}"
    n, cmd = spec.noun, spec.command
    ap = argparse.ArgumentParser(
        prog=prog, formatter_class=argparse.RawDescriptionHelpFormatter,
        description=_help(f"Recognise {cmd} in your photos, offline on this laptop. You name them; the tool\n"
                          "only suggests, and writes a name as a tag only once you named or confirmed it.\n"
                          "Off until bash setup.sh --recognition installs the models (docs/recognition.md).",
                          f"reads the library; writes the recognition store (biometric, 0600, never\nbacked up) "
                          f"and, with tag, {spec.prefix}<Name> in XMP sidecars. Never writes a\nphoto.",
                          "no photo or video. Rows of files trashed, erased or flagged since\n"
                          "are dropped from the store before every command."))
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    def p(name, help_, desc):
        return sub.add_parser(name, help=help_, description=desc)
    s = p("scan", f"find {n}s in new photos (and videos, with a frame source)",
          f"Find {n}s in photos not scanned yet and store a vector for each. Incremental; --limit resumes later.")
    s.add_argument("files", nargs="*", help="photos to scan instead of the library")
    s.add_argument("--root", action="append", help="a folder to scan (default: library, videos, project folders)")
    s.add_argument("--limit", type=int, help="at most N new files this run (run again to go on)")
    s = p("cluster", f"group the {n}s that look alike", f"Group {n}s that look alike and suggest owner names.")
    s.add_argument("--link", type=float, help=f"link threshold (cosine; default {spec.link})")
    s = p("clusters", "list the groups", "List clusters: number, count, photos, name.")
    s.add_argument("--min", type=int, default=2, help="only clusters with at least N (default 2)")
    s.add_argument("--unnamed", action="store_true", help="only clusters with no name yet")
    s = p("name", "name a cluster", "Name a cluster (your decision: written as a tag by tag).")
    s.add_argument("cluster", type=int, help="the cluster number (from clusters)")
    s.add_argument("name", nargs="+", help="the name, e.g. Ann")
    s = p("merge", "say two clusters are the same", "Merge clusters: OTHER joins CLUSTER (kept across runs).")
    s.add_argument("cluster", type=int, help="the cluster that stays")
    s.add_argument("other", type=int, nargs="+", help="clusters that join it")
    s = p("ignore", "not someone you know", "Ignore clusters: strangers, posters, statues. Never tagged.")
    s.add_argument("cluster", type=int, nargs="+", help="cluster numbers")
    s = p("unname", "undo name or ignore", "Undo name or ignore for these clusters.")
    s.add_argument("cluster", type=int, nargs="+", help="cluster numbers")
    s = p("take-out", "not this one", "Take detections out of their name (final until put-back).")
    s.add_argument("det", type=int, nargs="+", help="detection ids (from suggestions or the gallery)")
    s = p("put-back", "undo take-out", "Put taken-out detections back into their cluster's name.")
    s.add_argument("det", type=int, nargs="+", help="detection ids")
    s = p("suggestions", "list suggestions waiting", "List suggested names waiting for confirm or reject.")
    s.add_argument("--cluster", type=int, help="only this cluster")
    for name, verb in (("confirm", "confirm"), ("reject", "reject")):
        s = p(name, f"{verb} suggested names",
              f"{verb.capitalize()} suggestions: by cluster, by detection id (--det) or every one (--all).")
        s.add_argument("cluster", type=int, nargs="*", help="cluster numbers whose suggestions to take")
        s.add_argument("--det", type=int, action="append", default=[], help="a detection id (repeatable)")
        s.add_argument("--all", action="store_true", help="every suggestion waiting")
    s = p("tag", f"write {spec.prefix}<Name> tags to sidecars", f"Write {spec.prefix}<Name> into XMP sidecars "
          "for owner names; removes only tags it wrote; never re-adds one you removed.")
    s.add_argument("--dry-run", action="store_true", help="count what would change; write nothing")
    s = p("find", "files with this name", "List files with this name, largest first.")
    s.add_argument("name", nargs="+", help="the name")
    s.add_argument("--alone", action="store_true", help=f"only files with no other {n} in them")
    s.add_argument("--min-box", type=float, default=0.0, help="the box's share of the frame, e.g. 0.02")
    s.add_argument("--suggested", action="store_true", help="count suggestions too, not just your names")
    s.add_argument("--limit", type=int, help="at most N files")
    p(cmd, "names and photo counts", "List the names, with file counts and suggestions waiting.")
    s = p("eval", "how well it tells them apart (read-only)",
          f"Score the vectors on files you tagged Calibrate|{cmd}|<Name> (one {n} in the file). Read-only.")
    s.add_argument("--link", type=float, help=f"link threshold to test (default {spec.link})")
    return ap


def _counts(cnt):
    return ", ".join(f"{k} {v}" for k, v in cnt.items() if v)


def main(spec, argv=None, prog=None):
    a = build_parser(spec, prog).parse_args(argv)
    tool = f"media-intake {spec.command}"
    missing = spec.missing()
    if missing:
        sys.exit(f"{tool} is off: its models are not installed ({len(missing)} missing). Turn it on with "
                 "bash setup.sh --recognition (docs/recognition.md)")
    try:
        check_location(store_dir())
    except ValueError as e:
        sys.exit(f"{tool}: {e}")
    c = connect()
    pr = prune(c)
    if pr:
        print("dropped from the recognition store: " + _counts(pr))
    try:
        if a.cmd == "scan":
            roots = a.root or ([] if a.files else [r for r in config.library_roots() if os.path.isdir(r)])
            paths = candidates([r for r in roots if os.path.isdir(r)], a.files)
            print(f"{len(paths)} files to look at", flush=True)
            print("scan: " + _counts(scan(c, spec, paths, limit=a.limit)))
            print(f"next: {tool} cluster, then name the clusters ({tool} clusters, {tool} name N NAME)")
        elif a.cmd == "cluster":
            t0 = time.monotonic()
            print("cluster: " + _counts(recluster(c, spec, a.link)) + f" ({time.monotonic() - t0:.1f} s)")
        elif a.cmd == "clusters":
            for d in clusters(c, spec.kind, a.min, a.unnamed):
                who = "ignored" if d["ignored"] else (d["name"] or "-") + (f" ({d['how']})" if d["how"] else "")
                sp = f"  {d['species']}" if d["species"] else ""
                print(f"{d['id']:>6}  {d['count']:>5} {spec.noun}s  {d['photos']:>5} files{sp}  {who}")
        elif a.cmd == "name":
            n, tot = name_cluster(c, spec.kind, a.cluster, " ".join(a.name))
            print(f"cluster {a.cluster}: {n} of {tot} named; {tool} tag writes the sidecars")
        elif a.cmd == "merge":
            for o in a.other:
                name = merge_clusters(c, spec.kind, a.cluster, o)
                print(f"cluster {o} joined {a.cluster}" + (f" ({'ignored' if name == IGNORED else name})" if name else ""))
        elif a.cmd == "ignore":
            for cl in a.cluster:
                print(f"cluster {cl}: {name_cluster(c, spec.kind, cl, IGNORED)[0]} ignored")
        elif a.cmd == "unname":
            for cl in a.cluster:
                print(f"cluster {cl}: {unname_cluster(c, spec.kind, cl)} unnamed")
        elif a.cmd == "take-out":
            print(f"{take_out(c, spec.kind, a.det)} taken out")
        elif a.cmd == "put-back":
            print(f"{put_back(c, spec.kind, a.det)} put back")
        elif a.cmd == "suggestions":
            rows = suggestions(c, spec.kind, a.cluster)
            for s in rows:
                print(f"{s['det']:>8}  cluster {s['cluster']:>5}  {s['name']}")
            print(f"{len(rows)} suggestions (detection id, cluster, name)", file=sys.stderr)
        elif a.cmd in ("confirm", "reject"):
            if not (a.cluster or a.det or a.all):
                sys.exit(f"{tool} {a.cmd}: name clusters, --det ID or --all")
            ids = set(a.det)
            if a.all or a.cluster:
                ids |= set(suggested_ids(c, spec.kind, None if a.all else a.cluster))
            ids = sorted(ids)
            n = (confirm if a.cmd == "confirm" else reject)(c, spec.kind, ids)
            print(f"{n} suggestions {a.cmd}ed" + (f"; {tool} tag writes the sidecars" if a.cmd == "confirm" else ""))
        elif a.cmd == "tag":
            cnt = write_tags(c, spec, dry=a.dry_run)
            print(("tag (dry run): " if a.dry_run else "tag: ")
                  + ", ".join(f"{k} {v}" for k, v in cnt.items() if v or k.startswith("sidecars")))
        elif a.cmd == "find":
            hits = find(c, spec.kind, check_name(" ".join(a.name)), a.alone, a.min_box, a.suggested)
            for big, n, path in hits[:a.limit]:
                print(f"{big:.3f}  {n}  {path}")
            print(f"{len(hits)} files (box share of the frame, {spec.noun}s in it, path)", file=sys.stderr)
        elif a.cmd == spec.command:
            for name, n, waiting in names(c, spec.kind):
                print(f"{n:>6} files  {name}" + (f"  ({waiting} suggestions waiting)" if waiting else ""))
        elif a.cmd == "eval":
            labels = calibration_labels(c, spec)
            res = evaluate(c, spec, labels, link=a.link)
            print(json.dumps(res, indent=1, sort_keys=True))
            if not labels:
                print(f"no labelled files: tag some Calibrate|{spec.command}|<Name> in the gallery (one {spec.noun} "
                      "in the file)", file=sys.stderr)
    except ValueError as e:
        sys.exit(f"{tool}: {e}")
    finally:
        c.close()
    return 0
