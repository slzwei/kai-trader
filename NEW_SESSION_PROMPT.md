# NEW_SESSION_PROMPT — Phase 6.1: Structured decision audit

> **DELETE THIS FILE** when the session has completed successfully. It is a one-shot prompt, not a permanent doc. The permanent task spec lives in `LIVE_TRADING_PLAN.md` Section "Phase 6.1: Structured decision audit".

---

## Paste-this-into-Claude block (everything between the rules)

---

You are picking up an in-flight project. Read this entire message first, then begin.

**Source of truth.** `LIVE_TRADING_PLAN.md` at the repo root. The Phase 6.1 task spec (T-6.1.0 through T-6.1.8) is the canonical definition of the work. This prompt sequences and gates it. If anything in this prompt conflicts with the plan, the plan wins. Also read `CLAUDE.md` (project-wide conventions, env vars, deploy mechanics) and `TRACKER.md` (history of what shipped) before any code action.

**Mission.** In this single session, ship Phase 6.1 (T-6.1.0 through T-6.1.8) in order, fully. No skipping. No reordering. Each task lands on the deploy branch (`claude/kai-trader-phase-1-sHFJk`), confirmed live on Render, with green tests. After T-6.1.8 ships, post the end-of-session summary and stop. Do not start Phase 7 work in this session.

**Why this phase.** The 2026-05-02 diagnostic surfaced finding F-17: per-symbol decision rationale is computed every tick by `BuildDiagnostics` but rendered into a Telegram heartbeat string and discarded. Operator cannot answer "why didn't we enter MSFT on 2026-04-30" without grepping notification rows. Closes hard gate G-14.

**Scope discipline (read this twice).** This phase does NOT change selection behaviour. No new filters, no new caps, no ranker changes. It only adds persistence and a read tool. If you find yourself editing the funnel logic in `candidates.py` to do anything other than emit `SymbolDecision` objects, stop and report. Splitting that change into its own phase is correct; bundling it is not.

---

## Step 0: pre-flight (do this BEFORE touching T-6.1.0)

Run these in order. If any check fails, STOP and ask the user. Do not proceed.

1. `git status` — confirm you are on `main`. The only expected uncommitted file is `NEW_SESSION_PROMPT.md` itself (this file). If you see other uncommitted work, stop.
2. `git fetch origin` — confirm clean fetch.
3. `git log --oneline origin/main..HEAD` — must report nothing (local main equals origin/main). If local is ahead, stop and ask before proceeding.
4. `git log --oneline -3 origin/main` — confirm the head commit is `deca5ad feat: harden Kai chat accuracy with structured prompt and live/history tool tags` or newer. If older, this prompt is stale; ask the user.
5. `git log --oneline -1 origin/claude/kai-trader-phase-1-sHFJk` — note the deploy branch HEAD. After T-6.1.8 it should be ahead by exactly the number of commits this session creates.
6. `uv run pytest --no-cov 2>&1 | tail -10` — must report 0 failures. The 5 strategy worker tests fixed in W-6 must still be green.
7. `uv run ruff check src/ tests/ 2>&1 | tail -5` — must say "All checks passed!"
8. `uv run mypy --strict src/ 2>&1 | tail -5` — must say "Success: no issues found".
9. Confirm Supabase reachable and migrations are at the expected head: `uv run python scripts/apply_migrations.py`. Expected output reports applied migrations 011-018, 020, 021 (no 019, no 022+) and then "no pending migrations" or equivalent. If the script errors on connection, check the `.env` `DATABASE_URL` / `SUPABASE_DB_PASSWORD` before continuing.

Only after all 9 checks pass, begin T-6.1.0.

---

## Per-task execution loop

For each task in order (T-6.1.0, T-6.1.1, T-6.1.2, T-6.1.3, T-6.1.4, T-6.1.5, T-6.1.6, T-6.1.7, T-6.1.8):

1. **Plan in writing.** Use `TaskCreate` to add the task. Mark `in_progress` immediately. Re-read the task spec in `LIVE_TRADING_PLAN.md` Section "Phase 6.1" carefully. Identify every file you'll touch.
2. **Read the relevant code first.** Do not write changes from memory. Specifically: re-read `src/kai_trader/strategy/candidates.py` for `BuildDiagnostics` shape and the funnel order, and `src/kai_trader/db/orders.py` for the existing `record_intent` signature.
3. **Implement the change.** Match the plan's spec, not your interpretation. If the plan is wrong about a path, function name, or schema field, stop and report — do not silently improvise.
4. **Write the tests specified by the task.** Match the test cases listed in the task acceptance criteria.
5. **Run quality gates locally:**
   - `uv run ruff check <files-you-touched>` — must pass.
   - `uv run mypy --strict src/` — must pass.
   - `uv run pytest <task-relevant-test-files> --no-cov` — must pass.
   - `uv run pytest --no-cov 2>&1 | tail -10` — confirm no regressions.
6. **Stage only files you yourself edited.** Use `git add <specific-paths>`. Never `git add .` or `git add -A`.
7. **Commit** with the conventional message specified in the task checklist below. Include the trailer:
   ```
   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   ```
8. **Push to deploy branch:** `git push origin main:claude/kai-trader-phase-1-sHFJk`. (Push `main` first if you want, but the deploy push is the one that goes live.)
9. **Verify the deploy.** Wait up to 90 seconds. Then ask the user to confirm in the Render dashboard that a deploy event for your commit hash appeared. If they confirm "no deploy event", ask them to hit **Manual Deploy → Deploy latest commit**. Do not retry the push. Do not move to the next task until the user confirms the deploy is live.
10. **Apply migrations on the live DB** (only T-6.1.0 and T-6.1.1 need this): after the migration commit deploys, run `uv run python scripts/apply_migrations.py` locally against the same Supabase. Confirm the migration shows up in `schema_migrations`. The bot only deploys code, not schema; migrations require this manual step.
11. **Mark the task `completed` in TaskCreate.** Move to the next task.

One short status update per task transition. No narration inside a task.

---

## Per-task completion checklist (verify each box before marking done)

### T-6.1.0: Migration `022_tick_decisions.sql`
- [ ] File `src/kai_trader/db/migrations/022_tick_decisions.sql` created. Numbering does not collide with 011-018, 020, 021.
- [ ] Schema matches the plan exactly: 12 enum values in CHECK constraint, 4 indices, default `now()` on `created_at`.
- [ ] `uv run python scripts/apply_migrations.py` succeeds locally and is idempotent (re-run produces no changes).
- [ ] After migration: `select count(*) from tick_decisions` returns 0.
- [ ] `kai_chat_ro` role has SELECT (verify: `set role kai_chat_ro; select count(*) from tick_decisions; reset role;` returns 0, not a permission error). If it errors, run `scripts/create_chat_ro_role.py` to refresh grants.
- [ ] Commit subject: `feat: tick_decisions table for per-symbol decision audit`.

### T-6.1.1: Migration `023_orders_tick_id.sql`
- [ ] File `src/kai_trader/db/migrations/023_orders_tick_id.sql` created.
- [ ] `alter table orders add column if not exists tick_id uuid;` — idempotent.
- [ ] Partial index on non-null tick_id created.
- [ ] After migration: `select count(*) from orders where tick_id is null` equals total row count (no backfill).
- [ ] Commit subject: `feat: link orders rows to producing tick_id`.

### T-6.1.2: `SymbolDecision` dataclass + outcome enum
- [ ] All 12 outcome constants defined in `src/kai_trader/strategy/candidates.py`, names mirror the migration CHECK values exactly.
- [ ] `ALL_OUTCOMES: frozenset[str]` exported with `len(ALL_OUTCOMES) == 12`.
- [ ] `SymbolDecision` is `@dataclass(frozen=True)` with fields: `sleeve, symbol, outcome, reason, selected_option_symbol, selected_strike`.
- [ ] `BuildDiagnostics` gets a new `decisions: list[SymbolDecision]` field with `default_factory=list`. Existing fields preserved.
- [ ] Type-check passes (`uv run mypy --strict src/`).
- [ ] No behaviour change. Existing tests still green.
- [ ] Commit subject: `feat: SymbolDecision dataclass and outcome enum for tick audit`.

### T-6.1.3: Wire `SymbolDecision` through every filter branch
- [ ] Every existing filter branch in `build_intents_with_diagnostics` that increments a counter now also appends a `SymbolDecision` to `diagnostics.decisions`.
- [ ] The 12 outcome tags are exhaustive: every code path through the funnel results in exactly one `SymbolDecision` per symbol per sleeve. No symbol considered ends with zero decisions; none with two.
- [ ] Required `reason` keys per outcome match the table in the plan exactly. Decimal values serialised as strings (matches existing `intent_payload` convention).
- [ ] Existing diagnostic counters (`symbols_skipped_for_earnings`, etc.) are NOT removed; they remain for the heartbeat reader. Both paths populate together.
- [ ] New test file `tests/test_candidates_decisions.py` covers each branch with at least one positive case. 12 outcomes × 1 case minimum = 12 new tests. Each asserts (a) outcome tag, (b) every required reason key present, (c) values are correct types.
- [ ] One additional integration test: a 5-symbol mock tick produces exactly 5 decisions in `diagnostics.decisions`.
- [ ] Selection behaviour is unchanged. The set of intents returned for a given input is identical before and after this commit.
- [ ] Commit subject: `feat: emit SymbolDecision from every candidate funnel branch`.

### T-6.1.4: tick_decisions DB helpers
- [ ] New file `src/kai_trader/db/tick_decisions.py`.
- [ ] `record_tick_decisions(tick_id, tick_at, decisions) -> int` uses `executemany` (single batch INSERT).
- [ ] Empty `decisions` list is a no-op returning 0 (no DB call).
- [ ] `recent_tick_decisions(*, symbol=None, since=None, outcome=None, limit=200)` constructs parameterised SQL (NEVER string-concat). `limit > 1000` raises ValueError. Invalid `outcome` raises ValueError.
- [ ] `TickDecisionRow` dataclass mirrors the schema 1:1.
- [ ] New test file `tests/test_db_tick_decisions.py`: round-trip, filter by symbol, filter by outcome, filter by since, limit truncation, empty-list no-op.
- [ ] Commit subject: `feat: tick_decisions read and batch-write helpers`.

### T-6.1.5: Wire worker.py
- [ ] `worker.py` generates `tick_id = uuid4()` and `tick_at = datetime.now(UTC)` at the top of each tick.
- [ ] After `build_intents_with_diagnostics`, calls `record_tick_decisions(tick_id, tick_at, diagnostics.decisions)` BEFORE any submit calls.
- [ ] Failure of `record_tick_decisions` is logged at WARNING and does NOT block trading. Audit failure must never gate trading.
- [ ] Every `record_intent` call passes `tick_id=tick_id` (depends on T-6.1.6).
- [ ] Extends e2e smoke test: 5-symbol tick produces 5 `tick_decisions` rows with same `tick_id`; resulting `orders` rows carry the same `tick_id`.
- [ ] Commit subject: `feat: worker generates tick_id and persists per-tick decisions`.

### T-6.1.6: Expand intent_payload
- [ ] `record_intent` signature accepts `tick_id: UUID | None = None` and writes it to the column.
- [ ] `intent_payload` now contains every field of `TradeIntent`: `sleeve, symbol, option_symbol, strike, expiration, target_delta, actual_delta, bid, ask, mid, qty, collateral, expected_premium, yield_pct` (14 fields). Decimals as strings; date as ISO-8601.
- [ ] `Order` dataclass + `_row_to_order` updated to expose `tick_id`.
- [ ] `tests/test_orders.py` extended: assert all 14 keys in round-tripped `intent_payload`; assert `tick_id` round-trips.
- [ ] Existing tests still pass.
- [ ] Commit subject: `feat: persist full TradeIntent and tick_id in orders.intent_payload`.

### T-6.1.7: Chat tool `query_tick_decisions`
- [ ] New tool defined in `TOOL_DEFINITIONS` (`src/kai_trader/chat/tools.py`), description begins with "HISTORY tool." per the W-1..W-9 chat accuracy convention.
- [ ] Tool handler caps at 200 rows. Returns `_meta.truncated`, `_meta.as_of_utc`, `_meta.as_of_sgt` per the existing readonly convention.
- [ ] Outcome arg validated against `ALL_OUTCOMES`; invalid outcome returns `{"error": "invalid outcome ...", "allowed": [...]}`.
- [ ] System prompt (`src/kai_trader/chat/system_prompt.py`) gets one sentence appended to the HISTORY list under `# Live vs history`: `"- query_tick_decisions: per-symbol per-tick funnel outcomes (selected or skipped, with structured reason)."`
- [ ] `tests/test_chat_tools.py`: 3+ new tests (round-trip, limit cap, outcome validation).
- [ ] `tests/test_chat_accuracy.py`: `_REQUIRED_PROMPT_RULES` updated to include the new tool mention; the tool-tag check covers `query_tick_decisions` with tag `HISTORY`.
- [ ] Commit subject: `feat: query_tick_decisions chat tool for decision audit reads`.

### T-6.1.8: e2e smoke + final regression
- [ ] e2e test (extend existing `tests/test_e2e_strategy_pipeline.py` or equivalent) seeds a 5-symbol tick: 2 selected, 3 skipped (one per of: earnings, iv_rv_floor, per_name_cap).
- [ ] Asserts: 5 `tick_decisions` rows with one shared `tick_id`; 2 `orders` rows with same `tick_id`; orders' `intent_payload` has all 14 fields.
- [ ] `uv run pytest --no-cov` reports 0 failures, 0 errors.
- [ ] Coverage still ≥ 80%.
- [ ] Commit subject: `test: e2e coverage for tick_decisions and intent_payload expansion`.

---

## Final session verification (after T-6.1.8 ships)

Once T-6.1.8 is committed, deployed, and confirmed live, run this final sweep before posting the summary:

1. **9 commits present on the deploy branch:**
   ```
   git fetch origin
   git log --oneline -12 origin/claude/kai-trader-phase-1-sHFJk
   ```
   Expected: 9 new commits since the session-start head, with subjects matching T-6.1.0..T-6.1.8 above. List them in your summary.

2. **Test suite is fully green:**
   ```
   uv run pytest --no-cov 2>&1 | tail -10
   ```
   Expected: 0 failures, 0 errors.

3. **Lint and types still green:**
   ```
   uv run ruff check src/ tests/ 2>&1 | tail -5
   uv run mypy --strict src/ 2>&1 | tail -5
   ```

4. **Coverage threshold met:**
   ```
   uv run pytest 2>&1 | tail -5
   ```
   Expected: coverage ≥ 80%.

5. **Schema is live on Supabase:**
   ```
   uv run python -c "import asyncio, sys; sys.path.insert(0,'src'); from kai_trader.config import get_settings; import asyncpg
   async def m():
     s=get_settings(); c=await asyncpg.connect(s.database_url or s.computed_database_url)
     try: print(await c.fetchval(\"select count(*) from information_schema.tables where table_name='tick_decisions'\"))
     finally: await c.close()
   asyncio.run(m())"
   ```
   Expected: `1` (table exists).

6. **Production tick is writing rows (ask the user to verify):** request the user wait until at least one strategy tick has run after the deploy (5-min cadence; after market open if currently closed). Then run:
   ```
   uv run python -c "import asyncio, sys; sys.path.insert(0,'src'); from kai_trader.config import get_settings; import asyncpg
   async def m():
     s=get_settings(); c=await asyncpg.connect(s.database_url or s.computed_database_url)
     try:
       n=await c.fetchval('select count(*) from tick_decisions where tick_at > now() - interval \\'15 minutes\\'')
       print(f'rows in last 15min: {n}')
     finally: await c.close()
   asyncio.run(m())"
   ```
   Expected: > 0 if the bot ticked during a market-open window. If market closed, expected 0 (no ticks emit decisions). Tell the user explicitly which case you're in.

7. **Spot-check via Kai:** ask the user to send Kai (Telegram free-form): "why was MARA picked on the last tick?" Kai's new `query_tick_decisions` tool should fire; the response should cite `tick_decisions` rows with structured reasons. If Kai answers from `intent_payload` only without invoking the new tool, the system prompt addendum did not take — investigate before declaring success.

8. **Hard-gate update**: G-14 is now closed by this session. List it in the summary.

---

## End-of-session summary (post this when you stop)

Format:

```
Phase 6.1 status: <count>/9 tasks shipped, <count> blocked (with reason).

Commits on deploy branch since session start:
  <hash> <subject>
  ... etc

Test suite: <passed>/<total>, coverage <X>%.

Hard gates closed by this session: G-14.

Action items for the user:
  - Spot-check Kai's "why X" answers next market-open day.
  - <any others surfaced during the session>

Anything I changed that wasn't in the plan: <list, with rationale>. If "none", say none.

Anything I deferred: <list, with rationale>.
```

---

## GUARDRAILS (these are not optional)

- **Deploy branch is `claude/kai-trader-phase-1-sHFJk`, NOT main.** Pushing to main does nothing for production. Always `git push origin main:claude/kai-trader-phase-1-sHFJk` after you push to main.
- **Render auto-deploy webhook is unreliable.** After every push, confirm a deploy event appears within 90 seconds. If not, ask the user to hit Manual Deploy in the Render dashboard. Do NOT retry the push (creates noisy commit hashes for nothing). Do NOT move to the next task until the user confirms the deploy is live.
- **Migrations require a manual `apply_migrations.py` run** after the code deploys. The bot only deploys code; schema is your responsibility. Run after T-6.1.0 and T-6.1.1.
- **Behaviour change is forbidden in this phase.** No new filters, no new caps, no ranker tweaks. The set of intents the funnel returns for a given input must be byte-identical before and after the phase. If a test for the existing funnel behaviour starts failing for any reason other than "the test was wrong about an unrelated detail", stop and ask. The phase is plumbing, not strategy.
- **Audit failure must never block trading.** `record_tick_decisions` failures are WARNING-level logs, not exceptions that bubble out of the tick. The trading path takes priority.
- **Use `executemany` for batch inserts.** Per-row INSERTs in a loop will be slow and noisy in logs. Single batch per tick.
- **Decimals are strings in jsonb.** Match the existing `intent_payload` convention. Float Decimal serialisation will silently round; never use it.
- **No new dependencies (`uv add`) without telling the user first.** If you think you need one, stop and ask. The codebase already has asyncpg, structlog, pydantic, anthropic, alpaca-py — that's enough.
- **Conventional commits only.** `feat`, `fix`, `chore`, `test`, `docs`, `refactor`. **No em-dashes anywhere** (code, comments, docs, commit messages). Periods, commas, colons.
- **Migration sequence: 022 then 023.** If you accidentally pick a duplicate number, the migration runner will fail loudly; fix the number, don't fight the runner. Don't reuse 019 (missing for unknown reasons; leave it alone).
- **Don't push to `origin/main` without an explicit user say-so.** This session pushes to `main` then forwards to the deploy branch. If the user wants `main` left at the prior head, you can push to the deploy branch directly via `git push origin HEAD:claude/kai-trader-phase-1-sHFJk`.
- **Don't open PRs.** Direct push to deploy branch.
- **Don't create planning or analysis documents.** `LIVE_TRADING_PLAN.md` is the plan. Update it inline only if a task description is materially wrong (and tell the user when you do).
- **Don't widen `tick_decisions` schema in this phase.** If a future requirement surfaces (e.g. include the per-tick equity snapshot), add it in a new migration in a later phase. Migration 022 is frozen after it lands.

---

## Anti-patterns (don't do these)

1. Don't redesign the plan. If you have a strong opinion something is wrong, stop and tell the user. Don't act on it.
2. Don't catch broad exceptions silently. Log structured (`structlog`), re-raise, or return a typed result. The exception to this is `record_tick_decisions` failures, which are deliberately warning-and-continue (audit must not block trading).
3. Don't `print`. Use `structlog` via `kai_trader.logging.get_logger`.
4. Don't add config to constants when sleeve_config or another DB table would be the right home. For Phase 6.1, there is no new tunable behaviour, so this rule is mostly moot.
5. Don't lower coverage to make a test pass. Fix the test or fix the code.
6. Don't trust the auto-deploy webhook silently. Always confirm.
7. Don't rename or move existing fields in `intent_payload`. Phase 6.1 only ADDS keys. Existing chat tools and queries must continue to work.
8. Don't try to backfill `tick_id` on historical orders. They stay NULL forever. The new column is forward-only.

---

## Critical context (you don't need to re-discover this)

The discovery that prompted this work: during a 2026-05-02 diagnostic, the operator asked "why did the bot pick SNAP" and the only available answer was reconstructed from the live position plus the `intent_payload` jsonb. The funnel had already computed the answer (per-symbol decision tag + structured reason) inside `BuildDiagnostics`, but rendered it into the Telegram heartbeat as free-form text and discarded the structure. The 5 SNAP fills (10 contracts each, between 2026-05-01 18:22-18:48 UTC) and the 3 MARA fills happened before the per-name 15% cap and per-tick velocity cap landed (commits `98afd75` and `c0824a5`); the over-concentration is pre-fix. Phase 6.1 makes the rationale queryable so the next over-concentration (or near-miss) is diagnosable in seconds, not minutes of grep.

The chat accuracy hardening from the same day (commit `deca5ad`) gave Kai the LIVE/HISTORY routing and freshness rules. The new `query_tick_decisions` tool slots into the HISTORY class. Kai will use it when asked "why was X picked" or "why did we skip Y" and report stored facts, not narrate from intuition.

---

## Begin

Start with Step 0 pre-flight. Do not skip any check. Report status after Step 0 completes.

---

> End of paste-this-into-Claude block.
