"""Mirror the FMP fundamentals corpus to local disk, for every symbol it covers.

The four things this pulls, and how each one is actually sourced:

* HISTORICAL ANALYST ESTIMATES AND REVISIONS. Estimates come from
  ``analyst-estimates-bulk`` (one CSV per year per period, every covered
  symbol in it). Revisions do not exist in bulk form: the individual
  upgrade/downgrade/price-target actions live only behind
  ``grades?symbol=``, and the month-by-month consensus counts only behind
  ``grades-historical?symbol=``. Those two are crawled per symbol across the
  whole universe, which is the slow half of this script.
* FINANCIAL STATEMENTS, RATIOS AND METRICS. All bulk, keyed by year and
  period: income statement, balance sheet, cash flow, ratios, key metrics.
  Plus the TTM and score snapshots, which are current-state only.
* EARNINGS AND SURPRISES. ``earnings-surprises-bulk`` by year, plus the full
  ``earnings-calendar`` history swept in date windows.
* INDEX CONSTITUENTS AND DELISTINGS. Current and historical membership for
  the S&P 500, Nasdaq 100 and Dow, plus the delisted-company register and the
  ticker-change log, which is what makes the delisting record usable.

Rate limits, measured against the mirror on 2026-09-01 rather than assumed:

* The account-wide ceiling is ~400 requests/minute. At 6.0/s every request
  returned 200; at 10/s a third of them 429'd and effective throughput did
  not improve (~6.6/s either way). So the limiter defaults to 5.5/s, which
  sits under the cliff with headroom.
* Bulk endpoints carry a SECOND, much tighter limit of their own, and it is
  not the same counter. Two back-to-back bulk calls fail the second one with
  "Limit Reach. This is a bulk endpoint..." even when the global budget is
  untouched. Roughly 8 seconds of spacing clears it, so bulk work is
  serialised behind its own gate at ``--bulk-interval`` (default 10s) and
  leans on backoff for the rest.

Everything is resumable. Each completed unit of work appends a line to
``_manifest.jsonl``; a re-run skips whatever is already recorded there, so an
interrupted crawl picks up where it stopped rather than starting over. Delete
a manifest line (or pass ``--force``) to re-fetch that unit.

Bulk CSV is stored gzipped exactly as served, so nothing is lost to a parsing
opinion made today. Per-symbol JSON is stored as gzipped JSONL, sharded, one
line per symbol.

Usage:
    uv run python scripts/fmp_download.py --only reference,indices
    uv run python scripts/fmp_download.py --only statements,earnings,estimates
    uv run python scripts/fmp_download.py --only revisions   # the ~9 hour one
    uv run python scripts/fmp_download.py --status
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "fmp_data"

# Bulk statement history. FMP thins out fast before the mid-1990s; the crawler
# walks backwards and stops after STOP_AFTER_EMPTY consecutive dry years, so
# the floor is discovered rather than guessed.
YEAR_MAX = 2027
YEAR_MIN = 1985
STOP_AFTER_EMPTY = 3

PERIODS = ("FY", "Q1", "Q2", "Q3", "Q4")

# earnings-calendar truncates a wide window (a one-week 2026 window came back
# with exactly 4000 rows). Any window that lands at or above this gets split.
CALENDAR_CAP = 3900
CALENDAR_START = dt.date(1995, 1, 1)
CALENDAR_END = dt.date(2027, 12, 31)


class BudgetExhaustedError(RuntimeError):
    """This run hit its own --budget-mb ceiling. Self-imposed, not the server."""


class BandwidthExhaustedError(RuntimeError):
    """The rolling 30-day byte quota is gone. No amount of waiting helps."""


class RateLimiter:
    """Token bucket shared by every request, bulk and per-symbol alike."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait <= 0:
                self._next = now + self._interval
                wait = 0.0
            else:
                self._next += self._interval
        if wait > 0:
            time.sleep(wait)

    def penalise(self, seconds: float) -> None:
        """Push every waiting caller back after a 429."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class BulkGate:
    """One independent gate PER bulk endpoint.

    Measured 2026-09-02: while ``analyst-estimates-bulk`` was being throttled
    to one call per 65s, ``dcf-bulk``, ``rating-bulk`` and
    ``income-statement-bulk`` all answered in 2 to 8 seconds. The bulk limiter
    is therefore per-endpoint, not global. Gating them together serialised work
    that the server was happy to run in parallel and turned the statements
    phase into a 19 hour job instead of a 4 hour one.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._registry = threading.Lock()
        self._gates: dict[str, tuple[threading.Lock, list[float]]] = {}

    def _gate(self, key: str) -> tuple[threading.Lock, list[float]]:
        with self._registry:
            if key not in self._gates:
                self._gates[key] = (threading.Lock(), [0.0])
            return self._gates[key]

    def acquire(self, key: str) -> None:
        lock, last = self._gate(key)
        lock.acquire()
        wait = self._interval - (time.monotonic() - last[0])
        if wait > 0:
            time.sleep(wait)

    def release(self, key: str) -> None:
        _, last = self._gate(key)
        last[0] = time.monotonic()
        self._gate(key)[0].release()


@dataclass
class Client:
    """HTTP client with an on-disk cache of every byte the mirror ever sent.

    The quota here is bandwidth over a rolling 30-day window, which makes a
    discarded response a permanent loss: you cannot re-check something for
    free, and "does this endpoint work?" costs exactly as much as wanting the
    data. The first pass at this cost 581 MB of in-scope data that was fetched
    and binned, plus 52 MB paid for twice. So every 200 is written to
    ``_cache/`` keyed by the full URL, and every fetch looks there first.
    Re-runs, coverage probes and interrupted jobs are then free.
    """

    base: str
    key: str
    limiter: RateLimiter
    bulk_gate: BulkGate
    max_attempts: int = 8
    max_net_attempts: int = 25  # ~20 min of outage tolerance
    stats: dict[str, int] = field(default_factory=dict)
    wire_bytes: int = 0
    cache_dir: Path | None = None
    budget_bytes: int = 0  # 0 = unlimited

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.gz"
    _stats_lock: threading.Lock = field(default_factory=threading.Lock)

    def _bump(self, name: str) -> None:
        with self._stats_lock:
            self.stats[name] = self.stats.get(name, 0) + 1

    def fetch(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        bulk: bool = False,
        timeout: float | None = None,
    ) -> bytes:
        """Return the raw response body, retrying through 429s and 5xxs."""
        query = dict(params or {})
        query["apikey"] = self.key
        url = f"{self.base}/{endpoint}?{urllib.parse.urlencode(query)}"
        timeout = timeout if timeout is not None else (900.0 if bulk else 60.0)
        last: Exception | None = None

        if self.budget_bytes and self.wire_bytes >= self.budget_bytes:
            raise BudgetExhaustedError(
                f"run budget of {self.budget_bytes / 1e6:.0f} MB reached "
                f"({self.wire_bytes / 1e6:.1f} MB spent)"
            )

        cached = self._cache_path(url)
        if cached is not None and cached.exists():
            self._bump("cache_hit")
            with gzip.open(cached, "rb") as handle:
                return bytes(handle.read())

        attempt = 0
        net_attempt = 0
        while attempt < self.max_attempts and net_attempt < self.max_net_attempts:
            if bulk:
                self.bulk_gate.acquire(endpoint)
            try:
                self.limiter.acquire()
                # Ask for compressed transfer. The binding quota on this
                # mirror is BANDWIDTH over a rolling 30-day window, and these
                # CSVs are ~4x smaller gzipped, so this is worth roughly 4x
                # the effective monthly budget. urllib does not negotiate it
                # on its own, which is why the first runs burned the quota at
                # full uncompressed size.
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "kai-trader-fmp-mirror/1.0",
                        "Accept-Encoding": "gzip, deflate",
                    },
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                    encoding = (response.headers.get("Content-Encoding") or "").lower()
                self.wire_bytes += len(raw)
                if "gzip" in encoding:
                    body: bytes = gzip.decompress(raw)
                elif "deflate" in encoding:
                    body = zlib.decompress(raw, -zlib.MAX_WBITS)
                else:
                    body = raw
                self._bump("ok")
                if cached is not None:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cached.with_suffix(".tmp")
                    with gzip.open(tmp, "wb", compresslevel=6) as handle:
                        handle.write(body)
                    tmp.replace(cached)
                return body
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 429:
                    detail = exc.read()[:200]
                    if b"Bandwidth limit" in detail:
                        self._bump("bandwidth_exhausted")
                        raise BandwidthExhaustedError(detail.decode("utf8", "replace")) from exc
                    self._bump("http_429")
                    # Bulk backs off far harder: its private limiter is the
                    # one being tripped and it recovers on a seconds scale.
                    base = 10.0 if bulk else 3.0
                    delay = base * (2**attempt) + random.uniform(0, 2)
                    if not bulk:
                        self.limiter.penalise(min(delay, 30.0))
                    attempt += 1
                    time.sleep(min(delay, 120.0))
                    continue
                if 500 <= exc.code < 600:
                    self._bump(f"http_{exc.code}")
                    attempt += 1
                    time.sleep(min(4.0 * (2**attempt), 60.0))
                    continue
                self._bump(f"http_{exc.code}")
                raise RuntimeError(f"HTTP {exc.code} on {endpoint} {params}") from exc
            except Exception as exc:  # connection lost: wait it out
                last = exc
                self._bump("neterr")
                net_attempt += 1
                time.sleep(min(4.0 * (2 ** min(net_attempt, 4)), 60.0))
            finally:
                if bulk:
                    self.bulk_gate.release(endpoint)

        self._bump("exhausted")
        marker = "NETWORK: " if net_attempt >= self.max_net_attempts else ""
        raise RuntimeError(f"{marker}{endpoint} {params} failed after retries: {last}")

    def fetch_json(
        self, endpoint: str, params: dict[str, Any] | None = None, **kw: Any
    ) -> Any:
        body = self.fetch(endpoint, params, **kw)
        return json.loads(body)


class Manifest:
    """Append-only record of finished units, so a re-run can skip them."""

    def __init__(self, path: Path, force: bool) -> None:
        self.path = path
        self.force = force
        self._lock = threading.Lock()
        self._done: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._done[row["key"]] = row

    def has(self, key: str) -> bool:
        return not self.force and key in self._done

    def record(self, key: str, **fields: Any) -> None:
        row = {"key": key, "at": dt.datetime.now(dt.UTC).isoformat(), **fields}
        with self._lock:
            self._done[key] = row
            with self.path.open("a") as handle:
                handle.write(json.dumps(row) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        return list(self._done.values())


def write_gz(path: Path, payload: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with gzip.open(tmp, "wb", compresslevel=6) as handle:
        handle.write(payload)
    tmp.replace(path)
    return path.stat().st_size


def csv_rows(payload: bytes) -> int:
    """Data rows in a CSV body, header excluded."""
    if not payload.strip():
        return 0
    return max(payload.count(b"\n") - 1, 0)


def looks_like_error(payload: bytes) -> str | None:
    head = payload[:200].lstrip()
    if head.startswith(b"{") and b"Error Message" in head:
        try:
            return str(json.loads(payload.decode())["Error Message"])[:150]
        except Exception:
            return head[:150].decode("utf8", "replace")
    return None


# --------------------------------------------------------------------------
# Task groups
# --------------------------------------------------------------------------


@dataclass
class Context:
    client: Client
    manifest: Manifest
    out: Path
    log: Callable[[str], None]


def _simple_json(ctx: Context, subdir: str, endpoint: str, params: dict[str, Any] | None = None) -> None:
    key = f"{subdir}/{endpoint}"
    if ctx.manifest.has(key):
        ctx.log(f"  skip {key}")
        return
    try:
        payload = ctx.client.fetch(endpoint, params)
    except RuntimeError as exc:
        ctx.log(f"  FAIL {key}: {exc}")
        return
    err = looks_like_error(payload)
    if err:
        ctx.log(f"  FAIL {key}: {err}")
        return
    parsed = json.loads(payload)
    count = len(parsed) if isinstance(parsed, list) else 1
    path = ctx.out / subdir / f"{endpoint}.json.gz"
    size = write_gz(path, payload)
    ctx.manifest.record(key, endpoint=endpoint, rows=count, bytes=size, path=str(path.relative_to(ctx.out)))
    ctx.log(f"  {key}: {count:,} rows, {size / 1e6:.1f} MB")


def _paged_json(
    ctx: Context,
    subdir: str,
    endpoint: str,
    *,
    limit: int = 100,
    extra: dict[str, Any] | None = None,
    max_pages: int = 5000,
) -> None:
    """Walk ``page=0,1,2,...`` until a page comes back empty."""
    key = f"{subdir}/{endpoint}"
    if ctx.manifest.has(key):
        ctx.log(f"  skip {key}")
        return
    rows: list[Any] = []
    seen: set[str] = set()
    for page in range(max_pages):
        params = {"page": page, "limit": limit, **(extra or {})}
        try:
            payload = ctx.client.fetch(endpoint, params)
        except RuntimeError as exc:
            # The API caps pagination and answers HTTP 400 past the last page
            # (insider-trading/search stops at page 100). That is the end of
            # the feed, not a failure of the group.
            ctx.log(f"  {key}: pagination ended at page {page} ({exc})")
            break
        if looks_like_error(payload):
            break
        chunk = json.loads(payload)
        if not isinstance(chunk, list) or not chunk:
            break
        fingerprint = json.dumps(chunk[0], sort_keys=True)
        if fingerprint in seen:  # server ignoring `page`, stop rather than loop
            break
        seen.add(fingerprint)
        rows.extend(chunk)
        if len(chunk) < limit:
            break
    path = ctx.out / subdir / f"{endpoint}.jsonl.gz"
    body = "\n".join(json.dumps(r) for r in rows).encode()
    size = write_gz(path, body)
    ctx.manifest.record(key, endpoint=endpoint, rows=len(rows), bytes=size, path=str(path.relative_to(ctx.out)))
    ctx.log(f"  {key}: {len(rows):,} rows, {size / 1e6:.1f} MB")


def _capped_json(ctx: Context, subdir: str, endpoint: str, *, limit: int) -> None:
    """For endpoints that ignore ``page``: ask for everything in one shot."""
    key = f"{subdir}/{endpoint}"
    if ctx.manifest.has(key):
        ctx.log(f"  skip {key}")
        return
    try:
        payload = ctx.client.fetch(endpoint, {"limit": limit})
    except RuntimeError as exc:
        ctx.log(f"  FAIL {key}: {exc}")
        return
    if looks_like_error(payload):
        return
    rows = json.loads(payload)
    path = ctx.out / subdir / f"{endpoint}.jsonl.gz"
    size = write_gz(path, "\n".join(json.dumps(r) for r in rows).encode())
    ctx.manifest.record(key, endpoint=endpoint, rows=len(rows), bytes=size, path=str(path.relative_to(ctx.out)))
    note = "  (looks capped, history may be truncated)" if len(rows) in (100, 1000, limit) else ""
    ctx.log(f"  {key}: {len(rows):,} rows, {size / 1e6:.1f} MB{note}")


def _bulk_part_series(ctx: Context, subdir: str, endpoint: str, max_parts: int = 60) -> None:
    """``part=0,1,2,...`` bulk CSVs until one comes back with no data rows."""
    for part in range(max_parts):
        key = f"{subdir}/{endpoint}/part-{part}"
        if ctx.manifest.has(key):
            ctx.log(f"  skip {key}")
            continue
        try:
            payload = ctx.client.fetch(endpoint, {"part": part}, bulk=True)
        except RuntimeError as exc:
            ctx.log(f"  FAIL {key}: {exc}; leaving it for a re-run")
            return
        err = looks_like_error(payload)
        if err:
            ctx.log(f"  stop {endpoint} at part {part}: {err}")
            return
        rows = csv_rows(payload)
        if rows == 0:
            ctx.log(f"  stop {endpoint} at part {part}: empty")
            return
        path = ctx.out / subdir / endpoint / f"part-{part:03d}.csv.gz"
        size = write_gz(path, payload)
        ctx.manifest.record(key, endpoint=endpoint, part=part, rows=rows, bytes=size, path=str(path.relative_to(ctx.out)))
        ctx.log(f"  {key}: {rows:,} rows, {size / 1e6:.1f} MB")


def _bulk_snapshot(ctx: Context, subdir: str, endpoint: str) -> None:
    key = f"{subdir}/{endpoint}"
    if ctx.manifest.has(key):
        ctx.log(f"  skip {key}")
        return
    try:
        payload = ctx.client.fetch(endpoint, bulk=True)
    except RuntimeError as exc:
        ctx.log(f"  FAIL {key}: {exc}; leaving it for a re-run")
        return
    err = looks_like_error(payload)
    if err:
        ctx.log(f"  FAIL {key}: {err}")
        return
    rows = csv_rows(payload)
    path = ctx.out / subdir / f"{endpoint}.csv.gz"
    size = write_gz(path, payload)
    ctx.manifest.record(key, endpoint=endpoint, rows=rows, bytes=size, path=str(path.relative_to(ctx.out)))
    ctx.log(f"  {key}: {rows:,} rows, {size / 1e6:.1f} MB")


def _bulk_year_series(
    ctx: Context,
    subdir: str,
    endpoint: str,
    *,
    periods: Sequence[str] | None,
    year_max: int = YEAR_MAX,
    year_min: int = YEAR_MIN,
    anchor: int = 2024,
) -> None:
    """Walk outward from a year known to hold data, both directions.

    Anchoring matters. Walking straight down from ``year_max`` means the first
    years tried are in the future, and three empty ones in a row satisfies the
    stop rule before the walk ever reaches real data, silently truncating the
    dataset to nothing.
    """
    for period in periods or [None]:  # type: ignore[list-item]
        label = period or "all"
        back = list(range(anchor, year_min - 1, -1))
        forward = list(range(anchor + 1, year_max + 1))
        for direction in (back, forward):
            _walk_years(ctx, subdir, endpoint, period, label, direction)


def _walk_years(
    ctx: Context,
    subdir: str,
    endpoint: str,
    period: str | None,
    label: str,
    years: Sequence[int],
) -> None:
        empty_streak = 0
        for year in years:
            key = f"{subdir}/{endpoint}/{label}/{year}"
            if ctx.manifest.has(key):
                ctx.log(f"  skip {key}")
                empty_streak = 0
                continue
            params: dict[str, Any] = {"year": year}
            if period is not None:
                params["period"] = period
            try:
                payload = ctx.client.fetch(endpoint, params, bulk=True)
            except RuntimeError as exc:
                ctx.log(f"  FAIL {key}: {exc}")
                empty_streak += 1
                if empty_streak >= STOP_AFTER_EMPTY:
                    break
                continue
            err = looks_like_error(payload)
            rows = 0 if err else csv_rows(payload)
            if rows == 0:
                empty_streak += 1
                ctx.log(f"  {key}: empty{' (' + err + ')' if err else ''}")
                ctx.manifest.record(key, endpoint=endpoint, period=period, year=year, rows=0, bytes=0, path=None)
                if empty_streak >= STOP_AFTER_EMPTY:
                    ctx.log(f"  -> {endpoint} {label} history ends near {year}")
                    break
                continue
            empty_streak = 0
            path = ctx.out / subdir / endpoint / label / f"{year}.csv.gz"
            size = write_gz(path, payload)
            ctx.manifest.record(key, endpoint=endpoint, period=period, year=year, rows=rows, bytes=size, path=str(path.relative_to(ctx.out)))
            ctx.log(f"  {key}: {rows:,} rows, {size / 1e6:.1f} MB")


def task_reference(ctx: Context) -> None:
    ctx.log("[reference] symbol universe, delistings, ticker changes, profiles")
    for endpoint in (
        "stock-list",
        "financial-statement-symbol-list",
        "actively-trading-list",
        "available-exchanges",
        "available-industries",
        "available-sectors",
    ):
        _simple_json(ctx, "reference", endpoint)
    _paged_json(ctx, "reference", "delisted-companies", limit=100)
    _capped_json(ctx, "reference", "symbol-change", limit=100_000)
    _bulk_part_series(ctx, "reference", "profile-bulk")
    _bulk_snapshot(ctx, "reference", "peers-bulk")


def task_indices(ctx: Context) -> None:
    ctx.log("[indices] current and historical constituents")
    for endpoint in (
        "sp500-constituent",
        "historical-sp500-constituent",
        "nasdaq-constituent",
        "historical-nasdaq-constituent",
        "dowjones-constituent",
        "historical-dowjones-constituent",
    ):
        _simple_json(ctx, "indices", endpoint)


STATEMENT_ENDPOINTS = (
    "income-statement-bulk",
    "balance-sheet-statement-bulk",
    "cash-flow-statement-bulk",
    "ratios-bulk",
    "key-metrics-bulk",
)


def task_statements(ctx: Context) -> None:
    """Run the five statement endpoints CONCURRENTLY.

    Each has its own server-side cooldown, so working them one after another
    wastes four idle gates for every busy one.
    """
    ctx.log("[statements] income, balance, cash flow, ratios, key metrics; 5 endpoints in parallel")
    with ThreadPoolExecutor(max_workers=len(STATEMENT_ENDPOINTS)) as pool:
        futures = {
            pool.submit(_bulk_year_series, ctx, "statements", ep, periods=PERIODS): ep
            for ep in STATEMENT_ENDPOINTS
        }
        for future in futures:
            future.result()


def task_snapshots(ctx: Context) -> None:
    ctx.log("[snapshots] TTM ratios and metrics, scores, DCF, ratings (current state only)")
    for endpoint in (
        "key-metrics-ttm-bulk",
        "ratios-ttm-bulk",
        "scores-bulk",
        "dcf-bulk",
        "rating-bulk",
        "price-target-summary-bulk",
        "upgrades-downgrades-consensus-bulk",
    ):
        _bulk_snapshot(ctx, "snapshots", endpoint)


def task_estimates(ctx: Context) -> None:
    ctx.log("[estimates] analyst estimates by year, annual and quarterly")
    for period in ("annual", "quarter"):
        _bulk_year_series(
            ctx,
            "estimates",
            "analyst-estimates-bulk",
            periods=[period],
            year_max=2032,  # estimates run years into the future
            anchor=2024,  # verified to hold data before anchoring on it
        )


def _calendar_windows(start: dt.date, end: dt.date, step_days: int) -> Iterator[tuple[dt.date, dt.date]]:
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=step_days - 1), end)
        yield cursor, stop
        cursor = stop + dt.timedelta(days=1)


def task_earnings(ctx: Context) -> None:
    ctx.log("[earnings] surprises by year, plus the full earnings calendar")
    _bulk_year_series(ctx, "earnings", "earnings-surprises-bulk", periods=None)

    ctx.log("[earnings] sweeping earnings-calendar in windows (splitting any that truncate)")
    shard_dir = ctx.out / "earnings" / "earnings-calendar"
    for win_start, win_end in _calendar_windows(CALENDAR_START, CALENDAR_END, 28):
        key = f"earnings/earnings-calendar/{win_start}"
        if ctx.manifest.has(key):
            continue
        rows = _calendar_window(ctx, win_start, win_end, depth=0)
        path = shard_dir / f"{win_start.year}" / f"{win_start}.jsonl.gz"
        body = "\n".join(json.dumps(r) for r in rows).encode()
        size = write_gz(path, body)
        ctx.manifest.record(key, endpoint="earnings-calendar", start=str(win_start), end=str(win_end), rows=len(rows), bytes=size, path=str(path.relative_to(ctx.out)))
        if win_start.month == 1:
            ctx.log(f"  {win_start} .. {win_end}: {len(rows):,} rows")


def _calendar_window(ctx: Context, start: dt.date, end: dt.date, depth: int) -> list[Any]:
    """Fetch one window, halving it if the server truncates the response."""
    payload = ctx.client.fetch("earnings-calendar", {"from": str(start), "to": str(end)})
    if looks_like_error(payload):
        return []
    rows = json.loads(payload)
    if not isinstance(rows, list):
        return []
    if len(rows) >= CALENDAR_CAP and start < end and depth < 6:
        mid = start + (end - start) // 2
        left = _calendar_window(ctx, start, mid, depth + 1)
        right = _calendar_window(ctx, mid + dt.timedelta(days=1), end, depth + 1)
        return left + right
    return rows


def _load_universe(ctx: Context) -> list[str]:
    """Every symbol the mirror lists, from the reference pull."""
    path = ctx.out / "reference" / "stock-list.json.gz"
    if not path.exists():
        ctx.log("  stock-list missing, fetching it first")
        task_reference_minimal(ctx)
    with gzip.open(path, "rb") as handle:
        rows = json.loads(handle.read())
    symbols = sorted({str(r["symbol"]) for r in rows if r.get("symbol")})
    return symbols


def _analyst_covered(ctx: Context) -> set[str]:
    """Symbols with any analyst footprint, from two small bulk CSVs (~640 KB)."""
    covered: set[str] = set()
    for endpoint in ("upgrades-downgrades-consensus-bulk", "price-target-summary-bulk"):
        try:
            payload = ctx.client.fetch(endpoint, bulk=True)
        except RuntimeError as exc:
            ctx.log(f"  could not read {endpoint} for ordering: {exc}")
            continue
        if looks_like_error(payload):
            continue
        for line in payload.decode("utf8", "replace").splitlines()[1:]:
            if line:
                covered.add(line.split(",")[0].strip('"'))
    return covered


CALENDAR_SWEEPS = (
    # endpoint, subdir, window days, start year. All return every symbol for a
    # date range, so they cost a few hundred calls instead of ~92k.
    ("dividends-calendar", "corpactions", 28, 1985),
    ("splits-calendar", "corpactions", 365, 1985),
    ("ipos-calendar", "corpactions", 365, 1985),
)


def _date_sweep(
    ctx: Context,
    endpoint: str,
    subdir: str,
    step: int,
    start_year: int,
    extra: dict[str, Any] | None = None,
    label: str | None = None,
) -> None:
    """Sweep a from/to endpoint across history, splitting truncated windows.

    Wide ranges are NOT safe on every endpoint: treasury-rates and
    economic-indicators return an empty list rather than an error when the
    window is too large, which silently produces no data. Always sweep.
    """
    tag = label or endpoint
    start = dt.date(start_year, 1, 1)
    for win_start, win_end in _calendar_windows(start, CALENDAR_END, step):
        key = f"{subdir}/{tag}/{win_start}"
        if ctx.manifest.has(key):
            continue
        rows = _window_rows(ctx, endpoint, win_start, win_end, depth=0, extra=extra)
        if not rows:
            continue
        path = ctx.out / subdir / tag / str(win_start.year) / f"{win_start}.jsonl.gz"
        size = write_gz(path, "\n".join(json.dumps(r) for r in rows).encode())
        ctx.manifest.record(key, endpoint=endpoint, start=str(win_start), end=str(win_end),
                            rows=len(rows), bytes=size, path=str(path.relative_to(ctx.out)))
        if win_start.month == 1:
            ctx.log(f"  {tag} {win_start.year}: {len(rows):,} rows")


def _window_rows(ctx: Context, endpoint: str, start: dt.date, end: dt.date, depth: int,
                 extra: dict[str, Any] | None = None) -> list[Any]:
    try:
        payload = ctx.client.fetch(endpoint, {"from": str(start), "to": str(end), **(extra or {})})
    except RuntimeError as exc:
        ctx.log(f"  FAIL {endpoint} {start}..{end}: {exc}")
        return []
    if looks_like_error(payload):
        return []
    rows = json.loads(payload)
    if not isinstance(rows, list):
        return []
    if len(rows) >= CALENDAR_CAP and start < end and depth < 6:
        mid = start + (end - start) // 2
        return _window_rows(ctx, endpoint, start, mid, depth + 1, extra) + _window_rows(
            ctx, endpoint, mid + dt.timedelta(days=1), end, depth + 1, extra
        )
    return rows


def task_corpactions(ctx: Context) -> None:
    """Dividends, splits and IPOs. Corporate actions gate adjusted prices."""
    ctx.log("[corpactions] dividends, splits, IPOs swept by date across all symbols")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_date_sweep, ctx, ep, sub, step, yr)
                for ep, sub, step, yr in CALENDAR_SWEEPS]
        for f in futs:
            f.result()


def task_macro(ctx: Context) -> None:
    """Rates, economic series, and sector/industry valuation history.

    Everything here is swept in windows. A single wide request returns an
    empty list on these endpoints rather than an error, which is how the
    first run silently produced nothing.
    """
    # MEASURED: treasury-rates and economic-indicators cap at ~90 DAYS of
    # data per call, not a row count. A one-year request silently returns only
    # the last quarter (2020 gave 61 rows, all Oct-Dec). Sweep in 90-day steps.
    ctx.log("[macro] treasury rates and economic indicators, swept in 90-day windows")
    _date_sweep(ctx, "treasury-rates", "macro", 90, 1990)
    for name in ("CPI", "federalFunds", "inflationRate", "unemploymentRate",
                 "GDP", "realGDP", "retailSales", "consumerSentiment",
                 "industrialProductionTotalIndex", "totalNonfarmPayroll",
                 "initialClaims", "durableGoods",
                 "newPrivatelyOwnedHousingUnitsStartedTotalUnits",
                 "30YearFixedRateMortgageAverage", "15YearFixedRateMortgageAverage"):
        _date_sweep(ctx, "economic-indicators", "macro", 90, 1960,
                    extra={"name": name}, label=f"economic-indicators/{name}")

    ctx.log("[macro] sector and industry PE history")
    sectors = _list_values(ctx, "available-sectors", "sector")
    industries = _list_values(ctx, "available-industries", "industry")
    for sector in sectors:
        _date_sweep(ctx, "historical-sector-pe", "macro", 365, 2000,
                    extra={"sector": sector}, label=f"historical-sector-pe/{_slug(sector)}")
    for industry in industries:
        _date_sweep(ctx, "historical-industry-pe", "macro", 365, 2000,
                    extra={"industry": industry}, label=f"historical-industry-pe/{_slug(industry)}")
    _paged_json(ctx, "macro", "shares-float-all", limit=1000)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()


def _list_values(ctx: Context, endpoint: str, field: str) -> list[str]:
    path = ctx.out / "macro" / f"{endpoint}.json.gz"
    ref = ctx.out / "reference" / f"{endpoint}.json.gz"
    src = ref if ref.exists() else path
    if not src.exists():
        return []
    with gzip.open(src, "rb") as handle:
        rows = json.loads(handle.read())
    return [str(r[field]) for r in rows if r.get(field)]


ALT_PERSYMBOL: tuple[tuple[str, dict[str, Any]], ...] = (
    ("insider-trading/statistics", {}),
    ("esg-disclosures", {}),
    ("esg-ratings", {}),
    ("senate-trades", {}),
    ("house-trades", {}),
)


def _us_symbols(ctx: Context) -> list[str]:
    """US-listed subset of the statement universe."""
    path = ctx.out / "reference" / "financial-statement-symbol-list.json.gz"
    with gzip.open(path, "rb") as handle:
        rows = json.loads(handle.read())
    return sorted(
        {
            str(r["symbol"])
            for r in rows
            if r.get("symbol") and r.get("tradingCurrency") == "USD" and "." not in str(r["symbol"])
        }
    )


def task_alt(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """Tier 2: insider, institutional, political, ESG, M&A, ETF holdings."""
    ctx.log("[alt] global lists and paginated feeds")
    for endpoint in ("forex-list", "commodities-list", "crypto-list", "etf-list",
                     "earnings-transcript-list"):
        _simple_json(ctx, "alt", endpoint)
    for endpoint in ("insider-trading/search", "institutional-ownership/latest",
                     "mergers-acquisitions-latest"):
        _paged_json(ctx, "alt", endpoint, limit=1000, max_pages=2000)
    _bulk_part_series(ctx, "alt", "etf-holder-bulk", max_parts=40)

    # Scoped to US-listed names. Senate and house trades, ESG disclosures and
    # SEC-derived insider data only exist for US filers, so running the other
    # 42,000 foreign listings would spend ~17 hours confirming nothing. Filter
    # is tradingCurrency == USD and no exchange suffix: 26,172 of 68,207.
    symbols = _us_symbols(ctx)
    ctx.log(f"[alt] {len(symbols):,} US-listed symbols x {len(ALT_PERSYMBOL)} per-symbol endpoints")
    for endpoint, params in ALT_PERSYMBOL:
        _shard_crawl(ctx, "alt", endpoint, params, symbols, workers, stop_after_empty)


PRICE_ENDPOINTS = (
    "historical-price-eod/full",
    "historical-price-eod/dividend-adjusted",
)
PRICE_PAGE_CAP = 5000
PRICE_FLOOR = dt.date(1970, 1, 1)


def _price_history(ctx: Context, endpoint: str, _params: dict[str, Any], symbol: str) -> list[Any]:
    """Full history for one symbol, paged past the 5,000-row response cap.

    The endpoint returns at most 5,000 bars ending at ``to``. Walk ``to``
    backwards to the oldest bar received until a short page arrives, which is
    the start of the listing.
    """
    out: list[Any] = []
    to = dt.date.today()
    for _ in range(20):
        rows = ctx.client.fetch_json(
            endpoint, {"symbol": symbol, "from": str(PRICE_FLOOR), "to": str(to)}
        )
        if isinstance(rows, dict):
            rows = rows.get("historical", [])
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < PRICE_PAGE_CAP:
            break
        oldest = min(str(r["date"])[:10] for r in rows)
        to = dt.date.fromisoformat(oldest) - dt.timedelta(days=1)
        if to <= PRICE_FLOOR:
            break
    return out


def task_prices(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """Daily price history for every symbol, including delisted ones.

    MEASURED 2026-09-04: per-symbol beats the eod-bulk day sweep on both axes.
    eod-bulk needs ~18-30s per trading day because its per-endpoint limiter
    rejects anything faster, and it is served UNCOMPRESSED: 5,517 remaining
    days would be 28-46 h and ~14 GB. The per-symbol route runs at the normal
    request rate and gzips 7.6x (AAPL: 1.14 MB of JSON in 0.15 MB of wire),
    so the whole universe is ~5 h and ~3.5 GB.

    Delisted symbols are included deliberately. Prices for companies that no
    longer exist are exactly what a survivorship-bias-free backtest needs.
    """
    live = json.loads(gzip.open(ctx.out / "reference" / "stock-list.json.gz", "rb").read())
    symbols = {str(r["symbol"]) for r in live if r.get("symbol")}
    delisted_path = ctx.out / "reference" / "delisted-companies.jsonl.gz"
    if delisted_path.exists():
        with gzip.open(delisted_path, "rt") as handle:
            for line in handle:
                if line.strip():
                    sym = json.loads(line).get("symbol")
                    if sym:
                        symbols.add(str(sym))
    # Close the coverage gap against eod-bulk. A day file lists everything that
    # actually traded, which includes index and commodity pseudo-tickers
    # (000001.SS, GCUSD, CLUSD) that appear in no symbol list. Measured at 57
    # symbols, 0.4% of a trading day. Fold them in so the per-symbol route is
    # a strict superset of what the day sweep would have returned.
    seen = ctx.out / "prices" / "eod-bulk"
    if seen.exists():
        import csv as _csv
        import io as _io
        for f in seen.rglob("*.csv.gz"):
            with gzip.open(f, "rt") as handle:
                for row in _csv.DictReader(_io.StringIO(handle.read())):
                    if row.get("symbol"):
                        symbols.add(row["symbol"])
    ordered = sorted(symbols)
    ctx.log(f"[prices] {len(ordered):,} symbols (incl. delisted) x {len(PRICE_ENDPOINTS)} series")
    for endpoint in PRICE_ENDPOINTS:
        _shard_crawl(ctx, "prices", endpoint, {}, ordered, workers,
                     stop_after_empty, fetcher=_price_history)


# STAGE A. Verified 2026-09-04 on 50 securities across sector, age, domicile
# and restatement history: financial-statement-full-as-reported is a LOSSLESS
# SUPERSET of the three individual as-reported endpoints. Zero concepts were
# unique to the individual endpoints; 43,902 were unique to FULL. The three
# are therefore SKIP_REDUNDANT, saving ~205,000 requests.
#
# FULL carries no filingDate, acceptedDate or accession, so financial-reports-
# dates is fetched alongside it and is the ONLY source of PIT metadata here.
ASREPORTED_ENDPOINTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("financial-statement-full-as-reported", {"limit": 400}),
    ("financial-reports-dates", {}),
)
PRICE_TARGET_ENDPOINTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("price-target-news", {"limit": 1000}),      # individual analyst actions, timestamped
    ("price-target-consensus", {}),              # CURRENT snapshot, not history
    ("price-target-summary", {}),                # CURRENT snapshot, not history
)
SEC_PERSYMBOL: tuple[tuple[str, dict[str, Any]], ...] = (
    ("sec-filings-search/symbol", {"from": "1990-01-01", "to": "2027-12-31", "limit": 1000}),
)


def task_asreported(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """As-reported XBRL filings for every symbol that has statements."""
    path = ctx.out / "reference" / "financial-statement-symbol-list.json.gz"
    with gzip.open(path, "rb") as handle:
        rows = json.loads(handle.read())
    symbols = sorted({str(r["symbol"]) for r in rows if r.get("symbol")})
    ctx.log(f"[asreported] {len(symbols):,} symbols x {len(ASREPORTED_ENDPOINTS)} endpoints")
    for endpoint, params in ASREPORTED_ENDPOINTS:
        _shard_crawl(ctx, "asreported", endpoint, params, symbols, workers, stop_after_empty)


def task_pricetargets(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    symbols = _us_symbols(ctx)
    ctx.log(f"[pricetargets] {len(symbols):,} US symbols x {len(PRICE_TARGET_ENDPOINTS)} endpoints")
    for endpoint, params in PRICE_TARGET_ENDPOINTS:
        _shard_crawl(ctx, "pricetargets", endpoint, params, symbols, workers, stop_after_empty)


def task_secfilings(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """Filing metadata and EDGAR links. Document bodies are deliberately not
    pulled through FMP: it only points at EDGAR, so the index is the value."""
    ctx.log("[secfilings] date-swept 8-K and financial filing feeds")
    _date_sweep(ctx, "sec-filings-8k", "secfilings", 28, 1995)
    _date_sweep(ctx, "sec-filings-financials", "secfilings", 28, 1995)
    symbols = _us_symbols(ctx)
    ctx.log(f"[secfilings] {len(symbols):,} US symbols, per-symbol filing index")
    for endpoint, params in SEC_PERSYMBOL:
        _shard_crawl(ctx, "secfilings", endpoint, params, symbols, workers, stop_after_empty)


# STAGE B/C, shaped by measured endpoint behaviour (2026-09-04):
#  * etf/holdings is REDUNDANT: same grain as etf-holder-bulk (ETF -> held
#    security) but WITHOUT marketValue or lastUpdated. The bulk is strictly
#    richer, so only the weightings endpoints are fetched per ETF.
#  * news history effectively starts ~2015 (2010 returns nothing, 2015 gives
#    51 rows for a whole year) and pages cap at 250 rows.
#  * insider-trading and institutional-ownership global feeds cap at page 100
#    (~3 months). Only the per-symbol route reaches back years.
ETF_WEIGHTINGS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("etf/sector-weightings", {}),
    ("etf/country-weightings", {}),
)
STAGEB_PERSYMBOL: tuple[tuple[str, dict[str, Any]], ...] = (
    ("governance-executive-compensation", {}),
    ("insider-trading/search", {"page": 0, "limit": 1000}),
)
NEWS_SWEEPS = (("news/stock", 14, 2014), ("news/press-releases", 28, 2014))
COT_SWEEPS = (("commitment-of-traders-report", 90, 1990),
              ("commitment-of-traders-analysis", 90, 1990))
IPO_SWEEPS = (("ipos-prospectus", 180, 1995), ("ipos-disclosure", 180, 1995))

FX_SYMBOLS = ("EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF",
              "USDSGD", "USDCNY", "USDCNH", "EURJPY", "EURGBP", "NZDUSD",
              "USDHKD", "USDKRW", "USDINR", "USDTWD", "USDMYR", "USDTHB")
COMMODITY_SYMBOLS = ("GCUSD", "SIUSD", "CLUSD", "BZUSD", "NGUSD", "HGUSD",
                     "PLUSD", "PAUSD", "ZCUSD", "ZWUSD", "ZSUSD", "KCUSD")
INDEX_SYMBOLS = ("^GSPC", "^IXIC", "^NDX", "^DJI", "^RUT", "^RUA", "^VIX",
                 "^FTSE", "^GDAXI", "^FCHI", "^STOXX50E", "^N225", "^HSI",
                 "^000001.SS", "^AXJO", "^BSESN", "^KS11", "^STI")


def task_stageb(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    ctx.log("[stageb] ETF weightings, news, exec comp, IPO disclosure, fundraising, insider")
    for endpoint, step, yr in IPO_SWEEPS + NEWS_SWEEPS:
        _date_sweep(ctx, endpoint, "stageb", step, yr)
    _paged_json(ctx, "stageb", "fundraising-latest", limit=100, max_pages=4000)
    _year_series_json(ctx, "stageb", "executive-compensation-benchmark", 2000, 2027)
    etfs = _etf_universe(ctx)
    ctx.log(f"[stageb] {len(etfs):,} ETFs x {len(ETF_WEIGHTINGS)} weighting endpoints")
    for endpoint, params in ETF_WEIGHTINGS:
        _shard_crawl(ctx, "stageb", endpoint, params, etfs, workers, stop_after_empty)
    symbols = _us_symbols(ctx)
    for endpoint, params in STAGEB_PERSYMBOL:
        _shard_crawl(ctx, "stageb", endpoint, params, symbols, workers, stop_after_empty)


def _year_series_json(ctx: Context, subdir: str, endpoint: str, lo: int, hi: int) -> None:
    for year in range(lo, hi + 1):
        key = f"{subdir}/{endpoint}/{year}"
        if ctx.manifest.has(key):
            continue
        try:
            payload = ctx.client.fetch(endpoint, {"year": year})
        except RuntimeError as exc:
            ctx.log(f"  FAIL {key}: {exc}")
            continue
        if looks_like_error(payload):
            continue
        rows = json.loads(payload)
        if not rows:
            continue
        path = ctx.out / subdir / endpoint / f"{year}.json.gz"
        size = write_gz(path, payload)
        ctx.manifest.record(key, endpoint=endpoint, year=year, rows=len(rows),
                            bytes=size, path=str(path.relative_to(ctx.out)))
        ctx.log(f"  {key}: {len(rows):,} rows")


def _etf_universe(ctx: Context) -> list[str]:
    """ETF tickers, taken from the holdings bulk we already hold."""
    out: set[str] = set()
    d = ctx.out / "alt" / "etf-holder-bulk"
    if d.exists():
        import csv as _csv
        import io as _io
        for f in sorted(d.glob("*.csv.gz")):
            with gzip.open(f, "rt") as h:
                for row in _csv.DictReader(_io.StringIO(h.read())):
                    if row.get("symbol") and row["symbol"].strip() not in ("", "--", "-- "):
                        out.add(row["symbol"].strip())
    return sorted(out)


def task_stagec(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    ctx.log("[stagec] COT, FX, commodities, indices")
    for endpoint, step, yr in COT_SWEEPS:
        _date_sweep(ctx, endpoint, "stagec", step, yr)
    for label, syms in (("fx", FX_SYMBOLS), ("commodities", COMMODITY_SYMBOLS),
                        ("indices", INDEX_SYMBOLS)):
        ctx.log(f"[stagec] {label}: {len(syms)} instruments")
        _shard_crawl(ctx, f"stagec/{label}", "historical-price-eod/full", {},
                     list(syms), workers, 0, fetcher=_price_history)


def _transcript_symbols(ctx: Context) -> list[str]:
    """Companies known to have transcripts, from the single global index."""
    d = ctx.out / "alt" / "earnings-transcript-list.json.gz"
    if d.exists():
        rows = json.loads(gzip.open(d, "rb").read())
    else:
        shard = sorted((ctx.out / "alt" / "earnings-transcript-list").glob("*.jsonl.gz"))
        rows = []
        for line in gzip.open(shard[0], "rt"):
            o = json.loads(line)
            if "_error" not in o:
                rows = o["data"]
                break
    return sorted({str(r["symbol"]) for r in rows if r.get("symbol")})


def _transcript_text(ctx: Context, endpoint: str, _params: dict[str, Any], symbol: str) -> list[Any]:
    """Every transcript for one company: enumerate its dates, then fetch each.

    Two phases because the index only gives a COUNT per company. Sized
    2026-09-04: 11,218 companies, 283,886 transcript events, median 18 each.
    """
    try:
        dates = ctx.client.fetch_json("earning-call-transcript-dates", {"symbol": symbol})
    except RuntimeError:
        return []
    if not isinstance(dates, list):
        return []
    out: list[Any] = []
    for d in dates:
        year, quarter = d.get("fiscalYear") or d.get("year"), d.get("quarter")
        if year is None or quarter is None:
            continue
        try:
            body = ctx.client.fetch_json(
                endpoint, {"symbol": symbol, "year": year, "quarter": quarter}
            )
        except RuntimeError:
            continue
        for rec in body if isinstance(body, list) else [body]:
            if isinstance(rec, dict) and rec.get("content"):
                rec.setdefault("symbol", symbol)
                rec.setdefault("_index_date", d.get("date"))
                out.append(rec)
    return out


def task_transcripts(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """Raw earnings-call transcript TEXT. Never summarised, never embedded."""
    symbols = _transcript_symbols(ctx)
    ctx.log(f"[transcripts] {len(symbols):,} companies; ~283,886 transcript events expected")
    _shard_crawl(ctx, "transcripts", "earning-call-transcript", {}, symbols,
                 workers, 0, fetcher=_transcript_text)


def task_reference_minimal(ctx: Context) -> None:
    _simple_json(ctx, "reference", "stock-list")


SHARD_SIZE = 500


PERSYMBOL_ENDPOINTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("financial-growth", {"limit": 200}),
    ("enterprise-values", {"limit": 200}),
    ("historical-market-capitalization", {"limit": 5000}),
    ("revenue-product-segmentation", {}),
    ("revenue-geographic-segmentation", {}),
    ("owner-earnings", {"limit": 200}),
    ("employee-count", {"limit": 200}),
    ("key-executives", {}),
)


def task_persymbol(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """Tier 1 per-symbol endpoints, over the symbols that have statements.

    None of these has a bulk form. Scoped to ``financial-statement-symbol-list``
    (68,207) rather than the full 91,902, because a company with no financials
    has no growth, enterprise value or segment data either.
    """
    path = ctx.out / "reference" / "financial-statement-symbol-list.json.gz"
    with gzip.open(path, "rb") as handle:
        rows = json.loads(handle.read())
    symbols = sorted({str(r["symbol"]) for r in rows if r.get("symbol")})
    ctx.log(f"[persymbol] {len(symbols):,} symbols x {len(PERSYMBOL_ENDPOINTS)} endpoints")
    for endpoint, params in PERSYMBOL_ENDPOINTS:
        _shard_crawl(ctx, "persymbol", endpoint, params, symbols, workers, stop_after_empty)


def task_revisions(ctx: Context, workers: int, stop_after_empty: int = 0) -> None:
    """Per-symbol analyst actions and consensus history, across the universe.

    This is the only part of the corpus with no bulk equivalent, so it costs
    one request per symbol per endpoint. Symbols are grouped into shards of
    SHARD_SIZE and a shard is manifested only once every symbol in it landed,
    which is what makes an interrupted run resumable without re-fetching.
    """
    symbols = _load_universe(ctx)
    covered = _analyst_covered(ctx)
    # Deterministic: same ordering every run, so shard N always means the same
    # 500 symbols and resume stays correct.
    symbols = sorted(symbols, key=lambda sym: (sym not in covered, sym))
    ctx.log(
        f"[revisions] {len(symbols):,} symbols x 2 endpoints; "
        f"{len(covered & set(symbols)):,} with known analyst coverage go first"
    )

    for endpoint, params in (
        ("grades", {}),
        ("grades-historical", {"limit": 10000}),
    ):
        _shard_crawl(ctx, "revisions", endpoint, params, symbols, workers, stop_after_empty)


def _is_network_error(message: str) -> bool:
    """Connection-level failure rather than a server rejection."""
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("urlopen error", "network:", "timed out", "connection",
                      "nodename", "temporary failure", "ssl", "unreachable")
    )


def _shard_crawl(
    ctx: Context,
    subdir: str,
    endpoint: str,
    params: dict[str, Any],
    symbols: Sequence[str],
    workers: int,
    stop_after_empty: int,
    fetcher: Callable[[Context, str, dict[str, Any], str], Any] | None = None,
) -> None:
    """Crawl one per-symbol endpoint in resumable shards of SHARD_SIZE."""
    shards = [symbols[i : i + SHARD_SIZE] for i in range(0, len(symbols), SHARD_SIZE)]
    pending = [
        (i, sh) for i, sh in enumerate(shards)
        if not ctx.manifest.has(f"{subdir}/{endpoint}/shard-{i:04d}")
    ]
    ctx.log(f"  {endpoint}: {len(pending):,}/{len(shards):,} shards to go")
    started = time.time()
    empty_run = 0
    for done, (index, shard) in enumerate(pending, start=1):
        if stop_after_empty and empty_run >= stop_after_empty:
            ctx.log(
                f"  {endpoint}: {empty_run} consecutive empty shards "
                f"({empty_run * SHARD_SIZE:,} symbols, no data); stopping this endpoint"
            )
            break
        key = f"{subdir}/{endpoint}/shard-{index:04d}"
        results: list[str] = []
        covered = 0
        errors = 0
        net_errors = 0

        def one(symbol: str, _ep: str = endpoint, _p: dict[str, Any] = params) -> tuple[str, Any]:
            try:
                if fetcher is not None:
                    return symbol, fetcher(ctx, _ep, _p, symbol)
                return symbol, ctx.client.fetch_json(_ep, {"symbol": symbol, **_p})
            except Exception as exc:  # record the gap, keep going
                return symbol, {"_error": str(exc)[:200]}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for symbol, data in pool.map(one, shard):
                if isinstance(data, list) and data:
                    covered += 1
                    results.append(json.dumps({"symbol": symbol, "data": data}))
                elif isinstance(data, dict) and "_error" in data:
                    errors += 1
                    if _is_network_error(str(data["_error"])):
                        net_errors += 1
                    results.append(json.dumps({"symbol": symbol, **data}))

        path = ctx.out / subdir / endpoint / f"shard-{index:04d}.jsonl.gz"
        size = write_gz(path, "\n".join(results).encode())
        # ANY connectivity failure leaves the shard unmarked, no matter how
        # few symbols it hit: an errored symbol is indistinguishable from a
        # symbol with no data once the shard is recorded as complete.
        if net_errors or errors > max(2, len(shard) // 50):
            reason = f"{net_errors} network" if net_errors else f"{errors}/{len(shard)}"
            ctx.log(f"  {key}: {reason} errored, NOT marking done")
            continue
        ctx.manifest.record(key, endpoint=endpoint, shard=index, symbols=len(shard),
                            covered=covered, errors=errors, bytes=size,
                            path=str(path.relative_to(ctx.out)))
        empty_run = empty_run + 1 if covered == 0 else 0
        if done % 10 == 0 or done == len(pending):
            rate = done / max(time.time() - started, 1e-9)
            eta = (len(pending) - done) / max(rate, 1e-9) / 3600
            ctx.log(f"  {endpoint} {done:,}/{len(pending):,} shards, ETA {eta:.1f}h")


TASKS: dict[str, Callable[[Context], None]] = {
    "reference": task_reference,
    "indices": task_indices,
    "statements": task_statements,
    "snapshots": task_snapshots,
    "estimates": task_estimates,
    "earnings": task_earnings,
    "corpactions": task_corpactions,
    "macro": task_macro,
}
ALL_GROUPS = ["indices", "earnings", "estimates", "revisions", "statements",
              "snapshots", "reference", "corpactions", "macro", "persymbol", "alt", "prices",
              "asreported", "pricetargets", "secfilings", "stageb", "stagec", "transcripts"]


def load_key() -> tuple[str, str]:
    key = os.environ.get("FMP_API_KEY")
    base = os.environ.get("FMP_BASE_URL", "https://fmp-v2.icu/stable")
    if not key:
        env = REPO_ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line.startswith("FMP_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                elif line.startswith("FMP_BASE_URL="):
                    base = line.split("=", 1)[1].strip()
    if not key:
        raise SystemExit("FMP_API_KEY not set (env or .env)")
    return key, base.rstrip("/")


def record_spend(out: Path, wire_bytes: int, groups: Sequence[str]) -> int:
    """Accumulate wire bytes across runs so quota spend is never a guess."""
    path = out / "_wire_ledger.json"
    ledger: dict[str, Any] = {"total_bytes": 0, "runs": []}
    if path.exists():
        try:
            ledger = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    ledger["total_bytes"] = int(ledger.get("total_bytes", 0)) + wire_bytes
    ledger["runs"].append({
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "bytes": wire_bytes,
        "groups": list(groups),
    })
    ledger["runs"] = ledger["runs"][-200:]
    path.write_text(json.dumps(ledger, indent=2))
    return int(ledger["total_bytes"])


def print_status(out: Path) -> None:
    manifest = out / "_manifest.jsonl"
    if not manifest.exists():
        print(f"no manifest at {manifest}")
        return
    groups: dict[str, dict[str, int]] = {}
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        group = row["key"].split("/", 1)[0]
        agg = groups.setdefault(group, {"units": 0, "rows": 0, "bytes": 0})
        agg["units"] += 1
        agg["rows"] += int(row.get("rows") or row.get("covered") or 0)
        agg["bytes"] += int(row.get("bytes") or 0)
    print(f"{'group':14s} {'units':>8s} {'rows':>14s} {'on disk':>12s}")
    total = {"units": 0, "rows": 0, "bytes": 0}
    for group, agg in sorted(groups.items()):
        print(f"{group:14s} {agg['units']:8,d} {agg['rows']:14,d} {agg['bytes'] / 1e6:9.1f} MB")
        for field_name in total:
            total[field_name] += agg[field_name]
    print(f"{'TOTAL':14s} {total['units']:8,d} {total['rows']:14,d} {total['bytes'] / 1e6:9.1f} MB")
    ledger = out / "_wire_ledger.json"
    if ledger.exists():
        spent = json.loads(ledger.read_text()).get("total_bytes", 0)
        print(f"\nquota spent by this tool: {spent / 1e6:,.1f} MB over the wire")
    cache = out / "_cache"
    if cache.exists():
        n = sum(1 for _ in cache.rglob("*.gz"))
        print(f"cached responses        : {n:,} (re-fetching any of these costs nothing)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--only", default=",".join(ALL_GROUPS), help=f"comma-separated: {','.join(ALL_GROUPS)}")
    parser.add_argument("--rate", type=float, default=5.5, help="global requests/sec (measured ceiling ~6.6)")
    parser.add_argument("--bulk-interval", type=float, default=10.0, help="min seconds between bulk calls")
    parser.add_argument("--workers", type=int, default=6, help="concurrency for the per-symbol crawl")
    parser.add_argument("--force", action="store_true", help="ignore the manifest and re-fetch")
    parser.add_argument("--revisions-stop-after-empty", type=int, default=0,
                        help="DEPRECATED and OFF by default. Stopping a per-symbol crawl on N "
                             "consecutive empty shards is unsafe: symbols are ordered "
                             "alphabetically, so a run of foreign tickers looks identical to the "
                             "end of coverage. It halted employee-count 6 shards before the first "
                             "US ticker. Exhaustive means every symbol attempted. Opt in only for "
                             "a crawl explicitly ordered by known coverage.")
    parser.add_argument("--budget-mb", type=float, default=0, help="stop this run after N MB over the wire (0 = unlimited)")
    parser.add_argument("--probe", help="fetch one endpoint ad hoc, THROUGH the cache, e.g. 'ratios-bulk?year=2024&period=FY'")
    parser.add_argument("--no-cache", action="store_true", help="do not read or write the raw response cache")
    parser.add_argument("--status", action="store_true", help="summarise what is already downloaded")
    args = parser.parse_args(argv)

    out: Path = args.out
    if args.status:
        print_status(out)
        return 0

    out.mkdir(parents=True, exist_ok=True)
    key, base = load_key()
    client = Client(
        base=base,
        key=key,
        limiter=RateLimiter(args.rate),
        bulk_gate=BulkGate(args.bulk_interval),
        cache_dir=None if args.no_cache else out / "_cache",
        budget_bytes=int(args.budget_mb * 1e6),
    )
    manifest = Manifest(out / "_manifest.jsonl", force=args.force)

    def log(message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        print(f"{stamp} {message}", flush=True)

    if args.probe:
        endpoint, _, raw_query = args.probe.partition("?")
        params = dict(urllib.parse.parse_qsl(raw_query))
        try:
            body = client.fetch(endpoint, params, bulk=endpoint.endswith("-bulk"))
        except (BandwidthExhaustedError, BudgetExhaustedError) as exc:
            log(f"{endpoint}: {exc}")
            record_spend(out, client.wire_bytes, [f"probe:{endpoint}"])
            return 1
        except RuntimeError as exc:
            log(f"{endpoint}: {exc}")
            return 1
        err = looks_like_error(body)
        head = body[:300].decode("utf8", "replace").replace("\n", " ")
        rows = csv_rows(body) if body[:1] in (b'"', b"s") else None
        if rows is None:
            try:
                parsed = json.loads(body)
                rows = len(parsed) if isinstance(parsed, list) else 1
            except json.JSONDecodeError:
                rows = -1
        log(f"{endpoint} {params}: {len(body):,} B, {rows:,} rows, wire {client.wire_bytes / 1e6:.2f} MB")
        log(f"  {'ERROR: ' + err if err else head[:200]}")
        record_spend(out, client.wire_bytes, [f"probe:{endpoint}"])
        return 0

    ctx = Context(client=client, manifest=manifest, out=out, log=log)
    groups = [g.strip() for g in args.only.split(",") if g.strip()]
    unknown = [g for g in groups if g not in ALL_GROUPS]
    if unknown:
        raise SystemExit(f"unknown group(s): {unknown}; valid: {ALL_GROUPS}")

    started = time.time()
    for group in groups:
        try:
            if group == "revisions":
                task_revisions(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "persymbol":
                task_persymbol(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "alt":
                task_alt(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "prices":
                task_prices(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "asreported":
                task_asreported(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "pricetargets":
                task_pricetargets(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "secfilings":
                task_secfilings(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "stageb":
                task_stageb(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "stagec":
                task_stagec(ctx, args.workers, args.revisions_stop_after_empty)
            elif group == "transcripts":
                task_transcripts(ctx, args.workers, args.revisions_stop_after_empty)
            else:
                TASKS[group](ctx)
        except KeyboardInterrupt:
            log("interrupted; manifest holds what finished")
            return 130
        except BudgetExhaustedError as exc:
            log(f"[{group}] STOPPED: {exc}")
            log("Raise --budget-mb to continue. Everything finished is in the manifest.")
            break
        except BandwidthExhaustedError as exc:
            log(f"[{group}] STOPPED: {exc}")
            log("30-day byte quota is gone; everything finished so far is in the manifest.")
            log("Re-run the same command when quota returns: it resumes, it does not restart.")
            break
        except Exception as exc:  # one bad group must not sink the rest
            log(f"[{group}] ABORTED: {type(exc).__name__}: {exc}")

    lifetime = record_spend(out, client.wire_bytes, groups)
    log(f"done in {(time.time() - started) / 60:.1f} min; wire {client.wire_bytes / 1e6:.1f} MB "
        f"this run, {lifetime / 1e6:,.1f} MB lifetime; http stats {client.stats}")
    print_status(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
