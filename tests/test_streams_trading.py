"""Unit tests for the TradingStream worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kai_trader.streams import trading_stream as ts


class _FakeOrder:
    def __init__(
        self,
        *,
        id: str = "alp-1",
        symbol: str = "AMZN260506P00250000",
        side: str = "sell",
        filled_qty: str | None = "1",
        filled_avg_price: str | None = "1.10",
    ) -> None:
        self.id = id
        self.symbol = symbol
        self.side = side
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class _FakeTradeUpdate:
    def __init__(
        self,
        *,
        event: str = "fill",
        order: _FakeOrder | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        self.event = event
        self.order = order or _FakeOrder()
        self.timestamp = timestamp or datetime(2026, 4, 27, 16, 55, tzinfo=UTC)


# ------------- _extract_fill_update -------------


def test_extract_fill_update_happy_path() -> None:
    raw = _FakeTradeUpdate(event="fill")
    update = ts._extract_fill_update(raw)
    assert update is not None
    assert update.event == "fill"
    assert update.alpaca_order_id == "alp-1"
    assert update.symbol == "AMZN260506P00250000"
    assert update.side == "sell"
    assert update.filled_qty == Decimal("1")
    assert update.filled_avg_price == Decimal("1.10")


def test_extract_fill_update_returns_none_without_event() -> None:
    raw = MagicMock()
    raw.event = None
    raw.order = _FakeOrder()
    assert ts._extract_fill_update(raw) is None


def test_extract_fill_update_returns_none_without_order_id() -> None:
    raw = _FakeTradeUpdate()
    raw.order.id = None  # type: ignore[assignment]
    assert ts._extract_fill_update(raw) is None


def test_extract_fill_update_handles_missing_filled_fields() -> None:
    order = _FakeOrder(filled_qty=None, filled_avg_price=None)
    raw = _FakeTradeUpdate(event="canceled", order=order)
    update = ts._extract_fill_update(raw)
    assert update is not None
    assert update.filled_qty is None
    assert update.filled_avg_price is None


# ------------- _map_alpaca_event_to_status -------------


def test_event_to_status_mapping() -> None:
    assert ts._map_alpaca_event_to_status("fill") == "filled"
    assert ts._map_alpaca_event_to_status("canceled") == "cancelled"
    assert ts._map_alpaca_event_to_status("expired") == "cancelled"
    assert ts._map_alpaca_event_to_status("rejected") == "failed"
    assert ts._map_alpaca_event_to_status("partial_fill") is None
    assert ts._map_alpaca_event_to_status("new") is None
    assert ts._map_alpaca_event_to_status("anything_else") is None


# ------------- _format_fill_notification -------------


def test_format_fill_notification_full_fill() -> None:
    update = ts._FillUpdate(
        event="fill",
        alpaca_order_id="alp-1",
        symbol="AMZN260506P00250000",
        side="sell",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("1.10"),
        timestamp=datetime(2026, 4, 27, tzinfo=UTC),
    )
    msg = ts._format_fill_notification(update)
    assert "Fill" in msg
    assert "AMZN260506P00250000" in msg
    assert "sell" in msg
    assert "1" in msg
    assert "1.10" in msg


def test_format_fill_notification_partial_fill_says_partial() -> None:
    update = ts._FillUpdate(
        event="partial_fill",
        alpaca_order_id="alp-1",
        symbol="AMZN260506P00250000",
        side="sell",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("1.10"),
        timestamp=None,
    )
    msg = ts._format_fill_notification(update)
    assert "Partial fill" in msg


# ------------- handler integration -------------


@pytest.fixture
def _patch_apply_and_enqueue(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    apply_mock = AsyncMock(return_value=True)
    enqueue_mock = AsyncMock(return_value="notification-uuid")
    monkeypatch.setattr(ts, "_apply_fill_update", apply_mock)
    monkeypatch.setattr(ts, "enqueue", enqueue_mock)
    return {"apply": apply_mock, "enqueue": enqueue_mock}


async def test_on_trade_update_fill_updates_orders_and_notifies(
    _patch_apply_and_enqueue: dict[str, AsyncMock],
) -> None:
    worker = ts.TradingStreamWorker()
    raw = _FakeTradeUpdate(event="fill")
    await worker._on_trade_update(raw)
    _patch_apply_and_enqueue["apply"].assert_awaited_once()
    _patch_apply_and_enqueue["enqueue"].assert_awaited_once()
    args = _patch_apply_and_enqueue["enqueue"].await_args
    assert "Fill" in args.args[0]


async def test_on_trade_update_partial_fill_notifies(
    _patch_apply_and_enqueue: dict[str, AsyncMock],
) -> None:
    worker = ts.TradingStreamWorker()
    raw = _FakeTradeUpdate(event="partial_fill")
    await worker._on_trade_update(raw)
    _patch_apply_and_enqueue["enqueue"].assert_awaited_once()
    args = _patch_apply_and_enqueue["enqueue"].await_args
    assert "Partial fill" in args.args[0]


async def test_on_trade_update_canceled_does_not_notify(
    _patch_apply_and_enqueue: dict[str, AsyncMock],
) -> None:
    worker = ts.TradingStreamWorker()
    raw = _FakeTradeUpdate(event="canceled")
    await worker._on_trade_update(raw)
    _patch_apply_and_enqueue["apply"].assert_awaited_once()
    _patch_apply_and_enqueue["enqueue"].assert_not_awaited()


async def test_on_trade_update_unparseable_logs_and_swallows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_mock = AsyncMock(return_value=True)
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(ts, "_apply_fill_update", apply_mock)
    monkeypatch.setattr(ts, "enqueue", enqueue_mock)

    worker = ts.TradingStreamWorker()
    bad = MagicMock()
    bad.event = None
    bad.order = None
    await worker._on_trade_update(bad)
    apply_mock.assert_not_awaited()
    enqueue_mock.assert_not_awaited()


async def test_on_trade_update_swallows_handler_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad row in the DB or a transient error must not kill the consumer task."""
    apply_mock = AsyncMock(side_effect=RuntimeError("db down"))
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(ts, "_apply_fill_update", apply_mock)
    monkeypatch.setattr(ts, "enqueue", enqueue_mock)

    worker = ts.TradingStreamWorker()
    raw = _FakeTradeUpdate(event="fill")
    # Should not raise.
    await worker._on_trade_update(raw)
    apply_mock.assert_awaited_once()


# ------------- reconnect / lifecycle -------------


async def test_start_idempotent() -> None:
    """Calling start twice must not spawn a second connect loop."""
    worker = ts.TradingStreamWorker()
    # Replace the internal connect with one that blocks forever.
    blocked = asyncio.Event()

    async def _stub_connect(self: ts.TradingStreamWorker) -> None:
        await blocked.wait()

    worker._connect_and_run = _stub_connect.__get__(worker)  # type: ignore[assignment]
    await worker.start()
    first_task = worker._task
    await worker.start()  # second call must not replace
    assert worker._task is first_task
    blocked.set()
    await worker.stop()


async def test_run_loop_backs_off_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect failure should bump the consecutive-failures counter."""
    worker = ts.TradingStreamWorker()
    fail_count = 0

    async def _failing_connect(self: ts.TradingStreamWorker) -> None:
        nonlocal fail_count
        fail_count += 1
        if fail_count >= 2:
            self._stopping.set()
        raise RuntimeError("nope")

    worker._connect_and_run = _failing_connect.__get__(worker)  # type: ignore[assignment]
    # Fast backoff so the test does not actually wait seconds.
    monkeypatch.setattr(ts, "_BACKOFF_CAP_S", 0.05)
    await worker._run_loop()
    assert fail_count >= 2
    assert worker._consecutive_failures >= 1


async def test_connect_raises_typed_error_when_run_forever_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7: a future alpaca-py upgrade that renames _run_forever must fail loudly.

    The trading stream depends on a private SDK entry point. If a minor
    version bump removes or renames it, we want a typed RuntimeError
    surfaced with a clear log line, not an AttributeError traceback that
    looks like a generic crash.
    """
    fake_settings = MagicMock()
    fake_settings.effective_alpaca_api_key = "key"
    fake_settings.effective_alpaca_secret_key = "secret"
    fake_settings.alpaca_paper = True

    class _FakeStream:
        def subscribe_trade_updates(self, _cb: Any) -> None:
            return None
        # Deliberately omit _run_forever to simulate the SDK rename.

    monkeypatch.setattr(ts, "TradingStream", lambda **_kw: _FakeStream())
    worker = ts.TradingStreamWorker(settings=fake_settings)
    with pytest.raises(RuntimeError, match="_run_forever missing"):
        await worker._connect_and_run()


def test_worker_init_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor must not touch the network. start() does."""
    fake_settings = MagicMock()
    fake_settings.alpaca_api_key.get_secret_value.return_value = "key"
    fake_settings.alpaca_secret_key.get_secret_value.return_value = "secret"
    fake_settings.alpaca_paper = True
    worker = ts.TradingStreamWorker(settings=fake_settings)
    assert worker._task is None
    assert worker._stream is None
    assert worker._connected is False


# ------------- _apply_fill_update database surface -------------


async def test_apply_fill_update_routes_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fill events should set status='filled' alongside fill data."""
    fetched_args: list[Any] = []

    class _FakeConn:
        async def fetchrow(self, sql: str, *args: Any) -> Any:
            fetched_args.append((sql, args))
            return {"id": "row-1"}

    class _FakePoolCM:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *a: Any) -> None:
            return None

    class _FakePool:
        def acquire(self) -> _FakePoolCM:
            return _FakePoolCM()

    monkeypatch.setattr(ts, "get_pool", AsyncMock(return_value=_FakePool()))

    update = ts._FillUpdate(
        event="fill",
        alpaca_order_id="alp-1",
        symbol="AMZN260506P00250000",
        side="sell",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("1.10"),
        timestamp=datetime(2026, 4, 27, tzinfo=UTC),
    )
    ok = await ts._apply_fill_update(update)
    assert ok is True
    sql, args = fetched_args[0]
    assert "set status" in sql
    assert args[1] == "filled"


async def test_apply_fill_update_partial_fill_skips_status_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial fills should NOT change status, only update fill data."""
    fetched: list[str] = []

    class _FakeConn:
        async def fetchrow(self, sql: str, *args: Any) -> Any:
            fetched.append(sql)
            return {"id": "row-1"}

    class _FakePoolCM:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *a: Any) -> None:
            return None

    class _FakePool:
        def acquire(self) -> _FakePoolCM:
            return _FakePoolCM()

    monkeypatch.setattr(ts, "get_pool", AsyncMock(return_value=_FakePool()))

    update = ts._FillUpdate(
        event="partial_fill",
        alpaca_order_id="alp-1",
        symbol="AMZN260506P00250000",
        side="sell",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("1.10"),
        timestamp=None,
    )
    ok = await ts._apply_fill_update(update)
    assert ok is True
    sql = fetched[0]
    assert "set status" not in sql.lower()
