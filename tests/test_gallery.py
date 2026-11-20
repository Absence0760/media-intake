#!/usr/bin/env python3
"""Tests for gallery.py on synthetic media in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_gallery.py

Images are drawn with Pillow, videos are ffmpeg test patterns. Trash and Erase run
the real intake.py (trash / forget) against the throwaway HOME only; nothing points
at the real ~/.local/share/media-intake, ~/Pictures or ~/Videos. A toy plugin
checks the gallery_views() seam (plugins.py). The job tests swap in a fake intake.py
(MEDIA_REVIEW_INTAKE) whose trash waits on a FIFO the test opens, so a job is held
mid-step without a sleep while servers are killed and restarted. The system-player action
runs a fake xdg-open put first on PATH, which only records its arguments. Events (rename, merge, set
on a selection, an event held only in XMP-iptcExt:Event) and the share-safe export (to the
default folder and to --share-out DIR) are tested through the HTTP API; ViewsEndpointsTest covers their
guards and the 409 while the library is locked with hand-written sidecars, so it needs no exiftool.
"""
import errno, hashlib, http.client, io, json, os, re, shutil, signal, stat, subprocess, sys, tempfile, time
import unittest, urllib.parse
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
GALLERY = os.path.join(HERE, "gallery.py")
INTAKE = os.path.join(HERE, "intake.py")
PY = sys.executable

from PIL import Image  # noqa: E402

EMPTY = ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
         "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n</rdf:RDF>\n</x:xmpmeta>\n"
         "<?xpacket end='w'?>\n")

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def img(p, color, size=(96, 64)):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    Image.new("RGB", size, color).save(p)


def vid(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                    "-pix_fmt", "yuv420p", p], check=True)


def tags_of(p):
    x = p + ".xmp"
    if not os.path.exists(x):
        return [], [], None
    r = subprocess.run(["exiftool", "-j", "-q", "-HierarchicalSubject", "-XMP-dc:Subject", "-XMP-tiff:Orientation",
                        "-n", x], capture_output=True, text=True)
    row = json.loads(r.stdout)[0]
    as_l = lambda v: [] if v is None else [str(i) for i in v] if isinstance(v, list) else [str(v)]  # noqa: E731
    return as_l(row.get("HierarchicalSubject")), as_l(row.get("Subject")), row.get("Orientation")


def gallery_read(p):
    """The gallery's own sidecar reader (stdlib XML) on p's sidecar, or {} without one."""
    import gallery
    return gallery.read_xmp(p + ".xmp") if os.path.exists(p + ".xmp") else {}


def write_tags(p, hier, extra=()):
    x = p + ".xmp"
    with open(x, "w") as f:
        f.write(EMPTY)
    args = []
    for h in hier:
        args += [f"-XMP-lr:HierarchicalSubject+={h}", f"-XMP-dc:Subject+={h.split('|')[-1]}"]
    subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", *args, *extra, x], check=True)


GATED = r'''
import os, sys
gate = os.environ["FAKE_GATE"]
if os.path.exists(gate):
    print("trash: waiting at the gate", flush=True)
    with open(gate) as f:
        f.read()
    os.remove(gate)
for p in sys.argv[2:]:
    if os.path.isabs(p) and os.path.lexists(p):
        os.remove(p)
        print("trashed 1")
'''


def until(pred, what, timeout=60):
    """Poll a real condition (the job API) until it holds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


FAKE_OPENER = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_OPEN_LOG"] + ".tmp", "w") as f:
    json.dump(sys.argv[1:], f)
os.replace(os.environ["FAKE_OPEN_LOG"] + ".tmp", os.environ["FAKE_OPEN_LOG"])
'''


TOY_PLUGIN = """
import os
SECRET = os.path.join(os.environ["PHOTO_LIBRARY"], "2026/2026-11/secret.jpg")
POSTS = []  # every body a toy post handler ran with: a handler refused at the lock never runs
def post(rest, body):
    POSTS.append(body)
    return 200, dict(got=body)
def gallery_views():
    return [dict(id="toy", title="Toy", script="window.mediaIntakeViews.toy={render(el,api){}};",
                 get=lambda rest, q: (200, dict(rest=rest, **q)) if rest == "hello"
                 else (200, dict(posts=len(POSTS))) if rest == "posts" else None,
                 post=post,
                 media=lambda rest: dict(path="__CLIP__", mime="video/mp4") if rest == "clip"
                 else dict(path=SECRET, mime="image/jpeg") if rest == "secret" else None),
            dict(id="ro", title="Read-only", post_read_only=True, post=post)]
"""


class Server:
    def __init__(self, env, *extra):
        self.p = subprocess.Popen([PY, "-I", GALLERY, *extra], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        line = self.p.stdout.readline()
        m = re.fullmatch(r"xdg-open 'http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)'\n", line)
        if not m:
            self.p.kill()
            raise AssertionError(f"first line not an xdg-open URL: {line!r} {self.p.stdout.read()}")
        self.port, self.token = int(m.group(1)), m.group(2)
        self.note = self.p.stdout.readline()
        self._csrf = None

    def req(self, method, path, host=None, headers=None, body=None, token=True, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Token"] = self.token
        h.update(headers or {})
        c.putrequest(method, path, skip_host=True)
        for k, v in h.items():
            c.putheader(k, v)
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        if data is not None:
            if "Content-Type" not in h:
                c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r, out

    def csrf(self):
        if not self._csrf:
            r, b = self.req("GET", "/")
            self._csrf = re.search(rb'<meta name="csrf" content="([^"]+)">', b).group(1).decode()
        return self._csrf

    def get(self, path):
        r, b = self.req("GET", path)
        assert r.status == 200, (path, r.status, b[:200])
        return json.loads(b)

    def post(self, path, body):
        r, b = self.req("POST", path, headers={"X-CSRF": self.csrf()}, body=body)
        return r.status, json.loads(b or b"{}")

    def stop(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        self.p.stdout.close()


class GalleryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-test-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.foot = os.path.join(self.home, "Videos/demo-footage")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.foot))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.thumbs = os.path.join(self.home, ".cache/media-intake/thumbs")
        self.servers = []
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        self.a = L("2026/2026-11/a.jpg")          # junk suggestion, AI 0.9, label food, place
        self.b = L("2026/2026-10/b.jpg")          # no sidecar
        self.c = L("2026/2026-11/c.png")          # child-in-shot, used
        self.k = L("2026/2026-11/k.jpg")          # owner kept: never suggested
        self.lo = L("2026/2026-11/lo.jpg")        # AI 0.5: below the suggestion line
        self.v = os.path.join(self.vids, "2026/2026-09/v.mp4")
        self.clip = os.path.join(self.foot, "originals/2026-11-08-city-expo/phone/clip.mp4")
        img(self.a, (200, 30, 30), (400, 300))
        img(self.b, (30, 200, 30))
        img(self.c, (30, 30, 200))
        img(self.k, (90, 90, 90))
        img(self.lo, (120, 20, 120))
        vid(self.v)
        vid(self.clip)
        vid(os.path.join(self.foot, "stock/road.mp4"))
        write_tags(self.a, ["Project|Personal", "Source|phone camera", "Label|food", "Review|maybe-trash",
                            "AI|vlm/m1/trash/blurry", "AI|vlm/m1/conf/0.9", "Place|Riverton, EL, US"])
        write_tags(self.c, ["Project|Personal", "Review|child-in-shot", "Used|2026-11-10-demo-test"])
        write_tags(self.k, ["Project|Personal", "Review|maybe-trash", "Owner|keep"])
        write_tags(self.lo, ["Project|Personal", "AI|vlm/m1/trash/dark", "AI|vlm/m1/conf/0.5"])
        write_tags(self.clip, ["Project|Demo", "Event|city-expo", "Review|maybe-trash", "Review|child-in-shot"])
        # Never listed: flagged (erase log), quarantine, staging, review, a symlink out, a hidden folder.
        self.secret = L("2026/2026-11/secret.jpg")
        img(self.secret, (1, 2, 3))
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-01 10:00 b0 sha256:{sha(self.secret)} size:{os.path.getsize(self.secret)}\n")
        img(os.path.join(self.state, "quarantine/b1/DCIM/q.jpg"), (5, 5, 5))
        img(os.path.join(self.state, "batches/b1/raw/DCIM/r.jpg"), (6, 6, 6))
        img(os.path.join(self.state, "batches/b1/review/DCIM/rv.jpg"), (7, 7, 7))
        outside = os.path.join(self.tmp, "outside.jpg")
        img(outside, (8, 8, 8))
        os.symlink(outside, L("2026/2026-11/link.jpg"))
        img(L(".hidden/h.jpg"), (9, 9, 9))
        self.originals = {p: sha(p) for p in (self.a, self.b, self.c, self.k, self.v)}

    def tearDown(self):
        for s in self.servers:
            s.stop()
        shutil.rmtree(self.tmp)

    def start(self, *extra):
        s = Server(self.env, *extra)
        self.servers.append(s)
        return s

    def names(self, s, **f):
        j = s.get("/api/list?" + urllib.parse.urlencode(dict(f, limit=500)))
        return [i["n"] for i in j["items"]], j

    def id_of(self, s, name):
        j = s.get("/api/list?limit=500")
        return next(i["id"] for i in j["items"] if i["n"] == name)

    def wait_job(self, s):
        for _ in range(600):
            j = s.get("/api/job")
            if j["done"]:
                return j
            time.sleep(0.1)
        raise self.failureException("job never finished")

    def test_security(self):
        s = self.start()
        self.assertTrue(s.note.startswith("(") and "private browser window" in s.note)
        r, _ = s.req("GET", "/api/list", token=False)
        self.assertEqual(r.status, 403)
        r, _ = s.req("GET", "/api/list", host=f"evil.example:{s.port}")
        self.assertEqual(r.status, 403)
        for path in ("/api/facets", "/api/job", "/api/job?since=0", "/api/progress"):  # every API route
            r, _ = s.req("GET", path, token=False)
            self.assertEqual(r.status, 403, path)
            r, _ = s.req("GET", path, token=False, headers={"X-Token": "wrong"})
            self.assertEqual(r.status, 403, path)
        r, _ = s.req("GET", f"/?t={s.token}", token=False)
        self.assertEqual(r.status, 303)
        ck = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", ck)
        self.assertIn("SameSite=Strict", ck)
        r, b = s.req("GET", "/api/facets", token=False, headers={"Cookie": ck.split(";")[0]})
        self.assertEqual(r.status, 200)
        self.assertIn("no-store", r.getheader("Cache-Control"))
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        r, b = s.req("GET", "/")
        self.assertNotIn(b"http://", b.replace(b"http://www.w3.org", b""))  # nothing from anywhere else
        # POST: CSRF and Origin
        ida = self.id_of(s, "a.jpg")
        r, _ = s.req("POST", "/api/keep", body={"ids": [ida]})
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/keep", headers={"X-CSRF": s.csrf(), "Origin": "http://evil.example"},
                     body={"ids": [ida]})
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/keep", headers={"X-CSRF": s.csrf(), "Content-Type": "text/plain"}, raw=b"{}")
        self.assertEqual(r.status, 415)
        # Media by opaque id only
        for p in ("/m/../../etc/passwd", "/m/x", f"/m/{2 ** 20}", "/t/-1", "/m/" + self.a, "/c/0"):
            r, _ = s.req("GET", p)
            self.assertEqual(r.status, 404, p)
        r, b = s.req("GET", f"/m/{ida}", headers={"Range": "bytes=0-9"})
        self.assertEqual((r.status, len(b)), (206, 10))
        with open(self.a, "rb") as f:
            self.assertEqual(b, f.read(10))
        # Nothing from quarantine, staging, review, the erase log, symlinks or hidden folders
        names, j = self.names(s)
        self.assertEqual(sorted(names), ["a.jpg", "b.jpg", "c.png", "clip.mp4", "k.jpg", "lo.jpg", "v.mp4"])
        self.assertIn("1 flagged/erased left out", s.note)
        # No item JSON carries a path
        d = s.get(f"/api/item/{ida}")
        self.assertNotIn(self.home, json.dumps(d).replace("~/", ""))

    # ------------------------------------------------------------ listing and filters
    def test_filters(self):
        s = self.start()
        n = lambda **f: sorted(self.names(s, **f)[0])  # noqa: E731
        self.assertEqual(n(project="Demo"), ["clip.mp4"])
        self.assertEqual(n(label="food"), ["a.jpg"])
        self.assertEqual(n(place="Riverton, EL, US"), ["a.jpg"])
        self.assertEqual(n(event="city-expo"), ["clip.mp4"])
        self.assertEqual(n(source="phone camera"), ["a.jpg", "b.jpg", "c.png", "clip.mp4", "k.jpg", "lo.jpg", "v.mp4"])
        self.assertEqual(n(review="maybe-trash"), ["a.jpg", "clip.mp4", "k.jpg"])
        self.assertEqual(n(review="child-in-shot"), ["c.png", "clip.mp4"])
        self.assertEqual(n(review="untagged"), ["b.jpg", "v.mp4"])
        self.assertEqual(n(ai="trash", aimin="0.7"), ["a.jpg"])
        self.assertEqual(n(ai="trash", aimin="0.4"), ["a.jpg", "lo.jpg"])
        self.assertEqual(n(used="used"), ["c.png"])
        self.assertNotIn("c.png", n(used="unused", days="10000"))
        self.assertIn("c.png", n(used="unused", days="1"))  # used 10 Nov 2026: more than a day ago
        self.assertEqual(n(kind="video"), ["clip.mp4", "v.mp4"])
        self.assertEqual(n(ym="2026-10"), ["b.jpg"])
        self.assertEqual(n(project="Personal", kind="photo", review="maybe-trash"), ["a.jpg", "k.jpg"])
        # Newest first, grouped by month
        months = [i["mo"] for i in self.names(s)[1]["items"]]
        self.assertEqual(months, sorted(months, reverse=True))
        # Paging
        j1, j2 = s.get("/api/list?limit=3"), s.get("/api/list?limit=3&offset=3")
        self.assertEqual(j1["total"], 7)
        self.assertFalse({i["id"] for i in j1["items"]} & {i["id"] for i in j2["items"]})
        # Text search: no model in the temp HOME, so it says so and matches tags and names
        names, j = self.names(s, q="food")
        self.assertEqual(names, ["a.jpg"])
        self.assertIn("unavailable", j["note"])
        self.assertIn("semantic search off", "semantic search off" if s.get("/api/facets")["search"] else "")
        # Similar without an index says so
        j = s.get(f"/api/list?similar={self.id_of(s, 'b.jpg')}")
        self.assertEqual(j["total"], 0)
        self.assertIn("run media-intake index", j["note"])
        # Trash suggestions: maybe-trash or AI >= 0.7, never kept / used / Demo; by confidence
        names, j = self.names(s, view="suggest")
        self.assertEqual(names, ["a.jpg"])
        f = s.get("/api/facets")["facets"]
        self.assertIn(["food", 1], f["label"])

    def test_suggested_junk_why_and_score(self):
        """Suggested junk lists the vision check's verdicts from 0.7 up by default and lower on request (?jmin=),
        narrows by reason (?why=), and says in `junk` what each reason holds, how many more a lower score adds,
        and how many it holds without the other filters. Kept, used and protected files never, at any score."""
        s = self.start()
        names, j = self.names(s, view="suggest")
        self.assertEqual(names, ["a.jpg"])
        self.assertEqual(next(i for i in j["items"] if i["n"] == "a.jpg")["r"], "blurry")  # the tile's reason
        jk = j["junk"]
        self.assertEqual((jk["all"], jk["unfiltered"], jk["lower"], jk["floor"], jk["line"]), (1, 1, 1, 0.7, 0.7))
        self.assertEqual(dict(map(tuple, jk["why"])), {"blurry": 1, "label": 1})
        # a lower score: lo.jpg (0.5, dark) after a.jpg, most confident first; k.jpg (kept), c.png (used) and
        # clip.mp4 (a protected project) stay out even at any score
        names, j = self.names(s, view="suggest", jmin="0")
        self.assertEqual(names, ["a.jpg", "lo.jpg"])
        self.assertEqual(j["junk"]["lower"], 0)
        self.assertEqual(dict(map(tuple, j["junk"]["why"])), {"blurry": 1, "label": 1, "dark": 1})
        # Why: a reason, the content label's tag, a reason below the score (and how many a lower one adds)
        self.assertEqual(self.names(s, view="suggest", why="dark", jmin="0")[0], ["lo.jpg"])
        self.assertEqual(self.names(s, view="suggest", why="label")[0], ["a.jpg"])
        names, j = self.names(s, view="suggest", why="dark")
        self.assertEqual((names, j["junk"]["lower"]), ([], 1))
        for bad in ("x", "", "nan"):  # not a number: the usual line
            self.assertEqual(self.names(s, view="suggest", jmin=bad)[0], ["a.jpg"], bad)
        self.assertEqual(self.names(s, view="suggest", jmin="-3")[0], ["a.jpg", "lo.jpg"])  # clamped to 0
        # other filters narrow it; `unfiltered` keeps the view's own count for "N suggested, M shown"
        names, j = self.names(s, view="suggest", kind="video")
        self.assertEqual((names, j["junk"]["unfiltered"]), ([], 1))
        # the sidebar: the view's count, how many a lower score adds, and Judged junk by score for the slider
        f = s.get("/api/facets")
        self.assertEqual((f["counts"]["suggest"], f["counts"]["suggest:lower"]), (1, 1))
        self.assertEqual(f["ai_scores"], [[0.5, 1], [0.9, 1]])
        for lo in ("0.7", "0.4"):
            self.assertEqual(sum(n for sc, n in f["ai_scores"] if sc >= float(lo)),
                             len(self.names(s, ai="trash", aimin=lo)[0]), lo)

    # ------------------------------------------------------------ sidecar actions
    def test_keep_tag_rotate(self):
        s = self.start()
        ida, idb = self.id_of(s, "a.jpg"), self.id_of(s, "b.jpg")
        code, j = s.post("/api/keep", {"ids": [ida, idb]})
        self.assertEqual(code, 200, j)
        h, dc, _ = tags_of(self.a)
        self.assertIn("Owner|keep", h)
        self.assertIn("Owner|not-trash", h)
        self.assertNotIn("Review|maybe-trash", h)
        self.assertIn("Label|food", h)  # nothing else touched
        self.assertNotIn("maybe-trash", dc)
        self.assertIn("Owner|keep", tags_of(self.b)[0])  # sidecar created
        self.assertEqual(self.names(s, view="suggest")[0], [])
        # my tags: added, removed; tool namespaces refused
        code, j = s.post("/api/tag", {"ids": [ida], "tag": "food", "op": "add"})
        self.assertEqual(code, 200, j)
        code, j = s.post("/api/tag", {"ids": [ida], "tag": "Trips|Spain", "op": "add"})
        self.assertEqual(code, 200, j)
        self.assertIn("Trips|Spain", tags_of(self.a)[0])
        self.assertIn("Spain", tags_of(self.a)[1])
        self.assertIn(["Trips|Spain", 1], s.get("/api/facets")["facets"]["mine"])
        for bad in ("Label|x", "review|x", "AI|vlm/x", "Owner|keep", "Used|2026-01-01-x", "", "a\nb"):
            code, _ = s.post("/api/tag", {"ids": [ida], "tag": bad, "op": "add"})
            self.assertEqual(code, 400, bad)
        code, _ = s.post("/api/tag", {"ids": [ida], "tag": "Label|food", "op": "remove"})
        self.assertEqual(code, 400)
        code, j = s.post("/api/tag", {"ids": [ida], "tag": "food", "op": "remove"})
        self.assertEqual(code, 200, j)
        h, dc, _ = tags_of(self.a)
        self.assertNotIn("food", h)
        self.assertIn("Label|food", h)
        self.assertIn("food", dc)  # leaf still used by Label|food
        # rotate: sidecar only
        t = os.path.join(self.thumbs, self.originals[self.a] + "-r0.jpg")
        r, _ = s.req("GET", f"/t/{ida}")
        self.assertEqual(r.status, 200)
        with Image.open(t) as im:
            self.assertEqual(im.size, (320, 240))
        self.assertEqual(oct(os.stat(t).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.thumbs).st_mode & 0o777), "0o700")
        code, j = s.post("/api/rotate", {"ids": [ida], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(tags_of(self.a)[2], 6)
        t90 = os.path.join(self.thumbs, self.originals[self.a] + "-r90.jpg")
        self.assertFalse(os.path.exists(t90))  # made on the next request, rotated (its own cache key)
        s.req("GET", f"/t/{ida}")
        with Image.open(t90) as im:
            self.assertEqual(im.size, (240, 320))
        self.assertEqual(s.get(f"/api/item/{ida}")["rot"], 90)
        s.post("/api/rotate", {"ids": [ida], "dir": "cw"})
        self.assertEqual(tags_of(self.a)[2], 3)
        s.post("/api/rotate", {"ids": [ida], "dir": "ccw"})
        s.post("/api/rotate", {"ids": [ida], "dir": "ccw"})
        self.assertEqual(tags_of(self.a)[2], 1)
        code, _ = s.post("/api/rotate", {"ids": [ida], "dir": "up"})
        self.assertEqual(code, 400)
        code, _ = s.post("/api/keep", {"ids": [999]})
        self.assertEqual(code, 400)
        for p, h0 in self.originals.items():
            self.assertEqual(sha(p), h0, p)  # originals never written

    def test_named_person_or_pet_never_suggested(self):
        """A People|<Name> or Pets|<Name> tag (recognition writes only names you gave or confirmed) keeps a file
        out of Suggested junk at any Junk score; it is not shown as Kept."""
        write_tags(self.a, ["Project|Personal", "Review|maybe-trash", "AI|vlm/m1/trash/blurry", "AI|vlm/m1/conf/0.9",
                            "Pets|Rex"])
        write_tags(self.lo, ["Project|Personal", "AI|vlm/m1/trash/dark", "AI|vlm/m1/conf/0.5", "People|Ann"])
        s = self.start()
        for f in ({}, {"jmin": "0"}):
            self.assertNotIn("a.jpg", self.names(s, view="suggest", **f)[0], f)
            self.assertNotIn("lo.jpg", self.names(s, view="suggest", **f)[0], f)
        item = next(i for i in self.names(s)[1]["items"] if i["n"] == "a.jpg")
        self.assertFalse(item.get("kept"), "a name is no Keep")
        write_tags(self.a, ["Project|Personal", "Owner|trash", "Pets|Rex"])  # a copy you trashed: still offered
        s2 = self.start()
        self.assertIn("a.jpg", self.names(s2, view="suggest")[0])

    def test_favorites(self):
        """The star: xmp:Rating 5 in the sidecar and nothing else; a favorite (also five stars from digiKam) is
        never Suggested junk; the Favorites view (review=favorite) and its count; four stars are no favorite."""
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-xmp:Rating=5", self.clip + ".xmp"], check=True)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-xmp:Rating=4", self.lo + ".xmp"], check=True)
        s = self.start()
        ida, idb = self.id_of(s, "a.jpg"), self.id_of(s, "b.jpg")
        rating = lambda p: gallery_read(p).get("rating")  # noqa: E731
        self.assertEqual(self.names(s, review="favorite")[0], ["clip.mp4"])  # digiKam's five stars
        self.assertEqual(s.get("/api/facets")["counts"]["review:favorite"], 1)
        sug = self.names(s, view="suggest")[0]
        self.assertNotIn("clip.mp4", sug)  # Review|maybe-trash, but a favorite
        self.assertIn("a.jpg", sug)
        before = tags_of(self.a)
        code, j = s.post("/api/favorite", {"ids": [ida, idb], "on": True})
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"], "Added 2 to Favorites. They won't be suggested as junk.")
        self.assertIn("undo", j)
        self.assertEqual({i["id"]: i["fav"] for i in j["items"]}, {ida: True, idb: True})
        self.assertEqual((rating(self.a), rating(self.b)), ("5", "5"))  # b's sidecar made for it
        self.assertEqual(tags_of(self.a), before)  # no tag, leaf or orientation touched
        self.assertNotIn("Owner|keep", tags_of(self.b)[0])
        self.assertEqual(sorted(self.names(s, review="favorite")[0]), ["a.jpg", "b.jpg", "clip.mp4"])
        self.assertNotIn("a.jpg", self.names(s, view="suggest")[0])
        self.assertTrue(s.get(f"/api/item/{ida}")["fav"])
        self.assertEqual(s.get("/api/facets")["counts"]["review:favorite"], 3)
        # starring again changes nothing (no token); unstarring deletes the rating
        code, j = s.post("/api/favorite", {"ids": [ida], "on": True})
        self.assertEqual((code, j["result"]), (200, "Already in Favorites: nothing changed."))
        self.assertNotIn("undo", j)
        code, j = s.post("/api/favorite", {"ids": [ida], "on": False})
        self.assertEqual((code, j["result"]), (200, "Removed 1 from Favorites."), j)
        self.assertIsNone(rating(self.a))
        self.assertIn("a.jpg", self.names(s, view="suggest")[0])  # a suggestion again
        self.assertEqual(s.get(f"/api/item/{self.id_of(s, 'lo.jpg')}")["fav"], False)  # four stars
        for bad in ({"ids": [ida], "on": "yes"}, {"ids": [ida]}, {"ids": [999], "on": True}):
            code, _ = s.post("/api/favorite", bad)
            self.assertEqual(code, 400, bad)
        for p, h0 in self.originals.items():
            self.assertEqual(sha(p), h0, p)  # originals never written

    # ------------------------------------------------------------ trash / erase and the thumbnail cache
    def test_trash_and_erase(self):
        s = self.start()
        ida, idb = self.id_of(s, "a.jpg"), self.id_of(s, "b.jpg")
        for i in (ida, idb):
            self.assertEqual(s.req("GET", f"/t/{i}")[0].status, 200)
        ta = os.path.join(self.thumbs, self.originals[self.a] + "-r0.jpg")
        tb = os.path.join(self.thumbs, self.originals[self.b] + "-r0.jpg")
        self.assertTrue(os.path.exists(ta) and os.path.exists(tb))
        code, _ = s.post("/api/trash", {"ids": [ida]})
        self.assertEqual(code, 400)  # no confirm
        self.assertTrue(os.path.exists(self.a))
        code, j = s.post("/api/trash", {"ids": [ida], "confirm": True})
        self.assertEqual(code, 200, j)
        j = self.wait_job(s)
        self.assertTrue(j["ok"], j)
        self.assertFalse(os.path.exists(self.a) or os.path.exists(self.a + ".xmp") or os.path.exists(ta))
        with open(os.path.join(self.state, "trashed.json")) as f:
            trashed = json.load(f)
        self.assertIn(self.originals[self.a], trashed)
        self.assertNotIn("a.jpg", self.names(s)[0])
        self.assertEqual(s.req("GET", f"/m/{ida}")[0].status, 404)
        # Erase: needs ERASE typed
        for bad in (None, True, "erase", "yes"):
            code, _ = s.post("/api/erase", {"ids": [idb], "confirm": bad})
            self.assertEqual(code, 400)
        code, j = s.post("/api/erase", {"ids": [idb], "confirm": "ERASE"})
        self.assertEqual(code, 200, j)
        j = self.wait_job(s)
        out = "\n".join(j["lines"])
        self.assertTrue(j["ok"], out)
        self.assertIn("$ intake.py forget [1 file] --yes", out)
        self.assertFalse(os.path.exists(self.b) or os.path.exists(tb))
        # Once the erase ended, no name or path of what it erased is left in <state>/jobs/ (the erase log keeps
        # only sha256 and size); the job keeps counts, its step label and ok.
        jd = os.path.join(self.state, "jobs")
        for f in os.listdir(jd):
            with open(os.path.join(jd, f), "rb") as fh:
                data = fh.read()
            for n in ("b.jpg", self.b, self.lib, self.home):
                self.assertNotIn(n.encode(), data, (f, n))
        with open(os.path.join(jd, "gallery.json")) as f:
            st = json.load(f)
        self.assertEqual((st["state"], st["ok"], st["step"], st["total"]), ("done", True, 1, 1))
        self.assertEqual((st["spec"]["label"], st["spec"]["count"], len(st["spec"]["files"])), ("erase", 1, 1))
        # a server started now sees the scrubbed output only, but still the summary line
        s2 = self.start()
        j2 = s2.get("/api/job")
        self.assertNotIn("b.jpg", json.dumps(j2))
        self.assertEqual(j2["lines"][-1], "done: 1 of 1 gone from the library")
        with open(os.path.join(self.state, "erase-log.txt")) as f:
            self.assertIn(self.originals[self.b], f.read())
        os.remove(self.secret)  # the fixture's own erase-log entry (planted, not erased)
        r = subprocess.run([PY, "-I", INTAKE, "verify-gone"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # verify-gone checks the gallery cache too: every rotation's thumbnail, and the older content-only key
        for name in ("-r270.jpg", ".jpg"):
            left = os.path.join(self.thumbs, self.originals[self.b] + name)
            with open(left, "wb") as f:
                f.write(b"\xff\xd8 planted")
            r = subprocess.run([PY, "-I", INTAKE, "verify-gone"], env=self.env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("STILL PRESENT: 1 copies: gallery thumbnails (~/.cache/media-intake/thumbs) 1", r.stdout)
            os.remove(left)

    def test_apply_skips_a_trash_mark_starred_or_kept_since(self):
        """Suggested junk's Apply (/api/decide) re-reads each sidecar marked Trash: one starred (this page) or kept
        (another tool) since the mark is skipped and said so, the rest trashed. Plain Trash on a favorite stays
        allowed: it is asked for item by item and confirmed."""
        s = self.start()
        ida, idb, idlo = self.id_of(s, "a.jpg"), self.id_of(s, "b.jpg"), self.id_of(s, "lo.jpg")
        code, j = s.post("/api/favorite", {"ids": [ida], "on": True})
        self.assertEqual(code, 200, j)
        write_tags(self.b, ["Owner|keep"])  # kept elsewhere (digiKam, the CLI) after the page loaded
        code, j = s.post("/api/decide", {"keep": [], "trash": [ida, idb, idlo]})
        self.assertEqual(code, 400, j)  # Apply's trash still needs its confirm
        code, j = s.post("/api/decide", {"keep": [], "trash": [ida, idb, idlo], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertEqual((j["result"], j["trashed"]), ("Trashing 1. Skipped 2: favorite/kept.", [idlo]))
        self.assertTrue(self.wait_job(s)["ok"])
        self.assertFalse(os.path.exists(self.lo))
        self.assertTrue(os.path.exists(self.a) and os.path.exists(self.b))
        # every mark skipped: no job at all
        code, j = s.post("/api/decide", {"keep": [], "trash": [ida], "confirm": True})
        self.assertEqual((code, j["result"], j["trashed"]), (200, "Skipped 1: favorite/kept.", []), j)
        self.assertEqual(s.get("/api/job")["ids"], [idlo])  # still the last job
        # plain Trash of a favorite: allowed (its own confirm already says what goes)
        code, j = s.post("/api/trash", {"ids": [ida], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertTrue(self.wait_job(s)["ok"])
        self.assertFalse(os.path.exists(self.a))

    def test_display_name_goes_with_the_last_file_of_its_event(self):
        """Erase or Trash of an event's last file, or Set event moving the last one out, drops the display name
        you typed from event-names.json; the event made again later shows its default; Undo bringing an
        event back brings it back with its default name. Another event's name stays."""
        names = os.path.join(self.state, "event-names.json")
        def stored():
            with open(names) as f:
                return next(iter(json.load(f)["libraries"].values()), {})
        write_tags(self.b, ["Event|beach"])
        write_tags(self.lo, ["Project|Personal", "Event|hike"])
        s = self.start()
        for ev, n in (("city-expo", "The expo"), ("beach", "Sea days"), ("hike", "Mountain walk")):
            code, j = s.post("/api/event/name", {"event": ev, "name": n})
            self.assertEqual(code, 200, j)
        idclip, idb, idlo = self.id_of(s, "clip.mp4"), self.id_of(s, "b.jpg"), self.id_of(s, "lo.jpg")
        code, j = s.post("/api/erase", {"ids": [idclip], "confirm": "ERASE"})
        self.assertEqual(code, 200, j)
        self.assertTrue(self.wait_job(s)["ok"])
        s.get("/api/facets")  # any request after the job reconciles it
        self.assertEqual(stored(), {"beach": "Sea days", "hike": "Mountain walk"})
        with open(names) as f:
            self.assertNotIn("The expo", f.read())  # the typed text is gone from the file
        code, j = s.post("/api/trash", {"ids": [idb], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertTrue(self.wait_job(s)["ok"])
        s.get("/api/facets")
        self.assertEqual(stored(), {"hike": "Mountain walk"})
        # Set event "" on the last file: the name goes; Undo makes the event again, with its default name
        code, j = s.post("/api/event/set", {"ids": [idlo], "event": ""})
        self.assertEqual(code, 200, j)
        self.assertEqual(stored(), {})
        code, u = s.post("/api/undo", {"undo": j["undo"]})
        self.assertEqual(code, 200, u)
        self.assertEqual(s.get("/api/facets")["event_names"]["hike"], {"label": "Hike", "name": ""})
        # an event made again by another path (Set event here; apply or tag-files the same) starts plain
        code, j = s.post("/api/event/set", {"ids": [self.id_of(s, "k.jpg")], "event": "city-expo"})
        self.assertEqual(code, 200, j)
        self.assertEqual(s.get("/api/facets")["event_names"]["city-expo"], {"label": "City expo", "name": ""})

    def test_twins_keep_their_own_rotation(self):
        """Byte-identical copies share a thumbnail only while turned alike: rotating one copy (its sidecar) shows
        the new turn at once and leaves its twin upright; a copy turned the same way reuses that thumbnail; Trash
        of one copy removes every variant of the content."""
        twin, third = (os.path.join(self.lib, "2026/2026-08", n) for n in ("twin.jpg", "third.jpg"))
        for p in (twin, third):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copyfile(self.a, p)
        s = self.start()
        ida, idt, idu = (self.id_of(s, n) for n in ("a.jpg", "twin.jpg", "third.jpg"))

        def size(i):
            r, b = s.req("GET", f"/t/{i}")
            self.assertEqual(r.status, 200, i)
            with Image.open(io.BytesIO(b)) as im:
                return im.size

        self.assertEqual([size(i) for i in (ida, idt, idu)], [(320, 240)] * 3)
        code, j = s.post("/api/rotate", {"ids": [ida], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(size(ida), (240, 320))  # the new turn at once
        self.assertEqual(size(idt), (320, 240))  # its twin keeps its own (none)
        code, j = s.post("/api/rotate", {"ids": [idu], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(size(idu), (240, 320))
        h = self.originals[self.a]
        self.assertEqual(sorted(f for f in os.listdir(self.thumbs) if f.startswith(h)),
                         [h + "-r0.jpg", h + "-r90.jpg"])  # one file per turn, shared by the copies turned alike
        code, j = s.post("/api/trash", {"ids": [ida], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertTrue(self.wait_job(s)["ok"])
        self.assertEqual([f for f in os.listdir(self.thumbs) if f.startswith(h)], [])  # every variant
        self.assertEqual(sha(twin), h)  # originals never written

    def test_intake_cli_drops_gallery_thumbs(self):
        """trash and forget (and so erase) delete every ~/.cache/media-intake/thumbs/<sha>-r<turn>.jpg (each
        rotation) and the older content-only <sha>.jpg without the gallery."""
        os.makedirs(self.thumbs)
        names = (".jpg", "-r0.jpg", "-r90.jpg", "-r180.jpg", "-r270.jpg")
        ta = [os.path.join(self.thumbs, self.originals[self.a] + n) for n in names]
        tk = [os.path.join(self.thumbs, self.originals[self.k] + n) for n in names]
        other = [os.path.join(self.thumbs, "f" * 64 + n) for n in names]
        for p in ta + tk + other:
            open(p, "wb").close()
        r = subprocess.run([PY, "-I", INTAKE, "trash", self.a, "--yes"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([p for p in ta if os.path.exists(p)], [])
        r = subprocess.run([PY, "-I", INTAKE, "forget", self.k, "--yes"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([p for p in tk if os.path.exists(p)], [])
        self.assertTrue(all(os.path.exists(p) for p in other))  # only those shas

    def test_job_survives_restart_and_refuses_a_second_action(self):
        fake, gate = os.path.join(self.tmp, "gated_intake.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(GATED)
        os.mkfifo(gate)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s1 = self.start()
        ida, idb = self.id_of(s1, "a.jpg"), self.id_of(s1, "b.jpg")
        code, j = s1.post("/api/trash", {"ids": [ida], "confirm": True})
        self.assertEqual(code, 200, j)
        until(lambda: any("waiting at the gate" in x for x in s1.get("/api/job")["lines"]), "the trash to start")
        j = s1.get("/api/job")
        self.assertEqual((j["done"], j["state"], j["ids"], j["step"], j["total"]), (False, "running", [ida], 1, 1))
        self.assertNotIn("spec", j)
        self.assertEqual(s1.req("GET", f"/t/{ida}")[0].status, 404)  # busy while it runs
        code, j = s1.post("/api/erase", {"ids": [idb], "confirm": "ERASE"})
        self.assertEqual(code, 409, j)  # one action at a time
        jd = os.path.join(self.state, "jobs")
        for f in ("gallery.json", "gallery.log", "gallery.lock"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(jd, f)).st_mode), 0o600, f)
        self.assertEqual(stat.S_IMODE(os.stat(jd).st_mode), 0o700)
        # The server dies; a new one (and its page) picks the job up from the status file.
        s1.p.send_signal(signal.SIGKILL)
        s1.p.wait()
        s2 = self.start()
        self.assertIn("still running", s2.p.stdout.readline())
        ida2, idb2 = self.id_of(s2, "a.jpg"), self.id_of(s2, "b.jpg")
        j = s2.get("/api/job")
        self.assertEqual((j["done"], j["ids"]), (False, [ida2]))
        self.assertEqual(j["lines"][0], "$ intake.py trash 'a.jpg' --yes")
        self.assertEqual(s2.req("GET", f"/m/{ida2}")[0].status, 404)
        code, j = s2.post("/api/trash", {"ids": [idb2], "confirm": True})
        self.assertEqual(code, 409, j)
        def w():
            try:
                fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno == errno.ENXIO:
                    return False
                raise
            os.write(fd, b"go")
            os.close(fd)
            return True
        until(w, "the fake to wait at the gate")
        j = self.wait_job(s2)
        self.assertTrue(j["ok"], j)
        self.assertEqual(j["lines"][-1], "done: 1 of 1 gone from the library")
        self.assertFalse(os.path.exists(self.a))
        self.assertNotIn("a.jpg", self.names(s2)[0])
        self.assertEqual(s2.req("GET", f"/m/{ida2}")[0].status, 404)
        # finished: the next action starts at once
        code, j = s2.post("/api/trash", {"ids": [idb2], "confirm": True})
        self.assertEqual(code, 200, j)
        self.assertTrue(self.wait_job(s2)["ok"])
        self.assertFalse(os.path.exists(self.b))

    def test_crashed_job_is_reported(self):
        fake, gate = os.path.join(self.tmp, "gated_intake.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(GATED)
        os.mkfifo(gate)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s = self.start()
        ida = self.id_of(s, "a.jpg")
        code, j = s.post("/api/trash", {"ids": [ida], "confirm": True})
        self.assertEqual(code, 200, j)
        until(lambda: any("waiting at the gate" in x for x in s.get("/api/job")["lines"]), "the trash to start")
        with open(os.path.join(self.state, "jobs", "gallery.json")) as f:
            os.kill(json.load(f)["pid"], signal.SIGKILL)
        j = self.wait_job(s)
        self.assertEqual((j["state"], j["ok"], j["step_running"]), ("crashed", False, True))
        code, _ = s.post("/api/trash", {"ids": [self.id_of(s, "b.jpg")], "confirm": True})
        self.assertEqual(code, 409)  # the step the dead worker started still holds the lock
        self.assertEqual(s.req("GET", f"/t/{ida}")[0].status, 404)  # and its files stay busy meanwhile
        fd = os.open(gate, os.O_WRONLY)  # the fake is waiting on it: this returns at once
        os.write(fd, b"go")
        os.close(fd)
        until(lambda: not s.get("/api/job")["step_running"], "the orphaned step to end")
        self.assertEqual(s.get("/api/job")["state"], "crashed")
        self.assertNotIn("a.jpg", self.names(s)[0])  # the orphaned step did finish; the listing follows the disk

    # ------------------------------------------------------------ plugin views
    def with_toy_plugin(self):
        pdir = os.path.join(self.tmp, "plugins")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "toy_plugin.py"), "w") as f:
            f.write(TOY_PLUGIN.replace("__CLIP__", self.clip))
        cfg = os.path.join(self.tmp, "plugin-config.toml")
        with open(self.env["MEDIA_INTAKE_CONFIG"]) as f:
            base = f.read()
        with open(cfg, "w") as f:
            f.write(base + f'\n[plugins]\npaths = ["{pdir}"]\nmodules = ["toy_plugin"]\n')
        self.env["MEDIA_INTAKE_CONFIG"] = cfg

    def test_plugin_view(self):
        self.with_toy_plugin()
        s = self.start()
        r, page = s.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b'"id": "toy"', page)
        self.assertIn(b"window.mediaIntakeViews.toy", page)
        self.assertEqual(s.get("/plugin/toy/hello?x=1"), {"rest": "hello", "x": "1"})
        code, j = s.post("/plugin/toy/echo", {"a": 1})
        self.assertEqual((code, j), (200, {"got": {"a": 1}}))
        r, b = s.req("GET", "/plugin/toy/media/clip", headers={"Range": "bytes=0-3"})
        self.assertEqual(r.status, 206)
        r, _ = s.req("GET", "/plugin/toy/media/secret")  # a scan-flagged file: never served
        self.assertEqual(r.status, 404)
        r, _ = s.req("GET", "/plugin/nope/hello")
        self.assertEqual(r.status, 404)
        r, _ = s.req("GET", "/plugin/toy/hello", token=False)  # the gallery's guard runs first
        self.assertNotEqual(r.status, 200)

    def test_plugin_post_takes_the_library_lock(self):
        """A plugin POST runs under the library lock (as Keep does): 409 naming the holder while a job holds it,
        unless the view declares post_read_only."""
        self.with_toy_plugin()
        fake, gate = os.path.join(self.tmp, "gated_intake.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(GATED)
        os.mkfifo(gate)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s = self.start()
        code, j = s.post("/api/trash", {"ids": [self.id_of(s, "a.jpg")], "confirm": True})
        self.assertEqual(code, 200, j)
        until(lambda: any("waiting at the gate" in x for x in s.get("/api/job")["lines"]), "the trash to start")
        code, j = s.post("/plugin/toy/write", {"a": 1})
        self.assertEqual(code, 409, j)
        self.assertEqual((j["error"], j.get("busy")), ("a Trash is still running; wait for it to finish, then try again", True))  # plain words: no PID, no lock path
        self.assertEqual(s.get("/plugin/toy/posts"), {"posts": 0})  # the handler never ran
        code, j = s.post("/plugin/ro/look", {"b": 2})  # declared read-only: not locked
        self.assertEqual((code, j), (200, {"got": {"b": 2}}))
        fd = os.open(gate, os.O_WRONLY)  # the fake is waiting on it: this returns at once
        os.write(fd, b"go")
        os.close(fd)
        self.assertTrue(self.wait_job(s)["ok"])
        code, j = s.post("/plugin/toy/write", {"a": 1})  # the job is over: the lock is free
        self.assertEqual((code, j), (200, {"got": {"a": 1}}))
        self.assertEqual(s.get("/plugin/toy/posts"), {"posts": 2})

    # ------------------------------------------------------------ an unplayable video's codec
    def test_item_gives_a_videos_codec(self):
        """/api/item says the codec ffprobe read (the large view names HEVC only when it is), none for a photo."""
        s = self.start()
        ids = {i["n"]: i["id"] for i in s.get("/api/list?limit=50")["items"]}
        self.assertIsNone(s.get(f"/api/item/{ids['a.jpg']}")["codec"])
        self.assertEqual(s.get(f"/api/item/{ids['v.mp4']}")["codec"], "h264")

    # ------------------------------------------------------------ page layout and the sidebar's counts
    def test_page_layout_and_facet_counts(self):
        """Left sidebar (views, search, filters), bottom action bar, lightbox info pane; nothing external."""
        s = self.start()
        r, b = s.req("GET", "/")
        self.assertEqual(r.status, 200)
        for needle in (b'<aside id="sidebar"', b'<nav id="views"', b'id="filters"', b'id="q"', b'id="chips"',
                       b'id="tsize"', b'id="actionbar" hidden', b'aria-label="Selection actions"', b'<aside id="info"',
                       b'data-v="fav"', b'data-v="recent"', b'data-v="child"', b'data-v="untagged"',
                       b'data-dialog="confirm"', b'id="bfav"', b'id="bunfav"'):
            self.assertIn(needle, b)
        self.assertNotIn(b"://", b)  # no URL of any kind, so nothing loads from anywhere else
        for bad in (b"<link", b"@import", b"url(", b"<iframe", b"@font-face"):
            self.assertNotIn(bad, b)
        self.assertEqual(re.findall(rb"<(?:script|img|video|source)[^>]*\bsrc=", b), [])
        f = s.get("/api/facets")
        c = f["counts"]
        self.assertEqual(f["total"], 7)
        self.assertEqual((c["kind:photo"], c["kind:video"]), (5, 2))
        self.assertEqual(c["review:maybe-trash"], 3)
        for k, flt in (("review:child-in-shot", dict(review="child-in-shot")),
                       ("review:untagged", dict(review="untagged")), ("review:favorite", dict(review="favorite")),
                       ("ai:trash", dict(ai="trash", aimin="0")), ("ai:none", dict(ai="none")),
                       ("used:used", dict(used="used")), ("suggest", dict(view="suggest"))):
            self.assertEqual(c.get(k, 0), len(self.names(s, **flt)[0]), k)  # each count is what its filter lists
        self.assertEqual(c["used:used"] + c["used:unused"], f["total"])
        # Added in the last 30 days: every file in this fresh HOME was filed (its inode changed) just now, so it
        # counts them all; its name says the window (WORDS)
        self.assertEqual(f["recent_days"], 30)
        self.assertIn(b"Added in the last 30 days", b)
        self.assertEqual(c["recent"], f["total"])
        self.assertEqual(len(self.names(s, added=str(f["recent_days"]))[0]), f["total"])
        for bad in ("0", "-3", "x", ""):  # not a positive day count: no filter
            self.assertEqual(len(self.names(s, added=bad)[0]), f["total"], bad)

    # ------------------------------------------------------------ the system player (xdg-open)
    def test_open_in_system_player(self):
        fdir = os.path.join(self.tmp, "fakebin")
        os.makedirs(fdir)
        opener = os.path.join(fdir, "xdg-open")
        with open(opener, "w") as f:
            f.write(FAKE_OPENER)
        os.chmod(opener, 0o700)
        log = os.path.join(self.tmp, "opened.json")
        self.env.update(PATH=fdir + os.pathsep + self.env["PATH"], FAKE_OPEN_LOG=log)
        s = self.start()
        idv, idb, idk = self.id_of(s, "v.mp4"), self.id_of(s, "b.jpg"), self.id_of(s, "k.jpg")
        # a POST from the page only: no token, no CSRF or another Origin never runs it
        r, _ = s.req("POST", "/api/open", token=False, headers={"X-CSRF": s.csrf()}, body={"ids": [idv]})
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/open", body={"ids": [idv]})
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/open", headers={"X-CSRF": s.csrf(), "Origin": "http://evil.example"},
                     body={"ids": [idv]})
        self.assertEqual(r.status, 403)
        for bad in ([], [idv, idb], [999], ["0"], [True], None, "0"):
            code, _ = s.post("/api/open", {"ids": bad})
            self.assertEqual(code, 400, bad)
        self.assertFalse(os.path.exists(log))
        code, j = s.post("/api/open", {"ids": [idv]})
        self.assertEqual(code, 200, j)
        def opened():
            if not os.path.exists(log):
                return None
            with open(log) as f:
                return json.load(f)
        self.assertEqual(until(opened, "the fake opener to record its arguments"), [self.v])
        os.remove(log)
        # swapped for a symlink after the scan, or flagged since: refused like the media routes refuse it
        os.remove(self.b)
        os.symlink(os.path.join(self.tmp, "outside.jpg"), self.b)
        code, _ = s.post("/api/open", {"ids": [idb]})
        self.assertEqual(code, 400)
        self.assertEqual(s.req("GET", f"/m/{idb}")[0].status, 404)
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"2026-11-02 10:00 b0 sha256:{sha(self.k)} size:{os.path.getsize(self.k)}\n")
        code, _ = s.post("/api/open", {"ids": [idk]})
        self.assertEqual(code, 400)
        self.assertFalse(os.path.exists(log))

    # ------------------------------------------------------------ a trash job's surviving copies
    def test_trash_refreshes_copies_tags(self):
        """trash marks other library copies Owner|trash; the gallery shows that without a restart."""
        copy = os.path.join(self.lib, "2026/2026-08/a-copy.jpg")
        os.makedirs(os.path.dirname(copy))
        shutil.copyfile(self.a, copy)
        h = sha(self.a)
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({p: {"sha": h, "size": os.path.getsize(p)} for p in (self.a, copy)}, f)
        s = self.start()
        ida, idc = self.id_of(s, "a.jpg"), self.id_of(s, "a-copy.jpg")
        self.assertNotIn("a-copy.jpg", self.names(s, view="suggest")[0])
        code, j = s.post("/api/trash", {"ids": [ida], "confirm": True})
        self.assertEqual(code, 200, j)
        j = self.wait_job(s)
        self.assertTrue(j["ok"], j)
        self.assertIn("Owner|trash", tags_of(copy)[0])  # what intake wrote
        names, lst = self.names(s, view="suggest")
        self.assertIn("a-copy.jpg", names)
        self.assertIn("trashed copy", next(i for i in lst["items"] if i["id"] == idc)["f"])
        self.assertIn("Owner|trash", s.get(f"/api/item/{idc}")["tags"])
        self.assertEqual(s.get("/api/facets")["counts"]["suggest"], len(names))

    # ------------------------------------------------------------ events: the sidebar list, rename, merge
    def test_events_rename_and_merge(self):
        """Events come from Event| tags; rename and merge rewrite only Event| (and XMP-iptcExt:Event) in the
        sidecars that have it: Owner| and every other tag stay, originals are never written."""
        write_tags(self.b, ["Event|beach", "Trips|beach"])  # a tag of mine with the same leaf
        write_tags(self.k, ["Project|Personal", "Review|maybe-trash", "Owner|keep", "Event|beach"],
                   extra=["-XMP-iptcExt:Event=beach"])
        write_tags(self.lo, ["Project|Personal", "AI|vlm/m1/trash/dark", "AI|vlm/m1/conf/0.5", "Event|hike"])
        self.originals.update({p: sha(p) for p in (self.k, self.lo, self.clip)})
        s = self.start()
        ev = dict(map(tuple, s.get("/api/facets")["facets"]["event"]))
        self.assertEqual(ev, {"beach": 2, "hike": 1, "city-expo": 1})
        self.assertEqual(sorted(self.names(s, event="beach")[0]), ["b.jpg", "k.jpg"])
        # rename: a slug, a new name, an event that exists
        for body in ({"from": "beach", "to": "Not A Slug"}, {"from": "beach", "to": ""},
                     {"from": "beach", "to": "hike"}, {"from": "nope", "to": "coast"}, {"from": "beach", "to": "beach"}, {"from": None, "to": "coast"},
                     {"from": "beach", "to": "-x"}):
            code, j = s.post("/api/event/rename", body)
            self.assertEqual(code, 400, (body, j))
        self.assertIn("Event|beach", tags_of(self.k)[0])  # nothing written by a refusal
        code, j = s.post("/api/event/rename", {"from": "beach", "to": "coast"})
        self.assertEqual(code, 200, j)
        self.assertIn("2 sidecars", j["result"])
        h, dc, _ = tags_of(self.k)
        self.assertIn("Event|coast", h)
        self.assertNotIn("Event|beach", h)
        for t in ("Owner|keep", "Project|Personal", "Review|maybe-trash"):
            self.assertIn(t, h)
        self.assertIn("coast", dc)
        self.assertNotIn("beach", dc)  # no other tag of k.jpg uses the leaf
        r = subprocess.run(["exiftool", "-s3", "-XMP-iptcExt:Event", self.k + ".xmp"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "coast")
        h, dc, _ = tags_of(self.b)
        self.assertEqual(sorted(h), ["Event|coast", "Trips|beach"])
        self.assertIn("beach", dc)  # still used by Trips|beach
        ev = dict(map(tuple, s.get("/api/facets")["facets"]["event"]))
        self.assertEqual(ev, {"coast": 2, "hike": 1, "city-expo": 1})
        self.assertEqual(sorted(self.names(s, event="coast")[0]), ["b.jpg", "k.jpg"])
        self.assertEqual(self.names(s, event="beach")[0], [])
        # merge: into an existing event only, never into itself
        for body in ({"from": ["coast"], "into": "nope"}, {"from": ["coast"], "into": "coast"},
                     {"from": ["nope"], "into": "hike"}, {"from": [], "into": "hike"}, {"from": [3], "into": "hike"},
                     {"from": ["coast"], "into": None}):
            code, j = s.post("/api/event/merge", body)
            self.assertEqual(code, 400, (body, j))
        code, j = s.post("/api/event/merge", {"from": ["coast", "city-expo"], "into": "hike"})
        self.assertEqual(code, 200, j)
        ev = dict(map(tuple, s.get("/api/facets")["facets"]["event"]))
        self.assertEqual(ev, {"hike": 4})
        self.assertEqual(sorted(self.names(s, event="hike")[0]), ["b.jpg", "clip.mp4", "k.jpg", "lo.jpg"])
        h = tags_of(self.clip)[0]
        self.assertIn("Event|hike", h)
        self.assertNotIn("Event|city-expo", h)
        self.assertIn("Project|Demo", h)
        self.assertIn("Owner|keep", tags_of(self.k)[0])
        self.assertEqual(tags_of(self.a)[0].count("Event|hike"), 0)  # never in an event: untouched
        for p, h0 in self.originals.items():
            self.assertEqual(sha(p), h0, p)

    def test_rename_an_event_held_only_in_iptc(self):
        """An event a file carries only as XMP-iptcExt:Event (no Event| tag: another tool wrote it) is listed,
        and renaming it rewrites that field and adds Event|<new> to keep the two in step; other tags stay."""
        write_tags(self.b, [], extra=["-XMP-iptcExt:Event=dunes"])
        write_tags(self.lo, ["Project|Personal", "Owner|keep"], extra=["-XMP-iptcExt:Event=dunes"])
        self.originals[self.lo] = sha(self.lo)
        s = self.start()
        ev = dict(map(tuple, s.get("/api/facets")["facets"]["event"]))
        self.assertEqual(ev.get("dunes"), 2)
        self.assertEqual(sorted(self.names(s, event="dunes")[0]), ["b.jpg", "lo.jpg"])
        code, j = s.post("/api/event/rename", {"from": "dunes", "to": "sands"})
        self.assertEqual(code, 200, j)
        self.assertIn("2 sidecars", j["result"])
        for p in (self.b, self.lo):
            r = subprocess.run(["exiftool", "-s3", "-XMP-iptcExt:Event", p + ".xmp"], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "sands", p)
            h, dc, _ = tags_of(p)
            self.assertIn("Event|sands", h, p)
            self.assertIn("sands", dc, p)
            self.assertFalse([t for t in h if t.startswith("Event|") and t != "Event|sands"], p)
        self.assertEqual(sorted(tags_of(self.lo)[0]), ["Event|sands", "Owner|keep", "Project|Personal"])
        ev = dict(map(tuple, s.get("/api/facets")["facets"]["event"]))
        self.assertNotIn("dunes", ev)
        self.assertEqual(ev.get("sands"), 2)
        self.assertEqual(sorted(self.names(s, event="sands")[0]), ["b.jpg", "lo.jpg"])
        self.assertEqual(self.names(s, event="dunes")[0], [])
        for p, h0 in self.originals.items():
            self.assertEqual(sha(p), h0, p)

    def test_display_name_follows_rename_and_merge(self):
        """Rename moves an event's display name to the new slug (a stale name left for that slug never attaches);
        merge keeps the target's name and drops the merged events'."""
        write_tags(self.b, ["Event|beach"])
        write_tags(self.lo, ["Project|Personal", "Event|hike"])
        names = os.path.join(self.state, "event-names.json")
        s = self.start()
        for ev, n in (("beach", "Sea days"), ("hike", "Mountain walk"), ("city-expo", "The expo")):
            code, j = s.post("/api/event/name", {"event": ev, "name": n})
            self.assertEqual(code, 200, j)
        # a name stored for "coast" from before (an event since emptied): never picked up by the rename
        with open(names) as f:
            d = json.load(f)
        (key, lib), = d["libraries"].items()
        lib["coast"] = "Stale"
        with open(names, "w") as f:
            json.dump(d, f)
        code, j = s.post("/api/event/rename", {"from": "beach", "to": "coast"})
        self.assertEqual(code, 200, j)
        en = s.get("/api/facets")["event_names"]
        self.assertEqual(en["coast"], {"label": "Sea days", "name": "Sea days"})
        self.assertNotIn("beach", en)
        with open(names) as f:
            self.assertNotIn("beach", json.load(f)["libraries"][key])
        code, j = s.post("/api/event/merge", {"from": ["coast", "city-expo"], "into": "hike"})
        self.assertEqual(code, 200, j)
        en = s.get("/api/facets")["event_names"]
        self.assertEqual(en, {"hike": {"label": "Mountain walk", "name": "Mountain walk"}})
        with open(names) as f:
            self.assertEqual(json.load(f)["libraries"][key], {"hike": "Mountain walk"})
        self.assertEqual(stat.S_IMODE(os.stat(names).st_mode), 0o600)
        # Set event making a new event: a name left over for that slug never attaches either
        with open(names) as f:
            d = json.load(f)
        d["libraries"][key]["picnic"] = "Stale"
        with open(names, "w") as f:
            json.dump(d, f)
        code, j = s.post("/api/event/set", {"ids": [self.id_of(s, "a.jpg")], "event": "picnic"})
        self.assertEqual(code, 200, j)
        self.assertEqual(s.get("/api/facets")["event_names"]["picnic"], {"label": "Picnic", "name": ""})
        with open(names) as f:
            self.assertEqual(json.load(f)["libraries"][key], {"hike": "Mountain walk"})
        # Set event into an event that exists keeps its name
        code, j = s.post("/api/event/set", {"ids": [self.id_of(s, "b.jpg")], "event": "hike"})
        self.assertEqual(code, 200, j)
        self.assertEqual(s.get("/api/facets")["event_names"]["hike"]["label"], "Mountain walk")
        # an unreadable names file: rename and merge still rewrite the sidecars, say the names weren't
        # updated, and leave the file as it was
        with open(names, "w") as f:
            f.write("{not json")
        code, j = s.post("/api/event/rename", {"from": "hike", "to": "trail"})
        self.assertEqual(code, 200, j)
        self.assertIn("display names not updated", j["result"])
        self.assertIn("Event|trail", tags_of(self.lo)[0])
        code, j = s.post("/api/event/merge", {"from": ["picnic"], "into": "trail"})
        self.assertEqual(code, 200, j)
        self.assertIn("display names not updated", j["result"])
        self.assertIn("Event|trail", tags_of(self.a)[0])
        with open(names) as f:
            self.assertEqual(f.read(), "{not json")
        for p, h0 in self.originals.items():
            self.assertEqual(sha(p), h0, p)

    # ------------------------------------------------------------ bulk edits on a selection
    def test_bulk_edits(self):
        """Tags, event and rotate on several items at once, through the same endpoints as one item."""
        s = self.start()
        ida, idb, idk = self.id_of(s, "a.jpg"), self.id_of(s, "b.jpg"), self.id_of(s, "k.jpg")
        sel = [ida, idb, idk]
        code, j = s.post("/api/tag", {"ids": sel, "tag": "Trips|Spain", "op": "add"})
        self.assertEqual(code, 200, j)
        for p in (self.a, self.b, self.k):
            self.assertIn("Trips|Spain", tags_of(p)[0], p)
        self.assertIn(["Trips|Spain", 3], s.get("/api/facets")["facets"]["mine"])
        code, j = s.post("/api/tag", {"ids": sel, "tag": "Trips|Spain", "op": "remove"})
        self.assertEqual(code, 200, j)
        for p in (self.a, self.b, self.k):
            self.assertNotIn("Trips|Spain", tags_of(p)[0], p)
        # set event: replaces each item's other events; refused for a non-slug; "" takes them out of every event
        code, _ = s.post("/api/event/set", {"ids": sel, "event": "Picnic Day"})
        self.assertEqual(code, 400)
        code, _ = s.post("/api/event/set", {"ids": sel})
        self.assertEqual(code, 400)
        idc = self.id_of(s, "clip.mp4")
        code, j = s.post("/api/event/set", {"ids": sel + [idc], "event": "picnic"})
        self.assertEqual(code, 200, j)
        for p in (self.a, self.b, self.k, self.clip):
            h = [t for t in tags_of(p)[0] if t.startswith("Event|")]
            self.assertEqual(h, ["Event|picnic"], p)
        self.assertIn("Owner|keep", tags_of(self.k)[0])
        self.assertIn("Label|food", tags_of(self.a)[0])
        self.assertEqual(sorted(self.names(s, event="picnic")[0]), ["a.jpg", "b.jpg", "clip.mp4", "k.jpg"])
        code, j = s.post("/api/event/set", {"ids": [ida, idc], "event": ""})
        self.assertEqual(code, 200, j)
        self.assertFalse([t for t in tags_of(self.a)[0] if t.startswith("Event|")])
        self.assertEqual(s.get(f"/api/item/{idc}")["event"], "")
        self.assertEqual(sorted(self.names(s, event="picnic")[0]), ["b.jpg", "k.jpg"])
        # rotate the selection: each one's own orientation, sidecar only
        code, j = s.post("/api/rotate", {"ids": [ida, idb], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual((tags_of(self.a)[2], tags_of(self.b)[2]), (6, 6))
        code, j = s.post("/api/rotate", {"ids": [ida], "dir": "ccw"})
        self.assertEqual(code, 200, j)
        code, j = s.post("/api/rotate", {"ids": [ida, idb], "dir": "ccw"})
        self.assertEqual((tags_of(self.a)[2], tags_of(self.b)[2]), (8, 1))
        code, _ = s.post("/api/event/set", {"ids": [ida, 999], "event": "x"})
        self.assertEqual(code, 400)  # one unknown id refuses the whole selection
        for p, h0 in self.originals.items():
            self.assertEqual(sha(p), h0, p)

    # ------------------------------------------------------------ share-safe export
    def test_share_safe_export(self):
        """share-copy (exiftool here) on the selection into a new folder under ~/Pictures/share-safe."""
        g = os.path.join(self.lib, "2026/2026-07/g.jpg")
        os.makedirs(os.path.dirname(g))
        ex = Image.Exif()
        ex[0x0110] = "SyntheticCam"
        Image.new("RGB", (64, 48), (10, 120, 200)).save(g, exif=ex)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-GPSLatitude=37.536", "-GPSLatitudeRef=N",
                        "-GPSLongitude=77.4347", "-GPSLongitudeRef=W", g], check=True)
        write_tags(g, ["Event|trip"])
        before = {p: sha(p) for p in (g, g + ".xmp")}
        s = self.start()
        idg = self.id_of(s, "g.jpg")
        code, j = s.post("/api/share", {"ids": [idg]})
        self.assertEqual(code, 200, j)
        self.assertRegex(j["dest"], r"^~/Pictures/share-safe/\d{4}-\d\d-\d\d-\d{6}$")
        out = os.path.join(self.home, j["dest"][2:])
        j = self.wait_job(s)
        self.assertTrue(j["ok"], j)
        self.assertTrue(any(ln.strip() == f"{out}/g.jpg  gps=none" for ln in j["lines"]), j["lines"])
        r = subprocess.run(["exiftool", "-j", "-q", "-n", "-GPSLatitude", "-Model", os.path.join(out, "g.jpg")],
                           capture_output=True, text=True)
        row = json.loads(r.stdout)[0]
        self.assertNotIn("GPSLatitude", row)
        self.assertNotIn("Model", row)
        self.assertEqual({p: sha(p) for p in before}, before)  # the original and its sidecar only read
        self.assertFalse(out.startswith(self.lib + os.sep))  # never a library file

    # ------------------------------------------------------------ close
    def test_close_button(self):
        s = self.start()
        code, _ = s.post("/api/close", {})
        self.assertEqual(code, 200)
        self.assertEqual(s.p.wait(timeout=10), 0)


def raw_xmp(p, hier, event=None):
    """A sidecar written as plain XMP text (the gallery reads sidecars with stdlib XML): for the tests that
    never write a sidecar themselves, so they need no exiftool."""
    li = lambda xs: "".join(f"<rdf:li>{x}</rdf:li>" for x in xs)  # noqa: E731
    ev = f"<Iptc4xmpExt:Event>{event}</Iptc4xmpExt:Event>" if event else ""
    with open(p + ".xmp", "w", encoding="utf-8") as f:
        f.write("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
                "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
                "<rdf:Description rdf:about='' xmlns:lr='http://ns.adobe.com/lightroom/1.0/' "
                "xmlns:dc='http://purl.org/dc/elements/1.1/' "
                "xmlns:Iptc4xmpExt='http://iptc.org/std/Iptc4xmpExt/2008-02-29/'>\n"
                f"<lr:hierarchicalSubject><rdf:Bag>{li(hier)}</rdf:Bag></lr:hierarchicalSubject>\n"
                f"<dc:subject><rdf:Bag>{li(h.split('|')[-1] for h in hier)}</rdf:Bag></dc:subject>{ev}\n"
                "</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")


class ViewsEndpointsTest(unittest.TestCase):
    """Events, bulk event and share-safe export endpoints: the guards, 409 while the library is locked, and the
    export itself. No step here writes a sidecar, so these run without exiftool (share-copy then re-encodes with
    Pillow; with exiftool it strips with exiftool: both leave no metadata)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gallery-views-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        os.makedirs(self.state)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_INTAKE", None)
        self.a = os.path.join(self.lib, "2026/2026-11/a.jpg")
        self.b = os.path.join(self.lib, "2026/2026-10/b.jpg")
        self.b2 = os.path.join(self.lib, "2026/2026-09/b.jpg")  # the same file name as b.jpg, another folder
        os.makedirs(os.path.dirname(self.a))
        ex = Image.Exif()
        ex[0x0110] = "SyntheticCam"
        g = ex.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = "N", (37.0, 32.0, 10.0), "W", (77.0, 26.0, 5.0)
        Image.new("RGB", (64, 48), (200, 30, 30)).save(self.a, exif=ex)
        img(self.b, (30, 200, 30))
        img(self.b2, (30, 30, 200))
        raw_xmp(self.a, ["Project|Personal", "Owner|keep", "Event|beach"], event="beach")
        raw_xmp(self.b, ["Event|hike", "Trips|beach"])
        self.share_root = os.path.join(self.home, "Pictures/share-safe")
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def start(self, *extra):
        s = Server(self.env, *extra)
        self.servers.append(s)
        return s

    def ids(self, s):
        return {(i["n"], i["d"][:7]): i["id"] for i in s.get("/api/list?limit=500")["items"]}

    def files(self):
        out = {}
        for p in (self.a, self.b, self.b2):
            for q in (p, p + ".xmp"):
                if os.path.exists(q):
                    out[q] = sha(q)
        return out

    def bodies(self, s):
        """A valid body for every new POST endpoint (each would change something if it got through)."""
        ids = self.ids(s)
        ida, idb = ids[("a.jpg", "2026-11")], ids[("b.jpg", "2026-10")]
        return {"/api/event/set": {"ids": [ida, idb], "event": "picnic"},
                "/api/event/rename": {"from": "beach", "to": "coast"},
                "/api/event/merge": {"from": ["hike"], "into": "beach"},
                "/api/event/name": {"event": "beach", "name": "Beach day"},
                "/api/share": {"ids": [ida]},
                "/api/favorite": {"ids": [ida, idb], "on": True},
                "/api/undo": {"undo": "not-a-token"}}

    def test_display_names_of_emptied_events_dropped_at_start(self):
        """An event emptied while no gallery ran (a CLI erase): the next gallery drops its display name at start,
        but only when every library folder is there and nothing was left out as flagged."""
        s = self.start()
        code, j = s.post("/api/event/name", {"event": "beach", "name": "Sea days"})
        self.assertEqual(code, 200, j)
        s.stop()
        names = os.path.join(self.state, "event-names.json")
        def stored():
            with open(names) as f:
                return next(iter(json.load(f)["libraries"].values()), {})
        os.remove(self.a)
        os.remove(self.a + ".xmp")
        self.start().stop()  # the videos and project folders are missing (an unmounted drive): kept
        self.assertEqual(stored(), {"beach": "Sea days"})
        os.makedirs(self.env["PERSONAL_FOOTAGE"])
        os.makedirs(os.path.join(self.home, "Videos/demo-footage/originals"))
        link = os.path.join(self.lib, "on-a-drive")  # a symlinked folder whose drive isn't mounted: not a full scan
        os.symlink(os.path.join(self.home, "no-such-drive"), link)
        self.start().stop()
        self.assertEqual(stored(), {"beach": "Sea days"})
        os.remove(link)
        self.start().stop()
        self.assertEqual(stored(), {})

    def test_events_listed_and_filtered(self):
        s = self.start()
        f = s.get("/api/facets")
        self.assertEqual(f["facets"]["event"], [["beach", 1], ["hike", 1]])
        self.assertEqual(f["share_out"], "~/Pictures/share-safe")
        j = s.get("/api/list?event=beach")
        self.assertEqual([i["n"] for i in j["items"]], ["a.jpg"])
        self.assertEqual(s.get("/api/list?event=hike")["total"], 1)
        r, page = s.req("GET", "/")
        for needle in (b'id="evlist"', b'id="evbar"', b'id="bevent"', b'id="bshare"', b'id="mfield"'):
            self.assertIn(needle, page)

    def test_event_display_names(self):
        """A display name is stored in <state>/event-names.json (0600, per library), survives a restart, never
        touches a sidecar, and is refused when it would look like another event's slug or name."""
        raw_xmp(self.b2, ["Event|some-trip-2024"])
        c = os.path.join(self.lib, "2026/2026-08/c.jpg")
        img(c, (90, 90, 90))
        raw_xmp(c, [], event="Hike day")  # another tool's event text, not a slug
        s = self.start()
        en = s.get("/api/facets")["event_names"]
        self.assertEqual(en["some-trip-2024"], {"label": "Some trip 2024", "name": ""})  # the default
        self.assertEqual(en["beach"], {"label": "Beach", "name": ""})
        self.assertEqual(en["Hike day"]["label"], "Hike day")
        before = self.files()
        names = os.path.join(self.state, "event-names.json")
        # bad input: nothing stored
        for body in ({"event": "beach", "name": "x" * 65}, {"event": "beach", "name": "a\nb"},
                     {"event": "beach", "name": "be\u200bach"}, {"event": "beach", "name": 5},
                     {"event": "no-such", "name": "Nope"}, {"event": "", "name": "Nope"}, {"name": "Nope"},
                     {"event": "beach", "name": "HIKE"},                 # another event's slug, but for case
                     {"event": "beach", "name": "  some  TRIP_2024 "},   # spacing, dashes, underscores
                     {"event": "beach", "name": "hike-day"}):            # another tool's event text
            code, j = s.post("/api/event/name", body)
            self.assertEqual(code, 400, (body, j))
        code, j = s.post("/api/event/name", {"event": "beach", "name": ""})  # clearing none: nothing written
        self.assertEqual(code, 200, j)
        self.assertIn("already shows its default name", j["result"])
        self.assertFalse(os.path.exists(names))
        code, j = s.post("/api/event/name", {"event": "beach", "name": "  Summer   at <b>the</b> sea "})
        self.assertEqual(code, 200, j)
        self.assertEqual(stat.S_IMODE(os.stat(names).st_mode), 0o600)
        with open(names) as f:
            d = json.load(f)
        self.assertEqual(d["version"], 1)
        (key, lib), = d["libraries"].items()
        self.assertRegex(key, r"^[0-9a-f]{16}$")  # a hash of the library's folders, never a path
        self.assertEqual(lib, {"beach": "Summer at <b>the</b> sea"})
        # a name set for one event: another event can't take one that looks like it
        code, j = s.post("/api/event/name", {"event": "hike", "name": "summer AT <b>the</b> sea"})
        self.assertEqual(code, 400, j)
        self.assertIn("looks like", j["error"])
        code, j = s.post("/api/event/name", {"event": "hike", "name": "Mountain walk"})
        self.assertEqual(code, 200, j)
        # a default that looks like another event's text falls back to the slug: some-trip-2024 beside
        # "Some Trip 2024" (another tool's), seen once the gallery restarts
        img(os.path.join(self.lib, "2026/2026-07/d.jpg"), (10, 90, 90))
        raw_xmp(os.path.join(self.lib, "2026/2026-07/d.jpg"), [], event="Some Trip 2024")
        self.assertEqual(self.files(), before)  # never a sidecar
        s.stop()
        s = self.start()  # a restarted gallery shows the same names (and now sees d.jpg)
        en = s.get("/api/facets")["event_names"]
        self.assertEqual(en["beach"], {"label": "Summer at <b>the</b> sea", "name": "Summer at <b>the</b> sea"})
        self.assertEqual(en["hike"]["label"], "Mountain walk")
        self.assertEqual(en["some-trip-2024"]["label"], "some-trip-2024")
        self.assertEqual(en["Some Trip 2024"]["label"], "Some Trip 2024")
        # "" goes back to the default
        code, j = s.post("/api/event/name", {"event": "hike", "name": ""})
        self.assertEqual(code, 200, j)
        self.assertEqual(s.get("/api/facets")["event_names"]["hike"], {"label": "Hike", "name": ""})
        # an unreadable file is never overwritten (another library's names may be in it)
        with open(names, "w") as f:
            f.write("{not json")
        self.assertEqual(s.get("/api/facets")["event_names"]["beach"]["label"], "Beach")
        code, j = s.post("/api/event/name", {"event": "beach", "name": "Sea"})
        self.assertEqual(code, 409, j)
        with open(names) as f:
            self.assertEqual(f.read(), "{not json")

    def test_share_out_flag_reaches_the_app(self):
        """gallery --share-out DIR: the page names DIR and an export lands in a new folder under it, never under
        the default ~/Pictures/share-safe."""
        out_root = os.path.join(self.home, "Exports/safe")
        s = self.start("--share-out", out_root)
        self.assertEqual(s.get("/api/facets")["share_out"], "~/Exports/safe")
        ida = self.ids(s)[("a.jpg", "2026-11")]
        code, j = s.post("/api/share", {"ids": [ida]})
        self.assertEqual(code, 200, j)
        self.assertRegex(j["dest"], r"^~/Exports/safe/\d{4}-\d\d-\d\d-\d{6}$")
        self.assertTrue(self.wait_job(s)["ok"])
        self.assertEqual(os.listdir(os.path.join(self.home, j["dest"][2:])), ["a.jpg"])
        self.assertFalse(os.path.exists(self.share_root), "the default folder is never used")
        # Outside HOME the page shows the absolute path.
        elsewhere = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(self.start("--share-out", elsewhere).get("/api/facets")["share_out"], elsewhere)

    def test_new_posts_refuse_a_bad_token_origin_host_or_type(self):
        s = self.start()
        before = self.files()
        for path, body in self.bodies(s).items():
            data = json.dumps(body).encode()
            for kw in (dict(token=False, headers={"X-CSRF": s.csrf()}),                        # no token or cookie
                       dict(headers={"X-CSRF": s.csrf(), "X-Token": "wrong"}),                # a wrong token
                       dict(headers={}),                                                      # no CSRF value
                       dict(headers={"X-CSRF": "wrong"}),                                     # a wrong CSRF value
                       dict(headers={"X-CSRF": s.csrf(), "Origin": "http://evil.example"}),   # another Origin
                       dict(headers={"X-CSRF": s.csrf()}, host=f"evil.example:{s.port}")):    # DNS rebinding
                r, _ = s.req("POST", path, raw=data, **kw)
                self.assertEqual(r.status, 403, (path, kw))
            r, _ = s.req("POST", path, headers={"X-CSRF": s.csrf(), "Content-Type": "text/plain"}, raw=data)
            self.assertEqual(r.status, 415, path)
        self.assertEqual(self.files(), before)
        self.assertFalse(os.path.exists(self.share_root))
        self.assertFalse(os.path.exists(os.path.join(self.state, "event-names.json")))  # no display name stored
        self.assertEqual(s.get("/api/job")["id"], None)  # no job started

    def test_rename_and_merge_refuse_an_unsafe_old_name_from_metadata(self):
        """An event name read from a file's metadata (not typed here) can look like an exiftool option; it is
        refused before any exiftool argument is built, and no sidecar changes."""
        raw_xmp(self.b, ["Trips|beach"], event="-o")
        s = self.start()
        self.assertIn(["-o", 1], s.get("/api/facets")["facets"]["event"])
        before = self.files()
        for path, body in (("/api/event/rename", {"from": "-o", "to": "coast"}),
                           ("/api/event/merge", {"from": ["-o"], "into": "beach"})):
            code, j = s.post(path, body)
            self.assertEqual(code, 400, (path, j))
            self.assertIn("can't be rewritten", j["error"], path)
        self.assertEqual(self.files(), before)

    def test_new_posts_get_409_while_the_library_is_locked(self):
        fake, gate = os.path.join(self.tmp, "gated_intake.py"), os.path.join(self.tmp, "gate")
        with open(fake, "w") as f:
            f.write(GATED)
        os.mkfifo(gate)
        self.env.update(MEDIA_REVIEW_INTAKE=fake, FAKE_GATE=gate)
        s = self.start()
        idb2 = self.ids(s)[("b.jpg", "2026-09")]
        code, j = s.post("/api/trash", {"ids": [idb2], "confirm": True})
        self.assertEqual(code, 200, j)
        until(lambda: any("waiting at the gate" in x for x in s.get("/api/job")["lines"]), "the trash to start")
        before = self.files()
        for path, body in self.bodies(s).items():
            code, j = s.post(path, body)
            self.assertEqual(code, 409, (path, j))
            self.assertEqual((j["error"], j.get("busy")), ("a Trash is still running; wait for it to finish, then try again", True), path)
        self.assertEqual(self.files(), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "event-names.json")))
        self.assertEqual(os.listdir(self.share_root), [])  # the export's folder went with the refusal
        fd = os.open(gate, os.O_WRONLY)  # the fake is waiting on it: this returns at once
        os.write(fd, b"go")
        os.close(fd)
        self.assertTrue(self.wait_job(s)["ok"])

    def wait_job(self, s):
        return until(lambda: (lambda j: j if j["done"] else None)(s.get("/api/job")), "the job to end")

    def test_share_safe_export(self):
        s = self.start()
        ids = self.ids(s)
        ida, idb, idb2 = ids[("a.jpg", "2026-11")], ids[("b.jpg", "2026-10")], ids[("b.jpg", "2026-09")]
        before = self.files()
        for bad in ([], None, [999], [ida, "1"], [idb, idb2]):  # idb, idb2: one name, two files
            code, j = s.post("/api/share", {"ids": bad})
            self.assertEqual(code, 400, (bad, j))
        self.assertFalse(os.path.exists(self.share_root))
        code, j = s.post("/api/share", {"ids": [ida, idb]})
        self.assertEqual(code, 200, j)
        self.assertEqual(set(j), {"result", "dest", "job"})  # the folder and the job, nothing of the copies
        self.assertRegex(j["dest"], r"^~/Pictures/share-safe/\d{4}-\d\d-\d\d-\d{6}$")
        out = os.path.join(self.home, j["dest"][2:])
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o700)
        job = self.wait_job(s)
        self.assertTrue(job["ok"], job)
        self.assertEqual(job["ids"], [])  # nothing was held busy: a copy deletes nothing
        self.assertIn("$ intake.py share-copy 'a.jpg' 'b.jpg' --out '" + os.path.basename(out) + "'", job["lines"])
        for n in ("a.jpg", "b.jpg"):
            self.assertIn(f"  {out}/{n}  gps=none", job["lines"])
        self.assertEqual(job["lines"][-1], "done: 0 of 2 gone from the library")
        self.assertEqual(sorted(os.listdir(out)), ["a.jpg", "b.jpg"])  # no sidecar goes with them
        with Image.open(os.path.join(out, "a.jpg")) as im:
            ex = im.getexif()
            self.assertIsNone(ex.get(0x0110))
            self.assertFalse(ex.get_ifd(0x8825))
        self.assertEqual(self.files(), before)  # originals and sidecars only read
        self.assertEqual(s.req("GET", f"/t/{ida}")[0].status, 200)  # still served: never busy
        # a second export gets its own folder; a flagged file is refused like Open refuses it
        code, j2 = s.post("/api/share", {"ids": [idb2]})
        self.assertEqual(code, 200, j2)
        self.assertNotEqual(j2["dest"], j["dest"])
        self.assertTrue(self.wait_job(s)["ok"])
        with open(os.path.join(self.state, "erase-log.txt"), "a") as f:
            f.write(f"2026-11-02 10:00 b0 sha256:{sha(self.b)} size:{os.path.getsize(self.b)}\n")
        code, _ = s.post("/api/share", {"ids": [idb]})
        self.assertEqual(code, 400)

    def test_share_opens_its_own_folder(self):
        """Open folder (POST /api/share/open) names a job, never a path: it runs xdg-open (a fake here, which only
        records its arguments) on the folder this server made for that Share, and refuses anything else."""
        fdir = os.path.join(self.tmp, "fakebin")
        os.makedirs(fdir)
        opener = os.path.join(fdir, "xdg-open")
        with open(opener, "w") as f:
            f.write(FAKE_OPENER)
        os.chmod(opener, 0o700)
        log = os.path.join(self.tmp, "opened.json")
        self.env.update(PATH=fdir + os.pathsep + self.env["PATH"], FAKE_OPEN_LOG=log)
        s = self.start()
        ida = self.ids(s)[("a.jpg", "2026-11")]
        os.makedirs(os.path.join(self.share_root, "planted"))
        for body in ({}, {"job": None}, {"job": 1}, {"job": ["x"]}, {"job": "x"}, {"job": "../../.."},
                     {"job": os.path.join(self.share_root, "planted")}, {"path": os.path.join(self.share_root, "planted")}):
            code, j = s.post("/api/share/open", body)
            self.assertEqual(code, 400, (body, j))  # no Share of this server's: nothing to open
        code, j = s.post("/api/share", {"ids": [ida]})
        self.assertEqual(code, 200, j)
        out = os.path.join(self.home, j["dest"][2:])
        job = self.wait_job(s)
        self.assertTrue(job["ok"], job)
        self.assertEqual((job["dest"], job["id"]), (j["dest"], j["job"]))  # a page that re-attached finds it too
        self.assertFalse(os.path.exists(log))  # the server never opens it by itself
        # only from the page: no CSRF value, or another Origin, never runs it
        r, _ = s.req("POST", "/api/share/open", body={"job": j["job"]})
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/share/open", headers={"X-CSRF": s.csrf(), "Origin": "http://evil.example"},
                     body={"job": j["job"]})
        self.assertEqual(r.status, 403)
        self.assertFalse(os.path.exists(log))
        code, o = s.post("/api/share/open", {"job": j["job"]})
        self.assertEqual(code, 200, o)
        self.assertEqual(o["dest"], j["dest"])

        def opened():
            if not os.path.exists(log):
                return None
            with open(log) as f:
                return json.load(f)
        self.assertEqual(until(opened, "the fake opener to record its arguments"), [out])
        os.remove(log)
        # the folder swapped for a symlink that leads elsewhere, then gone: refused, nothing opened
        shutil.rmtree(out)
        os.symlink(self.lib, out)
        code, o = s.post("/api/share/open", {"job": j["job"]})
        self.assertEqual(code, 400, o)
        os.remove(out)
        code, o = s.post("/api/share/open", {"job": j["job"]})
        self.assertEqual(code, 400, o)
        self.assertFalse(os.path.exists(log))


class ShareOpenGuardTest(unittest.TestCase):
    """App.open_share in-process, with jobs.status, shutil.which and Popen mocked (nothing is launched): only the
    folder this server made for that job, directly inside --share-out after resolving links, once the job ended."""

    def setUp(self):
        import gallery
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-share-open-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "share-out")
        self.app = gallery.App([], share_out=self.root)
        self.d = os.path.join(self.root, "2026-01-02-030405")
        os.makedirs(self.d)
        self.app.shares["j1"] = self.d
        self.status = {"id": "j1", "done": True}
        for target, attr, kw in ((gallery.jobs, "status", dict(side_effect=lambda key: self.status)),
                                 (gallery.shutil, "which", dict(return_value="/usr/bin/xdg-open")),
                                 (gallery.subprocess, "Popen", {})):
            m = mock.patch.object(target, attr, **kw)
            setattr(self, attr, m.start())
            self.addCleanup(m.stop)

    def test_opens_that_folder_only(self):
        r = self.app.open_share("j1")
        self.assertEqual(r["dest"], self.d)
        (args, kw), = self.Popen.call_args_list
        self.assertEqual(args, ([self.g.OPENER, self.d],))  # an argument list: no shell, the folder as one argument
        self.assertNotIn("shell", kw)
        self.assertTrue(kw["start_new_session"])
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_refusals(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        nested = os.path.join(self.root, "a", "b")
        os.makedirs(nested)
        link = os.path.join(self.root, "2026-01-02-030406")
        os.symlink(elsewhere, link)
        self.app.shares.update(out=elsewhere, nested=nested, dots=os.path.join(self.root, "x", "..", "..", "elsewhere"),
                               root=os.path.join(self.root, "a", ".."), link=link,
                               gone=os.path.join(self.root, "2026-01-02-030407"))
        for jid in ("j2", None, 1, ["j1"], self.d, "out", "nested", "dots", "root", "link", "gone"):
            with self.assertRaises(ValueError, msg=jid):
                self.app.open_share(jid)
        self.status = {"id": "j1", "done": False}  # still copying
        with self.assertRaisesRegex(ValueError, "still being made"):
            self.app.open_share("j1")
        self.Popen.assert_not_called()

    def test_no_xdg_open(self):
        self.which.return_value = None
        with self.assertRaisesRegex(RuntimeError, "xdg-open is not installed.*xdg-utils.*doctor"):
            self.app.open_share("j1")
        self.Popen.assert_not_called()


class EventBlockTest(unittest.TestCase):
    """The exiftool blocks events and tags build (no exiftool runs): only Event| goes, leaves are spared."""

    def setUp(self):
        import gallery
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-block-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = gallery.App([])

    def item(self, tags, xevent="", tagslist=()):
        p = os.path.join(self.tmp, f"i{len(os.listdir(self.tmp))}.jpg")
        open(p, "wb").close()
        return dict(path=p, tags=list(tags), tagslist=list(tagslist), xevent=xevent, gone=False)

    def test_rename_moves_only_event_tags(self):
        it = self.item(["Owner|keep", "Event|beach", "Trips|beach", "Event|other"], xevent="beach")
        b = self.app.event_block(it, "coast", {"beach"})
        self.assertIn("-XMP-lr:HierarchicalSubject-=Event|beach", b)
        self.assertIn("-XMP-lr:HierarchicalSubject+=Event|coast", b)
        self.assertIn("-XMP-iptcExt:Event=coast", b)
        self.assertNotIn("-XMP-dc:Subject-=beach", b)  # Trips|beach still uses the leaf
        self.assertFalse([x for x in b if "-=" in x and ("Owner|" in x or "Event|other" in x or "Trips|" in x)])
        self.assertEqual(b[-1], it["path"] + ".xmp")
        self.assertTrue(os.path.exists(b[-1]))  # created from the empty template when adding

    def test_set_and_clear(self):
        it = self.item(["Event|a", "Event|b", "Owner|not-trash"], xevent="a")
        b = self.app.event_block(it, "c", None)  # set: out of every other event
        for t in ("-XMP-lr:HierarchicalSubject-=Event|a", "-XMP-lr:HierarchicalSubject-=Event|b",
                  "-XMP-dc:Subject-=a", "-XMP-dc:Subject-=b", "-XMP-lr:HierarchicalSubject+=Event|c",
                  "-XMP-iptcExt:Event=c"):
            self.assertIn(t, b)
        b = self.app.event_block(it, "", None)  # clear
        self.assertIn("-XMP-iptcExt:Event=", b)
        self.assertFalse([x for x in b if "+=" in x])
        self.assertIsNone(self.app.event_block(self.item(["Event|c"], xevent="c"), "c", None))  # already there
        self.assertIsNone(self.app.event_block(self.item(["Owner|keep"]), "", None))  # in no event

    def test_rename_an_iptc_only_event(self):
        """No Event| tag, only XMP-iptcExt:Event: the rename writes the new name there and adds Event|<new>."""
        it = self.item(["Owner|keep"], xevent="dunes")
        self.assertEqual(self.app.events_of(it), ["dunes"])
        b = self.app.event_block(it, "sands", {"dunes"})
        self.assertIn("-XMP-iptcExt:Event=sands", b)
        self.assertIn("-XMP-lr:HierarchicalSubject+=Event|sands", b)
        self.assertFalse([x for x in b if "-=" in x and not x.endswith("=Event|sands") and not x.endswith("=sands")])

    def test_events_of_and_filter(self):
        two = self.item(["Event|a", "Event|b"])
        x = self.item([], xevent="iptc-only")
        self.assertEqual(self.app.events_of(two), ["a", "b"])
        self.assertEqual(self.app.events_of(x), ["iptc-only"])
        for it in (two, x):
            it.update(media="photo", date="2026-01-01 00:00", project="", source="", place="", labels=[], mine=[],
                      used=[], ai_trash=None, ai_keep=False)
        self.assertTrue(self.app.matches(two, {"event": "b"}))  # an item in two events shows in both
        self.assertFalse(self.app.matches(two, {"event": "iptc-only"}))
        self.assertTrue(self.app.matches(x, {"event": "iptc-only"}))

    def test_share_folder_never_in_the_library(self):
        inside = [self.g.LIBRARY, os.path.join(self.g.LIBRARY, "share"), os.path.join(self.g.STATE, "x")]
        inside += [os.path.join(r, "share") for r in self.g.ROOTS]  # videos and every project folder too
        os.makedirs(self.g.LIBRARY, exist_ok=True)
        link = os.path.join(self.tmp, "looks-elsewhere")
        os.symlink(self.g.LIBRARY, link)
        inside.append(os.path.join(link, "share"))  # a symlink that leads into the library
        for d in inside:
            with self.assertRaises(ValueError, msg=d):
                self.g.App([], share_out=d).share_dir()
        self.assertFalse(os.path.exists(os.path.join(self.g.LIBRARY, "share")), "nothing created on refusal")


class EventLabelsTest(unittest.TestCase):
    """Display names in process: never two events that look alike, and the file kept per library."""

    def setUp(self):
        import gallery
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-evnames-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_defaults_only_when_unique(self):
        L = self.g.event_labels
        self.assertEqual(L(["some-trip-2024", "beach"], {}), {"some-trip-2024": "Some trip 2024", "beach": "Beach"})
        # a-b and "a b" (another tool's text) would look the same prettified: a-b keeps its slug
        self.assertEqual(L(["a-b", "a b"], {}), {"a-b": "a-b", "a b": "a b"})
        self.assertEqual(L(["trip", "Trip"], {}), {"trip": "trip", "Trip": "Trip"})
        self.assertEqual(L(["a--b", "x-", "Mixed-Case"], {}), {"a--b": "a--b", "x-": "x-", "Mixed-Case": "Mixed-Case"})
        # a set name is shown unless it looks like another event's slug or set name; then the defaults
        self.assertEqual(L(["beach", "coast"], {"coast": "Sea"}), {"beach": "Beach", "coast": "Sea"})
        self.assertEqual(L(["beach", "coast"], {"coast": "BEACH "}), {"beach": "Beach", "coast": "Coast"})
        self.assertEqual(L(["beach", "coast"], {"beach": "Sea", "coast": "sea"}), {"beach": "Beach", "coast": "Coast"})
        self.assertEqual(L(["beach"], {"gone-event": "Beach"}), {"beach": "Beach"})  # a name of no current event
        self.assertEqual(L(["beach", "x"], {"beach": "beach"}), {"beach": "beach", "x": "X"})  # its slug, by choice
        for events, names in ((["a-b", "a b", "A_B", "trip"], {"trip": "A  b"}),
                              ([f"e-{i}" for i in range(30)] + ["E 3", "e 4"], {"e-5": "E 6", "e-7": "Seven"})):
            shown = L(events, names)
            self.assertEqual(sorted(shown), sorted(events))
            self.assertEqual(len(set(shown.values())), len(events), shown)
            k = self.g.look_key
            for e, v in shown.items():  # a label other than its own slug looks like no other event's slug or label
                if v != e:
                    self.assertFalse([f for f in events if f != e and k(v) in (k(f), k(shown[f]))], (e, v))

    def test_check_display_name(self):
        c = self.g.check_display_name
        self.assertEqual(c("  Summer   trip "), "Summer trip")
        self.assertEqual(c(""), "")
        for bad in ("x" * 65, "a\tb\x00", "be\u200bach", "\u202eevil", 3, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                c(bad)

    def test_file_is_per_library_private_and_atomic(self):
        st = os.path.join(self.tmp, "state")
        one, two = [os.path.join(self.tmp, "lib1")], [os.path.join(self.tmp, "lib2"), os.path.join(self.tmp, "v")]
        self.assertEqual(self.g.load_event_names(st, one), {})  # no file yet
        self.g.save_event_names({"beach": "Sea"}, st, one)
        self.g.save_event_names({"beach": "Lake"}, st, two)
        self.assertEqual(self.g.load_event_names(st, one), {"beach": "Sea"})
        self.assertEqual(self.g.load_event_names(st, two), {"beach": "Lake"})
        p = os.path.join(st, self.g.EVENT_NAMES)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(st).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(st)), [self.g.EVENT_NAMES])  # no temp file left behind
        with open(p) as f:
            self.assertNotIn(self.tmp, f.read())  # libraries by hash, never a path
        with open(p) as f:  # a name edited by hand into one the page would refuse is left out
            d = json.load(f)
        d["libraries"][self.g.library_key(one)].update(bad="a\nb", sp="  x  ", zw="a\u200bb", ok="Fine")
        with open(p, "w") as f:
            json.dump(d, f)
        self.assertEqual(self.g.load_event_names(st, one), {"beach": "Sea", "ok": "Fine"})
        self.g.save_event_names({}, st, one)  # the last name gone: that library's entry goes
        self.assertEqual(self.g.load_event_names(st, one), {})
        self.assertEqual(self.g.load_event_names(st, two), {"beach": "Lake"})


class SemanticArgvTest(unittest.TestCase):
    """The gallery asks semantic.py for --format machine explicitly (the `score  path` lines it parses), never
    relying on the auto default that turns human at a terminal."""

    def test_search_and_similar_pass_format_machine(self):
        import gallery
        tmp = tempfile.mkdtemp(prefix="gallery-argv-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "a.jpg")
        img(p, (10, 20, 30))
        app = gallery.App([dict(path=p, gone=False, tags=[], place="", event="")])
        app.text_why = None
        app.text_checked.set()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")  # no hits: only the argv matters
        real_exists = os.path.exists
        exists = lambda q: q == gallery.intake.VENV_PY or q.endswith("embeddings.npz") or real_exists(q)  # noqa: E731
        with mock.patch.object(gallery.subprocess, "run", run), mock.patch.object(gallery.os.path, "exists", exists):
            for f in ({"q": "beach"}, {"similar": "0"}):
                self.assertEqual(app.listing(f)["total"], 0)
        self.assertEqual([c[3] for c in calls], ["search", "similar"])
        for argv in calls:
            self.assertEqual(argv[-2:], ["--format", "machine"], argv)


class VenvReexecTest(unittest.TestCase):
    """The system python has no pillow-heif, so HEIC would show only as its thumbnail: the gallery re-runs itself
    under setup.sh's venv (review_ui.venv_reexec, as the review page does), with every argument, before it prints
    anything. Not when already in the venv, when MEDIA_REVIEW_NO_VENV=1, or when there is no venv. os.execv is
    mocked: nothing is exec'd."""

    class Stop(Exception):
        pass

    def setUp(self):
        import gallery
        self.g = gallery
        self.tmp = tempfile.mkdtemp(prefix="gallery-venv-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.venv_py = os.path.join(self.tmp, "venv", "bin", "python")
        os.makedirs(os.path.dirname(self.venv_py))
        with open(self.venv_py, "w") as f:
            f.write("#!/bin/false\n")
        os.chmod(self.venv_py, 0o700)
        self.drive, self.share = os.path.join(self.tmp, "drive"), os.path.join(self.tmp, "share")
        os.makedirs(self.drive)
        self.args = ["--drive", self.drive, "--port", "0", "--share-out", self.share]
        for target, attr, value in ((gallery.R, "HEIF", False), (gallery.R, "VENV_PY", self.venv_py)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEDIA_REVIEW_NO_VENV", None)

    def run_main(self, argv):
        """gallery.main up to its exec, or up to jobs.sweep (the first step after it) when it doesn't exec."""
        out = io.StringIO()
        seen = []

        def execv(path, args):
            seen.append(out.getvalue())  # what had been printed by the time of the exec
            raise self.Stop

        with mock.patch.object(self.g.R.os, "execv", side_effect=execv) as ex, \
                mock.patch.object(self.g.jobs, "sweep", side_effect=self.Stop) as sweep, \
                mock.patch("sys.stdout", out), self.assertRaises(self.Stop):
            self.g.main(argv)
        return ex, sweep, seen

    def test_reexecs_under_the_venv_with_every_argument(self):
        ex, sweep, seen = self.run_main(list(self.args))
        ex.assert_called_once_with(self.venv_py, [self.venv_py, "-I", GALLERY, *self.args])
        sweep.assert_not_called()
        self.assertEqual(seen, [""], "nothing printed before the exec: the xdg-open line comes once, from the venv")

    def test_argv_none_takes_the_command_line(self):
        with mock.patch.object(sys, "argv", [GALLERY, *self.args]):
            ex, _, _ = self.run_main(None)
        ex.assert_called_once_with(self.venv_py, [self.venv_py, "-I", GALLERY, *self.args])

    def test_not_when_opted_out_in_the_venv_or_without_one(self):
        venv_root = os.path.dirname(os.path.dirname(self.venv_py))
        cases = (("MEDIA_REVIEW_NO_VENV=1", mock.patch.dict(os.environ, MEDIA_REVIEW_NO_VENV="1")),
                 ("already the venv", mock.patch.object(sys, "prefix", venv_root)),
                 ("no venv", mock.patch.object(self.g.R, "VENV_PY", os.path.join(self.tmp, "none", "python"))),
                 ("pillow-heif here", mock.patch.object(self.g.R, "HEIF", True)))
        for why, patch in cases:
            with self.subTest(why), patch:
                ex, sweep, _ = self.run_main(list(self.args))
                ex.assert_not_called()
                sweep.assert_called_once()  # it went on to serve under this python

    def test_review_page_still_reexecs_itself(self):
        with mock.patch.object(self.g.R.os, "execv") as ex:
            self.g.R.venv_reexec(["b1", "--port", "0"])
        ex.assert_called_once_with(self.venv_py, [self.venv_py, "-I", os.path.join(HERE, "review_ui.py"),
                                                  "b1", "--port", "0"])


class HeicWithoutPillowHeifTest(unittest.TestCase):
    """Without pillow-heif (and no venv to re-run under), a HEIC's large view can only show its thumbnail: the
    item says so (heic), /p/ is a 404, and the page's words are honest about the small preview."""

    def setUp(self):
        import review_ui
        if review_ui.HEIF:
            self.skipTest("this python has pillow-heif: HEIC shows full size")
        self.tmp = tempfile.mkdtemp(prefix="gallery-heic-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        state = os.path.join(home, ".local/share/media-intake")
        self.lib = os.path.join(home, "Pictures/library")
        os.makedirs(state)
        self.env = dict(os.environ, HOME=home, MEDIA_INTAKE_STATE=state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(home, "Videos/demo-footage")))
        self.env.pop("MEDIA_REVIEW_NO_VENV", None)  # no venv in this HOME: the gallery runs under this python
        os.makedirs(os.path.join(self.lib, "2026/2026-11"))
        with open(os.path.join(self.lib, "2026/2026-11/h.heic"), "wb") as f:
            f.write(b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 64)  # not decodable here
        img(os.path.join(self.lib, "2026/2026-11/j.jpg"), (10, 20, 30))

    def test_item_says_heic_and_the_full_size_is_refused(self):
        s = Server(self.env)
        self.addCleanup(s.stop)
        ids = {i["n"]: i["id"] for i in s.get("/api/list?limit=50")["items"]}
        h, j = s.get(f"/api/item/{ids['h.heic']}"), s.get(f"/api/item/{ids['j.jpg']}")
        self.assertTrue(h["heic"])
        self.assertEqual(h["src"], f"/p/{ids['h.heic']}")
        self.assertFalse(j["heic"])
        r, _ = s.req("GET", h["src"])
        self.assertEqual(r.status, 404)
        _, page = s.req("GET", "/")
        self.assertIn(b"This is a small preview: HEIC photos need setup.sh's venv (pillow-heif) to show full size.",
                      page)
        self.assertNotIn(b'cantPlay(d,"This browser can\'t show this picture.")', page)


class AddedFilterTest(unittest.TestCase):
    """The "added in the last N days" filter on items whose inode changed long ago (no test can set a ctime)."""

    def test_added_days(self):
        import gallery
        app = gallery.App([])
        old = dict(gone=False, tags=[], media="photo", date="2026-01-01 00:00", ctime=time.time() - 40 * 86400)
        new = dict(old, ctime=time.time() - 86400)
        for it, f, want in ((old, {"added": "30"}, False), (new, {"added": "30"}, True), (old, {"added": "41"}, True),
                            (old, {"added": "0"}, True), (old, {"added": "x"}, True), (old, {}, True)):
            self.assertEqual(app.matches(it, f), want, (it["ctime"], f))


class NotBestShotTest(unittest.TestCase):
    """best-shot's AI|bestshot/not-best reaches Suggested junk as "Not the best shot" (at any Junk score, after every
    scored verdict, owner guards first) and AI|bestshot/cover leads its burst stack. Items are built from tags
    with gallery.facts, as load_sidecar does: no sidecar, no exiftool."""

    def it(self, name, *tags, size=1, **kw):
        import gallery
        it = dict(path=f"/lib/2026/2026-11/{name}", root="/lib", tags=list(tags), pick="", rating="", gone=False,
                  media="photo", date="2026-11-01 10:00", size=size, burst=None, burst_pos=None)
        it.update(kw)
        gallery.facts(it)
        return it

    def test_constants_match_best_shot(self):
        import best_shot, gallery, intake
        self.assertEqual((gallery.BEST_COVER, gallery.NOT_BEST), (best_shot.COVER, best_shot.NOT_BEST))
        self.assertEqual(intake.BESTSHOT_NOT_BEST, best_shot.NOT_BEST)
        self.assertEqual(gallery.WORDS["why"][gallery.WHY_BEST], "Not the best shot")

    def test_offered_at_any_floor_with_its_reason(self):
        import gallery
        nb = self.it("nb.jpg", "AI|bestshot/not-best")
        cover = self.it("cover.jpg", "AI|bestshot/cover")
        app = gallery.App([nb, cover])
        for floor in (gallery.SUGGEST_MIN, 0.0, 1.0):
            self.assertTrue(app.suggested(nb, floor), floor)
            self.assertEqual(app.junk_why(nb, floor), {gallery.WHY_BEST}, floor)
            self.assertFalse(app.suggested(cover, floor), "the cover is no junk")
        self.assertTrue(app.matches(nb, dict(view="suggest", why=gallery.WHY_BEST)))
        self.assertFalse(app.matches(nb, dict(view="suggest", why="blurry")))
        # the Why filter's counts list the reason
        self.assertIn([gallery.WHY_BEST, 1], [list(x) for x in app.junk_summary([0, 1], dict(view="suggest"))["why"]])

    def test_owner_guards_win(self):
        import gallery
        nb = "AI|bestshot/not-best"
        prot = sorted(gallery.PROTECTED)[0] if gallery.PROTECTED else None
        guarded = [self.it("kept.jpg", nb, "Owner|keep"), self.it("fav.jpg", nb, rating="5"),
                   self.it("used.jpg", nb, "Used|2026-11-01-post"), self.it("named.jpg", nb, "People|Ann")]
        if prot:
            guarded.append(self.it("prot.jpg", nb, prot))
        app = gallery.App(guarded)
        for it in guarded:
            for floor in (gallery.SUGGEST_MIN, 0.0):
                self.assertFalse(app.suggested(it, floor), (it["path"], floor))

    def test_sorted_after_scored_verdicts(self):
        import gallery
        items = [self.it("nb.jpg", "AI|bestshot/not-best"),
                 self.it("low.jpg", "AI|vlm/m/trash/dark", "AI|vlm/m/conf/0.1"),
                 self.it("hi.jpg", "AI|vlm/m/trash/blurry", "AI|vlm/m/conf/0.9"),
                 self.it("label.jpg", "Review|maybe-trash"),
                 self.it("both.jpg", "AI|bestshot/not-best", "AI|vlm/m/trash/dark", "AI|vlm/m/conf/0.8")]
        app = gallery.App(items)
        order = sorted(items, key=app.junk_rank)
        self.assertEqual([os.path.basename(i["path"]) for i in order],
                         ["hi.jpg", "both.jpg", "label.jpg", "low.jpg", "nb.jpg"])
        both = items[4]
        self.assertEqual(app.junk_why(both, gallery.SUGGEST_MIN), {"dark", gallery.WHY_BEST})

    def test_cover_leads_its_burst_stack(self):
        import gallery
        items = [self.it(f"s{k}.jpg", *(["AI|bestshot/cover"] if k == 2 else ["AI|bestshot/not-best"]),
                         burst=0, burst_pos=k) for k in range(3)]
        app = gallery.App(items)
        ids, info = app.stacked([0, 1, 2])
        self.assertEqual(ids, [2, 0, 1])
        self.assertTrue(info[2]["bc"])
        self.assertFalse(info[0]["bc"])
        # no best-shot tags: the earliest shot is the cover, as before
        plain = [self.it(f"p{k}.jpg", burst=0, burst_pos=k) for k in range(3)]
        self.assertEqual(gallery.App(plain).stacked([2, 1, 0])[0], [0, 1, 2])

    def test_space_lists_them(self):
        import gallery, space
        nb, plain = self.it("nb.jpg", "AI|bestshot/not-best", size=9), self.it("plain.jpg", size=5)
        got = space.junk([nb, plain])
        self.assertEqual([(os.path.basename(i["path"]), w) for i, w in got], [("nb.jpg", ["Not the best shot"])])


class PageAfterTrashTest(unittest.TestCase):
    """The page's own code (gallery.PAGE, no server): a Trash that went well drops its tiles in place (no reload)
    and ends in a message with Details, not the Done dialog; tile labels are 12 px, short and never cut with an
    ellipsis; a video the browser can't play is called HEVC only when /api/item says its codec is HEVC."""

    def setUp(self):
        import gallery
        self.page = gallery.PAGE

    def rule(self, sel):
        r = self.page[self.page.index("\n" + sel) + 1:]
        return r[:r.index("}")]

    def test_trash_keeps_your_place(self):
        p = self.page
        self.assertIn("if(trashed)dropItems(job.gone||job.ids);else{closeViewer();reset()}", p)
        self.assertIn("function dropItems(ids)", p)
        self.assertIn("job.gone=keep.concat(trash)", p)  # Suggested junk's Apply drops every decided tile
        self.assertIn("if(k>=0&&k<items.length)openViewer(k);else closeViewer()", p)  # the large view moves on

    def test_after_trash_a_message_with_details(self):
        p = self.page
        self.assertIn("if(trashed&&jobInDialog())closeModal();", p)
        self.assertIn("else if(trashed)detailsToast(job.status)", p)
        self.assertIn('el("button",null,"Details");b.type="button";b.id="tdetails"', p)

    def test_tile_labels_are_12px_short_and_whole(self):
        for sel in (".tile .badge{", ".tile .x{", ".flag{"):
            r = self.rule(sel)
            self.assertIn("font-size:12px", r, sel)
            self.assertNotIn("ellipsis", r, sel)
        for sel in (".tile .x{", ".flag{"):
            self.assertNotIn("nowrap", self.rule(sel), sel)  # a long one wraps
        self.assertNotIn("text-overflow:ellipsis}\n/* a small tile", self.page)
        # a phone tile: the flag sits above the star and Share, at most two lines so it stays inside the tile,
        # with its whole text as the title (CI's browser test checks the boxes)
        phone = self.page[self.page.index("\n .tile:has(.star) .flag{") + 1:]
        phone = phone[:phone.index("}")]
        for want in ("bottom:50px", "-webkit-line-clamp:2", "overflow:hidden"):
            self.assertIn(want, phone)
        self.assertEqual(self.page.count('const f=el("span","flag",fl);f.title=fl;t.append(f)'), 2)
        self.assertIn('el("span","vh",it.bc?"Burst: ":"Burst: shot "),it.bc?`${it.bn} shots`:`${pos} of ${it.bn}`',
                      self.page)

    def test_an_unplayable_video_is_not_guessed_hevc(self):
        p = self.page
        self.assertNotIn("HEVC, most likely", p)
        self.assertIn('d.codec==="hevc"?"This browser can\'t play HEVC video', p)
        self.assertIn(':"This browser can\'t play this video."}', p)
        self.assertIn(":noPlay(d))};", p)


class PlainWordsTest(unittest.TestCase):
    """busy_text (a 409's words on the page) and WORDS (the one map of internal names): what is running in plain
    words, never a PID, a lock-file path or a tool namespace."""

    def busy(self, status, holder):
        import gallery
        with mock.patch.object(gallery.jobs, "status", lambda key: status):
            return gallery.busy_text(holder)

    def test_busy_text_names_what_runs(self):
        tail = " is still running; wait for it to finish, then try again"
        for label, what in (("trash", "a Trash"), ("erase", "an Erase"), ("share", "a Share")):
            st = dict(done=False, spec=dict(label=label), label="")
            self.assertEqual(self.busy(st, {"what": "job gallery", "pid": 510}), what + tail)
        done = dict(done=True, spec=dict(label="trash"))
        for holder, what in (({"what": "job review-phone-1", "pid": 7}, "a run from the review page"),
                             ({"what": "job maybe-trash"}, "a run from the review page"),
                             ({"what": "job gallery"}, "a Trash, Erase or Share from another gallery"),
                             ({"what": "gallery keep", "pid": 9}, "a change from another gallery page"),
                             ({"what": "intake.py apply", "pid": 3}, "the media-intake apply command"),
                             ({"what": "intake.py /tmp/x (PID"}, "another media-intake command"),
                             ({}, "another media-intake command")):
            got = self.busy(done if holder.get("what") else None, holder)
            self.assertEqual(got, what + tail, holder)
            self.assertNotRegex(got, r"PID|/|lock")

    def test_words_name_no_tool_namespace(self):
        import gallery
        shown = json.dumps({k: v for k, v in gallery.WORDS.items() if k != "tag"})
        shown += json.dumps(list(gallery.WORDS["tag"].values()))
        self.assertNotRegex(shown, r"(Owner|Review|Label|AI|Used|Project|Source|Event|Place)\|")
        for f in ("maybe-trash", "child-in-shot", "kept", "trashed copy", "used", "favorite"):  # summary(), Flags
            self.assertIn(f, gallery.WORDS["flag"])
        for k in ("trash", "erase", "share", "forget", "share-copy"):  # a job by its spec and by its step
            self.assertIn(k, gallery.WORDS["job"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
