#!/usr/bin/env python3
"""PostToolUse(any tool) hook -- the call ran, so remove it from the waiting set
awask_tool_stash.py keeps.

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
        sys.argv.append("--call-done")
    runpy.run_path(str(Path(__file__).with_name("awask_notification_card.py")),
                   run_name="__main__")
