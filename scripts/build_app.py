#!/usr/bin/env python3
"""Build SpotifySync for this platform into dist/.

  windows:  dist/SpotifySync.exe              libmpv bundled
  appimage: dist/SpotifySync-x86_64.AppImage
  deb:      dist/spotifysync_<ver>_amd64.deb
  rpm:      dist/spotifysync-<ver>.x86_64.rpm
  binary:   dist/SpotifySync                   single Linux binary

PyInstaller bundles the build machine's libmpv and ffmpeg; the sound server
client libraries are left to the system. Build Linux packages on the oldest
distro they should run on (CI uses Ubuntu 22.04).

Third-party downloads are pinned and checked against their SHA-256.
"""

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "_build"
DIST = ROOT / "dist"
CACHE = BUILD / "downloads"

LIBMPV = (
    "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20261021/"
    "mpv-dev-x86_64-20261021-git-e471496c56.7z",
    "efc719eff3dc5954b4a5efc800632808bcec01cc9034940dd14e5eb7fe9ea633",
)
APPIMAGETOOL = (
    "https://github.com/AppImage/appimagetool/releases/download/1.9.1/"
    "appimagetool-x86_64.AppImage",
    "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0",
)

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=SpotifySync
GenericName=Music Player
Comment=Play Spotify's queue through Tidal's lossless and hi-res streams
Exec=SpotifySync
Icon=spotifysync
Terminal=false
Categories=AudioVideo;Audio;Player;
Keywords=spotify;tidal;music;hifi;flac;
StartupWMClass=spotifysync
"""

RPM_SPEC = """Name: spotifysync
Version: {version}
Release: 1
Summary: Play Spotify's queue through Tidal's lossless and hi-res streams
License: LicenseRef-OFL-1.0
URL: https://github.com/Lukasvd123/SpotifyTidalSync
Requires: alsa-lib, pulseaudio-libs, pipewire-libs, libglvnd-egl, libglvnd-glx, libglvnd-opengl, libdrm, libwayland-client, libwayland-cursor, libwayland-egl, libxcb
AutoReqProv: no
%define __strip /bin/true
%define _build_id_links none
%define debug_package %{{nil}}

%description
Spotify plays muted as the queue; SpotifySync plays each track from Tidal.

%install
cp -a {root}/. %{{buildroot}}/

%files
/opt/spotifysync
/usr/bin/spotifysync
/usr/share/applications/spotifysync.desktop
/usr/share/icons/hicolor/512x512/apps/spotifysync.png
/usr/share/icons/hicolor/scalable/apps/spotifysync.svg
"""

APPRUN = """#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/spotifysync/SpotifySync" "$@"
"""


def version():
    text = (ROOT / "spotifysync" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'APP_VERSION = "([^"]+)"', text).group(1)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, expected):
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / url.rsplit("/", 1)[-1]
    if target.exists() and sha256(target) == expected:
        return target
    print(f"Downloading {url}")
    partial = target.with_suffix(target.suffix + ".part")
    urllib.request.urlretrieve(url, partial)
    actual = sha256(partial)
    if actual != expected:
        partial.unlink()
        sys.exit(f"Checksum mismatch for {target.name}: expected {expected}, got {actual}")
    partial.replace(target)
    return target


def extract_7z(archive, member, out):
    """The mpv archives use the BCJ2 filter, which py7zr cannot unpack."""
    out.mkdir(parents=True, exist_ok=True)
    candidates = [shutil.which(n) for n in ("7z", "7za", "7zz")]
    candidates.append(os.path.join(os.environ.get("ProgramFiles", ""), "7-Zip", "7z.exe"))
    for tool in candidates:
        if tool and os.path.isfile(tool):
            subprocess.run([tool, "e", "-y", f"-o{out}", str(archive), member],
                           check=True, stdout=subprocess.DEVNULL)
            return
    sys.exit("7-Zip is needed to unpack libmpv: install it from https://www.7-zip.org/ "
             "(or 'winget install 7zip.7zip') and run the build again.")


def fetch_libmpv():
    """libmpv-2.dll into lib/, where both the build and a source checkout find it."""
    dll = ROOT / "lib" / "libmpv-2.dll"
    archive = download(*LIBMPV)
    marker = dll.with_suffix(".sha256")
    if dll.exists() and marker.exists() and marker.read_text() == LIBMPV[1]:
        return dll
    dll.parent.mkdir(exist_ok=True)
    out = CACHE / "libmpv"
    shutil.rmtree(out, ignore_errors=True)
    extract_7z(archive, "libmpv-2.dll", out)
    shutil.copy2(out / "libmpv-2.dll", dll)
    marker.write_text(LIBMPV[1])
    print(f"libmpv: {dll}")
    return dll


def pyinstaller(onefile, extra=()):
    sep = os.pathsep
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--noconsole", "--onefile" if onefile else "--onedir",
        "--name", "SpotifySync",
        "--distpath", str(BUILD / "dist"),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        "--icon", str(ROOT / "assets" / "logo.ico"),
        "--add-data", f"{ROOT / 'assets' / 'logo.png'}{sep}assets",
        *extra,
        str(ROOT / "spotify.py"),
    ]
    subprocess.run(args, check=True, cwd=ROOT)


# Sound server clients must match the running daemon and load its plugins from
# system paths: an old bundled libpipewire next to a newer PipeWire is a
# classic cause of no sound. Every Linux desktop has these, so use the system's.
SYSTEM_AUDIO_LIBS = ("libpipewire-", "libpulse", "libasound.")


def onedir():
    pyinstaller(onefile=False)
    app = BUILD / "dist" / "SpotifySync"
    for lib in (app / "_internal").iterdir():
        if lib.name.startswith(SYSTEM_AUDIO_LIBS):
            lib.unlink()
    return app


def build_windows():
    dll = fetch_libmpv()
    pyinstaller(onefile=True, extra=["--add-binary", f"{dll}{os.pathsep}."])
    DIST.mkdir(exist_ok=True)
    out = DIST / "SpotifySync.exe"
    shutil.copy2(BUILD / "dist" / "SpotifySync.exe", out)
    return out


def build_binary():
    pyinstaller(onefile=True)
    DIST.mkdir(exist_ok=True)
    out = DIST / "SpotifySync"
    shutil.copy2(BUILD / "dist" / "SpotifySync", out)
    return out


def build_appimage():
    app = onedir()
    appdir = BUILD / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(app, appdir / "usr" / "lib" / "spotifysync", symlinks=True)

    (appdir / "AppRun").write_text(APPRUN)
    (appdir / "spotifysync.desktop").write_text(DESKTOP_ENTRY)
    shutil.copy2(ROOT / "assets" / "logo.png", appdir / "spotifysync.png")
    icons = appdir / "usr" / "share" / "icons" / "hicolor"
    (icons / "512x512" / "apps").mkdir(parents=True)
    (icons / "scalable" / "apps").mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "logo.png", icons / "512x512" / "apps" / "spotifysync.png")
    shutil.copy2(ROOT / "assets" / "logo.svg", icons / "scalable" / "apps" / "spotifysync.svg")
    for path in (appdir / "AppRun",):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    tool = download(*APPIMAGETOOL)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    DIST.mkdir(exist_ok=True)
    out = DIST / "SpotifySync-x86_64.AppImage"
    # Extract-and-run: CI runners and many desktops have no FUSE 2.
    env = dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1", ARCH="x86_64")
    subprocess.run([str(tool), "--no-appstream", str(appdir), str(out)], check=True, env=env)
    return out


def _system_tree():
    """/opt/spotifysync plus menu entry and icons, shared by the deb and rpm."""
    app = onedir()
    tree = BUILD / "pkgroot"
    shutil.rmtree(tree, ignore_errors=True)
    shutil.copytree(app, tree / "opt" / "spotifysync", symlinks=True)
    (tree / "usr" / "bin").mkdir(parents=True)
    os.symlink("/opt/spotifysync/SpotifySync", tree / "usr" / "bin" / "spotifysync")
    apps = tree / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "spotifysync.desktop").write_text(
        DESKTOP_ENTRY.replace("Exec=SpotifySync", "Exec=/usr/bin/spotifysync"))
    icons = tree / "usr" / "share" / "icons" / "hicolor"
    (icons / "512x512" / "apps").mkdir(parents=True)
    (icons / "scalable" / "apps").mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "logo.png", icons / "512x512" / "apps" / "spotifysync.png")
    shutil.copy2(ROOT / "assets" / "logo.svg", icons / "scalable" / "apps" / "spotifysync.svg")
    return tree


def build_deb():
    tree = _system_tree()
    control = tree / "DEBIAN"
    control.mkdir()
    size_kb = sum(f.stat().st_size for f in tree.rglob("*") if f.is_file()) // 1024
    (control / "control").write_text(
        f"Package: spotifysync\nVersion: {version()}\nArchitecture: amd64\n"
        "Maintainer: Lukas <noreply@github.com>\n"
        "Depends: libc6 (>= 2.35), libasound2 | libasound2t64, libpulse0, "
        "libpipewire-0.3-0 | libpipewire-0.3-0t64, libegl1, libgl1, libopengl0, libdrm2, "
        "libwayland-client0, libwayland-cursor0, libwayland-egl1, libxcb1, libxcb-dri3-0\n"
        f"Installed-Size: {size_kb}\nSection: sound\nPriority: optional\n"
        "Description: Play Spotify's queue through Tidal's lossless and hi-res streams\n")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"spotifysync_{version()}_amd64.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(tree), str(out)], check=True)
    return out


def build_rpm():
    tree = _system_tree()
    top = BUILD / "rpmbuild"
    shutil.rmtree(top, ignore_errors=True)
    (top / "SPECS").mkdir(parents=True)
    spec = top / "SPECS" / "spotifysync.spec"
    spec.write_text(RPM_SPEC.format(version=version(), root=tree))
    subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)], check=True)
    built = next((top / "RPMS").rglob("*.rpm"))
    DIST.mkdir(exist_ok=True)
    out = DIST / built.name
    shutil.copy2(built, out)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=["windows", "appimage", "deb", "rpm", "binary", "libmpv"])
    target = parser.parse_args().target
    builder = {"windows": build_windows, "appimage": build_appimage, "deb": build_deb,
               "rpm": build_rpm, "binary": build_binary, "libmpv": fetch_libmpv}[target]
    out = builder()
    print(f"Built {out}  sha256 {sha256(out)}")


if __name__ == "__main__":
    main()
