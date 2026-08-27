# Drawdown forensics: why the baseline hit 39.6% and what fixes it

Date: 2026-08-27. Author: research harness run via Claude Code.
Status: research only at time of writing. ADDENDUM (later the same
day): recommendation 1 shipped as Safety S2, the assignment-aware
per-name economic cap in `risk/gate.py` (PER_NAME_ECONOMIC_CAP_PCT,
default 0.20). The production-faithful validation used the WITH-TREND
baseline (`trend`: 41.0% ret / 28.7% DD / peak name 59.8%) rather
than the trendless one quoted below, probed under chaos capital and
quarter-spread fills, and then re-validated the gate-native
implementation itself (`gate_native_*` runs in
`backtest_runs/dd_controls/`): DD 23.2 / 23.2 / 21.0 across probes,
peak name 25.6-32.9% (residual above 20% is post-entry appreciation,
never new risk), return never below baseline, and
`--econ-cap-pct 0` reproduces `trend` to the cent. One harness
caveat found during that validation: `state.account_snapshot()`
carries long equity at cost, so the backtest's cap dollars run
slightly loose when shares are under water; live production sizes
off Alpaca market equity and is tighter in exactly that regime.
Recommendation 3 (breaker numbness in slow grinds) remains open as a
separate research item. The body below is preserved as written.

## Question

The 2024-03-01 to 2026-08-20 baseline (the PT-experiment baseline:
$30,000, cash-secured, pessimistic fills, frozen 2026-08-27 production
sleeve snapshot, s1_freeze breaker) returned +72.9% with a 39.6% max
drawdown. What exactly caused the drawdown, and what is the smallest
set of risk controls that would have removed most of it without
destroying returns?

## Verdict up front

The drawdown was one position. MARA, held as assigned shares, was
45.2% of NAV at the equity peak and accounted for **109.5% of the
peak-to-trough loss** (every other position netted out slightly
positive). The mechanism is structural, not bad luck: **assigned
shares are invisible to the per-name risk cap.** `risk/gate.py`
budgets only short-put collateral, so the moment a put assigns, that
exposure stops consuming the 12% per-name cap and the strategy is free
to sell more puts on the same falling name. It did, 22 times, and
averaged down 1,100 shares into 2,700.

The fix that works is the direct one: **count assigned-share market
value against the same per-name cap when admitting new CSPs.** At a
20% economic cap the backtest keeps the baseline's return (+80.7% vs
+72.9%, inside noise) while cutting max drawdown from 39.6% to 22.8%.
At 12% the drawdown falls to 13.3% for a CAGR cost of about 5 points.
The drawdown reduction is pinned across every robustness probe
(capital chaos, fill model); the baseline's is not. Moving from ~40%
drawdown into the 15-25% target band without materially hurting CAGR
is realistic, because the drawdown was never the cost of the premium
engine. It was the cost of an accounting hole.

## 1. Exact reconstruction of the maximum drawdown

From `backtest_runs/pt_time_aware/baseline/equity.csv`:

| | date | equity |
|---|---|---:|
| Peak | 2025-10-15 | $55,645 |
| Trough | 2026-02-05 | $33,611 |
| Recovery (first close >= peak) | 2026-05-29 | $56,452 |

Fall: -$22,034 (-39.60%) over 77 trading days (113 calendar days).
Recovery took another 78 trading days; peak-to-peak was 226 calendar
days. The trough is NOT in December 2025: the November crash did most
of the damage, but the low printed 2026-02-05, the day the book
carried 1,800 MARA shares into MARA's $6.73 close.

The book through the drawdown (replayed from `trades.csv`, priced from
the bar cache; the replay ties to the run's recorded daily cash within
$112, which is the unreplayed fee drag):

| date | NAV | MARA shares | MARA avg cost | MARA close | MARA % NAV | assigned % NAV |
|---|---:|---:|---:|---:|---:|---:|
| 2025-10-15 | $55,645 | 1,100 | 18.68 | 22.84 | 45.2% | 87.4% |
| 2025-11-07 | $49,919 | 1,400 | 18.64 | ~15.9 | 44.5% | 76% |
| 2025-11-21 | $39,447 | 1,800 | 17.61 | 10.07 | 46.0% | 97% |
| 2025-12-19 | $40,696 | 1,800 | 17.61 | ~10.2 | 45.0% | 97% |
| 2026-01-30 | $40,853 | 1,800 | 17.61 | 9.50 | 57.3% | 80% |
| 2026-02-05 | $33,611 | 1,800 | 17.61 | 6.73 | 54.8% | 80% |

At the peak the "premium capture wheel" was in fact 87.4% assigned
stock and 8.8% put collateral, with 45% of NAV in a single bitcoin
miner. MARA fell 70.5% peak to trough; the book rode all of it and
bought more on the way down.

## 2. Top positions responsible

Per-symbol P&L, peak to trough (MtM + realized + assignment flows):

| symbol | contribution | share of the fall |
|---|---:|---:|
| MARA | -$24,132 | 109.5% |
| SOFI | -$952 | 4.3% |
| BAC, F, KMI | -$27 | 0.1% |
| RIVN, T, RIOT, PFE | +$2,058 | -9.3% (offsets) |

This was not a MARA+RIOT joint event on the book: RIOT's position was
small during the crash and contributed +$734. Nominal diversification
(9 names traded) concealed a single-name event.

## 3. How the inventory was created (MARA trace)

34 CSP entries on MARA over the run. **22 of them were opened while
already holding MARA shares**, and 18 were opened with the close below
the 50-DMA. Key sequence:

* Through 2025 the wheel assigned into MARA repeatedly during
  rallies and chop (14 assignments over the run; the position that
  entered the crash traces to lots from 2024-12 onward, average cost
  $18.68 by October 2025).
* 2025-09-19: holding 800 shares ($14.6k), sold 3 more P18 (economic
  exposure $20.0k against a $5.96k 12% cap).
* 2025-09-30: holding 1,100 shares ($20.1k), sold 3 more P18
  ($25.5k economic vs $6.0k cap).
* 2025-11-07: assigned +300 at $18.50 as the crash began (1,400).
* 2025-11-11: close below the 50-DMA, sold 4x P14. Assigned +400 at
  $14 on 2025-11-21 (1,800).
* 2026-01-30: equity was 26.6% below the run peak, but the breaker's
  7-day lookback saw only 4.76%, so entries were enabled. Sold 7x P9
  ($6.3k face on top of $17.1k of shares, 57% of NAV economic).
  Assigned +700 at $9 on 2026-02-06, the day after the trough.
* 2026-02-17: sold 9x P7. 2026-03: two more P8.5 entries, both
  assigned. Final position 2,700 shares at $14.70 average, still held
  at run end (-$9,622 unrealized), first lot never exited in 1.7
  years.

Note the contract escalation (3 -> 4 -> 7 -> 9): the cap is notional,
so the same dollar headroom admits more contracts as the strike falls.
The falling knife gets caught with progressively more hands.

Full-run MARA economics: +$10,814 realized (options plus called-away
stock) against -$9,622 unrealized at end. **Net +$1,192 for carrying
an average 43% of NAV in one name for 2.5 years.**

## 4. Assignment vs concentration: which dominated

Both, causally chained: assignment is the vehicle, concentration is
the damage, and the cap blindness is the enabler.

* 68 assignments (MARA 14, F 11, RIOT 11, SOFI 8, RIVN 8, T 6, BAC 5,
  PFE 3, KMI 2), average $3,871, largest $6,600.
* Assigned-share market value averaged **77.3% of NAV** across the
  whole run and peaked at **101.4%** (an assignment wave exceeded
  cash; the sim models the margin debit). Up to 8 names assigned
  simultaneously.
* 34 lots were called away (median 49 days held, max 448). The rest
  stuck below cost basis; MARA never fully exited.
* The covered-call leg is NOT the failure. 91% of MARA share-days
  were below cost basis, yet CCs were still written on most of them
  at strikes above basis (29 of 33 MARA CC opens), exactly as the
  cost-basis floor intends. The premiums were simply small relative
  to a 70% share decline. The floor also correctly prevented
  locking in the loss before the 2026 recovery.

## 5. Concentration through the run

* Largest single-name economic exposure: **average 43.2% of NAV, max
  81.5%** (2026-06-22, MARA again, after the recovery).
* Top-3 exposure: average 74.0%, max 97.0%. Max top-5: 103.5%.
* MARA+RIOT cluster: average 41.0%, max 85.4%. Their daily-return
  correlation over the window is **0.79** (next-closest pair 0.52;
  MARA-T is -0.06), so the two miners are one trade. In this
  particular path the book rarely held both heavily at once, which is
  why the crash read as single-name; the exposure series shows the
  cluster risk was carried all the same.
* The `index_core` sleeve (35% target) whitelist is MARA, RIOT, SOFI,
  RIVN: four high-beta names of which two are the same bet. The name
  says index; the contents say leveraged crypto-beta.

## 6. Did the existing limits behave as intended?

Yes, and that is the finding. No limit malfunctioned:

* The 12% per-name cap correctly capped **put collateral**. Every one
  of the 22 while-holding MARA entries was within the cap as written
  (typically $5-6.5k of put face) while total economic exposure stood
  at 3-6x the cap ($20-46k). The cap measured the wrong thing.
* The S1 freeze breaker fired 18 times and froze entries on the sharp
  legs. But its 7-day high-water lookback goes numb in a long grind:
  inside the 77-day drawdown it was frozen on only 3 ticks, and on
  2026-01-30 it read 4.76% while the book was 26.6% under water.
  Production uses the same 7-day lookback.
* The cost-basis floor, net-credit roll rule, cooldowns, per-tick and
  per-day caps all operated as specified.

## 7. Structural loopholes found

1. **Assigned shares exit the risk budget** (production and backtest).
   `risk/gate.py::_committed_collateral` sums short puts only;
   `worker.py` passes only `shorts_for_caps` into the builder; the
   backtest runner passes only `existing_short_puts`. Long equity is
   priced into buying power (cash is gone) but not into any per-name,
   sleeve, or portfolio risk limit. After assignment a position can
   only grow via new puts, and nothing counts it.
2. **The harness omits production's 50-DMA trend filter** (backtest
   only). `runner._run_csp_entries` passed `trend_status=None`, which
   fail-opens the gate. 18 of the 34 MARA entries were below the
   50-DMA; live production (Variant A+ P1) would have refused most of
   the deep averaging-down entries. The baseline therefore overstates
   what production would have done after the crash began. It does NOT
   explain the pre-crash accumulation: the September entries that
   built the 45% position were above the 50-DMA.
3. **Breaker numbness on slow grinds** (production and backtest): the
   7-day lookback re-arms entries once the bottom flattens, mid-
   drawdown. Observation only; no change proposed or made here.
4. No accounting bug found. The independent trade-log replay
   reproduces the recorded daily cash within $112 (unreplayed fees)
   and the recorded 39.60% drawdown exactly. Dividends are not
   simulated (understates the stable sleeve's carry slightly).

## 8. Candidate controls tested

Chosen from the observed mechanism, not a parameter sweep. All are
entry-side only, applied to the unchanged production screen+gate
output, able only to shrink or drop intents (mirroring the `ai_filter`
containment):

* `econ12` / `econ20`: per-name economic cap. New CSP admitted only
  while shares MV + open put face + this tick's accepted face stays
  within 12% / 20% of NAV (NAV at market, not cost).
* `assigned50`: portfolio brake, no new CSPs while assigned shares
  exceed 50% of NAV.
* `trend`: production 50-DMA filter restored (harness parity, not a
  new control).
* `trend_econ12`, `trend_econ20`, and cluster variants
  (`econ20_cluster25`, `trend_econ20_cluster25`): MARA+RIOT share one
  25% bucket.
* A parity run (`baseline_parity`) confirmed the new hook reproduces
  the baseline to the cent when disabled.

## 9. Comparison against baseline

Same window, capital, fills, sleeves; only the control changes.
(Sharpe/Sortino here are recomputed uniformly across runs by the
analysis script; the run summaries' own Sharpe uses a slightly
different convention. Comparisons are internally consistent.)

| run | TotRet% | CAGR% | MaxDD% | DD days | Rec days | Sharpe | Sortino | Calmar | Asgn | Prem$ | AvgUtil% | AvgAsgn% | PeakName% | PeakClus% | CAGR cost per DD pt |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 72.9 | 24.8 | 39.6 | 77 | 78 | 0.75 | 1.15 | 0.63 | 68 | 33,393 | 14.8 | 77.3 | 81.5 | 85.4 | - |
| trend | 41.0 | 14.9 | 28.7 | 77 | 77 | 0.61 | 0.92 | 0.52 | 56 | 22,222 | 12.2 | 73.5 | 59.8 | 62.3 | 0.90 |
| **econ12** | 55.1 | 19.4 | **13.3** | 47 | 17 | 1.01 | 1.53 | **1.46** | 71 | 20,869 | 12.2 | 48.0 | 16.0 | 25.8 | 0.20 |
| **econ20** | **80.7** | **27.1** | 22.8 | 54 | 24 | 0.97 | 1.46 | 1.19 | 89 | 31,311 | 17.2 | 70.3 | 26.9 | 41.5 | **-0.13** |
| assigned50 | 70.1 | 24.0 | 21.1 | 37 | 30 | 0.99 | 1.52 | 1.14 | 52 | 24,719 | 11.7 | 54.7 | 53.4 | 63.2 | 0.04 |
| econ20_cluster25 | 69.9 | 23.7 | 22.6 | 54 | 33 | 0.94 | 1.41 | 1.05 | 82 | 29,000 | 15.9 | 68.4 | 26.8 | 30.9 | 0.06 |
| trend_econ12 | 39.9 | 14.6 | 10.8 | 54 | 27 | 0.99 | 1.48 | 1.35 | 45 | 14,585 | 8.7 | 41.0 | 17.5 | 28.9 | 0.36 |
| trend_econ20 | 49.5 | 17.7 | 21.7 | 54 | 43 | 0.79 | 1.19 | 0.82 | 65 | 21,199 | 11.9 | 68.8 | 24.0 | 37.5 | 0.40 |
| trend_econ20_cluster25 | 44.2 | 16.0 | 22.1 | 54 | 44 | 0.76 | 1.13 | 0.72 | 70 | 19,352 | 12.0 | 66.6 | 23.3 | 32.7 | 0.51 |

Idle capital is unchanged everywhere (0.3% of days with zero
exposure): the caps redirect premium flow to other names rather than
parking cash. econ20 actually raised assignment count (89 vs 68,
smaller clips across more names) and kept premium capture within 6%
of baseline.

Rolling 126-day return windows vs baseline are coin flips for every
variant (econ20 5/8, econ12 4/8, trend 1/8), consistent with the
known return noise floor. The drawdown column is the signal.

## 10. Robustness

The PT experiment established the noise floor: +$50 of starting
capital moved baseline return 72.9% -> 52.7% and its DD 39.6% -> 42.5%;
quarter-spread fills moved baseline to +148.9% (DD 25.3%). Probes on
the candidates:

| run | main | chaos (+$50) | quarter-spread |
|---|---|---|---|
| baseline return / DD | 72.9 / 39.6 | 52.7 / 42.5 | 148.9 / 25.3 |
| econ20 return / DD | 80.7 / 22.8 | 72.7 / 23.5 | 84.8 / 23.2 |
| econ12 return / DD | 55.1 / 13.3 | 55.5 / 13.6 | 62.7 / 12.9 |
| assigned50 return / DD | 70.1 / 21.1 | 70.0 / 21.1 | 32.4 / 23.4 |

Read: the capped drawdowns are **pinned** (econ12 within 0.7 points,
econ20 within 0.7 points across all probes) while the baseline's
swings by 17 points. econ12's return barely moves at all, which is
itself evidence that the baseline's return dispersion IS the
concentration lottery. Return levels remain noise-dominated
everywhere, so the honest claim is: the DD reduction is structural
and robust; the return cost of econ20 is indistinguishable from zero;
econ12's ~5-point CAGR cost is real but stable. assigned50 fails
falsification: its return collapses to +32.4% under a KINDER fill
model, the signature of a fragile binary brake.

## 11. Best risk/return trade-off

`econ20` dominates the baseline on this path (higher return, 17
points less drawdown, better Sharpe/Sortino/Calmar) and its drawdown
holds ~23% under every probe. `econ12` is the conservative option:
best Calmar (1.46), 13% drawdown, at a real but modest CAGR cost. In
the user's own terms: econ12 gives up 0.20 CAGR points per drawdown
point removed; econ20 gives up nothing measurable.

The 15-25% target band is achievable without structural redesign.

## 12. Recommended

1. **Assignment-aware per-name economic cap in the production risk
   gate**, counting assigned-share market value into the existing
   per-symbol headroom. Start at 20% of equity (least disruptive,
   dominated the baseline in-test; tightening later is a config
   change). This is a change to `risk/gate.py` inputs (the worker
   already fetches the long book every tick) and must go through its
   own spec, tests, and golden-parity update before touching
   production.
2. **Wire the trend provider into every future backtest** (done here
   as the `trend` control): the harness must not fail-open a gate
   production enforces. Any future baseline should be quoted
   with-trend.
3. Worth a separate look (observation, not implemented): the
   breaker's 7-day lookback goes numb in long grinds; an additional
   slower anchor (e.g. 30-day or trip-time HWM) would have kept the
   freeze active on 2026-01-30.

## 13. Rejected

* **Cluster cap (MARA+RIOT)**: no incremental drawdown benefit once
  the per-name cap exists (22.6% vs 22.8%) at a return cost. The 0.79
  correlation is real; revisit only if both miners run hot
  simultaneously under the cap, which this path never showed.
* **assigned50 portfolio brake**: fill-model fragile (+70% -> +32%),
  blunt (blocks good names because of unrelated inventory), and
  leaves the single-name hole open (peak name still 53.4%).
* **Trend filter as the drawdown fix**: costs 0.90 CAGR points per DD
  point in this harness, the worst ratio tested. It stays in
  production for its live rationale (it is already deployed and this
  research does not contradict its entry-quality purpose), but the
  drawdown case rests on the economic cap.
* **Dynamic under-drawdown sizing and regime-sensitive caps**: not
  tested. The failure mechanism was concentration, not sizing during
  drawdowns; adding a second state-dependent sizing layer is not
  justified by this evidence.
* **Stop-loss liquidation**: excluded by design constraints (and by
  the wheel's accept-assignment philosophy).

## 14. What kind of problem this was

* **Genuinely structural**: yes. The assignment-blind cap is in
  production code today; any name that falls far enough will
  reproduce this.
* **Bad luck**: the specific -51% November 2025 miner crash was luck;
  carrying 45% of NAV in one name into it was not. Luck chose the
  date, structure chose the size.
* **Simulator path dependence**: return LEVELS are path-noisy (20-
  point swings from $50); this drawdown's mechanism and the caps' DD
  reduction survive every probe.
* **Weak diversification**: yes, secondary. index_core is four
  high-beta names, two of them one trade.
* **Excessive assignment accumulation**: yes, as the vehicle enabled
  by the cap hole.
* **Bug/accounting issue**: one harness divergence (missing trend
  filter) inflates the baseline's averaging-down; no accounting bug;
  replay ties to the recorded artifacts.

## 15. Files added or modified for research

Added (research only, no production imports):

* `src/kai_trader/backtest/experiments/risk_controls.py` (controls
  registry, asof-bounded 50-DMA provider reusing the production pure
  function, entry filter that can only shrink or drop gate output)
* `scripts/analyze_drawdown.py` (trade-log replay, drawdown
  reconstruction, concentration and assignment forensics; validates
  against recorded cash)
* `scripts/trace_symbol.py` (per-symbol inventory life trace)
* `scripts/run_dd_experiment.py` (matrix + robustness probes driver)
* `scripts/analyze_dd_experiment.py` (comparison table, rolling
  windows, trade-off stat)
* `tests/backtest/test_risk_controls.py` (10 tests)

Modified (backtest harness only, additive hooks, default off):

* `src/kai_trader/backtest/runner.py`: optional `entry_controls`
  threaded to `_run_csp_entries`; wires the builder's existing
  `trend_status` hook when enabled.
* `src/kai_trader/backtest/cli.py`: `--entry-controls` flag +
  run_config provenance.

Run artifacts (gitignored, regenerable): `backtest_runs/dd_controls/`
plus forensics JSON under `backtest_runs/*/analysis/`.

## 16. Tests and gates

* `uv run pytest`: 1,177 passed, 7 env-gated integration skips
  (includes the 10 new control tests; coverage gate remains the known
  pre-existing shortfall).
* `uv run ruff check src/` clean; new scripts clean.
* `uv run mypy --strict src/` clean.
* Parity: `--entry-controls none` reproduces the existing baseline
  artifacts to the cent ($51,864.35 / 72.88% / 39.60%).

## Limitations

Daily bars only (no intraday assignment or breaker timing), estimated
historical spreads, Black-Scholes greek reconstruction, no dividends,
assignment modeled at expiry only, and the gate sizes off cost-basis
equity exactly as production does. The forensic per-symbol attribution
carries roughly a 5% residual against the recorded equity change
(fees and mark convention). None of these affect the direction of any
conclusion above.
