# Spotify to Tidal Sync Player

A lightweight Windows desktop application that syncs your Spotify playback to Tidal in real-time. It detects what you are listening to on Spotify, finds the highest quality match on Tidal (HiFi/Master), and plays it seamlessly using an integrated VLC-based player.

This tool is designed for users who prefer the music discovery and UI of Spotify but demand the lossless audio quality of Tidal.

## Features

* **Real-time Sync:** Automatically detects track changes on Spotify and plays the corresponding track on Tidal.

* **Local Playback Control:** Track advancement is based on local playback finishing, not Spotify's internal timing. No more premature skipping when track lengths differ slightly.

* **Prefetch Cache:** Pre-fetches the next 4-5 songs from your Spotify queue in the background with rate-limiting, so track switches are near-instant.

* **Smart Quality Fallback:** Tries to play in **HiRes/Max** quality first. If a track fails (e.g., region lock or API restriction), it automatically retries with Lossless or High quality to ensure music keeps playing.

* **Seek Slider & Skip Controls:** Drag the seek bar to jump anywhere in the track, or use the **-10s / +10s** buttons to skip forward and backward.

* **Manual Match Correction:** If the app picks the wrong song or a cover, click the **"Report Wrong Song / Fix Match"** button to manually search Tidal and map the correct track. This preference is saved permanently.

* **Community Corrections:** Track corrections can optionally be shared with all users. On each startup the app syncs the latest community corrections from GitHub, and your own fixes can be submitted automatically to help others. You are prompted on first launch and can toggle this anytime in Settings.

* **Playback Control:** Pause, Play, Next, Previous, and seek controls that sync seamlessly.

* **Smart Muting:** Can automatically mute the Spotify desktop app. On Windows, uses per-app audio muting (pycaw) for reliable silence. Falls back to API volume control if needed.

* **Audio Isolation:** For complete Spotify audio isolation, the app guides you through setting up VB-Cable (free virtual audio cable) so Spotify outputs to a silent virtual device. See Settings > Audio Isolation.

* **Auto-Favorite:** Optional setting to automatically add songs to your Tidal favorites if you listen to 90% of the track.

* **Selectable Audio Output:** Choose your specific output device (DAC, Headphones, Speakers) within the app settings.

* **Secure Session Caching:** You only need to log in to Tidal once. Your tokens are securely stored in the **Windows Credential Manager** (or Linux Keyring) using `keyring`. They are encrypted and tied to your OS login.

* **Browser Fallback:** If the app cannot open your browser during login, the URL is displayed in a dialog and copied to your clipboard so you can open it manually.

## Requirements

Before running or building the application, ensure you have the following:

### 1. Software

* **VLC Media Player (64-bit):**

  * **Critical:** You must have the **64-bit** version of VLC installed. The app uses `libvlc` for audio decoding.

  * [Download VLC Here](https://www.videolan.org/vlc/)

* **Windows 10 or 11** (Linux supported via source)

* **Python 3.10+** (only for building or running from source — the build script will install it for you if missing)

### 2. Service Accounts

* **Spotify Premium:** Required for full API playback control (Pause/Seek/Volume) and status syncing.

* **Tidal HiFi or HiFi Plus:** Required to access the lossless audio streams via the API.

## Setup Guide

### 1. Get Spotify API Credentials

To allow the app to see what you are playing, you need a Client ID and Secret from Spotify.

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).

2. Log in and click **"Create App"**.

3. Name it (e.g., "TidalSync") and give it a description.

4. In the **Redirect URI** field, enter exactly:
   `http://127.0.0.1:8888/callback`

5. Save the app.

6. Copy the **Client ID** and **Client Secret** for the next step.

### 2. Building & Configuration

You do **not** need to create configuration files manually. The build script handles this for you.

1. Open the project folder.

2. Double-click `build_windows.bat`.

3. **First Run:** The script will detect that `.env` is missing. It will generate a template file and pause.

   * If Python is not installed, the script will offer to install it automatically. Type `y` to confirm.
   * If Python is installed but not in your PATH, the script will find it automatically in common install locations.
   * If pip is broken, the script will repair it using `ensurepip` or `get-pip.py`.

4. **Edit Configuration:** Open the newly created `.env` file in Notepad. Paste your **Client ID** and **Client Secret** from Step 1. Save and close.

5. **Second Run:** Double-click `build_windows.bat` again. It will now detect the configuration, install dependencies, compile the application, and sign the executable.

## Installation & Running

### Option A: Running the Executable (.exe)

1. **Build** the app using the steps above.

2. The finished `SpotifySync.exe` will appear in the project folder.

3. Double-click `SpotifySync.exe`.

   * *Note: The first time you run the .exe, it extracts your configuration to `%APPDATA%\SpotifyTidalSync`. You can move the .exe anywhere after that.*

4. **Authorization:**

   * A browser tab will open for **Spotify Login**. Click "Agree".

   * A browser tab will open for **Tidal Login**. Log in to your Tidal account.

   * If your browser cannot be opened automatically, the URL will be shown in a dialog and copied to your clipboard.

5. The app will open and begin waiting for Spotify activity.

### Option B: Running from Source (Python)

If you are a developer or want to run the raw script:

1. **Install Python 3.10+**.

2. **Install Dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

   On Windows, also install `pycaw` for per-app audio muting:

   ```bash
   pip install pycaw
   ```

3. **Run the script:**

   ```bash
   python spotify.py
   ```

## Usage Tips

* **Audio Output:** Go to **Settings > General** to select your specific audio device (e.g., "External DAC").

* **Wrong Song?** If the sync plays the wrong track, click the **"Report Wrong Song"** button on the main screen. Search for the correct track on Tidal, select it, and the app will remember this mapping forever. If you opted in, the correction is also shared with the community so everyone benefits.

* **Community Corrections:** On first launch you'll be asked if you want to share corrections. You can change this anytime in **Settings > General > "Share track corrections with community"**. The app automatically downloads new community corrections from GitHub on each startup.

* **Muting Spotify:** In Settings, enable **"Mute Spotify Desktop App"**. On Windows with pycaw installed, this mutes Spotify at the OS audio mixer level. Otherwise it sets Spotify's API volume to 0.

* **Full Audio Isolation:** For complete silence from Spotify, go to **Settings > Audio Isolation** for instructions on setting up VB-Cable (free). This routes Spotify's audio to a virtual device that produces no sound.

* **Seeking:** Use the seek slider to jump to any position in the track, or use the **-10s / +10s** buttons for quick skips.

* **Resetting:** If you need to switch accounts or fix a login loop, go to **Settings > Factory Reset (Red Button)**. This wipes the settings file and securely deletes your tokens from the Windows Credential Manager.

## Code Signing

The build script signs the executable with a self-signed certificate. This reduces some antivirus false positives but **will not** bypass Windows SmartScreen or corporate security policies. For full trust on managed devices, an EV code signing certificate from a Certificate Authority (e.g., DigiCert, Sectigo) is required.

## Troubleshooting

* **App crashes immediately:** Usually missing VLC 64-bit. Ensure it is installed.

* **401 Unauthorized Errors:** The app handles this automatically by lowering the quality for that specific song (e.g., from Max to High) until it plays.

* **Tidal Login fails:** Ensure you have an active subscription. Free accounts do not support API streaming.

* **Browser won't open during login:** The app will show the login URL in a dialog and copy it to your clipboard. Open it manually in any browser.

* **Premature track skipping:** This should be resolved in v0.02. The app now waits for local VLC playback to finish before advancing. If Spotify auto-advances while VLC is still playing, Spotify is paused until VLC catches up.
