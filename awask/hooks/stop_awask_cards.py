#!/usr/bin/env python3
"""Stop hook — hold the turn open while a card raised by THIS session is unanswered.

Without this, answering a card is still useful but always LATE: the answer lands in
the mailbox and reaches the agent at the owner's next prompt, which may be tomorrow.
The agent has already stopped. With it, an answer given inside the wait window
resumes the run immediately — the owner clicks a button and the work continues
without anyone touching the terminal.

**The trigger is "this session has an open card", not a text convention.** The
in-house version of this hook parses a house-style ``LOOP-DONE:`` line out of the
agent's final message, which is precise there and meaningless anywhere else. Keying
on the STORE instead means it works for any agent that raises a card, which is the
only thing a stranger can rely on.

Design constraints, each learned the hard way:

* **Never breaks a turn.** Every failure path prints ``{}`` and exits 0. A hook that
  can wedge a session is worse than no hook.
* **Bounded wait.** ``AWASK_STOP_WAIT`` seconds, default 50 — deliberately under the
  usual 60s hook timeout, because a hook killed mid-wait loses whatever it was about
  to say.
* **Waiting is not required.** Miss the window and nothing is lost; the mailbox drain
  hook delivers the same answer at the next prompt. The wait is an optimisation on
  latency, never the delivery mechanism. Those must not be the same thing, or a
  timeout becomes a lost decision.
* **Silence is not an answer.** If the card is still open when the window closes, the
  turn ends normally. It does NOT apply the default — the default is what happens if
  the owner never answers at all, and that is the store's call at expiry, not a
  hook's guess after 50 seconds.

Installed by ``awask install-hooks``. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import time

WAIT_SECONDS = float(os.getenv("AWASK_STOP_WAIT", "50"))
POLL_SECONDS = 1.0


def _allow() -> int:
    """The only way this hook ends a turn: explicitly, with an empty decision."""
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


def open_cards(session_id: str):
    """Open cards raised by this session, or [] if awask is unavailable."""
    try:
        from awask.store import STATUS_OPEN, get_store
    except Exception:
        return []
    try:
        return get_store().list(status=STATUS_OPEN, session_id=session_id)
    except Exception:
        return []


def wait_for_answer(session_id: str, card_ids, seconds: float):
    """Poll until one of `card_ids` closes. Returns the closed card, or None."""
    try:
        from awask.store import get_store
    except Exception:
        return None
    store = get_store()
    deadline = time.time() + seconds
    while time.time() < deadline:
        for cid in card_ids:
            try:
                card = store.get(cid)
            except Exception:
                continue
            if card is not None and not card.is_open():
                return card
        time.sleep(POLL_SECONDS)
    return None


def instruction_for(card) -> str:
    """What the agent is told to do. Names the OPTION and any free-text note, because
    an instruction that says only "the owner answered" makes the agent go and look."""
    chosen = (getattr(card, "answer", "") or "").strip()
    parts = ["The owner answered your decision card %s." % getattr(card, "id", "?")]
    if chosen:
        opt = None
        try:
            opt = card.option(chosen)
        except Exception:
            opt = None
        label = getattr(opt, "label", "") if opt else ""
        parts.append("They chose: %s%s." % (chosen, (" — %s" % label) if label else ""))
    note = (getattr(card, "answer_note", "") or "").strip()
    if note:
        parts.append("They added: %s" % note)
    parts.append("Act on that now. Do not re-ask.")
    return " ".join(parts)


def main() -> int:
    payload = read_payload()
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return _allow()

    cards = open_cards(session_id)
    if not cards:
        return _allow()

    ids = [getattr(c, "id", "") for c in cards if getattr(c, "id", "")]
    if not ids:
        return _allow()

    answered = wait_for_answer(session_id, ids, WAIT_SECONDS)
    if answered is None:
        # Still open. End the turn — the drain hook delivers whenever they answer.
        return _allow()

    sys.stdout.write(json.dumps({"decision": "block", "reason": instruction_for(answered)}))
    return 0


def self_test() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        ok = ok and cond

    class _Opt:
        label = "Ship it"

    class _Card:
        id = "d-abcd"
        answer = "ship"
        answer_note = "but tag the release first"

        def option(self, key):
            return _Opt() if key == "ship" else None

    text = instruction_for(_Card())
    check("the instruction names the card", "d-abcd" in text)
    check("the instruction names the chosen option", "ship" in text)
    check("the instruction carries the label", "Ship it" in text)
    check("the instruction carries the owner's note", "tag the release first" in text)
    check("the instruction forbids re-asking", "not re-ask" in text)

    class _Bare:
        id = "d-bare"
        answer = ""
        answer_note = ""

        def option(self, key):
            return None

    check("an answer with no option still produces an instruction",
          "d-bare" in instruction_for(_Bare()))

    payload = json.dumps({"decision": "block", "reason": "x"})
    decoded = json.loads(payload)
    check("the block payload uses the documented keys",
          decoded.get("decision") == "block" and bool(decoded.get("reason")))

    # The fail-open path is the whole safety story: no awask, no store, no session
    # must all end the turn normally rather than raise.
    check("no open cards when the store is unreachable", open_cards("nope") == [])
    check("waiting on an unreachable store returns None",
          wait_for_answer("nope", ["d-x"], 0.0) is None)

    print("self-test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # a hook must never kill a turn
        sys.stderr.write("[warn] stop_awask_cards: %s\n" % exc)
        sys.stdout.write("{}")
        raise SystemExit(0)
