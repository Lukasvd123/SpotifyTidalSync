"""UI smoke test (UI*): build every window against a stub engine. Skipped
without a display."""

from types import SimpleNamespace as NS

import pytest

tk = pytest.importorskip("tkinter")


class StubEngine:
    def __init__(self):
        self.calls = []
        self.player = NS(position_ms=lambda: 1000, duration_ms=lambda: 200000,
                         get_audio_devices=lambda: [("Speakers", "dev1")],
                         set_device=lambda d: None)
        self.source = NS(api_available=False)
        self.library = NS(is_pkce=False)
        self.current = NS(has_track=True, title="Song", artist="Artist")

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
            return []
        return record


@pytest.fixture
def app():
    try:
        from spotifysync.ui import MainApp
        window = MainApp(StubEngine())
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    window.withdraw()
    yield window
    window.destroy()


def test_UI1_snapshot_renders(app):
    app._apply({"state": "playing", "status": "Playing: Song [LOSSLESS]", "track": "Song",
                "position_ms": 1000, "duration_ms": 200000, "art_url": "", "source_track": "",
                "source_app": "spotify", "api": False, "quality": "LOSSLESS",
                "match": "search (140)", "low_confidence": True})
    app.update_idletasks()
    assert app.match_label.cget("text") == "Matched by search (140)"
    assert app.fix_button.cget("bg") == "#aa3333"


def test_UI2_buttons_queue_engine_commands(app):
    app.event_generate("<space>")
    app.engine.next_track()
    app.volume.set(30)
    app.update()
    names = [c[0] for c in app.engine.calls]
    assert "next_track" in names


def test_UI3_match_popup_opens_once(app):
    app._prompt_match(app.engine.current)
    first = app._match_window
    app._prompt_match(app.engine.current)
    assert app._match_window is first
    first.not_on_tidal()
    assert ("mark_not_on_tidal", (app.engine.current,)) in app.engine.calls


def test_UI4_settings_window_builds(app):
    from spotifysync.ui.settings_window import SettingsWindow
    window = SettingsWindow(app, app.engine)
    app.update()
    window.destroy()


def test_UI5_tidal_login_dialog_needs_the_code_address(app):
    from spotifysync.ui.tidal_login import TidalLoginWindow
    results = []
    win = TidalLoginWindow(app, "https://login.tidal.com/authorize?x", results.append)
    win.entry.insert(0, "https://tidal.com/")
    win._submit()
    assert results == [] and "no login code" in win.error.cget("text")
    win.entry.delete(0, "end")
    win.entry.insert(0, " https://tidal.com/android/login/auth?code=abc ")
    win._submit()
    assert results == ["https://tidal.com/android/login/auth?code=abc"]
