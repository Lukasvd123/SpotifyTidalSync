"""Tidal session, matching and stream resolution."""

import atexit
import math
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    """A Tidal track plus, when already fetched, something the player can open:
    a stream URL or a local DASH manifest."""
    track: object
    url: str = ""
    quality: str = ""           # what Tidal actually sent, e.g. "FLAC 24-bit/96 kHz"
    lossless: bool = False
    gain_db: float = 0.0        # track ReplayGain, capped so the peak cannot clip
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


def format_label(codec, bit_depth, sample_rate, audio_quality=""):
    codec = (codec or "").upper()
    if "FLAC" in codec or "ALAC" in codec:
        name = "FLAC" if "FLAC" in codec else "ALAC"
        if bit_depth and sample_rate:
            return f"{name} {bit_depth}-bit/{sample_rate / 1000:g} kHz"
        return name
    kbps = {"HIGH": "320 kbps", "LOW": "96 kbps"}.get(str(audio_quality).split(".")[-1].upper())
    return f"AAC {kbps}" if kbps else "AAC"


def replaygain_db(gain, peak):
    if not gain:
        return 0.0
    gain = float(gain)
    if peak and float(peak) > 0:
        gain = min(gain, -20 * math.log10(float(peak)))
    return round(gain, 2)


_stream_dir = None


def _manifest_dir():
    global _stream_dir
    if _stream_dir is None:
        _stream_dir = tempfile.mkdtemp(prefix="spotifysync-")
        atexit.register(shutil.rmtree, _stream_dir, True)
    return _stream_dir


def write_manifest(track_id, manifest):
    """Store a DASH manifest where mpv can open it; drop ones older than an
    hour (their signed segment URLs are long dead by then)."""
    folder = _manifest_dir()
    now = time.time()
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(path) > 3600:
                os.remove(path)
        except OSError:
            pass
    path = os.path.join(folder, f"{track_id}-{int(now * 1000)}.mpd")
    data = manifest if isinstance(manifest, str) else manifest.decode("utf-8")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    return path


class TidalLibrary:
    def __init__(self, session=None):
        self.session = session
        self.mappings = MappingStore()
        self.auto_matches = MappingStore(MATCH_CACHE_FILE)
        self._strikes = {}          # quality -> set of track ids refused in a row
        self._disabled = set()
        self._warned_lossy = False

    @property
    def is_pkce(self):
        return bool(getattr(self.session, "is_pkce", False))

    # --- login -----------------------------------------------------------
    def login(self, open_browser=None, ask_redirect=None):
        """Log in, preferring the cached session. Returns a LoginResult.

        `ask_redirect(url)` shows the PKCE login and returns the address the
        browser ended on (None when cancelled). Tidal only streams FLAC to
        PKCE logins; the older device login gets AAC, so a cached one is
        offered an upgrade.
        """
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
                if not self.is_pkce and ask_redirect:
                    logger.info("Tidal: this login only gets AAC streams; "
                                "asking for a new one for lossless")
                    if not self.login_pkce(open_browser, ask_redirect):
                        logger.info("Tidal: keeping the old login (AAC only)")
                return LoginResult.OK
        except Exception as e:
            if _is_network_error(e):
                logger.warning(f"Tidal: offline ({e.__class__.__name__}), will retry")
                return LoginResult.OFFLINE
            logger.warning(f"Cached Tidal session unusable: {e}")

        logger.info("Tidal: login required")
        if ask_redirect:
            try:
                ok = self.login_pkce(open_browser, ask_redirect, raise_network=True)
            except Exception as e:
                logger.warning(f"Tidal: offline during login ({e.__class__.__name__})")
                return LoginResult.OFFLINE
            return LoginResult.OK if ok else LoginResult.FAILED
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

    def login_pkce(self, open_browser, ask_redirect, raise_network=False):
        """Browser login ending on a Tidal "Oops" page whose address the user
        pastes back. True once logged in and saved."""
        try:
            url = self.session.pkce_login_url()
            if open_browser:
                open_browser(url)
            redirect = ask_redirect(url)
            if not redirect:
                return False
            token = self.session.pkce_get_auth_token(redirect.strip())
            self.session.process_auth_token(token, is_pkce_token=True)
            if not self.session.check_login():
                logger.error("Tidal: login did not complete")
                return False
        except Exception as e:
            if raise_network and _is_network_error(e):
                raise
            logger.error(f"Tidal login failed: {e}")
            return False
        self._save_session()
        self._disabled.clear()
        self._strikes.clear()
        self._warned_lossy = False
        logger.info("Tidal: logged in (lossless and hi-res available)")
        return True

    def _load_cached_session(self):
        data = credentials.get_json(credentials.TIDAL_SESSION_KEY)
        if not data or not data.get("access_token"):
            return False
        expiry = None
        if data.get("expiry_time"):
            expiry = datetime.fromtimestamp(data["expiry_time"])
        is_pkce = bool(data.get("is_pkce"))
        loaded = self.session.load_oauth_session(
            data.get("token_type", "Bearer"),
            data.get("access_token"),
            data.get("refresh_token"),
            expiry,
            is_pkce=is_pkce,
        )
        if loaded is False:
            # Access token rejected outright: try the refresh token before
            # making the user log in again.
            refresh = data.get("refresh_token")
            if not refresh or not self.session.token_refresh(refresh):
                return False
            loaded = self.session.load_oauth_session(
                self.session.token_type, self.session.access_token,
                self.session.refresh_token or refresh, self.session.expiry_time,
                is_pkce=is_pkce)
        return bool(loaded) and self.session.check_login()

    def _save_session(self):
        expiry = getattr(self.session, "expiry_time", None)
        credentials.set_json(credentials.TIDAL_SESSION_KEY, {
            "token_type": self.session.token_type,
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expiry_time": expiry.timestamp() if expiry else None,
            "is_pkce": self.is_pkce,
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
        ids = self._isrc_ids(isrc)
        if ids is not None:
            # tidalapi fetches each release one by one; at a track boundary
            # that was seconds of silence for an ISRC with several releases.
            with ThreadPoolExecutor(max_workers=min(8, len(ids) or 1)) as pool:
                return [t for t in pool.map(self.track_by_id, ids) if t is not None]
        getter = getattr(self.session, "get_tracks_by_isrc", None)
        if getter is None:
            return []
        try:
            return getter(isrc) or []
        except Exception as e:
            logger.debug(f"ISRC lookup failed for {isrc}: {e}")
            return []

    def _isrc_ids(self, isrc):
        """Track ids for an ISRC from Tidal's v2 catalogue, or None when that
        endpoint is unavailable (then tidalapi's own lookup is used)."""
        request = getattr(self.session, "request", None)
        base = getattr(getattr(self.session, "config", None), "openapi_v2_location", None)
        if request is None or not base:
            return None
        try:
            res = request.request("GET", "tracks", params={"filter[isrc]": isrc},
                                  base_url=base).json()
            return [item["id"] for item in res.get("data") or []]
        except Exception as e:
            logger.debug(f"ISRC lookup failed for {isrc}: {e}")
            return None

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
        """A Resolved for `track` at up to `quality`, or None.

        The label comes from what Tidal actually returned: asking for hi-res
        used to be reported as hi-res while the older login got AAC.
        """
        label = str(quality).split(".")[-1].upper()
        try:
            self.session.config.quality = quality
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            if getattr(stream, "is_mpd", False):
                # PKCE logins get every format as MPEG-DASH segments.
                url = write_manifest(track.id, stream.get_manifest_data())
            else:
                url = manifest.urls[0] if manifest.urls else ""
            if url:
                codec = getattr(manifest, "codecs", "")
                return Resolved(
                    track=track, url=url,
                    quality=format_label(codec, getattr(stream, "bit_depth", 0),
                                         getattr(stream, "sample_rate", 0),
                                         getattr(stream, "audio_quality", "")),
                    lossless=any(c in str(codec).upper() for c in ("FLAC", "ALAC")),
                    gain_db=replaygain_db(getattr(stream, "track_replay_gain", 0),
                                          getattr(stream, "track_peak_amplitude", 0)))
        except Exception as e:
            if "401" in str(e):
                self._refused(quality, track, label)
                return None
            logger.debug(f"get_stream({label}) failed: {e}")

        try:
            self.session.config.quality = quality
            url = track.get_url()
            if url:
                return Resolved(track=track, url=url, quality=label)
        except Exception as e:
            if "401" in str(e):
                self._refused(quality, track, label)
            else:
                logger.debug(f"get_url({label}) failed: {e}")
        return None

    def resolve(self, track):
        """Match plus a playable stream, ready to hand straight to the player."""
        for quality in self._qualities():
            resolved = self._try_quality(track, quality)
            if resolved:
                self._strikes.pop(quality, None)
                if not resolved.lossless and not self._warned_lossy and not self.is_pkce:
                    self._warned_lossy = True
                    logger.warning("Tidal only streams AAC (lossy) to this login; "
                                   "log in again (Settings > Tidal) for FLAC and hi-res")
                return resolved
        logger.error(f"Could not stream '{track.name}' at any quality")
        return None

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

    def clear(self):
        with self._lock:
            self._cache = {}

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
