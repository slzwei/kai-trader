"""Build the cleaned, distributable options dataset from the vendor archive.

Unlike ``extract_vendor_quotes.py``, which carves the ~41 names the trading
system happens to trade out into gzipped CSV, this covers EVERY symbol in the
archive and targets a dataset other people consume. That changes two things.

Format. Roughly 4.4 billion rows survive cleaning. As gzipped CSV keyed by
symbol that is ~120 GB and needs ~241 GB of working space, which does not fit
on a 64 GB disk, and it opens ~13,800 concurrent compressors. Parquet
partitioned by year lands in the 25-40 GB range instead: the heavy columns
(``option_symbol``, ``asof``, ``expiration``, ``right``) are enormously
repetitive and dictionary-encode almost to nothing. It is also what a buyer
expects, and partition pruning means reading one year does not touch the rest.

Correctness. Every defect the archive audit turned up is repaired here, and
each one is silent if left alone. See ``scripts/audit_vendor_archive.py`` for
the detector that finds them and the full evidence:

* SPLICED SESSIONS. Five files hold a second, unrelated session beside the one
  they are named for: 2015-09-01 also holds 2015-10-16, 2009-01-02 holds
  2009-01-29, 2010-10-01 holds 2010-10-14, 2016-10-03 holds 2016-10-07, and
  2020-05-01 holds 2020-05-04. Dating a row by its filename therefore back-dates
  real quotes by up to 45 days, which is lookahead: 2015-09-01 would ship
  October's prices. Rows are dated from their own ``Data Date`` column, and a
  row disagreeing with its file is dropped. That is lossless because all five
  spliced sessions also ship as their own correctly named files. Re-dating in
  place instead is a trap: the guest rows leave the member out of date order.
* EXPIRED CONTRACTS. ~51M rows quote a contract that had already expired,
  mostly Nov 2019 to Jun 2020 and again in 2026. They repeat the contract's
  last live quote and are not tradable. Dropped.
* DUPLICATE CONTRACT-DAYS. ~2.9M rows, 1.0M of which disagree between copies,
  typically one carrying the quote with zeroed volume and open interest and
  the other carrying the trade counts. The richer copy wins.
* NON-SESSION FILES. Market holidays ship a stale copy of the prior session
  under the holiday's own date, plus one Saturday file. Filtered against a real
  exchange calendar.
* CROSSED AND NEGATIVE QUOTES. Dropped rather than passed through.

Two defects cannot be repaired from the archive and remain in the output as
holes, deliberately, because inventing values would be worse: 2024-12-31 was
never delivered, and 2018-10-12 is truncated at ticker ALT1, losing 158
symbols from A to ALSN including AAPL. Both need a vendor re-delivery or an
outside source such as Cboe DataShop, whose history reaches back to 2012.

Usage:
    uv sync --extra data
    uv run python scripts/build_vendor_dataset.py --source Greeks
    uv run python scripts/build_vendor_dataset.py --source Greeks --years 2024 2025
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)

_MEMBER_RE: Final = re.compile(r"Greek_(\d{8})_OData(\d*)\.csv$")
_SESSION_CACHE: Final = Path("backtest_cache/vendor_sessions.json")

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

# ``underlying_symbol`` and ``right`` repeat across millions of rows, so both
# are dictionary-encoded. Prices stay float64 because the vendor quotes
# fractions of a cent on some pre-decimalisation contracts.
_SCHEMA: Final = pa.schema(
    [
        pa.field("underlying_symbol", pa.dictionary(pa.int32(), pa.string())),
        pa.field("option_symbol", pa.string()),
        pa.field("asof", pa.date32()),
        pa.field("expiration", pa.date32()),
        pa.field("strike", pa.float64()),
        pa.field("right", pa.dictionary(pa.int32(), pa.string())),
        pa.field("bid", pa.float64()),
        pa.field("ask", pa.float64()),
        pa.field("bid_size", pa.int32()),
        pa.field("ask_size", pa.int32()),
        pa.field("last", pa.float64()),
        pa.field("volume", pa.int64()),
        pa.field("open_interest", pa.int64()),
        pa.field("underlying", pa.float64()),
        # Greeks and IV are vendor-computed analytics quoted to four decimals.
        # float32 carries ~7 significant digits, so it is lossless at that
        # precision and roughly halves what is otherwise over half the file.
        # Prices stay float64.
        pa.field("iv", pa.float32()),
        pa.field("delta", pa.float32()),
        pa.field("gamma", pa.float32()),
        pa.field("vega", pa.float32()),
        pa.field("rho", pa.float32()),
        pa.field("theta", pa.float32()),
    ]
)

_COLUMNS: Final = [f.name for f in _SCHEMA]


def _norm_date(value: str) -> str | None:
    """Normalise a vendor date to ISO. Two archive days ship ``M/D/YYYY``."""
    text = value.strip()
    if "-" in text:
        parts = text.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
        return None
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return None


def _occ_symbol(root: str, expiration: str, put_call: str, strike: float) -> str:
    """OCC symbol: ``F`` + ``2024-03-15`` + put + 12.82 -> ``F240315P00012820``."""
    yy, mm, dd = expiration[2:4], expiration[5:7], expiration[8:10]
    cp = "P" if put_call[:1].lower() == "p" else "C"
    return f"{root}{yy}{mm}{dd}{cp}{round(strike * 1000):08d}"


def _f(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _i(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def _to_array(column: tuple[Any, ...], target: pa.DataType) -> pa.Array:
    """Build one Arrow column, casting the types Arrow will not infer.

    Dates arrive as ISO strings and dictionary columns as plain strings;
    neither can be handed straight to ``pa.array`` with the target type, so
    both are built as strings and converted.
    """
    if pa.types.is_date(target):
        return pa.array(column, type=pa.string()).cast(target)
    if pa.types.is_dictionary(target):
        return pa.array(column, type=pa.string()).dictionary_encode().cast(target)
    return pa.array(column, type=target)


def _quality(row: list[Any]) -> tuple[float, float, float]:
    """Rank two copies of one contract-day; the richer copy wins."""
    return (row[12] or 0.0, row[11] or 0.0, row[6] or 0.0)


def _index(source: Path) -> dict[str, dict[str, tuple[str, str]]]:
    index: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for zip_path in sorted(source.glob("**/*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                match = _MEMBER_RE.search(member)
                if match is not None:
                    index[match.group(1)][member.rsplit("/", 1)[-1]] = (str(zip_path), member)
    return index


def _build_one_zip(
    task: tuple[str, list[tuple[str, str]], str, list[str], str, int],
) -> dict[str, int]:
    """Worker: clean one zip's members and append them to that year's Parquet."""
    zip_path, members, out_root, sessions_list, compression, level = task
    sessions = set(sessions_list)
    stem = Path(zip_path).stem
    writers: dict[str, pq.ParquetWriter] = {}
    stats: dict[str, int] = defaultdict(int)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member, file_asof in members:
                # One contract-day appears in exactly one member: the spliced
                # rows are dropped below, and multi-part days split the
                # alphabet, so per-member deduplication is per-day.
                rows: dict[str, list[Any]] = {}
                with archive.open(member) as raw:
                    raw.readline()
                    for line in raw:
                        parts = line.split(b",")
                        if len(parts) != _N_COLS:
                            stats["malformed"] += 1
                            continue
                        row = next(csv.reader([line.decode("utf-8", "replace")]))
                        symbol = row[_C_SYMBOL].strip().upper()
                        if not symbol or symbol == "SYMBOL":
                            continue
                        stats["read"] += 1

                        asof = _norm_date(row[_C_DATA_DATE])
                        if asof is None:
                            stats["malformed"] += 1
                            continue
                        if asof != file_asof:
                            stats["spliced_foreign_date"] += 1
                            continue
                        if asof not in sessions:
                            stats["non_session"] += 1
                            continue
                        expiration = _norm_date(row[_C_EXPIRATION])
                        if expiration is None:
                            stats["malformed"] += 1
                            continue
                        if expiration < asof:
                            stats["expired"] += 1
                            continue
                        strike = _f(row[_C_STRIKE])
                        if strike is None or strike < 0:
                            stats["malformed"] += 1
                            continue
                        bid, ask = _f(row[_C_BID]), _f(row[_C_ASK])
                        if (bid is not None and bid < 0) or (ask is not None and ask < 0):
                            stats["negative"] += 1
                            continue
                        if bid and ask and bid > ask:
                            stats["crossed"] += 1
                            continue

                        occ = _occ_symbol(symbol, expiration, row[_C_PUT_CALL], strike)
                        built: list[Any] = [
                            symbol,
                            occ,
                            asof,
                            expiration,
                            strike,
                            "put" if row[_C_PUT_CALL][:1].lower() == "p" else "call",
                            bid,
                            ask,
                            _i(row[_C_BID_SIZE]),
                            _i(row[_C_ASK_SIZE]),
                            _f(row[_C_LAST]),
                            _i(row[_C_VOLUME]),
                            _i(row[_C_OPEN_INTEREST]),
                            _f(row[_C_UNDERLYING]),
                            _f(row[_C_IV]),
                            _f(row[_C_DELTA]),
                            _f(row[_C_GAMMA]),
                            _f(row[_C_VEGA]),
                            _f(row[_C_RHO]),
                            _f(row[_C_THETA]),
                        ]
                        incumbent = rows.get(occ)
                        if incumbent is None:
                            rows[occ] = built
                        else:
                            stats["duplicate"] += 1
                            if _quality(built) > _quality(incumbent):
                                rows[occ] = built

                if not rows:
                    continue
                year = file_asof[:4]
                writer = writers.get(year)
                if writer is None:
                    part = Path(out_root) / f"year={year}"
                    part.mkdir(parents=True, exist_ok=True)
                    writer = pq.ParquetWriter(
                        part / f"part-{stem}.parquet",
                        _SCHEMA,
                        compression=compression,
                        compression_level=level if compression == "zstd" else None,
                        use_dictionary=True,
                    )
                    writers[year] = writer
                ordered = list(rows.values())
                columns = list(zip(*ordered, strict=True))
                writer.write_table(
                    pa.Table.from_arrays(
                        [_to_array(col, _SCHEMA.field(i).type) for i, col in enumerate(columns)],
                        schema=_SCHEMA,
                    )
                )
                stats["written"] += len(ordered)
    finally:
        for writer in writers.values():
            writer.close()
    return dict(stats)


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="Greeks")
    parser.add_argument("--out", default="dataset/options_eod")
    parser.add_argument("--workers", type=int, default=max(1, min(10, mp.cpu_count() - 4)))
    parser.add_argument("--compression", default="zstd", choices=["zstd", "snappy", "gzip"])
    parser.add_argument("--level", type=int, default=9, help="zstd compression level")
    parser.add_argument("--years", nargs="*", default=None, help="restrict to these years")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        _log.error("dataset.source.missing", path=str(source))
        return 1
    if not _SESSION_CACHE.exists():
        _log.error("dataset.sessions.missing", path=str(_SESSION_CACHE))
        return 1
    stored = json.loads(_SESSION_CACHE.read_text())
    sessions = sorted(stored["sessions"] if isinstance(stored, dict) else stored)

    index = _index(source)
    if not index:
        _log.error("dataset.index.empty", path=str(source))
        return 1

    def _iso(day: str) -> str:
        return f"{day[:4]}-{day[4:6]}-{day[6:8]}"

    session_set = set(sessions)
    wanted = set(args.years) if args.years else None
    by_zip: dict[str, list[tuple[str, str]]] = defaultdict(list)
    days = 0
    for day in sorted(index):
        iso = _iso(day)
        if iso not in session_set:
            continue
        if wanted and iso[:4] not in wanted:
            continue
        days += 1
        for _, (zip_path, member) in sorted(index[day].items()):
            by_zip[zip_path].append((member, iso))

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    tasks = [
        (z, m, str(out_root), sessions, args.compression, args.level)
        for z, m in sorted(by_zip.items())
    ]
    _log.info(
        "dataset.build.start", zips=len(tasks), sessions=days, workers=args.workers, out=args.out
    )

    totals: dict[str, int] = defaultdict(int)
    with mp.Pool(args.workers) as pool:
        for i, stats in enumerate(pool.imap_unordered(_build_one_zip, tasks), start=1):
            for key, value in stats.items():
                totals[key] += value
            _log.info("dataset.zip.done", done=i, of=len(tasks), written=totals["written"])

    size = sum(p.stat().st_size for p in out_root.rglob("*.parquet"))
    print("=" * 62)
    print(f"Sessions:      {days:,}")
    print(f"Rows read:     {totals['read']:,}")
    print(f"Rows written:  {totals['written']:,}")
    for key in ("spliced_foreign_date", "expired", "duplicate", "crossed", "negative",
                "non_session", "malformed"):
        print(f"  dropped {key:<22} {totals.get(key, 0):>14,}")
    print(f"Output:        {out_root}/  ({size / 1024 ** 3:.1f} GB)")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
