"""Curated candidate pool the weekly universe review screens.

A static, code-reviewed list rather than a live market scan: candidate
DISCOVERY is the easiest place for adverse selection to creep in (a
premium-seeking scan auto-selects for danger), so the pool changes via
pull request while the weekly run only decides which pool members are
worth proposing right now. Every name was chosen for weekly options
with real volume and a share price whose cash-secured strike can fit a
small account's per-name cap as equity grows. The screen re-checks all
of that against live data anyway; this list is the funnel's mouth, not
a promise.
"""

from __future__ import annotations

from typing import Final

# Grouped for reviewability; the screen treats this as one flat set.
CANDIDATE_POOL: Final[tuple[str, ...]] = (
    # Defensives and income names with liquid weeklies.
    "T", "VZ", "KO", "PFE", "KVUE", "MO", "KMI", "WBA", "KHC", "CSCO",
    "INTC", "GM", "F", "BAC", "WFC", "C",
    # Quality cyclicals and growth with moderate IV.
    "MU", "PLTR", "HOOD", "SOFI", "GOLD", "CCL", "DAL", "UBER",
    "PYPL", "SNAP", "RIVN", "LCID", "NIO",
    # High-IV cohort (crypto-adjacent and speculative; the underwriter
    # is expected to reject these unless conditions genuinely warrant).
    "MARA", "RIOT", "CLSK", "MSTR", "COIN",
    # Non-correlated ETFs (no earnings risk, steady premium).
    "GDX", "SLV", "XLE", "XLF", "EEM", "TLT", "EWZ",
)
