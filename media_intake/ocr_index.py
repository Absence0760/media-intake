#!/usr/bin/env python3
"""The private text index: what the words in your photos say, for `media-intake search --text`.

The sensitive-content scan (scan_sensitive.py) already reads every photo's text with Tesseract to look for
IDs, cards and secret words. For a photo it found nothing in, that text is kept here, keyed by the photo's
sha256, so search can find screenshots, receipts and signs by what they say (docs/search.md, "Text in
photos"). `media-intake index --text` reads the text of library photos scanned before this index existed.

  <state>/ocr-text.db   sqlite, 0600 in the 0700 state folder; one row per content: sha256, size, text.

Never indexed: a file the scan flagged for any reason (an "unscanned-*" label included), anything
quarantined, erased or trashed (label.flagged, trashed.json). search() is read-only: it opens the store
read-only and leaves out refused content in memory. Rows of content flagged since are dropped by
`index --text` (prune, before it reads anything new) and by erase, forget, trash and sweep-erased (forget);
verify-gone counts what is left (leftovers).
Deletes overwrite the freed pages (secure_delete). Stdlib only: the system Python and the venv both run it.
"""
import json, os, re, sqlite3, sys, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.append(HERE)
import config  # noqa: E402

SHA_RE = re.compile(r"[0-9a-f]{64}")
MAX_TEXT = 20000  # characters kept per content; a photo of a page holds a few thousand
PLACE = "text index of photos (state folder, ocr-text.db)"  # erase's coverage report and verify-gone


def state_dir():
    return config.get()["state"]


def db_path(state=None):
    return os.path.join(state or state_dir(), "ocr-text.db")


def clean(text):
    """The text as kept: whitespace runs collapsed to one space, at most MAX_TEXT characters."""
    return " ".join((text or "").split())[:MAX_TEXT]


def connect(path, create=True):
    """The store, created 0600 (its folder 0700) when create; None when it is not there and not created."""
    if not os.path.isfile(path):
        if not create:
            return None
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)
    c = sqlite3.connect(path, timeout=30)
    try:
        c.execute("PRAGMA secure_delete=ON")  # a dropped row's text is overwritten, not left in a free page
        c.execute("CREATE TABLE IF NOT EXISTS text (sha TEXT PRIMARY KEY, size INTEGER, body TEXT)")
    except sqlite3.Error:
        c.close()
        raise
    return c


def refused_shas(state=None):
    """sha256s that must never be kept: flagged, quarantined or erased (label.flagged) and trashed."""
    import label
    state = state or state_dir()
    out = set(label.flagged(state))
    try:
        with open(os.path.join(state, "trashed.json")) as f:
            out |= set(json.load(f) or {})
    except (FileNotFoundError, ValueError):
        pass  # nothing trashed yet
    return out


def add(rows, state=None):
    """Keep [(sha, size, text)]: rows with no text, a bad sha or refused content are left out. Returns the
    number kept. The caller passes only content the scan found nothing in."""
    refused = refused_shas(state)
    keep = [(s, int(n), clean(t)) for s, n, t in rows
            if SHA_RE.fullmatch(s or "") and s not in refused and clean(t)]
    if not keep:
        return 0
    c = connect(db_path(state))
    try:
        c.executemany("INSERT OR REPLACE INTO text VALUES (?, ?, ?)", keep)
        c.commit()
    finally:
        c.close()
    return len(keep)


def known(state=None):
    """sha256s with a row (index --text skips them)."""
    c = connect(db_path(state), create=False)
    if c is None:
        return set()
    try:
        return {r[0] for r in c.execute("SELECT sha FROM text")}
    finally:
        c.close()


def forget(shas, state=None):
    """erase, forget, trash and sweep-erased: drop the rows of these sha256s. Never creates the store.
    Returns how many rows went; -1 when the store can't be read (the caller says so)."""
    shas = sorted({s for s in shas if SHA_RE.fullmatch(s or "")})
    p = db_path(state)
    if not shas or not os.path.isfile(p):
        return 0
    try:
        c = connect(p)
        try:
            n = 0
            for i in range(0, len(shas), 500):
                part = shas[i:i + 500]
                n += c.execute(f"DELETE FROM text WHERE sha IN ({','.join('?' * len(part))})", part).rowcount
            c.commit()
            return n
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return -1


def leftovers(shas, state=None):
    """verify-gone: rows of these sha256s still in the store (read-only); -1 when it can't be read."""
    shas = sorted({s for s in shas if SHA_RE.fullmatch(s or "")})
    p = db_path(state)
    if not shas or not os.path.isfile(p):
        return 0
    try:
        c = sqlite3.connect("file:" + urllib.parse.quote(p) + "?mode=ro", uri=True, timeout=30)
        try:
            n = 0
            for i in range(0, len(shas), 500):
                part = shas[i:i + 500]
                try:
                    n += c.execute(f"SELECT COUNT(*) FROM text WHERE sha IN ({','.join('?' * len(part))})",
                                   part).fetchone()[0]
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e):
                        raise  # unreadable (locked, permissions): never "nothing left"
                    return 0  # no table yet: nothing there
            return n
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return -1


def prune(state=None):
    """Drop rows of content flagged, quarantined, erased or trashed since it was kept. Returns the count."""
    return max(0, forget(known(state) & refused_shas(state), state))


def words(query):
    return re.findall(r"\w+", (query or "").casefold())


def search(query, state=None):
    """{sha: hits} of content whose text holds every word of query (case-insensitive, a word matching at the
    start of a word: "rec" finds "Receipt"); hits counts how often the words appear. Read-only: the store is
    opened read-only and never created, and content flagged, quarantined, erased or trashed since it was kept
    is left out here, in memory (index --text, erase and trash drop its row)."""
    ws = words(query)
    p = db_path(state)
    if not ws or not os.path.isfile(p):
        return {}
    refused = refused_shas(state)
    rxs = [re.compile(r"\b" + re.escape(w)) for w in ws]
    out = {}
    c = sqlite3.connect("file:" + urllib.parse.quote(p) + "?mode=ro", uri=True, timeout=30)
    try:
        try:
            rows = c.execute("SELECT sha, body FROM text")
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {}  # an empty store: nothing kept yet
            raise
        for sha, body in rows:
            if sha in refused:
                continue
            low = body.casefold()
            counts = [len(rx.findall(low)) for rx in rxs]
            if all(counts):
                out[sha] = sum(counts)
    finally:
        c.close()
    return out


def backfill(state=None, ocr=None):
    """index --text: read the text of library photos (index.json) with no row yet and keep it, as the scan
    would. Refused content is never opened; a photo whose text trips a sensitive check (an ID, a card, secret
    words) is not kept and only counted. ocr(im, tmpdir, keep) is scan_sensitive.ocr_flags (tests pass a fake).
    Returns counts only."""
    import shutil, tempfile
    from PIL import Image
    import label, scan_sensitive
    state = state or state_dir()
    if ocr is None:
        if not shutil.which("tesseract"):
            sys.exit("index --text needs tesseract: sudo dnf install tesseract tesseract-langpack-eng")
        ocr = scan_sensitive.ocr_flags
    try:
        with open(os.path.join(state, "index.json")) as f:
            lib = json.load(f) or {}
    except (FileNotFoundError, ValueError):
        lib = {}
    pruned = prune(state)  # rows of content flagged since: search leaves them out, this drops them
    have, refused = known(state), refused_shas(state)
    todo = {}
    for p, e in sorted(lib.items()):
        s = e.get("sha") if isinstance(e, dict) else None
        if (SHA_RE.fullmatch(s or "") and s not in have and s not in refused and s not in todo
                and os.path.splitext(p)[1].lower() in scan_sensitive.IMG and os.path.isfile(p)
                and os.path.getsize(p) == e.get("size")):
            todo[s] = p
    out = {"read": 0, "kept": 0, "no text": 0, "sensitive, not kept": 0, "unreadable": 0, "refused": 0,
           "pruned": pruned}
    work = os.path.join(os.path.expanduser("~"), ".cache", "media-intake")
    os.makedirs(work, mode=0o700, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=work)  # 0700: the grey copy tesseract reads
    rows = []
    try:
        for s, p in todo.items():
            if label.refused([p], state):
                out["refused"] += 1
                continue
            keep = []
            try:
                with Image.open(p) as im:
                    im.load()
                    flags = ocr(im, tmp, keep)
            except Exception:
                out["unreadable"] += 1
                continue
            out["read"] += 1
            if flags:
                out["sensitive, not kept"] += 1
            elif not clean("\n".join(keep)):
                out["no text"] += 1
            else:
                rows.append((s, os.path.getsize(p), "\n".join(keep)))
            if len(rows) >= 100:
                out["kept"] += add(rows, state)
                rows = []
        out["kept"] += add(rows, state)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
