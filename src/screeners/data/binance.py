"""Binance USDⓈ-M futures history.

Two things shape this module:

* Klines carry `takerBuyBaseAssetVolume`, so CVD is recoverable from history
  here. Bybit does not report it, which is why CVD filters are Binance-only.
* There is no open-interest websocket and `/futures/data/openInterestHist`
  retains only ~30 days at 5m granularity. That is the hard ceiling on how far
  back the OI screener can be backtested.

`/fapi/*` and `/futures/data/*` bill against *separate* IP limit pools, so they
get separate budgets.
"""
from __future__ import annotations

import logging
import time

from ..models import Bar, Exchange
from .http import WeightBudget, get_json

log = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"

# REQUEST_WEIGHT: 2400/min, confirmed live from /fapi/v1/exchangeInfo.
FAPI_BUDGET = WeightBudget(2400, window_s=60.0, name="binance /fapi")
# /futures/data/* is capped separately at 1000 requests / 5 min per IP.
DATA_BUDGET = WeightBudget(1000, window_s=300.0, name="binance /futures/data")

KLINE_LIMIT = 1500
OI_LIMIT = 500
OI_RETENTION_DAYS = 30
# 30d of 5m samples is 8640 rows = 18 pages; leave generous headroom.
OI_MAX_PAGES = 60

# Milliseconds per supported interval.
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def universe(quote: str = "USDT") -> list[str]:
    """Perpetual, currently-trading coin symbols. ~525 for USDT as of 2026-09."""
    info = get_json(f"{FAPI}/fapi/v1/exchangeInfo", budget=FAPI_BUDGET, weight=1)
    return sorted(
        s["symbol"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == quote
        and s.get("status") == "TRADING"
        # Only coins. Binance also lists index perpetuals (BTC dominance and
        # similar, underlyingType "INDEX"), which are not tradable assets.
        and s.get("underlyingType") == "COIN"
    )


def top_by_turnover(n: int, quote: str = "USDT") -> list[str]:
    """The n most-traded perps by 24h quote volume. Used to trim the backtest universe."""
    tickers = get_json(f"{FAPI}/fapi/v1/ticker/24hr", budget=FAPI_BUDGET, weight=40)
    perps = set(universe(quote))
    rows = [t for t in tickers if t["symbol"] in perps]
    rows.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    return [t["symbol"] for t in rows[:n]]


def kline_weight(limit: int) -> int:
    """Binance bills /fapi/v1/klines by the `limit` REQUESTED, not rows returned:
    [1,100) -> 1, [100,500) -> 2, [500,1000] -> 5, above -> 10."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Bar]:
    """OHLCV + taker-buy volume, paginated forward. `oi` is left None here.

    Each page asks for only as many bars as remain. Always asking for 1500
    cost weight 10 even for a two-bar live refresh -- 10x the bill, which is
    what capped the live screener at a fraction of the universe.
    """
    step = INTERVAL_MS[interval]
    out: list[Bar] = []
    cursor = start_ms
    while cursor < end_ms:
        want = min(KLINE_LIMIT, max(1, -(-(end_ms - cursor) // step)))
        rows = get_json(
            f"{FAPI}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor,
             "endTime": end_ms, "limit": want},
            budget=FAPI_BUDGET, weight=kline_weight(want),
        )
        if not rows:
            break
        for r in rows:
            out.append(Bar(
                ts=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]), taker_buy=float(r[9]),
            ))
        last = int(rows[-1][0])
        if last + step <= cursor:  # no forward progress; bail rather than spin
            break
        cursor = last + step
        if len(rows) < want:
            break
    return [b for b in out if start_ms <= b.ts < end_ms]


def open_interest(symbol: str, period: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """(timestamp, open interest in base units). Only ~30 days are retained.

    `/futures/data/openInterestHist` is **end-anchored**: given a range it
    returns the newest `limit` rows in it, not the oldest. Paging forward from
    `startTime` therefore returns rows ending at `endTime` on the first call
    and terminates immediately -- a 30-day request silently yields the last
    ~1.7 days. So walk backwards, moving `endTime` to just before the oldest
    row each round.
    """
    step = INTERVAL_MS[period]
    # Asking before retention is a hard 400, which would drop the symbol
    # entirely -- including its klines. Clamp instead.
    floor_ms = int(time.time() * 1000) - OI_RETENTION_DAYS * 86_400_000
    lo = max(start_ms, floor_ms + step)
    if lo >= end_ms:
        return []

    seen: dict[int, float] = {}
    cursor_end = end_ms
    for _ in range(OI_MAX_PAGES):
        rows = get_json(
            f"{FAPI}/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "startTime": lo,
             "endTime": cursor_end, "limit": OI_LIMIT},
            budget=DATA_BUDGET, weight=1,
        )
        if not rows:
            break
        for r in rows:
            seen[int(r["timestamp"])] = float(r["sumOpenInterest"])
        oldest = min(int(r["timestamp"]) for r in rows)
        if oldest <= lo or len(rows) < OI_LIMIT:
            break
        nxt = oldest - 1
        if nxt >= cursor_end:  # no backward progress; refuse to spin
            break
        cursor_end = nxt
    return [(t, seen[t]) for t in sorted(seen) if start_ms <= t < end_ms]


EXCHANGE = Exchange.BINANCE
