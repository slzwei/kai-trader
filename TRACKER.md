# TRACKER.md

Daily work log for Kai Trader. Append new entries at the top. One entry per
working day, short bullets, no corporate polish.

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
