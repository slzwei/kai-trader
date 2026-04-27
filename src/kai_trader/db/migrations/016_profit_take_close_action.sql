-- Migration 016: extend orders.action to include profit_take_close.
-- Phase 5b: when a short put's current ask falls to (1 - profit_take_pct)
-- of the original credit, the strategy submits a buy-to-close order with
-- this distinct action type. Keeping it separate from the manual 'close'
-- action keeps the audit trail clean and lets future analytics filter on
-- captured-premium events specifically.
-- Idempotent: drops the old constraint by name if present, then re-adds.

alter table orders drop constraint if exists orders_action_check;

alter table orders add constraint orders_action_check
  check (action in (
    'open_short_put',
    'close',
    'roll',
    'open_covered_call',
    'close_covered_call',
    'assignment',
    'profit_take_close'
  ));
