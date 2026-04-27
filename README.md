# Kai Trader

Automated options wheel trading, monitored and controlled through Telegram.
Phases 1, 2, and 3 (read-only Alpaca + flag surface + wheel strategy with
order placement and roll/close logic) have shipped. Phase 4 adds a
conversational Telegram bot ("Kai") with an approval flow for any change to
trades, params, or watchlists.

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
# then edit .env and fill in the required values:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID,
#   SUPABASE_URL, SUPABASE_DB_PASSWORD, SUPABASE_KEY,
#   ALPACA_API_KEY, ALPACA_SECRET_KEY,
#   ANTHROPIC_API_KEY (for the chat handler),
#   KAI_CHAT_RO_PASSWORD (for the read-only DB role).

# 3. Install dependencies
uv sync --extra dev

# 4. Apply database migrations
uv run python scripts/apply_migrations.py

# 5. (One-time) Bootstrap the read-only Postgres role used by the chat
#    layer. The script reads KAI_CHAT_RO_PASSWORD from the env. Re-run
#    whenever you rotate the password.
KAI_CHAT_RO_PASSWORD="$(grep KAI_CHAT_RO_PASSWORD .env | cut -d= -f2-)" \
  uv run python scripts/create_chat_ro_role.py

# 6. Set DATABASE_URL_RO in your .env to the same Supabase pooler URL
#    you use for DATABASE_URL, but with kai_chat_ro / KAI_CHAT_RO_PASSWORD.
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

Slash commands cover read paths (account, positions, regime, sleeves,
chain, history, etc.) and explicit operator actions (`/flag`, `/kill`,
`/close`, `/trade_now`). Free-form text from the owner is routed to
Kai, the conversational layer, which can read repo files, query the
read-only DB, hit Alpaca read endpoints, and **propose** changes via
inline Approve / Reject / Modify buttons. Kai never writes trades or
params directly.

Run `/help` from your bot for the live command list.

## Run the tests

```bash
uv run pytest
uv run ruff check
uv run mypy --strict src/
```

The suite targets 80%+ coverage and currently sits around 92%. One
integration test hits the live Supabase; it is skipped unless you set
`SUPABASE_INTEGRATION_TEST=1` in your environment.

## Render deployment

`render.yaml` declares a single Background Worker (no inbound HTTP because
the bot uses Telegram long-polling). The Background Worker stays up across
idle periods, which matters for the chat handler and event dispatcher.
Secrets (every `sync: false` key) are pasted into the Render dashboard and
never committed.

## MCP integration

`.mcp.json` wires the Supabase MCP server at project scope. From a Claude
Code session in this repo, run `/mcp` to authenticate, then the assistant
can query schemas and logs directly.

## Where to look next

- [CLAUDE.md](./CLAUDE.md) for the architecture, conventions, and the list of
  things that are intentionally not built yet.
- [TRACKER.md](./TRACKER.md) for the daily work log.
