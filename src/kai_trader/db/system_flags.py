"""Read and write the three system flags that gate trading behaviour.

The ``system_flags`` table holds three known keys: ``trading_enabled``,
``new_entries_enabled``, and ``kill_switch``. They were seeded as ``false``
in migration 001 and stay the source of truth for "is the trading engine
allowed to act right now". Strategy code (later phases) must consult these
before placing any order. Phase 2.5 only adds the read/write surface; no
caller in this phase actually trades.
"""

from __future__ import annotations

from kai_trader.db.client import get_pool
from kai_trader.logging import get_logger

KNOWN_FLAGS: tuple[str, ...] = (
    "trading_enabled",
    "new_entries_enabled",
    "kill_switch",
)

_log = get_logger(__name__)


def _parse(raw: str) -> bool:
    """Coerce a stored flag value to bool. Anything not 'true' is False."""
    return raw.strip().lower() == "true"


async def get_all_flags() -> dict[str, bool]:
    """Return every known flag's current value.

    Missing rows are reported as ``False`` so callers always see the safe
    default even if the seed insert was somehow skipped.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("select key, value from system_flags")
    found = {row["key"]: _parse(row["value"]) for row in rows}
    return {key: found.get(key, False) for key in KNOWN_FLAGS}


async def set_flag(key: str, value: bool, *, actor: int) -> bool:
    """Set ``key`` to ``value`` and return the prior value.

    ``actor`` is recorded in ``updated_by`` so the row carries who flipped
    it. Raises ``ValueError`` for unknown keys to avoid silently inserting
    junk into the flags table.
    """
    if key not in KNOWN_FLAGS:
        raise ValueError(f"Unknown flag: {key!r}. Known: {', '.join(KNOWN_FLAGS)}")

    new_raw = "true" if value else "false"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            prior = await conn.fetchval("select value from system_flags where key = $1", key)
            await conn.execute(
                """
                insert into system_flags (key, value, updated_at, updated_by)
                values ($1, $2, now(), $3)
                on conflict (key) do update
                  set value = excluded.value,
                      updated_at = excluded.updated_at,
                      updated_by = excluded.updated_by
                """,
                key,
                new_raw,
                str(actor),
            )

    prior_bool = _parse(prior) if prior is not None else False
    _log.info(
        "system_flags.set",
        key=key,
        prior=prior_bool,
        value=value,
        actor=actor,
    )
    return prior_bool
