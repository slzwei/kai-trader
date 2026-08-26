-- Migration 036: per-tick position snapshots for the web dashboard.
-- The strategy worker already fetches the full position book (short
-- options and long equity) on every open-market tick; this table
-- persists that fetch so the read-only dashboard can show near-live
-- positions from Postgres alone, with no broker keys anywhere near
-- the web service. One row per position per tick, grouped by
-- captured_at; the dashboard reads the newest capture group.
-- kai_chat_ro inherits SELECT via the role's default privileges.

create table if not exists position_snapshots (
  id uuid primary key default gen_random_uuid(),
  captured_at timestamptz not null,
  account_number text,
  symbol text not null,
  asset_kind text not null check (asset_kind in ('option', 'equity')),
  qty numeric(14, 4) not null,
  side text not null,
  avg_entry_price numeric(14, 4),
  current_price numeric(14, 4),
  market_value numeric(14, 2),
  unrealized_pl numeric(14, 2)
);

create index if not exists idx_position_snapshots_captured
  on position_snapshots(captured_at desc);
