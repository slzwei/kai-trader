"""Mock-driven smoke test mirroring ``scripts/test_drawdown_trip.py``.

The companion script proves the drawdown breaker against real Postgres. This
test exercises the same orchestration in-process with the DB layer stubbed
out, so CI catches regressions without needing live credentials. Together
they form belt-and-braces evidence that ``check_and_trip`` actually trips
the kill switch and emits a critical notification on a 7% drawdown.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kai_trader.db.account_snapshots import StoredSnapshot

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "test_drawdown_trip.py"
)


def _load_script() -> ModuleType:
    """Import ``scripts/test_drawdown_trip.py`` as a fresh module each call.

    We bypass the standard import system because pytest's ``testpaths`` is
    scoped to ``tests/`` and pytest must not auto-collect the script under
    its ``test_`` filename. A clean spec_from_file_location load gives us a
    private module to monkeypatch without polluting ``sys.modules``.
    """
    spec = importlib.util.spec_from_file_location(
        "drawdown_dry_run_script", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    """In-memory stand-in for an asyncpg connection.

    Resolves queries by substring match so the script's exact SQL strings
    are not coupled to the test. Only the four SQL operations the script
    issues are modelled.
    """

    def __init__(
        self,
        *,
        now_value: datetime,
        critical_rows: list[dict[str, Any]],
    ) -> None:
        self.now_value = now_value
        self.critical_rows = critical_rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "now()" in query:
            return self.now_value
        raise AssertionError(f"unexpected fetchval: {query!r}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "notifications" in query and "critical" in query:
            return self.critical_rows
        raise AssertionError(f"unexpected fetch: {query!r}")

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        # Imitate asyncpg's "UPDATE n" / "DELETE n" status strings so the
        # script's row-count parsing path is exercised.
        if query.strip().lower().startswith("update"):
            return "UPDATE 1"
        if query.strip().lower().startswith("delete"):
            return "DELETE 1"
        return "OK"


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


def _stored_snapshot(equity: Decimal, days_ago: int = 0) -> StoredSnapshot:
    when = datetime(2026, 5, 6, 14, tzinfo=UTC) - timedelta(days=days_ago)
    return StoredSnapshot(
        id=f"snap-{days_ago}",
        captured_at=when,
        equity=equity,
        last_equity=equity,
        cash=equity,
        buying_power=equity * Decimal("4"),
        portfolio_value=equity,
        day_pl=Decimal("0"),
        status="DRAWDOWN_TEST",
        paper=True,
    )


@pytest.fixture
def script_with_mocks(monkeypatch: pytest.MonkeyPatch) -> tuple[
    ModuleType, dict[str, Any]
]:
    """Load the script and stub every external dependency it touches."""
    monkeypatch.setenv("ALPACA_PAPER", "true")

    module = _load_script()

    # Pool with the synthetic snapshot already "inserted" so check_and_trip
    # can compute a 7% breach against the script's HWM_EQUITY.
    now_ts = datetime(2026, 5, 6, 14, tzinfo=UTC)
    fake_conn = _FakeConn(
        now_value=now_ts,
        critical_rows=[
            {
                "id": "00000000-0000-0000-0000-000000000abc",
                "message": (
                    "*DRAWDOWN CIRCUIT BREAKER*\n7.00% drop from 10000000 "
                    "to 9300000.\nKill switch engaged automatically."
                ),
                "created_at": now_ts,
            }
        ],
    )
    fake_pool = _FakePool(fake_conn)

    get_pool_mock = AsyncMock(return_value=fake_pool)
    close_pool_mock = AsyncMock(return_value=None)
    record_snapshot_mock = AsyncMock(
        return_value="11111111-1111-1111-1111-111111111111"
    )

    flags_state: dict[str, bool] = {
        "trading_enabled": False,
        "new_entries_enabled": False,
        "kill_switch": False,
    }

    async def fake_get_all_flags() -> dict[str, bool]:
        return dict(flags_state)

    async def fake_set_flag(key: str, value: bool, *, actor: int) -> bool:
        prior = flags_state.get(key, False)
        flags_state[key] = value
        return prior

    enqueue_mock = AsyncMock(return_value="22222222-2222-2222-2222-222222222222")

    # Patch on the script's namespace so its `from X import Y` references
    # resolve to the stubs when main() calls them.
    monkeypatch.setattr(module, "get_pool", get_pool_mock)
    monkeypatch.setattr(module, "close_pool", close_pool_mock)
    monkeypatch.setattr(module, "record_snapshot", record_snapshot_mock)
    monkeypatch.setattr(module, "get_all_flags", fake_get_all_flags)
    monkeypatch.setattr(module, "set_flag", fake_set_flag)

    # check_and_trip itself runs for real, but its DB-touching deps are
    # stubbed so it does not try to open a Postgres pool.
    from kai_trader.strategy import drawdown as drawdown_mod

    recent_mock = AsyncMock(
        return_value=[_stored_snapshot(Decimal("10000000.00"))]
    )
    drawdown_set_flag_mock = AsyncMock(return_value=False)

    async def drawdown_set_flag_proxy(
        key: str, value: bool, *, actor: int
    ) -> bool:
        # Mirror the writes through the same flags_state so the script's
        # post-trip get_all_flags() observes the breaker's effect.
        prior = flags_state.get(key, False)
        flags_state[key] = value
        drawdown_set_flag_mock(key, value, actor=actor)
        return prior

    monkeypatch.setattr(drawdown_mod, "recent_snapshots", recent_mock)
    monkeypatch.setattr(drawdown_mod, "set_flag", drawdown_set_flag_proxy)
    monkeypatch.setattr(drawdown_mod, "enqueue", enqueue_mock)

    return module, {
        "flags_state": flags_state,
        "fake_conn": fake_conn,
        "get_pool": get_pool_mock,
        "close_pool": close_pool_mock,
        "record_snapshot": record_snapshot_mock,
        "enqueue": enqueue_mock,
        "drawdown_set_flag": drawdown_set_flag_mock,
        "recent_snapshots": recent_mock,
    }


async def test_main_passes_when_breaker_trips_and_restores_state(
    script_with_mocks: tuple[ModuleType, dict[str, Any]],
) -> None:
    module, mocks = script_with_mocks

    rc = await module.main()

    assert rc == 0
    # Synthetic HWM snapshot was written.
    mocks["record_snapshot"].assert_awaited_once()
    snapshot_arg = mocks["record_snapshot"].await_args.args[0]
    assert snapshot_arg.equity == module.HWM_EQUITY
    assert snapshot_arg.status == module.SYNTHETIC_STATUS
    assert snapshot_arg.paper is True

    # The breaker enqueued a critical notification with the marker.
    mocks["enqueue"].assert_awaited_once()
    body, priority = (
        mocks["enqueue"].await_args.args[0],
        mocks["enqueue"].await_args.args[1],
    )
    assert priority == "critical"
    assert module.NOTIFICATION_MARKER in body

    # The breaker flipped kill_switch to True via the drawdown set_flag.
    mocks["drawdown_set_flag"].assert_called_once_with(
        "kill_switch", True, actor=-1
    )

    # The script issued the expected SQL: an UPDATE that suppresses the
    # synthetic critical notification before the live worker can deliver
    # it, and DELETEs that remove the synthetic snapshot and notification.
    executed = mocks["fake_conn"].executed
    assert any("update notifications" in q and "sent_at = now()" in q
               for q, _ in executed)
    assert any("delete from account_snapshots" in q and "id" in q
               for q, _ in executed)
    assert any("delete from account_snapshots" in q and "status" in q
               for q, _ in executed)
    assert any("delete from notifications" in q and "message like" in q
               for q, _ in executed)

    # State was restored: kill_switch ends back at the initial False.
    assert mocks["flags_state"]["kill_switch"] is False

    # Pool was closed at the end of main().
    mocks["close_pool"].assert_awaited_once()


async def test_main_refuses_to_run_when_paper_flag_is_off(
    script_with_mocks: tuple[ModuleType, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, mocks = script_with_mocks

    # Force the settings cache to reflect ALPACA_PAPER=false. The script
    # imports get_settings from kai_trader.config and the cache is owned
    # there, so reset it after flipping the env var.
    from kai_trader import config as config_module

    monkeypatch.setenv("ALPACA_PAPER", "false")
    config_module.reset_settings_cache()

    rc = await module.main()

    assert rc == 2
    mocks["record_snapshot"].assert_not_awaited()
    mocks["enqueue"].assert_not_awaited()
    mocks["drawdown_set_flag"].assert_not_called()


async def test_main_restores_state_even_when_breaker_does_not_breach(
    script_with_mocks: tuple[ModuleType, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the breaker fails to trip we must still restore flags + DB rows.

    This guards against a regression where the breaker silently no-ops,
    the script reports a failed assertion, and the operator is left with
    a synthetic snapshot polluting their account history. Cleanup must
    run unconditionally inside the finally block.
    """
    module, mocks = script_with_mocks

    # Replace recent_snapshots with one whose HWM matches DOWN_EQUITY so the
    # drawdown computation comes out at 0% and the breaker does not trip.
    from kai_trader.strategy import drawdown as drawdown_mod

    monkeypatch.setattr(
        drawdown_mod,
        "recent_snapshots",
        AsyncMock(return_value=[_stored_snapshot(module.DOWN_EQUITY)]),
    )
    # No breaker fire means no critical notification in the queue.
    mocks["fake_conn"].critical_rows = []

    rc = await module.main()

    assert rc == 1  # at least one assertion failed
    mocks["enqueue"].assert_not_awaited()
    # State still gets restored.
    assert mocks["flags_state"]["kill_switch"] is False
    mocks["close_pool"].assert_awaited_once()
    executed = mocks["fake_conn"].executed
    assert any("delete from account_snapshots" in q for q, _ in executed)
