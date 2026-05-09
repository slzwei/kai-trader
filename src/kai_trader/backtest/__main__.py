"""Module entrypoint so `python -m kai_trader.backtest ...` works."""

from __future__ import annotations

import sys

from kai_trader.backtest.cli import main

if __name__ == "__main__":
    sys.exit(main())
