"""Unit tests for the Alpaca broker wrapper.

The underlying alpaca-py TradingClient is patched out so these tests never
make network calls. The integration test in test_integration_alpaca.py covers
the live paper API.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any
from unittest.mock import MagicMock

import pytest

from kai_trader.broker import alpaca as broker


class _FakeStatus(Enum):
    ACTIVE = "ACTIVE"


class _FakeSide(Enum):
    LONG = "long"


class _FakeAccount:
    def __init__(self) -> None:
        self.equity = "100000"
        self.last_equity = "99500"
        self.cash = "100000"
        self.buying_power = "400000"
        self.portfolio_value = "100000"
        self.status = _FakeStatus.ACTIVE


class _FakePosition:
    def __init__(
        self,
        *,
        symbol: str = "AAPL",
        qty: str = "100",
        side: _FakeSide = _FakeSide.LONG,
        avg_entry_price: str = "150",
        current_price: str | None = "152.5",
        market_value: str | None = "15250",
        unrealized_pl: str | None = "250",
        unrealized_intraday_pl: str | None = "100",
    ) -> None:
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price
        self.market_value = market_value
        self.unrealized_pl = unrealized_pl
        self.unrealized_intraday_pl = unrealized_intraday_pl


@pytest.fixture(autouse=True)
def _reset_client() -> Any:
    broker.reset_client()
    yield
    broker.reset_client()


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(broker, "_get_client", lambda settings=None: client)


async def test_get_account_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_account.return_value = _FakeAccount()
    _install_fake_client(monkeypatch, fake)

    snapshot = await broker.get_account()

    assert snapshot.equity == Decimal("100000")
    assert snapshot.last_equity == Decimal("99500")
    assert snapshot.day_pl == Decimal("500")
    assert snapshot.cash == Decimal("100000")
    assert snapshot.buying_power == Decimal("400000")
    assert snapshot.status == "ACTIVE"
    assert snapshot.paper is True


async def test_get_account_handles_missing_numerics(monkeypatch: pytest.MonkeyPatch) -> None:
    account = _FakeAccount()
    account.equity = None  # type: ignore[assignment]
    account.last_equity = None  # type: ignore[assignment]
    fake = MagicMock()
    fake.get_account.return_value = account
    _install_fake_client(monkeypatch, fake)

    snapshot = await broker.get_account()

    assert snapshot.equity == Decimal("0")
    assert snapshot.day_pl == Decimal("0")


async def test_get_account_rejects_raw_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_account.return_value = {"raw": "data"}
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="raw dict"):
        await broker.get_account()


async def test_list_positions_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = []
    _install_fake_client(monkeypatch, fake)

    assert await broker.list_positions() == []


async def test_list_positions_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = [
        _FakePosition(),
        _FakePosition(
            symbol="MSFT",
            qty="50",
            avg_entry_price="400",
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    _install_fake_client(monkeypatch, fake)

    positions = await broker.list_positions()

    assert len(positions) == 2
    aapl = positions[0]
    assert aapl.symbol == "AAPL"
    assert aapl.qty == Decimal("100")
    assert aapl.side == "long"
    assert aapl.current_price == Decimal("152.5")
    assert aapl.unrealized_pl == Decimal("250")

    msft = positions[1]
    assert msft.symbol == "MSFT"
    assert msft.current_price is None
    assert msft.unrealized_pl is None


async def test_list_positions_rejects_raw_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = {"raw": "data"}
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="raw dict"):
        await broker.list_positions()


async def test_ping_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.return_value = MagicMock()
    _install_fake_client(monkeypatch, fake)

    assert await broker.ping() is True


async def test_ping_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.side_effect = RuntimeError("boom")
    _install_fake_client(monkeypatch, fake)

    assert await broker.ping() is False


def test_get_client_caches_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(broker, "_build_client", fake_build)

    one = broker._get_client()
    two = broker._get_client()
    assert one is two
    assert len(builds) == 1


def test_reset_client_forces_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(broker, "_build_client", fake_build)

    broker._get_client()
    broker.reset_client()
    broker._get_client()
    assert len(builds) == 2


# ------------- submit_short_put gating -------------

async def test_submit_short_put_refuses_when_kill_switch_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": True, "trading_enabled": True}),
    )

    result = await broker.submit_short_put(
        option_symbol="SPY260501P00500000",
        qty=1,
        limit_price=Decimal("1.10"),
    )

    assert result.submitted is False
    assert result.reason == "kill_switch_engaged"
    fake.submit_order.assert_not_called()


async def test_submit_short_put_refuses_when_trading_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": False,
            "new_entries_enabled": True,
        }),
    )

    result = await broker.submit_short_put(
        option_symbol="SPY260501P00500000",
        qty=1,
        limit_price=Decimal("1.10"),
    )

    assert result.submitted is False
    assert result.reason == "trading_disabled"
    fake.submit_order.assert_not_called()


async def test_submit_short_put_refuses_when_new_entries_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": False,
        }),
    )

    result = await broker.submit_short_put(
        option_symbol="SPY260501P00500000",
        qty=1,
        limit_price=Decimal("1.10"),
    )

    assert result.submitted is False
    assert result.reason == "new_entries_disabled"
    fake.submit_order.assert_not_called()


async def test_submit_short_put_submits_when_flags_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    class _FakeOrder:
        id = "alpaca-uuid"
        status = "accepted"

    fake = MagicMock()
    fake.submit_order.return_value = _FakeOrder()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": True,
        }),
    )

    result = await broker.submit_short_put(
        option_symbol="SPY260501P00500000",
        qty=1,
        limit_price=Decimal("1.10"),
        client_order_id="kai-12345678",
    )

    assert result.submitted is True
    assert result.alpaca_order_id == "alpaca-uuid"
    assert result.order_status == "accepted"
    fake.submit_order.assert_called_once()
    request = fake.submit_order.call_args.args[0]
    assert request.symbol == "SPY260501P00500000"
    assert request.qty == 1
    assert request.limit_price == 1.10
    assert request.client_order_id == "kai-12345678"


async def test_submit_short_put_handles_alpaca_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order.side_effect = RuntimeError("alpaca down")
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": True,
        }),
    )

    result = await broker.submit_short_put(
        option_symbol="SPY260501P00500000",
        qty=1,
        limit_price=Decimal("1.10"),
    )

    assert result.submitted is False
    assert result.reason == "submit_exception"
    assert result.error == "alpaca down"


async def test_submit_short_put_classifies_known_error_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An APIError carrying a known code should map to a typed reason."""
    from decimal import Decimal
    from unittest.mock import AsyncMock

    class _FakeAPIError(Exception):
        def __init__(self, code: int, message: str) -> None:
            super().__init__(message)
            self.code = code

    fake = MagicMock()
    fake.submit_order.side_effect = _FakeAPIError(
        40310000, "insufficient options buying power"
    )
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": True,
        }),
    )

    result = await broker.submit_short_put(
        option_symbol="AMZN260501P00250000",
        qty=1,
        limit_price=Decimal("4.55"),
    )

    assert result.submitted is False
    assert result.reason == "insufficient_options_buying_power"
    assert result.error == "insufficient options buying power"


async def test_classify_submit_error_falls_back_to_generic() -> None:
    """Unknown error codes (and exceptions without `code`) stay generic."""
    class _FakeAPIError(Exception):
        def __init__(self, code: int) -> None:
            super().__init__("???")
            self.code = code

    assert broker._classify_submit_error(_FakeAPIError(99999999)) == "submit_exception"
    assert broker._classify_submit_error(RuntimeError("boom")) == "submit_exception"


async def test_close_position_refused_when_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.close_position = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": True, "trading_enabled": True}),
    )

    result = await broker.close_position("SPY")
    assert result.submitted is False
    assert result.reason == "kill_switch_engaged"
    fake.close_position.assert_not_called()


async def test_close_position_submits_when_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    class _FakeOrder:
        id = "alpaca-uuid"
        status = "accepted"

    fake = MagicMock()
    fake.close_position.return_value = _FakeOrder()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": False, "trading_enabled": False}),
    )

    # Closes are allowed even when trading_enabled is off.
    result = await broker.close_position("SPY")
    assert result.submitted is True
    assert result.alpaca_order_id == "alpaca-uuid"
    fake.close_position.assert_called_once_with("SPY")


async def test_close_position_handles_alpaca_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.close_position.side_effect = RuntimeError("alpaca down")
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": False, "trading_enabled": True}),
    )

    result = await broker.close_position("SPY")
    assert result.submitted is False
    assert result.reason == "close_exception"
    assert result.error == "alpaca down"


async def test_close_position_detects_position_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.close_position.side_effect = RuntimeError(
        '{"code":40410000,"message":"position not found: SPY"}'
    )
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": False, "trading_enabled": True}),
    )

    result = await broker.close_position("SPY")
    assert result.submitted is False
    assert result.reason == "position_not_found"
    assert result.error is None


def test_is_position_not_found_table() -> None:
    assert broker._is_position_not_found('{"code":40410000,"message":"oops"}')
    assert broker._is_position_not_found("APIError: position not found: SPY")
    assert not broker._is_position_not_found("403 forbidden")
    assert not broker._is_position_not_found("network down")


def test_is_stale_connection_table() -> None:
    assert broker._is_stale_connection("RemoteDisconnected('...')")
    assert broker._is_stale_connection("Connection aborted.")
    assert broker._is_stale_connection("Connection reset by peer")
    assert broker._is_stale_connection("BadStatusLine")
    assert not broker._is_stale_connection("403 forbidden")


async def test_call_alpaca_retries_once_on_stale_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-call ConnectionError triggers a client reset and a single retry."""
    builds: list[int] = []

    class _FlakyClient:
        def __init__(self, fail_first: bool) -> None:
            self.fail_first = fail_first
            self.calls = 0

        def get_account(self) -> str:
            self.calls += 1
            if self.fail_first and self.calls == 1:
                raise ConnectionError(
                    "('Connection aborted.', RemoteDisconnected('eof'))"
                )
            return "ok"

    instances: list[_FlakyClient] = []

    def fake_build(_cfg: Any) -> _FlakyClient:
        builds.append(1)
        # First instance fails on call; replacement after reset succeeds.
        client = _FlakyClient(fail_first=len(instances) == 0)
        instances.append(client)
        return client

    monkeypatch.setattr(broker, "_build_client", fake_build)

    result = await broker._call_alpaca_with_retry("get_account")
    assert result == "ok"
    assert len(builds) == 2  # first build, retry built a fresh one
    assert instances[0].calls == 1  # first instance called once and failed
    assert instances[1].calls == 1  # retry instance called once and succeeded


async def test_call_alpaca_does_not_retry_on_unrelated_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[int] = []

    class _AlwaysFails:
        def get_account(self) -> str:
            raise RuntimeError("403 forbidden")

    def fake_build(_cfg: Any) -> _AlwaysFails:
        builds.append(1)
        return _AlwaysFails()

    monkeypatch.setattr(broker, "_build_client", fake_build)

    with pytest.raises(RuntimeError, match="403 forbidden"):
        await broker._call_alpaca_with_retry("get_account")
    assert len(builds) == 1  # no retry, no rebuild


async def test_get_order_status_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    class _FakeOrder:
        id = "alpaca-uuid"
        status = "filled"
        filled_qty = "1"
        filled_avg_price = "1.25"
        filled_at = datetime(2026, 4, 27, tzinfo=UTC)
        submitted_at = datetime(2026, 4, 27, tzinfo=UTC)
        canceled_at = None
        failed_at = None

    fake = MagicMock()
    fake.get_order_by_id.return_value = _FakeOrder()
    _install_fake_client(monkeypatch, fake)

    snap = await broker.get_order_status("alpaca-uuid")
    from decimal import Decimal as Dec
    assert snap.status == "filled"
    assert snap.filled_qty == Dec("1")
    assert snap.filled_avg_price == Dec("1.25")


# ------------- submit_short_call (Phase 5a) -------------


async def test_submit_short_call_refuses_when_kill_switch_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": True, "trading_enabled": True}),
    )
    result = await broker.submit_short_call(
        option_symbol="AMZN260506C00260000",
        qty=1,
        limit_price=Decimal("1.10"),
    )
    assert result.submitted is False
    assert result.reason == "kill_switch_engaged"
    fake.submit_order.assert_not_called()


async def test_submit_short_call_refuses_when_trading_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": False,
            "new_entries_enabled": True,
        }),
    )
    result = await broker.submit_short_call(
        option_symbol="AMZN260506C00260000",
        qty=1,
        limit_price=Decimal("1.10"),
    )
    assert result.submitted is False
    assert result.reason == "trading_disabled"
    fake.submit_order.assert_not_called()


async def test_submit_short_call_refuses_when_new_entries_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": False,
        }),
    )
    result = await broker.submit_short_call(
        option_symbol="AMZN260506C00260000",
        qty=1,
        limit_price=Decimal("1.10"),
    )
    assert result.submitted is False
    assert result.reason == "new_entries_disabled"
    fake.submit_order.assert_not_called()


async def test_submit_short_call_submits_when_flags_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    class _FakeOrder:
        id = "alpaca-cc-uuid"
        status = "accepted"

    fake = MagicMock()
    fake.submit_order.return_value = _FakeOrder()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": True,
        }),
    )
    result = await broker.submit_short_call(
        option_symbol="AMZN260506C00260000",
        qty=1,
        limit_price=Decimal("1.10"),
        client_order_id="kai-cc-12345678",
    )
    assert result.submitted is True
    assert result.alpaca_order_id == "alpaca-cc-uuid"
    request = fake.submit_order.call_args.args[0]
    assert request.symbol == "AMZN260506C00260000"
    assert request.client_order_id == "kai-cc-12345678"


async def test_submit_short_call_handles_alpaca_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order.side_effect = RuntimeError("boom")
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False, "trading_enabled": True,
            "new_entries_enabled": True,
        }),
    )
    result = await broker.submit_short_call(
        option_symbol="AMZN260506C00260000",
        qty=1,
        limit_price=Decimal("1.10"),
    )
    assert result.submitted is False
    assert result.reason == "submit_exception"
    assert result.error == "boom"


# ------------- list_long_equity_positions -------------


async def test_list_long_equity_positions_filters_options_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = [
        _FakePosition(symbol="AMZN", qty="100"),
        _FakePosition(symbol="AMZN260506P00250000", qty="-1"),  # short put position
        _FakePosition(symbol="AVGO", qty="100"),
        _FakePosition(symbol="MSFT", qty="0", side=_FakeSide.LONG),
    ]
    _install_fake_client(monkeypatch, fake)

    out = await broker.list_long_equity_positions()
    symbols = [p.symbol for p in out]
    assert "AMZN" in symbols
    assert "AVGO" in symbols
    assert "AMZN260506P00250000" not in symbols
    assert "MSFT" not in symbols  # qty=0 excluded


class _FakeShortSide(Enum):
    SHORT = "short"


async def test_list_short_option_positions_filters_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = [
        # Short put: should be included.
        _FakePosition(
            symbol="AMZN260506P00250000",
            qty="-1",
            side=_FakeShortSide.SHORT,
        ),
        # Long stock: excluded.
        _FakePosition(symbol="AMZN", qty="100"),
        # Long option: excluded.
        _FakePosition(
            symbol="AVGO260506C00400000",
            qty="1",
        ),
    ]
    _install_fake_client(monkeypatch, fake)
    out = await broker.list_short_option_positions()
    symbols = [p.symbol for p in out]
    assert symbols == ["AMZN260506P00250000"]


# ------------- submit_buy_to_close (Phase 5b) -------------


async def test_submit_buy_to_close_refused_when_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order = MagicMock()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": True, "trading_enabled": True}),
    )
    result = await broker.submit_buy_to_close(
        option_symbol="AMZN260506P00250000",
        qty=1,
        limit_price=Decimal("0.50"),
    )
    assert result.submitted is False
    assert result.reason == "kill_switch_engaged"
    fake.submit_order.assert_not_called()


async def test_submit_buy_to_close_allowed_when_trading_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closes are allowed even when new entries are off - they reduce exposure."""
    from decimal import Decimal
    from unittest.mock import AsyncMock

    class _FakeOrder:
        id = "alpaca-btc-uuid"
        status = "accepted"

    fake = MagicMock()
    fake.submit_order.return_value = _FakeOrder()
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={
            "kill_switch": False,
            "trading_enabled": False,  # off
            "new_entries_enabled": False,  # off
        }),
    )
    result = await broker.submit_buy_to_close(
        option_symbol="AMZN260506P00250000",
        qty=1,
        limit_price=Decimal("0.50"),
    )
    assert result.submitted is True
    assert result.alpaca_order_id == "alpaca-btc-uuid"
    request = fake.submit_order.call_args.args[0]
    from alpaca.trading.enums import OrderSide as _OrderSide
    assert request.side == _OrderSide.BUY
    assert request.symbol == "AMZN260506P00250000"


async def test_submit_buy_to_close_handles_alpaca_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    fake = MagicMock()
    fake.submit_order.side_effect = RuntimeError("boom")
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(
        broker,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": False, "trading_enabled": True}),
    )
    result = await broker.submit_buy_to_close(
        option_symbol="AMZN260506P00250000",
        qty=1,
        limit_price=Decimal("0.50"),
    )
    assert result.submitted is False
    assert result.reason == "submit_exception"
    assert result.error == "boom"
