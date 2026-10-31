"""The source app as a single object: reading its state and controlling it."""

import time

from ..config import API_SNAPSHOT_INTERVAL, DJ_CONTEXT_ID
from ..matching import make_mapping_key
from .detector import MediaDetector
from .model import EMPTY, SourceState
from .spotify_api import SpotifyController


class PlaybackSource:
    """Merges the Spotify Web API and OS media detection into one view.

    The API is preferred when connected (stable track ids, exact durations,
    seek control); OS detection covers everything else and acts as the control
    fallback when the API rejects a command.
    """

    def __init__(self):
        self.api = SpotifyController()
        self.detector = MediaDetector()
        self.paused_by_us = False
        self._cached = None
        self._cached_at = 0.0

    # --- lifecycle -------------------------------------------------------
    def connect_api(self):
        return self.api.login()

    @property
    def api_available(self):
        return self.api.available

    # --- reading ---------------------------------------------------------
    def read(self, force=False):
        """Current source state. Throttled to avoid hammering the Web API.

        Pass force=True after anything that changes what the source is doing
        (a track load, a seek) so the reading is not a pre-change snapshot.
        """
        now = time.monotonic()
        if not force and self._cached is not None and (now - self._cached_at) < API_SNAPSHOT_INTERVAL:
            return self._cached
        state = self._build()
        self._cached, self._cached_at = state, now
        return state

    def invalidate(self):
        self._cached = None

    def _build(self):
        state = self._from_api()
        if state is not None:
            return state
        return self._from_detector()

    def _from_api(self):
        playback = self.api.playback()
        if not playback:
            return None

        context = (playback.get("context") or {}).get("uri") or ""
        is_dj = DJ_CONTEXT_ID in context
        item = playback.get("item")
        playing_type = playback.get("currently_playing_type") or ""
        is_playing = bool(playback.get("is_playing"))

        # A DJ commentary segment does not resolve as a track.
        if is_dj and (item is None or playing_type != "track"):
            return SourceState(ok=True, is_dj_context=True, is_commentary=True,
                               is_playing=is_playing, app_name="Spotify", from_api=True)

        if not item:
            return None

        artists = item.get("artists") or []
        album = item.get("album") or {}
        images = album.get("images") or []

        return SourceState(
            ok=True,
            track_id=item.get("id") or "",
            spotify_id=item.get("id") or "",
            title=item.get("name") or "",
            artist=artists[0]["name"] if artists else "",
            album=album.get("name") or "",
            position_ms=playback.get("progress_ms") or 0,
            duration_ms=item.get("duration_ms") or 0,
            is_playing=is_playing,
            is_dj_context=is_dj,
            art_url=images[0]["url"] if images else "",
            app_name="Spotify",
            from_api=True,
        )

    def _from_detector(self):
        media = self.detector.read("spotify")
        if media is None or not media.title:
            return EMPTY
        # Without the API there is no stable id, so identity falls back to the
        # normalised title|artist pair.
        return SourceState(
            ok=True,
            track_id=make_mapping_key(media.title, media.artist),
            title=media.title,
            artist=media.artist,
            album=media.album,
            position_ms=media.position_ms,
            duration_ms=media.duration_ms,
            is_playing=media.is_playing,
            app_name=media.app_name,
        )

    # --- control ---------------------------------------------------------
    def pause(self):
        """Pause the source and remember we owe it a resume."""
        if not self.api.pause():
            self.detector.send_control("pause")
        self.paused_by_us = True
        self.invalidate()

    def resume(self):
        if not self.api.resume():
            self.detector.send_control("play")
        self.paused_by_us = False
        self.invalidate()

    def seek(self, position_ms):
        """Seek the source. Only meaningful with the Web API connected."""
        ok = self.api.seek(position_ms)
        if ok:
            self.invalidate()
        return ok

    def next_track(self):
        if not self.api.next_track():
            self.detector.send_control("next")
        self.invalidate()

    def previous_track(self):
        if not self.api.previous_track():
            self.detector.send_control("previous")
        self.invalidate()

    def toggle_play(self):
        state = self.read(force=True)
        if state.is_playing:
            self.pause()
        else:
            self.resume()

    def upcoming(self, limit=5):
        return self.api.queue(limit)
