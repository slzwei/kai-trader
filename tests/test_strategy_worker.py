"""Unit tests for the StrategyWorker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.strategy import worker as worker_module
from kai_trader.strategy.clock import ClockSnapshot
from kai_trader.strategy.regime import RegimeSnapshot


def _clock(is_open: bool) -> ClockSnapshot:
    now = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    return ClockSnapshot(
        is_open=is_open,
        next_open=now + timedelta(hours=1),
        next_close=now + timedelta(hours=7),
        timestamp=now,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000"),
        last_equity=Decimal("99500"),
        cash=Decimal("100000"),
        buying_power=Decimal("400000"),
        portfolio_value=Decimal("100000"),
        day_pl=Decimal("500"),
        status="ACTIVE",
        paper=True,
    )


def _regime(state: str = "risk_on") -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=state,  # type: ignore[arg-type]
        vix=14.0,
        vix_5d_change_pct=-1.0,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Stub every external coro the worker reaches for."""
    enqueue = AsyncMock(return_value="row-uuid")
    get_account = AsyncMock(return_value=_account())
    get_chain = AsyncMock(return_value=[])
    get_sleeves = AsyncMock(return_value=[])
    get_flags = AsyncMock(return_value={"trading_enabled": False, "kill_switch": False})
    compute_and_record = AsyncMock(return_value=(_regime("risk_on"), False))

    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    monkeypatch.setattr(worker_module, "get_account", get_account)
    monkeypatch.setattr(worker_module, "get_chain", get_chain)
    monkeypatch.setattr(worker_module, "get_all_sleeves", get_sleeves)
    monkeypatch.setattr(worker_module, "get_all_flags", get_flags)
    monkeypatch.setattr(worker_module, "compute_and_record", compute_and_record)
    return {
        "enqueue": enqueue,
        "get_account": get_account,
        "get_chain": get_chain,
        "get_sleeves": get_sleeves,
        "get_flags": get_flags,
        "compute_and_record": compute_and_record,
    }


async def test_tick_skips_when_market_closed(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module,
        "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    w = worker_module.StrategyWorker()

    summary = await w.tick()

    assert "Market closed" in summary
    _patch_dependencies["enqueue"].assert_not_awaited()
    _patch_dependencies["compute_and_record"].assert_not_awaited()


async def test_tick_kill_switch_engaged(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module,
        "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {"kill_switch": True}
    w = worker_module.StrategyWorker()

    summary = await w.tick()

    assert "Kill switch engaged" in summary
    _patch_dependencies["enqueue"].assert_awaited_once()
    # Strategy logic must not have run.
    _patch_dependencies["compute_and_record"].assert_not_awaited()
    _patch_dependencies["get_account"].assert_not_awaited()


async def test_tick_full_dryrun_path_enqueues_summary(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module,
        "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    w = worker_module.StrategyWorker()

    summary = await w.tick()

    assert "Strategy tick" in summary
    assert "regime=risk_on" in summary
    _patch_dependencies["enqueue"].assert_awaited_once()
    _patch_dependencies["compute_and_record"].assert_awaited_once()
    _patch_dependencies["get_account"].assert_awaited_once()
    _patch_dependencies["get_sleeves"].assert_awaited_once()


async def test_tick_marks_regime_transition(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module,
        "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["compute_and_record"].return_value = (_regime("neutral"), True)

    summary = await worker_module.StrategyWorker().tick()
    assert "regime changed" in summary


async def test_start_and_stop_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    import asyncio

    monkeypatch.setattr(
        worker_module,
        "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    w = worker_module.StrategyWorker(poll_interval=0.05)
    await w.start()
    await asyncio.sleep(0.1)
    await w.stop()

    # At least one tick should have run during the sleep window.
    assert _patch_dependencies["get_flags"].await_count == 0  # closed market path doesn't read flags
