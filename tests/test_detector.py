"""OS detection helpers (DT*): pure functions, so they run on every OS."""

import datetime

from spotifysync.sources.detector import (extrapolate_position, identify_app, parse_mpris,
                                          parse_spotify_trackid, pick_service)

# Captured from a live Spotify (Flatpak) MPRIS session.
SPOTIFY_META = {
    "mpris:trackid": "/com/spotify/track/1BV0m40U0M4t1SLIsDnwZl",
    "mpris:length": 280026000,
    "mpris:artUrl": "https://i.scdn.co/image/ab67616d0000b27363a70775eddaa46c50a8c703",
    "xesam:album": "All the Lost Souls",
    "xesam:artist": ["James Blunt"],
    "xesam:title": "1973",
}


def test_DT1_spotify_wins_over_a_playing_browser_listed_first():
    entries = [("org.mpris.MediaPlayer2.firefox.instance_1_42", "firefox", "Playing"),
               ("org.mpris.MediaPlayer2.spotify", "spotify", "Paused")]
    assert pick_service(entries, "spotify") == "org.mpris.MediaPlayer2.spotify"


def test_DT2_without_spotify_a_playing_player_wins_over_the_first():
    entries = [("org.mpris.MediaPlayer2.rhythmbox", "rhythmbox", "Paused"),
               ("org.mpris.MediaPlayer2.chromium.instance9", "chromium", "Playing")]
    assert pick_service(entries, "spotify") == "org.mpris.MediaPlayer2.chromium.instance9"


def test_DT3_our_own_vlc_and_tidal_are_ignored():
    entries = [("org.mpris.MediaPlayer2.vlc", "vlc", "Playing"),
               ("org.mpris.MediaPlayer2.tidal", "tidal", "Playing")]
    assert pick_service(entries, "spotify") is None


def test_DT4_parse_live_spotify_metadata():
    media = parse_mpris(SPOTIFY_META, "Playing", 202027000, "spotify")
    assert media.title == "1973" and media.artist == "James Blunt"
    assert media.spotify_id == "1BV0m40U0M4t1SLIsDnwZl"
    assert media.duration_ms == 280026
    assert media.position_ms == 202027
    assert media.art_url.startswith("https://i.scdn.co/")
    assert media.kind == "track" and media.is_playing


def test_DT5_spotify_ads_and_episodes_are_classified():
    ad = parse_mpris({"xesam:title": "Advertisement", "xesam:artist": [],
                      "mpris:trackid": "/com/spotify/ad/abc"}, "Playing", 0, "spotify")
    assert ad.kind == "ad"
    ep = parse_mpris({"xesam:title": "Episode 12", "xesam:artist": ["Show"],
                      "mpris:trackid": "/com/spotify/episode/xyz"}, "Playing", 0, "spotify")
    assert ep.kind == "episode"


def test_DT6_trackid_formats():
    assert parse_spotify_trackid("/com/spotify/track/ABC") == ("track", "ABC")
    assert parse_spotify_trackid("spotify:track:ABC") == ("track", "ABC")
    assert parse_spotify_trackid("spotify:episode:XYZ") == ("episode", "XYZ")
    assert parse_spotify_trackid("") == ("track", "")
    assert parse_spotify_trackid("/org/mpris/MediaPlayer2/TrackList/NoTrack") == ("track", "")


def test_DT7_missing_position_and_title():
    assert parse_mpris({"xesam:title": ""}, "Playing", 0, "spotify") is None
    media = parse_mpris({"xesam:title": "X"}, "Paused", None, "spotify")
    assert media.position_ms == -1 and media.status == "paused"


def test_DT8_smtc_position_is_extrapolated_while_playing():
    t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    later = t0 + datetime.timedelta(seconds=12.5)
    assert extrapolate_position(30000, t0, later, True) == 42500
    assert extrapolate_position(30000, t0, later, False) == 30000
    assert extrapolate_position(30000, t0, later, True, duration_ms=35000) == 35000
    assert extrapolate_position(-1, t0, later, True) == -1
    assert extrapolate_position(30000, None, later, True) == 30000


def test_DT9_windows_app_ids():
    assert identify_app("Spotify.exe") == "Spotify"
    assert identify_app("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "Spotify"
    assert identify_app("TIDAL.exe") == "Tidal"
    assert identify_app("") == "unknown"
