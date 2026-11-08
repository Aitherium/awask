"""The credential ladder must name the CARD it brings forward, and admit failure.

awask is generated from ``adk/decisions``; on 2026-11-07 the shipped copy still
called ``raise_desk_inbox()`` with no card id, so the desk raised its Inbox on
whatever it showed last (the owner's phone said "the secure prompt is open on
your PC" while the PC showed nothing), and a failed raise still reported a
"desk" surface. Both are the owner-visible half of "the link opens nothing
useful", so they are pinned HERE, in the package that actually ships.
"""

from __future__ import annotations

import pytest
from awask import notify
from awask.secure_prompt import open_credential_surface
from awask.store import DecisionCard

CARD_ID = "d-4krg"


def _card():
    return DecisionCard(id=CARD_ID, title="GitHub token", kind="credential",
                        secret_name="GITHUB_TOKEN")


@pytest.fixture
def desk(monkeypatch):
    """awdesk is up; record every raise instead of dialling the bridge."""
    seen = []

    def raise_inbox(*args, **kwargs):
        seen.append((args, kwargs))
        return None

    monkeypatch.setattr(notify, "desk_alive", lambda: True)
    monkeypatch.setattr(notify, "raise_desk_inbox", raise_inbox)
    return seen


def test_raise_names_the_card(desk):
    surface = open_credential_surface(_card(), owner_initiated=True)
    assert surface.surface == "desk"
    assert desk == [((), {"card_id": CARD_ID})], (
        "without the id the desk opens whatever Inbox row it showed last")


def test_a_failed_raise_is_not_reported_as_desk(desk, monkeypatch):
    monkeypatch.setattr(notify, "raise_desk_inbox",
                        lambda *a, **k: "desk bridge unreachable: WinError 10061")
    surface = open_credential_surface(_card(), owner_initiated=True)
    assert surface.surface == "none", "a refusal must not read as 'the field is on your PC'"
    assert "WinError 10061" in surface.detail


def test_a_desk_started_on_demand_also_names_the_card(monkeypatch):
    seen = []
    monkeypatch.setattr(notify, "desk_alive", lambda: False)
    monkeypatch.setattr(notify, "raise_desk_inbox",
                        lambda *a, **k: seen.append((a, k)) or None)
    surface = open_credential_surface(
        _card(), owner_initiated=True, start=lambda: (True, "started awdesk"))
    assert surface.surface == "desk"
    assert seen == [((), {"card_id": CARD_ID})]
