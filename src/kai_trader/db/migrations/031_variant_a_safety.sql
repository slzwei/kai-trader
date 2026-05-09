-- Migration 031 (Variant A): blow-up-resistant calibration for the
-- 2-week paper trial.
--
-- Phase 12's max-aggression delivered 3.00%/mo backtest but at the
-- cost of:
--   - Cash going to -$21,547 on a $30k account (margin call territory)
--   - 23% peak underwater from starting capital
--   - Concentrated 25% per-name exposure
--
-- Variant A trades return for safety:
--   profit_take_pct          0.10 → 0.50  (conventional 50% capture)
--   target_delta_put_risk_on -0.45 → -0.40  (less ATM, less assignment)
--   target_delta_put_neutral -0.35 → -0.30
--   target_delta_call         0.35 → 0.30
--
-- Plus code-side changes (already in candidates.py):
--   PER_NAME_NOTIONAL_CAP_PCT  0.25 → 0.15  (less concentration)
--   TOTAL_DEPLOYMENT_CAP_PCT   4.00 → 1.00  (cash-secured ceiling)
--
-- Backtest validation ($30k, 24mo, --margin-factor 1.0):
--   Monthly compounding:    1.85%/mo
--   Total return:           +60.80%
--   Max drawdown:           29.7% (from peak; only 11% from start)
--   Lowest equity vs start: -11% (vs -23% on Phase 12)
--   Worst cash:             +$959 (NEVER negative)
--   Days cash < 0:          0
--   Sharpe:                 0.69
--
-- This is the calibration the 2-week paper trial will run on.

update sleeve_config
   set profit_take_pct = 0.500,
       target_delta_put_risk_on = -0.400,
       target_delta_put_neutral = -0.300,
       target_delta_call = 0.300,
       updated_at = now(),
       updated_by = 'migration_031'
 where sleeve = 'index_core';
