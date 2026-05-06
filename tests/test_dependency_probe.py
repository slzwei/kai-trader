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
