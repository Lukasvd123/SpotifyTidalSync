"""Tidal session, matching and stream resolution."""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import tidalapi

from . import credentials
from .config import logger
from .matching import MappingStore, make_mapping_key, pick_best

# Tidal stream URLs are signed and time limited. Treat a prefetched one as stale
# well before that so we never hand VLC a dead link.
STREAM_URL_TTL = 240.0

_QUALITY_ORDER = ("hi_res_lossless", "high_lossless", "low_320k", "low_96k",
                  "lossless", "high", "low")


def preferred_quality():
    quality = getattr(tidalapi, "Quality", None)
    if quality is None:
        return None
    for name in ("hi_res_lossless", "high_lossless", "low_320k", "lossless",
                 "LOSSLESS", "high", "HIGH"):
        if hasattr(quality, name):
            return getattr(quality, name)
    return None


@dataclass
class Resolved:
    """A Tidal track plus, when already fetched, a playable stream URL."""
    track: object
    url: str = ""
    quality: str = ""
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def url_fresh(self):
        return bool(self.url) and (time.monotonic() - self.fetched_at) < STREAM_URL_TTL


class TidalLibrary:
    def __init__(self):
        self.session = None
        self.mappings = MappingStore()
        self._unauthorized = set()

    # --- login -----------------------------------------------------------
    def login(self, open_browser=None):
        quality = preferred_quality()
        if not quality:
            logger.error("tidalapi exposes no Quality enum; cannot continue")
            return False

        self.session = tidalapi.Session(config=tidalapi.Config(quality=quality))

        if self._load_cached_session() and self.session.check_login():
            logger.info("Tidal: logged in from cached session")
            return True

        logger.info("Tidal: login required")
        try:
            result = self.session.login_oauth()
            link = result[0] if isinstance(result, (tuple, list)) else result
            url = (getattr(link, "verification_uri_complete", None)
                   or getattr(link, "verificationUriComplete", None))
            if url and open_browser:
                open_browser(url if url.startswith("http") else "https://" + url)

            if isinstance(result, (tuple, list)) and len(result) > 1:
                result[1].result()
            else:
                self.session.complete_login(link)

            if self.session.check_login():
                self._save_session()
                return True
            logger.error("Tidal: login did not complete")
            return False
        except Exception as e:
            logger.error(f"Tidal login failed: {e}")
            return False

    def _load_cached_session(self):
        data = credentials.get_json(credentials.TIDAL_SESSION_KEY)
        if not data:
            return False
        try:
            expiry = None
            if data.get("expiry_time"):
                expiry = datetime.fromtimestamp(data["expiry_time"])
            self.session.load_oauth_session(
                data.get("token_type", "Bearer"),
                data.get("access_token"),
                data.get("refresh_token"),
                expiry,
            )
            return True
        except Exception as e:
            logger.warning(f"Cached Tidal session unusable: {e}")
            return False

    def _save_session(self):
        expiry = self.session.expiry_time
        credentials.set_json(credentials.TIDAL_SESSION_KEY, {
            "token_type": self.session.token_type,
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expiry_time": expiry.timestamp() if expiry else None,
        })
        logger.info("Tidal: session cached")

    def ensure_session(self):
        """Refresh the OAuth token if it has expired.

        The old check_and_refresh_session() called check_login() twice and
        returned; it never actually refreshed anything.
        """
        try:
            if self.session.check_login():
                return True
            logger.warning("Tidal session expired, refreshing...")
            if self.session.token_refresh(self.session.refresh_token):
                self._save_session()
                logger.info("Tidal: token refreshed")
                return True
        except Exception as e:
            logger.error(f"Tidal token refresh failed: {e}")
        return False

    # --- lookup ----------------------------------------------------------
    def track_by_id(self, tidal_id):
        try:
            return self.session.track(tidal_id)
        except Exception:
            return None

    def search(self, query, limit=15):
        try:
            result = self.session.search(query, models=[tidalapi.media.Track], limit=limit)
        except Exception as e:
            logger.warning(f"Tidal search error: {e}")
            return []
        if isinstance(result, dict):
            return result.get("tracks", []) or []
        return getattr(result, "tracks", []) or []

    def find_match(self, title, artist, duration_ms=0, spotify_id=None):
        """Resolve a source track to a Tidal track, honouring user mappings."""
        key = make_mapping_key(title, artist)
        mapped = self.mappings.lookup(key, spotify_id)
        if mapped:
            track = self.track_by_id(mapped)
            if track:
                logger.info(f"Using saved mapping for '{title}'")
                return track

        clean = title.split("(")[0].split("-")[0].strip()
        query = f"{clean} {artist}".strip()
        logger.info(f"Searching Tidal: '{query}'")
        candidates = self.search(query)
        if not candidates:
            logger.warning(f"No Tidal results for '{title}' by '{artist}'")
            return None

        match = pick_best(candidates, title, artist, duration_ms)
        if match is None:
            logger.warning(f"No confident match for '{title}' by '{artist}'")
        return match

    def save_mapping(self, key, tidal_id):
        self.mappings.put(key, tidal_id)

    # --- streaming -------------------------------------------------------
    def _qualities(self):
        quality = tidalapi.Quality
        order = [getattr(quality, name) for name in _QUALITY_ORDER if hasattr(quality, name)]
        usable = [q for q in order if q not in self._unauthorized]
        return usable or order or [preferred_quality()]

    def stream_url(self, track):
        """Return (url, quality_label), or (None, None)."""
        for quality in self._qualities():
            url, label = self._try_quality(track, quality)
            if url:
                return url, label
        logger.error(f"Could not stream '{track.name}' at any quality")
        return None, None

    def _try_quality(self, track, quality):
        label = str(quality).split(".")[-1].upper()
        try:
            self.session.config.quality = quality
            url = track.get_url()
            if url:
                return url, label
        except Exception as e:
            if "401" in str(e):
                if quality not in self._unauthorized:
                    logger.info(f"Quality {label} not available on this account")
                    self._unauthorized.add(quality)
                return None, None
            logger.debug(f"get_url({label}) failed: {e}")

        try:
            self.session.config.quality = quality
            stream = track.get_stream()
            # MPEG-DASH manifests are segment lists VLC cannot play directly.
            if getattr(stream, "is_mpd", False):
                return None, None
            manifest = stream.get_stream_manifest()
            if manifest.urls:
                return manifest.urls[0], str(stream.audio_quality).split(".")[-1].upper()
        except Exception as e:
            logger.debug(f"get_stream({label}) failed: {e}")
        return None, None

    def resolve(self, track):
        """Match plus stream URL, ready to hand straight to VLC."""
        url, quality = self.stream_url(track)
        if not url:
            return None
        return Resolved(track=track, url=url, quality=quality)

    def favorite(self, track_id):
        for attempt in (lambda: self.session.user.favorites.add_track(track_id),
                        lambda: self.session.add_favorite(track_id)):
            try:
                attempt()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def cover_url(track, size=640):
        """Album art straight off the Tidal track, no third-party lookup."""
        try:
            return track.album.image(size)
        except Exception:
            return ""


class Prefetcher:
    """Resolves upcoming queue tracks in the background.

    Matching costs a Tidal search and fetching a stream URL costs several more
    round trips; doing both at the boundary is what makes track changes drag.
    """

    def __init__(self, library, source):
        self.library = library
        self.source = source
        self._lock = threading.Lock()
        self._cache = {}
        self._busy = False

    def get(self, *keys):
        with self._lock:
            for key in keys:
                if key and key in self._cache:
                    return self._cache[key]
        return None

    def _store(self, keys, resolved):
        with self._lock:
            for key in keys:
                if key:
                    self._cache[key] = resolved

    def _prune(self, keep):
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k in keep}

    def start(self, current_keys=()):
        if not self.source.api_available:
            return
        with self._lock:
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._run, args=(set(current_keys),), daemon=True).start()

    def _run(self, keep):
        try:
            upcoming = self.source.upcoming(limit=3)
            for track in upcoming:
                artists = track.get("artists") or []
                if not artists:
                    continue
                keep.add(make_mapping_key(track["name"], artists[0]["name"]))
                keep.add(track.get("id"))
            self._prune(keep)

            for position, track in enumerate(upcoming):
                artists = track.get("artists") or []
                if not artists:
                    continue
                keys = (make_mapping_key(track["name"], artists[0]["name"]), track.get("id"))
                if self.get(*keys):
                    continue

                match = self.library.find_match(
                    title=track["name"],
                    artist=artists[0]["name"],
                    duration_ms=track.get("duration_ms", 0),
                    spotify_id=track.get("id"),
                )
                if not match:
                    continue

                # Only pre-resolve the stream URL for the immediate next track;
                # further ahead it would expire before we get there.
                resolved = self.library.resolve(match) if position == 0 else Resolved(track=match)
                if resolved:
                    self._store(keys, resolved)
                    logger.debug(f"Prefetched: {track['name']}"
                                 f"{' (+stream)' if resolved.url else ''}")
                time.sleep(1.0)
        except Exception as e:
            logger.debug(f"Prefetch error: {e}")
        finally:
            with self._lock:
                self._busy = False
