"""Read-only market data access via Alpaca's StockHistoricalDataClient.

Phase 2.8 exposes only latest-quote and latest-trade lookups. Bars, options
chains, and historical streams arrive when the strategy code needs them.
The underlying SDK is sync, so each call is pushed through
``asyncio.to_thread`` to keep the bot's event loop responsive.

Free Alpaca paper accounts get the IEX feed by default; symbols not active
on IEX may return stale or empty quotes during off-hours. That's a data
issue, not a wrapper bug.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

from kai_trader.config import Settings, get_settings
from kai_trader.logging import get_logger

_client: StockHistoricalDataClient | None = None
_log = get_logger(__name__)


@dataclass(frozen=True)
class QuoteSnapshot:
    """Narrow view of a Quote, decoupled from alpaca-py types."""

    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    timestamp: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def mid(self) -> Decimal:
        return (self.ask_price + self.bid_price) / Decimal("2")


@dataclass(frozen=True)
class TradeSnapshot:
    """Narrow view of a Trade, decoupled from alpaca-py types."""

    symbol: str
    price: Decimal
    size: Decimal
    timestamp: datetime


def _build_client(cfg: Settings) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=cfg.alpaca_api_key.get_secret_value(),
        secret_key=cfg.alpaca_secret_key.get_secret_value(),
    )


def _get_client(settings: Settings | None = None) -> StockHistoricalDataClient:
    """Return the lazily-built singleton market data client."""
    global _client
    if _client is None:
        _client = _build_client(settings or get_settings())
    return _client


def reset_client() -> None:
    """Drop the cached client. Tests use this to swap in a stub."""
    global _client
    _client = None


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


async def get_latest_quote(symbol: str) -> QuoteSnapshot:
    """Fetch the latest bid/ask quote for ``symbol``."""
    upper = symbol.upper()
    client = _get_client()
    request = StockLatestQuoteRequest(symbol_or_symbols=upper)
    result = await asyncio.to_thread(client.get_stock_latest_quote, request)
    if isinstance(result, dict) and upper not in result:
        raise LookupError(f"No quote returned for {upper!r}.")
    quote = result[upper]
    return QuoteSnapshot(
        symbol=upper,
        bid_price=_to_decimal(quote.bid_price),
        ask_price=_to_decimal(quote.ask_price),
        bid_size=_to_decimal(quote.bid_size),
        ask_size=_to_decimal(quote.ask_size),
        timestamp=quote.timestamp,
    )


async def get_latest_trade(symbol: str) -> TradeSnapshot:
    """Fetch the most recent trade print for ``symbol``."""
    upper = symbol.upper()
    client = _get_client()
    request = StockLatestTradeRequest(symbol_or_symbols=upper)
    result = await asyncio.to_thread(client.get_stock_latest_trade, request)
    if isinstance(result, dict) and upper not in result:
        raise LookupError(f"No trade returned for {upper!r}.")
    trade = result[upper]
    return TradeSnapshot(
        symbol=upper,
        price=_to_decimal(trade.price),
        size=_to_decimal(trade.size),
        timestamp=trade.timestamp,
    )
