"""Whitelist middleware for the Telegram bot.

Phase 1 runs with a single owner. Any message from a user whose Telegram ID
does not match ``TELEGRAM_OWNER_ID`` is silently dropped: no reply, no
acknowledgement. This keeps the bot's identity opaque to random probers who
stumble across the token. Both authorised and unauthorised attempts are
logged to ``bot_commands`` so we have a forensic trail.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import Update

from kai_trader.config import Settings
from kai_trader.db.client import record_bot_command
from kai_trader.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class CommandContext:
    """Parsed command metadata handed to each handler after auth passes."""

    telegram_user_id: int
    command: str
    args: str | None
    authorized: bool
    audit_row_id: str | None


def _extract(update: Update) -> tuple[int | None, str, str | None]:
    """Pull (user_id, command, args_text) out of an Update. Blank command if missing."""
    user = update.effective_user
    user_id = user.id if user is not None else None

    message = update.effective_message
    text = message.text if message is not None and message.text else ""
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0]  # strip @botname suffix
        args = parts[1] if len(parts) > 1 else None
    else:
        command = ""
        args = text or None
    return user_id, command, args


async def authorize(update: Update, settings: Settings) -> CommandContext | None:
    """Check the update against the whitelist.

    Returns a ``CommandContext`` when the user is authorised. Returns ``None``
    when the user is not authorised (or the update has no user at all); the
    caller should silently stop processing in that case. The attempt is always
    recorded in ``bot_commands``.
    """
    user_id, command, args = _extract(update)

    if user_id is None:
        _log.warning("bot.auth.no_user", update_id=update.update_id)
        return None

    authorized = user_id == settings.telegram_owner_id

    audit_row_id = await record_bot_command(
        telegram_user_id=user_id,
        command=command or "<non-command>",
        args=args,
        authorized=authorized,
    )

    _log.info(
        "bot.command.received",
        telegram_user_id=user_id,
        command=command or "<non-command>",
        authorized=authorized,
    )

    if not authorized:
        _log.warning(
            "bot.auth.rejected",
            telegram_user_id=user_id,
            command=command or "<non-command>",
        )
        return None

    return CommandContext(
        telegram_user_id=user_id,
        command=command,
        args=args,
        authorized=True,
        audit_row_id=audit_row_id,
    )


def user_id_from_update(update: Update) -> int | None:
    """Return the sending user's Telegram ID, or ``None`` if unavailable."""
    user = update.effective_user
    if user is None:
        return None
    return user.id
