-- Migration 023 (P1 from INCOME_PLAN.md): concentrate universe to high-IV cohort.
--
-- The 30-name universe under migration 018 was sized for capital
-- preservation: a broad pool of moderately-priced liquid names
-- diversifies single-name risk but dilutes per-dollar yield. For
-- 6%/month income generation we need top-decile IV exposure, so
-- this migration trims the universe to 8 high-IV names that
-- consistently produce IV percentile rank >= 50th over 252-day
-- windows in 2024-2026 data.
--
-- The chosen 8:
--   * MARA, RIOT — crypto miners. Routinely >80% IV; tightly-coupled
--     to BTC moves so vol surface is rich on both directions.
--   * SOFI, HOOD — retail fintech. 50-60% IV with active weekly chains.
--   * PLTR — high-multiple growth. 50-60% IV; deep weekly liquidity.
--   * SNAP — social media + ad-cycle exposure. 50-60% IV.
--   * RIVN — EV growth + binary catalyst risk. 60-80% IV.
--   * MU — semis cyclical. 40-60% IV. Anchors the cohort with a more
--     defensive vol profile.
--
-- Existing positions on dropped names (F, T, BAC, PFE, KO, KVUE, VZ,
-- INTC, CSCO, GE, KMI, KHC, MO, WBA, WFC, GM, C, GDX, SLV, XLF, XLE,
-- EEM) continue to be MANAGED — the strategy can still close, profit-
-- take, roll, or accept assignment on them. Only NEW entries are
-- restricted by the new whitelist. This avoids a forced liquidation
-- on the migration date.
--
-- Risk acknowledged: this concentrates significantly. The 8 names are
-- correlated across crypto (MARA, RIOT), fintech (SOFI, HOOD, PLTR),
-- and growth-cycle (PLTR, SNAP, RIVN, MU). A 2018-style vol-spike or
-- 2022-style growth crash will hit all 8 together. P4 (tighter roll
-- trigger, migration 022) ships first to cap the per-position loss
-- on a vol-spike.

update sleeve_config
   set symbol_whitelist = '[
         "MARA", "RIOT", "SOFI", "HOOD",
         "PLTR", "MU", "SNAP", "RIVN"
       ]'::jsonb,
       updated_at = now(),
       updated_by = 'migration_023'
 where sleeve = 'index_core';
