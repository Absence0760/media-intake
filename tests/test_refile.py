"""refile: a date-fixed file moves, with its sidecar, into the YYYY/YYYY-MM folder of its fixed date, and every
record of its path is rekeyed (docs/dates.md, "Re-filing a fixed file"). Synthetic files in a throwaway HOME;
no exiftool needed (refile reads date-fixes.json, never the files)."""
import datetime, errno, hashlib, importlib.util, json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import support  # noqa: E402  (hermetic config)
sys.path.insert(0, support.PKG)
import intake  # noqa: E402
import label  # noqa: E402
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
        self.assertNotIn("refile", intake.READ_ONLY, "the dry run too: it reads what another job may be changing")


def sha(p):
    return hashlib.sha256(read(p)).hexdigest()


class MoveTest(unittest.TestCase):
    """_move, move_pair and _prune_empty on throwaway folders (LIB_ROOTS pointed at them)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refile-move-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lib, self.vids = os.path.join(self.tmp, "library"), os.path.join(self.tmp, "videos")
        os.makedirs(self.lib)
        os.makedirs(self.vids)
        p = mock.patch.object(intake, "LIB_ROOTS", [self.lib, self.vids])
        p.start()
        self.addCleanup(p.stop)
        self.src = os.path.join(self.lib, "2016", "2016-01", "a.jpg")
        self.dst = os.path.join(self.lib, "2024", "2024-05", "a.jpg")
        write(self.src, b"synthetic photo")
        write(intake.sidecar(self.src), b"<x:xmpmeta/>")

    def test_no_hard_links_falls_back_to_a_rename(self):
        for code in sorted(refile.NO_LINK):
            with self.subTest(errno=errno.errorcode[code]):
                with mock.patch.object(refile.os, "link", side_effect=OSError(code, "no links here")):
                    refile._move(self.src, self.dst)
                self.assertFalse(os.path.lexists(self.src))
                self.assertEqual(read(self.dst), b"synthetic photo")
                os.rename(self.dst, self.src)

    def test_no_hard_links_still_never_replaces(self):
        write(self.dst, b"another")
        with mock.patch.object(refile.os, "link", side_effect=OSError(errno.EPERM, "no links here")):
            with self.assertRaises(FileExistsError):
                refile._move(self.src, self.dst)
        self.assertEqual(read(self.dst), b"another")
        self.assertTrue(os.path.isfile(self.src))

    def test_another_filesystem_is_refused(self):
        with mock.patch.object(refile.os, "link", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(refile.CrossDevice):
                refile._move(self.src, self.dst)
        self.assertTrue(os.path.isfile(self.src))
        self.assertFalse(os.path.lexists(self.dst))

    def test_another_link_error_is_raised(self):
        with mock.patch.object(refile.os, "link", side_effect=OSError(errno.EIO, "i/o")):
            with self.assertRaises(OSError) as cm:
                refile._move(self.src, self.dst)
        self.assertEqual(cm.exception.errno, errno.EIO)
        self.assertTrue(os.path.isfile(self.src))

    def test_the_file_goes_back_when_its_sidecar_cannot_follow(self):
        write(intake.sidecar(self.dst), b"someone else's sidecar")  # the sidecar's move fails: EEXIST
        before = sha(self.src)
        with self.assertRaises(FileExistsError):
            refile.move_pair(self.src, self.dst)
        self.assertEqual(sha(self.src), before, "the file is back where it was")
        self.assertFalse(os.path.lexists(self.dst))
        self.assertEqual(read(intake.sidecar(self.src)), b"<x:xmpmeta/>")
        self.assertEqual(read(intake.sidecar(self.dst)), b"someone else's sidecar")

    def test_a_move_keeps_the_bytes_and_the_times(self):
        before, xmp, mtime = sha(self.src), read(intake.sidecar(self.src)), os.stat(self.src).st_mtime_ns
        refile.move_pair(self.src, self.dst)
        self.assertEqual(sha(self.dst), before)
        self.assertEqual(read(intake.sidecar(self.dst)), xmp)
        self.assertEqual(os.stat(self.dst).st_mtime_ns, mtime, "a rename: the resume records' stamp still holds")
        self.assertFalse(os.path.isdir(os.path.join(self.lib, "2016")), "the emptied folders go")

    def test_prune_never_removes_a_folder_with_anything_in_it_or_a_root(self):
        keep = os.path.join(self.lib, "2016", "b.jpg")
        write(keep, b"b")
        empty = os.path.join(self.lib, "2016", "2016-01", "x", "y")
        os.makedirs(empty)
        os.remove(self.src)
        os.remove(intake.sidecar(self.src))
        refile._prune_empty(empty)
        self.assertFalse(os.path.isdir(os.path.join(self.lib, "2016", "2016-01")))
        self.assertTrue(os.path.isfile(keep), "a folder with a file in it stays")
        os.remove(keep)
        refile._prune_empty(os.path.join(self.lib, "2016"))
        self.assertTrue(os.path.isdir(self.lib), "never the library folder itself")
        refile._prune_empty(self.vids)
        self.assertTrue(os.path.isdir(self.vids), "never the videos folder")
        outside = os.path.join(self.tmp, "elsewhere", "empty")
        os.makedirs(outside)
        refile._prune_empty(outside)
        self.assertTrue(os.path.isdir(outside), "never outside the library")


class PlanRefusals(unittest.TestCase):
    """plan() in-process, with the library, videos and state folders pointed at a throwaway HOME."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refile-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lib, self.vids = os.path.join(self.tmp, "library"), os.path.join(self.tmp, "videos")
        self.state = os.path.join(self.tmp, "state")
        for d in (self.lib, self.vids, self.state):
            os.makedirs(d)
        for name, v in (("LIBRARY", self.lib), ("VIDEOS", self.vids), ("LIB_ROOTS", [self.lib, self.vids]),
                        ("STATE", self.state)):
            p = mock.patch.object(intake, name, v)
            p.start()
            self.addCleanup(p.stop)

    def fixes(self, *paths, new="2024-05-12T10:00:00"):
        with open(os.path.join(self.state, "date-fixes.json"), "w") as f:
            json.dump(dict(version=1, fixes=[dict(id="f1", at="x", reason="", report=None, undone=None, dropped=[],
                                                  files=[dict(path=p, old="2016:01:01 00:00:00", new=new)
                                                         for p in paths])]), f)

    def test_a_flagged_or_quarantined_file_stays_put(self):
        p = os.path.join(self.lib, "2016", "2016-01", "a.jpg")
        write(p, b"flagged synthetic")
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-19 10:00 b1 sha256:{sha(p)} size:{os.path.getsize(p)}\n")
        self.assertEqual(label.refused([p], self.state), [p])
        self.fixes(p)
        moves, refused, here = refile.plan(self.state)
        self.assertEqual(moves, [])
        self.assertIn("never touched", refused[p])

    def test_a_symlink_or_a_file_outside_the_library_is_refused(self):
        real = os.path.join(self.lib, "2016", "2016-01", "a.jpg")
        write(real, b"a")
        link = os.path.join(self.lib, "2016", "2016-01", "link.jpg")
        os.symlink(real, link)
        outside = os.path.join(self.tmp, "elsewhere", "2016", "2016-01", "b.jpg")
        write(outside, b"b")
        missing = os.path.join(self.lib, "2016", "2016-01", "gone.jpg")
        self.fixes(link, outside, missing)
        moves, refused, _ = refile.plan(self.state)
        self.assertEqual(moves, [])
        self.assertEqual(set(refused), {link, outside, missing})
        self.assertTrue(all(w == "no longer in the library" for w in refused.values()))

    def test_another_drive_is_refused(self):
        p = os.path.join(self.lib, "2016", "2016-01", "a.jpg")
        write(p, b"a")
        self.fixes(p)
        with mock.patch.object(refile, "_dev_of", return_value=os.stat(p).st_dev + 1):
            moves, refused, _ = refile.plan(self.state)
        self.assertEqual(moves, [])
        self.assertIn("another drive", refused[p])

    def test_no_dated_folder_is_refused(self):
        p = os.path.join(self.lib, "my own folder", "a.jpg")
        write(p, b"a")
        self.fixes(p)
        moves, refused, _ = refile.plan(self.state)
        self.assertEqual(moves, [])
        self.assertIn("not in a dated folder", refused[p])

    def test_two_files_wanting_one_name_get_free_ones(self):
        a = os.path.join(self.lib, "2016", "2016-01", "IMG_0009.JPG")
        b = os.path.join(self.lib, "2016", "2016-02", "IMG_0009.JPG")
        taken = os.path.join(self.lib, "2024", "2024-05", "IMG_0009.JPG")
        for p in (a, b, taken):
            write(p, p.encode())
        self.fixes(a, b)
        moves, refused, _ = refile.plan(self.state)
        self.assertEqual(refused, {})
        stem = os.path.join(self.lib, "2024", "2024-05", "IMG_0009")
        self.assertEqual([mv["dst"] for mv in moves], [stem + "_1.JPG", stem + "_2.JPG"])

    def test_the_fixed_wall_time_decides_the_month_not_the_zone(self):
        import time
        p = os.path.join(self.lib, "2016", "2016-01", "a.jpg")
        write(p, b"a")
        self.fixes(p, new="2024-05-31T23:30:00-07:00")  # 2024-06-01 in New York, still May where it was taken
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, TZ="America/New_York"):
            time.tzset()
            moves, _, _ = refile.plan(self.state)
        self.assertEqual([mv["dst"] for mv in moves], [os.path.join(self.lib, "2024", "2024-05", "a.jpg")])

    def test_an_unreadable_log_is_an_empty_one(self):
        for junk in (b"not json", b"[1, 2]", b'{"version": 9, "refiles": []}', b'{"version": 1, "refiles": 3}'):
            with self.subTest(junk=junk):
                write(refile.log_path(self.state), junk)
                self.assertEqual(refile.load_log(self.state), dict(version=refile.VERSION, refiles=[]))
                self.assertEqual(refile.forget_paths({"/x"}, self.state)[0], 0)
                self.assertEqual(read(refile.log_path(self.state)), junk, "never rewritten by forget")


class ForgetRecords(PlanRefusals):
    """intake.path_forget: what erase, forget, trash and sweep-erased drop from refiles.json and the resume
    records; path_stale_count: what verify-gone counts."""

    def log(self, *refiles):
        datecheck_save = __import__("datecheck").save_private
        datecheck_save(refile.log_path(self.state), dict(version=1, refiles=[
            dict(id=f"r{i}", at="x", undone=None, moves=[{"from": a, "to": b} for a, b in mv])
            for i, mv in enumerate(refiles)]))

    def test_a_forgotten_file_takes_its_whole_move_history(self):
        L = lambda *p: os.path.join(self.lib, *p)  # noqa: E731
        x, y, z = L("2016/2016-01/a.jpg"), L("2020/2020-01/a.jpg"), L("2024/2024-05/a.jpg")
        o1, o2 = L("2016/2016-01/b.jpg"), L("2024/2024-05/b.jpg")
        write(z, b"a")
        write(o2, b"b")
        self.log([(x, y), (o1, o2)], [(y, z)])  # a refiled again later, b once
        for name in refile.PROGRESS:
            with open(os.path.join(self.state, name), "w") as f:
                json.dump({z: dict(digest="d", stamp=[1, 2]), o2: dict(digest="d", stamp=[1, 2])}, f)
        self.assertEqual(intake.path_stale_count(), 0)
        n, why = intake.path_forget({z})
        self.assertEqual((n, why), (4, ""), "two moves, and one entry in each resume record")
        log = refile.load_log(self.state)
        self.assertEqual([r["moves"] for r in log["refiles"]], [[{"from": o1, "to": o2}]], "the empty refile goes")
        for name in (refile.LOG, *refile.PROGRESS):
            p = os.path.join(self.state, name)
            self.assertNotIn("a.jpg", read(p).decode(), name)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, name)
        self.assertIn(o2, intake.load(os.path.join(self.state, refile.PROGRESS[1])))

    def test_a_file_gone_by_hand_is_counted_then_dropped(self):
        a, b = os.path.join(self.lib, "2016/2016-01/a.jpg"), os.path.join(self.lib, "2024/2024-05/a.jpg")
        write(os.path.join(self.lib, "2024/2024-06/other.jpg"), b"o")  # the library is there (not an empty mount)
        self.log([(a, b)])  # neither is there any more
        with open(os.path.join(self.state, "captions-progress.json"), "w") as f:
            json.dump({b: dict(digest="d", stamp=[1, 2])}, f)
        self.assertEqual(intake.path_stale_count(), 2)
        self.assertEqual(intake.path_forget(set()), (2, ""))
        self.assertEqual(intake.path_stale_count(), 0)

    def test_an_unplugged_folder_is_not_gone(self):
        shutil.rmtree(self.vids)
        a, b = os.path.join(self.vids, "2016/2016-01/v.mp4"), os.path.join(self.vids, "2024/2024-05/v.mp4")
        self.log([(a, b)])
        self.assertEqual(intake.path_stale_count(), 0)
        self.assertEqual(intake.path_forget(set()), (0, ""))


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
        st = os.stat(self.photo)
        for name in refile.PROGRESS:
            self.save(name, {self.photo: dict(digest="d", stamp=[st.st_size, st.st_mtime_ns], verdict="KEEP")})
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
        st = os.stat(self.photo_to)
        for name in refile.PROGRESS:  # vlm-trash --all and captions --all don't send the moved photo again
            self.assertEqual(self.load(name), {self.photo_to: dict(digest="d", stamp=[st.st_size, st.st_mtime_ns],
                                                                   verdict="KEEP")}, name)
            self.assertEqual(os.stat(os.path.join(self.state, name)).st_mode & 0o777, 0o600, name)
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
        for name in refile.PROGRESS:
            self.assertEqual(list(self.load(name)), [self.photo], name)
        self.assertEqual(self.load("date-fixes.json")["fixes"][0]["files"][0]["path"], self.photo)
        self.assertNotEqual(self.run_cli("--undo", rid, "--yes", ok=False).returncode, 0, "undone already")

    def test_a_locked_library_moves_nothing(self):
        holder = subprocess.Popen([PY, "-I", "-c", HOLDER, support.PKG], env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 30)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        before = self.snapshot()
        for args in (("--yes",), (), ("--undo",)):  # the dry runs wait for the lock too
            with self.subTest(args=args):
                r = self.run_cli(*args, env=dict(self.env, **{library_lock.WAIT_ENV: "0"}), ok=False)
                self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stderr)
                self.assertIn("refused", r.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_a_partial_failure_logs_and_rekeys_only_what_moved(self):
        blocked = os.path.join(self.vids, "2023")
        os.makedirs(blocked)
        os.chmod(blocked, 0o500)  # the video's new folder can't be made
        self.addCleanup(os.chmod, blocked, 0o700)
        r = self.run_cli("--yes", ok=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not moved", r.stdout)
        self.assertTrue(os.path.isfile(self.video), "the video stays")
        self.assertTrue(os.path.isfile(self.photo_to))
        moves = self.load(refile.LOG)["refiles"][0]["moves"]
        self.assertEqual(moves, [{"from": self.photo, "to": self.photo_to}, {"from": self.clash, "to": self.clash_to}])
        self.assertEqual(sorted(self.load("index.json")), sorted([self.photo_to, self.video, self.taken]))
        self.assertEqual([f["path"] for f in self.load("date-fixes.json")["fixes"][0]["files"]],
                         [self.photo_to, self.video, self.clash_to, self.stay])

    def test_undo_refuses_and_moves_nothing(self):
        self.assertIn("nothing to undo", self.run_cli("--undo", "--yes", ok=False).stderr)
        self.run_cli("--yes")
        rid = self.load(refile.LOG)["refiles"][0]["id"]
        r = self.run_cli("--undo", "nosuchid", "--yes", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no refile 'nosuchid'", r.stderr)
        write(self.photo, b"a new file at the old place")
        before = self.snapshot()
        r = self.run_cli("--undo", rid, "--yes", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("something is there now", r.stderr)
        self.assertEqual(self.snapshot(), before, "nothing moved, even with --yes")
        os.remove(self.photo)
        os.remove(self.video_to)
        before = self.snapshot()
        r = self.run_cli("--undo", "--yes", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no longer there", r.stderr)
        self.assertEqual(self.snapshot(), before, "nothing moved, even with --yes")
        self.assertIsNone(self.load(refile.LOG)["refiles"][0]["undone"])

    def test_a_garbage_log_is_no_crash(self):
        write(os.path.join(self.state, refile.LOG), b"\x00 not json")
        self.assertIn("nothing to undo", self.run_cli("--undo", ok=False).stderr)
        self.run_cli("--yes")
        self.assertEqual(len(self.load(refile.LOG)["refiles"]), 1)

    def test_wall_time_under_another_zone(self):
        fixes = self.load("date-fixes.json")
        fixes["fixes"][0]["files"][0]["new"] = "2024-05-31T23:30:00-07:00"
        self.save("date-fixes.json", fixes)
        self.run_cli("--yes", env=dict(self.env, TZ="America/New_York"))
        self.assertTrue(os.path.isfile(self.photo_to), "filed by the wall time it was taken: 2024-05")

    def test_trash_drops_the_moved_file_from_every_path_record(self):
        """Privacy: refiles.json and the resume records keep no path of a trashed file (old or new)."""
        self.run_cli("--yes")
        name = os.path.basename(self.photo)
        r = subprocess.run([PY, "-I", INTAKE, "trash", self.photo_to, "--yes"], env=self.env, capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.lexists(self.photo_to))
        for rel in (refile.LOG, *refile.PROGRESS):
            self.assertNotIn(name, read(os.path.join(self.state, rel)).decode(), rel)
        moves = self.load(refile.LOG)["refiles"][0]["moves"]
        self.assertEqual(sorted(mv["to"] for mv in moves), sorted([self.video_to, self.clash_to]),
                         "the others' moves stay")


if __name__ == "__main__":
    unittest.main()
