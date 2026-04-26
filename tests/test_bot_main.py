"""Smoke test for the bot application builder.

We do not start the network loop; just confirm build_application wires up
every handler and that handler modules expose the expected entry points.
"""

from __future__ import annotations

import pytest
from telegram.ext import CommandHandler

from kai_trader.bot import main as bot_main


def test_build_application_registers_commands() -> None:
    from kai_trader.config import get_settings

    app = bot_main.build_application(get_settings())
    # python-telegram-bot stores handlers in a dict keyed by group; default group is 0.
    groups = app.handlers
    handlers = []
    for group in groups.values():
        handlers.extend(group)

    command_names: set[str] = set()
    for h in handlers:
        if isinstance(h, CommandHandler):
            command_names.update(h.commands)

    assert {
        "start", "help", "health", "status", "account",
        "positions", "flags", "flag", "kill", "notify_test", "quote",
        "snapshot_now", "history",
    } <= command_names


@pytest.mark.parametrize(
    "module_name",
    [
        "kai_trader.bot.handlers.start",
        "kai_trader.bot.handlers.help",
        "kai_trader.bot.handlers.health",
        "kai_trader.bot.handlers.status",
        "kai_trader.bot.handlers.account",
        "kai_trader.bot.handlers.positions",
        "kai_trader.bot.handlers.flags",
        "kai_trader.bot.handlers.flag",
        "kai_trader.bot.handlers.kill",
        "kai_trader.bot.handlers.notify_test",
        "kai_trader.bot.handlers.quote",
        "kai_trader.bot.handlers.snapshot_now",
        "kai_trader.bot.handlers.history",
    ],
)
def test_each_handler_module_exports_handle(module_name: str) -> None:
    import importlib

    module = importlib.import_module(module_name)
    assert callable(module.handle)
