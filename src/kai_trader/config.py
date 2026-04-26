"""Typed configuration loaded from environment variables.

Settings are parsed once at startup via Pydantic. The rest of the codebase
imports ``get_settings()`` rather than reading ``os.environ`` directly so that
missing or malformed values fail loudly at boot instead of deep in a handler.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "staging", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Runtime configuration pulled from env vars or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: SecretStr = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_owner_id: int = Field(..., alias="TELEGRAM_OWNER_ID")

    supabase_url: str = Field(..., alias="SUPABASE_URL")
    supabase_key: SecretStr = Field(
        default=SecretStr("unset"),
        alias="SUPABASE_KEY",
        description="Service role JWT. Reserved for later phases.",
    )
    supabase_db_password: SecretStr = Field(..., alias="SUPABASE_DB_PASSWORD")
    database_url_override: SecretStr | None = Field(
        default=None,
        alias="DATABASE_URL",
        description=(
            "Full Postgres connection string. When set, used verbatim and the "
            "direct-host URL is not computed. Needed on IPv4-only networks "
            "where db.<ref>.supabase.co is unreachable, in which case paste "
            "the Session pooler string from the Supabase dashboard."
        ),
    )

    alpaca_api_key: SecretStr = Field(..., alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr = Field(..., alias="ALPACA_SECRET_KEY")
    alpaca_paper: bool = Field(
        default=True,
        alias="ALPACA_PAPER",
        description=(
            "If true, route through Alpaca paper. Live trading requires this "
            "flag plus the trading-enabled system flag flipped on."
        ),
    )

    env: Environment = Field(default="dev", alias="ENV")
    log_level: LogLevel = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="Asia/Singapore", alias="TIMEZONE")

    @field_validator("supabase_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def supabase_project_ref(self) -> str:
        """Extract the project ref (subdomain) from the Supabase URL."""
        host = urlparse(self.supabase_url).hostname or ""
        if not host.endswith(".supabase.co"):
            raise ValueError(
                f"SUPABASE_URL must be a *.supabase.co URL, got: {self.supabase_url}"
            )
        return host.split(".", 1)[0]

    @property
    def database_url(self) -> str:
        """Postgres connection string for the Supabase project.

        Prefers ``DATABASE_URL`` when set so operators on IPv4-only networks
        can point at the Supavisor pooler. Falls back to the direct host,
        which on modern projects is IPv6-only.
        """
        if self.database_url_override is not None:
            return self.database_url_override.get_secret_value()
        password = self.supabase_db_password.get_secret_value()
        return (
            f"postgresql://postgres:{password}"
            f"@db.{self.supabase_project_ref}.supabase.co:5432/postgres"
        )

    def env_completeness(self) -> dict[str, bool]:
        """Lightweight presence check used by the /health command."""
        return {
            "TELEGRAM_BOT_TOKEN": bool(self.telegram_bot_token.get_secret_value()),
            "TELEGRAM_OWNER_ID": self.telegram_owner_id > 0,
            "SUPABASE_URL": bool(self.supabase_url),
            "SUPABASE_DB_PASSWORD": bool(self.supabase_db_password.get_secret_value()),
            "SUPABASE_KEY": self.supabase_key.get_secret_value() not in ("", "unset"),
            "ALPACA_API_KEY": bool(self.alpaca_api_key.get_secret_value()),
            "ALPACA_SECRET_KEY": bool(self.alpaca_secret_key.get_secret_value()),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached, validated settings object."""
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Clear the settings cache. Intended for tests only."""
    get_settings.cache_clear()
