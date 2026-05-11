"""Unit tests for the boot-time dependency probe."""

from __future__ import annotations

import pytest

from kai_trader.observability import dependency_probe


def test_probe_dependencies_returns_one_row_per_probe() -> None:
    results = dependency_probe.probe_dependencies()
    assert len(results) == len(dependency_probe._PROBES)
    names = {r.module for r in results}
    assert names == {name for name, _ in dependency_probe._PROBES}


def test_probe_dependencies_passes_in_test_env() -> None:
    """All declared deps must import cleanly inside the test environment.

    If this fails locally the test env is missing a wheel pyproject claims
    to require, which is itself the bug class the probe is meant to
    catch. Fix the env, not the test.
    """
    results = dependency_probe.probe_dependencies()
    failed = [r for r in results if not r.ok]
    assert failed == [], f"Unexpected probe failures: {failed}"


def test_assert_passes_when_all_deps_load() -> None:
    # Should be a no-op in the test env.
    dependency_probe.assert_dependencies_loadable()


def test_assert_raises_with_combined_message_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing probe surfaces as DependencyProbeError listing every miss."""
    fake_results = [
        dependency_probe.ProbeResult(
            module="lxml",
            purpose="ETF earnings filter",
            ok=False,
            error="No module named 'lxml'",
        ),
        dependency_probe.ProbeResult(
            module="yfinance",
            purpose="VIX snapshot",
            ok=False,
            error="No module named 'yfinance'",
        ),
        dependency_probe.ProbeResult(
            module="alpaca",
            purpose="trading client",
            ok=True,
        ),
    ]
    monkeypatch.setattr(
        dependency_probe, "probe_dependencies", lambda: fake_results
    )

    with pytest.raises(dependency_probe.DependencyProbeError) as exc:
        dependency_probe.assert_dependencies_loadable()

    msg = str(exc.value)
    assert "2 module(s)" in msg
    assert "lxml" in msg
    assert "yfinance" in msg
    # Successful probes don't appear in the failure list.
    assert "alpaca" not in msg.split("Fix the missing")[0]


def test_assert_records_structured_failure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path emits a structured log event before raising."""
    captured: list[dict[str, object]] = []

    class _FakeLog:
        def error(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

        def info(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

    monkeypatch.setattr(dependency_probe, "_log", _FakeLog())
    monkeypatch.setattr(
        dependency_probe, "probe_dependencies",
        lambda: [
            dependency_probe.ProbeResult(
                module="lxml",
                purpose="x",
                ok=False,
                error="missing",
            ),
        ],
    )

    with pytest.raises(dependency_probe.DependencyProbeError):
        dependency_probe.assert_dependencies_loadable()

    error_events = [c for c in captured if c["event"] == "dependency_probe.failed"]
    assert len(error_events) == 1
    assert error_events[0]["count"] == 1
    assert error_events[0]["failures"] == ["lxml"]


def test_assert_records_passed_event_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class _FakeLog:
        def error(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

        def info(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

    monkeypatch.setattr(dependency_probe, "_log", _FakeLog())
    monkeypatch.setattr(
        dependency_probe, "probe_dependencies",
        lambda: [
            dependency_probe.ProbeResult(
                module="lxml", purpose="x", ok=True,
            ),
        ],
    )

    dependency_probe.assert_dependencies_loadable()

    pass_events = [c for c in captured if c["event"] == "dependency_probe.passed"]
    assert len(pass_events) == 1
    assert pass_events[0]["probed"] == 1


# ------------- Autonomy gap 1: Alpaca key boot probe -------------


def _make_settings(*, paper: bool, **overrides: object) -> object:
    """Build a settings stub that mimics the resolver's behaviour."""

    class _Stub:
        alpaca_paper = paper

        @property
        def effective_alpaca_api_key(self) -> str:
            value = overrides.get("api_key", "papk-real")
            if isinstance(value, BaseException):
                raise value
            return str(value)

        @property
        def effective_alpaca_secret_key(self) -> str:
            value = overrides.get("secret", "secret-real")
            if isinstance(value, BaseException):
                raise value
            return str(value)

    return _Stub()


def test_alpaca_key_probe_passes_in_paper_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kai_trader.config.get_settings",
        lambda: _make_settings(paper=True),
    )
    # Should not raise.
    dependency_probe.assert_alpaca_keys_resolvable()


def test_alpaca_key_probe_passes_with_live_keys_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kai_trader.config.get_settings",
        lambda: _make_settings(
            paper=False, api_key="live-key", secret="live-secret"
        ),
    )
    dependency_probe.assert_alpaca_keys_resolvable()


def test_alpaca_key_probe_raises_when_resolver_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live deploy without ALPACA_API_KEY_LIVE must not start polling."""
    monkeypatch.setattr(
        "kai_trader.config.get_settings",
        lambda: _make_settings(
            paper=False,
            api_key=ValueError("ALPACA_PAPER=false but ALPACA_API_KEY_LIVE is unset"),
        ),
    )
    with pytest.raises(
        dependency_probe.AlpacaKeyConfigError,
        match="ALPACA_API_KEY_LIVE is unset",
    ):
        dependency_probe.assert_alpaca_keys_resolvable()


def test_alpaca_key_probe_raises_on_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces guard: empty string slipped through env loading."""
    monkeypatch.setattr(
        "kai_trader.config.get_settings",
        lambda: _make_settings(paper=True, api_key=""),
    )
    with pytest.raises(
        dependency_probe.AlpacaKeyConfigError, match="empty string"
    ):
        dependency_probe.assert_alpaca_keys_resolvable()


def test_eodhd_key_probe_warns_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key set: must log warning event so deploy logs surface the gap."""
    captured: list[dict[str, object]] = []

    class _FakeLog:
        def error(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs, "level": "error"})

        def warning(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs, "level": "warning"})

        def info(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs, "level": "info"})

    class _Stub:
        eodhd_api_key = None

    monkeypatch.setattr("kai_trader.config.get_settings", lambda: _Stub())
    monkeypatch.setattr(dependency_probe, "_log", _FakeLog())

    dependency_probe.log_eodhd_key_status()

    events = [c for c in captured if c["event"] == "eodhd_key_probe.missing"]
    assert len(events) == 1
    assert events[0]["level"] == "warning"


def test_eodhd_key_probe_logs_present_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key set: log length + 6-char prefix so the operator can spot a typo."""
    captured: list[dict[str, object]] = []

    class _FakeLog:
        def error(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs, "level": "error"})

        def warning(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs, "level": "warning"})

        def info(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs, "level": "info"})

    class _Secret:
        def get_secret_value(self) -> str:
            return "682e16bc7c4ad7.88056657"

    class _Stub:
        eodhd_api_key = _Secret()

    monkeypatch.setattr("kai_trader.config.get_settings", lambda: _Stub())
    monkeypatch.setattr(dependency_probe, "_log", _FakeLog())

    dependency_probe.log_eodhd_key_status()

    events = [c for c in captured if c["event"] == "eodhd_key_probe.present"]
    assert len(events) == 1
    assert events[0]["level"] == "info"
    assert events[0]["length"] == len("682e16bc7c4ad7.88056657")
    assert events[0]["prefix"] == "682e16"
