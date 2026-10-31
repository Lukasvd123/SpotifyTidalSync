"""Deterministic simulation of Spotify, VLC and Tidal around the real engine.

The real SyncEngine and PlaybackSource run unmodified; only the outside world
is faked. Time is a fake clock, so a whole album plays in milliseconds and every
run is identical. A recorder samples who is audible every 50ms, which is what
the gap/jump/double-audio assertions are made against.
"""

import socket
from dataclasses import dataclass, field

from spotifysync.config import POLL_INTERVAL
from spotifysync.sources import PlaybackSource
from spotifysync.sources.detector import DetectedMedia
from spotifysync.sync import SyncEngine
from spotifysync.tidal import Match, Prefetcher, Resolved

STEP = 0.05
HOSTNAME = socket.gethostname()


class Clock:
    def __init__(self, start=1000.0):
        self.start = start
        self.t = start
        self.listeners = []
        self.events = []            # (time, fn): user actions mid-flight

    def __call__(self):
        return self.t

    def at(self, offset_s, fn):
        """Run `fn` at `offset_s` seconds after the simulation started."""
        self.events.append((self.start + offset_s, fn))
        self.events.sort(key=lambda e: e[0])

    def advance(self, seconds):
        remaining = seconds
        while remaining > 1e-9:
            dt = min(STEP, remaining)
            self.t += dt
            remaining -= dt
            for listener in self.listeners:
                listener(dt)
            while self.events and self.events[0][0] <= self.t:
                _, fn = self.events.pop(0)
                fn()

    sleep = advance


# --- Spotify ------------------------------------------------------------------
@dataclass
class Track:
    id: str
    title: str
    artist: str = "Artist"
    duration_ms: int = 180000
    album: str = "Album"
    kind: str = "track"             # track | episode | ad | commentary
    isrc: str = ""
    local: bool = False

    @property
    def uri(self):
        return f"spotify:local:{self.title}" if self.local else f"spotify:track:{self.id}"


class FakeSpotify:
    """The (muted) Spotify app: a queue, a playhead and a mute flag."""

    def __init__(self, clock, queue, crossfade_ms=0, repeat_track=False, dj=False):
        self.clock = clock
        self.queue = list(queue)
        self.index = 0
        self.position = 0.0
        self.playing = False
        self.muted = False
        self.crossfade_ms = crossfade_ms
        self.repeat_track = repeat_track
        self.dj = dj
        self.device = {"id": "local-device", "name": HOSTNAME, "type": "Computer"}
        self.reject_api = set()     # API commands answered with 403
        self.commands = []          # (time, command)
        self.history = []           # (time, index, position, playing) for API lag
        clock.listeners.append(self._step)

    @property
    def track(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def _record(self):
        self.history.append((self.clock(), self.index, self.position, self.playing))
        if len(self.history) > 4000:
            del self.history[:2000]

    def _step(self, dt):
        if self.playing and self.track is not None:
            self.position += dt * 1000
            end = self.track.duration_ms - (self.crossfade_ms if self.track.kind == "track" else 0)
            if self.position >= end:
                if self.repeat_track:
                    self.position = 0.0
                else:
                    self._advance()
        self._record()

    def _advance(self):
        self.index += 1
        self.position = 0.0
        if self.track is None:
            self.playing = False

    def state_at(self, lag):
        """Index, position and playing as the API would report them `lag` seconds late."""
        if lag <= 0 or not self.history:
            return self.index, self.position, self.playing
        target = self.clock() - lag
        for t, index, position, playing in reversed(self.history):
            if t <= target:
                return index, position, playing
        t, index, position, playing = self.history[0]
        return index, position, playing

    # user and app control
    def play(self):
        self.commands.append((self.clock(), "play"))
        if self.track is not None:
            self.playing = True
        self._record()

    def pause(self):
        self.commands.append((self.clock(), "pause"))
        self.playing = False
        self._record()

    def next(self):
        self.commands.append((self.clock(), "next"))
        self._advance()
        if self.track is not None:
            self.playing = True
        self._record()

    def previous(self):
        self.commands.append((self.clock(), "previous"))
        if self.position > 3000 or self.index == 0:
            self.position = 0.0
        else:
            self.index -= 1
            self.position = 0.0
        self._record()

    def seek(self, ms):
        self.commands.append((self.clock(), f"seek:{int(ms)}"))
        self.position = float(ms)
        self._record()

    def count(self, prefix):
        return sum(1 for _, c in self.commands if c.startswith(prefix))


class FakeApi:
    """Stands in for SpotifyController, built on FakeSpotify."""

    def __init__(self, spotify, lag=0.0, available=True):
        self.spotify = spotify
        self.lag = lag
        self.available = available

    def login(self):
        return self.available

    def playback(self):
        if not self.available:
            return None
        sp = self.spotify
        index, position, playing = sp.state_at(self.lag)
        track = sp.queue[index] if 0 <= index < len(sp.queue) else None
        if track is None:
            return {"is_playing": False, "item": None, "device": sp.device,
                    "currently_playing_type": "unknown"}
        context = {"uri": "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"} if sp.dj else {"uri": "spotify:playlist:x"}
        base = {"is_playing": playing, "progress_ms": int(position), "device": sp.device,
                "context": context, "repeat_state": "track" if sp.repeat_track else "off"}
        if track.kind == "commentary":
            return {**base, "item": None, "currently_playing_type": "unknown"}
        if track.kind == "ad":
            return {**base, "item": None, "currently_playing_type": "ad"}
        if track.kind == "episode":
            return {**base, "currently_playing_type": "episode",
                    "item": {"type": "episode", "name": track.title, "id": track.id,
                             "duration_ms": track.duration_ms}}
        return {**base, "currently_playing_type": "track", "item": {
            "type": "track", "id": None if track.local else track.id, "uri": track.uri,
            "name": track.title, "is_local": track.local,
            "artists": [{"name": track.artist}], "album": {"name": track.album, "images": []},
            "duration_ms": track.duration_ms,
            "external_ids": {"isrc": track.isrc} if track.isrc else {},
        }}

    def queue(self, limit=5):
        sp = self.spotify
        if sp.dj:
            return []           # the DJ hides what it plays next
        upcoming = sp.queue[sp.index + 1: sp.index + 1 + limit]
        return [{"id": t.id, "name": t.title, "artists": [{"name": t.artist}],
                 "album": {"name": t.album}, "duration_ms": t.duration_ms}
                for t in upcoming if t.kind == "track"]

    def _command(self, name, fn, *args):
        if not self.available:
            return False
        if name in self.spotify.reject_api:
            return False
        fn(*args)
        return True

    def pause(self):
        return self._command("pause", self.spotify.pause)

    def resume(self):
        return self._command("resume", self.spotify.play)

    def next_track(self):
        return self._command("next", self.spotify.next)

    def previous_track(self):
        return self._command("previous", self.spotify.previous)

    def seek(self, ms):
        return self._command("seek", self.spotify.seek, ms)


class FakeDetector:
    """MPRIS/SMTC stand-in.

    position_mode: "live" (MPRIS), "events" (raw SMTC: only updates on
    play/pause/seek/track change) or "zero" (players that never report it).
    """

    def __init__(self, spotify, available=True, position_mode="live", with_id=True):
        self.spotify = spotify
        self.available = available
        self.position_mode = position_mode
        self.with_id = with_id
        self._event_position = 0.0
        self._event_key = None

    def read(self, preferred="spotify"):
        if not self.available:
            return None
        sp = self.spotify
        track = sp.track
        if track is None:
            return None
        key = (sp.index, sp.playing, len(sp.commands))
        if self.position_mode == "events" and key != self._event_key:
            self._event_key = key
            self._event_position = sp.position
        position = {"live": sp.position, "events": self._event_position, "zero": 0}[self.position_mode]
        kind = track.kind if track.kind in ("ad", "episode") else "track"
        return DetectedMedia(
            title=track.title, artist=track.artist, album=track.album,
            status="playing" if sp.playing else "paused", app_name="spotify",
            position_ms=int(position), duration_ms=track.duration_ms,
            spotify_id=track.id if self.with_id and not track.local else "", kind=kind)

    def send_control(self, command):
        if not self.available:
            return False
        {"play": self.spotify.play, "pause": self.spotify.pause,
         "next": self.spotify.next, "previous": self.spotify.previous,
         "play_pause": lambda: (self.spotify.pause() if self.spotify.playing
                                else self.spotify.play())}[command]()
        return True

    def seek(self, ms):
        if not self.available or self.position_mode != "live":
            return False
        self.spotify.seek(ms)
        return True


# --- VLC ------------------------------------------------------------------------
class FakeVLC:
    """AudioPlayer stand-in with buffering, seeks that re-buffer, and failures."""

    def __init__(self, clock, durations, buffer_s=1.0, seek_buffer_s=0.8):
        self.clock = clock
        self.durations = durations      # url -> duration_ms
        self.buffer_s = buffer_s
        self.seek_buffer_s = seek_buffer_s
        self.state = "idle"
        self.url = None
        self.position = 0.0
        self.duration = 0
        self._loading = 0.0
        self.seeks = []                 # (time, target)
        self.plays = []                 # (time, url, start_ms, gapless)
        self.fail_at = {}               # url -> position_ms where the stream errors
        self.fail_on_resume_after_s = None
        self._paused_at = None
        self.standby_url = None
        self._standby_ready_at = 0.0
        self.volume = 100
        # libvlc's get_time lags the audio on Tidal's MP4 streams (9-13s by the
        # end of a song, measured); the stream itself still ends on time.
        self.time_lag_ms = 0
        clock.listeners.append(self._step)

    def _step(self, dt):
        if self.state == "loading":
            self._loading -= dt
            if self._loading <= 0:
                self.state = "playing"
        elif self.state == "playing":
            self.position += dt * 1000
            fail = self.fail_at.get(self.url)
            if fail is not None and self.position >= fail:
                self.fail_at.pop(self.url)
                self.state = "error"
            elif self.position >= self.duration:
                self.state = "ended"

    def play_url(self, url, start_ms=0, gain_db=0.0):
        gapless = (start_ms <= 0 and url == self.standby_url
                   and self.clock() >= self._standby_ready_at)
        self.plays.append((self.clock(), url, start_ms, gapless))
        self.url = url
        self.duration = self.durations[url]
        self.position = float(start_ms)
        self.standby_url = None
        if gapless:
            self.state = "playing"
        else:
            self.state = "loading"
            self._loading = self.buffer_s

    def preload(self, url, gain_db=0.0):
        if url != self.standby_url:
            self.standby_url = url
            self._standby_ready_at = self.clock() + self.buffer_s

    def clear_preload(self):
        self.standby_url = None

    def pause(self):
        if self.state == "playing":
            self.state = "paused"
            self._paused_at = self.clock()

    def resume(self):
        if self.state == "paused":
            paused_for = self.clock() - (self._paused_at or self.clock())
            if self.fail_on_resume_after_s is not None and paused_for > self.fail_on_resume_after_s:
                self.state = "error"
            else:
                self.state = "playing"

    def stop(self):
        self.state = "idle"
        self.url = None

    def is_playing(self):
        return self.state == "playing"

    def status(self):
        return self.state

    def position_ms(self):
        if self.state in ("idle", "ended", "error"):
            return -1
        lag = self.time_lag_ms * self.position / max(1, self.duration)
        return max(0, int(self.position - lag))

    def duration_ms(self):
        return -1 if self.state in ("idle", "ended", "error") else self.duration

    def remaining_ms(self):
        position, duration = self.position_ms(), self.duration_ms()
        if duration <= 0 or position < 0:
            return -1
        return max(0, duration - position)

    def has_track(self):
        return self.duration_ms() > 0

    def finished(self):
        return self.state in ("ended", "error")

    def output_info(self):
        return {"bit_perfect": self.volume == 100}

    def seek(self, ms):
        self.seeks.append((self.clock(), int(ms)))
        self.position = float(max(0, ms))
        if self.state == "playing":
            self.state = "loading"
            self._loading = self.seek_buffer_s

    def wait_until_playing(self, timeout=6.0):
        waited = 0.0
        while waited < timeout:
            if self.state == "playing":
                return True
            if self.state == "error":
                return False
            self.clock.sleep(0.05)
            waited += 0.05
        return self.state == "playing"

    def set_volume(self, level):
        self.volume = level

    def get_audio_devices(self):
        return []


# --- Tidal ----------------------------------------------------------------------
@dataclass
class TidalTrack:
    id: int
    name: str
    duration: int                    # seconds, like tidalapi


class FakeLibrary:
    """Catalogue keyed by Spotify title. `tidal_ms` overrides the Tidal
    duration (masters often differ from Spotify's by a few seconds)."""

    def __init__(self, tracks, tidal_ms=None, missing=(), unstreamable=(),
                 sleep=None, search_s=0.8):
        self.sleep = sleep
        self.search_s = search_s
        self.background = False
        self.durations = {}
        self.catalogue = {}
        self.missing = set(missing)
        self.unstreamable = set(unstreamable)
        self.not_on_tidal = set()
        self.searches = []
        self.resolves = []
        self.favorites = []
        self.mappings = {}
        self._url_seq = 0
        tidal_ms = tidal_ms or {}
        for i, t in enumerate(tracks):
            if t.kind != "track":
                continue
            ms = tidal_ms.get(t.title, t.duration_ms)
            self.catalogue[t.title] = TidalTrack(id=5000 + i, name=t.title, duration=ms / 1000)

    def login(self, open_browser=None, ask_redirect=None):
        return "ok"

    def is_not_on_tidal(self, query, spotify_id=None):
        return query.title in self.not_on_tidal

    def _wait(self, seconds):
        # Background (prefetch) work runs on its own thread in the real app
        # and must not hold up the engine's clock.
        if self.sleep and not self.background:
            self.sleep(seconds)

    def run_background(self, fn):
        self.background = True
        try:
            fn()
        finally:
            self.background = False

    def find_match(self, query, spotify_id=None):
        self.searches.append(query.title)
        self._wait(self.search_s)
        if query.title in self.missing or query.title not in self.catalogue:
            return None
        return Match(self.catalogue[query.title], "search", 200.0)

    def ensure_session(self):
        return True

    def resolve(self, track):
        self.resolves.append(track.name)
        self._wait(0.3)
        if track.name in self.unstreamable:
            return None
        self._url_seq += 1
        url = f"tidal://{track.id}/{self._url_seq}"
        self.durations[url] = int(track.duration * 1000)
        return Resolved(track=track, url=url, quality="LOSSLESS")

    def cover_url(self, track):
        return ""

    def favorite(self, track_id):
        self.favorites.append(track_id)
        return True

    def save_mapping(self, key, tidal_id, spotify_id=None):
        self.mappings[key] = tidal_id

    def mark_not_on_tidal(self, key, spotify_id=None):
        title = key.split("|")[0]
        for name in self.catalogue:
            if name.lower() == title:
                self.not_on_tidal.add(name)
        self.not_on_tidal.add(title)

    def search(self, query, limit=15):
        return []


class FakeMuter:
    def __init__(self, spotify):
        self.spotify = spotify
        self.enabled = True
        self.events = []

    def mute(self, force=False):
        if self.enabled:
            self.spotify.muted = True

    def unmute(self):
        self.spotify.muted = False
        self.events.append("unmute")

    def restore(self):
        self.spotify.muted = False
        self.events.append("restore")

    def recover_from_crash(self):
        pass

    @property
    def is_muted(self):
        return self.spotify.muted


# --- world ----------------------------------------------------------------------
@dataclass
class Sample:
    t: float
    vlc: bool
    spotify: bool
    vlc_url: str
    spotify_index: int
    spotify_playing: bool


@dataclass
class World:
    clock: Clock
    spotify: FakeSpotify
    vlc: FakeVLC
    library: FakeLibrary
    engine: SyncEngine
    source: PlaybackSource
    samples: list = field(default_factory=list)
    prompts: list = field(default_factory=list)

    def run(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            self.engine.tick()
            self.clock.advance(POLL_INTERVAL)

    def run_until(self, predicate, timeout=600):
        end = self.clock() + timeout
        while self.clock() < end:
            if predicate():
                return True
            self.engine.tick()
            self.clock.advance(POLL_INTERVAL)
        return predicate()

    # --- assertions helpers ----------------------------------------------
    def window(self, start, end):
        return [s for s in self.samples if start <= s.t <= end]

    def longest_silence(self, start=0.0, end=float("inf")):
        """Longest stretch with nothing audible while Spotify had something to play."""
        longest = run = 0.0
        for s in self.window(start, end):
            silent = not s.vlc and not s.spotify
            wanted = s.spotify_index < len(self.spotify.queue)
            if silent and wanted:
                run += STEP
                longest = max(longest, run)
            else:
                run = 0.0
        return longest

    def double_audio(self):
        return sum(STEP for s in self.samples if s.vlc and s.spotify)

    def seeks_between(self, start, end):
        return [s for s in self.vlc.seeks if start <= s[0] <= end]

    def played_titles(self):
        by_url = {}
        for title, track in self.library.catalogue.items():
            by_url[track.id] = title
        out = []
        for _, url, _, _ in self.vlc.plays:
            tid = int(url.split("/")[2])
            out.append(by_url[tid])
        return out


def make_world(queue, *, api=True, api_lag=0.0, detector=True, position_mode="live",
               with_id=True, crossfade_ms=0, repeat_track=False, dj=False,
               tidal_ms=None, missing=(), unstreamable=(), buffer_s=1.0,
               start_playing=True, start_position_ms=0):
    clock = Clock()
    spotify = FakeSpotify(clock, queue, crossfade_ms=crossfade_ms,
                          repeat_track=repeat_track, dj=dj)
    spotify.position = float(start_position_ms)
    spotify.playing = start_playing
    library = FakeLibrary(queue, tidal_ms=tidal_ms, missing=missing,
                          unstreamable=unstreamable, sleep=clock.sleep)
    vlc = FakeVLC(clock, library.durations, buffer_s=buffer_s)
    source = PlaybackSource(api=FakeApi(spotify, lag=api_lag, available=api),
                            detector=FakeDetector(spotify, available=detector,
                                                  position_mode=position_mode, with_id=with_id),
                            clock=clock)
    prefetch = Prefetcher(library, source, run_async=library.run_background)
    engine = SyncEngine(player=vlc, source=source, library=library,
                        muter=FakeMuter(spotify), prefetch=prefetch,
                        clock=clock, sleep=clock.sleep)
    world = World(clock, spotify, vlc, library, engine, source)
    engine.on_match_needed = world.prompts.append

    def sample(_dt):
        audible_spotify = (spotify.playing and not spotify.muted and spotify.track is not None)
        world.samples.append(Sample(clock(), vlc.state == "playing", audible_spotify,
                                    vlc.url or "", spotify.index, spotify.playing))

    clock.listeners.append(sample)
    return world


def album(n=4, duration_ms=180000, prefix="Song"):
    return [Track(id=f"id{i}", title=f"{prefix} {i}", duration_ms=duration_ms) for i in range(n)]
