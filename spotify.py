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
from datetime import timedelta, datetime
from io import BytesIO

# GUI Imports
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog
from PIL import Image, ImageTk
import requests

# Audio / API Imports
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler
import tidalapi
import vlc
from dotenv import load_dotenv

# Secure Storage
import keyring

# Windows per-app audio control (optional)
PYCAW_AVAILABLE = False
if platform.system() == "Windows":
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        from comtypes import CLSCTX_ALL
        PYCAW_AVAILABLE = True
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
KEYRING_SERVICE = "SpotifyTidalSync"

APPDATA_DIR = os.path.join(os.environ['APPDATA'] if platform.system() == "Windows" else os.path.expanduser('~/.config'), APP_NAME)

if not os.path.exists(APPDATA_DIR):
    os.makedirs(APPDATA_DIR)

SETTINGS_FILE = os.path.join(APPDATA_DIR, "settings.json")
MAPPINGS_FILE = os.path.join(APPDATA_DIR, "mappings.json")
ENV_FILE = os.path.join(APPDATA_DIR, ".env")
LOG_FILE = os.path.join(APPDATA_DIR, "debug.log")

# --- INITIALIZATION ---
def extract_bundled_files():
    if getattr(sys, 'frozen', False):
        bundled_env = os.path.join(sys._MEIPASS, ".env")
        if os.path.exists(bundled_env) and not os.path.exists(ENV_FILE):
            try:
                shutil.copy2(bundled_env, ENV_FILE)
            except Exception: pass

extract_bundled_files()
load_dotenv(ENV_FILE)

REFRESH_RATE = 1.0

# --- COMMUNITY CORRECTIONS ---
WORKER_URL = "https://correction-worker.lukas-van-dee.workers.dev"
GITHUB_MAPPINGS_URL = "https://raw.githubusercontent.com/Lukasvd123/SpotifyTidalSync/main/mappings.json"
APP_VERSION = "0.02"

# --- CLEAN LOGGING SETUP ---
log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_queue.put(msg)
        except Exception:
            self.handleError(record)

# Filter out verbose API logs
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("spotipy").setLevel(logging.WARNING)
logging.getLogger("tidalapi").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
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
def migrate_credentials_to_keyring():
    env_id = os.getenv('SPOTIFY_CLIENT_ID')
    env_secret = os.getenv('SPOTIFY_CLIENT_SECRET')

    if env_id and env_secret and env_id != "your_pasted_client_id_here":
        if not keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID"):
            try:
                keyring.set_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID", env_id)
                keyring.set_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET", env_secret)
                logger.info("Migrated Spotify Credentials to Secure Keyring")
            except Exception as e:
                logger.error(f"Failed to migrate credentials to keyring: {e}")

def get_credentials():
    client_id = keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID")
    client_secret = keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        client_id = os.getenv('SPOTIFY_CLIENT_ID')
        client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')

    return client_id, client_secret

# Run Migration on Startup
migrate_credentials_to_keyring()
SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET = get_credentials()
SPOTIFY_REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:8888/callback')

# --- DATA PERSISTENCE ---
def load_json(filepath):
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Load error {filepath}: {e}")
    return {}

def save_json(filepath, data):
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Save error {filepath}: {e}")

# Mappings: Spotify ID -> Tidal ID
def load_mappings(): return load_json(MAPPINGS_FILE)
def save_mapping(sp_id, tidal_id):
    data = load_mappings()
    data[sp_id] = tidal_id
    save_json(MAPPINGS_FILE, data)
    logger.info(f"Mapping saved: {sp_id} -> {tidal_id}")

# Settings
def load_settings(): return load_json(SETTINGS_FILE)
def save_setting(key, value):
    data = load_settings()
    data[key] = value
    save_json(SETTINGS_FILE, data)

# --- SECURE TOKEN STORAGE ---
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
        options = ['hi_res_lossless', 'high_lossless', 'lossless', 'LOSSLESS', 'high', 'HIGH']
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
            # No GUI available yet - print to console
            print(f"\n{'='*60}")
            print("BROWSER COULD NOT BE OPENED")
            print(f"{'='*60}")
            print(f"Please open this URL manually:\n{url}")
            print(f"{'='*60}\n")

# --- COMMUNITY MAPPINGS SYNC ---
def sync_community_mappings():
    """Fetch community corrections from GitHub and merge new entries into local mappings."""
    try:
        resp = requests.get(GITHUB_MAPPINGS_URL, timeout=10)
        if resp.status_code == 200:
            community = resp.json()
            local = load_mappings()
            added = 0
            for sp_id, tidal_id in community.items():
                if sp_id not in local:
                    local[sp_id] = tidal_id
                    added += 1
            if added > 0:
                save_json(MAPPINGS_FILE, local)
                logger.info(f"Synced {added} new community corrections")
            else:
                logger.info("Community mappings up to date")
        else:
            logger.debug(f"Community mappings fetch returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not sync community mappings: {e}")

def submit_correction(sp_id, tidal_id):
    """Submit a track correction to the community worker."""
    try:
        data = {
            "old_value": sp_id,
            "correct_value": str(tidal_id),
            "app_version": APP_VERSION
        }
        resp = requests.post(WORKER_URL, json=data, timeout=10)
        if resp.status_code == 200:
            logger.info("Correction shared with community")
        else:
            logger.warning(f"Failed to share correction: {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not submit correction: {e}")

# --- WINDOWS AUDIO MUTING (PYCAW) ---
def mute_spotify_windows():
    """Mute Spotify.exe at the Windows audio mixer level using pycaw."""
    if not PYCAW_AVAILABLE:
        return False
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process and session.Process.name().lower() == "spotify.exe":
                volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                volume.SetMute(1, None)
                return True
    except Exception as e:
        logger.debug(f"pycaw mute error: {e}")
    return False

# --- PREFETCH CACHE ---
class PrefetchCache:
    """Pre-fetches upcoming Tidal tracks from the Spotify queue to reduce latency on track changes."""

    def __init__(self):
        self._cache = {}  # spotify_id -> tidal_track
        self._lock = threading.Lock()
        self._prefetching = False

    def get(self, sp_id):
        with self._lock:
            return self._cache.get(sp_id)

    def put(self, sp_id, tidal_track):
        with self._lock:
            self._cache[sp_id] = tidal_track

    def has(self, sp_id):
        with self._lock:
            return sp_id in self._cache

    def clear_old(self, keep_ids):
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k in keep_ids}

    def start_prefetch(self, sp_client, search_func, current_track_id):
        if self._prefetching:
            return

        def _prefetch():
            self._prefetching = True
            try:
                queue_data = sp_client.queue()
                upcoming = queue_data.get('queue', [])[:5]

                keep_ids = {current_track_id}
                for track in upcoming:
                    keep_ids.add(track['id'])

                self.clear_old(keep_ids)

                for i, track in enumerate(upcoming):
                    sp_id = track['id']
                    if not self.has(sp_id):
                        tidal_match = search_func(track)
                        if tidal_match:
                            self.put(sp_id, tidal_match)
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
class AudioPlayer:
    def __init__(self):
        self.instance = vlc.Instance('--no-video', '--verbose=-1', '--aout=directsound' if platform.system() == "Windows" else '', '--network-caching=1500')
        self.player = self.instance.media_player_new()
        try: self.player.audio_set_volume(100)
        except: pass

        settings = load_settings()
        saved_device = settings.get("last_device_id")
        if saved_device:
            threading.Timer(1.0, lambda: self.set_device(saved_device)).start()

    def get_audio_devices(self):
        devices = []
        try:
            mods = self.player.audio_output_device_enum()
            if mods:
                mod = mods
                while mod:
                    mod = mod.contents
                    desc = mod.description.decode('utf-8', 'ignore') if mod.description else "Unknown"
                    dev_id = mod.device.decode('utf-8', 'ignore') if mod.device else None
                    if dev_id: devices.append((desc, dev_id))
                    mod = mod.next
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
        self.sp = None
        self.tidal = None
        self.player = AudioPlayer()
        self.gui_callback = gui_callback
        self.request_manual_match = request_manual_match_callback
        self.running = True

        self.current_spotify_track = None
        self.current_tidal_track = None
        self.status = "Initializing..."
        self.is_paused_waiting = False
        self.current_image_url = None
        self.user_skip_pending = False

        self.prefetch_cache = PrefetchCache()

        settings = load_settings()
        self.mute_spotify = settings.get("mute_spotify", True)
        self.auto_favorite = settings.get("auto_favorite", False)
        self.current_song_favorited = False
        self.waiting_for_user_selection = False
        self.share_corrections = settings.get("share_corrections", False)

    def login(self):
        # Spotify
        if not SPOTIFY_CLIENT_ID:
            self.status = "Missing Credentials"
            return False
        try:
            cache_handler = KeyringCacheHandler("spotify_token")

            auth_manager = SpotifyOAuth(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
                                        redirect_uri=SPOTIFY_REDIRECT_URI, scope="user-read-playback-state user-modify-playback-state user-read-currently-playing",
                                        cache_handler=cache_handler)
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            user = self.sp.current_user()
            logger.info(f"Spotify: Logged in as {user['display_name']}")
        except Exception as e:
            logger.error(f"Spotify Login Failed: {e}")
            return False

        # Tidal
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

    def search_tidal_match(self, sp_track):
        # 1. Check Mappings
        mappings = load_mappings()
        sp_id = sp_track['id']
        if sp_id in mappings:
            t_track = self.get_tidal_track_by_id(mappings[sp_id])
            if t_track:
                logger.info(f"Found manual mapping for '{sp_track['name']}'")
                return t_track

        # 2. Search
        track_name = sp_track['name']
        artist_name = sp_track['artists'][0]['name']
        duration_ms = sp_track['duration_ms']

        try:
            clean_name = track_name.split('(')[0].split('-')[0].strip()
            query = f"{clean_name} {artist_name}"
            logger.info(f"Searching Tidal: '{query}'")
            search = self.tidal.search(query, models=[tidalapi.media.Track], limit=10)

            best_match = None
            for t in search['tracks']:
                if abs((t.duration * 1000) - duration_ms) <= 5000:
                    best_match = t
                    break

            if best_match: return best_match

            logger.warning(f"No exact match found for '{track_name}'. Waiting for user.")
            return None

        except Exception as e:
            logger.warning(f"Search error: {e}")
        return None

    def check_and_refresh_session(self):
        if not self.tidal.check_login():
            logger.warning("Session expired. Attempting refresh...")
            if not self.tidal.check_login():
                logger.error("Session refresh failed. Re-login required.")
                return False
        return True

    def attempt_play_tidal(self, tidal_track, sp_is_playing):
        if not self.check_and_refresh_session():
             self.status = "Session Expired"
             return False

        Q = tidalapi.Quality
        qualities_to_try = []
        try:
            possible_attrs = ['hi_res_lossless', 'high_lossless', 'lossless', 'high', 'low']
            for attr in possible_attrs:
                if hasattr(Q, attr):
                    qualities_to_try.append(getattr(Q, attr))
        except: pass

        if not qualities_to_try: qualities_to_try = [PREFERRED_QUALITY]

        url = None
        used_quality = "Unknown"

        for quality in qualities_to_try:
            try:
                self.tidal.config.quality = quality
                url = tidal_track.get_url()

                if url:
                    used_quality = str(quality).split(".")[-1].upper()
                    break
            except Exception as e:
                logger.warning(f"Quality {quality} failed for this track: {e}")
                continue

        if not url:
            logger.error(f"FATAL: Could not stream '{tidal_track.name}' (Tried all qualities).")
            self.player.stop()
            return False

        try:
            self.player.play_url(url)
            time.sleep(0.5)

            if not sp_is_playing: self.sp.start_playback(); self.sp.seek_track(0)
            else: self.sp.seek_track(0)

            self.status = f"Playing: {tidal_track.name} [{used_quality}]"
            logger.info(f"Playing Tidal: {tidal_track.name} [{used_quality}]")

            # Start prefetching next tracks after successfully playing
            self.prefetch_cache.start_prefetch(
                self.sp, self.search_tidal_match,
                self.current_spotify_track['id'] if self.current_spotify_track else ""
            )

            return True
        except Exception as e:
            logger.error(f"Tidal Playback Crash: {e}")
            self.player.stop()
            return False

    def _mute_spotify(self, sp_playback):
        """Mute Spotify using pycaw (Windows) or API volume fallback."""
        # Try pycaw first for better per-app muting
        if mute_spotify_windows():
            return
        # Fallback: API volume control
        try:
            if sp_playback.get('device', {}).get('volume_percent') != 0: self.sp.volume(0)
        except: pass

    def _handle_new_track(self, sp_track, sp_is_playing):
        """Handle switching to a new track."""
        logger.info(f"Spotify Changed: {sp_track['name']}")
        self.current_spotify_track = sp_track
        self.waiting_for_user_selection = False
        self.current_song_favorited = False
        self.is_paused_waiting = False
        self.user_skip_pending = False

        # Check prefetch cache first
        sp_id = sp_track['id']
        tidal_track = self.prefetch_cache.get(sp_id)
        if tidal_track:
            logger.info(f"Using prefetched match: {tidal_track.name}")
        else:
            tidal_track = self.search_tidal_match(sp_track)

        if tidal_track:
            self.current_tidal_track = tidal_track
            self.status = f"Loading: {tidal_track.name}..."
            if not self.attempt_play_tidal(tidal_track, sp_is_playing):
                self.status = "Playback Error - Stopped"
        else:
            self.status = "Match Not Found - Waiting for User"
            self.player.stop()
            self.current_tidal_track = None
            self.waiting_for_user_selection = True
            if self.request_manual_match:
                self.request_manual_match(sp_track)

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
        try: sp_playback = self.sp.current_playback()
        except: self.status = "Spotify Error"; return

        if not sp_playback or not sp_playback.get('item'):
            self.status = "Spotify Idle"
            return

        sp_track = sp_playback['item']
        sp_id = sp_track['id']
        sp_is_playing = sp_playback['is_playing']

        # Mute Spotify
        if self.mute_spotify:
            self._mute_spotify(sp_playback)

        # Get Art
        try: self.current_image_url = sp_track['album']['images'][0]['url']
        except: self.current_image_url = None

        # VLC state
        vlc_time = self.player.get_time()
        vlc_duration = self.player.get_duration()
        vlc_is_playing = self.player.is_playing()
        vlc_has_track = vlc_duration > 0
        vlc_time_left = (vlc_duration - vlc_time) if vlc_has_track else 0

        # --- VLC Finished: Advance to next track ---
        # This is the primary trigger for track advancement (local playback based)
        if (self.current_tidal_track and vlc_has_track and not vlc_is_playing
                and vlc_time > 1000 and vlc_time_left < 1500):
            logger.info("Local playback finished - advancing to next track")
            self.is_paused_waiting = False
            try:
                self.sp.next_track()
            except: pass
            # Reset to force re-detection next cycle
            self.current_spotify_track = None
            self.current_tidal_track = None
            return

        # --- Track Change Detection ---
        if self.current_spotify_track is None or sp_id != self.current_spotify_track['id']:

            if self.user_skip_pending:
                # User used our skip controls - honor immediately
                self._handle_new_track(sp_track, sp_is_playing)
                return

            if vlc_is_playing and vlc_time_left > 15000:
                # VLC has lots of time left - likely a user skip in Spotify app
                logger.info(f"External skip detected: {sp_track['name']}")
                self._handle_new_track(sp_track, sp_is_playing)
                return

            if vlc_is_playing and vlc_time_left > 2000:
                # VLC still playing with moderate time left - Spotify auto-advanced
                # Wait for local playback to finish
                if not self.is_paused_waiting:
                    try:
                        self.sp.pause_playback()
                    except: pass
                    self.is_paused_waiting = True
                    logger.info("Pausing Spotify - waiting for local playback to finish")
                return

            # VLC is done or nearly done - safe to switch
            self._handle_new_track(sp_track, sp_is_playing)
            return

        # --- Playback Monitor (same track) ---
        if self.current_tidal_track and not self.waiting_for_user_selection:
            # Pause/Resume Sync
            if not sp_is_playing and vlc_is_playing:
                self.player.pause()
            elif sp_is_playing and not vlc_is_playing and not self.is_paused_waiting:
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
                            # Fallback for older tidalapi versions
                            self.tidal.add_favorite(self.current_tidal_track.id)
                            self.current_song_favorited = True
                            logger.info("Auto-Favorited Track")
                        except: pass

    def control_loop(self):
        if not self.login(): return
        # Sync community corrections on startup
        sync_community_mappings()
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
        return {
            'status': self.status,
            'tidal_track': t_name,
            'vlc_time': vlc_time,
            'vlc_duration': vlc_duration,
            'image_url': self.current_image_url
        }

    # --- Playback Commands ---
    def manual_map_track(self, tidal_track):
        if self.current_spotify_track:
            save_mapping(self.current_spotify_track['id'], tidal_track.id)
            # Share with community if opted in
            if self.share_corrections:
                threading.Thread(
                    target=submit_correction,
                    args=(self.current_spotify_track['id'], tidal_track.id),
                    daemon=True
                ).start()
            self.current_tidal_track = tidal_track
            self.waiting_for_user_selection = False
            self.status = f"Mapped: {tidal_track.name}"
            try: sp_playing = self.sp.current_playback()['is_playing']
            except: sp_playing = True

            if not self.attempt_play_tidal(tidal_track, sp_playing):
                 messagebox.showerror("Playback Error", "Could not stream this track.\nIt might be region-locked or unavailable on your plan.")

    def toggle_play(self):
        try:
            if self.sp.current_playback()['is_playing']: self.sp.pause_playback()
            else: self.sp.start_playback()
        except: pass

    def next_track(self):
        self.user_skip_pending = True
        self.player.stop()
        try: self.sp.next_track()
        except: pass

    def prev_track(self):
        self.user_skip_pending = True
        self.player.stop()
        try: self.sp.previous_track()
        except: pass

    def seek_to(self, position_ms):
        """Seek VLC and Spotify to a specific position."""
        self.player.set_position(position_ms)
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

class ModernToplevel(tk.Toplevel):
    def __init__(self, parent, title, geometry):
        super().__init__(parent)
        self.title(title)
        self.geometry(geometry)
        self.configure(bg="#1e1e1e")
        self.iconbitmap(default='')

class ManualSelectWindow(ModernToplevel):
    def __init__(self, parent, manager, sp_track):
        super().__init__(parent, "Fix Incorrect Match", "700x500")
        self.manager = manager
        self.sp_track = sp_track

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

        tk.Label(header, text=f"Fixing Match For: {sp_track['name']}", bg="#1e1e1e", fg="white", font=("Segoe UI", 12, "bold")).pack(anchor='w')
        tk.Label(header, text=f"Artist: {sp_track['artists'][0]['name']}", bg="#1e1e1e", fg="#bbbbbb", font=("Segoe UI", 10)).pack(anchor='w')

        search_frame = tk.Frame(self, bg="#1e1e1e")
        search_frame.pack(fill='x', padx=20, pady=5)

        self.entry_search = tk.Entry(search_frame, width=40, bg="#333333", fg="white", insertbackground="white", relief="flat", font=("Segoe UI", 10))
        self.entry_search.pack(side='left', padx=(0, 10), ipady=3)
        self.entry_search.insert(0, f"{sp_track['name']} {sp_track['artists'][0]['name']}")

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
                  bg="#008800", fg="white", relief="flat", padx=15, pady=5, font=("Segoe UI", 9, "bold")).pack(side='right')

        tk.Button(btn_frame, text="Cancel", command=self.destroy,
                  bg="#444444", fg="white", relief="flat", padx=10, pady=5, font=("Segoe UI", 9)).pack(side='right', padx=10)

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
        super().__init__(parent, "Settings", "700x650")
        self.manager = manager

        style = ttk.Style()
        style.configure("TNotebook", background="#1e1e1e")
        style.configure("TNotebook.Tab", padding=[10, 5])

        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True, padx=10, pady=10)

        tab_gen = tk.Frame(tabs, bg="#1e1e1e")
        tab_audio = tk.Frame(tabs, bg="#1e1e1e")
        tab_log = tk.Frame(tabs, bg="#1e1e1e")
        tabs.add(tab_gen, text="General")
        tabs.add(tab_audio, text="Audio Isolation")
        tabs.add(tab_log, text="Logs")

        self.build_general(tab_gen)
        self.build_audio_isolation(tab_audio)
        self.build_logs(tab_log)

    def build_general(self, frame):
        # Audio Device
        tk.Label(frame, text="Audio Output Device:", bg="#1e1e1e", fg="white", font=("Segoe UI", 10)).pack(anchor='w', padx=20, pady=(20,5))

        self.combo_device = ttk.Combobox(frame, state="readonly", width=60)
        self.combo_device.pack(anchor='w', padx=20, pady=(0, 20))
        self.combo_device.set("Loading devices...")
        self.combo_device.bind("<<ComboboxSelected>>", self.on_device)

        threading.Thread(target=self.load_devices, daemon=True).start()

        # Toggles
        self.mute_var = tk.BooleanVar(value=self.manager.mute_spotify)
        chk_mute = tk.Checkbutton(frame, text="Mute Spotify Desktop App", variable=self.mute_var,
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
        mixer_text = "Open Volume Mixer"
        if platform.system() == "Linux": mixer_text = "Open Linux Audio Control"

        tk.Button(frame, text=mixer_text, command=self.open_mixer,
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20, pady=20)

        # Danger Zone
        tk.Label(frame, text="Reset Data", bg="#1e1e1e", fg="#ff5555", font=("Segoe UI", 10, "bold")).pack(anchor='w', padx=20, pady=(20,5))
        tk.Button(frame, text="Factory Reset (Wipe All Data)", command=self.wipe_data,
                  bg="#880000", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20)

    def build_audio_isolation(self, frame):
        """Tab for setting up Spotify audio isolation (virtual audio device)."""
        tk.Label(frame, text="Spotify Audio Isolation", bg="#1e1e1e", fg="white",
                 font=("Segoe UI", 12, "bold")).pack(anchor='w', padx=20, pady=(20, 5))

        # Current method info
        if PYCAW_AVAILABLE:
            method_text = "Active: Spotify is muted at the Windows audio mixer level (pycaw)."
            method_color = "#00cc00"
        else:
            method_text = "Active: Spotify is muted via API volume control."
            method_color = "#cccc00"

        tk.Label(frame, text=method_text, bg="#1e1e1e", fg=method_color,
                 font=("Segoe UI", 9)).pack(anchor='w', padx=20, pady=(0, 15))

        # VB-Cable section
        tk.Label(frame, text="Full Audio Isolation (Optional)", bg="#1e1e1e", fg="white",
                 font=("Segoe UI", 10, "bold")).pack(anchor='w', padx=20, pady=(10, 5))

        info_text = (
            "For complete audio isolation, you can route Spotify's output to a\n"
            "virtual audio device so it produces no sound at all. This requires\n"
            "installing VB-Cable (free virtual audio cable)."
        )
        tk.Label(frame, text=info_text, bg="#1e1e1e", fg="#bbbbbb",
                 font=("Segoe UI", 9), justify='left').pack(anchor='w', padx=20, pady=(0, 10))

        # Detect VB-Cable
        self.vbcable_status = tk.Label(frame, text="Checking for virtual audio devices...",
                                        bg="#1e1e1e", fg="#888888", font=("Segoe UI", 9))
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

        # Setup instructions
        tk.Label(frame, text="Setup Steps:", bg="#1e1e1e", fg="white",
                 font=("Segoe UI", 10, "bold")).pack(anchor='w', padx=20, pady=(20, 5))

        steps = (
            "1. Install VB-Cable from the link above\n"
            "2. Open Windows Sound Settings (button above)\n"
            "3. Under 'App volume and device preferences', find Spotify\n"
            "4. Set Spotify's Output to 'CABLE Input (VB-Audio Virtual Cable)'\n"
            "5. Spotify's audio will now go to the virtual device (silence)"
        )
        tk.Label(frame, text=steps, bg="#1e1e1e", fg="#bbbbbb",
                 font=("Segoe UI", 9), justify='left').pack(anchor='w', padx=20)

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

    def build_logs(self, frame):
        self.log_text = scrolledtext.ScrolledText(frame, bg="#101010", fg="#00ff00", font=("Consolas", 9), state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=5, pady=5)
        self.update_logs()

    def load_devices(self):
        self.dev_map = {}
        try:
            time.sleep(1)
            devs = self.manager.player.get_audio_devices()
            names = []
            if devs:
                for name, did in devs:
                    self.dev_map[name] = did
                    names.append(name)
            else:
                names = ["Default / No Devices Found"]
                self.dev_map[names[0]] = None

            def _update():
                self.combo_device['values'] = names
                if names: self.combo_device.set(names[0])
            self.after(0, _update)
        except Exception as e:
            logger.error(f"Failed to load devices: {e}")

    def on_device(self, e):
        did = self.dev_map.get(self.combo_device.get())
        if did: self.manager.player.set_device(did)

    def open_mixer(self):
        sys_os = platform.system()
        try:
            if sys_os == "Windows":
                subprocess.Popen(["start", "ms-settings:apps-volume"], shell=True)
            elif sys_os == "Linux":
                cmd = None
                if shutil.which("pavucontrol"): cmd = ["pavucontrol"]
                elif shutil.which("gnome-control-center"): cmd = ["gnome-control-center", "sound"]

                if cmd: subprocess.Popen(cmd)
                else: messagebox.showinfo("Linux Audio", "Could not find 'pavucontrol' or gnome-settings.")
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
        super().__init__()
        self.manager = manager
        self.title("SpotifyTidalSync")
        self.geometry("400x780")
        self.configure(bg="#121212")

        # Set window icon
        try:
            icon_path = get_resource_path(os.path.join("assets", "logo.png"))
            if os.path.exists(icon_path):
                icon_img = Image.open(icon_path)
                self._icon_photo = ImageTk.PhotoImage(icon_img)
                self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        self.last_img = None
        self.slider_dragging = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Styles
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Main.TLabel", background="#121212", foreground="white", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#121212", foreground="#888888", font=("Segoe UI", 9))
        style.configure("Seek.Horizontal.TScale", background="#121212", troughcolor="#333333")

        # Album Art
        self.lbl_art = tk.Label(self, bg="#121212", text="[No Art]", fg="#444444")
        self.lbl_art.pack(pady=20)

        # Track Name
        self.lbl_track = ttk.Label(self, text="Waiting for Spotify...", font=("Segoe UI", 13, "bold"),
                                   wraplength=380, justify="center", style="Main.TLabel")
        self.lbl_track.pack(pady=(0,5))

        # Status
        self.lbl_status = ttk.Label(self, text="Status: Initializing", style="Status.TLabel")
        self.lbl_status.pack(pady=(0,10))

        # --- Seek Slider ---
        slider_frame = tk.Frame(self, bg="#121212")
        slider_frame.pack(fill='x', padx=25, pady=(5, 0))

        self.lbl_current_time = tk.Label(slider_frame, text="0:00", bg="#121212", fg="#bbbbbb",
                                          font=("Segoe UI", 8), width=5, anchor='w')
        self.lbl_current_time.pack(side='left')

        self.seek_slider = ttk.Scale(slider_frame, from_=0, to=100, orient='horizontal',
                                      style="Seek.Horizontal.TScale")
        self.seek_slider.pack(side='left', fill='x', expand=True, padx=5)
        self.seek_slider.bind("<ButtonPress-1>", self._on_slider_press)
        self.seek_slider.bind("<ButtonRelease-1>", self._on_slider_release)

        self.lbl_total_time = tk.Label(slider_frame, text="0:00", bg="#121212", fg="#bbbbbb",
                                        font=("Segoe UI", 8), width=5, anchor='e')
        self.lbl_total_time.pack(side='right')

        # --- Controls ---
        ctrl_frame = tk.Frame(self, bg="#121212")
        ctrl_frame.pack(pady=15)

        btn_style = {"bg": "#282828", "fg": "white", "relief": "flat", "font": ("Segoe UI", 10),
                     "activebackground": "#404040", "activeforeground": "white"}

        tk.Button(ctrl_frame, text="-10s", command=manager.skip_backward_10, width=4, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text="<<", command=manager.prev_track, width=4, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text="Play/Pause", command=manager.toggle_play, width=10, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text=">>", command=manager.next_track, width=4, **btn_style).pack(side='left', padx=3)
        tk.Button(ctrl_frame, text="+10s", command=manager.skip_forward_10, width=4, **btn_style).pack(side='left', padx=3)

        # Fix Match Button
        tk.Button(self, text="Report Wrong Song / Fix Match", command=self.open_manual_match,
                  bg="#552222", fg="#ffbbbb", relief="flat", font=("Segoe UI", 9)).pack(pady=15)

        # Settings
        tk.Button(self, text="Settings", command=self.open_settings,
                  bg="#1a1a1a", fg="#888888", relief="flat").pack(side='bottom', pady=20, fill='x')

        # Start slider update timer
        self._update_slider_timer()

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

    def open_manual_match(self, sp_track=None):
        track_to_fix = sp_track if sp_track else self.manager.current_spotify_track
        if not track_to_fix:
            messagebox.showinfo("Info", "No Spotify track detected to fix.")
            return
        ManualSelectWindow(self, self.manager, track_to_fix)

    def open_settings(self):
        SettingsWindow(self, self.manager)

    def update_ui(self, info):
        self.after(0, lambda: self._update(info))

    def _update(self, info):
        self.lbl_track.config(text=info['tidal_track'])
        self.lbl_status.config(text=info['status'])

        url = info.get('image_url')
        if url != self.last_img:
            self.last_img = url
            if url:
                try:
                    data = requests.get(url).content
                    img = Image.open(BytesIO(data))
                    img = img.resize((300, 300), Image.Resampling.LANCZOS)
                    self.photo = ImageTk.PhotoImage(img)
                    self.lbl_art.config(image=self.photo, width=300, height=300)
                except: pass

    def on_close(self):
        self.manager.shutdown()
        self.destroy()
        try:
            sys.exit(0)
        except:
            os._exit(0)

if __name__ == "__main__":
    manager = SyncManager()
    app = MainApp(manager)

    # Link callbacks
    manager.gui_callback = app.update_ui
    manager.request_manual_match_callback = app.open_manual_match

    t = threading.Thread(target=manager.control_loop, daemon=True)
    t.start()

    app.mainloop()
