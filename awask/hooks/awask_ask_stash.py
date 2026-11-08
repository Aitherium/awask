#!/usr/bin/env python3
"""PreToolUse(AskUserQuestion) hook -- stash the ask so the Notification card can
mirror its question and options.

Claude Code fires Notification before it flushes the asking turn to the transcript
(measured 2026-11-08: the tool_use record landed ~9 s after the card), so the card
hook cannot read the ask from there. This hook sees ``tool_input`` first.

A separate file (not a flag on awask_notification_card.py) because
``awask install-hooks`` wires one hook file to one event and matches on the
filename. Standard library only; never breaks a turn.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    if "--self-test" not in sys.argv:
        sys.argv.append("--stash-ask")
    runpy.run_path(str(Path(__file__).with_name("awask_notification_card.py")),
                   run_name="__main__")
