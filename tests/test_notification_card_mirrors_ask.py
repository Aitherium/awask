"""A card for an AskUserQuestion shows THAT question and THOSE options.

Measured 2026-11-08: a session blocked on a three-tab AskUserQuestion raised a card
reading only "Needs: Claude needs your permission" with "I am looking now" / "Not
now". Claude Code had not flushed the asking turn to the transcript when Notification
fired (the tool_use record landed ~9 s after the card), so the hook saw only an older
text turn. The PreToolUse stash is the source that is there in time.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "awask" / "hooks" / "awask_notification_card.py"
spec = importlib.util.spec_from_file_location("awask_notification_card_ask", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

QUESTIONS = [
    {"question": "I couldn't find CertTracker on C:, E:, D: or in Downloads. Where is it?",
     "header": "CertTracker", "multiSelect": False,
     "options": [
         {"label": "Jason uploaded it", "description": "It's in GargBot uploads."},
         {"label": "I'll drop it", "description": "You'll put the zip in Downloads."},
         {"label": "It's a GitHub repo", "description": "Tell me the org/repo name."},
         {"label": "Build it fresh", "description": "It doesn't exist as code yet."}]},
    {"question": "Which teaser version do you want tonight?", "header": "Teaser",
     "multiSelect": False,
     "options": [{"label": "Full world pack", "description": "x"},
                 {"label": "Stills only", "description": "y"}]},
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_DECISIONS_POPUP", "0")
    monkeypatch.setenv("AITHER_NO_GUI_PROMPT", "1")


def _transcript(tmp_path: Path, records) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(p)


def _stale_transcript(tmp_path):
    """The transcript as it stood when the 11-08 card fired: the ask not yet in it."""
    return _transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "Garg deliverables"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "The executive-brief hook doesn't apply here."}]}},
    ])


def _stash(sid="s1", tid="toolu_ask1"):
    return hook.stash_ask({"tool_name": "AskUserQuestion", "session_id": sid,
                           "tool_use_id": tid, "tool_input": {"questions": QUESTIONS}})


def _payload(tmp_path, transcript=None, kind="permission_prompt", sid="s1"):
    return {"session_id": sid, "notification_type": kind,
            "message": "Claude needs your permission", "cwd": "C:\\AitherOS-Fresh",
            "transcript_path": transcript or _stale_transcript(tmp_path)}


def test_the_stash_reaches_the_card_when_the_transcript_lags(tmp_path):
    from awask.store import get_store
    assert _stash()
    assert hook.handle(_payload(tmp_path)) is True
    [card] = [c for c in get_store().list(session_id="s1")]
    assert card.title.startswith("CertTracker: I couldn't find CertTracker")
    # The dialog is on screen: options are LISTED (nothing can type into the
    # terminal), and the one action is opening the session.
    assert [o.key for o in card.options] == ["tui:type", "later"]
    assert "2. I'll drop it -- You'll put the zip in Downloads." in card.facts
    assert "4. Build it fresh -- It doesn't exist as code yet." in card.facts
    assert "Which teaser version" in card.summary and "Full world pack" in card.summary
    assert card.source.agent == "claude-code" and card.source.session_id == "s1"
    assert card.default_key == "later"
    assert (card.dedupe_key or "").startswith(hook.KEY_PREFIX + "s1:ask-")


def test_an_ask_in_the_transcript_is_used_without_a_stash(tmp_path):
    t = _transcript(tmp_path, [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu7", "name": "AskUserQuestion",
             "input": {"questions": QUESTIONS[:1]}}]}}])
    ask = hook.pending_ask("s1", hook.transcript_context(t))
    assert ask and ask["id"] == "tu7" and len(ask["questions"]) == 1


def test_an_answered_ask_is_not_mirrored(tmp_path):
    _stash(tid="tu7")
    t = _transcript(tmp_path, [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu7", "name": "AskUserQuestion",
             "input": {"questions": QUESTIONS}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu7", "content": "answered"}]}}])
    assert hook.pending_ask("s1", hook.transcript_context(t)) is None


def test_a_multiselect_question_offers_no_number_keys():
    qs = [dict(QUESTIONS[0], multiSelect=True)]
    keys = [o["key"] for o in hook.ask_card_parts(qs, {}, {})["options"]]
    assert keys == ["tui:type", "later"]


def test_every_quoted_string_is_redacted():
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    qs = [{"question": "Use %s?" % secret, "header": "Key", "multiSelect": False,
           "options": [{"label": "yes %s" % secret, "description": "token=%s" % secret}]},
          {"question": "and %s" % secret, "header": "", "options": [{"label": secret}]}]
    parts = hook.ask_card_parts(qs, {}, {"asked": "here is %s" % secret})
    assert secret not in json.dumps(parts)


def test_a_permission_prompt_without_an_ask_keeps_the_generic_card(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(hook, "raise_card",
                        lambda payload, sid, message, need="", ask=None:
                        seen.append(ask) or True)
    assert hook.handle(_payload(tmp_path)) is True
    assert seen == [None]


def test_resolve_clears_the_stash(tmp_path):
    _stash()
    hook.resolve("s1")
    assert hook.pending_ask("s1", {}) is None


def test_the_installer_wires_the_stash_to_askuserquestion_only(tmp_path):
    from awask import install
    assert install.HOOKS["awask_ask_stash.py"] == "PreToolUse"
    added, _notes = install.merge_settings(tmp_path, dry_run=False)
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    pre = data["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in pre
            if "awask_ask_stash.py" in g["hooks"][0]["command"]] == ["AskUserQuestion"]
    assert all(g["matcher"] == "" for g in data["hooks"]["Notification"])
