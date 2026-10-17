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
            else:
                said = [t for t in _texts(content) if t.strip()]
                if said:
                    ctx["said"] = said[-1]
                tools = [b for b in (content or []) if isinstance(b, dict)
                         and b.get("type") == "tool_use"]
                if tools:
                    ctx["pending_tool"] = _describe_tool(tools[-1])
                elif said:
                    ctx["pending_tool"] = ""
                launched += sum(1 for b in tools if _is_background(b))
    ctx["bg_pending"] = max(launched - done, 0)
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


def _open_cards_for(store, session_id: str) -> list:
    if not session_id:
        return []
    return [c for c in store.list(session_id=session_id)
            if (c.dedupe_key or "").startswith(KEY_PREFIX + session_id + ":")]


def raise_card(payload: dict, session_id: str, message: str, need: str = "") -> bool:
    try:
        from awask.store import DecisionCard, DecisionOption, DecisionSource, get_store
    except Exception:
        return False
    try:
        store = get_store()
        kind = str(payload.get("notification_type") or "")
        key = (wait_key(session_id) if kind not in BLOCKING_TYPES
               else "%s%s:perm-%s" % (KEY_PREFIX, session_id, fingerprint(session_id, need)))
        # One live card per session: the newest wait replaces the older one.
        for old in _open_cards_for(store, session_id):
            if old.dedupe_key != key:
                store.cancel(old.id, note="superseded by a newer wait in the same session")
        ctx = transcript_context(str(payload.get("transcript_path") or ""))
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
    clear_wait(session_id)
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
    blocking = kind in BLOCKING_TYPES or (not kind and "permission" in message.lower())
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
