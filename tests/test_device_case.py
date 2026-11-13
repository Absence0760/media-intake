#!/usr/bin/env python3
"""Case-insensitive device storage: no device delete of a path that differs from another only by case.

  python3 -I -m unittest discover -s tests -p test_device_case.py

Android's emulated storage, and the FAT/exFAT of cameras and drives, ignore letter
case. After a case-only rename the listing (gvfs over MTP or gphoto2) can show both
names, they are one file on the device, and deleting the "duplicate" deletes the real
one. The laptop's filesystem is case-sensitive, so a fake device tree here holds two
real files that differ only by case, the way the listing shows them. Each test drives
the real intake.py (stage, plan, apply, then offload / erase / trash) in a throwaway
HOME with synthetic Pillow images, and checks the delete is refused with both names
listed and every device file survives.
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

from PIL import Image, ImageDraw  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
INTAKE = os.path.join(HERE, "intake.py")
PY = sys.executable


def picture(path, seed, dt):
    """A synthetic JPEG with an EXIF date and enough structure for dHash."""
    im = Image.new("RGB", (800, 600), (30 * seed % 255, 80, 140))
    d = ImageDraw.Draw(im)
    for i in range(10):
        x, y = (i * 83 * (seed + 2)) % 800, (i * 47 * (seed + 4)) % 600
        d.rectangle([x, y, x + 130, y + 110], fill=((i * 37 + seed * 60) % 255, (i * 29) % 255, (seed * 45) % 255))
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = dt
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, quality=90, exif=ex)
    return path


def same_file_twice(path, other_name, seed, dt):
    """path and a case-only variant of it in the same folder, identical bytes (one file on the device)."""
    picture(path, seed, dt)
    twin = os.path.join(os.path.dirname(path), other_name)
    shutil.copyfile(path, twin)
    return path, twin


class DeviceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intake-device-case-")
        t = self.tmp
        self.home = os.path.join(t, "home")
        self.state = os.path.join(self.home, ".local/share/media-intake")
        self.lib = os.path.join(self.home, "Pictures/library")
        gvfs = os.path.join(t, "run", "gvfs")  # where GNOME mounts devices; synthetic host names
        self.mtp = os.path.join(gvfs, "mtp:host=SYNTHETIC_PHONE", "Internal shared storage")
        self.gphoto = os.path.join(gvfs, "gphoto2:host=SYNTHETIC_CAMERA")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, MEDIA_INTAKE_STATE=self.state, PHOTO_LIBRARY=self.lib,
                        PERSONAL_FOOTAGE=os.path.join(self.home, "Videos/personal"),
                        MEDIA_INTAKE_CONFIG=support.write_config(os.path.join(self.home, "Videos/demo-footage")),
                        TZ="America/New_York", MEDIA_INTAKE_NO_LOCALSEARCH="1")
        # Android over MTP: a case-only renamed camera photo listed under both names, and a plain one.
        self.m_upper, self.m_lower = same_file_twice(
            os.path.join(self.mtp, "DCIM/Camera/IMG_20261101_100000.jpg"), "img_20261101_100000.jpg", 1,
            "2026:11:01 10:00:00")
        self.m_plain = picture(os.path.join(self.mtp, "DCIM/Camera/IMG_20261102_100000.jpg"), 2, "2026:11:02 10:00:00")
        # An iPhone over gphoto2: the same, in DCIM/100APPLE.
        self.g_upper, self.g_lower = same_file_twice(
            os.path.join(self.gphoto, "DCIM/100APPLE/IMG_0001.JPG"), "img_0001.jpg", 3, "2026:11:03 10:00:00")
        self.g_plain = picture(os.path.join(self.gphoto, "DCIM/100APPLE/IMG_0002.JPG"), 4, "2026:11:04 10:00:00")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, *args, code=0):
        r = subprocess.run([PY, "-I", INTAKE, *args], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, code, f"{args}\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def filed(self, root, batch, flags=None):
        self.intake("stage", root, "--kind", "phone", "--batch", batch)
        self.intake("plan", batch)
        if flags is None:
            self.intake("apply", batch, "--no-scan")
        else:
            with open(os.path.join(self.state, "batches", batch, "scan.json"), "w") as f:
                json.dump(flags, f)
            self.intake("apply", batch, "--allow-missing-detectors")

    def assert_refused(self, out, root, *names):
        self.assertIn("refused: nothing deleted from the device", out)
        self.assertIn("differ only by letter case", out)
        rels = [os.path.relpath(n, root) for n in names]
        self.assertTrue(any(all(r in ln for r in rels) for ln in out.splitlines()),
                        f"colliding paths listed together:\n{out}")

    def assert_on_device(self, *paths):
        for p in paths:
            self.assertTrue(os.path.isfile(p), f"deleted from the device: {p}")

    # ------------------------------------------------------------------ offload
    def test_offload_refused_on_mtp_then_allowed_once_resolved(self):
        self.filed(self.mtp, "2026-11-13-mtp")
        out = self.intake("offload", "2026-11-13-mtp", "--phone", self.mtp, "--yes", code=1)
        self.assert_refused(out, self.mtp, self.m_upper, self.m_lower)
        self.assertNotIn(os.path.basename(self.m_plain), out, "only the colliding paths are listed")
        self.assert_on_device(self.m_upper, self.m_lower, self.m_plain)  # nothing at all was deleted
        # Resolved on the device (one name left) and staged again: offload goes ahead.
        os.remove(self.m_lower)
        shutil.rmtree(os.path.join(self.state, "batches", "2026-11-13-mtp"))
        self.filed(self.mtp, "2026-11-13-mtp")
        out = self.intake("offload", "2026-11-13-mtp", "--phone", self.mtp, "--yes")
        self.assertIn("deleted 2 files from the phone", out)
        self.assertFalse(os.path.exists(self.m_upper) or os.path.exists(self.m_plain))

    def test_offload_refused_on_gphoto2(self):
        self.filed(self.gphoto, "2026-11-13-cam")
        out = self.intake("offload", "2026-11-13-cam", "--phone", self.gphoto, "--yes", code=1)
        self.assert_refused(out, self.gphoto, self.g_upper, self.g_lower)
        self.assert_on_device(self.g_upper, self.g_lower, self.g_plain)

    def test_offload_refused_when_only_one_name_is_a_target(self):
        """The other name needn't be deleted itself: any manifest path counts."""
        self.filed(self.mtp, "2026-11-13-mtp")
        plan = os.path.join(self.state, "batches", "2026-11-13-mtp", "plan.json")
        with open(plan) as f:
            rows = json.load(f)
        for p in rows:  # make the lower-case name ineligible for offload (say it went to review)
            if p["rel"] == os.path.relpath(self.m_lower, self.mtp):
                p["action"] = "review"
        with open(plan, "w") as f:
            json.dump(rows, f)
        out = self.intake("offload", "2026-11-13-mtp", "--phone", self.mtp, "--yes", code=1)
        self.assert_refused(out, self.mtp, self.m_upper, self.m_lower)
        self.assert_on_device(self.m_upper, self.m_lower, self.m_plain)

    def test_offload_refused_on_a_source_drive(self):
        """exFAT ignores case too: a drive batch's offload goes through the same gate."""
        mount = os.path.join(self.tmp, "media", "SYNTHETIC_DRIVE")
        upper, lower = same_file_twice(os.path.join(mount, "trip/IMG_0100.JPG"), "img_0100.jpg", 6,
                                       "2026:11:06 10:00:00")
        self.env.update(MEDIA_INTAKE_TEST_MOUNT=mount, MEDIA_INTAKE_TEST_UUID="0000-TEST")
        self.intake("stage", mount, "--kind", "drive", "--batch", "2026-11-13-drive")
        self.intake("plan", "2026-11-13-drive")
        self.intake("apply", "2026-11-13-drive", "--no-scan")
        out = self.intake("offload", "2026-11-13-drive", "--phone", mount, "--yes", code=1)
        self.assert_refused(out, mount, upper, lower)
        self.assert_on_device(upper, lower)

    # ------------------------------------------------------------------ erase
    def test_erase_refused_on_mtp_and_nothing_erased(self):
        rels = [os.path.relpath(p, self.mtp) for p in (self.m_upper, self.m_lower)]
        self.filed(self.mtp, "2026-11-13-mtp", {r: ["nudity"] for r in rels})  # as scan flags both copies
        quar = os.path.join(self.state, "quarantine", "2026-11-13-mtp")
        held = [r for r in rels if os.path.isfile(os.path.join(quar, r))]
        self.assertEqual(len(held), 1, "the twin is an exact duplicate: quarantined once")
        out = self.intake("erase", "2026-11-13-mtp", "--phone", self.mtp, "--yes", code=1)
        self.assert_refused(out, self.mtp, self.m_upper, self.m_lower)
        self.assert_on_device(self.m_upper, self.m_lower, self.m_plain)
        self.assertTrue(os.path.isfile(os.path.join(quar, held[0])), "the quarantined copy stays until a real erase")
        self.assertFalse(os.path.exists(os.path.join(self.state, "erase-log.txt")))

    def test_erase_refused_on_gphoto2(self):
        self.filed(self.gphoto, "2026-11-13-cam",
                   {os.path.relpath(p, self.gphoto): ["passport-mrz"] for p in (self.g_upper, self.g_lower)})
        out = self.intake("erase", "2026-11-13-cam", "--phone", self.gphoto, "--yes", code=1)
        self.assert_refused(out, self.gphoto, self.g_upper, self.g_lower)
        self.assert_on_device(self.g_upper, self.g_lower, self.g_plain)

    def test_erase_phone_thumbnails_with_case_collision(self):
        """A collision inside .thumbnails refuses erase too; --keep-phone-thumbnails leaves them alone."""
        os.remove(self.m_lower)  # no collision among the originals
        rel = os.path.relpath(self.m_plain, self.mtp)
        self.filed(self.mtp, "2026-11-13-mtp", {rel: ["nudity"]})
        tdir = os.path.join(self.mtp, "DCIM/.thumbnails")
        t_upper, t_lower = same_file_twice(os.path.join(tdir, "Thumb_1.jpg"), "thumb_1.jpg", 5, "2026:11:05 10:00:00")
        out = self.intake("erase", "2026-11-13-mtp", "--phone", self.mtp, "--yes", code=1)
        self.assert_refused(out, self.mtp, t_upper, t_lower)
        self.assert_on_device(self.m_plain, t_upper, t_lower)
        out = self.intake("erase", "2026-11-13-mtp", "--phone", self.mtp, "--yes", "--keep-phone-thumbnails")
        self.assertIn("phone 1;", out)
        self.assertFalse(os.path.exists(self.m_plain))
        self.assert_on_device(self.m_upper, t_upper, t_lower)

    # ------------------------------------------------------------------ trash
    def test_trash_with_phone_refused_on_gphoto2(self):
        self.filed(self.gphoto, "2026-11-13-cam")
        kept = [os.path.join(r, f) for r, _, fs in os.walk(self.lib) for f in fs if f.lower() == "img_0001.jpg"]
        self.assertEqual(len(kept), 1, "the case twin is an exact duplicate: filed once")
        out = self.intake("trash", "2026-11-13-cam", kept[0], "--phone", self.gphoto, code=1)
        self.assert_refused(out, self.gphoto, self.g_upper, self.g_lower)
        self.assertTrue(os.path.isfile(kept[0]), "nothing trashed when a phone delete is refused")
        self.assertFalse(os.path.exists(os.path.join(self.state, "trashed.json")))
        self.assert_on_device(self.g_upper, self.g_lower, self.g_plain)

    def test_trash_with_phone_refused_on_mtp_across_batches(self):
        """No batch named: every batch's manifest is a known device path."""
        self.filed(self.mtp, "2026-11-13-mtp")
        kept = [os.path.join(r, f) for r, _, fs in os.walk(self.lib) for f in fs
                if f.lower() == "img_20261101_100000.jpg"]
        out = self.intake("trash", kept[0], "--phone", self.mtp, code=1)
        self.assert_refused(out, self.mtp, self.m_upper, self.m_lower)
        self.assert_on_device(self.m_upper, self.m_lower, self.m_plain)


class OneDeviceGate(unittest.TestCase):
    """Every device write is the one `gio remove` in device_remove; nothing renames or moves on a device."""

    def test_single_gio_call(self):
        calls = {}
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".py"):
                with open(os.path.join(HERE, name)) as f:
                    src = f.read()
                for m in re.finditer(r'\[\s*"gio"\s*,\s*"([^"]+)"', src):
                    fn = re.findall(r"^def (\w+)", src[:m.start()], re.M)[-1]
                    calls.setdefault((name, fn, m.group(1)), 0)
                    calls[(name, fn, m.group(1))] += 1
        self.assertEqual(calls, {("intake.py", "device_remove", "remove"): 1})


if __name__ == "__main__":
    unittest.main()
