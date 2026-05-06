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
    alpaca_api_key_paper: SecretStr | None = Field(
        default=None,
        alias="ALPACA_API_KEY_PAPER",
        description=(
            "Optional explicit paper-key override. When set, the broker uses "
            "this in paper mode instead of ALPACA_API_KEY. Lets the operator "
            "keep both paper and live key pairs configured simultaneously "
            "and toggle between them with the ALPACA_PAPER flag."
        ),
    )
    alpaca_secret_key_paper: SecretStr | None = Field(
        default=None,
        alias="ALPACA_SECRET_KEY_PAPER",
        description="Paired with ALPACA_API_KEY_PAPER. Optional.",
    )
    alpaca_api_key_live: SecretStr | None = Field(
        default=None,
        alias="ALPACA_API_KEY_LIVE",
        description=(
            "Live-trading API key. Required when ALPACA_PAPER=false. Paper "
            "fallback is intentionally NOT used in live mode; sending paper "
            "keys to the live endpoint just 401s and the failure is louder "
            "as a configuration error than an auth error."
        ),
    )
    alpaca_secret_key_live: SecretStr | None = Field(
        default=None,
        alias="ALPACA_SECRET_KEY_LIVE",
        description="Paired with ALPACA_API_KEY_LIVE. Required in live mode.",
    )
    alpaca_paper: bool = Field(
        default=True,
        alias="ALPACA_PAPER",
        description=(
            "If true, route through Alpaca paper. Live trading requires this "
            "flag plus the trading-enabled system flag flipped on."
        ),
    )

    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="ANTHROPIC_API_KEY",
        description="Required for the conversational chat handler. When unset, /help still works but free-form messages are ignored.",
    )
    chat_model: str = Field(
        default="claude-sonnet-4-6",
        alias="CHAT_MODEL",
        description="Claude model id used by the chat handler.",
    )
    chat_max_tokens: int = Field(default=1500, alias="CHAT_MAX_TOKENS")
    chat_history_keep: int = Field(default=20, alias="CHAT_HISTORY_KEEP")
    chat_history_compact_threshold: int = Field(
        default=40,
        alias="CHAT_HISTORY_COMPACT_THRESHOLD",
    )
    database_url_ro: SecretStr | None = Field(
        default=None,
        alias="DATABASE_URL_RO",
        description=(
            "Read-only Postgres DSN used by the chat tool layer. Should "
            "authenticate as the kai_chat_ro role. When unset, query_supabase "
            "tool calls fail closed."
        ),
    )
    heartbeat_url: SecretStr | None = Field(
        default=None,
        alias="HEARTBEAT_URL",
        description=(
            "Optional out-of-band liveness URL pinged after every successful "
            "strategy tick. Use a service like healthchecks.io that emails "
            "when pings stop. When unset, the heartbeat is a no-op."
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
    def effective_alpaca_api_key(self) -> str:
        """Resolve which Alpaca API key the broker should use right now.

        Paper mode prefers ``ALPACA_API_KEY_PAPER`` and falls back to the
        legacy ``ALPACA_API_KEY``. Live mode strictly requires
        ``ALPACA_API_KEY_LIVE`` and refuses to fall back to the paper key,
        because sending paper credentials at the live endpoint produces a
        confusing 401 instead of a clear configuration error.
        """
        if self.alpaca_paper:
            override = self.alpaca_api_key_paper
            if override is not None and override.get_secret_value():
                return override.get_secret_value()
            return self.alpaca_api_key.get_secret_value()
        live = self.alpaca_api_key_live
        if live is None or not live.get_secret_value():
            raise ValueError(
                "ALPACA_PAPER=false but ALPACA_API_KEY_LIVE is unset. "
                "Set the live API key explicitly; the paper key is not "
                "used as a fallback in live mode."
            )
        return live.get_secret_value()

    @property
    def effective_alpaca_secret_key(self) -> str:
        """Resolve the secret key paired with ``effective_alpaca_api_key``."""
        if self.alpaca_paper:
            override = self.alpaca_secret_key_paper
            if override is not None and override.get_secret_value():
                return override.get_secret_value()
            return self.alpaca_secret_key.get_secret_value()
        live = self.alpaca_secret_key_live
        if live is None or not live.get_secret_value():
            raise ValueError(
                "ALPACA_PAPER=false but ALPACA_SECRET_KEY_LIVE is unset. "
                "Set the live secret key explicitly; the paper secret is "
                "not used as a fallback in live mode."
            )
        return live.get_secret_value()

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
        ro = self.database_url_ro
        return {
            "TELEGRAM_BOT_TOKEN": bool(self.telegram_bot_token.get_secret_value()),
            "TELEGRAM_OWNER_ID": self.telegram_owner_id > 0,
            "SUPABASE_URL": bool(self.supabase_url),
            "SUPABASE_DB_PASSWORD": bool(self.supabase_db_password.get_secret_value()),
            "SUPABASE_KEY": self.supabase_key.get_secret_value() not in ("", "unset"),
            "ALPACA_API_KEY": bool(self.alpaca_api_key.get_secret_value()),
            "ALPACA_SECRET_KEY": bool(self.alpaca_secret_key.get_secret_value()),
            "ANTHROPIC_API_KEY": bool(self.anthropic_api_key.get_secret_value()),
            "DATABASE_URL_RO": bool(ro.get_secret_value()) if ro is not None else False,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached, validated settings object."""
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Clear the settings cache. Intended for tests only."""
    get_settings.cache_clear()
