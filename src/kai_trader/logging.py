"""structlog setup.

Production emits JSON so logs stay greppable in aggregators. Local dev uses a
colourised console renderer so the stream is readable at a glance. Call
``configure_logging()`` once at process start.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import Processor

from kai_trader.config import Settings

_configured = False


def configure_logging(settings: Settings) -> None:
    """Configure the root logger and structlog.

    Safe to call more than once: subsequent calls are ignored so tests and
    repeated imports do not stack handlers.
    """
    global _configured
    if _configured:
        return

    level = getattr(logging, settings.log_level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor
    if settings.env == "dev":
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, optionally bound with initial context."""
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial_values:
        logger = logger.bind(**initial_values)
    return logger  # type: ignore[no-any-return]


def reset_logging_for_tests() -> None:
    """Reset the configured flag so tests can reconfigure at will."""
    global _configured
    _configured = False
