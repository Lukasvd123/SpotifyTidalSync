"""AudioPlayer against a real libvlc (IN*). Uses the dummy audio output, so it
plays nothing audible and needs no sound card. Skipped when VLC is missing."""

import math
import os
import struct
import time
import wave

import pytest

try:
    import vlc
    _usable = vlc.Instance("--aout=dummy") is not None
except Exception as e:  # python-vlc raises several types when libvlc is missing
    _usable, _reason = False, repr(e)
else:
    _reason = "vlc.Instance() returned None"

if not _usable:
    # CI installs VLC on purpose; a skip there would hide a broken setup.
    if os.environ.get("REQUIRE_VLC"):
        raise RuntimeError(f"libvlc required but not usable: {_reason}")
    pytest.skip(f"libvlc not usable: {_reason}", allow_module_level=True)

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
    return "file://" + str(path) if os.name != "nt" else "file:///" + str(path).replace("\\", "/")


@pytest.fixture
def player(monkeypatch):
    monkeypatch.setattr(player_module, "build_vlc_args",
                        lambda: ["--no-video", "--aout=dummy", "--verbose=-1"])
    p = player_module.AudioPlayer()
    yield p
    p.stop()
    p.clear_preload()


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


def test_IN2_start_time_starts_mid_stream_without_a_seek(player, tone):
    player.play_url(tone, start_ms=2500)
    assert player.wait_until_playing(timeout=5)
    # VLC reports Playing ~250ms before the start position shows up in
    # get_time(), so wait for the first real reading.
    assert wait_for(lambda: player.position_ms() > 0)
    assert player.position_ms() >= 2300


def test_IN3_preloaded_track_starts_from_the_standby_player(player, tone):
    player.preload(tone)
    assert wait_for(player._standby_ready)
    standby = player._standby
    player.play_url(tone)
    assert player.player is standby
    assert player.wait_until_playing(timeout=2)
    assert player.position_ms() < 1500


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
    assert wait_for(lambda: player.status() in ("error", "ended"))
