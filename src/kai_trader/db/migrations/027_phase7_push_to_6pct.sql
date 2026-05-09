-- Migration 027 (Phase 7): final push to the 6%/month target.
--
-- profit_take_pct      0.20 → 0.15  (faster cycle = more cycles/mo)
-- target_delta_put_risk_on  -0.45 → -0.50  (ATM = max premium)
-- target_delta_put_neutral  -0.30 → -0.40
-- target_delta_call    0.30 → 0.40
-- target_dte_max       14 → 21      (wider DTE band, more candidates)
--
-- Combined with code changes in Phase 7 (yield floor → 0, risk_off
-- regime filter dropped), the strategy now harvests vol in every
-- regime, with maximum premium per trade and minimum cycle time.
--
-- Risk profile: very aggressive. ATM puts assign frequently. CC
-- exposure on assigned stock is significant. Drawdowns 30-50%
-- expected. The income target requires this risk envelope.

update sleeve_config
   set profit_take_pct = 0.150,
       target_delta_put_risk_on = -0.500,
       target_delta_put_neutral = -0.400,
       target_delta_call = 0.400,
       target_dte_max = 21,
       updated_at = now(),
       updated_by = 'migration_027'
 where sleeve = 'index_core';
