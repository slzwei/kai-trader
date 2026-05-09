-- Migration 028 (Phase 8): revert Phase 7 ATM delta and switch to
-- 10% profit-take for ultra-fast cycling.
--
-- Phase 7 (delta -0.50 ATM, profit-take 0.15) caused 59 cash-
-- exhaustion broker rejections from too many assignments and
-- delivered 2.30%/mo. Phase 6 (delta -0.45, profit-take 0.20)
-- delivered 2.50%/mo without the rejection storm.
--
-- Phase 8 returns to the Phase 6 delta band (-0.45) but pushes
-- profit-take to 0.10 (down from 0.20). At 10% the bot exits at
-- ~10-15% of position lifecycle, redeploying the freed cash
-- immediately. Hypothesis: 2x cycle count compensates for the
-- smaller per-cycle yield, pushing monthly return higher.

update sleeve_config
   set profit_take_pct = 0.100,
       target_delta_put_risk_on = -0.450,
       target_delta_put_neutral = -0.350,
       target_delta_call = 0.350,
       target_dte_max = 14,
       updated_at = now(),
       updated_by = 'migration_028'
 where sleeve = 'index_core';
