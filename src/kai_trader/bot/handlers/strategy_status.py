"""/strategy_status handler: on-demand intent-list preview.

This is a dry-run view of what the strategy would submit if it ticked
right now. The visual style mirrors the periodic tick notification:
section-bold headings, an Account block, and Notes for any diagnostic
warnings. The Preview section replaces the tick's "This tick" body.
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    bold,
    format_money,
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
    trading_state = "on" if flags.get("trading_enabled", False) else "off"

    sections: list[str] = []
    subtitle = f"{ts} . {regime.regime} regime . VIX {regime.vix:.1f}"
    sections.append(header("Strategy Status - Preview", subtitle))
    sections.append(italic(
        "Dry-run only. The worker submits on its own schedule."
    ))

    account_lines = [
        f"Equity        {format_money(account.equity)}",
        f"Market        {market_state}",
        f"Trading       {trading_state}",
        f"Kill switch   {kill_state}",
    ]
    sections.append(bold("Account") + "\n" + pre("\n".join(account_lines)))

    if intents:
        sections.append(bold("Preview") + "\n" + pre(summarise_intents(intents)))
    else:
        sections.append(
            bold("Preview") + "\n" + italic(summarise_intents(intents))
        )

    warning_lines = diagnostics.warning_lines()
    if warning_lines:
        sections.append(bold("Notes") + "\n" + pre("\n".join(warning_lines)))

    return "\n\n".join(sections)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
