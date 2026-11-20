#!/usr/bin/env python3
"""`media-intake space [--top N] [--json]`: what takes room, and how much each kind of clean-up would free.

  media-intake space              a plain-words report
  media-intake space --top 25     list 25 files per section (default 10; 0 lists every one)
  media-intake space --json       the same, as one JSON object for scripts
  python3 -I media_intake/space.py [--top N] [--json]   (same)

Read-only: it deletes, moves and writes nothing, and takes no lock. Its sections:

  - Largest videos: the library's videos by size.
  - Exact copies: library files with the same content (sha256 from index.json when the size matches, else
    hashed here, and only for sizes two files share). Keeping one of each frees the rest.
  - Near-copies: library files intake planned as a near-duplicate (a resize or re-save, set aside by apply)
    that you kept into the library since (gallery.plan_entries). Nothing else is compared here.
  - Review folders: what apply set aside per batch (near-duplicates and junk), counts and sizes only;
    `purge-review BATCH --yes` frees it.
  - Suggested junk: the gallery's Suggested junk view at its default Junk score (gallery.App.suggested,
    gallery.SUGGEST_MIN), the same files `suggest-trash` finds.
  - Gallery cache: thumbnails and video proxies, and how much `prune-cache` would free (cache_prune's
    dry run).

The library is what the gallery lists (gallery.scan_library): never the state folder, a file scan flagged,
quarantine, an erased file's content, nor a photo held for the records inbox. A trashed file is gone, so it
is not listed either. Sections can overlap (a Suggested junk file may also be a copy), so their totals are
never added up. docs/gallery.md, "Free up space".
"""
import argparse, collections, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cache_prune  # noqa: E402  (in_use, sweep, open_cache_dir: what prune-cache would free)
import gallery  # noqa: E402  (scan_library, App.suggested, plan_entries, WORDS)
import intake  # noqa: E402  (STATE, THUMBS, PROXIES, sha256)

TOP = 10
HOME = os.path.expanduser("~")


def human(n):
    return gallery.R.human(n)


def shown(path):
    """A path as the report prints it: ~/... under the home folder."""
    return "~/" + os.path.relpath(path, HOME) if path.startswith(HOME + os.sep) else path


def exact_copies(items):
    """[[item, ...]] of library files with the same content, two or more each, biggest saving first. A file
    is hashed only when its size matches another's and index.json has no sha256 for it; one that can't be
    read is left out."""
    by_size = collections.defaultdict(list)
    for it in items:
        by_size[it["size"]].append(it)
    sets = collections.defaultdict(list)
    for same in by_size.values():
        if len(same) < 2:
            continue
        for it in same:
            try:
                sha = it.get("sha") or intake.sha256(it["path"])
            except OSError:
                continue
            sets[sha].append(it)
    out = [sorted(v, key=lambda it: it["path"]) for v in sets.values() if len(v) > 1]
    out.sort(key=lambda s: (-s[0]["size"] * (len(s) - 1), s[0]["path"]))
    return out


def near_copies(items):
    """[item] of library files intake planned as a near-duplicate (and so set aside for review) that were kept
    into the library afterwards, largest first."""
    plans = gallery.plan_entries()
    out = [it for it in items
           if str((plans.get(it["path"]) or {}).get("why") or "").startswith("near-duplicate")]
    return sorted(out, key=lambda it: (-it["size"], it["path"]))


def folder_size(path):
    """(files, bytes) of the regular files under path; symlinks are never followed or counted."""
    n = size = 0
    for d, dirs, names in os.walk(path, followlinks=False):
        for x in names:
            try:
                st = os.lstat(os.path.join(d, x))
            except OSError:
                continue
            if os.path.isfile(os.path.join(d, x)) and not os.path.islink(os.path.join(d, x)):
                n, size = n + 1, size + st.st_size
    return n, size


def review_folders(state=None):
    """[{batch, files, bytes}] for each batch whose review folder holds something, largest first."""
    bdir = os.path.join(state or intake.STATE, "batches")
    try:
        names = sorted(os.listdir(bdir))
    except OSError:
        return []
    out = []
    for b in names:
        rd = os.path.join(bdir, b, "review")
        if os.path.islink(rd) or not os.path.isdir(rd):
            continue
        n, size = folder_size(rd)
        if n:
            out.append(dict(batch=b, files=n, bytes=size))
    return sorted(out, key=lambda r: (-r["bytes"], r["batch"]))


def junk(items):
    """[(item, [reason words])] of Suggested junk at the gallery's default Junk score, largest first."""
    app = gallery.App(items)
    words = gallery.WORDS["why"]
    out = [(it, sorted(words.get(w, w) for w in app.junk_why(it, gallery.SUGGEST_MIN)))
           for it in items if app.suggested(it)]
    return sorted(out, key=lambda x: (-x[0]["size"], x[0]["path"]))


def cache_files(folder):
    """(files, bytes) of the regular files directly in a cache folder (cache_prune.open_cache_dir: never
    through a symlink)."""
    fd = cache_prune.open_cache_dir(folder)
    if fd is None:
        return 0, 0
    n = size = 0
    try:
        with os.scandir(fd) as entries:
            for e in entries:
                try:
                    if e.is_file(follow_symlinks=False):
                        n, size = n + 1, size + e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    finally:
        os.close(fd)
    return n, size


def cache(items):
    """The gallery cache's sizes, and what prune-cache would free now (its dry run: nothing is deleted)."""
    used, unread = cache_prune.in_use(items)
    missing = [r for r in gallery.ROOTS if not os.path.isdir(r)]
    certain = not unread and not missing
    counts = collections.Counter()
    freed = 0
    for folder, reason in ((intake.THUMBS, cache_prune.thumb_reason), (intake.PROXIES, cache_prune.proxy_reason)):
        freed += cache_prune.sweep(folder, reason, used, certain, True, counts)[0]
    tn, tb = cache_files(intake.THUMBS)
    pn, pb = cache_files(intake.PROXIES)
    return dict(thumbnails=dict(files=tn, bytes=tb), proxies=dict(files=pn, bytes=pb),
                prune=dict(files=sum(counts.values()), bytes=freed, certain=certain))


def entry(it, **extra):
    return dict(path=it["path"], bytes=it["size"], **extra)


def report(top=TOP):
    """The whole report as a dict (what --json prints). top limits each list (0: no limit); totals are always
    over everything."""
    items, _ = gallery.scan_library()
    cut = (lambda xs: xs) if not top else (lambda xs: xs[:top])  # noqa: E731
    videos = sorted((it for it in items if it["media"] == "video"), key=lambda it: (-it["size"], it["path"]))
    copies = exact_copies(items)
    near = near_copies(items)
    review = review_folders()
    sug = junk(items)
    return dict(
        library=dict(files=len(items), bytes=sum(it["size"] for it in items)),
        videos=dict(files=len(videos), bytes=sum(it["size"] for it in videos), list=[entry(it) for it in cut(videos)]),
        copies=dict(sets=len(copies), extra=sum(len(s) - 1 for s in copies),
                    frees=sum(s[0]["size"] * (len(s) - 1) for s in copies),
                    list=[dict(bytes_each=s[0]["size"], frees=s[0]["size"] * (len(s) - 1),
                               paths=[it["path"] for it in s]) for s in cut(copies)]),
        near=dict(files=len(near), frees=sum(it["size"] for it in near), list=[entry(it) for it in cut(near)]),
        review=dict(batches=len(review), files=sum(r["files"] for r in review), frees=sum(r["bytes"] for r in review), list=cut(review)),
        junk=dict(files=len(sug), frees=sum(it["size"] for it, _ in sug),
                  list=[entry(it, why=why) for it, why in cut(sug)]),
        cache=cache(items),
    )


def plural(n, word):
    return f"{n:,} {word}" + ("" if n == 1 else ("es" if word.endswith("sh") else "s"))


def more(shown_n, total, noun):
    return [f"  and {total - shown_n:,} more (--top 0 lists every {noun})"] if total > shown_n else []


def text(r):
    """The report in plain words, as lines."""
    lib, v, c, nr, rv, j, ca = (r[k] for k in ("library", "videos", "copies", "near", "review", "junk", "cache"))
    out = ["What takes room (a report only: nothing was changed)",
           f"Library: {plural(lib['files'], 'file')}, {human(lib['bytes'])}", ""]
    out.append(f"Largest videos: {plural(v['files'], 'video')}, {human(v['bytes'])} together")
    out += [f"  {human(x['bytes']):>9}  {shown(x['path'])}" for x in v["list"]]
    out += more(len(v["list"]), v["files"], "video")
    out += ["", f"Exact copies: {plural(c['sets'], 'set')}, {c['extra']:,} extra; keeping one of each frees "
                f"{human(c['frees'])}"]
    for s in c["list"]:
        out.append(f"  {len(s['paths'])} copies of {human(s['bytes_each'])} each, frees {human(s['frees'])}:")
        out += [f"    {shown(p)}" for p in s["paths"]]
    out += more(len(c["list"]), c["sets"], "set")
    out += ["", f"Near-copies kept in the library: {plural(nr['files'], 'file')}, {human(nr['frees'])} "
                "(apply set them aside as near-duplicates; they were kept since)"]
    out += [f"  {human(x['bytes']):>9}  {shown(x['path'])}" for x in nr["list"]]
    out += more(len(nr["list"]), nr["files"], "file")
    out += ["", f"Review folders: {plural(rv['files'], 'file')} (near-duplicates and junk) set aside, {human(rv['frees'])}"]
    out += [f"  {human(x['bytes']):>9}  batch {x['batch']}: {plural(x['files'], 'file')} "
            f"(media-intake purge-review {x['batch']} --yes)" for x in rv["list"]]
    out += more(len(rv["list"]), rv["batches"], "batch")
    out += ["", f"Suggested junk: {plural(j['files'], 'file')}, {human(j['frees'])}"]
    out += [f"  {human(x['bytes']):>9}  {shown(x['path'])}  ({', '.join(x['why'])})" for x in j["list"]]
    out += more(len(j["list"]), j["files"], "file")
    t, p, pr = ca["thumbnails"], ca["proxies"], ca["prune"]
    out += ["", f"Gallery cache: {plural(t['files'], 'thumbnail')}, {human(t['bytes'])}; {p['files']:,} video "
                f"{'proxy' if p['files'] == 1 else 'proxies'}, "
                f"{human(p['bytes'])}",
            f"  media-intake prune-cache would free {human(pr['bytes'])} ({plural(pr['files'], 'file')} nothing shows any more)"
            + ("" if pr["certain"] else "; only legacy thumbnails, since a library folder is missing or a file "
                                        "could not be read")]
    out += ["", "Sections can overlap, so their sizes are not added up. To act: Suggested junk and copies in "
                "media-intake gallery (or trash FILE... --yes), purge-review BATCH --yes, prune-cache."]
    return out


def main(argv=None, prog="media-intake space"):
    ap = argparse.ArgumentParser(prog=prog, description="Show what takes room in the library and how much "
                                 "each clean-up would free: the largest videos, exact copies, near-copies, review "
                                 "folders, Suggested junk and the gallery cache. Changes nothing "
                                 "(docs/gallery.md, \"Free up space\").")
    ap.add_argument("--top", type=int, default=TOP, metavar="N",
                    help=f"list at most N files per section (default {TOP}; 0 lists every one)")
    ap.add_argument("--json", action="store_true", help="one JSON object on stdout, for scripts")
    a = ap.parse_args(argv)
    if a.top < 0:
        ap.error("--top takes 0 or more")
    r = report(a.top)
    if a.json:
        json.dump(r, sys.stdout, indent=1)
        print()
    else:
        print("\n".join(text(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
