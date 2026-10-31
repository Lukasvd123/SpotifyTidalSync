"""Application entry point."""

import atexit
import os
import platform
import signal
import sys
import tkinter as tk
from tkinter import messagebox

from .config import APPDATA_DIR, IS_MACOS, IS_WINDOWS, logger, setup_logging

VLC_URL = "https://www.videolan.org/vlc/"


def acquire_instance_lock():
    """Keep a single instance; two of them would fight over the mute state."""
    path = os.path.join(APPDATA_DIR, "instance.lock")
    try:
        # "a", not "w": opening with "w" truncated the running instance's pid
        # before the lock attempt even failed.
        handle = open(path, "a+")
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    except (IOError, OSError):
        return None


def _error_dialog(message):
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("SpotifyTidalSync", message)
        root.destroy()
    except Exception:
        pass
    logger.error(message)


def _enable_dpi_awareness():
    """Without this Windows bitmap-scales the whole UI on HiDPI screens."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _install_exit_handlers(engine):
    """Unmute the source however we exit: window close, logout, kill."""
    atexit.register(engine.shutdown)

    def on_signal(signum, _frame):
        logger.info(f"Signal {signum}; shutting down")
        engine.shutdown()
        os._exit(0)

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass


def main():
    setup_logging()

    if IS_MACOS:
        _error_dialog(
            "macOS is not supported yet.\n\n"
            "SpotifyTidalSync needs to detect, control and mute Spotify through the "
            "operating system, which is only implemented for Windows and Linux.")
        return 1

    lock = acquire_instance_lock()
    if lock is None:
        _error_dialog("Another instance is already running.")
        return 1

    _enable_dpi_awareness()

    try:
        from .sync import SyncEngine
        engine = SyncEngine()
    except Exception as e:
        logger.exception("Startup failed")
        hint = ""
        # python-vlc fails in several ways when libvlc is missing or the wrong
        # architecture: ImportError, OSError, NameError, NotImplementedError.
        if "vlc" in repr(e).lower() or isinstance(
                e, (ImportError, OSError, NameError, NotImplementedError)):
            bits = "64-bit " if platform.machine().endswith("64") else ""
            hint = (f"\n\nVLC media player ({bits}desktop version) is required for playback. "
                    f"Install it from {VLC_URL} and start the app again.")
        _error_dialog(f"Could not start: {e}{hint}")
        return 1

    from .sources.detector import DBUS_AVAILABLE, SMTC_AVAILABLE
    from .ui import MainApp
    logger.info(f"Platform: {platform.system()} {platform.release()} | Python {platform.python_version()} "
                f"| MPRIS: {DBUS_AVAILABLE} | SMTC: {SMTC_AVAILABLE}")

    _install_exit_handlers(engine)
    app = MainApp(engine)

    engine.on_update = app.update_snapshot
    engine.on_match_needed = app.request_match
    engine.open_browser = app.open_browser

    engine.start_background()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
