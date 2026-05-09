-- Migration 029 (Phase 9): drop earnings blackout, push to 5% profit-take.
--
-- Phase 8 (delta -0.45, profit-take 0.10) hit 3.00%/mo. Phase 9 pushes
-- two more levers:
--
-- 1. profit_take_pct 0.10 → 0.05. Even faster cycle (exits at 5% of
--    credit captured, redeploys immediately). Per-cycle yield drops
--    materially but cycle count rises proportionally.
--
-- 2. earnings_blackout_enabled = False. The 14-day blackout was
--    killing deployment in earnings season — most names report
--    quarterly so 4-6 weeks/year per name are dead. Dropping it
--    accepts the binary-event vol risk in exchange for continuous
--    premium capture. The roll trigger (0.35) catches positions
--    that gap challenged post-earnings before they assign.
--
-- 3. max_new_entries_per_tick 2 → 5. With profit-take cycling so
--    fast, each tick may have many freshly-redeployable candidates;
--    the previous cap of 2 was throttling throughput.
--
-- Risk profile: very aggressive. Drawdowns 30-50% expected, with
-- earnings vol-spikes adding tail risk. The income target requires
-- this envelope.

update sleeve_config
   set profit_take_pct = 0.050,
       earnings_blackout_enabled = false,
       max_new_entries_per_tick = 5,
       updated_at = now(),
       updated_by = 'migration_029'
 where sleeve = 'index_core';
