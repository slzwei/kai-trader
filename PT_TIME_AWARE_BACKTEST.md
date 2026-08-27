# Time-aware profit-taking: backtest report

Date: 2026-08-27. Author: research harness run via Claude Code.
Status: research only. No production behavior was changed.

## Question

Would Kai benefit from closing unusually fast winners early (35 to 40%
of premium captured within the first day) instead of always waiting for
the static 50% target, once redeployment of the freed collateral is
modeled honestly?

## Verdict up front

**Keep the static 50% rule.** The time-aware variants produced no
reliable improvement. One variant (B) looked better on the headline
numbers, but the gap is path luck, not rule quality: it sits inside the
measured noise floor of the simulator, it reverses under a kinder fill
model, and the per-event counterfactual shows the early exits
themselves roughly broke even. Details below.

## 1. The actual production rule (confirmed, not assumed)

`strategy/profit_take.py`: for each open short put, close when

    current_ask <= original_credit * (1 - sleeve.profit_take_pct)

where `original_credit` is the fill price of the most recent filled
`open_short_put` for that contract. The buy-to-close is submitted at
the ask, gated by `kill_switch` only. Live `sleeve_config` read on
2026-08-27: `profit_take_pct = 0.500` on both enabled sleeves
(index_core, 35%, MARA RIOT SOFI RIVN; stable_largecap, 55%, F T PFE
KMI BAC KO; opportunistic disabled at 0.300). So the baseline is
genuinely 50%, uniform across the live book. There is no time
component anywhere in the production rule, and
`POST_PROFIT_TAKE_COOLDOWN_MINUTES = 0`, so freed collateral is
immediately eligible for redeployment through the normal gate.

## 2. How the harness models profit-taking and redeployment

The backtest (src/kai_trader/backtest/) replays one tick per trading
day: expiries settle, then profit-takes, then rolls, then new CSP
entries, then covered calls. Because profit-takes run before entries
inside the same tick, collateral freed by a close is available to the
entry builder that same day, exactly like production. Entries pass
through the real `candidates` screen and `risk/gate.py` caps (per-name
notional, per-tick 25%, per-day 80%, contract ceilings, cooldowns,
committed-collateral subtraction), so redeployment only happens when a
real entry would have been allowed. Fills are pessimistic by default
(sell at bid, buy at ask), fees are OCC + ORF + SEC per contract, and
a capital invariant aborts the run if any CSP is ever unbacked.

## 3. Data-resolution limitations

* Daily bars only. The harness cannot see 12-hour windows. Variant C
  (35% within 12h) is therefore untestable and was not run: at daily
  resolution it is indistinguishable from variant B. The smallest
  defensible window is one trading day: entry at day T close, first
  evaluation at day T+1 close, roughly 24 hours.
* Variant D's 12h/24h ladder was adapted to a 1-day/2-day ladder.
* Historical bid/ask spreads are estimated from option volume buckets
  calibrated against real trade prints; greeks are Black-Scholes
  reconstructions. Both are shared by every variant, so comparisons
  are internally consistent.
* The drawdown breaker was modeled with the new S1 semantics (breach
  freezes new entries, closes keep working, entries re-enable once the
  7-day drawdown is back under 7%). The backtest cooldown after an
  entry is about 2 days, stricter than production's 15 minutes, which
  slightly handicaps same-name redeployment for every variant equally.

## 4. Experiment setup

Window 2024-03-01 to 2026-08-20 (620 trading days; caches were
extended to August on the free Alpaca tier). Capital $30,000,
cash-secured, pessimistic fills, production sleeve snapshot above.
Runs, changing ONLY the profit-take rule:

| Run | Rule |
|---|---|
| baseline | 50% target (production) |
| variantA | 50%, or >= 40% captured at age 1 trading day |
| variantB | 50%, or >= 35% captured at age 1 trading day |
| variantD | 50%, or >= 35% at age 1, or >= 40% at age 2 |
| static40 / static60 | flat 40% / 60% targets (attribution reference) |
| baseline_autoreset | 50% under the old auto-reset breaker (model reference) |

Plus sensitivity probes: baseline and variantB re-run with capital
$30,050 (chaos probe) and with quarter-spread fills (fill-model probe).

## 5. Headline comparison

| Metric | baseline | A | B | D | static40 | static60 |
|---|---:|---:|---:|---:|---:|---:|
| Total return % | 72.9 | 68.5 | 91.8 | 77.8 | 96.7 | 128.9 |
| Max drawdown % | 39.6 | 40.5 | 23.2 | 32.7 | 21.1 | 22.3 |
| Sharpe | 0.63 | 0.60 | 0.81 | 0.68 | 0.86 | 1.08 |
| Sortino | 0.67 | 0.64 | 0.86 | 0.71 | 0.89 | 1.14 |
| CSP episodes closed | 200 | 198 | 220 | 204 | 234 | 255 |
| Win rate % (option leg) | 98.5 | 98.5 | 99.1 | 99.0 | 99.1 | 98.4 |
| Avg hold (trading days) | 5.13 | 5.14 | 4.92 | 5.01 | 4.88 | 5.30 |
| Fast (early) exits | 0 | 5 | 13 | 16 | 0 | 0 |
| Assignments | 69 | 69 | 74 | 65 | 72 | 89 |
| Avg collateral utilisation % | 14.8 | 14.4 | 16.7 | 15.0 | 17.3 | 20.0 |
| Collateral efficiency %/yr | 100.5 | 99.3 | 94.5 | 97.0 | 94.4 | 100.3 |
| Transaction costs $ | 47 | 45 | 58 | 49 | 63 | 67 |
| Breaker engagements | 18 | 21 | 16 | 19 | 15 | 10 |

Full tables (regime split, phase split, tails, robustness) live in
`backtest_runs/pt_time_aware/analysis/comparison.md`.

## 6. Why the headline table cannot be taken at face value

Three independent falsification checks:

1. **Chaos probe.** Changing starting capital by $50 (0.17%) moved the
   baseline from +72.9% to +52.7% and its drawdown from 39.6% to
   42.5%. A 20-point swing from nothing: the run-level noise floor is
   the same size as every variant's apparent edge.
2. **Fill-model probe.** Under quarter-spread fills the ranking
   reverses: baseline +148.9% vs variantB +102.5%. A real edge does
   not flip sign when fills get kinder.
3. **Non-monotonicity.** A (stricter fast exit) does worse than
   baseline while B (looser) does much better; static40 and static60
   BOTH beat static50. Rule-quality effects are monotone-ish; path
   lottery is not. Rolling 126-day windows: B beats baseline in only
   12 of 24 windows, A in 9, D in 9. Coin flips.

The mechanism behind the dispersion is concrete: the whole ranking was
decided by which book each run carried into November-December 2025,
when MARA fell 51% and RIOT 36%. Baseline entered that crash holding
1,100 MARA and 200 RIOT shares from earlier assignments; variantB held
800 MARA and no RIOT; static60 held 300 MARA. Those inventory
differences trace back to entry-sequence divergence months earlier
(tiny cash differences flip discrete gate decisions), not to the
profit-take rules themselves. B had zero fast exits during the two
months in which its entire final edge was built.

## 7. What the rule actually did (per-event evidence)

Fast exits are rare under honest economics: 5 (A), 13 (B), 16 (D)
events in 2.5 years, out of about 200 episodes. Reason: with entry at
the bid and exit at the ask, capturing 35 to 40% net of the double
spread within one day requires the option's mid to collapse roughly 50
to 60% overnight. That only happens after violent IV crush or gap-up
moves, mostly on the miner names.

Counterfactual replay of every fast exit under the baseline rule
(hold until ask <= 50% of credit, else expiry, same caches, same fee
model):

| | A | B | D |
|---|---:|---:|---:|
| Events | 5 | 13 | 16 |
| Actual early-exit P&L $ | 165 | 440 | 548 |
| Hold-under-baseline P&L $ | 78 | 511 | 749 |
| Net given up by exiting early $ | -87 | +71 | +201 |
| Events where holding won | 80% | 92% | 94% |
| Median given up per event $ | 7 | 11 | 10 |
| Counterfactual assignments | 1 | 2 | 2 |

Read: holding almost always wins by a few dollars (the last 10 to 15%
of premium decays fast on a 7 to 10 DTE put). Occasionally an early
exit dodges an assignment; variantA's one dodge (and one of B's, RIOT
strike 21 exited 2025-10-24, which would have assigned 300 shares
four days before the miner crash) is worth a few hundred dollars on
the option leg and more on the avoided stock ride. But that benefit is
crash-timing luck, and it cuts the other way when the dodged
assignment precedes a rally (RIVN rose 45% over the same two months).
Note the counterfactual books assignment losses at expiry intrinsic
only and does not model the subsequent covered-call recovery the wheel
is designed for, so it is biased IN FAVOR of early exits, and they
still only break even.

## 8. Collateral efficiency and redeployment

* Realized CSP P&L per collateral-day, annualised: baseline 100.5%,
  A 99.3%, B 94.5%, D 97.0%. The time-aware variants were LESS
  collateral-efficient, not more.
* When fast exits fired, the freed collateral was absorbed quickly:
  94 to 100% redeployed within 5 trading days, median 1 day, many same
  day. So redeployment capacity exists.
* But it did not create value, because capital was rarely the binding
  constraint: average CSP collateral utilisation ran 14 to 20% with
  $3.4k to $4.9k average idle cash. The binding constraints are the
  screens, per-name caps, and the entry pipeline (and, episodically,
  cash locked in assigned stock, which fast CSP exits do not free).
  Freeing a strike of collateral one to three days early mostly funds
  a trade that would have happened anyway.

This directly addresses the hypothesis that earlier experiments failed
because Kai could not redeploy: redeployment worked fine. The freed
capital simply had nowhere better to go, and the abandoned tail of
premium was worth about as much as the replacement trades.

## 9. Tail risk

No variant showed a reliable tail improvement attributable to the
rule. Collateral exposure on the eve of SPY's 15 worst days was
similar (10.6 to 13.0% vs overall 14 to 17%). B and D's lower max
drawdowns in the main matrix come from the November 2025 inventory
lottery described above; A's drawdown was slightly WORSE than
baseline. Assignment rates were statistically indistinguishable
(31.9 to 34.8%). In the three bear-bucket months every variant lost
14.7 to 19.9%; the dispersion across variants inside each phase is as
large as the dispersion between phases, which is the path-noise
signature again.

## 10. Answers to the decision criteria

* Equal or higher portfolio return: not demonstrable above the noise
  floor, and sign-flips under the fill model.
* Similar or lower max drawdown: not attributable to the rule.
* Improved collateral efficiency: no, slightly worse in every variant.
* Sufficient redeployment opportunities: yes mechanically, but
  valueless because capital is not the binding constraint at $30k
  with the current 10-name universe.
* No material increase in tail risk: neutral.
* Robustness across periods: fails (9 to 12 wins out of 24 windows).

## 11. Recommendation

Retain the static 50% rule. Do not add the time-aware layer. The
evidence does not clear even a generous bar, and the added complexity
(per-position age tracking, two thresholds, more ledger states) would
buy variance, not edge. Two honest follow-ups if the idea stays
interesting:

1. The hypothesis is really about intraday dynamics this harness
   cannot see. The cheap, sound next step is a production shadow
   metric: when an open CSP crosses 35 or 40% captured within its
   first day, log the would-be exit and track the counterfactual
   forward. Zero risk, real fills, real spreads, decidable in a few
   months of live data.
2. The static-threshold sweep (40/50/60) also cannot be ranked from
   these runs for the same noise reasons. static60's headline win is
   the same November-2025 inventory lottery. Nothing here justifies
   moving the static threshold either.

## 12. Files added or modified for the experiment

Added (research only, no production imports):

* `src/kai_trader/backtest/experiments/__init__.py`
* `src/kai_trader/backtest/experiments/time_aware_pt.py` (rule
  dataclasses, variant definitions, evaluator that reuses the
  production profit-take evaluator for both legs)
* `scripts/run_pt_experiment.py` (matrix driver, frozen production
  sleeve snapshot of 2026-08-27)
* `scripts/analyze_pt_experiment.py` (episodes, collateral series,
  redeployment attribution, counterfactuals, phase splits, bootstrap,
  rolling windows, comparison renderer)
* `tests/backtest/test_time_aware_pt.py` (10 tests)

Modified (backtest harness only):

* `src/kai_trader/backtest/runner.py`: optional `pt_rule` hook
  (default None keeps the production path byte-identical, verified by
  an exact-parity re-run); rolls are now held whole when any entry
  flag blocks, matching production's entries-gated rolls and
  preventing an impossible half-roll under a freeze.
* `src/kai_trader/backtest/drawdown_sim.py`: new `s1_freeze` mode
  mirroring the Safety S1 breaker.
* `src/kai_trader/backtest/cli.py`: `--pt-variant`, `s1_freeze`
  choice, run_config.json provenance, and a fix so the sleeve snapshot
  written to the run dir is the config actually used.

Run artifacts (gitignored, regenerable): `backtest_runs/pt_time_aware/`
and `backtest_runs/pt_time_aware_sens/`.

## 13. Tests and gates

* `uv run pytest tests/backtest/`: 120 passed (including the 10 new).
* `uv run ruff check src/` clean; new scripts and tests clean (9
  pre-existing findings in old May-campaign scripts are untouched).
* `uv run mypy --strict src/` clean.
* Parity: the default backtest path after the changes reproduces the
  pre-change smoke run to the cent.
