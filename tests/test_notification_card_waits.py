"""A "Claude Code needs you" card means the session is waiting on its HUMAN -- once.

Measured 2026-10-16: cards fired while a session was only "Waiting on the full gate
run" (a background CI task), re-raised every 15 minutes for the same idle session
(1019 stamp files), and each raise was a new card and a new phone DM.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "awask" / "hooks" / "awask_notification_card.py"
spec = importlib.util.spec_from_file_location("awask_notification_card", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def _transcript(tmp_path: Path, records) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(p)


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text, tools=()):
    content = [{"type": "text", "text": text}] + [
        {"type": "tool_use", "id": f"tu{i}", "name": n, "input": inp}
        for i, (n, inp) in enumerate(tools)]
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


@pytest.fixture
def raised(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    cards = []
    monkeypatch.setattr(hook, "raise_card",
                        lambda payload, sid, message, need="": cards.append(
                            {"sid": sid, "need": need, "kind": payload.get("notification_type")})
                        or True)
    return cards


def _notify(tmp_path, records, kind="idle_prompt", sid="s1",
            message="Claude is waiting for your input"):
    payload = {"session_id": sid, "notification_type": kind, "message": message,
               "transcript_path": _transcript(tmp_path, records), "cwd": "C:\\AitherOS-Fresh"}
    return hook.handle(payload)


def test_waiting_on_ci_with_a_background_task_raises_nothing(raised, tmp_path):
    recs = [_user("ship it"),
            _assistant("Started the gates.", [("Bash", {"command": "run_gates.py",
                                                        "run_in_background": True})]),
            _assistant("Waiting on the full gate run.")]
    _notify(tmp_path, recs)
    assert raised == []


def test_a_finished_turn_that_asks_nothing_raises_nothing(raised, tmp_path):
    _notify(tmp_path, [_user("fix it"), _assistant("Done. Merged #13170.\nDecide: nothing")])
    assert raised == []


def test_a_real_question_raises_one_card_with_one_line_saying_what_it_needs(raised, tmp_path):
    recs = [_user("deploy"), _assistant("Both builds are green.\nDecide: roll Genesis now "
                                        "or wait for the 18:00 window?")]
    _notify(tmp_path, recs)
    assert len(raised) == 1
    assert "roll Genesis now or wait for the 18:00 window?" in raised[0]["need"]


def test_the_same_wait_never_raises_twice_until_the_owner_answers(raised, tmp_path):
    recs = [_user("deploy"), _assistant("Which branch should I merge first?")]
    _notify(tmp_path, recs)
    _notify(tmp_path, recs)
    _notify(tmp_path, recs)
    assert len(raised) == 1
    hook.clear_wait("s1")  # what --resolve does when the owner types
    _notify(tmp_path, recs)
    assert len(raised) == 2


def test_a_permission_prompt_is_a_real_wait_even_with_background_work(raised, tmp_path):
    recs = [_user("go"), _assistant("Running it.", [("Bash", {"command": "x",
                                                               "run_in_background": True}),
                                                     ("Bash", {"command": "rm -rf build"})])]
    _notify(tmp_path, recs, kind="permission_prompt",
            message="Claude needs your permission to use Bash")
    assert len(raised) == 1 and "rm -rf build" in raised[0]["need"]


def test_a_finished_background_task_no_longer_counts_as_pending(raised, tmp_path):
    recs = [_user("go"),
            _assistant("Started.", [("Bash", {"command": "ci", "run_in_background": True})]),
            _user("<task-notification><task-id>b1</task-id><status>completed</status>"),
            _assistant("CI is red on ruff. Should I fix the lint or revert the change?")]
    _notify(tmp_path, recs)
    assert len(raised) == 1


def test_the_card_key_is_stable_within_one_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "d"))
    first = hook.begin_wait("s9")
    assert first and hook.begin_wait("s9") is None, "second event in the same wait"
    assert hook.wait_key("s9").startswith(hook.KEY_PREFIX + "s9:")
    assert hook.wait_key("s9") == hook.wait_key("s9")
