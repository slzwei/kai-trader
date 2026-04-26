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

from kai_trader.config import Settings, get_settings
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
    client = _get_client()
    account = await asyncio.to_thread(client.get_account)
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
    client = _get_client()
    positions = await asyncio.to_thread(client.get_all_positions)
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


async def ping() -> bool:
    """Return True if the Alpaca API responds to a lightweight call.

    Uses ``get_clock`` because it is the cheapest endpoint that proves auth
    and network. ``get_account`` would also work but pulls a heavier payload.
    """
    try:
        client = _get_client()
        await asyncio.to_thread(client.get_clock)
        return True
    except Exception as exc:
        _log.warning("alpaca.ping.failed", error=str(exc))
        return False
