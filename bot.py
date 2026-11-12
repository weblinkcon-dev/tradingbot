"""Backward-compatible entry point for the MT5 trading runner.

The project runner lives in :mod:`mt5_auto`.  This small wrapper preserves the
older ``python bot.py --demo`` command while forwarding all other arguments to
the maintained entry point.
"""

import sys

from mt5_auto import main


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "--demo" in arguments:
        arguments = [
            argument
            for argument in arguments
            if argument != "--demo"
        ]
        arguments[0:0] = ["--mode", "auto"]
    sys.argv[1:] = arguments
    main()
