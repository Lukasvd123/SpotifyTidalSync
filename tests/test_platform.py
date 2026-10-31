"""Mute handling, settings and the instance lock (PL*). Run on every OS in CI."""

import json
import os

import pytest

from spotifysync import config
from spotifysync.audio import mute

PACTL_OUTPUT = """\
Sink Input #41
	Driver: PipeWire
	Mute: no
	Properties:
		application.name = "spotify"
		application.process.binary = "spotify"
		application.process.id = "4242"
Sink Input #57
	Mute: yes
	Properties:
		application.name = "VLC media player (LibVLC 3.0.24)"
		application.process.id = "1111"
Sink Input #60
	Mute: no
	Properties:
		application.name = "Firefox"
		media.name = "Spotify - Web Player"
"""


def test_PL1_pactl_output_is_parsed(monkeypatch):
    monkeypatch.setattr(mute.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": PACTL_OUTPUT})())
    sinks = mute._pactl_sink_inputs()
    assert [i for i, _ in sinks] == ["41", "57", "60"]
    assert sinks[0][1]["_mute"] == "no" and sinks[1][1]["_mute"] == "yes"
    assert mute._is_spotify(sinks[0][1])
    assert mute._is_ours(sinks[1][1])


def test_PL2_mute_marker_survives_a_crash_and_is_recovered(monkeypatch):
    applied = []
    muter = mute.SourceMuter(enabled=True)
    monkeypatch.setattr(muter, "_apply", applied.append)
    muter.mute(force=True)
    assert os.path.exists(config.MUTE_MARKER_FILE)

    # A new process after a crash.
    fresh = mute.SourceMuter(enabled=True)
    monkeypatch.setattr(fresh, "_apply", applied.append)
    fresh.recover_from_crash()
    assert applied == [True, False]
    assert not os.path.exists(config.MUTE_MARKER_FILE)


def test_PL3_restore_unmutes_and_clears_marker(monkeypatch):
    applied = []
    muter = mute.SourceMuter(enabled=True)
    monkeypatch.setattr(muter, "_apply", applied.append)
    muter.mute(force=True)
    muter.restore()
    assert applied == [True, False]
    assert not os.path.exists(config.MUTE_MARKER_FILE)


def test_PL4_mute_is_throttled_unless_forced(monkeypatch):
    now = [0.0]
    applied = []
    muter = mute.SourceMuter(enabled=True, clock=lambda: now[0])
    monkeypatch.setattr(muter, "_apply", applied.append)
    muter.mute()
    muter.mute()
    now[0] = 10.0
    muter.mute()
    muter.mute(force=True)
    assert applied == [True, True, True]


def test_PL5_corrupt_settings_fall_back_to_defaults_and_are_kept():
    with open(config.SETTINGS_FILE, "w") as f:
        f.write("{not json")
    settings = config.load_settings()
    assert settings["mute_spotify"] is True
    assert os.path.exists(config.SETTINGS_FILE + ".corrupt")


def test_PL6_settings_round_trip_unicode():
    config.save_setting("last_device_id", "Kopfhörer — 東京")
    assert config.load_settings()["last_device_id"] == "Kopfhörer — 東京"
    with open(config.SETTINGS_FILE, encoding="utf-8") as f:
        assert json.load(f)["last_device_id"] == "Kopfhörer — 東京"


def test_PL7_second_instance_is_refused_without_clobbering_the_pid():
    main = pytest.importorskip("spotifysync.main")
    first = main.acquire_instance_lock()
    assert first is not None
    try:
        assert main.acquire_instance_lock() is None
        with open(os.path.join(config.APPDATA_DIR, "instance.lock")) as f:
            assert f.read() == str(os.getpid())
    finally:
        first.close()


def test_PL8_data_dir_override():
    assert config.APPDATA_DIR == os.environ["SPOTIFYSYNC_HOME"]
