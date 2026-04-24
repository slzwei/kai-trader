# CLAUDE.md

Reference for Claude (and humans) working inside this repo. Keep it current as
the project evolves.

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
| ENV                   | no       | `dev`, `staging`, or `prod`. Default `dev`.        |
| LOG_LEVEL             | no       | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default INFO. |
| TIMEZONE              | no       | IANA name. Default `Asia/Singapore`.               |

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

## Current state

Phase 1 is complete:

- Repo scaffolding, typed config, structlog, pyproject.
- Four SQL migrations: system flags, bot commands, notifications, positions.
- Idempotent migration runner with checksum drift detection.
- Telegram bot with `/start`, `/help`, `/health`, `/status` (mocked),
  `/positions` (placeholder).
- Whitelist auth middleware with silent-ignore for non-owners.
- Test suite at 90%+ coverage.
- Clean `ruff check`, clean `mypy --strict src/`.

## What is not built yet

- Alpaca integration (Phase 2+). No broker API client, no market data pull,
  no order routing.
- Trading logic. The wheel strategy, regime detection, risk sleeves, and
  premium-capture rules all live in later phases.
- Notification delivery worker. `notifications` rows are queued but nothing
  drains them yet.
- Dashboard / web UI. Not in scope until Phase 5+.
- Doppler secret management. `.env` is the only store for now.
- SMS channel. `notifications.channel` accepts `sms` and `both` but no
  deliverer exists.

## Known issues

- The integration test against live Supabase is gated behind
  `SUPABASE_INTEGRATION_TEST=1`. CI should leave it off until real
  credentials are wired in.
- `mypy --strict src/` prints a benign `unused section(s): module = ['tests.*']`
  note because the `tests.*` override is only used when mypy also scans the
  tests directory. The check itself succeeds.

## MCP

`.mcp.json` at the repo root configures the Supabase MCP server for project
scope. Running `claude /mcp` inside a Claude Code session on this repo will
offer to authenticate, after which Claude can query the schema, run SQL, and
inspect logs directly.
