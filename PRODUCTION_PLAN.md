# Production Plan

**Status:** drafted 2026-05-06 after a heavy day of fixes. Live money is **not yet** authorized. This plan is the concrete checklist that gets us there.

The detailed companion document is `LIVE_TRADING_PLAN.md` (1064 lines, drafted 2026-05-02). This file is the short, current, actionable version. Any conflict between the two — this file wins because it reflects today's state.

---

## What today fixed

Five days of silent failures surfaced and were resolved between 2026-05-05 and 2026-05-06:

- [x] `lxml` dependency missing → added; ETF earnings filter now works (commit `e5f7817`)
- [x] yfinance `curl_cffi` 170 MB retention → swapped to `Ticker.calendar`, 13× memory reduction (`392259d`)
- [x] ETF false positives in earnings filter → bypass via `fast_info.quote_type` (`3e7da0f`)
- [x] Migrations 020 + 021 not applied → manually applied; bot resumed ticking
- [x] No startup migration check → `assert_schema_up_to_date` runs on boot (`eee2167`)
- [x] Telegram crossover noise → quiet `Conflict` error handler (`eee2167`)
- [x] No out-of-band liveness alerting → healthchecks.io ping per tick (`d51b780`)
- [x] No CI → `.github/workflows/ci.yml` with unit + Postgres schema integration jobs (`d8773d6`)
- [x] No income visibility → `/income` slash command with round-trip P&L + mark-to-market (`061ae20`)
- [x] OOM crashloop on 512 MB → upgraded Render plan to 2 GB Standard

Today's bot is provably better than yesterday's, but it's also brand-new in terms of stable runtime hours. The hard prerequisites below are about closing that gap.

---

## Hard prerequisites — must be green before any live capital

### P0-1. Drawdown circuit breaker dry-run (~2 hours)

The circuit breaker at `src/kai_trader/strategy/drawdown.py` is the only emergency brake on the bot. **It has never fired in this account.** With real money this is unacceptable; if it's silently broken (like migration 021 was), you'd find out during a real drawdown — too late.

**What to ship:**

- `scripts/test_drawdown_trip.py` that:
  1. Inserts a synthetic `account_snapshots` row 7% below the high-water mark
  2. Forces a strategy tick (or calls `check_drawdown` directly)
  3. Asserts `kill_switch=true` was set in `system_flags`
  4. Asserts a `critical` notification was enqueued
  5. Resets state at the end (so paper trading resumes)
- Run it. Bot must trip cleanly.
- Add it to CI as a smoke test that uses a fresh test DB.

**Owner:** Claude session; ~2 hours total.

### P0-2. Bot survives 5+ trading days clean (running now → ~2026-05-12)

This is just observation. The bot needs to:

- Tick reliably every market open (cadence ~7-9 min)
- Survive a weekend → market open transition without intervention
- Survive a Render deploy crossover (the next code push will exercise this)
- Show no surprise failures in 5 trading days

**Tracker:**

| Trading day | Date (UTC) | Status |
|---|---|---|
| 1 | 2026-05-05 (today) | ✅ ticking, no errors, /income working |
| 2 | 2026-05-06 | (pending) |
| 3 | 2026-05-07 | (pending) |
| 4 | 2026-05-08 (Fri) | weekend boundary test |
| 5 | 2026-05-11 (Mon) | post-weekend resume test |

If any day surfaces a new bug, the timer restarts.

### P0-3. Full wheel cycle observed (timing depends on positions)

None of these have happened on this account yet:

- **Assignment**: a short put goes ITM at expiry, account is assigned 100 shares per contract
- **Covered call**: bot writes a CC against the assigned shares
- **CC profit-take or expiration**: shares are called away or premium expires worthless

The current open positions (F P11.5, GM P75, WFC P78) all expire 2026-05-15. If any go ITM on that Friday, the assignment cycle starts and we'd observe leg 1. If not, write deliberately closer-to-the-money strikes after the next Monday so the cycle can be exercised.

**Acceptance:** at least one full assignment → CC → close cycle observed end-to-end without manual intervention. The relevant code paths in `kai_trader/strategy/{assignment,covered_calls}.py` need to fire on real data.

---

## Capital ramp — once P0s are green

Live trading requires reduced scale on day 1 regardless of how clean the paper run looks. Today's evidence is "we found three silent bugs in one day" — the right mental model is "one more bug exists; we just haven't tripped it yet."

| Stage | Capital | Per-tick cap | Per-day cap | Drawdown trigger | Whitelist |
|---|---|---|---|---|---|
| **Stage A: live launch** | $10k–$15k | 5% | 10% | -3% | 5–9 cheapest names |
| **Stage B: 2 weeks clean** | $25k–$50k | 7% | 20% | -5% | 14 names |
| **Stage C: full size** | full | 10% | 30% | -7% | full whitelist |

### Stage A details

- Capital: $10k–$15k. Anything less than ~$10k can't deploy meaningful diversification under the per-name 15% cap.
- Pick **5–9 cheapest names** that fit (F, T, KVUE, PFE, WBA, HOOD, MARA, RIVN, SOFI). Drop everything ≥ $50 strike for now.
- Tighter caps and tighter drawdown trigger because we're proving the live path, not seeking yield.
- ALPACA_PAPER=false flips the switch. Trading_enabled flag is the second guard.
- Watch daily; one surprise → kill switch, debug, reset Stage A timer.

### Stage B details

- Bump capital after 14 calendar days of clean Stage A operation.
- Re-add medium-strike names (BAC, KO, MO, GM, WFC, GDX, SLV, XLF).
- Loosen drawdown trigger to -5%.
- Caps move toward production levels.

### Stage C details

- Full whitelist, full caps. Look like paper.
- Only after Stage B has proven 14+ clean days.

---

## Ongoing operational discipline

These don't gate Stage A but they tighten the loop:

- [x] Boot-time migration check (shipped today)
- [x] CI runs on every PR (shipped today)
- [x] Heartbeat alerts within 30 min of any silent stop (shipped today)
- [ ] Pre-deploy checklist: dependency probe, migration dry-run, dry-tick. (Schema migration check now runs at boot, which covers a lot, but a pre-merge tick smoke would catch unrelated breakage.)
- [ ] Daily realized-P&L summary auto-posted to Telegram at UTC midnight. The `/income` command exists; this would just be a scheduled call.
- [ ] Weekly equity-curve chart in Telegram (uses `account_snapshots` history).
- [ ] Periodic account snapshot writer (currently manual via `/snapshot_now`). Wire a 1-hour scheduled task during market hours.

These are all nice-to-have. None of them block live; all of them improve the operator UX.

---

## Kill-switch protocol for live operation

Once live, these are the only conditions that are NOT operator-discretionary:

1. **Drawdown breaker fires** (-X% from HWM) → kill_switch flips automatically; new entries blocked; rolls and closes still allowed.
2. **Two consecutive ticks fail with the same error** → manual investigation. Don't restart blindly.
3. **Any "alert" or "critical" notification** → read in full before next tick; if unclear, kill switch and investigate.

Operator-discretionary stops:

- Macro shock (FOMC surprise, earnings volatility spike across the whitelist) → discretionary kill until resolved.
- Want to be away from the bot for >24 hours → kill before going dark.

---

## Open questions to decide before live

- [ ] **Real-money Alpaca account funding source.** Currently the paper key is hardcoded into Render env vars. The live key needs a separate path; ideally `ALPACA_API_KEY_LIVE` + `ALPACA_API_KEY_PAPER` and `ALPACA_PAPER` selects which is used. Today the live and paper keys collide in env naming.
- [ ] **Sleeve allocation under reduced caps.** Currently the only enabled sleeve is `index_core` with a 30-symbol whitelist (despite the name). Stage A wants a smaller whitelist: do we change the sleeve config row in DB, or add a new "live_minimal" sleeve and disable the others?
- [ ] **What's the trigger for moving from Stage A to Stage B?** "14 calendar days clean" is the calendar floor. But "clean" needs a definition: zero failed orders, zero alert-priority notifications, drawdown breaker hasn't tripped, no operator interventions. Write this down before starting Stage A so it's not a moving target.

---

## Timeline (best-case)

| Date | Milestone |
|---|---|
| 2026-05-06 (today) | Drawdown dry-run shipped + verified |
| 2026-05-06 to 2026-05-12 | 5 trading days of paper observation |
| 2026-05-12 to 2026-05-15 | Decide on env split, write Stage A acceptance criteria, prepare reduced sleeve config |
| 2026-05-15 (Fri) | Earliest plausible Stage A flip. Open positions expire that day; clean weekend leads into Monday's start |
| 2026-05-18 (Mon) | Stage A live with $10-15k |
| 2026-06-01 (Mon) | Stage B if Stage A is clean for 14 days |
| 2026-06-15 (Mon) | Stage C if Stage B is clean for 14 days |

That's ~6 weeks from today to full size, assuming nothing surprises us. Faster paths exist but they require accepting more risk on the front-end. The honest advice is don't.
