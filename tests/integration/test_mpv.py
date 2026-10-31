"""AudioPlayer against a real libmpv (IN*). Uses the null audio output, so it
plays nothing audible and needs no sound card. Skipped when libmpv is missing."""

import math
import os
import struct
import time
import wave
from types import SimpleNamespace

import pytest

try:
    import mpv
    mpv.MPV(ao="null", video=False, config=False).terminate()
    _usable, _reason = True, ""
except Exception as e:  # OSError when libmpv is missing
    _usable, _reason = False, repr(e)

if not _usable:
    # CI installs libmpv on purpose; a skip there would hide a broken setup.
    if os.environ.get("REQUIRE_MPV"):
        raise RuntimeError(f"libmpv required but not usable: {_reason}")
    pytest.skip(f"libmpv not usable: {_reason}", allow_module_level=True)

from spotifysync.audio import player as player_module  # noqa: E402

SECONDS = 4


@pytest.fixture(scope="module")
def tone(tmp_path_factory):
    path = tmp_path_factory.mktemp("audio") / "tone.wav"
    rate = 44100
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
                          for i in range(rate * SECONDS))
        w.writeframes(frames)
    return str(path)


@pytest.fixture
def player(monkeypatch):
    monkeypatch.setenv("PIPEWIRE_PROPS", "")   # restored afterwards
    p = player_module.AudioPlayer(ao="null")
    yield p
    p.stop()
    p.shutdown()


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_IN1_plays_to_the_end_and_reports_ended(player, tone):
    player.play_url(tone)
    assert player.wait_until_playing(timeout=5)
    assert wait_for(lambda: player.position_ms() > 300)
    assert player.duration_ms() == pytest.approx(SECONDS * 1000, abs=300)
    assert wait_for(lambda: player.status() == "ended", timeout=SECONDS + 4)
    assert player.finished()


def test_IN2_start_position_starts_mid_stream_without_a_seek(player, tone):
    player.play_url(tone, start_ms=2500)
    assert player.wait_until_playing(timeout=5)
    assert 2300 <= player.position_ms() < 3500


def test_IN3_preloaded_track_is_the_next_entry(player, tone):
    player.play_url(tone, start_ms=2000)
    assert player.wait_until_playing(timeout=5)
    player.preload(tone)
    assert player._has_next()
    assert wait_for(lambda: player.status() == "ended", timeout=SECONDS + 2)
    # keep-open=always: mpv must not run on into the queued track by itself.
    assert player.status() == "ended"
    player.play_url(tone)
    assert player.wait_until_playing(timeout=2)
    assert player.position_ms() < 1500
    assert player._get("playlist_count") == 1


def test_IN4_pause_resume_and_volume(player, tone):
    player.play_url(tone)
    assert player.wait_until_playing(timeout=5)
    player.pause()
    assert wait_for(lambda: player.status() == "paused")
    player.resume()
    assert wait_for(lambda: player.status() == "playing")
    player.set_volume(35)
    assert player.volume == 35


def test_IN5_unreachable_stream_reports_error(player):
    player.play_url("http://127.0.0.1:9/nothing.flac")
    assert not player.wait_until_playing(timeout=5)
    assert wait_for(lambda: player.status() == "error")


def test_IN6_new_track_clears_a_previous_error(player, tone):
    player.play_url("http://127.0.0.1:9/nothing.flac")
    assert wait_for(lambda: player.status() == "error")
    player.play_url(tone)
    assert player.status() != "error"
    assert player.wait_until_playing(timeout=5)


def test_IN7_ended_is_not_reported_between_tracks(player, tone):
    player.play_url(tone, start_ms=3000)
    assert wait_for(lambda: player.status() == "ended", timeout=5)
    player.play_url(tone)
    assert player.status() != "ended"


def test_IN8_untouched_output_at_full_volume(player, tone):
    player.play_url(tone)
    assert player.wait_until_playing(timeout=5)
    info = player.output_info()
    assert info["source"]["samplerate"] == info["output"]["samplerate"] == 44100
    player.set_volume(80)
    assert not player.output_info()["bit_perfect"]


def test_IN9_replaygain_filter_loads(player, tone):
    player.replaygain = True
    player.play_url(tone, gain_db=-3.5)
    assert player.wait_until_playing(timeout=5)
    assert player.status() == "playing"
    assert not player.output_info()["bit_perfect"]


def test_IN10_loadfile_without_index_argument(player, monkeypatch):
    """The Linux packages bundle an older libmpv: its loadfile takes no index
    and may not return the entry id."""
    calls = []
    old_mpv = SimpleNamespace(mpv_version_tuple=(0, 34, 1),
                              command=lambda *a: calls.append(a) or None)
    monkeypatch.setattr(player, "_get", lambda name, default=None:
                        [{"id": 4, "current": True}, {"id": 5}] if name == "playlist" else default)
    real, player._mpv = player._mpv, old_mpv
    try:
        assert player._load("x.flac", "replace", {"start": "1.000"}) == 4
        assert player._load("y.flac", "append", {}) == 5
    finally:
        player._mpv = real
    assert calls == [("loadfile", "x.flac", "replace", "start=1.000"),
                     ("loadfile", "y.flac", "append", "")]
