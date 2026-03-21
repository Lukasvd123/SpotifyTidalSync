import time
import threading
import sys
import os
import shutil
import logging
import webbrowser
import json
import queue
import subprocess
import platform
import re
import unicodedata
import asyncio
from datetime import timedelta, datetime
from io import BytesIO

# GUI Imports — import PIL before tkinter to avoid _tkinter_finder issues
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog
import requests

# Conditional: Spotify API (optional)
SPOTIPY_AVAILABLE = False
try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    from spotipy.cache_handler import CacheHandler
    SPOTIPY_AVAILABLE = True
except ImportError:
    pass

# Audio / API Imports
import tidalapi
import vlc

# Secure Storage
import keyring

# Windows per-app audio control (optional)
PYCAW_AVAILABLE = False
if platform.system() == "Windows":
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        import comtypes
        from comtypes import CLSCTX_ALL
        PYCAW_AVAILABLE = True
    except ImportError:
        pass

# Windows SMTC media detection (optional)
SMTC_AVAILABLE = False
if platform.system() == "Windows":
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
        )
        SMTC_AVAILABLE = True
    except ImportError:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as SessionManager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
            )
            SMTC_AVAILABLE = True
        except ImportError:
            pass

# --- RESOURCE HELPER ---
def get_resource_path(relative_path):
    """Get path to resource, works for dev and PyInstaller bundled exe."""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

# --- PATH CONFIGURATION ---
APP_NAME = "SpotifyTidalSync"
UI_FONT = "Segoe UI" if platform.system() == "Windows" else "DejaVu Sans"
KEYRING_SERVICE = "SpotifyTidalSync"

APPDATA_DIR = os.path.join(os.environ['APPDATA'], APP_NAME)

if not os.path.exists(APPDATA_DIR):
    os.makedirs(APPDATA_DIR)

SETTINGS_FILE = os.path.join(APPDATA_DIR, "settings.json")
MAPPINGS_FILE = os.path.join(APPDATA_DIR, "mappings.json")
LOG_FILE = os.path.join(APPDATA_DIR, "debug.log")

REFRESH_RATE = 1.0

# --- COMMUNITY CORRECTIONS ---
WORKER_URL = "https://correction-worker.lukas-van-dee.workers.dev"
GITHUB_MAPPINGS_URL = "https://raw.githubusercontent.com/Lukasvd123/SpotifyTidalSync/main/mappings.json"
APP_VERSION = "0.04"

# --- CLEAN LOGGING SETUP ---
log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_queue.put(msg)
        except Exception:
            self.handleError(record)

# Filter out verbose third-party logs
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
if SPOTIPY_AVAILABLE:
    logging.getLogger("spotipy").setLevel(logging.WARNING)
logging.getLogger("tidalapi").setLevel(logging.WARNING)
logging.getLogger("keyring").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        QueueHandler(),
        logging.FileHandler(LOG_FILE, mode='w')
    ]
)
logger = logging.getLogger("SyncApp")
logger.setLevel(logging.DEBUG)

# --- CREDENTIAL MANAGEMENT (KEYRING) ---
def get_credentials():
    client_id = keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID")
    client_secret = keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET")
    return client_id, client_secret

# --- DATA PERSISTENCE ---
def load_json(filepath):
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
    except Exception as e:
        logger.error(f"Load error {filepath}: {e}")
    return {}

def save_json(filepath, data):
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Save error {filepath}: {e}")

# Mappings: key -> Tidal ID (key can be spotify ID or normalized "title|artist")
def load_mappings(): return load_json(MAPPINGS_FILE)
def save_mapping(key, tidal_id):
    data = load_mappings()
    data[key] = tidal_id
    save_json(MAPPINGS_FILE, data)
    logger.info(f"Mapping saved: {key} -> {tidal_id}")

# Settings
def load_settings(): return load_json(SETTINGS_FILE)
def save_setting(key, value):
    data = load_settings()
    data[key] = value
    save_json(SETTINGS_FILE, data)

# --- NORMALIZED MAPPING KEYS ---
def normalize_text(text):
    """Lowercase, strip accents, remove special chars for fuzzy matching."""
    if not text:
        return ""
    text = text.lower().strip()
    # Strip accents
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    # Remove special chars except spaces
    text = re.sub(r'[^a-z0-9 ]', '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def make_mapping_key(title, artist):
    """Create a normalized mapping key from title and artist."""
    return f"{normalize_text(title)}|{normalize_text(artist)}"

# --- SECURE TOKEN STORAGE ---
if SPOTIPY_AVAILABLE:
    class KeyringCacheHandler(CacheHandler):
        def __init__(self, username_key="spotify_token"):
            self.username_key = username_key

        def get_cached_token(self):
            try:
                token_string = keyring.get_password(KEYRING_SERVICE, self.username_key)
                if token_string:
                    return json.loads(token_string)
            except Exception as e:
                logger.warning(f"Keyring read error (Spotify): {e}")
            return None

        def save_token_to_cache(self, token_info):
            try:
                keyring.set_password(KEYRING_SERVICE, self.username_key, json.dumps(token_info))
            except Exception as e:
                logger.error(f"Keyring write error (Spotify): {e}")

def get_tidal_quality():
    try:
        if not hasattr(tidalapi, 'Quality'): return None
        Q = tidalapi.Quality
        # tidalapi 0.8.x: hi_res_lossless, high_lossless, low_320k, low_96k
        # older versions: LOSSLESS, HIGH, LOW, etc.
        options = ['hi_res_lossless', 'high_lossless', 'low_320k', 'lossless', 'LOSSLESS', 'high', 'HIGH']
        for opt in options:
            if hasattr(Q, opt): return getattr(Q, opt)
        return None
    except: return None

PREFERRED_QUALITY = get_tidal_quality()

# --- BROWSER HELPER ---
def safe_open_browser(url, parent_window=None):
    """Open URL in browser. If it fails, show the URL to the user."""
    try:
        success = webbrowser.open(url)
        if not success:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        logger.warning(f"Could not open browser automatically: {e}")
        msg = (
            "Could not open your browser automatically.\n\n"
            "Please open this URL manually:\n\n"
            f"{url}"
        )
        if parent_window:
            try:
                parent_window.clipboard_clear()
                parent_window.clipboard_append(url)
                msg += "\n\n(URL has been copied to your clipboard)"
            except:
                pass
            messagebox.showinfo("Browser Not Available", msg)
        else:
            print(f"\n{'='*60}")
            print("BROWSER COULD NOT BE OPENED")
            print(f"{'='*60}")
            print(f"Please open this URL manually:\n{url}")
            print(f"{'='*60}\n")

# --- COMMUNITY MAPPINGS SYNC ---
def sync_community_mappings():
    """Fetch community corrections from GitHub and merge new entries into local mappings."""
    try:
        logger.info("Fetching community mappings from GitHub...")
        resp = requests.get(GITHUB_MAPPINGS_URL, timeout=10)
        if resp.status_code == 200:
            community = resp.json()
            local = load_mappings()
            added = 0
            for key, tidal_id in community.items():
                if key not in local:
                    local[key] = tidal_id
                    added += 1
            if added > 0:
                save_json(MAPPINGS_FILE, local)
                logger.info(f"Community sync: fetched {added} new correction(s) from GitHub")
            else:
                logger.info(f"Community sync: up to date ({len(community)} mappings on server, {len(local)} local)")
        elif resp.status_code == 404:
            logger.info("Community sync: no community mappings file on GitHub yet")
        else:
            logger.warning(f"Community sync: GitHub returned HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Community sync failed: {e}")

def submit_correction(key, tidal_id):
    """Submit a track correction to the community worker."""
    try:
        data = {
            "old_value": key,
            "correct_value": str(tidal_id),
            "app_version": APP_VERSION
        }
        logger.info(f"Submitting correction to community server ({key} -> Tidal:{tidal_id})...")
        resp = requests.post(WORKER_URL, json=data, timeout=10)
        if resp.status_code == 200:
            logger.info("Community: correction submitted successfully")
        else:
            logger.warning(f"Community: server returned HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Community: could not submit correction - {e}")

# --- AUDIO MUTING ---
def mute_spotify_windows():
    """Mute Spotify.exe at the Windows audio mixer level using pycaw."""
    if not PYCAW_AVAILABLE:
        return False
    try:
        comtypes.CoInitialize()
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.name().lower() == "spotify.exe":
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    volume.SetMute(1, None)
                    return True
        finally:
            comtypes.CoUninitialize()
    except Exception as e:
        logger.debug(f"pycaw mute error: {e}")
    return False

def mute_spotify_linux():
    """Mute source app via pactl (works with PulseAudio and PipeWire-pulse).
    Carefully avoids muting our own VLC stream."""
    try:
        result = subprocess.run(
            ['pactl', 'list', 'sink-inputs'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode != 0:
            return False

        muted_any = False
        current_index = None
        is_vlc = False
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('Sink Input #'):
                current_index = line.split('#')[1]
                is_vlc = False
            elif current_index and 'application.name' in line:
                val = line.split('=', 1)[-1].strip().strip('"').lower()
                if 'vlc' in val or 'spotifytidalsync' in val:
                    is_vlc = True
                    current_index = None  # Skip our own stream
                elif 'spotify' in val and not is_vlc:
                    subprocess.run(
                        ['pactl', 'set-sink-input-mute', current_index, '1'],
                        capture_output=True, timeout=2
                    )
                    muted_any = True
                    current_index = None
            elif current_index and 'application.process.binary' in line:
                val = line.split('=', 1)[-1].strip().strip('"').lower()
                if 'spotify' in val and not is_vlc:
                    subprocess.run(
                        ['pactl', 'set-sink-input-mute', current_index, '1'],
                        capture_output=True, timeout=2
                    )
                    muted_any = True
                    current_index = None
        return muted_any
    except FileNotFoundError:
        logger.debug("pactl not found — cannot mute source app on Linux")
        return False
    except Exception as e:
        logger.debug(f"Linux mute error: {e}")
        return False

# --- MEDIA INFO & DETECTION ---

# Apps whose media sessions we ignore (our own playback)
IGNORED_APPS = {'vlc', 'vlc media player', 'tidal', 'spotifytidalsync'}

class MediaInfo:
    """Holds detected media information from OS-level media detection."""
    def __init__(self, title="", artist="", album="", playback_status="unknown",
                 source_app="", image_url=None, position_ms=-1):
        self.title = title or ""
        self.artist = artist or ""
        self.album = album or ""
        self.playback_status = playback_status  # "playing", "paused", "stopped", "unknown"
        self.source_app = source_app
        self.image_url = image_url
        self.position_ms = position_ms  # -1 = unknown, >=0 = position in ms

    @property
    def mapping_key(self):
        return make_mapping_key(self.title, self.artist)

    @property
    def is_playing(self):
        return self.playback_status == "playing"

    def matches(self, other):
        """Check if this represents the same track as another MediaInfo."""
        if other is None:
            return False
        return self.mapping_key == other.mapping_key

    def __repr__(self):
        return f"MediaInfo('{self.title}' by '{self.artist}' [{self.source_app}] {self.playback_status})"


class MediaDetector:
    """Detects currently playing media via Windows SMTC."""

    def __init__(self):
        self._last_error_time = 0
        self._error_throttle = 30  # seconds between repeated error logs
        self._loop = None
        self._loop_thread = None
        if SMTC_AVAILABLE:
            self._start_event_loop()

    def _start_event_loop(self):
        """Start a dedicated asyncio event loop for SMTC calls."""
        def _run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True)
        self._loop_thread.start()
        # Give the loop a moment to start
        time.sleep(0.1)

    def _run_async(self, coro):
        """Run an async coroutine on our dedicated event loop."""
        if self._loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=3.0)
        except Exception:
            return None

    def get_current_media(self, preferred_source="spotify"):
        """Get currently playing media from OS. Returns MediaInfo or None."""
        if SMTC_AVAILABLE:
            return self._get_windows_media(preferred_source)
        else:
            self._log_error_throttled("No media detection available (install winsdk)")
            return None

    def _get_windows_media(self, preferred_source):
        """Get media info via Windows SMTC."""
        try:
            return self._run_async(self._async_get_windows_media(preferred_source))
        except Exception as e:
            self._log_error_throttled(f"SMTC error: {e}")
            return None

    async def _async_get_windows_media(self, preferred_source):
        """Async implementation of Windows SMTC media detection."""
        try:
            manager = await SessionManager.request_async()
            sessions = manager.get_sessions()

            if not sessions:
                return None

            best_session = None
            preferred_session = None

            for session in sessions:
                app_id = self._identify_app(session.source_app_user_model_id)
                if app_id.lower() in IGNORED_APPS:
                    continue

                info = session.get_playback_info()
                if info is None:
                    continue

                status = info.playback_status

                if app_id.lower() == preferred_source.lower():
                    preferred_session = session
                    break
                elif status == PlaybackStatus.PLAYING and best_session is None:
                    best_session = session

            session = preferred_session or best_session
            if session is None:
                # Fall back to first non-ignored session
                for s in sessions:
                    app_id = self._identify_app(s.source_app_user_model_id)
                    if app_id.lower() not in IGNORED_APPS:
                        session = s
                        break

            if session is None:
                return None

            media_props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            app_id = self._identify_app(session.source_app_user_model_id)

            status_str = "unknown"
            if info:
                status_map = {
                    PlaybackStatus.PLAYING: "playing",
                    PlaybackStatus.PAUSED: "paused",
                    PlaybackStatus.STOPPED: "stopped",
                    PlaybackStatus.CLOSED: "stopped",
                }
                status_str = status_map.get(info.playback_status, "unknown")

            title = media_props.title or ""
            artist = media_props.artist or ""
            album = media_props.album_title or ""

            # Read playback position from timeline properties
            position_ms = -1
            try:
                timeline = session.get_timeline_properties()
                if timeline:
                    position_ms = int(timeline.position.total_seconds() * 1000)
            except Exception:
                pass

            if not title:
                return None

            return MediaInfo(
                title=title,
                artist=artist,
                album=album,
                playback_status=status_str,
                source_app=app_id,
                position_ms=position_ms,
            )
        except Exception as e:
            self._log_error_throttled(f"SMTC async error: {e}")
            return None

    def _get_linux_media(self, preferred_source):
        """Get media info via Linux D-Bus MPRIS."""
        try:
            bus = dbus.SessionBus()
            services = [s for s in bus.list_names() if s.startswith('org.mpris.MediaPlayer2.')]

            if not services:
                return None

            best_service = None
            preferred_service = None

            for service_name in services:
                app_name = service_name.replace('org.mpris.MediaPlayer2.', '').split('.')[0].lower()
                if app_name in IGNORED_APPS:
                    continue

                if app_name == preferred_source.lower():
                    preferred_service = service_name
                    break

                # Check if playing
                try:
                    proxy = bus.get_object(service_name, '/org/mpris/MediaPlayer2')
                    props = dbus.Interface(proxy, 'org.freedesktop.DBus.Properties')
                    status = str(props.Get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus'))
                    if status == 'Playing' and best_service is None:
                        best_service = service_name
                except:
                    continue

            service_name = preferred_service or best_service
            if service_name is None:
                # Fall back to first non-ignored
                for s in services:
                    app_name = s.replace('org.mpris.MediaPlayer2.', '').split('.')[0].lower()
                    if app_name not in IGNORED_APPS:
                        service_name = s
                        break

            if service_name is None:
                return None

            proxy = bus.get_object(service_name, '/org/mpris/MediaPlayer2')
            props = dbus.Interface(proxy, 'org.freedesktop.DBus.Properties')

            metadata = props.Get('org.mpris.MediaPlayer2.Player', 'Metadata')
            status = str(props.Get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus'))

            title = str(metadata.get('xesam:title', ''))
            artists = metadata.get('xesam:artist', [])
            artist = str(artists[0]) if artists else ''
            album = str(metadata.get('xesam:album', ''))

            # Read playback position (MPRIS Position is in microseconds)
            position_ms = -1
            try:
                pos_us = props.Get('org.mpris.MediaPlayer2.Player', 'Position')
                position_ms = int(pos_us) // 1000
            except Exception:
                pass

            app_name = service_name.replace('org.mpris.MediaPlayer2.', '').split('.')[0]

            status_map = {'Playing': 'playing', 'Paused': 'paused', 'Stopped': 'stopped'}
            status_str = status_map.get(status, 'unknown')

            if not title:
                return None

            return MediaInfo(
                title=title,
                artist=artist,
                album=album,
                playback_status=status_str,
                source_app=app_name,
                position_ms=position_ms,
            )
        except Exception as e:
            self._log_error_throttled(f"MPRIS error: {e}")
            return None

    def send_control(self, command):
        """Send media control command via OS media controls.
        Commands: 'play', 'pause', 'play_pause', 'next', 'previous'"""
        if SMTC_AVAILABLE:
            self._run_async(self._async_send_control(command))

    async def _async_send_control(self, command):
        """Send control via SMTC."""
        try:
            manager = await SessionManager.request_async()
            session = manager.get_current_session()
            if session is None:
                return
            cmd_map = {
                'play': session.try_play_async,
                'pause': session.try_pause_async,
                'play_pause': session.try_toggle_play_pause_async,
                'next': session.try_skip_next_async,
                'previous': session.try_skip_previous_async,
            }
            func = cmd_map.get(command)
            if func:
                await func()
        except Exception as e:
            logger.debug(f"SMTC control error: {e}")

    def _identify_app(self, raw_id):
        """Map OS app IDs to known friendly names."""
        if not raw_id:
            return "unknown"
        raw_lower = raw_id.lower()
        known = {
            'spotify': 'Spotify',
            'tidal': 'Tidal',
            'vlc': 'VLC',
            'firefox': 'Firefox',
            'chrome': 'Chrome',
            'msedge': 'Edge',
            'musicbee': 'MusicBee',
            'foobar': 'foobar2000',
            'winamp': 'Winamp',
            'itunes': 'iTunes',
            'apple music': 'Apple Music',
        }
        for key, name in known.items():
            if key in raw_lower:
                return name
        # Return cleaned raw ID
        return raw_id.split('.')[-1].split('!')[-1] or raw_id

    def _log_error_throttled(self, msg):
        """Log an error at most once per throttle period."""
        now = time.time()
        if now - self._last_error_time > self._error_throttle:
            logger.warning(msg)
            self._last_error_time = now


def fetch_album_art_url(title, artist, album=""):
    """Fetch album art URL from iTunes Search API (free, no auth required)."""
    try:
        query = f"{title} {artist}".strip()
        if not query:
            return None
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": query, "media": "music", "entity": "song", "limit": 3},
            timeout=5
        )
        if resp.status_code == 200:
            results = resp.json().get('results', [])
            if results:
                # Get highest resolution art (replace 100x100 with 600x600)
                art_url = results[0].get('artworkUrl100', '')
                if art_url:
                    return art_url.replace('100x100bb', '600x600bb')
    except Exception:
        pass
    return None


# --- PREFETCH CACHE ---
class PrefetchCache:
    """Pre-fetches upcoming Tidal tracks from the Spotify queue to reduce latency on track changes."""

    def __init__(self):
        self._cache = {}  # key -> tidal_track (key = mapping_key or spotify_id)
        self._lock = threading.Lock()
        self._prefetching = False

    def get(self, key):
        with self._lock:
            return self._cache.get(key)

    def put(self, key, tidal_track):
        with self._lock:
            self._cache[key] = tidal_track

    def has(self, key):
        with self._lock:
            return key in self._cache

    def clear_old(self, keep_keys):
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k in keep_keys}

    def start_prefetch(self, sp_client, search_func, current_key):
        """Start prefetching. Returns immediately if sp_client is None (no Spotify API)."""
        if sp_client is None:
            return
        if self._prefetching:
            return

        def _prefetch():
            self._prefetching = True
            try:
                queue_data = sp_client.queue()
                upcoming = queue_data.get('queue', [])[:5]

                keep_keys = {current_key}
                for track in upcoming:
                    key = make_mapping_key(track['name'], track['artists'][0]['name'])
                    keep_keys.add(key)
                    keep_keys.add(track['id'])

                self.clear_old(keep_keys)

                for i, track in enumerate(upcoming):
                    key = make_mapping_key(track['name'], track['artists'][0]['name'])
                    if not self.has(key) and not self.has(track['id']):
                        tidal_match = search_func(
                            title=track['name'],
                            artist=track['artists'][0]['name'],
                            duration_ms=track.get('duration_ms', 0),
                            spotify_id=track['id']
                        )
                        if tidal_match:
                            self.put(key, tidal_match)
                            self.put(track['id'], tidal_match)
                            logger.info(f"Prefetched: {track['name']}")
                        else:
                            logger.debug(f"Prefetch: no match for '{track['name']}'")
                        # Rate limiting between searches
                        if i < len(upcoming) - 1:
                            time.sleep(2.0)
            except Exception as e:
                logger.warning(f"Prefetch error: {e}")
            finally:
                self._prefetching = False

        threading.Thread(target=_prefetch, daemon=True).start()

# --- AUDIO PLAYER ---
def _detect_linux_audio_backend():
    """Detect best available VLC audio output on Linux.

    Prefer 'pulse' over native 'pipewire' — VLC's PipeWire plugin has a bug
    where audio_set_volume segfaults before a stream is active. The PulseAudio
    compat layer (pipewire-pulse) still routes through PipeWire for hi-res
    sample rate switching, without the crash.
    """
    for backend, check_cmd in [
        ('pulse', 'pipewire-pulse'),
        ('pulse', 'pulseaudio'),
        ('alsa', 'aplay'),
    ]:
        if shutil.which(check_cmd):
            return backend
    return None

def _fix_vlc_env():
    """Fix VLC plugin discovery when running from a PyInstaller bundle.

    PyInstaller overrides LD_LIBRARY_PATH which prevents VLC from finding
    its plugins. Restore the original path and set VLC_PLUGIN_PATH.
    """
    orig_ld = os.environ.get('LD_LIBRARY_PATH_ORIG')
    if orig_ld is not None:
        os.environ['LD_LIBRARY_PATH'] = orig_ld
    elif getattr(sys, '_MEIPASS', None) and 'LD_LIBRARY_PATH' in os.environ:
        del os.environ['LD_LIBRARY_PATH']

    if 'VLC_PLUGIN_PATH' not in os.environ:
        for plugin_dir in ['/usr/lib64/vlc/plugins', '/usr/lib/vlc/plugins',
                           '/usr/lib/x86_64-linux-gnu/vlc/plugins',
                           '/usr/lib/aarch64-linux-gnu/vlc/plugins']:
            if os.path.isdir(plugin_dir):
                os.environ['VLC_PLUGIN_PATH'] = plugin_dir
                break

class AudioPlayer:
    def __init__(self):
        if platform.system() != "Windows":
            _fix_vlc_env()

        vlc_args = [
            '--no-video',
            '--verbose=-1',
            '--network-caching=1500',
            '--no-sout-audio',          # Disable audio stream output (not needed)
            '--gain=1.0',               # No software gain (preserve dynamic range)
            '--no-audio-time-stretch',  # Disable time-stretching for cleaner output
        ]
        if platform.system() == "Windows":
            vlc_args.append('--aout=mmdevice')
        else:
            backend = _detect_linux_audio_backend()
            if backend:
                vlc_args.append(f'--aout={backend}')
                logger.debug(f"VLC audio backend: {backend}")
            # Set role to "music" so PipeWire stream-restore doesn't apply
            # stale mute from VLC's default "video" role
            vlc_args.append('--role=music')
        self.instance = vlc.Instance(*vlc_args)
        self.player = self.instance.media_player_new()
        try: self.player.audio_set_volume(100)
        except: pass

        settings = load_settings()
        saved_device = settings.get("last_device_id")
        if saved_device:
            threading.Timer(1.0, lambda: self.set_device(saved_device)).start()
        logger.debug(f"AudioPlayer initialized with args: {vlc_args}")

    def get_audio_devices(self):
        devices = []
        try:
            mods = self.player.audio_output_device_enum()
            if mods:
                mod = mods
                count = 0
                while mod and count < 100:
                    mod = mod.contents
                    desc = mod.description.decode('utf-8', 'ignore') if mod.description else "Unknown"
                    dev_id = mod.device.decode('utf-8', 'ignore') if mod.device else None
                    if dev_id: devices.append((desc, dev_id))
                    mod = mod.next
                    count += 1
                vlc.libvlc_audio_output_device_list_release(mods)
        except Exception as e:
            logger.error(f"Error listing audio devices: {e}")
        return devices

    def set_device(self, device_id):
        try:
            self.player.audio_output_device_set(None, device_id)
            save_setting("last_device_id", device_id)
        except: pass

    def play_url(self, url):
        media = self.instance.media_new(url)
        self.player.set_media(media)
        self.player.play()
        # On Linux, unmute our VLC stream via pactl in case PipeWire
        # stream-restore remembered a muted state from a previous run
        if platform.system() == "Linux":
            threading.Timer(0.5, self._unmute_vlc_stream).start()

    def _unmute_vlc_stream(self):
        """Unmute our own VLC sink-input via pactl."""
        try:
            result = subprocess.run(
                ['pactl', 'list', 'sink-inputs'],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode != 0:
                return
            current_index = None
            for line in result.stdout.splitlines():
                line_s = line.strip()
                if line_s.startswith('Sink Input #'):
                    current_index = line_s.split('#')[1]
                elif current_index and 'application.name' in line_s:
                    val = line_s.split('=', 1)[-1].strip().strip('"').lower()
                    if 'vlc' in val:
                        subprocess.run(
                            ['pactl', 'set-sink-input-mute', current_index, '0'],
                            capture_output=True, timeout=2
                        )
                        logger.debug(f"Unmuted VLC sink-input #{current_index}")
                        return
                    current_index = None
        except Exception as e:
            logger.debug(f"VLC unmute error: {e}")

    def pause(self): self.player.set_pause(1)
    def resume(self): self.player.set_pause(0)
    def stop(self): self.player.stop()
    def is_playing(self): return self.player.is_playing()
    def get_time(self): return self.player.get_time()
    def get_duration(self): return self.player.get_length()

    def set_position(self, time_ms):
        """Seek to an absolute position in milliseconds."""
        self.player.set_time(int(time_ms))

    def seek_relative(self, delta_ms):
        """Seek forward (positive) or backward (negative) by delta milliseconds."""
        current = self.get_time()
        duration = self.get_duration()
        if duration <= 0:
            return
        new_pos = max(0, min(current + delta_ms, duration - 500))
        self.set_position(new_pos)

# --- SYNC MANAGER ---
class SyncManager:
    def __init__(self, gui_callback=None, request_manual_match_callback=None):
        self.sp = None  # Spotify API client (optional)
        self.tidal = None
        self.player = AudioPlayer()
        self.gui_callback = gui_callback
        self.request_manual_match = request_manual_match_callback
        self.running = True

        self.current_media = None  # MediaInfo from OS detection
        self.current_tidal_track = None
        self.current_quality = ""  # Current playback quality string
        self.status = "Initializing..."
        self.is_paused_waiting = False
        self.current_image_url = None
        self.user_skip_pending = False
        self.spotify_api_available = False

        self.media_detector = MediaDetector()
        self._art_cache = {}  # mapping_key -> art_url

        self.prefetch_cache = PrefetchCache()

        settings = load_settings()
        self.mute_spotify = settings.get("mute_spotify", True)
        self.auto_favorite = settings.get("auto_favorite", False)
        self.current_song_favorited = False
        self.waiting_for_user_selection = False
        self.share_corrections = settings.get("share_corrections", False)
        self._pending_next_media = None  # Track that source advanced to while VLC still playing
        self._last_track_change_time = 0  # Timestamp of last track switch (for skip cooldown)
        self._initial_sync_done = False   # Whether we synced position at start of track

    def login(self):
        """Login to Tidal (required) and optionally Spotify API."""
        if not self.login_tidal():
            return False
        settings = load_settings()
        if settings.get("use_spotify_api", False):
            self.login_spotify()
        else:
            logger.info("Spotify API: disabled (using OS media detection)")
            logger.info("  -> Enable in Settings > Spotify API for queue prefetch & seek sync")
        return True

    def login_spotify(self):
        """Optional Spotify API login. Sets self.sp if credentials are available."""
        if not SPOTIPY_AVAILABLE:
            logger.info("Spotify API: spotipy not installed (OS media detection will be used)")
            return

        client_id, client_secret = get_credentials()
        if not client_id or not client_secret:
            logger.info("Spotify API: no credentials configured (OS media detection will be used)")
            logger.info("  -> Enter credentials in Settings > Spotify API for queue prefetch & seek sync")
            return

        try:
            redirect_uri = 'http://127.0.0.1:8888/callback'
            cache_handler = KeyringCacheHandler("spotify_token")
            auth_manager = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope="user-read-playback-state user-modify-playback-state user-read-currently-playing",
                cache_handler=cache_handler
            )
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            user = self.sp.current_user()
            self.spotify_api_available = True
            logger.info(f"Spotify API: logged in as {user['display_name']} (prefetch & seek sync enabled)")
        except Exception as e:
            logger.warning(f"Spotify API login failed: {e}")
            logger.info("  -> Continuing with OS media detection only")
            self.sp = None
            self.spotify_api_available = False

    def login_tidal(self):
        """Required Tidal login."""
        try:
            if not PREFERRED_QUALITY: return False
            config = tidalapi.Config(quality=PREFERRED_QUALITY)
            self.tidal = tidalapi.Session(config=config)

            loaded = False
            try:
                session_json = keyring.get_password(KEYRING_SERVICE, "tidal_session")
                if session_json:
                    data = json.loads(session_json)

                    expiry = None
                    if data.get('expiry_time'):
                        expiry = datetime.fromtimestamp(data['expiry_time'])

                    self.tidal.load_oauth_session(
                        data.get('token_type', 'Bearer'),
                        data.get('access_token'),
                        data.get('refresh_token'),
                        expiry
                    )
                    loaded = True
            except Exception as e:
                logger.warning(f"Cached Tidal login invalid/expired: {e}")

            if loaded and self.tidal.check_login():
                 logger.info("Tidal: Logged in (Cached via Keyring)")
            else:
                 logger.info("Tidal: Login Required (No Cache or Expired)...")
                 auth_res = self.tidal.login_oauth()

                 link_login = auth_res[0] if isinstance(auth_res, (tuple, list)) else auth_res
                 url = getattr(link_login, 'verification_uri_complete', None) or getattr(link_login, 'verificationUriComplete', None)

                 if url:
                     if not url.startswith('http'): url = 'https://' + url
                     safe_open_browser(url)

                 if isinstance(auth_res, (tuple, list)) and len(auth_res) > 1:
                     auth_res[1].result()
                 else:
                     self.tidal.complete_login(link_login)

                 if self.tidal.check_login():
                     expiry_ts = None
                     if self.tidal.expiry_time:
                         expiry_ts = self.tidal.expiry_time.timestamp()

                     session_data = {
                         'token_type': self.tidal.token_type,
                         'access_token': self.tidal.access_token,
                         'refresh_token': self.tidal.refresh_token,
                         'expiry_time': expiry_ts
                     }
                     try:
                         keyring.set_password(KEYRING_SERVICE, "tidal_session", json.dumps(session_data))
                         logger.info("Tidal: Session Cached to Keyring")
                     except Exception as e:
                         logger.error(f"Failed to save Tidal session to Keyring: {e}")

            return True
        except Exception as e:
            logger.error(f"Tidal Login Failed: {e}")
            return False

    def get_tidal_track_by_id(self, tidal_id):
        try: return self.tidal.track(tidal_id)
        except: return None

    def search_tidal_match(self, title, artist, duration_ms=0, spotify_id=None):
        """Search for a Tidal match. Decoupled from Spotify track dict."""
        mappings = load_mappings()

        # 1. Check normalized key mapping
        norm_key = make_mapping_key(title, artist)
        if norm_key in mappings:
            t_track = self.get_tidal_track_by_id(mappings[norm_key])
            if t_track:
                logger.info(f"Found mapping (normalized) for '{title}'")
                return t_track

        # 2. Check Spotify ID mapping (backward compat)
        if spotify_id and spotify_id in mappings:
            t_track = self.get_tidal_track_by_id(mappings[spotify_id])
            if t_track:
                logger.info(f"Found mapping (Spotify ID) for '{title}'")
                return t_track

        # 3. Search Tidal
        try:
            clean_name = title.split('(')[0].split('-')[0].strip()
            query = f"{clean_name} {artist}"
            logger.info(f"Searching Tidal: '{query}'")
            search = self.tidal.search(query, models=[tidalapi.media.Track], limit=10)

            tracks = search.get('tracks', []) if isinstance(search, dict) else getattr(search, 'tracks', [])
            logger.debug(f"Tidal search returned {len(tracks)} results")

            best_match = None
            for t in tracks:
                if duration_ms > 0:
                    # Duration-based matching when we have duration info
                    if abs((t.duration * 1000) - duration_ms) <= 5000:
                        best_match = t
                        break
                else:
                    # Name-based matching when no duration (OS detection)
                    t_name = normalize_text(t.name)
                    t_artist = normalize_text(t.artist.name)
                    s_name = normalize_text(title)
                    s_artist = normalize_text(artist)
                    if t_name == s_name and t_artist == s_artist:
                        best_match = t
                        break
                    # Partial match: title contains and artist matches
                    if s_name in t_name and t_artist == s_artist:
                        best_match = t
                        break

            # Fallback: if no exact match, take first result with matching artist
            if not best_match:
                s_artist = normalize_text(artist)
                for t in tracks:
                    if normalize_text(t.artist.name) == s_artist:
                        best_match = t
                        break

            # Last resort: take first result if artist is close enough
            if not best_match and tracks:
                s_artist = normalize_text(artist)
                t_artist = normalize_text(tracks[0].artist.name)
                if s_artist in t_artist or t_artist in s_artist:
                    best_match = tracks[0]
                    logger.debug(f"Using fuzzy artist match: '{tracks[0].artist.name}'")

            if best_match:
                logger.debug(f"Matched: '{best_match.name}' by '{best_match.artist.name}'")
                return best_match

            if tracks:
                logger.warning(f"No match for '{title}' by '{artist}'. Top result: '{tracks[0].name}' by '{tracks[0].artist.name}'")
            else:
                logger.warning(f"No results at all for '{title}' by '{artist}'")
            return None

        except Exception as e:
            logger.warning(f"Search error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
        return None

    def check_and_refresh_session(self):
        if not self.tidal.check_login():
            logger.warning("Session expired. Attempting refresh...")
            if not self.tidal.check_login():
                logger.error("Session refresh failed. Re-login required.")
                return False
        return True

    def _get_stream_url(self, tidal_track, quality):
        """Get a playable URL for a track, trying get_url() first then get_stream().
        Skips MPEG-DASH (MPD) streams as VLC can't play individual DASH segments."""
        # Try get_url() first (non-PKCE sessions, returns direct URL)
        try:
            self.tidal.config.quality = quality
            url = tidal_track.get_url()
            if url:
                return url, str(quality).split(".")[-1].upper()
        except Exception as e:
            logger.debug(f"get_url() failed for quality {quality}: {e}")

        # Fallback: get_stream() + StreamManifest (works with PKCE too)
        try:
            self.tidal.config.quality = quality
            stream = tidal_track.get_stream()

            # Skip MPEG-DASH (MPD) streams - they have many segment URLs
            # that VLC can't play individually. Only use BTS (direct URL) streams.
            if hasattr(stream, 'is_mpd') and stream.is_mpd:
                logger.debug(f"Skipping MPD stream for quality {quality} (not directly playable)")
                return None, None

            manifest = stream.get_stream_manifest()
            if manifest.urls:
                q_str = str(stream.audio_quality).split(".")[-1].upper()
                logger.debug(f"Using stream manifest: {manifest.codecs} {q_str}")
                return manifest.urls[0], q_str
        except Exception as e:
            logger.debug(f"get_stream() failed for quality {quality}: {e}")

        return None, None

    def attempt_play_tidal(self, tidal_track, source_is_playing, source_position_ms=-1):
        if not self.check_and_refresh_session():
             self.status = "Session Expired"
             return False

        Q = tidalapi.Quality
        qualities_to_try = []
        try:
            # tidalapi 0.8.x: hi_res_lossless, high_lossless, low_320k, low_96k
            # older versions: lossless, high, low
            possible_attrs = ['hi_res_lossless', 'high_lossless', 'low_320k', 'low_96k',
                              'lossless', 'high', 'low']
            for attr in possible_attrs:
                if hasattr(Q, attr):
                    qualities_to_try.append(getattr(Q, attr))
        except: pass

        if not qualities_to_try: qualities_to_try = [PREFERRED_QUALITY]

        url = None
        used_quality = "Unknown"

        for quality in qualities_to_try:
            url, used_quality = self._get_stream_url(tidal_track, quality)
            if url:
                break

        if not url:
            logger.error(f"Could not stream '{tidal_track.name}' (tried all qualities)")
            self.player.stop()
            return False

        try:
            logger.debug(f"Playing URL: {url[:80]}...")
            self.player.play_url(url)
            time.sleep(0.5)

            # Verify VLC actually started
            vlc_state = self.player.player.get_state()
            vlc_playing = self.player.is_playing()
            logger.debug(f"VLC state after play: {vlc_state}, is_playing={vlc_playing}")

            if not vlc_playing:
                # Give it more time
                time.sleep(1.5)
                vlc_state = self.player.player.get_state()
                vlc_playing = self.player.is_playing()
                logger.debug(f"VLC state after extra wait: {vlc_state}, is_playing={vlc_playing}")

            # Determine where source is and sync VLC to match
            start_pos = self._get_source_position(source_position_ms)

            if start_pos > 2000:
                # Source is mid-song — seek VLC to match
                vlc_dur = self.player.get_duration()
                if vlc_dur > 0 and start_pos < vlc_dur - 500:
                    self.player.set_position(start_pos)
                    logger.info(f"Started mid-song: synced VLC to {start_pos}ms")
                elif vlc_dur > 0:
                    # Source position beyond our track length — clamp near end
                    logger.info(f"Source position {start_pos}ms exceeds our track ({vlc_dur}ms), starting from beginning")
            else:
                # Starting from beginning — if source was mid-song but near 0, that's fine
                pass

            self.current_quality = used_quality
            self.status = f"Playing: {tidal_track.name} [{used_quality}]"
            logger.info(f"Playing Tidal: {tidal_track.name} [{used_quality}]")
            if used_quality != str(PREFERRED_QUALITY).split(".")[-1].upper():
                logger.info(f"Note: requested {str(PREFERRED_QUALITY).split('.')[-1].upper()}, got {used_quality} (best available for this track)")

            # Start prefetching next tracks (only if Spotify API available)
            current_key = self.current_media.mapping_key if self.current_media else ""
            self.prefetch_cache.start_prefetch(
                self.sp, self.search_tidal_match, current_key
            )

            return True
        except Exception as e:
            logger.error(f"Tidal Playback Crash: {e}")
            self.player.stop()
            return False

    def _get_source_position(self, hint_position_ms=-1):
        """Get the source app's current playback position in ms.
        Uses Spotify API if available, falls back to MPRIS/SMTC position hint."""
        # Try Spotify API first (most accurate)
        if self.sp:
            try:
                sp_playback = self.sp.current_playback()
                if sp_playback and sp_playback.get('progress_ms') is not None:
                    return sp_playback['progress_ms']
            except Exception:
                pass
        # Fall back to position from OS media detection (MPRIS/SMTC)
        if hint_position_ms >= 0:
            return hint_position_ms
        return 0

    def _mute_spotify(self):
        """Mute source app (Windows: pycaw, Linux: pactl)."""
        if platform.system() == "Windows":
            mute_spotify_windows()
        elif platform.system() == "Linux":
            mute_spotify_linux()

    def _handle_new_track(self, media_info):
        """Handle switching to a new track."""
        logger.info(f"Track Changed: {media_info.title} by {media_info.artist} [{media_info.source_app}]")
        self.current_media = media_info
        self.waiting_for_user_selection = False
        self.current_song_favorited = False
        self.is_paused_waiting = False
        self.user_skip_pending = False
        self._last_track_change_time = time.time()
        self._initial_sync_done = False

        # Get album art
        art_key = media_info.mapping_key
        if art_key in self._art_cache:
            self.current_image_url = self._art_cache[art_key]
        else:
            # Fetch in background
            def _fetch_art():
                url = fetch_album_art_url(media_info.title, media_info.artist, media_info.album)
                self._art_cache[art_key] = url
                self.current_image_url = url
            threading.Thread(target=_fetch_art, daemon=True).start()

        # Check prefetch cache (by normalized key and spotify ID if available)
        norm_key = media_info.mapping_key
        tidal_track = self.prefetch_cache.get(norm_key)

        if not tidal_track:
            # Try to get spotify ID for cache lookup if API available
            if self.sp:
                try:
                    sp_playback = self.sp.current_playback()
                    if sp_playback and sp_playback.get('item'):
                        sp_id = sp_playback['item']['id']
                        tidal_track = self.prefetch_cache.get(sp_id)
                except:
                    pass

        if tidal_track:
            logger.info(f"Using prefetched match: {tidal_track.name}")
        else:
            # Get duration from Spotify API if available for better matching
            duration_ms = 0
            spotify_id = None
            if self.sp:
                try:
                    sp_playback = self.sp.current_playback()
                    if sp_playback and sp_playback.get('item'):
                        duration_ms = sp_playback['item'].get('duration_ms', 0)
                        spotify_id = sp_playback['item']['id']
                except:
                    pass

            tidal_track = self.search_tidal_match(
                title=media_info.title,
                artist=media_info.artist,
                duration_ms=duration_ms,
                spotify_id=spotify_id
            )

        if tidal_track:
            self.current_tidal_track = tidal_track
            self.status = f"Loading: {tidal_track.name}..."
            if not self.attempt_play_tidal(tidal_track, media_info.is_playing, media_info.position_ms):
                self.status = "Playback Error - Stopped"
        else:
            self.status = "Match Not Found - Waiting for User"
            self.player.stop()
            self.current_tidal_track = None
            self.waiting_for_user_selection = True
            if self.request_manual_match:
                self.request_manual_match(media_info)

    def shutdown(self):
        self.running = False
        try:
            if self.sp:
                self.sp.pause_playback()
        except:
            pass
        try:
            self.player.stop()
        except:
            pass

    def sync_logic(self):
        # Get current media from OS detection
        media_info = self.media_detector.get_current_media("spotify")

        if media_info is None or not media_info.title:
            if self.status != "Waiting for media...":
                logger.debug("No media detected from OS (MPRIS/SMTC)")
            self.status = "Waiting for media..."
            return

        # Ignore remote playback: if source is not playing locally and we
        # don't have an active track, don't start anything. This prevents
        # the app from reacting when the user is listening on another device.
        if not media_info.is_playing and self.current_tidal_track is None:
            self.status = "Waiting for local playback..."
            return

        # Mute Spotify
        if self.mute_spotify:
            self._mute_spotify()

        # VLC state
        vlc_time = self.player.get_time()
        vlc_duration = self.player.get_duration()
        vlc_is_playing = self.player.is_playing()
        vlc_has_track = vlc_duration > 0
        vlc_time_left = (vlc_duration - vlc_time) if vlc_has_track else 0

        # --- VLC Finished ---
        vlc_finished = (self.current_tidal_track and vlc_has_track
                        and not vlc_is_playing and vlc_time > 1000
                        and vlc_time_left < 1500)

        # Source changed track?
        source_changed = (self.current_media is None
                          or not media_info.matches(self.current_media))

        # Skip cooldown: ignore rapid source changes within 2s of last switch
        # (prevents double-skip when both source and VLC advance together)
        time_since_last_change = time.time() - self._last_track_change_time
        in_skip_cooldown = time_since_last_change < 2.0

        if vlc_finished:
            logger.info("Local playback finished")
            self.is_paused_waiting = False
            self._pending_next_media = None

            if source_changed:
                # Source already on next track (same-length songs or was paused waiting)
                # Just play whatever source has now - no need to call next_track()
                logger.info(f"Source already advanced to: {media_info.title}")
                self._handle_new_track(media_info)
                # Resume source in case we had it paused
                if self.sp:
                    try: self.sp.start_playback()
                    except: pass
                else:
                    self.media_detector.send_control('play')
            else:
                # Source still on same track - we need to advance it
                if self.sp:
                    try: self.sp.next_track()
                    except: pass
                else:
                    self.media_detector.send_control('next')
                self.current_media = None
                self.current_tidal_track = None
            return

        # --- Track Change Detection (VLC still playing) ---
        if source_changed:

            if self.user_skip_pending:
                # User used our skip controls - honor immediately
                self._handle_new_track(media_info)
                return

            # During skip cooldown, ignore source changes (prevents double-skip)
            if in_skip_cooldown:
                return

            # Race condition zone: VLC is in its last ~5 seconds
            race_zone = vlc_is_playing and vlc_has_track and vlc_time_left <= 5000

            if race_zone and vlc_time_left > 1500:
                # Near end of our track - pause source, let VLC finish
                if not self.is_paused_waiting:
                    if self.sp:
                        try: self.sp.pause_playback()
                        except: pass
                    else:
                        self.media_detector.send_control('pause')
                    self.is_paused_waiting = True
                    self._pending_next_media = media_info
                    logger.info(f"Near end ({vlc_time_left}ms left) - letting local playback finish")
                return

            # Normal skip (user skipped on Spotify) or VLC almost done - switch
            self._handle_new_track(media_info)
            return

        # --- Playback Monitor (same track) ---
        if self.current_tidal_track and not self.waiting_for_user_selection:
            # Get source position for sync checks
            source_pos = self._get_source_position(media_info.position_ms)

            # --- Seek / Restart Detection ---
            # Detect if source position jumped significantly compared to VLC
            if vlc_has_track and source_pos >= 0 and vlc_time >= 0:
                pos_diff = source_pos - vlc_time

                # Restart detection: source near 0 but VLC far ahead
                # (user hit "restart" or "previous" on same song)
                if source_pos < 3000 and vlc_time > 10000:
                    logger.info(f"Song restart detected (source at {source_pos}ms, VLC at {vlc_time}ms)")
                    self.player.set_position(source_pos)
                    self._initial_sync_done = True

                # Forward seek: source jumped ahead of VLC by > 5s
                elif pos_diff > 5000:
                    if source_pos < vlc_duration - 500:
                        logger.info(f"Source seeked forward: {vlc_time}ms -> {source_pos}ms")
                        self.player.set_position(source_pos)
                    else:
                        logger.info(f"Source seeked to {source_pos}ms (near/past our end {vlc_duration}ms), clamping")
                        self.player.set_position(max(0, vlc_duration - 1000))
                    self._initial_sync_done = True

                # Backward seek: source jumped behind VLC by > 5s
                elif pos_diff < -5000:
                    target = max(0, source_pos)
                    logger.info(f"Source seeked backward: {vlc_time}ms -> {target}ms")
                    self.player.set_position(target)
                    self._initial_sync_done = True

                # One-time initial sync (handles starting mid-song with small drift)
                elif not self._initial_sync_done and vlc_is_playing and vlc_time > 500:
                    self._initial_sync_done = True
                    if abs(pos_diff) > 3000:
                        target = min(source_pos, vlc_duration - 500) if vlc_duration > 0 else source_pos
                        if target > 0:
                            self.player.set_position(target)
                            logger.info(f"Initial position sync: {vlc_time}ms -> {target}ms")

            # Pause/Resume Sync
            if not media_info.is_playing and vlc_is_playing:
                self.player.pause()
            elif media_info.is_playing and not vlc_is_playing and not self.is_paused_waiting:
                if vlc_has_track and vlc_time < vlc_duration - 500:
                    self.player.resume()

            # Auto Favorite
            if self.auto_favorite and not self.current_song_favorited and vlc_is_playing:
                if vlc_duration > 0 and (vlc_time / vlc_duration) >= 0.90:
                    try:
                        self.tidal.user.favorites.add_track(self.current_tidal_track.id)
                        self.current_song_favorited = True
                        logger.info("Auto-Favorited Track")
                    except:
                        try:
                            self.tidal.add_favorite(self.current_tidal_track.id)
                            self.current_song_favorited = True
                            logger.info("Auto-Favorited Track")
                        except: pass

    def control_loop(self):
        if not self.login(): return
        # Sync community corrections on startup
        sync_community_mappings()

        # Log system info for debugging
        logger.info(f"Platform: {platform.system()} | DBUS: {DBUS_AVAILABLE} | SMTC: {SMTC_AVAILABLE}")
        devs = self.player.get_audio_devices()
        if devs:
            logger.info(f"Audio devices: {', '.join(d[0] for d in devs)}")
        else:
            logger.warning("No audio output devices detected")
        self.status = "Running"
        while self.running:
            try:
                self.sync_logic()
                if self.gui_callback: self.gui_callback(self.get_debug_info())
            except Exception as e:
                logger.error(f"Loop Error: {e}")
            time.sleep(REFRESH_RATE)

    def get_debug_info(self):
        t_name = self.current_tidal_track.name if self.current_tidal_track else "None"
        if self.waiting_for_user_selection: t_name = "(Selection Needed)"
        vlc_time = max(0, self.player.get_time())
        vlc_duration = max(0, self.player.get_duration())
        source_track = ""
        source_app = ""
        if self.current_media:
            source_track = f"{self.current_media.title} - {self.current_media.artist}"
            source_app = self.current_media.source_app
        return {
            'status': self.status,
            'tidal_track': t_name,
            'vlc_time': vlc_time,
            'vlc_duration': vlc_duration,
            'image_url': self.current_image_url,
            'source_track': source_track,
            'source_app': source_app,
            'spotify_api': self.spotify_api_available,
            'quality': self.current_quality,
        }

    # --- Playback Commands ---
    def manual_map_track(self, tidal_track):
        if self.current_media:
            key = self.current_media.mapping_key
            save_mapping(key, tidal_track.id)
            # Share with community if opted in
            if self.share_corrections:
                threading.Thread(
                    target=submit_correction,
                    args=(key, tidal_track.id),
                    daemon=True
                ).start()
            self.current_tidal_track = tidal_track
            self.waiting_for_user_selection = False
            self.status = f"Mapped: {tidal_track.name}"

            source_playing = self.current_media.is_playing if self.current_media else True
            if not self.attempt_play_tidal(tidal_track, source_playing):
                 messagebox.showerror("Playback Error", "Could not stream this track.\nIt might be region-locked or unavailable on your plan.")

    def toggle_play(self):
        if self.sp:
            try:
                if self.sp.current_playback()['is_playing']:
                    self.sp.pause_playback()
                else:
                    self.sp.start_playback()
                return
            except: pass
        self.media_detector.send_control('play_pause')

    def next_track(self):
        self.user_skip_pending = True
        self.player.stop()
        if self.sp:
            try:
                self.sp.next_track()
                return
            except: pass
        self.media_detector.send_control('next')

    def prev_track(self):
        self.user_skip_pending = True
        self.player.stop()
        if self.sp:
            try:
                self.sp.previous_track()
                return
            except: pass
        self.media_detector.send_control('previous')

    def seek_to(self, position_ms):
        """Seek VLC and optionally Spotify to a specific position."""
        self.player.set_position(position_ms)
        if self.sp:
            try:
                self.sp.seek_track(int(position_ms))
            except: pass

    def skip_forward_10(self):
        """Skip forward 10 seconds."""
        current = self.player.get_time()
        self.seek_to(current + 10000)

    def skip_backward_10(self):
        """Skip backward 10 seconds."""
        current = self.player.get_time()
        self.seek_to(max(0, current - 10000))

# --- GUI CLASSES ---

def apply_dark_title_bar(window):
    """Apply dark title bar on Windows 10/11 using DWM API."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass

class ModernToplevel(tk.Toplevel):
    def __init__(self, parent, title, geometry):
        super().__init__(parent)
        self.title(title)
        # Scale geometry for HiDPI
        try:
            dpi = self.winfo_fpixels('1i')
            scale = max(1.0, dpi / 96.0)
            w, h = geometry.split('x')
            self.geometry(f"{int(int(w) * scale)}x{int(int(h) * scale)}")
        except:
            self.geometry(geometry)
        self.configure(bg="#1e1e1e")
        if platform.system() == "Windows":
            self.iconbitmap(default='')
        self.update_idletasks()
        apply_dark_title_bar(self)

class ManualSelectWindow(ModernToplevel):
    def __init__(self, parent, manager, media_info):
        super().__init__(parent, "Fix Incorrect Match", "700x500")
        self.manager = manager
        self.media_info = media_info

        # Accept both MediaInfo objects and legacy sp_track dicts
        if isinstance(media_info, MediaInfo):
            self.track_title = media_info.title
            self.track_artist = media_info.artist
        else:
            # Legacy dict format (backward compat)
            self.track_title = media_info.get('name', media_info.get('title', ''))
            artists = media_info.get('artists', [])
            self.track_artist = artists[0]['name'] if artists else media_info.get('artist', '')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Treeview",
                        background="#2b2b2b",
                        foreground="#eeeeee",
                        fieldbackground="#2b2b2b",
                        borderwidth=0,
                        rowheight=25)
        style.map('Treeview', background=[('selected', '#444444')])
        style.configure("Treeview.Heading", background="#1e1e1e", foreground="#dddddd", relief="flat")
        style.map("Treeview.Heading", background=[('active', '#333333')])

        header = tk.Frame(self, bg="#1e1e1e")
        header.pack(fill='x', padx=20, pady=20)

        tk.Label(header, text=f"Fixing Match For: {self.track_title}", bg="#1e1e1e", fg="white", font=(UI_FONT, 12, "bold")).pack(anchor='w')
        tk.Label(header, text=f"Artist: {self.track_artist}", bg="#1e1e1e", fg="#bbbbbb", font=(UI_FONT, 10)).pack(anchor='w')

        search_frame = tk.Frame(self, bg="#1e1e1e")
        search_frame.pack(fill='x', padx=20, pady=5)

        self.entry_search = tk.Entry(search_frame, width=40, bg="#333333", fg="white", insertbackground="white", relief="flat", font=(UI_FONT, 10))
        self.entry_search.pack(side='left', padx=(0, 10), ipady=3)
        self.entry_search.insert(0, f"{self.track_title} {self.track_artist}")

        tk.Button(search_frame, text="Search Tidal", command=self.do_search,
                  bg="#444444", fg="white", relief="flat", padx=10, pady=2).pack(side='left')

        self.tree = ttk.Treeview(self, columns=("Title", "Artist", "Album"), show='headings', height=10)
        self.tree.heading("Title", text="Song Title")
        self.tree.heading("Artist", text="Artist")
        self.tree.heading("Album", text="Album")

        self.tree.column("Title", width=250)
        self.tree.column("Artist", width=150)
        self.tree.column("Album", width=200)

        self.tree.pack(fill='both', expand=True, padx=20, pady=10)

        btn_frame = tk.Frame(self, bg="#1e1e1e")
        btn_frame.pack(fill='x', padx=20, pady=20)

        tk.Button(btn_frame, text="Select & Map This Track", command=self.select_track,
                  bg="#008800", fg="white", relief="flat", padx=15, pady=5, font=(UI_FONT, 9, "bold")).pack(side='right')

        tk.Button(btn_frame, text="Cancel", command=self.destroy,
                  bg="#444444", fg="white", relief="flat", padx=10, pady=5, font=(UI_FONT, 9)).pack(side='right', padx=10)

        self.found_tracks = []
        self.do_search()

    def do_search(self):
        query = self.entry_search.get()
        if not query: return
        for i in self.tree.get_children(): self.tree.delete(i)
        try:
            threading.Thread(target=self._search_thread, args=(query,), daemon=True).start()
        except: pass

    def _search_thread(self, query):
        try:
            results = self.manager.tidal.search(query, models=[tidalapi.media.Track], limit=20)
            self.found_tracks = results['tracks']
            self.after(0, self._update_list)
        except Exception as e:
            logger.error(f"Manual search failed: {e}")

    def _update_list(self):
        for t in self.found_tracks:
            self.tree.insert("", "end", values=(t.name, t.artist.name, t.album.name))

    def select_track(self):
        sel = self.tree.selection()
        if not sel: return
        idx = self.tree.index(sel[0])
        track = self.found_tracks[idx]
        self.manager.manual_map_track(track)
        self.destroy()

class SettingsWindow(ModernToplevel):
    def __init__(self, parent, manager):
        super().__init__(parent, "Settings", "700x700")
        self.manager = manager

        style = ttk.Style()
        style.configure("TNotebook", background="#1e1e1e")
        style.configure("TNotebook.Tab", padding=[10, 5])

        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True, padx=10, pady=10)

        tab_gen = tk.Frame(tabs, bg="#1e1e1e")
        tab_audio = tk.Frame(tabs, bg="#1e1e1e")
        tab_spotify = tk.Frame(tabs, bg="#1e1e1e")
        tab_log = tk.Frame(tabs, bg="#1e1e1e")
        tabs.add(tab_gen, text="General")
        tabs.add(tab_audio, text="Audio Isolation")
        tabs.add(tab_spotify, text="Spotify API")
        tabs.add(tab_log, text="Logs")

        self.build_general(tab_gen)
        self.build_audio_isolation(tab_audio)
        self.build_spotify_api(tab_spotify)
        self.build_logs(tab_log)

    def build_general(self, frame):
        # Audio Device
        dev_header = tk.Frame(frame, bg="#1e1e1e")
        dev_header.pack(fill='x', padx=20, pady=(20, 5))
        tk.Label(dev_header, text="Audio Output Device:", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 10)).pack(side='left')
        tk.Button(dev_header, text="\u21bb Refresh", command=self._refresh_devices,
                  bg="#333333", fg="#bbbbbb", relief="flat", padx=8, pady=1,
                  font=(UI_FONT, 8), activebackground="#444444", activeforeground="white",
                  bd=0, highlightthickness=0, cursor="hand2").pack(side='right')

        device_frame = tk.Frame(frame, bg="#2b2b2b", highlightbackground="#333333",
                                highlightthickness=1)
        device_frame.pack(fill='x', padx=20, pady=(0, 15))

        # Style the scrollbar to match the dark theme
        style = ttk.Style()
        style.configure("Dark.Vertical.TScrollbar",
                         background="#404040", troughcolor="#2b2b2b",
                         arrowcolor="#888888", borderwidth=0)
        style.map("Dark.Vertical.TScrollbar",
                  background=[('active', '#555555'), ('pressed', '#666666')])

        self.device_listbox = tk.Listbox(device_frame, bg="#2b2b2b", fg="#eeeeee",
                                          selectbackground="#1DB954", selectforeground="white",
                                          relief="flat", font=(UI_FONT, 9), height=4,
                                          activestyle='none', exportselection=False,
                                          bd=0, highlightthickness=0)
        device_scrollbar = ttk.Scrollbar(device_frame, orient='vertical',
                                          command=self.device_listbox.yview,
                                          style="Dark.Vertical.TScrollbar")
        self.device_listbox.configure(yscrollcommand=device_scrollbar.set)

        self.device_listbox.pack(side='left', fill='both', expand=True, padx=(5, 0), pady=3)
        device_scrollbar.pack(side='right', fill='y', pady=2)

        self.device_listbox.insert(0, "Loading devices...")
        self.device_listbox.bind("<<ListboxSelect>>", self.on_device)

        threading.Thread(target=self.load_devices, daemon=True).start()

        # Toggles
        self.mute_var = tk.BooleanVar(value=self.manager.mute_spotify)
        chk_mute = tk.Checkbutton(frame, text="Mute Source App (Spotify/etc)", variable=self.mute_var,
                                  bg="#1e1e1e", fg="white", selectcolor="#1e1e1e", activebackground="#1e1e1e", activeforeground="white",
                                  command=self.save_toggles)
        chk_mute.pack(anchor='w', padx=15, pady=5)

        self.fav_var = tk.BooleanVar(value=self.manager.auto_favorite)
        chk_fav = tk.Checkbutton(frame, text="Auto-Favorite on Tidal (90% played)", variable=self.fav_var,
                                 bg="#1e1e1e", fg="white", selectcolor="#1e1e1e", activebackground="#1e1e1e", activeforeground="white",
                                 command=self.save_toggles)
        chk_fav.pack(anchor='w', padx=15, pady=5)

        self.share_var = tk.BooleanVar(value=self.manager.share_corrections)
        chk_share = tk.Checkbutton(frame, text="Share track corrections with community", variable=self.share_var,
                                   bg="#1e1e1e", fg="white", selectcolor="#1e1e1e", activebackground="#1e1e1e", activeforeground="white",
                                   command=self.save_toggles)
        chk_share.pack(anchor='w', padx=15, pady=5)

        # Mixer Button
        tk.Button(frame, text="Open Volume Mixer", command=self.open_mixer,
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20, pady=20)

        # Danger Zone
        tk.Label(frame, text="Reset Data", bg="#1e1e1e", fg="#ff5555", font=(UI_FONT, 10, "bold")).pack(anchor='w', padx=20, pady=(20,5))
        tk.Button(frame, text="Factory Reset (Wipe All Data)", command=self.wipe_data,
                  bg="#880000", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20)

    def build_audio_isolation(self, frame):
        """Tab for setting up audio isolation (virtual audio device)."""
        max_text_width = 620

        tk.Label(frame, text="Audio Isolation", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 12, "bold")).pack(anchor='w', padx=20, pady=(20, 5))

        if platform.system() == "Linux":
            self._build_audio_isolation_linux(frame, max_text_width)
        else:
            self._build_audio_isolation_windows(frame, max_text_width)

    def _build_audio_isolation_linux(self, frame, max_text_width):
        """Linux audio isolation info."""
        has_pactl = shutil.which("pactl") is not None

        if has_pactl:
            method_text = "Active: Source app is muted via PulseAudio/PipeWire (pactl)."
            method_color = "#00cc00"
        else:
            method_text = "pactl not found. Install pulseaudio-utils or pipewire-pulse for per-app muting."
            method_color = "#cccc00"

        tk.Label(frame, text=method_text, bg="#1e1e1e", fg=method_color,
                 font=(UI_FONT, 9), wraplength=max_text_width, justify='left').pack(anchor='w', padx=20, pady=(0, 15))

        # Detect audio server
        backend = _detect_linux_audio_backend()
        backend_name = {'pipewire': 'PipeWire', 'pulse': 'PulseAudio', 'alsa': 'ALSA'}.get(backend, 'Unknown')
        tk.Label(frame, text=f"Audio backend: {backend_name}", bg="#1e1e1e", fg="#aaaaaa",
                 font=(UI_FONT, 9)).pack(anchor='w', padx=20, pady=(0, 10))

        info_text = (
            "SpotifySync automatically mutes your source app using per-stream volume control. "
            "No additional setup is needed on Linux — PipeWire and PulseAudio both support "
            "per-application muting natively."
        )
        tk.Label(frame, text=info_text, bg="#1e1e1e", fg="#bbbbbb",
                 font=(UI_FONT, 9), justify='left', wraplength=max_text_width).pack(anchor='w', padx=20, pady=(0, 10))

        tk.Label(frame, text="Full Audio Isolation (Optional)", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 10, "bold")).pack(anchor='w', padx=20, pady=(10, 5))

        adv_text = (
            "For complete silence from the source app, you can create a null sink:\n"
            "  pactl load-module module-null-sink sink_name=SpotifySink\n"
            "Then set Spotify's output to 'SpotifySink' in pavucontrol."
        )
        tk.Label(frame, text=adv_text, bg="#1e1e1e", fg="#bbbbbb",
                 font=(UI_FONT, 9), justify='left', wraplength=max_text_width).pack(anchor='w', padx=20, pady=(0, 10))

        tk.Button(frame, text="Open Sound Settings",
                  command=self.open_mixer,
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20, pady=5)

    def _build_audio_isolation_windows(self, frame, max_text_width):
        """Windows audio isolation info."""
        if PYCAW_AVAILABLE:
            method_text = "Active: Source app is muted at the Windows audio mixer level (pycaw)."
            method_color = "#00cc00"
        else:
            method_text = "Active: pycaw not available. Install for per-app muting on Windows."
            method_color = "#cccc00"

        tk.Label(frame, text=method_text, bg="#1e1e1e", fg=method_color,
                 font=(UI_FONT, 9), wraplength=max_text_width, justify='left').pack(anchor='w', padx=20, pady=(0, 15))

        tk.Label(frame, text="Full Audio Isolation (Optional)", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 10, "bold")).pack(anchor='w', padx=20, pady=(10, 5))

        info_text = (
            "For complete audio isolation, you can route your source app's output to a "
            "virtual audio device so it produces no sound at all. This requires "
            "installing VB-Cable (free virtual audio cable)."
        )
        tk.Label(frame, text=info_text, bg="#1e1e1e", fg="#bbbbbb",
                 font=(UI_FONT, 9), justify='left', wraplength=max_text_width).pack(anchor='w', padx=20, pady=(0, 10))

        self.vbcable_status = tk.Label(frame, text="Checking for virtual audio devices...",
                                        bg="#1e1e1e", fg="#888888", font=(UI_FONT, 9),
                                        wraplength=max_text_width, justify='left')
        self.vbcable_status.pack(anchor='w', padx=20, pady=(0, 10))
        threading.Thread(target=self._check_vbcable, daemon=True).start()

        btn_frame = tk.Frame(frame, bg="#1e1e1e")
        btn_frame.pack(anchor='w', padx=20, pady=5)

        tk.Button(btn_frame, text="Download VB-Cable (Free)",
                  command=lambda: safe_open_browser("https://vb-audio.com/Cable/", self),
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5).pack(side='left', padx=(0, 10))

        tk.Button(btn_frame, text="Open Sound Settings",
                  command=self.open_mixer,
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5).pack(side='left')

        tk.Label(frame, text="Setup Steps:", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 10, "bold")).pack(anchor='w', padx=20, pady=(20, 5))

        steps = (
            "1. Install VB-Cable from the link above\n"
            "2. Open Windows Sound Settings (button above)\n"
            "3. Under 'App volume and device preferences', find your source app\n"
            "4. Set its Output to 'CABLE Input (VB-Audio Virtual Cable)'\n"
            "5. The source app's audio will now go to the virtual device (silence)"
        )
        tk.Label(frame, text=steps, bg="#1e1e1e", fg="#bbbbbb",
                 font=(UI_FONT, 9), justify='left', wraplength=max_text_width).pack(anchor='w', padx=20)

    def _check_vbcable(self):
        """Check if VB-Cable or similar virtual audio device is installed."""
        found = []
        try:
            devices = self.manager.player.get_audio_devices()
            for name, dev_id in devices:
                name_lower = name.lower()
                if any(kw in name_lower for kw in ['cable', 'virtual', 'vb-audio', 'voicemeeter']):
                    found.append(name)
        except: pass

        def _update():
            if found:
                self.vbcable_status.config(
                    text=f"Found: {', '.join(found)}", fg="#00cc00")
            else:
                self.vbcable_status.config(
                    text="No virtual audio devices detected. Install VB-Cable for full isolation.",
                    fg="#cc8800")
        self.after(0, _update)

    def build_spotify_api(self, frame):
        """Tab for optional Spotify API credentials."""
        max_text_width = 620

        tk.Label(frame, text="Spotify API (Optional)", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 12, "bold")).pack(anchor='w', padx=20, pady=(20, 5))

        info_text = (
            "By default the app detects your current track via OS media controls "
            "(works with Spotify, Apple Music, YouTube, and any other player).\n\n"
            "Enable the Spotify API for extra features:\n"
            "  - Queue prefetch (pre-loads next tracks for faster switching)\n"
            "  - Precise seek position sync between Spotify and Tidal\n"
            "  - More reliable track duration matching"
        )
        tk.Label(frame, text=info_text, bg="#1e1e1e", fg="#bbbbbb",
                 font=(UI_FONT, 9), justify='left', wraplength=max_text_width).pack(anchor='w', padx=20, pady=(0, 15))

        # Enable toggle
        settings = load_settings()
        self.spotify_api_var = tk.BooleanVar(value=settings.get("use_spotify_api", False))
        chk_api = tk.Checkbutton(frame, text="Enable Spotify API", variable=self.spotify_api_var,
                                 bg="#1e1e1e", fg="white", selectcolor="#1e1e1e",
                                 activebackground="#1e1e1e", activeforeground="white",
                                 font=(UI_FONT, 10, "bold"),
                                 command=self._toggle_spotify_api)
        chk_api.pack(anchor='w', padx=15, pady=(0, 10))

        # Status indicator
        if self.manager.spotify_api_available:
            status_text = "Status: Connected (API + OS detection)"
            status_color = "#00cc00"
        elif settings.get("use_spotify_api", False):
            status_text = "Status: Enabled but not connected (check credentials, restart app)"
            status_color = "#cc8800"
        else:
            status_text = "Status: OS media detection only"
            status_color = "#cccc00"

        self.lbl_spotify_status = tk.Label(frame, text=status_text, bg="#1e1e1e", fg=status_color,
                 font=(UI_FONT, 9, "bold"))
        self.lbl_spotify_status.pack(anchor='w', padx=20, pady=(0, 15))

        # Credentials frame (shown/hidden based on toggle)
        self.spotify_creds_frame = tk.Frame(frame, bg="#1e1e1e")
        self.spotify_creds_frame.pack(fill='x', padx=0, pady=0)

        # Client ID
        tk.Label(self.spotify_creds_frame, text="Client ID:", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 9)).pack(anchor='w', padx=20, pady=(5, 2))
        self.entry_client_id = tk.Entry(self.spotify_creds_frame, width=50, bg="#333333", fg="white",
                                         insertbackground="white", relief="flat", font=(UI_FONT, 10))
        self.entry_client_id.pack(anchor='w', padx=20, ipady=3)

        # Client Secret
        tk.Label(self.spotify_creds_frame, text="Client Secret:", bg="#1e1e1e", fg="white",
                 font=(UI_FONT, 9)).pack(anchor='w', padx=20, pady=(10, 2))
        self.entry_client_secret = tk.Entry(self.spotify_creds_frame, width=50, bg="#333333", fg="white",
                                             insertbackground="white", relief="flat", font=(UI_FONT, 10), show="*")
        self.entry_client_secret.pack(anchor='w', padx=20, ipady=3)

        # Pre-fill from keyring
        try:
            saved_id = keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID") or ""
            saved_secret = keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET") or ""
            self.entry_client_id.insert(0, saved_id)
            self.entry_client_secret.insert(0, saved_secret)
        except:
            pass

        # Buttons
        btn_frame = tk.Frame(self.spotify_creds_frame, bg="#1e1e1e")
        btn_frame.pack(anchor='w', padx=20, pady=15)

        tk.Button(btn_frame, text="Save Credentials", command=self._save_spotify_creds,
                  bg="#008800", fg="white", relief="flat", padx=10, pady=5,
                  font=(UI_FONT, 9, "bold")).pack(side='left', padx=(0, 10))

        tk.Button(btn_frame, text="Clear Credentials", command=self._clear_spotify_creds,
                  bg="#880000", fg="white", relief="flat", padx=10, pady=5,
                  font=(UI_FONT, 9)).pack(side='left', padx=(0, 10))

        tk.Button(btn_frame, text="Open Spotify Dashboard",
                  command=lambda: safe_open_browser("https://developer.spotify.com/dashboard", self),
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5,
                  font=(UI_FONT, 9)).pack(side='left')

        tk.Label(self.spotify_creds_frame, text="After saving, restart the app to connect.", bg="#1e1e1e",
                 fg="#888888", font=(UI_FONT, 8)).pack(anchor='w', padx=20, pady=(5, 0))

        # Show/hide credentials based on current toggle state
        if not self.spotify_api_var.get():
            self.spotify_creds_frame.pack_forget()

    def _toggle_spotify_api(self):
        enabled = self.spotify_api_var.get()
        save_setting("use_spotify_api", enabled)
        if enabled:
            self.spotify_creds_frame.pack(fill='x', padx=0, pady=0)
            self.lbl_spotify_status.config(text="Enabled - enter credentials below and restart",
                                            fg="#cc8800")
        else:
            self.spotify_creds_frame.pack_forget()
            self.lbl_spotify_status.config(text="Status: OS media detection only (restart to apply)",
                                            fg="#cccc00")

    def _save_spotify_creds(self):
        client_id = self.entry_client_id.get().strip()
        client_secret = self.entry_client_secret.get().strip()
        if not client_id or not client_secret:
            messagebox.showwarning("Missing", "Please enter both Client ID and Client Secret.")
            return
        try:
            keyring.set_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID", client_id)
            keyring.set_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET", client_secret)
            messagebox.showinfo("Saved", "Spotify credentials saved. Restart the app to connect.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save credentials: {e}")

    def _clear_spotify_creds(self):
        if messagebox.askyesno("Clear", "Remove Spotify API credentials? The app will use OS media detection only."):
            try:
                keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID")
            except: pass
            try:
                keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET")
            except: pass
            try:
                keyring.delete_password(KEYRING_SERVICE, "spotify_token")
            except: pass
            self.entry_client_id.delete(0, tk.END)
            self.entry_client_secret.delete(0, tk.END)
            messagebox.showinfo("Cleared", "Spotify credentials removed. Restart the app.")

    def build_logs(self, frame):
        self.log_text = scrolledtext.ScrolledText(frame, bg="#101010", fg="#00ff00", font=("Consolas", 9), state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=5, pady=5)
        self.update_logs()

    def load_devices(self):
        self.dev_map = {}
        self.dev_names = []
        try:
            # Retry device enumeration (VLC may need time to init audio output)
            devs = []
            for attempt in range(5):
                time.sleep(0.5)
                devs = self.manager.player.get_audio_devices()
                if devs:
                    break
                logger.debug(f"Device enumeration attempt {attempt+1}: no devices yet")

            # On Linux, also try pactl as fallback for device listing
            if not devs and platform.system() == "Linux" and shutil.which("pactl"):
                try:
                    result = subprocess.run(
                        ['pactl', 'list', 'short', 'sinks'],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.returncode == 0:
                        for line in result.stdout.strip().splitlines():
                            parts = line.split('\t')
                            if len(parts) >= 2:
                                devs.append((parts[1], parts[1]))
                        if devs:
                            logger.info(f"Loaded {len(devs)} device(s) via pactl fallback")
                except Exception as e:
                    logger.debug(f"pactl device fallback error: {e}")

            if devs:
                for name, did in devs:
                    self.dev_map[name] = did
                    self.dev_names.append(name)
                logger.info(f"Audio devices found: {len(devs)}")
            else:
                self.dev_names = ["Default (system default)"]
                self.dev_map[self.dev_names[0]] = None
                logger.warning("No audio devices found from VLC, using system default")

            def _update():
                try:
                    if not self.winfo_exists():
                        return
                    self.device_listbox.delete(0, tk.END)
                    for name in self.dev_names:
                        self.device_listbox.insert(tk.END, name)
                    # Select the currently saved device if present
                    saved_id = load_settings().get("last_device_id")
                    selected = False
                    if saved_id:
                        for i, name in enumerate(self.dev_names):
                            if self.dev_map.get(name) == saved_id:
                                self.device_listbox.selection_set(i)
                                self.device_listbox.see(i)
                                selected = True
                                break
                    if not selected and self.dev_names:
                        self.device_listbox.selection_set(0)
                except Exception:
                    pass
            self.after(0, _update)
        except Exception as e:
            logger.error(f"Failed to load devices: {e}")

    def _refresh_devices(self):
        self.device_listbox.delete(0, tk.END)
        self.device_listbox.insert(0, "Refreshing...")
        threading.Thread(target=self.load_devices, daemon=True).start()

    def on_device(self, e):
        sel = self.device_listbox.curselection()
        if not sel:
            return
        name = self.device_listbox.get(sel[0])
        did = self.dev_map.get(name)
        if did: self.manager.player.set_device(did)

    def open_mixer(self):
        sys_os = platform.system()
        try:
            if sys_os == "Windows":
                subprocess.Popen(["start", "ms-settings:apps-volume"], shell=True)
            elif sys_os == "Linux":
                cmd = None
                for candidate in [
                    ["pavucontrol"],
                    ["gnome-control-center", "sound"],
                    ["plasma-pa"],
                    ["xfce4-mixer"],
                    ["mate-volume-control"],
                ]:
                    if shutil.which(candidate[0]):
                        cmd = candidate
                        break
                if cmd:
                    subprocess.Popen(cmd)
                else:
                    messagebox.showinfo("Audio Control",
                        "No audio mixer found.\nInstall one with your package manager, e.g.:\n"
                        "  sudo dnf install pavucontrol\n"
                        "  sudo apt install pavucontrol")
        except Exception as e:
            logger.error(f"Error opening mixer: {e}")

    def save_toggles(self):
        self.manager.mute_spotify = self.mute_var.get()
        self.manager.auto_favorite = self.fav_var.get()
        self.manager.share_corrections = self.share_var.get()
        save_setting("mute_spotify", self.manager.mute_spotify)
        save_setting("auto_favorite", self.manager.auto_favorite)
        save_setting("share_corrections", self.manager.share_corrections)

    def wipe_data(self):
        if messagebox.askyesno("Reset", "Delete all settings and login data? App will close."):
            logging.shutdown()
            try:
                try: keyring.delete_password(KEYRING_SERVICE, "tidal_session")
                except: pass
                try: keyring.delete_password(KEYRING_SERVICE, "spotify_token")
                except: pass
                try: keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID")
                except: pass
                try: keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET")
                except: pass

                shutil.rmtree(APPDATA_DIR, ignore_errors=True)
            except: pass
            os._exit(0)

    def update_logs(self):
        if not self.winfo_exists(): return
        try:
            lines = []
            while not log_queue.empty():
                lines.append(log_queue.get_nowait())

            if lines:
                self.log_text.config(state='normal')
                self.log_text.insert(tk.END, "\n".join(lines) + "\n")
                self.log_text.see(tk.END)
                self.log_text.config(state='disabled')
        except: pass
        self.after(500, self.update_logs)

class MainApp(tk.Tk):
    def __init__(self, manager):
        super().__init__(className="spotifysync")
        self.manager = manager
        self.title("SpotifyTidalSync")

        # Set WM_CLASS on Linux so the window manager matches our .desktop file icon
        if platform.system() == "Linux":
            try:
                self.wm_attributes('-type', 'normal')
                self.tk.call('wm', 'iconname', self._w, 'spotifysync')
            except Exception:
                pass

        # Scale window for HiDPI displays
        try:
            self.tk.call('tk', 'scaling', self.winfo_fpixels('1i') / 72.0)
        except: pass
        dpi = self.winfo_fpixels('1i')
        scale = max(1.0, dpi / 96.0)
        w = int(400 * scale)
        h = int(810 * scale)
        self.geometry(f"{w}x{h}")

        self.configure(bg="#121212")
        self.update_idletasks()
        apply_dark_title_bar(self)

        # Set window icon
        try:
            icon_path = get_resource_path(os.path.join("assets", "logo.png"))
            if os.path.exists(icon_path):
                icon_img = Image.open(icon_path)
                self._icon_photo = ImageTk.PhotoImage(icon_img)
                self.iconphoto(True, self._icon_photo)
        except Exception:
            # Fallback: try native tkinter PhotoImage
            try:
                icon_path = get_resource_path(os.path.join("assets", "logo.png"))
                if os.path.exists(icon_path):
                    self._icon_photo = tk.PhotoImage(file=icon_path)
                    self.iconphoto(True, self._icon_photo)
            except Exception:
                pass

        self.last_img = None
        self.slider_dragging = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Styles
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Main.TLabel", background="#121212", foreground="white", font=(UI_FONT, 10))
        style.configure("Status.TLabel", background="#121212", foreground="#888888", font=(UI_FONT, 9))
        style.configure("Source.TLabel", background="#121212", foreground="#666666", font=(UI_FONT, 8))
        style.configure("Seek.Horizontal.TScale", background="#121212", troughcolor="#333333")

        # Album Art
        self.lbl_art = tk.Label(self, bg="#121212", text="[No Art]", fg="#444444")
        self.lbl_art.pack(pady=20)

        # Track Name
        self.lbl_track = ttk.Label(self, text="Waiting for media...", font=(UI_FONT, 13, "bold"),
                                   wraplength=380, justify="center", style="Main.TLabel")
        self.lbl_track.pack(pady=(0,5))

        # Status
        self.lbl_status = ttk.Label(self, text="Status: Initializing", style="Status.TLabel")
        self.lbl_status.pack(pady=(0,3))

        # Source indicator
        self.lbl_source = ttk.Label(self, text="", style="Source.TLabel")
        self.lbl_source.pack(pady=(0,3))

        # Quality indicator
        style.configure("Quality.TLabel", background="#121212", foreground="#1DB954", font=(UI_FONT, 9, "bold"))
        self.lbl_quality = ttk.Label(self, text="", style="Quality.TLabel")
        self.lbl_quality.pack(pady=(0,10))

        # --- Seek Slider ---
        slider_frame = tk.Frame(self, bg="#121212")
        slider_frame.pack(fill='x', padx=25, pady=(5, 0))

        self.lbl_current_time = tk.Label(slider_frame, text="0:00", bg="#121212", fg="#bbbbbb",
                                          font=(UI_FONT, 8), width=5, anchor='w')
        self.lbl_current_time.pack(side='left')

        self.seek_slider = ttk.Scale(slider_frame, from_=0, to=100, orient='horizontal',
                                      style="Seek.Horizontal.TScale")
        self.seek_slider.pack(side='left', fill='x', expand=True, padx=5)
        self.seek_slider.bind("<ButtonPress-1>", self._on_slider_press)
        self.seek_slider.bind("<ButtonRelease-1>", self._on_slider_release)

        self.lbl_total_time = tk.Label(slider_frame, text="0:00", bg="#121212", fg="#bbbbbb",
                                        font=(UI_FONT, 8), width=5, anchor='e')
        self.lbl_total_time.pack(side='right')

        # --- Controls ---
        ctrl_frame = tk.Frame(self, bg="#121212")
        ctrl_frame.pack(pady=15)

        btn_style = {"bg": "#282828", "fg": "white", "relief": "flat", "font": (UI_FONT, 10),
                     "activebackground": "#404040", "activeforeground": "white"}

        tk.Button(ctrl_frame, text="-10s", command=manager.skip_backward_10, width=4, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text="<<", command=manager.prev_track, width=4, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text="Play/Pause", command=manager.toggle_play, width=10, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text=">>", command=manager.next_track, width=4, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text="+10s", command=manager.skip_forward_10, width=4, **btn_style).pack(side='left', padx=3)

        # Fix Match Button
        tk.Button(self, text="Report Wrong Song / Fix Match", command=self.open_manual_match,
                  bg="#552222", fg="#ffbbbb", relief="flat", font=(UI_FONT, 9)).pack(pady=15)

        # Settings
        tk.Button(self, text="Settings", command=self.open_settings,
                  bg="#1a1a1a", fg="#888888", relief="flat").pack(side='bottom', pady=20, fill='x')

        # Pending UI update from background thread
        self._pending_info = None

        # Start slider update timer
        self._update_slider_timer()

        # Start UI update poller (reads from background thread safely)
        self._poll_ui_updates()

        # First-time community corrections opt-in prompt
        self.after(2000, self._check_share_prompt)

    def _check_share_prompt(self):
        """On first run, ask if the user wants to share corrections with the community."""
        settings = load_settings()
        if 'share_corrections' not in settings:
            result = messagebox.askyesno(
                "Community Corrections",
                "Would you like to automatically share your track corrections "
                "with the community?\n\n"
                "When you fix a wrong song match, the correction will be sent "
                "to a shared database so all users benefit.\n\n"
                "You can change this later in Settings."
            )
            save_setting('share_corrections', result)
            self.manager.share_corrections = result

    def _on_slider_press(self, event):
        self.slider_dragging = True

    def _on_slider_release(self, event):
        self.slider_dragging = False
        pos = int(float(self.seek_slider.get()))
        if pos >= 0:
            self.manager.seek_to(pos)

    def _format_time(self, ms):
        ms = max(0, ms)
        total_seconds = ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"

    def _poll_ui_updates(self):
        """Poll for UI updates from the background sync thread.
        Runs on the main tkinter thread so widget updates are safe."""
        try:
            info = self._pending_info
            if info is not None:
                self._pending_info = None
                self._update(info)
        except Exception as e:
            logger.debug(f"UI poll error: {e}")
        self.after(500, self._poll_ui_updates)

    def _update_slider_timer(self):
        """Frequently update the seek slider for smooth movement."""
        try:
            vlc_time = self.manager.player.get_time()
            vlc_duration = self.manager.player.get_duration()

            if not self.slider_dragging and vlc_duration > 0:
                self.seek_slider.configure(to=vlc_duration)
                self.seek_slider.set(vlc_time)

            self.lbl_current_time.config(text=self._format_time(vlc_time))
            self.lbl_total_time.config(text=self._format_time(vlc_duration))
        except:
            pass
        self.after(250, self._update_slider_timer)

    def open_manual_match(self, media_info=None):
        info = media_info if media_info else self.manager.current_media
        if not info:
            messagebox.showinfo("Info", "No media detected to fix.")
            return
        ManualSelectWindow(self, self.manager, info)

    def open_settings(self):
        SettingsWindow(self, self.manager)

    def update_ui(self, info):
        # Store latest info for the main-thread poller to pick up
        self._pending_info = info

    def _update(self, info):
        try:
            self.lbl_track.config(text=info['tidal_track'])
            self.lbl_status.config(text=info['status'])

            # Source indicator
            source_parts = []
            if info.get('source_app'):
                source_parts.append(f"Source: {info['source_app']}")
            if info.get('spotify_api'):
                source_parts.append("API")
            else:
                source_parts.append("OS detection")
            self.lbl_source.config(text=" | ".join(source_parts))

            # Quality display
            quality = info.get('quality', '')
            if quality:
                # Format nicely: HI_RES_LOSSLESS -> Hi-Res Lossless
                q_display = quality.replace('_', ' ').title()
                self.lbl_quality.config(text=f"Quality: {q_display}")
            else:
                self.lbl_quality.config(text="")
        except Exception as e:
            logger.debug(f"UI text update error: {e}")

        # Fetch album art in background to avoid blocking the UI
        url = info.get('image_url')
        if url and url != self.last_img:
            self.last_img = url
            def _fetch_art(art_url):
                try:
                    data = requests.get(art_url, timeout=5).content
                    img = Image.open(BytesIO(data))
                    img = img.resize((300, 300), Image.Resampling.LANCZOS)
                    # Store PIL image; PhotoImage must be created on main thread
                    self._pending_art_pil = img
                except Exception as e:
                    logger.debug(f"Art fetch error: {e}")
            threading.Thread(target=_fetch_art, args=(url,), daemon=True).start()

        # Apply any pending album art (convert to PhotoImage on main thread)
        pending_pil = getattr(self, '_pending_art_pil', None)
        if pending_pil is not None:
            self._pending_art_pil = None
            try:
                photo = ImageTk.PhotoImage(pending_pil)
                self._set_art(photo)
            except Exception as e:
                logger.debug(f"Art display error (ImageTk): {e}")
                # Fallback: save to temp file and load via tkinter's native PhotoImage
                try:
                    import tempfile
                    tmp = os.path.join(tempfile.gettempdir(), "spotifysync_art.png")
                    pending_pil.save(tmp, "PNG")
                    photo = tk.PhotoImage(file=tmp)
                    self._set_art(photo)
                except Exception as e2:
                    logger.debug(f"Art display fallback error: {e2}")

    def _set_art(self, photo):
        try:
            self.photo = photo
            self.lbl_art.config(image=self.photo, width=300, height=300)
        except: pass

    def on_close(self):
        self.manager.shutdown()
        self.destroy()
        try:
            sys.exit(0)
        except:
            os._exit(0)

def _acquire_instance_lock():
    """Ensure only one instance of the app runs at a time.
    Returns the lock file handle (must stay open) or None if another instance is running."""
    lock_path = os.path.join(APPDATA_DIR, "instance.lock")
    try:
        lock_file = open(lock_path, 'w')
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return lock_file
    except (IOError, OSError):
        return None

if __name__ == "__main__":
    _instance_lock = _acquire_instance_lock()
    if _instance_lock is None:
        # Another instance is already running — show error and exit
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("SpotifyTidalSync", "Another instance is already running.")
            root.destroy()
        except Exception:
            print("Another instance is already running.")
        sys.exit(1)

    manager = SyncManager()
    app = MainApp(manager)

    # Link callbacks
    manager.gui_callback = app.update_ui
    manager.request_manual_match_callback = app.open_manual_match

    t = threading.Thread(target=manager.control_loop, daemon=True)
    t.start()

    app.mainloop()
