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
import sys
import time
from pathlib import Path

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
    env = os.getenv("AWASK_DIR", "").strip()
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


def raise_card(session_id: str, message: str) -> bool:
    try:
        from awask.store import DecisionCard, DecisionOption, DecisionSource, get_store
    except Exception:
        return False
    try:
        card = DecisionCard(
            title="Your agent is waiting on you",
            summary=message.strip()[:2000],
            facts=["This session cannot continue until you respond."],
            options=[
                DecisionOption(key="ack", label="I am looking now",
                               consequence="the session keeps waiting for you in the terminal"),
                DecisionOption(key="later", label="Not now",
                               consequence="the session stays blocked; nothing is lost"),
            ],
            recommend="ack",
            default="ack",
            urgency="normal",
            source=DecisionSource(session_id=session_id),
        )
        get_store().create(card)
        return True
    except Exception:
        return False


def main() -> int:
    payload = read_payload()
    message = str(payload.get("message") or "").strip()
    if not message:
        return _quiet()
    session_id = str(payload.get("session_id") or "").strip()
    if recently_raised(fingerprint(session_id, message)):
        return _quiet()
    raise_card(session_id, message)
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
        os.environ["AWASK_DIR"] = tmp
        key = fingerprint("s", "m")
        check("the first raise is allowed", recently_raised(key) is False)
        check("an immediate repeat is muted", recently_raised(key) is True)

    # Fail CLOSED: an unwritable state dir must mute, never storm.
    os.environ["AWASK_DIR"] = os.devnull
    check("an unusable state dir mutes rather than storms",
          recently_raised(fingerprint("x", "y")) is True)

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
