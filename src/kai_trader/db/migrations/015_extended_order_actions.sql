-- Migration 015: extend orders.action to include covered call lifecycle.
-- Phase 5a adds open_covered_call (sell call against assigned shares),
-- close_covered_call (buy back), and assignment (audit row when a CSP
-- exercises and shares land). The original CHECK constraint enumerates
-- only put-side actions, so we drop and recreate it with the new set.
-- Idempotent: drops the old constraint by name if present, then re-adds.

alter table orders drop constraint if exists orders_action_check;

alter table orders add constraint orders_action_check
  check (action in (
    'open_short_put',
    'close',
    'roll',
    'open_covered_call',
    'close_covered_call',
    'assignment'
  ));
