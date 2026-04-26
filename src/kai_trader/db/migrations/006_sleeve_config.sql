-- Migration 006: per-sleeve trading configuration.
-- Three known sleeves seeded with the calibrated PHASE3.md values for
-- a 3% monthly return target with <10% drawdown using 7 DTE weeklies.
-- Operator can edit values at runtime; the strategy worker re-reads
-- this table each tick.

create table if not exists sleeve_config (
  sleeve text primary key
    check (sleeve in ('index_core', 'stable_largecap', 'opportunistic')),
  target_pct numeric(5,4) not null,
  target_delta_put_risk_on numeric(4,3) not null,
  target_delta_put_neutral numeric(4,3) not null,
  target_delta_call numeric(4,3) not null,
  target_dte_min int not null,
  target_dte_max int not null,
  profit_take_pct numeric(4,3) not null,
  roll_trigger_delta numeric(4,3) not null,
  symbol_whitelist jsonb not null,
  enabled boolean not null default true,
  updated_at timestamptz default now() not null,
  updated_by text
);

insert into sleeve_config
  (sleeve, target_pct, target_delta_put_risk_on, target_delta_put_neutral,
   target_delta_call, target_dte_min, target_dte_max, profit_take_pct,
   roll_trigger_delta, symbol_whitelist)
values
  ('index_core', 0.40, -0.30, -0.20, 0.20, 7, 10, 0.50, 0.45,
   '["SPY", "QQQ", "IWM"]'),
  ('stable_largecap', 0.40, -0.30, -0.20, 0.20, 7, 10, 0.50, 0.45,
   '["AAPL", "MSFT", "GOOGL", "AMZN", "META", "V", "JPM"]'),
  ('opportunistic', 0.20, -0.30, -0.20, 0.20, 7, 10, 0.50, 0.45,
   '["NVDA", "AMD", "TSLA", "AVGO", "COIN"]')
on conflict (sleeve) do nothing;
