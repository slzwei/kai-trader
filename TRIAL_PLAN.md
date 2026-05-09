# Kai Trader 2-Week Paper Trial — Phase 12 Calibration

_Plan for running the current calibration on the kai-trader-tester
paper account ($30k, PA3GV4CEIGB6) for 14 calendar days (10 trading
days) and using the data to decide whether to advance to live capital._

## Pre-flight checklist (do once, before the trial starts)

These are gating items. If any fails, stop and fix before the
trial starts. The goal is making sure the bot is actually running
the calibration we think it's running.

- [ ] **Render env vars updated** to point to PA3GV4CEIGB6
  - `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_API_KEY_PAPER`,
    `ALPACA_SECRET_KEY_PAPER` all = the new paper key + secret
  - Render service has redeployed (check Deploys tab; status "Live")
- [ ] **`/account` in Telegram** returns account number
  `PA3GV4CEIGB6` and equity ≈ $30,000
- [ ] **`/positions` in Telegram** returns 0 positions (fresh
  account)
- [ ] **`/sleeves` in Telegram** shows the 12-name universe with
  - profit_take_pct = 0.10, target_delta_put_risk_on = -0.45,
    target_dte_max = 14, roll_trigger_delta = 0.35,
    earnings_blackout_enabled = true
- [ ] **`/flags` in Telegram** shows trading_enabled=true,
  new_entries_enabled=true, kill_switch=false
- [ ] **First strategy tick** logs successfully on Monday open
  (check `account_snapshots` table for a fresh row dated Monday
  9:30 ET; check `orders` for the first CSP attempt)

## Trial parameters

- **Window**: 14 calendar days starting next trading session
  (effectively 10 trading days, ~2 expiration cycles for 7-DTE
  options)
- **Capital**: $30,000 (paper)
- **Calibration**: Phase 12 (current code state). Cash-secured
  initially (margin_factor=1.0); we evaluate leverage in week 2 if
  the calibration is producing the expected results.
- **Telegram alerts**: every tick summary, every fill, every
  drawdown trip

## Week 1 (Mon 5/11 - Fri 5/15): cash-secured baseline

Goal: verify the bot's behavior matches the backtest under
identical (cash-secured) conditions.

**Watch for daily:**
- **Fill count per day**: backtest projects 1.2 fills/day average.
  A day with 0 fills is fine; a week with <3 fills total = problem.
- **Average fill price vs day-median trade price**: fills should
  land near the day's median (the bot now submits at mid). If 50%+
  are below day-Q1, the mid-pricing change isn't working.
- **Account equity drift**: backtest shows monthly compound 3.0%/mo;
  for 1 week that's roughly +0.7%. Reality may be -3% to +5%
  (single-week variance).
- **Open position count**: should grow from 0 to 8-15 by end of
  week 1 as the bot builds the book.

**Red flags:**
- Kill switch trips at any point (drawdown breaker fired)
- More than 30% of intent attempts log `failed` status
  (broker-side rejection)
- Any position larger than 25% of equity ($7,500 face)
- Any one symbol with more than the per-name dollar cap

**End-of-week-1 review (Sat 5/16):**
- Compute week 1 actual return vs backtest projection (~+0.7%)
- Check fill quality on each entry vs OPRA trade prints (same
  script I ran earlier — `head -30 trades.csv`)
- Decide: continue to week 2 with same calibration, or adjust

## Week 2 (Mon 5/18 - Fri 5/22): margin-enabled comparison

Goal: see if margin actually helps the live result like the
backtest predicted (it didn't for Phase 12 → same headline as cash-
secured).

**Action at start of week 2:**
- Update `ALPACA_PAPER` to keep paper, but flip Render env var
  setting any margin gate (currently the worker doesn't pass a
  margin factor — production is implicitly cash-secured because
  Alpaca's paper account itself decides margin via Reg-T). On the
  Alpaca account, margin behavior is automatic; the bot doesn't
  need a flag flip.
- The PRODUCTION code still has TOTAL_DEPLOYMENT_CAP_PCT = 4.00
  (Phase 12), so the bot will deploy aggressively against whatever
  margin Alpaca's paper account allows.

**Watch for week 2:**
- **Buying power utilization**: the current `/account` check shows
  buying_power. Track how close the bot gets to 100% utilization.
  Backtest predicted bot wouldn't reach the cap; reality should
  match.
- **Rolls fired**: backtest shows 12-14 rolls in 24 months. For
  2 weeks expect 0-2 rolls. More than 5 = something's wrong.
- **Assignments**: backtest projects 47-54 assignments over 24
  months ≈ 2/week. A week with 5+ assignments suggests the
  underlying is in a regime the strategy doesn't handle well.

**End-of-trial review (Sat 5/23):**
- Compute 2-week total return vs backtest projection (~+1.5%)
- Calculate fill-quality vs OPRA trade prints across all entries
- Compute "deployment ratio": avg open collateral / equity
- Decide: live capital, more paper time, or back to recalibration

## Backtest projections to compare against

These are what the Variant A calibration (cash-secured, blow-up-resistant) produced over 24 months
on $30k starting capital. Reality may diverge for many reasons —
single-week variance is huge — but if the trial diverges by >2x in
either direction over 2 weeks, that's a signal.

| Metric | 24-month backtest | Per-week pro-rata | 2-week trial expectation |
|---|---:|---:|---:|
| Total return | +60.8% | ~1.7% | +0% to +5% (high variance) |
| Monthly compounding | 3.00% | — | — |
| Max drawdown 30% | — | up to 15% in 2 weeks plausible |
| CSP fills | 300 / 542d | 3 / week | 4-8 fills total |
| Profit takes | 97 / 542d | 1.3 / week | 1-3 in 2 weeks |
| Assignments | 54 / 542d | 0.5 / week | 0-2 in 2 weeks |
| Sharpe | 0.91 | — | — |
| Win rate (per row) | 91% | — | — |

## Decision matrix at end of trial

| Outcome | Decision |
|---|---|
| 2-week return -10% to +5%, DD < 15%, fills landed near mid, no kill switch | **Continue paper trading 1 more month, then consider live small** |
| 2-week return > +5% with reasonable DD | **Watch for over-deployment risk; continue paper 1 month** |
| 2-week return < -10% (drawdown too deep too fast) | **Stop. Recalibrate. The risk profile is worse than backtest** |
| Kill switch tripped at any point | **Stop. Investigate the trigger before any further trading** |
| < 5 fills total across both weeks | **Stop. Bot is gate-bound or not deploying. Diagnose, don't trade real money on a barely-active strategy** |
| Fill quality consistently below day-Q1 | **Stop. The mid-pricing change isn't working as expected. Diagnose** |

## Live-capital sizing (only if 2-week paper passes the matrix)

Do NOT deploy 100% of intended capital on day 1. Sizing schedule:
- Month 1 of live: 25% of intended capital
- Month 2 of live: 50% (if month 1 hit ±50% of paper expectation)
- Month 3 of live: 100% (if month 2 also tracked paper)
- Any month that diverges materially → drop back one tier

## What to do every day during the trial

- **9:30 ET (22:30 SGT)**: bot starts ticking. First tick takes
  ~5 min to settle, populate snapshots, evaluate candidates.
- **End of NY session (16:00 ET, 04:00 SGT)**: read the daily
  Telegram summary the bot posts.
- **Saturday morning SGT**: review the week. Use the bot's
  weekly-chart Telegram post as a starting point.

## What to do if something breaks

- **Kill switch trips**: first check `/flags`. If kill_switch=true,
  read the most recent `notifications` table row tagged 'critical'
  to find the trigger reason. Do NOT clear the flag without
  understanding what caused it.
- **Bot stops responding**: check Render service logs. If the
  worker process crashed, Render auto-restarts but ticks may have
  been missed.
- **Massive negative day**: don't intervene. Wait for the next
  tick. If kill_switch hasn't tripped, the drawdown breaker
  doesn't think it's bad enough to halt. Trust the gate.
- **Discretionary close needed**: use `/close <SYMBOL>` then
  `/close_confirm <SYMBOL>` within 30 seconds.

## What I'll do during the trial

I won't ship more code changes during the trial unless something
is broken. The point of the trial is to measure THIS calibration
against the backtest, not a moving target. If the data is good,
we discuss live capital next. If the data is bad, we recalibrate
with new evidence.

If you want me to check in mid-trial (e.g. end of week 1), ping
me and I'll pull the data from Supabase + compare to backtest
projections.
