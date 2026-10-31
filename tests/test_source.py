"""PlaybackSource (SR*): device filtering, content types, control fallbacks."""

import pytest

from sim import Clock, FakeApi, FakeDetector, FakeSpotify, Track, album
from spotifysync.sources import Content, PlaybackSource


@pytest.fixture
def world():
    clock = Clock()
    spotify = FakeSpotify(clock, album(2))
    spotify.playing = True
    source = PlaybackSource(api=FakeApi(spotify), detector=FakeDetector(spotify), clock=clock)
    source._local_device = None
    return clock, spotify, source


def test_SR1_this_computer_is_learned_by_hostname(world):
    _, spotify, source = world
    state = source.read(force=True)
    assert not state.is_remote and state.title == "Song 0"
    assert source._local_device == "local-device"


def test_SR2_phone_playback_is_remote(world):
    _, spotify, source = world
    spotify.device = {"id": "phone", "name": "Pixel", "type": "Smartphone"}
    assert source.read(force=True).is_remote


def test_SR3_other_computer_is_remote_once_this_one_is_known(world):
    _, spotify, source = world
    source.read(force=True)
    spotify.device = {"id": "laptop", "name": "Laptop", "type": "Computer"}
    assert source.read(force=True).is_remote


def test_SR4_unknown_name_learned_when_os_detection_agrees(world):
    _, spotify, source = world
    spotify.device = {"id": "renamed", "name": "My Desktop", "type": "Computer"}
    assert not source.read(force=True).is_remote
    assert source._local_device == "renamed"


def test_SR5_content_types(world):
    clock, spotify, source = world
    spotify.queue = [Track(id="e", title="Ep", kind="episode"), Track(id="a", title="", kind="ad")]
    assert source.read(force=True).content == Content.EPISODE
    spotify.index = 1
    assert source.read(force=True).content == Content.AD
    spotify.dj = True
    spotify.queue[1] = Track(id="c", title="", kind="commentary")
    assert source.read(force=True).is_commentary


def test_SR6_local_file_gets_an_id(world):
    _, spotify, source = world
    spotify.queue = [Track(id="x", title="Local", local=True)]
    state = source.read(force=True)
    assert state.track_id == "spotify:local:Local" and state.spotify_id == ""


def test_SR7_readings_are_timestamped_and_cached(world):
    clock, _, source = world
    first = source.read(force=True)
    clock.advance(0.5)
    assert source.read() is first
    clock.advance(0.6)
    second = source.read()
    assert second.read_at > first.read_at


def test_SR8_api_rejection_falls_back_to_media_keys(world):
    _, spotify, source = world
    spotify.reject_api = {"pause"}
    assert source.pause()
    assert source.paused_by_us and not spotify.playing


def test_SR9_failed_pause_is_not_owed_a_resume(world):
    _, spotify, source = world
    spotify.reject_api = {"pause"}
    source.detector.available = False
    assert not source.pause()
    assert not source.paused_by_us


def test_SR10_user_pause_via_toggle_is_not_ours(world):
    _, spotify, source = world
    source.toggle_play()
    assert not spotify.playing and not source.paused_by_us


def test_SR11_detector_only_identity_uses_spotify_id(world):
    _, spotify, source = world
    source.api.available = False
    state = source.read(force=True)
    assert state.track_id == "id0" and state.spotify_id == "id0"
