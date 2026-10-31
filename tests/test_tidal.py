"""TidalLibrary (TD*): lookup order, caches, quality fallback, login outcomes."""

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("tidalapi")
import requests  # noqa: E402
import tidalapi  # noqa: E402

from spotifysync import credentials, tidal  # noqa: E402
from spotifysync.matching import MappingStore, TrackQuery  # noqa: E402

Q = tidalapi.Quality


class FakeTrack:
    def __init__(self, tid, name, artist="Artist", duration=200, refuse=()):
        self.id = tid
        self.name = self.full_name = name
        self.artist = NS(name=artist)
        self.artists = [self.artist]
        self.album = NS(name="Album")
        self.duration = duration
        self.refuse = set(refuse)
        self.session = None

    def get_url(self):
        quality = self.session.config.quality
        if quality in self.refuse:
            raise Exception("401 Client Error: Unauthorized")
        return f"https://cdn/{self.id}/{quality}"

    def get_stream(self):
        raise Exception("no stream")


class FakeSession:
    def __init__(self, tracks, isrc=None):
        self.tracks = {t.id: t for t in tracks}
        for t in tracks:
            t.session = self
        self.isrc = isrc or {}
        self.config = NS(quality=Q.hi_res_lossless)
        self.queries = []

    def track(self, tid):
        return self.tracks[tid]

    def search(self, query, models=None, limit=15):
        self.queries.append(query)
        words = query.lower().split()
        return {"tracks": [t for t in self.tracks.values() if t.name.lower().split()[0] in words]}

    def get_tracks_by_isrc(self, isrc):
        return [self.tracks[i] for i in self.isrc.get(isrc, [])]


@pytest.fixture
def library(tmp_path):
    def make(tracks, isrc=None):
        lib = tidal.TidalLibrary(session=FakeSession(tracks, isrc))
        lib.mappings = MappingStore(str(tmp_path / "mappings.json"))
        lib.auto_matches = MappingStore(str(tmp_path / "cache.json"))
        return lib
    return make


def test_TD1_saved_mapping_wins_without_searching(library):
    lib = library([FakeTrack(1, "Song"), FakeTrack(2, "Song")])
    lib.save_mapping("song|artist", 2)
    match = lib.find_match(TrackQuery("Song", "Artist", duration_ms=200000))
    assert match.track.id == 2 and match.method == "mapping"
    assert lib.session.queries == []


def test_TD2_not_on_tidal_is_remembered(library):
    lib = library([FakeTrack(1, "Song")])
    lib.mark_not_on_tidal("song|artist")
    query = TrackQuery("Song", "Artist")
    assert lib.is_not_on_tidal(query)
    assert lib.find_match(query) is None
    assert lib.session.queries == []


def test_TD3_isrc_is_tried_first(library):
    lib = library([FakeTrack(1, "Song"), FakeTrack(7, "Song")], isrc={"USX1": [7]})
    match = lib.find_match(TrackQuery("Song", "Artist", duration_ms=200000, isrc="USX1"))
    assert match.track.id == 7 and match.method == "isrc"
    assert lib.session.queries == []


def test_TD4_confident_matches_are_cached_for_next_time(library):
    lib = library([FakeTrack(1, "Song")])
    query = TrackQuery("Song", "Artist", duration_ms=200000)
    assert lib.find_match(query).method == "search"
    assert lib.find_match(query).method == "cache"
    assert len(lib.session.queries) == 1


def test_TD5_progressive_queries_reach_album_and_title_only(library):
    lib = library([FakeTrack(1, "Elsewhere", artist="Other")])
    lib.find_match(TrackQuery("Nothing", "Artist", album="Some Album", duration_ms=200000))
    assert lib.session.queries == ["Nothing Artist", "Nothing Some Album", "Nothing"]


def test_TD6_one_track_without_hires_does_not_downgrade_the_session(library):
    """debug.log: one 401 turned every later track into LOSSLESS."""
    lib = library([FakeTrack(1, "A", refuse={Q.hi_res_lossless}), FakeTrack(2, "B")])
    assert lib.resolve(lib.session.track(1)).quality == "LOSSLESS"
    assert lib.resolve(lib.session.track(2)).quality == "HI_RES_LOSSLESS"


def test_TD7_hires_disabled_after_repeated_refusals(library):
    tracks = [FakeTrack(i, f"T{i}", refuse={Q.hi_res_lossless}) for i in range(4)]
    lib = library(tracks)
    for t in tracks[:3]:
        lib.resolve(t)
    assert Q.hi_res_lossless in lib._disabled
    assert Q.hi_res_lossless not in lib._qualities()


def test_TD8_offline_login_is_retryable(library, monkeypatch):
    lib = library([])
    monkeypatch.setattr(credentials, "get_json", lambda key: {"access_token": "x", "refresh_token": "y"})

    def offline(*_a, **_k):
        raise requests.ConnectionError("network down")

    lib.session.load_oauth_session = offline
    assert lib.login() == tidal.LoginResult.OFFLINE


def test_TD9_rejected_access_token_is_refreshed_before_relogin(library, monkeypatch):
    lib = library([])
    saved = {}
    monkeypatch.setattr(credentials, "get_json", lambda key: {"access_token": "old", "refresh_token": "r"})
    monkeypatch.setattr(credentials, "set_json", lambda key, data: saved.update(data))
    calls = []

    def load(token_type, access, refresh, expiry):
        calls.append(access)
        return access == "new"

    def refresh(token):
        lib.session.access_token = "new"
        return True

    lib.session.load_oauth_session = load
    lib.session.token_refresh = refresh
    lib.session.check_login = lambda: True
    lib.session.token_type, lib.session.refresh_token, lib.session.expiry_time = "Bearer", "r", None
    assert lib.login() == tidal.LoginResult.OK
    assert calls == ["old", "new"]
    assert saved["access_token"] == "new"
