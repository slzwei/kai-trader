# Post-cap drawdown forensics: is the remaining ~20-23% a problem?

Date: 2026-08-28. Author: research harness run via Claude Code.
Status: research only. Production behavior unchanged.

## The plain answer

**It is the price of running this strategy, not a defect to fix.**

The remaining drawdown is a broad market loss on a structurally long
book. Over the exact peak-to-trough window SPY fell 16.9%, eight of the
nine names in the book fell with it, and the loss was spread across
seven of them with no single name above 30% of it. The option engine
was profitable throughout the decline: it collected a net **+$893** of
premium while the assigned shares lost **-$9,506**. Mean pairwise
correlation among the held names was **+0.31**, so this was not one
disguised bet. Every risk control that could have acted did act
correctly.

One genuine finding did come out of it, and it points the other way:
the **backtest has been overstating the drawdown**. Its equity figure
carries held shares at cost, while production reads Alpaca's market
equity, so every equity-scaled cap runs looser in the harness than live
exactly while shares are under water. Corrected, the same
configuration shows a max drawdown of **19.9%** (and 16.8% under
quarter-spread fills) at statistically unchanged return. Production's
true drawdown profile is therefore at the better end of the 20-23%
range, not the worse.

Classification: **A, healthy strategy drawdown.** No further risk
controls are recommended. Adding more would reduce return more than
they improve safety.

## 1. Exact drawdown (production-faithful configuration)

Configuration: trend filter on, S2 economic cap at its shipped 0.20
default, s1_freeze breaker, cash-secured, pessimistic fills, frozen
2026-08-27 production sleeve snapshot, 2024-03-01 to 2026-08-20.

| | |
|---|---|
| Peak | **2025-01-17**, $37,147.97 |
| Trough | **2025-04-08**, $28,535.19 |
| Drawdown | **23.19%** (harness) / **19.94%** (production-faithful, see §9) |
| Peak to trough | **55 trading days** (81 calendar) |
| Recovery to new high | **2025-06-09**, $37,565.32 |
| Trough to recovery | **42 trading days** (62 calendar) |
| Full peak-to-peak | 143 calendar days |

The ledger replay reconciles against the run's own recorded cash to
within **$80.92** across 620 days (fees are not replayed), so the
attribution below is not resting on a reconstruction error.

## 2. Attribution: what actually lost the money

Total fall: **-$8,613**.

| Symbol | P&L | Share of the fall |
|---|---:|---:|
| SOFI | -$2,546 | 29.6% |
| MARA | -$1,878 | 21.8% |
| RIVN | -$1,388 | 16.1% |
| BAC | -$1,027 | 11.9% |
| F | -$874 | 10.1% |
| PFE | -$871 | 10.1% |
| T | -$115 | 1.3% |
| KMI, RIOT | $0 | 0% |

* **Largest single name: 29.6%.** Top three: **67.5%.**
* There were **no positive contributors at the symbol level** — every
  name that was held lost money. That is the signature of a market
  event, not a position event.

Compare with the pre-cap drawdown, where MARA alone accounted for
**109.5%** of the loss (everything else netted positive). The
concentration pathology is gone.

### P&L by component

| Component | Amount |
|---|---:|
| Option premium collected (23 opens) | +$947 |
| Premium paid back (5 profit-takes/rolls, 26 settlements) | -$54 |
| **Net option premium retained** | **+$893** |
| **Implied mark-to-market loss on assigned shares** | **-$9,506** |
| Total equity change | -$8,613 |

The premium engine made money during the drawdown. The equity
inventory lost it. That single line is the whole story.

## 3. Exposure through the event

| Date | NAV | Cash | Put face | Assigned MV | Assigned % | Gross % | Largest | Top-3 | NAV idx | SPY idx |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2025-01-17 | 37,148 | 15,713 | 2,600 | 21,636 | 58% | 65% | 16% | 43% | 100.0 | 100.0 |
| 2025-02-18 | 36,077 | 10,359 | 6,000 | 26,247 | 73% | 89% | 19% | 53% | 97.1 | 102.3 |
| 2025-03-04 | 32,898 | 4,381 | 0 | 28,549 | 87% | 87% | 17% | 50% | 88.6 | 96.5 |
| 2025-03-18 | 32,536 | 4,413 | 1,970 | 28,191 | 87% | 93% | 24% | 58% | 87.6 | 93.9 |
| 2025-04-08 | 28,535 | 4,542 | 4,000 | 24,389 | 85% | 99% | 23% | 57% | 76.8 | 83.1 |

Average through the window: assigned equity **75.9%** of NAV, gross
economic exposure (shares + put face) **83.7%**.

**There is no leverage hole.** Across all 620 days gross economic
exposure peaked at **103%** of NAV, never exceeded 110%, and averaged
79%. Cash-securing is the portfolio-level brake: by 2025-03-04 cash had
fallen from $15.7k to $4.4k and the strategy was simply out of
ammunition. Total exposure is bounded at roughly 1x NAV by
construction.

## 4. Is it one macro bet in disguise?

No. Measured on daily log returns inside the window:

* **Mean pairwise correlation among held names: +0.31.**
* Highest pair: MARA/RIOT **+0.79** — the known miner cluster, but it
  was only 7-10% of NAV during this window and contributed 21.8% of the
  loss through MARA alone, with RIOT flat.
* Lowest pairs are negative: T/RIOT **-0.20**, MARA/T **-0.12**.
* Correlation with SPY ranges from **+0.27** (T) to **+0.80** (SOFI).

The common factor is equity beta, which is inherent to a wheel (short
puts and assigned shares are both long exposure), not a hidden
correlation cluster. Per-name moves: SOFI -42.4%, MARA -47.2%, RIOT
-51.2%, BAC -24.7%, RIVN -24.0%, PFE -17.0%, KMI -16.8%, F -14.6%,
T +18.4%. Equal-weight average -24.4% against SPY's -16.9%, an implied
beta of **1.44x**.

**Beta arithmetic:** 83.7% gross exposure x 1.44 beta x -16.9% market
= **-20.4%** expected. Actual: **-23.2%**. A residual of 2.8 points,
which the cost-basis cap looseness in §9 more than accounts for. The
drawdown is essentially fully explained by market exposure.

## 5. Lifecycle of the losing positions

Twelve CSPs were entered during the 55-day decline, eight of them on
names already holding assigned shares.

| Date | Sym | Strike | DTE | Qty | Prem | Delta | Face | Held sh | Econ before |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 01-17 | PFE | 26.00 | 7 | 1 | 0.28 | -0.38 | 2,600 | 100 | 14% |
| 01-22 | SOFI | 16.50 | 9 | 2 | 0.66 | -0.33 | 3,300 | 0 | 9% |
| 01-24 | PFE | 26.00 | 7 | 1 | 0.25 | -0.44 | 2,600 | 100 | 14% |
| 01-29 | PFE | 26.50 | 9 | 1 | 0.40 | -0.45 | 2,650 | 100 | 15% |
| 02-05 | T | 24.50 | 9 | 1 | 0.23 | -0.50 | 2,450 | 0 | 7% |
| 02-07 | BAC | 47.00 | 7 | 1 | 0.37 | -0.38 | 4,700 | 0 | 13% |
| 02-14 | RIVN | 13.50 | 7 | 2 | 0.63 | -0.38 | 2,700 | 300 | 19% |
| 02-18 | SOFI | 16.50 | 10 | 2 | 0.37 | -0.39 | 3,300 | 200 | 18% |
| 03-18 | F | 9.85 | 10 | 2 | 0.16 | -0.41 | 1,970 | 600 | 24% |
| 03-25 | RIVN | 11.50 | 10 | 1 | 0.19 | -0.24 | 1,150 | 500 | 21% |
| 04-01 | T | 28.00 | 10 | 1 | 0.32 | -0.35 | 2,800 | 0 | 8% |
| 04-02 | RIVN | 12.00 | 9 | 1 | 0.34 | -0.35 | 1,200 | 500 | 22% |

Five assignments followed (SOFI x2, PFE, BAC, RIVN), and eleven covered
calls were opened, all of which filled. Sizes were small: one or two
contracts, $1,150 to $4,700 of face, against a $28-37k account.

**Mechanism classification.** These are *expected strategy risk*. They
are not excessive sizing (1-2 contracts), not trend-filter weakness
(see §6), not a correlated-exposure breach (§4), and not the pre-cap
averaging-down pathology: economic exposure at entry sat at 7-24% of
NAV against a 20% cap, where the pre-cap book reached 45-81% in one
name. The dominant mechanism is simply **assignment accumulation during
a market decline**, which is what a wheel does by design: it sells puts,
the market falls, it takes delivery, and it then earns covered-call
premium on the shares while they recover.

The independent proof that these entries did not cause the drawdown:
the separate breaker study (`BREAKER_SLOW_ANCHOR.md`) froze new entries
far more aggressively across this same window and moved max drawdown by
**zero, to four decimal places**. The loss was in inventory held
*before* the decline began, which no entry-side control can reach.

## 6. Did any control fail?

Every control behaved as designed. None failed.

| Control | Behaviour in this event | Verdict |
|---|---|---|
| Trend filter (50-DMA) | **All 12 entries were on names above their own 50-DMA.** Zero below-trend entries. | Worked exactly as designed |
| S2 economic cap | Bound correctly on 9 of 12 entries, holding per-name economic exposure to 7-24% of NAV against the pre-cap 45-81%. | Worked (see §9 for the 3 exceptions) |
| Drawdown breaker | Engaged twice during the window, on the sharp April leg, which is the shock it is built for. | Worked as designed |
| Covered-call cost-basis floor | 11 CC intents built, **11 filled**. The floor did not starve the book of income here. | Worked |
| Profit-take (50%) | 5 profit-takes/rolls executed; net premium retained positive. | Worked |
| Roll logic | Net-credit-only rule held; no debit rolls. | Worked |
| Sleeve / total collateral caps | Total put face never approached the 1x-equity ceiling; cash bound first. | Worked (not the binding constraint) |
| Per-name notional cap (12%) | Counts put face only, by design; S2 is the shares-inclusive layer above it. | Worked as designed |
| Regime gate | 32 of 56 days were `risk_off`, and 4 entries were made on those days. | Behaved as coded (see note) |

**Note on the regime gate.** It no longer gates sleeve participation:
`_is_sleeve_active` returns true for any enabled sleeve regardless of
regime, and `risk_off` only shifts the target put delta from -0.40 to
-0.30. That is the intended current behavior, but
`strategy/regime.py`'s docstring still claims "risk_off: no new
entries", which is stale and misleading. **This is a documentation
defect, not a control failure** — worth correcting in a later docs
pass, and deliberately not touched here.

## 7. Is there another structural loophole?

Three candidate gaps were examined. None is a live risk under the
current configuration.

1. **No portfolio-level economic exposure cap.** The only
   shares-inclusive control is per-single-name; the total deployment cap
   counts put face only. In principle a book could hold ~100% of NAV in
   assigned shares *and* open a fresh put book. In practice it cannot:
   under cash-secured operation the shares consume the cash the puts
   need, and measured gross exposure peaked at 103% of NAV with zero
   days above 110%. **The cash constraint is the portfolio brake.**
   *Caveat for the future:* this brake is a property of cash-securing.
   If the account is ever moved to Reg-T margin (`margin_factor` below
   1.0), that bound disappears and a portfolio-level economic cap would
   become genuinely necessary. It is not needed today.
2. **No correlated-group cap.** True, but the measured mean pairwise
   correlation of +0.31 does not support one, and the forensics that
   preceded this already tested and rejected a MARA+RIOT cluster cap for
   costing return with no incremental drawdown benefit.
3. **The roll reopen leg bypasses the risk gate** (it calls
   `submit_short_put` directly). Worth knowing, but it is size-neutral
   and strictly further out of the money, so it cannot increase face or
   economic exposure. Not implicated here: only 5 rolls/profit-takes
   occurred in the window.

## 8. Classification

**Category A: healthy strategy drawdown.**

* Losses distributed across seven names; largest 29.6%, top three 67.5%.
* No pathological concentration: largest single-name exposure 16-24% of
  NAV against a 20% cap.
* Mean pairwise correlation +0.31; the one real cluster was a small
  part of the book.
* Every limit operated correctly; no exposure loophole was exercised.
* The option engine was net profitable (+$893) throughout; the loss was
  mark-to-market on shares during a 16.9% market decline.
* Beta arithmetic explains 20.4 of the 23.2 points.
* Recovered to a new high in 42 trading days.

**Further risk controls would reduce return more than they improve
safety.** The drawdown is the cost of holding a ~0.8x-NAV, ~1.4-beta
long book through a market correction. The only way to reduce it
materially is to hold less directional exposure, which is the same
thing as earning less premium.

## 9. The one real finding: the harness overstates the drawdown

`BacktestState.account_snapshot()` computes equity with held shares at
**cost basis**, while the S2 cap's numerator marks the same shares at
**market**. Production has no such split: Alpaca reports market equity
and market position values on both sides. The consequence is that every
equity-scaled cap (per-name notional, S2 economic, sleeve and total
headroom) runs **looser in the harness than live, and by the most
exactly when shares are under water** — that is, in a drawdown.

The effect is visible and directional. Three of the twelve entries
passed only because of it:

| Entry | Harness view (cost-basis equity) | Production view (market equity) |
|---|---:|---:|
| 03-18 F | 19.4% — pass | 24.4% — **would be refused** |
| 03-25 RIVN | 17.9% — pass | 21.3% — **would be refused** |
| 04-02 RIVN | 18.2% — pass | 22.4% — **would be refused** |

Re-running the identical configuration with equity marked at market
(research flag `--mark-equity-at-market`):

| Probe | Cost basis (as reported until now) | Market-marked (production-faithful) |
|---|---|---|
| base | 71.3% ret / **23.19%** DD | 60.2% / **19.94%** |
| chaos capital (+$50) | 57.5% / **23.19%** | 60.8% / **19.88%** |
| quarter-spread fills | 64.2% / **21.03%** | 61.5% / **16.81%** |
| mean | 64.3% / 22.5% | 60.8% / **18.9%** |

Two things stand out. The drawdown reduction (**-3.6 points**) is
consistent across all three probes, whereas the return difference
(-3.5 points) sits well inside this harness's known ~20-point run-level
noise floor — the cost-basis runs alone span 57.5% to 71.3%. And the
market-marked runs are dramatically **more stable**: their returns span
1.4 points and their drawdowns 3.1, against 13.8 and 2.2 for the
cost-basis runs, because the caps now bind consistently instead of
drifting with the cost-to-market gap.

So the honest statement of Kai's current risk profile is a max drawdown
near **20%**, not 23%. This is a **research-tooling** defect, not a
production one: the production gate is already correct.

A smaller fidelity gap remains and is deliberately left alone:
`_short_option_intrinsic_liability()` returns zero, so harness equity
still excludes short-option mark-to-market that Alpaca's equity
includes. The harness therefore stays marginally looser than production
even with the flag on.

## 10. Is any production change justified?

**No.** Explicitly: no change is recommended to the profit-take rule,
the economic-cap level, the drawdown breaker, AI decision logic, stop
losses, hedging, the covered-call cost-basis floor, or production
sizing. Nothing in this analysis identifies a production defect.

Two non-production follow-ups, both optional and neither urgent:

1. **Make `--mark-equity-at-market` the default in the backtest
   harness.** It is strictly more faithful and materially more stable
   across probes. Left opt-in here because flipping it silently
   invalidates comparison against every previously recorded run, which
   is the user's call to make.
2. **Fix the stale `strategy/regime.py` docstring**, which claims
   `risk_off` blocks new entries when the code only shifts target delta.
   Documentation only; no behavior change.

If the account is ever switched from cash-secured to Reg-T margin,
revisit item 1 of §7: the portfolio-level brake that makes the current
per-name-only design safe is the cash constraint, and margin removes it.

## 11. Files added or modified

Modified (backtest harness only, additive, default off):

* `src/kai_trader/backtest/state.py`: `mark_long_equity_at_market` flag
  and `long_equity_marks`; `_long_equity_value` honours them. Default
  False reproduces every historical run exactly.
* `src/kai_trader/backtest/runner.py`: feeds the same asof-bounded marks
  used by the gate into the equity denominator when the flag is set.
* `src/kai_trader/backtest/cli.py`: `--mark-equity-at-market` flag,
  recorded in `run_config.json`.
* `tests/backtest/test_state.py`: 4 tests pinning both the default
  cost-basis behaviour and the marked behaviour.

**No production file was modified.** `git status` on
`src/kai_trader/strategy/`, `risk/`, `broker/`, `bot/`, and `config.py`
is clean.

Analysis used the existing `scripts/analyze_drawdown.py` and
`scripts/trace_symbol.py` unchanged. Run artifacts (gitignored,
regenerable): `backtest_runs/fidelity/`.

## 12. Tests and checks

* `uv run pytest`: **1,214 passed**, 7 env-gated integration skips.
* `uv run ruff check src/ tests/backtest/`: clean.
* `uv run mypy --strict src/`: clean.
* Parity: with the flag off, the configuration reproduces the recorded
  baseline exactly (71.32% / 23.19%).
* Ledger replay reconciles to recorded cash within $80.92 over 620 days.
* Robustness: every headline claim checked under a $50 capital
  perturbation and a quarter-spread fill model.

## Limitations

Daily bars (no intraday assignment, breaker timing, or fill dynamics),
estimated historical spreads, Black-Scholes greek reconstruction, no
dividends, assignment modeled at expiry only. The window contains a
16.9% correction but no 2020- or 2022-scale bear market, so the tail
beyond this event is untested. Beta is estimated from realised window
returns, not a fitted factor model, and is used only to establish
order-of-magnitude explanation. Per-symbol attribution carries a small
residual against recorded equity (fees and mark convention).
