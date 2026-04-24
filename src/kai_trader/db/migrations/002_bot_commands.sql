-- Migration 002: audit log for every Telegram command received.
-- Authorised and unauthorised attempts both land here.

create table if not exists bot_commands (
  id uuid primary key default gen_random_uuid(),
  received_at timestamptz default now(),
  telegram_user_id bigint not null,
  command text not null,
  args text,
  authorized boolean not null,
  response_sent boolean default false,
  error text
);

create index if not exists idx_bot_commands_received
  on bot_commands(received_at desc);
