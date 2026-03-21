# Spotify to Tidal Sync Player

A lightweight Windows/Linux desktop application that detects what you are listening to on any media player, finds the highest quality match on Tidal (HiFi/Master), and plays it seamlessly using an integrated VLC-based player.

By default the app uses **OS-level media detection** (Windows SMTC / Linux MPRIS) so it works with Spotify, Apple Music, YouTube, and any other media player. Optionally, you can enable the **Spotify API** in Settings for extra features like queue prefetch and seek sync.

This tool is designed for users who prefer the music discovery and UI of their favorite player but demand the lossless audio quality of Tidal.

## Features

* **OS Media Detection:** Automatically detects the currently playing track via Windows SMTC or Linux MPRIS. Works with any media player, not just Spotify.

* **Local Playback Control:** Track advancement is based on local playback finishing, not the source app's internal timing. No more premature skipping when track lengths differ slightly.

* **Position Sync:** Automatically syncs playback position with your source app. If you start a song mid-way, seek to a different position, or restart the track, VLC follows. Works via Spotify API or OS-level position detection (MPRIS/SMTC).

* **Optional Spotify API:** Enable in Settings > Spotify API for queue prefetch (pre-loads next 4-5 tracks), precise seek sync, and duration-based matching. Disabled by default.

* **Smart Quality Fallback:** Tries to play in **HiRes/Max** quality first. If a track fails (e.g., region lock or API restriction), it automatically retries with Lossless or High quality to ensure music keeps playing. The current playback quality is displayed in the UI.

* **Single Instance:** Only one instance of the app can run at a time. Attempting to launch a second instance shows an error and exits.

* **Remote Playback Ignored:** If your source app (e.g., Spotify) is playing on another device (phone, web player), the app won't start local playback. It only reacts to local playback activity.

* **Seek Slider & Skip Controls:** Drag the seek bar to jump anywhere in the track, or use the **-10s / +10s** buttons to skip forward and backward.

* **Manual Match Correction:** If the app picks the wrong song or a cover, click the **"Report Wrong Song / Fix Match"** button to manually search Tidal and map the correct track. This preference is saved permanently.

* **Community Corrections:** Track corrections can optionally be shared with all users. On each startup the app syncs the latest community corrections from GitHub, and your own fixes can be submitted automatically to help others. You are prompted on first launch and can toggle this anytime in Settings.

* **Playback Control:** Pause, Play, Next, Previous, and seek controls that sync seamlessly. Uses Spotify API when enabled, otherwise controls via OS media transport.

* **Smart Muting:** Can automatically mute the source app. On Windows, uses per-app audio muting (pycaw) for reliable silence. On Linux, uses pactl for per-stream muting.

* **Audio Isolation:** For complete audio isolation, the app guides you through setting up VB-Cable (free virtual audio cable) so your source app outputs to a silent virtual device. See Settings > Audio Isolation.

* **Auto-Favorite:** Optional setting to automatically add songs to your Tidal favorites if you listen to 90% of the track.

* **Selectable Audio Output:** Choose your specific output device (DAC, Headphones, Speakers) within the app settings. Device list is scrollable with a refresh button, and falls back to pactl device listing on Linux if VLC enumeration fails.

* **Secure Session Caching:** You only need to log in to Tidal once. Your tokens are securely stored in the **Windows Credential Manager** (or Linux Keyring) using `keyring`. They are encrypted and tied to your OS login.

* **Browser Fallback:** If the app cannot open your browser during login, the URL is displayed in a dialog and copied to your clipboard so you can open it manually.

## Requirements

Before running or building the application, ensure you have the following:

### 1. Software

* **VLC Media Player (64-bit):**

  * **Critical:** You must have the **64-bit** version of VLC installed. The app uses `libvlc` for audio decoding.

  * [Download VLC Here](https://www.videolan.org/vlc/)

* **Windows 10 or 11 / Linux** (Fedora, Ubuntu, Arch, etc.)

* **Python 3.10+** (only for building or running from source — the build script will install it for you if missing)

### 2. Service Accounts

* **Tidal HiFi or HiFi Plus:** Required to access the lossless audio streams via the API.

* **Spotify Premium** (optional): Only needed if you enable the Spotify API in Settings for queue prefetch and seek sync.

## Setup Guide

### 1. Building

No configuration files are needed before building. The build script handles everything.

1. Open the project folder.

2. Double-click `build_windows.bat`.

   * If Python is not installed, the script will offer to install it automatically. Type `y` to confirm.
   * If Python is installed but not in your PATH, the script will find it automatically in common install locations.
   * If pip is broken, the script will repair it using `ensurepip` or `get-pip.py`.

3. The script will install dependencies, compile the application, and sign the executable.

### 2. (Optional) Enable Spotify API

If you want queue prefetch and seek sync, you can enable the Spotify API after the app is running:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).

2. Log in and click **"Create App"**.

3. Name it (e.g., "TidalSync") and give it a description.

4. In the **Redirect URI** field, enter exactly:
   `http://127.0.0.1:8888/callback`

5. Save the app.

6. Copy the **Client ID** and **Client Secret**.

7. In the app, go to **Settings > Spotify API**, check **"Enable Spotify API"**, enter your credentials, click **Save**, and restart.

## Installation & Running

### Option A: Running the Executable (.exe)

1. **Build** the app using the steps above.

2. The finished `SpotifySync.exe` will appear in the project folder.

3. Double-click `SpotifySync.exe`.

4. **Authorization:**

   * A browser tab will open for **Tidal Login**. Log in to your Tidal account.

   * If your browser cannot be opened automatically, the URL will be shown in a dialog and copied to your clipboard.

5. The app will open and begin waiting for media activity. Play something in Spotify (or any media player) and it will be detected automatically.

### Option B: Running from Source (Python)

If you are a developer or want to run the raw script:

1. **Install Python 3.10+**.

2. **Install Dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

   On Windows, pycaw and winrt packages are installed automatically. On Linux, you may need `dbus-python` via your system package manager.

   If you want Spotify API support, also install spotipy:

   ```bash
   pip install spotipy
   ```

3. **Run the script:**

   ```bash
   python spotify.py
   ```

## Usage Tips

* **Audio Output:** Go to **Settings > General** to select your specific audio device (e.g., "External DAC"). The device list scrolls vertically if you have many devices.

* **Wrong Song?** If the sync plays the wrong track, click the **"Report Wrong Song"** button on the main screen. Search for the correct track on Tidal, select it, and the app will remember this mapping forever. If you opted in, the correction is also shared with the community so everyone benefits.

* **Community Corrections:** On first launch you'll be asked if you want to share corrections. You can change this anytime in **Settings > General > "Share track corrections with community"**. The app automatically downloads new community corrections from GitHub on each startup.

* **Muting:** In Settings, enable **"Mute Source App (Spotify/etc)"**. On Windows with pycaw installed, this mutes the source app at the OS audio mixer level.

* **Full Audio Isolation:** For complete silence from your source app, go to **Settings > Audio Isolation** for instructions on setting up VB-Cable (free). This routes the source app's audio to a virtual device that produces no sound.

* **Seeking:** Use the seek slider to jump to any position in the track, or use the **-10s / +10s** buttons for quick skips.

* **Spotify API:** To enable queue prefetch and seek sync, go to **Settings > Spotify API**, check the toggle, enter your credentials from the Spotify Developer Dashboard, and restart the app.

* **Resetting:** If you need to switch accounts or fix a login loop, go to **Settings > Factory Reset (Red Button)**. This wipes the settings file and securely deletes your tokens from the Windows Credential Manager.

## Code Signing

The build script signs the executable with a self-signed certificate. This reduces some antivirus false positives but **will not** bypass Windows SmartScreen or corporate security policies. For full trust on managed devices, an EV code signing certificate from a Certificate Authority (e.g., DigiCert, Sectigo) is required.

## Troubleshooting

* **App crashes immediately:** Usually missing VLC 64-bit. Ensure it is installed.

* **No media detected:** Make sure you are playing music in a media player. On Windows, the app uses SMTC (System Media Transport Controls) — most modern players support this. Check that the `winrt-Windows.Media.Control` package is installed.

* **401 Unauthorized Errors:** The app handles this automatically by lowering the quality for that specific song (e.g., from Max to High) until it plays.

* **Tidal Login fails:** Ensure you have an active subscription. Free accounts do not support API streaming.

* **Browser won't open during login:** The app will show the login URL in a dialog and copy it to your clipboard. Open it manually in any browser.

* **Premature track skipping:** The app waits for local VLC playback to finish before advancing. If the source app auto-advances while VLC is still playing, the source is paused until VLC catches up. A skip cooldown prevents double-skipping.

* **Album art not showing (Linux):** If you see a `PIL._tkinter_finder` error, make sure `python3-pillow-tk` (or equivalent) is installed. The app has a fallback that saves art to a temp file, but the PIL/tkinter bridge works best when properly installed.

* **Wrong window icon (Linux):** The app sets `WM_CLASS` to `spotifysync` to match the `.desktop` file. If the icon still doesn't show, run `gtk-update-icon-cache ~/.local/share/icons/hicolor/` or log out and back in.

* **Audio devices not loading:** Click the **Refresh** button in Settings > General to retry device enumeration. On Linux, the app falls back to `pactl` if VLC's device listing fails.

* **App reacts when listening on another device:** The app only starts playback when the source is actively playing locally. If Spotify reports a track but is paused (playing on phone/web), the app waits.

* **Spotify API not connecting:** Make sure you've checked "Enable Spotify API" in Settings, entered valid credentials, and restarted the app. The redirect URI must be exactly `http://127.0.0.1:8888/callback` in your Spotify app settings.
