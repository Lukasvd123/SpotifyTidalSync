"""Muting the source app so only our Tidal stream is audible.

Spotify keeps playing (silently) as the queue master; we mirror it through our own player.
"""

import os
import subprocess
import time

from ..config import IS_LINUX, IS_WINDOWS, MUTE_MARKER_FILE, logger

PYCAW_AVAILABLE = False
if IS_WINDOWS:
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        import comtypes
        PYCAW_AVAILABLE = True
    except ImportError:
        pass

# The old code shelled out to pactl on every loop tick, once a second forever.
# The mute only needs re-asserting periodically, in case Spotify or PipeWire
# stream-restore overrides it.
REASSERT_INTERVAL = 5.0


def _pactl_sink_inputs():
    """Parse `pactl list sink-inputs` into [(index, {prop: value})]."""
    try:
        result = subprocess.run(["pactl", "list", "sink-inputs"],
                                capture_output=True, text=True, timeout=3)
        if result.returncode != 0:
            return []
    except FileNotFoundError:
        return []
    except Exception:
        return []

    sinks, index, props = [], None, {}
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            if index is not None:
                sinks.append((index, props))
            index, props = line.split("#")[1], {}
        elif index is not None and line.startswith("Mute:"):
            props["_mute"] = line.split(":", 1)[1].strip().lower()
        elif index is not None and "=" in line:
            key, _, val = line.partition("=")
            props[key.strip()] = val.strip().strip('"')
    if index is not None:
        sinks.append((index, props))
    return sinks


def _is_ours(props):
    if props.get("application.process.id") == str(os.getpid()):
        return True
    name = (props.get("application.name", "") + " " + props.get("media.name", "")).lower()
    return "vlc" in name or "spotifytidalsync" in name


def _is_spotify(props):
    """Spotify's stream names differ across native, Flatpak and Snap builds."""
    haystack = " ".join([
        props.get("application.name", ""),
        props.get("application.process.binary", ""),
        props.get("node.name", ""),
        props.get("media.name", ""),
        props.get("application.process.properties", ""),
    ]).lower()
    return "spotify" in haystack


def _write_marker(muted):
    """Record that we hold the source muted, so a crash can be undone.

    Windows persists per-app mutes in the mixer and PipeWire's stream-restore
    remembers them too: a killed app used to leave Spotify silent for good.
    """
    try:
        if muted:
            with open(MUTE_MARKER_FILE, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        elif os.path.exists(MUTE_MARKER_FILE):
            os.remove(MUTE_MARKER_FILE)
    except OSError as e:
        logger.debug(f"Mute marker error: {e}")


class SourceMuter:
    def __init__(self, enabled=True, clock=time.monotonic):
        self.enabled = enabled
        self.clock = clock
        self._muted = None          # our intended state, None = untouched
        self._last_assert = 0.0
        self._warned = False

    def recover_from_crash(self):
        """Unmute a source a previous run left muted."""
        if os.path.exists(MUTE_MARKER_FILE):
            logger.info("Previous run left the source muted; restoring it")
            self._apply(False)
            _write_marker(False)

    def mute(self, force=False):
        """Ensure the source is muted. Throttled unless `force`.

        Force at track boundaries, where a late mute leaks a second of audio.
        """
        if not self.enabled:
            return
        now = self.clock()
        if not force and self._muted is True and (now - self._last_assert) < REASSERT_INTERVAL:
            return
        self._last_assert = now
        if self._muted is not True:
            _write_marker(True)
        self._muted = True
        self._apply(True)

    def unmute(self):
        """Restore the source's audio (AI DJ commentary, fallback, shutdown)."""
        was_muted = self._muted
        self._muted = False
        self._last_assert = self.clock()
        if was_muted is not False:
            self._apply(False)
        _write_marker(False)

    @property
    def is_muted(self):
        return self._muted is True

    def restore(self):
        if self._muted:
            self._apply(False)
        self._muted = None
        _write_marker(False)

    def _apply(self, mute):
        if IS_WINDOWS:
            self._apply_windows(mute)
        elif IS_LINUX:
            self._apply_linux(mute)

    # --- windows ---------------------------------------------------------
    def _apply_windows(self, mute):
        if not PYCAW_AVAILABLE:
            return
        try:
            comtypes.CoInitialize()
            try:
                for session in AudioUtilities.GetAllSessions():
                    if session.Process and session.Process.name().lower() == "spotify.exe":
                        volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                        volume.SetMute(1 if mute else 0, None)
            finally:
                comtypes.CoUninitialize()
        except Exception as e:
            logger.debug(f"pycaw mute error: {e}")

    # --- linux -----------------------------------------------------------
    def _apply_linux(self, mute):
        target = "1" if mute else "0"
        found = False
        try:
            for index, props in _pactl_sink_inputs():
                if _is_ours(props) or not _is_spotify(props):
                    continue
                found = True
                if props.get("_mute") == ("yes" if mute else "no"):
                    continue
                subprocess.run(["pactl", "set-sink-input-mute", index, target],
                               capture_output=True, timeout=2)
                logger.debug(f"{'Muted' if mute else 'Unmuted'} Spotify sink #{index}")
        except Exception as e:
            logger.debug(f"Linux mute error: {e}")
            return

        if mute and not found and not self._warned:
            names = [p.get("application.name") or p.get("media.name") or "?"
                     for _, p in _pactl_sink_inputs()]
            logger.warning(
                "Could not find a Spotify audio stream to mute. Active streams: "
                f"{names or 'none'}. Spotify's stream only appears once it plays audio."
            )
            self._warned = True
        elif found:
            self._warned = False
