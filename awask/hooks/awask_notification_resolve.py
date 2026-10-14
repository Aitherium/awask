#!/usr/bin/env python3
"""UserPromptSubmit hook -- the owner is typing in this session again, so the
"Claude Code needs you" card its Notification hook raised is withdrawn.

A separate file (not a flag on awask_notification_card.py) because
``awask install-hooks`` wires one hook file to one event and matches on the
filename. Standard library only; never breaks a turn.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.argv.append("--resolve")
    runpy.run_path(str(Path(__file__).with_name("awask_notification_card.py")),
                   run_name="__main__")
