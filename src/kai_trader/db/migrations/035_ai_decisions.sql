-- Migration 035: AI decision audit log (Phase A1).
-- One row per candidate the AI decision layer evaluated, TAKE and
-- REJECT alike, including fail-closed error rejections and cache
-- replays. This is the future evaluation/training dataset, so the row
-- carries the full candidate packet the model saw, the validated
-- response, model/prompt lineage, token/latency/cost accounting, and
-- the final pipeline disposition (what actually happened downstream:
-- rejected_by_ai, forwarded_to_gate, gate_rejected, submitted,
-- skipped_by_flag, submit_failed).
--
-- decision is constrained to the two operational answers; the schema
-- has no MAYBE. A row born from a failure path is decision='REJECT'
-- with error set and the model-judgment columns null. Numeric AI
-- fields are nullable for exactly that error path; candidate_packet is
-- not, because even a failed evaluation knows what it was evaluating.

create table if not exists ai_decisions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  sleeve text not null,
  symbol text not null,
  option_symbol text not null,
  decision text not null check (decision in ('TAKE', 'REJECT')),
  confidence numeric(4, 3),
  ai_score numeric(4, 3),
  quant_score numeric,
  final_score numeric,
  event_risk text check (event_risk in ('LOW', 'MEDIUM', 'HIGH', 'EXTREME')),
  fundamental_view text check (fundamental_view in (
    'VERY_BEARISH', 'BEARISH', 'NEUTRAL', 'BULLISH', 'VERY_BULLISH'
  )),
  risk_flags jsonb,
  positive_factors jsonb,
  thesis text,
  candidate_packet jsonb not null,
  response_json jsonb,
  provider text not null,
  model text not null,
  prompt_version text not null,
  input_tokens int,
  output_tokens int,
  latency_ms int,
  cost_usd numeric(10, 6),
  cache_hit boolean not null default false,
  error text,
  pipeline_disposition text not null,
  source_freshness jsonb
);

create index if not exists idx_ai_decisions_created
  on ai_decisions(created_at desc);

create index if not exists idx_ai_decisions_symbol
  on ai_decisions(symbol, created_at desc);
