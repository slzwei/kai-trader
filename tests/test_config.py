"""Tests for typed settings."""

from __future__ import annotations

import pytest

from kai_trader import config as config_module


def test_settings_load_from_env() -> None:
    s = config_module.get_settings()
    assert s.telegram_owner_id == 42
    assert s.supabase_project_ref == "test-ref"
    assert s.database_url.startswith("postgresql://postgres:")
    assert "db.test-ref.supabase.co:5432/postgres" in s.database_url


def test_env_completeness_reports_each_key() -> None:
    status = config_module.get_settings().env_completeness()
    assert set(status.keys()) == {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_ID",
        "SUPABASE_URL",
        "SUPABASE_DB_PASSWORD",
        "SUPABASE_KEY",
    }
    assert all(status.values())


def test_env_completeness_marks_unset_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    config_module.reset_settings_cache()
    status = config_module.get_settings().env_completeness()
    assert status["SUPABASE_KEY"] is False


def test_non_supabase_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.com")
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    with pytest.raises(ValueError):
        _ = s.supabase_project_ref


def test_trailing_slash_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test-ref.supabase.co/")
    config_module.reset_settings_cache()
    assert config_module.get_settings().supabase_url == "https://test-ref.supabase.co"


def test_database_url_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    override = (
        "postgresql://postgres.test-ref:override-pw"
        "@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres"
    )
    monkeypatch.setenv("DATABASE_URL", override)
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    assert s.database_url == override
    # Direct host pieces must not leak through when the override is set.
    assert "db.test-ref.supabase.co" not in s.database_url
