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

# --- PATH CONFIGURATION ---
APP_NAME = "SpotifyTidalSync"
# Service name for Windows Credential Manager / Linux Keyring
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
    """Moves env credentials to secure keyring if they exist in file but not in ring."""
    env_id = os.getenv('SPOTIFY_CLIENT_ID')
    env_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
    
    if env_id and env_secret and env_id != "your_pasted_client_id_here":
        # Check if already in keyring
        if not keyring.get_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID"):
            try:
                keyring.set_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID", env_id)
                keyring.set_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET", env_secret)
                logger.info("Migrated Spotify Credentials to Secure Keyring")
            except Exception as e:
                logger.error(f"Failed to migrate credentials to keyring: {e}")

def get_credentials():
    """Gets credentials from Keyring first, then falls back to ENV."""
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
    """
    Custom CacheHandler for Spotipy to store tokens in OS Keyring/Credential Manager
    instead of a plaintext file.
    """
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
        # Prefer High Res
        options = ['hi_res_lossless', 'high_lossless', 'lossless', 'LOSSLESS', 'high', 'HIGH']
        for opt in options:
            if hasattr(Q, opt): return getattr(Q, opt)
        return None
    except: return None

PREFERRED_QUALITY = get_tidal_quality()

# --- AUDIO PLAYER ---
class AudioPlayer:
    def __init__(self):
        # VLC Instance
        self.instance = vlc.Instance('--no-video', '--verbose=-1', '--aout=directsound' if platform.system() == "Windows" else '', '--network-caching=1500') 
        self.player = self.instance.media_player_new()
        try: self.player.audio_set_volume(100)
        except: pass 
        
        settings = load_settings()
        saved_device = settings.get("last_device_id")
        if saved_device:
            threading.Timer(1.0, lambda: self.set_device(saved_device)).start()

    def get_audio_devices(self):
        # This can be slow, call asynchronously where possible
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

# --- SYNC MANAGER ---
class SyncManager:
    def __init__(self, gui_callback=None, request_manual_match_callback=None):
        self.sp = None
        self.tidal = None
        self.player = AudioPlayer()
        self.gui_callback = gui_callback
        self.request_manual_match = request_manual_match_callback # Callback to open GUI
        self.running = True
        
        self.current_spotify_track = None # Full Object
        self.current_tidal_track = None
        self.status = "Initializing..."
        self.is_paused_waiting = False
        self.current_image_url = None
        
        settings = load_settings()
        self.mute_spotify = settings.get("mute_spotify", True)
        self.auto_favorite = settings.get("auto_favorite", False)
        self.current_song_favorited = False
        self.waiting_for_user_selection = False

    def login(self):
        # Spotify
        if not SPOTIFY_CLIENT_ID:
            self.status = "Missing Credentials"
            return False
        try:
            # Use Keyring Cache Handler
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

            # --- TRY LOADING CACHED SESSION FROM KEYRING ---
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

            # Verify Login Validity
            if loaded and self.tidal.check_login():
                 logger.info("Tidal: Logged in (Cached via Keyring)")
            else:
                 logger.info("Tidal: Login Required (No Cache or Expired)...")
                 auth_res = self.tidal.login_oauth()
                 
                 # Handle TidalAPI variations
                 link_login = auth_res[0] if isinstance(auth_res, (tuple, list)) else auth_res
                 url = getattr(link_login, 'verification_uri_complete', None) or getattr(link_login, 'verificationUriComplete', None)
                 
                 if url:
                     if not url.startswith('http'): url = 'https://' + url
                     webbrowser.open(url)
                 
                 if isinstance(auth_res, (tuple, list)) and len(auth_res) > 1: 
                     auth_res[1].result()
                 else: 
                     self.tidal.complete_login(link_login)
                
                 # --- SAVE SESSION IF SUCCESSFUL TO KEYRING ---
                 if self.tidal.check_login():
                     expiry_ts = None
                     if self.tidal.expiry_time:
                         # tidalapi might store expiry as datetime object
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
        """Checks if session is valid, attempts refresh if not."""
        if not self.tidal.check_login():
            logger.warning("Session expired. Attempting refresh...")
            # TidalAPI handles refresh auto if load_oauth_session was used correctly
            # But we double check here
            if not self.tidal.check_login():
                logger.error("Session refresh failed. Re-login required.")
                return False
        return True

    def attempt_play_tidal(self, tidal_track, sp_is_playing):
        if not self.check_and_refresh_session():
             self.status = "Session Expired"
             return False

        # --- CORRECT QUALITY FALLBACK LOGIC ---
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
                # Set Session Quality
                self.tidal.config.quality = quality
                
                # Fetch URL (No arguments passed to get_url)
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
            return True
        except Exception as e:
            logger.error(f"Tidal Playback Crash: {e}")
            self.player.stop()
            return False

    def shutdown(self):
        self.running = False
        try:
            # Force pause on Spotify
            if self.sp:
                self.sp.pause_playback()
        except: 
            pass
        
        try:
            # Stop Tidal
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
        
        # Mute Spotify Logic
        if self.mute_spotify:
            try: 
                if sp_playback.get('device', {}).get('volume_percent') != 0: self.sp.volume(0)
            except: pass

        # Get Art
        try: self.current_image_url = sp_track['album']['images'][0]['url']
        except: self.current_image_url = None

        # --- Track Change ---
        if self.current_spotify_track is None or sp_id != self.current_spotify_track['id']:
            
            # Don't switch if finishing last song
            vlc_left = self.player.get_duration() - self.player.get_time()
            if self.player.is_playing() and 0 < vlc_left < 5000: return

            logger.info(f"Spotify Changed: {sp_track['name']}")
            self.current_spotify_track = sp_track
            self.waiting_for_user_selection = False
            self.current_song_favorited = False

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

        # --- Playback Monitor ---
        if self.current_tidal_track and not self.waiting_for_user_selection:
            # Simple Pause/Resume Sync
            if not sp_is_playing and self.player.is_playing(): self.player.pause()
            elif sp_is_playing and not self.player.is_playing() and not self.is_paused_waiting:
                if self.player.get_time() < self.player.get_duration() - 500: self.player.resume()

            # Auto Favorite
            if self.auto_favorite and not self.current_song_favorited and self.player.is_playing():
                if (self.player.get_time() / self.player.get_duration()) >= 0.90:
                    try:
                        self.tidal.add_favorite(self.current_tidal_track.id)
                        self.current_song_favorited = True
                        logger.info("Auto-Favorited Track")
                    except: pass
            
            # End of Track Handling
            time_left_tidal = self.player.get_duration() - self.player.get_time()
            time_left_sp = sp_track['duration_ms'] - sp_playback['progress_ms']
            
            # If Spotify is way ahead (next song buffered), pause it
            if time_left_sp < 3000 and time_left_tidal > 5000 and not self.is_paused_waiting:
                self.sp.pause_playback()
                self.is_paused_waiting = True
                logger.info("Buffering: Pausing Spotify to let Tidal finish")

            # If Tidal finishes, force Spotify Next
            if self.is_paused_waiting and (time_left_tidal < 1000 or not self.player.is_playing()):
                self.sp.next_track()
                self.is_paused_waiting = False

    def control_loop(self):
        if not self.login(): return
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
        return {
            'status': self.status,
            'tidal_track': t_name,
            'vlc_time': str(timedelta(milliseconds=self.player.get_time())),
            'image_url': self.current_image_url
        }

    # Commands
    def manual_map_track(self, tidal_track):
        if self.current_spotify_track:
            save_mapping(self.current_spotify_track['id'], tidal_track.id)
            self.current_tidal_track = tidal_track
            self.waiting_for_user_selection = False
            self.status = f"Mapped: {tidal_track.name}"
            # Helper to check play state
            try: sp_playing = self.sp.current_playback()['is_playing']
            except: sp_playing = True
            
            # Force attempt play and Log result
            if not self.attempt_play_tidal(tidal_track, sp_playing):
                 messagebox.showerror("Playback Error", "Could not stream this track.\nIt might be region-locked or unavailable on your plan.")

    def toggle_play(self):
        try:
            if self.sp.current_playback()['is_playing']: self.sp.pause_playback()
            else: self.sp.start_playback()
        except: pass
    def next_track(self):
        try: self.sp.next_track()
        except: pass
    def prev_track(self):
        try: self.sp.previous_track()
        except: pass

# --- GUI CLASSES ---

class ModernToplevel(tk.Toplevel):
    """Base class for styled windows"""
    def __init__(self, parent, title, geometry):
        super().__init__(parent)
        self.title(title)
        self.geometry(geometry)
        self.configure(bg="#1e1e1e")
        self.iconbitmap(default='') # Standard generic icon
        
class ManualSelectWindow(ModernToplevel):
    def __init__(self, parent, manager, sp_track):
        super().__init__(parent, "Fix Incorrect Match", "700x500")
        self.manager = manager
        self.sp_track = sp_track
        
        # Styles for Treeview
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

        # UI Fix: Use tk.Frame with explicit bg to prevent "grey bars"
        header = tk.Frame(self, bg="#1e1e1e")
        header.pack(fill='x', padx=20, pady=20)
        
        tk.Label(header, text=f"Fixing Match For: {sp_track['name']}", bg="#1e1e1e", fg="white", font=("Segoe UI", 12, "bold")).pack(anchor='w')
        tk.Label(header, text=f"Artist: {sp_track['artists'][0]['name']}", bg="#1e1e1e", fg="#bbbbbb", font=("Segoe UI", 10)).pack(anchor='w')
        
        # Search Bar
        search_frame = tk.Frame(self, bg="#1e1e1e")
        search_frame.pack(fill='x', padx=20, pady=5)
        
        # Modern Flat Entry
        self.entry_search = tk.Entry(search_frame, width=40, bg="#333333", fg="white", insertbackground="white", relief="flat", font=("Segoe UI", 10))
        self.entry_search.pack(side='left', padx=(0, 10), ipady=3)
        self.entry_search.insert(0, f"{sp_track['name']} {sp_track['artists'][0]['name']}")
        
        # Modern Flat Search Button
        tk.Button(search_frame, text="Search Tidal", command=self.do_search, 
                  bg="#444444", fg="white", relief="flat", padx=10, pady=2).pack(side='left')

        # Results List
        self.tree = ttk.Treeview(self, columns=("Title", "Artist", "Album"), show='headings', height=10)
        self.tree.heading("Title", text="Song Title")
        self.tree.heading("Artist", text="Artist")
        self.tree.heading("Album", text="Album")
        
        self.tree.column("Title", width=250)
        self.tree.column("Artist", width=150)
        self.tree.column("Album", width=200)
        
        self.tree.pack(fill='both', expand=True, padx=20, pady=10)
        
        # Buttons Frame
        btn_frame = tk.Frame(self, bg="#1e1e1e")
        btn_frame.pack(fill='x', padx=20, pady=20)
        
        # Modern Flat Action Buttons
        tk.Button(btn_frame, text="Select & Map This Track", command=self.select_track,
                  bg="#008800", fg="white", relief="flat", padx=15, pady=5, font=("Segoe UI", 9, "bold")).pack(side='right')
                  
        tk.Button(btn_frame, text="Cancel", command=self.destroy,
                  bg="#444444", fg="white", relief="flat", padx=10, pady=5, font=("Segoe UI", 9)).pack(side='right', padx=10)

        self.found_tracks = []
        self.do_search() # Auto search on open

    def do_search(self):
        query = self.entry_search.get()
        if not query: return
        
        # Clear tree
        for i in self.tree.get_children(): self.tree.delete(i)
        
        try:
            # Run in thread to not freeze GUI
            threading.Thread(target=self._search_thread, args=(query,), daemon=True).start()
        except: pass

    def _search_thread(self, query):
        try:
            results = self.manager.tidal.search(query, models=[tidalapi.media.Track], limit=20)
            self.found_tracks = results['tracks']
            
            # Update UI on main thread
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
        super().__init__(parent, "Settings", "700x550")
        self.manager = manager
        
        style = ttk.Style()
        style.configure("TNotebook", background="#1e1e1e")
        style.configure("TNotebook.Tab", padding=[10, 5])

        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True, padx=10, pady=10)

        # Tabs
        tab_gen = tk.Frame(tabs, bg="#1e1e1e")
        tab_log = tk.Frame(tabs, bg="#1e1e1e")
        tabs.add(tab_gen, text="General")
        tabs.add(tab_log, text="Logs")

        self.build_general(tab_gen)
        self.build_logs(tab_log)

    def build_general(self, frame):
        # Audio Device
        tk.Label(frame, text="Audio Output Device:", bg="#1e1e1e", fg="white", font=("Segoe UI", 10)).pack(anchor='w', padx=20, pady=(20,5))
        
        self.combo_device = ttk.Combobox(frame, state="readonly", width=60)
        self.combo_device.pack(anchor='w', padx=20, pady=(0, 20))
        self.combo_device.set("Loading devices...")
        self.combo_device.bind("<<ComboboxSelected>>", self.on_device)
        
        # Async load devices
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

        # Mixer Button
        mixer_text = "Open Volume Mixer"
        if platform.system() == "Linux": mixer_text = "Open Linux Audio Control"
        
        tk.Button(frame, text=mixer_text, command=self.open_mixer,
                  bg="#333333", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20, pady=20)
        
        # Danger Zone
        tk.Label(frame, text="Reset Data", bg="#1e1e1e", fg="#ff5555", font=("Segoe UI", 10, "bold")).pack(anchor='w', padx=20, pady=(20,5))
        tk.Button(frame, text="Factory Reset (Wipe All Data)", command=self.wipe_data,
                  bg="#880000", fg="white", relief="flat", padx=10, pady=5).pack(anchor='w', padx=20)

    def build_logs(self, frame):
        self.log_text = scrolledtext.ScrolledText(frame, bg="#101010", fg="#00ff00", font=("Consolas", 9), state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=5, pady=5)
        self.update_logs()

    def load_devices(self):
        self.dev_map = {}
        try:
            # Add delay to ensure VLC is ready
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
        # Open in thread/process to avoid freezing
        sys_os = platform.system()
        try:
            if sys_os == "Windows":
                subprocess.Popen(["start", "ms-settings:apps-volume"], shell=True)
            elif sys_os == "Linux":
                # Try standard linux mixers
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
        save_setting("mute_spotify", self.manager.mute_spotify)
        save_setting("auto_favorite", self.manager.auto_favorite)

    def wipe_data(self):
        if messagebox.askyesno("Reset", "Delete all settings and login data? App will close."):
            logging.shutdown()
            try:
                # Wipe Credentials from Keyring
                try: keyring.delete_password(KEYRING_SERVICE, "tidal_session")
                except: pass
                try: keyring.delete_password(KEYRING_SERVICE, "spotify_token")
                except: pass
                # Wipe Migrated Credentials
                try: keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_ID")
                except: pass
                try: keyring.delete_password(KEYRING_SERVICE, "SPOTIFY_CLIENT_SECRET")
                except: pass
                
                # Wipe Files
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
        self.geometry("400x700")
        self.configure(bg="#121212")
        self.last_img = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Styles
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Main.TLabel", background="#121212", foreground="white", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#121212", foreground="#888888", font=("Segoe UI", 9))
        
        # UI - UPDATED LABEL (No fixed size to allow image to dictate size)
        self.lbl_art = tk.Label(self, bg="#121212", text="[No Art]", fg="#444444")
        self.lbl_art.pack(pady=30)
        
        self.lbl_track = ttk.Label(self, text="Waiting for Spotify...", font=("Segoe UI", 13, "bold"), 
                                   wraplength=380, justify="center", style="Main.TLabel")
        self.lbl_track.pack(pady=(0,5))
        
        self.lbl_status = ttk.Label(self, text="Status: Initializing", style="Status.TLabel")
        self.lbl_status.pack(pady=(0,10))
        
        self.lbl_time = ttk.Label(self, text="0:00", style="Main.TLabel")
        self.lbl_time.pack(pady=5)

        # Controls
        ctrl_frame = tk.Frame(self, bg="#121212")
        ctrl_frame.pack(pady=20)
        
        btn_style = {"bg": "#282828", "fg": "white", "relief": "flat", "font": ("Segoe UI", 10), "activebackground": "#404040", "activeforeground": "white"}
        
        tk.Button(ctrl_frame, text="<<", command=manager.prev_track, width=5, **btn_style).pack(side='left', padx=5)
        tk.Button(ctrl_frame, text="Play/Pause", command=manager.toggle_play, width=10, **btn_style).pack(side='left', padx=5)
        tk.Button(ctrl_frame, text=">>", command=manager.next_track, width=5, **btn_style).pack(side='left', padx=5)

        # Fix Match Button
        tk.Button(self, text="⚠ Report Wrong Song / Fix Match", command=self.open_manual_match, 
                  bg="#552222", fg="#ffbbbb", relief="flat", font=("Segoe UI", 9)).pack(pady=20)

        # Settings
        tk.Button(self, text="Settings", command=self.open_settings, 
                  bg="#1a1a1a", fg="#888888", relief="flat").pack(side='bottom', pady=20, fill='x')

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
        self.lbl_time.config(text=info['vlc_time'])
        
        url = info.get('image_url')
        if url != self.last_img:
            self.last_img = url
            if url:
                try:
                    data = requests.get(url).content
                    img = Image.open(BytesIO(data))
                    # UPDATED RESIZE for better quality & size
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