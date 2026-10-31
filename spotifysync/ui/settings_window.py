"""Settings: audio device, toggles, Tidal login and output, Spotify API, logs."""

import os
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .. import credentials
from ..config import (APPDATA_DIR, IS_LINUX, IS_WINDOWS, UI_FONT, load_settings,
                      log_queue, logger, save_setting)
from .theme import DANGER, DIM, MUTED, PANEL, TEXT, DarkWindow, open_browser

API_HELP = (
    "Track detection uses your OS media controls by default, which works with "
    "any player.\n\n"
    "Connecting the Spotify API additionally enables:\n"
    "  - Queue prefetch, so track changes are near instant\n"
    "  - Exact seek and position sync\n"
    "  - Stable track identity and durations for better matching\n"
    "  - AI DJ support"
)


class SettingsWindow(DarkWindow):
    def __init__(self, parent, engine):
        super().__init__(parent, "Settings", 640, 620)
        self.engine = engine
        self.devices = {}

        style = ttk.Style()
        style.configure("TNotebook", background=PANEL)
        style.configure("TNotebook.Tab", padding=[12, 6])

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=10, pady=10)

        general = tk.Frame(tabs, bg=PANEL)
        quality = tk.Frame(tabs, bg=PANEL)
        spotify = tk.Frame(tabs, bg=PANEL)
        logs = tk.Frame(tabs, bg=PANEL)
        tabs.add(general, text="General")
        tabs.add(quality, text="Tidal & Quality")
        tabs.add(spotify, text="Spotify API")
        tabs.add(logs, text="Logs")

        self._build_general(general)
        self._build_quality(quality)
        self._build_spotify(spotify)
        self._build_logs(logs)

    # --- general ---------------------------------------------------------
    def _build_general(self, frame):
        tk.Label(frame, text="Audio output device", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 10)).pack(anchor="w", padx=20, pady=(20, 5))

        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill="x", padx=20, pady=(0, 15))
        self.device_box = ttk.Combobox(row, state="readonly", values=["Loading..."])
        self.device_box.pack(side="left", fill="x", expand=True)
        self.device_box.bind("<<ComboboxSelected>>", self._on_device)
        tk.Button(row, text="Refresh", command=self._load_devices, bg="#333333",
                  fg=DIM, relief="flat", padx=10, font=(UI_FONT, 8)).pack(side="left", padx=(8, 0))

        settings = load_settings()
        self.mute_var = tk.BooleanVar(value=settings["mute_spotify"])
        self.fav_var = tk.BooleanVar(value=settings["auto_favorite"])
        self._checkbox(frame, "Mute the source app while mirroring", self.mute_var)
        self._checkbox(frame, "Auto-favorite on Tidal at 90% played", self.fav_var)

        tk.Button(frame, text="Open Volume Mixer", command=self._open_mixer,
                  bg="#333333", fg=TEXT, relief="flat", padx=10, pady=5
                  ).pack(anchor="w", padx=20, pady=20)

        tk.Label(frame, text="Reset", bg=PANEL, fg="#ff5555",
                 font=(UI_FONT, 10, "bold")).pack(anchor="w", padx=20, pady=(20, 5))
        tk.Button(frame, text="Wipe all data and logins", command=self._wipe,
                  bg=DANGER, fg=TEXT, relief="flat", padx=10, pady=5).pack(anchor="w", padx=20)

        self._load_devices()

    def _checkbox(self, frame, text, variable):
        tk.Checkbutton(frame, text=text, variable=variable, bg=PANEL, fg=TEXT,
                       selectcolor=PANEL, activebackground=PANEL, activeforeground=TEXT,
                       command=self._save_toggles, font=(UI_FONT, 9)
                       ).pack(anchor="w", padx=18, pady=4)

    def _save_toggles(self):
        save_setting("mute_spotify", self.mute_var.get())
        save_setting("auto_favorite", self.fav_var.get())
        self.engine.auto_favorite = self.fav_var.get()
        self.engine.set_muting(self.mute_var.get())

    def _load_devices(self):
        self.device_box.set("Loading...")
        threading.Thread(target=self._load_devices_worker, daemon=True).start()

    def _load_devices_worker(self):
        found = self.engine.player.get_audio_devices()
        if not found and IS_LINUX and shutil.which("pactl"):
            found = self._pactl_sinks()
        if not found:
            found = [("System default", None)]

        self.devices = {name: dev_id for name, dev_id in found}
        current = load_settings().get("last_device_id")
        selected = next((n for n, d in found if d == current), found[0][0])

        def apply():
            if not self.winfo_exists():
                return
            self.device_box["values"] = list(self.devices)
            self.device_box.set(selected)

        try:
            self.after(0, apply)
        except Exception:
            pass

    @staticmethod
    def _pactl_sinks():
        try:
            result = subprocess.run(["pactl", "list", "short", "sinks"],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode != 0:
                return []
            sinks = []
            for line in result.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    sinks.append((parts[1], parts[1]))
            return sinks
        except Exception:
            return []

    def _on_device(self, _event):
        device_id = self.devices.get(self.device_box.get())
        if device_id:
            self.engine.player.set_device(device_id)

    def _open_mixer(self):
        try:
            if IS_WINDOWS:
                subprocess.Popen(["start", "ms-settings:apps-volume"], shell=True)
                return
            for candidate in (["pavucontrol"], ["gnome-control-center", "sound"],
                              ["plasma-pa"], ["mate-volume-control"]):
                if shutil.which(candidate[0]):
                    subprocess.Popen(candidate)
                    return
            messagebox.showinfo(
                "Audio Control",
                "No mixer found. Install one, for example:\n  sudo dnf install pavucontrol")
        except Exception as e:
            logger.error(f"Could not open mixer: {e}")

    def _wipe(self):
        if not messagebox.askyesno("Reset", "Delete all settings and logins? The app will close."):
            return
        # Unmute first: the wipe exits the app, and a Spotify left muted in the
        # mixer stays muted after we are gone.
        self.engine.shutdown()
        credentials.delete_all()
        shutil.rmtree(APPDATA_DIR, ignore_errors=True)
        os._exit(0)

    # --- tidal and quality -----------------------------------------------
    def _build_quality(self, frame):
        tk.Label(frame, text="Tidal login", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(20, 5))
        if self.engine.library.is_pkce:
            state = "Logged in for FLAC and hi-res."
        else:
            state = ("This login only receives AAC (lossy) from Tidal. "
                     "Log in again to get FLAC and hi-res.")
        tk.Label(frame, text=state, bg=PANEL, fg=DIM, font=(UI_FONT, 9),
                 justify="left", wraplength=560).pack(anchor="w", padx=20)
        tk.Button(frame, text="Log in to Tidal again", command=self._tidal_login,
                  bg="#333333", fg=TEXT, relief="flat", padx=10, pady=5
                  ).pack(anchor="w", padx=20, pady=(8, 18))

        tk.Label(frame, text="Bit-perfect output", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(0, 5))
        settings = load_settings()
        if IS_WINDOWS:
            self.exclusive_var = tk.BooleanVar(value=settings["audio_exclusive"])
            self._option(frame, "Exclusive mode: the device plays only Tidal, at the track's own "
                                "rate and bit depth (other apps are silent on it meanwhile)",
                         self.exclusive_var,
                         lambda: self.engine.set_output_mode(exclusive=self.exclusive_var.get()))
        elif IS_LINUX:
            self.rate_var = tk.BooleanVar(value=settings["force_rate"])
            self._option(frame, "Switch the device to each track's sample rate (other apps "
                                "are resampled instead of the music)",
                         self.rate_var,
                         lambda: self.engine.set_output_mode(force_rate=self.rate_var.get()))
        self.rg_var = tk.BooleanVar(value=settings["replaygain"])
        self._option(frame, "Even out loudness between tracks (ReplayGain, from the next track; "
                            "changes the samples, so not bit-perfect)",
                     self.rg_var, lambda: self.engine.set_replaygain(self.rg_var.get()))
        tk.Label(frame, text="Bit-perfect also needs the app volume at 100%: set the "
                             "loudness on your speakers or amplifier instead.",
                 bg=PANEL, fg=MUTED, font=(UI_FONT, 9), justify="left",
                 wraplength=560).pack(anchor="w", padx=20, pady=(10, 0))

    def _option(self, frame, text, variable, command):
        tk.Checkbutton(frame, text=text, variable=variable, bg=PANEL, fg=TEXT,
                       selectcolor=PANEL, activebackground=PANEL, activeforeground=TEXT,
                       command=command, font=(UI_FONT, 9), wraplength=540, justify="left"
                       ).pack(anchor="w", padx=18, pady=4)

    def _tidal_login(self):
        self.engine.tidal_login()
        self.destroy()

    # --- spotify api -----------------------------------------------------
    def _build_spotify(self, frame):
        tk.Label(frame, text="Spotify API (optional)", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(20, 5))
        tk.Label(frame, text=API_HELP, bg=PANEL, fg=DIM, font=(UI_FONT, 9),
                 justify="left", wraplength=560).pack(anchor="w", padx=20, pady=(0, 15))

        settings = load_settings()
        self.api_var = tk.BooleanVar(value=settings["use_spotify_api"])
        tk.Checkbutton(frame, text="Enable Spotify API", variable=self.api_var,
                       bg=PANEL, fg=TEXT, selectcolor=PANEL, activebackground=PANEL,
                       activeforeground=TEXT, font=(UI_FONT, 10, "bold"),
                       command=self._toggle_api).pack(anchor="w", padx=18, pady=(0, 10))

        if self.engine.source.api_available:
            status, colour = "Connected", "#00cc00"
        elif settings["use_spotify_api"]:
            status, colour = "Enabled but not connected (check credentials, then restart)", "#cc8800"
        else:
            status, colour = "Using OS media detection", "#cccc00"
        self.status_label = tk.Label(frame, text=f"Status: {status}", bg=PANEL, fg=colour,
                                     font=(UI_FONT, 9, "bold"), wraplength=560, justify="left")
        self.status_label.pack(anchor="w", padx=20, pady=(0, 15))

        self.creds = tk.Frame(frame, bg=PANEL)
        self.creds.pack(fill="x")

        self.entry_id = self._credential_entry("Client ID", False)
        self.entry_secret = self._credential_entry("Client Secret", True)

        saved_id, saved_secret = credentials.get_spotify_credentials()
        self.entry_id.insert(0, saved_id or "")
        self.entry_secret.insert(0, saved_secret or "")

        buttons = tk.Frame(self.creds, bg=PANEL)
        buttons.pack(anchor="w", padx=20, pady=15)
        tk.Button(buttons, text="Save", command=self._save_credentials, bg="#008800",
                  fg=TEXT, relief="flat", padx=12, pady=5,
                  font=(UI_FONT, 9, "bold")).pack(side="left", padx=(0, 10))
        tk.Button(buttons, text="Clear", command=self._clear_credentials, bg=DANGER,
                  fg=TEXT, relief="flat", padx=12, pady=5).pack(side="left", padx=(0, 10))
        tk.Button(buttons, text="Spotify Dashboard",
                  command=lambda: open_browser("https://developer.spotify.com/dashboard", self),
                  bg="#333333", fg=TEXT, relief="flat", padx=12, pady=5).pack(side="left")

        tk.Label(self.creds, text="Restart the app after saving.", bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 8)).pack(anchor="w", padx=20)

        if not self.api_var.get():
            self.creds.pack_forget()

    def _credential_entry(self, label, secret):
        tk.Label(self.creds, text=f"{label}:", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 9)).pack(anchor="w", padx=20, pady=(8, 2))
        entry = tk.Entry(self.creds, width=48, bg="#333333", fg=TEXT, relief="flat",
                         insertbackground=TEXT, font=(UI_FONT, 10),
                         show="*" if secret else "")
        entry.pack(anchor="w", padx=20, ipady=3)
        return entry

    def _toggle_api(self):
        enabled = self.api_var.get()
        save_setting("use_spotify_api", enabled)
        if enabled:
            self.creds.pack(fill="x")
            self.status_label.config(text="Status: enter credentials below, then restart",
                                     fg="#cc8800")
        else:
            self.creds.pack_forget()
            self.status_label.config(text="Status: OS media detection (restart to apply)",
                                     fg="#cccc00")

    def _save_credentials(self):
        client_id = self.entry_id.get().strip()
        client_secret = self.entry_secret.get().strip()
        if not client_id or not client_secret:
            messagebox.showwarning("Missing", "Enter both the Client ID and Client Secret.")
            return
        credentials.set(credentials.SPOTIFY_ID_KEY, client_id)
        credentials.set(credentials.SPOTIFY_SECRET_KEY, client_secret)
        messagebox.showinfo("Saved", "Credentials saved. Restart the app to connect.")

    def _clear_credentials(self):
        if not messagebox.askyesno("Clear", "Remove the Spotify API credentials?"):
            return
        for key in (credentials.SPOTIFY_ID_KEY, credentials.SPOTIFY_SECRET_KEY,
                    credentials.SPOTIFY_TOKEN_KEY):
            credentials.delete(key)
        self.entry_id.delete(0, tk.END)
        self.entry_secret.delete(0, tk.END)
        messagebox.showinfo("Cleared", "Credentials removed. Restart the app.")

    # --- logs ------------------------------------------------------------
    def _build_logs(self, frame):
        self.log_view = scrolledtext.ScrolledText(frame, bg="#101010", fg="#00cc44",
                                                  font=("monospace", 9), state="disabled",
                                                  relief="flat")
        self.log_view.pack(fill="both", expand=True, padx=5, pady=5)
        self._pump_logs()

    def _pump_logs(self):
        if not self.winfo_exists():
            return
        try:
            lines = []
            while not log_queue.empty():
                lines.append(log_queue.get_nowait())
            if lines:
                self.log_view.config(state="normal")
                self.log_view.insert(tk.END, "\n".join(lines) + "\n")
                self.log_view.see(tk.END)
                self.log_view.config(state="disabled")
        except Exception:
            pass
        self.after(500, self._pump_logs)
