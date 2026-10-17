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


# --- a card leaves the machine: what the owner pasted must not ride on it -----------
# Measured 2026-10-17: a prompt carrying an OAuth app's client secret ("Application ID
# <id> Secret <value> ...") reached a "Claude Code needs you" card's "You asked:" line
# verbatim, and urgency=high cards are DMed to Discord. Fake values are assembled at
# runtime so no secret-shaped literal sits in the repo.
_FAKE = "".join(["Qz7aB3dE5f", "G7hJ9kL1mN", "3pQ5rS7tU9", "vW1xY3z_Ab"])
_FAKE_ID = "".join(["SuXy", "Q9w2", "Ee7r", "Tt5y", "Uu3i", "Oo1p", "Aa8s", "Dd6f"])


def test_a_pasted_client_secret_never_reaches_the_card():
    ctx = {"asked": f"Application: aither Application ID {_FAKE_ID} Secret {_FAKE} "
                    "Icon None Scope default",
           "said": "Decide: publish Android 0.3.28 to GitHub?"}
    summ = hook.card_summary("Claude is waiting", ctx, "idle_prompt",
                             "publish Android 0.3.28 to GitHub?")
    title = hook.card_title({"cwd": "C:\\AitherOS-Fresh"}, {"asked": ctx["asked"]})
    for text in (summ, title):
        assert _FAKE not in text and _FAKE[:12] not in text
        assert _FAKE_ID not in text
    assert "You asked: Application: aither" in summ
    assert "publish Android 0.3.28" in summ


def test_a_secret_cut_by_the_clip_is_still_masked():
    asked = "x " * 140 + f"Secret {_FAKE}"  # the clip at 300 chars lands inside it
    summ = hook.card_summary("waiting", {"asked": asked}, "idle_prompt", "ok?")
    assert _FAKE[:6] not in summ


def test_labelled_and_known_token_shapes_are_masked():
    gh = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    cases = ["password: " + "hunter" + "twelve", "api_key=" + "k" * 4 + "9" * 6,
             f"use token {gh} now", "app password " + "abcd" * 3 + "1234"]
    for text in cases:
        assert "[redacted]" in hook.redact_secrets(text), text
    assert gh not in hook.redact_secrets(f"use token {gh} now")


def test_redaction_leaves_ordinary_prose_shas_and_paths_alone():
    for text in ("the token economy earns credits", "password reset flow is broken",
                 "commit 4481001f69a2b3c4d5e6f708192a3b4c5d6e7f80 landed",
                 "AitherOS/lib/support/mail_evidence.py line 407",
                 "Waiting on the full gate run."):
        assert hook.redact_secrets(text) == text, text
