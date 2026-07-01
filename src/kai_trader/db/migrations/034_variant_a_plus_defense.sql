-- Migration 034 (Variant A+): defensive tuning to harden the live 15%/yr target.
--
-- The 7-week live trial on Variant A (migration 031) confirmed the premium
-- engine works (+$2,325 realized over 46 round-trips) but leaks most of it
-- to assignment into downtrending names (SNAP -21%, F -9% on 2026-06-30).
-- This migration ships the parameter half of the "keep more of the premium"
-- fix. The code half (a 50-DMA trend filter on new puts, a covered-call
-- cost-basis floor, a tighter per-name cap, and an options-buying-power
-- safety buffer) lands alongside in candidates.py / covered_calls.py.
--
-- Two changes here:
--
--   1. DTE band 7-14 -> 7-10 on index_core (P2). The owner's mandate is
--      7-10 DTE only. index_core still carried the old 7-14 band; the
--      other two sleeves are already 7-10. Concentrating on 7-10 harvests
--      the steepest part of the theta curve and shortens the assignment-
--      risk window per cycle.
--
--   2. roll_trigger_delta 0.35 -> 0.30 on all sleeves (P4). Rolling the
--      short put earlier (while it is less in-the-money) buys runway to
--      find a net-credit roll before assignment. In the trial, assignments
--      (8) outpaced credit rolls (6); a lower trigger tilts that back
--      toward rolling and away from being put weak stock.
--
-- Not changed here (staged for a follow-up, like the delta booster P7):
-- the small-debit roll-down-and-out behaviour. Changing roll economics on
-- live capital earns its own migration and validation.

update sleeve_config
   set target_dte_max = 10,
       updated_at = now(),
       updated_by = 'migration_034'
 where sleeve = 'index_core'
   and target_dte_max <> 10;

update sleeve_config
   set roll_trigger_delta = 0.300,
       updated_at = now(),
       updated_by = 'migration_034'
 where roll_trigger_delta <> 0.300;
