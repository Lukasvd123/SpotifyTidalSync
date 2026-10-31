"""OS-level media detection: MPRIS on Linux, SMTC on Windows.

Used as the primary source when the Spotify Web API is off, and as the fallback
(and control channel) when it is on.
"""

import asyncio
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

# Media sessions we must ignore: our own playback, and the Tidal app itself.
IGNORED_APPS = {"vlc", "vlc media player", "tidal", "spotifytidalsync", "spotifysync"}

_STATUS_MAP = {"Playing": "playing", "Paused": "paused", "Stopped": "stopped"}


class DetectedMedia:
    __slots__ = ("title", "artist", "album", "status", "app_name", "position_ms", "duration_ms")

    def __init__(self, title="", artist="", album="", status="unknown",
                 app_name="", position_ms=-1, duration_ms=0):
        self.title = title or ""
        self.artist = artist or ""
        self.album = album or ""
        self.status = status
        self.app_name = app_name
        self.position_ms = position_ms
        self.duration_ms = duration_ms

    @property
    def is_playing(self):
        return self.status == "playing"


class MediaDetector:
    def __init__(self):
        self._last_error = 0.0
        self._loop = None
        if SMTC_AVAILABLE:
            self._start_loop()

    # --- windows event loop ----------------------------------------------
    def _start_loop(self):
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        time.sleep(0.1)

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
        self._warn("No media detection available (install dbus-python or winsdk)")
        return None

    def send_control(self, command):
        """command: play | pause | play_pause | next | previous"""
        if SMTC_AVAILABLE:
            self._run_async(self._control_windows(command))
        elif DBUS_AVAILABLE:
            self._control_linux(command)

    # --- linux -----------------------------------------------------------
    def _mpris_services(self, bus):
        return [s for s in bus.list_names() if s.startswith("org.mpris.MediaPlayer2.")]

    @staticmethod
    def _mpris_app(service):
        return service.replace("org.mpris.MediaPlayer2.", "").split(".")[0]

    def _pick_linux_service(self, bus, preferred):
        services = self._mpris_services(bus)
        fallback = None
        for service in services:
            app = self._mpris_app(service).lower()
            if app in IGNORED_APPS:
                continue
            if app == preferred.lower():
                return service
            if fallback is None:
                fallback = service
            try:
                proxy = bus.get_object(service, "/org/mpris/MediaPlayer2")
                props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
                if str(props.Get("org.mpris.MediaPlayer2.Player", "PlaybackStatus")) == "Playing":
                    return service
            except Exception:
                continue
        return fallback

    def _read_linux(self, preferred):
        try:
            bus = dbus.SessionBus()
            service = self._pick_linux_service(bus, preferred)
            if service is None:
                return None

            proxy = bus.get_object(service, "/org/mpris/MediaPlayer2")
            props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
            meta = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
            status = str(props.Get("org.mpris.MediaPlayer2.Player", "PlaybackStatus"))

            title = str(meta.get("xesam:title", ""))
            if not title:
                return None
            artists = meta.get("xesam:artist", [])

            position_ms = -1
            try:
                position_ms = int(props.Get("org.mpris.MediaPlayer2.Player", "Position")) // 1000
            except Exception:
                pass

            duration_ms = 0
            try:
                duration_ms = int(meta.get("mpris:length", 0)) // 1000
            except Exception:
                pass

            return DetectedMedia(
                title=title,
                artist=str(artists[0]) if artists else "",
                album=str(meta.get("xesam:album", "")),
                status=_STATUS_MAP.get(status, "unknown"),
                app_name=self._mpris_app(service),
                position_ms=position_ms,
                duration_ms=duration_ms,
            )
        except Exception as e:
            self._warn(f"MPRIS error: {e}")
            return None

    def _control_linux(self, command):
        try:
            bus = dbus.SessionBus()
            target = None
            for service in self._mpris_services(bus):
                app = self._mpris_app(service).lower()
                if app in IGNORED_APPS:
                    continue
                if app == "spotify":
                    target = service
                    break
                if target is None:
                    target = service
            if target is None:
                return
            proxy = bus.get_object(target, "/org/mpris/MediaPlayer2")
            player = dbus.Interface(proxy, "org.mpris.MediaPlayer2.Player")
            action = {
                "play": player.Play, "pause": player.Pause,
                "play_pause": player.PlayPause,
                "next": player.Next, "previous": player.Previous,
            }.get(command)
            if action:
                action()
        except Exception as e:
            logger.debug(f"MPRIS control error: {e}")

    # --- windows ---------------------------------------------------------
    async def _read_windows(self, preferred):
        try:
            manager = await SessionManager.request_async()
            sessions = manager.get_sessions()
            if not sessions:
                return None

            chosen, playing, fallback = None, None, None
            for session in sessions:
                app = self._identify_app(session.source_app_user_model_id)
                if app.lower() in IGNORED_APPS:
                    continue
                if fallback is None:
                    fallback = session
                if app.lower() == preferred.lower():
                    chosen = session
                    break
                info = session.get_playback_info()
                if info and info.playback_status == PlaybackStatus.PLAYING and playing is None:
                    playing = session

            session = chosen or playing or fallback
            if session is None:
                return None

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
                    position_ms = int(timeline.position.total_seconds() * 1000)
                    duration_ms = int(timeline.end_time.total_seconds() * 1000)
            except Exception:
                pass

            return DetectedMedia(
                title=title,
                artist=props.artist or "",
                album=props.album_title or "",
                status=status,
                app_name=self._identify_app(session.source_app_user_model_id),
                position_ms=position_ms,
                duration_ms=duration_ms,
            )
        except Exception as e:
            self._warn(f"SMTC error: {e}")
            return None

    async def _control_windows(self, command):
        try:
            manager = await SessionManager.request_async()
            session = manager.get_current_session()
            if session is None:
                return
            action = {
                "play": session.try_play_async,
                "pause": session.try_pause_async,
                "play_pause": session.try_toggle_play_pause_async,
                "next": session.try_skip_next_async,
                "previous": session.try_skip_previous_async,
            }.get(command)
            if action:
                await action()
        except Exception as e:
            logger.debug(f"SMTC control error: {e}")

    @staticmethod
    def _identify_app(raw_id):
        if not raw_id:
            return "unknown"
        low = raw_id.lower()
        for key, name in (("spotify", "Spotify"), ("tidal", "Tidal"), ("vlc", "VLC")):
            if key in low:
                return name
        return raw_id.split(".")[-1].split("!")[-1] or raw_id

    def _warn(self, msg):
        now = time.time()
        if now - self._last_error > 30:
            logger.warning(msg)
            self._last_error = now
