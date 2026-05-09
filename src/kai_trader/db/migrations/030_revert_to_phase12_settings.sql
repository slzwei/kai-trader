-- Migration 030: revert Phase 9's overshoot, restore Phase 12 settings.
--
-- Phase 9 (migration 029) lowered profit_take_pct to 0.05 and disabled
-- the earnings blackout. Backtest showed those changes WORSENED the
-- monthly return (2.71% vs Phase 8's 3.00%) because:
--   - 5% profit-take exits before theta delivers
--   - dropping earnings caused more losing trades than the extra
--     deployment gained
--
-- Phase 11/12 reverted these in the backtest CLI defaults but never
-- shipped a migration. Result: the production bot was running Phase 9
-- settings while the headline numbers I quoted were from Phase 12 backtests.
--
-- This migration restores the Phase 8/12 production state:
--   profit_take_pct          0.05 → 0.10
--   earnings_blackout_enabled False → True
--   target_delta_put_neutral -0.350 → -0.350 (unchanged, Phase 11/12)
--   target_delta_call         0.350 → 0.350 (unchanged)
--   max_new_entries_per_tick  5 → 5 (unchanged)
--
-- All other Phase 12 settings (delta -0.45, DTE max 14, roll
-- trigger 0.35, 12-name universe) are already in place from
-- earlier migrations.

update sleeve_config
   set profit_take_pct = 0.100,
       earnings_blackout_enabled = true,
       updated_at = now(),
       updated_by = 'migration_030'
 where sleeve = 'index_core';
