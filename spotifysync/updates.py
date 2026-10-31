"""Update check against GitHub Releases, and in-place updates where possible.

The .exe and the AppImage replace themselves after verifying the download's
SHA-256 against the release; deb/rpm installs and source checkouts are pointed
at the release page instead (they need the package manager or git).
"""

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from . import APP_VERSION
from .config import IS_WINDOWS, logger

REPO = "Lukasvd123/SpotifyTidalSync"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_INTERVAL_S = 24 * 3600

ASSETS = {"exe": "SpotifySync.exe", "appimage": "SpotifySync-x86_64.AppImage"}


@dataclass
class Release:
    version: str
    page: str
    notes: str = ""
    assets: dict = field(default_factory=dict)   # name -> (url, sha256 or "")


def parse_version(text):
    """"v0.06" -> (0, 6). A pre-release suffix ("-rc1") is ignored, so it never
    outranks the final release."""
    return tuple(int(n) for n in re.findall(r"\d+", (text or "").split("-")[0]))


def is_newer(candidate, current=None):
    return parse_version(candidate) > parse_version(current or APP_VERSION)


def install_kind():
    """exe | appimage | package | source"""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if getattr(sys, "frozen", False):
        return "exe" if IS_WINDOWS else "package"
    return "source"


def parse_release(data):
    """Release from the GitHub API's JSON. Checksums come from each asset's
    digest, or from a SHA256SUMS file published with the release."""
    assets = {}
    for asset in data.get("assets") or []:
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        assets[asset.get("name")] = (asset.get("browser_download_url"), sha)
    return Release(version=(data.get("tag_name") or "").lstrip("v"),
                   page=data.get("html_url") or RELEASES_PAGE,
                   notes=data.get("body") or "", assets=assets)


def parse_sums(text):
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0]
    return sums


def check(session=None):
    """The latest release if it is newer than this build, else None."""
    import requests
    http = session or requests
    response = http.get(LATEST_URL, timeout=8,
                        headers={"Accept": "application/vnd.github+json"})
    if response.status_code == 404:
        return None     # no releases yet
    response.raise_for_status()
    release = parse_release(response.json())
    if not is_newer(release.version):
        return None
    if "SHA256SUMS" in release.assets:
        try:
            sums = parse_sums(http.get(release.assets["SHA256SUMS"][0], timeout=8).text)
            for name, (url, sha) in list(release.assets.items()):
                release.assets[name] = (url, sha or sums.get(name, ""))
        except Exception as e:
            logger.debug(f"SHA256SUMS unavailable: {e}")
    return release


def can_self_update(release):
    kind = install_kind()
    name = ASSETS.get(kind)
    return bool(name and name in release.assets and release.assets[name][1])


def download_verified(url, expected, folder, session=None):
    import requests
    http = session or requests
    fd, path = tempfile.mkstemp(dir=folder, prefix=".spotifysync-update-")
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out, http.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            for chunk in response.iter_content(1 << 20):
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"checksum mismatch: expected {expected}, got {digest.hexdigest()}")
        return path
    except Exception:
        os.unlink(path)
        raise


def install(release, session=None):
    """Replace the running .exe or AppImage with the release's and return the
    command that starts the new one. The caller shuts down, then runs it."""
    kind = install_kind()
    url, expected = release.assets[ASSETS[kind]]
    target = os.environ["APPIMAGE"] if kind == "appimage" else sys.executable
    folder = os.path.dirname(target)
    new = download_verified(url, expected, folder, session)
    if kind == "exe":
        # A running .exe cannot be overwritten, but it can be renamed.
        old = target + ".old"
        if os.path.exists(old):
            os.remove(old)
        os.replace(target, old)
        os.replace(new, target)
    else:
        os.chmod(new, 0o755)
        os.replace(new, target)
    logger.info(f"Updated to {release.version}; restarting")
    return [target, "--after-update"]


def cleanup_previous():
    """Remove the .exe left behind by the last update."""
    if install_kind() == "exe":
        try:
            os.remove(sys.executable + ".old")
        except OSError:
            pass


def relaunch(command):
    kwargs = {"close_fds": True}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # detached, new group
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)
