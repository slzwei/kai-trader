"""Read-only Alpaca broker access.

Phase 2 deliberately exposes only fetch operations: account snapshot, open
positions, and a liveness ping. No order placement, no cancellations, no
state mutation. The wheel strategy and order routing arrive in later phases
and will be gated behind the trading_enabled system flag.

The official ``alpaca-py`` SDK is sync. We push each call into a worker
thread via ``asyncio.to_thread`` so the bot's event loop stays responsive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from kai_trader.config import Settings, get_settings
from kai_trader.db.system_flags import get_all_flags
from kai_trader.logging import get_logger

_client: TradingClient | None = None
_log = get_logger(__name__)


@dataclass(frozen=True)
class AccountSnapshot:
    """Narrow view of a TradeAccount, decoupled from alpaca-py types."""

    equity: Decimal
    last_equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    day_pl: Decimal
    status: str
    paper: bool


@dataclass(frozen=True)
class PositionSnapshot:
    """Narrow view of a Position, decoupled from alpaca-py types."""

    symbol: str
    qty: Decimal
    side: str
    avg_entry_price: Decimal
    current_price: Decimal | None
    market_value: Decimal | None
    unrealized_pl: Decimal | None
    unrealized_intraday_pl: Decimal | None


def _build_client(cfg: Settings) -> TradingClient:
    return TradingClient(
        api_key=cfg.alpaca_api_key.get_secret_value(),
        secret_key=cfg.alpaca_secret_key.get_secret_value(),
        paper=cfg.alpaca_paper,
    )


def _get_client(settings: Settings | None = None) -> TradingClient:
    """Return the lazily-built singleton TradingClient."""
    global _client
    if _client is None:
        _client = _build_client(settings or get_settings())
    return _client


def reset_client() -> None:
    """Drop the cached client. Tests use this to swap in a stub."""
    global _client
    _client = None


_STALE_CONNECTION_HINTS = (
    "RemoteDisconnected",
    "Connection aborted",
    "Connection reset",
    "BadStatusLine",
)


def _is_stale_connection(error_str: str) -> bool:
    return any(hint in error_str for hint in _STALE_CONNECTION_HINTS)


# Map known Alpaca APIError codes (https://docs.alpaca.markets/docs/error-codes)
# to short, queryable reason strings. Anything not listed here falls back to
# the generic ``submit_exception`` so the unknown error still gets surfaced
# with full text via ``SubmitResult.error``.
_ALPACA_ERROR_CODE_REASONS: dict[int, str] = {
    40310000: "insufficient_options_buying_power",
    40310001: "insufficient_buying_power",
    40010001: "invalid_order_request",
    42210000: "wash_trade_blocked",
}


def _classify_submit_error(exc: BaseException) -> str:
    """Return a typed reason string for a known APIError code, else generic.

    Lets the orders table show a readable reason like
    ``insufficient_options_buying_power`` instead of the catch-all
    ``submit_exception`` for the common Alpaca rejections we have
    observed. Unknown codes still fall through to ``submit_exception``
    and the full ``str(exc)`` is preserved on ``SubmitResult.error``.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        mapped = _ALPACA_ERROR_CODE_REASONS.get(code)
        if mapped is not None:
            return mapped
    return "submit_exception"


async def _call_alpaca_with_retry(method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Run a sync TradingClient method via to_thread, retry once on stale conn.

    Idle keep-alive connections to Alpaca occasionally get closed by the
    server. The next call sees ``RemoteDisconnected`` from urllib3 inside
    a ConnectionError. When we detect that, we drop the cached client,
    rebuild, and retry once. Any other exception bubbles up unchanged.
    """
    client = _get_client()
    try:
        return await asyncio.to_thread(getattr(client, method_name), *args, **kwargs)
    except Exception as exc:
        if not _is_stale_connection(str(exc)):
            raise
        _log.warning("alpaca.connection.stale_retry", method=method_name, error=str(exc))
        reset_client()
        client = _get_client()
        return await asyncio.to_thread(getattr(client, method_name), *args, **kwargs)


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        # Fields are Optional[str] in alpaca-py; treat missing numerics as zero
        # so callers do not get a None where a Decimal is contractually returned.
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


async def get_account() -> AccountSnapshot:
    """Fetch the current account state from Alpaca."""
    account = await _call_alpaca_with_retry("get_account")
    if isinstance(account, dict):  # raw_data path; not used here but defensive.
        raise RuntimeError("Alpaca client returned raw dict, expected TradeAccount.")
    equity = _to_decimal(account.equity)
    last_equity = _to_decimal(account.last_equity)
    return AccountSnapshot(
        equity=equity,
        last_equity=last_equity,
        cash=_to_decimal(account.cash),
        buying_power=_to_decimal(account.buying_power),
        portfolio_value=_to_decimal(account.portfolio_value),
        day_pl=equity - last_equity,
        status=_enum_value(account.status),
        paper=get_settings().alpaca_paper,
    )


async def list_positions() -> list[PositionSnapshot]:
    """Return all open positions on the account; possibly empty."""
    positions = await _call_alpaca_with_retry("get_all_positions")
    if isinstance(positions, dict):  # raw_data path; defensive.
        raise RuntimeError("Alpaca client returned raw dict, expected list[Position].")
    snapshots: list[PositionSnapshot] = []
    for p in positions:
        snapshots.append(
            PositionSnapshot(
                symbol=p.symbol,
                qty=_to_decimal(p.qty),
                side=_enum_value(p.side),
                avg_entry_price=_to_decimal(p.avg_entry_price),
                current_price=_to_decimal_or_none(p.current_price),
                market_value=_to_decimal_or_none(p.market_value),
                unrealized_pl=_to_decimal_or_none(p.unrealized_pl),
                unrealized_intraday_pl=_to_decimal_or_none(p.unrealized_intraday_pl),
            )
        )
    return snapshots


async def list_short_option_positions() -> list[PositionSnapshot]:
    """Return short option positions only (negative qty + OCC symbol).

    Used by Phase 5 profit-take logic to find open short puts whose
    current price has decayed enough to close at a captured-premium
    threshold. The qty in the returned snapshot is preserved as Alpaca
    returns it (negative for short positions).
    """
    from kai_trader.broker.options_data import parse_occ_symbol  # local import to avoid cycle

    out: list[PositionSnapshot] = []
    for p in await list_positions():
        try:
            parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if p.side.lower() == "short" or p.qty < 0:
            out.append(p)
    return out


async def list_long_equity_positions() -> list[PositionSnapshot]:
    """Return long stock positions only, excluding options.

    Used by Phase 5 covered-call logic to find shares that came from
    put assignments. Filtering is by OCC-pattern detection on the
    symbol because alpaca-py exposes asset_class but the narrowed
    PositionSnapshot does not retain it.
    """
    from kai_trader.broker.options_data import parse_occ_symbol  # local import to avoid cycle

    out: list[PositionSnapshot] = []
    for p in await list_positions():
        try:
            parse_occ_symbol(p.symbol)
        except ValueError:
            # Not an OCC symbol. Treat as equity if long with positive qty.
            if p.side.lower() == "long" and p.qty > 0:
                out.append(p)
    return out


async def ping() -> bool:
    """Return True if the Alpaca API responds to a lightweight call.

    Uses ``get_clock`` because it is the cheapest endpoint that proves auth
    and network. ``get_account`` would also work but pulls a heavier payload.
    """
    try:
        await _call_alpaca_with_retry("get_clock")
        return True
    except Exception as exc:
        _log.warning("alpaca.ping.failed", error=str(exc))
        return False


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of an order submission attempt.

    When ``submitted`` is ``False`` the trade was deliberately not sent to
    Alpaca; ``reason`` explains why and ``flags`` carries the system_flags
    snapshot so the caller can audit the decision.
    """

    submitted: bool
    alpaca_order_id: str | None
    order_status: str | None
    reason: str | None
    flags: dict[str, bool]
    error: str | None = None


async def submit_short_put(
    *,
    option_symbol: str,
    qty: int,
    limit_price: Decimal,
    client_order_id: str | None = None,
) -> SubmitResult:
    """Submit a sell-to-open short put, gated by system flags.

    Reads ``trading_enabled`` and ``kill_switch`` from ``system_flags`` BEFORE
    touching Alpaca. If the kill switch is engaged or trading is not enabled,
    nothing is sent and the caller gets a typed refusal back.
    """
    flags = await get_all_flags()
    if flags.get("kill_switch", False):
        _log.warning(
            "alpaca.submit.refused_kill_switch",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="kill_switch_engaged",
            flags=flags,
        )
    if not flags.get("trading_enabled", False):
        _log.warning(
            "alpaca.submit.refused_trading_disabled",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="trading_disabled",
            flags=flags,
        )
    if not flags.get("new_entries_enabled", False):
        _log.warning(
            "alpaca.submit.refused_new_entries_disabled",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="new_entries_disabled",
            flags=flags,
        )

    request = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=float(limit_price),
        client_order_id=client_order_id,
    )
    try:
        order = await _call_alpaca_with_retry("submit_order", request)
    except Exception as exc:
        reason = _classify_submit_error(exc)
        _log.error(
            "alpaca.submit.failed",
            option_symbol=option_symbol,
            reason=reason,
            error=str(exc),
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason=reason,
            flags=flags,
            error=str(exc),
        )

    if isinstance(order, dict):
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="raw_dict_payload",
            flags=flags,
        )

    _log.info(
        "alpaca.submit.ok",
        option_symbol=option_symbol,
        alpaca_order_id=str(order.id),
        order_status=str(order.status),
    )
    return SubmitResult(
        submitted=True,
        alpaca_order_id=str(order.id),
        order_status=_enum_value(order.status),
        reason=None,
        flags=flags,
    )


async def submit_short_call(
    *,
    option_symbol: str,
    qty: int,
    limit_price: Decimal,
    client_order_id: str | None = None,
) -> SubmitResult:
    """Submit a sell-to-open short call (covered call leg), gated by flags.

    Same gating layer as ``submit_short_put``: kill_switch first,
    trading_enabled second, new_entries_enabled third. Even though calls
    against assigned shares technically reduce risk relative to holding
    bare stock, they create new short-option exposure and are therefore
    treated as new entries for flag-gating purposes.
    """
    flags = await get_all_flags()
    if flags.get("kill_switch", False):
        _log.warning(
            "alpaca.submit_call.refused_kill_switch",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="kill_switch_engaged",
            flags=flags,
        )
    if not flags.get("trading_enabled", False):
        _log.warning(
            "alpaca.submit_call.refused_trading_disabled",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="trading_disabled",
            flags=flags,
        )
    if not flags.get("new_entries_enabled", False):
        _log.warning(
            "alpaca.submit_call.refused_new_entries_disabled",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="new_entries_disabled",
            flags=flags,
        )

    request = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=float(limit_price),
        client_order_id=client_order_id,
    )
    try:
        order = await _call_alpaca_with_retry("submit_order", request)
    except Exception as exc:
        reason = _classify_submit_error(exc)
        _log.error(
            "alpaca.submit_call.failed",
            option_symbol=option_symbol,
            reason=reason,
            error=str(exc),
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason=reason,
            flags=flags,
            error=str(exc),
        )

    if isinstance(order, dict):
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="raw_dict_payload",
            flags=flags,
        )

    _log.info(
        "alpaca.submit_call.ok",
        option_symbol=option_symbol,
        alpaca_order_id=str(order.id),
        order_status=str(order.status),
    )
    return SubmitResult(
        submitted=True,
        alpaca_order_id=str(order.id),
        order_status=_enum_value(order.status),
        reason=None,
        flags=flags,
    )


@dataclass(frozen=True)
class OrderStatusSnapshot:
    """Narrow status pull for one Alpaca order id."""

    alpaca_order_id: str
    status: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    filled_at: Any  # datetime when present
    submitted_at: Any
    cancelled_at: Any
    failed_at: Any


def _is_position_not_found(error_str: str) -> bool:
    """Detect Alpaca's 40410000 'position not found' code in an error string."""
    return "40410000" in error_str or "position not found" in error_str.lower()


async def close_position(symbol: str) -> SubmitResult:
    """Submit a market close on a held position. Gated by kill_switch only.

    Closes are allowed when ``kill_switch`` is engaged and even when
    ``trading_enabled`` is off, because closing reduces exposure rather
    than adding to it. The kill switch check stays as a manual brake on
    everything including discretionary closes; clear it first if the
    operator really wants to close while the breaker is tripped.
    """
    flags = await get_all_flags()
    if flags.get("kill_switch", False):
        _log.warning("alpaca.close.refused_kill_switch", symbol=symbol)
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="kill_switch_engaged",
            flags=flags,
        )
    try:
        order = await _call_alpaca_with_retry("close_position", symbol)
    except Exception as exc:
        if _is_position_not_found(str(exc)):
            _log.info("alpaca.close.no_position", symbol=symbol)
            return SubmitResult(
                submitted=False,
                alpaca_order_id=None,
                order_status=None,
                reason="position_not_found",
                flags=flags,
            )
        _log.error("alpaca.close.failed", symbol=symbol, error=str(exc))
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="close_exception",
            flags=flags,
            error=str(exc),
        )
    if isinstance(order, dict):
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="raw_dict_payload",
            flags=flags,
        )
    _log.info(
        "alpaca.close.ok",
        symbol=symbol,
        alpaca_order_id=str(order.id),
        order_status=str(order.status),
    )
    return SubmitResult(
        submitted=True,
        alpaca_order_id=str(order.id),
        order_status=_enum_value(order.status),
        reason=None,
        flags=flags,
    )


async def submit_buy_to_close(
    *,
    option_symbol: str,
    qty: int,
    limit_price: Decimal,
    client_order_id: str | None = None,
) -> SubmitResult:
    """Submit a buy-to-close limit order on a short option position.

    Used for profit-take execution: closing a short put when its current
    ask has decayed to (1 - profit_take_pct) of the original credit.

    Gated by ``kill_switch`` only, mirroring ``close_position`` and
    ``/close``: closing reduces exposure rather than adding to it, so
    ``trading_enabled`` and ``new_entries_enabled`` do not block. The
    kill switch remains a manual brake on every outbound order.
    """
    flags = await get_all_flags()
    if flags.get("kill_switch", False):
        _log.warning(
            "alpaca.btc.refused_kill_switch",
            option_symbol=option_symbol,
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="kill_switch_engaged",
            flags=flags,
        )

    request = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=float(limit_price),
        client_order_id=client_order_id,
    )
    try:
        order = await _call_alpaca_with_retry("submit_order", request)
    except Exception as exc:
        reason = _classify_submit_error(exc)
        _log.error(
            "alpaca.btc.failed",
            option_symbol=option_symbol,
            reason=reason,
            error=str(exc),
        )
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason=reason,
            flags=flags,
            error=str(exc),
        )

    if isinstance(order, dict):
        return SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="raw_dict_payload",
            flags=flags,
        )

    _log.info(
        "alpaca.btc.ok",
        option_symbol=option_symbol,
        alpaca_order_id=str(order.id),
        order_status=str(order.status),
    )
    return SubmitResult(
        submitted=True,
        alpaca_order_id=str(order.id),
        order_status=_enum_value(order.status),
        reason=None,
        flags=flags,
    )


async def get_order_status(alpaca_order_id: str) -> OrderStatusSnapshot:
    """Fetch the latest status for an order we previously submitted."""
    order = await _call_alpaca_with_retry("get_order_by_id", alpaca_order_id)
    if isinstance(order, dict):
        raise RuntimeError("Alpaca client returned raw dict, expected Order.")
    return OrderStatusSnapshot(
        alpaca_order_id=str(order.id),
        status=_enum_value(order.status),
        filled_qty=_to_decimal(order.filled_qty),
        filled_avg_price=_to_decimal_or_none(order.filled_avg_price),
        filled_at=order.filled_at,
        submitted_at=order.submitted_at,
        cancelled_at=order.canceled_at,
        failed_at=order.failed_at,
    )
