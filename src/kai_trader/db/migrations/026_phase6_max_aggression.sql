-- Migration 026 (Phase 6 max-aggression): push profit-take and delta
-- targets to support the 6%/month income objective.
--
-- Three sleeve_config changes:
--
-- 1. profit_take_pct 0.30 → 0.20. Faster cycle, smaller per-cycle
--    yield, but materially more cycles per month. The post-profit-
--    take cooldown is also disabled in Phase 6 (in candidates.py),
--    so the freed cash redeploys on the next tick.
--
-- 2. target_delta_put_risk_on -0.40 → -0.45. Closer to the money,
--    more premium per dollar of collateral. Standard wheel
--    practitioner range for income generation. Assignment risk goes
--    up but assignments produce stock that we sell CCs on — the
--    full wheel mechanic.
--
-- 3. target_dte_max 10 → 14. Wider DTE band gives more candidate
--    expirations per tick, especially when one expiration is
--    earnings-blackout'd or thinly traded. Also unlocks 14-day
--    contracts which often have richer per-day yield than 7-day.

update sleeve_config
   set profit_take_pct = 0.200,
       target_delta_put_risk_on = -0.450,
       target_dte_max = 14,
       updated_at = now(),
       updated_by = 'migration_026'
 where sleeve = 'index_core';
