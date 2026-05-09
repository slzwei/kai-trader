"""Covered call candidate builder.

The wheel's second leg: once a CSP assigns and 100 shares per contract
land, we sell calls against them. This module is the call-side mirror of
``candidates.py``, with two structural differences:

1. No capital deployment math. CCs do not consume cash collateral; the
   shares ARE the collateral. The per-symbol cap and total deployment
   cap from ``candidates.py`` are not applied here.
2. Quantity is derived from existing share holdings, not headroom:
   ``qty = floor(shares / 100)`` per underlying.

Selection is target-delta-closest within the sleeve DTE band, mirroring
``select_put_strike``. Sleeves whose ``symbol_whitelist`` does not
include the held underlying are silently skipped (the assignment came
from a sleeve that owned the put; if the underlying is no longer in any
whitelist, we hold the shares without a CC).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger
from kai_trader.strategy.earnings import EARNINGS_BLACKOUT_DAYS, EarningsStatus
from kai_trader.strategy.regime import RegimeSnapshot

ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]
EarningsStatusProvider = Callable[[str, date, int], Awaitable[EarningsStatus]]

_log = get_logger(__name__)


@dataclass(frozen=True)
class CallIntent:
    """A would-be covered-call trade for one underlying with held shares."""

    sleeve: str
    symbol: str
    option_symbol: str
    strike: Decimal
    expiration: date
    target_delta: Decimal
    actual_delta: Decimal
    bid: Decimal
    ask: Decimal
    mid: Decimal
    qty: int
    expected_premium: Decimal


@dataclass(frozen=True)
class CallDiagnostic:
    """Per-sleeve counters describing why CC intents were or were not built."""

    sleeve: str
    symbols_evaluated: int
    chains_fetched: int
    chain_errors: int
    calls_seen: int
    calls_with_delta: int
    calls_in_dte_band: int
    calls_with_quotes: int
    intents_built: int


@dataclass(frozen=True)
class CallBuildDiagnostics:
    """Aggregate of per-sleeve CC diagnostics."""

    sleeves: list[CallDiagnostic]

    def warning_lines(self) -> list[str]:
        active = [s for s in self.sleeves if s.symbols_evaluated > 0]
        if not active:
            return []
        total_intents = sum(s.intents_built for s in active)
        if total_intents > 0:
            return []
        total_calls = sum(s.calls_seen for s in active)
        total_with_delta = sum(s.calls_with_delta for s in active)
        total_in_band = sum(s.calls_in_dte_band for s in active)
        total_with_quotes = sum(s.calls_with_quotes for s in active)
        if total_calls == 0:
            return [
                "covered-call eval: held shares present but option chains "
                "returned no calls"
            ]
        if total_with_delta == 0:
            return [
                f"covered-call eval: {total_calls} calls seen, none had delta"
            ]
        if total_in_band == 0:
            return [
                f"covered-call eval: {total_with_delta} calls had delta, "
                f"none in sleeve DTE band"
            ]
        if total_with_quotes == 0:
            return [
                f"covered-call eval: {total_in_band} calls in band, none had bid+ask"
            ]
        return []


def _within_dte_band(expiration: date, today: date, sleeve: SleeveConfig) -> bool:
    dte = (expiration - today).days
    return sleeve.target_dte_min <= dte <= sleeve.target_dte_max


def select_call_strike(
    chain: list[OptionContract],
    target_delta: Decimal,
    sleeve: SleeveConfig,
    today: date,
) -> OptionContract | None:
    """Return the call closest to ``target_delta`` within the sleeve DTE band.

    Pure function. ``target_delta`` is positive for calls. Filters to call
    contracts that report a delta and whose expiration falls within the
    sleeve's preferred DTE window.
    """
    typed_candidates: list[tuple[OptionContract, Decimal]] = []
    for c in chain:
        if c.option_type != "call":
            continue
        if c.delta is None:
            continue
        if not _within_dte_band(c.expiration, today, sleeve):
            continue
        typed_candidates.append((c, c.delta))
    if not typed_candidates:
        return None
    chosen, _delta = min(
        typed_candidates,
        key=lambda pair: abs(pair[1] - target_delta),
    )
    return chosen


def _sleeve_for_symbol(
    sleeves: list[SleeveConfig], symbol: str
) -> SleeveConfig | None:
    """Find the sleeve that owns this underlying via whitelist match.

    Returns the first sleeve whose whitelist contains the symbol. If the
    symbol is whitelisted by multiple sleeves (shouldn't happen by
    convention), the first match wins.
    """
    upper = symbol.upper()
    for s in sleeves:
        if not s.enabled:
            continue
        if upper in (w.upper() for w in s.symbol_whitelist):
            return s
    return None


def _intent_from(
    sleeve: SleeveConfig,
    contract: OptionContract,
    target_delta: Decimal,
    qty: int,
) -> CallIntent | None:
    if contract.bid is None or contract.ask is None or contract.delta is None:
        return None
    if qty < 1:
        return None
    bid = contract.bid
    ask = contract.ask
    mid = (bid + ask) / Decimal("2")
    expected_premium = mid * Decimal("100") * Decimal(qty)
    return CallIntent(
        sleeve=sleeve.sleeve,
        symbol=contract.underlying,
        option_symbol=contract.symbol,
        strike=contract.strike,
        expiration=contract.expiration,
        target_delta=target_delta,
        actual_delta=contract.delta,
        bid=bid,
        ask=ask,
        mid=mid,
        qty=qty,
        expected_premium=expected_premium,
    )


async def build_call_intents(
    long_equity_positions: list[PositionSnapshot],
    sleeves: list[SleeveConfig],
    regime: RegimeSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_status: EarningsStatusProvider | None = None,
) -> tuple[list[CallIntent], CallBuildDiagnostics]:
    """Walk held equity positions, find a CC for each whose sleeve owns it.

    Returns a list of intents (one per sleeve+underlying that produced
    a viable strike) plus a per-sleeve diagnostic.

    Earnings blackout (added 2026-05-10): when ``earnings_status`` is
    supplied AND the sleeve has ``earnings_blackout_enabled``, skip
    underlyings whose next earnings date falls inside the sleeve's
    DTE window. Selling a CC that spans an earnings report exposes
    the assigned shares to a post-earnings gap-up that calls them
    away at the strike, capping upside on a binary event. Fail-
    closed: an ``unknown`` earnings status causes the symbol to be
    skipped (matches the W-1 production posture for puts).
    """
    today = today or datetime.now(UTC).date()

    # Group held positions by which sleeve owns them.
    by_sleeve: dict[str, list[tuple[SleeveConfig, PositionSnapshot]]] = {}
    for p in long_equity_positions:
        s = _sleeve_for_symbol(sleeves, p.symbol)
        if s is None:
            _log.info(
                "strategy.cc.no_sleeve_owner",
                symbol=p.symbol,
                qty=str(p.qty),
            )
            continue
        by_sleeve.setdefault(s.sleeve, []).append((s, p))

    intents: list[CallIntent] = []
    sleeve_diags: list[CallDiagnostic] = []

    for sleeve in sleeves:
        positions_for_sleeve = by_sleeve.get(sleeve.sleeve, [])
        if not positions_for_sleeve:
            sleeve_diags.append(
                CallDiagnostic(
                    sleeve=sleeve.sleeve,
                    symbols_evaluated=0,
                    chains_fetched=0,
                    chain_errors=0,
                    calls_seen=0,
                    calls_with_delta=0,
                    calls_in_dte_band=0,
                    calls_with_quotes=0,
                    intents_built=0,
                )
            )
            continue

        # Phase 9 (2026-05-09): risk_off no longer blocks CC entries.
        # Selling calls on assigned stock generates premium regardless
        # of regime; the bigger risk in risk_off is rolling losses on
        # the put leg, not new CC entries.
        if False and regime.regime == "risk_off":
            _log.info(
                "strategy.cc.skipped_regime",
                sleeve=sleeve.sleeve,
                regime=regime.regime,
            )
            sleeve_diags.append(
                CallDiagnostic(
                    sleeve=sleeve.sleeve,
                    symbols_evaluated=len(positions_for_sleeve),
                    chains_fetched=0,
                    chain_errors=0,
                    calls_seen=0,
                    calls_with_delta=0,
                    calls_in_dte_band=0,
                    calls_with_quotes=0,
                    intents_built=0,
                )
            )
            continue

        target_delta = sleeve.target_delta_call

        chains_fetched = 0
        chain_errors = 0
        calls_seen = 0
        calls_with_delta = 0
        calls_in_dte_band = 0
        calls_with_quotes = 0
        intents_built_for_sleeve = 0

        for _sleeve_match, position in positions_for_sleeve:
            # Earnings filter (2026-05-10): skip the symbol if its next
            # earnings date falls inside the sleeve's DTE window. A CC
            # that spans earnings caps upside if the stock gaps up on
            # the report and the call assigns; a CC that's still open
            # post-earnings often drops below the entry credit on a
            # gap-down, leaving the holder with both an unrealized
            # loss on the stock AND a worthless short call.
            if (
                earnings_status is not None
                and sleeve.earnings_blackout_enabled
            ):
                try:
                    status = await earnings_status(
                        position.symbol, today, EARNINGS_BLACKOUT_DAYS
                    )
                except Exception as exc:
                    _log.warning(
                        "strategy.cc.earnings_status_failed",
                        sleeve=sleeve.sleeve,
                        symbol=position.symbol,
                        error=str(exc),
                    )
                    status = "unknown"
                if status != "outside_window":
                    _log.info(
                        "strategy.cc.earnings_blackout",
                        sleeve=sleeve.sleeve,
                        symbol=position.symbol,
                        status=status,
                    )
                    continue
            try:
                chain = await chain_fetcher(position.symbol, None)
            except Exception as exc:
                chain_errors += 1
                _log.warning(
                    "strategy.cc.chain_fetch_failed",
                    sleeve=sleeve.sleeve,
                    symbol=position.symbol,
                    error=str(exc),
                )
                continue
            chains_fetched += 1

            for c in chain:
                if c.option_type != "call":
                    continue
                calls_seen += 1
                if c.delta is None:
                    continue
                calls_with_delta += 1
                if not _within_dte_band(c.expiration, today, sleeve):
                    continue
                calls_in_dte_band += 1
                if c.bid is None or c.ask is None:
                    continue
                calls_with_quotes += 1

            contract = select_call_strike(chain, target_delta, sleeve, today)
            if contract is None:
                continue

            qty = int(position.qty // Decimal("100"))
            if qty < 1:
                continue

            intent = _intent_from(sleeve, contract, target_delta, qty)
            if intent is None:
                continue
            intents.append(intent)
            intents_built_for_sleeve += 1

        sleeve_diags.append(
            CallDiagnostic(
                sleeve=sleeve.sleeve,
                symbols_evaluated=len(positions_for_sleeve),
                chains_fetched=chains_fetched,
                chain_errors=chain_errors,
                calls_seen=calls_seen,
                calls_with_delta=calls_with_delta,
                calls_in_dte_band=calls_in_dte_band,
                calls_with_quotes=calls_with_quotes,
                intents_built=intents_built_for_sleeve,
            )
        )

    return intents, CallBuildDiagnostics(sleeves=sleeve_diags)


def summarise_call_intents(intents: list[CallIntent]) -> str:
    """Render a compact one-liner per CC intent for notifications."""
    if not intents:
        return "No covered-call candidates this tick."
    lines = []
    total_premium = Decimal("0")
    for i in intents:
        total_premium += i.expected_premium
        lines.append(
            f"{i.sleeve}/{i.symbol} {i.expiration} {i.qty}xC {i.strike} "
            f"d={i.actual_delta:.2f} prem={i.expected_premium:.2f}"
        )
    if total_premium > 0:
        lines.append("")
        lines.append(
            f"Total: {len(intents)} CC intents, premium {total_premium:.2f}"
        )
    return "\n".join(lines)
