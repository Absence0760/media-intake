#!/usr/bin/env python3
"""Tests for `media-intake prune-cache` (cache_prune.py) on a synthetic library and cache in a throwaway HOME.

  python3 -I -m unittest discover -s tests -p test_cache_prune.py

The cache holds, per content: the legacy <sha>.jpg, every -r<turn>.jpg and a proxy, for content in the
library (turned alike, turned differently by a sidecar), content gone from it, and a flagged file's content.
Sidecars are hand-written XMP (no exiftool). The command runs as a subprocess with HOME set, so the real
~/.cache and library are never looked at.
"""
import fcntl, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (hermetic config)

CLI = os.path.join(support.PKG, "cli.py")
PY = sys.executable
# Runs cli.main with intake.sha256 failing (PermissionError) for one path: an unreadable file even as root.
DENY = """import sys
sys.path.insert(0, sys.argv[1])
import cli, intake
real, denied = intake.sha256, sys.argv[2]
def sha256(p):
    if p == denied:
        raise PermissionError(13, "Permission denied", p)
    return real(p)
intake.sha256 = sha256
sys.exit(cli.main(sys.argv[3:]))
"""

from PIL import Image  # noqa: E402

import cache_prune  # noqa: E402
import library_lock  # noqa: E402


def xmp(orient):
    return ("<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>\n<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n"
            "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n"
            f"<rdf:Description xmlns:tiff='http://ns.adobe.com/tiff/1.0/' tiff:Orientation='{orient}'/>\n"
            "</rdf:RDF>\n</x:xmpmeta>\n<?xpacket end='w'?>\n")


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def snapshot(folder):
    """{path: bytes} of every file under folder (originals and sidecars must come out byte-identical)."""
    out = {}
    for d, _, names in os.walk(folder):
        for n in names:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                with open(p, "rb") as f:
                    out[p] = f.read()
    return out


class PruneCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cache-prune-test-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        self.vids = os.path.join(self.home, "Videos/personal")
        self.foot = os.path.join(self.home, "Videos/demo-footage")
        for d in (self.state, os.path.join(self.foot, "originals")):
            os.makedirs(d)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=self.vids, MEDIA_INTAKE_CONFIG=support.write_config(self.foot),
                        MEDIA_INTAKE_LOCK_WAIT="0")
        self.thumbs = os.path.join(self.home, ".cache/media-intake/thumbs")
        self.proxies = os.path.join(self.home, ".cache/media-intake/proxies")
        os.makedirs(self.thumbs, mode=0o700)
        os.makedirs(self.proxies, mode=0o700)
        L = lambda *p: os.path.join(self.lib, "2026/2026-11", *p)  # noqa: E731
        os.makedirs(L())
        os.makedirs(os.path.join(self.vids, "2026/2026-09"))
        # a.jpg shows turn 0; its byte-identical twin is turned 180 in its sidecar; r.jpg is turned 90.
        Image.new("RGB", (40, 30), (200, 30, 30)).save(L("a.jpg"))
        shutil.copyfile(L("a.jpg"), L("twin.jpg"))
        with open(L("twin.jpg.xmp"), "w") as f:
            f.write(xmp(3))
        Image.new("RGB", (40, 30), (30, 200, 30)).save(L("r.jpg"))
        with open(L("r.jpg.xmp"), "w") as f:
            f.write(xmp(6))
        self.v = os.path.join(self.vids, "2026/2026-09/v.mp4")
        with open(self.v, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42 not a real video")  # never decoded: no sidecar, turn 0
        # Flagged (erase log): the gallery never lists it, so its cached copies are not "in the library".
        self.secret = L("secret.jpg")
        Image.new("RGB", (40, 30), (1, 2, 3)).save(self.secret)
        with open(os.path.join(self.state, "erase-log.txt"), "w") as f:
            f.write(f"2026-11-01 10:00 b0 sha256:{sha(self.secret)} size:{os.path.getsize(self.secret)}\n")
        self.A, self.R, self.V, self.S = sha(L("a.jpg")), sha(L("r.jpg")), sha(self.v), sha(self.secret)
        self.O = "e" * 64  # content no longer anywhere
        T = lambda name: os.path.join(self.thumbs, name)  # noqa: E731
        P = lambda name: os.path.join(self.proxies, name)  # noqa: E731
        self.keep = [T(self.A + "-r0.jpg"), T(self.A + "-r180.jpg"), T(self.R + "-r90.jpg"), T(self.V + "-r0.jpg"),
                     P(self.V + ".mp4"), P(self.A + ".mp4"),  # a proxy is kept while its content is there
                     P(self.V + ".failed"),  # and so is the record that none could be made
                     # names the gallery doesn't write as a cache entry: never touched
                     T(self.A + "-r0.jpg.123.140.tmp"), T(self.O + "-r45.jpg"), T("notes.txt"), P("x.mp4")]
        self.legacy = [T(h + ".jpg") for h in (self.A, self.R, self.O)]
        self.turns = [T(self.A + "-r90.jpg"), T(self.A + "-r270.jpg"), T(self.R + "-r0.jpg")]
        self.gone = [T(self.O + "-r0.jpg"), T(self.O + "-r180.jpg"), T(self.S + "-r0.jpg"),
                     P(self.O + ".mp4"), P(self.S + ".mp4"), P(self.O + ".failed")]
        for p in self.keep + self.legacy + self.turns + self.gone:
            with open(p, "wb") as f:
                f.write(b"x" * 10)
        # A proxy work folder (a transcode in progress) and symlinks under cache names, pointing outside.
        os.makedirs(P(".work-1"), mode=0o700)
        os.symlink(L("a.jpg"), P(".work-1/in"))
        self.outside = os.path.join(self.tmp, "outside.jpg")
        with open(self.outside, "wb") as f:
            f.write(b"outside")
        self.links = [T(self.O + "-r90.jpg"), T(self.O + "-r270.jpg"), P("f" * 64 + ".mp4")]
        for p in self.links:
            os.symlink(self.outside, p)
        self.media = snapshot(self.home + "/Pictures")
        self.media.update(snapshot(self.home + "/Videos"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args):
        return subprocess.run([PY, "-I", CLI, "prune-cache", *args], env=self.env, capture_output=True, text=True,
                              timeout=120)

    def present(self, paths):
        return [p for p in paths if os.path.lexists(p)]

    def assert_untouched_outside(self):
        after = snapshot(self.home + "/Pictures")
        after.update(snapshot(self.home + "/Videos"))
        self.assertEqual(after, self.media)  # originals and sidecars byte-identical, none gone
        with open(self.outside, "rb") as f:
            self.assertEqual(f.read(), b"outside")
        self.assertEqual(self.present(self.links), self.links)  # symlinks neither followed nor removed
        self.assertTrue(os.path.islink(os.path.join(self.proxies, ".work-1/in")))

    def test_dry_run_deletes_nothing(self):
        before = sorted(os.listdir(self.thumbs)) + sorted(os.listdir(self.proxies))
        r = self.run_cli("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(os.listdir(self.thumbs)) + sorted(os.listdir(self.proxies)), before)
        self.assertIn("would delete 3 legacy thumbnails", r.stdout)
        self.assertIn("would delete 3 thumbnails of a turn no copy shows", r.stdout)
        self.assertIn("would delete 3 thumbnails of content not in the library", r.stdout)
        self.assertIn("would delete 3 video proxies of content not in the library", r.stdout)
        self.assertIn("would delete 12 files", r.stdout)
        self.assertIn("left 3 entries that are not regular files", r.stdout)
        self.assert_untouched_outside()

    def test_prune(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("deleted 12 files", r.stdout)
        self.assertEqual(self.present(self.legacy + self.turns + self.gone), [])
        self.assertEqual(self.present(self.keep), self.keep)
        self.assertNotIn(self.lib, r.stdout + r.stderr)  # counts, never media paths
        self.assert_untouched_outside()
        r = self.run_cli()  # nothing left to do
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("deleted 0 files", r.stdout)

    def test_missing_library_folder_prunes_only_legacy(self):
        os.rmdir(os.path.join(self.foot, "originals"))  # the project folder, as on an unplugged drive
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("library folder not found", r.stdout)
        self.assertIn("only legacy thumbnails are in scope this time (a library folder is missing, so what the "
                      "library holds is unknown). Run it again once every library folder is there and readable.",
                      r.stdout)
        self.assertNotIn("pruned", r.stdout)
        self.assertEqual(self.present(self.legacy), [])
        self.assertEqual(self.present(self.keep + self.turns + self.gone), self.keep + self.turns + self.gone)

    def test_dry_run_with_a_missing_folder_never_says_pruned(self):
        """A dry run deletes nothing, so its summary never says anything was pruned or deleted."""
        os.rmdir(os.path.join(self.foot, "originals"))
        before = sorted(os.listdir(self.thumbs)) + sorted(os.listdir(self.proxies))
        r = self.run_cli("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(os.listdir(self.thumbs)) + sorted(os.listdir(self.proxies)), before)
        self.assertIn("would delete 3 legacy thumbnails", r.stdout)
        self.assertIn("only legacy thumbnails are in scope this time", r.stdout)
        for word in ("pruned", "deleted"):
            self.assertNotIn(word, r.stdout)

    def test_help_says_what_it_deletes(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        h = " ".join(r.stdout.split())
        self.assertIn("usage: media-intake prune-cache", h)
        self.assertIn("DELETES the gallery's cached thumbnails and video proxies", h)
        self.assertIn("Never touches your photos, videos or tags", h)
        self.assertIn("only count what would be deleted", h)

    def test_symlinked_cache_folder_is_not_followed(self):
        """thumbs/ itself a symlink to a folder elsewhere: nothing in that folder is listed or deleted."""
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.rename(self.thumbs, elsewhere)
        os.symlink(elsewhere, self.thumbs)
        listed = sorted(os.listdir(elsewhere))
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(os.listdir(elsewhere)), listed)
        self.assertEqual(self.present([os.path.join(self.proxies, self.O + ".mp4")]), [])  # proxies/ still pruned

    def test_refused_while_the_library_is_locked(self):
        path = library_lock.lock_path({"library": self.lib, "state": self.state})
        os.makedirs(os.path.dirname(path), mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            r = self.run_cli()
            self.assertEqual(r.returncode, library_lock.EX_TEMPFAIL, r.stdout + r.stderr)
            self.assertIn("refused", r.stderr)
            self.assertEqual(self.present(self.legacy), self.legacy)  # nothing deleted
            r = self.run_cli("--dry-run")  # writes nothing, so it doesn't need the lock
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            os.close(fd)


    def only_legacy_went(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 library files could not be read", r.stdout)
        self.assertIn("only legacy thumbnails are in scope this time (a library file could not be read, so",
                      r.stdout)
        self.assertEqual(self.present(self.legacy), [])
        self.assertEqual(self.present(self.keep + self.turns + self.gone), self.keep + self.turns + self.gone)
        self.assert_untouched_outside()

    def unindexed(self):
        """A library file intake never indexed, so prune-cache must hash it."""
        p = os.path.join(self.lib, "2026/2026-11", "u.jpg")
        Image.new("RGB", (40, 30), (9, 99, 199)).save(p)
        with open(p, "rb") as f:
            self.media[p] = f.read()
        return p

    def test_unreadable_file_prunes_only_legacy(self):
        """A file whose content can't be read may be a copy of anything: only the legacy names go."""
        u = self.unindexed()
        script = os.path.join(self.tmp, "deny.py")
        with open(script, "w") as f:
            f.write(DENY)
        r = subprocess.run([PY, "-I", script, support.PKG, u, "prune-cache"], env=self.env, capture_output=True,
                           text=True, timeout=120)
        self.only_legacy_went(r)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-0 file; the fault-injecting test covers this")
    def test_mode_0_file_prunes_only_legacy(self):
        u = self.unindexed()
        os.chmod(u, 0)
        try:
            r = self.run_cli()
        finally:
            os.chmod(u, 0o600)
        self.only_legacy_went(r)

    def test_index_sha_only_when_the_size_matches(self):
        """index.json's sha is taken for a file whose size matches; a stale entry (other size) is hashed."""
        a_jpg, r_jpg = (os.path.join(self.lib, "2026/2026-11", n) for n in ("a.jpg", "r.jpg"))
        D, E = "d" * 64, "c" * 64
        with open(os.path.join(self.state, "index.json"), "w") as f:
            json.dump({a_jpg: {"sha": D, "size": os.path.getsize(a_jpg)},
                       r_jpg: {"sha": E, "size": os.path.getsize(r_jpg) + 1}}, f)
        T = lambda name: os.path.join(self.thumbs, name)  # noqa: E731
        for n in (D + "-r0.jpg", E + "-r90.jpg"):
            with open(T(n), "wb") as f:
                f.write(b"x")
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # a.jpg: index.json's sha, not hashed, so turn 0 is D's; A keeps only its twin's turn 180.
        self.assertTrue(os.path.exists(T(D + "-r0.jpg")))
        self.assertFalse(os.path.exists(T(self.A + "-r0.jpg")))
        self.assertTrue(os.path.exists(T(self.A + "-r180.jpg")))
        # r.jpg: the stale entry is ignored and the file hashed.
        self.assertFalse(os.path.exists(T(E + "-r90.jpg")))
        self.assertTrue(os.path.exists(T(self.R + "-r90.jpg")))
        self.assert_untouched_outside()


class InUseTest(unittest.TestCase):
    """in_use without a library: a failing read counts as unread, never as a sha."""

    def test_unreadable_counts(self):
        items = [dict(path="/nowhere/a.jpg", media="photo", sha="a" * 64),
                 dict(path="/nowhere/b.jpg", media="photo", sha=None)]
        with unittest.mock.patch.object(cache_prune.intake, "sha256", side_effect=PermissionError(13, "denied")):
            used, unread = cache_prune.in_use(items)
        self.assertEqual((used, unread), ({"a" * 64: {0}}, 1))


class OpenCacheDirTest(unittest.TestCase):
    """open_cache_dir never enters a cache folder through a symlink, at either level."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cache-prune-dir-")
        self.real = os.path.join(self.tmp, "real-media-intake")
        os.makedirs(os.path.join(self.real, "proxies"), mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_real_folder_opens(self):
        fd = cache_prune.open_cache_dir(os.path.join(self.real, "proxies"))
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_media_intake_folder_symlink_refused(self):
        link = os.path.join(self.tmp, "media-intake")
        os.symlink(self.real, link)
        self.assertIsNone(cache_prune.open_cache_dir(os.path.join(link, "proxies")))

    def test_proxies_symlink_refused(self):
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(cache, mode=0o700)
        os.symlink(os.path.join(self.real, "proxies"), os.path.join(cache, "proxies"))
        self.assertIsNone(cache_prune.open_cache_dir(os.path.join(cache, "proxies")))

    def test_absent_folder(self):
        self.assertIsNone(cache_prune.open_cache_dir(os.path.join(self.tmp, "none", "proxies")))


class ReasonTest(unittest.TestCase):
    """The name rules, without a library."""

    def test_names(self):
        h, o = "a" * 64, "b" * 64
        used = {h: {0, 90}}
        self.assertEqual(cache_prune.thumb_reason(h + ".jpg", used, True), cache_prune.LEGACY)
        self.assertEqual(cache_prune.thumb_reason(h + ".jpg", used, False), cache_prune.LEGACY)
        self.assertIsNone(cache_prune.thumb_reason(h + "-r90.jpg", used, True))
        self.assertEqual(cache_prune.thumb_reason(h + "-r180.jpg", used, True), cache_prune.TURN)
        self.assertEqual(cache_prune.thumb_reason(o + "-r0.jpg", used, True), cache_prune.GONE_THUMB)
        self.assertIsNone(cache_prune.thumb_reason(o + "-r0.jpg", used, False))
        for odd in (h + "-r090.jpg", h + "-r45.jpg", h.upper() + ".jpg", h[:63] + ".jpg", h + ".jpg.tmp",
                    "../" + o + ".jpg", h + ".JPG"):
            self.assertIsNone(cache_prune.thumb_reason(odd, used, True), odd)
        self.assertIsNone(cache_prune.proxy_reason(h + ".mp4", used, True))
        self.assertEqual(cache_prune.proxy_reason(o + ".mp4", used, True), cache_prune.GONE_PROXY)
        self.assertIsNone(cache_prune.proxy_reason(o + ".mp4", used, False))
        self.assertIsNone(cache_prune.proxy_reason(".work-1", used, True))


if __name__ == "__main__":
    unittest.main()
