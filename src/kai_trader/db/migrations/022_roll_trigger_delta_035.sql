-- Migration 022 (P4 from INCOME_PLAN.md): tighten roll trigger delta.
--
-- Roll trigger 0.50 → 0.35. The previous 0.50 trigger waits until a
-- short put has effectively become an at-the-money option before
-- considering a roll, by which point the position is already deep
-- in unrealized loss and the chain may not pay a net credit on a
-- defensive roll. Income-focused wheel operators trigger sooner
-- (0.30-0.40 delta band) — the position has moved against us but the
-- chain is still rich enough to pay a net credit on a further-OTM
-- roll, capping realized loss tighter.
--
-- Effect: more rolls fire (more decisions per tick), each averaging
-- a smaller realized loss when the roll is forced. Net expected
-- effect is risk-reducing without sacrificing income generation
-- (the trigger only fires on already-challenged positions).
--
-- Applies to all three sleeves so any future re-enabling of
-- stable_largecap / opportunistic inherits the same protective
-- trigger. Idempotent: re-running this migration is a no-op when
-- the value is already 0.350.

update sleeve_config
   set roll_trigger_delta = 0.350,
       updated_at = now(),
       updated_by = 'migration_022'
 where roll_trigger_delta != 0.350;
