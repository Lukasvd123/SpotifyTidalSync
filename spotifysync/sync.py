"""The sync engine.

Model: Spotify plays muted and drives the queue; VLC plays the matching Tidal
stream and is the only thing you hear. Two rules follow from that and between
them remove most of the old complexity:

  1. Our player is the clock. Boundaries are decided by when *our* audio ends,
     not by when Spotify flips its metadata (crossfade flips it up to ~12s
     early, and cutting on that clipped the end of every song).
  2. Drift is always corrected by moving Spotify, never us. Spotify is muted, so
     seeking it is free; seeking a network stream forces a re-buffer that stalls
     playback for seconds, which widened the gap that triggered the next seek.
"""

import threading
import time

from .audio import AudioPlayer, SourceMuter
from .config import (HANDOFF_MS, MIDSONG_START_MS, NATURAL_END_MS, POLL_INTERVAL,
                     SEEK_EPSILON_MS, SEEK_SUPPRESS_S, load_settings, logger)
from .sources import EMPTY, PlaybackSource
from .tidal import Prefetcher, TidalLibrary


class State:
    IDLE = "idle"
    PLAYING = "playing"
    NO_MATCH = "no_match"
    DJ_TALKING = "dj_talking"


class SyncEngine:
    def __init__(self, on_update=None, on_match_needed=None, open_browser=None):
        self.on_update = on_update
        self.on_match_needed = on_match_needed
        self.open_browser = open_browser

        self.player = AudioPlayer()
        self.source = PlaybackSource()
        self.library = TidalLibrary()
        self.prefetch = Prefetcher(self.library, self.source)

        settings = load_settings()
        self.muter = SourceMuter(settings["mute_spotify"])
        self.auto_favorite = settings["auto_favorite"]

        self.running = True
        self.state = State.IDLE
        self.status = "Starting..."
        self.current = EMPTY
        self.current_track = None
        self.current_quality = ""
        self.art_url = ""

        self._last_reading = None
        self._last_reading_at = 0.0
        self._pending = None
        self._favorited = False
        self._suppress_seek_until = 0.0
        self._user_skip_until = 0.0

    # --- main loop -------------------------------------------------------
    def run(self):
        if not self.library.login(open_browser=self.open_browser):
            self.status = "Tidal login failed"
            return

        if load_settings()["use_spotify_api"]:
            self.source.connect_api()
        else:
            logger.info("Spotify API disabled; using OS media detection")
            logger.info("  Enable it in Settings for queue prefetch and seek sync")

        if not self.source.api_available and not self.source.detector.available:
            self.status = "No way to detect playback"
            logger.error("Neither the Spotify API nor OS media detection is available")
            return

        self.status = "Running"
        while self.running:
            try:
                self.tick()
            except Exception as e:
                logger.error(f"Sync error: {e}")
            if self.on_update:
                try:
                    self.on_update(self.snapshot())
                except Exception:
                    pass
            time.sleep(POLL_INTERVAL)

    def tick(self):
        src = self.source.read()

        # Commentary first: a spoken segment carries no track metadata at all.
        if src.is_commentary:
            self._handle_commentary()
            return
        if self.state == State.DJ_TALKING:
            self._end_commentary()

        if not src.has_track:
            # Never tear down playback over a transient metadata gap.
            if self.state != State.PLAYING:
                self.status = "Waiting for media..."
            return

        if (not src.is_playing and self.state != State.PLAYING
                and not self.source.paused_by_us):
            self.status = "Waiting for playback..."
            return

        if self.state == State.PLAYING and self.player.finished():
            self._on_finished(src)
            return

        if src.track_id != self.current.track_id:
            self._on_source_changed(src)
            return

        if self.state == State.PLAYING:
            self.muter.mute()
            self._mirror_seek(src)
            self._mirror_pause(src)
            self._maybe_favorite()

        self._remember(src)

    # --- transitions -----------------------------------------------------
    def _on_source_changed(self, src):
        if time.monotonic() < self._user_skip_until:
            self._user_skip_until = 0.0
            self._start_track(src)
            return

        # Whether the *source's own* track really ended, measured against its
        # exact duration. Guessing from how much of our Tidal master was left
        # broke whenever the two releases differed in length.
        previous = self._last_reading
        natural_end = (previous is not None
                       and previous.track_id == self.current.track_id
                       and previous.near_end(NATURAL_END_MS))

        remaining = self.player.remaining_ms()
        if (natural_end and self.state == State.PLAYING
                and self.player.is_playing() and remaining > HANDOFF_MS):
            if self._pending is None or self._pending.track_id != src.track_id:
                self._pending = src
                # Hold the source so it cannot run on into the track after this
                # one while we finish playing.
                self.source.pause()
                logger.info(f"Source moved to '{src.title}' with {remaining}ms "
                            "left; letting ours finish")
            self.status = f"Finishing track ({remaining // 1000}s left)"
            return

        self._start_track(src)

    def _on_finished(self, src):
        target = self._pending
        if target is None and src.track_id != self.current.track_id:
            target = src
        self._pending = None

        if target is None:
            # Our master was shorter than the source's; wait for it to advance.
            logger.info("Our track finished; waiting for the source to advance")
            self.player.stop()
            self.current_track = None
            self.state = State.IDLE
            if self.source.paused_by_us:
                self.source.resume()
            self.status = "Waiting for next track..."
            return

        self._start_track(target)

    def _start_track(self, src):
        logger.info(f"Track: {src.title} by {src.artist}")

        # Mute before any network work. Muting after the search and stream
        # fetch leaks a second or more of source audio at every boundary.
        self.muter.mute(force=True)

        self._pending = None
        self._last_reading = None
        self._favorited = False
        self.current = src
        self.art_url = src.art_url
        self.status = f"Loading {src.title}..."

        resolved = self._resolve(src)
        if resolved is None:
            self._no_match(src)
            return

        source_was_playing = src.is_playing or self.source.paused_by_us
        if source_was_playing:
            # Hold the source while the stream buffers, otherwise it runs ahead
            # by the whole load time and the song starts already behind.
            self.source.pause()

        try:
            self.player.play_url(resolved.url)
            if not self.player.wait_until_playing(timeout=6.0):
                logger.warning(f"VLC did not start '{resolved.track.name}' in time")

            self._align(src)

            self.current_track = resolved.track
            self.current_quality = resolved.quality
            self.state = State.PLAYING
            self.status = f"Playing: {resolved.track.name} [{resolved.quality}]"
            logger.info(self.status)

            if not self.art_url:
                self.art_url = self.library.cover_url(resolved.track)
            self.prefetch.start({src.mapping_key, src.spotify_id})
        except Exception as e:
            logger.error(f"Playback failed: {e}")
            self.player.stop()
            self.current_track = None
            self.state = State.IDLE
            self.status = "Playback error"
        finally:
            if source_was_playing:
                self.source.resume()
            self._remember(self.source.read(force=True))

    def _align(self, src):
        """Choose our start position, then pull the source into line with it."""
        fresh = self.source.read(force=True)

        start = 0
        # A position is only usable if it belongs to the track we just loaded.
        # Re-reading *after* the load and checking the id is the fix for the old
        # jumps: a snapshot taken before the transition was being applied to the
        # new track, throwing playback minutes in or skipping the first verse.
        if (fresh.ok and fresh.track_id == src.track_id
                and fresh.position_ms >= MIDSONG_START_MS):
            duration = self.player.duration_ms()
            if duration <= 0 or fresh.position_ms < duration - 1000:
                self.player.seek(fresh.position_ms)
                start = fresh.position_ms
                logger.info(f"Resuming mid-song at {start}ms")

        position = self.player.position_ms()
        target = position if position >= 0 else start
        if self.source.seek(target):
            self._suppress_seek_until = time.monotonic() + SEEK_SUPPRESS_S
            logger.debug(f"Source realigned to {target}ms")

    def _resolve(self, src):
        cached = self.prefetch.get(src.mapping_key, src.spotify_id)
        if cached and cached.url_fresh:
            logger.info(f"Using prefetched stream: {cached.track.name}")
            return cached

        track = cached.track if cached else self.library.find_match(
            title=src.title,
            artist=src.artist,
            duration_ms=src.duration_ms,
            spotify_id=src.spotify_id,
        )
        if track is None:
            return None
        if not self.library.ensure_session():
            self.status = "Tidal session expired"
            return None
        return self.library.resolve(track)

    def _no_match(self, src):
        self.player.stop()
        self.current_track = None
        if self.source.paused_by_us:
            self.source.resume()

        if src.is_dj_context:
            # Under the DJ an unmatched segment is usually leftover commentary
            # rather than a song the user chose, so wait instead of nagging.
            logger.info(f"AI DJ: no match for '{src.title}', waiting")
            self.state = State.IDLE
            self.status = "AI DJ..."
            return

        self.state = State.NO_MATCH
        self.status = "No match found"
        logger.warning(f"No Tidal match for '{src.title}' by '{src.artist}'")
        if self.on_match_needed:
            self.on_match_needed(src)

    # --- AI DJ -----------------------------------------------------------
    def _handle_commentary(self):
        """Hand audio back to Spotify for the DJ, but only once our song is
        actually done. The DJ crossfades its commentary in before our master
        ends, and handing over on that flip cut the outro off every track."""
        if self.state == State.PLAYING and self.player.is_playing():
            remaining = self.player.remaining_ms()
            if remaining < 0 or remaining > HANDOFF_MS:
                self.muter.mute()
                self.status = "Finishing track (DJ next)"
                return

        if self.state != State.DJ_TALKING:
            logger.info("AI DJ speaking: unmuting Spotify, stopping Tidal")
            self.player.stop()
            self.current_track = None
            self.current = EMPTY
            self._last_reading = None
            self._pending = None
            if self.source.paused_by_us:
                self.source.resume()
            self.muter.unmute()
            self.state = State.DJ_TALKING
        self.status = "AI DJ speaking..."

    def _end_commentary(self):
        """A real track is back. Re-mute first, before any lookup, so the DJ's
        song does not play out loud while we resolve it."""
        logger.info("AI DJ commentary ended; resuming Tidal mirroring")
        self.muter.mute(force=True)
        self.state = State.IDLE
        self.current = EMPTY

    # --- steady state ----------------------------------------------------
    def _mirror_seek(self, src):
        """Follow a deliberate seek on the source.

        The source is compared against its own expected progression, never
        against our player, so normal load lag can never be mistaken for a seek.
        """
        now = time.monotonic()
        previous = self._last_reading
        if (previous is None or previous.track_id != src.track_id
                or not src.is_playing or src.position_ms < 0
                or now < self._suppress_seek_until):
            return

        expected = previous.position_ms + (now - self._last_reading_at) * 1000
        if abs(src.position_ms - expected) < SEEK_EPSILON_MS:
            return

        logger.info(f"Source seeked to {src.position_ms}ms; following")
        self.player.seek(src.position_ms)
        self._suppress_seek_until = now + SEEK_SUPPRESS_S

    def _mirror_pause(self, src):
        if self.source.paused_by_us:
            return  # our own pause must not bounce back and freeze us
        playing = self.player.is_playing()
        if not src.is_playing and playing:
            self.player.pause()
        elif src.is_playing and not playing and self.player.has_track():
            self.player.resume()

    def _maybe_favorite(self):
        if not self.auto_favorite or self._favorited or self.current_track is None:
            return
        duration = self.player.duration_ms()
        position = self.player.position_ms()
        if duration > 0 and position >= 0 and position / duration >= 0.90:
            self._favorited = True
            if self.library.favorite(self.current_track.id):
                logger.info("Auto-favorited on Tidal")

    def _remember(self, src):
        if src.ok and src.track_id:
            self._last_reading = src
            self._last_reading_at = time.monotonic()

    # --- commands --------------------------------------------------------
    def toggle_play(self):
        self.source.toggle_play()

    def next_track(self):
        # A deadline rather than a flag: a skip that never lands expires by
        # itself instead of silently cutting the next natural track end short.
        self._user_skip_until = time.monotonic() + 5.0
        self.source.next_track()

    def previous_track(self):
        self._user_skip_until = time.monotonic() + 5.0
        self.source.previous_track()

    def seek_to(self, position_ms):
        position_ms = max(0, int(position_ms))
        self.player.seek(position_ms)
        if self.source.seek(position_ms):
            self._suppress_seek_until = time.monotonic() + SEEK_SUPPRESS_S

    def skip(self, delta_ms):
        position = self.player.position_ms()
        if position < 0:
            return
        duration = self.player.duration_ms()
        target = max(0, position + delta_ms)
        if duration > 0:
            target = min(target, duration - 500)
        self.seek_to(target)

    def manual_map(self, tidal_track):
        """Apply a user-chosen Tidal track and play it."""
        src = self.current
        if not src.has_track:
            return False

        self.library.save_mapping(src.mapping_key, tidal_track.id)
        resolved = self.library.resolve(tidal_track)
        if resolved is None:
            self.status = "Could not stream that track"
            return False

        self.muter.mute(force=True)
        source_was_playing = src.is_playing or self.source.paused_by_us
        if source_was_playing:
            self.source.pause()
        try:
            self.player.play_url(resolved.url)
            self.player.wait_until_playing(timeout=6.0)
            self._align(src)
            self.current_track = resolved.track
            self.current_quality = resolved.quality
            self.art_url = self.library.cover_url(resolved.track) or self.art_url
            self.state = State.PLAYING
            self.status = f"Playing: {resolved.track.name} [{resolved.quality}]"
            return True
        except Exception as e:
            logger.error(f"Manual playback failed: {e}")
            return False
        finally:
            if source_was_playing:
                self.source.resume()
            self._remember(self.source.read(force=True))

    def search_tidal(self, query, limit=20):
        return self.library.search(query, limit=limit)

    def set_muting(self, enabled):
        self.muter.enabled = enabled
        if enabled:
            self.muter.mute(force=True)
        else:
            self.muter.unmute()

    # --- reporting -------------------------------------------------------
    def snapshot(self):
        if self.state == State.NO_MATCH:
            track = "(match needed)"
        elif self.current_track is not None:
            track = self.current_track.name
        else:
            track = self.current.title or "Waiting for media..."

        source_track = ""
        if self.current.has_track:
            source_track = f"{self.current.title} - {self.current.artist}"

        return {
            "state": self.state,
            "status": self.status,
            "track": track,
            "position_ms": max(0, self.player.position_ms()),
            "duration_ms": max(0, self.player.duration_ms()),
            "art_url": self.art_url,
            "source_track": source_track,
            "source_app": self.current.app_name,
            "api": self.source.api_available,
            "quality": self.current_quality,
        }

    def shutdown(self):
        self.running = False
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            if self.source.paused_by_us:
                self.source.resume()
        except Exception:
            pass
        # Never leave the user with a silently muted Spotify.
        try:
            self.muter.restore()
        except Exception as e:
            logger.debug(f"Unmute on shutdown failed: {e}")

    def start_background(self):
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread
