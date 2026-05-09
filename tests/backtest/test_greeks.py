"""Black-Scholes pricing, IV solver, and Greeks reconstruction tests.

Validation against published reference values (Hull, Options, Futures and
Other Derivatives, standard ATM/ITM/OTM cases) plus self-consistency
checks (price-then-solve-IV-then-recompute round trips).

These are the foundation of the entire backtest. If the math is wrong,
every chain has wrong delta, and every strike-selection decision is
wrong. Treat any failure here as a P0.
"""

from __future__ import annotations

import math

import pytest

from kai_trader.backtest.data.greeks import (
    bs_greeks,
    bs_price,
    bs_vega,
    reconstruct_greeks,
    solve_iv,
)


class TestBSPrice:
    """Reference values from Hull, ch. 13. ATM call S=K=100, r=5%, sigma=20%, T=1."""

    def test_atm_call_one_year(self) -> None:
        price = bs_price("call", 100, 100, 0.05, 0.20, 1.0)
        assert abs(price - 10.4506) < 0.001

    def test_atm_put_one_year(self) -> None:
        price = bs_price("put", 100, 100, 0.05, 0.20, 1.0)
        assert abs(price - 5.5735) < 0.001

    def test_otm_put_short_dated(self) -> None:
        # SPY-like: spot 510, strike 500, 0.13 IV, 7 days, r=5%.
        price = bs_price("put", 510, 500, 0.05, 0.13, 7 / 365.0)
        # Expected ~$0.50 per Hull approximation; loosen for short-DTE drift.
        assert 0.30 < price < 0.80

    def test_expired_call_intrinsic_only(self) -> None:
        # S=110, K=100, T=0 -> price equals max(S-K, 0) = 10.
        price = bs_price("call", 110, 100, 0.05, 0.20, 0.0)
        assert price == 10.0

    def test_expired_put_intrinsic_only(self) -> None:
        # S=90, K=100, T=0 -> price = max(K-S, 0) = 10.
        price = bs_price("put", 90, 100, 0.05, 0.20, 0.0)
        assert price == 10.0

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError):
            bs_price("call", 0, 100, 0.05, 0.20, 1.0)
        with pytest.raises(ValueError):
            bs_price("put", 100, -1, 0.05, 0.20, 1.0)
        with pytest.raises(ValueError):
            bs_price("call", 100, 100, 0.05, 0, 1.0)


class TestBSGreeks:
    """Hull reference: delta, gamma, vega, theta for ATM call S=K=100."""

    def test_atm_call_delta(self) -> None:
        d, _g, _t, _v = bs_greeks("call", 100, 100, 0.05, 0.20, 1.0)
        assert abs(d - 0.6368) < 0.001

    def test_atm_put_delta(self) -> None:
        d, _g, _t, _v = bs_greeks("put", 100, 100, 0.05, 0.20, 1.0)
        assert abs(d - (-0.3632)) < 0.001

    def test_call_delta_is_positive_put_delta_is_negative(self) -> None:
        c_d, _, _, _ = bs_greeks("call", 100, 100, 0.05, 0.20, 1.0)
        p_d, _, _, _ = bs_greeks("put", 100, 100, 0.05, 0.20, 1.0)
        assert c_d > 0
        assert p_d < 0

    def test_otm_put_delta_around_target_range(self) -> None:
        # ~1% OTM put on a 510 stock at 13% IV, 7 days. This is the
        # neighbourhood the strategy actually targets for SPY.
        d, _g, _t, _v = bs_greeks("put", 510, 505, 0.05, 0.13, 7 / 365.0)
        # 1% OTM at 13% IV / 7 DTE should land in the -0.30 to -0.10 zone.
        assert -0.40 < d < -0.10

    def test_gamma_peaks_near_atm(self) -> None:
        _d, gamma_atm, _t, _v = bs_greeks("call", 100, 100, 0.05, 0.20, 1.0)
        _d, gamma_itm, _t, _v = bs_greeks("call", 100, 80, 0.05, 0.20, 1.0)
        _d, gamma_otm, _t, _v = bs_greeks("call", 100, 120, 0.05, 0.20, 1.0)
        assert gamma_atm > gamma_itm
        assert gamma_atm > gamma_otm

    def test_vega_positive(self) -> None:
        _d, _g, _t, vega = bs_greeks("call", 100, 100, 0.05, 0.20, 1.0)
        assert vega > 0


class TestIVSolver:
    """Round-trip: price an option, solve IV, verify recovery."""

    def test_recover_iv_from_atm_call(self) -> None:
        true_iv = 0.20
        price = bs_price("call", 100, 100, 0.05, true_iv, 1.0)
        recovered = solve_iv("call", price, 100, 100, 0.05, 1.0)
        assert recovered is not None
        assert abs(recovered - true_iv) < 1e-5

    def test_recover_iv_from_atm_put(self) -> None:
        true_iv = 0.30
        price = bs_price("put", 100, 100, 0.05, true_iv, 1.0)
        recovered = solve_iv("put", price, 100, 100, 0.05, 1.0)
        assert recovered is not None
        assert abs(recovered - true_iv) < 1e-5

    def test_recover_iv_high_volatility(self) -> None:
        true_iv = 0.80
        price = bs_price("call", 100, 100, 0.05, true_iv, 0.5)
        recovered = solve_iv("call", price, 100, 100, 0.05, 0.5)
        assert recovered is not None
        assert abs(recovered - true_iv) < 1e-4

    def test_recover_iv_short_dated_otm(self) -> None:
        true_iv = 0.13
        price = bs_price("put", 510, 500, 0.05, true_iv, 7 / 365.0)
        recovered = solve_iv("put", price, 510, 500, 0.05, 7 / 365.0)
        assert recovered is not None
        assert abs(recovered - true_iv) < 1e-4

    def test_zero_price_returns_none(self) -> None:
        assert solve_iv("call", 0.0, 100, 100, 0.05, 1.0) is None
        assert solve_iv("put", 0.0, 100, 100, 0.05, 1.0) is None

    def test_expired_returns_none(self) -> None:
        assert solve_iv("call", 5.0, 100, 100, 0.05, 0.0) is None

    def test_below_intrinsic_returns_none(self) -> None:
        # Put with strike 100, spot 80: intrinsic ~20. A price of 5 is impossible.
        assert solve_iv("put", 5.0, 80, 100, 0.05, 1.0) is None


class TestReconstructGreeks:
    """End-to-end reconstruction matches the BS reference for known IVs."""

    def test_round_trip_atm_call(self) -> None:
        true_iv = 0.20
        price = bs_price("call", 100, 100, 0.05, true_iv, 1.0)
        result = reconstruct_greeks("call", price, 100, 100, 0.05, 1.0)
        assert result is not None
        assert abs(result.iv - true_iv) < 1e-5
        assert abs(result.delta - 0.6368) < 0.001

    def test_round_trip_atm_put(self) -> None:
        true_iv = 0.30
        price = bs_price("put", 100, 100, 0.05, true_iv, 1.0)
        result = reconstruct_greeks("put", price, 100, 100, 0.05, 1.0)
        assert result is not None
        assert abs(result.iv - true_iv) < 1e-5
        # ATM put delta at sigma=0.30 sits around -0.40.
        assert -0.50 < result.delta < -0.30

    def test_degenerate_inputs_return_none(self) -> None:
        # Negative time
        assert reconstruct_greeks("call", 5, 100, 100, 0.05, -0.1) is None
        # Zero price
        assert reconstruct_greeks("put", 0, 100, 100, 0.05, 1.0) is None
        # Zero spot
        assert reconstruct_greeks("call", 5, 0, 100, 0.05, 1.0) is None


class TestVega:
    """Vega is the IV-solver derivative; it must be positive and finite."""

    def test_vega_positive_ranges(self) -> None:
        for sigma in (0.05, 0.20, 0.50, 1.0):
            for T in (1 / 365.0, 7 / 365.0, 30 / 365.0, 1.0):
                v = bs_vega(100, 100, 0.05, sigma, T)
                assert v >= 0
                assert math.isfinite(v)

    def test_vega_zero_at_expiry(self) -> None:
        assert bs_vega(100, 100, 0.05, 0.20, 0.0) == 0.0
