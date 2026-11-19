"""Owner decisions in XMP sidecar tags: the one definition every tool reads.

Stdlib only, no side effects at import, so intake.py, gallery.py and the venv tools
(vlm_trash.py, semantic.py) share it. Rules (docs/intake.md, "Tags: who writes what"):

  Owner|keep       you kept this file (Keep on the review page or in the gallery)
  Owner|not-trash  never suggest this file as trash again (written with Owner|keep)
  Owner|trash      you trashed this content; on a sidecar only for a library copy that
                   is still on disk (`trash` deletes the paths you name; other copies
                   of the same sha256 are marked, not deleted). The durable record is
                   the sha256 in <state>/trashed.json, which keeps it from coming back.
  xmp:Rating >= 5  a favorite (the gallery's star writes 5; digiKam's five stars are the
                   same field). Counts as kept: never suggested as junk, never judged.
  People|<Name>, Pets|<Name>
                   someone or a pet you named is in it (`people tag` / `pets tag` write
                   only names you gave or confirmed, never a suggestion; one you typed
                   yourself is yours too). named(): never suggested as junk, never judged
                   by the vision check, never Review|maybe-trash. Not a Keep: a trash
                   decision on the file still stands.

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
FAVORITE = 5  # xmp:Rating (XMP-xmp:Rating, 0-5 stars) at or above this is a favorite; the gallery writes 5
NAMED = ("People|", "Pets|")  # recognition's tags (recog.py): names you gave or confirmed


def normalise(tags):
    """tags with each legacy spelling read as its Owner| tag."""
    return [LEGACY.get(t, t) for t in tags]


def pick_label(v):
    """digiKam's PickLabel as a string ('' when unset), whether exiftool printed it as text or a number."""
    return "" if v is None else str(v).strip()


def rating(v):
    """xmp:Rating as an int (exiftool prints it as a number or text, "5" or "5.0"); None when unset or bad."""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError, OverflowError):
        return None


def favorite(stars):
    """A favorite: xmp:Rating at least FAVORITE (the gallery's star, or five stars set in digiKam)."""
    r = rating(stars)
    return r is not None and r >= FAVORITE


def kept(tags, pick="", stars=None):
    """You kept it: Owner|keep, Owner|not-trash (or the legacy spelling), digiKam Accepted, or a favorite
    (stars: the sidecar's xmp:Rating)."""
    t = set(normalise(tags))
    return KEEP in t or NOT_TRASH in t or pick_label(pick) == ACCEPTED or favorite(stars)


def named(tags):
    """A person or pet you named is in it: a People|<Name> or Pets|<Name> tag. Never junk."""
    return any(t.startswith(NAMED) and len(t) > len(t.split("|", 1)[0]) + 1 for t in tags)


def trashed(tags):
    """You trashed this content (a surviving copy); a later Keep on the file wins."""
    return TRASH in tags and not kept(tags)


def decided(tags, pick="", stars=None):
    """Any owner decision on the file: kept (a favorite too), trashed, or any digiKam pick label."""
    return kept(tags, pick, stars) or TRASH in tags or pick_label(pick) not in ("", "0")
