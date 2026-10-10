"""Rebuild the backtest contracts + chains caches from the vendor quote store.

Alpaca's historical options API starts in February 2024, which capped every
backtest at ~2.5 years of mostly-rising tape. The DiscountOptionData archive
reaches back to 2005, so this rebuilds both option caches from
``backtest_cache/quotes/`` instead and unlocks the 2022 bear market.

Deliberately rebuilds the WHOLE requested range from the vendor rather than
splicing vendor history onto the Alpaca-derived 2024+ cache. A source change
part-way through a sample is indistinguishable from a regime change in the
results, and the vendor has already been validated against Alpaca on the
overlap (99.6% of contract-days matched; underlying closes agreed to 1-2c).

Field mapping, and why:

* ``bid``/``ask`` are the real quotes. These are what ``_build_contract``
  now uses for fills and for pricing the Greeks off the quote midpoint.
* ``close`` is the last trade when the contract traded, else the quote
  midpoint. Alpaca-derived bars always had a real trade here, but a quoted
  contract that simply did not trade is still tradable, and dropping it
  would bias the sample toward liquid strikes.
* ``high``/``low`` are set to ask/bid. They feed only the estimator's
  range-based spread floor, which no longer runs when real quotes exist.

Usage:
    uv run python scripts/build_chains_from_vendor.py --start 2021-01-01 --end 2026-08-27
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Final

from kai_trader.backtest.cli import _DEFAULT_SLEEVE_CONFIG
from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)

_QUOTES_DIR: Final = Path("backtest_cache/quotes")
_CHAINS_DIR: Final = Path("backtest_cache/chains")
_CONTRACTS_DIR: Final = Path("backtest_cache/contracts")


def _universe(sleeve_config: Path | None) -> list[str]:
    if sleeve_config is not None and sleeve_config.exists():
        rows = json.loads(sleeve_config.read_text(encoding="utf-8"))
    else:
        rows = _DEFAULT_SLEEVE_CONFIG
    out: set[str] = set()
    for row in rows:
        if row.get("enabled"):
            out.update(row.get("symbol_whitelist") or [])
    return sorted(out)


def build_symbol(symbol: str, start: str, end: str) -> tuple[int, int, int]:
    """Return ``(contracts, contract_days, days)`` written for one symbol."""
    quote_path = _QUOTES_DIR / f"{symbol}.csv.gz"
    if not quote_path.exists():
        return 0, 0, 0

    chains: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    meta: dict[str, dict[str, str]] = {}
    days: set[str] = set()

    with gzip.open(quote_path, "rt", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return 0, 0, 0
        idx = {name: i for i, name in enumerate(header)}
        for row in reader:
            asof = row[idx["asof"]]
            if asof < start or asof > end:
                continue
            bid_raw = row[idx["bid"]].strip()
            ask_raw = row[idx["ask"]].strip()
            if not bid_raw or not ask_raw:
                continue
            try:
                bid = float(bid_raw)
                ask = float(ask_raw)
            except ValueError:
                continue
            # No bid means it cannot be sold; the strategy only ever opens
            # short premium, so such a contract is not actionable.
            if bid <= 0 or ask < bid:
                continue
            last_raw = row[idx["last"]].strip()
            try:
                last = float(last_raw)
            except ValueError:
                last = 0.0
            close = last if last > 0 else (bid + ask) / 2

            option_symbol = row[idx["option_symbol"]]
            chains[option_symbol][asof] = {
                "open": f"{close:.4f}",
                "high": ask_raw,
                "low": bid_raw,
                "close": f"{close:.4f}",
                "volume": row[idx["volume"]].strip() or "0",
                "bid": bid_raw,
                "ask": ask_raw,
            }
            days.add(asof)
            if option_symbol not in meta:
                meta[option_symbol] = {
                    "symbol": option_symbol,
                    "underlying": symbol,
                    "option_type": row[idx["right"]],
                    "strike": row[idx["strike"]],
                    "expiration": row[idx["expiration"]],
                }

    if not chains:
        return 0, 0, 0

    _CHAINS_DIR.mkdir(parents=True, exist_ok=True)
    _CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    chain_tmp = _CHAINS_DIR / f"{symbol}.json.tmp"
    with chain_tmp.open("w", encoding="utf-8") as handle:
        json.dump(chains, handle, sort_keys=True)
    chain_tmp.replace(_CHAINS_DIR / f"{symbol}.json")

    meta_tmp = _CONTRACTS_DIR / f"{symbol}.json.tmp"
    with meta_tmp.open("w", encoding="utf-8") as handle:
        json.dump(sorted(meta.values(), key=lambda m: m["symbol"]), handle, sort_keys=True)
    meta_tmp.replace(_CONTRACTS_DIR / f"{symbol}.json")

    return len(meta), sum(len(v) for v in chains.values()), len(days)


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--sleeve-config", default=None)
    parser.add_argument(
        "--backup-suffix",
        default="alpaca_era",
        help="copy the existing caches aside before overwriting (empty to skip)",
    )
    args = parser.parse_args(argv)

    date.fromisoformat(args.start)
    date.fromisoformat(args.end)

    symbols = _universe(Path(args.sleeve_config) if args.sleeve_config else None)
    if not symbols:
        _log.error("vendor.build.no_symbols")
        return 1

    if args.backup_suffix:
        for src in (_CHAINS_DIR, _CONTRACTS_DIR):
            dest = src.with_name(f"{src.name}_backup_{args.backup_suffix}")
            if src.exists() and not dest.exists():
                shutil.copytree(src, dest)
                _log.info("vendor.build.backed_up", src=str(src), dest=str(dest))

    print(f"{'symbol':<8}{'contracts':>12}{'contract-days':>16}{'sessions':>10}")
    print("-" * 46)
    for symbol in symbols:
        contracts, contract_days, days = build_symbol(symbol, args.start, args.end)
        if contracts == 0:
            print(f"{symbol:<8}{'(no vendor quotes)':>12}")
            continue
        print(f"{symbol:<8}{contracts:>12,}{contract_days:>16,}{days:>10,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
