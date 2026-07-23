"""Tests for the local Flask CSV editor's startup path (webserver.py).

Deliberately hermetic: `make_server` is stubbed in every bind test, so
nothing here opens a real socket. Binding for real would make the suite
depend on which ports happen to be free — and on Windows, on which ranges
Hyper-V/WSL have reserved this boot, which is the very failure these
tests exist to pin down.
"""

import pytest

pytest.importorskip("flask")
pytest.importorskip("werkzeug")

import werkzeug.serving  # noqa: E402

from green_tracker import webserver  # noqa: E402
from green_tracker.webserver import PORT, CsvEditorServer  # noqa: E402


class _FakeServer:
    """Stand-in for werkzeug's WSGIServer: records the port it 'bound'."""

    def __init__(self, port):
        self.server_port = port
        self.served = False

    def serve_forever(self):
        self.served = True


def _server():
    return CsvEditorServer(read_rows=lambda: [], write_rows=lambda rows: None)


@pytest.fixture
def make_server_calls(monkeypatch):
    """Replace make_server with a scripted stub; return the call log.

    `_ensure_started` imports make_server from werkzeug.serving at call
    time, so patching the source module is what the local import picks up.
    """
    calls = []

    def _install(behaviour):
        # Reset, so a test that re-scripts the stub mid-way (e.g. retry
        # after a failure) asserts on that phase's calls alone.
        calls.clear()

        def fake(host, port, app, **kwargs):
            calls.append(port)
            return behaviour(port)
        monkeypatch.setattr(werkzeug.serving, "make_server", fake)
        return calls

    return _install


# ---------------------------------------------------------------------------
# Port choice
# ---------------------------------------------------------------------------

class TestPreferredPort:
    def test_default_port_is_outside_the_ephemeral_range(self):
        """Regression guard for the ERR_CONNECTION_REFUSED bug.

        The old default (49377) sat inside the IANA ephemeral range, which
        Windows carves into reserved exclusion ranges that are assigned
        dynamically at boot. Binding inside one fails with WinError 10013,
        so a port that worked yesterday can refuse today with no code
        change. Anything >= 49152 is exposed to that and must not be the
        default.
        """
        assert PORT < 49152

    def test_preferred_port_is_used_when_it_binds(self, make_server_calls):
        calls = make_server_calls(lambda port: _FakeServer(port))
        srv = _server()
        srv._ensure_started()

        assert calls == [PORT], "should not fall back when the port binds"
        assert srv._port == PORT


# ---------------------------------------------------------------------------
# Fallback to an OS-assigned port
# ---------------------------------------------------------------------------

class TestBindFallback:
    def test_falls_back_to_os_assigned_port(self, make_server_calls):
        """A refused preferred port must degrade, not fail."""
        def behaviour(port):
            if port == PORT:
                raise OSError(
                    "[WinError 10013] An attempt was made to access a socket "
                    "in a way forbidden by its access permissions"
                )
            return _FakeServer(54321)

        calls = make_server_calls(behaviour)
        srv = _server()
        srv._ensure_started()

        assert calls == [PORT, 0], "second attempt must ask the OS (port 0)"
        assert srv._port == 54321, "must adopt the port actually bound"
        assert srv._started is True

    def test_browser_url_matches_the_port_actually_bound(
        self, make_server_calls, monkeypatch,
    ):
        """The whole point of the fallback: the URL must follow the bind.

        A fallback that bound port 54321 but still opened the preferred
        port would reproduce the original bug exactly.
        """
        def behaviour(port):
            if port == PORT:
                raise OSError("refused")
            return _FakeServer(54321)

        make_server_calls(behaviour)
        opened = []
        monkeypatch.setattr(webserver.webbrowser, "open", opened.append)

        srv = _server()
        srv.open_in_browser()

        assert opened == ["http://127.0.0.1:54321/"]

    def test_url_uses_preferred_port_when_no_fallback(
        self, make_server_calls, monkeypatch,
    ):
        make_server_calls(lambda port: _FakeServer(port))
        opened = []
        monkeypatch.setattr(webserver.webbrowser, "open", opened.append)

        srv = _server()
        srv.open_in_browser()

        assert opened == [f"http://127.0.0.1:{PORT}/"]


# ---------------------------------------------------------------------------
# Failure is surfaced, not swallowed
# ---------------------------------------------------------------------------

class TestStartupFailureIsVisible:
    def test_total_bind_failure_raises(self, make_server_calls):
        """Both attempts failing must reach the caller.

        The original bug bound inside the server thread, so the OSError
        died with the thread and the user got a browser tab pointed at
        nothing. The caller can only show an error if it sees one.
        """
        def behaviour(port):
            raise OSError("no ports available")

        make_server_calls(behaviour)
        srv = _server()
        with pytest.raises(OSError):
            srv._ensure_started()

    def test_failed_start_is_retryable(self, make_server_calls):
        """A failed start must not latch `_started`.

        `_started` used to be set unconditionally, so once the bind failed
        every later attempt silently no-opped for the rest of the session.
        """
        def failing(port):
            raise OSError("no ports available")

        make_server_calls(failing)
        srv = _server()
        with pytest.raises(OSError):
            srv._ensure_started()
        assert srv._started is False

        # A later attempt, once the port is free again, must actually try.
        calls = make_server_calls(lambda port: _FakeServer(port))
        srv._ensure_started()
        assert calls == [PORT]
        assert srv._started is True

    def test_start_is_idempotent(self, make_server_calls):
        calls = make_server_calls(lambda port: _FakeServer(port))
        srv = _server()
        srv._ensure_started()
        srv._ensure_started()
        assert calls == [PORT], "second call must not re-bind"


# ---------------------------------------------------------------------------
# Undo glyph (web-editor surface)
# ---------------------------------------------------------------------------

class TestUndoGlyphInPage:
    def test_page_renders_the_unicode_undo_glyph(self):
        assert "&#x21A9;" in webserver._HTML

    def test_old_inline_svg_icon_is_gone(self):
        """The swap must replace the icon, not sit alongside it."""
        assert "svg+xml" not in webserver._HTML
        assert "button.icon-btn img" not in webserver._HTML

    def test_glyph_keeps_the_icon_button_styling(self):
        """Sizing/colour carried over from the <img> it replaced."""
        assert "undo-glyph" in webserver._HTML
        assert "button.icon-btn:disabled" in webserver._HTML
