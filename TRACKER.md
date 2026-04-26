# TRACKER.md

Daily work log for Kai Trader. Append new entries at the top. One entry per
working day, short bullets, no corporate polish.

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
