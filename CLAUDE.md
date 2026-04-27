# CLAUDE.md

Reference for Claude (and humans) working inside this repo. Keep it current as
the project evolves.

## Product vision

Kai Trader is a single-owner automated options trading system. One person
runs it, one person watches it, and nothing about the design caters to a
second user. Control and monitoring happen through a private Telegram bot.
Supabase Postgres holds the truth for every flag, every command, every trade,
every notification.

### What it trades

A defensive, premium-capture wheel on Alpaca. Paper trading comes first. Live
trading follows only after explicit flags are flipped. Capital is split across
three risk sleeves:

- Index core. Broad market exposure, the steady base.
- Stable large-cap. High-quality single names with reliable premium.
- Opportunistic. A smaller, selective bucket for setups that earn their way in.

Entries are regime-aware, exits lean conservative, and the bot does not chase.
No heroics.

### Non-negotiable design values

- Defence over offence. The kill-switch, the new-entries gate, and the global
  trading-enabled flag are always respected. If one says stop, the system
  stops. No hidden overrides.
- Audit everything. Every inbound command and every outbound trade writes a
  row. State never changes in the dark.
- Silent-ignore for strangers. Unauthorised Telegram users get no reply, not
  even an error. The bot will not confirm its own identity to probers.
- Small surface area. Nothing ships outside the active phase spec. Feature
  creep is worse than a missing feature.
- Quality gates are not optional. Type hints, `ruff`, `mypy --strict`, and a
  real test suite are the floor, not the ceiling.

### Phased build plan

- Phase 1: foundation and bot skeleton. Complete.
- Phase 2+: Alpaca integration, the wheel strategy itself, regime detection,
  risk-sleeve allocation, a notification delivery worker, and eventually a
  dashboard. Each phase gets its own spec and its own acceptance criteria
  before anything merges.

## Architecture

Kai Trader is a standalone automated options wheel trading system that the
owner monitors and controls through a Telegram bot. The trading loop will
place defensive, premium-capture wheel trades on Alpaca. Everything flows
through a single Supabase Postgres database.

Phase 1 ships the foundation only: repo structure, config, logging, database
schema, and a Telegram bot skeleton with read-only commands. There is no
Alpaca integration, no trading logic, and no dashboard yet.

```
                 +-----------------------+
                 |   Telegram (owner)    |
                 +----------+------------+
                            | long poll
                            v
                 +-----------------------+
                 |   Kai Trader bot      |   (Phase 1: this repo)
                 |   python-telegram-bot |
                 |   auth + handlers     |
                 +----+-------------+----+
                      |             |
                reads |             | writes audit, flags, notifications
                      v             v
                 +-----------------------+
                 |  Supabase Postgres    |
                 |  - system_flags       |
                 |  - bot_commands       |
                 |  - notifications      |
                 |  - positions          |
                 +-----------+-----------+
                             ^
                             | later phases
                             |
                 +-----------+-----------+
                 |  Trading engine       |   (Phase 2+: not built yet)
                 |  wheel strategy       |
                 |  Alpaca client        |
                 +-----------+-----------+
                             |
                             v
                 +-----------------------+
                 |  Alpaca (paper first) |
                 +-----------------------+
```

## Tech stack

- Python 3.11+
- uv for dependency management (not pip, not poetry)
- Supabase Postgres (direct asyncpg connection for raw SQL)
- python-telegram-bot v20+ (async)
- Pydantic v2 + pydantic-settings for typed configuration
- structlog for JSON logging in prod, console renderer in dev
- pytest + pytest-asyncio for tests
- ruff for linting, mypy --strict for types

## Directory layout

```
kai-trader/
  pyproject.toml            uv-managed project + tool config
  .env.example              env var reference, safe to commit
  .env                      local secrets, gitignored
  .mcp.json                 project-scoped MCP config (Supabase MCP)
  src/kai_trader/
    config.py               Pydantic Settings, env var loading
    logging.py              structlog setup (JSON prod, console dev)
    db/
      client.py             asyncpg pool + audit helpers
      migrations/           numbered .sql files, applied in order
    bot/
      main.py               entrypoint, wires handlers, starts polling
      auth.py               whitelist middleware (silent-ignore on reject)
      formatting.py         shared formatting helpers
      handlers/             one file per command
        start.py
        help.py
        health.py
        status.py
        positions.py
        _common.py          auth + reply + audit wrapper
  tests/                    pytest suite, 80%+ coverage
  scripts/
    run_bot.sh              launches the bot with uv
    apply_migrations.py     idempotent schema applier
```

## Local dev setup

1. Install uv if you do not have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Copy env template: `cp .env.example .env` and fill in real values.
3. Install deps: `uv sync --extra dev`
4. Apply migrations: `uv run python scripts/apply_migrations.py`
5. Run the bot: `bash scripts/run_bot.sh`
6. Run tests: `uv run pytest`
7. Lint: `uv run ruff check`
8. Type check: `uv run mypy --strict src/`

## Environment variables

| Key                   | Required | Notes                                              |
|-----------------------|----------|----------------------------------------------------|
| TELEGRAM_BOT_TOKEN    | yes      | From BotFather.                                    |
| TELEGRAM_OWNER_ID     | yes      | Your personal Telegram ID (int).                   |
| SUPABASE_URL          | yes      | `https://<project-ref>.supabase.co`.               |
| SUPABASE_DB_PASSWORD  | yes      | Postgres password from Supabase dashboard.         |
| SUPABASE_KEY          | no       | Service role JWT. Reserved for later phases.       |
| DATABASE_URL          | no       | Full Postgres URL. Set on IPv4-only networks, use the Session pooler string from the Supabase dashboard. Overrides the computed direct host. |
| ALPACA_API_KEY        | yes      | From the Alpaca dashboard. Paper keys start with PK. |
| ALPACA_SECRET_KEY     | yes      | Paired with the API key. Shown once on key creation. |
| ALPACA_PAPER          | no       | `true` (default) routes to Alpaca paper. `false` switches to live, but live trades still require the trading-enabled flag. |
| ENV                   | no       | `dev`, `staging`, or `prod`. Default `dev`.        |
| LOG_LEVEL             | no       | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default INFO. |
| TIMEZONE              | no       | IANA name. Default `Asia/Singapore`.               |
| ANTHROPIC_API_KEY     | for chat | Required for the conversational handler. Without it, free-form messages return a "not configured" reply and slash commands continue to work. |
| CHAT_MODEL            | no       | Override the chat model. Default `claude-sonnet-4-6`. |
| DATABASE_URL_RO       | for chat | Read-only DSN for the chat tool layer. Authenticate as `kai_chat_ro`. Without it, `query_supabase` fails closed. |
| KAI_CHAT_RO_PASSWORD  | bootstrap | Used by `scripts/create_chat_ro_role.py` to create or rotate the `kai_chat_ro` role. Not read at runtime by the bot. |

## Conventions

- Type hints required on every function. `mypy --strict src/` must pass.
- No em dashes anywhere (code, comments, docs, commit messages). Use periods,
  commas, or colons.
- Humanised writing style, not corporate AI-speak.
- Never `print`. Use `structlog` via `kai_trader.logging.get_logger`.
- Every module has a top-level docstring explaining purpose.
- Conventional commits: `feat:`, `chore:`, `test:`, `docs:`, `fix:`, `refactor:`.
- Audit every command. Both authorised and unauthorised Telegram messages
  land in `bot_commands` for forensic review.
- Unauthorised Telegram users get silent ignore. No reply, not even an error.
  The bot should not confirm its own identity to random probers.
- Secrets live only in `.env`. Never committed. `.env.example` holds the keys
  with placeholder values.
- Migrations are plain SQL, numbered, idempotent. Applied in filename order.
  `schema_migrations` tracks what has been run.
- Free-form text from the owner is routed to Kai (the conversational
  handler). Slash commands stay authoritative; the LLM is for inspection
  and for proposing changes.
- Kai's tool layer is the **only** path to data. The system prompt enforces
  grounding. Reads go through the read-only `kai_chat_ro` role.
- Anything that mutates trades, params, or watchlists must go through
  `pending_changes`. The applier (the only writer outside the trading
  engine) lives in `kai_trader.approvals.applier` and writes a
  `decision_log` row for every applied change.
- Outbound notifications that are not direct chat replies go through the
  `events` table and the `EventDispatcher` worker. The existing
  `notifications` queue stays for plain-text strategy heartbeats.

## Current state

Phases 1, 2, 2.5, 2.7, 2.8, 2.9, 3.1-3.6, 4, 5a, 5b, 5c, **and 5d** shipped:

- Phase 5d adds the earnings blackout filter. Migration
  `017_sleeve_earnings_blackout.sql` adds an
  `earnings_blackout_enabled` column to `sleeve_config` (default
  `true`). The new `kai_trader/strategy/earnings.py` looks up the
  next earnings date per symbol via yfinance with a 24-hour
  per-symbol cache; failures fail open (return None, log warning,
  do not block trading). `build_intents_with_diagnostics` accepts
  an optional `earnings_filter` callable; when the sleeve has the
  flag enabled and the filter reports earnings inside the DTE
  window, the symbol is skipped before any chain fetch and counted
  in a new `symbols_skipped_for_earnings` diagnostic. The strategy
  worker passes `is_earnings_in_window` as the filter on every tick.
- Phase 5c ships the `TradingStream` WebSocket worker for real-time
  fill notifications. New package `kai_trader/streams/` with
  `trading_stream.py:TradingStreamWorker`. Subscribes to Alpaca's
  `trade_updates` channel; on each event, applies the matching
  `orders` row mutation (status, filled_at, filled_avg_price) and
  enqueues a Telegram notification for fill / partial_fill via the
  existing notifications producer. Reconnects with exponential
  backoff (cap 60s); heartbeat logs every 60s while connected. The
  strategy worker's periodic `_reconcile_pending` stays as belt
  and suspenders. Wired into `bot/main.py` startup/shutdown
  alongside the other workers.
- Phase 5b adds profit-take execution. Migration
  `016_profit_take_close_action.sql` extends `orders.action` to admit
  `profit_take_close`. `broker/alpaca.py` adds `submit_buy_to_close`
  (gated by `kill_switch` only, mirroring `close_position`) and
  `list_short_option_positions`. The new
  `kai_trader/strategy/profit_take.py` walks open short puts, looks up
  the originating CSP via `recent_orders` to read `filled_avg_price`
  as the original credit, fetches the live chain, and emits a
  `CloseIntent` when current ask <= original_credit * (1 -
  profit_take_pct). The worker tick runs `_handle_profit_takes`
  between rolls and CSP build so any freed capital is reusable on the
  same tick. Tick summary surfaces "Profit-take: N closed at
  threshold".
- Phase 5a closes the wheel loop: covered calls + assignment detection.
  Migration `015_extended_order_actions.sql` widens `orders.action` to
  accept `open_covered_call`, `close_covered_call`, and `assignment`.
  `broker/alpaca.py` adds `submit_short_call` (gated by the same flag
  triad as puts) and `list_long_equity_positions` (filters out OCC
  symbols). New strategy modules: `assignment.py` matches recently
  filled CSPs against current long stock holdings and records audit
  rows; `covered_calls.py` mirrors `candidates.py` for the call leg —
  one CC per held underlying via sleeve whitelist match, qty derived
  from `floor(shares / 100)`, no per-symbol cap math because shares
  are the collateral. The strategy worker now runs assignment
  detection and CC build/submit after CSP build each tick; tick
  summary surfaces "Assigned: N new" and "CCs: ..." lines plus
  per-warning diagnostics.
- Phase 4 ships the conversational chat handler, the read-only DB role,
  the approval flow, and the proactive event dispatcher. `migrations/
  011-014` add `chat_history`, `decision_log`, `events`,
  `pending_changes`. `scripts/create_chat_ro_role.py` (run once after
  migrations) creates the `kai_chat_ro` Postgres role used by the chat
  tool layer's `query_supabase` tool.
- New modules: `kai_trader/chat/{client,tools,conversation,system_prompt,
  chunker,locks}.py`, `kai_trader/db/{chat_history,decision_log,events,
  pending_changes,readonly}.py`, `kai_trader/approvals/applier.py`,
  `kai_trader/events/{dispatcher,render}.py`,
  `kai_trader/bot/handlers/{chat,approval}.py`.
- Free-form Telegram text from the owner is routed to
  `chat.conversation.handle_message`, which calls
  `claude-sonnet-4-6` via the official `anthropic` SDK with prompt
  caching on the system prompt and tool definitions. The tool surface is
  read-only with one exception: `propose_change` writes a row to
  `pending_changes` (status=pending) and enqueues a
  `pending_change_created` event.
- The `EventDispatcher` worker drains `events`, renders each into a
  Telegram message (with inline Approve / Reject / Modify buttons for
  pending changes), and marks dispatched. The `CallbackQueryHandler`
  routes the click, runs the applier on Approve, and writes a
  `decision_log` row.
- Per-user `asyncio.Lock` (in `chat.locks`) serialises concurrent chat
  messages from the same owner. The chat handler keeps the typing
  indicator alive via a 4-second refresh task and chunks long replies on
  paragraph boundaries to fit Telegram's 4096-char limit.

Earlier phases unchanged:

- Repo scaffolding, typed config, structlog, pyproject.
- Ten SQL migrations: system flags, bot commands, notifications, positions,
  account snapshots, sleeve config, regime history, orders, sleeve
  recalibration (3.6), enable new entries (3.6).
- Idempotent migration runner with checksum drift detection.
- Telegram bot with `/start`, `/help`, `/health`, `/status` (mocked),
  `/account` (live Alpaca paper), `/positions` (live Alpaca paper),
  `/flags`, `/flag`, `/kill`, `/notify_test`, `/quote`, `/snapshot_now`,
  `/history`, `/chain`, `/sleeves`, `/regime`, `/strategy_status`,
  `/trade_now`, `/recent_trades`, `/close`, `/close_confirm`.
- Whitelist auth middleware with silent-ignore for non-owners.
- Read-only Alpaca client at `src/kai_trader/broker/alpaca.py`. Wraps the
  sync `alpaca-py` SDK with `asyncio.to_thread`. Exposes `get_account`,
  `list_positions`, `ping`. No order placement methods exist anywhere.
- Market data wrapper at `src/kai_trader/broker/market_data.py`. Same
  async-via-to_thread pattern around Alpaca's StockHistoricalDataClient.
  Exposes `get_latest_quote` and `get_latest_trade` returning
  `QuoteSnapshot` / `TradeSnapshot` dataclasses. Free IEX feed by default.
- Options data wrapper at `src/kai_trader/broker/options_data.py` around
  Alpaca's `OptionHistoricalDataClient`. Exposes `get_chain(symbol,
  expiration=None)` returning `OptionContract` dataclasses (strike,
  expiration, type, bid, ask, last, delta, gamma, theta, vega, IV).
  Includes `parse_occ_symbol` utility for decoding OCC strings.
- Daily-bar helper added to `market_data.py`: `get_daily_bars(symbol,
  lookback_days)` returns `DailyBar` rows. Used by the regime classifier
  for SPY moving averages and realized volatility.
- Strategy package at `src/kai_trader/strategy/`:
  - `indicators.py`: `get_vix_snapshot()` (yfinance ^VIX, level + 5d
    change) and `get_spy_snapshot()` (Alpaca daily bars, price + 20dma
    + 50dma + 10d realized vol).
  - `regime.py`: pure `classify(vix, spy)` returning `risk_on` /
    `neutral` / `risk_off` per the calibrated PHASE3.md thresholds,
    plus `evaluate()` (live snapshot, no write) and
    `compute_and_record(notes)` (writes a `regime_history` row only
    on transition).
- Sleeve config helpers at `src/kai_trader/db/sleeve_config.py`:
  `get_all_sleeves`, `get_sleeve(name)`, `update_sleeve(name, *,
  actor, **fields)` with column allow-list. Three rows seeded by
  migration 006 (40/40/20 split, calibrated deltas, weekly DTE band,
  weekly-liquid symbol whitelists).
- Regime history helpers at `src/kai_trader/db/regime_history.py`:
  `append_regime`, `most_recent_regime`, `recent_transitions(limit)`.
- Strategy tick loop in dry-run mode (`src/kai_trader/strategy/`):
  - `clock.py` wraps Alpaca `get_clock` so the worker respects market
    hours and holidays without a local calendar.
  - `candidates.py` is the pure intent builder. `select_put_strike`
    picks the put closest to the regime-dependent target delta inside
    the sleeve DTE band. `build_intents` walks active sleeves
    (skipping opportunistic in neutral, all in risk_off), fetches
    chains via an injected callable for testability, applies the
    sleeve dollar cap, and returns a list of `TradeIntent`.
  - `worker.py` runs `StrategyWorker` every 5 minutes; it skips
    closed-market ticks, skips strategy when `kill_switch` is on
    (still notifies a heartbeat), records regime transitions, and
    enqueues one info-priority notification per tick summarising the
    intents it would have submitted.
  - `/strategy_status` runs the same flow on demand and replies inline.
- Order placement (Phase 3.4) is wired:
  - Migration 008 creates the `orders` table (intent + alpaca_order_id +
    status + gating_decision + fill data).
  - `db/orders.py` exposes `record_intent`, `mark_submitted`,
    `mark_status`, `recent_orders`, `pending_orders`.
  - `broker/alpaca.py` adds `submit_short_put` (sell-to-open limit
    order, gated by `kill_switch` and `trading_enabled`) returning a
    typed `SubmitResult` that distinguishes "not sent" from "broker
    error", plus `get_order_status` for reconciliation.
  - The strategy worker now: reconciles pending Alpaca orders at the
    top of each tick (writes back fill price and status), then for each
    candidate intent records a row, calls the gated submitter, and
    updates the row to `submitted` / `skipped_by_flag` / `failed`.
  - `/trade_now` forces an immediate tick. `/recent_trades [N]` reads
    the orders table newest-first.
  - The flag gate inside `submit_short_put` is the **last** check
    before any HTTP call to Alpaca. Even if the worker code path
    races with someone toggling kill_switch from Telegram, the broker
    refuses cleanly and the row is marked `skipped_by_flag`.
- Defensive layers (Phase 3.5):
  - **Drawdown circuit breaker** at `src/kai_trader/strategy/drawdown.py`.
    Each tick reads recent `account_snapshots`, computes the high-water
    mark over a 7-day window, and if equity is down 7% or more from
    that high it auto-engages `kill_switch` and fires a `critical`
    notification. Idempotent: re-tripping on the same already-killed
    state does not re-notify.
  - **Roll logic** at `src/kai_trader/strategy/rolls.py`. Worker
    fetches Alpaca positions, identifies short puts whose live delta
    has crossed the sleeve's `roll_trigger_delta` (default 0.45), and
    builds a roll candidate further OTM at the same or later
    expiration. Only rolls for **net credit**: if the chain has no
    candidate where the new put's bid exceeds the existing put's ask,
    holds and surfaces a "no_net_credit_candidate" line in the tick
    summary. Roll execution is gated by `trading_enabled` and
    `kill_switch` (held rolls are reported even when execution is
    blocked, so the operator can see the situation).
  - **`/close <SYMBOL>` and `/close_confirm <SYMBOL>`** for manual
    discretionary closes. Two-step confirmation with a 30-second TTL
    keyed by (user_id, symbol). Closes are gated by `kill_switch`
    only (not by `trading_enabled`, because closing reduces exposure).
    Each successful close lands an `action='close'` row in `orders`.
- Account snapshot history via migration 005 + `src/kai_trader/db/
  account_snapshots.py`. `record_snapshot` persists an `AccountSnapshot`,
  `recent_snapshots(limit)` reads them back newest first. The bot exposes
  `/snapshot_now` to capture and `/history [N]` to view. Periodic
  background snapshots are intentionally not wired yet; manual is enough
  pre-strategy.
- System-flag helpers at `src/kai_trader/db/system_flags.py`. Reads and
  atomically updates `trading_enabled`, `new_entries_enabled`, and
  `kill_switch`. Records the actor's Telegram ID in `updated_by`.
- Notification queue producer + worker at `src/kai_trader/notifications/`.
  Producer enqueues into the `notifications` table. Worker runs as an
  async task inside the bot, polls every 5s, claims undelivered telegram
  rows via `select for update skip locked`, sends through the bot's
  Telegram client, and marks `sent_at`. Failures bump `retry_count`;
  exhausted rows stay queued for inspection.
- `/health` reports DB and Alpaca up/down side by side.
- Test suite at 90%+ coverage. Clean `ruff check`, clean `mypy --strict src/`.

## What is not built yet

- Trading logic. The wheel strategy, regime detection, risk sleeves, and
  premium-capture rules all live in later phases.
- Order placement. The Alpaca client deliberately exposes only fetch methods.
  Submit, cancel, and close arrive when strategy lands. They will read the
  three flags via `kai_trader.db.system_flags.get_all_flags` before sending
  anything to the broker.
- Live (non-paper) trading. `ALPACA_PAPER=true` is the default; flipping it
  to `false` only matters once orders exist.
- Dashboard / web UI. Not in scope until Phase 5+.
- Doppler secret management. `.env` is the only store for now.
- SMS channel. The producer accepts `channel='sms'` and `channel='both'`
  rows, but the Phase 2.7 worker only delivers `telegram`. SMS-bound rows
  sit in the queue until an SMS deliverer ships.

## Known issues

- Integration tests against live Supabase and live Alpaca are gated behind
  `SUPABASE_INTEGRATION_TEST=1` and `ALPACA_INTEGRATION_TEST=1` respectively.
  CI should leave both off until credentials are wired in.
- `mypy --strict src/` prints a benign `unused section(s): module = ['tests.*']`
  note because the `tests.*` override is only used when mypy also scans the
  tests directory. The check itself succeeds.

## MCP

`.mcp.json` at the repo root configures the Supabase MCP server for project
scope. Running `claude /mcp` inside a Claude Code session on this repo will
offer to authenticate, after which Claude can query the schema, run SQL, and
inspect logs directly.
