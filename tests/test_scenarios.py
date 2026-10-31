"""End-to-end playback scenarios, run against the real engine in the simulator.

Every test id (ST1, BD2, ...) matches a row in docs/SCENARIOS.md.
"""

import pytest

from sim import Track, album, make_world
from spotifysync.sync import State

# Startup has to search, fetch and buffer the first track.
STARTUP_S = 3.0
# Silence allowed at a track boundary.
BOUNDARY_SILENCE_S = 1.5


def start_time(world, index=0):
    """Clock time at which VLC started the index-th play."""
    return world.vlc.plays[index][0]


def assert_clean(world):
    assert world.double_audio() <= 0.2, "Spotify and Tidal were audible together"


def aligned(world, tolerance_ms=1500):
    return abs(world.spotify.position - world.vlc.position) <= tolerance_ms


# --- start stability: no skipping around at the beginning --------------------
def test_ST1_fresh_start_plays_from_zero_without_seeks():
    w = make_world(album(2))
    w.run(20)
    assert w.vlc.plays[0][2] == 0
    assert w.seeks_between(0, float("inf")) == []
    assert w.vlc.state == "playing"
    assert aligned(w)
    assert_clean(w)


def test_ST2_mid_song_start_uses_start_time_not_a_seek():
    w = make_world(album(2), start_position_ms=90000)
    w.run(15)
    start = w.vlc.plays[0][2]
    assert 89000 <= start <= 93000
    assert w.seeks_between(0, float("inf")) == []
    assert aligned(w)


def test_ST3_slow_buffer_does_not_cause_catch_up_seeks():
    w = make_world(album(2), buffer_s=4.0)
    w.run(25)
    assert w.seeks_between(0, float("inf")) == []
    assert aligned(w)


def test_ST4_stale_api_progress_after_our_seek_is_not_a_user_seek():
    w = make_world(album(2), api_lag=2.0)
    w.run(30)
    assert w.seeks_between(0, float("inf")) == []


@pytest.mark.parametrize("mode", ["events", "zero"])
def test_ST5_ST6_unreliable_os_positions_never_move_our_stream(mode):
    # events: raw Windows SMTC, only updated on play/pause/seek/track change.
    # zero: players that never report a position.
    w = make_world(album(2), api=False, position_mode=mode, with_id=False)
    w.run(170)
    assert w.seeks_between(0, float("inf")) == []
    assert_clean(w)


def test_ST7_waits_while_spotify_is_paused_at_startup():
    w = make_world(album(2), start_playing=False)
    w.run(10)
    assert w.vlc.plays == []
    assert w.engine.status == "Waiting for playback..."
    w.spotify.play()
    w.run(STARTUP_S)
    assert w.vlc.state == "playing"


# --- boundaries -------------------------------------------------------------
def test_BD1_shorter_tidal_master_skips_spotify_instead_of_silence():
    """The 1-6s gaps in debug.log: our master ended first and we waited."""
    w = make_world(album(3), tidal_ms={"Song 0": 174000})
    w.run(STARTUP_S)
    t0 = start_time(w)
    w.run(185)
    assert w.played_titles()[:2] == ["Song 0", "Song 1"]
    assert w.longest_silence(t0 + 5) <= BOUNDARY_SILENCE_S
    assert w.spotify.count("next") >= 1
    assert_clean(w)


def test_BD2_crossfade_never_cuts_our_longer_master():
    w = make_world(album(3), crossfade_ms=10000, tidal_ms={"Song 0": 185000})
    w.run(STARTUP_S)
    t0 = start_time(w)
    w.run(200)
    titles = w.played_titles()
    assert titles[:2] == ["Song 0", "Song 1"]
    # Song 1 only started once our 185s of Song 0 had played out.
    assert start_time(w, 1) - t0 >= 184.0
    assert w.longest_silence(t0 + 5) <= BOUNDARY_SILENCE_S
    assert_clean(w)


def test_BD3_equal_lengths_switch_cleanly():
    w = make_world(album(3))
    w.run(STARTUP_S)
    t0 = start_time(w)
    w.run(400)
    assert w.played_titles()[:3] == ["Song 0", "Song 1", "Song 2"]
    assert w.longest_silence(t0 + 5) <= BOUNDARY_SILENCE_S
    assert_clean(w)


def test_BD4_next_track_is_prebuffered_for_a_gapless_switch():
    w = make_world(album(3))
    w.run(STARTUP_S + 185)
    assert len(w.vlc.plays) >= 2
    assert w.vlc.plays[1][3], "second track did not start from the pre-buffer"


def test_BD5_repeat_one_plays_the_track_again():
    w = make_world(album(2), repeat_track=True)
    w.run(STARTUP_S)
    t0 = start_time(w)
    w.run(200)
    assert w.played_titles()[:2] == ["Song 0", "Song 0"]
    assert w.longest_silence(t0 + 5) <= 3.0


def test_BD6_same_track_twice_in_a_row_is_not_skipped():
    a = Track(id="dup", title="Twice")
    w = make_world([a, Track(id="dup", title="Twice"), Track(id="b", title="After")])
    w.run(STARTUP_S + 200)
    assert w.played_titles()[:2] == ["Twice", "Twice"]
    assert w.spotify.index == 1, "the duplicate was skipped"


def test_BD7_end_of_queue_goes_idle():
    w = make_world(album(1, duration_ms=60000))
    w.run(80)
    assert w.engine.state == State.IDLE
    assert w.vlc.state != "playing"


def test_BD8_shorter_master_with_long_crossfade():
    w = make_world(album(3), crossfade_ms=12000, tidal_ms={"Song 0": 176000})
    w.run(STARTUP_S)
    t0 = start_time(w)
    w.run(190)
    assert w.played_titles()[:2] == ["Song 0", "Song 1"]
    assert w.longest_silence(t0 + 5) <= BOUNDARY_SILENCE_S


# --- user actions -----------------------------------------------------------
def test_UA1_skip_in_spotify_switches_promptly():
    w = make_world(album(3))
    w.clock.at(60, w.spotify.next)
    w.run(64)
    assert w.played_titles()[:2] == ["Song 0", "Song 1"]
    assert start_time(w, 1) - (w.clock.start + 60) <= 3.0


def test_UA2_skip_burst_does_not_play_every_track_in_between():
    w = make_world(album(6))
    for i in range(4):
        w.clock.at(30 + i * 0.4, w.spotify.next)
    w.run(40)
    titles = w.played_titles()
    assert titles[-1] == "Song 4"
    assert len(titles) <= 3, f"loaded intermediate tracks: {titles}"


def test_UA3_seek_in_spotify_is_followed_once():
    w = make_world(album(2))
    w.clock.at(40, lambda: w.spotify.seek(100000))
    w.run(50)
    seeks = w.seeks_between(0, float("inf"))
    assert len(seeks) == 1
    assert abs(seeks[0][1] - 100000) <= 2500
    assert seeks[0][0] - (w.clock.start + 40) <= 3.0


def test_UA4_pause_and_resume_are_mirrored():
    w = make_world(album(2))
    w.clock.at(30, w.spotify.pause)
    w.clock.at(40, w.spotify.play)
    w.run(32)
    assert w.vlc.state == "paused"
    w.run(10)
    assert w.vlc.state == "playing"
    assert w.seeks_between(0, float("inf")) == []
    assert aligned(w)


def test_UA5_app_next_button():
    w = make_world(album(3))
    w.run(30)
    w.engine.next_track()
    w.run(3)
    assert w.played_titles()[-1] == "Song 1"


def test_UA6_app_seek_moves_both_without_echo():
    w = make_world(album(2))
    w.run(30)
    w.engine.seek_to(120000)
    w.run(10)
    seeks = w.seeks_between(0, float("inf"))
    assert len(seeks) == 1 and seeks[0][1] == 120000
    assert w.spotify.count("seek:120000") == 1
    assert aligned(w)


def test_UA7_previous_restarts_the_same_track():
    w = make_world(album(2))
    w.clock.at(50, w.spotify.previous)
    w.run(55)
    seeks = w.seeks_between(0, float("inf"))
    assert len(seeks) == 1 and seeks[0][1] <= 3000


def test_UA8_volume_slider_sets_player_volume():
    w = make_world(album(1))
    w.engine.set_volume(40)
    assert w.vlc.volume == 40


# --- spotify modes ----------------------------------------------------------
@pytest.mark.parametrize("kind", ["episode", "ad"])
def test_SM1_SM2_non_music_is_handed_back_to_spotify(kind):
    q = [Track(id="s0", title="Song 0", duration_ms=40000),
         Track(id="x", title="Something", kind=kind, duration_ms=30000),
         Track(id="s1", title="Song 1")]
    w = make_world(q)
    w.run(55)
    assert w.engine.state == State.PASSTHROUGH
    assert not w.spotify.muted
    assert w.vlc.state != "playing"
    assert "Something" not in w.library.searches
    assert w.prompts == []
    w.run(25)
    assert w.played_titles()[-1] == "Song 1"
    assert w.spotify.muted
    assert_clean(w)


def test_SM3_dj_commentary_waits_for_our_song_then_unmutes():
    q = [Track(id="s0", title="Song 0"),
         Track(id="dj", title="", kind="commentary", duration_ms=20000),
         Track(id="s1", title="Song 1")]
    w = make_world(q, dj=True, crossfade_ms=8000)
    w.run(STARTUP_S)
    t0 = start_time(w)
    w.run(176)
    # Inside the crossfade: our song still plays, Spotify stays muted.
    assert w.vlc.state == "playing" and w.spotify.muted
    w.run(10)
    assert w.engine.state == State.PASSTHROUGH and not w.spotify.muted
    w.run(25)
    assert w.played_titles()[-1] == "Song 1"
    assert any(s.spotify for s in w.window(t0 + 180, t0 + 195))
    assert_clean(w)


def test_SM4_playback_on_another_device_is_left_alone():
    w = make_world(album(2))
    w.spotify.device = {"id": "phone", "name": "Pixel", "type": "Smartphone"}
    w.run(20)
    assert w.vlc.plays == []
    assert w.engine.state == State.REMOTE
    assert w.spotify.count("pause") == 0 and w.spotify.count("seek") == 0


def test_SM5_local_files_are_detected():
    q = [Track(id="l1", title="Local A", local=True, duration_ms=60000),
         Track(id="l2", title="Local B", local=True, duration_ms=60000)]
    w = make_world(q)
    w.run(STARTUP_S + 65)
    assert w.played_titles()[:2] == ["Local A", "Local B"]


def test_SM6_non_latin_titles_are_distinct_tracks_without_the_api():
    q = [Track(id="j1", title="東京", artist="アーティスト", duration_ms=60000),
         Track(id="j2", title="大阪", artist="アーティスト", duration_ms=60000)]
    w = make_world(q, api=False, with_id=False)
    w.run(STARTUP_S + 65)
    assert w.played_titles()[:2] == ["東京", "大阪"]


# --- failures ---------------------------------------------------------------
def test_FL1_no_match_unmutes_spotify_and_asks_once():
    w = make_world(album(3), missing={"Song 1"})
    w.run(STARTUP_S + 185)
    assert w.engine.state == State.FALLBACK
    assert not w.spotify.muted
    assert [p.title for p in w.prompts] == ["Song 1"]
    w.run(185)
    assert w.played_titles()[-1] == "Song 2"
    assert w.spotify.muted
    assert [p.title for p in w.prompts] == ["Song 1"]
    assert_clean(w)


def test_FL2_stream_drop_reconnects_at_the_same_position():
    w = make_world(album(2))
    w.clock.at(10, lambda: w.vlc.fail_at.update({w.vlc.url: 60000}))
    w.run(80)
    assert len(w.vlc.plays) == 2
    assert 59000 <= w.vlc.plays[1][2] <= 61000
    assert w.spotify.muted
    assert w.longest_silence(w.clock.start + 10) <= 2.5


def test_FL3_stream_that_keeps_failing_falls_back_to_spotify():
    w = make_world(album(2))
    w.clock.at(10, lambda: (w.vlc.fail_at.update({w.vlc.url: 60000}),
                            w.library.unstreamable.add("Song 0")))
    w.run(80)
    assert w.engine.state == State.FALLBACK
    assert not w.spotify.muted


def test_FL4_resume_after_long_pause_recovers_dead_stream():
    w = make_world(album(2))
    w.vlc.fail_on_resume_after_s = 600
    w.clock.at(30, w.spotify.pause)
    w.clock.at(30 + 1200, w.spotify.play)
    w.run(30 + 1200 + 5)
    assert w.vlc.state == "playing"
    assert abs(w.vlc.plays[-1][2] - 30000) <= 2000
    assert w.spotify.muted


def test_FL5_unstreamable_match_falls_back_without_nagging():
    w = make_world(album(2), unstreamable={"Song 0"})
    w.run(10)
    assert w.engine.state == State.FALLBACK
    assert not w.spotify.muted
    assert w.prompts == []


def test_FL6_api_403_falls_back_to_media_keys():
    w = make_world(album(2))
    w.spotify.reject_api = {"pause", "resume", "seek"}
    w.run(20)
    assert w.vlc.state == "playing"
    assert w.spotify.playing
    assert aligned(w)


def test_FL8_not_on_tidal_skips_search_and_popup():
    w = make_world(album(2))
    w.library.not_on_tidal.add("Song 0")
    w.run(10)
    assert "Song 0" not in w.library.searches
    assert w.engine.state == State.FALLBACK and not w.spotify.muted
    assert w.prompts == []


def test_FL9_shutdown_unmutes_spotify():
    w = make_world(album(2))
    w.run(10)
    assert w.spotify.muted
    w.engine.shutdown()
    assert not w.spotify.muted
    assert w.vlc.state == "idle"


def test_FL10_mark_not_on_tidal_from_the_ui():
    w = make_world(album(2))
    w.run(10)
    w.engine.mark_not_on_tidal()
    w.run(1)
    assert w.engine.state == State.FALLBACK and not w.spotify.muted
    assert "Song 0" in w.library.not_on_tidal


def test_FL11_manual_match_replaces_the_track():
    w = make_world(album(2), missing={"Song 0"})
    w.run(10)
    assert w.engine.state == State.FALLBACK
    w.engine.manual_map(w.library.catalogue["Song 1"])
    w.run(3)
    assert w.engine.state == State.PLAYING and w.spotify.muted
    assert w.library.mappings
