-- Migration 032: weekly AI review run history.
-- Phase 6.1 scaffold. One row per scheduled (or manual) weekly review
-- run. Stores token usage, cost, the model's structured findings, and
-- any error so the rolling 30-day cost ceiling check and a future
-- dashboard view can query history without re-invoking the model.
--
-- Severity is constrained to the three rubric levels. Nullable because
-- a run that errored before the model produced a result has no
-- severity to record. Same reason summary, findings_json, and the
-- token columns are nullable.

create table if not exists weekly_reviews (
  id uuid primary key default gen_random_uuid(),
  run_at timestamptz not null default now(),
  model text not null,
  prompt_tokens integer,
  output_tokens integer,
  cache_read_tokens integer,
  cost_usd numeric(10, 4),
  tool_call_count integer,
  summary text,
  overall_severity text check (overall_severity in ('info', 'warning', 'critical')),
  findings_json jsonb,
  error text
);

create index if not exists idx_weekly_reviews_run_at
  on weekly_reviews(run_at desc);
