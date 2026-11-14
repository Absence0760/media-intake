"""Owner decisions in XMP sidecar tags: the one definition every tool reads.

Stdlib only, no side effects at import, so intake.py, gallery.py and the venv tools
(vlm_trash.py, semantic.py) share it. Rules (docs/intake.md, "Tags: who writes what"):

  Owner|keep       you kept this file (Keep on the review page or in the gallery)
  Owner|not-trash  never suggest this file as trash again (written with Owner|keep)
  Owner|trash      you trashed this content; on a sidecar only for a library copy that
                   is still on disk (`trash` deletes the paths you name; other copies
                   of the same sha256 are marked, not deleted). The durable record is
                   the sha256 in <state>/trashed.json, which keeps it from coming back.

No tool removes an Owner| tag except a newer owner decision on the same file (Keep
drops Owner|trash). AI tags never override them.

Review|not-trash is the legacy spelling of Owner|not-trash. Tools read it as its
Owner| spelling until it is rewritten (`media-intake migrate-tags`, or any tag write
to that sidecar); nothing writes it any more.
"""
KEEP = "Owner|keep"
NOT_TRASH = "Owner|not-trash"
TRASH = "Owner|trash"
LEGACY = {"Review|not-trash": NOT_TRASH}
ACCEPTED = "3"  # digiKam pick label Accepted (XMP-digiKam:PickLabel)


def normalise(tags):
    """tags with each legacy spelling read as its Owner| tag."""
    return [LEGACY.get(t, t) for t in tags]


def pick_label(v):
    """digiKam's PickLabel as a string ('' when unset), whether exiftool printed it as text or a number."""
    return "" if v is None else str(v).strip()


def kept(tags, pick=""):
    """You kept it: Owner|keep, Owner|not-trash (or the legacy spelling), or digiKam Accepted."""
    t = set(normalise(tags))
    return KEEP in t or NOT_TRASH in t or pick_label(pick) == ACCEPTED


def trashed(tags):
    """You trashed this content (a surviving copy); a later Keep on the file wins."""
    return TRASH in tags and not kept(tags)


def decided(tags, pick=""):
    """Any owner decision on the file: kept, trashed, or any digiKam pick label."""
    return kept(tags, pick) or TRASH in tags or pick_label(pick) not in ("", "0")
