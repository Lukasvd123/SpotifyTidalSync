"""Main player window."""

import os
import threading
import tkinter as tk
from io import BytesIO
from tkinter import messagebox, ttk

import requests
from PIL import Image, ImageTk

from ..config import UI_FONT, logger, resource_path
from .manual_match import ManualMatchWindow
from .settings_window import SettingsWindow
from .theme import (ACCENT, BG, BUTTON_STYLE, DIM, MUTED, TEXT, apply_dark_title_bar,
                    open_browser, scaled)

ART_SIZE = 300


class MainApp(tk.Tk):
    def __init__(self, engine):
        super().__init__(className="spotifysync")
        self.engine = engine
        self.title("SpotifyTidalSync")
        self.configure(bg=BG)

        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
        width, height = scaled(self, 400, 760)
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        apply_dark_title_bar(self)
        self._set_icon()

        self._pending_snapshot = None
        self._pending_art = None
        self._art_url = None
        self._imagetk_broken = False
        self._dragging = False

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._bind_keys()

        self._poll_snapshot()
        self._poll_progress()

    def _set_icon(self):
        path = resource_path(os.path.join("assets", "logo.png"))
        if not os.path.exists(path):
            return
        for loader in (lambda: ImageTk.PhotoImage(Image.open(path)),
                       lambda: tk.PhotoImage(file=path)):
            try:
                self._icon = loader()
                self.iconphoto(True, self._icon)
                return
            except Exception:
                continue

    # --- layout ----------------------------------------------------------
    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Main.TLabel", background=BG, foreground=TEXT, font=(UI_FONT, 10))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 9))
        style.configure("Source.TLabel", background=BG, foreground="#666666", font=(UI_FONT, 8))
        style.configure("Quality.TLabel", background=BG, foreground=ACCENT,
                        font=(UI_FONT, 9, "bold"))
        style.configure("Seek.Horizontal.TScale", background=BG, troughcolor="#333333")

        self.art = tk.Label(self, bg=BG, text="[no art]", fg="#444444",
                            width=ART_SIZE, height=ART_SIZE)
        self.art.pack(pady=20)

        self.track_label = ttk.Label(self, text="Waiting for media...", wraplength=360,
                                     justify="center", style="Main.TLabel",
                                     font=(UI_FONT, 13, "bold"))
        self.track_label.pack(pady=(0, 5))

        self.status_label = ttk.Label(self, text="Starting...", style="Status.TLabel")
        self.status_label.pack()
        self.source_label = ttk.Label(self, text="", style="Source.TLabel")
        self.source_label.pack(pady=(3, 0))
        self.quality_label = ttk.Label(self, text="", style="Quality.TLabel")
        self.quality_label.pack(pady=(0, 10))

        seek_row = tk.Frame(self, bg=BG)
        seek_row.pack(fill="x", padx=25, pady=(5, 0))
        self.time_label = tk.Label(seek_row, text="0:00", bg=BG, fg=DIM,
                                   font=(UI_FONT, 8), width=5, anchor="w")
        self.time_label.pack(side="left")
        self.total_label = tk.Label(seek_row, text="0:00", bg=BG, fg=DIM,
                                    font=(UI_FONT, 8), width=5, anchor="e")
        self.total_label.pack(side="right")
        self.slider = ttk.Scale(seek_row, from_=0, to=100, orient="horizontal",
                                style="Seek.Horizontal.TScale")
        self.slider.pack(side="left", fill="x", expand=True, padx=5)
        self.slider.bind("<ButtonPress-1>", lambda _e: setattr(self, "_dragging", True))
        self.slider.bind("<ButtonRelease-1>", self._on_seek)

        controls = tk.Frame(self, bg=BG)
        controls.pack(pady=15)
        for text, command, width in (
            ("-10s", lambda: self.engine.skip(-10000), 4),
            ("<<", self.engine.previous_track, 4),
            ("Play/Pause", self.engine.toggle_play, 10),
            (">>", self.engine.next_track, 4),
            ("+10s", lambda: self.engine.skip(10000), 4),
        ):
            tk.Button(controls, text=text, width=width,
                      command=lambda c=command: self._run(c), **BUTTON_STYLE
                      ).pack(side="left", padx=3)

        tk.Button(self, text="Wrong song? Fix match", command=self.open_manual_match,
                  bg="#552222", fg="#ffbbbb", relief="flat", font=(UI_FONT, 9),
                  bd=0, cursor="hand2").pack(pady=15)

        tk.Button(self, text="Settings", command=self.open_settings, bg="#1a1a1a",
                  fg=MUTED, relief="flat", bd=0, cursor="hand2"
                  ).pack(side="bottom", fill="x", pady=20)

    def _bind_keys(self):
        self.bind("<space>", lambda _e: self._run(self.engine.toggle_play))
        self.bind("<Right>", lambda _e: self._run(lambda: self.engine.skip(10000)))
        self.bind("<Left>", lambda _e: self._run(lambda: self.engine.skip(-10000)))
        self.bind("<Control-Right>", lambda _e: self._run(self.engine.next_track))
        self.bind("<Control-Left>", lambda _e: self._run(self.engine.previous_track))
        self.bind("<Control-comma>", lambda _e: self.open_settings())

    @staticmethod
    def _run(command):
        """Run an engine command off the UI thread; they make network calls."""
        threading.Thread(target=command, daemon=True).start()

    # --- updates ---------------------------------------------------------
    def update_snapshot(self, snapshot):
        """Called from the sync thread; the UI thread picks it up."""
        self._pending_snapshot = snapshot

    def _poll_snapshot(self):
        snapshot = self._pending_snapshot
        if snapshot is not None:
            self._pending_snapshot = None
            try:
                self._apply(snapshot)
            except Exception as e:
                logger.debug(f"UI update error: {e}")
        self.after(400, self._poll_snapshot)

    def _apply(self, snapshot):
        self.track_label.config(text=snapshot["track"])
        self.status_label.config(text=snapshot["status"])

        parts = []
        if snapshot["source_app"]:
            parts.append(f"Source: {snapshot['source_app']}")
        parts.append("API" if snapshot["api"] else "OS detection")
        self.source_label.config(text="  |  ".join(parts))

        quality = snapshot["quality"]
        self.quality_label.config(
            text=f"Quality: {quality.replace('_', ' ').title()}" if quality else "")

        self._load_art(snapshot["art_url"])
        self._show_pending_art()

    def _poll_progress(self):
        """Progress comes straight from the player so the bar stays smooth."""
        try:
            position = max(0, self.engine.player.position_ms())
            duration = max(0, self.engine.player.duration_ms())
            if not self._dragging and duration > 0:
                self.slider.configure(to=duration)
                self.slider.set(position)
            self.time_label.config(text=self._format(position))
            self.total_label.config(text=self._format(duration))
        except Exception:
            pass
        self.after(250, self._poll_progress)

    @staticmethod
    def _format(ms):
        seconds = max(0, ms) // 1000
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _on_seek(self, _event):
        self._dragging = False
        position = int(float(self.slider.get()))
        self._run(lambda: self.engine.seek_to(position))

    # --- album art -------------------------------------------------------
    def _load_art(self, url):
        if not url or url == self._art_url:
            return
        self._art_url = url

        def fetch():
            try:
                data = requests.get(url, timeout=6).content
                image = Image.open(BytesIO(data)).resize(
                    (ART_SIZE, ART_SIZE), Image.Resampling.LANCZOS)
                self._pending_art = image
            except Exception as e:
                logger.debug(f"Album art fetch failed: {e}")

        threading.Thread(target=fetch, daemon=True).start()

    def _show_pending_art(self):
        image = self._pending_art
        if image is None:
            return
        self._pending_art = None

        # ImageTk is missing in some PyInstaller and system-Python combinations.
        # Once it fails, stop retrying and use the temp-file path instead.
        if not self._imagetk_broken:
            try:
                self._set_art(ImageTk.PhotoImage(image))
                return
            except Exception as e:
                logger.info(f"ImageTk unavailable ({e}); using PhotoImage fallback")
                self._imagetk_broken = True
        try:
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "spotifysync_art.png")
            image.save(path, "PNG")
            self._set_art(tk.PhotoImage(file=path))
        except Exception as e:
            logger.debug(f"Album art display failed: {e}")

    def _set_art(self, photo):
        self._art_photo = photo
        self.art.config(image=photo, width=ART_SIZE, height=ART_SIZE, text="")

    # --- windows ---------------------------------------------------------
    def open_manual_match(self, source_state=None):
        state = source_state if source_state is not None else self.engine.current
        if state is None or not state.has_track:
            messagebox.showinfo("Fix Match", "Nothing is playing to fix yet.")
            return
        ManualMatchWindow(self, self.engine, state)

    def request_match(self, source_state):
        """Called from the sync thread when nothing matched."""
        self.after(0, lambda: self.open_manual_match(source_state))

    def open_settings(self):
        SettingsWindow(self, self.engine)

    def open_browser(self, url):
        self.after(0, lambda: open_browser(url, self))

    def on_close(self):
        self.engine.shutdown()
        self.destroy()
        os._exit(0)
