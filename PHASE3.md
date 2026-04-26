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

## Targets

- 3% per month return on equity (~36% annualised simple, ~42% compounded).
- Maximum drawdown under 10%.
- Weekly options as the trading vehicle (7 DTE).

These targets are stretch goals. Multiple sources are explicit that
3% per month is at the edge of what the wheel produces sustainably and
typically only happens in elevated-vol regimes (CBOE WPUT data shows
~37% gross premium per year for weekly puts on SPX, but net of
losses through drawdowns it lands much lower). Plan for 1.5-2.5%
average across a full cycle, with the high months covering low ones.
The parameter set below is the calibration that pushes toward 3% in
friendly regimes while clamping risk hard enough to keep drawdowns
inside the 10% floor.

## Calibrated decisions (research-backed, recalibrated 3.6)

The 30-45 DTE references in earlier sections of this document are
superseded by 7 DTE. The numbers below are the active values for
sub-phases 3.2 onward, with Phase 3.6 changes marked.

### Phase 3.6 recalibration (active set)

The original 3.2 calibration was too conservative for a $100k account
at current SPY prices: SPY/QQQ/META each require ~$50-70k of
collateral per contract, and a 40% sleeve cap on $100k allowed at most
1 contract from those names — leaving the strategy under-deployed.
The recalibration loosens the cap matrix and broadens the whitelists
so the strategy can actually push toward 3% per month.

**What changed**:

- **Allocations**: 25 / 30 / 45 (was 40 / 40 / 20). Opportunistic gets
  the largest share because that is where the IV juice lives.
- **Target deltas**: -0.40 risk_on, -0.30 neutral (was -0.30 / -0.20).
  Higher deltas trade larger assignment probability for materially
  more premium per dollar of collateral.
- **Multi-contract per symbol** allowed up to a per-symbol
  concentration cap of **15% of equity** and a hard ceiling of
  **10 contracts per symbol per cycle**. Cheap names (F, SOFI, PLTR)
  now contribute multiple contracts of premium instead of one each.
- **Total deployment cap**: 70% of equity in CSP collateral (was 60%).
- **Opportunistic stays active in neutral** (was paused). Lower delta
  in neutral keeps it bounded; pausing the highest-IV sleeve was a
  major drag on average yield.
- **Roll trigger**: 0.50 absolute delta (was 0.45). Let trades work
  harder before paying to close.
- **`new_entries_enabled` flag is now wired** as the third gate inside
  `submit_short_put`. Migration 010 set the existing seeded value to
  true so the bot can submit; operator can flip it off to pause new
  entries while keeping rolls/closes alive.
- **Symbol whitelists expanded**:
  - `index_core`: SPY, QQQ, IWM, DIA
  - `stable_largecap`: AAPL, MSFT, GOOGL, AMZN, META, V, JPM, BAC,
    DIS, KO, F, T, PFE, C
  - `opportunistic`: NVDA, AMD, TSLA, AVGO, COIN, PLTR, SOFI, MARA,
    MU, BABA, SMCI, MSTR, RIOT, SNAP

**Honest math on the new calibration**:

For a 5-contract weekly portfolio at -0.30 to -0.40 delta on
$100k equity at ~60-70% deployment, expected weekly premium ranges
~0.6-1.0% on equity, which compounds to ~2.5-4% per month in
friendly regimes. Hostile regimes (risk_off) produce zero new
premium and the open positions decay or get rolled. Average over a
full cycle: ~2-2.5%, with friendly months hitting 3% and hostile
months landing closer to 0-1%.

The 7% drawdown circuit breaker is unchanged. The expanded delta
exposure is offset by the per-symbol concentration cap (15% of
equity max in any single underlying) and the breaker's ability to
auto-engage `kill_switch` on a fast-moving drawdown.

### 1. Wheel parameters

- **DTE**: 7-10 days. Sell Monday-Wednesday for the upcoming Friday
  weekly expiration.
- **Target delta puts**: -0.30 in `risk_on`, -0.20 in `neutral`, no
  new entries in `risk_off`. Higher delta in friendly regimes is
  what makes 3% per month plausible; lower delta in neutral keeps
  drawdown bounded.
- **Target delta calls**: 0.20 (defensive on the upside, never below
  cost basis). Same in all regimes.
- **Profit take**: 50% of credit received. 7 DTE means 50% of premium
  often arrives within 2-3 days, freeing the collateral to redeploy
  into the next cycle.
- **Roll trigger**: option delta crosses 0.45 (in absolute terms). At
  7 DTE deltas move fast; rolling at 0.40 like the textbook 30-45 DTE
  number would over-roll.
- **Hold-to-expiry rule**: if a position is untested at Thursday
  close, hold through Friday rather than buy back for the last few
  cents. The gamma risk is real but the leftover premium is
  meaningful at this DTE.

### 2. Sleeve allocation

40% / 40% / 20% (revised from the original 50/35/15). The 3% target
needs `stable_largecap` doing real work; weighting it equal to
`index_core` increases the average premium yield while the
`opportunistic` 20% provides the upside in friendly regimes.

### 3. Sleeve symbol whitelists

All names below have deep weekly options liquidity (tight spreads,
high open interest), which matters more for weekly cycles than for
monthlies.

- `index_core`: SPY, QQQ, IWM
- `stable_largecap`: AAPL, MSFT, GOOGL, AMZN, META, V, JPM
- `opportunistic`: NVDA, AMD, TSLA, AVGO, COIN

`stable_largecap` deliberately excludes traditional defensives
(JNJ, KO, WMT) because their IV is too low to pull the average
yield toward 3% per month. They can be added back later as a
defensive tilt if drawdowns run hot.

### 4. Regime thresholds

- `risk_off`: VIX > 25 OR SPY < 50dma OR VIX 5-day change > +30%.
  Tightened from the original spec to add the VIX-spike detector;
  weekly put sellers get hurt fastest by sudden vol expansion.
- `risk_on`: VIX < 17 AND SPY > 20dma AND realized vol < 15. VIX
  cap lowered from 18 to 17 to be slightly more selective about
  full-delta entries.
- `neutral`: otherwise.

### 5. Risk controls

- **Per-symbol cap**: max 1 short put per symbol per weekly cycle.
  This replaces the original 10% dollar cap because at $100k equity
  with SPY at $500/share, even one SPY contract requires ~$50k of
  collateral and a percentage cap would forbid all index trades.
- **Per-sleeve dollar cap**: sleeve % times equity, hard ceiling.
- **Total open premium cap**: 60% of equity in cash-secured put
  collateral. Higher than the original 25% because the 3% target
  requires most of the capital to be deployed at any given time.
  Weekly cycling means turnover is high so this is not "always 60%
  exposed", it is "up to 60% deployed at any moment".
- **Per-cycle entry cap**: at most 7 new positions in any one weekly
  cycle. Prevents the worker from filling 12 positions in a single
  morning if the regime flips friendly.
- **Drawdown circuit breaker**: if equity drops 7% from prior week's
  high (read from `account_snapshots`), the worker auto-engages
  `kill_switch` and fires a `critical` notification. 7% sits between
  the comfortable operating band and the 10% hard limit; gives the
  owner time to investigate before manual stop-out.

### 6. Tick interval

5 minutes during US regular market hours (09:30-16:00 ET, Mon-Fri).
Faster than 15 min because at 7 DTE every hour matters for hitting
the 50% profit-take target before the next move.

### 7. Opportunistic sleeve when paused

Capital sits in cash. Not redistributed to other sleeves; the
sleeve lines stay clean and the cash is available the moment regime
flips back to `risk_on`.

### 8. Auto kill-switch on 7% drawdown breach

Yes, auto. The strategy worker flips `kill_switch=true` and fires a
`critical`-priority notification. Operator can review the situation
and clear the kill switch via `/flag kill_switch off` once
satisfied.

### 9. Position sizing

- One contract per qualifying symbol per weekly cycle (no doubling
  up). The worker greedily ranks candidates by yield (premium per
  dollar of collateral) within the target-delta band and submits
  until the per-sleeve cap or the 60% total deployment cap is hit.
- Skip a candidate if its single-contract collateral exceeds the
  remaining sleeve headroom. The cash stays unused that week rather
  than forcing into a smaller name and breaking the sleeve discipline.

### What this set of choices implies

- Friendly week (`risk_on`, ~7-10 positions filled at delta 0.30,
  IV 18-22): expected weekly yield ~0.7-0.9% on deployed capital,
  around 0.4-0.6% on total equity. Hits 3% monthly when full month
  is `risk_on`.
- Mixed week (`neutral`, fewer positions, delta 0.20): ~0.3-0.5%
  weekly on equity. Months with two weeks neutral and two weeks
  risk_on land near 2%.
- Hostile week (`risk_off`, no new entries, manage existing only):
  zero new premium; existing positions either profit-take or get
  rolled. Months with multiple risk_off weeks land near 1% or less.

The 7% drawdown circuit breaker plus weekly cycling means the worst
plausible run is a couple of bad fills before the brake kicks in,
which is what keeps the 10% drawdown ceiling realistic.

## What this spec still does not decide

- IV rank entry filter (e.g. only sell when IVR > 30). Worth
  considering as a 3.5 enhancement once we have a few weeks of
  paper data.
- Earnings-window blackouts (skip names with earnings in the next
  7 days). Recommended for `stable_largecap` and `opportunistic` in
  3.5; safe to skip for `index_core`.
- Multiple concurrent expirations on the same symbol. The
  one-contract-per-symbol-per-cycle rule above implicitly forbids
  this; if we want to change it later we will.
- Pinning behaviour at expiration. Defer.
