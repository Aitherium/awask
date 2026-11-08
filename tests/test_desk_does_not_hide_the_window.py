"""A live awdesk must not stop the card window: the owner wants cards IN FRONT (2026-11-07)."""

from __future__ import annotations

from awask import notify as n
from awask.store import DecisionCard, DecisionOption


def _card() -> DecisionCard:
    return DecisionCard(id="d-test", title="Pick one", kind="decision",
                        options=[DecisionOption(key="a", label="A"),
                                 DecisionOption(key="b", label="B")],
                        default_key="a")


def _wire(monkeypatch, tmp_path, *, desk: bool, desk_only: bool):
    monkeypatch.setattr(n.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(n, "desk_alive", lambda *a, **k: desk)
    monkeypatch.setattr(n, "popup_enabled", lambda: True)
    monkeypatch.setattr(n, "native_toast", lambda *a, **k: "off in test")
    monkeypatch.setattr(n, "_webhook_post", lambda *a, **k: "off in test")
    opened = []
    monkeypatch.setattr(n, "open_card_window", lambda cid: opened.append(cid) or None)
    if desk_only:
        (tmp_path / ".aither" / "decisions").mkdir(parents=True)
        (tmp_path / ".aither" / "decisions" / ".desk-only").write_text("", encoding="utf-8")
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "store"))
    return opened


def test_live_desk_still_opens_the_window(monkeypatch, tmp_path):
    opened = _wire(monkeypatch, tmp_path, desk=True, desk_only=False)
    res = n.notify(_card())
    assert "awdesk" in res.delivered and "card window" in res.delivered
    assert opened == ["d-test"]


def test_desk_only_file_restores_one_surface(monkeypatch, tmp_path):
    opened = _wire(monkeypatch, tmp_path, desk=True, desk_only=True)
    res = n.notify(_card())
    assert res.delivered == ["awdesk"] and opened == []
    assert any("desk-only" in s for s in res.skipped)


def test_no_desk_opens_the_window(monkeypatch, tmp_path):
    opened = _wire(monkeypatch, tmp_path, desk=False, desk_only=False)
    res = n.notify(_card())
    assert "card window" in res.delivered and opened == ["d-test"]
