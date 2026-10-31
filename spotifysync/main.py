"""Application entry point."""

import atexit
import os
import platform
import signal
import sys
import time
import tkinter as tk
from tkinter import messagebox

from . import updates
from .config import APPDATA_DIR, IS_MACOS, IS_WINDOWS, logger, setup_logging

LIBMPV_HINT = (
    "libmpv is required for playback. "
    + ("It ships with SpotifySync.exe; if you run from source, run build_windows.bat once."
       if IS_WINDOWS else
       "Install it with your package manager, e.g. 'sudo dnf install mpv-libs' (Fedora) "
       "or 'sudo apt install libmpv2' (Debian/Ubuntu)."))


_LOCK_OFFSET = 4096


def acquire_instance_lock():
    """Keep a single instance; two of them would fight over the mute state."""
    path = os.path.join(APPDATA_DIR, "instance.lock")
    try:
        # "a", not "w": opening with "w" truncated the running instance's pid
        # before the lock attempt even failed.
        handle = open(path, "a+")
        if IS_WINDOWS:
            import msvcrt
            # Windows locks block reads of the locked range too: lock a byte
            # past the pid so it stays readable.
            handle.seek(_LOCK_OFFSET)
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


def self_test(audio=False):
    """`SpotifySync --self-test [audio]`: load libmpv and open a player, for
    checking a build (CI runs it on every package). With `audio`, one second
    of silence goes through the real output device. Exit code 0 when it works;
    the result is also written to self-test.txt next to the log, since the
    windowed .exe has no console.
    """
    try:
        from .audio.player import AudioPlayer
        player = AudioPlayer() if audio else AudioPlayer(ao="null")
        import mpv
        line, code = f"ok: {player._mpv.mpv_version} from {mpv.backend._name}", 0
        if audio:
            player._mpv.volume = 0
            player.play_url("av://lavfi:anullsrc=d=1")
            if not player.wait_until_playing(timeout=5) or not player._get("current_ao"):
                raise RuntimeError("audio output did not open")
            line += f", output {player._get('current_ao')}"
        player.shutdown()
    except Exception as e:
        line, code = f"failed: {e!r}", 1
    if sys.stdout is not None:
        print(line)
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        with open(os.path.join(APPDATA_DIR, "self-test.txt"), "w", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return code


def main():
    if "--self-test" in sys.argv:
        return self_test(audio="audio" in sys.argv)
    setup_logging()

    if IS_MACOS:
        _error_dialog(
            "macOS is not supported yet.\n\n"
            "SpotifyTidalSync needs to detect, control and mute Spotify through the "
            "operating system, which is only implemented for Windows and Linux.")
        return 1

    lock = acquire_instance_lock()
    if lock is None and "--after-update" in sys.argv:
        # Started by the old version, which is still on its way out.
        for _ in range(30):
            time.sleep(0.5)
            lock = acquire_instance_lock()
            if lock is not None:
                break
    if lock is None:
        _error_dialog("Another instance is already running.")
        return 1

    _enable_dpi_awareness()
    updates.cleanup_previous()

    try:
        from .sync import SyncEngine
        engine = SyncEngine()
    except Exception as e:
        logger.exception("Startup failed")
        hint = ""
        # python-mpv raises OSError when libmpv is missing or the wrong
        # architecture.
        if "mpv" in repr(e).lower() or isinstance(e, (ImportError, OSError)):
            hint = "\n\n" + LIBMPV_HINT
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
    engine.ask_tidal_login = app.ask_tidal_login

    engine.start_background()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
