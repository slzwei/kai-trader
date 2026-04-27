"""/strategy_status handler: on-demand intent-list display."""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    bold,
    format_sgt_timestamp,
    header,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import get_account
from kai_trader.broker.options_data import get_chain
from kai_trader.config import get_settings
from kai_trader.db.sleeve_config import get_all_sleeves
from kai_trader.db.system_flags import get_all_flags
from kai_trader.strategy.candidates import (
    build_intents_with_diagnostics,
    summarise_intents,
)
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
    intents, diagnostics = await build_intents_with_diagnostics(
        regime=regime,
        sleeves=sleeves,
        account=account,
        chain_fetcher=get_chain,
        today=datetime.now(UTC).date(),
    )

    market_state = "open" if clock.is_open else "closed"
    kill_state = "ENGAGED" if flags.get("kill_switch", False) else "off"
    meta_lines = [
        f"{bold('Market')}: {market_state}",
        f"{bold('Regime')}: {regime.regime} · VIX {regime.vix:.2f}",
        f"{bold('Equity')}: USD {account.equity}",
        f"{bold('Kill switch')}: {kill_state}",
    ]
    parts = [
        header("Strategy Status", ts),
        "\n".join(meta_lines),
        italic("Dry-run preview. The worker submits on its own schedule."),
        "",
    ]
    if intents:
        parts.append(pre(summarise_intents(intents)))
    else:
        parts.append(italic(summarise_intents(intents)))
    for warning in diagnostics.warning_lines():
        parts.append(italic(f"Warning: {warning}"))
    return "\n".join(parts)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
