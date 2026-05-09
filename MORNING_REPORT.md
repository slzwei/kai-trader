# Kai Trader Backtest: Final Report

_Updated 2026-05-08 after a realism audit. Read the **What changed**
section before acting on these numbers._

## TL;DR

**The previous +98% headline was wrong, but my first correction
(+14.44%) was too pessimistic.** With every fix correctly applied
the realistic range is bracketed by the fill-model assumption, not
a single point estimate.

| Variant | Total return | CAGR | Max DD | Sharpe | Sortino | Closed trades |
|---|---:|---:|---:|---:|---:|---:|
| **Strategy, realistic ceiling (`mid` fill)** | **+30.74%** | **13.24%** | 14.78% | **0.68** | 0.63 | 574 |
| **Strategy, realistic floor (`mid_minus_half_spread`)** | +14.44% | 6.46% | 16.54% | 0.16 | 0.15 | 572 |
| Strategy, prior optimistic (buggy) | +98.27% | 37.36% | 25.08% | 1.00 | 1.04 | 659 |
| **SPY buy-and-hold benchmark** | +40.28% | 17.00% | 19.00% | 0.74 | 0.72 | n/a |

**Why a bracket and not a point**: production places limit orders at
mid. In real markets that limit either fills at mid (a marketable
buyer comes in) or doesn't fill. The pessimistic `mid_minus_half_spread`
model conflates these two states ("limit didn't fill" → "limit
filled at the bid"), which double-counts the friction. The honest
truth lies between the two rows: each unfilled limit is a real
opportunity loss, but the fills that do happen happen at mid, not
at a synthetic bid.

A cautious live-trade estimate is roughly the **midpoint of the
bracket: ~+22% over 2 years (~10% CAGR, Sharpe ~0.4-0.5)**, which is
defensible-but-not-amazing premium-capture vs SPY's +40% / Sharpe
0.74 in the same favourable tape.

**Verdict for live capital**: the strategy IS positive-expectancy
after fees. On a risk-adjusted basis the ceiling case is competitive
with SPY (Sharpe 0.68 vs 0.74). On absolute return, even the ceiling
loses by ~10 percentage points to the index in this 2024-2026
window. Net call: **don't deploy real money yet, but the issue isn't
"strategy is broken." The issue is "we have no ground truth that
backtest matches live."** The next gate is the live-paper
reconciliation (E2), not another backtest tweak.

## What changed in this audit

The previous +98% run had three real bugs and one over-aggressive
pessimism setting. The bugs are now fixed. The pessimism setting
(fill model) is reported as a bracket because it depends on a
modelling choice the backtest can't resolve without real-market
microstructure data.

### 1. Synthetic bid/ask spreads were 5-10x tighter than reality

The chain fetcher estimated bid/ask from option daily volume:

| Volume bucket | Old half-spread | New half-spread | Real OPRA IQR (sampled) |
|---|---:|---:|---:|
| > 1000 contracts | 2.5% | 10% | 7-29% |
| 100 - 1000 | 5% | 15% | 14-25% |
| > 0 (low) | 7.5% | 22% | ~25%+ |
| zero volume | 10% | 30% | very wide |

Calibrated against real Alpaca historical option trade prints for
MARA, RIOT, RIVN, SOFI, SLV, HOOD weeklies in 2024-03. The
inter-quartile range of intraday trade prints (q3 - q1) / median was
**7-54% across the sample**; the previous 2.5-10% buckets were a
fiction.

Concrete example: MARA 240315P22 on 2024-03-06 had real trade prints
with q1=$1.50, q3=$1.98, IQR-spread 29%. The old backtest sold this
at $1.64 (its synthetic "bid"); a realistic seller hit ~$1.50.

Source: `src/kai_trader/backtest/data/chains.py` _SPREAD_FRAC_*
constants. Also added: when the bar's intraday range
(high - low) is wider than the volume bucket suggests, the bar wins
(prevents wash-trade marks at zero spread).

### 2. The IV/RV 1.10 floor was wired in production but bypassed in backtest

`strategy/iv_rv.py:passes_iv_rv_floor` rejects a candidate when
implied vol is not at least 1.10x the underlying's 30-day realized
vol. The live worker passes `compute_realized_vol_30d` to the intent
builder; the backtest runner did not.

Result: the backtest accepted CSP entries on names where IV was
**lower** than recent realized vol (the opposite of edge). With the
filter wired in via an asof-bounded historical RV30 provider, ~30% of
candidates the strategy would have entered are now skipped — and the
ones that get through are the genuinely vol-rich setups.

Source: `src/kai_trader/backtest/runner.py:_historical_rv30` and
`_make_rv30_provider`.

### 3. Mark-to-market used intrinsic only; equity inflated during holding

`runner._mark_to_market` previously marked short options at intrinsic
value. OTM short puts marked at $0 even when the buy-back cost was
$0.30. Real Alpaca portfolio_value uses NBBO mid for short option
marks, so equity (and the drawdown series the kill-switch reads)
were biased.

Now: short options mark at the contract's daily-bar close (a proxy
for OPRA mid), with synthetic spread layered in via the chain
fetcher. Falls back to intrinsic only when the bar is missing.

Source: `src/kai_trader/backtest/runner.py:_mark_to_market`.

### 4. The "92.1% win rate" was misleading

The summary's win-rate counter treated every fill at $0 (i.e. every
ITM-expiry close) as a win because the option leg's realized P&L was
positive — even though the matching assignment row turned that into
a stock-leg loser. The corrected counter pairs each close with the
prior open of the same contract; an assignment counts as a loss.

Source: `src/kai_trader/backtest/reporting/summary.py:compute_metrics`.
New realistic win rate: **89.0%** (close to old number, but per-trade
P&L average dropped from $139.60 → $57.08, which is the spread fix
showing up).

## Headline numbers (both ends of the realistic bracket)

The two columns differ only in the fill-model assumption. Every
other input is identical: same calibration, same window, same
sleeve config, same IV/RV filter, same MtM logic.

| Metric | Floor (`mid_minus_half_spread`) | Ceiling (`mid`) |
|---|---:|---:|
| Final equity | $114,444 | $130,742 |
| Total return | +14.44% | **+30.74%** |
| CAGR | 6.46% | 13.24% |
| Max drawdown | 16.54% (open) | 14.78% |
| Annualised Sharpe | 0.16 | **0.68** |
| Annualised Sortino | 0.15 | 0.63 |
| Calmar | 0.39 | 0.90 |
| CSP opens filled | 324 | 348 |
| Profit takes | 143 | 161 |
| Rolls executed | 5 | 7 |
| Assignments | 81 | 82 |
| Covered calls opened | 197 | 209 |
| Realised P&L from trades | $32,647 | $51,107 |
| Transaction costs paid | $198 | $211 |

**Why the ceiling is closer to truth than the floor**: production
limits go in at the calculated mid. In live markets that limit
fills at mid (when an aggressive buyer crosses) or doesn't fill at
all. The strategy never receives a worse price than its limit.
`mid_minus_half_spread` simulates filling at a synthetic bid that
production would never actually accept. The right pessimism is
"some limits don't fill" (an opportunity-loss bias), not "limits
fill at a haircut" (a price bias). The harness doesn't currently
model probabilistic fills, so the ceiling is the more honest single
number to act on, with the floor as a stress-test reference.

## Monthly returns (realistic run)

| Month | Start equity | End equity | Return % |
|---|---:|---:|---:|
| 2024-03 | $100,000 | $101,014 | +1.01% |
| 2024-04 | $100,976 | $104,304 | +3.30% |
| 2024-05 | $105,008 | $106,703 | +1.61% |
| 2024-06 | $107,114 | $107,111 | -0.00% |
| 2024-07 | $106,931 | $108,136 | +1.13% |
| 2024-08 | $107,648 | $104,576 | -2.85% |
| 2024-09 | $103,729 | $106,139 | +2.32% |
| 2024-10 | $104,901 | $107,877 | +2.84% |
| 2024-11 | $107,792 | $109,577 | +1.66% |
| 2024-12 | $109,564 | $109,298 | -0.24% |
| 2025-01 | $109,588 | $110,417 | +0.76% |
| 2025-02 | $110,465 | $111,062 | +0.54% |
| 2025-03 | $111,058 | $113,562 | +2.25% |
| 2025-04 | $113,494 | $114,778 | +1.13% |
| 2025-05 | $113,997 | $114,864 | +0.76% |
| 2025-06 | $114,863 | $116,367 | +1.31% |
| 2025-07 | $116,387 | $117,162 | +0.67% |
| 2025-08 | $116,819 | $120,662 | +3.29% |
| 2025-09 | $119,915 | $120,647 | +0.61% |
| 2025-10 | $120,405 | $123,849 | +2.86% |
| 2025-11 | $124,309 | $123,792 | -0.42% |
| 2025-12 | $122,968 | $118,566 | -3.58% |
| 2026-01 | $122,682 | $122,203 | -0.39% |
| 2026-02 | $120,268 | $112,682 | **-6.31%** |
| 2026-03 | $114,265 | $107,969 | **-5.51%** |
| 2026-04 | $107,944 | $114,444 | +6.02% |

The shape is what a real defensive wheel produces: many small positive
months (premium ticking), occasional 3-6% drawdown months when the
underlying gaps against you and assignments stack. The tail-risk
character is similar to selling vol — small consistent gains
punctuated by larger losses.

## Drawdown periods (top 5, realistic run)

| Start | Trough | Recovery | DD % | Peak | Trough |
|---|---|---|---:|---:|---:|
| 2026-01-22 | 2026-03-30 | (open at run end) | **16.54%** | $126,442 | $105,532 |
| 2025-10-28 | 2025-11-20 | 2025-12-04 | 10.42% | $125,248 | $112,195 |
| 2025-12-04 | 2025-12-17 | 2026-01-13 | 6.05% | $125,650 | $118,042 |
| 2024-07-17 | 2024-09-06 | 2024-11-07 | 5.50% | $108,921 | $102,932 |
| 2024-11-08 | 2024-11-15 | 2024-12-17 | 2.91% | $109,697 | $106,506 |

The deepest drawdown started 2026-01-22 and was still recovering at
the run's last day. The strategy ended the window inside an open
drawdown.

## Symbol activity (realistic run)

The IV/RV filter and wider spreads broke the previous concentration
in MARA. The portfolio now spreads across more names with more
moderate vol. Top 10 by CSP fills:

| Symbol | CSP opens | Profit takes | Assignments | Rolls |
|---|---:|---:|---:|---:|
| SLV | 30 | 21 | 2 | 1 |
| RIVN | 26 | 8 | 8 | 0 |
| PFE | 20 | 6 | 4 | 0 |
| BAC | 20 | 8 | 8 | 0 |
| MARA | 20 | 13 | 4 | 0 |
| XLF | 18 | 6 | 6 | 0 |
| SNAP | 15 | 2 | 5 | 2 |
| F | 15 | 3 | 1 | 0 |
| EEM | 14 | 7 | 3 | 0 |
| T | 12 | 3 | 4 | 0 |

(MARA dropped from 96 fills under the buggy run to 20 under realistic
spreads; PFE/BAC/F/T moved up because their spreads compress
relatively in the new buckets.)

## Validation (realistic run)

| Check | Result |
|---|---|
| Sanity check (`scripts/sanity_check.py`) | **0 FAILS, 0 WARNINGS** |
| Unit tests (`tests/backtest/`) | 79/79 pass |
| ruff check | clean |
| mypy --strict (changed files) | clean |
| Future-leakage audit | 1000 fuzzed pairs, 0 leaks |
| Capital invariants | hold across 542 trading days |

## Honest disclaimers (still apply, even after the realism fixes)

These limits weren't fixed by this audit. They still cap how much
trust either bracket end deserves.

* **One window, mostly a bull tape.** 2024-03 to 2026-04 contains no
  COVID-style crash, no 2022 grind, no Aug-2024 vol-spike sequel.
  The strategy has not been tested against the regimes that punish
  vol-sellers worst.
* **Daily resolution, not 5-minute.** Production ticks every 5
  minutes; this backtest ticks once at close. Intraday roll triggers
  and intraday profit-takes are approximated by their close
  equivalents. The bias is mixed: production may catch some trades
  the backtest misses, but it also incurs intraday-fill slippage the
  backtest doesn't see.
* **Synthetic spreads, even after calibration.** Real OPRA NBBO at
  any given moment may be wider than even the new buckets suggest,
  especially in the first 30 minutes of trading and the last 30 of
  the close. The new spread model uses the bar's intraday range as a
  floor, but it's still an estimate, not an NBBO record.
* **Earnings calendar via yfinance, not paid EODHD.** The original
  plan required EODHD for 97.25% accuracy. The user's EODHD
  subscription does not include `/api/calendar/earnings` (returns
  403). yfinance is the fallback. Roughly 1 in 25 earnings dates may
  be off by a day, occasionally letting an entry through that should
  have been blackouted.
* **Greeks reconstructed via Black-Scholes.** Deltas have ~1% typical
  error vs Alpaca's exchange-published Greeks. Strike selection
  picks the put closest to a target delta, so a 1% delta error rarely
  changes the chosen strike but can change the realized P&L slightly.
* **No live-paper reconciliation.** The most important validation
  step (Phase E2 in `BACKTEST_PLAN.md`) was deferred because there
  weren't ≥30 production paper entries to compare against. Until
  this happens, there is **no ground truth** that the backtest
  matches what live trading actually does.

## What you should do next

1. **Run the live-paper reconciliation gate (E2). This is the
   blocker, not the backtest number.**
   - The bot has been paper-trading. Pull ≥30 real CSP entries from
     `orders` and replay them through this harness with the matching
     sleeve config snapshot.
   - Per-trade match target: ≥80% same-strike same-DTE for entries.
     Equity drift target: <5% over the same window.
   - This is the only check that pins the bracket. If live paper
     fills are clustering near the ceiling, the strategy ships with
     the +30% expectation. If they're closer to the floor, +14% is
     the real story. Right now we're guessing which end of the
     bracket is right.

2. **Do not size up to full conviction yet, even if E2 passes.** A
   +30% / Sharpe 0.68 strategy is good but not generationally good;
   start at a fraction of intended size, watch for live drift vs
   backtest, scale up after a quarter of clean results.

3. **Re-think the calibration.** Possible directions:
   - Tighter delta target (current 0.30/0.40 is aggressive; 0.20-0.25
     would reduce assignment frequency at the cost of less premium).
   - Longer DTE band (current 7-10 days; 30-45 days lets more
     theta decay before an assignment crystallises).
   - Index-only universe (drop the high-IV small-caps where the
     edge is most spread-sensitive).
   - Run the sensitivity sweep across all three to see which
     calibration generates a positive risk-adjusted edge net of
     realistic spreads.

4. **Stress-test against the 2022 bear.** The current data window
   (2024-03 onward) starts after the 2022 grind. Alpaca options
   history begins Feb 2024 so we can't directly replay 2022, but a
   walk-forward synthesis with calibrated 2022 IV surfaces would
   answer the "what if real money was running through that tape"
   question.

5. **Buy the EODHD calendar add-on ($19.99/mo)** and re-run the
   earnings filter. yfinance miss rate is small but non-zero, and
   for live capital the difference between catching every earnings
   blackout and missing 1-2 per quarter is material.

## Files

In the repo root:
- `MORNING_REPORT.md` — this file (realism-audited)
- `BACKTEST_PLAN.md` — the original design doc

Per-run artefacts:

- `backtest_runs/realism_fix/` — **the realistic result**, this audit
  - `summary.md`, `analysis.md`, `equity.csv`, `trades.csv`,
    `ticks.csv`, `sleeve_attribution.csv`,
    `sleeve_config_snapshot.json`
- `backtest_runs/auto_reset/` — prior buggy-optimistic run (kept for
  reference; do **not** cite the +98% number as a result)
- `backtest_runs/full_run/` — earliest run (permanent kill_switch)

Source code: `src/kai_trader/backtest/`. Tests: `tests/backtest/`.
Helper scripts: `scripts/postprocess_backtest.py`,
`scripts/sanity_check.py`, `scripts/spy_benchmark.py`,
`scripts/run_backtest_sensitivity.py`.

## What I changed in this audit (code-level)

1. `src/kai_trader/backtest/data/chains.py`
   - Recalibrated `_SPREAD_FRAC_*` constants against real Alpaca
     historical option trade prints (2.5-10% → 10-30% half-spread).
   - Added intraday-range floor: `(bar.high - bar.low) / (4 * close)`
     wins when wider than the volume bucket.
   - Memoised JSON load for chains and contracts so the new MtM path
     does not thrash the 50MB+ files.

2. `src/kai_trader/backtest/runner.py`
   - Added `_historical_rv30(symbol, asof)` and
     `_make_rv30_provider(asof)` — asof-bounded RV30 reconstruction
     from cached daily bars.
   - Wired the provider into `build_intents_with_diagnostics` so the
     IV/RV 1.10 floor (production gate) now applies in the backtest.
   - Replaced intrinsic-only short option mark with a chain-mid-based
     mark, using the same fetcher path the strategy uses (so any
     leakage check there protects MtM too).

3. `src/kai_trader/backtest/reporting/summary.py`
   - Rewrote `compute_metrics`'s win-rate logic to pair each close
     with its prior open of the same contract, count assignments as
     losses on the option leg, and stop double-counting.

All 79 backtest tests still pass. ruff and mypy --strict are clean
on the changed files.

## Reliability principles (still enforced)

What the backtest still gets right (from `BACKTEST_PLAN.md`):

* Strategy code is **imported, not copied**. Production modules
  (`candidates`, `rolls`, `profit_take`, `assignment`,
  `covered_calls`, `regime`, `drawdown`) are called directly. Any
  signature drift would surface as a `TypeError` at run time.
* No future leakage. Every fetcher (`bars`, `chains`, `rates`,
  `earnings`) is asof-bounded. The CI-runnable `audit/leakage.py`
  fuzzed 1000 random pairs and found zero leaks.
* Survivorship-aware universe via `data/universe.py` (Alpaca daily
  bar presence as the listing proxy at each asof).
* Capital invariants asserted on every state mutation. Cash never
  goes negative outside the documented margin-debit on assignment.
* Real production sleeve config snapshot (migration 018) used as
  the run config; SHA recorded in run metadata.
