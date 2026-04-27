-- Migration 013: outbound event queue for the proactive dispatcher.
-- The trading engine and the chat layer write rows here. The event
-- dispatcher worker drains them, formats each for Telegram, and sends.
-- dispatched_at is set when the row has been delivered.

create table if not exists events (
  id uuid primary key default gen_random_uuid(),
  kind text not null,
  payload jsonb not null,
  dispatched_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists idx_events_undispatched
  on events(created_at)
  where dispatched_at is null;

create index if not exists idx_events_kind on events(kind, created_at desc);
