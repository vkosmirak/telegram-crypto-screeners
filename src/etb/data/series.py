"""Assemble per-symbol Bar series with open interest merged in, and cache them.

Neither venue puts open interest on a kline, so OI arrives as its own
timestamped series and has to be attached. OI is only ever available at 5m or
coarser, while the pump rule wants 1m bars -- so OI is forward-filled onto
finer bars: each bar takes the most recent OI sample at or before its open.
That is the honest reading. It never looks ahead.
"""
from __future__ import annotations

import bisect
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..models import Bar, Exchange
from . import binance, bybit
from .binance import INTERVAL_MS
from .cache import Cache

log = logging.getLogger(__name__)

ADAPTERS = {Exchange.BINANCE: binance, Exchange.BYBIT: bybit}


def merge_oi(bars: list[Bar], oi: list[tuple[int, float]],
             interval_ms: int = 0) -> list[Bar]:
    """Forward-fill OI onto bars, as of each bar's CLOSE.

    A rule acts at the bar close, and compares OI against a close-to-close
    price change, so OI has to be read at the same instant. Anchoring on the
    bar's *open* instead (which this used to do) left OI describing a window
    shifted one full bar from the price it was compared with -- enough to make
    `price_up` pass on exactly the OI-up/price-down setup it exists to reject.

    Still no lookahead: a sample stamped at or before the close is known by
    the close. Bars before the first sample keep oi=None.
    """
    if not oi:
        return bars
    times = [t for t, _ in oi]
    values = [v for _, v in oi]
    out: list[Bar] = []
    for b in bars:
        i = bisect.bisect_right(times, b.ts + interval_ms) - 1
        out.append(b if i < 0 else Bar(
            ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
            volume=b.volume, taker_buy=b.taker_buy, oi=values[i],
        ))
    return out


@dataclass(slots=True)
class Series:
    """One symbol on one venue over one window, at one bar interval."""

    exchange: Exchange
    symbol: str
    interval: str
    bars: list[Bar]

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def has_cvd(self) -> bool:
        return bool(self.bars) and all(b.has_cvd for b in self.bars)

    @property
    def has_oi(self) -> bool:
        return any(b.oi is not None for b in self.bars)

    def cvd(self) -> list[float] | None:
        """Cumulative volume delta, rebased to 0 at the start of the window."""
        if not self.has_cvd:
            return None
        total = 0.0
        out = []
        for b in self.bars:
            d = b.delta
            if d is None:
                # A hole mid-series would flat-line CVD through the gap and
                # quietly hand the filter a wrong answer. Refuse instead.
                return None
            total += d
            out.append(total)
        return out


def load_series(
    exchange: Exchange,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    with_oi: bool = True,
    oi_period: str = "5m",
    cache: Cache | None = None,
) -> Series:
    """Fetch (or read from cache) one symbol's bars with OI attached."""
    adapter = ADAPTERS[exchange]
    ex = exchange.value

    raw = cache.get("kline", ex, symbol, interval, start_ms, end_ms) if cache else None
    if raw is None:
        bars = adapter.klines(symbol, interval, start_ms, end_ms)
        if cache:
            cache.put("kline", ex, symbol, interval, start_ms, end_ms,
                      [[b.ts, b.open, b.high, b.low, b.close, b.volume, b.taker_buy] for b in bars])
    else:
        bars = [Bar(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
                    volume=r[5], taker_buy=r[6]) for r in raw]

    if with_oi:
        oi_raw = cache.get("oi", ex, symbol, oi_period, start_ms, end_ms) if cache else None
        if oi_raw is None:
            oi = adapter.open_interest(symbol, oi_period, start_ms, end_ms)
            if cache:
                cache.put("oi", ex, symbol, oi_period, start_ms, end_ms, oi)
        else:
            oi = [(int(t), float(v)) for t, v in oi_raw]
        bars = merge_oi(bars, oi, INTERVAL_MS.get(interval, 0))

    return Series(exchange, symbol, interval, bars)


def load_many(
    exchange: Exchange,
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    with_oi: bool = True,
    oi_period: str = "5m",
    cache: Cache | None = None,
    workers: int = 8,
    on_progress=None,
) -> dict[str, Series]:
    """Fetch many symbols concurrently. The shared WeightBudget keeps the pool legal."""
    out: dict[str, Series] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(load_series, exchange, s, interval, start_ms, end_ms,
                        with_oi=with_oi, oi_period=oi_period, cache=cache): s
            for s in symbols
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                series = fut.result()
            except Exception as e:  # one bad symbol must not sink the run
                log.warning("%s %s: %s", exchange.value, sym, e)
                continue
            if series.bars:
                out[sym] = series
            if on_progress:
                on_progress(done, len(symbols), sym)
    return out
