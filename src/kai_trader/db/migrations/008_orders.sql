-- Migration 008: order intent and submission audit.
-- Every trade decision the strategy worker makes lands here, regardless of
-- whether it actually went to Alpaca. gating_decision captures the flag
-- state at decision time so we can review later why a trade was or was not
-- submitted.

create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now() not null,
  sleeve text not null,
  symbol text not null,
  option_symbol text not null,
  action text not null check (action in ('open_short_put', 'close', 'roll')),
  intent_payload jsonb not null,
  alpaca_order_id text,
  status text not null check (status in (
    'pending', 'submitted', 'filled', 'cancelled', 'skipped_by_flag', 'failed'
  )),
  gating_decision jsonb,
  submitted_at timestamptz,
  filled_at timestamptz,
  filled_avg_price numeric(12,4),
  error_text text
);

create index if not exists idx_orders_status on orders(status);
create index if not exists idx_orders_created_at on orders(created_at desc);
create index if not exists idx_orders_alpaca_id on orders(alpaca_order_id)
  where alpaca_order_id is not null;
