-- Migration 024 (P2 from INCOME_PLAN.md): profit-take threshold 50% → 30%.
--
-- The 50% profit-take threshold means we hold positions until they've
-- captured half their full credit, which is roughly 60-70% of their
-- lifecycle. At 30% we exit at 20-30% of the lifecycle and redeploy
-- the freed collateral into the next opportunity. Same dollar earns
-- 2-3x more cycles per month.
--
-- The math:
--   * 50% threshold + 7-day cycle ≈ 4 days holding ≈ 7 cycles/month
--   * 30% threshold + 7-day cycle ≈ 2 days holding ≈ 14 cycles/month
--   * Per-cycle yield drops from ~50% of credit to ~30%, but cycle
--     count doubles, so total monthly capture goes UP roughly 1.5x.
--
-- Risk: faster cycles mean more transactions, more fee drag, and more
-- opportunities for the bot to mis-time an entry (less compensated
-- premium per entry-decision). The post-profit-take cooldown (4 hours,
-- shipped commit 050254b) protects against the worst churn pattern.
--
-- Backtest validation under Phase 1.1 monthly compounding metrics
-- should show monthly_compound_rate_pct rising even though
-- per-cycle yield drops.

update sleeve_config
   set profit_take_pct = 0.300,
       updated_at = now(),
       updated_by = 'migration_024'
 where profit_take_pct != 0.300;
