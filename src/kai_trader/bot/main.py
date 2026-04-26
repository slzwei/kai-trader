"""Telegram bot entrypoint.

Builds the python-telegram-bot ``Application``, wires up command handlers,
and blocks on long-polling. All configuration is read via ``get_settings()``.
"""

from __future__ import annotations

import asyncio

from telegram.ext import Application, CommandHandler

from kai_trader.bot.handlers import (
    account,
    flag,
    flags,
    health,
    history,
    kill,
    notify_test,
    positions,
    quote,
    snapshot_now,
    start,
    status,
)
from kai_trader.bot.handlers import help as help_handler
from kai_trader.config import Settings, get_settings
from kai_trader.db.client import close_pool, get_pool
from kai_trader.logging import configure_logging, get_logger
from kai_trader.notifications.worker import NotificationWorker

_worker: NotificationWorker | None = None


def build_application(settings: Settings) -> Application:  # type: ignore[type-arg]
    """Construct the bot Application with every handler registered."""
    app = Application.builder().token(settings.telegram_bot_token.get_secret_value()).build()

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

    return app


async def _startup(app: Application) -> None:  # type: ignore[type-arg]
    """Prime DB pool, then spin up the notification delivery worker."""
    global _worker
    await get_pool()

    settings = get_settings()
    owner_id = settings.telegram_owner_id

    async def _send_to_owner(message: str) -> None:
        await app.bot.send_message(chat_id=owner_id, text=message)

    _worker = NotificationWorker(_send_to_owner)
    await _worker.start()


async def _shutdown(_app: Application) -> None:  # type: ignore[type-arg]
    global _worker
    if _worker is not None:
        await _worker.stop()
        _worker = None
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
