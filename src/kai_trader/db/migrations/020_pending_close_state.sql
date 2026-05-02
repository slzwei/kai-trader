-- Migration 020: persist /close staged state to Postgres.
-- W-5: the previous in-process _pending dict is lost on bot restart
-- (e.g. the 2026-04-30 OOM event), creating a window where staged
-- closes silently disappear. This table makes Postgres the source of
-- truth for staged closes; the in-memory cache in close.py becomes a
-- read-through optimisation.

create table if not exists pending_close (
    id bigserial primary key,
    user_id bigint not null,
    symbol text not null,
    staged_at timestamptz not null default now(),
    ttl_seconds int not null default 30,
    status text not null default 'staged',
    created_at timestamptz not null default now(),
    consumed_at timestamptz
);

create index if not exists pending_close_active_idx
    on pending_close (user_id, symbol)
    where status = 'staged';

create index if not exists pending_close_staged_at_idx
    on pending_close (staged_at desc);
