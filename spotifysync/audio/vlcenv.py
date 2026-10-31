"""VLC environment fixes that must run before libvlc is initialised."""

import os
import shutil
import sys

from ..config import IS_WINDOWS, logger


def detect_linux_backend():
    """Pick the VLC audio output module to use on Linux.

    Prefer 'pulse' over native 'pipewire': VLC's PipeWire plugin segfaults in
    audio_set_volume before a stream is active. The PulseAudio compat layer
    still routes through PipeWire (so hi-res sample-rate switching works)
    without the crash.
    """
    for backend, probe in (("pulse", "pipewire-pulse"),
                           ("pulse", "pulseaudio"),
                           ("alsa", "aplay")):
        if shutil.which(probe):
            return backend
    return None


def fix_plugin_path():
    """Restore VLC plugin discovery when running from a PyInstaller bundle.

    PyInstaller overrides LD_LIBRARY_PATH, which stops VLC finding its plugins.
    """
    orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if orig is not None:
        os.environ["LD_LIBRARY_PATH"] = orig
    elif getattr(sys, "_MEIPASS", None) and "LD_LIBRARY_PATH" in os.environ:
        del os.environ["LD_LIBRARY_PATH"]

    if "VLC_PLUGIN_PATH" not in os.environ:
        for path in ("/usr/lib64/vlc/plugins",
                     "/usr/lib/vlc/plugins",
                     "/usr/lib/x86_64-linux-gnu/vlc/plugins",
                     "/usr/lib/aarch64-linux-gnu/vlc/plugins"):
            if os.path.isdir(path):
                os.environ["VLC_PLUGIN_PATH"] = path
                break


def build_vlc_args():
    if not IS_WINDOWS:
        fix_plugin_path()

    args = [
        "--no-video",
        "--verbose=-1",
        # 1500ms was adding a second and a half to every track start. 800 still
        # rides out normal jitter on Tidal's CDN.
        "--network-caching=800",
        "--no-sout-audio",
        "--gain=1.0",
        "--no-audio-time-stretch",
    ]
    if IS_WINDOWS:
        args.append("--aout=mmdevice")
    else:
        backend = detect_linux_backend()
        if backend:
            args.append(f"--aout={backend}")
            logger.debug(f"VLC audio backend: {backend}")
        # Role "music" stops PipeWire stream-restore applying a stale mute
        # remembered against VLC's default "video" role.
        args.append("--role=music")
    return args
