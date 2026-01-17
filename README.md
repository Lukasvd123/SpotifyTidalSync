# Spotify to Tidal Sync Player

A lightweight Windows desktop application that syncs your Spotify playback to Tidal in real-time. It detects what you are listening to on Spotify, finds the highest quality match on Tidal (HiFi/Master), and plays it seamlessly using an integrated VLC-based player.

This tool is designed for users who prefer the music discovery and UI of Spotify but demand the lossless audio quality of Tidal.

## Features

* **Real-time Sync:** Automatically detects track changes on Spotify and plays the corresponding track on Tidal.

* **Smart Quality Fallback:** Tries to play in **HiRes/Max** quality first. If a track fails (e.g., region lock or API restriction), it automatically retries with Lossless or High quality to ensure music keeps playing.

* **Manual Match Correction:** If the app picks the wrong song or a cover, click the **"Report Wrong Song / Fix Match"** button to manually search Tidal and map the correct track. This preference is saved permanently.

* **Playback Control:** Pause, Play, Next, and Previous controls that sync seamlessly.

* **Smart Muting:** Can automatically mute the Spotify desktop app so you only hear the high-quality Tidal stream.

* **Auto-Favorite:** Optional setting to automatically add songs to your Tidal favorites if you listen to 90% of the track.

* **Selectable Audio Output:** Choose your specific output device (DAC, Headphones, Speakers) within the app settings.

* **Secure Session Caching:** * You only need to log in to Tidal once.
  * Your tokens are securely stored in the **Windows Credential Manager** (or Linux Keyring) using `keyring`. They are encrypted and tied to your OS login.

## Requirements

Before running or building the application, ensure you have the following:

### 1. Software

* **VLC Media Player (64-bit):**

  * **Critical:** You must have the **64-bit** version of VLC installed. The app uses `libvlc` for audio decoding.

  * [Download VLC Here](https://www.videolan.org/vlc/)

* **Windows 10 or 11** (Linux supported via source)

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

2. Double-click `build_exe.bat`.

3. **First Run:** The script will detect that `.env` is missing. It will generate a template file and pause.

4. **Edit Configuration:** Open the newly created `.env` file in Notepad. Paste your **Client ID** and **Client Secret** from Step 1. Save and close.

5. **Second Run:** Double-click `build_exe.bat` again. It will now detect the configuration and compile the application.

## Installation & Running

### Option A: Running the Executable (.exe)

1. **Build** the app using the steps above.

2. The finished `SpotifySync.exe` will appear in the `dist/` folder.

3. Double-click `SpotifySync.exe`.

   * *Note: The first time you run the .exe, it extracts your configuration to `%APPDATA%\\SpotifyTidalSync`. You can move the .exe anywhere after that.*

4. **Authorization:**

   * A browser tab will open for **Spotify Login**. Click "Agree".

   * A browser tab will open for **Tidal Login**. Log in to your Tidal account.

5. The app will open and begin waiting for Spotify activity.

### Option B: Running from Source (Python)

If you are a developer or want to run the raw script:

1. **Install Python 3.10+**.

2. **Install Dependencies:**

   ```bash
   pip install -r requirements.txt
   ```
   
   *Make sure `keyring` is installed.*

3. **Run the script:**

   ```bash
   python spotify.py
   ```

## Usage Tips

* **Audio Output:** Go to **Settings > General** to select your specific audio device (e.g., "External DAC").

* **Wrong Song?** If the sync plays the wrong track, click the **"Report Wrong Song"** button on the main screen. Search for the correct track on Tidal, select it, and the app will remember this mapping forever.

* **Muting Spotify:** In Settings, enable **"Mute Spotify Desktop App"**. This allows you to use the Spotify UI for control while hearing the audio strictly from Tidal/VLC.

* **Resetting:** If you need to switch accounts or fix a login loop, go to **Settings > Factory Reset (Red Button)**. This wipes the settings file and securely deletes your tokens from the Windows Credential Manager.

## Troubleshooting

* **App crashes immediately:** Usually missing VLC 64-bit. Ensure it is installed.

* **401 Unauthorized Errors:** The app now handles this automatically by lowering the quality for that specific song (e.g., from Max to High) until it plays.

* **Tidal Login fails:** Ensure you have an active subscription. Free accounts do not support API streaming.