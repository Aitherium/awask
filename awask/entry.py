"""Console-script entry point.

``awask.cli`` is GENERATED from the upstream decisions plane, so it must not be
hand-edited to grow an ``install-hooks`` subcommand -- the next regeneration would
revert it, and the parity gate would go red in the meantime. This thin overlay
intercepts the awask-only subcommand and delegates everything else unchanged.

Keeping the seam here rather than in the generated file is the whole point: the
generated modules stay a pure rename of their source, so a drift between the two
trees is always a real drift and never this.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] == "install-hooks":
        from awask.install import main as install_main

        return install_main(args[1:])

    from awask.cli import main as cli_main

    # The generated CLI reads sys.argv itself, so hand it the same list rather than
    # passing arguments it does not accept.
    if argv is not None:
        sys.argv = [sys.argv[0]] + args
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
