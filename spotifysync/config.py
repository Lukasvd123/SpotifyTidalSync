"""Paths, tunables, settings persistence and logging setup."""

import json
import logging
import os
import platform
import queue
import sys

APP_NAME = "SpotifyTidalSync"
KEYRING_SERVICE = "SpotifyTidalSync"
UI_FONT = "Segoe UI" if platform.system() == "Windows" else "DejaVu Sans"

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"


def resource_path(relative_path):
    """Resolve a bundled asset, both in dev and inside a PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, relative_path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, relative_path)


def _appdata_root():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif IS_MACOS:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


APPDATA_DIR = _appdata_root()
os.makedirs(APPDATA_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(APPDATA_DIR, "settings.json")
MAPPINGS_FILE = os.path.join(APPDATA_DIR, "mappings.json")
LOG_FILE = os.path.join(APPDATA_DIR, "debug.log")

# --- Sync tunables ---------------------------------------------------------
# The loop is cheap (VLC state reads); only the Spotify snapshot is rate
# limited, so we can poll fast enough to catch boundaries crisply.
POLL_INTERVAL = 0.35

# Minimum gap between Spotify Web API playback snapshots. The old code made one
# call per loop tick (~3600/hour) purely to re-detect DJ mode.
API_SNAPSHOT_INTERVAL = 1.0

# A source track change is a natural end (or a crossfade flipping the metadata
# early) when the previous track had at most this much left. Measured against
# the *source's own* duration, which is exact, rather than guessed from how much
# of our Tidal master remains.
NATURAL_END_MS = 15000

# Once the source has moved on for a natural end, let our audio finish rather
# than cutting it. We only hold the source back while more than this remains.
HANDOFF_MS = 1500

# Only adopt the source's position when starting a track if it is genuinely
# mid-song. Below this we always start at 0 and rewind the (muted) source.
MIDSONG_START_MS = 20000

# A source position that deviates from its own expected progression by more
# than this means the user seeked deliberately, so mirror it onto VLC.
SEEK_EPSILON_MS = 3500

# After we seek the source ourselves, ignore seek detection briefly so our own
# correction is not mistaken for a user seek.
SEEK_SUPPRESS_S = 2.5

# Spotify's AI DJ ("DJ X") streams from this playlist context. Its spoken
# segments do not resolve as tracks, so we hand audio back to Spotify for them.
DJ_CONTEXT_ID = "37i9dQZF1EYkqdzj48dyYq"

# --- Settings --------------------------------------------------------------
DEFAULT_SETTINGS = {
    "mute_spotify": True,
    "auto_favorite": False,
    "use_spotify_api": False,
    "last_device_id": None,
}


def _load_json(path):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                content = f.read().strip()
                return json.loads(content) if content else {}
    except Exception as e:
        logger.error(f"Load error {path}: {e}")
    return {}


def _save_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"Save error {path}: {e}")


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings.update(_load_json(SETTINGS_FILE))
    return settings


def save_setting(key, value):
    data = _load_json(SETTINGS_FILE)
    data[key] = value
    _save_json(SETTINGS_FILE, data)


# --- Logging ---------------------------------------------------------------
log_queue = queue.Queue()


class _QueueHandler(logging.Handler):
    """Feeds the in-app log viewer."""

    def emit(self, record):
        try:
            log_queue.put(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(level=logging.DEBUG):
    for noisy in ("requests", "urllib3", "tidalapi", "keyring", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # spotipy dumps a full HTTP traceback for every rejected command. Spotify
    # answers 403 "Restriction violated" for a pause or resume at a track
    # boundary, which happens twice per track and is handled by falling back to
    # the OS media key, so its own logging is pure noise.
    logging.getLogger("spotipy").setLevel(logging.CRITICAL)

    handlers = [logging.StreamHandler(sys.stdout), _QueueHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, mode="w"))
    except OSError:
        pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


logger = logging.getLogger("SpotifySync")
