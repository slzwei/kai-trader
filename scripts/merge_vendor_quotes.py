"""Merge real vendor bid/ask into the backtest chains cache.

``backtest_cache/chains/{SYMBOL}.json`` holds ``{option_symbol: {date:
{open, high, low, close, volume}}}`` built from Alpaca option bars, which
carry no quotes. The backtest therefore estimated the bid-ask spread from
daily volume (``_SPREAD_FRAC_*`` in ``backtest.data.chains``). Measured
against the DiscountOptionData archive those buckets ran 3.4-4.5x too wide,
which understated every short-put credit and overstated every buy-to-close.

This adds ``bid`` and ``ask`` to each contract-day the archive covers.
``get_chain`` prefers them when present and falls back to the estimator
otherwise, so partial coverage degrades cleanly rather than failing.

Run ``scripts/extract_vendor_quotes.py`` first to build the quote store.

Usage:
    uv run python scripts/merge_vendor_quotes.py
    uv run python scripts/merge_vendor_quotes.py --symbols F BAC --dry-run
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from pathlib import Path
from typing import Final

from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)

_CHAINS_DIR: Final = Path("backtest_cache/chains")
_QUOTES_DIR: Final = Path("backtest_cache/quotes")


def merge_symbol(symbol: str, *, dry_run: bool) -> tuple[int, int, int]:
    """Return ``(contract_days_in_cache, matched, no_bid)`` for one symbol."""
    chain_path = _CHAINS_DIR / f"{symbol}.json"
    quote_path = _QUOTES_DIR / f"{symbol}.csv.gz"
    if not chain_path.exists() or not quote_path.exists():
        return 0, 0, 0

    with chain_path.open("r", encoding="utf-8") as handle:
        chains = json.load(handle)
    if not isinstance(chains, dict):
        return 0, 0, 0

    total = sum(len(v) for v in chains.values() if isinstance(v, dict))
    matched = 0
    no_bid = 0
    with gzip.open(quote_path, "rt", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return total, 0, 0
        i_sym = header.index("option_symbol")
        i_date = header.index("asof")
        i_bid = header.index("bid")
        i_ask = header.index("ask")
        for row in reader:
            days = chains.get(row[i_sym])
            if not isinstance(days, dict):
                continue
            record = days.get(row[i_date])
            if not isinstance(record, dict):
                continue
            bid = row[i_bid].strip()
            ask = row[i_ask].strip()
            if not bid or not ask:
                continue
            record["bid"] = bid
            record["ask"] = ask
            matched += 1
            try:
                if float(bid) <= 0:
                    no_bid += 1
            except ValueError:
                pass

    if not dry_run and matched:
        tmp = chain_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(chains, handle, sort_keys=True)
        tmp.replace(chain_path)
    return total, matched, no_bid


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = sorted(p.stem for p in _CHAINS_DIR.glob("*.json"))
    if not symbols:
        _log.error("merge.chains.empty", path=str(_CHAINS_DIR))
        return 1

    grand_total = grand_matched = grand_no_bid = 0
    print(f"{'symbol':<8}{'contract-days':>15}{'with real quote':>17}{'coverage':>10}{'no bid':>9}")
    print("-" * 59)
    for symbol in symbols:
        total, matched, no_bid = merge_symbol(symbol, dry_run=args.dry_run)
        if total == 0:
            print(f"{symbol:<8}{'(no chains or no quotes)':>15}")
            continue
        grand_total += total
        grand_matched += matched
        grand_no_bid += no_bid
        print(
            f"{symbol:<8}{total:>15,}{matched:>17,}"
            f"{matched / total * 100:>9.1f}%{no_bid:>9,}"
        )
    print("-" * 59)
    if grand_total:
        print(
            f"{'TOTAL':<8}{grand_total:>15,}{grand_matched:>17,}"
            f"{grand_matched / grand_total * 100:>9.1f}%{grand_no_bid:>9,}"
        )
    if args.dry_run:
        print("\n(dry run: nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
