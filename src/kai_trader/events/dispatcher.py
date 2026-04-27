"""Event dispatcher worker.

Same start/stop pattern as the notification worker. Each tick claims a
batch of undispatched events, renders each via
:func:`kai_trader.events.render.render_event`, sends through the supplied
``send`` callable, and marks dispatched. Failures are logged and re-tried
on the next tick because no max_retries column exists on ``events`` (the
event-level retry policy is "keep trying until something changes").
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import InlineKeyboardMarkup

from kai_trader.db import events as events_db
from kai_trader.events.render import render_event
from kai_trader.logging import get_logger

_log = get_logger(__name__)

SendCallable = Callable[
    [str, str, InlineKeyboardMarkup | None],
    Awaitable[None],
]


class EventDispatcher:
    """Drains the events queue."""

    def __init__(
        self,
        send: SendCallable,
        *,
        poll_interval: float = 5.0,
        batch_size: int = 5,
    ) -> None:
        self._send = send
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="events.dispatcher")
        _log.info("events.dispatcher.started", poll_interval=self._poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log.info("events.dispatcher.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.tick()
                if processed == 0:
                    await self._wait_or_stop(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("events.dispatcher.tick_error", error=str(exc))
                await self._wait_or_stop(self._poll_interval)

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def tick(self) -> int:
        rows = await events_db.claim_undispatched(limit=self._batch_size)
        for row in rows:
            await self._dispatch_one(row)
        return len(rows)

    async def _dispatch_one(self, row: events_db.EventRow) -> None:
        try:
            rendered = await render_event(row.kind, row.payload)
        except Exception as exc:
            _log.error(
                "events.dispatcher.render_failed",
                event_id=row.id,
                kind=row.kind,
                error=str(exc),
            )
            return
        if rendered is None:
            await events_db.mark_dispatched(row.id)
            _log.info(
                "events.dispatcher.skipped",
                event_id=row.id,
                kind=row.kind,
                reason="rendered_none",
            )
            return
        try:
            await self._send(rendered.text, rendered.parse_mode, rendered.reply_markup)
        except Exception as exc:
            _log.warning(
                "events.dispatcher.delivery_failed",
                event_id=row.id,
                kind=row.kind,
                error=str(exc),
            )
            return
        await events_db.mark_dispatched(row.id)
        _log.info("events.dispatcher.delivered", event_id=row.id, kind=row.kind)


def build_owner_send(app: Any, owner_id: int) -> SendCallable:
    """Closure used by ``bot.main`` to wire the dispatcher into the bot."""

    async def _send(
        text: str,
        parse_mode: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None:
        await app.bot.send_message(
            chat_id=owner_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    return _send
