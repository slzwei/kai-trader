# Kai Trader: Backtest Harness Plan

## Purpose

Determine whether the wheel strategy, as currently calibrated in production,
would have been profitable on real historical data. Reliability and accuracy
take precedence over speed, breadth, and convenience. Where bias is
unavoidable, prefer pessimistic bias over optimistic bias.

## Goal

Stand up a deterministic, replay-style backtest harness that:

- Reuses the pure intent builders already in `src/kai_trader/strategy/` without
  duplicating logic.
- Runs against ~2 years of real Alpaca OPRA option data (Feb 2024 onward) plus
  long-history equities, VIX, and a paid historical earnings calendar.
- Produces per-tick equity, drawdown, sleeve attribution, and a trade ledger
  shaped like the live `orders` table.
- Survives a future-leakage audit and a live-paper reconciliation before any
  number is reported.

## Non-goals

- Live option data backfill before Alpaca's history start (Feb 2024).
- Parameter sweeps or walk-forward optimisation.
- Web UI. CLI plus flat-file outputs only.
- Any modification to production strategy code. The harness consumes it; it
  does not change it.

## Decisions (deterministic)

These are settled. The harness ships exactly this way.

1. **Earnings and dividends source: EODHD Calendar API.** $19.99/mo. Reason:
   published 97.25% exact-date precision on earnings vs. the Nasdaq benchmark,
   positioned as the fundamentals specialist (earnings is fundamentals data).
   Cheaper than Polygon and more directly validated for this data type.
   `EODHD_API_KEY` required in `.env` before Phase A6 runs. Backtest must
   spot-check 30 random historical earnings against SEC 8-K filings before
   results are trusted; require ≥27/30 exact-date matches.
2. **Backtest start date: 2024-03-01.** First full month of reliable Alpaca
   options data. No synthetic chain extrapolation; we only report what the
   data can support.
3. **Backtest end date: T-7 days from today.** Avoids tail-window data
   freshness issues at the option chain level.
4. **Starting capital: $100,000.** Matches the production paper account.
   Codified in `BacktestConfig` defaults.
5. **Sleeve config source: snapshot of current production `sleeve_config` at
   backtest run time.** SHA of the snapshot recorded in `summary.md`.
   Replaying historical configs from `decision_log` is out of scope for now.
6. **Persistence: flat files only.** No writes to Supabase. Each run writes
   to `backtest_runs/<timestamp>/`. Idempotent and reproducible.
7. **Default fill model: `mid_minus_half_spread`.** Headline reports use this.
   `mid` is sensitivity-only and always tagged "optimistic" in output.
8. **Two-tick fill rule.** A limit must be inside the spread (i.e. fillable)
   for two consecutive 5-minute ticks before it fills. Pessimistic bias.
9. **Sequential leg roll execution.** Rolls execute as two legs across two
   ticks, not atomically. If leg 2 fails to fill within 3 ticks of leg 1,
   the roll is broken and surfaced in the ledger.
10. **Continuous-data approximation: 5-minute ticks for everything, with a
    documented bias note.** A 1-minute mode for roll-trigger evaluation only
    is deferred; the bias is acknowledged in `summary.md`.
11. **Transaction costs included.** OCC clearing fee + ORF + SEC fee on every
    options leg. Defaults: $0.05/contract clearing, $0.02925/contract ORF on
    sells, $0.0000278 × notional SEC fee on sells. Configurable.
12. **Dividend modelling on long stock positions.** Ex-dividend dates from
    Polygon are credited to cash on the ex-date.

## What we lean on

The strategy is already factored for replay. Each pure function takes injected
data; the harness swaps live fetchers for historical ones.

| Module | Injected dep we override |
|---|---|
| `regime.classify(vix, spy)` | historical `VixSnapshot` / `SpySnapshot` |
| `candidates.build_intents_with_diagnostics(...)` | `chain_fetcher`, `existing_short_puts`, `earnings_filter` |
| `rolls.find_roll_candidate(...)` | `chain_fetcher` |
| `profit_take.scan_for_profit_takes(...)` | `chain_fetcher` |
| `assignment.detect_assignments(...)` | broker position snapshots |
| `covered_calls.build_cc_intents(...)` | `chain_fetcher`, broker long stock |
| `earnings.is_earnings_in_window(...)` | historical earnings calendar |

If a function reads from Postgres directly (sleeve config, flags, recent
orders, short option positions), the harness binds an in-memory
`BacktestState` that satisfies the same read shape.

## Data sources

| Need | Source | Notes |
|---|---|---|
| Equities daily bars | Alpaca `StockHistoricalDataClient`, SIP feed | 2016+. Cached locally. |
| Equities intraday (for fills, expiries) | Alpaca `StockHistoricalDataClient`, SIP feed, 5-min bars | Cached. |
| VIX daily | yfinance `^VIX` | Long history. Cache to parquet. |
| Option bars and quotes | Alpaca `OptionHistoricalDataClient` | Feb 2024 onward. Hard limit on backtest start. |
| Greeks and IV | Reconstruct via Black-Scholes from mid + risk-free rate | See Phase A3. |
| Risk-free rate (3M T-bill) | FRED `DGS3MO` | Free, no key. |
| Earnings calendar | EODHD `/api/calendar/earnings` | Required. Phase A6. |
| Dividends and ex-dates | EODHD `/api/calendar/dividends` and `/api/div/{symbol}` | Phase A6. |
| Listing/delisting proxy | Alpaca daily bar presence at `asof_dt` | Phase A5. No third-party listing-history dependency. |

## Architecture

New package, isolated from production paths.

```
src/kai_trader/backtest/
  __init__.py
  cli.py                # `uv run python -m kai_trader.backtest ...`
  config.py             # BacktestConfig (start, end, capital, fill model)
  clock.py              # iterator over historical NYSE session ticks
  state.py              # BacktestState: cash, positions, orders, sleeve snapshot, kill_switch
  broker.py             # BacktestBroker: submits/fills orders against historical chains
  costs.py              # TransactionCostModel (OCC + ORF + SEC)
  data/
    chains.py           # HistoricalChainFetcher (Alpaca options + Greeks reconstruction)
    greeks.py           # BS pricer + Newton-Raphson IV solver
    rates.py            # FRED 3M T-bill cache
    bars.py             # Equity daily and intraday + VIX cache
    earnings.py         # EODHD historical earnings calendar
    dividends.py        # EODHD dividends and ex-dates
    universe.py         # Survivorship-aware whitelist resolver (Alpaca bar presence)
  fills.py              # FillModel (mid_minus_half_spread default, two-tick rule)
  assignment_sim.py     # Expiry assignment logic (puts) and ITM call assignment (CCs)
  drawdown_sim.py       # Mirrors strategy/drawdown.py: kill_switch auto-trip
  reporting/
    equity.py           # Equity curve, drawdown, Sharpe, Sortino
    trades.py           # Trade ledger CSV
    sleeves.py          # Per-sleeve P&L attribution
    summary.py          # summary.md generator
  audit/
    leakage.py          # CI-runnable future-leakage gate
  runner.py             # Top-level orchestrator
tests/backtest/
  ...                   # Unit tests per module + a 1-week golden replay
scripts/
  run_backtest.sh       # Convenience wrapper
backtest_runs/
  <timestamp>/
    config.json
    sleeve_config_snapshot.json
    equity.parquet
    trades.csv
    sleeve_attribution.csv
    coverage_report.csv
    summary.md
```

## Reliability principles (codified)

These are non-negotiable invariants the implementation must enforce.

- **No future leakage.** Every data fetch carries an `asof_dt`. Hard asserts in
  `HistoricalChainFetcher`, `bars`, `greeks`, `earnings`, `dividends`, `rates`
  reject any row with `timestamp > asof_dt`. The CI-runnable
  `audit/leakage.py` re-checks this on a fuzzed sample of asofs.
- **Survivorship-aware universe.** The trading universe at `asof_dt` is the
  intersection of (a) today's sleeve whitelist, (b) symbols that were listed
  and continuously traded at `asof_dt`, and (c) symbols meeting a per-tick
  data-coverage threshold. Resolved by `data/universe.py`.
- **Pessimistic-by-default execution.** `mid_minus_half_spread` fill model,
  two-tick fill rule, sequential leg rolls, transaction costs always on.
- **Strategy code is imported, not copied.** The backtest uses the production
  modules under `src/kai_trader/strategy/` directly. Any divergence is a bug.
  `summary.md` records the git SHA at run time.
- **Capital invariants.** Cash never goes negative. Every short put is backed
  by cash in the same sleeve. Every short call is backed by ≥100 shares of
  the underlying. Hard asserts on every state mutation.
- **Determinism.** No RNG anywhere. Same inputs produce the same outputs
  byte-for-byte. Run hash recorded in `summary.md`.

## Phases

### Phase A: Data spine

Standalone, no strategy code yet.

- A1. `data/bars.py`: cache and fetch SPY daily bars and 5-min intraday bars
  (Alpaca SIP) and ^VIX daily (yfinance) to local parquet. Idempotent.
  Asof-bounded reads.
- A2. `data/rates.py`: cache FRED `DGS3MO` daily series. Asof-bounded reads.
- A3. `data/greeks.py`: Black-Scholes call and put pricer + Newton-Raphson IV
  solver. Validate reconstruction error against current Alpaca live snapshots
  for ≥50 contracts spanning the sleeve whitelist; require median error <1%
  and p95 <3%. If thresholds fail, calibrate IV anchor to Alpaca's live
  snapshot at fetch time and re-validate.
- A4. `data/chains.py`: `HistoricalChainFetcher.get_chain(symbol, asof_dt,
  expiration=None)` returns chains shaped exactly like the live
  `OptionContract` dataclass, with reconstructed Greeks. Per-(symbol, date)
  parquet cache. Hard asserts on `asof_dt`. Coverage report per (symbol,
  expiration) showing percent of expected ticks with quote data; strikes
  below 80% coverage are excluded and counted in the report.
- A5. `data/universe.py`: survivorship-aware whitelist resolver. For each
  `asof_dt`, returns the subset of today's sleeve whitelist that has
  continuous Alpaca daily bars on or before `asof_dt`. Uses bar presence as
  the listing proxy (more accurate than formal listing dates for our
  purposes: the question is "could we have actually traded this then").
- A6. `data/earnings.py` and `data/dividends.py`: EODHD historical earnings
  and dividends. Asof-bounded. `is_earnings_in_window(symbol, asof_dt,
  dte_days)` matches the live `earnings.is_earnings_in_window` signature.
  Includes a spot-check harness that samples 30 random earnings dates across
  the whitelist and validates each against SEC 8-K filings; requires ≥27/30
  exact match before the harness is trusted.

**Acceptance:** loading 2024-03-01 through T-7 for the survivorship-resolved
whitelist completes in under 60 minutes on a fresh cache; chain rows match
live snapshots when called for today; A3 thresholds met; coverage report
generated; zero rows with `timestamp > asof_dt` across a 1000-asof fuzz.

### Phase B: Replay engine

- B1. `clock.py`: yield 5-minute ticks during NYSE sessions between `start`
  and `end`, skipping holidays. Use `pandas_market_calendars`.
- B2. `state.py`: in-memory `BacktestState` exposing the same read methods the
  strategy uses (`get_all_sleeves`, `get_all_flags`, `recent_orders`,
  `list_short_option_positions`, `list_long_equity_positions`). Seeded from a
  snapshot of current production rows. Includes `kill_switch` state.
- B3. `costs.py`: `TransactionCostModel` charges OCC clearing + ORF + SEC fee
  per leg.
- B4. `broker.py`: `BacktestBroker` exposing `submit_short_put`,
  `submit_short_call`, `submit_buy_to_close`, `close_position`. Records
  intents, fills via `FillModel` (two-tick rule), applies costs, mutates
  `state`. Sequential leg execution for rolls.
- B5. `runner.py`: walk the clock; at each tick, pass `state` plus
  `BacktestBroker` plus `HistoricalChainFetcher` into a noop step.

**Acceptance:** runner completes a 1-month dry walk in under 60 seconds with
no strategy plugged in. State remains coherent (cash unchanged, zero orders
submitted).

### Phase C: Plug in the strategy

Existing pure functions get wired in. Production strategy code does not move.

- C1. Wire `regime.classify` per-day from cached VIX + SPY snapshots.
- C2. Wire `candidates.build_intents_with_diagnostics` with `existing_short_puts`
  from `state` (Phase 5e collateral subtraction must work end-to-end; included
  as a regression test) and `earnings_filter` bound to Polygon-backed
  `is_earnings_in_window`.
- C3. Submit each intent through `BacktestBroker`; `FillModel` decides whether
  the limit fills (two-tick rule).
- C4. Wire `rolls.find_roll_candidate` and execute net-credit rolls
  sequentially across ticks.
- C5. Wire `profit_take` scan and close.
- C6. `assignment_sim.py`: at each option expiry, ITM short puts assign,
  producing long shares and a cash debit; ITM short calls assign, removing
  shares and crediting cash. `assignment.detect_assignments` runs against the
  new state.
- C7. Wire `covered_calls.build_cc_intents` and submit.
- C8. `drawdown_sim.py`: mirror `strategy/drawdown.py` exactly. On each tick,
  compute 7-day high-water mark from the backtest equity curve; if equity is
  down ≥7%, auto-engage `kill_switch` in `state`. Record the trip in the
  trade ledger as a synthetic event.
- C9. Dividend credits on long stock positions on ex-dates.

**Acceptance:** a 3-month run completes; trade ledger satisfies invariants
(no short put without collateral, no CC without 100 shares, every assignment
leaves cash plus shares consistent); the Phase 5e regression case passes
(no re-attempt of already-held strikes); kill_switch trips correctly when
drawdown breaches threshold.

### Phase D: Reporting

- D1. End-of-day equity curve: cash + position mark-to-market via historical
  option mids and stock closes.
- D2. Drawdown series, max-DD, annualised Sharpe (vs. risk-free), Sortino,
  Calmar.
- D3. Per-sleeve P&L attribution. Rolls and CCs attributed to their original
  sleeve.
- D4. Trade ledger CSV in the shape of the production `orders` table, plus
  realised P&L per closed position, plus transaction costs column.
- D5. `coverage_report.csv`: per (symbol, expiration) data coverage.
- D6. `summary.md`: human-readable per-run report. Includes:
  - Run config, git SHA, sleeve config snapshot SHA, run hash.
  - Equity, max-DD, Sharpe, Sortino under headline fill model.
  - Sensitivity table: same metrics under `mid` and `mid_minus_quarter_spread`.
  - Symbols excluded by survivorship and by coverage.
  - **Mandatory disclaimer** (boilerplate, hardcoded): "These results are
    directional, not predictive. The 2-year window does not include the 2020
    or 2022 stress regimes. Microstructure (queue position, slippage on wide
    quotes, partial fills) is not fully modelled. Live results will differ."

**Acceptance:** a 6-month run produces all artefacts; numbers tie out (sum of
realised trade P&L + open-position MtM − transaction costs + dividends =
end equity − start equity, within $0.01).

### Phase E: Validation

Gate before any number is reported as truth.

- E1. **Future-leakage CI gate** (`audit/leakage.py`). Promoted to a
  CI-runnable command: `uv run python -m kai_trader.backtest.audit.leakage`.
  Runs on every PR that touches `src/kai_trader/backtest/data/`. Fuzzes 1000
  random `(symbol, asof_dt)` pairs across the window; asserts no fetched row
  has `timestamp > asof_dt`. Failures block merge.
- E2. **Live-paper reconciliation.** Pick a window of ≥30 real entry trades
  from production paper. Replay through the harness with the matching sleeve
  config snapshot. Per-trade match rate target: ≥80% same-strike same-DTE for
  entries; equity drift <5%. If paper has fewer than 30 entries, defer E2
  until it does, but ship the rest of the harness with the deferred-E2 status
  printed in `summary.md`.
- E3. **Slippage sensitivity.** Same window under three fill models (`mid`,
  `mid_minus_quarter_spread`, `mid_minus_half_spread`). Reported in `summary.md`.
- E4. **Capital invariants.** Hard asserts run on every state mutation. Any
  violation aborts the run with a diagnostic dump.
- E5. **Sequential-leg roll regression.** Test that simulates a roll where leg
  1 fills but leg 2 does not within 3 ticks; assert the position is left in
  a broken-roll state and surfaced in the ledger.
- E6. **Phase 5e collateral regression.** Test that asserts the harness does
  not re-attempt strikes already held as short puts.

## Acceptance for "the harness works"

A single command:

```
uv run python -m kai_trader.backtest \
  --start 2024-03-01 --end <T-7> \
  --capital 100000 \
  --output backtest_runs/$(date +%s)
```

produces equity curve, trade ledger, drawdown report, sensitivity table,
coverage report, and `summary.md` with the mandatory disclaimer. E1 passes
in CI; E2 either passes or is explicitly deferred with reason; E4 invariants
hold across the entire run.

## Out of scope, explicitly

- Parameter optimisation. The backtest answers profitability; it does not
  search the parameter space.
- Walk-forward analysis.
- Modifying production code in `src/kai_trader/strategy/`.
- Multi-broker support.
- 1-minute tick mode.
- Replaying historical sleeve configs from `decision_log`.

## Appendix: FMEA

Failure modes considered during planning. Scoring: Severity × Occurrence ×
Detection = RPN, each 1-10. RPN ≥ 200 required design-time mitigation; all
have been folded into the plan above.

| # | Failure mode | Effect | S | O | D | RPN | Where mitigated |
|---|---|---|---:|---:|---:|---:|---|
| 1 | Backtest looks accurate, reality differs (microstructure not modelled) | Trust over-extended; live underperforms | 9 | 7 | 8 | 504 | D6 mandatory disclaimer; E2 reconciliation tolerance |
| 2 | Survivorship bias in sleeve whitelist | Symbols on today's list missing from past; delisted names absent | 7 | 8 | 8 | 448 | A5 universe resolver |
| 3 | Future leakage (cached data with wrong asof) | Backtest looks fantastic, signal is fake | 10 | 5 | 8 | 400 | Reliability principles; E1 CI gate |
| 4 | Leakage audit incomplete | The one safety net for #3 has holes | 10 | 5 | 7 | 350 | E1 fuzzed CI gate |
| 5 | Sleeve config snapshot drift | Backtest config doesn't reflect what was live | 7 | 8 | 6 | 336 | Decision 5; snapshot SHA in summary.md |
| 6 | Earnings filter wrong / disabled / stale | Bias on names where assignment around earnings hurt | 8 | 6 | 7 | 336 | Decision 1: Polygon paid feed |
| 7 | Overfitting to backtester | Strategy ends up tuned to synthetic environment | 8 | 5 | 7 | 280 | Out-of-scope: no parameter optimisation |
| 8 | Fill model assumes instant intra-tick fill | Limit orders fill that wouldn't have crossed | 5 | 8 | 7 | 280 | Decision 8: two-tick rule |
| 9 | Atomic roll execution | Underestimates rolls that would have failed mid-execution | 6 | 7 | 6 | 252 | Decision 9: sequential legs; E5 regression |
| 10 | Alpaca historical option data has gaps | Strikes silently skipped | 7 | 7 | 5 | 245 | A4 coverage report; 80% threshold |
| 11 | Mid-fill optimism | Returns inflated 30-50% | 9 | 9 | 3 | 243 | Decision 7: mid_minus_half_spread default |
| 12 | Drawdown circuit breaker not modelled | Equity diverges from what live would have done | 7 | 6 | 5 | 210 | C8 drawdown_sim |
| 13 | Tick-boundary roll triggers vs continuous | Misses or delays triggers | 5 | 7 | 6 | 210 | Decision 10: documented bias |
| 14 | Phase 5e collateral subtraction not replicated | Validates broken behaviour | 8 | 5 | 4 | 160 | C2 + E6 regression |
| 15 | Live-paper reconciliation has too few trades | Ground-truth check is meaningless | 7 | 7 | 3 | 147 | E2 minimum 30 trades; defer if not met |
| 16 | Greeks reconstruction error > target | Wrong strikes selected | 8 | 6 | 3 | 144 | A3 thresholds + IV anchor fallback |
| 17 | Strategy code drifts | Stale results | 6 | 8 | 3 | 144 | Reliability principles: import not copy; SHA pin |
| 18 | Dividend credits missing on long stock | Equity understated post-assignment | 5 | 6 | 4 | 120 | Decision 12; C9 |
| 19 | Transaction costs missing | Returns overstated | 6 | 5 | 3 | 90 | Decision 11; B3 costs.py |
