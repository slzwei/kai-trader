"""Fill model: translate strategy intents into actual fills.

The strategy builds intents with ``bid``, ``ask``, and ``mid`` populated
from the historical chain. The fill model decides at what price the
limit order would have actually filled.

Three modes are supported:

* ``"mid"`` -- fill at mid. Optimistic; assumes we always get the
  midpoint. Good for sensitivity reference; never the headline default.
* ``"mid_minus_quarter_spread"`` -- fill at ``mid - 0.25 * (ask - bid)``.
  A more realistic "good fill" assumption: we got a quarter of the way
  inside the spread.
* ``"mid_minus_half_spread"`` -- fill at the bid (sells) or the ask
  (buys). Pessimistic; assumes we got the worst fill. **Default.**

For sells (sell-to-open shorts, sell-to-close longs), pessimistic means
filling at the bid: we accept the lower of the spread.

For buys (buy-to-open longs, buy-to-close shorts), pessimistic means
filling at the ask: we pay the higher of the spread.

There is no two-tick fill rule at daily resolution: each tick is the
end of a trading day, so a "limit unfilled this tick" decision is a
"limit unfilled today" decision. The harness does not currently model
multi-day limit-order persistence; every intent either fills today
(under the chosen fill model) or is dropped. This is documented as a
known limitation; in practice the production strategy resubmits stale
limits on the next tick anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

FillModelName = Literal["mid", "mid_minus_quarter_spread", "mid_minus_half_spread"]


@dataclass(frozen=True)
class Quote:
    """Bid/ask/mid snapshot for a single contract."""

    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def half_spread(self) -> Decimal:
        return (self.ask - self.bid) / Decimal("2")


@dataclass(frozen=True)
class FillModel:
    """Decides the price at which a sell or buy limit order fills."""

    name: FillModelName

    def fill_price_for_sell(self, quote: Quote) -> Decimal:
        """Return the fill price for a sell-to-open or sell-to-close.

        Pessimistic mode (``mid_minus_half_spread``) returns the bid:
        we accept the worst price a marketable sell limit could have
        gotten. Quantize to cents for stability across runs.
        """
        if self.name == "mid":
            return quote.mid.quantize(Decimal("0.01"))
        if self.name == "mid_minus_quarter_spread":
            half = quote.half_spread
            price = quote.mid - half / Decimal("2")
            return max(price, quote.bid).quantize(Decimal("0.01"))
        # mid_minus_half_spread -> bid
        return quote.bid.quantize(Decimal("0.01"))

    def fill_price_for_buy(self, quote: Quote) -> Decimal:
        """Return the fill price for a buy-to-open or buy-to-close."""
        if self.name == "mid":
            return quote.mid.quantize(Decimal("0.01"))
        if self.name == "mid_minus_quarter_spread":
            half = quote.half_spread
            price = quote.mid + half / Decimal("2")
            return min(price, quote.ask).quantize(Decimal("0.01"))
        # mid_minus_half_spread -> ask (pessimistic for buys)
        return quote.ask.quantize(Decimal("0.01"))


DEFAULT_FILL_MODEL: FillModel = FillModel(name="mid_minus_half_spread")
