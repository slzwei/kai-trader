"""Push-based fill notifications via Alpaca's TradingStream.

Subscribes to ``trade_updates`` over WebSocket. On each event:

1. Updates the matching ``orders`` row by ``alpaca_order_id`` (status,
   filled_at, filled_avg_price).
2. Enqueues a Telegram notification for fill / partial_fill events so
   Shawn sees executions within seconds of the broker reporting them.

Reconnects with exponential backoff if the socket drops. Logs a
heartbeat every 60s while connected so silent failures are visible. The
periodic ``_reconcile_pending`` in the strategy worker remains as a
belt-and-suspenders catchup if the stream misses anything.

This worker does NOT replace the existing reconcile path; it
augments it. The strategy worker still calls ``_reconcile_pending`` at
each tick. If the stream is healthy, the reconcile is a no-op (rows
already up to date). If the stream is dead, the reconcile catches up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from alpaca.trading.stream import TradingStream

from kai_trader.config import Settings, get_settings
from kai_trader.db.client import get_pool
from kai_trader.db.orders import OrderStatus
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue

_log = get_logger(__name__)

_HEARTBEAT_INTERVAL_S = 60.0
_BACKOFF_CAP_S = 60.0
_NOTIFY_REASONS_FILL = {"fill", "partial_fill"}
_TERMINAL_EVENTS = {"fill", "canceled", "expired", "rejected", "replaced"}


def _map_alpaca_event_to_status(event: str) -> OrderStatus | None:
    """Translate an Alpaca trade_update event name to our status vocab.

    Returns ``None`` for events that should not change the orders row
    status (new, accepted, pending_*, etc.).
    """
    if event == "fill":
        return "filled"
    if event in {"canceled", "expired"}:
        return "cancelled"
    if event == "rejected":
        return "failed"
    if event == "partial_fill":
        # Stay 'submitted' until the full fill arrives.
        return None
    return None


def _to_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class _FillUpdate:
    """Narrow view of the fields we read from a TradeUpdate."""

    event: str
    alpaca_order_id: str
    symbol: str
    side: str | None
    filled_qty: Decimal | None
    filled_avg_price: Decimal | None
    timestamp: datetime | None


def _extract_fill_update(data: Any) -> _FillUpdate | None:
    """Pull the fields we need out of an Alpaca TradeUpdate object.

    The alpaca-py TradeUpdate has ``event``, ``order``, ``timestamp``.
    The ``order`` is an Order with ``id``, ``symbol``, ``side``,
    ``filled_qty``, ``filled_avg_price``. We never trust the raw object
    shape beyond these fields; missing attributes return ``None``.
    """
    event = getattr(data, "event", None)
    order = getattr(data, "order", None)
    if event is None or order is None:
        return None
    alpaca_order_id = getattr(order, "id", None)
    if alpaca_order_id is None:
        return None
    return _FillUpdate(
        event=str(event),
        alpaca_order_id=str(alpaca_order_id),
        symbol=str(getattr(order, "symbol", "") or ""),
        side=str(getattr(order, "side", "") or "") or None,
        filled_qty=_to_decimal_or_none(getattr(order, "filled_qty", None)),
        filled_avg_price=_to_decimal_or_none(getattr(order, "filled_avg_price", None)),
        timestamp=getattr(data, "timestamp", None),
    )


async def _apply_fill_update(update: _FillUpdate) -> bool:
    """Update the orders row matching this alpaca_order_id.

    Returns True when a row was updated. Returns False when no row
    matches (the order was placed outside our system or before we
    were running).
    """
    new_status = _map_alpaca_event_to_status(update.event)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if new_status is None:
            # Partial fill or non-status event: just update fill data.
            row = await conn.fetchrow(
                """
                update orders
                   set filled_avg_price = coalesce($2, filled_avg_price),
                       filled_at = coalesce($3, filled_at)
                 where alpaca_order_id = $1
                returning id
                """,
                update.alpaca_order_id,
                update.filled_avg_price,
                update.timestamp,
            )
        else:
            row = await conn.fetchrow(
                """
                update orders
                   set status = $2,
                       filled_avg_price = coalesce($3, filled_avg_price),
                       filled_at = coalesce($4, filled_at)
                 where alpaca_order_id = $1
                returning id
                """,
                update.alpaca_order_id,
                new_status,
                update.filled_avg_price,
                update.timestamp,
            )
    return row is not None


def _format_fill_notification(update: _FillUpdate) -> str:
    side = update.side or "?"
    qty = update.filled_qty if update.filled_qty is not None else "?"
    price = update.filled_avg_price if update.filled_avg_price is not None else "?"
    if update.event == "partial_fill":
        prefix = "Partial fill"
    else:
        prefix = "Fill"
    return f"{prefix}: {update.symbol} {side} {qty} @ {price}"


SendNotification = Callable[[str], Awaitable[Any]]


class TradingStreamWorker:
    """Manages the Alpaca TradingStream WebSocket lifecycle.

    Owns one persistent connection. On each trade_update, applies the
    corresponding orders-table mutation and (for fills) enqueues a
    Telegram notification via the existing notifications producer.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._stream: TradingStream | None = None
        self._connected = False
        self._consecutive_failures = 0
        self._last_event_at: datetime | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="streams.trading")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name="streams.trading.heartbeat"
        )
        _log.info("streams.trading.started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._stream is not None:
            try:
                await self._stream.stop_ws()
            except Exception as exc:
                _log.warning("streams.trading.stop_ws_failed", error=str(exc))
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log.info("streams.trading.stopped")

    async def _run_loop(self) -> None:
        """Top-level reconnect loop. Recreates the stream on each disconnect."""
        while not self._stopping.is_set():
            try:
                await self._connect_and_run()
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._consecutive_failures += 1
                _log.error(
                    "streams.trading.disconnected",
                    error=str(exc),
                    consecutive_failures=self._consecutive_failures,
                )
            if self._stopping.is_set():
                break
            await self._wait_backoff()

    async def _connect_and_run(self) -> None:
        cfg = self._settings
        self._stream = TradingStream(
            api_key=cfg.effective_alpaca_api_key,
            secret_key=cfg.effective_alpaca_secret_key,
            paper=cfg.alpaca_paper,
        )
        self._stream.subscribe_trade_updates(self._on_trade_update)
        self._connected = True
        _log.info("streams.trading.connected", paper=cfg.alpaca_paper)
        # alpaca-py marks _run_forever as private but it is the official
        # async entrypoint; the public ``run`` is sync and would block.
        # B7: detect explicitly when an SDK upgrade renames or removes
        # this attribute so the operator sees a typed error instead of
        # an attribute-error traceback. The pyproject pin keeps the
        # blast radius small; this guard makes the failure mode loud
        # if someone bumps the pin without noticing the rename.
        run_forever = getattr(self._stream, "_run_forever", None)
        if run_forever is None or not callable(run_forever):
            _log.error(
                "streams.trading.run_forever_missing",
                detail=(
                    "alpaca-py TradingStream._run_forever is missing or "
                    "not callable. Likely an SDK upgrade renamed it. "
                    "Pin pyproject back or update streams/trading_stream.py."
                ),
            )
            raise RuntimeError(
                "alpaca-py TradingStream._run_forever missing; SDK upgrade?"
            )
        await run_forever()
        self._connected = False

    async def _wait_backoff(self) -> None:
        delay = min(_BACKOFF_CAP_S, 2 ** self._consecutive_failures)
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _heartbeat(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=_HEARTBEAT_INTERVAL_S
                )
                return
            except TimeoutError:
                pass
            silent_seconds: float | None = None
            if self._last_event_at is not None:
                silent_seconds = (
                    datetime.now(UTC) - self._last_event_at
                ).total_seconds()
            _log.info(
                "streams.trading.heartbeat",
                connected=self._connected,
                consecutive_failures=self._consecutive_failures,
                seconds_since_last_event=silent_seconds,
            )

    async def _on_trade_update(self, data: Any) -> None:
        """Single entry point for every trade_update event.

        Wrapped with a broad try/except so a single bad message never
        kills the consumer task.
        """
        try:
            self._last_event_at = datetime.now(UTC)
            update = _extract_fill_update(data)
            if update is None:
                _log.warning(
                    "streams.trading.update_unparseable",
                    raw=str(data)[:200],
                )
                return
            row_updated = await _apply_fill_update(update)
            _log.info(
                "streams.trading.event_received",
                trade_event=update.event,
                alpaca_order_id=update.alpaca_order_id,
                symbol=update.symbol,
                row_updated=row_updated,
            )
            if update.event in _NOTIFY_REASONS_FILL:
                await enqueue(
                    _format_fill_notification(update),
                    "info",
                    channel="telegram",
                )
        except Exception as exc:
            _log.error("streams.trading.handler_error", error=str(exc))
