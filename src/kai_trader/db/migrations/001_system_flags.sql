-- Migration 001: system-wide feature flags.
-- Used by the trading engine (later phases) and the bot to gate behaviour.

create table if not exists system_flags (
  key text primary key,
  value text not null,
  updated_at timestamptz default now(),
  updated_by text
);

insert into system_flags (key, value) values
  ('trading_enabled', 'false'),
  ('new_entries_enabled', 'false'),
  ('kill_switch', 'false')
on conflict (key) do nothing;
