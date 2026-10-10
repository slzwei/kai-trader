"""Audit the vendor options archive for delivery defects.

Run this after every new delivery. Each defect it looks for has already
occurred at least once in the DiscountOptionData archive, and every one of
them is silent: the extract completes, the cache looks plausible, and the
damage only shows up as an unexplained result months later.

Structural checks (default, seconds, reads only the zip central directories):

* Sessions with no file at all. 2024-12-31 is the known case.
* Files dated to non-sessions. Market holidays ship a stale copy of the prior
  session under the holiday's date, and one Saturday file exists.
* Members that appear in more than one zip with differing bytes. Deduplication
  is by basename, so a genuine byte conflict would silently pick a winner.
* Days whose part count falls below the era's norm, which can mean a dropped
  ``OData2``/``OData3`` half.

Deep checks (``--deep``, streams every row, tens of minutes):

* TRUNCATION. 2018-10-12 begins at ticker ALT1 and silently loses 158 symbols
  from A to ALSN, including AAPL. It is undetectable by file size (136.6 MB
  against 141.1 and 135.7 either side) and nearly undetectable by symbol count
  (4,340 against ~4,500, a 3.6% dip). What does catch it: the absentees form
  one contiguous alphabetical block. Compare each session against the symbols
  present on BOTH neighbours, then measure the longest contiguous run within
  that sorted expected universe. 2018-10-12 scores 98.8%; no other session in
  21 years exceeds 44%.
* SPLICED SESSIONS. Five files hold a second, unrelated session beside the one
  they are named for, revealed only by the ``Data Date`` column disagreeing
  with the filename.
* Expired contracts, crossed quotes and negative prices, which the extractor
  drops but which are worth knowing the size of.

Usage:
    uv run python scripts/audit_vendor_archive.py --source Greeks
    uv run python scripts/audit_vendor_archive.py --source Greeks --deep
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Final, NamedTuple

from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)

_MEMBER_RE: Final = re.compile(r"Greek_(\d{8})_OData(\d*)\.csv$")
_SESSION_CACHE: Final = Path("backtest_cache/vendor_sessions.json")

# A day whose absentees are this concentrated in one alphabetical run is a
# truncated export, not a quiet tape. The worst genuine session measures 44%.
_TRUNCATION_BLOCK_FRAC: Final = 0.60
_MIN_ABSENTEES: Final = 15


class DayScan(NamedTuple):
    """One session's worth of deep-scan results."""

    day: str
    symbols: frozenset[str]
    rows: int
    foreign_date_rows: int
    foreign_dates: tuple[str, ...]
    expired_rows: int
    crossed_rows: int
    negative_rows: int


def _iso(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _norm(raw: bytes) -> bytes:
    """ISO passthrough; two archive days ship ``M/D/YYYY``."""
    if b"/" not in raw:
        return raw
    try:
        month, dom, year = raw.split(b"/")
    except ValueError:
        return raw
    return year + b"-" + month.rjust(2, b"0") + b"-" + dom.rjust(2, b"0")


def index_members(source: Path) -> tuple[dict[str, dict[str, tuple[str, str]]], list[str]]:
    """Map ``YYYYMMDD -> {basename: (zip, member)}`` and report byte conflicts."""
    index: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    fingerprints: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for zip_path in sorted(source.glob("**/*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                match = _MEMBER_RE.search(info.filename)
                if match is None:
                    continue
                base = info.filename.rsplit("/", 1)[-1]
                index[match.group(1)][base] = (str(zip_path), info.filename)
                fingerprints[f"{match.group(1)}|{base}"].add((info.file_size, info.CRC))
    conflicts = sorted(k for k, v in fingerprints.items() if len(v) > 1)
    return index, conflicts


def _load_sessions() -> set[str]:
    if not _SESSION_CACHE.exists():
        _log.error("audit.sessions.missing", path=str(_SESSION_CACHE))
        return set()
    stored = json.loads(_SESSION_CACHE.read_text())
    return set(stored["sessions"] if isinstance(stored, dict) else stored)


def scan_day(task: tuple[str, list[tuple[str, str]]]) -> DayScan:
    """Stream one session's members and collect everything the deep pass needs."""
    day, members = task
    asof = _iso(day).encode()
    symbols: set[str] = set()
    seen_dates: Counter[bytes] = Counter()
    rows = foreign = expired = crossed = negative = 0
    for zip_path, member in members:
        with zipfile.ZipFile(zip_path) as archive:
            with archive.open(member) as handle:
                handle.readline()  # header
                for line in handle:
                    parts = line.split(b",")
                    if len(parts) != 20:
                        continue
                    symbol = parts[1].strip().upper()
                    if symbol == b"SYMBOL":
                        continue
                    rows += 1
                    symbols.add(symbol.decode("utf-8", "replace"))
                    row_date = _norm(parts[19].strip())
                    if row_date != asof:
                        foreign += 1
                        seen_dates[row_date] += 1
                    if _norm(parts[2]) < row_date:
                        expired += 1
                    try:
                        bid, ask = float(parts[5] or 0), float(parts[3] or 0)
                    except ValueError:
                        continue
                    if bid < 0 or ask < 0:
                        negative += 1
                    elif bid > 0 and ask > 0 and bid > ask:
                        crossed += 1
    return DayScan(
        day=_iso(day),
        symbols=frozenset(symbols),
        rows=rows,
        foreign_date_rows=foreign,
        foreign_dates=tuple(d.decode("utf-8", "replace") for d, _ in seen_dates.most_common(3)),
        expired_rows=expired,
        crossed_rows=crossed,
        negative_rows=negative,
    )


def find_truncated(scans: list[DayScan]) -> list[tuple[str, int, int, float, list[str]]]:
    """Flag sessions whose absentees form one contiguous alphabetical block.

    A symbol is only expected on a day if it traded on BOTH the previous and
    the following session, which keeps ordinary listings and delistings out of
    the signal.
    """
    flagged: list[tuple[str, int, int, float, list[str]]] = []
    for i in range(1, len(scans) - 1):
        cur, prev, nxt = scans[i], scans[i - 1], scans[i + 1]
        expected = sorted(prev.symbols & nxt.symbols)
        missing = [j for j, sym in enumerate(expected) if sym not in cur.symbols]
        if len(missing) < _MIN_ABSENTEES:
            continue
        best = run = 1
        for k in range(1, len(missing)):
            run = run + 1 if missing[k] == missing[k - 1] + 1 else 1
            best = max(best, run)
        frac = best / len(missing)
        if frac >= _TRUNCATION_BLOCK_FRAC:
            names = [expected[j] for j in missing[:8]]
            flagged.append((cur.day, len(missing), best, frac, names))
    return flagged


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="Greeks", help="directory holding the vendor zips")
    parser.add_argument("--deep", action="store_true", help="stream every row (slow)")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        _log.error("audit.source.missing", path=str(source))
        return 1

    index, conflicts = index_members(source)
    sessions = _load_sessions()
    if not index or not sessions:
        return 1

    dated = sorted(index)
    present = {_iso(d) for d in dated}
    lo, hi = min(present), max(present)
    window = sorted(s for s in sessions if lo <= s <= hi)
    missing_sessions = [s for s in window if s not in present]
    non_sessions = sorted(d for d in present if d not in sessions)

    failures = 0
    print("=" * 68)
    print(f"VENDOR ARCHIVE AUDIT   {lo} .. {hi}")
    print("=" * 68)
    print(f"dated files          {len(dated):,}")
    print(f"sessions in window   {len(window):,}")

    print(f"\nmissing sessions     {len(missing_sessions)}")
    for day in missing_sessions:
        print(f"   FAIL no file for session {day}")
    failures += len(missing_sessions)

    print(f"\nnon-session files    {len(non_sessions)}  (expected: holidays + 2020-08-01)")
    print(f"byte-conflicting dup {len(conflicts)}")
    for key in conflicts:
        print(f"   FAIL same member differs between zips: {key}")
    failures += len(conflicts)

    parts = Counter({_iso(d): len(index[d]) for d in dated if _iso(d) in sessions})
    by_year: dict[str, Counter[int]] = defaultdict(Counter)
    for day, n in parts.items():
        by_year[day[:4]][n] += 1
    thin = [
        (day, n)
        for day, n in sorted(parts.items())
        if n < by_year[day[:4]].most_common(1)[0][0]
    ]
    print(f"\ndays below the year's modal part count: {len(thin)}")
    for day, n in thin[:20]:
        print(f"   note {day} has {n} part(s), year mode {by_year[day[:4]].most_common(1)[0][0]}")

    if not args.deep:
        print("\n(skipped truncation and row checks; re-run with --deep)")
        print("=" * 68)
        return 1 if failures else 0

    tasks = [
        (day, [index[day][b] for b in sorted(index[day])])
        for day in dated
        if _iso(day) in sessions
    ]
    _log.info("audit.deep.start", sessions=len(tasks), workers=args.workers)
    scans: list[DayScan] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, scan in enumerate(pool.map(scan_day, tasks, chunksize=4)):
            scans.append(scan)
            if i % 500 == 0:
                _log.info("audit.deep.progress", done=i, total=len(tasks))
    scans.sort(key=lambda s: s.day)

    truncated = find_truncated(scans)
    print(f"\nTRUNCATED SESSIONS   {len(truncated)}")
    for day, nmiss, block, frac, names in truncated:
        print(
            f"   FAIL {day} lost {nmiss} symbols, {block} in one alphabetical "
            f"block ({frac:.1%}): {', '.join(names)} ..."
        )
    failures += len(truncated)

    spliced = [s for s in scans if s.foreign_date_rows]
    print(f"\nSPLICED SESSIONS     {len(spliced)}")
    for scan in spliced:
        print(
            f"   FAIL {scan.day} carries {scan.foreign_date_rows:,} rows dated "
            f"{', '.join(scan.foreign_dates)}"
        )
    failures += len(spliced)

    print("\nROW HYGIENE (extractor drops these; sizes are informational)")
    print(f"   expired contracts {sum(s.expired_rows for s in scans):,}")
    print(f"   crossed quotes    {sum(s.crossed_rows for s in scans):,}")
    print(f"   negative prices   {sum(s.negative_rows for s in scans):,}")
    print(f"   rows scanned      {sum(s.rows for s in scans):,}")

    print("=" * 68)
    print("RESULT: " + (f"{failures} defect(s) need attention" if failures else "clean"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
