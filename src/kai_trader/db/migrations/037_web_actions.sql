-- Migration 037: web approval queue (Phase U1).
-- The read-only dashboard can request that a pending change be
-- approved or rejected, but it must never hold write access to
-- trading configuration. This table is the entire bridge: the web
-- service INSERTs a request row, and the bot process (which owns the
-- applier and full credentials) validates and executes it, stamping
-- processed_at plus the outcome. kai_chat_ro is granted SELECT and
-- INSERT on this one table only; the grant is conditional so CI
-- databases without the role still migrate cleanly. The chat layer's
-- SQL validator and read-only transactions are unaffected by the
-- grant: query_supabase still rejects any non-SELECT statement.

create table if not exists web_actions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  pending_change_id uuid not null,
  action text not null check (action in ('approve', 'reject')),
  processed_at timestamptz,
  result text,
  error text
);

create index if not exists idx_web_actions_unprocessed
  on web_actions(created_at)
  where processed_at is null;

do $$
begin
  if exists (select from pg_roles where rolname = 'kai_chat_ro') then
    grant select, insert on web_actions to kai_chat_ro;
  end if;
end
$$;
