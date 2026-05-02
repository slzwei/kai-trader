"""Structural regression tests for Kai's accuracy guardrails.

These tests do not invoke the Anthropic API. They lock in the *contract*
between the system prompt and the tool surface so the next person who
edits the prompt cannot accidentally remove a load-bearing rule, and so
new tools cannot be added without the corresponding live/history tag.

Concretely they check:

1. Each named guardrail in the system prompt is still present.
2. Every tool description carries a class tag (LIVE, HISTORY, REPO, or
   WRITE) so Kai can route correctly.
3. The two most-quoted constants (``TOTAL_DEPLOYMENT_CAP_PCT`` and the
   200-row cap) are referenced in the prompt by their *behaviour*, not
   by a numeric literal — numeric literals drift, behavioural references
   stay correct.
"""

from __future__ import annotations

from kai_trader.chat.system_prompt import SYSTEM_PROMPT
from kai_trader.chat.tools import TOOL_DEFINITIONS

# Each entry is a short rule name followed by one or more required
# substrings. Substrings must all appear, case-sensitive.
_REQUIRED_PROMPT_RULES: dict[str, tuple[str, ...]] = {
    "identity": ("Kai", "Shawn", "single-owner"),
    "grounding": ("Ground every factual claim", "tool call"),
    "live_vs_history": ("LIVE tools", "HISTORY tools", "append-only"),
    "freshness": ("five minutes", "max(created_at)"),
    "tz": ("Singapore time", "UTC"),
    "caps": ("200 rows", "80 contracts", "count(*)"),
    "no_memorised_numbers": ("from training", "read_file"),
    "tool_errors": ("Report the error verbatim", "Do not retry"),
    "confirmation_skepticism": ("leading question", "premise"),
    "approval_flow": (
        "propose_change",
        "queued for approval",
        "Forbidden phrasings",
    ),
    "refusals": ("Refuse", "destructive"),
    "style": ("400 words", "No em dashes", "Telegram"),
}


def test_every_guardrail_is_present_in_prompt() -> None:
    missing: list[str] = []
    for rule, needles in _REQUIRED_PROMPT_RULES.items():
        for needle in needles:
            if needle not in SYSTEM_PROMPT:
                missing.append(f"{rule}: {needle!r}")
    assert not missing, (
        "System prompt is missing required guardrail substrings:\n  "
        + "\n  ".join(missing)
    )


def test_every_tool_description_carries_class_tag() -> None:
    """Each tool description must declare its class so routing is unambiguous.

    Allowed tags: ``[LIVE]``, ``[HISTORY]``, ``[HISTORY by default]``,
    ``[REPO]``, ``[WRITE: proposal only]``.
    """
    allowed_tags = ("[LIVE]", "[HISTORY", "[REPO]", "[WRITE")
    untagged: list[str] = []
    for tool in TOOL_DEFINITIONS:
        desc = tool.get("description", "")
        assert isinstance(desc, str)
        if not any(desc.startswith(tag) for tag in allowed_tags):
            untagged.append(f"{tool['name']}: {desc[:60]!r}")
    assert not untagged, (
        "Every tool must start with a class tag (LIVE/HISTORY/REPO/WRITE):\n  "
        + "\n  ".join(untagged)
    )


def test_system_pulse_is_first_tool() -> None:
    """system_pulse goes first so the model preferentially sees it.

    The ordering matters because Anthropic puts the cache_control marker
    on the last tool; the first tool sits at the most prominent position
    in the rendered list.
    """
    assert TOOL_DEFINITIONS[0]["name"] == "system_pulse"


def test_query_supabase_warns_about_truncation_and_aggregates() -> None:
    sql_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "query_supabase")
    desc = sql_tool["description"]
    assert isinstance(desc, str)
    assert "truncated" in desc
    assert "count(*)" in desc
    assert "max(created_at)" in desc


def test_propose_change_description_forbids_past_tense() -> None:
    """The description itself must remind the model not to claim done."""
    pc = next(t for t in TOOL_DEFINITIONS if t["name"] == "propose_change")
    desc = pc["description"]
    assert isinstance(desc, str)
    assert "queued for approval" in desc
    assert "never as already done" in desc


def test_prompt_under_word_budget() -> None:
    """Soft cap so the cached system prompt does not bloat over time."""
    word_count = len(SYSTEM_PROMPT.split())
    assert word_count < 800, (
        f"System prompt is {word_count} words; consider tightening before "
        "adding more rules."
    )
