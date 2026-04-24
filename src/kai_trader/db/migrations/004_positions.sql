-- Migration 004: options positions ledger.
-- Schema only for Phase 1. The trading engine will populate this in later
-- phases; Phase 1 /positions returns a placeholder response.

create table if not exists positions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  symbol text not null,
  option_type text check (option_type in ('put', 'call')),
  strike numeric(10,2) not null,
  expiration date not null,
  contracts int not null,
  side text check (side in ('short', 'long')) not null,
  entry_credit numeric(10,2),
  entry_delta numeric(5,4),
  status text check (status in ('open', 'closed', 'assigned', 'expired', 'rolled')) not null,
  closed_at timestamptz,
  close_pnl numeric(10,2),
  sleeve text check (sleeve in ('index_core', 'stable_largecap', 'opportunistic')),
  alpaca_order_id text,
  notes text
);

create index if not exists idx_positions_open
  on positions(status)
  where status = 'open';

create index if not exists idx_positions_symbol
  on positions(symbol);
