"""Transaction-cost model for option trades in the backtest.

Per-contract fees on options trades, charged at fill time:

* **OCC clearing fee**: $0.05/contract on every open and close. The OCC
  publishes this; current rates are flat across exchanges.
* **ORF (Options Regulatory Fee)**: $0.02925/contract, sells only.
* **SEC fee**: $0.0000278 of notional, sells only. Notional is
  ``strike * 100 * qty`` for puts (the assignment-amount basis), or
  ``mid_price * 100 * qty`` for calls (premium basis); we use mid-price
  notional for both because the SEC fee is computed on the dollar
  amount of the sale, which for an option premium sale is the premium
  received.

These add up to roughly $0.08-$0.10 per contract round-trip. On a $0.20
weekly put that is 4-5% of premium, which is meaningful for a yield
strategy. Excluding them inflates returns 3-5%/year on an actively
trading book.

Defaults are hard-coded constants. The ``TransactionCostModel`` class
exposes them as overridable arguments so a sensitivity run can zero
them out for comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ActionSide = Literal["sell_to_open", "buy_to_close", "buy_to_open", "sell_to_close"]


@dataclass(frozen=True)
class TransactionCostModel:
    """Per-contract option fees applied at fill time.

    Defaults reflect 2025 OCC + ORF + SEC schedules. The SEC fee
    rate is the value published by the SEC and updated annually; this
    constant is the rate applied through 2025-2026.
    """

    occ_clearing_per_contract: Decimal = Decimal("0.05")
    orf_per_contract_sell: Decimal = Decimal("0.02925")
    sec_fee_rate: Decimal = Decimal("0.0000278")

    def total_for(
        self,
        side: ActionSide,
        qty: int,
        fill_price: Decimal,
    ) -> Decimal:
        """Return total fees for a fill. ``fill_price`` is per-share option price.

        Quantity is in contracts. Notional for the SEC fee is
        ``fill_price * 100 * qty``: each contract represents 100 shares.
        """
        qty_dec = Decimal(qty)
        cost = self.occ_clearing_per_contract * qty_dec
        if side in ("sell_to_open", "sell_to_close"):
            cost += self.orf_per_contract_sell * qty_dec
            notional = fill_price * Decimal("100") * qty_dec
            cost += self.sec_fee_rate * notional
        return cost.quantize(Decimal("0.01"))


DEFAULT_COST_MODEL: TransactionCostModel = TransactionCostModel()
