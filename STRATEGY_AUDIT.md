# Strategy Audit and Forward Plan

Recorded on `claude/audit-volatility-wheel-strategy-YmI5i`. Read-only audit
of the strategy, execution, and risk surface against an aggressive
volatility-harvesting wheel mandate. No code changes accompany this doc;
the items in [Tier 1](#tier-1-must-fix-before-live-trading) and
[Tier 2](#tier-2-high-edge-and-survivability) are the proposed work plan.

## 1. Executive Summary

Infrastructure is genuinely production-grade for a single-operator bot:
typed config, structured logging, idempotent migrations, multi-layer
audit trail, kill-switch and drawdown breakers, watchdog, event
dispatcher, both polling and streaming fill reconciliation, and an LLM
chat surface gated behind `pending_changes`.

The strategy itself does not match the stated brief.

* The intended universe is mega-cap high-IV liquid names (NVDA, TSLA,
  META, AMZN, MSFT, AVGO, GOOGL). The live whitelist after
  `migrations/018_small_account_pool.sql` contains none of those. It is
  a 30-name pool of low-priced names plus sector ETFs.
* Three live-money execution defects are blocking:
  1. Roll path is broken. `_execute_roll` calls
     `close_position(roll.underlying)` (the equity ticker) instead of
     the OCC option symbol, and re-opens at `qty=1` regardless of
     challenged size.
  2. Drawdown breaker disables exposure-reducing trades. When the 7%
     drawdown trips `kill_switch`, profit-takes, rolls, and even close
     orders refuse to fire.
  3. Limit submission at chain mid with TIF=DAY on 25%-spread cheap
     names will under-fill, and the cool-down then locks the symbol
     out of re-entry. Realised fill rate is unmeasured.
* Calibration is conservative for an aggressive-growth mandate. 10%
  per-tick + 30% per-day deployment caps and a 6-tick cool-down imply
  ~7 trading days from cold start to 70% deployment. `risk_off` blocks
  all new entries during the highest-IV regimes.

Verdict: **structurally flawed for the stated objective**, mostly
because the universe and the regime gating contradict the stated goal,
and because the roll execution path is broken. Survivable for paper
trading; should not be flipped to live capital before
[Tier 1](#tier-1-must-fix-before-live-trading) lands.

## 2. Repository Map

* `src/kai_trader/strategy/` -- strategy core (`worker.py`,
  `candidates.py`, `rolls.py`, `profit_take.py`, `covered_calls.py`,
  `assignment.py`, `drawdown.py`, `regime.py`, `indicators.py`,
  `iv_rv.py`, `earnings.py`, `clock.py`, `render.py`).
* `src/kai_trader/broker/` -- Alpaca trading client, market data,
  options chain.
* `src/kai_trader/streams/trading_stream.py` -- WebSocket fill
  reconciliation.
* `src/kai_trader/db/` -- asyncpg helpers and 21 migrations.
* `src/kai_trader/observability/` -- daily report, weekly chart,
  snapshot writer, watchdog, heartbeat, dependency probe, flags-nag.
* `src/kai_trader/bot/` -- Telegram entrypoint and command handlers.
* `src/kai_trader/chat/` -- LLM-driven conversational layer
  (read-only DB role plus `pending_changes` write path).

## 3. Trade Decision Lifecycle (per 5-minute tick)

1. `StrategyWorker.tick` reconciles pending Alpaca orders.
2. Fetch clock; skip if closed.
3. `check_drawdown` trips `kill_switch` if equity is at or below 93% of
   the 7-day high-water mark.
4. If `kill_switch` is on, emit alert and return.
5. `compute_and_record` regime (VIX plus SPY 20/50 SMA plus 10d RV).
6. `evaluate_rolls` closes plus reopens for net credit; held otherwise.
7. `evaluate_profit_takes` BTC at ask if the current ask is at or below
   `original_credit * (1 - profit_take_pct)`.
8. `build_intents_with_diagnostics` ranks CSP candidates after IV/RV,
   earnings, cool-down, contract-ceiling, and dollar-cap filters.
9. `submit_short_put` at chain mid, TIF=DAY.
10. `detect_assignments` audits the case where shares match a filled CSP.
11. `build_call_intents` writes covered calls on every assigned
    underlying.
12. Render Telegram tick summary.

## 4. Current Strategy Assessment

### 4.1 Universe (CRITICAL DEVIATION)

`migrations/018_small_account_pool.sql` is the live state. The whitelist
collapses three sleeves into one and replaces the mega-cap pool with:

```
F, T, BAC, PFE, KO, KVUE, VZ, INTC, CSCO, GE, KMI, KHC, MO, WBA,
HOOD, SOFI, PLTR, MU, MARA, RIOT, SNAP, RIVN,
WFC, GM, C,
GDX, SLV, XLF, XLE, EEM
```

The brief named NVDA, TSLA, META, AMZN, MSFT, AVGO, GOOGL, COIN. **None
of those are present.** The pool is a mix of slow-mover defensives,
cyclical financials, speculative low-priced single names, and sector
ETFs. Selling MARA and RIOT puts is closer to "directional speculative
income" than "defensive vol harvesting on blue chips." The strategy
that runs is therefore not the strategy that was specified.

The single justification in the migration file is small-account fit
("$25k accounts can't deploy a single SPY/QQQ/AAPL CSP"). That is a
real constraint; the response (drop mega-caps entirely and replace with
low-priced specs) is the wrong response. See
[AUM-aware dynamic universe](#a-aum-aware-dynamic-universe-tier-2-1)
for the proposed fix.

### 4.2 Strike and DTE Selection

* DTE band 7-10 days. Consistent with the high-rotation goal.
* Target delta -0.40 in `risk_on`, -0.30 in `neutral`. Aggressive but
  defensible.
* Selection is `argmin(|delta - target|)` within DTE band. Pure,
  deterministic.
* `MIN_BID_PREMIUM = $0.15` floors out scrap premium.
* Score is `annualised_yield * spread_quality`, with a hard drop at
  spread/mid >= 30%. Reasonable baseline. Does not consider IV rank or
  per-name vol regime.

### 4.3 IV Filtering

Only filter is `IV / RV30 >= 1.10`, and it is fail-open: missing data
lets the trade through. A 1.10 ratio means "IV is just 10% above
realized." For a vol-harvester this is a very weak floor. It will admit
trades into mean-IV regimes and miss the most edge-rich outlier
conditions. Real vol-harvesting strategies use IVR >= 30-40 minimum,
often 50.

No term-structure check (front-month vs back-month IV) and no reference
to historical IV distribution for the symbol.

### 4.4 Trend and Regime Filtering

3-state regime: `risk_on` / `neutral` / `risk_off` driven by VIX level,
VIX 5d change, SPY vs 20/50 SMA, and SPY 10d RV.

* `risk_on` -> all sleeves, target -0.40.
* `neutral` -> all sleeves still active, target -0.30.
* `risk_off` -> all new entries blocked; rolls and CCs still allowed.

The `risk_off` rule is the right shape for capital preservation and the
wrong shape for vol harvesting. VIX > 25 with SPY < 50dma is exactly
when premium is fattest. An aggressive-growth wheel should narrow to
defensives plus index ETFs at lower delta, not stop trading.

### 4.5 Position Management

* Profit take at 50% of credit (sleeve-configurable).
* 4-hour post-profit-take cool-down per symbol.
* Roll trigger at `|delta| >= 0.50`. Holds (no roll) if no net-credit
  candidate.
* Earnings blackout for new entries and rolls. Hard ETF allowlist
  short-circuits yfinance hiccups.
* Post-fill delta drift alert at >0.10 deviation from target.
* Roll execution is structurally broken. See
  [10.1](#101-roll-execution-is-broken-critical).

### 4.6 Capital Deployment

* Total cap 70% of equity.
* Per-name cap 15% of equity.
* Per-symbol contract ceiling 10. Cumulative across ticks.
* Per-tick cap 10% of equity. Per-day cap 30% of equity. Both too
  restrictive for the aggressive-growth mandate.
* 30-min base cool-down plus 4-hour post-profit-take cool-down per
  symbol. Also restrictive.

### 4.7 Concentration and Correlation (CRITICAL GAP)

15% per-name cap is the only concentration control. There is no:

* sector concentration cap;
* factor concentration cap;
* net beta budget;
* net vega or gamma budget;
* correlation matrix or correlation-cluster cap.

In the production whitelist this is materially dangerous: a bank-led
selloff puts BAC, WFC, C up for simultaneous assignment. A
speculative-cluster selloff hits MARA, RIOT, SOFI, HOOD, PLTR, RIVN,
and SNAP all at once.

### 4.8 Sleeve Framework

Migration 018 collapses to a single sleeve at 100% allocation. Two
other sleeves are `enabled=false`. The sleeve abstraction is vestigial
in production. The CLAUDE.md description ("40/40/20 risk sleeves") is
no longer accurate.

## 5. Edge Assessment

The wheel itself has a documented small but real edge from:

1. The volatility risk premium (VRP).
2. Mean-reversion on short-DTE OTM puts.
3. Skew premium on OTM puts.

This implementation captures part of (1) but very little of (2) or (3):

* (1) VRP: the IV/RV 1.10 floor is too soft.
* (2) gamma: mid-pricing on a 25% spread with TIF=DAY likely
  under-fills. The post-fill delta drift alert hints fills are landing
  materially OTM.
* (3) skew: never measured, never targeted.

The universe is not chosen for edge, it's chosen for fit. Cheap-priced
names (F, T, KO) have less VRP than mega-caps because they're less
actively traded by short-vol funds.

Net read: marginally profitable in calm markets; loses meaningfully in
stressed markets.

## 6. Execution Assessment

| Concern | Status | Notes |
|---|---|---|
| Order types | mid limit DAY for opens; ask limit for BTC; market for /close | mixed |
| Stale quote handling | absent | mid-pricing on stale chains is a real risk |
| Spread quality check | hard drop at >= 30% spread/mid | good |
| Liquidity / volume / OI check | absent | a 0-OI synthetic-spread contract can be selected |
| Failed order handling | same-day repeat suppression | good |
| Duplicate prevention | cool-down plus contract ceiling | good |
| Race conditions | last-mile flag check inside submit | good |
| Partial fill handling | profit-take uses live position qty | OK |
| Reconciliation | polling plus stream, idempotent | good |
| Mid-vs-bid pricing audit | not measured after switch to mid | risk |

The two execution items most likely to silently bleed PnL:

1. Mid-priced limits with TIF=DAY on wide-spread names. Half of the
   production whitelist routinely shows >20% spread on weekly OTM puts.
2. Buy-to-close at the ask plus sell-to-open at mid pays 1.5x spread
   per cycle, not 1x.

## 7. Risk Assessment

| Layer | Implementation | Adequate for $25k aggressive growth? |
|---|---|---|
| Per-name notional | 15% | yes |
| Per-name contract count | 10 | yes |
| Total deployment | 70% | yes |
| Per-tick | 10% | too restrictive |
| Per-day | 30% | too restrictive |
| Sector concentration | none | NO |
| Correlation cluster | none | NO |
| Net vega budget | none | NO |
| Net beta budget | none | NO |
| Net gamma budget | none | NO |
| Margin/BP exhaustion | error-code mapping only | weak |
| Volatility expansion | none, only post-hoc breaker | NO |
| Drawdown breaker | 7% from 7-day HWM trips kill_switch | wrong wiring |
| Manual kill switch | yes | good |

Within current limits the strategy can be 100% deployed across 5
highly-correlated speculative names. There is no risk model that says
"you already have 30% in the speculative-tech cluster, refuse the next
SOFI/HOOD/PLTR even though each is under 15%."

## 8. Survivability Assessment

The system survives ordinary markets. It has serious failure modes
during regime change.

* Drawdown breaker trips after the move, not during it. Once tripped,
  the position can only get worse.
* `risk_off` shuts off premium harvesting at fat IV.
* No vol-spike-aware sizing.
* No tail hedge.
* Single broker dependency.

In a 2008-shape event the bot would: (a) trip the breaker after the
first -7% day, (b) stop managing existing positions, (c) take
assignment on every challenged put, (d) potentially write CCs below
cost basis on the recovery, and (e) realize permanent capital loss.

## 9. Market Regime Analysis

| Regime | Expected behavior | Expected outcome |
|---|---|---|
| Strong bull | risk_on, full deployment, -0.40 delta | best-case; 1-3% monthly |
| Sideways | neutral, -0.30 delta | 1-2% monthly |
| Vol crush | regime flips through risk_off -> risk_on rapidly | misses the best window |
| High-IV expansion | risk_off as soon as VIX > 25 | adverse for a vol harvester |
| Slow grinding bear | rolls broken; assignments cascade; CCs below cost basis | steady loss |
| Violent crash | breaker trips on day 1; management frozen | permanent loss likely |
| Prolonged stagnation | low utilization | flat |
| Tech selloff | speculative cluster moves together | concentrated loss |
| Liquidity crisis | spreads blow out; bot stops trading | survives, opportunity cost high |

## 10. Critical Bugs and Dangerous Logic

### 10.1 Roll execution is broken (CRITICAL)

`worker.py:443` calls `close_position(roll.underlying)` -- the equity
ticker. Alpaca's `DELETE /v2/positions/{symbol}` matches the equity
position, not the OCC option symbol the short put lives under. Two
outcomes:

* No equity held: `position_not_found`, the close is marked failed,
  the new put is never reopened. Rolls silently no-op.
* Equity held (e.g. from prior assignment): Alpaca closes the assigned
  shares instead of the short put. Worse than no-op.

Compounding: `_execute_roll` hard-codes `qty=1` on the new-open leg.
A 5-contract challenged put would re-open as 1 contract.

The test `tests/test_strategy_worker.py:621` verifies this broken
behavior with `assert_awaited_once_with("SPY")`. The test must be
updated when the bug is fixed.

Fix shape:

```python
close_result = await submit_buy_to_close(
    option_symbol=roll.current_option_symbol,
    qty=int(roll.current_qty),
    limit_price=roll.close_price,
    client_order_id=f"kai-rollclose-{close_row_id[:8]}",
)
new_result = await submit_short_put(
    option_symbol=roll.new_option_symbol,
    qty=int(roll.current_qty),
    limit_price=roll.new_credit,
    client_order_id=f"kai-roll-{new_row_id[:8]}",
)
```

`RollIntent` needs a `current_qty` field set from `pos.qty`.

### 10.2 Drawdown breaker freezes exposure-reducing actions (CRITICAL)

`drawdown.py:103` sets `kill_switch=True` on a -7% drawdown. Then:

* `submit_short_put` refuses -- correct.
* `submit_short_call` refuses -- correct.
* `submit_buy_to_close` refuses -- WRONG. Closing reduces exposure.
* `close_position` refuses -- WRONG.
* `_handle_profit_takes` early-returns -- WRONG.
* `_handle_rolls` skips execution -- WRONG (rolls reduce risk).

The drawdown breaker behavior is "we just lost 7%, now lock the book
and watch it lose more until the operator clears the flag." This is
the opposite of defensive.

Fix shape: introduce an `auto_breaker_engaged` flag separate from
`kill_switch`. New entries gated on
`!kill_switch && !auto_breaker_engaged && trading_enabled`.
Profit-takes and rolls gated on `!kill_switch` only. Operator clears
auto_breaker manually after assessing.

### 10.3 Universe does not match the brief (CRITICAL)

See [4.1](#41-universe-critical-deviation). The right path is one of:

* Run mega-caps but use defined-risk put credit spreads instead of
  CSPs.
* Keep CSPs and write only mega-caps the account can afford, accepting
  lower rotation.
* Trade spread strategies on mega-cap proxies (e.g. QQQ).

The proposed fix is the
[AUM-aware dynamic universe](#a-aum-aware-dynamic-universe-tier-2-1)
plus optional credit-spread support.

### 10.4 Covered calls can be sold below cost basis (HIGH)

`build_call_intents` selects the call closest to the regime's
`target_delta_call` (default +0.30) within the DTE band. There is no
constraint that strike >= cost basis.

Concrete failure: assigned at $50 -> stock at $45 -> -0.30 delta call
near $46-$47. Selling that means called away at $46, locking in a $4
realized loss minus premium. Textbook wheel trap.

Fix shape:

```python
# In select_call_strike, after target-delta filter:
typed_candidates = [
    (c, c.delta) for c, d in typed_candidates
    if c.strike >= cost_basis
]
# If empty, hold (no CC) and surface in diagnostic.
```

### 10.5 Mid-priced limit + TIF=DAY + cool-down compound an under-fill failure

Selling at mid on a 25%-spread name leaves a meaningful chance the
order won't fill before EOD. When it cancels, the symbol is on
cool-down (30 min, possibly 4 hours after a profit-take) and won't
re-enter. The next eligibility, the chain has moved.

Fix options:

* Submit at `mid - $0.01` with TIF=GTC and bump the price after N
  minutes if unfilled.
* Submit at `bid + 30% of spread`, not mid.
* On cancel, retry once at a tighter price before applying cool-down.

### 10.6 IV/RV floor is fail-open

`iv_rv.py:87` returns `True` when either IV or RV30 is missing. The
fail-open posture means the strategy silently relaxes its filter
during exactly the data conditions when caution is warranted.

Fix: flip to fail-closed for the IV branch (a contract with no
reported IV is illiquid). Keep RV30 fail-open.

### 10.7 No staleness check on chain quotes

`select_put_strike` and `evaluate_profit_takes` use whatever the chain
returned without any check on quote age. Mid-pricing on a 5-minute-old
chain is a real source of slippage.

Fix: record snapshot timestamp on each `OptionContract` and refuse to
act on chains older than ~30s.

## 11. Strategy Weakness Ranking (worst first)

| # | Weakness | Severity | Impact on compounding | Hidden blow-up risk |
|---|---|---|---|---|
| 1 | Roll execution broken | CRITICAL | eliminates primary defensive action | yes |
| 2 | Universe does not match brief | CRITICAL | wrong edge profile; speculative cluster correlation | yes |
| 3 | Drawdown breaker freezes profit-takes and rolls | CRITICAL | accelerates the drawdown it was meant to prevent | yes |
| 4 | CC strike not bound >= cost basis | HIGH | forced realized losses on every recovery cycle | yes |
| 5 | No sector / correlation / beta / gamma cap | HIGH | concentrated cluster blow-up possible | yes |
| 6 | risk_off blocks all entries | HIGH | bot offline during the richest IV regimes | no |
| 7 | Mid + TIF=DAY + cool-down -> under-fill | MEDIUM | real fill rate likely 50-80% on speculative names | no |
| 8 | No IVR or IV percentile gating | MEDIUM | mediocre regimes admitted | no |
| 9 | Per-tick 10% / per-day 30% caps | MEDIUM | slow capital deployment | no |
| 10 | No vega/gamma/correlation aggregation | MEDIUM | book risk summed only via dollar collateral | yes |
| 11 | yfinance SPOF for VIX/RV30 | MEDIUM | regime classifier fails loud with yfinance | no |
| 12 | Single sleeve in production; framework vestigial | LOW | future code may be written assuming three sleeves | no |
| 13 | Notification flood | LOW | operator desensitization | indirect |
| 14 | BTC at ask + open at mid = 1.5x spread per cycle | LOW | basis-points drag | no |
| 15 | No quote staleness check | LOW | marginal slippage | no |

## Tier 1: Must Fix Before Live Trading

1. **Fix the roll execution path.** Replace
   `close_position(roll.underlying)` with `submit_buy_to_close(
   option_symbol=roll.current_option_symbol, qty=current_qty, ...)`.
   Pipe `current_qty` through `RollIntent`. Submit the new put at the
   same qty. Update the test that asserts the broken behavior.
2. **Decouple drawdown breaker from kill_switch.** Add
   `auto_breaker_engaged` flag. New entries gated on
   `!kill_switch && !auto_breaker_engaged && trading_enabled`.
   Profit-takes and rolls gated on `!kill_switch` only. Operator
   clears auto_breaker manually.
3. **Constrain covered calls to strike >= cost basis.**
   `select_call_strike` filters by `c.strike >= position.avg_entry_price`.
   If empty, hold (no CC) and surface in diagnostic.
4. **Resolve the universe contradiction** via Tier 2 item 1
   (AUM-aware dynamic universe). Without that, the brief and the
   live whitelist remain incompatible.

## Tier 2: High Edge and Survivability

### A. AUM-aware dynamic universe (Tier 2 #1)

Make per-contract sizing reactive to current `account.equity` instead
of frozen by migration 018.

```
max_strike_at_current_equity = (equity * per_name_cap_pct) / 100
```

Worked examples (15% per-name cap):

| Equity | Max single-contract strike | Eligible names (illustrative) |
|---|---|---|
| $10k | $15 | F, T, SOFI, HOOD, INTC, GE, KMI, BAC (low strikes) |
| $25k | $37.50 | + PFE, KO, VZ, MU, MARA, RIOT, CSCO, WBA, GDX |
| $50k | $75 | + WFC, C, GM, MO, KHC, XLF, XLE, EEM, SLV |
| $100k | $150 | + PLTR ($135) NOW eligible, + AAPL, AMZN, V, JPM |
| $250k | $375 | + AVGO, MSFT, META, GOOGL |
| $500k+ | $750+ | + NVDA, TSLA, AMZN, full mega-cap pool |

Implementation:

1. Replace the static migration-018 whitelist with a single master
   pool of ~50 names, each tagged with sector, cluster, beta, and a
   typical strike anchor (current price as a proxy is fine).
2. At the top of every tick, after `account.equity` is fetched,
   compute `max_strike = equity * per_name_cap_pct / 100`.
3. Filter the pool to symbols whose nearest target-delta strike has
   `strike <= max_strike`. Cache for the tick.
4. The strategy expands its universe automatically as the account
   grows. A $25k account starts on F/T/PFE/KO and graduates to
   PLTR/AAPL/MSFT/META/AMZN/NVDA at $50k, $100k, $250k, $500k
   respectively, with no migration needed.

This also fixes [10.3](#103-universe-does-not-match-the-brief-critical)
without forcing a binary choice between cheap-only and mega-cap-only.

Where it lives: a new `kai_trader/strategy/universe.py` module that
takes `equity` and returns a filtered whitelist for the tick.
Replaces the per-sleeve `symbol_whitelist` field at use-site (the
column stays for backward compat but is overridden by the dynamic
filter in production).

### B. Sector + cluster + net-beta + net-gamma budgets (Tier 2 #2)

Per-name 15% cap is necessary but insufficient. Replace with a stacked
cap tested in this order on every candidate:

| Layer | Cap | Source |
|---|---|---|
| Per-name notional | 15% of equity | already implemented |
| Per-sector notional | 25% of equity | new: sector tag column |
| Per-cluster notional | 20% of equity | new: cluster tag (`speculative_growth`, `financials`, `energy`, `mega_tech`, `defensive`, `etf_broad`, `etf_sector`) |
| Net portfolio beta (SPY-relative) | abs(net beta) <= 1.5 * deployment_pct | new: per-symbol beta table |
| Net portfolio gamma (per 1% SPY move) | gamma * 1% * SPY < 1.5% of equity | new: aggregate from open shorts |

Where the layers live:

* Sector and cluster tags: extend the master pool from
  `text[]` to `jsonb[]` of `{symbol, sector, cluster, beta}`. New
  migration `022_symbol_metadata.sql`.
* Beta budget: aggregate at the top of
  `build_intents_with_diagnostics`. Each candidate's contribution is
  `delta * qty * 100 * beta`. Reject when adding it pushes
  `|net_beta|` over the cap.
* Gamma budget: aggregate gamma from the live chain at scoring time.
  Reject when `net_sum_gamma * 1% * SPY > 1.5%` of equity.

Both budgets short-circuit before chain fetch when the existing book
is already at cap. Both surface in the tick diagnostic so the
operator sees "rejected for net-beta cap" vs "rejected for net-gamma
cap" vs "rejected for sector cap."

Open question: net-beta data source. Options:

* (a) Static beta table refreshed weekly from yfinance.
* (b) Live beta from rolling 60-day correlations against SPY using
  the daily-bars helper that already exists.
* (c) Hard-coded per-symbol beta shipped in the migration and
  reviewed quarterly.

Recommendation: start with (c) for predictability, then upgrade to (b)
once the rolling computation is validated against (a).

### C. Replace IV/RV ratio with IV rank

Compute IV rank per symbol from a rolling 252-day window of
front-month IV. Floor candidates at IVR >= 30 (configurable). Falls
back to the IV/RV ratio when IV history isn't long enough.

### D. risk_off ratchet-down instead of full-stop

In `risk_off`: target_delta drops to -0.20, only ETFs and defensives
are eligible, per-name cap halves. Don't go to zero; ratchet down.

### E. Mid-pricing cancel-and-bump

On EOD cancel, retry once at `bid + 60% of spread` before entering
cool-down. Or: TIF=GTC for entries with a tick-level price-walk
mechanism.

## Tier 3: Operational Hardening

1. **Cost-basis tracking inside the orders table.** Don't trust
   Alpaca's `avg_entry_price` for CC strike floor; reconstruct from
   filled CSP rows.
2. **Quote staleness check.** Reject any chain snapshot older than
   ~30s.
3. **Reorder tick phases.** Reconcile -> drawdown -> assignment-detect
   -> CC-build -> roll -> profit-take -> CSP-build. Currently CSP-build
   happens before assignment detection on the same tick.
4. **Notification cadence.** One tick summary per 4 hours. Real-time:
   only fills, drawdown trips, errors, regime transitions, post-fill
   delta drift.
5. **Drop the vestigial sleeve framework, or actually use three
   sleeves.** Pick one and update CLAUDE.md.

## Optional Advanced Improvements

For after Tier 1 and Tier 2 land.

* Replace bare CSPs with put credit spreads on the high-vol names.
  Defined risk per contract; same delta target; capital-efficiency
  multiplier of 5-20x. Allows the strategy to trade NVDA/TSLA/META on
  a $25k book without per-name cap collapsing.
* Long-vol overlay. ~5% of equity in OTM SPY puts or VIX calls,
  rolled monthly. Pays off in the exact regime the wheel loses.
* Backtesting harness. Replay against 3-5 years of OPRA history.
  Without this, every parameter is a guess.
* IV rank + IV percentile + skew steepness as scoring inputs. Replace
  `mid/strike * 365/dte * spread_quality` with a composite that
  includes IVR and -25 delta skew steepness vs ATM.
* Skew-aware strike selection. Pick the put at target delta only when
  skew > some threshold; otherwise pass on the symbol entirely.
* Earnings IV-crush plays explicitly opted-in as a sleeve.
  Currently earnings is a hard blackout. A dedicated sleeve writing
  2-day-pre-earnings ATM-ish put credit spreads with explicit risk
  caps would harvest the IV crush.
* Per-symbol historical edge tracking. Roll up realized PnL per
  symbol, per regime, per IV bucket. Symbols with negative edge over
  50+ trades exit the whitelist automatically.
* Greek-aware exit triggers. Exit when `|delta| > 0.7` OR
  `theta < 50%` of opening theta OR IV crush of >30%. Currently only
  delta and price thresholds drive exits.

## Final Verdict

Structurally flawed for the stated objective. Not safe for real-money
fully-automated trading in the current state.

Infrastructure is solid. The strategy itself does not match the brief:
the universe is not mega-cap, the IV filter is too soft, the regime
gating is upside-down, the roll path is broken, the drawdown breaker
fires defensive actions, and concentration is uncapped beyond per-name.

If the goal is aggressive growth on $25k -> meaningful capital with
vol harvesting on mega-caps, the right move is one of:

* Pivot to defined-risk put credit spreads on real mega-caps, keep
  the current execution shell, fix the Tier 1 bugs.
* Or accept the current cheap-priced universe is the actual strategy,
  rename it accordingly, and tune for that universe (sector cap, IV
  rank, cost-basis CC floor, working rolls, AUM-aware universe).

The work needed to reach "viable for live capital" is the
[Tier 1](#tier-1-must-fix-before-live-trading) list -- four items, all
surgical. The work needed to reach "potentially viable for the stated
aggressive growth thesis" is Tier 1 plus
[Tier 2](#tier-2-high-edge-and-survivability). Tier 3 and the
optional list are post-cutover.

In its current state I would not flip `ALPACA_PAPER=false` on this bot.
