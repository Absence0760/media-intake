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
on a selection) and the share-safe export are tested through the HTTP API; ViewsEndpointsTest covers their
guards and the 409 while the library is locked with hand-written sidecars, so it needs no exiftool.
"""
import errno, hashlib, http.client, json, os, re, shutil, signal, stat, subprocess, sys, tempfile, time
import unittest, urllib.parse

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
        self.p = subprocess.Popen([PY, "-I", INTAKE, "gallery", *extra], env=env, stdout=subprocess.PIPE,
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
        self.assertIn("semantic index", j["note"])
        # Trash suggestions: maybe-trash or AI >= 0.7, never kept / used / Demo; by confidence
        names, j = self.names(s, view="suggest")
        self.assertEqual(names, ["a.jpg"])
        f = s.get("/api/facets")["facets"]
        self.assertIn(["food", 1], f["label"])

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
        t = os.path.join(self.thumbs, self.originals[self.a] + ".jpg")
        r, _ = s.req("GET", f"/t/{ida}")
        self.assertEqual(r.status, 200)
        with Image.open(t) as im:
            self.assertEqual(im.size, (320, 240))
        self.assertEqual(oct(os.stat(t).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.thumbs).st_mode & 0o777), "0o700")
        code, j = s.post("/api/rotate", {"ids": [ida], "dir": "cw"})
        self.assertEqual(code, 200, j)
        self.assertEqual(tags_of(self.a)[2], 6)
        self.assertFalse(os.path.exists(t))  # remade on the next request, rotated
        s.req("GET", f"/t/{ida}")
        with Image.open(t) as im:
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

    # ------------------------------------------------------------ trash / erase and the thumbnail cache
    def test_trash_and_erase(self):
        s = self.start()
        ida, idb = self.id_of(s, "a.jpg"), self.id_of(s, "b.jpg")
        for i in (ida, idb):
            self.assertEqual(s.req("GET", f"/t/{i}")[0].status, 200)
        ta = os.path.join(self.thumbs, self.originals[self.a] + ".jpg")
        tb = os.path.join(self.thumbs, self.originals[self.b] + ".jpg")
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
        # verify-gone checks the gallery cache too
        with open(tb, "wb") as f:
            f.write(b"\xff\xd8 planted")
        r = subprocess.run([PY, "-I", INTAKE, "verify-gone"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("gallery thumbnails", r.stdout)

    def test_intake_cli_drops_gallery_thumbs(self):
        """trash and forget (and so erase) delete ~/.cache/media-intake/thumbs/<sha>.jpg without the gallery."""
        os.makedirs(self.thumbs)
        ta = os.path.join(self.thumbs, self.originals[self.a] + ".jpg")
        tk = os.path.join(self.thumbs, self.originals[self.k] + ".jpg")
        other = os.path.join(self.thumbs, "f" * 64 + ".jpg")
        for p in (ta, tk, other):
            open(p, "wb").close()
        r = subprocess.run([PY, "-I", INTAKE, "trash", self.a], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(ta))
        r = subprocess.run([PY, "-I", INTAKE, "forget", self.k, "--yes"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(tk))
        self.assertTrue(os.path.exists(other))  # only those shas

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
        self.assertEqual(j["lines"][0], "$ intake.py trash 'a.jpg'")
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
        self.assertIn("the library is busy: job gallery", j["error"])
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

    # ------------------------------------------------------------ page layout and the sidebar's counts
    def test_page_layout_and_facet_counts(self):
        """Left sidebar (views, search, filters), bottom action bar, lightbox info pane; nothing external."""
        s = self.start()
        r, b = s.req("GET", "/")
        self.assertEqual(r.status, 200)
        for needle in (b'<aside id="sidebar"', b'<nav id="views"', b'id="filters"', b'id="q"', b'id="chips"',
                       b'id="tsize"', b'id="actionbar" hidden', b'aria-label="Selection actions"', b'<aside id="info"',
                       b'data-v="recent"', b'data-v="child"', b'data-v="untagged"', b'data-dialog="confirm"'):
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
                       ("review:untagged", dict(review="untagged")),
                       ("ai:trash", dict(ai="trash", aimin="0")), ("ai:none", dict(ai="none")),
                       ("used:used", dict(used="used")), ("suggest", dict(view="suggest"))):
            self.assertEqual(c.get(k, 0), len(self.names(s, **flt)[0]), k)  # each count is what its filter lists
        self.assertEqual(c["used:used"] + c["used:unused"], f["total"])
        # Recently added: every file in this fresh HOME was filed (its inode changed) just now
        self.assertEqual(f["recent_days"], 30)
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

    def start(self):
        s = Server(self.env)
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
                "/api/share": {"ids": [ida]}}

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
        self.assertEqual(s.get("/api/job")["id"], None)  # no job started

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
            self.assertRegex(j["error"], r"the library is busy: job gallery|still running", path)
        self.assertEqual(self.files(), before)
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
        self.assertEqual(set(j), {"result", "dest"})  # the destination folder, nothing of the copies
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
        for inside in (self.g.LIBRARY, os.path.join(self.g.LIBRARY, "share"), os.path.join(self.g.STATE, "x")):
            with self.assertRaises(ValueError):
                self.g.App([], share_out=inside).share_dir()


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
