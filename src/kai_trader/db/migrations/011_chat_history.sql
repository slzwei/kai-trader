-- Migration 011: per-owner chat transcript with Kai.
-- Phase 4 conversational bot. Each user-or-assistant turn is one row.
-- Older halves of long transcripts are replaced by a single role='system'
-- summary row when the count exceeds the compaction threshold.

create table if not exists chat_history (
  id uuid primary key default gen_random_uuid(),
  telegram_id bigint not null,
  role text not null check (role in ('user', 'assistant', 'system')),
  content jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_chat_history_user_recent
  on chat_history(telegram_id, created_at desc);
