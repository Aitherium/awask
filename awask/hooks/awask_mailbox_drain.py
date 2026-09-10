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

#: awfocus asks ride the same mailbox, marked so they are framed as a QUESTION
#: (answer it) rather than a decision answer (act on it). Kept as a literal, not
#: an import — either hook may be installed alone, and awfocus.steer spells the
#: same string; the self-test pins both sides of that contract.
AWFOCUS_MARKER = "<!-- awfocus:question -->"

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


def build_context(blocks: "list[str]") -> str:
    """Frame mailbox bodies for the agent — per-block, by marker.

    Two shapes ride one mailbox: decision-card ANSWERS (framed as decisions to
    act on) and awfocus QUESTIONS (marked `<!-- awfocus:question -->`, framed
    as an owner ask to answer). The frame is the instruction, so mixing them
    under one frame would tell the agent to "act on" a question and to "answer"
    a decision — each block is framed by its own marker.
    """
    decision_blocks = [b for b in blocks if not b.startswith(AWFOCUS_MARKER)]
    question_blocks = []
    for b in blocks:
        if b.startswith(AWFOCUS_MARKER):
            body = b[len(AWFOCUS_MARKER):].strip()
            # Drop the marker's boilerplate paragraph; keep the question text.
            parts = body.split("\n\n", 1)
            body = parts[1].strip() if len(parts) == 2 else parts[0].strip()
            question_blocks.append(body)
    noun = ("a decision card" if len(decision_blocks) == 1
            else ("%d decision cards" % len(decision_blocks)))
    context = ""
    if decision_blocks:
        context += (
            "The owner answered " + noun + " you raised. These are their decisions — "
            "act on them and do not re-ask:\n\n" + "\n\n---\n\n".join(decision_blocks)
        )
    if question_blocks:
        if context:
            context += "\n\n---\n\n"
        context += (
            "The owner asked you directly. Answer it now — this is a question, "
            "not a decision to act on:\n\n" + "\n\n---\n\n".join(question_blocks)
        )
    if len(context) > _MAX_CHARS:
        context = context[:_MAX_CHARS] + "\n\n[truncated]"
    return context


def main() -> int:
    payload = read_payload()
    session_id = str(payload.get("session_id") or "").strip()
    answers = drain(session_id)
    if not answers:
        return 0

    blocks = [body.strip() for _name, body in answers if body.strip()]
    if not blocks:
        return 0

    context = build_context(blocks)
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

    # The two framing shapes must never swap instructions: a decision answer
    # is "act on it", an awfocus question is "answer it".
    ctx = build_context(["ship the red button"])
    check("a decision answer is framed as a decision",
          "act on them and do not re-ask" in ctx and "decisions" in ctx)
    qbody = (AWFOCUS_MARKER + "\n\n" +
             "The owner asked this of you directly. Answer it, and if it needs "
             "a decision you cannot make, say so:\n\n" +
             "which session fixed the tunnel?")
    ctxq = build_context([qbody])
    check("an awfocus question is framed as a question",
          "Answer it now" in ctxq and "which session fixed the tunnel" in ctxq)
    check("the question's boilerplate is stripped, not injected",
          "Answer it, and if it needs" not in ctxq)
    ctxm = build_context(["do the thing", qbody])
    check("mixed mailboxes keep both framings",
          "act on them and do not re-ask" in ctxm and "Answer it now" in ctxm)

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
