"""Boot-time dependency probe.

When ``lxml`` was missing on Render the ETF earnings filter silently
fell over and the strategy worker kept running with a broken
fail-closed branch for days. This module exists to make that exact
failure mode loud at boot rather than at the first time the broken
code path is hit.

The probe imports every transitive dependency the bot relies on at
runtime. If any import fails, ``assert_dependencies_loadable`` raises
a single combined ``DependencyProbeError`` listing every missing
package. Wired into ``bot.main._startup`` alongside the schema check
so a deploy with a missing wheel refuses to start polling rather than
booting silently broken.

The probe list is curated, not exhaustive. We probe the deps whose
runtime presence has historically been load-bearing (lxml, yfinance,
asyncpg) plus the obvious top-level deps. Adding a new entry here is
cheap; the cost of missing one is the original silent-fail incident.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from kai_trader.logging import get_logger

_log = get_logger(__name__)


# Modules to probe. Each entry is (module_name, why_it_matters). The
# importance string surfaces in the failure message so the operator
# knows which code path is broken before they grep.
_PROBES: tuple[tuple[str, str], ...] = (
    ("alpaca", "Alpaca trading client (orders, positions, account)"),
    ("alpaca.trading.client", "TradingClient build path"),
    ("alpaca.trading.stream", "TradingStream WebSocket worker"),
    ("alpaca.data.historical.option", "Option chain fetches"),
    ("alpaca.data.historical.stock", "Stock daily bars + quotes"),
    ("anthropic", "Conversational chat handler"),
    ("asyncpg", "Postgres pool"),
    ("lxml", "ETF earnings filter (yfinance HTML parse path)"),
    ("yfinance", "VIX snapshot + earnings calendar"),
    ("pydantic", "Settings"),
    ("pydantic_settings", "Settings env loader"),
    ("structlog", "Logging"),
    ("telegram", "Telegram bot"),
    ("telegram.ext", "Telegram bot handlers"),
    ("zoneinfo", "Timezone handling (depends on tzdata on minimal images)"),
    ("dotenv", "Local .env loader"),
)


class DependencyProbeError(RuntimeError):
    """Raised at startup when one or more required imports fail."""


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing a single module."""

    module: str
    purpose: str
    ok: bool
    error: str | None = None


def probe_dependencies() -> list[ProbeResult]:
    """Try to import every module in ``_PROBES``. Returns one row per probe.

    Never raises. The companion ``assert_dependencies_loadable`` does the
    raising, so callers that just want a status report (a future
    ``/health`` extension, say) can iterate this list without try/except.
    """
    results: list[ProbeResult] = []
    for module_name, purpose in _PROBES:
        try:
            importlib.import_module(module_name)
            results.append(
                ProbeResult(module=module_name, purpose=purpose, ok=True)
            )
        except Exception as exc:
            results.append(
                ProbeResult(
                    module=module_name,
                    purpose=purpose,
                    ok=False,
                    error=str(exc),
                )
            )
    return results


def assert_dependencies_loadable() -> None:
    """Block boot when any required dependency fails to import.

    Raises :class:`DependencyProbeError` with a single combined message
    listing every missing module and the reason. Logs a structured
    ``dependency_probe.passed`` event on the happy path so the operator
    can grep the deploy logs for a known-good marker.
    """
    results = probe_dependencies()
    failures = [r for r in results if not r.ok]
    if failures:
        lines = [
            f"  - {r.module} ({r.purpose}): {r.error}"
            for r in failures
        ]
        message = (
            f"Dependency probe failed for {len(failures)} module(s):\n"
            + "\n".join(lines)
            + "\nFix the missing wheel(s) and redeploy. "
            "This check exists because lxml went missing once and the "
            "ETF earnings filter silently fell over for days."
        )
        _log.error(
            "dependency_probe.failed",
            failures=[r.module for r in failures],
            count=len(failures),
        )
        raise DependencyProbeError(message)
    _log.info(
        "dependency_probe.passed",
        probed=len(results),
    )


class AlpacaKeyConfigError(RuntimeError):
    """Raised at startup when the Alpaca key resolution does not produce credentials."""


def assert_alpaca_keys_resolvable() -> None:
    """Block boot when the configured Alpaca keys cannot be resolved.

    In paper mode the resolution falls back to ``ALPACA_API_KEY`` /
    ``ALPACA_SECRET_KEY`` (which Pydantic already requires). In live
    mode the resolver requires ``ALPACA_API_KEY_LIVE`` /
    ``ALPACA_SECRET_KEY_LIVE`` and refuses to fall back. Without this
    boot check, that refusal surfaces lazily on the first broker call,
    which on a fresh deploy is several minutes after the bot has
    started polling Telegram. The operator only finds out the key is
    missing when the strategy worker logs an error - too late if the
    market is already open.

    This probe runs the resolution once at boot. A misconfiguration
    refuses to start polling, the same posture as a missing wheel.
    """
    # Imported here to keep the dependency probe importable in test
    # environments that do not set the full settings surface.
    from kai_trader.config import get_settings

    settings = get_settings()
    try:
        api_key = settings.effective_alpaca_api_key
        secret = settings.effective_alpaca_secret_key
    except ValueError as exc:
        _log.error(
            "alpaca_key_probe.failed",
            paper=settings.alpaca_paper,
            error=str(exc),
        )
        raise AlpacaKeyConfigError(
            f"Alpaca key resolution failed: {exc}"
        ) from exc
    # Pydantic already enforces the value is non-empty; the resolver
    # also raises when the live override is missing. A bare-string
    # belt-and-braces check below catches the unlikely case of an
    # explicit empty string slipped through env loading.
    if not api_key or not secret:
        _log.error(
            "alpaca_key_probe.empty",
            paper=settings.alpaca_paper,
            api_key_blank=not api_key,
            secret_blank=not secret,
        )
        raise AlpacaKeyConfigError(
            "Alpaca key resolution returned an empty string. Check that "
            "the relevant *_LIVE or paper env vars are set non-empty."
        )
    _log.info(
        "alpaca_key_probe.passed",
        paper=settings.alpaca_paper,
    )


def log_eodhd_key_status() -> None:
    """Log whether EODHD_API_KEY made it into the live process at boot.

    Non-fatal on purpose: the earnings module has yfinance as a fallback,
    so a missing EODHD key degrades the strategy rather than killing
    boot. The log line exists because diagnosing "EODHD not being read"
    from tick output alone is painful: this surfaces the truth in deploy
    logs the moment the new container starts.
    """
    from kai_trader.config import get_settings

    settings = get_settings()
    if settings.eodhd_api_key is None:
        _log.warning(
            "eodhd_key_probe.missing",
            note=(
                "EODHD_API_KEY not set; earnings filter will run on "
                "yfinance only. Expect 'unknown, fail-closed' skips "
                "across the universe."
            ),
        )
        return
    raw = settings.eodhd_api_key.get_secret_value()
    _log.info(
        "eodhd_key_probe.present",
        length=len(raw),
        prefix=raw[:6] if len(raw) >= 6 else raw,
    )
