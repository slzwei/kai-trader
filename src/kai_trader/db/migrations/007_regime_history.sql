-- Migration 007: regime classifier transition log.
-- Append one row each time the regime changes since the previous row;
-- the strategy worker recomputes the regime every tick but only writes
-- when a transition actually happens. Operator can review history to
-- understand entry behaviour over time.

create table if not exists regime_history (
  id uuid primary key default gen_random_uuid(),
  captured_at timestamptz default now() not null,
  regime text not null check (regime in ('risk_on', 'neutral', 'risk_off')),
  vix numeric(8,4),
  vix_5d_change_pct numeric(8,4),
  spy_price numeric(10,4),
  spy_20dma numeric(10,4),
  spy_50dma numeric(10,4),
  realized_vol_10d_pct numeric(8,4),
  notes text
);

create index if not exists idx_regime_history_captured_at
  on regime_history(captured_at desc);
