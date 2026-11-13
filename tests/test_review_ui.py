#!/usr/bin/env python3
"""Tests for review_ui.py on synthetic media in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_review_ui.py

Images are drawn with Pillow and the video is an ffmpeg test pattern: no real
photo is read. intake.py is replaced (MEDIA_REVIEW_INTAKE) by a fake that
records its argv and mimics release/erase/purge-review on the fake state, so
the real erase never runs. Nothing points at the real ~/.local/share/media-intake,
~/Pictures, ~/Videos or /run/user/*/gvfs.
"""
import http.client, json, os, re, shutil, stat, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
UI = os.path.join(HERE, "review_ui.py")
PY = sys.executable
BATCH = "phone-2026-11-13"

from PIL import Image  # noqa: E402

FAKE = r'''
import json, os, shutil, sys
state = os.environ["MEDIA_INTAKE_STATE"]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
cmd, batch = sys.argv[1], sys.argv[2]
quar = os.path.join(state, "quarantine", batch)
man = os.path.join(quar, "quarantine.json")
if cmd == "release":
    q = json.load(open(man))
    for p in sys.argv[3:]:
        rel = os.path.relpath(p, quar)
        if q.get(rel, {}).get("dst"):
            os.remove(p)
            del q[rel]
            print("released -> library")
    json.dump(q, open(man, "w"))
elif cmd == "erase":
    print(f"erased {len(json.load(open(man)))} files; library copies 0; phone 0; drive 0")
    shutil.rmtree(quar)
elif cmd == "verify-gone":
    print("verify-gone: no copies remain")
elif cmd == "trash":
    args = sys.argv[2:]
    if "--phone" in args:
        i = args.index("--phone"); del args[i:i + 2]
    if "--batch" in args:
        i = args.index("--batch"); del args[i:i + 2]
    for p in args:
        os.remove(p)
    print(f"trashed {len(args)} files")
elif cmd == "not-trash":
    print(f"kept {len(sys.argv[2:])} files: Owner|not-trash")
elif cmd == "purge-review":
    shutil.rmtree(os.path.join(state, "batches", batch, "review"))
    print("deleted 2 reviewed near-duplicates/junk")
'''


def write_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(obj, f)


def img(p, color, size=(64, 48)):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    Image.new("RGB", size, color).save(p)


class Server:
    """review_ui.py in a subprocess; parses its xdg-open line."""

    def __init__(self, env, *extra, batch=BATCH):
        self.p = subprocess.Popen([PY, "-I", UI, *([batch] if batch else []), *extra], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        line = self.p.stdout.readline()
        m = re.fullmatch(r"xdg-open 'http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)'\n", line)
        if not m:
            self.p.kill()
            raise AssertionError(f"first line not an xdg-open URL: {line!r} {self.p.stdout.read()}")
        self.port, self.token = int(m.group(1)), m.group(2)
        self.note = self.p.stdout.readline()

    def req(self, method, path, host=None, headers=None, body=None, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Token"] = self.token
        h.update(headers or {})
        c.putrequest(method, path, skip_host=True)
        for k, v in h.items():
            c.putheader(k, v)
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r, out

    def csrf(self):
        r, b = self.req("GET", "/")
        return re.search(rb'<meta name="csrf" content="([^"]+)">', b).group(1).decode()

    def stop(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        self.p.stdout.close()


class ReviewUITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-ui-test-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.phone = os.path.join(self.tmp, "phone")
        self.drive = os.path.join(self.tmp, "drive")
        for d in (self.home, self.phone, self.drive):
            os.makedirs(d)
        self.fake = os.path.join(self.tmp, "fake_intake.py")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        self.log = os.path.join(self.tmp, "argv.log")
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state,
                        MEDIA_REVIEW_INTAKE=self.fake, FAKE_LOG=self.log)
        for k in ("PHOTO_LIBRARY", "PERSONAL_FOOTAGE", "MEDIA_INTAKE_CONFIG"):
            self.env[k] = os.path.join(self.home, k.lower())
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()
        shutil.rmtree(self.tmp)

    # ------------------------------------------------------------ fixtures
    def quarantine(self):
        quar = os.path.join(self.state, "quarantine", BATCH)
        img(os.path.join(quar, "DCIM/Camera/a.jpg"), (200, 30, 30))
        img(os.path.join(quar, "DCIM/Camera/b.png"), (30, 200, 30))
        vid = os.path.join(quar, "DCIM/Camera/c.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                        "-pix_fmt", "yuv420p", vid], check=True)
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        write_json(os.path.join(quar, "quarantine.json"), {
            "DCIM/Camera/a.jpg": {"reasons": ["nudity:exposed"], "sha": "0" * 64, "size": 1, "dst": f"{lib}/a.jpg"},
            "DCIM/Camera/b.png": {"reasons": ["document:id-card"], "sha": "1" * 64, "size": 1, "dst": f"{lib}/b.png"},
            "DCIM/Camera/c.mp4": {"reasons": ["nudity:exposed"], "sha": "2" * 64, "size": 1, "dst": f"{lib}/c.mp4"},
        })
        near = os.path.join(self.state, "quarantine", f"near-{BATCH}")
        img(os.path.join(near, "Downloads/n.jpg"), (90, 90, 200))
        write_json(os.path.join(near, "quarantine.json"), {
            "Downloads/n.jpg": {"reasons": ["near-duplicate"], "sha": "3" * 64, "size": 1,
                                "dst": os.path.join(self.home, "Downloads/n.jpg"),
                                "near_of_sha": "0" * 64, "erased_on": "2026-11-12"}})
        return quar, vid

    def review(self):
        b = os.path.join(self.state, "batches", BATCH)
        img(os.path.join(b, "review/DCIM/Camera/r1.jpg"), (10, 10, 10), (20, 20))
        img(os.path.join(b, "review/Pictures/Screenshots/r2.png"), (250, 250, 250))
        write_json(os.path.join(b, "plan.json"), [
            {"rel": "DCIM/Camera/r1.jpg", "action": "review", "why": "junk: tiny or empty"},
            {"rel": "Pictures/Screenshots/r2.png", "action": "review", "why": "near-duplicate of DCIM/x.png"}])
        return os.path.join(b, "review")

    def start(self, *extra, batch=BATCH):
        s = Server(self.env, *extra, batch=batch)
        self.servers.append(s)
        return s

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def items(self, s):
        r, b = s.req("GET", "/api/items")
        self.assertEqual(r.status, 200)
        return json.loads(b)["items"]

    def wait_done(self, s):
        since, out = 0, []
        for _ in range(100):
            r, b = s.req("GET", f"/api/progress?since={since}")
            j = json.loads(b)
            out += j["lines"]
            since = j["next"]
            if j["done"]:
                return j, out
            time.sleep(0.1)
        self.fail("run never finished")

    def originals(self):
        """Review items that are near-duplicates: of a filed batch item, of a library video, and of
        originals that must never be served (outside the roots, another batch's quarantine, a symlink out)."""
        b = os.path.join(self.state, "batches", BATCH)
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        img(os.path.join(lib, "x.png"), (30, 30, 200), (128, 96))
        img(os.path.join(b, "review/DCIM/x-small.png"), (30, 30, 200), (64, 48))
        vids = os.path.join(self.home, "personal_footage/2026/2026-11")
        os.makedirs(vids)
        for p, size in ((os.path.join(vids, "v.mp4"), "320x240"), (os.path.join(b, "review/DCIM/v2.mp4"), "160x120")):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"testsrc=size={size}:rate=10:duration=1", "-pix_fmt", "yuv420p", p], check=True)
        outside = os.path.join(self.tmp, "outside.jpg")
        img(outside, (1, 2, 3))
        other_q = os.path.join(self.state, "quarantine", "other-batch", "secret.jpg")
        img(other_q, (4, 5, 6))
        link = os.path.join(lib, "link.jpg")
        os.symlink(outside, link)
        bad = {"o1.png": outside, "o2.png": other_q, "o3.png": link,
               "o4.png": os.path.join(lib, "../../../.ssh/id.jpg")}
        for name in bad:
            img(os.path.join(b, "review/DCIM", name), (7, 8, 9))
        write_json(os.path.join(b, "plan.json"), [
            {"rel": "DCIM/x.png", "action": "file", "why": "", "dst": os.path.join(lib, "x.png"),
             "lib_dst": os.path.join(lib, "x.png"), "date": "2026-11-01T10:00:00"},
            {"rel": "DCIM/x-small.png", "action": "review", "why": "near-duplicate of DCIM/x.png",
             "date": "2026-11-02T11:00:00"},
            {"rel": "DCIM/v2.mp4", "action": "review", "why": f"near-duplicate of library {vids}/v.mp4"},
        ] + [{"rel": f"DCIM/{n}", "action": "review", "why": f"near-duplicate of library {p}"} for n, p in bad.items()])
        return lib, vids, outside, other_q

    # ------------------------------------------------------------ tests
    def test_original_beside_near_duplicate(self):
        self.quarantine()
        lib, vids, outside, other_q = self.originals()
        s = self.start()
        items = {i["rel"]: i for i in self.items(s)}
        # filed original of a batch item, with its metadata
        x = items["DCIM/x-small.png"]
        o = x["orig"]
        self.assertEqual(o["kind"], "image")
        self.assertEqual(o["meta"]["res"], "128x96")
        self.assertEqual(o["meta"]["folder"], "~/photo_library/2026/2026-11")
        self.assertEqual(o["meta"]["date"], "2026-11-01 10:00")
        self.assertTrue(o["meta"]["size"].endswith("B"))
        self.assertEqual(x["meta"]["res"], "64x48")
        self.assertEqual(x["meta"]["date"], "2026-11-02 11:00")
        self.assertNotIn(lib + "/x.png", json.dumps(x))  # folder only, relative to HOME; served by id
        r, b = s.req("GET", f"/o/{o['id']}")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "image/png"))
        with open(os.path.join(lib, "x.png"), "rb") as f:
            self.assertEqual(b, f.read())
        # video original: both sides are videos
        v = items["DCIM/v2.mp4"]
        self.assertEqual((v["kind"], v["orig"]["kind"]), ("video", "video"))
        if shutil.which("ffprobe"):
            self.assertEqual((v["orig"]["meta"]["res"], v["meta"]["res"]), ("320x240", "160x120"))
        r, b = s.req("GET", f"/o/{v['orig']['id']}", headers={"Range": "bytes=0-9"})
        self.assertEqual((r.status, r.getheader("Content-Type"), len(b)), (206, "video/mp4", 10))
        # outside the roots, another batch's quarantine, a symlink out, a ../ escape: never served
        for n in ("o1.png", "o2.png", "o3.png", "o4.png"):
            it = items[f"DCIM/{n}"]
            self.assertNotIn("id", it["orig"], n)
            self.assertIn("missing", it["orig"], n)
        served = sum(1 for i in items.values() if i.get("orig") and "id" in i["orig"])
        self.assertEqual(served, 2)
        for bad in (f"/o/{served}", "/o/999", "/o/-1", "/o/" + outside, "/o/" + other_q, "/o/0/../1", "/o/x"):
            r, body = s.req("GET", bad)
            self.assertEqual(r.status, 404, bad)
        # near-copy of an erased photo: a note with the date, no second media
        n = items["Downloads/n.jpg"]
        self.assertEqual(n["orig"], {"erased": True, "date": "2026-11-12"})
        # quarantine items that are not near-duplicates get no original
        self.assertIsNone(items["DCIM/Camera/a.jpg"]["orig"])
        r, page = s.req("GET", "/")
        self.assertIn(b"Near-copy of a photo you erased", page)
        self.assertIn(b"Original (kept)", page)
        self.assertIn(b"This copy", page)
        # an original that moves out of the roots after start-up is refused too
        os.remove(os.path.join(lib, "x.png"))
        os.symlink(outside, os.path.join(lib, "x.png"))
        r, _ = s.req("GET", f"/o/{o['id']}")
        self.assertEqual(r.status, 404)
        self.assertEqual(self.calls(), [])

    def test_empty_batch_exits_without_serving(self):
        r = subprocess.run([PY, "-I", UI, BATCH], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"nothing to review for {BATCH}")
        self.assertNotIn("xdg-open", r.stdout)

    def test_guards_headers_and_media(self):
        quar, vid = self.quarantine()
        self.review()
        s = self.start()
        self.assertIn("private browser window", s.note)
        # token
        r, _ = s.req("GET", "/", token=False)
        self.assertEqual(r.status, 403)
        r, _ = s.req("GET", "/api/items", token=False, headers={"X-Token": "wrong"})
        self.assertEqual(r.status, 403)
        r, _ = s.req("GET", "/m/0", token=False)
        self.assertEqual(r.status, 403)
        # host (DNS rebinding)
        for host in ("evil.example:%d" % s.port, "127.0.0.1:1", "127.0.0.1", "localhost.evil.example:%d" % s.port):
            r, _ = s.req("GET", "/api/items", host=host)
            self.assertEqual(r.status, 403, host)
        r, _ = s.req("GET", "/api/items", host=f"localhost:{s.port}")
        self.assertEqual(r.status, 200)
        # query token -> Strict HttpOnly cookie, which then works on its own
        r, _ = s.req("GET", f"/?t={s.token}", token=False)
        self.assertEqual(r.status, 303)
        ck = r.getheader("Set-Cookie")
        self.assertIn("SameSite=Strict", ck)
        self.assertIn("HttpOnly", ck)
        r, page = s.req("GET", "/", token=False, headers={"Cookie": ck.split(";")[0]})
        self.assertEqual(r.status, 200)
        self.assertNotIn(b"<script src", page)
        self.assertNotIn(b"//", page.replace(b"</", b""))  # no external URL of any kind: all inline
        # headers on every response, refusals included
        for path, tok in (("/", True), ("/m/0", True), ("/nope", True), ("/", False)):
            r, _ = s.req("GET", path, token=tok)
            self.assertEqual(r.getheader("Cache-Control"), "no-store, max-age=0", path)
            self.assertEqual(r.getheader("Pragma"), "no-cache")
            self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
            csp = r.getheader("Content-Security-Policy")
            for d in ("default-src 'none'", "img-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
                self.assertIn(d, csp)
        # media by id only
        items = self.items(s)
        self.assertEqual([i["tab"] for i in items].count("quarantine"), 4)
        self.assertEqual([i["tab"] for i in items].count("review"), 2)
        self.assertTrue(all("path" not in i for i in items))
        a = next(i for i in items if i["rel"] == "DCIM/Camera/a.jpg")
        self.assertEqual(a["reasons"], ["nudity:exposed"])
        r1 = next(i for i in items if i["rel"] == "DCIM/Camera/r1.jpg")
        self.assertEqual(r1["why"], "junk: tiny or empty")
        r, b = s.req("GET", f"/m/{a['id']}")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "image/jpeg"))
        with open(os.path.join(quar, "DCIM/Camera/a.jpg"), "rb") as f:
            self.assertEqual(b, f.read())
        for bad in ("/m/999", "/m/-1", "/m/0/../../etc/passwd", "/m/..%2f..%2fetc%2fpasswd",
                    "/m/" + os.path.join(quar, "DCIM/Camera/a.jpg"), "/../../etc/passwd", "/m/1x"):
            r, _ = s.req("GET", bad)
            self.assertEqual(r.status, 404, bad)
        # Range on the video
        v = next(i for i in items if i["kind"] == "video")
        size = os.path.getsize(vid)
        with open(vid, "rb") as f:
            raw = f.read()
        r, b = s.req("GET", f"/m/{v['id']}", headers={"Range": "bytes=10-99"})
        self.assertEqual(r.status, 206)
        self.assertEqual(r.getheader("Content-Range"), f"bytes 10-99/{size}")
        self.assertEqual(b, raw[10:100])
        r, b = s.req("GET", f"/m/{v['id']}", headers={"Range": "bytes=-16"})
        self.assertEqual((r.status, b), (206, raw[-16:]))
        r, _ = s.req("GET", f"/m/{v['id']}", headers={"Range": f"bytes={size}-"})
        self.assertEqual(r.status, 416)
        self.assertEqual(r.getheader("Accept-Ranges") or "bytes", "bytes")
        # nothing ran, nothing written
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "batches", BATCH, "review-decisions.json")))

    def test_submit_flow(self):
        quar, _ = self.quarantine()
        rv = self.review()
        thumbs = os.path.join(self.home, ".cache/thumbnails/normal")
        os.makedirs(thumbs)
        s = self.start("--phone", self.phone, "--drive", self.drive)
        items = self.items(s)
        csrf = s.csrf()
        ids = {(i["batch"], i["rel"]): i["id"] for i in items}
        want = {ids[(BATCH, "DCIM/Camera/a.jpg")]: "keep", ids[(BATCH, "DCIM/Camera/b.png")]: "erase",
                ids[(BATCH, "DCIM/Camera/c.mp4")]: "erase", ids[(f"near-{BATCH}", "Downloads/n.jpg")]: "erase",
                ids[(BATCH, "DCIM/Camera/r1.jpg")]: "delete", ids[(BATCH, "Pictures/Screenshots/r2.png")]: "keep"}
        full = {str(k): v for k, v in want.items()}
        # refused: no CSRF, foreign Origin, undecided item, wrong confirm, bad decision value
        r, _ = s.req("POST", "/api/submit", body={"decisions": full, "confirm": "ERASE"})
        self.assertEqual(r.status, 403)
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": csrf, "Origin": "http://evil.example"},
                     body={"decisions": full, "confirm": "ERASE"})
        self.assertEqual(r.status, 403)
        part = dict(full)
        part.pop(next(iter(part)))
        r, b = s.req("POST", "/api/submit", headers={"X-CSRF": csrf}, body={"decisions": part, "confirm": "ERASE"})
        self.assertEqual(r.status, 409, b)
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": csrf}, body={"decisions": full, "confirm": "erase"})
        self.assertEqual(r.status, 400)
        bad = dict(full, **{str(ids[(BATCH, "DCIM/Camera/r1.jpg")]): "erase"})  # review takes keep/delete
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": csrf}, body={"decisions": bad, "confirm": "ERASE"})
        self.assertEqual(r.status, 409)
        self.assertEqual(self.calls(), [])
        # accepted
        r, b = s.req("POST", "/api/submit", headers={"X-CSRF": csrf, "Origin": f"http://127.0.0.1:{s.port}"},
                     body={"decisions": full, "confirm": "ERASE"})
        self.assertEqual(r.status, 202, b)
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": csrf}, body={"decisions": full, "confirm": "ERASE"})
        self.assertEqual(r.status, 409)  # only once
        j, out = self.wait_done(s)
        self.assertTrue(j["ok"], out)
        dev = ["--phone", self.phone, "--drive", self.drive]
        nq = os.path.join(self.state, "quarantine", f"near-{BATCH}")
        self.assertEqual(self.calls(), [
            ["erase", f"near-{BATCH}", "--yes", *dev],
            ["verify-gone", f"near-{BATCH}", "--drive", self.drive],
            ["release", BATCH, os.path.join(quar, "DCIM/Camera/a.jpg")],
            ["erase", BATCH, "--yes", *dev],
            ["verify-gone", BATCH, "--drive", self.drive],
        ])
        self.assertFalse(os.path.exists(nq))
        # review: r1 deleted on its own, r2 left, no purge
        self.assertFalse(os.path.exists(os.path.join(rv, "DCIM/Camera/r1.jpg")))
        self.assertTrue(os.path.exists(os.path.join(rv, "Pictures/Screenshots/r2.png")))
        self.assertTrue(any("no command to file them yet" in x for x in out))
        self.assertTrue(any("verify-gone: no copies remain" in x for x in out))
        # decisions file: 0600, rel paths + decision only
        dp = os.path.join(self.state, "batches", BATCH, "review-decisions.json")
        self.assertEqual(stat.S_IMODE(os.stat(dp).st_mode), 0o600)
        with open(dp) as f:
            d = json.load(f)
        self.assertEqual(d["quarantine"], {BATCH: {"DCIM/Camera/a.jpg": "keep", "DCIM/Camera/b.png": "erase",
                                                   "DCIM/Camera/c.mp4": "erase"},
                                           f"near-{BATCH}": {"Downloads/n.jpg": "erase"}})
        self.assertEqual(d["review"], {"DCIM/Camera/r1.jpg": "delete", "Pictures/Screenshots/r2.png": "keep"})
        self.assertNotIn(self.state, json.dumps(d))
        # the server exits by itself once the page has read the last lines
        self.assertEqual(s.p.wait(timeout=30), 0)

    def test_all_review_deleted_uses_purge_review(self):
        self.review()
        s = self.start()
        items = self.items(s)
        self.assertTrue(all(i["tab"] == "review" for i in items))
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()},
                     body={"decisions": {str(i["id"]): "delete" for i in items}, "confirm": "ERASE"})
        self.assertEqual(r.status, 202)
        j, out = self.wait_done(s)
        self.assertTrue(j["ok"], out)
        self.assertEqual(self.calls(), [["purge-review", BATCH]])
        self.assertEqual(s.p.wait(timeout=30), 0)

    def xcheck(self):
        """quarantine/xcheck-<batch>: messaging copies of library photos; no plan, no manifest."""
        xq = os.path.join(self.state, "quarantine", f"xcheck-{BATCH}")
        lib = os.path.join(self.home, "photo_library/2025/2025-06")
        img(os.path.join(lib, "IMG_1.jpg"), (200, 120, 40), (160, 120))
        img(os.path.join(xq, "photo_library/Messaging/WhatsApp/2025/w1.jpg"), (200, 120, 40), (80, 60))
        img(os.path.join(xq, "photo_library/Messaging/WhatsApp/2025/w2.jpg"), (40, 120, 200), (80, 60))
        outside = os.path.join(self.tmp, "elsewhere.jpg")
        img(outside, (1, 1, 1))
        mdir = os.path.join(self.home, "photo_library/Messaging/WhatsApp/2025")
        write_json(os.path.join(xq, "quarantine.json"), {
            "photo_library/Messaging/WhatsApp/2025/w1.jpg": {
                "reasons": ["messaging copy of a camera photo"], "sha": "4" * 64, "size": 1,
                "dst": f"{mdir}/w1.jpg", "of": os.path.join(lib, "IMG_1.jpg")},
            "photo_library/Messaging/WhatsApp/2025/w2.jpg": {
                "reasons": ["messaging copy of a camera photo"], "sha": "5" * 64, "size": 1,
                "dst": f"{mdir}/w2.jpg", "of": outside}})
        return xq, lib

    def test_xcheck_messaging_copies(self):
        xq, lib = self.xcheck()
        s = self.start("--phone", self.phone, "--drive", self.drive)
        items = {i["rel"]: i for i in self.items(s)}
        self.assertEqual(len(items), 2)
        w1, w2 = (items[f"photo_library/Messaging/WhatsApp/2025/{n}"] for n in ("w1.jpg", "w2.jpg"))
        self.assertEqual((w1["tab"], w1["batch"]), ("quarantine", f"xcheck-{BATCH}"))
        self.assertEqual(w1["orig"]["label"], "WhatsApp/Messages copy of this photo")
        self.assertEqual((w1["orig"]["meta"]["res"], w1["meta"]["res"]), ("160x120", "80x60"))
        self.assertEqual(w1["orig"]["meta"]["folder"], "~/photo_library/2025/2025-06")
        self.assertNotIn("of", w1)
        r, b = s.req("GET", f"/o/{w1['orig']['id']}")
        with open(os.path.join(lib, "IMG_1.jpg"), "rb") as f:
            self.assertEqual((r.status, b), (200, f.read()))
        self.assertNotIn("id", w2["orig"])  # `of` outside the allowed roots: never served
        self.assertIn("missing", w2["orig"])
        r, _ = s.req("GET", "/o/1")
        self.assertEqual(r.status, 404)
        dec = {str(w1["id"]): "keep", str(w2["id"]): "erase"}
        r, b = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()}, body={"decisions": dec, "confirm": "ERASE"})
        self.assertEqual(r.status, 202, b)
        j, out = self.wait_done(s)
        self.assertTrue(j["ok"], out)
        self.assertEqual(self.calls(), [
            ["release", f"xcheck-{BATCH}", os.path.join(xq, "photo_library/Messaging/WhatsApp/2025/w1.jpg")],
            ["erase", f"xcheck-{BATCH}", "--yes", "--drive", self.drive],  # no --phone for xcheck
            ["verify-gone", f"xcheck-{BATCH}", "--drive", self.drive],
        ])
        self.assertEqual(s.p.wait(timeout=30), 0)

    def test_xcheck_batch_on_its_own(self):
        self.xcheck()
        env = self.env  # the quarantine-only batch by name: no plan.json or manifest, still serves
        s2 = subprocess.Popen([PY, "-I", UI, f"xcheck-{BATCH}"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        try:
            line = s2.stdout.readline()
            self.assertTrue(line.startswith("xdg-open 'http://127.0.0.1:"), line)
        finally:
            s2.kill()
            s2.wait()
            s2.stdout.close()

    def test_trash_decisions(self):
        quar, _ = self.quarantine()
        rv = self.review()
        xq, _ = self.xcheck()
        s = self.start("--phone", self.phone, "--drive", self.drive)
        items = self.items(s)
        ids = {(i["batch"], i["rel"]): i["id"] for i in items}
        X = f"xcheck-{BATCH}"
        w = "photo_library/Messaging/WhatsApp/2025/"
        want = {(BATCH, "DCIM/Camera/a.jpg"): "trash", (BATCH, "DCIM/Camera/b.png"): "erase",
                (BATCH, "DCIM/Camera/c.mp4"): "keep", (f"near-{BATCH}", "Downloads/n.jpg"): "keep",
                (BATCH, "DCIM/Camera/r1.jpg"): "trash", (BATCH, "Pictures/Screenshots/r2.png"): "delete",
                (X, w + "w1.jpg"): "trash", (X, w + "w2.jpg"): "erase"}
        dec = {str(ids[k]): v for k, v in want.items()}
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()},
                     body={"decisions": dict(dec, **{str(ids[(BATCH, "DCIM/Camera/r1.jpg")]): "erase"}), "confirm": "ERASE"})
        self.assertEqual(r.status, 409)  # review takes keep/delete/trash
        r, b = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()}, body={"decisions": dec, "confirm": "ERASE"})
        self.assertEqual(r.status, 202, b)
        j, out = self.wait_done(s)
        self.assertTrue(j["ok"], out)
        ph, dr = ["--phone", self.phone], ["--drive", self.drive]
        self.assertEqual(self.calls(), [
            ["trash", "--batch", BATCH, os.path.join(rv, "DCIM/Camera/r1.jpg"), *ph],
            ["purge-review", BATCH],
            ["release", f"near-{BATCH}", os.path.join(self.state, "quarantine", f"near-{BATCH}", "Downloads/n.jpg")],
            ["trash", "--batch", X, os.path.join(xq, w + "w1.jpg")],  # xcheck: never --phone
            ["erase", X, "--yes", *dr],
            ["verify-gone", X, *dr],
            ["trash", "--batch", BATCH, os.path.join(quar, "DCIM/Camera/a.jpg"), *ph],
            ["release", BATCH, os.path.join(quar, "DCIM/Camera/c.mp4")],
            ["erase", BATCH, "--yes", *ph, *dr],
            ["verify-gone", BATCH, *dr],
        ])
        with open(os.path.join(self.state, "batches", BATCH, "review-decisions.json")) as f:
            d = json.load(f)
        self.assertEqual(d["quarantine"][BATCH]["DCIM/Camera/a.jpg"], "trash")
        self.assertEqual(d["review"]["DCIM/Camera/r1.jpg"], "trash")
        self.assertEqual(s.p.wait(timeout=30), 0)

    def test_maybe_trash_tab(self):
        lib = os.path.join(self.home, "photo_library/2026/2026-11")
        pasta, floor = os.path.join(lib, "pasta.jpg"), os.path.join(lib, "floor.jpg")
        img(pasta, (200, 180, 90))
        img(floor, (60, 50, 40))
        outside = os.path.join(self.tmp, "o.jpg")
        img(outside, (1, 1, 1))
        inq = os.path.join(self.state, "quarantine", "b", "q.jpg")
        img(inq, (2, 2, 2))
        write_json(os.path.join(self.state, "maybe-trash.json"), {"batch": None, "items": [
            {"path": pasta, "labels": ["Review|maybe-trash", "Label|food"]}, {"path": floor, "labels": ["Review|maybe-trash"]},
            {"path": outside, "labels": []}, {"path": inq, "labels": []}, {"path": "relative.jpg"}]})
        s = self.start("--maybe-trash", "--phone", self.phone, batch=None)
        items = self.items(s)
        self.assertEqual([(i["tab"], i["rel"]) for i in items],
                         [("maybe", "~/photo_library/2026/2026-11/pasta.jpg"), ("maybe", "~/photo_library/2026/2026-11/floor.jpg")])
        self.assertEqual(items[0]["reasons"], ["Review|maybe-trash", "Label|food"])
        r, b = s.req("GET", f"/m/{items[0]['id']}")
        with open(pasta, "rb") as f:
            self.assertEqual((r.status, b), (200, f.read()))
        r, page = s.req("GET", "/")
        self.assertIn(b"Maybe trash", page)
        self.assertIn(b"Trash (T)", page)
        csrf = s.csrf()
        bad = {str(items[0]["id"]): "erase", str(items[1]["id"]): "trash"}
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": csrf}, body={"decisions": bad, "confirm": "ERASE"})
        self.assertEqual(r.status, 409)  # maybe items take keep or trash only
        dec = {str(items[0]["id"]): "trash", str(items[1]["id"]): "keep"}
        r, b = s.req("POST", "/api/submit", headers={"X-CSRF": csrf}, body={"decisions": dec, "confirm": "ERASE"})
        self.assertEqual(r.status, 202, b)
        j, out = self.wait_done(s)
        self.assertTrue(j["ok"], out)
        self.assertEqual(self.calls(), [["not-trash", floor], ["trash", pasta, "--phone", self.phone]])
        dp = os.path.join(self.state, "maybe-trash-decisions.json")
        self.assertEqual(stat.S_IMODE(os.stat(dp).st_mode), 0o600)
        self.assertTrue(os.path.isfile(outside) and os.path.isfile(inq))
        self.assertEqual(s.p.wait(timeout=30), 0)

    def test_unreleasable_keep_blocks_erase(self):
        quar, _ = self.quarantine()
        man = os.path.join(quar, "quarantine.json")
        with open(man) as f:
            q = json.load(f)
        q["DCIM/Camera/a.jpg"]["dst"] = None  # quarantined from review: no library place
        write_json(man, q)
        s = self.start()
        items = self.items(s)
        dec = {str(i["id"]): ("keep" if i["rel"] == "DCIM/Camera/a.jpg" else "erase") for i in items}
        r, _ = s.req("POST", "/api/submit", headers={"X-CSRF": s.csrf()}, body={"decisions": dec, "confirm": "ERASE"})
        self.assertEqual(r.status, 202)
        j, out = self.wait_done(s)
        self.assertFalse(j["ok"])
        self.assertNotIn(["erase", BATCH, "--yes"], self.calls())
        self.assertTrue(os.path.exists(os.path.join(quar, "DCIM/Camera/a.jpg")))
        self.assertEqual(s.p.wait(timeout=30), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
