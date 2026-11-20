#!/usr/bin/env python3
"""Re-file date-fixed library files into the folder their corrected date belongs in.

  media-intake refile                 a dry run: each file's old and new place
  media-intake refile --yes           move them (each with its .xmp sidecar)
  media-intake refile --undo [ID]     put one earlier refile back (a dry run too; --yes to move)

fix-dates corrects a date in the sidecar only, so the file stays in its old YYYY/YYYY-MM folder. This moves
each file a fix-dates write covers (date-fixes.json, the fix no undo took back) whose folder disagrees with
its fixed date: the dated parts of its path (a YYYY folder, and a YYYY-MM folder under it) under the library
or videos folder change, the rest (a camera's device folder, the name) stays. Project folders are named by day
and event, not re-filed. A move is a rename on the same filesystem (a hard link then an unlink, so a file
already at the destination is never replaced); a destination taken by another file gets the next free
<stem>_<n> name, as `apply` does. Every record that names the old path is rekeyed (records(), below) and
each run is logged in <state>/refiles.json (0600) for --undo; erase, forget, trash and sweep-erased drop a
file's moves from it (forget_paths). Runs under the library lock (intake.LOCKED), the dry run too: it reads
date-fixes.json and the library, which another job may be changing. Output is paths and counts.
docs/dates.md, "Re-filing a fixed file".
"""
import collections, datetime, errno, glob, os, re, secrets, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import intake  # noqa: E402
import datecheck  # noqa: E402
import label  # noqa: E402

LOG = "refiles.json"
VERSION = 1
YEAR_RX = re.compile(r"^(19|20)\d\d$")
MONTH_RX = re.compile(r"^((19|20)\d\d)-(0[1-9]|1[0-2])$")
PROGRESS = ("vlm-progress.json", "captions-progress.json")  # vlm-trash's and captions' resume records, by path
NO_LINK = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EMLINK, errno.ENOSYS}  # exFAT, some FUSE mounts


class CrossDevice(OSError):
    """The destination is on another filesystem: a move would be a copy and a delete. Refused."""


def log_path(state=None):
    return os.path.join(state or intake.STATE, LOG)


def load_log(state=None):
    try:
        d = intake.load(log_path(state), None)
    except (OSError, ValueError):
        d = None
    if not isinstance(d, dict) or d.get("version") != VERSION or not isinstance(d.get("refiles"), list):
        return dict(version=VERSION, refiles=[])
    return d


def _read_log(state):
    """(log, "") for forget_paths and stale_count, which rewrite it: (None, why) when it can't be read, (None,
    "") when there is none."""
    p = log_path(state)
    if not os.path.lexists(p):
        return None, ""
    try:
        d = intake.load(p, None)
    except (OSError, ValueError):
        d = None
    if not isinstance(d, dict) or d.get("version") != VERSION or not isinstance(d.get("refiles"), list):
        return None, f"{LOG} is unreadable: its records were not checked"
    return d, ""


def _histories(log):
    """{path: root}: one file's moves link up (a refile of a refiled file, an undo, a refile again), so every
    path a file was known by shares one root."""
    parent = {}

    def find(p):
        parent.setdefault(p, p)
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p
    for rec in log["refiles"]:
        for mv in rec.get("moves", []) if isinstance(rec, dict) else []:
            if isinstance(mv, dict) and isinstance(mv.get("from"), str) and isinstance(mv.get("to"), str):
                parent[find(mv["from"])] = find(mv["to"])
    return {p: find(p) for p in list(parent)}


def _dead(log, paths, gone):
    """(roots of the histories to drop, {path: root}): those naming one of paths, and (gone: path -> bool)
    those whose every path is gone."""
    roots = _histories(log)
    members = collections.defaultdict(set)
    for p, r in roots.items():
        members[r].add(p)
    return {r for r, ps in members.items() if ps & paths or (gone and all(gone(p) for p in ps))}, roots


def forget_paths(paths, state=None, gone=None):
    """Erase / forget / trash / sweep-erased: drop every move of these files from refiles.json, with the rest of
    each one's history (its earlier paths), and (gone: path -> bool) every history whose paths are all gone.
    A refile left with no move goes. Rewritten atomically, 0600; the caller holds the library lock. Returns
    (moves dropped, "" or why not). Never raises."""
    state = state or intake.STATE
    log, why = _read_log(state)
    if log is None:
        return 0, why
    dead, roots = _dead(log, {p for p in paths if isinstance(p, str)}, gone)
    if not dead:
        return 0, ""
    n, keep = 0, []
    for rec in log["refiles"]:
        old = rec.get("moves", []) if isinstance(rec, dict) else []
        moves = [mv for mv in old if not (isinstance(mv, dict) and roots.get(mv.get("from")) in dead)]
        n += len(old) - len(moves)
        if moves:
            keep.append(dict(rec, moves=moves))
    try:
        datecheck.save_private(log_path(state), dict(log, refiles=keep))
    except OSError as e:
        return 0, f"{LOG}: {e.strerror or e}"
    return n, ""


def stale_count(gone, state=None):
    """verify-gone: moves in refiles.json of files whose every path is gone."""
    log, _ = _read_log(state or intake.STATE)
    if log is None:
        return 0
    dead, roots = _dead(log, set(), gone)
    return sum(1 for rec in log["refiles"] if isinstance(rec, dict) for mv in rec.get("moves", [])
               if isinstance(mv, dict) and roots.get(mv.get("from")) in dead)


def fixed_dates(state=None):
    """{library path: naive datetime} of the latest fix-dates write per file that no undo took back: the wall
    time the sidecar now holds (its offset, if any, is not a folder's business)."""
    out = {}
    for fx in datecheck.load_fixes(state)["fixes"]:
        if fx.get("undone"):
            continue
        for f in fx.get("files", []):
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                d = datecheck.from_iso(f.get("new"))
                if d:
                    out[f["path"]] = d.replace(tzinfo=None)
    return out


def target(path, when):
    """Where path belongs for date when: its first dated folder (YYYY, and a YYYY-MM right under it) under the
    library or videos folder set to when's year and month, everything else kept. None when it has no dated
    folder there (a project folder, a folder of your own)."""
    for root in (intake.LIBRARY, intake.VIDEOS):
        if not datecheck.under(path, root):
            continue
        parts = os.path.relpath(os.path.abspath(path), os.path.abspath(root)).split(os.sep)
        for i, part in enumerate(parts[:-1]):
            if YEAR_RX.match(part):
                parts[i] = f"{when:%Y}"
                m = MONTH_RX.match(parts[i + 1]) if i + 1 < len(parts) - 1 else None
                if m and m[1] == part:
                    parts[i + 1] = f"{when:%Y-%m}"
                return os.path.join(os.path.abspath(root), *parts)
        return None
    return None


def _dev_of(path):
    """st_dev of path's nearest existing folder."""
    d = os.path.dirname(path)
    while not os.path.isdir(d):
        d = os.path.dirname(d)
    return os.stat(d).st_dev


def plan(state=None):
    """(moves [dict(src, dst)], refused {path: why}, in place count). Writes nothing."""
    state = state or intake.STATE
    fixed = fixed_dates(state)
    paths = sorted(p for p in fixed if os.path.isfile(p) and not os.path.islink(p) and datecheck.in_library(p))
    refused = {p: "no longer in the library" for p in fixed if p not in paths}
    for p in label.refused(paths, state):
        refused[p] = "quarantined, staged or flagged by scan (never touched)"
    moves, here, taken = [], 0, set()
    for p in paths:
        if p in refused:
            continue
        dst = target(p, fixed[p])
        if dst is None:
            refused[p] = "not in a dated folder of the library or videos folder"
            continue
        if dst == os.path.abspath(p):
            here += 1
            continue
        if _dev_of(dst) != os.stat(p).st_dev:
            refused[p] = "its folder is on another drive (a move would be a copy): refused"
            continue
        final = free(dst, taken)
        taken.add(final)
        moves.append(dict(src=os.path.abspath(p), dst=final, wanted=dst))
    return moves, refused, here


def free(dst, taken=()):
    """intake.free_path, also skipping names this run already gave out."""
    stem, ext = os.path.splitext(dst)
    p, i = dst, 1
    while os.path.lexists(p) or os.path.lexists(intake.sidecar(p)) or p in taken:
        p, i = f"{stem}_{i}{ext}", i + 1
    return p


def _move(src, dst):
    """Rename src to dst on one filesystem, never replacing anything at dst."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.stat(os.path.dirname(dst)).st_dev != os.stat(src).st_dev:
        raise CrossDevice(errno.EXDEV, "on another filesystem", dst)
    try:
        os.link(src, dst, follow_symlinks=False)  # fails with EEXIST rather than replace a file
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossDevice(errno.EXDEV, "on another filesystem", dst) from None
        if e.errno not in NO_LINK:
            raise
        if os.path.lexists(dst):  # no hard links here: check, then rename (the library lock is held)
            raise FileExistsError(errno.EEXIST, "already there", dst) from None
        os.rename(src, dst)
        return
    os.unlink(src)


def _prune_empty(folder):
    """Remove the folders a move emptied, up to (not including) the library or videos folder."""
    stop = {os.path.abspath(r) for r in intake.LIB_ROOTS}
    d = os.path.abspath(folder)
    while d not in stop and os.path.dirname(d) != d and any(datecheck.under(d, r) for r in stop):
        try:
            os.rmdir(d)  # only an empty folder: never anything in it
        except OSError:
            return
        d = os.path.dirname(d)


def move_pair(src, dst):
    """Move a file and its sidecar together; on a failure half way, the file goes back."""
    _move(src, dst)
    if os.path.lexists(intake.sidecar(src)):
        try:
            _move(intake.sidecar(src), intake.sidecar(dst))
        except OSError:
            _move(dst, src)
            raise
    _prune_empty(os.path.dirname(src))


# ---------------------------------------------------------------- the records that name a library path
def _rekey_index(m):
    p = os.path.join(intake.STATE, "index.json")
    ix = intake.load(p, None)
    if not isinstance(ix, dict):
        return 0, ""
    n = sum(k in m for k in ix)
    if n:
        intake.save(p, {m.get(k, k): v for k, v in ix.items()})
    return n, ""


def _rekey_plans(m):
    n = 0
    for p in sorted(glob.glob(os.path.join(intake.STATE, "batches", "*", "plan.json"))):
        rows = intake.load(p, None)
        if not isinstance(rows, list):
            continue
        k = 0
        for r in rows:
            for key in ("dst", "lib_dst"):
                if isinstance(r, dict) and isinstance(r.get(key), str) and r[key] in m:
                    r[key], k = m[r[key]], k + 1
        if k:
            intake.save(p, rows)
            n += k
    return n, ""


def _rekey_backup(m):
    """The backup record keys files by their path on the drive, and the drive has no copy under the new path
    yet: the old entries (file and .xmp) go, and the next backup copies the file to its new path. The copy
    under the old path stays on the drive (backup never deletes)."""
    n, failed, unread = intake.prune_backup_records(set(m), set())
    why = []
    if failed:
        why.append(f"{intake.plural(failed, 'backup record')} could not be rewritten")
    if unread:
        why.append(f"{intake.plural(unread, 'backup record')} unreadable, left as it is")
    return n, "; ".join(why)


def _rekey_recognition(m):
    """recognition.db's files (path -> sha, size, mtime: a move keeps both) and written (tags it wrote)."""
    import recog
    p = recog.db_path(recog.store_dir(intake.CFG))
    if not os.path.isfile(p):
        return 0, ""
    n = 0
    try:
        c = sqlite3.connect(p, timeout=30)
        try:
            with c:
                for old, new in m.items():
                    for t in ("files", "written"):
                        c.execute(f"DELETE FROM {t} WHERE path=?", (new,))  # noqa: S608 (a fixed table name)
                        n += c.execute(f"UPDATE {t} SET path=? WHERE path=?", (new, old)).rowcount  # noqa: S608
        finally:
            c.close()
    except sqlite3.Error as e:
        return 0, f"recognition store not rekeyed ({type(e).__name__})"
    return n, ""


def _rekey_search(m):
    """embeddings.npz's {path: [size, mtime, sha]} (numpy: the system's, as setup.sh's venv uses)."""
    if not os.path.isfile(os.path.join(intake.STATE, "embeddings.npz")):
        return 0, ""
    try:
        import numpy as np
    except ImportError:
        return 0, "not rekeyed (no numpy here): the next `media-intake index` finds the files again"
    import semantic
    shas, emb, files = semantic.load_store(np)
    n = sum(k in m for k in files)
    if n:
        semantic.save_store(np, shas, emb, {m.get(k, k): v for k, v in files.items()})
    return n, ""


def _rekey_maybe_trash(m):
    p = os.path.join(intake.STATE, "maybe-trash.json")
    d = intake.load(p, None)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return 0, ""
    n = 0
    for it in d["items"]:
        if isinstance(it, dict) and it.get("path") in m:
            it["path"], n = m[it["path"]], n + 1
    if n:
        datecheck.save_private(p, d)
    return n, ""


def _rekey_progress(name):
    """vlm-progress.json / captions-progress.json: {path: digest, size+mtime, verdict}. A rename keeps the size
    and mtime, so the moved file is not judged or captioned again."""
    def rekey(m):
        p = os.path.join(intake.STATE, name)
        d = intake.load(p, None)
        if not isinstance(d, dict):
            return 0, ""
        n = sum(k in m for k in d)
        if n:
            datecheck.save_private(p, {m.get(k, k): v for k, v in d.items()})
        return n, ""
    return rekey


def records(m):
    """[(what, rekeyed, problem)] for every record that names a library path. Not path-keyed, so nothing to do:
    event-names.json (by event), the gallery's thumbnails and proxies, trashed.json, ocr-text.db and the
    recognition store's detections (by sha256). Never raises: a record it can't rewrite is said, not fatal."""
    import places as myplaces
    steps = [("index.json", _rekey_index), ("batch plans", _rekey_plans),
             ("check-dates report and date fixes", lambda m: datecheck.rekey_paths(m, intake.STATE)),
             ("places.json", lambda m: myplaces.rekey_paths(intake.STATE, m)),
             ("backup records (entries dropped)", _rekey_backup), ("recognition store", _rekey_recognition),
             ("search index", _rekey_search), ("maybe-trash.json", _rekey_maybe_trash)]
    steps += [(name, _rekey_progress(name)) for name in PROGRESS]
    out = []
    for what, f in steps:
        try:
            n, why = f(m)
        except Exception as e:  # noqa: BLE001  the files are moved: say what is left, never stop half way
            n, why = 0, f"not rekeyed ({type(e).__name__})"
        out.append((what, n, why))
    return out


def run_moves(pairs):
    """Move each (src, dst) pair, then rekey every record. Returns (done [(src, dst)], failed {src: why},
    records())."""
    done, failed = [], {}
    for src, dst in pairs:
        try:
            move_pair(src, dst)
            done.append((src, dst))
        except CrossDevice:
            failed[src] = "on another filesystem: refused"
        except FileExistsError:
            failed[src] = "something is already at the destination: skipped"
        except OSError as e:
            failed[src] = f"could not be moved ({e.strerror or type(e).__name__})"
    m = {}
    for src, dst in done:
        m[src], m[intake.sidecar(src)] = dst, intake.sidecar(dst)
    return done, failed, records(m) if done else []


def _print_records(recs):
    for what, n, why in recs:
        if n or why:
            print(f"  {what}: {n}" + (f" ({why})" if why else ""))


def cmd_refile(a):
    state = intake.STATE
    if a.undo is not None:
        return cmd_undo(a, state)
    moves, refused, here = plan(state)
    print(f"refile: {intake.plural(len(moves), 'file')} to move" + ("" if a.yes else " (dry run: nothing moved)")
          + (f", {here} already in place" if here else "") + ":")
    for mv in moves:
        note = "  (the name was taken: a free one)" if mv["dst"] != mv["wanted"] else ""
        print(f"  {intake.shown_path(mv['src'])} -> {intake.shown_path(mv['dst'])}{note}")
    for p, why in sorted(refused.items()):
        print(f"  refused {intake.shown_path(p)}: {why}")
    if not moves:
        return
    if not a.yes:
        print("Run again with --yes to move them, each with its .xmp sidecar (refile --undo puts them back).")
        return
    done, failed, recs = run_moves([(mv["src"], mv["dst"]) for mv in moves])
    for src, why in failed.items():
        print(f"  not moved {intake.shown_path(src)}: {why}")
    if not done:
        sys.exit("refile: nothing was moved")
    rid = secrets.token_hex(4)
    log = load_log(state)
    log["refiles"].append(dict(id=rid, at=datecheck.iso(datetime.datetime.now()), undone=None,
                               moves=[{"from": s, "to": d} for s, d in done]))
    intake.private_dir(state)
    datecheck.save_private(log_path(state), log)
    print(f"moved {intake.plural(len(done), 'file')} (refile {rid}); records rekeyed:")
    _print_records(recs)
    print(f"Undo: media-intake refile --undo {rid} --yes. The backup drive keeps its copy under the old path; "
          "the next backup copies each file to its new one.")
    if failed:
        sys.exit(1)


def cmd_undo(a, state):
    log = load_log(state)
    live = [r for r in log["refiles"] if not r.get("undone")]
    if a.undo in ("", "last"):
        if not live:
            sys.exit("refile: nothing to undo")
        rec = live[-1]
    else:
        rec = next((r for r in log["refiles"] if r.get("id") == a.undo), None)
        if rec is None:
            sys.exit(f"refile: no refile {a.undo!r} (refile --undo with no id undoes the last one)")
        if rec.get("undone"):
            sys.exit(f"refile: refile {a.undo} was undone already")
    bad = []
    for mv in rec["moves"]:
        if not os.path.isfile(mv["to"]):
            bad.append(f"{intake.shown_path(mv['to'])}: no longer there")
        elif os.path.lexists(mv["from"]) or os.path.lexists(intake.sidecar(mv["from"])):
            bad.append(f"{intake.shown_path(mv['from'])}: something is there now")
    print(f"refile --undo {rec['id']} ({rec['at']}, {intake.plural(len(rec['moves']), 'file')}):")
    for mv in rec["moves"]:
        print(f"  {intake.shown_path(mv['to'])} -> {intake.shown_path(mv['from'])}")
    if bad:
        sys.exit("can't undo: " + "; ".join(bad) + ". Nothing was moved")
    if not a.yes:
        print("dry run: nothing moved. Run again with --yes to put these back.")
        return
    done, failed, recs = run_moves([(mv["to"], mv["from"]) for mv in rec["moves"]])
    rec["undone"] = datecheck.iso(datetime.datetime.now()) if not failed else None
    if failed:  # the log follows the files: what went back is no longer this refile's
        back = {s for s, _ in done}
        rec["moves"] = [mv for mv in rec["moves"] if mv["to"] not in back]
    datecheck.save_private(log_path(state), log)
    print(f"put back {intake.plural(len(done), 'file')}; records rekeyed:")
    _print_records(recs)
    for src, why in failed.items():
        print(f"  not moved {intake.shown_path(src)}: {why}")
    if failed:
        sys.exit(1)
