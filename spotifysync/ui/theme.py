"""Shared dark-theme colours and window helpers."""

import tkinter as tk
import webbrowser
from tkinter import messagebox

from ..config import IS_WINDOWS, UI_FONT, logger

BG = "#121212"
PANEL = "#1e1e1e"
FIELD = "#2b2b2b"
BUTTON = "#282828"
BUTTON_ACTIVE = "#404040"
TEXT = "#ffffff"
MUTED = "#888888"
DIM = "#bbbbbb"
ACCENT = "#1DB954"
DANGER = "#880000"

BUTTON_STYLE = {
    "bg": BUTTON, "fg": TEXT, "relief": "flat", "font": (UI_FONT, 10),
    "activebackground": BUTTON_ACTIVE, "activeforeground": TEXT,
    "bd": 0, "highlightthickness": 0, "cursor": "hand2",
}


def apply_dark_title_bar(window):
    """Dark title bar on Windows 10/11."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def scaled(window, width, height):
    """Scale a window size for HiDPI displays."""
    try:
        scale = max(1.0, window.winfo_fpixels("1i") / 96.0)
        return int(width * scale), int(height * scale)
    except Exception:
        return width, height


def open_browser(url, parent=None):
    """Open a URL, falling back to showing it if no browser is available."""
    try:
        if webbrowser.open(url):
            return
        raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        logger.warning(f"Could not open browser: {e}")
        message = f"Could not open your browser.\n\nPlease open this URL manually:\n\n{url}"
        if parent is not None:
            try:
                parent.clipboard_clear()
                parent.clipboard_append(url)
                message += "\n\n(copied to your clipboard)"
            except Exception:
                pass
            messagebox.showinfo("Browser Not Available", message)
        else:
            print(f"\nOpen this URL to continue:\n{url}\n")


class DarkWindow(tk.Toplevel):
    def __init__(self, parent, title, width, height):
        super().__init__(parent)
        self.title(title)
        w, h = scaled(self, width, height)
        self.geometry(f"{w}x{h}")
        self.configure(bg=PANEL)
        self.update_idletasks()
        apply_dark_title_bar(self)
