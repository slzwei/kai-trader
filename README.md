# Kai Trader

Automated options wheel trading on Alpaca with an AI decision layer,
controlled through a private Telegram bot and a read-only web
dashboard, with all state in Supabase Postgres. Single-owner system:
paper burn-in first, live only after explicit flag flips.

## What this is

A defensive, premium-capture wheel: sell cash-secured puts at a target
delta, take profits early, roll challenged positions for net credit
only, accept assignment when it comes, then sell covered calls against
the shares until called away. Repeat.

Since Phase A1 the pipeline has four layers, and the ordering is the
whole point:

```
market/options data (Alpaca chains, quotes, bars; VIX via yfinance)
        |
deterministic screener        picks strikes, applies filters, ranks
        |
AI decision layer             TAKE / REJECT per candidate (Claude)
        |
deterministic risk gate       sizes and caps; the final word on money
        |
flag-gated execution          re-reads kill_switch before every order
        |
Alpaca (paper by default)
```

The AI can only shrink and reorder what the screener produced. It
cannot call the broker, cannot construct an `ApprovedIntent`, cannot
bypass the gate, and cannot touch flags or risk limits; AST hygiene
tests in `tests/test_ai_pipeline.py` enforce all of that. Every AI
failure (timeout, malformed output, missing key, budget overrun) fails
CLOSED for new entries, and position management never routes through
the AI at all.

## The tick, every 5 minutes during US market hours

1. Reconcile pending Alpaca orders (runs even when the market is
   closed, so overnight fills land at the next open).
2. Drawdown circuit breaker: equity down 7% from the 7-day high-water
   mark engages the entry freeze (`new_entries_enabled` off, never
   `kill_switch`) and cancels working risk-increasing orders while the
   breach holds. Profit-takes, manual closes, and all observation keep
   running.
3. Skip if the market is closed. If the kill switch is on (manual
   only), stop after observation: fills, assignments, and position
   snapshots still record, but no orders are placed, closed, or
   cancelled.
4. Compute the regime (VIX + SPY moving averages); log transitions.
5. Evaluate rolls on challenged short puts (net-credit-only; the close
   leg must FILL before the reopen goes out).
6. Evaluate profit-takes (default: close at 50% of credit captured).
7. Screen new CSP candidates, pass the cap-viable ones through the AI
   underwriter, gate the TAKEs, submit the survivors at chain mid.
8. Detect assignments from Alpaca's OPASN feed; sell covered calls
   against held shares (cost-basis floor, minimum credit).
9. Persist the position book for the dashboard, post a tick summary to
   Telegram.

Ticks are serialised by a Postgres advisory lock, so `/trade_now`, the
scheduled loop, and a deploy crossover can never run two ticks at once.

## The screener (deterministic)

Per whitelisted symbol, in order: cool-down check, earnings blackout,
50-DMA trend filter, chain fetch, strike selection (put closest to the
regime's target delta inside the sleeve's DTE band), premium floors,
then scoring:

```
score = annualised_yield x spread_quality
annualised_yield = (mid / strike) x (365 / DTE)
spread_quality   = 1 - (spread / mid) / 0.30      (>=30% spread rejects)
```

**Earnings blackout is fail-closed** and reads the union of three
independent calendars (EODHD, Finnhub, yfinance): the soonest upcoming
date any source reports wins, and no confirmed date at all means skip.
The trend filter is fail-closed the same way. Both exist because the
strategy's worst realized losses came from assignment into
deteriorating names.

## The AI decision layer (Phase A1)

Mode is set by `AI_DECISION_MODE`: `off` (byte-identical to the pure
deterministic strategy, pinned by golden parity tests) or `filter`
(live since 2026-08-26). In filter mode each screened candidate gets a
structured packet (price, greeks, economics, breakeven and cushion,
volatility context, quant scores, portfolio context, recent headlines
with freshness stamps, and an explicit list of what data is missing)
and a Claude model answers one underwriting question: assuming
assignment is realistic, do we want to own this at the breakeven? The
answer is a strictly validated TAKE or REJECT with confidence,
wheel-suitability, event risk, fundamental view, risk flags, and a
thesis. There is no maybe.

TAKEs re-rank by `final_score = quant_composite x wheel_suitability`
and proceed to the gate. Candidates with provably zero per-name
headroom skip the AI entirely (the gate would reject them regardless,
so no tokens are spent on foregone conclusions). Decisions are cached
per contract, prompt version, regime, earnings/trend status, and
premium bucket. Every evaluation, TAKE and REJECT alike, is persisted
to `ai_decisions` with the full packet, model and prompt lineage,
tokens, latency, cost, and the final pipeline disposition. `/ai_status`
shows today's counters.

## The risk gate (Phase R1)

`kai_trader/risk/gate.py` owns every cap, and the worker's submission
path only accepts gate-issued `ApprovedIntent` values (enforced under
`mypy --strict` plus a runtime guard). Caps, all applied net of
collateral already locked by open positions and working unfilled
orders: total deployment vs equity, a haircut against the broker's
live options buying power, a per-name notional cap, a per-symbol
contract ceiling, per-tick and per-day deployment velocity caps, and
entry cool-downs. Constants live in `gate.py`; per-sleeve parameters
(deltas, DTE band, profit-take, roll trigger, whitelists) live in the
`sleeve_config` table and are visible live via `/sleeves`.

## The weekly universe review (Phase U1)

Which names the wheel may be assigned is the real risk surface, so it
is machine-proposed and human-ratified. Weekly (and on demand via
`/universe_review`), a curated candidate pool plus the current
whitelists are screened deterministically, then an AI curator judges
each survivor: ADD or SKIP for pool names, KEEP or RETIRE for
incumbents. Guardrails cap every run at 2 adds and 2 retires with
sleeve sizes bounded 4-10, and the output is only ever a
`pending_changes` proposal with the thesis attached. Nothing applies
until the owner taps Approve, on Telegram or on the dashboard. Retired
names keep being managed to close; only new entries stop.

## Surfaces

- **Telegram bot**: the control plane. Slash commands for reads and
  explicit operator actions (`/flag`, `/kill`, `/close`, `/trade_now`,
  `/ai_status`, `/universe_review`, `/help` for the full list).
  Free-form text routes to Kai, a conversational layer with read-only
  tools that can propose (never apply) config changes. Strangers get
  silent-ignored; every inbound command is audited.
- **Web dashboard** (`kai-trader-dashboard`, Render free tier): account
  stats, 7-day equity curve, live position book, recent orders, the
  last 20 AI decisions with theses, and Pending approvals with
  Approve/Reject buttons. It authenticates to Postgres as the
  read-only `kai_chat_ro` role and holds no broker keys or Telegram
  token; its approval buttons only file requests into `web_actions`,
  which the bot validates and executes with its own credentials.
  Access is gated by `DASHBOARD_TOKEN` (one `?token=` visit sets a
  30-day cookie).

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A Supabase project (project URL plus the Postgres password)
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and
  your own Telegram user ID
- An Anthropic API key (chat layer and AI decision layer)
- Optional but recommended: a free Finnhub API key (earnings calendar)

## Setup

```bash
# 1. Clone and enter
git clone https://github.com/slzwei/kai-trader.git
cd kai-trader

# 2. Create your local .env and fill in the values. The full variable
#    reference with notes lives in CLAUDE.md.
cp .env.example .env

# 3. Install dependencies
uv sync --extra dev

# 4. Apply database migrations (idempotent; re-run whenever new .sql
#    files land under src/kai_trader/db/migrations/)
uv run python scripts/apply_migrations.py

# 5. One-time: bootstrap the read-only Postgres role used by the chat
#    layer and the dashboard, then set DATABASE_URL_RO accordingly.
KAI_CHAT_RO_PASSWORD="$(grep KAI_CHAT_RO_PASSWORD .env | cut -d= -f2-)" \
  uv run python scripts/create_chat_ro_role.py
```

## Run the bot

```bash
bash scripts/run_bot.sh
```

Message `/start` to your bot from the whitelisted account. The
dashboard runs separately: `uv run python -m kai_trader.dashboard.main`
(it reads only `DATABASE_URL_RO`, `DASHBOARD_TOKEN`, and `PORT`).

## Run the tests

```bash
uv run pytest
uv run ruff check src/ tests/
uv run mypy --strict src/
uv run python scripts/e2e_smoke_test.py
```

Around 1,140 tests: golden parity fixtures pin that the gate
extraction and the AI layer's off mode changed no trading decision;
AST hygiene tests pin that the AI package cannot reach the broker.
Integration tests against live services are env-gated
(`SUPABASE_INTEGRATION_TEST`, `ALPACA_INTEGRATION_TEST`,
`KAI_SCHEMA_INTEGRATION_TEST`).

## Deployment

`render.yaml` declares two services from the same Docker image:

- `kai-trader`: a Background Worker running the bot and every worker
  loop (no inbound HTTP; Telegram long-polling).
- `kai-trader-dashboard`: a free-tier Web Service running the
  dashboard (spins down when idle; first visit after a quiet spell
  takes a few seconds).

Render watches the `claude/kai-trader-phase-1-sHFJk` branch, not
`main`; both branches are kept in lockstep. Secrets (`sync: false`
keys) are pasted into the Render dashboard and never committed. Any
deploy that touches strategy code resets the paper burn-in clock.

## Safety invariants

- The kill switch, `trading_enabled`, and `new_entries_enabled` are
  re-read from Postgres inside the broker module as the last step
  before every order. No code path skips them.
- The AI cannot call Alpaca, produce an `ApprovedIntent`, bypass the
  gate, change risk parameters, or block position management by being
  unavailable.
- Watchlist changes require a human Approve, whichever surface files
  them.
- Every command, order intent, fill, assignment, AI decision, applied
  change, and approval request is persisted. State never changes in
  the dark.

## Where to look next

- [CLAUDE.md](./CLAUDE.md) for the architecture, the full environment
  variable table, conventions, and the phase-by-phase current state.
- [TRACKER.md](./TRACKER.md) for the daily work log.
