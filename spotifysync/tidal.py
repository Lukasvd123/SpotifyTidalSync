"""Tidal session, matching and stream resolution."""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import credentials
from .config import MATCH_CACHE_FILE, logger
from .matching import (CONFIDENT_SCORE, NOT_ON_TIDAL, MappingStore, TrackQuery, describe,
                       legacy_mapping_key, make_mapping_key, pick_best, search_title)

# Tidal stream URLs are signed and time limited. Treat a prefetched one as stale
# well before that so we never hand VLC a dead link.
STREAM_URL_TTL = 240.0

_QUALITY_ORDER = ("hi_res_lossless", "high_lossless", "low_320k", "low_96k",
                  "lossless", "high", "low")

# A 401 for one quality is usually about that one track (no hi-res master in
# this region). Only give up on the quality for the whole session after this
# many different tracks in a row were refused.
QUALITY_STRIKES = 3


class LoginResult:
    OK = "ok"
    OFFLINE = "offline"
    FAILED = "failed"


def _tidalapi():
    import tidalapi
    return tidalapi


def _is_network_error(exc):
    try:
        import requests
        return isinstance(exc, (requests.ConnectionError, requests.Timeout))
    except ImportError:
        return False


def preferred_quality():
    quality = getattr(_tidalapi(), "Quality", None)
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


@dataclass
class Match:
    """How a source track was matched: method is mapping, cache, isrc or search."""
    track: object
    method: str
    score: float = 0.0

    @property
    def label(self):
        if self.method == "search":
            return f"search ({self.score:.0f})"
        return self.method


class TidalLibrary:
    def __init__(self, session=None):
        self.session = session
        self.mappings = MappingStore()
        self.auto_matches = MappingStore(MATCH_CACHE_FILE)
        self._strikes = {}          # quality -> set of track ids refused in a row
        self._disabled = set()

    # --- login -----------------------------------------------------------
    def login(self, open_browser=None):
        """Log in, preferring the cached session. Returns a LoginResult."""
        tidalapi = _tidalapi()
        quality = preferred_quality()
        if not quality:
            logger.error("tidalapi exposes no Quality enum; cannot continue")
            return LoginResult.FAILED

        if self.session is None:
            self.session = tidalapi.Session(config=tidalapi.Config(quality=quality))

        try:
            if self._load_cached_session():
                logger.info("Tidal: logged in from cached session")
                # tidalapi refreshes an expired access token while loading;
                # save it so the next start does not have to again.
                self._save_session()
                return LoginResult.OK
        except Exception as e:
            if _is_network_error(e):
                logger.warning(f"Tidal: offline ({e.__class__.__name__}), will retry")
                return LoginResult.OFFLINE
            logger.warning(f"Cached Tidal session unusable: {e}")

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
                return LoginResult.OK
            logger.error("Tidal: login did not complete")
            return LoginResult.FAILED
        except Exception as e:
            if _is_network_error(e):
                logger.warning(f"Tidal: offline during login ({e.__class__.__name__})")
                return LoginResult.OFFLINE
            logger.error(f"Tidal login failed: {e}")
            return LoginResult.FAILED

    def _load_cached_session(self):
        data = credentials.get_json(credentials.TIDAL_SESSION_KEY)
        if not data or not data.get("access_token"):
            return False
        expiry = None
        if data.get("expiry_time"):
            expiry = datetime.fromtimestamp(data["expiry_time"])
        loaded = self.session.load_oauth_session(
            data.get("token_type", "Bearer"),
            data.get("access_token"),
            data.get("refresh_token"),
            expiry,
        )
        if loaded is False:
            # Access token rejected outright: try the refresh token before
            # making the user log in again.
            refresh = data.get("refresh_token")
            if not refresh or not self.session.token_refresh(refresh):
                return False
            loaded = self.session.load_oauth_session(
                self.session.token_type, self.session.access_token,
                self.session.refresh_token or refresh, self.session.expiry_time)
        return bool(loaded) and self.session.check_login()

    def _save_session(self):
        expiry = getattr(self.session, "expiry_time", None)
        credentials.set_json(credentials.TIDAL_SESSION_KEY, {
            "token_type": self.session.token_type,
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expiry_time": expiry.timestamp() if expiry else None,
        })
        logger.debug("Tidal: session cached")

    def ensure_session(self):
        """Refresh the OAuth token if it has expired."""
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
            result = self.session.search(query, models=[_tidalapi().media.Track], limit=limit)
        except Exception as e:
            logger.warning(f"Tidal search error: {e}")
            return []
        if isinstance(result, dict):
            return result.get("tracks", []) or []
        return getattr(result, "tracks", []) or []

    @staticmethod
    def _keys(query, spotify_id=None):
        return (make_mapping_key(query.title, query.artist), spotify_id,
                legacy_mapping_key(query.title, query.artist))

    def is_not_on_tidal(self, query, spotify_id=None):
        return self.mappings.lookup(*self._keys(query, spotify_id)) is NOT_ON_TIDAL

    def find_match(self, query, spotify_id=None):
        """Resolve a source track to a Match, honouring user mappings.

        Searches progressively and stops at the first confident result: ISRC,
        then title + artist, title + album, and finally title alone (where the
        artist and duration gates in the scorer do the filtering).
        """
        keys = self._keys(query, spotify_id)
        mapped = self.mappings.lookup(*keys)
        if mapped is NOT_ON_TIDAL:
            return None
        if mapped:
            track = self.track_by_id(mapped)
            if track:
                logger.info(f"Using saved mapping for '{query.title}'")
                return Match(track, "mapping")

        cached = self.auto_matches.lookup(*keys[:2])
        if cached:
            track = self.track_by_id(cached)
            if track:
                logger.info(f"Using cached match for '{query.title}'")
                return Match(track, "cache")

        match = self._search_match(query)
        if match and (match.method == "isrc" or match.score >= CONFIDENT_SCORE):
            self.auto_matches.put(keys[0], match.track.id)
        return match

    def _search_match(self, query):
        if query.isrc:
            tracks = self._by_isrc(query.isrc)
            if tracks:
                track, score = pick_best(tracks, query)
                # Same recording by definition; the scorer just picks the best
                # of several releases and guards against bad ISRC data.
                if track is not None:
                    logger.info(f"Matched '{query.title}' by ISRC")
                    return Match(track, "isrc", score)

        title = search_title(query.title)
        queries = [f"{title} {query.artist}".strip()]
        if query.album and query.album.lower() != title.lower():
            queries.append(f"{title} {query.album}")
        queries.append(title)

        seen, pool = set(), []
        best, best_score = None, float("-inf")
        for text in dict.fromkeys(queries):
            logger.info(f"Searching Tidal: '{text}'")
            for cand in self.search(text):
                cand_id = getattr(cand, "id", id(cand))
                if cand_id not in seen:
                    seen.add(cand_id)
                    pool.append(cand)
            best, best_score = pick_best(pool, query)
            if best is not None and best_score >= CONFIDENT_SCORE:
                break

        if best is None:
            logger.warning(f"No confident match for '{query.title}' by '{query.artist}' "
                           f"(best score {best_score:.0f})")
            return None
        logger.info(f"Matched '{describe(best).title}' by '{query.artist}' (score {best_score:.0f})")
        return Match(best, "search", best_score)

    def _by_isrc(self, isrc):
        getter = getattr(self.session, "get_tracks_by_isrc", None)
        if getter is None:
            return []
        try:
            return getter(isrc) or []
        except Exception as e:
            logger.debug(f"ISRC lookup failed for {isrc}: {e}")
            return []

    def save_mapping(self, key, tidal_id, spotify_id=None):
        self.mappings.put(key, tidal_id)
        self.auto_matches.remove(key, spotify_id)

    def mark_not_on_tidal(self, key, spotify_id=None):
        self.mappings.put(key, NOT_ON_TIDAL)
        self.auto_matches.remove(key, spotify_id)

    # --- streaming -------------------------------------------------------
    def _qualities(self):
        quality = _tidalapi().Quality
        order = [getattr(quality, name) for name in _QUALITY_ORDER if hasattr(quality, name)]
        order = list(dict.fromkeys(order))
        usable = [q for q in order if q not in self._disabled]
        return usable or order or [preferred_quality()]

    def stream_url(self, track):
        """Return (url, quality_label), or (None, None)."""
        for quality in self._qualities():
            url, label = self._try_quality(track, quality)
            if url:
                self._strikes.pop(quality, None)
                return url, label
        logger.error(f"Could not stream '{track.name}' at any quality")
        return None, None

    def _refused(self, quality, track, label):
        strikes = self._strikes.setdefault(quality, set())
        strikes.add(getattr(track, "id", id(track)))
        if len(strikes) >= QUALITY_STRIKES and quality not in self._disabled:
            logger.info(f"Quality {label} refused for {len(strikes)} tracks in a row; "
                        "skipping it for this session")
            self._disabled.add(quality)
        else:
            logger.info(f"Quality {label} not available for '{track.name}'")

    def _try_quality(self, track, quality):
        label = str(quality).split(".")[-1].upper()
        try:
            self.session.config.quality = quality
            url = track.get_url()
            if url:
                return url, label
        except Exception as e:
            if "401" in str(e):
                self._refused(quality, track, label)
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


def query_from_api_item(item):
    artists = tuple(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))
    return TrackQuery(
        title=item.get("name") or "",
        artist=artists[0] if artists else "",
        artists=artists,
        album=(item.get("album") or {}).get("name") or "",
        duration_ms=item.get("duration_ms") or 0,
        isrc=(item.get("external_ids") or {}).get("isrc") or "",
    )


def _run_in_thread(fn):
    threading.Thread(target=fn, daemon=True).start()


class Prefetcher:
    """Works ahead on the Spotify queue so track changes do not wait on Tidal.

    At track start the next few queue entries are matched (a search each).
    Near the end of the track the immediate next one also gets its stream URL,
    late enough that the signed URL is still fresh when we get there; fetching
    it at track start left it expired for any song longer than the TTL.
    """

    def __init__(self, library, source, run_async=_run_in_thread):
        self.library = library
        self.source = source
        self._run_async = run_async
        self._lock = threading.Lock()
        self._cache = {}
        self._busy = False
        self._next_keys = ()

    def get(self, *keys):
        with self._lock:
            for key in keys:
                if key and key in self._cache:
                    return self._cache[key]
        return None

    @property
    def next_up(self):
        """Cached entry for the first track in the queue, if matched."""
        return self.get(*self._next_keys) if self._next_keys else None

    def _store(self, keys, resolved):
        with self._lock:
            for key in keys:
                if key:
                    self._cache[key] = resolved

    def _prune(self, keep):
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k in keep}

    def _claim(self):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def _release(self):
        with self._lock:
            self._busy = False

    def start(self, current_keys=()):
        """Match the upcoming queue in the background."""
        if not self.source.api_available or not self._claim():
            return
        self._run_async(lambda: self._match_upcoming(set(current_keys)))

    def warm_next(self):
        """Fetch the stream URL for the next track in the background."""
        entry = self.next_up
        if entry is None or entry.url_fresh or not self._claim():
            return
        self._run_async(lambda: self._resolve_next(entry))

    def _match_upcoming(self, keep):
        try:
            upcoming = self.source.upcoming(limit=3)
            logger.debug(f"Prefetch: queue has {len(upcoming)} upcoming track(s)")
            entries = []
            for item in upcoming:
                query = query_from_api_item(item)
                if not query.title or not query.artist:
                    continue
                keys = (make_mapping_key(query.title, query.artist), item.get("id"))
                keep.update(keys)
                entries.append((query, keys, item.get("id")))
            self._prune(keep)
            self._next_keys = entries[0][1] if entries else ()

            for query, keys, spotify_id in entries:
                if self.get(*keys) or self.library.is_not_on_tidal(query, spotify_id):
                    continue
                match = self.library.find_match(query, spotify_id=spotify_id)
                if match:
                    self._store(keys, Resolved(track=match.track))
                    logger.debug(f"Prefetched match: {query.title}")
        except Exception as e:
            logger.debug(f"Prefetch error: {e}")
        finally:
            self._release()

    def _resolve_next(self, entry):
        try:
            resolved = self.library.resolve(entry.track)
            if resolved:
                self._store(self._next_keys, resolved)
                logger.debug(f"Prefetched stream: {entry.track.name}")
        except Exception as e:
            logger.debug(f"Prefetch stream error: {e}")
        finally:
            self._release()
