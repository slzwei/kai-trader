# Live Trading Readiness Plan

Status: **drafted 2026-05-02**, awaiting execution. Do not flip `ALPACA_PAPER=false` until every Tier-0 hard gate is green and the Stage-A ramp protocol is in motion.

This document is **self-contained**. A fresh Claude instance with no prior conversation context should be able to read this top-to-bottom, understand the project, find every relevant file, and execute the work without asking for context. Where you need to look at files, exact paths are given. Where you need to make decisions, the criteria are written down.

---

## 1. Project context (read this first)

### What Kai Trader is

A single-owner automated options-wheel trading system. One operator (Shawn Lee, Telegram owner ID in `TELEGRAM_OWNER_ID`), one Telegram bot front-end, one Postgres database (Supabase) for state, Alpaca as the broker. Currently runs on Alpaca paper. Goal of this plan: prepare it for live capital.

The bot writes defensive cash-secured puts on a curated universe, takes assignments where they happen, writes covered calls against assigned shares, profit-takes early when premium captured exceeds a threshold, rolls underwater shorts when net credit is available, and respects a regime-aware risk posture (`risk_on` / `neutral` / `risk_off`).

### Architecture

```
Telegram (owner) ──► python-telegram-bot (long-poll)
                       │
                       ├─ slash commands (status, positions, close, kill, ...)
                       ├─ free-form text → conversational handler (Anthropic SDK)
                       └─ inline-keyboard callbacks (approval, close)
                       │
                       ▼
              Supabase Postgres (state of truth)
                       │
                       ▼
              Strategy worker (5-min tick) ──► Alpaca (paper today)
              Trading-stream worker (websocket fills)
              Notification worker (Telegram delivery)
              Event dispatcher (proactive alerts + approvals)
```

### Tech stack

- Python 3.11+, `uv` for dependency management
- Supabase Postgres via asyncpg
- python-telegram-bot v20 (async)
- Anthropic Python SDK (`claude-sonnet-4-6` via prompt caching) for the chat handler
- Pydantic v2, structlog, pytest + pytest-asyncio, ruff, mypy --strict
- Deploy: Render Background Worker (Docker, region: Singapore)

### Read these files before doing any work

| Purpose | Path |
|---|---|
| Project conventions, env vars, current state | `CLAUDE.md` |
| Phase tracker (history of shipped work) | `TRACKER.md` |
| Original Phase 3 spec (regime, sleeves) | `PHASE3.md` |
| Phase 5 spec (rolls, profit-take, CCs, assignment, earnings) | `PHASE5.md` |
| Strategy entry-point | `src/kai_trader/strategy/worker.py` |
| Candidate selection / cap math | `src/kai_trader/strategy/candidates.py` |
| Earnings filter | `src/kai_trader/strategy/earnings.py` |
| Profit-take engine | `src/kai_trader/strategy/profit_take.py` |
| Roll logic | `src/kai_trader/strategy/rolls.py` |
| Drawdown circuit breaker | `src/kai_trader/strategy/drawdown.py` |
| Regime classifier | `src/kai_trader/strategy/regime.py` |
| Broker (Alpaca wrapper) | `src/kai_trader/broker/alpaca.py` |
| Sleeve config table helpers | `src/kai_trader/db/sleeve_config.py` |
| Latest schema (small-account pool) | `src/kai_trader/db/migrations/018_small_account_pool.sql` |

### Conventions you must follow

- **Type hints required.** `mypy --strict src/` must pass.
- **No em-dashes anywhere** (code, comments, docs, commit messages). Periods, commas, colons only.
- **Never `print`.** Use `structlog` via `kai_trader.logging.get_logger`.
- **Conventional commits.** `feat:`, `fix:`, `chore:`, `test:`, `docs:`, `refactor:`.
- **Audit every command.** Both authorised and unauthorised Telegram messages land in `bot_commands`.
- **Silent-ignore strangers.** Bot does not reply to non-owner Telegram users at all.
- **Migrations are plain SQL, numbered, idempotent.** `schema_migrations` tracks what ran.
- **Gating is sacred.** `kill_switch`, `trading_enabled`, `new_entries_enabled`. The flag check inside the broker submitter is the **last** check before any HTTP call. Even if a tick races a flag flip, the broker refuses cleanly.
- **All mutating state changes from chat go through `pending_changes`.** Approval flow exists in `bot/handlers/approval.py`.

### Deploy mechanics (important; the webhook is unreliable)

- Render service: `kai-trader` (Background Worker, Docker, plan: starter, region: Singapore).
- Service ID: `srv-d7n5su7lk1mc73b4g8eg`.
- Render is configured to deploy from branch **`claude/kai-trader-phase-1-sHFJk`**, NOT `main`. Pushing to `main` does nothing.
- To deploy: `git push origin <local-branch>:claude/kai-trader-phase-1-sHFJk`.
- The auto-deploy webhook from GitHub to Render has been observed to **silently not fire**. If a push doesn't trigger a deploy event within ~30 seconds, go to the Render dashboard and hit **Manual Deploy → Deploy latest commit**.
- Render starter plan is 512MB. **The bot has been OOM-killed at least once (April 30 2026).** Plan must be bumped or the service migrated before live capital.

### MCPs available in this project

- **Alpaca MCP** (`mcp__alpaca__*`): full account, positions, orders, market data, options chain. Use this to ground any analysis in real broker state.
- **Render MCP** (`mcp__claude_ai_Render_MCP__*`): deploys, logs, env vars, services. Requires workspace selection — ask the user, do **not** select for them.
- **Supabase MCP** (in `.mcp.json`): schema, SQL queries, logs.

---

## 2. Current state snapshot (as of 2026-05-02 02:30 SGT)

### Account (Alpaca paper)

- Equity: **$101,186**
- Cash: $102,826
- Initial margin used: **$35,000** (~35% of equity)
- Options buying power: $67,826

### Open positions

| Symbol | Strike | Qty | Premium received | Current mark | P/L | Collateral | DTE |
|---|---|---|---|---|---|---|---|
| `MARA260508P00011500` | $11.50 P | -20 | $0.465 | $0.490 | -$50 | $23,000 | 6d |
| `SNAP260508P00006000` | $6.00 P | -20 | $0.325 | $0.330 | -$10 | $12,000 | 6d |

Earlier today the book held AMZN $250P x-2 + AVGO $400P x-1 with 89% collateral utilisation. Those were closed (manually or via expiry rolldown), freeing capital that the strategy redeployed into MARA + SNAP per `migration_018`.

### Active strategy posture (per `migration_018`)

- Single-sleeve pool (the prior 25/30/45 split fragmented capital below the deployable threshold for a $25K account).
- Sleeve `index_core`: `target_pct = 1.00`, enabled.
- Sleeves `stable_largecap` and `opportunistic`: disabled.
- 30-name whitelist (mix of cheap-liquid optionables): `F, T, BAC, PFE, KO, KVUE, VZ, INTC, CSCO, GE, KMI, KHC, MO, WBA, HOOD, SOFI, PLTR, MU, MARA, RIOT, SNAP, RIVN, WFC, GM, C, GDX, SLV, XLF, XLE, EEM`.
- `max_new_entries_per_tick = 2` to prevent the 30-name pool from flooding the book on a single tick.
- A multi-factor ranker in `candidates.py` picks the best 1-2 names per tick by annualised yield × spread quality (this needs verification — see Task T-6.1 below).

### What's running where

- **Render branch:** `claude/kai-trader-phase-1-sHFJk` (latest commit `fa10141` "feat: tappable inline-keyboard buttons for /close" as of 02:25 SGT).
- **Local `main`:** also at `fa10141`, plus uncommitted in-progress chat-related changes from a parallel work stream (do not touch unless you explicitly own them):
  - `M scripts/create_chat_ro_role.py`
  - `M src/kai_trader/broker/alpaca.py`
  - `M src/kai_trader/chat/system_prompt.py`
  - `M src/kai_trader/chat/tools.py`
  - `M src/kai_trader/db/readonly.py`
  - `M tests/test_broker_alpaca.py`
  - `M tests/test_chat_readonly.py`
  - `M tests/test_chat_tools.py`
  - `?? tests/test_chat_accuracy.py`

### Diagnostic findings from the 2026-05-02 session

These are the issues that surfaced during the live diagnostic. Each becomes a hard-gate or task below; cross-reference by ID.

| Finding ID | Severity | Description |
|---|---|---|
| **F-1** | Critical | `/close SYMBOL` was broken for option positions. Called Alpaca `close_position("AMZN")` with the bare ticker, which returns `40410000 position not found` because held positions are OCC option symbols. **Fixed in commits `f66bd33` + `fa10141`** (lookup-based + tappable buttons). |
| **F-2** | Critical | Cap math in `build_intents_with_diagnostics` did not subtract collateral already locked in open short puts, so the strategy stacked AMZN/AVGO to 89% of equity in two names. **Fixed in commit `63a77e6` (Phase 5e).** |
| **F-3** | Critical | Earnings filter (`strategy/earnings.py`) **fails open** when yfinance returns None. Acceptable for paper, **unacceptable for live capital.** See G-2 below. |
| **F-4** | Critical | OOM crash on Render starter (512MB) on April 30. Bot bounced and was visible in Render events as "Instance failed: Ran out of memory." See G-3, T-8.5. |
| **F-5** | High | 5 failing tests in `tests/test_strategy_worker.py` (`test_tick_submits_when_flags_green`, `test_tick_skipped_intent_records_skipped_status`, `test_tick_failed_intent_records_failure`, `test_tick_skips_intent_with_prior_same_day_failure`, `test_tick_submits_covered_call_against_held_shares`). All assert `"Submitted: 1" in summary` but production tick reports "Warning: no expirations in sleeve DTE band (1 puts had delta, none in band)". Pre-exists since commit `5137da5`. **Production code path diverges from what tests cover.** See G-6, T-6.0. |
| **F-6** | High | Render auto-deploy webhook is unreliable. Pushes to the deploy branch occasionally do not trigger a deploy event. Workaround documented (manual deploy). See T-8.8. |
| **F-7** | Medium | `_pending` close dict is in-process memory. Lost on restart (e.g. the OOM event). Combined with the 30-second TTL, this leaves a window where staged closes silently disappear. See T-8.1. |
| **F-8** | Medium | Triple-response on edited Telegram messages. When the operator edits a `/close` message, the bot processes the edit history as multiple commands, producing nonsense replies (e.g. "Usage: /close_confirm SYMBOL" + "No open AMZN position to close." + "Close staged for AVGO"). Annoying, not dangerous. See T-8.9. |
| **F-9** | Medium | Per-name notional cap is implicit (sleeve cap → per-symbol headroom math) but there is no explicit hard limit. MARA at $23K = 23% of equity is over a sensible 15% cap. See G-4, T-7.2. |
| **F-10** | Medium | No portfolio-level Greeks (delta, vega, gamma) aggregation or limits. The book's net exposures are blind. See G-4, T-7.1. |
| **F-11** | Strategic | The multi-factor ranker referenced in `migration_018` claims to score by "annualised yield × spread quality" but it is unverified whether it actually scores on the variable that matters for an options seller: **IV / RV ratio**. If it does not, the strategy is harvesting time decay without conditioning on whether vol is rich. That is not edge; that is randomly selling premium. See T-6.1, T-6.5. |
| **F-12** | Critical | `MAX_CONTRACTS_PER_SYMBOL = 10` at `candidates.py:32` is documented as a "hard ceiling regardless of sleeve headroom" but `_max_qty_for` (`candidates.py:317-336`) only applies it **per build call**, not against existing held contracts. **Observed live**: MARA reached 30 contracts (3 ticks × 10) and SNAP reached 40 contracts (4 ticks × 10), both 3-4× the intended ceiling. The constant exists to prevent exactly this; the implementation silently fails. See G-11, W-2. |
| **F-13** | Critical | The per-symbol $ cap (`per_symbol_cap_pct` × equity, default 60% of equity at $100K-tier) is **strike-blind**. On SPY-class strikes (~$580) it reasonably bounds qty. On MARA at $11.50 strike it allows 52 contracts; on SNAP at $6 it allows 100. This is how 40% of equity wound up in one social-media stock and 51% in one crypto miner. See G-11, W-3. |
| **F-14** | Critical | No per-tick total-deployment cap and no daily new-deployment cap. **Observed**: 4 ticks across 20 minutes (18:22 to 18:42 UTC on May 1) took the book from 0% to 96% of the $70,683 deployment cap, leaving $2,500 of dry powder for any overnight regime change. A live system needs both a per-tick velocity cap (prevents single-tick blow-out) and a daily ramp cap (prevents 4-hour blow-out across many ticks). See G-12, W-4. |
| **F-15** | High | The greedy ranker keeps returning MARA P11.50 and SNAP P6 as top scores each tick (high annualised yield × tight spread). Phase 5e's collateral-aware cap math only blocks re-attempts when the per-symbol $ cap is fully consumed; below that, the same strikes accumulate. The ranker has no anti-recency penalty and no cool-down between entries on the same symbol. See G-12, W-4. |
| **F-16** | Medium | Entry deltas are not verified post-fill. Target was -0.40 (risk_on) or -0.30 (neutral); `actual_delta` is stored only inside `orders.intent_payload` jsonb, not in a queryable column. **Without a post-fill check, a fill significantly outside the target band is invisible** until next tick's reconciliation, and even then only by inspecting position deltas. See G-13, W-9. |

---

## 3. Hard gates (must be true before any live capital)

These are not improvements; they are **defects** the system has today that would lose real money. None can be skipped, deferred, or downgraded. Each maps to one or more tasks below.

| Gate | Pass criteria | Tasks |
|---|---|---|
| **G-1: Edge proven, not assumed** | Backtest of the current rule-set across 2018, 2020, 2022, and 2024-2025 regimes shows positive risk-adjusted return after costs (slippage, commissions, exercise risk). Lower 5th-percentile bootstrap MaxDD < 30% of equity. | T-6.1 .. T-6.4 |
| **G-2: Earnings filter fails closed** | `is_earnings_in_window` returns "skip" (not "trade") on data unavailability. Primary source is a paid feed; yfinance is a fallback only. | T-6.0 (subtask) |
| **G-3: No OOM on the live runtime** | Bot has run for 14 consecutive days with zero OOM events. Memory ceiling at least 2× observed steady-state. | T-8.5 |
| **G-4: Portfolio Greeks tracked + limited** | `book_greeks` table populated each tick. Hard limits on net delta, net vega, net gamma at expiry, per-name notional. Pre-submit risk gate refuses any new entry that would breach a limit. | T-7.1, T-7.2 |
| **G-5: Concurrent-expiry correlation cap** | No more than X% of equity at risk on a single expiry date. Default X = 25%. | T-7.2 (subtask) |
| **G-6: All tests green** | `uv run pytest` zero failures. The 5 strategy worker test failures from `5137da5` are resolved (either tests updated to match new behaviour, or code fixed to match tests). | T-6.0 |
| **G-7: Live-capital sentinel separate from `trading_enabled`** | New flag `live_capital_enabled` (default false). Broker submitter checks it explicitly. Per-day, per-week, per-trade USD caps enforced when this flag is on. Telegram reports the live-vs-paper status on every relevant command. | T-7.8 |
| **G-8: State persistence** | `_pending` and any other in-memory transactional state move to Postgres. Bot restart leaves zero ambiguity about what was staged or in-flight. | T-8.1 |
| **G-9: Two-channel critical alerting** | Telegram for routine. SMS or PagerDuty (or equivalent) for: kill_switch tripped, broker error rate > N/min, last-tick > 30 min ago, drawdown threshold hit. One channel = single point of failure. | T-8.4 |
| **G-10: Operator runbooks committed** | `docs/runbooks/*.md` covering: bot won't start, broker 5xx for 5 min, last tick 1 hour ago, unexpected assignment, kill_switch stuck on, staged close lost. Each: symptoms, diagnosis, fix, prevention. | T-8.7 |
| **G-11: Per-symbol caps enforced cumulatively** | `MAX_CONTRACTS_PER_SYMBOL` and the per-symbol $ cap both subtract existing held contracts/collateral before computing remaining headroom. A symbol at the contract ceiling cannot accumulate further on subsequent ticks. The $ cap is tightened to a strike-aware floor that does not balloon for low-priced underlyings. | W-2, W-3 |
| **G-12: Deployment velocity capped (per tick + per day) + per-symbol cool-down** | Total new collateral deployed in any single tick is capped (default ≤ 10% of equity). Total new collateral deployed since UTC midnight is capped (default ≤ 30% of equity). A symbol entered in tick N is excluded from candidate selection for the next K ticks (default K = 6, ≈ 30 minutes at 5-min cadence). Greedy re-selection of the same strike, intra-day blow-out, and inter-tick stacking are all prevented by construction, not by chance. | W-4 |
| **G-13: Post-fill delta within target band** | Every filled CSP records `actual_delta` in a queryable column (not just inside intent_payload). A post-fill check warns or kill-switches if `abs(actual_delta - target_delta) > tolerance` (default tolerance 0.10). Operator gets a Telegram notification within one tick of a fill that lands materially outside intent. | W-9 |

---

## 4. Phased plan

Phases run in parallel where dependencies allow. The numbered IDs are stable references; do not renumber.

### Phase 6: Strategy validation (3-6 weeks of focused work)

The current strategy is plausible. It is not proven. Without backtest evidence, you are operating on intuition. Don't deploy real money on intuition.

#### T-6.0: Resolve test failures and earnings fail-open (PREREQUISITE)
Files: `tests/test_strategy_worker.py`, `src/kai_trader/strategy/earnings.py`, `src/kai_trader/strategy/candidates.py`.

The 5 failing tests assert old "Submitted: 1" behaviour. The new code emits "no expirations in sleeve DTE band". Investigate which is right:
1. Is the code correctly rejecting because no expiration falls inside the DTE band, but tests don't seed valid expirations?
2. Or did the new ranker change which contracts get considered, and tests need to seed the new shape?

**Pick the right behaviour, then make code and tests agree.** Do not let the tests drift further.

While there: change `is_earnings_in_window` to return `True` (= skip the symbol) when the earnings lookup fails, not `False`. Document the choice in the docstring. Add a test that mocks yfinance to return None and asserts the symbol is skipped, not allowed.

Acceptance: `uv run pytest` reports 0 failures. `is_earnings_in_window(symbol)` with a mocked failed lookup returns `True`.

#### T-6.1: Document and verify the multi-factor ranker
File: `src/kai_trader/strategy/candidates.py`, look for the ranker added in commit `5137da5`.

Read the code. Write a docstring (and a section in this plan) describing exactly which factors it scores on, with what weights, and why. Then evaluate: does it include **IV / RV ratio** or **IV rank**? If not, add them. The economic argument: a wheel only earns edge when IV is rich relative to subsequent realised vol. Selling at low IV means you collect small premium, then realised vol exceeds it, and you lose. That's not a strategy; that's giving money away.

Suggested factor set with rationale:
- **Annualised premium yield** (premium / collateral / DTE × 365). Already present.
- **IV30** absolute level. Filter: only consider symbols with IV30 above a regime-dependent floor.
- **IV30 / RV30 ratio**. Hard filter: skip symbols where IV < 1.1 × RV30.
- **IV rank** (current IV30 vs trailing 252d range). Bonus weight when > 50.
- **Bid-ask spread / mid** at the target strike. Filter: skip if > 5%.
- **Recent return** (5d, 20d). Filter: skip if symbol is in the bottom 10% of its trailing-90 day range (don't catch falling knives).
- **Correlation to existing book** (rolling 30d, daily returns). Penalise high correlation to existing positions.
- **Earnings-in-window** (already present, fix per T-6.0).

Acceptance: ranker code has a top-of-function docstring explicitly listing the factors and weights. Each factor has a unit test. There is a config row or constants block where weights can be tuned without code changes.

#### T-6.2: Backtest engine (vectorised, EOD)
New module: `src/kai_trader/backtest/`.

Inputs (acquire as part of this task):
- Historical OPRA chains EOD. **Polygon's options EOD is the realistic source** ($199/mo, 2 years history on the lower tier). Without this, the backtest is fantasy. Document the data acquisition step explicitly in the module README.
- Daily underlying bars (Alpaca historical or Polygon).
- Earnings calendar (Wall Street Horizon, Polygon, or paid alternative).
- VIX EOD (CBOE direct or Polygon).
- Dividends (Polygon).

Architecture:
- `Loader` reads parquet snapshots of chain + bars.
- `Simulator` walks day-by-day, applying the same rules as `worker.py`. It must reuse `candidates.py`, `rolls.py`, `profit_take.py`, `earnings.py` directly, not re-implement them. Otherwise the backtest is testing a different strategy than production runs.
- `CostModel` applies: bid-ask half-spread on entries and exits, $0.65/contract commissions if applicable, exercise/assignment fees, dividend risk on equity assignments.
- `Reporter` produces: equity curve, daily PnL, trade ledger, Sharpe, Sortino, max DD, win rate, profit factor, average winner, average loser, trade-count histogram, per-name PnL breakdown, per-regime PnL breakdown.

**Critical design rule: the backtest must drive `candidates.build_intents` directly with mocked chain/account state, not re-implement the candidate logic.** If you find yourself copy-pasting from `candidates.py` into the simulator, stop and refactor.

Acceptance: `uv run python -m kai_trader.backtest --start 2018-01-01 --end 2025-12-31` produces a report. Report shows trades. Report shows positive risk-adjusted return after costs in at least 3 of the 4 regime windows specified in G-1.

#### T-6.3: Walk-forward parameter optimisation
File: extend `kai_trader.backtest.optimizer`.

Every parameter currently in `sleeve_config` and as a constant in code needs walk-forward optimisation. Specifically:
- `target_delta_risk_on`, `target_delta_neutral` (sleeve_config)
- `dte_min`, `dte_max` (sleeve_config)
- `profit_take_pct` (sleeve_config)
- `roll_trigger_delta` (sleeve_config)
- `target_pct` (sleeve_config)
- `max_new_entries_per_tick` (sleeve_config)
- Drawdown thresholds (`strategy/drawdown.py` constants)
- IV/RV filter thresholds (T-6.1)

Method: 6-month rolling window. Optimise on first 4 months (objective: Sortino), validate on next 2. Report parameter stability across windows. Flag any parameter where optimised value drifts > 30% between adjacent windows — that's a regime-fragile parameter and needs a regime-conditional override or removal.

Acceptance: Markdown report at `docs/backtests/walk_forward_<run_date>.md` with parameter heat-maps and stability commentary.

#### T-6.4: Bootstrap uncertainty bounds
File: extend `kai_trader.backtest.uncertainty`.

Resample trades 1000× to estimate distributions of Sharpe, Sortino, max DD, and CAGR. Report:
- Median + 5th-percentile + 95th-percentile for each metric.
- Probability of MaxDD > 20%, > 30%, > 40%.
- Probability of negative-Sharpe outcomes given the realised trade distribution.

If the 5th-percentile MaxDD is greater than 30%, **the strategy is not ready for the size you intend.** Either reduce sizing, change the rules, or accept the risk explicitly in writing.

Acceptance: bootstrap section in the same report from T-6.3.

#### T-6.5: IV/RV edge verification
File: `kai_trader/backtest/edge_check.py`.

Run a separate analysis: for each historical entry the strategy made (or would have made), record (a) IV at entry, (b) realised vol over the held period. Plot IV minus RV. The mean of this distribution is the strategy's gross edge per trade. If it's not solidly positive after costs, the strategy is not viable, full stop.

Acceptance: dedicated section in the backtest report showing the IV − RV distribution with mean, median, 5th/95th percentiles. Mean must be positive after costs.

#### T-6.6: Regime-conditional parameter sets
After T-6.3 results land, propose three parameter sets (`risk_on`, `neutral`, `risk_off`) per the walk-forward findings. Migrate sleeve_config to support per-regime overrides if needed.

Acceptance: new migration adding `*_risk_on`, `*_neutral`, `*_risk_off` columns where the data shows regime-fragile parameters. Strategy worker reads the right column for the active regime each tick.

---

### Phase 7: Risk management hardening (2-4 weeks)

#### T-7.1: Portfolio Greeks aggregation
New module: `src/kai_trader/risk/greeks.py`. New migration: `019_book_greeks.sql`.

Each tick: walk all open positions, look up current chain quotes, sum delta, gamma, theta, vega across the book. Store a row in `book_greeks` (timestamp, net_delta, net_gamma, net_vega, net_theta, gross_short_premium, gross_long_premium, by_underlying_jsonb).

Read pattern: `/greeks` Telegram command displays current values plus 24h change.

Acceptance: `book_greeks` row appended each tick. `/greeks` returns within 1 second. Unit test verifies aggregation arithmetic against a fixed fake chain.

#### T-7.2: Pre-submit risk gate with hard limits
Files: `src/kai_trader/risk/limits.py` (new), wire into `src/kai_trader/strategy/worker.py` and `src/kai_trader/strategy/candidates.py`.

Before any new entry submission, **simulate** the post-trade book Greeks (sum current Greeks plus the candidate's Greeks). Refuse if any of these limits would be violated:

| Limit | Default value | Rationale |
|---|---|---|
| Net delta | ±0.30 × equity in $-equivalent SPY notional | Bound directional exposure to a 30% SPY-equivalent move scenario |
| Net delta short ceiling | -0.20 × equity | Asymmetric: short-delta in a down move is the catastrophic case |
| Net vega | -$200 / vol point | Wheel is a vol seller; cap how much |
| Net gamma at nearest expiry | < 0.5% of equity per 1% spot move | Pin-risk and gap-risk control |
| Per-name notional (collateral) | ≤ 15% of equity | Single-name blow-up cap |
| Per-expiry notional | ≤ 25% of equity | Concurrent-expiry correlation kill (G-5) |
| Per-sector notional | ≤ 35% of equity | Sector concentration |

Also a **per-day USD loss cap** (default: 3% of equity). On breach, auto-engage `kill_switch` and notify critical.

All limits live in `risk/limits.py` as constants for now; later move to a `risk_limits` table if you want runtime tunability.

Acceptance: A test seeds a book at 14% MARA and tries to add another MARA CSP that would push it to 17%. The submit is refused with `reason="per_name_notional_cap"`. An `orders` row is recorded with `status="skipped_by_risk_limit"`.

#### T-7.3: Tail-risk hedging sleeve
File: new `src/kai_trader/strategy/tail_hedge.py`.

For a wheel writing $35K of short premium, a far-OTM SPX or SPY put as monthly insurance is rationally priced (~0.5-1% of equity / month). On a 2020-style 30% drawdown, the hedge pays out multiples of its cost.

Logic:
- Each month, on the first trading day, check whether a tail hedge is open.
- If not, buy 1 SPY put with delta ≈ -0.05 (~5% OTM) at the next monthly expiration.
- Budget: 1% of equity per month max. Skip if budget exhausted.
- The hedge is **not** rolled or closed by strategy logic. Only the operator closes it manually (via `/close`) or it expires.

Acceptance: a unit test verifies the hedge selection picks the closest contract to delta -0.05 at the right monthly expiry within budget.

#### T-7.4: Position sizing model
File: extend `src/kai_trader/strategy/candidates.py`.

Current sizing: `floor(headroom / strike / 100)`. Better: fixed-fractional Kelly. For each candidate compute `max_loss = (strike - premium) × 100 × qty` and cap qty so `max_loss ≤ 2% of equity`. The 2% number is the conservative pick; T-6.3 walk-forward will tune it.

Acceptance: a candidate that would write a single contract whose `(strike - premium) × 100` already exceeds 2% of equity is rejected with `reason="single_trade_max_loss_cap"`.

#### T-7.5: Roll discipline tier-2
File: `src/kai_trader/strategy/rolls.py`.

Current: roll only on net credit, otherwise hold. Failure mode: when you really need to roll (tested strike, IV spiked), there is often no net-credit candidate further OTM, so the position is held into deeper trouble.

Add a tier-2 rule: if delta crosses a configurable `force_close_delta` (default 0.55), close the position at market regardless of credit. Defined max loss is better than discovering 2,000 shares of MARA at $11.50 on Friday close.

This is a behaviour change that needs a flag (`force_close_on_high_delta_enabled`) and a default-off rollout. Enable after backtest confirms it improves expected-value, not just psychology.

Acceptance: a test confirms a position with delta 0.60 triggers a close intent when the flag is on, and is held when the flag is off.

#### T-7.6: Wheel completion policy
File: `src/kai_trader/strategy/covered_calls.py`.

Document and enforce the policy for assigned shares:
- Initial CC strike: ≥ cost basis of the assigned shares (don't lock in losses).
- Roll-up: if stock rallies through the CC strike, roll up at the next monthly to a strike ≥ stock price (capture some upside, don't lose shares cheaply).
- Roll-out: if the CC is ITM at expiry but the operator doesn't want to lose shares, roll out one expiration cycle for net credit.
- Termination: if shares have been wheeled for > N weeks without closing the original cost basis, reassess (this is a notification, not an auto-action).

Acceptance: covered call selection has unit tests for: (a) refusing strikes below cost basis, (b) selecting roll-up strike above current spot.

#### T-7.7: Stair-step drawdown protocol
File: `src/kai_trader/strategy/drawdown.py`.

Current: -7% rolling 7d → kill_switch on. Stair-step it:

| Drawdown | Window | Action |
|---|---|---|
| -3% | intraday | halt new entries for 60 minutes |
| -5% | rolling 7d | halve sizing; tighten profit-take to 60% credit captured |
| -7% | rolling 7d | kill_switch on (current) |
| -10% | rolling 30d | kill_switch on; require manual `/reset_drawdown` from operator with written rationale |

Each tier writes a `drawdown_event` row and fires a critical notification.

Acceptance: backtest replay of a 2020 March week triggers tier escalation in the right order and the right notifications fire.

#### T-7.8: Live-capital sentinel and per-period caps
Files: new flag in `system_flags`, wire into `src/kai_trader/broker/alpaca.py` submitters.

New flag `live_capital_enabled` (default false). When false **and** `ALPACA_PAPER=false`, every submitter refuses with `reason="live_capital_disabled"`. This is a belt over the suspenders so flipping `ALPACA_PAPER=false` without flipping `live_capital_enabled=true` does not accidentally trade.

Add per-period USD caps (configurable, defaults conservative):
- `max_loss_per_day_usd`: 3% of starting-day equity
- `max_loss_per_week_usd`: 8% of starting-week equity
- `max_premium_collected_per_day_usd`: 3% of equity (controls how fast new exposure is added)

Each submitter checks the daily totals against these caps before submitting. Breach = refuse + critical notification.

Acceptance: when `live_capital_enabled=true` and the daily-loss cap has been breached, a new entry submission is refused with `reason="daily_loss_cap_breached"`. An audit row is written.

---

### Phase 8: Operational reliability (2-3 weeks)

#### T-8.1: Persistent state for `_pending` and in-flight orders
Files: new migration `020_pending_close_state.sql`, refactor `src/kai_trader/bot/handlers/close.py`.

Move the in-process `_pending` dict to a `pending_close` table with `(user_id, symbol, staged_at, ttl_seconds, status)`. On bot start, load fresh entries (within TTL) into memory cache. On every stage/consume, write through to Postgres.

In-flight orders: every intent that has been submitted to Alpaca but not yet reconciled has an `orders` row with `status='submitted'`. Confirm the existing reconciler correctly handles a process restart that leaves an order in the broker but not in our DB. Add a startup job that pulls open orders from Alpaca and reconciles against the DB.

Acceptance: kill the bot during a tick where a submission has gone to Alpaca but not been reconciled. Restart. Confirm the next reconciliation pass picks up the order and updates the DB row.

#### T-8.2: Order reconciliation as authoritative source
File: `src/kai_trader/strategy/worker.py` (existing `_reconcile_pending`).

Treat broker state as source of truth. Each tick: pull every open order and every position from Alpaca, compare against DB, fix divergences, alert on anomalies (e.g., a position in Alpaca that has no matching DB record).

Acceptance: a manual `/reconcile_now` Telegram command triggers a full reconciliation and reports any divergences found.

#### T-8.3: `/metrics` and observability surface
Files: new `src/kai_trader/bot/handlers/metrics.py`, new module `src/kai_trader/observability/`.

Telegram command `/metrics` returns:
- Last-tick timestamp (and seconds since)
- Current Greeks (delta, gamma, vega, theta)
- Today's PnL, week's PnL
- Today's trades (count, gross premium collected, gross losses realised)
- Drawdown vs 7d-high, 30d-high
- Open positions count by underlying
- Process memory, CPU (read /proc on Linux)

Plus structured log shipping to a real log store (Render → Logflare, Better Stack, or Datadog). Sentry for unhandled exceptions (Sentry MCP exists).

Acceptance: `/metrics` returns within 2 seconds. Logs are queryable in the chosen log store. Sentry receives a test exception.

#### T-8.4: Two-channel critical alerting
Files: extend `src/kai_trader/notifications/`.

Telegram is the routine channel (already done). Add a second channel for criticals only. Options:
- **Twilio SMS** (cheapest, ~$0.01/msg, requires phone number).
- **PagerDuty** (free tier supports 1 user, 1 escalation policy).
- **Email via SES** (cheap, slower).

Pick one. Wire the existing `notifications.priority='critical'` path to also send via this second channel, in addition to Telegram.

Critical events that must fire on both channels:
- `kill_switch` engaged
- Drawdown threshold tripped
- Broker error rate > 5/min for > 5 min
- Last tick > 30 min ago (heartbeat alarm)
- Unexpected assignment notification
- `live_capital_enabled` toggled

Acceptance: triggering kill_switch via `/kill` results in both a Telegram message and the chosen second-channel alert within 30 seconds.

#### T-8.5: Render plan upgrade or migration off Render
Decision needed. Options:
- **Bump Render to Standard** (~$25/mo, 2GB). Simplest. Same broken auto-deploy webhook.
- **Move to a VPS** (Hetzner CX22 ~€5/mo, 4GB; or DigitalOcean $6/mo droplet). SSH, real debugging, full control. More ops overhead.

Either way, profile memory usage first to find what was OOM'ing. Run the bot for 48h with `tracemalloc` snapshots every hour, check for leaks. The OOM might be a leak, not just a resource ceiling.

Acceptance: 14 consecutive days with zero OOM events on the chosen runtime.

#### T-8.6: Restart-safe semantics
Add a chaos test: kill -9 the bot at random intervals during a paper trading session. Confirm:
- No partial state in `orders` table (every row is either fully reconciled or marked for re-reconciliation).
- No staged closes lost (after T-8.1 lands).
- Notification queue resumes from where it left off.
- Trading-stream reconnects cleanly.

Acceptance: documented test procedure in `docs/runbooks/restart_chaos_test.md` plus a recorded test run output.

#### T-8.7: Operator runbooks
Path: `docs/runbooks/`.

Required runbooks (at minimum):
- `bot_wont_start.md`
- `broker_5xx_storm.md`
- `last_tick_too_old.md`
- `unexpected_assignment.md`
- `kill_switch_stuck_on.md`
- `staged_close_lost.md`
- `oom_crash.md`
- `live_capital_enable_checklist.md` (the most important one)
- `restart_chaos_test.md` (from T-8.6)
- `daily_review_checklist.md`
- `weekly_review_checklist.md`

Each: symptoms, diagnosis steps with copy-pasteable commands, fix, prevention follow-up. Treat the runbooks as code — review them after any incident.

Acceptance: runbooks committed to repo. The `live_capital_enable_checklist.md` is read aloud by the operator before flipping `live_capital_enabled=true` for the first time.

#### T-8.8: Deploy reliability
Three options:
1. Fix the GitHub → Render webhook (talk to Render support; possibly delete and re-create the webhook).
2. Use GitHub Actions to call Render's deploy API on push to the deploy branch (deterministic, bypasses the webhook).
3. Accept the manual step and document it.

Pick one. Recommend option 2 — it's cheap and removes a flaky dependency.

Acceptance: 5 consecutive pushes to the deploy branch result in 5 deploys without manual intervention.

#### T-8.9: Edited-message handling in Telegram bot
File: `src/kai_trader/bot/main.py`.

By default python-telegram-bot's `MessageHandler` does not process `edited_message` updates. Confirm whether `CommandHandler` is. The triple-response observed during the diagnostic suggests something is processing edits. Either suppress edited-message processing across the board (add filter) or explicitly handle it idempotently.

Acceptance: editing a `/close X` to `/close Y` results in exactly one bot response, for `Y`.

---

### Phase 9: Live capital ramp protocol

Don't go from $100K paper to $100K live. Stage it:

| Stage | Live capital | Paper alongside | Min duration | Promotion criteria |
|---|---|---|---|---|
| **A** | $5,000 | $100K | 4 weeks | All Tier-0 hard gates green; no kill_switch trips; live equity tracks paper within ±15%/week; weekly review documents written. |
| **B** | $15,000 | $100K | 4 weeks | Stage A all-clear; live Sharpe within 0.5σ of paper Sharpe; zero manual operator interventions in execution path. |
| **C** | $40,000 | $100K | 8 weeks | Stage B all-clear; at least one regime transition observed live; tail hedge functioning (i.e., monthly purchase happened on schedule). |
| **D** | $100,000+ | $100K shadow | ongoing | Stage C all-clear across at least one risk-off window. |

Rules during the entire ramp:

1. **Identical code on live and paper.** Same git commit, same DB. Only `ALPACA_PAPER` and `live_capital_enabled` differ. (Practical implementation: two Render services pointing at the same image and DB but different env vars, or two Python processes with different config.)
2. **Daily reconciliation.** Every evening, diff what live did vs. what paper would have done given identical inputs. Document divergences. Acceptable divergences: bid-ask fills, micro-timing of limit fills. Unacceptable divergences: different intents generated, different gating decisions.
3. **Weekly written review.** New file `docs/reviews/yyyy-mm-dd.md`. Wins, losses, surprises, parameter drift candidates, anomalies, decisions.
4. **Pending-changes for everything.** No parameter, threshold, or universe change ships without a `pending_changes` row, an approval, and a `decision_log` entry.
5. **Pre-promotion checklist.** Before each ramp stage, the `live_capital_enable_checklist.md` runbook is executed end-to-end and signed off in writing.

Stages are not time-boxed by maximum, only minimum. If Stage A reveals problems, you stay at Stage A until they're fixed and the clock resets.

---

## 5. Concrete this-week tasks (in priority order)

These are the tasks to do **first**, before any of the larger phase work. Each is a small, high-leverage, well-scoped chunk. Execute in this order.

### W-1: Fix the earnings filter to fail closed (1-2 hours)
- File: `src/kai_trader/strategy/earnings.py`.
- Change `is_earnings_in_window(symbol)` such that when the underlying lookup raises or returns None, the function returns `True` (= treat as earnings present, skip the symbol).
- Update the docstring to state the fail-closed posture.
- Add a unit test: mock `_fetch_next_earnings_date` to raise; assert `is_earnings_in_window("AAPL", dte_days=7)` returns `True`.
- Add a unit test: mock to return None; same assertion.
- Run `uv run pytest tests/test_strategy_earnings*` — must be green.
- Commit message: `fix: earnings filter fails closed on data unavailability`.
- Resolves: F-3, G-2.

### W-2: Make MAX_CONTRACTS_PER_SYMBOL cumulative against existing positions (2-3 hours)
- Files: `src/kai_trader/strategy/candidates.py` (the `_max_qty_for` function at line 317-336 and its callers).
- The constant `MAX_CONTRACTS_PER_SYMBOL = 10` at line 32 is documented as a hard ceiling but only applies per build call. Production observation: MARA reached 30 contracts and SNAP 40 contracts because successive ticks each filled up to the cap.
- Pass the symbol's existing held contract count into `_max_qty_for` (or via the broader build context). Compute `remaining_contract_headroom = max(0, MAX_CONTRACTS_PER_SYMBOL - existing_qty)`. Cap the returned qty by `remaining_contract_headroom`.
- The existing held count can be derived from the same `existing_short_puts` list already passed to `build_intents_with_diagnostics`. Sum `abs(qty)` for positions whose underlying matches the candidate's underlying.
- New diagnostic counter: `symbols_skipped_for_contract_ceiling`. Surface in tick summary.
- Tests:
  - Existing 8 contracts of MARA, candidate yields 5 → reduce to 2 (10 - 8).
  - Existing 10 contracts of MARA, candidate yields anything → return 0.
  - No existing → return min(qty, 10) (current behaviour preserved).
- Commit message: `fix: per-symbol contract ceiling counts existing held positions`.
- Resolves: F-12 (MARA / SNAP over-accumulation), G-11 (partial).

### W-3: Tighten per-symbol $ cap with strike-aware floor (2-4 hours)
- Files: `src/kai_trader/strategy/candidates.py` (`per_symbol_cap_pct` at lines 39-58, callers in `build_intents_with_diagnostics`).
- The existing tiered cap (60% for $50K-$150K accounts, 30% for $150K-$500K, 15% over) is strike-blind. On low-priced names like MARA at $11.50 the 60% cap allows 52 contracts; on SNAP at $6 it allows 100.
- Add a hard `PER_NAME_NOTIONAL_CAP_PCT = 0.15` ceiling that applies regardless of equity tier. The current tiered system can stay but must always cap at 15% as the upper bound.
- Pre-submit check (after the existing cap math, before emitting an intent): compute the symbol's existing collateral. If `existing + new_intent_collateral > equity * 0.15`, reduce qty (or skip if even 1 contract breaches).
- New diagnostic counter: `symbols_skipped_for_per_name_dollar_cap`. Surface in tick summary.
- Tests:
  - $100K equity, MARA already at $13K (13%), candidate adds 1 contract worth $1.15K → allowed (would be 14.15%).
  - $100K equity, MARA already at $14K (14%), candidate adds 1 contract worth $1.15K → reject (would be 15.15%).
  - $100K equity, no MARA position, candidate would add 30 MARA $11.50 contracts ($34.5K) → reduce qty to 13 (which is $14.95K, still under the cap).
- Commit message: `feat: enforce 15% per-name notional cap regardless of strike`.
- Resolves: F-13, G-11 (full).

### W-4: Per-tick + per-day deployment caps and per-symbol cool-down (4-6 hours)
- Files: `src/kai_trader/strategy/candidates.py`, possibly new `src/kai_trader/strategy/cooldown.py` and `src/kai_trader/strategy/deployment.py`, `src/kai_trader/db/orders.py` for cool-down and daily-deployment lookups.
- Three related fixes:
  1. **Per-tick total-deployment cap.** Sum the total new collateral across all candidate intents in a single tick. Cap at `PER_TICK_DEPLOYMENT_CAP_PCT * equity` (default 0.10, i.e. 10% of equity). If the cap is exceeded, drop lowest-ranked candidates first until under.
  2. **Per-day new-deployment cap.** Sum new collateral committed since UTC midnight by querying `orders` for rows where `submitted_at >= today_utc_midnight` and `action in ('open_short_put', 'open_covered_call')` and `status in ('submitted', 'filled')`. Cap the running daily total at `PER_DAY_NEW_DEPLOYMENT_PCT * equity` (default 0.30, i.e. 30% of equity in new short premium per UTC day). If a candidate would push the day total over the cap, reduce qty (or drop candidate).
  3. **Per-symbol cool-down.** A symbol entered (filled or submitted) in the last `COOLDOWN_TICKS` ticks (default 6, ≈ 30 minutes at 5-min cadence) is excluded from candidate selection.
- Implementation notes:
  - Compute today-deployment from `orders.submitted_at` (no new table needed — orders is the single source of truth). Add an `orders` index on `(submitted_at desc, action)` if query latency is poor; verify with `explain analyze` first before adding.
  - Cool-down lookup: query `orders` for the symbol's latest `submitted_at` and compare to now. Same `orders.submitted_at` index works.
  - Both the per-tick and per-day caps must be checked **after** the per-name caps (W-2, W-3) so a single name doesn't blow either velocity bucket.
  - Day boundary is UTC midnight to keep the cap deterministic across timezones; the operator's weekly review should be timezone-aware separately.
- Diagnostic counters: `intents_dropped_for_per_tick_cap`, `intents_dropped_for_per_day_cap`, `symbols_skipped_for_cooldown`. Surface in tick summary along with current `today_deployment_used_pct` and `today_deployment_remaining_usd`.
- Tests:
  - **Per-tick cap**: 5 candidates each $5K, equity $100K → cap 10% = $10K → top 2 candidates pass, rest dropped with `intents_dropped_for_per_tick_cap`.
  - **Per-day cap, fresh book**: equity $100K, no orders today → first tick allowed up to $10K (per-tick cap binds), but cumulative across the day cannot exceed $30K. Seed `orders` with $25K already committed today, candidate set worth $10K → only $5K allowed through.
  - **Per-day cap, day rollover**: seed `orders` with $40K committed yesterday (before today UTC midnight). Today's first tick should see day total = 0, full $10K per-tick allowed, $30K per-day allowed.
  - **Cool-down**: symbol entered 3 ticks ago, cool-down = 6 ticks → skipped. Symbol entered 7 ticks ago → eligible.
  - **All three together**: equity $100K, $25K already today, 5 candidates each $5K, one is in cool-down. Cool-down candidate dropped first; per-day cap clamps the rest to $5K total = 1 candidate; per-tick cap is non-binding.
- Commit message: `feat: per-tick and per-day deployment caps with per-symbol cool-down`.
- Resolves: F-14 (velocity, both per-tick and per-day), F-15 (greedy re-selection), G-12.

### W-5: Persist `_pending` close state to Postgres (4-6 hours)
- New migration `020_pending_close_state.sql`:
  ```sql
  create table pending_close (
      id bigserial primary key,
      user_id bigint not null,
      symbol text not null,
      staged_at timestamptz not null default now(),
      ttl_seconds int not null default 30,
      status text not null default 'staged',
      created_at timestamptz not null default now(),
      consumed_at timestamptz
  );
  create index pending_close_active_idx on pending_close (user_id, symbol)
      where status = 'staged';
  ```
- New helper: `src/kai_trader/db/pending_close.py` with `stage`, `consume`, `cleanup_expired`.
- Refactor `src/kai_trader/bot/handlers/close.py` to use the DB-backed helpers. Keep an in-memory cache for read latency, but Postgres is source of truth.
- On `bot/main.py` startup, run `cleanup_expired` once.
- Tests: existing close handler tests need to mock the new helpers. Bot restart simulation: stage, kill cache, restart, confirm — assert the consume succeeds.
- Commit message: `feat: persist /close staged state to Postgres for restart safety`.
- Resolves: F-7, G-8 (partial — full state persistence still needs in-flight orders).

### W-6: Fix or update the 5 failing strategy worker tests (2-4 hours)
- File: `tests/test_strategy_worker.py`.
- Run each failing test individually. Read the actual production tick output, compare to assertion. Decide per test: is the production behaviour wrong (fix code), or has behaviour intentionally changed (update assertion)?
- For `test_tick_submits_when_flags_green`: production reports "no expirations in sleeve DTE band" with the seeded fake chain. Likely the test fixture seeds a chain that doesn't include any expiration inside the new DTE filter. Update the fixture to include a contract at the right DTE.
- Apply the same diagnostic rigor to the other 4 failing tests.
- Run `uv run pytest` — must be 0 failures.
- Commit message: `test: align strategy worker tests with post-5137da5 ranker behaviour`.
- Resolves: F-5, G-6.

### W-7: Memory profile + Render plan decision (1-2 hours, mostly waiting)
- Add `tracemalloc.start()` at bot startup.
- Add a periodic snapshot every hour that logs the top 20 allocations (file:line, size).
- Deploy. Let it run 48h.
- Read the snapshots: is there a leak (steady growth) or a baseline bigger than 512MB?
- Decision: if leak, fix the leak. If baseline, bump Render plan to Standard ($25/mo).
- Commit and document.
- Resolves: F-4, G-3 (begins the 14-day clean-run window).

### W-8: Document the multi-factor ranker + add IV/RV hard filter (3-5 hours)
- File: `src/kai_trader/strategy/candidates.py` (the ranker code added in commit `5137da5`).
- Read the existing ranker. Write a top-of-function docstring listing the factors and weights.
- Add an `iv_rv_ratio_min` constant (default 1.10). For each candidate, compute IV30 from the chain and RV30 from `market_data.get_daily_bars`. Reject if ratio < threshold.
- Diagnostic counter: `symbols_skipped_for_iv_rv_floor`. Surface in tick summary.
- Test: candidate where IV30 = 0.30, RV30 = 0.35 (ratio 0.86) is rejected. Where IV30 = 0.40, RV30 = 0.30 (ratio 1.33) is allowed.
- Commit message: `feat: enforce IV/RV >= 1.10 floor on entry selection`.
- Resolves: F-11 (partial; T-6.5 completes the edge verification).

### W-9: Post-fill delta verification (3-4 hours)
- Files: new migration `021_orders_actual_delta.sql`, `src/kai_trader/db/orders.py`, `src/kai_trader/strategy/worker.py` (the reconciliation path), possibly a new `src/kai_trader/strategy/post_fill_check.py`.
- Migration adds `actual_delta numeric` and `target_delta numeric` columns to `orders` (queryable, indexed if needed).
- When an intent is recorded, write `target_delta` from the regime's target.
- When a fill is reconciled (`_reconcile_pending` in `worker.py`), look up the filled contract's current delta from the chain (or use the chain snapshot at fill time if cached); write to `actual_delta`.
- Define `DELTA_TOLERANCE = 0.10` constant. After each reconciliation, if `abs(actual_delta - target_delta) > DELTA_TOLERANCE`, enqueue a `notifications` row at priority `warning` with the symbol, target, actual, and order id. Multiple breaches in one tick get batched into one notification.
- Tests: target -0.40, actual -0.45 → no warning (within 0.10). Target -0.40, actual -0.55 → warning enqueued.
- Commit message: `feat: post-fill delta verification with warning on out-of-band fills`.
- Resolves: F-16, G-13.

After W-1 through W-9, the strategy has cumulative caps, velocity limits, cool-downs, fail-closed earnings, persisted state, fixed tests, IV/RV-conditioned selection, and delta verification. **None of this is sufficient for live capital** — that's what Phases 6-9 are for. But this clears the highest-ROI defects from the immediate state and closes the over-allocation failure mode that produced the MARA-30 / SNAP-40 incident.

---

## 6. Acceptance criteria template

For each task above, the bar is:
1. Code change has a top-of-function or top-of-module docstring explaining intent and any non-obvious decisions.
2. Unit tests added covering the happy path and at least one edge case.
3. `uv run ruff check` clean.
4. `uv run mypy --strict src/` clean (the benign `unused section(s): module = ['tests.*']` note is OK).
5. `uv run pytest` zero failures (this requires W-6 to land first; until then, expected failures are documented in the commit message).
6. Conventional commit message.
7. **For any change that touches the broker submitters or the strategy entry path: a manual paper test.** Send a `/trade_now` from Telegram, confirm the change took effect as intended, attach the Telegram screenshot to the PR or commit description.

---

## 7. Anti-patterns to avoid

These are specific, real anti-patterns you might be tempted toward. Don't.

1. **Don't widen the symbol whitelist without backtest evidence.** The current 30-name pool was already a deliberate choice with a written rationale (`migration_018`). Adding names should follow the same path: rationale, backtest, migration.
2. **Don't bypass the gating triad for "convenience".** `kill_switch`, `trading_enabled`, `new_entries_enabled`. Every submitter checks them. No exceptions for "just this once."
3. **Don't add config without a migration.** Sleeve config and risk limits live in the DB so the operator can change them without a redeploy. Constants in code are for things that genuinely should require a deploy to change.
4. **Don't return None from a function that should return a value, just to "be safe".** Make the failure mode explicit. Either raise, or return a typed result with a `reason` field, like `SubmitResult` does.
5. **Don't `print`.** `structlog` via `kai_trader.logging.get_logger`.
6. **Don't put em-dashes in code, comments, docs, or commit messages.** Periods, commas, colons.
7. **Don't catch broad exceptions silently.** If you catch, you log structured, you re-raise or you return an explicit error result. Silent swallow is how the OOM mystery from April 30 became a mystery.
8. **Don't ship a parameter change without an entry in `pending_changes` once the approval flow is in active use.** The audit trail is the strongest defence against parameter drift.
9. **Don't trust the auto-deploy webhook.** Always verify a deploy event appeared in Render after a push. Manual fallback documented above.
10. **Don't deploy untested live code paths to live capital.** If a code path has only ever run on paper, it's an unknown when it touches real money.

---

## 8. Reference: external dependencies and accounts needed

For the full plan to land, the operator needs to acquire/configure:

| Dependency | Use | Approx cost | Where |
|---|---|---|---|
| **Polygon options EOD** | Backtest data (T-6.2) | $199/mo (lower tier) | https://polygon.io/options |
| **Wall Street Horizon** *(or alt)* | Earnings calendar (T-6.0/G-2) | ~$50-100/mo | https://wallstreethorizon.com |
| **Render Standard plan** *(if staying on Render)* | Memory headroom (T-8.5/G-3) | $25/mo | dashboard |
| **Sentry** | Exception monitoring (T-8.3) | Free tier OK | Sentry MCP available |
| **Twilio SMS** *(or PagerDuty)* | Critical alerts (T-8.4/G-9) | $0.01/msg or free tier | Twilio dashboard |
| **CBOE VIX** *(direct, optional)* | More reliable VIX feed than yfinance | Free historical, paid live | https://www.cboe.com |

---

## 9. Where to start (execution order for the new Claude instance)

If you are picking this up fresh:

1. **Read `CLAUDE.md`, `TRACKER.md`, and this document end-to-end.**
2. **Run the test suite as-is.** Confirm you see exactly the 5 known failures from `test_strategy_worker.py`. If you see more, something else broke and you need to investigate before doing any new work.
3. **Sync the deploy branch.** `git fetch origin`, confirm `claude/kai-trader-phase-1-sHFJk` matches `main`. If not, ask the user before pushing.
4. **Pick W-1 (earnings fail-closed).** Smallest, highest-leverage. Execute end-to-end (read code, plan change, write code, write test, run tests, commit).
5. **Push and verify deploy.** `git push origin main:claude/kai-trader-phase-1-sHFJk`. Watch Render. If no deploy event appears in 30 seconds, ask the user to hit Manual Deploy.
6. **Confirm paper behaviour.** Send `/strategy_status` from Telegram. If the change is observable in the tick summary, screenshot.
7. **Repeat for W-2 through W-9** in order.
8. **Once W-1..W-9 are done, plan Phase 6 work.** Phase 6 is large enough that it should be its own sub-plan with the user.

The user can interrupt at any time and shift priorities. When they do, **stop, ask what they want, then proceed.** Do not assume the existing plan is canonical if the user is asking about something else.

---

## 10. Glossary (because precision matters)

- **CSP**: cash-secured put. Sell a put, hold cash equal to (strike × 100 × qty) as collateral.
- **CC**: covered call. Sell a call against shares you own.
- **OCC symbol**: option contract identifier in OCC format, e.g. `AMZN260506P00250000` = AMZN, 2026-05-06, Put, $250.00 strike.
- **DTE**: days to expiration.
- **Delta**: dollar change in option value per $1 change in underlying.
- **Gamma**: change in delta per $1 change in underlying. Highest near the strike, near expiry.
- **Vega**: change in option value per 1 vol-point change in implied volatility.
- **Theta**: time decay per day, dollar value.
- **IV**: implied volatility.
- **RV**: realised volatility (computed from historical returns, typically 30-day annualised).
- **IV rank**: current IV percentile against trailing 252-day range.
- **The wheel**: sell CSP → assigned → sell CC against shares → called away → repeat.
- **Pin risk**: risk of an option settling exactly at strike on expiry, leaving you uncertain whether you'll be assigned.
- **Fail-closed**: on data/system unavailability, default to the safe (no-trade) action.
- **Fail-open**: on data/system unavailability, default to the unsafe (trade-anyway) action. Anti-pattern in trading systems.
- **Sleeve**: a risk bucket within the book, with its own allocation cap and parameters.
- **Reg-T margin**: standard retail margin (Alpaca paper currently). 2× equity buying power.
- **Portfolio margin**: SPAN-based margin available > $125K accounts. ~6× capital efficiency on options. Out of scope until book grows.
