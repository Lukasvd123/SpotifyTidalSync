"""VLC playback wrapper."""

import os
import subprocess
import threading
import time

import vlc

from ..config import IS_LINUX, load_settings, logger, save_setting
from .vlcenv import build_vlc_args

_STATUS = {
    vlc.State.NothingSpecial: "idle",
    vlc.State.Opening: "loading",
    vlc.State.Buffering: "loading",
    vlc.State.Playing: "playing",
    vlc.State.Paused: "paused",
    vlc.State.Stopped: "idle",
    vlc.State.Ended: "ended",
    vlc.State.Error: "error",
}


class AudioPlayer:
    """Two libvlc players: the active one, and a standby that can pre-buffer
    the next track so the switch at a boundary is instant."""

    def __init__(self, extra_args=()):
        args = build_vlc_args() + list(extra_args)
        self.instance = vlc.Instance(*args)
        if self.instance is None:
            raise RuntimeError("libvlc failed to initialise")
        self.player = self.instance.media_player_new()
        self._standby = self.instance.media_player_new()
        self._standby_url = None

        settings = load_settings()
        self.volume = int(settings.get("volume", 100))
        self._apply_volume()

        self._device = settings.get("last_device_id")
        if self._device:
            threading.Timer(1.0, lambda: self.set_device(self._device, save=False)).start()
        logger.debug(f"AudioPlayer initialised: {args}")

    def _apply_volume(self):
        for player in (self.player, self._standby):
            try:
                player.audio_set_volume(self.volume)
            except Exception:
                pass

    def set_volume(self, level):
        self.volume = max(0, min(100, int(level)))
        self._apply_volume()
        save_setting("volume", self.volume)

    # --- devices ---------------------------------------------------------
    def get_audio_devices(self):
        devices = []
        try:
            mods = self.player.audio_output_device_enum()
            if not mods:
                return devices
            node, guard = mods, 0
            while node and guard < 100:
                node = node.contents
                desc = node.description.decode("utf-8", "ignore") if node.description else "Unknown"
                dev_id = node.device.decode("utf-8", "ignore") if node.device else None
                if dev_id:
                    devices.append((desc, dev_id))
                node = node.next
                guard += 1
            vlc.libvlc_audio_output_device_list_release(mods)
        except Exception as e:
            logger.error(f"Error listing audio devices: {e}")
        return devices

    def set_device(self, device_id, save=True):
        self._device = device_id
        for player in (self.player, self._standby):
            try:
                player.audio_output_device_set(None, device_id)
            except Exception as e:
                logger.debug(f"set_device failed: {e}")
        if save:
            save_setting("last_device_id", device_id)

    # --- transport -------------------------------------------------------
    def play_url(self, url, start_ms=0):
        """Start a stream, optionally part-way in.

        A mid-song start is passed to VLC as the media's start time. Starting at
        0 and seeking once playback began made the first fraction of a second
        audible and forced a second buffer: the jump at the start of a song.
        """
        if start_ms <= 0 and url and url == self._standby_url and self._standby_ready():
            self._swap_in_standby()
            return

        media = self.instance.media_new(url)
        if start_ms > 0:
            media.add_option(f":start-time={start_ms / 1000.0:.3f}")
        self.player.set_media(media)
        self.player.play()
        self._apply_volume()
        self._after_start()

    def preload(self, url):
        """Buffer `url` in the standby player, paused at the first frame."""
        if not url or url == self._standby_url:
            return
        media = self.instance.media_new(url)
        media.add_option(":start-paused")
        self._standby.set_media(media)
        self._standby.play()
        self._standby_url = url
        logger.debug("Pre-buffering next track")

    def _standby_ready(self):
        return _STATUS.get(self._standby.get_state()) in ("paused", "playing")

    def _swap_in_standby(self):
        old = self.player
        self.player, self._standby = self._standby, old
        self._standby_url = None
        self._apply_volume()
        self.player.set_pause(0)
        old.stop()
        logger.debug("Started pre-buffered track")
        self._after_start()

    def clear_preload(self):
        if self._standby_url:
            self._standby.stop()
            self._standby_url = None

    def _after_start(self):
        if IS_LINUX:
            # Sink-input registration is asynchronous, so retry: PipeWire
            # stream-restore may have remembered a muted state for us.
            for delay in (0.3, 0.8, 1.6):
                threading.Timer(delay, self._unmute_own_stream).start()

    def pause(self):
        self.player.set_pause(1)

    def resume(self):
        self.player.set_pause(0)

    def stop(self):
        self.player.stop()

    def is_playing(self):
        return bool(self.player.is_playing())

    def status(self):
        """idle | loading | playing | paused | ended | error"""
        try:
            return _STATUS.get(self.player.get_state(), "idle")
        except Exception:
            return "error"

    def position_ms(self):
        return self.player.get_time()

    def duration_ms(self):
        return self.player.get_length()

    def has_track(self):
        return self.duration_ms() > 0

    def remaining_ms(self):
        """Milliseconds of audio left, or -1 when unknown."""
        duration, position = self.duration_ms(), self.position_ms()
        if duration <= 0 or position < 0:
            return -1
        return max(0, duration - position)

    def finished(self):
        """True once the stream has stopped producing audio.

        Checked via VLC state rather than time arithmetic: at end-of-stream
        get_time() and get_length() both return -1.
        """
        return self.status() in ("ended", "error")

    def seek(self, position_ms):
        self.player.set_time(int(max(0, position_ms)))

    def wait_until_playing(self, timeout=6.0):
        """Block until VLC starts; Tidal streams take a few seconds to buffer."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status()
            if state == "playing":
                return True
            if state == "error":
                return False
            time.sleep(0.05)
        return self.is_playing()

    # --- linux self-unmute -----------------------------------------------
    def _unmute_own_stream(self):
        """Unmute our own sink-inputs, identified by PID only.

        Matching on "vlc" in the name also grabbed the user's own VLC, and
        forcing 100% volume overrode whatever they set in the mixer.
        """
        our_pid = str(os.getpid())
        try:
            result = subprocess.run(["pactl", "list", "sink-inputs"],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode != 0:
                return
        except Exception:
            return

        index, is_ours, muted = None, False, False

        def apply():
            if index and is_ours and muted:
                subprocess.run(["pactl", "set-sink-input-mute", index, "0"],
                               capture_output=True, timeout=2)

        try:
            for raw in result.stdout.splitlines():
                line = raw.strip()
                if line.startswith("Sink Input #"):
                    apply()
                    index, is_ours, muted = line.split("#")[1], False, False
                elif index and line.startswith("Mute:"):
                    muted = line.split(":", 1)[1].strip().lower() == "yes"
                elif index and "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() == "application.process.id" and val.strip().strip('"') == our_pid:
                        is_ours = True
            apply()
        except Exception as e:
            logger.debug(f"VLC self-unmute error: {e}")
