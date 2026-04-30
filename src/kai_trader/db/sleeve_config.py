"""Read and update the per-sleeve trading configuration.

Each row holds the parameters the strategy uses for one sleeve: target
allocation, target deltas (one for risk_on, one for neutral), DTE band,
profit-take, roll trigger, and the symbol whitelist. Operator can edit
fields at runtime; the strategy worker re-reads on each tick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kai_trader.db.client import get_pool
from kai_trader.logging import get_logger

KNOWN_SLEEVES: tuple[str, ...] = ("index_core", "stable_largecap", "opportunistic")

# Columns the operator can update via update_sleeve. Excludes primary key
# and timestamps which should not be touched by hand.
UPDATABLE_COLUMNS: tuple[str, ...] = (
    "target_pct",
    "target_delta_put_risk_on",
    "target_delta_put_neutral",
    "target_delta_call",
    "target_dte_min",
    "target_dte_max",
    "profit_take_pct",
    "roll_trigger_delta",
    "symbol_whitelist",
    "enabled",
    "earnings_blackout_enabled",
    "max_new_entries_per_tick",
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class SleeveConfig:
    """Narrow view of one ``sleeve_config`` row."""

    sleeve: str
    target_pct: Decimal
    target_delta_put_risk_on: Decimal
    target_delta_put_neutral: Decimal
    target_delta_call: Decimal
    target_dte_min: int
    target_dte_max: int
    profit_take_pct: Decimal
    roll_trigger_delta: Decimal
    symbol_whitelist: list[str]
    enabled: bool
    updated_at: datetime
    updated_by: str | None
    earnings_blackout_enabled: bool = True
    max_new_entries_per_tick: int = 2


def _row_to_config(row: dict[str, Any]) -> SleeveConfig:
    raw_whitelist = row["symbol_whitelist"]
    if isinstance(raw_whitelist, str):
        whitelist = list(json.loads(raw_whitelist))
    else:
        whitelist = list(raw_whitelist)
    return SleeveConfig(
        sleeve=row["sleeve"],
        target_pct=row["target_pct"],
        target_delta_put_risk_on=row["target_delta_put_risk_on"],
        target_delta_put_neutral=row["target_delta_put_neutral"],
        target_delta_call=row["target_delta_call"],
        target_dte_min=row["target_dte_min"],
        target_dte_max=row["target_dte_max"],
        profit_take_pct=row["profit_take_pct"],
        roll_trigger_delta=row["roll_trigger_delta"],
        symbol_whitelist=whitelist,
        enabled=row["enabled"],
        earnings_blackout_enabled=row.get("earnings_blackout_enabled", True),
        max_new_entries_per_tick=row.get("max_new_entries_per_tick", 2),
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


async def get_all_sleeves() -> list[SleeveConfig]:
    """Return all three sleeves in canonical order (index, stable, opp)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("select * from sleeve_config")
    by_name = {row["sleeve"]: _row_to_config(dict(row)) for row in rows}
    return [by_name[name] for name in KNOWN_SLEEVES if name in by_name]


async def get_sleeve(name: str) -> SleeveConfig | None:
    """Return one sleeve, or ``None`` if the row does not exist."""
    if name not in KNOWN_SLEEVES:
        raise ValueError(f"Unknown sleeve {name!r}. Known: {', '.join(KNOWN_SLEEVES)}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("select * from sleeve_config where sleeve = $1", name)
    if row is None:
        return None
    return _row_to_config(dict(row))


async def update_sleeve(name: str, *, actor: int, **fields: Any) -> SleeveConfig:
    """Update one or more fields of a sleeve and return the new state.

    Rejects unknown sleeve names and unknown column names. ``symbol_whitelist``
    is JSON-encoded automatically when passed as a list.
    """
    if name not in KNOWN_SLEEVES:
        raise ValueError(f"Unknown sleeve {name!r}. Known: {', '.join(KNOWN_SLEEVES)}")
    if not fields:
        raise ValueError("No fields supplied to update_sleeve.")
    bad = [k for k in fields if k not in UPDATABLE_COLUMNS]
    if bad:
        raise ValueError(
            f"Cannot update column(s) {bad}. Allowed: {', '.join(UPDATABLE_COLUMNS)}"
        )

    set_clauses: list[str] = []
    params: list[Any] = [name, str(actor)]
    for i, (col, value) in enumerate(fields.items(), start=3):
        set_clauses.append(f"{col} = ${i}")
        if col == "symbol_whitelist" and isinstance(value, list):
            params.append(json.dumps(value))
        else:
            params.append(value)
    set_sql = ", ".join(set_clauses)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"""
                update sleeve_config
                   set {set_sql},
                       updated_at = now(),
                       updated_by = $2
                 where sleeve = $1
                """,
                *params,
            )
            row = await conn.fetchrow("select * from sleeve_config where sleeve = $1", name)
    if row is None:
        raise RuntimeError(f"sleeve_config row for {name!r} disappeared mid-update.")
    config = _row_to_config(dict(row))
    _log.info(
        "sleeve_config.updated",
        sleeve=name,
        actor=actor,
        fields=list(fields.keys()),
    )
    return config
