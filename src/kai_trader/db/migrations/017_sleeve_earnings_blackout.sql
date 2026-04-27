-- Migration 017: per-sleeve earnings blackout flag.
-- Phase 5d: when enabled, the candidate builder skips any underlying
-- whose next earnings announcement falls inside the sleeve's DTE
-- window. Default true for all three sleeves; operator can opt out
-- per sleeve via update_sleeve.

alter table sleeve_config
  add column if not exists earnings_blackout_enabled boolean not null default true;
