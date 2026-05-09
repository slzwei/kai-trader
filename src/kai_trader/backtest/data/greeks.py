"""Black-Scholes pricing and Greeks reconstruction for the backtest.

Alpaca's historical option API returns OHLCV bars and quotes but does
not store historical Greeks. The strategy's delta-targeting logic needs
delta on every contract in every chain, so the backtest reconstructs
Greeks via Black-Scholes from:

* the option mid price (from the historical bar / quote)
* the underlying close (from cached daily bars)
* time to expiration in years
* the 3-month T-bill rate from FRED at the asof date

Implied volatility is solved via Newton-Raphson on the price equation,
seeded at 30%. The solver bails out cleanly on degenerate inputs
(expired contract, zero price, negative time) and the caller falls
back to skipping the contract rather than producing a bogus delta.

Pure math. No external dependencies beyond ``math``. Operates entirely
in ``float`` for speed; the public dataclass interface converts back
to ``Decimal`` at the boundary so the rest of the system keeps using
Decimal money.

Validation against live Alpaca snapshots is in
``tests/backtest/test_greeks_validation.py``; running it requires
``ALPACA_INTEGRATION_TEST=1``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

OptionType = Literal["call", "put"]

# Newton-Raphson tuning. Defaults converge on every contract we have
# tested in <10 iterations to better than 1e-7 price tolerance.
_IV_TOLERANCE: Final[float] = 1e-7
_IV_MAX_ITER: Final[int] = 80
_IV_INITIAL_GUESS: Final[float] = 0.30
_IV_LOWER_BOUND: Final[float] = 1e-5
_IV_UPPER_BOUND: Final[float] = 5.0


@dataclass(frozen=True)
class GreeksResult:
    """Reconstruction output. ``iv`` is annualised; Greeks are per BS scaling.

    Delta is signed: calls positive, puts negative. Theta is per calendar
    day (the per-year BS theta divided by 365). Vega is per 1.00 IV move
    (so a vega of 0.10 means a $0.10 price change for a 1.00 absolute
    increase in sigma; multiply by 0.01 for the more familiar "per 1%
    IV" reading).
    """

    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via stdlib erf. No scipy dep needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(
    spot: float,
    strike: float,
    rate: float,
    sigma: float,
    t_years: float,
) -> tuple[float, float]:
    """Black-Scholes d1, d2 helper. Caller validates positive inputs."""
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def bs_price(
    option_type: OptionType,
    spot: float,
    strike: float,
    rate: float,
    sigma: float,
    t_years: float,
) -> float:
    """Black-Scholes European price. Inputs in their natural units.

    Returns 0 (call) or ``max(strike - spot, 0)`` (put intrinsic) when
    ``t_years <= 0`` so the IV solver does not divide by zero on expired
    contracts. Negative or zero spot/strike/sigma raise; the caller
    should not feed such values.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError(f"spot and strike must be positive, got spot={spot}, strike={strike}")
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got sigma={sigma}")
    if t_years <= 0:
        if option_type == "call":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)
    d1, d2 = _d1_d2(spot, strike, rate, sigma, t_years)
    discount = math.exp(-rate * t_years)
    if option_type == "call":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_vega(spot: float, strike: float, rate: float, sigma: float, t_years: float) -> float:
    """Black-Scholes vega. Output is per 1.00 absolute change in sigma.

    Returns 0 on expired contracts. Used as the derivative in
    Newton-Raphson IV solving.
    """
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, rate, sigma, t_years)
    return spot * _norm_pdf(d1) * math.sqrt(t_years)


def solve_iv(
    option_type: OptionType,
    market_price: float,
    spot: float,
    strike: float,
    rate: float,
    t_years: float,
) -> float | None:
    """Newton-Raphson IV solver. Returns ``None`` on non-convergence.

    Bails on degenerate inputs (expired, zero or negative price, intrinsic
    violations). The caller should treat ``None`` as "skip this contract"
    rather than substituting a default IV.
    """
    if t_years <= 0:
        return None
    if market_price <= 0:
        return None
    if spot <= 0 or strike <= 0:
        return None

    # Reject prices below intrinsic: BS cannot match them with positive sigma.
    if option_type == "call":
        intrinsic = max(spot - strike * math.exp(-rate * t_years), 0.0)
    else:
        intrinsic = max(strike * math.exp(-rate * t_years) - spot, 0.0)
    if market_price < intrinsic - 1e-6:
        return None

    sigma = _IV_INITIAL_GUESS
    for _ in range(_IV_MAX_ITER):
        try:
            price = bs_price(option_type, spot, strike, rate, sigma, t_years)
        except ValueError:
            return None
        diff = price - market_price
        if abs(diff) < _IV_TOLERANCE:
            return sigma
        v = bs_vega(spot, strike, rate, sigma, t_years)
        if v < 1e-12:
            return None
        step = diff / v
        new_sigma = sigma - step
        if not math.isfinite(new_sigma):
            return None
        # Clamp to a sane range so a bad iteration does not run away.
        if new_sigma < _IV_LOWER_BOUND:
            new_sigma = _IV_LOWER_BOUND
        if new_sigma > _IV_UPPER_BOUND:
            new_sigma = _IV_UPPER_BOUND
        sigma = new_sigma
    return None


def bs_greeks(
    option_type: OptionType,
    spot: float,
    strike: float,
    rate: float,
    sigma: float,
    t_years: float,
) -> tuple[float, float, float, float]:
    """Return (delta, gamma, theta_per_day, vega) at the supplied inputs.

    Delta is signed. Theta is per calendar day. Vega is per 1.00 absolute
    sigma change. Expired contracts return zeros for all four.
    """
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0, 0.0, 0.0, 0.0
    d1, d2 = _d1_d2(spot, strike, rate, sigma, t_years)
    pdf_d1 = _norm_pdf(d1)
    sqrt_t = math.sqrt(t_years)
    discount = math.exp(-rate * t_years)
    if option_type == "call":
        delta = _norm_cdf(d1)
        theta_per_year = -spot * pdf_d1 * sigma / (2.0 * sqrt_t) - rate * strike * discount * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_per_year = -spot * pdf_d1 * sigma / (2.0 * sqrt_t) + rate * strike * discount * _norm_cdf(-d2)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t
    theta_per_day = theta_per_year / 365.0
    return delta, gamma, theta_per_day, vega


def reconstruct_greeks(
    option_type: OptionType,
    market_price: float,
    spot: float,
    strike: float,
    rate: float,
    t_years: float,
) -> GreeksResult | None:
    """One-shot helper: solve IV from market price, compute all Greeks.

    Returns ``None`` when IV solving fails (degenerate inputs, intrinsic
    violation, non-convergence). Callers treat ``None`` as "this contract
    does not produce reconstructable Greeks at this asof; skip it."
    """
    iv = solve_iv(option_type, market_price, spot, strike, rate, t_years)
    if iv is None:
        return None
    delta, gamma, theta_per_day, vega = bs_greeks(
        option_type, spot, strike, rate, iv, t_years
    )
    return GreeksResult(
        iv=iv,
        delta=delta,
        gamma=gamma,
        theta=theta_per_day,
        vega=vega,
    )


def reconstruct_greeks_decimal(
    option_type: OptionType,
    market_price: Decimal,
    spot: Decimal,
    strike: Decimal,
    rate: float,
    t_years: float,
) -> GreeksResult | None:
    """Decimal-friendly wrapper for the chain-builder boundary."""
    return reconstruct_greeks(
        option_type=option_type,
        market_price=float(market_price),
        spot=float(spot),
        strike=float(strike),
        rate=rate,
        t_years=t_years,
    )
