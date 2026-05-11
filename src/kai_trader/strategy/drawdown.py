"""Drawdown circuit breaker.

Reads recent ``account_snapshots`` rows, computes the high-water mark over
``lookback_days``, and decides whether the drawdown from that high exceeds
the configured threshold. The strategy worker calls ``check_and_trip``
each tick so a sudden equity drop auto-engages the kill switch and fires
a critical-priority notification.

Threshold and lookback come from PHASE3.md: 7% drop from the prior week's
high. The hard 10% drawdown ceiling sits one tick above the breaker so an
operator has time to investigate before manual stop-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from kai_trader.bot.formatting import bold
from kai_trader.db.account_snapshots import StoredSnapshot, recent_snapshots
from kai_trader.db.system_flags import set_flag
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue

DRAWDOWN_THRESHOLD_PCT = Decimal("7")
LOOKBACK_DAYS = 7
WORKER_ACTOR_ID = -1  # sentinel for "system worker", not a real Telegram user

_log = get_logger(__name__)


@dataclass(frozen=True)
class DrawdownCheck:
    high_water_mark: Decimal
    current_equity: Decimal
    drawdown_pct: Decimal
    breached: bool


def compute_drawdown(
    snapshots: list[StoredSnapshot],
    current_equity: Decimal,
) -> DrawdownCheck:
    """Compute the drawdown vs the highest equity in ``snapshots`` plus current.

    ``current_equity`` is treated as the latest data point. If snapshots are
    empty the high water mark is just the current equity and drawdown is zero.
    """
    candidates = [s.equity for s in snapshots]
    candidates.append(current_equity)
    high = max(candidates)
    if high <= 0:
        return DrawdownCheck(
            high_water_mark=high,
            current_equity=current_equity,
            drawdown_pct=Decimal("0"),
            breached=False,
        )
    drawdown_pct = (high - current_equity) / high * Decimal("100")
    return DrawdownCheck(
        high_water_mark=high,
        current_equity=current_equity,
        drawdown_pct=drawdown_pct,
        breached=drawdown_pct >= DRAWDOWN_THRESHOLD_PCT,
    )


async def check_and_trip(
    *,
    current_equity: Decimal,
    kill_switch_already_on: bool,
    current_account_number: str | None = None,
) -> DrawdownCheck:
    """Read recent snapshots, evaluate drawdown, trip the kill switch if needed.

    Returns the check result so the worker can include the numbers in its
    tick summary. When the breach is fresh (kill switch was off), this also
    fires a critical-priority notification.

    ``current_account_number`` scopes the snapshot lookup to the Alpaca
    account currently in use. Without it, swapping Alpaca accounts (e.g.
    replacing a 100k paper account with a fresh 30k one) leaves the old
    account's high-water mark in the table and trips a phantom drawdown
    on the next tick. When the worker passes the live account number,
    legacy or other-account rows are filtered out at the SQL layer.
    """
    snapshots = await recent_snapshots(
        limit=200,
        account_number=current_account_number,
    )
    # Filter to the lookback window. The cutoff is anchored to NOW
    # rather than to the most-recent snapshot's timestamp: if the bot
    # was offline for a few days, anchoring to the latest snapshot
    # would silently extend the lookback by the offline duration and
    # the breaker would compare current equity against a high-water
    # mark from outside the intended 7-day window.
    if snapshots:
        cutoff = datetime.now(UTC).timestamp() - LOOKBACK_DAYS * 86400
        snapshots = [s for s in snapshots if s.captured_at.timestamp() >= cutoff]

    check = compute_drawdown(snapshots, current_equity)
    if not check.breached:
        return check
    if kill_switch_already_on:
        _log.info(
            "drawdown.breached_already_killed",
            drawdown_pct=str(check.drawdown_pct),
            high_water_mark=str(check.high_water_mark),
            current_equity=str(check.current_equity),
        )
        return check

    await set_flag("kill_switch", True, actor=WORKER_ACTOR_ID)
    body = (
        f"{bold('DRAWDOWN CIRCUIT BREAKER')}\n"
        f"{check.drawdown_pct:.2f}% drop from {check.high_water_mark} "
        f"to {check.current_equity}.\n"
        "Kill switch engaged automatically. Investigate before clearing "
        "with /flag kill_switch off."
    )
    await enqueue(body, "critical", channel="telegram")
    _log.error(
        "drawdown.tripped",
        drawdown_pct=str(check.drawdown_pct),
        high_water_mark=str(check.high_water_mark),
        current_equity=str(check.current_equity),
    )
    return check
