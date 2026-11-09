"""A permission card names exactly the waiting call, or stays generic.

The transcript lags the screen (measured 2026-11-08), so a permission card that read
the waiting tool from it quoted an OLDER call. One stash file per call (PreToolUse),
removed when the call runs (PostToolUse), leaves exactly the calls still waiting.
The call is named only when that set is one fresh call; no Yes button is offered,
because nothing can deliver a keystroke into the terminal (measured 2026-11-08).
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "awask" / "hooks" / "awask_notification_card.py"
spec = importlib.util.spec_from_file_location("awask_notification_card_perm", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_DECISIONS_POPUP", "0")
    monkeypatch.setenv("AITHER_NO_GUI_PROMPT", "1")


def _pre(tool="Bash", tid="tb1", sid="s1", **args):
    args = args or {"command": "systemctl restart aither-relay"}
    return hook.stash_call({"tool_name": tool, "session_id": sid, "tool_use_id": tid,
                            "tool_input": args})


def _post(tid="tb1", sid="s1"):
    hook.clear_call({"session_id": sid, "tool_use_id": tid})


def _stale_transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Reading the old file."},
        {"type": "tool_use", "id": "old", "name": "Read",
         "input": {"file_path": "C:/old.txt"}}]}}), encoding="utf-8")
    return str(p)


def _notify(tmp_path, message="Claude needs your permission to use Bash", sid="s1"):
    return hook.handle({"session_id": sid, "notification_type": "permission_prompt",
                        "message": message, "cwd": "C:\\AitherOS-Fresh",
                        "transcript_path": _stale_transcript(tmp_path)})


def _card(sid="s1"):
    from awask.store import get_store
    [card] = list(get_store().list(session_id=sid))
    return card


def test_the_card_names_the_waiting_call_not_the_stale_transcript(tmp_path):
    assert _pre()
    assert _notify(tmp_path) is True
    card = _card()
    assert "Bash: systemctl restart aither-relay" in card.summary
    assert "C:/old.txt" not in card.summary
    assert [o.key for o in card.options] == ["tui:type", "later"]
    assert "systemctl restart aither-relay" in card.options[0].consequence
    assert card.default_key == "later"
    assert "ask again" not in json.dumps([o.to_dict() for o in card.options]).lower()


def test_a_message_without_a_tool_name_still_maps_one_fresh_call(tmp_path):
    _pre()
    assert _notify(tmp_path, message="Claude needs your permission") is True
    assert "Bash: systemctl restart aither-relay" in _card().summary
    assert _card().options[0].key == "tui:type"


@pytest.mark.parametrize("setup", ["none", "two", "stale", "other_tool", "plan", "done"])
def test_anything_uncertain_keeps_the_generic_card(tmp_path, setup):
    if setup == "two":
        _pre(tid="a")
        _pre(tid="b", command="rm -rf build")
    elif setup == "stale":
        _pre()
        f = hook._call_file("s1", "tb1")
        old = time.time() - hook.CALL_TTL_SECONDS - 5
        os.utime(f, (old, old))
    elif setup == "other_tool":
        _pre(tool="Write", file_path="C:/x.txt")
    elif setup == "plan":
        _pre(tool="ExitPlanMode", plan="do it")
    elif setup == "done":
        _pre()
        _post()
    assert _notify(tmp_path) is True
    keys = [o.key for o in _card().options]
    assert keys == ["ack", "later"], setup


def test_the_stash_is_redacted_and_small(tmp_path):
    _pre(command="curl -H 'Authorization: Bearer %s' %s" % (SECRET, "x" * 2000))
    raw = hook._call_file("s1", "tb1").read_text(encoding="utf-8")
    assert SECRET not in raw
    assert len(raw) < 700


def test_parallel_calls_never_overwrite_each_other(tmp_path):
    _pre(tid="a")
    _pre(tid="b")
    _post(tid="a")
    call = hook.waiting_call("s1", "")
    assert call and call["tool_use_id"] == "b"


def test_resolve_clears_the_waiting_calls(tmp_path):
    _pre()
    hook.resolve("s1")
    assert hook.waiting_call("s1", "") is None


def test_an_askuserquestion_still_gets_its_own_card(tmp_path):
    _pre(tool="AskUserQuestion", tid="q1", questions=[])
    hook.stash_ask({"tool_name": "AskUserQuestion", "session_id": "s1", "tool_use_id": "q1",
                    "tool_input": {"questions": [{"question": "Which?", "header": "H",
                                                  "options": [{"label": "A"}]}]}})
    assert _notify(tmp_path, message="Claude needs your permission") is True
    assert [o.key for o in _card().options] == ["tui:type", "later"]
    assert _card().title == "H: Which?"


def test_the_installer_wires_both_call_hooks_for_every_tool(tmp_path):
    from awask import install
    install.merge_settings(tmp_path, dry_run=False)
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))

    def matcher(event, name):
        return [g["matcher"] for g in data["hooks"][event]
                if name in g["hooks"][0]["command"]]
    assert matcher("PreToolUse", "awask_tool_stash.py") == [""]
    assert matcher("PostToolUse", "awask_tool_done.py") == [""]
    assert matcher("PreToolUse", "awask_ask_stash.py") == ["AskUserQuestion"]
