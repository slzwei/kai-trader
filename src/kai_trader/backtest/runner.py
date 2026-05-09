"""Top-level orchestrator: iterate ticks, replay the strategy, mark to market.

Per tick (one trading day):

  1. Settle expiries that landed on this date (puts + calls).
  2. Build regime snapshot from cached VIX + SPY.
  3. Mark portfolio to end-of-day, append equity point.
  4. Run drawdown check; trip kill_switch on breach.
  5. Run profit_take scan; close anything past threshold.
  6. Run roll evaluation; execute net-credit rolls only.
  7. Build CSP intents and submit through BacktestBroker.
  8. Build covered-call intents and submit.
  9. Aggregate per-tick diagnostics for the run report.

The strategy's pure functions are imported and called directly. No
production module is mocked or copied; any signature drift would
surface immediately as a TypeError.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.backtest import assignment_sim, drawdown_sim
from kai_trader.backtest.broker import BacktestBroker
from kai_trader.backtest.data import bars, earnings
from kai_trader.backtest.data.chains import HistoricalChainFetcher
from kai_trader.backtest.fills import Quote
from kai_trader.backtest.state import BacktestState
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.logging import get_logger
from kai_trader.strategy import candidates as strat_candidates
from kai_trader.strategy import covered_calls as strat_cc
from kai_trader.strategy import profit_take as strat_pt
from kai_trader.strategy import rolls as strat_rolls
from kai_trader.strategy.indicators import SpySnapshot, VixSnapshot
from kai_trader.strategy.iv_rv import RV_LOOKBACK_DAYS
from kai_trader.strategy.regime import RegimeSnapshot, classify

_log = get_logger(__name__)


@dataclass
class TickReport:
    """Per-tick aggregate of what happened. Collected for the run summary."""

    asof: date
    regime: str
    cash: Decimal
    equity: Decimal
    short_options_count: int
    long_equity_count: int
    expired_otm_puts: int
    assigned_puts: int
    expired_otm_calls: int
    called_away_calls: int
    profit_takes_executed: int
    rolls_executed: int
    rolls_held: int
    csp_intents_built: int
    csp_intents_filled: int
    cc_intents_built: int
    cc_intents_filled: int
    drawdown_pct: Decimal
    kill_switch_tripped: bool


@dataclass
class RunOutcome:
    """Top-level result of a backtest run."""

    start: date
    end: date
    starting_capital: Decimal
    final_equity: Decimal
    state: BacktestState
    ticks: list[TickReport] = field(default_factory=list)


def _build_regime_snapshot(asof: date) -> RegimeSnapshot | None:
    """Reconstruct a RegimeSnapshot from cached VIX + SPY at asof.

    Returns None when caches lack enough history at asof to evaluate.
    """
    vix_snap = bars.vix_snapshot_at(asof)
    spy_snap = bars.spy_snapshot_at(asof)
    if vix_snap is None or spy_snap is None:
        return None
    vix_input = VixSnapshot(level=vix_snap.level, five_day_change_pct=vix_snap.five_day_change_pct)
    spy_input = SpySnapshot(
        price=spy_snap.price,
        sma_20=spy_snap.sma_20,
        sma_50=spy_snap.sma_50,
        realized_vol_10d_pct=spy_snap.realized_vol_10d_pct,
    )
    regime = classify(vix_input, spy_input)
    return RegimeSnapshot(
        regime=regime,
        vix=vix_snap.level,
        vix_5d_change_pct=vix_snap.five_day_change_pct,
        spy_price=spy_snap.price,
        spy_20dma=spy_snap.sma_20,
        spy_50dma=spy_snap.sma_50,
        realized_vol_10d_pct=spy_snap.realized_vol_10d_pct,
    )


def _mark_to_market(state: BacktestState, asof: date) -> Decimal:
    """End-of-day MtM of all positions. Returns positions_value.

    * Long shares: marked at the underlying close on asof.
    * Short options: marked at the contract's daily-bar close (a proxy for
      the OPRA mid). Real Alpaca portfolio_value uses NBBO mid for short
      option marks; the previous intrinsic-only mark zeroed out time
      value, which inflated equity during holding periods and biased
      drawdown low. Using the contract's last-trade close keeps the mark
      grounded in real market data while preserving asof-boundedness:
      ``HistoricalChainFetcher`` raises ``LeakageError`` if a bar
      post-dates ``asof``. Falls back to intrinsic when the contract has
      no bar at or before ``asof`` (rare; e.g. the contract is brand new).
    """
    total = Decimal("0")
    for p in state.long_equity_positions:
        close = bars.get_close_on_or_before(p.symbol, asof)
        if close is None:
            total += p.avg_entry_price * p.qty
            continue
        _d, mark = close
        total += mark * p.qty

    if not state.short_option_positions:
        return total

    # Lazy chain fetch keyed by underlying so we hit the cache once per
    # underlying per tick (the typical book has positions clustered on a
    # handful of names).
    chain_cache: dict[str, dict[str, Decimal]] = {}
    for p in state.short_option_positions:
        try:
            underlying, _exp, opt, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        underlying_close = bars.get_close_on_or_before(underlying, asof)
        if underlying_close is None:
            continue
        _d, spot = underlying_close
        if underlying not in chain_cache:
            from kai_trader.backtest.data.chains import get_chain
            try:
                chain = get_chain(underlying, asof)
            except Exception:
                chain = []
            chain_cache[underlying] = {
                c.symbol: ((c.bid or Decimal("0")) + (c.ask or Decimal("0"))) / Decimal("2")
                for c in chain
                if c.bid is not None and c.ask is not None
            }
        mid_for_symbol = chain_cache[underlying].get(p.symbol)
        if mid_for_symbol is None or mid_for_symbol <= 0:
            if opt == "put":
                liability_per_share = max(strike - spot, Decimal("0"))
            else:
                liability_per_share = max(spot - strike, Decimal("0"))
        else:
            liability_per_share = mid_for_symbol
        total -= liability_per_share * Decimal("100") * abs(p.qty)
    return total


async def _run_profit_takes(
    state: BacktestState,
    broker: BacktestBroker,
    fetcher: HistoricalChainFetcher,
    asof: date,
) -> int:
    intents = await strat_pt.evaluate_profit_takes(
        short_option_positions=state.list_short_option_positions(),
        orders=state.orders,
        sleeves=state.get_all_sleeves(),
        chain_fetcher=fetcher,
    )
    executed = 0
    for intent in intents:
        chain = await fetcher(intent.underlying, None)
        contract = next((c for c in chain if c.symbol == intent.option_symbol), None)
        if contract is None or contract.bid is None or contract.ask is None:
            continue
        quote = Quote(bid=contract.bid, ask=contract.ask)
        result = broker.submit_buy_to_close(
            symbol=intent.option_symbol,
            underlying=intent.underlying,
            sleeve=intent.sleeve,
            qty=intent.qty,
            quote=quote,
            asof=datetime.combine(asof, datetime.max.time(), tzinfo=UTC),
            action="profit_take_close",
        )
        if result.outcome == "filled":
            executed += 1
    return executed


async def _run_rolls(
    state: BacktestState,
    broker: BacktestBroker,
    fetcher: HistoricalChainFetcher,
    regime: RegimeSnapshot,
    asof: date,
) -> tuple[int, int]:
    intents = await strat_rolls.evaluate_rolls(
        positions=state.list_short_option_positions(),
        sleeves=state.get_all_sleeves(),
        regime=regime,
        chain_fetcher=fetcher,
        today=asof,
    )
    executed = 0
    held = 0
    for intent in intents:
        if intent.reason != "rolled" or intent.new_option_symbol is None:
            held += 1
            continue
        chain = await fetcher(intent.underlying, None)
        current = next((c for c in chain if c.symbol == intent.current_option_symbol), None)
        new = next((c for c in chain if c.symbol == intent.new_option_symbol), None)
        if current is None or new is None:
            held += 1
            continue
        if current.bid is None or current.ask is None or new.bid is None or new.ask is None:
            held += 1
            continue
        # Two legs sequential: buy-to-close current, sell-to-open new.
        close_quote = Quote(bid=current.bid, ask=current.ask)
        close_result = broker.submit_buy_to_close(
            symbol=intent.current_option_symbol,
            underlying=intent.underlying,
            sleeve=intent.sleeve,
            qty=1,  # PositionSnapshot stores aggregate; close one contract per intent for simplicity
            quote=close_quote,
            asof=datetime.combine(asof, datetime.max.time(), tzinfo=UTC),
            action="roll",
        )
        if close_result.outcome != "filled":
            held += 1
            continue
        new_quote = Quote(bid=new.bid, ask=new.ask)
        open_result = broker.submit_short_put(
            symbol=intent.new_option_symbol,
            underlying=intent.underlying,
            sleeve=intent.sleeve,
            qty=1,
            quote=new_quote,
            asof=datetime.combine(asof, datetime.max.time(), tzinfo=UTC),
            target_delta=intent.new_delta,
            actual_delta=new.delta,
        )
        if open_result.outcome == "filled":
            executed += 1
        else:
            held += 1
    return executed, held


def _historical_rv30(symbol: str, asof: date) -> Decimal | None:
    """Reconstruct annualised realized vol over the trailing 30 trading days.

    Mirrors ``strategy.iv_rv.compute_realized_vol_30d`` but reads from the
    asof-bounded daily-bar cache instead of a live fetcher. Production
    rejects candidates where IV is not at least 1.10x recent RV30; without
    this provider the backtest skipped that gate entirely and accepted
    trades the live strategy would have refused.
    """
    history = bars.get_history_until(symbol, asof, lookback_days=RV_LOOKBACK_DAYS + 5)
    if len(history) < 2:
        return None
    closes = [float(b.close) for b in history[-(RV_LOOKBACK_DAYS + 1):]]
    log_returns: list[float] = []
    for i in range(1, len(closes)):
        prev, curr = closes[i - 1], closes[i]
        if prev <= 0 or curr <= 0:
            continue
        log_returns.append(math.log(curr / prev))
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    if variance <= 0:
        return None
    daily_stdev = math.sqrt(variance)
    annualised = daily_stdev * math.sqrt(252)
    return Decimal(f"{annualised:.6f}")


def _make_rv30_provider(asof: date) -> Callable[[str], Awaitable[Decimal | None]]:
    async def _provider(symbol: str) -> Decimal | None:
        return _historical_rv30(symbol, asof)
    return _provider


def _make_iv_percentile_provider(
    asof: date,
) -> Callable[[str, Decimal], Awaitable[Decimal | None]]:
    """Build an asof-bounded IV-percentile provider for the runner.

    Wraps ``backtest.data.iv_history.iv_percentile_rank`` so the
    strategy code's IV/RV-style filter pattern can call into the
    backtest's pre-built IV history cache. Returns None when fewer
    than 30 prior observations exist (matches production fail-open).
    """
    from kai_trader.backtest.data import iv_history

    async def _provider(symbol: str, current_iv: Decimal) -> Decimal | None:
        return iv_history.iv_percentile_rank(symbol, asof, current_iv)
    return _provider


async def _run_csp_entries(
    state: BacktestState,
    broker: BacktestBroker,
    fetcher: HistoricalChainFetcher,
    regime: RegimeSnapshot,
    asof: date,
) -> tuple[int, int]:
    """Build and submit CSP intents. Returns (built, filled)."""
    account = state.account_snapshot()
    intents, _diagnostics = await strat_candidates.build_intents_with_diagnostics(
        regime=regime,
        sleeves=state.get_all_sleeves(),
        account=account,
        chain_fetcher=fetcher,
        today=asof,
        earnings_status=earnings.earnings_status,
        existing_short_puts=state.list_short_option_positions(),
        today_already_deployed=state.today_deployed,
        cooldown_symbols={s for s in state.cooldown_symbols},
        # Phase 5 retuning (2026-05-09): IV/RV gate disabled in favour
        # of IV percentile alone. Running both was double-gating the
        # candidate stream into 80% rejection. The percentile rank is
        # the proper VRP signal; IV/RV stays available in the codebase
        # but is no longer wired to the runner.
        # rv30_provider=_make_rv30_provider(asof),
        iv_percentile_provider=_make_iv_percentile_provider(asof),
    )
    filled = 0
    for intent in intents:
        chain = await fetcher(intent.symbol, None)
        contract = next((c for c in chain if c.symbol == intent.option_symbol), None)
        if contract is None or contract.bid is None or contract.ask is None:
            continue
        quote = Quote(bid=contract.bid, ask=contract.ask)
        result = broker.submit_short_put(
            symbol=intent.option_symbol,
            underlying=intent.symbol,
            sleeve=intent.sleeve,
            qty=intent.qty,
            quote=quote,
            asof=datetime.combine(asof, datetime.max.time(), tzinfo=UTC),
            target_delta=intent.target_delta,
            actual_delta=intent.actual_delta,
        )
        if result.outcome == "filled":
            filled += 1
            state.cooldown_symbols[intent.symbol] = asof
    return len(intents), filled


async def _run_cc_entries(
    state: BacktestState,
    broker: BacktestBroker,
    fetcher: HistoricalChainFetcher,
    regime: RegimeSnapshot,
    asof: date,
) -> tuple[int, int]:
    intents, _diag = await strat_cc.build_call_intents(
        long_equity_positions=state.list_long_equity_positions(),
        sleeves=state.get_all_sleeves(),
        regime=regime,
        chain_fetcher=fetcher,
        today=asof,
    )
    # Filter out CC intents on shares that already have an open short call.
    existing_calls_by_underlying: set[str] = set()
    for p in state.short_option_positions:
        try:
            u, _e, opt, _s = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if opt == "call":
            existing_calls_by_underlying.add(u)
    filled = 0
    built = 0
    for intent in intents:
        if intent.symbol in existing_calls_by_underlying:
            continue
        built += 1
        chain = await fetcher(intent.symbol, None)
        contract = next((c for c in chain if c.symbol == intent.option_symbol), None)
        if contract is None or contract.bid is None or contract.ask is None:
            continue
        quote = Quote(bid=contract.bid, ask=contract.ask)
        result = broker.submit_short_call(
            symbol=intent.option_symbol,
            underlying=intent.symbol,
            sleeve=intent.sleeve,
            qty=intent.qty,
            quote=quote,
            asof=datetime.combine(asof, datetime.max.time(), tzinfo=UTC),
            target_delta=intent.target_delta,
            actual_delta=intent.actual_delta,
        )
        if result.outcome == "filled":
            filled += 1
    return built, filled


def _expire_cooldowns(state: BacktestState, asof: date, max_age_days: int = 1) -> None:
    """Drop cooldown entries older than ``max_age_days``."""
    expired = [s for s, d in state.cooldown_symbols.items() if (asof - d).days > max_age_days]
    for s in expired:
        del state.cooldown_symbols[s]


def _reset_today_deployment(state: BacktestState, asof: date, last_asof: date | None) -> None:
    """Reset today_deployed at the start of a new trading day."""
    if last_asof is None or last_asof != asof:
        state.today_deployed = Decimal("0")


async def run_tick(
    state: BacktestState,
    broker: BacktestBroker,
    asof: date,
    *,
    last_asof: date | None,
    kill_switch_mode: str = "permanent",
) -> TickReport:
    """Execute one backtest tick at ``asof``.

    ``kill_switch_mode`` is forwarded to ``drawdown_sim.check_and_trip``.
    Production-default ``permanent`` mirrors the live sticky-trip
    behaviour. ``auto_reset`` mimics realistic operator intervention:
    the flag clears once equity recovers above the trip-time HWM.
    """
    _reset_today_deployment(state, asof, last_asof)
    _expire_cooldowns(state, asof)

    expiry_result = assignment_sim.simulate_expiries(state, broker, asof)
    regime = _build_regime_snapshot(asof)
    if regime is None:
        positions_value = _mark_to_market(state, asof)
        state.append_equity(asof, positions_value)
        return TickReport(
            asof=asof,
            regime="unknown",
            cash=state.cash,
            equity=state.cash + positions_value,
            short_options_count=len(state.short_option_positions),
            long_equity_count=len(state.long_equity_positions),
            expired_otm_puts=expiry_result.puts_expired_otm,
            assigned_puts=expiry_result.puts_assigned,
            expired_otm_calls=expiry_result.calls_expired_otm,
            called_away_calls=expiry_result.calls_called_away,
            profit_takes_executed=0,
            rolls_executed=0,
            rolls_held=0,
            csp_intents_built=0,
            csp_intents_filled=0,
            cc_intents_built=0,
            cc_intents_filled=0,
            drawdown_pct=Decimal("0"),
            kill_switch_tripped=False,
        )

    fetcher = HistoricalChainFetcher(asof=asof)
    profit_takes = await _run_profit_takes(state, broker, fetcher, asof)
    rolls_done, rolls_held = await _run_rolls(state, broker, fetcher, regime, asof)
    csp_built, csp_filled = await _run_csp_entries(state, broker, fetcher, regime, asof)
    cc_built, cc_filled = await _run_cc_entries(state, broker, fetcher, regime, asof)

    positions_value = _mark_to_market(state, asof)
    state.append_equity(asof, positions_value)
    dd = drawdown_sim.check_and_trip(state, asof, mode=kill_switch_mode)  # type: ignore[arg-type]

    return TickReport(
        asof=asof,
        regime=regime.regime,
        cash=state.cash,
        equity=state.cash + positions_value,
        short_options_count=len(state.short_option_positions),
        long_equity_count=len(state.long_equity_positions),
        expired_otm_puts=expiry_result.puts_expired_otm,
        assigned_puts=expiry_result.puts_assigned,
        expired_otm_calls=expiry_result.calls_expired_otm,
        called_away_calls=expiry_result.calls_called_away,
        profit_takes_executed=profit_takes,
        rolls_executed=rolls_done,
        rolls_held=rolls_held,
        csp_intents_built=csp_built,
        csp_intents_filled=csp_filled,
        cc_intents_built=cc_built,
        cc_intents_filled=cc_filled,
        drawdown_pct=dd.drawdown_pct,
        kill_switch_tripped=dd.kill_switch_tripped,
    )


async def run_backtest(
    state: BacktestState,
    broker: BacktestBroker,
    trading_days: list[date],
    *,
    kill_switch_mode: str = "permanent",
) -> RunOutcome:
    """Walk every trading day; collect TickReport per day."""
    reports: list[TickReport] = []
    last_asof: date | None = None
    for d in trading_days:
        try:
            report = await run_tick(
                state, broker, d, last_asof=last_asof,
                kill_switch_mode=kill_switch_mode,
            )
        except Exception as exc:
            _log.error(
                "backtest.tick_failed",
                asof=d.isoformat(),
                error=str(exc),
            )
            raise
        reports.append(report)
        last_asof = d
        if report.kill_switch_tripped:
            _log.warning(
                "backtest.kill_switch_tripped_continuing_no_new_entries",
                asof=d.isoformat(),
            )
    return RunOutcome(
        start=trading_days[0] if trading_days else date.today(),
        end=trading_days[-1] if trading_days else date.today(),
        starting_capital=state.starting_capital,
        final_equity=state.cash + (reports[-1].equity - state.cash if reports else Decimal("0")),
        state=state,
        ticks=reports,
    )
