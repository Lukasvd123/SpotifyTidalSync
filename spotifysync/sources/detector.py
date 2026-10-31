"""OS-level media detection: MPRIS on Linux, SMTC on Windows.

Used as the primary source when the Spotify Web API is off, and as the fallback
(and control channel) when it is on. Reading and controlling always target the
same session: controlling "whatever the OS thinks is current" paused browsers
and video players instead of Spotify.
"""

import asyncio
import datetime
import threading
import time

from ..config import IS_LINUX, IS_WINDOWS, logger

SMTC_AVAILABLE = False
if IS_WINDOWS:
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

DBUS_AVAILABLE = False
dbus = None
if IS_LINUX:
    try:
        import dbus as _dbus
        dbus = _dbus
        DBUS_AVAILABLE = True
    except ImportError:
        pass

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# Media sessions we must ignore: our own playback, and the Tidal app itself.
IGNORED_APPS = {"vlc", "vlc media player", "tidal", "spotifytidalsync", "spotifysync"}

_STATUS_MAP = {"Playing": "playing", "Paused": "paused", "Stopped": "stopped"}

# Spotify's desktop client shows ads under these titles with no artist.
_AD_TITLES = {"advertisement", "spotify"}


class DetectedMedia:
    __slots__ = ("title", "artist", "artists", "album", "status", "app_name",
                 "position_ms", "duration_ms", "spotify_id", "kind", "art_url")

    def __init__(self, title="", artist="", album="", status="unknown", app_name="",
                 position_ms=-1, duration_ms=0, artists=(), spotify_id="",
                 kind="track", art_url=""):
        self.title = title or ""
        self.artist = artist or ""
        self.artists = tuple(artists) or ((self.artist,) if self.artist else ())
        self.album = album or ""
        self.status = status
        self.app_name = app_name
        self.position_ms = position_ms
        self.duration_ms = duration_ms
        self.spotify_id = spotify_id
        self.kind = kind
        self.art_url = art_url

    @property
    def is_playing(self):
        return self.status == "playing"


# --- pure helpers (unit tested) ----------------------------------------------
def mpris_app(service):
    return service.replace(MPRIS_PREFIX, "").split(".")[0]


def pick_service(entries, preferred):
    """Choose a player from [(service, app, status)].

    Order: the preferred app, then anything playing, then the first one. The
    old single pass returned a playing browser tab that happened to be listed
    before Spotify.
    """
    usable = [e for e in entries if e[1].lower() not in IGNORED_APPS]
    for entry in usable:
        if entry[1].lower() == preferred.lower():
            return entry[0]
    for entry in usable:
        if entry[2] == "Playing":
            return entry[0]
    return usable[0][0] if usable else None


def parse_spotify_trackid(trackid):
    """('track'|'episode'|'ad'|'local', id) from an MPRIS trackid or URI."""
    if not trackid:
        return "track", ""
    text = str(trackid)
    for sep in ("/com/spotify/", "spotify:"):
        if sep in text:
            rest = text.split(sep, 1)[1].replace(":", "/")
            kind, _, ident = rest.partition("/")
            return kind or "track", ident.split("/")[0]
    return "track", ""


def classify(title, artist, kind):
    """Map a reading to track / episode / ad."""
    if kind in ("ad", "episode"):
        return kind
    if not artist and (title or "").strip().lower() in _AD_TITLES:
        return "ad"
    return "track"


def parse_mpris(meta, status, position_us, app):
    """DetectedMedia from raw MPRIS properties, or None without a title."""
    title = str(meta.get("xesam:title", "") or "")
    if not title:
        return None
    artists = tuple(str(a) for a in (meta.get("xesam:artist") or []) if str(a))
    kind, ident = parse_spotify_trackid(meta.get("mpris:trackid", ""))
    try:
        duration_ms = int(meta.get("mpris:length", 0) or 0) // 1000
    except (TypeError, ValueError):
        duration_ms = 0
    position_ms = -1
    if position_us is not None:
        try:
            position_ms = int(position_us) // 1000
        except (TypeError, ValueError):
            pass
    artist = artists[0] if artists else ""
    return DetectedMedia(
        title=title, artist=artist, artists=artists,
        album=str(meta.get("xesam:album", "") or ""),
        status=_STATUS_MAP.get(str(status), "unknown"),
        app_name=app, position_ms=position_ms, duration_ms=duration_ms,
        spotify_id=ident if app.lower() == "spotify" and kind == "track" else "",
        kind=classify(title, artist, kind) if app.lower() == "spotify" else "track",
        art_url=str(meta.get("mpris:artUrl", "") or ""),
    )


def extrapolate_position(position_ms, last_updated, now, playing, duration_ms=0):
    """SMTC only refreshes the timeline on events (play, pause, seek, track
    change), so its position goes stale and then jumps. Project it forward from
    when it was last updated."""
    if position_ms < 0 or not playing or last_updated is None:
        return position_ms
    elapsed = (now - last_updated).total_seconds()
    if elapsed <= 0:
        return position_ms
    projected = position_ms + int(elapsed * 1000)
    if duration_ms > 0:
        projected = min(projected, duration_ms)
    return projected


def identify_app(raw_id):
    if not raw_id:
        return "unknown"
    low = raw_id.lower()
    for key, name in (("spotify", "Spotify"), ("tidal", "Tidal"), ("vlc", "VLC")):
        if key in low:
            return name
    return raw_id.split(".")[-1].split("!")[-1] or raw_id


class MediaDetector:
    def __init__(self):
        self._last_error = 0.0
        self._loop = None
        self._manager = None
        self._service = None       # MPRIS service last read
        self._session_app = None   # SMTC app id last read
        self._trackid = None       # MPRIS trackid object path, for SetPosition
        if SMTC_AVAILABLE:
            self._start_loop()

    # --- windows event loop ----------------------------------------------
    def _start_loop(self):
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(2.0)

    def _run_async(self, coro):
        if self._loop is None:
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=3.0)
        except Exception:
            return None

    # --- public ----------------------------------------------------------
    @property
    def available(self):
        return SMTC_AVAILABLE or DBUS_AVAILABLE

    def read(self, preferred="spotify"):
        if SMTC_AVAILABLE:
            return self._run_async(self._read_windows(preferred))
        if DBUS_AVAILABLE:
            return self._read_linux(preferred)
        self._warn("No media detection available (install dbus-python or winrt)")
        return None

    def send_control(self, command):
        """command: play | pause | play_pause | next | previous. Returns success."""
        if SMTC_AVAILABLE:
            return bool(self._run_async(self._control_windows(command)))
        if DBUS_AVAILABLE:
            return self._control_linux(command)
        return False

    def seek(self, position_ms):
        if SMTC_AVAILABLE:
            return bool(self._run_async(self._seek_windows(position_ms)))
        if DBUS_AVAILABLE:
            return self._seek_linux(position_ms)
        return False

    # --- linux -----------------------------------------------------------
    def _player_props(self, bus, service):
        proxy = bus.get_object(service, MPRIS_PATH)
        return dbus.Interface(proxy, "org.freedesktop.DBus.Properties")

    def _linux_entries(self, bus):
        entries = []
        for service in bus.list_names():
            if not service.startswith(MPRIS_PREFIX):
                continue
            app = mpris_app(service)
            status = ""
            if app.lower() not in IGNORED_APPS:
                try:
                    status = str(self._player_props(bus, service).Get(PLAYER_IFACE, "PlaybackStatus"))
                except Exception:
                    pass
            entries.append((str(service), app, status))
        return entries

    def _read_linux(self, preferred):
        try:
            bus = dbus.SessionBus()
            service = pick_service(self._linux_entries(bus), preferred)
            self._service = service
            if service is None:
                return None
            props = self._player_props(bus, service)
            meta = props.Get(PLAYER_IFACE, "Metadata")
            status = props.Get(PLAYER_IFACE, "PlaybackStatus")
            try:
                position = props.Get(PLAYER_IFACE, "Position")
            except Exception:
                position = None
            self._trackid = meta.get("mpris:trackid")
            return parse_mpris(meta, status, position, mpris_app(service))
        except Exception as e:
            self._warn(f"MPRIS error: {e}")
            return None

    def _linux_player(self):
        bus = dbus.SessionBus()
        service = self._service
        if service is None or service not in bus.list_names():
            service = pick_service(self._linux_entries(bus), "spotify")
        if service is None:
            return None
        return dbus.Interface(bus.get_object(service, MPRIS_PATH), PLAYER_IFACE)

    def _control_linux(self, command):
        try:
            player = self._linux_player()
            if player is None:
                return False
            action = {
                "play": player.Play, "pause": player.Pause,
                "play_pause": player.PlayPause,
                "next": player.Next, "previous": player.Previous,
            }.get(command)
            if action:
                action()
                return True
        except Exception as e:
            logger.debug(f"MPRIS control error: {e}")
        return False

    def _seek_linux(self, position_ms):
        try:
            player = self._linux_player()
            if player is None or not self._trackid:
                return False
            player.SetPosition(dbus.ObjectPath(self._trackid), dbus.Int64(int(position_ms) * 1000))
            return True
        except Exception as e:
            logger.debug(f"MPRIS seek error: {e}")
            return False

    # --- windows ---------------------------------------------------------
    async def _manager_async(self):
        if self._manager is None:
            self._manager = await SessionManager.request_async()
        return self._manager

    async def _pick_session(self, preferred):
        manager = await self._manager_async()
        sessions = list(manager.get_sessions() or [])
        entries = []
        by_id = {}
        for index, session in enumerate(sessions):
            app = identify_app(session.source_app_user_model_id)
            info = session.get_playback_info()
            playing = info is not None and info.playback_status == PlaybackStatus.PLAYING
            key = f"{index}:{session.source_app_user_model_id}"
            by_id[key] = session
            entries.append((key, app, "Playing" if playing else ""))
        chosen = pick_service(entries, preferred)
        return by_id.get(chosen) if chosen else None

    async def _read_windows(self, preferred):
        try:
            session = await self._pick_session(preferred)
            if session is None:
                return None
            self._session_app = session.source_app_user_model_id

            props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            title = props.title or ""
            if not title:
                return None

            status = "unknown"
            if info:
                status = {
                    PlaybackStatus.PLAYING: "playing",
                    PlaybackStatus.PAUSED: "paused",
                    PlaybackStatus.STOPPED: "stopped",
                    PlaybackStatus.CLOSED: "stopped",
                }.get(info.playback_status, "unknown")

            position_ms, duration_ms = -1, 0
            try:
                timeline = session.get_timeline_properties()
                if timeline:
                    duration_ms = int(timeline.end_time.total_seconds() * 1000)
                    position_ms = extrapolate_position(
                        int(timeline.position.total_seconds() * 1000),
                        getattr(timeline, "last_updated_time", None),
                        datetime.datetime.now(datetime.timezone.utc),
                        status == "playing", duration_ms)
            except Exception:
                pass

            app = identify_app(session.source_app_user_model_id)
            artist = props.artist or ""
            return DetectedMedia(
                title=title, artist=artist, album=props.album_title or "",
                status=status, app_name=app,
                position_ms=position_ms, duration_ms=duration_ms,
                kind=classify(title, artist, "track") if app == "Spotify" else "track",
            )
        except Exception as e:
            self._manager = None
            self._warn(f"SMTC error: {e}")
            return None

    async def _windows_session(self):
        manager = await self._manager_async()
        for session in manager.get_sessions() or []:
            if session.source_app_user_model_id == self._session_app:
                return session
        return await self._pick_session("spotify")

    async def _control_windows(self, command):
        try:
            session = await self._windows_session()
            if session is None:
                return False
            action = {
                "play": session.try_play_async,
                "pause": session.try_pause_async,
                "play_pause": session.try_toggle_play_pause_async,
                "next": session.try_skip_next_async,
                "previous": session.try_skip_previous_async,
            }.get(command)
            if action:
                return bool(await action())
        except Exception as e:
            self._manager = None
            logger.debug(f"SMTC control error: {e}")
        return False

    async def _seek_windows(self, position_ms):
        try:
            session = await self._windows_session()
            if session is None:
                return False
            # SMTC positions are in 100ns ticks.
            return bool(await session.try_change_playback_position_async(int(position_ms) * 10000))
        except Exception as e:
            logger.debug(f"SMTC seek error: {e}")
            return False

    def _warn(self, msg):
        now = time.time()
        if now - self._last_error > 30:
            logger.warning(msg)
            self._last_error = now
