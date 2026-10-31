"""Optional Spotify Web API client.

Adds stable track ids, exact durations, queue prefetch and seek control. The app
works without it, falling back to OS media detection.
"""

from .. import credentials
from ..config import logger

SPOTIPY_AVAILABLE = False
try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    from spotipy.cache_handler import CacheHandler
    SPOTIPY_AVAILABLE = True
except ImportError:
    CacheHandler = object

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = ("user-read-playback-state user-modify-playback-state "
         "user-read-currently-playing")


class _KeyringCache(CacheHandler):
    def get_cached_token(self):
        return credentials.get_json(credentials.SPOTIFY_TOKEN_KEY)

    def save_token_to_cache(self, token_info):
        credentials.set_json(credentials.SPOTIFY_TOKEN_KEY, token_info)


class SpotifyController:
    def __init__(self):
        self.client = None
        self.connected = False

    @property
    def available(self):
        return self.connected and self.client is not None

    def login(self):
        if not SPOTIPY_AVAILABLE:
            logger.info("Spotify API: spotipy not installed, using OS media detection")
            return False

        client_id, client_secret = credentials.get_spotify_credentials()
        if not client_id or not client_secret:
            logger.info("Spotify API: no credentials configured, using OS media detection")
            return False

        try:
            auth = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=REDIRECT_URI,
                scope=SCOPE,
                cache_handler=_KeyringCache(),
            )
            self.client = spotipy.Spotify(auth_manager=auth)
            user = self.client.current_user()
            self.connected = True
            logger.info(f"Spotify API: connected as {user['display_name']}")
            return True
        except Exception as e:
            logger.warning(f"Spotify API login failed: {e}")
            self.client, self.connected = None, False
            return False

    def playback(self):
        """Raw current_playback() dict, or None."""
        if not self.available:
            return None
        try:
            return self.client.current_playback()
        except Exception as e:
            logger.debug(f"current_playback failed: {e}")
            return None

    def queue(self, limit=5):
        if not self.available:
            return []
        try:
            return (self.client.queue() or {}).get("queue", [])[:limit]
        except Exception as e:
            logger.debug(f"queue() failed: {e}")
            return []

    # Each returns True only when the API actually accepted the command, so the
    # caller can fall back to the OS media key. Spotify answers 403 "Restriction
    # violated" near track boundaries, which is exactly when we need these.
    def _command(self, name, *args):
        if not self.available:
            return False
        try:
            getattr(self.client, name)(*args)
            return True
        except Exception as e:
            reason = "restricted at track boundary" if "403" in str(e) else str(e)
            logger.debug(f"Spotify API {name} rejected ({reason}); using media key")
            return False

    def pause(self):
        return self._command("pause_playback")

    def resume(self):
        return self._command("start_playback")

    def next_track(self):
        return self._command("next_track")

    def previous_track(self):
        return self._command("previous_track")

    def seek(self, position_ms):
        return self._command("seek_track", int(max(0, position_ms)))
