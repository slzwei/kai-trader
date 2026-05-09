"""Historical option chain fetcher matching the live ``OptionContract`` shape.

The strategy's pure intent builders (``candidates.build_intents_with_diagnostics``,
``rolls.find_roll_candidate``, ``profit_take.scan_for_profit_takes``,
``covered_calls.build_cc_intents``) all consume a ``ChainFetcher``:

    ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]

In production this is bound to ``broker.options_data.get_chain``, which
calls Alpaca's snapshot endpoint and returns current-time data with
exchange-published Greeks. In the backtest we bind it to a closure that
captures ``asof_dt`` and produces the same ``OptionContract`` shape from
historical bars + reconstructed Greeks.

Data flow at warm-up:

  1. List every contract for an underlying with expiration in the
     backtest window via Alpaca's ``GetOptionContractsRequest``.
  2. For each contract, fetch its daily bar series across the full
     window in one ``OptionBarsRequest`` (batched 50 symbols per
     request, since alpaca-py accepts ``symbol_or_symbols: list``).
  3. Cache the bars per (underlying, contract_symbol) to JSON.

Data flow at fetch time:

  1. Load cache for the underlying.
  2. Filter contracts whose expiration is in
     ``(asof_dt, asof_dt + max_dte_lookahead]`` and whose strike is
     within ``±strike_band_pct`` of the underlying close on ``asof_dt``.
  3. For each remaining contract:
       * use the bar's close on ``asof_dt`` as mid
       * estimate ``bid = close * (1 - spread_frac)`` and
         ``ask = close * (1 + spread_frac)``; ``spread_frac`` keys off
         the bar's volume so liquid names get tight spreads and
         illiquid ones get wider ones
       * reconstruct Greeks via Black-Scholes from
         (mid, underlying close, strike, DTE in years, risk-free rate)
  4. Return the result as ``list[OptionContract]``.

Asof-bounded reads are enforced by hard asserts. ``LeakageError`` is
raised if any returned contract has bar.timestamp > asof_dt or if the
underlying close used for Greeks reconstruction post-dates asof_dt.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from alpaca.trading.requests import GetOptionContractsRequest

from kai_trader.backtest.data import bars, rates
from kai_trader.backtest.data.greeks import reconstruct_greeks
from kai_trader.backtest.data.rates import LeakageError
from kai_trader.broker.options_data import OptionContract, parse_occ_symbol
from kai_trader.config import Settings, get_settings
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_DIR: Final[Path] = Path("backtest_cache/chains")
_CONTRACTS_DIR: Final[Path] = Path("backtest_cache/contracts")

# How far in the future from any asof we consider expirations. The
# strategy's max DTE is 14 days for index_core and 21 for stable_largecap;
# 60 gives plenty of headroom while keeping the contract universe finite.
_MAX_DTE_LOOKAHEAD_DAYS: Final[int] = 60

# Symmetric strike band around the underlying close. The 0.20-0.30 delta
# puts the strategy targets land in the 5-12% OTM range; ±25% gives
# enough cushion for ITM rolls and high-volatility edge cases without
# pulling far-OTM tail strikes that nobody trades.
_STRIKE_BAND_PCT: Final[Decimal] = Decimal("0.25")

# Bid/ask estimation buckets. Spread fraction is half the bid-ask spread
# divided by mid, so bid = mid * (1 - frac) and ask = mid * (1 + frac).
# Volume-keyed because option liquidity dominates spread width.
#
# Calibrated 2026-05-08 against real Alpaca historical option trade prints
# for the active sleeve (MARA, RIOT, RIVN, SOFI, SLV, HOOD on weekly OTM
# puts during 2024-03). Inter-quartile range of intraday trade prices
# divided by median came in at 7-54% across the sample (mean ~23%) — the
# previous 2.5-10% bucket values understated real spreads by 5-10x and
# inflated backtest fills. New bucket values are calibrated to match the
# observed IQR data: half-spread = ~half of the typical IQR-spread.
_SPREAD_FRAC_HIGH_VOLUME = Decimal("0.10")   # >1000 contracts traded that day; ~20% total spread
_SPREAD_FRAC_MED_VOLUME = Decimal("0.15")    # 100 to 1000; ~30% total
_SPREAD_FRAC_LOW_VOLUME = Decimal("0.22")    # <100; ~44% total
_SPREAD_FRAC_ZERO_VOLUME = Decimal("0.30")   # untraded that day; ~60% total

# Floor below which an option is treated as economically untradable
# (the bid would round to zero and no fill could be modeled).
_MIN_OPTION_PRICE = Decimal("0.05")

_options_client: OptionHistoricalDataClient | None = None
_trading_client: TradingClient | None = None


def _get_options_client(settings: Settings | None = None) -> OptionHistoricalDataClient:
    global _options_client
    if _options_client is None:
        cfg = settings or get_settings()
        _options_client = OptionHistoricalDataClient(
            api_key=cfg.effective_alpaca_api_key,
            secret_key=cfg.effective_alpaca_secret_key,
        )
    return _options_client


def _get_trading_client(settings: Settings | None = None) -> TradingClient:
    global _trading_client
    if _trading_client is None:
        cfg = settings or get_settings()
        _trading_client = TradingClient(
            api_key=cfg.effective_alpaca_api_key,
            secret_key=cfg.effective_alpaca_secret_key,
            paper=cfg.alpaca_paper,
        )
    return _trading_client


def reset_clients() -> None:
    """Drop cached Alpaca clients. Tests use this to swap stubs."""
    global _options_client, _trading_client
    _options_client = None
    _trading_client = None


@dataclass(frozen=True)
class ContractMeta:
    """Static contract metadata. Cached per underlying once and reused."""

    symbol: str
    underlying: str
    option_type: str
    strike: Decimal
    expiration: date


@dataclass(frozen=True)
class OptionDailyBar:
    """One day of OHLCV for a single contract."""

    asof: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _contracts_path(underlying: str) -> Path:
    return _CONTRACTS_DIR / f"{underlying.upper()}.json"


# In-process LRU cache for chain JSONs. The 50MB+ files would otherwise
# be reparsed on every tick — hot-path mark-to-market and roll-eval both
# call ``get_chain`` repeatedly. Cache keyed on path.mtime_ns so the
# tests that swap fixtures still see fresh state.
_contracts_cache_mem: dict[str, tuple[int, list[dict[str, Any]]]] = {}
_bars_cache_mem: dict[str, tuple[int, dict[str, dict[str, dict[str, str]]]]] = {}


def reset_chain_memo() -> None:
    """Clear the in-process chain JSON cache. Tests use this to swap fixtures."""
    _contracts_cache_mem.clear()
    _bars_cache_mem.clear()


def _load_contracts_cache(underlying: str) -> list[dict[str, Any]]:
    path = _contracts_path(underlying)
    if not path.exists():
        return []
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    cached = _contracts_cache_mem.get(underlying.upper())
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            return []
        rows = [r for r in data if isinstance(r, dict)]
        _contracts_cache_mem[underlying.upper()] = (mtime, rows)
        return rows
    except (OSError, ValueError) as exc:
        _log.warning(
            "backtest.chains.contracts_cache_read_failed",
            underlying=underlying,
            error=str(exc),
        )
        return []


def _save_contracts_cache(underlying: str, rows: list[dict[str, Any]]) -> None:
    _CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _contracts_path(underlying)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    tmp.replace(path)


def _bars_path(underlying: str) -> Path:
    return _CACHE_DIR / f"{underlying.upper()}.json"


def _load_bars_cache(underlying: str) -> dict[str, dict[str, dict[str, str]]]:
    """Returns ``{contract_symbol: {date_iso: {open, high, low, close, volume}}}``."""
    path = _bars_path(underlying)
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    cached = _bars_cache_mem.get(underlying.upper())
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        rows = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        _bars_cache_mem[underlying.upper()] = (mtime, rows)
        return rows
    except (OSError, ValueError) as exc:
        _log.warning(
            "backtest.chains.bars_cache_read_failed",
            underlying=underlying,
            error=str(exc),
        )
        return {}


def _save_bars_cache(
    underlying: str, rows: dict[str, dict[str, dict[str, str]]]
) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _bars_path(underlying)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    tmp.replace(path)


def _list_contracts_sync(
    underlying: str,
    exp_gte: date,
    exp_lte: date,
    *,
    strike_low: Decimal | None = None,
    strike_high: Decimal | None = None,
) -> list[dict[str, Any]]:
    """List every contract for ``underlying`` with expiration in [gte, lte].

    Queries both ACTIVE and INACTIVE statuses and unions the results so
    backtests covering the boundary between expired and currently-listed
    contracts get the full universe. ``strike_low`` / ``strike_high`` are
    pushed to Alpaca to reduce the page count for liquid names like SPY
    (which has thousands of strikes including far-OTM tail).
    """
    client = _get_trading_client()
    out: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "underlying_symbols": [underlying.upper()],
                "expiration_date_gte": exp_gte,
                "expiration_date_lte": exp_lte,
                "status": status,
                "limit": 10000,
                "page_token": page_token,
            }
            if strike_low is not None:
                kwargs["strike_price_gte"] = str(strike_low)
            if strike_high is not None:
                kwargs["strike_price_lte"] = str(strike_high)
            req = GetOptionContractsRequest(**kwargs)
            result = client.get_option_contracts(req)
            contracts = getattr(result, "option_contracts", None) or []
            for c in contracts:
                if c.symbol in seen_symbols:
                    continue
                seen_symbols.add(c.symbol)
                try:
                    _root, exp, opt_type, strike = parse_occ_symbol(c.symbol)
                except (ValueError, AttributeError):
                    continue
                out.append(
                    {
                        "symbol": c.symbol,
                        "underlying": underlying.upper(),
                        "option_type": opt_type,
                        "strike": str(strike),
                        "expiration": exp.isoformat(),
                    }
                )
            page_token = getattr(result, "next_page_token", None)
            if not page_token:
                break
    return out


def _fetch_option_bars_sync(
    contract_symbols: list[str],
    start: date,
    end: date,
) -> dict[str, list[OptionDailyBar]]:
    """Pull daily OHLCV bars for a batch of contract symbols."""
    if not contract_symbols:
        return {}
    client = _get_options_client()
    request = OptionBarsRequest(
        symbol_or_symbols=contract_symbols,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
        end=datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
    )
    result = client.get_option_bars(request)
    out: dict[str, list[OptionDailyBar]] = {}
    if hasattr(result, "data") and isinstance(result.data, dict):
        for sym, raw in result.data.items():
            bars_list: list[OptionDailyBar] = []
            for b in raw:
                ts = b.timestamp
                d = ts.date() if hasattr(ts, "date") else ts
                try:
                    bars_list.append(
                        OptionDailyBar(
                            asof=d,
                            open=Decimal(str(b.open)),
                            high=Decimal(str(b.high)),
                            low=Decimal(str(b.low)),
                            close=Decimal(str(b.close)),
                            volume=int(b.volume),
                        )
                    )
                except (ValueError, TypeError):
                    continue
            out[str(sym)] = bars_list
    return out


async def warm_contracts_for(
    underlying: str,
    backtest_start: date,
    backtest_end: date,
    *,
    strike_band_pct: Decimal = Decimal("0.40"),
) -> int:
    """Cache the contract list for ``underlying`` over the backtest window.

    Pulls every contract whose expiration falls between ``backtest_start``
    and ``backtest_end + lookahead``. Filters to strikes within
    ``strike_band_pct`` of the price RANGE the underlying traded over the
    window (min low to max high). This drops far-OTM tail strikes that
    nobody trades and reduces the contract universe by 5-10x for liquid
    names like SPY.
    """
    exp_gte = backtest_start
    exp_lte = backtest_end + timedelta(days=_MAX_DTE_LOOKAHEAD_DAYS)
    history = bars.get_history_until(underlying, backtest_end, lookback_days=10000)
    history = [b for b in history if backtest_start <= b.asof <= backtest_end]
    strike_low: Decimal | None = None
    strike_high: Decimal | None = None
    if history:
        lows = [b.low for b in history]
        highs = [b.high for b in history]
        strike_low = min(lows) * (Decimal("1") - strike_band_pct)
        strike_high = max(highs) * (Decimal("1") + strike_band_pct)
    fresh = await asyncio.to_thread(
        _list_contracts_sync,
        underlying,
        exp_gte,
        exp_lte,
        strike_low=strike_low,
        strike_high=strike_high,
    )
    existing = _load_contracts_cache(underlying)
    by_symbol = {r["symbol"]: r for r in existing}
    added = 0
    for r in fresh:
        if r["symbol"] not in by_symbol:
            by_symbol[r["symbol"]] = r
            added += 1
    if added > 0:
        _save_contracts_cache(underlying, list(by_symbol.values()))
        _log.info(
            "backtest.chains.warm_contracts",
            underlying=underlying,
            added=added,
            total=len(by_symbol),
            strike_low=str(strike_low) if strike_low else None,
            strike_high=str(strike_high) if strike_high else None,
        )
    return added


async def warm_bars_for(
    underlying: str,
    backtest_start: date,
    backtest_end: date,
    *,
    strike_range_pct: Decimal = Decimal("0.20"),
    batch_size: int = 50,
) -> int:
    """Cache daily option bars for the underlying over the backtest window.

    Selects contracts whose strike falls within ``strike_range_pct`` of
    the underlying's price RANGE (low to high) over the window, then
    fetches all selected contracts in ``OptionBarsRequest`` batches of
    ``batch_size``. Returns the number of (contract, date) pairs newly
    cached.

    The range-based selection (not "closest-to-end-spot") guarantees the
    strike grid covers strikes that were ATM or near-OTM at any point
    in the window, not just at the end. This was the root cause of the
    early-window backtest seeing only deep-ITM puts.
    """
    contracts = _load_contracts_cache(underlying)
    if not contracts:
        _log.info(
            "backtest.chains.no_contracts_cached",
            underlying=underlying,
            hint="call warm_contracts_for first",
        )
        return 0

    # Skip if the chains cache is already warm for this window. Heuristic:
    # the cache has data within 14 days of backtest_end. The previous warmup
    # path re-fetched every batch even when nothing was new, which thrashed
    # the API for ~50-100 sec per already-warmed symbol.
    existing_check = _load_bars_cache(underlying)
    if existing_check:
        latest_seen: date | None = None
        for sym_bars in existing_check.values():
            for d_str in sym_bars.keys():
                try:
                    d = date.fromisoformat(d_str)
                except ValueError:
                    continue
                if latest_seen is None or d > latest_seen:
                    latest_seen = d
        if latest_seen is not None and (backtest_end - latest_seen).days <= 14:
            _log.info(
                "backtest.chains.warm_bars_skipped_cached",
                underlying=underlying,
                latest_cached=latest_seen.isoformat(),
                backtest_end=backtest_end.isoformat(),
            )
            return 0

    history = bars.get_history_until(underlying, backtest_end, lookback_days=10000)
    history = [b for b in history if backtest_start <= b.asof <= backtest_end]
    if not history:
        _log.warning(
            "backtest.chains.no_underlying_close",
            underlying=underlying,
            asof=backtest_end.isoformat(),
        )
        return 0
    min_low = min(b.low for b in history)
    max_high = max(b.high for b in history)
    strike_floor = min_low * (Decimal("1") - strike_range_pct)
    strike_ceiling = max_high * (Decimal("1") + strike_range_pct)

    selected_symbols: list[str] = []
    for r in contracts:
        try:
            strike = Decimal(r["strike"])
        except (KeyError, ValueError):
            continue
        if strike < strike_floor or strike > strike_ceiling:
            continue
        selected_symbols.append(r["symbol"])

    existing_bars = _load_bars_cache(underlying)
    added = 0
    for i in range(0, len(selected_symbols), batch_size):
        batch = selected_symbols[i : i + batch_size]
        try:
            fetched = await asyncio.to_thread(
                _fetch_option_bars_sync, batch, backtest_start, backtest_end
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            _log.warning(
                "backtest.chains.bars_fetch_failed",
                underlying=underlying,
                batch_size=len(batch),
                error=str(exc),
            )
            continue
        for sym, blist in fetched.items():
            sym_bars = existing_bars.setdefault(sym, {})
            for b in blist:
                key = b.asof.isoformat()
                if key not in sym_bars:
                    sym_bars[key] = {
                        "open": str(b.open),
                        "high": str(b.high),
                        "low": str(b.low),
                        "close": str(b.close),
                        "volume": str(b.volume),
                    }
                    added += 1
    if added > 0:
        _save_bars_cache(underlying, existing_bars)
        _log.info(
            "backtest.chains.warm_bars",
            underlying=underlying,
            added=added,
            total_symbols=len(existing_bars),
            strike_floor=str(strike_floor),
            strike_ceiling=str(strike_ceiling),
        )
    return added


def _spread_frac_for_volume(volume: int) -> Decimal:
    if volume >= 1000:
        return _SPREAD_FRAC_HIGH_VOLUME
    if volume >= 100:
        return _SPREAD_FRAC_MED_VOLUME
    if volume > 0:
        return _SPREAD_FRAC_LOW_VOLUME
    return _SPREAD_FRAC_ZERO_VOLUME


def _build_contract(
    meta: ContractMeta,
    bar: OptionDailyBar,
    underlying_spot: Decimal,
    rate: float,
    asof: date,
) -> OptionContract | None:
    """Materialise an ``OptionContract`` from a bar + meta + reconstructed Greeks.

    Returns ``None`` when the bar's close is below the minimum tradable
    price floor, when t_years would be non-positive (already expired),
    or when the IV solver fails to converge.
    """
    if bar.close < _MIN_OPTION_PRICE:
        return None
    days_to_exp = (meta.expiration - asof).days
    if days_to_exp <= 0:
        return None
    t_years = days_to_exp / 365.0
    mid = bar.close
    spread_frac = _spread_frac_for_volume(bar.volume)
    # Use the bar's actual intraday trade range as a floor on the spread.
    # The IQR of intraday option trade prints is roughly the bid-ask gap
    # observed across the day; (high - low) / (4 * close) approximates a
    # conservative half-spread when the bar saw genuine two-sided trading.
    # Where this exceeds the volume-bucket value the bar wins; where the
    # bar was a single price the bucket wins. Calibrated 2026-05-08
    # against real Alpaca trade prints.
    if mid > 0 and bar.high > bar.low:
        range_frac = (bar.high - bar.low) / (Decimal("4") * mid)
        spread_frac = max(spread_frac, range_frac)
    bid = mid * (Decimal("1") - spread_frac)
    ask = mid * (Decimal("1") + spread_frac)
    if bid < _MIN_OPTION_PRICE:
        bid = _MIN_OPTION_PRICE
    greeks = reconstruct_greeks(
        option_type="call" if meta.option_type == "call" else "put",
        market_price=float(mid),
        spot=float(underlying_spot),
        strike=float(meta.strike),
        rate=rate,
        t_years=t_years,
    )
    if greeks is None:
        return None
    return OptionContract(
        symbol=meta.symbol,
        underlying=meta.underlying,
        option_type=meta.option_type,
        strike=meta.strike,
        expiration=meta.expiration,
        bid=bid.quantize(Decimal("0.01")),
        ask=ask.quantize(Decimal("0.01")),
        last=mid.quantize(Decimal("0.01")),
        delta=Decimal(str(greeks.delta)),
        gamma=Decimal(str(greeks.gamma)),
        theta=Decimal(str(greeks.theta)),
        vega=Decimal(str(greeks.vega)),
        implied_volatility=Decimal(str(greeks.iv)),
    )


def get_chain(
    underlying: str,
    asof: date,
    *,
    expiration: date | None = None,
    max_dte_lookahead: int = _MAX_DTE_LOOKAHEAD_DAYS,
    strike_band_pct: Decimal = _STRIKE_BAND_PCT,
) -> list[OptionContract]:
    """Build the option chain for ``underlying`` as it would have looked at ``asof``.

    Pulls from the local bars + contracts cache only. Asserts no row
    post-dates ``asof``: any contract with bar.asof > asof or any
    underlying close from after ``asof`` raises ``LeakageError``.
    """
    upper = underlying.upper()
    contracts_meta = _load_contracts_cache(upper)
    bars_cache = _load_bars_cache(upper)
    if not contracts_meta or not bars_cache:
        return []

    underlying_close = bars.get_close_on_or_before(upper, asof)
    if underlying_close is None:
        return []
    close_date, spot = underlying_close
    if close_date > asof:
        raise LeakageError(
            f"chains.get_chain underlying close {close_date} > asof {asof}"
        )
    rate = rates.get_rate(asof)

    band_low = spot * (Decimal("1") - strike_band_pct)
    band_high = spot * (Decimal("1") + strike_band_pct)

    out: list[OptionContract] = []
    for meta_raw in contracts_meta:
        try:
            sym = meta_raw["symbol"]
            opt_type = meta_raw["option_type"]
            strike = Decimal(meta_raw["strike"])
            exp = date.fromisoformat(meta_raw["expiration"])
        except (KeyError, ValueError):
            continue
        if expiration is not None and exp != expiration:
            continue
        if exp <= asof:
            continue
        days_to_exp = (exp - asof).days
        if days_to_exp > max_dte_lookahead:
            continue
        if strike < band_low or strike > band_high:
            continue
        sym_bars = bars_cache.get(sym, {})
        if not sym_bars:
            continue
        # Find the latest bar on or before asof. The strategy expects a
        # quoteable contract so we will not synthesize from a 7-day-stale
        # bar; pick the bar at asof itself when present, else the most
        # recent prior trading day.
        asof_str = asof.isoformat()
        chosen_date_str: str | None = None
        for d in sorted(sym_bars.keys(), reverse=True):
            if d <= asof_str:
                chosen_date_str = d
                break
        if chosen_date_str is None:
            continue
        bar_dict = sym_bars[chosen_date_str]
        bar_asof = date.fromisoformat(chosen_date_str)
        if bar_asof > asof:
            raise LeakageError(
                f"chains.get_chain returned bar {bar_asof} > asof {asof} for {sym}"
            )
        # Skip contracts whose latest bar is more than 5 trading days
        # stale: a contract that has not traded in over a week should
        # not be available for entry under a fail-closed liquidity policy.
        if (asof - bar_asof).days > 7:
            continue
        try:
            bar = OptionDailyBar(
                asof=bar_asof,
                open=Decimal(bar_dict["open"]),
                high=Decimal(bar_dict["high"]),
                low=Decimal(bar_dict["low"]),
                close=Decimal(bar_dict["close"]),
                volume=int(bar_dict["volume"]),
            )
        except (KeyError, ValueError):
            continue
        meta = ContractMeta(
            symbol=sym,
            underlying=upper,
            option_type=opt_type,
            strike=strike,
            expiration=exp,
        )
        contract = _build_contract(meta, bar, spot, rate, asof)
        if contract is not None:
            out.append(contract)
    out.sort(key=lambda c: (c.expiration, c.strike, c.option_type))
    return out


def make_historical_chain_fetcher(
    asof: date,
) -> HistoricalChainFetcher:
    """Bind ``asof`` and return a callable matching the live ``ChainFetcher``."""
    return HistoricalChainFetcher(asof=asof)


@dataclass
class HistoricalChainFetcher:
    """Async wrapper that exposes ``ChainFetcher`` shape for the strategy.

    The strategy code calls ``await chain_fetcher(symbol, expiration)``;
    we honour that signature, look up the cache at the bound asof, and
    return the materialised ``OptionContract`` list. No I/O happens
    inside the call (the cache is local), so wrapping in
    ``asyncio.to_thread`` is unnecessary; we keep the signature async
    for compatibility with the production type alias.
    """

    asof: date

    async def __call__(
        self, symbol: str, expiration: date | None = None
    ) -> list[OptionContract]:
        return get_chain(symbol, self.asof, expiration=expiration)
