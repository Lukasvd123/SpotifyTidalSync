"""VLC playback wrapper."""

import os
import subprocess
import threading
import time

import vlc

from ..config import IS_LINUX, load_settings, logger, save_setting
from .vlcenv import build_vlc_args


class AudioPlayer:
    def __init__(self):
        args = build_vlc_args()
        self.instance = vlc.Instance(*args)
        self.player = self.instance.media_player_new()
        self._set_volume(100)

        device = load_settings().get("last_device_id")
        if device:
            threading.Timer(1.0, lambda: self.set_device(device)).start()
        logger.debug(f"AudioPlayer initialised: {args}")

    def _set_volume(self, level):
        try:
            self.player.audio_set_volume(level)
        except Exception:
            pass

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

    def set_device(self, device_id):
        try:
            self.player.audio_output_device_set(None, device_id)
            save_setting("last_device_id", device_id)
        except Exception as e:
            logger.debug(f"set_device failed: {e}")

    # --- transport -------------------------------------------------------
    def play_url(self, url):
        media = self.instance.media_new(url)
        self.player.set_media(media)
        self.player.play()
        self._set_volume(100)
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

    def get_state(self):
        try:
            return self.player.get_state()
        except Exception:
            return None

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
        """True once the stream has actually ended.

        Checked via VLC state rather than time arithmetic: at end-of-stream
        get_time() and get_length() both return -1, which makes any time-based
        check miss the finish entirely.
        """
        return self.get_state() in (vlc.State.Ended, vlc.State.Error)

    def seek(self, position_ms):
        self.player.set_time(int(max(0, position_ms)))

    def wait_until_playing(self, timeout=6.0):
        """Block until VLC starts; Tidal streams take a few seconds to buffer."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.get_state()
            if state == vlc.State.Playing:
                return True
            if state == vlc.State.Error:
                return False
            time.sleep(0.05)
        return self.is_playing()

    # --- linux self-unmute -----------------------------------------------
    def _unmute_own_stream(self):
        """Unmute our own sink-inputs, identified by PID."""
        our_pid = str(os.getpid())
        try:
            result = subprocess.run(["pactl", "list", "sink-inputs"],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode != 0:
                return
        except Exception:
            return

        index, is_ours = None, False

        def apply():
            if index and is_ours:
                for cmd in (["set-sink-input-mute", index, "0"],
                            ["set-sink-input-volume", index, "100%"]):
                    subprocess.run(["pactl"] + cmd, capture_output=True, timeout=2)

        try:
            for raw in result.stdout.splitlines():
                line = raw.strip()
                if line.startswith("Sink Input #"):
                    apply()
                    index, is_ours = line.split("#")[1], False
                elif index and "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip().strip('"').lower()
                    if key == "application.process.id" and val == our_pid:
                        is_ours = True
                    elif key in ("application.name", "media.name") and "vlc" in val:
                        is_ours = True
            apply()
        except Exception as e:
            logger.debug(f"VLC self-unmute error: {e}")
