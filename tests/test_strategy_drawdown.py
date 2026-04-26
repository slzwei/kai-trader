"""Unit tests for the drawdown circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kai_trader.db.account_snapshots import StoredSnapshot
from kai_trader.strategy import drawdown


def _snap(equity: Decimal, days_ago: int = 0) -> StoredSnapshot:
    when = datetime(2026, 4, 27, 14, tzinfo=UTC) - timedelta(days=days_ago)
    return StoredSnapshot(
        id=f"row-{days_ago}",
        captured_at=when,
        equity=equity,
        last_equity=equity,
        cash=equity,
        buying_power=equity * Decimal("4"),
        portfolio_value=equity,
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
    )


def test_compute_drawdown_no_breach() -> None:
    snaps = [_snap(Decimal("100000"), days_ago=2)]
    check = drawdown.compute_drawdown(snaps, Decimal("99000"))

    assert check.high_water_mark == Decimal("100000")
    assert check.current_equity == Decimal("99000")
    assert check.drawdown_pct == Decimal("1")
    assert check.breached is False


def test_compute_drawdown_at_threshold_breaches() -> None:
    snaps = [_snap(Decimal("100000"))]
    # 7% exact → breached because the rule is >=.
    check = drawdown.compute_drawdown(snaps, Decimal("93000"))
    assert check.breached is True


def test_compute_drawdown_below_threshold_holds() -> None:
    snaps = [_snap(Decimal("100000"))]
    check = drawdown.compute_drawdown(snaps, Decimal("93001"))
    assert check.breached is False


def test_compute_drawdown_uses_current_when_higher() -> None:
    snaps = [_snap(Decimal("90000"))]
    check = drawdown.compute_drawdown(snaps, Decimal("100000"))
    assert check.high_water_mark == Decimal("100000")
    assert check.drawdown_pct == Decimal("0")


def test_compute_drawdown_handles_empty_snapshots() -> None:
    check = drawdown.compute_drawdown([], Decimal("100000"))
    assert check.high_water_mark == Decimal("100000")
    assert check.drawdown_pct == Decimal("0")


def test_compute_drawdown_zero_high_returns_zero() -> None:
    snaps = [_snap(Decimal("0"))]
    check = drawdown.compute_drawdown(snaps, Decimal("0"))
    assert check.breached is False
    assert check.drawdown_pct == Decimal("0")


async def test_check_and_trip_no_breach_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drawdown, "recent_snapshots",
        AsyncMock(return_value=[_snap(Decimal("100000"))]),
    )
    set_flag = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(drawdown, "set_flag", set_flag)
    monkeypatch.setattr(drawdown, "enqueue", enqueue)

    check = await drawdown.check_and_trip(
        current_equity=Decimal("99000"),
        kill_switch_already_on=False,
    )

    assert check.breached is False
    set_flag.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_check_and_trip_fresh_breach_engages_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drawdown, "recent_snapshots",
        AsyncMock(return_value=[_snap(Decimal("100000"))]),
    )
    set_flag = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(drawdown, "set_flag", set_flag)
    monkeypatch.setattr(drawdown, "enqueue", enqueue)

    check = await drawdown.check_and_trip(
        current_equity=Decimal("90000"),
        kill_switch_already_on=False,
    )

    assert check.breached is True
    set_flag.assert_awaited_once_with("kill_switch", True, actor=drawdown.WORKER_ACTOR_ID)
    enqueue.assert_awaited_once()
    args = enqueue.await_args
    assert args.args[1] == "critical"


async def test_check_and_trip_idempotent_when_already_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drawdown, "recent_snapshots",
        AsyncMock(return_value=[_snap(Decimal("100000"))]),
    )
    set_flag = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(drawdown, "set_flag", set_flag)
    monkeypatch.setattr(drawdown, "enqueue", enqueue)

    check = await drawdown.check_and_trip(
        current_equity=Decimal("90000"),
        kill_switch_already_on=True,
    )

    assert check.breached is True
    set_flag.assert_not_awaited()
    enqueue.assert_not_awaited()
