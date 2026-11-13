#!/usr/bin/env python3
"""config.py and plan's --event routing, on a throwaway config.

  python3 -I -m unittest discover -s tests -p test_config.py
"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (hermetic config)
import config  # noqa: E402


class Config(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mi-config-")
        self.p = os.path.join(self.d, "c.toml")
        self.saved = {k: os.environ.pop(k, None) for k in ("MEDIA_INTAKE_STATE", "PHOTO_LIBRARY", "PERSONAL_FOOTAGE")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is not None:
                os.environ[k] = v

    def write(self, text):
        with open(self.p, "w") as f:
            f.write(text)
        return config.load(self.p)

    def test_defaults_when_absent(self):
        c = config.load(os.path.join(self.d, "missing.toml"))
        home = os.path.expanduser("~")
        self.assertEqual(c["library"], os.path.join(home, "Pictures/library"))
        self.assertEqual(c["videos"], os.path.join(home, "Videos/personal"))
        self.assertEqual(c["state"], os.path.join(home, ".local/share/media-intake"))
        self.assertEqual((c["projects"], c["phone_extra"], c["backup_markers"], c["queue_wrapper"]), ({}, {}, [], []))

    def test_env_wins(self):
        os.environ["PHOTO_LIBRARY"] = os.path.join(self.d, "lib")
        c = self.write('library = "/elsewhere"\n')
        self.assertEqual(c["library"], os.path.join(self.d, "lib"))

    def test_projects_and_routing(self):
        c = self.write('[projects.club]\nname = "Run Club"\nfolder = "~/club"\nevents = ["*-race", "track-day"]\n'
                       '[projects.other]\nfolder = "/srv/o"\nprotect = false\n')
        self.assertEqual(c["projects"]["club"]["folder"], os.path.expanduser("~/club"))
        self.assertEqual(config.project_for_event("park-race", c)["key"], "club")
        self.assertEqual(config.project_for_event("track-day", c)["name"], "Run Club")
        self.assertIsNone(config.project_for_event("birthday", c))
        self.assertEqual(config.protected_tags(c), {"Project|Run Club"})
        self.assertEqual(config.library_roots(c)[2:], [os.path.expanduser("~/club"), "/srv/o"])

    def test_bad_files_refused(self):
        for text in ('nope = 1\n', '[projects.Bad]\nfolder = "/x"\n', '[projects.a]\nname = "a"\n',
                     '[projects.a]\nname = "Personal"\nfolder = "/x"\n',
                     '[phone.extra_folders]\n"../up" = "App"\n', '[queue]\nwrapper = "heavy"\n', 'library = [\n'):
            with self.assertRaises(config.ConfigError, msg=text):
                self.write(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
