"""Survivorship-aware trading universe for the backtest.

The naive backtest walks today's sleeve whitelist for every historical
asof. That biases results: today's whitelist may include symbols that
were not yet listed (or were too thinly traded) at past asofs, and it
omits names that were dropped from the whitelist after a delisting or
failed strategy attempt. Both flavours of bias inflate returns.

This module corrects for it by intersecting today's whitelist with
"could we actually have traded this symbol on asof_dt". The proxy is
Alpaca daily bar presence: if SPY has a bar on 2024-04-12 in the cache,
SPY was tradable that day. If a stock was added to the whitelist in
2025 but the bar cache shows continuous SPY bars from 2016 forward, it
passes; but a symbol that only starts having bars in 2025-01 is excluded
from the universe for any asof before 2025-01.

The bar-presence proxy is more accurate than a formal listing-date feed
for our purposes: the question is "would the strategy have been able
to trade this", not "was the SEC's record of listing complete". A
symbol with no Alpaca bars on a given day was effectively un-tradable
for us regardless of its formal listing status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from kai_trader.backtest.data import bars
from kai_trader.logging import get_logger

_log = get_logger(__name__)

# A symbol is considered tradable on asof if it has at least one daily
# bar in the trailing window. Five trading days absorbs Friday + 3-day
# weekend + a typical holiday.
_LIVENESS_WINDOW_DAYS: Final[int] = 7


@dataclass(frozen=True)
class UniverseSnapshot:
    """The set of tradable symbols at a given asof, with reasons for exclusions.

    ``allowed`` is the survivorship-resolved trading universe.
    ``excluded`` maps symbol -> reason so the summary report can show
    which names were filtered and why.
    """

    asof: date
    allowed: tuple[str, ...]
    excluded: dict[str, str]


def resolve_universe(
    whitelist: list[str],
    asof: date,
    *,
    liveness_window_days: int = _LIVENESS_WINDOW_DAYS,
) -> UniverseSnapshot:
    """Filter ``whitelist`` to symbols tradable on ``asof``.

    A symbol is tradable when its Alpaca daily-bar cache has at least
    one bar in ``[asof - liveness_window_days, asof]``. The lookback
    absorbs weekends and holidays so a symbol does not get dropped
    because asof landed on a Saturday.
    """
    allowed: list[str] = []
    excluded: dict[str, str] = {}
    earliest = asof - timedelta(days=liveness_window_days)
    for raw_symbol in whitelist:
        symbol = raw_symbol.upper()
        history = bars.get_history_until(symbol, asof, lookback_days=liveness_window_days * 2)
        if not history:
            excluded[symbol] = f"no cached bars on or before {asof.isoformat()}"
            continue
        recent = [b for b in history if b.asof >= earliest]
        if not recent:
            excluded[symbol] = (
                f"no bars in last {liveness_window_days} days "
                f"(latest cached: {history[-1].asof.isoformat()})"
            )
            continue
        allowed.append(symbol)
    return UniverseSnapshot(
        asof=asof,
        allowed=tuple(sorted(set(allowed))),
        excluded=excluded,
    )


def union_whitelist(sleeves_whitelists: list[list[str]]) -> list[str]:
    """Return the de-duplicated union of every sleeve's whitelist.

    Used to drive cache warmup so the backtest pulls every name that any
    sleeve might trade, before the per-sleeve resolver runs at each tick.
    """
    seen: set[str] = set()
    out: list[str] = []
    for wl in sleeves_whitelists:
        for s in wl:
            upper = s.upper()
            if upper not in seen:
                seen.add(upper)
                out.append(upper)
    return out
