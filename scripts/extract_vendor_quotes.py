"""Extract real bid/ask quotes for the candidate pool from the vendor archive.

The DiscountOptionData archive ships ~134 GB of zips (2005-01-03 to
2026-08-27) holding roughly 600 GB of daily CSVs. We only ever need the
symbols in ``kai_trader.universe.pool``, which is under 1% of the rows, so
this reads the zips in place and writes one compact gzipped CSV per symbol
(~3.6 GB total). The full pass happens once; re-querying the compact store
afterwards is cheap, including when the weekly universe review promotes a
new name.

Five archive hazards are handled explicitly, because none of them raise on
their own and all of them produce a plausible-looking but wrong cache:

* The per-day file split varies by era: one ``..._OData.csv`` for 2005-2019,
  ``OData1``/``OData2`` in 2021, and a third part in 2020. Globbing
  ``OData1`` silently drops every symbol from S-Z, which in the live sleeves
  means SOFI and T.
* 85 dates appear in more than one zip (June 2020 lives in both
  ``2020June.zip`` and ``2020_2G.zip``). Members are deduplicated by filename.
* Files exist for market holidays and carry a stale copy of the previous
  trading day's prices under the holiday's date. Dates are filtered against
  the real session calendar.
* One weekend-dated file exists (2020-08-01). The same filter removes it.
* Two days ship dates as ``M/D/YYYY`` instead of ISO (2018-11-06 and
  2019-09-03, ~47k rows). ``_norm_date`` accepts both and normalises output.

A second class of hazard is that the archive lies about its own dates and
about which rows are tradable. These are corrected here rather than left for
consumers, because every one of them is silent:

* Five files splice a SECOND session in beside the one they are named for,
  roughly 50/50: 2015-09-01 also holds 2015-10-16, 2009-01-02 holds
  2009-01-29, 2010-10-01 holds 2010-10-14, 2016-10-03 holds 2016-10-07 and
  2020-05-01 holds 2020-05-04. Dating rows from the filename therefore
  back-dates real quotes by up to 45 days, which is lookahead: a consumer
  reading 2015-09-01 would be handed October's prices. Every row is now
  dated from its own ``Data Date`` column, and a row whose date disagrees
  with its file is DROPPED rather than re-dated. Dropping is lossless
  because all five spliced sessions also ship as their own correctly named
  files. Re-dating them in place is what you would reach for first, and it
  is wrong: the guest rows leave the member out of date order, which breaks
  the ordering assumption ``merge_shards`` needs, and the duplicate
  sessions then survive into the output. Every drop is logged with both
  dates under ``vendor.session.spliced``.
* ~51M rows carry a contract that had ALREADY expired at the quote date,
  mostly Nov 2019 to Jun 2020 and again in 2026. They hold a stale copy of
  the contract's last live quote and are not tradable. Dropped.
* A small number of rows quote a negative price, or a bid above the ask.
  Dropped rather than passed through.
* Some contract-days appear twice with disagreeing volume and open interest.
  ``merge_shards`` keeps the richer copy; the quote itself agrees.

Known gap: 2024-12-31 is a real NYSE session with no vendor file, and it is
not recoverable from the archive (the holiday files each carry their own
date, so none of them hides it). It is the only missing session in the 5,447
between 2005-01-03 and 2026-08-27. 2018-10-12 is present but truncated: it
begins at ticker ALT1, losing 158 symbols from A to ALSN including AAPL.
Both need a re-delivery or an outside source. ``scripts/audit_vendor_archive.py``
re-checks for both classes of defect after any new delivery.

Usage:
    uv run python scripts/extract_vendor_quotes.py --source Greeks
    uv run python scripts/extract_vendor_quotes.py --source Greeks --limit-days 5
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import multiprocessing as mp
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger
from kai_trader.universe.pool import CANDIDATE_POOL

_log = get_logger(__name__)

_MEMBER_RE: Final = re.compile(r"Greek_(\d{8})_OData(\d*)\.csv$")

# Vendor column order, stable across every year 2005-2021 (verified).
_C_OPTION_KEY: Final = 0
_C_SYMBOL: Final = 1
_C_EXPIRATION: Final = 2
_C_ASK: Final = 3
_C_ASK_SIZE: Final = 4
_C_BID: Final = 5
_C_BID_SIZE: Final = 6
_C_LAST: Final = 7
_C_PUT_CALL: Final = 8
_C_STRIKE: Final = 9
_C_VOLUME: Final = 10
_C_OPEN_INTEREST: Final = 11
_C_UNDERLYING: Final = 12
_C_IV: Final = 13
_C_DELTA: Final = 14
_C_GAMMA: Final = 15
_C_VEGA: Final = 16
_C_RHO: Final = 17
_C_THETA: Final = 18
_C_DATA_DATE: Final = 19
_N_COLS: Final = 20

_OUT_HEADER: Final = [
    "option_symbol",
    "asof",
    "expiration",
    "strike",
    "right",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "last",
    "volume",
    "open_interest",
    "underlying",
    "iv",
    "delta",
    "gamma",
    "vega",
    "rho",
    "theta",
]


def _norm_date(value: str) -> tuple[str, str, str] | None:
    """Normalise a vendor date to ``(YYYY, MM, DD)``.

    Almost every file uses ISO ``2019-09-06``, but two days in the archive
    switch to ``M/D/YYYY`` without warning: 2018-11-06 (inside the Q4 2018
    selloff) and 2019-09-03. Those two days are ~47k rows that would
    otherwise be dropped silently.
    """
    text = value.strip()
    if "-" in text:
        parts = text.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            return parts[0], parts[1].zfill(2), parts[2].zfill(2)
        return None
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            return parts[2], parts[0].zfill(2), parts[1].zfill(2)
    return None


def _occ_symbol(root: str, expiration: str, put_call: str, strike: str) -> str | None:
    """Build the OCC symbol the rest of the system uses.

    ``F`` + ``2024-03-15`` + ``put`` + ``12.82`` -> ``F240315P00012820``.
    Matches ``kai_trader.broker.options_data.parse_occ_symbol``: strike is in
    thousandths of a dollar, zero-padded to eight digits. Returns ``None`` on
    anything malformed so the caller can count and skip it rather than write
    a corrupt key.
    """
    parsed = _norm_date(expiration)
    if parsed is None:
        return None
    yy, mm, dd = parsed
    try:
        thousandths = int(Decimal(strike) * 1000)
    except (ValueError, InvalidOperation, ArithmeticError):
        return None
    if thousandths < 0:
        return None
    cp = "P" if put_call.lower().startswith("p") else "C"
    return f"{root}{yy[2:]}{mm}{dd}{cp}{thousandths:08d}"


def _load_sessions(cache: Path, start: date, end: date) -> set[str]:
    """Return real NYSE session dates as ISO strings, cached on disk.

    Holiday-dated vendor files repeat the prior session verbatim, so this is
    what separates a real trading day from a stale duplicate.
    """
    if cache.exists():
        stored = json.loads(cache.read_text())
        # Older cache files were a bare list with no record of the range they
        # covered. Re-fetch unless the cache demonstrably spans what we need,
        # otherwise adding a new year of source data would silently drop every
        # date past the old cache's end as a "non-session".
        if isinstance(stored, dict):
            covered_from = stored.get("from", "")
            covered_to = stored.get("to", "")
            if covered_from <= start.isoformat() and covered_to >= end.isoformat():
                return set(stored["sessions"])
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    cfg = get_settings()
    client = TradingClient(
        api_key=cfg.effective_alpaca_api_key,
        secret_key=cfg.effective_alpaca_secret_key,
        paper=cfg.alpaca_paper,
    )
    sessions: set[str] = set()
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=365), end)
        for day in client.get_calendar(GetCalendarRequest(start=cursor, end=chunk_end)):
            sessions.add(day.date.isoformat())
        cursor = chunk_end + timedelta(days=1)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "sessions": sorted(sessions),
            }
        )
    )
    _log.info(
        "vendor.sessions.cached",
        count=len(sessions),
        path=str(cache),
        covers=f"{start.isoformat()}..{end.isoformat()}",
    )
    return sessions


def index_members(source: Path) -> dict[str, dict[str, tuple[str, str]]]:
    """Map ``YYYYMMDD -> {member_filename: (zip_path, member_path)}``.

    Keying the inner dict by filename deduplicates the 85 dates that appear
    in more than one zip, and keeps every part of a multi-part day.
    """
    index: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for zip_path in sorted(source.glob("**/*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                match = _MEMBER_RE.search(member)
                if match is None:
                    continue
                index[match.group(1)][member.rsplit("/", 1)[-1]] = (
                    str(zip_path),
                    member,
                )
    return index


def _extract_one_zip(
    task: tuple[str, list[tuple[str, str]], list[str], str, list[str]],
) -> tuple[str, int, int, dict[str, int], dict[str, int], dict[str, int]]:
    """Worker: stream one zip, write per-symbol shards, return counts.

    Rows are prefiltered at the bytes level on the Symbol column before any
    CSV parsing, because the pool is ~0.9% of rows and fully parsing the
    other 99% would dominate the runtime.

    Every row is dated from its own ``Data Date`` column, never from the
    filename, and is dropped unless that date is a real session. Rows whose
    contract already expired, or whose quote is negative or crossed, are
    dropped and counted. See the module docstring for why each of these
    exists.
    """
    zip_path, members, wanted_list, shard_root, sessions_list = task
    wanted = {s.encode() for s in wanted_list}
    sessions = set(sessions_list)
    shard_dir = Path(shard_root) / Path(zip_path).stem
    shard_dir.mkdir(parents=True, exist_ok=True)

    handles: dict[str, gzip.GzipFile] = {}
    writers: dict[str, csv.writer] = {}  # type: ignore[type-arg]
    per_symbol: dict[str, int] = defaultdict(int)
    rejects: dict[str, int] = defaultdict(int)
    foreign: dict[str, int] = defaultdict(int)
    kept = 0
    malformed = 0

    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member, file_asof in members:
                with archive.open(member) as raw:
                    raw.readline()  # header
                    for line in raw:
                        # Symbol is the second comma-separated field.
                        parts = line.split(b",", 3)
                        if len(parts) < 3 or parts[_C_SYMBOL] not in wanted:
                            continue
                        row = next(csv.reader([line.decode("utf-8", "replace")]))
                        if len(row) != _N_COLS:
                            malformed += 1
                            continue
                        symbol = row[_C_SYMBOL]
                        parsed_exp = _norm_date(row[_C_EXPIRATION])
                        occ = _occ_symbol(
                            symbol,
                            row[_C_EXPIRATION],
                            row[_C_PUT_CALL],
                            row[_C_STRIKE],
                        )
                        if occ is None or parsed_exp is None:
                            malformed += 1
                            continue
                        expiration_iso = "-".join(parsed_exp)

                        # The row's own date wins. Five archive files splice a
                        # second session in beside the one they are named for,
                        # so trusting the filename back-dates real quotes by up
                        # to 45 days and leaks the future into the past.
                        parsed_asof = _norm_date(row[_C_DATA_DATE])
                        if parsed_asof is None:
                            malformed += 1
                            continue
                        asof = "-".join(parsed_asof)
                        if asof != file_asof:
                            # A spliced session. Every one of them also ships as
                            # its own correctly named file, so this copy is
                            # redundant and dropping it is lossless. Keeping it
                            # would re-date rows into a member that is emitted
                            # out of order, which breaks the date-ordering
                            # ``merge_shards`` relies on. The dates are logged,
                            # and audit_vendor_archive.py --deep re-checks that
                            # each one still has a file of its own.
                            rejects["spliced_foreign_date"] += 1
                            foreign[f"{file_asof}->{asof}"] += 1
                            continue
                        if asof not in sessions:
                            rejects["non_session_date"] += 1
                            continue
                        # Dead contracts carry a stale copy of their last live
                        # quote. They are not tradable at this asof.
                        if expiration_iso < asof:
                            rejects["expired_contract"] += 1
                            continue
                        bid_raw, ask_raw = row[_C_BID].strip(), row[_C_ASK].strip()
                        try:
                            bid, ask = float(bid_raw or 0), float(ask_raw or 0)
                        except ValueError:
                            malformed += 1
                            continue
                        if bid < 0 or ask < 0:
                            rejects["negative_quote"] += 1
                            continue
                        if bid > 0 and ask > 0 and bid > ask:
                            rejects["crossed_quote"] += 1
                            continue

                        if symbol not in writers:
                            handle = gzip.open(
                                shard_dir / f"{symbol}.csv.gz", "wt", newline=""
                            )
                            handles[symbol] = handle  # type: ignore[assignment]
                            writers[symbol] = csv.writer(handle)
                        writers[symbol].writerow(
                            [
                                occ,
                                asof,
                                expiration_iso,
                                row[_C_STRIKE],
                                "put" if row[_C_PUT_CALL].lower().startswith("p") else "call",
                                row[_C_BID],
                                row[_C_ASK],
                                row[_C_BID_SIZE],
                                row[_C_ASK_SIZE],
                                row[_C_LAST],
                                row[_C_VOLUME],
                                row[_C_OPEN_INTEREST],
                                row[_C_UNDERLYING],
                                row[_C_IV].strip(),
                                row[_C_DELTA],
                                row[_C_GAMMA],
                                row[_C_VEGA],
                                row[_C_RHO],
                                row[_C_THETA],
                            ]
                        )
                        per_symbol[symbol] += 1
                        kept += 1
    finally:
        for handle in handles.values():
            handle.close()
    return zip_path, kept, malformed, dict(per_symbol), dict(rejects), dict(foreign)


def _row_quality(row: list[str]) -> tuple[float, float, float]:
    """Rank two copies of the same contract-day. Higher is more informative.

    The archive ships some contract-days twice, and the copies disagree:
    typically one carries the quote with zeroed volume and open interest
    while the other carries the trade counts. Keeping the richer row loses
    nothing, because the quote itself agrees between them.
    """

    def _f(value: str) -> float:
        try:
            return float(value or 0)
        except ValueError:
            return 0.0

    return (_f(row[11]), _f(row[10]), _f(row[5]))


def _flush_day(writer: Any, best: dict[str, list[str]]) -> int:
    """Write one buffered session's deduplicated rows and clear the buffer.

    ``writer`` is a ``csv.writer``, which has no usable static type.
    """
    written = 0
    for contract in sorted(best):
        writer.writerow(best[contract])
        written += 1
    best.clear()
    return written


def merge_shards(shard_root: Path, out_dir: Path, symbols: list[str]) -> tuple[dict[str, int], int]:
    """Merge per-zip shards into one date-ordered file per symbol.

    Streams rather than sorting in memory: a liquid name like TLT runs to
    millions of rows across 17 years, which would be several GB if collected
    into a list first. Each shard is already emitted in ``asof`` order, so a
    k-way heap merge on that key produces a date-ordered file at constant
    memory. Ordering within a single day is arbitrary, which is fine because
    consumers index by ``option_symbol``.

    One day is buffered at a time so duplicate contract-days can be collapsed.
    Dating rows from their own ``Data Date`` makes this mandatory rather than
    merely tidy: a spliced session now lands on the same date as its real
    file, so the two copies would otherwise both be written.
    """
    import heapq
    from contextlib import ExitStack

    out_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    deduped_total = 0
    for symbol in symbols:
        shards = sorted(shard_root.glob(f"*/{symbol}.csv.gz"))
        if not shards:
            totals[symbol] = 0
            continue
        count = 0
        with ExitStack() as stack:
            readers = [
                csv.reader(stack.enter_context(gzip.open(s, "rt", newline="")))
                for s in shards
            ]
            out = stack.enter_context(
                gzip.open(out_dir / f"{symbol}.csv.gz", "wt", newline="")
            )
            writer = csv.writer(out)
            writer.writerow(_OUT_HEADER)

            day: str | None = None
            best: dict[str, list[str]] = {}
            for row in heapq.merge(*readers, key=lambda r: r[1]):
                if row[1] != day:
                    count += _flush_day(writer, best)
                    day = row[1]
                incumbent = best.get(row[0])
                if incumbent is None:
                    best[row[0]] = row
                else:
                    deduped_total += 1
                    if _row_quality(row) > _row_quality(incumbent):
                        best[row[0]] = row
            count += _flush_day(writer, best)
        totals[symbol] = count
    return totals, deduped_total


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="Greeks", help="directory holding the vendor zips")
    parser.add_argument("--out", default="backtest_cache/quotes")
    parser.add_argument("--shard-dir", default="backtest_cache/_quote_shards")
    parser.add_argument("--workers", type=int, default=max(1, min(8, mp.cpu_count() - 2)))
    parser.add_argument(
        "--limit-days",
        type=int,
        default=0,
        help="smoke-test on the first N sessions only (0 = all)",
    )
    parser.add_argument(
        "--keep-shards", action="store_true", help="do not delete shards after merging"
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="override the candidate pool (e.g. a point-in-time universe)",
    )
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        _log.error("vendor.source.missing", path=str(source))
        return 1

    symbols = sorted({s.upper() for s in args.symbols}) if args.symbols else sorted(CANDIDATE_POOL)
    _log.info("vendor.extract.start", symbols=len(symbols), source=str(source))

    index = index_members(source)
    if not index:
        _log.error("vendor.index.empty", path=str(source))
        return 1
    days = sorted(index)
    sessions = _load_sessions(
        Path("backtest_cache/vendor_sessions.json"),
        date(int(days[0][:4]), 1, 1),
        date(int(days[-1][:4]), 12, 31),
    )

    def _iso(day: str) -> str:
        return f"{day[:4]}-{day[4:6]}-{day[6:8]}"

    real = [d for d in days if _iso(d) in sessions]
    dropped = len(days) - len(real)
    if args.limit_days:
        real = real[: args.limit_days]
    _log.info(
        "vendor.calendar.filtered",
        indexed=len(days),
        sessions=len(real),
        dropped_non_sessions=dropped,
    )

    # Group work by zip so each worker owns one archive and writes its own shards.
    by_zip: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for day in real:
        for _, (zip_path, member) in sorted(index[day].items()):
            by_zip[zip_path].append((member, _iso(day)))

    shard_root = Path(args.shard_dir)
    if shard_root.exists():
        shutil.rmtree(shard_root)
    shard_root.mkdir(parents=True, exist_ok=True)

    session_list = sorted(sessions)
    tasks = [(z, m, symbols, str(shard_root), session_list) for z, m in sorted(by_zip.items())]
    _log.info("vendor.extract.dispatch", zips=len(tasks), workers=args.workers)

    kept_total = malformed_total = 0
    per_symbol: dict[str, int] = defaultdict(int)
    rejects_total: dict[str, int] = defaultdict(int)
    foreign_total: dict[str, int] = defaultdict(int)
    with mp.Pool(args.workers) as pool:
        for zip_path, kept, malformed, counts, rejects, foreign in pool.imap_unordered(
            _extract_one_zip, tasks
        ):
            kept_total += kept
            malformed_total += malformed
            for sym, n in counts.items():
                per_symbol[sym] += n
            for reason, n in rejects.items():
                rejects_total[reason] += n
            for pair, n in foreign.items():
                foreign_total[pair] += n
            _log.info(
                "vendor.zip.done",
                zip=Path(zip_path).name,
                rows=kept,
                malformed=malformed,
                running_total=kept_total,
            )

    totals, deduped = merge_shards(shard_root, Path(args.out), symbols)
    if not args.keep_shards:
        shutil.rmtree(shard_root, ignore_errors=True)

    _log.info("vendor.extract.rejects", deduped=deduped, **dict(rejects_total))
    for pair in sorted(foreign_total):
        host, guest = pair.split("->")
        _log.warning(
            "vendor.session.spliced", file_date=host, foreign_date=guest, rows=foreign_total[pair]
        )

    missing = [s for s in symbols if totals.get(s, 0) == 0]
    print("=" * 62)
    print(f"Sessions processed: {len(real)}  (dropped {dropped} non-session dates)")
    print(f"Rows written:       {sum(totals.values()):,}")
    print(f"Malformed skipped:  {malformed_total:,}")
    for reason in sorted(rejects_total):
        print(f"  {reason:<26} {rejects_total[reason]:>12,}")
    for pair in sorted(foreign_total):
        print(f"    spliced {pair:<22} {foreign_total[pair]:>10,}")
    print(f"  {'duplicate_contract_day':<26} {deduped:>12,}")
    print(f"Output:             {args.out}/")
    if missing:
        print(f"WARNING no rows for: {', '.join(missing)}")
    print("=" * 62)
    for sym in symbols:
        print(f"  {sym:<6} {totals.get(sym, 0):>12,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
