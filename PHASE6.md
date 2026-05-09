# Phase 6 — Weekly AI Review

A scheduled, read-only Claude pass that reviews the week's trading
activity, open positions, regime, and system health, and surfaces
findings as severity-tagged events through the existing dispatcher.
Acts as a second pair of eyes, not a control loop.

> **Naming note.** The `phaseN_30k` directories under `backtest_runs/`
> are strategy-calibration iterations, not build phases. The
> operational phase numbering picks up at 6 here.

## Goal

Replace the operator's end-of-week eyeball-check with a deterministic
agent run that reads the same data the operator would, written by
Claude with the existing read-only tool surface, and posts findings to
Telegram on Saturday morning SGT. Catches anomalies the deterministic
gates (kill_switch, drawdown breaker, trading_enabled, sleeve caps)
would not flag on their own:

- positions trending toward the roll/assignment trigger but not over yet
- earnings inside DTE that slipped through (data source went stale)
- fills with unexpected slippage relative to the recorded credit
- regime transitions that warrant a sleeve posture review
- pending changes that have been sitting unresolved
- notifications queue or dispatcher backlog
- sleeve caps saturated for multiple weeks (deployment stuck)
- profit-take or roll candidates the worker held due to no_net_credit

## Cadence

Once a week, Saturday at `01:00 UTC` (= 09:00 SGT). Picked because:

- US options weeklies expire Friday, so by Saturday morning every
  weekly position has either expired worthless, been assigned, or
  rolled. The review reads a settled book.
- 09:00 SGT lands the report with the operator's morning coffee.
- ≈4.3 runs/month into a $10/month ceiling = ≈$2.30/run, which buys a
  thorough read: chain pulls on every held name, multi-day P&L
  analysis, sleeve-drift comparisons against the prior week's snapshot.

If shadow mode shows the run is high-signal and low-noise, a future
phase can promote to daily or add a cheap mid-week pulse on top.

## Architectural principles

- **Read-only.** Review uses the existing `kai_chat_ro` role and the
  `chat.tools` surface. No new write tools. If the review wants a
  config change, it calls the existing `propose_change` tool, which
  writes a `pending_changes` row that flows through the existing
  approval UI.
- **Findings, not actions.** The review never toggles flags, never
  closes positions, never submits orders. Even if it sees a fire.
- **Reuse the chat stack.** Same `claude-sonnet-4-6`, same Anthropic
  SDK client, same prompt caching, same tool definitions. The review
  is a different system prompt and a different invocation entry point
  on top of the same plumbing.
- **Findings flow through events, not notifications.** Each finding
  becomes an `events` row at the appropriate priority. The existing
  `EventDispatcher` renders it to Telegram. This keeps the daily
  realised-P&L summary (`notifications`) and the AI review (`events`)
  in separate channels with separate UX.
- **Cost is a first-class concern.** Hard caps on tool iterations and
  output tokens. Per-run cost is recorded. A budget alarm fires if a
  rolling 30-day cost crosses a configurable ceiling (default $10).
- **Shadow first.** Phase 6.1 ships in shadow mode: every finding is
  emitted as `info` regardless of model-assigned severity, so the
  operator can calibrate the prompt against several weeks of real
  output before warning/critical events start paging.

## Suggested timing

The 2-week paper trial only yields ~2 review runs, which is not enough
to calibrate severity. Practical sequence:

1. Land **6.1 (shadow mode)** as soon as the paper trial begins so the
   first runs accumulate during the trial.
2. Continue running shadow mode through the live-capital ramp (small
   size first). Aim for ≥ 4-6 weeks of shadow data before tuning.
3. Land **6.2 (severity tuning)** once you have enough runs to spot the
   model's calibration drift.
4. Land **6.3 (live alerting)** only after 6.2 lands and you trust the
   rubric.

---

## Phase 6.1 — Scaffold + shadow mode

**Acceptance.** Every Saturday at the configured UTC fire time, a row
lands in `weekly_reviews` with a non-null `findings_json` and a
non-null `summary`. A single `info`-priority `events` row is dispatched
to Telegram per run, containing the summary line plus a compact
rendering of all findings. No `warning` or `critical` events are
emitted yet, regardless of what the model says.

- [ ] 6.1.1 Migration `032_weekly_reviews.sql` — table with `id`,
  `run_at`, `model`, `prompt_tokens`, `output_tokens`,
  `cache_read_tokens`, `cost_usd`, `tool_call_count`, `summary text`,
  `overall_severity text`, `findings_json jsonb`, `error text`.
- [ ] 6.1.2 New module `src/kai_trader/reviews/schema.py` — Pydantic
  models for `Finding` (severity, category, title, body, data_refs)
  and `ReviewResult` (summary, overall_severity, findings).
- [ ] 6.1.3 New module `src/kai_trader/reviews/prompt.py` — review
  system prompt. Identity ("you are an independent reviewer"),
  read-only constraint, severity rubric, structured-output contract,
  bias toward "ask for human attention when uncertain", and weekly
  framing ("the week of YYYY-MM-DD through YYYY-MM-DD").
- [ ] 6.1.4 New module `src/kai_trader/reviews/runner.py` —
  `run_review() -> ReviewResult`. Builds the message list, calls
  `chat.client` with the review system prompt and the existing
  read-only tool list (no `propose_change` in 6.1), runs the tool
  loop with a hard iteration cap, parses the final assistant message
  into `ReviewResult`. Records token usage + cost.
- [ ] 6.1.5 New module `src/kai_trader/db/weekly_reviews.py` —
  `record_review(...)`, `most_recent(n)`, `cost_last_30_days()`.
- [ ] 6.1.6 New module `src/kai_trader/observability/weekly_review.py`
  — the scheduler. Mirrors the weekly cadence pattern in
  `observability/equity_chart.py` (uses `_UTC_DAY` + `_UTC_TIME` env
  vars, `_next_weekly_fire_at`). Calls `runner.run_review()`,
  persists the row, then emits one `weekly_review_completed` event
  (forced to `info` in shadow mode).
- [ ] 6.1.7 Render path — extend `events/render.py` with a renderer
  for `weekly_review_completed` event_kind. Format: header line with
  the week range and overall verdict, then one line per finding with
  a severity glyph (`i / W / C`), category, and title. Truncates to
  fit Telegram's 4096-char limit; uses the chat chunker if needed.
- [ ] 6.1.8 Wire worker into `bot/main.py` startup/shutdown alongside
  the daily-report and weekly-equity-chart workers.
- [ ] 6.1.9 Config + env:
  - `WEEKLY_REVIEW_UTC_DAY` (0=Mon..6=Sun, default `5` = Saturday)
  - `WEEKLY_REVIEW_UTC_TIME` (default `01:00` = 09:00 SGT)
  - `WEEKLY_REVIEW_ENABLED` (default `true`)
  - `WEEKLY_REVIEW_SHADOW_MODE` (default `true` until 6.3)
  - `WEEKLY_REVIEW_MAX_TOOL_CALLS` (default `40` — weekly run can
    afford a deeper tool loop than a daily one would)
  - `WEEKLY_REVIEW_MAX_OUTPUT_TOKENS` (default `8000`)
  - `WEEKLY_REVIEW_MODEL` (default `claude-sonnet-4-6`)
- [ ] 6.1.10 Tests. Unit: prompt construction, response-parser
  (well-formed and malformed model output), shadow-mode forces info,
  fire-day + fire-time math, schema validation. Mocked Anthropic
  client: replay a recorded tool transcript and assert one
  `weekly_reviews` row + one `events` row of kind
  `weekly_review_completed` and severity `info`. Coverage stays
  ≥ 80%.
- [ ] 6.1.11 Quality gates. `ruff check` clean, `mypy --strict src/`
  clean, full pytest passing. Docs: `CLAUDE.md` state update,
  `.env.example` keys, `TRACKER.md` row.

## Phase 6.2 — Severity policy + tuning

**Acceptance.** After ≥ 4-6 weekly shadow-mode runs, the model's
self-assigned severities are reviewed against ground truth (what the
operator actually wanted to be paged about). The system prompt and the
output rubric are tightened. A unit test pack pins the rubric so future
prompt edits are deliberate.

- [ ] 6.2.1 Pull all `weekly_reviews` rows from the shadow window.
  Hand the operator a one-page review of every finding the model
  labelled `warning`/`critical` and ask: actually warn-worthy,
  actually critical-worthy, or noise.
- [ ] 6.2.2 Edit the rubric in `reviews/prompt.py`. Concrete severity
  definitions:
  - **critical**: drawdown breaker fired during the week;
    kill_switch is on with no human-recorded reason in
    `decision_log` within the last 7 days; any open short put
    trading at >2.0× its original credit (deep trouble); orders
    stuck in `pending` for >30 minutes; trading stream worker has
    been disconnected >5 minutes; an Alpaca health probe failed
    during the week.
  - **warning**: roll trigger crossed and held due to
    `no_net_credit_candidate`; sleeve cap saturated all week; regime
    transitioned in the last 7 days; drawdown >3% but <7% from 7d
    high; earnings inside DTE for any held position; a profit-take
    threshold crossed during the week but not executed; chat tool
    errors recur for the same call across the week.
  - **info**: anything else, including "all systems nominal".
- [ ] 6.2.3 Add fixture-based tests. For each rubric example above,
  feed the runner a fixed tool-response transcript and assert the
  model's resulting severity passes the operator's review. Pin the
  prompt hash; if the prompt changes, tests force the operator to
  review.
- [ ] 6.2.4 Quality gates + docs.

## Phase 6.3 — Live alerting

**Acceptance.** `WEEKLY_REVIEW_SHADOW_MODE=false` flips the worker
into live mode. Findings emit at the model's assigned severity.
Operator receives `warning` and `critical` findings as separate
`events` rows (rendered with their own glyphs and inline action
buttons where relevant) on top of the `info`-priority summary.

- [ ] 6.3.1 Render path: each finding ≥ `warning` becomes its own
  event row, separately dispatched. Inline "Acknowledge" button
  writes a `decision_log` row so the dashboard can later show
  acknowledgement state.
- [ ] 6.3.2 Cost ceiling. New env: `WEEKLY_REVIEW_MAX_COST_USD_30D`
  (default `10.00`, ~$10/month). The worker computes a rolling
  30-day cost from `weekly_reviews.cost_usd` before each run; if
  exceeded, the run still fires but emits a `warning` event "review
  budget exceeded, last 30d cost = $X" and the operator decides
  whether to lower cadence, trim tools, or raise the ceiling. At
  one weekly run, $10/30d budgets ~$2.30/run, which is comfortable
  headroom for a Sonnet 4.6 tool-loop with prompt caching on the
  system prompt and tool defs.
- [ ] 6.3.3 Acknowledge handler. New `CallbackQueryHandler` for
  `weekly_review_ack:<finding_id>` writes `decision_log` and edits
  the Telegram message to strike through the title.
- [ ] 6.3.4 Tests + quality gates + docs.

## Phase 6.4 — Optional: bridge into approvals

**Acceptance.** When the review identifies a config change worth
proposing (sleeve target delta drift, IV-rank threshold, profit-take
percentage), the model can call `propose_change` directly during the
review run. The change lands in `pending_changes` and the existing
approval flow handles it.

- [ ] 6.4.1 Expand the review's tool list to include
  `propose_change`.
- [ ] 6.4.2 Add prompt guidance: when to propose, when to merely
  note. Bias against proposing on a single week's data.
- [ ] 6.4.3 Tests + quality gates + docs.

## Files

**New**

- `src/kai_trader/db/migrations/032_weekly_reviews.sql`
- `src/kai_trader/db/weekly_reviews.py`
- `src/kai_trader/reviews/__init__.py`
- `src/kai_trader/reviews/schema.py`
- `src/kai_trader/reviews/prompt.py`
- `src/kai_trader/reviews/runner.py`
- `src/kai_trader/observability/weekly_review.py`
- `tests/reviews/test_runner.py`
- `tests/reviews/test_schema.py`
- `tests/observability/test_weekly_review.py`

**Modified**

- `src/kai_trader/bot/main.py` (wire the worker)
- `src/kai_trader/config.py` (new env keys)
- `src/kai_trader/events/render.py` (new event_kind renderer)
- `.env.example` (new keys with documentation)
- `CLAUDE.md` (state + env table)
- `TRACKER.md`

## What is explicitly NOT in Phase 6

- More frequent cadence. Weekly only for now. Promote to daily or add
  a cheap mid-week pulse once shadow mode shows the weekly run is
  high-signal and low-noise, and only if the budget grows to
  accommodate it.
- Any control authority. Review cannot toggle flags, cancel orders,
  close positions, or modify configs without going through the
  existing approval flow.
- Replacing the deterministic guards. Drawdown breaker, kill_switch,
  trading_enabled, and sleeve caps remain authoritative. The review
  is oversight on top, not in front.
- Replacing the daily realised-P&L report. Both run, both deliver.
  The realised-P&L report stays in `notifications`, the AI review
  goes through `events`.
- Vector stores, embeddings, fine-tuned models, RAG. The review uses
  the same prompt-cached tool-using model the chat handler already
  uses. No new infrastructure.
- A dedicated UI surface. Findings render to Telegram. A dashboard
  view of historical reviews can be added in a later observability
  phase.
