-- Migration 014: approval-gated change proposals.
-- Kai never writes trades or params directly. The propose_change tool
-- inserts a row here with status='pending'. The dispatcher renders an
-- inline-keyboard message; on Approve, the applier flips status to
-- 'approved' then 'applied' (or 'failed') and writes to decision_log.
--
-- payload is the proposed new state, current_state captures what the
-- system shows now so the diff is reproducible later.

create table if not exists pending_changes (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('order', 'strategy_param', 'watchlist_edit')),
  payload jsonb not null,
  current_state jsonb,
  reason text,
  status text not null check (status in (
    'pending', 'approved', 'rejected', 'modified', 'applied', 'failed'
  )),
  proposed_by bigint not null,
  approved_by bigint,
  approved_at timestamptz,
  applied_at timestamptz,
  error_text text,
  created_at timestamptz not null default now()
);

create index if not exists idx_pending_changes_status
  on pending_changes(status, created_at desc);
