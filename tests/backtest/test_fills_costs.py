"""Fill model and transaction cost tests.

Pessimistic fill defaults are critical for honest backtest results;
the cost model is the difference between "looks profitable" and
"actually profitable" for low-premium options.
"""

from __future__ import annotations

from decimal import Decimal

from kai_trader.backtest.costs import DEFAULT_COST_MODEL, TransactionCostModel
from kai_trader.backtest.fills import DEFAULT_FILL_MODEL, FillModel, Quote


class TestFillModel:
    def test_mid_minus_half_spread_sell_returns_bid(self) -> None:
        model = FillModel(name="mid_minus_half_spread")
        q = Quote(bid=Decimal("1.00"), ask=Decimal("1.40"))
        # Pessimistic sell = the bid.
        assert model.fill_price_for_sell(q) == Decimal("1.00")

    def test_mid_minus_half_spread_buy_returns_ask(self) -> None:
        model = FillModel(name="mid_minus_half_spread")
        q = Quote(bid=Decimal("1.00"), ask=Decimal("1.40"))
        # Pessimistic buy = the ask.
        assert model.fill_price_for_buy(q) == Decimal("1.40")

    def test_mid_returns_midpoint(self) -> None:
        model = FillModel(name="mid")
        q = Quote(bid=Decimal("1.00"), ask=Decimal("1.40"))
        assert model.fill_price_for_sell(q) == Decimal("1.20")
        assert model.fill_price_for_buy(q) == Decimal("1.20")

    def test_quarter_spread_between_mid_and_extreme(self) -> None:
        model = FillModel(name="mid_minus_quarter_spread")
        q = Quote(bid=Decimal("1.00"), ask=Decimal("1.40"))
        sell_price = model.fill_price_for_sell(q)
        # Sell: mid (1.20) - 0.25 * spread (0.40) = 1.10
        assert sell_price == Decimal("1.10")
        buy_price = model.fill_price_for_buy(q)
        # Buy: mid (1.20) + 0.25 * spread (0.40) = 1.30
        assert buy_price == Decimal("1.30")

    def test_default_is_pessimistic(self) -> None:
        assert DEFAULT_FILL_MODEL.name == "mid_minus_half_spread"

    def test_zero_spread_collapses_all_models(self) -> None:
        q = Quote(bid=Decimal("2.00"), ask=Decimal("2.00"))
        for name in ("mid", "mid_minus_quarter_spread", "mid_minus_half_spread"):
            m = FillModel(name=name)
            assert m.fill_price_for_sell(q) == Decimal("2.00")
            assert m.fill_price_for_buy(q) == Decimal("2.00")


class TestCostModel:
    def test_sell_includes_orf_and_sec(self) -> None:
        cost = DEFAULT_COST_MODEL.total_for("sell_to_open", qty=1, fill_price=Decimal("2.00"))
        # OCC 0.05 + ORF 0.02925 + SEC 0.0000278 * 200 = 0.05 + 0.02925 + 0.00556 = 0.08481
        # quantize to 0.01 -> 0.08
        assert cost == Decimal("0.08")

    def test_buy_no_orf_or_sec(self) -> None:
        cost = DEFAULT_COST_MODEL.total_for("buy_to_close", qty=1, fill_price=Decimal("2.00"))
        # OCC 0.05 only
        assert cost == Decimal("0.05")

    def test_scales_with_qty_approximately(self) -> None:
        # Per-cent quantization makes the relationship not exact across orders
        # of magnitude, but the ratio should be near 10x (within 1 cent).
        cost1 = DEFAULT_COST_MODEL.total_for("sell_to_open", qty=10, fill_price=Decimal("2.00"))
        cost2 = DEFAULT_COST_MODEL.total_for("sell_to_open", qty=1, fill_price=Decimal("2.00"))
        diff = abs(cost1 - cost2 * Decimal("10"))
        assert diff <= Decimal("0.05")

    def test_round_trip_cost(self) -> None:
        # 1 contract round-trip on a $2 option.
        sell = DEFAULT_COST_MODEL.total_for("sell_to_open", qty=1, fill_price=Decimal("2.00"))
        buy = DEFAULT_COST_MODEL.total_for("buy_to_close", qty=1, fill_price=Decimal("1.00"))
        # ~$0.13 round trip per contract.
        assert sell + buy == Decimal("0.13")

    def test_disabled_model_zero(self) -> None:
        zero = TransactionCostModel(
            occ_clearing_per_contract=Decimal("0"),
            orf_per_contract_sell=Decimal("0"),
            sec_fee_rate=Decimal("0"),
        )
        assert zero.total_for("sell_to_open", qty=10, fill_price=Decimal("5")) == Decimal("0.00")
