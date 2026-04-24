-- Migration 003: outbound notification queue.
-- The trading engine (later phases) will enqueue rows here; the bot or a
-- dedicated worker will drain them and deliver via Telegram or SMS.

create table if not exists notifications (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  sent_at timestamptz,
  priority text check (priority in ('info', 'alert', 'critical')) not null,
  channel text check (channel in ('telegram', 'sms', 'both')) not null,
  message text not null,
  metadata jsonb,
  retry_count int default 0,
  max_retries int default 3
);

create index if not exists idx_notifications_unsent
  on notifications(created_at)
  where sent_at is null;
