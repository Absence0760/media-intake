"""Shared test setup: a hermetic config, so no test reads your real config.toml.

Importing this sets MEDIA_INTAKE_CONFIG (inherited by every subprocess the
tests start) to a temp config with one demo project and two messaging-app
folders in the phone allowlist. write_config() makes a per-test variant whose
project folder sits in that test's throwaway HOME.
"""
import atexit, os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "media_intake")
if PKG not in sys.path:
    sys.path.insert(0, PKG)

EXTRA = ("Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images",
         "Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video")
BACKUP_MARKERS = ("demo-footage", "personal", "photos")


def config_text(project_folder):
    extra = "".join(f'"{x}" = "WhatsApp"\n' for x in EXTRA)
    markers = ", ".join(f'"{m}"' for m in BACKUP_MARKERS)
    return (f"[phone.extra_folders]\n{extra}\n[drive]\nbackup_markers = [{markers}]\n\n"
            f'[projects.demo]\nname = "Demo"\nfolder = "{project_folder}"\nevents = ["*-shoot"]\n')


def write_config(footage, path=None):
    """A config whose demo project folder is <footage>/originals. Returns its path."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(footage)),
                                f"media-intake-{os.path.basename(footage)}.toml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(config_text(os.path.join(os.path.abspath(footage), "originals")))
    return path


_d = tempfile.mkdtemp(prefix="media-intake-test-config-")
atexit.register(shutil.rmtree, _d, True)
os.environ["MEDIA_INTAKE_CONFIG"] = write_config(os.path.join(_d, "no-such-home", "demo-footage"),
                                                 os.path.join(_d, "config.toml"))
os.environ.pop("XDG_CONFIG_HOME", None)
