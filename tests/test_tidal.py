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
    def __init__(self, tid, name, artist="Artist", duration=200, refuse=(), hires=False,
                 isrc=None, album="Album"):
        self.id = tid
        self.is_hi_res_lossless = hires
        self.isrc = isrc
        self.name = self.full_name = name
        self.artist = NS(name=artist)
        self.artists = [self.artist]
        self.album = NS(name=album)
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
    lib = library([FakeTrack(1, "Song"), FakeTrack(7, "Song", hires=True)], isrc={"USX1": [7]})
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

    def load(token_type, access, refresh, expiry, is_pkce=False):
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


class DashStream:
    is_mpd = True
    bit_depth, sample_rate, audio_quality = 24, 96000, "HI_RES_LOSSLESS"
    track_replay_gain, track_peak_amplitude = 6.0, 0.9

    def get_stream_manifest(self):
        return NS(codecs="FLAC", urls=["https://seg/1.mp4"])

    def get_manifest_data(self):
        return "<MPD/>"


def test_TD10_dash_stream_is_written_as_a_manifest_with_its_real_format(library):
    track = FakeTrack(1, "Song")
    track.get_stream = DashStream
    resolved = library([track]).resolve(track)
    assert resolved.url.endswith(".mpd")
    with open(resolved.url, encoding="utf-8") as f:
        assert f.read() == "<MPD/>"
    assert resolved.quality == "FLAC 24-bit/96 kHz" and resolved.lossless
    # +6 dB would push a 0.9 peak past full scale: capped at ~0.92 dB.
    assert resolved.gain_db == pytest.approx(0.92, abs=0.01)


def test_TD11_lossy_stream_is_labelled_as_what_it_is(library):
    """debug.log said HI_RES_LOSSLESS while Tidal sent AAC to the old login."""
    track = FakeTrack(1, "Song")
    track.get_stream = lambda: NS(is_mpd=False, bit_depth=16, sample_rate=44100,
                                  audio_quality="HIGH", track_replay_gain=None,
                                  track_peak_amplitude=None,
                                  get_stream_manifest=lambda: NS(codecs="MP4A", urls=["https://a.mp4"]))
    resolved = library([track]).resolve(track)
    assert resolved.quality == "AAC 320 kbps" and not resolved.lossless
    assert resolved.url == "https://a.mp4"


@pytest.mark.parametrize("codec,bits,rate,expected", [
    ("flac", 16, 44100, "FLAC 16-bit/44.1 kHz"),
    ("FLAC", 24, 192000, "FLAC 24-bit/192 kHz"),
    ("mp4a.40.2", 16, 44100, "AAC"),
])
def test_TD12_format_labels(codec, bits, rate, expected):
    assert tidal.format_label(codec, bits, rate) == expected


def _pkce_session(lib, saved, monkeypatch, old_pkce=False):
    monkeypatch.setattr(credentials, "get_json",
                        lambda key: {"access_token": "old", "refresh_token": "r", "is_pkce": old_pkce})
    monkeypatch.setattr(credentials, "set_json", lambda key, data: saved.update(data))
    sess = lib.session
    sess.is_pkce = False

    def load(token_type, access, refresh, expiry, is_pkce=False):
        sess.is_pkce = is_pkce
        return True

    def process(token, is_pkce_token=True):
        sess.is_pkce = is_pkce_token
        sess.access_token = token["access_token"]

    sess.load_oauth_session = load
    sess.check_login = lambda: True
    sess.pkce_login_url = lambda: "https://login.tidal.com/authorize?x"
    sess.pkce_get_auth_token = lambda url: {"access_token": "pkce"} if "code=" in url else 1 / 0
    sess.process_auth_token = process
    sess.token_type, sess.access_token, sess.refresh_token, sess.expiry_time = "Bearer", "old", "r", None
    return sess


def test_TD13_old_login_is_offered_the_lossless_upgrade(library, monkeypatch):
    lib, saved, asked = library([]), {}, []
    sess = _pkce_session(lib, saved, monkeypatch)
    ask = lambda url: asked.append(url) or "https://tidal.com/android/login/auth?code=abc"
    assert lib.login(ask_redirect=ask) == tidal.LoginResult.OK
    assert asked and sess.is_pkce
    assert saved["access_token"] == "pkce" and saved["is_pkce"] is True


def test_TD14_cancelled_upgrade_keeps_the_old_login(library, monkeypatch):
    lib, saved = library([]), {}
    sess = _pkce_session(lib, saved, monkeypatch)
    assert lib.login(ask_redirect=lambda url: None) == tidal.LoginResult.OK
    assert not sess.is_pkce and saved["access_token"] == "old"


def test_TD15_pkce_login_is_not_asked_again(library, monkeypatch):
    lib, saved = library([]), {}
    _pkce_session(lib, saved, monkeypatch, old_pkce=True)
    assert lib.login(ask_redirect=lambda url: 1 / 0) == tidal.LoginResult.OK
    assert lib.is_pkce


class V2Request:
    """Tidal's v2 /tracks?filter[isrc]= endpoint, 2 items per page."""

    def __init__(self, items):
        self.items, self.calls = items, []

    def request(self, method, path, params=None, base_url=None):
        self.calls.append(dict(params))
        start = int(params.get("page[cursor]", 0))
        page = self.items[start:start + 2]
        links = {"meta": {"nextCursor": str(start + 2)}} if start + 2 < len(self.items) else {}
        data = [{"id": tid, "attributes": {"mediaTags": ["LOSSLESS"] + (["HIRES_LOSSLESS"] if hi else [])}}
                for tid, hi in page]
        return NS(json=lambda: {"data": data, "links": links})


def _v2(lib, items):
    lib.session.request = V2Request(items)
    lib.session.config.openapi_v2_location = "https://openapi.tidal.com/v2/"
    return lib.session.request


def test_TD16_isrc_prefers_a_hi_res_release_on_a_later_page(library):
    tracks = [FakeTrack(1, "Song", album="Hits 2024"), FakeTrack(2, "Song", album="Now 99"),
              FakeTrack(3, "Song", album="Album", hires=True)]
    lib = library(tracks)
    req = _v2(lib, [(1, False), (2, False), (3, True)])
    match = lib.find_match(TrackQuery("Song", "Artist", album="Hits 2024",
                                      duration_ms=200000, isrc="USX1"))
    assert match.track.id == 3 and match.method == "isrc"
    assert len(req.calls) == 2 and req.calls[1]["page[cursor]"] == "2"


def test_TD17_isrc_without_hi_res_still_matches(library):
    lib = library([FakeTrack(1, "Song"), FakeTrack(2, "Other")])
    _v2(lib, [(1, False), (2, False)])
    match = lib.find_match(TrackQuery("Song", "Artist", duration_ms=200000, isrc="USX1"))
    assert match.track.id == 1 and match.method == "isrc"


def test_TD18_search_swaps_to_hi_res_twin_of_same_recording(library):
    lib = library([FakeTrack(1, "Song", isrc="USX1", album="Album"),
                   FakeTrack(2, "Song", isrc="USX1", album="Best Of", hires=True),
                   FakeTrack(3, "Song", isrc="USX9", album="Live", hires=True)])
    match = lib.find_match(TrackQuery("Song", "Artist", album="Album", duration_ms=200000))
    assert match.track.id == 2


def test_TD19_hi_res_release_missed_by_isrc_paging_is_found_by_search(library):
    lib = library([FakeTrack(1, "Song", album="Hits"),
                   FakeTrack(2, "Song", isrc="USX1", album="Song", hires=True),
                   FakeTrack(3, "Song", isrc="USX9", album="Live", hires=True)])
    _v2(lib, [(1, False)])
    match = lib.find_match(TrackQuery("Song", "Artist", duration_ms=200000, isrc="USX1"))
    assert match.track.id == 2 and match.method == "isrc"
