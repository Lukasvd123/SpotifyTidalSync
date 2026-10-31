"""The sync engine.

Model: Spotify plays muted and drives the queue; VLC plays the matching Tidal
stream and is the only thing you hear. Rules that follow from that:

  1. Our player is the clock. Boundaries are decided by when *our* audio ends,
     not by when Spotify flips its metadata (crossfade flips it up to ~12s
     early, and cutting on that clipped the end of every song). When our audio
     ends first, the muted source is skipped forward instead of waited for.
  2. We never seek ourselves to chase the source. Spotify is muted, so moving it
     is free; seeking a network stream forces a re-buffer that stalls playback.
     Only a deliberate user seek, confirmed by two readings, moves our player.
  3. Fail open. When Tidal cannot play a track, the source is unmuted rather
     than leaving the user in silence.
"""

import queue
import threading
import time

from .config import (EARLY_END_SKIP_MS, EARLY_EOF_MS, HANDOFF_MS, HOLD_MAX_MS,
                     MAX_RECOVERIES, MIDSONG_START_MS, NATURAL_END_MS, POLL_INTERVAL,
                     PREBUFFER_MS, PREFETCH_URL_MS, SEEK_EPSILON_MS, SEEK_SUPPRESS_S,
                     load_settings, logger)
from .matching import CONFIDENT_SCORE, TrackQuery
from .sources import EMPTY, Content, same_track

# Give up waiting for the source to report the track we skipped it to.
ADVANCE_TIMEOUT_S = 8.0
# A user or engine skip expires if the source never reports a change.
SKIP_WINDOW_S = 5.0


class State:
    IDLE = "idle"
    PLAYING = "playing"
    # Our track ended first; the source is being moved to the next one.
    ADVANCING = "advancing"
    # Tidal cannot play this track; the source is unmuted and audible.
    FALLBACK = "fallback"
    # DJ commentary, podcast or ad: audio belongs to the source.
    PASSTHROUGH = "passthrough"
    # Spotify is playing on another device; we stay out of it.
    REMOTE = "remote"


_PASSTHROUGH_STATUS = {
    Content.COMMENTARY: "AI DJ speaking...",
    Content.EPISODE: "Podcast: playing from Spotify",
    Content.AD: "Ad: playing from Spotify",
}


def query_for(src):
    return TrackQuery(title=src.title, artist=src.artist, artists=src.artists,
                      album=src.album, duration_ms=src.duration_ms, isrc=src.isrc)


class SyncEngine:
    def __init__(self, on_update=None, on_match_needed=None, open_browser=None,
                 player=None, source=None, library=None, muter=None, prefetch=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.on_update = on_update
        self.on_match_needed = on_match_needed
        self.open_browser = open_browser
        self.clock = clock
        self._sleep = sleep

        settings = load_settings()
        if player is None:
            from .audio.player import AudioPlayer
            player = AudioPlayer()
        if source is None:
            from .sources import PlaybackSource
            source = PlaybackSource(clock=clock)
        if library is None:
            from .tidal import TidalLibrary
            library = TidalLibrary()
        if muter is None:
            from .audio.mute import SourceMuter
            muter = SourceMuter(settings["mute_spotify"], clock=clock)
        if prefetch is None:
            from .tidal import Prefetcher
            prefetch = Prefetcher(library, source)

        self.player = player
        self.source = source
        self.library = library
        self.muter = muter
        self.prefetch = prefetch
        self.auto_favorite = settings["auto_favorite"]

        self.running = True
        self.state = State.IDLE
        self.status = "Starting..."
        self.current = EMPTY
        self.current_track = None
        self.current_quality = ""
        self.match_label = ""
        self.match_score = 0.0
        self.art_url = ""

        self._commands = queue.Queue()
        self._last_reading = None
        self._seek_candidate = None
        self._pending = None
        self._hold_deadline = 0.0
        self._favorited = False
        self._suppress_seek_until = 0.0
        self._expect_change_until = 0.0
        self._advanced_at = 0.0
        self._skip_at = None
        self._positions_live = False
        self._recoveries = 0
        self._last_pos = -1
        self._duration = 0
        self._prompted = set()

    # --- main loop -------------------------------------------------------
    def run(self):
        self.muter.recover_from_crash()
        if not self._login():
            return

        if load_settings()["use_spotify_api"]:
            self.source.connect_api()
        else:
            logger.info("Spotify API disabled; using OS media detection")
            logger.info("  Enable it in Settings for queue prefetch and seek sync")

        if not self.source.api_available and not self.source.detector.available:
            self.status = "No way to detect playback"
            logger.error("Neither the Spotify API nor OS media detection is available")
            self._publish()
            return

        self.status = "Running"
        while self.running:
            try:
                self.tick()
            except Exception as e:
                logger.exception(f"Sync error: {e}")
            self._publish()
            self._sleep(POLL_INTERVAL)

    def _login(self):
        """Log in to Tidal, retrying while offline (e.g. autostart before the
        network is up) instead of giving up for the whole session."""
        from .tidal import LoginResult
        delay = 5
        while self.running:
            self.status = "Logging in to Tidal..."
            self._publish()
            result = self.library.login(open_browser=self.open_browser)
            if result == LoginResult.OK:
                return True
            if result == LoginResult.FAILED:
                self.status = "Tidal login failed (restart to try again)"
                self._publish()
                return False
            self.status = f"Offline, retrying Tidal login in {delay}s"
            self._publish()
            self._sleep(delay)
            delay = min(60, delay * 2)
        return False

    def _publish(self):
        if self.on_update:
            try:
                self.on_update(self.snapshot())
            except Exception:
                pass

    def tick(self):
        self._drain_commands()
        # Read fresh while waiting for the source to land on the next track,
        # but only briefly: the throttle exists to spare the Web API.
        advancing = (self.state == State.ADVANCING
                     and self.clock() - self._advanced_at < ADVANCE_TIMEOUT_S)
        src = self.source.read(force=advancing)

        if src.is_remote:
            self._on_remote(src)
            return
        if self.state == State.REMOTE:
            self.state = State.IDLE

        # Commentary first: a spoken segment carries no track metadata at all.
        if src.is_passthrough:
            self._handle_passthrough(src)
            return
        if self.state == State.PASSTHROUGH:
            self._end_passthrough()

        if self.state == State.PLAYING:
            self._track_player()
            status = self.player.status()
            if status in ("ended", "error"):
                if self._cut_short(src, status):
                    self._recover(src)
                else:
                    self._on_finished(src)
                return
            if self._pending is not None and self.clock() > self._hold_deadline:
                logger.warning("Our track did not finish in time; moving on")
                self._start_track(self._pending)
                return

        if not src.has_track:
            # Never tear down playback over a transient metadata gap.
            if self.state != State.PLAYING:
                self.status = "Waiting for media..."
            return

        if not same_track(src, self.current):
            if (not src.is_playing and self.state != State.PLAYING
                    and not self.source.paused_by_us):
                self.status = "Waiting for playback..."
                return
            self._on_source_changed(src)
            return

        if self.state == State.PLAYING:
            self.muter.mute()
            self._mirror_seek(src)
            self._mirror_pause(src)
            self._maybe_favorite()
            self._maybe_prefetch()
        elif self.state == State.ADVANCING:
            self._check_restart(src)
        elif self.state == State.IDLE and src.is_playing:
            self._start_track(src)
            return

        self._remember(src)

    # --- transitions -----------------------------------------------------
    def _on_source_changed(self, src):
        now = self.clock()
        if now < self._expect_change_until:
            self._expect_change_until = 0.0
            self._start_track(src)
            return

        # Whether the *source's own* track really ended, measured against its
        # exact duration. Guessing from how much of our Tidal master was left
        # broke whenever the two releases differed in length.
        previous = self._last_reading
        natural_end = (previous is not None
                       and same_track(previous, self.current)
                       and previous.near_end(NATURAL_END_MS, now))

        remaining = self.player.remaining_ms()
        if (natural_end and self.state == State.PLAYING and self.player.is_playing()
                and HANDOFF_MS < remaining <= HOLD_MAX_MS):
            if self._pending is None or not same_track(self._pending, src):
                self._pending = src
                self._hold_deadline = now + remaining / 1000.0 + 5.0
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
        if target is None and not same_track(src, self.current):
            target = src
        self._pending = None
        if target is not None:
            self._start_track(target)
            return

        now = self.clock()
        self.player.stop()
        if self.source.paused_by_us:
            self.source.resume()
        # A cached reading can be a second old; extrapolating it past the end
        # once made us skip the track the source had just moved to.
        src = self.source.read(force=True)
        if not src.has_track:
            # The queue ran out, or the source is between tracks.
            self.current_track = None
            self.state = State.IDLE
            self.status = "Waiting for media..."
            return
        if not same_track(src, self.current):
            self._start_track(src)
            return

        self.state = State.ADVANCING
        self._advanced_at = now
        self.status = "Next track..."
        position = src.position_at(now) if self._positions_live else -1
        if src.repeat_track or (0 <= position < 5000 and src.duration_ms > 15000):
            # Repeat-one, or the same track queued twice: it already started over.
            logger.info("Source restarted the same track; playing it again")
            self._start_track(src, from_start=True)
            return

        if position >= 0 and src.duration_ms > 0:
            left = src.duration_ms - position
            if left <= EARLY_END_SKIP_MS:
                # The source is about to flip by itself.
                self._skip_at = now + max(0, left) / 1000.0 + 2.0
                return
            # Our master was shorter than the source's. Waiting for the muted
            # source to get there was seconds of silence at every such boundary.
            logger.info(f"Our track ended {left}ms before the source's; skipping it forward")
            self._skip_source(now)
        else:
            # No trustworthy position: give a natural flip a moment first.
            self._skip_at = now + 2.0

    def _skip_source(self, now):
        self._skip_at = None
        self._expect_change_until = now + SKIP_WINDOW_S
        self.source.next_track()

    def _check_restart(self, src):
        """While advancing, the same track starting over means repeat-one or a
        duplicate queue entry: play it again rather than sit silent."""
        now = self.clock()
        if (self._positions_live and src.is_playing and 0 <= src.position_ms < 5000
                and now - self._advanced_at > 1.0):
            logger.info("Source restarted the same track; playing it again")
            self._start_track(src, from_start=True)
            return
        if self._skip_at is not None and now >= self._skip_at:
            logger.info("Source did not move on; skipping it forward")
            self._skip_source(now)
            return
        if now - self._advanced_at > ADVANCE_TIMEOUT_S:
            self.status = "Waiting for next track..."

    def _start_track(self, src, from_start=False):
        logger.info(f"Track: {src.title} by {src.artist}")
        self._reset_track_state()
        self.current = src
        self.art_url = src.art_url
        query = query_for(src)

        if self.library.is_not_on_tidal(query, src.spotify_id):
            self._fallback(src, "Not on Tidal", popup=False)
            return

        # Mute before any network work. Muting after the search and stream
        # fetch leaks a second or more of source audio at every boundary.
        self.muter.mute(force=True)
        self.status = f"Loading {src.title}..."

        resolved, match = self._resolve(src, query)

        # The user may have skipped on while we searched. Loading this track
        # anyway made a burst of skips play every track in between.
        fresh = self.source.read(force=True)
        if fresh.has_track and not fresh.is_remote and not same_track(fresh, src):
            logger.info(f"Skipped past '{src.title}' while loading")
            self.state = State.IDLE
            self._expect_change_until = self.clock() + SKIP_WINDOW_S
            return

        if resolved is None:
            reason = "No match found" if match is None else "Could not stream"
            self._fallback(src, reason, popup=match is None)
            return
        self._play(src, resolved, match, from_start, fresh.is_playing)

    def _play(self, src, resolved, match, from_start, source_playing):
        source_was_playing = source_playing or self.source.paused_by_us
        if source_was_playing:
            # Hold the source while the stream buffers, otherwise it runs ahead
            # by the whole load time and the song starts already behind.
            self.source.pause()
        try:
            start = 0 if from_start else self._start_position(src)
            self.player.play_url(resolved.url, start_ms=start)
            if not self.player.wait_until_playing(timeout=6.0):
                raise RuntimeError(f"'{resolved.track.name}' did not start")

            # Pull the paused source to where we start, so both run in step.
            if self.source.seek(start):
                logger.debug(f"Source aligned to {start}ms")
            self._suppress_seek_until = self.clock() + SEEK_SUPPRESS_S

            self.current_track = resolved.track
            self.current_quality = resolved.quality
            self.match_label = match.label if match else ""
            self.match_score = match.score if match else 0.0
            self.state = State.PLAYING
            self.status = f"Playing: {resolved.track.name} [{resolved.quality}]"
            logger.info(self.status)

            if not self.art_url:
                self.art_url = self.library.cover_url(resolved.track)
            self.prefetch.start({src.mapping_key, src.spotify_id})
        except Exception as e:
            logger.error(f"Playback failed: {e}")
            self._fallback(src, "Playback error", popup=False)
        finally:
            if self.source.paused_by_us:
                self.source.resume()
            self._remember(self.source.read(force=True))

    def _start_position(self, src):
        """Where to start: 0, or the source's position when genuinely mid-song.

        Read while the source is paused, and only if the reading belongs to
        this track: a snapshot taken across a transition once threw playback
        minutes into a song that had just started.
        """
        reading = self.source.read(force=True)
        if not same_track(reading, src) or reading.position_ms < MIDSONG_START_MS:
            return 0
        duration = src.duration_ms or reading.duration_ms
        if duration > 0 and reading.position_ms >= duration - EARLY_EOF_MS:
            return 0
        logger.info(f"Starting mid-song at {reading.position_ms}ms")
        return reading.position_ms

    def _resolve(self, src, query):
        from .tidal import Match
        cached = self.prefetch.get(src.mapping_key, src.spotify_id)
        if cached and cached.url_fresh:
            logger.info(f"Using prefetched stream: {cached.track.name}")
            return cached, Match(cached.track, "prefetch", CONFIDENT_SCORE)

        if cached:
            match = Match(cached.track, "prefetch", CONFIDENT_SCORE)
        else:
            match = self.library.find_match(query, spotify_id=src.spotify_id)
        if match is None:
            return None, None
        if not self.library.ensure_session():
            logger.warning("Tidal session expired")
            return None, match
        return self.library.resolve(match.track), match

    def _fallback(self, src, reason, popup):
        """Fail open: let the source be heard instead of leaving silence."""
        self.player.stop()
        self.player.clear_preload()
        self.current_track = None
        self.current_quality = ""
        self.match_label = ""
        if self.source.paused_by_us:
            self.source.resume()
        self.muter.unmute()
        self.state = State.FALLBACK
        self.status = f"{reason}: playing Spotify's audio"
        logger.warning(f"{reason} for '{src.title}' by '{src.artist}'; source unmuted")

        # Under the DJ an unmatched segment is usually leftover commentary
        # rather than a song the user chose, so do not nag.
        if (popup and not src.is_dj_context and self.on_match_needed
                and src.track_id not in self._prompted):
            self._prompted.add(src.track_id)
            self.on_match_needed(src)

    def _recover(self, src):
        """Our stream died mid-track (expired URL after a long pause, suspend,
        a dropped connection). Reconnect at the same spot instead of treating
        it as the end of the song."""
        self._recoveries += 1
        position = max(0, self._last_pos)
        if self._positions_live and same_track(src, self.current):
            position = max(0, src.position_at(self.clock()))
        if self._recoveries > MAX_RECOVERIES or self.current_track is None:
            self._fallback(src, "Tidal stream failed", popup=False)
            return
        logger.warning(f"Stream dropped at {position}ms; reconnecting "
                       f"(attempt {self._recoveries}/{MAX_RECOVERIES})")
        self.status = "Reconnecting..."
        resolved = None
        if self.library.ensure_session():
            resolved = self.library.resolve(self.current_track)
        if resolved is None:
            self._fallback(src, "Tidal stream failed", popup=False)
            return
        self.player.play_url(resolved.url, start_ms=position)
        self.player.wait_until_playing(timeout=6.0)
        self._suppress_seek_until = self.clock() + SEEK_SUPPRESS_S
        self._seek_candidate = None
        if self.player.status() == "playing":
            self.status = f"Playing: {self.current_track.name} [{resolved.quality}]"

    def _reset_track_state(self):
        self._pending = None
        self._last_reading = None
        self._seek_candidate = None
        self._favorited = False
        self._recoveries = 0
        self._last_pos = -1
        self._duration = 0
        self._skip_at = None
        self._positions_live = False
        self.match_label = ""

    # --- non-music and remote --------------------------------------------
    def _handle_passthrough(self, src):
        """Hand audio back to Spotify, but only once our song is actually done.

        The DJ crossfades its commentary in before our master ends, and handing
        over on that flip cut the outro off every track. Bounded: a podcast
        started mid-song switches straight away.
        """
        if self.state == State.PLAYING and self.player.is_playing():
            remaining = self.player.remaining_ms()
            if HANDOFF_MS < remaining <= HOLD_MAX_MS:
                self.muter.mute()
                self.status = "Finishing track..."
                return

        if self.state != State.PASSTHROUGH:
            logger.info(f"Source is playing {src.content}; handing audio back to it")
            self.player.stop()
            self.player.clear_preload()
            self.current_track = None
            self.current_quality = ""
            self.current = EMPTY
            self._reset_track_state()
            if self.source.paused_by_us:
                self.source.resume()
            self.muter.unmute()
            self.state = State.PASSTHROUGH
        self.status = _PASSTHROUGH_STATUS.get(src.content, "Playing from Spotify")

    def _end_passthrough(self):
        """A real track is back. Re-mute first, before any lookup, so the
        source's song does not play out loud while we resolve it."""
        logger.info("Music is back; resuming Tidal mirroring")
        self.muter.mute(force=True)
        self.state = State.IDLE
        self.current = EMPTY

    def _on_remote(self, src):
        if self.state != State.REMOTE:
            logger.info(f"Spotify is playing on '{src.device_name}'; standing by")
            self.player.stop()
            self.player.clear_preload()
            self.current_track = None
            self.current_quality = ""
            self.current = EMPTY
            self._reset_track_state()
            # Never send pause/resume to another device.
            self.source.paused_by_us = False
            self.muter.restore()
            self.state = State.REMOTE
        self.status = f"Playing on {src.device_name or 'another device'}"

    # --- steady state ----------------------------------------------------
    def _track_player(self):
        position, duration = self.player.position_ms(), self.player.duration_ms()
        if position >= 0:
            self._last_pos = position
        if duration > 0:
            self._duration = duration

    def _cut_short(self, src, status):
        """Whether our stream died before the song was over.

        libvlc's position lags the audio on Tidal's MP4 streams by up to ~13s
        at the end (its duration is right), so its own position cannot tell a
        real end from a drop: that reconnected at the lagging position and
        replayed the ending. The source's position is the better clock.
        """
        if self._pending is not None or not same_track(src, self.current):
            return False  # the source has moved on: nothing left to resume
        if self._positions_live:
            now = self.clock()
            if self._duration > 0 and src.position_at(now) >= self._duration - EARLY_EOF_MS:
                return False
            if src.duration_ms > 0 and src.near_end(EARLY_EOF_MS, now):
                return False
            return True
        if status == "error":
            return True
        return (self._duration > 0 and self._last_pos >= 0
                and self._duration - self._last_pos > EARLY_EOF_MS)

    def _mirror_seek(self, src):
        """Follow a deliberate seek on the source.

        The source is compared against its own expected progression, timed by
        when each reading was actually taken. A jump only counts once the next
        reading confirms it, and only if our player is not already there: a
        stale reading catching up (Windows SMTC) or a lagging API snapshot right
        after our own seek must never move our stream.
        """
        previous = self._last_reading
        if (previous is None or not same_track(previous, src)
                or src.read_at <= previous.read_at):
            return
        if (not (previous.is_playing and src.is_playing) or src.position_ms < 0
                or previous.position_ms < 0 or self.clock() < self._suppress_seek_until):
            self._seek_candidate = None
            return

        expected = previous.position_at(src.read_at)
        if abs(src.position_ms - expected) >= SEEK_EPSILON_MS:
            self._seek_candidate = src
            return

        candidate, self._seek_candidate = self._seek_candidate, None
        if candidate is None or candidate is not previous:
            return
        ours = self.player.position_ms()
        if ours >= 0 and abs(src.position_ms - ours) < SEEK_EPSILON_MS:
            return
        logger.info(f"Source seeked to {src.position_ms}ms; following")
        self.player.seek(src.position_ms)
        self._suppress_seek_until = self.clock() + SEEK_SUPPRESS_S

    def _mirror_pause(self, src):
        if self.source.paused_by_us:
            return  # our own pause must not bounce back and freeze us
        playing = self.player.is_playing()
        if not src.is_playing and playing:
            self.player.pause()
        elif src.is_playing and not playing and self.player.status() == "paused":
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

    def _maybe_prefetch(self):
        remaining = self.player.remaining_ms()
        if remaining < 0:
            return
        if remaining <= PREFETCH_URL_MS:
            self.prefetch.warm_next()
        if remaining <= PREBUFFER_MS:
            entry = self.prefetch.next_up
            if entry is not None and entry.url_fresh:
                self.player.preload(entry.url)

    def _remember(self, src):
        previous = self._last_reading
        if not src.ok or not src.track_id:
            return
        if previous is not None and src.read_at == previous.read_at and same_track(src, previous):
            return
        # Positions are only trusted once seen moving: some players report a
        # constant 0, and raw SMTC only updates on events.
        if (previous is not None and same_track(src, previous) and previous.is_playing
                and src.is_playing and src.position_ms > previous.position_ms >= 0):
            self._positions_live = True
        self._last_reading = src

    # --- commands --------------------------------------------------------
    # Called from the UI thread. Queued and run by the sync loop, so a button
    # press can never race a track load.
    def _submit(self, fn, *args):
        self._commands.put((fn, args))

    def _drain_commands(self):
        while True:
            try:
                fn, args = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception as e:
                logger.error(f"Command {getattr(fn, '__name__', fn)} failed: {e}")

    def toggle_play(self):
        self._submit(self.source.toggle_play)

    def next_track(self):
        self._submit(self._skip_track, self.source.next_track)

    def previous_track(self):
        self._submit(self._skip_track, self.source.previous_track)

    def _skip_track(self, command):
        # A deadline rather than a flag: a skip that never lands expires by
        # itself instead of silently cutting the next natural track end short.
        self._expect_change_until = self.clock() + SKIP_WINDOW_S
        command()

    def seek_to(self, position_ms):
        self._submit(self._seek_to, position_ms)

    def _seek_to(self, position_ms):
        position_ms = max(0, int(position_ms))
        self.player.seek(position_ms)
        self._last_reading = None
        self._seek_candidate = None
        self._suppress_seek_until = self.clock() + SEEK_SUPPRESS_S
        self.source.seek(position_ms)

    def skip(self, delta_ms):
        self._submit(self._skip_by, delta_ms)

    def _skip_by(self, delta_ms):
        position = self.player.position_ms()
        if position < 0:
            return
        duration = self.player.duration_ms()
        target = max(0, position + delta_ms)
        if duration > 0:
            target = min(target, duration - 500)
        self._seek_to(target)

    def manual_map(self, tidal_track):
        """Apply a user-chosen Tidal track and play it."""
        self._submit(self._manual_map, tidal_track)

    def _manual_map(self, tidal_track):
        from .tidal import Match
        src = self.current
        if not src.has_track:
            return
        self.library.save_mapping(src.mapping_key, tidal_track.id, src.spotify_id)
        resolved = self.library.resolve(tidal_track)
        if resolved is None:
            self.status = "Could not stream that track"
            return
        self.muter.mute(force=True)
        self._reset_track_state()
        fresh = self.source.read(force=True)
        self._play(src, resolved, Match(tidal_track, "mapping"), False, fresh.is_playing)

    def mark_not_on_tidal(self, source_state=None):
        self._submit(self._mark_not_on_tidal, source_state)

    def _mark_not_on_tidal(self, source_state):
        src = source_state if source_state is not None else self.current
        if not src.has_track:
            return
        self.library.mark_not_on_tidal(src.mapping_key, src.spotify_id)
        if same_track(src, self.current) and self.state != State.FALLBACK:
            self._fallback(src, "Not on Tidal", popup=False)

    def search_tidal(self, query, limit=20):
        return self.library.search(query, limit=limit)

    def set_muting(self, enabled):
        self._submit(self._set_muting, enabled)

    def _set_muting(self, enabled):
        self.muter.enabled = enabled
        if enabled and self.state == State.PLAYING:
            self.muter.mute(force=True)
        elif not enabled:
            self.muter.unmute()

    def set_volume(self, level):
        self.player.set_volume(level)

    # --- reporting -------------------------------------------------------
    def snapshot(self):
        if self.state == State.FALLBACK:
            track = self.current.title or "Playing from Spotify"
        elif self.current_track is not None:
            track = self.current_track.name
        else:
            track = self.current.title or "Waiting for media..."

        source_track = ""
        if self.current.has_track:
            source_track = f"{self.current.title} - {self.current.artist}"

        low_confidence = (self.state == State.FALLBACK
                          or (self.match_label.startswith("search")
                              and self.match_score < CONFIDENT_SCORE))
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
            "match": self.match_label,
            "low_confidence": low_confidence,
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
