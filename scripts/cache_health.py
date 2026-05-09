"""Quick health check on the backtest cache.

Reports per-symbol cache coverage:
* contracts cache size
* chains cache size
* date range (earliest and latest bar)
* contract count

Usage::

    uv run python scripts/cache_health.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


def _summarise_chain(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"error": "unreadable"}
    if not isinstance(data, dict):
        return {"error": "not a dict"}
    contract_count = len(data)
    all_dates: set[str] = set()
    for sym, bars in data.items():
        if not isinstance(bars, dict):
            continue
        all_dates.update(bars.keys())
    if not all_dates:
        return {
            "contracts": str(contract_count),
            "dates": "0",
            "first": "(none)",
            "last": "(none)",
        }
    sorted_dates = sorted(all_dates)
    return {
        "contracts": str(contract_count),
        "dates": str(len(sorted_dates)),
        "first": sorted_dates[0],
        "last": sorted_dates[-1],
    }


def main() -> int:
    chains_dir = Path("backtest_cache/chains")
    contracts_dir = Path("backtest_cache/contracts")
    if not chains_dir.exists():
        print("no chains cache", file=sys.stderr)
        return 1

    rows: list[dict[str, str]] = []
    for path in sorted(chains_dir.glob("*.json")):
        sym = path.stem
        size_kb = path.stat().st_size // 1024
        contracts_path = contracts_dir / f"{sym}.json"
        contracts_size_kb = contracts_path.stat().st_size // 1024 if contracts_path.exists() else 0
        info = _summarise_chain(path)
        rows.append({
            "symbol": sym,
            "contracts_kb": str(contracts_size_kb),
            "chains_kb": str(size_kb),
            **info,
        })

    headers = ["symbol", "contracts_kb", "chains_kb", "contracts", "dates", "first", "last"]
    print(" | ".join(f"{h:>14s}" for h in headers))
    print(" | ".join("-" * 14 for _ in headers))
    for r in rows:
        print(" | ".join(f"{r.get(h, ''):>14s}" for h in headers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
