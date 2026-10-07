"""Stragglers and context cards (owner, 2026-10-07).

Measured that day: 30 open cards, the oldest 27 days, and the queue window
opened on that one every time; an hourly digest ("Nothing to click -- this is
context") sat open as "30 of 30" and popped like a decision.

The rules pinned here:
  * an ``info`` card with no deadline closes itself after the info TTL;
  * an old DECISION is never closed for being old (sweep()'s principle);
  * the interrupting queue shows fresh cards first and stragglers after them,
    oldest-first within each group, and drops nothing.
"""
from __future__ import annotations

import json
import time

from awask import STATUS_OPEN, DecisionCard, DecisionOption, DecisionStore
from awask.store import STATUS_EXPIRED, is_straggler, queue_order

DAY = 86400.0
SHIP = DecisionOption(key="ship", label="Ship it", consequence="live in ~2 min")
HOLD = DecisionOption(key="hold", label="Hold", consequence="blocks the release")


def _decision(title: str = "Ship it?") -> DecisionCard:
    return DecisionCard(id="", title=title, options=[SHIP, HOLD], default_key="hold")


def _info(title: str = "Hourly digest") -> DecisionCard:
    # Info cards carry no options (the store refuses them): "Got it -- dismiss" is
    # the window's own button, not an option.
    return DecisionCard(id="", kind="info", title=title)


def _age(store: DecisionStore, card: DecisionCard, seconds: float) -> None:
    """Backdate a stored card (the store stamps created_at on create)."""
    path = store.path / f"{card.id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["created_at"] = time.time() - seconds
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_an_old_info_card_closes_itself_and_says_why(tmp_path):
    store = DecisionStore(tmp_path)
    old = store.create(_info())
    _age(store, old, 7 * 3600)
    assert [c.id for c in store.list()] == []          # gone from the open queue
    closed = store.get(old.id)
    assert closed.status == STATUS_EXPIRED
    assert closed.answered_via == "info-ttl"
    assert "never a question" in closed.answer_note


def test_a_fresh_info_card_stays_open(tmp_path):
    store = DecisionStore(tmp_path)
    fresh = store.create(_info())
    _age(store, fresh, 3600)
    assert [c.id for c in store.list()] == [fresh.id]


def test_the_info_ttl_is_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_INFO_TTL_HOURS", "0.5")
    store = DecisionStore(tmp_path)
    card = store.create(_info())
    _age(store, card, 3600)
    assert store.list() == []


def test_an_old_decision_is_never_closed_for_being_old(tmp_path):
    store = DecisionStore(tmp_path)
    old = store.create(_decision())
    _age(store, old, 30 * DAY)
    [still] = store.list()
    assert still.id == old.id and still.status == STATUS_OPEN


def test_the_queue_leads_with_fresh_cards_and_keeps_every_straggler(tmp_path):
    store = DecisionStore(tmp_path)
    ancient = store.create(_decision("ancient"))
    stale = store.create(_decision("stale"))
    older_fresh = store.create(_decision("older fresh"))
    newest = store.create(_decision("newest"))
    _age(store, ancient, 27 * DAY)
    _age(store, stale, 9 * DAY)
    _age(store, older_fresh, 2 * DAY)
    _age(store, newest, 60)
    cards = store.list()                                  # oldest-first, as designed
    assert [c.title for c in cards] == ["ancient", "stale", "older fresh", "newest"]
    ordered = queue_order(cards)
    assert [c.title for c in ordered] == ["older fresh", "newest", "ancient", "stale"]
    assert sorted(c.id for c in ordered) == sorted(c.id for c in cards)
    assert [is_straggler(c) for c in ordered] == [False, False, True, True]


def test_the_staleness_line_is_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_STALE_DAYS", "1")
    store = DecisionStore(tmp_path)
    card = store.create(_decision())
    _age(store, card, 2 * DAY)
    assert is_straggler(store.get(card.id))
