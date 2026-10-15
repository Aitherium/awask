#!/usr/bin/env python3
"""Notification hook — turn "the agent is waiting for you" into a card.

Claude Code fires ``Notification`` when a session needs its human: a permission
prompt, or an idle session waiting on input. By default that produces a line in a
terminal nobody is looking at, and a run can sit blocked for forty minutes with
every component perfectly healthy. The signal exists; it just has no reader.

This converts that event into a durable card, so the same "you are the bottleneck"
fact reaches whatever surface the owner actually watches.

**It is deliberately quiet.** A card is an interruption, and a hook that fires on
every notification would train the owner to ignore all of them — which is the same
silence by a louder route. So: one card per session per distinct message, and a
cooldown, and nothing at all for notifications that carry no message.

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

#: Do not re-raise the same notification for this long.
COOLDOWN_SECONDS = float(os.getenv("AWASK_NOTIFY_COOLDOWN", "900"))


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


#: Claude Code notification types that mean "a human must act before this
#: session moves". Anything else (auth_success, ...) is not an ask.
WAITING_TYPES = {"permission_prompt", "idle_prompt", "elicitation_dialog"}

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


def transcript_context(path: str) -> dict:
    """What the owner needs to decide from a phone: what the session is about, what
    they last asked, what Claude last said, and (for a permission prompt) the exact
    tool call waiting. 2026-10-14 owner: the card "doesn't even tell me wtf the
    context or ask is". Every field is optional; a missing/unreadable transcript
    yields {} and the card still raises."""
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
                typed = [t for t in _texts(content) if _owner_typed(t)]
                if typed:
                    ctx["asked"] = typed[-1]
            else:
                said = [t for t in _texts(content) if t.strip()]
                if said:
                    ctx["said"] = said[-1]
                tools = [b for b in (content or []) if isinstance(b, dict)
                         and b.get("type") == "tool_use"]
                ctx["pending_tool"] = _describe_tool(tools[-1]) if tools else ""
    return ctx


def card_summary(message: str, ctx: dict, notification_type: str) -> str:
    lines = ["Waiting: %s" % _clip(message, 300)]
    if notification_type == "permission_prompt" and ctx.get("pending_tool"):
        lines.append("Wants to run: %s" % ctx["pending_tool"])
    if ctx.get("asked"):
        lines.append("You asked: %s" % _clip(ctx["asked"], 300))
    if ctx.get("said"):
        lines.append("Claude last said: %s" % _clip(ctx["said"], 500))
    return "\n".join(lines)[:2000]


def card_title(payload: dict, ctx: Optional[dict] = None) -> str:
    """NOT "Your agent is waiting on you": triage_decision_cards.py closes that
    title as a transient snapshot, which closed every one of these before any
    surface showed it. The project name tells the owner WHICH terminal."""
    cwd = str(payload.get("cwd") or "").strip().rstrip("/\\")
    project = re.split(r"[/\\]", cwd)[-1] if cwd else ""
    title = "Claude Code needs you" + (": %s" % project if project else "")
    topic = (ctx or {}).get("title") or (ctx or {}).get("asked") or ""
    if topic and topic != project:
        title += " -- " + _clip(topic, 70)
    return title[:200]


def _open_cards_for(store, session_id: str) -> list:
    if not session_id:
        return []
    return [c for c in store.list(session_id=session_id)
            if (c.dedupe_key or "").startswith(KEY_PREFIX + session_id + ":")]


def raise_card(payload: dict, session_id: str, message: str) -> bool:
    try:
        from awask.store import DecisionCard, DecisionOption, DecisionSource, get_store
    except Exception:
        return False
    try:
        store = get_store()
        # One live card per session: the newest wait replaces the older one.
        for old in _open_cards_for(store, session_id):
            store.cancel(old.id, note="superseded by a newer wait in the same session")
        ctx = transcript_context(str(payload.get("transcript_path") or ""))
        kind = str(payload.get("notification_type") or "")
        card = DecisionCard(
            id="",
            title=card_title(payload, ctx),
            summary=card_summary(message, ctx, kind),
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
            # at urgency >= high; "normal" only ever badged the desk.
            urgency="high",
            dedupe_key="%s%s:%d" % (KEY_PREFIX, session_id, int(time.time())),
            source=DecisionSource(session_id=session_id, agent="claude-code",
                                  cwd=str(payload.get("cwd") or "")),
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


def main() -> int:
    payload = read_payload()
    session_id = str(payload.get("session_id") or "").strip()
    if "--resolve" in sys.argv:
        resolve(session_id)
        return _quiet()
    message = str(payload.get("message") or "").strip()
    if not message or not is_waiting(payload, message):
        return _quiet()
    if recently_raised(fingerprint(session_id, message)):
        return _quiet()
    raise_card(payload, session_id, message)
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

    # Fail CLOSED: an unwritable state dir must mute, never storm.
    os.environ["AITHER_DECISIONS_DIR"] = os.devnull
    check("an unusable state dir mutes rather than storms",
          recently_raised(fingerprint("x", "y")) is True)

    check("a permission prompt is an ask", is_waiting({"notification_type": "permission_prompt"}, "x"))
    check("auth_success is not an ask", not is_waiting({"notification_type": "auth_success"}, "x"))
    check("legacy payload: permission text is an ask", is_waiting({}, "Claude needs your permission to use Bash"))
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
