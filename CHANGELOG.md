# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
