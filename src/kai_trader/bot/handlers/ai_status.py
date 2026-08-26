"""/ai_status handler: AI decision layer state and today's counters.

Read-only. Shows the configured mode and model, prompt version, and
aggregate counters over today's ``ai_decisions`` rows (takes, rejects,
fail-closed errors, cache hits, average latency, token and estimated
cost totals). Contains no strategy logic; everything shown is read from
config and the audit table.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.ai.prompts import PROMPT_VERSION
from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp, header, pre
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.ai_decisions import decisions_summary


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    head = header("AI Decision Layer", ts)

    lines = [
        f"Mode:            {settings.ai_decision_mode}"
        + ("" if settings.ai_decision_enabled else " (strategy unchanged)"),
        f"Model:           {settings.ai_decision_model}",
        f"Prompt version:  {PROMPT_VERSION}",
        f"Timeouts:        {settings.ai_decision_timeout_seconds:.0f}s/request, "
        f"{settings.ai_decision_tick_budget_seconds:.0f}s/tick",
        f"Concurrency:     {settings.ai_decision_max_concurrency}",
        f"Cache TTL:       {settings.ai_decision_cache_ttl_minutes} min",
        "",
    ]
    try:
        summary = await decisions_summary()
        lines.append("Today (UTC):")
        lines.append(f"  Decisions:     {summary.total}")
        lines.append(f"  TAKE:          {summary.takes}")
        lines.append(f"  REJECT:        {summary.rejects}")
        lines.append(f"  Fail-closed:   {summary.errors}")
        lines.append(f"  Cache hits:    {summary.cache_hits}")
        if summary.avg_latency_ms is not None:
            lines.append(f"  Avg latency:   {summary.avg_latency_ms} ms")
        lines.append(
            f"  Tokens:        {summary.total_input_tokens} in / "
            f"{summary.total_output_tokens} out"
        )
        if summary.total_cost_usd is not None:
            lines.append(f"  Est. cost:     ${summary.total_cost_usd:.4f}")
    except Exception as exc:
        lines.append(f"Today's counters unavailable: {type(exc).__name__}: {exc}")

    return f"{head}\n\n{pre(chr(10).join(lines))}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
