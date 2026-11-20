#!/usr/bin/env python3
"""Best shot per burst and near-duplicate set: the sharpest, best-exposed frame is the cover; the rest are
offered to Suggested junk as "Not the best shot". Never trashes, moves or deletes anything.

  media-intake best-shot [--dry-run]

Sets: the gallery's burst stacks (bursts.py over the plan entries, as gallery.attach_bursts makes them) joined
with near-duplicate photos in the library (dHashes from index.json at most NEAR_MAX bits apart, intake's
"recompressed or resized copy" threshold). A shot in a burst and in a near-duplicate set makes them one set.
Photos only; videos never are in a set.

Score (Pillow only, in memory, downscaled to at most MAX_PX; the original is opened read-only and never
written): sharpness is the variance of the Laplacian of the grey image (a blurred frame has few edges);
exposure is the share of pixels clipped to near black or near white. A frame with more than CLIP_MAX clipped
loses to any frame under it; then the higher sharpness * (1 - clipped) wins; a tie goes to the earlier
frame in the set. A file Pillow can't decode (RAW, HEIC without a plugin) is not scored and never tagged.

Tags, in this tool's own namespace in the XMP sidecar only (docs/intake.md, "Tags: who writes what"):
  AI|bestshot/cover      the best shot of its set
  AI|bestshot/not-best   another shot of a set: the gallery's Suggested junk, reason "Not the best shot"
A run replaces only AI|bestshot/ tags (a file no longer in any set loses its old one); no other keyword is
added or removed. Owner tags win: a file you decided on (owner.decided: Keep, Trash, a pick label, a
favorite), one with a person or pet you named, one used in a post (Used|) or in a protected project gets no
AI|bestshot/ tag at all, though it still counts when the cover is chosen. Files scan flagged, quarantined,
erased or held for the records inbox are never read (gallery.scan_library leaves them out).

Sidecars are read and written under one hold of the library lock (--dry-run takes none; a busy lock is waited
for as other CLI writers do, then refused), writes through exiftool. Output: counts only, never a file name.
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config  # noqa: E402
import intake  # noqa: E402  (NEAR_MAX, sidecar, EMPTY_XMP, exiftool_batch, load)
import library_lock  # noqa: E402
import owner  # noqa: E402

NS = "AI|bestshot/"
COVER = NS + "cover"
NOT_BEST = NS + "not-best"
MAX_PX = 512        # enough detail for a blur measure; keeps a 30-frame burst fast
CLIP_LOW, CLIP_HIGH = 4, 251  # grey levels counted as crushed black / blown white
CLIP_MAX = 0.25     # more than a quarter of the frame clipped: badly exposed
NEAR_MAX = intake.NEAR_MAX
LAPLACIAN = (0, 1, 0, 1, -4, 1, 0, 1, 0)


# ---------------------------------------------------------------- measures (Pillow, in memory)
def measure(img):
    """(sharpness, clipped) of a PIL image: the Laplacian's variance on the grey image (at most MAX_PX on its
    long side) and the share of pixels at or below CLIP_LOW or at or above CLIP_HIGH."""
    from PIL import ImageFilter, ImageStat
    g = img.convert("L")
    if max(g.size) > MAX_PX:
        g.thumbnail((MAX_PX, MAX_PX))
    # offset 128 keeps negative responses in 0..255; the variance is what matters, not the mean
    lap = g.filter(ImageFilter.Kernel((3, 3), LAPLACIAN, scale=1, offset=128))
    sharp = ImageStat.Stat(lap).var[0]
    h = g.histogram()
    total = sum(h) or 1
    clipped = (sum(h[:CLIP_LOW + 1]) + sum(h[CLIP_HIGH:])) / total
    return float(sharp), clipped


def score_file(path):
    """measure() of a file, or None when Pillow can't read it. Read-only; nothing is written or kept."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.draft("RGB", (MAX_PX, MAX_PX))  # JPEG: decode at a reduced size
            return measure(im)
    except Exception:  # noqa: BLE001  any decode failure: not scored, never tagged
        return None


def rank_key(s):
    """Higher is better: under CLIP_MAX first, then sharpness weighted by the unclipped share."""
    sharp, clipped = s
    return (clipped <= CLIP_MAX, sharp * (1.0 - clipped))


def best(scores):
    """Index of the best of a list of measure() results (None entries never win); None when none is scored.
    A tie goes to the earlier one."""
    out = None
    for i, s in enumerate(scores):
        if s is not None and (out is None or rank_key(s) > rank_key(scores[out])):
            out = i
    return out


# ---------------------------------------------------------------- sets
def near_pairs(hashes, near=NEAR_MAX):
    """Pairs (i, j) of positions whose 64-bit dHashes are at most `near` bits apart. Pigeonhole banding: split
    the hash into near+1 bands; two hashes that close agree on at least one band, so only those are compared."""
    bands = near + 1
    width = -(-64 // bands)
    buckets = {}
    for i, h in enumerate(hashes):
        if not isinstance(h, int):
            continue
        for b in range(bands):
            buckets.setdefault((b, (h >> (b * width)) & ((1 << width) - 1)), []).append(i)
    out = set()
    for members in buckets.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                i, j = members[x], members[y]
                if bin(hashes[i] ^ hashes[j]).count("1") <= near:
                    out.add((i, j))
    return out


def sets(items, index=None, near=NEAR_MAX):
    """Sets of item positions (lists of two or more, in the items' order): burst stacks (it["burst"]) joined with
    near-duplicate photos (index.json dHashes). Photos only."""
    index = index if isinstance(index, dict) else {}
    photos = [n for n, it in enumerate(items) if it.get("media") == "photo" and not it.get("gone")]
    parent = {n: n for n in photos}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first = {}
    for n in photos:
        g = items[n].get("burst")
        if g is not None:
            if g in first:
                join(first[g], n)
            else:
                first[g] = n
    hashes = [(index.get(items[n]["path"]) or {}).get("dhash") for n in photos]
    for i, j in near_pairs(hashes, near):
        join(photos[i], photos[j])
    groups = {}
    for n in photos:
        groups.setdefault(find(n), []).append(n)
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: g[0])


# ---------------------------------------------------------------- owner rules
def hands_off(it, protected=None):
    """Owner tags win: a file you decided on, with a name you gave, used in a post or in a protected project gets
    no AI|bestshot/ tag."""
    protected = config.protected_tags() if protected is None else protected
    tags = it.get("tags") or []
    return (owner.decided(tags, it.get("pick", ""), it.get("rating")) or owner.named(tags)
            or any(t.startswith("Used|") for t in tags) or bool(it.get("used"))
            or f"Project|{it.get('project')}" in protected or bool(set(protected) & set(tags)))


# ---------------------------------------------------------------- plan and write
def plan(items, index=None, score=None, protected=None):
    """({path: wanted AI|bestshot/ tag or None}, counts) for every item whose AI|bestshot/ tags must change.
    Only items in a set are scored (opened)."""
    score = score or score_file
    want = {}
    c = dict(sets=0, covers=0, not_best=0, owner=0, unscored=0, unchanged=0)
    for g in sets(items, index):
        scores = [score(items[n]["path"]) for n in g]
        b = best(scores)
        if b is None:
            c["unscored"] += len(g)
            continue
        c["sets"] += 1
        for k, n in enumerate(g):
            it = items[n]
            if scores[k] is None:
                c["unscored"] += 1
            elif hands_off(it, protected):
                c["owner"] += 1
            else:
                want[it["path"]] = COVER if k == b else NOT_BEST
                c["covers" if k == b else "not_best"] += 1
    changes = {}
    for it in items:
        have = sorted(t for t in it.get("tags") or [] if t.startswith(NS))
        new = want.get(it["path"])
        if have == ([new] if new else []):
            if new:
                c["unchanged"] += 1
            continue
        if new or have:
            changes[it["path"]] = (have, new)
    return changes, c


def blocks(changes):
    """exiftool argument blocks (one per sidecar): remove the old AI|bestshot/ tags, add the new one. Only the
    sidecar is ever the target, and only tags under NS are named."""
    out = []
    for path, (have, new) in sorted(changes.items()):
        x = intake.sidecar(path)
        if not x.endswith(".xmp") or os.path.splitext(path)[1].lower() == ".xmp" or "\n" in x:
            raise ValueError(f"refusing to write a non-sidecar: {x}")
        b = ["-q", "-m", "-overwrite_original"]
        for t in sorted(set(have) | ({new} if new else set())):
            if not t.startswith(NS):
                raise ValueError(f"refusing to touch a tag outside {NS}")
            b += [f"-XMP-lr:HierarchicalSubject-={t}", f"-XMP-dc:Subject-={t.split('|')[-1]}"]
        if new:
            b += [f"-XMP-lr:HierarchicalSubject+={new}", f"-XMP-dc:Subject+={new.split('|')[-1]}"]
        out.append(b + [x])
    return out


def write(changes):
    """Create a missing sidecar for a file getting a tag, then run every block in one exiftool call. A file that
    went away since it was read is left alone."""
    live = {p: v for p, v in changes.items() if os.path.lexists(p)}
    for p, (_, new) in live.items():
        x = intake.sidecar(p)
        if new and not os.path.exists(x):
            with open(x, "w", encoding="utf-8") as f:
                f.write(intake.EMPTY_XMP)
    intake.exiftool_batch(blocks(live))
    return len(live)


def summary(c, written=None, dry_run=False):
    s = (f"best-shot: {c['sets']:,} sets, {c['covers']:,} covers, {c['not_best']:,} not the best shot; "
         f"{c['owner']:,} left alone (your decision), {c['unscored']:,} not readable")
    if dry_run:
        return s + f"; would change {written:,} sidecars (--dry-run: nothing written)"
    return s + f"; {written:,} sidecars changed"


def build_parser(prog="media-intake best-shot"):
    p = argparse.ArgumentParser(
        prog=prog,
        description="Pick the best shot of each burst and near-duplicate set (the\n"
                    "sharpest, best exposed) and offer the rest to Suggested junk as\n"
                    "\"Not the best shot\". Never trashes anything itself.\n\n"
                    "Touches: sidecars of photos in a burst or near-duplicate set.\n"
                    "Deletes: nothing. Your Keep, Trash, favorites and names win.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="count what would change; write nothing")
    return p


def survey():
    """plan() over the library as the gallery reads it: never the state folder or a flagged file."""
    import gallery
    items, _ = gallery.scan_library()
    index = intake.load(os.path.join(gallery.STATE, "index.json"), {}) or {}
    return plan(items, index)


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.dry_run:  # reads only: no lock
        changes, c = survey()
        print(summary(c, len(changes), dry_run=True))
        return 0
    try:
        # The sidecars are read and written under one hold, so a Keep or a name given meanwhile is never
        # overwritten by a plan made before it.
        with library_lock.hold("best_shot.py", wait=library_lock.cli_wait(), out=sys.stderr):
            changes, c = survey()
            n = write(changes)
    except library_lock.Busy as e:
        print(f"best-shot: refused: {e}", file=sys.stderr)
        return 1
    print(summary(c, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
