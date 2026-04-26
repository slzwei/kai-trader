-- Migration 005: account snapshot history.
-- Captures a point-in-time view of the Alpaca account so we can plot equity,
-- buying power, and day P&L over time without depending on Alpaca's own
-- portfolio history endpoints. Phase 2.9 writes these manually via the
-- /snapshot_now command; a periodic background writer can be added later.

create table if not exists account_snapshots (
  id uuid primary key default gen_random_uuid(),
  captured_at timestamptz default now() not null,
  equity numeric(14,2) not null,
  last_equity numeric(14,2) not null,
  cash numeric(14,2) not null,
  buying_power numeric(14,2) not null,
  portfolio_value numeric(14,2) not null,
  day_pl numeric(14,2) not null,
  status text not null,
  paper boolean not null
);

create index if not exists idx_account_snapshots_captured_at
  on account_snapshots(captured_at desc);
