"""The source app as a single object: reading its state and controlling it."""

import socket
import time

from ..config import API_SNAPSHOT_INTERVAL, DJ_CONTEXT_ID, load_settings, logger, save_setting
from ..matching import make_mapping_key
from .model import EMPTY, Content, SourceState


class PlaybackSource:
    """Merges the Spotify Web API and OS media detection into one view.

    The API is preferred when connected (stable track ids, exact durations,
    seek control); OS detection covers everything else and acts as the control
    fallback when the API rejects a command.
    """

    def __init__(self, api=None, detector=None, clock=time.monotonic):
        if api is None:
            from .spotify_api import SpotifyController
            api = SpotifyController()
        if detector is None:
            from .detector import MediaDetector
            detector = MediaDetector()
        self.api = api
        self.detector = detector
        self.clock = clock
        self.paused_by_us = False
        self._cached = None
        self._cached_at = 0.0
        self._local_device = load_settings().get("spotify_device_id")
        self._hostname = socket.gethostname().split(".")[0].lower()

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
        now = self.clock()
        if not force and self._cached is not None and (now - self._cached_at) < API_SNAPSHOT_INTERVAL:
            return self._cached
        state = self._build(now)
        self._cached, self._cached_at = state, now
        return state

    def invalidate(self):
        self._cached = None

    def _build(self, now):
        state = self._from_api(now)
        if state is not None:
            return state
        return self._from_detector(now)

    def _is_local(self, device, item_name):
        """Whether the API's active device is this computer.

        The API reports playback on any device. Without this check, playing on
        the phone started Tidal on the PC and our pause/seek commands landed on
        the phone. The local device id is learned once, when OS detection
        confirms this machine's Spotify is playing the same track.
        """
        if not device:
            return True
        device_id = device.get("id")
        name = (device.get("name") or "").lower()
        if device_id and device_id == self._local_device:
            return True
        if device.get("type", "").lower() != "computer":
            return False

        by_name = bool(self._hostname) and name.split(".")[0] == self._hostname
        confirmed = False
        if not by_name and self.detector.available:
            media = self.detector.read("spotify")
            confirmed = (media is not None and media.app_name.lower() == "spotify"
                         and media.title == item_name)
        elif not self.detector.available:
            # No way to tell. Assume local rather than never playing.
            return self._local_device is None

        if by_name or (confirmed and self._local_device is None):
            if device_id and device_id != self._local_device:
                self._local_device = device_id
                save_setting("spotify_device_id", device_id)
                logger.info(f"Spotify: this computer is device '{device.get('name')}'")
            return True
        return False

    def _from_api(self, now):
        playback = self.api.playback()
        if not playback:
            return None

        context = (playback.get("context") or {}).get("uri") or ""
        is_dj = DJ_CONTEXT_ID in context
        item = playback.get("item")
        playing_type = playback.get("currently_playing_type") or ""
        is_playing = bool(playback.get("is_playing"))
        device = playback.get("device") or {}

        common = dict(ok=True, is_playing=is_playing, app_name="Spotify", from_api=True,
                      is_dj_context=is_dj, read_at=now, device_name=device.get("name") or "",
                      repeat_track=playback.get("repeat_state") == "track")

        if not self._is_local(device, (item or {}).get("name")):
            return SourceState(is_remote=True, **common)

        # A DJ commentary segment does not resolve as a track.
        if is_dj and (item is None or playing_type != "track"):
            return SourceState(content=Content.COMMENTARY, **common)
        if playing_type == "ad":
            return SourceState(content=Content.AD, **common)
        if playing_type == "episode" or (item or {}).get("type") == "episode":
            return SourceState(content=Content.EPISODE, title=(item or {}).get("name") or "",
                               **common)
        if not item:
            return None

        artists = tuple(a["name"] for a in item.get("artists") or [] if a.get("name"))
        album = item.get("album") or {}
        images = album.get("images") or []
        # Local files have no id; fall back to their uri so they still change.
        track_id = item.get("id") or item.get("uri") or make_mapping_key(
            item.get("name"), artists[0] if artists else "")

        return SourceState(
            track_id=track_id,
            spotify_id=item.get("id") or "",
            title=item.get("name") or "",
            artist=artists[0] if artists else "",
            artists=artists,
            album=album.get("name") or "",
            position_ms=playback.get("progress_ms") or 0,
            duration_ms=item.get("duration_ms") or 0,
            art_url=images[0]["url"] if images else "",
            isrc=(item.get("external_ids") or {}).get("isrc") or "",
            **common,
        )

    def _from_detector(self, now):
        media = self.detector.read("spotify")
        if media is None or not media.title:
            return EMPTY
        content = {"ad": Content.AD, "episode": Content.EPISODE}.get(media.kind, Content.TRACK)
        # Without a Spotify id, identity falls back to the normalised
        # title|artist pair.
        return SourceState(
            ok=True,
            track_id=media.spotify_id or make_mapping_key(media.title, media.artist),
            spotify_id=media.spotify_id,
            title=media.title,
            artist=media.artist,
            artists=tuple(media.artists),
            album=media.album,
            position_ms=media.position_ms,
            duration_ms=media.duration_ms,
            is_playing=media.is_playing,
            content=content,
            art_url=media.art_url,
            app_name=media.app_name,
            read_at=now,
        )

    # --- control ---------------------------------------------------------
    def pause(self):
        """Pause the source and remember we owe it a resume."""
        ok = self.api.pause() or self.detector.send_control("pause")
        self.paused_by_us = bool(ok)
        self.invalidate()
        return ok

    def resume(self):
        ok = self.api.resume() or self.detector.send_control("play")
        self.paused_by_us = False
        self.invalidate()
        return ok

    def seek(self, position_ms):
        ok = self.api.seek(position_ms) or self.detector.seek(position_ms)
        if ok:
            self.invalidate()
        return ok

    def next_track(self):
        ok = self.api.next_track() or self.detector.send_control("next")
        self.invalidate()
        return ok

    def previous_track(self):
        ok = self.api.previous_track() or self.detector.send_control("previous")
        self.invalidate()
        return ok

    def toggle_play(self):
        state = self.read(force=True)
        if state.is_playing:
            self.pause()
            # A user pause is theirs to undo, not ours.
            self.paused_by_us = False
        else:
            self.resume()

    def upcoming(self, limit=5):
        return self.api.queue(limit)
