# Spotify to Tidal Sync Player

Use Spotify for discovery, queues and the AI DJ, and hear Tidal's lossless and
hi-res streams instead. Spotify keeps playing **muted** as the queue master;
the app finds each track on Tidal and plays it through VLC, following Spotify's
pauses, seeks and skips.

## Platform support

| | Linux | Windows 10/11 | macOS |
|---|---|---|---|
| Detect Spotify | MPRIS (D-Bus) | SMTC | not yet |
| Control and seek Spotify | MPRIS, or Spotify API | SMTC, or Spotify API | not yet |
| Mute Spotify only | PulseAudio/PipeWire per-stream | per-app mixer (pycaw) | not yet |
| Status | supported | supported | shows "not supported yet" and exits |

## Features

* **Gap-free boundaries.** Our player is the clock: when the Tidal master ends
  first, muted Spotify is skipped forward; when Spotify crossfades early, our
  track still plays to its real end. With the Spotify API, the next track is
  pre-buffered in a standby player for a gapless switch.
* **No jumping at the start.** Mid-song starts open the stream at the right
  position instead of seeking after playback has begun, and seeks are only
  followed when two readings confirm them.
* **Smart matching.** ISRC first (with the API), then title + artist, title +
  album and title alone. Scoring uses artist (any credited artist), title,
  version (live, remix, instrumental...), album and runtime. It never plays a
  different song by the same artist, and handles study/lo-fi music with generic
  titles, instrumental-only releases, classical movements, collaborations and
  non-Latin titles.
* **Fails open.** If a track isn't on Tidal or its stream fails, Spotify is
  unmuted so you keep hearing music. A dropped stream (after a long pause or
  suspend) reconnects at the same position first.
* **Fix a match** by hand, or mark a track **Not on Tidal** so it plays from
  Spotify from then on without asking. Corrections are saved permanently.
* **Podcasts, ads and AI DJ commentary** are handed back to Spotify.
* **Playback on another device** (phone, speaker) is left alone.
* **Hi-res when available.** One track without a hi-res master no longer
  downgrades the rest of the session.
* **App volume slider**, seek bar, ±10s, previous/next, keyboard shortcuts
  (space, ←/→, Ctrl+←/→).
* **Safe muting.** A crash or kill never leaves Spotify muted: the next start
  restores it.
* Tokens stored in the OS keyring (Windows Credential Manager, Secret Service).

## Requirements

* **VLC media player**, desktop version, same architecture as Python (64-bit).
  [Download](https://www.videolan.org/vlc/)
* **Tidal HiFi** (or HiFi Plus for hi-res).
* **Python 3.10+** when running from source.
* Optional: **Spotify Premium** and a Spotify developer app for the Web API
  (queue prefetch, gapless switches, ISRC matching, AI DJ, device detection).

## Running

### Windows

Double-click `build_windows.bat`. It installs Python if needed, the
dependencies, and builds `SpotifySync.exe`.

### Linux

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # dbus-python needs dbus-devel / libdbus-1-dev
scripts/install_desktop.sh                    # app menu entry, runs from source
# or: scripts/build_linux.sh                  # standalone binary
```

On first start a browser opens for the Tidal login.

### Optional: Spotify Web API

1. Create an app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).
2. Redirect URI: `http://127.0.0.1:8888/callback`
3. In the app: **Settings > Spotify API**, enable it, paste the Client ID and
   Secret, save, restart.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests               # unit, scenario simulation, UI smoke
.venv/bin/python -m pytest tests/integration   # real libvlc (silent dummy output)
```

Every scenario the app handles, with what verifies it and a manual release
checklist, is in [docs/SCENARIOS.md](docs/SCENARIOS.md). CI runs the suite on
Linux, Windows and macOS.

## Troubleshooting

* **"Could not start" with a VLC hint:** install the desktop VLC matching your
  Python's architecture.
* **You hear Spotify instead of Tidal:** the track has no confident Tidal match
  or its stream failed; the status line says which. Use **Fix match**.
* **Spotify isn't muted (Linux):** its audio stream only appears once it plays;
  check Settings > Logs for "Could not find a Spotify audio stream".
* **Spotify stays muted after a crash:** start the app once; it restores the
  mute it left behind. Otherwise unmute Spotify in the volume mixer.
* **Wrong track picked:** **Fix match**, search, double-click the right one.
* **Login loop:** Settings > Wipe all data and logins.
* **Logs:** `debug.log` in the data folder (`%APPDATA%\SpotifyTidalSync` or
  `~/.config/SpotifyTidalSync`); the previous run is kept as `debug.log.1`.
