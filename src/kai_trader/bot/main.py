"""Telegram bot entrypoint.

Builds the python-telegram-bot ``Application``, wires up command handlers,
and blocks on long-polling. All configuration is read via ``get_settings()``.
"""

from __future__ import annotations

import asyncio

from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
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
from kai_trader.events.dispatcher import EventDispatcher, build_owner_send
from kai_trader.logging import configure_logging, get_logger
from kai_trader.notifications.worker import NotificationWorker
from kai_trader.strategy.worker import StrategyWorker
from kai_trader.streams.trading_stream import TradingStreamWorker

_worker: NotificationWorker | None = None
_strategy_worker: StrategyWorker | None = None
_event_dispatcher: EventDispatcher | None = None
_trading_stream: TradingStreamWorker | None = None


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

    return app


async def _startup(app: Application) -> None:  # type: ignore[type-arg]
    """Prime DB pool, then spin up notification + strategy + event + stream workers."""
    global _worker, _strategy_worker, _event_dispatcher, _trading_stream
    await get_pool()

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


async def _shutdown(_app: Application) -> None:  # type: ignore[type-arg]
    global _worker, _strategy_worker, _event_dispatcher, _trading_stream
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
