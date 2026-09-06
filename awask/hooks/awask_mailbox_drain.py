#!/usr/bin/env python3
"""UserPromptSubmit hook — deliver answered cards into this session.

**This is the hook that closes the loop, and the one whose absence is invisible.**
Answering a card records the choice and writes it to ``~/.aither/steer/<session-id>/``.
Without this hook that file sits there and the agent that asked never learns the
answer — so the card renders, the owner clicks, the store records a resolution, and
the run carries on as though nobody replied. Every component reports success.

An interactive Claude Code tab has no IPC: its TUI cannot be written to from outside.
Its HOOKS can, and UserPromptSubmit runs before the agent sees the turn. So the
mailbox is the inbound channel and this is the door.

Installed by ``awask install-hooks``. Standard library only, and it never breaks a
turn: every failure path exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

#: A session id reaches the filesystem as a directory name; validate its SHAPE rather
#: than trusting it, so a malformed payload cannot walk out of the mailbox.
_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: Cap the injected text. A pathological mailbox must not blow up the turn.
_MAX_CHARS = 8000


def steer_root() -> Path:
    """Where answers land. Same default and same env var as the rest of the family,
    so a card raised through one tool is drained by another without configuration."""
    env = os.getenv("AITHER_STEER_DIR", "").strip()
    return Path(env) if env else (Path.home() / ".aither" / "steer")


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


def drain(session_id: str) -> list:
    """Return (filename, body) per pending answer, moving each to delivered/."""
    if not session_id or not _SESSION_RE.match(session_id):
        return []
    box = steer_root() / session_id
    if not box.is_dir():
        return []

    out = []
    delivered_dir = box / "delivered"
    for target in sorted(box.glob("*.md")):
        try:
            body = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append((target.name, body))
        try:
            delivered_dir.mkdir(parents=True, exist_ok=True)
            # A collision here would silently drop the record of a delivered answer,
            # so the destination is made unique rather than overwritten.
            destination = delivered_dir / target.name
            if destination.exists():
                destination = delivered_dir / ("%d-%s" % (int(time.time() * 1000), target.name))
            os.replace(target, destination)
        except OSError:
            # Could not archive it. Better to re-inject an answer than to LOSE one,
            # so the file is left in place and gets picked up next prompt.
            continue
    return out


def main() -> int:
    payload = read_payload()
    session_id = str(payload.get("session_id") or "").strip()
    answers = drain(session_id)
    if not answers:
        return 0

    blocks = [body.strip() for _name, body in answers if body.strip()]
    if not blocks:
        return 0

    noun = "a decision card" if len(blocks) == 1 else ("%d decision cards" % len(blocks))
    context = (
        "The owner answered " + noun + " you raised. These are their decisions — "
        "act on them and do not re-ask:\n\n" + "\n\n---\n\n".join(blocks)
    )
    if len(context) > _MAX_CHARS:
        context = context[:_MAX_CHARS] + "\n\n[truncated]"

    sys.stdout.write(json.dumps({"context": context, "action": "add_context"}))
    return 0


def self_test() -> int:
    import tempfile

    ok = True

    def check(label, cond):
        nonlocal ok
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        ok = ok and cond

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_STEER_DIR"] = tmp
        box = Path(tmp) / "sess-1"
        box.mkdir()
        (box / "001.md").write_text("ship it", encoding="utf-8")

        got = drain("sess-1")
        check("drains a pending answer", len(got) == 1 and "ship it" in got[0][1])
        check("archives it so it is not re-injected", not (box / "001.md").exists())
        check("a second drain returns nothing", drain("sess-1") == [])
        check("refuses a traversal session id", drain("../../etc") == [])
        check("refuses an empty session id", drain("") == [])
        check("an unknown session drains nothing", drain("sess-none") == [])

        # The archive must never silently overwrite a previous delivery.
        (box / "001.md").write_text("second answer", encoding="utf-8")
        drain("sess-1")
        delivered = sorted((box / "delivered").glob("*.md"))
        check("a same-named second answer is kept, not clobbered", len(delivered) == 2)

    print("self-test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never break a session over an answer
        sys.stderr.write("[warn] awask_mailbox_drain: %s\n" % exc)
        raise SystemExit(0)
