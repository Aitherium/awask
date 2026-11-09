"""While the owner is away, an AskUserQuestion is answered from its card, no keys.

Measured 2026-11-08: SendKeys into Windows Terminal reported typed:true and reached
nothing, so a desk answer never got to the session. Claude Code documents the real
path: PreToolUse returns allow + updatedInput.answers and the tool runs without its
dialog (proven end to end against `claude` in SDK mode: the tool_result carried the
card's answer and the host was never asked).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "awask" / "hooks" / "awask_notification_card.py"
spec = importlib.util.spec_from_file_location("awask_notification_card_hold", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

Q1 = {"question": "Where is CertTracker?", "header": "CertTracker", "multiSelect": False,
      "options": [{"label": "Jason uploaded it", "description": "a"},
                  {"label": "I'll drop it", "description": "b"}]}
Q2 = {"question": "Which teaser?", "header": "Teaser", "multiSelect": False,
      "options": [{"label": "Full world pack"}, {"label": "Stills"}]}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_DECISIONS_POPUP", "0")
    monkeypatch.setenv("AITHER_NO_GUI_PROMPT", "1")
    monkeypatch.delenv("AWASK_ASK_HOLD", raising=False)
    import awask.store as store_mod
    monkeypatch.setattr(store_mod, "_STORE", None)  # the singleton caches its dir


def _payload(*questions):
    return {"tool_name": "AskUserQuestion", "session_id": "s1", "tool_use_id": "tu1",
            "cwd": "C:\\P", "tool_input": {"questions": list(questions)}}


def _store():
    from awask.store import get_store
    return get_store()


def _open():
    return [c for c in _store().list(session_id="s1") if c.status == "open"]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _answering(choices, clock=None):
    """A sleep() that answers the open card with the next choice."""
    queue = list(choices)

    def sleep(_s):
        if clock:
            clock.t += 1
        cards = _open()
        if cards and queue:
            _store().answer(cards[0].id, queue.pop(0))
    return sleep


def test_a_present_owner_is_never_held(monkeypatch):
    monkeypatch.setattr(hook, "owner_away", lambda: False)
    assert hook.hold_answer(_payload(Q1), sleep=_answering(["ans:1"])) is None
    assert _open() == []


def test_away_card_answers_become_updated_input(monkeypatch):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    out = hook.hold_answer(_payload(Q1, Q2), sleep=_answering(["ans:2", "ans:1"]))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["questions"] == [Q1, Q2]
    assert hso["updatedInput"]["answers"] == {"Where is CertTracker?": "I'll drop it",
                                              "Which teaser?": "Full world pack"}


def test_hold_cards_carry_the_questions_options_as_ans_keys(monkeypatch):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    seen = []

    def sleep(_s):
        seen.extend(_open())
        _store().answer(seen[-1].id, "ans:1")
    hook.hold_answer(_payload(Q1), sleep=sleep)
    card = seen[0]
    assert [o.key for o in card.options] == ["ans:1", "ans:2", "ans:type", "later"]
    assert card.title == "CertTracker: Where is CertTracker?"
    assert card.source.agent == "claude-code"


@pytest.mark.parametrize("choice", ["ans:type", "later"])
def test_answer_in_the_terminal_releases_to_the_dialog(monkeypatch, choice):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    assert hook.hold_answer(_payload(Q1), sleep=_answering([choice])) is None


def test_the_owner_coming_back_releases_and_withdraws_the_card(monkeypatch):
    state = {"away": True}
    monkeypatch.setattr(hook, "owner_away", lambda: state["away"])

    def sleep(_s):
        state["away"] = False
    assert hook.hold_answer(_payload(Q1), sleep=sleep) is None
    assert _open() == []


def test_the_hold_times_out_to_the_dialog(monkeypatch):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    clock = _Clock()
    assert hook.hold_answer(_payload(Q1), sleep=_answering([], clock), clock=clock) is None
    assert clock.t - 1000.0 <= hook.HOLD_SECONDS + 2
    assert _open() == []


@pytest.mark.parametrize("q", [dict(Q1, multiSelect=True), dict(Q1, options=[])])
def test_questions_a_card_cannot_answer_are_not_held(monkeypatch, q):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    assert hook.hold_answer(_payload(q), sleep=_answering(["ans:1"])) is None
    assert _open() == []


def test_hold_mode_env(monkeypatch):
    monkeypatch.setenv("AWASK_ASK_HOLD", "never")
    assert hook.owner_away() is False
    monkeypatch.setenv("AWASK_ASK_HOLD", "always")
    assert hook.owner_away() is True


def test_unknown_presence_is_not_away(monkeypatch):
    monkeypatch.setattr(hook, "_foreground_and_idle", lambda: None)
    assert hook.owner_away() is False


@pytest.mark.parametrize("seen,away", [
    (("CASCADIA_HOSTING_WINDOW_CLASS", "claude", 5.0), False),
    (("Chrome_WidgetWin_1", "YouTube", 5.0), True),
    (("CASCADIA_HOSTING_WINDOW_CLASS", "claude", 600.0), True),
])
def test_presence_from_foreground_and_idle(monkeypatch, seen, away):
    monkeypatch.setattr(hook, "_foreground_and_idle", lambda: seen)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert hook.owner_away() is away


Q3 = {"question": "Submit tonight?", "header": "Submit", "multiSelect": False,
      "options": [{"label": "Yes, submit"}, {"label": "Hold it"}]}


def test_a_three_tab_ask_collects_every_answer_before_sending(monkeypatch):
    """Owner report 2026-11-08: "it only let me do the first answer then it closed
    the card". Every question gets a card; nothing is sent until all are answered."""
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    seen, choices = [], ["ans:2", "ans:1", "ans:2"]

    def sleep(_s):
        cards = _open()
        assert len(cards) <= 1, "one live question at a time"
        if cards and choices:
            seen.append(cards[0])
            _store().answer(cards[0].id, choices.pop(0))
    out = hook.hold_answer(_payload(Q1, Q2, Q3), sleep=sleep)
    assert [c.title for c in seen] == ["CertTracker: Where is CertTracker?",
                                       "Teaser: Which teaser?", "Submit: Submit tonight?"]
    assert "Question 1 of 3" in seen[0].facts[1] and "until all 3" in seen[0].facts[1]
    assert "Question 3 of 3" in seen[2].facts[1] and "sends all 3" in seen[2].facts[1]
    assert "CertTracker = I'll drop it" in seen[1].summary
    assert "Teaser = Full world pack" in seen[2].summary
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Where is CertTracker?": "I'll drop it",
        "Which teaser?": "Full world pack",
        "Submit tonight?": "Hold it"}


def test_nothing_is_sent_when_a_later_question_is_not_answered(monkeypatch):
    """Answering the first card must not count as answering the ask."""
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    clock = _Clock()
    choices = ["ans:1"]

    def sleep(_s):
        clock.t += 1
        cards = _open()
        if cards and choices:
            _store().answer(cards[0].id, choices.pop(0))
    assert hook.hold_answer(_payload(Q1, Q2), sleep=sleep, clock=clock) is None
    assert _open() == [], "the unanswered second card is withdrawn, the dialog shows"


def test_each_question_gets_its_own_window_inside_the_ceiling(monkeypatch):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    monkeypatch.setattr(hook, "HOLD_SECONDS", 10.0)
    monkeypatch.setattr(hook, "HOLD_TOTAL_SECONDS", 1000.0)
    clock = _Clock()
    choices = ["ans:1", "ans:2"]

    def sleep(_s):
        clock.t += 1
        cards = _open()
        if cards and choices and clock.t - 1000.0 >= 8 * (3 - len(choices)):
            _store().answer(cards[0].id, choices.pop(0))  # each answered at ~8 s
    out = hook.hold_answer(_payload(Q1, Q2), sleep=sleep, clock=clock)
    assert out and len(out["hookSpecificOutput"]["updatedInput"]["answers"]) == 2


def test_duplicate_question_texts_are_not_held(monkeypatch):
    monkeypatch.setattr(hook, "owner_away", lambda: True)
    assert hook.hold_answer(_payload(Q1, dict(Q2, question=Q1["question"])),
                            sleep=_answering(["ans:1", "ans:1"])) is None
