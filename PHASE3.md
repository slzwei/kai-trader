# Phase 3 spec: wheel strategy on Alpaca paper

Status: draft, awaiting owner sign-off on the load-bearing decisions
listed at the end of this document.

## Goal

Ship the actual trading engine. The bot autonomously runs a defensive
options wheel on Alpaca paper, the owner monitors and intervenes via
Telegram, and every action is gated by the three system flags from
Phase 2.5 (`trading_enabled`, `new_entries_enabled`, `kill_switch`).

## In scope

1. Options chain data fetch via Alpaca's `OptionHistoricalDataClient`.
2. Wheel strategy module that:
   - Selects candidate symbols per sleeve (config in DB).
   - Picks strikes via target-delta and DTE rules.
   - Sells cash-secured puts when no short put on the name is already open.
   - Sells covered calls when assigned shares from a put.
   - Closes early at the configured profit-take threshold.
   - Rolls when the underlying option goes "challenged" (delta crosses a threshold).
3. Regime classifier that biases entries (skip new entries when hostile).
4. Risk controls: per-sleeve dollar caps, total open-premium cap, per-symbol
   exposure cap, equity drawdown circuit breaker.
5. Order placement via the trading client. The current Phase 2 wrapper is
   read-only; Phase 3 extends it with `submit_order`, `cancel_order`,
   `close_position`. These methods read the system flags before sending.
6. Position lifecycle tracking in the existing `positions` table from
   migration 004 (open, assigned, expired, rolled, closed).
7. Notification firing: every order intent, every submit, every fill,
   every assignment, every regime transition. All flow through the
   Phase 2.7 worker.
8. Bot commands: `/strategy_status`, `/trade_now`, `/close <symbol>`,
   `/sleeves`, `/regime`, `/chain`, `/recent_trades`.

## Out of scope (still)

- Live (non-paper) trading. `ALPACA_PAPER=true` remains the default and
  Phase 3 does not change that.
- Multi-leg strategies. Spreads, iron condors, calendars are deferred.
  Phase 3 is single-leg wheel only.
- Tax-aware logic (wash sales, cost-basis lot selection).
- Web dashboard.
- SMS or webhook notification deliverers.

## Strategy mechanics

### The wheel itself

Defaults below are starting points, every value lives in `sleeve_config`
and can be tuned per sleeve.

**Cash-secured puts** (the entry leg)

- Eligible when: no short put already open on the symbol, sleeve has cash
  headroom, regime is not `risk_off`, and `new_entries_enabled` is true.
- Strike: closest available to target delta of `-0.20` (about 80% probability OTM).
- DTE: prefer 30-45 days, monthly expirations preferred over weeklies.
- Sizing: 1 contract per `sizing_unit` dollars of sleeve equity (default
  `sizing_unit = sleeve_capital / 5`). Capped by per-symbol exposure rule.
- Profit take: close at 50% of max profit (i.e. buy back at 50% of credit received).
- Roll trigger: when option delta crosses `-0.40` (challenged).
- Roll target: same or further OTM, same or later expiration, net credit only.
  If no net-credit roll exists, hold and let assignment happen.

**Covered calls** (after put assignment)

- Eligible when: holding shares from a prior put assignment.
- Strike: target delta `+0.20` to `+0.30`. Hard floor: never below the
  share cost basis.
- DTE: 30-45 days.
- Profit take: 50% of max profit.
- Roll trigger: delta crosses `+0.40`. Roll up and out, net credit only.
  If no net-credit roll, let the call get assigned (called away).

**Cycle**

put sold → put expires worthless OR put assigned → if assigned, sell
covered calls → call expires worthless OR call assigned (shares called
away) → back to selling puts.

### Sleeves

Three sleeves, each a fixed percent of total equity. Operator can edit
the percentages or symbol lists in the `sleeve_config` table.

| Sleeve            | Default % | Suggested symbols          | Posture                                      |
|-------------------|-----------|----------------------------|----------------------------------------------|
| `index_core`      | 50%       | SPY, QQQ, IWM              | Steady premium, never sell calls below cost. |
| `stable_largecap` | 35%       | AAPL, MSFT, GOOGL, JNJ, KO | Higher premium on quality balance sheets.    |
| `opportunistic`   | 15%       | NVDA, AMD, etc (rotating)  | Higher delta targets, smaller positions.     |

When `opportunistic` is paused (regime != `risk_on`), its capital sits in
cash; it is not reallocated to other sleeves.

### Regime classifier

Three states: `risk_on`, `neutral`, `risk_off`.

Inputs (recomputed each tick):

- SPY price vs 20-day SMA.
- SPY price vs 50-day SMA.
- VIX level.
- VIX 5-day change.
- SPY 10-day realized volatility.

Output rules (evaluated in order):

1. `risk_off` if VIX > 25 OR SPY < 50dma.
2. `risk_on` if VIX < 18 AND SPY > 20dma AND realized vol < 15.
3. `neutral` otherwise.

Behaviour map:

- `risk_off`: skip all new entries (manage existing only). Worker also
  flips `new_entries_enabled=false` so the gate is doubly enforced.
- `neutral`: target deltas reduced by 0.05 in absolute value;
  `opportunistic` sleeve paused.
- `risk_on`: full deltas, all sleeves active.

Regime is appended to `regime_history` on every transition; a notification
fires.

### Risk controls

- **Per-symbol exposure cap**: max 10% of equity in any single underlying
  (sum of share value, short put collateral, short call notional).
- **Per-sleeve cap**: derived from the sleeve % of equity.
- **Total open premium cap**: short put collateral cannot exceed 25% of
  equity. Once breached, no new puts.
- **Drawdown circuit breaker**: if equity drops 5% from the prior week's
  high (read off `account_snapshots`), the worker auto-engages
  `kill_switch` and fires a `critical` notification.

### Tick loop

A new `StrategyWorker` (same pattern as `NotificationWorker`) runs every
5 minutes during US market hours (09:30-16:00 ET, weekdays). On each tick:

1. Refresh account state and write a row to `account_snapshots`.
2. Pull open Alpaca positions and reconcile with our `positions` table.
   Mark anything assigned/expired since last tick.
3. Compute regime, append to `regime_history` if changed, notify on transition.
4. Apply the drawdown circuit breaker.
5. For each sleeve in `risk_on` or `neutral`: build the candidate set
   (whitelist symbols with no existing short put).
6. For each candidate: fetch the option chain, pick the strike via
   target-delta search, build an order intent.
7. Check `trading_enabled`, `kill_switch`, `new_entries_enabled` (in that
   order). Record the intent in `orders` regardless. If the gates pass,
   submit. If not, mark the intent `skipped` with reason.
8. For each existing open position: check profit-take, roll trigger,
   expiration. Submit closes/rolls as needed.
9. Fire notifications for every state change worth surfacing.

The flag check is the last gate before any submit. Strategy code never
bypasses it.

## New database surface

- **Migration 006**: `sleeve_config`. Columns: `sleeve` (PK matching the
  three known sleeves), `target_pct` (numeric), `target_delta_put`,
  `target_delta_call`, `target_dte_min`, `target_dte_max`, `profit_take_pct`,
  `roll_trigger_delta`, `sizing_unit_divisor`, `symbol_whitelist` (jsonb
  array of strings), `enabled` (bool), `updated_at`, `updated_by`.
  Seeded with the three sleeves and the defaults from this spec.
- **Migration 007**: `regime_history`. Columns: `id`, `captured_at`,
  `regime`, `vix`, `vix_5d_change`, `spy_price`, `spy_20dma`, `spy_50dma`,
  `realized_vol_10d`, `notes`. One row per regime transition (not every tick).
- **Migration 008**: `orders`. Columns: `id`, `created_at`, `symbol`,
  `action` (`open_short_put` / `open_covered_call` / `close` / `roll`),
  `intent_payload` (jsonb: strike, expiration, qty, target_delta, etc),
  `position_id` (nullable, FK to `positions`),
  `alpaca_order_id` (nullable),
  `status` (`pending` / `submitted` / `filled` / `cancelled` /
  `skipped_by_flag` / `failed`),
  `gating_decision` (jsonb: which flags were checked and what they read),
  `submitted_at`, `filled_at`, `error_text`.

## Extensions to existing modules

- `kai_trader/broker/alpaca.py`: add `submit_order`, `cancel_order`,
  `close_position`. Each one reads `system_flags.get_all_flags` first
  and refuses to send if `kill_switch` or `not trading_enabled`. Refusal
  is not an error: it returns a typed result indicating which gate
  rejected. Callers (the strategy worker) record the gating decision in
  the `orders` table.
- `kai_trader/broker/options_data.py`: new module wrapping
  `OptionHistoricalDataClient`. Methods: `get_chain(symbol, expiration)`,
  `get_chain_snapshot(symbol)`, `get_latest_option_quote(option_symbol)`.

## New bot commands

- `/strategy_status` — current regime, per-sleeve open premium, candidate
  trades the next tick would consider, gate states. Read-only.
- `/trade_now` — force an immediate strategy tick (audit-logged).
- `/close <symbol>` — close all open positions on a symbol. Requires a
  `/close_confirm <symbol>` follow-up within 30s.
- `/sleeves` — sleeve config and current per-sleeve allocation.
- `/regime` — current regime classifier inputs and output.
- `/chain <symbol> [YYYY-MM-DD]` — option chain snapshot for the symbol
  (and optionally a specific expiration).
- `/recent_trades [N]` — last N rows from the `orders` table.

## Sub-phases

Each is a session, each merges its own conventional-commit chain.

- **3.1**  Options data wrapper. `OptionHistoricalDataClient` wrapped,
           `/chain` command. No strategy yet.
- **3.2**  Sleeve config + regime classifier. Migrations 006, 007.
           `/sleeves`, `/regime` commands. No order placement.
- **3.3**  Strategy tick loop in dry-run mode. `StrategyWorker` runs and
           builds intents, but `submit_order` is not added yet so nothing
           goes to Alpaca. Worker fires notifications describing what it
           would have done.
- **3.4**  Order placement gated by flags. Migration 008. Add
           `submit_order`, `cancel_order`, `close_position` to the broker
           module. Wire the worker to actually submit. `/trade_now`,
           `/close`, `/recent_trades`.
- **3.5**  Roll logic + drawdown circuit breaker + tighter regime tuning.

## Acceptance criteria for Phase 3 complete

- All five sub-phases shipped.
- A live integration test (gated behind `ALPACA_INTEGRATION_TEST=1` and
  a new `ALPACA_PHASE3_LIVE=1` to be extra explicit) places a real paper
  order, observes the fill, writes the position, and closes it.
- `ruff check`, `mypy --strict src/`, `pytest` all green.
- Coverage stays at 80% or above.
- `/strategy_status` returns sensible output during market hours.
- An overnight paper run (or weekend equivalent) goes through at least
  one full cycle: put sold, expired or assigned, calls sold (if assigned),
  call expired or assigned, back to puts.

## Load-bearing decisions awaiting owner sign-off

These are the choices the spec depends on. If any are wrong, the spec
needs an edit before 3.1 starts.

1. **Wheel parameters**: target delta -0.20 puts and 0.20-0.30 calls,
   30-45 DTE, 50% profit take, 0.40 roll trigger. OK?
2. **Sleeve allocation**: 50% `index_core`, 35% `stable_largecap`,
   15% `opportunistic`. OK?
3. **Sleeve symbol whitelists**: defaults above (SPY/QQQ/IWM,
   AAPL/MSFT/GOOGL/JNJ/KO, NVDA/AMD). OK or want different names?
4. **Regime thresholds**: VIX 18/25 boundaries, SPY vs 20dma/50dma,
   realized vol 15. OK?
5. **Risk controls**: 10% per-symbol cap, 25% total short-put collateral,
   5% weekly drawdown circuit. OK?
6. **Tick interval**: 5 minutes during US market hours. OK or want longer
   (15 min) to reduce API load and avoid over-trading?
7. **Opportunistic sleeve when paused**: capital sits in cash. OK?
8. **Auto kill-switch on drawdown breach**: yes, the worker flips
   `kill_switch=true` on its own when the 5% breach hits. OK or want
   the worker to only notify and let the owner pull the lever?
9. **Position sizing formula**: 1 contract per (sleeve_capital / 5)
   dollars. So a 50% sleeve on $100k equity is $50k, sized at $10k per
   contract. OK starting point or want a different unit divisor?

## What this spec deliberately does not decide

- IV rank entry filter (e.g. only sell when IVR > 30).
- Pinning behaviour very near expiration.
- Multiple concurrent expirations on the same symbol.
- Earnings-window blackouts.

These are noted as future tuning levers and will be picked up if the
backtest or paper run shows we need them.
