"""Read-only options data via Alpaca's OptionHistoricalDataClient.

Phase 3.1 ships chain fetch only. The wheel strategy in 3.2+ will use this
to walk strikes and pick the one closest to a target delta.

The official ``alpaca-py`` client is sync, so each call is pushed through
``asyncio.to_thread`` to keep the bot's event loop responsive. Returned
contracts are narrow dataclasses so handlers and strategy code do not
depend on alpaca-py types directly.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from alpaca.data.enums import OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

from kai_trader.config import Settings, get_settings
from kai_trader.logging import get_logger

_client: OptionHistoricalDataClient | None = None
_log = get_logger(__name__)

# OCC option symbol regex: <ROOT><YY><MM><DD><C|P><strike * 1000, 8 digits>.
# Root is variable length but always alphabetic; the rest is fixed-width.
_OCC_PATTERN = re.compile(
    r"^(?P<root>[A-Z]+)"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d{8})$"
)


@dataclass(frozen=True)
class OptionContract:
    """Narrow view of a single option contract snapshot."""

    symbol: str
    underlying: str
    option_type: str  # "call" or "put"
    strike: Decimal
    expiration: date
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    implied_volatility: Decimal | None


def parse_occ_symbol(symbol: str) -> tuple[str, date, str, Decimal]:
    """Decode an OCC option symbol into (underlying, expiration, type, strike).

    Example: ``AAPL250619C00150000`` -> ``("AAPL", date(2025, 6, 19), "call",
    Decimal("150.00"))``. Raises ``ValueError`` on malformed input.
    """
    match = _OCC_PATTERN.match(symbol)
    if match is None:
        raise ValueError(f"Not a valid OCC option symbol: {symbol!r}")
    root = match.group("root")
    expiration = date(2000 + int(match.group("yy")), int(match.group("mm")), int(match.group("dd")))
    option_type = "call" if match.group("cp") == "C" else "put"
    # Strike is in thousandths of a dollar, 8-digit zero-padded.
    strike = Decimal(match.group("strike")) / Decimal("1000")
    return root, expiration, option_type, strike


def _build_client(cfg: Settings) -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(
        api_key=cfg.alpaca_api_key.get_secret_value(),
        secret_key=cfg.alpaca_secret_key.get_secret_value(),
    )


def _get_client(settings: Settings | None = None) -> OptionHistoricalDataClient:
    """Return the lazily-built singleton options data client."""
    global _client
    if _client is None:
        _client = _build_client(settings or get_settings())
    return _client


def reset_client() -> None:
    """Drop the cached client. Tests use this to swap in a stub."""
    global _client
    _client = None


def _to_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _snapshot_to_contract(symbol: str, snap: Any) -> OptionContract:
    underlying, expiration, option_type, strike = parse_occ_symbol(symbol)

    quote = snap.latest_quote
    trade = snap.latest_trade
    greeks = snap.greeks
    iv = snap.implied_volatility

    return OptionContract(
        symbol=symbol,
        underlying=underlying,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        bid=_to_decimal_or_none(quote.bid_price) if quote is not None else None,
        ask=_to_decimal_or_none(quote.ask_price) if quote is not None else None,
        last=_to_decimal_or_none(trade.price) if trade is not None else None,
        delta=_to_decimal_or_none(greeks.delta) if greeks is not None else None,
        gamma=_to_decimal_or_none(greeks.gamma) if greeks is not None else None,
        theta=_to_decimal_or_none(greeks.theta) if greeks is not None else None,
        vega=_to_decimal_or_none(greeks.vega) if greeks is not None else None,
        implied_volatility=_to_decimal_or_none(iv),
    )


async def get_chain(
    underlying: str,
    expiration: date | None = None,
) -> list[OptionContract]:
    """Fetch the option chain for ``underlying``, optionally one expiration.

    Returns contracts sorted by (expiration, strike, type). Empty list when
    Alpaca returns no chain (e.g. symbol with no listed options or the data
    feed has nothing yet for the day).
    """
    upper = underlying.upper()
    client = _get_client()
    request_kwargs: dict[str, Any] = {
        "underlying_symbol": upper,
        "feed": OptionsFeed.OPRA,
    }
    if expiration is not None:
        request_kwargs["expiration_date"] = expiration
    request = OptionChainRequest(**request_kwargs)
    result = await asyncio.to_thread(client.get_option_chain, request)
    if not isinstance(result, dict):
        raise RuntimeError(
            "Alpaca client returned non-dict chain payload; raw_data mode unsupported."
        )

    contracts: list[OptionContract] = []
    for symbol, snap in result.items():
        try:
            contracts.append(_snapshot_to_contract(symbol, snap))
        except ValueError as exc:
            _log.warning("options_data.parse_failed", symbol=symbol, error=str(exc))
    contracts.sort(key=lambda c: (c.expiration, c.strike, c.option_type))
    return contracts
