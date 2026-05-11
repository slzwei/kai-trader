-- Migration 033: tag account snapshots with the Alpaca account number.
-- Without this, swapping Alpaca accounts (paper -> paper test, paper ->
-- live, or any account reset) leaves the previous account's snapshots
-- in the table. The drawdown circuit breaker reads the last 7 days of
-- equity unconditionally, so a fresh 30k account inheriting a 100k
-- predecessor's high-water mark trips a 70% false drawdown on the next
-- strategy tick.
--
-- The column is nullable so pre-migration rows survive. Production
-- writes will always carry the value (Alpaca always returns it). The
-- drawdown breaker filters by current account number, so legacy rows
-- with NULL are quietly excluded from the comparison.
--
-- The composite index supports both the drawdown lookback query
-- (account_number = $1 order by captured_at desc) and the existing
-- newest-first scans through index-only inclusion of captured_at.

alter table account_snapshots
  add column if not exists account_number text;

create index if not exists idx_account_snapshots_account_captured
  on account_snapshots(account_number, captured_at desc);
