"""Application entry point."""

import os
import platform
import sys
import tkinter as tk
from tkinter import messagebox

from .config import APPDATA_DIR, logger, setup_logging


def acquire_instance_lock():
    """Keep a single instance; two of them would fight over the mute state."""
    path = os.path.join(APPDATA_DIR, "instance.lock")
    try:
        handle = open(path, "w")
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    except (IOError, OSError):
        return None


def main():
    setup_logging()

    lock = acquire_instance_lock()
    if lock is None:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("SpotifyTidalSync", "Another instance is already running.")
            root.destroy()
        except Exception:
            print("Another instance is already running.")
        return 1

    # Imported here so logging is configured before VLC and tidalapi load.
    from .sync import SyncEngine
    from .ui import MainApp

    from .sources.detector import DBUS_AVAILABLE, SMTC_AVAILABLE
    logger.info(f"Platform: {platform.system()} | MPRIS: {DBUS_AVAILABLE} | SMTC: {SMTC_AVAILABLE}")

    engine = SyncEngine()
    app = MainApp(engine)

    engine.on_update = app.update_snapshot
    engine.on_match_needed = app.request_match
    engine.open_browser = app.open_browser

    engine.start_background()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
