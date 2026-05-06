"""Telegram bot entrypoint.

Builds the python-telegram-bot ``Application``, wires up command handlers,
and blocks on long-polling. All configuration is read via ``get_settings()``.
"""

from __future__ import annotations

import asyncio

from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from kai_trader.bot.handlers import (
    account,
    approval,
    chain,
    chat,
    close,
    flag,
    flags,
    health,
    history,
    income,
    kill,
    notify_test,
    positions,
    quote,
    recent_trades,
    regime,
    sleeves,
    snapshot_now,
    start,
    status,
    strategy_status,
    trade_now,
)
from kai_trader.bot.handlers import help as help_handler
from kai_trader.config import Settings, get_settings
from kai_trader.db.client import close_pool, get_pool
from kai_trader.db.pending_close import cleanup_expired as pending_close_cleanup_expired
from kai_trader.db.readonly import close_readonly_pool
from kai_trader.db.schema_check import assert_schema_up_to_date
from kai_trader.events.dispatcher import EventDispatcher, build_owner_send
from kai_trader.logging import configure_logging, get_logger
from kai_trader.notifications.worker import NotificationWorker
from kai_trader.observability.daily_report import DailyReportWorker
from kai_trader.observability.dependency_probe import assert_dependencies_loadable
from kai_trader.observability.equity_chart import WeeklyEquityChartWorker
from kai_trader.observability.memory_profile import (
    MemoryProfileWorker,
    start_tracemalloc,
)
from kai_trader.observability.snapshot_writer import SnapshotWorker
from kai_trader.strategy.worker import StrategyWorker
from kai_trader.streams.trading_stream import TradingStreamWorker

_worker: NotificationWorker | None = None
_strategy_worker: StrategyWorker | None = None
_event_dispatcher: EventDispatcher | None = None
_trading_stream: TradingStreamWorker | None = None
_memory_profile_worker: MemoryProfileWorker | None = None
_snapshot_worker: SnapshotWorker | None = None
_daily_report_worker: DailyReportWorker | None = None
_weekly_chart_worker: WeeklyEquityChartWorker | None = None


async def _telegram_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Quiet the deploy-crossover Conflict noise; log everything else.

    During a Render deploy crossover the new container starts polling
    Telegram before the old one has been killed. The losing side gets a
    409 Conflict on every getUpdates until Render reaps the old pod.
    Without this handler PTB dumps the full traceback on every failed
    poll, which buries real errors in the logs. The retry logic inside
    PTB still runs; we just downgrade the noise.
    """
    err = context.error
    log = get_logger("bot.main")
    if isinstance(err, Conflict):
        log.warning("telegram.poll.conflict", error=str(err))
        return
    log.exception("telegram.unhandled_error", exc_info=err)


def build_application(settings: Settings) -> Application:  # type: ignore[type-arg]
    """Construct the bot Application with every handler registered.

    HTML parse mode is the default so handlers can use <b>, <i>, <pre>
    without setting parse_mode on every reply call.
    """
    defaults = Defaults(parse_mode=ParseMode.HTML)
    app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .defaults(defaults)
        .build()
    )

    app.add_handler(CommandHandler("start", start.handle))
    app.add_handler(CommandHandler("help", help_handler.handle))
    app.add_handler(CommandHandler("health", health.handle))
    app.add_handler(CommandHandler("status", status.handle))
    app.add_handler(CommandHandler("account", account.handle))
    app.add_handler(CommandHandler("positions", positions.handle))
    app.add_handler(CommandHandler("flags", flags.handle))
    app.add_handler(CommandHandler("flag", flag.handle))
    app.add_handler(CommandHandler("kill", kill.handle))
    app.add_handler(CommandHandler("notify_test", notify_test.handle))
    app.add_handler(CommandHandler("quote", quote.handle))
    app.add_handler(CommandHandler("snapshot_now", snapshot_now.handle))
    app.add_handler(CommandHandler("history", history.handle))
    app.add_handler(CommandHandler("chain", chain.handle))
    app.add_handler(CommandHandler("sleeves", sleeves.handle))
    app.add_handler(CommandHandler("regime", regime.handle))
    app.add_handler(CommandHandler("strategy_status", strategy_status.handle))
    app.add_handler(CommandHandler("trade_now", trade_now.handle))
    app.add_handler(CommandHandler("recent_trades", recent_trades.handle))
    app.add_handler(CommandHandler("income", income.handle))
    app.add_handler(CommandHandler("close", close.handle_close))
    app.add_handler(CommandHandler("close_confirm", close.handle_confirm))

    # Free-form text from the owner is routed to the conversational
    # chat handler. Slash commands are matched by the CommandHandlers
    # above; everything else falls through to chat.handle.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat.handle))
    # Inline-keyboard callbacks. Each handler is scoped to its prefix so
    # the dispatch is unambiguous.
    app.add_handler(CallbackQueryHandler(approval.handle, pattern=r"^pc:"))
    app.add_handler(CallbackQueryHandler(close.handle_callback, pattern=r"^cls:"))

    app.add_error_handler(_telegram_error_handler)

    return app


async def _startup(app: Application) -> None:  # type: ignore[type-arg]
    """Prime DB pool, then spin up notification + strategy + event + stream workers."""
    global _worker, _strategy_worker, _event_dispatcher, _trading_stream
    global _memory_profile_worker, _snapshot_worker, _daily_report_worker
    global _weekly_chart_worker

    # Refuse to start when a required wheel went missing. lxml has
    # done this once already; surface that class of failure at boot
    # rather than at the first time the broken code path runs.
    assert_dependencies_loadable()

    # W-7: enable allocation tracking before the bot starts opening
    # connections so the snapshot worker captures every long-lived
    # object that survives boot. Profiling is permanently on and
    # cheap (one structured log line every hour).
    start_tracemalloc()

    pool = await get_pool()

    # Refuse to start the workers if the live DB is missing any
    # migration. Running with stale schema looks healthy but every tick
    # SQL-errors silently; a real-money bot must fail loud.
    await assert_schema_up_to_date(pool)

    settings = get_settings()
    owner_id = settings.telegram_owner_id

    # W-5: clean any pending_close rows whose TTL elapsed while the bot
    # was offline. Running this once on boot lets the next /close stage
    # for the same key proceed without colliding with a leftover row.
    try:
        cleaned = await pending_close_cleanup_expired()
        if cleaned:
            get_logger("bot.main").info(
                "bot.pending_close.cleaned", rows=cleaned
            )
    except Exception as exc:
        get_logger("bot.main").warning(
            "bot.pending_close.cleanup_failed", error=str(exc)
        )

    async def _send_to_owner(message: str) -> None:
        await app.bot.send_message(chat_id=owner_id, text=message)

    _worker = NotificationWorker(_send_to_owner)
    await _worker.start()

    _strategy_worker = StrategyWorker()
    await _strategy_worker.start()

    _event_dispatcher = EventDispatcher(build_owner_send(app, owner_id))
    await _event_dispatcher.start()

    _trading_stream = TradingStreamWorker(settings=settings)
    await _trading_stream.start()

    _memory_profile_worker = MemoryProfileWorker()
    await _memory_profile_worker.start()

    # Periodic equity snapshot writer. Only persists rows during open
    # market hours; the operator gets a continuous equity curve in the
    # account_snapshots table without having to remember /snapshot_now.
    _snapshot_worker = SnapshotWorker()
    await _snapshot_worker.start()

    # Daily realized-P&L summary auto-posted just before UTC midnight.
    # Reuses the /income builder so the body matches what the operator
    # gets on demand. Disabled via DAILY_REPORT_ENABLED=false.
    _daily_report_worker = DailyReportWorker()
    await _daily_report_worker.start()

    # Weekly equity-curve summary auto-posted at the configured weekday
    # and time (default Mon 00:00 UTC). Renders a Unicode sparkline plus
    # start/end/min/max/% change from account_snapshots.
    _weekly_chart_worker = WeeklyEquityChartWorker()
    await _weekly_chart_worker.start()


async def _shutdown(_app: Application) -> None:  # type: ignore[type-arg]
    global _worker, _strategy_worker, _event_dispatcher, _trading_stream
    global _memory_profile_worker, _snapshot_worker, _daily_report_worker
    global _weekly_chart_worker
    if _weekly_chart_worker is not None:
        await _weekly_chart_worker.stop()
        _weekly_chart_worker = None
    if _daily_report_worker is not None:
        await _daily_report_worker.stop()
        _daily_report_worker = None
    if _snapshot_worker is not None:
        await _snapshot_worker.stop()
        _snapshot_worker = None
    if _memory_profile_worker is not None:
        await _memory_profile_worker.stop()
        _memory_profile_worker = None
    if _trading_stream is not None:
        await _trading_stream.stop()
        _trading_stream = None
    if _event_dispatcher is not None:
        await _event_dispatcher.stop()
        _event_dispatcher = None
    if _strategy_worker is not None:
        await _strategy_worker.stop()
        _strategy_worker = None
    if _worker is not None:
        await _worker.stop()
        _worker = None
    await close_readonly_pool()
    await close_pool()


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    log = get_logger("bot.main")

    health.mark_boot_time()

    app = build_application(settings)
    app.post_init = _startup
    app.post_shutdown = _shutdown

    log.info(
        "bot.starting",
        env=settings.env,
        owner_id=settings.telegram_owner_id,
        timezone=settings.timezone,
    )

    try:
        app.run_polling(stop_signals=None)
    except (KeyboardInterrupt, SystemExit):
        log.info("bot.stopping")
    finally:
        # Ensure the pool is closed even when run_polling returns without post_shutdown.
        try:
            asyncio.run(close_pool())
        except RuntimeError:
            # An event loop is already running or already closed; nothing to do.
            pass


if __name__ == "__main__":
    main()
