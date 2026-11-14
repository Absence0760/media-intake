#!/usr/bin/env python3
"""Tests for bursts.py: burst grouping on synthetic records (no file is read).

  python3 -I -m unittest discover -s tests -p test_bursts.py
"""
import datetime, json, os, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_intake")
sys.path.insert(0, HERE)
import bursts  # noqa: E402

T0 = datetime.datetime(2026, 11, 8, 7, 15, 0)


def rec(name, secs, device="cam-a", dhash=0, **kw):
    return dict(path=f"/x/DCIM/Camera/{name}", datetime=(T0 + datetime.timedelta(seconds=secs)).isoformat(),
                device=device, dhash=dhash, **kw)


def names(gs):
    return [[os.path.basename(r["path"]) for r in g] for g in gs]


class BurstsTest(unittest.TestCase):
    def test_consecutive_shots_are_one_group(self):
        rs = [rec("a.jpg", 0), rec("b.jpg", 0.3), rec("c.jpg", 0.6), rec("d.jpg", 20)]
        self.assertEqual(names(bursts.groups(rs)), [["a.jpg", "b.jpg", "c.jpg"]])

    def test_singletons_are_not_groups(self):
        rs = [rec("a.jpg", 0), rec("b.jpg", 10), rec("c.jpg", 30)]
        self.assertEqual(bursts.groups(rs), [])
        self.assertEqual(bursts.groups([]), [])
        self.assertEqual(bursts.groups([rec("a.jpg", 0)]), [])

    def test_gap_boundary_is_inclusive(self):
        g = bursts.GAP.total_seconds()
        self.assertEqual(names(bursts.groups([rec("a.jpg", 0), rec("b.jpg", g)])), [["a.jpg", "b.jpg"]])
        self.assertEqual(bursts.groups([rec("a.jpg", 0), rec("b.jpg", g + 0.001)]), [])

    def test_gap_is_between_neighbours_not_from_the_first(self):
        g = bursts.GAP.total_seconds()
        rs = [rec(f"{i}.jpg", i * g) for i in range(5)]  # spans 4 GAPs, each step exactly one
        self.assertEqual(len(bursts.groups(rs)[0]), 5)

    def test_a_long_pause_splits_two_bursts(self):
        rs = [rec("a.jpg", 0), rec("b.jpg", 1), rec("c.jpg", 10), rec("d.jpg", 11)]
        self.assertEqual(names(bursts.groups(rs)), [["a.jpg", "b.jpg"], ["c.jpg", "d.jpg"]])

    def test_input_order_does_not_matter(self):
        rs = [rec("c.jpg", 1), rec("a.jpg", 0), rec("b.jpg", 0.5)]
        self.assertEqual(names(bursts.groups(rs)), [["a.jpg", "b.jpg", "c.jpg"]])

    def test_mixed_devices_never_share_a_group(self):
        rs = [rec("a1.jpg", 0, "cam-a"), rec("b1.jpg", 0.2, "cam-b"), rec("a2.jpg", 0.4, "cam-a"),
              rec("b2.jpg", 0.6, "cam-b"), rec("c1.jpg", 0.8, "cam-c")]
        self.assertEqual(sorted(names(bursts.groups(rs))), [["a1.jpg", "a2.jpg"], ["b1.jpg", "b2.jpg"]])

    def test_unknown_device_groups_only_with_unknown(self):
        rs = [rec("u1.jpg", 0, None), rec("k1.jpg", 0.5, "cam-a"), rec("u2.jpg", 1, "")]
        self.assertEqual(names(bursts.groups(rs)), [["u1.jpg", "u2.jpg"]])

    def test_dhash_confirms_or_splits(self):
        near = 0b1111  # 4 bits from 0
        far = (1 << 40) - 1  # 40 bits from 0
        self.assertEqual(len(bursts.groups([rec("a.jpg", 0, dhash=0), rec("b.jpg", 0.5, dhash=near)])), 1)
        self.assertEqual(bursts.groups([rec("a.jpg", 0, dhash=0), rec("b.jpg", 0.5, dhash=far)]), [])
        edge = (1 << bursts.DHASH_MAX) - 1
        self.assertEqual(len(bursts.groups([rec("a.jpg", 0, dhash=0), rec("b.jpg", 0.5, dhash=edge)])), 1)
        self.assertEqual(bursts.groups([rec("a.jpg", 0, dhash=0), rec("b.jpg", 0.5, dhash=edge << 1 | 1)]), [])
        # No dHash on one side (a format Pillow can't read): the clock alone decides.
        self.assertEqual(len(bursts.groups([rec("a.jpg", 0, dhash=None), rec("b.jpg", 0.5, dhash=far)])), 1)

    def test_filename_markers_win_over_the_clock(self):
        rs = [rec("IMG_20261108_071500_BURST001_COVER.jpg", 0, dhash=0),
              rec("IMG_20261108_071500_BURST002.jpg", 5, dhash=(1 << 40) - 1),  # past GAP, far dHash
              rec("IMG_20261108_071500_BURST003.jpg", 9)]
        self.assertEqual(len(bursts.groups(rs)[0]), 3)
        px = [rec("00000IMG_00000_BURST20261108071500123_COVER.jpg", 0),
              rec("00001IMG_00001_BURST20261108071500123.jpg", 4)]
        self.assertEqual(len(bursts.groups(px)), 1)

    def test_different_markers_are_never_one_burst(self):
        rs = [rec("IMG_1000_BURST001.jpg", 0), rec("IMG_2000_BURST001.jpg", 0.5)]
        self.assertEqual(bursts.groups(rs), [])
        rs = [rec("a.jpg", 0, burst_id="AAAA-1"), rec("b.jpg", 0.5, burst_id="BBBB-2")]
        self.assertEqual(bursts.groups(rs), [])

    def test_burst_id_groups_and_is_case_insensitive(self):
        rs = [rec("a.jpg", 0, burst_id="ABCD-1234"), rec("b.jpg", 30, burst_id="abcd-1234")]
        self.assertEqual(names(bursts.groups(rs)), [["a.jpg", "b.jpg"]])

    def test_marker_on_one_side_falls_back_to_the_clock(self):
        self.assertEqual(len(bursts.groups([rec("IMG_1_BURST001.jpg", 0), rec("b.jpg", 1)])), 1)
        self.assertEqual(bursts.groups([rec("IMG_1_BURST001.jpg", 0), rec("b.jpg", 5)]), [])

    def test_videos_and_undated_records_are_never_grouped(self):
        rs = [rec("a.mp4", 0), rec("b.MOV", 0.5), rec("c.jpg", 1)]
        rs[2]["datetime"] = None
        self.assertEqual(bursts.groups(rs), [])
        bad = [dict(path="/x/a.jpg", datetime="not a date", device="d"), dict(path="/x/b.jpg", datetime=7)]
        self.assertEqual(bursts.groups(bad), [])

    def test_datetime_objects_and_aware_strings(self):
        rs = [dict(path="/x/a.jpg", datetime=T0, device="d"),
              dict(path="/x/b.jpg", datetime=(T0 + datetime.timedelta(seconds=1)).isoformat() + "+02:00", device="d")]
        self.assertEqual(len(bursts.groups(rs)), 1)

    def test_extra_keys_pass_through_and_index(self):
        rs = [dict(rec("a.jpg", 0), id=7), dict(rec("x.jpg", 50), id=8), dict(rec("b.jpg", 1), id=9)]
        g = bursts.groups(rs)
        self.assertEqual([r["id"] for r in g[0]], [7, 9])
        self.assertIs(g[0][0], rs[0])
        self.assertEqual(bursts.index(rs), {0: 0, 2: 0})

    def test_runs_as_a_script(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "records.json")
            with open(p, "w") as f:
                json.dump([dict(rec("a.jpg", 0), id="a"), dict(rec("b.jpg", 1), id="b"), rec("c.jpg", 40)], f)
            r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "bursts.py"), p],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [{"count": 2, "members": ["a", "b"]}])


if __name__ == "__main__":
    unittest.main()
