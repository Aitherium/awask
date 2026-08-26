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

#: A "waiting on you" card is TRANSIENT state — it is only true while the session
#: waits. Without a deadline these cards accumulate forever: measured 2026-08-25,
#: 643 stale waiting-cards drowned a 30-card queue and froze the popup. The card
#: self-expires after this long, and the mailbox-drain hook cancels it the moment
#: the owner responds in the session (see ``session_ledger``).
TTL_SECONDS = float(os.getenv("AWASK_NOTIFY_TTL", "7200"))


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


def session_ledger(session_id: str) -> Path:
    """Where the ids of waiting-cards raised for one session are recorded, so the
    mailbox-drain hook can cancel them the moment the owner responds there."""
    safe = "".join(ch for ch in (session_id or "unknown") if ch.isalnum() or ch in "._-")
    return state_dir() / "by-session" / ("%s.ids" % (safe or "unknown"))


def supersede_previous(store, session_id: str) -> None:
    """Cancel this session's still-open waiting cards before raising a new one.

    Dedup by (session, message) alone lets one idle session park TWO standing
    cards ("waiting for your input" + "needs your permission"), and a dozen idle
    sessions kept the queue at 9+ notices — climbing, not draining (measured
    2026-08-25, an hour after the TTL fix). A waiting notice describes ONE
    session's current state; there is never a reason for a session to hold two.
    """
    ledger = session_ledger(session_id)
    if not ledger.is_file():
        return
    try:
        ids = ledger.read_text(encoding="utf-8").split()
    except OSError:
        return
    for card_id in ids:
        try:
            card = store.get(card_id)
            if card is not None and card.is_open:
                store.cancel(card_id,
                             note="superseded by a newer waiting notice from the same session")
        except Exception as exc:
            sys.stderr.write("[warn] awask_notification_card: supersede: %s\n" % exc)


def raise_card(session_id: str, message: str) -> bool:
    try:
        from awask.store import (
            DecisionCard,
            DecisionOption,
            DecisionSource,
            console_tab_title,
            get_store,
        )
    except Exception:
        return False
    try:
        supersede_previous(get_store(), session_id)
    except Exception as exc:
        sys.stderr.write("[warn] awask_notification_card: supersede pass: %s\n" % exc)
    try:
        # The card FACE must say WHICH session: the owner runs a dozen tabs, and
        # a toast reading "Claude is waiting for your input" with no identity is
        # noise they cannot act on (owner report, 2026-08-25: "I NEED MORE
        # CONTEXT THAN THIS"). The hook runs inside the session's own console,
        # so the tab title and cwd are simply true here.
        tab = console_tab_title()
        cwd = ""
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
        title = ("Waiting on you — %s" % tab) if tab else "Your agent is waiting on you"
        facts = ["This session cannot continue until you respond."]
        if cwd:
            facts.append("working directory: %s" % cwd)
        if session_id:
            facts.append("session: %s" % session_id[:12])
        # session_pid must be resolved HERE, at raise time: the card's terminal
        # controls ("Go to that terminal") act on it, and it must be a process
        # that is still ALIVE when the owner finally clicks. This hook's own
        # interpreter exits in milliseconds, so the walk starts at the hook's
        # PARENT — the session process that spawned it — and skips plumbing
        # (resolve_owner_pid's whole job; see the cli._resolve_session_pid
        # docstring for the dead-pid failure this prevents). Measured
        # 2026-08-25: waiting cards raised WITHOUT this field showed
        # "Go to that terminal — unavailable — no session process recorded on
        # this card" on EVERY card, which is precisely the class this hook
        # raises — the owner's most common card could never reach its terminal.
        session_pid = 0
        try:
            from awask.winproc import resolve_owner_pid

            session_pid = resolve_owner_pid(os.getppid())
        except Exception:
            session_pid = os.getppid() or 0
        card = DecisionCard(
            id="",  # minted by the store on create
            title=title,
            summary=message.strip()[:2000],
            facts=facts,
            options=[
                DecisionOption(key="ack", label="I am looking now",
                               consequence="the session keeps waiting for you in the terminal",
                               recommended=True),
                DecisionOption(key="later", label="Not now",
                               consequence="the session stays blocked; nothing is lost"),
            ],
            default_key="ack",
            # LOW on purpose: a waiting notice is the least urgent card class,
            # and the channel bridges gate on min_urgency (default "normal") —
            # at "normal" these DM'd the owner's Discord every few minutes
            # (measured 2026-08-25: three DMs in eight minutes, one per idle
            # session), which is the channel-destroying noise the doctrine
            # bans. Low keeps them in the store/tray/queue and out of DMs.
            urgency="low",
            deadline=time.time() + TTL_SECONDS,
            source=DecisionSource(session_id=session_id, cwd=cwd, tab_title=tab,
                                  session_pid=session_pid),
        )
        get_store().create(card)
    except Exception:
        return False
    # Record the id so a resume cancels the card. Failing to record must not
    # fail the raise — the TTL still bounds the card's life.
    try:
        ledger = session_ledger(session_id)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(card.id + "\n")
    except OSError as exc:
        sys.stderr.write("[warn] awask_notification_card: ledger write: %s\n" % exc)
    return True


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
    check("the same ask in one session is one key",
          a == fingerprint("sess-a", "Claude needs your permission"))
    check("whitespace and case do not make a new key",
          a == fingerprint("sess-a", "  CLAUDE NEEDS YOUR PERMISSION "))
    check("a DIFFERENT session is NOT muted",
          a != fingerprint("sess-b", "Claude needs your permission"))
    check("a different message is not muted", a != fingerprint("sess-a", "something else"))

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        key = fingerprint("s", "m")
        check("the first raise is allowed", recently_raised(key) is False)
        check("an immediate repeat is muted", recently_raised(key) is True)

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        raised = raise_card("sess-ttl", "Claude needs your permission")
        check("a card can be raised", raised)
        cards = []
        try:
            from awask.store import DecisionStore
            cards = DecisionStore(Path(tmp)).list()
        except Exception as exc:
            print("  note: store unavailable for the TTL arms: %s" % exc)
        check("the raised card is open", len(cards) == 1)
        check("a waiting card carries a TTL deadline — transient state must expire",
              bool(cards) and cards[0].deadline is not None)
        check("a waiting card is LOW urgency, so a min_urgency=normal DM bridge skips it",
              bool(cards) and cards[0].urgency == "low")
        check("the card face names WHERE it came from (cwd fact)",
              bool(cards) and any("working directory:" in f for f in cards[0].facts))
        ledger = session_ledger("sess-ttl")
        check("the raise is recorded in the session ledger so a resume can cancel it",
              bool(cards) and ledger.is_file()
              and cards[0].id in ledger.read_text(encoding="utf-8"))

        # One waiting card per session, EVER: a second raise (different message,
        # so the cooldown does not mute it) must supersede the first.
        raise_card("sess-ttl", "Claude needs a different thing now")
        try:
            open_after = [c for c in DecisionStore(Path(tmp)).list()
                          if c.source.session_id == "sess-ttl"]
        except Exception:
            open_after = None
        check("a second waiting notice supersedes the first — one card per session",
              open_after is not None and len(open_after) == 1
              and open_after[0].summary.startswith("Claude needs a different thing"))

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = tmp
        os.environ["AITHER_TAB_TITLE"] = "my-session-tab"
        try:
            import awask.store as store_module
            store_module._STORE = None  # rebind the process singleton to THIS tmp
            raise_card("sess-tab", "Claude needs your permission")
            got = store_module.DecisionStore(Path(tmp)).list()
            check("with a known tab title, the TITLE names the session — a dozen "
                  "anonymous toasts are noise",
                  bool(got) and got[0].title == "Waiting on you — my-session-tab")
            # The terminal row is what the owner acts on: a card without a
            # session_pid renders "Go to that terminal — unavailable — no
            # session process recorded on this card" (measured 2026-08-25 on
            # every waiting card). The hook's parent is alive during the
            # self-test, so the raise-time resolution must land a real pid.
            check("the card records a session pid at RAISE time, so the "
                  "terminal row can reach the session",
                  bool(got) and got[0].source.session_pid > 0)
        finally:
            os.environ.pop("AITHER_TAB_TITLE", None)

    # Fail CLOSED: an unwritable state dir must mute, never storm.
    os.environ["AITHER_DECISIONS_DIR"] = os.devnull
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
