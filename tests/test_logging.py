"""Tests for structlog setup."""

from __future__ import annotations

import pytest

from kai_trader import logging as logging_module
from kai_trader.config import get_settings


def test_configure_is_idempotent() -> None:
    logging_module.reset_logging_for_tests()
    logging_module.configure_logging(get_settings())
    logging_module.configure_logging(get_settings())  # second call must not raise
    log = logging_module.get_logger("test")
    # bind returns a logger; just confirm it works
    bound = log.bind(foo="bar")
    assert bound is not None


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_configure_for_each_env(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    from kai_trader import config as config_module

    monkeypatch.setenv("ENV", env)
    config_module.reset_settings_cache()
    logging_module.reset_logging_for_tests()
    logging_module.configure_logging(config_module.get_settings())
    log = logging_module.get_logger()
    assert log is not None


def test_get_logger_with_initial_values() -> None:
    logging_module.reset_logging_for_tests()
    logging_module.configure_logging(get_settings())
    log = logging_module.get_logger("named", request_id="abc")
    assert log is not None
