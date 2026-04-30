# Kai Trader

Automated options wheel trading on Alpaca, controlled through a private
Telegram bot, with all state in Supabase Postgres. Single-owner system
designed for paper-first, then live with explicit flag flips.

## What this is

A defensive, premium-capture wheel: sell cash-secured puts at a target
delta, take profits early, roll when challenged for net credit, accept
assignment when the math says so, then sell covered calls against held
shares until called away. Repeat. The system is built around the
single-sleeve, small-account configuration shipped by migration 018,
optimised for accounts in the $25k-$100k range.

## How the strategy works

### The universe

One active sleeve (`index_core`) with a 30-name pool of cheap-liquid
optionable underlyings. Each name has weekly options on Alpaca's OPRA
feed and a low enough strike that one CSP fits inside a small account.

| Bucket | Tickers |
|---|---|
| Defensive large-caps | F, T, BAC, PFE, KO, KVUE, VZ, INTC, CSCO, GE, KMI, KHC, MO, WBA |
| Mid-cap growth / cyclical | HOOD, SOFI, PLTR, MU, MARA, RIOT, SNAP, RIVN |
| Quality | WFC, GM, C |
| Non-correlated ETFs | GDX, SLV, XLF, XLE, EEM |

The two other sleeves (`stable_largecap`, `opportunistic`) are
disabled. Re-enable them in `sleeve_config` if you want the original
three-bucket allocation back.

### Tick cadence

The strategy worker runs every **5 minutes** during US market hours.
Order reconciliation runs on every tick regardless of market state, so
overnight fills are reflected at the next open. A separate
`TradingStream` WebSocket subscribes to Alpaca `trade_updates` for
real-time fill notifications; the 5-minute reconciliation is
belt-and-suspenders.

Each tick performs, in order:

1. Reconcile any pending Alpaca orders.
2. Skip if market is closed.
3. Run the drawdown circuit breaker (auto-trips kill switch on breach).
4. Skip if `kill_switch` is engaged (heartbeat only).
5. Compute the regime, write a row only on transition.
6. Evaluate rolls on existing short puts.
7. Evaluate profit-takes on existing short puts.
8. Build new CSP intents from the pool, submit the top-ranked picks.
9. Detect put assignments, build and submit covered calls.
10. Post a tick summary to Telegram.

### Strike selection

For each whitelisted underlying:

1. Skip if next earnings falls inside the DTE window
   (`earnings_blackout_enabled=true`, yfinance lookup, fail-open on
   error).
2. Fetch the option chain.
3. Pick the put whose absolute delta is closest to the target for the
   current regime, with expiration inside the sleeve's 7-10 DTE band.

| Regime | Target put delta | Target call delta |
|---|---|---|
| `risk_on` | -0.40 | +0.30 |
| `neutral` | -0.30 | +0.30 |
| `risk_off` | no new entries | no new entries |

### Ranking the candidates

Once a strike is picked per symbol, candidates are scored and the
greedy fill takes the best:

```
score = annualised_yield_pct × spread_quality

annualised_yield_pct = (mid / strike) × (365 / DTE) × 100
spread_quality       = 1 - (ask - bid) / mid / 0.30   (0 .. 1)
```

A spread of 30% or more of mid hard-rejects the candidate (the OPRA
feed often has stale quotes on thin names; a wide-spread headline
yield is fiction because you cannot fill at the bid). Annualisation
makes a 7-day and a 10-day candidate directly comparable.

The greedy fill iterates score-descending and stops as soon as any
cap binds. Stable sort means whitelist order breaks ties.

### Capital deployment caps

Three caps clamp how much of the account is at risk in cash-secured
puts. All three are enforced *after* subtracting collateral already
locked in open short puts, so the strategy will not re-attempt to
open contracts you already hold.

| Cap | Value | Source |
|---|---|---|
| Total deployment | 70% of equity | `TOTAL_DEPLOYMENT_CAP_PCT` in `candidates.py` |
| Sleeve | 100% of equity (`index_core` is the only active sleeve) | `sleeve_config.target_pct` |
| Per-symbol | tiered by equity (see below) | `_PER_SYMBOL_CAP_TIERS` in `candidates.py` |
| Hard contract ceiling | 10 contracts per symbol | `MAX_CONTRACTS_PER_SYMBOL` |

The per-symbol cap loosens for smaller accounts so a single normal
CSP can clear:

| Equity | Per-symbol cap |
|---|---|
| under $50k | 100% |
| $50k to $150k | 60% |
| $150k to $500k | 30% |
| $500k or more | 15% |

### Per-tick entry cap

A single tick will submit at most **2 new CSPs per sleeve**
(`sleeve_config.max_new_entries_per_tick`, default 2). With a
30-name pool, this prevents one volatile day from flooding the book.
Additional candidates wait for the next tick.

### Profit-take

For each open short put: read the original credit from the filled CSP
order, look up the current ask, and submit a buy-to-close at the ask
when

```
current_ask <= original_credit × (1 - profit_take_pct)
```

Default `profit_take_pct = 0.50`, so the system closes at 50% of max
credit captured. Capital released by a profit-take is available on
the same tick for new entries.

### Rolling

A short put becomes a roll candidate when its live delta crosses
`roll_trigger_delta` (default `0.50`). The roll candidate must be:

- Same underlying.
- Strike strictly **lower** (further OTM).
- Expiration **on or after** the current expiration, inside the DTE
  band.
- Closest to the regime's target delta among qualifying contracts.

Rolls only execute for **net credit**. If `new_bid - current_ask <= 0`,
the position holds and the operator sees a `no_net_credit_candidate`
line in the tick summary. Better to accept assignment risk than lock
in a debit.

`risk_off` does **not** block rolls (rolling reduces risk on a
challenged position).

### Covered calls

After CSP processing each tick, the worker:

1. Detects assignments by matching held long equity against
   recently-filled CSPs. Records an idempotent audit row in `orders`.
2. For each held block (100 shares per contract), finds the sleeve
   that whitelists the underlying and picks the call closest to
   `target_delta_call` (default 0.30) inside the DTE band.
3. Submits the CC at bid. Quantity = `floor(shares / 100)`.

No capital math here. The shares are the collateral.

### Regime classifier

Pure function over VIX and SPY indicators (yfinance ^VIX + Alpaca
daily SPY bars):

```
risk_off  if  VIX > 25
          OR  SPY price < SPY 50DMA
          OR  VIX 5-day change > +30%

risk_on   if  VIX < 17
          AND SPY price > SPY 20DMA
          AND SPY realized vol (10d) < 15%

neutral   otherwise
```

A row is written to `regime_history` only on transition.

### Defensive layers

- **Three flags**, all enforced inside the broker submit calls as the
  last gate before HTTP:
  - `kill_switch=true` blocks every new order. Closes still allowed.
  - `trading_enabled=false` blocks new entries (puts and calls).
    Rolls still allowed.
  - `new_entries_enabled=false` blocks new puts. Rolls and closes
    proceed.
- **Drawdown circuit breaker**: if equity drops 7% or more from the
  7-day high-water mark, `kill_switch` is auto-engaged and a
  critical-priority Telegram notification fires. Idempotent; will
  not re-notify on subsequent ticks while still tripped.
- **Earnings blackout**: per-sleeve flag skips any underlying whose
  next earnings announcement falls inside the sleeve's DTE window.
  yfinance lookup with 24-hour per-symbol cache; fail-open on lookup
  errors.
- **Retry-storm suppressor**: an option contract that already has a
  failed `open_short_put` row from earlier today is skipped without
  another DB write or HTTP call. Stops the 5-minute tick from spamming
  Alpaca with the same failing strike.
- **Spread-quality reject**: any candidate with a bid-ask spread of
  30% or more of mid never enters the ranker.

### Authority and overrides

The strategy never writes to its own config. Free-form Telegram text
from the owner routes to Kai (the conversational handler), which can
read state and **propose** changes via inline Approve / Reject /
Modify buttons. The applier in `kai_trader.approvals.applier` is the
only writer for config mutations and writes a `decision_log` row for
every applied change. Direct `/flag`, `/kill`, `/close`,
`/close_confirm`, and `/trade_now` slash commands stay authoritative
for time-sensitive operator actions.

### Where the numbers live

| Setting | Location |
|---|---|
| Sleeve config (whitelist, deltas, DTE band, profit-take, roll trigger, per-tick cap, earnings flag) | `sleeve_config` table; latest values in migrations `006`, `009`, `017`, `018` |
| Total deployment cap, per-symbol tiers, hard contract ceiling, spread cutoff | `src/kai_trader/strategy/candidates.py` |
| Drawdown threshold and lookback | `src/kai_trader/strategy/drawdown.py` |
| Regime thresholds | `src/kai_trader/strategy/regime.py` |
| Tick cadence | `StrategyWorker.poll_interval` in `src/kai_trader/strategy/worker.py` |

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
