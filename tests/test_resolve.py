"""`awask resolve` closes a promise as KEPT -- the verb shipped without its store method."""
from __future__ import annotations

import pytest

from awask.store import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    DecisionCard,
    DecisionError,
    DecisionSource,
    DecisionStore,
)


def _store(tmp_path):
    return DecisionStore(tmp_path / "cards.jsonl")


def _promise(store, title="ship it"):
    return store.create(DecisionCard(
        id="", title=title, kind="promise", deadline=4102444800.0,
        summary="", source=DecisionSource(agent="test", cwd="/"),
    ))


def test_resolve_closes_as_kept(tmp_path):
    store = _store(tmp_path)
    card = _promise(store)
    out = store.resolve(card.id, note="done")
    assert out.status == STATUS_ANSWERED
    assert out.answer == "kept"
    assert out.answer_note == "done"
    assert out.answered_via == "agent"
    assert store.get(card.id).status == STATUS_ANSWERED  # persisted
    assert not store.get(card.id).is_open


def test_resolve_is_not_cancel(tmp_path):
    """Cancelled reads as WITHDRAWN; a kept promise must not."""
    store = _store(tmp_path)
    kept = store.resolve(_promise(store, "a").id)
    withdrawn = store.cancel(_promise(store, "b").id)
    assert kept.status == STATUS_ANSWERED and withdrawn.status == STATUS_CANCELLED


def test_resolve_idempotent_on_closed(tmp_path):
    store = _store(tmp_path)
    card = _promise(store)
    store.cancel(card.id, note="withdrawn")
    again = store.resolve(card.id, note="kept after all")
    assert again.status == STATUS_CANCELLED  # first close wins, no overwrite


def test_resolve_unknown_card(tmp_path):
    with pytest.raises(DecisionError):
        _store(tmp_path).resolve("d-nope")
