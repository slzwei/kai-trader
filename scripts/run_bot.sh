#!/bin/bash
# Launch the Telegram bot in the foreground.
set -e
cd "$(dirname "$0")/.."
uv run python -m kai_trader.bot.main
