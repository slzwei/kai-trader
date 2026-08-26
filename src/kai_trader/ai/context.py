"""Candidate packet builder: the structured context one decision sees.

``build_candidate_packet`` is pure given its inputs. Missing values are
``None`` in the packet (JSON ``null``), never zero, and the
``data_quality`` section names what is missing so the model cannot
mistake absence for a benign reading. Portfolio figures are context
only; the deterministic risk gate downstream remains authoritative for
sizing, exposure, and buying power.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.strategy.candidates import TradeIntent
from kai_trader.strategy.regime import RegimeSnapshot

if TYPE_CHECKING:
    # Type-only: the AI package must never runtime-import the
    # order-capable broker module; a hygiene test enforces this.
    from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot


@dataclass(frozen=True)
class DecisionContext:
    """Tick-level context shared by every candidate in one batch."""

    regime: RegimeSnapshot
    account: AccountSnapshot
    existing_short_puts: Sequence[PositionSnapshot]
    earnings_sources: str
    today: date


def _score_decimal(intent: TradeIntent, key: str) -> Decimal | None:
    raw = intent.scores.get(key)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _score_str(intent: TradeIntent, key: str) -> str | None:
    raw = intent.scores.get(key)
    return raw if isinstance(raw, str) and raw else None


def _ticker_exposure(
    symbol: str, positions: Sequence[PositionSnapshot]
) -> tuple[int, Decimal, int]:
    """(contracts in this ticker, collateral in this ticker, open CSP count)."""
    contracts = 0
    collateral = Decimal("0")
    open_csps = 0
    for p in positions:
        try:
            underlying, _exp, opt_type, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if opt_type != "put":
            continue
        qty = int(abs(p.qty))
        if qty <= 0:
            continue
        open_csps += 1
        if underlying.upper() == symbol.upper():
            contracts += qty
            collateral += strike * Decimal("100") * qty
    return contracts, collateral, open_csps


def build_candidate_packet(
    intent: TradeIntent,
    ctx: DecisionContext,
    *,
    spot_price: Decimal | None,
    events: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the JSON-safe packet for one screened proposal.

    ``intent`` is the screener's one-contract-basis proposal, so the
    economics block is per contract. ``events`` is the serialised
    :class:`kai_trader.ai.providers.EventContext`.
    """
    dte = (intent.expiration - ctx.today).days
    spread = intent.ask - intent.bid
    # Per-share credit; breakeven is the effective ownership price on
    # assignment.
    breakeven = intent.strike - intent.mid
    downside_cushion_pct: float | None = None
    if spot_price is not None and spot_price > 0:
        downside_cushion_pct = round(
            float((spot_price - breakeven) / spot_price) * 100, 2
        )

    iv = _score_decimal(intent, "iv")
    rv30 = _score_decimal(intent, "rv30")
    iv_rv_ratio: float | None = None
    if iv is not None and rv30 is not None and rv30 > 0:
        iv_rv_ratio = round(float(iv / rv30), 3)

    contracts_held, collateral_held, open_csps = _ticker_exposure(
        intent.symbol, ctx.existing_short_puts
    )

    missing: list[str] = []
    if spot_price is None:
        missing.append("underlying_spot_price")
    if iv is None:
        missing.append("implied_volatility")
    if rv30 is None:
        missing.append("realized_vol_30d")
    if _score_decimal(intent, "iv_percentile_rank") is None:
        missing.append("iv_percentile_rank")
    if _score_decimal(intent, "gamma") is None:
        missing.append("gamma")
    if _score_decimal(intent, "theta") is None:
        missing.append("theta")
    if _score_decimal(intent, "vega") is None:
        missing.append("vega")
    # The current Alpaca snapshot wrapper carries no volume or open
    # interest; represented as null, listed as missing, never zeroed.
    missing.extend(["option_volume", "option_open_interest"])
    if events.get("news_status") != "ok":
        missing.append("recent_news")

    return {
        "as_of_utc": datetime.now(UTC).isoformat(),
        "underlying": {
            "ticker": intent.symbol,
            "spot_price": spot_price,
            "trend_vs_50dma": _score_str(intent, "trend"),
            "market_regime": ctx.regime.regime,
            "vix": ctx.regime.vix,
            "spy_vs_50dma": round(
                ctx.regime.spy_price - ctx.regime.spy_50dma, 2
            ),
        },
        "option": {
            "contract": intent.option_symbol,
            "type": "put",
            "side": "sell_to_open",
            "strike": intent.strike,
            "expiration": intent.expiration.isoformat(),
            "dte": dte,
        },
        "market": {
            "bid": intent.bid,
            "ask": intent.ask,
            "mid": intent.mid,
            "spread": spread,
            "spread_pct_of_mid": _score_decimal(intent, "spread_pct"),
            "volume": None,
            "open_interest": None,
        },
        "greeks": {
            "delta": intent.actual_delta,
            "gamma": _score_decimal(intent, "gamma"),
            "theta": _score_decimal(intent, "theta"),
            "vega": _score_decimal(intent, "vega"),
            "iv": iv,
        },
        "economics_per_contract": {
            "premium": intent.mid * Decimal("100"),
            "secured_collateral": intent.strike * Decimal("100"),
            "yield_pct": intent.yield_pct,
            "annualised_yield": _score_decimal(intent, "annualised_yield"),
            "breakeven": breakeven,
            "downside_cushion_pct": downside_cushion_pct,
        },
        "volatility": {
            "iv": iv,
            "rv30": rv30,
            "iv_rv_ratio": iv_rv_ratio,
            "iv_percentile_rank": _score_decimal(intent, "iv_percentile_rank"),
        },
        "quant_screen": {
            "composite_score": _score_decimal(intent, "composite"),
            "target_delta": intent.target_delta,
            "reason": intent.reason,
            "earnings_status": _score_str(intent, "earnings"),
            "trend_status": _score_str(intent, "trend"),
            "scores": dict(intent.scores),
        },
        "portfolio_context": {
            "note": (
                "context only; the deterministic risk gate owns sizing, "
                "exposure caps, and buying power"
            ),
            "equity": ctx.account.equity,
            "cash": ctx.account.cash,
            "options_buying_power": ctx.account.options_buying_power,
            "open_csp_positions": open_csps,
            "contracts_in_this_ticker": contracts_held,
            "collateral_in_this_ticker": collateral_held,
        },
        "events": events,
        "data_quality": {
            "missing": missing,
            "earnings_sources": ctx.earnings_sources,
        },
    }
