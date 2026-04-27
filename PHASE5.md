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

- [ ] 5b.1 Broker primitive: `submit_buy_to_close` (gated by `kill_switch` only)
- [ ] 5b.2 Profit-take evaluator module + tests
- [ ] 5b.3 Worker integration
- [ ] 5b.4 Order linking (intent_payload.original_order_id)
- [ ] 5b.5 Quality gates + docs + deploy

## Phase 5c — TradingStream + Real-Time Notifications

**Acceptance**: When an Alpaca order fills, you receive a Telegram notification within ~5s. When a put gets assigned, the bot detects via stream and triggers CC build immediately.

- [ ] 5c.1 Stream worker scaffolding (`streams/trading_stream.py`)
- [ ] 5c.2 Event routing (fill / partial_fill / canceled / expired / replaced)
- [ ] 5c.3 Reconciliation refactor (drift check at 10 min cadence)
- [ ] 5c.4 Bot integration (wire into main.py startup/shutdown)
- [ ] 5c.5 Failure modes (auth fail, repeated reconnects, silent stream)
- [ ] 5c.6 Tests
- [ ] 5c.7 Quality gates + docs + deploy

## Phase 5d — Earnings Blackout

**Acceptance**: For every CSP candidate, bot checks whether underlying has earnings inside DTE. If yes, skipped with diagnostic surface. Earnings dates cached 24h.

- [ ] 5d.1 Earnings data source module (yfinance, fail-open)
- [ ] 5d.2 Filter integration in `build_intents_with_diagnostics`
- [ ] 5d.3 Configuration (`earnings_blackout_enabled` column on `sleeve_config`, default true)
- [ ] 5d.4 Quality gates + docs + deploy

## What is explicitly NOT in Phase 5

- Postgres LISTEN/NOTIFY for dispatchers
- Per-position option quote streaming
- Account/equity streaming for drawdown breaker
- IV rank filter
- Backtest harness
- Dashboard UI
- BABA-style adjusted symbol log silencing (cosmetic)
- `/trade_now` chain-fetch parallelization (cosmetic)
