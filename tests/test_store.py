"""The store must actually store, answer, and deliver — not merely refuse.

Every assertion here is a POSITIVE round trip. A fail-closed store that returns
nothing on every path passes every "raises on bad input" test trivially, and
that is precisely the shape of an inert feature: the CLI exits 0, the card list
is empty, and "no decisions are waiting" is indistinguishable from "the store
never wrote anything". So each refusal below is paired with the happy path it is
supposed to be guarding.

The store is pointed at a tmp_path throughout. It defaults to ~/.aither/decisions,
and a suite that wrote there would answer the developer's own real cards.
"""
from __future__ import annotations

import json

import pytest
from awask import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_OPEN,
    DecisionCard,
    DecisionOption,
    DecisionStore,
)
from awask.store import DecisionError

SHIP = DecisionOption(key="ship", label="Ship it", consequence="live in ~2 min")
HOLD = DecisionOption(key="hold", label="Hold", consequence="blocks the release")


def _card(**kw) -> DecisionCard:
    """A VALID decision card. The store refuses a decision card with no options
    and no default_key, so a helper that omitted them would make every test here
    a test of the validator instead of a test of the store."""
    kw.setdefault("id", "")
    kw.setdefault("title", "Ship the migration now, or hold for review?")
    if kw.get("kind", "decision") == "decision":
        kw.setdefault("options", [SHIP, HOLD])
        kw.setdefault("default_key", "hold")
    return DecisionCard(**kw)


# ── the happy path, first, because it is the one that can silently not happen ──

def test_a_created_card_is_readable_back_by_a_second_store(tmp_path):
    """Durable and out-of-process is rule 1 of the package docstring."""
    written = DecisionStore(tmp_path).create(_card(summary="Tests pass."))
    assert written.id, "create() must mint an id"

    # A DIFFERENT store object, as another process would have.
    read = DecisionStore(tmp_path).get(written.id)
    assert read is not None, "the card did not survive being written to disk"
    assert read.title == written.title
    assert read.summary == "Tests pass."
    assert read.status == STATUS_OPEN
    assert [o.key for o in read.options] == ["ship", "hold"]


def test_list_returns_the_open_card(tmp_path):
    store = DecisionStore(tmp_path)
    created = store.create(_card())
    ids = [c.id for c in store.list()]
    assert created.id in ids, f"created card is missing from list(): {ids}"


def test_answering_records_the_choice_and_closes_the_card(tmp_path):
    store = DecisionStore(tmp_path)
    card = store.create(_card())

    answered = store.answer(card.id, "ship", deliver=False)
    assert answered.status == STATUS_ANSWERED
    assert answered.answer == "ship"

    # and it is durable, not merely returned
    assert DecisionStore(tmp_path).get(card.id).answer == "ship"


def test_steer_leaves_the_card_open(tmp_path):
    """Free-form guidance must NOT close the card — a steer is not an answer."""
    store = DecisionStore(tmp_path)
    card = store.create(_card())
    store.steer(card.id, "do the other thing first", via="test")
    assert DecisionStore(tmp_path).get(card.id).status == STATUS_OPEN


def test_cancel_closes_without_an_answer(tmp_path):
    store = DecisionStore(tmp_path)
    card = store.create(_card())
    store.cancel(card.id, note="no longer needed")
    reread = DecisionStore(tmp_path).get(card.id)
    assert reread.status == STATUS_CANCELLED
    assert reread.answer is None


def test_an_info_card_needs_no_options(tmp_path):
    """The paired positive for the two 'decision cards must have options' rules."""
    card = DecisionStore(tmp_path).create(
        _card(kind="info", title="The migration finished.")
    )
    assert DecisionStore(tmp_path).get(card.id).kind == "info"


# ── the refusals, each guarding one of the round trips above ──────────────────

def test_answering_twice_loses_rather_than_overwrites(tmp_path):
    """Compare-and-set: two surfaces racing produce one winner and one clear loser."""
    store = DecisionStore(tmp_path)
    card = store.create(_card())
    store.answer(card.id, "ship", deliver=False)
    with pytest.raises(DecisionError):
        store.answer(card.id, "hold", deliver=False)


def test_an_unknown_option_is_refused_but_a_known_one_is_not(tmp_path):
    store = DecisionStore(tmp_path)
    card = store.create(_card())
    with pytest.raises(DecisionError):
        store.answer(card.id, "launch", deliver=False)
    # the paired positive: the card is still answerable with a real key
    assert store.answer(card.id, "ship", deliver=False).answer == "ship"


def test_a_card_id_cannot_traverse_out_of_the_store(tmp_path):
    with pytest.raises(DecisionError):
        DecisionStore(tmp_path).get("../../etc/passwd")


def test_a_titleless_card_is_refused(tmp_path):
    with pytest.raises(DecisionError):
        DecisionStore(tmp_path).create(_card(title="   "))


def test_options_without_a_default_are_refused(tmp_path):
    """'What happens if the owner never answers' is what makes a card safe to
    ignore. Storing one without it would quietly remove that property."""
    with pytest.raises(DecisionError):
        DecisionStore(tmp_path).create(
            DecisionCard(id="", title="Pick one", options=[SHIP, HOLD])
        )


def test_a_decision_card_with_no_options_is_refused(tmp_path):
    with pytest.raises(DecisionError):
        DecisionStore(tmp_path).create(DecisionCard(id="", title="Think about it"))


def test_a_corrupt_file_is_skipped_rather_than_emptying_the_list(tmp_path):
    """One bad write must not read as 'you have no pending decisions'."""
    store = DecisionStore(tmp_path)
    good = store.create(_card())
    (tmp_path / "d-zzzz.json").write_text("{not json", encoding="utf-8")
    ids = [c.id for c in store.list()]
    assert good.id in ids, "a corrupt sibling emptied the list — that reads as 'done'"


def test_to_dict_round_trips_through_json(tmp_path):
    card = DecisionStore(tmp_path).create(_card(facts=["312 rows affected"]))
    again = DecisionCard.from_dict(json.loads(json.dumps(card.to_dict())))
    assert again.id == card.id
    assert again.facts == ["312 rows affected"]
    assert again.option("hold").consequence == "blocks the release"


def test_sweep_survives_a_card_it_cannot_delete(tmp_path, monkeypatch):
    """One file held open by a reader must not end the sweep (WinError 32).

    Measured 2026-09-18: the sweep died on the 1,155th of 2,772 closed cards and
    left the store 1,634 files deep -- every reader then walked all of them.
    """
    from pathlib import Path

    store = DecisionStore(tmp_path)
    ids = [store.create(_card(summary=f"card {i}")).id for i in range(3)]
    for card_id in ids:
        store.cancel(card_id)
    kept_open = store.create(_card(summary="still waiting")).id

    locked = ids[1]
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == f"{locked}.json":
            raise PermissionError(32, "being used by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    removed = store.sweep(keep_closed_seconds=-1)

    assert removed == 2
    assert store.last_sweep_busy == 1
    left = sorted(p.stem for p in tmp_path.glob("d-*.json"))
    assert left == sorted([locked, kept_open])


def test_sweep_archive_keeps_every_swept_card_and_is_reversible(tmp_path):
    """An unattended sweep must not be the only copy's end: with --archive each
    card lands in a monthly zip BEFORE it is removed, byte for byte."""
    import json
    import zipfile

    store = DecisionStore(tmp_path / "store")
    closed = [store.create(_card(summary=f"done {i}")).id for i in range(2)]
    for card_id in closed:
        store.cancel(card_id)
    kept_open = store.create(_card(summary="still waiting")).id
    originals = {c: (tmp_path / "store" / f"{c}.json").read_bytes() for c in closed}

    removed = store.sweep(keep_closed_seconds=-1, archive=tmp_path / "archive")

    assert removed == 2
    assert [p.stem for p in (tmp_path / "store").glob("d-*.json")] == [kept_open]
    bundles = list((tmp_path / "archive").glob("cards-*.zip"))
    assert len(bundles) == 1
    with zipfile.ZipFile(bundles[0]) as zf:
        assert sorted(zf.namelist()) == sorted(f"{c}.json" for c in closed)
        for c in closed:
            assert zf.read(f"{c}.json") == originals[c]
            assert json.loads(zf.read(f"{c}.json"))["status"] == "cancelled"

    # a second sweep appends to the same month without duplicating entries
    again = store.create(_card(summary="done later")).id
    store.cancel(again)
    store.sweep(keep_closed_seconds=-1, archive=tmp_path / "archive")
    with zipfile.ZipFile(bundles[0]) as zf:
        assert len(zf.namelist()) == 3
