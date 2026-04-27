# Phase 5 — Wheel Completeness + Real-Time Notifications

Tracking checklist for the Phase 5 bundle. Tick boxes as items land.

## Scope and ordering

Four shippable units, each leaves the bot working on its own.

1. **5a — Covered Calls + Assignment Detection** (closes the wheel loop)
2. **5b — Profit-Take Execution** (premium capture)
3. **5c — TradingStream** (real-time fills + assignment notifications)
4. **5d — Earnings Blackout** (risk filter)

## Architectural principles

- Every order placement remains gated by `trading_enabled` + `kill_switch` at the broker layer.
- Every state mutation writes to `orders` + `decision_log`. Audit trail is sacred.
- Assignment detection has a polling fallback after streaming lands.
- New `action` types in `orders` are additive — no schema break.
- Tests keep ≥80% coverage. Each phase ships with its own test pack.
- Conventional commits per phase.

---

## Phase 5a — Covered Calls + Assignment Detection

**Acceptance**: When a CSP gets assigned and the bot now holds 100 shares, the next strategy tick automatically submits a covered call at the sleeve's call delta target within the DTE band, logged to `orders` + `decision_log`, surfaced in the next tick notification.

- [x] 5a.1 Schema check + migration 015 (action column extended)
- [x] 5a.2 Broker primitive: `submit_short_call`
- [x] 5a.3 Position fetch helpers: `list_long_equity_positions`
- [x] 5a.4 Assignment detection module + tests
- [x] 5a.5 Covered call candidate builder + tests
- [x] 5a.6 Worker integration (CCs in tick, summary line)
- [ ] 5a.7 Bot surfacing (`/strategy_status`, `/positions`, `/recent_trades`) — deferred until first assignment lands
- [x] 5a.8 Approvals integration verification (no behavior change needed)
- [x] 5a.9 Quality gates (pytest 427 passing, 91% coverage, ruff/mypy clean)
- [x] 5a.10 Docs (CLAUDE.md, TRACKER.md)
- [x] 5a.11 Deploy (commit + push)

## Phase 5b — Profit-Take Execution

**Acceptance**: When an open short put's current ask reaches ≤ (1 - profit_take_pct) × original_credit, the bot submits a buy-to-close limit order at the ask, logged with `action='profit_take_close'`.

- [x] 5b.1 Broker primitive: `submit_buy_to_close` + `list_short_option_positions`
- [x] 5b.2 Profit-take evaluator module + tests
- [x] 5b.3 Worker integration (`_handle_profit_takes`, `_submit_close_intent`)
- [x] 5b.4 Order linking (intent_payload.original_order_id, captured_pct, current_ask)
- [x] 5b.5 Quality gates (442 passing, 91% coverage), migration 016 applied, docs updated

## Phase 5c — TradingStream + Real-Time Notifications

**Acceptance**: When an Alpaca order fills, you receive a Telegram notification within ~5s. When a put gets assigned, the bot detects via stream and triggers CC build immediately.

- [x] 5c.1 Stream worker scaffolding (`streams/trading_stream.py`)
- [x] 5c.2 Event routing (fill / partial_fill / canceled / expired / rejected)
- [ ] 5c.3 Reconciliation refactor — deferred. Current `_reconcile_pending` runs each tick as belt-and-suspenders; revisit if we observe drift in production.
- [x] 5c.4 Bot integration (wired into `bot/main.py` startup/shutdown)
- [x] 5c.5 Failure modes (exponential backoff up to 60s, broad-except handler, heartbeat every 60s)
- [x] 5c.6 Tests (17 new: extract / status mapping / handler routing / lifecycle / DB SQL paths)
- [x] 5c.7 Quality gates (459 passing, 90% coverage), docs updated

## Phase 5d — Earnings Blackout

**Acceptance**: For every CSP candidate, bot checks whether underlying has earnings inside DTE. If yes, skipped with diagnostic surface. Earnings dates cached 24h.

- [x] 5d.1 Earnings data source module (yfinance, fail-open, 24h cache)
- [x] 5d.2 Filter integration in `build_intents_with_diagnostics` (per-symbol pre-chain skip)
- [x] 5d.3 Configuration (`earnings_blackout_enabled` column on `sleeve_config`, default true via migration 017)
- [x] 5d.4 Quality gates (471 passing, ruff + mypy clean), docs updated

## Phase 5e — Collateral accounting (open positions reduce caps)

**Bug**: `build_intents_with_diagnostics` uses `equity * target_pct` and `equity * TOTAL_DEPLOYMENT_CAP_PCT` without subtracting the collateral already locked in open short put positions. Strategy keeps trying to open the same strikes every tick; Alpaca rejects with insufficient buying power.

**Acceptance**: cap math reflects reality. After 5e, opening positions are added to a "committed" total and subtracted from sleeve / total / per-symbol headroom before new candidates are sized. The persistent "Failed: 2 (AMZN P250, AVGO P400)" pattern stops; strategy either finds different names with remaining headroom or correctly reports zero candidates.

- [x] 5e.1 Helper: `_committed_collateral(short_puts, sleeves)` returns per-sleeve, per-symbol, total
- [x] 5e.2 `build_intents_with_diagnostics` accepts `existing_short_puts` and subtracts from sleeve / total / per-symbol caps
- [x] 5e.3 `_max_qty_for` accepts `per_symbol_remaining` (not just cap)
- [x] 5e.4 Worker fetches existing short puts and passes through
- [x] 5e.5 Tests: 7 new (committed reduces sleeve / total / per-symbol cap, unrelated underlying not blocked, default empty list, helper map correctness, ignores non-puts)
- [x] 5e.6 Quality gates (478 passing, ruff + mypy clean), docs updated, pushed

## What is explicitly NOT in Phase 5

- Postgres LISTEN/NOTIFY for dispatchers
- Per-position option quote streaming
- Account/equity streaming for drawdown breaker
- IV rank filter
- Backtest harness
- Dashboard UI
- BABA-style adjusted symbol log silencing (cosmetic)
- `/trade_now` chain-fetch parallelization (cosmetic)
