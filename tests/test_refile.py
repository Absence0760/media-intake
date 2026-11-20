"""refile: a date-fixed file moves, with its sidecar, into the YYYY/YYYY-MM folder of its fixed date, and every
record of its path is rekeyed (docs/dates.md, "Re-filing a fixed file"). Synthetic files in a throwaway HOME;
no exiftool needed (refile reads date-fixes.json, never the files)."""
import hashlib, importlib.util, json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import support  # noqa: E402  (hermetic config)
sys.path.insert(0, support.PKG)
import intake  # noqa: E402
import library_lock  # noqa: E402
import recog  # noqa: E402
import refile  # noqa: E402

PY = sys.executable
INTAKE = os.path.join(support.PKG, "intake.py")
HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HOLDER = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport library_lock\n"
          "fd = library_lock.acquire('a test holder')\nprint('held', flush=True)\nsys.stdin.read()\n")


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def read(p):
    with open(p, "rb") as f:
        return f.read()


def tree(*roots):
    """{path: sha256} of every file under roots (the test's own synthetic bytes)."""
    out = {}
    for r in roots:
        for d, _, fs in os.walk(r):
            for n in fs:
                p = os.path.join(d, n)
                with open(p, "rb") as f:
                    out[p] = hashlib.sha256(f.read()).hexdigest()
    return out


class TargetTest(unittest.TestCase):
    def test_only_the_dated_folders_change(self):
        import datetime
        when = datetime.datetime(2024, 5, 12, 10, 0)
        L = lambda *p: os.path.join(os.path.abspath(intake.LIBRARY), *p)  # noqa: E731
        V = lambda *p: os.path.join(os.path.abspath(intake.VIDEOS), *p)  # noqa: E731
        self.assertEqual(refile.target(L("2016", "2016-01", "a.jpg"), when), L("2024", "2024-05", "a.jpg"))
        self.assertEqual(refile.target(V("2016", "2016-01", "gopro-x", "GX010001.MP4"), when),
                         V("2024", "2024-05", "gopro-x", "GX010001.MP4"))
        self.assertEqual(refile.target(L("Screenshots", "2016", "s.png"), when), L("Screenshots", "2024", "s.png"))
        self.assertIsNone(refile.target(L("my own folder", "a.jpg"), when))
        self.assertIsNone(refile.target(os.path.join(tempfile.gettempdir(), "2016", "2016-01", "a.jpg"), when))

    def test_a_move_never_replaces_a_file(self):
        tmp = tempfile.mkdtemp(prefix="refile-move-")
        self.addCleanup(shutil.rmtree, tmp, True)
        a, b = os.path.join(tmp, "a.jpg"), os.path.join(tmp, "sub", "b.jpg")
        write(a, b"a")
        write(b, b"b")
        with self.assertRaises(FileExistsError):
            refile._move(a, b)
        self.assertEqual(read(b), b"b")
        self.assertTrue(os.path.exists(a))

    def test_refile_takes_the_library_lock(self):
        self.assertIn("refile", intake.LOCKED)


class RefileE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refile-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        os.makedirs(self.state, mode=0o700)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, TZ="UTC",
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")))
        self.env.pop(library_lock.FD_ENV, None)
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        V = lambda *p: os.path.join(self.vids, *p)  # noqa: E731
        self.photo, self.photo_to = L("2016/2016-01/IMG_0001.JPG"), L("2024/2024-05/IMG_0001.JPG")
        self.video, self.video_to = V("2016/2016-01/gopro-x/GX010001.MP4"), V("2023/2023-08/gopro-x/GX010001.MP4")
        self.clash, self.taken = L("2016/2016-02/IMG_0002.JPG"), L("2024/2024-05/IMG_0002.JPG")
        self.clash_to = L("2024/2024-05/IMG_0002_1.JPG")
        self.stay = L("2024/2024-06/IMG_0003.JPG")  # fixed to a date in its own month: stays
        for i, p in enumerate((self.photo, self.video, self.clash, self.taken, self.stay)):
            write(p, f"synthetic {i}".encode())
        for p in (self.photo, self.clash):
            write(intake.sidecar(p), b"<x:xmpmeta/>")
        fix = lambda p, new: dict(path=p, old="2016:01:01 00:00:00", new=new, tz=None, sha=None)  # noqa: E731
        self.save("date-fixes.json", dict(version=1, fixes=[dict(
            id="f1", at="2026-11-19T10:00:00", reason="", report="r1", undone=None,
            dropped=[dict(path=self.photo, kind="reset", confidence="high", reason="x", date="2016")],
            files=[fix(self.photo, "2024-05-12T10:00:00"), fix(self.video, "2023-08-01T09:00:00"),
                   fix(self.clash, "2024-05-13T10:00:00"), fix(self.stay, "2024-06-01T10:00:00")])]))
        self.save("datecheck.json", dict(version=1, id="r1", checked="x", files=4, findings=[
            dict(path=self.photo, kind="place", confidence="medium", reason="x")]))
        self.save("index.json", {p: dict(sha="0" * 64, size=11) for p in (self.photo, self.video, self.taken)})
        os.makedirs(os.path.join(self.state, "batches/b1"))
        self.save("batches/b1/plan.json", [dict(rel="DCIM/IMG_0001.JPG", action="file", dst=self.photo,
                                                lib_dst=self.photo)])
        self.save("places.json", {"version": 1, "libraries": {"k": {"places": {}, "tagged": {"p1": [self.photo]},
                                                                    "declined": {}}}})
        os.makedirs(os.path.join(self.state, "backup"))
        self.save("backup/d1.json", dict(version=1, files={
            "library/2016/2016-01/IMG_0001.JPG": dict(sha="0" * 64), "library/2016/2016-01/IMG_0001.JPG.xmp": {},
            "library/2024/2024-05/IMG_0002.JPG": dict(sha="1" * 64)}))
        self.save("maybe-trash.json", dict(batch=None, items=[dict(path=self.photo, labels=[])]))
        os.makedirs(os.path.join(self.state, "recognition"))
        c = sqlite3.connect(os.path.join(self.state, "recognition", recog.DB_NAME))
        c.executescript(recog.SCHEMA)
        c.execute("INSERT INTO files(path, sha, size, mtime) VALUES (?, 's', 11, 1)", (self.photo,))
        c.execute("INSERT INTO written(path, tag, state) VALUES (?, 'People|A', 'on')", (self.photo,))
        c.commit()
        c.close()
        if HAVE_NUMPY:
            import numpy as np
            with open(os.path.join(self.state, "embeddings.npz"), "wb") as f:
                np.savez(f, sha=np.array(["s"], dtype="U64"), emb=np.zeros((1, 4), np.float16),
                         files=np.array(json.dumps({self.photo: [11, 1, "s"]})))
        else:
            write(os.path.join(self.state, "embeddings.npz"), b"not read without numpy")

    def save(self, rel, obj):
        p = os.path.join(self.state, rel)
        with open(p, "w") as f:
            json.dump(obj, f)

    def load(self, rel):
        with open(os.path.join(self.state, rel)) as f:
            return json.load(f)

    def run_cli(self, *args, env=None, ok=True):
        r = subprocess.run([PY, "-I", INTAKE, "refile", *args], env=env or self.env, capture_output=True,
                           text=True, timeout=120)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def snapshot(self):
        """Every file and its content, leaving out the library lock's own file (its holder note)."""
        locks = os.path.join(self.state, "locks") + os.sep
        return {p: v for p, v in tree(self.lib, self.vids, self.state).items() if not p.startswith(locks)}

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        r = self.run_cli()
        self.assertIn("3 files to move (dry run: nothing moved), 1 already in place", r.stdout)
        self.assertIn("IMG_0002_1.JPG  (the name was taken", r.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_yes_moves_file_and_sidecar_and_rekeys_every_record(self):
        contents = {p: read(p) for p in (self.photo, self.video, self.clash, self.taken)}
        r = self.run_cli("--yes")
        for old, new in ((self.photo, self.photo_to), (self.video, self.video_to), (self.clash, self.clash_to)):
            self.assertFalse(os.path.lexists(old), old)
            self.assertEqual(read(new), contents[old], "the content is the same file's")
        self.assertEqual(read(self.taken), contents[self.taken], "the file already there is untouched")
        self.assertTrue(os.path.isfile(intake.sidecar(self.photo_to)))
        self.assertTrue(os.path.isfile(intake.sidecar(self.clash_to)))
        self.assertFalse(os.path.lexists(intake.sidecar(self.photo)))
        self.assertFalse(os.path.isdir(os.path.join(self.lib, "2016")), "the emptied folders go")
        self.assertTrue(os.path.isfile(self.stay))
        # every record of the old paths now names the new ones
        self.assertEqual(sorted(self.load("index.json")), sorted([self.photo_to, self.video_to, self.taken]))
        plan = self.load("batches/b1/plan.json")[0]
        self.assertEqual((plan["dst"], plan["lib_dst"]), (self.photo_to, self.photo_to))
        fx = self.load("date-fixes.json")["fixes"][0]
        self.assertEqual([f["path"] for f in fx["files"]], [self.photo_to, self.video_to, self.clash_to, self.stay])
        self.assertEqual(fx["dropped"][0]["path"], self.photo_to)
        self.assertEqual(self.load("datecheck.json")["findings"][0]["path"], self.photo_to)
        self.assertEqual(self.load("places.json")["libraries"]["k"]["tagged"]["p1"], [self.photo_to])
        self.assertEqual(self.load("maybe-trash.json")["items"][0]["path"], self.photo_to)
        self.assertEqual(sorted(self.load("backup/d1.json")["files"]), ["library/2024/2024-05/IMG_0002.JPG"],
                         "the old path's entries go: the drive has no copy under the new path yet")
        c = sqlite3.connect(os.path.join(self.state, "recognition", recog.DB_NAME))
        self.assertEqual([r[0] for r in c.execute("SELECT path FROM files")], [self.photo_to])
        self.assertEqual([r[0] for r in c.execute("SELECT path FROM written")], [self.photo_to])
        c.close()
        if HAVE_NUMPY:
            import numpy as np
            z = np.load(os.path.join(self.state, "embeddings.npz"), allow_pickle=False)
            self.assertEqual(list(json.loads(str(z["files"]))), [self.photo_to])
        else:
            self.assertIn("search index: 0 (not rekeyed (no numpy here)", r.stdout)
        log = os.path.join(self.state, refile.LOG)
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        # a second run finds everything in place and changes nothing
        before = self.snapshot()
        r = self.run_cli("--yes")
        self.assertIn("0 files to move", r.stdout)
        self.assertEqual(self.snapshot(), before)
        # --undo puts every file back, and the records with them
        rid = self.load(refile.LOG)["refiles"][0]["id"]
        self.run_cli("--undo")
        self.assertTrue(os.path.isfile(self.photo_to), "--undo without --yes moves nothing")
        self.run_cli("--undo", rid, "--yes")
        for old, new in ((self.photo, self.photo_to), (self.video, self.video_to), (self.clash, self.clash_to)):
            self.assertTrue(os.path.isfile(old))
            self.assertFalse(os.path.lexists(new))
        self.assertTrue(os.path.isfile(intake.sidecar(self.photo)))
        self.assertIn(self.photo, self.load("index.json"))
        self.assertEqual(self.load("date-fixes.json")["fixes"][0]["files"][0]["path"], self.photo)
        self.assertNotEqual(self.run_cli("--undo", rid, "--yes", ok=False).returncode, 0, "undone already")

    def test_a_locked_library_moves_nothing(self):
        holder = subprocess.Popen([PY, "-I", "-c", HOLDER, support.PKG], env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 30)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        before = self.snapshot()
        r = self.run_cli("--yes", env=dict(self.env, **{library_lock.WAIT_ENV: "0"}), ok=False)
        self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
        self.assertIn("refused", r.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
