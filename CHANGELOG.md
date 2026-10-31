# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [v0.05] - 2026-10-31

### Added
- **Scenario test suite**: the real sync engine runs against a simulated Spotify, VLC and Tidal on a fake clock, asserting on what is audible (gaps, double audio, seeks). Plus unit tests, a UI smoke test and real-libvlc integration tests. See `docs/SCENARIOS.md`
- **CI** on Linux, Windows and macOS (Python 3.10/3.12/3.13), with VLC installed for the integration tests
- **Gapless switches**: the next queue track is pre-buffered in a standby player
- **ISRC matching** and progressive search (title + artist, title + album, title)
- **Auto-match cache**: confident matches are remembered, replays skip the search
- **"Not on Tidal"** button: the track plays from Spotify from then on, no popup
- **Volume slider** in the app; match method and confidence shown under the quality
- **Stream recovery**: a dropped stream reconnects at the same position
- **Podcast and ad pass-through**, and playback on another device is left alone
- **Crash-safe muting**: a marker file lets the next start unmute Spotify
- MPRIS seek (`SetPosition`) and SMTC seek without the Spotify API
- New logo: the interlaced note, now white on a gradient disc so it shows on light and dark taskbars

### Changed
- When the Tidal master ends before Spotify's, Spotify is skipped forward instead of waited for (the 1-6s gaps)
- Mid-song starts open the stream at the position instead of seeking after playback began
- Seeks are only followed when confirmed by a second reading and our player is not already there
- No match or a failed stream unmutes Spotify instead of leaving silence; the popup appears once per track
- UI actions go through a command queue run by the sync loop
- Matching: title must actually match (numbers and words), all credited artists count, album and runtime weigh in, generic study titles need a close runtime, instrumental-only releases accepted when everything else agrees
- Linux: Spotify is chosen over other players, controls target the same player that is read, Spotify ids and art read from MPRIS
- Windows: controls target Spotify's session (not the "current" one), SMTC positions extrapolated, DPI aware
- Tidal login retries while offline and refreshes expired tokens before asking for a new login
- Log is UTF-8 and the previous run is kept as `debug.log.1`
- macOS shows a clear "not supported yet" message

### Fixed
- 1-6s silence at track boundaries when the Tidal master was shorter
- Jumps and stutters shortly after a track started
- One track without hi-res downgrading every later track to lossless
- A different song by the same artist being played when the right one was missing
- All Japanese/Korean/Cyrillic/Arabic titles sharing one mapping key
- Playing every track in between after a burst of skips
- Silence for the rest of a song after a long pause or suspend
- Duplicate tracks and repeat-one leaving silence
- Local files not being detected
- Phone playback being mirrored on the PC and paused by the app
- Spotify left muted after a crash, kill or factory reset
- Our own VLC stream (and the user's VLC) being forced to 100% volume
- Second instance truncating the running instance's lock file

### Removed
- Community corrections and the VB-Cable tab (dropped in the package refactor; README updated)

## [v0.04] - 2026-03-21

### Added
- **Position sync**: VLC now syncs to the source app's playback position. Starting a song mid-way, seeking forward/backward, or restarting the track in Spotify are all followed by VLC automatically
- **Source position reading**: Reads playback position from MPRIS (Linux) and SMTC (Windows) so position sync works even without the Spotify API
- **Ongoing seek detection**: Every sync cycle checks if the source position jumped (>5s difference) and follows forward seeks, backward seeks, and song restarts
- **Song restart detection**: If the source is near position 0 but VLC is far ahead, VLC restarts the song (handles "Previous" on same track)
- **Quality indicator in UI**: A green label below the source indicator shows the current playback quality (e.g., "Quality: Hi Res Lossless")
- **Quality mismatch logging**: Logs when the delivered quality differs from the requested quality (e.g., got LOSSLESS instead of HI_RES_LOSSLESS)
- **Single-instance lock**: Only one instance of the app can run at a time. Uses `fcntl.flock` on Linux and `msvcrt.locking` on Windows. A second launch shows an error dialog and exits
- **Remote playback filtering**: Ignores source app activity when it is not playing locally (e.g., Spotify playing on phone/web). Only reacts to local playback
- **Skip cooldown**: 2-second cooldown after track changes prevents double-skipping when source and VLC advance simultaneously
- **Audio device refresh button**: Refresh button in Settings > General to manually reload the device list
- **pactl device fallback**: On Linux, falls back to `pactl list short sinks` if VLC device enumeration returns nothing
- **Saved device selection**: Audio device list highlights the previously saved device on load

### Changed
- **No more Spotify position reset**: `attempt_play_tidal` no longer forces Spotify to seek to position 0. Instead reads the source's current position and starts VLC there
- **PIL imported before tkinter**: Moved `from PIL import Image, ImageTk` before tkinter imports to avoid `_tkinter_finder` module errors on Linux
- **Album art fallback**: If `ImageTk.PhotoImage` fails, falls back to saving to a temp PNG and loading via native `tk.PhotoImage`
- **Icon fallback**: Window icon loading has the same temp-file fallback if PIL/tkinter bridge fails
- **Linux WM_CLASS**: Main window created with `className="spotifysync"` so the window manager matches the `.desktop` file icon
- **Device list UI redesigned**: Dark-themed `ttk.Scrollbar`, Spotify-green selection highlight, subtle border, and refresh button in the header row
- **App version bumped to 0.04**

### Fixed
- Album art not displaying on Linux due to `No module named 'PIL._tkinter_finder'` error
- Linux window icon showing as generic default instead of the app logo
- Multiple instances of the app could run simultaneously, causing audio conflicts
- App starting playback when user is listening on another device (phone, web player)
- Double-skipping when source and VLC tracks end at roughly the same time
- VLC not matching source position when starting a song mid-way through
- Source seeks (forward, backward, restart) not being followed by VLC
- Audio device list not loading on some Linux systems where VLC enumeration fails
- Audio device list not selecting the previously saved device
- Device list scrollbar and refresh button using outdated default styling

## [v0.03] - 2026-03-02

### Added
- **OS media detection**: Detects currently playing track via Windows SMTC or Linux MPRIS instead of polling the Spotify API. Works with any media player (Spotify, Apple Music, YouTube, etc.)
- **Spotify API is now optional**: Credentials can be entered in Settings > Spotify API. When enabled, adds queue prefetch and precise seek sync. Disabled by default.
- **Settings toggle for Spotify API**: Enable/disable checkbox in Settings > Spotify API with credential fields that show/hide accordingly
- **Normalized track matching**: Track mappings now use normalized `title|artist` keys instead of Spotify IDs, making mappings source-agnostic
- **Album art via iTunes API**: Fetches album art from the free iTunes Search API when OS detection doesn't provide art URLs
- **Source indicator in UI**: Shows which app is being detected (e.g., "Source: Spotify | OS detection")
- **OS media controls fallback**: Play/pause/next/previous controls work via OS media transport when Spotify API is unavailable
- **winrt-based SMTC detection**: Uses pre-built `winrt-Windows.Media.Control` package (no C++ compiler required)
- **MPRIS support on Linux**: Detects media via D-Bus MPRIS interface for Linux compatibility
- **Dark title bar**: Main window and all settings/dialogs use the Windows dark title bar (DWM API) to match the dark UI theme

### Changed
- **No `.env` file required**: Removed all `.env` loading, `python-dotenv` dependency, and `extract_bundled_files()`. Spotify credentials are stored in the OS keyring via the Settings UI
- **`spotipy` is no longer a required dependency**: Only needed when Spotify API is enabled; users install it manually if wanted
- **Build scripts simplified**: Removed Step 3 (.env check/creation) from `build.ps1` and `--add-data ".env:."` from both build scripts
- **Audio device selector**: Replaced fixed-width Combobox with a scrollable Listbox that adapts to window size (no more horizontal overflow)
- **Mute fallback removed**: `_mute_spotify()` no longer falls back to Spotify API `volume(0)`, uses pycaw only
- **Prefetch cache rekeyed**: Uses normalized mapping keys instead of Spotify track IDs
- **App version bumped to 0.03**

### Fixed
- Audio device list overflowing the Settings window when device names are long
- Text in Audio Isolation and Spotify API settings tabs overflowing outside the window — all labels now use `wraplength` to wrap within the window bounds
- Backward compatibility with old Spotify-ID-based mappings in `mappings.json` (looked up as fallback)

## [v0.02] - 2026-02-08

### Added
- **Prefetch cache**: Pre-fetches the next 4-5 songs from the Spotify queue in the background with rate-limiting delays to avoid API flags
- **Seek slider**: Drag to jump to any point in the current track
- **10-second skip buttons**: Skip forward or backward 10 seconds
- **Audio isolation tab in Settings**: Detects virtual audio devices, provides VB-Cable download link and setup instructions for full Spotify audio isolation
- **pycaw integration**: Mutes Spotify at the Windows audio mixer level (per-app) instead of relying on API volume control
- **Browser fallback**: If the browser cannot be opened automatically during login, the URL is shown in a dialog and copied to the clipboard
- **Self-signed code signing**: Build script signs the exe to reduce antivirus false positives
- **Auto-install Python**: Build script detects Python (python/python3/py), installs it automatically via winget or direct download if missing (with user confirmation)
- **pip auto-repair**: Build script fixes broken pip installations using ensurepip or get-pip.py
- **PATH detection**: Build script scans common Python install directories if Python is installed but not in PATH
- **Community corrections**: Track corrections are optionally shared with a community database so all users benefit from fixes
- **Community sync on startup**: On each launch, the app fetches the latest community corrections from GitHub and merges new entries into local mappings (no duplicates)
- **First-run opt-in prompt**: Users are asked on first launch whether they want to share their corrections with the community
- **Share toggle in Settings**: Community correction sharing can be toggled on/off in Settings > General
- **Build script mappings sync**: Build script automatically merges community mappings.json from the repo into appdata and removes it from the project folder
- **Custom app icon**: Logo is converted to .ico during build and embedded in the executable
- **Clean project layout**: Build scripts moved to `scripts/`, logo to `assets/`, build artifacts to `_build/` — root only contains files users need

### Changed
- Track advancement is now based on local (VLC) playback finishing, not Spotify's internal timing
- Spotify is paused when it auto-advances ahead of VLC, preventing premature track skipping
- User skips from within the Spotify app are detected (heuristic: VLC has >15s remaining) and honored immediately
- User skips from app buttons set a flag for instant response
- Build script uses `python -m pip` and `python -m PyInstaller` instead of bare commands for better PATH reliability
- Window height increased to accommodate new controls
- Project folder reorganized: `scripts/` (build scripts), `assets/` (logo), `_build/` (artifacts) — users just see the .bat, README, and source

### Fixed
- Premature track skipping when Spotify and Tidal track lengths differ slightly
- Auto-favorite now tries both `tidal.user.favorites.add_track()` and legacy `tidal.add_favorite()` for tidalapi compatibility

## [v0.01] - 2025-01-01

Initial release.
