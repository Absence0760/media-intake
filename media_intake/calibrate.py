#!/usr/bin/env python3
"""`media-intake calibrate`: how well each SigLIP label's threshold matches your own marks. Reports only.

  media-intake calibrate [--precision P] [--min N] [--with-videos] [--json]
  python3 -I media_intake/calibrate.py ...   (same)

For every label in labels.yaml: how many items you marked as showing it (positive) or not (negative), then
precision, recall and F1 at its current threshold, and a suggested threshold: the best-F1 one, or with
--precision P the one with the most recall whose precision is at least P. Under --min (default 20) positives
or negatives it says so instead of suggesting a threshold. Never writes labels.yaml, the config, a sidecar
or anything else; output is counts and numbers, never a file name. docs/configuration.md, "Calibrating
label thresholds".

Scores: tag stores only the labels over each threshold, so the scores are worked out again here from the
search index (<state>/embeddings.npz, `media-intake index`) and labels.npz, with label.py's formula
(sigmoid(cos * exp(logit_scale) + logit_bias), max over the label's prompts). No photo or video is opened.
A photo's index vector is the one label.py scores; a video's is the mean of 8 frames where label.py takes
the max over up to 30, so videos are left out unless --with-videos. Flagged, quarantined or erased content
(label.flagged) is never counted. Only the SigLIP score is calibrated: label.py's pixel check (very blurry
or near-black photos) adds Review|maybe-trash on its own and is not covered.

Your marks (per content sha256; copies of one file pool their tags):
  Calibrate|<label>|yes / Calibrate|<label>|no   any label, a tag you add in the gallery ("+ tag", which
                                                 works on a selection); this wins over everything below
  maybe-trash, from your trash decisions:        negative: kept (owner.kept: Owner|keep, Owner|not-trash,
                                                 digiKam Accepted); positive: Owner|trash on a copy, or the
                                                 sha256 in <state>/trashed.json (while its index vector is
                                                 still there: the next `media-intake index` drops it)
An item marked both ways for a label is a conflict and is not counted for it.
"""
import argparse, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config  # noqa: E402
import label  # noqa: E402  (labels.yaml, labels.npz, the score formula, flagged content)
import owner  # noqa: E402  (Owner|keep / not-trash / trash)

TAG = "calibrate|"  # Calibrate|<label>|yes or |no, matched without case
MAYBE_TRASH = "maybe-trash"
MIN_EACH = 20


# ---------------------------------------------------------------- ground truth
def marks(tags, names):
    """({label: {True, False}} from Calibrate| tags, count of Calibrate| tags naming no label or no yes/no)."""
    by_fold = {n.casefold(): n for n in names}
    out, unknown = {}, 0
    for t in tags:
        if not t.casefold().startswith(TAG):
            continue
        name, sep, answer = t[len(TAG):].rpartition("|")
        name, answer = by_fold.get(name.strip().casefold()), answer.strip().casefold()
        if not sep or name is None or answer not in ("yes", "no"):
            unknown += 1
            continue
        out.setdefault(name, set()).add(answer == "yes")
    return out, unknown


def truth(copies, names, trashed=False):
    """({label: True / False / "conflict"}, count of unusable Calibrate| tags) for one content: copies is
    [(tags, pick)] of its library files, trashed whether its sha256 is in trashed.json. Labels without a mark
    are left out."""
    votes, unknown = {}, 0
    for tags, _ in copies:
        m, u = marks(tags, names)
        unknown += u
        for n, v in m.items():
            votes.setdefault(n, set()).update(v)
    out = {n: (v.pop() if len(v) == 1 else "conflict") for n, v in votes.items()}
    if MAYBE_TRASH in names and MAYBE_TRASH not in out:
        if any(owner.kept(tags, pick) for tags, pick in copies):
            out[MAYBE_TRASH] = False  # a Keep wins over a trashed copy (owner.trashed)
        elif trashed or any(owner.TRASH in tags for tags, _ in copies):
            out[MAYBE_TRASH] = True
    return out, unknown


# ---------------------------------------------------------------- scores
def scores(emb, txt, prompt_owner, names, np):
    """{label: score per row of emb}: label.py's sigmoid, max over each label's prompts. emb and txt are
    unit vectors (rows)."""
    logits = emb.astype(np.float64) @ txt.astype(np.float64).T * math.exp(label.LOGIT_SCALE) + label.LOGIT_BIAS
    prob = 1 / (1 + np.exp(-logits))
    return {n: prob[:, [i for i, o in enumerate(prompt_owner) if o == n]].max(axis=1) for n in names}


# ---------------------------------------------------------------- maths
def metrics(pos, neg, thr):
    """Precision, recall and F1 when score >= thr means "has the label" (label.py's rule). Precision is None
    when nothing reaches thr; F1 = 2TP / (2TP + FP + FN), 0 when TP is 0."""
    tp = sum(1 for s in pos if s >= thr)
    fp = sum(1 for s in neg if s >= thr)
    fn = len(pos) - tp
    return {"tp": tp, "fp": fp, "fn": fn, "tn": len(neg) - fp,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / len(pos) if pos else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}


def sweep(pos, neg):
    """Every distinct cut of the marked items, highest first: [(threshold, metrics)]. Each cut flags the items
    scoring at least one of their scores; its threshold sits halfway down to the next lower score (the
    lowest score itself for the last cut), so the same items stay on each side."""
    cuts = sorted(set(pos) | set(neg), reverse=True)
    out = []
    for i, c in enumerate(cuts):
        t = (c + cuts[i + 1]) / 2 if i + 1 < len(cuts) else c
        out.append((t, metrics(pos, neg, c)))
    return out


def suggest(pos, neg, precision=None):
    """(threshold, metrics) of the best-F1 cut, or with precision the cut with the most recall whose precision
    is at least that; ties go to the higher threshold (fewer flags). None when no cut qualifies."""
    best = None
    for t, m in sweep(pos, neg):
        if precision is None:
            key = (m["f1"] or 0.0,)
        elif m["precision"] is not None and m["precision"] >= precision:
            key = (m["recall"] or 0.0,)
        else:
            continue
        if best is None or key > best[0]:  # strictly better: the sweep runs from the higher threshold down
            best = (key, t, m)
    return None if best is None else (best[1], best[2])


def report_label(pos, neg, thr, min_each=MIN_EACH, precision=None):
    """One label's report: counts, metrics at thr, and a suggestion or the reason there is none."""
    r = {"positive": len(pos), "negative": len(neg), "threshold": thr, "current": metrics(pos, neg, thr),
         "suggested": None, "why_not": None}
    if len(pos) < min_each or len(neg) < min_each:
        r["why_not"] = (f"too few marked items: {len(pos)} positive, {len(neg)} negative; "
                        f"need at least {min_each} of each")
        return r
    s = suggest(pos, neg, precision)
    if s is None:
        r["why_not"] = f"no threshold reaches precision {precision:g}"
    else:
        r["suggested"] = {"threshold": s[0], **s[1]}
    return r


# ---------------------------------------------------------------- the library
def load_store(np, path):
    """(shas, emb float32, files {path: [size, mtime_ns, sha]}) of semantic.py's index, or empty."""
    try:
        with np.load(path, allow_pickle=False) as z:
            return list(z["sha"]), z["emb"].astype(np.float32), json.loads(str(z["files"]))
    except (FileNotFoundError, KeyError, ValueError):
        return [], np.zeros((0, 0), np.float32), {}


def read_tags(path):
    """(HierarchicalSubject tags, digiKam PickLabel) of path's sidecar; ([], "") without one."""
    import gallery  # stdlib XMP reader; imported here so `--help` stays light
    x = gallery.read_xmp(path + ".xmp") if os.path.isfile(path + ".xmp") else {}
    return owner.normalise(x.get("tags") or []), owner.pick_label(x.get("pick"))


def collect(np, state, npz=None, with_videos=False):
    """(names, thresholds, {label: (positive scores, negative scores)}, counts) for this library."""
    names, prompts, prompt_owner, thr, _ = label.label_config()
    txt = label.text_embeddings(prompts, np, npz or label.LABELS_NPZ)
    shas, emb, files = load_store(np, os.path.join(state, "embeddings.npz"))
    flagged = label.flagged(state)
    trashed = label._load(os.path.join(state, "trashed.json"), {}) or {}
    paths = {}
    for p, (_, _, sha) in files.items():
        paths.setdefault(sha, []).append(p)
    c = {"scored": 0, "videos_left_out": 0, "conflicts": 0, "unknown_tags": 0, "flagged_left_out": 0}
    rows, truths = [], []
    for i, sha in enumerate(shas):
        if sha in flagged:
            c["flagged_left_out"] += 1
            continue
        mine = paths.get(sha, [])
        if not with_videos and any(os.path.splitext(p)[1].lower() in label.VID for p in mine):
            c["videos_left_out"] += 1
            continue
        t, u = truth([read_tags(p) for p in mine if os.path.isfile(p)], names, sha in trashed)
        c["scored"] += 1
        c["unknown_tags"] += u
        if t:
            rows.append(i)
            truths.append(t)
    indexed = set(shas)
    c["trashed_without_scores"] = sum(1 for s in trashed if s not in indexed)
    c["marked"] = len(rows)
    sc = scores(emb[rows], txt, prompt_owner, names, np) if rows else {n: [] for n in names}
    out = {}
    for n in names:
        pos, neg = [], []
        for j, t in enumerate(truths):
            v = t.get(n)
            if v == "conflict":
                c["conflicts"] += 1
            elif v is not None:
                (pos if v else neg).append(float(sc[n][j]))
        out[n] = (pos, neg)
    return names, thr, out, c


def run(np, state, npz=None, with_videos=False, min_each=MIN_EACH, precision=None):
    names, thr, data, counts = collect(np, state, npz, with_videos)
    labels = {n: report_label(*data[n], thr[n], min_each, precision) for n in names}
    return {"counts": counts, "min_each": min_each, "target_precision": precision, "labels": labels}


# ---------------------------------------------------------------- output
def _f(v, d=3):
    return "-" if v is None else f"{v:.{d}f}"


def text(res, with_videos=False):
    c = res["counts"]
    lines = [f"calibrate: {c['scored']:,} items with stored scores ({'photos and videos' if with_videos else 'photos'}),"
             f" {c['marked']:,} with a mark"]
    notes = []
    if c["videos_left_out"]:
        notes.append(f"videos left out: {c['videos_left_out']:,} (--with-videos counts them)")
    if c["trashed_without_scores"]:
        notes.append(f"trashed items no longer in the index: {c['trashed_without_scores']:,}")
    if c["conflicts"]:
        notes.append(f"marks in conflict (yes and no), not counted: {c['conflicts']:,}")
    if c["unknown_tags"]:
        notes.append(f"Calibrate| tags naming no label or no yes/no, ignored: {c['unknown_tags']:,}")
    if c["flagged_left_out"]:
        notes.append(f"flagged items, never counted: {c['flagged_left_out']:,}")
    lines += [f"  {n}" for n in notes]
    goal = (f"most recall at precision >= {res['target_precision']:g}" if res["target_precision"] is not None
            else "best F1")
    lines.append(f"suggested threshold: {goal}; only with at least {res['min_each']} positive and "
                 f"{res['min_each']} negative marks")
    marked = {n: r for n, r in res["labels"].items() if r["positive"] or r["negative"]}
    if marked:
        w = max(len(n) for n in marked)
        lines.append(f"{'label':<{w}}  {'pos':>5} {'neg':>5}  {'thresh':>7} {'prec':>5} {'recall':>6} {'F1':>5}"
                     f"  | {'suggest':>7} {'prec':>5} {'recall':>6} {'F1':>5}")
        for n, r in marked.items():
            m = r["current"]
            row = (f"{n:<{w}}  {r['positive']:>5} {r['negative']:>5}  {r['threshold']:>7.4f} {_f(m['precision']):>5}"
                   f" {_f(m['recall']):>6} {_f(m['f1']):>5}  | ")
            s = r["suggested"]
            row += (f"{s['threshold']:>7.4f} {_f(s['precision']):>5} {_f(s['recall']):>6} {_f(s['f1']):>5}" if s
                    else r["why_not"])
            lines.append(row)
    rest = len(res["labels"]) - len(marked)
    if rest:
        lines.append(f"labels with no marks yet (Calibrate|<label>|yes or |no): {rest}")
    return "\n".join(lines)


def _round(res):
    """The report with every score-derived number to 4 decimals, for --json."""
    def r(v):
        return round(v, 4) if isinstance(v, float) else v
    out = dict(res, labels={})
    for n, x in res["labels"].items():
        y = dict(x, threshold=r(x["threshold"]), current={k: r(v) for k, v in x["current"].items()})
        if x["suggested"]:
            y["suggested"] = {k: r(v) for k, v in x["suggested"].items()}
        out["labels"][n] = y
    return out


def main(argv=None, prog="media-intake calibrate"):
    ap = argparse.ArgumentParser(prog=prog, description="Precision, recall and F1 of each SigLIP label at its "
                                 "threshold against your own Calibrate|<label>|yes / |no tags and trash decisions, "
                                 "and a suggested threshold. Reports only (docs/configuration.md).")
    ap.add_argument("--precision", type=float, metavar="P",
                    help="suggest the threshold with the most recall at precision >= P (0..1) instead of the best F1")
    ap.add_argument("--min", type=int, default=MIN_EACH, metavar="N",
                    help=f"positive and negative marks a label needs before a suggestion (default {MIN_EACH})")
    ap.add_argument("--with-videos", action="store_true",
                    help="count videos too (their index vector is a mean of frames: an approximation)")
    ap.add_argument("--json", action="store_true", help="one JSON object on stdout, for scripts")
    a = ap.parse_args(argv)
    if a.precision is not None and not 0 < a.precision <= 1:
        ap.error("--precision is a fraction in (0, 1]")
    if a.min < 1:
        ap.error("--min is at least 1")
    import numpy as np
    res = run(np, config.get()["state"], with_videos=a.with_videos, min_each=a.min, precision=a.precision)
    print(json.dumps(_round(res)) if a.json else text(res, a.with_videos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
