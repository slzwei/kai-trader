"""Database access layer for Kai Trader.

All DB traffic flows through asyncpg against the Supabase-hosted Postgres
instance. Migrations are plain SQL files under ``migrations/`` and are applied
by ``scripts/apply_migrations.py``.
"""
