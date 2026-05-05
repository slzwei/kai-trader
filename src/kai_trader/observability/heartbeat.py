"""Out-of-band liveness ping for the strategy worker.

The bot has plenty of *in-band* signals (Telegram tick summaries, DB
writes, Render's container health). All of them break in the same ways
the bot itself breaks: a Telegram outage, a Render region issue, a
silent worker hang. This module lets the bot phone home to a third
party that has none of those failure modes.

The intended target is a service like healthchecks.io: configure a
check with an expected interval (e.g. 10 minutes) and a grace period
(e.g. 30 minutes); the service emails the operator when pings stop. We
ping after every successful strategy tick, so a hang anywhere in the
tick body translates to a missed ping and an out-of-band email within
the grace window. No ping is no signal; we never ping on tick failure.

The URL itself is a secret because it's the only authentication on the
healthchecks.io endpoint — anyone with the URL can mark the bot alive.

This module is intentionally tiny: one async helper, no worker, no
state. It's called inline from the strategy worker's outer loop after
``tick()`` returns successfully.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from kai_trader.config import get_settings
from kai_trader.logging import get_logger

_log = get_logger(__name__)

PING_TIMEOUT_SECONDS = 10


async def ping_heartbeat() -> None:
    """Fire-and-forget GET to the configured heartbeat URL.

    No-op when ``HEARTBEAT_URL`` is unset. Network failures are logged
    as warnings and swallowed so a heartbeat outage never breaks the
    strategy. Schedule this *after* a successful tick body completes so
    a hung tick translates to a missed ping.
    """
    settings = get_settings()
    secret = settings.heartbeat_url
    url = secret.get_secret_value() if secret is not None else ""
    if not url:
        return
    try:
        await asyncio.to_thread(_ping_sync, url)
    except Exception as exc:
        _log.warning("heartbeat.ping_failed", error=str(exc))


def _ping_sync(url: str) -> None:
    """Synchronous GET. Caller wraps in asyncio.to_thread."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=PING_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status >= 400:
                raise RuntimeError(f"heartbeat endpoint returned {status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"heartbeat URL unreachable: {exc.reason}") from exc
