"""``raise_desk_inbox`` must knock on the port the desk actually bound.

Twins ``AitherOS/lib/core/desk_bridge.py``: awdesk's launcher no longer pins a
port, because a Hyper-V/WSL reserved TCP range can swallow 47931 after a reboot.
It picks a port that binds and persists it to ``%APPDATA%\\Desk\\bridge-port.txt``.

Measured 2026-11-07: the bridge was listening on 49031, every raise knocked on the
hardcoded 47931 and returned "desk bridge unreachable: WinError 10061", and the
owner's Inbox never came forward. The persisted file is the source of truth; the
constant is only the fallback when nobody ever picked.
"""

from __future__ import annotations

import urllib.request

import pytest
from awask import notify

PERSISTED_PORT = 49031


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    """A temp APPDATA with the same file awdesk's launcher writes."""
    (tmp_path / "Desk").mkdir()
    (tmp_path / "Desk" / "bridge-port.txt").write_text(f"{PERSISTED_PORT}\n", encoding="ascii")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AWDESK_URL", raising=False)
    monkeypatch.delenv("AITHERSHELL_AWDESK_URL", raising=False)
    return tmp_path


def test_url_is_built_from_the_persisted_port(appdata):
    assert str(appdata) in str(notify.desk_bridge_port_file())
    assert notify.desk_bridge_port() == PERSISTED_PORT
    assert notify.desk_bridge_url() == f"http://127.0.0.1:{PERSISTED_PORT}"


def test_explicit_env_url_still_wins(appdata, monkeypatch):
    monkeypatch.setenv("AWDESK_URL", "http://127.0.0.1:5005/")
    assert notify.desk_bridge_url() == "http://127.0.0.1:5005"


def test_an_unusable_file_falls_back_to_the_constant(appdata):
    (appdata / "Desk" / "bridge-port.txt").write_text("not-a-port", encoding="ascii")
    assert notify.desk_bridge_port() == notify.DESK_BRIDGE_DEFAULT_PORT
    assert notify.desk_bridge_url() == "http://127.0.0.1:47931"


class _Response:
    status = 200

    def read(self) -> bytes:
        return b'{"ok": true}'

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_raise_posts_to_the_persisted_port(appdata, monkeypatch):
    """The regression itself: the POST lands on 49031, not on the dead default."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("AITHER_NO_GUI_PROMPT", raising=False)
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert notify.raise_desk_inbox(card_id="d-4krg") is None
    assert len(seen) == 1
    assert seen[0].full_url == f"http://127.0.0.1:{PERSISTED_PORT}/console/open"
    assert b'"card": "d-4krg"' in (seen[0].data or b"")


def test_awsh_events_url_is_stripped_to_the_base(appdata, monkeypatch):
    """AITHERSHELL_AWDESK_URL is awsh's EVENTS url; this client needs the base."""
    monkeypatch.setenv("AITHERSHELL_AWDESK_URL", "http://127.0.0.1:49031/events")
    assert notify.desk_bridge_url() == "http://127.0.0.1:49031"
