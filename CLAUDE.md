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
| ALPACA_API_KEY        | yes      | From the Alpaca dashboard. Paper keys start with PK. Used as the paper key fallback when ALPACA_API_KEY_PAPER is unset. |
| ALPACA_SECRET_KEY     | yes      | Paired with the API key. Shown once on key creation. Used as the paper secret fallback. |
| ALPACA_API_KEY_PAPER  | no       | Optional explicit paper key. Lets you keep both pairs configured and toggle with ALPACA_PAPER. |
| ALPACA_SECRET_KEY_PAPER | no     | Paired with ALPACA_API_KEY_PAPER. |
| ALPACA_API_KEY_LIVE   | for live | Live-trading API key. Required when ALPACA_PAPER=false. The paper key is intentionally NOT a fallback in live mode. |
| ALPACA_SECRET_KEY_LIVE | for live | Paired with ALPACA_API_KEY_LIVE. Required in live mode. |
| ALPACA_PAPER          | no       | `true` (default) routes to Alpaca paper. `false` switches to live, but live trades still require the trading-enabled flag. |
| ALPACA_STOCK_FEED     | no       | `iex` (free, default) or `sip` (paid). SIP needs an active Alpaca market-data subscription. Without one, every request raises "subscription does not permit querying recent SIP data" and the strategy tick throws. |
| ALPACA_OPTIONS_FEED   | no       | `indicative` (free, default) or `opra` (paid). Same failure mode as above. Indicative quotes are derived rather than true NBBO and some contracts carry no greeks, so strike coverage is thinner. |
| ENV                   | no       | `dev`, `staging`, or `prod`. Default `dev`.        |
| LOG_LEVEL             | no       | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default INFO. |
| TIMEZONE              | no       | IANA name. Default `Asia/Singapore`.               |
| ANTHROPIC_API_KEY     | for chat | Required for the conversational handler. Without it, free-form messages return a "not configured" reply and slash commands continue to work. |
| CHAT_MODEL            | no       | Override the chat model. Default `claude-sonnet-4-6`. |
| DATABASE_URL_RO       | for chat | Read-only DSN for the chat tool layer. Authenticate as `kai_chat_ro`. Without it, `query_supabase` fails closed. |
| KAI_CHAT_RO_PASSWORD  | bootstrap | Used by `scripts/create_chat_ro_role.py` to create or rotate the `kai_chat_ro` role. Not read at runtime by the bot. |
| HEARTBEAT_URL         | no       | Out-of-band liveness URL pinged after every successful strategy tick (e.g. healthchecks.io). When unset, the heartbeat is a no-op. |
| ACCOUNT_SNAPSHOT_INTERVAL_SECONDS | no | Cadence for the periodic account-snapshot writer. Default 3600s. Floored at 60s to avoid self-rate-limiting Alpaca. |
| DAILY_REPORT_UTC_TIME | no       | `HH:MM` UTC for the daily realized-P&L summary post. Default `23:55`. |
| DAILY_REPORT_ENABLED  | no       | `true` (default) or `false` to suppress the daily summary entirely. |
| WEEKLY_CHART_UTC_DAY  | no       | Weekday for the weekly equity chart, 0=Mon..6=Sun. Default `0`. |
| WEEKLY_CHART_UTC_TIME | no       | `HH:MM` UTC for the weekly chart post. Default `00:00`. |
| WEEKLY_CHART_ENABLED  | no       | `true` (default) or `false` to suppress the weekly chart entirely. |
| EODHD_API_KEY         | strongly recommended | EODHD Calendar API key. Primary earnings source for the live bot (`src/kai_trader/strategy/earnings.py`) with yfinance as fallback, and required by the backtest harness. Without it the live bot falls through to yfinance only; coverage gaps trigger fail-closed unknown-skips across the universe. |
| FINNHUB_API_KEY       | recommended | Finnhub earnings calendar (free tier). Third source in the earnings union with EODHD and yfinance; independent calendars cross-check so fail-closed unknowns and stale-date risk both shrink. |
| AI_DECISION_MODE      | no       | `off` (default) or `filter`. `filter` lets the AI decision layer TAKE/REJECT screened CSP candidates before the risk gate; failures fail closed for new entries. Position management never depends on it. |
| AI_DECISION_MODEL     | no       | Claude model id for the decision layer. Default `claude-sonnet-4-6`. Persisted with every decision. |
| AI_DECISION_TIMEOUT_SECONDS | no | Per-candidate request ceiling. Default 30. |
| AI_DECISION_TICK_BUDGET_SECONDS | no | Whole-tick AI ceiling; unevaluated candidates are rejected fail-closed. Default 120. |
| AI_DECISION_MAX_CONCURRENCY | no | Concurrent decision requests. Default 3. |
| AI_DECISION_CACHE_TTL_MINUTES | no | Decision reuse window per contract/regime/earnings/trend/premium bucket. Default 30. |
| PER_NAME_ECONOMIC_CAP_PCT | no | S2 assignment-aware per-name economic cap as a fraction of equity. Held shares at market value + open/working short-put face + the proposed put's face must fit under it before a new CSP is admitted. 0 disables (pre-S2 behaviour). Default 0.20. The dashboard service reads it too, for display only: it draws the cap line on the concentration bars. Unset there, it falls back to the same 0.20 default. |
| SLEEVE_ECONOMIC_CAP_MULT | no | S3 sleeve-level economic cap, as a multiplier on each sleeve's own `target_pct`. The sleeve budget otherwise counts short-put collateral only, so assigned shares escape it. 1.0 enforces the sleeve mandate against shares + put face; above 1.0 grants headroom. 0 disables (pre-S3 behaviour). Default 1.0. |
| DASHBOARD_TOKEN       | dashboard service | Access token for the read-only web dashboard (auto-generated by the Render blueprint). Not read by the bot. |
| DATABASE_URL_RO       | for chat + dashboard | Read-only DSN (kai_chat_ro). The bot's chat tool layer reads it AND the kai-trader-dashboard service needs the same value set manually once. |

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

Phases 1, 2, 2.5, 2.7, 2.8, 2.9, 3.1-3.6, 4, 5a, 5b, 5c, 5d, 5e, R1, A1, D1, U1, S1, S2, **and S3** shipped:

- Safety S3 (2026-08-28) closes the same assignment hole one level up,
  at the sleeve. `sleeve_remaining` was `equity * target_pct -
  committed_per_sleeve`, and `committed_per_sleeve` counts short-put
  collateral only, so assigned shares escaped the sleeve budget exactly
  as they escaped the per-name cap before S2. Measured at $30k over
  2024-03 to 2026-08: index_core held its PUT face inside its 35%
  mandate (peak 36.8%) while its true economic footprint ran at 46.2%
  of NAV mean and 72.2% peak. `apply_gate` now optionally budgets each
  sleeve's ECONOMIC exposure (sleeve-owned shares at market + its open
  and working put face + face accepted this tick), expressed as
  SLEEVE_ECONOMIC_CAP_MULT x the sleeve's own `target_pct` so the
  existing mandate is what gains teeth. Symbols on two whitelists are
  attributed to the first enabled sleeve, so no double counting.
  Oversized proposals shrink and only reject at zero, with reason
  `sleeve_economic_cap`, a structured log line and a per-sleeve
  counter. Shipped at 1.0: max drawdown 24.5% -> 18.2%, Sharpe 0.69 ->
  0.86, Sortino 1.03 -> 1.29, peak miner-pair exposure 47.1% -> 31.6%,
  index_core economic footprint 46.2/72.2 -> 30.6/44.5 (mean/peak),
  with CAGR (16.2 -> 15.8) and utilisation (12.0 -> 12.3) unchanged
  inside noise. Drawdown held at 18.2 / 18.2 / 14.2 across the main,
  capital-chaos and quarter-spread probes. The benefit concentrates at
  small accounts, where the imbalance is worst; at $100k the same cap
  moves drawdown 23.0 -> 22.3 for 0.7 CAGR points, and multiplier 1.25
  was worse than 1.0 everywhere tested. 0 disables and reproduces the
  pre-S3 gate.

- Safety S2 (2026-08-27) closes the assignment concentration loophole
  with a per-name ECONOMIC cap in the risk gate. The 12% notional cap
  counts short-put collateral only, so once a put assigned, the
  exposure left the risk budget and the strategy kept selling puts on
  the same falling name (drawdown forensics: one name reached 45% of
  NAV and caused 109% of the 39.6% backtest max drawdown). Now
  ``apply_gate`` admits a new CSP only while held shares at market
  value + open and working short-put face + face already approved
  this batch + the proposed put's face stays within
  PER_NAME_ECONOMIC_CAP_PCT of equity (default 0.20; 0 disables and
  reproduces the pre-S2 gate, pinned by golden parity). Oversized
  proposals are shrunk contract by contract; a fully capped one is
  rejected with the machine-readable reason ``economic_cap``, a
  structlog audit line carrying the full exposure breakdown, sleeve
  counters, and an always-visible tick-summary warning. Shares
  covered by short calls are counted once at market; already-above-
  cap positions are never liquidated, they just stop growing. The
  worker now fetches the long book BEFORE the CSP build (fail-closed
  when the fetch fails) and reuses it for the snapshot, CC builder,
  and render. Backtest harness parity: the runner marks long equity
  at asof closes and threads ``--econ-cap-pct`` (default 0.20; the
  experiment scripts pass 0 to reproduce pre-S2 baselines).
  Production-faithful validation (with-trend baseline, 2024-03..
  2026-08, pessimistic fills): max DD 28.7% -> 23.2%, pinned within
  21.0-23.2% across capital-chaos and kinder-fill probes (baseline
  swings 23.1-28.7%), peak single-name exposure 59.8% -> 32.9% of
  NAV (the residual above 20% is post-entry appreciation, which the
  cap deliberately does not trim), return within noise (+30/+16/+0.3
  points across probes). Known harness caveat: the backtest sizes
  the cap off cost-basis long equity (account_snapshot convention),
  so live production, which uses Alpaca market equity, is tighter
  than the backtest exactly when shares are under water.
- Safety S1 (2026-08-27) splits the market-risk freeze from the system
  kill. The drawdown breaker (7% off the 7-day high) no longer engages
  `kill_switch`; it flips `new_entries_enabled` off (actor -1) and
  fires one critical notification. Under the freeze: no new CSPs, no
  covered calls, no roll reopens (rolls were already entries-gated;
  the challenged put rides to assignment by design), while
  reconciliation, assignment detection, position snapshots,
  profit-takes, and `/close` all keep working. Each breached tick also
  requests broker cancellation of working risk-increasing orders
  (`open_short_put`, `open_covered_call`, `roll` reopen legs) via the
  new `broker.alpaca.cancel_order` (kill-gated, request-only:
  reconciliation stays the sole writer of terminal statuses, so a
  cancel race with a partial fill is recorded truthfully); close-side
  working orders are never touched. The sweep re-runs while the breach
  holds, so failures retry and restarts are safe; a still-breached
  account re-freezes on the next tick even if the operator re-enables
  entries. `kill_switch` is now manual-only (`/kill`) and stricter:
  no orders, closes, or cancels, but the killed tick still reconciles
  fills, records OPASN assignments, and persists position snapshots,
  so being killed never means being blind (killed-tick summaries also
  dropped from alert to info priority). Recovery: `/flag
  new_entries_enabled on` when ready; with entries off and kill off,
  management (profit-takes, rolls held by design, manual closes) runs
  without new entries. No auto-liquidation anywhere: the breaker
  freezes and escalates, the human decides (paper anomalies like the
  2026-07-07 assigned-stock wipe can fake a crater).
- Phase U1 (2026-08-27) adds the weekly universe review and web
  approvals. New `kai_trader/universe/` package: a curated candidate
  pool (`pool.py`, changed only by PR), a deterministic eligibility
  screen (`screen.py`: weekly put near the delta band, spread quality,
  strike fits the per-name cap at current equity, bid-yield floor,
  trend, known earnings calendar), and an AI curator pass
  (`prompts.py` v1.0.0, strict ADD/SKIP/KEEP/RETIRE verdicts in
  `models.py`, fail-closed to SKIP/KEEP). Guardrails: max 2 adds and 2
  retires per run, sleeve size 4-10, and nothing is ever applied
  directly; changes are filed as ordinary `pending_changes`
  watchlist_edit proposals (proposed_by -1) with the thesis in the
  reason. Runs weekly via `UniverseReviewWorker` (immediately at first
  boot, then every ~6.5 days) or on demand with `/universe_review`;
  every run lands in `weekly_reviews`. Web approvals: the dashboard
  shows Pending approvals with the add/retire diff and Approve/Reject
  buttons that INSERT a request into the new `web_actions` table
  (migration 037; kai_chat_ro gets SELECT+INSERT on that one table
  only). The bot's `WebActionWorker` is the sole executor: it
  revalidates the change is still pending and drives the same state
  machine as the Telegram buttons, so web and Telegram approvals
  cannot double-apply and the web service still holds no broker keys
  or write access to trading config.
- The dashboard front end was rebuilt from the Claude Design source on
  2026-08-28. `kai_trader/dashboard/theme.py` holds the design system
  verbatim (CSS, icon sprite, progressive-enhancement script) and
  `render.py` composes it: a mode-aware top bar, an alert stack for
  kill switch / live money / frozen entries / stale book, three flag
  tiles, a context strip, the approval card with an add-remove-keep
  diff, account stats, an interactive 7-day equity chart (hover, touch
  and arrow keys; gaps over 20 h shaded, so weeknight closes draw
  through), positions split into short options and assigned shares, a
  new concentration-by-underlying section drawing the S2 per-name
  economic cap, AI decision cards, and the orders table. Query
  failures render inside the section that owns them. Three defects in
  the design source were fixed rather than copied: sprite stroke
  attributes moved onto each `<symbol>` (a `<use>` clone does not
  inherit them from the sprite wrapper, so every line icon filled
  solid), `.kt-cell-tag` no longer uses `display:flex` (it took the
  `<td>` out of the table layout and merged the Action and Status
  columns), and copy that claimed the drawdown breaker releases itself
  now names `/flag new_entries_enabled on`, which is what S1 actually
  requires.
- Phase D1 (2026-08-27) adds the read-only web dashboard: a second
  Render service (`kai-trader-dashboard`, free tier, same Docker image,
  `dockerCommand: uv run python -m kai_trader.dashboard.main`) serving
  account stats, a 7-day equity chart, the latest position book,
  recent orders, and the last 20 AI decisions with theses. It reads
  Postgres only, as `kai_chat_ro` via `DATABASE_URL_RO`: no broker
  keys, no Telegram token, no write credentials. Access is gated by
  `DASHBOARD_TOKEN` (blueprint-generated); a one-time `?token=` visit
  moves the secret into an HttpOnly Secure cookie. Missing config
  serves a setup notice, never data. Positions come from the new
  `position_snapshots` table (migration 036): the strategy worker
  persists the book it already fetched each open-market tick,
  skipping the write whenever either fetch failed so a partial book
  is never recorded as whole; 7-day retention. The free instance
  spins down when idle, so the first visit after a quiet spell takes
  a few seconds to wake.
- Phase A1 (2026-08-26) is the first operational AI decision layer,
  governing NEW CSP entries only. New package `kai_trader/ai/`
  (models, prompts, context, providers, client, decision): the
  deterministic screener's ranked proposals pass through
  `AIDecisionEngine.evaluate_proposals`, which asks a Claude model
  (default `claude-sonnet-4-6`, configurable via AI_DECISION_MODEL,
  prompt version persisted) for a strictly validated TAKE/REJECT with
  confidence, wheel_suitability, event risk, fundamental view, risk
  flags, and thesis. Only TAKE candidates proceed, reordered by
  `final_score = quant_composite * wheel_suitability`, into the
  UNCHANGED deterministic risk gate and `ApprovedIntent` submission
  path. The hook is `ai_filter` on
  `build_approved_intents_with_diagnostics`; it can only shrink and
  reorder the screener's own proposals (foreign, duplicated, or
  mutated candidates are discarded). Modes via AI_DECISION_MODE:
  `off` (default; behaviour byte-identical to R1, pinned by parity
  tests) and `filter`. Every failure (timeout, malformed output,
  invalid enum, provider error, missing key, tick budget) fails
  CLOSED for new entries and never touches rolls, profit-takes,
  assignments, covered calls, or reconciliation. Every evaluation,
  TAKE and REJECT alike, is persisted to the new `ai_decisions` table
  (migration 035) with the full candidate packet, model/prompt
  lineage, tokens, latency, cost estimate, source freshness, and
  final pipeline disposition. Event context comes from yfinance
  headlines plus the existing earnings module, freshness-stamped,
  with EODHD degradation surfaced honestly. Telegram: the tick
  summary gains an "AI decisions" section and `/ai_status` shows
  mode, model, and today's counters. The AI package holds no broker
  imports and cannot construct `ApprovedIntent` (AST hygiene tests
  enforce this). Also ships the M4 security fix: Kai's chat
  `read_file`/`list_dir` refuse `.env*` (except `.env.example`), key
  and certificate files, and `.ssh`/`.aws`/`.gnupg` paths.
- Phase R1 (2026-08-26) is a behaviour-preserving safety refactor that
  prepares the repo for a future quant/AI decision layer. The cap
  matrix (total deployment, options buying power, per-name notional,
  contract ceiling, per-tick, per-day, cool-down, committed plus
  in-flight collateral) moved verbatim from `strategy/candidates.py`
  into the new `kai_trader/risk/gate.py`: a pure
  `apply_gate(proposals, RiskContext) -> GateResult` that returns
  `ApprovedIntent` values and machine-readable rejections. The worker's
  new-entry submission path accepts ONLY gate-issued `ApprovedIntent`
  (enforced by `mypy --strict` and a runtime guard), so any future
  producer must pass the gate to reach the broker. `candidates.py` is
  now screen+score only; `build_intents_with_diagnostics` keeps its
  exact signature and output as a screen-then-gate composition (the
  backtest, `/strategy_status`, and existing tests are unchanged; the
  cap constants are re-exported for back-compat). Each submitted entry
  now persists decision lineage in `orders.intent_payload`: a `reason`
  sentence plus `scores` (composite, annualised yield, spread pct, IV,
  trend, earnings, regime, bid/ask/mid, DTE). `StrategyWorker.tick` is
  serialised by a non-blocking Postgres advisory lock (key
  `KAI_TICK`), closing the H1 concurrency window between the scheduled
  loop, `/trade_now`, and Render deploy crossover; a contended tick
  skips safely and does not ping the liveness heartbeat. A golden
  parity test (`tests/test_gate_golden_parity.py` plus
  `tests/golden_gate_parity.json`, captured against the pre-refactor
  build) pins that no trading decision, quantity, rejection, or
  diagnostic changed.
- Phase 5e fixes a real bug: the cap math in
  `build_intents_with_diagnostics` used `equity * pct` without
  subtracting cash already locked in open short put positions, so
  the strategy kept re-attempting the same strikes every tick and
  Alpaca rejected each new submission with insufficient buying
  power. The fix: a new `_committed_collateral(short_puts,
  sleeves)` helper returns per-sleeve, per-symbol, and total
  locked dollars; the build function accepts `existing_short_puts`
  and clamps `sleeve_remaining`, `total_remaining`, and per-symbol
  headroom to zero after subtraction. The worker fetches via
  `list_short_option_positions` and passes through. CSPs now stop
  re-attempting positions you already hold.
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
    that high it engages the entry freeze (`new_entries_enabled` off,
    see Safety S1 above; it never touches `kill_switch`) and fires a
    `critical` notification. Idempotent: a breach while already frozen
    logs but does not re-set or re-notify.
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
