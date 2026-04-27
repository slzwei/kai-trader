-- Migration 012: applied-change audit log.
-- Every approval that the engine actually applies writes a row here.
-- inputs hold the proposed payload, outputs hold whatever the apply step
-- returned (e.g. an Alpaca order id, or {"stub": true} until order
-- placement is wired in).

create table if not exists decision_log (
  id uuid primary key default gen_random_uuid(),
  kind text not null,
  inputs jsonb not null,
  outputs jsonb not null,
  reason text,
  created_at timestamptz not null default now()
);

create index if not exists idx_decision_log_recent on decision_log(created_at desc);
create index if not exists idx_decision_log_kind on decision_log(kind, created_at desc);
