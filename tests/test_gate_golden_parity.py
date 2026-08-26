"""Golden parity: the Phase R1 gate extraction changed no trading decision.

``tests/golden_gate_parity.json`` was captured by running the scenario
suite in ``tests/golden_gate_scenarios.py`` against the PRE-refactor
``build_intents_with_diagnostics`` (cap math inline in candidates.py).
This test re-runs the same scenarios against the current screen-then-
gate composition and asserts the serialised output is identical:
selected contracts, granted quantities, every rejection counter, every
diagnostics field, and the rendered warning lines.

If a deliberate strategy change ever invalidates the fixture, re-capture
it consciously (see the scenarios module docstring); never edit the JSON
by hand to make a red test green.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.golden_gate_scenarios import run_all

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_gate_parity.json"


async def test_gate_extraction_matches_pre_refactor_output() -> None:
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = await run_all()
    assert set(actual) == set(expected)
    for name in sorted(expected):
        assert actual[name]["intents"] == expected[name]["intents"], name
        assert actual[name]["diagnostics"] == expected[name]["diagnostics"], name
        assert actual[name]["warnings"] == expected[name]["warnings"], name
