-- Migration 021: surface target_delta and actual_delta as queryable columns.
-- W-9: the orders.intent_payload jsonb already contains target_delta,
-- but extracting it for monitoring requires a JSONB cast on every
-- query. Promote it to a real column. actual_delta is new: post-fill
-- we look up the live chain to record the contract's delta at fill
-- time so we can detect fills that landed materially outside the
-- regime target.

alter table orders
  add column if not exists target_delta numeric;

alter table orders
  add column if not exists actual_delta numeric;

create index if not exists orders_actual_delta_idx
  on orders (actual_delta)
  where actual_delta is not null;
