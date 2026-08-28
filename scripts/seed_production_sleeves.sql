-- Seed a FRESH Kai Trader database with the live production sleeve_config.
-- Captured from the running bot on 2026-08-28 (kai_chat_ro, read-only).
--
-- Run AFTER scripts/apply_migrations.py on each new parallel-account DB, so
-- every account starts from an identical strategy configuration and the only
-- variable across accounts is starting equity. Idempotent: safe to re-run.
--
-- Sleeve targets are FRACTIONS of equity, so this same config scales to any
-- account size without edits. That is the point: it keeps the capital-tier
-- comparison clean.

update sleeve_config set
  target_pct = 0.3500,
  target_delta_put_risk_on = -0.400,
  target_delta_put_neutral = -0.300,
  target_delta_call = 0.300,
  target_dte_min = 7,
  target_dte_max = 10,
  profit_take_pct = 0.500,
  roll_trigger_delta = 0.450,
  symbol_whitelist = '["MARA", "RIOT", "SOFI", "RIVN"]'::jsonb,
  enabled = true,
  earnings_blackout_enabled = true,
  max_new_entries_per_tick = 5,
  updated_at = now(),
  updated_by = 'seed_production_sleeves'
where sleeve = 'index_core';

update sleeve_config set
  target_pct = 0.4500,
  target_delta_put_risk_on = -0.400,
  target_delta_put_neutral = -0.300,
  target_delta_call = 0.300,
  target_dte_min = 7,
  target_dte_max = 10,
  profit_take_pct = 0.300,
  roll_trigger_delta = 0.300,
  symbol_whitelist = '["NVDA", "AMD", "TSLA", "AVGO", "COIN", "PLTR", "SOFI", "MARA", "MU", "BABA", "SMCI", "MSTR", "RIOT", "SNAP"]'::jsonb,
  enabled = false,
  earnings_blackout_enabled = true,
  max_new_entries_per_tick = 2,
  updated_at = now(),
  updated_by = 'seed_production_sleeves'
where sleeve = 'opportunistic';

update sleeve_config set
  target_pct = 0.5500,
  target_delta_put_risk_on = -0.400,
  target_delta_put_neutral = -0.300,
  target_delta_call = 0.300,
  target_dte_min = 7,
  target_dte_max = 10,
  profit_take_pct = 0.500,
  roll_trigger_delta = 0.450,
  symbol_whitelist = '["F", "T", "PFE", "KMI", "BAC", "KO"]'::jsonb,
  enabled = true,
  earnings_blackout_enabled = true,
  max_new_entries_per_tick = 2,
  updated_at = now(),
  updated_by = 'seed_production_sleeves'
where sleeve = 'stable_largecap';

select sleeve, enabled, target_pct, symbol_whitelist from sleeve_config order by enabled desc, target_pct desc;