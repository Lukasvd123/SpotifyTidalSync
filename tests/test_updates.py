"""Update check and self-update (UP*), against a fake GitHub."""

import hashlib
import os
import sys

import pytest

from spotifysync import updates

NEW = b"new build"
NEW_SHA = hashlib.sha256(NEW).hexdigest()


class Response:
    def __init__(self, status=200, json=None, text="", content=b""):
        self.status_code, self._json, self.text, self.content = status, json, text, content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, _size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeGitHub:
    def __init__(self, release=None, files=None):
        self.release, self.files = release, files or {}

    def get(self, url, **_kw):
        if url == updates.LATEST_URL:
            return Response(404) if self.release is None else Response(json=self.release)
        body = self.files[url]
        return Response(text=body.decode(), content=body)


def release_json(tag, digest=True, sums=False):
    assets = [{"name": "SpotifySync.exe", "browser_download_url": "https://dl/exe",
               "digest": f"sha256:{NEW_SHA}" if digest else None},
              {"name": "SpotifySync-x86_64.AppImage", "browser_download_url": "https://dl/appimage",
               "digest": f"sha256:{NEW_SHA}" if digest else None}]
    if sums:
        assets.append({"name": "SHA256SUMS", "browser_download_url": "https://dl/sums"})
    return {"tag_name": tag, "html_url": "https://github.com/x/releases/tag/" + tag,
            "body": "notes", "assets": assets}


@pytest.mark.parametrize("candidate,current,newer", [
    ("0.06", "0.05", True), ("v0.10", "0.9", True), ("0.05", "0.05", False),
    ("0.04", "0.05", False), ("1.0", "0.99", True),
])
def test_UP1_version_comparison(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


def test_UP2_no_release_or_same_version_is_not_offered(monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "0.06")
    assert updates.check(FakeGitHub(None)) is None
    assert updates.check(FakeGitHub(release_json("v0.06"))) is None


def test_UP3_newer_release_carries_checksums(monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "0.05")
    release = updates.check(FakeGitHub(release_json("v0.06")))
    assert release.version == "0.06"
    assert release.assets["SpotifySync.exe"] == ("https://dl/exe", NEW_SHA)


def test_UP4_checksums_fall_back_to_sha256sums(monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "0.05")
    sums = f"{NEW_SHA}  SpotifySync.exe\n{NEW_SHA} *SpotifySync-x86_64.AppImage\n".encode()
    gh = FakeGitHub(release_json("v0.06", digest=False, sums=True), {"https://dl/sums": sums})
    release = updates.check(gh)
    assert release.assets["SpotifySync-x86_64.AppImage"][1] == NEW_SHA


def test_UP5_only_exe_and_appimage_update_themselves(monkeypatch):
    release = updates.parse_release(release_json("v0.06"))
    monkeypatch.setattr(updates, "install_kind", lambda: "appimage")
    assert updates.can_self_update(release)
    for kind in ("package", "source"):
        monkeypatch.setattr(updates, "install_kind", lambda kind=kind: kind)
        assert not updates.can_self_update(release)
    unsigned = updates.parse_release(release_json("v0.06", digest=False))
    monkeypatch.setattr(updates, "install_kind", lambda: "exe")
    assert not updates.can_self_update(unsigned)


def test_UP6_tampered_download_is_rejected_and_nothing_replaced(tmp_path, monkeypatch):
    app = tmp_path / "SpotifySync-x86_64.AppImage"
    app.write_bytes(b"old build")
    monkeypatch.setenv("APPIMAGE", str(app))
    release = updates.parse_release(release_json("v0.06"))
    gh = FakeGitHub(files={"https://dl/appimage": b"evil build"})
    with pytest.raises(ValueError, match="checksum"):
        updates.install(release, gh)
    assert app.read_bytes() == b"old build"
    assert sorted(os.listdir(tmp_path)) == [app.name]


def test_UP7_appimage_is_replaced_in_place(tmp_path, monkeypatch):
    app = tmp_path / "SpotifySync-x86_64.AppImage"
    app.write_bytes(b"old build")
    monkeypatch.setenv("APPIMAGE", str(app))
    release = updates.parse_release(release_json("v0.06"))
    command = updates.install(release, FakeGitHub(files={"https://dl/appimage": NEW}))
    assert app.read_bytes() == NEW and os.access(app, os.X_OK)
    assert command == [str(app), "--after-update"]


def test_UP8_running_exe_is_renamed_then_replaced(tmp_path, monkeypatch):
    exe = tmp_path / "SpotifySync.exe"
    exe.write_bytes(b"old build")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(updates, "install_kind", lambda: "exe")
    monkeypatch.setattr(sys, "executable", str(exe))
    release = updates.parse_release(release_json("v0.06"))
    updates.install(release, FakeGitHub(files={"https://dl/exe": NEW}))
    assert exe.read_bytes() == NEW
    assert (tmp_path / "SpotifySync.exe.old").read_bytes() == b"old build"
    updates.cleanup_previous()
    assert not (tmp_path / "SpotifySync.exe.old").exists()
