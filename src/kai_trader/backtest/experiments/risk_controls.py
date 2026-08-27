"""Research-only entry risk controls for the backtest harness.

Motivated by the drawdown forensics of the 2024-03 to 2026-08 baseline
(39.6% max DD): assigned shares are invisible to the per-name notional
cap (``risk/gate.py`` counts short-put collateral only), so the wheel
accumulated MARA up to 81.5% of NAV by re-entering puts on names it was
already assigned into, and the harness additionally omits production's
50-DMA trend filter (``trend_status=None`` fail-opens the gate), so it
kept averaging down below the 50-DMA where production would have
refused.

Two families of controls, applied to the intents the UNCHANGED
production screen+gate already produced. They can only shrink or drop
intents, mirroring how the production ``ai_filter`` hook is allowed to
act, so the deterministic pipeline is never widened:

* **Economic per-name cap**: admit a new CSP only while
  ``shares_MV + open_put_face + accepted_face`` for that underlying
  stays within ``pct * NAV``; oversized intents are shrunk contract by
  contract, mirroring the gate's own headroom sizing. An optional
  cluster list applies the same test to a group of correlated names
  (MARA+RIOT trade as one bet in the data: the forensics show the
  cluster peaking at 85.4% of NAV).
* **Assigned-equity NAV brake**: while total assigned-share market
  value exceeds ``pct * NAV``, no new CSP entries at all.

The 50-DMA trend filter is not a new control: enabling it wires the
asof-bounded cache into the EXISTING ``trend_status`` hook of the
production candidates builder, restoring harness parity with the live
bot.

Nothing in this module is imported by production code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal

from kai_trader.backtest.data import bars
from kai_trader.backtest.state import BacktestState
from kai_trader.broker.market_data import DailyBar as ProdDailyBar
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.logging import get_logger
from kai_trader.strategy.candidates import TradeIntent
from kai_trader.strategy.trend import (
    SMA_PERIOD_DEFAULT,
    TrendStatus,
    compute_trend_status,
)

_log = get_logger(__name__)

MINER_CLUSTER: tuple[str, ...] = ("MARA", "RIOT")


@dataclass(frozen=True)
class RiskControls:
    """One named bundle of entry-side risk controls for a run."""

    name: str
    # Per-underlying economic cap as a fraction of NAV, counting
    # assigned shares at market PLUS open put face PLUS this tick's
    # already-accepted intents. None disables.
    per_name_econ_cap_pct: Decimal | None = None
    # Correlated groups that share one cap. Only used when
    # cluster_cap_pct is set.
    clusters: tuple[tuple[str, ...], ...] = ()
    cluster_cap_pct: Decimal | None = None
    # Restore production's 50-DMA trend filter via the candidates
    # builder's existing trend_status hook.
    trend_filter: bool = False
    # Portfolio brake: no new CSPs while assigned-share MV exceeds
    # this fraction of NAV. None disables.
    assigned_nav_cap_pct: Decimal | None = None


@dataclass
class ControlDecision:
    """Per-tick audit of what the controls did (for the run report)."""

    rejected: int = 0
    shrunk: int = 0
    accepted: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)

    def note_reject(self, reason: str) -> None:
        self.rejected += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1


# Registry the CLI exposes. Cap levels chosen from the observed failure,
# not a parameter sweep: 12% matches the existing per-name put cap
# (assigned shares simply stop being invisible to it), 20% is a looser
# variant to price the return cost of strictness, 18% cluster keeps
# MARA+RIOT jointly below ~1/5 of the book, and 50% assigned-NAV is the
# blunt portfolio brake for comparison.
CONTROLS: dict[str, RiskControls] = {
    "trend": RiskControls(name="trend", trend_filter=True),
    "econ12": RiskControls(
        name="econ12", per_name_econ_cap_pct=Decimal("0.12")
    ),
    "econ20": RiskControls(
        name="econ20", per_name_econ_cap_pct=Decimal("0.20")
    ),
    "assigned50": RiskControls(
        name="assigned50", assigned_nav_cap_pct=Decimal("0.50")
    ),
    "econ20_cluster25": RiskControls(
        name="econ20_cluster25",
        per_name_econ_cap_pct=Decimal("0.20"),
        clusters=(MINER_CLUSTER,),
        cluster_cap_pct=Decimal("0.25"),
    ),
    "trend_econ12": RiskControls(
        name="trend_econ12",
        trend_filter=True,
        per_name_econ_cap_pct=Decimal("0.12"),
    ),
    "trend_econ20": RiskControls(
        name="trend_econ20",
        trend_filter=True,
        per_name_econ_cap_pct=Decimal("0.20"),
    ),
    "trend_econ20_cluster25": RiskControls(
        name="trend_econ20_cluster25",
        trend_filter=True,
        per_name_econ_cap_pct=Decimal("0.20"),
        clusters=(MINER_CLUSTER,),
        cluster_cap_pct=Decimal("0.25"),
    ),
}


def make_trend_provider(
    asof: date,
) -> Callable[[str], Awaitable[TrendStatus]]:
    """Asof-bounded 50-DMA provider matching production semantics.

    Uses the production ``compute_trend_status`` pure function over the
    backtest bar cache so the classification rule cannot drift from the
    live bot: latest close below the 50-bar SMA is ``below``; fewer
    than 50 bars is the fail-closed ``unknown``.
    """

    async def _provider(symbol: str) -> TrendStatus:
        history = bars.get_history_until(
            symbol, asof, lookback_days=SMA_PERIOD_DEFAULT + 25
        )
        # Adapt the backtest bar shape to the production DailyBar so the
        # production pure function stays the single source of the rule.
        prod_bars = [
            ProdDailyBar(
                symbol=symbol,
                timestamp=datetime.combine(b.asof, time.min, tzinfo=UTC),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=Decimal(b.volume),
            )
            for b in history
        ]
        return compute_trend_status(prod_bars, SMA_PERIOD_DEFAULT)

    return _provider


def _shares_mv_by_symbol(state: BacktestState, asof: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in state.long_equity_positions:
        close = bars.get_close_on_or_before(p.symbol, asof)
        mark = close[1] if close is not None else p.avg_entry_price
        out[p.symbol] = out.get(p.symbol, Decimal("0")) + mark * p.qty
    return out


def _put_face_by_symbol(state: BacktestState) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in state.short_option_positions:
        try:
            u, _e, opt, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if opt != "put":
            continue
        out[u] = out.get(u, Decimal("0")) + strike * Decimal("100") * abs(p.qty)
    return out


def _control_nav(state: BacktestState, asof: date, shares_mv: dict[str, Decimal]) -> Decimal:
    """NAV basis for the caps: cash + shares at market - put intrinsic.

    A risk control should see market value, not cost basis; using cost
    basis would loosen exactly when the shares are underwater, which is
    when the control matters most.
    """
    intrinsic = Decimal("0")
    for p in state.short_option_positions:
        try:
            u, _e, opt, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if opt != "put":
            continue
        close = bars.get_close_on_or_before(u, asof)
        if close is None:
            continue
        intrinsic += max(strike - close[1], Decimal("0")) * Decimal("100") * abs(p.qty)
    return state.cash + sum(shares_mv.values(), Decimal("0")) - intrinsic


def apply_entry_controls(
    intents: list[TradeIntent],
    state: BacktestState,
    asof: date,
    controls: RiskControls,
) -> tuple[list[TradeIntent], ControlDecision]:
    """Shrink or drop gate-approved intents per the active controls.

    Preserves the gate's ranking order. Never adds, reorders, or
    enlarges an intent.
    """
    decision = ControlDecision()
    if not intents:
        return intents, decision

    shares_mv = _shares_mv_by_symbol(state, asof)
    put_face = _put_face_by_symbol(state)
    nav = _control_nav(state, asof, shares_mv)
    if nav <= 0:
        for _ in intents:
            decision.note_reject("nav_nonpositive")
        return [], decision

    assigned_mv = sum(shares_mv.values(), Decimal("0"))
    if (
        controls.assigned_nav_cap_pct is not None
        and assigned_mv > nav * controls.assigned_nav_cap_pct
    ):
        for _ in intents:
            decision.note_reject("assigned_nav_brake")
        _log.info(
            "backtest.controls.assigned_nav_brake",
            asof=asof.isoformat(),
            assigned_mv=str(assigned_mv),
            nav=str(nav),
        )
        return [], decision

    cluster_of: dict[str, tuple[str, ...]] = {}
    for cluster in controls.clusters:
        for sym in cluster:
            cluster_of[sym] = cluster

    accepted_face: dict[str, Decimal] = {}
    out: list[TradeIntent] = []
    for intent in intents:
        sym = intent.symbol
        strike_face = intent.strike * Decimal("100")

        def _econ(symbols: tuple[str, ...]) -> Decimal:
            return sum(
                (
                    shares_mv.get(s, Decimal("0"))
                    + put_face.get(s, Decimal("0"))
                    + accepted_face.get(s, Decimal("0"))
                    for s in symbols
                ),
                Decimal("0"),
            )

        # Headroom under each active cap, in contracts.
        max_qty = intent.qty
        if controls.per_name_econ_cap_pct is not None:
            cap = nav * controls.per_name_econ_cap_pct
            room = cap - _econ((sym,))
            max_qty = min(max_qty, int(room // strike_face) if room > 0 else 0)
        if controls.cluster_cap_pct is not None and sym in cluster_of:
            cap = nav * controls.cluster_cap_pct
            room = cap - _econ(cluster_of[sym])
            max_qty = min(max_qty, int(room // strike_face) if room > 0 else 0)

        if max_qty <= 0:
            decision.note_reject("econ_cap")
            _log.info(
                "backtest.controls.rejected",
                asof=asof.isoformat(),
                symbol=sym,
                option_symbol=intent.option_symbol,
                qty=intent.qty,
            )
            continue
        if max_qty < intent.qty:
            decision.shrunk += 1
            intent = replace(
                intent,
                qty=max_qty,
                collateral=intent.strike * Decimal("100") * max_qty,
                expected_premium=intent.mid * Decimal("100") * max_qty,
            )
        decision.accepted += 1
        accepted_face[sym] = accepted_face.get(sym, Decimal("0")) + strike_face * intent.qty
        out.append(intent)
    return out, decision
