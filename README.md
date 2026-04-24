# Kai Trader

Automated options wheel trading, monitored and controlled through Telegram.
This repo is currently at **Phase 1: Foundation + Telegram bot skeleton**.
No trading logic or broker integration yet. The bot answers read-only
commands and audits every message.

## What this is

A single-owner trading system that will eventually run a defensive, premium
capture wheel strategy on Alpaca. You interact with it through a private
Telegram bot. Database of record is Supabase Postgres.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A Supabase project (you need the project URL and the Postgres password)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your own Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))

## Setup

```bash
# 1. Clone and enter
git clone https://github.com/slzwei/kai-trader.git
cd kai-trader

# 2. Create your local .env
cp .env.example .env
# then edit .env and fill in the five required values:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID,
#   SUPABASE_URL, SUPABASE_DB_PASSWORD, SUPABASE_KEY

# 3. Install dependencies
uv sync --extra dev

# 4. Apply database migrations
uv run python scripts/apply_migrations.py
```

The migration script is idempotent. Run it again whenever new `.sql` files
land under `src/kai_trader/db/migrations/`.

## Run the bot

```bash
bash scripts/run_bot.sh
```

Then message `/start` to your bot from your whitelisted Telegram account.
Non-whitelisted users are silently ignored; they get no reply at all, by
design.

Available commands in Phase 1:

- `/start` . wake check, echoes your Telegram ID
- `/help` . command list
- `/health` . bot uptime, Postgres ping, env completeness, SGT timestamp
- `/status` . mocked portfolio summary (clearly labelled)
- `/positions` . placeholder until the trading engine ships

## Run the tests

```bash
uv run pytest
uv run ruff check
uv run mypy --strict src/
```

The suite targets 80%+ coverage and currently sits around 92%. One
integration test hits the live Supabase; it is skipped unless you set
`SUPABASE_INTEGRATION_TEST=1` in your environment.

## MCP integration

`.mcp.json` wires the Supabase MCP server at project scope. From a Claude
Code session in this repo, run `/mcp` to authenticate, then the assistant
can query schemas and logs directly.

## Where to look next

- [CLAUDE.md](./CLAUDE.md) for the architecture, conventions, and the list of
  things that are intentionally not built yet.
- [TRACKER.md](./TRACKER.md) for the daily work log.
