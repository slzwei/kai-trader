-- Migration 010: flip new_entries_enabled to true.
-- Phase 3.6 wires submit_short_put to honour this flag. Initial seed in
-- migration 001 left it false (Phase 1 had no caller). Operators who want
-- new entries paused while keeping rolls and closes active can flip it
-- back off via /flag new_entries_enabled off at any time.

update system_flags
   set value = 'true',
       updated_at = now(),
       updated_by = 'migration_010'
 where key = 'new_entries_enabled'
   and value = 'false';
