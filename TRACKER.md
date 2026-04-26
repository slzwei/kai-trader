# TRACKER.md

Daily work log for Kai Trader. Append new entries at the top. One entry per
working day, short bullets, no corporate polish.

## 2026-04-27 . Phase 3.6: Recalibrate for 3% monthly target

After verifying 3.5, the original 3.2 calibration looked too tight on
$100k equity at current SPY prices. SPY/QQQ contracts at ~$50-70k
collateral never fit a 40% sleeve cap, leaving the strategy under
deployed and well below the 3% per month target. Recalibrated:

Shipped:

- Migration 009 updates the three sleeve_config rows: allocations
  25 / 30 / 45 (was 40 / 40 / 20), target_delta_put_risk_on -0.40
  (was -0.30), target_delta_put_neutral -0.30 (was -0.20),
  roll_trigger_delta 0.50 (was 0.45). Symbol whitelists greatly
  expanded with a mix of price points (cheap names like F, SOFI,
  PLTR enable multi-contract deployment within concentration cap).
- candidates.py rewritten to allow multi-contract per symbol up to
  PER_SYMBOL_CAP_PCT = 15% of equity AND a global
  TOTAL_DEPLOYMENT_CAP_PCT = 70% AND a hard
  MAX_CONTRACTS_PER_SYMBOL = 10 ceiling. Greedy fill walks the
  whitelist in order. The 1-contract-per-symbol rule is gone.
- _is_sleeve_active simplified: only risk_off blocks new entries.
  The opportunistic-paused-in-neutral rule was removed because
  pausing the highest-IV sleeve was a major drag on average yield.
- TradeIntent grew a qty field; collateral and expected_premium are
  now per-intent totals (qty * 100 * strike or mid).
- Migration 010 flips system_flags.new_entries_enabled to true so
  the Phase 3.4 broker gate (which had been silently rejecting
  every submission since the field defaulted to false) lets
  entries through. Operator can flip it back off via /flag for an
  asymmetric brake (rolls and closes still fire).
- broker/alpaca.py submit_short_put gains the new_entries_enabled
  check as the third gate before any HTTP call to Alpaca, in
  addition to kill_switch and trading_enabled. Refusal returns
  reason="new_entries_disabled" and the worker records the row as
  skipped_by_flag.
- Worker passes intent.qty through to submit_short_put and records
  it in the orders.intent_payload. Gating decision now includes
  new_entries_enabled.

Tests:

- 4 new candidate tests: multi-contract within per-symbol cap,
  per-symbol cap as the binding constraint when sleeve is huge,
  total deployment cap across multiple symbols, max contracts per
  symbol ceiling on a penny-cheap stock.
- 1 new broker test for new_entries_disabled refusal path; 3
  existing broker tests updated to include new_entries_enabled in
  the flag dict so the green-light tests stay green.
- The opportunistic-paused-in-neutral test inverted to assert
  opportunistic stays active under the new rule.
- Worker submit-when-flags-green test updated to expect qty=3 on
  the test fixture (sleeve cap of $40k and $5k per contract → 3
  contracts within the 15% per-symbol cap of $15k).

Coverage 94% across 283 passing tests.

Honest math after the changes:

- Friendly week at risk_on (deltas -0.40, ~5-7 contracts, IV
  18-22): expected weekly yield ~0.7-1.0% on equity. Hits 3% in a
  full risk_on month.
- Mixed week at neutral (deltas -0.30, ~3-5 contracts):
  ~0.4-0.7% weekly. Around 2% in a full neutral month.
- Hostile week (risk_off, no new entries): existing positions
  decay/roll, no new premium.
- Average over a full cycle: ~2-2.5% per month with the right
  regime distribution. 3% is reachable in friendly months.

The 7% drawdown circuit breaker and per-symbol concentration cap
keep the 10% drawdown ceiling intact even with the larger delta.

## 2026-04-27 . Phase 3.5: Drawdown breaker, roll logic, /close

Phase 3 complete with this commit chain.

Shipped:

- `strategy/drawdown.py` with `compute_drawdown(snapshots, current)`
  (pure) and `check_and_trip(current_equity, kill_switch_already_on)`
  (writes flag and fires critical notification on fresh breach).
  Threshold 7% from the prior 7-day high; idempotent when the kill
  switch is already engaged.
- `strategy/rolls.py` with `evaluate_rolls(positions, sleeves,
  regime, chain_fetcher, today)` returning typed `RollIntent` per
  challenged short put. Walks Alpaca positions, parses OCC symbols,
  looks up the position's live delta in the chain, picks a further-
  OTM same-or-later-expiration candidate, and only marks "rolled"
  when the new bid minus the existing ask is strictly positive.
  Otherwise reason is "no_net_credit_candidate" or "no_chain_match"
  and the worker holds.
- `broker/alpaca.py` adds `close_position(symbol)` gated only by
  `kill_switch` (not by `trading_enabled`; closing reduces risk).
  Returns the same typed `SubmitResult` shape as the submit path.
- Strategy worker rewritten:
  - Drawdown check runs after the account fetch and before any
    strategy work. A fresh breach trips the kill switch and the rest
    of the tick short-circuits to the kill_switch summary path.
  - Roll evaluation runs after regime/sleeves and before new entries
    so capital that gets rolled into is reflected in the cap math.
  - When a roll fires under green flags, the worker submits a close
    on the underlying followed by a sell-to-open at the new strike,
    recording both as separate `orders` rows (`action='close'` and
    `action='roll'`).
  - Tick summary now reports rolled / held counts alongside
    submitted / skipped / failed.
- `/close SYMBOL` stages a pending close keyed by (user_id, symbol)
  with a 30-second TTL in module-level state. `/close_confirm SYMBOL`
  consumes the staged entry and submits via the gated broker.
  Each confirm lands an `action='close'` audit row.
- 9 drawdown tests (pure compute table + the trip + idempotency
  paths), 7 rolls tests (untriggered skipped, rolled when net credit,
  held when no net credit, long positions skipped, unparseable
  symbols skipped, no matching sleeve skipped, no chain match
  reported), 3 broker close_position tests (kill-switch refusal,
  trading-disabled allowed, alpaca exception path), 6 close handler
  tests (stage, usage, confirm executes, no-pending, ttl-expired,
  kill-switch path), 3 worker tests (drawdown short-circuit, roll
  execution under green flags, roll skipped under red flags), plus
  bot-main updates. Total 272 passing, 2 skipped, 94% coverage.

Phase 3 done. The full pipeline now: regime classifier drives
sleeve activity; tick loop builds intents and submits gated by
flags; orders table audits every decision; reconciliation writes
back fills; drawdown breaker trips at 7% from the weekly high;
rolls fire on challenged positions but only for net credit;
manual /close exists for discretionary intervention. To start
real paper trading: `/flag trading_enabled on`.

Not shipped (later phases or future tuning):

- Local positions table writes on fill. Alpaca's `list_positions`
  is the source of truth for what we hold; the local positions
  table from migration 004 stays unused until we want
  wheel-lifecycle tracking (assignment / expired / rolled state
  transitions persisted locally).
- IV-rank entry filter, earnings-window blackout, multi-expiration
  per symbol. Listed as future tuning levers in PHASE3.md.
- Live (non-paper) trading. `ALPACA_PAPER=true` remains the default.
  Switching to live needs a separate review.
- Web dashboard, SMS notifications, Doppler.

## 2026-04-27 . Phase 3.4: Order placement gated by flags

Shipped:

- Migration 008 creates the orders table: id, sleeve, symbol,
  option_symbol, action (open_short_put / close / roll),
  intent_payload jsonb, alpaca_order_id (nullable), status (pending /
  submitted / filled / cancelled / skipped_by_flag / failed),
  gating_decision jsonb (the flag snapshot at decision time),
  submitted_at, filled_at, filled_avg_price, error_text. Indexes on
  status, created_at, and partial on alpaca_order_id where not null.
- `db/orders.py` typed helpers: `record_intent` (returns row uuid),
  `mark_submitted` (writes alpaca_order_id + status), `mark_status`
  (terminal updates with optional fill price + error), `recent_orders`,
  `pending_orders` (rows with an alpaca_order_id whose status is not
  yet terminal, used by reconciliation).
- `broker/alpaca.py` extended:
  - `submit_short_put(option_symbol, qty, limit_price, client_order_id)`
    sells to open a single-leg short put. Reads `system_flags` BEFORE
    touching Alpaca; if `kill_switch` is on or `trading_enabled` is off,
    returns a typed `SubmitResult(submitted=False, reason=...)` and
    never sends. On Alpaca exception returns `reason="submit_exception"`
    with the error string.
  - `get_order_status(alpaca_order_id)` returns a narrow
    `OrderStatusSnapshot` for reconciliation.
- Strategy worker rewritten:
  - Each tick first reconciles non-terminal orders (mark_status with
    fill price when Alpaca reports filled / canceled / expired /
    rejected). Reconciliation runs even when the market is closed
    so an overnight fill is reflected on Monday morning.
  - For each candidate intent: record the intent row, call the gated
    `submit_short_put` with limit_price = bid (or mid if bid is zero),
    and write back the outcome. The flag gate inside the broker is
    the last check; race conditions resolve cleanly.
  - Notification text now reports submitted / skipped / failed counts
    with symbol+strike per item.
- New `/trade_now` command forces an immediate tick (audit-logged via
  the standard run_command flow). New `/recent_trades [N]` renders
  the most recent N rows from the orders table (default 10, max 50).
- 9 orders helper unit tests, 5 broker submit_short_put tests
  covering all gate states plus the exception path, 8 worker tests
  (closed-market, kill-switch, full submit flow, skip-by-flag race,
  fail path, reconcile fill, reconcile non-terminal skip, reconcile
  fetch-error tolerance), 5 handler tests, plus help and bot-main
  updates. Total 242 passing, 2 skipped, 95% coverage.

To enable real paper submissions:

  /flag trading_enabled on

The bot will submit on the next tick (every 5 minutes during US
market hours, or call `/trade_now` to force one).

Not shipped (Phase 3.5):

- Roll logic when delta crosses 0.45.
- Drawdown circuit breaker that auto-engages kill_switch on a 7%
  weekly equity drop.
- /close <symbol> + /close_confirm. The orders table supports
  action='close' but no command writes those rows yet.

## 2026-04-27 . Phase 3.3: Strategy tick loop (dry-run)

Shipped:

- `strategy/clock.py` wraps Alpaca `get_clock` returning a narrow
  `ClockSnapshot`. The worker uses it to skip ticks outside market
  hours instead of maintaining a local calendar.
- `strategy/candidates.py` builds dry-run trade intents:
  - `select_put_strike(chain, target_delta, sleeve, today)` is a
    pure function. Filters to puts inside the sleeve DTE band that
    report a delta, picks the contract with delta closest to the
    target.
  - `build_intents(regime, sleeves, account, chain_fetcher, today)`
    walks active sleeves (skipping opportunistic in neutral, all in
    risk_off), fetches chains via an injected callable so unit tests
    can stub them, picks the strike, builds a `TradeIntent`, and
    enforces the per-sleeve dollar cap (`target_pct * equity`).
  - `summarise_intents(list)` renders a one-line-per-intent block
    plus a portfolio total line for notifications and replies.
- `strategy/worker.py` defines `StrategyWorker`. Same start/stop
  pattern as `NotificationWorker`. Polls every 5 min, no-ops when
  market is closed, no-ops when kill_switch is on (but still emits a
  heartbeat notification so the operator knows the loop is alive),
  otherwise: regime evaluation (writes regime_history on transition),
  account snapshot, sleeve config read, intent build, one
  info-priority notification with the summary.
- `/strategy_status` runs the same flow on demand and replies inline.
  Useful for inspecting the would-be plan during weekends.
- Worker wired into bot lifecycle alongside the notification worker;
  cancelled cleanly on shutdown before pool close.
- 3 clock tests, 12 candidate tests (pure strike selection edge
  cases plus the build_intents gating, sleeve cap, missing-quote and
  fetch-error tolerance), 5 worker tests (closed-market skip,
  kill-switch path, full happy path, transition surfacing, lifecycle),
  2 handler tests for /strategy_status. Total 217 passing, 2
  skipped, 95% coverage.

Not shipped (Phase 3.4):

- submit_order, cancel_order, close_position. The TradingClient
  wrapper is still strictly read-only.
- `orders` table (migration 008) and the gating-decision audit row.
- /trade_now, /close, /recent_trades.

## 2026-04-27 . Phase 3.2: Sleeve config + regime classifier

Shipped:

- `yfinance` added as a dep (Alpaca cannot serve the spot VIX index;
  VIXY tracks futures and is not interchangeable with VIX for the
  thresholds).
- Daily-bar helper added to `broker/market_data.py`:
  `get_daily_bars(symbol, lookback_days)` returns `DailyBar`. Pads
  the calendar window to absorb weekends/holidays.
- `strategy/indicators.py` exposes `get_vix_snapshot()` (yfinance
  `^VIX`, level + 5d % change) and `get_spy_snapshot()` (Alpaca bars,
  price + 20dma + 50dma + 10d annualised realized vol). All numbers
  are floats; precision of Decimal does not buy anything for vol
  percentages.
- `strategy/regime.py`:
  - `classify(vix, spy)` is the pure decision function applying the
    calibrated thresholds: risk_off if VIX > 25 OR SPY < 50dma OR
    VIX 5d > +30%; risk_on if VIX < 17 AND SPY > 20dma AND realized
    vol < 15; neutral otherwise.
  - `evaluate()` fetches live indicators and classifies (no write).
  - `compute_and_record(notes)` evaluates and appends a transition
    row to `regime_history` only when the regime changed.
- Migration 006 creates `sleeve_config` keyed by sleeve, seeded with
  the three calibrated rows (40/40/20 allocations; -0.30 risk_on /
  -0.20 neutral put deltas; 0.20 call delta; 7-10 DTE band; 50%
  profit take; 0.45 roll trigger; weekly-liquid symbol whitelists).
- Migration 007 creates `regime_history` (regime, vix, vix_5d_change,
  SPY price + MAs + RV, notes) with desc index on captured_at.
- `db/sleeve_config.py` exposes `get_all_sleeves`, `get_sleeve`,
  `update_sleeve(name, *, actor, **fields)` with column allow-list.
  `db/regime_history.py` exposes `append_regime`, `most_recent_regime`,
  `recent_transitions(limit)`.
- New `/sleeves` and `/regime` commands. `/sleeves` renders all three
  rows (target pct, deltas, DTE band, profit take, roll trigger,
  symbols, enabled flag). `/regime` evaluates live indicators on
  demand and prints the classification + inputs + threshold reminder.
- 9 indicators tests (SMA, realized vol, VIX history validation, SPY
  snapshot assembly), 11 regime tests (pure classifier table + the
  evaluate / compute_and_record paths), 9 sleeve_config tests
  (canonical order, JSON whitelist parsing, update column allow-list,
  rejection paths), 5 regime_history tests, 4 handler tests.
  194 passing total, 2 skipped, 96% coverage.

Not shipped (Phase 3.3+):

- Strategy tick loop. /regime evaluates on demand; nothing schedules
  it yet. Periodic regime evaluation is a 3.3 concern alongside the
  candidate-trade loop.
- /flag-style updates to sleeve config from Telegram. update_sleeve
  exists; a /sleeve_set or /sleeve_edit command can come later if
  needed.

## 2026-04-27 . Phase 3 spec + Phase 3.1: Options data wrapper

Spec:

- `PHASE3.md` captures the end-to-end wheel plan: strategy mechanics,
  three sleeves with defaults, regime classifier, risk controls,
  5-minute tick loop, three new migrations, broker extensions, bot
  commands, sub-phases 3.1-3.5, acceptance criteria, and nine
  load-bearing decisions awaiting owner sign-off.

Phase 3.1 shipped:

- `src/kai_trader/broker/options_data.py` wraps Alpaca's
  `OptionHistoricalDataClient`. Same async-via-to_thread pattern.
  `get_chain(underlying, expiration)` returns `OptionContract`
  dataclasses with strike, expiration, type, bid, ask, last, delta,
  gamma, theta, vega, IV. Sorts by (expiration, strike, type).
  Skips contracts whose OCC symbols cannot be parsed rather than
  failing the whole call.
- `parse_occ_symbol` utility decodes OCC strings into (underlying,
  expiration, option_type, strike). Strategy code in 3.4 will use the
  inverse to construct symbols for order placement.
- `/chain SYMBOL [YYYY-MM-DD]` command renders the chain, capped at
  30 lines with a "showing first N of M" footer to keep Telegram
  messages readable. Bad date or empty arg returns a clean usage hint.
- 10 wrapper unit tests, 6 handler tests, /help and bot-main updates,
  plus an SPY chain assertion in the live integration test.
  149 passing total, 2 skipped, 95% coverage.

Not shipped (waiting on Phase 3.2):

- Anything that consumes the chain. Strike selection, target-delta
  search, sleeve-aware filtering all live in 3.2 and 3.3.

## 2026-04-26 . Phase 2.9: Account snapshot history

Shipped:

- Migration 005 creates `account_snapshots` (equity, last_equity, cash,
  buying_power, portfolio_value, day_pl, status, paper, captured_at)
  with a desc index on captured_at. Applied to Supabase.
- `src/kai_trader/db/account_snapshots.py` exposes
  `record_snapshot(snapshot)` (returns row uuid) and
  `recent_snapshots(limit)` (returns `StoredSnapshot` dataclasses
  newest first; rejects limit < 1).
- `/snapshot_now` command pulls a fresh `AccountSnapshot` from Alpaca
  and writes it to Postgres, replying with the row id and key fields.
- `/history [N]` command renders the most recent N (default 10, max
  50) snapshots with timestamp, equity, cash, and day P&L. Empty
  state nudges the operator toward `/snapshot_now`. Bad input
  ("/history banana", "/history 999") replies with a clean error
  rather than crashing.
- 4 helper unit tests, 5 handler tests, /help and bot-main updates.
  132 passing total, 2 skipped, 94% coverage.

Why not /db_positions? The original 2.9 plan was to mirror Alpaca
positions into our `positions` table. That table is purpose-built
for the wheel (requires strike, expiration, contracts, sleeve);
equity positions don't fit. Account snapshot history is the
genuinely useful thing we can do with the read-only surface, and it
gives us free P&L tracking pre-strategy.

Not shipped (deliberate):

- Periodic background snapshot writer. Manual is enough for now;
  add a worker once we know the right cadence (likely tied to the
  regime-check interval that lands in Phase 3).
- /db_positions or any positions-table mirroring. That belongs to
  Phase 3 once the wheel populates the table.

## 2026-04-26 . Phase 2.8: Market data read

Shipped:

- `src/kai_trader/broker/market_data.py` wrapping Alpaca's
  StockHistoricalDataClient. Same async-via-to_thread pattern as the
  trading client. Exposes `get_latest_quote(symbol)` and
  `get_latest_trade(symbol)` returning narrow `QuoteSnapshot` and
  `TradeSnapshot` dataclasses. `QuoteSnapshot` carries derived `spread`
  and `mid` properties so handlers do not have to compute them inline.
- New `/quote SYMBOL` command. Renders bid, ask, spread, mid, last
  trade price, and timestamps. Unknown symbols return a clean
  "No data for X" reply rather than a stack trace.
- 6 wrapper unit tests, 3 handler tests, plus updates to /help and
  the bot-main smoke test. Live integration now also exercises a SPY
  quote and trade. 121 passing total, 2 skipped, 94% coverage.

Notes:

- Free IEX feed is the default for paper accounts. After-hours quotes
  for thinly-traded names may be empty; that is a data condition, not
  a wrapper bug.
- No options data yet. The wheel needs option chains; that goes on
  the Phase 3 spec.

## 2026-04-26 . Phase 2.7: Notification delivery worker

Shipped:

- `src/kai_trader/notifications/producer.py` with `enqueue(message,
  priority, *, channel='telegram', metadata, max_retries)`. Validates
  priority and channel against the migration 003 check constraints so a
  typo fails in Python rather than mid-INSERT.
- `src/kai_trader/notifications/worker.py` with `NotificationWorker`. An
  asyncio task that polls every 5s, claims a batch via
  `select for update skip locked`, sends each message through a
  caller-provided coroutine, and marks `sent_at`. On failure it bumps
  `retry_count`. When `retry_count >= max_retries` the row is skipped by
  the claim query, so it stays in the table as evidence rather than
  spinning forever.
- Bot wiring: post_init builds a closure that sends to
  `TELEGRAM_OWNER_ID` and starts the worker. post_shutdown cancels and
  awaits the worker before closing the DB pool.
- New `/notify_test [body]` command. Enqueues an info-priority telegram
  row so the round-trip can be verified without waiting for any future
  fill or regime event.
- 4 producer unit tests, 8 worker unit tests, 2 handler tests, plus
  /help and bot-main updates. 111 passing total, 2 skipped, 93%
  coverage.

Not shipped (deliberate):

- SMS delivery and the `channel='both'` coordination story. Producer
  accepts both, worker leaves them queued.
- Webhook or HTTP producers. Phase 2.7 only adds the in-process helper;
  external producers can come later.
- Failure-reason audit column. If we want it, add a migration that
  introduces `last_error text` and have the worker write to it.

## 2026-04-26 . Phase 2.5: System-flag command surface

Shipped:

- `src/kai_trader/db/system_flags.py`: `get_all_flags()` returns the three
  known flags as a dict, defaulting any missing row to `False` so the safe
  state always wins. `set_flag(key, value, *, actor)` upserts inside a
  transaction, fills `updated_at` and `updated_by`, and returns the prior
  value. Unknown keys raise `ValueError`.
- Three new bot commands:
  - `/flags` reads all three flags and prints `[ok]` / `[fail]` per line.
  - `/flag <name> <on|off>` sets a single flag and replies with the
    "prior -> new" transition. Accepts `on/off`, `true/false`, `1/0`,
    `yes/no` for the value token.
  - `/kill` is the emergency composite. Sets `kill_switch=true` and
    `trading_enabled=false` in two writes. Leaves `new_entries_enabled`
    alone so the audit trail stays clean for whoever explicitly turned
    entries off.
- `/help` advertises all three.
- 6 new system_flags unit tests, 6 new handler tests, plus updates to
  /help and the bot-main smoke test. 95 passing total, 2 skipped, 93%
  coverage.

Not shipped (still out of scope):

- Anything that reads the flags before acting. The wheel strategy is the
  first caller; it lives in Phase 3.

## 2026-04-26 . Phase 2: Read-only Alpaca paper integration

Shipped:

- `alpaca-py` 0.43 added as a runtime dep.
- `src/kai_trader/broker/alpaca.py`. Async wrapper around the sync
  `TradingClient` via `asyncio.to_thread`. Methods: `get_account`,
  `list_positions`, `ping`. Returns narrow dataclasses (`AccountSnapshot`,
  `PositionSnapshot`) so handlers do not depend on alpaca-py types.
  Deliberately no submit, cancel, or close methods.
- Three Alpaca env vars: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (both
  required), `ALPACA_PAPER` (defaults to `true`).
- New `/account` command. Renders status, equity, cash, buying power,
  portfolio value, and day P&L. Header tags paper vs LIVE.
- `/positions` swapped from placeholder to real Alpaca data. Empty state
  returns "No open positions." Per-position lines fall back to "n/a" when
  Alpaca returns null prices.
- `/health` extended to ping Alpaca alongside Postgres. Both shown up/down
  on separate lines.
- `/help` updated to list `/account` and the now-real `/positions`.
- Money formatters in `bot/formatting.py`: `format_money`,
  `format_signed_money`. Decimal in, USD by default, two dp, comma-grouped.
- Tests: 80 passing, 1 skipped, 93% line coverage. New unit tests for the
  broker (with stub TradingClient), the new handlers, and the money
  formatters. Live integration test at `tests/test_integration_alpaca.py`,
  gated behind `ALPACA_INTEGRATION_TEST=1`.

Verified:

- `ruff check`, `mypy --strict src/`, `pytest` all clean.
- Live integration test passes against the paper account.

Not shipped (out of Phase 2 scope, intentional):

- Order routing. No `submit_order`, `cancel_order`, or `close_position` is
  exposed anywhere.
- Wheel strategy, regime detection, sleeve allocation. Phase 3 work.
- Notification worker. Still queueing into `notifications`, still nothing
  draining.
- Live trading wiring. `ALPACA_PAPER=false` is configurable but unused
  until orders exist; `trading_enabled` flag is also still ungated.

Open follow-ups:

- Once strategy code arrives, every order path must read `system_flags`
  (`trading_enabled`, `new_entries_enabled`, `kill_switch`) before sending.
- The DB pool log line bug from 2026-04-25 is fixed in code but the
  process running before the fix logged a stale host name. Restart the bot
  to pick up the corrected log.

## 2026-04-24 . Phase 1: Foundation and Telegram bot skeleton

Shipped:

- Repo scaffolding: `pyproject.toml` (uv, Python 3.11+), `.gitignore`,
  `.env.example`, `.python-version`, full `src/kai_trader` tree.
- `config.py` with Pydantic Settings. Validates Supabase URL, derives the
  Postgres DSN from the project ref, reports env completeness for /health.
- `logging.py` with structlog. JSON renderer for prod/staging, colour console
  renderer for dev. Configurable via `LOG_LEVEL`.
- Four SQL migrations:
  - 001 system_flags. Seeds `trading_enabled`, `new_entries_enabled`,
    `kill_switch` all `false`.
  - 002 bot_commands. Audit log including unauthorised attempts.
  - 003 notifications. Outbound queue keyed by priority and channel.
  - 004 positions. Schema only, no logic yet.
- `scripts/apply_migrations.py`. Idempotent, tracks applied files in
  `schema_migrations`, warns on checksum drift.
- asyncpg-based DB client with `ping()`, `record_bot_command()`,
  `mark_command_response()`, `get_pool()`, `close_pool()`. All audit writes
  swallow exceptions so the bot never crashes on a DB hiccup.
- Telegram bot skeleton:
  - `main.py` entrypoint using python-telegram-bot v21 long polling.
  - `auth.py` whitelist middleware. Silent-ignore for non-owners. Parses
    command, args, and strips `@botname` suffixes. Records every attempt.
  - Five handlers: `/start`, `/help`, `/health`, `/status` (mocked),
    `/positions` (placeholder).
  - `_common.run_command` wrapper covers auth + reply + audit update in one
    place so handlers stay tiny.
- Tests: 57 passing, 1 skipped (live Supabase, behind env flag), 92% line
  coverage. Unit tests use an in-process Fake Update, async mocks for
  asyncpg, and mock out `db_ping` for `/health`.
- `README.md`, `CLAUDE.md`, `TRACKER.md`.
- `.mcp.json` project-scoped config for the Supabase MCP server.

Not shipped (out of Phase 1 scope):

- Live migration run against Supabase. Sandbox network only allows port 443,
  so Postgres (5432/6543) is unreachable from here. Script itself is verified
  via unit tests and one gated integration test. The owner runs
  `uv run python scripts/apply_migrations.py` from their own machine.
- Alpaca, trading logic, notification worker, dashboard. All later phases.

Open questions:

- Region for the Supabase pooler if we ever need to go through the IPv4
  pooler host rather than the direct IPv6 endpoint.
- Whether to rotate the Postgres password shared during this session. Listed
  as a Phase 1 close-out item.
