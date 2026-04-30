-- Migration 018: small-account single-sleeve pool with per-tick entry cap.
-- Phase 5f: the prior 25/30/45 sleeve split fragments capital below the
-- threshold where any whitelist contract fits ($25k accounts can't deploy
-- a single SPY/QQQ/AAPL CSP within the per-sleeve cap). This migration
-- collapses to a single active sleeve at full deployment, expands the
-- whitelist to a 30-name pool of cheap-liquid optionable names, and adds
-- a per-tick entry cap so a 30-name pool does not flood the book on a
-- single tick.
--
-- The new ranker in candidates.py picks the best entries each tick by
-- annualised yield * spread quality, so the strategy enters only the
-- best 1-2 names per tick from the broader pool.
--
-- index_core: target_pct = 1.00 (sleeve cap no longer fragments).
-- stable_largecap: enabled = false.
-- opportunistic: enabled = false.
--
-- The 30-name pool mixes defensives (telecom, staples, financials,
-- pharma), cyclical growth (semis, fintech), quality mid-caps, and
-- non-correlated ETFs (gold miners, silver, energy, EM, sector-spy).
-- All have weekly options on Alpaca's OPRA feed; all have low enough
-- per-contract collateral to fit a $25k account.

alter table sleeve_config
  add column if not exists max_new_entries_per_tick int not null default 2;

update sleeve_config
   set target_pct = 1.00,
       symbol_whitelist = '[
         "F", "T", "BAC", "PFE", "KO", "KVUE", "VZ", "INTC", "CSCO",
         "GE", "KMI", "KHC", "MO", "WBA",
         "HOOD", "SOFI", "PLTR", "MU", "MARA", "RIOT", "SNAP", "RIVN",
         "WFC", "GM", "C",
         "GDX", "SLV", "XLF", "XLE", "EEM"
       ]'::jsonb,
       max_new_entries_per_tick = 2,
       updated_at = now(),
       updated_by = 'migration_018'
 where sleeve = 'index_core';

update sleeve_config
   set enabled = false,
       updated_at = now(),
       updated_by = 'migration_018'
 where sleeve in ('stable_largecap', 'opportunistic');
