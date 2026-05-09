-- Migration 025 (Phase 5 gate retuning): expand universe to 12 names.
--
-- Migration 023 cut the universe to 8 high-IV names for income
-- generation. Backtest results (phase3a/3b/3c/4) showed deployment
-- ratio dropped to 15% of cash because cooldown windows + earnings
-- blackouts on a small universe leave most ticks with no eligible
-- candidates. Phase 5 expands to 12 names: keeps the 8 original
-- high-IV cohort and adds 4 mid-priced names with reasonable IV +
-- weekly options + strike notionals that fit a $30k account at the
-- 15% per-name dollar cap ($4,500).
--
-- Adds: F (~$13), INTC (~$25), GM (~$50), KMI (~$30).
--
-- Trade-off: F/GM/KMI have moderate IV (20-30%) vs the original
-- cohort (50-90%). The IV percentile gate (lowered to 25th in
-- Phase 5) will route flow to whichever names have rich vol on a
-- given tick — the moderate-IV adds provide deployment continuity
-- when the high-IV cohort is in earnings blackout or cooldown.

update sleeve_config
   set symbol_whitelist = '[
         "MARA", "RIOT", "SOFI", "HOOD",
         "PLTR", "MU", "SNAP", "RIVN",
         "F", "INTC", "GM", "KMI"
       ]'::jsonb,
       updated_at = now(),
       updated_by = 'migration_025'
 where sleeve = 'index_core';
