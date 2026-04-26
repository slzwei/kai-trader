-- Migration 009: recalibrate sleeves to push toward 3% monthly target.
-- Wider whitelists with mix of price points (cheap names like F, SOFI,
-- PLTR enable multi-contract deployment within per-symbol concentration
-- caps), higher target delta in risk_on, allocations shifted toward
-- opportunistic where the IV juice lives.
--
-- Deltas: -0.40 in risk_on (was -0.30), -0.30 in neutral (was -0.20).
-- Allocations: 25 / 30 / 45 (was 40 / 40 / 20).
-- Roll trigger: 0.50 (was 0.45) so we let trades work harder before rolling.
-- Profit take: 50% (unchanged).
-- DTE band: 7-10 (unchanged).
--
-- Note: opportunistic sleeve stays enabled. The pause-in-neutral behaviour
-- is moved out of the data layer; the regime classifier no longer pauses
-- it because we want premium juice across all regimes when not in risk_off.

update sleeve_config
   set target_pct = 0.25,
       target_delta_put_risk_on = -0.40,
       target_delta_put_neutral = -0.30,
       target_delta_call = 0.30,
       roll_trigger_delta = 0.50,
       symbol_whitelist = '["SPY", "QQQ", "IWM", "DIA"]',
       updated_at = now(),
       updated_by = 'migration_009'
 where sleeve = 'index_core';

update sleeve_config
   set target_pct = 0.30,
       target_delta_put_risk_on = -0.40,
       target_delta_put_neutral = -0.30,
       target_delta_call = 0.30,
       roll_trigger_delta = 0.50,
       symbol_whitelist = '["AAPL", "MSFT", "GOOGL", "AMZN", "META", "V", "JPM", "BAC", "DIS", "KO", "F", "T", "PFE", "C"]',
       updated_at = now(),
       updated_by = 'migration_009'
 where sleeve = 'stable_largecap';

update sleeve_config
   set target_pct = 0.45,
       target_delta_put_risk_on = -0.40,
       target_delta_put_neutral = -0.30,
       target_delta_call = 0.30,
       roll_trigger_delta = 0.50,
       symbol_whitelist = '["NVDA", "AMD", "TSLA", "AVGO", "COIN", "PLTR", "SOFI", "MARA", "MU", "BABA", "SMCI", "MSTR", "RIOT", "SNAP"]',
       updated_at = now(),
       updated_by = 'migration_009'
 where sleeve = 'opportunistic';
