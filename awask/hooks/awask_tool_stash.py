#!/usr/bin/env python3
"""PreToolUse(any tool) hook -- record the call as waiting (one small redacted file
per call) so a permission card can name exactly the call it is about.

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
        sys.argv.append("--stash-call")
    runpy.run_path(str(Path(__file__).with_name("awask_notification_card.py")),
                   run_name="__main__")
