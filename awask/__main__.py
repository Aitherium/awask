"""``python -m awask`` — the same entry point as the ``awask`` console script.

A stranger who has the package but not its script on PATH (a virtualenv that was
never activated, a hook running under a bare interpreter, a CI step) otherwise has
no way in at all: ``python -m awask`` is the form people try first, and without
this file it answers "No module named awask.__main__" — which reads as a broken
install rather than a missing shim.

It delegates to ``awask.entry`` rather than ``awask.cli`` so that BOTH doors offer
the same subcommands, ``install-hooks`` included.
"""

from __future__ import annotations

from awask.entry import main

if __name__ == "__main__":
    raise SystemExit(main())
