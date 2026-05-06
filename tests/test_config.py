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
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "DATABASE_URL_RO",
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


def test_effective_alpaca_key_paper_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_PAPER", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY_PAPER", raising=False)
    monkeypatch.setenv("ALPACA_PAPER", "true")
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    assert s.effective_alpaca_api_key == "PKTEST00000000000000"
    assert s.effective_alpaca_secret_key == "test-alpaca-secret"


def test_effective_alpaca_key_paper_uses_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_PAPER", "PK-EXPLICIT-PAPER")
    monkeypatch.setenv("ALPACA_SECRET_KEY_PAPER", "secret-paper")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    assert s.effective_alpaca_api_key == "PK-EXPLICIT-PAPER"
    assert s.effective_alpaca_secret_key == "secret-paper"


def test_effective_alpaca_key_live_requires_live_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.delenv("ALPACA_API_KEY_LIVE", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY_LIVE", raising=False)
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    with pytest.raises(ValueError, match="ALPACA_API_KEY_LIVE"):
        _ = s.effective_alpaca_api_key
    with pytest.raises(ValueError, match="ALPACA_SECRET_KEY_LIVE"):
        _ = s.effective_alpaca_secret_key


def test_effective_alpaca_key_live_uses_live_env_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("ALPACA_API_KEY_LIVE", "AK-REAL-LIVE")
    monkeypatch.setenv("ALPACA_SECRET_KEY_LIVE", "secret-live")
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    assert s.effective_alpaca_api_key == "AK-REAL-LIVE"
    assert s.effective_alpaca_secret_key == "secret-live"
    # Paper key must not leak into live mode even when configured.
    assert s.effective_alpaca_api_key != "PKTEST00000000000000"


def test_effective_alpaca_key_live_does_not_fallback_to_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy ALPACA_API_KEY exists in the test env and is the paper key.

    Live mode must refuse to use it as a fallback. This guards against a
    cutover where the operator flips ALPACA_PAPER=false but forgets to
    add the live keys: silently sending paper credentials to the live
    endpoint produces a confusing 401, not a clear configuration error.
    """
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.delenv("ALPACA_API_KEY_LIVE", raising=False)
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    # Legacy ALPACA_API_KEY is still set in the test env.
    assert s.alpaca_api_key.get_secret_value() == "PKTEST00000000000000"
    # But the effective resolver refuses it.
    with pytest.raises(ValueError):
        _ = s.effective_alpaca_api_key
