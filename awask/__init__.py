"""awask — your agent asks you a question, and acts on your answer.

A coding agent that needs a human has exactly one channel today: prose in a
terminal. That channel loses. The important sentence sits in paragraph four of a
dense block, the owner is in another window, and the run sits idle for forty
minutes because nobody knew it was waiting.

awask inverts that. The agent writes a small, structured artifact — headline,
what you need to know, the options, the recommendation, what happens if you say
nothing — into a durable store. Every surface then renders the SAME card: the
terminal, a desktop window, a chat DM, a phone.

Three properties are load-bearing, and each is a rule rather than a nicety:

1. **The card is durable and out-of-process.** It lives in ``~/.aither/decisions``,
   not in a transcript. A card outlives the run that raised it, so closing the
   laptop does not lose the question, and a process in another terminal can raise
   one without this process cooperating at all.

2. **A card always states its default.** "What happens if you ignore this" is the
   field that makes a card safe to ignore. An agent that cannot name a default is
   not blocked on a decision — it is blocked on doing its own thinking.

3. **Answering closes the loop.** The answer is written back to the raising run's
   steering mailbox, which its hooks drain. A card a human answers into a void is
   worse than no card at all, because it reports a resolution that reached nobody.

Zero dependencies — standard library only, on purpose. The store is a directory of
JSON files, so `pip install awask` is the whole setup: no server, no login, no
network, and nothing to run before it works.

Quickstart
----------

    awask ask "Ship the migration now, or hold for review?" \\
        --summary "Tests pass; the rollback path is untested." \\
        --fact "312 rows affected" \\
        --option "ship|Ship it|goes live in ~2 min, rollback unproven" \\
        --option "hold|Hold|no risk, blocks the release" \\
        --recommend hold --default hold

    awask list                 # what is waiting
    awask answer <id> ship     # pick an option
    awask steer <id> "do X first"   # free-form guidance, card stays open

To make cards fire automatically inside Claude Code — including carrying your
answer back into the running session — install the hooks::

    awask install-hooks

See ``awask install-hooks --help`` for what it writes and where.
"""

from __future__ import annotations

from awask.store import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    DecisionCard,
    DecisionOption,
    DecisionStore,
    get_store,
)

__version__ = "0.1.0"

__all__ = [
    "STATUS_ANSWERED",
    "STATUS_CANCELLED",
    "STATUS_EXPIRED",
    "STATUS_OPEN",
    "DecisionCard",
    "DecisionOption",
    "DecisionStore",
    "__version__",
    "get_store",
]
