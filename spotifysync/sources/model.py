"""What the source app is doing right now."""

from dataclasses import dataclass

from ..matching import make_mapping_key


class Content:
    TRACK = "track"
    # Spoken or non-music segments we cannot mirror: hand audio back to Spotify.
    COMMENTARY = "commentary"
    EPISODE = "episode"
    AD = "ad"

    PASSTHROUGH = (COMMENTARY, EPISODE, AD)


@dataclass(frozen=True)
class SourceState:
    """A single reading of the source app.

    `track_id` is the important field. Every position here belongs to *this*
    track and must never be applied to another one: reading a position during a
    track transition and applying it to the newly loaded track is what made
    playback jump minutes into a song that had just started.

    `read_at` is when the reading was taken (engine clock). Seek detection must
    extrapolate from that, not from when a cached copy was last looked at.
    """

    ok: bool = False
    track_id: str = ""
    title: str = ""
    artist: str = ""
    artists: tuple = ()
    album: str = ""
    position_ms: int = -1
    duration_ms: int = 0
    is_playing: bool = False
    is_dj_context: bool = False
    content: str = Content.TRACK
    art_url: str = ""
    spotify_id: str = ""
    isrc: str = ""
    app_name: str = ""
    from_api: bool = False
    is_remote: bool = False
    device_name: str = ""
    repeat_track: bool = False
    read_at: float = 0.0

    @property
    def mapping_key(self):
        return make_mapping_key(self.title, self.artist)

    @property
    def has_track(self):
        return bool(self.ok and self.title and self.content == Content.TRACK)

    @property
    def is_passthrough(self):
        return self.ok and self.content in Content.PASSTHROUGH

    @property
    def is_commentary(self):
        return self.ok and self.content == Content.COMMENTARY

    def position_at(self, now):
        """Position extrapolated to `now`, or -1 when unknown."""
        if self.position_ms < 0:
            return -1
        if not self.is_playing or now <= self.read_at:
            return self.position_ms
        return self.position_ms + int((now - self.read_at) * 1000)

    def near_end(self, window_ms, now=None):
        """True when this reading sat within `window_ms` of the track's end."""
        if self.duration_ms <= 0 or self.position_ms < 0:
            return False
        position = self.position_ms if now is None else self.position_at(now)
        return position >= self.duration_ms - window_ms

    def __repr__(self):
        if self.content != Content.TRACK:
            return f"SourceState({self.content})"
        state = "playing" if self.is_playing else "paused"
        return (f"SourceState('{self.title}' by '{self.artist}' "
                f"{self.position_ms}/{self.duration_ms}ms {state})")


EMPTY = SourceState()


def same_track(a, b):
    """Whether two readings are the same track.

    Readings can come from the API (Spotify id) or OS detection (no id on
    Windows), so ids decide when both have one, else the title|artist key.
    """
    if not a.ok or not b.ok:
        return False
    if a.track_id and a.track_id == b.track_id:
        return True
    if a.spotify_id and b.spotify_id:
        return a.spotify_id == b.spotify_id
    return bool(a.title) and a.mapping_key == b.mapping_key
