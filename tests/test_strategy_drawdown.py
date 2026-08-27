"""Unit tests for the drawdown circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kai_trader.db.account_snapshots import StoredSnapshot
from kai_trader.strategy import drawdown


def _snap(equity: Decimal, days_ago: int = 0) -> StoredSnapshot:
    # Anchor to ``now`` so the snapshot always falls inside the 7-day
    # drawdown lookback regardless of when the suite runs. The B9 fix
    # changed the cutoff from "snapshots[0].timestamp - 7d" to
    # "now - 7d", which made hard-coded calendar fixtures age out.
    when = datetime.now(UTC) - timedelta(days=days_ago)
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
        entries_enabled=True,
    )

    assert check.breached is False
    set_flag.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_check_and_trip_fresh_breach_engages_entry_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh breach flips new_entries_enabled off and notifies. It must
    never touch kill_switch: the freeze stops risk-taking, not the system."""
    monkeypatch.setattr(
        drawdown, "recent_snapshots",
        AsyncMock(return_value=[_snap(Decimal("100000"))]),
    )
    set_flag = AsyncMock(return_value=True)  # prior value: entries were on
    enqueue = AsyncMock()
    monkeypatch.setattr(drawdown, "set_flag", set_flag)
    monkeypatch.setattr(drawdown, "enqueue", enqueue)

    check = await drawdown.check_and_trip(
        current_equity=Decimal("90000"),
        entries_enabled=True,
    )

    assert check.breached is True
    set_flag.assert_awaited_once_with(
        "new_entries_enabled", False, actor=drawdown.WORKER_ACTOR_ID
    )
    enqueue.assert_awaited_once()
    args = enqueue.await_args
    assert args.args[1] == "critical"
    body = args.args[0]
    assert "DRAWDOWN CIRCUIT BREAKER" in body
    assert "new_entries_enabled" in body
    assert "kill_switch" not in body


async def test_check_and_trip_idempotent_when_already_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries already off (prior trip or operator preference): the breach
    is logged but nothing is re-set and nothing is re-notified, so a
    multi-day breach cannot flood Telegram or churn the flag audit row."""
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
        entries_enabled=False,
    )

    assert check.breached is True
    set_flag.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_check_and_trip_skips_notify_when_freeze_race_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploy-crossover race: a twin process froze the flag between our
    flags read and our write. set_flag reports prior=False, so this
    process must not send a duplicate critical notification."""
    monkeypatch.setattr(
        drawdown, "recent_snapshots",
        AsyncMock(return_value=[_snap(Decimal("100000"))]),
    )
    set_flag = AsyncMock(return_value=False)  # someone else already froze it
    enqueue = AsyncMock()
    monkeypatch.setattr(drawdown, "set_flag", set_flag)
    monkeypatch.setattr(drawdown, "enqueue", enqueue)

    check = await drawdown.check_and_trip(
        current_equity=Decimal("90000"),
        entries_enabled=True,
    )

    assert check.breached is True
    set_flag.assert_awaited_once()
    enqueue.assert_not_awaited()
