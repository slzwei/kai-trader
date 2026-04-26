"""/strategy_status handler: on-demand dry-run intent display.

Runs the same flow the StrategyWorker does on its own schedule, but
replies inline rather than enqueueing a notification. Always evaluates
even when the market is closed (operator can inspect what the worker
would have considered if it were open).
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import get_account
from kai_trader.broker.options_data import get_chain
from kai_trader.config import get_settings
from kai_trader.db.sleeve_config import get_all_sleeves
from kai_trader.db.system_flags import get_all_flags
from kai_trader.strategy.candidates import build_intents, summarise_intents
from kai_trader.strategy.clock import get_clock_snapshot
from kai_trader.strategy.regime import evaluate


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)

    clock = await get_clock_snapshot()
    flags = await get_all_flags()
    regime = await evaluate()
    account = await get_account()
    sleeves = await get_all_sleeves()
    intents = await build_intents(
        regime=regime,
        sleeves=sleeves,
        account=account,
        chain_fetcher=get_chain,
        today=datetime.now(UTC).date(),
    )

    market_state = "open" if clock.is_open else "closed"
    kill_state = "ENGAGED" if flags.get("kill_switch", False) else "off"
    header = (
        f"Strategy status. {ts}\n"
        f"Market: {market_state}\n"
        f"Regime: {regime.regime}, VIX {regime.vix:.2f}\n"
        f"Equity: USD {account.equity}\n"
        f"Kill switch: {kill_state}\n"
        f"Note: dry-run only, no orders are placed in Phase 3.3.\n"
    )
    return header + "\n" + summarise_intents(intents)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
