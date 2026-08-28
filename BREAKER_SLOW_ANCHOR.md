# Drawdown breaker: does a slow anchor help?

Date: 2026-08-28. Author: research harness run via Claude Code.
Status: research only. No production behavior was changed.

Closes recommendation 3 of `DRAWDOWN_FORENSICS.md`.

## Question

The production breaker (`strategy/drawdown.py`) freezes new entries
when equity is 7% below the highest equity of the trailing 7 calendar
days. That window goes numb in a slow grind: the 7-day high drifts
down with the account, so an arbitrarily deep decline can accumulate
without the breaker ever seeing 7%. Would adding a second, slower
anchor (all-time high-water mark, or a 30-day window) improve
outcomes?

## Verdict up front

**No. Keep the production breaker exactly as it is.**

The numbness is real and measurable. Fixing it changes nothing,
because an entry freeze is the wrong instrument for this drawdown:
the losses come from inventory already on the books, which a freeze
cannot touch. Under the current production configuration the slow
anchors nearly tripled freeze duration and moved max drawdown by
**zero** (23.185% vs 23.185%, identical to four decimal places, with
339 of 620 days on a genuinely different equity path). Two of the
three rules made drawdown marginally worse. In the pre-S2 world where
drawdowns were deeper, a slow anchor did buy 2-3 drawdown points, but
at 1.5-1.6 CAGR points each: nearly twice the cost of the worst
control previously tested, and it is redundant with a fix that already
shipped.

## 1. The numbness is real

Measured on the production-faithful baseline (620 trading days,
2024-03-01 to 2026-08-20, trend filter on, S2 economic cap at its
shipped 0.20 default):

| | |
|---|---:|
| Days the 7-day breaker reported a breach | 13 |
| Days true drawdown (from the all-time peak) was >= 10% | 88 |
| ... of those, days the breaker was silent | 81 |
| Days true drawdown was >= 15% | 16 |
| ... of those, days the breaker was silent | 11 |

Episodes where the true drawdown exceeded 15%:

| Window | Days | Worst | Breaker active |
|---|---:|---:|---:|
| 2025-03-13 | 1 | 15.3% | 0 days (0%) |
| 2025-04-03 to 2025-04-23 | 13 | 23.2% | 4 days (31%) |
| 2026-03-27 to 2026-03-31 | 2 | 16.5% | 1 day (50%) |

So the observation behind recommendation 3 is correct: the account
spent 81 days more than 10% below its peak with the breaker reporting
nothing.

## 2. Fixing it changes nothing

Four rules, identical in every other respect (production-faithful
base: trend filter, S2 cap at 0.20, s1_freeze semantics, pessimistic
fills, frozen 2026-08-27 sleeve snapshot):

* `baseline` production 7-day/7% rule alone
* `peak15` union with >= 15% below the all-time high-water mark
* `peak12` union with >= 12% below the all-time high-water mark
* `win30_10` union with >= 10% below the trailing 30-day high

| Metric | baseline | peak12 | peak15 | win30_10 |
|---|---:|---:|---:|---:|
| Total return % | 71.3 | 69.5 | 71.3 | 69.3 |
| CAGR % | 24.5 | 23.9 | 24.5 | 23.9 |
| **Max drawdown %** | **23.185** | **23.238** | **23.185** | **23.237** |
| Calmar | 1.06 | 1.03 | 1.05 | 1.03 |
| Days DD >= 15% | 16 | 15 | 16 | 19 |
| Longest underwater (td) | 116 | 117 | 116 | 116 |
| Days entries frozen | 13 | 41 | 24 | 29 |
| Longest freeze (td) | 4 | 14 | 13 | 7 |
| CSP entries blocked | 2 | 13 | 8 | 5 |
| Covered calls blocked | 2 | 10 | 5 | 5 |
| Assignments | 68 | 65 | 68 | 66 |

The rules are genuinely active, not no-ops: `peak15` differs from
baseline on 339 of 620 days, `peak12` on 359, `win30_10` on 511. They
simply do not move the number they exist to move. `peak12` and
`win30_10` end up slightly worse.

Both falsification probes agree. Max drawdown is identical across all
four rules within each probe family (23.2% under a $50 capital
perturbation, 21.0% under quarter-spread fills). Rolling 126-day
windows against the same-family baseline:

| Rule | base | chaos capital | quarter-spread |
|---|---:|---:|---:|
| peak15 | 9/24 | 4/24 | 4/24 |
| peak12 | 5/24 | 7/24 | 0/24 |
| win30_10 | 8/24 | 15/24 | 9/24 |

Every rule loses more windows than it wins in five of nine cases and
never wins convincingly. (The `peak12` chaos row shows +12.0 return
points, which is the harness's known ~20-point noise floor, not an
effect: the same rule shows -6.0 under quarter-spread fills and wins
0 of 24 windows there.)

## 3. Why it fails

Three reinforcing reasons, all visible in the data.

**A freeze cannot touch inventory.** The worst drawdown ran from
2025-01-17 ($37,148) to 2025-04-08 ($28,535): 55 trading days, exactly
the slow grind the recommendation targeted. Over those 56 ticks the
strategy filled just 12 new CSPs and took 5 assignments, while cash
fell $11,174 as assignments converted it into stock. The decline is
mark-to-market on assigned shares and open puts. Freezing new entries
does not sell, hedge, or shrink any of that. By the time any anchor
trips, the loss is already sitting in positions the freeze leaves
untouched.

**The entry pipeline has already stopped itself.** In precisely the
conditions where a slow anchor fires, the deterministic gates are
already refusing almost everything: the 50-DMA trend filter rejects
names trading below their average, the S2 economic cap counts the
assigned shares against the per-name budget, and cash is locked in
stock. That is why `peak15` froze for an extra 11 days and blocked
only 8 CSP entries all run. The breaker is largely redundant with
constraints that already bind.

**What the freeze does bite is the recovery.** `new_entries_enabled`
gates the covered-call leg too, so a longer freeze suppresses the
income the wheel earns on assigned stock, which is its recovery
mechanism. The clearest signal is `peak12` under quarter-spread fills:
47 covered calls blocked, 6.0 return points lost, drawdown unchanged.
The cost of these rules is not theoretical; it is CC premium forgone
while holding the same risk.

## 4. Fairness check: was S2 doing the work?

A reasonable objection: maybe the slow anchor looks useless only
because the S2 economic cap already removed the deep drawdowns. So the
same rules were run against the pre-S2 configuration (trend filter on,
economic cap disabled), where the numbness was originally observed:

| Pre-S2 run | Return % | CAGR % | Max DD % | CAGR cost per DD point |
|---|---:|---:|---:|---:|
| baseline | 40.96 | 14.98 | 28.65 | — |
| peak15 | 30.58 | 11.46 | 26.47 | 1.61 |
| peak12 | 28.49 | 10.73 | 25.86 | 1.52 |

So the anchors do work pre-S2, and the price is terrible. For
reference, from the forensics: the S2 economic cap at 20% removed
about 5.5 drawdown points at no measurable CAGR cost, the 12% variant
cost 0.20 CAGR points per drawdown point, and the trend filter (the
worst control tested there) cost 0.90. A slow breaker anchor costs
1.5-1.6, roughly double the previous worst, to buy protection that S2
already provides more cheaply. The objection does not rescue the rule;
it explains why the rule is redundant.

## 5. Decision criteria

* Lower max drawdown: **no** (identical under production config;
  marginally worse for two of three rules).
* Equal or higher return: **no** (neutral at best, -1.8 to -2.0 points
  for two rules; -10 points pre-S2).
* Fewer days deep in drawdown: **no** (88 vs 88-89 days at >= 10%).
* Robust across probes: **no** (max DD unmoved in all three families;
  rolling windows losing).
* Reduces tail risk another way: **no** (assignments unchanged, 65-68).
* Justifies added complexity: **no**.

## 6. Recommendation

Keep `strategy/drawdown.py` exactly as it is: 7% below the trailing
7-day high, freezing entries only. Recommendation 3 is closed as
tested and rejected.

The underlying instinct was sound: a 7-day window genuinely cannot see
a slow grind. The error was assuming the breaker was the right place
to fix it. Drawdown in this strategy is an inventory problem, and the
control that works on inventory is the one already shipped (S2's
assignment-aware economic cap, which prevents the concentration from
forming). Adding a second freeze rule on top treats a symptom the
freeze cannot reach, and charges covered-call income for the
privilege.

If slow-grind visibility is still wanted, the honest form is
**reporting, not control**: surface the peak-to-date drawdown in the
daily Telegram summary so the operator can see a grind developing and
exercise judgement. That costs nothing, blocks nothing, and preserves
the human-in-the-loop posture the S1 design already chose. It is not
implemented here; it is a suggestion, not a finding.

## 7. Files added or modified

Added (research only, no production imports):

* `src/kai_trader/backtest/experiments/breaker_rules.py` (slow-anchor
  definitions and the pure drawdown function)
* `scripts/run_breaker_experiment.py` (12-run matrix: 4 rules x 3
  probe families)
* `scripts/analyze_breaker_experiment.py` (exact freeze-state
  reconstruction, blocked-intent accounting, cross-family consistency)
* `tests/backtest/test_breaker_rules.py` (15 tests)

Modified (backtest harness only, additive, default off):

* `src/kai_trader/backtest/drawdown_sim.py`: optional `breaker_rule`
  parameter. The union can only trip earlier or hold longer; with no
  rule, every number is the production one unchanged.
* `src/kai_trader/backtest/runner.py`, `cli.py`: `--breaker` flag
  threaded through, recorded in `run_config.json`.

**No production file was touched by this experiment.**

Run artifacts (gitignored, regenerable): `backtest_runs/breaker/`,
`backtest_runs/breaker_pre_s2/`.

## 8. Tests and gates

* `uv run pytest`: 1,210 passed, 7 env-gated integration skips
  (includes the 15 new breaker tests).
* `uv run ruff check src/` and the new scripts/tests: clean.
* `uv run mypy --strict src/`: clean.
* Parity: `--breaker baseline` reproduces the recorded
  `gate_native_trend_econ20` run with a byte-identical `equity.csv`
  and `ticks.csv`.

## Limitations

Daily bars, so the breaker is evaluated once per day at the close
where production evaluates it every 5 minutes; an intraday crash would
trip production sooner than this harness shows, which if anything
makes the fast rule look worse here than it is. Estimated historical
spreads, Black-Scholes greek reconstruction, no dividends, assignment
modeled at expiry only. The window contains no 2020- or 2022-style
bear market. The freeze-state reconstruction in the analyzer is exact
(the breaker is a pure function of the recorded equity curve), and
entries are attributed to the tick after a breach because the harness
evaluates the breaker at end of tick.
