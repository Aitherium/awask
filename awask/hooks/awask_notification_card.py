#!/usr/bin/env python3
"""Notification hook — turn "the agent is waiting for you" into a card.

Claude Code fires ``Notification`` when a session needs its human: a permission
prompt, or an idle session waiting on input. By default that produces a line in a
terminal nobody is looking at, and a run can sit blocked for forty minutes with
every component perfectly healthy. The signal exists; it just has no reader.

This converts that event into a durable card, so the same "you are the bottleneck"
fact reaches whatever surface the owner actually watches.

**It fires only on a REAL wait, and once per wait.** A card is an interruption, and
a hook that fires on every notification trains the owner to ignore all of them.
Measured 2026-10-16: ``idle_prompt`` fires after ANY finished turn, so cards went
out while a session was only "Waiting on the full gate run" with a background task
running, re-raised every 15 minutes (the idle message never changes, so a
(session, message) cooldown expired and fired again), each raise a new card and a
new phone DM. Now:

- a permission or elicitation prompt is always a wait (the session is blocked);
- an idle prompt is a wait only when Claude's last words ASK the owner something
  (a question, or a ``Decide:`` line that is not "nothing") and no background
  task it started is still running;
- one card per session per wait: the wait opens on the first card and closes only
  when the owner types in that session (``--resolve``, UserPromptSubmit);
- the card's first line says what it needs, in one line.

**An AskUserQuestion ask mirrors the terminal.** Measured 2026-11-08: the card for
a three-tab AskUserQuestion said only "Claude needs your permission" with generic
buttons, because Claude Code had not yet flushed the asking turn to the transcript
when Notification fired (the tool_use record landed ~9 s AFTER the card). The
PreToolUse hook ``awask_ask_stash.py`` now stashes the ask's ``tool_input`` under the
state dir; this hook reads it and offers the question's own options as ``tui:<n>``
(the terminal's numbering), so a surface that can type into the session answers it.

**A permission prompt names the exact waiting call.** The same lag left a permission
card quoting an OLDER tool from the transcript. ``awask_tool_stash.py`` (PreToolUse)
writes one small redacted file per call and ``awask_tool_done.py`` (PostToolUse)
deletes it, so the calls still waiting are exactly the files left. When exactly ONE
fresh call is waiting (and the notification text, if it names a tool, names that
one) the card quotes it and offers "Open the session" (``tui:type``) and ``later``.
No Yes button: nothing can deliver it into the terminal (see below). Anything less
certain keeps the generic card.

**When the owner is away, the card IS the answer path -- no keystrokes.** Measured
2026-11-08: typing a card's option number into Windows Terminal (SendKeys) reported
success and reached nothing. Claude Code documents the real path: a PreToolUse hook
on AskUserQuestion that returns ``permissionDecision: "allow"`` with ``updatedInput``
holding ``answers`` (question text -> option label) runs the tool without the dialog.
So ``awask_ask_stash.py`` HOLDS the ask -- raises one ``ans:<n>`` card per question
and waits for the answers -- only while the owner is away from a terminal (another
window in front, or idle). Back at a terminal, "Answer in the terminal", "Not now",
or the hold timing out all release it to the ordinary dialog. A terminal user never
waits.

Installed by ``awask install-hooks``. Standard library only; never breaks a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

#: Do not re-raise the same PERMISSION ask for this long (a permission prompt the
#: owner approves in the terminal never reaches --resolve).
COOLDOWN_SECONDS = float(os.getenv("AWASK_NOTIFY_COOLDOWN", "900"))
#: An idle wait nobody resolved is forgotten after this long (a crashed session).
WAIT_TTL_SECONDS = float(os.getenv("AWASK_NOTIFY_WAIT_TTL", "86400"))


def _quiet() -> int:
    sys.stdout.write("{}")
    return 0


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def state_dir() -> Path:
    env = os.getenv("AITHER_DECISIONS_DIR", "").strip()
    base = Path(env) if env else (Path.home() / ".aither" / "decisions")
    return base / "_notify"


def fingerprint(session_id: str, message: str) -> str:
    """Session-scoped on purpose: two sessions blocked on the same thing are two
    asks, not one. A global key would mute the second session's card entirely."""
    h = hashlib.sha256()
    h.update((session_id or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((message or "").strip().lower().encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def recently_raised(key: str) -> bool:
    """True if this exact ask is still within its cooldown.

    Written before the raise, and treated as 'raised' on any write failure — a
    cooldown that fails OPEN would let a notification storm raise a card per event.
    """
    marker = state_dir() / ("%s.stamp" % key)
    now = time.time()
    try:
        if marker.is_file() and (now - marker.stat().st_mtime) < COOLDOWN_SECONDS:
            return True
    except OSError:
        return True
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now), encoding="utf-8")
    except OSError:
        return True
    return False


def _safe_sid(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", session_id or "nosession")[:80]


def _wait_marker(session_id: str) -> Path:
    return state_dir() / ("%s.wait" % _safe_sid(session_id))


def begin_wait(session_id: str) -> Optional[str]:
    """Open this session's wait and return its start stamp, or None when a wait is
    already open (the card for it exists) -- or when the marker cannot be written:
    failing CLOSED, so a broken state dir mutes rather than storms."""
    marker = _wait_marker(session_id)
    now = time.time()
    try:
        if marker.is_file() and (now - marker.stat().st_mtime) < WAIT_TTL_SECONDS:
            return None
        marker.parent.mkdir(parents=True, exist_ok=True)
        stamp = str(int(now))
        marker.write_text(stamp, encoding="utf-8")
        return stamp
    except OSError:
        return None


def wait_key(session_id: str) -> str:
    """The card's dedupe key for this session's CURRENT wait: stable for the whole
    wait (so a re-raise is the same card), new for the next one."""
    try:
        stamp = _wait_marker(session_id).read_text(encoding="utf-8").strip() or "0"
    except OSError:
        stamp = "0"
    return "%s%s:%s" % (KEY_PREFIX, session_id, re.sub(r"[^0-9]", "", stamp) or "0")


def clear_wait(session_id: str) -> None:
    try:
        _wait_marker(session_id).unlink(missing_ok=True)
    except OSError as exc:  # a stuck marker only mutes this session's next card
        sys.stderr.write("[warn] awask_notification_card: wait not cleared: %s\n" % exc)


#: The tool whose ask a card can mirror option for option.
ASK_TOOL = "AskUserQuestion"
#: A stashed ask older than this is forgotten (the session moved on or died).
ASK_TTL_SECONDS = float(os.getenv("AWASK_ASK_TTL", "1800"))


def _ask_marker(session_id: str) -> Path:
    return state_dir() / ("%s.ask.json" % _safe_sid(session_id))


def clean_questions(raw) -> list:
    """AskUserQuestion's ``questions`` normalised to
    ``[{question, header, multiSelect, options: [{label, description}]}]``.
    Anything off-shape is dropped, never guessed at."""
    out: list = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        opts = []
        for o in q.get("options") or []:
            if isinstance(o, dict) and str(o.get("label") or "").strip():
                opts.append({"label": str(o["label"]).strip(),
                             "description": str(o.get("description") or "").strip()})
            elif isinstance(o, str) and o.strip():
                opts.append({"label": o.strip(), "description": ""})
        out.append({"question": str(q["question"]).strip(),
                    "header": str(q.get("header") or "").strip(),
                    "multiSelect": bool(q.get("multiSelect")),
                    "options": opts})
    return out


def stash_ask(payload: dict) -> bool:
    """PreToolUse(AskUserQuestion): keep the ask where the Notification hook can read
    it. True when stashed. Local only; every quoted string is redacted when a card
    is built from it, not here."""
    if str(payload.get("tool_name") or "") != ASK_TOOL:
        return False
    session_id = str(payload.get("session_id") or "").strip()
    args = payload.get("tool_input") or {}
    questions = clean_questions(args.get("questions") if isinstance(args, dict) else None)
    if not session_id or not questions:
        return False
    marker = _ask_marker(session_id)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"ts": time.time(),
                                      "tool_use_id": str(payload.get("tool_use_id") or ""),
                                      "questions": questions}), encoding="utf-8")
        return True
    except OSError as exc:
        sys.stderr.write("[warn] awask_notification_card: ask not stashed: %s\n" % exc)
        return False


def clear_ask(session_id: str) -> None:
    try:
        _ask_marker(session_id).unlink(missing_ok=True)
    except OSError as exc:  # a stale stash is TTL-bounded and id-checked
        sys.stderr.write("[warn] awask_notification_card: ask not cleared: %s\n" % exc)


def pending_ask(session_id: str, ctx: Optional[dict] = None) -> Optional[dict]:
    """The AskUserQuestion this session is blocked on, ``{"id", "questions"}``, or None.

    The stash wins (the transcript lags the screen); it is ignored once stale or once
    the transcript holds the tool_result answering it. Else the transcript's own last
    tool_use, when it is an unanswered ask."""
    ctx = ctx or {}
    answered = set(ctx.get("answered_ids") or ())
    marker = _ask_marker(session_id)
    try:
        if marker.is_file() and (time.time() - marker.stat().st_mtime) < ASK_TTL_SECONDS:
            data = json.loads(marker.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
            tid = str(data.get("tool_use_id") or "")
            questions = clean_questions(data.get("questions"))
            if questions and not (tid and tid in answered):
                return {"id": tid, "questions": questions}
    except (OSError, ValueError) as exc:
        sys.stderr.write("[warn] awask_notification_card: ask stash unreadable: %s\n" % exc)
    if ctx.get("pending_ask"):
        return {"id": str(ctx.get("pending_ask_id") or ""), "questions": ctx["pending_ask"]}
    return None


#: A stashed call older than this cannot be the one a permission prompt is about.
CALL_TTL_SECONDS = float(os.getenv("AWASK_CALL_TTL", "30"))
#: Tools whose permission dialog is NOT "1. Yes" (or that have their own card).
_UNMAPPED_TOOLS = {ASK_TOOL, "ExitPlanMode"}
_MSG_TOOL_RE = re.compile(r"permission to use\s+([A-Za-z0-9_.:-]+)", re.IGNORECASE)


def _calls_dir(session_id: str) -> Path:
    return state_dir() / ("%s.calls" % _safe_sid(session_id))


def _call_file(session_id: str, tool_use_id: str) -> Path:
    return _calls_dir(session_id) / ("%s.json" % _safe_sid(tool_use_id))


def stash_call(payload: dict) -> bool:
    """PreToolUse(any tool): record this call as waiting, redacted and small. One file
    per call, so parallel calls never overwrite each other. True when stashed."""
    session_id = str(payload.get("session_id") or "").strip()
    tool = str(payload.get("tool_name") or "").strip()
    args = payload.get("tool_input")
    if not session_id or not tool:
        return False
    tid = str(payload.get("tool_use_id") or "").strip() or (
        "noid-%s" % fingerprint(session_id, tool + json.dumps(args, sort_keys=True,
                                                              default=str)))
    what = _describe_tool({"name": tool, "input": args if isinstance(args, dict) else {}})
    try:
        f = _call_file(session_id, tid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"ts": time.time(), "tool_use_id": tid, "tool": tool,
                                 "what": _clip(redact_secrets(what), 400)}),
                     encoding="utf-8")
        return True
    except OSError as exc:
        sys.stderr.write("[warn] awask_notification_card: call not stashed: %s\n" % exc)
        return False


def clear_call(payload: dict) -> None:
    """PostToolUse: the call ran, so it is no longer waiting."""
    session_id = str(payload.get("session_id") or "").strip()
    tid = str(payload.get("tool_use_id") or "").strip()
    if not session_id or not tid:
        return
    try:
        _call_file(session_id, tid).unlink(missing_ok=True)
    except OSError as exc:  # TTL-bounded; a leftover only forces the generic card
        sys.stderr.write("[warn] awask_notification_card: call not cleared: %s\n" % exc)


def clear_calls(session_id: str) -> None:
    d = _calls_dir(session_id)
    try:
        for f in d.glob("*.json"):
            f.unlink(missing_ok=True)
    except OSError as exc:
        sys.stderr.write("[warn] awask_notification_card: calls not cleared: %s\n" % exc)


def waiting_call(session_id: str, message: str = "") -> Optional[dict]:
    """The ONE call a permission prompt can be about, ``{tool_use_id, tool, what}``, or
    None whenever that is not certain: no fresh call, more than one, a tool whose
    dialog is not "1. Yes", or a notification text that names a different tool."""
    now = time.time()
    fresh = []
    try:
        for f in _calls_dir(session_id).glob("*.json"):
            try:
                if now - f.stat().st_mtime >= CALL_TTL_SECONDS:
                    f.unlink(missing_ok=True)
                    continue
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("tool"):
                fresh.append(data)
    except OSError:
        return None
    if len(fresh) != 1:
        return None
    call = fresh[0]
    tool = str(call.get("tool") or "")
    if tool in _UNMAPPED_TOOLS:
        return None
    m = _MSG_TOOL_RE.search(message or "")
    if m and m.group(1).rstrip(".").lower() != tool.lower():
        return None
    return {"tool_use_id": str(call.get("tool_use_id") or ""), "tool": tool,
            "what": redact_secrets(str(call.get("what") or tool))}


def permission_options(call: dict) -> list:
    """Yes at 1 (the dialog's own number), "No / something else" opens the session,
    and later. "Don't ask again" is never offered from a card."""
    what = _clip(redact_secrets(call.get("what") or call.get("tool") or ""), 220)
    return [
        {"key": "tui:type", "label": "Open the session",
         "consequence": "brings it forward to approve or refuse %s" % what,
         "recommended": False},
        {"key": "later", "label": "Not now",
         "consequence": "the session stays blocked; nothing is lost", "recommended": False},
    ]


#: How long an away owner's ask is held PER QUESTION before the dialog shows.
HOLD_SECONDS = float(os.getenv("AWASK_ASK_HOLD_SECONDS", "120"))
#: The whole set's ceiling: under Claude Code's 600 s command-hook default, so the
#: hook always returns (and releases to the dialog) before it could be killed.
HOLD_TOTAL_SECONDS = float(os.getenv("AWASK_ASK_HOLD_TOTAL_SECONDS", "540"))
#: No keyboard/mouse input for this long = away, whatever window is in front.
AWAY_IDLE_SECONDS = float(os.getenv("AWASK_AWAY_IDLE_SECONDS", "90"))
_POLL_SECONDS = 1.0
#: Foreground window classes that mean "the owner is at a terminal".
_TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass",
                     "mintty", "PseudoConsoleWindow"}


def _foreground_and_idle() -> Optional[tuple]:
    """(foreground class, foreground title, idle seconds) on Windows, else None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _LII(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        lii = _LII(ctypes.sizeof(_LII), 0)
        idle = 0.0
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            idle = ((kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
        hwnd = user32.GetForegroundWindow()
        cls = ctypes.create_unicode_buffer(256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, cls, 256)
        user32.GetWindowTextW(hwnd, title, 512)
        return cls.value, title.value, idle
    except (OSError, AttributeError, ValueError):
        return None


def owner_away() -> bool:
    """Is the owner away from every terminal, so a card should carry the ask?

    ``AWASK_ASK_HOLD``: ``never`` (default off-Windows) / ``always`` / ``auto``.
    Auto = idle past AWAY_IDLE_SECONDS, or a non-terminal window in front (VS Code
    counts as a terminal for a session started in its terminal). Unknown = NOT away:
    the cost of a wrong "away" is a terminal user staring at no dialog."""
    mode = os.getenv("AWASK_ASK_HOLD", "auto").strip().lower()
    if mode in ("0", "off", "never", "no"):
        return False
    if mode in ("1", "on", "always", "yes"):
        return True
    seen = _foreground_and_idle()
    if seen is None:
        return False
    cls, title, idle = seen
    if idle >= AWAY_IDLE_SECONDS:
        return True
    if cls in _TERMINAL_CLASSES:
        return False
    if os.getenv("TERM_PROGRAM", "").lower() == "vscode" and "Visual Studio Code" in title:
        return False
    return bool(cls)


def _hold_card(store, payload: dict, questions: list, index: int, tid: str,
               answered: Optional[list] = None):
    from awask.store import DecisionCard, DecisionOption, DecisionSource
    session_id = str(payload.get("session_id") or "").strip()
    parts = ask_card_parts(questions[index:], payload, {}, key_prefix="ans")
    opts = []
    for o in parts["options"]:
        if o["key"] == "ans:type":
            o = dict(o, label="Answer in the terminal",
                     consequence="shows the question in the session now")
        elif o["key"] == "later":
            o = dict(o, consequence="shows the question in the session; nothing is lost")
        opts.append(DecisionOption(**o))
    facts = list(parts["facts"]) + [
        "Answering here answers the session directly (no typing into the terminal)."]
    summary = parts["summary"]
    if len(questions) > 1:
        left = len(questions) - index - 1
        facts[1:2] = ["Question %d of %d. %s" % (
            index + 1, len(questions),
            "The next question follows as soon as you answer; nothing is sent to the "
            "session until all %d are answered." % len(questions) if left
            else "The last one: answering it sends all %d answers together."
            % len(questions))]
        if answered:
            summary = redact_secrets("Answered so far: %s\n%s" % (
                "; ".join("%s = %s" % (_clip(h, 40), _clip(a, 60)) for h, a in answered),
                summary))[:2000]
    card = DecisionCard(
        id="", title=parts["title"], summary=summary, kind="blocked",
        facts=facts, options=opts, default_key="later", urgency="high",
        dedupe_key="%s%s:hold-%s" % (KEY_PREFIX, session_id,
                                     fingerprint(session_id, "%s\x00%d" % (tid, index))),
        source=DecisionSource(session_id=session_id, agent="claude-code",
                              cwd=str(payload.get("cwd") or "")))
    if session_id:
        card.subject = "session:%s" % session_id
    return store.create(card)


def hold_answer(payload: dict, sleep=time.sleep, clock=time.time) -> Optional[dict]:
    """PreToolUse(AskUserQuestion) while the owner is away: one card per question,
    each raised the moment the previous one is answered, and NOTHING sent until the
    whole set is answered -- then ONE PreToolUse output whose ``answers`` maps every
    question to its label and answers the tool without its dialog (owner report
    2026-11-08: "it only let me do the first answer then it closed the card").
    None (the dialog shows as usual) on anything else: owner present or back, a
    multi-select or option-less question, two questions with the same text, "Answer
    in the terminal"/"Not now", a cancelled card, or a question's window passing."""
    if str(payload.get("tool_name") or "") != ASK_TOOL:
        return None
    args = payload.get("tool_input")
    raw = args.get("questions") if isinstance(args, dict) else None
    questions = clean_questions(raw)
    if not questions or len(questions) != len(raw) \
            or any(q["multiSelect"] or not q["options"] for q in questions) \
            or len({q["question"] for q in questions}) != len(questions):
        # (answers is keyed by question text: two identical texts cannot both map)
        return None
    if not owner_away():
        return None
    try:
        from awask.store import STATUS_ANSWERED, get_store
        store = get_store()
    except Exception as exc:
        sys.stderr.write("[warn] awask_notification_card: no store to hold on: %s\n" % exc)
        return None
    tid = str(payload.get("tool_use_id") or "") or str(int(clock()))
    ceiling = clock() + HOLD_TOTAL_SECONDS
    answers: dict = {}
    answered: list = []
    for i, q in enumerate(questions):
        # Each question gets its own window, so a three-tab ask is not squeezed into
        # one question's time; the ceiling keeps the hook inside its timeout.
        deadline = min(clock() + HOLD_SECONDS, ceiling)
        try:
            card = _hold_card(store, payload, questions, i, tid, answered)
        except Exception as exc:
            sys.stderr.write("[warn] awask_notification_card: hold card failed: %s\n" % exc)
            return None
        while True:
            cur = store.get(card.id)
            if cur is not None and cur.status == STATUS_ANSWERED:
                break
            why = ""
            if cur is None or cur.status != "open":
                why = "closed"
            elif clock() >= deadline:
                why = "the hold timed out; the question is in the terminal"
            elif not owner_away():
                why = "you are back at a terminal; answer it there"
            if why:
                if cur is not None and cur.status == "open":
                    store.cancel(card.id, note=why)
                return None
            sleep(_POLL_SECONDS)
        m = re.fullmatch(r"ans:(\d+)", str(cur.answer or ""))
        n = int(m.group(1)) if m else 0
        if not 1 <= n <= len(raw[i].get("options") or []):
            return None  # "Answer in the terminal" / "Not now": release to the dialog
        opt = raw[i]["options"][n - 1]
        answers[raw[i]["question"]] = str(opt.get("label") if isinstance(opt, dict) else opt)
        answered.append((q.get("header") or q["question"], answers[raw[i]["question"]]))
    clear_ask(str(payload.get("session_id") or ""))
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "answered on the owner's decision card",
        "updatedInput": dict(args, answers=answers)}}


#: Claude Code notification types that block the session until a human acts.
BLOCKING_TYPES = {"permission_prompt", "elicitation_dialog"}
#: Types that MAY be a wait -- only when the transcript shows an ask (see owner_need).
WAITING_TYPES = BLOCKING_TYPES | {"idle_prompt"}

#: Every card this hook raises carries this prefix in its dedupe key, so the
#: UserPromptSubmit side (``--resolve``) can find and withdraw it.
KEY_PREFIX = "cc-waiting:"


def is_waiting(payload: dict, message: str) -> bool:
    kind = str(payload.get("notification_type") or "").strip()
    if kind:
        return kind in WAITING_TYPES
    low = message.lower()
    return "permission" in low or "waiting for your input" in low


#: How much of a transcript's tail to read. A session JSONL reaches tens of MB; the
#: context a card needs is always in the last few turns.
_TAIL_BYTES = 600_000


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _texts(content) -> list:
    if isinstance(content, str):
        return [content]
    out = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            out.append(part.get("text") or "")
    return out


def _owner_typed(text: str) -> bool:
    """A user-role record the OWNER typed -- not a hook/system/task notice."""
    t = text.lstrip()
    return bool(t) and not t.startswith(("<", "Stop hook", "[SYSTEM", "Caveat:"))


def _describe_tool(block: dict) -> str:
    name = str(block.get("name") or "a tool")
    args = block.get("input") or {}
    for key in ("command", "file_path", "path", "url", "pattern", "description", "prompt"):
        if isinstance(args, dict) and args.get(key):
            return "%s: %s" % (name, _clip(args[key], 160))
    return name


def _is_background(block: dict) -> bool:
    args = block.get("input") or {}
    return isinstance(args, dict) and bool(args.get("run_in_background"))


def transcript_context(path: str) -> dict:
    """What the owner needs to decide from a phone: what the session is about, what
    they last asked, what Claude last said, (for a permission prompt) the exact tool
    call waiting, and ``bg_pending`` -- background tasks started since the owner last
    typed whose completion notice has not arrived. Every field is optional; a
    missing/unreadable transcript yields {} and the card still raises."""
    ctx: dict = {}
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # drop the partial first line
            raw = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError, TypeError):
        return ctx
    launched = done = 0
    answered: list = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        if kind == "custom-title" and rec.get("customTitle"):
            ctx["title"] = str(rec["customTitle"])
        elif kind == "last-prompt" and rec.get("lastPrompt"):
            ctx["asked"] = str(rec["lastPrompt"])
        elif kind in ("user", "assistant"):
            msg = rec.get("message") or {}
            content = msg.get("content")
            if kind == "user":
                texts = _texts(content)
                typed = [t for t in texts if _owner_typed(t)]
                if typed:
                    ctx["asked"] = typed[-1]
                    launched = done = 0  # a new owner turn: count from here
                done += sum(t.count("<task-notification>") for t in texts)
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" \
                            and b.get("tool_use_id"):
                        answered.append(str(b["tool_use_id"]))
                        if ctx.get("pending_ask_id") == answered[-1]:
                            ctx.pop("pending_ask", None)
            else:
                said = [t for t in _texts(content) if t.strip()]
                if said:
                    ctx["said"] = said[-1]
                tools = [b for b in (content or []) if isinstance(b, dict)
                         and b.get("type") == "tool_use"]
                if tools:
                    ctx["pending_tool"] = _describe_tool(tools[-1])
                    last = tools[-1]
                    args = last.get("input") if isinstance(last.get("input"), dict) else {}
                    asked = (clean_questions(args.get("questions"))
                             if last.get("name") == ASK_TOOL else [])
                    if asked:
                        ctx["pending_ask"] = asked
                        ctx["pending_ask_id"] = str(last.get("id") or "")
                    else:
                        ctx.pop("pending_ask", None)
                elif said:
                    ctx["pending_tool"] = ""
                launched += sum(1 for b in tools if _is_background(b))
    ctx["bg_pending"] = max(launched - done, 0)
    if answered:
        ctx["answered_ids"] = answered
    return ctx


_DECIDE_RE = re.compile(r"^\s*\**\s*Decide\s*:?\**\s*:?\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ASK_PHRASES = ("need you to", "needs you to", "your call", "can you ", "could you ",
                "please confirm", "do you want", "should i ", "which do you", "let me know",
                "waiting for your", "need your")


def owner_need(said: str) -> str:
    """The one line Claude's last words ask of the owner, or "" when they ask nothing.

    A ``Decide:`` line wins (unless it is "nothing"); else the last sentence ending in
    a question mark; else a sentence with an explicit ask phrase. "Waiting on CI",
    "Done.", a report -- none of those ask anything."""
    text = str(said or "")
    m = None
    for m in _DECIDE_RE.finditer(text):
        pass
    if m:
        what = m.group(1).strip().strip("*").strip()
        if what and not what.lower().startswith("nothing"):
            return _clip(what, 240)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    for s in reversed(sentences[-6:]):
        if s.endswith("?"):
            return _clip(s, 240)
    for s in reversed(sentences[-6:]):
        if any(p in s.lower() for p in _ASK_PHRASES):
            return _clip(s, 240)
    return ""


#: Token shapes that are a credential wherever they appear.
_TOKEN_RES = (
    re.compile(r"\b(?:sk-ant-|sk-|sk_live_|sk_test_|pk_live_|rk_live_|ghp_|gho_|ghs_|ghu_"
               r"|github_pat_|xox[abposr]-|aither_sk_[a-z]+_|AKIA|ASIA|AIza)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
               re.DOTALL),
)
#: "Secret <value>", "password: <value>", "api_key=<value>" ...
_LABELLED_RE = re.compile(
    r"(?i)\b(secret|password|passwd|passcode|pwd|token|api[ _-]?key|access[ _-]?key"
    r"|private[ _-]?key|client[ _-]?secret|app[ _-]?password|bearer|authorization)"
    r"(\s*[:=]\s*|\s+)([\"']?)([^\s\"',;]{6,})")
#: A long random-looking word: upper, lower AND digit (a git sha or a path is not).
_RANDOM_RE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")
REDACTED = "[redacted]"


def _looks_random(word: str) -> bool:
    return (any(c.isupper() for c in word) and any(c.islower() for c in word)
            and any(c.isdigit() for c in word))


def redact_secrets(text: str) -> str:
    """The text with every credential-shaped value replaced by ``[redacted]``.

    A card leaves the machine (the Discord DM is its phone path), and its summary
    quotes what the owner typed: measured 2026-10-17, an OAuth client secret pasted
    into a prompt reached a card verbatim. A labelled value is masked when the label is
    followed by ``:``/``=`` (any value) or by a space and a value that looks random.
    """
    out = str(text or "")
    for rx in _TOKEN_RES:
        out = rx.sub(REDACTED, out)

    def _labelled(m: "re.Match") -> str:
        value = m.group(4)
        if value == REDACTED or value.startswith("[redacted"):
            return m.group(0)
        if not m.group(2).strip() and not (_looks_random(value) or len(value) >= 16
                                           and any(c.isdigit() for c in value)):
            return m.group(0)
        return m.group(1) + m.group(2) + m.group(3) + REDACTED

    out = _LABELLED_RE.sub(_labelled, out)
    return _RANDOM_RE.sub(lambda m: REDACTED if _looks_random(m.group(0)) else m.group(0), out)


def _redacted_ctx(ctx: Optional[dict]) -> dict:
    """``ctx`` with every quoted text redacted BEFORE it is clipped (a clip can cut a
    secret to a fragment no pattern recognises)."""
    out = dict(ctx or {})
    for key in ("title", "asked", "said", "pending_tool"):
        if out.get(key):
            out[key] = redact_secrets(out[key])
    return out


def card_summary(message: str, ctx: dict, notification_type: str, need: str = "") -> str:
    ctx = _redacted_ctx(ctx)
    message, need = redact_secrets(message), redact_secrets(need)
    lines = ["Needs: %s" % _clip(need or message, 240)]
    if notification_type == "permission_prompt" and ctx.get("pending_tool") \
            and ctx["pending_tool"] not in (need or ""):
        lines.append("Wants to run: %s" % ctx["pending_tool"])
    if ctx.get("asked"):
        lines.append("You asked: %s" % _clip(ctx["asked"], 300))
    if ctx.get("said") and _clip(ctx["said"], 500) not in (need or ""):
        lines.append("Claude last said: %s" % _clip(ctx["said"], 500))
    return redact_secrets("\n".join(lines))[:2000]


def card_title(payload: dict, ctx: Optional[dict] = None) -> str:
    """NOT "Your agent is waiting on you": triage_decision_cards.py closes that
    title as a transient snapshot, which closed every one of these before any
    surface showed it. The project name tells the owner WHICH terminal."""
    cwd = str(payload.get("cwd") or "").strip().rstrip("/\\")
    project = re.split(r"[/\\]", cwd)[-1] if cwd else ""
    title = "Claude Code needs you" + (": %s" % project if project else "")
    ctx = _redacted_ctx(ctx)
    topic = ctx.get("title") or ctx.get("asked") or ""
    if topic and topic != project:
        title += " -- " + _clip(topic, 70)
    return redact_secrets(title)[:200]


def ask_card_parts(questions: list, payload: dict, ctx: Optional[dict] = None,
                   key_prefix: str = "tui", numbered: bool = True) -> dict:
    """Title, summary, facts and options for an AskUserQuestion card.

    The options ARE the first question's options, keyed ``tui:<n>`` with the 1-based
    number the terminal shows, so a surface that types into the session can answer
    it. A multi-select question gets no ``tui:<n>`` (one keystroke toggles, it does
    not answer). ``tui:type`` opens the session; ``later`` changes nothing."""
    ctx = _redacted_ctx(ctx)
    first = questions[0]
    question = redact_secrets(first["question"])
    header = redact_secrets(first.get("header") or "")
    title = ("%s: %s" % (header, question)) if header else question
    cwd = str(payload.get("cwd") or "").strip().rstrip("/\\")
    project = re.split(r"[/\\]", cwd)[-1] if cwd else ""
    lines = ["Needs: %s" % _clip(question, 300)]
    for i, q in enumerate(questions[1:], start=2):
        labels = " / ".join(redact_secrets(o["label"]) for o in q["options"])
        lines.append("Then (%d of %d%s): %s%s" % (
            i, len(questions), ", " + redact_secrets(q["header"]) if q.get("header") else "",
            _clip(redact_secrets(q["question"]), 240),
            " [%s]" % _clip(labels, 200) if labels else ""))
    if ctx.get("asked"):
        lines.append("You asked: %s" % _clip(ctx["asked"], 300))
    facts = ["Claude Code%s is waiting on this question in the terminal." % (
        " (%s)" % project if project else "")]
    if len(questions) > 1:
        facts.append("Question 1 of %d; the other %d need the session (or the next card)."
                     % (len(questions), len(questions) - 1))
    options = []
    if not numbered:
        for n, o in enumerate(first["options"], start=1):
            desc = redact_secrets(o.get("description") or "")
            facts.append(_clip("%d. %s%s" % (n, redact_secrets(o["label"]),
                                             " -- " + desc if desc else ""), 240))
    elif not first.get("multiSelect"):
        for n, o in enumerate(first["options"], start=1):
            label = redact_secrets(o["label"])
            desc = redact_secrets(o.get("description") or "")
            options.append({"key": "%s:%d" % (key_prefix, n), "label": _clip(label, 80),
                            "consequence": _clip(desc or "answers %d in the terminal" % n, 240),
                            "recommended": "(recommended)" in label.lower()})
    else:
        facts.append("Multi-select: pick the answers in the session.")
    options.append({"key": "%s:type" % key_prefix,
                    "label": "Type something" if numbered else "Open the session",
                    "consequence": "opens the session so you can answer it there",
                    "recommended": False})
    options.append({"key": "later", "label": "Not now",
                    "consequence": "the session stays blocked; nothing is lost",
                    "recommended": False})
    return {"title": redact_secrets(_clip(title, 200)),
            "summary": redact_secrets("\n".join(lines))[:2000],
            "facts": [redact_secrets(f) for f in facts], "options": options}


def _open_cards_for(store, session_id: str) -> list:
    if not session_id:
        return []
    return [c for c in store.list(session_id=session_id)
            if (c.dedupe_key or "").startswith(KEY_PREFIX + session_id + ":")]


def raise_card(payload: dict, session_id: str, message: str, need: str = "",
               ask: Optional[dict] = None, call: Optional[dict] = None) -> bool:
    try:
        from awask.store import DecisionCard, DecisionOption, DecisionSource, get_store
    except Exception:
        return False
    try:
        store = get_store()
        kind = str(payload.get("notification_type") or "")
        if ask:
            # The tool_use id makes the same question asked twice two cards (the
            # store refuses a key it has seen, whatever that card's status).
            key = "%s%s:ask-%s" % (KEY_PREFIX, session_id,
                                   fingerprint(session_id, need + "\x00" + ask.get("id", "")))
        elif call:
            key = "%s%s:perm-%s" % (KEY_PREFIX, session_id, fingerprint(
                session_id, need + "\x00" + call.get("tool_use_id", "")))
        else:
            key = (wait_key(session_id) if kind not in BLOCKING_TYPES
                   else "%s%s:perm-%s" % (KEY_PREFIX, session_id,
                                          fingerprint(session_id, need)))
        # One live card per session: the newest wait replaces the older one.
        for old in _open_cards_for(store, session_id):
            if old.dedupe_key != key:
                store.cancel(old.id, note="superseded by a newer wait in the same session")
        ctx = transcript_context(str(payload.get("transcript_path") or ""))
        source = DecisionSource(session_id=session_id, agent="claude-code",
                                cwd=str(payload.get("cwd") or ""))
        if ask:
            # The dialog is already on screen: no card button can reach it (keys
            # typed into Windows Terminal were measured to land nowhere), so the
            # options are listed and the one action is opening the session.
            parts = ask_card_parts(ask["questions"], payload, ctx, numbered=False)
            card = DecisionCard(
                id="", title=parts["title"], summary=parts["summary"], kind="blocked",
                facts=parts["facts"],
                options=[DecisionOption(**o) for o in parts["options"]],
                default_key="later", urgency="high", dedupe_key=key, source=source)
            if session_id:
                card.subject = "session:%s" % session_id
            store.create(card)
            return True
        if call:
            ctx = dict(ctx, pending_tool=call["what"])
            card = DecisionCard(
                id="", title=card_title(payload, ctx),
                summary=card_summary(message, ctx, "permission_prompt", need),
                kind="blocked",
                facts=["Claude Code is asking permission to run exactly this call.",
                       "Approve or refuse it in the session."],
                options=[DecisionOption(**o) for o in permission_options(call)],
                default_key="later", urgency="high", dedupe_key=key, source=source)
            if session_id:
                card.subject = "session:%s" % session_id
            store.create(card)
            return True
        card = DecisionCard(
            id="",
            title=card_title(payload, ctx),
            summary=card_summary(message, ctx, kind, need),
            kind="blocked",
            facts=["This Claude Code session cannot continue until you respond in it.",
                   "The card closes itself when you next type in that session."],
            options=[
                DecisionOption(key="ack", label="I am looking now",
                               consequence="the session keeps waiting for you in the terminal",
                               recommended=True),
                DecisionOption(key="later", label="Not now",
                               consequence="the session stays blocked; nothing is lost"),
            ],
            default_key="ack",
            # high: channels.json gates the Discord DM (the one proven phone path)
            # at urgency >= high. Only real waits get here, once per wait.
            urgency="high",
            dedupe_key=key,
            source=source,
        )
        # The universe object this card is drawn on: the waiting session. Set as an
        # attribute (not a constructor kwarg) so an older store without the field
        # still raises the card; the store normalises an off-grammar id to "".
        if session_id:
            card.subject = "session:%s" % session_id
        store.create(card)
        return True
    except Exception as exc:
        sys.stderr.write("[warn] awask_notification_card: raise failed: %s\n" % exc)
        return False


def resolve(session_id: str) -> int:
    """UserPromptSubmit: the owner is back in this session, so its wait is over."""
    clear_wait(session_id)
    clear_ask(session_id)
    clear_calls(session_id)
    if not session_id:
        return 0
    try:
        from awask.store import get_store
        store = get_store()
        cards = _open_cards_for(store, session_id)
        for c in cards:
            store.cancel(c.id, note="owner answered in the terminal")
        return len(cards)
    except Exception as exc:
        sys.stderr.write("[warn] awask_notification_card: resolve failed: %s\n" % exc)
        return 0


def handle(payload: dict) -> bool:
    """Decide and (maybe) raise. True when a card was raised."""
    session_id = str(payload.get("session_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    if not message or not is_waiting(payload, message):
        return False
    kind = str(payload.get("notification_type") or "").strip()
    ctx = transcript_context(str(payload.get("transcript_path") or ""))
    ask = pending_ask(session_id, ctx)
    if ask:
        need = ask["questions"][0]["question"]
        if recently_raised(fingerprint(session_id, "ask\x00" + ask["id"] + "\x00" + need)):
            return False
        return bool(raise_card(payload, session_id, message, need, ask=ask))
    blocking = kind in BLOCKING_TYPES or (not kind and "permission" in message.lower())
    if blocking and kind != "elicitation_dialog":
        call = waiting_call(session_id, message)
        if call:
            need = "permission to run %s" % call["what"]
            if recently_raised(fingerprint(session_id, "call\x00" + call["tool_use_id"])):
                return False
            return bool(raise_card(payload, session_id, message, need, call=call))
    if blocking:
        tool = ctx.get("pending_tool") or ""
        need = ("permission to run %s" % tool) if tool else message
        if recently_raised(fingerprint(session_id, message + "\x00" + tool)):
            return False
        return bool(raise_card(payload, session_id, message, need))
    # idle: a wait only when Claude asked the owner something and nothing it started
    # in the background is still running.
    need = owner_need(ctx.get("said") or "")
    if not need or ctx.get("bg_pending"):
        return False
    if begin_wait(session_id) is None:
        return False
    return bool(raise_card(payload, session_id, message, need))


def main() -> int:
    payload = read_payload()
    session_id = str(payload.get("session_id") or "").strip()
    if "--resolve" in sys.argv:
        resolve(session_id)
        return _quiet()
    if "--stash-ask" in sys.argv:
        stash_ask(payload)
        decision = hold_answer(payload)
        if decision:
            sys.stdout.write(json.dumps(decision))
            return 0
        return _quiet()
    if "--stash-call" in sys.argv:
        stash_call(payload)
        return _quiet()
    if "--call-done" in sys.argv:
        clear_call(payload)
        return _quiet()
    handle(payload)
    return _quiet()


def self_test() -> int:
    import tempfile

    ok = True

    def check(label, cond):
        nonlocal ok
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        ok = ok and cond

    a = fingerprint("sess-a", "Claude needs your permission")
    check("the same ask in one session is one key", a == fingerprint("sess-a", "Claude needs your permission"))
    check("whitespace and case do not make a new key", a == fingerprint("sess-a", "  CLAUDE NEEDS YOUR PERMISSION "))
    check("a DIFFERENT session is NOT muted", a != fingerprint("sess-b", "Claude needs your permission"))
    check("a different message is not muted", a != fingerprint("sess-a", "something else"))

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        key = fingerprint("s", "m")
        check("the first raise is allowed", recently_raised(key) is False)
        check("an immediate repeat is muted", recently_raised(key) is True)
        check("a wait opens once", begin_wait("w") is not None and begin_wait("w") is None)
        clear_wait("w")
        check("and reopens after the owner answers", begin_wait("w") is not None)

    # Fail CLOSED: an unwritable state dir must mute, never storm.
    os.environ["AITHER_DECISIONS_DIR"] = os.devnull
    check("an unusable state dir mutes rather than storms",
          recently_raised(fingerprint("x", "y")) is True and begin_wait("z") is None)

    check("a permission prompt is an ask", is_waiting({"notification_type": "permission_prompt"}, "x"))
    check("auth_success is not an ask", not is_waiting({"notification_type": "auth_success"}, "x"))
    check("legacy payload: permission text is an ask", is_waiting({}, "Claude needs your permission to use Bash"))
    check("'Waiting on the full gate run' asks nothing",
          owner_need("Waiting on the full gate run.") == "")
    check("'Decide: nothing' asks nothing", owner_need("Done.\nDecide: nothing") == "")
    check("a Decide line is the need", owner_need("Green.\nDecide: merge now?") == "merge now?")
    check("a question is the need",
          owner_need("Built it. Which tenant first?") == "Which tenant first?")
    t = card_title({"cwd": "C:\\AitherOS-Fresh"})
    check("title names the project", t == "Claude Code needs you: AitherOS-Fresh")
    triage = re.compile(r"^(?:\d+\s+things?\s+(?:is|are)|your\s+agent\s+is)\s+waiting on you\s*$", re.I)
    check("title escapes the transient triage rule", not triage.match(t))

    with tempfile.TemporaryDirectory() as tmp:
        tp = Path(tmp) / "t.jsonl"
        recs = [
            {"type": "custom-title", "customTitle": "fix the mail plane"},
            {"type": "user", "message": {"role": "user", "content": "deploy the relay fix"}},
            {"type": "user", "message": {"role": "user", "content": "<task-notification>x"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Restarting the relay now."},
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "systemctl restart aither-relay"}}]}},
        ]
        tp.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
        ctx = transcript_context(str(tp))
        check("context: the owner's ask, not a task notice", ctx.get("asked") == "deploy the relay fix")
        check("context: Claude's last words", ctx.get("said") == "Restarting the relay now.")
        summ = card_summary("Claude needs your permission to use Bash", ctx, "permission_prompt")
        check("summary names the exact command waiting",
              "systemctl restart aither-relay" in summ and "You asked: deploy" in summ)
        check("title carries the session topic",
              card_title({"cwd": "C:\\X"}, ctx) == "Claude Code needs you: X -- fix the mail plane")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        qs = [{"question": "Where is CertTracker?", "header": "CertTracker",
               "multiSelect": False,
               "options": [{"label": "Jason uploaded it", "description": "search uploads"},
                           {"label": "Build it fresh", "description": "token=Zx9Qw81LmN0pRtYv"}]},
              {"question": "Which teaser?", "header": "Teaser",
               "options": [{"label": "Full world pack"}, {"label": "Stills"}]}]
        check("an ask is stashed",
              stash_ask({"tool_name": "AskUserQuestion", "session_id": "sa",
                         "tool_use_id": "tu9", "tool_input": {"questions": qs}}))
        check("another tool is not stashed",
              not stash_ask({"tool_name": "Bash", "session_id": "sa", "tool_input": {}}))
        ask = pending_ask("sa", {})
        check("the stash is the pending ask", bool(ask) and ask["id"] == "tu9")
        check("an answered ask is not pending",
              pending_ask("sa", {"answered_ids": ["tu9"]}) is None)
        parts = ask_card_parts(ask["questions"], {"cwd": "C:\\P"}, {})
        keys = [o["key"] for o in parts["options"]]
        check("options are the terminal's numbers",
              keys == ["tui:1", "tui:2", "tui:type", "later"])
        listed = ask_card_parts(ask["questions"], {}, {}, numbered=False)
        check("a dialog-side card lists options and only opens the session",
              [o["key"] for o in listed["options"]] == ["tui:type", "later"]
              and any(f.startswith("2. Build it fresh") for f in listed["facts"]))
        check("title is the question", parts["title"] == "CertTracker: Where is CertTracker?")
        check("the second question is in the summary", "Which teaser?" in parts["summary"])
        check("descriptions are redacted",
              "Zx9Qw81LmN0pRtYv" not in json.dumps(parts["options"]))
        clear_ask("sa")
        check("resolve clears the stash", pending_ask("sa", {}) is None)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        bash = {"tool_name": "Bash", "session_id": "sp", "tool_use_id": "tb1",
                "tool_input": {"command": "systemctl restart relay --token=Zx9Qw81LmN0pRtYv"}}
        check("a call is stashed", stash_call(bash))
        call = waiting_call("sp", "Claude needs your permission to use Bash")
        check("the one waiting call is named", bool(call) and call["tool"] == "Bash")
        check("the stash is redacted", bool(call) and "Zx9Qw81LmN0pRtYv" not in call["what"])
        check("a message naming another tool falls back",
              waiting_call("sp", "Claude needs your permission to use Write") is None)
        opts = [o["key"] for o in permission_options(call or {})]
        check("options: open the session, later (no Yes it cannot deliver)",
              opts == ["tui:type", "later"])
        check("never 'don't ask again'",
              "ask again" not in json.dumps(permission_options(call or {})).lower())
        stash_call(dict(bash, tool_use_id="tb2"))
        check("two waiting calls fall back", waiting_call("sp", "") is None)
        clear_call(dict(bash, tool_use_id="tb2"))
        check("a finished call stops waiting", waiting_call("sp", "") is not None)
        clear_calls("sp")
        check("resolve clears every call", waiting_call("sp", "") is None)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        os.environ["AWASK_ASK_HOLD"] = "never"
        hq = {"tool_name": "AskUserQuestion", "session_id": "sh", "tool_use_id": "th",
              "tool_input": {"questions": [{"question": "Which?", "header": "H",
                                            "options": [{"label": "A"}, {"label": "B"}]}]}}
        check("a present owner is never held", hold_answer(hq) is None)
        os.environ["AWASK_ASK_HOLD"] = "always"
        try:
            from awask.store import get_store as _gs

            def _answer_then_sleep(_s):
                for c in _gs().list(session_id="sh"):
                    if c.status == "open":
                        _gs().answer(c.id, "ans:2")
            out = hold_answer(hq, sleep=_answer_then_sleep)
            hq2 = dict(hq, tool_use_id="th2", tool_input={"questions": [
                {"question": "One?", "header": "A", "options": [{"label": "x"}, {"label": "y"}]},
                {"question": "Two?", "header": "B", "options": [{"label": "p"}, {"label": "q"}]},
            ]})
            out2 = hold_answer(hq2, sleep=_answer_then_sleep)
            check("a two-question set is sent only once BOTH are answered",
                  bool(out2) and out2["hookSpecificOutput"]["updatedInput"]["answers"]
                  == {"One?": "y", "Two?": "q"})
            check("a card answer becomes updatedInput.answers",
                  bool(out) and out["hookSpecificOutput"]["updatedInput"]["answers"]
                  == {"Which?": "B"}
                  and out["hookSpecificOutput"]["permissionDecision"] == "allow")
        except ImportError:
            check("awask.store importable for the hold arm", False)
        os.environ.pop("AWASK_ASK_HOLD", None)
    check("an unreadable transcript is empty context, not a crash",
          transcript_context(os.path.join(os.devnull, "nope.jsonl")) == {})

    print("self-test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write("[warn] awask_notification_card: %s\n" % exc)
        sys.stdout.write("{}")
        raise SystemExit(0)
