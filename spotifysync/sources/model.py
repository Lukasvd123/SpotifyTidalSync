"""What the source app is doing right now."""

from dataclasses import dataclass

from ..matching import make_mapping_key


@dataclass(frozen=True)
class SourceState:
    """A single reading of the source app.

    `track_id` is the important field. Every position here belongs to *this*
    track and must never be applied to another one: reading a position during a
    track transition and applying it to the newly loaded track is what made
    playback jump minutes into a song that had just started.
    """

    ok: bool = False
    track_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    position_ms: int = -1
    duration_ms: int = 0
    is_playing: bool = False
    is_dj_context: bool = False
    is_commentary: bool = False
    art_url: str = ""
    spotify_id: str = ""
    app_name: str = ""
    from_api: bool = False

    @property
    def mapping_key(self):
        return make_mapping_key(self.title, self.artist)

    @property
    def has_track(self):
        return bool(self.ok and self.title)

    def near_end(self, window_ms):
        """True when this reading sat within `window_ms` of the track's end."""
        if self.duration_ms <= 0 or self.position_ms < 0:
            return False
        return self.position_ms >= self.duration_ms - window_ms

    def __repr__(self):
        if self.is_commentary:
            return "SourceState(DJ commentary)"
        state = "playing" if self.is_playing else "paused"
        return (f"SourceState('{self.title}' by '{self.artist}' "
                f"{self.position_ms}/{self.duration_ms}ms {state})")


EMPTY = SourceState()
