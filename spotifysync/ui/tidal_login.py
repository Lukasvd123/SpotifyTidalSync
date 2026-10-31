"""Tidal PKCE login: the browser ends on a Tidal "Oops" page whose address the
user pastes back here. Tidal only streams FLAC and hi-res to this login."""

import threading
import tkinter as tk

from ..config import UI_FONT
from .theme import ACCENT, BUTTON_STYLE, DIM, FIELD, PANEL, TEXT, DarkWindow, open_browser


class TidalLoginWindow(DarkWindow):
    def __init__(self, parent, url, on_done):
        super().__init__(parent, "Log in to Tidal", 520, 330)
        self.url = url
        self.on_done = on_done
        self.transient(parent)

        tk.Label(self, text="Log in to Tidal for lossless and hi-res", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 12, "bold")).pack(pady=(18, 8))
        steps = ("1. Log in to Tidal in the browser window that opened.\n"
                 "2. You end up on a page saying \"Oops\" or \"not found\". That is expected.\n"
                 "3. Copy that page's full address from the address bar and paste it below.")
        tk.Label(self, text=steps, bg=PANEL, fg=DIM, justify="left",
                 font=(UI_FONT, 10), wraplength=470).pack(padx=20, anchor="w")

        tk.Button(self, text="Open the login page again", command=self._reopen,
                  **BUTTON_STYLE).pack(pady=(10, 6))

        self.entry = tk.Entry(self, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=(UI_FONT, 10))
        self.entry.pack(fill="x", padx=20, ipady=5)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.error = tk.Label(self, text="", bg=PANEL, fg="#ff7777", font=(UI_FONT, 9))
        self.error.pack(pady=(4, 0))

        row = tk.Frame(self, bg=PANEL)
        row.pack(pady=10)
        tk.Button(row, text="Log in", command=self._submit, width=12,
                  **{**BUTTON_STYLE, "bg": ACCENT}).pack(side="left", padx=6)
        tk.Button(row, text="Cancel", command=self._cancel, width=12,
                  **BUTTON_STYLE).pack(side="left", padx=6)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.entry.focus_set()

    def _reopen(self):
        open_browser(self.url, self)

    def _submit(self):
        text = self.entry.get().strip()
        if "code=" not in text:
            self.error.config(text="That address has no login code. Paste the full address "
                                   "of the page you landed on.")
            return
        self._finish(text)

    def _cancel(self):
        self._finish(None)

    def _finish(self, result):
        callback, self.on_done = self.on_done, None
        self.destroy()
        if callback:
            callback(result)


def ask_blocking(root, url):
    """Show the dialog from a worker thread and wait for the pasted address."""
    done = threading.Event()
    result = []

    def finish(value):
        result.append(value)
        done.set()

    def show():
        try:
            TidalLoginWindow(root, url, finish)
        except Exception:
            finish(None)

    root.after(0, show)
    done.wait()
    return result[0] if result else None
