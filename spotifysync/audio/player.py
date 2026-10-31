"""Audio output through libmpv.

libmpv plays Tidal's MPEG-DASH lossless and hi-res streams (VLC 3 could not),
keeps a clock that matches the audio (libvlc's lagged up to 13s by the end of
a Tidal MP4 stream), and outputs samples untouched at volume 100: on Windows
through exclusive WASAPI, on Linux with the PipeWire graph forced to the
track's rate.
"""

import glob
import os
import subprocess
import sys
import threading
import time

from ..config import IS_LINUX, IS_WINDOWS, load_settings, logger, resource_path, save_setting


def _use_bundled_libmpv():
    """Make python-mpv load the libmpv shipped with a build.

    python-mpv looks for libmpv-2.dll on PATH (Windows) and asks
    ctypes.util.find_library("mpv") (Linux), which only searches the system:
    a package would use whatever libmpv happens to be installed, or none.
    """
    if IS_WINDOWS:
        for folder in (getattr(sys, "_MEIPASS", None), resource_path("lib")):
            if folder and os.path.isfile(os.path.join(folder, "libmpv-2.dll")):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
                os.add_dll_directory(folder)
                return
    elif IS_LINUX and getattr(sys, "frozen", False):
        bundled = sorted(glob.glob(os.path.join(sys._MEIPASS, "libmpv.so*")))
        if bundled:
            import ctypes.util
            system_lookup = ctypes.util.find_library
            ctypes.util.find_library = (
                lambda name: bundled[-1] if name == "mpv" else system_lookup(name))


_use_bundled_libmpv()

import mpv  # noqa: E402

# The DASH demuxer opens a local manifest and fetches its segments over https;
# ffmpeg refuses that mix unless the protocols are allowed explicitly.
_PROTOCOLS = "protocol_whitelist=[file,http,https,tls,tcp,crypto,data]"

# Stream properties libpipewire reads from the environment when mpv opens its
# stream: force the graph (and the DAC) to the track's rate.
_PIPEWIRE_PROPS = "{ node.force-rate = 0 media.role = Music }"


def _audio_output():
    if IS_WINDOWS:
        return "wasapi"
    if IS_LINUX:
        return "pipewire,pulse,alsa"
    return ""


class AudioPlayer:
    """One mpv core. The next track is pre-buffered as the next playlist entry
    (mpv prefetches it), and `keep-open=always` stops mpv advancing on its own:
    the engine decides when a track is over."""

    def __init__(self, **overrides):
        settings = load_settings()
        self.volume = int(settings.get("volume", 100))
        self.replaygain = bool(settings.get("replaygain", False))
        self.exclusive = IS_WINDOWS and bool(settings.get("audio_exclusive", True))
        self.force_rate = IS_LINUX and bool(settings.get("force_rate", True))
        if self.force_rate:
            # Without this the track is resampled to whatever rate other
            # apps' streams hold the device at.
            os.environ["PIPEWIRE_PROPS"] = _PIPEWIRE_PROPS

        options = dict(
            video=False, vo="null", ao=_audio_output(), idle=True,
            keep_open="always", prefetch_playlist=True, gapless_audio=True,
            cache=True, demuxer_max_bytes="64MiB", demuxer_lavf_o=_PROTOCOLS,
            volume=self.volume, volume_max=100, replaygain="no",
            audio_exclusive=self.exclusive, audio_client_name="SpotifyTidalSync",
            input_default_bindings=False, input_vo_keyboard=False,
            terminal=False, config=False, ytdl=False,
        )
        options.update(overrides)
        self._mpv = mpv.MPV(**{k: v for k, v in options.items() if v != ""})
        self._lock = threading.Lock()
        self._current_id = None     # mpv playlist entry id of what we play
        self._failed = set()        # entry ids that ended in an error
        self._preloaded = None      # (url, entry id) queued after the current one
        self._mpv.event_callback("end-file")(self._on_end_file)

        self._device = settings.get("last_device_id")
        if self._device:
            self.set_device(self._device, save=False)
        logger.debug(f"AudioPlayer: mpv {self._mpv.mpv_version}, ao={options['ao'] or 'auto'}, "
                     f"exclusive={self.exclusive}, force_rate={self.force_rate}")

    def _on_end_file(self, event):
        data = getattr(event, "data", None)
        if getattr(data, "reason", None) == mpv.MpvEventEndFile.ERROR:
            self._failed.add(data.playlist_entry_id)
            logger.debug(f"mpv end-file error {data.error} for entry {data.playlist_entry_id}")

    def _load(self, url, mode, options):
        """loadfile, returning the new playlist entry's id. Linux packages
        bundle the libmpv of an older distro (0.34): its loadfile has no index
        argument and may not return the id."""
        encoded = ",".join(f"{k}={v}" for k, v in options.items())
        if self._mpv.mpv_version_tuple >= (0, 38, 0):
            result = self._mpv.command("loadfile", url, mode, -1, encoded)
        else:
            result = self._mpv.command("loadfile", url, mode, encoded)
        entry = (result or {}).get("playlist_entry_id")
        if entry is None:
            playlist = self._get("playlist", [])
            pick = playlist[-1:] if mode == "append" else [e for e in playlist if e.get("current")]
            entry = pick[0].get("id") if pick else None
        return entry

    def _get(self, name, default=None):
        try:
            value = getattr(self._mpv, name)
        except Exception:
            return default
        return default if value is None else value

    # --- volume ----------------------------------------------------------
    def set_volume(self, level):
        self.volume = max(0, min(100, int(level)))
        self._mpv.volume = self.volume
        save_setting("volume", self.volume)

    def set_replaygain(self, enabled):
        """Applied from the next track on."""
        self.replaygain = bool(enabled)
        save_setting("replaygain", self.replaygain)

    def set_output_mode(self, exclusive=None, force_rate=None):
        """Change how the device is opened; reopens it (a short dropout)."""
        if exclusive is not None and IS_WINDOWS:
            self.exclusive = bool(exclusive)
            save_setting("audio_exclusive", self.exclusive)
            self._mpv.audio_exclusive = self.exclusive
        if force_rate is not None and IS_LINUX:
            self.force_rate = bool(force_rate)
            save_setting("force_rate", self.force_rate)
            if self.force_rate:
                os.environ["PIPEWIRE_PROPS"] = _PIPEWIRE_PROPS
            else:
                os.environ.pop("PIPEWIRE_PROPS", None)
        try:
            self._mpv.command("ao-reload")
        except Exception as e:
            logger.debug(f"ao-reload failed: {e}")

    def _file_options(self, gain_db):
        options = {}
        if self.replaygain and gain_db:
            # A per-file filter rather than volume-gain (mpv 0.36+ only).
            options["af"] = f"lavfi=[volume={gain_db:.2f}dB]"
        return options

    # --- devices ---------------------------------------------------------
    def get_audio_devices(self):
        devices = []
        for entry in self._get("audio_device_list", []):
            name = entry.get("name")
            if name and name != "auto":
                devices.append((entry.get("description") or name, name))
        return devices

    def _resolve_device(self, device_id):
        """Map a saved id to mpv's. VLC saved bare ids ("alsa_output.usb-..."),
        mpv prefixes the backend ("pipewire/alsa_output.usb-...")."""
        names = [name for _, name in self.get_audio_devices()]
        if device_id in names:
            return device_id
        for name in names:
            if name.split("/", 1)[-1] == device_id:
                return name
        return None

    def set_device(self, device_id, save=True):
        resolved = self._resolve_device(device_id) if device_id else None
        if device_id and resolved is None:
            logger.debug(f"Audio device '{device_id}' not found; using the default")
        self._device = resolved or device_id
        try:
            self._mpv.audio_device = resolved or "auto"
        except Exception as e:
            logger.debug(f"set_device failed: {e}")
        if save:
            save_setting("last_device_id", self._device)

    # --- transport -------------------------------------------------------
    def play_url(self, url, start_ms=0, gain_db=0.0):
        """Start a stream (URL or local DASH manifest), optionally part-way in.

        The start position is a per-file option, so mpv opens the stream there
        instead of playing from 0 and seeking: no jump at the start of a song.
        """
        with self._lock:
            preloaded, self._preloaded = self._preloaded, None
            if (start_ms <= 0 and url and preloaded and preloaded[0] == url
                    and self._has_next()):
                self._mpv.command("playlist-next", "force")
                self._current_id = preloaded[1]
                self._drop_finished()
                logger.debug("Started pre-buffered track")
            else:
                options = self._file_options(gain_db)
                if start_ms > 0:
                    options["start"] = f"{start_ms / 1000.0:.3f}"
                self._current_id = self._load(url, "replace", options)
            self._mpv.pause = False
        self._after_start()

    def _has_next(self):
        return self._get("playlist_count", 0) > self._get("playlist_pos", -1) + 1

    def _drop_finished(self):
        try:
            while self._get("playlist_pos", 0) > 0:
                self._mpv.command("playlist-remove", 0)
        except Exception as e:
            logger.debug(f"playlist cleanup failed: {e}")

    def preload(self, url, gain_db=0.0):
        """Queue `url` as the next entry; mpv buffers it ahead of the switch."""
        if not url or (self._preloaded and self._preloaded[0] == url):
            return
        with self._lock:
            if self._current_id is None:
                return
            self._mpv.command("playlist-clear")
            entry = self._load(url, "append", self._file_options(gain_db))
            self._preloaded = (url, entry)
        logger.debug("Pre-buffering next track")

    def clear_preload(self):
        with self._lock:
            if self._preloaded:
                self._mpv.command("playlist-clear")
                self._preloaded = None

    def _after_start(self):
        if IS_LINUX:
            # Sink-input registration is asynchronous, so retry: PipeWire
            # stream-restore may have remembered a muted state for us.
            for delay in (0.3, 0.8, 1.6):
                threading.Timer(delay, self._unmute_own_stream).start()

    def pause(self):
        self._mpv.pause = True

    def resume(self):
        if self.status() != "ended":
            self._mpv.pause = False

    def stop(self):
        with self._lock:
            self._mpv.command("stop")
            self._current_id = None
            self._preloaded = None

    def is_playing(self):
        return self.status() == "playing"

    def status(self):
        """idle | loading | playing | paused | ended | error"""
        if self._current_id is None:
            return "idle"
        if self._current_id in self._failed:
            return "error"
        if self._get("idle_active", False):
            return "idle"
        if self._get("eof_reached", False):
            return "ended"
        if self._get("pause", False):
            return "paused"
        if self._get("core_idle", True) or self._get("time_pos") is None:
            return "loading"
        return "playing"

    def position_ms(self):
        pos = self._get("time_pos")
        return -1 if pos is None or self._current_id is None else int(pos * 1000)

    def duration_ms(self):
        duration = self._get("duration")
        return -1 if duration is None or self._current_id is None else int(duration * 1000)

    def has_track(self):
        return self.duration_ms() > 0

    def remaining_ms(self):
        """Milliseconds of audio left, or -1 when unknown."""
        duration, position = self.duration_ms(), self.position_ms()
        if duration <= 0 or position < 0:
            return -1
        return max(0, duration - position)

    def finished(self):
        return self.status() in ("ended", "error")

    def seek(self, position_ms):
        try:
            self._mpv.seek(max(0, position_ms) / 1000.0, "absolute+exact")
        except Exception as e:
            logger.debug(f"seek failed: {e}")

    def wait_until_playing(self, timeout=6.0):
        """Block until audio runs; Tidal streams take a moment to buffer."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status()
            if state == "playing":
                self._check_output()
                return True
            if state == "error":
                return False
            time.sleep(0.05)
        return self.is_playing()

    def _check_output(self):
        """mpv keeps playing silently when it cannot open the audio device.
        Exclusive mode fails that way while another app holds the device
        exclusively; fall back to shared mode rather than play nothing."""
        if self._get("current_ao") or not self.exclusive:
            return
        logger.warning("Could not open the audio device exclusively; using shared mode")
        self.exclusive = False
        try:
            self._mpv.audio_exclusive = False
            self._mpv.command("ao-reload")
        except Exception as e:
            logger.debug(f"ao-reload failed: {e}")

    def output_info(self):
        """What reaches the device: the source format, mpv's output format, and
        whether nothing in between changes a sample."""
        source = self._get("audio_params", {}) or {}
        out = self._get("audio_out_params", {}) or {}
        untouched = (bool(source) and source.get("samplerate") == out.get("samplerate")
                     and source.get("format") == out.get("format")
                     and self.volume == 100 and not self.replaygain)
        # Untouched by mpv is only bit-perfect if the system mixer does not
        # resample or mix it either: exclusive WASAPI, or a PipeWire graph
        # forced to the track's rate.
        bit_perfect = untouched and (self.exclusive and bool(self._get("current_ao"))
                                     or self.force_rate)
        return {"source": source, "output": out, "bit_perfect": bit_perfect}

    def shutdown(self):
        try:
            self._mpv.terminate()
        except Exception:
            pass

    # --- linux self-unmute -----------------------------------------------
    def _unmute_own_stream(self):
        """Unmute our own sink-inputs, identified by PID only.

        Matching by name also grabbed other players, and forcing 100% volume
        overrode whatever the user set in the mixer.
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
            logger.debug(f"Self-unmute error: {e}")
